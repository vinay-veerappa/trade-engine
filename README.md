# trade-engine

Generic trading engine: event-sourced ledger, OMS, risk layer, simulator, and venue adapters.

## Architecture & Invariants

This engine is designed around strict invariants (I1–I13):

1. **I1: One writer, one truth.** The engine's event ledger is the only source of state. Sinks are fed from it.
2. **I2: State = fold(events).** Nothing is updated in place; every state transition is an appended event; restarts replay the log.
3. **I3: Idempotent by persisted key.** Every command carries a `command_id` stored in the ledger; replaying a command is a no-op.
4. **I4: Single instance.** Exactly one process per ledger (OS file lock); a second process refuses to start.
5. **I5: Refuse, never guess.** Missing or stale price, bar, earnings date, regime, or unknown symbol refuses the action and records why. No defaults.
6. **I6: Instruments are resolved objects, not strings.** Books are keyed by resolved instrument (OCC symbol for options) and carry multipliers.
7. **I7: Time is injected.** All engine components read from an injected `Clock`; replay uses bar timestamps, live uses wall time. All timestamps are UTC.
8. **I8: Every account owns everything it holds.** Shares written against must be in that account's ledger with cost paid from its cash.
9. **I9: Expiry/assignment on the settle.** Processed after the 16:00 ET close on the official close price, booking intrinsic value per leg.
10. **I10: Unknown broker state = pending, never filled.** Environment (`sim`, `paper`, `live`) is declared by the adapter and proven at connect; live requires explicit acknowledgment in configuration.
11. **I11: Every decision is recorded with its reason.** Accepted, refused (and by which specific rule), or skipped. Every rule is evaluated without short-circuiting.
12. **I12: A sink confirms delivery, not a 200.** Outbox drains in order and stops at the first failure until confirmed.
13. **I13: Strategies know nothing about brokers; adapters know nothing about strategies.** Clean separation of concerns.

## Risk Evaluation

`trade_engine.risk.RiskEngine` evaluates equity intents against explicit
`AccountRiskRules`, market/account measurements in `RiskContext`, and `VenueRiskRails`.
Missing measurements (including regime, price, earnings distance, and daily P&L) fail
their rule instead of being inferred. `RiskVerdict` includes every rule result and the
approved whole-share quantity when accepted.

Account rule fields ending in `_frac` store Decimal fractions (for example,
`Decimal("0.0075")` is 0.75%). `AccountRiskRules.from_mapping` is the config boundary:
percentage-valued keys use explicit strings such as `risk_per_trade: "0.75%"`;
bare numeric percentages and unknown keys are rejected. Rule-specific sanity caps reject
slipped decimal points (including per-trade risk above 5%). Measured drawdown is a
non-negative fraction; signed session P&L remains a separate measurement. The intent's
`quantity_rule` is honored (`risk_0.75pct`, `notional_5pct` or `fixed_10`), but account risk
limits remain authoritative: `notional_<x>pct` sizes `floor(equity * x% / entry)`, may not exceed
the position cap, and its stop-distance risk must still fit the (regime/drawdown-scaled) risk
budget. By default a `risk_<x>pct` size over the position cap is refused; the optional
`clamp_to_position_cap: true` rule (a real boolean) reduces it to the cap instead and says so in
the `max_position` verdict reason. Fixed sizes are never clamped.

Paper/live `VenueRiskRails` require an allowlist, quantity and daily-order caps, daily
loss limit, trading hours, duplicate protection, and a persistent kill switch. Risk
control changes are append-only ledger events; provide unique command IDs to
`engage_kill_switch` and `release_kill_switch`. `RiskEngine.evaluate` can append
drawdown-brake and suspension transitions to the ledger; it is not a pure read. A
drawdown suspension remains latched until the configured recovery threshold is reached.
Trading hours are intersected with the configured exchange calendar, including holidays
and early closes.

## Order Time in Force

`OrderIntent.entry_tif` defaults to `DAY` and `exit_tif` to `GTC`, so the protective stop and
targets of a multi-day swing trade (equity or option) stay live after the close. Set
`entry_tif=TimeInForce.GTC` for an entry that should rest until filled or cancelled. Brackets
accept only `DAY` and `GTC`; `OrderManager.create_bracket` refuses the whole bracket before
the entry is placed when the venue does not support either value.

## Entry Type

`OrderIntent.entry_type` defaults to `LIMIT`: buy at or below `entry_price` (sell at or
above it for a short), which fills at once when the market is already through that price.
Set `entry_type=OrderType.STOP` for a breakout trigger: the entry rests as a stop at
`entry_price` and fills only once price trades through it (at the open when it gaps over).
A venue without native stops refuses a stop entry before anything is persisted; an emulated
entry would need a live price feed that an EOD bracket does not have.

Set `entry_type=OrderType.STOP_LIMIT` with `entry_limit_price` for a breakout with a chase
limit ("buy-stop above the high, skip if it opens more than 0.5 ATR over the trigger"):
the stop triggers at `entry_price`, then the order works as a limit at `entry_limit_price`.
A gap open over the limit does not fill; if price later trades back to or under the limit
while the order is working, it fills. `entry_limit_price` is required for `STOP_LIMIT` and
refused for other entry types; a buy's limit must be at or above `entry_price`, a sell's
at or below it. The limit joins the bracket fingerprint only for `STOP_LIMIT`, so existing
brackets keep theirs. A venue without native stop-limit orders refuses the entry before
anything is persisted, for the same reason as a stop entry.

## Target Fractions

`OrderIntent.target_fractions` sets the share of the position each profit target exits,
in target order. Left as `None`, the whole position splits evenly across the targets.
Fractions summing below 1 leave a runner that only the protective stop (or a strategy exit)
closes: `target_fractions=(Decimal("1") / 3,)` sells a third at the target and trails the
rest. Whole shares round by largest remainder over the targets and the runner; a bracket
too small to give every target at least one share is refused. A partial entry fill keeps
the same proportions, so the runner is never absorbed into the targets.

## Strategy Exits at the Close

A strategy may define `manage_positions(brackets, context)`. The EOD runner calls it once
per account after marks and before D+1 entries, passing an `OpenBracket` for every bracket
that still holds open quantity: average entry, entry session, `sessions_held`, current stop,
targets filled and still open, and the session's last close. It returns exit actions from
`trade_engine.domain.exits`, which the OMS applies:

- `MoveStop(entry_order_id, stop_price, reason, command_id)` replaces the protective stop.
  Stops only tighten; one that would widen the bracket's risk refuses and fails the run.
- `ClosePosition(entry_order_id, reason, command_id)` sends a DAY market order for the whole
  open quantity, which fills at the next session's open. The stop keeps protecting until it
  fills; its fill cancels the remaining stop and targets, and a stop fill first cancels it.
- `ReducePosition(entry_order_id, fraction, reason, command_id)` sends a DAY market order for
  `floor(open quantity × fraction)`, `0 < fraction < 1`, which fills at the next session's
  open: "a third after 5 days", "half at the day-3 close". It replaces the resting profit
  targets, which it cancels (the partial is taken at the target or after N days, whichever
  comes first). Once it fills, the protective stop shrinks to the remaining quantity and keeps
  working. A fraction that rounds down to nothing refuses, as does a reduce while a close or
  another reduce is still working (and a close while a reduce is working). Any stop fill,
  full or partial, before the reduce fills cancels it.

An action naming anything but an open bracket of the account refuses. Command ids make a
replayed action a no-op (I3).

## Equity Simulation

`trade_engine.sim.SimBroker` is a single-account paper venue driven only by explicit
one-minute `Bar` values. Configure slippage in basis points and call `connect()` before
submitting orders. Bars are stamped at their opening minute; no fill is inferred from a
cached or prior price. Each session must start with its exchange-calendar open bar, and
missing intraday bars, a session that ends before its last regular bar, or skipped
sessions raise `MissingBarError`. Bars outside regular hours keep the sequence contiguous
but never fill an order. DAY orders work the session they were entered in, or the next
session when entered after the close, and expire at that session's close. Parent-linked exits are capped at
the simulator's current position while the OMS reconciles their fills. If a stop and a
target are both touched in one bar, the stop wins because the bar does not reveal the
intrabar price path; when multiple targets are touched, they fill in numeric target order.
A protective stop the entry's own bar reached fills in that bar (at the stop, or at the
entry price when the entry was already through it); targets the entry bar reached wait
for a later bar, since the bar may have reached them before the entry.
Callers must reconcile after every bar: an exit submitted after bars following its entry
fill were simulated is rejected, because those bars can no longer be matched.
A stop-limit triggers on a bar that trades at or through its stop and then works as a
limit for the rest of its life. When the bar opens at or through the stop it fills at the
open if that is within the limit, at the limit if the bar's range comes back to it, and
not at all otherwise. A stop first reached inside the bar fills at the stop (or, with a
limit short of the stop, waits for a later bar). Like limit fills, stop-limit fills take
the configured slippage but never fill past the limit.
Time-stop exits use market-on-open orders (`TimeInForce.OPG`) and fill only at the first
eligible 09:30 ET session open after submission; they expire if that open passes
without being simulated. A missing expected open raises `MissingBarError`.

SimBroker keeps its book in memory, so every new process starts empty. The EOD runner
restores an empty SimBroker from the ledger before replay: working orders (with their
original submit times), the rest of their brackets, those orders' fills, and open
positions. The runner then checks that the venue holds every order the ledger says is
working, and refuses if it doesn't. An order left `PENDING_UNKNOWN`, or one with no
recorded submission, can't be restored and is refused too. So is an unfilled stop-limit
that bars since its submission may already have triggered (a GTC one that lived through a
session): whether it triggered is not in the ledger. A DAY stop-limit entry placed after
the close restores untriggered and is simulated from the next open.

## Development

Requires Python >= 3.13.

### Local Setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .[test]
```

### Running Tests & Local CI

```powershell
pytest -q
python tools/ci_local.py
```

### Package Version Check

```powershell
python -m trade_engine --version
```

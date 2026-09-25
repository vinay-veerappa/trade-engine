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

## Option Chains

No historical intraday option quotes exist, so option decisions and paper fills read a
`trade_engine.market_data.ChainSnapshot`: one underlying's `OptionQuote`s as they stood
at the snapshot's `as_of`, with the spot, rate and dividend yield the source quoted. No
quote may be stamped after its snapshot, and every contract must be listed under the
snapshot's underlying. `require_fresh(now, max_age_seconds)` refuses a snapshot older
than the caller allows, and one from after `now`. `split_by_quote_age` separates quotes
nobody has updated lately from the rest.

`ChainSnapshotStore` keeps one gzipped JSON file per snapshot under
`<root>/<UNDERLYING>/<as_of>.json.gz`; a full SPX chain is about 0.46 MB, against 4.4 MB
uncompressed. Storing the same snapshot again is a no-op; a different snapshot at the
same instant refuses. `latest(underlying, now, max_age_seconds)`
returns the newest snapshot taken at or before `now`, so a replay cannot see a later chain
from its own day. Nothing stored, or a stale snapshot, raises `StaleDataError`.

`trade_engine.domain.option_roots` says what a root trades. `SPX` (monthlies) is
European, cash-settled and AM-settled on the expiry-day open, and last trades the
session before. `SPXW` is the same index, PM-settled on the close. Any other root is an
equity option: American, physically settled, PM. Other index roots (`NDX`, `RUT`, `VIX`,
`XSP`, ...) refuse until they are modelled. `settlement_instant` refuses an expiry date
that is not a session.

`trade_engine.market_data.greeks` gives Black-Scholes-Merton prices, implied volatility and
greeks through `vollib`. Time runs to the settlement instant, in years of 365 days. Rate
and dividend yield are required inputs, never defaults. A price below intrinsic, or a
contract already settled, raises `GreeksUnavailable`. American options are priced as
European. `OptionQuote.greeks` holds a vendor's published greeks when a quote carries
them (`source="vendor"`), and `model_greeks` returns `source="model"`.

## Options Lifecycle

`lifecycle.LifecyclePass(ledger, clock, calendar, settlements, dividends=, quotes=)`
settles a session's option positions after its close (I9). `run(session)` appends one
`OptionLifecycle` event per position settled, filed as `Expiry`, `Exercise` (a long
position) or `Assignment` (a short one), with command id
`lifecycle:<account>:<occ>:<session>`, so a re-run appends nothing.

- **At expiry** a contract settles on its official price: the close for PM-settled
  roots, the opening settlement for `SPX` monthlies. At least $0.01 in the money it is
  exercised or assigned (the OCC's exercise by exception); otherwise it expires
  worthless.
- **Physical delivery** moves shares at the strike, and each lot's premium goes into the
  shares' price. An assigned put buys at strike − credit, an assigned call sells at
  strike + credit, an exercised call buys at strike + debit, and an exercised put sells
  at strike − debit. The option leg closes with no P&L of its own. So a cash-secured put
  assigned in the money holds shares at a cost basis of strike − credit, and a put spread
  through both strikes realises exactly its maximum loss.
- **Cash settlement** (`SPX`, `SPXW`) closes the option at its intrinsic value and moves
  that cash.
- **Early assignment before an ex-dividend date.** A short American call in the money at
  the close, whose shares go ex next session, is assigned when the dividend is larger
  than the call's bid minus its intrinsic value (what the holder would give up by
  exercising rather than selling).

The official close is an input (`Settlements`; `FixedSettlements` ships). The close of
the 15:59 one-minute bar is not the official close, and `SPX`'s opening settlement isn't
its first print. So are dividends (`FixedDividends`, or `CorporateActionDividends` over
a provider) and option quotes (`SnapshotQuotes` over the chain snapshot store). The pass
refuses, and writes nothing for any account, in these cases:
- the clock is before the close;
- a price, dividend or quote it needs is unknown, or is stamped after the clock;
- a settlement price is stamped before its settlement instant;
- an option expired in an earlier session and was never settled;
- a short American call faces a decision with no dividend source.

The fold (`ledger.state`) refuses a lifecycle event that contradicts the book or the
rules: no open position, the wrong side, more contracts than are held, an in-the-money
contract expiring worthless, an out-of-the-money one exercised or assigned, or a
European contract assigned early. Assignment fees are zero. Pin risk and partial
assignment are not modelled.

## Option Margin

`metrics.account_margin(state, overrides=, underlying_prices=)` margins options as
strategies. Each underlying's options, together with whole lots of its shares, are
grouped into strategies. Each strategy is then margined as a unit
(`metrics.option_margin`). The matching and the formulas are ported from LEAN's
`OptionStrategyPositionGroupBuyingPowerModel` and `OptionMarginModel`. Any shares no
strategy uses are margined as plain stock.

| Strategy | Maintenance (initial where it differs) |
|---|---|
| Naked put / call | premium + max(10% of strike (put) or underlying (call), 20% of underlying − OTM); 15% for an index |
| Vertical, diagonal | width when the long is further out of the money, else 0; initial adds a net debit |
| Covered call | max(ITM + stock margin at min(price, strike), stock margin); initial 0.8 × call value + stock initial |
| Protective put / call | min(10% of strike + OTM, stock maintenance); initial is the stock's |
| Covered put | stock initial + ITM |
| Collar | min(10% of put strike + put OTM, 25% of call strike); initial stock initial + call ITM |
| Calendar | 0 (long); the short leg's naked margin (short) |
| Long option | 0; initial is its premium |

Each strategy also records `cash_secured`, the cash an account without margin must hold
for it. That's the strike for a naked put and the width for a credit spread. Where cash
can't secure it at all, as with a naked call, it's `None`.

Where this differs from LEAN:
- **Diagonals.** LEAN has none. Its calendars need equal strikes, so a poor man's covered
  call would split into a naked call plus a long call. Here `Call/Put Diagonal Spread`
  covers a short with a long of the same right, a different strike and a later expiry.
  A long that expires before its short covers nothing.
- **Cheapest grouping.** LEAN keeps its first greedy grouping, which can pair the wrong
  legs. Two bull put spreads, 95/90 and 85/80, come out as a 90/85 bear put spread plus
  a 95/80 bull put spread: 1,500 instead of 1,000. So every grouping is also searched,
  and the one with the least margin is kept. On a tie, or when the book is too large to
  search, LEAN's grouping stands.
- **Marks.** LEAN's maintenance for a naked option uses the premium it was sold for.
  Here it uses the current mark.

Missing inputs refuse: an option or underlying without a price, a fractional contract,
mixed contract multipliers on one underlying, or a `Combo` held as a single position.

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

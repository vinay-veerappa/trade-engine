# Broker platform plan: trade-engine as the one broker port

Status: plan, 2026-11-11. Owner decision the same day: trade-engine is where broker
access, the canonical instrument and the canonical account live. riskguard, Vela and
the NT8/TV surfaces are clients of it. Tradovate (through ntx) is the first broker;
Schwab and IBKR follow on the same contract.

This plan follows the engine's existing rules: invariants I1-I13 (README), and the Rust
port decisions D1-D7 (`docs/RUST_PORT.md`). In particular, a rule lives once, in
`te_core`, and Python keeps only data carriers and I/O (D2, D3). Money crosses the
boundary as strings and never as floats (D6).

## Why here

- `BrokerAdapter` (`src/trade_engine/interfaces/broker.py`) already has the shape
  needed: `env` proven at connect, `VenueIdentity{account_id, env, connected_at,
  broker_name}`, capabilities, and `orders/fills/positions/cash_events(since)`. It
  also has refuse-never-approximate (`UnsupportedCapability`, I5) and
  pending-never-filled (I10).
- The OMS already calls RiskGuard at the submit chokepoint (`911994b`, `0d1fe64`,
  `5d47af7`). The engine and riskguard are already joined in the right direction: the
  engine is the broker side and riskguard is the rule side.
- D2 already names Vela and NT8 as clients of the engine.

Gaps this plan closes:

1. There is no futures instrument. `Instrument` covers only `Equity`, `OptionContract`
   and `Combo`.
2. There is no Tradovate adapter. The only real adapter is `tos_paper`.
3. `BrokerAdapter` has no account list. It knows its own identity, but it cannot answer
   "which accounts exist at this broker" or report when that list changes.
4. Nothing publishes accounts or instruments to riskguard. riskguard keeps its own flat
   fleet, which is the L-246 defect.

## The three planes (fixed)

| Plane | What it is | Who owns it |
|---|---|---|
| Broker | Holds the money: Tradovate, Schwab, IBKR. It is the truth for accounts, positions, fills and cash. | One adapter per broker, in trade-engine. |
| Platform | Where the owner clicks: TV, NT8 Desktop, Tradovate web, thinkorswim, TWS. It is a route to a broker account, and the place to capture an order before it reaches the broker. | Surface code (riskguard surfaces). It reads its account scope from the registry. |
| Core | Rules, budgets, lockouts and strikes, keyed by `broker:accountId` and the canonical instrument. | riskguard-service (rules), fed by trade-engine (facts). |

A rule attaches to an account key and never to a route. A trip on one route applies to
every route of that account. Platform-local accounts (NT8 Sim101, SimFan*, Playback,
TV Paper) are `local:<platform>:<name>` and stay in their own platform's scope.

## Invariants added by this plan

- **B1 One source per broker.** Exactly one adapter is the authority on that broker's
  account list and status. Every other component reads from it, and nothing infers an
  account from a label.
- **B2 Instruments are canonical.** A position, fill or rule input carries the
  canonical contract (`NQZ6`: root, month code, year), not a platform spelling
  (`NQ 12-26`, `/NQZ26`, `NQ1!`). Platform spellings are converted at the edge by one
  `te_core` table, and an unknown spelling is refused (I5, I6).
- **B3 Last good list.** A failed or slow read never replaces a list with an empty one.
- **B4 Freshness is per broker.** A stale broker source never blocks another broker's
  accounts. A known account is never refused only because its source is stale. An
  unseen account is refused with `account_unconfirmed` until its source confirms it.
- **B5 Masked outside the registry.** Logs, events and UI show `display` only (`*194`).
  Full ids appear only in the registry file and in requests to the broker.
- **B6 Read before write.** Each adapter ships read-only first (accounts, positions,
  fills, cash, realized P&L). Order placement through an adapter is a separate phase
  with its own owner gate.

## Phases

Each phase is one commit series, ships on its own, and is done only in the commit that
adds its enforcing test (the RUST_PORT working rule). Grades: **G1** = tests in this
repo, **G2** = an integration test against a spawned peer (ntx fake, riskguard-service),
**G3** = a live read on the owner's machine, while flat, with masked output.

### Phase 0: contract freeze (docs only, about half a day)

- This file, plus a `docs/BROKER_CONTRACT.md` stub that holds the frozen wire shapes
  from phases 1, 2 and 4: the instrument JSON, the account record, and the account
  events.
- The registry record is ACCT-REGISTRY-1 decision 2, unchanged: `key`, `display`,
  `env`, `status`, `class`, `firm`, `account_type`, `api_ref`, `routes[]`, `source`,
  `confirmed_at`. `class` and `firm` are overlays that riskguard computes (classifier,
  firm map). The engine sends `null` for them and never guesses.
- Done when: riskguard B's author has read the record and event shapes and agreed to
  them in its brief.

### Phase 1: canonical futures instrument (`te_core::futures`)

- **Rust:** `FutureRoot{root, exchange, tick_size, tick_value, multiplier, currency,
  session}` and `FutureContract{root, month_code, year}`.
  - Only the roots traded today, from one table: NQ, MNQ, ES, MES, YM, MYM, RTY, M2K,
    CL, MCL, GC, MGC.
  - Money and ticks use `rust_decimal` (`tick_value` NQ 5.00, MNQ 0.50).
  - The trading session comes from `te_core::calendar::globex`.
- **Symbol map, at the edge only, in one table:** canonical `NQZ6` from and to:
  - NT8 `NQ 12-26`;
  - Tradovate `NQZ6`;
  - TV `NQZ2026`, plus `NQ1!` continuous;
  - Schwab `/NQZ26`;
  - IBKR `localSymbol` `NQZ6` with `lastTradeDateOrContractMonth` `202612`.

  A continuous symbol (`NQ1!`) resolves only through an explicit front-month rule that
  takes the date and roll table as inputs. It never resolves from wall time inside the
  rule (I7).
- **Roll:** an `expiry` and `roll_date` per contract from a dated table. An unknown
  contract month is refused, never extrapolated.
- **Python:** a data carrier `Future(Instrument)` in `domain/instruments.py` that calls
  `trade_engine_rs`, the same pattern as `OptionContract`.
- **Tests (G1):**
  - every platform spelling round-trips to the canonical form;
  - an unknown root or month is refused with a `kind`;
  - `tick_value * ticks` matches the published contract specs for every root;
  - a proptest that the canonical form round-trips for every month in 2024-2030;
  - `NQ1!` resolves on both sides of a roll date.
- **Consumers after this phase:** riskguard and Vela can import the table through the
  engine's HTTP read API (`GET /v1/instruments/futures`) or the pyo3 module. riskguard
  rules keep their own tick math until phase 4 switches them.

### Phase 2: account port on `BrokerAdapter`

- Add to the protocol:
  - `accounts() -> AccountSnapshot`, the full list for this broker, each entry an
    `AccountRecord` minus the overlays;
  - `account_events(since) -> list[AccountEvent]` with `added | removed | changed`.
- **`te_core::accounts`:** the diff rule (old snapshot → new snapshot → events), the
  masking rule (`display`) and the key rule (`broker:accountId`, case kept,
  whitespace refused). One implementation, used by every adapter.
- **Poll-and-diff helper** for brokers that cannot push: a minimum of 30 s, a default
  of 60 s, and the last good snapshot kept on any error (B3). It emits events only from
  a successful read.
- **Freshness:** each adapter reports `confirmed_at`. `stale_after` is 120 s by
  default, set per broker.
- **Ledger:** account events are appended to the engine ledger (I1, I2), so a restart
  replays membership instead of starting empty.
- **`tos_paper`** gets the trivial implementation first. `mirror.json` becomes its
  fixed list (ACCT-REGISTRY-1 sub-slice D moves here).
- **Tests (G1):**
  - snapshot → add, remove, change → events;
  - a failed poll emits nothing and keeps the list;
  - a restart replays membership from the ledger;
  - no full id appears in any event or log line (a scan over captured logs);
  - a contract test that every registered adapter satisfies the protocol.

### Phase 3: Tradovate adapter over ntx (read-only)

- **Transport:** ntx-daemon `127.0.0.1:9234` (bearer token `daemon-token.txt`, never
  logged):
  - `GET /snapshot` (broker-sdk shaped, across all identities);
  - `GET /events` (SSE on WebSocket `props` mutations, plus keepalive);
  - `GET /health`.

  The daemon is structurally read-only: it links no order-placement symbol. This phase
  keeps it that way.
- **Mapping into engine types:**
  - Each identity becomes one `VenueIdentity`. `env` comes from the identity's
    connect, never from the account name.
  - Each account becomes `tradovate:<name>`, with the numeric id in `api_ref`.
  - The alias map (`observer.rs` `build_alias_entries`: numeric, `D<id>`, name) becomes
    the source of `routes[]` for TV (`D<id>`) and NT8 (name).
  - Positions and fills become `VenuePosition` and `VenueFill` with canonical
    instruments (phase 1).
  - ntx money arrives as `f64`. It is converted to `Decimal` through its shortest
    `repr`. NaN and inf are refused.
- **Membership:** follow `/events`; on reconnect, take a fresh `/snapshot` and diff it.
  If the daemon is down, the adapter keeps the last good list and goes stale after
  120 s (B3, B4).
- **Realized P&L:** ntx realized P&L is exposed per account and day. That is the first
  source in PNL-SOURCE-1 (ntx realized → ledger fills → NT8 counter).
- **Open checks, answered in this phase's first commit (they are riskguard B's checks
  1-3):**
  1. Do ntx `props` events cover account entities, or only cash and position? If only
     the latter, diff on each snapshot instead.
  2. Which identities does ntx hold, against the Tradovate-routed fleet rows (masked)?
     Missing accounts are reported, never auto-closed, until the owner confirms.
  3. NT8 name ↔ ntx name ↔ TV `D<id>`: exact, prefix, or numeric? Add one test per
     pattern found.
- **Tests:**
  - G2 against a fake ntx daemon: snapshot, SSE add, SSE remove, daemon down → stale
    → known accounts unaffected, reconnect → diff;
  - G3 on a live read, flat, masked: the account count and keys match `/v1/fleet`'s
    Tradovate-routed rows, or the differences are listed.

### Phase 4: publish to riskguard, which becomes the consumer

- **Engine side:** a sink (I12, outbox semantics) that pushes account events and
  snapshots to riskguard-service. Delivery is confirmed, not a 200, and the outbox
  drains in order.
- **riskguard side (ACCT-REGISTRY-1 sub-slice B, re-scoped):**
  - B owns the registry store, adding the overlays (`class`, `firm`);
  - `state/account_registry.json`;
  - `GET /v1/accounts?platform=` and `scope=all`;
  - `account.*` events on `/v1/events`;
  - the `account_unconfirmed` gate.

  B does not read ntx itself. It ingests from the engine sink through a
  connector-token endpoint (`POST /v1/registry/ingest`). This avoids a second reader
  of the Tradovate truth (D3 applied across repos).
- **ntx observer:** `observer.rs` already pushes to riskguard (`RG_SERVICE_URL`).
  Membership moves to the engine path. The observer keeps its current fill and P&L
  push until phase 4b retires it, after a G3 week with both paths compared and no
  mismatch.
- **Instruments:** riskguard tick and value math switches to the phase 1 table. Its
  own copy is retired (kept as reference, with a gate test that nothing live imports
  it).
- **Tests:**
  - G2: a spawned riskguard-service plus a fake ntx shows account added at ntx → event
    in the engine → registry → `/v1/accounts?platform=tradingview` within 2 s;
  - ntx down → stale → known accounts are not refused;
  - an unseen account → `account_unconfirmed`.

### Phase 5: Schwab

- **5a:** `mirror.json` fixed list (done in phase 2 via `tos_paper`), `env: paper`,
  route `{platform: tos}`.
- **5b:** Trader API accounts adapter:
  - `accountNumber` is the id and `hashValue` goes in `api_ref`;
  - ACCT_ACTIVITY is routed through the existing `schwab_hub.py`, never a second
    streamer, because that would drop the market feed;
  - the hub's REST allowlist gains only the accounts endpoint;
  - a 60 s poll-and-diff is the fallback.
- Owner gate: any hub change is deployed outside market hours, and the feed is checked
  afterwards.

### Phase 6: platform routes and IBKR

- **NT8 Desktop as a route:**
  - NT8 is a platform, not a broker, so there is no NT8 `BrokerAdapter`;
  - NT8 account names map to broker keys through the phase 3 alias rule;
  - NT8-local accounts are `local:nt8:<name>`.
- **IBKR:**
  - TWS or IB Gateway managed accounts plus `reqAccountUpdates`, or Client Portal
    `/portfolio/accounts`;
  - never Flex.
- Order placement through any adapter (B6): a separate brief for each broker, owner
  approved, with paper or sim first, then one live account.

### Phase 7: Vela

- Vela reads instruments (phase 1) and accounts (phase 4 API) from the engine. It
  never keeps its own symbol map or account list.
- Done when: a gate in Vela fails if it defines a tick-value or symbol-map table of
  its own.

## Order and sizing

| Phase | Depends on | Size | Unblocks |
|---|---|---|---|
| 0 | none | about half a day | B re-scope |
| 1 | 0 | 1-2 days | 3, 4 (instrument), Vela |
| 2 | 0 | 1-2 days | 3, 5a |
| 3 | 1, 2 | 2 days | 4, PNL-SOURCE-1 |
| 4 | 3, riskguard B | 2 days (engine) plus B | L-246 closed for Tradovate |
| 5 | 2 (5a), 4 (5b) | 1 day (5a), 3 days (5b) | Schwab |
| 6 | 4 | 2-3 days | NT8 routes, IBKR |
| 7 | 1, 4 | 1 day | Vela |

Phases 1 and 2 can run in parallel. ACCT-REGISTRY-1 sub-slice A (TV keeps its last
good list, short list) is independent of all of this and ships as it is.

## Riskguard sub-slice B: re-scope

- **Keep:** the registry store and overlays, the disk file, `/v1/accounts`, the
  `account.*` events, the `account_unconfirmed` gate, and the scoped read tests.
- **Keep as read-only investigation:** open checks 1-3. Report the answers; they feed
  phase 3.
- **Drop from B:** the ntx adapter. It is built here in phase 3.
- **Add to B:** `POST /v1/registry/ingest`, connector-token gated, taking the phase 0
  wire shape. Until phase 4 lands, B's tests feed it from a fixture.

## Owner gates

- No live config change, deploy or hub change without the owner. Deploy only when flat,
  then check `isArmed`.
- G3 reads are read-only and use masked output only.
- Order placement through any adapter needs its own approval for each broker.

Parent design: trade-engine/docs/design/BROKER-PLATFORM.md

# BP-4: publish accounts to riskguard (plan phase 4)

Read BP-COMMON.md first, then plan phase 4. Starts after BP-3 is merged.
riskguard sub-slice B is already built (nt8-riskguard `027c3cf1`):
`POST /v1/registry/ingest` (connector-token gated, `broker-account/1` wire shape),
the registry store, `/v1/accounts?platform=`, `account.*` events and the
`account_unconfirmed` gate. Read that code; do not rebuild it.

Two sub-slices, two commits, in this order.

## 4a: engine sink + outbox (trade-engine)

1. A sink (I12, outbox semantics) that sends account snapshots and events to
   riskguard `POST /v1/registry/ingest`. Delivery counts as confirmed only when the
   response body acknowledges the `seq` it applied, not on a bare 200. If B's response
   does not carry the applied seq today, stop and report; do not change riskguard in 4a.
2. Outbox on disk, drained strictly in order. Membership is idempotent, so a long outage
   compacts to the latest snapshot plus the tail since it.
3. The connector token is read from the riskguard token file the service already uses
   for connectors (by path, never printed, never logged). Do not read
   `service-tokens.json`, `.env` or any secrets file; if the token file is not where B's
   docs say, stop and report.
4. A switch to turn the sink off (config, default off). Rollback = off; the ntx observer
   push in riskguard stays untouched until 4b (not in this brief).

## 4c: riskguard tick/value math from the phase 1 table (nt8-riskguard)

1. riskguard reads instrument tick size and tick value from the BP-1 table (exported as
   a generated JSON or a crate dependency, whichever BP-1 exposes; no hand copy).
2. riskguard's own table is retired: kept as reference, with a gate test that nothing
   live imports it (D3).
3. A parity test: for every root riskguard knows today, old and new values are equal.
   Any difference stops the slice and is reported, not "fixed".

## Tests

- G2 (4a): spawned riskguard-service from the nt8-riskguard workspace build + a fake ntx
  daemon (reuse BP-3's) → account added at ntx → engine event → registry →
  `/v1/accounts?platform=tradingview` within 2 s.
- ntx down → stale → known accounts are not refused by the gate.
- An unseen account → `account_unconfirmed`.
- Outbox: 24 h of events stays under the cap and replays in order; riskguard down for
  the outage then up → drains in order, seq gaps refused.
- 4c parity test and import gate.
- Run the full nt8-riskguard suites for 4c (Rust workspace, surfaces `run_tests.mjs`)
  and state pass counts.

## Performance and memory budget (design 6.8a, B7)

| Component | Latency / CPU budget | Memory ceiling | How it stays bounded | Enforcing test |
|---|---|---|---|---|
| Outbox to riskguard (P4) | Delivery adds < 50 ms to the 2 s visibility budget | On disk ≤ 50 MB | Membership is idempotent, so a long outage compacts to the latest snapshot plus tail instead of growing | Outage test: 24 h of events stays under cap and replays in order |
| riskguard ingest + store (P4) | Ingest < 5 ms for 200 records; gate lookup < 1 ms | Registry < 5 MB RSS | Body cap 1 MB; disk writes coalesced (≤ 1 per 500 ms, atomic); gate reads an immutable snapshot swapped by pointer | Gate latency under concurrent ingest; write-coalesce test (verify B has these; add if missing) |
| `/v1/accounts?platform=` (P4) | p99 < 20 ms | One response < 32 KB | Scoped only; ETag → 304 | Response-size test; 304 test (verify B has these) |

## Not in this brief

- Deploy. Turning the sink on, restarting rg_service, or anything live needs the owner,
  flat, then `isArmed` and the three switches checked.
- 4b (retiring the ntx observer's membership push) waits for a one-week G3 comparison.

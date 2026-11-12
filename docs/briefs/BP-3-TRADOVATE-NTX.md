Parent design: trade-engine/docs/design/BROKER-PLATFORM.md

# BP-3: Tradovate adapter over ntx, read-only (plan phase 3)

Read BP-COMMON.md first, then plan phase 3 in `docs/BROKER_PLATFORM_PLAN.md` and design
section 10 (open questions 1-3 are already answered; use those answers, do not re-ask).
Phases 1 (futures table) and 2 (account port) are merged on main. Phase 4 waits for this.

## What to build

1. **ntx client (Python I/O shell)** for ntx-daemon `127.0.0.1:9234`:
   `GET /snapshot`, `GET /events` (SSE), `GET /health`. Bearer token read from
   `daemon-token.txt` at the path ntx documents (`nt_tradovate_mcp` repo, read-only
   reference). The token is never logged, never in an exception message, never in `repr`.
   The adapter links no order-placement call and has no code path that sends one. Add a
   gate test that scans the adapter module for order verbs (place, cancel, modify,
   liquidate, flatten) and fails if any appear as calls or URLs.
2. **`TradovateAdapter(BrokerAdapter)`** implementing `accounts()` and
   `account_events(since)` from phase 2:
   - one `VenueIdentity` per ntx identity; `env` from the identity's connect, never
     parsed from the account name;
   - each account → `AccountRecord` key `tradovate:<ntx name>`, `api_ref` = numeric
     Tradovate id (engine-only, D-13), `source: ntx`;
   - `routes`: `{platform: nt8, local_name: <ntx name>}` and
     `{platform: tradingview, local_name: "D" + <numeric id>}` (design 10.3). One test per
     pattern.
   - positions/fills → `VenuePosition` / `VenueFill` with canonical instruments via the
     BP-1 map. An unknown root or month is refused with a `kind` (I5).
   - money: ntx `f64` → `Decimal` via shortest `repr`; NaN/inf refused.
3. **Membership (design 10.1):** `props` has no typed add/remove diff. Diff on every
   `/snapshot` (connect, reconnect) using `te_core::accounts::diff`; an account-entity
   `props` event may only trigger an early re-read, rate-limited to one per 5 s.
   Daemon down → keep last good list, stale after 120 s (B3, B4). A missing account is
   reported, never closed (design 10.2): no auto-remove while any identity reads non-200.
   Model this explicitly: a snapshot where an identity is degraded (401 etc.) does not
   emit `removed` for that identity's accounts; it marks them stale.
4. **Realized P&L:** expose ntx realized P&L per account and day as a read
   (`realized_pnl(account_key, day) -> Money | None`). First source for PNL-SOURCE-1. No
   consumer yet.

## Tests

- G1/G2 against a fake ntx daemon (local HTTP + SSE server in the test): snapshot →
  added; SSE account event → early re-read → added/removed; daemon down → stale after
  120 s, known accounts unchanged; reconnect → one fresh snapshot → diff; one identity
  401 → no `removed` for its accounts, they go stale.
- Token never appears in any captured log, exception or repr (scan with a fake token).
- Order-verb gate test (above).
- Mapping: name/`D<id>` routes, env from connect, unknown instrument refused, NaN refused.
- **G3 is NOT part of this slice's done.** ntx currently returns 401 on all four identities
  (design 10.2); a live read needs the owner to re-login. Write the G3 script
  (`tools/g3_ntx_accounts.py`: flat, read-only, masked output, compares count and keys to
  riskguard `/v1/fleet` Tradovate rows, records RSS/CPU before and after) and run it only
  if `/health` shows every identity 200. Otherwise say "G3 blocked on ntx 401" in the report.

## Performance and memory budget (design 6.8a, B7)

| Component | Latency / CPU budget | Memory ceiling | How it stays bounded | Enforcing test |
|---|---|---|---|---|
| ntx SSE client (P3) | One connection. Per-event parse < 100 µs | Line buffer cap 1 MB | Oversize frame dropped and counted; reconnect backoff 1 s → 30 s with jitter; `/snapshot` only on connect and reconnect, never per event | Oversize-frame test; reconnect-storm test (ntx flapping) keeps one socket and bounded memory |

- Early re-read limiter: at most one `/snapshot` per 5 s; extra triggers coalesce, counted.

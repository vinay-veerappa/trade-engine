# OA demo latency runbook (OA-5-CHECK)

Purpose: measure the round trip of the ntx order routes (`POST /v1/orders`, `/v1/orders/modify`,
`/v1/orders/cancel`) and the reduce-only `POST /v1/flatten` on the Tradovate **demo** account
***194, then compare against the bar in section 5.

This runbook is for the parent to execute. The check author placed no order. Nothing here
restarts a process or edits config. Every step that writes to the venue is marked **[ORDER]**.

## 0. Hard rules

- **Demo only.** Step 1 must confirm the account's environment is demo. If it is not demo, stop.
- **One contract (1 MNQ).** ntx `instrument_allowed` permits only MNQ and NQ roots, so MES is refused. Every cycle starts flat and ends flat.
- **Enter with a stop, every time.** A bracket without `stop` is refused as `no_stop`.
- **Never resend.** Each request gets a new `clOrdId`. A timeout or `PENDING` is reconciled
  from `GET /snapshot`; do not send it again.
- **Never widen a stop.** Modify only toward the market (section 3, step 3), so the modify is a
  reducing action and needs no verdict.
- **Never touch `%LOCALAPPDATA%\ntx_tradovate\secrets\`** beyond reading the token into memory.
  Never print the token, and mask the account to its last 3 digits in every log and paste.

## 1. Preconditions (read-only)

1. ntx daemon is up on `127.0.0.1:9234`. `GET /health` answers. The startup line in the ntx
   log lists `POST /v1/orders[/modify|/cancel]` and `POST /v1/flatten`. If the order routes are
   missing, the running daemon predates ORDER-API-1 OA-2; stop and report.
2. **Allowlist.** `POST /v1/orders*` refuses every account unless `orders.json` in the ntx secrets dir
   (`%LOCALAPPDATA%\ntx_tradovate\secrets\orders.json`; `session_store_dir()` appends `\secrets`)
   lists the account. As of 2026-11-20 the file lists ***194 only (owner decision 3); before
   that it did not exist, so every order was refused with `account_not_allowlisted`. The owner adds the
   demo account. The parent must not write the file on the owner's behalf.
3. **Environment.** `GET /snapshot` (with the bearer header) shows the account's identity as
   demo. If the environment field is anything other than demo, abort.
4. **Flat.** The account has no open position and no working orders (`/snapshot`).
5. **Gate state.** riskguard shows the account not locked and the service armed (the same
   verdict document NT8 uses, `/v1/verdicts`). ntx refuses openings otherwise.
6. **Verdict source.** Placing a bracket is an opening, so it needs a fresh riskguard verdict
   (`POST /v1/check?account=<core>`, the call in `src/trade_engine/oms/precheck.py:178`). The
   verdict must be `allowed: true` and no more than 2 s old when ntx receives the place.
   Measure the check separately; do not count it in the ntx round trip.

Token and auth: `Authorization: Bearer <token>` with the token read from the ntx token file
(`%LOCALAPPDATA%\ntx_tradovate\secrets\daemon-token.txt`, or `NTX_STORE` if set). Read it into a
variable inside the script. Do not `echo` or log it.

## 2. What to log (one JSON line per request)

Write to `%LOCALAPPDATA%\trade_engine\oa_demo_latency\<YYYY-MM-DD>.jsonl`, outside the repo.

| field | content |
|---|---|
| `cycle` | 1..10 |
| `op` | `place`, `modify_stop`, `cancel_target`, `flatten` |
| `clOrdId` | the id sent (`flatten` has none) |
| `t_send_utc` | high-resolution UTC stamp taken just before the socket write |
| `t_recv_utc` | UTC stamp after the full body is read |
| `wall_ms` | `t_recv_utc - t_send_utc` |
| `http_status` | code |
| `outcome`, `code`, `reason` | from the response body (`SENT`, `PENDING`, `REFUSED`) |
| `orderIds` | `entryId`, `stopId`, `targetId` from place |
| `timingMs` | from the ntx log line, for `flatten` only (NTX-TIMING-1 stamps) |
| `account` | masked, `*194` form |
| `verdict_age_ms` | place only: time from the riskguard verdict's `checkedAt` to send |
| `position_after` | position qty read from `/snapshot` after the op settles |

Also keep the raw ntx log lines for the same window (`ntx flatten:` lines carry `timingMs`).

## 3. Steps (per cycle)

Use one `clOrdId` per request, charset `[A-Za-z0-9._-]`, at most 64 characters, for example
`oa5-<cycle>-<op>-<utc compact>`. Prices are strings or numbers; use the current bid/ask
for the account's contract, quoted from `/snapshot` or the quote route.

1. **[ORDER] place.** `POST /v1/orders` with body:
   ```json
   {"account": "<demo account>", "clOrdId": "<id>", "symbol": "<front MNQ symbol from ntx, e.g. MNQZ6>",
    "side": "Buy", "qty": 1, "entry": {"type": "market"},
    "stop": "<bid - 10 ticks>", "target": "<ask + 40 ticks>",
    "verdict": {"allowed": true, "verdictRevision": <rev>, "checkedAt": "<RFC3339>"}}
   ```
   Expect `outcome: SENT` and `orderIds` with `entryId`, `stopId`, `targetId`. Wait for the
   position to read long 1 from `/snapshot`, then continue. Log the `place` line.
   (Body shape: `WirePlace` in `nt_tradovate_mcp/ntx/src/submit.rs`; unknown fields are refused.)

2. **[ORDER] modify the stop toward the market.** `POST /v1/orders/modify`:
   ```json
   {"account": "<demo account>", "clOrdId": "<id>", "orderId": <stopId>, "qty": 1,
    "to": {"type": "stop", "price": "<old stop + 2 ticks>"}}
   ```
   The new stop must stay below the bid, so it moves toward the market. No `verdict` is sent
   (`classify` treats a toward-market stop move as reducing). If ntx answers that the modify
   needs a verdict, that is a finding: record it and stop the cycle.

3. **[ORDER] cancel the target.** `POST /v1/orders/cancel`:
   ```json
   {"account": "<demo account>", "clOrdId": "<id>", "orderId": <targetId>}
   ```
   Keep the stop. Cancelling the stop would leave the position unprotected during the test.

4. **[ORDER] flatten.** `POST /v1/flatten` with body `{"account": "<demo account>"}`. This is
   reduce-only; it cancels the stop and closes the position. Log its wall time and the
   `timingMs` from the `ntx flatten:` line.

5. **Confirm flat** from `/snapshot`: no position, no working orders. If the cycle did not end
   flat, call `/v1/flatten` once more and stop the run. Report it; do not start another cycle.

Run 10 cycles, one after another, not in parallel. Between cycles, confirm flat (step 5).

**Abort rules.** Stop the run and flatten if: more than one contract fills, the position is not
the expected size, a verdict is refused, or any request times out. Report the outcome of each
request. A `PENDING` result is reconciled from `/snapshot` and never resent.

## 4. Analysis

- For each op, take the `wall_ms` values across the 10 cycles: p50, p95 and max. With n = 10,
  the p95 is taken as the second-largest value, and the max is reported alongside it.
- Put `flatten` `wall_ms` and `timingMs` in the same table.
- Put `place` `verdict_age_ms` in the table. Any value above 2000 ms means ntx would have
  refused the place, so it is a failure of the cycle.
- Note any cycle that needed a second flatten or any `PENDING` result.

## 5. Pass bar

Reference: the **498 ms flatten from 11-17** (`docs/product/slice_briefs/NTX-FLATTEN-6.md:11`
and `docs/product/11_REMAINING_WORK.md`). That number is a flatten, not an order round trip,
so the flatten row compares like with like and the order rows compare against the budget in
`docs/design/L-333e.md:33`.

| measure | pass | fail |
|---|---|---|
| `flatten` wall time, p95 | ≤ 498 ms | > 498 ms |
| `place`, `modify_stop`, `cancel_target` wall time, p95 | ≤ 250 ms (L-333e venue verdict budget) | > 250 ms |
| `place` verdict age at send | ≤ 2000 ms, all cycles | any cycle over 2000 ms |
| every cycle ends flat, no unexpected fill | all 10 | any miss |
| no request sent twice, no unhandled `PENDING` | all | any |

The **fill-seen to modify-sent ≤ 50 ms** budget from L-333e is measured inside the service,
not by this client-side run. It is not covered here; say so in the report.

Context from NTX-TIMING-1: the 11-17 venue stop cancel call took 103 ms through ntx, and
Tradovate created the market order 263 ms after the call. Most of the flatten time is venue
time, so the order-route p95 may be close to the bar even when ntx is fast.

## 6. What to report back

- The table in section 4, with the masked account and the date.
- The verdict per row in section 5.
- Any refusal code, `PENDING`, or abort, with its raw response body.
- Whether the allowlist, verdict and environment preconditions held.

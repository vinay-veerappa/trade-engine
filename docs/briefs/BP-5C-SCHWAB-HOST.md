Parent design: trade-engine/docs/design/BROKER-PLATFORM.md (phase 5). Standing rules: `BP-COMMON.md`. Builds on BP-5b (`src/trade_engine/schwab/adapter.py`, merged) and BP-4d/4e/4f (`src/trade_engine/tradovate/registry_feed.py`, merged and running).

# BP-5c: host the Schwab accounts adapter in the registry feed

## Why
- BP-5b built `SchwabAccountsAdapter`, and its hub half is live: hub `POST /request` now answers `get_account_numbers` and `get_accounts` (no params; any param is refused 403).
- Nothing constructs the adapter at runtime. It has no HTTP client by design (the hub is the only Schwab client), so a host must fetch the two reads and hand the text to `adapter.feed(...)`.
- The registry feed is the process that already owns a sink and a loop. Host it there, behind the adapter's own switch, off by default.

## Hub contract (read `tvDownloadOHLC/scripts/streaming/schwab_hub.py` ~line 493 and `providers/rest_policy.py`; do not edit them)
- `POST http://127.0.0.1:8081/request` with body `{"method": "get_account_numbers"}` or `{"method": "get_accounts"}`. Nothing else in the body.
- Success: `{"status": "success", "data": <json>}`. Anything else (`status` error, `code` timeout, HTTP 403/5xx, connection refused) is a failed read.
- Base URL from env `TE_SCHWAB_HUB_URL`, default `http://127.0.0.1:8081`. Loopback only: refuse a non-loopback host at config time (ValueError, exit like the other bad-env cases).

## Do
1. **A small hub client** `src/trade_engine/schwab/hub_client.py`: `HubAccountsClient(base_url, *, timeout_s=10, post=None)` with `read() -> tuple[str, str]` returning `(account_numbers_json, accounts_json)` as `json.dumps(data)` text. `post` is injectable for tests (default `urllib.request`; no new dependency). It raises one `HubReadError(kind)` on any failure; `kind` is one of `refused`, `timeout`, `unreachable`, `bad_shape`. Never log a response body.
2. **A second sink for broker `schwab`.** `RegistrySinkConfig` already has `broker`. Build a second `RiskguardRegistrySink` with `broker="schwab"` and its own outbox file: `TE_SCHWAB_OUTBOX_PATH`, default the Tradovate outbox path with `registry_outbox.json` → `registry_outbox_schwab.json`. Same riskguard URL and token file. The Tradovate sink, its outbox and its bytes on the wire must not change (I1).
3. **Wire it in `registry_feed.py`.**
   - In `Feed.build` (or a sibling `SchwabFeed.build` called from `run`), when `SchwabAccountsConfig.from_env(environ).enabled`, construct the adapter with `on_poll=schwab_sink.observe` and the hub client.
   - In `run`, add one thread (same pattern as the existing `poll` thread, same `stop_event`): every tick, if `adapter.next_poll_due(now)` then `client.read()` → `adapter.feed(...)` → `adapter.poller.poll()`. On `HubReadError`, count it by kind, back off (30 s, 60 s, cap 300 s; jittered with the injected `rand`), and do not call `feed`. Never poll faster than the adapter's 60 s floor.
   - `start()`/`stop()` the schwab sink with the Tradovate one. If the switch is off, nothing Schwab is built and no thread starts.
   - The minute heartbeat gets `schwab_accounts`, `schwab_reads_ok`, `schwab_read_fail_<kind>`, and the schwab sink's `stats` counts. No account numbers, no `hashValue`, no money.
   - `run_once` (`--once`): when the switch is on, also do one Schwab read + poll + pump, and add `"schwab_accounts": n, "schwab_sent": n` to the JSON.
4. **ACCOUNTS_CHANGED hint: not in this slice.** The 60 s poll is the contract. Leave a one-line TODO naming `adapter.on_activity` and the hub `/ws` `ACCOUNTS_CHANGED` frame.
5. **Docs:** one short section in `docs/BROKER_CONTRACT.md` (or wherever BP-4d documented the feed env): `TE_SCHWAB_ACCOUNTS`, `TE_SCHWAB_HUB_URL`, `TE_SCHWAB_OUTBOX_PATH`, and how to check `/v1/fleet` for `schwab:` slots.

## Tests (`tests/test_bp5c_schwab_host.py`; reuse the BP-4d fake riskguard endpoint and the BP-5b fixtures with fake ids `ACCT-TEST-...`)
- Switch off: no hub call, no schwab thread, Tradovate ingest body byte-identical to before.
- Switch on, `--once` with a fake hub: one ingest to riskguard with broker `schwab` and masked keys; the string `hashValue` and every fake hash value are absent from the ingest body, the logs and the schwab outbox file (string search).
- Hub failures (`status` error, timeout, 403, connection refused, non-JSON): no `feed` call, counter by kind, backoff grows and caps at 300 s, no exception escapes the thread.
- Poll cadence: with a fake clock, 10 simulated minutes produce at most 10 hub read pairs (≤ 2 REST calls/min of the hub's shared 120/min).
- Non-loopback `TE_SCHWAB_HUB_URL` → ValueError.
- The stop event ends the schwab thread within one tick; both sinks get `stop()`.
- Every existing BP-3, BP-4*, BP-5b and PS-4a test stays green.

## Performance and memory budget (B7)

| Component | Budget | Bound | Enforcing test |
|---|---|---|---|
| Hub reads | ≤ 2 calls/min | adapter 60 s floor + backoff | cadence test |
| Schwab outbox | same cap_bytes as Tradovate | `RegistrySinkConfig.cap_bytes` | existing BP-4a cap test applies |
| Thread | one, blocking waits | no busy loop | stop-event test |

## Verify
`python -m pytest tests -q -k "bp4 or bp5 or registry or schwab"`, then full `python -m pytest -q`, plus ruff.

Do not start the real feed, do not call the real hub or riskguard, do not touch quantd, and do not read any real token file. One commit: `feat(registry): BP-5c host the Schwab accounts adapter in registry-feed`. Do not push.

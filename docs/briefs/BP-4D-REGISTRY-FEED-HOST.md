Parent design: trade-engine/docs/design/BROKER-PLATFORM.md (phase 4) and nt8-riskguard/docs/design/PNL-SOURCE-1.md (§9 PS-4). Standing rules: `BP-COMMON.md`. Builds on BP-3 (`tradovate/adapter.py`, `ntx_client.py`, `AccountWatcher`), BP-4a (`sinks/riskguard_registry.py`) and PS-4a (the `pnl` rows), all merged.

# BP-4d: a host process that runs the Tradovate registry feed

## Why
- BP-4a and PS-4a built the sink, and BP-3 built the adapter and the watcher.
- Nothing constructs them at runtime. `grep RiskguardRegistrySink src` finds only the module itself.
- The owner approved turning on the registry sink and `TE_RISKGUARD_REGISTRY_PNL` on 2026-11-15, so there must be something to turn on.

## Do
1. Add a module `src/trade_engine/tradovate/registry_feed.py` with `run(environ, *, clock, sleep, rand, stop_event) -> int`. It:
   - builds the `NtxClient` the way existing BP-3 code or tests do, using the existing token loader and the loopback default;
   - builds the `TradovateAdapter`, the `AccountWatcher` and the `RiskguardRegistrySink(RegistrySinkConfig.from_env(environ))`;
   - on every good poll, calls `sink.observe(snapshot, events, seq, pnl=adapter.pnl_rows(snapshot))`. Use the adapter's `on_poll` hook or the poller's diff output, whichever BP-3 already exposes. Do not add a second poll loop;
   - calls `sink.start()`, then loops `watcher.run_once()` with the watcher's own backoff until `stop_event` is set, then calls `sink.stop()`;
   - writes one heartbeat log line a minute, holding only counts: snapshots, connects, the sink's `stats` and the account count. No money, no account ids and no token.
2. Add a CLI subcommand `trade_engine registry-feed` in `cli.py`. It uses `--once`, which does one snapshot, one `pump()` and an exit, to print `{"accounts": n, "pnl_rows": n, "sent": n, "halted": ...}`. Without `--once` it calls `run(os.environ, ...)` and exits 0 on SIGINT or SIGTERM.
3. When the sink config is disabled, the process exits at once with code 2 and the line `registry sink off (TE_RISKGUARD_REGISTRY_SINK!=on)`. It does not idle.
4. Add a `docs/RUNBOOK` section, or a short section in `docs/BROKER_CONTRACT.md`, covering:
   - the env vars;
   - the quantd service entry to add. Write the entry text in the doc and do not edit any quantd config;
   - how to check `/v1/fleet` on riskguard for the broker P&L slot.
5. **B7 budgets:**
   - memory: the adapter's ACCOUNT_CAP and the outbox cap_bytes stay the bounds, with no new unbounded list;
   - CPU: no busy loop, so the watcher blocks on the stream;
   - one test per bound.

## Tests (`tests/test_bp4d_registry_feed.py`)
- Use a fake NtxClient with a snapshot of two accounts and a fake riskguard HTTP endpoint (the BP-4a tests have one; reuse it). After `--once`, the ingest body has the snapshot, plus `pnl` only when `TE_RISKGUARD_REGISTRY_PNL=on`.
- With the sink off, the exit code is 2 and no HTTP call is made.
- With PNL off, the body is byte-identical to the BP-4a envelope (I1).
- The stop event ends `run` within one watcher iteration, and `sink.stop()` is called.
- No secret appears in the logs: write the token file with a sentinel string and assert it is absent from the captured logs.
- Every existing BP-3, BP-4a and PS-4a test stays green.

## Verify
`python -m pytest tests -q -k "bp3 or bp4 or ps4 or registry or tradovate"`, then the full `python -m pytest -q`, plus ruff if the repo uses it.

Do not start the real feed, do not touch quantd, ntx or riskguard, and do not read any real token file. One commit: `feat(registry): BP-4d host process for the Tradovate registry feed`. Do not push.

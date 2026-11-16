Parent design: trade-engine/docs/design/BROKER-PLATFORM.md (phase 4) and nt8-riskguard/docs/design/PNL-SOURCE-1.md. Standing rules: `BP-COMMON.md`. Builds on BP-4d (`tradovate/registry_feed.py`), merged.

# BP-4e: keep the broker P&L figure fresh between fills

## Why
- BP-4d went live on 2026-11-15. riskguard's broker slots fill (22 accounts).
- Each pnl row's `as_of` is the daemon snapshot's `as_of_utc` (`adapter.pnl_rows`). `AccountWatcher` re-reads the snapshot only on connect and on hint events, so `as_of` freezes between fills. The outbox re-sends the unchanged figure on its heartbeat (40 s), but with the old `as_of`.
- riskguard treats a broker figure older than 10 s as stale (`pnl_source.rs`). Observed: every broker slot was 459–639 s old while the feed was up.
- This is harmless while PS-6 `select_broker` is off. **It is a prerequisite for turning PS-6 on**: a stale broker figure is never selected, so PS-6 would silently fall back to the guard figure.

## Do
1. In `registry_feed.run`, add a bounded periodic re-snapshot. Interval: 2 s while any account has a non-zero position, 30 s when all are flat. Use the existing `NtxClient` snapshot call and the existing `observe` path. No second poll loop: drive it from the watcher's wait timeout, or a single timer thread that the stop event ends.
2. Stamp the row's `as_of` from the snapshot read time only when the daemon's `as_of_utc` is not older than the read. If the daemon's own data is older than 10 s (its stream is down), keep the daemon's time. The figure must go stale honestly and never be freshened by our clock alone.
3. Heartbeat line gains `resnapshots` and `max_as_of_age_s`. Counts and ages only.
4. **B7:**
   - CPU: a flat fleet costs at most one snapshot per 30 s, and an open one at most one per 2 s. Test both with a fake clock.
   - Memory: no new list.
   - Daemon load: snapshot p95 measured once on the live daemon and written in the brief result.

## Tests (`tests/test_bp4e_pnl_freshness.py`)
- With a position open and no hints, `as_of` advances every 2 s (fake clock).
- With everyone flat, the interval is 30 s.
- When the daemon's `as_of_utc` is 20 s old, the row keeps it (stays stale).
- Stop ends the timer within one interval.
- Every BP-3, BP-4 and PS-4 test stays green.

## Verify
Run `python -m pytest tests -q -k "bp3 or bp4 or ps4 or registry or tradovate"`, then the full suite. Do not start the real feed and do not touch quantd, ntx or riskguard. Use one commit: `feat(registry): BP-4e keep broker P&L fresh between fills`. Do not push.

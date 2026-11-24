# EOD flow

| | |
|---|---|
| Status | Draft |
| Owner | owner (approver) |
| Author | — |
| Last updated | 2026-11-24 |
| Repos touched | trade-engine |
| Plan | — |
| Briefs | — |
| Issues | W6-F4c (delete the last Rust→Python→Rust trampolines) |

## 1. Summary

The end-of-day runner (E7, Architecture §4.9) runs the 17:45 ET job per account and
session, in order:

1. Gate: a session whose previous session has no completed run for the account
   refuses (bootstrap: an account's first run has no previous marker to demand).
2. Replay: one-minute bars for every instrument an account orders or holds, merged
   into one timeline across accounts and instruments so a single clock walks the
   session minute by minute. After **every** bar the runner reconciles every order
   the venue touched that bar — fills recorded, states read back, brackets
   synchronized. SimBroker rejects a protective stop submitted after later bars were
   simulated, and the OMS raises; the runner never batches reconciliation to the end
   of the session, because the bars in between are gone.
3. Close: the clock moves to the session close, so marks are dated at the close and a
   D+1 DAY entry belongs to the next session instead of expiring at its open. Each
   account then finishes in this order (`eod_flow.finish`): reconcile; for options
   accounts, sync and dividends; Marks; Manage; Entries; Marker.
   - Dividends: options accounts are credited (or charged) today's ex-dividends.
   - Marks: MTM, one Mark per open position at the last regular bar's close; a missing
     regular bar refuses (I5).
   - Manage (exits): a strategy with `manage_positions` sees its open brackets and may
     tighten stops, close positions or reduce them at the next open (`domain.exits`);
     the OMS applies them.
   - Entries for D+1: discovered signals and strategies produce intents; the risk layer
     evaluates each; approved intents become brackets whose DAY entry works the next
     session. A verdict an interrupted run already recorded is resumed, not decided again.
4. Marker: one `EodRun` event per account, claimed by `eod:<job>:<account>:<session>`,
   written after Entries. A re-run replays deterministically and every command id it
   derives is already claimed, so the second run appends nothing.
5. Outbox: every configured destination drains in order (I12).

Options accounts (O4) are those whose venue matches chain snapshots (`SnapshotVenue`).
They replay no bars. Instead:

- each of the session's chain snapshots (`chain_snapshots`) is matched at its own
  `as_of` inside the same timeline, and the strategy's `manage_options` runs right
  after it (`eod.options`); a strategy may narrow which snapshots its account sees by
  exposing `option_universe(session)`, and the account's held option and share
  underlyings always keep their snapshots;
- after the close the clock moves on by `settle_delay`, to when the official close is
  known (daily bars settle at 17:00 ET). The O2 lifecycle pass then settles expiries and
  assignments, dividends going ex next session are credited on the shares held at the
  close, and positions are marked: options at the newest snapshot's mid, shares and
  underlyings at the official close (`settlements`);
- close-phase exits and D+1 entries go through the options OMS (`oms.options`), with the
  C3/C4/C5 guards, and entries through the account's options risk engine.

Equity accounts finish at the close before any of that, so their marks keep the close's
stamp.

An in-session pass (`run_pass(session, through, name)`, `name` one of `PASSES`;
`run_morning` is the morning one) runs an options account's part of the session up to
`through`, while the market is open: the session's snapshots taken since the account's
previous pass are matched and the strategy acts on them, exactly as the after-close run
would have. What the strategy decides at a pass's snapshot is decided and filled on those
quotes, so the host can send it to a venue while the market is still open. Each pass
claims its own marker (`eod:<job>-<name>:<account>:<session>`, `at_close` = `through`);
a later pass, and the after-close run, skip every snapshot at or before the newest of
them, taking each only as the newest quote of its underlying. Nothing is settled, marked
or entered from signals in a pass: that is still the after-close run's.

The runner owns orchestration only. Expiry/assignment semantics stay with O2, EOD exit
rules belong to strategy plugins (I13) and reach the venue only through the OMS, and
market data comes from injected providers (I5: no default source).

## 2. Context and problem

The native flow (`crates/te_py/src/eod_flow.rs`, `te_core::runtime::eod_flow`) owns the
orchestration; Python owns the seams that do real work (venue restore, reconcile, routers,
managers, strategy plugins). W6-F4c removed the remaining Rust→Python→Rust trampolines, so
`eod_flow.rs` now calls the native implementations directly and only the kept seams go
through Python.

## 3. Goals and non-goals

- **Goals:** one native EOD flow with no round-trip trampolines; identical ledger events,
  order, refusals and result dicts.
- **Non-goals:** moving the kept seams (venue restore, reconcile, routers, managers, entry
  plumbing) into Rust — those stay Python until W6-E.

## 4. Prior art and patterns considered

None.

## 5. Architecture

`EodRunner.run` / `run_pass` → `rs.eod_run` / `rs.eod_pass` → `eod_flow.rs` `Host`
(`EodHost`) → `te_core::runtime::eod_flow::run` / `replay` / `finish`. The host reads the
ledger and clock natively; the venue, manager, router and reconcile seams are reached
through Python `call_method*`.

## 6. Design

The phases and their invariants are named in the Summary. Failure modes: a missing regular
bar, a stale mark quote, or a refused exit all refuse the run (I5); a second run of the
same session appends nothing because every derived command id is already claimed.

### Build/source skew guard (W6)

te.exe embeds `trade_engine_rs` (te_py) at the commit it was built from, and imports the
`trade_engine` package from the configured `engine_source` checkout — in production a
separate git worktree. When the two came from different commits, jobs failed only at run
time with attribute errors (`trade_engine_rs has no attribute oms_brackets_to_resync`,
production 2026-11-24). The guard refuses to serve instead:

- te_py's `build.rs` stamps `TE_GIT_SHA` (`<40-hex>`, `-dirty` when tracked files changed,
  `unknown` without git) into `trade_engine_rs.__git_sha__` (crates/te_py/build.rs:21-40,
  crates/te_py/src/lib.rs:887).
- Config key `skew_guard` (optional boolean, default false) turns the check on
  (crates/te_runtime/src/config.rs:30). When true, `verify_engine_source` refuses unless the
  built sha is clean and equal to `engine_source`'s clean git HEAD
  (crates/te_runtime/src/plugins.rs:13 `skew_refusal`, :58 `verify_no_skew`); the refusal
  text starts with `build skew: `.
- The owner's startup `serving` status and report carry `"git_sha"` (the built sha)
  (crates/te_runtime/src/main.rs:100, :188), so a logged report names the exact build.

## 7. Alternatives rejected

None.

## 8. Decisions

None.

## 9. Rollout and verification

Targeted tests: `tests/test_eod_*.py`, `tests/test_p4c_eod.py`,
`tests/test_restore_positions_only.py`, `tests/test_fill_crash_resync.py`.

## 10. Open questions

None.

## 11. Review log

| Date | Reviewer | Comment | Resolution |
|---|---|---|---|

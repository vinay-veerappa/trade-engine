# Intraday service

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

The intraday service for 0DTE strategies (build plan I1, Architecture §4.9, P3) is a
*live* loop, unlike the EOD runner's replay: the future's snapshots do not exist yet, so
the session cannot be re-derived. Instead it steps the clock tick by tick between the
session open and the close, and at each tick:

1. Pulls the underlying's chain snapshot now (injected source) and proves it is the
   live market (I5): the snapshot was taken within `max_quote_age_seconds`, the
   *underlying's own quote time* is that recent too, and so is the quote of every leg
   the account holds. A snapshot that does not say when the underlying was quoted
   proves nothing and is refused — `as_of` is only when the answer arrived.
2. **Stale quote ⇒ flat-and-refuse**: working entries are cancelled, every open
   structure is closed at market, and entries are refused until fresh quotes return.
   The strategy is not asked anything on a stale tick. The flatten order rests at the
   venue and fills at the next fresh snapshot.
3. On a fresh tick, only the quotes that are themselves fresh are handed on: the venue
   matches working orders against them, the E4 rule reconciles at once, and the
   strategy sees the same `OptionContext` the EOD run does — so no candidate leg is
   ever priced from an old quote.
4. The session's clock rules are the service's, early closes included: entries end at
   `min(entry_end, close − entry_before_close)` and the sweep flattens at
   `min(flat_at, close − flat_before_close)`. Past the entry end, working entries are
   cancelled and any entry the strategy returns is dropped.
5. Every fresh tick records a position check against the venue (`VenueReconcile`,
   I11), which is also the session's snapshot count; drift stops the service.
6. Writes a **heartbeat** file (atomically) from start-up to exit: the supervisor
   watches it, and a fresh one from this session refuses a second instance (C4); the
   ledger's OS lock enforces it too (I4).

Idempotency (I3): every derived command is claimed by id, so a restart mid-session
folds the ledger, restores the venue book from the fold (`oms.restore`), resolves
what the crash left half-done, and continues — a session replayed to the end after a
restart yields the same ledger as an uninterrupted run. The after-close work (expiry,
assignment, official-close marks) stays with the EOD job (O2, I9); its marker for the
previous session gates this one, and its marker for this session ends it.

## 2. Context and problem

The native flow (`crates/te_py/src/intraday_flow.rs`, `te_core::runtime::intraday_flow`)
owns the tick loop; Python owns the seams that do real work (tick, flatten, close mark,
heartbeat, gate, router). W6-F4c removed the remaining Rust→Python→Rust trampolines, so
`intraday_flow.rs` now calls the native implementations directly and only the kept seams
go through Python.

## 3. Goals and non-goals

- **Goals:** one native intraday loop with no round-trip trampolines; identical ledger
  events, order, refusals and result dicts.
- **Non-goals:** moving the kept seams (tick, flatten, close mark, heartbeat, gate, router)
  into Rust — those stay Python (tests monkeypatch `_tick`, `_flatten` and
  `_write_heartbeat`).

## 4. Prior art and patterns considered

None.

## 5. Architecture

`IntradayService.run` → `native.intraday_run` → `intraday_flow.rs` `Host` (`FlowHost`) →
`te_core::runtime::intraday_flow::run` / `tick` / `wait`. The host reads the ledger and
clock natively; the tick, flatten, close mark, heartbeat, gate and router seams are reached
through Python `call_method*`.

## 6. Design

The tick, stale-quote, heartbeat and idempotency rules are named in the Summary. Failure
modes: a stale quote flat-and-refuses; an unexpected error tries to be flat on a fresh
quote, alerts loudly and exits; a fresh heartbeat from the same session refuses a second
instance.

## 7. Alternatives rejected

None.

## 8. Decisions

None.

## 9. Rollout and verification

Targeted tests: `tests/test_intraday_service.py`, `tests/test_p4c_intraday.py`.

## 10. Open questions

None.

## 11. Review log

| Date | Reviewer | Comment | Resolution |
|---|---|---|---|

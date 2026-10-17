# Ledger invariant audit — 2026-10-17

Tool: `tools/ledger_invariants_audit.py` (read-only, `file:<path>?mode=ro`, uri=True;
rows folded with the Rust fold through `trade_engine_rs` via
`trade_engine.ledger.state.fold_account`, one account at a time so one refused account
hides no other). Tests: `tests/test_ledger_invariants_audit.py` — one clean ledger passes
and one seeded defect fails, per invariant (14 tests, all passing, run with the venv313
pytest, only this file).

## Run against the three p7 ledgers (exit code 0 each)

```
options-ledger-p7.db: 1520 rows, 7 accounts
OK A1 seq strictly increasing, no gaps 1520
OK A2 rows decode and refold without refusal 1527
OK A3 command_id unique per account 1520
OK A4 cash ties out to cash flows, fills and fees 7
OK A5 position quantity equals signed fill sum; flat means no lots 7
OK A6 open lots remain in acquisition order 0
OK A7 every order one state; terminal events leave terminal orders 4
OK A8 no fill after a terminal order state 36
```

```
scan-ledger-p7.db: 7632 rows, 12 accounts
OK A1 seq strictly increasing, no gaps 7632
OK A2 rows decode and refold without refusal 7644
OK A3 command_id unique per account 7632
OK A4 cash ties out to cash flows, fills and fees 12
OK A5 position quantity equals signed fill sum; flat means no lots 12
OK A6 open lots remain in acquisition order 0
OK A7 every order one state; terminal events leave terminal orders 252
OK A8 no fill after a terminal order state 122
```

```
options-0dte-ledger-p7.db: 6987 rows, 1 accounts
OK A1 seq strictly increasing, no gaps 6987
OK A2 rows decode and refold without refusal 6988
OK A3 command_id unique per account 6987
OK A4 cash ties out to cash flows, fills and fees 1
OK A5 position quantity equals signed fill sum; flat means no lots 1
OK A6 open lots remain in acquisition order 0
OK A7 every order one state; terminal events leave terminal orders 26
OK A8 no fill after a terminal order state 164
```

All three ledgers are clean on every check; none contains an `Expiry`, `Exercise` or
`Assignment` row, so no settlement flow was left untied in this run (see the A4 note
below — on a ledger that does carry settlement rows those flows would be reported as
untied, not failed). A6 examined 0 positions on all three (no position currently holds
two or more open lots).

## A4 — what was tied out, what was not

Tied out, per `crates/te_core/src/ledger/fold.rs`, from the raw rows only:

- `CashFlow.amount` (a deposit, withdrawal, interest, borrow fee);
- each `Fill`: signed gross − fee (a buy pays `qty·price·multiplier + fee`, a sell
  receives `qty·price·multiplier − fee`); the multiplier is 1 for an equity, the
  contract's stored multiplier for an option (README I6);
- each `Expiry`: books no cash (its exit price is zero in fold.rs).

Not tied out and reported, not guessed: `Exercise`/`Assignment` settlement cash.
Fold.rs books intrinsic-value settlement for cash-settled contracts and
strike-times-shares delivery for physical ones, with the rules in te_options' lifecycle
(`lc::`); the ledger rows alone do not state them, so an account carrying those events
is reported per account as untied rather than failed. The README's
"Architecture & Invariants" wording ("deposits + realized pnl − fees ± settlement")
would double-count fees: the fold already books each fee into `realized_pnl`
(`updated.realized_pnl = closed pnl − fee`), so the tie-out above counts each fee once.

## Findings about the schema (not violations)

- A3 is already enforced by the store's schema: `events` carries a partial UNIQUE index
  `ux_events_command_id ON events(command_id) WHERE command_id IS NOT NULL`. The audit
  re-derives the property from rows so a dropped/tampered index is caught (the seeded
  defect drops the index, then stores the replayed command twice).
- A fold-legal defect the audit can see: replaying an `OrderSubmitted` whose order has
  moved on (e.g. FILLED) refuses as a fold refusal (A2), not as an A3 finding; a replay
  of an order still `Submitted` folds as a no-op and is caught by A3's raw-row check.

## Not checked, and why

- **A5/A8 long-only clause** — "no negative quantity on a long-only cash account":
  nothing in the README's invariants (I1–I13) or `crates/te_core` marks an account as
  long-only; the fold books a flip to a short position without refusing, and there is no
  account configuration in the ledger to consult. Which accounts the clause applies to
  is not defined, so it is not checked.
- **A4 Exercise/Assignment settlement flows** — as above: defined by te_options' lifecycle
  rules, not by the ledger itself; reported per account as untied rather than guessed.
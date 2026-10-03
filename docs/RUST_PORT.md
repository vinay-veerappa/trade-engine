# Trade Engine: phased port to Rust (R5)

This is the canonical plan and status for moving this engine to Rust. The
umbrella decision lives in `tvDownloadOHLC/docs/architecture/STRATEGY_WORKFLOW.md`
§15.16 (R5 row), which points here.

## Decisions (2026-10-02)

| # | Decision | Why |
|---|---|---|
| D1 | **Phased (strangler) port.** One module at a time is replaced by Rust behind its unchanged Python API. | Each phase ships and stays useful if the port pauses. Only quiet modules move while the active work (`tos_paper`, `ledger`) continues in Python. |
| D2 | **The Rust lives in this repo**: a Cargo workspace under `crates/`, Python reaches it through ONE pyo3 module, `trade_engine_rs`. | The engine is its own product; Vela, NT8 and `scan_engine` are clients. It does not depend on `tvDownloadOHLC`. The rule "one pyo3 module per repo" replaces "one pyo3 module". |
| D3 | **No second reader.** The phase that ports a module deletes its Python logic in the same commit; the Python file shrinks to a thin shim that converts types and calls Rust. | Two implementations of one rule drift. |
| D4 | **The Python engine is the oracle.** A third-party library the Python module wrapped (`exchange_calendars`, `vollib`) stays as a **test-only** dependency, and an oracle test compares Rust against it across a dense grid. | Parity is measured, not asserted. The existing Python tests must pass unchanged through the shim. |
| D5 | **A missing `trade_engine_rs` is an ERROR**, never `importorskip`. `tools/ci_local.py` builds it (`maturin develop --release`) before running the tests. | A skipped test reads like a pass. |
| D6 | **Money is `rust_decimal` with `checked_*` arithmetic**, crossing the language boundary as strings. Floats only where the Python already uses floats (greeks, IV). | The ledger is exact; an overflow must refuse, not wrap. |
| D7 | The engine owns its exchange calendar (`te_core::calendar`, XNYS 2000-2040, with early closes and special closures). `tvDownloadOHLC`'s `spine_data::nyse` (2022+, no early closes) should later consume it instead of keeping its own table. | One calendar per rule; the engine's is the complete one. |

## Library choices (researched 2026-10-02)

| Need | Use | Avoid |
|---|---|---|
| Implied vol | `implied-vol` (Jäckel "Let's Be Rational", the same algorithm as `vollib`; returns `None` for an impossible price) | nautilus `imply_vol` (`unwrap_or(0.0)` turns "no answer" into 0) |
| Decimal | `rust_decimal` | f64 money, nautilus fixed-point |
| Storage | `rusqlite` + `serde` | an event-sourcing framework |
| Lock | `fd-lock` | — |
| HTTP | `axum` | — |
| Tests | `rstest`, `proptest`, `insta` | — |
| Sim broker | nautilus `SimulatedExchange` behind an adapter (P3, evaluate then) | nautilus risk engine (short-circuits rules), margin (flat %), settlement (last trade), reconciliation (inferred fills; LGPL) |

Nothing open-source covers the OMS, Reg-T option margin, OCC symbol parsing, the
TOS mirror or the outbox. Those are written here.

## Phases

Ordered by churn (commits in the last 7 days, measured 2026-10-02) and
dependency. A phase's gate must be green before the next one starts.

| Phase | Moves | Python lines | Gate | Status |
|---|---|---|---|---|
| **P0 Harness** | `crates/` workspace, `te_core` (pure Rust, no pyo3), `te_py` → `trade_engine_rs`; `ci_local.py` and CI build it | — | `import trade_engine_rs` works in CI; a missing module fails the run | **done**: `tools/ci_local.py` `build_extension` (fails the gate on a failed build or import) and `check_rust_invariants` (I7 in Rust) |
| **P1a Calendar** | `calendar/sessions.py` | 199 | `tests/test_calendar.py` unchanged; oracle: every date 2000-01-01..2040-12-31 agrees with `exchange_calendars` XNYS on session / open / close / early close | **done**: `tests/test_calendar_oracle.py` (4 mutants of the rules, all killed) + `tests/test_calendar.py` unchanged |
| **P1b Greeks & IV** | `market_data/greeks.py` | 122 | `tests/test_option_chains.py` + greeks tests unchanged; oracle: IV, price and greeks match `vollib` over a strike × expiry × vol grid, and every price `vollib` refuses, Rust refuses | **done**: `tests/test_greeks_oracle.py` (price and greeks rel 1e-9 / abs 1e-12 and IV vs vollib over a 2x7x7x7x4x3 grid, refusals both ways, 5 mutants of `greeks.rs` all killed) + `tests/test_option_chains.py` unchanged; `vollib` is test-only |
| **P1c Margin** | `metrics/margin.py`, `metrics/option_margin.py` | 795 | margin tests unchanged; property tests on the Reg-T formulas | **done**: `tests/test_margin_parity.py` (dense grid vs the frozen pre-port Python in `tests/frozen_margin/`, exact Decimal equality and identical refusal type/message; 5 mutants of margin.rs / margin/option.rs, all killed) + `te_core` unit and proptest properties in `margin.rs` and `margin/option.rs`; the two Python files are shims over `margin_*` in `trade_engine_rs` |
| **P1d Risk rules** | `risk.py` (every rule recorded, no short-circuit) | 934 | `tests/test_risk_engine.py` unchanged; recorded order × account corpus produces identical verdict lists | **done**: `tests/test_risk_parity.py` (1837 generated order x account x control x instant cases vs the frozen pre-port Python embedded from commit b9274d9: identical verdicts, ordered rule results, control events; construction and `from_mapping` refusals with identical type and message; dense `TradingHours.is_open` sweep) + `te_core::risk` unit tests (no short-circuit: all 21 rules present when the first fails; boundaries; overflow refuses; drawdown latches); `tests/test_risk_engine.py` unchanged; 8 mutants of `risk.rs` (`>=` vs `>`, kill switch ignored, wrong timezone, drawdown thresholds x2, hours close `<=`, short-circuit, dropped rule), all killed. Scope: rules and config validation are Rust (one JSON document in, one out); **ledger reads and writes stay Python until P2** (`risk.py` reads state, calls Rust, appends the control events). Accepted deviations (Rust refuses where Python accepted): non-ASCII digits in an integer rule, integers beyond i64, decimals beyond 28 places, a truthy non-str `industry`. |
| **P1e Option rules** | OCC parse/format and contract validation in `domain/instruments.py` (the dataclasses stay as Python carriers until P2), `domain/option_roots.py`, `domain/option_lifecycle.py` | ~470 | `tests/test_option_chains.py` and lifecycle tests unchanged; parity vs the frozen pre-port Python over every OCC string shape, root and settlement case, identical refusals | **done**: `tests/test_option_rules_parity.py` (frozen pre-port Python embedded from commit 0f26fb3 in `tests/frozen_option_rules/`; equality of value (Decimal by `str`, so scale counts) or refusal (type name AND message): ~1,500 generated OCC strings (canonical and compact, root 1-6, lowercase, padded, bad dates incl. Feb 29 non-leap, YY pivot 68/69/70, strike 0 and 99999999, malformed, non-str), every MMDD for nine years plus 20,000 seeded six-digit dates, a code-point sweep of `strip`/`\s`/`\d`/`int()`/`upper()` (2,384 points), ~80 strikes x multipliers and ~22 rights x ~23 underlyings, error order across four bad fields, every index root, settlement and last-trade for SPX/SPXW/AAPL over every day 1999-12-30 to 2041-01-03, and lifecycle grids (intrinsic, outcome, delivery, dividend incl. the bid-intrinsic == dividend tie)) + `te_core::options` unit tests; `test_option_chains.py`, `test_option_lifecycle.py`, `test_instrument_resolver.py` unchanged. 9 mutants of the Rust, all killed: SPX AM->PM (`test_option_style_and_chain_roots[SPX]`), 3-dp check dropped (`test_constructor_huge_exponents`), OCC strike bound `>` to `>=` and year pivot 68->69 and U+001C..1F not whitespace (all `test_from_occ_matches`), put intrinsic sign swapped (`test_intrinsic_and_outcome_over_the_grid`), dividend `<` to `<=` (`test_exercised_for_dividend_over_the_grid`), decimal scale dropped on add/sub and put premium floor `<=` to `<` (both `test_delivery_over_the_grid`). Preserved defects: `%y` pivot (69 -> 1969, 70 -> 1970), a strike of more than 28 significant digits slips the 3-dp check (`strike * 1000` rounds first), Unicode digits and whitespace accepted in an OCC string, `ß` upper-cases to `SS`, and `strptime`'s first-match regex (`261301` reads as month 1, day 3, leftover `01` refused by its own message). Accepted deviations (Rust refuses or differs where Python accepted): a non-int `multiplier` (100.0, `Decimal(100)`), non-Decimal/int money (floats), infinite or exponent-form (`1E+2`) money and money beyond 28 digits in lifecycle, `settlement_instant`/`last_trade_date` take only the XNYS `ExchangeCalendar` (every caller does; a fake calendar now raises), the 3.13 wording of the day-range `strptime` error, characters newly cased after Unicode 15.1 (the echoed text of a refusal). |
| **P2a Ledger shadow** | Rust domain + event types, the event codec and the fold (`ledger/state.py`, `ledger/mirror.py` fold) in `te_core`, with NO Python reader: production still runs the Python ledger | — (additive) | byte parity: every event the Python codec writes, Rust decodes and re-encodes to identical bytes; fold parity: every generated event stream folds to an identical `AccountState` (as canonical JSON); every Rust refusal matches a Python refusal. Until P2b the two implementations co-exist, so any ledger change must land in both; the parity test is what makes a one-sided change fail | **done**: `te_core::ledger` (codec, fold, mirror fold) exposed as `ledger_reencode` / `ledger_fold` / `ledger_fold_all`; gates `tests/test_ledger_codec_parity.py` + `tests/test_ledger_fold_parity.py` (155 tests: the per-kind zoo and every prefix, the streams 20 existing suites fold, 1500 seeded episode streams a third perturbed); 8 hand mutants killed (P&L sign, FIFO→LIFO, duplicate fill, fee into cash, fee out of realized, any transition legal, unsorted keys, non-ASCII unescaped). `tools/ledger_parity.py` (local, `mode=ro`) over the real ledgers 2026-10-02: PM-B mirror 4095, 0DTE 3278, PM-A options 436, scan 6296 events, codec and fold identical, 0 strict refusals. Sanctioned asymmetry: Rust refuses as `strict`/`unsupported` field types Python is lax about (counted, never the reverse) |
| **P2b Ledger switch** | `codec`, `state`, `store` read through `trade_engine_rs`; the Python codec/fold deleted (D3) | ~4.6k | P2a parity tests become the regression gate; every existing ledger test unchanged; a local tool folds the real paper ledgers both ways before the switch | **done**: the oracle is FROZEN first: the pre-port `codec.py`/`state.py`/`mirror.py` embedded in `tests/frozen_ledger/` (imports rewritten to each other), and `ledger_gen`, the recorder plugin, `test_ledger_fold_parity.py` and `tools/ledger_parity.py` compare Rust AND the production shims (`prod_*`) against it, never against production. Deleted from Python (D3, same commit as the shims): the codec's per-type encode/decode and validation (`codec.py`), every fold handler, `apply_fill`/`_apply_trade`/lot consumption, the lifecycle handlers and `FoldCache`'s fold (`state.py`), the mirror fold steps, `pro_rata` and `ticket_contracts` (`mirror.py`); 1,098 lines deleted, 559 added (net -539) across `codec`/`state`/`mirror`/`store`/`reader` plus the new `errors.py` (the exception classes, `__module__` kept) and `_rs.py` (the one door: a Rust refusal crosses as `ValueError(kind, message)` and is raised as the pre-port exception type for that kind). Rust gained `te_core::ledger::bridge` (row/payload checks, carrier JSON in) and `canon::export_full`/`export_delta`; `trade_engine_rs.LedgerFold` is the `#[pyclass]` holding folded state per account: the store's append applies its stored row ONCE (no refold), the reader applies only rows past its last fold, `FoldCache` is an atomic handle; Python carriers are built on first read and then PATCHED from Rust's delta (`+t` new fills, `+fs` new fill ids, `+m` changed map entries), holding no rules. Gates: full suite 1880 passed (every pre-existing ledger test unchanged); `tools/ledger_parity.py` over the real ledgers 2026-10-03, codec/state/fold all identical: PM-B mirror 4095, 0DTE 3278, PM-A options 436 (9 accounts), scan 6296 (12 accounts) events; new `tests/test_ledger_p2b_glue.py` reads state between EVERY event (FoldCache over 300 seeded streams, store and a concurrent `LedgerReader` over 100) against the frozen oracle, and pins the handle's refusal contract and the seeded `base_seq` boundary. 10 hand mutants of the glue, all killed (each restored, run under `python -B`, Rust ones rebuilt; final restore + rebuild unconditional): carrier served while stale, kind `fold` mapped to `ValueError`, `+m` drops untouched entries, `base_seq` `<=` to `<`, store keeps touched accounts after a failed write, seed ignored, `+fs` replaces, Rust `fills_held` never advances, atomic apply skips its restore, non-atomic decode refusal keeps the account (the last five first SURVIVED the existing suites, which read a carrier only once per fold; the glue test is what kills them). Timing, 5000 appends: 2.33-2.34 s vs 2.82-2.92 s pre-port (gate <= 2x). Deviations: `register_handler` stays importable but always raises `LedgerFoldError` (no callers outside `tests/test_ledger.py`'s import; a Python handler would be a fold the store, reader and restart never run); `HANDLERS` is a read-only map whose values are the Rust step; the `MirrorState` read-only queries (`handled`, `open_tickets`, `exposure`, `expected`) stay Python; codec and fold messages are Rust's text; `KeyError`/`JSONDecodeError`/decimal-signal refusals are rebuilt from kind + message (no `pos`/`doc` on a JSON error); `strict`/`unsupported` inputs and lone surrogates refuse as `PayloadCodecError`; carriers are rebuilt from JSON, so no object identity survives a fold (a `FoldCache` seed object is not the state returned after an event), datetimes come back via `fromisoformat`; `ticket_contracts` returns a plain dict; the store folds the row AS STORED, so a payload that encodes but would not decode is refused at append rather than on the next restart; a cold `state()` decodes and folds row by row, so with two defects in one log the first refusal may name a different one; the store drops touched accounts on any `BaseException` (rollback still only on `Exception`). |
| **P3a Fill & option-risk rules** | `sim/broker.py`, `sim/snapshot_venue.py`, `oms/trailing.py`, `risk_options.py` (and the structure reading in `oms/options.py` they share) | ~2.0k | a FROZEN copy of the pre-port Python (`tests/frozen_p3a/`) and the production shims are driven in lockstep by seeded generators; every step agrees on the return value (Decimals by `str`) or on the refusal (type name AND message) | **done**: `te_core::sim` (`broker`: the one-minute-bar equity book, bar sequencing, MARKET/LIMIT/STOP/STOP_LIMIT matching, gaps, slippage, brackets, OCO and target priority, the late-exit and inside-entry-bar stop rules, DAY/OPG expiry, restore; `snapshot`: the chain-snapshot book, model price, combo shading, fees, stale quotes, DAY expiry, restore; `trailing`), `te_core::oms::structures` (`open_structures`, `uncovered_calls`) and `te_core::risk_options` (every option rule from `regime` to `covered_calls`, entry-quote gates included, recorded without short-circuit), reached through `trade_engine_rs.SimBook` / `SnapBook` / `trail_update` / `oms_open_structures` / `oms_uncovered_calls` / `option_risk_evaluate` (one door `sim/_rs.py`: a refusal crosses as `ValueError(kind, message)` and is raised as the pre-port type). Deleted from Python (D3, same commit as the shims): 1,742 lines, 640 added (net -1,102) across `broker.py` (-732/+213), `snapshot_venue.py` (-380/+214), `risk_options.py` (-513/+105), `oms/options.py` (-98/+29), `trailing.py` (-19/+36) and the new `sim/_rs.py` (43). The duplicate-protection and persistent kill-switch checks and the rules' config validation stay Python (they read the ledger/config, not the book). Gates: `tests/test_p3a_parity.py`, 8 tests, 614,180 compared steps per run: `SimBroker` 900 seeded walks (513,514 steps: 13 sessions incl. both DST regimes, early closes and holiday eves; every order type and TIF, both sides, gaps, bars touching or one cent short of a working price, brackets sent right after their entry fills, replaces, cancels, clock jumps, reads exactly at the close, nine kinds of malformed bar, restore 25% of walks, a third of them perturbed), `SnapshotVenue` 2,000 walks (73,847 steps: singles, shares, 2-4 leg combos, limits on the model net, stale/zero/look-ahead quotes, DAY expiry, restore with leg perturbations, invalid constructor arguments), `underlying_of` 331 instruments, trailing 1,500 seeds (18,920 updates incl. 0, negative, NaN, Inf), `open_structures`/`uncovered_calls` over 1,000 folded books (2,703 calls; entries working, partial, filled, cancelled, targets, closes, expiries), and 4,865 `OptionRiskEngine.evaluate` verdicts against random rules (every optional rule on and off, every entry gate) where each of the 24 rule names both passes and fails (`entry_quote.vertical` at least evaluates). Full suite 1896 passed (1888 pre-existing, unchanged: no existing test edited, plus the 8 parity tests); `cargo test -p te_core` 78 passed. 11 hand mutants of the Rust, each rebuilt, run under `python -B`, restored in `try/finally` with an unconditional final restore + rebuild + green parity run: all 11 KILLED: buy-stop gap fill at the stop instead of the open, buy limit needing `low < limit`, sell stop read on the high, DAY expiring strictly after the close, slippage sign, venue fee per order instead of per contract, snapshot sell limit needing `price > limit`, trailing sell trigger `<`, `min_short_bid` as `>=`, margin no longer passing when the entry reduces it, and an early return after a failed `regime` (the rule short-circuit). The DAY-expiry mutant first SURVIVED: a read with the clock exactly at the close was too rare, so the walk gained `to_close` (simulate through the last regular bar, read AT the close); then killed. Deviations: one production defect found by the generator and fixed before commit (a `SnapshotVenue.restore` refused part-way left Rust holding restored positions the shim had not recorded, so `positions()` raised `IndexError`; the shim now records them before the call); datetimes cross as ISO strings and come back via `fromisoformat` (the fold/DST semantics of the pre-port `astimezone` are reproduced, not shared); `SnapshotVenue` accepts only the XNYS calendar; `max_quote_age_seconds` crosses as f64; a fill number or leg id with non-ASCII digits, or a fill number beyond u128, refuses (`unsupported`) where Python would parse it; restore consumes its iterables eagerly; `is_structure`/`legs_of` exist in both languages (the Python ones are domain helpers other modules use); the Reg-T margin request for the risk book is built in Rust, duplicating `metrics/option_margin.py`'s input shape until P3b; snapshot quotes are precomputed eagerly in Python; `equity_marks` parse errors are reported in insertion order; earnings dates cross as ISO strings and `regime_of` must return `str` or `None`; the new pyfunction is `option_risk_evaluate` (P1's `risk_evaluate` keeps its name). |
| **P3b-1 OMS core, non-manager half** | `oms/reconcile.py`, `oms/restore.py`, the remaining decision logic of `oms/options.py` | ~0.9k | a FROZEN copy of the pre-port Python (`tests/frozen_p3b/`) and the production shims are driven in lockstep (two worlds, each with its own ledger, clock and `SnapshotVenue`); every step agrees on the return value (Decimals by `str`), or on the refusal (type name AND message), and after every step on the ledger events and the outbox; folded state byte for byte at the end of a walk | **done**: `te_core::oms::reconcile` (which venue orders `reconcile_after` re-reads; the unknown-order refusal (I5); the journal payload of one fill, its asset class, multiplier, bracket stop and target, the missing-event refusal (I1)), `te_core::oms::restore` (which orders, brackets, fills and positions rebuild a venue; what a PENDING_UNKNOWN order becomes in `refuse` and `resolve` mode; the pending-request classification; the refusals), `te_core::oms::options` (intent and order fingerprints with an in-crate SHA-256, replay and fingerprint conflicts, `plan_open` duplicate (C4) and uncovered (C3) refusals, `plan_close` incl. the out-of-ratio refusal and the `:close:N` numbering, `plan_holding`, and the `sync` plan), reached through 15 `trade_engine_rs.oms_*` functions (`oms_orders_to_read`, `oms_restorable`, ...) and the one door `sim/_rs.py` (refusal kinds `reconcile`, `restore`, `option`, `duplicate`, `uncovered`, `closed`, `idempotency`). Ledger reads and writes and venue I/O stay Python and cross as callbacks (`restorable` makes its two ledger reads in the order Python did). Deleted from Python in the same commit as the shims (D3): 197+48+83 lines, 86+36+77 added (net -129). Gates: `tests/test_p3b_parity.py`, 7 tests: 631 random folded books (5,747 compared steps: open new/replayed/conflicting, close incl. bogus ids and reused commands, `close_holding`, `sync`, `restorable`/`restorable_positions` in both pending modes and with malformed modes, journal fills recorded or not, ORDER_PENDING events injected), 220 flow walks (10,277 compared steps over real `SnapshotVenue` sessions: entries with and without targets, snapshots that fill them, `reconcile_after` with and without the journal, `sync`, closes, requests left pending, restarts that rebuild the venue in `refuse` and `resolve` mode, a venue order the ledger never knew, orders whose parent the ledger lacks), `reconcile_after` re-read order over 631 books with NEW, working, terminal and unknown venue orders, and the import check (`trade_engine_rs` is imported unconditionally: missing is an ERROR, D5). Coverage asserted on the tally: every refusal kind both happens and succeeds (reconcile, restore, key, option, duplicate, uncovered, closed, idempotency). Full suite 1903 passed (1896 pre-existing, unchanged; plus 7); `cargo test -p te_core` 79 passed. 16 hand mutants (`tools/mutate_p3b.py`: rebuilt each time, `python -B`, original bytes restored in `finally`, final restore + rebuild, baseline run first), all 16 KILLED: restore keeping NEW children, a cancel-pending order restored as ACCEPTED, the pending-cancel test ignoring the reason, a forgotten earlier-unresolved order, a combo target on every leg of the journal, `reconcile_after` re-reading NEW orders, the latest fill read as the earliest, `sync` cancelling a non-target on a settled leg, `sync` ignoring a terminal entry, `close_holding` skipping the cover check, the over-close test on the signed holding, duplicate protection counting flat positions, duplicate protection counting terminal entries, a `close` replay of any target, `:close:N` numbering from 0, and the out-of-ratio check off. Two first SURVIVED and were strengthened: `reconcile_after` re-reading NEW orders (books never held a NEW order, so a direct test appends NEW children and has the venue list every order) and the latest-fill tie rule (an equivalent mutant: on a tie both choices read the same timestamp, so the mutant became the earliest-fill one, which the flows kill). Deviations: the order fingerprint takes its UTC offset from the order's own `created_at` rather than the zone at year 1 (identical for the UTC clocks every runner uses); the non-str `pending` guard stays in the shim (same message); a non-ASCII, surrogate or overflowing value refuses as in P3a; `restorable` scans `ledger.events` once, and only when a PENDING_UNKNOWN order exists in `resolve` mode; `close_holding` raises its held/uncovered refusals before reading the clock, so a naive clock together with a refusal now raises the refusal; `enqueue_journal_fill` looks the ledger event up before the order lookups (the I1 refusal still comes after them); pyo3 reaches the shims only through `trade_engine.sim._rs`, there is no `oms/_rs.py`. |
| **P3b-2a OMS manager decisions** | Pure validation, allocation, fingerprints, classification and command plans in `te_core::oms::manager`; one binding in `trade_engine_rs` | 1,668 pre-port | Frozen-manager lockstep, refusal counters, Rust hand mutants, unchanged tests and real-ledger parity | **done**: `tests/test_p3b2_parity.py`, `tools/mutate_p3b2.py`; measured evidence and boundaries below |
| **P3b-2b OMS manager orchestration** | Remaining ledger/clock/venue I/O in `oms/manager.py`; move process ownership without changing sequencing | remaining host orchestration | Preserve durable-before-network ordering and exact callbacks/read-back behavior | **pending**; P3b-2a does not move storage, clocks or broker transports |
| **P4 Runtime flip** | `eod`, `intraday`, `lifecycle`, `sinks`, `server` (axum). The Rust process owns the single-instance lock; Python callers become clients | ~3.9k | a paper session run side by side with the Python engine produces the same ledger | after P3b |
| **P5 TOS mirror** | `tos_paper` logic; the UI-automation transport stays Python behind a callback | ~3.4k | mirror tests unchanged; a paper round trip matches | last (most active module) |
| **P6 Browser & retire** | `web/engine`, `replay-sim` → wasm; delete the Python package | ~1.5k | browser replay matches the engine | after P5 |

### P3b-2a verification and boundary

The oracle is `tests/frozen_p3b2/oracle_manager.py`, frozen from clean `c878969`
**before** the production edits. Its bytes match that manager apart from the
import of the frozen P3a trailing emulator. It does not import production manager
decision helpers. The original tests were not edited.

Rust owns:

- Equity whole-share validation; split, largest-remainder and fractional/runner
  allocation, partial-fill target budgets and reduce rounding. Equity allocation
  uses the existing arbitrary-precision integer dependency, not a bounded
  integer product or f64. Decimal arithmetic reuses the checked P2b `PyDec`
  helpers, preserving precision, scale and Python decimal signals; all decimal
  carriers cross as strings. Huge integer, huge-exponent zero and nonfinite
  helper parity are explicitly tested.
- Bracket/reduce/order fingerprint field selection and hashing, using the
  existing exact JSON and SHA helpers. Optional fingerprint fields, Unicode and
  decimal spellings remain significant.
- Native type/stop capability, TIF and trigger choices; entry/child/stop/reduce/
  closer/sibling classification; working, terminal and unresolved-replace rules.
- Create, reduce, close, move-stop, replace and replay validation; emulated
  observation/routing plans; child sizing and synchronization; submit/cancel/
  replace acknowledgement classification and reconciliation decisions.

Python retains only ordered I/O and boundary plumbing:

- Read cached account/order contexts, original creation events, idempotency
  events and venue read-back. Unresolved replace scans preserve the original
  short-circuit order: read accepted first, rejected only if needed, stop at the
  first unresolved request.
- Read the injected clock, validate its timezone awareness, construct domain/
  event/venue carriers, execute Rust plans and append events in the original
  order. The original domain constructors still own carrier validation. Missing
  lookup KeyErrors and creation-order iteration are ledger lookup plumbing.
- Call broker submit/replace/cancel/orders/fills and wrap transport exceptions
  exactly as before. No new broad catch or fallback was introduced. Venue
  override/default selection, missing-value marshalling and action dispatch are
  carrier plumbing, not duplicate OMS rules.
- Keep protection sizing durable **before** reading/validating target budgets;
  process reconciled fills before terminal venue state. Stop/target cancellation
  and emulated-trigger submission retain their original I/O ordering.

The extension is mandatory (D5). Manager exceptions, `StopIteration` and the
Python integer-conversion `OverflowError` are registered through `sim/_rs.py`;
there is no optional import, `importorskip`, silent fallback or second Python
decision implementation. No new accepted business-rule deviation was introduced;
the pre-existing P2b decimal-context boundaries still apply.

Measured parity (10 tests):

| Campaign | Checked steps/cases |
|---|---:|
| 48 seeded bracket command walks, 38 steps each | 1,824 |
| Native/emulated STOP, STOP_LIMIT and TRAIL, both sides | 216 |
| 24 independent SimBroker/SnapshotVenue worlds | 360 |
| Replay, conflict, capability and ambiguous-replace campaign | 74 |
| Network, TIF, cancellation/OCO and reconciliation boundaries | 68 |
| Datetime-equivalent carrier replay | 2 |
| Partial entry/target/stop fills, runners, submit acknowledgements | 40 |
| Native-stop flag independent of advertised types | 8 |
| **Total lockstep command steps** | **2,592** |
| Quantity/fingerprint, unbounded-integer and nonfinite helper comparisons | **949** |

Each command compares the exact return or refusal **type name and message**,
ledger events and outbox; each world ends with canonical folded-state equality.
Coverage counters assert successful counterparts and refusals. Measured allocation
results are 104 successes/72 refusals, split 80/30 and fractions 77/55; fingerprint
comparisons are bracket 300, reduce 100 and order 100. Further examples:
observations 30 successes/30 refusals, local replaces 8/4, trail API 2/10;
real-venue price processing and reconcile each succeed 96 times; real-venue stop
updates succeed 38 times and refuse 58 times. Every seeded walk exercises 48
successful reduces/closes and 48 refusals each for their working-order overlaps,
protective cancellation, loosened stops, zero-sized reduces and replay conflicts.

`tools/mutate_p3b2.py` builds a green baseline, rebuilds every mutant, runs Python
with `-B`, restores original source bytes in `finally`, and unconditionally
rebuilds and checks green restored parity. The final campaign kills **16/16**
compiling hand mutants:

| Mutant | Killing parity test |
|---|---|
| Reduce rounds nearest instead of floor | quantity/fingerprint grid |
| Allocate smallest remainder first | quantity/fingerprint grid |
| Break allocation ties last-first | quantity/fingerprint grid |
| Omit option allocation residue correction | quantity/fingerprint grid |
| Drop fraction runner | quantity/fingerprint grid |
| Omit fingerprint quantity | quantity/fingerprint grid |
| Omit fingerprint entry type | quantity/fingerprint grid |
| Omit STOP_LIMIT fingerprint limit | quantity/fingerprint grid |
| Ignore native-stop capability flag | independent native-stop flag |
| Prefer LIMIT over MARKET trigger | refusal/ambiguous replace |
| Disable TIF check | boundary counterparts |
| Stop touch is exclusive instead of inclusive | boundary counterparts |
| Activate targets before terminal entry | seeded bracket walks |
| Drop synchronization runner budget | seeded bracket walks |
| Permit reduce with working close | seeded bracket walks |
| Treat pending replace acknowledgement as resolved | refusal/ambiguous replace |

The first flag mutant survived: the non-native fixtures did not advertise STOP.
The independent-flag campaign closes that gap; the mutant was **not** weakened.

Final gates: `cargo test --manifest-path crates/Cargo.toml -p te_core` **83 passed**;
`python tools/ci_local.py --include-uncommitted` **exit 0**, I7/version/extension
checks green and **1,913 Python tests passed**. Read-only
`python tools/ledger_parity.py` **exit 0**: mirror-PM-B 4,095 events, options-0dte
3,278, options 436, scan 6,296; all **14,105** codec rows, **23 ledger/account
states** and four folds identical. No user ledger or log was moved or deleted.
Builds and probes explicitly use this worktree's `src` with the shared interpreter;
extension installation is never assumed to match source.

Preserved pre-existing behavior/defects, not silently repaired:

- A locally emulated protective stop stays NEW; management can refuse it because
  `_open_bracket_stop` requires ACCEPTED/PARTIALLY_FILLED venue protection.
- Status-only read-back cannot resolve working pending-replace terms; terminal
  read-back can resolve them.
- Replace treats any acknowledgement other than PENDING/REJECTED as accepted,
  including an unrecognized status.
- Replayed close ignores a changed reason once the same command has been sent.
- Native submit replay returns persisted non-NEW state, including pending state,
  without resubmission; emulated pending submissions have their separate refusal
  behavior.

Re-verified independently before commit (2026-10-03): `ci_local.py` 1,913 passed,
`ledger_parity.py` exit 0. Known leftovers for P3b-2b: the decision ops
`kind_is`, `replay` and `unresolved` have no caller, and the OMS tests run about 35%
slower than pre-port (one JSON round trip per decision; 87 call sites).

### Costs of phasing (accepted)

- Until P4 Python calls Rust across pyo3 with plain values or JSON; most of that
  marshalling is thrown away at the flip.
- P1 functions that take domain objects (risk rules) receive them as JSON until
  P2 makes the types native.
- P4 is the one large switch (who owns the process); the side-by-side paper
  session exists to make it safe.

## Working rules

- Build: `python -m pip install --no-deps --force-reinstall ./crates/te_py`
  (maturin backend via `crates/te_py/pyproject.toml`; `tools/ci_local.py` does this).
  `crates/te_py/pyproject.toml` must exist: without it maturin reads the root
  pyproject and replaces the `trade-engine` editable install.
- Rust tests: `cargo test --manifest-path crates/Cargo.toml -p te_core` (also run by
  `ci_local.py`). Not the whole workspace: `te_py` links pyo3, which refuses a
  Python newer than it supports.
- Each phase is one commit series: Rust + oracle test, then the shim and the
  deletion of the Python logic, then this table's status. A row turns **done**
  only in the commit that adds the enforcing test, which the row names.

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
| D6 | **Money is `rust_decimal` with `checked_*` arithmetic**, crossing the language boundary as strings. Floats only where the Python already uses floats (greeks, IV). | The ledger is exact; an overflow must refuse, not wrap. **Exception until P7:** wherever a result is compared with the Python oracle (the ledger since P2b, the OMS, P4 runtime), money is `ledger::pydec::PyDec`, a checked port of Python's default `decimal` context, because `rust_decimal` spells results differently (scale, positive exponents, `-0`, 28-digit rounding) and the string parity gates would fail on correct values. P7 moves everything to `rust_decimal`. |
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
| **P3b-2b OMS manager orchestration** | The command flow of `oms/manager.py` (`te_core::oms::flow`); Python keeps only the host effects | 1,170 -> ~390 lines | Durable-before-network ordering, exact callbacks and read-back, frozen-manager lockstep per ticket family, unchanged `test_oms.py` | **done**: `tests/test_p3b2b_flow.py`, `tests/test_p3b2_parity.py` (now against the switched manager), `tools/mutate_p3b2b.py`; measured evidence and boundaries below |
| **P4a Runtime decisions** | `eod/runner.py`, `eod/options_routing.py`, `intraday/service.py` decisions into `te_core::runtime`; one binding in `trade_engine_rs`, thin Python shims | 2,388 pre-port | Frozen-oracle lockstep, refusal counterparts, Rust hand mutants, unchanged tests, lockstep session replay | **done**: `tests/test_p4a_parity.py`, `tools/mutate_p4a.py`; measured evidence and boundaries below |
| **P4b Lifecycle and journal decisions** | After-close expiry/assignment, source value validation and journal mapping/read-back decisions; Python keeps ordered ledger/source/network effects | 819 pre-port | Frozen lockstep with ordered ledger/source/HTTP effects, asserted refusal counterparts, compiling hand mutants and realistic-book timing <= 1.25x | **done**: `tests/test_p4b_parity.py`, `tools/mutate_p4b.py`, `tools/time_p4b.py`; evidence and ownership below |
| **P4c Runtime flip** | `server` (axum), the single-instance lock, process ownership; Python callers become clients | ~2.5k | A paper session run side by side with the Python engine produces the same ledger | pending; after P3b-2b |
| **P5 TOS mirror** | `tos_paper` logic; the UI-automation transport stays Python behind a callback | ~3.4k | mirror tests unchanged; a paper round trip matches | **done except the live round trip**: see "P5 verification and boundary" |
| **P6 Browser & retire** | `web/engine`, `replay-sim` → wasm; delete the Python package | ~1.5k | browser replay matches the engine | **web half landed** (tvDownloadOHLC main ba935583, bfe51308; `SIM_TE_WASM` default OFF); retiring the Python package waits for P4c and P5 |
| **P6b Futures in te_core** | `Instrument::Future` and tick arithmetic, the CME Globex equity-futures calendar, `Book::new_futures`, te_wasm futures books | — (additive: the Python engine has no futures book) | P3a equity parity unchanged; seeded Globex walks agree te_core vs the te_wasm API; the browser differential agrees replay-sim vs te_wasm on MNQ/MES/ES | **done**: see "P6b verification and boundary" |
| **P6C Globex calendars per root** | CME energy/metals holiday tables; the Globex calendar chosen by root; YM/MYM/RTY/M2K/CL/MCL/GC/MGC specs | — (additive) | P6b equity table byte-identical; the 1m store agrees with every calendar (allow-listed deviations with reasons); pmc energy/metals disagreements allow-listed | **done**: see "P6C verification and boundary" |
| **P7 Decimal migration** | `PyDec` → `rust_decimal` everywhere (D6 without its exception); one canonical decimal spelling for the ledger, canonical state and fingerprints | — | a one-shot, reversible migration of the stored ledgers (backup kept): every ledger re-canonicalized and re-folded, balances and positions equal by value before and after, fingerprints/idempotency keys rehashed with an old→new map so replays still dedupe; Rust-vs-`PyDec` value-equality proptests over the arithmetic; a timing comparison | after the last oracle-gated phase (the Python history is small, so the data rewrite is cheap; it waits only because every gate before it compares decimal strings with Python) |

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
`ledger_parity.py` exit 0. Its leftovers (the uncalled decision ops and the ~35%
OMS slowdown) were closed by P3b-2b.

### P3b-2b verification and boundary

`OrderManager` is now a shell: every public method is one `trade_engine_rs.oms_flow`
call, and `_Host` performs the effects the flow asks for, one call each, where the
flow makes them. It decides nothing. The Python command flow is deleted (D3).

Rust owns (`te_core::oms::flow`, driven through `crates/te_py/src/flow.rs`):

- every command: `create_bracket`, `submit`, `submit_trailing`, `update_trailing`,
  `update_emulated_order`, `record_fill`, `cancel`, `move_stop`, `close_bracket`,
  `reduce_bracket`, `replace`, `reconcile_order`, and the bracket helpers;
- the ordering: each ORDER_PENDING / ORDERS_CREATED append precedes its venue call,
  replays read the persisted command, and a read-back never resends.

Python keeps: the clock, the ledger (append, reads, the fold handle), the broker
(submit, cancel, replace, orders, fills, capabilities), and the error classes. The
pure helpers (`_validate_quantity`, `_split_quantity`, fingerprints and the others)
stay the P3b-2a shims; they are the only 10 decide ops left. The 71 decide arms the
switched manager no longer calls were removed from `te_core::oms::manager`.

Seam: `Host::send_reduce`. `reduce_bracket` sends its reduce through the host, so a
caller that patches `OrderManager._send_reduce` (the `test_oms` crash test) still
takes effect.

Gates:

- `tests/test_oms.py` unchanged and green.
- `tests/test_p3b2b_flow.py`: the flow against the frozen c878969 manager, by ticket
  family (T1-T6 and plumbing).
- `tests/test_p3b2_parity.py`: the frozen oracle against the switched manager.
- Hand mutants (`tools/mutate_p3b2b.py`, replacing `tools/mutate_p3b2.py`, whose
  targets were the deleted dispatcher): **25 of 25 killed**. One survived the first run
  and was closed by a lockstep step, not by weakening it (below).

| Mutant | Killing test |
|---|---|
| ORDERS_CREATED append skipped | `test_t6_brackets` |
| submit ORDER_PENDING marker skipped | `test_t1_submit_native_and_acks` |
| replace ORDER_PENDING before `broker.replace` skipped | `test_t2_replace` |
| cancel pending marker before `broker.cancel` skipped | `test_t3_cancel_and_fills` |
| submit replay sends the caller's NEW order, not the stored one | `test_t1_submit_native_and_acks` |
| unknown submit ack becomes ACCEPTED | `test_t1_submit_native_and_acks` |
| PENDING replace ack treated as accepted | `test_t2_replace` |
| emulated-stop replace persists the old order | `test_t4_emulation` |
| OCO siblings not cancelled before the triggered order | `test_t4_emulation` |
| failed OCO venue cancel raises the plain broker-unknown error | `test_t3_cancel_and_fills` (after the addition) |
| replayed fill gets a fresh command id | `test_t3_cancel_and_fills` |
| bracket synchronization skipped after a fill | `test_t3_cancel_and_fills` |
| stop touch exclusive instead of inclusive | `test_t4_emulation` |
| trigger prefers LIMIT over MARKET | `test_plumbing_context_planned_and_venue_order` |
| native-stop capability flag ignored | `test_native_stops_flag_independent_of_advertised_types` |
| read-back resends the order | `test_t5_reconcile` |
| terminal (CANCELLED) read-back not recorded | `test_t5_reconcile` |
| pending-replace gate dropped in reconcile | `test_t5_reconcile` |
| reduce submitted before the targets are cancelled | `test_t6_brackets` |
| close replay ignores the stored close | `test_t6_brackets` |
| reduce rounds half-even instead of floor | `test_t6_brackets` |
| runner weight zero in synchronization | `test_t3_cancel_and_fills` |
| targets re-budgeted before the entry is terminal | `test_t3_cancel_and_fills` |
| `utc_now` accepts a naive clock | `test_plumbing_context_planned_and_venue_order` |
| `append_replay` ignores a payload mismatch | `test_plumbing_append_refuse_and_planned_refusal` |

  The survivor: the only OCO-unknown step ran after the order was already pending,
  so it stopped at the "already pending" refusal. A new `oco-network` pair in
  `test_t3_cancel_and_fills` (T3 steps 104 -> 106) cancels a working OCO sibling
  whose venue cancel raises. Not equivalent: the host maps `oco_unknown` to
  `OCOOutcomeUnknownError`. The P3b-2a JSON-only mutants (fingerprint, allocation,
  TIF check) were dropped with their code; their 16/16 campaign is recorded above.
- Final gates at `fd9a851` plus these docs (2026-10-03): `python tools/ci_local.py
  --include-uncommitted` **exit 0**, invariant/version/extension checks green,
  **1,938 Python tests passed**, te_core **88 passed**. Read-only
  `python tools/ledger_parity.py` **exit 0**: mirror-PM-B 4,095 events, options-0dte
  3,278, options 436, scan 6,296; all **14,105** codec rows, **23 account states**
  and four folds identical.

Test scaffolding changed by the switch (reported, not silent):

- `tests/flow_p3b2b.py`: `FlowManager` now subclasses production and adds only the
  wrappers for the internal ops the lockstep steps call.
- `tests/test_p3b2_parity.py` drives that subclass.
- The planned-refusal comparison's reference was the deleted Python manager. Its 13
  results were recorded from the P3b-2a manager at 29bd759 into
  `tests/frozen_p3b2b/planned_refusal.json`, and the test now compares against that.

Performance: the first switch was 5x the pre-port oracle. The host serialised the
whole AccountState to canon JSON on every read (about four per command), and Rust
parsed it back. Now the host hands across the ledger's Rust fold
(`Ledger.fold_handle`), and the flow clones the AccountState in place. On 48 seeded
`bracket_walk`s, best of 3, run alone: the oracle takes **2.30 s**, the switched
flow **2.55 s** (1.11x); before the fast path the flow took 10.0 s. Both raise the
same exceptions by type (144 idempotency, 661 order management, 78 value, 119
reconciliation).

Found along the way: `codec.canon` wrote a frozenset in iteration order. A carrier's
set grows by `old | new` deltas, so equal states could print apart, depending on how
often the state had been read. `canon` now sorts a set's members by their text; Rust
already sorted on read.

### P4a verification and boundary

The oracle is `tests/frozen_p4a/` (`runner.py`, `service.py`, `options_routing.py`),
frozen from clean `88affb4` (commit `17aa252`) before the production edits. Its bytes
match that pre-port runtime apart from the import rewrites to the frozen copies.
The original tests were not edited.

Rust owns:

- EOD configuration and session boundary validation (`te_core::runtime::eod`):
  validating non-empty `job_name`, broker presence, positive bar max age, and
  non-negative settlement delay; verifying trading session dates (I5), pass names
  (`morning`, `midday`, `late`), timezone-aware `through` datetime stamps (I7),
  in-session pass boundaries (`pass_boundary` inside the session), monotonic pass resume
  ordering (I7), and pass cutoff (`now <= cutoff`).
- EOD replay validation, order classification and MTM decisions: replay clock not
  past session open (I7); barring non-equity instruments from bar replay (I5);
  validating one-minute bar series completeness (starts at session open, ends at final
  minute, I5); rehydrating venue working orders against ledger state (I5); classifying
  bracket children into stop (`STOP`) and target (`LIMIT`) lists; classifying terminal
  (`FILLED`, `CANCELLED`, `EXPIRED`, `REJECTED`) and working (`SUBMITTED`, `ACCEPTED`,
  `PARTIALLY_FILLED`, `PENDING_UNKNOWN`) orders; requiring regular-session bars for open
  positions to mark (I5); validating options chain snapshot coverage (I5) and
  requiring a lifecycle pass configuration when holding options (I9).
- Exact portfolio and dividend arithmetic: `_derive_risk_context` equity
  (`cash + sum(q * mark)`), gross position value (`sum(|q| * mark)`), active position
  count, and string-based cash truncation supporting arbitrary-scale decimals and
  unbounded integers; pre-open dividend qualification (`now < session_open`) and
  dividend credit calculation (`sum(per_share) * quantity`). All money arithmetic
  uses checked `PyDec` under D6's approved exception (2026-10-03).
- Official close and settlement verification: official PM settlement identity (I5),
  prevention of look-ahead settlement timestamps (I7), verifying close timestamps
  occur at or after session close (I9), and verifying sink publish capability (I5).
- Intraday configuration and timing decisions (`te_core::runtime::intraday`):
  validating non-empty identity fields (`job_name`, `account_id`, `underlying`,
  `eod_job_name`), positive rates and TTLs; enforcing wall-clock ordering
  (`entry_end <= flat_at`) and early-close parameters (`0 < flat_before_close <= entry_before_close`);
  deadlines calculation determining `flat = min(flat_at, close - flat_before_close)`
  and `entry = min(entry_end, close - entry_before_close, flat)` while returning the
  governing branch index; tick state evaluation (`flat_due`, `can_enter`).
- Intraday snapshot freshness and position reconciliation: chain snapshot identity (I5),
  underlying quote timestamp presence and max-age check (`underlying_age <= max_quote_age_seconds`,
  I5), held leg presence and freshness (I5); held open structure and flatten classification;
  generating flatten command IDs (`intraday:flat:...`); close sequence numbering (`:close:N`);
  restart action classification (`ignore`, `cancel`, `submit`); venue vs ledger
  position drift detection using `PyDec::eq_num` (I11); heartbeat freshness verification
  (`heartbeat_ttl_seconds`, C4); and previous session completed EOD marker check (I3).
- Options routing decisions (`te_core::runtime::routing`): routing phase selection
  (`snapshot` vs `close`); intent target account verification (I8); options risk engine
  requirement for entry intents (I5); risk verdict evaluation and resize detection via
  `PyDec::eq_num`; idempotency check (`taken`) verifying whether an entry or close action
  was claimed in the ledger; tally accumulation (`RoutingTally + RoutingTally`) using
  arbitrary-precision integer arithmetic (`BigInt`); and runaway strategy loop detection
  (`rounds`).

Python retains host observations, effects, sequencing, and boundary plumbing:

- Reading clocks and provider I/O: reading injected clocks (`Clock.now_utc()`,
  `ReplayClock`, `SettableClock`); timestamps cross the boundary as integer
  microseconds since Unix epoch UTC (`micros()`) or ISO strings. No clock is read
  inside `te_core` (I7). Calling data providers (`MarketData.bars()`, `chain_snapshots()`,
  `settlements`, `lifecycle`, `dividends`), and reading heartbeat JSON files from disk.
- Querying state and ledger: reading cached accounts, querying ledger states
  (`ledger.state()`), scanning events (`ledger.events()`), checking command existence
  (`ledger.has_command()`), and inspecting venue orders, positions and fills.
- Executing effects and mutations: appending events to the ledger (`EodRun`, `Mark`,
  `CashFlow`, `VenueReconcile`); submitting, cancelling, and reconciling orders on
  brokers; publishing outbox messages to external sinks; and writing heartbeat files
  atomically to disk.
- Orchestration and loop sequencing: the bar-by-bar EOD replay loop advancing the
  clock and reconciling after every bar; the intraday tick-by-tick loop stepping from
  open to close and sleeping `tick_seconds`; in-session pass sequencing (`run_morning`,
  `run_pass`, after-close run); alert wrapping (`IntradayServiceAlert`) and process
  error handling.
- Deleted from Python in the same commit as the shims (D3): 478 lines deleted, 289
  added (net -189) across `src/trade_engine/eod/runner.py` (-291/+147),
  `src/trade_engine/eod/options_routing.py` (-53/+42),
  `src/trade_engine/intraday/service.py` (-134/+81), and the new transport shim
  `src/trade_engine/eod/_runtime.py` (+19).

The extension is mandatory (D5). Decisions cross pyo3 through a single function
`trade_engine_rs.runtime_decide`, called by `eod._runtime.decide` / `flag`. Refusals
cross as `ValueError((kind, message))` and are mapped by `trade_engine.sim._rs.call`
to the exact Python exception types registered via `register()`: `runtime_eod`
(`EodRunnerError`), `runtime_incomplete` (`SessionIncompleteError`), `runtime_replay`
(`ReplayDataError`), `runtime_intraday` (`IntradayServiceError`), and `runtime_stale`
(`StaleDataError`).

Measured parity (8 tests in `tests/test_p4a_parity.py`, 102,221 lockstep comparisons):

| Campaign | Checked steps/cases |
|---|---:|
| Dense runtime campaign (45k risk contexts, 20k deadlines, 25k snapshot views, 32 config, 10k tallies, 4 unbounded risk, 1 unbounded tally, 32 taken) | 100,069 |
| Refusal kinds and succeeding counterparts (all 5 refusal families exercised) | 10 |
| Pre-open dividend timing boundary (-1us, 0, +1us) and exact cash flow amount | 3 |
| EOD options lockstep sessions (3 sessions x 4 passes: morning, midday, late, full) | 12 |
| Equity replay idempotency across sessions | 2 |
| Intraday tick walks and restarts (4 failure modes x 31 minutes with restart at minute 20) | 124 |
| Ordered intraday configuration validation (8 refusals + 1 success) | 9 |
| Full intraday sessions with resume (6 calendar sessions incl. early closes: 12 outer steps + 1,980 tick frames) | 1,992 |
| **Total lockstep parity comparisons** | **102,221** |

Every step compares return values, exact refusal type names and error messages, and
ledger events; full sessions assert identical canonical folded ledger state at the close.
Gates reported: `python tools/ci_local.py` exit 0 with all **1,929 Python tests passed**
(1,913 pre-existing unchanged + 16 parity tests); **88 Rust tests passed** (89 passed
when re-verified with `cargo test -p te_core -q`).

`tools/mutate_p4a.py` runs against the parity suite, building each mutant, running
pytest under `-B`, restoring source bytes in `finally`, and unconditionally checking
restored green baseline parity. All **9/9** compiling hand mutants are killed:

| Mutant | File | Mutated behavior | Killing parity test |
|---|---|---|---|
| `pass-cutoff-exclusive` | `eod.rs` | Pass cutoff `<=` changed to `<` | `test_eod_options_lockstep_sessions_and_passes` |
| `stale-boundary-exclusive` | `intraday.rs` | Max quote age `>` changed to `>=` | `test_dense_runtime_campaign` (fresh view) |
| `preopen-inclusive` | `eod.rs` | Pre-open dividend boundary `<` changed to `<=` | `test_preopen_dividend_boundary_and_amount` |
| `dividend-double-amount` | `eod.rs` | Dividend credit amount multiplied by 2 | `test_preopen_dividend_boundary_and_amount` |
| `early-close-ignored` | `intraday.rs` | Early-close delay ignored in flat deadline | `test_dense_runtime_campaign` (deadlines) |
| `heartbeat-exclusive` | `intraday.rs` | Heartbeat freshness age `<=` changed to `<` | `test_runtime_refusal_kinds_and_counterparts` (heartbeat) |
| `tally-subtraction` | `routing.rs` | `RoutingTally` addition replaced with subtraction | `test_dense_runtime_campaign` (tally) |
| `taken-forgets-entry-risk` | `routing.rs` | `taken` ignores entry risk command check | `test_dense_runtime_campaign` (taken) |
| `previous-session-disabled` | `eod.rs` | Previous session completion check disabled | `test_runtime_refusal_kinds_and_counterparts` (previous) |

Deviations and boundary choices:

- Money and decimal arithmetic use `PyDec` (from `te_core::ledger::pydec::PyDec`)
  under D6's exception, approved by the owner on 2026-10-03, crossing the boundary as
  strings. This preserves Python decimal scale, precision and exact formatting for
  portfolio equity, gross value, and dividend credits until P7.
- Cash truncation uses arbitrary-precision integers (`BigUint`), preserving exact
  integer conversion for numbers with huge exponents.
- `RoutingTally` arithmetic uses `BigInt`, matching Python's unbounded integer arithmetic.
- Microsecond integer timestamps (`micros`): datetimes cross into Rust as integer
  microseconds since Unix epoch UTC. Timezone-aware datetimes are strictly enforced (I7).
- Plain-value pyo3 transport: no domain structs cross pyo3; data crosses as flat vectors
  of strings, 64-bit integers, booleans, and floats. Domain object extraction and assembly
  remain entirely in Python shims.
- Five runtime refusal error kinds (`runtime_eod`, `runtime_incomplete`, `runtime_replay`,
  `runtime_intraday`, `runtime_stale`) registered through `trade_engine.sim._rs` preserve
  exact Python exception hierarchy and message strings.

Timing: reported stable timing at **1.073x** the pre-port baseline.

### P4b verification and boundary

The standalone oracle commit is `a8769dd70ceb394c20692befce366377da6b5fd1`:
only the three pre-port modules in `tests/frozen_p4b/` were committed, with
`after_close` importing its frozen `sources`. Those files are immutable thereafter.
No existing test, protected Python directory, ledger lock or user ledger was edited.

Rust owns the new decisions in `te_core::runtime::{lifecycle,journal}`:

- Session/close guards, account and held-option ordering, position side/absolute
  quantity and expiry classification, overdue refusal, settlement identity and
  chronology, expiry kind/reason, dividend eligibility/source/chronology, summed
  dividends, quote requirements/look-ahead, early-assignment reason and event
  fields/command-id suffix.
- Settlement/dividend positive finite Decimal, required provenance and timezone
  validation; corporate-action amount usability. The existing P1 option intrinsic,
  exercise-style, settlement-instant and dividend-exercise rules are **reused**,
  not copied or expanded. This preserves P1's numeric-range refusals, including
  `1E+50`, rather than silently accepting values P1 refused.
- Required journal fields and mapping shape; timestamp seq residue; tag-source
  precedence and append rule; account guard, derivative/multiplier action, HTTP
  insert acknowledgement, trade selection, numeric read-back tolerances and
  annotation fields. `crates/te_py/src/lifecycle_sinks.rs` converts Python carriers,
  performs rich datetime comparisons and uses the existing stdlib ISO/JSON codecs
  and `str`/`int`/`float` conversions. These preserve Unicode, unbounded event seq,
  accepted ISO syntax, decimal spellings and conversion exception text; there is
  no second Python implementation of these mappings.

The **one new Python door** is `trade_engine._lifecycle_runtime`, outside the
protected `eod/` and `intraday/` directories. It unconditionally imports the mandatory
extension through the established refusal bridge (D5). New P4b arithmetic uses
string-carried `ledger::pydec::PyDec` (D6's exception); only the journal's existing
HTTP float fields/tolerances use floats. No Rust clock read was introduced.

Python retains the clock, ordered ledger reads and one final append, settlement/
dividend/quote callbacks, carrier construction, source/store adapters and HTTP,
auth/error handling/retries. A whole lifecycle plan is still decided before any
write, including a later-account refusal. Source max-age calculation belongs to
the existing chain store/market-data provider; P4b passes the identical age and
instant and preserves its stale refusal rather than moving that adapter.

Measured parity: **8 tests, 12,711 compared steps/cases**:

| Generator | Comparisons |
|---|---:|
| 100 seeded lifecycle worlds: first pass, recovery counterpart and replay | 300 |
| Source value grid and explicit refusal counterparts | 4,422 |
| Journal timestamp/ISO/tag/payload grid and refusal counterparts | 4,832 |
| 26 journal HTTP modes, 8 seeds, refusal/success counterparts | 416 |
| Expiry/dividend threshold helper grid | 2,400 |
| Journal read-back tolerances and annotation/tag combinations | 89 |
| Corporate-action and snapshot source adapters | 19 |
| Inherited option-money, naive-clock and time boundaries | 233 |

Every lifecycle step compares return or exception **type name and exact message**,
the complete ledger rows and ordered ledger/source observations; each world also
compares folded states. Journal worlds compare every ordered HTTP callback/body.
Decimal comparisons use `str`, including scale. Fifteen lifecycle refusal families
each refuse five times and have five successful counterparts: session, pre-close,
overdue, invalid expiry, missing/wrong/future/pre-instant settlement, missing/
unknown/future dividend, missing quote source and missing/stale/future quote.
The main lifecycle campaign records 50 `LifecycleError` and 25 `StaleDataError`.
Journal delivery records 24 initial successes, 184 initial refusals, and 208
successful counterparts. Source and mapping refusals are separately asserted;
all nine required journal fields have individual missing-field cases.

`tools/mutate_p4b.py` builds a green baseline, rebuilds each mutant, runs `python -B`,
accepts **only assertion failures** as kills, restores original source bytes in
`finally`, and unconditionally restores/rebuilds/checks green at the end.
The final campaign kills **14/14 compiling mutants**:

| Mutant | Killing parity test |
|---|---|
| Before-close pass allowed | lifecycle seeded worlds |
| Settlement identity ignored | lifecycle seeded worlds |
| Pre-instant settlement allowed | lifecycle seeded worlds |
| Exact exercise threshold expires | lifecycle seeded worlds |
| Dividend/extrinsic tie assigns | lifecycle seeded worlds |
| Dividend sum doubled | lifecycle seeded worlds |
| Long call assigned early | lifecycle seeded worlds |
| Early command suffix dropped | lifecycle seeded worlds |
| Zero source price accepted | source value grid |
| Journal seq residue dropped | journal mapping grid |
| Skipped insert accepted | journal HTTP worlds |
| Cross-account delivery accepted | journal HTTP worlds |
| List tags ignored | journal mapping grid |
| Quantity tolerance inclusive | journal read-back boundaries |

There were **no initial survivors**. The campaign was rerun after preserving the
inherited P1 monetary boundary and naive-datetime comparison error wording; no
mutant was weakened. Build/import/collection failures never count as kills.

Realistic-book timing (`tools/time_p4b.py`), nine independently seeded real SQLite
ledgers, 3 trading accounts plus the cash-only fixture account, 120 option positions
(96 expiring, 24 future short calls),
AAPL/SPXW/SPX, both rights/sides, quantities 1..4: pre-port median **44.1154 ms**,
post-port median **31.2000 ms**, ratio **0.7072**, below the **1.25** limit.
Construction/seeding are outside the timer; source reads, plan, append and fold
are inside. Each sample appends exactly 96 lifecycle events; no live/paper job
or user ledger is used.

Final gates: `cargo test --manifest-path crates/Cargo.toml -p te_core` **91 passed**;
the private Python 3.13 full `tools/ci_local.py` gate is recorded in the final
P4b report. Existing lifecycle/journal tests pass unchanged (**64**), alongside
the eight new parity tests. The extension and package paths were verified inside
this worktree/private `.venv`; all cargo output stays in `crates/target`.

Deviations: **no new accepted business-rule deviation**. ISO/JSON parsing and
dynamic scalar conversion deliberately use Python's stdlib from the Rust binding
to preserve the oracle's accepted syntax and exceptions. P1 option-money boundaries
and the journal's zero-fee replacement/NaN tolerance behavior remain unchanged.
Remaining/out of scope: OMS orchestration, `eod`, `intraday`, server/axum, ledger
lock, `tos_paper`, all process/connection ownership and the final P4 paper-session
flip. Rollback is a checkout of the base branch with a rebuild of its private
extension: **no ledger rewrite or decimal migration**.

### P5 verification and boundary

Commits: 64afa1c (frozen Python oracle); T1-T8 77783c1..d95d629 (transport, normalize, slippage, reconcile, cover, netting, exits, follow decisions in te_core, each in lockstep with the frozen oracle); T9 5d4573a (the broker state machine); T10 76b26c4 (production `tos_paper` runs on `trade_engine_rs`); hand mutants 90874bf, 1c823f5, 7680e21, 0415eb8, 6c68e86; 73213c9 and a7e2f6e (test move and rebase fix, below).
- **T10.** `src/trade_engine/tos_paper/*.py` are thin doors over `trade_engine_rs`. The ported Python logic is deleted (D3), and the public API is unchanged.
  - `tos_paper/_rs.py` imports the extension unconditionally (D5: a missing extension is an error).
  - It maps each Rust error kind to the existing exception (NormalizeError, SlippageError, UnsupportedCapability, NettingError, ExitPlanError, TosPaperBrokerError, VenueUnreadable, OverflowError, InvalidOperation).
  - The UI-automation transport stays Python, behind the host callback.
- **Accepted deviations from the oracle** (T9 and T10; each was grepped against the callers, and none is reachable from them):
  - the preflight reads the clock through the host;
  - naive and aware datetimes are handled the same way;
  - a non-mapping row is refused earlier;
  - MirrorFill and MirrorAck validate their fields in a different order;
  - rows are JSON-native (`default=str`);
  - `plan_exits` asks for prices lazily (only for the legs it prices).

Gates:
- The P5 lockstep parity tests against the frozen oracle, plus the existing `tos_paper` tests, run on the production path.
- Hand mutants `tools/mutate_p5.py`: 40 of 40 KILLED. Each kill is read twice, through the door and through production. One mutant (`cover-kuhn-short-order-reversed`) is door-only, with its reason recorded: which shorts a long covers can't be seen through `uncovered()`.
- On the tree rebased onto c336915: `cargo test --workspace` 189 green, the wasm32 release build green, ci_local 1989 passed.
- Existing tests changed:
  - `tests/p5_broker_host.py` is deleted; the Python host it emulated is gone.
  - Two tests that poked deleted private state (`_queue`, `netting._ticket`) now go through the public path:
    - `test_an_inexpressible_queued_ticket_is_rejected_not_raised`, via `mirror_batch` with a GTD order (its Rust twin is in `broker.rs`);
    - `test_a_ticket_error_refuses_its_orders_not_the_batch`, via fault injection (`object.__setattr__` sets `limit_price=None`).
- a7e2f6e adds the `Instrument::Future` arms in cover, follow and wire that P6b's new variant needed.

Boundary:
- **Not yet done:** a live paperMoney round trip (read, send-cancel) on the PM accounts with this tree; the live plugins still pin the engine at 1d2d029. Bump the pin only after that round trip passes.
- P4c (unpushed when P5 landed) conflicts with this commit only in `crates/te_py/src/lib.rs`'s module register list. Keep both.

### P6b verification and boundary

Plan: `.worktrees/plans/P6B_FUTURES_IN_TE_CORE.md`. Its §0 decisions (2026-10-03) apply:
- the session calendar is CME's published schedule, not a third-party library;
- futures slippage is a whole number of adverse ticks;
- margin is report-only;
- an in-session gap is legal.

Commits:
- **T1, `Instrument::Future` and tick arithmetic** (3723bc8, da81283):
  - prices snap to the contract's tick;
  - P&L uses the point value.
- **T2, `te_core::calendar::globex`** (56e015e):
  - NQ/MNQ/ES/MES sessions run 2006 through 2027, built from `cme_equity_holidays.csv` (CME notices and the trading-hours API via the Wayback Machine, each row sourced).
  - The calendar handles the daily 17:00-18:00 ET halt, the weekend, `early_halt` and `closed` dates, and CME's published reopen times.
  - Instants outside the table refuse (I5).
  - `tests/cme_pmc_oracle.rs` compares every date with `pandas_market_calendars` `CME_Equity`. Each of the 162 disagreements is allow-listed with its CME source. An unlisted difference, or an allow-listed one that has gone away, fails the test.
  - Three 2023 dates with no CME source are deliberately left as normal days (see `globex.rs`).
- **T3, `Book::new_futures`** (1950e59, a0e6538):
  - a bar must lie inside a Globex session and be strictly later than the previous bar; it may jump to any later session;
  - Day orders expire at the session close;
  - stops and market orders slip by whole ticks; limits never slip.
- **T4, te_wasm futures** (3acc171):
  - `SimBook.newFutures(account, slippageTicks)` and `position_pnl`;
  - position rows carry `point_value` and `tick_size`;
  - a mixed-venue book refuses.
- **T5** (3d90054): 200 seeded NQ/MNQ walks (slippage 0-3 ticks), te_core vs the te_wasm API. Every step must agree on the result or the refusal (kind and message). The walks cover:
  - in-session gaps;
  - daily-halt and weekend jumps;
  - three early-halt dates and two closed dates;
  - bad bars inside a halt or on a weekend;
  - Day expiry.

Gates:
- `cargo test --workspace` is green: te_core 116, the CME oracle, te_wasm 7, parity 4.
- The wasm32 release build is green.
- `golden.json` and the P3a equity walks are unchanged.
- The browser side is tvDownloadOHLC f0548ce4:
  - te_wasm drives MNQ/NQ/MES/ES natively on real bar times, and the SPY/bps encoding is deleted;
  - `p6-differential` agrees replay-sim vs te_wasm, including halt, weekend, Day expiry, 1-tick stop, MES and ES;
  - 722/722 pass with the flag OFF, and 722/722 with the flag ON;
  - parity rows 4, 16 and 17 are SAME.

Boundary:
- CL/MCL refuse: there is no energy holiday table yet.
- With the flag ON, a bar outside the venue's scope (an unmodelled root, a sub-minute bar or a non-minute clock, a year outside 2006-2027) runs the pure TypeScript rules.
- The browser book is built frictionless, and replay-sim applies the tick slippage once.
- The web's Day order expires at 16:00 ET, te_core's at the Globex close (17:00 ET). The web check fires first.
- Plan Gate 3 (golden fills recorded on NinjaTrader 8) is not built.

### P6C verification and boundary

Owner decision (2026-10-06): calendars for every root we hold 1m data for (YM, RTY, CL, GC and their micros), not only NQ/ES.

Commits: 2a53413 (energy/metals tables), f0c45f2 (calendar per root, specs), 4e86502 (1m data oracle, mutants), 3b57b14 (review: micro ranges, equity session eras).
- **Tables.** `cme_energy_holidays.csv` (CL, MCL) and `cme_metals_holidays.csv` (GC, MGC): 213 rows each, 2009-2027, built by `tools/build_cme_energy_metals_holidays.py` from the CME captures in `.worktrees/plans/cme_raw`. Provenance, conflicts and the 13 OBSERVED-OVERRIDE rows are in `crates/te_core/data/cme_energy_metals_holidays.md`. 2008 was not built: the sources cover one 2008 holiday of about nine. The equity table is unchanged byte for byte.
- **Calendar per root.** `GlobexCalendar::for_root`; the broker takes the calendar from the order's or bar's instrument, so te_wasm needed no API change (the root comes from the symbol).
- **Ranges (I5).** Equity 2006-2027; YM/MYM from 2008-01-27 (CBOT moved to Globex); RTY/M2K from 2017-07-09 (RTY returned from ICE); CL/MCL and GC/MGC 2009-2027. A micro takes its mini's range, because the web serves mini data under the micro symbol.
- **Equity session eras** (`EQUITY_ERAS`): a 16:15 ET Friday close to 2012-11-16, and a 16:15-16:30 ET weekday halt to 2021-06-25. This rests on CME's 2009-2010 notices and on the bars; the boundaries are OBSERVED. After 2021-06-25 the default rule is unchanged. Energy and metals have no era.
- **Specs** match tv `web/lib/contract-specs.ts`: YM 1.0/5, MYM 1.0/0.5, RTY 0.1/50, M2K 0.1/5, CL 0.01/1000, MCL 0.01/100, GC 0.1/100, MGC 0.1/10. Unknown roots still refuse.

Gates:
- `tests/calendar_data_oracle.rs` checks the 1m store (READ ONLY, SHA-256 per source file in each fixture) against each root's calendar: no bars when closed, after an early halt, inside a halt or on a weekend; every open session has bars. The allow-list has 83 entries (ES 29, NQ 23, YM 14, RTY 5, CL 6, GC 6), each with a reason (store gap, stamp artifact, OBSERVED one-off, unsourced 2023 equity rows).
- `cme_pmc_oracle.rs` is extended to `CMEGlobex_CL` and `CMEGlobex_GC`: the session dates and opens are the same, and 72 close minutes differ. All 72 are allow-listed, and pmc is the wrong side on each.
- Hand mutants `tools/mutate_p6c.py`: 15 of 15 KILLED.
- `cargo test --workspace`: 156 green. The wasm32 release build is green. `golden.json` and the P6b walks are unchanged. ci_local: 1946 passed.
- Existing tests changed: only the `sim/tick.rs` tests that asserted CL/MCL/RTY/YM/GC refuse; they now assert an unknown root refuses.

Boundary:
- The web (`web/lib/orders/cme-calendar.ts`) still mirrors the equity table only, without the eras. Day expiry for a pre-2021 equity date, or for CL/GC, can differ from te_core until the web follow-up lands: per-group generated tables, the 12 venue roots and the per-root ranges.
- Before 2021-06-28, te_core keeps 17:00 ET as the session identity and does not model CME's 16:15 ET trade-date roll (documented in the .md).

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

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
| **P4c Runtime flip** | `server` (axum), the single-instance lock, process ownership; Python callers become clients | ~2.5k | A paper session run side by side with the Python engine produces the same ledger | **in progress (T1,T3,T4,T5,T6)**; scoped checkpoints verified; runtime/session flip remains unverified, evidence below |
| **P5 TOS mirror** | `tos_paper` logic; the UI-automation transport stays Python behind a callback | ~3.4k | mirror tests unchanged; a paper round trip matches | last (most active module) |
| **P6 Browser & retire** | `web/engine`, `replay-sim` → wasm; delete the Python package | ~1.5k | browser replay matches the engine | after P5 |
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

### P4c verification and boundary

**P4C-T1 only**, based on `038045ac9f8e94c01fe9201e335f9404bff70ed7`.
The standalone oracle commit is
`4d8d505f63ea909c19ab1d217f0a6e062a4fca65`: only
`tests/frozen_p4c/lock.py`, copied before production edits. Its Git blob
`47a9deb5c145210fc4e8e8b03fa30a5579bf397e` is identical to the base's production
lock. The oracle remains immutable; no pre-existing test was edited.

Rust now owns parent-directory creation, sidecar open, nonblocking OS exclusion,
PID write/truncate/flush, and the guard's native file lifetime, in the new
`te_host::lock` infrastructure crate using **fd-lock 4.0.4**. The existing ONE
`trade_engine_rs` module registers one small `LedgerLock` binding. `te_core` and
`te_wasm` acquire no OS dependency or clock read. The fd-lock borrowed guard is
transferred to the owned file's close without a self-reference, unsafe code, or
a leaked descriptor; release and process teardown free the lock.

Python's `ledger/lock.py` is now only path/PID conversion, the native handle,
the unchanged `LedgerLockError` and exact refusal message, and context-manager
plumbing. It shrinks **107 -> 42 lines, net -65** (Git diff: +8/-73).
`_lock_file`, `_unlock_file`, msvcrt/fcntl calls and Python file operations are
deleted. The existing store remains unchanged: acquire still precedes every
writable SQLite open; failed opens release the guard. Python still owns SQLite,
server, clocks, plugins and runtime loops. T1 adds **no runtime binary**; D5's
mandatory extension import/build applies without an imaginary binary skip.

Measured Windows/Python 3.13 proof (`tests/test_p4c_lock.py`, **23 tests**):

- **8,000 seeded lockstep API steps**, asserting exact outcomes and held state:
  **5,722 successes / 2,278 refusals**. Exception type names and messages match.
  Only the two distinct synthetic root strings in generated refusal messages
  are normalized; subprocess, path/open errors and all other text are exact.
- **8 owner/contender process walks**: old Python/new Rust in both directions,
  Rust/Rust and old/old, each with graceful release and forced process death.
  All 8 contenders refuse; all 8 post-exit acquisitions succeed.
- **24 simultaneous acquisition races** (8 each old/new, new/old, new/new):
  exactly 24 winners and 24 matching refusals, followed by successful reacquire.
- Same-process contention; relative/dot/dot-dot, hardlink and Windows case
  aliases; stale PID files, PID capture/text, held-state/idempotent acquire and
  release, context exit and garbage-collection close. A native unit test reads
  PID text through the owning handle while held.
- Five malformed/open-path refusals and one missing-directory success, plus
  read-only-file PermissionError, null filename/parent error order, BMP,
  non-BMP and unpaired-surrogate paths. A failed open does not poison the handle.
  Windows held sidecars cannot be deleted or replaced to bypass exclusion.
  The SQLite-open spy proves refusal occurs before any writable DB open.

`tools/mutate_p4c_t1.py` uses the private interpreter, rebuilds every mutant,
runs `python -B`, asserts one raw-source occurrence (preserving CRLF), restores
original bytes in `finally`, and unconditionally rebuilds and checks restored
green. An explicit pytest exception hook recognizes **AssertionError only**;
compiler/import/collection failures never count. Final baseline and restored
runs both pass **23 tests**; **12/12 compiling mutants killed**, no equivalents
or survivors:

| Mutant | Killing test |
|---|---|
| `guard-dropped-before-return` | generated lockstep |
| `pid-not-truncated` | aliases/PID/stale/context |
| `pid-prefix-lost` | aliases/PID/stale/context |
| `held-file-delete-sharing` | held sidecar deletion/replacement |
| `parent-creation-lost` | generated lockstep |
| `truncate-before-lock` | aliases/PID/stale/context |
| `contention-accepted` | generated lockstep |
| `held-always-false` | generated lockstep |
| `release-leaks-descriptor` | generated lockstep |
| `reacquire-not-idempotent` | generated lockstep |
| `pid-capture-ignored` | aliases/PID/stale/context |
| `open-error-filename-lost` | exact invalid-path errors |

Final gates (2026-10-03):

- Pre-port existing locking baseline: **5 passed, 86 deselected**.
- Restored focused lock + unchanged ledger suite: **114 passed**.
- `cargo test --manifest-path crates/Cargo.toml --workspace`: **94 passed**:
  90 core unit tests, 1 host unit test, 3 wasm integration tests; zero failures,
  ignored tests or doctests. te_py and te_wasm unit targets also build.
- Private `python -B tools/ci_local.py --include-uncommitted`: **exit 0**,
  **1,969 Python tests passed in 795.04 s**; I7/version/extension gates green.
- Read-only `tools/ledger_parity.py`: **exit 0**; mirror-PM-B 4,095 events,
  0DTE 3,278, options 436, scan 6,296: **14,105 codec rows, 23 account states
  and 4 folds identical**. All oracle folds succeed. This remains a codec/fold
  gate (refusal kind only), not full runtime/tape certification.

Timing (`tools/time_p4c_t1.py`): startup/open/close is **not a tick hot path**.
Nine independent synthetic populated books per implementation, three accounts
and 300 cash-flow events per book; identical unchanged store/schema/WAL/FULL
lifecycle, 50 opens per sample, seeding/build outside the timer. No recorded
session or trading fixture is claimed. Old/new median open/close:
**2.655864 / 2.926010 ms**, ratio **1.101717** (<1.25); largest sample means
**4.344502 / 4.027056 ms**. Lock-only median **0.576044 / 0.455764 ms**,
ratio **0.791197**; tails **0.878112 / 0.821142 ms**.

Compatibility details and discrepancies found, not silent library departures:

- fd-lock's Windows LockFileEx range (byte zero, length one) actually excludes
  the old msvcrt holder both ways. Native open explicitly shares read/write,
  **not delete**, matching Python's CRT sharing flags. Write access (rather than
  append-only access) is required for locked truncation on Windows.
- The pinned PyO3 0.23 PathBuf extractor panics on non-BMP Windows filenames.
  The binding uses a lossless UTF-16/surrogatepass conversion instead. OS errors
  use Python's Windows/CRT errno and message formatting, preserving exact
  mkdir-vs-open and embedded-null text. No dependency/version/design substitute
  was made. Unix fd-lock/flock paths exist but were **not executed on this
  Windows machine**; do not infer a separate Unix platform certification.
- An unpinned Cargo workspace command initially discovered Python 3.14 and
  refused it. Explicit `PYO3_PYTHON` set to the private 3.13 interpreter makes
  the requested whole-workspace gate pass without changing PyO3 features.
- Initial harness failures (Windows venv launcher PID versus actual child PID,
  reused null-test roots, CRLF anchors and pytest's abbreviated assertion text)
  were corrected. Death tests terminate the reported synthetic child's actual
  PID. No invalid campaign was counted as a completed mutation gate.

Isolation: branch `te/p4c-t1`, worktree
`C:\Users\vinay\trade-engine\.worktrees\p4c-t1`, private `.venv` only. Package
resolution is this worktree's `src`; the actual
`trade_engine_rs.cp313-win_amd64.pyd` is under this private `.venv`.
Cargo target is worktree-local `crates\target`. Full CI removes inherited
`PYTEST_ADDOPTS` and sets TMPDIR/TEMP/TMP to `.ci-local\temp`, so nested pytest
numbered roots coexist. Timing/native scratch files and probe processes are
cleaned by their exact names. No root checkout, client, plan file, scheduled
task, real writer, venue or trading job was changed or started.

Only this documentation evidence/status was edited after the accepted full-tree
gate; production, tests, build manifests and mutation sources remain unchanged.
The documentation-only artifact is checked with `git diff --check`.
Rollback is the retained base release plus its rebuilt private extension after
the current guard closes: mixed-version handoff is tested, stale PID files need
not be deleted, and **no ledger/schema/decimal rewrite** is required. No push,
merge, PR, T0 or later-ticket work is part of this checkpoint.

#### P4C-T3 additive embedded-host packaging

Branch `te/p4c-t3` starts from the **unmerged prerequisite**
`te/p4c-t1` at `7f20078d32eff8598e0a9e9847c62046e58b5fd9`. T1's oracle
`4d8d505f63ea909c19ab1d217f0a6e062a4fca65` and all existing frozen oracles
remain immutable. T3 replaces **no pre-existing Python responsibility**: there
was no engine Rust executable/bootstrap to freeze. A standalone frozen-oracle
commit is therefore **not applicable**, not omitted parity or an invented old
runtime. Existing production Python and pre-existing tests are unchanged.

The new `te_runtime` executable is **only**
`te --proof --config <absolute JSON path>`, with mode `packaging-proof`.
It owns its native process and interpreter startup, registers the **same**
`trade_engine_rs` module before CPython initialization, discovers a configured
fake plugin/factory, reports provenance and exits. It does not acquire a real
ledger, start a server, import SCAN strategies/providers, or run any job.
No clock/store/loop/job/client responsibility moved. Temporary embedding is
approved; new Rust strategy implementations and embedding retirement remain
separate future tickets, not presumed available crates or fixtures.

PyO3 remains pinned to **0.23.5**, CPython **3.13**, without an ABI/version
substitution. `te_py` emits an rlib and cdylib; its default feature set is empty,
`embed` exposes the existing initializer registration, and only maturin enables
`extension-module` for ordinary Python installations. There is still **one**
`#[pymodule]` in this repository. No second dynamic `.pyd` or module alias is
loaded by `te`. `te_core` and `te_wasm` retain their pure boundary.

Private build/proof, from the worktree (never the root/client environment):

```powershell
py -3.13 -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[test]"
$env:PATH = "C:\Users\vinay\.cargo\bin;" + $env:PATH
$env:PYO3_PYTHON = "$PWD\.venv\Scripts\python.exe"
$env:CARGO_TARGET_DIR = "$PWD\crates\target"
Remove-Item Env:PYTEST_ADDOPTS -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force .ci-local\temp | Out-Null
$env:TMPDIR = "$PWD\.ci-local\temp"
$env:TEMP = $env:TMPDIR
$env:TMP = $env:TMPDIR
.venv\Scripts\python.exe -B tools\build_p4c_t3.py
cargo test --manifest-path crates\Cargo.toml --workspace
.venv\Scripts\python.exe -B -m pytest tests\test_p4c_embed.py -q
```

The build tool assembles `crates\target\release\te.exe` and its colocated
`python313.dll`, then installs the ordinary extension into the private venv.
`ci_local.py` now also builds the release bundle and runs the whole Rust
workspace, pinning its resolved Python and worktree-local Cargo target. Missing
binary, DLL or extension fails; none of these gates is skipped.

The JSON configuration has **all fields required**, rejects unknown fields,
relative/missing paths and every mode except `packaging-proof`:

```json
{
  "mode": "packaging-proof",
  "python_home": "C:\\absolute\\Python313",
  "python_dll": "C:\\absolute\\worktree\\crates\\target\\release\\python313.dll",
  "python_executable": "C:\\absolute\\worktree\\.venv\\Scripts\\python.exe",
  "site_packages": "C:\\absolute\\worktree\\.venv\\Lib\\site-packages",
  "engine_source": "C:\\absolute\\worktree\\src",
  "plugin_paths": ["C:\\absolute\\synthetic plugins"],
  "plugin_module": "fake_plugin",
  "plugin_factory": "probe",
  "plugin_config": {"tag": "synthetic packaging only"}
}
```

The venv's `pyvenv.cfg` must identify the configured home and Python 3.13;
site-packages must be that venv's directory. The DLL must be beside `te.exe`,
byte-identical to the configured home's DLL, and actually loaded from that
configured location. Home must contain the standard library/encodings and DLLs;
source must contain `trade_engine`. The plugin is a plain module identifier,
not an engine alias, and its resolved `.py` must lie on the explicit plugin
roots **before its code executes**. This is trusted plugin plumbing, not a
sandbox for malicious Python code.

`PyConfig_InitIsolatedConfig` plus explicit home/executable/search paths disables
environment discovery, user-site/site startup, `.pth` processing, bytecode writes
and signal-handler installation. Search paths are exactly home `Lib`, home
`DLLs`, private site-packages, engine source and explicit plugin roots. An
unrelated cwd or poisoned PYTHONHOME/PYTHONPATH cannot change them. CPython 3.13
with explicit home and disabled site reports `sys.prefix == sys.base_prefix ==
home`, just as the standalone `-I -S` baseline does; the private venv paths and
configured executable are separately verified, not fabricated prefix values.
The host does not launch Python as a worker. Reinitialization in the same
process explicitly refuses; imports retain identity; CPython lives until native
process exit (no unsafe finalize/reinitialize cycle).

Windows loader constraint, measured rather than hidden:
MSVC refuses `/DELAYLOAD:python313.dll` with **LNK1194** because pinned PyO3
imports data (`__imp_PyBaseObject_Type`) as well as functions. The successful
design is the conventional colocated private DLL bundle, not delay loading,
another PyO3 version, a worker, or a fallback. Missing/malformed **configured**
paths produce JSON `RuntimeConfigError` on stderr and exit 2. Missing/corrupt
**native bundle dependencies** fail before Rust entry with Windows loader
statuses **0xC0000135 / 0xC000012F**, respectively; no JSON can be emitted before
entry. Copied synthetic bundles prove both failures with Python absent from
PATH and loader dialogs disabled. A mismatched loaded DLL refuses rather than
using PATH's Python. Plugin/import exceptions retain their exact type/message
and exit 2.

Standalone comparison is the private interpreter, `-I -S -B`, using the same
explicit discovery paths and fake-plugin inputs. It is not an invented frozen
runtime owner or a recorded trading session. The deliberate packaging-only
differences are explicit: a built-in module is not a package, so attempted
`trade_engine_rs.<alias>` raises the built-in's exact "not a package" message;
the ordinary installed extension retains its existing package wrapper.
A non-callable configured factory has the host's explicit TypeError message;
ordinary Python calling an integer gives its usual "'int' object is not
callable". No existing test was edited to accommodate either difference.

Focused proof: **34 packaging tests**, no skips. They exercise the real optimized
executable, not a mocked initializer: one registered built-in entry, one loaded
mandatory module identity, zero `trade_engine_rs` `.pyd` mappings (measured with
Windows `EnumProcessModules`), correct private source/extension paths and CPython
version. Imports and second initialization are repeated in-process. Both native
and ordinary extension bindings acquire/refuse/release a synthetic lock sidecar;
no SQLite book is needed or created. The native process continues to load its
built-in with the installed `.pyd` missing **and** corrupt, while ordinary
extension imports explicitly refuse both cases.

The five applicable plugin refusal pairs compare **exact exception type name and
message** against standalone Python: missing module, syntax error, missing
factory, Unicode/non-BMP ValueError and SystemExit. The non-callable/alias
differences above are pinned explicitly. Configuration tests cover unsupported
mode, absent/corrupt/unknown-field config, relative/missing interpreter/DLL/home/
source/site/plugin paths, wrong private-venv home/version, reserved/invalid module
and factory names, non-object plugin configuration, unconfigured stdlib/source
fallback and refusal before plugin execution. Space/BMP/non-BMP paths cover the
native executable, home, private executable/site-packages, source, config and
plugin roots; unrelated cwd, poisoned environment and a PATH without Python are
also exercised. All fixtures are labelled synthetic; none is a strategy tape.

`tools/mutate_p4c_t3.py` builds both release binary/DLL bundle and installed
extension before a green **34-test** baseline, rebuilds both for every mutant,
runs Python with `-B`, requires exactly one raw-source occurrence, recognizes
only pytest call-stage **AssertionError** kills, and restores original bytes
in `finally`. An unconditional final rebuild and **34-test** green run prove
restoration. The final campaign kills **15/15 compiling mutants** with zero
invalid builds, collection/import-error kills, survivors or equivalents:

| Mutant | Killing packaging test |
|---|---|
| Live mode allowed | mode configuration refusal |
| Home standard-library check lost | wrong home/DLL/venv |
| Bundled DLL path check lost | wrong home/DLL/venv |
| Private site-packages check lost | wrong home/DLL/venv |
| Venv version check lost | wrong venv home/version |
| Reserved mandatory module accepted as plugin | reserved-module configuration refusal |
| Non-object plugin config accepted | plugin-config object refusal |
| Environment isolation lost | private built-in/path/repeat smoke |
| Site startup enabled | private built-in/path/repeat smoke |
| Bytecode writes enabled | private built-in/path/repeat smoke |
| Plugin search path omitted | private built-in/path/repeat smoke |
| Built-in registration omitted | private built-in/path/repeat smoke |
| Built-in version wrong | private built-in/path/repeat smoke |
| Reinitialization guard lost | private built-in/path/repeat smoke |
| Plugin provenance guard lost | unconfigured plugin refused before execution |

An earlier 14-mutant run also killed every mutant; the final run adds the
wrong-version initializer and reruns all 15 after the final provenance/lock/
missing-and-corrupt-extension checks. No mutant was weakened. Baseline/restored
focused timings were **4.10 / 4.22 seconds** (not performance gates).

Startup timing (`tools/time_p4c_t3.py`): **nine independently configured synthetic
plugin roots**, two untimed warmups and five measured fresh processes per path,
alternating standalone/native order. Both paths include interpreter startup,
explicit config/path setup, mandatory module import, fake-plugin discovery/
invocation and provenance serialization; construction/build are excluded.
Standalone uses its real private venv launcher, `-I -S -B`, and the ordinary
extension. Native includes its additional preflight checks. Median sample
medians: **48.262400 ms Python / 42.663700 ms native**, ratio **0.883995**;
largest sample medians **50.925500 / 45.597300 ms**. This is a fair bootstrap
comparison, **not a tick/strategy/runtime hot-path or recorded-session gate**.

Final Windows gates:

- Pre-edit private CI baseline: **1,969 Python tests passed in 687.30 s**,
  exit 0, with the original T1 source/extension; no pre-existing test edited.
- Whole Rust workspace: **94 passed** (90 core, 1 host, 3 wasm integration;
  te_py and runtime targets link/build; zero failures/ignored tests).
- Final private `python -B tools\ci_local.py --include-uncommitted`:
  **exit 0, 2,003 Python tests passed in 585.13 s** (1,969 unchanged + 34 new);
  invariant/version/whole-workspace/release-bundle/normal-extension gates green.
  The inherited `PYTEST_ADDOPTS` was removed; TMPDIR/TEMP/TMP used the private
  `.ci-local\temp` parent so nested numbered pytest roots coexist.
- Explicit feature-unification gate:
  `cargo test --manifest-path crates\Cargo.toml --workspace
  --features te_py/extension-module`: **94 passed**. This actually unifies
  extension and embed features; it is not a `te_core` substitute or a skip.
- Normal private extension install/import and optimized native executable/
  built-in/fake-plugin smoke coexist; every mutation rebuilds both artifacts.
- Authorized read-only `tools/ledger_parity.py`: **exit 0**, mirror-PM-B 4,095,
  0DTE 3,278, options 436, scan 6,296 events: **14,105 codec rows, 23 account
  states and 4 folds identical**. This remains the existing codec/fold gate
  (refusal kind only), not new runtime/session certification.
- Toolchain: **rustc 1.98.1**, **cargo 1.98.1**, **PyO3 0.23.5**,
  private **CPython 3.13.15, AMD64 / MSC 1944**.

Tested provenance:

- Worktree: `C:\Users\vinay\trade-engine\.worktrees\p4c-t3`.
- Interpreter: this worktree's `.venv\Scripts\python.exe`; home is
  `C:\Users\vinay\AppData\Local\Programs\Python\Python313`.
- Engine import: this worktree's `src\trade_engine\__init__.py`.
- Normal extension: this worktree's
  `.venv\Lib\site-packages\trade_engine_rs\trade_engine_rs.cp313-win_amd64.pyd`.
- Native entry: this worktree's `crates\target\release\te.exe`; its report
  identifies the real executable, the configured/loaded private DLL and exactly
  one `trade_engine_rs` built-in entry, with no module file or dynamic alias.

Final tested artifact SHA-256 (build outputs remain ignored, never committed):

| Artifact | SHA-256 |
|---|---|
| `te.exe` | `6620134ce8756c82708d3873480d945792713be584f609e7eb5f2edf4dd5e301` |
| Bundled `python313.dll` | `e820bf024efd2b56bb2b82791e6b6ddc7303f070f8e72cba7637482a8a906238` |
| Private normal `.pyd` | `be05aeb45d1ae872b789aa997e0fa39fa75de4c87b0fc2201e0b8b59db9d40df` |

Changed-path inventory against T1 (Git additions/deletions; no Python logic
deleted because no existing responsibility moved):

| Path | Added | Deleted | Net |
|---|---:|---:|---:|
| `README.md` | 11 | 0 | +11 |
| `crates/Cargo.lock` | 11 | 0 | +11 |
| `crates/Cargo.toml` | 1 | 1 | 0 |
| `crates/te_py/Cargo.toml` | 7 | 3 | +4 |
| `crates/te_py/pyproject.toml` | 1 | 0 | +1 |
| `crates/te_py/src/lib.rs` | 10 | 0 | +10 |
| `crates/te_runtime/Cargo.toml` | 19 | 0 | +19 |
| `crates/te_runtime/build.rs` | 12 | 0 | +12 |
| `crates/te_runtime/src/config.rs` | 156 | 0 | +156 |
| `crates/te_runtime/src/main.rs` | 39 | 0 | +39 |
| `crates/te_runtime/src/proof.py` | 53 | 0 | +53 |
| `crates/te_runtime/src/python.rs` | 137 | 0 | +137 |
| `docs/RUST_PORT.md` | 264 | 4 | +260 |
| `tests/test_p4c_embed.py` | 400 | 0 | +400 |
| `tools/build_p4c_t3.py` | 41 | 0 | +41 |
| `tools/ci_local.py` | 20 | 3 | +17 |
| `tools/mutate_p4c_t3.py` | 135 | 0 | +135 |
| `tools/time_p4c_t3.py` | 73 | 0 | +73 |
| **Total (18 paths)** | **1,390** | **11** | **+1,379** |

After the accepted full-tree gate, only this documentation evidence was updated;
`git diff --check` verifies it. Production/test/build/mutation bytes stayed
unchanged. Synthetic scratch was cleaned; private venv, release artifacts and
ignored gate logs remain worktree-local. Root checkout, client, plan files,
scheduled tasks, real writers and trading jobs were never changed or started.

Rollback: retain T1's release/private extension and stop using the opt-in proof
binary. No task selector, writer handoff, ledger/schema/Decimal rewrite, live
rollout, push, merge or PR exists in T3. Windows x86-64 alone is certified here;
Unix/macOS embedding and deployment, real plugin dependency packaging,
recorded-session parity and all later P4c tickets remain unverified.

#### P4C-T4 native event-store checkpoint

Dependency: this worktree/branch `te/p4c-t4` was created from **unmerged**
`te/p4c-t3` commit `dc1bed0581782b6cb2e82f6390007b025cfb4bec`, including T1
`7f20078d`. The oracle-only commit, made before production edits, is
`c27a4fdcc9b4203bdbb00af51d2f2348aca29d31`: current store/reader frozen under
`tests/frozen_p4c`, with only frozen lock/mutual imports redirected. T1's
immutable lock blob remains `47a9deb5c145210fc4e8e8b03fa30a5579bf397e`.
All frozen files remain unchanged; parent-owned plans were not edited.

**Sole owner-approved existing-test change:** the first affected run stopped
at **244 passed / 1 failed** because
`tests/test_p4c_lock.py::test_refusal_precedes_any_writable_db_open` required
Python `sqlite3.connect` to run after releasing the guard. That expectation
conflicted with native SQL ownership. After explicit approval, only this
function changed: its held-lock refusal/no-created-database/no-held-guard
assertions remain; after release, a directory at the synthetic database path
causes genuine SQLite-open failure. Frozen and native owners both refuse with
`OperationalError("unable to open database file")` and leave no guard held.
Frozen/native locks then reacquire the same sidecar, and removing the directory
allows a real native open/close. There is no fake Python writer or production
injection seam. The compiling **open-before-guard mutant is killed by this
function alone**. A source comparison proves every other existing test and
the rest of this file unchanged.

Final measured gates:

- Pre-edit unchanged relevant store/P2b/OMS/server baseline:
  **131 passed in 49.43 s**.
- Affected store/outbox/P2b/OMS/server/T1 gates plus all existing T3
  packaging tests: **281 passed, 12 warnings in 89.25 s**.
- New T4 coverage: **35 tests**; mutation baseline and unconditional
  restored-tree gate each include these and the approved T1 function
  (**36 passed**).
- Exact `cargo test --manifest-path crates/Cargo.toml --workspace`:
  **97 passed** (90 core, 4 host, 3 wasm integration), zero failures/ignored.
  The explicit `--features te_py/extension-module` workspace gate also
  passes **97**, retaining embed/extension feature-unification proof.
- Private `python -B tools/ci_local.py --include-uncommitted`: **exit 0**;
  mandatory native release/private-DLL and force-reinstalled extension builds
  remain intact, Rust/invariant/version gates pass, full Python suite
  **2,038 passed, 12 warnings in 901.84 s**. No mandatory artifact was skipped.
- Dense frozen/native tests compare every stored event/outbox/meta row,
  encoded event, return/refusal type/message and account state at each prefix:
  **100 seeded walks, 1,700 prefixes, 1,544 successes / 156 refusals**.
  Separate walks cover command replay, malformed SQLite field types and
  decode/fold error ordering, snapshots/seed/base_seq, account-first ordering,
  commit/KeyboardInterrupt/post-commit failures, batch/outbox rollback,
  reader cursor failure/recovery, listener commit-only/replay behavior,
  retained SQL handles after close, read-only refresh/reopen and three
  coordinated concurrent readers. A final counter-only rerun passes.
- SQL compatibility tests exercise the same native writable connection's
  actual transactions, cursor iteration/fetches, named/positional parameters,
  rows/factories, scripts and exact refusals. Twelve warnings are the frozen
  CPython 3.13 deprecation of named placeholders with sequence bindings.
- The rebuilt release `te.exe` runs a synthetic plugin that appends/replays an
  event with atomic outbox rows, reads meta/native folded state and opens a
  native read-only reader while Python `sqlite3.connect` is forbidden.
  The report says `trade_engine_rs` is **built-in**, with **zero loaded
  trade_engine_rs .pyd modules**. Final private source/extension provenance
  checks pass after CI rebuilt both artifacts.
- Read-only `tools/ledger_parity.py`: **exit 0**, four real ledgers,
  **14,105/14,105 event codecs**, **23/23 account states**, four full folds
  identical, no mismatch/refusal. Both SQL readers are read-only; this tool's
  historical refusal comparison is kind-only, whereas synthetic T4 walks
  compare exact type/message. No real ledger writer was opened.

T4 puts SQLite/schema/open/read/event transaction ownership in
`te_host` (bundled rusqlite 0.37), registers store/connection/cursor/row handles
through the existing ONE `trade_engine_rs`, and reuses the existing codec,
PyDec and cached Rust fold handle; hot OMS calls do not serialize the whole
account. `te_core`/`te_wasm` gain no effects.
Python **outbox/meta business sequencing remains T5's existing sole logic**
over the same native writable connection; it has not been ported. The
`_commit` seam and `.conn` transaction surface are real native operations.
No schema/decimal/fingerprint migration or runtime/strategy port was attempted.
SQL compatibility covers the methods actually used here, not arbitrary SQLite
UDFs or the entire CPython connection API.

`tools/mutate_p4c_t4.py`: **15/15 compiling assertion kills**, zero survivors
or invalid runs. Each anchor occurs exactly once; each mutant rebuilds the
release binary and private extension, then runs Python `-B`. Compile/import/
runtime errors do not count as kills. Original bytes are restored in `finally`;
the unconditional final artifact rebuild and green gate pass.

| Native-store mutant | Compile + assertion kill |
|---|---|
| `open-before-guard` | yes; approved T1 function alone |
| `wal-disabled` | yes |
| `durability-weakened` | yes |
| `foreign-keys-disabled` | yes |
| `command-replay-lost` | yes |
| `accounts-alphabetized` | yes |
| `fold-applied-twice` | yes |
| `atomic-outbox-lost` | yes |
| `batch-commits-per-event` | yes |
| `failure-cache-retained` | yes |
| `baseexception-rollback-changed` | yes |
| `reader-refresh-lost` | yes |
| `snapshot-cutoff-exclusive` | yes |
| `timestamp-precision-lost` | yes |
| `sequence-validation-lost` | yes |

Nine paired populated-book samples: 1,720 synthetic seed events, three
accounts with 120 positions/orders each, 60 hot appends including state/handle
reads, and 15 open/closes per sample; seed work is outside timers and owner
order alternates. Final states/counts match; final untouched-code confirmation:

| Operation | Frozen/native median ms | Native/frozen | Frozen/native maximum ms |
|---|---:|---:|---:|
| 60 append/fold/handle calls | 40.3763 / 48.4250 | **1.199342x <= 1.25x** | 70.3746 / 63.3954 |
| Full fold | 168.7079 / 164.2958 | 0.973848x | 230.9367 / 220.4936 |
| 15 open/closes | 38.6947 / 32.6412 | 0.843557x | 62.1902 / 43.1432 |

**Timing variability disclosed, not silently waived:** an initial run
overlapping Cargo compilation failed at 1.353161x; a serial rerun passed
at 1.060528x (31.8036 / 33.7286 ms). A post-CI serial run then failed at
1.289555x (64.2680 / 82.8771 ms). A read-only five-second process sample
observed 21.375 CPU-seconds of other work on this shared 24-processor machine;
it does not prove the failure's cause. Exactly one unchanged-harness
confirmation passed as tabulated above. No source, threshold, fixture or
assertion was weakened between measurements; performance under arbitrary
shared-machine load is not certified.

Isolation: `C:\Users\vinay\trade-engine\.worktrees\p4c-t4`; private
`.venv\Scripts\python.exe` **CPython 3.13.15**, **PyO3 0.23.5**,
source `src\trade_engine`, native extension
`.venv\Lib\site-packages\trade_engine_rs\trade_engine_rs.cp313-win_amd64.pyd`;
explicit `PYO3_PYTHON` and worktree-only `crates\target`.
Authored logs/synthetic fixtures use `.ci-local\temp`; inherited
`PYTEST_ADDOPTS` was removed and TMPDIR/TEMP/TMP point there. No root/client
environment, scheduled/live job or real ledger writer was touched. Windows
x86-64 alone was exercised; Unix/macOS, deployment and recorded/live-session
runtime equivalence remain unverified.

Final artifact SHA256:

- `crates/target/release/te.exe`:
  `ab2b42c6430813ba43200521cb413412e9ede1f03a3e74a7af899d648cb966fd`.
- `crates/target/release/python313.dll`:
  `e820bf024efd2b56bb2b82791e6b6ddc7303f070f8e72cba7637482a8a906238`.
- Private `trade_engine_rs.cp313-win_amd64.pyd`:
  `a346617bb213635e1127805f0e9369be3029e4c9187afa5891665a0836e16b84`.

The 15 scoped port/evidence paths are:
`crates/Cargo.lock`, `crates/te_host/Cargo.toml`,
`crates/te_host/src/{lib,store}.rs`, `crates/te_py/src/{lib,lock,store}.rs`,
`src/trade_engine/ledger/{store,reader}.py`, `tests/p4c_store_fixture.py`,
`tests/test_p4c_store.py`, the sole approved `tests/test_p4c_lock.py` function,
`tools/{mutate,time}_p4c_t4.py`,
`docs/RUST_PORT.md`. Production Python store/reader changes are **net -145
lines** (+72/-217); new tests/tools are additive. The final documentation-only
evidence replacement follows accepted full CI; executable/test sources and
native source hashes are unchanged afterward. Rollback is reverting the T4
port commit and rebuilding private T3 artifacts (the oracle checkpoint remains
available); no ledger rewrite is required.
Environment deviation: the initial private dependency installation inherited
Windows' default TEMP before explicit worktree TMPDIR/TEMP/TMP pinning;
subsequent builds/tests and all authored scratch/log files were worktree-local.
No push, merge, PR or later ticket was started.

### Costs of phasing (accepted)

- Until P4 Python calls Rust across pyo3 with plain values or JSON; most of that
  marshalling is thrown away at the flip.
- P1 functions that take domain objects (risk rules) receive them as JSON until
  P2 makes the types native.
- P4 is the one large switch (who owns the process); the side-by-side paper
  session exists to make it safe.

#### P4C-T5 native outbox/meta checkpoint

Branch `te/p4c-t5` starts from **unmerged T4** `92b8121`, not the root checkout
or the concurrent P5/P6 branches. Its private `.venv` uses CPython 3.13.15;
both the engine import and extension resolve inside this worktree. No root/client
environment, job, task, listener, production writer or deployment was changed.

The separate oracle commit is `f9dee71`: `tests/frozen_p4c/t5_store.py` is a
verbatim copy of the pre-T5 facade. No import rewrite was needed: its dependencies
are unchanged Python carriers/codecs and the inherited T4 event owner. Its
outbox/meta SQL and delivery sequencing stay frozen; all earlier oracles and
existing test files are unchanged.

Ownership:

- `te_host::outbox` owns event-timestamp lookup, insertion/defaults/constraints,
  FIFO filtering, delivery/failure SQL and transactions, meta upsert/read, and
  drain sequencing. These use T4's **same connection**, never another writer.
- `te_py::outbox` converts bindings, validates inputs in oracle order, preserves
  exact exception classes/messages/causes, invokes sink/clock effects, and builds
  the unchanged `OutboxItem`/`DrainResult` carriers. JSON/ISO/`int`/`str` calls use
  Python's existing codecs, including Unicode, non-UTC aware timestamps,
  microseconds, arbitrary schema-version integers and malformed-row refusals.
- Python's replaced SQL, transaction control and FIFO loop are deleted in the
  port commit. Its remaining outbox/meta methods and `_outbox_items` are thin
  calls. `_insert_outbox`, `_commit`, `_rollback` and delivery callbacks remain
  real reentrant seams, not fake connections or fallback implementations.
- Meta writes preserve autocommit and any caller-owned transaction; no new
  schema or admission records were added. Snapshot ownership remains T4's native
  fold. Money stays checked `PyDec`; no clock read was added to Rust.

Proofs:

- Unchanged pre-port outbox/ledger baseline: **103 passed**.
- New parity suite: **94 tests**, including embedded-binary provenance.
  Forty seeded worlds x 24 commands x 11 compared prefixes = **10,560** seeded
  comparisons: **9,600 successes / 960 exact duplicate refusals**. Every prefix
  compares values (Decimal strings), refusals (type/message/cause), event codec
  bytes, all outbox/meta rows, transaction state and full folded state.
- Focused cases additionally cover validation/unopened precedence, default
  event timestamps, malformed rows, unsupported bindings, SQL constraints,
  commit failure and BaseException rollback behavior, atomic event/outbox
  failure/replay, retry, clock/truthiness/publisher exceptions, callback order,
  nested reads/enqueue, uncommitted metadata and reopen. An asserted eight-family
  tally gives destination, unknown-event, created/delivered-timezone, JSON,
  duplicate, binding and constraint refusals **one success and one refusal each**.
- Embedded release test forbids Python `sqlite3.connect`, runs append/enqueue,
  failure/retry delivery, meta/schema and concurrent read-only access, and
  requires one built-in module and **zero loaded extension copies**.
- Workspace tests: **100 passed** (90 core, 7 host, 3 wasm); explicit
  `--workspace --features te_py/extension-module` gate: **100 passed**.
- Final `.venv\Scripts\python.exe -B tools\ci_local.py --include-uncommitted`:
  **exit 0**, **2,132 passed / 12 warnings** (885.01 seconds for pytest).
  It rebuilt the release executable and extension with the private interpreter.
  The warnings are inherited frozen CPython 3.13 named-binding deprecations.
- Authorized read-only `tools\ledger_parity.py`: **exit 0**, **14,105/14,105**
  event codecs, **23/23** account states and **four** full folds identical.
  This is codec/fold evidence, not delivery or runtime-session certification.

Measured release artifact SHA256 (full CI's artifacts):

| Artifact | SHA256 |
|---|---|
| `crates/target/release/te.exe` | `39ccfc54c9aac41bc851ae131acb125f8460a17b82e7ea455fafa8eac4b75c41` |
| `crates/target/release/python313.dll` | `e820bf024efd2b56bb2b82791e6b6ddc7303f070f8e72cba7637482a8a906238` |
| private installed `trade_engine_rs.cp313-win_amd64.pyd` | `5704a48fc9fd48add1257f4df965deccd0b0597284fd32bdff58404552d24d49` |

Oracle Git object `a114bf2d0cb6a32c0012040e7ca2e516894f6721` matches the
pre-T5 production file exactly. The final docs-only evidence update followed CI;
no production source or test changed after the final full gate.

Mutation campaign (`tools/mutate_p4c_t5.py`): **15/15 compiled and killed**, no
survivor or invalid run. Each anchor matched once, each mutant rebuilt both
artifacts, and each kill was a test-body AssertionError, not build/import failure.
Original bytes were restored in `finally`; the unconditional rebuild and final
selected gate passed. Baseline was **52 passed / 39 deselected**; three additive
proofs were written during the campaign, so restoration was **55 passed / 39
deselected**. No test was weakened.

| Mutant | Kill |
|---|---|
| FIFO reversed | seeded prefix row/value parity |
| failed filter lost | seeded pending parity |
| attempts not incremented | seeded raw row parity |
| failure error lost | seeded raw row parity |
| delivery timestamp lost | successful drain row parity |
| duplicate ignored | seeded exact refusal parity |
| meta update lost | seeded meta parity |
| rollback lost | seeded failed-enqueue transaction parity |
| BaseException rolled back | KeyboardInterrupt commit failure |
| FIFO failure skipped | first-unconfirmed delivery walk |
| destination strip lost | seeded row/value parity |
| JSON sort lost | seeded raw payload parity |
| timestamp precision lost | seeded timestamp parity |
| constraint wrapper lost | seeded refusal type/message/cause |
| BaseException swallowed | interrupted publisher walk |

Timing (`tools/time_p4c_t5.py`): nine independently populated synthetic paired
books, alternating old/new order, each with **1,720 seed events** (three accounts,
120 positions/orders per account) and **120 hot items**. Build/setup are excluded;
SQL commits, serialization, carrier construction, sink/clock effects and nested
state reads are included. Raw samples and maxima are printed by the enforcing
tool; every measured median must be <= **1.25x**.

| Path | Frozen median ms | Native median ms | Ratio | Frozen/native max ms |
|---|---:|---:|---:|---:|
| enqueue + meta upsert/read | 115.0208 | 117.0506 | 1.017647x | 127.2923 / 142.9903 |
| pending row conversion | 0.7519 | 0.5068 | 0.674026x | 1.5793 / 0.9963 |
| FIFO drain | 53.2965 | 51.9209 | 0.974190x | 56.5343 / 73.5312 |

The **first implementation failed** the pending-row threshold at **1.306361x**
(enqueue/meta 0.952403x, drain 1.033837x). The binding then eliminated repeated
per-row codec/import lookups and an intermediate Python dictionary, using one
codec adapter per read and direct carrier construction. The same fixture and
threshold passed afterward; no timing assertion was relaxed. These are
shared-machine measurements, not tail-latency certification or a resolution of
T4's previously disclosed unstable hot-path performance.

Scope/deviations: only T5 was implemented. No existing test changed, no data
migration, no new dependency, no runtime loop/HTTP/client/factory/strategy port.
The retained `_commit`/`_rollback` effect seams execute SQL on the native
connection as required by T4 compatibility. Rollback is reverting the T5 port
and rebuilding private T4 artifacts, with **no ledger rewrite**. Windows x86-64
is tested; other OSes and recorded/live runtime equivalence remain unverified.
Production Python `ledger/store.py`: **+12 / -165, net -153 lines** (515 to 362).
The frozen oracle's 515 lines are test-only and were committed separately.

#### P4C-T6 owner clocks and adapter factory seam

T6 branches from **unmerged T5** (`1bec7f1d4aaf1aaac4c79bda8bfd613c9290e6bd`)
on `te/p4c-t6`, retaining the T1/T3/T4 single-writer/embedding chain without
importing independent P5/P6 branches. The private Python 3.13.15 interpreter,
extension and editable engine resolve inside `.worktrees/p4c-t6`.

The standalone oracle commit is **`0a5195e30f47cab73e6238f1c307ba489ca2e44d`**. Its `t6_replay.py` (63 lines)
and `t6_wall.py` (27 lines) are byte-identical copies of the pre-port clocks,
verified before the production edits. No existing test or frozen oracle changed.
The factory protocol is new: there was no pre-existing owner factory to freeze.
Its oracle is an explicitly authored fake factory invoked directly with the
frozen clock and an independent synthetic ledger; old entry-point discovery
remains unchanged Python host plumbing.

| Pre-port responsibility | Classification | T6 destination |
|---|---|---|
| `ReplayClock.__init__` | validation + state ownership | native UTC normalization/awareness and replay state |
| `ReplayClock.now_utc` | supplied-state read | native owner clock |
| `ReplayClock.advance_to` | pure monotonic decision + state update | host sequencing, binding datetime comparison/refusal |
| `ReplayClock.advance_by` | pure validation/arithmetic + state update | native duration classification, host update after successful addition |
| `ReplayClock.sleep` | deterministic sequencing | native validation and overridable `advance_by` dispatch |
| `WallClock.now_utc` | system-clock I/O | `te_host::clock::system_utc_microseconds` |
| `WallClock.sleep` | validation + OS effect | native validation/zero-noop and CPython OS sleep primitive |
| `discover_plugins` / `_load_group` | Python metadata discovery | unchanged, not a second owner-factory implementation |

Rust owns `te_host/src/clock.rs`, `te_py/src/clock.rs`, the provenance/validation/
factory invocation in `te_py/src/plugins.rs`, and the opt-in native composition
proof in `te_runtime/src/plugins.rs`. Python clock methods are shims; their
replaced decisions/state updates are deleted in this port. `_current_time`
remains a native-backed compatibility property because unchanged fixtures reset
it and replay subclasses depend on it. Replay sleep still invokes overrides
without a native mutable borrow across the callback.

The new `trade_engine.runtime` protocol is engine/client-independent:

- `load_factory(module, factory, plugin_paths, ledger=..., clock=..., config=...)`
  requires an already-open owner `Ledger`, an injected `Clock`, absolute plugin
  directories and a mapping. Configured source provenance is checked **before**
  module execution and again after import; cached outside-path modules refuse.
- A factory receives a frozen native `FactoryContext` containing non-owning
  store and clock views. The store exposes state/events/accounts/command/meta/
  count/seq reads, **not** open/close/connection/write authority; the clock
  delegates now/sleep to the same injected owner. No SQLite mutex spans callbacks.
- `FactoryResult(config, strategies=(), adapters=None)` is a carrier, not a job
  owner. Wrong shapes and directly returned owning ledgers/runners refuse.
  Funding/holdings effect invocation belongs to later role-flow/composition
  tickets; T6 only returns adapters and never starts a trading/provider job.
- A scoped native, process-wide construction fence rejects any additional
  **engine** writer, including import-time and joined-worker-thread opens.
  RAII releases the fence on ordinary and BaseException failures. Existing
  read-only readers and the already-open owner's handle remain available.
- This is **trusted-plugin composition, not a Python/SQLite sandbox**. Deliberate
  raw `sqlite3` access, forged imports or post-construction arbitrary code cannot
  be made safe by a Python object facade. Plugins must obey the injected-handle
  contract; same-ledger independent writers remain excluded by the OS guard.
- The loader temporarily adds configured roots and restores the identical
  `sys.path` object on return/refusal. A future long-lived interpreter retains
  configured plugin roots via T3's explicit startup search path; there is no
  client-specific import in the engine.

`te --proof --config <absolute-path>` additionally accepts **`factory-proof`**
with `owner: {ledger_path, clock, initial_time}`. The clock is `replay` (aware ISO
initial time required) or `wall` (no initial time). The ledger and its resolved
parent must remain inside the offline config directory; invalid clock input
refuses before a writable open. This is an offline seam, **not** `serve`, a
live job, an actor, readiness/cutover or the runtime flip. Existing T3
`packaging-proof` behavior and config refusals remain unchanged.

I7's CI scanner formerly prohibited clocks in *all* workspace crates. It now
permits only the exact `te_host/src/clock.rs` path, retaining refusal in core,
other host files, bindings/runtime and wasm. An additive negative test proves
both core and sibling-host violations still fire. No checked PyDec behavior,
schema, command ID, money spelling, dependency or calendar changes.

Final measured evidence (directly run; no existing test changed):

| Gate | Evidence |
|---|---|
| Pre-port clock tests | 8 passed unchanged |
| New parity/contract tests | **173**, included in final green CI |
| Clock/consumer focused gate | **278 passed**, including unchanged EOD/intraday service tests |
| Clock lockstep | 40 seeds x 250 commands = 10,000 prefixes; 9,080 successes / 920 exact refusals; each also compares current time |
| Factory walks | 100 append prefixes; callbacks during construction, after commit and from strategies; exact trace, codec bytes and full fold |
| Asserted counterparts | eight clock families and ten factory-result/construction families; additional input/import/provenance counterparts |
| Hand mutants | **12/12 compiled and killed** on final callback-safe source; baseline/restored each **134 passed / 39 deselected** |
| Private full CI | **2,305 passed**, 12 inherited warnings, exit 0; pytest **600.46 seconds** |
| Rust workspace | **102 passed**: 90 core, 9 host, 3 wasm |
| Explicit extension-feature unification | **102 passed** |
| Read-only real-ledger parity | **14,105 codecs / 23 states / four full folds identical**, exit 0 |
| Native factory-mode provenance | one built-in module; **zero loaded extension copies**; Python SQLite opens forbidden |

Full CI rebuilt both the native release executable and the installed extension
with this worktree's private interpreter before pytest. Final artifact SHA256:

- `te.exe`: `5eb6a466460adf69b4263e235978ed8dba946c247937ead2c0865b91746416b5`.
- bundled `python313.dll`: `e820bf024efd2b56bb2b82791e6b6ddc7303f070f8e72cba7637482a8a906238`.
- installed extension: `47c9ab700feec075d3b48a7a29857ad561ef8afe78ec2f1a37f87bd2beab594f`.

All mutants were killed by assertion failures, not compile/import/collection
errors. Sources were restored byte-for-byte in `finally`, followed by the
unconditional binary/extension rebuild and green final run:

| Mutant | Killing proof |
|---|---|
| backward movement allowed | one-microsecond backwards target |
| duration advance lost | successful sleep changes time |
| UTC normalization lost | non-UTC constructor preserves exact UTC result |
| finite/negative duration check lost | exact numeric refusal |
| bool duration accepted | bool type/message parity |
| negative timedelta allowed | negative-duration refusal/current-time preservation |
| overridden sleep advance bypassed | replay subclass callback/current time |
| zero wall sleep called | zero/bool injected effect trace |
| factory writer allowed | other-ledger open refusal and absent database |
| source check moved after import | uncached outside-path source never executes |
| factory result carrier unchecked | exact bad-result refusal |
| offline path confinement lost | no writable open outside offline config directory |

Restored Rust SHA256: host clock
`30aedb5d9a456d78015a23a34f807fabbc8c812a2de3ad94213d67602a7107c1`;
clock binding `f4e7d14ceaf5f0b991744886962783ca04825795d2115f16531c772ed13ec22f`;
factory binding `e50da0af83bff7d8990fd915b178222f398a6fbfca22bed5a6a74510028b739c`;
runtime config `dcea1f51399efaf622bce5d423f264ec864a792453d4c7a2bc55f4ff9ce8a8bc`.

Timing: `tools/time_p4c_t6.py`, nine independent paired synthetic cash books in
alternating order, three accounts, 360 observation/append ticks per book,
120 quote calculations per provider observation. Fixture construction and build
are excluded; clock reads/advances/sleep, fake provider work, native nested state
reads, codec construction and SQL commits are included. Frozen median
**235.8086 ms**, native **232.7046 ms**, **0.986837x** (<=1.25x); maxima
**299.4080 / 310.9781 ms**. This is the callback-safe implementation's final
measurement; the first-pass path had measured 1.010417x. Raw nine-pair samples are printed by the enforcing
tool. This is a clock-bearing synthetic hot-path measurement, not full-role
session/queue/strategy or tail-latency certification; T4's variability remains.

Deviations/boundaries: CPython datetime, timedelta, `math.isfinite`, repr and
in-place-add primitives remain binding codecs so subclasses, banker rounding,
overflow/type errors and refusal text match exactly. Native wall sleep uses
CPython's **OS primitive** to preserve signal interruption, bool/type conversion
and the real `time.sleep` effect seam; Python validation/sequencing does not
survive. Wall observations from independent real clocks are bracketed rather
than claimed identical; injected observations compare exactly.

Production clocks: `replay.py` **+16/-28, net -12** (63 to 51),
`wall.py` **+9/-9, net 0** (27 to 27). The new generic runtime carrier/shim
adds **45** Python lines; combined production Python net **+33**. The 90-line
oracle is test-only. Native clock ownership removes logic even though preserving
private compatibility seams and adding a genuinely new protocol costs lines.

Port code/test/tool churn (documentation excluded; oracle committed separately):

| File | Added | Deleted | Net |
|---|---:|---:|---:|
| `crates/te_host/src/clock.rs` | 83 | 0 | +83 |
| `crates/te_host/src/lib.rs` | 1 | 0 | +1 |
| `crates/te_py/src/clock.rs` | 245 | 0 | +245 |
| `crates/te_py/src/lib.rs` | 4 | 0 | +4 |
| `crates/te_py/src/plugins.rs` | 305 | 0 | +305 |
| `crates/te_py/src/store.rs` | 3 | 0 | +3 |
| `crates/te_runtime/src/config.rs` | 55 | 1 | +54 |
| `crates/te_runtime/src/main.rs` | 3 | 1 | +2 |
| `crates/te_runtime/src/plugins.rs` | 111 | 0 | +111 |
| `crates/te_runtime/src/python.rs` | 3 | 0 | +3 |
| `src/trade_engine/clock/replay.py` | 16 | 28 | -12 |
| `src/trade_engine/clock/wall.py` | 9 | 9 | 0 |
| `src/trade_engine/runtime/__init__.py` | 5 | 0 | +5 |
| `src/trade_engine/runtime/plugins.py` | 40 | 0 | +40 |
| `tests/test_p4c_clock.py` | 652 | 0 | +652 |
| `tools/ci_local.py` | 3 | 1 | +2 |
| `tools/mutate_p4c_t6.py` | 121 | 0 | +121 |
| `tools/time_p4c_t6.py` | 79 | 0 | +79 |

All Python in this port: **+925/-38, net +887**; including the separate 90-line
oracle, **net +977**. No existing test file was edited.

An additional datetime-subclass reentrancy probe found a real first-pass bug:
`advance_by` held a native mutable borrow across `__iadd__`, so an oracle-valid
nested `now_utc()` call refused with `Already mutably borrowed`. The first full
CI run was stopped before completion, not counted as a pass. The binding now
clones state under a short lock, performs timezone/comparison/arithmetic callbacks
unlocked, and replaces/decrefs old state outside the lock. Additive tests cover
reentrant arithmetic, timezone offsets and comparison mutations, including the
oracle's post-comparison refusal message. Final mutation/timing/full gates
cover this fix; their results supersede the earlier measured artifacts.
The first callback-safe mutation rerun had 11 valid kills and one **invalid**
run caused by a test that recreated a missing plugin without invalidating
Python's directory cache. The test now explicitly invalidates that cache before
its success counterpart. No assertion/mutant was weakened; a complete clean
12-mutant campaign then compiled/killed all 12 and restored 134 green tests.

Rollback is reverting this port and rebuilding the private T5 artifacts, without
rewriting any ledger. No push/merge/PR, client environment change, live/paper job,
scheduled-task action, server listener or deployment occurs. T0/T2, T7 onward,
historical session evidence, fresh strategy ports and non-Windows execution are
still outstanding.

## Working rules

- Build: `python -m pip install --no-deps --force-reinstall ./crates/te_py`
  (maturin backend via `crates/te_py/pyproject.toml`; `tools/ci_local.py` does this).
  `crates/te_py/pyproject.toml` must exist: without it maturin reads the root
  pyproject and replaces the `trade-engine` editable install.
- Rust tests: `cargo test --manifest-path crates\Cargo.toml --workspace` (also run
  by `ci_local.py`). Explicitly pin `PYO3_PYTHON` to the private Python 3.13 and
  `CARGO_TARGET_DIR` to this worktree's `crates\target`. Maturin enables
  `te_py/extension-module`; ordinary workspace tests use linkable embedding
  features. Do not let Cargo discover the unsupported global Python 3.14.
- Each phase is one commit series: Rust + oracle test, then the shim and the
  deletion of the Python logic, then this table's status. A row turns **done**
  only in the commit that adds the enforcing test, which the row names.

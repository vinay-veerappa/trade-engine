Parent design: trade-engine/docs/design/BROKER-PLATFORM.md

# BP-1: canonical futures instrument and the edge symbol map (plan phase 1)

Read BP-COMMON.md first.

## What already exists (do not duplicate)

`crates/te_core/src/sim/tick.rs` (re-exported as `te_core::futures` in `lib.rs`) already holds the spec table `FUTURE_SPECS` (12 roots; `tick_size`, `point_value`), `spec_for_root`, `month_from_code` / `month_to_code`, `parse_future_symbol` (accepts `NQ`, `/NQ`, `NQZ6`, `NQZ26`, `NQZ2026`), `round_to_tick` and `slip_ticks`. `ledger::model::FutureContract{root, contract_month, tick_size, point_value}` and `Instrument::Future` carry a contract. `calendar::globex::GlobexCalendar::for_root` gives the session.

Extend these. Do not add a second spec table: one table, with exchange, currency and `tick_value` (= tick_size * point_value; NQ 5.00, MNQ 0.50, ES 12.50) derived from or added to the same rows. The existing test that keeps the root list equal to the Globex calendar's stays green.

## What to build

1. **Edge symbol map, one table, in te_core** (a new module under `crates/te_core/src/`, e.g. `futures_symbols.rs`; the `te_core::futures` alias already exists, so keep it and pick a non-clashing module name). Canonical form is `NQZ6` (root + month code + one-digit year; the decade rule is in item 2). Convert from and to:
   - NT8 `NQ 12-26`
   - Tradovate `NQZ6`
   - TradingView `NQZ2026`
   - Schwab `/NQZ26`
   - IBKR: `localSymbol` `NQZ6` with `lastTradeDateOrContractMonth` `202612`

   One enum `Platform`; a conversion in each direction per platform. An unknown platform, root, month code or malformed spelling is refused with a `kind`.
2. **Decade rule.** A one-digit year is ambiguous. The resolver takes a `reference_date` argument (never a clock) and picks the nearest year that is at most 1 year in the past and within 10 years ahead. Test both sides of a decade boundary.
3. **Expiry and roll table.** A dated table: per root and contract month, an `expiry` and a `roll_date`, for 2024-2030, for the quarterly index roots and for the monthly CL/MCL/GC/MGC set the repo already supports. Cite each rule in a comment (CME rule, for example equity index third Friday of the quarter month; the roll date is the stated number of days before expiry). A contract month outside the table is refused, never extrapolated.
4. **Continuous symbol.** `NQ1!` (TV) and a bare root resolve to the front month ONLY through `front_month(root, as_of_date, roll_table)`. The date is an argument. `NQ1!` the day before the roll date gives the old contract; on the roll date it gives the next.
5. **pyo3 + Python carrier.** Expose the map through `trade_engine_rs`. Add `Future(Instrument)` to `src/trade_engine/domain/instruments.py`: a data carrier that calls Rust, the same pattern as `OptionContract` (`contract_args` style). No rule in Python.
6. **Read API.** If the engine's HTTP server (`src/trade_engine/server`) already has a read-route pattern, add `GET /v1/instruments/futures` (the table, as strings). If wiring it takes more than a small route, skip it and say so in the final reply.

## Tests (the enforcing tests; name them in the RUST_PORT.md row)

- Every platform spelling round-trips to the canonical form and back, for every root in the table and every month in 2024-2030 (proptest).
- Unknown root, unknown month code, malformed spelling, whitespace, wrong case where not allowed: each refused with a `kind`.
- `tick_value * ticks` matches the published specs for all 12 roots (state the figures in the test).
- `NQ1!` on both sides of a roll date; a front-month request with a date beyond the table is refused.
- Python: a `Future` round-trip through the pyo3 module; a check that Python holds no table of its own.
- All existing tests stay green.

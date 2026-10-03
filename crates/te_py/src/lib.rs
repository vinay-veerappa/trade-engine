//! `trade_engine_rs`: the ONE pyo3 module Python reaches te_core by
//! (docs/RUST_PORT.md D2). Thin wrappers only: plain values in and out, every
//! te_core refusal raised as ValueError with te_core's own message. A rule
//! written here instead of in te_core would be a second reader.

use chrono::{DateTime, NaiveDate, Utc};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use te_core::calendar as cal;
use te_core::greeks as gk;

fn date(iso: &str) -> PyResult<NaiveDate> {
    NaiveDate::parse_from_str(iso, "%Y-%m-%d")
        .map_err(|_| PyValueError::new_err(format!("Invalid date string format: {iso:?}")))
}

fn refuse<E: std::fmt::Display>(e: E) -> PyErr {
    PyValueError::new_err(e.to_string())
}

fn iso(d: NaiveDate) -> String {
    d.format("%Y-%m-%d").to_string()
}

#[pyfunction]
fn calendar_is_session(d: &str) -> PyResult<bool> {
    cal::is_session(date(d)?).map_err(refuse)
}

#[pyfunction]
fn calendar_is_holiday(d: &str) -> PyResult<bool> {
    cal::is_holiday(date(d)?).map_err(refuse)
}

#[pyfunction]
fn calendar_is_early_close(d: &str) -> PyResult<bool> {
    cal::is_early_close(date(d)?).map_err(refuse)
}

/// Seconds since the Unix epoch, UTC.
#[pyfunction]
fn calendar_session_open(d: &str) -> PyResult<i64> {
    cal::session_open(date(d)?).map(|t| t.timestamp()).map_err(refuse)
}

/// Seconds since the Unix epoch, UTC.
#[pyfunction]
fn calendar_session_close(d: &str) -> PyResult<i64> {
    cal::session_close(date(d)?).map(|t| t.timestamp()).map_err(refuse)
}

#[pyfunction]
fn calendar_next_session(d: &str) -> PyResult<String> {
    cal::next_session(date(d)?).map(iso).map_err(refuse)
}

#[pyfunction]
fn calendar_previous_session(d: &str) -> PyResult<String> {
    cal::previous_session(date(d)?).map(iso).map_err(refuse)
}

#[pyfunction]
fn calendar_roll_to_session(d: &str, forward: bool) -> PyResult<String> {
    cal::roll_to_session(date(d)?, forward).map(iso).map_err(refuse)
}

#[pyfunction]
fn calendar_sessions_in_range(start: &str, end: &str) -> PyResult<Vec<String>> {
    cal::sessions_in_range(date(start)?, date(end)?)
        .map(|v| v.into_iter().map(iso).collect())
        .map_err(refuse)
}

#[pyfunction]
fn calendar_sessions_window_back(end: &str, count: i64) -> PyResult<Vec<String>> {
    cal::sessions_window_back(date(end)?, count)
        .map(|v| v.into_iter().map(iso).collect())
        .map_err(refuse)
}

/// `epoch_us` is microseconds since the Unix epoch, UTC.
#[pyfunction]
fn calendar_is_open_at(epoch_us: i64) -> PyResult<bool> {
    let t = DateTime::<Utc>::from_timestamp_micros(epoch_us)
        .ok_or_else(|| PyValueError::new_err(format!("timestamp {epoch_us}us is out of range")))?;
    cal::is_open_at(t).map_err(refuse)
}

fn right(flag: &str) -> PyResult<gk::Right> {
    match flag {
        "c" => Ok(gk::Right::Call),
        "p" => Ok(gk::Right::Put),
        other => Err(PyValueError::new_err(format!("flag must be 'c' or 'p', got {other:?}"))),
    }
}

#[pyfunction]
fn greeks_price(flag: &str, spot: f64, strike: f64, t: f64, rate: f64, q: f64, sigma: f64) -> PyResult<f64> {
    gk::price(right(flag)?, spot, strike, t, rate, q, sigma).map_err(refuse)
}

#[pyfunction]
fn greeks_implied_vol(flag: &str, spot: f64, strike: f64, t: f64, rate: f64, q: f64, price: f64) -> PyResult<f64> {
    gk::implied_vol(right(flag)?, spot, strike, t, rate, q, price).map_err(refuse)
}

/// (delta, gamma, theta per day, vega per point, rho per point)
#[pyfunction]
fn greeks_greeks(flag: &str, spot: f64, strike: f64, t: f64, rate: f64, q: f64, sigma: f64) -> PyResult<(f64, f64, f64, f64, f64)> {
    let g = gk::greeks(right(flag)?, spot, strike, t, rate, q, sigma).map_err(refuse)?;
    Ok((g.delta, g.gamma, g.theta, g.vega, g.rho))
}

// --- margin (P1c) ------------------------------------------------------------------
// Decimals cross as strings, contracts and positions as JSON (te_core::margin::wire).
// A refusal is a ValueError whose args are (kind, message): the shim re-raises it as
// the Python exception type `kind` names ("value", "option", "unresolvable").

use te_core::margin::{wire, MarginError};

fn refuse_margin(e: MarginError) -> PyErr {
    PyValueError::new_err((e.kind(), e.message().to_string()))
}

#[pyfunction]
#[pyo3(signature = (quantity, mark, initial=None, maintenance=None))]
fn margin_requirement(
    quantity: &str,
    mark: &str,
    initial: Option<&str>,
    maintenance: Option<&str>,
) -> PyResult<(String, String, String)> {
    wire::requirement_str(quantity, mark, initial, maintenance).map_err(refuse_margin)
}

#[pyfunction]
fn margin_validate_override(initial: &str, maintenance: &str) -> PyResult<()> {
    wire::validate_override_str(initial, maintenance).map_err(refuse_margin)
}

#[pyfunction]
fn margin_summary(equity: &str, maintenance: &str) -> PyResult<(String, String)> {
    wire::summary_str(equity, maintenance).map_err(refuse_margin)
}

#[pyfunction]
fn margin_naked(request: &str) -> PyResult<String> {
    wire::naked_str(request).map_err(refuse_margin)
}

#[pyfunction]
fn margin_match(request: &str) -> PyResult<String> {
    wire::match_json(request).map_err(refuse_margin)
}

#[pyfunction]
fn margin_strategy(request: &str) -> PyResult<String> {
    wire::strategy_json(request).map_err(refuse_margin)
}

#[pyfunction]
fn margin_book(request: &str) -> PyResult<String> {
    wire::book_json(request).map_err(refuse_margin)
}

#[pyfunction]
fn margin_account(request: &str) -> PyResult<String> {
    wire::account_json(request).map_err(refuse_margin)
}

#[pyfunction]
fn margin_constants() -> PyResult<String> {
    wire::constants_json().map_err(refuse_margin)
}

#[pyfunction]
fn margin_definitions() -> PyResult<String> {
    wire::definitions_json().map_err(refuse_margin)
}

fn register_margin(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(margin_requirement, m)?)?;
    m.add_function(wrap_pyfunction!(margin_validate_override, m)?)?;
    m.add_function(wrap_pyfunction!(margin_summary, m)?)?;
    m.add_function(wrap_pyfunction!(margin_naked, m)?)?;
    m.add_function(wrap_pyfunction!(margin_match, m)?)?;
    m.add_function(wrap_pyfunction!(margin_strategy, m)?)?;
    m.add_function(wrap_pyfunction!(margin_book, m)?)?;
    m.add_function(wrap_pyfunction!(margin_account, m)?)?;
    m.add_function(wrap_pyfunction!(margin_constants, m)?)?;
    m.add_function(wrap_pyfunction!(margin_definitions, m)?)?;
    Ok(())
}
// --- end margin -----------------------------------------------------------------------

// --- option rules (P1e) -------------------------------------------------------------
// A refusal crosses as ValueError(kind, message); the Python shims map the kind back
// to the exception type the pre-port code raised.

use te_core::options as opt;

fn refuse_opt(e: opt::OptionError) -> PyErr {
    PyValueError::new_err((e.kind().to_string(), e.message().to_string()))
}

/// A Decimal as the shims send it: negative, digits, exponent, special code, str().
type DecArgs = (bool, String, i64, String, String);
/// A contract: underlying, year, month, day, right code, then the strike's Decimal.
type ContractArgs = (String, i32, u32, u32, String, bool, String, i64, String, String);

fn dec_wire(a: DecArgs) -> PyResult<opt::DecWire> {
    let special = opt::Special::from_code(&a.3)
        .ok_or_else(|| PyValueError::new_err(("value".to_string(), format!("Unknown Decimal special {:?}", a.3))))?;
    Ok(opt::DecWire { neg: a.0, digits: a.1, exp: a.2, special, text: a.4 })
}

fn contract_wire(a: ContractArgs) -> PyResult<opt::ContractWire> {
    let right = opt::Right::from_code(&a.4)
        .ok_or_else(|| PyValueError::new_err(("value".to_string(), format!("Invalid option right: {}", a.4))))?;
    let strike = dec_wire((a.5, a.6, a.7, a.8, a.9))?;
    Ok(opt::ContractWire { underlying: a.0, year: a.1, month: a.2, day: a.3, right, strike })
}

#[pyfunction]
#[pyo3(signature = (symbol))]
fn option_equity_symbol(symbol: Option<&str>) -> PyResult<String> {
    opt::equity_symbol(symbol).map_err(refuse_opt)
}

#[pyfunction]
fn option_validate_underlying(underlying: &str) -> PyResult<String> {
    opt::validate_underlying(underlying).map_err(refuse_opt)
}

/// Strike, multiplier and right, in `__post_init__` order; returns the right's code.
#[pyfunction]
#[pyo3(signature = (strike, multiplier, multiplier_text, right, right_text))]
fn option_validate_rest(
    strike: DecArgs,
    multiplier: Option<&str>,
    multiplier_text: &str,
    right: Option<&str>,
    right_text: &str,
) -> PyResult<&'static str> {
    let strike = dec_wire(strike)?;
    opt::validate_rest(&strike, multiplier, multiplier_text, right, right_text).map(opt::Right::code).map_err(refuse_opt)
}

#[pyfunction]
fn option_to_occ(c: ContractArgs) -> PyResult<String> {
    contract_wire(c)?.occ().map_err(refuse_opt)
}

/// (root, year, month, day, right code, strike text).
#[pyfunction]
#[pyo3(signature = (occ))]
fn option_parse_occ(occ: Option<&str>) -> PyResult<(String, i32, u32, u32, &'static str, String)> {
    let p = opt::parse_occ(occ).map_err(refuse_opt)?;
    Ok((p.root, p.year, p.month, p.day, p.right.code(), p.strike_text))
}

/// (root, underlying, exercise code, settlement code, settle-time code).
#[pyfunction]
fn option_style(root: &str) -> PyResult<(String, String, &'static str, &'static str, &'static str)> {
    let s = opt::option_style(root).map_err(refuse_opt)?;
    Ok((s.root, s.underlying, s.exercise.code(), s.settlement.code(), s.settle_time.code()))
}

#[pyfunction]
fn option_chain_roots(underlying: &str) -> PyResult<Vec<String>> {
    opt::chain_roots(underlying).map_err(refuse_opt)
}

/// Seconds since the Unix epoch, UTC.
#[pyfunction]
fn option_settlement_instant(c: ContractArgs) -> PyResult<i64> {
    opt::settlement_instant(&contract_wire(c)?).map_err(refuse_opt)
}

#[pyfunction]
fn option_last_trade_date(c: ContractArgs) -> PyResult<String> {
    opt::last_trade_date(&contract_wire(c)?).map(iso).map_err(refuse_opt)
}

#[pyfunction]
fn option_intrinsic(c: ContractArgs, price: &str) -> PyResult<String> {
    opt::lifecycle::intrinsic(&contract_wire(c)?, price).map_err(refuse_opt)
}

#[pyfunction]
fn option_expiry_outcome(c: ContractArgs, held_buy: bool, price: &str) -> PyResult<&'static str> {
    opt::lifecycle::expiry_outcome(&contract_wire(c)?, held_buy, price).map_err(refuse_opt)
}

#[pyfunction]
fn option_is_cash_settled(c: ContractArgs) -> PyResult<bool> {
    opt::lifecycle::is_cash_settled(&contract_wire(c)?).map_err(refuse_opt)
}

#[pyfunction]
fn option_can_exercise_early(c: ContractArgs) -> PyResult<bool> {
    opt::lifecycle::can_exercise_early(&contract_wire(c)?).map_err(refuse_opt)
}

#[pyfunction]
fn option_deliverable(c: ContractArgs) -> PyResult<String> {
    opt::lifecycle::deliverable(&contract_wire(c)?).map_err(refuse_opt)
}

#[pyfunction]
fn option_delivery(c: ContractArgs, held_buy: bool, premium: &str) -> PyResult<(bool, String)> {
    opt::lifecycle::delivery(&contract_wire(c)?, held_buy, premium).map_err(refuse_opt)
}

#[pyfunction]
fn option_exercised_for_dividend(c: ContractArgs, close: &str, bid: &str, dividend: &str) -> PyResult<bool> {
    opt::lifecycle::exercised_for_dividend(&contract_wire(c)?, close, bid, dividend).map_err(refuse_opt)
}

#[pyfunction]
fn option_exercise_threshold() -> &'static str {
    opt::lifecycle::exercise_threshold()
}

fn register_options(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(option_equity_symbol, m)?)?;
    m.add_function(wrap_pyfunction!(option_validate_underlying, m)?)?;
    m.add_function(wrap_pyfunction!(option_validate_rest, m)?)?;
    m.add_function(wrap_pyfunction!(option_to_occ, m)?)?;
    m.add_function(wrap_pyfunction!(option_parse_occ, m)?)?;
    m.add_function(wrap_pyfunction!(option_style, m)?)?;
    m.add_function(wrap_pyfunction!(option_chain_roots, m)?)?;
    m.add_function(wrap_pyfunction!(option_settlement_instant, m)?)?;
    m.add_function(wrap_pyfunction!(option_last_trade_date, m)?)?;
    m.add_function(wrap_pyfunction!(option_intrinsic, m)?)?;
    m.add_function(wrap_pyfunction!(option_expiry_outcome, m)?)?;
    m.add_function(wrap_pyfunction!(option_is_cash_settled, m)?)?;
    m.add_function(wrap_pyfunction!(option_can_exercise_early, m)?)?;
    m.add_function(wrap_pyfunction!(option_deliverable, m)?)?;
    m.add_function(wrap_pyfunction!(option_delivery, m)?)?;
    m.add_function(wrap_pyfunction!(option_exercised_for_dividend, m)?)?;
    m.add_function(wrap_pyfunction!(option_exercise_threshold, m)?)?;
    Ok(())
}
// --- end option rules -----------------------------------------------------------------

#[pyfunction]
fn risk_rules_from_mapping(doc: &str) -> String {
    te_core::risk::json_rules_from_mapping(doc)
}

#[pyfunction]
fn risk_validate_rules(doc: &str) -> String {
    te_core::risk::json_validate_rules(doc)
}

#[pyfunction]
fn risk_validate_rails(doc: &str) -> String {
    te_core::risk::json_validate_rails(doc)
}

#[pyfunction]
fn risk_validate_hours(doc: &str) -> String {
    te_core::risk::json_validate_hours(doc)
}

#[pyfunction]
fn risk_validate_context(doc: &str) -> String {
    te_core::risk::json_validate_context(doc)
}

#[pyfunction]
fn risk_hours_is_open(doc: &str) -> String {
    te_core::risk::json_hours_is_open(doc)
}

#[pyfunction]
fn risk_drawdown_controls(doc: &str) -> String {
    te_core::risk::json_drawdown_controls(doc)
}

#[pyfunction]
fn risk_evaluate(doc: &str) -> String {
    te_core::risk::json_evaluate(doc)
}

// --- ledger shadow (P2a) ------------------------------------------------------------
// Test-only entry points: a refusal crosses as ValueError(kind, message).

fn refuse_ledger(e: te_core::ledger::model::LErr) -> PyErr {
    PyValueError::new_err((e.kind.to_string(), e.msg))
}

#[pyfunction]
fn ledger_reencode(data: &[u8]) -> PyResult<Vec<u8>> {
    te_core::ledger::codec::reencode(data).map_err(refuse_ledger)
}

fn decode_all(events: &[Vec<u8>]) -> Result<Vec<te_core::ledger::model::Event>, te_core::ledger::model::LErr> {
    events.iter().map(|b| te_core::ledger::codec::event_from_bytes(b)).collect()
}

/// Fold encoded events for one account; canonical state JSON bytes.
#[pyfunction]
fn ledger_fold(events: Vec<Vec<u8>>, account: &str) -> PyResult<Vec<u8>> {
    let run = || -> te_core::ledger::model::R<String> {
        let evs = decode_all(&events)?;
        let st = te_core::ledger::fold::fold_account(&evs, account)?;
        Ok(te_core::ledger::json::dumps(&te_core::ledger::canon::canon_account(&st)?))
    };
    run().map(String::into_bytes).map_err(refuse_ledger)
}

/// Fold every account: `{"m":[[account,state],...]}` canonical JSON bytes.
#[pyfunction]
fn ledger_fold_all(events: Vec<Vec<u8>>) -> PyResult<Vec<u8>> {
    let run = || -> te_core::ledger::model::R<String> {
        let evs = decode_all(&events)?;
        let st = te_core::ledger::fold::fold(&evs)?;
        Ok(te_core::ledger::json::dumps(&te_core::ledger::canon::canon_states(&st)?))
    };
    run().map(String::into_bytes).map_err(refuse_ledger)
}

#[pymodule]
fn trade_engine_rs(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add("__version__", env!("CARGO_PKG_VERSION"))?;
    m.add_function(wrap_pyfunction!(calendar_is_session, m)?)?;
    m.add_function(wrap_pyfunction!(calendar_is_holiday, m)?)?;
    m.add_function(wrap_pyfunction!(calendar_is_early_close, m)?)?;
    m.add_function(wrap_pyfunction!(calendar_session_open, m)?)?;
    m.add_function(wrap_pyfunction!(calendar_session_close, m)?)?;
    m.add_function(wrap_pyfunction!(calendar_next_session, m)?)?;
    m.add_function(wrap_pyfunction!(calendar_previous_session, m)?)?;
    m.add_function(wrap_pyfunction!(calendar_roll_to_session, m)?)?;
    m.add_function(wrap_pyfunction!(calendar_sessions_in_range, m)?)?;
    m.add_function(wrap_pyfunction!(calendar_sessions_window_back, m)?)?;
    m.add_function(wrap_pyfunction!(calendar_is_open_at, m)?)?;
    m.add_function(wrap_pyfunction!(greeks_price, m)?)?;
    m.add_function(wrap_pyfunction!(greeks_implied_vol, m)?)?;
    m.add_function(wrap_pyfunction!(greeks_greeks, m)?)?;
    register_margin(m)?;
    m.add_function(wrap_pyfunction!(ledger_reencode, m)?)?;
    m.add_function(wrap_pyfunction!(ledger_fold, m)?)?;
    m.add_function(wrap_pyfunction!(ledger_fold_all, m)?)?;
    register_options(m)?;
    m.add_function(wrap_pyfunction!(risk_rules_from_mapping, m)?)?;
    m.add_function(wrap_pyfunction!(risk_validate_rules, m)?)?;
    m.add_function(wrap_pyfunction!(risk_validate_rails, m)?)?;
    m.add_function(wrap_pyfunction!(risk_validate_hours, m)?)?;
    m.add_function(wrap_pyfunction!(risk_validate_context, m)?)?;
    m.add_function(wrap_pyfunction!(risk_hours_is_open, m)?)?;
    m.add_function(wrap_pyfunction!(risk_drawdown_controls, m)?)?;
    m.add_function(wrap_pyfunction!(risk_evaluate, m)?)?;
    Ok(())
}

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
    Ok(())
}

//! `trade_engine_rs`: the ONE pyo3 module Python reaches te_core by
//! (docs/RUST_PORT.md D2). Thin wrappers only: plain values in and out, every
//! te_core refusal raised as ValueError with te_core's own message. A rule
//! written here instead of in te_core would be a second reader.

use chrono::{DateTime, NaiveDate, Utc};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use te_core::calendar as cal;

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
    Ok(())
}

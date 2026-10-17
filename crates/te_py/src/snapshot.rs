//! Daily account snapshot door into te_core::snapshot (E8b, rules doc §11).

use std::collections::HashMap;
use std::str::FromStr;
use chrono::NaiveDate;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use rust_decimal::Decimal;
use te_core::ledger::model::{Event, LErr};
use te_core::snapshot::{self as snap, AccountSnapshot, SnapshotError};

fn refuse_snapshot(e: SnapshotError) -> PyErr {
    let kind = match &e {
        SnapshotError::NoAsOf { .. } => "snapshot",
        SnapshotError::NoSessionCloseMark { .. } => "snapshot",
        SnapshotError::NoSessionCloseMarkOpenPosition { .. } => "snapshot",
        SnapshotError::EmptySnapshots(_) => "snapshot",
        SnapshotError::Margin(m) => {
            if m.message().starts_with("No session-close mark") {
                "snapshot"
            } else {
                m.kind()
            }
        }
        SnapshotError::Ledger(_) => "ledger",
        SnapshotError::Overflow => "overflow",
        SnapshotError::Value(_) => "value",
    };
    PyValueError::new_err((kind.to_string(), e.message()))
}

fn refuse_ledger(e: LErr) -> PyErr {
    PyValueError::new_err((e.kind.to_string(), e.msg))
}

fn decode_all(events: &[Vec<u8>]) -> Result<Vec<Event>, LErr> {
    events.iter().map(|b| te_core::ledger::codec::event_from_bytes(b)).collect()
}

pub type RawSnapshot = (
    String,      // 0: session YYYY-MM-DD
    String,      // 1: equity
    String,      // 2: cash
    String,      // 3: gross_exposure
    String,      // 4: net_exposure
    String,      // 5: margin_used
    String,      // 6: margin_available
    String,      // 7: margin_peak
    String,      // 8: heat
    Vec<String>, // 9: heat_unknown_positions
    String,      // 10: drawdown_from_peak
    usize,       // 11: drawdown_duration_sessions
);

fn raw_from_snapshot(s: &AccountSnapshot) -> RawSnapshot {
    (
        s.session.format("%Y-%m-%d").to_string(),
        s.equity.to_string(),
        s.cash.to_string(),
        s.gross_exposure.to_string(),
        s.net_exposure.to_string(),
        s.margin_used.to_string(),
        s.margin_available.to_string(),
        s.margin_peak.to_string(),
        s.heat.to_string(),
        s.heat_unknown_positions.clone(),
        s.drawdown_from_peak.to_string(),
        s.drawdown_duration_sessions,
    )
}

fn snapshot_from_raw(r: &RawSnapshot) -> Result<AccountSnapshot, SnapshotError> {
    let session = NaiveDate::parse_from_str(&r.0, "%Y-%m-%d")
        .map_err(|e| SnapshotError::Value(format!("Invalid date {}: {e}", r.0)))?;
    let equity = Decimal::from_str(&r.1).map_err(|e| SnapshotError::Value(e.to_string()))?;
    let cash = Decimal::from_str(&r.2).map_err(|e| SnapshotError::Value(e.to_string()))?;
    let gross_exposure = Decimal::from_str(&r.3).map_err(|e| SnapshotError::Value(e.to_string()))?;
    let net_exposure = Decimal::from_str(&r.4).map_err(|e| SnapshotError::Value(e.to_string()))?;
    let margin_used = Decimal::from_str(&r.5).map_err(|e| SnapshotError::Value(e.to_string()))?;
    let margin_available = Decimal::from_str(&r.6).map_err(|e| SnapshotError::Value(e.to_string()))?;
    let margin_peak = Decimal::from_str(&r.7).map_err(|e| SnapshotError::Value(e.to_string()))?;
    let heat = Decimal::from_str(&r.8).map_err(|e| SnapshotError::Value(e.to_string()))?;
    let heat_unknown_positions = r.9.clone();
    let drawdown_from_peak = Decimal::from_str(&r.10).map_err(|e| SnapshotError::Value(e.to_string()))?;
    let drawdown_duration_sessions = r.11;

    Ok(AccountSnapshot {
        session,
        equity,
        cash,
        gross_exposure,
        net_exposure,
        margin_used,
        margin_available,
        margin_peak,
        heat,
        heat_unknown_positions,
        drawdown_from_peak,
        drawdown_duration_sessions,
    })
}

#[pyfunction]
#[pyo3(signature = (events, account, overrides=None))]
fn snapshot_daily(
    events: Vec<Vec<u8>>,
    account: &str,
    overrides: Option<Vec<(String, String, String)>>,
) -> PyResult<Vec<RawSnapshot>> {
    let evs = decode_all(&events).map_err(refuse_ledger)?;
    let parsed_overrides = match overrides {
        Some(list) => {
            let mut map = HashMap::new();
            for (sym, init_s, maint_s) in list {
                let init = Decimal::from_str(&init_s)
                    .map_err(|e| PyValueError::new_err(("value".to_string(), e.to_string())))?;
                let maint = Decimal::from_str(&maint_s)
                    .map_err(|e| PyValueError::new_err(("value".to_string(), e.to_string())))?;
                map.insert(sym, (init, maint));
            }
            Some(map)
        }
        None => None,
    };
    let snaps = snap::daily_snapshots_with_overrides(&evs, account, parsed_overrides.as_ref())
        .map_err(refuse_snapshot)?;
    Ok(snaps.iter().map(raw_from_snapshot).collect())
}

#[pyfunction]
fn snapshot_peak_margin(snapshots: Vec<RawSnapshot>) -> PyResult<String> {
    let snaps = snapshots
        .iter()
        .map(snapshot_from_raw)
        .collect::<Result<Vec<_>, _>>()
        .map_err(refuse_snapshot)?;
    let peak = snap::peak_margin(&snaps).map_err(refuse_snapshot)?;
    Ok(peak.to_string())
}

#[pyfunction]
fn snapshot_max_drawdown(snapshots: Vec<RawSnapshot>) -> PyResult<String> {
    let snaps = snapshots
        .iter()
        .map(snapshot_from_raw)
        .collect::<Result<Vec<_>, _>>()
        .map_err(refuse_snapshot)?;
    let dd = snap::max_drawdown(&snaps).map_err(refuse_snapshot)?;
    Ok(dd.to_string())
}

#[pyfunction]
fn snapshot_day_note_line(snapshot: RawSnapshot, account: &str) -> PyResult<String> {
    let snap = snapshot_from_raw(&snapshot).map_err(refuse_snapshot)?;
    Ok(snap::day_note_line(&snap, account))
}

#[pyfunction]
fn snapshot_protective_stop(state_text: &str, instrument_text: &str) -> PyResult<Option<String>> {
    let st = te_core::ledger::bridge::account_from_text(state_text).map_err(refuse_ledger)?;
    let instr = match te_core::ledger::bridge::obj_from_text(instrument_text).map_err(refuse_ledger)? {
        te_core::ledger::model::Obj::Instr(i) => i,
        _ => return Err(PyValueError::new_err(("value".to_string(), "expected instrument".to_string()))),
    };
    Ok(snap::protective_stop(&st, &instr).map(|d| d.to_string()))
}

#[pyfunction]
fn snapshot_working_stop_risk(state_text: &str) -> PyResult<(String, Vec<String>)> {
    let st = te_core::ledger::bridge::account_from_text(state_text).map_err(refuse_ledger)?;
    let (heat, unknown) = snap::working_stop_risk(&st).map_err(refuse_snapshot)?;
    Ok((heat.to_string(), unknown))
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(snapshot_daily, m)?)?;
    m.add_function(wrap_pyfunction!(snapshot_peak_margin, m)?)?;
    m.add_function(wrap_pyfunction!(snapshot_max_drawdown, m)?)?;
    m.add_function(wrap_pyfunction!(snapshot_day_note_line, m)?)?;
    m.add_function(wrap_pyfunction!(snapshot_protective_stop, m)?)?;
    m.add_function(wrap_pyfunction!(snapshot_working_stop_risk, m)?)?;
    Ok(())
}

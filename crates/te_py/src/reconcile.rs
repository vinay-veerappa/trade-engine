//! W7-CORE-C6: `oms/reconcile.py` and `oms/restore.py` move into te_py.
//!
//! The two modules' public functions were pure sequencing over the native ledger rules
//! (`te_core::oms::reconcile` / `te_core::oms::restore`, reached through `trade_engine_rs`'s
//! `oms_native_*` functions) and Python collaborators (the ledger, the clock, the broker, the
//! order manager). This file runs the same steps, in the same order, calling the native
//! functions directly and every collaborator through Python attribute lookup.
//!
//! A native refusal crosses as `ValueError((kind, message))`, exactly as `oms_native` raises
//! it, so the Python wrapper's `_rs.call` maps it to the exception each module registered
//! (`ReconcileError`, `RestoreError`, and the ledger's kinds). A Python exception a
//! collaborator raised (the manager, the broker, the clock) propagates unchanged.

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::sync::GILOnceCell;
use pyo3::types::PyDict;

use crate::oms_native;

/// `trade_engine.domain.portfolio.Fill`, imported once and cached (the carrier stays Python;
/// Rust builds it here with the exact field values `_fill_of` used).
static FILL_CLS: GILOnceCell<Py<PyAny>> = GILOnceCell::new();

fn fill_class(py: Python<'_>) -> PyResult<&'static Py<PyAny>> {
    FILL_CLS.get_or_try_init(py, || -> PyResult<Py<PyAny>> {
        Ok(py.import("trade_engine.domain.portfolio")?.getattr("Fill")?.unbind())
    })
}

/// `_fill_of`: a `Fill` carrier from a `VenueFill`, with `venue_env` read from the broker.
fn fill_of<'py>(
    py: Python<'py>,
    broker: &Bound<'py, PyAny>,
    account_id: &str,
    venue_fill: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    let venue_fill_id = venue_fill.getattr("venue_fill_id")?;
    let venue_order_id = venue_fill.getattr("venue_order_id")?;
    let kwargs = PyDict::new(py);
    kwargs.set_item("fill_id", &venue_fill_id)?;
    kwargs.set_item("order_id", &venue_order_id)?;
    kwargs.set_item("account_id", account_id)?;
    kwargs.set_item("instrument", venue_fill.getattr("instrument")?)?;
    kwargs.set_item("quantity", venue_fill.getattr("quantity")?)?;
    kwargs.set_item("price", venue_fill.getattr("price")?)?;
    kwargs.set_item("venue_env", broker.getattr("env")?)?;
    kwargs.set_item("filled_at", venue_fill.getattr("filled_at")?)?;
    kwargs.set_item("side", venue_fill.getattr("side")?)?;
    kwargs.set_item("fee", venue_fill.getattr("fee")?)?;
    kwargs.set_item("venue_order_id", &venue_order_id)?;
    kwargs.set_item("venue_execution_id", &venue_fill_id)?;
    kwargs.set_item("leg_id", venue_fill.getattr("leg_id")?)?;
    fill_class(py)?.bind(py).call((), Some(&kwargs))
}

/// `enqueue_journal_fill`: one journal execution per fill. The payload's bracket legs, asset
/// class and multiplier are Rust's (`oms_native_journal_payload`); the outbox row is the
/// ledger's. Shared by `_record_venue_fill` and `_heal_recorded_fill`.
#[pyfunction]
pub(crate) fn enqueue_journal_fill(
    py: Python<'_>,
    ledger: &Bound<'_, PyAny>,
    clock: &Bound<'_, PyAny>,
    journal_account: &str,
    state: &Bound<'_, PyAny>,
    fill: &Bound<'_, PyAny>,
) -> PyResult<Py<PyAny>> {
    let account_id: String = state.getattr("account_id")?.extract()?;
    let codec = py.import("trade_engine.ledger.codec")?;
    let tree = codec.call_method1("encode_payload", (fill,))?;
    let fill_tree: String = codec.call_method1("text", (tree,))?.extract()?;
    let (payload, seq) =
        oms_native::oms_native_journal_payload(ledger, &account_id, state, &fill_tree, journal_account)?;
    let payload_obj = py.import("json")?.call_method1("loads", (payload,))?;
    let created_at = clock.call_method0("now_utc")?;
    let kwargs = PyDict::new(py);
    kwargs.set_item("created_at", &created_at)?;
    Ok(ledger
        .call_method(
            "enqueue_outbox",
            (seq, format!("journal:{journal_account}"), payload_obj),
            Some(&kwargs),
        )?
        .unbind())
}

/// `_heal_recorded_fill`: finish what a crash after a committed fill may have left undone
/// (F2). The bracket is re-synced only for the bracket's latest fill; the journal row only
/// for the account's latest fill.
fn heal_recorded_fill(
    py: Python<'_>,
    ledger: &Bound<'_, PyAny>,
    clock: &Bound<'_, PyAny>,
    manager: &Bound<'_, PyAny>,
    account_id: &str,
    fill: &Bound<'_, PyAny>,
    journal_account: Option<&str>,
) -> PyResult<()> {
    let state = ledger.call_method1("state", (account_id,))?;
    let fill_id: String = fill.getattr("fill_id")?.extract()?;
    let order_id: String = fill.getattr("order_id")?.extract()?;
    let (resync, rejournal_candidate, fill_seq) =
        oms_native::oms_native_heal_plan(ledger, account_id, &state, &fill_id, &order_id)?;
    if let Some((entry_id, cause)) = resync {
        let entry = state.getattr("orders")?.get_item(entry_id)?;
        manager.call_method1("synchronize_bracket", (entry, cause))?;
    }
    if journal_account.is_none() || fill_seq.is_none() || !rejournal_candidate {
        return Ok(());
    }
    let journal_account = journal_account.expect("checked above");
    let fill_seq = fill_seq.expect("checked above");
    let row = ledger.call_method1("outbox_row", (fill_seq, format!("journal:{journal_account}")))?;
    if row.is_none() {
        let state = ledger.call_method1("state", (account_id,))?;
        enqueue_journal_fill(py, ledger, clock, journal_account, &state, fill)?;
    }
    Ok(())
}

/// `_record_venue_fill`: record one venue fill, or heal the bracket the last time left undone
/// if the fill is a replay (I3). Returns 1 when newly recorded, else 0.
fn record_venue_fill(
    py: Python<'_>,
    ledger: &Bound<'_, PyAny>,
    clock: &Bound<'_, PyAny>,
    broker: &Bound<'_, PyAny>,
    manager: &Bound<'_, PyAny>,
    account_id: &str,
    venue_fill: &Bound<'_, PyAny>,
    journal_account: Option<&str>,
) -> PyResult<i64> {
    let venue_fill_id: String = venue_fill.getattr("venue_fill_id")?.extract()?;
    if ledger
        .call_method1("has_command", (format!("fill:{venue_fill_id}"),))?
        .extract::<bool>()?
    {
        let fill = fill_of(py, broker, account_id, venue_fill)?;
        heal_recorded_fill(py, ledger, clock, manager, account_id, &fill, journal_account)?;
        return Ok(0);
    }
    let state = ledger.call_method1("state", (account_id,))?;
    let venue_order_id: String = venue_fill.getattr("venue_order_id")?.extract()?;
    oms_native::oms_native_check_known_order(&state, &venue_fill_id, &venue_order_id, account_id)?;
    let fill = fill_of(py, broker, account_id, venue_fill)?;
    manager.call_method1("record_fill", (&fill,))?;
    if let Some(journal_account) = journal_account {
        let state = ledger.call_method1("state", (account_id,))?;
        enqueue_journal_fill(py, ledger, clock, journal_account, &state, &fill)?;
    }
    Ok(1)
}

/// `reconcile_after`: record every venue fill at or after `since`; re-read the states it
/// changed. Returns how many fills were newly recorded.
#[pyfunction]
#[pyo3(signature = (ledger, clock, broker, manager, account_id, since, journal_account=None))]
pub(crate) fn reconcile_after(
    py: Python<'_>,
    ledger: &Bound<'_, PyAny>,
    clock: &Bound<'_, PyAny>,
    broker: &Bound<'_, PyAny>,
    manager: &Bound<'_, PyAny>,
    account_id: &str,
    since: &Bound<'_, PyAny>,
    journal_account: Option<&str>,
) -> PyResult<i64> {
    let mut recorded = 0i64;
    for venue_fill in broker.call_method1("fills", (since,))?.try_iter()? {
        let venue_fill = venue_fill?;
        recorded += record_venue_fill(py, ledger, clock, broker, manager, account_id, &venue_fill, journal_account)?;
    }
    let state = ledger.call_method1("state", (account_id,))?;
    let venue_order_ids: Vec<String> = broker
        .call_method1("orders", (since,))?
        .try_iter()?
        .map(|order_state| Ok(order_state?.getattr("venue_order_id")?.extract::<String>()?))
        .collect::<PyResult<Vec<String>>>()?;
    for order_id in oms_native::oms_native_orders_to_read(&state, venue_order_ids)? {
        manager.call_method1("reconcile_order", (order_id,))?;
    }
    Ok(recorded)
}

/// `bracket_resync_cause`: the bracket entry and the fill id of its latest fill, or `None`
/// with no fills. An unknown order raises `KeyError` from Rust, as the Python lookup did.
#[pyfunction]
pub(crate) fn bracket_resync_cause(
    ledger: &Bound<'_, PyAny>,
    account_id: &str,
    order_id: &str,
) -> PyResult<Option<(Py<PyAny>, String)>> {
    let state = ledger.call_method1("state", (account_id,))?;
    let found = oms_native::oms_native_resync_cause(ledger, account_id, &state, order_id)?;
    match found {
        None => Ok(None),
        Some((entry_id, fill_id)) => {
            let entry = state.getattr("orders")?.get_item(entry_id)?;
            Ok(Some((entry.unbind(), fill_id)))
        }
    }
}

/// `resync_open_brackets`: re-sync every open bracket with a filled entry, before a run's
/// first reconcile (F2). Returns how many entries were synced.
#[pyfunction]
pub(crate) fn resync_open_brackets(
    ledger: &Bound<'_, PyAny>,
    manager: &Bound<'_, PyAny>,
    account_id: &str,
) -> PyResult<usize> {
    let state = ledger.call_method1("state", (account_id,))?;
    let pairs = oms_native::oms_native_brackets_to_resync(ledger, account_id, &state)?;
    let orders = state.getattr("orders")?;
    for (entry_id, fill_id) in &pairs {
        let entry = orders.get_item(entry_id)?;
        manager.call_method1("synchronize_bracket", (entry, fill_id.clone()))?;
    }
    Ok(pairs.len())
}

/// `pending_request`: which request left `order_id` pending (`submit`, `cancel` or `unknown`).
#[pyfunction]
pub(crate) fn pending_request(
    ledger: &Bound<'_, PyAny>,
    account_id: &str,
    order_id: &str,
) -> PyResult<&'static str> {
    oms_native::oms_native_pending_request(ledger, account_id, order_id)
}

/// `restorable`: working orders plus their brackets, and the fills, built as Python carriers
/// by `oms_native`. A `resolve` resolution is reported back and applied to `resolution`.
#[pyfunction]
#[pyo3(signature = (ledger, account_id, state, pending, resolution=None))]
pub(crate) fn restorable(
    ledger: &Bound<'_, PyAny>,
    account_id: &str,
    state: &Bound<'_, PyAny>,
    pending: &Bound<'_, PyAny>,
    resolution: Option<&Bound<'_, PyAny>>,
) -> PyResult<(Vec<Py<PyAny>>, Vec<Py<PyAny>>)> {
    let pending: &str = match pending.extract::<&str>() {
        Ok(s) => s,
        Err(_) => {
            let repr = pending.repr()?.to_string();
            return Err(PyValueError::new_err(format!(
                "pending must be 'refuse' or 'resolve', got {repr}"
            )));
        }
    };
    let (orders, fills, resolved, unresolved) =
        oms_native::oms_native_restorable(ledger, account_id, state, pending, resolution)?;
    if let Some(resolution) = resolution {
        resolution.getattr("resolved")?.call_method1("extend", (resolved,))?;
        resolution.getattr("unresolved")?.call_method1("update", (unresolved,))?;
    }
    Ok((orders, fills))
}

/// `restorable_positions`: the venue's held positions, in state order, each dated by its
/// latest fill.
#[pyfunction]
pub(crate) fn restorable_positions(state: &Bound<'_, PyAny>) -> PyResult<Vec<Py<PyAny>>> {
    oms_native::oms_native_positions(state)
}

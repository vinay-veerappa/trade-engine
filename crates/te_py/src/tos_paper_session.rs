//! W6-E2: the mirror session's decisions as typed pyfunctions (`session_<op>`). Each takes Python
//! objects (orders, mirror state, datetimes, dates, decimal text, strings) and returns Python
//! objects, converted by `store_ledger`; there is no JSON door. A te_core refusal raises the
//! `ValueError((kind, msg))` the session doors always raised, so `_rs.call` maps it unchanged.

use std::collections::{BTreeSet, HashMap};

use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};
use te_core::ledger::model::{Obj, Order};
use te_core::tos_paper::netting::Ticket;
use te_core::tos_paper::session::{self, AccountBatchData, CreatedBatch, MarkerEvent, WriteAheadItem};
use te_core::tos_paper::wire::state_of;

use crate::store_ledger as sl;
use sl::door_err;

/// A Python sequence of orders, each as the sim carries it.
fn orders_of(value: &Bound<'_, PyAny>) -> PyResult<Vec<Order>> {
    value.try_iter()?.map(|order| sl::order_of(&order?)).collect()
}

/// A Python list of orders, as the codec's Order objects.
fn orders_py<'py>(py: Python<'py>, orders: &[Order]) -> PyResult<Bound<'py, PyAny>> {
    let items = orders.iter().map(|order| sl::obj_py(py, &Obj::Order(order.clone()))).collect::<PyResult<Vec<_>>>()?;
    Ok(PyList::new(py, items)?.into_any())
}

/// One mirrored account's batch input: `(markers, created_events, current_orders)`, where a marker
/// is `(seq, session date, job)` and a created batch is `(seq, orders)`.
fn batch_data(value: &Bound<'_, PyAny>) -> PyResult<AccountBatchData> {
    let (markers, created, current): (Bound<'_, PyAny>, Bound<'_, PyAny>, Bound<'_, PyAny>) = value.extract()?;
    let mut out_markers = Vec::new();
    for marker in markers.try_iter()? {
        let (seq, session, job): (i128, Bound<'_, PyAny>, String) = marker?.extract()?;
        out_markers.push(MarkerEvent { seq, session: sl::date_of(&session)?, job });
    }
    let mut created_events = Vec::new();
    for batch in created.try_iter()? {
        let (seq, orders): (i128, Bound<'_, PyAny>) = batch?.extract()?;
        created_events.push(CreatedBatch { seq, orders: orders_of(&orders)? });
    }
    let mut current_orders = HashMap::new();
    for order in current.try_iter()? {
        let order = sl::order_of(&order?)?;
        current_orders.insert(order.order_id.clone(), order);
    }
    Ok(AccountBatchData { markers: out_markers, created_events, current_orders })
}

fn accounts_data_of(value: &Bound<'_, PyDict>) -> PyResult<HashMap<String, AccountBatchData>> {
    let mut map = HashMap::new();
    for (account, data) in value.iter() {
        map.insert(account.extract::<String>()?, batch_data(&data)?);
    }
    Ok(map)
}

#[pyfunction]
fn session_working_orders<'py>(
    py: Python<'py>,
    mirrored_accounts: Vec<String>,
    account_orders: Bound<'py, PyDict>,
    mirror: Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    let mut by_account = HashMap::new();
    for (account, orders) in account_orders.iter() {
        by_account.insert(account.extract::<String>()?, orders_of(&orders)?);
    }
    let found = session::working_orders(&mirrored_accounts, &by_account, &sl::mirror_state_of(&mirror)?);
    orders_py(py, &found)
}

#[pyfunction]
#[pyo3(signature = (mirrored_accounts, accounts_data, session, close, job, mirror))]
fn session_pending_orders<'py>(
    py: Python<'py>,
    mirrored_accounts: Vec<String>,
    accounts_data: Bound<'py, PyDict>,
    session: Bound<'py, PyAny>,
    close: Bound<'py, PyAny>,
    job: Option<String>,
    mirror: Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    let found = session::pending_orders(
        &mirrored_accounts,
        &accounts_data_of(&accounts_data)?,
        sl::date_of(&session)?,
        &sl::datetime_of(&close)?,
        job.as_deref().filter(|job| !job.is_empty()),
        &sl::mirror_state_of(&mirror)?,
    )
    .map_err(door_err)?;
    orders_py(py, &found)
}

#[pyfunction]
#[pyo3(signature = (mirrored_accounts, accounts_data, session, session_open, job, mirror))]
fn session_morning_orders<'py>(
    py: Python<'py>,
    mirrored_accounts: Vec<String>,
    accounts_data: Bound<'py, PyDict>,
    session: Bound<'py, PyAny>,
    session_open: Bound<'py, PyAny>,
    job: Option<String>,
    mirror: Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    let found = session::morning_orders(
        &mirrored_accounts,
        &accounts_data_of(&accounts_data)?,
        sl::date_of(&session)?,
        &sl::datetime_of(&session_open)?,
        job.as_deref().filter(|job| !job.is_empty()),
        &sl::mirror_state_of(&mirror)?,
    )
    .map_err(door_err)?;
    orders_py(py, &found)
}

#[pyfunction]
fn session_reconcile_command_id(
    venue: String,
    phase: String,
    as_of: Bound<'_, PyAny>,
    event: Bound<'_, PyAny>,
) -> PyResult<String> {
    session::reconcile_command_id(&venue, &phase, &sl::datetime_of(&as_of)?, &sl::reconcile_of(&event)?)
        .map_err(door_err)
}

#[pyfunction]
#[pyo3(signature = (doing, exc, recorded=None))]
fn session_deferred_why(doing: String, exc: String, recorded: Option<String>) -> String {
    session::deferred_why(&doing, &exc, recorded.as_deref())
}

#[pyfunction]
fn session_halt_unproven<'py>(
    py: Python<'py>,
    venue: String,
    phase: String,
    as_of: Bound<'py, PyAny>,
    mirror: Bound<'py, PyAny>,
) -> PyResult<Option<(Bound<'py, PyAny>, String)>> {
    let halt = session::halt_unproven(&venue, &sl::mirror_state_of(&mirror)?, &sl::datetime_of(&as_of)?, &phase)
        .map_err(door_err)?;
    match halt {
        None => Ok(None),
        Some((event, cmd)) => Ok(Some((sl::obj_py(py, &Obj::Reconcile(event))?, cmd))),
    }
}

#[pyfunction]
fn session_collect_items(
    venue: String,
    fills: Vec<(String, String)>,
    closes: Vec<(String, Option<String>)>,
) -> PyResult<Vec<String>> {
    session::collect_items(&venue, &fills, &closes).map_err(door_err)
}

#[pyfunction]
fn session_fold_halt<'py>(
    py: Python<'py>,
    venue: String,
    as_of: Bound<'py, PyAny>,
    fold_err: String,
    mirror: Bound<'py, PyAny>,
    fills: Vec<(String, String)>,
) -> PyResult<Bound<'py, PyAny>> {
    let event = session::fold_halt(
        &venue,
        &sl::datetime_of(&as_of)?,
        &fold_err,
        &sl::mirror_state_of(&mirror)?,
        &fills,
    )
    .map_err(door_err)?;
    sl::obj_py(py, &Obj::Reconcile(event))
}

#[pyfunction]
#[pyo3(signature = (venue, ticket_key, order_id, ack_status, ack_message, ack_timestamp))]
fn session_build_cancel_ack<'py>(
    py: Python<'py>,
    venue: String,
    ticket_key: String,
    order_id: Option<String>,
    ack_status: String,
    ack_message: String,
    ack_timestamp: Bound<'py, PyAny>,
) -> PyResult<Option<(Bound<'py, PyAny>, String)>> {
    let ack = session::build_cancel_ack(
        &venue,
        &ticket_key,
        order_id.as_deref().filter(|id| !id.is_empty()),
        &ack_status,
        &ack_message,
        &sl::datetime_of(&ack_timestamp)?,
    );
    match ack {
        None => Ok(None),
        Some((ack, cmd)) => Ok(Some((sl::obj_py(py, &Obj::MAck(ack))?, cmd))),
    }
}

/// `(todo, waiting, refused)`: the orders to send, then `(order id, why)` for those held and for those refused.
#[pyfunction]
fn session_classify_cover<'py>(
    py: Python<'py>,
    mirror: Bound<'py, PyAny>,
    orders: Bound<'py, PyAny>,
    may_wait: bool,
) -> PyResult<(Bound<'py, PyAny>, Vec<(String, String)>, Vec<(String, String)>)> {
    let mirror = sl::mirror_state_of(&mirror)?;
    let orders = orders_of(&orders)?;
    let candidates = session::filter_candidates(&orders, &mirror);
    let (todo, waiting, refused) = session::classify_cover(&mirror, &candidates, may_wait).map_err(door_err)?;
    Ok((orders_py(py, &todo)?, waiting, refused))
}

/// The write-ahead items, each `(payload, command id)`: refusals and queued tickets, in order.
#[pyfunction]
fn session_prepare_write_ahead<'py>(
    py: Python<'py>,
    venue: String,
    accounts: HashMap<String, String>,
    cover_refused: Vec<(String, String)>,
    batch_refused: Vec<(String, String)>,
    batch_venue_orders: Bound<'py, PyAny>,
    broker_queued_keys: Vec<String>,
    now: Bound<'py, PyAny>,
) -> PyResult<Vec<(Bound<'py, PyAny>, String)>> {
    let tickets: Vec<Ticket> =
        batch_venue_orders.try_iter()?.map(|ticket| sl::venue_order_ticket_of(&ticket?)).collect::<PyResult<_>>()?;
    let queued_keys: BTreeSet<String> = broker_queued_keys.into_iter().collect();
    let items = session::prepare_write_ahead(
        &venue,
        &accounts,
        &cover_refused,
        &batch_refused,
        &tickets,
        &queued_keys,
        &sl::datetime_of(&now)?,
    );
    items
        .into_iter()
        .map(|item| match item {
            WriteAheadItem::Refused(refused, cmd) => Ok((sl::obj_py(py, &Obj::MRefused(refused))?, cmd)),
            WriteAheadItem::Queued(queued, cmd) => Ok((sl::obj_py(py, &Obj::MQueued(queued))?, cmd)),
        })
        .collect()
}

/// The drain's acks, each `(payload, command id)`. `acks` are `(ticket key, status, message, timestamp)`
/// (an empty message is none); `proven` maps a ticket key to `(venue order id, book state text)`.
#[pyfunction]
fn session_build_drain_acks<'py>(
    py: Python<'py>,
    venue: String,
    acks: Vec<(String, String, String, Bound<'py, PyAny>)>,
    proven: HashMap<String, (String, String)>,
) -> PyResult<Vec<(Bound<'py, PyAny>, String)>> {
    let mut rows = Vec::new();
    for (key, status, message, timestamp) in acks {
        rows.push((key, status, (!message.is_empty()).then_some(message), sl::datetime_of(&timestamp)?));
    }
    let mut proven_states = HashMap::new();
    for (key, (order_id, book_state)) in proven {
        proven_states.insert(key, (order_id, state_of(&book_state).map_err(door_err)?));
    }
    session::build_drain_acks(&venue, &rows, &proven_states)
        .into_iter()
        .map(|(ack, cmd)| Ok((sl::obj_py(py, &Obj::MAck(ack))?, cmd)))
        .collect()
}

/// `(holders, events)`: the holders cleared, then `(account, payload, command id)` for each cleared halt.
#[pyfunction]
fn session_clear_halt<'py>(
    py: Python<'py>,
    venue: String,
    reason: String,
    as_of: Bound<'py, PyAny>,
    holders: Vec<String>,
    reconciles: Vec<(i128, Bound<'py, PyAny>)>,
) -> PyResult<(Vec<String>, Vec<(String, Bound<'py, PyAny>, String)>)> {
    let mut pairs = Vec::new();
    for (seq, event) in reconciles {
        pairs.push((seq, sl::reconcile_of(&event)?));
    }
    let plan = session::clear_halt(&venue, &reason, &sl::datetime_of(&as_of)?, &holders, &pairs).map_err(door_err)?;
    let mut events = Vec::new();
    for (account, event, cmd) in plan.events {
        events.push((account, sl::obj_py(py, &Obj::HaltCleared(event))?, cmd));
    }
    Ok((plan.holders, events))
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(session_working_orders, m)?)?;
    m.add_function(wrap_pyfunction!(session_pending_orders, m)?)?;
    m.add_function(wrap_pyfunction!(session_morning_orders, m)?)?;
    m.add_function(wrap_pyfunction!(session_reconcile_command_id, m)?)?;
    m.add_function(wrap_pyfunction!(session_deferred_why, m)?)?;
    m.add_function(wrap_pyfunction!(session_halt_unproven, m)?)?;
    m.add_function(wrap_pyfunction!(session_collect_items, m)?)?;
    m.add_function(wrap_pyfunction!(session_fold_halt, m)?)?;
    m.add_function(wrap_pyfunction!(session_build_cancel_ack, m)?)?;
    m.add_function(wrap_pyfunction!(session_classify_cover, m)?)?;
    m.add_function(wrap_pyfunction!(session_prepare_write_ahead, m)?)?;
    m.add_function(wrap_pyfunction!(session_build_drain_acks, m)?)?;
    m.add_function(wrap_pyfunction!(session_clear_halt, m)?)?;
    Ok(())
}

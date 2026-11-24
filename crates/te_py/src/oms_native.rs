//! W6-D2: `restore.py` and `reconcile.py` read the ledger natively. The ledger reads the OMS
//! restore/reconcile rules need (`ORDER_PENDING` events, `FILL` events, a command's event)
//! run against a plain `Ledger`'s store, and the account state is read from the Python state
//! object in place (a `LedgerFold`) or as canonical text (the fallback), never re-folded from
//! the store. The carriers (`VenueOrder`, `VenueOrderAllocation`, `OrderState`, `VenueFill`,
//! `VenuePosition`) are built by calling the Python classes from Rust, with exactly the field
//! values `restore.py` used.
//!
//! Plain values cross; a refusal crosses as `ValueError((kind, message))` so `_rs.call` maps it
//! to the exception `restore.py` / `reconcile.py` registered. An exception a Python call raised
//! is held as a host error and re-raised as itself.

use std::collections::HashMap;

use pyo3::prelude::*;
use pyo3::types::{PyDict, PyTuple};
use te_core::ledger::bridge as lb;
use te_core::ledger::codec::jenum;
use te_core::ledger::fold::AccountState;
use te_core::ledger::json::{dumps, Json};
use te_core::ledger::model::{
    err, Event, EventKind, Fill, LErr, Obj, Order, OrderState, R as LR,
};
use te_core::money::Money;
use te_core::oms::reconcile as rc;
use te_core::oms::restore as rs;

use crate::sim::Host;
use crate::store::LedgerStore;
use crate::store_ledger as sl;
use crate::LedgerFold;

fn fill(text: &str) -> LR<Fill> {
    match lb::obj_from_text(text)? {
        Obj::Fill(f) => Ok(f),
        _ => err("value", "expected a fill"),
    }
}

/// `json.dumps` of the top-level object without sorting: the payload keeps its order.
fn ordered(j: &Json) -> String {
    match j {
        Json::Obj(pairs) => {
            let items: Vec<String> =
                pairs.iter().map(|(k, v)| format!("{}:{}", dumps(&Json::Str(k.clone())), ordered(v))).collect();
            format!("{{{}}}", items.join(","))
        }
        other => dumps(other),
    }
}

/// A Python `Enum` member, as the codec builds it (`{"e": name, "v": value}`).
fn enum_py<'py>(py: Python<'py>, name: &str, value: &str) -> PyResult<Bound<'py, PyAny>> {
    py.import("trade_engine.ledger.codec")?.call_method1("build_text", (dumps(&jenum(name, value)),))
}

/// `MIN_TIME` (`datetime.min.replace(tzinfo=timezone.utc)`), as `restore.py` defines it.
fn min_time<'py>(py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
    let dt = py.import("datetime")?;
    let min = dt.getattr("datetime")?.getattr("min")?;
    let utc = dt.getattr("timezone")?.getattr("utc")?;
    let kwargs = PyDict::new(py);
    kwargs.set_item("tzinfo", utc)?;
    min.call_method("replace", (), Some(&kwargs))
}

fn opt_money<'py>(py: Python<'py>, v: &Option<Money>) -> PyResult<Bound<'py, PyAny>> {
    match v {
        Some(d) => sl::decimal_py(py, d),
        None => Ok(py.None().into_bound(py)),
    }
}

fn opt_str<'py>(py: Python<'py>, v: &Option<String>) -> Bound<'py, PyAny> {
    match v {
        Some(s) => s.clone().into_pyobject(py).expect("a str").into_any(),
        None => py.None().into_bound(py),
    }
}

/// The account state behind a Python state object: a `LedgerFold` read in place (named by
/// `account`), or canonical text otherwise. Never re-folded from the store: callers may pass a
/// state that is not the store's current fold.
pub(crate) fn state_of<'py>(py_state: &Bound<'py, PyAny>, account: Option<&str>, host: &Host) -> LR<AccountState> {
    if let Some(account) = account {
        if let Ok(fold) = py_state.downcast::<LedgerFold>() {
            return match fold.borrow().state_of(account) {
                Some(st) => Ok(st.clone()),
                None => err("key", format!("'{account}'")),
            };
        }
    }
    let py = py_state.py();
    let codec = py.import("trade_engine.ledger.codec").map_err(|e| host.fail(e))?;
    let canon = codec.call_method1("canon", (py_state,)).map_err(|e| host.fail(e))?;
    let text: String = codec
        .call_method1("text", (&canon,))
        .map_err(|e| host.fail(e))?
        .extract()
        .map_err(|e| host.fail(e))?;
    lb::account_from_text(&text)
}

/// The account's `ORDER_PENDING` events as `PendingEvent`s, in append order.
fn pending_events(py: Python<'_>, store: &LedgerStore, account: &str, host: &Host) -> LR<Vec<rs::PendingEvent>> {
    let mut out = Vec::new();
    for event in store.kind_events(py, EventKind::OrderPending.value()).map_err(|e| host.fail(e))? {
        if event.account != account {
            continue;
        }
        let Obj::StateChange(sc) = &event.payload else {
            continue;
        };
        out.push(rs::PendingEvent {
            order_id: sc.order_id.clone(),
            command_id: event.command_id.clone(),
            reason: sc.reason.clone().unwrap_or_else(|| "None".to_string()),
        });
    }
    Ok(out)
}

/// The account's `FILL` events as `FillRef`s, in ledger-seq (append) order.
fn fill_refs(py: Python<'_>, store: &LedgerStore, account: &str, host: &Host) -> LR<Vec<rc::FillRef>> {
    let mut out = Vec::new();
    for event in store.kind_events(py, EventKind::Fill.value()).map_err(|e| host.fail(e))? {
        if event.account != account {
            continue;
        }
        let Obj::Fill(f) = &event.payload else {
            continue;
        };
        let Some(seq) = event.seq else {
            continue;
        };
        out.push(rc::FillRef { seq: seq as i64, order_id: f.order_id.clone(), fill_id: f.fill_id.clone() });
    }
    Ok(out)
}

/// A `VenueOrder` from the fold's `Order`, its venue id and the submission event's timestamp.
fn build_venue_order<'py>(
    py: Python<'py>,
    broker: &Bound<'py, PyAny>,
    order: &Order,
    venue_order_id: &str,
    submitted_at: &Bound<'py, PyAny>,
    host: &Host,
) -> LR<Bound<'py, PyAny>> {
    let cls = broker.getattr("VenueOrder").map_err(|e| host.fail(e))?;
    let alloc_cls = broker.getattr("VenueOrderAllocation").map_err(|e| host.fail(e))?;
    let kwargs = PyDict::new(py);
    kwargs.set_item("venue_order_id", venue_order_id).map_err(|e| host.fail(e))?;
    kwargs.set_item("instrument", sl::instrument_py(py, &order.instrument).map_err(|e| host.fail(e))?).map_err(|e| host.fail(e))?;
    kwargs.set_item("order_type", enum_py(py, "OrderType", order.order_type.value()).map_err(|e| host.fail(e))?).map_err(|e| host.fail(e))?;
    kwargs.set_item("side", sl::side_py(py, order.side).map_err(|e| host.fail(e))?).map_err(|e| host.fail(e))?;
    kwargs.set_item("quantity", sl::decimal_py(py, &order.quantity).map_err(|e| host.fail(e))?).map_err(|e| host.fail(e))?;
    kwargs.set_item("submitted_at", submitted_at).map_err(|e| host.fail(e))?;
    kwargs.set_item("tif", enum_py(py, "TimeInForce", order.tif.value()).map_err(|e| host.fail(e))?).map_err(|e| host.fail(e))?;
    kwargs.set_item("limit_price", opt_money(py, &order.limit_price).map_err(|e| host.fail(e))?).map_err(|e| host.fail(e))?;
    kwargs.set_item("stop_price", opt_money(py, &order.stop_price).map_err(|e| host.fail(e))?).map_err(|e| host.fail(e))?;
    kwargs.set_item("trail_amount", opt_money(py, &order.trail_amount).map_err(|e| host.fail(e))?).map_err(|e| host.fail(e))?;
    let alloc = alloc_cls
        .call((order.order_id.clone(), order.account_id.clone(), sl::decimal_py(py, &order.quantity).map_err(|e| host.fail(e))?), None)
        .map_err(|e| host.fail(e))?;
    let allocations = PyTuple::new(py, [alloc]).map_err(|e| host.fail(e))?;
    kwargs.set_item("allocations", allocations).map_err(|e| host.fail(e))?;
    kwargs.set_item("parent_order_id", opt_str(py, &order.parent_order_id)).map_err(|e| host.fail(e))?;
    kwargs.set_item("oco_group", opt_str(py, &order.oco_group)).map_err(|e| host.fail(e))?;
    cls.call((), Some(&kwargs)).map_err(|e| host.fail(e))
}

/// A `VenueFill` from the fold's `Fill`, its venue fill id and venue order id.
fn build_venue_fill<'py>(
    py: Python<'py>,
    broker: &Bound<'py, PyAny>,
    fill: &Fill,
    venue_fill_id: &str,
    venue_order_id: &str,
    host: &Host,
) -> LR<Bound<'py, PyAny>> {
    let cls = broker.getattr("VenueFill").map_err(|e| host.fail(e))?;
    let kwargs = PyDict::new(py);
    kwargs.set_item("venue_fill_id", venue_fill_id).map_err(|e| host.fail(e))?;
    kwargs.set_item("venue_order_id", venue_order_id).map_err(|e| host.fail(e))?;
    kwargs.set_item("instrument", sl::instrument_py(py, &fill.instrument).map_err(|e| host.fail(e))?).map_err(|e| host.fail(e))?;
    kwargs.set_item("quantity", sl::decimal_py(py, &fill.quantity).map_err(|e| host.fail(e))?).map_err(|e| host.fail(e))?;
    kwargs.set_item("price", sl::decimal_py(py, &fill.price).map_err(|e| host.fail(e))?).map_err(|e| host.fail(e))?;
    kwargs.set_item("filled_at", sl::datetime_py(py, &fill.filled_at).map_err(|e| host.fail(e))?).map_err(|e| host.fail(e))?;
    kwargs.set_item("side", sl::side_py(py, fill.side).map_err(|e| host.fail(e))?).map_err(|e| host.fail(e))?;
    kwargs.set_item("fee", sl::decimal_py(py, &fill.fee).map_err(|e| host.fail(e))?).map_err(|e| host.fail(e))?;
    kwargs.set_item("leg_id", opt_str(py, &fill.leg_id)).map_err(|e| host.fail(e))?;
    cls.call((), Some(&kwargs)).map_err(|e| host.fail(e))
}

/// The `VenuePosition`s of a folded state, in the state's order, each dated by its latest fill
/// (`MIN_TIME` when it has none).
pub(crate) fn build_positions<'py>(py: Python<'py>, st: &AccountState, host: &Host) -> LR<Vec<Py<PyAny>>> {
    let broker = py.import("trade_engine.interfaces.broker").map_err(|e| host.fail(e))?;
    let cls = broker.getattr("VenuePosition").map_err(|e| host.fail(e))?;
    let min = min_time(py).map_err(|e| host.fail(e))?;
    let plan = rs::positions_plan(st)?;
    let mut out = Vec::new();
    for (index, latest) in plan {
        let (instrument, position) = st.positions.iter().nth(index).expect("positions_plan index in range");
        let as_of = match latest {
            None => min.clone(),
            Some(i) => sl::datetime_py(py, &st.fills[i].filled_at).map_err(|e| host.fail(e))?,
        };
        let kwargs = PyDict::new(py);
        kwargs.set_item("instrument", sl::instrument_py(py, instrument).map_err(|e| host.fail(e))?).map_err(|e| host.fail(e))?;
        kwargs.set_item("quantity", sl::decimal_py(py, &position.quantity).map_err(|e| host.fail(e))?).map_err(|e| host.fail(e))?;
        kwargs.set_item("avg_price", sl::decimal_py(py, &position.avg_cost).map_err(|e| host.fail(e))?).map_err(|e| host.fail(e))?;
        kwargs.set_item("as_of", as_of).map_err(|e| host.fail(e))?;
        out.push(cls.call((), Some(&kwargs)).map_err(|e| host.fail(e))?.unbind());
    }
    Ok(out)
}

/// What `restorable` keeps, built as Python carriers, plus the resolution the resolve mode
/// reports (the caller applies it to its `PendingResolution`).
pub(crate) struct RestoreOutcome {
    pub(crate) orders: Vec<Py<PyAny>>,
    pub(crate) fills: Vec<Py<PyAny>>,
    pub(crate) resolved: Vec<String>,
    pub(crate) unresolved: Vec<(String, String)>,
}

/// The shared restore: classify the pending orders, read each submission event natively, and
/// build the venue carriers. The `note` callback's `(resolved, unresolved)` is returned so the
/// caller applies it, keeping the order of the ledger reads (`ORDER_PENDING`, then the
/// submission events) unchanged.
pub(crate) fn restore_native(
    py: Python<'_>,
    store: &LedgerStore,
    account_id: &str,
    st: &AccountState,
    pending: &str,
    has_resolution: bool,
    pre_unresolved: &[String],
    host: &Host,
) -> LR<RestoreOutcome> {
    let mut events: Vec<rs::PendingEvent> = Vec::new();
    if pending == "resolve" && has_resolution && st.orders.values().any(|o| o.state == OrderState::PendingUnknown) {
        events = pending_events(py, store, account_id, host)?;
    }

    let mut resolved: Vec<String> = Vec::new();
    let mut unresolved: Vec<(String, String)> = Vec::new();
    let mut found: HashMap<String, Event> = HashMap::new();

    let mut on_note = |r: &[String], u: &[(String, String)]| -> LR<()> {
        resolved.extend_from_slice(r);
        unresolved.extend_from_slice(u);
        Ok(())
    };
    let mut on_submitted = |command: &str| -> LR<bool> {
        let event = store.command_event(py, command).map_err(|e| host.fail(e))?;
        if let Some(ev) = &event {
            found.insert(command.to_string(), ev.clone());
        }
        Ok(event.is_some())
    };
    let mut cb = rs::Callbacks { note: &mut on_note, submitted: &mut on_submitted };
    let plan = rs::restorable(st, account_id, pending, has_resolution, pre_unresolved, &events, &mut cb)?;

    let broker = py.import("trade_engine.interfaces.broker").map_err(|e| host.fail(e))?;
    let mut orders = Vec::new();
    for (order_id, venue_order_id, venue_state) in &plan.orders {
        let order = st.orders.get(order_id).ok_or_else(|| LErr { kind: "key", msg: format!("'{order_id}'") })?;
        let submitted = found
            .get(&format!("{}:submit", order.command_id))
            .ok_or_else(|| {
                LErr {
                    kind: "restore",
                    msg: format!(
                        "Order '{order_id}' for '{account_id}' has no submission event; cannot restore when it reached the venue (I5)"
                    ),
                }
            })?;
        let submitted_at = sl::datetime_py(py, &submitted.ts_utc).map_err(|e| host.fail(e))?;
        let venue_order = build_venue_order(py, &broker, order, venue_order_id, &submitted_at, host)?;
        let state_obj = enum_py(py, "OrderState", venue_state).map_err(|e| host.fail(e))?;
        let pair = PyTuple::new(py, [venue_order, state_obj]).map_err(|e| host.fail(e))?;
        orders.push(pair.into_any().unbind());
    }

    let mut fills = Vec::new();
    for (index, venue_fill_id, venue_order_id) in &plan.fills {
        let fill = &st.fills[*index];
        let venue_fill = build_venue_fill(py, &broker, fill, venue_fill_id, venue_order_id, host)?;
        fills.push(venue_fill.unbind());
    }

    Ok(RestoreOutcome { orders, fills, resolved, unresolved })
}

/// The intraday gate's refusal for a native restore: a `restore` refusal becomes
/// `IntradayServiceError` with the `RestoreError` as its cause (the `intraday_restore`
/// mapping); a held Python error crosses as itself; anything else maps as `flow_refusal`.
pub(crate) fn intraday_restore_refusal(py: Python<'_>, e: LErr, host: &Host) -> PyErr {
    if e.kind == "host" {
        return host.take();
    }
    if e.kind == "restore" {
        let cause = match py
            .import("trade_engine.oms.restore")
            .and_then(|m| m.getattr("RestoreError"))
            .and_then(|c| c.call1((&e.msg,)))
        {
            Ok(v) => PyErr::from_value(v),
            Err(err) => err,
        };
        let refused = match py
            .import("trade_engine.intraday.service")
            .and_then(|m| m.getattr("IntradayServiceError"))
            .and_then(|c| c.call1((&e.msg,)))
        {
            Ok(v) => PyErr::from_value(v),
            Err(err) => err,
        };
        refused.set_cause(py, Some(cause));
        return refused;
    }
    crate::runtime::flow_refusal(e)
}

// --- the pyfunctions -------------------------------------------------------------------

#[pyfunction]
#[pyo3(signature = (ledger, account_id, state, pending, resolution=None))]
pub(crate) fn oms_native_restorable(
    ledger: &Bound<'_, PyAny>,
    account_id: &str,
    state: &Bound<'_, PyAny>,
    pending: &str,
    resolution: Option<&Bound<'_, PyAny>>,
) -> PyResult<(Vec<Py<PyAny>>, Vec<Py<PyAny>>, Vec<String>, Vec<(String, String)>)> {
    let host = Host::new();
    let store = sl::ledger_store(ledger)?;
    let guard = store.borrow();
    let py = ledger.py();
    let has_resolution = resolution.is_some();
    let mut pre_unresolved: Vec<String> = Vec::new();
    if let Some(res) = resolution {
        let unresolved = res.getattr("unresolved")?;
        for key in unresolved.call_method0("keys")?.try_iter()? {
            pre_unresolved.push(key?.extract()?);
        }
    }
    let r = (|| -> LR<RestoreOutcome> {
        let st = state_of(state, Some(account_id), &host)?;
        restore_native(py, &*guard, account_id, &st, pending, has_resolution, &pre_unresolved, &host)
    })();
    let outcome = host.finish(r)?;
    Ok((outcome.orders, outcome.fills, outcome.resolved, outcome.unresolved))
}

#[pyfunction]
pub(crate) fn oms_native_positions(state: &Bound<'_, PyAny>) -> PyResult<Vec<Py<PyAny>>> {
    let host = Host::new();
    let py = state.py();
    let r = (|| -> LR<Vec<Py<PyAny>>> {
        let st = state_of(state, None, &host)?;
        build_positions(py, &st, &host)
    })();
    host.finish(r)
}

#[pyfunction]
pub(crate) fn oms_native_pending_request(
    ledger: &Bound<'_, PyAny>,
    account_id: &str,
    order_id: &str,
) -> PyResult<&'static str> {
    let host = Host::new();
    let store = sl::ledger_store(ledger)?;
    let guard = store.borrow();
    let py = ledger.py();
    let r = (|| -> LR<&'static str> {
        let events = pending_events(py, &*guard, account_id, &host)?;
        Ok(rs::pending_request(&events, order_id))
    })();
    host.finish(r)
}

#[pyfunction]
pub(crate) fn oms_native_heal_plan(
    ledger: &Bound<'_, PyAny>,
    account_id: &str,
    state: &Bound<'_, PyAny>,
    fill_id: &str,
    order_id: &str,
) -> PyResult<(Option<(String, String)>, bool, Option<i64>)> {
    let host = Host::new();
    let store = sl::ledger_store(ledger)?;
    let guard = store.borrow();
    let py = ledger.py();
    let r = (|| -> LR<(Option<(String, String)>, bool, Option<i64>)> {
        let st = state_of(state, Some(account_id), &host)?;
        let fills = fill_refs(py, &*guard, account_id, &host)?;
        let fill_event = guard.command_event(py, &format!("fill:{fill_id}")).map_err(|e| host.fail(e))?;
        let fill_seq = fill_event.as_ref().and_then(|e| e.seq).map(|s| s as i64);
        let plan = rc::heal_plan(&st, &fills, fill_id, order_id, fill_seq)?;
        Ok((plan.resync, plan.rejournal_candidate, fill_seq))
    })();
    host.finish(r)
}

#[pyfunction]
pub(crate) fn oms_native_resync_cause(
    ledger: &Bound<'_, PyAny>,
    account_id: &str,
    state: &Bound<'_, PyAny>,
    order_id: &str,
) -> PyResult<Option<(String, String)>> {
    let host = Host::new();
    let store = sl::ledger_store(ledger)?;
    let guard = store.borrow();
    let py = ledger.py();
    let r = (|| -> LR<Option<(String, String)>> {
        let st = state_of(state, Some(account_id), &host)?;
        let fills = fill_refs(py, &*guard, account_id, &host)?;
        rc::bracket_resync_cause(&st, &fills, order_id)
    })();
    host.finish(r)
}

#[pyfunction]
pub(crate) fn oms_native_brackets_to_resync(
    ledger: &Bound<'_, PyAny>,
    account_id: &str,
    state: &Bound<'_, PyAny>,
) -> PyResult<Vec<(String, String)>> {
    let host = Host::new();
    let store = sl::ledger_store(ledger)?;
    let guard = store.borrow();
    let py = ledger.py();
    let r = (|| -> LR<Vec<(String, String)>> {
        let st = state_of(state, Some(account_id), &host)?;
        let fills = fill_refs(py, &*guard, account_id, &host)?;
        Ok(rc::brackets_to_resync(&st, &fills))
    })();
    host.finish(r)
}

#[pyfunction]
pub(crate) fn oms_native_orders_to_read(state: &Bound<'_, PyAny>, venue_order_ids: Vec<String>) -> PyResult<Vec<String>> {
    let host = Host::new();
    let r = (|| -> LR<Vec<String>> {
        let st = state_of(state, None, &host)?;
        Ok(rc::orders_to_read(&st, &venue_order_ids))
    })();
    host.finish(r)
}

#[pyfunction]
pub(crate) fn oms_native_check_known_order(
    state: &Bound<'_, PyAny>,
    fill_id: &str,
    venue_order_id: &str,
    account_id: &str,
) -> PyResult<()> {
    let host = Host::new();
    let r = (|| -> LR<()> {
        let st = state_of(state, Some(account_id), &host)?;
        rc::check_known_order(&st, fill_id, venue_order_id, account_id)
    })();
    host.finish(r)
}

#[pyfunction]
pub(crate) fn oms_native_journal_payload(
    ledger: &Bound<'_, PyAny>,
    account_id: &str,
    state: &Bound<'_, PyAny>,
    fill_tree: &str,
    journal_account: &str,
) -> PyResult<(String, Option<i64>)> {
    let host = Host::new();
    let store = sl::ledger_store(ledger)?;
    let guard = store.borrow();
    let py = ledger.py();
    let r = (|| -> LR<(String, Option<i64>)> {
        let st = state_of(state, Some(account_id), &host)?;
        let f = fill(fill_tree)?;
        let event = guard.command_event(py, &format!("fill:{}", f.fill_id)).map_err(|e| host.fail(e))?;
        let seq = event.as_ref().and_then(|e| e.seq).map(|s| s as i64);
        let has_seq = seq.is_some();
        let payload = ordered(&rc::journal_payload(&st, &f, journal_account, has_seq)?);
        Ok((payload, seq))
    })();
    host.finish(r)
}

pub(crate) fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(oms_native_restorable, m)?)?;
    m.add_function(wrap_pyfunction!(oms_native_positions, m)?)?;
    m.add_function(wrap_pyfunction!(oms_native_pending_request, m)?)?;
    m.add_function(wrap_pyfunction!(oms_native_heal_plan, m)?)?;
    m.add_function(wrap_pyfunction!(oms_native_resync_cause, m)?)?;
    m.add_function(wrap_pyfunction!(oms_native_brackets_to_resync, m)?)?;
    m.add_function(wrap_pyfunction!(oms_native_orders_to_read, m)?)?;
    m.add_function(wrap_pyfunction!(oms_native_check_known_order, m)?)?;
    m.add_function(wrap_pyfunction!(oms_native_journal_payload, m)?)?;
    Ok(())
}

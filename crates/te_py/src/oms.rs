//! P3b-1: the OMS decisions of `reconcile.py`, `restore.py` and `oms/options.py`
//! (`te_core::oms::{reconcile, restore, options}`) as the Python shims reach them. Plain
//! values in and out; a refusal crosses as `ValueError((kind, message))`; an exception a
//! host callback raised (a ledger read) is re-raised as itself.

use pyo3::prelude::*;
use te_core::ledger::bridge as lb;
use te_core::ledger::json::{dumps, Json};
use te_core::ledger::model::{err, Fill, Instrument, Obj, Side, R as LR};
use te_core::oms::options as oo;
use te_core::oms::reconcile as rc;
use te_core::oms::restore as rs;

use crate::sim::{dec, instrument, refuse, Host};

fn fill(text: &str) -> LR<Fill> {
    match lb::obj_from_text(text)? {
        Obj::Fill(f) => Ok(f),
        _ => err("value", "expected a fill"),
    }
}

fn side(s: &str) -> LR<Side> {
    match Side::parse(s) {
        Some(v) => Ok(v),
        None => err("value", format!("bad side {s:?}")),
    }
}

// --- reconcile --------------------------------------------------------------------------

/// The venue orders to re-read, in the venue's order.
#[pyfunction]
pub(crate) fn oms_orders_to_read(state: &str, venue_order_ids: Vec<String>) -> PyResult<Vec<String>> {
    let r = || -> LR<Vec<String>> {
        let st = lb::account_from_text(state)?;
        Ok(rc::orders_to_read(&st, &venue_order_ids))
    };
    r().map_err(refuse)
}

#[pyfunction]
pub(crate) fn oms_check_known_order(state: &str, fill_id: &str, venue_order_id: &str, account_id: &str) -> PyResult<()> {
    let r = || -> LR<()> {
        let st = lb::account_from_text(state)?;
        rc::check_known_order(&st, fill_id, venue_order_id, account_id)
    };
    r().map_err(refuse)
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

/// The journal execution as JSON text.
#[pyfunction]
pub(crate) fn oms_journal_payload(state: &str, fill_tree: &str, journal_account: &str, has_seq: bool) -> PyResult<String> {
    let r = || -> LR<String> {
        let st = lb::account_from_text(state)?;
        let f = fill(fill_tree)?;
        Ok(ordered(&rc::journal_payload(&st, &f, journal_account, has_seq)?))
    };
    r().map_err(refuse)
}

// --- restore ----------------------------------------------------------------------------

type PendingT = (String, Option<String>, String);

fn events(raw: Vec<PendingT>) -> Vec<rs::PendingEvent> {
    raw.into_iter().map(|(order_id, command_id, reason)| rs::PendingEvent { order_id, command_id, reason }).collect()
}

/// `submit`, `cancel` or `unknown`, from the account's ORDER_PENDING events.
#[pyfunction]
pub(crate) fn oms_pending_request(raw: Vec<PendingT>, order_id: &str) -> &'static str {
    rs::pending_request(&events(raw), order_id)
}

type RestoredT = (Vec<(String, String, String)>, Vec<(usize, String, String)>);

#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub(crate) fn oms_restorable(
    state: &str,
    account_id: &str,
    pending: &str,
    has_resolution: bool,
    pre_unresolved: Vec<String>,
    raw: Vec<PendingT>,
    note: &Bound<'_, PyAny>,
    submitted: &Bound<'_, PyAny>,
) -> PyResult<RestoredT> {
    let host = Host::new();
    let r = (|| -> LR<RestoredT> {
        let st = lb::account_from_text(state)?;
        let mut on_note = |resolved: &[String], unresolved: &[(String, String)]| -> LR<()> {
            note.call1((resolved.to_vec(), unresolved.to_vec())).map(|_| ()).map_err(|e| host.fail(e))
        };
        let mut on_submitted = |command: &str| -> LR<bool> {
            submitted.call1((command,)).and_then(|v| v.extract::<bool>()).map_err(|e| host.fail(e))
        };
        let mut cb = rs::Callbacks { note: &mut on_note, submitted: &mut on_submitted };
        let plan = rs::restorable(&st, account_id, pending, has_resolution, &pre_unresolved, &events(raw), &mut cb)?;
        Ok((plan.orders.into_iter().map(|(a, b, c)| (a, b, c.to_string())).collect(), plan.fills))
    })();
    host.finish(r)
}

#[pyfunction]
pub(crate) fn oms_restorable_positions(state: &str) -> PyResult<Vec<(usize, Option<usize>)>> {
    let r = || -> LR<Vec<(usize, Option<usize>)>> {
        let st = lb::account_from_text(state)?;
        rs::positions_plan(&st)
    };
    r().map_err(refuse)
}

// --- options ----------------------------------------------------------------------------

#[pyfunction]
#[pyo3(signature = (intent_id, account_id, instrument_tree, side, quantity, order_type, limit_price, tif, profit_target, reason))]
#[allow(clippy::too_many_arguments)]
pub(crate) fn oms_intent_fingerprint(
    intent_id: &str,
    account_id: &str,
    instrument_tree: &str,
    side: &str,
    quantity: &str,
    order_type: &str,
    limit_price: Option<&str>,
    tif: &str,
    profit_target: Option<&str>,
    reason: &str,
) -> PyResult<String> {
    oo::intent_fingerprint(&oo::IntentTerms {
        intent_id,
        account_id,
        instrument: instrument_tree,
        side,
        quantity,
        order_type,
        limit_price,
        tif,
        profit_target,
        reason,
    })
    .map_err(refuse)
}

#[pyfunction]
pub(crate) fn oms_order_fingerprint(order_tree: &str) -> PyResult<String> {
    oo::order_fingerprint(order_tree).map_err(refuse)
}

#[pyfunction]
pub(crate) fn oms_replay_conflict(command_id: &str, orders_created: bool, same_account: bool) -> PyResult<()> {
    oo::replay_conflict(command_id, orders_created, same_account).map_err(refuse)
}

#[pyfunction]
pub(crate) fn oms_fingerprint_conflict(command_id: &str, existing: &str, expected: &str) -> PyResult<()> {
    oo::fingerprint_conflict(command_id, existing, expected).map_err(refuse)
}

#[pyfunction]
#[pyo3(signature = (command_id, closed, entry_order_id))]
pub(crate) fn oms_close_target_conflict(command_id: &str, closed: Option<&str>, entry_order_id: &str) -> PyResult<()> {
    oo::close_target_conflict(command_id, closed, entry_order_id).map_err(refuse)
}

#[pyfunction]
pub(crate) fn oms_plan_open(
    state: &str,
    account_id: &str,
    instrument_tree: &str,
    side_value: &str,
    quantity: &str,
) -> PyResult<()> {
    let r = || -> LR<()> {
        let st = lb::account_from_text(state)?;
        let i: Instrument = instrument(instrument_tree)?;
        oo::plan_open(&st, account_id, &i, side(side_value)?, &dec(quantity)?)
    };
    r().map_err(refuse)
}

type CloseT = (String, String, Vec<usize>, String, String, String, Option<String>);

/// `(order_id, mode, open leg indices, side, quantity, oco_group, target_order_id)`.
#[pyfunction]
pub(crate) fn oms_plan_close(state: &str, account_id: &str, entry_order_id: &str) -> PyResult<CloseT> {
    let r = || -> LR<CloseT> {
        let st = lb::account_from_text(state)?;
        let p = oo::plan_close(&st, account_id, entry_order_id)?;
        Ok((
            p.order_id,
            p.mode.to_string(),
            p.legs,
            p.side.value().to_string(),
            p.quantity.to_py_string(),
            p.oco_group,
            p.target_order_id,
        ))
    };
    r().map_err(refuse)
}

/// The holding close's side, or a refusal.
#[pyfunction]
pub(crate) fn oms_plan_holding(
    state: &str,
    account_id: &str,
    instrument_tree: &str,
    quantity: &str,
    command_id: &str,
) -> PyResult<String> {
    let r = || -> LR<String> {
        let st = lb::account_from_text(state)?;
        let i = instrument(instrument_tree)?;
        Ok(oo::plan_holding(&st, account_id, &i, &dec(quantity)?, command_id)?.value().to_string())
    };
    r().map_err(refuse)
}

/// `[(child_order_id, action or None)]` in `sync`'s order.
#[pyfunction]
pub(crate) fn oms_sync_plan(state: &str) -> PyResult<Vec<(String, Option<String>)>> {
    let r = || -> LR<Vec<(String, Option<String>)>> {
        let st = lb::account_from_text(state)?;
        Ok(oo::sync_plan(&st)?.into_iter().map(|(id, a)| (id, a.map(|x| x.name().to_string()))).collect())
    };
    r().map_err(refuse)
}

pub(crate) fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(oms_manager_decide, m)?)?;
    m.add_function(wrap_pyfunction!(crate::flow::oms_flow, m)?)?;
    m.add_function(wrap_pyfunction!(oms_orders_to_read, m)?)?;
    m.add_function(wrap_pyfunction!(oms_check_known_order, m)?)?;
    m.add_function(wrap_pyfunction!(oms_journal_payload, m)?)?;
    m.add_function(wrap_pyfunction!(oms_pending_request, m)?)?;
    m.add_function(wrap_pyfunction!(oms_restorable, m)?)?;
    m.add_function(wrap_pyfunction!(oms_restorable_positions, m)?)?;
    m.add_function(wrap_pyfunction!(oms_intent_fingerprint, m)?)?;
    m.add_function(wrap_pyfunction!(oms_order_fingerprint, m)?)?;
    m.add_function(wrap_pyfunction!(oms_replay_conflict, m)?)?;
    m.add_function(wrap_pyfunction!(oms_fingerprint_conflict, m)?)?;
    m.add_function(wrap_pyfunction!(oms_close_target_conflict, m)?)?;
    m.add_function(wrap_pyfunction!(oms_plan_open, m)?)?;
    m.add_function(wrap_pyfunction!(oms_plan_close, m)?)?;
    m.add_function(wrap_pyfunction!(oms_plan_holding, m)?)?;
    m.add_function(wrap_pyfunction!(oms_sync_plan, m)?)?;
    Ok(())
}

#[pyfunction]
pub(crate) fn oms_manager_decide(operation: &str, request: &str) -> PyResult<String> {
    te_core::oms::manager::decide(operation, request).map_err(refuse)
}

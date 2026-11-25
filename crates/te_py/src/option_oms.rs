//! W7-CORE-C7: `OptionOrderManager`'s sequencing moves into te_py (`OptionOrderManagerBase`).
//!
//! `OptionOrderManager` (a plain class, not an `OrderManager` subclass) wraps an inner
//! `OrderManager`, validates option structures and margin through the te_core option rules
//! (`te_core::oms::options`), and routes legs. The class bodies below do the same sequencing
//! step for step and in the same order as `oms/options.py` did.
//!
//! Every collaborator (the inner `OrderManager`, the clock, the ledger) stays a Python object
//! and is reached through attribute lookup on the instance (`slf.getattr(...)` then
//! `call_method*`); the `OrderManager` is ported by a parallel ticket, so its concrete type is
//! never assumed. The carriers (`Order`, `OrdersCreated`, `Event`, the enums) stay Python and are
//! built or read through the codec, which is the one reader of their shape. Where the old Python
//! called `_rs.rs.<fn>` (te_core), this calls the Rust function directly; a te_core refusal is a
//! `ValueError((kind, message))` that `_rs.call` used to translate, and `option_refusal` does the
//! same translation here (through `trade_engine.sim._rs.refusal`, the one door, so the exception
//! type and message stay exactly as they were).

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyTuple};

/// A module attribute, imported once per call (Python caches the import).
fn import_attr<'py>(py: Python<'py>, module: &str, name: &str) -> PyResult<Bound<'py, PyAny>> {
    py.import(module)?.getattr(name)
}

/// `codec.text(codec.canon(state))` — the folded state's canonical JSON text the option rules read.
fn state_text<'py>(py: Python<'py>, state: &Bound<'py, PyAny>) -> PyResult<String> {
    let codec = py.import("trade_engine.ledger.codec")?;
    let canon = codec.call_method1("canon", (state,))?;
    codec.call_method1("text", (canon,))?.extract()
}

/// `codec.text(codec.encode_payload(value))` — a carrier's encoded JSON text.
fn tree<'py>(py: Python<'py>, value: &Bound<'py, PyAny>) -> PyResult<String> {
    let codec = py.import("trade_engine.ledger.codec")?;
    let encoded = codec.call_method1("encode_payload", (value,))?;
    codec.call_method1("text", (encoded,))?.extract()
}

/// `self._clock.now_utc()`, refused when the clock returns a naive datetime (I7).
fn now_of<'py>(clock: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    let now = clock.call_method0("now_utc")?;
    if now.getattr("tzinfo")?.is_none() {
        return Err(PyValueError::new_err("Clock returned a naive datetime"));
    }
    if now.call_method0("utcoffset")?.is_none() {
        return Err(PyValueError::new_err("Clock returned a naive datetime"));
    }
    Ok(now)
}

/// `_rs.call`'s refusal translation: a te_core `ValueError((kind, message))` becomes the Python
/// exception its kind names, exactly as `trade_engine.sim._rs.call` did. Any other error passes
/// through unchanged.
fn option_refusal(py: Python<'_>, error: PyErr) -> PyErr {
    let value = error.value(py);
    if !value.get_type().is(&py.get_type::<PyValueError>()) {
        return error;
    }
    let Ok((kind, message)) = value.getattr("args").and_then(|args| args.extract::<(String, String)>())
    else {
        return error;
    };
    match py
        .import("trade_engine.sim._rs")
        .and_then(|module| module.call_method1("refusal", (kind, message)))
    {
        Ok(refused) => PyErr::from_value(refused),
        Err(failed) => failed,
    }
}

fn is_terminal<'py>(py: Python<'py>, state: &Bound<'py, PyAny>) -> PyResult<bool> {
    let os = import_attr(py, "trade_engine.domain.orders", "OrderState")?;
    Ok(state.is(&os.getattr("FILLED")?)
        || state.is(&os.getattr("CANCELLED")?)
        || state.is(&os.getattr("REJECTED")?)
        || state.is(&os.getattr("EXPIRED")?))
}

fn is_new<'py>(py: Python<'py>, state: &Bound<'py, PyAny>) -> PyResult<bool> {
    let os = import_attr(py, "trade_engine.domain.orders", "OrderState")?;
    Ok(state.is(&os.getattr("NEW")?))
}

/// `OrdersCreated(orders=..., fingerprint=..., reason=...)`.
fn build_orders_created<'py>(
    py: Python<'py>,
    orders: &Bound<'py, PyTuple>,
    fingerprint: &str,
    reason: &str,
) -> PyResult<Bound<'py, PyAny>> {
    let kwargs = PyDict::new(py);
    kwargs.set_item("orders", orders)?;
    kwargs.set_item("fingerprint", fingerprint)?;
    kwargs.set_item("reason", reason)?;
    import_attr(py, "trade_engine.ledger", "OrdersCreated")?.call((), Some(&kwargs))
}

/// `self._ledger.append(Event(...))` — the W6-E native options OMS write.
#[allow(clippy::too_many_arguments)]
fn append_event<'py>(
    py: Python<'py>,
    clock: &Bound<'py, PyAny>,
    ledger: &Bound<'py, PyAny>,
    account_id: &str,
    payload: &Bound<'py, PyAny>,
    command_id: &str,
) -> PyResult<Py<PyAny>> {
    let now = now_of(clock)?;
    let kwargs = PyDict::new(py);
    kwargs.set_item("account", account_id)?;
    kwargs.set_item("kind", import_attr(py, "trade_engine.ledger", "EventKind")?.getattr("ORDERS_CREATED")?)?;
    kwargs.set_item("payload", payload)?;
    kwargs.set_item("ts_utc", &now)?;
    kwargs.set_item("command_id", command_id)?;
    let event = import_attr(py, "trade_engine.ledger", "Event")?.call((), Some(&kwargs))?;
    Ok(ledger.call_method1("append", (event,))?.unbind())
}

/// The `OrderManager` that wraps the broker, clock and ledger for one option account.
///
/// The inner `OrderManager` is a Python collaborator (another wave-2 ticket ports it in
/// parallel); it is built here and reached through attribute lookup everywhere else.
#[pyclass(subclass, module = "trade_engine_rs")]
pub struct OptionOrderManagerBase {
    #[pyo3(get)]
    _clock: Py<PyAny>,
    #[pyo3(get)]
    _ledger: Py<PyAny>,
    #[pyo3(get)]
    orders: Py<PyAny>,
}

#[pymethods]
impl OptionOrderManagerBase {
    #[new]
    fn new(
        py: Python<'_>,
        broker: &Bound<'_, PyAny>,
        clock: &Bound<'_, PyAny>,
        ledger: &Bound<'_, PyAny>,
    ) -> PyResult<Self> {
        let orders = import_attr(py, "trade_engine.oms.manager", "OrderManager")?
            .call1((broker, clock, ledger))?
            .unbind();
        Ok(OptionOrderManagerBase {
            _clock: clock.clone().unbind(),
            _ledger: ledger.clone().unbind(),
            orders,
        })
    }

    fn open<'py>(slf: &Bound<'py, OptionOrderManagerBase>, intent: &Bound<'py, PyAny>) -> PyResult<Py<PyAny>> {
        let slf = slf.as_any();
        let py = slf.py();
        let fingerprint: String = slf.call_method1("_intent_fingerprint", (intent,))?.extract()?;
        let command_id: String = intent.getattr("command_id")?.extract()?;
        let account_id: String = intent.getattr("account_id")?.extract()?;
        let ledger = slf.getattr("_ledger")?;
        let existing = ledger.call_method1("event_by_command", (command_id.as_str(),))?;
        if !existing.is_none() {
            let kind = existing.getattr("kind")?;
            let orders_created =
                kind.is(&import_attr(py, "trade_engine.ledger", "EventKind")?.getattr("ORDERS_CREATED")?);
            let existing_account: String = existing.getattr("account")?.extract()?;
            crate::oms::oms_replay_conflict(&command_id, orders_created, existing_account == account_id)
                .map_err(|e| option_refusal(py, e))?;
            let existing_fingerprint: String = existing.getattr("payload")?.getattr("fingerprint")?.extract()?;
            let presented: String = slf
                .call_method1(
                    "_presented_fingerprint",
                    (intent, existing_fingerprint.as_str(), fingerprint.as_str()),
                )?
                .extract()?;
            crate::oms::oms_fingerprint_conflict(&command_id, &existing_fingerprint, &presented)
                .map_err(|e| option_refusal(py, e))?;
            let order = existing.getattr("payload")?.getattr("orders")?.get_item(0)?;
            return slf.call_method1("_submit", (order,)).map(|o| o.unbind());
        }
        let state = ledger.call_method1("state", (account_id.as_str(),))?;
        let state_text = state_text(py, &state)?;
        let instrument = intent.getattr("instrument")?;
        let instrument_tree = tree(py, &instrument)?;
        let side_value: String = intent.getattr("side")?.getattr("value")?.extract()?;
        let quantity: String = intent.getattr("quantity")?.str()?.extract()?;
        crate::oms::oms_plan_open(&state_text, &account_id, &instrument_tree, &side_value, &quantity)
            .map_err(|e| option_refusal(py, e))?;
        let clock = slf.getattr("_clock")?;
        let now = now_of(&clock)?;
        let entry = build_entry(py, intent, &command_id, &account_id, &now)?;
        let mut orders = vec![entry.clone()];
        let profit_target = intent.getattr("profit_target")?;
        if !profit_target.is_none() {
            let (instrument, side) = reverse_of(py, &instrument, &intent.getattr("side")?)?;
            let entry_order_id: String = entry.getattr("order_id")?.extract()?;
            let target_id = format!("{entry_order_id}:target");
            let target = build_target(py, intent, &account_id, &now, &instrument, &side, &entry_order_id, &target_id)?;
            orders.push(target);
        }
        let reason = format!("Options entry: {}", intent.getattr("reason")?.extract::<String>()?);
        let payload = build_orders_created(py, &PyTuple::new(py, orders)?, &fingerprint, &reason)?;
        append_event(py, &clock, &ledger, &account_id, &payload, &command_id)?;
        slf.call_method1("_submit", (entry,)).map(|o| o.unbind())
    }

    fn close<'py>(slf: &Bound<'py, OptionOrderManagerBase>, account_id: &str, action: &Bound<'py, PyAny>) -> PyResult<Py<PyAny>> {
        let slf = slf.as_any();
        let py = slf.py();
        let ledger = slf.getattr("_ledger")?;
        let clock = slf.getattr("_clock")?;
        let orders = slf.getattr("orders")?;
        let command_id: String = action.getattr("command_id")?.extract()?;
        let entry_order_id: String = action.getattr("entry_order_id")?.extract()?;
        let existing = ledger.call_method1("event_by_command", (command_id.as_str(),))?;
        if !existing.is_none() {
            let kind = existing.getattr("kind")?;
            let orders_created =
                kind.is(&import_attr(py, "trade_engine.ledger", "EventKind")?.getattr("ORDERS_CREATED")?);
            let existing_account: String = existing.getattr("account")?.extract()?;
            crate::oms::oms_replay_conflict(&command_id, orders_created, existing_account == account_id)
                .map_err(|e| option_refusal(py, e))?;
            let order = existing.getattr("payload")?.getattr("orders")?.get_item(0)?;
            let parent: Option<String> = order.getattr("parent_order_id")?.extract()?;
            crate::oms::oms_close_target_conflict(&command_id, parent.as_deref(), &entry_order_id)
                .map_err(|e| option_refusal(py, e))?;
            return slf.call_method1("_submit", (order,)).map(|o| o.unbind());
        }
        let state = ledger.call_method1("state", (account_id,))?;
        let state_text = state_text(py, &state)?;
        let (order_id, mode, leg_indices, side_value, quantity, oco_group, target_id) =
            crate::oms::oms_plan_close(&state_text, account_id, &entry_order_id)
                .map_err(|e| option_refusal(py, e))?;
        let entry = state.getattr("orders")?.get_item(entry_order_id.as_str())?;
        let held = import_attr(py, "trade_engine.domain.option_orders", "legs_of")?
            .call1((entry.getattr("instrument")?, entry.getattr("side")?))?;
        let mut open_legs: Vec<Bound<'py, PyAny>> = Vec::new();
        for index in &leg_indices {
            open_legs.push(held.get_item(*index)?);
        }
        let instrument = if mode == "single" {
            open_legs[0].getattr("contract")?
        } else {
            let combo_leg = import_attr(py, "trade_engine.domain.instruments", "ComboLeg")?;
            let mut legs: Vec<Bound<'py, PyAny>> = Vec::new();
            for leg in &open_legs {
                legs.push(combo_leg.call1((leg.getattr("contract")?, leg.getattr("ratio")?, leg.getattr("side")?))?);
            }
            let combo = import_attr(py, "trade_engine.domain.instruments", "Combo")?.call1((PyTuple::new(py, legs)?,))?;
            let reversed = import_attr(py, "trade_engine.domain.option_orders", "reverse")?
                .call1((combo, entry.getattr("side")?))?;
            reversed.get_item(0)?
        };
        let now = now_of(&clock)?;
        let limit_price = action.getattr("limit_price")?;
        let order_type = if limit_price.is_none() { "MARKET" } else { "LIMIT" };
        let kwargs = PyDict::new(py);
        kwargs.set_item("order_id", &order_id)?;
        kwargs.set_item("account_id", account_id)?;
        kwargs.set_item("instrument", &instrument)?;
        kwargs.set_item("order_type", import_attr(py, "trade_engine.domain.orders", "OrderType")?.call1((order_type,))?)?;
        kwargs.set_item("side", import_attr(py, "trade_engine.domain.instruments", "Side")?.call1((side_value,))?)?;
        kwargs.set_item("quantity", py.import("decimal")?.getattr("Decimal")?.call1((quantity,))?)?;
        kwargs.set_item("command_id", &command_id)?;
        kwargs.set_item("created_at", &now)?;
        kwargs.set_item("limit_price", &limit_price)?;
        kwargs.set_item("tif", import_attr(py, "trade_engine.domain.orders", "TimeInForce")?.call1(("DAY",))?)?;
        kwargs.set_item("parent_order_id", &entry_order_id)?;
        kwargs.set_item("oco_group", &oco_group)?;
        let close = import_attr(py, "trade_engine.domain.orders", "Order")?.call((), Some(&kwargs))?;
        if let Some(target_id) = target_id {
            let kwargs = PyDict::new(py);
            kwargs.set_item("command_id", format!("{command_id}:replaces-target"))?;
            orders.call_method("cancel", (target_id.as_str(),), Some(&kwargs))?;
        }
        let fingerprint: String = slf.call_method1("_order_fingerprint", (close.clone(),))?.extract()?;
        let reason = format!("Options close: {}", action.getattr("reason")?.extract::<String>()?);
        let payload = build_orders_created(py, &PyTuple::new(py, [close.clone()])?, &fingerprint, &reason)?;
        append_event(py, &clock, &ledger, account_id, &payload, &command_id)?;
        slf.call_method1("_submit", (close,)).map(|o| o.unbind())
    }

    fn close_holding<'py>(slf: &Bound<'py, OptionOrderManagerBase>, account_id: &str, action: &Bound<'py, PyAny>) -> PyResult<Py<PyAny>> {
        let slf = slf.as_any();
        let py = slf.py();
        let ledger = slf.getattr("_ledger")?;
        let clock = slf.getattr("_clock")?;
        let command_id: String = action.getattr("command_id")?.extract()?;
        let existing = ledger.call_method1("event_by_command", (command_id.as_str(),))?;
        if !existing.is_none() {
            let kind = existing.getattr("kind")?;
            let orders_created =
                kind.is(&import_attr(py, "trade_engine.ledger", "EventKind")?.getattr("ORDERS_CREATED")?);
            let existing_account: String = existing.getattr("account")?.extract()?;
            crate::oms::oms_replay_conflict(&command_id, orders_created, existing_account == account_id)
                .map_err(|e| option_refusal(py, e))?;
            let order = existing.getattr("payload")?.getattr("orders")?.get_item(0)?;
            return slf.call_method1("_submit", (order,)).map(|o| o.unbind());
        }
        let state = ledger.call_method1("state", (account_id,))?;
        let state_text = state_text(py, &state)?;
        let instrument = action.getattr("instrument")?;
        let instrument_tree = tree(py, &instrument)?;
        let quantity: String = action.getattr("quantity")?.str()?.extract()?;
        let side_value = crate::oms::oms_plan_holding(&state_text, account_id, &instrument_tree, &quantity, &command_id)
            .map_err(|e| option_refusal(py, e))?;
        let side = import_attr(py, "trade_engine.domain.instruments", "Side")?.call1((side_value,))?;
        let order_id = format!("{command_id}:holding");
        let now = now_of(&clock)?;
        let kwargs = PyDict::new(py);
        kwargs.set_item("order_id", &order_id)?;
        kwargs.set_item("account_id", account_id)?;
        kwargs.set_item("instrument", &instrument)?;
        kwargs.set_item("order_type", import_attr(py, "trade_engine.domain.orders", "OrderType")?.call1(("MARKET",))?)?;
        kwargs.set_item("side", &side)?;
        kwargs.set_item("quantity", action.getattr("quantity")?)?;
        kwargs.set_item("command_id", &command_id)?;
        kwargs.set_item("created_at", &now)?;
        kwargs.set_item("tif", import_attr(py, "trade_engine.domain.orders", "TimeInForce")?.call1(("DAY",))?)?;
        let order = import_attr(py, "trade_engine.domain.orders", "Order")?.call((), Some(&kwargs))?;
        let fingerprint: String = slf.call_method1("_order_fingerprint", (order.clone(),))?.extract()?;
        let reason = format!("Holding close: {}", action.getattr("reason")?.extract::<String>()?);
        let payload = build_orders_created(py, &PyTuple::new(py, [order.clone()])?, &fingerprint, &reason)?;
        append_event(py, &clock, &ledger, account_id, &payload, &command_id)?;
        slf.call_method1("_submit", (order,)).map(|o| o.unbind())
    }

    fn sync<'py>(slf: &Bound<'py, OptionOrderManagerBase>, account_id: &str, cause: &str) -> PyResult<()> {
        let slf = slf.as_any();
        let py = slf.py();
        let ledger = slf.getattr("_ledger")?;
        let orders = slf.getattr("orders")?;
        let state = ledger.call_method1("state", (account_id,))?;
        let state_text = state_text(py, &state)?;
        let plan = crate::oms::oms_sync_plan(&state_text).map_err(|e| option_refusal(py, e))?;
        for (child_id, action) in plan {
            let current = orders.call_method1("get_order", (child_id.as_str(),))?;
            let state = current.getattr("state")?;
            if is_terminal(py, &state)? {
                continue;
            }
            if let Some(action) = action {
                match action.as_str() {
                    "structure-done" | "leg-settled" => {
                        let kwargs = PyDict::new(py);
                        kwargs.set_item("command_id", format!("{cause}:{child_id}:{action}"))?;
                        orders.call_method("cancel", (child_id.as_str(),), Some(&kwargs))?;
                    }
                    "submit-target" => {
                        if is_new(py, &state)? {
                            slf.call_method1("_submit", (current,))?;
                        }
                    }
                    _ => {}
                }
            }
        }
        Ok(())
    }

    fn _submit<'py>(slf: &Bound<'py, OptionOrderManagerBase>, order: &Bound<'py, PyAny>) -> PyResult<Py<PyAny>> {
        let slf = slf.as_any();
        let py = slf.py();
        let orders = slf.getattr("orders")?;
        let order_id: String = order.getattr("order_id")?.extract()?;
        let current = orders.call_method1("get_order", (order_id.as_str(),))?;
        if !is_new(py, &current.getattr("state")?)? {
            return Ok(current.unbind());
        }
        let submitted = orders.call_method1("submit", (current,))?;
        let submitted_state = submitted.getattr("state")?;
        if !is_terminal(py, &submitted_state)? {
            return orders
                .call_method1("reconcile_order", (order_id.as_str(),))
                .map(|o| o.unbind());
        }
        Ok(submitted.unbind())
    }
}

/// `Order(order_id=f"{command_id}:entry", ...)` — the entry order of an `open`.
fn build_entry<'py>(
    py: Python<'py>,
    intent: &Bound<'py, PyAny>,
    command_id: &str,
    account_id: &str,
    now: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    let entry_id = format!("{command_id}:entry");
    let kwargs = PyDict::new(py);
    kwargs.set_item("order_id", &entry_id)?;
    kwargs.set_item("account_id", account_id)?;
    kwargs.set_item("instrument", intent.getattr("instrument")?)?;
    kwargs.set_item("order_type", intent.getattr("order_type")?)?;
    kwargs.set_item("side", intent.getattr("side")?)?;
    kwargs.set_item("quantity", intent.getattr("quantity")?)?;
    kwargs.set_item("command_id", &entry_id)?;
    kwargs.set_item("created_at", now)?;
    kwargs.set_item("limit_price", intent.getattr("limit_price")?)?;
    kwargs.set_item("tif", intent.getattr("tif")?)?;
    import_attr(py, "trade_engine.domain.orders", "Order")?.call((), Some(&kwargs))
}

/// The resting profit target of an `open`: `reverse(intent.instrument, intent.side)`, a GTC limit.
#[allow(clippy::too_many_arguments)]
fn build_target<'py>(
    py: Python<'py>,
    intent: &Bound<'py, PyAny>,
    account_id: &str,
    now: &Bound<'py, PyAny>,
    instrument: &Bound<'py, PyAny>,
    side: &Bound<'py, PyAny>,
    entry_order_id: &str,
    target_id: &str,
) -> PyResult<Bound<'py, PyAny>> {
    let kwargs = PyDict::new(py);
    kwargs.set_item("order_id", target_id)?;
    kwargs.set_item("account_id", account_id)?;
    kwargs.set_item("instrument", instrument)?;
    kwargs.set_item("order_type", import_attr(py, "trade_engine.domain.orders", "OrderType")?.call1(("LIMIT",))?)?;
    kwargs.set_item("side", side)?;
    kwargs.set_item("quantity", intent.getattr("quantity")?)?;
    kwargs.set_item("command_id", target_id)?;
    kwargs.set_item("created_at", now)?;
    kwargs.set_item("limit_price", intent.getattr("profit_target")?)?;
    kwargs.set_item("tif", import_attr(py, "trade_engine.domain.orders", "TimeInForce")?.call1(("GTC",))?)?;
    kwargs.set_item("parent_order_id", entry_order_id)?;
    kwargs.set_item("oco_group", format!("{}:exits", intent.getattr("command_id")?.extract::<String>()?))?;
    import_attr(py, "trade_engine.domain.orders", "Order")?.call((), Some(&kwargs))
}

/// `reverse(instrument, side)` -> the `(instrument, side)` that undoes it.
fn reverse_of<'py>(
    py: Python<'py>,
    instrument: &Bound<'py, PyAny>,
    side: &Bound<'py, PyAny>,
) -> PyResult<(Bound<'py, PyAny>, Bound<'py, PyAny>)> {
    import_attr(py, "trade_engine.domain.option_orders", "reverse")?
        .call1((instrument, side))?
        .extract()
}

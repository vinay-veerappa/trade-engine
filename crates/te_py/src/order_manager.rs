//! W7-CORE-C5: `OrderManager`'s sequencing moves into Rust (`OrderManagerBase`).
//!
//! `OrderManager` (Python, `src/trade_engine/oms/manager.py`) sequences submit / cancel /
//! replace / bracket / OCO / fill handling over a ledger, a broker and the native OMS core
//! (`te_core::oms::flow`, reached through `trade_engine_rs.oms_flow` / `oms_flow_native`).
//! `OrderManagerBase` owns those method bodies, step for step and in the same order.
//!
//! The collaborators stay Python and are reached through attribute lookup: the ledger, the
//! broker, the clock and the precheck gate are held by the `_Host` adapter (a Python class
//! that stays importable — tests construct it and monkeypatch `_Host.after_append`), and the
//! flow's effects are one adapter call each. The module-level helpers (`_wire`, `_order`,
//! `_bracket`, `_changes`, `_dec`, `_OrderContext`) also stay Python (tests import them), so
//! the base calls them through `trade_engine.oms.manager` rather than re-reading them.
//!
//! Exceptions stay Python: a flow refusal crosses as `ValueError((kind, message))` and is
//! re-raised through `trade_engine.sim._rs.refusal` (which maps the kind to the exception the
//! module registered), with the broker error preserved as `__cause__`. The `__init__` gate's
//! `PrecheckConfigError` is imported and raised the same way.

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyTuple};

use crate::store::LedgerStore;

/// `module.Class(msg)` — raise a Python exception class by name, as the session doors do.
fn raise_as(py: Python<'_>, module: &str, class: &str, msg: &str) -> PyErr {
    match py
        .import(module)
        .and_then(|m| m.getattr(class))
        .and_then(|c| c.call1((msg,)))
    {
        Ok(value) => PyErr::from_value(value),
        Err(error) => error,
    }
}

/// `_rs.refusal(kind, message)`, with the flow's `__cause__` preserved (a broker error held
/// behind a `broker_unknown` / `oco_unknown` refusal).
fn flow_refusal(py: Python<'_>, kind: &str, message: &str, cause: Option<PyErr>) -> PyErr {
    let err = match py
        .import("trade_engine.sim._rs")
        .and_then(|m| m.getattr("refusal"))
        .and_then(|f| f.call1((kind, message)))
    {
        Ok(value) => PyErr::from_value(value),
        Err(error) => error,
    };
    if let Some(cause) = cause {
        err.set_cause(py, Some(cause));
    }
    err
}

/// `_flow`'s except clause: a plain `ValueError` with exactly two string args becomes the
/// registered exception; anything else (a host error such as a `KeyError` or `RuntimeError`)
/// propagates unchanged.
fn map_refusal(py: Python<'_>, err: PyErr) -> PyErr {
    let value_error = py.get_type::<PyValueError>().into_any();
    if !err.get_type(py).is(&value_error) {
        return err;
    }
    let (kind, message) = match err
        .value(py)
        .getattr("args")
        .and_then(|args| args.extract::<(String, String)>())
    {
        Ok(pair) => pair,
        Err(_) => return err,
    };
    let cause = err
        .value(py)
        .getattr("__cause__")
        .ok()
        .filter(|cause| !cause.is_none())
        .map(PyErr::from_value);
    flow_refusal(py, &kind, &message, cause)
}

/// `codec.text(_wire(request))`: the request dict as the flow's JSON text. `_wire` stays
/// Python (imported by other modules), so it is reached through `trade_engine.oms.manager`.
fn wire_text(py: Python<'_>, request: &Bound<'_, PyDict>) -> PyResult<String> {
    let wired = py
        .import("trade_engine.oms.manager")?
        .getattr("_wire")?
        .call1((request,))?;
    py.import("trade_engine.ledger.codec")?
        .call_method1("text", (&wired,))?
        .extract()
}

/// `_order(tree)` — a flow result tree as an `Order`.
fn order_of(py: Python<'_>, tree: &Bound<'_, PyAny>) -> PyResult<Py<PyAny>> {
    Ok(py
        .import("trade_engine.oms.manager")?
        .getattr("_order")?
        .call1((tree,))?
        .unbind())
}

/// `_bracket(tree)` — a flow result tree as a `Bracket`.
fn bracket_of(py: Python<'_>, tree: &Bound<'_, PyAny>) -> PyResult<Py<PyAny>> {
    Ok(py
        .import("trade_engine.oms.manager")?
        .getattr("_bracket")?
        .call1((tree,))?
        .unbind())
}

#[pyclass(subclass, module = "trade_engine_rs")]
pub struct OrderManagerBase {
    ledger: Option<Py<PyAny>>,
    host: Option<Py<PyAny>>,
}

#[pymethods]
impl OrderManagerBase {
    #[new]
    #[pyo3(signature = (*args, **kwargs))]
    fn new(args: &Bound<'_, PyTuple>, kwargs: Option<&Bound<'_, PyDict>>) -> Self {
        let _ = (args, kwargs);
        OrderManagerBase { ledger: None, host: None }
    }

    #[pyo3(signature = (broker, clock, ledger, precheck=None))]
    fn __init__(
        slf: &Bound<'_, Self>,
        broker: &Bound<'_, PyAny>,
        clock: &Bound<'_, PyAny>,
        ledger: &Bound<'_, PyAny>,
        precheck: Option<&Bound<'_, PyAny>>,
    ) -> PyResult<()> {
        let py = slf.py();

        // TradovateAdapter needs riskguard.precheck.mode = enforce (OA-3): warn or off would
        // send unchecked orders to a live venue.
        let tradovate = py
            .import("trade_engine.tradovate.adapter")?
            .getattr("TradovateAdapter")?;
        let unguarded = match precheck {
            None => true,
            Some(config) => config.getattr("mode")?.extract::<String>()? != "enforce",
        };
        if broker.is_instance(&tradovate)? && unguarded {
            return Err(raise_as(
                py,
                "trade_engine.oms.precheck",
                "PrecheckConfigError",
                "TradovateAdapter needs riskguard.precheck.mode = enforce",
            ));
        }

        // gate = Precheck(precheck, clock, ledger) if precheck is not None and precheck.mode != "off"
        let gate: Option<Bound<'_, PyAny>> = match precheck {
            Some(config) => {
                let mode: String = config.getattr("mode")?.extract()?;
                if mode != "off" {
                    let cls = py.import("trade_engine.oms.precheck")?.getattr("Precheck")?;
                    Some(cls.call1((config, clock, ledger))?)
                } else {
                    None
                }
            }
            None => None,
        };

        // self._host = _Host(self, broker, clock, ledger, gate)
        let host_cls = py.import("trade_engine.oms.manager")?.getattr("_Host")?;
        let host = match &gate {
            Some(gate) => host_cls.call1((slf.as_any(), broker, clock, ledger, gate))?,
            None => host_cls.call1((slf.as_any(), broker, clock, ledger))?,
        };

        // The original `__init__` stored the collaborators as instance attributes; tests read
        // `manager._broker` (and the ledger/clock/host), so they stay Python attributes too.
        let any = slf.as_any();
        any.setattr("_broker", broker)?;
        any.setattr("_clock", clock)?;
        any.setattr("_ledger", ledger)?;
        any.setattr("_host", &host)?;

        let mut this = slf.borrow_mut();
        this.ledger = Some(ledger.clone().unbind());
        this.host = Some(host.unbind());
        Ok(())
    }

    #[pyo3(signature = (operation, **request))]
    fn _flow(
        &self,
        py: Python<'_>,
        operation: &str,
        request: Option<&Bound<'_, PyDict>>,
    ) -> PyResult<Py<PyAny>> {
        let request = match request {
            Some(dict) => dict.clone(),
            None => PyDict::new(py),
        };
        Ok(self.run(py, operation, &request)?.unbind())
    }

    fn create_bracket(
        &self,
        py: Python<'_>,
        intent: &Bound<'_, PyAny>,
        quantity: &Bound<'_, PyAny>,
    ) -> PyResult<Py<PyAny>> {
        let request = PyDict::new(py);
        request.set_item("intent", intent)?;
        request.set_item("quantity", quantity)?;
        let tree = self.run(py, "create_bracket", &request)?;
        bracket_of(py, &tree)
    }

    fn get_order(&self, py: Python<'_>, order_id: &str) -> PyResult<Py<PyAny>> {
        let request = PyDict::new(py);
        request.set_item("order_id", order_id)?;
        let tree = self.run(py, "get_order", &request)?;
        order_of(py, &tree)
    }

    fn submit(&self, py: Python<'_>, order: &Bound<'_, PyAny>) -> PyResult<Py<PyAny>> {
        let request = PyDict::new(py);
        request.set_item("order", order)?;
        let tree = self.run(py, "submit", &request)?;
        order_of(py, &tree)
    }

    fn submit_trailing(&self, py: Python<'_>, order: &Bound<'_, PyAny>) -> PyResult<Py<PyAny>> {
        let request = PyDict::new(py);
        request.set_item("order", order)?;
        let tree = self.run(py, "submit_trailing", &request)?;
        order_of(py, &tree)
    }

    #[pyo3(signature = (order_id, price, *, command_id))]
    fn update_trailing(
        &self,
        py: Python<'_>,
        order_id: &str,
        price: &Bound<'_, PyAny>,
        command_id: &str,
    ) -> PyResult<Py<PyAny>> {
        let request = PyDict::new(py);
        request.set_item("order_id", order_id)?;
        request.set_item("price", price)?;
        request.set_item("command_id", command_id)?;
        let tree = self.run(py, "update_trailing", &request)?;
        order_of(py, &tree)
    }

    #[pyo3(signature = (order_id, price, *, command_id))]
    fn update_emulated_order(
        &self,
        py: Python<'_>,
        order_id: &str,
        price: &Bound<'_, PyAny>,
        command_id: &str,
    ) -> PyResult<Py<PyAny>> {
        let request = PyDict::new(py);
        request.set_item("order_id", order_id)?;
        request.set_item("price", price)?;
        request.set_item("command_id", command_id)?;
        let tree = self.run(py, "update_emulated_order", &request)?;
        order_of(py, &tree)
    }

    fn record_fill(&self, py: Python<'_>, fill: &Bound<'_, PyAny>) -> PyResult<Py<PyAny>> {
        let request = PyDict::new(py);
        request.set_item("fill", fill)?;
        let tree = self.run(py, "record_fill", &request)?;
        order_of(py, &tree)
    }

    fn synchronize_bracket(
        &self,
        py: Python<'_>,
        order: &Bound<'_, PyAny>,
        cause: &str,
    ) -> PyResult<()> {
        let request = PyDict::new(py);
        request.set_item("order", order)?;
        request.set_item("cause", cause)?;
        self.run(py, "synchronize_bracket", &request)?;
        Ok(())
    }

    #[pyo3(signature = (order_id, *, command_id))]
    fn cancel(&self, py: Python<'_>, order_id: &str, command_id: &str) -> PyResult<Py<PyAny>> {
        let request = PyDict::new(py);
        request.set_item("order_id", order_id)?;
        request.set_item("command_id", command_id)?;
        let tree = self.run(py, "cancel", &request)?;
        order_of(py, &tree)
    }

    #[pyo3(signature = (entry_order_id, stop_price, *, command_id))]
    fn move_stop(
        &self,
        py: Python<'_>,
        entry_order_id: &str,
        stop_price: &Bound<'_, PyAny>,
        command_id: &str,
    ) -> PyResult<Py<PyAny>> {
        let request = PyDict::new(py);
        request.set_item("entry", entry_order_id)?;
        request.set_item("stop_price", stop_price)?;
        request.set_item("command_id", command_id)?;
        let tree = self.run(py, "move_stop", &request)?;
        order_of(py, &tree)
    }

    #[pyo3(signature = (entry_order_id, *, command_id, reason))]
    fn close_bracket(
        &self,
        py: Python<'_>,
        entry_order_id: &str,
        command_id: &str,
        reason: &str,
    ) -> PyResult<Py<PyAny>> {
        let request = PyDict::new(py);
        request.set_item("entry", entry_order_id)?;
        request.set_item("command_id", command_id)?;
        request.set_item("reason", reason)?;
        let tree = self.run(py, "close_bracket", &request)?;
        order_of(py, &tree)
    }

    #[pyo3(signature = (entry_order_id, fraction, *, command_id, reason))]
    fn reduce_bracket(
        &self,
        py: Python<'_>,
        entry_order_id: &str,
        fraction: &Bound<'_, PyAny>,
        command_id: &str,
        reason: &str,
    ) -> PyResult<Py<PyAny>> {
        let request = PyDict::new(py);
        request.set_item("entry", entry_order_id)?;
        request.set_item("fraction", fraction)?;
        request.set_item("command_id", command_id)?;
        request.set_item("reason", reason)?;
        let tree = self.run(py, "reduce_bracket", &request)?;
        order_of(py, &tree)
    }

    #[pyo3(signature = (order_id, changes, *, command_id))]
    fn replace(
        &self,
        py: Python<'_>,
        order_id: &str,
        changes: &Bound<'_, PyAny>,
        command_id: &str,
    ) -> PyResult<Py<PyAny>> {
        let request = PyDict::new(py);
        request.set_item("order_id", order_id)?;
        let wired_changes = py
            .import("trade_engine.oms.manager")?
            .getattr("_changes")?
            .call1((changes,))?;
        request.set_item("changes", wired_changes)?;
        request.set_item("command_id", command_id)?;
        let tree = self.run(py, "replace", &request)?;
        order_of(py, &tree)
    }

    fn reconcile_order(&self, py: Python<'_>, order_id: &str) -> PyResult<Py<PyAny>> {
        let request = PyDict::new(py);
        request.set_item("order_id", order_id)?;
        let tree = self.run(py, "reconcile_order", &request)?;
        order_of(py, &tree)
    }

    fn _send_reduce(
        &self,
        py: Python<'_>,
        reduce: &Bound<'_, PyAny>,
        command_id: &str,
    ) -> PyResult<Py<PyAny>> {
        let request = PyDict::new(py);
        request.set_item("order", reduce)?;
        request.set_item("command_id", command_id)?;
        let tree = self.run(py, "send_reduce", &request)?;
        order_of(py, &tree)
    }

    fn _bracket_children(
        &self,
        py: Python<'_>,
        entry_order_id: &str,
    ) -> PyResult<Vec<Py<PyAny>>> {
        let request = PyDict::new(py);
        request.set_item("entry", entry_order_id)?;
        let tree = self.run(py, "bracket_children", &request)?;
        let mut out = Vec::new();
        for item in tree.try_iter()? {
            out.push(order_of(py, &item?)?);
        }
        Ok(out)
    }

    fn _open_bracket_stop(
        &self,
        py: Python<'_>,
        entry_order_id: &str,
    ) -> PyResult<(Py<PyAny>, Py<PyAny>)> {
        let request = PyDict::new(py);
        request.set_item("entry", entry_order_id)?;
        let tree = self.run(py, "open_bracket_stop", &request)?;
        let stop_tree = tree.get_item(0)?;
        let open_quantity = tree.get_item(1)?;
        let stop = order_of(py, &stop_tree)?;
        let quantity = py
            .import("decimal")?
            .getattr("Decimal")?
            .call1((&open_quantity,))?
            .unbind();
        Ok((stop, quantity))
    }
}

impl OrderManagerBase {
    /// One `OrderManager` method (`te_core::oms::flow::run`) against the `_Host`: the request
    /// dict is wired to JSON text, the flow runs (natively against a plain `Ledger`'s store,
    /// or over the adapter's ledger methods otherwise), and the result is `json.loads`-ed.
    fn run<'py>(
        &self,
        py: Python<'py>,
        operation: &str,
        request: &Bound<'_, PyDict>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let text = wire_text(py, request)?;
        let ledger = self.ledger.as_ref().expect("initialized").bind(py);
        let host = self.host.as_ref().expect("initialized").bind(py);

        let out: String = {
            let plain = py.import("trade_engine.ledger.store")?.getattr("Ledger")?;
            if ledger.get_type().is(&plain) {
                // The ledger's effects run natively against its store. The store's notifier
                // must be the ledger's listeners: an owner-built Ledger has none set, so it is
                // set here (idempotent).
                let store = ledger.getattr("_store")?;
                store.call_method1("set_notifier", (ledger.call_method0("_notifier")?,))?;
                let store_ref = store.downcast_into::<LedgerStore>()?;
                crate::flow::oms_flow_native(operation, store_ref.borrow(), host, &text)
            } else {
                crate::flow::oms_flow(operation, host, &text)
            }
        }
        .map_err(|err| map_refusal(py, err))?;

        py.import("json")?.call_method1("loads", (out,))
    }
}

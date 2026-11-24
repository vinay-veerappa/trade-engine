//! The `TosBroker` pyclass (the `TosPaperBroker` state machine, `te_core::tos_paper::broker`).
//! Refusals cross as `ValueError((kind, message))` for `sim/_rs.py` to map.
//!
//! The broker takes the host's callbacks (the clock, the transport reads, sends and cancels)
//! as methods of one adapter object, and answers Python objects (`plain_of`/`plain_py` convert
//! them, not JSON text). An `Exception` the transport raised crosses as a tagged result (the
//! adapter classifies it); a `BaseException` unwinds as itself, and an exception the broker
//! lets through (`TransportUnavailable`) is re-raised by the adapter's `reraise()` as the
//! very object it raised.
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use te_core::ledger::json::Json;
use te_core::ledger::model::{LErr, R as LR};
use te_core::tos_paper::broker::{Broker, Class, Fault, Host as BrokerHost, Raised};

use crate::plain::{plain_of, plain_py};
use crate::sim::{refuse, Host};

/// `("ok", value, "", "")` or `("exc", class, type name, str(exc))`: the `"ok"` payload is a
/// Python object (read with `plain_of`); the `"exc"` class name is a string.
type Tagged = (String, Py<PyAny>, String, String);

struct PyBrokerHost<'py> {
    host: Bound<'py, PyAny>,
    stash: Host,
}

impl<'py> PyBrokerHost<'py> {
    fn new(host: &Bound<'py, PyAny>) -> PyBrokerHost<'py> {
        PyBrokerHost { host: host.clone(), stash: Host::new() }
    }

    fn call<T: for<'a> FromPyObject<'a>>(
        &self,
        name: &str,
        args: impl IntoPyObject<'py, Target = pyo3::types::PyTuple>,
    ) -> Result<T, Fault> {
        match self.host.call_method1(name, args).and_then(|v| v.extract::<T>()) {
            Ok(v) => Ok(v),
            Err(e) => Err(Fault::Fatal(self.stash.fail(e))),
        }
    }

    fn net(
        &self,
        name: &str,
        args: impl IntoPyObject<'py, Target = pyo3::types::PyTuple>,
    ) -> Result<Json, Fault> {
        let (tag, a, b, c): Tagged = self.call(name, args)?;
        if tag == "ok" {
            return plain_of(&a.bind(self.host.py())).map_err(|e| Fault::Fatal(self.stash.fail(e)));
        }
        let class = match a.bind(self.host.py()).extract::<String>().as_deref() {
            Ok("refused") => Class::Refused,
            Ok("replay") => Class::Replay,
            Ok("unavailable") => Class::Unavailable,
            _ => Class::Other,
        };
        Err(Fault::Raised(Raised { class, name: b, text: c }))
    }

    fn rows(&self, name: &str) -> Result<Vec<Json>, Fault> {
        match self.net(name, ())? {
            Json::Arr(rows) => Ok(rows),
            _ => Err(Fault::from(LErr { kind: "tos_wire", msg: format!("{name} did not answer a list") })),
        }
    }

    fn finish<T>(self, r: LR<T>) -> PyResult<T> {
        match r {
            Err(e) if e.kind == te_core::tos_paper::RAISED => Err(match self.host.call_method0("reraise") {
                Err(original) => original,
                Ok(_) => PyValueError::new_err((e.kind, e.msg)),
            }),
            r => self.stash.finish(r),
        }
    }
}

impl<'py> BrokerHost for PyBrokerHost<'py> {
    fn now(&mut self) -> Result<String, Fault> {
        self.call("now", ())
    }

    fn place_order(&mut self, spec: &Json, key: &str) -> Result<Json, Fault> {
        let py = self.host.py();
        let spec = plain_py(py, spec).map_err(|e| Fault::Fatal(self.stash.fail(e)))?;
        self.net("place_order", (spec, key.to_string()))
    }

    fn read_positions(&mut self) -> Result<Vec<Json>, Fault> {
        self.rows("read_positions")
    }

    fn read_working_orders(&mut self) -> Result<Vec<Json>, Fault> {
        self.rows("read_working_orders")
    }

    fn read_order_fills(&mut self) -> Result<Vec<Json>, Fault> {
        self.rows("read_order_fills")
    }

    fn cancel_order(&mut self, order_id: &str) -> Result<Json, Fault> {
        self.net("cancel_order", (order_id.to_string(),))
    }

    fn can_read_fills(&mut self) -> Result<bool, Fault> {
        self.call("can_read_fills", ())
    }

    fn can_cancel(&mut self) -> Result<bool, Fault> {
        self.call("can_cancel", ())
    }
}

fn list_of(value: &Bound<'_, PyAny>) -> PyResult<Vec<Json>> {
    match plain_of(value)? {
        Json::Arr(a) => Ok(a),
        _ => Err(PyValueError::new_err(("tos_wire", "a list was expected".to_string()))),
    }
}

/// The `TosPaperBroker`'s decision state (docs/RUST_PORT.md P5-T9).
#[pyclass(module = "trade_engine_rs")]
pub struct TosBroker {
    core: Broker,
}

#[pymethods]
impl TosBroker {
    #[new]
    fn new(venue: &str, mirrored: Vec<String>, halted: bool) -> TosBroker {
        TosBroker { core: Broker::new(venue, mirrored, halted) }
    }

    fn mark_connected(&mut self) {
        self.core.mark_connected();
    }

    #[getter]
    fn halted(&self) -> bool {
        self.core.halted()
    }

    fn mirror_batch(&mut self, host: &Bound<'_, PyAny>, orders: &Bound<'_, PyAny>, holdings: &Bound<'_, PyAny>) -> PyResult<PyObject> {
        let (orders, holdings) = (list_of(orders)?, list_of(holdings)?);
        let mut h = PyBrokerHost::new(host);
        let r = self.core.mirror_batch(&mut h, &orders, &holdings);
        let j = h.finish(r)?;
        Ok(plain_py(host.py(), &j)?.unbind())
    }

    fn restore(&mut self, mirror: &Bound<'_, PyAny>, halted_venues: Vec<String>) -> PyResult<()> {
        self.core.restore(&plain_of(mirror)?, &halted_venues).map_err(refuse)
    }

    fn collect_fills(&mut self, host: &Bound<'_, PyAny>, mirror: &Bound<'_, PyAny>) -> PyResult<PyObject> {
        let mirror = plain_of(mirror)?;
        let mut h = PyBrokerHost::new(host);
        let r = self.core.collect_fills(&mut h, &mirror);
        let j = h.finish(r)?;
        Ok(plain_py(host.py(), &j)?.unbind())
    }

    fn preflight(&mut self, host: &Bound<'_, PyAny>) -> PyResult<()> {
        let mut h = PyBrokerHost::new(host);
        let r = self.core.preflight(&mut h);
        h.finish(r)
    }

    fn drain(&mut self, host: &Bound<'_, PyAny>) -> PyResult<PyObject> {
        let mut h = PyBrokerHost::new(host);
        let r = self.core.drain(&mut h);
        let j = h.finish(r)?;
        Ok(plain_py(host.py(), &j)?.unbind())
    }

    fn reconcile_now(&mut self, host: &Bound<'_, PyAny>, defer_unavailable: bool) -> PyResult<PyObject> {
        let mut h = PyBrokerHost::new(host);
        let r = self.core.reconcile_now(&mut h, defer_unavailable);
        let j = h.finish(r)?;
        Ok(plain_py(host.py(), &j)?.unbind())
    }

    fn submit(&mut self, host: &Bound<'_, PyAny>, ticket: &Bound<'_, PyAny>) -> PyResult<PyObject> {
        let ticket = plain_of(ticket)?;
        let mut h = PyBrokerHost::new(host);
        let r = self.core.submit(&mut h, &ticket);
        let j = h.finish(r)?;
        Ok(plain_py(host.py(), &j)?.unbind())
    }

    fn cancel(&mut self, host: &Bound<'_, PyAny>, venue_order_id: &str) -> PyResult<PyObject> {
        let mut h = PyBrokerHost::new(host);
        let r = self.core.cancel(&mut h, venue_order_id);
        let j = h.finish(r)?;
        Ok(plain_py(host.py(), &j)?.unbind())
    }

    fn positions(&mut self, host: &Bound<'_, PyAny>) -> PyResult<PyObject> {
        let mut h = PyBrokerHost::new(host);
        let r = self.core.positions(&mut h);
        let j = h.finish(r)?;
        Ok(plain_py(host.py(), &j)?.unbind())
    }

    fn proven_order_id(&self, ticket_key: &str) -> Option<String> {
        match self.core.proven_order_id(ticket_key) {
            Json::Str(id) => Some(id),
            _ => None,
        }
    }

    fn state(&self, py: Python<'_>) -> PyResult<PyObject> {
        Ok(plain_py(py, &self.core.state())?.unbind())
    }
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<TosBroker>()?;
    Ok(())
}

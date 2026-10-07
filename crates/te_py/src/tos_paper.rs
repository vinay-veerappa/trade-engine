//! P5's doors: `tos_paper_decide(op, json) -> json` (the stateless decisions), and the
//! `TosBroker` pyclass (the `TosPaperBroker` state machine, `te_core::tos_paper::broker`).
//! Refusals cross as `ValueError((kind, message))` for `sim/_rs.py` to map.
//!
//! The broker takes the host's callbacks (the clock, the transport reads, sends and cancels)
//! as methods of one adapter object, and answers JSON text. An `Exception` the transport
//! raised crosses as a tagged result (the adapter classifies it); a `BaseException` unwinds
//! as itself, and an exception the broker lets through (`TransportUnavailable`) is re-raised
//! by the adapter's `reraise()` as the very object it raised.
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use te_core::ledger::json::{self, Json};
use te_core::ledger::model::{LErr, R as LR};
use te_core::tos_paper::broker::{Broker, Class, Fault, Host as BrokerHost, Raised};

use crate::sim::{refuse, Host};

#[pyfunction]
fn tos_paper_decide(op: &str, json: &str) -> PyResult<String> {
    te_core::tos_paper::decide(op, json).map_err(|e| PyValueError::new_err((e.kind, e.msg)))
}

/// `("ok", json, "", "")` or `("exc", class, type name, str(exc))`.
type Tagged = (String, String, String, String);

struct PyBrokerHost<'py> {
    host: Bound<'py, PyAny>,
    stash: Host,
}

fn parse(text: &str) -> Result<Json, Fault> {
    json::parse(text).map_err(|e| Fault::from(LErr { kind: "tos_wire", msg: format!("bad host json: {}", e.message) }))
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
            return parse(&a);
        }
        let class = match a.as_str() {
            "refused" => Class::Refused,
            "replay" => Class::Replay,
            "unavailable" => Class::Unavailable,
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
        self.net("place_order", (json::dumps(spec), key.to_string()))
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

fn doc(text: &str) -> PyResult<Json> {
    json::parse(text).map_err(|e| PyValueError::new_err(("tos_wire", format!("bad door json: {}", e.message))))
}

fn list(text: &str) -> PyResult<Vec<Json>> {
    match doc(text)? {
        Json::Arr(a) => Ok(a),
        _ => Err(PyValueError::new_err(("tos_wire", "a list was expected".to_string()))),
    }
}

/// The `TosPaperBroker`'s decision state (docs/RUST_PORT.md P5-T9).
#[pyclass(module = "trade_engine_rs", unsendable)]
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

    fn mirror_batch(&mut self, host: &Bound<'_, PyAny>, orders: &str, holdings: &str) -> PyResult<String> {
        let (orders, holdings) = (list(orders)?, list(holdings)?);
        let mut h = PyBrokerHost::new(host);
        let r = self.core.mirror_batch(&mut h, &orders, &holdings);
        h.finish(r).map(|j| json::dumps(&j))
    }

    fn restore(&mut self, mirror: &str, halted_venues: Vec<String>) -> PyResult<()> {
        self.core.restore(&doc(mirror)?, &halted_venues).map_err(refuse)
    }

    fn collect_fills(&mut self, host: &Bound<'_, PyAny>, mirror: &str) -> PyResult<String> {
        let mirror = doc(mirror)?;
        let mut h = PyBrokerHost::new(host);
        let r = self.core.collect_fills(&mut h, &mirror);
        h.finish(r).map(|j| json::dumps(&j))
    }

    fn preflight(&mut self, host: &Bound<'_, PyAny>) -> PyResult<()> {
        let mut h = PyBrokerHost::new(host);
        let r = self.core.preflight(&mut h);
        h.finish(r)
    }

    fn drain(&mut self, host: &Bound<'_, PyAny>) -> PyResult<String> {
        let mut h = PyBrokerHost::new(host);
        let r = self.core.drain(&mut h);
        h.finish(r).map(|j| json::dumps(&j))
    }

    fn reconcile_now(&mut self, host: &Bound<'_, PyAny>, defer_unavailable: bool) -> PyResult<String> {
        let mut h = PyBrokerHost::new(host);
        let r = self.core.reconcile_now(&mut h, defer_unavailable);
        h.finish(r).map(|j| json::dumps(&j))
    }

    fn submit(&mut self, host: &Bound<'_, PyAny>, ticket: &str) -> PyResult<String> {
        let ticket = doc(ticket)?;
        let mut h = PyBrokerHost::new(host);
        let r = self.core.submit(&mut h, &ticket);
        h.finish(r).map(|j| json::dumps(&j))
    }

    fn cancel(&mut self, host: &Bound<'_, PyAny>, venue_order_id: &str) -> PyResult<String> {
        let mut h = PyBrokerHost::new(host);
        let r = self.core.cancel(&mut h, venue_order_id);
        h.finish(r).map(|j| json::dumps(&j))
    }

    fn positions(&mut self, host: &Bound<'_, PyAny>) -> PyResult<String> {
        let mut h = PyBrokerHost::new(host);
        let r = self.core.positions(&mut h);
        h.finish(r).map(|j| json::dumps(&j))
    }

    fn proven_order_id(&self, ticket_key: &str) -> Option<String> {
        match self.core.proven_order_id(ticket_key) {
            Json::Str(id) => Some(id),
            _ => None,
        }
    }

    fn state(&self) -> String {
        json::dumps(&self.core.state())
    }
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(tos_paper_decide, m)?)?;
    m.add_class::<TosBroker>()?;
    Ok(())
}

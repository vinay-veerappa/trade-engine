//! W7-CORE-C12: `TosPaperBroker`'s method bodies, moved into Rust (`TosPaperBrokerBase`).
//!
//! The decisions already live in `te_core::tos_paper::broker::Broker` (reached before
//! through `trade_engine_rs.TosBroker`); this type owns that `Broker` directly and calls
//! its methods instead of going back through the Python `TosBroker` pyclass. The host
//! callbacks (the clock and the transport's reads, sends and cancels) stay Python — the
//! `_Host` adapter class in `tos_paper/broker.py`, held as the `_host` instance attribute —
//! reached through a `BrokerHost` shim (`PyBrokerHost`) exactly as `tos_paper.rs` did.
//!
//! The collaborators (transport, binding, clock, balance reader, mirror path, account
//! ledger) and the internal state (`_identity`, `balance_proven`, `_last_accounts`,
//! `_poller`) stay Python instance attributes, set by the Python subclass's `__init__`;
//! every public method here is reached through normal Python attribute lookup, so the
//! tests' monkeypatches still apply. The carrier conversions (`_rs.venue_order`,
//! `_rs.ack`, `_rs.event`, `_rs.order_doc`, `_rs.holdings_doc`, `_rs.unwire`, `_rs.dec`)
//! stay Python helpers and are called through `trade_engine.tos_paper._rs`.
//!
//! Exceptions stay Python: a `Broker` refusal crosses as `(kind, message)` and is re-raised
//! through `trade_engine.sim._rs.refusal` (which maps the kind to the exception the module
//! registered, `tos_broker_error` -> `TosPaperBrokerError`, `tos_venue_unreadable` ->
//! `VenueUnreadable`, ...), exactly as `_rs.call` did. The two `raise X from err` sites in
//! `broker.py` (`_as_decimal`, `load_mirror_accounts`) are module-level helpers that stay
//! Python, so their cause chains survive unchanged.

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::sync::GILOnceCell;
use pyo3::types::{PyDict, PyList, PyModule, PyTuple};

use te_core::ledger::json::Json;
use te_core::ledger::model::{LErr, R as LR};
use te_core::tos_paper::broker::{Broker, Class, Fault, Host as BrokerHost, Raised};
use te_core::tos_paper::RAISED;

use crate::plain::{plain_of, plain_py};
use crate::sim::Host;

/// `plain_of` a Python list: the broker takes `&[Json]`.
fn list_of(value: &Bound<'_, PyAny>) -> PyResult<Vec<Json>> {
    match plain_of(value)? {
        Json::Arr(a) => Ok(a),
        _ => Err(PyValueError::new_err(("tos_wire", "a list was expected".to_string()))),
    }
}

/// `_rs.call`'s refusal mapping: `sim._rs.refusal(kind, message)` builds the registered
/// exception instance (e.g. `TosPaperBrokerError`, `VenueUnreadable`), raised as-is.
fn refusal(py: Python<'_>, kind: &str, msg: String) -> PyErr {
    match py
        .import("trade_engine.sim._rs")
        .and_then(|m| m.getattr("refusal"))
        .and_then(|f| f.call1((kind, msg)))
    {
        Ok(value) => PyErr::from_value(value),
        Err(error) => error,
    }
}

/// `TosPaperBrokerError(msg)`.
fn broker_error(py: Python<'_>, msg: &str) -> PyErr {
    match py
        .import("trade_engine.tos_paper.broker")
        .and_then(|m| m.getattr("TosPaperBrokerError"))
        .and_then(|c| c.call1((msg,)))
    {
        Ok(value) => PyErr::from_value(value),
        Err(error) => error,
    }
}

/// The Python classes and modules `TosPaperBrokerBase` reaches, imported once.
struct Ctx {
    rs: Py<PyModule>,          // trade_engine.tos_paper._rs
    broker: Py<PyModule>,      // trade_engine.tos_paper.broker (module)
    codec: Py<PyModule>,       // trade_engine.ledger.codec (module)
    decimal: Py<PyAny>,        // decimal.Decimal
    datetime: Py<PyAny>,       // datetime.datetime
    order_state: Py<PyAny>,    // trade_engine.domain.orders.OrderState
    venue_identity: Py<PyAny>, // trade_engine.interfaces.broker.VenueIdentity
    venue_position: Py<PyAny>, // trade_engine.interfaces.broker.VenuePosition
    account_record: Py<PyAny>, // trade_engine.interfaces.broker.AccountRecord
    account_snapshot: Py<PyAny>, // trade_engine.interfaces.broker.AccountSnapshot
    route: Py<PyAny>,          // trade_engine.interfaces.broker.Route
    poll_and_diff: Py<PyAny>,  // trade_engine.accounts.poll.PollAndDiff
    mirror_fill: Py<PyAny>,    // trade_engine.ledger.events.MirrorFill
    mirror_ack: Py<PyAny>,     // trade_engine.ledger.events.MirrorAck
    netted_batch: Py<PyAny>,   // trade_engine.tos_paper.netting.NettedBatch
    drain_report: Py<PyAny>,   // trade_engine.tos_paper.broker.DrainReport
    fill_collection: Py<PyAny>, // trade_engine.tos_paper.broker.FillCollection
}

impl Ctx {
    fn new(py: Python<'_>) -> PyResult<Ctx> {
        let interfaces = py.import("trade_engine.interfaces.broker")?;
        let orders = py.import("trade_engine.domain.orders")?;
        let events = py.import("trade_engine.ledger.events")?;
        let accounts_poll = py.import("trade_engine.accounts.poll")?;
        let broker = py.import("trade_engine.tos_paper.broker")?;
        let decimal = py.import("decimal")?;
        let datetime = py.import("datetime")?;
        Ok(Ctx {
            rs: py.import("trade_engine.tos_paper._rs")?.unbind(),
            broker: broker.clone().unbind(),
            codec: py.import("trade_engine.ledger.codec")?.unbind(),
            decimal: decimal.getattr("Decimal")?.unbind(),
            datetime: datetime.getattr("datetime")?.unbind(),
            order_state: orders.getattr("OrderState")?.unbind(),
            venue_identity: interfaces.getattr("VenueIdentity")?.unbind(),
            venue_position: interfaces.getattr("VenuePosition")?.unbind(),
            account_record: interfaces.getattr("AccountRecord")?.unbind(),
            account_snapshot: interfaces.getattr("AccountSnapshot")?.unbind(),
            route: interfaces.getattr("Route")?.unbind(),
            poll_and_diff: accounts_poll.getattr("PollAndDiff")?.unbind(),
            mirror_fill: events.getattr("MirrorFill")?.unbind(),
            mirror_ack: events.getattr("MirrorAck")?.unbind(),
            netted_batch: py.import("trade_engine.tos_paper.netting")?.getattr("NettedBatch")?.unbind(),
            drain_report: broker.getattr("DrainReport")?.unbind(),
            fill_collection: broker.getattr("FillCollection")?.unbind(),
        })
    }
}

fn ctx(py: Python<'_>) -> PyResult<&'static Ctx> {
    static CTX: GILOnceCell<Ctx> = GILOnceCell::new();
    CTX.get_or_try_init(py, || Ctx::new(py))
}

/// `("ok", value, "", "")` or `("exc", class, type name, str(exc))`: the host's tagged answer.
type Tagged = (String, Py<PyAny>, String, String);

/// The `Broker`'s host shim: it answers the clock and the transport reads/sends/cancels by
/// calling the Python `_Host` adapter, and classifies a transport exception the way
/// `_Host._ask` tagged it. A refusal crosses as `(kind, message)`, mapped by [`refusal`]; a
/// `TransportUnavailable` the broker let through is re-raised by `_Host.reraise()` as the
/// very object the transport raised.
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

    /// The `Broker`'s `LErr` as a `PyErr`: a transport exception the broker let through is
    /// re-raised by `reraise()`; a genuine host crash is the stashed error; anything else
    /// is the `(kind, message)` refusal, mapped through `sim._rs.refusal`.
    fn finish<T>(self, r: LR<T>) -> PyResult<T> {
        let py = self.host.py();
        match r {
            Ok(v) => Ok(v),
            Err(e) if e.kind == RAISED => match self.host.call_method0("reraise") {
                Err(original) => Err(original),
                Ok(_) => Err(refusal(py, e.kind, e.msg)),
            },
            Err(e) if e.kind == "host" => Err(self.stash.take()),
            Err(e) => Err(refusal(py, e.kind, e.msg)),
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

/// `TosPaperBroker`'s decision state and method bodies. The Python subclass keeps a
/// `__dict__`, so the existing tests can still subclass it and monkeypatch instance
/// attributes and methods; every public method here is reached through normal Python
/// attribute lookup.
#[pyclass(subclass, module = "trade_engine_rs")]
pub struct TosPaperBrokerBase {
    core: Broker,
}

#[pymethods]
impl TosPaperBrokerBase {
    #[new]
    #[pyo3(signature = (transport, binding, *, clock, balance_reader=None, balance_unproven_ok=false, halted_venues=None, mirror_path=None, account_ledger=None))]
    fn new(
        py: Python<'_>,
        transport: &Bound<'_, PyAny>,
        binding: &Bound<'_, PyAny>,
        clock: &Bound<'_, PyAny>,
        balance_reader: Option<&Bound<'_, PyAny>>,
        balance_unproven_ok: bool,
        halted_venues: Option<&Bound<'_, PyAny>>,
        mirror_path: Option<&Bound<'_, PyAny>>,
        account_ledger: Option<&Bound<'_, PyAny>>,
    ) -> PyResult<Self> {
        let _ = (transport, clock, balance_reader, balance_unproven_ok, mirror_path, account_ledger);
        let venue: String = binding.getattr("venue_account")?.extract()?;
        let mirrored_list = py.import("builtins")?.call_method1("list", (binding.getattr("mirrored_accounts")?,))?;
        let mirrored: Vec<String> = mirrored_list.extract()?;
        let frozen = match halted_venues {
            Some(hv) => py.import("builtins")?.getattr("frozenset")?.call1((hv,))?,
            None => py.import("builtins")?.getattr("frozenset")?.call0()?,
        };
        let halted: bool = frozen.call_method1("__contains__", (venue.as_str(),))?.extract()?;
        Ok(TosPaperBrokerBase { core: Broker::new(&venue, mirrored, halted) })
    }

    // -- the public attributes -----------------------------------------------------

    #[getter]
    fn venue(slf: &Bound<'_, Self>) -> PyResult<String> {
        Ok(slf.as_any().getattr("binding")?.getattr("venue_account")?.extract()?)
    }

    #[getter]
    fn halted(slf: &Bound<'_, Self>) -> bool {
        slf.borrow().core.halted()
    }

    #[getter]
    fn queued<'py>(slf: &Bound<'py, Self>) -> PyResult<Py<PyAny>> {
        let py = slf.py();
        let state = slf.borrow().core.state();
        let queued = state.get("queued").cloned().unwrap_or(Json::Arr(Vec::new()));
        let rs = ctx(py)?.rs.bind(py);
        let mut out: Vec<Bound<'py, PyAny>> = Vec::new();
        if let Json::Arr(items) = queued {
            for d in items {
                let d_py = plain_py(py, &d)?;
                out.push(rs.call_method1("venue_order", (&d_py,))?);
            }
        }
        Ok(PyTuple::new(py, out)?.into_any().unbind())
    }

    // -- connection ----------------------------------------------------------------

    fn connect<'py>(slf: &Bound<'py, Self>) -> PyResult<Py<PyAny>> {
        let py = slf.py();
        let any = slf.as_any();
        let transport = any.getattr("transport")?;
        let identity = transport.call_method0("connect")?;
        let number_v = identity.call_method1("get", ("number", ""))?;
        let number: String = number_v.str()?.extract()?;
        let kind_v = identity.call_method1("get", ("type", ""))?;
        let kind_raw: String = kind_v.str()?.extract()?;
        let kind = kind_raw.trim().to_lowercase();
        let binding = any.getattr("binding")?;
        let venue_account: String = binding.getattr("venue_account")?.extract()?;
        let account_type: String = binding.getattr("account_type")?.extract()?;
        let minimum_balance = binding.getattr("minimum_balance")?;

        if number != venue_account {
            return Err(broker_error(
                py,
                &format!("transport connected '{number}', binding is '{venue_account}' — refusing to mirror"),
            ));
        }
        if kind != account_type {
            let kind_repr = if kind.is_empty() { "'unknown'".to_string() } else { format!("'{kind}'") };
            return Err(broker_error(
                py,
                &format!("{number} is a {kind_repr} account, binding needs '{account_type}' — refusing to mirror"),
            ));
        }

        let balance = Self::balance(slf)?;
        let balance_bound = balance.bind(py);
        if balance_bound.is_none() {
            if !any.getattr("_balance_unproven_ok")?.is_truthy()? {
                let min_text: String = minimum_balance.str()?.extract()?;
                return Err(broker_error(
                    py,
                    &format!(
                        "{number}: no balance reader, so the §4.7 minimum {min_text} is unproven — refusing to mirror (pass balance_unproven_ok=True to accept that explicitly)"
                    ),
                ));
            }
            any.setattr("balance_proven", false)?;
        } else {
            if balance_bound.lt(&minimum_balance)? {
                let balance_text: String = balance_bound.str()?.extract()?;
                let min_text: String = minimum_balance.str()?.extract()?;
                return Err(broker_error(
                    py,
                    &format!("{number} balance {balance_text} is under the §4.7 minimum {min_text}; refusing to mirror"),
                ));
            }
            any.setattr("balance_proven", true)?;
        }

        let env = any.getattr("env")?;
        let name = any.getattr("name")?;
        let connected_at = any.getattr("_clock")?.call_method0("now_utc")?;
        let kwargs = PyDict::new(py);
        kwargs.set_item("account_id", venue_account.as_str())?;
        kwargs.set_item("env", &env)?;
        kwargs.set_item("connected_at", &connected_at)?;
        kwargs.set_item("broker_name", &name)?;
        let identity_obj = ctx(py)?.venue_identity.bind(py).call((), Some(&kwargs))?;
        any.setattr("_identity", &identity_obj)?;
        slf.borrow_mut().core.mark_connected();
        Ok(identity_obj.unbind())
    }

    fn balance<'py>(slf: &Bound<'py, Self>) -> PyResult<Py<PyAny>> {
        let py = slf.py();
        let reader = slf.as_any().getattr("_balance_reader")?;
        if reader.is_none() {
            return Ok(py.None().into_bound(py).unbind());
        }
        let value = reader.call_method0("net_liquidation")?;
        let as_decimal = ctx(py)?.broker.bind(py).getattr("_as_decimal")?;
        Ok(as_decimal.call1((value, "net liquidation"))?.unbind())
    }

    // -- the mirror layer (sim critical path: no transport calls) ------------------

    fn mirror_batch<'py>(
        slf: &Bound<'py, Self>,
        strategy_orders: &Bound<'py, PyAny>,
        holdings: &Bound<'py, PyAny>,
    ) -> PyResult<Py<PyAny>> {
        let py = slf.py();
        let rs = ctx(py)?.rs.bind(py);
        let orders_list = py.import("builtins")?.call_method1("list", (strategy_orders,))?;
        let order_doc_fn = rs.getattr("order_doc")?;
        let order_docs = PyList::empty(py);
        for o in orders_list.try_iter()? {
            order_docs.append(order_doc_fn.call1((o?,))?)?;
        }
        let holdings_doc = rs.call_method1("holdings_doc", (holdings,))?;
        let orders = list_of(&order_docs)?;
        let holdings_v = list_of(&holdings_doc)?;

        let host = slf.as_any().getattr("_host")?;
        let mut h = PyBrokerHost::new(&host);
        let r = slf.borrow_mut().core.mirror_batch(&mut h, &orders, &holdings_v);
        let out = h.finish(r)?;
        let out_py = plain_py(py, &out)?;

        let mut vos: Vec<Bound<'py, PyAny>> = Vec::new();
        for d in out_py.get_item("venue_orders")?.try_iter()? {
            vos.push(rs.call_method1("venue_order", (d?,))?);
        }
        let venue_orders = PyTuple::new(py, vos)?;

        let mut refs: Vec<Bound<'py, PyAny>> = Vec::new();
        for pair in out_py.get_item("refused")?.try_iter()? {
            let pair = pair?;
            let o = pair.get_item(0)?;
            let r = pair.get_item(1)?;
            refs.push(PyTuple::new(py, [o, r])?.into_any());
        }
        let refused = PyTuple::new(py, refs)?;

        let kwargs = PyDict::new(py);
        kwargs.set_item("venue_orders", venue_orders)?;
        kwargs.set_item("refused", refused)?;
        Ok(ctx(py)?.netted_batch.bind(py).call((), Some(&kwargs))?.unbind())
    }

    // -- the ledger's memory (restart-safe) ----------------------------------------

    #[pyo3(signature = (mirror, *, halted_venues=None))]
    fn restore<'py>(
        slf: &Bound<'py, Self>,
        mirror: &Bound<'py, PyAny>,
        halted_venues: Option<&Bound<'py, PyAny>>,
    ) -> PyResult<()> {
        let py = slf.py();
        let mirror_canon = ctx(py)?.codec.bind(py).call_method1("canon", (mirror,))?;
        let mirror_json = plain_of(&mirror_canon)?;
        let halted_list = match halted_venues {
            Some(hv) => py.import("builtins")?.call_method1("list", (hv,))?,
            None => PyList::empty(py).into_any(),
        };
        let halted: Vec<String> = halted_list.extract()?;
        slf.borrow_mut()
            .core
            .restore(&mirror_json, &halted)
            .map_err(|e| refusal(py, e.kind, e.msg))
    }

    fn collect_fills<'py>(
        slf: &Bound<'py, Self>,
        mirror: &Bound<'py, PyAny>,
    ) -> PyResult<Py<PyAny>> {
        let py = slf.py();
        let mirror_canon = ctx(py)?.codec.bind(py).call_method1("canon", (mirror,))?;
        let mirror_json = plain_of(&mirror_canon)?;

        let host = slf.as_any().getattr("_host")?;
        let mut h = PyBrokerHost::new(&host);
        let r = slf.borrow_mut().core.collect_fills(&mut h, &mirror_json);
        let out = h.finish(r)?;
        let out_py = plain_py(py, &out)?;

        let ctx = ctx(py)?;
        let decimal = ctx.decimal.bind(py);
        let datetime = ctx.datetime.bind(py);
        let rs = ctx.rs.bind(py);

        let mut fills: Vec<Bound<'py, PyAny>> = Vec::new();
        for f in out_py.get_item("fills")?.try_iter()? {
            let f = f?;
            let kwargs = PyDict::new(py);
            kwargs.set_item("venue", f.get_item("venue")?)?;
            kwargs.set_item("ticket_key", f.get_item("ticket_key")?)?;
            kwargs.set_item("venue_order_id", f.get_item("venue_order_id")?)?;
            kwargs.set_item("filled", decimal.call1((f.get_item("filled")?,))?)?;
            kwargs.set_item("avg_price", rs.call_method1("dec", (f.get_item("avg_price")?,))?)?;
            kwargs.set_item("at", datetime.call_method1("fromisoformat", (f.get_item("at")?,))?)?;
            fills.push(ctx.mirror_fill.bind(py).call((), Some(&kwargs))?);
        }
        let fills = PyTuple::new(py, fills)?;

        let mut closes: Vec<Bound<'py, PyAny>> = Vec::new();
        for c in out_py.get_item("closes")?.try_iter()? {
            let c = c?;
            let kwargs = PyDict::new(py);
            kwargs.set_item("venue", c.get_item("venue")?)?;
            kwargs.set_item("ticket_key", c.get_item("ticket_key")?)?;
            kwargs.set_item("status", c.get_item("status")?)?;
            kwargs.set_item("message", c.get_item("message")?)?;
            kwargs.set_item("at", datetime.call_method1("fromisoformat", (c.get_item("at")?,))?)?;
            kwargs.set_item("venue_order_id", c.get_item("venue_order_id")?)?;
            kwargs.set_item("book_status", ctx.order_state.bind(py).call1((c.get_item("book_status")?,))?)?;
            closes.push(ctx.mirror_ack.bind(py).call((), Some(&kwargs))?);
        }
        let closes = PyTuple::new(py, closes)?;

        let kwargs = PyDict::new(py);
        kwargs.set_item("fills", fills)?;
        kwargs.set_item("closes", closes)?;
        Ok(ctx.fill_collection.bind(py).call((), Some(&kwargs))?.unbind())
    }

    // -- the slow path (host-driven, off the sim critical path) ---------------------

    fn preflight<'py>(slf: &Bound<'py, Self>) -> PyResult<()> {
        let host = slf.as_any().getattr("_host")?;
        let mut h = PyBrokerHost::new(&host);
        let r = slf.borrow_mut().core.preflight(&mut h);
        h.finish(r)
    }

    fn drain<'py>(slf: &Bound<'py, Self>) -> PyResult<Py<PyAny>> {
        let py = slf.py();
        let host = slf.as_any().getattr("_host")?;
        let mut h = PyBrokerHost::new(&host);
        let r = slf.borrow_mut().core.drain(&mut h);
        let out = h.finish(r)?;
        let out_py = plain_py(py, &out)?;

        let ctx = ctx(py)?;
        let rs = ctx.rs.bind(py);

        let mut acks: Vec<Bound<'py, PyAny>> = Vec::new();
        for a in out_py.get_item("acks")?.try_iter()? {
            acks.push(rs.call_method1("ack", (a?,))?);
        }
        let acks = PyTuple::new(py, acks)?;

        let reconcile_py = out_py.get_item("reconcile")?;
        let reconcile = if reconcile_py.is_none() {
            py.None().into_bound(py)
        } else {
            rs.call_method1("event", (&reconcile_py,))?
        };

        let proven_dict = PyDict::new(py);
        for item in out_py.get_item("proven")?.try_iter()? {
            let item = item?;
            let key = item.get_item(0)?;
            let pair = item.get_item(1)?;
            let oid = pair.get_item(0)?;
            let state = ctx.order_state.bind(py).call1((pair.get_item(1)?,))?;
            proven_dict.set_item(key, PyTuple::new(py, [oid, state])?)?;
        }

        let kwargs = PyDict::new(py);
        kwargs.set_item("acks", acks)?;
        kwargs.set_item("reconcile", reconcile)?;
        kwargs.set_item("proven", proven_dict)?;
        Ok(ctx.drain_report.bind(py).call((), Some(&kwargs))?.unbind())
    }

    #[pyo3(signature = (*, defer_unavailable=false))]
    fn reconcile_now<'py>(
        slf: &Bound<'py, Self>,
        defer_unavailable: bool,
    ) -> PyResult<Py<PyAny>> {
        let py = slf.py();
        let host = slf.as_any().getattr("_host")?;
        let mut h = PyBrokerHost::new(&host);
        let r = slf.borrow_mut().core.reconcile_now(&mut h, defer_unavailable);
        let out = h.finish(r)?;
        let out_py = plain_py(py, &out)?;
        Ok(ctx(py)?.rs.bind(py).call_method1("event", (&out_py,))?.unbind())
    }

    fn submit<'py>(slf: &Bound<'py, Self>, order: &Bound<'py, PyAny>) -> PyResult<Py<PyAny>> {
        let py = slf.py();
        let rs = ctx(py)?.rs.bind(py);
        let ticket_doc = rs.call_method1("venue_order_doc", (order,))?;
        let ticket = plain_of(&ticket_doc)?;
        let host = slf.as_any().getattr("_host")?;
        let mut h = PyBrokerHost::new(&host);
        let r = slf.borrow_mut().core.submit(&mut h, &ticket);
        let out = h.finish(r)?;
        let out_py = plain_py(py, &out)?;
        Ok(rs.call_method1("ack", (&out_py,))?.unbind())
    }

    fn cancel<'py>(slf: &Bound<'py, Self>, venue_order_id: &str) -> PyResult<Py<PyAny>> {
        let py = slf.py();
        let host = slf.as_any().getattr("_host")?;
        let mut h = PyBrokerHost::new(&host);
        let r = slf.borrow_mut().core.cancel(&mut h, venue_order_id);
        let out = h.finish(r)?;
        let out_py = plain_py(py, &out)?;
        Ok(ctx(py)?.rs.bind(py).call_method1("ack", (&out_py,))?.unbind())
    }

    fn proven_order_id(slf: &Bound<'_, Self>, ticket_key: &str) -> Option<String> {
        match slf.borrow().core.proven_order_id(ticket_key) {
            Json::Str(id) => Some(id),
            _ => None,
        }
    }

    fn replace<'py>(
        slf: &Bound<'py, Self>,
        venue_order_id: &Bound<'py, PyAny>,
        changes: &Bound<'py, PyAny>,
    ) -> PyResult<Py<PyAny>> {
        let _ = (venue_order_id, changes);
        Err(broker_error(
            slf.py(),
            "replace (TOS Cancel/replace order) is not mapped over JAB yet; cancel() and submit a new ticket instead",
        ))
    }

    fn orders<'py>(slf: &Bound<'py, Self>, since: &Bound<'py, PyAny>) -> PyResult<Py<PyAny>> {
        let _ = since;
        Err(broker_error(
            slf.py(),
            "venue order states cannot be read per venue order id over JAB yet; use reconcile_now() (Order Book vs mirror book)",
        ))
    }

    fn fills<'py>(slf: &Bound<'py, Self>, since: &Bound<'py, PyAny>) -> PyResult<Py<PyAny>> {
        let _ = since;
        Err(broker_error(
            slf.py(),
            "venue fills are not read per fill id (not wired); use collect_fills(mirror), which reads cumulative fills per venue Order ID into the mirror ledger",
        ))
    }

    fn positions<'py>(slf: &Bound<'py, Self>) -> PyResult<Py<PyAny>> {
        let py = slf.py();
        let host = slf.as_any().getattr("_host")?;
        let mut h = PyBrokerHost::new(&host);
        let r = slf.borrow_mut().core.positions(&mut h);
        let out = h.finish(r)?;
        let out_py = plain_py(py, &out)?;

        let ctx = ctx(py)?;
        let rs = ctx.rs.bind(py);
        let decimal = ctx.decimal.bind(py);
        let datetime = ctx.datetime.bind(py);
        let mut pos: Vec<Bound<'py, PyAny>> = Vec::new();
        for p in out_py.try_iter()? {
            let p = p?;
            let instrument = rs.call_method1("unwire", (p.get_item("instrument")?,))?;
            let quantity = decimal.call1((p.get_item("quantity")?,))?;
            let avg_price = decimal.call1((p.get_item("avg_price")?,))?;
            let as_of = datetime.call_method1("fromisoformat", (p.get_item("as_of")?,))?;
            pos.push(ctx.venue_position.bind(py).call1((instrument, quantity, avg_price, as_of))?);
        }
        Ok(PyList::new(py, pos)?.into_any().unbind())
    }

    fn cash_events<'py>(slf: &Bound<'py, Self>, since: &Bound<'py, PyAny>) -> PyResult<Py<PyAny>> {
        let _ = since;
        Err(broker_error(slf.py(), "venue cash events (assignment/exercise) cannot be read yet"))
    }

    // -- the account port (BROKER_PLATFORM phase 2) ----------------------------------

    fn accounts<'py>(slf: &Bound<'py, Self>) -> PyResult<Py<PyAny>> {
        let py = slf.py();
        let any = slf.as_any();
        let clock = any.getattr("_clock")?;
        let confirmed_at = clock.call_method0("now_utc")?;
        let mirror_path = any.getattr("_mirror_path")?;
        let binding = any.getattr("binding")?;
        let venue_account: String = binding.getattr("venue_account")?.extract()?;
        let env = any.getattr("env")?;
        let ctx = ctx(py)?;

        let snapshot = (|| -> PyResult<Py<PyAny>> {
            let records;
            if mirror_path.is_none() {
                let route = ctx.route.bind(py).call1(("tos", venue_account.as_str()))?;
                let routes = PyTuple::new(py, [route])?;
                let kwargs = PyDict::new(py);
                kwargs.set_item("env", &env)?;
                kwargs.set_item("status", "active")?;
                kwargs.set_item("account_type", binding.getattr("account_type")?)?;
                kwargs.set_item("routes", routes)?;
                kwargs.set_item("source", "tos_paper")?;
                kwargs.set_item("confirmed_at", &confirmed_at)?;
                let record = ctx
                    .account_record
                    .bind(py)
                    .call_method("of", ("tos_paper", venue_account.as_str()), Some(&kwargs))?;
                records = PyTuple::new(py, [record])?.into_any();
            } else {
                let load = ctx.broker.bind(py).getattr("load_mirror_accounts")?;
                let kwargs = PyDict::new(py);
                kwargs.set_item("clock", &clock)?;
                records = load.call((mirror_path,), Some(&kwargs))?;
            }
            let kwargs = PyDict::new(py);
            kwargs.set_item("records", records)?;
            kwargs.set_item("confirmed_at", &confirmed_at)?;
            Ok(ctx.account_snapshot.bind(py).call((), Some(&kwargs))?.unbind())
        })();

        match snapshot {
            Ok(snap) => {
                any.setattr("_last_accounts", &snap)?;
                Ok(snap)
            }
            Err(e) => {
                let last = any.getattr("_last_accounts")?;
                if last.is_none() {
                    Err(e)
                } else {
                    Ok(last.unbind())
                }
            }
        }
    }

    fn account_events<'py>(slf: &Bound<'py, Self>, since: &Bound<'py, PyAny>) -> PyResult<Py<PyAny>> {
        let py = slf.py();
        let any = slf.as_any();
        let poller = any.getattr("_poller")?;
        let poller = if poller.is_none() {
            let kwargs = PyDict::new(py);
            kwargs.set_item("clock", any.getattr("_clock")?)?;
            kwargs.set_item("ledger", any.getattr("_account_ledger")?)?;
            kwargs.set_item("stale_after", any.getattr("stale_after")?)?;
            kwargs.set_item("broker", "tos_paper")?;
            let p = ctx(py)?.poll_and_diff.bind(py).call((any,), Some(&kwargs))?;
            any.setattr("_poller", &p)?;
            p
        } else {
            poller
        };
        Ok(poller.call_method1("account_events", (since,))?.unbind())
    }
}

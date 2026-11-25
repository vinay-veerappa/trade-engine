use chrono::{NaiveDate, NaiveTime};
use pyo3::exceptions::{PyException, PyKeyError, PyTypeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict, PyList, PyString, PyTuple};
use te_core::ledger::model::{parse_datetime, DateTime, Instrument, R as LR};
use te_core::money::Money;
use te_core::runtime::eod_glue::Ledger;
use te_core::runtime::intraday as typed;
use te_core::runtime::intraday_flow::{self as flow, Host as FlowHost};
use te_core::runtime::intraday_glue as glue;

use crate::store_ledger::{
    date_of, instrument_of, instrument_py, micros, money, require_native_intraday, with_native, Pending, PyClock,
    StoreLedger,
};

type Value<'py> = Bound<'py, PyAny>;

fn iso(value: &Value<'_>) -> PyResult<String> { value.call_method0("isoformat")?.extract() }
fn text(value: &Value<'_>) -> PyResult<String> { value.str()?.extract() }
fn kwargs<'py>(py: Python<'py>, alert: bool, exited: bool) -> PyResult<Bound<'py, PyDict>> {
    let args = PyDict::new(py);
    if alert { args.set_item("alert", true)?; }
    if exited { args.set_item("exited", true)?; }
    Ok(args)
}

struct Host<'py> {
    service: Value<'py>,
    session: Value<'py>,
    state: Value<'py>,
    start: Value<'py>,
    stop: Value<'py>,
    open: Option<Value<'py>>,
    close: Option<Value<'py>>,
    flat: Option<i64>,
}

impl<'py> Host<'py> {
    fn module(&self) -> PyResult<Bound<'py, PyModule>> { self.service.py().import("trade_engine.intraday.service") }
    fn config(&self) -> PyResult<Value<'py>> { self.service.getattr("_config") }
    fn clock(&self) -> PyResult<Value<'py>> { self.service.getattr("_clock") }
    fn calendar(&self) -> PyResult<Value<'py>> { self.service.getattr("_calendar") }
    fn ledger(&self) -> PyResult<Value<'py>> { self.service.getattr("_ledger") }
    fn account(&self) -> PyResult<Value<'py>> { self.config()?.getattr("account_id") }
    fn manager(&self) -> PyResult<Value<'py>> {
        self.service.getattr("_router")?.call_method1("manager", (self.account()?,))
    }
    fn increment(&self, count: &Value<'py>) -> PyResult<()> {
        let tally = self.state.getattr("tally")?;
        let args = (tally.getattr("exit_actions")?, count);
        let result = self.service.py().import("operator")?.call_method1("add", args)?;
        tally.setattr("exit_actions", result)
    }
    fn advance(&self, target: &Value<'py>) -> PyResult<()> {
        intraday_advance(self.service.clone(), target.clone())
    }
    fn error_text(&self, error: &PyErr) -> PyResult<(String, String)> {
        let value = error.value(self.service.py());
        Ok((value.get_type().getattr("__name__")?.extract()?, text(value.as_any())?))
    }
    fn native<T>(&self, f: impl FnOnce(&mut StoreLedger<'_>, &PyClock<'_, '_>) -> LR<T>) -> PyResult<T> {
        native_on(&self.service, f)
    }
    fn account_text(&self) -> PyResult<String> { self.account()?.extract() }
    fn underlying(&self) -> PyResult<String> { self.config()?.getattr("underlying")?.extract() }
    fn job(&self) -> PyResult<String> { self.config()?.getattr("job_name")?.extract() }
    fn eod_job(&self) -> PyResult<String> { self.config()?.getattr("eod_job_name")?.extract() }
    fn cause_text(&self) -> PyResult<String> {
        glue::cause(&self.job()?, &self.account_text()?, date_of(&self.session)?).map_err(super::flow_refusal)
    }
    fn venue_name(&self) -> PyResult<String> {
        let broker = self.config()?.getattr("broker")?;
        if broker.hasattr("name")? { broker.getattr("name")?.extract() } else { broker.get_type().getattr("__name__")?.extract() }
    }
    fn venue_positions(&self) -> PyResult<Vec<(String, Money)>> {
        let broker = self.config()?.getattr("broker")?;
        let mut positions = Vec::new();
        for position in broker.call_method0("positions")?.try_iter()? {
            let position = position?;
            let symbol: String = position.getattr("instrument")?.getattr("symbol")?.extract()?;
            positions.push((symbol, money(&position.getattr("quantity")?)?));
        }
        Ok(positions)
    }
    /// `_require_previous_eod_complete`, natively: own marker, the previous session's marker, then `typed::previous`.
    fn previous_guard(&mut self) -> PyResult<()> {
        let cause = self.cause_text()?;
        if self.native(|l, _| glue::present(&*l, &cause))? { return Ok(()); }
        let previous = self.calendar()?.call_method1("previous_session", (&self.session,))?;
        let previous_day = date_of(&previous)?;
        let account = self.account_text()?;
        let eod_job = self.eod_job()?;
        let marker = glue::eod_marker(&eod_job, &account, previous_day).map_err(super::flow_refusal)?;
        if self.native(|l, _| glue::present(&*l, &marker))? { return Ok(()); }
        let has_history = self.native(|l, _| glue::has_history(&*l, &account))?;
        typed::previous(has_history, &iso(&self.session)?, &account, &iso(&previous)?, &eod_job, &marker)
            .map_err(super::flow_refusal)
    }
}

impl<'py> FlowHost for Host<'py> {
    type Error = PyErr;
    type Value = Value<'py>;
    fn validate(&mut self) -> PyResult<()> {
        let calendar = self.calendar()?;
        let session_text = iso(&self.session)?;
        let exchange: String = calendar.getattr("exchange")?.extract()?;
        let is_session = calendar.call_method1("is_session", (&self.session,))?.is_truthy()?;
        typed::session(is_session, &session_text, &exchange).map_err(super::flow_refusal)?;
        Ok(())
    }
    fn init_state(&mut self) -> PyResult<()> {
        self.state = self.module()?.getattr("_TickState")?.call0()?;
        Ok(())
    }
    fn settled(&mut self) -> PyResult<bool> {
        let (eod_job, account, session) = (self.eod_job()?, self.account_text()?, date_of(&self.session)?);
        self.native(|l, _| glue::settled(&*l, &eod_job, &account, session))
    }
    fn admit(&mut self) -> PyResult<()> {
        intraday_live_guard(self.service.clone(), self.session.clone())?;
        self.previous_guard()
    }
    fn beat(&mut self, note: &str, alert: bool, exited: bool) -> PyResult<()> {
        self.service.call_method("_write_heartbeat", (&self.session, &self.state, note),
            Some(&kwargs(self.service.py(), alert, exited)?))?;
        Ok(())
    }
    fn connect_restore(&mut self) -> PyResult<()> {
        self.config()?.getattr("broker")?.call_method0("connect")?;
        intraday_restore(self.service.clone(), self.session.clone(), self.state.clone())?;
        Ok(())
    }
    fn boundaries(&mut self) -> PyResult<()> {
        self.open = Some(self.calendar()?.call_method1("session_open", (&self.session,))?);
        self.close = Some(self.calendar()?.call_method1("session_close", (&self.session,))?);
        Ok(())
    }
    fn start(&mut self) -> PyResult<()> {
        if !self.start.is_none() { self.advance(&self.start)?; }
        Ok(())
    }
    fn can_advance(&mut self) -> PyResult<bool> {
        let method = self.clock()?.getattr("advance_to").or_else(|error| {
            if error.is_instance_of::<pyo3::exceptions::PyAttributeError>(self.service.py()) {
                Ok(self.service.py().None().into_bound(self.service.py()))
            } else { Err(error) }
        })?;
        Ok(method.is_callable())
    }
    fn advance_open(&mut self) -> PyResult<()> { self.advance(self.open.as_ref().unwrap()) }
    fn before_open(&mut self) -> PyResult<bool> { self.now()?.lt(self.open.as_ref().unwrap()) }
    fn wait_beat(&mut self) -> PyResult<()> {
        self.beat(&format!("waiting for the open at {}", iso(self.open.as_ref().unwrap())?), false, false)
    }
    fn wait_sleep(&mut self) -> PyResult<()> {
        let now = self.now()?;
        let remaining = self.open.as_ref().unwrap().call_method1("__sub__", (now,))?
            .call_method0("total_seconds")?;
        let builtins = self.service.py().import("builtins")?;
        let duration = builtins.call_method1("min", (self.config()?.getattr("tick_seconds")?, remaining))?;
        let duration = builtins.call_method1("max", (duration, 0.001))?;
        self.clock()?.call_method1("sleep", (duration,))?;
        Ok(())
    }
    fn wait(&mut self) -> PyResult<()> {
        intraday_wait(self.service.clone(), self.open.as_ref().unwrap().clone(),
            self.session.clone(), self.state.clone())
    }
    fn before_close(&mut self) -> PyResult<bool> { self.now()?.lt(self.close.as_ref().unwrap()) }
    fn stopped(&mut self) -> PyResult<bool> { Ok(!self.stop.is_none() && self.now()?.ge(&self.stop)?) }
    fn result(&mut self, settled: bool, stopped: bool) -> PyResult<Value<'py>> {
        let py = self.service.py();
        let result = PyDict::new(py);
        result.set_item("session", &self.session)?;
        result.set_item("account_id", self.config()?.getattr("account_id")?)?;
        let tally = self.state.getattr("tally")?;
        result.set_item("orders_submitted", tally.getattr("orders_submitted")?)?;
        result.set_item("exit_actions", tally.getattr("exit_actions")?)?;
        result.set_item("snapshots_processed", tally.getattr("snapshots_processed")?)?;
        result.set_item("entries_dropped", self.service.getattr("_gate")?.getattr("dropped")?)?;
        if settled { result.set_item("settled", true)?; }
        if stopped {
            let now = self.now()?;
            result.set_item("stopped_at", iso(&now)?)?;
        }
        Ok(result.into_any())
    }
    fn now(&mut self) -> PyResult<Value<'py>> { self.clock()?.call_method0("now_utc") }
    fn fetch(&mut self, now: &Value<'py>) -> PyResult<Value<'py>> {
        self.config()?.getattr("snapshot_source")?.call1((self.config()?.getattr("underlying")?, now))
    }
    fn fresh(&mut self, snapshot: Value<'py>, now: &Value<'py>) -> PyResult<Value<'py>> {
        fresh_view_of(&self.service, snapshot, now)
    }
    fn stale(&self, error: &PyErr) -> PyResult<bool> {
        Ok(error.is_instance_of::<PyValueError>(self.service.py()) ||
            error.matches(self.service.py(), self.module()?.getattr("StaleDataError")?)?)
    }
    fn exception(&self, error: &PyErr) -> bool { error.is_instance_of::<PyException>(self.service.py()) }
    fn stale_refusal(&mut self, error: PyErr) -> PyResult<()> {
        let (_, message) = self.error_text(&error)?;
        intraday_refuse(self.service.clone(), self.session.clone(), self.state.clone(), &message)?;
        self.beat(&format!("stale quote: {message}"), false, false)
    }
    fn clear_refusal(&mut self) -> PyResult<()> { self.state.setattr("refusing", false) }
    fn deadlines(&mut self, now: &Value<'py>) -> PyResult<(bool, bool)> {
        let config = self.config()?;
        let flat_at = clock_time(&config.getattr("flat_at")?)?;
        let entry_end = clock_time(&config.getattr("entry_end")?)?;
        let flat_before = micros_of(&config.getattr("flat_before_close")?)?;
        let entry_before = micros_of(&config.getattr("entry_before_close")?)?;
        let (entry, flat) = glue::deadlines(date_of(&self.session)?, flat_at, entry_end, flat_before, entry_before)
            .map_err(super::flow_refusal)?;
        let now_us = micros(&dt_of(now)?);
        let barred = !self.state.getattr("barred")?.is_none();
        let (flat_due, may_enter) = typed::tick(now_us, entry, flat, barred).map_err(super::flow_refusal)?;
        self.flat = Some(flat);
        Ok((flat_due, may_enter))
    }
    fn cancel(&mut self, code: &str) -> PyResult<()> {
        intraday_cancel(self.service.clone(), self.session.clone(), code)?;
        Ok(())
    }
    fn flatten(&mut self, code: &str) -> PyResult<()> {
        let flat = self.flat.expect("the deadlines are read before a flatten");
        let reason = format!("flat by {} ET", glue::flat_label(flat).map_err(super::flow_refusal)?);
        let count = self.service.call_method1("_flatten", (&self.session, code, reason))?;
        self.increment(&count)
    }
    fn gate(&mut self, allow: bool) -> PyResult<()> { self.service.getattr("_gate")?.setattr("allow", allow) }
    fn manage(&mut self, snapshot: &Value<'py>) -> PyResult<()> {
        let cause = self.cause_text()?;
        let added = self.service.getattr("_router")?.call_method1("manage_at_snapshot",
            (self.account()?, &self.session, snapshot, self.state.getattr("snapshots")?, cause))?;
        let tally = self.service.py().import("operator")?.call_method1("iadd", (self.state.getattr("tally")?, added))?;
        self.state.setattr("tally", tally)
    }
    /// `_record_tick`, natively: one position check per fresh snapshot, skipped if already written.
    fn record(&mut self, snapshot: &Value<'py>) -> PyResult<()> {
        let cause = self.cause_text()?;
        let account = self.account_text()?;
        let as_of = iso(&snapshot.getattr("as_of")?)?;
        let underlying: String = snapshot.getattr("underlying")?.extract()?;
        let command = format!("{cause}:tick:{as_of}");
        if self.native(|l, _| glue::present(&*l, &command))? { return Ok(()); }
        let venue = self.venue_name()?;
        let positions = self.venue_positions()?;
        self.native(|l, clock| glue::record_tick(l, clock, &account, &cause, &as_of, &underlying, &venue, &positions))
    }
    fn pause(&mut self) -> PyResult<()> { self.service.call_method0("_pause")?; Ok(()) }
    fn tick(&mut self) -> PyResult<()> {
        self.service.call_method1("_tick", (&self.session, &self.state))?;
        Ok(())
    }
    fn close(&mut self) -> PyResult<String> { self.service.call_method1("_close_mark", (&self.session, &self.state))?.extract() }
    fn emergency(&mut self, error: PyErr) -> PyResult<Value<'py>> {
        intraday_emergency(self.service.clone(), self.session.clone(), self.state.clone(),
            error.value(self.service.py()).as_any().clone())
    }
}

fn host<'py>(service: Value<'py>, session: Value<'py>, state: Value<'py>) -> Host<'py> {
    let none = service.py().None().into_bound(service.py());
    Host { service, session, state, start: none.clone(), stop: none, open: None, close: None, flat: None }
}

#[pyfunction]
pub(crate) fn intraday_run<'py>(service: Value<'py>, session: Value<'py>, start: Value<'py>, stop: Value<'py>) -> PyResult<Value<'py>> {
    let state = service.py().None().into_bound(service.py());
    let mut host = host(service, session, state);
    host.start = start;
    host.stop = stop;
    flow::run(&mut host)
}
#[pyfunction]
pub(crate) fn intraday_tick(service: Value<'_>, session: Value<'_>, state: Value<'_>) -> PyResult<()> {
    flow::tick(&mut host(service, session, state))
}
/// `_wait_until`: nothing ticks before the open — a replay clock jumps, a wall clock waits.
#[pyfunction]
fn intraday_wait(service: Value<'_>, target: Value<'_>, session: Value<'_>, state: Value<'_>) -> PyResult<()> {
    let mut host = host(service, session, state);
    host.open = Some(target);
    flow::wait(&mut host)
}
#[pyfunction]
fn intraday_advance(service: Value<'_>, target: Value<'_>) -> PyResult<()> {
    let py = service.py();
    let clock = service.getattr("_clock")?;
    let advance = py.import("builtins")?.call_method1("getattr", (&clock, "advance_to", py.None()))?;
    if advance.is_callable() && clock.call_method0("now_utc")?.lt(&target)? {
        advance.call1((target,))?;
    }
    Ok(())
}
/// `_go_flat_and_refuse`: stale quote — cancel working entries, ask to be flat, refuse
/// entries (I5). The command ids stay free of the stale message (it varies between the
/// first process and a resumed one) while the human reason carries the detail (I3).
#[pyfunction]
fn intraday_refuse(service: Value<'_>, session: Value<'_>, state: Value<'_>, why: &str) -> PyResult<()> {
    let mut host = host(service, session, state);
    host.state.setattr("refusing", true)?;
    host.cancel("stale-quote")?;
    let count = host.service.call_method1("_flatten", (&host.session, "stale-quote", format!("stale quote: {why}")))?;
    host.increment(&count)
}
#[pyfunction]
pub(crate) fn intraday_flatten(service: Value<'_>, session: Value<'_>, code: &str, reason: &str) -> PyResult<i64> {
    let py = service.py();
    let host = host(service, session, py.None().into_bound(py));
    let manager = host.manager()?;
    let folded = host.ledger()?.call_method1("state", (host.account()?,))?;
    let sim = py.import("trade_engine.sim")?;
    let structures = py.import("trade_engine.oms.options")?.call_method1("open_structures", (&folded,))?;
    let underlying: String = host.config()?.getattr("underlying")?.extract()?;
    let mut closed = 0;
    for structure in structures.try_iter()? {
        let structure = structure?;
        let underlying_of: String = sim.call_method1("underlying_of", (structure.getattr("instrument")?,))?.extract()?;
        let closing_sent = !structure.getattr("closing_order_id")?.is_none();
        if !typed::flatten(&underlying_of, &underlying, closing_sent).map_err(super::flow_refusal)? { continue; }
        let mut ids: Vec<String> = vec![structure.getattr("entry_order_id")?.extract()?];
        for id in folded.getattr("orders")?.try_iter()? { ids.push(id?.extract()?); }
        let attempt = typed::attempt(&ids[0], &ids).map_err(super::flow_refusal)?;
        let close = flat_close_of(&structure, &host.session, code, attempt, reason)?;
        manager.call_method1("close", (host.account()?, close))?;
        closed += 1;
    }
    Ok(closed)
}
fn sorted_orders<'py>(py: Python<'py>, folded: Value<'py>) -> PyResult<Vec<Value<'py>>> {
    let args = PyDict::new(py);
    args.set_item("key", py.import("operator")?.call_method1("attrgetter", ("order_id",))?)?;
    py.import("builtins")?.getattr("sorted")?.call((folded.getattr("orders")?.call_method0("values")?,), Some(&args))?
        .try_iter()?.collect()
}
/// `_cancel_working_entries`: cancel every entry still working — past the entry end, at the
/// sweep, when stale.
#[pyfunction]
fn intraday_cancel(service: Value<'_>, session: Value<'_>, code: &str) -> PyResult<()> {
    let py = service.py();
    let host = host(service, session, py.None().into_bound(py));
    let manager = host.manager()?;
    let folded = host.ledger()?.call_method1("state", (host.account()?,))?;
    let sim = py.import("trade_engine.sim")?;
    let domain = py.import("trade_engine.domain.option_orders")?;
    for order in sorted_orders(py, folded)? {
        let instrument = order.getattr("instrument")?;
        let is_structure = domain.call_method1("is_structure", (&instrument,))?.is_truthy()?;
        let underlying = if is_structure { sim.call_method1("underlying_of", (&instrument,))?.extract()? } else { String::new() };
        let state: String = order.getattr("state")?.getattr("value")?.extract()?;
        let configured: String = host.config()?.getattr("underlying")?.extract()?;
        let top_level = order.getattr("parent_order_id")?.is_none();
        let allowed = typed::cancel(top_level, is_structure, &state, &underlying, &configured).map_err(super::flow_refusal)?;
        if !allowed { continue; }
        let id = order.getattr("order_id")?;
        let args = PyDict::new(py);
        args.set_item("command_id", format!("intraday:cancel-entry:{}:{code}:{}", text(&id)?, iso(&host.session)?))?;
        manager.getattr("orders")?.call_method("cancel", (id,), Some(&args))?;
    }
    Ok(())
}
/// `_rehydrate`: a new process starts with an empty venue; restore it from the fold (I2).
/// Then finish what a crash left half-done: a pending venue request is carried out on the
/// rebuilt venue and read back (``oms.restore``); an entry created but never sent is
/// cancelled (it was decided on quotes that are gone); a close created but never sent is
/// sent (the decision to be flat stands).
#[pyfunction]
fn intraday_restore(service: Value<'_>, session: Value<'_>, state: Value<'_>) -> PyResult<()> {
    let py = service.py();
    let host = host(service, session, state);
    let reconcile = py.import("trade_engine.oms.reconcile")?;
    let broker = host.config()?.getattr("broker")?;
    let min = reconcile.getattr("MIN_TIME")?;
    if broker.call_method1("orders", (&min,))?.is_truthy()? || broker.call_method1("fills", (&min,))?.is_truthy()? {
        return Ok(());
    }
    let account = host.account()?;
    let account_id: String = account.extract()?;
    let ledger = host.ledger()?;
    let folded = ledger.call_method1("state", (&account,))?;
    let calendar = host.calendar()?;
    let store = require_native_intraday(&ledger, &calendar)?;
    let guard = store.borrow();

    let stash = crate::sim::Host::new();
    let r = (|| -> LR<(crate::oms_native::RestoreOutcome, Vec<Py<PyAny>>)> {
        let st = crate::oms_native::state_of(&folded, Some(account_id.as_str()), &stash)?;
        let outcome =
            crate::oms_native::restore_native(py, &*guard, account_id.as_str(), &st, "resolve", true, &[], &stash)?;
        let positions = crate::oms_native::build_positions(py, &st, &stash)?;
        Ok((outcome, positions))
    })();
    let (outcome, positions) = match r {
        Ok(v) => v,
        Err(e) => return Err(crate::oms_native::intraday_restore_refusal(py, e, &stash)),
    };

    let has_any = !outcome.orders.is_empty() || !outcome.fills.is_empty() || !positions.is_empty();
    if has_any {
        let orders = PyList::new(py, outcome.orders)?;
        let fills = PyList::new(py, outcome.fills)?;
        let positions = PyList::new(py, positions)?;
        broker.call_method1("restore", (orders, fills, positions))?;
    }
    let manager = host.manager()?;
    for id in &outcome.resolved {
        manager.getattr("orders")?.call_method1("reconcile_order", (id,))?;
    }
    if !outcome.unresolved.is_empty() {
        let dict = PyDict::new(py);
        for (id, why) in &outcome.unresolved {
            dict.set_item(id, why)?;
        }
        let joined = "; ".into_pyobject(py)?.call_method1("join", (dict.call_method0("values")?,))?;
        host.state.setattr("barred", joined)?;
    }
    let domain = py.import("trade_engine.domain.option_orders")?;
    let terminals = py.import("trade_engine.domain.orders")?.getattr("OrderState")?;
    for order in sorted_orders(py, ledger.call_method1("state", (&account,))?)? {
        let state: String = order.getattr("state")?.getattr("value")?.extract()?;
        let order_id: String = order.getattr("order_id")?.extract()?;
        let parent_id = text(&order.getattr("parent_order_id")?)?;
        let top_level = order.getattr("parent_order_id")?.is_none();
        let structure = domain.call_method1("is_structure", (order.getattr("instrument")?,))?.is_truthy()?;
        let action = typed::restart(&state, top_level, structure, &order_id, &parent_id).map_err(super::flow_refusal)?;
        match action.as_str() {
            "cancel" => {
                let args = PyDict::new(py);
                let id = order.getattr("order_id")?;
                args.set_item("command_id", format!("intraday:orphan:{}:{}", text(&id)?, iso(&host.session)?))?;
                manager.getattr("orders")?.call_method("cancel", (id,), Some(&args))?;
            }
            "submit" => {
                let sent = manager.getattr("orders")?.call_method1("submit", (&order,))?;
                let sent_state = sent.getattr("state")?;
                let mut terminal = false;
                for name in ["FILLED", "CANCELLED", "REJECTED", "EXPIRED"] {
                    if sent_state.eq(terminals.getattr(name)?)? { terminal = true; break; }
                }
                if !terminal { manager.getattr("orders")?.call_method1("reconcile_order", (order.getattr("order_id")?,))?; }
            }
            _ => {}
        }
    }
    Ok(())
}
/// `_close_plan` and the ledger half of the close, natively: the marks at a fresh snapshot taken
/// near the close (or none), then one MARK per priced symbol and the session's EOD_RUN.
#[pyfunction]
pub(crate) fn intraday_close<'py>(service: Value<'py>, session: Value<'py>, state: Value<'py>) -> PyResult<Value<'py>> {
    let py = service.py();
    let host = host(service, session, state);
    let session_day = date_of(&host.session)?;
    let account = host.account_text()?;
    let cause = host.cause_text()?;
    let job = host.job()?;
    let underlying = host.underlying()?;
    let config = host.config()?;
    let max_age: f64 = config.getattr("max_quote_age_seconds")?.extract()?;
    let window: f64 = config.getattr("close_mark_window_seconds")?.extract()?;
    let now = dt_of(&host.clock()?.call_method0("now_utc")?)?;
    let close = glue::ts_micros(&te_core::sim::session_close(session_day).map_err(super::flow_refusal)?);
    let held = native_on(&host.service, |l, _| {
        let folded = l.state(&account)?;
        glue::held_instruments(&folded)
    })?;
    let snapshot = host.state.getattr("snapshots")?.call_method1("get", (&underlying,))?;
    let has_snapshot = !snapshot.is_none();
    let snapshot_us = if has_snapshot { micros(&dt_of(&snapshot.getattr("as_of")?)?) } else { 0 };
    let age = if has_snapshot { (close - snapshot_us) as f64 / 1_000_000.0 } else { 0.0 };
    let usable = !held.is_empty() && typed::close_usable(has_snapshot, age, window).map_err(super::flow_refusal)?;
    let option_class = py.import("trade_engine.domain.instruments")?.getattr("OptionContract")?;
    let mut quotes: Vec<(Instrument, Value<'py>)> = Vec::new();
    if usable {
        for instrument in &held {
            let contract = instrument_py(py, instrument)?;
            if contract.is_instance(&option_class)? {
                quotes.push((instrument.clone(), snapshot.call_method1("get", (&contract,))?));
            }
        }
    }
    // A quote the snapshot carries but nobody re-quoted is no mark: each must be fresh too.
    let mut mids: Vec<String> = Vec::with_capacity(quotes.len());
    let mut ages: Vec<f64> = Vec::with_capacity(quotes.len());
    for (_, quote) in &quotes {
        if quote.is_none() {
            mids.push("None".to_string());
            ages.push(0.0);
        } else {
            mids.push(quote.getattr("mid")?.str()?.extract()?);
            ages.push((snapshot_us - micros(&dt_of(&quote.getattr("as_of")?)?)) as f64 / 1_000_000.0);
        }
    }
    let quotes_ok = typed::close_quotes_ok(&mids, max_age, &ages).map_err(super::flow_refusal)?;
    let plan = typed::close_plan(usable, quotes_ok, quotes.len() as i64, held.len() as i64).map_err(super::flow_refusal)?;
    let mut marks: Vec<(Instrument, Money, String)> = Vec::new();
    let note = if held.is_empty() {
        "nothing held at the close".to_string()
    } else if !plan {
        "no fresh snapshot near the close; marks left to the after-close pass".to_string()
    } else {
        let source = format!("snapshot:{}", iso(&snapshot.getattr("as_of")?)?);
        for (instrument, quote) in &quotes {
            marks.push((instrument.clone(), money(&quote.getattr("mid")?)?, source.clone()));
        }
        let equity = py.import("trade_engine.domain.instruments")?.getattr("Equity")?.call1((&underlying,))?;
        marks.push((instrument_of(&equity)?, money(&snapshot.getattr("underlying_price")?)?, source.clone()));
        format!("marked at {source}")
    };
    let bars = host.native(|l, _| glue::snapshot_count(&*l, &account, &cause))?;
    host.native(|l, _| glue::append_close(l, &now, &account, session_day, &marks, &job, &cause, i128::from(bars)))?;
    Ok(PyString::new(py, &note).into_any())
}
#[pyfunction]
fn intraday_live_guard(service: Value<'_>, session: Value<'_>) -> PyResult<()> {
    let py = service.py();
    let host = host(service, session, py.None().into_bound(py));
    let heartbeat = host.service.call_method0("_read_heartbeat")?;
    if heartbeat.is_none() { return Ok(()); }
    let file_account: String = heartbeat.getattr("account_id")?.extract()?;
    let account: String = host.account()?.extract()?;
    let path = text(&host.service.getattr("_heartbeat_path")?)?;
    typed::heartbeat_account(&file_account, &account, &path).map_err(super::flow_refusal)?;
    if heartbeat.getattr("exited")?.is_truthy()? { return Ok(()); }
    let at = heartbeat.getattr("at_utc")?;
    let age: f64 = host.clock()?.call_method0("now_utc")?.call_method1("__sub__", (&at,))?.call_method0("total_seconds")?.extract()?;
    let account: String = host.account()?.extract()?;
    let written = iso(&at)?;
    let same_session = heartbeat.getattr("session")?.eq(&host.session)?;
    let ttl: f64 = host.config()?.getattr("heartbeat_ttl_seconds")?.extract()?;
    typed::heartbeat(&account, &written, same_session, age, ttl).map_err(super::flow_refusal)?;
    Ok(())
}
#[pyfunction]
pub(crate) fn intraday_require_native(ledger: Value<'_>, calendar: Value<'_>) -> PyResult<()> {
    require_native_intraday(&ledger, &calendar).map(|_| ())
}
/// `IntradayConfig.__post_init__`'s checks, in the order the rules run them.
#[pyfunction]
fn intraday_config_check(config: Value<'_>) -> PyResult<()> {
    let truthy = |name: &str| -> PyResult<bool> { config.getattr(name)?.is_truthy() };
    typed::config_identity(truthy("job_name")?, truthy("account_id")?, truthy("underlying")?, truthy("eod_job_name")?)
        .map_err(super::flow_refusal)?;
    let number = |name: &str| -> PyResult<f64> { config.getattr(name)?.extract() };
    typed::config_rates(
        number("max_quote_age_seconds")?,
        number("tick_seconds")?,
        number("heartbeat_ttl_seconds")?,
        number("close_mark_window_seconds")?,
    )
    .map_err(super::flow_refusal)?;
    let midnight = NaiveDate::from_ymd_opt(2000, 1, 1).expect("a valid date");
    let entry_end = glue::wall_micros(midnight, clock_time(&config.getattr("entry_end")?)?);
    let flat_at = glue::wall_micros(midnight, clock_time(&config.getattr("flat_at")?)?);
    typed::config_wall(entry_end, flat_at).map_err(super::flow_refusal)?;
    typed::config_early(
        micros_of(&config.getattr("flat_before_close")?)?,
        micros_of(&config.getattr("entry_before_close")?)?,
    )
    .map_err(super::flow_refusal)
}
/// `_EntryGate.manage_options`' gate: which actions pass, and the new dropped count.
#[pyfunction]
fn intraday_gate(allow: bool, intents: Vec<bool>, dropped: i64) -> PyResult<(Vec<bool>, i64)> {
    let keep = typed::gate(allow, &intents).map_err(super::flow_refusal)?;
    let removed = keep.iter().filter(|kept| !**kept).count() as i64;
    Ok((keep, te_core::runtime::routing::increment(dropped, removed)))
}
#[pyfunction]
fn intraday_fresh_view<'py>(service: Value<'py>, snapshot: Value<'py>, now: Value<'py>) -> PyResult<Value<'py>> {
    fresh_view_of(&service, snapshot, &now)
}

/// Run `f` against the service's ledger and clock natively (`require_native_intraday` on its attributes).
fn native_on<T>(service: &Value<'_>, f: impl FnOnce(&mut StoreLedger<'_>, &PyClock<'_, '_>) -> LR<T>) -> PyResult<T> {
    let store = require_native_intraday(&service.getattr("_ledger")?, &service.getattr("_calendar")?)?;
    let clock = service.getattr("_clock")?;
    with_native(&store, &clock, &Pending::default(), f)
}

/// A Python `time` as a `NaiveTime`.
fn clock_time(value: &Value<'_>) -> PyResult<NaiveTime> {
    let hour: u32 = value.getattr("hour")?.extract()?;
    let minute: u32 = value.getattr("minute")?.extract()?;
    let second: u32 = value.getattr("second")?.extract()?;
    let microsecond: u32 = value.getattr("microsecond")?.extract()?;
    NaiveTime::from_hms_micro_opt(hour, minute, second, microsecond)
        .ok_or_else(|| PyValueError::new_err("a clock time out of range"))
}
/// A Python `timedelta` as microseconds.
fn micros_of(value: &Value<'_>) -> PyResult<i64> {
    let days: i64 = value.getattr("days")?.extract()?;
    let seconds: i64 = value.getattr("seconds")?.extract()?;
    let microseconds: i64 = value.getattr("microseconds")?.extract()?;
    Ok((days * 86_400 + seconds) * 1_000_000 + microseconds)
}
/// A Python aware datetime as a te_core instant.
fn dt_of(value: &Value<'_>) -> PyResult<DateTime> {
    parse_datetime(&iso(value)?).map(|(dt, _)| dt).map_err(super::flow_refusal)
}

/// `_fresh_view`: `snapshot` cut to its fresh quotes, if it proves a live market; else refuse (I5).
fn fresh_view_of<'py>(service: &Value<'py>, snapshot: Value<'py>, now: &Value<'py>) -> PyResult<Value<'py>> {
    let py = service.py();
    let config = service.getattr("_config")?;
    let configured: String = config.getattr("underlying")?.extract()?;
    let limit_value = config.getattr("max_quote_age_seconds")?;
    let limit: f64 = limit_value.extract()?;
    let chain = py.import("trade_engine.market_data.chains")?.getattr("ChainSnapshot")?;
    let is_chain = snapshot.is_instance(&chain)?;
    let source: String = snapshot.get_type().getattr("__name__")?.extract()?;
    let got: String = if is_chain { snapshot.getattr("underlying")?.extract()? } else { String::new() };
    typed::snapshot_identity(is_chain, &source, &configured, &got).map_err(super::flow_refusal)?;
    snapshot.call_method1("require_fresh", (now, &limit_value))?;
    let now_us = micros(&dt_of(now)?);
    let quoted = snapshot.getattr("underlying_as_of")?;
    let as_of_text = iso(&snapshot.getattr("as_of")?)?;
    typed::underlying_stamp(!quoted.is_none(), &got, &as_of_text).map_err(super::flow_refusal)?;
    let quoted_text = iso(&quoted)?;
    let age = (now_us - micros(&dt_of(&quoted)?)) as f64 / 1_000_000.0;
    typed::underlying_age(age, limit, &got, &format!("{age:.0}"), &quoted_text, &format!("{limit:.0}"))
        .map_err(super::flow_refusal)?;
    let account: String = config.getattr("account_id")?.extract()?;
    let held = native_on(service, |l, _| {
        let folded = l.state(&account)?;
        glue::held_contracts(&folded, &configured)
    })?;
    for instrument in &held {
        let contract = instrument_py(py, instrument)?;
        let quote = snapshot.call_method1("get", (&contract,))?;
        let quoted_leg = !quote.is_none();
        let leg_age = if quoted_leg { (now_us - micros(&dt_of(&quote.getattr("as_of")?)?)) as f64 / 1_000_000.0 } else { 0.0 };
        let occ: String = contract.getattr("occ")?.call_method0("strip")?.extract()?;
        typed::leg(quoted_leg, leg_age, limit, &occ, &got, &format!("{leg_age:.0}"), &format!("{limit:.0}"))
            .map_err(super::flow_refusal)?;
    }
    let quotes: Vec<Value<'py>> = snapshot.getattr("quotes")?.try_iter()?.collect::<PyResult<_>>()?;
    let mut ages = Vec::with_capacity(quotes.len());
    for quote in &quotes {
        ages.push((now_us - micros(&dt_of(&quote.getattr("as_of")?)?)) as f64 / 1_000_000.0);
    }
    let keep = typed::fresh(limit, &ages).map_err(super::flow_refusal)?;
    if keep.iter().all(|kept| *kept) {
        return Ok(snapshot);
    }
    let kept: Vec<Value<'py>> = quotes.into_iter().zip(keep).filter(|(_, kept)| *kept).map(|(quote, _)| quote).collect();
    let kwargs = PyDict::new(py);
    kwargs.set_item("quotes", PyTuple::new(py, kept)?)?;
    py.import("dataclasses")?.call_method("replace", (snapshot,), Some(&kwargs))
}

/// `flat_close`: the market close that undoes whatever is still open of `structure`.
fn flat_close_of<'py>(structure: &Value<'py>, session: &Value<'py>, code: &str, attempt: i64, reason: &str) -> PyResult<Value<'py>> {
    let py = structure.py();
    let entry_id: String = structure.getattr("entry_order_id")?.extract()?;
    let (reason, command) = typed::flat_close(reason, code, &entry_id, &iso(session)?, attempt).map_err(super::flow_refusal)?;
    let kwargs = PyDict::new(py);
    kwargs.set_item("entry_order_id", structure.getattr("entry_order_id")?)?;
    kwargs.set_item("reason", reason)?;
    kwargs.set_item("command_id", command)?;
    py.import("trade_engine.domain.option_orders")?.getattr("CloseStructure")?.call((), Some(&kwargs))
}
/// `_emergency`: an unexpected error — try to be flat on a fresh quote, alert loudly, exit
/// (I5). The flatten is only asked on a quote that proves the market is live; it rests in
/// the ledger, so the restarted service (restart-on-failure) fills it at its first fresh
/// snapshot. With no fresh quote nothing is sent and the alert says so.
#[pyfunction]
fn intraday_emergency<'py>(service: Value<'py>, session: Value<'py>, state: Value<'py>, error: Value<'py>) -> PyResult<Value<'py>> {
    let mut host = host(service, session, state);
    let py = host.service.py();
    let kind: String = error.get_type().getattr("__name__")?.extract()?;
    let mut note = format!("ALERT {kind}: {}", text(&error)?);
    host.state.setattr("refusing", true)?;
    let fresh = (|| {
        let now = host.now()?;
        let snapshot = host.fetch(&now)?;
        host.fresh(snapshot, &now)?;
        Ok::<_, PyErr>(())
    })();
    match fresh {
        Err(stale) if host.exception(&stale) => {
            note += &format!("; no fresh quote to flatten on ({}); positions left open, entries refused", host.error_text(&stale)?.1);
        }
        Err(error) => return Err(error),
        Ok(()) => {
            let flattened = (|| {
                host.cancel("emergency")?;
                host.service.call_method1("_flatten", (&host.session, "emergency", format!("unexpected error: {kind}")))
            })();
            match flattened {
                Ok(asked) => note += &format!("; flatten asked for {} structure(s) at a fresh quote, entries refused", text(&asked)?),
                Err(second) if host.exception(&second) => {
                    let (kind, message) = host.error_text(&second)?;
                    note += &format!("; flatten failed: {kind}: {message}");
                }
                Err(error) => return Err(error),
            }
        }
    }
    host.beat(&note, true, true)?;
    let message = format!("The intraday service for '{}' stopped on {}: {note}", text(&host.account()?)?, iso(&host.session)?);
    let alert = PyErr::from_value(host.module()?.getattr("IntradayServiceAlert")?.call1((message,))?);
    alert.set_cause(py, Some(PyErr::from_value(error)));
    Err(alert)
}

fn file_error(py: Python<'_>, error: te_host::heartbeat::FileError) -> PyResult<PyErr> {
    if let Some(destination) = error.destination {
        let raw = error.error.raw_os_error().unwrap_or(5);
        let source = error.path.into_pyobject(py)?;
        let destination = destination.into_pyobject(py)?;
        #[cfg(windows)]
        let value = {
            let message: String = py.import("ctypes")?.call_method1("FormatError", (raw,))?.extract()?;
            py.import("builtins")?.getattr("OSError")?.call1((0, message.trim().trim_end_matches('.'), source, raw, destination))?
        };
        #[cfg(not(windows))]
        let value = py.import("builtins")?.getattr("OSError")?.call1((raw,
            py.import("os")?.call_method1("strerror", (raw,))?, source, py.None(), destination))?;
        Ok(PyErr::from_value(value))
    } else {
        crate::lock::io_error(py, error.error, &error.path, false)
    }
}
fn path(value: &Value<'_>) -> PyResult<std::path::PathBuf> {
    crate::lock::path_from_python(&value.str()?.into_any())
}
#[pyfunction]
pub(crate) fn intraday_heartbeat_write(service: Value<'_>, session: Value<'_>, state: Value<'_>, note: &str, alert: bool, exited: bool) -> PyResult<()> {
    let heartbeat_path = service.getattr("_heartbeat_path")?;
    if heartbeat_path.is_none() { return Ok(()); }
    let py = service.py();
    let args = PyDict::new(py);
    let config = service.getattr("_config")?;
    let barred = state.getattr("barred")?;
    args.set_item("account_id", config.getattr("account_id")?)?;
    args.set_item("session", session)?;
    args.set_item("at_utc", service.getattr("_clock")?.call_method0("now_utc")?)?;
    let refusing = state.getattr("refusing")?;
    args.set_item("refusing", if refusing.is_truthy()? { refusing } else { (!barred.is_none()).into_pyobject(py)?.to_owned().into_any() })?;
    args.set_item("note", if barred.is_none() { note.to_owned() } else { format!("{note}; entries barred: {}", text(&barred)?) })?;
    args.set_item("alert", alert)?;
    args.set_item("exited", exited)?;
    let beat = py.import("trade_engine.intraday.service")?.getattr("Heartbeat")?.call((), Some(&args))?;
    let body = py.import("dataclasses")?.call_method1("asdict", (&beat,))?;
    body.set_item("session", iso(&beat.getattr("session")?)?)?;
    body.set_item("at_utc", iso(&beat.getattr("at_utc")?)?)?;
    let encoded = py.import("json")?.call_method1("dumps", (body,))?.call_method1("encode", ("utf-8",))?;
    let bytes: Vec<u8> = encoded.extract()?;
    let temporary = heartbeat_path.call_method1("with_name", (format!("{}.tmp", text(&heartbeat_path.getattr("name")?)?),))?;
    te_host::heartbeat::write(&path(&heartbeat_path)?, &path(&temporary)?, &bytes)
        .map_err(|error| file_error(py, error).unwrap_or_else(|error| error))
}
#[pyfunction]
pub(crate) fn intraday_heartbeat_read<'py>(service: Value<'py>) -> PyResult<Value<'py>> {
    let py = service.py();
    let heartbeat_path = service.getattr("_heartbeat_path")?;
    if heartbeat_path.is_none() { return Ok(py.None().into_bound(py)); }
    let bytes = te_host::heartbeat::read(&path(&heartbeat_path)?)
        .map_err(|error| file_error(py, error).unwrap_or_else(|error| error))?;
    let Some(bytes) = bytes else { return Ok(py.None().into_bound(py)); };
    let module = py.import("trade_engine.intraday.service")?;
    let parsed = (|| {
        let decoded = PyBytes::new(py, &bytes).call_method1("decode", ("utf-8",))?
            .call_method1("replace", ("\r\n", "\n"))?.call_method1("replace", ("\r", "\n"))?;
        let raw = py.import("json")?.call_method1("loads", (decoded,))?;
        let dt = py.import("datetime")?;
        let builtins = py.import("builtins")?;
        let args = PyDict::new(py);
        args.set_item("account_id", raw.get_item("account_id")?)?;
        args.set_item("session", dt.getattr("date")?.call_method1("fromisoformat", (raw.get_item("session")?,))?)?;
        args.set_item("at_utc", dt.getattr("datetime")?.call_method1("fromisoformat", (raw.get_item("at_utc")?,))?)?;
        args.set_item("refusing", builtins.call_method1("bool", (raw.get_item("refusing")?,))?)?;
        args.set_item("note", builtins.call_method1("str", (raw.call_method1("get", ("note", ""))?,))?)?;
        args.set_item("alert", builtins.call_method1("bool", (raw.call_method1("get", ("alert", false))?,))?)?;
        args.set_item("exited", builtins.call_method1("bool", (raw.call_method1("get", ("exited", false))?,))?)?;
        module.getattr("Heartbeat")?.call((), Some(&args))
    })();
    match parsed {
        Err(error) if error.is_instance_of::<PyValueError>(py) || error.is_instance_of::<PyKeyError>(py) || error.is_instance_of::<PyTypeError>(py) => {
            let kind: String = error.value(py).get_type().getattr("__name__")?.extract()?;
            let message = format!("The heartbeat file {} cannot be read ({kind}: {}); it cannot prove no other instance is running, so the service refuses to start (C4). Remove it once you have checked that no service is running.",
                text(&heartbeat_path)?, text(error.value(py).as_any())?);
            let wrapped = PyErr::from_value(module.getattr("IntradayServiceError")?.call1((message,))?);
            wrapped.set_cause(py, Some(error));
            Err(wrapped)
        }
        result => result,
    }
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(intraday_run, m)?)?;
    m.add_function(wrap_pyfunction!(intraday_tick, m)?)?;
    m.add_function(wrap_pyfunction!(intraday_wait, m)?)?;
    m.add_function(wrap_pyfunction!(intraday_advance, m)?)?;
    m.add_function(wrap_pyfunction!(intraday_refuse, m)?)?;
    m.add_function(wrap_pyfunction!(intraday_flatten, m)?)?;
    m.add_function(wrap_pyfunction!(intraday_cancel, m)?)?;
    m.add_function(wrap_pyfunction!(intraday_restore, m)?)?;
    m.add_function(wrap_pyfunction!(intraday_close, m)?)?;
    m.add_function(wrap_pyfunction!(intraday_live_guard, m)?)?;
    m.add_function(wrap_pyfunction!(intraday_require_native, m)?)?;
    m.add_function(wrap_pyfunction!(intraday_config_check, m)?)?;
    m.add_function(wrap_pyfunction!(intraday_gate, m)?)?;
    m.add_function(wrap_pyfunction!(intraday_fresh_view, m)?)?;
    m.add_function(wrap_pyfunction!(intraday_emergency, m)?)?;
    m.add_function(wrap_pyfunction!(intraday_heartbeat_write, m)?)?;
    m.add_function(wrap_pyfunction!(intraday_heartbeat_read, m)?)?;
    Ok(())
}

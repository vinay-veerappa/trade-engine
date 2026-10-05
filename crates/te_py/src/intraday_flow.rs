use pyo3::exceptions::{PyException, PyKeyError, PyTypeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict};
use te_core::runtime::intraday_flow::{self as flow, Host as FlowHost};

type Value<'py> = Bound<'py, PyAny>;

fn iso(value: &Value<'_>) -> PyResult<String> { value.call_method0("isoformat")?.extract() }
fn text(value: &Value<'_>) -> PyResult<String> { value.str()?.extract() }
fn rule(op: &str, text: Vec<String>, numbers: Vec<i64>, flags: Vec<bool>) -> PyResult<te_core::runtime::Plan> {
    te_core::runtime::decide(op, &text, &numbers, &flags, &[]).map_err(super::flow_refusal)
}
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
    flat: Option<Value<'py>>,
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
        self.service.call_method1("_advance", (target,))?;
        Ok(())
    }
    fn error_text(&self, error: &PyErr) -> PyResult<(String, String)> {
        let value = error.value(self.service.py());
        Ok((value.get_type().getattr("__name__")?.extract()?, text(value.as_any())?))
    }
}

impl<'py> FlowHost for Host<'py> {
    type Error = PyErr;
    type Value = Value<'py>;
    fn validate(&mut self) -> PyResult<()> {
        let calendar = self.calendar()?;
        rule("intraday:session", vec![iso(&self.session)?, calendar.getattr("exchange")?.extract()?], vec![],
            vec![calendar.call_method1("is_session", (&self.session,))?.is_truthy()?])?;
        Ok(())
    }
    fn init_state(&mut self) -> PyResult<()> {
        self.state = self.module()?.getattr("_TickState")?.call0()?;
        Ok(())
    }
    fn settled(&mut self) -> PyResult<bool> {
        let marker = self.service.call_method1("_eod_marker", (&self.session,))?;
        Ok(!self.ledger()?.call_method1("event_by_command", (marker,))?.is_none())
    }
    fn admit(&mut self) -> PyResult<()> {
        self.service.call_method1("_refuse_other_live_instance", (&self.session,))?;
        self.service.call_method1("_require_previous_eod_complete", (&self.session,))?;
        Ok(())
    }
    fn beat(&mut self, note: &str, alert: bool, exited: bool) -> PyResult<()> {
        self.service.call_method("_write_heartbeat", (&self.session, &self.state, note),
            Some(&kwargs(self.service.py(), alert, exited)?))?;
        Ok(())
    }
    fn connect_restore(&mut self) -> PyResult<()> {
        self.config()?.getattr("broker")?.call_method0("connect")?;
        self.service.call_method1("_rehydrate", (&self.session, &self.state))?;
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
        self.service.call_method1("_wait_until", (self.open.as_ref().unwrap(), &self.session, &self.state))?;
        Ok(())
    }
    fn before_close(&mut self) -> PyResult<bool> { self.now()?.lt(self.close.as_ref().unwrap()) }
    fn stopped(&mut self) -> PyResult<bool> { Ok(!self.stop.is_none() && self.now()?.ge(&self.stop)?) }
    fn result(&mut self, settled: bool, stopped: bool) -> PyResult<Value<'py>> {
        let args = PyDict::new(self.service.py());
        if settled { args.set_item("settled", true)?; }
        if stopped { args.set_item("stopped_at", self.now()?)?; }
        self.service.call_method("_result", (&self.session, &self.state), Some(&args))
    }
    fn now(&mut self) -> PyResult<Value<'py>> { self.clock()?.call_method0("now_utc") }
    fn fetch(&mut self, now: &Value<'py>) -> PyResult<Value<'py>> {
        self.config()?.getattr("snapshot_source")?.call1((self.config()?.getattr("underlying")?, now))
    }
    fn fresh(&mut self, snapshot: Value<'py>, now: &Value<'py>) -> PyResult<Value<'py>> {
        self.service.call_method1("_fresh_view", (snapshot, now))
    }
    fn stale(&self, error: &PyErr) -> PyResult<bool> {
        Ok(error.is_instance_of::<PyValueError>(self.service.py()) ||
            error.matches(self.service.py(), self.module()?.getattr("StaleDataError")?)?)
    }
    fn exception(&self, error: &PyErr) -> bool { error.is_instance_of::<PyException>(self.service.py()) }
    fn stale_refusal(&mut self, error: PyErr) -> PyResult<()> {
        let (_, message) = self.error_text(&error)?;
        self.service.call_method1("_go_flat_and_refuse", (&self.session, &self.state, &message))?;
        self.beat(&format!("stale quote: {message}"), false, false)
    }
    fn clear_refusal(&mut self) -> PyResult<()> { self.state.setattr("refusing", false) }
    fn deadlines(&mut self, now: &Value<'py>) -> PyResult<(bool, bool)> {
        let deadlines = self.service.call_method1("_deadlines", (&self.session,))?;
        let entry = deadlines.get_item(0)?;
        let flat = deadlines.get_item(1)?;
        let module = self.service.py().import("trade_engine.eod._runtime")?;
        let micros = |v: &Value<'py>| -> PyResult<i64> { module.call_method1("micros", (v,))?.extract() };
        let plan = rule("intraday:tick", vec![], vec![micros(now)?, micros(&entry)?, micros(&flat)?],
            vec![!self.state.getattr("barred")?.is_none()])?;
        self.flat = Some(flat);
        Ok((plan.flags[0], plan.flags[1]))
    }
    fn cancel(&mut self, code: &str) -> PyResult<()> {
        self.service.call_method1("_cancel_working_entries", (&self.session, code))?;
        Ok(())
    }
    fn flatten(&mut self, code: &str) -> PyResult<()> {
        let at = self.flat.as_ref().unwrap().call_method1("astimezone", (self.module()?.getattr("NEW_YORK")?,))?;
        let reason = format!("flat by {} ET", at.call_method1("strftime", ("%H:%M",))?.extract::<String>()?);
        let count = self.service.call_method1("_flatten", (&self.session, code, reason))?;
        self.increment(&count)
    }
    fn gate(&mut self, allow: bool) -> PyResult<()> { self.service.getattr("_gate")?.setattr("allow", allow) }
    fn manage(&mut self, snapshot: &Value<'py>) -> PyResult<()> {
        let added = self.service.getattr("_router")?.call_method1("manage_at_snapshot",
            (self.account()?, &self.session, snapshot, self.state.getattr("snapshots")?,
                self.service.call_method1("_cause", (&self.session,))?))?;
        let tally = self.service.py().import("operator")?.call_method1("iadd", (self.state.getattr("tally")?, added))?;
        self.state.setattr("tally", tally)
    }
    fn record(&mut self, snapshot: &Value<'py>) -> PyResult<()> {
        self.service.call_method1("_record_tick", (&self.session, snapshot))?;
        Ok(())
    }
    fn pause(&mut self) -> PyResult<()> { self.service.call_method0("_pause")?; Ok(()) }
    fn tick(&mut self) -> PyResult<()> {
        self.service.call_method1("_tick", (&self.session, &self.state))?;
        Ok(())
    }
    fn close(&mut self) -> PyResult<String> { self.service.call_method1("_close_mark", (&self.session, &self.state))?.extract() }
    fn emergency(&mut self, error: PyErr) -> PyResult<Value<'py>> {
        self.service.call_method1("_emergency", (&self.session, &self.state, error.value(self.service.py())))
    }
}

fn host<'py>(service: Value<'py>, session: Value<'py>, state: Value<'py>) -> Host<'py> {
    let none = service.py().None().into_bound(service.py());
    Host { service, session, state, start: none.clone(), stop: none, open: None, close: None, flat: None }
}

#[pyfunction]
fn intraday_run<'py>(service: Value<'py>, session: Value<'py>, start: Value<'py>, stop: Value<'py>) -> PyResult<Value<'py>> {
    let state = service.py().None().into_bound(service.py());
    let mut host = host(service, session, state);
    host.start = start;
    host.stop = stop;
    flow::run(&mut host)
}
#[pyfunction]
fn intraday_tick(service: Value<'_>, session: Value<'_>, state: Value<'_>) -> PyResult<()> {
    flow::tick(&mut host(service, session, state))
}
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
#[pyfunction]
fn intraday_refuse(service: Value<'_>, session: Value<'_>, state: Value<'_>, why: &str) -> PyResult<()> {
    let mut host = host(service, session, state);
    host.state.setattr("refusing", true)?;
    host.cancel("stale-quote")?;
    let count = host.service.call_method1("_flatten", (&host.session, "stale-quote", format!("stale quote: {why}")))?;
    host.increment(&count)
}
#[pyfunction]
fn intraday_flatten(service: Value<'_>, session: Value<'_>, code: &str, reason: &str) -> PyResult<i64> {
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
        let plan = rule("intraday:flatten", vec![
            sim.call_method1("underlying_of", (structure.getattr("instrument")?,))?.extract()?, underlying.clone()],
            vec![], vec![!structure.getattr("closing_order_id")?.is_none()])?;
        if !plan.flags[0] { continue; }
        let mut ids = vec![structure.getattr("entry_order_id")?.extract()?];
        for id in folded.getattr("orders")?.try_iter()? { ids.push(id?.extract()?); }
        let attempt = rule("intraday:attempt", ids, vec![], vec![])?.numbers[0];
        let close = host.module()?.getattr("flat_close")?.call1((structure, &host.session, code, attempt, reason))?;
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
        let allowed = rule("intraday:cancel", vec![order.getattr("state")?.getattr("value")?.extract()?,
            underlying, host.config()?.getattr("underlying")?.extract()?], vec![],
            vec![order.getattr("parent_order_id")?.is_none(), is_structure])?.flags[0];
        if !allowed { continue; }
        let id = order.getattr("order_id")?;
        let args = PyDict::new(py);
        args.set_item("command_id", format!("intraday:cancel-entry:{}:{code}:{}", text(&id)?, iso(&host.session)?))?;
        manager.getattr("orders")?.call_method("cancel", (id,), Some(&args))?;
    }
    Ok(())
}
#[pyfunction]
fn intraday_restore(service: Value<'_>, session: Value<'_>, state: Value<'_>) -> PyResult<()> {
    let py = service.py();
    let host = host(service, session, state);
    let restore = py.import("trade_engine.oms.restore")?;
    let broker = host.config()?.getattr("broker")?;
    let min = restore.getattr("MIN_TIME")?;
    if broker.call_method1("orders", (&min,))?.is_truthy()? || broker.call_method1("fills", (&min,))?.is_truthy()? {
        return Ok(());
    }
    let account = host.account()?;
    let ledger = host.ledger()?;
    let folded = ledger.call_method1("state", (&account,))?;
    let resolution = restore.getattr("PendingResolution")?.call0()?;
    let args = PyDict::new(py);
    args.set_item("pending", "resolve")?;
    args.set_item("resolution", &resolution)?;
    let restored = match restore.getattr("restorable")?.call((&ledger, &account, &folded), Some(&args)) {
        Err(error) if error.matches(py, restore.getattr("RestoreError")?)? => {
            let refused = PyErr::from_value(host.module()?.getattr("IntradayServiceError")?.call1((text(error.value(py).as_any())?,))?);
            refused.set_cause(py, Some(error));
            return Err(refused);
        }
        result => result?,
    };
    let orders = restored.get_item(0)?;
    let fills = restored.get_item(1)?;
    let positions = restore.call_method1("restorable_positions", (&folded,))?;
    if orders.is_truthy()? || fills.is_truthy()? || positions.is_truthy()? {
        broker.call_method1("restore", (orders, fills, positions))?;
    }
    let manager = host.manager()?;
    for id in resolution.getattr("resolved")?.try_iter()? {
        manager.getattr("orders")?.call_method1("reconcile_order", (id?,))?;
    }
    let unresolved = resolution.getattr("unresolved")?;
    if unresolved.is_truthy()? {
        host.state.setattr("barred", "; ".into_pyobject(py)?.call_method1("join", (unresolved.call_method0("values")?,))?)?;
    }
    let domain = py.import("trade_engine.domain.option_orders")?;
    let terminals = py.import("trade_engine.domain.orders")?.getattr("OrderState")?;
    for order in sorted_orders(py, ledger.call_method1("state", (&account,))?)? {
        let action = rule("intraday:restart", vec![order.getattr("state")?.getattr("value")?.extract()?,
            order.getattr("order_id")?.extract()?, text(&order.getattr("parent_order_id")?)?], vec![],
            vec![order.getattr("parent_order_id")?.is_none(), domain.call_method1("is_structure", (order.getattr("instrument")?,))?.is_truthy()?])?.text;
        match action[0].as_str() {
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
#[pyfunction]
fn intraday_close<'py>(service: Value<'py>, session: Value<'py>, state: Value<'py>) -> PyResult<Value<'py>> {
    let py = service.py();
    let host = host(service, session, state);
    let plan = host.service.call_method1("_close_plan", (&host.session, &host.state))?;
    let now = plan.get_item(0)?;
    let marks = plan.get_item(1)?;
    let mut ordered = Vec::new();
    for instrument in marks.try_iter()? {
        let instrument = instrument?;
        ordered.push((instrument.getattr("symbol")?.extract::<String>()?, instrument));
    }
    ordered.sort_by(|a, b| a.0.cmp(&b.0));
    let ledger = host.ledger()?;
    let module = py.import("trade_engine.ledger")?;
    let kinds = module.getattr("EventKind")?;
    let account = host.account()?;
    for (symbol, instrument) in ordered {
        let command = format!("intraday:mark:{}:{}:{symbol}", text(&account)?, iso(&host.session)?);
        if !ledger.call_method1("event_by_command", (&command,))?.is_none() { continue; }
        let mark = marks.get_item(&instrument)?;
        let args = PyDict::new(py);
        args.set_item("instrument", instrument)?;
        args.set_item("price", mark.get_item(0)?)?;
        args.set_item("as_of", &now)?;
        args.set_item("source", mark.get_item(1)?)?;
        let payload = module.getattr("Mark")?.call((), Some(&args))?;
        let event = PyDict::new(py);
        event.set_item("account", &account)?;
        event.set_item("kind", kinds.getattr("MARK")?)?;
        event.set_item("payload", payload)?;
        event.set_item("ts_utc", &now)?;
        event.set_item("command_id", command)?;
        ledger.call_method1("append", (module.getattr("Event")?.call((), Some(&event))?,))?;
    }
    let args = PyDict::new(py);
    args.set_item("session", &host.session)?;
    args.set_item("job", format!("intraday:{}", text(&host.config()?.getattr("job_name")?)?))?;
    args.set_item("account_id", &account)?;
    args.set_item("bars_processed", host.service.call_method1("_session_snapshot_count", (&host.session,))?)?;
    args.set_item("at_close", &now)?;
    let payload = module.getattr("EodRun")?.call((), Some(&args))?;
    let event = PyDict::new(py);
    event.set_item("account", account)?;
    event.set_item("kind", kinds.getattr("EOD_RUN")?)?;
    event.set_item("payload", payload)?;
    event.set_item("ts_utc", now)?;
    event.set_item("command_id", host.service.call_method1("_own_marker", (&host.session,))?)?;
    ledger.call_method1("append", (module.getattr("Event")?.call((), Some(&event))?,))?;
    plan.get_item(2)
}
#[pyfunction]
fn intraday_live_guard(service: Value<'_>, session: Value<'_>) -> PyResult<()> {
    let py = service.py();
    let host = host(service, session, py.None().into_bound(py));
    let heartbeat = host.service.call_method0("_read_heartbeat")?;
    if heartbeat.is_none() { return Ok(()); }
    rule("intraday:heartbeat_account", vec![heartbeat.getattr("account_id")?.extract()?,
        host.account()?.extract()?, text(&host.service.getattr("_heartbeat_path")?)?], vec![], vec![])?;
    if heartbeat.getattr("exited")?.is_truthy()? { return Ok(()); }
    let at = heartbeat.getattr("at_utc")?;
    let age: f64 = host.clock()?.call_method0("now_utc")?.call_method1("__sub__", (&at,))?.call_method0("total_seconds")?.extract()?;
    te_core::runtime::decide("intraday:heartbeat",
        &[host.account()?.extract()?, iso(&at)?], &[],
        &[heartbeat.getattr("session")?.eq(&host.session)?],
        &[age, host.config()?.getattr("heartbeat_ttl_seconds")?.extract()?]).map_err(super::flow_refusal)?;
    Ok(())
}
#[pyfunction]
fn intraday_previous_guard(service: Value<'_>, session: Value<'_>) -> PyResult<()> {
    let py = service.py();
    let host = host(service, session, py.None().into_bound(py));
    let ledger = host.ledger()?;
    if !ledger.call_method1("event_by_command", (host.service.call_method1("_own_marker", (&host.session,))?,))?.is_none() {
        return Ok(());
    }
    let previous = host.calendar()?.call_method1("previous_session", (&host.session,))?;
    if !ledger.call_method1("event_by_command", (host.service.call_method1("_eod_marker", (&previous,))?,))?.is_none() {
        return Ok(());
    }
    rule("intraday:previous", vec![iso(&host.session)?, host.account()?.extract()?, iso(&previous)?,
        host.config()?.getattr("eod_job_name")?.extract()?, host.service.call_method1("_eod_marker", (&previous,))?.extract()?],
        vec![], vec![host.service.call_method1("_has_history", (host.account()?,))?.is_truthy()?])?;
    Ok(())
}
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
fn intraday_heartbeat_write(service: Value<'_>, session: Value<'_>, state: Value<'_>, note: &str, alert: bool, exited: bool) -> PyResult<()> {
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
fn intraday_heartbeat_read<'py>(service: Value<'py>) -> PyResult<Value<'py>> {
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
    m.add_function(wrap_pyfunction!(intraday_previous_guard, m)?)?;
    m.add_function(wrap_pyfunction!(intraday_emergency, m)?)?;
    m.add_function(wrap_pyfunction!(intraday_heartbeat_write, m)?)?;
    m.add_function(wrap_pyfunction!(intraday_heartbeat_read, m)?)?;
    Ok(())
}

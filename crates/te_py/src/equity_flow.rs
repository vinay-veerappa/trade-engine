use pyo3::exceptions::PyAttributeError;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyIterator};
use te_core::runtime::eod as rules;
use te_core::runtime::eod_flow::{self as flow, EquityEntryHost, ExitHost, ExitKind, SignalHost};
use te_core::runtime::routing;

type Value<'py> = Bound<'py, PyAny>;
fn optional<'py>(value: &Value<'py>, name: &str) -> PyResult<Value<'py>> {
    match value.getattr(name) {
        Ok(value) => Ok(value),
        Err(e) if e.is_instance_of::<PyAttributeError>(value.py()) => Ok(value.py().None().into_bound(value.py())),
        Err(e) => Err(e),
    }
}
fn terminal(order: &Value<'_>) -> PyResult<bool> {
    Ok(rules::terminal(&order.getattr("state")?.getattr("value")?.extract::<String>()?))
}

struct Inputs<'py> {
    runner: Value<'py>,
    account: String,
    session: Value<'py>,
    tally: Value<'py>,
    options: bool,
    signals: Option<Value<'py>>,
    iterator: Option<Bound<'py, PyIterator>>,
    strategy: Option<Value<'py>>,
    risk: Option<Value<'py>>,
    manager: Option<Value<'py>>,
}

impl<'py> Inputs<'py> {
    fn config(&self) -> PyResult<Value<'py>> { self.runner.getattr("_config") }
    fn context(&self) -> PyResult<Value<'py>> {
        let py = self.runner.py();
        if !self.options {
            let context = PyDict::new(py);
            context.set_item("session", &self.session)?;
            context.set_item("account_id", &self.account)?;
            return Ok(context.into_any());
        }
        let router = self.runner.call_method1("_router", (&self.account,))?;
        let now = self.runner.getattr("_clock")?.call_method0("now_utc")?;
        let state = self.runner.getattr("_ledger")?.call_method1("state", (&self.account,))?;
        let structures = py.import("trade_engine.oms.options")?.call_method1("open_structures", (state,))?;
        let kwargs = PyDict::new(py);
        kwargs.set_item("snapshots", self.tally.getattr("snapshots")?)?;
        router.call_method("context", (&self.account, &self.session, now, structures), Some(&kwargs))
    }
    fn next(&mut self) -> PyResult<Option<Value<'py>>> {
        self.iterator.as_mut().expect("initialized stream").next().transpose()
    }
}

impl<'py> SignalHost for Inputs<'py> {
    type Value = Value<'py>;
    type Error = PyErr;
    fn setup(&mut self) -> PyResult<bool> {
        let config = self.config()?;
        let adapter = config.getattr("signal_adapters")?.call_method1("get", (&self.account,))?;
        let strategy = config.getattr("strategies")?.call_method1("get", (&self.account,))?;
        let risk = if self.options { self.runner.py().None().into_bound(self.runner.py()) }
            else { config.getattr("risk_engines")?.call_method1("get", (&self.account,))? };
        if adapter.is_none() || strategy.is_none() || (!self.options && risk.is_none()) {
            return Ok(false);
        }
        if !self.options {
            let broker = config.getattr("brokers")?.get_item(&self.account)?;
            self.manager = Some(self.runner.call_method1("_manager_for", (&self.account, broker))?);
        }
        self.strategy = Some(strategy);
        self.risk = Some(risk);
        let signals = adapter.call_method1("read_signals", (&self.session,))?;
        self.iterator = Some(signals.try_iter()?);
        self.signals = Some(signals);
        Ok(true)
    }
    fn next_signal(&mut self) -> PyResult<Option<Self::Value>> { self.next() }
    fn record_signal(&self, signal: &Self::Value) -> PyResult<()> {
        self.runner.call_method1("_record_signal", (&self.account, signal))?;
        Ok(())
    }
    fn start_intents(&mut self) -> PyResult<()> {
        let context = self.context()?;
        let intents = self.strategy.as_ref().expect("configured strategy").call_method1("generate_intents",
            (self.signals.as_ref().expect("observed signals"), context))?;
        self.iterator = Some(intents.try_iter()?);
        Ok(())
    }
    fn next_intent(&mut self) -> PyResult<Option<Self::Value>> { self.next() }
    fn enter(&self, intent: &Self::Value) -> PyResult<i64> {
        if self.options {
            self.runner.call_method1("_enter_option", (&self.account, &self.session, intent,
                &self.tally, self.runner.py().None()))?.extract()
        } else {
            flow::enter_equity(self, intent)
        }
    }
    fn count(&self, submitted: i64) -> PyResult<()> {
        if self.options {
            let current = self.tally.getattr("orders_submitted")?;
            self.tally.setattr("orders_submitted", routing::increment(current.extract()?, submitted))?;
        }
        Ok(())
    }
}

impl<'py> EquityEntryHost for Inputs<'py> {
    type Value = Value<'py>;
    type Error = PyErr;
    fn validate(&self, intent: &Self::Value) -> PyResult<()> {
        rules::intent(&intent.getattr("intent_id")?.extract::<String>()?, &intent.getattr("account_id")?.extract::<String>()?,
            &self.account).map_err(super::flow_refusal)?;
        Ok(())
    }
    fn recorded(&self, intent: &Self::Value) -> PyResult<Option<Self::Value>> {
        let verdict = self.runner.call_method1("recorded_verdict", (&self.account, intent))?;
        Ok(if verdict.is_none() { None } else { Some(verdict) })
    }
    fn evaluate(&self, intent: &Self::Value) -> PyResult<Self::Value> {
        let state = self.runner.getattr("_ledger")?.call_method1("state", (&self.account,))?;
        let builder = self.config()?.getattr("context_builder")?;
        let context = if builder.is_none() {
            self.runner.call_method1("_derive_risk_context", (intent, state))?
        } else { builder.call1((intent, state, &self.session))? };
        self.risk.as_ref().expect("configured risk").call_method1("evaluate", (intent, context))
    }
    fn record(&self, intent: &Self::Value, verdict: &Self::Value) -> PyResult<()> {
        self.runner.call_method1("_record_verdict", (&self.account, intent, verdict))?;
        Ok(())
    }
    fn approved(&self, verdict: &Self::Value) -> PyResult<bool> {
        let accepted = verdict.getattr("accepted")?.is_truthy()?;
        Ok(rules::equity_approved(accepted, !verdict.getattr("approved_quantity")?.is_none()))
    }
    fn bracket(&self, intent: &Self::Value, verdict: &Self::Value) -> PyResult<Self::Value> {
        self.manager.as_ref().expect("configured manager").call_method1("create_bracket",
            (intent, verdict.getattr("approved_quantity")?))
    }
    fn submit(&self, bracket: &Self::Value) -> PyResult<Self::Value> {
        self.manager.as_ref().expect("configured manager").call_method1("submit", (bracket.getattr("entry")?,))
    }
    fn terminal(&self, order: &Self::Value) -> PyResult<bool> { terminal(order) }
    fn reconcile(&self, bracket: &Self::Value) -> PyResult<()> {
        self.manager.as_ref().expect("configured manager").call_method1("reconcile_order",
            (bracket.getattr("entry")?.getattr("order_id")?,))?;
        Ok(())
    }
}

struct Exits<'py> {
    runner: Value<'py>,
    account: String,
    session: Value<'py>,
    closes: Value<'py>,
    manager: Option<Value<'py>>,
    by_entry: Option<Bound<'py, PyDict>>,
}
impl<'py> ExitHost for Exits<'py> {
    type Value = Value<'py>;
    type Error = PyErr;
    fn actions(&mut self) -> PyResult<Vec<Self::Value>> {
        let py = self.runner.py();
        let config = self.runner.getattr("_config")?;
        let strategy = config.getattr("strategies")?.call_method1("get", (&self.account,))?;
        let manage = optional(&strategy, "manage_positions")?;
        if !manage.is_callable() { return Ok(Vec::new()); }
        let brackets = self.runner.call_method1("_open_brackets", (&self.account, &self.session, &self.closes))?;
        if !brackets.is_truthy()? { return Ok(Vec::new()); }
        let context = PyDict::new(py);
        context.set_item("session", &self.session)?;
        context.set_item("account_id", &self.account)?;
        let actions = py.import("builtins")?.call_method1("list", (manage.call1((&brackets, context))?,))?;
        let by_entry = PyDict::new(py);
        for bracket in brackets.try_iter()? {
            let bracket = bracket?;
            by_entry.set_item(bracket.getattr("entry_order_id")?, bracket)?;
        }
        self.by_entry = Some(by_entry);
        let broker = config.getattr("brokers")?.get_item(&self.account)?;
        self.manager = Some(self.runner.call_method1("_manager_for", (&self.account, broker))?);
        actions.try_iter()?.collect()
    }
    fn classify(&self, action: &Self::Value) -> PyResult<ExitKind> {
        let classes = self.runner.py().import("trade_engine.domain.exits")?;
        let moved = action.is_instance(&classes.getattr("MoveStop")?)?;
        let closed = action.is_instance(&classes.getattr("ClosePosition")?)?;
        let reduced = action.is_instance(&classes.getattr("ReducePosition")?)?;
        let command_id: String = action.getattr("command_id")?.extract()?;
        let entry_order_id: String = action.getattr("entry_order_id")?.extract()?;
        let kind: String = action.get_type().getattr("__name__")?.extract()?;
        let open_bracket = self.by_entry.as_ref().expect("brackets inspected").contains(action.getattr("entry_order_id")?)?;
        rules::exit_action(&command_id, &entry_order_id, &self.account, &kind, open_bracket, moved || closed || reduced)
            .map_err(super::flow_refusal)?;
        Ok(if moved { ExitKind::Move } else if closed { ExitKind::Close } else { ExitKind::Reduce })
    }
    fn move_stop(&self, action: &Self::Value) -> PyResult<()> {
        let kwargs = PyDict::new(self.runner.py());
        kwargs.set_item("command_id", action.getattr("command_id")?)?;
        self.manager.as_ref().expect("manager acquired").call_method("move_stop",
            (action.getattr("entry_order_id")?, action.getattr("stop_price")?), Some(&kwargs))?;
        Ok(())
    }
    fn close(&self, action: &Self::Value, reduce: bool) -> PyResult<Self::Value> {
        let kwargs = PyDict::new(self.runner.py());
        kwargs.set_item("command_id", action.getattr("command_id")?)?;
        kwargs.set_item("reason", action.getattr("reason")?)?;
        let manager = self.manager.as_ref().expect("manager acquired");
        if reduce {
            manager.call_method("reduce_bracket",
                (action.getattr("entry_order_id")?, action.getattr("fraction")?), Some(&kwargs))
        } else { manager.call_method("close_bracket", (action.getattr("entry_order_id")?,), Some(&kwargs)) }
    }
    fn terminal(&self, order: &Self::Value) -> PyResult<bool> { terminal(order) }
    fn reconcile(&self, order: &Self::Value) -> PyResult<()> {
        self.manager.as_ref().expect("manager acquired").call_method1("reconcile_order", (order.getattr("order_id")?,))?;
        Ok(())
    }
}

#[pyfunction]
fn eod_entries<'py>(runner: Value<'py>, account: String, session: Value<'py>, tally: Value<'py>, options: bool) -> PyResult<i64> {
    let mut host = Inputs { runner, account, session, tally, options, signals: None,
        iterator: None, strategy: None, risk: None, manager: None };
    flow::entries(&mut host)
}
#[pyfunction]
fn eod_manage_positions<'py>(runner: Value<'py>, account: String, session: Value<'py>, closes: Value<'py>) -> PyResult<i64> {
    let mut host = Exits { runner, account, session, closes, manager: None, by_entry: None };
    flow::manage_positions(&mut host)
}
pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(eod_entries, m)?)?;
    m.add_function(wrap_pyfunction!(eod_manage_positions, m)?)?;
    Ok(())
}

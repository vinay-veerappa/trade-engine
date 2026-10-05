use std::cell::RefCell;
use pyo3::exceptions::{PyAttributeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};
use te_core::runtime::eod_flow::{self as flow, ActionKind, EntryHost, RoutingHost};

type Value<'py> = Bound<'py, PyAny>;

fn rule(op: &str, text: Vec<String>, numbers: Vec<i64>, flags: Vec<bool>) -> PyResult<te_core::runtime::Plan> {
    te_core::runtime::decide(op, &text, &numbers, &flags, &[])
        .map_err(super::flow_refusal)
}
fn optional<'py>(value: &Value<'py>, name: &str) -> PyResult<Value<'py>> {
    match value.getattr(name) {
        Ok(value) => Ok(value),
        Err(e) if e.is_instance_of::<PyAttributeError>(value.py()) => Ok(value.py().None().into_bound(value.py())),
        Err(e) => Err(e),
    }
}
fn typename(value: &Value<'_>) -> PyResult<String> { value.get_type().getattr("__name__")?.extract() }
fn text(value: &Value<'_>) -> PyResult<String> { value.str()?.extract() }

struct Router<'py> {
    owner: Value<'py>,
    account: String,
    session: Value<'py>,
    snapshot: Value<'py>,
    snapshots: Value<'py>,
    cause: Value<'py>,
    manager: RefCell<Option<Value<'py>>>,
    manage: RefCell<Option<Value<'py>>>,
}

impl<'py> Router<'py> {
    fn context(&self) -> PyResult<Value<'py>> {
        let py = self.owner.py();
        let state = self.owner.getattr("_ledger")?.call_method1("state", (&self.account,))?;
        let manage_now = self.owner.getattr("_clock")?.call_method0("now_utc")?;
        let structures = py.import("trade_engine.oms.options")?.call_method1("open_structures", (state,))?;
        let kwargs = PyDict::new(py);
        kwargs.set_item("snapshot", &self.snapshot)?;
        kwargs.set_item("snapshots", &self.snapshots)?;
        self.owner.call_method("context", (&self.account, &self.session, manage_now, structures), Some(&kwargs))
    }
}

impl<'py> RoutingHost for Router<'py> {
    type Action = Value<'py>;
    type Tally = Value<'py>;
    type Error = PyErr;

    fn manager(&self) -> PyResult<()> {
        let manager = self.owner.call_method1("manager", (&self.account,))?;
        *self.manager.borrow_mut() = Some(manager);
        Ok(())
    }
    fn match_snapshot(&self) -> PyResult<()> {
        self.owner.call_method1("match_snapshot",
            (&self.account, &self.snapshot, &self.cause, self.snapshot.getattr("as_of")?))?;
        Ok(())
    }
    fn remember(&self) -> PyResult<()> {
        self.snapshots.set_item(self.snapshot.getattr("underlying")?, &self.snapshot)
    }
    fn has_strategy(&self) -> PyResult<bool> {
        let strategy = self.owner.getattr("_strategies")?.call_method1("get", (&self.account,))?;
        let manage = optional(&strategy, "manage_options")?;
        let callable = manage.is_callable();
        *self.manage.borrow_mut() = Some(manage);
        Ok(callable)
    }
    fn actions(&self) -> PyResult<Vec<Self::Action>> {
        let manage = self.manage.borrow().as_ref().expect("strategy inspected").clone();
        let actions = self.owner.py().import("builtins")?.call_method1("list", (manage.call1((self.context()?,))?,))?;
        actions.try_iter()?.collect()
    }
    fn taken(&self, action: &Self::Action) -> PyResult<bool> {
        self.owner.call_method1("taken", (action,))?.extract()
    }
    fn classify(&self, action: &Self::Action) -> PyResult<ActionKind> {
        let classes = self.owner.py().import("trade_engine.domain.option_orders")?;
        let underlying = if !self.snapshot.is_none() {
            self.owner.call_method1("_action_underlying", (&self.account, action))?.extract::<String>()?
        } else { String::new() };
        let entry = action.is_instance(&classes.getattr("OptionIntent")?)?;
        let structure = action.is_instance(&classes.getattr("CloseStructure")?)?;
        let holding = action.is_instance(&classes.getattr("CloseHolding")?)?;
        let command = optional(action, "command_id")?;
        rule("routing:action", vec![self.account.clone(), typename(action)?,
            if command.is_none() { "?".into() } else { text(&command)? }, underlying,
            if self.snapshot.is_none() { String::new() } else { self.snapshot.getattr("underlying")?.extract()? }],
            vec![], vec![!self.snapshot.is_none(), entry || structure || holding])?;
        if entry { Ok(ActionKind::Entry) }
        else if structure { Ok(ActionKind::Structure) }
        else if holding { Ok(ActionKind::Holding) }
        else {
            let message = format!("Strategy for '{}' returned {}; options actions are OptionIntent, CloseStructure or CloseHolding",
                self.account, typename(action)?);
            Err(PyErr::from_value(self.owner.py().import("trade_engine.eod.runner")?.getattr("EodRunnerError")?.call1((message,))?))
        }
    }
    fn enter(&self, action: &Self::Action) -> PyResult<i64> {
        self.owner.call_method1("enter_option", (&self.account, &self.session, action, &self.snapshot, &self.snapshots))?.extract()
    }
    fn close(&self, action: &Self::Action, holding: bool) -> PyResult<()> {
        let manager = self.manager.borrow().as_ref().expect("manager acquired").clone();
        manager.call_method1(if holding { "close_holding" } else { "close" }, (&self.account, action))?;
        Ok(())
    }
    fn apply_actions(&self, actions: Vec<Self::Action>) -> PyResult<(i64, i64)> {
        let tally = self.owner.call_method1("apply", (&self.account, &self.session,
            PyList::new(self.owner.py(), actions)?, &self.snapshot, &self.snapshots, &self.cause))?;
        Ok((tally.getattr("orders_submitted")?.extract()?, tally.getattr("exit_actions")?.extract()?))
    }
    fn tally(&self, orders: i64, exits: i64, snapshots: i64) -> PyResult<Self::Tally> {
        self.owner.py().import("trade_engine.eod.options_routing")?.getattr("RoutingTally")?.call1((orders, exits, snapshots))
    }
    fn rounds_refusal(&self) -> PyErr {
        match (|| {
            let rounds = self.owner.py().import("trade_engine.eod.options_routing")?.getattr("ROUNDS")?.extract()?;
            rule("routing:rounds", vec![self.account.clone(), self.snapshot.getattr("underlying")?.extract()?], vec![rounds], vec![])?;
            Err(PyValueError::new_err("snapshot round guard did not refuse"))
        })() {
            Err(e) => e,
            Ok(()) => unreachable!("round guard"),
        }
    }
}

struct Entry<'py> {
    owner: Value<'py>,
    account: String,
    session: Value<'py>,
    snapshot: Value<'py>,
    snapshots: Value<'py>,
    engine: Option<Value<'py>>,
}

impl<'py> EntryHost for Entry<'py> {
    type Value = Value<'py>;
    type Error = PyErr;
    fn validate(&mut self, intent: &Self::Value) -> PyResult<()> {
        let valid = intent.is_instance(&self.owner.py().import("trade_engine.domain.option_orders")?.getattr("OptionIntent")?)?;
        let account = optional(intent, "account_id")?;
        let id = optional(intent, "intent_id")?;
        rule("routing:entry", vec![self.account.clone(), typename(intent)?,
            if account.is_none() { String::new() } else { text(&account)? },
            if id.is_none() { String::new() } else { text(&id)? }], vec![], vec![valid])?;
        let engine = self.owner.getattr("_option_risk_engines")?.call_method1("get", (&self.account,))?;
        rule("routing:engine", vec![self.account.clone(), intent.getattr("intent_id")?.extract()?], vec![], vec![!engine.is_none()])?;
        self.engine = Some(engine);
        Ok(())
    }
    fn evaluate(&self, intent: &Self::Value) -> PyResult<Self::Value> {
        let py = self.owner.py();
        let now = self.owner.getattr("_clock")?.call_method0("now_utc")?;
        let state = self.owner.getattr("_ledger")?.call_method1("state", (&self.account,))?;
        let structures = py.import("trade_engine.oms.options")?.call_method1("open_structures", (state,))?;
        let kwargs = PyDict::new(py);
        kwargs.set_item("snapshot", &self.snapshot)?;
        kwargs.set_item("snapshots", &self.snapshots)?;
        let context = self.owner.call_method("context", (&self.account, &self.session, now, structures), Some(&kwargs))?;
        self.engine.as_ref().expect("validated engine").call_method1("evaluate", (intent, context))
    }
    fn record(&self, intent: &Self::Value, verdict: &Self::Value) -> PyResult<()> {
        self.owner.call_method1("_record_verdict", (&self.account, intent, verdict))?;
        Ok(())
    }
    fn approved(&self, intent: &Self::Value, verdict: &Self::Value) -> PyResult<(bool, bool)> {
        let quantity = verdict.getattr("approved_quantity")?;
        let flags = rule("routing:approved", vec![text(&quantity)?, text(&intent.getattr("quantity")?)?],
            vec![], vec![verdict.getattr("accepted")?.is_truthy()?, !quantity.is_none()])?.flags;
        Ok((flags[0], flags[1]))
    }
    fn resize(&self, intent: &Self::Value, verdict: &Self::Value) -> PyResult<Self::Value> {
        let kwargs = PyDict::new(self.owner.py());
        kwargs.set_item("quantity", verdict.getattr("approved_quantity")?)?;
        self.owner.py().import("dataclasses")?.getattr("replace")?.call((intent,), Some(&kwargs))
    }
    fn open(&self, intent: &Self::Value) -> PyResult<()> {
        self.owner.call_method1("manager", (&self.account,))?.call_method1("open", (intent,))?;
        Ok(())
    }
}

#[pyfunction]
fn options_manage<'py>(owner: Value<'py>, account: String, session: Value<'py>, snapshot: Value<'py>,
    snapshots: Value<'py>, cause: Value<'py>) -> PyResult<Value<'py>> {
    let rounds = owner.py().import("trade_engine.eod.options_routing")?.getattr("ROUNDS")?.extract::<usize>()?;
    let host = Router { owner, account, session, snapshot, snapshots, cause,
        manager: RefCell::new(None), manage: RefCell::new(None) };
    flow::manage_snapshot(&host, rounds)
}

#[pyfunction]
fn options_apply<'py>(owner: Value<'py>, account: String, session: Value<'py>, actions: Value<'py>,
    snapshot: Value<'py>, snapshots: Value<'py>, cause: Value<'py>) -> PyResult<Value<'py>> {
    let host = Router { owner, account, session, snapshot, snapshots, cause,
        manager: RefCell::new(None), manage: RefCell::new(None) };
    // Acquire the manager before touching even an action iterator which can raise.
    host.manager()?;
    let iterator = actions.try_iter()?;
    let (orders, exits) = flow::apply(&host, iterator)?;
    host.tally(orders, exits, 0)
}

#[pyfunction]
fn options_enter<'py>(owner: Value<'py>, account: String, session: Value<'py>, intent: Value<'py>,
    snapshot: Value<'py>, snapshots: Value<'py>) -> PyResult<i64> {
    let mut host = Entry { owner, account, session, snapshot, snapshots, engine: None };
    flow::enter_option(&mut host, intent)
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(options_manage, m)?)?;
    m.add_function(wrap_pyfunction!(options_apply, m)?)?;
    m.add_function(wrap_pyfunction!(options_enter, m)?)?;
    Ok(())
}

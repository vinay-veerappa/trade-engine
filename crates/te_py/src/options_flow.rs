use std::cell::RefCell;
use pyo3::exceptions::{PyAttributeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};
use te_core::ledger::model::Obj;
use te_core::runtime::eod_flow::{self as flow, ActionKind, EntryHost, RoutingHost};
use te_core::runtime::eod_glue::{self as glue, Ledger as _};
use te_core::runtime::routing;
use te_core::sim::snapshot::underlying_of;

use crate::store_ledger::{self as sl, Pending};

/// how many times a strategy may act at one snapshot: a buy-write needs two
const ROUNDS: usize = 4;

type Value<'py> = Bound<'py, PyAny>;

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
    /// The underlying an action concerns, checked by `routing::underlying` (was `_action_underlying`).
    fn action_underlying(&self, action: &Value<'py>) -> PyResult<String> {
        let classes = self.owner.py().import("trade_engine.domain.option_orders")?;
        let (kind, underlying, is_order) = if action.is_instance(&classes.getattr("OptionIntent")?)? {
            let instrument = sl::instrument_of(&action.getattr("instrument")?)?;
            ("intent", underlying_of(&instrument).map_err(super::flow_refusal)?, false)
        } else if action.is_instance(&classes.getattr("CloseHolding")?)? {
            ("holding", action.getattr("instrument")?.getattr("symbol")?.extract::<String>()?, false)
        } else if action.is_instance(&classes.getattr("CloseStructure")?)? {
            let order_id: String = action.getattr("entry_order_id")?.extract()?;
            let store = sl::ledger_store(&self.owner.getattr("_ledger")?)?;
            let pending = Pending::default();
            let account = self.account.as_str();
            let entry = sl::with_native(&store, &self.owner.getattr("_clock")?, &pending, |l, _| {
                let state = l.state(account)?;
                Ok(state.orders.get(&order_id).map(|order| order.instrument.clone()))
            })?;
            match entry {
                Some(instrument) => ("close", underlying_of(&instrument).map_err(super::flow_refusal)?, true),
                None => ("close", String::new(), false),
            }
        } else {
            ("unknown", String::new(), false)
        };
        let command = optional(action, "command_id")?;
        let command_id = if command.is_none() { String::new() } else { text(&command)? };
        let named = optional(action, "entry_order_id")?;
        let named_id = if named.is_none() { String::new() } else { text(&named)? };
        routing::underlying(kind, &self.account, &command_id, &named_id, is_order, &underlying, &typename(action)?)
            .map_err(super::flow_refusal)
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
        let command = optional(action, "command_id")?;
        if !command.is_truthy()? { return Ok(false); }
        let command_id = text(&command)?;
        let store = sl::ledger_store(&self.owner.getattr("_ledger")?)?;
        // The native `has_command`: the store's own existence check, without decoding the event.
        let direct: bool = store.call_method1("has_command", (&command_id,))?.extract()?;
        if routing::taken(true, direct, false, false) { return Ok(true); }
        let intent = action.is_instance(&self.owner.py().import("trade_engine.domain.option_orders")?.getattr("OptionIntent")?)?;
        if !routing::risk_lookup(true, intent) { return Ok(false); }
        let pending = Pending::default();
        let verdict = sl::with_native(&store, &self.owner.getattr("_clock")?, &pending,
            |l, _| glue::recorded_verdict(&*l, &command_id))?;
        let refused = verdict.as_ref().is_some_and(|v| !v.accepted);
        Ok(routing::taken(true, false, verdict.is_some(), refused))
    }
    fn classify(&self, action: &Self::Action) -> PyResult<ActionKind> {
        let classes = self.owner.py().import("trade_engine.domain.option_orders")?;
        let underlying = if !self.snapshot.is_none() { self.action_underlying(action)? } else { String::new() };
        let entry = action.is_instance(&classes.getattr("OptionIntent")?)?;
        let structure = action.is_instance(&classes.getattr("CloseStructure")?)?;
        let holding = action.is_instance(&classes.getattr("CloseHolding")?)?;
        let command = optional(action, "command_id")?;
        let name = if command.is_none() { "?".to_string() } else { text(&command)? };
        let snapshot_underlying = if self.snapshot.is_none() { String::new() } else { self.snapshot.getattr("underlying")?.extract::<String>()? };
        routing::action(&self.account, &typename(action)?, &name, &underlying, &snapshot_underlying,
            !self.snapshot.is_none(), entry || structure || holding).map_err(super::flow_refusal)?;
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
        match (|| -> PyResult<()> {
            routing::rounds(&self.account, &self.snapshot.getattr("underlying")?.extract::<String>()?, ROUNDS as i64)
                .map_err(super::flow_refusal)?;
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
    fn recorded(&self, intent: &Self::Value) -> PyResult<Option<Self::Value>> {
        let command: String = intent.getattr("command_id")?.extract()?;
        let store = sl::ledger_store(&self.owner.getattr("_ledger")?)?;
        let pending = Pending::default();
        let verdict = sl::with_native(&store, &self.owner.getattr("_clock")?, &pending,
            |l, _| glue::recorded_verdict(&*l, &command))?;
        match verdict {
            Some(verdict) => Ok(Some(sl::obj_py(self.owner.py(), &Obj::RiskVerdict(verdict))?)),
            None => Ok(None),
        }
    }
    fn validate(&mut self, intent: &Self::Value) -> PyResult<()> {
        let valid = intent.is_instance(&self.owner.py().import("trade_engine.domain.option_orders")?.getattr("OptionIntent")?)?;
        let account = optional(intent, "account_id")?;
        let id = optional(intent, "intent_id")?;
        let account_text = if account.is_none() { String::new() } else { text(&account)? };
        let id_text = if id.is_none() { String::new() } else { text(&id)? };
        routing::entry(&self.account, &typename(intent)?, &account_text, &id_text, valid).map_err(super::flow_refusal)?;
        let engine = self.owner.getattr("_option_risk_engines")?.call_method1("get", (&self.account,))?;
        routing::engine(&self.account, &intent.getattr("intent_id")?.extract::<String>()?, !engine.is_none())
            .map_err(super::flow_refusal)?;
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
        let command: String = intent.getattr("command_id")?.extract()?;
        let verdict = sl::verdict_of(verdict)?;
        let store = sl::ledger_store(&self.owner.getattr("_ledger")?)?;
        let pending = Pending::default();
        let account = self.account.as_str();
        sl::with_native(&store, &self.owner.getattr("_clock")?, &pending,
            |l, c| glue::record_verdict(l, c, account, &command, verdict))
    }
    fn approved(&self, intent: &Self::Value, verdict: &Self::Value) -> PyResult<(bool, bool)> {
        let quantity = verdict.getattr("approved_quantity")?;
        let accepted = verdict.getattr("accepted")?.is_truthy()?;
        routing::approved(accepted, !quantity.is_none(), &text(&quantity)?, &text(&intent.getattr("quantity")?)?)
            .map_err(super::flow_refusal)
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
    let host = Router { owner, account, session, snapshot, snapshots, cause,
        manager: RefCell::new(None), manage: RefCell::new(None) };
    flow::manage_snapshot(&host, ROUNDS)
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

/// The phase a context reports (`routing::phase`): a named phase, else the snapshot or close.
#[pyfunction]
#[pyo3(signature = (phase, has_snapshot))]
fn routing_phase(phase: Option<String>, has_snapshot: bool) -> String {
    routing::phase(phase.as_deref().unwrap_or(""), has_snapshot)
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(routing_phase, m)?)?;
    m.add_function(wrap_pyfunction!(options_manage, m)?)?;
    m.add_function(wrap_pyfunction!(options_apply, m)?)?;
    m.add_function(wrap_pyfunction!(options_enter, m)?)?;
    Ok(())
}

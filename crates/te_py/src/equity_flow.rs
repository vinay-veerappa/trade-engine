use pyo3::exceptions::PyAttributeError;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyIterator, PyList, PyTuple};
use te_core::ledger::model::Obj;
use te_core::runtime::eod as rules;
use te_core::runtime::eod_flow::{self as flow, EquityEntryHost, ExitHost, ExitKind, SignalHost};
use te_core::runtime::eod_glue::{self as glue, Ledger as _, OpenBracket, RiskContext};
use te_core::runtime::routing;

use crate::store::LedgerStore;
use crate::store_ledger::{self as sl, Pending};

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

/// The fields `RiskContext` takes that the fold cannot supply: `None`, as the risk context has always set them.
const UNSOURCED: [&str; 12] = [
    "current_price", "portfolio_heat", "industry", "industry_positions", "average_dollar_volume_20d",
    "sessions_until_earnings", "regime", "macro_high_risk_day", "drawdown_from_peak_frac",
    "previous_session_pnl_frac", "venue_orders_today", "venue_daily_pnl",
];

/// The fold's risk inputs as the Python `RiskContext`.
fn risk_context_py<'py>(py: Python<'py>, context: &RiskContext) -> PyResult<Value<'py>> {
    let kwargs = PyDict::new(py);
    let equity = match &context.equity {
        Some(equity) => sl::decimal_py(py, equity)?,
        None => py.None().into_bound(py),
    };
    kwargs.set_item("equity", equity)?;
    kwargs.set_item("gross_exposure", sl::decimal_py(py, &context.gross_exposure)?)?;
    kwargs.set_item("open_positions", context.open_positions)?;
    kwargs.set_item("current_position_quantity", context.current_position_quantity)?;
    for name in UNSOURCED {
        kwargs.set_item(name, py.None())?;
    }
    py.import("trade_engine.risk")?.getattr("RiskContext")?.call((), Some(&kwargs))
}

/// A te_core open bracket as the Python `OpenBracket` `_open_brackets` builds.
fn open_bracket_py<'py>(py: Python<'py>, bracket: &OpenBracket) -> PyResult<Value<'py>> {
    let mut targets = Vec::with_capacity(bracket.open_targets.len());
    for (limit, remaining) in &bracket.open_targets {
        targets.push(PyTuple::new(py, [sl::decimal_py(py, limit)?, sl::decimal_py(py, remaining)?])?);
    }
    let last_close = match &bracket.last_close {
        Some(close) => sl::decimal_py(py, close)?,
        None => py.None().into_bound(py),
    };
    let kwargs = PyDict::new(py);
    kwargs.set_item("entry_order_id", &bracket.entry_order_id)?;
    kwargs.set_item("account_id", &bracket.account_id)?;
    kwargs.set_item("instrument", sl::instrument_py(py, &bracket.instrument)?)?;
    kwargs.set_item("side", sl::side_py(py, bracket.side)?)?;
    kwargs.set_item("entry_quantity", sl::decimal_py(py, &bracket.entry_quantity)?)?;
    kwargs.set_item("open_quantity", sl::decimal_py(py, &bracket.open_quantity)?)?;
    kwargs.set_item("average_entry_price", sl::decimal_py(py, &bracket.average_entry_price)?)?;
    kwargs.set_item("entry_filled_at", sl::datetime_py(py, &bracket.entry_filled_at)?)?;
    kwargs.set_item("entry_session", sl::date_py(py, bracket.entry_session)?)?;
    kwargs.set_item("sessions_held", bracket.sessions_held)?;
    kwargs.set_item("stop_price", sl::decimal_py(py, &bracket.stop_price)?)?;
    kwargs.set_item("targets_filled", bracket.targets_filled)?;
    kwargs.set_item("open_targets", PyTuple::new(py, targets)?)?;
    kwargs.set_item("last_close", last_close)?;
    py.import("trade_engine.domain.exits")?.getattr("OpenBracket")?.call((), Some(&kwargs))
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
    /// The plain ledger's store, past the gate in `store_ledger::native_store`.
    native: Bound<'py, LedgerStore>,
    pending: Pending,
}

impl<'py> Inputs<'py> {
    fn config(&self) -> PyResult<Value<'py>> { self.runner.getattr("_config") }
    fn clock(&self) -> PyResult<Value<'py>> { self.runner.getattr("_clock") }
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
        let signal = sl::signal_of(signal)?;
        let account = self.account.as_str();
        sl::with_native(&self.native, &self.clock()?, &self.pending, |l, c| glue::record_signal(l, c, account, signal))
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
        let command: String = intent.getattr("command_id")?.extract()?;
        let verdict = sl::with_native(&self.native, &self.clock()?, &self.pending,
            |l, _| glue::recorded_verdict(&*l, &command))?;
        match verdict {
            Some(verdict) => Ok(Some(sl::obj_py(self.runner.py(), &Obj::RiskVerdict(verdict))?)),
            None => Ok(None),
        }
    }
    fn evaluate(&self, intent: &Self::Value) -> PyResult<Self::Value> {
        let py = self.runner.py();
        let builder = self.config()?.getattr("context_builder")?;
        let context = if builder.is_none() {
            let instrument = sl::instrument_of(&intent.getattr("instrument")?)?;
            let account = self.account.as_str();
            let context = sl::with_native(&self.native, &self.clock()?, &self.pending, |l, _| {
                let state = l.state(account)?;
                glue::derive_risk_context(&state, &instrument)
            })?;
            risk_context_py(py, &context)?
        } else {
            let state = self.runner.getattr("_ledger")?.call_method1("state", (&self.account,))?;
            builder.call1((intent, state, &self.session))?
        };
        self.risk.as_ref().expect("configured risk").call_method1("evaluate", (intent, context))
    }
    fn record(&self, intent: &Self::Value, verdict: &Self::Value) -> PyResult<()> {
        let command: String = intent.getattr("command_id")?.extract()?;
        let verdict = sl::verdict_of(verdict)?;
        let account = self.account.as_str();
        sl::with_native(&self.native, &self.clock()?, &self.pending,
            |l, c| glue::record_verdict(l, c, account, &command, verdict))
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
    /// The plain ledger's store, past the gate in `store_ledger::native_store`.
    native: Bound<'py, LedgerStore>,
    pending: Pending,
}

impl<'py> Exits<'py> {
    /// `_open_brackets(account, session, closes)`, from the glue's brackets.
    fn brackets(&self) -> PyResult<Value<'py>> {
        let py = self.runner.py();
        let closes = sl::closes(&self.closes)?;
        let account = self.account.as_str();
        let session = sl::date_of(&self.session)?;
        let clock = self.runner.getattr("_clock")?;
        let brackets = sl::with_native(&self.native, &clock, &self.pending, |l, _| {
            let state = l.state(account)?;
            glue::open_brackets(&state, account, session, &closes)
        })?;
        let items = brackets.iter().map(|bracket| open_bracket_py(py, bracket)).collect::<PyResult<Vec<_>>>()?;
        Ok(PyList::new(py, items)?.into_any())
    }
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
        let brackets = self.brackets()?;
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

/// `_submit_new_orders` / `_submit_new_option_entries`, natively: the account's D+1 entries
/// (``eod_entries`` with ``tally=None`` for equity accounts, ``tally`` for options accounts).
pub(crate) fn entries<'py>(runner: Value<'py>, account: String, session: Value<'py>, tally: Value<'py>, options: bool) -> PyResult<i64> {
    let native = sl::native_store(&runner)?;
    let mut host = Inputs { runner, account, session, tally, options, signals: None,
        iterator: None, strategy: None, risk: None, manager: None, native, pending: Pending::default() };
    flow::entries(&mut host)
}
/// `_manage_positions`, natively: hand the strategy its open brackets and apply the exits it
/// asks for. Runs after marks and before D+1 entries. ``manage_positions`` is optional on a
/// strategy; one without it keeps only its stops and targets. Every action goes through the
/// OMS, which refuses a stop that would loosen (I5); a refused action fails the run loudly
/// rather than leaving a position managed by half its rules.
pub(crate) fn manage_positions<'py>(runner: Value<'py>, account: String, session: Value<'py>, closes: Value<'py>) -> PyResult<i64> {
    let native = sl::native_store(&runner)?;
    let mut host = Exits { runner, account, session, closes, manager: None, by_entry: None,
        native, pending: Pending::default() };
    flow::manage_positions(&mut host)
}
pub fn register(_m: &Bound<'_, PyModule>) -> PyResult<()> {
    Ok(())
}

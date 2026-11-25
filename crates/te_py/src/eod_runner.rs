//! W7-CORE-C9: `EodRunner`'s shell, as a `#[pyclass(subclass)]` base.
//!
//! The sequencing already lives in `eod_flow.rs` (reached before through `rs.eod_run` /
//! `rs.eod_pass`); this type owns the Python shell around it: `run`/`run_pass` re-sync
//! brackets then hand the runner to the native flow, `_restore_venue` rebuilds an empty
//! simulated venue from the ledger, `_manager_for`/`_option_manager`/`_router`/`_enter_option`
//! cache and route through the Python collaborators, and `_reconcile_after_bar`/`_resync_brackets`
//! reach `oms.reconcile` through Python attribute lookup so the tests' monkeypatches still
//! apply. The module-level `void_session` and `write_off_order` become `#[pyfunction]`s that
//! call `te_core::runtime::eod_glue` directly.
//!
//! Every collaborator (ledger, clock, calendar, market data, brokers, managers, routers) is a
//! Python object reached through attribute lookup; none is assumed to be a concrete Rust type.
//! The five attributes the native flow reads off the runner (`_ledger`, `_clock`, `_calendar`,
//! `_market_data`, `_config`) stay Python instance attributes, set by the Python subclass's
//! `__init__`, so `eod_flow.rs`'s `getattr` keeps working and the tests can still monkeypatch.

use pyo3::exceptions::PyAttributeError;
use pyo3::prelude::*;
use pyo3::sync::GILOnceCell;
use pyo3::types::{PyDate, PyDateTime, PyDict, PyTuple};
use te_core::runtime::eod_glue as glue;

use crate::store_ledger::{self as sl, with_native, Pending};

type Value<'py> = Bound<'py, PyAny>;

fn values<'py>(value: &Value<'py>) -> PyResult<Vec<Value<'py>>> {
    value.try_iter()?.collect()
}

fn sorted<'py>(value: &Value<'py>) -> PyResult<Vec<Value<'py>>> {
    let py = value.py();
    values(&py.import("builtins")?.call_method1("sorted", (value,))?)
}

fn text_of(value: &Value<'_>) -> Option<String> {
    value.extract::<String>().ok()
}

fn repr(value: &Value<'_>) -> PyResult<String> {
    value.repr()?.extract()
}

/// The Python classes, constants and exceptions `EodRunnerBase` reaches, imported once.
struct Ctx {
    sim_broker: Py<PyAny>,
    snapshot_venue: Py<PyAny>,
    order_manager: Py<PyAny>,
    option_order_manager: Py<PyAny>,
    min_time: Py<PyAny>,
    eod_runner_error: Py<PyAny>,
    reconcile_error: Py<PyAny>,
    restore_error: Py<PyAny>,
}

impl Ctx {
    fn new(py: Python<'_>) -> PyResult<Ctx> {
        let sim = py.import("trade_engine.sim")?;
        let manager = py.import("trade_engine.oms.manager")?;
        let options = py.import("trade_engine.oms.options")?;
        let runner = py.import("trade_engine.eod.runner")?;
        let reconcile = py.import("trade_engine.oms.reconcile")?;
        let restore = py.import("trade_engine.oms.restore")?;
        Ok(Ctx {
            sim_broker: sim.getattr("SimBroker")?.unbind(),
            snapshot_venue: sim.getattr("SnapshotVenue")?.unbind(),
            order_manager: manager.getattr("OrderManager")?.unbind(),
            option_order_manager: options.getattr("OptionOrderManager")?.unbind(),
            min_time: runner.getattr("MIN_TIME")?.unbind(),
            eod_runner_error: runner.getattr("EodRunnerError")?.unbind(),
            reconcile_error: reconcile.getattr("ReconcileError")?.unbind(),
            restore_error: restore.getattr("RestoreError")?.unbind(),
        })
    }
}

fn ctx(py: Python<'_>) -> PyResult<&'static Ctx> {
    static CTX: GILOnceCell<Ctx> = GILOnceCell::new();
    CTX.get_or_try_init(py, || Ctx::new(py))
}

/// `EodRunnerError(str(err))` when `e` is the given exception class, else `e` unchanged.
fn map_to_eod(py: Python<'_>, e: PyErr, exc: &Bound<'_, PyAny>, eod: &Bound<'_, PyAny>) -> PyErr {
    if e.is_instance(py, exc) {
        let msg: String = e
            .value(py)
            .str()
            .and_then(|s| s.extract())
            .unwrap_or_default();
        match eod.call1((msg,)) {
            Ok(value) => PyErr::from_value(value),
            Err(fallback) => fallback,
        }
    } else {
        e
    }
}

/// `EodRunner`'s method bodies, in the exact order the Python method ran them. All state is
/// Python instance attributes (`_ledger`, `_clock`, `_calendar`, `_market_data`, `_config`,
/// `_managers`, `_option_managers`, `_option_routers`), set by `EodRunner.__init__`. A method
/// that tests subclass or monkeypatch must be reached through `slf.call_method*`, never as a
/// direct Rust call.
#[pyclass(subclass, module = "trade_engine_rs")]
pub struct EodRunnerBase;

#[pymethods]
impl EodRunnerBase {
    #[new]
    fn new(
        ledger: &Bound<'_, PyAny>,
        clock: &Bound<'_, PyAny>,
        calendar: &Bound<'_, PyAny>,
        market_data: &Bound<'_, PyAny>,
        config: &Bound<'_, PyAny>,
    ) -> Self {
        // The Python subclass's __init__ sets the attributes and builds the routers; these
        // are accepted only so __new__ agrees with __init__'s signature.
        let _ = (ledger, clock, calendar, market_data, config);
        EodRunnerBase
    }

    fn run<'py>(slf: &Bound<'py, Self>, session: &Bound<'py, PyAny>) -> PyResult<Value<'py>> {
        let py = slf.py();
        slf.call_method0("_resync_brackets")?;
        py.import("trade_engine_rs")?.call_method1("eod_run", (slf.as_any(), session))
    }

    fn run_morning<'py>(
        slf: &Bound<'py, Self>,
        session: &Bound<'py, PyAny>,
        through: &Bound<'py, PyAny>,
    ) -> PyResult<Value<'py>> {
        slf.call_method1("run_pass", (session, through, "morning"))
    }

    fn _resync_brackets<'py>(slf: &Bound<'py, Self>) -> PyResult<()> {
        let py = slf.py();
        let brokers = slf.getattr("_config")?.getattr("brokers")?;
        // Looked up by name each run: the tests monkeypatch `trade_engine.eod.runner`'s.
        let resync = py.import("trade_engine.eod.runner")?.getattr("resync_open_brackets")?;
        let ledger = slf.getattr("_ledger")?;
        for account_id in sorted(&brokers)? {
            let account_id: String = account_id.extract()?;
            let broker = brokers.get_item(&account_id)?;
            let manager = slf.call_method1("_manager_for", (&account_id, broker))?;
            resync.call1((&ledger, &manager, &account_id))?;
        }
        Ok(())
    }

    fn run_pass<'py>(
        slf: &Bound<'py, Self>,
        session: &Bound<'py, PyAny>,
        through: &Bound<'py, PyAny>,
        name: &Bound<'py, PyAny>,
    ) -> PyResult<Value<'py>> {
        let py = slf.py();
        slf.call_method0("_resync_brackets")?;
        py.import("trade_engine_rs")?.call_method1("eod_pass", (slf.as_any(), session, through, name))
    }

    #[pyo3(signature = (session, accounts, *, reason, owner))]
    fn void_session<'py>(
        slf: &Bound<'py, Self>,
        session: &Bound<'py, PyAny>,
        accounts: &Bound<'py, PyAny>,
        reason: &Bound<'py, PyAny>,
        owner: &Bound<'py, PyAny>,
    ) -> PyResult<Value<'py>> {
        let py = slf.py();
        let kwargs = PyDict::new(py);
        kwargs.set_item("reason", reason)?;
        kwargs.set_item("owner", owner)?;
        let job = slf.getattr("_config")?.getattr("job_name")?;
        py.import("trade_engine.eod.runner")?.call_method(
            "void_session",
            (
                slf.getattr("_ledger")?,
                slf.getattr("_clock")?,
                slf.getattr("_calendar")?,
                job,
                session,
                accounts,
            ),
            Some(&kwargs),
        )
    }

    // -- per-account run ---------------------------------------------------------

    fn _restore_venue<'py>(
        slf: &Bound<'py, Self>,
        account_id: &str,
        broker: &Bound<'py, PyAny>,
    ) -> PyResult<()> {
        let py = slf.py();
        let ctx = ctx(py)?;
        let is_venue = broker.is_instance(ctx.sim_broker.bind(py))?
            || broker.is_instance(ctx.snapshot_venue.bind(py))?;
        if is_venue
            && !broker.call_method1("orders", (ctx.min_time.bind(py),))?.is_truthy()?
            && !broker.call_method1("fills", (ctx.min_time.bind(py),))?.is_truthy()?
        {
            let state = slf.getattr("_ledger")?.call_method1("state", (account_id,))?;
            let restorable = slf.call_method1("_restorable", (account_id, &state))?;
            let orders = restorable.get_item(0)?;
            let fills = restorable.get_item(1)?;
            let positions = slf.call_method1("_restorable_positions", (&state,))?;
            if orders.is_truthy()? || positions.is_truthy()? {
                broker.call_method1("restore", (orders, fills, positions))?;
            }
        }
        Ok(())
    }

    fn _restorable<'py>(
        slf: &Bound<'py, Self>,
        account_id: &str,
        state: &Bound<'py, PyAny>,
    ) -> PyResult<Value<'py>> {
        let py = slf.py();
        let ctx = ctx(py)?;
        let restorable = py.import("trade_engine.oms.restore")?.getattr("restorable")?;
        let ledger = slf.getattr("_ledger")?;
        match restorable.call1((ledger, account_id, state)) {
            Ok(value) => Ok(value),
            Err(e) => Err(map_to_eod(py, e, &ctx.restore_error.bind(py), &ctx.eod_runner_error.bind(py))),
        }
    }

    #[staticmethod]
    fn _restorable_positions<'py>(state: &Bound<'py, PyAny>) -> PyResult<Value<'py>> {
        let py = state.py();
        py.import("trade_engine.oms.restore")?.call_method1("restorable_positions", (state,))
    }

    #[pyo3(signature = (account_id, broker, manager, bar_timestamp=None))]
    fn _reconcile_after_bar<'py>(
        slf: &Bound<'py, Self>,
        account_id: &str,
        broker: &Bound<'py, PyAny>,
        manager: &Bound<'py, PyAny>,
        bar_timestamp: Option<&Bound<'py, PyAny>>,
    ) -> PyResult<i64> {
        let py = slf.py();
        let ctx = ctx(py)?;
        let reconcile_after = py.import("trade_engine.oms.reconcile")?.getattr("reconcile_after")?;
        let since = match bar_timestamp {
            Some(ts) => ts.clone(),
            None => ctx.min_time.bind(py).clone(),
        };
        let journal = slf
            .getattr("_config")?
            .getattr("journal_accounts")?
            .call_method1("get", (account_id,))?;
        let kwargs = PyDict::new(py);
        kwargs.set_item("journal_account", journal)?;
        let result = reconcile_after.call(
            (slf.getattr("_ledger")?, slf.getattr("_clock")?, broker, manager, account_id, since),
            Some(&kwargs),
        );
        match result {
            Ok(value) => value.extract::<i64>(),
            Err(e) => {
                let mapped = map_to_eod(py, e, &ctx.reconcile_error.bind(py), &ctx.eod_runner_error.bind(py));
                Err(mapped)
            }
        }
    }

    fn _manager_for<'py>(
        slf: &Bound<'py, Self>,
        account_id: &str,
        broker: &Bound<'py, PyAny>,
    ) -> PyResult<Value<'py>> {
        let py = slf.py();
        let ctx = ctx(py)?;
        let config_broker = slf.getattr("_config")?.getattr("brokers")?.get_item(account_id)?;
        let process = config_broker.getattr("process_snapshot").or_else(|e| {
            if e.is_instance_of::<PyAttributeError>(py) {
                Ok(py.None().into_bound(py))
            } else {
                Err(e)
            }
        })?;
        if process.is_callable() {
            return Ok(slf.call_method1("_option_manager", (account_id,))?.getattr("orders")?);
        }
        let managers = slf.getattr("_managers")?;
        let manager = managers.call_method1("get", (account_id,))?;
        if manager.is_none() {
            let manager = ctx
                .order_manager
                .bind(py)
                .call1((broker, slf.getattr("_clock")?, slf.getattr("_ledger")?))?;
            managers.set_item(account_id, &manager)?;
            return Ok(manager);
        }
        Ok(manager)
    }

    // -- options accounts (O4) ---------------------------------------------------

    fn _option_manager<'py>(slf: &Bound<'py, Self>, account_id: &str) -> PyResult<Value<'py>> {
        let py = slf.py();
        let ctx = ctx(py)?;
        let option_managers = slf.getattr("_option_managers")?;
        let manager = option_managers.call_method1("get", (account_id,))?;
        if manager.is_none() {
            let broker = slf.getattr("_config")?.getattr("brokers")?.get_item(account_id)?;
            let manager = ctx
                .option_order_manager
                .bind(py)
                .call1((broker, slf.getattr("_clock")?, slf.getattr("_ledger")?))?;
            option_managers.set_item(account_id, &manager)?;
            return Ok(manager);
        }
        Ok(manager)
    }

    fn _router<'py>(slf: &Bound<'py, Self>, account_id: &str) -> PyResult<Value<'py>> {
        Ok(slf.getattr("_option_routers")?.get_item(account_id)?)
    }

    fn _enter_option<'py>(
        slf: &Bound<'py, Self>,
        account_id: &str,
        session: &Bound<'py, PyAny>,
        intent: &Bound<'py, PyAny>,
        tally: &Bound<'py, PyAny>,
        snapshot: &Bound<'py, PyAny>,
    ) -> PyResult<i64> {
        let router = slf.call_method1("_router", (account_id,))?;
        router
            .call_method1("enter_option", (account_id, session, intent, snapshot, tally.getattr("snapshots")?))?
            .extract::<i64>()
    }
}

/// `void_session` (module level): the owner's void of `session` for each account, native.
#[pyfunction]
#[pyo3(signature = (ledger, clock, calendar, job, session, accounts, *, reason, owner))]
pub(crate) fn void_session<'py>(
    py: Python<'py>,
    ledger: &Bound<'py, PyAny>,
    clock: &Bound<'py, PyAny>,
    calendar: &Bound<'py, PyAny>,
    job: String,
    session: &Bound<'py, PyAny>,
    accounts: &Bound<'py, PyAny>,
    reason: &Bound<'py, PyAny>,
    owner: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyTuple>> {
    let store = sl::require_native(ledger, calendar)?;
    // A datetime passes `isinstance(date)` but is not a session: refuse it with the core's text.
    if !session.is_instance_of::<PyDate>() || session.is_instance_of::<PyDateTime>() {
        let msg = format!("void session must be a date, got {}", repr(session)?);
        return Err(sl::refuse(session.py(), &msg));
    }
    let session = sl::date_of(session)?;
    let accounts_list = py.import("builtins")?.call_method1("list", (accounts,))?;
    let accounts = values(&accounts_list)?
        .iter()
        .map(|account| account.extract::<String>())
        .collect::<PyResult<Vec<_>>>()?;
    let (reason, owner) = (text_of(reason), text_of(owner));
    let pending = Pending::default();
    let result: Vec<String> = with_native(&store, clock, &pending, |l, c| {
        glue::void_session(l, c, &job, session, &accounts, reason.as_deref(), owner.as_deref())
    })?;
    Ok(PyTuple::new(py, result)?)
}

/// `write_off_order` (module level): the owner's write-off of one stuck working order, native.
#[pyfunction]
#[pyo3(signature = (ledger, clock, account_id, order_id, *, reason, owner, venue_cancel_confirmed=false))]
pub(crate) fn write_off_order<'py>(
    py: Python<'py>,
    ledger: &Bound<'py, PyAny>,
    clock: &Bound<'py, PyAny>,
    account_id: String,
    order_id: String,
    reason: &Bound<'py, PyAny>,
    owner: &Bound<'py, PyAny>,
    venue_cancel_confirmed: bool,
) -> PyResult<Bound<'py, PyTuple>> {
    let store = sl::ledger_store(ledger)?;
    let (reason, owner) = (text_of(reason), text_of(owner));
    let pending = Pending::default();
    let result: Vec<String> = with_native(&store, clock, &pending, |l, c| {
        glue::write_off_order(l, c, &account_id, &order_id, reason.as_deref(), owner.as_deref(), venue_cancel_confirmed)
    })?;
    Ok(PyTuple::new(py, result)?)
}

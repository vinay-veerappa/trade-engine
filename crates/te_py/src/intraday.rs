//! W7-CORE-C11: `IntradayService`'s method bodies, moved into Rust.
//!
//! `IntradayServiceBase` owns the sequencing `trade_engine.intraday.service.IntradayService`
//! did in Python: the constructor wires the Python collaborators (ledger, clock, calendar,
//! the `_EntryGate` and the `OptionRouter`), `run` re-syncs the open brackets and then drives
//! the native tick loop, and the tick / flatten / close / heartbeat hooks are the direct Rust
//! calls behind the old `native.intraday_*` shims. The router, the clock, the calendar, the
//! chain/quote source, the ledgers and the sinks stay Python and are reached through attribute
//! lookup; `OptionRouter` / `RoutingTally` and `_EntryGate` / `_TickState` stay Python (the
//! flow still imports `_TickState` from this module, and the router is ported in parallel).
//! `resync_open_brackets` is looked up on `trade_engine.oms.reconcile` at each call, so a test
//! that patches that module picks it up.

use pyo3::prelude::*;
use pyo3::types::{PyAny, PyDict};

use crate::runtime::intraday_flow;

type Value<'py> = Bound<'py, PyAny>;

#[pyclass(subclass, module = "trade_engine_rs")]
pub struct IntradayServiceBase {
    ledger: Py<PyAny>,
    clock: Py<PyAny>,
    calendar: Py<PyAny>,
    config: Py<PyAny>,
    heartbeat_path: Py<PyAny>,
    gate: Py<PyAny>,
    router: Py<PyAny>,
}

#[pymethods]
impl IntradayServiceBase {
    #[new]
    #[pyo3(signature = (ledger, clock, calendar, config, *, heartbeat_path=None))]
    fn new(
        py: Python<'_>,
        ledger: Bound<'_, PyAny>,
        clock: Bound<'_, PyAny>,
        calendar: Bound<'_, PyAny>,
        config: Bound<'_, PyAny>,
        heartbeat_path: Option<Bound<'_, PyAny>>,
    ) -> PyResult<Self> {
        intraday_flow::intraday_require_native(ledger.clone(), calendar.clone())?;

        let service = py.import("trade_engine.intraday.service")?;
        let gate = service
            .getattr("_EntryGate")?
            .call1((config.getattr("strategy")?,))?;

        let account_id = config.getattr("account_id")?;
        let broker = config.getattr("broker")?;
        let brokers = PyDict::new(py);
        brokers.set_item(&account_id, &broker)?;
        let strategies = PyDict::new(py);
        strategies.set_item(&account_id, &gate)?;

        let option_risk_engine = config.getattr("option_risk_engine")?;
        let option_risk_engines = PyDict::new(py);
        if !option_risk_engine.is_none() {
            option_risk_engines.set_item(&account_id, &option_risk_engine)?;
        }
        let journal_account = config.getattr("journal_account")?;
        let journal_accounts = PyDict::new(py);
        if !journal_account.is_none() {
            journal_accounts.set_item(&account_id, &journal_account)?;
        }

        let kwargs = PyDict::new(py);
        kwargs.set_item("brokers", &brokers)?;
        kwargs.set_item("strategies", &strategies)?;
        kwargs.set_item("option_risk_engines", &option_risk_engines)?;
        kwargs.set_item("journal_accounts", &journal_accounts)?;
        let router = py
            .import("trade_engine.eod.options_routing")?
            .getattr("OptionRouter")?
            .call((&ledger, &clock), Some(&kwargs))?;

        let heartbeat_path = match heartbeat_path {
            None => py.None().into_bound(py).unbind(),
            Some(path) => py.import("pathlib")?.getattr("Path")?.call1((path,))?.unbind(),
        };

        Ok(IntradayServiceBase {
            ledger: ledger.unbind(),
            clock: clock.unbind(),
            calendar: calendar.unbind(),
            config: config.unbind(),
            heartbeat_path,
            gate: gate.unbind(),
            router: router.unbind(),
        })
    }

    #[pyo3(signature = (session, *, start_at=None, stop_at=None))]
    fn run<'py>(
        slf: Bound<'py, Self>,
        session: Bound<'py, PyAny>,
        start_at: Option<Bound<'py, PyAny>>,
        stop_at: Option<Bound<'py, PyAny>>,
    ) -> PyResult<Value<'py>> {
        let py = slf.py();
        let none = py.None().into_bound(py);
        let account_id = slf.getattr("_config")?.getattr("account_id")?;
        let orders = slf
            .getattr("_router")?
            .call_method1("manager", (&account_id,))?
            .getattr("orders")?;
        let resync = py
            .import("trade_engine.oms.reconcile")?
            .getattr("resync_open_brackets")?;
        resync.call1((slf.getattr("_ledger")?, orders, account_id))?;
        intraday_flow::intraday_run(
            slf.into_any(),
            session,
            start_at.unwrap_or_else(|| none.clone()),
            stop_at.unwrap_or(none),
        )
    }

    fn _tick(slf: Bound<'_, Self>, session: Value<'_>, state: Value<'_>) -> PyResult<()> {
        intraday_flow::intraday_tick(slf.into_any(), session, state)
    }

    fn _flatten(slf: Bound<'_, Self>, session: Value<'_>, code: &str, reason: &str) -> PyResult<i64> {
        intraday_flow::intraday_flatten(slf.into_any(), session, code, reason)
    }

    fn _close_mark<'py>(
        slf: Bound<'py, Self>,
        session: Value<'py>,
        state: Value<'py>,
    ) -> PyResult<Value<'py>> {
        intraday_flow::intraday_close(slf.into_any(), session, state)
    }

    #[pyo3(signature = (session, state, note, *, alert=false, exited=false))]
    fn _write_heartbeat(
        slf: Bound<'_, Self>,
        session: Value<'_>,
        state: Value<'_>,
        note: &str,
        alert: bool,
        exited: bool,
    ) -> PyResult<()> {
        intraday_flow::intraday_heartbeat_write(slf.into_any(), session, state, note, alert, exited)
    }

    fn _read_heartbeat<'py>(slf: Bound<'py, Self>) -> PyResult<Value<'py>> {
        intraday_flow::intraday_heartbeat_read(slf.into_any())
    }

    fn _pause(slf: Bound<'_, Self>) -> PyResult<()> {
        let tick_seconds = slf.getattr("_config")?.getattr("tick_seconds")?;
        slf.getattr("_clock")?.call_method1("sleep", (tick_seconds,))?;
        Ok(())
    }

    #[getter(_ledger)]
    fn get_ledger(&self, py: Python<'_>) -> Py<PyAny> {
        self.ledger.clone_ref(py)
    }
    #[getter(_clock)]
    fn get_clock(&self, py: Python<'_>) -> Py<PyAny> {
        self.clock.clone_ref(py)
    }
    #[getter(_calendar)]
    fn get_calendar(&self, py: Python<'_>) -> Py<PyAny> {
        self.calendar.clone_ref(py)
    }
    #[getter(_config)]
    fn get_config(&self, py: Python<'_>) -> Py<PyAny> {
        self.config.clone_ref(py)
    }
    #[setter(_config)]
    fn set_config(&mut self, value: Py<PyAny>) {
        self.config = value;
    }
    #[getter(_heartbeat_path)]
    fn get_heartbeat_path(&self, py: Python<'_>) -> Py<PyAny> {
        self.heartbeat_path.clone_ref(py)
    }
    #[setter(_heartbeat_path)]
    fn set_heartbeat_path(&mut self, value: Py<PyAny>) {
        self.heartbeat_path = value;
    }
    #[getter(_gate)]
    fn get_gate(&self, py: Python<'_>) -> Py<PyAny> {
        self.gate.clone_ref(py)
    }
    #[getter(_router)]
    fn get_router(&self, py: Python<'_>) -> Py<PyAny> {
        self.router.clone_ref(py)
    }
}

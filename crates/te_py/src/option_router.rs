//! W7-CORE-C10: `OptionRouter`'s method bodies, moved into Rust (`OptionRouterBase`).
//!
//! `OptionRouter` sequences one options account's snapshot pass: it caches the account's
//! `OptionOrderManager`, builds the strategy's `OptionContext`, and matches and manages each
//! chain snapshot. The heavy rules are already native — `rs.options_manage`, `rs.options_apply`
//! and `rs.options_enter` (in `options_flow.rs`) drive `te_core::runtime::eod_flow`, calling
//! back into this object through Python attribute lookup (`call_method1`), so a test that
//! subclasses or monkeypatches `manager`/`match_snapshot`/`context`/`apply`/`enter_option` still
//! reaches the override. The option OMS (`OptionOrderManager`), the chain source and the sinks
//! are Python collaborators, created and called by attribute lookup; `open_structures` is looked
//! up on `trade_engine.oms.options` at call time (see `options_flow.rs`).

use pyo3::prelude::*;
use pyo3::types::PyDict;
use te_core::runtime::routing;

/// `OptionRouter`'s body, as a `#[pyclass(subclass)]` the Python module subclasses. Every
/// collaborator stays a Python object; the field getters keep the `_ledger`, `_clock`, `_brokers`,
/// `_strategies`, `_option_risk_engines`, `_journal_accounts` and `_option_managers` attributes the
/// native hosts read through `getattr` (see `options_flow.rs`).
#[pyclass(subclass, module = "trade_engine_rs")]
pub struct OptionRouterBase {
    #[pyo3(get)]
    _ledger: Py<PyAny>,
    #[pyo3(get)]
    _clock: Py<PyAny>,
    #[pyo3(get)]
    _brokers: Py<PyAny>,
    #[pyo3(get)]
    _strategies: Py<PyAny>,
    #[pyo3(get)]
    _option_risk_engines: Py<PyAny>,
    #[pyo3(get)]
    _journal_accounts: Py<PyAny>,
    #[pyo3(get)]
    _option_managers: Py<PyDict>,
}

#[pymethods]
impl OptionRouterBase {
    #[new]
    #[pyo3(signature = (ledger, clock, *, brokers, strategies, option_risk_engines, journal_accounts=None))]
    fn new(
        py: Python<'_>,
        ledger: Py<PyAny>,
        clock: Py<PyAny>,
        brokers: Py<PyAny>,
        strategies: Py<PyAny>,
        option_risk_engines: Py<PyAny>,
        journal_accounts: Option<Py<PyAny>>,
    ) -> PyResult<Self> {
        let journal_accounts = match journal_accounts {
            Some(accounts) => accounts,
            None => PyDict::new(py).unbind().into_any(),
        };
        Ok(Self {
            _ledger: ledger,
            _clock: clock,
            _brokers: brokers,
            _strategies: strategies,
            _option_risk_engines: option_risk_engines,
            _journal_accounts: journal_accounts,
            _option_managers: PyDict::new(py).unbind(),
        })
    }

    // -- plumbing ----------------------------------------------------------------

    fn manager(&self, py: Python<'_>, account_id: &str) -> PyResult<Py<PyAny>> {
        let managers = self._option_managers.bind(py);
        if let Some(existing) = managers.get_item(account_id)? {
            return Ok(existing.unbind());
        }
        let cls = py.import("trade_engine.oms.options")?.getattr("OptionOrderManager")?;
        let broker = self._brokers.bind(py).get_item(account_id)?;
        let manager = cls.call1((broker, self._clock.bind(py), self._ledger.bind(py)))?;
        managers.set_item(account_id, &manager)?;
        Ok(manager.unbind())
    }

    #[pyo3(signature = (account_id, session, now, structures, snapshot=None, snapshots=None, phase=None))]
    fn context(
        &self,
        py: Python<'_>,
        account_id: &str,
        session: Bound<'_, PyAny>,
        now: Bound<'_, PyAny>,
        structures: Bound<'_, PyAny>,
        snapshot: Option<Bound<'_, PyAny>>,
        snapshots: Option<Bound<'_, PyAny>>,
        phase: Option<String>,
    ) -> PyResult<Py<PyAny>> {
        // The phase `context` reports (`routing::phase`): a named phase, else snapshot or close.
        let has_snapshot = snapshot.is_some();
        let phase = routing::phase(phase.as_deref().unwrap_or(""), has_snapshot);
        let state = self._ledger.bind(py).call_method1("state", (account_id,))?;
        // `dict(snapshots or {})`: a shallow copy, or an empty dict.
        let snapshots_arg = match snapshots {
            Some(value) => py.import("builtins")?.call_method1("dict", (value,))?,
            None => PyDict::new(py).into_any(),
        };
        let kwargs = PyDict::new(py);
        kwargs.set_item("session", session)?;
        kwargs.set_item("account_id", account_id)?;
        kwargs.set_item("phase", phase)?;
        kwargs.set_item("now", now)?;
        kwargs.set_item("state", state)?;
        kwargs.set_item("structures", structures)?;
        kwargs.set_item("snapshot", snapshot)?;
        kwargs.set_item("snapshots", snapshots_arg)?;
        let cls = py.import("trade_engine.eod.options")?.getattr("OptionContext")?;
        Ok(cls.call((), Some(&kwargs))?.unbind())
    }

    // -- the snapshot pass -------------------------------------------------------

    fn match_snapshot(
        &self,
        py: Python<'_>,
        account_id: &str,
        snapshot: Bound<'_, PyAny>,
        cause: &str,
        since: Bound<'_, PyAny>,
    ) -> PyResult<Py<PyAny>> {
        let broker = self._brokers.bind(py).get_item(account_id)?;
        broker.call_method1("process_snapshot", (&snapshot,))?;
        let reconcile = py.import("trade_engine.oms.reconcile")?;
        let reconcile_after = reconcile.getattr("reconcile_after")?;
        let reconcile_error = reconcile.getattr("ReconcileError")?;
        let min_time = reconcile.getattr("MIN_TIME")?;
        let since_arg = if since.is_none() { min_time } else { since };
        let manager = self.manager(py, account_id)?;
        let orders = manager.bind(py).getattr("orders")?;
        let journal = self._journal_accounts.bind(py).call_method1("get", (account_id,))?;
        let kwargs = PyDict::new(py);
        kwargs.set_item("journal_account", journal)?;
        let recorded = match reconcile_after.call(
            (self._ledger.bind(py), self._clock.bind(py), &broker, orders, account_id, since_arg),
            Some(&kwargs),
        ) {
            Ok(recorded) => recorded,
            Err(err) if err.is_instance(py, &reconcile_error) => {
                // `_fail(str(err))`: the runners' refusal type (EodRunnerError).
                let message: String = err.value(py).str()?.extract()?;
                let raised = py
                    .import("trade_engine.eod.runner")?
                    .getattr("EodRunnerError")?
                    .call1((message,))?;
                return Err(PyErr::from_value(raised));
            }
            Err(err) => return Err(err),
        };
        let underlying: String = snapshot.getattr("underlying")?.extract()?;
        manager
            .bind(py)
            .call_method1("sync", (account_id, format!("{cause}:{underlying}")))?;
        Ok(recorded.unbind())
    }

    fn manage_at_snapshot<'py>(
        slf: PyRef<'py, Self>,
        account_id: &str,
        session: Bound<'py, PyAny>,
        snapshot: Bound<'py, PyAny>,
        snapshots: Bound<'py, PyAny>,
        cause: &str,
    ) -> PyResult<Py<PyAny>> {
        let py = slf.py();
        let owner = slf.into_pyobject(py).unwrap().into_any();
        py.import("trade_engine_rs")?
            .call_method1("options_manage", (owner, account_id, session, snapshot, snapshots, cause))
            .map(Bound::unbind)
    }

    fn apply<'py>(
        slf: PyRef<'py, Self>,
        account_id: &str,
        session: Bound<'py, PyAny>,
        actions: Bound<'py, PyAny>,
        snapshot: Bound<'py, PyAny>,
        snapshots: Bound<'py, PyAny>,
        cause: &str,
    ) -> PyResult<Py<PyAny>> {
        let py = slf.py();
        let owner = slf.into_pyobject(py).unwrap().into_any();
        py.import("trade_engine_rs")?
            .call_method1("options_apply", (owner, account_id, session, actions, snapshot, snapshots, cause))
            .map(Bound::unbind)
    }

    fn enter_option<'py>(
        slf: PyRef<'py, Self>,
        account_id: &str,
        session: Bound<'py, PyAny>,
        intent: Bound<'py, PyAny>,
        snapshot: Bound<'py, PyAny>,
        snapshots: Bound<'py, PyAny>,
    ) -> PyResult<i64> {
        let py = slf.py();
        let owner = slf.into_pyobject(py).unwrap().into_any();
        py.import("trade_engine_rs")?
            .call_method1("options_enter", (owner, account_id, session, intent, snapshot, snapshots))?
            .extract()
    }
}

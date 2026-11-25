//! W7-CORE-C10: `LifecyclePass`'s method bodies, moved into Rust (`LifecyclePassBase`).
//!
//! `LifecyclePass` settles a session's expiring option positions and early assignments after the
//! close. The pass itself is already native (`trade_engine_rs.lifecycle_run`, in
//! `lifecycle_pass.rs`), which runs `te_core::runtime::lifecycle_glue::run` over the store while
//! the settlement, dividend and quote sources stay Python plugins it calls back into. `run` here
//! keeps today's shape: it hands `self` to `lifecycle_run` (reached through the module, since the
//! pass lives in a sibling module this ticket must not edit) and wraps the returned events in the
//! Python `LifecycleResult`. The exception mapping — `LifecycleError`, `StaleDataError`, and the
//! fold's refusals — stays inside `lifecycle_run`, so types and messages are unchanged.

use pyo3::prelude::*;

/// `LifecyclePass`'s body, as a `#[pyclass(subclass)]` the Python module subclasses. The
/// `_ledger`, `_clock`, `_calendar`, `_settlements`, `_dividends` and `_quotes` getters keep the
/// attributes `lifecycle_run` reads through `getattr`.
#[pyclass(subclass, module = "trade_engine_rs")]
pub struct LifecyclePassBase {
    #[pyo3(get)]
    _ledger: Py<PyAny>,
    #[pyo3(get)]
    _clock: Py<PyAny>,
    #[pyo3(get)]
    _calendar: Py<PyAny>,
    #[pyo3(get)]
    _settlements: Py<PyAny>,
    #[pyo3(get)]
    _dividends: Py<PyAny>,
    #[pyo3(get)]
    _quotes: Py<PyAny>,
}

#[pymethods]
impl LifecyclePassBase {
    #[new]
    #[pyo3(signature = (ledger, clock, calendar, settlements, *, dividends=None, quotes=None))]
    fn new(
        py: Python<'_>,
        ledger: Py<PyAny>,
        clock: Py<PyAny>,
        calendar: Py<PyAny>,
        settlements: Py<PyAny>,
        dividends: Option<Py<PyAny>>,
        quotes: Option<Py<PyAny>>,
    ) -> PyResult<Self> {
        Ok(Self {
            _ledger: ledger,
            _clock: clock,
            _calendar: calendar,
            _settlements: settlements,
            _dividends: dividends.unwrap_or_else(|| py.None()),
            _quotes: quotes.unwrap_or_else(|| py.None()),
        })
    }

    #[pyo3(signature = (session, accounts=None))]
    fn run<'py>(
        slf: PyRef<'py, Self>,
        session: Bound<'py, PyAny>,
        accounts: Option<Bound<'py, PyAny>>,
    ) -> PyResult<Py<PyAny>> {
        let py = slf.py();
        let owner = slf.into_pyobject(py).unwrap().into_any();
        let events = py
            .import("trade_engine_rs")?
            .call_method1("lifecycle_run", (owner, session.clone(), accounts))?;
        let events = py.import("builtins")?.call_method1("tuple", (events,))?;
        let result = py
            .import("trade_engine.lifecycle.after_close")?
            .getattr("LifecycleResult")?
            .call1((session, events))?;
        Ok(result.unbind())
    }
}

//! The native one-shot EOD actor client: durable admission around the
//! existing runner flow, with the CLI staying a thin resolver of plugins.
use pyo3::{
    exceptions::{PyRuntimeError, PyTypeError},
    prelude::*,
};
use std::collections::BTreeSet;
use te_host::actor::{run_once, JobHost, Outcome};
use te_host::jobs::{Error as JournalError, Failure, Journal, Record};

fn journal_error(_py: Python<'_>, error: JournalError) -> PyErr {
    PyRuntimeError::new_err(format!("{}: {}", error.kind, error.message))
}

/// The one-shot host runs the existing EodRunner flow inside the actor's
/// durable admission; no second writer, no re-derived session.
struct OnceHost {
    runner: Py<PyAny>,
    session: Py<PyAny>,
}

impl JobHost for OnceHost {
    fn run(&mut self, record: &Record) -> Outcome {
        Python::with_gil(|py| {
            let outcome = self
                .runner
                .bind(py)
                .call_method1("run", (self.session.bind(py),));
            match outcome {
                Ok(result) => {
                    // Preserve each account's exact counters for the CLI lines.
                    let accounts = (|| -> PyResult<Vec<serde_json::Value>> {
                        let mut lines = Vec::new();
                        for account in result.getattr("accounts")?.try_iter()? {
                            let account = account?;
                            lines.push(serde_json::json!({
                                "account_id": account.getattr("account_id")?.extract::<String>()?,
                                "bars_processed": account.getattr("bars_processed")?.extract::<i64>()?,
                                "fills_recorded": account.getattr("fills_recorded")?.extract::<i64>()?,
                                "marks_appended": account.getattr("marks_appended")?.extract::<i64>()?,
                                "orders_submitted": account.getattr("orders_submitted")?.extract::<i64>()?,
                            }));
                        }
                        Ok(lines)
                    })();
                    match accounts {
                        Ok(accounts) => Outcome::Completed(serde_json::json!({
                            "request_id": record.request_id,
                            "job": "daily",
                            "accounts": accounts,
                        })),
                        Err(error) => outcome_error(py, error),
                    }
                }
                Err(error) => outcome_error(py, error),
            }
        })
    }
}

/// Classify a runner exception: EodRunnerError family is the runner's refusal
/// surface with its exact message preserved; any other exception is a host
/// failure with its type recorded.
fn outcome_error(py: Python<'_>, error: PyErr) -> Outcome {
    let kind = error
        .get_type(py)
        .name()
        .map_or("RuntimeError".into(), |n| n.to_string());
    let message = error
        .value(py)
        .str()
        .map(|s| s.to_string())
        .unwrap_or_default();
    let refusal = py
        .import("trade_engine.eod.runner")
        .and_then(|module| module.getattr("EodRunnerError"))
        .ok();
    let is_refusal = match &refusal {
        Some(base) => error.is_instance(py, base),
        None => false,
    };
    let failure = Failure {
        r#type: kind,
        message,
    };
    if is_refusal {
        Outcome::Refused(failure)
    } else {
        Outcome::Failed(failure)
    }
}

/// The CLI one-shot actor: recover foreign incomplete records, admit durably,
/// run once, record the terminal state. The runner is the existing Python
/// `EodRunner`; the ledger is its open owner.
#[pyfunction]
#[pyo3(signature = (ledger, runner, session, request_id))]
fn eod_once(
    py: Python<'_>,
    ledger: &Bound<'_, PyAny>,
    runner: &Bound<'_, PyAny>,
    session: &Bound<'_, PyAny>,
    request_id: String,
) -> PyResult<Py<PyAny>> {
    let native = ledger.getattr("_native")?;
    if native.is_none() {
        return Err(PyRuntimeError::new_err(
            "the ledger is not open; a one-shot actor requires the owner's connection",
        ));
    }
    let held = native.getattr("held")?.extract::<bool>()?;
    if !held {
        return Err(PyRuntimeError::new_err(
            "the ledger does not hold its writer; a one-shot actor requires the owner",
        ));
    }
    if !runner.getattr("run")?.is_callable() {
        return Err(PyTypeError::new_err("runner.run must be callable"));
    }
    let store: PyRef<'_, crate::store::LedgerStore> = native.extract()?;
    let store = store.store_connection();
    let session_text = session.call_method0("isoformat")?.extract::<String>()?;
    let journal = Journal::new(
        store,
        "eod".into(),
        "cli".into(),
        "once".into(),
        BTreeSet::from(["daily".into()]),
        64,
    )
    .map_err(|e| journal_error(py, e))?;
    let payload = serde_json::json!({
        "version": 1,
        "request_id": request_id,
        "generation": "cli",
        "role": "eod",
        "job": "daily",
        "session": session_text,
        "options": {},
    });
    let mut host = OnceHost {
        runner: runner.clone().unbind(),
        session: session.clone().unbind(),
    };
    let (record, _inserted) = run_once(&journal, &mut host, payload)
        .map_err(|e| journal_error(py, e))?;
    // The record's wire JSON is returned via the stdlib decoder; the actor
    // never fabricates shape.
    let text = serde_json::to_string(&record).map_err(|e| PyRuntimeError::new_err(e.to_string()))?;
    let value = py.import("json")?.getattr("loads")?.call1((text,))?;
    value.extract()
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(eod_once, m)?)?;
    Ok(())
}
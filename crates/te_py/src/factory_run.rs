//! Trusted owner-side job execution: the owner passes its ONE attached
//! ledger handle to a configured client entry point, which assembles the
//! runner over it and runs the session. The entry point is configured, not
//! submitted; the request carries data, never code.
use pyo3::{exceptions::PyRuntimeError, prelude::*};

/// Run one configured job in the owner process.
///
/// `options_json` is the admitted record's options (data only). The
/// `entry_module`/`entry` name the OWNER-CONFIGURED client entry point —
/// they are passed by the runtime from its validated configuration, never
/// taken from the request. The entry point receives `(ledger, session,
/// options)` where `ledger` is the owner's attached Ledger (I4: the one
/// writer) and must return the per-account run summary; the result crosses
/// back as its JSON text, exactly as produced.
#[pyfunction]
#[pyo3(signature = (options_json, entry_module, entry))]
fn owner_job_run(
    py: Python<'_>,
    options_json: String,
    entry_module: String,
    entry: String,
) -> PyResult<Py<PyAny>> {
    let options: serde_json::Value = serde_json::from_str(&options_json)
        .map_err(|e| PyRuntimeError::new_err(format!("RuntimeRequestError: {e}")))?;
    for (label, name) in [("entry_module", &entry_module), ("entry", &entry)] {
        // A module path is dotted plain identifiers; the entry member is one
        // plain identifier. Nothing else is accepted.
        let segment = |value: &str| {
            !value.is_empty()
                && value.len() <= 128
                && value
                    .bytes()
                    .enumerate()
                    .all(|(i, c)| c == b'_' || c.is_ascii_alphabetic() || (i > 0 && c.is_ascii_digit()))
        };
        let valid = match label {
            "entry_module" => !name.is_empty() && name.len() <= 512 && name.split('.').all(segment),
            _ => segment(name),
        };
        if !valid {
            return Err(PyRuntimeError::new_err(format!(
                "RuntimeRequestError: {label} must be a dotted plain identifier"
            )));
        }
    }
    let get = |key: &str| -> PyResult<String> {
        options[key]
            .as_str()
            .map(str::to_owned)
            .ok_or_else(|| {
                PyRuntimeError::new_err(format!(
                    "RuntimeRequestError: the job options lack '{key}'"
                ))
            })
    };
    let ledger = py
        .import("trade_engine_rs")?
        .getattr("owner_ledger")?
        .call0()?;
    let session = py
        .import("datetime")?
        .getattr("date")?
        .call_method1("fromisoformat", (get("session")?,))?;
    let options_mapping: pyo3::Bound<'_, pyo3::types::PyAny> = py
        .import("json")?
        .getattr("loads")?
        .call1((
            serde_json::to_string(&options.get("options").cloned().unwrap_or_default())
                .map_err(|e| PyRuntimeError::new_err(e.to_string()))?,
        ))?;
    let module = py.import(&entry_module)?;
    if module.is_none() {
        return Err(PyRuntimeError::new_err(format!(
            "RuntimeRequestError: the entry module '{entry_module}' is not configured"
        )));
    }
    let entry_point = module.getattr(&entry)?;
    if !entry_point.is_callable() {
        return Err(PyRuntimeError::new_err(format!(
            "RuntimeRequestError: the entry point '{entry_module}.{entry}' is not callable"
        )));
    }
    let result = entry_point.call((ledger, session, options_mapping), None)?;
    // The result crosses as its JSON text: the owner records exactly what the
    // entry point produced, never a re-shaped copy.
    let text = py
        .import("json")?
        .getattr("dumps")?
        .call1((result,))?
        .extract::<String>()?;
    Ok(text.into_py(py))
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(owner_job_run, m)?)?;
    Ok(())
}
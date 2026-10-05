use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;

#[path = "lifecycle_sinks.rs"]
mod lifecycle_sinks;
#[path = "eod_flow.rs"]
mod eod_flow;
#[path = "options_flow.rs"]
mod options_flow;
#[path = "equity_flow.rs"]
mod equity_flow;

pub(super) fn flow_refusal(error: te_core::ledger::model::LErr) -> PyErr {
    Python::with_gil(|py| {
        match py.import("trade_engine.sim._rs")
            .and_then(|module| module.call_method1("refusal", (error.kind, error.msg))) {
            Ok(value) => PyErr::from_value(value),
            Err(error) => error,
        }
    })
}
#[pyfunction]
fn runtime_decide(op: &str, text: Vec<String>, numbers: Vec<i64>, flags: Vec<bool>, floats: Vec<f64>) -> PyResult<(Vec<String>,Vec<i64>,Vec<bool>)> {
    let p = te_core::runtime::decide(op,&text,&numbers,&flags,&floats)
        .map_err(|e| PyValueError::new_err((e.kind,e.msg)))?;
    Ok((p.text,p.numbers,p.flags))
}
pub fn register(m: &Bound<'_,PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(runtime_decide,m)?)?;
    lifecycle_sinks::register(m)?;
    eod_flow::register(m)?;
    options_flow::register(m)?;
    equity_flow::register(m)?;
    Ok(())
}

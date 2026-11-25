use pyo3::prelude::*;

#[path = "lifecycle_sinks.rs"]
mod lifecycle_sinks;
#[path = "eod_flow.rs"]
mod eod_flow;
#[path = "options_flow.rs"]
mod options_flow;
#[path = "equity_flow.rs"]
mod equity_flow;
#[path = "intraday_flow.rs"]
pub(crate) mod intraday_flow;
#[path = "lifecycle_pass.rs"]
mod lifecycle_pass;

pub(super) fn flow_refusal(error: te_core::ledger::model::LErr) -> PyErr {
    Python::with_gil(|py| {
        match py.import("trade_engine.sim._rs")
            .and_then(|module| module.call_method1("refusal", (error.kind, error.msg))) {
            Ok(value) => PyErr::from_value(value),
            Err(error) => error,
        }
    })
}
pub fn register(m: &Bound<'_,PyModule>) -> PyResult<()> {
    lifecycle_sinks::register(m)?;
    eod_flow::register(m)?;
    options_flow::register(m)?;
    equity_flow::register(m)?;
    intraday_flow::register(m)?;
    lifecycle_pass::register(m)?;
    Ok(())
}

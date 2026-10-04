//! The ONE door for P5 (`tos_paper`): `tos_paper_decide(op, json) -> json`. Refusals cross
//! as `ValueError((kind, message))` for `sim/_rs.py` to map.
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;

#[pyfunction]
fn tos_paper_decide(op: &str, json: &str) -> PyResult<String> {
    te_core::tos_paper::decide(op, json).map_err(|e| PyValueError::new_err((e.kind, e.msg)))
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(tos_paper_decide, m)?)?;
    Ok(())
}

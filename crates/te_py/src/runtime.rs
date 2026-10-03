use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;

#[pyfunction]
fn runtime_decide(op: &str, text: Vec<String>, numbers: Vec<i64>, flags: Vec<bool>, floats: Vec<f64>) -> PyResult<(Vec<String>,Vec<i64>,Vec<bool>)> {
    let p = te_core::runtime::decide(op,&text,&numbers,&flags,&floats)
        .map_err(|e| PyValueError::new_err((e.kind,e.msg)))?;
    Ok((p.text,p.numbers,p.flags))
}
pub fn register(m: &Bound<'_,PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(runtime_decide,m)?)?;
    Ok(())
}

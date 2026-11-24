//! W6-F0b-3b: Python objects across the PyO3 boundary, not JSON text.
//!
//! `TosBroker` and `PyBrokerHost` used to pass `json.dumps(value, default=str)` text one
//! way and `json.loads` the other. [`plain_of`] turns a Python value into the `Json` tree
//! `json.loads(json.dumps(value, default=str))` would give; [`plain_py`] turns a `Json`
//! tree back into the Python object `json.loads` builds. Together they replace the
//! `json.dumps`/`json.loads` round trip around every `TosBroker` / `PyBrokerHost` call,
//! with the exact same semantics:
//!
//! - `None` -> `Null`; `bool` (checked before `int`, since `bool` is a subclass) -> `Bool`;
//!   `int` -> `Int`, or `BigInt` via `str()` past `i128`.
//! - `float` -> `Float` of Python `repr(value)` (never Rust `f64::to_string`: `json.dumps`
//!   writes `float.__repr__`).
//! - `str` and str subclasses (`StrEnum`) -> `Str`.
//! - `list` and `tuple` -> `Arr`; `dict` -> `Obj` in insertion order, keys converted the
//!   way `json.dumps` converts them (str as-is, `True`/`False`/`None` -> the JSON words,
//!   `int` -> `str()`, `float` -> `repr()`).
//! - anything else -> `Str(str(value))`, which is `default=str`.

use pyo3::prelude::*;
use pyo3::types::{PyBool, PyDict, PyFloat, PyInt, PyList, PyString, PyTuple};
use pyo3::IntoPyObjectExt;
use te_core::ledger::json::Json;

/// A dict key the way `json.dumps` spells it (str as-is, bool/None as the JSON words,
/// int via `str()`, float via `repr()`).
fn key_of(value: &Bound<'_, PyAny>) -> PyResult<String> {
    if value.is_instance_of::<PyBool>() {
        let b: bool = value.extract()?;
        return Ok(if b { "true" } else { "false" }.to_string());
    }
    if value.is_none() {
        return Ok("null".to_string());
    }
    if value.is_instance_of::<PyString>() {
        return value.extract();
    }
    if value.is_instance_of::<PyInt>() {
        return value.str()?.extract();
    }
    if value.is_instance_of::<PyFloat>() {
        return value.repr()?.extract();
    }
    // `json.dumps` raises `TypeError` on any other key; a transport row never carries one.
    value.str()?.extract()
}

/// A Python object as the `Json` tree `json.loads(json.dumps(value, default=str))` would give.
pub(crate) fn plain_of(value: &Bound<'_, PyAny>) -> PyResult<Json> {
    if value.is_none() {
        return Ok(Json::Null);
    }
    if value.is_instance_of::<PyBool>() {
        return Ok(Json::Bool(value.extract()?));
    }
    if value.is_instance_of::<PyInt>() {
        if let Ok(n) = value.extract::<i128>() {
            return Ok(Json::Int(n));
        }
        return Ok(Json::BigInt(value.str()?.extract()?));
    }
    if value.is_instance_of::<PyFloat>() {
        return Ok(Json::Float(value.repr()?.extract()?));
    }
    if value.is_instance_of::<PyString>() {
        return Ok(Json::Str(value.extract()?));
    }
    if value.is_instance_of::<PyTuple>() {
        let tuple = value.downcast::<PyTuple>()?;
        let mut items = Vec::new();
        for item in tuple.iter() {
            items.push(plain_of(&item)?);
        }
        return Ok(Json::Arr(items));
    }
    if value.is_instance_of::<PyList>() {
        let list = value.downcast::<PyList>()?;
        let mut items = Vec::new();
        for item in list.iter() {
            items.push(plain_of(&item)?);
        }
        return Ok(Json::Arr(items));
    }
    if value.is_instance_of::<PyDict>() {
        let dict = value.downcast::<PyDict>()?;
        let mut pairs = Vec::new();
        for (k, v) in dict.iter() {
            pairs.push((key_of(&k)?, plain_of(&v)?));
        }
        return Ok(Json::Obj(pairs));
    }
    // `default=str`.
    Ok(Json::Str(value.str()?.extract()?))
}

/// A `Json` tree as the Python object `json.loads` builds (`float(text)` keeps `NaN`/`Infinity`).
pub(crate) fn plain_py<'py>(py: Python<'py>, json: &Json) -> PyResult<Bound<'py, PyAny>> {
    let builtins = py.import("builtins")?;
    match json {
        Json::Null => Ok(py.None().into_bound(py)),
        Json::Bool(b) => Ok((*b).into_bound_py_any(py)?),
        Json::Int(n) => Ok((*n).into_bound_py_any(py)?),
        Json::BigInt(t) => Ok(builtins.getattr("int")?.call1((t.as_str(),))?),
        Json::Float(t) => Ok(builtins.getattr("float")?.call1((t.as_str(),))?),
        Json::Str(s) => Ok(s.as_str().into_bound_py_any(py)?),
        Json::Arr(items) => {
            let list = PyList::empty(py);
            for item in items {
                list.append(plain_py(py, item)?)?;
            }
            Ok(list.into_any())
        }
        Json::Obj(pairs) => {
            let dict = PyDict::new(py);
            for (k, v) in pairs {
                dict.set_item(k.as_str(), plain_py(py, v)?)?;
            }
            Ok(dict.into_any())
        }
    }
}

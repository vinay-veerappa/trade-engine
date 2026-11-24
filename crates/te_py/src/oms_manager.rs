//! W6-F2: the typed pyfunctions behind `oms/manager.py`'s pure decision shims
//! (`te_core::oms::manager`). Each is the typed form of one `decision` arm, so a
//! refusal crosses as `ValueError((kind, message))` exactly as `oms_manager_decide` did.
//!
//! Decimals arrive as Python `Decimal` (parsed from `str`, with the door's "invalid
//! decimal" refusal), an `Order` as the Python carrier (`store_ledger::order_of`), and
//! bools and strs as themselves. Quantities come back as Python `Decimal`s built from
//! the same canonical text the door produced. `fingerprint` and `bracket_fingerprint`
//! take the canonical JSON text Python builds with `codec.text(_wire(x))` and hash it
//! byte-identically to the door (`dumps` sorts keys); the Order is never re-encoded here.

use pyo3::prelude::*;
use pyo3::types::PyList;
use te_core::ledger::bridge;
use te_core::ledger::json::{dumps, Json};
use te_core::ledger::model::{LErr, R as LR};
use te_core::ledger::ops::{le, lt};
use te_core::money::Money;
use te_core::oms::manager as mm;
use te_core::oms::options::sha256_hex;

use crate::sim::refuse;
use crate::store_ledger::{self as sl};

/// A Python `Decimal` as the door's `dec` read it: `str(value)` parsed, refused with the
/// door's exact "invalid decimal" message (not `sim::dec`'s "not a Decimal").
fn dec_py(value: &Bound<'_, PyAny>) -> PyResult<Money> {
    let s: String = value.str()?.extract()?;
    match Money::parse(&s) {
        Some(m) => Ok(m),
        None => Err(refuse(LErr { kind: "value", msg: "invalid decimal".into() })),
    }
}

/// A Python list/tuple of `Decimal`s, in order.
fn decs_py(value: &Bound<'_, PyAny>) -> PyResult<Vec<Money>> {
    value.try_iter()?.map(|item| dec_py(&item?)).collect()
}

/// The `field` helper the door used to read a named JSON member.
fn field<'a>(j: &'a Json, k: &str) -> LR<&'a Json> {
    j.get(k).ok_or_else(|| LErr { kind: "value", msg: format!("manager request missing {k}") })
}

/// The door's `text`: a JSON string value, else "manager expected string".
fn text(j: &Json) -> LR<&str> {
    match j {
        Json::Str(s) => Ok(s),
        _ => Err(LErr { kind: "value", msg: "manager expected string".into() }),
    }
}

/// The door's `dec`: a JSON string parsed as `Money`, else "invalid decimal".
fn dec_json(j: &Json) -> LR<Money> {
    Money::parse(text(j)?).ok_or_else(|| LErr { kind: "value", msg: "invalid decimal".into() })
}

/// The door's `d`: a named JSON string member parsed as `Money`.
fn d_json(j: &Json, k: &str) -> LR<Money> {
    dec_json(field(j, k)?)
}

/// A Python value that is `None` or a `Decimal`, as `Option<Money>`.
fn opt_dec(value: &Bound<'_, PyAny>) -> PyResult<Option<Money>> {
    sl::opt(value, dec_py)
}

fn money_list<'py>(py: Python<'py>, values: &[Money]) -> PyResult<Bound<'py, PyList>> {
    let items = values.iter().map(|m| sl::decimal_py(py, m)).collect::<PyResult<Vec<_>>>()?;
    PyList::new(py, items)
}

/// `_allocate_quantity`: largest-remainder allocation over the weights.
#[pyfunction]
fn oms_manager_allocate<'py>(
    quantity: &Bound<'py, PyAny>,
    weights: &Bound<'py, PyAny>,
    equity: bool,
) -> PyResult<Bound<'py, PyList>> {
    let py = quantity.py();
    let q = dec_py(quantity)?;
    let w = decs_py(weights)?;
    let out = mm::allocate(&q, &w, equity).map_err(refuse)?;
    money_list(py, &out)
}

/// `_split_quantity`: equal weights, each portion positive.
#[pyfunction]
fn oms_manager_split<'py>(
    quantity: &Bound<'py, PyAny>,
    weights: &Bound<'py, PyAny>,
    equity: bool,
) -> PyResult<Bound<'py, PyList>> {
    let py = quantity.py();
    let q = dec_py(quantity)?;
    let w = decs_py(weights)?;
    let out = mm::split(&q, &w, equity).map_err(refuse)?;
    money_list(py, &out)
}

/// `_fraction_quantities`: each target's fraction, the remainder a runner.
#[pyfunction]
fn oms_manager_fractions<'py>(
    quantity: &Bound<'py, PyAny>,
    weights: &Bound<'py, PyAny>,
    equity: bool,
) -> PyResult<Bound<'py, PyList>> {
    let py = quantity.py();
    let q = dec_py(quantity)?;
    let w = decs_py(weights)?;
    let out = mm::fractions(&q, &w, equity).map_err(refuse)?;
    money_list(py, &out)
}

/// `_fingerprint_order`: the encoded order's digest, hashed from Python's payload text.
#[pyfunction]
fn oms_manager_fingerprint(request: &str) -> PyResult<String> {
    let r = || -> LR<String> {
        let j = bridge::json_from_text(request)?;
        let payload = field(&j, "payload")?;
        Ok(sha256_hex(dumps(payload).as_bytes()))
    };
    r().map_err(refuse)
}

/// `_reduce_fingerprint`: the command fingerprint over its entry, fraction and reason.
#[pyfunction]
fn oms_manager_reduce_fingerprint(
    entry: &str,
    fraction: &Bound<'_, PyAny>,
    reason: &str,
) -> PyResult<String> {
    let fraction = dec_py(fraction)?;
    Ok(mm::reduce_fingerprint(entry, &fraction, reason))
}

/// `_bracket_fingerprint`: hashed from Python's wired intent text (never re-encoded here).
#[pyfunction]
fn oms_manager_bracket_fingerprint(request: &str) -> PyResult<String> {
    let r = || -> LR<String> {
        let j = bridge::json_from_text(request)?;
        let intent = field(&j, "intent")?;
        let quantity = d_json(&j, "quantity")?;
        mm::bracket_fingerprint(intent, &quantity)
    };
    r().map_err(refuse)
}

/// `_is_reduce`: whether `order_id` is a reduce order of `entry`.
#[pyfunction]
fn oms_manager_is_reduce(entry: &str, order_id: &str) -> bool {
    mm::is_reduce(entry, order_id)
}

/// `_replacement_terms`' first call: `None` on success. The guard order is the door's:
/// `filled` is read only when a quantity is present, and `equity` only when that quantity
/// is finite, positive and at least the filled amount.
#[pyfunction]
fn oms_manager_replace_quantity(
    quantity: &Bound<'_, PyAny>,
    filled: &Bound<'_, PyAny>,
    equity: bool,
) -> PyResult<()> {
    let q = opt_dec(quantity)?;
    let filled = if q.is_some() { dec_py(filled)? } else { Money::int(0) };
    let positive = match &q {
        Some(v) => v.is_finite() && !lt(v, &filled).map_err(refuse)? && !le(v, &Money::int(0)).map_err(refuse)?,
        None => false,
    };
    mm::replace_quantity(q.as_ref(), &filled, if positive { equity } else { false }).map_err(refuse)
}

/// `_replacement_terms`: the changed order's `(quantity, limit_price, stop_price, noop)`.
#[pyfunction]
fn oms_manager_replace_terms<'py>(
    order: &Bound<'py, PyAny>,
    quantity: &Bound<'py, PyAny>,
    limit: &Bound<'py, PyAny>,
    stop: &Bound<'py, PyAny>,
) -> PyResult<(
    Bound<'py, PyAny>,
    Option<Bound<'py, PyAny>>,
    Option<Bound<'py, PyAny>>,
    bool,
)> {
    let py = order.py();
    let o = sl::order_of(order)?;
    let q = opt_dec(quantity)?;
    let l = opt_dec(limit)?;
    let s = opt_dec(stop)?;
    let (updated, same) = mm::replace_terms(&o, q.as_ref(), l.as_ref(), s.as_ref());
    Ok((
        sl::decimal_py(py, &updated.quantity)?,
        updated.limit_price.as_ref().map(|m| sl::decimal_py(py, m)).transpose()?,
        updated.stop_price.as_ref().map(|m| sl::decimal_py(py, m)).transpose()?,
        same,
    ))
}

pub(crate) fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(oms_manager_allocate, m)?)?;
    m.add_function(wrap_pyfunction!(oms_manager_split, m)?)?;
    m.add_function(wrap_pyfunction!(oms_manager_fractions, m)?)?;
    m.add_function(wrap_pyfunction!(oms_manager_fingerprint, m)?)?;
    m.add_function(wrap_pyfunction!(oms_manager_reduce_fingerprint, m)?)?;
    m.add_function(wrap_pyfunction!(oms_manager_bracket_fingerprint, m)?)?;
    m.add_function(wrap_pyfunction!(oms_manager_is_reduce, m)?)?;
    m.add_function(wrap_pyfunction!(oms_manager_replace_quantity, m)?)?;
    m.add_function(wrap_pyfunction!(oms_manager_replace_terms, m)?)?;
    Ok(())
}

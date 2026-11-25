//! W5 P1: the RiskGuard pre-check's decision rules (`te_core::oms::precheck`) as
//! `src/trade_engine/oms/precheck.py` reaches them. Money crosses as its canonical text; a
//! refusal crosses as `ValueError((kind, message))`. The Python shim keeps the I/O.

use std::collections::HashMap;

use pyo3::prelude::*;
use pyo3::sync::GILOnceCell;
use pyo3::types::{PyDict, PyList, PyTuple};
use te_core::ledger::model::R as LR;
use te_core::money::Money;
use te_core::oms::precheck as pc;

use crate::sim::{dec, instrument, refuse, side as parse_side};

#[pyfunction]
fn precheck_split_quantity(q: &str, side: &str, position: &str) -> PyResult<(String, String)> {
    let run = || -> LR<(Money, Money)> { pc::split_quantity(&dec(q)?, parse_side(side)?, &dec(position)?) };
    let (reducing, opening) = run().map_err(refuse)?;
    Ok((reducing.to_string(), opening.to_string()))
}

type Indexed = Vec<(usize, String)>;

#[pyfunction]
fn precheck_split_allocations(
    side: &str,
    allocs: Vec<(String, String)>,
    positions: HashMap<String, String>,
) -> PyResult<(Indexed, Indexed, Vec<(String, String)>)> {
    let run = || -> LR<pc::AllocSplit> {
        let allocs = allocs.iter().map(|(account, q)| Ok((account.clone(), dec(q)?))).collect::<LR<Vec<_>>>()?;
        let positions = positions
            .iter()
            .map(|(account, p)| Ok((account.clone(), dec(p)?)))
            .collect::<LR<HashMap<_, _>>>()?;
        pc::split_allocations(parse_side(side)?, &allocs, &positions)
    };
    let split = run().map_err(refuse)?;
    let indexed = |rows: &[(usize, Money)]| rows.iter().map(|(i, m)| (*i, m.to_string())).collect();
    let openings = split.openings.iter().map(|(account, m)| (account.clone(), m.to_string())).collect();
    Ok((indexed(&split.reducing), indexed(&split.opening), openings))
}

/// The RiskGuard root for an instrument's text (`codec.text(codec.canon(instrument))`);
/// `None` when it has none or the text does not parse. Never raises.
#[pyfunction]
fn precheck_root(instrument_text: &str) -> Option<String> {
    instrument(instrument_text).ok().and_then(|found| pc::root(&found))
}

#[pyfunction]
fn precheck_valid_response(body: &str) -> bool {
    pc::valid_response(body)
}

#[pyfunction]
fn precheck_loopback_base(url: &str) -> PyResult<String> {
    pc::loopback_base(url).map_err(refuse)
}

#[pyfunction]
#[pyo3(signature = (order_qty, new_qty, side, position, price_changed))]
fn precheck_replace_opening(
    order_qty: &str,
    new_qty: Option<String>,
    side: &str,
    position: &str,
    price_changed: bool,
) -> PyResult<Option<String>> {
    let run = || -> LR<Option<Money>> {
        let new_qty = new_qty.as_deref().map(dec).transpose()?;
        pc::replace_opening(&dec(order_qty)?, new_qty, parse_side(side)?, &dec(position)?, price_changed)
    };
    Ok(run().map_err(refuse)?.map(|opening| opening.to_string()))
}

// --- W7-CORE-C8: the `Precheck` orchestration, moved into Rust -------------------------
//
// `PrecheckBase` owns the sequencing `src/trade_engine/oms/precheck.py::Precheck` did: the
// submit and replace chokepoints, the split, the first-refusal loop and the check body.
// Every collaborator (`_config`, `_clock`, `_ledger`, `_client`) and the `send` callback are
// Python objects reached through attribute lookup; the decision rules (`pc::split_allocations`,
// `pc::replace_opening`, `pc::root`) are te_core, called directly. `PrecheckConfigError` and
// the `logging` logger stay Python: the logger is called here, with the same name, level and
// messages the tests read via `caplog`.

/// A Python `str` as a `PyObject`.
fn py_str(py: Python<'_>, value: &str) -> PyResult<PyObject> {
    Ok(value.into_pyobject(py)?.into_any().unbind())
}

/// `_json_number`: an integral quantity crosses as an `int`, anything else as a `float`.
fn json_number(py: Python<'_>, quantity: &Bound<'_, PyAny>) -> PyResult<PyObject> {
    let integral = quantity.call_method0("to_integral_value")?;
    let is_integral = quantity.eq(&integral)?;
    let builtin = py.import("builtins")?;
    if is_integral {
        Ok(builtin.getattr("int")?.call1((quantity,))?.unbind())
    } else {
        Ok(builtin.getattr("float")?.call1((quantity,))?.unbind())
    }
}

/// `_text`: a Decimal as the plain fixed-point text the decision rules parse (never an exponent).
fn text_of(value: &Bound<'_, PyAny>) -> PyResult<String> {
    value.call_method1("__format__", ("f",))?.extract()
}

/// A `Decimal` built from a money value's canonical text (`Decimal(q)`).
fn decimal_of(py: Python<'_>, text: &str) -> PyResult<PyObject> {
    Ok(decimal_type(py)?.bind(py).call1((text,))?.unbind())
}

/// `dataclasses.replace(obj, **kwargs)`.
fn dcreplace(
    py: Python<'_>,
    obj: &Bound<'_, PyAny>,
    kwargs: &Bound<'_, PyDict>,
) -> PyResult<PyObject> {
    Ok(dataclasses_replace(py)?.bind(py).call((obj,), Some(kwargs))?.unbind())
}

/// `_total`: `sum((item.quantity for item in allocations), Decimal("0"))`.
fn total(py: Python<'_>, allocations: &Bound<'_, PyAny>) -> PyResult<PyObject> {
    let mut acc = decimal_type(py)?.bind(py).call1(("0",))?;
    for item in allocations.try_iter()? {
        let quantity = item?.getattr("quantity")?;
        acc = acc.call_method1("__add__", (&quantity,))?;
    }
    Ok(acc.unbind())
}

/// `CheckResult(allowed, kind)`.
fn check_result(py: Python<'_>, allowed: bool, kind: &str) -> PyResult<PyObject> {
    Ok(check_result_type(py)?.bind(py).call1((allowed, kind))?.unbind())
}

/// `VenueAck(venue_order_id, status, timestamp, message)`.
fn venue_ack(
    py: Python<'_>,
    venue_order_id: &str,
    status: &str,
    timestamp: &Bound<'_, PyAny>,
    message: &str,
) -> PyResult<PyObject> {
    Ok(venue_ack_type(py)?.bind(py).call1((venue_order_id, status, timestamp, message))?.unbind())
}

/// The request `_check` builds: one account key, one instrument root, the side, the (int or
/// float) quantity, and the fixed `isReducing`/`origin`.
fn check_body<'py>(
    py: Python<'py>,
    binding: &Bound<'py, PyAny>,
    root: &str,
    side_value: &str,
    quantity: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyDict>> {
    let request = PyDict::new(py);
    request.set_item("accountKey", binding.getattr("account_key")?.call_method0("copy")?)?;
    request.set_item("instrument", root)?;
    request.set_item("side", if side_value == "BUY" { "long" } else { "short" })?;
    request.set_item("qty", json_number(py, quantity)?)?;
    request.set_item("isReducing", false)?;
    request.set_item("origin", "strategy")?;
    Ok(request)
}

/// `logger.warning(fmt, *args)`: the same lazy `%`-style formatting and logger the tests read.
fn warn(py: Python<'_>, fmt: &'static str, args: Vec<PyObject>) -> PyResult<()> {
    let logger = precheck_logger(py)?;
    let mut items: Vec<PyObject> = Vec::with_capacity(args.len() + 1);
    items.push(py_str(py, fmt)?);
    items.extend(args);
    let tuple = PyTuple::new(py, items)?;
    logger.bind(py).call_method1("warning", tuple)?;
    Ok(())
}

// Cached Python objects (imported once, held for the interpreter's life).
fn check_result_type(py: Python<'_>) -> PyResult<&'static Py<PyAny>> {
    static CELL: GILOnceCell<Py<PyAny>> = GILOnceCell::new();
    CELL.get_or_try_init(py, || {
        Ok(py.import("trade_engine.oms.precheck")?.getattr("CheckResult")?.unbind())
    })
}

fn venue_ack_type(py: Python<'_>) -> PyResult<&'static Py<PyAny>> {
    static CELL: GILOnceCell<Py<PyAny>> = GILOnceCell::new();
    CELL.get_or_try_init(py, || {
        Ok(py.import("trade_engine.interfaces.broker")?.getattr("VenueAck")?.unbind())
    })
}

fn dataclasses_replace(py: Python<'_>) -> PyResult<&'static Py<PyAny>> {
    static CELL: GILOnceCell<Py<PyAny>> = GILOnceCell::new();
    CELL.get_or_try_init(py, || Ok(py.import("dataclasses")?.getattr("replace")?.unbind()))
}

fn decimal_type(py: Python<'_>) -> PyResult<&'static Py<PyAny>> {
    static CELL: GILOnceCell<Py<PyAny>> = GILOnceCell::new();
    CELL.get_or_try_init(py, || Ok(py.import("decimal")?.getattr("Decimal")?.unbind()))
}

fn precheck_logger(py: Python<'_>) -> PyResult<&'static Py<PyAny>> {
    static CELL: GILOnceCell<Py<PyAny>> = GILOnceCell::new();
    CELL.get_or_try_init(py, || {
        Ok(py.import("logging")?.call_method1("getLogger", ("trade_engine.oms.precheck",))?.unbind())
    })
}

fn codec_module(py: Python<'_>) -> PyResult<&'static Py<PyAny>> {
    static CELL: GILOnceCell<Py<PyAny>> = GILOnceCell::new();
    CELL.get_or_try_init(py, || {
        Ok(py.import("trade_engine.ledger.codec")?.into_any().unbind())
    })
}

/// The submit and replace wrapper, as a base class: `class Precheck(rs.PrecheckBase)`.
///
/// It holds no Rust state. `Precheck.__init__` (still Python) stores `_config`, `_clock`,
/// `_ledger` and `_client` on the instance `__dict__`; every method here reads them back
/// through Python attribute lookup, so a test subclass or a monkeypatched attribute keeps
/// working.
#[pyclass(subclass, module = "trade_engine_rs")]
pub(crate) struct PrecheckBase;

impl PrecheckBase {
    fn is_enforcing(slf: &Bound<'_, Self>) -> PyResult<bool> {
        let mode: String = slf.getattr("_config")?.getattr("mode")?.extract()?;
        Ok(mode == "enforce")
    }

    /// `_position`: the account's position quantity as a Decimal, or `Decimal("0")` when flat.
    fn position(
        slf: &Bound<'_, Self>,
        account_id: &str,
        instr: &Bound<'_, PyAny>,
    ) -> PyResult<PyObject> {
        let py = slf.py();
        let ledger = slf.getattr("_ledger")?;
        let state = ledger.call_method1("state", (account_id,))?;
        let position = state.getattr("positions")?.call_method1("get", (instr,))?;
        if position.is_none() {
            Ok(decimal_type(py)?.bind(py).call1(("0",))?.unbind())
        } else {
            Ok(position.getattr("quantity")?.unbind())
        }
    }

    /// `_text(_position(...))`.
    fn position_text(
        slf: &Bound<'_, Self>,
        account_id: &str,
        instr: &Bound<'_, PyAny>,
    ) -> PyResult<String> {
        let position = Self::position(slf, account_id, instr)?;
        text_of(position.bind(slf.py()))
    }

    /// `_working_orders`: the working orders whose venue id is `venue_order_id`.
    fn working_orders(slf: &Bound<'_, Self>, venue_order_id: &str) -> PyResult<PyObject> {
        let py = slf.py();
        let ledger = slf.getattr("_ledger")?;
        let found = PyList::empty(py);
        for account in ledger.call_method0("accounts")?.try_iter()? {
            let account = account?;
            let state = ledger.call_method1("state", (&account,))?;
            let orders = state.getattr("orders")?;
            let venue_order_ids = state.getattr("venue_order_ids")?;
            for item in orders.call_method0("items")?.try_iter()? {
                let (order_id, order): (Bound<'_, PyAny>, Bound<'_, PyAny>) = item?.extract()?;
                let order_id_str: String = order_id.extract()?;
                let mapped: String = venue_order_ids
                    .call_method1("get", (&order_id_str, &order_id_str))?
                    .extract()?;
                if mapped == venue_order_id {
                    found.append(order)?;
                }
            }
        }
        Ok(found.into_any().unbind())
    }

    /// `_check`: the binding lookup, the root, the request body and the client call.
    fn check(
        slf: &Bound<'_, Self>,
        account_id: Option<&str>,
        instr: &Bound<'_, PyAny>,
        side: &Bound<'_, PyAny>,
        quantity: &Bound<'_, PyAny>,
    ) -> PyResult<PyObject> {
        let py = slf.py();
        let binding = match account_id {
            None => None,
            Some(id) => {
                let accounts = slf.getattr("_config")?.getattr("accounts")?;
                let binding = accounts.call_method1("get", (id,))?;
                if binding.is_none() { None } else { Some(binding) }
            }
        };
        let Some(binding) = binding else {
            return check_result(py, false, "unmapped_account");
        };
        // `precheck_root(codec.text(codec.canon(instrument)))`.
        let codec = codec_module(py)?;
        let canon = codec.bind(py).call_method1("canon", (instr,))?;
        let text: String = codec.bind(py).call_method1("text", (canon,))?.extract()?;
        let root = instrument(&text).ok().and_then(|found| pc::root(&found));
        let Some(root) = root else {
            return check_result(py, false, "invalid_instrument");
        };
        let side_value: String = side.getattr("value")?.extract()?;
        let request = check_body(py, &binding, &root, &side_value, quantity)?;
        let core_account: String = binding.getattr("core_account")?.extract()?;
        let client = slf.getattr("_client")?;
        Ok(client.call_method1("check", (core_account, request))?.unbind())
    }

    /// `_split_allocations`: the split and the rebuild of the two allocation tuples and the
    /// opening-by-account mapping.
    fn split_allocations(
        slf: &Bound<'_, Self>,
        order: &Bound<'_, PyAny>,
    ) -> PyResult<(PyObject, PyObject, PyObject)> {
        let py = slf.py();
        let instrument = order.getattr("instrument")?;
        let allocations = order.getattr("allocations")?;
        let side_value: String = order.getattr("side")?.getattr("value")?.extract()?;
        let side = parse_side(&side_value).map_err(refuse)?;

        let mut allocs: Vec<(String, Money)> = Vec::new();
        let mut accounts: Vec<String> = Vec::new();
        for item in allocations.try_iter()? {
            let item = item?;
            let account: String = item.getattr("account_id")?.extract()?;
            let qty_text = text_of(&item.getattr("quantity")?)?;
            allocs.push((account.clone(), dec(&qty_text).map_err(refuse)?));
            if !accounts.contains(&account) {
                accounts.push(account);
            }
        }
        let mut positions: HashMap<String, Money> = HashMap::new();
        for account in &accounts {
            let text = Self::position_text(slf, account, &instrument)?;
            positions.insert(account.clone(), dec(&text).map_err(refuse)?);
        }
        let split = pc::split_allocations(side, &allocs, &positions).map_err(refuse)?;

        let rebuild = |rows: &[(usize, Money)]| -> PyResult<PyObject> {
            let items: Vec<PyObject> = rows
                .iter()
                .map(|(i, m)| {
                    let kwargs = PyDict::new(py);
                    kwargs.set_item("quantity", decimal_of(py, &m.to_string())?)?;
                    Ok(dcreplace(py, &allocations.get_item(*i)?, &kwargs)?)
                })
                .collect::<PyResult<_>>()?;
            Ok(PyTuple::new(py, items)?.into_any().unbind())
        };

        let reducing_allocs = rebuild(&split.reducing)?;
        let opening_allocs = rebuild(&split.opening)?;
        let openings = PyDict::new(py);
        for (account, m) in &split.openings {
            openings.set_item(account.as_str(), decimal_of(py, &m.to_string())?)?;
        }
        Ok((reducing_allocs, opening_allocs, openings.into_any().unbind()))
    }

    /// `_first_refusal`: each opening account checked once, in order; the first refusal stops.
    fn first_refusal(
        slf: &Bound<'_, Self>,
        order: &Bound<'_, PyAny>,
        openings: &Bound<'_, PyAny>,
    ) -> PyResult<(Option<PyObject>, Option<PyObject>)> {
        let py = slf.py();
        let instrument = order.getattr("instrument")?;
        let side = order.getattr("side")?;
        let mut verdict: Option<PyObject> = None;
        for item in openings.call_method0("items")?.try_iter()? {
            let (account, quantity): (Bound<'_, PyAny>, Bound<'_, PyAny>) = item?.extract()?;
            let account_str: String = account.extract()?;
            let result = Self::check(slf, Some(&account_str), &instrument, &side, &quantity)?;
            if !result.bind(py).getattr("allowed")?.extract::<bool>()? {
                return Ok((Some(result), None));
            }
            verdict = Some(result.bind(py).call_method0("verdict")?.unbind());
        }
        Ok((None, verdict))
    }

    /// `_refuse`: the warning and the `REJECTED` ack.
    fn refuse_ack(
        slf: &Bound<'_, Self>,
        venue_order_id: &str,
        result: &Bound<'_, PyAny>,
    ) -> PyResult<PyObject> {
        let py = slf.py();
        let kind: String = result.getattr("kind")?.extract()?;
        warn(
            py,
            "riskguard precheck refused: venue_order=%s kind=%s",
            vec![py_str(py, venue_order_id)?, py_str(py, &kind)?],
        )?;
        let now = slf.getattr("_clock")?.call_method0("now_utc")?;
        let message = format!("riskguard precheck refused: {kind}");
        venue_ack(py, venue_order_id, "REJECTED", &now, &message)
    }

    /// `_submit_split`: the reducing part at once, then the checked opening part.
    fn submit_split(
        slf: &Bound<'_, Self>,
        order: &Bound<'_, PyAny>,
        reducing_allocs: &Bound<'_, PyAny>,
        opening_allocs: &Bound<'_, PyAny>,
        openings: &Bound<'_, PyAny>,
        send: &Bound<'_, PyAny>,
    ) -> PyResult<PyObject> {
        let py = slf.py();
        let reducing = total(py, reducing_allocs)?;
        let opening = total(py, opening_allocs)?;
        let venue_order_id: String = order.getattr("venue_order_id")?.extract()?;

        let kwargs = PyDict::new(py);
        kwargs.set_item("quantity", &reducing)?;
        kwargs.set_item("allocations", reducing_allocs)?;
        let reducing_order = dcreplace(py, order, &kwargs)?;
        let reduce_ack = send.call1((reducing_order,))?;
        if reduce_ack.getattr("status")?.extract::<String>()? == "REJECTED" {
            return Ok(reduce_ack.unbind());
        }

        let (result, verdict) = Self::first_refusal(slf, order, openings)?;
        if let Some(result) = result {
            let result = result.bind(py);
            let kind: String = result.getattr("kind")?.extract()?;
            warn(
                py,
                "riskguard precheck refused opening part: venue_order=%s kind=%s qty=%s",
                vec![py_str(py, &venue_order_id)?, py_str(py, &kind)?, opening.clone_ref(py)],
            )?;
            let opening_text: String = opening.bind(py).str()?.extract()?;
            let kwargs = PyDict::new(py);
            kwargs.set_item("message", format!("opening {opening_text} refused: {kind}"))?;
            return dcreplace(py, &reduce_ack, &kwargs);
        }

        let kwargs = PyDict::new(py);
        kwargs.set_item("venue_order_id", format!("{venue_order_id}:open"))?;
        kwargs.set_item("quantity", &opening)?;
        kwargs.set_item("allocations", opening_allocs)?;
        if let Some(verdict) = verdict {
            kwargs.set_item("riskguard_verdict", verdict)?;
        }
        let opening_order = dcreplace(py, order, &kwargs)?;
        let open_ack = send.call1((opening_order,))?;
        if open_ack.getattr("status")?.extract::<String>()? == "REJECTED" {
            let opening_text: String = opening.bind(py).str()?.extract()?;
            let kwargs = PyDict::new(py);
            kwargs.set_item("message", format!("opening {opening_text} rejected by the venue"))?;
            return dcreplace(py, &reduce_ack, &kwargs);
        }
        Ok(reduce_ack.unbind())
    }
}

#[pymethods]
impl PrecheckBase {
    #[new]
    #[pyo3(signature = (*args, **kwargs))]
    fn new(args: &Bound<'_, PyAny>, kwargs: Option<&Bound<'_, PyAny>>) -> Self {
        let _ = (args, kwargs);
        PrecheckBase
    }

    #[getter]
    fn enforcing(slf: &Bound<'_, Self>) -> PyResult<bool> {
        Self::is_enforcing(slf)
    }

    fn submit(
        slf: &Bound<'_, Self>,
        order: &Bound<'_, PyAny>,
        send: &Bound<'_, PyAny>,
    ) -> PyResult<PyObject> {
        let py = slf.py();
        let (reducing_allocs, opening_allocs, openings) = Self::split_allocations(slf, order)?;
        let reducing_allocs = reducing_allocs.bind(py);
        let opening_allocs = opening_allocs.bind(py);
        let openings = openings.bind(py);
        let reducing = total(py, reducing_allocs)?;
        let opening = total(py, opening_allocs)?;

        if !Self::is_enforcing(slf)? {
            let ack = send.call1((order,))?;
            let instrument = order.getattr("instrument")?;
            let side = order.getattr("side")?;
            for item in openings.call_method0("items")?.try_iter()? {
                let (account, quantity): (Bound<'_, PyAny>, Bound<'_, PyAny>) = item?.extract()?;
                let account_str: String = account.extract()?;
                let result = Self::check(slf, Some(&account_str), &instrument, &side, &quantity)?;
                if !result.bind(py).getattr("allowed")?.extract::<bool>()? {
                    warn(
                        py,
                        "riskguard precheck would refuse (warn): venue_order=%s account=%s kind=%s qty=%s",
                        vec![
                            order.getattr("venue_order_id")?.unbind(),
                            account.unbind(),
                            result.bind(py).getattr("kind")?.unbind(),
                            quantity.unbind(),
                        ],
                    )?;
                }
            }
            return Ok(ack.unbind());
        }

        if opening.bind(py).eq(0)? {
            return Ok(send.call1((order,))?.unbind());
        }
        if reducing.bind(py).eq(0)? {
            let (result, verdict) = Self::first_refusal(slf, order, openings)?;
            if let Some(result) = result {
                let venue_order_id: String = order.getattr("venue_order_id")?.extract()?;
                return Self::refuse_ack(slf, &venue_order_id, result.bind(py));
            }
            let kwargs = PyDict::new(py);
            if let Some(verdict) = verdict {
                kwargs.set_item("riskguard_verdict", verdict)?;
            }
            let replaced = dcreplace(py, order, &kwargs)?;
            return Ok(send.call1((replaced,))?.unbind());
        }

        Self::submit_split(slf, order, reducing_allocs, opening_allocs, openings, send)
    }

    fn replace(
        slf: &Bound<'_, Self>,
        venue_order_id: &str,
        changes: &Bound<'_, PyAny>,
        send: &Bound<'_, PyAny>,
    ) -> PyResult<PyObject> {
        let py = slf.py();
        let orders = Self::working_orders(slf, venue_order_id)?;
        let orders = orders.bind(py);
        let len = orders.len()?;
        let venue_id_py = py_str(py, venue_order_id)?;

        if len > 1 {
            let mut accounts: Vec<String> = Vec::new();
            for item in orders.try_iter()? {
                let account_id: String = item?.getattr("account_id")?.extract()?;
                if !accounts.contains(&account_id) {
                    accounts.push(account_id);
                }
            }
            accounts.sort();
            warn(
                py,
                "riskguard precheck: replace of a netted order not checked: venue_order=%s accounts=%s",
                vec![venue_id_py.clone_ref(py), py_str(py, &accounts.join(","))?],
            )?;
            return Ok(send.call1((venue_order_id, changes))?.unbind());
        }

        let order = if len == 1 { Some(orders.get_item(0)?) } else { None };
        let Some(order) = order else {
            if Self::is_enforcing(slf)? {
                let result = check_result(py, false, "unknown_order")?;
                return Self::refuse_ack(slf, venue_order_id, result.bind(py));
            }
            warn(
                py,
                "riskguard precheck: replace of an unknown order not checked: venue_order=%s",
                vec![venue_id_py.clone_ref(py)],
            )?;
            return Ok(send.call1((venue_order_id, changes))?.unbind());
        };

        let new_limit = changes.getattr("new_limit_price")?;
        let new_stop = changes.getattr("new_stop_price")?;
        let order_limit = order.getattr("limit_price")?;
        let order_stop = order.getattr("stop_price")?;
        let price_changed = (!new_limit.is_none() && !new_limit.eq(&order_limit)?)
            || (!new_stop.is_none() && !new_stop.eq(&order_stop)?);

        let new_qty = changes.getattr("new_quantity")?;
        let new_qty_text: Option<String> = if new_qty.is_none() {
            None
        } else {
            Some(text_of(&new_qty)?)
        };
        let order_qty_text = text_of(&order.getattr("quantity")?)?;
        let side_value: String = order.getattr("side")?.getattr("value")?.extract()?;
        let account_id: String = order.getattr("account_id")?.extract()?;
        let position_text = Self::position_text(slf, &account_id, &order.getattr("instrument")?)?;

        let opening = {
            let order_qty = dec(&order_qty_text).map_err(refuse)?;
            let new_qty = new_qty_text.as_deref().map(dec).transpose().map_err(refuse)?;
            let side = parse_side(&side_value).map_err(refuse)?;
            let position = dec(&position_text).map_err(refuse)?;
            pc::replace_opening(&order_qty, new_qty, side, &position, price_changed).map_err(refuse)?
        };
        let Some(opening) = opening else {
            return Ok(send.call1((venue_order_id, changes))?.unbind());
        };

        let opening_decimal = decimal_of(py, &opening.to_string())?;
        let result = Self::check(
            slf,
            Some(&account_id),
            &order.getattr("instrument")?,
            &order.getattr("side")?,
            opening_decimal.bind(py),
        )?;
        let result_allowed: bool = result.bind(py).getattr("allowed")?.extract()?;

        if !result_allowed && Self::is_enforcing(slf)? {
            return Self::refuse_ack(slf, venue_order_id, result.bind(py));
        }

        let changes_out: PyObject;
        if !result_allowed {
            warn(
                py,
                "riskguard precheck would refuse replace (warn): venue_order=%s kind=%s qty=%s",
                vec![venue_id_py, result.bind(py).getattr("kind")?.unbind(), opening_decimal],
            )?;
            changes_out = changes.clone().unbind();
        } else if Self::is_enforcing(slf)? {
            let kwargs = PyDict::new(py);
            kwargs.set_item("riskguard_verdict", result.bind(py).call_method0("verdict")?)?;
            changes_out = dcreplace(py, changes, &kwargs)?;
        } else {
            changes_out = changes.clone().unbind();
        }
        Ok(send.call1((venue_order_id, changes_out))?.unbind())
    }

    fn _check(
        slf: &Bound<'_, Self>,
        account_id: &str,
        instrument: &Bound<'_, PyAny>,
        side: &Bound<'_, PyAny>,
        quantity: &Bound<'_, PyAny>,
    ) -> PyResult<PyObject> {
        Self::check(slf, Some(account_id), instrument, side, quantity)
    }

    fn _position(
        slf: &Bound<'_, Self>,
        account_id: &str,
        instrument: &Bound<'_, PyAny>,
    ) -> PyResult<PyObject> {
        Self::position(slf, account_id, instrument)
    }

    fn _working_orders(slf: &Bound<'_, Self>, venue_order_id: &str) -> PyResult<PyObject> {
        Self::working_orders(slf, venue_order_id)
    }

    fn _split_allocations(
        slf: &Bound<'_, Self>,
        order: &Bound<'_, PyAny>,
    ) -> PyResult<(PyObject, PyObject, PyObject)> {
        Self::split_allocations(slf, order)
    }

    fn _submit_split(
        slf: &Bound<'_, Self>,
        order: &Bound<'_, PyAny>,
        reducing_allocs: &Bound<'_, PyAny>,
        opening_allocs: &Bound<'_, PyAny>,
        openings: &Bound<'_, PyAny>,
        send: &Bound<'_, PyAny>,
    ) -> PyResult<PyObject> {
        Self::submit_split(slf, order, reducing_allocs, opening_allocs, openings, send)
    }

    fn _first_refusal(
        slf: &Bound<'_, Self>,
        order: &Bound<'_, PyAny>,
        openings: &Bound<'_, PyAny>,
    ) -> PyResult<(Option<PyObject>, Option<PyObject>)> {
        Self::first_refusal(slf, order, openings)
    }

    fn _refuse(
        slf: &Bound<'_, Self>,
        venue_order_id: &str,
        result: &Bound<'_, PyAny>,
    ) -> PyResult<PyObject> {
        Self::refuse_ack(slf, venue_order_id, result)
    }
}

pub(crate) fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(precheck_split_quantity, m)?)?;
    m.add_function(wrap_pyfunction!(precheck_split_allocations, m)?)?;
    m.add_function(wrap_pyfunction!(precheck_root, m)?)?;
    m.add_function(wrap_pyfunction!(precheck_valid_response, m)?)?;
    m.add_function(wrap_pyfunction!(precheck_loopback_base, m)?)?;
    m.add_function(wrap_pyfunction!(precheck_replace_opening, m)?)?;
    m.add_class::<PrecheckBase>()?;
    Ok(())
}

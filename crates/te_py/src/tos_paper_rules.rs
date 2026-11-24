//! Typed doors for the `tos_paper` pure decisions (W6-F0b): the `_rs.decide` JSON round
//! trip is gone. Each pyfunction takes Python objects and answers Python objects; a refusal
//! crosses as `ValueError((kind, message))` exactly as `_rs.decide` raised it, so
//! `_rs.call` maps it back to the same Python exception.

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyDict, PyFloat, PyInt, PyList, PyString, PyTuple};
use te_core::ledger::json::Json;
use te_core::ledger::mirror::book_hk;
use te_core::ledger::model::{Instrument, LErr, OrderState, OrderType, Side, Tif};
use te_core::ledger::ops::OMap;
use te_core::money::Money;
use te_core::options::Right;
use te_core::tos_paper::wire::{ComboLeg as WireComboLeg, Instrument as WireInstrument, OptionC};
use te_core::tos_paper::{cover, netting, normalize, reconcile, slippage};

use crate::store_ledger as sl;

fn refuse(e: LErr) -> PyErr {
    PyValueError::new_err((e.kind.to_string(), e.msg))
}

fn wire_err(msg: impl Into<String>) -> PyErr {
    PyValueError::new_err(("tos_wire".to_string(), msg.into()))
}

fn side_of(s: &str) -> PyResult<Side> {
    Side::parse(s).ok_or_else(|| PyValueError::new_err(format!("bad side {s:?}")))
}

fn order_type_of(s: &str) -> PyResult<OrderType> {
    OrderType::parse(s).ok_or_else(|| PyValueError::new_err(format!("bad order type {s:?}")))
}

fn tif_of(s: &str) -> PyResult<Tif> {
    Tif::parse(s).ok_or_else(|| PyValueError::new_err(format!("bad tif {s:?}")))
}

/// A Python `StrEnum` (`.value`).
fn enum_value(v: &Bound<'_, PyAny>) -> PyResult<String> {
    v.getattr("value")?.extract()
}

fn opt_money(v: &Bound<'_, PyAny>) -> PyResult<Option<Money>> {
    if v.is_none() {
        Ok(None)
    } else {
        sl::money(v).map(Some)
    }
}

fn isoformat(v: &Bound<'_, PyAny>) -> PyResult<String> {
    v.call_method0("isoformat")?.extract()
}

/// A Python `StrEnum` value as the enum object.
fn enum_py<'py>(py: Python<'py>, module: &str, name: &str, value: &str) -> PyResult<Bound<'py, PyAny>> {
    py.import(module)?.getattr(name)?.call1((value,))
}

/// A Python object as the te_core JSON value `json.loads` would have built it (the raw rows
/// the normalize door reads). Non-JSON values fall back to `str(value)` (`default=str`).
fn py_to_json(value: &Bound<'_, PyAny>) -> PyResult<Json> {
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
        return Ok(Json::BigInt(value.str()?.extract::<String>()?));
    }
    if value.is_instance_of::<PyFloat>() {
        let f: f64 = value.extract()?;
        return Ok(Json::Float(f.to_string()));
    }
    if value.is_instance_of::<PyString>() {
        return Ok(Json::Str(value.extract()?));
    }
    if value.is_instance_of::<PyList>() {
        let list = value.downcast::<PyList>()?;
        let mut items = Vec::new();
        for item in list.iter() {
            items.push(py_to_json(&item)?);
        }
        return Ok(Json::Arr(items));
    }
    if value.is_instance_of::<PyDict>() {
        let dict = value.downcast::<PyDict>()?;
        let mut pairs = Vec::new();
        for (k, v) in dict.iter() {
            let key: String = k.extract()?;
            pairs.push((key, py_to_json(&v)?));
        }
        return Ok(Json::Obj(pairs));
    }
    Ok(Json::Str(value.str()?.extract::<String>()?))
}

// -- cover --------------------------------------------------------------------------------

#[pyfunction]
fn tos_cover_covers(long: &Bound<'_, PyAny>, short: &Bound<'_, PyAny>) -> PyResult<bool> {
    let long = sl::instrument_of(long)?;
    let short = sl::instrument_of(short)?;
    let (Instrument::Option(long), Instrument::Option(short)) = (long, short) else {
        return Err(wire_err("expected an option contract"));
    };
    cover::covers(&long, &short).map_err(refuse)
}

#[pyfunction]
fn tos_cover_uncovered(py: Python<'_>, held: &Bound<'_, PyAny>) -> PyResult<PyObject> {
    let held = sl::closes(held)?;
    let found = cover::uncovered(&held).map_err(refuse)?;
    let dict = PyDict::new(py);
    for (underlying, count) in found {
        dict.set_item(underlying, sl::decimal_py(py, &count)?)?;
    }
    Ok(dict.into_any().unbind())
}

#[pyfunction]
fn tos_cover_holdings(py: Python<'_>, mirror: &Bound<'_, PyAny>) -> PyResult<PyObject> {
    let mirror = sl::mirror_of(mirror)?;
    let held = cover::holdings(&mirror).map_err(refuse)?;
    let dict = PyDict::new(py);
    for (instrument, qty) in held.iter() {
        dict.set_item(sl::instrument_py(py, instrument)?, sl::decimal_py(py, qty)?)?;
    }
    Ok(dict.into_any().unbind())
}

fn sold_of(order: &Bound<'_, PyAny>) -> PyResult<cover::Sold> {
    let instrument = sl::instrument_of(&order.getattr("instrument")?)?;
    let side = side_of(&enum_value(&order.getattr("side")?)?)?;
    let quantity = sl::money(&order.getattr("quantity")?)?;
    cover::sold(&instrument, side, &quantity).map_err(refuse)
}

#[pyfunction]
fn tos_cover_cover_reason(
    mirror: &Bound<'_, PyAny>,
    order: &Bound<'_, PyAny>,
    accepted: Vec<Bound<'_, PyAny>>,
) -> PyResult<Option<String>> {
    let mirror = sl::mirror_of(mirror)?;
    let sold = sold_of(order)?;
    let accepted = accepted.iter().map(sold_of).collect::<PyResult<Vec<_>>>()?;
    cover::cover_reason(&mirror, &sold, &accepted).map_err(refuse)
}

// -- netting ------------------------------------------------------------------------------

#[pyfunction]
#[pyo3(signature = (venue_account, instrument, side, quantity, order_type, limit_price, tif, order_ids))]
#[allow(clippy::too_many_arguments)]
fn tos_netting_ticket_key(
    venue_account: &str,
    instrument: &Bound<'_, PyAny>,
    side: &str,
    quantity: &Bound<'_, PyAny>,
    order_type: &str,
    limit_price: Option<&Bound<'_, PyAny>>,
    tif: &str,
    order_ids: Vec<String>,
) -> PyResult<String> {
    let instrument = sl::instrument_of(instrument)?;
    let side = side_of(side)?;
    let quantity = sl::money(quantity)?;
    let order_type = order_type_of(order_type)?;
    let limit_price = limit_price.map(sl::money).transpose()?;
    let tif = tif_of(tif)?;
    netting::ticket_key(venue_account, &instrument, side, &quantity, order_type, &limit_price, tif, &order_ids)
        .map_err(refuse)
}

#[pyfunction]
fn tos_netting_vertical_reason(combo: &Bound<'_, PyAny>) -> PyResult<Option<String>> {
    let instrument = sl::instrument_of(combo)?;
    let Instrument::Combo(legs) = instrument else {
        return Err(wire_err("vertical_reason takes a combo"));
    };
    netting::vertical_reason_model(&legs).map_err(refuse)
}

fn norder_of(o: &Bound<'_, PyAny>) -> PyResult<netting::NOrder> {
    let instrument = o.getattr("instrument")?;
    let wire = wire_instrument_of(&instrument)?;
    let model = if matches!(wire, WireInstrument::Other { .. }) {
        None
    } else {
        Some(sl::instrument_of(&instrument)?)
    };
    Ok(netting::NOrder {
        order_id: o.getattr("order_id")?.extract()?,
        account_id: o.getattr("account_id")?.extract()?,
        wire,
        model,
        order_type: order_type_of(&enum_value(&o.getattr("order_type")?)?)?,
        side: side_of(&enum_value(&o.getattr("side")?)?)?,
        quantity: sl::money(&o.getattr("quantity")?)?,
        tif: tif_of(&enum_value(&o.getattr("tif")?)?)?,
        limit_price: opt_money(&o.getattr("limit_price")?)?,
    })
}

/// A Python instrument as the wire form `_rs.wire` builds it: an equity, an option, a combo,
/// or `other` (the `repr` of anything the mirror does not know, a future or a subclass).
fn wire_instrument_of(value: &Bound<'_, PyAny>) -> PyResult<WireInstrument> {
    let instruments = value.py().import("trade_engine.domain.instruments")?;
    let equity = instruments.getattr("Equity")?;
    let option = instruments.getattr("OptionContract")?;
    let combo = instruments.getattr("Combo")?;
    if value.is_instance(&equity)? {
        let symbol: String = value.getattr("symbol")?.extract()?;
        return Ok(WireInstrument::Equity { symbol });
    }
    if value.is_instance(&option)? {
        let underlying: String = value.getattr("underlying")?.extract()?;
        let expiry: String = value.getattr("expiry")?.call_method0("isoformat")?.extract()?;
        let strike = sl::money(&value.getattr("strike")?)?;
        let right: String = value.getattr("right")?.getattr("value")?.extract()?;
        let right = if right == "C" { Right::Call } else { Right::Put };
        let multiplier: i128 = value.getattr("multiplier")?.extract()?;
        return Ok(WireInstrument::Option(OptionC { underlying, expiry, strike, right, multiplier }));
    }
    if value.is_instance(&combo)? {
        let mut legs = Vec::new();
        for leg in value.getattr("legs")?.try_iter()? {
            let leg = leg?;
            let contract = wire_instrument_of(&leg.getattr("contract")?)?;
            let ratio: i128 = leg.getattr("ratio")?.extract()?;
            let side: String = leg.getattr("side")?.getattr("value")?.extract()?;
            legs.push(WireComboLeg { contract, ratio, side });
        }
        return Ok(WireInstrument::Combo(legs));
    }
    let repr: String = value.repr()?.extract()?;
    Ok(WireInstrument::Other { repr })
}

fn holdings_of(value: &Bound<'_, PyAny>) -> PyResult<OMap<(String, Instrument), Money>> {
    let mut out = OMap::new();
    for item in value.call_method0("items")?.try_iter()? {
        let (key, qty): (Bound<'_, PyAny>, Bound<'_, PyAny>) = item?.extract()?;
        let (account, instrument): (String, Bound<'_, PyAny>) = key.extract()?;
        let instrument = sl::instrument_of(&instrument)?;
        let qty = sl::money(&qty)?;
        let hk = book_hk(&account, &instrument);
        out.insert(hk, (account, instrument), qty);
    }
    Ok(out)
}

fn venue_order_py(py: Python<'_>, t: &netting::Ticket) -> PyResult<PyObject> {
    let broker = py.import("trade_engine.interfaces.broker")?;
    let vo_cls = broker.getattr("VenueOrder")?;
    let alloc_cls = broker.getattr("VenueOrderAllocation")?;
    let mut allocation_items = Vec::new();
    for (soid, account, qty) in &t.allocations {
        allocation_items.push(alloc_cls.call1((soid, account, sl::decimal_py(py, qty)?))?);
    }
    let allocations = PyTuple::new(py, allocation_items)?;
    let datetime = py.import("datetime")?.getattr("datetime")?;
    let limit = match &t.limit_price {
        Some(p) => sl::decimal_py(py, p)?,
        None => py.None().into_bound(py),
    };
    let kwargs = PyDict::new(py);
    kwargs.set_item("venue_order_id", &t.venue_order_id)?;
    kwargs.set_item("instrument", sl::instrument_py(py, &t.instrument)?)?;
    kwargs.set_item("order_type", enum_py(py, "trade_engine.domain.orders", "OrderType", t.order_type.value())?)?;
    kwargs.set_item("side", enum_py(py, "trade_engine.domain.instruments", "Side", t.side.value())?)?;
    kwargs.set_item("quantity", sl::decimal_py(py, &t.quantity)?)?;
    kwargs.set_item("submitted_at", datetime.call_method1("fromisoformat", (t.submitted_at.as_str(),))?)?;
    kwargs.set_item("tif", enum_py(py, "trade_engine.domain.orders", "TimeInForce", t.tif.value())?)?;
    kwargs.set_item("limit_price", limit)?;
    kwargs.set_item("allocations", allocations)?;
    Ok(vo_cls.call((), Some(&kwargs))?.unbind())
}

#[pyfunction]
#[pyo3(signature = (orders, venue_account, mirrored, at, holdings=None))]
fn tos_netting_net_strategy_orders(
    py: Python<'_>,
    orders: &Bound<'_, PyAny>,
    venue_account: &str,
    mirrored: Vec<String>,
    at: &Bound<'_, PyAny>,
    holdings: Option<&Bound<'_, PyAny>>,
) -> PyResult<(Vec<PyObject>, Vec<(String, String)>)> {
    let at = isoformat(at)?;
    let mut norders = Vec::new();
    for o in orders.try_iter()? {
        norders.push(norder_of(&o?)?);
    }
    let holdings = match holdings {
        None => OMap::new(),
        Some(v) => holdings_of(v)?,
    };
    let batch = netting::net_strategy_orders(&norders, venue_account, &mirrored, &at, &holdings).map_err(refuse)?;
    let mut venue_orders = Vec::new();
    for t in &batch.venue_orders {
        venue_orders.push(venue_order_py(py, t)?);
    }
    Ok((venue_orders, batch.refused))
}

#[pyfunction]
fn tos_netting_account_for_everything(orders: Vec<String>, allocated: Vec<String>, refused: Vec<String>) -> PyResult<()> {
    let mut outcomes = allocated;
    outcomes.extend(refused);
    netting::account_for_everything(&orders, &outcomes).map_err(refuse)
}

// -- reconcile ----------------------------------------------------------------------------

fn positions_of(value: &Bound<'_, PyAny>) -> PyResult<Vec<(Instrument, Money)>> {
    let mut out = Vec::new();
    for item in value.try_iter()? {
        let item = item?;
        let instrument = sl::instrument_of(&item.getattr("instrument")?)?;
        let quantity = sl::money(&item.getattr("quantity")?)?;
        out.push((instrument, quantity));
    }
    Ok(out)
}

fn rows_of(value: &Bound<'_, PyAny>) -> PyResult<Vec<reconcile::Row>> {
    let mut out = Vec::new();
    for item in value.try_iter()? {
        let item = item?;
        out.push(reconcile::Row {
            instrument: sl::instrument_of(&item.getattr("instrument")?)?,
            side: side_of(&enum_value(&item.getattr("side")?)?)?,
            quantity: sl::money(&item.getattr("quantity")?)?,
            filled: sl::money(&item.getattr("filled")?)?,
            order_type: order_type_of(&enum_value(&item.getattr("order_type")?)?)?,
            limit_price: opt_money(&item.getattr("limit_price")?)?,
            state: OrderState::parse(&enum_value(&item.getattr("state")?)?)
                .ok_or_else(|| PyValueError::new_err("bad order state"))?,
        });
    }
    Ok(out)
}

fn ticket_of(value: &Bound<'_, PyAny>) -> PyResult<reconcile::Ticket> {
    Ok(reconcile::Ticket {
        instrument: sl::instrument_of(&value.getattr("instrument")?)?,
        order_type: order_type_of(&enum_value(&value.getattr("order_type")?)?)?,
        side: side_of(&enum_value(&value.getattr("side")?)?)?,
        quantity: sl::money(&value.getattr("quantity")?)?,
        limit_price: opt_money(&value.getattr("limit_price")?)?,
    })
}

fn dict_of_pairs(py: Python<'_>, book: &OMap<Instrument, Money>) -> PyResult<PyObject> {
    let dict = PyDict::new(py);
    for (instrument, qty) in book.iter() {
        dict.set_item(sl::instrument_py(py, instrument)?, sl::decimal_py(py, qty)?)?;
    }
    Ok(dict.into_any().unbind())
}

fn venue_reconcile_py(py: Python<'_>, event: &reconcile::ReconcileEvent, as_of: &Bound<'_, PyAny>) -> PyResult<PyObject> {
    let cls = py.import("trade_engine.ledger.events")?.getattr("VenueReconcile")?;
    let kwargs = PyDict::new(py);
    kwargs.set_item("venue", &event.venue)?;
    kwargs.set_item("as_of", as_of)?;
    kwargs.set_item("reconciled", event.reconciled)?;
    kwargs.set_item("drift", PyTuple::new(py, event.drift.iter().map(|s| s.as_str()))?)?;
    kwargs.set_item("note", event.note.as_deref())?;
    Ok(cls.call((), Some(&kwargs))?.unbind())
}

#[pyfunction]
fn tos_reconcile_position_book(py: Python<'_>, positions: &Bound<'_, PyAny>) -> PyResult<PyObject> {
    let book = reconcile::position_book(&positions_of(positions)?).map_err(refuse)?;
    dict_of_pairs(py, &book)
}

#[pyfunction]
fn tos_reconcile_reconcile(
    py: Python<'_>,
    venue: &str,
    as_of: &Bound<'_, PyAny>,
    expected: &Bound<'_, PyAny>,
    positions: &Bound<'_, PyAny>,
    working: &Bound<'_, PyAny>,
) -> PyResult<PyObject> {
    let as_of_text = isoformat(as_of)?;
    let expected = sl::closes(expected)?;
    let positions = positions_of(positions)?;
    let working = rows_of(working)?;
    let event = reconcile::reconcile_event(venue, &as_of_text, &expected, &positions, &working).map_err(refuse)?;
    venue_reconcile_py(py, &event, as_of)
}

#[pyfunction]
fn tos_reconcile_unreadable(
    py: Python<'_>,
    venue: &str,
    as_of: &Bound<'_, PyAny>,
    contracts: &Bound<'_, PyAny>,
    why: &str,
) -> PyResult<PyObject> {
    let as_of_text = isoformat(as_of)?;
    let mut contracts_vec = Vec::new();
    for c in contracts.try_iter()? {
        contracts_vec.push(sl::instrument_of(&c?)?);
    }
    let event = reconcile::unreadable_event(venue, &as_of_text, &contracts_vec, why).map_err(refuse)?;
    venue_reconcile_py(py, &event, as_of)
}

#[pyfunction]
fn tos_reconcile_confirm_ticket(
    ticket: &Bound<'_, PyAny>,
    before: &Bound<'_, PyAny>,
    positions: &Bound<'_, PyAny>,
    working: &Bound<'_, PyAny>,
    claimed: Vec<usize>,
) -> PyResult<(String, String, Vec<usize>)> {
    let ticket = ticket_of(ticket)?;
    let before = sl::closes(before)?;
    let positions = positions_of(positions)?;
    let working = rows_of(working)?;
    let mut claimed = claimed;
    let (status, reason) =
        reconcile::confirm_ticket(&ticket, &before, &positions, &working, &mut claimed).map_err(refuse)?;
    claimed.sort_unstable();
    claimed.dedup();
    Ok((status.to_string(), reason, claimed))
}

#[pyfunction]
#[pyo3(signature = (ticket, units=None))]
fn tos_reconcile_ticket_contracts(
    py: Python<'_>,
    ticket: &Bound<'_, PyAny>,
    units: Option<&Bound<'_, PyAny>>,
) -> PyResult<PyObject> {
    let ticket = ticket_of(ticket)?;
    let units = units.map(sl::money).transpose()?;
    let book = reconcile::ticket_contracts(&ticket, units.as_ref()).map_err(refuse)?;
    dict_of_pairs(py, &book)
}

// -- normalize ----------------------------------------------------------------------------

fn venue_ack(
    py: Python<'_>,
    venue_order_id: &str,
    status: &str,
    at: &Bound<'_, PyAny>,
    message: &str,
) -> PyResult<PyObject> {
    let cls = py.import("trade_engine.interfaces.broker")?.getattr("VenueAck")?;
    Ok(cls.call1((venue_order_id, status, at, message))?.unbind())
}

#[pyfunction]
fn tos_normalize_place_result(
    py: Python<'_>,
    raw: &Bound<'_, PyAny>,
    venue_order_id: &str,
    at: &Bound<'_, PyAny>,
) -> PyResult<PyObject> {
    let (status, message) = normalize::place_result_raw(&py_to_json(raw)?).map_err(refuse)?;
    venue_ack(py, venue_order_id, &status, at, &message)
}

#[pyfunction]
fn tos_normalize_place_exception(
    py: Python<'_>,
    class: &str,
    name: &str,
    text: &str,
    venue_order_id: &str,
    at: &Bound<'_, PyAny>,
) -> PyResult<PyObject> {
    let (status, message) = normalize::place_exception_raw(class, name, text).map_err(refuse)?;
    venue_ack(py, venue_order_id, &status, at, &message)
}

#[pyfunction]
fn tos_normalize_placed_order_id(raw: &Bound<'_, PyAny>) -> PyResult<Option<String>> {
    normalize::placed_order_id_raw(&py_to_json(raw)?).map_err(refuse)
}

#[pyfunction]
fn tos_normalize_cancel_result(
    py: Python<'_>,
    raw: &Bound<'_, PyAny>,
    venue_order_id: &str,
    at: &Bound<'_, PyAny>,
) -> PyResult<PyObject> {
    let (status, message) = normalize::cancel_result_raw(&py_to_json(raw)?).map_err(refuse)?;
    venue_ack(py, venue_order_id, &status, at, &message)
}

#[pyfunction]
fn tos_normalize_cancel_exception(
    py: Python<'_>,
    class: &str,
    name: &str,
    text: &str,
    venue_order_id: &str,
    at: &Bound<'_, PyAny>,
) -> PyResult<PyObject> {
    let (status, message) = normalize::cancel_exception_raw(class, name, text).map_err(refuse)?;
    venue_ack(py, venue_order_id, &status, at, &message)
}

#[pyfunction]
fn tos_normalize_working_order(py: Python<'_>, raw: &Bound<'_, PyAny>) -> PyResult<PyObject> {
    let w = normalize::working_order_raw(&py_to_json(raw)?).map_err(refuse)?;
    let cls = py.import("trade_engine.tos_paper.normalize")?.getattr("WorkingOrder")?;
    let limit = match &w.limit_price {
        Some(p) => sl::decimal_py(py, p)?,
        None => py.None().into_bound(py),
    };
    let kwargs = PyDict::new(py);
    kwargs.set_item("instrument", sl::instrument_py(py, &w.instrument)?)?;
    kwargs.set_item("side", enum_py(py, "trade_engine.domain.instruments", "Side", w.side.value())?)?;
    kwargs.set_item("quantity", sl::decimal_py(py, &w.quantity)?)?;
    kwargs.set_item("filled", sl::decimal_py(py, &w.filled)?)?;
    kwargs.set_item("order_type", enum_py(py, "trade_engine.domain.orders", "OrderType", w.order_type.value())?)?;
    kwargs.set_item("limit_price", limit)?;
    kwargs.set_item("state", enum_py(py, "trade_engine.domain.orders", "OrderState", w.state.value())?)?;
    Ok(cls.call((), Some(&kwargs))?.unbind())
}

#[pyfunction]
fn tos_normalize_book_state(raw_status: &Bound<'_, PyAny>) -> PyResult<String> {
    Ok(normalize::book_state_of(&py_to_json(raw_status)?).to_string())
}

#[pyfunction]
fn tos_normalize_order_fill(py: Python<'_>, raw: &Bound<'_, PyAny>) -> PyResult<PyObject> {
    let f = normalize::order_fill_raw(&py_to_json(raw)?).map_err(refuse)?;
    let cls = py.import("trade_engine.tos_paper.normalize")?.getattr("OrderFill")?;
    let avg = match &f.avg_price {
        Some(p) => sl::decimal_py(py, p)?,
        None => py.None().into_bound(py),
    };
    let kwargs = PyDict::new(py);
    kwargs.set_item("order_id", &f.order_id)?;
    kwargs.set_item("filled", sl::decimal_py(py, &f.filled)?)?;
    kwargs.set_item("avg_price", avg)?;
    kwargs.set_item("state", enum_py(py, "trade_engine.domain.orders", "OrderState", f.state.value())?)?;
    Ok(cls.call((), Some(&kwargs))?.unbind())
}

#[pyfunction]
fn tos_normalize_position(
    py: Python<'_>,
    raw: &Bound<'_, PyAny>,
    as_of: &Bound<'_, PyAny>,
) -> PyResult<PyObject> {
    let p = normalize::position_raw(&py_to_json(raw)?).map_err(refuse)?;
    let cls = py.import("trade_engine.interfaces.broker")?.getattr("VenuePosition")?;
    let kwargs = PyDict::new(py);
    kwargs.set_item("instrument", sl::instrument_py(py, &p.instrument)?)?;
    kwargs.set_item("quantity", sl::decimal_py(py, &p.quantity)?)?;
    kwargs.set_item("avg_price", sl::decimal_py(py, &p.avg_price)?)?;
    kwargs.set_item("as_of", as_of)?;
    Ok(cls.call((), Some(&kwargs))?.unbind())
}

// -- slippage -----------------------------------------------------------------------------

fn venue_fill_of(value: &Bound<'_, PyAny>) -> PyResult<slippage::VenueFill> {
    Ok(slippage::VenueFill {
        venue_fill_id: value.getattr("venue_fill_id")?.extract()?,
        venue_order_id: value.getattr("venue_order_id")?.extract()?,
        instrument: sl::instrument_of(&value.getattr("instrument")?)?,
        side: side_of(&enum_value(&value.getattr("side")?)?)?,
        quantity: sl::money(&value.getattr("quantity")?)?,
        price: sl::money(&value.getattr("price")?)?,
        filled_at: isoformat(&value.getattr("filled_at")?)?,
        fee: sl::money(&value.getattr("fee")?)?,
    })
}

fn venue_ticket_of(value: &Bound<'_, PyAny>) -> PyResult<slippage::VenueTicket> {
    let mut allocations = Vec::new();
    for a in value.getattr("allocations")?.try_iter()? {
        let a = a?;
        allocations.push((
            a.getattr("strategy_order_id")?.extract()?,
            a.getattr("account_id")?.extract()?,
            sl::money(&a.getattr("quantity")?)?,
        ));
    }
    Ok(slippage::VenueTicket {
        venue_order_id: value.getattr("venue_order_id")?.extract()?,
        instrument: sl::instrument_of(&value.getattr("instrument")?)?,
        side: side_of(&enum_value(&value.getattr("side")?)?)?,
        quantity: sl::money(&value.getattr("quantity")?)?,
        allocations,
    })
}

fn fill_py(
    py: Python<'_>,
    f: &slippage::AllocFill,
    instrument: &Bound<'_, PyAny>,
    filled_at: &Bound<'_, PyAny>,
) -> PyResult<PyObject> {
    let cls = py.import("trade_engine.domain.portfolio")?.getattr("Fill")?;
    let kwargs = PyDict::new(py);
    kwargs.set_item("fill_id", &f.fill_id)?;
    kwargs.set_item("order_id", &f.order_id)?;
    kwargs.set_item("account_id", &f.account_id)?;
    kwargs.set_item("instrument", instrument)?;
    kwargs.set_item("quantity", sl::decimal_py(py, &f.quantity)?)?;
    kwargs.set_item("price", sl::decimal_py(py, &f.price)?)?;
    kwargs.set_item("venue_env", f.venue_env)?;
    kwargs.set_item("filled_at", filled_at)?;
    kwargs.set_item("side", enum_py(py, "trade_engine.domain.instruments", "Side", f.side.value())?)?;
    kwargs.set_item("fee", sl::decimal_py(py, &f.fee)?)?;
    kwargs.set_item("venue_order_id", &f.venue_order_id)?;
    kwargs.set_item("venue_execution_id", &f.venue_execution_id)?;
    Ok(cls.call((), Some(&kwargs))?.unbind())
}

#[pyfunction]
#[pyo3(signature = (fill, ticket, already_filled=None))]
fn tos_slippage_allocate_venue_fill(
    py: Python<'_>,
    fill: &Bound<'_, PyAny>,
    ticket: &Bound<'_, PyAny>,
    already_filled: Option<&Bound<'_, PyAny>>,
) -> PyResult<Vec<PyObject>> {
    let fill_instrument = fill.getattr("instrument")?;
    let fill_at = fill.getattr("filled_at")?;
    let fill = venue_fill_of(fill)?;
    let ticket = venue_ticket_of(ticket)?;
    let mut prior = Vec::new();
    if let Some(v) = already_filled {
        for item in v.call_method0("items")?.try_iter()? {
            let (k, q): (Bound<'_, PyAny>, Bound<'_, PyAny>) = item?.extract()?;
            prior.push((k.extract()?, sl::money(&q)?));
        }
    }
    let fills = slippage::allocate_venue_fill_raw(&fill, &ticket, &prior).map_err(refuse)?;
    fills.iter().map(|f| fill_py(py, f, &fill_instrument, &fill_at)).collect()
}

fn sim_fills_of(value: &Bound<'_, PyAny>) -> PyResult<Vec<slippage::SimFill>> {
    let mut out = Vec::new();
    for item in value.try_iter()? {
        let item = item?;
        out.push(slippage::SimFill {
            fill_id: item.getattr("fill_id")?.extract()?,
            order_id: item.getattr("order_id")?.extract()?,
            instrument: sl::instrument_of(&item.getattr("instrument")?)?,
            side: side_of(&enum_value(&item.getattr("side")?)?)?,
            quantity: sl::money(&item.getattr("quantity")?)?,
            price: sl::money(&item.getattr("price")?)?,
        });
    }
    Ok(out)
}

#[pyfunction]
fn tos_slippage_report(
    py: Python<'_>,
    venue: &str,
    as_of: &Bound<'_, PyAny>,
    sim_fills: &Bound<'_, PyAny>,
    venue_fills: &Bound<'_, PyAny>,
) -> PyResult<PyObject> {
    let as_of_text = isoformat(as_of)?;
    let r = slippage::slippage_report_raw(venue, &as_of_text, &sim_fills_of(sim_fills)?, &sim_fills_of(venue_fills)?)
        .map_err(refuse)?;
    let pair_cls = py.import("trade_engine.tos_paper.slippage")?.getattr("SlippagePair")?;
    let report_cls = py.import("trade_engine.tos_paper.slippage")?.getattr("SlippageReport")?;
    let mut pair_items = Vec::new();
    for p in &r.pairs {
        let bps = match &p.slippage_bps {
            Some(b) => sl::decimal_py(py, b)?,
            None => py.None().into_bound(py),
        };
        let kwargs = PyDict::new(py);
        kwargs.set_item("order_id", &p.order_id)?;
        kwargs.set_item("instrument", &p.instrument)?;
        kwargs.set_item("side", enum_py(py, "trade_engine.domain.instruments", "Side", p.side.value())?)?;
        kwargs.set_item("quantity", sl::decimal_py(py, &p.quantity)?)?;
        kwargs.set_item("sim_price", sl::decimal_py(py, &p.sim_price)?)?;
        kwargs.set_item("venue_price", sl::decimal_py(py, &p.venue_price)?)?;
        kwargs.set_item("slippage_points", sl::decimal_py(py, &p.slippage_points)?)?;
        kwargs.set_item("slippage_bps", bps)?;
        pair_items.push(pair_cls.call((), Some(&kwargs))?);
    }
    let pairs = PyTuple::new(py, pair_items)?;
    let refused = PyTuple::new(py, r.refused.iter().map(|(a, b)| (a.as_str(), b.as_str())))?;
    let mean = match &r.mean_slippage_bps {
        Some(m) => sl::decimal_py(py, m)?,
        None => py.None().into_bound(py),
    };
    let kwargs = PyDict::new(py);
    kwargs.set_item("venue", &r.venue)?;
    kwargs.set_item("as_of", as_of)?;
    kwargs.set_item("pairs", pairs)?;
    kwargs.set_item("unmatched_sim", PyTuple::new(py, r.unmatched_sim.iter().map(|s| s.as_str()))?)?;
    kwargs.set_item("unmatched_venue", PyTuple::new(py, r.unmatched_venue.iter().map(|s| s.as_str()))?)?;
    kwargs.set_item("refused", refused)?;
    kwargs.set_item("mean_slippage_bps", mean)?;
    Ok(report_cls.call((), Some(&kwargs))?.unbind())
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(tos_cover_covers, m)?)?;
    m.add_function(wrap_pyfunction!(tos_cover_uncovered, m)?)?;
    m.add_function(wrap_pyfunction!(tos_cover_holdings, m)?)?;
    m.add_function(wrap_pyfunction!(tos_cover_cover_reason, m)?)?;
    m.add_function(wrap_pyfunction!(tos_netting_ticket_key, m)?)?;
    m.add_function(wrap_pyfunction!(tos_netting_vertical_reason, m)?)?;
    m.add_function(wrap_pyfunction!(tos_netting_net_strategy_orders, m)?)?;
    m.add_function(wrap_pyfunction!(tos_netting_account_for_everything, m)?)?;
    m.add_function(wrap_pyfunction!(tos_reconcile_position_book, m)?)?;
    m.add_function(wrap_pyfunction!(tos_reconcile_reconcile, m)?)?;
    m.add_function(wrap_pyfunction!(tos_reconcile_unreadable, m)?)?;
    m.add_function(wrap_pyfunction!(tos_reconcile_confirm_ticket, m)?)?;
    m.add_function(wrap_pyfunction!(tos_reconcile_ticket_contracts, m)?)?;
    m.add_function(wrap_pyfunction!(tos_normalize_place_result, m)?)?;
    m.add_function(wrap_pyfunction!(tos_normalize_place_exception, m)?)?;
    m.add_function(wrap_pyfunction!(tos_normalize_placed_order_id, m)?)?;
    m.add_function(wrap_pyfunction!(tos_normalize_cancel_result, m)?)?;
    m.add_function(wrap_pyfunction!(tos_normalize_cancel_exception, m)?)?;
    m.add_function(wrap_pyfunction!(tos_normalize_working_order, m)?)?;
    m.add_function(wrap_pyfunction!(tos_normalize_book_state, m)?)?;
    m.add_function(wrap_pyfunction!(tos_normalize_order_fill, m)?)?;
    m.add_function(wrap_pyfunction!(tos_normalize_position, m)?)?;
    m.add_function(wrap_pyfunction!(tos_slippage_allocate_venue_fill, m)?)?;
    m.add_function(wrap_pyfunction!(tos_slippage_report, m)?)?;
    Ok(())
}

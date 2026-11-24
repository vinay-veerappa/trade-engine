//! W6-F0b-2: the typed doors the `tos_paper` planning modules (`exits`, `follow`,
//! `transport`) call instead of `_rs.decide(op, doc)`. Arguments are Python carriers
//! (`Instruments`, `Decimals`, `str`s, `datetime`s, `Orders`, `AccountState`/`MirrorState`,
//! tickets) converted with `store_ledger`; results are Python objects. There is no
//! JSON-string round trip here, and no `_rs.decide` in the Python modules.
//!
//! A refusal crosses as `ValueError((kind, message))` for the module's own `_rs.call` to
//! map, exactly as `tos_paper_decide` did.

use std::collections::HashSet;

use pyo3::prelude::*;
use pyo3::types::{PyBool, PyFloat, PyInt, PyString};
use te_core::ledger::fold::AccountState;
use te_core::ledger::model as lm;
use te_core::ledger::model::{parse_datetime, DateTime, LErr};
use te_core::ledger::ops::OMap;
use te_core::money::Money;
use te_core::tos_paper::exits::{self, Ctx, Exit, XOrder};
use te_core::tos_paper::follow;
use te_core::tos_paper::transport;
use te_core::tos_paper::wire;

use crate::sim::refuse;
use crate::store_ledger as sl;

/// `_rs.call`'s mapping: a `LErr` refusal as `ValueError((kind, message))`.
fn dt_of(value: &Bound<'_, PyAny>) -> PyResult<DateTime> {
    let text: String = value.call_method0("isoformat")?.extract()?;
    let (t, _) = parse_datetime(&text).map_err(refuse)?;
    Ok(t)
}

/// `follow`'s door check: an unaware datetime is refused with the door's own text.
fn aware_dt(value: &Bound<'_, PyAny>, key: &str) -> PyResult<DateTime> {
    let text: String = value.call_method0("isoformat")?.extract()?;
    let (t, is_aware) = parse_datetime(&text).map_err(refuse)?;
    if !is_aware {
        return Err(refuse(LErr { kind: "tos_wire", msg: format!("{key} must be timezone-aware") }));
    }
    Ok(t)
}

/// The pass's `session` (`date.isoformat()`), `Ctx`'s spelling.
fn session_of(value: &Bound<'_, PyAny>) -> PyResult<String> {
    value.call_method0("isoformat")?.extract()
}

// -- exits -------------------------------------------------------------------------------------

/// `close_id`.
#[pyfunction]
fn tos_exits_close_id(account: &str, contract: &Bound<'_, PyAny>, session: &str, name: &str) -> PyResult<String> {
    let contract = sl::instrument_of(contract)?;
    exits::close_id(account, &contract, session, name).map_err(refuse)
}

/// `target_id`.
#[pyfunction]
fn tos_exits_target_id(order_id: &str, session: &str) -> PyResult<String> {
    Ok(exits::target_id(order_id, session))
}

/// `plan_exits` after the pass name is accepted: the venue's exits, fed the host's quotes.
#[pyfunction]
#[pyo3(signature = (mirror, accounts, mirrored, session, name, at, price))]
fn tos_exits_plan(
    mirror: &Bound<'_, PyAny>,
    accounts: Vec<(String, Py<PyAny>)>,
    mirrored: Vec<String>,
    session: &Bound<'_, PyAny>,
    name: &str,
    at: &Bound<'_, PyAny>,
    price: &Bound<'_, PyAny>,
) -> PyResult<(Vec<String>, Vec<Py<PyAny>>, Vec<(String, String, String)>, Vec<(String, String)>)> {
    let py = mirror.py();
    let mirror = sl::mirror_of(mirror)?;
    let mut amap: OMap<String, AccountState> = OMap::new();
    for (account, state) in &accounts {
        amap.insert(account.clone(), account.clone(), sl::account_of(state.bind(py))?);
    }
    let session_str = session_of(session)?;
    let at_text: String = at.call_method0("isoformat")?.extract()?;
    let (at_dt, aware) = parse_datetime(&at_text).map_err(refuse)?;

    // The planning loop: run the plan, fetch the first quote the plan has not been answered
    // for, in order, until it needs nothing more. The number and order of `price` calls is
    // exactly the old Python loop's.
    let mut prices: Vec<(String, lm::Side, Option<Money>)> = Vec::new();
    let mut seen: HashSet<(String, lm::Side)> = HashSet::new();
    loop {
        let ctx = Ctx {
            mirror: mirror.clone(),
            accounts: amap.clone(),
            empty: AccountState::new(""),
            mirrored: mirrored.clone(),
            session: session_str.clone(),
            prices: prices.clone(),
            at: at_text.clone(),
            aware,
        };
        let out = exits::plan_exits(&ctx, name).map_err(refuse)?;
        let need = out.priced.iter().find(|(contract, side)| !seen.contains(&(contract.hk(), *side)));
        let Some((contract, side)) = need else {
            return Ok(convert_exit(py, &out, &at_dt)?);
        };
        let key = (contract.hk(), *side);
        seen.insert(key.clone());
        let contract_py = sl::instrument_py(py, contract)?;
        let side_py = sl::side_py(py, *side)?;
        let quote = price.call1((contract_py, side_py))?;
        let money = if quote.is_none() { None } else { Some(sl::money(&quote)?) };
        prices.push((key.0, *side, money));
    }
}

fn convert_exit(
    py: Python<'_>,
    out: &Exit,
    at: &DateTime,
) -> PyResult<(Vec<String>, Vec<Py<PyAny>>, Vec<(String, String, String)>, Vec<(String, String)>)> {
    let mut orders = Vec::with_capacity(out.orders.len());
    for x in &out.orders {
        orders.push(xorder_py(py, x, at)?);
    }
    Ok((out.cancel.clone(), orders, out.refused.clone(), out.waits.clone()))
}

/// An exit order as a Python `Order` (a LIMIT DAY order of the pass).
fn xorder_py(py: Python<'_>, x: &XOrder, at: &DateTime) -> PyResult<Py<PyAny>> {
    let order = lm::Order {
        order_id: x.order_id.clone(),
        account_id: x.account_id.clone(),
        instrument: x.instrument.clone(),
        order_type: lm::OrderType::Limit,
        side: x.side,
        quantity: x.quantity.clone(),
        command_id: x.order_id.clone(),
        created_at: at.clone(),
        limit_price: x.limit_price.clone(),
        stop_price: None,
        trail_amount: None,
        tif: lm::Tif::Day,
        state: lm::OrderState::New,
        parent_order_id: None,
        oco_group: None,
    };
    Ok(sl::obj_py(py, &lm::Obj::Order(order))?.unbind())
}

// -- follow ------------------------------------------------------------------------------------

/// `follow_entries`.
#[pyfunction]
#[pyo3(signature = (mirror, accounts, mirrored, session_open, now, max_age_us))]
fn tos_follow_entries(
    mirror: &Bound<'_, PyAny>,
    accounts: Vec<(String, Py<PyAny>)>,
    mirrored: Vec<String>,
    session_open: &Bound<'_, PyAny>,
    now: &Bound<'_, PyAny>,
    max_age_us: i128,
) -> PyResult<(Vec<(String, String)>, Vec<(String, String, String)>)> {
    let py = mirror.py();
    let mirror = sl::mirror_of(mirror)?;
    let mut amap: OMap<String, AccountState> = OMap::new();
    for (account, state) in &accounts {
        amap.insert(account.clone(), account.clone(), sl::account_of(state.bind(py))?);
    }
    let session_open = aware_dt(session_open, "session_open")?;
    let now = aware_dt(now, "now")?;
    let out = follow::follow_entries(&mirror, &amap, &mirrored, &session_open, &now, max_age_us).map_err(refuse)?;
    Ok((out.send, out.refused))
}

/// `_flat`: the sim holds none of `order`'s instrument.
#[pyfunction]
fn tos_follow_flat(state: &Bound<'_, PyAny>, order: &Bound<'_, PyAny>) -> PyResult<bool> {
    let state = sl::account_of(state)?;
    let lm::Obj::Order(order) = sl::payload(order)? else {
        return Err(refuse(LErr { kind: "value", msg: "expected an Order".into() }));
    };
    follow::flat(&state, &order).map_err(refuse)
}

/// `pass_name`: `follow-HHMM` in New York.
#[pyfunction]
fn tos_follow_pass_name(now: &Bound<'_, PyAny>) -> PyResult<String> {
    follow::pass_name(&dt_of(now)?).map_err(refuse)
}

// -- transport ---------------------------------------------------------------------------------

/// A Python value as a validator's [`transport::Arg`]: the `repr` of anything that is not a
/// `str` or a (non-bool) `int` is computed here, so the refusal text reads as Python does.
fn arg_of_py(value: &Bound<'_, PyAny>) -> PyResult<transport::Arg> {
    if value.is_instance_of::<PyBool>() {
        return Ok(transport::Arg::Other(if value.extract::<bool>()? { "True".into() } else { "False".into() }));
    }
    if value.is_instance_of::<PyInt>() {
        if let Ok(n) = value.extract::<i128>() {
            return Ok(transport::Arg::Int(n));
        }
        return Ok(transport::Arg::BigInt(value.str()?.extract::<String>()?));
    }
    if value.is_instance_of::<PyString>() {
        return Ok(transport::Arg::Str(value.extract::<String>()?));
    }
    if value.is_instance_of::<PyFloat>() {
        return Ok(transport::Arg::Other(value.repr()?.extract::<String>()?));
    }
    if value.is_none() {
        return Ok(transport::Arg::Other("None".into()));
    }
    Ok(transport::Arg::Other(value.repr()?.extract::<String>()?))
}

fn opt_money(value: Option<&Bound<'_, PyAny>>) -> PyResult<Option<Money>> {
    value.map(sl::money).transpose()
}

/// The `MirrorTicket` validation (a single option ticket).
#[pyfunction]
#[pyo3(signature = (side, quantity, order_type, tif, limit_price))]
fn tos_transport_validate_option(
    side: &Bound<'_, PyAny>,
    quantity: &Bound<'_, PyAny>,
    order_type: &Bound<'_, PyAny>,
    tif: &Bound<'_, PyAny>,
    limit_price: Option<&Bound<'_, PyAny>>,
) -> PyResult<()> {
    let limit = opt_money(limit_price)?;
    transport::validate_single(
        &arg_of_py(side)?,
        &arg_of_py(quantity)?,
        &arg_of_py(order_type)?,
        &arg_of_py(tif)?,
        &limit,
    )
    .map_err(refuse)
}

/// The `MirrorStockTicket` validation.
#[pyfunction]
#[pyo3(signature = (symbol, side, quantity, order_type, tif, limit_price))]
fn tos_transport_validate_stock(
    symbol: &Bound<'_, PyAny>,
    side: &Bound<'_, PyAny>,
    quantity: &Bound<'_, PyAny>,
    order_type: &Bound<'_, PyAny>,
    tif: &Bound<'_, PyAny>,
    limit_price: Option<&Bound<'_, PyAny>>,
) -> PyResult<()> {
    let limit = opt_money(limit_price)?;
    transport::validate_stock(
        &arg_of_py(symbol)?,
        &arg_of_py(side)?,
        &arg_of_py(quantity)?,
        &arg_of_py(order_type)?,
        &arg_of_py(tif)?,
        &limit,
    )
    .map_err(refuse)
}

/// The `MirrorComboLeg` validation.
#[pyfunction]
fn tos_transport_validate_leg(side: &Bound<'_, PyAny>, ratio: &Bound<'_, PyAny>) -> PyResult<()> {
    transport::validate_leg(&arg_of_py(side)?, &arg_of_py(ratio)?).map_err(refuse)
}

/// The `MirrorComboTicket` validation.
#[pyfunction]
#[pyo3(signature = (legs, quantity, order_type, limit_price, effect, tif))]
fn tos_transport_validate_combo(
    legs: Vec<(Bound<'_, PyAny>, Bound<'_, PyAny>)>,
    quantity: &Bound<'_, PyAny>,
    order_type: &Bound<'_, PyAny>,
    limit_price: Option<&Bound<'_, PyAny>>,
    effect: &Bound<'_, PyAny>,
    tif: &Bound<'_, PyAny>,
) -> PyResult<()> {
    let mut pairs = Vec::with_capacity(legs.len());
    for (side, ratio) in &legs {
        pairs.push((arg_of_py(side)?, arg_of_py(ratio)?));
    }
    let limit = opt_money(limit_price)?;
    transport::validate_combo(&pairs, &arg_of_py(quantity)?, &arg_of_py(order_type)?, &limit, &arg_of_py(effect)?, &arg_of_py(tif)?)
        .map_err(refuse)
}

/// A Python instrument as the mirror's wire instrument (an unmirrorable future keeps Python's
/// own `repr`).
fn wire_instrument_of(value: &Bound<'_, PyAny>) -> PyResult<wire::Instrument> {
    let inst = sl::instrument_of(value)?;
    Ok(match inst {
        lm::Instrument::Equity(symbol) => wire::Instrument::Equity { symbol },
        lm::Instrument::Option(c) => wire::Instrument::Option(wire::OptionC {
            underlying: c.underlying,
            expiry: lm::date_iso(&c.expiry),
            strike: c.strike,
            right: c.right,
            multiplier: c.multiplier,
        }),
        lm::Instrument::Combo(legs) => wire::Instrument::Combo(
            legs.iter()
                .map(|l| wire::ComboLeg {
                    contract: wire_instrument_lm(&l.contract),
                    ratio: l.ratio,
                    side: l.side.value().to_string(),
                })
                .collect(),
        ),
        lm::Instrument::Future(_) => wire::Instrument::Other { repr: value.repr()?.extract::<String>()? },
    })
}

fn wire_instrument_lm(inst: &lm::Instrument) -> wire::Instrument {
    match inst {
        lm::Instrument::Equity(symbol) => wire::Instrument::Equity { symbol: symbol.clone() },
        lm::Instrument::Option(c) => wire::Instrument::Option(wire::OptionC {
            underlying: c.underlying.clone(),
            expiry: lm::date_iso(&c.expiry),
            strike: c.strike.clone(),
            right: c.right,
            multiplier: c.multiplier,
        }),
        lm::Instrument::Combo(legs) => wire::Instrument::Combo(
            legs.iter()
                .map(|l| wire::ComboLeg {
                    contract: wire_instrument_lm(&l.contract),
                    ratio: l.ratio,
                    side: l.side.value().to_string(),
                })
                .collect(),
        ),
        lm::Instrument::Future(f) => wire::Instrument::Other { repr: f.root.clone() },
    }
}

/// `ticket_for`: a venue order's fields -> the ticket as a Python mapping (`ticket_of` builds
/// the carrier, re-running its `__post_init__` validation).
#[pyfunction]
#[pyo3(signature = (instrument, order_type, tif, quantity, side, limit_price))]
fn tos_transport_ticket_for(
    instrument: &Bound<'_, PyAny>,
    order_type: &str,
    tif: &str,
    quantity: &Bound<'_, PyAny>,
    side: &str,
    limit_price: Option<&Bound<'_, PyAny>>,
) -> PyResult<Py<PyAny>> {
    let py = instrument.py();
    let inst = wire_instrument_of(instrument)?;
    let quantity = sl::money(quantity)?;
    let limit = opt_money(limit_price)?;
    let ticket = transport::ticket_for_typed(&inst, order_type, tif, &quantity, side, limit.as_ref()).map_err(refuse)?;
    Ok(py.import("json")?.call_method1("loads", (te_core::ledger::json::dumps(&ticket),))?.unbind())
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(tos_exits_close_id, m)?)?;
    m.add_function(wrap_pyfunction!(tos_exits_target_id, m)?)?;
    m.add_function(wrap_pyfunction!(tos_exits_plan, m)?)?;
    m.add_function(wrap_pyfunction!(tos_follow_entries, m)?)?;
    m.add_function(wrap_pyfunction!(tos_follow_flat, m)?)?;
    m.add_function(wrap_pyfunction!(tos_follow_pass_name, m)?)?;
    m.add_function(wrap_pyfunction!(tos_transport_ticket_for, m)?)?;
    m.add_function(wrap_pyfunction!(tos_transport_validate_option, m)?)?;
    m.add_function(wrap_pyfunction!(tos_transport_validate_stock, m)?)?;
    m.add_function(wrap_pyfunction!(tos_transport_validate_leg, m)?)?;
    m.add_function(wrap_pyfunction!(tos_transport_validate_combo, m)?)?;
    Ok(())
}

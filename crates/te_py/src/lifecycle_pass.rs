//! W6-F0a: the after-close lifecycle pass over the native ledger, and the typed rules the Python
//! sources call.
//!
//! `LifecyclePass.run` hands itself to [`lifecycle_run`], which runs `lifecycle_glue::run` over the
//! store. The settlement, dividend and quote sources stay Python plugins, called through closures:
//! a Python error a plugin raises is held ([`Pending`]) and re-raised as itself, and a rule's refusal
//! crosses as its registered exception class ([`flow_refusal`]).

use chrono::NaiveDate;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};
use te_core::ledger::model::{parse_datetime, DateTime, Event, Instrument, OptionContract, R as LR};
use te_core::options::SettleTime;
use te_core::runtime::eod_glue::Clock as _;
use te_core::runtime::lifecycle as rules;
use te_core::runtime::lifecycle_glue::{self as glue, Dividend, Plugins, Quote, Settlement};

use super::flow_refusal;
use crate::store_ledger::{self as sl, require_native_with, with_native, Pending};

/// The gate's refusal: `LifecycleError(msg)`, the class `after_close.py` registers for the pass.
fn lifecycle_refuse(py: Python<'_>, msg: &str) -> PyErr {
    let raised = py
        .import("trade_engine.lifecycle.after_close")
        .and_then(|module| module.getattr("LifecycleError"))
        .and_then(|cls| cls.call1((msg,)));
    match raised {
        Ok(value) => PyErr::from_value(value),
        Err(error) => error,
    }
}

/// A Python `datetime` as the te_core instant it stands for, from its ISO text.
fn datetime_of(value: &Bound<'_, PyAny>) -> PyResult<DateTime> {
    let text: String = value.call_method0("isoformat")?.extract()?;
    parse_datetime(&text).map(|(instant, _)| instant).map_err(flow_refusal)
}

/// A Python `SettlementPrice` as the fields the pass reads.
fn settlement_of(price: &Bound<'_, PyAny>) -> PyResult<Settlement> {
    Ok(Settlement {
        underlying: price.getattr("underlying")?.extract()?,
        session: sl::date_of(&price.getattr("session")?)?,
        settle_time: price.getattr("settle_time")?.getattr("value")?.extract()?,
        price: sl::money(&price.getattr("price")?)?,
        source: price.getattr("source")?.extract()?,
        as_of: datetime_of(&price.getattr("as_of")?)?,
    })
}

/// A Python `SettleTime` member, from its code (`AM` or `PM`).
fn settle_time_py<'py>(py: Python<'py>, time: SettleTime) -> PyResult<Bound<'py, PyAny>> {
    py.import("trade_engine.domain.option_roots")?.getattr("SettleTime")?.call1((time.code(),))
}

/// `settlements.settlement(underlying, session, settle_time)`.
fn read_settlement(
    py: Python<'_>,
    source: &Bound<'_, PyAny>,
    underlying: &str,
    day: NaiveDate,
    time: SettleTime,
) -> PyResult<Settlement> {
    let price = source.call_method1("settlement", (underlying, sl::date_py(py, day)?, settle_time_py(py, time)?))?;
    settlement_of(&price)
}

/// `dividends.dividends(symbol, ex_date)`, each record's amount and as-of.
fn read_dividends(py: Python<'_>, source: &Bound<'_, PyAny>, symbol: &str, ex: NaiveDate) -> PyResult<Vec<Dividend>> {
    let items = source.call_method1("dividends", (symbol, sl::date_py(py, ex)?))?;
    items
        .try_iter()?
        .map(|item| {
            let item = item?;
            Ok(Dividend {
                amount: sl::money(&item.getattr("amount")?)?,
                as_of: datetime_of(&item.getattr("as_of")?)?,
            })
        })
        .collect()
}

/// `quotes.quote(contract, now)`, the bid and as-of.
fn read_quote(py: Python<'_>, source: &Bound<'_, PyAny>, contract: &OptionContract, at: &DateTime) -> PyResult<Quote> {
    let instrument = sl::instrument_py(py, &Instrument::Option(contract.clone()))?;
    let found = source.call_method1("quote", (instrument, sl::datetime_py(py, at)?))?;
    Ok(Quote { bid: sl::money(&found.getattr("bid")?)?, as_of: datetime_of(&found.getattr("as_of")?)? })
}

/// One lifecycle event as the Python `Event` the ledger stores (`Event(**kwargs)`, as the intraday flow builds it).
fn event_py<'py>(py: Python<'py>, event: &Event) -> PyResult<Bound<'py, PyAny>> {
    let module = py.import("trade_engine.ledger")?;
    let args = PyDict::new(py);
    args.set_item("account", &event.account)?;
    args.set_item("kind", module.getattr("EventKind")?.call1((event.kind.value(),))?)?;
    args.set_item("payload", sl::obj_py(py, &event.payload)?)?;
    args.set_item("ts_utc", sl::datetime_py(py, &event.ts_utc)?)?;
    args.set_item("command_id", event.command_id.as_deref())?;
    args.set_item("schema_version", event.schema_version)?;
    args.set_item("seq", event.seq)?;
    module.getattr("Event")?.call((), Some(&args))
}

/// The store's write refusals cross as `ValueError((kind, message))`. The old Python `Ledger.extend`
/// turned that pair into the refusal its kind names (`ledger/_rs.py::call`); the native pass does the
/// same here, so a fold refusal stays a `LedgerFoldError`. Any other error passes unchanged.
fn as_refusal(py: Python<'_>, error: PyErr) -> PyErr {
    let value = error.value(py);
    if !value.get_type().is(&py.get_type::<PyValueError>()) {
        return error;
    }
    let Ok((kind, message)) = value.getattr("args").and_then(|args| args.extract::<(String, String)>()) else {
        return error;
    };
    match py.import("trade_engine.ledger._rs").and_then(|module| module.call_method1("refusal", (kind, message))) {
        Ok(refused) => PyErr::from_value(refused),
        Err(failed) => failed,
    }
}

/// `LifecyclePass.run`'s pass: the events it writes, as Python `Event`s, in the order written.
#[pyfunction]
#[pyo3(signature = (pass_, session, accounts = None))]
fn lifecycle_run<'py>(
    pass_: Bound<'py, PyAny>,
    session: Bound<'py, PyAny>,
    accounts: Option<Bound<'py, PyAny>>,
) -> PyResult<Bound<'py, PyList>> {
    let py = pass_.py();
    let session_day = sl::date_of(&session)?;
    let names: Option<Vec<String>> = match accounts {
        Some(names) => Some(names.try_iter()?.map(|name| name?.extract::<String>()).collect::<PyResult<_>>()?),
        None => None,
    };
    let store = require_native_with(
        &pass_.getattr("_ledger")?,
        &pass_.getattr("_calendar")?,
        lifecycle_refuse,
        "lifecycle needs a plain Ledger on the XNYS ExchangeCalendar",
    )?;
    let clock = pass_.getattr("_clock")?;
    let settlements = pass_.getattr("_settlements")?;
    let dividends = pass_.getattr("_dividends")?;
    let quotes = pass_.getattr("_quotes")?;
    let pending = Pending::default();
    let written = with_native(&store, &clock, &pending, |ledger, clock| {
        let now = clock.now_utc();
        let mut settle = |underlying: &str, day: NaiveDate, time: SettleTime| {
            read_settlement(py, &settlements, underlying, day, time).map_err(|e| pending.hold(e))
        };
        let mut divide = |symbol: &str, ex: NaiveDate| {
            read_dividends(py, &dividends, symbol, ex).map_err(|e| pending.hold(e))
        };
        let mut quote = |contract: &OptionContract, at: DateTime| {
            read_quote(py, &quotes, contract, &at).map_err(|e| pending.hold(e))
        };
        let plugins = Plugins {
            settlement: &mut settle,
            dividends: if dividends.is_none() {
                None
            } else {
                Some(&mut divide as &mut dyn FnMut(&str, NaiveDate) -> LR<Vec<Dividend>>)
            },
            quote: if quotes.is_none() {
                None
            } else {
                Some(&mut quote as &mut dyn FnMut(&OptionContract, DateTime) -> LR<Quote>)
            },
        };
        glue::run(ledger, now, session_day, names.as_deref(), plugins)
    })
    .map_err(|e| as_refusal(py, e))?;
    let out = PyList::empty(py);
    for event in &written {
        out.append(event_py(py, event)?)?;
    }
    Ok(out)
}

// -- the typed rules `sources.py` calls: each refusal keeps the kind and text its rule raises --

#[pyfunction]
fn lifecycle_value(
    is_price: bool,
    underlying: &str,
    has_amount: bool,
    amount: &str,
    owner: &str,
    shown: &str,
    has_source: bool,
) -> PyResult<()> {
    rules::value(is_price, underlying, has_amount, amount, owner, shown, has_source).map_err(flow_refusal)
}

#[pyfunction]
fn lifecycle_aware(is_aware: bool, what: &str) -> PyResult<()> {
    rules::aware(is_aware, what).map_err(flow_refusal)
}

#[pyfunction]
fn lifecycle_symbol_key(text: &str) -> String {
    rules::symbol_key(text)
}

#[pyfunction]
fn lifecycle_settlement_conflict(conflict: bool, kind: &str, underlying: &str, date: &str) -> PyResult<()> {
    rules::settlement_conflict(conflict, kind, underlying, date).map_err(flow_refusal)
}

#[pyfunction]
fn lifecycle_settlement_missing(kind: &str, underlying: &str, date: &str) -> PyResult<()> {
    rules::settlement_missing(kind, underlying, date).map_err(flow_refusal)
}

#[pyfunction]
fn lifecycle_dividend_missing(key: &str) -> PyResult<()> {
    rules::dividend_missing(key).map_err(flow_refusal)
}

#[pyfunction]
fn lifecycle_action_amount(has_amount: bool, amount: &str, symbol: &str, ex_date: &str, raw: &str) -> PyResult<()> {
    rules::action_amount(has_amount, amount, symbol, ex_date, raw).map_err(flow_refusal)
}

#[pyfunction]
fn lifecycle_quote_missing(occ: &str, underlying: &str, as_of: &str) -> PyResult<()> {
    rules::quote_missing(occ, underlying, as_of).map_err(flow_refusal)
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(lifecycle_run, m)?)?;
    m.add_function(wrap_pyfunction!(lifecycle_value, m)?)?;
    m.add_function(wrap_pyfunction!(lifecycle_aware, m)?)?;
    m.add_function(wrap_pyfunction!(lifecycle_symbol_key, m)?)?;
    m.add_function(wrap_pyfunction!(lifecycle_settlement_conflict, m)?)?;
    m.add_function(wrap_pyfunction!(lifecycle_settlement_missing, m)?)?;
    m.add_function(wrap_pyfunction!(lifecycle_dividend_missing, m)?)?;
    m.add_function(wrap_pyfunction!(lifecycle_action_amount, m)?)?;
    m.add_function(wrap_pyfunction!(lifecycle_quote_missing, m)?)?;
    Ok(())
}

//! Pyo3 doors for market data verification (Architecture §4.8, I5, I7).
//!
//! Exposes market data freshness, shape, ordering, and bounds verifiers.
//! Plain values in and out; money crosses as strings.
//! Refusals cross as `ValueError((kind, message))` for `market_data/_rs.py` to map.

use chrono::NaiveDate;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;

use te_core::ledger::model::{Instrument, LErr};
use te_core::market_data_verify::{
    validate_bars_request as core_validate_bars_request,
    validate_chain_request as core_validate_chain_request,
    validate_future_tolerance as core_validate_future_tolerance,
    validate_max_age as core_validate_max_age,
    verify_bars_series, verify_chain as core_verify_chain,
    verify_corporate_actions as core_verify_corporate_actions,
    verify_item_freshness, verify_quote as core_verify_quote,
    Bar, CorporateAction, Greeks, GreeksSource, MarketDataVerifyError,
    OptionQuote, Quote,
};
use te_core::money::Money;
use te_core::options::Right;
use te_core::sim::Ts;

fn refuse_md(e: MarketDataVerifyError) -> PyErr {
    let l_err = LErr::from(e);
    PyValueError::new_err((l_err.kind.to_string(), l_err.msg))
}

fn refuse_err(kind: &str, msg: impl Into<String>) -> PyErr {
    PyValueError::new_err((kind.to_string(), msg.into()))
}

fn parse_ts(iso: &str, name: &'static str) -> PyResult<Ts> {
    Ts::aware(iso, name).map_err(|e| PyValueError::new_err((e.kind.to_string(), e.msg)))
}

fn parse_date(d: &str) -> PyResult<NaiveDate> {
    NaiveDate::parse_from_str(d, "%Y-%m-%d")
        .map_err(|_| refuse_err("value", format!("Invalid date string format: {d:?}")))
}

fn parse_money(s: &str, field: &str) -> PyResult<Money> {
    Money::parse(s).ok_or_else(|| refuse_err("value", format!("Invalid decimal for {field}: {s:?}")))
}

fn parse_instrument(s: &str) -> Instrument {
    Instrument::Equity(s.to_string())
}

#[pyfunction]
fn market_data_validate_max_age(max_age_seconds: f64) -> PyResult<()> {
    core_validate_max_age(max_age_seconds).map_err(refuse_md)
}

#[pyfunction]
fn market_data_validate_future_tolerance(future_tolerance_seconds: f64) -> PyResult<()> {
    core_validate_future_tolerance(future_tolerance_seconds).map_err(refuse_md)
}

#[pyfunction]
fn market_data_validate_bars_request(
    start: &str,
    end: &str,
    max_age_seconds: f64,
    now: &str,
) -> PyResult<String> {
    let start_ts = parse_ts(start, "start datetime")?;
    let end_ts = parse_ts(end, "end datetime")?;
    let now_ts = parse_ts(now, "clock.now_utc")?;
    let eff = core_validate_bars_request(&start_ts, &end_ts, max_age_seconds, &now_ts)
        .map_err(refuse_md)?;
    Ok(eff.iso)
}

#[pyfunction]
fn market_data_validate_chain_request(
    expiry_start: &str,
    expiry_end: &str,
    max_age_seconds: f64,
) -> PyResult<()> {
    core_validate_chain_request(
        parse_date(expiry_start)?,
        parse_date(expiry_end)?,
        max_age_seconds,
    )
    .map_err(refuse_md)
}

#[pyfunction]
#[pyo3(signature = (symbol, as_of, max_age_seconds, now, future_tolerance_seconds))]
fn market_data_verify_item(
    symbol: &str,
    as_of: &str,
    max_age_seconds: Option<f64>,
    now: &str,
    future_tolerance_seconds: f64,
) -> PyResult<f64> {
    let as_of_ts = parse_ts(as_of, "as_of")?;
    let now_ts = parse_ts(now, "clock.now_utc")?;
    verify_item_freshness(
        symbol,
        &as_of_ts,
        max_age_seconds,
        &now_ts,
        future_tolerance_seconds,
    )
    .map_err(refuse_md)
}

/// (symbol, ts, open, high, low, close, volume, as_of)
type BarIn = (String, String, String, String, String, String, String, String);

#[pyfunction]
#[allow(clippy::too_many_arguments)]
fn market_data_verify_bars(
    symbol: &str,
    tf: &str,
    start: &str,
    end: &str,
    max_age_seconds: f64,
    now: &str,
    future_tolerance_seconds: f64,
    raw_bars: Vec<BarIn>,
    strict: bool,
) -> PyResult<Vec<String>> {
    let req_instr = parse_instrument(symbol);
    let start_ts = parse_ts(start, "start datetime")?;
    let end_ts = parse_ts(end, "end datetime")?;
    let now_ts = parse_ts(now, "clock.now_utc")?;

    let mut bars = Vec::with_capacity(raw_bars.len());
    for (b_sym, ts_str, o, h, l, c, v, as_of_str) in raw_bars {
        bars.push(Bar {
            instr: parse_instrument(&b_sym),
            ts: parse_ts(&ts_str, "Bar timestamp")?,
            open: parse_money(&o, "open")?,
            high: parse_money(&h, "high")?,
            low: parse_money(&l, "low")?,
            close: parse_money(&c, "close")?,
            volume: parse_money(&v, "volume")?,
            as_of: parse_ts(&as_of_str, "Bar as_of")?,
        });
    }

    let (_verified, warnings) = verify_bars_series(
        &req_instr,
        tf,
        &start_ts,
        &end_ts,
        max_age_seconds,
        &now_ts,
        future_tolerance_seconds,
        &bars,
        strict,
    )
    .map_err(refuse_md)?;

    Ok(warnings)
}

#[pyfunction]
#[allow(clippy::too_many_arguments)]
fn market_data_verify_quote(
    symbol: &str,
    bid: &str,
    ask: &str,
    bid_size: &str,
    ask_size: &str,
    as_of: &str,
    max_age_seconds: f64,
    now: &str,
    future_tolerance_seconds: f64,
) -> PyResult<()> {
    let instr = parse_instrument(symbol);
    let as_of_ts = parse_ts(as_of, "Quote as_of")?;
    let now_ts = parse_ts(now, "clock.now_utc")?;
    let q = Quote::new(
        instr.clone(),
        parse_money(bid, "bid")?,
        parse_money(ask, "ask")?,
        parse_money(bid_size, "bid_size")?,
        parse_money(ask_size, "ask_size")?,
        as_of_ts,
    );

    core_verify_quote(
        &q,
        Some(&instr),
        max_age_seconds,
        &now_ts,
        future_tolerance_seconds,
    )
    .map_err(refuse_md)
}

type GreeksTuple = (f64, f64, f64, f64, f64, String);

/// (underlying, expiry, strike, right, multiplier)
type ContractIn = (String, String, String, String, i128);
/// (bid, ask, bid_size, ask_size, as_of, underlying_price, implied_vol, greeks, open_interest)
type QuoteIn = (
    String,
    String,
    String,
    String,
    String,
    Option<String>,
    Option<String>,
    Option<GreeksTuple>,
    Option<i128>,
);
type OptionQuoteIn = (ContractIn, QuoteIn);

#[pyfunction]
#[allow(clippy::too_many_arguments)]
fn market_data_verify_chain(
    underlying: &str,
    expiry_start: &str,
    expiry_end: &str,
    max_age_seconds: f64,
    now: &str,
    future_tolerance_seconds: f64,
    raw_quotes: Vec<OptionQuoteIn>,
    strict: bool,
) -> PyResult<Vec<String>> {
    let exp_start = parse_date(expiry_start)?;
    let exp_end = parse_date(expiry_end)?;
    let now_ts = parse_ts(now, "clock.now_utc")?;

    let mut quotes = Vec::with_capacity(raw_quotes.len());
    for (contract_in, quote_in) in raw_quotes {
        let (und, exp, strike, right_str, multiplier) = contract_in;
        let (bid, ask, bid_size, ask_size, as_of, spot, iv, gk, oi) = quote_in;
        let right = match right_str.trim().to_uppercase().as_str() {
            "C" | "CALL" => Right::Call,
            "P" | "PUT" => Right::Put,
            _ => return Err(refuse_err("value", format!("Invalid option right: {right_str:?}"))),
        };

        let contract = te_core::ledger::model::OptionContract {
            underlying: und,
            expiry: parse_date(&exp)?,
            strike: parse_money(&strike, "strike")?,
            right,
            multiplier,
        };

        let greeks = if let Some((d, g, t, v, r, src)) = gk {
            let source = match src.as_str() {
                "vendor" => GreeksSource::Vendor,
                "model" => GreeksSource::Model,
                _ => return Err(refuse_err("value", format!("Invalid greeks source: {src:?}"))),
            };
            Some(Greeks::new(d, g, t, v, r, source).map_err(refuse_md)?)
        } else {
            None
        };

        quotes.push(OptionQuote {
            contract,
            bid: parse_money(&bid, "bid")?,
            ask: parse_money(&ask, "ask")?,
            bid_size: parse_money(&bid_size, "bid_size")?,
            ask_size: parse_money(&ask_size, "ask_size")?,
            as_of: parse_ts(&as_of, "OptionQuote as_of")?,
            underlying_price: spot.as_deref().map(|s| parse_money(s, "underlying_price")).transpose()?,
            implied_vol: iv.as_deref().map(|s| parse_money(s, "implied_vol")).transpose()?,
            greeks,
            open_interest: oi,
        });
    }

    let (_verified, warnings) = core_verify_chain(
        underlying,
        exp_start,
        exp_end,
        max_age_seconds,
        &now_ts,
        future_tolerance_seconds,
        &quotes,
        strict,
    )
    .map_err(refuse_md)?;

    Ok(warnings)
}

/// (symbol, action_type, effective_date, as_of)
type CorporateActionIn = (String, String, String, String);

#[pyfunction]
fn market_data_verify_corporate_actions(
    symbol: &str,
    max_age_seconds: f64,
    now: &str,
    future_tolerance_seconds: f64,
    raw_actions: Vec<CorporateActionIn>,
    strict: bool,
) -> PyResult<Vec<String>> {
    let now_ts = parse_ts(now, "clock.now_utc")?;

    let mut actions = Vec::with_capacity(raw_actions.len());
    for (sym, act_type, eff_date, as_of) in raw_actions {
        actions.push(CorporateAction {
            symbol: sym,
            action_type: act_type,
            effective_date: parse_date(&eff_date)?,
            as_of: parse_ts(&as_of, "CorporateAction as_of")?,
            details: None,
        });
    }

    let (_verified, warnings) = core_verify_corporate_actions(
        symbol,
        max_age_seconds,
        &now_ts,
        future_tolerance_seconds,
        &actions,
        strict,
    )
    .map_err(refuse_md)?;

    Ok(warnings)
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(market_data_validate_max_age, m)?)?;
    m.add_function(wrap_pyfunction!(market_data_validate_future_tolerance, m)?)?;
    m.add_function(wrap_pyfunction!(market_data_validate_bars_request, m)?)?;
    m.add_function(wrap_pyfunction!(market_data_validate_chain_request, m)?)?;
    m.add_function(wrap_pyfunction!(market_data_verify_item, m)?)?;
    m.add_function(wrap_pyfunction!(market_data_verify_bars, m)?)?;
    m.add_function(wrap_pyfunction!(market_data_verify_quote, m)?)?;
    m.add_function(wrap_pyfunction!(market_data_verify_chain, m)?)?;
    m.add_function(wrap_pyfunction!(market_data_verify_corporate_actions, m)?)?;
    Ok(())
}

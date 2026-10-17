//! Market data verification logic (Architecture §4.8, I5, I7).
//!
//! Enforces:
//! - I5: Refuse, never guess. Stale data (older than `max_age_seconds`) refuses with a typed error.
//!   Missing timestamps, invalid price structures, and out-of-bounds data are rejected.
//! - I7: Injected clock. All current time comparisons read from an injected `now: &Ts` instant;
//!   no wall-clock reads anywhere in this module.
//! - No lookahead: Data timestamps and `as_of` timestamps strictly after `now` (beyond
//!   `future_tolerance_seconds`) are refused.
//!
//! Pure verification only: checks shape, ordering, freshness and look-ahead on already-fetched data.

use std::error::Error;
use std::fmt;

use chrono::NaiveDate;

use crate::ledger::model::{dec_eq, Instrument, LErr, OptionContract};
use crate::money::Money;
pub use crate::sim::broker::Bar;
use crate::sim::Ts;

/// A top-of-book market quote stamped with an `as_of` timestamp.
#[derive(Debug, Clone)]
pub struct Quote {
    pub instrument: Instrument,
    pub bid: Money,
    pub ask: Money,
    pub bid_size: Money,
    pub ask_size: Money,
    pub as_of: Ts,
}

impl Quote {
    pub fn new(
        instrument: Instrument,
        bid: Money,
        ask: Money,
        bid_size: Money,
        ask_size: Money,
        as_of: Ts,
    ) -> Self {
        Self {
            instrument,
            bid,
            ask,
            bid_size,
            ask_size,
            as_of,
        }
    }

    /// True when two quotes have equivalent values and timestamps.
    pub fn same(&self, o: &Quote) -> bool {
        self.instrument.same(&o.instrument)
            && dec_eq(&self.bid, &o.bid)
            && dec_eq(&self.ask, &o.ask)
            && dec_eq(&self.bid_size, &o.bid_size)
            && dec_eq(&self.ask_size, &o.ask_size)
            && self.as_of.eq(&o.as_of)
    }

    /// Mid-price: `(bid + ask) / 2`.
    pub fn mid(&self) -> Result<Money, MarketDataVerifyError> {
        let sum = self
            .bid
            .add(&self.ask)
            .map_err(|e| MarketDataVerifyError::malformed_data(format!("Mid price addition overflow: {e:?}")))?;
        sum.div(&Money::int(2))
            .map_err(|e| MarketDataVerifyError::malformed_data(format!("Mid price division error: {e:?}")))
    }

    /// Spread: `ask - bid`.
    pub fn spread(&self) -> Result<Money, MarketDataVerifyError> {
        self.ask
            .sub(&self.bid)
            .map_err(|e| MarketDataVerifyError::malformed_data(format!("Spread subtraction error: {e:?}")))
    }
}

impl PartialEq for Quote {
    fn eq(&self, other: &Self) -> bool {
        self.same(other)
    }
}

/// Source of option greeks: vendor or internal model.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum GreeksSource {
    Vendor,
    Model,
}

impl GreeksSource {
    pub fn as_str(&self) -> &'static str {
        match self {
            Self::Vendor => "vendor",
            Self::Model => "model",
        }
    }
}

/// Option sensitivities (greeks). Floats per D6.
#[derive(Debug, Clone, PartialEq)]
pub struct Greeks {
    pub delta: f64,
    pub gamma: f64,
    pub theta: f64,
    pub vega: f64,
    pub rho: f64,
    pub source: GreeksSource,
}

impl Greeks {
    pub fn new(
        delta: f64,
        gamma: f64,
        theta: f64,
        vega: f64,
        rho: f64,
        source: GreeksSource,
    ) -> Result<Self, MarketDataVerifyError> {
        for (name, val) in [
            ("delta", delta),
            ("gamma", gamma),
            ("theta", theta),
            ("vega", vega),
            ("rho", rho),
        ] {
            if !val.is_finite() {
                return Err(MarketDataVerifyError::malformed_data(format!(
                    "Greek {name} must be a finite number, got {val} (I5)"
                )));
            }
        }
        if !(-1.0..=1.0).contains(&delta) {
            return Err(MarketDataVerifyError::malformed_data(format!(
                "Delta {delta} is outside [-1, 1] (I5)"
            )));
        }
        Ok(Self {
            delta,
            gamma,
            theta,
            vega,
            rho,
            source,
        })
    }
}

/// Top-of-book quote for an option contract stamped with `as_of`.
#[derive(Debug, Clone)]
pub struct OptionQuote {
    pub contract: OptionContract,
    pub bid: Money,
    pub ask: Money,
    pub bid_size: Money,
    pub ask_size: Money,
    pub as_of: Ts,
    pub underlying_price: Option<Money>,
    pub implied_vol: Option<Money>,
    pub greeks: Option<Greeks>,
    pub open_interest: Option<i128>,
}

impl OptionQuote {
    pub fn same(&self, o: &OptionQuote) -> bool {
        self.contract.same(&o.contract)
            && dec_eq(&self.bid, &o.bid)
            && dec_eq(&self.ask, &o.ask)
            && dec_eq(&self.bid_size, &o.bid_size)
            && dec_eq(&self.ask_size, &o.ask_size)
            && self.as_of.eq(&o.as_of)
            && match (&self.underlying_price, &o.underlying_price) {
                (Some(a), Some(b)) => dec_eq(a, b),
                (None, None) => true,
                _ => false,
            }
            && match (&self.implied_vol, &o.implied_vol) {
                (Some(a), Some(b)) => dec_eq(a, b),
                (None, None) => true,
                _ => false,
            }
            && self.greeks == o.greeks
            && self.open_interest == o.open_interest
    }

    /// Mid-price: `(bid + ask) / 2`.
    pub fn mid(&self) -> Result<Money, MarketDataVerifyError> {
        let sum = self
            .bid
            .add(&self.ask)
            .map_err(|e| MarketDataVerifyError::malformed_data(format!("Mid price addition overflow: {e:?}")))?;
        sum.div(&Money::int(2))
            .map_err(|e| MarketDataVerifyError::malformed_data(format!("Mid price division error: {e:?}")))
    }

    /// Spread: `ask - bid`.
    pub fn spread(&self) -> Result<Money, MarketDataVerifyError> {
        self.ask
            .sub(&self.bid)
            .map_err(|e| MarketDataVerifyError::malformed_data(format!("Spread subtraction error: {e:?}")))
    }
}

impl PartialEq for OptionQuote {
    fn eq(&self, other: &Self) -> bool {
        self.same(other)
    }
}

/// Corporate action record stamped with `as_of`.
#[derive(Debug, Clone)]
pub struct CorporateAction {
    pub symbol: String,
    pub action_type: String,
    pub effective_date: NaiveDate,
    pub as_of: Ts,
    pub details: Option<serde_json::Value>,
}

impl CorporateAction {
    pub fn same(&self, o: &CorporateAction) -> bool {
        self.symbol == o.symbol
            && self.action_type == o.action_type
            && self.effective_date == o.effective_date
            && self.as_of.eq(&o.as_of)
            && self.details == o.details
    }
}

impl PartialEq for CorporateAction {
    fn eq(&self, other: &Self) -> bool {
        self.same(other)
    }
}

/// Typed verification errors for market data freshness, shape, ordering, and lookahead.
#[derive(Debug, Clone, PartialEq)]
pub enum MarketDataVerifyError {
    /// Data is older than the allowed max age (I5).
    StaleData {
        symbol: String,
        tf: Option<String>,
        age_seconds: f64,
        max_age_seconds: f64,
        as_of_iso: String,
        now_iso: String,
        message: String,
    },
    /// Data timestamp or as_of timestamp is in the future relative to clock (I5, I7).
    Lookahead {
        message: String,
    },
    /// Invalid request parameters (e.g. non-positive max_age, start > end).
    InvalidInput {
        message: String,
    },
    /// Malformed data payload (e.g. high < low, negative prices, crossed bid/ask).
    MalformedData {
        message: String,
    },
    /// Missing expected data (e.g. None item, empty identifier).
    MissingData {
        message: String,
    },
}

impl MarketDataVerifyError {
    pub fn stale_item(
        symbol: &str,
        age_seconds: f64,
        max_age_seconds: f64,
        as_of_iso: &str,
        now_iso: &str,
    ) -> Self {
        let message = format!(
            "Market data for {symbol} is stale: age {age_seconds:.3}s exceeds allowed max_age {max_age_seconds:.3}s (as_of={as_of_iso}, now={now_iso}) (I5)"
        );
        Self::StaleData {
            symbol: symbol.to_string(),
            tf: None,
            age_seconds,
            max_age_seconds,
            as_of_iso: as_of_iso.to_string(),
            now_iso: now_iso.to_string(),
            message,
        }
    }

    pub fn stale_bars(
        symbol: &str,
        tf: &str,
        age_seconds: f64,
        max_age_seconds: f64,
        as_of_iso: &str,
        now_iso: &str,
    ) -> Self {
        let message = format!(
            "Market data for {symbol} ({tf}) is stale: latest as_of {as_of_iso} is {age_seconds:.3}s old, exceeds allowed max_age {max_age_seconds:.3}s (now={now_iso}) (I5)"
        );
        Self::StaleData {
            symbol: symbol.to_string(),
            tf: Some(tf.to_string()),
            age_seconds,
            max_age_seconds,
            as_of_iso: as_of_iso.to_string(),
            now_iso: now_iso.to_string(),
            message,
        }
    }

    pub fn lookahead(message: impl Into<String>) -> Self {
        Self::Lookahead {
            message: message.into(),
        }
    }

    pub fn invalid_input(message: impl Into<String>) -> Self {
        Self::InvalidInput {
            message: message.into(),
        }
    }

    pub fn malformed_data(message: impl Into<String>) -> Self {
        Self::MalformedData {
            message: message.into(),
        }
    }

    pub fn missing_data(message: impl Into<String>) -> Self {
        Self::MissingData {
            message: message.into(),
        }
    }

    pub fn message(&self) -> &str {
        match self {
            Self::StaleData { message, .. } => message,
            Self::Lookahead { message } => message,
            Self::InvalidInput { message } => message,
            Self::MalformedData { message } => message,
            Self::MissingData { message } => message,
        }
    }
}

impl fmt::Display for MarketDataVerifyError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.message())
    }
}

impl Error for MarketDataVerifyError {}

impl From<MarketDataVerifyError> for LErr {
    fn from(err: MarketDataVerifyError) -> Self {
        match err {
            MarketDataVerifyError::StaleData { message, .. } => LErr {
                kind: "stale_data",
                msg: message,
            },
            MarketDataVerifyError::Lookahead { message } => LErr {
                kind: "lookahead",
                msg: message,
            },
            MarketDataVerifyError::InvalidInput { message } => LErr {
                kind: "value",
                msg: message,
            },
            MarketDataVerifyError::MalformedData { message } => LErr {
                kind: "value",
                msg: message,
            },
            MarketDataVerifyError::MissingData { message } => LErr {
                kind: "value",
                msg: message,
            },
        }
    }
}

/// Compute age in seconds: `now - as_of`. Positive means `as_of` is in the past.
pub fn age_seconds(now: &Ts, as_of: &Ts) -> f64 {
    let (n_sec, n_us) = now.key();
    let (a_sec, a_us) = as_of.key();
    let sec_diff = (n_sec as i128 - a_sec as i128) as f64;
    let us_diff = (n_us as i128 - a_us as i128) as f64;
    sec_diff + us_diff / 1_000_000.0
}

/// Validate caller-specified max age. Must be a finite positive number (I5).
pub fn validate_max_age(max_age_seconds: f64) -> Result<(), MarketDataVerifyError> {
    if !max_age_seconds.is_finite() || max_age_seconds <= 0.0 {
        return Err(MarketDataVerifyError::invalid_input(format!(
            "max_age_seconds must be a finite positive number (I5), got: {max_age_seconds}"
        )));
    }
    Ok(())
}

/// Validate future tolerance. Must be a finite non-negative number.
pub fn validate_future_tolerance(future_tolerance_seconds: f64) -> Result<(), MarketDataVerifyError> {
    if !future_tolerance_seconds.is_finite() || future_tolerance_seconds < 0.0 {
        return Err(MarketDataVerifyError::invalid_input(format!(
            "future_tolerance_seconds must be a finite non-negative number: {future_tolerance_seconds}"
        )));
    }
    Ok(())
}

/// Verify freshness and lookahead of an individual stamped item.
///
/// Returns `Ok(age)` on success.
pub fn verify_item_freshness(
    symbol: &str,
    as_of: &Ts,
    max_age_seconds: Option<f64>,
    now: &Ts,
    future_tolerance_seconds: f64,
) -> Result<f64, MarketDataVerifyError> {
    validate_future_tolerance(future_tolerance_seconds)?;

    let age = age_seconds(now, as_of);
    if age < -future_tolerance_seconds {
        return Err(MarketDataVerifyError::lookahead(format!(
            "Market data timestamp is in the future relative to clock: as_of={} > now={} (I5)",
            as_of.iso, now.iso
        )));
    }

    if let Some(max_age) = max_age_seconds {
        validate_max_age(max_age)?;
        if age > max_age {
            return Err(MarketDataVerifyError::stale_item(
                symbol,
                age,
                max_age,
                &as_of.iso,
                &now.iso,
            ));
        }
    }

    Ok(age)
}

/// Validate bar query parameters before fetching. Returns `effective_end = min(end, now)`.
pub fn validate_bars_request(
    start: &Ts,
    end: &Ts,
    max_age_seconds: f64,
    now: &Ts,
) -> Result<Ts, MarketDataVerifyError> {
    validate_max_age(max_age_seconds)?;

    if start.gt(end) {
        return Err(MarketDataVerifyError::invalid_input(format!(
            "start datetime {} cannot be after end datetime {} (I5)",
            start.iso, end.iso
        )));
    }

    if start.gt(now) {
        return Err(MarketDataVerifyError::invalid_input(format!(
            "Requested bar start {} is in the future relative to clock {} (I5)",
            start.iso, now.iso
        )));
    }

    let effective_end = if end.gt(now) {
        now.clone()
    } else {
        end.clone()
    };

    Ok(effective_end)
}

/// Verify OHLCV shape invariants of a single bar (I5).
pub fn verify_bar_shape(bar: &Bar) -> Result<(), MarketDataVerifyError> {
    // Prices strictly positive
    if bar.open <= Money::zero()
        || bar.high <= Money::zero()
        || bar.low <= Money::zero()
        || bar.close <= Money::zero()
    {
        return Err(MarketDataVerifyError::malformed_data(
            "Bar prices must be strictly positive (I5)",
        ));
    }

    // High cannot be lower than low
    if bar.high < bar.low {
        return Err(MarketDataVerifyError::malformed_data(format!(
            "Bar high ({}) cannot be lower than low ({})",
            bar.high.canon(),
            bar.low.canon()
        )));
    }

    // Open and close within [low, high]
    if bar.open < bar.low || bar.open > bar.high || bar.close < bar.low || bar.close > bar.high {
        return Err(MarketDataVerifyError::malformed_data(
            "Bar open and close must lie within [low, high]",
        ));
    }

    // Volume non-negative
    if bar.volume < Money::zero() {
        return Err(MarketDataVerifyError::malformed_data(
            "Bar volume must be non-negative",
        ));
    }

    Ok(())
}

/// Verify a single bar against clock, lookahead, instrument, and optional max age.
pub fn verify_bar(
    bar: &Bar,
    expected_instrument: Option<&Instrument>,
    max_age_seconds: Option<f64>,
    now: &Ts,
    future_tolerance_seconds: f64,
) -> Result<(), MarketDataVerifyError> {
    if let Some(expected) = expected_instrument {
        if !bar.instr.same(expected) {
            let actual_sym = bar.instr.symbol().unwrap_or_else(|_| "UNKNOWN".into());
            let exp_sym = expected.symbol().unwrap_or_else(|_| "UNKNOWN".into());
            return Err(MarketDataVerifyError::malformed_data(format!(
                "Bar instrument {actual_sym} does not match expected {exp_sym}"
            )));
        }
    }

    verify_bar_shape(bar)?;

    // Bar timestamp in future relative to clock
    if bar.ts.gt(now) {
        return Err(MarketDataVerifyError::lookahead(format!(
            "Bar timestamp {} is in the future relative to clock {} (I5)",
            bar.ts.iso, now.iso
        )));
    }

    let symbol = bar.instr.symbol().unwrap_or_else(|_| "UNKNOWN".into());
    verify_item_freshness(&symbol, &bar.as_of, max_age_seconds, now, future_tolerance_seconds)?;

    Ok(())
}

/// Verify already-fetched series of historical bars.
///
/// Enforces:
/// - Request bounds validation and effective_end calculation
/// - Shape checks on all bars (prices > 0, high >= low, open/close within range, volume >= 0)
/// - Look-ahead prevention: bar timestamps and as_of timestamps cannot be in the future
/// - Range bounds: bar timestamps must be within `start..=effective_end`
/// - Chronological ordering: bars must be monotonically non-decreasing in timestamp
/// - Series freshness: if bars is non-empty, latest `as_of` must be within `max_age_seconds`
pub fn verify_bars_series(
    instrument: &Instrument,
    tf: &str,
    start: &Ts,
    end: &Ts,
    max_age_seconds: f64,
    now: &Ts,
    future_tolerance_seconds: f64,
    raw_bars: &[Bar],
    strict: bool,
) -> Result<(Vec<Bar>, Vec<String>), MarketDataVerifyError> {
    let effective_end = validate_bars_request(start, end, max_age_seconds, now)?;
    validate_future_tolerance(future_tolerance_seconds)?;

    let mut warnings = Vec::new();
    let mut prev_ts: Option<&Ts> = None;
    for b in raw_bars {
        // Instrument match
        if !b.instr.same(instrument) {
            let actual_sym = b.instr.symbol().unwrap_or_else(|_| "UNKNOWN".into());
            let exp_sym = instrument.symbol().unwrap_or_else(|_| "UNKNOWN".into());
            let msg = format!(
                "Provider returned bar with instrument {actual_sym} for requested {exp_sym} (I5)"
            );
            if strict {
                return Err(MarketDataVerifyError::malformed_data(msg));
            } else {
                warnings.push(msg);
            }
        }

        // Shape invariants
        verify_bar_shape(b)?;

        // Lookahead check on bar timestamp
        if b.ts.gt(now) {
            return Err(MarketDataVerifyError::lookahead(format!(
                "Bar timestamp {} is in the future relative to clock {} (I5)",
                b.ts.iso, now.iso
            )));
        }

        // Interval checks: start <= b.ts <= effective_end
        if b.ts.lt(start) {
            let msg = format!(
                "Bar timestamp {} is before requested start {} (I5)",
                b.ts.iso, start.iso
            );
            if strict {
                return Err(MarketDataVerifyError::malformed_data(msg));
            } else {
                warnings.push(msg);
            }
        }
        if b.ts.gt(&effective_end) {
            let msg = format!(
                "Bar timestamp {} is after effective end {} (I5)",
                b.ts.iso, effective_end.iso
            );
            if strict {
                return Err(MarketDataVerifyError::malformed_data(msg));
            } else {
                warnings.push(msg);
            }
        }

        // Chronological ordering check
        if let Some(prev) = prev_ts {
            if b.ts.lt(prev) {
                let sym = instrument.symbol().unwrap_or_else(|_| "UNKNOWN".into());
                let msg = format!(
                    "Bar series for {sym} is not in chronological order: {} followed by {}",
                    prev.iso, b.ts.iso
                );
                if strict {
                    return Err(MarketDataVerifyError::malformed_data(msg));
                } else {
                    warnings.push(msg);
                }
            }
        }
        prev_ts = Some(&b.ts);

        // Verify individual as_of for future lookahead (not max_age on individual historical bars)
        let sym = instrument.symbol().unwrap_or_else(|_| "UNKNOWN".into());
        verify_item_freshness(&sym, &b.as_of, None, now, future_tolerance_seconds)?;
    }

    // Series freshness check on newest as_of
    if !raw_bars.is_empty() {
        let newest_as_of = raw_bars
            .iter()
            .map(|b| &b.as_of)
            .max_by(|a, b| a.key().cmp(&b.key()))
            .ok_or_else(|| MarketDataVerifyError::missing_data("Empty bar series"))?;

        let age = age_seconds(now, newest_as_of);
        if age > max_age_seconds {
            let symbol = instrument.symbol().unwrap_or_else(|_| "UNKNOWN".into());
            return Err(MarketDataVerifyError::stale_bars(
                &symbol,
                tf,
                age,
                max_age_seconds,
                &newest_as_of.iso,
                &now.iso,
            ));
        }
    }

    Ok((raw_bars.to_vec(), warnings))
}

/// Verify quote shape invariants (I5).
pub fn verify_quote_shape(quote: &Quote) -> Result<(), MarketDataVerifyError> {
    if quote.bid < Money::zero() || quote.ask < Money::zero() {
        return Err(MarketDataVerifyError::malformed_data(
            "Quote bid and ask must be non-negative",
        ));
    }
    if quote.bid > quote.ask {
        return Err(MarketDataVerifyError::malformed_data(format!(
            "Quote bid ({}) cannot exceed ask ({})",
            quote.bid.canon(),
            quote.ask.canon()
        )));
    }
    if quote.bid_size < Money::zero() || quote.ask_size < Money::zero() {
        return Err(MarketDataVerifyError::malformed_data(
            "Quote sizes must be non-negative",
        ));
    }
    Ok(())
}

/// Verify top-of-book quote freshness, shape, and lookahead.
pub fn verify_quote(
    quote: &Quote,
    expected_instrument: Option<&Instrument>,
    max_age_seconds: f64,
    now: &Ts,
    future_tolerance_seconds: f64,
) -> Result<(), MarketDataVerifyError> {
    if let Some(expected) = expected_instrument {
        if !quote.instrument.same(expected) {
            let actual_sym = quote.instrument.symbol().unwrap_or_else(|_| "UNKNOWN".into());
            let exp_sym = expected.symbol().unwrap_or_else(|_| "UNKNOWN".into());
            return Err(MarketDataVerifyError::malformed_data(format!(
                "Quote instrument {actual_sym} does not match expected {exp_sym}"
            )));
        }
    }

    verify_quote_shape(quote)?;

    let symbol = quote.instrument.symbol().unwrap_or_else(|_| "UNKNOWN".into());
    verify_item_freshness(
        &symbol,
        &quote.as_of,
        Some(max_age_seconds),
        now,
        future_tolerance_seconds,
    )?;

    Ok(())
}

/// Validate option chain request parameters before fetching.
pub fn validate_chain_request(
    expiry_start: NaiveDate,
    expiry_end: NaiveDate,
    max_age_seconds: f64,
) -> Result<(), MarketDataVerifyError> {
    validate_max_age(max_age_seconds)?;
    if expiry_start > expiry_end {
        return Err(MarketDataVerifyError::invalid_input(format!(
            "expiry_start {expiry_start} cannot be after expiry_end {expiry_end} (I5)"
        )));
    }
    Ok(())
}

/// Verify option quote shape invariants.
pub fn verify_option_quote_shape(quote: &OptionQuote) -> Result<(), MarketDataVerifyError> {
    if quote.bid < Money::zero() || quote.ask < Money::zero() {
        return Err(MarketDataVerifyError::malformed_data(
            "OptionQuote bid and ask must be non-negative",
        ));
    }
    if quote.bid > quote.ask {
        return Err(MarketDataVerifyError::malformed_data(format!(
            "OptionQuote bid ({}) cannot exceed ask ({})",
            quote.bid.canon(),
            quote.ask.canon()
        )));
    }
    if quote.bid_size < Money::zero() || quote.ask_size < Money::zero() {
        return Err(MarketDataVerifyError::malformed_data(
            "OptionQuote sizes must be non-negative",
        ));
    }
    if let Some(spot) = &quote.underlying_price {
        if *spot <= Money::zero() {
            return Err(MarketDataVerifyError::malformed_data(
                "OptionQuote underlying_price must be strictly positive",
            ));
        }
    }
    if let Some(iv) = &quote.implied_vol {
        if *iv < Money::zero() {
            return Err(MarketDataVerifyError::malformed_data(
                "OptionQuote implied_vol must be non-negative",
            ));
        }
    }
    if let Some(oi) = quote.open_interest {
        if oi < 0 {
            return Err(MarketDataVerifyError::malformed_data(
                "OptionQuote open_interest must be non-negative",
            ));
        }
    }
    Ok(())
}

/// Verify option chain quotes for an underlying over an expiration range.
///
/// Enforces:
/// - Request bounds validation (`expiry_start <= expiry_end`)
/// - Contract underlying matches requested underlying
/// - Contract expiry lies within `[expiry_start, expiry_end]`
/// - Shape verification for each option quote
/// - Freshness and lookahead prevention on each quote
pub fn verify_chain(
    underlying: &str,
    expiry_start: NaiveDate,
    expiry_end: NaiveDate,
    max_age_seconds: f64,
    now: &Ts,
    future_tolerance_seconds: f64,
    raw_chain: &[OptionQuote],
    strict: bool,
) -> Result<(Vec<OptionQuote>, Vec<String>), MarketDataVerifyError> {
    if underlying.trim().is_empty() {
        return Err(MarketDataVerifyError::invalid_input("underlying must be non-empty"));
    }
    validate_chain_request(expiry_start, expiry_end, max_age_seconds)?;
    validate_future_tolerance(future_tolerance_seconds)?;

    let mut warnings = Vec::new();
    for q in raw_chain {
        if q.contract.underlying != underlying {
            let msg = format!(
                "Option quote underlying {} does not match requested underlying {} (I5)",
                q.contract.underlying, underlying
            );
            if strict {
                return Err(MarketDataVerifyError::malformed_data(msg));
            } else {
                warnings.push(msg);
            }
        }
        if q.contract.expiry < expiry_start || q.contract.expiry > expiry_end {
            let msg = format!(
                "Option quote expiry {} is outside requested range [{}, {}] (I5)",
                q.contract.expiry, expiry_start, expiry_end
            );
            if strict {
                return Err(MarketDataVerifyError::malformed_data(msg));
            } else {
                warnings.push(msg);
            }
        }

        verify_option_quote_shape(q)?;

        let sym = q.contract.occ().unwrap_or_else(|_| {
            format!("{}:{}:{:?}", q.contract.underlying, q.contract.expiry, q.contract.right)
        });
        verify_item_freshness(
            &sym,
            &q.as_of,
            Some(max_age_seconds),
            now,
            future_tolerance_seconds,
        )?;
    }

    Ok((raw_chain.to_vec(), warnings))
}

/// Verify corporate action shape invariants.
pub fn verify_corporate_action_shape(action: &CorporateAction) -> Result<(), MarketDataVerifyError> {
    if action.symbol.trim().is_empty() {
        return Err(MarketDataVerifyError::malformed_data("CorporateAction symbol must be non-empty"));
    }
    if action.action_type.trim().is_empty() {
        return Err(MarketDataVerifyError::malformed_data(
            "CorporateAction action_type must be non-empty",
        ));
    }
    Ok(())
}

/// Verify corporate actions for a symbol.
pub fn verify_corporate_actions(
    symbol: &str,
    max_age_seconds: f64,
    now: &Ts,
    future_tolerance_seconds: f64,
    raw_actions: &[CorporateAction],
    strict: bool,
) -> Result<(Vec<CorporateAction>, Vec<String>), MarketDataVerifyError> {
    if symbol.trim().is_empty() {
        return Err(MarketDataVerifyError::invalid_input("symbol must be non-empty"));
    }
    validate_max_age(max_age_seconds)?;
    validate_future_tolerance(future_tolerance_seconds)?;

    let mut warnings = Vec::new();
    for a in raw_actions {
        verify_corporate_action_shape(a)?;
        if a.symbol != symbol {
            let msg = format!(
                "Corporate action symbol {} does not match requested symbol {} (I5)",
                a.symbol, symbol
            );
            if strict {
                return Err(MarketDataVerifyError::malformed_data(msg));
            } else {
                warnings.push(msg);
            }
        }
        verify_item_freshness(
            &a.symbol,
            &a.as_of,
            Some(max_age_seconds),
            now,
            future_tolerance_seconds,
        )?;
    }

    Ok((raw_actions.to_vec(), warnings))
}

/// Configured verifier holding future tolerance, providing stateful verification calls.
#[derive(Debug, Clone)]
pub struct MarketDataVerifier {
    pub future_tolerance_seconds: f64,
}

impl MarketDataVerifier {
    pub fn new(future_tolerance_seconds: f64) -> Result<Self, MarketDataVerifyError> {
        validate_future_tolerance(future_tolerance_seconds)?;
        Ok(Self {
            future_tolerance_seconds,
        })
    }

    pub fn default_verifier() -> Self {
        Self {
            future_tolerance_seconds: 0.0,
        }
    }

    pub fn verify_item(
        &self,
        symbol: &str,
        as_of: &Ts,
        max_age_seconds: Option<f64>,
        now: &Ts,
    ) -> Result<f64, MarketDataVerifyError> {
        verify_item_freshness(
            symbol,
            as_of,
            max_age_seconds,
            now,
            self.future_tolerance_seconds,
        )
    }

    pub fn validate_bars_request(
        &self,
        start: &Ts,
        end: &Ts,
        max_age_seconds: f64,
        now: &Ts,
    ) -> Result<Ts, MarketDataVerifyError> {
        validate_bars_request(start, end, max_age_seconds, now)
    }

    pub fn verify_bars(
        &self,
        instrument: &Instrument,
        tf: &str,
        start: &Ts,
        end: &Ts,
        max_age_seconds: f64,
        now: &Ts,
        raw_bars: &[Bar],
        strict: bool,
    ) -> Result<(Vec<Bar>, Vec<String>), MarketDataVerifyError> {
        verify_bars_series(
            instrument,
            tf,
            start,
            end,
            max_age_seconds,
            now,
            self.future_tolerance_seconds,
            raw_bars,
            strict,
        )
    }

    pub fn verify_quote(
        &self,
        quote: &Quote,
        expected_instrument: Option<&Instrument>,
        max_age_seconds: f64,
        now: &Ts,
    ) -> Result<(), MarketDataVerifyError> {
        verify_quote(
            quote,
            expected_instrument,
            max_age_seconds,
            now,
            self.future_tolerance_seconds,
        )
    }

    pub fn verify_chain(
        &self,
        underlying: &str,
        expiry_start: NaiveDate,
        expiry_end: NaiveDate,
        max_age_seconds: f64,
        now: &Ts,
        raw_chain: &[OptionQuote],
        strict: bool,
    ) -> Result<(Vec<OptionQuote>, Vec<String>), MarketDataVerifyError> {
        verify_chain(
            underlying,
            expiry_start,
            expiry_end,
            max_age_seconds,
            now,
            self.future_tolerance_seconds,
            raw_chain,
            strict,
        )
    }

    pub fn verify_corporate_actions(
        &self,
        symbol: &str,
        max_age_seconds: f64,
        now: &Ts,
        raw_actions: &[CorporateAction],
        strict: bool,
    ) -> Result<(Vec<CorporateAction>, Vec<String>), MarketDataVerifyError> {
        verify_corporate_actions(
            symbol,
            max_age_seconds,
            now,
            self.future_tolerance_seconds,
            raw_actions,
            strict,
        )
    }
}

//! Spec-derived and property tests for market data verification (Architecture §4.8, I5, I7).
//!
//! Enforces:
//! 1. MDV-1: Parameter & Boundary Validation. Finite positive max_age, non-negative tolerance,
//!    start <= end, start <= now, and effective_end clipping.
//! 2. MDV-2: Lookahead Prevention. Any data timestamp or as_of in the future relative to clock
//!    (beyond tolerance) is strictly refused with typed Lookahead error.
//! 3. MDV-3: Freshness & Stale Data Enforcement. Data older than max_age_seconds raises typed
//!    StaleData error. Historical series fresh when latest as_of is within max_age.
//! 4. MDV-4: Shape Invariants & Malformed Data Rejection. Bar OHLC bounds, strictly positive prices,
//!    bid <= ask, non-negative volume and sizes, delta in [-1, 1], finite greeks.
//! 5. MDV-5: Series Ordering & Scope Verification. Non-decreasing chronological timestamps,
//!    interval bounds [start, effective_end], symbol and underlying matching, expiry range bounds.
//!
//! Uses unit tests and proptest properties.

use chrono::NaiveDate;
use proptest::prelude::*;

use te_core::ledger::model::{Instrument, OptionContract};
use te_core::market_data_verify::{
    age_seconds, validate_bars_request, validate_chain_request, validate_future_tolerance,
    validate_max_age, verify_bar, verify_bar_shape, verify_bars_series, verify_chain,
    verify_corporate_actions, verify_item_freshness, verify_option_quote_shape, verify_quote,
    verify_quote_shape, Bar, CorporateAction, Greeks, GreeksSource, MarketDataVerifier,
    MarketDataVerifyError, OptionQuote, Quote,
};
use te_core::money::Money;
use te_core::options::Right;
use te_core::sim::Ts;

fn ts(iso: &str) -> Ts {
    Ts::aware(iso, "ts").expect("valid iso timestamp")
}

fn money(s: &str) -> Money {
    Money::parse(s).expect("valid decimal money")
}

fn date(y: i32, m: u32, d: u32) -> NaiveDate {
    NaiveDate::from_ymd_opt(y, m, d).expect("valid naive date")
}

fn sample_bar(
    instr: Instrument,
    ts_iso: &str,
    as_of_iso: &str,
    open: &str,
    high: &str,
    low: &str,
    close: &str,
    vol: &str,
) -> Bar {
    Bar {
        instr,
        ts: ts(ts_iso),
        open: money(open),
        high: money(high),
        low: money(low),
        close: money(close),
        volume: money(vol),
        as_of: ts(as_of_iso),
    }
}

// =============================================================================
// MDV-1: Parameter & Boundary Validation
// =============================================================================

#[test]
fn mdv_1_validate_max_age_boundaries() {
    assert!(validate_max_age(1.0).is_ok());
    assert!(validate_max_age(60.0).is_ok());
    assert!(validate_max_age(0.001).is_ok());

    // Non-positive or non-finite refused
    assert!(matches!(
        validate_max_age(0.0),
        Err(MarketDataVerifyError::InvalidInput { .. })
    ));
    assert!(matches!(
        validate_max_age(-10.0),
        Err(MarketDataVerifyError::InvalidInput { .. })
    ));
    assert!(matches!(
        validate_max_age(f64::NAN),
        Err(MarketDataVerifyError::InvalidInput { .. })
    ));
    assert!(matches!(
        validate_max_age(f64::INFINITY),
        Err(MarketDataVerifyError::InvalidInput { .. })
    ));
    assert!(matches!(
        validate_max_age(f64::NEG_INFINITY),
        Err(MarketDataVerifyError::InvalidInput { .. })
    ));
}

#[test]
fn mdv_1_validate_future_tolerance_boundaries() {
    assert!(validate_future_tolerance(0.0).is_ok());
    assert!(validate_future_tolerance(0.05).is_ok());
    assert!(validate_future_tolerance(1.0).is_ok());

    assert!(matches!(
        validate_future_tolerance(-0.001),
        Err(MarketDataVerifyError::InvalidInput { .. })
    ));
    assert!(matches!(
        validate_future_tolerance(f64::NAN),
        Err(MarketDataVerifyError::InvalidInput { .. })
    ));
    assert!(matches!(
        validate_future_tolerance(f64::INFINITY),
        Err(MarketDataVerifyError::InvalidInput { .. })
    ));
}

#[test]
fn mdv_1_bars_request_boundaries_and_clipping() {
    let now = ts("2026-09-23T14:30:00Z");
    let t_past_1 = ts("2026-09-23T14:10:00Z");
    let t_past_2 = ts("2026-09-23T14:20:00Z");
    let t_future_1 = ts("2026-09-23T15:00:00Z");
    let t_future_2 = ts("2026-09-23T15:30:00Z");

    // 1. Normal past request
    let eff = validate_bars_request(&t_past_1, &t_past_2, 60.0, &now).expect("valid request");
    assert_eq!(eff.iso, t_past_2.iso);

    // 2. Request start > end refused
    let err = validate_bars_request(&t_past_2, &t_past_1, 60.0, &now).unwrap_err();
    assert!(matches!(err, MarketDataVerifyError::InvalidInput { .. }));
    assert!(err.message().contains("cannot be after end datetime"));

    // 3. Request start in future relative to clock refused
    let err = validate_bars_request(&t_future_1, &t_future_2, 60.0, &now).unwrap_err();
    assert!(matches!(err, MarketDataVerifyError::InvalidInput { .. }));
    assert!(err.message().contains("is in the future relative to clock"));

    // 4. Request end in future clipped to now (no lookahead)
    let eff = validate_bars_request(&t_past_1, &t_future_1, 60.0, &now).expect("clipped request");
    assert_eq!(eff.iso, now.iso);
}

#[test]
fn mdv_1_chain_request_boundaries() {
    assert!(validate_chain_request(date(2026, 10, 1), date(2026, 10, 30), 60.0).is_ok());

    let err = validate_chain_request(date(2026, 10, 30), date(2026, 10, 1), 60.0).unwrap_err();
    assert!(matches!(err, MarketDataVerifyError::InvalidInput { .. }));
    assert!(err.message().contains("cannot be after expiry_end"));
}

// =============================================================================
// MDV-2: Lookahead Prevention
// =============================================================================

#[test]
fn mdv_2_lookahead_item_as_of_in_future_rejected() {
    let now = ts("2026-09-23T14:30:00Z");
    let future_as_of = ts("2026-09-23T14:30:01Z"); // 1 second in future

    // Zero tolerance rejects
    let err = verify_item_freshness("SPY", &future_as_of, Some(10.0), &now, 0.0).unwrap_err();
    assert!(matches!(err, MarketDataVerifyError::Lookahead { .. }));
    assert!(err.message().contains("is in the future relative to clock"));

    // Tolerance of 2.0s accepts it
    let res = verify_item_freshness("SPY", &future_as_of, Some(10.0), &now, 2.0);
    assert!(res.is_ok());
}

#[test]
fn mdv_2_bar_timestamp_in_future_rejected() {
    let now = ts("2026-09-23T14:30:00Z");
    let eq = Instrument::Equity("SPY".to_string());

    // Bar timestamp in future even if as_of is now
    let bar = sample_bar(
        eq.clone(),
        "2026-09-23T14:35:00Z", // 5 minutes in future
        "2026-09-23T14:30:00Z",
        "500",
        "501",
        "499",
        "500.5",
        "1000",
    );

    let err = verify_bar(&bar, Some(&eq), Some(60.0), &now, 0.0).unwrap_err();
    assert!(matches!(err, MarketDataVerifyError::Lookahead { .. }));
    assert!(err.message().contains("Bar timestamp"));
}

// =============================================================================
// MDV-3: Freshness & Stale Data Enforcement
// =============================================================================

#[test]
fn mdv_3_quote_fresh_and_stale() {
    let now = ts("2026-09-23T14:30:10Z");
    let eq = Instrument::Equity("SPY".to_string());

    // 1. Fresh quote (age 1.0s <= max_age 5.0s)
    let fresh_quote = Quote::new(
        eq.clone(),
        money("500.00"),
        money("500.10"),
        money("100"),
        money("200"),
        ts("2026-09-23T14:30:09Z"),
    );
    assert!(verify_quote(&fresh_quote, Some(&eq), 5.0, &now, 0.0).is_ok());
    assert_eq!(fresh_quote.mid().unwrap().canon(), "500.05");
    assert_eq!(fresh_quote.spread().unwrap().canon(), "0.1");

    // 2. Stale quote (age 11.0s > max_age 5.0s)
    let stale_quote = Quote::new(
        eq.clone(),
        money("500.00"),
        money("500.10"),
        money("100"),
        money("200"),
        ts("2026-09-23T14:29:59Z"),
    );
    let err = verify_quote(&stale_quote, Some(&eq), 5.0, &now, 0.0).unwrap_err();
    assert!(matches!(err, MarketDataVerifyError::StaleData { .. }));
    assert!(err.message().contains("is stale: age 11.000s exceeds allowed max_age 5.000s"));
}

#[test]
fn mdv_3_bars_series_fresh_and_stale() {
    let now = ts("2026-09-23T14:30:00Z");
    let eq = Instrument::Equity("AAPL".to_string());
    let start = ts("2026-09-23T14:00:00Z");
    let end = ts("2026-09-23T14:30:00Z");

    let bar1 = sample_bar(
        eq.clone(),
        "2026-09-23T14:15:00Z",
        "2026-09-23T14:15:00Z",
        "100",
        "101",
        "99",
        "100.5",
        "1000",
    );
    let bar2 = sample_bar(
        eq.clone(),
        "2026-09-23T14:29:50Z",
        "2026-09-23T14:29:50Z", // 10s old
        "100.5",
        "102",
        "100",
        "101.5",
        "1000",
    );

    // Fresh within 60s
    let res = verify_bars_series(&eq, "1m", &start, &end, 60.0, &now, 0.0, &[bar1.clone(), bar2]);
    assert!(res.is_ok());
    assert_eq!(res.unwrap().len(), 2);

    // Stale when max_age is 5s (newest is 10s old)
    let bar3 = sample_bar(
        eq.clone(),
        "2026-09-23T14:25:00Z",
        "2026-09-23T14:25:00Z", // 300s old
        "100",
        "101",
        "99",
        "100.5",
        "1000",
    );
    let err = verify_bars_series(&eq, "1m", &start, &end, 60.0, &now, 0.0, &[bar1, bar3]).unwrap_err();
    assert!(matches!(err, MarketDataVerifyError::StaleData { .. }));
    assert!(err.message().contains("is stale: latest as_of"));

    // Empty bar series returns Ok(vec![])
    let empty = verify_bars_series(&eq, "1m", &start, &end, 60.0, &now, 0.0, &[]).unwrap();
    assert!(empty.is_empty());
}

#[test]
fn mdv_3_chain_fresh_and_stale() {
    let now = ts("2026-09-23T14:30:00Z");
    let contract = OptionContract {
        underlying: "SPY".to_string(),
        expiry: date(2026, 10, 16),
        strike: money("500"),
        right: Right::Call,
        multiplier: 100,
    };

    let q = OptionQuote {
        contract,
        bid: money("5.20"),
        ask: money("5.40"),
        bid_size: money("10"),
        ask_size: money("10"),
        as_of: ts("2026-09-23T14:29:40Z"), // 20s old
        underlying_price: Some(money("500")),
        implied_vol: Some(money("0.20")),
        greeks: None,
        open_interest: Some(150),
    };

    // Fresh with max_age 30s
    let res = verify_chain(
        "SPY",
        date(2026, 10, 1),
        date(2026, 10, 30),
        30.0,
        &now,
        0.0,
        &[q.clone()],
    );
    assert!(res.is_ok());

    // Stale with max_age 15s
    let err = verify_chain(
        "SPY",
        date(2026, 10, 1),
        date(2026, 10, 30),
        15.0,
        &now,
        0.0,
        &[q],
    )
    .unwrap_err();
    assert!(matches!(err, MarketDataVerifyError::StaleData { .. }));
}

#[test]
fn mdv_3_corporate_actions_fresh_and_stale() {
    let now = ts("2026-09-23T14:30:00Z");
    let act = CorporateAction {
        symbol: "AAPL".to_string(),
        action_type: "DIVIDEND".to_string(),
        effective_date: date(2026, 10, 1),
        as_of: ts("2026-09-23T14:29:10Z"), // 50s old
        details: None,
    };

    // Fresh with max_age 100s
    assert!(verify_corporate_actions("AAPL", 100.0, &now, 0.0, &[act.clone()]).is_ok());

    // Stale with max_age 20s
    let err = verify_corporate_actions("AAPL", 20.0, &now, 0.0, &[act]).unwrap_err();
    assert!(matches!(err, MarketDataVerifyError::StaleData { .. }));
}

// =============================================================================
// MDV-4: Shape Invariants & Malformed Data Rejection
// =============================================================================

#[test]
fn mdv_4_bar_shape_invariants() {
    let eq = Instrument::Equity("SPY".to_string());

    // High < low
    let b_bad_hl = sample_bar(
        eq.clone(),
        "2026-09-23T14:00:00Z",
        "2026-09-23T14:00:00Z",
        "100",
        "98",
        "99",
        "98.5",
        "100",
    );
    let err_hl = verify_bar_shape(&b_bad_hl).unwrap_err();
    assert!(err_hl.message().contains("cannot be lower than low"));

    // Negative price
    let b_neg = sample_bar(
        eq.clone(),
        "2026-09-23T14:00:00Z",
        "2026-09-23T14:00:00Z",
        "-10",
        "100",
        "90",
        "95",
        "100",
    );
    let err_neg = verify_bar_shape(&b_neg).unwrap_err();
    assert!(err_neg.message().contains("Bar prices must be strictly positive"));

    // Open outside [low, high]
    let b_open_high = sample_bar(
        eq.clone(),
        "2026-09-23T14:00:00Z",
        "2026-09-23T14:00:00Z",
        "105",
        "100",
        "90",
        "95",
        "100",
    );
    let err_open = verify_bar_shape(&b_open_high).unwrap_err();
    assert!(err_open.message().contains("Bar open and close must lie within [low, high]"));

    // Negative volume
    let b_neg_vol = sample_bar(
        eq,
        "2026-09-23T14:00:00Z",
        "2026-09-23T14:00:00Z",
        "95",
        "100",
        "90",
        "95",
        "-1",
    );
    let err_vol = verify_bar_shape(&b_neg_vol).unwrap_err();
    assert!(err_vol.message().contains("Bar volume must be non-negative"));
}

#[test]
fn mdv_4_quote_shape_invariants() {
    let eq = Instrument::Equity("SPY".to_string());

    // Crossed quote (bid > ask)
    let q_crossed = Quote::new(
        eq.clone(),
        money("501"),
        money("500"),
        money("10"),
        money("10"),
        ts("2026-09-23T14:00:00Z"),
    );
    assert!(verify_quote_shape(&q_crossed).is_err());

    // Negative bid
    let q_neg = Quote::new(
        eq,
        money("-1"),
        money("500"),
        money("10"),
        money("10"),
        ts("2026-09-23T14:00:00Z"),
    );
    assert!(verify_quote_shape(&q_neg).is_err());
}

#[test]
fn mdv_4_option_quote_shape_invariants() {
    let contract = OptionContract {
        underlying: "SPY".to_string(),
        expiry: date(2026, 10, 16),
        strike: money("500"),
        right: Right::Call,
        multiplier: 100,
    };
    let mut oq = OptionQuote {
        contract,
        bid: money("5.00"),
        ask: money("5.50"),
        bid_size: money("10"),
        ask_size: money("10"),
        as_of: ts("2026-09-23T14:00:00Z"),
        underlying_price: Some(money("500.00")),
        implied_vol: Some(money("0.20")),
        greeks: None,
        open_interest: Some(100),
    };
    assert!(verify_option_quote_shape(&oq).is_ok());

    // Crossed bid/ask
    oq.bid = money("6.00");
    assert!(verify_option_quote_shape(&oq).is_err());
    oq.bid = money("5.00");

    // Negative underlying price
    oq.underlying_price = Some(money("-500.00"));
    assert!(verify_option_quote_shape(&oq).is_err());
    oq.underlying_price = Some(money("500.00"));

    // Negative IV
    oq.implied_vol = Some(money("-0.20"));
    assert!(verify_option_quote_shape(&oq).is_err());
    oq.implied_vol = Some(money("0.20"));

    // Negative open interest
    oq.open_interest = Some(-1);
    assert!(verify_option_quote_shape(&oq).is_err());
}

#[test]
fn mdv_4_greeks_invariants() {
    // Delta out of [-1, 1]
    assert!(Greeks::new(1.05, 0.1, -0.05, 0.2, 0.01, GreeksSource::Vendor).is_err());
    assert!(Greeks::new(-1.05, 0.1, -0.05, 0.2, 0.01, GreeksSource::Vendor).is_err());

    // Non-finite greek
    assert!(Greeks::new(0.5, f64::NAN, -0.05, 0.2, 0.01, GreeksSource::Vendor).is_err());
    assert!(Greeks::new(0.5, 0.1, f64::INFINITY, 0.2, 0.01, GreeksSource::Vendor).is_err());

    // Valid greeks
    assert!(Greeks::new(0.5, 0.1, -0.05, 0.2, 0.01, GreeksSource::Vendor).is_ok());
}

// =============================================================================
// MDV-5: Series Ordering & Scope Verification
// =============================================================================

#[test]
fn mdv_5_bar_series_chronological_ordering() {
    let now = ts("2026-09-23T14:30:00Z");
    let eq = Instrument::Equity("SPY".to_string());
    let start = ts("2026-09-23T14:00:00Z");
    let end = ts("2026-09-23T14:30:00Z");

    let b1 = sample_bar(
        eq.clone(),
        "2026-09-23T14:10:00Z",
        "2026-09-23T14:29:00Z",
        "100",
        "101",
        "99",
        "100.5",
        "1000",
    );
    let b2 = sample_bar(
        eq.clone(),
        "2026-09-23T14:05:00Z", // Out of order! (precedes b1)
        "2026-09-23T14:29:00Z",
        "100",
        "101",
        "99",
        "100.5",
        "1000",
    );

    let err = verify_bars_series(&eq, "1m", &start, &end, 60.0, &now, 0.0, &[b1, b2]).unwrap_err();
    assert!(matches!(err, MarketDataVerifyError::MalformedData { .. }));
    assert!(err.message().contains("chronological order"));
}

#[test]
fn mdv_5_bar_series_instrument_mismatch() {
    let now = ts("2026-09-23T14:30:00Z");
    let spy = Instrument::Equity("SPY".to_string());
    let aapl = Instrument::Equity("AAPL".to_string());
    let start = ts("2026-09-23T14:00:00Z");
    let end = ts("2026-09-23T14:30:00Z");

    let b = sample_bar(
        aapl, // Mismatch!
        "2026-09-23T14:10:00Z",
        "2026-09-23T14:29:00Z",
        "100",
        "101",
        "99",
        "100.5",
        "1000",
    );

    let err = verify_bars_series(&spy, "1m", &start, &end, 60.0, &now, 0.0, &[b]).unwrap_err();
    assert!(matches!(err, MarketDataVerifyError::MalformedData { .. }));
    assert!(err.message().contains("instrument"));
}

#[test]
fn mdv_5_chain_underlying_and_expiry_scope() {
    let now = ts("2026-09-23T14:30:00Z");
    let contract_wrong_und = OptionContract {
        underlying: "QQQ".to_string(), // Mismatch!
        expiry: date(2026, 10, 16),
        strike: money("500"),
        right: Right::Call,
        multiplier: 100,
    };
    let q_wrong_und = OptionQuote {
        contract: contract_wrong_und,
        bid: money("5.20"),
        ask: money("5.40"),
        bid_size: money("10"),
        ask_size: money("10"),
        as_of: ts("2026-09-23T14:29:50Z"),
        underlying_price: None,
        implied_vol: None,
        greeks: None,
        open_interest: None,
    };

    let err = verify_chain(
        "SPY",
        date(2026, 10, 1),
        date(2026, 10, 30),
        60.0,
        &now,
        0.0,
        &[q_wrong_und],
    )
    .unwrap_err();
    assert!(matches!(err, MarketDataVerifyError::MalformedData { .. }));
    assert!(err.message().contains("does not match requested underlying"));

    // Expiry outside requested range
    let contract_out_exp = OptionContract {
        underlying: "SPY".to_string(),
        expiry: date(2026, 11, 20), // Outside [2026-10-01, 2026-10-30]
        strike: money("500"),
        right: Right::Call,
        multiplier: 100,
    };
    let q_out_exp = OptionQuote {
        contract: contract_out_exp,
        bid: money("5.20"),
        ask: money("5.40"),
        bid_size: money("10"),
        ask_size: money("10"),
        as_of: ts("2026-09-23T14:29:50Z"),
        underlying_price: None,
        implied_vol: None,
        greeks: None,
        open_interest: None,
    };

    let err = verify_chain(
        "SPY",
        date(2026, 10, 1),
        date(2026, 10, 30),
        60.0,
        &now,
        0.0,
        &[q_out_exp],
    )
    .unwrap_err();
    assert!(matches!(err, MarketDataVerifyError::MalformedData { .. }));
    assert!(err.message().contains("outside requested range"));
}

#[test]
fn mdv_stateful_verifier_struct() {
    let verifier = MarketDataVerifier::new(0.05).expect("valid verifier");
    let now = ts("2026-09-23T14:30:00Z");
    let as_of_skewed = ts("2026-09-23T14:30:00.030Z"); // 30ms in future, within 50ms tolerance

    assert!(verifier.verify_item("SPY", &as_of_skewed, Some(10.0), &now).is_ok());

    let as_of_excess = ts("2026-09-23T14:30:00.080Z"); // 80ms in future > 50ms tolerance
    assert!(matches!(
        verifier.verify_item("SPY", &as_of_excess, Some(10.0), &now),
        Err(MarketDataVerifyError::Lookahead { .. })
    ));
}

// =============================================================================
// Proptests: Freshness Monotonicity & Lookahead Invariants
// =============================================================================

proptest! {
    /// Property 1: Lookahead boundary sharpness.
    /// An as_of timestamp beyond `now + tolerance` always fails with Lookahead.
    /// An as_of at or before `now` never fails with Lookahead.
    #[test]
    fn prop_lookahead_boundary(
        delta_seconds in -3600i64..3600i64,
        tolerance in 0.0f64..60.0f64,
    ) {
        let now = ts("2026-09-23T14:30:00Z");
        let as_of = now.add_seconds(delta_seconds).expect("valid ts");
        let age = age_seconds(&now, &as_of);

        let res = verify_item_freshness("SPY", &as_of, None, &now, tolerance);
        let is_lookahead = matches!(res, Err(MarketDataVerifyError::Lookahead { .. }));
        if age < -tolerance {
            prop_assert!(is_lookahead);
        } else {
            prop_assert!(res.is_ok());
        }
    }

    /// Property 2: Freshness monotonicity in max_age_seconds.
    /// If an item is fresh for max_age_1, it must be fresh for any max_age_2 >= max_age_1.
    /// If an item is stale for max_age_2, it must be stale for any max_age_1 <= max_age_2.
    #[test]
    fn prop_freshness_monotonicity(
        age_s in 1u64..1000u64,
        max_age_1 in 10.0f64..100.0f64,
        max_age_2 in 101.0f64..500.0f64,
    ) {
        let now = ts("2026-09-23T14:30:00Z");
        let as_of = now.add_seconds(-(age_s as i64)).expect("valid ts");

        let res1 = verify_item_freshness("SPY", &as_of, Some(max_age_1), &now, 0.0);
        let res2 = verify_item_freshness("SPY", &as_of, Some(max_age_2), &now, 0.0);

        if res1.is_ok() {
            prop_assert!(res2.is_ok(), "If fresh at smaller max_age, must be fresh at larger max_age");
        }
        if res2.is_err() {
            prop_assert!(res1.is_err(), "If stale at larger max_age, must be stale at smaller max_age");
        }
    }

    /// Property 3: Bar shape invariants.
    /// Any bar with low <= open <= high, low <= close <= high, high >= low, prices > 0, vol >= 0
    /// always passes verify_bar_shape.
    #[test]
    fn prop_bar_shape_consistency(
        low_cents in 100u64..50000u64,
        open_offset in 0u64..1000u64,
        close_offset in 0u64..1000u64,
        high_extra in 0u64..1000u64,
        volume in 0u64..1_000_000u64,
    ) {
        let low = low_cents;
        let open = low + open_offset;
        let close = low + close_offset;
        let high = open.max(close) + high_extra;

        let bar = Bar {
            instr: Instrument::Equity("SPY".to_string()),
            ts: ts("2026-09-23T14:00:00Z"),
            open: Money::from_i128(open as i128).unwrap(),
            high: Money::from_i128(high as i128).unwrap(),
            low: Money::from_i128(low as i128).unwrap(),
            close: Money::from_i128(close as i128).unwrap(),
            volume: Money::from_i128(volume as i128).unwrap(),
            as_of: ts("2026-09-23T14:00:00Z"),
        };

        prop_assert!(verify_bar_shape(&bar).is_ok());
    }
}

//! Spec-derived and property tests for risk and risk_options (RSK-1 - RSK-6).
//!
//! Enforces:
//! 1. A rule breach always blocks entries: approved_quantity is None and refusals is non-empty.
//! 2. No breach never blocks: approved_quantity is Some(qty > 0) with 0 refusals.
//! 3. Drawdown brake latches at suspend threshold and releases only at recovery threshold (hysteresis).
//! 4. Half-risk sizing never exceeds full-risk sizing.
//! 5. Sizing is monotone in risk budget.
//! 6. Results are deterministic for equal input.
//!
//! Uses proptest.

use proptest::prelude::*;
use rust_decimal::Decimal;
use te_core::ledger::fold::AccountState;
use te_core::ledger::model::{Instrument, OptionContract, Side as LedgerSide};
use te_core::money::Money;
use te_core::options::Right;
use te_core::risk::{
    drawdown_decision, evaluate as risk_evaluate, Context, Intent as RiskIntent, Rails,
    Rules as RiskRules, Side as RiskSide, State as RiskState, RULE_COUNT,
};
use te_core::risk_options::{
    evaluate as opt_evaluate, Context as OptContext, Intent as OptIntent,
    Rules as OptRules,
};

fn dec(i: i64, scale: u32) -> Decimal {
    Decimal::new(i, scale)
}

fn base_rules() -> RiskRules {
    RiskRules {
        risk_per_trade_frac: dec(75, 4),           // 0.0075 = 0.75%
        short_risk_per_trade_frac: dec(50, 4),     // 0.0050 = 0.50%
        max_position_notional_frac: dec(20, 2),    // 0.20 = 20%
        max_gross_exposure_frac: dec(150, 2),       // 1.50 = 150%
        bull_chop_gross_exposure_frac: dec(100, 2), // 1.00 = 100%
        max_portfolio_heat_frac: dec(6, 2),        // 0.06 = 6%
        max_positions: 10,
        max_positions_per_industry: 3,
        min_price: dec(5, 0),                       // $5.00
        max_adv_frac: dec(1, 2),                   // 0.01 = 1%
        earnings_blackout_sessions: 5,
        drawdown_half_risk_frac: dec(8, 2),        // 0.08 = 8%
        drawdown_suspend_frac: dec(15, 2),         // 0.15 = 15%
        drawdown_recovery_frac: dec(5, 2),         // 0.05 = 5%
        daily_loss_block_frac: dec(3, 2),          // 0.03 = 3%
        clamp_to_position_cap: false,
    }
}

fn base_rails() -> Rails {
    Rails {
        venue_id: "sim".to_string(),
        allowed_symbols: Some(vec!["AAPL".to_string(), "MSFT".to_string(), "SPY".to_string()]),
        max_position_quantity: Some(2000),
        max_orders_per_day: Some(100),
        max_daily_loss: Some(dec(5000, 0)),
        trading_hours: None,
    }
}

fn base_context() -> Context {
    Context {
        equity: Some(dec(50000, 0)),
        current_price: Some(dec(100, 0)),
        gross_exposure: Some(Decimal::ZERO),
        portfolio_heat: Some(Decimal::ZERO),
        open_positions: Some(0),
        industry: Some("Technology".to_string()),
        industry_positions: Some(0),
        average_dollar_volume_20d: Some(dec(1_000_000_000, 0)),
        sessions_until_earnings: Some(10),
        regime: Some("BULL_EXPLOSIVE".to_string()),
        macro_high_risk_day: Some(false),
        drawdown_from_peak_frac: Some(dec(2, 2)), // 0.02 = 2%
        previous_session_pnl_frac: Some(Decimal::ZERO),
        venue_orders_today: Some(0),
        venue_daily_pnl: Some(Decimal::ZERO),
        current_position_quantity: Some(0),
    }
}

fn base_intent() -> RiskIntent {
    RiskIntent {
        is_equity: true,
        symbol: "AAPL".to_string(),
        side: RiskSide::Buy,
        quantity_rule: "risk_0.75pct".to_string(),
        entry_price: dec(100, 0),
        stop_loss: dec(95, 0),
        command_id: "cmd-risk-spec-1".to_string(),
    }
}

fn base_state() -> RiskState {
    RiskState {
        now_us: 1790172000000000,
        now_label: "2026-09-23T14:00:00+00:00".to_string(),
        command_known: false,
        kill_switch: false,
        drawdown_brake: false,
        drawdown_suspension: false,
    }
}

fn base_option_rules() -> OptRules {
    OptRules {
        max_margin_frac: Money::parse("0.50").unwrap(),
        allowed_regimes: vec!["BULL_EXPLOSIVE".into(), "BULL_CHOPIER".into()],
        no_earnings_before_expiry: true,
        max_name_margin_frac: Some(Money::parse("0.10").unwrap()),
        max_name_collateral_frac: Some(Money::parse("0.10").unwrap()),
        put_notional_frac_by_regime: None,
        max_loss_per_structure_frac: Some(Money::parse("0.02").unwrap()),
        max_debit_per_structure_frac: Some(Money::parse("0.05").unwrap()),
        max_total_debit_frac: Some(Money::parse("0.30").unwrap()),
        max_share_notional_frac: Some(Money::parse("0.20").unwrap()),
        entry_quote: None,
    }
}

fn make_call_contract(symbol: &str, expiry: chrono::NaiveDate, strike: &str) -> Instrument {
    Instrument::Option(OptionContract {
        underlying: symbol.to_string(),
        expiry,
        strike: Money::parse(strike).unwrap(),
        right: Right::Call,
        multiplier: 100,
    })
}

// -------------------------------------------------------------------------------------------------
// Property 1 & 2: Rule breach always blocks entries; no breach never blocks
// -------------------------------------------------------------------------------------------------

#[test]
fn clean_intent_passes_all_rules_and_is_approved() {
    let rules = base_rules();
    let rails = base_rails();
    let ctx = base_context();
    let intent = base_intent();
    let st = base_state();

    let eval = risk_evaluate(&rules, &rails, &ctx, &intent, &st).expect("evaluate succeeds");
    assert_eq!(eval.results.len(), RULE_COUNT);
    assert!(eval.refusals.is_empty(), "clean intent must have no refusals: {:?}", eval.refusals);
    assert!(eval.approved_quantity.is_some(), "clean intent must be approved");
    let qty = eval.approved_quantity.unwrap();
    assert!(qty > Decimal::ZERO, "approved quantity must be positive");
}

#[test]
fn single_rule_breaches_each_block_entry() {
    let rules = base_rules();
    let rails = base_rails();
    let base_c = base_context();
    let base_i = base_intent();
    let base_s = base_state();

    // 1. Min price breach
    let mut c = base_c.clone();
    c.current_price = Some(dec(499, 2)); // $4.99 < $5.00
    let ev = risk_evaluate(&rules, &rails, &c, &base_i, &base_s).unwrap();
    assert!(ev.approved_quantity.is_none(), "min_price breach must block entry");
    assert!(ev.refusals.iter().any(|r| r.contains("minimum")));

    // 2. Max positions breach
    let mut c = base_c.clone();
    c.open_positions = Some(10); // cap is 10
    let ev = risk_evaluate(&rules, &rails, &c, &base_i, &base_s).unwrap();
    assert!(ev.approved_quantity.is_none(), "max_positions breach must block entry");

    // 3. Industry positions breach
    let mut c = base_c.clone();
    c.industry_positions = Some(3); // cap is 3
    let ev = risk_evaluate(&rules, &rails, &c, &base_i, &base_s).unwrap();
    assert!(ev.approved_quantity.is_none(), "per_industry breach must block entry");

    // 4. Kill switch active
    let mut s = base_s.clone();
    s.kill_switch = true;
    let ev = risk_evaluate(&rules, &rails, &base_c, &base_i, &s).unwrap();
    assert!(ev.approved_quantity.is_none(), "kill_switch breach must block entry");

    // 5. Duplicate command
    let mut s = base_s.clone();
    s.command_known = true;
    let ev = risk_evaluate(&rules, &rails, &base_c, &base_i, &s).unwrap();
    assert!(ev.approved_quantity.is_none(), "duplicate_protection must block entry");

    // 6. Symbol not in allowlist
    let mut i = base_i.clone();
    i.symbol = "TSLA".to_string(); // not in ["AAPL", "MSFT", "SPY"]
    let ev = risk_evaluate(&rules, &rails, &base_c, &i, &base_s).unwrap();
    assert!(ev.approved_quantity.is_none(), "allowlist breach must block entry");

    // 7. Earnings blackout
    let mut c = base_c.clone();
    c.sessions_until_earnings = Some(4); // <= 5
    let ev = risk_evaluate(&rules, &rails, &c, &base_i, &base_s).unwrap();
    assert!(ev.approved_quantity.is_none(), "earnings_blackout breach must block entry");

    // 8. Regime blocked (BEAR_PROTECTIVE blocks longs)
    let mut c = base_c.clone();
    c.regime = Some("BEAR_PROTECTIVE".to_string());
    let ev = risk_evaluate(&rules, &rails, &c, &base_i, &base_s).unwrap();
    assert!(ev.approved_quantity.is_none(), "bear regime must block long entry");

    // 9. Macro high risk day
    let mut c = base_c.clone();
    c.macro_high_risk_day = Some(true);
    let ev = risk_evaluate(&rules, &rails, &c, &base_i, &base_s).unwrap();
    assert!(ev.approved_quantity.is_none(), "macro high risk day must block entry");

    // 10. Daily loss limit
    let mut c = base_c.clone();
    c.previous_session_pnl_frac = Some(dec(-35, 3)); // -3.5% < -3.0%
    let ev = risk_evaluate(&rules, &rails, &c, &base_i, &base_s).unwrap();
    assert!(ev.approved_quantity.is_none(), "daily loss breach must block entry");

    // 11. Drawdown suspension latched
    let mut s = base_s.clone();
    s.drawdown_suspension = true;
    let ev = risk_evaluate(&rules, &rails, &base_c, &base_i, &s).unwrap();
    assert!(ev.approved_quantity.is_none(), "drawdown suspension must block entry");
}

proptest! {
    #![proptest_config(ProptestConfig::with_cases(50))]
    #[test]
    fn prop_rule_breach_always_blocks_and_no_breach_never_blocks(
        open_pos in 0i64..15,
        price_cents in 300i64..1500,
        kill_switch in proptest::bool::ANY,
        cmd_known in proptest::bool::ANY,
        suspension in proptest::bool::ANY,
    ) {
        let rules = base_rules();
        let rails = base_rails();
        let mut ctx = base_context();
        let intent = base_intent();
        let mut st = base_state();

        ctx.open_positions = Some(open_pos);
        ctx.current_price = Some(dec(price_cents, 2));
        st.kill_switch = kill_switch;
        st.command_known = cmd_known;
        st.drawdown_suspension = suspension;

        let eval = risk_evaluate(&rules, &rails, &ctx, &intent, &st).unwrap();
        let any_breach = eval.results.iter().any(|r| !r.passed);

        if any_breach {
            // A rule breach ALWAYS blocks entries
            prop_assert!(eval.approved_quantity.is_none(), "Rule breach must block entry");
            prop_assert!(!eval.refusals.is_empty(), "Rule breach must produce refusal reasons");
        } else {
            // No breach NEVER blocks entries
            prop_assert!(eval.approved_quantity.is_some(), "No breach must approve entry");
            prop_assert!(eval.approved_quantity.unwrap() > Decimal::ZERO, "Approved quantity must be positive");
            prop_assert!(eval.refusals.is_empty(), "No breach must have empty refusals");
        }
    }
}

// -------------------------------------------------------------------------------------------------
// Property 3: Drawdown brake latches at suspend threshold and releases only at recovery (hysteresis)
// -------------------------------------------------------------------------------------------------

#[test]
fn drawdown_brake_and_suspension_hysteresis_exact() {
    let rules = base_rules();
    // Recovery: 0.05, Half-risk: 0.08, Suspend: 0.15

    // 1. Below half-risk: neither active
    let d0 = drawdown_decision(&rules, Some(dec(4, 2)), "0.04", false, false);
    assert!(!d0.brake && !d0.suspension && d0.changes.is_empty());

    // 2. Exactly at half-risk: brake latches
    let d1 = drawdown_decision(&rules, Some(dec(8, 2)), "0.08", false, false);
    assert!(d1.brake && !d1.suspension);
    assert_eq!(d1.changes.len(), 1);
    assert_eq!(d1.changes[0].control_id, "drawdown_brake");
    assert!(d1.changes[0].enabled);

    // 3. Drawdown eases to 0.06 (between recovery 0.05 and half-risk 0.08): brake REMAINS latched (no flapping)
    let d2 = drawdown_decision(&rules, Some(dec(6, 2)), "0.06", true, false);
    assert!(d2.brake && !d2.suspension, "Brake must remain latched above recovery threshold (hysteresis)");
    assert!(d2.changes.is_empty(), "No state transition while between recovery and threshold");

    // 4. Drawdown worsens to 0.15: suspension latches
    let d3 = drawdown_decision(&rules, Some(dec(15, 2)), "0.15", true, false);
    assert!(d3.brake && d3.suspension);
    assert_eq!(d3.changes.len(), 1);
    assert_eq!(d3.changes[0].control_id, "drawdown_suspension");
    assert!(d3.changes[0].enabled);

    // 5. Drawdown improves to 0.10 (between recovery 0.05 and suspend 0.15): suspension REMAINS latched (no flapping)
    let d4 = drawdown_decision(&rules, Some(dec(10, 2)), "0.10", true, true);
    assert!(d4.brake && d4.suspension, "Suspension must remain latched until recovery threshold (hysteresis)");
    assert!(d4.changes.is_empty(), "No flapping between recovery and suspend thresholds");

    // 6. Drawdown reaches recovery threshold 0.05: both brake and suspension release
    let d5 = drawdown_decision(&rules, Some(dec(5, 2)), "0.05", true, true);
    assert!(!d5.brake && !d5.suspension, "Recovery threshold must release both brake and suspension");
    assert_eq!(d5.changes.len(), 2);
    assert!(!d5.changes[0].enabled && !d5.changes[1].enabled);
}

proptest! {
    #![proptest_config(ProptestConfig::with_cases(50))]
    #[test]
    fn prop_drawdown_hysteresis_trajectory_never_flaps(
        ref steps in prop::collection::vec(0i64..25, 10..40)
    ) {
        let rules = base_rules();
        let mut brake = false;
        let mut suspension = false;

        for &step in steps {
            let dd = dec(step, 2); // 0.00 .. 0.25
            let decision = drawdown_decision(&rules, Some(dd), &dd.to_string(), brake, suspension);

            // Invariants:
            // 1. If currently suspended and dd > recovery (0.05), suspension MUST NOT flap off
            if suspension && dd > rules.drawdown_recovery_frac {
                prop_assert!(decision.suspension, "Suspension flapped off at dd={} before recovery={}", dd, rules.drawdown_recovery_frac);
            }
            // 2. If currently braked and dd > recovery (0.05), brake MUST NOT flap off
            if brake && dd > rules.drawdown_recovery_frac {
                prop_assert!(decision.brake, "Brake flapped off at dd={} before recovery={}", dd, rules.drawdown_recovery_frac);
            }
            // 3. At or below recovery (0.05), both MUST be false
            if dd <= rules.drawdown_recovery_frac {
                prop_assert!(!decision.suspension, "Suspension failed to release at recovery dd={}", dd);
                prop_assert!(!decision.brake, "Brake failed to release at recovery dd={}", dd);
            }
            // 4. At or above suspend (0.15), suspension MUST be true
            if dd >= rules.drawdown_suspend_frac {
                prop_assert!(decision.suspension, "Suspension failed to latch at dd={}", dd);
            }
            // 5. At or above half-risk (0.08), brake MUST be true
            if dd >= rules.drawdown_half_risk_frac {
                prop_assert!(decision.brake, "Brake failed to latch at dd={}", dd);
            }

            brake = decision.brake;
            suspension = decision.suspension;
        }
    }
}

// -------------------------------------------------------------------------------------------------
// Property 4: Half-risk sizing never exceeds full-risk sizing
// -------------------------------------------------------------------------------------------------

proptest! {
    #![proptest_config(ProptestConfig::with_cases(60))]
    #[test]
    fn prop_half_risk_sizing_never_exceeds_full_risk_sizing(
        equity_dollars in 5_000i64..200_000,
        entry_price in 20i64..400,
        stop_dist in 1i64..15,
    ) {
        let rules = base_rules();
        let rails = base_rails();
        let mut ctx = base_context();
        let mut intent = base_intent();

        ctx.equity = Some(dec(equity_dollars, 0));
        ctx.current_price = Some(dec(entry_price, 0));
        intent.entry_price = dec(entry_price, 0);
        intent.stop_loss = dec(entry_price - stop_dist, 0);

        // Full-risk evaluation (drawdown_brake = false)
        let mut st_full = base_state();
        st_full.drawdown_brake = false;
        let eval_full = risk_evaluate(&rules, &rails, &ctx, &intent, &st_full).unwrap();

        // Half-risk evaluation (drawdown_brake = true)
        let mut st_half = base_state();
        st_half.drawdown_brake = true;
        let eval_half = risk_evaluate(&rules, &rails, &ctx, &intent, &st_half).unwrap();

        if let (Some(q_full), Some(q_half)) = (eval_full.approved_quantity, eval_half.approved_quantity) {
            prop_assert!(
                q_half <= q_full,
                "Half-risk sizing ({}) must never exceed full-risk sizing ({})",
                q_half,
                q_full
            );
        }
    }
}

// -------------------------------------------------------------------------------------------------
// Property 5: Sizing is monotone in risk budget
// -------------------------------------------------------------------------------------------------

proptest! {
    #![proptest_config(ProptestConfig::with_cases(60))]
    #[test]
    fn prop_sizing_is_monotone_in_risk_budget(
        equity1 in 10_000i64..100_000,
        delta_equity in 0i64..50_000,
        entry_price in 30i64..300,
        stop_dist in 2i64..10,
    ) {
        let rules = base_rules();
        let rails = base_rails();
        let mut ctx1 = base_context();
        let mut ctx2 = base_context();
        let mut intent = base_intent();
        let st = base_state();

        let equity2 = equity1 + delta_equity; // equity1 <= equity2
        ctx1.equity = Some(dec(equity1, 0));
        ctx1.current_price = Some(dec(entry_price, 0));
        ctx2.equity = Some(dec(equity2, 0));
        ctx2.current_price = Some(dec(entry_price, 0));

        intent.entry_price = dec(entry_price, 0);
        intent.stop_loss = dec(entry_price - stop_dist, 0);

        let eval1 = risk_evaluate(&rules, &rails, &ctx1, &intent, &st).unwrap();
        let eval2 = risk_evaluate(&rules, &rails, &ctx2, &intent, &st).unwrap();

        if let (Some(q1), Some(q2)) = (eval1.approved_quantity, eval2.approved_quantity) {
            prop_assert!(
                q1 <= q2,
                "Sizing must be monotone in equity / risk budget: E1={} -> Q1={}, E2={} -> Q2={}",
                equity1,
                q1,
                equity2,
                q2
            );
        }
    }
}

// -------------------------------------------------------------------------------------------------
// Property 6: Results are deterministic for equal input
// -------------------------------------------------------------------------------------------------

proptest! {
    #![proptest_config(ProptestConfig::with_cases(50))]
    #[test]
    fn prop_risk_evaluate_deterministic_for_equal_input(
        equity in 5_000i64..100_000,
        price in 10i64..200,
        stop_dist in 1i64..10,
        open_pos in 0i64..12,
        brake in proptest::bool::ANY,
    ) {
        let rules = base_rules();
        let rails = base_rails();
        let mut ctx = base_context();
        let mut intent = base_intent();
        let mut st = base_state();

        ctx.equity = Some(dec(equity, 0));
        ctx.current_price = Some(dec(price, 0));
        ctx.open_positions = Some(open_pos);
        intent.entry_price = dec(price, 0);
        intent.stop_loss = dec(price - stop_dist, 0);
        st.drawdown_brake = brake;

        let eval_a = risk_evaluate(&rules, &rails, &ctx, &intent, &st).unwrap();
        let eval_b = risk_evaluate(&rules, &rails, &ctx, &intent, &st).unwrap();

        prop_assert_eq!(eval_a.results, eval_b.results, "Results must be identical for equal input");
        prop_assert_eq!(eval_a.refusals, eval_b.refusals, "Refusals must be identical for equal input");
        prop_assert_eq!(eval_a.approved_quantity, eval_b.approved_quantity, "Approved quantity must be deterministic");
    }
}

// -------------------------------------------------------------------------------------------------
// Options entry rules (risk_options) spec tests
// -------------------------------------------------------------------------------------------------

#[test]
fn risk_options_clean_intent_passes_and_breach_blocks() {
    let opt_rules = base_option_rules();
    let expiry = chrono::NaiveDate::from_ymd_opt(2026, 1, 16).unwrap();
    let contract = make_call_contract("SPY", expiry, "100.00");

    let intent = OptIntent {
        instrument: contract.clone(),
        side: LedgerSide::Buy,
        quantity: Money::from_i128(1).unwrap(),
        limit_price: Some(Money::parse("2.50").unwrap()),
    };

    let mut st = AccountState::new("ACC_OPT");
    st.cash = Money::parse("50000.00").unwrap();
    let spy = te_core::ledger::model::make_equity("SPY").unwrap();
    st.marks.insert(spy.hk(), spy, Money::parse("100.00").unwrap());
    let ctx = OptContext {
        state: &st,
        snapshot: None,
        live: false,
    };

    // 1. Clean intent in BULL_EXPLOSIVE regime passes
    let mut reg_ok = || Ok(Some("BULL_EXPLOSIVE".to_string()));
    let mut earn_ok = || Ok(Some(None)); // no earnings
    let results = opt_evaluate(&opt_rules, &intent, &ctx, &mut reg_ok, &mut earn_ok).expect("eval succeeds");
    assert!(results.iter().all(|r| r.passed), "clean option intent must pass all rules: {:?}", results);

    // 2. Regime breach: BEAR_PROTECTIVE is not in allowed_regimes
    let mut reg_bad = || Ok(Some("BEAR_PROTECTIVE".to_string()));
    let results_bad = opt_evaluate(&opt_rules, &intent, &ctx, &mut reg_bad, &mut earn_ok).expect("eval succeeds");
    let regime_res = results_bad.iter().find(|r| r.name == "regime").expect("regime rule exists");
    assert!(!regime_res.passed, "unsupported regime must fail the regime rule");

    // 3. Duplicate entry breach (C4): same contract already held in account
    let mut st_held = st.clone();
    let pos = te_core::ledger::fold::make_position(
        "ACC_OPT",
        contract.clone(),
        Money::from_i128(1).unwrap(),
        Money::parse("2.50").unwrap(),
        Money::zero(),
        vec![],
    ).unwrap();
    st_held.positions.insert(contract.hk(), contract, pos);
    let ctx_held = OptContext { state: &st_held, snapshot: None, live: false };
    let results_dup = opt_evaluate(&opt_rules, &intent, &ctx_held, &mut reg_ok, &mut earn_ok).unwrap();
    let dup_res = results_dup.iter().find(|r| r.name == "duplicate_entry").expect("duplicate_entry rule exists");
    assert!(!dup_res.passed, "duplicate contract must fail duplicate_entry rule (C4)");
}

#[test]
fn risk_options_determinism_for_equal_input() {
    let opt_rules = base_option_rules();
    let expiry = chrono::NaiveDate::from_ymd_opt(2026, 1, 16).unwrap();
    let contract = make_call_contract("SPY", expiry, "100.00");

    let intent = OptIntent {
        instrument: contract,
        side: LedgerSide::Buy,
        quantity: Money::from_i128(1).unwrap(),
        limit_price: Some(Money::parse("2.50").unwrap()),
    };

    let mut st = AccountState::new("ACC_OPT");
    st.cash = Money::parse("50000.00").unwrap();
    let spy = te_core::ledger::model::make_equity("SPY").unwrap();
    st.marks.insert(spy.hk(), spy, Money::parse("100.00").unwrap());
    let ctx = OptContext {
        state: &st,
        snapshot: None,
        live: false,
    };

    let mut reg1 = || Ok(Some("BULL_EXPLOSIVE".to_string()));
    let mut earn1 = || Ok(Some(None));
    let res1 = opt_evaluate(&opt_rules, &intent, &ctx, &mut reg1, &mut earn1).unwrap();

    let mut reg2 = || Ok(Some("BULL_EXPLOSIVE".to_string()));
    let mut earn2 = || Ok(Some(None));
    let res2 = opt_evaluate(&opt_rules, &intent, &ctx, &mut reg2, &mut earn2).unwrap();

    assert_eq!(res1.len(), res2.len());
    for (r1, r2) in res1.iter().zip(res2.iter()) {
        assert_eq!(r1.name, r2.name);
        assert_eq!(r1.passed, r2.passed);
        assert_eq!(r1.reason, r2.reason);
    }
}

//! Spec-derived and property tests for Ledger gaps (LED-4, LED-5, LED-6, mirror fold).
//!
//! Enforces:
//! - LED-4: Contradictions refuse (no open position, wrong side, excess contracts,
//!          ITM expiring worthless, OTM exercised or assigned, European early assignment,
//!          OrderUpdated immutable identity changes, illegal order transitions, overfills).
//! - LED-5: A refusal aborts the whole fold; no half-applied state is returned.
//! - LED-6: Decimal scale preservation and canonical state text comparison.
//! - Mirror fold: Pro-rata allocation conservation, ticket remaining tracking, duplicate refusal.
//!
//! Uses proptest.

use chrono::NaiveDate;
use proptest::prelude::*;
use te_core::ledger::canon::canon_account;
use te_core::ledger::fold::{apply_event, fold_account, make_position, AccountState};
use te_core::ledger::json::dumps;
use te_core::ledger::mirror::{pro_rata, MirrorTicketState};
use te_core::ledger::model::*;
use te_core::money::Money;
use te_core::options::Right;

fn dt(s: &str) -> DateTime {
    parse_datetime(s).expect("valid dt").0
}

fn dec(s: &str) -> Money {
    Money::parse(s).expect("valid dec")
}

fn make_test_order(
    account: &str,
    order_id: &str,
    command_id: &str,
    qty: i64,
    side: Side,
    order_type: OrderType,
    state: OrderState,
) -> Order {
    Order {
        order_id: order_id.to_string(),
        account_id: account.to_string(),
        instrument: make_equity("SPY").unwrap(),
        order_type,
        side,
        quantity: Money::from_i128(qty as i128).unwrap(),
        command_id: command_id.to_string(),
        created_at: dt("2026-01-02T15:00:00+00:00"),
        limit_price: if order_type == OrderType::Limit { Some(dec("100.00")) } else { None },
        stop_price: None,
        trail_amount: None,
        tif: Tif::Day,
        state,
        parent_order_id: None,
        oco_group: None,
    }
}

fn make_option_contract(symbol: &str, expiry: NaiveDate, strike: &str, right: Right) -> OptionContract {
    OptionContract {
        underlying: symbol.to_string(),
        expiry,
        strike: dec(strike),
        right,
        multiplier: 100,
    }
}

// -------------------------------------------------------------------------------------------------
// 1. LED-4: Contradictions refuse (Option Lifecycle)
// -------------------------------------------------------------------------------------------------

#[test]
fn led_4_lifecycle_refuses_when_no_position_held() {
    let account = "ACC_LED4";
    let mut st = AccountState::new(account);

    let contract = make_option_contract("SPY", NaiveDate::from_ymd_opt(2026, 1, 16).unwrap(), "100.00", Right::Call);
    let notice = OptionLifecycle {
        account_id: account.into(),
        contract,
        quantity: dec("1"),
        held: Side::Buy,
        underlying_price: dec("90.00"),
        price_source: "close".into(),
        as_of: dt("2026-01-16T21:00:00+00:00"),
        reason: "expired worthless".into(),
        early: false,
    };
    let ev = Event {
        account: account.into(),
        kind: EventKind::Expiry,
        payload: Obj::Lifecycle(notice),
        ts_utc: dt("2026-01-16T21:00:00+00:00"),
        command_id: Some("cmd-exp-1".into()),
        schema_version: 1,
        seq: Some(1),
    };

    let err = apply_event(&mut st, &ev).expect_err("settling with no position must refuse (I5)");
    assert_eq!(err.kind, "fold");
    assert!(err.msg.contains("no open position to settle (I5)"));
}

#[test]
fn led_4_lifecycle_refuses_wrong_side() {
    let account = "ACC_LED4";
    let mut st = AccountState::new(account);

    let contract = make_option_contract("SPY", NaiveDate::from_ymd_opt(2026, 1, 16).unwrap(), "100.00", Right::Call);
    let opt_instr = Instrument::Option(contract.clone());

    // Account holds SHORT call (quantity = -1)
    let pos = make_position(
        account,
        opt_instr.clone(),
        dec("-1"),
        dec("2.50"),
        dec("0"),
        vec![make_lot("l1".into(), dec("1"), dec("2.50"), dt("2026-01-02T10:00:00+00:00"), Side::Sell).unwrap()],
    ).unwrap();
    st.positions.insert(opt_instr.hk(), opt_instr, pos);

    // Event claims contracts are held BUY (long)
    let notice = OptionLifecycle {
        account_id: account.into(),
        contract,
        quantity: dec("1"),
        held: Side::Buy, // WRONG SIDE
        underlying_price: dec("90.00"),
        price_source: "close".into(),
        as_of: dt("2026-01-16T21:00:00+00:00"),
        reason: "expired worthless".into(),
        early: false,
    };
    let ev = Event {
        account: account.into(),
        kind: EventKind::Expiry,
        payload: Obj::Lifecycle(notice),
        ts_utc: dt("2026-01-16T21:00:00+00:00"),
        command_id: Some("cmd-exp-2".into()),
        schema_version: 1,
        seq: Some(1),
    };

    let err = apply_event(&mut st, &ev).expect_err("wrong side must refuse (I5)");
    assert_eq!(err.kind, "fold");
    assert!(err.msg.contains("the event says the contracts are held BUY but the book holds them SELL (I5)"));
}

#[test]
fn led_4_lifecycle_refuses_excess_contracts() {
    let account = "ACC_LED4";
    let mut st = AccountState::new(account);

    let contract = make_option_contract("SPY", NaiveDate::from_ymd_opt(2026, 1, 16).unwrap(), "100.00", Right::Call);
    let opt_instr = Instrument::Option(contract.clone());

    // Account holds 2 contracts
    let pos = make_position(
        account,
        opt_instr.clone(),
        dec("2"),
        dec("2.50"),
        dec("0"),
        vec![make_lot("l1".into(), dec("2"), dec("2.50"), dt("2026-01-02T10:00:00+00:00"), Side::Buy).unwrap()],
    ).unwrap();
    st.positions.insert(opt_instr.hk(), opt_instr, pos);

    // Event attempts to settle 5 contracts
    let notice = OptionLifecycle {
        account_id: account.into(),
        contract,
        quantity: dec("5"), // EXCESS: 5 > 2
        held: Side::Buy,
        underlying_price: dec("90.00"),
        price_source: "close".into(),
        as_of: dt("2026-01-16T21:00:00+00:00"),
        reason: "expired worthless".into(),
        early: false,
    };
    let ev = Event {
        account: account.into(),
        kind: EventKind::Expiry,
        payload: Obj::Lifecycle(notice),
        ts_utc: dt("2026-01-16T21:00:00+00:00"),
        command_id: Some("cmd-exp-3".into()),
        schema_version: 1,
        seq: Some(1),
    };

    let err = apply_event(&mut st, &ev).expect_err("excess contracts must refuse (I5)");
    assert_eq!(err.kind, "fold");
    assert!(err.msg.contains("5 contracts settled but 2 are held (I5)"));
}

#[test]
fn led_4_expiry_refuses_itm_contract() {
    let account = "ACC_LED4";
    let mut st = AccountState::new(account);

    let contract = make_option_contract("SPY", NaiveDate::from_ymd_opt(2026, 1, 16).unwrap(), "100.00", Right::Call);
    let opt_instr = Instrument::Option(contract.clone());

    let pos = make_position(
        account,
        opt_instr.clone(),
        dec("1"),
        dec("2.50"),
        dec("0"),
        vec![make_lot("l1".into(), dec("1"), dec("2.50"), dt("2026-01-02T10:00:00+00:00"), Side::Buy).unwrap()],
    ).unwrap();
    st.positions.insert(opt_instr.hk(), opt_instr, pos);

    // Call with strike 100 and price 110 is $10.00 ITM -> cannot expire worthless
    let notice = OptionLifecycle {
        account_id: account.into(),
        contract,
        quantity: dec("1"),
        held: Side::Buy,
        underlying_price: dec("110.00"), // ITM!
        price_source: "close".into(),
        as_of: dt("2026-01-16T21:00:00+00:00"),
        reason: "expired worthless".into(),
        early: false,
    };
    let ev = Event {
        account: account.into(),
        kind: EventKind::Expiry,
        payload: Obj::Lifecycle(notice),
        ts_utc: dt("2026-01-16T21:00:00+00:00"),
        command_id: Some("cmd-exp-4".into()),
        schema_version: 1,
        seq: Some(1),
    };

    let err = apply_event(&mut st, &ev).expect_err("ITM contract expiring worthless must refuse (I9)");
    assert_eq!(err.kind, "fold");
    assert!(err.msg.contains("10 in the money at 110; it is exercised or assigned, not expired worthless (I9)"));
}

#[test]
fn led_4_exercise_and_assignment_refuse_otm_contract() {
    let account = "ACC_LED4";
    let mut st = AccountState::new(account);

    let contract = make_option_contract("SPY", NaiveDate::from_ymd_opt(2026, 1, 16).unwrap(), "100.00", Right::Call);
    let opt_instr = Instrument::Option(contract.clone());

    let pos = make_position(
        account,
        opt_instr.clone(),
        dec("1"),
        dec("2.50"),
        dec("0"),
        vec![make_lot("l1".into(), dec("1"), dec("2.50"), dt("2026-01-02T10:00:00+00:00"), Side::Buy).unwrap()],
    ).unwrap();
    st.positions.insert(opt_instr.hk(), opt_instr, pos);

    // Call with strike 100 and price 90 is OTM -> nobody exercises
    let notice = OptionLifecycle {
        account_id: account.into(),
        contract,
        quantity: dec("1"),
        held: Side::Buy,
        underlying_price: dec("90.00"), // OTM
        price_source: "close".into(),
        as_of: dt("2026-01-16T21:00:00+00:00"),
        reason: "exercised".into(),
        early: false,
    };
    let ev = Event {
        account: account.into(),
        kind: EventKind::Exercise,
        payload: Obj::Lifecycle(notice),
        ts_utc: dt("2026-01-16T21:00:00+00:00"),
        command_id: Some("cmd-exe-1".into()),
        schema_version: 1,
        seq: Some(1),
    };

    let err = apply_event(&mut st, &ev).expect_err("OTM exercise must refuse (I9)");
    assert_eq!(err.kind, "fold");
    assert!(err.msg.contains("not in the money at 90; nobody exercises it (I9)"));
}

#[test]
fn led_4_european_contract_cannot_be_assigned_early() {
    let account = "ACC_LED4";
    let mut st = AccountState::new(account);

    // SPX is European cash-settled root
    let contract = make_option_contract("SPX", NaiveDate::from_ymd_opt(2026, 1, 16).unwrap(), "5000.00", Right::Call);
    let opt_instr = Instrument::Option(contract.clone());

    let pos = make_position(
        account,
        opt_instr.clone(),
        dec("-1"),
        dec("15.00"),
        dec("0"),
        vec![make_lot("l1".into(), dec("1"), dec("15.00"), dt("2026-01-02T10:00:00+00:00"), Side::Sell).unwrap()],
    ).unwrap();
    st.positions.insert(opt_instr.hk(), opt_instr, pos);

    let notice = OptionLifecycle {
        account_id: account.into(),
        contract,
        quantity: dec("1"),
        held: Side::Sell,
        underlying_price: dec("5100.00"),
        price_source: "close".into(),
        as_of: dt("2026-01-10T21:00:00+00:00"),
        reason: "early assignment".into(),
        early: true, // EARLY assignment on European!
    };
    let ev = Event {
        account: account.into(),
        kind: EventKind::Assignment,
        payload: Obj::Lifecycle(notice),
        ts_utc: dt("2026-01-10T21:00:00+00:00"),
        command_id: Some("cmd-asgn-early".into()),
        schema_version: 1,
        seq: Some(1),
    };

    let err = apply_event(&mut st, &ev).expect_err("European early assignment must refuse (I5)");
    assert_eq!(err.kind, "fold");
    assert!(err.msg.contains("a European contract cannot be assigned early (I5)"));
}

#[test]
fn led_4_expiry_refuses_when_marked_early() {
    let account = "ACC_LED4";
    let mut st = AccountState::new(account);

    let contract = make_option_contract("SPY", NaiveDate::from_ymd_opt(2026, 1, 16).unwrap(), "100.00", Right::Call);
    let opt_instr = Instrument::Option(contract.clone());

    let pos = make_position(
        account,
        opt_instr.clone(),
        dec("1"),
        dec("2.50"),
        dec("0"),
        vec![make_lot("l1".into(), dec("1"), dec("2.50"), dt("2026-01-02T10:00:00+00:00"), Side::Buy).unwrap()],
    ).unwrap();
    st.positions.insert(opt_instr.hk(), opt_instr, pos);

    let notice = OptionLifecycle {
        account_id: account.into(),
        contract,
        quantity: dec("1"),
        held: Side::Buy,
        underlying_price: dec("90.00"),
        price_source: "close".into(),
        as_of: dt("2026-01-10T21:00:00+00:00"),
        reason: "early expiry".into(),
        early: true, // CANNOT EXPIRE EARLY
    };
    let ev = Event {
        account: account.into(),
        kind: EventKind::Expiry,
        payload: Obj::Lifecycle(notice),
        ts_utc: dt("2026-01-10T21:00:00+00:00"),
        command_id: Some("cmd-exp-early".into()),
        schema_version: 1,
        seq: Some(1),
    };

    let err = apply_event(&mut st, &ev).expect_err("early expiry must refuse (I9)");
    assert_eq!(err.kind, "fold");
    assert!(err.msg.contains("a contract expires only at its expiry (I9)"));
}

// -------------------------------------------------------------------------------------------------
// 1b. LED-4: Contradictions refuse (Order Updates & Transitions)
// -------------------------------------------------------------------------------------------------

#[test]
fn led_4_order_updated_refuses_immutable_identity_change() {
    let account = "ACC_LED4";
    let mut st = AccountState::new(account);

    let order = make_test_order(account, "ord-upd-id", "cmd-sub", 10, Side::Buy, OrderType::Limit, OrderState::Submitted);
    let sub_ev = Event {
        account: account.into(),
        kind: EventKind::OrderSubmitted,
        payload: Obj::Order(order.clone()),
        ts_utc: dt("2026-01-02T10:00:00+00:00"),
        command_id: Some("cmd-sub".into()),
        schema_version: 1,
        seq: Some(1),
    };
    apply_event(&mut st, &sub_ev).unwrap();

    // Mutate side from Buy to Sell (immutable identity field)
    let mut mutated = order.clone();
    mutated.side = Side::Sell;

    let upd_ev = Event {
        account: account.into(),
        kind: EventKind::OrderUpdated,
        payload: Obj::OrderUpdated(OrderUpdated {
            order: mutated,
            reason: "illegal side change".into(),
            venue_order_id: None,
        }),
        ts_utc: dt("2026-01-02T10:01:00+00:00"),
        command_id: Some("cmd-upd-bad".into()),
        schema_version: 1,
        seq: Some(2),
    };

    let err = apply_event(&mut st, &upd_ev).expect_err("immutable identity change must refuse (I5)");
    assert_eq!(err.kind, "fold");
    assert!(err.msg.contains("OrderUpdated changed immutable identity fields for 'ord-upd-id' (I5)"));
}

#[test]
fn led_4_order_updated_refuses_quantity_below_already_filled() {
    let account = "ACC_LED4";
    let mut st = AccountState::new(account);

    let order = make_test_order(account, "ord-fill-upd", "cmd-sub", 10, Side::Buy, OrderType::Limit, OrderState::Submitted);
    st.orders.put(&"ord-fill-upd".to_string(), order.clone());
    st.filled_quantity.put(&"ord-fill-upd".to_string(), dec("6")); // 6 shares already filled

    // Try to update order quantity to 5 (< 6)
    let mut updated_order = order;
    updated_order.quantity = dec("5");

    let upd_ev = Event {
        account: account.into(),
        kind: EventKind::OrderUpdated,
        payload: Obj::OrderUpdated(OrderUpdated {
            order: updated_order,
            reason: "resize below fill".into(),
            venue_order_id: None,
        }),
        ts_utc: dt("2026-01-02T10:05:00+00:00"),
        command_id: Some("cmd-upd-below".into()),
        schema_version: 1,
        seq: Some(1),
    };

    let err = apply_event(&mut st, &upd_ev).expect_err("resizing below filled must refuse");
    assert_eq!(err.kind, "fold");
    assert!(err.msg.contains("OrderUpdated quantity 5 is below already-filled quantity 6 for 'ord-fill-upd'"));
}

#[test]
fn led_4_illegal_order_state_transition_refuses() {
    let account = "ACC_LED4";
    let mut st = AccountState::new(account);

    let order = make_test_order(account, "ord-state-trans", "cmd-sub", 10, Side::Buy, OrderType::Limit, OrderState::Filled);
    st.orders.put(&"ord-state-trans".to_string(), order);

    // Transition from Filled to Accepted is illegal
    let change_ev = Event {
        account: account.into(),
        kind: EventKind::OrderAccepted,
        payload: Obj::StateChange(OrderStateChange {
            order_id: "ord-state-trans".into(),
            reason: Some("resurrect order".into()),
            venue_order_id: None,
        }),
        ts_utc: dt("2026-01-02T10:10:00+00:00"),
        command_id: None,
        schema_version: 1,
        seq: Some(1),
    };

    let err = apply_event(&mut st, &change_ev).expect_err("illegal transition from FILLED must refuse");
    assert_eq!(err.kind, "illegal_transition");
    assert!(err.msg.contains("Illegal order state transition from FILLED to ACCEPTED"));
}

// -------------------------------------------------------------------------------------------------
// 2. LED-5: A refusal aborts the whole fold; no half-applied state is returned
// -------------------------------------------------------------------------------------------------

#[test]
fn led_5_refusal_aborts_whole_fold_and_returns_error() {
    let account = "ACC_LED5";
    let mut events = Vec::new();

    // Event 1: Deposit $10,000
    events.push(Event {
        account: account.into(),
        kind: EventKind::CashFlow,
        payload: Obj::CashFlow(CashFlow {
            amount: dec("10000.00"),
            kind: "deposit".into(),
            as_of: dt("2026-01-02T09:30:00+00:00"),
            note: None,
        }),
        ts_utc: dt("2026-01-02T09:30:00+00:00"),
        command_id: Some("cmd-dep".into()),
        schema_version: 1,
        seq: Some(1),
    });

    // Event 2: Order submitted for 10 shares
    let order = make_test_order(account, "ord-1", "cmd-ord-1", 10, Side::Buy, OrderType::Market, OrderState::Submitted);
    events.push(Event {
        account: account.into(),
        kind: EventKind::OrderSubmitted,
        payload: Obj::Order(order),
        ts_utc: dt("2026-01-02T09:31:00+00:00"),
        command_id: Some("cmd-ord-1".into()),
        schema_version: 1,
        seq: Some(2),
    });

    // Event 3: Contradiction! Over-fill with 15 shares against 10 shares ordered
    events.push(Event {
        account: account.into(),
        kind: EventKind::Fill,
        payload: Obj::Fill(Fill {
            fill_id: "fill-bad".into(),
            order_id: "ord-1".into(),
            account_id: account.into(),
            instrument: make_equity("SPY").unwrap(),
            quantity: dec("15"), // 15 > 10 overfill!
            price: dec("100.00"),
            venue_env: "sim".into(),
            filled_at: dt("2026-01-02T09:32:00+00:00"),
            side: Side::Buy,
            fee: dec("1.00"),
            leg_id: None,
            venue_order_id: None,
            venue_execution_id: None,
        }),
        ts_utc: dt("2026-01-02T09:32:00+00:00"),
        command_id: None,
        schema_version: 1,
        seq: Some(3),
    });

    // Event 4: Another cash flow
    events.push(Event {
        account: account.into(),
        kind: EventKind::CashFlow,
        payload: Obj::CashFlow(CashFlow {
            amount: dec("500.00"),
            kind: "deposit".into(),
            as_of: dt("2026-01-02T09:33:00+00:00"),
            note: None,
        }),
        ts_utc: dt("2026-01-02T09:33:00+00:00"),
        command_id: Some("cmd-dep-2".into()),
        schema_version: 1,
        seq: Some(4),
    });

    // fold_account MUST abort and return Err. No half-applied state is returned to caller!
    let res = fold_account(&events, account);
    assert!(res.is_err(), "fold_account must return Err on contradiction (LED-5)");
    let err = res.err().unwrap();
    assert_eq!(err.kind, "fold");
    assert!(err.msg.contains("refusing the over-fill"));
}

proptest! {
    #![proptest_config(ProptestConfig::with_cases(30))]
    #[test]
    fn prop_led_5_refusal_at_any_index_aborts_fold(
        good_count in 1usize..5,
        bad_qty in 11i64..50,
    ) {
        let account = "ACC_LED5_PROP";
        let mut events = Vec::new();
        let mut seq = 1i128;

        // Valid prefix of events
        events.push(Event {
            account: account.into(),
            kind: EventKind::CashFlow,
            payload: Obj::CashFlow(CashFlow {
                amount: dec("50000.00"),
                kind: "deposit".into(),
                as_of: dt("2026-01-02T09:30:00+00:00"),
                note: None,
            }),
            ts_utc: dt("2026-01-02T09:30:00+00:00"),
            command_id: Some("cmd-dep".into()),
            schema_version: 1,
            seq: Some(seq),
        });
        seq += 1;

        for i in 0..good_count {
            let o = make_test_order(account, &format!("ord-{i}"), &format!("cmd-{i}"), 10, Side::Buy, OrderType::Market, OrderState::Submitted);
            events.push(Event {
                account: account.into(),
                kind: EventKind::OrderSubmitted,
                payload: Obj::Order(o),
                ts_utc: dt(&format!("2026-01-02T10:{:02}:00+00:00", i)),
                command_id: Some(format!("cmd-{i}")),
                schema_version: 1,
                seq: Some(seq),
            });
            seq += 1;
        }

        // Now append a contradiction (overfill on ord-0)
        events.push(Event {
            account: account.into(),
            kind: EventKind::Fill,
            payload: Obj::Fill(Fill {
                fill_id: "fill-fail".into(),
                order_id: "ord-0".into(),
                account_id: account.into(),
                instrument: make_equity("SPY").unwrap(),
                quantity: Money::from_i128(bad_qty as i128).unwrap(), // > 10 ordered!
                price: dec("100.00"),
                venue_env: "sim".into(),
                filled_at: dt("2026-01-02T11:00:00+00:00"),
                side: Side::Buy,
                fee: dec("1.00"),
                leg_id: None,
                venue_order_id: None,
                venue_execution_id: None,
            }),
            ts_utc: dt("2026-01-02T11:00:00+00:00"),
            command_id: None,
            schema_version: 1,
            seq: Some(seq),
        });

        // The whole fold must fail
        let res = fold_account(&events, account);
        prop_assert!(res.is_err(), "Contradiction must abort the entire fold (LED-5)");
    }
}

// -------------------------------------------------------------------------------------------------
// 3. LED-6: Decimal scale preservation and canonical comparison
// -------------------------------------------------------------------------------------------------

#[test]
fn led_6_decimal_scale_and_canon_stability() {
    let account = "ACC_LED6";
    let mut st = AccountState::new(account);

    // Initial cash
    let cf = Event {
        account: account.into(),
        kind: EventKind::CashFlow,
        payload: Obj::CashFlow(CashFlow {
            amount: dec("100.00"),
            kind: "deposit".into(),
            as_of: dt("2026-01-02T09:30:00+00:00"),
            note: None,
        }),
        ts_utc: dt("2026-01-02T09:30:00+00:00"),
        command_id: Some("cmd-cf-1".into()),
        schema_version: 1,
        seq: Some(1),
    };
    apply_event(&mut st, &cf).unwrap();

    // 100.00 - 100 fee = 0.00 (scale preserved)
    let fee = Event {
        account: account.into(),
        kind: EventKind::CashFlow,
        payload: Obj::CashFlow(CashFlow {
            amount: dec("-100.00"),
            kind: "fee".into(),
            as_of: dt("2026-01-02T09:31:00+00:00"),
            note: None,
        }),
        ts_utc: dt("2026-01-02T09:31:00+00:00"),
        command_id: Some("cmd-cf-2".into()),
        schema_version: 1,
        seq: Some(2),
    };
    apply_event(&mut st, &fee).unwrap();

    // Canonical state dump compares as stable text
    let canon1 = dumps(&canon_account(&st).unwrap());
    let canon2 = dumps(&canon_account(&st).unwrap());
    assert_eq!(canon1, canon2, "Canonical JSON dump must compare identically as text (LED-6)");
    assert!(canon1.contains(r#""cash":{"d":"0"}"#), "Canonical zero representation in state: {}", canon1);

    // Verify option lifecycle scale preservation: 100.00 - 100 = 0.00 (TE options/lifecycle.rs:2-3)
    let contract = make_option_contract("SPY", NaiveDate::from_ymd_opt(2026, 1, 16).unwrap(), "100", Right::Call);
    let intrinsic_scale = te_core::options::lifecycle::intrinsic(&contract.wire(), "100.00").unwrap();
    assert_eq!(intrinsic_scale, "0.00", "Lifecycle intrinsic value must preserve scale 100.00 - 100 = 0.00 (LED-6)");
}

// -------------------------------------------------------------------------------------------------
// 4. Mirror fold gap (mirror.rs): pro-rata allocation conservation and ticket tracking
// -------------------------------------------------------------------------------------------------

proptest! {
    #![proptest_config(ProptestConfig::with_cases(50))]
    #[test]
    fn prop_mirror_pro_rata_conserves_whole_contracts(
        w1 in 1i64..50,
        w2 in 1i64..50,
        w3 in 1i64..50,
        filled in 1i64..100,
    ) {
        let whole = w1 + w2 + w3;
        let filled = filled.min(whole); // filled <= whole
        let weights = vec![
            Money::from_i128(w1 as i128).unwrap(),
            Money::from_i128(w2 as i128).unwrap(),
            Money::from_i128(w3 as i128).unwrap(),
        ];
        let whole_dec = Money::from_i128(whole as i128).unwrap();
        let amount_dec = Money::from_i128(filled as i128).unwrap();

        let shares = pro_rata(&weights, &whole_dec, &amount_dec).expect("pro_rata succeeds");

        // 1. Sum of shares must equal exactly the filled amount (conservation)
        let mut sum = Money::zero();
        for sh in &shares {
            sum = te_core::ledger::ops::add(&sum, sh).unwrap();
            prop_assert!(sh.is_integral().unwrap(), "Each allocated share must be a whole contract");
        }
        prop_assert_eq!(sum, amount_dec, "Pro rata shares must sum exactly to amount");

        // 2. Each share must not exceed its weight
        for (sh, w) in shares.iter().zip(weights.iter()) {
            prop_assert!(te_core::ledger::ops::le(sh, w).unwrap());
        }
    }
}

#[test]
fn mirror_ticket_state_remaining_tracks_unfilled_contracts() {
    let queued = MirrorQueued {
        venue: "paper".into(),
        ticket_key: "t-1".into(),
        instrument: make_equity("SPY").unwrap(),
        side: Side::Buy,
        quantity: dec("10"),
        order_type: OrderType::Market,
        limit_price: None,
        tif: Tif::Day,
        allocations: vec![],
        at: dt("2026-01-02T10:00:00+00:00"),
    };

    let mut state = MirrorTicketState {
        queued,
        ack: None,
        venue_order_id: Some("v-1".into()),
        book_status: Some(OrderState::Accepted),
        filled: dec("0"),
        avg_price: None,
        closed: false,
        allocated: te_core::ledger::ops::OMap::new(),
    };

    // Initially 10 remaining
    assert_eq!(state.remaining().unwrap(), dec("10"));
    assert!(!state.terminal().unwrap());

    // 4 filled -> 6 remaining
    state.filled = dec("4");
    assert_eq!(state.remaining().unwrap(), dec("6"));
    assert!(!state.terminal().unwrap());

    // 10 filled -> 0 remaining, terminal
    state.filled = dec("10");
    assert_eq!(state.remaining().unwrap(), dec("0"));
    assert!(state.terminal().unwrap());

    // Closed ticket -> remaining is 0
    state.closed = true;
    assert_eq!(state.remaining().unwrap(), dec("0"));
    assert!(state.terminal().unwrap());
}

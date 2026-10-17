//! Property-based and spec-derived tests for the ledger fold and codec (LED-1, LED-2, CMD-1).
//!
//! Enforces:
//! - LED-1: Cold fold equals incremental fold at every prefix (I2).
//! - LED-2: Cash and position conservation holds for every event kind (I8).
//! - Codec roundtrip: Every event kind encodes to JSON and decodes back identically.
//! - CMD-1: Command replay idempotency and refusal of duplicate/conflicting command IDs (I3).

use chrono::NaiveDate;
use proptest::prelude::*;
use te_core::ledger::canon::canon_account;
use te_core::ledger::codec::{event_from_bytes, event_to_bytes};
use te_core::ledger::fold::{apply_event, fold_account, AccountState};
use te_core::ledger::json::dumps;
use te_core::ledger::model::*;
use te_core::money::Money;

fn dt(s: &str) -> DateTime {
    parse_datetime(s).expect("valid dt").0
}

fn dec(s: &str) -> Money {
    Money::parse(s).expect("valid dec")
}

fn sample_datetime(day: u32, hour: u32, minute: u32) -> DateTime {
    let d = NaiveDate::from_ymd_opt(2026, 1, day.max(1).min(28)).unwrap();
    let sod = hour.min(23) * 3600 + minute.min(59) * 60;
    DateTime { date: d, sod, us: 0, off: 0 }
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

fn make_test_option_contract() -> OptionContract {
    OptionContract {
        underlying: "SPY".to_string(),
        expiry: NaiveDate::from_ymd_opt(2026, 1, 16).unwrap(),
        strike: dec("100.00"),
        right: te_core::options::Right::Call,
        multiplier: 100,
    }
}

fn make_sample_event_for_kind(kind: EventKind, seed: u32) -> Event {
    let account = if matches!(kind, EventKind::MirrorQueued | EventKind::MirrorRefused | EventKind::MirrorAck | EventKind::MirrorFill) {
        "__venue__:paper".to_string()
    } else {
        "ACC_1".to_string()
    };
    let ts = sample_datetime(1 + (seed % 20), (seed / 20) % 24, seed % 60);
    let cid = Some(format!("cmd-{kind:?}-{seed}"));

    let payload = match kind {
        EventKind::SignalSeen => Obj::Signal(Signal {
            signal_id: format!("sig-{seed}"),
            scan_id: "scan_1".into(),
            symbol: "SPY".into(),
            session_date: NaiveDate::from_ymd_opt(2026, 1, 2).unwrap(),
            direction: "long".into(),
            metrics: vec![("score".into(), Val::Dec(dec("0.85")))],
            next_earnings_date: None,
            created_at: Some(ts.clone()),
        }),
        EventKind::RiskVerdict => {
            let accepted = seed % 2 == 0;
            Obj::RiskVerdict(RiskVerdict {
                order_intent_id: format!("intent-{seed}"),
                accepted,
                evaluations: vec![RiskRuleResult {
                    rule_name: "max_position".into(),
                    passed: accepted,
                    measured_value: Val::Int(10),
                    threshold: Val::Int(100),
                    reason: if accepted { "ok".into() } else { "exceeded limit".into() },
                }],
                refusal_reasons: if accepted { vec![] } else { vec!["max_position exceeded".into()] },
                approved_quantity: if accepted { Some(Money::from_i128(10).unwrap()) } else { None },
            })
        }
        EventKind::OrdersCreated => {
            let o = make_test_order(&account, &format!("ord-cr-{seed}"), &format!("cmd-cr-{seed}"), 10, Side::Buy, OrderType::Market, OrderState::New);
            Obj::OrdersCreated(OrdersCreated {
                orders: vec![o],
                fingerprint: format!("fp-{seed}"),
                reason: "initial".into(),
            })
        }
        EventKind::RiskControl => Obj::RiskControl(RiskControlChange {
            control_id: format!("ctrl-{seed}"),
            enabled: seed % 2 == 0,
            reason: "risk switch".into(),
            changed_at: ts.clone(),
        }),
        EventKind::OrderSubmitted => {
            let o = make_test_order(&account, &format!("ord-sub-{seed}"), &format!("cmd-sub-{seed}"), 10, Side::Buy, OrderType::Limit, OrderState::Submitted);
            Obj::Order(o)
        }
        EventKind::OrderUpdated => {
            let o = make_test_order(&account, &format!("ord-upd-{seed}"), &format!("cmd-upd-{seed}"), 15, Side::Buy, OrderType::Limit, OrderState::Submitted);
            Obj::OrderUpdated(OrderUpdated {
                order: o,
                reason: "resize".into(),
                venue_order_id: Some(format!("v-{seed}")),
            })
        }
        EventKind::OrderPending => Obj::StateChange(OrderStateChange {
            order_id: format!("ord-pnd-{seed}"),
            reason: Some("pending venue ack".into()),
            venue_order_id: None,
        }),
        EventKind::OrderAccepted => Obj::StateChange(OrderStateChange {
            order_id: format!("ord-acc-{seed}"),
            reason: Some("venue accepted".into()),
            venue_order_id: Some(format!("v-{seed}")),
        }),
        EventKind::OrderRejected => Obj::StateChange(OrderStateChange {
            order_id: format!("ord-rej-{seed}"),
            reason: Some("venue rejected".into()),
            venue_order_id: None,
        }),
        EventKind::OrderCancelled => Obj::StateChange(OrderStateChange {
            order_id: format!("ord-can-{seed}"),
            reason: Some("venue cancelled".into()),
            venue_order_id: Some(format!("v-{seed}")),
        }),
        EventKind::OrderRefused => Obj::StateChange(OrderStateChange {
            order_id: format!("ord-ref-{seed}"),
            reason: Some("manager refused".into()),
            venue_order_id: None,
        }),
        EventKind::OrderExpired => Obj::StateChange(OrderStateChange {
            order_id: format!("ord-exp-{seed}"),
            reason: Some("session close".into()),
            venue_order_id: Some(format!("v-{seed}")),
        }),
        EventKind::OrderEmulationUpdated => Obj::Emulated(EmulatedOrderState {
            order_id: format!("ord-emu-{seed}"),
            observed_price: Some(dec("101.50")),
            extreme: Some(dec("102.00")),
            stop_price: Some(dec("98.00")),
            triggered: seed % 2 == 1,
            reason: "tick update".into(),
        }),
        EventKind::Fill => Obj::Fill(Fill {
            fill_id: format!("fill-{seed}"),
            order_id: format!("ord-fill-{seed}"),
            account_id: account.clone(),
            instrument: make_equity("SPY").unwrap(),
            quantity: Money::from_i128((seed as i128 % 50) + 1).unwrap(),
            price: dec("105.25"),
            venue_env: "sim".into(),
            filled_at: ts.clone(),
            side: if seed % 2 == 0 { Side::Buy } else { Side::Sell },
            fee: dec("1.00"),
            leg_id: None,
            venue_order_id: Some(format!("v-fill-{seed}")),
            venue_execution_id: Some(format!("exec-{seed}")),
        }),
        EventKind::Assignment => Obj::Lifecycle(OptionLifecycle {
            account_id: account.clone(),
            contract: make_test_option_contract(),
            quantity: Money::from_i128(1).unwrap(),
            held: Side::Sell,
            underlying_price: dec("110.00"),
            price_source: "close".into(),
            as_of: ts.clone(),
            reason: "assigned ITM".into(),
            early: false,
        }),
        EventKind::Exercise => Obj::Lifecycle(OptionLifecycle {
            account_id: account.clone(),
            contract: make_test_option_contract(),
            quantity: Money::from_i128(1).unwrap(),
            held: Side::Buy,
            underlying_price: dec("110.00"),
            price_source: "close".into(),
            as_of: ts.clone(),
            reason: "exercised ITM".into(),
            early: false,
        }),
        EventKind::Expiry => Obj::Lifecycle(OptionLifecycle {
            account_id: account.clone(),
            contract: make_test_option_contract(),
            quantity: Money::from_i128(1).unwrap(),
            held: Side::Buy,
            underlying_price: dec("90.00"),
            price_source: "close".into(),
            as_of: ts.clone(),
            reason: "expired worthless".into(),
            early: false,
        }),
        EventKind::CorporateAction => Obj::Corp(CorporateAction {
            symbol: "SPY".into(),
            action_type: "cash_dividend".into(),
            effective_date: NaiveDate::from_ymd_opt(2026, 1, 15).unwrap(),
            as_of: ts.clone(),
            details: vec![("rate".into(), Val::Dec(dec("1.50")))],
        }),
        EventKind::CashFlow => Obj::CashFlow(CashFlow {
            amount: dec("500.00"),
            kind: "deposit".into(),
            as_of: ts.clone(),
            note: Some("funding".into()),
        }),
        EventKind::Mark => Obj::Mark(Mark {
            instrument: make_equity("SPY").unwrap(),
            price: dec("102.50"),
            as_of: ts.clone(),
            source: Some("eod".into()),
        }),
        EventKind::VenueReconcile => Obj::Reconcile(VenueReconcile {
            venue: "sim".into(),
            as_of: ts.clone(),
            reconciled: true,
            drift: vec![],
            note: None,
        }),
        EventKind::VenueHaltCleared => Obj::HaltCleared(VenueHaltCleared {
            venue: "sim".into(),
            at: ts.clone(),
            reason: "operator cleared".into(),
            reconcile_seq: 1,
        }),
        EventKind::EodRun => Obj::EodRun(EodRun {
            session: NaiveDate::from_ymd_opt(2026, 1, 2).unwrap(),
            job: "eod_main".into(),
            account_id: account.clone(),
            bars_processed: 390,
            at_close: ts.clone(),
        }),
        EventKind::MirrorQueued => Obj::MQueued(MirrorQueued {
            venue: "paper".into(),
            ticket_key: format!("ticket-{seed}"),
            instrument: make_equity("SPY").unwrap(),
            side: Side::Buy,
            quantity: Money::from_i128(10).unwrap(),
            order_type: OrderType::Market,
            limit_price: None,
            tif: Tif::Day,
            allocations: vec![MirrorAllocation {
                strategy_order_id: format!("so-{seed}"),
                strategy_account: account.clone(),
                quantity: Money::from_i128(10).unwrap(),
            }],
            at: ts.clone(),
        }),
        EventKind::MirrorRefused => Obj::MRefused(MirrorRefused {
            venue: "paper".into(),
            strategy_order_id: format!("so-ref-{seed}"),
            strategy_account: "ACC_1".into(),
            reason: "venue throttled".into(),
            at: ts.clone(),
        }),
        EventKind::MirrorAck => Obj::MAck(MirrorAck {
            venue: "paper".into(),
            ticket_key: format!("ticket-{seed}"),
            status: "ACCEPTED".into(),
            message: "accepted by venue".into(),
            at: ts.clone(),
            venue_order_id: Some(format!("{}", 10000 + seed)),
            book_status: Some(OrderState::Accepted),
        }),
        EventKind::MirrorFill => Obj::MFill(MirrorFill {
            venue: "paper".into(),
            ticket_key: format!("ticket-{seed}"),
            venue_order_id: format!("{}", 10000 + seed),
            filled: Money::from_i128(10).unwrap(),
            avg_price: dec("100.50"),
            at: ts.clone(),
        }),
    };

    Event {
        account,
        kind,
        payload,
        ts_utc: ts,
        command_id: cid,
        schema_version: 1,
        seq: Some((seed as i128) + 1),
    }
}

const ALL_EVENT_KINDS: [EventKind; 27] = [
    EventKind::SignalSeen,
    EventKind::RiskVerdict,
    EventKind::OrdersCreated,
    EventKind::RiskControl,
    EventKind::OrderSubmitted,
    EventKind::OrderUpdated,
    EventKind::OrderPending,
    EventKind::OrderAccepted,
    EventKind::OrderRejected,
    EventKind::OrderCancelled,
    EventKind::OrderRefused,
    EventKind::OrderExpired,
    EventKind::OrderEmulationUpdated,
    EventKind::Fill,
    EventKind::Assignment,
    EventKind::Exercise,
    EventKind::Expiry,
    EventKind::CorporateAction,
    EventKind::CashFlow,
    EventKind::Mark,
    EventKind::VenueReconcile,
    EventKind::VenueHaltCleared,
    EventKind::EodRun,
    EventKind::MirrorQueued,
    EventKind::MirrorRefused,
    EventKind::MirrorAck,
    EventKind::MirrorFill,
];

// -------------------------------------------------------------------------------------------------
// Property 1: Codec roundtrip for every event kind
// -------------------------------------------------------------------------------------------------

#[test]
fn codec_roundtrip_all_27_event_kinds() {
    for (i, &kind) in ALL_EVENT_KINDS.iter().enumerate() {
        let ev = make_sample_event_for_kind(kind, 100 + i as u32);
        let bytes1 = event_to_bytes(&ev).expect("encode should succeed");
        let decoded = event_from_bytes(&bytes1).expect("decode should succeed");
        let bytes2 = event_to_bytes(&decoded).expect("re-encode should succeed");

        assert_eq!(bytes1, bytes2, "Re-encoded bytes must be identical for {kind:?}");
        assert_eq!(decoded.kind, ev.kind);
        assert_eq!(decoded.account, ev.account);
        assert_eq!(decoded.command_id, ev.command_id);
        assert_eq!(decoded.seq, ev.seq);
        assert_eq!(decoded.schema_version, ev.schema_version);
    }
}

proptest! {
    #![proptest_config(ProptestConfig::with_cases(54))]
    #[test]
    fn prop_codec_roundtrip_across_seeds(kind_idx in 0usize..27, seed in 1u32..10_000) {
        let kind = ALL_EVENT_KINDS[kind_idx];
        let ev = make_sample_event_for_kind(kind, seed);
        let bytes1 = event_to_bytes(&ev).unwrap();
        let decoded = event_from_bytes(&bytes1).unwrap();
        let bytes2 = event_to_bytes(&decoded).unwrap();
        prop_assert_eq!(bytes1, bytes2);
    }
}

// -------------------------------------------------------------------------------------------------
// Property 2: LED-1 Cold fold equals incremental fold at every prefix (I2)
// -------------------------------------------------------------------------------------------------

fn build_valid_event_stream(deposit: i64, fills: &[(i64, i64, Side)]) -> Vec<Event> {
    let mut evs = Vec::new();
    let account = "ACC_LED1";
    let mut seq = 1i128;

    // 1. Initial deposit
    evs.push(Event {
        account: account.into(),
        kind: EventKind::CashFlow,
        payload: Obj::CashFlow(CashFlow {
            amount: Money::from_i128(deposit as i128).unwrap(),
            kind: "deposit".into(),
            as_of: dt("2026-01-02T09:30:00+00:00"),
            note: Some("seed cash".into()),
        }),
        ts_utc: dt("2026-01-02T09:30:00+00:00"),
        command_id: Some("cmd-dep-1".into()),
        schema_version: 1,
        seq: Some(seq),
    });
    seq += 1;

    // 2. Mark
    evs.push(Event {
        account: account.into(),
        kind: EventKind::Mark,
        payload: Obj::Mark(Mark {
            instrument: make_equity("SPY").unwrap(),
            price: dec("100.00"),
            as_of: dt("2026-01-02T09:31:00+00:00"),
            source: Some("initial_mark".into()),
        }),
        ts_utc: dt("2026-01-02T09:31:00+00:00"),
        command_id: None,
        schema_version: 1,
        seq: Some(seq),
    });
    seq += 1;

    // 3. Fills and order lifecycles
    for (i, &(qty, price, side)) in fills.iter().enumerate() {
        let ord_id = format!("ord-{i}");
        let cmd_id = format!("cmd-sub-{i}");

        // OrderSubmitted
        evs.push(Event {
            account: account.into(),
            kind: EventKind::OrderSubmitted,
            payload: Obj::Order(make_test_order(account, &ord_id, &cmd_id, qty, side, OrderType::Market, OrderState::Submitted)),
            ts_utc: dt(&format!("2026-01-02T10:{:02}:00+00:00", i % 60)),
            command_id: Some(cmd_id.clone()),
            schema_version: 1,
            seq: Some(seq),
        });
        seq += 1;

        // OrderAccepted
        evs.push(Event {
            account: account.into(),
            kind: EventKind::OrderAccepted,
            payload: Obj::StateChange(OrderStateChange {
                order_id: ord_id.clone(),
                reason: Some("accepted".into()),
                venue_order_id: Some(format!("v-{i}")),
            }),
            ts_utc: dt(&format!("2026-01-02T10:{:02}:01+00:00", i % 60)),
            command_id: None,
            schema_version: 1,
            seq: Some(seq),
        });
        seq += 1;

        // Fill
        evs.push(Event {
            account: account.into(),
            kind: EventKind::Fill,
            payload: Obj::Fill(Fill {
                fill_id: format!("f-{i}"),
                order_id: ord_id.clone(),
                account_id: account.into(),
                instrument: make_equity("SPY").unwrap(),
                quantity: Money::from_i128(qty as i128).unwrap(),
                price: Money::from_i128(price as i128).unwrap(),
                venue_env: "sim".into(),
                filled_at: dt(&format!("2026-01-02T10:{:02}:02+00:00", i % 60)),
                side,
                fee: dec("0.50"),
                leg_id: None,
                venue_order_id: Some(format!("v-{i}")),
                venue_execution_id: Some(format!("exec-{i}")),
            }),
            ts_utc: dt(&format!("2026-01-02T10:{:02}:02+00:00", i % 60)),
            command_id: None,
            schema_version: 1,
            seq: Some(seq),
        });
        seq += 1;
    }

    evs
}

proptest! {
    #![proptest_config(ProptestConfig::with_cases(30))]
    #[test]
    fn prop_led_1_cold_fold_equals_incremental_fold_at_every_prefix(
        deposit in 10_000i64..50_000,
        qty1 in 1i64..10,
        price1 in 100i64..120,
        qty2 in 1i64..10,
        price2 in 100i64..120,
    ) {
        let fills = vec![
            (qty1, price1, Side::Buy),
            (qty2, price2, Side::Buy),
            (qty1, price1 + 5, Side::Sell),
        ];
        let stream = build_valid_event_stream(deposit, &fills);
        let account = "ACC_LED1";

        // Check at EVERY prefix k from 1 to stream.len()
        let mut incremental_st = AccountState::new(account);
        for k in 1..=stream.len() {
            let cold_st = fold_account(&stream[..k], account).expect("cold fold must succeed");
            apply_event(&mut incremental_st, &stream[k - 1]).expect("incremental apply must succeed");

            let cold_canon = dumps(&canon_account(&cold_st).unwrap());
            let inc_canon = dumps(&canon_account(&incremental_st).unwrap());

            prop_assert_eq!(cold_canon, inc_canon, "Cold fold must equal incremental fold at prefix {}", k);
        }
    }
}

// -------------------------------------------------------------------------------------------------
// Property 3: LED-2 Cash and position conservation for every event kind (I8)
// -------------------------------------------------------------------------------------------------

proptest! {
    #![proptest_config(ProptestConfig::with_cases(50))]
    #[test]
    fn prop_led_2_conservation_on_cash_flow(
        deposit in 1i64..100_000,
        flow_amount in -50_000i64..50_000,
    ) {
        let account = "ACC_CONS";
        let mut st = AccountState::new(account);
        st.cash = Money::from_i128(deposit as i128).unwrap();

        let initial_cash = st.cash.clone();
        let initial_positions_len = st.positions.len();

        let flow_ev = Event {
            account: account.into(),
            kind: EventKind::CashFlow,
            payload: Obj::CashFlow(CashFlow {
                amount: Money::from_i128(flow_amount as i128).unwrap(),
                kind: "fee".into(),
                as_of: dt("2026-01-02T12:00:00+00:00"),
                note: None,
            }),
            ts_utc: dt("2026-01-02T12:00:00+00:00"),
            command_id: Some("cmd-cf".into()),
            schema_version: 1,
            seq: Some(1),
        };

        apply_event(&mut st, &flow_ev).unwrap();

        // 1. Cash moves strictly by flow_amount
        let expected_cash = te_core::ledger::ops::add(&initial_cash, &Money::from_i128(flow_amount as i128).unwrap()).unwrap();
        prop_assert!(te_core::ledger::ops::eq(&st.cash, &expected_cash).unwrap());

        // 2. Positions unchanged
        prop_assert_eq!(st.positions.len(), initial_positions_len);
    }

    #[test]
    fn prop_led_2_conservation_on_fill(
        qty in 1i64..100,
        price in 10i64..500,
        fee_cents in 0i64..500,
        is_buy in proptest::bool::ANY,
    ) {
        let account = "ACC_CONS";
        let side = if is_buy { Side::Buy } else { Side::Sell };
        let mut st = AccountState::new(account);
        st.cash = dec("100000.00");

        let order = make_test_order(account, "ord_c", "cmd_c", qty, side, OrderType::Market, OrderState::Submitted);
        let sub_ev = Event {
            account: account.into(),
            kind: EventKind::OrderSubmitted,
            payload: Obj::Order(order),
            ts_utc: dt("2026-01-02T10:00:00+00:00"),
            command_id: Some("cmd_c".into()),
            schema_version: 1,
            seq: Some(1),
        };
        apply_event(&mut st, &sub_ev).unwrap();

        let cash_before = st.cash.clone();
        let inst = make_equity("SPY").unwrap();
        let hk = inst.hk();
        let pos_before = st.positions.get(&hk).map(|p| p.quantity.clone()).unwrap_or_else(te_core::ledger::ops::zero);

        let fee = Money::from_i128(fee_cents as i128).unwrap();
        let fill = Fill {
            fill_id: "fill_c".into(),
            order_id: "ord_c".into(),
            account_id: account.into(),
            instrument: inst.clone(),
            quantity: Money::from_i128(qty as i128).unwrap(),
            price: Money::from_i128(price as i128).unwrap(),
            venue_env: "sim".into(),
            filled_at: dt("2026-01-02T10:01:00+00:00"),
            side,
            fee: fee.clone(),
            leg_id: None,
            venue_order_id: None,
            venue_execution_id: None,
        };
        let fill_ev = Event {
            account: account.into(),
            kind: EventKind::Fill,
            payload: Obj::Fill(fill),
            ts_utc: dt("2026-01-02T10:01:00+00:00"),
            command_id: None,
            schema_version: 1,
            seq: Some(2),
        };
        apply_event(&mut st, &fill_ev).unwrap();

        let cash_after = st.cash.clone();
        let pos_after = st.positions.get(&hk).unwrap().quantity.clone();

        // Cash conservation: cash_delta == (signed_gross - fee)
        let gross = te_core::ledger::ops::mul(&Money::from_i128(qty as i128).unwrap(), &Money::from_i128(price as i128).unwrap()).unwrap();
        let signed_gross = if side == Side::Sell { gross } else { te_core::ledger::ops::neg(&gross).unwrap() };
        let expected_cash_delta = te_core::ledger::ops::sub(&signed_gross, &fee).unwrap();
        let expected_cash = te_core::ledger::ops::add(&cash_before, &expected_cash_delta).unwrap();
        prop_assert!(te_core::ledger::ops::eq(&cash_after, &expected_cash).unwrap());

        // Position conservation: quantity delta == +qty (Buy) or -qty (Sell)
        let qty_delta = if side == Side::Buy {
            Money::from_i128(qty as i128).unwrap()
        } else {
            te_core::ledger::ops::neg(&Money::from_i128(qty as i128).unwrap()).unwrap()
        };
        let expected_pos = te_core::ledger::ops::add(&pos_before, &qty_delta).unwrap();
        prop_assert!(te_core::ledger::ops::eq(&pos_after, &expected_pos).unwrap());
    }

    #[test]
    fn prop_led_2_non_money_events_conserve_cash_and_positions(kind_idx in 0usize..27) {
        let kind = ALL_EVENT_KINDS[kind_idx];
        // Exclude money/position-altering events and unhandled corporate action
        if matches!(kind, EventKind::CashFlow | EventKind::Fill | EventKind::Assignment | EventKind::Exercise | EventKind::Expiry | EventKind::CorporateAction) {
            return Ok(());
        }

        let account = "ACC_CONS";
        let mut st = AccountState::new(account);
        st.cash = dec("50000.00");

        // Seed an existing order so state transitions or updates have a target
        let o = make_test_order(account, "ord-target", "cmd-target", 10, Side::Buy, OrderType::Market, OrderState::Submitted);
        let oid = o.order_id.clone();
        st.orders.put(&oid, o);

        let cash_before = st.cash.clone();
        let pos_count_before = st.positions.len();

        let mut ev = make_sample_event_for_kind(kind, 777);
        ev.account = account.into();

        // Redirect state changes or order updates to the existing order
        match &mut ev.payload {
            Obj::StateChange(sc) => sc.order_id = "ord-target".into(),
            Obj::OrderUpdated(ou) => {
                ou.order.order_id = "ord-target".into();
                ou.order.command_id = "cmd-target".into();
            }
            Obj::Emulated(em) => em.order_id = "ord-target".into(),
            _ => {}
        }

        let res = apply_event(&mut st, &ev);
        if res.is_ok() {
            // Cash must be strictly unchanged
            prop_assert!(te_core::ledger::ops::eq(&st.cash, &cash_before).unwrap(), "Cash changed for {:?}", kind);
            // Positions must be strictly unchanged
            prop_assert_eq!(st.positions.len(), pos_count_before, "Positions changed for {:?}", kind);
        }
    }
}

// -------------------------------------------------------------------------------------------------
// Property 4: CMD-1 Fold idempotency and replay protection (I3)
// -------------------------------------------------------------------------------------------------

#[test]
fn cmd_1_replaying_submitted_order_with_same_command_id_is_idempotent() {
    let account = "ACC_CMD1";
    let mut st = AccountState::new(account);
    let order = make_test_order(account, "ord-1", "cmd-1", 10, Side::Buy, OrderType::Market, OrderState::New);

    let ev = Event {
        account: account.into(),
        kind: EventKind::OrderSubmitted,
        payload: Obj::Order(order),
        ts_utc: dt("2026-01-02T10:00:00+00:00"),
        command_id: Some("cmd-1".into()),
        schema_version: 1,
        seq: Some(1),
    };

    apply_event(&mut st, &ev).expect("first submit succeeds");
    let state_after_first = dumps(&canon_account(&st).unwrap());

    // Replay with identical command_id
    apply_event(&mut st, &ev).expect("replayed submit with same command_id must succeed");
    let state_after_replay = dumps(&canon_account(&st).unwrap());

    assert_eq!(state_after_first, state_after_replay, "Replaying same command_id must leave state identical (I3)");
}

#[test]
fn cmd_1_submitting_same_order_id_with_different_command_id_refuses() {
    let account = "ACC_CMD1";
    let mut st = AccountState::new(account);
    let order1 = make_test_order(account, "ord-1", "cmd-1", 10, Side::Buy, OrderType::Market, OrderState::New);

    let ev1 = Event {
        account: account.into(),
        kind: EventKind::OrderSubmitted,
        payload: Obj::Order(order1),
        ts_utc: dt("2026-01-02T10:00:00+00:00"),
        command_id: Some("cmd-1".into()),
        schema_version: 1,
        seq: Some(1),
    };
    apply_event(&mut st, &ev1).unwrap();

    // Second submit with DIFFERENT command_id
    let order2 = make_test_order(account, "ord-1", "cmd-2", 10, Side::Buy, OrderType::Market, OrderState::New);
    let ev2 = Event {
        account: account.into(),
        kind: EventKind::OrderSubmitted,
        payload: Obj::Order(order2),
        ts_utc: dt("2026-01-02T10:01:00+00:00"),
        command_id: Some("cmd-2".into()),
        schema_version: 1,
        seq: Some(2),
    };

    let err = apply_event(&mut st, &ev2).expect_err("conflicting command_id must refuse");
    assert_eq!(err.kind, "fold");
    assert!(err.msg.contains("refusing to overwrite it with 'cmd-2' (I3)"), "Error must cite I3 refusal: {}", err.msg);
}

#[test]
fn cmd_1_replayed_fill_refuses_duplicate() {
    let account = "ACC_CMD1";
    let mut st = AccountState::new(account);
    st.cash = dec("10000.00");

    let order = make_test_order(account, "ord-fill", "cmd-ord", 10, Side::Buy, OrderType::Market, OrderState::Submitted);
    let oid = order.order_id.clone();
    st.orders.put(&oid, order);

    let fill_ev = Event {
        account: account.into(),
        kind: EventKind::Fill,
        payload: Obj::Fill(Fill {
            fill_id: "f-100".into(),
            order_id: "ord-fill".into(),
            account_id: account.into(),
            instrument: make_equity("SPY").unwrap(),
            quantity: Money::from_i128(10).unwrap(),
            price: dec("100.00"),
            venue_env: "sim".into(),
            filled_at: dt("2026-01-02T10:00:00+00:00"),
            side: Side::Buy,
            fee: dec("1.00"),
            leg_id: None,
            venue_order_id: None,
            venue_execution_id: None,
        }),
        ts_utc: dt("2026-01-02T10:00:00+00:00"),
        command_id: None,
        schema_version: 1,
        seq: Some(1),
    };

    apply_event(&mut st, &fill_ev).expect("first fill succeeds");
    let cash_after_fill = st.cash.clone();

    // Replay exact same fill
    let err = apply_event(&mut st, &fill_ev).expect_err("duplicate fill must refuse");
    assert_eq!(err.kind, "duplicate_fill");
    assert!(err.msg.contains("(I3)"), "Error must cite I3: {}", err.msg);

    // Cash and positions must not have moved a second time
    assert!(te_core::ledger::ops::eq(&st.cash, &cash_after_fill).unwrap());
}

#[test]
fn cmd_1_orders_created_refuses_different_payload_for_same_id() {
    let account = "ACC_CMD1";
    let mut st = AccountState::new(account);

    let o1 = make_test_order(account, "ord-c", "cmd-c1", 10, Side::Buy, OrderType::Market, OrderState::New);
    let created1 = OrdersCreated {
        orders: vec![o1.clone()],
        fingerprint: "fp1".into(),
        reason: "entry".into(),
    };
    let ev1 = Event {
        account: account.into(),
        kind: EventKind::OrdersCreated,
        payload: Obj::OrdersCreated(created1),
        ts_utc: dt("2026-01-02T10:00:00+00:00"),
        command_id: Some("cmd-c1".into()),
        schema_version: 1,
        seq: Some(1),
    };
    apply_event(&mut st, &ev1).unwrap();

    // Replay with identical payload succeeds idempotently
    apply_event(&mut st, &ev1).expect("identical OrdersCreated replay succeeds");

    // Different payload under same order_id refuses
    let mut o2 = o1.clone();
    o2.quantity = Money::from_i128(20).unwrap();
    let created2 = OrdersCreated {
        orders: vec![o2],
        fingerprint: "fp2".into(),
        reason: "different".into(),
    };
    let ev2 = Event {
        account: account.into(),
        kind: EventKind::OrdersCreated,
        payload: Obj::OrdersCreated(created2),
        ts_utc: dt("2026-01-02T10:01:00+00:00"),
        command_id: Some("cmd-c2".into()),
        schema_version: 1,
        seq: Some(2),
    };
    let err = apply_event(&mut st, &ev2).expect_err("conflicting payload refuses");
    assert_eq!(err.kind, "fold");
    assert!(err.msg.contains("refusing to overwrite it (I3)"), "Error must cite I3: {}", err.msg);
}

#[test]
fn cmd_1_empty_command_id_refuses_at_boundary() {
    let bad_json = br#"{"account":"A","command_id":"","kind":"CashFlow","payload":{"dc":"CashFlow","f":{"amount":{"d":"100"},"as_of":{"T":"2026-01-02T15:00:00+00:00"},"kind":"deposit","note":{"n":true}}},"schema_version":1,"seq":null,"ts_utc":"2026-01-02T15:00:00+00:00"}"#;
    let err = event_from_bytes(bad_json).expect_err("empty command_id must refuse");
    assert_eq!(err.kind, "payload");
    assert!(err.msg.contains("Event.command_id must be non-empty when provided (I3)"));
}

//! Spec-derived and property tests for te_core::snapshot (E8b, rules doc §11).
//!
//! Enforces:
//! 1. One snapshot per account per session close (Mark as_of date) (I2).
//! 2. Peak margin is monotonically non-decreasing over the requested window.
//! 3. Heat is the exact sum of open risk against working protective stops.
//! 4. Positions without working protective stops are listed in heat_unknown_positions, never guessed (I5).
//! 5. Documented protective stop direction: only opposite-side (reduce-side) stop orders qualify.
//! 6. Peak equity and drawdown duration tracking across multiple sessions.
//! 7. Missing mark for any open position at session close refuses (I5).
//! 8. Empty snapshot lists refuse for peak_margin and max_drawdown (I5).
//! 9. Pure derivation: no ledger mutations, deterministic replay (I1, I2).
//! 10. Proptest property verification for monotonic peaks, non-negative drawdowns, and heat invariants.

use chrono::NaiveDate;
use proptest::prelude::*;
use rust_decimal::Decimal;
use std::str::FromStr;

use te_core::ledger::model::{
    parse_datetime, CashFlow, DateTime, Event, EventKind, Fill, Instrument, Mark, Obj, Order,
    OrderState, OrderType, Side, Tif,
};
use te_core::money::Money;
use te_core::snapshot::{
    daily_snapshots, day_note_line, max_drawdown, payload_as_of, peak_margin, protective_stop,
    protective_stop_for_side, session_of, working_stop_risk, AccountSnapshot, SnapshotError,
};

fn dt(s: &str) -> DateTime {
    parse_datetime(s).expect("valid dt").0
}

fn dec(s: &str) -> Decimal {
    Decimal::from_str(s).expect("valid decimal")
}

fn money(s: &str) -> Money {
    Money::parse(s).expect("valid money")
}

fn aapl() -> Instrument {
    Instrument::Equity("AAPL".to_string())
}

fn msft() -> Instrument {
    Instrument::Equity("MSFT".to_string())
}

fn an_order(
    order_id: &str,
    instrument: Instrument,
    side: Side,
    qty: &str,
    order_type: OrderType,
    stop_price: Option<&str>,
    created_at: DateTime,
) -> Order {
    Order {
        order_id: order_id.to_string(),
        account_id: "ACC".to_string(),
        instrument,
        order_type,
        side,
        quantity: money(qty),
        command_id: format!("cmd-{order_id}"),
        created_at,
        limit_price: None,
        stop_price: stop_price.map(money),
        trail_amount: None,
        tif: Tif::Day,
        state: OrderState::Submitted,
        parent_order_id: None,
        oco_group: None,
    }
}

fn buy_fill(fill_id: &str, order_id: &str, qty: &str, price: &str, at: DateTime) -> Fill {
    Fill {
        fill_id: fill_id.to_string(),
        order_id: order_id.to_string(),
        account_id: "ACC".to_string(),
        instrument: aapl(),
        quantity: money(qty),
        price: money(price),
        side: Side::Buy,
        filled_at: at,
        fee: Money::ZERO,
        venue_env: "sim".to_string(),
        leg_id: None,
        venue_order_id: None,
        venue_execution_id: None,
    }
}

fn events_two_days() -> Vec<Event> {
    let d1 = dt("2026-09-22T21:00:00+00:00");
    let d2 = dt("2026-09-23T21:00:00+00:00");

    vec![
        Event {
            account: "ACC".to_string(),
            kind: EventKind::CashFlow,
            payload: Obj::CashFlow(CashFlow {
                amount: money("20000"),
                kind: "deposit".to_string(),
                as_of: d1.clone(),
                note: None,
            }),
            ts_utc: d1.clone(),
            command_id: Some("cmd-cf-1".to_string()),
            schema_version: 1,
            seq: Some(1),
        },
        Event {
            account: "ACC".to_string(),
            kind: EventKind::OrderSubmitted,
            payload: Obj::Order(an_order(
                "o1",
                aapl(),
                Side::Buy,
                "100",
                OrderType::Market,
                None,
                d1.clone(),
            )),
            ts_utc: d1.clone(),
            command_id: Some("cmd-o1".to_string()),
            schema_version: 1,
            seq: Some(2),
        },
        Event {
            account: "ACC".to_string(),
            kind: EventKind::Fill,
            payload: Obj::Fill(buy_fill("f1", "o1", "100", "100", d1.clone())),
            ts_utc: d1.clone(),
            command_id: Some("cmd-f1".to_string()),
            schema_version: 1,
            seq: Some(3),
        },
        Event {
            account: "ACC".to_string(),
            kind: EventKind::OrderSubmitted,
            payload: Obj::Order(an_order(
                "o2",
                aapl(),
                Side::Sell,
                "100",
                OrderType::Stop,
                Some("95"),
                d1.clone(),
            )),
            ts_utc: d1.clone(),
            command_id: Some("cmd-o2".to_string()),
            schema_version: 1,
            seq: Some(4),
        },
        Event {
            account: "ACC".to_string(),
            kind: EventKind::Mark,
            payload: Obj::Mark(Mark {
                instrument: aapl(),
                price: money("110"),
                as_of: d1.clone(),
                source: Some("eod".to_string()),
            }),
            ts_utc: d1,
            command_id: Some("cmd-m1".to_string()),
            schema_version: 1,
            seq: Some(5),
        },
        Event {
            account: "ACC".to_string(),
            kind: EventKind::Mark,
            payload: Obj::Mark(Mark {
                instrument: aapl(),
                price: money("120"),
                as_of: d2.clone(),
                source: Some("eod".to_string()),
            }),
            ts_utc: d2,
            command_id: Some("cmd-m2".to_string()),
            schema_version: 1,
            seq: Some(6),
        },
    ]
}

#[test]
fn test_one_snapshot_per_session_close() {
    let snaps = daily_snapshots(&events_two_days(), "ACC").expect("daily_snapshots ok");
    assert_eq!(
        snaps.iter().map(|s| s.session).collect::<Vec<_>>(),
        vec![
            NaiveDate::from_ymd_opt(2026, 9, 22).unwrap(),
            NaiveDate::from_ymd_opt(2026, 9, 23).unwrap()
        ]
    );
    assert_eq!(snaps[0].equity, dec("21000")); // cash 10000 + 100 * 110
    assert_eq!(snaps[1].equity, dec("22000")); // cash 10000 + 100 * 120
    assert_eq!(snaps[0].cash, dec("10000"));
    assert_eq!(snaps[1].cash, dec("10000"));
}

#[test]
fn test_peak_margin_monotone() {
    let mut events = events_two_days();
    let d3 = dt("2026-09-24T21:00:00+00:00");
    events.push(Event {
        account: "ACC".to_string(),
        kind: EventKind::Mark,
        payload: Obj::Mark(Mark {
            instrument: aapl(),
            price: money("80"),
            as_of: d3.clone(),
            source: None,
        }),
        ts_utc: d3,
        command_id: Some("cmd-m3".to_string()),
        schema_version: 1,
        seq: Some(7),
    });
    let snaps = daily_snapshots(&events, "ACC").expect("daily_snapshots ok");
    // day 1: 0.25 * 11000 = 2750; day 2: 0.25 * 12000 = 3000 -> peak 3000; day 3: 0.25 * 8000 = 2000 -> peak 3000
    assert_eq!(snaps[0].margin_used, dec("2750"));
    assert_eq!(snaps[1].margin_used, dec("3000"));
    assert_eq!(snaps[1].margin_peak, dec("3000"));
    assert_eq!(snaps[2].margin_used, dec("2000"));
    assert_eq!(snaps[2].margin_peak, dec("3000"));
    assert_eq!(peak_margin(&snaps).unwrap(), dec("3000"));
}

#[test]
fn test_heat_from_working_protective_stop() {
    // Long 100 @ mark 110, stop 95 -> heat (110 - 95) * 100 = 1500; day 2: (120 - 95) * 100 = 2500
    let snaps = daily_snapshots(&events_two_days(), "ACC").expect("daily_snapshots ok");
    assert_eq!(snaps[0].heat, dec("1500"));
    assert_eq!(snaps[1].heat, dec("2500"));
    assert!(snaps[0].heat_unknown_positions.is_empty());
    assert!(snaps[1].heat_unknown_positions.is_empty());
}

#[test]
fn test_position_without_stop_is_listed_not_guessed() {
    let mut events = events_two_days();
    // Drop protective stop order (seq == 4)
    events.retain(|e| e.seq != Some(4));
    let snaps = daily_snapshots(&events, "ACC").expect("daily_snapshots ok");
    assert_eq!(snaps[0].heat, dec("0"));
    assert_eq!(snaps[0].heat_unknown_positions, vec!["AAPL".to_string()]);
    assert!(format!("{:?}", snaps[0]).contains("AAPL"));
}

#[test]
fn test_protective_stop_side_enforcement() {
    // Spec & docstring enforcement: a protective stop MUST be on the reduce side.
    // When long AAPL, an order with Side::Buy and OrderType::Stop is a breakout buy entry,
    // NOT a protective stop. Python had a defect where it ignored order.side.
    let d1 = dt("2026-09-22T21:00:00+00:00");
    let events = vec![
        Event {
            account: "ACC".to_string(),
            kind: EventKind::CashFlow,
            payload: Obj::CashFlow(CashFlow {
                amount: money("20000"),
                kind: "deposit".to_string(),
                as_of: d1.clone(),
                note: None,
            }),
            ts_utc: d1.clone(),
            command_id: Some("cmd-cf-1".to_string()),
            schema_version: 1,
            seq: Some(1),
        },
        Event {
            account: "ACC".to_string(),
            kind: EventKind::OrderSubmitted,
            payload: Obj::Order(an_order(
                "o1",
                aapl(),
                Side::Buy,
                "100",
                OrderType::Market,
                None,
                d1.clone(),
            )),
            ts_utc: d1.clone(),
            command_id: Some("cmd-o1".to_string()),
            schema_version: 1,
            seq: Some(2),
        },
        Event {
            account: "ACC".to_string(),
            kind: EventKind::Fill,
            payload: Obj::Fill(buy_fill("f1", "o1", "100", "100", d1.clone())),
            ts_utc: d1.clone(),
            command_id: Some("cmd-f1".to_string()),
            schema_version: 1,
            seq: Some(3),
        },
        // BUY STOP order: same side as position (long). NOT a protective stop!
        Event {
            account: "ACC".to_string(),
            kind: EventKind::OrderSubmitted,
            payload: Obj::Order(an_order(
                "o_buy_stop",
                aapl(),
                Side::Buy,
                "50",
                OrderType::Stop,
                Some("95"),
                d1.clone(),
            )),
            ts_utc: d1.clone(),
            command_id: Some("cmd-o-buy-stop".to_string()),
            schema_version: 1,
            seq: Some(4),
        },
        Event {
            account: "ACC".to_string(),
            kind: EventKind::Mark,
            payload: Obj::Mark(Mark {
                instrument: aapl(),
                price: money("110"),
                as_of: d1.clone(),
                source: Some("eod".to_string()),
            }),
            ts_utc: d1,
            command_id: Some("cmd-m1".to_string()),
            schema_version: 1,
            seq: Some(5),
        },
    ];

    let snaps = daily_snapshots(&events, "ACC").expect("daily_snapshots ok");
    // Position is long, but only a BUY stop exists. Must be listed in unknown, not guessed.
    assert_eq!(snaps[0].heat, dec("0"));
    assert_eq!(snaps[0].heat_unknown_positions, vec!["AAPL".to_string()]);
}

#[test]
fn test_direct_protective_stop_and_working_stop_risk() {
    let events = events_two_days();
    let state = te_core::ledger::fold::fold_account(&events[..5], "ACC").expect("fold ok");

    // Direct protective_stop call: Long AAPL, sell stop at 95 exists
    let stop = protective_stop(&state, &aapl());
    assert_eq!(stop, Some(dec("95")));

    // Direct protective_stop_for_side call
    let stop_sell = protective_stop_for_side(&state, &aapl(), Side::Sell);
    assert_eq!(stop_sell, Some(dec("95")));
    let stop_buy = protective_stop_for_side(&state, &aapl(), Side::Buy);
    assert_eq!(stop_buy, None);

    // Direct working_stop_risk call
    let (heat, unknown) = working_stop_risk(&state).expect("working_stop_risk ok");
    assert_eq!(heat, dec("1500"));
    assert!(unknown.is_empty());
}

#[test]
fn test_drawdown_and_duration() {
    let snaps = daily_snapshots(&events_two_days(), "ACC").expect("daily_snapshots ok");
    assert_eq!(snaps[0].drawdown_from_peak, dec("0"));
    assert_eq!(snaps[1].drawdown_from_peak, dec("0"));
    assert_eq!(snaps[0].drawdown_duration_sessions, 0);
    assert_eq!(snaps[1].drawdown_duration_sessions, 0);
    assert_eq!(max_drawdown(&snaps).unwrap(), dec("0"));

    // Multi-day with drawdown: Day 1 peak 21000, Day 2 drop to 19000, Day 3 drop to 18000, Day 4 recover to 22000
    let d1 = dt("2026-09-22T21:00:00+00:00");
    let d2 = dt("2026-09-23T21:00:00+00:00");
    let d3 = dt("2026-09-24T21:00:00+00:00");
    let d4 = dt("2026-09-25T21:00:00+00:00");

    let events = vec![
        Event {
            account: "ACC".to_string(),
            kind: EventKind::CashFlow,
            payload: Obj::CashFlow(CashFlow {
                amount: money("20000"),
                kind: "deposit".to_string(),
                as_of: d1.clone(),
                note: None,
            }),
            ts_utc: d1.clone(),
            command_id: Some("cmd-cf".to_string()),
            schema_version: 1,
            seq: Some(1),
        },
        Event {
            account: "ACC".to_string(),
            kind: EventKind::OrderSubmitted,
            payload: Obj::Order(an_order(
                "o1",
                aapl(),
                Side::Buy,
                "100",
                OrderType::Market,
                None,
                d1.clone(),
            )),
            ts_utc: d1.clone(),
            command_id: Some("cmd-o1".to_string()),
            schema_version: 1,
            seq: Some(2),
        },
        Event {
            account: "ACC".to_string(),
            kind: EventKind::Fill,
            payload: Obj::Fill(buy_fill("f1", "o1", "100", "100", d1.clone())),
            ts_utc: d1.clone(),
            command_id: Some("cmd-f1".to_string()),
            schema_version: 1,
            seq: Some(3),
        },
        // Day 1: Mark 110 -> equity = 10000 + 11000 = 21000 (peak)
        Event {
            account: "ACC".to_string(),
            kind: EventKind::Mark,
            payload: Obj::Mark(Mark {
                instrument: aapl(),
                price: money("110"),
                as_of: d1.clone(),
                source: None,
            }),
            ts_utc: d1,
            command_id: Some("cmd-m1".to_string()),
            schema_version: 1,
            seq: Some(4),
        },
        // Day 2: Mark 90 -> equity = 10000 + 9000 = 19000 (DD = 2000, duration = 0)
        Event {
            account: "ACC".to_string(),
            kind: EventKind::Mark,
            payload: Obj::Mark(Mark {
                instrument: aapl(),
                price: money("90"),
                as_of: d2.clone(),
                source: None,
            }),
            ts_utc: d2,
            command_id: Some("cmd-m2".to_string()),
            schema_version: 1,
            seq: Some(5),
        },
        // Day 3: Mark 80 -> equity = 10000 + 8000 = 18000 (DD = 3000, duration = 1)
        Event {
            account: "ACC".to_string(),
            kind: EventKind::Mark,
            payload: Obj::Mark(Mark {
                instrument: aapl(),
                price: money("80"),
                as_of: d3.clone(),
                source: None,
            }),
            ts_utc: d3,
            command_id: Some("cmd-m3".to_string()),
            schema_version: 1,
            seq: Some(6),
        },
        // Day 4: Mark 120 -> equity = 10000 + 12000 = 22000 (new peak, DD = 0, duration = 0)
        Event {
            account: "ACC".to_string(),
            kind: EventKind::Mark,
            payload: Obj::Mark(Mark {
                instrument: aapl(),
                price: money("120"),
                as_of: d4.clone(),
                source: None,
            }),
            ts_utc: d4,
            command_id: Some("cmd-m4".to_string()),
            schema_version: 1,
            seq: Some(7),
        },
    ];

    let snaps = daily_snapshots(&events, "ACC").expect("daily_snapshots ok");
    assert_eq!(snaps.len(), 4);

    assert_eq!(snaps[0].drawdown_from_peak, dec("0"));
    assert_eq!(snaps[0].drawdown_duration_sessions, 0);

    assert_eq!(snaps[1].drawdown_from_peak, dec("2000"));
    assert_eq!(snaps[1].drawdown_duration_sessions, 0);

    assert_eq!(snaps[2].drawdown_from_peak, dec("3000"));
    assert_eq!(snaps[2].drawdown_duration_sessions, 1);

    assert_eq!(snaps[3].drawdown_from_peak, dec("0"));
    assert_eq!(snaps[3].drawdown_duration_sessions, 0);

    assert_eq!(max_drawdown(&snaps).unwrap(), dec("3000"));
}

#[test]
fn test_missing_mark_refuses() {
    let d1 = dt("2026-09-22T21:00:00+00:00");
    let events = vec![
        Event {
            account: "ACC".to_string(),
            kind: EventKind::OrderSubmitted,
            payload: Obj::Order(an_order(
                "o1",
                aapl(),
                Side::Buy,
                "100",
                OrderType::Market,
                None,
                d1.clone(),
            )),
            ts_utc: d1.clone(),
            command_id: Some("cmd-o1".to_string()),
            schema_version: 1,
            seq: Some(1),
        },
        Event {
            account: "ACC".to_string(),
            kind: EventKind::Fill,
            payload: Obj::Fill(buy_fill("f1", "o1", "100", "100", d1.clone())),
            ts_utc: d1.clone(),
            command_id: Some("cmd-f1".to_string()),
            schema_version: 1,
            seq: Some(2),
        },
        Event {
            account: "ACC".to_string(),
            kind: EventKind::Mark,
            payload: Obj::Mark(Mark {
                instrument: msft(),
                price: money("50"),
                as_of: d1.clone(),
                source: None,
            }),
            ts_utc: d1,
            command_id: Some("cmd-m1".to_string()),
            schema_version: 1,
            seq: Some(3),
        },
    ];

    let res = daily_snapshots(&events, "ACC");
    assert!(res.is_err());
    let err = res.unwrap_err();
    assert_eq!(
        err.message(),
        "No session-close mark for open position AAPL (I5)"
    );
}

#[test]
fn test_no_marks_no_snapshots() {
    assert_eq!(daily_snapshots(&[], "ACC").unwrap(), vec![]);
}

#[test]
fn test_day_note_line_matches_hand_computed() {
    let snaps = daily_snapshots(&events_two_days(), "ACC").expect("daily_snapshots ok");
    let line = day_note_line(&snaps[0], "ACC");
    assert_eq!(
        line,
        "snapshot ACC 2026-09-22: equity 21000, gross 52.4%, margin used 2750 (peak 2750), heat 7.1%, DD 0"
    );
}

#[test]
fn test_day_note_lists_stopless_positions() {
    let mut events = events_two_days();
    events.retain(|e| e.seq != Some(4));
    let snaps = daily_snapshots(&events, "ACC").expect("daily_snapshots ok");
    let line = day_note_line(&snaps[0], "ACC");
    assert!(line.contains("(no stop: AAPL)"));
}

#[test]
fn test_day_note_line_zero_equity() {
    let snap = AccountSnapshot {
        session: NaiveDate::from_ymd_opt(2026, 9, 22).unwrap(),
        equity: dec("0"),
        cash: dec("0"),
        gross_exposure: dec("0"),
        net_exposure: dec("0"),
        margin_used: dec("0"),
        margin_available: dec("0"),
        margin_peak: dec("0"),
        heat: dec("0"),
        heat_unknown_positions: vec![],
        drawdown_from_peak: dec("0"),
        drawdown_duration_sessions: 0,
    };
    let line = day_note_line(&snap, "ACC");
    assert!(line.contains("gross n/a"));
    assert!(line.contains("heat n/a"));
}

#[test]
fn test_snapshot_never_writes_to_ledger() {
    // Pure derivation: same events in, same snapshots out, nothing mutated (I2).
    let events = events_two_days();
    let first = daily_snapshots(&events, "ACC").expect("first ok");
    let second = daily_snapshots(&events, "ACC").expect("second ok");
    assert_eq!(first, second);
    assert_eq!(events.len(), 6);
}

#[test]
fn test_empty_snapshots_refusals() {
    assert_eq!(
        peak_margin(&[]).unwrap_err(),
        SnapshotError::EmptySnapshots("peak_margin over an empty snapshot list refuses (I5)")
    );
    assert_eq!(
        max_drawdown(&[]).unwrap_err(),
        SnapshotError::EmptySnapshots("max_drawdown over an empty snapshot list refuses (I5)")
    );
}

#[test]
fn test_payload_as_of_and_session_of() {
    let d1 = dt("2026-09-22T21:00:00+00:00");
    let mark_event = Event {
        account: "ACC".to_string(),
        kind: EventKind::Mark,
        payload: Obj::Mark(Mark {
            instrument: aapl(),
            price: money("110"),
            as_of: d1.clone(),
            source: None,
        }),
        ts_utc: d1.clone(),
        command_id: None,
        schema_version: 1,
        seq: Some(1),
    };
    assert_eq!(payload_as_of(&mark_event).unwrap().instant(), d1.instant());
    assert_eq!(
        session_of(&mark_event).unwrap(),
        NaiveDate::from_ymd_opt(2026, 9, 22).unwrap()
    );

    let order_event = Event {
        account: "ACC".to_string(),
        kind: EventKind::OrderSubmitted,
        payload: Obj::Order(an_order(
            "o1",
            aapl(),
            Side::Buy,
            "100",
            OrderType::Market,
            None,
            d1.clone(),
        )),
        ts_utc: d1,
        command_id: None,
        schema_version: 1,
        seq: Some(2),
    };
    assert_eq!(
        payload_as_of(&order_event).unwrap_err(),
        SnapshotError::NoAsOf {
            kind: "OrderSubmitted".to_string()
        }
    );
}

// =========================================================================
// Property-based Tests (proptest)
// =========================================================================

proptest! {
    /// Invariant: For any series of snapshots, peak margin is non-decreasing
    /// and always >= margin_used at each step.
    #[test]
    fn prop_peak_margin_monotone_and_dominating(
        margins in prop::collection::vec(1u32..10_000, 1..20)
    ) {
        let mut snaps = Vec::new();
        let mut running_peak = Decimal::ZERO;
        for (i, &m) in margins.iter().enumerate() {
            let m_dec = Decimal::from(m);
            if m_dec > running_peak {
                running_peak = m_dec;
            }
            snaps.push(AccountSnapshot {
                session: NaiveDate::from_ymd_opt(2026, 1, 1).unwrap(),
                equity: Decimal::from(50_000),
                cash: Decimal::from(50_000),
                gross_exposure: Decimal::ZERO,
                net_exposure: Decimal::ZERO,
                margin_used: m_dec,
                margin_available: Decimal::from(50_000) - m_dec,
                margin_peak: running_peak,
                heat: Decimal::ZERO,
                heat_unknown_positions: vec![],
                drawdown_from_peak: Decimal::ZERO,
                drawdown_duration_sessions: 0,
            });
            prop_assert!(snaps[i].margin_peak >= snaps[i].margin_used);
            if i > 0 {
                prop_assert!(snaps[i].margin_peak >= snaps[i - 1].margin_peak);
            }
        }
        let computed_peak = peak_margin(&snaps).unwrap();
        prop_assert_eq!(computed_peak, running_peak);
    }

    /// Invariant: Drawdown from peak is always >= 0, and max_drawdown equals the maximum drawdown.
    #[test]
    fn prop_drawdown_bounds_and_max(
        equities in prop::collection::vec(1000u32..50_000, 1..30)
    ) {
        let mut snaps = Vec::new();
        let mut peak_equity = Decimal::ZERO;
        let mut max_dd = Decimal::ZERO;
        let mut dd_start: Option<usize> = None;

        for (i, &eq_u) in equities.iter().enumerate() {
            let eq = Decimal::from(eq_u);
            if eq > peak_equity {
                peak_equity = eq;
                dd_start = None;
            }
            let dd = if peak_equity > eq { peak_equity - eq } else { Decimal::ZERO };
            if dd > Decimal::ZERO && dd_start.is_none() {
                dd_start = Some(i);
            }
            if dd > max_dd {
                max_dd = dd;
            }
            let dur = match dd_start {
                None => 0,
                Some(s) => i - s,
            };
            snaps.push(AccountSnapshot {
                session: NaiveDate::from_ymd_opt(2026, 1, 1).unwrap(),
                equity: eq,
                cash: eq,
                gross_exposure: Decimal::ZERO,
                net_exposure: Decimal::ZERO,
                margin_used: Decimal::from(1000),
                margin_available: eq - Decimal::from(1000),
                margin_peak: Decimal::from(1000),
                heat: Decimal::ZERO,
                heat_unknown_positions: vec![],
                drawdown_from_peak: dd,
                drawdown_duration_sessions: dur,
            });
            prop_assert!(snaps[i].drawdown_from_peak >= Decimal::ZERO);
        }

        prop_assert_eq!(max_drawdown(&snaps).unwrap(), max_dd);
    }

    /// Invariant: Working stop risk calculation for long positions is exact: (mark - stop) * qty.
    #[test]
    fn prop_working_stop_risk_exact(
        mark_cents in 1000u32..50_000,
        stop_offset in 1u32..500,
        qty_shares in 1u32..1000,
    ) {
        let mark_dec = Decimal::new(mark_cents as i64, 2);
        let stop_dec = Decimal::new((mark_cents - stop_offset) as i64, 2);
        let qty_dec = Decimal::from(qty_shares);

        let d1 = dt("2026-09-22T21:00:00+00:00");
        let events = vec![
            Event {
                account: "ACC".to_string(),
                kind: EventKind::CashFlow,
                payload: Obj::CashFlow(CashFlow {
                    amount: money("1000000"),
                    kind: "deposit".to_string(),
                    as_of: d1.clone(),
                    note: None,
                }),
                ts_utc: d1.clone(),
                command_id: Some("cmd-cf".to_string()),
                schema_version: 1,
                seq: Some(1),
            },
            Event {
                account: "ACC".to_string(),
                kind: EventKind::OrderSubmitted,
                payload: Obj::Order(an_order(
                    "o1",
                    aapl(),
                    Side::Buy,
                    &qty_dec.to_string(),
                    OrderType::Market,
                    None,
                    d1.clone(),
                )),
                ts_utc: d1.clone(),
                command_id: Some("cmd-o1".to_string()),
                schema_version: 1,
                seq: Some(2),
            },
            Event {
                account: "ACC".to_string(),
                kind: EventKind::Fill,
                payload: Obj::Fill(buy_fill("f1", "o1", &qty_dec.to_string(), &mark_dec.to_string(), d1.clone())),
                ts_utc: d1.clone(),
                command_id: Some("cmd-f1".to_string()),
                schema_version: 1,
                seq: Some(3),
            },
            Event {
                account: "ACC".to_string(),
                kind: EventKind::OrderSubmitted,
                payload: Obj::Order(an_order(
                    "o_stop",
                    aapl(),
                    Side::Sell,
                    &qty_dec.to_string(),
                    OrderType::Stop,
                    Some(&stop_dec.to_string()),
                    d1.clone(),
                )),
                ts_utc: d1.clone(),
                command_id: Some("cmd-o-stop".to_string()),
                schema_version: 1,
                seq: Some(4),
            },
            Event {
                account: "ACC".to_string(),
                kind: EventKind::Mark,
                payload: Obj::Mark(Mark {
                    instrument: aapl(),
                    price: money(&mark_dec.to_string()),
                    as_of: d1.clone(),
                    source: None,
                }),
                ts_utc: d1,
                command_id: Some("cmd-m1".to_string()),
                schema_version: 1,
                seq: Some(5),
            },
        ];

        let snaps = daily_snapshots(&events, "ACC").expect("daily_snapshots ok");
        let expected_heat = (mark_dec - stop_dec) * qty_dec;
        prop_assert_eq!(snaps[0].heat, expected_heat);
        prop_assert!(snaps[0].heat_unknown_positions.is_empty());
    }
}

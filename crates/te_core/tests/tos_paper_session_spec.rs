//! Spec-derived and property tests for tos_paper mirror session logic.
//!
//! Enforces:
//! 1. Batch Predicates & Windows:
//!    - `is_working` accepts exactly SUBMITTED, ACCEPTED, PARTIALLY_FILLED.
//!    - `is_ended_unfilled` accepts exactly CANCELLED, REJECTED, EXPIRED.
//!    - `pending_wanted` requires created_at >= close AND working state.
//!    - `morning_wanted` requires created_at >= session_open AND not ended unfilled (fills kept).
//! 2. Deterministic Working Orders Selection:
//!    - Entries only (parent_order_id must be None; exits reach venue through tos_paper::exits).
//!    - Excludes already handled orders (queued or refused in MirrorState).
//!    - Account order then order_id order: deterministic first-in rule.
//! 3. Batch Markers & I5 Refuse Incomplete Batches:
//!    - Missing marker for an account refuses (I5: incomplete batch).
//!    - Multiple marker jobs without an explicit job filter refuses (I5: name the job).
//!    - Events created after marker (seq > marker.seq) are skipped.
//! 4. Unproven Open Tickets & Venue Halting (I5):
//!    - An open ticket without a proven venue Order ID halts the venue with VenueReconcile.
//!    - Drift lists the sorted symbols of all unproven contracts.
//!    - Derived command ID is deterministic with SHA-256 digest.
//! 5. Cover Rule & Candidate Filtering (I3, I11):
//!    - Deduplicates candidate orders preserving first seen.
//!    - Orders failing cover rule are refused (may_wait = false) or deferred to waiting (may_wait = true).
//! 6. Write-Ahead & Drain Acks Invariants (I2, I3):
//!    - Reused ticket keys never re-sent: allocations refused with reason (I3).
//!    - Drain acks map proven Order IDs and book statuses into MirrorAck.
//! 7. Lifting Operator Halts (§4.5, I11):
//!    - Empty reason refuses (I11).
//!    - Unhalted venue refuses.
//!    - Drifting newest reconcile refuses.
//!    - Clean newest reconcile clears each holding account with derived command ID.

use std::collections::HashMap;

use chrono::NaiveDate;
use proptest::prelude::*;
use te_core::ledger::json::{parse, Json};
use te_core::ledger::mirror::MirrorState;
use te_core::ledger::model::*;
use te_core::money::Money;
use te_core::tos_paper::decide;
use te_core::tos_paper::netting::Ticket;
use te_core::tos_paper::session::*;
use te_core::tos_paper::SESSION;

fn dec(s: &str) -> Money {
    Money::parse(s).expect("valid decimal")
}

fn dt(s: &str) -> DateTime {
    parse_datetime(s).expect("valid dt").0
}

fn nd(s: &str) -> NaiveDate {
    NaiveDate::parse_from_str(s, "%Y-%m-%d").expect("valid date")
}

fn make_order(
    account: &str,
    order_id: &str,
    state: OrderState,
    created_at: DateTime,
    parent: Option<&str>,
) -> Order {
    Order {
        order_id: order_id.to_string(),
        account_id: account.to_string(),
        instrument: make_equity("AAPL").unwrap(),
        order_type: OrderType::Limit,
        side: Side::Buy,
        quantity: dec("10"),
        command_id: format!("cmd-{order_id}"),
        created_at,
        limit_price: Some(dec("150.00")),
        stop_price: None,
        trail_amount: None,
        tif: Tif::Day,
        state,
        parent_order_id: parent.map(|s| s.to_string()),
        oco_group: None,
    }
}

// -------------------------------------------------------------------------------------------------
// Group 1: Batch Predicates & Windows
// -------------------------------------------------------------------------------------------------

#[test]
fn test_is_working_matches_spec() {
    let working = [
        OrderState::Submitted,
        OrderState::Accepted,
        OrderState::PartiallyFilled,
    ];
    let non_working = [
        OrderState::New,
        OrderState::Filled,
        OrderState::Cancelled,
        OrderState::Rejected,
        OrderState::Expired,
        OrderState::PendingUnknown,
    ];

    for s in working {
        assert!(is_working(s), "State {s:?} must be working");
    }
    for s in non_working {
        assert!(!is_working(s), "State {s:?} must NOT be working");
    }
}

#[test]
fn test_is_ended_unfilled_matches_spec() {
    let ended_unfilled = [
        OrderState::Cancelled,
        OrderState::Rejected,
        OrderState::Expired,
    ];
    let other = [
        OrderState::New,
        OrderState::Submitted,
        OrderState::Accepted,
        OrderState::PartiallyFilled,
        OrderState::Filled,
        OrderState::PendingUnknown,
    ];

    for s in ended_unfilled {
        assert!(is_ended_unfilled(s), "State {s:?} must be ended unfilled");
        assert!(!is_working(s), "Ended unfilled state {s:?} must not be working");
    }
    for s in other {
        assert!(!is_ended_unfilled(s), "State {s:?} must NOT be ended unfilled");
    }
}

#[test]
fn test_pass_of_recognizes_passes() {
    assert_eq!(pass_of("eod-morning"), Some("morning"));
    assert_eq!(pass_of("strategy1-morning"), Some("morning"));
    assert_eq!(pass_of("eod-midday"), Some("midday"));
    assert_eq!(pass_of("eod-late"), Some("late"));
    assert_eq!(pass_of("eod"), None);
    assert_eq!(pass_of("morning"), None);
    assert_eq!(pass_of(""), None);
}

#[test]
fn test_pending_wanted_window_boundary() {
    let close = dt("2026-01-02T21:00:00+00:00");
    let before = dt("2026-01-02T20:59:59+00:00");
    let at_close = dt("2026-01-02T21:00:00+00:00");
    let after = dt("2026-01-02T21:00:01+00:00");

    let order_at = make_order("A", "o1", OrderState::Submitted, at_close, None);
    let order_after = make_order("A", "o2", OrderState::Accepted, after.clone(), None);
    let order_before = make_order("A", "o3", OrderState::Submitted, before, None);
    let order_filled = make_order("A", "o4", OrderState::Filled, after, None);

    assert!(pending_wanted(&order_at, &order_at, &close));
    assert!(pending_wanted(&order_after, &order_after, &close));
    assert!(!pending_wanted(&order_before, &order_before, &close), "Order created before close must be rejected");
    assert!(!pending_wanted(&order_filled, &order_filled, &close), "Filled order must be rejected from pending batch");
}

#[test]
fn test_morning_wanted_window_boundary() {
    let session_open = dt("2026-01-02T14:30:00+00:00");
    let before = dt("2026-01-02T14:29:59+00:00");
    let at_open = dt("2026-01-02T14:30:00+00:00");
    let after = dt("2026-01-02T14:35:00+00:00");

    let order_at = make_order("A", "o1", OrderState::Submitted, at_open, None);
    let order_after = make_order("A", "o2", OrderState::Filled, after.clone(), None); // Fills kept for morning pass!
    let order_before = make_order("A", "o3", OrderState::Submitted, before, None);
    let order_cancelled = make_order("A", "o4", OrderState::Cancelled, after, None);

    assert!(morning_wanted(&order_at, &order_at, &session_open));
    assert!(morning_wanted(&order_after, &order_after, &session_open), "Filled morning entries are kept");
    assert!(!morning_wanted(&order_before, &order_before, &session_open), "Order created before open must be rejected");
    assert!(!morning_wanted(&order_cancelled, &order_cancelled, &session_open), "Cancelled order must be excluded");
}

proptest! {
    #![proptest_config(ProptestConfig::with_cases(100))]
    #[test]
    fn prop_batch_window_predicates(
        offset_secs in -3600i32..3600i32,
        state_idx in 0usize..10,
    ) {
        let all_states = [
            OrderState::New,
            OrderState::Submitted,
            OrderState::Accepted,
            OrderState::PartiallyFilled,
            OrderState::Filled,
            OrderState::Cancelled,
            OrderState::Rejected,
            OrderState::Expired,
            OrderState::PendingUnknown,
        ];
        let state = all_states[state_idx % all_states.len()];
        let base = dt("2026-01-02T16:00:00+00:00");
        let (secs, us) = base.instant();
        let target_secs = secs + offset_secs as i64;
        let days = target_secs.div_euclid(86400);
        let sod = target_secs.rem_euclid(86400) as u32;
        let date = NaiveDate::from_num_days_from_ce_opt(days as i32).unwrap();
        let created_at = DateTime { date, sod, us, off: 0 };

        let order = make_order("A", "prop_ord", state, created_at, None);

        // pending_wanted: created_at >= base && is_working(state)
        let pw = pending_wanted(&order, &order, &base);
        let expected_pw = offset_secs >= 0 && is_working(state);
        prop_assert_eq!(pw, expected_pw);

        // morning_wanted: created_at >= base && !is_ended_unfilled(state)
        let mw = morning_wanted(&order, &order, &base);
        let expected_mw = offset_secs >= 0 && !is_ended_unfilled(state);
        prop_assert_eq!(mw, expected_mw);
    }
}

// -------------------------------------------------------------------------------------------------
// Group 2: Working Orders Selection
// -------------------------------------------------------------------------------------------------

#[test]
fn test_working_orders_entries_only_and_order_sorting() {
    let mirror = MirrorState::default();
    let accounts = vec!["ACC_B".to_string(), "ACC_A".to_string()];

    let mut orders_by_account = HashMap::new();
    // For ACC_B: order-id unsorted, includes exit (parent order) and filled order
    let b_orders = vec![
        make_order("ACC_B", "ord-b-2", OrderState::Submitted, dt("2026-01-02T10:00:00+00:00"), None),
        make_order("ACC_B", "ord-b-1", OrderState::Accepted, dt("2026-01-02T10:00:00+00:00"), None),
        make_order("ACC_B", "ord-b-exit", OrderState::Submitted, dt("2026-01-02T10:00:00+00:00"), Some("parent-1")), // exit: skip
        make_order("ACC_B", "ord-b-filled", OrderState::Filled, dt("2026-01-02T10:00:00+00:00"), None), // filled: skip
    ];
    // For ACC_A:
    let a_orders = vec![
        make_order("ACC_A", "ord-a-1", OrderState::PartiallyFilled, dt("2026-01-02T10:00:00+00:00"), None),
    ];

    orders_by_account.insert("ACC_B".to_string(), b_orders);
    orders_by_account.insert("ACC_A".to_string(), a_orders);

    let result = working_orders(&accounts, &orders_by_account, &mirror);

    // Assert account order: ACC_B first, then ACC_A
    assert_eq!(result.len(), 3);
    assert_eq!(result[0].order_id, "ord-b-1");
    assert_eq!(result[1].order_id, "ord-b-2");
    assert_eq!(result[2].order_id, "ord-a-1");
}

#[test]
fn test_working_orders_filters_handled() {
    let mut mirror = MirrorState::default();
    mirror.queued_orders.insert("ord-queued".to_string(), "t-1".to_string(), "v-1".to_string());
    mirror.refused_orders.insert("ord-refused".to_string(), "t-2".to_string(), "why".to_string());

    let accounts = vec!["ACC_1".to_string()];
    let mut orders = HashMap::new();
    orders.insert(
        "ACC_1".to_string(),
        vec![
            make_order("ACC_1", "ord-queued", OrderState::Submitted, dt("2026-01-02T10:00:00+00:00"), None),
            make_order("ACC_1", "ord-refused", OrderState::Submitted, dt("2026-01-02T10:00:00+00:00"), None),
            make_order("ACC_1", "ord-fresh", OrderState::Submitted, dt("2026-01-02T10:00:00+00:00"), None),
        ],
    );

    let res = working_orders(&accounts, &orders, &mirror);
    assert_eq!(res.len(), 1);
    assert_eq!(res[0].order_id, "ord-fresh");
}

proptest! {
    #![proptest_config(ProptestConfig::with_cases(50))]
    #[test]
    fn prop_working_orders_deterministic_sorting(
        mut ids in proptest::collection::vec("[a-z]{3}-[0-9]{3}", 2..20)
    ) {
        let mirror = MirrorState::default();
        let accounts = vec!["ACC".to_string()];

        let orders: Vec<Order> = ids.iter().map(|id| {
            make_order("ACC", id, OrderState::Submitted, dt("2026-01-02T10:00:00+00:00"), None)
        }).collect();

        let mut map = HashMap::new();
        map.insert("ACC".to_string(), orders);

        let res = working_orders(&accounts, &map, &mirror);

        ids.sort();
        let res_ids: Vec<String> = res.iter().map(|o| o.order_id.clone()).collect();
        prop_assert_eq!(res_ids, ids);
    }
}

// -------------------------------------------------------------------------------------------------
// Group 3: Batch Markers & I5 Incomplete Batch Refusals
// -------------------------------------------------------------------------------------------------

#[test]
fn test_batch_refuses_when_account_has_no_marker() {
    let mirror = MirrorState::default();
    let accounts = vec!["ACC_MISSING".to_string()];
    let session = nd("2026-01-02");
    let close = dt("2026-01-02T21:00:00+00:00");

    let mut data_map = HashMap::new();
    data_map.insert("ACC_MISSING".to_string(), AccountBatchData::default());

    let err = pending_orders(&accounts, &data_map, session, &close, None, &mirror).unwrap_err();
    assert_eq!(err.kind, SESSION);
    assert!(
        err.msg.contains("'ACC_MISSING' has no EOD run for 2026-01-02; refusing to mirror a batch that may be incomplete (I5)")
    );
}

#[test]
fn test_batch_refuses_when_multiple_jobs_and_no_job_filter() {
    let mirror = MirrorState::default();
    let accounts = vec!["ACC_MULTI".to_string()];
    let session = nd("2026-01-02");
    let close = dt("2026-01-02T21:00:00+00:00");

    let markers = vec![
        MarkerEvent { seq: 10, session, job: "jobA".to_string() },
        MarkerEvent { seq: 20, session, job: "jobB".to_string() },
    ];

    let mut data_map = HashMap::new();
    data_map.insert(
        "ACC_MULTI".to_string(),
        AccountBatchData {
            markers,
            created_events: vec![],
            current_orders: HashMap::new(),
        },
    );

    let err = pending_orders(&accounts, &data_map, session, &close, None, &mirror).unwrap_err();
    assert_eq!(err.kind, SESSION);
    assert!(err.msg.contains("has EOD runs of [\"jobA\", \"jobB\"] for 2026-01-02; name the job (I5)"));
}

#[test]
fn test_batch_skips_events_after_marker_seq() {
    let mirror = MirrorState::default();
    let accounts = vec!["ACC_1".to_string()];
    let session = nd("2026-01-02");
    let close = dt("2026-01-02T21:00:00+00:00");
    let after_close = dt("2026-01-02T21:05:00+00:00");

    let markers = vec![MarkerEvent { seq: 50, session, job: "eod".to_string() }];

    let ord_in = make_order("ACC_1", "ord-in", OrderState::Submitted, after_close.clone(), None);
    let ord_late = make_order("ACC_1", "ord-late", OrderState::Submitted, after_close, None);

    let created_events = vec![
        CreatedBatch { seq: 40, orders: vec![ord_in.clone()] },   // seq 40 <= marker 50: kept
        CreatedBatch { seq: 60, orders: vec![ord_late.clone()] }, // seq 60 > marker 50: skipped!
    ];

    let mut current_orders = HashMap::new();
    current_orders.insert("ord-in".to_string(), ord_in);
    current_orders.insert("ord-late".to_string(), ord_late);

    let mut data_map = HashMap::new();
    data_map.insert(
        "ACC_1".to_string(),
        AccountBatchData {
            markers,
            created_events,
            current_orders,
        },
    );

    let res = pending_orders(&accounts, &data_map, session, &close, Some("eod"), &mirror).unwrap();
    assert_eq!(res.len(), 1);
    assert_eq!(res[0].order_id, "ord-in");
}

// -------------------------------------------------------------------------------------------------
// Group 4: Unproven Open Tickets & Venue Halting
// -------------------------------------------------------------------------------------------------

#[test]
fn test_halt_unproven_halts_venue_when_ticket_lacks_order_id() {
    let mut mirror = MirrorState::default();
    let q = MirrorQueued {
        venue: "paper-venue".to_string(),
        ticket_key: "t-unproven-1".to_string(),
        instrument: make_equity("AAPL").unwrap(),
        side: Side::Buy,
        quantity: dec("10"),
        order_type: OrderType::Limit,
        limit_price: Some(dec("150.00")),
        tif: Tif::Day,
        allocations: vec![MirrorAllocation {
            strategy_order_id: "so-1".to_string(),
            strategy_account: "ACC_1".to_string(),
            quantity: dec("10"),
        }],
        at: dt("2026-01-02T10:00:00+00:00"),
    };
    te_core::ledger::mirror::on_queued(&mut mirror, &q).unwrap();

    let now = dt("2026-01-02T10:30:00+00:00");
    let res = halt_unproven("paper-venue", &mirror, &now, "collect").unwrap();
    assert!(res.is_some(), "Venue must halt when open ticket has no proven Order ID (I5)");

    let (halt, cmd) = res.unwrap();
    assert_eq!(halt.venue, "paper-venue");
    assert!(!halt.reconciled);
    assert_eq!(halt.drift, vec!["AAPL"]);
    assert!(halt.note.unwrap().contains("no proven venue Order ID for open ticket(s) t-unproven-1"));
    assert!(cmd.starts_with("mirror:paper-venue:reconcile:unproven:collect:2026-01-02T10:30:00+00:00:"));
}

#[test]
fn test_halt_unproven_none_when_all_tickets_proven() {
    let mut mirror = MirrorState::default();
    let q = MirrorQueued {
        venue: "paper-venue".to_string(),
        ticket_key: "t-proven".to_string(),
        instrument: make_equity("SPY").unwrap(),
        side: Side::Buy,
        quantity: dec("5"),
        order_type: OrderType::Market,
        limit_price: None,
        tif: Tif::Day,
        allocations: vec![MirrorAllocation {
            strategy_order_id: "so-2".to_string(),
            strategy_account: "ACC_2".to_string(),
            quantity: dec("5"),
        }],
        at: dt("2026-01-02T10:00:00+00:00"),
    };
    te_core::ledger::mirror::on_queued(&mut mirror, &q).unwrap();
    let ack = MirrorAck {
        venue: "paper-venue".to_string(),
        ticket_key: "t-proven".to_string(),
        status: "ACCEPTED".to_string(),
        message: "accepted".to_string(),
        at: dt("2026-01-02T10:00:01+00:00"),
        venue_order_id: Some("v-1002".to_string()), // proven order id!
        book_status: Some(OrderState::Accepted),
    };
    te_core::ledger::mirror::on_ack(&mut mirror, &ack).unwrap();

    let now = dt("2026-01-02T10:30:00+00:00");
    let res = halt_unproven("paper-venue", &mirror, &now, "collect").unwrap();
    assert!(res.is_none(), "When all tickets are proven, halt_unproven returns None");
}

// -------------------------------------------------------------------------------------------------
// Group 5: Cover Rule Classification & Candidates
// -------------------------------------------------------------------------------------------------

#[test]
fn test_filter_candidates_deduplicates_and_skips_handled() {
    let mut mirror = MirrorState::default();
    mirror.queued_orders.insert("so-queued".to_string(), "t-1".to_string(), "v-1".to_string());

    let orders = vec![
        make_order("A", "so-1", OrderState::Submitted, dt("2026-01-02T10:00:00+00:00"), None),
        make_order("A", "so-1", OrderState::Submitted, dt("2026-01-02T10:00:00+00:00"), None), // duplicate: skip
        make_order("A", "so-queued", OrderState::Submitted, dt("2026-01-02T10:00:00+00:00"), None), // handled: skip
        make_order("A", "so-2", OrderState::Submitted, dt("2026-01-02T10:00:00+00:00"), None),
    ];

    let filtered = filter_candidates(&orders, &mirror);
    assert_eq!(filtered.len(), 2);
    assert_eq!(filtered[0].order_id, "so-1");
    assert_eq!(filtered[1].order_id, "so-2");
}

#[test]
fn test_classify_cover_short_call_waiting_vs_refusal() {
    let mirror = MirrorState::default(); // holds zero shares/calls
    let call_instr = Instrument::Option(OptionContract {
        underlying: "AAPL".to_string(),
        expiry: nd("2026-01-16"),
        strike: dec("200.00"),
        right: te_core::options::Right::Call,
        multiplier: 100,
    });
    let short_call = Order {
        order_id: "so-naked-call".to_string(),
        account_id: "A".to_string(),
        instrument: call_instr,
        order_type: OrderType::Limit,
        side: Side::Sell,
        quantity: dec("1"),
        command_id: "cmd-call".to_string(),
        created_at: dt("2026-01-02T10:00:00+00:00"),
        limit_price: Some(dec("2.50")),
        stop_price: None,
        trail_amount: None,
        tif: Tif::Day,
        state: OrderState::Submitted,
        parent_order_id: None,
        oco_group: None,
    };

    // 1. may_wait = false: refused immediately
    let (todo, waiting, refused) = classify_cover(&mirror, &[short_call.clone()], false).unwrap();
    assert!(todo.is_empty());
    assert!(waiting.is_empty());
    assert_eq!(refused.len(), 1);
    assert_eq!(refused[0].0, "so-naked-call");
    assert!(refused[0].1.contains("AAPL"));

    // 2. may_wait = true: held in waiting
    let (todo2, waiting2, refused2) = classify_cover(&mirror, &[short_call], true).unwrap();
    assert!(todo2.is_empty());
    assert!(refused2.is_empty());
    assert_eq!(waiting2.len(), 1);
    assert_eq!(waiting2[0].0, "so-naked-call");
}

// -------------------------------------------------------------------------------------------------
// Group 6: Write-Ahead & Drain Acks Invariants
// -------------------------------------------------------------------------------------------------

#[test]
fn test_prepare_write_ahead_refuses_unrecorded_reused_keys() {
    let mut accounts = HashMap::new();
    accounts.insert("so-1".to_string(), "ACC_1".to_string());

    let ticket = Ticket {
        venue_order_id: "t-reused".to_string(),
        instrument: make_equity("AAPL").unwrap(),
        order_type: OrderType::Limit,
        side: Side::Buy,
        quantity: dec("10"),
        submitted_at: "2026-01-02T10:00:00+00:00".to_string(),
        tif: Tif::Day,
        limit_price: Some(dec("150.00")),
        allocations: vec![("so-1".to_string(), "ACC_1".to_string(), dec("10"))],
    };

    let broker_queued_keys = std::collections::BTreeSet::new(); // empty -> ticket was NOT queued by broker!
    let now = dt("2026-01-02T10:05:00+00:00");

    let items = prepare_write_ahead(
        "paper-venue",
        &accounts,
        &[],
        &[],
        &[ticket],
        &broker_queued_keys,
        &now,
    );

    assert_eq!(items.len(), 1);
    match &items[0] {
        WriteAheadItem::Refused(r, cmd) => {
            assert_eq!(r.strategy_order_id, "so-1");
            assert!(r.reason.contains("ticket t-reused was already used by this broker process and never recorded; refused (I3)"));
            assert_eq!(cmd, "mirror:paper-venue:refused:so-1");
        }
        _ => panic!("Expected refusal for reused ticket key"),
    }
}

#[test]
fn test_build_drain_acks_and_cancel_ack() {
    let mut proven = HashMap::new();
    proven.insert("t-1".to_string(), ("v-999".to_string(), OrderState::Accepted));

    let acks = vec![
        ("t-1".to_string(), "ACCEPTED".to_string(), Some("filled".to_string()), dt("2026-01-02T10:00:01+00:00")),
        ("t-2".to_string(), "REJECTED".to_string(), None, dt("2026-01-02T10:00:02+00:00")),
    ];

    let drain_acks = build_drain_acks("paper-venue", &acks, &proven);
    assert_eq!(drain_acks.len(), 2);

    let (ack1, cmd1) = &drain_acks[0];
    assert_eq!(ack1.ticket_key, "t-1");
    assert_eq!(ack1.venue_order_id, Some("v-999".to_string()));
    assert_eq!(ack1.book_status, Some(OrderState::Accepted));
    assert_eq!(cmd1, "mirror:paper-venue:ack:t-1");

    let (ack2, cmd2) = &drain_acks[1];
    assert_eq!(ack2.ticket_key, "t-2");
    assert_eq!(ack2.venue_order_id, None);
    assert_eq!(ack2.book_status, None);
    assert_eq!(ack2.message, "REJECTED");
    assert_eq!(cmd2, "mirror:paper-venue:ack:t-2");

    // Cancel ack test
    let cancel = build_cancel_ack(
        "paper-venue",
        "t-1",
        Some("v-999"),
        "ACCEPTED",
        "cancelled by user",
        &dt("2026-01-02T10:05:00+00:00"),
    ).unwrap();
    assert_eq!(cancel.0.book_status, Some(OrderState::Cancelled));
    assert_eq!(cancel.1, "mirror:paper-venue:cancel:t-1");
}

// -------------------------------------------------------------------------------------------------
// Group 7: Lifting Operator Halts (§4.5, I11)
// -------------------------------------------------------------------------------------------------

#[test]
fn test_clear_halt_validations() {
    let now = dt("2026-01-02T12:00:00+00:00");
    let holders = vec!["ACC_1".to_string()];

    // 1. Empty reason refused (I11)
    let err = clear_halt("paper-venue", "   ", &now, &holders, &[]).unwrap_err();
    assert_eq!(err.kind, SESSION);
    assert!(err.msg.contains("a halt is lifted only with the operator's reason (I11)"));

    // 2. Not halted in any account
    let err2 = clear_halt("paper-venue", "clean", &now, &[], &[]).unwrap_err();
    assert_eq!(err2.kind, SESSION);
    assert!(err2.msg.contains("venue paper-venue is not halted"));

    // 3. Newest reconcile still drifts
    let reconciles = vec![
        (
            1,
            VenueReconcile {
                venue: "paper-venue".to_string(),
                as_of: dt("2026-01-02T11:00:00+00:00"),
                reconciled: false,
                drift: vec!["AAPL".to_string()],
                note: Some("position drift".to_string()),
            },
        ),
    ];
    let err3 = clear_halt("paper-venue", "clean", &now, &holders, &reconciles).unwrap_err();
    assert_eq!(err3.kind, SESSION);
    assert!(err3.msg.contains("still drifts: [\"AAPL\"] (position drift); fix it and collect again first"));

    // 4. Success when newest reconcile is clean
    let clean_reconciles = vec![
        (
            1,
            VenueReconcile {
                venue: "paper-venue".to_string(),
                as_of: dt("2026-01-02T11:00:00+00:00"),
                reconciled: false,
                drift: vec!["AAPL".to_string()],
                note: Some("old drift".to_string()),
            },
        ),
        (
            2,
            VenueReconcile {
                venue: "paper-venue".to_string(),
                as_of: dt("2026-01-02T11:30:00+00:00"),
                reconciled: true,
                drift: vec![],
                note: None,
            },
        ),
    ];
    let plan = clear_halt("paper-venue", "operator verified venue", &now, &holders, &clean_reconciles).unwrap();
    assert_eq!(plan.holders, vec!["ACC_1"]);
    assert_eq!(plan.events.len(), 1);
    let (acct, ev, cmd) = &plan.events[0];
    assert_eq!(acct, "ACC_1");
    assert_eq!(ev.venue, "paper-venue");
    assert_eq!(ev.reason, "operator verified venue");
    assert_eq!(ev.reconcile_seq, 2);
    assert_eq!(cmd, "halt-cleared:paper-venue:ACC_1:2");
}

// -------------------------------------------------------------------------------------------------
// Group 8: Door JSON Handlers
// -------------------------------------------------------------------------------------------------

#[test]
fn test_door_session_ops() {
    // 1. deferred_why
    let doc = r#"{"doing": "collecting fills", "exc": "connection lost", "recorded": ""}"#;
    let res = decide("session_deferred_why", doc).unwrap();
    let parsed = parse(&res).unwrap();
    let why = match parsed.get("why") {
        Some(Json::Str(s)) => s,
        _ => panic!("missing why"),
    };
    assert!(why.contains("the venue could not be asked while collecting fills (connection lost); nothing was sent or recorded; the next run asks again"));

    // 2. clear_halt via decide
    let clear_doc = r#"{
        "venue": "paper-venue",
        "reason": "fixed",
        "as_of": "2026-01-02T12:00:00+00:00",
        "holders": ["ACC_1"],
        "reconciles": [
            [1, {"dc": "VenueReconcile", "f": {"venue": "paper-venue", "as_of": {"T": "2026-01-02T11:00:00+00:00"}, "reconciled": true, "drift": {"t": []}, "note": {"n": true}}}]
        ]
    }"#;
    let res = decide("session_clear_halt", clear_doc).unwrap();
    let parsed = parse(&res).unwrap();
    let holders = match parsed.get("holders") {
        Some(Json::Arr(a)) => a,
        _ => panic!("missing holders"),
    };
    assert_eq!(holders.len(), 1);
}

//! Spec-derived and property tests for tos_paper normalisation and session logic.
//!
//! Enforces:
//! 1. An unrecognised status never becomes a terminal state (always PENDING or PENDING_UNKNOWN).
//! 2. Normalisation is idempotent (deterministic, stable fixed-point).
//! 3. Fill totals never exceed order quantity (working_order and ledger fold over-fill rejection).
//! 4. A cancelled order never fills afterwards (terminal state contradiction in fold and broker).
//!
//! Uses proptest.

use proptest::prelude::*;
use te_core::ledger::fold::{apply_event, AccountState};
use te_core::ledger::json::{dumps, parse, Json};
use te_core::ledger::model::*;
use te_core::money::Money;
use te_core::tos_paper::broker::{Broker, Fault, Host};
use te_core::tos_paper::normalize::{
    book_state_op, cancel_result, order_fill, place_result, position, working_order,
};

fn dec(s: &str) -> Money {
    Money::parse(s).expect("valid dec")
}

fn dt(s: &str) -> DateTime {
    parse_datetime(s).expect("valid dt").0
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
        instrument: make_equity("AAPL").unwrap(),
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

// -------------------------------------------------------------------------------------------------
// Mock Host for tos_paper::Broker
// -------------------------------------------------------------------------------------------------

#[derive(Default)]
struct MockBrokerHost {
    now: String,
    positions: Vec<Json>,
    working: Vec<Json>,
    fills: Vec<Json>,
    cancelled_ids: Vec<String>,
    can_fills: bool,
    can_cancel: bool,
}

impl Host for MockBrokerHost {
    fn now(&mut self) -> Result<String, Fault> {
        if self.now.is_empty() {
            Ok("2026-09-24T20:00:00+00:00".into())
        } else {
            Ok(self.now.clone())
        }
    }

    fn place_order(&mut self, _spec: &Json, _key: &str) -> Result<Json, Fault> {
        let raw = format!(r#"{{"status": "SENT", "order_id": "10001", "book_status": "WORKING"}}"#);
        Ok(parse(&raw).unwrap())
    }

    fn read_positions(&mut self) -> Result<Vec<Json>, Fault> {
        Ok(self.positions.clone())
    }

    fn read_working_orders(&mut self) -> Result<Vec<Json>, Fault> {
        Ok(self.working.clone())
    }

    fn read_order_fills(&mut self) -> Result<Vec<Json>, Fault> {
        Ok(self.fills.clone())
    }

    fn cancel_order(&mut self, order_id: &str) -> Result<Json, Fault> {
        self.cancelled_ids.push(order_id.to_string());
        let raw = format!(r#"{{"status": "CANCELED", "order_id": "{order_id}"}}"#);
        Ok(parse(&raw).unwrap())
    }

    fn can_read_fills(&mut self) -> Result<bool, Fault> {
        Ok(self.can_fills)
    }

    fn can_cancel(&mut self) -> Result<bool, Fault> {
        Ok(self.can_cancel)
    }
}

// -------------------------------------------------------------------------------------------------
// Property 1: An unrecognised status never becomes a terminal state
// -------------------------------------------------------------------------------------------------

const TERMINAL_STATES: [&str; 4] = ["FILLED", "CANCELLED", "REJECTED", "EXPIRED"];

#[test]
fn unrecognised_book_status_never_becomes_terminal() {
    let unrecognised_examples = [
        "", "UNKNOWN", "SOMETHING_NEW", "WAITING", "CONFIRMED", "PROCESSING",
        "IN_FLIGHT", "PENDING_ACK", "HELD", "123", "null", "suspended"
    ];

    for status in unrecognised_examples {
        let doc = parse(&format!(r#"{{"status": "{status}"}}"#)).unwrap();
        let res = book_state_op(&doc).expect("book_state_op succeeds");
        let state = res.get("state").and_then(|v| match v { Json::Str(s) => Some(s.as_str()), _ => None }).unwrap();

        assert_eq!(state, "PENDING_UNKNOWN", "Status '{}' must become PENDING_UNKNOWN", status);
        assert!(!TERMINAL_STATES.contains(&state), "Unrecognised status '{}' must not become terminal", status);
    }
}

#[test]
fn unrecognised_place_result_never_becomes_terminal_or_accepted() {
    let unrecognised_transport_statuses = [
        "ACCEPTED", "FILLED", "CONFIRMED", "RECEIVED", "WAITING", "UNKNOWN", "ROUTED"
    ];

    for status in unrecognised_transport_statuses {
        let doc = parse(&format!(r#"{{"raw": {{"status": "{status}"}}}}"#)).unwrap();
        let res = place_result(&doc).expect("place_result succeeds");
        let state = res.get("status").and_then(|v| match v { Json::Str(s) => Some(s.as_str()), _ => None }).unwrap();

        // Any unknown status must become PENDING awaiting reconcile, NEVER ACCEPTED or terminal
        assert_eq!(state, "PENDING", "Unrecognised transport status '{}' must become PENDING", status);
        assert!(!TERMINAL_STATES.contains(&state));
    }
}

#[test]
fn unrecognised_cancel_result_never_becomes_terminal_or_accepted() {
    let unrecognised_cancel_statuses = [
        "SUBMITTED", "WORKING", "PENDING", "UNKNOWN", "WAITING", "ERROR"
    ];

    for status in unrecognised_cancel_statuses {
        let doc = parse(&format!(r#"{{"raw": {{"status": "{status}"}}}}"#)).unwrap();
        let res = cancel_result(&doc).expect("cancel_result succeeds");
        let state = res.get("status").and_then(|v| match v { Json::Str(s) => Some(s.as_str()), _ => None }).unwrap();
        assert_eq!(state, "PENDING", "Cancel result with status '{}' must remain PENDING", status);
    }
}

proptest! {
    #![proptest_config(ProptestConfig::with_cases(100))]
    #[test]
    fn prop_unrecognised_status_never_becomes_terminal(status in "[a-zA-Z0-9_ ]{1,30}") {
        let recognized_terminal = ["FILLED", "CANCELED", "CANCELLED", "REJECTED", "EXPIRED"];
        let trimmed_upper = status.trim().to_uppercase();

        let doc = parse(&format!(r#"{{"status": "{status}"}}"#)).unwrap();
        let res = book_state_op(&doc).unwrap();
        let state = match res.get("state").unwrap() { Json::Str(s) => s.as_str(), _ => unreachable!() };

        if !recognized_terminal.contains(&trimmed_upper.as_str()) {
            // An unrecognised status must NEVER become a terminal state
            prop_assert!(
                !TERMINAL_STATES.contains(&state),
                "Unrecognised status '{}' normalized to terminal state '{}'",
                status,
                state
            );
        }
    }
}

// -------------------------------------------------------------------------------------------------
// Property 2: Normalisation is idempotent
// -------------------------------------------------------------------------------------------------

proptest! {
    #![proptest_config(ProptestConfig::with_cases(50))]
    #[test]
    fn prop_normalisation_is_idempotent_on_working_orders(
        qty in 1i64..1000,
        filled in 0i64..500,
        price_cents in 100i64..5000,
    ) {
        let filled = filled.min(qty); // ensure valid filled <= qty
        let raw = format!(
            r#"{{"symbol": "AAPL", "kind": "stock", "side": "buy", "quantity": "{}", "filled": "{}", "order_type": "LMT", "limit_price": "{}.{:02}", "status": "WORKING"}}"#,
            qty, filled, price_cents / 100, price_cents % 100
        );
        let doc = parse(&format!(r#"{{"raw": {raw}}}"#)).unwrap();

        let res1 = working_order(&doc).unwrap();
        let res2 = working_order(&doc).unwrap();

        // 1. Exact determinism on multiple invocations
        prop_assert_eq!(dumps(&res1), dumps(&res2), "working_order normalization must be identical across runs");

        // 2. Canonical values match
        prop_assert_eq!(res1.get("quantity"), Some(&Json::Str(qty.to_string())));
        prop_assert_eq!(res1.get("filled"), Some(&Json::Str(filled.to_string())));
        prop_assert_eq!(res1.get("state"), Some(&Json::Str("ACCEPTED".to_string())));
    }

    #[test]
    fn prop_normalisation_is_idempotent_on_positions(
        qty in -1000i64..1000,
        price_dollars in 1i64..500,
    ) {
        let raw = format!(
            r#"{{"symbol": "AAPL", "kind": "stock", "quantity": "{}", "avg_price": "{}.00"}}"#,
            qty, price_dollars
        );
        let doc = parse(&format!(r#"{{"raw": {raw}}}"#)).unwrap();

        let res1 = position(&doc).unwrap();
        let res2 = position(&doc).unwrap();

        prop_assert_eq!(dumps(&res1), dumps(&res2), "position normalization must be idempotent");
        prop_assert_eq!(res1.get("quantity"), Some(&Json::Str(qty.to_string())));
    }

    #[test]
    fn prop_normalisation_is_idempotent_on_order_fills(
        oid in 1000u32..9999,
        filled in 1i64..100,
        price_cents in 100i64..2000,
    ) {
        let raw = format!(
            r#"{{"order_id": "{}", "filled": "{}", "avg_price": "{}.{:02}", "status": "PARTIAL"}}"#,
            oid, filled, price_cents / 100, price_cents % 100
        );
        let doc = parse(&format!(r#"{{"raw": {raw}}}"#)).unwrap();

        let res1 = order_fill(&doc).unwrap();
        let res2 = order_fill(&doc).unwrap();

        prop_assert_eq!(dumps(&res1), dumps(&res2), "order_fill normalization must be idempotent");
    }
}

// -------------------------------------------------------------------------------------------------
// Property 3: Fill totals never exceed order quantity
// -------------------------------------------------------------------------------------------------

proptest! {
    #![proptest_config(ProptestConfig::with_cases(60))]
    #[test]
    fn prop_working_order_refuses_when_filled_exceeds_quantity(
        qty in 1i64..100,
        extra in 1i64..100,
    ) {
        let overfilled = qty + extra; // overfilled > qty
        let raw = format!(
            r#"{{"symbol": "AAPL", "kind": "stock", "side": "buy", "quantity": "{}", "filled": "{}", "order_type": "MKT", "limit_price": null, "status": "WORKING"}}"#,
            qty, overfilled
        );
        let doc = parse(&format!(r#"{{"raw": {raw}}}"#)).unwrap();

        let err = working_order(&doc).expect_err("filled > quantity must refuse in working_order (I5)");
        prop_assert_eq!(err.kind, "tos_normalize");
        let expected_msg = format!("working order quantity {} / filled {}", qty, overfilled);
        prop_assert!(err.msg.contains(&expected_msg));
    }
}

#[test]
fn ledger_fold_refuses_overfill_beyond_order_quantity() {
    let account = "ACC_TOS_PROP3";
    let mut st = AccountState::new(account);
    st.cash = dec("10000.00");

    let order = make_test_order(account, "ord-limit-10", "cmd-sub", 10, Side::Buy, OrderType::Market, OrderState::Submitted);
    let oid = order.order_id.clone();
    st.orders.put(&oid, order);

    // Fill for 11 shares against an order for 10 shares
    let overfill_ev = Event {
        account: account.into(),
        kind: EventKind::Fill,
        payload: Obj::Fill(Fill {
            fill_id: "fill-over-11".into(),
            order_id: "ord-limit-10".into(),
            account_id: account.into(),
            instrument: make_equity("AAPL").unwrap(),
            quantity: dec("11"), // 11 > 10!
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

    let err = apply_event(&mut st, &overfill_ev).expect_err("over-fill must refuse in ledger fold (I5)");
    assert_eq!(err.kind, "fold");
    assert!(err.msg.contains("would take order 'ord-limit-10' to 11 filled against an order size of 10; refusing the over-fill"));
}

#[test]
fn order_fill_refuses_zero_fill_marked_as_filled() {
    let raw = r#"{"order_id": "1001", "filled": "0", "status": "FILLED"}"#;
    let doc = parse(&format!(r#"{{"raw": {raw}}}"#)).unwrap();
    let err = order_fill(&doc).expect_err("FILLED with filled=0 must refuse (I5)");
    assert_eq!(err.kind, "tos_normalize");
    assert!(err.msg.contains("reads FILLED with nothing filled"));
}

// -------------------------------------------------------------------------------------------------
// Property 4: A cancelled order never fills afterwards
// -------------------------------------------------------------------------------------------------

#[test]
fn cancelled_order_never_fills_afterwards_in_ledger_fold() {
    let account = "ACC_TOS_PROP4";
    let mut st = AccountState::new(account);
    st.cash = dec("10000.00");

    let order = make_test_order(account, "ord-cancelled", "cmd-sub", 10, Side::Buy, OrderType::Market, OrderState::Submitted);
    let oid = order.order_id.clone();
    st.orders.put(&oid, order);

    // 1. Order transition to Cancelled
    let cancel_ev = Event {
        account: account.into(),
        kind: EventKind::OrderCancelled,
        payload: Obj::StateChange(OrderStateChange {
            order_id: "ord-cancelled".into(),
            reason: Some("cancelled by user".into()),
            venue_order_id: Some("v-100".into()),
        }),
        ts_utc: dt("2026-01-02T10:01:00+00:00"),
        command_id: Some("cmd-cancel".into()),
        schema_version: 1,
        seq: Some(1),
    };
    apply_event(&mut st, &cancel_ev).expect("order cancel succeeds");
    assert_eq!(st.orders.get("ord-cancelled").unwrap().state, OrderState::Cancelled);

    // 2. An incoming fill afterwards must refuse with I5 contradiction
    let fill_ev = Event {
        account: account.into(),
        kind: EventKind::Fill,
        payload: Obj::Fill(Fill {
            fill_id: "fill-late".into(),
            order_id: "ord-cancelled".into(),
            account_id: account.into(),
            instrument: make_equity("AAPL").unwrap(),
            quantity: dec("10"),
            price: dec("100.00"),
            venue_env: "sim".into(),
            filled_at: dt("2026-01-02T10:02:00+00:00"),
            side: Side::Buy,
            fee: dec("1.00"),
            leg_id: None,
            venue_order_id: Some("v-100".into()),
            venue_execution_id: Some("exec-100".into()),
        }),
        ts_utc: dt("2026-01-02T10:02:00+00:00"),
        command_id: None,
        schema_version: 1,
        seq: Some(2),
    };

    let err = apply_event(&mut st, &fill_ev).expect_err("fill after cancel must refuse (I5)");
    assert_eq!(err.kind, "fold");
    assert!(err.msg.contains("arrived for order 'ord-cancelled' already in terminal state CANCELLED; the log contradicts the order state (I5)"));
}

#[test]
fn broker_drops_cancelled_queued_order_so_it_never_sends_or_fills() {
    let mut broker = Broker::new("paper-venue", vec!["ACC_1".into()], false);
    broker.mark_connected();

    let mut host = MockBrokerHost::default();
    host.can_cancel = true;

    let orders_json = vec![parse(r#"{
        "order_id": "so-1",
        "account_id": "ACC_1",
        "instrument": {"kind": "equity", "symbol": "AAPL"},
        "side": "BUY",
        "quantity": "100",
        "order_type": "LIMIT",
        "limit_price": "150.00",
        "tif": "DAY",
        "command_id": "cmd-1"
    }"#).unwrap()];

    let holdings_json = vec![];
    let mirror_res = broker.mirror_batch(&mut host, &orders_json, &holdings_json).unwrap();
    let queued = match mirror_res.get("venue_orders") {
        Some(Json::Arr(a)) => a,
        _ => panic!("expected array of venue_orders"),
    };
    assert_eq!(queued.len(), 1);

    let ticket_key = match queued[0].get("venue_order_id") {
        Some(Json::Str(k)) => k.clone(),
        _ => panic!("missing venue_order_id"),
    };

    // 2. Cancel the ticket before drain
    let cancel_res = broker.cancel(&mut host, &ticket_key).unwrap();
    assert_eq!(cancel_res.get("status"), Some(&Json::Str("ACCEPTED".into())));
    match cancel_res.get("message") {
        Some(Json::Str(msg)) => assert!(msg.contains("cancelled before send")),
        other => panic!("expected string message, got {:?}", other),
    }

    // 3. Drain the broker: nothing should be sent to the transport
    let drain_res = broker.drain(&mut host).unwrap();
    let acks = match drain_res.get("acks") {
        Some(Json::Arr(a)) => a,
        _ => panic!("expected array of acks"),
    };
    assert!(acks.is_empty(), "Cancelled ticket was dropped and never sent");
    assert!(host.cancelled_ids.is_empty(), "Never sent to venue, so no venue cancel needed");
}

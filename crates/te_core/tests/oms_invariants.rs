//! Spec-derived invariant tests for OMS components:
//! - OMS-3: Durable before network; failing network submit yields broker_unknown and leaves order persisted in pending (RP:52, flow.rs:873, 949).
//! - oms/structures.rs: OMS-1 uncovered calls (C3/I8), working order cover exclusion, multiplier consistency, open_structures.
//! - oms/reconcile.rs: orders_to_read filtering of NEW/terminal orders, unknown order refusal (I5), journal_payload seq requirement (I1).
//! - oms/restore.rs: OMS-6 venue restore, PENDING_UNKNOWN handling in refuse and resolve modes, submission event requirement (I5), positions_plan.

use std::cell::RefCell;
use chrono::NaiveDate;
use te_core::ledger::fold::{apply_event, AccountState};
use te_core::ledger::model::*;
use te_core::money::Money;
use te_core::oms::flow::{
    self, Capabilities, Host, Net, OrderChanges, VenueAck, VenueFill, VenueOrder, VenueOrderState,
};
use te_core::oms::reconcile::{check_known_order, journal_payload, orders_to_read};
use te_core::oms::restore::{pending_request, positions_plan, restorable, Callbacks, PendingEvent};
use te_core::oms::structures::{is_structure, multiplier, uncovered_calls};

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

fn make_option_contract(symbol: &str, expiry: NaiveDate, strike: &str, right: te_core::options::Right) -> OptionContract {
    OptionContract {
        underlying: symbol.to_string(),
        expiry,
        strike: dec(strike),
        right,
        multiplier: 100,
    }
}

fn put_order(st: &mut AccountState, o: Order) {
    let id = o.order_id.clone();
    st.orders.put(&id, o);
}

// -------------------------------------------------------------------------------------------------
// Mock Host for OMS-3 Durable-Before-Network Testing
// -------------------------------------------------------------------------------------------------

struct MockOmsHost {
    account: String,
    state: RefCell<AccountState>,
    appended: RefCell<Vec<Event>>,
    events: RefCell<Vec<Event>>,
    fail_submit: bool,
    fail_replace: bool,
    now: DateTime,
}

impl MockOmsHost {
    fn new(account: &str) -> Self {
        Self {
            account: account.to_string(),
            state: RefCell::new(AccountState::new(account)),
            appended: RefCell::new(Vec::new()),
            events: RefCell::new(Vec::new()),
            fail_submit: false,
            fail_replace: false,
            now: dt("2026-01-02T15:00:00+00:00"),
        }
    }
}

impl Host for MockOmsHost {
    type Venue = VenueOrder;

    fn now_utc(&self) -> R<(DateTime, bool)> {
        Ok((self.now.clone(), true))
    }

    fn accounts(&self) -> R<Vec<String>> {
        Ok(vec![self.account.clone()])
    }

    fn account_state(&self, account: &str) -> R<AccountState> {
        if account == self.account {
            Ok(self.state.borrow().clone())
        } else {
            Ok(AccountState::new(account))
        }
    }

    fn event_by_command(&self, command: &str) -> R<Option<Event>> {
        Ok(self.events.borrow().iter().find(|e| e.command_id.as_deref() == Some(command)).cloned())
    }

    fn events_of_kind(&self, kind: EventKind) -> R<Vec<Event>> {
        Ok(self.events.borrow().iter().filter(|e| e.kind == kind).cloned().collect())
    }

    fn append(&self, event: &Event) -> R<Event> {
        let mut ev = event.clone();
        let seq = self.appended.borrow().len() as i128 + 1;
        ev.seq = Some(seq);
        self.appended.borrow_mut().push(ev.clone());
        self.events.borrow_mut().push(ev.clone());
        let _ = apply_event(&mut self.state.borrow_mut(), &ev);
        Ok(ev)
    }

    fn capabilities(&self) -> R<Capabilities> {
        Ok(Capabilities {
            types: vec![OrderType::Market, OrderType::Limit, OrderType::Stop],
            tifs: vec![Tif::Day, Tif::Gtc],
            native_stops: true,
        })
    }

    fn env(&self) -> R<String> {
        Ok("sim".to_string())
    }

    fn venue_order(&self, order: &VenueOrder) -> R<Self::Venue> {
        Ok(order.clone())
    }

    fn submit(&self, _order: &Self::Venue) -> R<Net<VenueAck>> {
        if self.fail_submit {
            Ok(Net::Failed)
        } else {
            Ok(Net::Ok(VenueAck {
                venue_order_id: "v-1".into(),
                status: "ACCEPTED".into(),
                message: None,
            }))
        }
    }

    fn cancel(&self, _venue_order_id: &str) -> R<Net<VenueAck>> {
        Ok(Net::Ok(VenueAck {
            venue_order_id: "v-1".into(),
            status: "ACCEPTED".into(),
            message: None,
        }))
    }

    fn replace(&self, venue_order_id: &str, _changes: &OrderChanges) -> R<Net<VenueAck>> {
        if self.fail_replace {
            Ok(Net::Failed)
        } else {
            Ok(Net::Ok(VenueAck {
                venue_order_id: venue_order_id.to_string(),
                status: "ACCEPTED".into(),
                message: None,
            }))
        }
    }

    fn orders(&self, _since: &DateTime) -> R<Vec<VenueOrderState>> {
        Ok(vec![])
    }

    fn fills(&self, _since: &DateTime) -> R<Vec<VenueFill>> {
        Ok(vec![])
    }

    fn cause(&self, _e: LErr) {}
}

// -------------------------------------------------------------------------------------------------
// 1. OMS-3: Order persisted before network call; failing submit yields broker_unknown
// -------------------------------------------------------------------------------------------------

#[test]
fn test_oms_3_submit_persisted_before_network_call_and_failing_submit_yields_broker_unknown() {
    let mut host = MockOmsHost::new("ACC_OMS3");
    host.fail_submit = true;

    let order = make_test_order("ACC_OMS3", "ord-oms3", "cmd-oms3", 10, Side::Buy, OrderType::Market, OrderState::New);
    // Seed initial order in state
    put_order(&mut host.state.borrow_mut(), order.clone());

    let res = flow::submit_native(&host, &order);
    let err = res.expect_err("failing broker submit must yield broker_unknown error (OMS-3)");
    assert_eq!(err.kind, "broker_unknown");
    assert!(err.msg.contains("Submit outcome for order 'ord-oms3' is unknown"));

    // Verify durable-before-network: Order was persisted into the ledger BEFORE the network call failed
    let appended = host.appended.borrow();
    assert_eq!(appended.len(), 2, "Expected 2 events persisted before network submit");
    assert_eq!(appended[0].kind, EventKind::OrderUpdated);
    assert_eq!(appended[0].command_id, Some("cmd-oms3:submit".into()));
    assert_eq!(appended[1].kind, EventKind::OrderPending);
    assert_eq!(appended[1].command_id, Some("cmd-oms3:submit-pending".into()));

    // Order state in folded ledger is now PendingUnknown, awaiting reconciliation (I10 / OMS-3)
    let current_order = host.state.borrow().orders.get("ord-oms3").cloned().unwrap();
    assert_eq!(current_order.state, OrderState::PendingUnknown);
}

#[test]
fn test_oms_3_replace_persisted_before_network_call_and_failing_replace_yields_broker_unknown() {
    let mut host = MockOmsHost::new("ACC_OMS3");
    host.fail_replace = true;

    let order = make_test_order("ACC_OMS3", "ord-rep", "cmd-rep", 10, Side::Buy, OrderType::Limit, OrderState::Submitted);
    put_order(&mut host.state.borrow_mut(), order.clone());
    host.state.borrow_mut().venue_order_ids.put(&"ord-rep".to_string(), "v-rep".into());

    let changes = OrderChanges {
        new_quantity: Some(dec("20")),
        new_limit_price: Some(dec("105.00")),
        new_stop_price: None,
    };

    let res = flow::replace(&host, "ord-rep", &changes, "cmd-rep-call");
    let err = res.expect_err("failing broker replace must yield broker_unknown error (OMS-3)");
    assert_eq!(err.kind, "broker_unknown");
    assert!(err.msg.contains("Replace outcome for order 'ord-rep' is unknown; reconciliation is required"));

    // Verify OrderPending was persisted under cmd-rep-call:pending BEFORE the network call failed
    let appended = host.appended.borrow();
    assert!(!appended.is_empty());
    let pending_ev = &appended[0];
    assert_eq!(pending_ev.kind, EventKind::OrderPending);
    assert_eq!(pending_ev.command_id, Some("cmd-rep-call:pending".into()));
}

// -------------------------------------------------------------------------------------------------
// 2. oms/structures.rs: OMS-1 uncovered calls, multiplier, is_structure, open_structures
// -------------------------------------------------------------------------------------------------

#[test]
fn test_oms_1_uncovered_calls_requires_cover_or_shares() {
    let mut st = AccountState::new("ACC_OMS1");

    let call_exp = NaiveDate::from_ymd_opt(2026, 1, 16).unwrap();
    let contract = make_option_contract("SPY", call_exp, "100.00", te_core::options::Right::Call);
    let opt_instr = Instrument::Option(contract);
    let hk = opt_instr.hk();

    // 1. Naked short call (1 contract = 100 shares deliverable) with 0 shares and 0 long calls
    let pos_short = te_core::ledger::fold::make_position(
        "ACC_OMS1",
        opt_instr.clone(),
        dec("-1"),
        dec("2.50"),
        dec("0"),
        vec![te_core::ledger::model::make_lot("l1".into(), dec("1"), dec("2.50"), dt("2026-01-02T10:00:00+00:00"), Side::Sell).unwrap()],
    ).unwrap();
    st.positions.insert(hk.clone(), opt_instr.clone(), pos_short);

    let res = uncovered_calls(&st, true).unwrap();
    assert_eq!(res.len(), 1);
    assert_eq!(res[0].0, "SPY");
    assert_eq!(res[0].1, dec("100"), "Naked short call must report 100 shares uncovered (OMS-1 / C3)");
    assert_eq!(res[0].2, dec("0"), "Free shares must be 0");

    // 2. Add 100 shares of underlying equity to cover the short call
    let eq_instr = make_equity("SPY").unwrap();
    let eq_hk = eq_instr.hk();
    let pos_shares = te_core::ledger::fold::make_position(
        "ACC_OMS1",
        eq_instr.clone(),
        dec("100"),
        dec("100.00"),
        dec("0"),
        vec![te_core::ledger::model::make_lot("ls".into(), dec("100"), dec("100.00"), dt("2026-01-02T10:00:00+00:00"), Side::Buy).unwrap()],
    ).unwrap();
    st.positions.insert(eq_hk.clone(), eq_instr.clone(), pos_shares);

    let res = uncovered_calls(&st, true).unwrap();
    assert_eq!(res.len(), 1);
    assert_eq!(res[0].1, dec("100"), "Uncovered calls before subtracting free shares is 100");
    assert_eq!(res[0].2, dec("100"), "Held 100 free shares cover the delivery");
}

#[test]
fn test_oms_1_working_sell_order_shares_do_not_count_as_cover() {
    let mut st = AccountState::new("ACC_OMS1");

    // Account holds 100 shares of SPY
    let eq_instr = make_equity("SPY").unwrap();
    let pos_shares = te_core::ledger::fold::make_position(
        "ACC_OMS1",
        eq_instr.clone(),
        dec("100"),
        dec("100.00"),
        dec("0"),
        vec![te_core::ledger::model::make_lot("ls".into(), dec("100"), dec("100.00"), dt("2026-01-02T10:00:00+00:00"), Side::Buy).unwrap()],
    ).unwrap();
    st.positions.insert(eq_instr.hk(), eq_instr.clone(), pos_shares);

    // But a working sell order is selling all 100 shares
    let working_sell = make_test_order("ACC_OMS1", "ord-sell", "cmd-sell", 100, Side::Sell, OrderType::Limit, OrderState::Submitted);
    put_order(&mut st, working_sell);

    let res = uncovered_calls(&st, true).unwrap();
    assert_eq!(res.len(), 1);
    assert_eq!(res[0].2, dec("0"), "Shares committed to working sell order must NOT count as free cover (OMS-1 / I8)");
}

#[test]
fn test_oms_1_long_call_must_expire_no_earlier_to_cover() {
    let mut st = AccountState::new("ACC_OMS1");

    let short_exp = NaiveDate::from_ymd_opt(2026, 1, 16).unwrap();
    let short_c = make_option_contract("SPY", short_exp, "105.00", te_core::options::Right::Call);
    let short_instr = Instrument::Option(short_c);
    let pos_short = te_core::ledger::fold::make_position(
        "ACC_OMS1",
        short_instr.clone(),
        dec("-1"),
        dec("2.00"),
        dec("0"),
        vec![te_core::ledger::model::make_lot("ls".into(), dec("1"), dec("2.00"), dt("2026-01-02T10:00:00+00:00"), Side::Sell).unwrap()],
    ).unwrap();
    st.positions.insert(short_instr.hk(), short_instr.clone(), pos_short);

    // 1. Earlier expiring long call (2026-01-09 < 2026-01-16) does NOT cover
    let earlier_exp = NaiveDate::from_ymd_opt(2026, 1, 9).unwrap();
    let earlier_c = make_option_contract("SPY", earlier_exp, "100.00", te_core::options::Right::Call);
    let earlier_instr = Instrument::Option(earlier_c);
    let pos_earlier = te_core::ledger::fold::make_position(
        "ACC_OMS1",
        earlier_instr.clone(),
        dec("1"),
        dec("3.00"),
        dec("0"),
        vec![te_core::ledger::model::make_lot("le".into(), dec("1"), dec("3.00"), dt("2026-01-02T10:00:00+00:00"), Side::Buy).unwrap()],
    ).unwrap();
    st.positions.insert(earlier_instr.hk(), earlier_instr.clone(), pos_earlier);

    let res = uncovered_calls(&st, true).unwrap();
    assert_eq!(res[0].1, dec("100"), "Earlier expiry long call cannot cover later short call (OMS-1)");

    // 2. Same expiring long call (2026-01-16 == 2026-01-16) DOES cover
    let same_exp = NaiveDate::from_ymd_opt(2026, 1, 16).unwrap();
    let same_c = make_option_contract("SPY", same_exp, "100.00", te_core::options::Right::Call);
    let same_instr = Instrument::Option(same_c);
    let pos_same = te_core::ledger::fold::make_position(
        "ACC_OMS1",
        same_instr.clone(),
        dec("1"),
        dec("4.00"),
        dec("0"),
        vec![te_core::ledger::model::make_lot("lsame".into(), dec("1"), dec("4.00"), dt("2026-01-02T10:00:00+00:00"), Side::Buy).unwrap()],
    ).unwrap();
    st.positions.insert(same_instr.hk(), same_instr.clone(), pos_same);

    let res = uncovered_calls(&st, true).unwrap();
    assert_eq!(res[0].1, dec("0"), "Same expiry long call covers the short call (OMS-1 >=)");
}

#[test]
fn test_structures_multiplier_mixed_legs_refuse() {
    let c1 = make_option_contract("SPY", NaiveDate::from_ymd_opt(2026, 1, 16).unwrap(), "100.00", te_core::options::Right::Call);
    let mut c2 = c1.clone();
    c2.multiplier = 10; // different multiplier!

    let combo = Instrument::Combo(vec![
        ComboLeg { contract: Instrument::Option(c1), ratio: 1, side: Side::Buy },
        ComboLeg { contract: Instrument::Option(c2), ratio: 1, side: Side::Sell },
    ]);

    let err = multiplier(&combo).expect_err("mixed multiplier combo must refuse (I6)");
    assert_eq!(err.kind, "value");
    assert!(err.msg.contains("Combo has mixed leg multipliers [10, 100]; value it per leg (I6)"));
}

#[test]
fn test_structures_is_structure_options_and_combos() {
    let opt = Instrument::Option(make_option_contract("SPY", NaiveDate::from_ymd_opt(2026, 1, 16).unwrap(), "100.00", te_core::options::Right::Call));
    let eq = make_equity("SPY").unwrap();
    let fut = te_core::ledger::model::make_future("/ES").unwrap();

    assert!(is_structure(&opt));
    assert!(!is_structure(&eq));
    assert!(!is_structure(&fut));

    let combo_opt = Instrument::Combo(vec![
        ComboLeg { contract: opt.clone(), ratio: 1, side: Side::Buy }
    ]);
    assert!(is_structure(&combo_opt));

    let combo_mixed = Instrument::Combo(vec![
        ComboLeg { contract: opt.clone(), ratio: 1, side: Side::Buy },
        ComboLeg { contract: eq.clone(), ratio: 100, side: Side::Sell },
    ]);
    assert!(!is_structure(&combo_mixed), "Combo with equity leg is not purely options structure");
}

// -------------------------------------------------------------------------------------------------
// 3. oms/reconcile.rs: orders_to_read, check_known_order, journal_payload
// -------------------------------------------------------------------------------------------------

#[test]
fn test_reconcile_orders_to_read_filters_new_and_terminal() {
    let mut st = AccountState::new("ACC_REC");

    let o_new = make_test_order("ACC_REC", "o_new", "cmd-new", 10, Side::Buy, OrderType::Market, OrderState::New);
    let o_sub = make_test_order("ACC_REC", "o_sub", "cmd-sub", 10, Side::Buy, OrderType::Market, OrderState::Submitted);
    let o_acc = make_test_order("ACC_REC", "o_acc", "cmd-acc", 10, Side::Buy, OrderType::Limit, OrderState::Accepted);
    let o_fil = make_test_order("ACC_REC", "o_fil", "cmd-fil", 10, Side::Buy, OrderType::Limit, OrderState::Filled);
    let o_can = make_test_order("ACC_REC", "o_can", "cmd-can", 10, Side::Buy, OrderType::Limit, OrderState::Cancelled);

    put_order(&mut st, o_new);
    put_order(&mut st, o_sub);
    put_order(&mut st, o_acc);
    put_order(&mut st, o_fil);
    put_order(&mut st, o_can);

    let venue_ids = vec![
        "o_new".into(),
        "o_sub".into(),
        "o_acc".into(),
        "o_fil".into(),
        "o_can".into(),
        "o_unknown".into(),
    ];

    let to_read = orders_to_read(&st, &venue_ids);
    assert_eq!(to_read, vec!["o_sub", "o_acc"], "orders_to_read must exclude NEW, terminal and unknown orders");
}

#[test]
fn test_reconcile_check_known_order_refuses_unknown() {
    let mut st = AccountState::new("ACC_REC");
    let o_known = make_test_order("ACC_REC", "o_known", "cmd-k", 10, Side::Buy, OrderType::Market, OrderState::Submitted);
    put_order(&mut st, o_known);

    assert!(check_known_order(&st, "fill-1", "o_known", "ACC_REC").is_ok());

    let err = check_known_order(&st, "fill-2", "o_stranger", "ACC_REC").expect_err("unknown order must refuse");
    assert_eq!(err.kind, "reconcile");
    assert!(err.msg.contains("references unknown order 'o_stranger' for 'ACC_REC' (I5)"));
}

#[test]
fn test_reconcile_journal_payload_requires_seq() {
    let mut st = AccountState::new("ACC_REC");
    let order = make_test_order("ACC_REC", "o_j", "cmd-j:sub", 10, Side::Buy, OrderType::Market, OrderState::Submitted);
    put_order(&mut st, order);

    let fill = Fill {
        fill_id: "f_j".into(),
        order_id: "o_j".into(),
        account_id: "ACC_REC".into(),
        instrument: make_equity("SPY").unwrap(),
        quantity: dec("10"),
        price: dec("100.00"),
        venue_env: "sim".into(),
        filled_at: dt("2026-01-02T15:00:00+00:00"),
        side: Side::Buy,
        fee: dec("1.00"),
        leg_id: None,
        venue_order_id: None,
        venue_execution_id: None,
    };

    // 1. Missing seq / ledger event missing refuses with I1
    let err = journal_payload(&st, &fill, "JOURNAL_ACC", false).expect_err("missing seq must refuse (I1)");
    assert_eq!(err.kind, "reconcile");
    assert!(err.msg.contains("recorded but its ledger event is missing (I1)"));

    // 2. With seq succeeds and produces complete execution payload
    let payload = journal_payload(&st, &fill, "JOURNAL_ACC", true).expect("valid journal payload");
    assert_eq!(payload.get("symbol"), Some(&te_core::ledger::json::Json::Str("SPY".into())));
    assert_eq!(payload.get("asset_class"), Some(&te_core::ledger::json::Json::Str("equity".into())));
    assert_eq!(payload.get("multiplier"), Some(&te_core::ledger::json::Json::Int(1)));
    assert_eq!(payload.get("strategy_tag"), Some(&te_core::ledger::json::Json::Str("cmd-j".into())));
}

// -------------------------------------------------------------------------------------------------
// 4. oms/restore.rs: refuse and resolve modes, PENDING_UNKNOWN, missing submission event
// -------------------------------------------------------------------------------------------------

#[test]
fn test_restore_refuse_mode_refuses_pending_unknown() {
    let mut st = AccountState::new("ACC_RES");
    let order = make_test_order("ACC_RES", "ord-pu", "cmd-pu", 10, Side::Buy, OrderType::Market, OrderState::PendingUnknown);
    put_order(&mut st, order);

    let mut note_called = false;
    let mut submitted_called = false;
    let mut cb = Callbacks {
        note: &mut |_res, _unres| { note_called = true; Ok(()) },
        submitted: &mut |_cmd| { submitted_called = true; Ok(true) },
    };

    let err = restorable(&st, "ACC_RES", "refuse", false, &[], &[], &mut cb)
        .expect_err("pending='refuse' must refuse PENDING_UNKNOWN order (I5)");
    assert_eq!(err.kind, "restore");
    assert!(err.msg.contains("is PENDING_UNKNOWN; the simulated venue's answer is gone, reconcile it before replay (I5)"));
}

#[test]
fn test_restore_resolve_mode_classifies_submit_and_cancel() {
    let mut st = AccountState::new("ACC_RES");

    let o1 = make_test_order("ACC_RES", "o_sub", "cmd-1", 10, Side::Buy, OrderType::Market, OrderState::PendingUnknown);
    let o2 = make_test_order("ACC_RES", "o_can", "cmd-2", 10, Side::Buy, OrderType::Market, OrderState::PendingUnknown);
    put_order(&mut st, o1);
    put_order(&mut st, o2);

    let pending_events = vec![
        PendingEvent {
            order_id: "o_sub".into(),
            command_id: Some("cmd-1:submit-pending".into()),
            reason: "Submit pending".into(),
        },
        PendingEvent {
            order_id: "o_can".into(),
            command_id: Some("cmd-2:pending".into()),
            reason: "Cancel pending: user cancel".into(),
        },
    ];

    assert_eq!(pending_request(&pending_events, "o_sub"), "submit");
    assert_eq!(pending_request(&pending_events, "o_can"), "cancel");

    let mut noted_res = Vec::new();
    let mut cb = Callbacks {
        note: &mut |res, _unres| { noted_res = res.to_vec(); Ok(()) },
        submitted: &mut |_cmd| Ok(true),
    };

    let plan = restorable(&st, "ACC_RES", "resolve", true, &[], &pending_events, &mut cb).unwrap();
    assert_eq!(noted_res, vec!["o_sub", "o_can"]);

    // o_sub restored as Accepted, o_can restored as Cancelled
    let sub_plan = plan.orders.iter().find(|(id, _, _)| id == "o_sub").unwrap();
    assert_eq!(sub_plan.2, OrderState::Accepted.value());

    let can_plan = plan.orders.iter().find(|(id, _, _)| id == "o_can").unwrap();
    assert_eq!(can_plan.2, OrderState::Cancelled.value());
}

#[test]
fn test_restore_refuses_order_with_no_submission_event() {
    let mut st = AccountState::new("ACC_RES");
    let o = make_test_order("ACC_RES", "o_nosub", "cmd-no", 10, Side::Buy, OrderType::Market, OrderState::Accepted);
    put_order(&mut st, o);

    let mut cb = Callbacks {
        note: &mut |_res, _unres| Ok(()),
        submitted: &mut |_cmd| Ok(false), // NO submission event in ledger!
    };

    let err = restorable(&st, "ACC_RES", "refuse", false, &[], &[], &mut cb)
        .expect_err("order with no submission event must refuse (I5)");
    assert_eq!(err.kind, "restore");
    assert!(err.msg.contains("has no submission event; cannot restore when it reached the venue (I5)"));
}

#[test]
fn test_restore_positions_plan_skips_flat() {
    let mut st = AccountState::new("ACC_RES");

    let eq_held = make_equity("SPY").unwrap();
    let pos_held = te_core::ledger::fold::make_position(
        "ACC_RES",
        eq_held.clone(),
        dec("50"),
        dec("100.00"),
        dec("0"),
        vec![te_core::ledger::model::make_lot("l1".into(), dec("50"), dec("100.00"), dt("2026-01-02T10:00:00+00:00"), Side::Buy).unwrap()],
    ).unwrap();
    st.positions.insert(eq_held.hk(), eq_held.clone(), pos_held);

    let eq_flat = make_equity("AAPL").unwrap();
    let pos_flat = te_core::ledger::fold::make_position(
        "ACC_RES",
        eq_flat.clone(),
        dec("0"),
        dec("0"),
        dec("0"),
        vec![],
    ).unwrap();
    st.positions.insert(eq_flat.hk(), eq_flat.clone(), pos_flat);

    let plan = positions_plan(&st).unwrap();
    assert_eq!(plan.len(), 1, "positions_plan must skip flat positions (quantity == 0)");
    assert_eq!(plan[0].0, 0, "Index 0 (SPY) is the only non-flat position");
}

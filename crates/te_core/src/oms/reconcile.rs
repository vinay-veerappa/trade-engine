//! `reconcile.py`'s decisions (docs/RUST_PORT.md P3b-1): which venue orders are read back
//! after a bar's fills are recorded, which venue fill refuses (I5), and the journal
//! payload one fill becomes. Ledger and venue I/O stay with the host.
//!
//! W5 P2 moved three more decisions here: which bracket entry and fill is the resync
//! cause (`bracket_resync_cause`), which brackets to resync (`brackets_to_resync`), and
//! whether a recorded fill heals its bracket and re-journals (`heal_plan`).

use std::collections::HashSet;

use crate::ledger::codec::py_repr;
use crate::ledger::fold::AccountState;
use crate::ledger::json::Json;
use crate::ledger::model::{err, Fill, Instrument, LErr, Order, OrderState, OrderType, R};

fn terminal(s: OrderState) -> bool {
    matches!(s, OrderState::Filled | OrderState::Cancelled | OrderState::Expired | OrderState::Rejected)
}

/// Python's `KeyError(id)`: the refusal the ledger's `key` kind becomes.
fn key_err(id: &str) -> LErr {
    LErr { kind: "key", msg: py_repr(id) }
}

/// The venue orders `reconcile_after` re-reads, in the venue's order: those the ledger
/// knows and that are neither NEW (never sent) nor terminal (already folded).
pub fn orders_to_read(st: &AccountState, venue_order_ids: &[String]) -> Vec<String> {
    venue_order_ids
        .iter()
        .filter(|id| match st.orders.get(id) {
            None => false,
            Some(o) => o.state != OrderState::New && !terminal(o.state),
        })
        .cloned()
        .collect()
}

/// A venue fill for an order the ledger does not know refuses (I5).
pub fn check_known_order(st: &AccountState, fill_id: &str, venue_order_id: &str, account_id: &str) -> R<()> {
    if st.orders.contains(venue_order_id) {
        return Ok(());
    }
    err(
        "reconcile",
        format!("Venue fill '{fill_id}' references unknown order '{venue_order_id}' for '{account_id}' (I5)"),
    )
}

/// A recorded fill whose ledger event is missing, or has no seq, refuses (I1).
fn check_fill_event(fill_id: &str, has_seq: bool) -> R<()> {
    if has_seq {
        return Ok(());
    }
    err("reconcile", format!("Fill '{fill_id}' was recorded but its ledger event is missing (I1)"))
}

fn text_of(v: &Option<crate::money::Money>) -> String {
    v.as_ref().map_or_else(|| "None".to_string(), |d| d.canon())
}

/// An integral factor is a JSON int (as before); a fractional point value (MYM 0.5) is its decimal text.
fn multiplier_json(m: &crate::money::Money) -> Json {
    match m.to_i128_exact() {
        Some(n) => Json::Int(n),
        None => Json::Float(m.canon()),
    }
}

/// The journal execution one fill becomes, as an ordered JSON object: the asset class,
/// the multiplier and the bracket's stop and target. `has_seq`: the fill's ledger event was
/// found with a seq; the check sits where Python's did, after the bracket lookups.
pub fn journal_payload(st: &AccountState, fill: &Fill, journal_account: &str, has_seq: bool) -> R<Json> {
    let order = st.orders.get(&fill.order_id).ok_or_else(|| key_err(&fill.order_id))?;
    let entry = entry_of(st, order)?;
    let children: Vec<_> =
        st.orders.values().filter(|c| c.parent_order_id.as_deref() == Some(entry.order_id.as_str())).collect();
    let stop = children.iter().find(|c| c.order_type == OrderType::Stop);
    let target = children.iter().find(|c| c.order_type == OrderType::Limit);
    check_fill_event(&fill.fill_id, has_seq)?;
    let s = |x: &str| Json::Str(x.to_string());
    let strategy_tag = entry.command_id.split(':').next().unwrap_or("").to_string();
    Ok(Json::Obj(vec![
        ("symbol".into(), Json::Str(fill.instrument.symbol()?)),
        ("side".into(), s(fill.side.value())),
        ("quantity".into(), Json::Str(fill.quantity.canon())),
        ("price".into(), Json::Str(fill.price.canon())),
        ("fee".into(), Json::Str(fill.fee.canon())),
        ("executed_at".into(), Json::Str(fill.filled_at.iso())),
        ("account_id".into(), s(journal_account)),
        (
            "asset_class".into(),
            s(match fill.instrument {
                Instrument::Option(_) => "option",
                Instrument::Future(_) => "future",
                _ => "equity",
            }),
        ),
        ("multiplier".into(), multiplier_json(&crate::oms::structures::multiplier(&fill.instrument)?)),
        ("stop_loss".into(), stop.map_or(Json::Null, |c| Json::Str(text_of(&c.stop_price)))),
        (
            "profit_target".into(),
            match target {
                // A combo's target is a net price, not this leg's.
                Some(t) if t.instrument.same(&fill.instrument) => Json::Str(text_of(&t.limit_price)),
                _ => Json::Null,
            },
        ),
        ("strategy_tag".into(), Json::Str(strategy_tag)),
        ("notes".into(), Json::Str(format!("trade-engine {}", fill.order_id))),
    ]))
}

/// The bracket entry of an order: its parent when the parent id is non-empty, else the order
/// itself. An unknown parent refuses, as Python's lookup raised KeyError.
fn entry_of<'a>(st: &'a AccountState, order: &'a Order) -> R<&'a Order> {
    match order.parent_order_id.as_deref() {
        Some(p) if !p.is_empty() => st.orders.get(p).ok_or_else(|| key_err(p)),
        _ => Ok(order),
    }
}

/// One FILL event the ledger holds: its ledger seq, the order it filled and its fill id.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FillRef {
    pub seq: i64,
    pub order_id: String,
    pub fill_id: String,
}

/// The bracket entry and the fill id of its resync cause, or None when no fill matches.
///
/// `order_id` is the entry or one of its children. The members are the entry and every order
/// whose parent is the entry; the cause is the last of `fills` (ledger-seq order) whose order is
/// a member.
pub fn bracket_resync_cause(st: &AccountState, fills: &[FillRef], order_id: &str) -> R<Option<(String, String)>> {
    let order = st.orders.get(order_id).ok_or_else(|| key_err(order_id))?;
    let entry = entry_of(st, order)?;
    let members: HashSet<&str> = std::iter::once(entry.order_id.as_str())
        .chain(
            st.orders
                .values()
                .filter(|c| c.parent_order_id.as_deref() == Some(entry.order_id.as_str()))
                .map(|c| c.order_id.as_str()),
        )
        .collect();
    Ok(fills
        .iter()
        .rev()
        .find(|f| members.contains(f.order_id.as_str()))
        .map(|f| (entry.order_id.clone(), f.fill_id.clone())))
}

/// The `(entry_id, cause_fill_id)` of every bracket to re-sync, in the state's insertion order.
///
/// An entry is a top-level order that has a fill (a missing or zero filled quantity skips it)
/// and at least one child that is not terminal. An entry with no children is skipped, as the
/// all-terminal rule would skip it.
pub fn brackets_to_resync(st: &AccountState, fills: &[FillRef]) -> Vec<(String, String)> {
    let mut out = Vec::new();
    for order in st.orders.values() {
        if order.parent_order_id.is_some() {
            continue;
        }
        if !st.filled_quantity.get(&order.order_id).is_some_and(|q| !q.is_zero()) {
            continue;
        }
        let has_live_child = st.orders.values().any(|c| {
            c.parent_order_id.as_deref() == Some(order.order_id.as_str()) && !terminal(c.state)
        });
        if !has_live_child {
            continue;
        }
        // A top-level order is its own entry, so the lookup cannot refuse.
        let cause = bracket_resync_cause(st, fills, &order.order_id)
            .expect("a top-level order is its own bracket entry");
        if let Some(pair) = cause {
            out.push(pair);
        }
    }
    out
}

/// What a recorded fill heals (`_heal_recorded_fill`): the bracket to re-sync, and whether the
/// fill is the account's latest FILL, which makes it a candidate to re-journal.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct HealPlan {
    pub resync: Option<(String, String)>,
    pub rejournal_candidate: bool,
}

/// Which recorded fill heals what after a crash (F2).
///
/// The bracket is re-synced only for the bracket's latest fill: an older cause can collide with
/// a command id already used for a different quantity, so `resync` is kept only when the cause's
/// fill is `fill_id`. The fill is a re-journal candidate only for the account's latest fill:
/// reconcile records and journals one fill at a time, so a crash can leave only the last one
/// unjournaled. `fill_seq` is the fill's ledger seq, None when its event is missing. The host
/// still checks the journal account and whether an outbox row exists.
pub fn heal_plan(
    st: &AccountState,
    fills: &[FillRef],
    fill_id: &str,
    order_id: &str,
    fill_seq: Option<i64>,
) -> R<HealPlan> {
    let resync = bracket_resync_cause(st, fills, order_id)?.filter(|(_, cause)| cause == fill_id);
    let rejournal_candidate = matches!((fills.last(), fill_seq), (Some(last), Some(seq)) if last.seq == seq);
    Ok(HealPlan { resync, rejournal_candidate })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::ledger::model::{make_equity, parse_datetime, DateTime, Side, Tif};
    use crate::money::Money;

    fn ts() -> DateTime {
        parse_datetime("2026-01-02T15:00:00+00:00").expect("valid dt").0
    }

    fn order(id: &str, parent: Option<&str>, order_type: OrderType, state: OrderState) -> Order {
        Order {
            order_id: id.to_string(),
            account_id: "A".to_string(),
            instrument: make_equity("SPY").expect("equity"),
            order_type,
            side: Side::Buy,
            quantity: Money::parse("2").expect("qty"),
            command_id: format!("{id}-cmd"),
            created_at: ts(),
            limit_price: None,
            stop_price: None,
            trail_amount: None,
            tif: Tif::Day,
            state,
            parent_order_id: parent.map(str::to_string),
            oco_group: None,
        }
    }

    /// A ledger state with these orders, in this insertion order, and these filled quantities.
    fn book(orders: Vec<Order>, filled: &[(&str, &str)]) -> AccountState {
        let mut st = AccountState::new("A");
        for o in orders {
            let id = o.order_id.clone();
            st.orders.put(&id, o);
        }
        for (id, q) in filled {
            st.filled_quantity.put(id, Money::parse(q).expect("qty"));
        }
        st
    }

    fn fill(seq: i64, order_id: &str, fill_id: &str) -> FillRef {
        FillRef { seq, order_id: order_id.to_string(), fill_id: fill_id.to_string() }
    }

    fn pair(entry: &str, fill_id: &str) -> Option<(String, String)> {
        Some((entry.to_string(), fill_id.to_string()))
    }

    /// Entry `E`, filled, with a live stop `S` and a live target `T`.
    fn bracket(extra: Vec<Order>) -> AccountState {
        let mut orders = vec![
            order("E", None, OrderType::Limit, OrderState::Filled),
            order("S", Some("E"), OrderType::Stop, OrderState::Accepted),
            order("T", Some("E"), OrderType::Limit, OrderState::Accepted),
        ];
        orders.extend(extra);
        book(orders, &[("E", "2")])
    }

    #[test]
    fn an_entry_with_no_fills_has_no_cause() {
        let st = bracket(vec![]);
        assert_eq!(bracket_resync_cause(&st, &[], "E"), Ok(None));
    }

    #[test]
    fn a_child_fill_is_the_cause_and_the_entry_is_named() {
        let st = bracket(vec![]);
        let fills = [fill(1, "E", "e1"), fill(2, "S", "s1")];
        assert_eq!(bracket_resync_cause(&st, &fills, "S"), Ok(pair("E", "s1")));
        assert_eq!(bracket_resync_cause(&st, &fills, "E"), Ok(pair("E", "s1")));
    }

    #[test]
    fn the_latest_bracket_fill_wins_and_other_orders_are_ignored() {
        let st = bracket(vec![order("X", None, OrderType::Market, OrderState::Filled)]);
        let fills = [fill(1, "E", "e1"), fill(2, "S", "s1"), fill(3, "E", "e2"), fill(4, "X", "x1")];
        assert_eq!(bracket_resync_cause(&st, &fills, "T"), Ok(pair("E", "e2")));
    }

    #[test]
    fn an_unknown_order_refuses_with_a_key_error() {
        let st = bracket(vec![]);
        assert_eq!(bracket_resync_cause(&st, &[], "nope"), Err(LErr { kind: "key", msg: "'nope'".to_string() }));
    }

    #[test]
    fn an_unknown_parent_refuses_with_a_key_error_naming_the_parent() {
        let st = book(vec![order("S", Some("GONE"), OrderType::Stop, OrderState::Accepted)], &[]);
        assert_eq!(bracket_resync_cause(&st, &[], "S"), Err(LErr { kind: "key", msg: "'GONE'".to_string() }));
    }

    #[test]
    fn brackets_to_resync_keeps_a_filled_entry_with_a_live_child() {
        let st = bracket(vec![]);
        assert_eq!(brackets_to_resync(&st, &[fill(1, "E", "e1")]), vec![("E".to_string(), "e1".to_string())]);
    }

    #[test]
    fn brackets_to_resync_skips_a_child_order() {
        // S is filled too, but it has a parent, so only E is listed.
        let st = book(
            vec![
                order("E", None, OrderType::Limit, OrderState::Filled),
                order("S", Some("E"), OrderType::Stop, OrderState::Accepted),
            ],
            &[("E", "2"), ("S", "2")],
        );
        assert_eq!(brackets_to_resync(&st, &[fill(1, "S", "s1")]), vec![("E".to_string(), "s1".to_string())]);
    }

    #[test]
    fn brackets_to_resync_skips_an_unfilled_entry() {
        let live_children = || {
            vec![
                order("E", None, OrderType::Limit, OrderState::Filled),
                order("S", Some("E"), OrderType::Stop, OrderState::Accepted),
            ]
        };
        let missing = book(live_children(), &[]);
        let zero = book(live_children(), &[("E", "0")]);
        let fills = [fill(1, "S", "s1")];
        assert!(brackets_to_resync(&missing, &fills).is_empty());
        assert!(brackets_to_resync(&zero, &fills).is_empty());
    }

    #[test]
    fn brackets_to_resync_skips_an_entry_whose_children_are_all_terminal() {
        let st = book(
            vec![
                order("E", None, OrderType::Limit, OrderState::Filled),
                order("S", Some("E"), OrderType::Stop, OrderState::Filled),
                order("T", Some("E"), OrderType::Limit, OrderState::Cancelled),
            ],
            &[("E", "2")],
        );
        assert!(brackets_to_resync(&st, &[fill(1, "S", "s1")]).is_empty());
    }

    #[test]
    fn brackets_to_resync_skips_an_entry_with_no_children() {
        let st = book(vec![order("E", None, OrderType::Limit, OrderState::Filled)], &[("E", "2")]);
        assert!(brackets_to_resync(&st, &[fill(1, "E", "e1")]).is_empty());
    }

    #[test]
    fn brackets_to_resync_keeps_the_states_insertion_order() {
        // Not sorted: the state's insertion order, not the id order, decides the result.
        let mut orders = Vec::new();
        let mut filled = Vec::new();
        let mut fills = Vec::new();
        for (i, name) in ["Z", "A", "M"].into_iter().enumerate() {
            let child = format!("{name}-s");
            orders.push(order(name, None, OrderType::Limit, OrderState::Filled));
            orders.push(order(&child, Some(name), OrderType::Stop, OrderState::Accepted));
            filled.push((name, "2"));
            fills.push(fill(i as i64 + 1, name, &format!("{name}-fill")));
        }
        let st = book(orders, &filled);
        assert_eq!(
            brackets_to_resync(&st, &fills),
            vec![
                ("Z".to_string(), "Z-fill".to_string()),
                ("A".to_string(), "A-fill".to_string()),
                ("M".to_string(), "M-fill".to_string()),
            ]
        );
    }

    #[test]
    fn heal_plan_resyncs_only_for_the_brackets_latest_fill() {
        let st = bracket(vec![]);
        let fills = [fill(1, "S", "s1"), fill(2, "E", "e2")];
        // s1 is not the bracket's latest fill: no resync, and not the account's latest either.
        assert_eq!(
            heal_plan(&st, &fills, "s1", "S", Some(1)),
            Ok(HealPlan { resync: None, rejournal_candidate: false })
        );
        // e2 is both the bracket's and the account's latest fill.
        assert_eq!(
            heal_plan(&st, &fills, "e2", "E", Some(2)),
            Ok(HealPlan { resync: pair("E", "e2"), rejournal_candidate: true })
        );
    }

    #[test]
    fn heal_plan_does_not_rejournal_a_fill_that_is_not_the_accounts_latest() {
        let st = bracket(vec![order("X", None, OrderType::Market, OrderState::Filled)]);
        let fills = [fill(1, "E", "e1"), fill(2, "X", "x1")];
        assert_eq!(
            heal_plan(&st, &fills, "e1", "E", Some(1)),
            Ok(HealPlan { resync: pair("E", "e1"), rejournal_candidate: false })
        );
    }

    #[test]
    fn heal_plan_with_no_fills_neither_resyncs_nor_rejournals() {
        let st = bracket(vec![]);
        let no_event = HealPlan { resync: None, rejournal_candidate: false };
        assert_eq!(heal_plan(&st, &[], "e1", "E", None), Ok(no_event.clone()));
        assert_eq!(heal_plan(&st, &[], "e1", "E", Some(1)), Ok(no_event));
    }
}

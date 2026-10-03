//! `reconcile.py`'s decisions (docs/RUST_PORT.md P3b-1): which venue orders are read back
//! after a bar's fills are recorded, which venue fill refuses (I5), and the journal
//! payload one fill becomes. Ledger and venue I/O stay with the host.

use crate::ledger::codec::py_repr;
use crate::ledger::fold::AccountState;
use crate::ledger::json::Json;
use crate::ledger::model::{err, Fill, Instrument, OrderState, OrderType, R};

fn terminal(s: OrderState) -> bool {
    matches!(s, OrderState::Filled | OrderState::Cancelled | OrderState::Expired | OrderState::Rejected)
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

fn text_of(v: &Option<crate::ledger::pydec::PyDec>) -> String {
    v.as_ref().map_or_else(|| "None".to_string(), |d| d.to_py_string())
}

/// The journal execution one fill becomes, as an ordered JSON object: the asset class,
/// the multiplier and the bracket's stop and target. `has_seq`: the fill's ledger event was
/// found with a seq; the check sits where Python's did, after the bracket lookups.
pub fn journal_payload(st: &AccountState, fill: &Fill, journal_account: &str, has_seq: bool) -> R<Json> {
    let key = |id: &str| -> crate::ledger::model::LErr {
        crate::ledger::model::LErr { kind: "key", msg: py_repr(id) }
    };
    let order = st.orders.get(&fill.order_id).ok_or_else(|| key(&fill.order_id))?;
    let entry = match order.parent_order_id.as_deref() {
        Some(p) if !p.is_empty() => st.orders.get(p).ok_or_else(|| key(p))?,
        _ => order,
    };
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
        ("quantity".into(), Json::Str(fill.quantity.to_py_string())),
        ("price".into(), Json::Str(fill.price.to_py_string())),
        ("fee".into(), Json::Str(fill.fee.to_py_string())),
        ("executed_at".into(), Json::Str(fill.filled_at.iso())),
        ("account_id".into(), s(journal_account)),
        (
            "asset_class".into(),
            s(if matches!(fill.instrument, Instrument::Option(_)) { "option" } else { "equity" }),
        ),
        ("multiplier".into(), Json::Int(crate::oms::structures::multiplier(&fill.instrument)?)),
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

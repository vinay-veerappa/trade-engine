//! `restore.py`'s decisions (docs/RUST_PORT.md P3b-1): which orders, fills and positions
//! of the folded ledger rebuild a venue's book, what a PENDING_UNKNOWN order becomes, and
//! when the ledger cannot rebuild it (I5). The ledger reads the decisions need are host
//! callbacks, made in the order the Python made them.

use std::collections::BTreeSet;

use crate::ledger::codec::py_repr;
use crate::ledger::fold::AccountState;
use crate::ledger::model::{err, LErr, OrderState, R};
use crate::ledger::ops::eq;
use crate::ledger::pydec::PyDec;

/// States in which the ledger says the venue holds the order.
fn venue_working(s: OrderState) -> bool {
    matches!(
        s,
        OrderState::Submitted | OrderState::Accepted | OrderState::PartiallyFilled | OrderState::PendingUnknown
    )
}

/// One ORDER_PENDING event of the account: its order, its command id, `str(reason)`.
#[derive(Debug, Clone)]
pub struct PendingEvent {
    pub order_id: String,
    pub command_id: Option<String>,
    pub reason: String,
}

/// Which request left `order_id` pending: `submit`, `cancel` or `unknown` (the last
/// ORDER_PENDING for it decides).
pub fn pending_request(events: &[PendingEvent], order_id: &str) -> &'static str {
    let Some(last) = events.iter().rev().find(|e| e.order_id == order_id) else {
        return "unknown";
    };
    let command = last.command_id.as_deref().unwrap_or("");
    if command.ends_with(":submit-pending") {
        "submit"
    } else if command.ends_with(":pending") && last.reason.starts_with("Cancel pending") {
        "cancel"
    } else {
        "unknown"
    }
}

/// What `restorable` keeps: orders as `(order_id, venue_order_id, venue state)`, in id
/// order, and fills as `(index in state.fills, venue_fill_id, venue_order_id)`.
#[derive(Debug, Clone, Default)]
pub struct Plan {
    pub orders: Vec<(String, String, &'static str)>,
    pub fills: Vec<(usize, String, String)>,
}

pub struct Callbacks<'a> {
    /// Called once the PENDING_UNKNOWN orders are classified (resolve mode), before any
    /// ledger lookup: `(resolved, unresolved)`, to append to the caller's resolution.
    pub note: &'a mut dyn FnMut(&[String], &[(String, String)]) -> R<()>,
    /// Does the ledger hold the command `{command_id}:submit`?
    pub submitted: &'a mut dyn FnMut(&str) -> R<bool>,
}

#[allow(clippy::too_many_arguments)]
pub fn restorable(
    st: &AccountState,
    account_id: &str,
    pending: &str,
    has_resolution: bool,
    pre_unresolved: &[String],
    events: &[PendingEvent],
    cb: &mut Callbacks<'_>,
) -> R<Plan> {
    if pending != "refuse" && pending != "resolve" {
        return err("value", format!("pending must be 'refuse' or 'resolve', got {}", py_repr(pending)));
    }
    if pending == "resolve" && !has_resolution {
        return err("value", "pending='resolve' needs a PendingResolution to report into");
    }
    let mut wanted: BTreeSet<String> = BTreeSet::new();
    let mut restored_as: Vec<(String, &'static str)> = Vec::new();
    let mut resolved: Vec<String> = Vec::new();
    let mut unresolved: Vec<(String, String)> = Vec::new();
    for order in st.orders.values() {
        if !venue_working(order.state) {
            continue;
        }
        if order.state == OrderState::PendingUnknown {
            if pending == "refuse" {
                return err(
                    "restore",
                    format!(
                        "Order '{}' for '{account_id}' is PENDING_UNKNOWN; the simulated venue's answer is gone, reconcile it before replay (I5)",
                        order.order_id
                    ),
                );
            }
            match pending_request(events, &order.order_id) {
                "submit" => restored_as.push((order.order_id.clone(), OrderState::Accepted.value())),
                "cancel" => restored_as.push((order.order_id.clone(), OrderState::Cancelled.value())),
                _ => {
                    unresolved.push((
                        order.order_id.clone(),
                        format!(
                            "'{0}' is PENDING_UNKNOWN after a request that cannot be carried out on a rebuilt venue; it stays unresolved (I5)",
                            order.order_id
                        ),
                    ));
                    continue;
                }
            }
            resolved.push(order.order_id.clone());
        }
        let root = match order.parent_order_id.as_deref() {
            Some(p) if !p.is_empty() => p.to_string(),
            _ => order.order_id.clone(),
        };
        for c in st.orders.values() {
            if c.parent_order_id.as_deref() == Some(root.as_str()) && c.state != OrderState::New {
                wanted.insert(c.order_id.clone());
            }
        }
        wanted.insert(root);
    }
    if pending == "resolve" {
        (cb.note)(&resolved, &unresolved)?;
    }
    let skipped = |id: &str| {
        has_resolution && (pre_unresolved.iter().any(|u| u == id) || unresolved.iter().any(|(u, _)| u == id))
    };
    let mut plan = Plan::default();
    for id in &wanted {
        let order = st.orders.get(id).ok_or_else(|| LErr { kind: "key", msg: py_repr(id) })?;
        if skipped(id) {
            continue;
        }
        if !(cb.submitted)(&format!("{}:submit", order.command_id))? {
            return err(
                "restore",
                format!(
                    "Order '{id}' for '{account_id}' has no submission event; cannot restore when it reached the venue (I5)"
                ),
            );
        }
        let venue_state = restored_as.iter().find(|(r, _)| r == id).map_or(order.state.value(), |(_, s)| *s);
        let venue_id = st.venue_order_ids.get(id).cloned().unwrap_or_else(|| id.clone());
        plan.orders.push((id.clone(), venue_id, venue_state));
    }
    let kept: BTreeSet<&str> = plan.orders.iter().map(|(id, _, _)| id.as_str()).collect();
    for (i, f) in st.fills.iter().enumerate() {
        if !kept.contains(f.order_id.as_str()) {
            continue;
        }
        let fill_id = match f.venue_execution_id.as_deref() {
            Some(e) if !e.is_empty() => e.to_string(),
            _ => f.fill_id.clone(),
        };
        let venue_order = st.venue_order_ids.get(&f.order_id).cloned().unwrap_or_else(|| f.order_id.clone());
        plan.fills.push((i, fill_id, venue_order));
    }
    Ok(plan)
}

/// `restorable_positions`: each non-flat position, in state order, with the index of the
/// latest fill of its instrument (the first on a tie), `None` when it has none.
pub fn positions_plan(st: &AccountState) -> R<Vec<(usize, Option<usize>)>> {
    let mut out = Vec::new();
    for (i, (instrument, position)) in st.positions.iter().enumerate() {
        if eq(&position.quantity, &PyDec::zero())? {
            continue;
        }
        let mut latest: Option<usize> = None;
        for (j, f) in st.fills.iter().enumerate() {
            if !f.instrument.same(instrument) {
                continue;
            }
            match latest {
                Some(k) if st.fills[k].filled_at.instant() >= f.filled_at.instant() => {}
                _ => latest = Some(j),
            }
        }
        out.push((i, latest));
    }
    Ok(out)
}

//! The pure fold over ledger events (was `ledger/state.py`; I2). Every handler refuses a
//! contradiction the way the Python did, in the same check order, with the same message:
//! the parity test compares refusals by kind AND message. Mutation is in place, which is
//! safe because a refusal aborts the whole fold (the caller never sees a half-applied
//! state).
//!
//! Python behaviours that look like defects are preserved, not fixed (see the P2a report):
//! `OrdersCreated` stores orders as `NEW`, so a following `OrderSubmitted` for the same
//! order always refuses; an `OrderUpdated` illegal transition is a `LedgerFoldError` but
//! the same transition in an order-state event raises raw; the first-inserted key of an
//! equal-key dict write is the one kept.

use std::collections::BTreeSet;

use crate::options::lifecycle as lc;

use super::codec::py_repr;
use super::mirror::{self, MirrorState};
use super::model::*;
use super::ops::*;
use super::pydec::PyDec;

fn fe<T>(msg: impl Into<String>) -> R<T> {
    err("fold", msg)
}

// --- state -------------------------------------------------------------------------------

#[derive(Debug, Clone)]
pub struct Position {
    pub account_id: String,
    pub instrument: Instrument,
    pub quantity: PyDec,
    pub avg_cost: PyDec,
    pub realized_pnl: PyDec,
    pub open_lots: Vec<Lot>,
}

/// `Position.__post_init__`.
pub fn make_position(
    account_id: &str,
    instrument: Instrument,
    quantity: PyDec,
    avg_cost: PyDec,
    realized_pnl: PyDec,
    open_lots: Vec<Lot>,
) -> R<Position> {
    if account_id.is_empty() {
        return err("value", "account_id must be non-empty");
    }
    if !open_lots.is_empty() {
        let signed = signed_total(&open_lots)?;
        if ne(&signed, &quantity)? {
            return err(
                "value",
                format!("Position quantity {} disagrees with open lots total {} (I2)", s(&quantity), s(&signed)),
            );
        }
    }
    Ok(Position { account_id: account_id.to_string(), instrument, quantity, avg_cost, realized_pnl, open_lots })
}

/// `sum((q if BUY else -q for each lot), ZERO)`.
fn signed_total(lots: &[Lot]) -> R<PyDec> {
    let mut total = zero();
    for lot in lots {
        let q = if lot.side == Side::Buy { lot.quantity.clone() } else { neg(&lot.quantity)? };
        total = add(&total, &q)?;
    }
    Ok(total)
}

/// `sum((cost * qty for each lot), ZERO)`.
fn cost_total(lots: &[Lot]) -> R<PyDec> {
    let mut total = zero();
    for lot in lots {
        total = add(&total, &mul(&lot.cost_basis, &lot.quantity)?)?;
    }
    Ok(total)
}

#[derive(Debug, Clone)]
pub struct AccountState {
    pub account_id: String,
    pub cash: PyDec,
    pub positions: OMap<Instrument, Position>,
    pub orders: OMap<String, Order>,
    pub filled_quantity: OMap<String, PyDec>,
    pub leg_filled: OMap<(String, i128), PyDec>,
    pub venue_order_ids: OMap<String, String>,
    pub emulated_orders: OMap<String, EmulatedOrderState>,
    pub fills: Vec<Fill>,
    pub fill_ids: BTreeSet<String>,
    pub marks: OMap<Instrument, PyDec>,
    pub realized_pnl: PyDec,
    pub signals_seen: i128,
    pub verdicts: i128,
    pub refusals: i128,
    pub last_reconcile: Option<VenueReconcile>,
    pub venue_halted: bool,
    pub halted_venues: BTreeSet<String>,
    pub risk_controls: OMap<String, bool>,
    pub mirror: MirrorState,
    pub last_seq: i128,
}

impl AccountState {
    pub fn new(account_id: &str) -> AccountState {
        AccountState {
            account_id: account_id.to_string(),
            cash: zero(),
            positions: OMap::new(),
            orders: OMap::new(),
            filled_quantity: OMap::new(),
            leg_filled: OMap::new(),
            venue_order_ids: OMap::new(),
            emulated_orders: OMap::new(),
            fills: Vec::new(),
            fill_ids: BTreeSet::new(),
            marks: OMap::new(),
            realized_pnl: zero(),
            signals_seen: 0,
            verdicts: 0,
            refusals: 0,
            last_reconcile: None,
            venue_halted: false,
            halted_venues: BTreeSet::new(),
            risk_controls: OMap::new(),
            mirror: MirrorState::default(),
            last_seq: 0,
        }
    }
}

pub fn leg_hk(order_id: &str, index: i128) -> String {
    format!("{}:{}#{}", order_id.len(), order_id, index)
}

// --- order state machine -------------------------------------------------------------------

fn allowed(from: OrderState, to: OrderState) -> bool {
    use OrderState::*;
    match from {
        New => matches!(to, Submitted | Cancelled | Rejected),
        Submitted => matches!(to, Accepted | PartiallyFilled | Filled | Rejected | Cancelled | PendingUnknown),
        Accepted => matches!(to, PartiallyFilled | Filled | Cancelled | Expired | Rejected | PendingUnknown),
        PartiallyFilled => matches!(to, PartiallyFilled | Filled | Cancelled | Expired | PendingUnknown),
        PendingUnknown => matches!(to, Submitted | Accepted | PartiallyFilled | Filled | Cancelled | Expired | Rejected),
        Filled | Cancelled | Expired | Rejected => false,
    }
}

/// `validate_order_transition`: the error is `IllegalOrderStateTransitionError`.
pub fn validate_transition(from: OrderState, to: OrderState) -> R<()> {
    if from == to || allowed(from, to) {
        return Ok(());
    }
    err("illegal_transition", format!("Illegal order state transition from {} to {}", from.value(), to.value()))
}

fn transition_to(order: &Order, to: OrderState) -> R<Order> {
    validate_transition(order.state, to)?;
    let mut o = order.clone();
    o.state = to;
    Ok(o)
}

// --- lots and trades -------------------------------------------------------------------------

fn lot_with(lot: &Lot, quantity: PyDec) -> R<Lot> {
    make_lot(lot.lot_id.clone(), quantity, lot.cost_basis.clone(), lot.acquired_at.clone(), lot.side)
}

fn consume_lots(lots: &[Lot], quantity: &PyDec, exit_price: &PyDec, multiplier: i128) -> R<(Vec<Lot>, PyDec)> {
    let mut remaining = quantity.clone();
    let mut realized = zero();
    let mut kept: Vec<Lot> = Vec::new();
    for lot in lots {
        if le(&remaining, &zero())? {
            kept.push(lot.clone());
            continue;
        }
        let closed;
        if le(&lot.quantity, &remaining)? {
            closed = lot.quantity.clone();
            remaining = sub(&remaining, &closed)?;
        } else {
            closed = remaining.clone();
            kept.push(lot_with(lot, sub(&lot.quantity, &remaining)?)?);
            remaining = zero();
        }
        let per = if lot.side == Side::Buy { sub(exit_price, &lot.cost_basis)? } else { sub(&lot.cost_basis, exit_price)? };
        realized = add(&realized, &mul_i(&mul(&per, &closed)?, multiplier)?)?;
    }
    if ne(&remaining, &zero())? {
        return fe(format!("Open lots ran short by {}; ledger is inconsistent (I2)", s(&remaining)));
    }
    Ok((kept, realized))
}

fn take_lots(lots: &[Lot], quantity: &PyDec) -> R<(Vec<Lot>, Vec<Lot>)> {
    let mut remaining = quantity.clone();
    let mut kept = Vec::new();
    let mut taken = Vec::new();
    for lot in lots {
        if le(&remaining, &zero())? {
            kept.push(lot.clone());
            continue;
        }
        let part = if lt(&remaining, &lot.quantity)? { remaining.clone() } else { lot.quantity.clone() };
        taken.push(lot_with(lot, part.clone())?);
        if lt(&part, &lot.quantity)? {
            kept.push(lot_with(lot, sub(&lot.quantity, &part)?)?);
        }
        remaining = sub(&remaining, &part)?;
    }
    if ne(&remaining, &zero())? {
        return fe(format!("Open lots ran short by {}; ledger is inconsistent (I2)", s(&remaining)));
    }
    Ok((kept, taken))
}

fn remaining_position(position: &Position, lots: Vec<Lot>, realized: PyDec) -> R<Position> {
    let quantity = signed_total(&lots)?;
    let avg = if ne(&quantity, &zero())? { div(&cost_total(&lots)?, &abs(&quantity)?)? } else { zero() };
    make_position(&position.account_id, position.instrument.clone(), quantity, avg, realized, lots)
}

#[allow(clippy::too_many_arguments)]
fn apply_trade(
    account_id: &str,
    position: Option<&Position>,
    instrument: &Instrument,
    side: Side,
    quantity: &PyDec,
    price: &PyDec,
    at: &DateTime,
    lot_id: String,
    multiplier: i128,
) -> R<Position> {
    let signed_delta = if side == Side::Buy { quantity.clone() } else { neg(quantity)? };

    let flat = match position {
        None => true,
        Some(p) => eq(&p.quantity, &zero())?,
    };
    if flat {
        let carried = position.map_or_else(zero, |p| p.realized_pnl.clone());
        let lot = make_lot(lot_id, quantity.clone(), price.clone(), at.clone(), side)?;
        return make_position(account_id, instrument.clone(), signed_delta, price.clone(), carried, vec![lot]);
    }
    let position = position.expect("not flat");
    let old_qty = position.quantity.clone();
    let avg = position.avg_cost.clone();
    let mut realized = position.realized_pnl.clone();
    let increasing = gt(&old_qty, &zero())? == gt(&signed_delta, &zero())?;

    let new_qty;
    let new_avg;
    let lots: Vec<Lot>;
    if increasing {
        new_qty = add(&old_qty, &signed_delta)?;
        let new_lot = make_lot(lot_id, quantity.clone(), price.clone(), at.clone(), side)?;
        let weighted = add(&mul(&avg, &abs(&old_qty)?)?, &mul(price, quantity)?)?;
        new_avg = div(&weighted, &abs(&new_qty)?)?;
        let mut l = position.open_lots.clone();
        l.push(new_lot);
        lots = l;
    } else {
        let a = abs(&signed_delta)?;
        let b = abs(&old_qty)?;
        let closing = if lt(&b, &a)? { b.clone() } else { a.clone() };
        let (kept, close_pnl) = consume_lots(&position.open_lots, &closing, price, multiplier)?;
        realized = add(&realized, &close_pnl)?;
        new_qty = add(&old_qty, &signed_delta)?;
        if gt(&abs(&signed_delta)?, &abs(&old_qty)?)? {
            let flip_qty = sub(&abs(&signed_delta)?, &abs(&old_qty)?)?;
            let new_lot = make_lot(lot_id, flip_qty, price.clone(), at.clone(), side)?;
            lots = vec![new_lot];
            new_avg = price.clone();
        } else if eq(&new_qty, &zero())? {
            lots = kept;
            new_avg = zero();
        } else {
            new_avg = div(&cost_total(&kept)?, &abs(&new_qty)?)?;
            lots = kept;
        }
    }
    make_position(account_id, instrument.clone(), new_qty, new_avg, realized, lots)
}

/// `apply_trade`, for `state.apply_fill` (P2b: the public one-fill step).
#[allow(clippy::too_many_arguments)]
pub fn apply_trade_pub(
    account_id: &str,
    position: Option<&Position>,
    instrument: &Instrument,
    side: Side,
    quantity: &PyDec,
    price: &PyDec,
    at: &DateTime,
    lot_id: String,
    multiplier: i128,
) -> R<Position> {
    apply_trade(account_id, position, instrument, side, quantity, price, at, lot_id, multiplier)
}

// --- helpers -----------------------------------------------------------------------------------

fn multiplier(i: &Instrument) -> R<i128> {
    match i {
        Instrument::Equity(_) => Ok(1),
        Instrument::Option(c) => Ok(c.multiplier),
        Instrument::Combo(legs) => {
            let mut mults: Vec<i128> = Vec::new();
            for l in legs {
                let m = multiplier(&l.contract)?;
                if !mults.contains(&m) {
                    mults.push(m);
                }
            }
            mults.sort();
            if mults.len() != 1 {
                let listed: Vec<String> = mults.iter().map(|m| m.to_string()).collect();
                return fe(format!(
                    "Cannot value a mixed-multiplier combo as one instrument; value it per leg: Combo has mixed leg multipliers [{}]; value it per leg (I6)",
                    listed.join(", ")
                ));
            }
            multiplier(&legs[0].contract)
        }
    }
}

fn require_order(st: &AccountState, order_id: &str, kind: EventKind) -> R<Order> {
    match st.orders.get(order_id) {
        Some(o) => Ok(o.clone()),
        None => fe(format!(
            "{} references unknown order '{}' in account '{}'; refusing to invent one (I5)",
            kind.value(),
            order_id,
            st.account_id
        )),
    }
}

fn check_finite(d: &PyDec, name: &str) -> R<()> {
    if !d.is_finite() {
        return fe(format!("{name} must be a finite Decimal, got {} (I5)", s(d)));
    }
    Ok(())
}

fn combo_leg(order: &Order, fill: &Fill) -> R<(i128, Option<ComboLeg>)> {
    let legs = match &order.instrument {
        Instrument::Combo(legs) => legs,
        _ => return Ok((-1, None)),
    };
    let bad = || -> R<(i128, Option<ComboLeg>)> {
        err(
            "fill_mismatch",
            format!(
                "Fill {} on combo order '{}' names leg {}; a combo fill must name one of its {} legs by index (I5)",
                fill.fill_id,
                fill.order_id,
                fill.leg_id.as_deref().map_or("None".to_string(), py_repr),
                legs.len()
            ),
        )
    };
    let leg_id = match &fill.leg_id {
        Some(l) if py_isdigit(l) => l,
        _ => return bad(),
    };
    // a digit string too long for i128 is certainly past the last leg
    let index: i128 = match leg_id.trim_start_matches('0').parse::<i128>() {
        Ok(n) => n,
        Err(_) if leg_id.chars().all(|c| c == '0') => 0,
        Err(_) => return bad(),
    };
    if index >= legs.len() as i128 {
        return bad();
    }
    let leg = legs[index as usize].clone();
    if !fill.instrument.same(&leg.contract) {
        return err(
            "fill_mismatch",
            format!(
                "Fill {} on {} was filed against leg {} of combo order '{}', which is {} (I5)",
                fill.fill_id,
                fill.instrument.symbol()?,
                index,
                fill.order_id,
                leg.contract.symbol()?
            ),
        );
    }
    if fill.side != leg.side {
        return err(
            "fill_mismatch",
            format!(
                "Fill {} is a {} but leg {} of combo order '{}' is a {}; refusing the wrong direction (I5)",
                fill.fill_id,
                fill.side.value(),
                index,
                fill.order_id,
                leg.side.value()
            ),
        );
    }
    Ok((index, Some(leg)))
}

// --- handlers ----------------------------------------------------------------------------------

fn on_fill(st: &mut AccountState, fill: &Fill) -> R<()> {
    let order = match st.orders.get(&fill.order_id) {
        Some(o) => o.clone(),
        None => return fe(format!("Fill {} references unknown order '{}' (I5)", fill.fill_id, fill.order_id)),
    };
    let (leg_index, leg) = combo_leg(&order, fill)?;
    if leg.is_none() && !fill.instrument.same(&order.instrument) {
        return err(
            "fill_mismatch",
            format!(
                "Fill {} on instrument {} was filed against order '{}' for {}; the log contradicts the order (I5)",
                fill.fill_id,
                fill.instrument.symbol()?,
                fill.order_id,
                order.instrument.symbol()?
            ),
        );
    }
    if leg.is_none() && fill.side != order.side {
        return err(
            "fill_mismatch",
            format!(
                "Fill {} is a {} but its order '{}' is a {}; refusing to apply the wrong direction (I5)",
                fill.fill_id,
                fill.side.value(),
                fill.order_id,
                order.side.value()
            ),
        );
    }
    if st.fill_ids.contains(&fill.fill_id) {
        return err(
            "duplicate_fill",
            format!(
                "Fill '{}' is already in the ledger for account '{}'; a replayed venue fill is a no-op, not a second position (I3)",
                fill.fill_id, st.account_id
            ),
        );
    }
    check_finite(&fill.quantity, &format!("Fill {} quantity", fill.fill_id))?;
    check_finite(&fill.price, &format!("Fill {} price", fill.fill_id))?;
    check_finite(&fill.fee, &format!("Fill {} fee", fill.fill_id))?;

    let mult = multiplier(&fill.instrument)?;
    let gross = mul_i(&mul(&fill.quantity, &fill.price)?, mult)?;
    let signed_gross = if fill.side == Side::Sell { gross } else { neg(&gross)? };
    let cash_delta = sub(&signed_gross, &fill.fee)?;

    let hk = fill.instrument.hk();
    let position = st.positions.get(&hk).cloned();
    let prior_realized = position.as_ref().map_or_else(zero, |p| p.realized_pnl.clone());
    let mut updated = apply_trade(
        &st.account_id,
        position.as_ref(),
        &fill.instrument,
        fill.side,
        &fill.quantity,
        &fill.price,
        &fill.filled_at,
        fill.fill_id.clone(),
        mult,
    )?;
    updated.realized_pnl = sub(&updated.realized_pnl, &fill.fee)?;
    let updated_realized = updated.realized_pnl.clone();
    st.positions.insert(hk, fill.instrument.clone(), updated);

    match &leg {
        None => {
            let have = st.filled_quantity.get(&fill.order_id).cloned().unwrap_or_else(zero);
            st.filled_quantity.put(&fill.order_id, add(&have, &fill.quantity)?);
        }
        Some(leg) => {
            let key = leg_hk(&fill.order_id, leg_index);
            let have = st.leg_filled.get(&key).cloned().unwrap_or_else(zero);
            let now = add(&have, &fill.quantity)?;
            st.leg_filled.insert(key, (fill.order_id.clone(), leg_index), now.clone());
            let ordered = mul_i(&order.quantity, leg.ratio)?;
            if gt(&now, &ordered)? {
                return fe(format!(
                    "Fill {} would take leg {} of combo order '{}' to {} contracts against {} ordered; refusing the over-fill (I5)",
                    fill.fill_id,
                    leg_index,
                    fill.order_id,
                    s(&now),
                    s(&ordered)
                ));
            }
            let legs = match &order.instrument {
                Instrument::Combo(legs) => legs,
                _ => unreachable!("a leg exists only on a combo"),
            };
            let mut best: Option<PyDec> = None;
            for (index, combo_leg) in legs.iter().enumerate() {
                let filled_leg = st.leg_filled.get(&leg_hk(&fill.order_id, index as i128)).cloned().unwrap_or_else(zero);
                let units = div_i(&filled_leg, combo_leg.ratio)?;
                best = Some(match best {
                    None => units,
                    Some(b) => {
                        if lt(&units, &b)? {
                            units
                        } else {
                            b
                        }
                    }
                });
            }
            let best = match best {
                Some(b) => b,
                None => return err("value", "min() iterable argument is empty"),
            };
            st.filled_quantity.put(&fill.order_id, best);
        }
    }

    let total = st.filled_quantity.get(&fill.order_id).cloned().expect("just set");
    if gt(&total, &order.quantity)? {
        return fe(format!(
            "Fill {} would take order '{}' to {} filled against an order size of {}; refusing the over-fill rather than inventing an oversized order (I5)",
            fill.fill_id,
            fill.order_id,
            s(&total),
            s(&order.quantity)
        ));
    }
    if matches!(order.state, OrderState::Cancelled | OrderState::Expired | OrderState::Rejected) {
        return fe(format!(
            "Fill {} arrived for order '{}' already in terminal state {}; the log contradicts the order state (I5)",
            fill.fill_id,
            fill.order_id,
            order.state.value()
        ));
    }
    if order.state != OrderState::Filled {
        let next = if ge(&total, &order.quantity)? { OrderState::Filled } else { OrderState::PartiallyFilled };
        if next != order.state {
            let moved = transition_to(&order, next)?;
            st.orders.put(&fill.order_id, moved);
        }
    }

    st.cash = add(&st.cash, &cash_delta)?;
    st.realized_pnl = add(&st.realized_pnl, &sub(&updated_realized, &prior_realized)?)?;
    st.fills.push(fill.clone());
    st.fill_ids.insert(fill.fill_id.clone());
    Ok(())
}

fn on_order_submitted(st: &mut AccountState, o: &Order) -> R<()> {
    let mut order = o.clone();
    if order.state == OrderState::New {
        order = transition_to(&order, OrderState::Submitted)?;
    }
    if let Some(existing) = st.orders.get(&order.order_id) {
        if existing.command_id != order.command_id {
            return fe(format!(
                "Order '{}' was already submitted under command '{}'; refusing to overwrite it with '{}' (I3)",
                order.order_id, existing.command_id, order.command_id
            ));
        }
        if existing.state != OrderState::Submitted {
            return fe(format!(
                "Order '{}' is already {}; refusing a second OrderSubmitted that would reset it (I2)",
                order.order_id,
                existing.state.value()
            ));
        }
    }
    let id = order.order_id.clone();
    st.orders.put(&id, order);
    Ok(())
}

fn on_orders_created(st: &mut AccountState, created: &OrdersCreated) -> R<()> {
    for order in &created.orders {
        if let Some(existing) = st.orders.get(&order.order_id) {
            if !existing.same(order) {
                return fe(format!(
                    "Order '{}' already exists with a different payload; refusing to overwrite it (I3)",
                    order.order_id
                ));
            }
        }
        st.orders.put(&order.order_id, order.clone());
    }
    Ok(())
}

fn on_order_updated(st: &mut AccountState, update: &OrderUpdated, kind: EventKind) -> R<()> {
    let current = &update.order;
    let previous = require_order(st, &current.order_id, kind)?;
    let identity_changed = previous.account_id != current.account_id
        || !previous.instrument.same(&current.instrument)
        || previous.side != current.side
        || previous.command_id != current.command_id
        || previous.parent_order_id != current.parent_order_id
        || previous.oco_group != current.oco_group;
    if identity_changed {
        return fe(format!("OrderUpdated changed immutable identity fields for '{}' (I5)", current.order_id));
    }
    if current.state != previous.state {
        if let Err(e) = validate_transition(previous.state, current.state) {
            return fe(e.msg);
        }
    }
    let filled = st.filled_quantity.get(&current.order_id).cloned().unwrap_or_else(zero);
    if lt(&current.quantity, &filled)? {
        return fe(format!(
            "OrderUpdated quantity {} is below already-filled quantity {} for '{}'",
            s(&current.quantity),
            s(&filled),
            current.order_id
        ));
    }
    st.orders.put(&current.order_id, current.clone());
    if let Some(v) = &update.venue_order_id {
        st.venue_order_ids.put(&current.order_id, v.clone());
    }
    let stop_kind = matches!(current.order_type, OrderType::Stop | OrderType::StopLimit);
    if let Some(emulation) = st.emulated_orders.get(&current.order_id).cloned() {
        if stop_kind && !opt_dec_eq(&current.stop_price, &previous.stop_price) {
            if emulation.triggered || current.stop_price.is_none() {
                return fe(format!(
                    "Cannot change stop price for triggered emulated order '{}' (I5)",
                    current.order_id
                ));
            }
            let opt = |d: &Option<PyDec>| d.clone().map_or(Val::None, Val::Dec);
            let rebuilt = build_emulated(vec![
                ("order_id".into(), Val::Str(current.order_id.clone())),
                ("observed_price".into(), opt(&emulation.observed_price)),
                ("extreme".into(), Val::None),
                ("stop_price".into(), opt(&current.stop_price)),
                ("triggered".into(), Val::Bool(false)),
                ("reason".into(), Val::Str(update.reason.clone())),
            ])?;
            st.emulated_orders.put(&current.order_id, rebuilt);
        }
    }
    Ok(())
}

fn on_order_state(st: &mut AccountState, change: &OrderStateChange, kind: EventKind, target: OrderState) -> R<()> {
    let order = require_order(st, &change.order_id, kind)?;
    let moved = transition_to(&order, target)?;
    st.orders.put(&change.order_id, moved);
    if let Some(v) = &change.venue_order_id {
        st.venue_order_ids.put(&change.order_id, v.clone());
    }
    Ok(())
}

fn exercise_threshold() -> PyDec {
    PyDec::parse(lc::exercise_threshold()).expect("constant")
}

/// A `ValueError` or `UnresolvableInstrumentError` becomes a fold refusal; the decimal
/// signals stay raw.
fn lifecycle_wrap(label: &str, e: OptionErrorPair) -> LErr {
    if matches!(e.0, "value" | "unresolvable") {
        LErr { kind: "fold", msg: format!("{label}: {}", e.1) }
    } else {
        LErr { kind: e.0, msg: e.1 }
    }
}

type OptionErrorPair = (&'static str, String);

fn pair(e: crate::options::OptionError) -> OptionErrorPair {
    (e.kind(), e.message().to_string())
}

fn on_lifecycle(st: &mut AccountState, notice: &OptionLifecycle, kind: EventKind) -> R<()> {
    let contract = &notice.contract;
    let label = format!(
        "{} of {} in '{}'",
        kind.value(),
        crate::options::pyrules::strip(&contract.occ()?),
        st.account_id
    );
    let hk = Instrument::Option(contract.clone()).hk();
    let position = match st.positions.get(&hk) {
        Some(p) if !eq(&p.quantity, &zero())? => p.clone(),
        _ => return fe(format!("{label}: no open position to settle (I5)")),
    };
    let held = if gt(&position.quantity, &zero())? { Side::Buy } else { Side::Sell };
    if held != notice.held {
        return fe(format!(
            "{label}: the event says the contracts are held {} but the book holds them {} (I5)",
            notice.held.value(),
            held.value()
        ));
    }
    let held_abs = abs(&position.quantity)?;
    if gt(&notice.quantity, &held_abs)? {
        return fe(format!(
            "{label}: {} contracts settled but {} are held (I5)",
            s(&notice.quantity),
            s(&held_abs)
        ));
    }
    let wire = contract.wire();
    let price_text = s(&notice.underlying_price);
    let value_text = lc::intrinsic(&wire, &price_text).map_err(|e| lifecycle_wrap(&label, pair(e)))?;
    let value = PyDec::parse(&value_text).ok_or_else(|| LErr { kind: "value", msg: value_text.clone() })?;
    let cash_settled = lc::is_cash_settled(&wire).map_err(|e| lifecycle_wrap(&label, pair(e)))?;
    let american = lc::can_exercise_early(&wire).map_err(|e| lifecycle_wrap(&label, pair(e)))?;

    if kind == EventKind::Expiry {
        if notice.early {
            return fe(format!("{label}: a contract expires only at its expiry (I9)"));
        }
        if ge(&value, &exercise_threshold())? {
            return fe(format!(
                "{label}: {} in the money at {}; it is exercised or assigned, not expired worthless (I9)",
                s(&value),
                s(&notice.underlying_price)
            ));
        }
    } else {
        let wanted = if kind == EventKind::Exercise { Side::Buy } else { Side::Sell };
        if held != wanted {
            return fe(format!(
                "{label}: {} applies to {} contracts; these are held {} (I5)",
                kind.value(),
                if wanted == Side::Buy { "long" } else { "short" },
                held.value()
            ));
        }
        if lt(&value, &exercise_threshold())? {
            return fe(format!(
                "{label}: not in the money at {}; nobody exercises it (I9)",
                s(&notice.underlying_price)
            ));
        }
        if notice.early && !american {
            return fe(format!("{label}: a European contract cannot be assigned early (I5)"));
        }
    }

    let mult = contract.multiplier;
    let mut cash = st.cash.clone();
    let mut realized = zero();
    if kind == EventKind::Expiry || cash_settled {
        let exit_price = if kind == EventKind::Expiry { zero() } else { value.clone() };
        let (kept, closed_pnl) = consume_lots(&position.open_lots, &notice.quantity, &exit_price, mult)?;
        realized = add(&realized, &closed_pnl)?;
        let rem = remaining_position(&position, kept, add(&position.realized_pnl, &closed_pnl)?)?;
        st.positions.insert(hk, Instrument::Option(contract.clone()), rem);
        let amount = mul_i(&mul(&exit_price, &notice.quantity)?, mult)?;
        cash = if held == Side::Buy { add(&cash, &amount)? } else { sub(&cash, &amount)? };
    } else {
        let (kept, taken) = take_lots(&position.open_lots, &notice.quantity)?;
        let rem = remaining_position(&position, kept, position.realized_pnl.clone())?;
        st.positions.insert(hk, Instrument::Option(contract.clone()), rem);
        // only a ValueError is wrapped here; an unresolvable refusal escapes raw
        let only_value = |e: LErr| -> LErr {
            if e.kind == "value" {
                LErr { kind: "fold", msg: format!("{label}: {}", e.msg) }
            } else {
                e
            }
        };
        let symbol = lc::deliverable(&wire).map_err(|e| only_value(oerr(e)))?;
        let shares_instrument = make_equity(&symbol).map_err(only_value)?;
        let shares_hk = shares_instrument.hk();
        for lot in &taken {
            let (buys, price_text) =
                lc::delivery(&wire, held == Side::Buy, &s(&lot.cost_basis)).map_err(|e| only_value(oerr(e)))?;
            let side = if buys { Side::Buy } else { Side::Sell };
            let price = PyDec::parse(&price_text).ok_or_else(|| LErr { kind: "value", msg: price_text.clone() })?;
            let shares = mul_i(&lot.quantity, mult)?;
            let shares_position = st.positions.get(&shares_hk).cloned();
            let prior = shares_position.as_ref().map_or_else(zero, |p| p.realized_pnl.clone());
            let updated = apply_trade(
                &st.account_id,
                shares_position.as_ref(),
                &shares_instrument,
                side,
                &shares,
                &price,
                &notice.as_of,
                format!("{}:{}:{}", kind.value(), notice.as_of.iso(), lot.lot_id),
                multiplier(&shares_instrument)?,
            )?;
            realized = add(&realized, &sub(&updated.realized_pnl, &prior)?)?;
            st.positions.insert(shares_hk.clone(), shares_instrument.clone(), updated);
            let paid = mul(&contract.strike, &shares)?;
            cash = if side == Side::Buy { sub(&cash, &paid)? } else { add(&cash, &paid)? };
        }
    }
    st.cash = cash;
    st.realized_pnl = add(&st.realized_pnl, &realized)?;
    Ok(())
}

fn mirror_step(st: &mut AccountState, kind: EventKind, step: impl FnOnce(&mut MirrorState) -> R<()>) -> R<()> {
    match step(&mut st.mirror) {
        Ok(()) => Ok(()),
        Err(e) if e.kind == mirror::MIRROR_FOLD => fe(format!("{}: {}", kind.value(), e.msg)),
        Err(e) => Err(e),
    }
}

/// `_dispatch` then `last_seq`: the one step `fold` and every cache share.
pub fn apply_event(st: &mut AccountState, event: &Event) -> R<()> {
    let kind = event.kind;
    match &event.payload {
        Obj::Signal(_) if kind == EventKind::SignalSeen => st.signals_seen += 1,
        Obj::RiskVerdict(v) => {
            st.verdicts += 1;
            if !v.accepted {
                st.refusals += 1;
            }
        }
        Obj::OrdersCreated(c) => on_orders_created(st, c)?,
        Obj::RiskControl(c) => st.risk_controls.put(&c.control_id, c.enabled),
        Obj::Order(o) => on_order_submitted(st, o)?,
        Obj::OrderUpdated(u) => on_order_updated(st, u, kind)?,
        Obj::StateChange(c) => match kind {
            EventKind::OrderPending => on_order_state(st, c, kind, OrderState::PendingUnknown)?,
            EventKind::OrderAccepted => on_order_state(st, c, kind, OrderState::Accepted)?,
            EventKind::OrderRejected => on_order_state(st, c, kind, OrderState::Rejected)?,
            EventKind::OrderCancelled => on_order_state(st, c, kind, OrderState::Cancelled)?,
            EventKind::OrderExpired => on_order_state(st, c, kind, OrderState::Expired)?,
            _ => {
                require_order(st, &c.order_id, kind)?;
                st.refusals += 1;
            }
        },
        Obj::Emulated(e) => {
            require_order(st, &e.order_id, kind)?;
            st.emulated_orders.put(&e.order_id, e.clone());
        }
        Obj::Fill(f) => on_fill(st, f)?,
        Obj::CashFlow(c) => st.cash = add(&st.cash, &c.amount)?,
        Obj::Mark(m) => st.marks.insert(m.instrument.hk(), m.instrument.clone(), m.price.clone()),
        Obj::Reconcile(n) => {
            st.last_reconcile = Some(n.clone());
            st.venue_halted = st.venue_halted || !n.reconciled;
            if !n.reconciled {
                st.halted_venues.insert(n.venue.clone());
            }
        }
        Obj::HaltCleared(c) => {
            if !st.halted_venues.contains(&c.venue) {
                return fe(format!("VenueHaltCleared for {}, which '{}' has not halted (I5)", c.venue, st.account_id));
            }
            st.halted_venues.remove(&c.venue);
            st.venue_halted = !st.halted_venues.is_empty();
        }
        Obj::EodRun(_) => {}
        Obj::Lifecycle(n) => on_lifecycle(st, n, kind)?,
        Obj::MQueued(q) => mirror_step(st, kind, |m| mirror::on_queued(m, q))?,
        Obj::MRefused(r) => mirror_step(st, kind, |m| mirror::on_refused(m, r))?,
        Obj::MAck(a) => mirror_step(st, kind, |m| mirror::on_ack(m, a))?,
        Obj::MFill(f) => mirror_step(st, kind, |m| mirror::on_fill(m, f))?,
        Obj::Corp(_) => {
            return err(
                "unhandled",
                "CorporateAction is owned by a later work package (dividends and splits are not modelled); E1 refuses to invent its ledger semantics (I5)",
            )
        }
        other => return err("unhandled", format!("No fold handler registered for {} (I5)", other.tag())),
    }
    if let Some(seq) = event.seq {
        st.last_seq = seq;
    }
    Ok(())
}

/// `_ordered`: by `seq` when every event has one (stable), else as given.
pub fn ordered(events: &[Event]) -> Vec<&Event> {
    let mut v: Vec<&Event> = events.iter().collect();
    if v.iter().all(|e| e.seq.is_some()) {
        v.sort_by_key(|e| e.seq.expect("all have seq"));
    }
    v
}

fn fold_refs(events: &[&Event]) -> R<OMap<String, AccountState>> {
    let mut states: OMap<String, AccountState> = OMap::new();
    let all: Vec<Event> = events.iter().map(|e| (*e).clone()).collect();
    for event in ordered(&all) {
        if !states.contains(&event.account) {
            states.put(&event.account, AccountState::new(&event.account));
        }
        let st = states.get_mut(&event.account).expect("just inserted");
        apply_event(st, event)?;
    }
    Ok(states)
}

/// `fold`: the full log into per-account states.
pub fn fold(events: &[Event]) -> R<OMap<String, AccountState>> {
    let refs: Vec<&Event> = events.iter().collect();
    fold_refs(&refs)
}

/// `fold_account`: the events of one account, ordered, folded; an empty state if none.
pub fn fold_account(events: &[Event], account: &str) -> R<AccountState> {
    let subset: Vec<&Event> = ordered(events).into_iter().filter(|e| e.account == account).collect();
    let states = fold_refs(&subset)?;
    Ok(states.get(account).cloned().unwrap_or_else(|| AccountState::new(account)))
}

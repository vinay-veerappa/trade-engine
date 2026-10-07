//! The venue mirror's folded state (was `ledger/mirror.py`; T2, I2). Pure: every step
//! refuses a contradiction the way the Python did. A refusal here is `LErr` kind
//! `mirror_fold` (`MirrorFoldError`); the ledger fold wraps it as a `LedgerFoldError`.

use super::model::{
    derr, err, opt_dec_eq, Instrument, MirrorAck, MirrorFill, MirrorQueued, MirrorRefused, OrderState, Side, R,
};
use super::ops::*;
use super::pydec::{PyDec, Round};

pub const MIRROR_FOLD: &str = "mirror_fold";

fn mfold<T>(msg: impl Into<String>) -> R<T> {
    err(MIRROR_FOLD, msg)
}

/// `pro_rata`: integer shares of `amount` by weight; floors, then one each to the first
/// shares still under their weight.
pub fn pro_rata(weights: &[PyDec], whole: &PyDec, amount: &PyDec) -> R<Vec<PyDec>> {
    let mut shares = Vec::new();
    for w in weights {
        let x = div(&mul(w, amount)?, whole)?;
        shares.push(x.to_integral(Round::Floor).map_err(derr)?);
    }
    let mut total = zero();
    for sh in &shares {
        total = add(&total, sh)?;
    }
    let mut remainder = sub(amount, &total)?.trunc_i128().map_err(derr)?;
    for (index, weight) in weights.iter().enumerate() {
        if remainder == 0 {
            break;
        }
        if lt(&shares[index], weight)? {
            shares[index] = shares[index].add_i128(1).map_err(derr)?;
            remainder -= 1;
        }
    }
    Ok(shares)
}

fn sign(side: Side) -> i128 {
    if side == Side::Buy {
        1
    } else {
        -1
    }
}

/// Where the sign is applied. The mirror fold's `ticket_contracts` multiplies `_sign(side) *
/// units * ratio` (the sign first); `tos_paper.reconcile.ticket_contracts` negates
/// `units * ratio` (the sign last, and a BUY is `units` untouched). The two agree in value;
/// they can round differently only past 28 digits, so each keeps its own order.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SignAt {
    First,
    Last,
}

/// Signed contracts `units` of a ticket put on the venue, per contract (legs as written).
/// A repeated contract keeps its first position and its LAST value (dict comprehension).
pub fn signed_contracts(instrument: &Instrument, side: Side, units: &PyDec, at: SignAt) -> R<OMap<Instrument, PyDec>> {
    let one = |side: Side, ratio: Option<i128>| -> R<PyDec> {
        match at {
            SignAt::First => {
                let signed = units_signed(side, units)?;
                match ratio {
                    Some(r) => mul_i(&signed, r),
                    None => Ok(signed),
                }
            }
            SignAt::Last => {
                let magnitude = match ratio {
                    Some(r) => mul_i(units, r)?,
                    None => units.clone(),
                };
                if side == Side::Buy {
                    Ok(magnitude)
                } else {
                    neg(&magnitude)
                }
            }
        }
    };
    let mut out: OMap<Instrument, PyDec> = OMap::new();
    match instrument {
        Instrument::Combo(legs) => {
            for leg in legs {
                let v = one(leg.side, Some(leg.ratio))?;
                out.insert(leg.contract.hk(), leg.contract.clone(), v);
            }
        }
        other => {
            let v = one(side, None)?;
            out.insert(other.hk(), other.clone(), v);
        }
    }
    Ok(out)
}

/// `ticket_contracts(queued, units)` of the mirror fold.
pub fn ticket_contracts(q: &MirrorQueued, units: &PyDec) -> R<OMap<Instrument, PyDec>> {
    signed_contracts(&q.instrument, q.side, units, SignAt::First)
}

/// `_sign(side) * units`.
fn units_signed(side: Side, units: &PyDec) -> R<PyDec> {
    mul(&PyDec::from_i128(sign(side)), units)
}

#[derive(Debug, Clone)]
pub struct MirrorTicketState {
    pub queued: MirrorQueued,
    pub ack: Option<MirrorAck>,
    pub venue_order_id: Option<String>,
    pub book_status: Option<OrderState>,
    pub filled: PyDec,
    pub avg_price: Option<PyDec>,
    pub closed: bool,
    pub allocated: OMap<String, PyDec>,
}

impl MirrorTicketState {
    /// `terminal`: closed, or filled through its quantity.
    pub fn terminal(&self) -> R<bool> {
        if self.closed {
            return Ok(true);
        }
        ge(&self.filled, &self.queued.quantity)
    }

    /// `remaining`: units still expected to rest or fill at the venue; zero once terminal.
    pub fn remaining(&self) -> R<PyDec> {
        if self.terminal()? {
            return Ok(zero());
        }
        sub(&self.queued.quantity, &self.filled)
    }

    fn new(queued: MirrorQueued) -> MirrorTicketState {
        MirrorTicketState {
            queued,
            ack: None,
            venue_order_id: None,
            book_status: None,
            filled: zero(),
            avg_price: None,
            closed: false,
            allocated: OMap::new(),
        }
    }
}

#[derive(Debug, Clone, Default)]
pub struct MirrorState {
    pub venue: Option<String>,
    pub tickets: OMap<String, MirrorTicketState>,
    /// `(strategy account, contract)` to signed contracts.
    pub book: OMap<(String, Instrument), PyDec>,
    pub queued_orders: OMap<String, String>,
    pub refused_orders: OMap<String, String>,
    pub order_ids: OMap<String, String>,
}

impl MirrorState {
    /// `handled`: already queued or refused at this venue, never mirrored again (I3).
    pub fn handled(&self, strategy_order_id: &str) -> bool {
        self.queued_orders.contains(strategy_order_id) || self.refused_orders.contains(strategy_order_id)
    }

    /// `open_tickets`: the tickets not terminal, in ticket-key order.
    pub fn open_tickets(&self) -> R<Vec<&MirrorTicketState>> {
        let mut all: Vec<(&String, &MirrorTicketState)> = self.tickets.iter().collect();
        all.sort_by(|a, b| a.0.cmp(b.0));
        let mut out = Vec::new();
        for (_, t) in all {
            if !t.terminal()? {
                out.push(t);
            }
        }
        Ok(out)
    }

    /// `exposure`: per (strategy account, contract) the book plus each open ticket's unfilled
    /// part, the nonzero entries only.
    pub fn exposure(&self) -> R<OMap<(String, Instrument), PyDec>> {
        let mut exposure: OMap<(String, Instrument), PyDec> = OMap::new();
        for (k, v) in self.book.iter() {
            exposure.insert(book_hk(&k.0, &k.1), k.clone(), v.clone());
        }
        for ticket in self.open_tickets()? {
            for allocation in &ticket.queued.allocations {
                let have = ticket.allocated.get(&allocation.strategy_order_id).cloned().unwrap_or_else(zero);
                let lacking = sub(&allocation.quantity, &have)?;
                for (contract, quantity) in ticket_contracts(&ticket.queued, &lacking)?.iter() {
                    let hk = book_hk(&allocation.strategy_account, contract);
                    let current = exposure.get(&hk).cloned().unwrap_or_else(zero);
                    let total = add(&current, quantity)?;
                    exposure.insert(hk, (allocation.strategy_account.clone(), contract.clone()), total);
                }
            }
        }
        let mut out: OMap<(String, Instrument), PyDec> = OMap::new();
        for (k, v) in exposure.iter() {
            if !eq(v, &zero())? {
                out.insert(book_hk(&k.0, &k.1), k.clone(), v.clone());
            }
        }
        Ok(out)
    }

    /// `expected`: per contract the book plus every open ticket's live remainder.
    pub fn expected(&self) -> R<OMap<Instrument, PyDec>> {
        let mut expected: OMap<Instrument, PyDec> = OMap::new();
        for ((_, contract), quantity) in self.book.iter() {
            let current = expected.get(&contract.hk()).cloned().unwrap_or_else(zero);
            expected.insert(contract.hk(), contract.clone(), add(&current, quantity)?);
        }
        for ticket in self.open_tickets()? {
            for (contract, quantity) in ticket_contracts(&ticket.queued, &ticket.remaining()?)?.iter() {
                let current = expected.get(&contract.hk()).cloned().unwrap_or_else(zero);
                expected.insert(contract.hk(), contract.clone(), add(&current, quantity)?);
            }
        }
        Ok(expected)
    }
}

/// The key of a book entry: injective over `(account, instrument)`.
pub fn book_hk(account: &str, instrument: &Instrument) -> String {
    format!("{}:{}{}", account.len(), account, instrument.hk())
}

fn check_venue(state: &MirrorState, venue: &str) -> R<()> {
    if let Some(v) = &state.venue {
        if v != venue {
            return mfold(format!("mirror event for venue '{venue}' folded into venue '{v}' (I8)"));
        }
    }
    Ok(())
}

fn ticket<'a>(state: &'a MirrorState, key: &str, what: &str) -> R<&'a MirrorTicketState> {
    match state.tickets.get(key) {
        Some(t) => Ok(t),
        None => mfold(format!("{what} names ticket '{key}', which was never queued (I5)")),
    }
}

pub fn on_queued(state: &mut MirrorState, q: &MirrorQueued) -> R<()> {
    check_venue(state, &q.venue)?;
    if let Some(existing) = state.tickets.get(&q.ticket_key) {
        if !existing.queued.same(q) {
            return mfold(format!("ticket '{}' was already queued with other contents (I3)", q.ticket_key));
        }
        return Ok(());
    }
    // every allocation is checked against the state BEFORE this event
    for a in &q.allocations {
        let oid = &a.strategy_order_id;
        if let Some(t) = state.queued_orders.get(oid) {
            return mfold(format!("strategy order '{oid}' is already on ticket '{t}'; refusing to mirror it twice (I3)"));
        }
        if state.refused_orders.contains(oid) {
            return mfold(format!("strategy order '{oid}' was refused at this venue; it is not queued later (I3)"));
        }
    }
    for a in &q.allocations {
        state.queued_orders.put(&a.strategy_order_id, q.ticket_key.clone());
    }
    state.venue = Some(q.venue.clone());
    state.tickets.put(&q.ticket_key, MirrorTicketState::new(q.clone()));
    Ok(())
}

pub fn on_refused(state: &mut MirrorState, r: &MirrorRefused) -> R<()> {
    check_venue(state, &r.venue)?;
    let oid = &r.strategy_order_id;
    if let Some(t) = state.queued_orders.get(oid) {
        return mfold(format!("strategy order '{oid}' is on ticket '{t}'; it cannot also be refused (I11)"));
    }
    if state.refused_orders.contains(oid) {
        return Ok(());
    }
    state.refused_orders.put(oid, r.reason.clone());
    state.venue = Some(r.venue.clone());
    Ok(())
}

pub fn on_ack(state: &mut MirrorState, ack: &MirrorAck) -> R<()> {
    check_venue(state, &ack.venue)?;
    let t = ticket(state, &ack.ticket_key, "MirrorAck")?;
    let key = ack.ticket_key.clone();
    let mut venue_order_id = t.venue_order_id.clone();
    let mut new_order_id: Option<String> = None;
    if let Some(v) = &ack.venue_order_id {
        if let Some(cur) = &venue_order_id {
            if cur != v {
                return mfold(format!("ticket '{key}' is venue order {cur}; an ack names {v} (I5)"));
            }
        }
        if let Some(owner) = state.order_ids.get(v) {
            if owner != &key {
                return mfold(format!(
                    "venue order {v} is already ticket '{owner}'; it cannot also be '{key}' (I5)"
                ));
            }
        }
        venue_order_id = Some(v.clone());
        new_order_id = Some(v.clone());
    }
    let closed = t.closed
        || ack.status == "REJECTED"
        || matches!(ack.book_status, Some(OrderState::Cancelled | OrderState::Expired | OrderState::Rejected));
    let book_status = ack.book_status.or(t.book_status);
    if let Some(v) = new_order_id {
        state.order_ids.put(&v, key.clone());
    }
    let t = state.tickets.get_mut(&key).expect("ticket checked");
    t.ack = Some(ack.clone());
    t.venue_order_id = venue_order_id;
    t.book_status = book_status;
    t.closed = closed;
    Ok(())
}

pub fn on_fill(state: &mut MirrorState, fill: &MirrorFill) -> R<()> {
    check_venue(state, &fill.venue)?;
    let t = ticket(state, &fill.ticket_key, "MirrorFill")?;
    if t.venue_order_id.as_deref() != Some(fill.venue_order_id.as_str()) {
        let proven = t.venue_order_id.as_deref().unwrap_or("None");
        return mfold(format!(
            "MirrorFill for '{}' names venue order {}; the ticket's proven Order ID is {proven} (I5)",
            fill.ticket_key, fill.venue_order_id
        ));
    }
    let queued = &t.queued;
    if gt(&fill.filled, &queued.quantity)? {
        return mfold(format!(
            "ticket '{}' filled {} of {}; refusing the over-fill (I5)",
            fill.ticket_key,
            s(&fill.filled),
            s(&queued.quantity)
        ));
    }
    if lt(&fill.filled, &t.filled)? {
        return mfold(format!(
            "ticket '{}' was filled {}; a cumulative {} cannot go down (I5)",
            fill.ticket_key,
            s(&t.filled),
            s(&fill.filled)
        ));
    }
    if eq(&fill.filled, &t.filled)? {
        if !opt_dec_eq(&Some(fill.avg_price.clone()), &t.avg_price) {
            let was = t.avg_price.as_ref().map_or("None".to_string(), s);
            return mfold(format!(
                "ticket '{}' filled {} at {was}; the venue now says {} for the same fill (I5)",
                fill.ticket_key,
                s(&fill.filled),
                s(&fill.avg_price)
            ));
        }
        return Ok(());
    }
    // the increment is split pro-rata over what each strategy order still lacks
    let mut allocated = t.allocated.clone();
    let mut lacking = Vec::new();
    for a in &queued.allocations {
        let have = allocated.get(&a.strategy_order_id).cloned().unwrap_or_else(zero);
        lacking.push(sub(&a.quantity, &have)?);
    }
    let pieces = pro_rata(&lacking, &sub(&queued.quantity, &t.filled)?, &sub(&fill.filled, &t.filled)?)?;
    let mut book = state.book.clone();
    for (a, piece) in queued.allocations.iter().zip(pieces.iter()) {
        let have = allocated.get(&a.strategy_order_id).cloned().unwrap_or_else(zero);
        allocated.put(&a.strategy_order_id, add(&have, piece)?);
        for (contract, quantity) in ticket_contracts(queued, piece)?.iter() {
            let hk = book_hk(&a.strategy_account, contract);
            let current = book.get(&hk).cloned().unwrap_or_else(zero);
            let total = add(&current, quantity)?;
            if eq(&total, &zero())? {
                book.remove(&hk);
            } else {
                book.insert(hk, (a.strategy_account.clone(), contract.clone()), total);
            }
        }
    }
    let key = fill.ticket_key.clone();
    let t = state.tickets.get_mut(&key).expect("ticket checked");
    t.filled = fill.filled.clone();
    t.avg_price = Some(fill.avg_price.clone());
    t.allocated = allocated;
    state.book = book;
    Ok(())
}

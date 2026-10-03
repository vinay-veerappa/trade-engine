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

/// Signed contracts `units` of a ticket put on the venue, per contract (legs as written).
/// A repeated contract keeps its first position and its LAST value (dict comprehension).
pub fn ticket_contracts(q: &MirrorQueued, units: &PyDec) -> R<OMap<Instrument, PyDec>> {
    let mut out: OMap<Instrument, PyDec> = OMap::new();
    match &q.instrument {
        Instrument::Combo(legs) => {
            for leg in legs {
                let v = mul_i(&units_signed(leg.side, units)?, leg.ratio)?;
                out.insert(leg.contract.hk(), leg.contract.clone(), v);
            }
        }
        other => {
            let v = units_signed(q.side, units)?;
            out.insert(other.hk(), other.clone(), v);
        }
    }
    Ok(out)
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

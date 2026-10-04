//! The chain-snapshot option venue (was `sim/snapshot_venue.py`): order validation, the
//! restore checks, DAY expiry, and every fill price (the quarter-spread model, the limit
//! rule, the combo leg scaling and its rounding residue).
//!
//! The host (`trade_engine.sim.snapshot_venue.SnapshotVenue`) passes values in and builds
//! its carriers from what comes back; a quote is looked up through a host callback by OCC
//! symbol, as `ChainSnapshot.get` does. Refusal kinds: `snapshot` (SnapshotVenueError),
//! `value` (ValueError), `type` (TypeError), `unresolvable` (UnresolvableInstrumentError).

use std::collections::HashMap;

use super::broker::{d, day_session, working, zero, Ack, VOrder};
use super::{ascii_decimal, dec, dk, dmin, now, rpartition, session_close, ts_max, Clock, Ts};
use crate::ledger::codec::py_repr;
use crate::ledger::model::{err, parse_datetime, ComboLeg, Instrument, LErr, OrderState, OrderType, Side, Tif, R};
use crate::ledger::pydec::PyDec;
use crate::options::option_style;

/// One option quote as the snapshot holds it: `mid`, `spread` and its own quote time.
#[derive(Debug, Clone)]
pub struct Quote {
    pub mid: PyDec,
    pub spread: PyDec,
    pub as_of: Ts,
}

/// The parts of a `ChainSnapshot` the venue reads; quotes come from the host by OCC.
pub struct Snap<'a> {
    pub underlying: String,
    pub as_of: Ts,
    pub underlying_price: PyDec,
    pub quote: &'a mut dyn FnMut(&str) -> R<Option<Quote>>,
}

/// A `VenueFill` the venue holds. `src` is the index of a restored fill in the host's
/// input; `leg` is the index of the order leg a fill this venue made executed.
#[derive(Debug, Clone)]
pub struct SFill {
    pub fill_id: String,
    pub order_id: String,
    pub instr: Instrument,
    pub quantity: PyDec,
    pub price: PyDec,
    pub filled_at: Ts,
    pub side: Side,
    pub fee: PyDec,
    pub leg_id: Option<String>,
    pub leg: Option<usize>,
    pub src: Option<usize>,
}

/// Where a position's instrument object came from (a dict keeps its first key): a
/// restored position (its index in the host's input) or a fill (its index here).
#[derive(Debug, Clone, Copy)]
pub enum Origin {
    Restored(usize),
    Fill(usize),
}

#[derive(Debug, Clone)]
struct Working {
    order: VOrder,
    state: OrderState,
    filled: PyDec,
    updated_at: Ts,
}

struct PosRow {
    hk: String,
    instr: Instrument,
    qty: PyDec,
    origin: Origin,
}

const BPS: i128 = 10_000;
const TICK: i64 = -4;

fn snap(msg: impl Into<String>) -> LErr {
    LErr { kind: "snapshot", msg: msg.into() }
}

/// The underlying whose chain snapshot prices `instrument`.
pub fn underlying_of(instrument: &Instrument) -> R<String> {
    match instrument {
        Instrument::Equity(s) => Ok(s.clone()),
        Instrument::Option(c) => match option_style(&c.underlying) {
            Ok(s) => Ok(s.underlying),
            Err(e) => Err(LErr { kind: if e.kind() == "unresolvable" { "unresolvable" } else { "value" }, msg: e.message().to_string() }),
        },
        Instrument::Combo(legs) => {
            let mut found: Vec<String> = Vec::new();
            for leg in legs {
                let u = underlying_of(&leg.contract)?;
                if !found.contains(&u) {
                    found.push(u);
                }
            }
            if found.len() != 1 {
                found.sort();
                let shown: Vec<String> = found.iter().map(|s| py_repr(s)).collect();
                return err(
                    "value",
                    format!("Combo {} spans underlyings [{}]", instrument.symbol()?, shown.join(", ")),
                );
            }
            Ok(found.pop().unwrap_or_default())
        }
        Instrument::Future(_) => err("unsupported", "futures not supported in snapshot venue"),
    }
}

fn is_combo(order: &VOrder) -> bool {
    matches!(order.instr, Instrument::Combo(_))
}

fn legs(order: &VOrder) -> Vec<ComboLeg> {
    match &order.instr {
        Instrument::Combo(legs) => legs.clone(),
        other => vec![ComboLeg { contract: other.clone(), ratio: 1, side: order.side }],
    }
}

fn multiplier_of(i: &Instrument) -> i128 {
    match i {
        Instrument::Option(c) => c.multiplier,
        _ => 1,
    }
}

/// Option points: a combo's net is quoted per unit of its option multiplier.
fn multiplier(order: &VOrder) -> R<i128> {
    let mut found: Vec<i128> = Vec::new();
    if let Instrument::Combo(legs) = &order.instr {
        for leg in legs {
            if let Instrument::Option(c) = &leg.contract {
                if !found.contains(&c.multiplier) {
                    found.push(c.multiplier);
                }
            }
        }
    }
    if found.len() != 1 {
        found.sort();
        let shown: Vec<String> = found.iter().map(|m| m.to_string()).collect();
        return Err(snap(format!("Combo '{}' has option multipliers [{}] (I6)", order.id, shown.join(", "))));
    }
    Ok(found[0])
}

fn pos_cmp(x: &PyDec) -> R<std::cmp::Ordering> {
    x.cmp_int(0).map_err(dk)
}

/// A `since` as Python compared it: an aware instant, or a naive one that refuses only
/// when it is compared with something.
struct Since {
    ts: Option<Ts>,
}

impl Since {
    fn parse(iso: &str) -> R<Since> {
        let (dt, aware) = parse_datetime(iso)?;
        Ok(Since { ts: if aware { Some(Ts { dt, iso: iso.to_string() }) } else { None } })
    }
    fn le(&self, t: &Ts) -> R<bool> {
        match &self.ts {
            Some(s) => Ok(t.ge(s)),
            None => err("type", "can't compare offset-naive and offset-aware datetimes"),
        }
    }
}

pub struct Venue {
    pub account_id: String,
    fraction: PyDec,
    fee: PyDec,
    slippage: PyDec,
    max_quote_age: f64,
    connected: bool,
    orders: Vec<Working>,
    index: HashMap<String, usize>,
    pub fills: Vec<SFill>,
    fill_counts: HashMap<String, u128>,
    positions: Vec<PosRow>,
    seen: HashMap<String, Ts>,
}

impl Venue {
    /// `SnapshotVenue.__init__`'s checks. A `None` decimal was not a `Decimal`; a `None`
    /// age was not an int or float (or was a bool).
    pub fn new(
        account_id: &str,
        fill_fraction: Option<PyDec>,
        fee_per_contract: Option<PyDec>,
        equity_slippage_bps: Option<PyDec>,
        max_quote_age_seconds: Option<f64>,
    ) -> R<Venue> {
        if account_id.is_empty() {
            return err("value", "account_id must be non-empty");
        }
        let check = |name: &str, v: Option<PyDec>| -> R<PyDec> {
            match v {
                Some(x) if x.is_finite() && !pos_cmp(&x)?.is_lt() => Ok(x),
                _ => err("value", format!("{name} must be a finite, non-negative Decimal")),
            }
        };
        let fraction = check("fill_fraction", fill_fraction)?;
        let fee = check("fee_per_contract", fee_per_contract)?;
        let slippage = check("equity_slippage_bps", equity_slippage_bps)?;
        let age = match max_quote_age_seconds {
            Some(a) if a > 0.0 => a,
            _ => return err("value", "max_quote_age_seconds must be positive"),
        };
        if fraction.gt(&dec("0.5")).map_err(dk)? {
            return err("value", "fill_fraction above 0.5 would trade outside the quote");
        }
        Ok(Venue {
            account_id: account_id.to_string(),
            fraction,
            fee,
            slippage,
            max_quote_age: age,
            connected: false,
            orders: Vec::new(),
            index: HashMap::new(),
            fills: Vec::new(),
            fill_counts: HashMap::new(),
            positions: Vec::new(),
            seen: HashMap::new(),
        })
    }

    pub fn connect(&mut self, clock: &mut Clock<'_>) -> R<Ts> {
        self.connected = true;
        now(clock)
    }

    /// Load what the ledger says this venue holds into an empty venue (I2).
    pub fn restore(&mut self, orders: Vec<(VOrder, OrderState)>, fills: Vec<SFill>, positions: Vec<(Instrument, PyDec)>) -> R<()> {
        if !self.orders.is_empty() || !self.fills.is_empty() || !self.positions.is_empty() {
            return Err(snap("restore() requires an empty SnapshotVenue"));
        }
        for (order, state) in orders {
            if !matches!(
                state,
                OrderState::Accepted
                    | OrderState::PartiallyFilled
                    | OrderState::Filled
                    | OrderState::Cancelled
                    | OrderState::Expired
                    | OrderState::Rejected
            ) {
                return Err(snap(format!("Cannot restore '{}' in state {}", order.id, state.value())));
            }
            if self.index.contains_key(&order.id) {
                return Err(snap(format!("Order '{}' restored twice", order.id)));
            }
            self.validate(&order)?;
            let updated_at = order.submitted_at.clone();
            self.index.insert(order.id.clone(), self.orders.len());
            self.orders.push(Working { order, state, filled: zero(), updated_at });
        }
        let mut per_leg: Vec<((String, Option<String>), PyDec)> = Vec::new();
        let mut sorted = fills;
        sorted.sort_by(|a, b| a.filled_at.key().cmp(&b.filled_at.key()).then_with(|| a.fill_id.cmp(&b.fill_id)));
        for fill in sorted {
            let Some(&i) = self.index.get(&fill.order_id) else {
                return Err(snap(format!(
                    "Fill '{}' references unrestored order '{}'",
                    fill.fill_id, fill.order_id
                )));
            };
            let (prefix, sep, number) = rpartition(&fill.fill_id, ":fill:");
            if prefix != fill.order_id || !sep || !ascii_decimal(number) {
                return Err(snap(format!("Fill id '{}' is not a SnapshotVenue fill id", fill.fill_id)));
            }
            let leg = leg_of(&self.orders[i].order, &fill)?;
            if !fill.instr.same(&leg.contract) || fill.side != leg.side {
                return Err(snap(format!("Fill '{}' does not match order '{}'", fill.fill_id, fill.order_id)));
            }
            let n: u128 = match number.trim_start_matches('0') {
                "" => 0,
                t => t.parse().map_err(|_| LErr { kind: "unsupported", msg: "a fill number beyond u128".to_string() })?,
            };
            let count = self.fill_counts.get(&fill.order_id).copied().unwrap_or(0);
            self.fill_counts.insert(fill.order_id.clone(), count.max(n));
            let key = (fill.order_id.clone(), fill.leg_id.clone());
            match per_leg.iter_mut().find(|(k, _)| *k == key) {
                Some(slot) => slot.1 = d(slot.1.add(&fill.quantity))?,
                None => per_leg.push((key, d(zero().add(&fill.quantity))?)),
            }
            let w = &mut self.orders[i];
            w.updated_at = ts_max(&w.updated_at, &fill.filled_at).clone();
            self.fills.push(fill);
        }
        for w in &mut self.orders {
            let combo = is_combo(&w.order);
            let mut least: Option<PyDec> = None;
            for (index, leg) in legs(&w.order).iter().enumerate() {
                let key = (w.order.id.clone(), if combo { Some(index.to_string()) } else { None });
                let total = per_leg.iter().find(|(k, _)| *k == key).map(|(_, q)| q.clone()).unwrap_or_else(zero);
                let units = d(total.div(&PyDec::from_i128(leg.ratio)))?;
                least = Some(match least {
                    None => units,
                    Some(l) => dmin(&l, &units)?,
                });
            }
            w.filled = least.unwrap_or_else(zero);
            let q = &w.order.quantity;
            let consistent = match w.state {
                OrderState::Accepted => pos_cmp(&w.filled)?.is_eq(),
                OrderState::Filled => w.filled.eq_num(q).map_err(dk)?,
                _ => w.filled.le(q).map_err(dk)?,
            };
            if !consistent {
                return Err(snap(format!(
                    "Order '{}' is {} with {} of {} filled",
                    w.order.id,
                    w.state.value(),
                    w.filled.to_py_string(),
                    q.to_py_string()
                )));
            }
        }
        for (i, (instr, qty)) in positions.into_iter().enumerate() {
            let hk = instr.hk();
            if self.positions.iter().any(|p| p.hk == hk) {
                return Err(snap(format!("Position {} restored twice", instr.symbol()?)));
            }
            if !pos_cmp(&qty)?.is_eq() {
                self.positions.push(PosRow { hk, instr, qty, origin: Origin::Restored(i) });
            }
        }
        Ok(())
    }

    pub fn submit(&mut self, order: VOrder, clock: &mut Clock<'_>) -> R<Ack> {
        self.require_connected()?;
        self.expire_due(clock)?;
        self.validate(&order)?;
        if let Some(&i) = self.index.get(&order.id) {
            let existing = &self.orders[i];
            if !existing.order.same(&order) {
                return Err(snap(format!("venue_order_id '{}' was reused with different terms", order.id)));
            }
            if matches!(existing.state, OrderState::Cancelled | OrderState::Rejected) {
                let msg = format!("Order is {}", existing.state.value());
                return ack(&order.id, "REJECTED", Some(msg), clock);
            }
            return ack(&order.id, "ACCEPTED", None, clock);
        }
        let at = now(clock)?;
        let id = order.id.clone();
        self.index.insert(id.clone(), self.orders.len());
        self.orders.push(Working { order, state: OrderState::Accepted, filled: zero(), updated_at: at });
        ack(&id, "ACCEPTED", None, clock)
    }

    pub fn cancel(&mut self, id: &str, clock: &mut Clock<'_>) -> R<Ack> {
        self.expire_due(clock)?;
        let i = self.require(id)?;
        let state = self.orders[i].state;
        if state == OrderState::Cancelled {
            return ack(id, "ACCEPTED", None, clock);
        }
        if !working(state) {
            return ack(id, "REJECTED", Some(format!("Order is {}", state.value())), clock);
        }
        self.orders[i].state = OrderState::Cancelled;
        self.orders[i].updated_at = now(clock)?;
        ack(id, "ACCEPTED", None, clock)
    }

    pub fn replace(&mut self, id: &str, clock: &mut Clock<'_>) -> R<Ack> {
        self.require(id)?;
        ack(id, "REJECTED", Some("SnapshotVenue does not replace orders".to_string()), clock)
    }

    /// `orders(since)`: `(id, state, filled, remaining, updated_at)` by id.
    pub fn orders_since(&mut self, since: &str, clock: &mut Clock<'_>) -> R<Vec<(String, OrderState, PyDec, PyDec, Ts)>> {
        let since = Since::parse(since)?;
        self.expire_due(clock)?;
        let mut ids: Vec<&String> = self.index.keys().collect();
        ids.sort();
        let mut out = Vec::new();
        for id in ids {
            let w = &self.orders[self.index[id]];
            if since.le(&w.updated_at)? {
                out.push((id.clone(), w.state, w.filled.clone(), d(w.order.quantity.sub(&w.filled))?, w.updated_at.clone()));
            }
        }
        Ok(out)
    }

    pub fn fills_since(&mut self, since: &str, clock: &mut Clock<'_>) -> R<Vec<usize>> {
        let since = Since::parse(since)?;
        self.expire_due(clock)?;
        let mut out = Vec::new();
        for (i, f) in self.fills.iter().enumerate() {
            if since.le(&f.filled_at)? {
                out.push(i);
            }
        }
        Ok(out)
    }

    /// `positions()`: `(origin, quantity)` by symbol, and the `as_of` (the clock).
    pub fn positions(&self, clock: &mut Clock<'_>) -> R<(Ts, Vec<(Origin, PyDec)>)> {
        let at = now(clock)?;
        let mut keyed = Vec::new();
        for p in &self.positions {
            keyed.push((p.instr.symbol()?, p));
        }
        keyed.sort_by(|a, b| a.0.cmp(&b.0));
        let mut out = Vec::new();
        for (_, p) in keyed {
            if !pos_cmp(&p.qty)?.is_eq() {
                out.push((p.origin, p.qty.clone()));
            }
        }
        Ok((at, out))
    }

    /// Match every working order on the snapshot's underlying against it; the indices of
    /// the fills it made.
    pub fn process_snapshot(&mut self, s: &mut Snap<'_>, clock: &mut Clock<'_>) -> R<Vec<usize>> {
        self.require_connected()?;
        let at = now(clock)?;
        if s.as_of.gt(&at) {
            return Err(snap(format!(
                "{} snapshot of {} is after the clock {}: look-ahead (I7)",
                s.underlying, s.as_of.iso, at.iso
            )));
        }
        self.expire_due(clock)?;
        let mut order: Vec<usize> = (0..self.orders.len()).collect();
        order.sort_by(|&a, &b| {
            let (x, y) = (&self.orders[a].order, &self.orders[b].order);
            x.submitted_at.key().cmp(&y.submitted_at.key()).then_with(|| x.id.cmp(&y.id))
        });
        let mut made = Vec::new();
        for i in order {
            let w = &self.orders[i];
            if !working(w.state) || w.order.submitted_at.gt(&s.as_of) || underlying_of(&w.order.instr)? != s.underlying {
                continue;
            }
            let o = w.order.clone();
            let Some(prices) = self.model_prices(&o, s)? else {
                if o.otype == OrderType::Market {
                    self.orders[i].state = OrderState::Rejected;
                    self.orders[i].updated_at = s.as_of.clone();
                }
                continue;
            };
            let Some(filled_at) = fill_prices(&o, &prices)? else {
                continue;
            };
            let as_of = s.as_of.clone();
            made.extend(self.fill(i, filled_at, &as_of)?);
        }
        let next = match self.seen.get(&s.underlying) {
            None => s.as_of.clone(),
            Some(prev) => ts_max(prev, &s.as_of).clone(),
        };
        self.seen.insert(s.underlying.clone(), next);
        Ok(made)
    }

    /// What `side` of `instrument` trades at in the snapshot; None if it can't be priced.
    pub fn model_price(&self, instrument: &Instrument, side: Side, s: &mut Snap<'_>) -> R<Option<PyDec>> {
        match instrument {
            Instrument::Equity(sym) => {
                if *sym != s.underlying {
                    return Ok(None);
                }
                let price = &s.underlying_price;
                let moved = d(d(price.mul(&self.slippage))?.div(&PyDec::from_i128(BPS)))?;
                Ok(Some(if side == Side::Buy { d(price.add(&moved))? } else { d(price.sub(&moved))? }))
            }
            Instrument::Option(c) => {
                let occ = c.occ()?;
                let Some(q) = (s.quote)(&occ)? else {
                    return Ok(None);
                };
                let (a, b) = (s.as_of.key(), q.as_of.key());
                let us = (a.0 as i128 - b.0 as i128) * 1_000_000 + (a.1 as i128 - b.1 as i128);
                if (us as f64) / 1e6 > self.max_quote_age {
                    // Not quoted, or a quote nobody has updated for a while: not today's market.
                    return Ok(None);
                }
                let shade = d(self.fraction.mul(&q.spread))?;
                let price = if side == Side::Buy { d(q.mid.add(&shade))? } else { d(q.mid.sub(&shade))? };
                Ok(if pos_cmp(&price)?.is_gt() { Some(price) } else { None })
            }
            Instrument::Combo(_) => Ok(None),
            Instrument::Future(_) => err("unsupported", "futures not supported in snapshot venue"),
        }
    }

    fn model_prices(&self, order: &VOrder, s: &mut Snap<'_>) -> R<Option<Vec<PyDec>>> {
        let mut prices = Vec::new();
        for leg in legs(order) {
            match self.model_price(&leg.contract, leg.side, s)? {
                None => return Ok(None),
                Some(p) => prices.push(p),
            }
        }
        Ok(Some(prices))
    }

    fn fill(&mut self, i: usize, prices: Vec<PyDec>, at: &Ts) -> R<Vec<usize>> {
        let order = self.orders[i].order.clone();
        let units = d(order.quantity.sub(&self.orders[i].filled))?;
        let combo = is_combo(&order);
        let mut made = Vec::new();
        for (index, (leg, price)) in legs(&order).into_iter().zip(prices).enumerate() {
            let quantity = d(units.mul_i128(leg.ratio))?;
            let number = self.fill_counts.get(&order.id).copied().unwrap_or(0) + 1;
            self.fill_counts.insert(order.id.clone(), number);
            let fee = if matches!(leg.contract, Instrument::Option(_)) { d(self.fee.mul(&quantity))? } else { zero() };
            // VenueFill.__post_init__
            if pos_cmp(&quantity)?.is_le() {
                return err("value", format!("VenueFill quantity must be strictly positive, got {}", quantity.to_py_string()));
            }
            if pos_cmp(&price)?.is_le() {
                return err("value", format!("VenueFill price must be strictly positive, got {} (I5)", price.to_py_string()));
            }
            let hk = leg.contract.hk();
            let change = if leg.side == Side::Buy { quantity.clone() } else { d(quantity.neg())? };
            self.fills.push(SFill {
                fill_id: format!("{}:fill:{}", order.id, number),
                order_id: order.id.clone(),
                instr: leg.contract.clone(),
                quantity,
                price,
                filled_at: at.clone(),
                side: leg.side,
                fee,
                leg_id: if combo { Some(index.to_string()) } else { None },
                leg: Some(index),
                src: None,
            });
            let fi = self.fills.len() - 1;
            match self.positions.iter_mut().find(|p| p.hk == hk) {
                Some(p) => p.qty = d(p.qty.add(&change))?,
                None => self.positions.push(PosRow {
                    hk,
                    instr: leg.contract,
                    qty: d(zero().add(&change))?,
                    origin: Origin::Fill(fi),
                }),
            }
            made.push(fi);
        }
        let w = &mut self.orders[i];
        w.filled = w.order.quantity.clone();
        w.state = OrderState::Filled;
        w.updated_at = at.clone();
        Ok(made)
    }

    fn validate(&self, order: &VOrder) -> R<()> {
        if !matches!(order.otype, OrderType::Market | OrderType::Limit) {
            return err("value", format!("SnapshotVenue does not support {} orders", order.otype.value()));
        }
        if !matches!(order.tif, Tif::Day | Tif::Gtc) {
            return err("value", format!("SnapshotVenue does not support time in force {}", order.tif.value()));
        }
        if order.allocs.len() != 1 || order.allocs[0].account != self.account_id {
            return err("value", "SnapshotVenue requires one allocation to its own account");
        }
        if let Instrument::Combo(legs) = &order.instr {
            if legs.iter().any(|l| !matches!(l.contract, Instrument::Option(_))) {
                return err("value", "SnapshotVenue combos are options only; trade the shares on their own");
            }
            multiplier(order)?;
        }
        underlying_of(&order.instr)?;
        Ok(())
    }

    fn expire_due(&mut self, clock: &mut Clock<'_>) -> R<()> {
        let at = now(clock)?;
        for w in &mut self.orders {
            if !working(w.state) || w.order.tif != Tif::Day {
                continue;
            }
            let close = session_close(day_session(&w.order.submitted_at)?)?;
            let seen = self.seen.get(&underlying_of(&w.order.instr)?);
            if at.ge(&close) {
                if let Some(seen) = seen {
                    if w.order.submitted_at.le(seen) && seen.le(&close) {
                        w.state = OrderState::Expired;
                        w.updated_at = close;
                    }
                }
            }
        }
        Ok(())
    }

    fn require_connected(&self) -> R<()> {
        if !self.connected {
            return Err(snap("Call connect() before using SnapshotVenue"));
        }
        Ok(())
    }

    fn require(&self, id: &str) -> R<usize> {
        match self.index.get(id) {
            Some(&i) => Ok(i),
            None => Err(snap(format!("Unknown SnapshotVenue order '{id}'"))),
        }
    }

    /// The order a fill belongs to, for a host building a fill's carrier.
    pub fn has(&self, id: &str) -> bool {
        self.index.contains_key(id)
    }
}

fn ack(id: &str, status: &'static str, msg: Option<String>, clock: &mut Clock<'_>) -> R<Ack> {
    Ok(Ack { id: id.to_string(), status, ts: now(clock)?, msg })
}

fn leg_of(order: &VOrder, fill: &SFill) -> R<ComboLeg> {
    let mut ls = legs(order);
    if !is_combo(order) {
        if fill.leg_id.is_some() {
            return Err(snap(format!("Fill '{}' names a leg of a single order", fill.fill_id)));
        }
        return Ok(ls.remove(0));
    }
    let shown = match &fill.leg_id {
        None => "None".to_string(),
        Some(s) => py_repr(s),
    };
    let bad = || snap(format!("Fill '{}' names leg {shown}", fill.fill_id));
    let Some(id) = &fill.leg_id else {
        return Err(bad());
    };
    if !ascii_decimal(id) {
        return Err(bad());
    }
    let t = id.trim_start_matches('0');
    let n: usize = if t.is_empty() { 0 } else { t.parse().unwrap_or(usize::MAX) };
    if n >= ls.len() {
        return Err(bad());
    }
    Ok(ls.swap_remove(n))
}

/// The price each leg fills at, or None if the order does not fill.
pub fn fill_prices(order: &VOrder, prices: &[PyDec]) -> R<Option<Vec<PyDec>>> {
    if order.otype == OrderType::Market {
        let mut out = Vec::new();
        for p in prices {
            out.push(d(p.quantize(TICK))?);
        }
        return Ok(Some(out));
    }
    let Some(limit) = &order.limit else {
        return Err(snap(format!("LIMIT order '{}' has no limit price", order.id)));
    };
    if !is_combo(order) {
        let price = &prices[0];
        let through = if order.side == Side::Sell { price.ge(limit) } else { price.le(limit) }.map_err(dk)?;
        return Ok(if through { Some(vec![limit.clone()]) } else { None });
    }
    let ls = legs(order);
    let scale = PyDec::from_i128(multiplier(order)?);
    // The side whose legs are shaded to land on the limit: the legs sold for a credit,
    // the legs bought for a debit. The other side keeps its model prices.
    let shaded = order.side;
    let net = |leg_prices: &[PyDec]| -> R<PyDec> {
        let mut total = zero();
        for (leg, price) in ls.iter().zip(leg_prices) {
            let sign: i128 = if leg.side == shaded { 1 } else { -1 };
            let term = d(d(PyDec::from_i128(sign * leg.ratio).mul(price))?.mul_i128(multiplier_of(&leg.contract)))?;
            total = d(total.add(&term))?;
        }
        d(total.div(&scale))
    };
    let model = net(prices)?;
    let out = if order.side == Side::Sell { model.lt(limit) } else { model.gt(limit) }.map_err(dk)?;
    if out {
        return Ok(None);
    }
    let mut kept = zero();
    for (leg, price) in ls.iter().zip(prices) {
        if leg.side != shaded {
            let term = d(d(PyDec::from_i128(leg.ratio).mul(price))?.mul_i128(multiplier_of(&leg.contract)))?;
            kept = d(kept.add(&term))?;
        }
    }
    let kept = d(kept.div(&scale))?;
    let factor = d(d(limit.add(&kept))?.div(&d(model.add(&kept))?))?;
    let mut filled = Vec::new();
    for (leg, price) in ls.iter().zip(prices) {
        let p = if leg.side == shaded { d(price.mul(&factor))? } else { price.clone() };
        filled.push(d(p.quantize(TICK))?);
    }
    let residue = d(limit.sub(&net(&filled)?))?;
    if !residue.is_zero() {
        // Rounding residue goes on the largest shaded leg, so the net is the limit.
        let mut best: Option<usize> = None;
        for (i, leg) in ls.iter().enumerate() {
            if leg.side != shaded {
                continue;
            }
            best = Some(match best {
                None => i,
                // max by (filled[i], -i): a later leg wins only when strictly larger.
                Some(b) => {
                    if filled[i].gt(&filled[b]).map_err(dk)? {
                        i
                    } else {
                        b
                    }
                }
            });
        }
        let Some(index) = best else {
            return err("value", "max() iterable argument is empty");
        };
        let leg = &ls[index];
        let step = d(d(residue.mul(&scale))?.div(&PyDec::from_i128(leg.ratio * multiplier_of(&leg.contract))))?;
        filled[index] = d(filled[index].add(&step))?;
    }
    for p in &filled {
        if pos_cmp(p)?.is_le() {
            return Ok(None);
        }
    }
    Ok(Some(filled))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::sim::broker::Alloc;

    fn order(otype: OrderType, limit: Option<&str>) -> VOrder {
        VOrder {
            id: "o1".into(),
            instr: Instrument::Equity("AAPL".into()),
            otype,
            side: Side::Buy,
            quantity: dec("10"),
            submitted_at: Ts::aware("2026-03-02T15:00:00+00:00", "x").unwrap(),
            tif: Tif::Day,
            limit: limit.map(dec),
            stop: None,
            trail: None,
            allocs: vec![Alloc { soid: "s".into(), account: "A".into(), qty: dec("10") }],
            parent: None,
            oco: None,
        }
    }

    #[test]
    fn market_quantizes_to_the_tick() {
        let p = fill_prices(&order(OrderType::Market, None), &[dec("1.23456")]).unwrap().unwrap();
        assert_eq!(p[0].to_py_string(), "1.2346");
    }

    #[test]
    fn buy_limit_is_inclusive() {
        let o = order(OrderType::Limit, Some("2"));
        assert!(fill_prices(&o, &[dec("2")]).unwrap().is_some());
        assert!(fill_prices(&o, &[dec("2.01")]).unwrap().is_none());
    }

    #[test]
    fn config_checks() {
        let e = Venue::new("A", Some(dec("0.6")), Some(dec("0.65")), Some(dec("5")), Some(900.0)).err().unwrap();
        assert_eq!(e.msg, "fill_fraction above 0.5 would trade outside the quote");
        let e = Venue::new("A", Some(dec("0.25")), None, Some(dec("5")), Some(900.0)).err().unwrap();
        assert_eq!(e.msg, "fee_per_contract must be a finite, non-negative Decimal");
    }
}

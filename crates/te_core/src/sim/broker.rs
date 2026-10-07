//! The deterministic one-minute-bar equity venue (was `sim/broker.py`): order
//! validation, the restore checks, expiry, the bar-sequence rule, and every fill price.
//!
//! The host (`trade_engine.sim.broker.SimBroker`) passes values in and builds its
//! carriers from what comes back. Refusal kinds: `sim` (SimBrokerError), `missing_bar`
//! (MissingBarError), `unknown_order` (UnknownVenueOrderError), `value` (ValueError).

use std::collections::{BTreeMap, HashMap};

use chrono::NaiveDate;

use super::{
    ascii_decimal, date_str, dec, dk, dmax, dmin, is_session, next_session, now, roll_next, rpartition, session_close,
    session_open, ts_max, Clock, Ts,
};
pub use crate::calendar::VenueCalendar;
use crate::ledger::model::{dec_eq, err, opt_dec_eq, Instrument, OrderState, OrderType, Side, Tif, R};
use crate::money::Money;

#[derive(Debug, Clone)]
pub struct Alloc {
    pub soid: String,
    pub account: String,
    pub qty: Money,
}

/// A `VenueOrder`'s fields.
#[derive(Debug, Clone)]
pub struct VOrder {
    pub id: String,
    pub instr: Instrument,
    pub otype: OrderType,
    pub side: Side,
    pub quantity: Money,
    pub submitted_at: Ts,
    pub tif: Tif,
    pub limit: Option<Money>,
    pub stop: Option<Money>,
    pub trail: Option<Money>,
    pub allocs: Vec<Alloc>,
    pub parent: Option<String>,
    pub oco: Option<String>,
}

impl VOrder {
    /// Dataclass `==`.
    pub fn same(&self, o: &VOrder) -> bool {
        self.id == o.id
            && self.instr.same(&o.instr)
            && self.otype == o.otype
            && self.side == o.side
            && dec_eq(&self.quantity, &o.quantity)
            && self.submitted_at.eq(&o.submitted_at)
            && self.tif == o.tif
            && opt_dec_eq(&self.limit, &o.limit)
            && opt_dec_eq(&self.stop, &o.stop)
            && opt_dec_eq(&self.trail, &o.trail)
            && self.allocs.len() == o.allocs.len()
            && self
                .allocs
                .iter()
                .zip(&o.allocs)
                .all(|(a, b)| a.soid == b.soid && a.account == b.account && dec_eq(&a.qty, &b.qty))
            && self.parent == o.parent
            && self.oco == o.oco
    }
}

/// A `VenueFill` the venue holds. `src` is the index of a restored fill in the host's
/// input (the host hands back its own object); a fill this venue made has none.
#[derive(Debug, Clone)]
pub struct VFill {
    pub fill_id: String,
    pub order_id: String,
    pub instr: Instrument,
    pub quantity: Money,
    pub price: Money,
    pub filled_at: Ts,
    pub side: Side,
    pub src: Option<usize>,
}

/// A one-minute `Bar`.
#[derive(Debug, Clone)]
pub struct Bar {
    pub instr: Instrument,
    pub ts: Ts,
    pub open: Money,
    pub high: Money,
    pub low: Money,
    pub close: Money,
    pub volume: Money,
    pub as_of: Ts,
}

impl Bar {
    fn same(&self, o: &Bar) -> bool {
        self.instr.same(&o.instr)
            && self.ts.eq(&o.ts)
            && dec_eq(&self.open, &o.open)
            && dec_eq(&self.high, &o.high)
            && dec_eq(&self.low, &o.low)
            && dec_eq(&self.close, &o.close)
            && dec_eq(&self.volume, &o.volume)
            && self.as_of.eq(&o.as_of)
    }
}

#[derive(Debug, Clone)]
pub struct Pos {
    pub instr: Instrument,
    pub qty: Money,
    pub avg: Money,
    pub as_of: Ts,
}

impl Pos {
    /// Contract point value: 1 for equities/options, or `point_value` from the spec for futures.
    pub fn point_value(&self) -> Money {
        match &self.instr {
            Instrument::Future(c) => c.point_value.clone(),
            _ => dec("1"),
        }
    }

    /// Unrealized P&L at `mark`: `(mark - avg) * point_value * qty`.
    pub fn unrealized_pnl(&self, mark: &Money) -> R<Money> {
        let diff = d(mark.sub(&self.avg))?;
        let pv = self.point_value();
        d(d(diff.mul(&pv))?.mul(&self.qty))
    }
}

#[derive(Debug, Clone)]
pub struct Working {
    pub order: VOrder,
    pub state: OrderState,
    pub filled: Money,
    pub updated_at: Ts,
    pub triggered: bool,
}

#[derive(Debug, Clone)]
pub struct Ack {
    pub id: String,
    pub status: &'static str,
    pub ts: Ts,
    pub msg: Option<String>,
}

/// What `replace` does after its own checks: answer now, or let the host build the
/// replacement `VenueOrder` (whose constructor may refuse) at this quantity.
pub enum Begin {
    Done(Ack),
    Go(Money),
}

/// An insertion-ordered map keyed by `Instrument` (a dict's iteration order).
#[derive(Debug, Clone)]
struct IMap<V> {
    items: Vec<(String, V)>,
}

impl<V> IMap<V> {
    fn new() -> Self {
        IMap { items: Vec::new() }
    }
    fn get(&self, k: &str) -> Option<&V> {
        self.items.iter().find(|(h, _)| h == k).map(|(_, v)| v)
    }
    fn contains(&self, k: &str) -> bool {
        self.get(k).is_some()
    }
    fn put(&mut self, k: String, v: V) {
        match self.items.iter_mut().find(|(h, _)| *h == k) {
            Some(slot) => slot.1 = v,
            None => self.items.push((k, v)),
        }
    }
    fn is_empty(&self) -> bool {
        self.items.is_empty()
    }
}

pub(super) fn working(s: OrderState) -> bool {
    matches!(s, OrderState::Accepted | OrderState::PartiallyFilled)
}

pub(super) fn d(r: Result<Money, crate::money::DecErr>) -> R<Money> {
    r.map_err(dk)
}

pub(super) fn zero() -> Money {
    dec("0")
}

/// The Globex calendar of a futures instrument's root (P6C): the calendar follows the instrument, not the book.
fn gcal(instr: &Instrument) -> R<crate::calendar::globex::GlobexCalendar> {
    match instr {
        Instrument::Future(fc) => crate::calendar::globex::GlobexCalendar::for_root(&fc.root).map_err(gerr),
        _ => err("value", "SimBroker supports futures only"),
    }
}

fn gerr(e: crate::calendar::globex::GlobexError) -> crate::ledger::model::LErr {
    crate::ledger::model::LErr { kind: "value", msg: e.to_string() }
}

/// An `int(digits)` sort key: shorter (leading zeros dropped) is smaller.
fn int_key(digits: &str) -> (usize, String) {
    let t = digits.trim_start_matches('0');
    (t.len(), t.to_string())
}

type Prio = (bool, bool, (usize, String), String);

pub struct Book {
    pub account_id: String,
    pub venue: VenueCalendar,
    bps: Money,
    pub slippage_ticks: u32,
    connected: bool,
    orders: Vec<Working>,
    index: HashMap<String, usize>,
    pub fills: Vec<VFill>,
    fill_counts: HashMap<String, u128>,
    last_bars: IMap<Bar>,
    positions: IMap<Pos>,
}

impl Book {
    /// `SimBroker.__init__`'s checks. `is_decimal`: the slippage was a `Decimal`.
    pub fn new(account_id: &str, is_decimal: bool, slippage_bps: Money) -> R<Book> {
        if account_id.is_empty() {
            return err("value", "account_id must be non-empty");
        }
        if !is_decimal || !slippage_bps.is_finite() {
            return err("value", "slippage_bps must be a finite Decimal");
        }
        if slippage_bps.cmp_int(0).map_err(dk)?.is_lt() {
            return err("value", "slippage_bps must be non-negative");
        }
        Ok(Book {
            account_id: account_id.to_string(),
            venue: VenueCalendar::Xnys,
            bps: slippage_bps,
            slippage_ticks: 0,
            connected: false,
            orders: Vec::new(),
            index: HashMap::new(),
            fills: Vec::new(),
            fill_counts: HashMap::new(),
            last_bars: IMap::new(),
            positions: IMap::new(),
        })
    }

    /// Dedicated constructor for a futures simulation book using `calendar::globex`.
    pub fn new_futures(account_id: &str, slippage_ticks: u32) -> R<Book> {
        if account_id.is_empty() {
            return err("value", "account_id must be non-empty");
        }
        Ok(Book {
            account_id: account_id.to_string(),
            venue: VenueCalendar::Globex,
            bps: zero(),
            slippage_ticks,
            connected: false,
            orders: Vec::new(),
            index: HashMap::new(),
            fills: Vec::new(),
            fill_counts: HashMap::new(),
            last_bars: IMap::new(),
            positions: IMap::new(),
        })
    }

    /// Constructor specifying venue calendar and slippage parameters explicitly.
    pub fn new_with_venue(
        account_id: &str,
        is_decimal: bool,
        slippage_bps: Money,
        slippage_ticks: u32,
        venue: VenueCalendar,
    ) -> R<Book> {
        if account_id.is_empty() {
            return err("value", "account_id must be non-empty");
        }
        if !is_decimal || !slippage_bps.is_finite() {
            return err("value", "slippage_bps must be a finite Decimal");
        }
        if slippage_bps.cmp_int(0).map_err(dk)?.is_lt() {
            return err("value", "slippage_bps must be non-negative");
        }
        Ok(Book {
            account_id: account_id.to_string(),
            venue,
            bps: slippage_bps,
            slippage_ticks,
            connected: false,
            orders: Vec::new(),
            index: HashMap::new(),
            fills: Vec::new(),
            fill_counts: HashMap::new(),
            last_bars: IMap::new(),
            positions: IMap::new(),
        })
    }

    pub fn with_venue(mut self, venue: VenueCalendar) -> Self {
        self.venue = venue;
        self
    }

    pub fn with_slippage_ticks(mut self, slippage_ticks: u32) -> Self {
        self.slippage_ticks = slippage_ticks;
        self
    }

    pub fn venue(&self) -> VenueCalendar {
        self.venue
    }

    pub fn position_pnl(&self, symbol: &str, mark: &Money) -> R<Option<Money>> {
        for (_, p) in &self.positions.items {
            if p.instr.symbol()? == symbol {
                if !p.qty.eq_num(&zero()).map_err(dk)? {
                    return Ok(Some(p.unrealized_pnl(mark)?));
                }
            }
        }
        Ok(None)
    }

    pub fn connect(&mut self, clock: &mut Clock<'_>) -> R<Ts> {
        self.connected = true;
        now(clock)
    }

    pub fn restore(
        &mut self,
        orders: Vec<(VOrder, OrderState)>,
        fills: Vec<VFill>,
        positions: Vec<Pos>,
        clock: &mut Clock<'_>,
    ) -> R<()> {
        if !self.orders.is_empty() || !self.fills.is_empty() || !self.last_bars.is_empty() || !self.positions.is_empty() {
            return err("sim", "restore() requires an empty SimBroker");
        }
        let mut restored = orders;
        restored.sort_by_key(|(o, _)| o.parent.is_some());
        for (order, state) in restored {
            if !matches!(
                state,
                OrderState::Accepted
                    | OrderState::PartiallyFilled
                    | OrderState::Filled
                    | OrderState::Cancelled
                    | OrderState::Expired
                    | OrderState::Rejected
            ) {
                return err("sim", format!("Cannot restore '{}' in state {}", order.id, state.value()));
            }
            if self.index.contains_key(&order.id) {
                return err("sim", format!("Order '{}' restored twice", order.id));
            }
            self.validate(&order)?;
            if order.otype == OrderType::StopLimit && state == OrderState::Accepted {
                let first = now(clock)?;
                if !self.has_expired(&order, &first)? {
                    let second = now(clock)?;
                    if second.gt(&self.first_eligible_bar(&order.instr, &order.submitted_at)?) {
                        return err(
                            "sim",
                            format!(
                                "Cannot restore stop-limit '{}': bars since its submission may have triggered it",
                                order.id
                            ),
                        );
                    }
                }
            }
            let updated_at = order.submitted_at.clone();
            self.index.insert(order.id.clone(), self.orders.len());
            self.orders.push(Working { order, state, filled: zero(), updated_at, triggered: false });
        }
        let mut sorted = fills;
        sorted.sort_by(|a, b| a.filled_at.key().cmp(&b.filled_at.key()).then_with(|| a.fill_id.cmp(&b.fill_id)));
        for fill in sorted {
            let Some(&i) = self.index.get(&fill.order_id) else {
                return err(
                    "sim",
                    format!("Fill '{}' references unrestored order '{}'", fill.fill_id, fill.order_id),
                );
            };
            let (prefix, sep, number) = rpartition(&fill.fill_id, ":fill:");
            if prefix != fill.order_id || !sep || !ascii_decimal(number) {
                return err("sim", format!("Fill id '{}' is not a SimBroker fill id", fill.fill_id));
            }
            let w = &self.orders[i];
            if !fill.instr.same(&w.order.instr) || fill.side != w.order.side {
                return err("sim", format!("Fill '{}' does not match order '{}'", fill.fill_id, fill.order_id));
            }
            let n: u128 = match number.trim_start_matches('0') {
                "" => 0,
                t => t.parse().map_err(|_| crate::ledger::model::LErr {
                    kind: "unsupported",
                    msg: "a fill number beyond u128".to_string(),
                })?,
            };
            let count = self.fill_counts.get(&fill.order_id).copied().unwrap_or(0);
            self.fill_counts.insert(fill.order_id.clone(), count.max(n));
            let w = &mut self.orders[i];
            w.filled = d(w.filled.add(&fill.quantity))?;
            w.updated_at = ts_max(&w.updated_at, &fill.filled_at).clone();
            w.triggered = true;
            self.fills.push(fill);
        }
        for w in &self.orders {
            let filled = &w.filled;
            let quantity = &w.order.quantity;
            let consistent = match w.state {
                OrderState::Accepted => filled.cmp_int(0).map_err(dk)?.is_eq(),
                OrderState::PartiallyFilled => filled.cmp_int(0).map_err(dk)?.is_gt() && filled.lt(quantity).map_err(dk)?,
                OrderState::Filled => filled.eq_num(quantity).map_err(dk)?,
                _ => filled.le(quantity).map_err(dk)?,
            };
            if !consistent {
                return err(
                    "sim",
                    format!(
                        "Order '{}' is {} with {} of {} filled",
                        w.order.id,
                        w.state.value(),
                        filled.canon(),
                        quantity.canon()
                    ),
                );
            }
        }
        for p in positions {
            let hk = p.instr.hk();
            if self.positions.contains(&hk) {
                return err("sim", format!("Position {} restored twice", p.instr.symbol()?));
            }
            if !p.qty.eq_num(&zero()).map_err(dk)? {
                self.positions.put(hk, p);
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
                return err("sim", format!("venue_order_id '{}' was reused with different terms", order.id));
            }
            return match existing.state {
                OrderState::Cancelled => self.ack(&order.id, "REJECTED", Some("Order is already cancelled".into()), clock),
                OrderState::Rejected => self.ack(&order.id, "REJECTED", Some("Order was rejected".into()), clock),
                _ => self.ack(&order.id, "ACCEPTED", None, clock),
            };
        }
        let at = now(clock)?;
        let late = self.late_exit_reason(&order);
        let id = order.id.clone();
        let state = if late.is_none() { OrderState::Accepted } else { OrderState::Rejected };
        let idx = self.orders.len();
        self.index.insert(id.clone(), idx);
        self.orders.push(Working { order, state, filled: zero(), updated_at: at, triggered: false });
        if let Some(reason) = late {
            return self.ack(&id, "REJECTED", Some(reason), clock);
        }
        self.fill_stop_inside_entry_bar(idx)?;
        self.ack(&id, "ACCEPTED", None, clock)
    }

    pub fn cancel(&mut self, id: &str, clock: &mut Clock<'_>) -> R<Ack> {
        self.expire_due(clock)?;
        let i = self.require_order(id)?;
        match self.orders[i].state {
            OrderState::Cancelled => return self.ack(id, "ACCEPTED", None, clock),
            s @ (OrderState::Filled | OrderState::Expired | OrderState::Rejected) => {
                return self.ack(id, "REJECTED", Some(format!("Order is {}", s.value())), clock)
            }
            _ => {}
        }
        self.orders[i].state = OrderState::Cancelled;
        self.orders[i].updated_at = self.event_time(clock)?;
        self.ack(id, "ACCEPTED", None, clock)
    }

    /// `replace` up to building the replacement order.
    pub fn replace_begin(&mut self, id: &str, new_quantity: Option<Money>, clock: &mut Clock<'_>) -> R<Begin> {
        self.expire_due(clock)?;
        let i = self.require_order(id)?;
        let w = &self.orders[i];
        if !working(w.state) {
            let msg = format!("Order is {}", w.state.value());
            return Ok(Begin::Done(self.ack(id, "REJECTED", Some(msg), clock)?));
        }
        let has_new = new_quantity.is_some();
        let quantity = new_quantity.unwrap_or_else(|| w.order.quantity.clone());
        if !quantity.is_finite() || quantity.lt(&w.filled).map_err(dk)? {
            let msg = "Replacement quantity must be finite and at least the filled quantity".to_string();
            return Ok(Begin::Done(self.ack(id, "REJECTED", Some(msg), clock)?));
        }
        if quantity.cmp_int(0).map_err(dk)?.is_le() {
            return Ok(Begin::Done(self.ack(id, "REJECTED", Some("Replacement quantity must be positive".into()), clock)?));
        }
        if has_new && w.order.allocs.len() != 1 {
            let msg = "SimBroker requires one-to-one order allocations".to_string();
            return Ok(Begin::Done(self.ack(id, "REJECTED", Some(msg), clock)?));
        }
        Ok(Begin::Go(quantity))
    }

    /// The replacement's constructor refused: reject with its message.
    pub fn replace_reject(&mut self, id: &str, msg: String, clock: &mut Clock<'_>) -> R<Ack> {
        self.ack(id, "REJECTED", Some(msg), clock)
    }

    /// Install the replacement order.
    pub fn replace_commit(&mut self, id: &str, order: VOrder, clock: &mut Clock<'_>) -> R<Ack> {
        let i = self.require_order(id)?;
        let full = self.orders[i].filled.eq_num(&order.quantity).map_err(dk)?;
        self.orders[i].order = order;
        if full {
            self.orders[i].state = OrderState::Filled;
        }
        self.orders[i].updated_at = self.event_time(clock)?;
        self.ack(id, "ACCEPTED", None, clock)
    }

    /// `orders(since)`: `(id, state, filled, remaining, updated_at)` by id.
    pub fn orders_since(&mut self, since: &str, clock: &mut Clock<'_>) -> R<Vec<(String, OrderState, Money, Money, Ts)>> {
        let since = Ts::aware(since, "since")?;
        self.expire_due(clock)?;
        let mut ids: Vec<&String> = self.index.keys().collect();
        ids.sort();
        let mut out = Vec::new();
        for id in ids {
            let w = &self.orders[self.index[id]];
            if w.updated_at.ge(&since) {
                out.push((id.clone(), w.state, w.filled.clone(), d(w.order.quantity.sub(&w.filled))?, w.updated_at.clone()));
            }
        }
        Ok(out)
    }

    /// `fills(since)`: the positions in `fills` of those at or after `since`.
    pub fn fills_since(&mut self, since: &str, clock: &mut Clock<'_>) -> R<Vec<usize>> {
        let since = Ts::aware(since, "since")?;
        self.expire_due(clock)?;
        Ok((0..self.fills.len()).filter(|&i| self.fills[i].filled_at.ge(&since)).collect())
    }

    /// `positions()`: the open positions by symbol.
    pub fn positions(&mut self, clock: &mut Clock<'_>) -> R<Vec<Pos>> {
        self.expire_due(clock)?;
        let mut keyed = Vec::new();
        for (_, p) in &self.positions.items {
            keyed.push((p.instr.symbol()?, p.clone()));
        }
        keyed.sort_by(|a, b| a.0.cmp(&b.0));
        let mut out = Vec::new();
        for (_, p) in keyed {
            if !p.qty.eq_num(&zero()).map_err(dk)? {
                out.push(p);
            }
        }
        Ok(out)
    }

    pub fn cash_events(&self, since: &str) -> R<()> {
        Ts::aware(since, "since").map(|_| ())
    }

    /// Match working orders against one bar; the positions in `fills` of the new fills.
    pub fn process_bar(&mut self, bar: Option<Bar>) -> R<Vec<usize>> {
        self.require_connected()?;
        let Some(bar) = bar else {
            return err("missing_bar", "Cannot simulate fills without an observed bar");
        };
        match self.venue {
            VenueCalendar::Xnys => {
                if !matches!(bar.instr, Instrument::Equity(_)) {
                    return err("value", "SimBroker supports equities only");
                }
            }
            VenueCalendar::Globex => {
                if !matches!(bar.instr, Instrument::Future(_)) {
                    return err("value", "SimBroker supports futures only");
                }
            }
        }
        if bar.ts.off_minute() {
            return err("value", "SimBroker requires minute-aligned bar timestamps");
        }
        let hk = bar.instr.hk();
        if let Some(previous) = self.last_bars.get(&hk) {
            if bar.ts.eq(&previous.ts) {
                if !bar.same(previous) {
                    return err("value", "Conflicting bars share the same instrument and timestamp");
                }
                return Ok(Vec::new());
            }
        }
        self.check_bar_sequence(&bar)?;
        for i in 0..self.orders.len() {
            let w = &self.orders[i];
            if w.order.instr.same(&bar.instr) && working(w.state) && self.has_expired(&w.order, &bar.ts)? {
                let w = &mut self.orders[i];
                w.state = OrderState::Expired;
                w.updated_at = bar.ts.clone();
            }
        }
        if !self.is_regular_session_bar(&bar)? {
            self.last_bars.put(hk, bar);
            return Ok(Vec::new());
        }
        let mut active = Vec::new();
        for (i, w) in self.orders.iter().enumerate() {
            if w.order.instr.same(&bar.instr) && working(w.state) && w.filled.lt(&w.order.quantity).map_err(dk)? {
                active.push(i);
            }
        }
        let mut candidates: Vec<(usize, Money)> = Vec::new();
        for i in active {
            if let Some(price) = self.execution_price(i, &bar)? {
                candidates.push((i, price));
            }
        }
        let mut by_group: BTreeMap<String, Vec<(usize, Money)>> = BTreeMap::new();
        let mut standalone = Vec::new();
        for c in candidates {
            match &self.orders[c.0].order.oco {
                None => standalone.push(c),
                Some(g) => by_group.entry(g.clone()).or_default().push(c),
            }
        }
        let start = self.fills.len();
        let mut out = Vec::new();
        for (i, price) in standalone {
            if let Some(f) = self.fill(i, price, &bar.ts)? {
                out.push(f);
            }
        }
        for (_, mut choices) in by_group {
            let keys: Vec<Prio> = choices.iter().map(|(i, _)| self.oco_priority(*i)).collect();
            let mut order: Vec<usize> = (0..choices.len()).collect();
            order.sort_by(|&a, &b| keys[a].cmp(&keys[b]));
            let sorted: Vec<(usize, Money)> = order.into_iter().map(|k| choices[k].clone()).collect();
            choices = sorted;
            let stop = choices.iter().find(|(i, _)| self.orders[*i].order.otype == OrderType::Stop).cloned();
            let selected = match stop {
                Some(s) => vec![s],
                None => choices,
            };
            for (i, price) in selected {
                if let Some(f) = self.fill(i, price, &bar.ts)? {
                    out.push(f);
                }
            }
        }
        debug_assert!(out.iter().all(|&f| f >= start));
        self.last_bars.put(hk, bar);
        Ok(out)
    }

    fn execution_price(&mut self, i: usize, bar: &Bar) -> R<Option<Money>> {
        let order = self.orders[i].order.clone();
        if bar.ts.le(&order.submitted_at) {
            return Ok(None);
        }
        let base = if order.tif == Tif::Opg {
            if !bar.ts.eq(&self.opg_session_open(&order.instr, &order.submitted_at)?) || order.otype != OrderType::Market {
                return Ok(None);
            }
            bar.open.clone()
        } else {
            match order.otype {
                OrderType::Market => bar.open.clone(),
                OrderType::Limit => {
                    let Some(limit) = &order.limit else {
                        return err("sim", "Accepted limit order has no limit price");
                    };
                    if order.side == Side::Buy {
                        if bar.low.gt(limit).map_err(dk)? {
                            return Ok(None);
                        }
                        dmin(&bar.open, limit)?
                    } else {
                        if bar.high.lt(limit).map_err(dk)? {
                            return Ok(None);
                        }
                        dmax(&bar.open, limit)?
                    }
                }
                OrderType::Stop => {
                    let Some(stop) = &order.stop else {
                        return err("sim", "Accepted stop order has no stop price");
                    };
                    if order.side == Side::Buy {
                        if bar.high.lt(stop).map_err(dk)? {
                            return Ok(None);
                        }
                        dmax(&bar.open, stop)?
                    } else {
                        if bar.low.gt(stop).map_err(dk)? {
                            return Ok(None);
                        }
                        dmin(&bar.open, stop)?
                    }
                }
                OrderType::StopLimit => match self.stop_limit_base(i, bar)? {
                    None => return Ok(None),
                    Some(b) => b,
                },
                other => return err("sim", format!("Unsupported accepted order type {}", other.value())),
            }
        };
        let slipped = self.slipped_price(&base, &order)?;
        Ok(Some(slipped))
    }

    fn stop_limit_base(&mut self, i: usize, bar: &Bar) -> R<Option<Money>> {
        let w = &mut self.orders[i];
        let (Some(stop), Some(limit)) = (w.order.stop.clone(), w.order.limit.clone()) else {
            return err("sim", "Accepted stop-limit order has no stop or limit price");
        };
        if w.order.side == Side::Buy {
            if !w.triggered {
                if bar.high.lt(&stop).map_err(dk)? {
                    return Ok(None);
                }
                w.triggered = true;
                if bar.open.lt(&stop).map_err(dk)? {
                    return Ok(if stop.gt(&limit).map_err(dk)? { None } else { Some(stop) });
                }
            }
            if bar.low.gt(&limit).map_err(dk)? {
                return Ok(None);
            }
            return Ok(Some(dmin(&bar.open, &limit)?));
        }
        if !w.triggered {
            if bar.low.gt(&stop).map_err(dk)? {
                return Ok(None);
            }
            w.triggered = true;
            if bar.open.gt(&stop).map_err(dk)? {
                return Ok(if stop.lt(&limit).map_err(dk)? { None } else { Some(stop) });
            }
        }
        if bar.high.lt(&limit).map_err(dk)? {
            return Ok(None);
        }
        Ok(Some(dmax(&bar.open, &limit)?))
    }

    fn slipped(&self, base: &Money, side: Side) -> R<Money> {
        let one = dec("1");
        let frac = d(self.bps.div(&dec("10000")))?;
        let factor = if side == Side::Buy { d(one.add(&frac))? } else { d(one.sub(&frac))? };
        d(base.mul(&factor))
    }

    fn slipped_price(&self, base: &Money, order: &VOrder) -> R<Money> {
        match &order.instr {
            Instrument::Future(contract) => {
                if matches!(order.otype, OrderType::Limit | OrderType::StopLimit) {
                    // Limits do not slip (replay-sim.ts rule 4)
                    Ok(base.clone())
                } else {
                    crate::sim::tick::slip_ticks(base, order.side, self.slippage_ticks, &contract.tick_size)
                }
            }
            _ => {
                let slipped = self.slipped(base, order.side)?;
                if matches!(order.otype, OrderType::Limit | OrderType::StopLimit) {
                    if let Some(limit) = &order.limit {
                        return Ok(if order.side == Side::Buy { dmin(&slipped, limit)? } else { dmax(&slipped, limit)? });
                    }
                }
                Ok(slipped)
            }
        }
    }

    fn late_exit_reason(&self, order: &VOrder) -> Option<String> {
        let parent = order.parent.as_ref()?;
        if order.otype == OrderType::Market {
            return None;
        }
        let mut latest_entry: Option<&Ts> = None;
        for f in &self.fills {
            if &f.order_id == parent {
                latest_entry = Some(match latest_entry {
                    None => &f.filled_at,
                    Some(m) => ts_max(m, &f.filled_at),
                });
            }
        }
        let latest_bar = self.last_bars.get(&order.instr.hk());
        let (Some(entry), Some(bar)) = (latest_entry, latest_bar) else {
            return None;
        };
        if bar.ts.le(entry) {
            return None;
        }
        Some(format!(
            "Exit arrived after bars following entry '{parent}' filled at {} were simulated (latest bar {}); reconcile after every bar",
            entry.iso, bar.ts.iso
        ))
    }

    fn fill_stop_inside_entry_bar(&mut self, i: usize) -> R<()> {
        let order = self.orders[i].order.clone();
        let Some(parent) = &order.parent else {
            return Ok(());
        };
        if order.otype != OrderType::Stop {
            return Ok(());
        }
        let Some(entry_bar) = self.last_bars.get(&order.instr.hk()).cloned() else {
            return Ok(());
        };
        if !self.is_regular_session_bar(&entry_bar)? {
            return Ok(());
        }
        let entry_price = self
            .fills
            .iter()
            .filter(|f| &f.order_id == parent && f.filled_at.eq(&entry_bar.ts))
            .last()
            .map(|f| f.price.clone());
        let (Some(entry_price), Some(stop)) = (entry_price, order.stop.clone()) else {
            return Ok(());
        };
        let base = if order.side == Side::Sell {
            if entry_bar.low.gt(&stop).map_err(dk)? {
                return Ok(());
            }
            dmin(&stop, &entry_price)?
        } else {
            if entry_bar.high.lt(&stop).map_err(dk)? {
                return Ok(());
            }
            dmax(&stop, &entry_price)?
        };
        let price = self.slipped_price(&base, &order)?;
        self.fill(i, price, &entry_bar.ts)?;
        Ok(())
    }

    fn fill(&mut self, i: usize, price: Money, filled_at: &Ts) -> R<Option<usize>> {
        if !working(self.orders[i].state) {
            return Ok(None);
        }
        let order = self.orders[i].order.clone();
        let mut quantity = d(order.quantity.sub(&self.orders[i].filled))?;
        if let Some(parent) = &order.parent {
            quantity = dmin(&quantity, &self.bracket_open_quantity(parent)?)?;
            let position = self.positions.get(&order.instr.hk()).map(|p| p.qty.clone()).unwrap_or_else(zero);
            let closes = (position.cmp_int(0).map_err(dk)?.is_gt() && order.side == Side::Sell)
                || (position.cmp_int(0).map_err(dk)?.is_lt() && order.side == Side::Buy);
            let available = if closes { d(position.abs())? } else { zero() };
            quantity = dmin(&quantity, &available)?;
            if quantity.cmp_int(0).map_err(dk)?.is_eq() {
                return Ok(None);
            }
        }
        let n = self.fill_counts.get(&order.id).copied().unwrap_or(0) + 1;
        self.fill_counts.insert(order.id.clone(), n);
        // VenueFill.__post_init__
        if quantity.cmp_int(0).map_err(dk)?.is_le() {
            return err(
                "value",
                format!("VenueFill quantity must be strictly positive, got {}", quantity.canon()),
            );
        }
        if price.cmp_int(0).map_err(dk)?.is_le() {
            return err("value", format!("VenueFill price must be strictly positive, got {} (I5)", price.canon()));
        }
        let fill = VFill {
            fill_id: format!("{}:fill:{}", order.id, n),
            order_id: order.id.clone(),
            instr: order.instr.clone(),
            quantity: quantity.clone(),
            price,
            filled_at: filled_at.clone(),
            side: order.side,
            src: None,
        };
        self.fills.push(fill);
        let w = &mut self.orders[i];
        w.filled = d(w.filled.add(&quantity))?;
        w.state = if w.filled.eq_num(&w.order.quantity).map_err(dk)? {
            OrderState::Filled
        } else {
            OrderState::PartiallyFilled
        };
        w.updated_at = filled_at.clone();
        let at = self.fills.len() - 1;
        self.update_position(at)?;
        Ok(Some(at))
    }

    fn bracket_open_quantity(&self, parent: &str) -> R<Money> {
        let p = self.require_order(parent)?;
        let mut exited = zero();
        for w in &self.orders {
            if w.order.parent.as_deref() == Some(parent) {
                exited = d(exited.add(&w.filled))?;
            }
        }
        dmax(&d(self.orders[p].filled.sub(&exited))?, &zero())
    }

    fn update_position(&mut self, f: usize) -> R<()> {
        let fill = self.fills[f].clone();
        let hk = fill.instr.hk();
        let (quantity, average) = match self.positions.get(&hk) {
            Some(p) => (p.qty.clone(), p.avg.clone()),
            None => (zero(), zero()),
        };
        let change = if fill.side == Side::Buy { fill.quantity.clone() } else { d(fill.quantity.neg())? };
        let updated = d(quantity.add(&change))?;
        let q_sign = quantity.cmp_int(0).map_err(dk)?;
        let updated_avg = if q_sign.is_eq() || q_sign.is_gt() == change.cmp_int(0).map_err(dk)?.is_gt() {
            let aq = d(quantity.abs())?;
            let ac = d(change.abs())?;
            let basis = d(aq.add(&ac))?;
            d(d(d(aq.mul(&average))?.add(&d(ac.mul(&fill.price))?))?.div(&basis))?
        } else if updated.cmp_int(0).map_err(dk)?.is_eq() {
            zero()
        } else if updated.cmp_int(0).map_err(dk)?.is_gt() != q_sign.is_gt() {
            fill.price.clone()
        } else {
            average
        };
        self.positions.put(hk, Pos { instr: fill.instr.clone(), qty: updated, avg: updated_avg, as_of: fill.filled_at.clone() });
        Ok(())
    }

    fn expire_due(&mut self, clock: &mut Clock<'_>) -> R<()> {
        let at = now(clock)?;
        for i in 0..self.orders.len() {
            let w = &self.orders[i];
            if !working(w.state) || !self.has_expired(&w.order, &at)? {
                continue;
            }
            let Some(instant) = self.expiry_instant(&w.order)? else {
                return err("sim", "Expired order has no expiry instant");
            };
            let last_chance = if w.order.tif == Tif::Opg { instant.clone() } else { instant.add_seconds(-60)? };
            match self.last_bars.get(&w.order.instr.hk()) {
                None => continue,
                Some(b) if b.ts.lt(&last_chance) => continue,
                Some(_) => {}
            }
            let w = &mut self.orders[i];
            w.state = OrderState::Expired;
            w.updated_at = instant;
        }
        Ok(())
    }

    fn has_expired(&self, order: &VOrder, at: &Ts) -> R<bool> {
        if at.le(&order.submitted_at) {
            return Ok(false);
        }
        let Some(instant) = self.expiry_instant(order)? else {
            return Ok(false);
        };
        Ok(if order.tif == Tif::Opg { at.gt(&instant) } else { at.ge(&instant) })
    }

    fn expiry_instant(&self, order: &VOrder) -> R<Option<Ts>> {
        match order.tif {
            Tif::Day => match self.venue {
                VenueCalendar::Xnys => Ok(Some(session_close(day_session(&order.submitted_at)?)?)),
                VenueCalendar::Globex => {
                    let t_utc = order.submitted_at.to_utc_chrono()?;
                    let cal = gcal(&order.instr)?;
                    let td = cal.session_or_next(t_utc).map_err(gerr)?;
                    let close_utc = cal.session_close(td).map_err(gerr)?;
                    Ok(Some(Ts::utc(close_utc)))
                }
            },
            Tif::Opg => Ok(Some(self.opg_session_open(&order.instr, &order.submitted_at)?)),
            _ => Ok(None),
        }
    }

    fn first_eligible_bar(&self, instr: &Instrument, submitted_at: &Ts) -> R<Ts> {
        match self.venue {
            VenueCalendar::Xnys => {
                let open = session_open(day_session(submitted_at)?)?;
                if submitted_at.lt(&open) {
                    return Ok(open);
                }
                submitted_at.floor_minute().add_seconds(60)
            }
            VenueCalendar::Globex => {
                let t_utc = submitted_at.to_utc_chrono()?;
                let cal = gcal(instr)?;
                let td = cal.session_or_next(t_utc).map_err(gerr)?;
                let open_utc = cal.session_open(td).map_err(gerr)?;
                let open = Ts::utc(open_utc);
                if submitted_at.lt(&open) {
                    return Ok(open);
                }
                submitted_at.floor_minute().add_seconds(60)
            }
        }
    }

    fn opg_session_open(&self, instr: &Instrument, submitted_at: &Ts) -> R<Ts> {
        match self.venue {
            VenueCalendar::Xnys => {
                let d = submitted_at.ny_date()?;
                let session = if is_session(d)? {
                    let open = session_open(d)?;
                    if submitted_at.lt(&open) {
                        return Ok(open);
                    }
                    next_session(d)?
                } else {
                    roll_next(d)?
                };
                session_open(session)
            }
            VenueCalendar::Globex => {
                let t_utc = submitted_at.to_utc_chrono()?;
                let cal = gcal(instr)?;
                let td = cal.session_or_next(t_utc).map_err(gerr)?;
                let open_utc = cal.session_open(td).map_err(gerr)?;
                let open = Ts::utc(open_utc);
                if submitted_at.lt(&open) {
                    return Ok(open);
                }
                let next_td = cal.next_session(td).map_err(gerr)?;
                let next_open_utc = cal.session_open(next_td).map_err(gerr)?;
                Ok(Ts::utc(next_open_utc))
            }
        }
    }

    fn is_regular_session_bar(&self, bar: &Bar) -> R<bool> {
        match self.venue {
            VenueCalendar::Xnys => {
                let d = bar.ts.ny_date()?;
                if !session_open(d)?.le(&bar.ts) {
                    return Ok(false);
                }
                Ok(bar.ts.lt(&session_close(d)?))
            }
            VenueCalendar::Globex => {
                let t_utc = bar.ts.to_utc_chrono()?;
                gcal(&bar.instr)?.is_open_at(t_utc).map_err(gerr)
            }
        }
    }

    fn oco_priority(&self, i: usize) -> Prio {
        let w = &self.orders[i];
        if w.order.otype == OrderType::Stop {
            return (false, false, (0, String::new()), w.order.id.clone());
        }
        let soid = &w.order.allocs[0].soid;
        let (prefix, sep, suffix) = rpartition(soid, ":target:");
        if sep && !prefix.is_empty() && ascii_decimal(suffix) {
            return (true, false, int_key(suffix), soid.clone());
        }
        (true, true, (0, String::new()), w.order.id.clone())
    }

    fn check_bar_sequence(&self, bar: &Bar) -> R<()> {
        match self.venue {
            VenueCalendar::Xnys => self.check_bar_sequence_equity(bar),
            VenueCalendar::Globex => self.check_bar_sequence_futures(bar),
        }
    }

    fn check_bar_sequence_equity(&self, bar: &Bar) -> R<()> {
        let sym = bar.instr.symbol()?;
        let previous = self.last_bars.get(&bar.instr.hk());
        let bar_date = bar.ts.ny_date()?;
        if !is_session(bar_date)? {
            return err("missing_bar", format!("Bar for {sym} is on non-session date {}", date_str(&bar_date)));
        }
        let open = session_open(bar_date)?;
        let opening = || {
            err("missing_bar", format!("Missing session opening one-minute bar for {sym} at {}", open.iso))
        };
        let Some(previous) = previous else {
            if !bar.ts.eq(&open) {
                return opening();
            }
            return Ok(());
        };
        if bar.ts.eq(&previous.ts) {
            return Ok(());
        }
        if bar.ts.lt(&previous.ts) {
            return err("value", format!("Out-of-order bar for {sym}: {} follows {}", bar.ts.iso, previous.ts.iso));
        }
        let previous_date = previous.ts.ny_date()?;
        if bar_date == previous_date {
            let (a, au) = bar.ts.key();
            let (b, bu) = previous.ts.key();
            if !(a - b == 60 && au == bu) {
                return err(
                    "missing_bar",
                    format!("Missing one-minute bar for {sym} between {} and {}", previous.ts.iso, bar.ts.iso),
                );
            }
            return Ok(());
        }
        let last_regular = session_close(previous_date)?.add_seconds(-60)?;
        if previous.ts.lt(&last_regular) {
            return err(
                "missing_bar",
                format!(
                    "Missing closing one-minute bars for {sym}: session {} ended at {}, expected {}",
                    date_str(&previous_date),
                    previous.ts.iso,
                    last_regular.iso
                ),
            );
        }
        let next = next_session(previous_date)?;
        if bar_date != next {
            return err(
                "missing_bar",
                format!("Missing session bars for {sym}: expected {}, received {}", date_str(&next), date_str(&bar_date)),
            );
        }
        if !bar.ts.eq(&open) {
            return opening();
        }
        Ok(())
    }

    fn check_bar_sequence_futures(&self, bar: &Bar) -> R<()> {
        let sym = bar.instr.symbol()?;
        let t_utc = bar.ts.to_utc_chrono()?;
        match gcal(&bar.instr)?.session_at(t_utc) {
            Ok(Some(_)) => {}
            Ok(None) => {
                return err(
                    "missing_bar",
                    format!("Bar for {sym} at {} is not inside a Globex session (halt, weekend or closure)", bar.ts.iso),
                );
            }
            Err(e) => return Err(gerr(e)),
        };
        let previous = self.last_bars.get(&bar.instr.hk());
        let Some(previous) = previous else {
            // First bar inside a Globex session is accepted (§0.4)
            return Ok(());
        };
        if bar.ts.eq(&previous.ts) {
            return Ok(());
        }
        if bar.ts.lt(&previous.ts) {
            return err("value", format!("Out-of-order bar for {sym}: {} follows {}", bar.ts.iso, previous.ts.iso));
        }
        // In-session gaps are accepted (§0.4), and so is a jump to any later
        // session: a whole session missing from the data (a feed hole, or a
        // CME closure the published table could not source, e.g. Good Friday
        // 2023) is the data audit's concern, not the book's. Working orders
        // wait through it; Day orders still expire by instant in expire_due.
        Ok(())
    }

    fn validate(&self, order: &VOrder) -> R<()> {
        match self.venue {
            VenueCalendar::Xnys => {
                if !matches!(order.instr, Instrument::Equity(_)) {
                    return err("value", "SimBroker accepts equity orders only");
                }
            }
            VenueCalendar::Globex => {
                if !matches!(order.instr, Instrument::Future(_)) {
                    return err("value", "SimBroker accepts futures orders only");
                }
            }
        }
        if !matches!(order.otype, OrderType::Market | OrderType::Limit | OrderType::Stop | OrderType::StopLimit) {
            return err("value", format!("Unsupported order type {}", order.otype.value()));
        }
        if !matches!(order.tif, Tif::Day | Tif::Gtc | Tif::Opg) {
            return err("value", format!("Unsupported time in force {}", order.tif.value()));
        }
        if order.tif == Tif::Opg && order.otype != OrderType::Market {
            return err("value", "OPG is supported only for market-on-open orders");
        }
        if order.allocs.iter().any(|a| a.account != self.account_id) {
            return err("value", "Venue order allocation account does not match SimBroker account");
        }
        if order.allocs.len() != 1 {
            return err("value", "SimBroker requires one-to-one strategy-order allocations");
        }
        if let Some(parent) = &order.parent {
            let Some(&p) = self.index.get(parent) else {
                return err("value", format!("Parent order '{parent}' is not held by this SimBroker"));
            };
            let p = &self.orders[p].order;
            if !p.instr.same(&order.instr) || p.side == order.side {
                return err(
                    "value",
                    format!("Exit '{}' must close parent '{parent}' in the same instrument", order.id),
                );
            }
        }
        Ok(())
    }

    fn require_connected(&self) -> R<()> {
        if !self.connected {
            return err("sim", "Call connect() before using SimBroker");
        }
        Ok(())
    }

    fn require_order(&self, id: &str) -> R<usize> {
        match self.index.get(id) {
            Some(&i) => Ok(i),
            None => err("unknown_order", format!("Unknown SimBroker order '{id}'")),
        }
    }

    fn event_time(&self, clock: &mut Clock<'_>) -> R<Ts> {
        let at = now(clock)?;
        let mut latest: Option<&Ts> = None;
        for (_, b) in &self.last_bars.items {
            latest = Some(match latest {
                None => &b.ts,
                Some(m) => ts_max(m, &b.ts),
            });
        }
        let latest = latest.cloned().unwrap_or_else(|| at.clone());
        Ok(ts_max(&at, &latest).clone())
    }

    fn ack(&self, id: &str, status: &'static str, msg: Option<String>, clock: &mut Clock<'_>) -> R<Ack> {
        Ok(Ack { id: id.to_string(), status, ts: self.event_time(clock)?, msg })
    }

    /// The order's state, for a host that must tell whether a call changed it.
    pub fn state_of(&self, id: &str) -> Option<OrderState> {
        self.index.get(id).map(|&i| self.orders[i].state)
    }
}

/// `_day_session`: the session a DAY order submitted at this instant works in.
pub(super) fn day_session(submitted_at: &Ts) -> R<NaiveDate> {
    let d = submitted_at.ny_date()?;
    if is_session(d)? {
        if submitted_at.lt(&session_close(d)?) {
            return Ok(d);
        }
        return next_session(d);
    }
    roll_next(d)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn ts(s: &str) -> Ts {
        Ts::aware(s, "t").unwrap()
    }

    fn order(id: &str, otype: OrderType, side: Side, q: &str, at: &str) -> VOrder {
        VOrder {
            id: id.into(),
            instr: Instrument::Equity("SPY".into()),
            otype,
            side,
            quantity: dec(q),
            submitted_at: ts(at),
            tif: Tif::Day,
            limit: None,
            stop: None,
            trail: None,
            allocs: vec![Alloc { soid: format!("{id}-s"), account: "A".into(), qty: dec(q) }],
            parent: None,
            oco: None,
        }
    }

    fn bar(at: &str, o: &str, h: &str, l: &str, c: &str) -> Bar {
        Bar {
            instr: Instrument::Equity("SPY".into()),
            ts: ts(at),
            open: dec(o),
            high: dec(h),
            low: dec(l),
            close: dec(c),
            volume: dec("100"),
            as_of: ts(at),
        }
    }

    #[test]
    fn market_fills_at_open_with_slippage() {
        let mut b = Book::new("A", true, dec("10")).unwrap();
        let mut clock = || Ok("2026-03-02T14:00:00+00:00".to_string());
        b.connect(&mut clock).unwrap();
        b.submit(order("o1", OrderType::Market, Side::Buy, "10", "2026-03-02T14:00:00+00:00"), &mut clock).unwrap();
        let fills = b.process_bar(Some(bar("2026-03-02T14:30:00+00:00", "100", "101", "99", "100"))).unwrap();
        assert_eq!(fills.len(), 1);
        assert_eq!(b.fills[0].price.canon(), "100.1");
        assert!(b.process_bar(Some(bar("2026-03-02T14:31:00+00:00", "100", "101", "99", "100"))).unwrap().is_empty());
    }

    #[test]
    fn missing_open_bar_refuses() {
        let mut b = Book::new("A", true, dec("0")).unwrap();
        b.connect(&mut || Ok("2026-03-02T14:00:00+00:00".to_string())).unwrap();
        let e = b.process_bar(Some(bar("2026-03-02T14:31:00+00:00", "1", "1", "1", "1"))).unwrap_err();
        assert_eq!(e.kind, "missing_bar");
        assert_eq!(e.msg, "Missing session opening one-minute bar for SPY at 2026-03-02T14:30:00+00:00");
    }

    fn future_instr(sym: &str) -> Instrument {
        Instrument::Future(crate::sim::tick::parse_future_symbol(sym).unwrap())
    }

    fn f_order(id: &str, sym: &str, otype: OrderType, side: Side, q: &str, at: &str) -> VOrder {
        VOrder {
            id: id.into(),
            instr: future_instr(sym),
            otype,
            side,
            quantity: dec(q),
            submitted_at: ts(at),
            tif: Tif::Day,
            limit: None,
            stop: None,
            trail: None,
            allocs: vec![Alloc { soid: format!("{id}-s"), account: "A".into(), qty: dec(q) }],
            parent: None,
            oco: None,
        }
    }

    fn f_limit_order(id: &str, sym: &str, side: Side, q: &str, at: &str, limit: &str) -> VOrder {
        let mut o = f_order(id, sym, OrderType::Limit, side, q, at);
        o.limit = Some(dec(limit));
        o
    }

    fn f_bar(sym: &str, at: &str, o: &str, h: &str, l: &str, c: &str) -> Bar {
        Bar {
            instr: future_instr(sym),
            ts: ts(at),
            open: dec(o),
            high: dec(h),
            low: dec(l),
            close: dec(c),
            volume: dec("100"),
            as_of: ts(at),
        }
    }

    #[test]
    fn test_futures_bar_sequence_halts_and_weekends() {
        let mut b = Book::new_futures("A", 0).unwrap();
        let mut clock = || Ok("2020-11-20T21:00:00+00:00".to_string());
        b.connect(&mut clock).unwrap();

        // 1. Weekend transition: Friday 2020-11-20 close to Sunday 2020-11-22 open.
        // Friday 16:59 ET is 21:59 UTC (session 2020-11-20).
        b.process_bar(Some(f_bar("NQ", "2020-11-20T21:59:00+00:00", "11900", "11905", "11895", "11902"))).unwrap();
        // Sunday 18:00 ET is 23:00 UTC (session 2020-11-23 open).
        b.process_bar(Some(f_bar("NQ", "2020-11-22T23:00:00+00:00", "11910", "11915", "11905", "11912"))).unwrap();

        // 2. Daily halt transition: Monday 2020-11-23 close to Monday evening 18:00 ET (Tuesday session).
        // Monday 16:59 ET is 21:59 UTC (session 2020-11-23).
        b.process_bar(Some(f_bar("NQ", "2020-11-23T21:59:00+00:00", "11920", "11925", "11915", "11922"))).unwrap();
        // Monday 18:00 ET is 23:00 UTC (session 2020-11-24 open).
        b.process_bar(Some(f_bar("NQ", "2020-11-23T23:00:00+00:00", "11930", "11935", "11925", "11932"))).unwrap();
    }

    #[test]
    fn test_futures_bar_sequence_early_halt_and_closed_day() {
        let mut b = Book::new_futures("A", 0).unwrap();
        b.connect(&mut || Ok("2015-04-03T12:00:00+00:00".to_string())).unwrap();

        // 1. Good Friday 2015 (early halt at 09:15 ET = 13:15 UTC):
        // Bar at 09:14 ET is accepted.
        b.process_bar(Some(f_bar("NQ", "2015-04-03T13:14:00+00:00", "4400", "4405", "4395", "4402"))).unwrap();
        // Reopen Sunday 2015-04-05 at 18:00 ET (22:00 UTC) for session 2015-04-06 is accepted across early halt & weekend.
        b.process_bar(Some(f_bar("NQ", "2015-04-05T22:00:00+00:00", "4410", "4415", "4405", "4412"))).unwrap();

        // 2. Thanksgiving 2020 (Thursday 2020-11-26 early halt at 13:00 ET = 18:00 UTC,
        // Friday 2020-11-27 early halt at 13:15 ET = 18:15 UTC):
        let mut b_tg = Book::new_futures("A", 0).unwrap();
        b_tg.connect(&mut || Ok("2020-11-26T12:00:00+00:00".to_string())).unwrap();
        // Thanksgiving Thursday at 12:59 ET (17:59 UTC)
        b_tg.process_bar(Some(f_bar("NQ", "2020-11-26T17:59:00+00:00", "12150", "12155", "12145", "12152"))).unwrap();
        // Thursday evening 18:00 ET (23:00 UTC) opens Friday session
        b_tg.process_bar(Some(f_bar("NQ", "2020-11-26T23:00:00+00:00", "12160", "12165", "12155", "12162"))).unwrap();
        // Friday 13:14 ET (18:14 UTC)
        b_tg.process_bar(Some(f_bar("NQ", "2020-11-27T18:14:00+00:00", "12170", "12175", "12165", "12172"))).unwrap();
        // Reopen Sunday 2020-11-29 at 18:00 ET (23:00 UTC) for Monday 2020-11-30 session
        b_tg.process_bar(Some(f_bar("NQ", "2020-11-29T23:00:00+00:00", "12180", "12185", "12175", "12182"))).unwrap();

        // 3. Closed day (Christmas 2026):
        let mut b_xmas = Book::new_futures("A", 0).unwrap();
        b_xmas.connect(&mut || Ok("2026-12-24T12:00:00+00:00".to_string())).unwrap();
        // Thursday 2026-12-24 early halt at 13:15 ET (18:15 UTC); bar at 13:14 ET (18:14 UTC)
        b_xmas.process_bar(Some(f_bar("NQ", "2026-12-24T18:14:00+00:00", "22000", "22010", "21990", "22005"))).unwrap();
        // Friday 2026-12-25 is closed all day; next session opens Sunday 2026-12-27 at 18:00 ET (23:00 UTC)
        b_xmas.process_bar(Some(f_bar("NQ", "2026-12-27T23:00:00+00:00", "22020", "22030", "22010", "22025"))).unwrap();
    }

    #[test]
    fn test_futures_in_session_gaps_and_halt_refusals() {
        let mut b = Book::new_futures("A", 0).unwrap();
        b.connect(&mut || Ok("2020-11-22T23:00:00+00:00".to_string())).unwrap();

        // In-session bar
        b.process_bar(Some(f_bar("NQ", "2020-11-22T23:00:00+00:00", "11900", "11905", "11895", "11900"))).unwrap();
        // In-session gap: 15 minutes later, same session (accepted per §0.4)
        b.process_bar(Some(f_bar("NQ", "2020-11-22T23:15:00+00:00", "11905", "11910", "11900", "11908"))).unwrap();

        // Bar timestamped during daily halt (17:30 ET = 22:30 UTC on 2020-11-23)
        let mut b_halt = Book::new_futures("A", 0).unwrap();
        b_halt.connect(&mut || Ok("2020-11-23T22:30:00+00:00".to_string())).unwrap();
        let e = b_halt.process_bar(Some(f_bar("NQ", "2020-11-23T22:30:00+00:00", "11900", "11905", "11895", "11900"))).unwrap_err();
        assert_eq!(e.kind, "missing_bar");
        assert!(e.msg.contains("is not inside a Globex session (halt, weekend or closure)"));

        // Bar timestamped during weekend (Saturday 12:00 ET = 17:00 UTC on 2020-11-21)
        let mut b_wknd = Book::new_futures("A", 0).unwrap();
        b_wknd.connect(&mut || Ok("2020-11-21T17:00:00+00:00".to_string())).unwrap();
        let e = b_wknd.process_bar(Some(f_bar("NQ", "2020-11-21T17:00:00+00:00", "11900", "11905", "11895", "11900"))).unwrap_err();
        assert_eq!(e.kind, "missing_bar");
        assert!(e.msg.contains("is not inside a Globex session (halt, weekend or closure)"));

        // Bar timestamped during closed day (Christmas 2026-12-25 12:00 ET = 17:00 UTC)
        let mut b_closed = Book::new_futures("A", 0).unwrap();
        b_closed.connect(&mut || Ok("2026-12-25T17:00:00+00:00".to_string())).unwrap();
        let e = b_closed.process_bar(Some(f_bar("NQ", "2026-12-25T17:00:00+00:00", "22000", "22010", "21990", "22000"))).unwrap_err();
        assert_eq!(e.kind, "missing_bar");
        assert!(e.msg.contains("is not inside a Globex session (halt, weekend or closure)"));

        // Skipped session: Friday 2020-11-20 followed by Tuesday 2020-11-24 (Monday 2020-11-23
        // missing from the data) is accepted - whole-session data holes are the data audit's
        // concern (§0.4), and Good Friday 2023 is closed at CME but absent from the table.
        let mut b_skip = Book::new_futures("A", 0).unwrap();
        b_skip.connect(&mut || Ok("2020-11-20T21:00:00+00:00".to_string())).unwrap();
        b_skip.process_bar(Some(f_bar("NQ", "2020-11-20T21:59:00+00:00", "11900", "11905", "11895", "11900"))).unwrap();
        b_skip.process_bar(Some(f_bar("NQ", "2020-11-23T23:00:00+00:00", "11910", "11915", "11905", "11910"))).unwrap();
    }

    #[test]
    fn test_futures_day_expiry() {
        // 1. Normal session: Day order submitted during session expires at 17:00 ET (22:00 UTC).
        let mut b = Book::new_futures("A", 0).unwrap();
        let mut clock = || Ok("2020-11-23T15:00:00+00:00".to_string());
        b.connect(&mut clock).unwrap();
        b.submit(f_limit_order("o1", "NQ", Side::Buy, "1", "2020-11-23T15:00:00+00:00", "10000"), &mut clock).unwrap();
        assert_eq!(b.state_of("o1"), Some(OrderState::Accepted));
        // Bar at 16:59 ET (21:59 UTC); order does not fill
        b.process_bar(Some(f_bar("NQ", "2020-11-23T21:59:00+00:00", "11900", "11905", "11895", "11900"))).unwrap();
        assert_eq!(b.state_of("o1"), Some(OrderState::Accepted));
        // Advance clock to 17:00 ET (22:00 UTC); order expires
        b.orders_since("2020-11-23T00:00:00+00:00", &mut || Ok("2020-11-23T22:00:00+00:00".to_string())).unwrap();
        assert_eq!(b.state_of("o1"), Some(OrderState::Expired));

        // 2. Early halt: Thanksgiving Friday 2020-11-27 has early halt at 13:15 ET (18:15 UTC).
        let mut b_early = Book::new_futures("A", 0).unwrap();
        let mut clock_early = || Ok("2020-11-27T15:00:00+00:00".to_string());
        b_early.connect(&mut clock_early).unwrap();
        b_early.submit(f_limit_order("o2", "NQ", Side::Buy, "1", "2020-11-27T15:00:00+00:00", "10000"), &mut clock_early).unwrap();
        // Bar at 13:14 ET (18:14 UTC)
        b_early.process_bar(Some(f_bar("NQ", "2020-11-27T18:14:00+00:00", "12170", "12175", "12165", "12170"))).unwrap();
        assert_eq!(b_early.state_of("o2"), Some(OrderState::Accepted));
        // Advance clock to 13:15 ET (18:15 UTC); order expires at early halt
        b_early.orders_since("2020-11-27T00:00:00+00:00", &mut || Ok("2020-11-27T18:15:00+00:00".to_string())).unwrap();
        assert_eq!(b_early.state_of("o2"), Some(OrderState::Expired));

        // 3. Submitted during daily halt: Wednesday 2020-11-25 17:30 ET (22:30 UTC).
        // Belongs to NEXT session (Thanksgiving 2020-11-26, early halt at 13:00 ET = 18:00 UTC).
        let mut b_halt = Book::new_futures("A", 0).unwrap();
        let mut clock_halt = || Ok("2020-11-25T22:30:00+00:00".to_string());
        b_halt.connect(&mut clock_halt).unwrap();
        b_halt.submit(f_limit_order("o3", "NQ", Side::Buy, "1", "2020-11-25T22:30:00+00:00", "10000"), &mut clock_halt).unwrap();
        // Bar at 12:59 ET (17:59 UTC) on 2020-11-26
        b_halt.process_bar(Some(f_bar("NQ", "2020-11-26T17:59:00+00:00", "12150", "12155", "12145", "12150"))).unwrap();
        assert_eq!(b_halt.state_of("o3"), Some(OrderState::Accepted));
        // Advance clock to 13:00 ET (18:00 UTC) on 2020-11-26; expires at Thanksgiving early halt
        b_halt.orders_since("2020-11-25T00:00:00+00:00", &mut || Ok("2020-11-26T18:00:00+00:00".to_string())).unwrap();
        assert_eq!(b_halt.state_of("o3"), Some(OrderState::Expired));

        // 4. Submitted during weekend: Saturday 2020-11-21 12:00 ET (17:00 UTC).
        // Belongs to NEXT session (Monday 2020-11-23, closes 17:00 ET = 22:00 UTC).
        let mut b_wknd = Book::new_futures("A", 0).unwrap();
        let mut clock_wknd = || Ok("2020-11-21T17:00:00+00:00".to_string());
        b_wknd.connect(&mut clock_wknd).unwrap();
        b_wknd.submit(f_limit_order("o4", "NQ", Side::Buy, "1", "2020-11-21T17:00:00+00:00", "10000"), &mut clock_wknd).unwrap();
        // Bar at Monday 16:59 ET (21:59 UTC)
        b_wknd.process_bar(Some(f_bar("NQ", "2020-11-23T21:59:00+00:00", "11900", "11905", "11895", "11900"))).unwrap();
        assert_eq!(b_wknd.state_of("o4"), Some(OrderState::Accepted));
        // Advance clock to Monday 17:00 ET (22:00 UTC); expires
        b_wknd.orders_since("2020-11-21T00:00:00+00:00", &mut || Ok("2020-11-23T22:00:00+00:00".to_string())).unwrap();
        assert_eq!(b_wknd.state_of("o4"), Some(OrderState::Expired));
    }

    /// The calendar follows the instrument's root (P6C): on 2024-07-04 energy halts 13:30 ET, equity 13:00 ET.
    #[test]
    fn test_futures_calendar_follows_the_root() {
        // 13:15 ET = 17:15 UTC: inside the CL session, past the NQ halt.
        let mut cl = Book::new_futures("A", 0).unwrap();
        cl.connect(&mut || Ok("2024-07-04T14:00:00+00:00".to_string())).unwrap();
        cl.process_bar(Some(f_bar("CL", "2024-07-04T17:15:00+00:00", "80", "81", "79", "80"))).unwrap();
        let mut nq = Book::new_futures("A", 0).unwrap();
        nq.connect(&mut || Ok("2024-07-04T14:00:00+00:00".to_string())).unwrap();
        let e = nq.process_bar(Some(f_bar("NQ", "2024-07-04T17:15:00+00:00", "18000", "18010", "17990", "18000"))).unwrap_err();
        assert_eq!(e.kind, "missing_bar");
        // 13:30 ET = 17:30 UTC: the CL halt itself is outside the session.
        let mut cl2 = Book::new_futures("A", 0).unwrap();
        cl2.connect(&mut || Ok("2024-07-04T14:00:00+00:00".to_string())).unwrap();
        let e = cl2.process_bar(Some(f_bar("CL", "2024-07-04T17:30:00+00:00", "80", "81", "79", "80"))).unwrap_err();
        assert_eq!(e.kind, "missing_bar");
        // A CL Day order expires at the energy halt (13:30 ET = 17:30 UTC), not at 13:00.
        let mut b = Book::new_futures("A", 0).unwrap();
        let mut clock = || Ok("2024-07-04T14:00:00+00:00".to_string());
        b.connect(&mut clock).unwrap();
        b.submit(f_limit_order("c1", "CL", Side::Buy, "1", "2024-07-04T14:00:00+00:00", "10"), &mut clock).unwrap();
        b.process_bar(Some(f_bar("CL", "2024-07-04T17:29:00+00:00", "80", "81", "79", "80"))).unwrap();
        b.orders_since("2024-07-04T00:00:00+00:00", &mut || Ok("2024-07-04T17:29:00+00:00".to_string())).unwrap();
        assert_eq!(b.state_of("c1"), Some(OrderState::Accepted));
        b.orders_since("2024-07-04T00:00:00+00:00", &mut || Ok("2024-07-04T17:30:00+00:00".to_string())).unwrap();
        assert_eq!(b.state_of("c1"), Some(OrderState::Expired));
    }

    #[test]
    fn test_futures_tick_slippage() {
        // NQ tick size is 0.25. 2 ticks slippage = 0.50.
        let mut b = Book::new_futures("A", 2).unwrap();
        let mut clock = || Ok("2020-11-22T22:00:00+00:00".to_string());
        b.connect(&mut clock).unwrap();

        // 1. Buy market order slips UP by 2 ticks (0.50)
        b.submit(f_order("buy_mkt", "NQ", OrderType::Market, Side::Buy, "1", "2020-11-22T22:00:00+00:00"), &mut clock).unwrap();
        b.process_bar(Some(f_bar("NQ", "2020-11-22T23:00:00+00:00", "20000.00", "20010.00", "19990.00", "20005.00"))).unwrap();
        assert_eq!(b.fills[0].price.canon(), "20000.5");

        // 2. Sell market order slips DOWN by 2 ticks (0.50)
        b.submit(f_order("sell_mkt", "NQ", OrderType::Market, Side::Sell, "1", "2020-11-22T23:01:00+00:00"), &mut clock).unwrap();
        b.process_bar(Some(f_bar("NQ", "2020-11-22T23:02:00+00:00", "20000.00", "20010.00", "19990.00", "20005.00"))).unwrap();
        assert_eq!(b.fills[1].price.canon(), "19999.5");

        // 3. Zero slippage ticks: price is untouched
        let mut b_zero = Book::new_futures("A", 0).unwrap();
        b_zero.connect(&mut || Ok("2020-11-22T22:00:00+00:00".to_string())).unwrap();
        b_zero.submit(f_order("buy_zero", "NQ", OrderType::Market, Side::Buy, "1", "2020-11-22T22:00:00+00:00"), &mut clock).unwrap();
        b_zero.process_bar(Some(f_bar("NQ", "2020-11-22T23:00:00+00:00", "20000.00", "20010.00", "19990.00", "20005.00"))).unwrap();
        assert_eq!(b_zero.fills[0].price.canon(), "20000");

        // 4. Limit order with slippage_ticks = 2 does NOT slip (replay-sim.ts rule)
        let mut b_lim = Book::new_futures("A", 2).unwrap();
        b_lim.connect(&mut || Ok("2020-11-22T22:00:00+00:00".to_string())).unwrap();
        // Buy limit at 20000.00; bar opens at 19998.00 (better than limit); fills at 19998.00 without slippage
        b_lim.submit(f_limit_order("buy_lim1", "NQ", Side::Buy, "1", "2020-11-22T22:00:00+00:00", "20000.00"), &mut clock).unwrap();
        b_lim.process_bar(Some(f_bar("NQ", "2020-11-22T23:00:00+00:00", "19998.00", "20005.00", "19995.00", "20000.00"))).unwrap();
        assert_eq!(b_lim.fills[0].price.canon(), "19998");

        // Buy limit at 20000.00; bar opens at 20002.00, low reaches 19995.00; fills at 20000.00 without slippage
        b_lim.submit(f_limit_order("buy_lim2", "NQ", Side::Buy, "1", "2020-11-22T23:00:30+00:00", "20000.00"), &mut clock).unwrap();
        b_lim.process_bar(Some(f_bar("NQ", "2020-11-22T23:01:00+00:00", "20002.00", "20003.00", "19995.00", "19998.00"))).unwrap();
        assert_eq!(b_lim.fills[1].price.canon(), "20000");
    }

    #[test]
    fn test_futures_point_value_pnl() {
        // NQ point value is 20
        let mut b_nq = Book::new_futures("A", 0).unwrap();
        let mut clock = || Ok("2020-11-22T22:00:00+00:00".to_string());
        b_nq.connect(&mut clock).unwrap();
        b_nq.submit(f_order("nq_buy", "NQ", OrderType::Market, Side::Buy, "2", "2020-11-22T22:00:00+00:00"), &mut clock).unwrap();
        b_nq.process_bar(Some(f_bar("NQ", "2020-11-22T23:00:00+00:00", "20000.00", "20010.00", "19990.00", "20005.00"))).unwrap();

        let pos_nq = &b_nq.positions(&mut clock).unwrap()[0];
        assert_eq!(pos_nq.point_value().canon(), "20");
        // Long 2 contracts @ 20000.00, mark @ 20010.00: PnL = (20010 - 20000) * 20 * 2 = 400.00
        let pnl_nq = b_nq.position_pnl("NQ", &dec("20010.00")).unwrap().unwrap();
        assert_eq!(pnl_nq.canon(), "400");

        // MNQ point value is 2
        let mut b_mnq = Book::new_futures("A", 0).unwrap();
        b_mnq.connect(&mut clock).unwrap();
        b_mnq.submit(f_order("mnq_sell", "MNQ", OrderType::Market, Side::Sell, "5", "2020-11-22T22:00:00+00:00"), &mut clock).unwrap();
        b_mnq.process_bar(Some(f_bar("MNQ", "2020-11-22T23:00:00+00:00", "20000.00", "20010.00", "19990.00", "20005.00"))).unwrap();

        let pos_mnq = &b_mnq.positions(&mut clock).unwrap()[0];
        assert_eq!(pos_mnq.point_value().canon(), "2");
        // Short 5 contracts @ 20000.00, mark @ 19950.00: PnL = (19950 - 20000) * 2 * (-5) = 500.00
        let pnl_mnq = b_mnq.position_pnl("MNQ", &dec("19950.00")).unwrap().unwrap();
        assert_eq!(pnl_mnq.canon(), "500");

        // Equity point value is 1
        let mut b_eq = Book::new("A", true, dec("0")).unwrap();
        b_eq.connect(&mut || Ok("2026-03-02T14:00:00+00:00".to_string())).unwrap();
        b_eq.submit(order("spy_buy", OrderType::Market, Side::Buy, "10", "2026-03-02T14:00:00+00:00"), &mut clock).unwrap();
        b_eq.process_bar(Some(bar("2026-03-02T14:30:00+00:00", "100.00", "101.00", "99.00", "100.00"))).unwrap();

        let pos_eq = &b_eq.positions(&mut clock).unwrap()[0];
        assert_eq!(pos_eq.point_value().canon(), "1");
        // Long 10 shares @ 100.00, mark @ 105.00: PnL = (105 - 100) * 1 * 10 = 50.00
        let pnl_eq = b_eq.position_pnl("SPY", &dec("105.00")).unwrap().unwrap();
        assert_eq!(pnl_eq.canon(), "50");
    }

    #[test]
    fn test_mixed_venue_refusal() {
        let mut clock = || Ok("2020-11-22T22:00:00+00:00".to_string());

        // Globex book refuses equity order
        let mut b_fut = Book::new_futures("A", 0).unwrap();
        b_fut.connect(&mut clock).unwrap();
        let e = b_fut.submit(order("eq_in_fut", OrderType::Market, Side::Buy, "10", "2020-11-22T22:00:00+00:00"), &mut clock).unwrap_err();
        assert_eq!(e.kind, "value");
        assert_eq!(e.msg, "SimBroker accepts futures orders only");

        // Globex book refuses option order
        let opt_contract = crate::ledger::model::OptionContract {
            underlying: "SPY".into(),
            expiry: NaiveDate::from_ymd_opt(2026, 3, 20).unwrap(),
            right: crate::options::Right::Call,
            strike: dec("500"),
            multiplier: 100,
        };
        let opt_ord = VOrder {
            id: "opt_in_fut".into(),
            instr: Instrument::Option(opt_contract),
            otype: OrderType::Market,
            side: Side::Buy,
            quantity: dec("1"),
            submitted_at: ts("2020-11-22T22:00:00+00:00"),
            tif: Tif::Day,
            limit: None,
            stop: None,
            trail: None,
            allocs: vec![Alloc { soid: "opt_in_fut-s".into(), account: "A".into(), qty: dec("1") }],
            parent: None,
            oco: None,
        };
        let e = b_fut.submit(opt_ord, &mut clock).unwrap_err();
        assert_eq!(e.kind, "value");
        assert_eq!(e.msg, "SimBroker accepts futures orders only");

        // Globex book refuses equity bar
        let e = b_fut.process_bar(Some(bar("2020-11-22T23:00:00+00:00", "100", "101", "99", "100"))).unwrap_err();
        assert_eq!(e.kind, "value");
        assert_eq!(e.msg, "SimBroker supports futures only");

        // Equity book refuses futures order
        let mut b_eq = Book::new("A", true, dec("0")).unwrap();
        b_eq.connect(&mut || Ok("2026-03-02T14:00:00+00:00".to_string())).unwrap();
        let e = b_eq.submit(f_order("fut_in_eq", "NQ", OrderType::Market, Side::Buy, "1", "2026-03-02T14:00:00+00:00"), &mut clock).unwrap_err();
        assert_eq!(e.kind, "value");
        assert_eq!(e.msg, "SimBroker accepts equity orders only");

        // Equity book refuses futures bar
        let e = b_eq.process_bar(Some(f_bar("NQ", "2026-03-02T14:30:00+00:00", "20000", "20010", "19990", "20000"))).unwrap_err();
        assert_eq!(e.kind, "value");
        assert_eq!(e.msg, "SimBroker supports equities only");
    }
}

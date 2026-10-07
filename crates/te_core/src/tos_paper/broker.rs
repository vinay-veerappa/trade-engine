//! The `TosPaperBroker` state machine (was the decision half of `tos_paper/broker.py`): the
//! submit queue, the keys, the expected book, the sent tickets and their proven Order IDs,
//! the cancelled set and the sticky halt, and every transition that moves them.
//!
//! **Callbacks, not a step protocol.** The host answers [`Host`] calls (the clock and the
//! transport's reads, sends and cancels); this module decides what to call next, in the
//! order the Python called it, so a host exception, a clock tick and a transport call land
//! exactly where they did. The host is a sequencer: it holds no decision. Nothing here
//! reads a clock (I7): `now` is a host call, made where Python made it.
//!
//! The data crosses as JSON (D6: money is a Decimal carried as text). Every normalization,
//! reconcile and netting decision is the T1-T8 code, called, not copied.
//!
//! Exceptions: a transport call that raised an `Exception` is a [`Raised`] the broker may
//! classify (a refused send, a replay, an unavailable venue); anything the Python did not
//! catch crosses back as an [`LErr`] and the host re-raises its own object.

use std::collections::{BTreeSet, HashMap};

use super::netting::{self, ticket_json, Ticket};
use super::reconcile::{self, confirm_ticket, position_book, unreadable, venue_reconcile, Book, Row};
use super::wire::{
    dec_of, jstr, linstr, linstr_json, obj, opt_dec, order_type_of, req, req_arr, req_dec, req_str, side_of, tif_of,
    wire,
};
use super::{normalize, transport, BROKER, RAISED, UNSUPPORTED, VALUE, VENUE_UNREADABLE};
use crate::ledger::bridge;
use crate::ledger::json::{self, Json};
use crate::ledger::mirror::{self as fold, MirrorState};
use crate::ledger::model::{err, opt_dec_eq, parse_datetime, Instrument, LErr, MirrorQueued, OrderState, OrderType, R};
use crate::ledger::ops::{add, eq, gt, le, lt, mul_i, ne, s, zero, OMap};
use crate::ledger::pydec::PyDec;

/// `PREFLIGHT_MAX_AGE`: a preflight read older than this is not used by the drain (microseconds).
const PREFLIGHT_MAX_AGE_US: i128 = 30_000_000;

/// What a transport exception is to the broker: `TransportRefused`, `TransportReplay`,
/// `TransportUnavailable`, or anything else.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Class {
    Refused,
    Replay,
    Unavailable,
    Other,
}

/// An `Exception` the host's transport raised: its class, its type name and `str(exc)`.
#[derive(Debug, Clone)]
pub struct Raised {
    pub class: Class,
    pub name: String,
    pub text: String,
}

/// Why a host call or a normalization did not answer. `Raised` and `Refusal` are what the
/// Python's `except Exception` catches; `Fatal` (a `BaseException` in the host, a bug) never is.
#[derive(Debug, Clone)]
pub enum Fault {
    Raised(Raised),
    Refusal(LErr),
    Fatal(LErr),
}

impl From<LErr> for Fault {
    fn from(e: LErr) -> Fault {
        if e.kind == "host" {
            Fault::Fatal(e)
        } else {
            Fault::Refusal(e)
        }
    }
}

impl Fault {
    /// `str(exc)`.
    fn text(&self) -> String {
        match self {
            Fault::Raised(r) => r.text.clone(),
            Fault::Refusal(e) | Fault::Fatal(e) => e.msg.clone(),
        }
    }

    fn unavailable(&self) -> bool {
        matches!(self, Fault::Raised(r) if r.class == Class::Unavailable)
    }

    /// The refusal the exception crosses back as when the Python let it propagate.
    pub fn into_lerr(self) -> LErr {
        match self {
            Fault::Raised(r) => LErr { kind: RAISED, msg: r.text },
            Fault::Refusal(e) | Fault::Fatal(e) => e,
        }
    }
}

type X<T> = Result<T, Fault>;

/// What the host does for the broker. Rows cross as JSON (the transport's own dicts).
pub trait Host {
    /// `clock.now_utc().isoformat()`.
    fn now(&mut self) -> X<String>;
    /// `transport.place_order(spec, key)`: the raw result.
    fn place_order(&mut self, spec: &Json, key: &str) -> X<Json>;
    fn read_positions(&mut self) -> X<Vec<Json>>;
    fn read_working_orders(&mut self) -> X<Vec<Json>>;
    fn read_order_fills(&mut self) -> X<Vec<Json>>;
    fn cancel_order(&mut self, order_id: &str) -> X<Json>;
    /// `isinstance(transport, OrderFillReader)`.
    fn can_read_fills(&mut self) -> X<bool>;
    /// `isinstance(transport, OrderCanceller)`.
    fn can_cancel(&mut self) -> X<bool>;
}

fn broker<T>(msg: impl Into<String>) -> R<T> {
    err(BROKER, msg)
}

/// `VenueAck`: the constructor's refusal for a naive instant.
struct Ack {
    id: String,
    status: String,
    at: String,
    message: String,
}

impl Ack {
    fn new(id: &str, status: &str, at: &str, message: String) -> R<Ack> {
        let (_, aware) = parse_datetime(at)?;
        if !aware {
            return err(VALUE, "timestamp must be timezone-aware UTC datetime (I7)");
        }
        Ok(Ack { id: id.to_string(), status: status.to_string(), at: at.to_string(), message })
    }

    /// An ack the normalizer built (`{"status", "message"}`), stamped here.
    fn of(doc: &Json, id: &str, at: &str) -> R<Ack> {
        Ack::new(id, req_str(doc, "status")?, at, req_str(doc, "message")?.to_string())
    }

    fn json(&self) -> Json {
        obj(vec![
            ("venue_order_id", jstr(self.id.clone())),
            ("status", jstr(self.status.clone())),
            ("at", jstr(self.at.clone())),
            ("message", jstr(self.message.clone())),
        ])
    }
}

/// One ticket a send proved a venue Order ID for: the ticket, the id, the units still resting.
#[derive(Clone)]
struct Sent {
    ticket: Ticket,
    order_id: String,
    resting: PyDec,
}

/// `normalize.OrderFill`.
struct Fill {
    order_id: String,
    filled: PyDec,
    avg_price: Option<PyDec>,
    state: OrderState,
}

/// The broker's decision state.
pub struct Broker {
    venue: String,
    mirrored: Vec<String>,
    connected: bool,
    halted: bool,
    restored: bool,
    queue: Vec<Ticket>,
    keys: BTreeSet<String>,
    expected: Book,
    sent: OMap<String, Sent>,
    proven: OMap<String, (String, String)>,
    preflight_book: Option<(String, Book)>,
    cancelled: BTreeSet<String>,
}

fn contracts_of(ticket: &Ticket) -> reconcile::Ticket {
    reconcile::Ticket {
        instrument: ticket.instrument.clone(),
        order_type: ticket.order_type,
        side: ticket.side,
        quantity: ticket.quantity.clone(),
        limit_price: ticket.limit_price.clone(),
    }
}

/// `_add`: the ticket's signed contracts (`units`, default all) into `expected`, times `sign`.
fn add_ticket(expected: &mut Book, ticket: &Ticket, sign: i128, units: Option<&PyDec>) -> R<()> {
    for (contract, quantity) in reconcile::ticket_contracts(&contracts_of(ticket), units)?.iter() {
        let current = reconcile::get(expected, contract);
        let total = add(&current, &mul_i(quantity, sign)?)?;
        expected.insert(contract.hk(), contract.clone(), total);
    }
    Ok(())
}

/// `venue_order_of`: the venue order a `MirrorQueued` recorded, rebuilt exactly (and the
/// `VenueOrder` constructor's refusals).
pub fn venue_order_of(q: &MirrorQueued) -> R<Ticket> {
    if q.ticket_key.is_empty() {
        return err(VALUE, "venue_order_id must be non-empty");
    }
    if le(&q.quantity, &zero())? {
        return err(VALUE, format!("quantity must be positive, got {}", s(&q.quantity)));
    }
    match q.order_type {
        OrderType::Market if q.limit_price.is_some() => return err(VALUE, "MARKET order cannot have a limit_price"),
        OrderType::Limit if q.limit_price.is_none() => return err(VALUE, "LIMIT order must have a limit_price"),
        _ => {}
    }
    if q.allocations.is_empty() {
        return err(VALUE, "VenueOrder must carry at least one strategy-order allocation");
    }
    let mut allocated = zero();
    for a in &q.allocations {
        allocated = add(&allocated, &a.quantity)?;
    }
    if ne(&allocated, &q.quantity)? {
        return err(
            VALUE,
            format!("VenueOrder allocations total {} but order quantity is {}", s(&allocated), s(&q.quantity)),
        );
    }
    Ok(Ticket {
        venue_order_id: q.ticket_key.clone(),
        instrument: q.instrument.clone(),
        order_type: q.order_type,
        side: q.side,
        quantity: q.quantity.clone(),
        submitted_at: q.at.iso(),
        tif: q.tif,
        limit_price: q.limit_price.clone(),
        allocations: q
            .allocations
            .iter()
            .map(|a| (a.strategy_order_id.clone(), a.strategy_account.clone(), a.quantity.clone()))
            .collect(),
    })
}

/// A ticket document (`netting::ticket_json`) read back.
pub fn ticket_of(j: &Json) -> R<Ticket> {
    let mut allocations = Vec::new();
    for a in req_arr(j, "allocations")? {
        let Json::Arr(p) = a else { return wire("an allocation is not a triple") };
        let [Json::Str(o), Json::Str(acct), Json::Str(q)] = p.as_slice() else {
            return wire("an allocation is not a triple");
        };
        allocations.push((o.clone(), acct.clone(), dec_of(q)?));
    }
    Ok(Ticket {
        venue_order_id: req_str(j, "venue_order_id")?.to_string(),
        instrument: linstr(req(j, "instrument")?)?,
        order_type: order_type_of(req_str(j, "order_type")?)?,
        side: side_of(req_str(j, "side")?)?,
        quantity: req_dec(j, "quantity")?,
        submitted_at: req_str(j, "submitted_at")?.to_string(),
        tif: tif_of(req_str(j, "tif")?)?,
        limit_price: opt_dec(j, "limit_price")?,
        allocations,
    })
}

/// The document `transport::ticket_for` reads for a venue order.
fn ticket_for_doc(t: &Ticket) -> Json {
    obj(vec![
        ("instrument", linstr_json(&t.instrument)),
        ("order_type", jstr(t.order_type.value())),
        ("tif", jstr(t.tif.value())),
        ("quantity", jstr(s(&t.quantity))),
        ("side", jstr(t.side.value())),
        ("limit_price", super::wire::jopt_dec(&t.limit_price)),
    ])
}

fn raw_doc(raw: &Json) -> Json {
    obj(vec![("raw", raw.clone())])
}

fn exc_doc(r: &Raised) -> Json {
    let class = match r.class {
        Class::Refused => "refused",
        Class::Replay => "replay",
        Class::Unavailable | Class::Other => "other",
    };
    obj(vec![("exc", obj(vec![("class", jstr(class)), ("type", jstr(r.name.clone())), ("text", jstr(r.text.clone()))]))])
}

fn reconciled(event: &Json) -> bool {
    matches!(event.get("reconciled"), Some(Json::Bool(true)))
}

fn pairs_of_book(book: &Book) -> Json {
    reconcile::pairs_json(book)
}

fn microseconds(iso: &str) -> R<i128> {
    let (dt, _) = parse_datetime(iso)?;
    let (secs, us) = dt.instant();
    Ok(secs as i128 * 1_000_000 + us as i128)
}

impl Broker {
    pub fn new(venue: &str, mirrored: Vec<String>, halted: bool) -> Broker {
        Broker {
            venue: venue.to_string(),
            mirrored,
            connected: false,
            halted,
            restored: false,
            queue: Vec::new(),
            keys: BTreeSet::new(),
            expected: Book::new(),
            sent: OMap::new(),
            proven: OMap::new(),
            preflight_book: None,
            cancelled: BTreeSet::new(),
        }
    }

    /// The host proved the venue (`connect`): the identity, type and balance checks stay host-side.
    pub fn mark_connected(&mut self) {
        self.connected = true;
    }

    pub fn halted(&self) -> bool {
        self.halted
    }

    fn require_connected(&self, what: &str) -> R<()> {
        if self.connected {
            Ok(())
        } else {
            broker(format!("{what} before connect; prove the venue first (I10)"))
        }
    }

    // -- the host's reads, normalized ----------------------------------------------------------

    /// `_read_positions`: the clock first, then the transport; one normalized document per row.
    fn position_docs(&self, h: &mut dyn Host) -> X<Vec<Json>> {
        let now = h.now()?;
        let mut out = Vec::new();
        for row in h.read_positions()? {
            let doc = normalize::position(&raw_doc(&row))?;
            let (_, aware) = parse_datetime(&now)?;
            if !aware {
                return Err(LErr { kind: VALUE, msg: "as_of must be timezone-aware UTC datetime (I7)".into() }.into());
            }
            let Json::Obj(mut fields) = doc else { return Err(LErr { kind: super::WIRE, msg: "position doc".into() }.into()) };
            fields.push(("as_of".to_string(), jstr(now.clone())));
            out.push(Json::Obj(fields));
        }
        Ok(out)
    }

    fn read_positions(&self, h: &mut dyn Host) -> X<Vec<(Instrument, PyDec)>> {
        let mut out = Vec::new();
        for doc in self.position_docs(h)? {
            out.push((linstr(req(&doc, "instrument")?)?, req_dec(&doc, "quantity")?));
        }
        Ok(out)
    }

    fn read_working(&self, h: &mut dyn Host) -> X<Vec<Row>> {
        let mut out = Vec::new();
        for row in h.read_working_orders()? {
            out.push(reconcile::row_of(&normalize::working_order(&raw_doc(&row))?)?);
        }
        Ok(out)
    }

    fn read_fills(&self, h: &mut dyn Host) -> X<Vec<Fill>> {
        let mut out = Vec::new();
        for row in h.read_order_fills()? {
            let doc = normalize::order_fill(&raw_doc(&row))?;
            let state = match req_str(&doc, "state")? {
                text => OrderState::parse(text).map_or_else(|| wire(format!("bad order state {text:?}")), Ok)?,
            };
            out.push(Fill {
                order_id: req_str(&doc, "order_id")?.to_string(),
                filled: req_dec(&doc, "filled")?,
                avg_price: opt_dec(&doc, "avg_price")?,
                state,
            });
        }
        Ok(out)
    }

    fn ack(&self, h: &mut dyn Host, ticket: &Ticket, status: &str, message: String) -> X<Ack> {
        let now = h.now()?;
        Ok(Ack::new(&ticket.venue_order_id, status, &now, message)?)
    }

    /// `_unreadable`: the venue halts, after the event was built.
    fn unreadable_halt(&mut self, event: R<Json>) -> LErr {
        match event {
            Ok(event) => {
                self.halted = true;
                LErr { kind: VENUE_UNREADABLE, msg: json::dumps(&event) }
            }
            Err(e) => e,
        }
    }

    // -- the mirror layer ------------------------------------------------------------------------

    /// `mirror_batch`: net a batch and queue its tickets. Never calls the transport.
    pub fn mirror_batch(&mut self, h: &mut dyn Host, orders: &[Json], holdings: &[Json]) -> R<Json> {
        self.require_connected("mirror_batch")?;
        if self.halted {
            let mut refused = Vec::new();
            for o in orders {
                refused.push(Json::Arr(vec![
                    jstr(req_str(o, "order_id")?),
                    jstr(format!("venue {} is halted by a reconcile drift; refused", self.venue)),
                ]));
            }
            return Ok(obj(vec![("venue_orders", Json::Arr(Vec::new())), ("refused", Json::Arr(refused))]));
        }
        let parsed: Vec<netting::NOrder> = orders.iter().map(netting::norder_of).collect::<R<_>>()?;
        let at = h.now().map_err(Fault::into_lerr)?;
        let holdings = netting::holdings_of(holdings)?;
        let batch = netting::net_strategy_orders(&parsed, &self.venue, &self.mirrored, &at, &holdings)?;
        let mut expected: Book = if self.restored {
            self.expected.clone()
        } else {
            let mut book = Book::new();
            for ((_, instrument), quantity) in holdings.iter() {
                let current = reconcile::get(&book, instrument);
                book.insert(instrument.hk(), instrument.clone(), add(&current, quantity)?);
            }
            for queued in &self.queue {
                add_ticket(&mut book, queued, 1, None)?;
            }
            book
        };
        for ticket in &batch.venue_orders {
            if self.keys.contains(&ticket.venue_order_id) {
                continue;
            }
            self.keys.insert(ticket.venue_order_id.clone());
            self.queue.push(ticket.clone());
            add_ticket(&mut expected, ticket, 1, None)?;
        }
        self.expected = expected;
        Ok(obj(vec![
            ("venue_orders", Json::Arr(batch.venue_orders.iter().map(ticket_json).collect())),
            (
                "refused",
                Json::Arr(batch.refused.iter().map(|(o, r)| Json::Arr(vec![jstr(o.clone()), jstr(r.clone())])).collect()),
            ),
        ]))
    }

    /// `restore`: load the venue's folded mirror: the fold, not this process, is the memory (I2).
    pub fn restore(&mut self, mirror: &Json, halted_venues: &[String]) -> R<()> {
        let m: MirrorState = bridge::uncanon_mirror(mirror)?;
        if let Some(v) = &m.venue {
            if *v != self.venue {
                return broker(format!("cannot restore venue {v}'s mirror into {} (I8)", self.venue));
            }
        }
        if !self.queue.is_empty() {
            return broker("restore over an undrained queue; drain (or cancel) it first");
        }
        if halted_venues.contains(&self.venue) {
            self.halted = true;
        }
        let mut sent: OMap<String, Sent> = OMap::new();
        for ticket in m.open_tickets()? {
            if let Some(order_id) = &ticket.venue_order_id {
                let key = ticket.queued.ticket_key.clone();
                sent.insert(
                    key,
                    ticket.queued.ticket_key.clone(),
                    Sent {
                        ticket: venue_order_of(&ticket.queued)?,
                        order_id: order_id.clone(),
                        resting: ticket.remaining()?,
                    },
                );
            }
        }
        self.sent = sent;
        self.restored = true;
        for (key, _) in m.tickets.iter() {
            self.keys.insert(key.clone());
        }
        for (key, ticket) in m.tickets.iter() {
            if ticket.book_status == Some(OrderState::Cancelled) {
                self.cancelled.insert(key.clone());
            }
        }
        self.expected = m.expected()?;
        Ok(())
    }

    /// `collect_fills`: the venue's cumulative fills per Order ID as mirror increments (I3).
    pub fn collect_fills(&mut self, h: &mut dyn Host, mirror: &Json) -> R<Json> {
        self.require_connected("collect_fills")?;
        let m: MirrorState = bridge::uncanon_mirror(mirror)?;
        if let Some(v) = &m.venue {
            if *v != self.venue {
                return broker(format!("venue {v}'s mirror is not {}'s (I8)", self.venue));
            }
        }
        if !h.can_read_fills().map_err(Fault::into_lerr)? {
            return broker(
                "the transport cannot read order fills (OrderFillReader); refusing to guess the mirror book (I5)",
            );
        }
        let now = h.now().map_err(Fault::into_lerr)?;
        let mut sorted: Vec<(&String, &fold::MirrorTicketState)> = m.tickets.iter().collect();
        sorted.sort_by(|a, b| a.0.cmp(b.0));
        let tracked: Vec<&fold::MirrorTicketState> =
            sorted.into_iter().map(|(_, t)| t).filter(|t| t.venue_order_id.is_some()).collect();
        let mut contracts: Vec<Instrument> = Vec::new();
        for t in &tracked {
            for (c, _) in fold::ticket_contracts(&t.queued, &t.queued.quantity)?.iter() {
                contracts.push(c.clone());
            }
        }
        let rows = match self.read_fills(h) {
            Ok(rows) => rows,
            Err(f) if f.unavailable() => return Err(f.into_lerr()),
            Err(Fault::Fatal(e)) => return Err(e),
            Err(f) => {
                let why = format!("fill read-back failed: {}", f.text());
                return Err(self.unreadable_halt(unreadable(&self.venue, &now, &contracts, &why)));
            }
        };
        let mut by_id: HashMap<String, Fill> = HashMap::new();
        for row in rows {
            if by_id.contains_key(&row.order_id) {
                let why = format!("two fill rows for order {}", row.order_id);
                return Err(self.unreadable_halt(unreadable(&self.venue, &now, &contracts, &why)));
            }
            by_id.insert(row.order_id.clone(), row);
        }
        let mut fills: Vec<Json> = Vec::new();
        let mut closes: Vec<Json> = Vec::new();
        let mut contradicted: Vec<(&MirrorQueued, String)> = Vec::new();
        for ticket in tracked {
            let Some(oid) = &ticket.venue_order_id else { continue };
            let Some(row) = by_id.get(oid) else { continue };
            let quantity = &ticket.queued.quantity;
            if lt(&row.filled, &ticket.filled)? || gt(&row.filled, quantity)? {
                contradicted.push((
                    &ticket.queued,
                    format!(
                        "order {} reads filled {}; the mirror has {} of {}",
                        row.order_id,
                        s(&row.filled),
                        s(&ticket.filled),
                        s(quantity)
                    ),
                ));
                continue;
            }
            if row.state == OrderState::Filled && ne(&row.filled, quantity)? {
                contradicted.push((
                    &ticket.queued,
                    format!("order {} reads FILLED at {} of {}", row.order_id, s(&row.filled), s(quantity)),
                ));
                continue;
            }
            if eq(&row.filled, &ticket.filled)?
                && gt(&row.filled, &zero())?
                && !opt_dec_eq(&row.avg_price, &ticket.avg_price)
            {
                contradicted.push((
                    &ticket.queued,
                    format!(
                        "order {} reads filled {} at {}; the mirror booked it at {}",
                        row.order_id,
                        s(&row.filled),
                        row.avg_price.as_ref().map_or("None".to_string(), s),
                        ticket.avg_price.as_ref().map_or("None".to_string(), s)
                    ),
                ));
                continue;
            }
            let (_, aware) = parse_datetime(&now)?;
            if gt(&row.filled, &ticket.filled)? {
                if !aware {
                    return err("payload", "MirrorFill.at must be timezone-aware UTC datetime (I7)");
                }
                fills.push(obj(vec![
                    ("venue", jstr(self.venue.clone())),
                    ("ticket_key", jstr(ticket.queued.ticket_key.clone())),
                    ("venue_order_id", jstr(row.order_id.clone())),
                    ("filled", jstr(s(&row.filled))),
                    ("avg_price", super::wire::jopt_dec(&row.avg_price)),
                    ("at", jstr(now.clone())),
                ]));
            }
            let ended = matches!(
                row.state,
                OrderState::Filled | OrderState::Cancelled | OrderState::Expired | OrderState::Rejected
            );
            if ended && !ticket.closed && ticket.book_status != Some(row.state) {
                if !aware {
                    return err("payload", "MirrorAck.at must be timezone-aware UTC datetime (I7)");
                }
                closes.push(obj(vec![
                    ("venue", jstr(self.venue.clone())),
                    ("ticket_key", jstr(ticket.queued.ticket_key.clone())),
                    ("status", jstr(if row.state == OrderState::Rejected { "REJECTED" } else { "ACCEPTED" })),
                    (
                        "message",
                        jstr(format!(
                            "order book: order {} reads {}, filled {} of {}",
                            row.order_id,
                            row.state.value(),
                            s(&row.filled),
                            s(quantity)
                        )),
                    ),
                    ("at", jstr(now.clone())),
                    ("venue_order_id", jstr(row.order_id.clone())),
                    ("book_status", jstr(row.state.value())),
                ]));
            }
        }
        if !contradicted.is_empty() {
            let mut names: Vec<String> = Vec::new();
            for (queued, _) in &contradicted {
                for (c, _) in fold::ticket_contracts(queued, &queued.quantity)?.iter() {
                    let symbol = c.symbol()?;
                    if !names.contains(&symbol) {
                        names.push(symbol);
                    }
                }
            }
            names.sort();
            let whys: Vec<&str> = contradicted.iter().map(|(_, w)| w.as_str()).collect();
            let note = format!("venue fills contradict the mirror ({}); venue halted", whys.join("; "));
            let event = venue_reconcile(&self.venue, &now, false, names, Some(note));
            return Err(self.unreadable_halt(event));
        }
        Ok(obj(vec![("fills", Json::Arr(fills)), ("closes", Json::Arr(closes))]))
    }

    // -- the slow path ---------------------------------------------------------------------------

    /// `preflight`: read the venue's positions now, for the next drain.
    pub fn preflight(&mut self, h: &mut dyn Host) -> R<()> {
        self.require_connected("preflight")?;
        self.preflight_book = None;
        let attempt: X<(String, Book)> = (|| {
            let when = h.now()?;
            let book = position_book(&self.read_positions(h)?)?;
            Ok((when, book))
        })();
        match attempt {
            Ok(read) => {
                self.preflight_book = Some(read);
                Ok(())
            }
            Err(f) if f.unavailable() => Err(f.into_lerr()),
            Err(Fault::Fatal(e)) => Err(e),
            Err(_) => {
                self.preflight_book = None;
                Ok(())
            }
        }
    }

    /// `drain`: send queued tickets one at a time, each read back, then reconcile.
    pub fn drain(&mut self, h: &mut dyn Host) -> R<Json> {
        self.require_connected("drain")?;
        let taken = self.preflight_book.take();
        if self.queue.is_empty() {
            return Ok(obj(vec![
                ("acks", Json::Arr(Vec::new())),
                ("reconcile", Json::Null),
                ("proven", Json::Arr(Vec::new())),
            ]));
        }
        let tickets: Vec<Ticket> = std::mem::take(&mut self.queue);
        self.proven = OMap::new();
        let mut contracts: Vec<Instrument> = Vec::new();
        for t in &tickets {
            for (c, _) in reconcile::ticket_contracts(&contracts_of(t), None)?.iter() {
                contracts.push(c.clone());
            }
        }
        let mut acks: Vec<Ack> = Vec::new();
        let fresh = match &taken {
            Some((when, _)) => {
                let now = h.now().map_err(Fault::into_lerr)?;
                microseconds(&now)? - microseconds(when)? <= PREFLIGHT_MAX_AGE_US
            }
            None => false,
        };
        let before: X<Book> = match (&taken, fresh) {
            (Some((_, book)), true) => Ok(book.clone()),
            _ => self.read_positions(h).and_then(|p| Ok(position_book(&p)?)),
        };
        let mut before = match before {
            Ok(book) => book,
            Err(Fault::Fatal(e)) => return Err(e),
            Err(f) => {
                let text = f.text();
                for ticket in &tickets {
                    acks.push(
                        self.ack(h, ticket, "REJECTED", format!("cannot read the venue before sending: {text}"))
                            .map_err(Fault::into_lerr)?,
                    );
                    self.unexpect(ticket, None)?;
                }
                return self.finish(h, acks, &contracts, Some(format!("pre-send read failed: {text}")));
            }
        };
        let mut claimed: Vec<usize> = Vec::new();
        for ticket in &tickets {
            if self.halted {
                acks.push(
                    self.ack(h, ticket, "REJECTED", format!("venue {} is halted; not sent", self.venue))
                        .map_err(Fault::into_lerr)?,
                );
                self.unexpect(ticket, None)?;
                continue;
            }
            let sent = self.send(h, ticket).map_err(Fault::into_lerr)?;
            if sent.status == "REJECTED" {
                self.unexpect(ticket, None)?;
                acks.push(sent);
                continue;
            }
            let read = self.read_positions(h).and_then(|p| Ok((p, self.read_working(h)?)));
            let (positions, working) = match read {
                Ok(read) => read,
                Err(Fault::Fatal(e)) => return Err(e),
                Err(f) => {
                    let message = format!("{}; read-back failed: {}", sent.message, f.text());
                    acks.push(self.ack(h, ticket, "PENDING", message).map_err(Fault::into_lerr)?);
                    continue;
                }
            };
            let (status, reason) =
                confirm_ticket(&contracts_of(ticket), &before, &positions, &working, &mut claimed)?;
            if status == "REJECTED" {
                self.unexpect(ticket, None)?;
            }
            let message = if status == "PENDING" { format!("{}; {reason}", sent.message) } else { reason };
            acks.push(self.ack(h, ticket, status, message).map_err(Fault::into_lerr)?);
            before = position_book(&positions)?;
        }
        self.finish(h, acks, &contracts, None)
    }

    fn finish(&mut self, h: &mut dyn Host, acks: Vec<Ack>, contracts: &[Instrument], failed: Option<String>) -> R<Json> {
        let now = h.now().map_err(Fault::into_lerr)?;
        let event = match failed {
            Some(why) => {
                let mut all: Vec<Instrument> = contracts.to_vec();
                all.extend(self.expected.iter().map(|(i, _)| i.clone()));
                unreadable(&self.venue, &now, &all, &why)?
            }
            None => self.reconcile_now_inner(h, false)?,
        };
        if !reconciled(&event) {
            self.halted = true;
        }
        let proven: Vec<Json> = self
            .proven
            .iter()
            .map(|(k, (oid, state))| Json::Arr(vec![jstr(k.clone()), Json::Arr(vec![jstr(oid.clone()), jstr(state.clone())])]))
            .collect();
        Ok(obj(vec![
            ("acks", Json::Arr(acks.iter().map(Ack::json).collect())),
            ("reconcile", event),
            ("proven", Json::Arr(proven)),
        ]))
    }

    /// `reconcile_now`: compare the venue with the mirror book; drift (or an unreadable venue) halts.
    pub fn reconcile_now(&mut self, h: &mut dyn Host, defer_unavailable: bool) -> R<Json> {
        self.reconcile_now_inner(h, defer_unavailable)
    }

    fn reconcile_now_inner(&mut self, h: &mut dyn Host, defer_unavailable: bool) -> R<Json> {
        let now = h.now().map_err(Fault::into_lerr)?;
        let read = self.read_positions(h).and_then(|p| Ok((p, self.read_working(h)?)));
        let event = match read {
            Ok((positions, working)) => reconcile::reconcile(&self.venue, &now, &self.expected, &positions, &working)?,
            Err(Fault::Fatal(e)) => return Err(e),
            Err(f) => {
                if defer_unavailable && f.unavailable() {
                    return Err(f.into_lerr());
                }
                let contracts: Vec<Instrument> = self.expected.iter().map(|(i, _)| i.clone()).collect();
                unreadable(&self.venue, &now, &contracts, &f.text())?
            }
        };
        if !reconciled(&event) {
            self.halted = true;
        }
        Ok(event)
    }

    /// `_send`: one ticket to the venue. A refused or unclear send is an ack, never a raise.
    fn send(&mut self, h: &mut dyn Host, ticket: &Ticket) -> X<Ack> {
        let id = &ticket.venue_order_id;
        let spec = match transport::ticket_for(&ticket_for_doc(ticket)) {
            Ok(spec) => spec,
            Err(e) if e.kind == UNSUPPORTED => {
                return self.ack(h, ticket, "REJECTED", format!("UnsupportedCapability: {}", e.msg));
            }
            Err(e) => return Err(e.into()),
        };
        match h.place_order(&spec, id) {
            Err(Fault::Raised(r)) => {
                let now = h.now()?;
                Ok(Ack::of(&normalize::place_exception(&exc_doc(&r))?, id, &now)?)
            }
            Err(other) => Err(other),
            Ok(raw) => {
                let rawdoc = raw_doc(&raw);
                if let Some(Json::Str(order_id)) = normalize::placed_order_id(&rawdoc)?.get("order_id") {
                    self.sent.insert(
                        id.clone(),
                        id.clone(),
                        Sent { ticket: ticket.clone(), order_id: order_id.clone(), resting: ticket.quantity.clone() },
                    );
                    let status = raw.get("book_status").cloned().unwrap_or(Json::Null);
                    let state = normalize::book_state_op(&obj(vec![("status", status)]))?;
                    self.proven.insert(id.clone(), id.clone(), (order_id.clone(), req_str(&state, "state")?.to_string()));
                }
                let now = h.now()?;
                Ok(Ack::of(&normalize::place_result(&rawdoc)?, id, &now)?)
            }
        }
    }

    /// `submit`: send one venue order directly. PENDING until read back.
    pub fn submit(&mut self, h: &mut dyn Host, ticket: &Json) -> R<Json> {
        self.require_connected("submit")?;
        let ticket = ticket_of(ticket)?;
        if self.halted {
            let message = format!("venue {} is halted by a reconcile drift", self.venue);
            return Ok(self.ack(h, &ticket, "REJECTED", message).map_err(Fault::into_lerr)?.json());
        }
        transport::ticket_for(&ticket_for_doc(&ticket))?;
        Ok(self.send(h, &ticket).map_err(Fault::into_lerr)?.json())
    }

    // -- cancel ----------------------------------------------------------------------------------

    /// `cancel`: one ticket by its key. Allowed while halted: a cancel only lowers risk.
    pub fn cancel(&mut self, h: &mut dyn Host, venue_order_id: &str) -> R<Json> {
        self.require_connected("cancel")?;
        let now = h.now().map_err(Fault::into_lerr)?;
        if self.cancelled.contains(venue_order_id) {
            return Ok(Ack::new(venue_order_id, "ACCEPTED", &now, "already cancelled".into())?.json());
        }
        if let Some(index) = self.queue.iter().position(|q| q.venue_order_id == venue_order_id) {
            let queued = self.queue.remove(index);
            self.unexpect(&queued, None)?;
            self.cancelled.insert(venue_order_id.to_string());
            return Ok(Ack::new(
                venue_order_id,
                "ACCEPTED",
                &now,
                "cancelled before send: dropped from the queue".into(),
            )?
            .json());
        }
        let Some(sent) = self.sent.get(venue_order_id).cloned() else {
            return Ok(Ack::new(
                venue_order_id,
                "REJECTED",
                &now,
                "no venue Order ID was proven for this ticket; refusing to guess the row (I5)".into(),
            )?
            .json());
        };
        if !h.can_cancel().map_err(Fault::into_lerr)? {
            return Ok(Ack::new(venue_order_id, "REJECTED", &now, "the transport cannot cancel; refusing".into())?.json());
        }
        let raw = match h.cancel_order(&sent.order_id) {
            Err(Fault::Raised(r)) => {
                let now = h.now().map_err(Fault::into_lerr)?;
                return Ok(Ack::of(&normalize::cancel_exception(&exc_doc(&r))?, venue_order_id, &now)?.json());
            }
            Err(other) => return Err(other.into_lerr()),
            Ok(raw) => raw,
        };
        let now = h.now().map_err(Fault::into_lerr)?;
        let ack = Ack::of(&normalize::cancel_result(&raw_doc(&raw))?, venue_order_id, &now)?;
        if ack.status == "ACCEPTED" {
            self.sent.remove(venue_order_id);
            self.unexpect(&sent.ticket, Some(&sent.resting))?;
            self.cancelled.insert(venue_order_id.to_string());
        }
        Ok(ack.json())
    }

    /// `_unexpect`: the ticket's contracts (`units`, default all) leave the expected book.
    fn unexpect(&mut self, ticket: &Ticket, units: Option<&PyDec>) -> R<()> {
        add_ticket(&mut self.expected, ticket, -1, units)
    }

    /// `proven_order_id`: the venue Order ID a send (or the restored fold) proved, or null.
    pub fn proven_order_id(&self, ticket_key: &str) -> Json {
        self.sent.get(ticket_key).map_or(Json::Null, |s| jstr(s.order_id.clone()))
    }

    /// `positions`: the venue's positions, normalized.
    pub fn positions(&mut self, h: &mut dyn Host) -> R<Json> {
        self.require_connected("positions")?;
        Ok(Json::Arr(self.position_docs(h).map_err(Fault::into_lerr)?))
    }

    // -- the state, for the host and the lockstep gate -------------------------------------------

    pub fn state(&self) -> Json {
        let pairs = |book: &Book| pairs_of_book(book);
        obj(vec![
            ("halted", Json::Bool(self.halted)),
            ("restored", Json::Bool(self.restored)),
            ("queued", Json::Arr(self.queue.iter().map(ticket_json).collect())),
            ("keys", Json::Arr(self.keys.iter().cloned().map(Json::Str).collect())),
            ("expected", pairs(&self.expected)),
            (
                "sent",
                Json::Arr(
                    self.sent
                        .iter()
                        .map(|(k, v)| {
                            Json::Arr(vec![
                                jstr(k.clone()),
                                ticket_json(&v.ticket),
                                jstr(v.order_id.clone()),
                                jstr(s(&v.resting)),
                            ])
                        })
                        .collect(),
                ),
            ),
            (
                "proven",
                Json::Arr(
                    self.proven
                        .iter()
                        .map(|(k, (oid, st))| {
                            Json::Arr(vec![jstr(k.clone()), Json::Arr(vec![jstr(oid.clone()), jstr(st.clone())])])
                        })
                        .collect(),
                ),
            ),
            ("cancelled", Json::Arr(self.cancelled.iter().cloned().map(Json::Str).collect())),
            (
                "preflight",
                match &self.preflight_book {
                    Some((when, book)) => Json::Arr(vec![jstr(when.clone()), pairs(book)]),
                    None => Json::Null,
                },
            ),
        ])
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A host that records what the broker asked of it.
    #[derive(Default)]
    struct Mock {
        calls: Vec<String>,
    }

    impl Host for Mock {
        fn now(&mut self) -> X<String> {
            self.calls.push("now".into());
            Ok("2026-09-24T20:00:00+00:00".into())
        }
        fn place_order(&mut self, _: &Json, key: &str) -> X<Json> {
            self.calls.push(format!("place {key}"));
            Ok(obj(vec![("status", jstr("SENT"))]))
        }
        fn read_positions(&mut self) -> X<Vec<Json>> {
            self.calls.push("positions".into());
            Ok(Vec::new())
        }
        fn read_working_orders(&mut self) -> X<Vec<Json>> {
            self.calls.push("working".into());
            Ok(Vec::new())
        }
        fn read_order_fills(&mut self) -> X<Vec<Json>> {
            Ok(Vec::new())
        }
        fn cancel_order(&mut self, _: &str) -> X<Json> {
            Ok(Json::Null)
        }
        fn can_read_fills(&mut self) -> X<bool> {
            Ok(false)
        }
        fn can_cancel(&mut self) -> X<bool> {
            Ok(false)
        }
    }

    fn ticket(tif: &str) -> Ticket {
        let doc = obj(vec![
            ("venue_order_id", jstr("tos:x")),
            ("instrument", obj(vec![("kind", jstr("equity")), ("symbol", jstr("AAPL"))])),
            ("order_type", jstr("LIMIT")),
            ("side", jstr("SELL")),
            ("quantity", jstr("100")),
            ("submitted_at", jstr("2026-09-24T20:00:00+00:00")),
            ("tif", jstr(tif)),
            ("limit_price", jstr("2.00")),
            ("allocations", Json::Arr(vec![Json::Arr(vec![jstr("so-1"), jstr("OPT_CSP"), jstr("100")])])),
        ]);
        ticket_of(&doc).unwrap_or_else(|e| panic!("{}", e.msg))
    }

    /// The Python test pokes `_queue`; here the same injection: a queued ticket the venue cannot
    /// express is REJECTED with the capability's text, and never reaches the transport.
    #[test]
    fn an_inexpressible_queued_ticket_is_rejected_not_raised() {
        let mut b = Broker::new("D-00000001", vec!["OPT_CSP".into()], false);
        b.mark_connected();
        b.queue.push(ticket("GTD"));
        let mut h = Mock::default();
        let out = b.drain(&mut h).unwrap_or_else(|e| panic!("{}", e.msg));
        let text = json::dumps(&out);
        assert!(text.contains("REJECTED") && text.contains("UnsupportedCapability"), "{text}");
        assert!(h.calls.iter().all(|c| !c.starts_with("place")), "{:?}", h.calls);
        assert!(b.queue.is_empty());
    }

    #[test]
    fn nothing_runs_before_connect() {
        let mut b = Broker::new("D-00000001", vec!["OPT_CSP".into()], false);
        let mut h = Mock::default();
        let e = b.drain(&mut h).err().expect("a refusal");
        assert_eq!(e.msg, "drain before connect; prove the venue first (I10)");
        assert!(h.calls.is_empty());
    }

    #[test]
    fn a_halted_venue_stays_halted_across_restore() {
        let mut b = Broker::new("D-00000001", vec!["OPT_CSP".into()], true);
        assert!(b.halted());
        let mirror = obj(vec![("venue", jstr("D-00000001")), ("tickets", Json::Arr(vec![])), ("book", Json::Arr(vec![]))]);
        let _ = b.restore(&mirror, &[]);
        assert!(b.halted());
    }
}

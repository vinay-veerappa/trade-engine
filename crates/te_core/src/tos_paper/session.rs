//! Pure mirror session decision logic (was `src/trade_engine/tos_paper/session.py`).
//!
//! Orchestration and pure decisions for one mirror session around a `TosPaperBroker`
//! (§4.7, docs/RUST_PORT.md P5):
//! - Which orders form the batch: [`working_orders`], [`pending_orders`], [`morning_orders`], [`batch`].
//! - Window and state batch predicates: [`is_working`], [`is_ended_unfilled`], [`pending_wanted`], [`morning_wanted`].
//! - When to halt the venue: unproven open tickets ([`halt_unproven`]), unreadable venue, or reconcile drift.
//! - Write-ahead items preparation ([`prepare_write_ahead`]) and drain ack processing ([`build_drain_acks`]).
//! - Ticket cancellation ([`build_cancel_ack`]) and lifting operator halts ([`clear_halt`]).
//!
//! Pure decision logic only: ledger I/O, clock reads, and broker transport calls remain outside.
//! Every timestamp is injected (I7), money is [`Money`] / [`crate::ledger::model::Money`] (never `f64`),
//! and missing or contradictory states refuse with typed errors ([`SESSION`]) rather than guessing (I5).

use std::collections::{BTreeSet, HashMap};

use chrono::NaiveDate;

use super::cover::{self, Sold};
use super::netting::Ticket;
use super::wire::{jstr, obj, req, req_arr, req_str};
use super::{SESSION, VALUE};
use crate::ledger::codec;
use crate::ledger::json::Json;
use crate::ledger::mirror::{ticket_contracts, MirrorState, MirrorTicketState};
use crate::ledger::model::{
    date_iso, err, parse_datetime, DateTime, Instrument, MirrorAck, MirrorAllocation, MirrorFill,
    MirrorQueued, MirrorRefused, Obj, Order, OrderState, Side, Val, VenueHaltCleared, VenueReconcile, R,
};
use crate::oms::options::sha256_hex;

/// Morning EOD job suffix: `"-morning"`.
pub const MORNING_SUFFIX: &str = "-morning";

/// The sim still works these: a mirrored order must be live in the book of record.
pub fn is_working(state: OrderState) -> bool {
    matches!(state, OrderState::Submitted | OrderState::Accepted | OrderState::PartiallyFilled)
}

/// A morning entry the sim filled at once is still mirrored; one it refused or dropped is not.
pub fn is_ended_unfilled(state: OrderState) -> bool {
    matches!(state, OrderState::Cancelled | OrderState::Rejected | OrderState::Expired)
}

/// The in-session pass a run marker's job names (`eod-midday`), or None.
pub fn pass_of(job: &str) -> Option<&'static str> {
    for name in &["morning", "midday", "late"] {
        if job.ends_with(&format!("-{name}")) {
            return Some(*name);
        }
    }
    None
}

/// Whether an order is an entry the sim is currently working and this venue has not handled.
pub fn is_working_entry(order: &Order, mirror: &MirrorState) -> bool {
    order.parent_order_id.is_none() && is_working(order.state) && !mirror.handled(&order.order_id)
}

/// Predicate for EOD batch orders: created after or at session close and sim still works it.
pub fn pending_wanted(created: &Order, current: &Order, close: &DateTime) -> bool {
    created.created_at.instant() >= close.instant() && is_working(current.state)
}

/// Predicate for morning batch orders: created in the session (>= open) and not ended unfilled.
pub fn morning_wanted(created: &Order, current: &Order, session_open: &DateTime) -> bool {
    created.created_at.instant() >= session_open.instant() && !is_ended_unfilled(current.state)
}

/// Every entry the mirrored accounts' sim still works that this venue has not handled.
///
/// Entries only (no parent order). In account order, then order-id order, so the
/// first-in rule is deterministic.
pub fn working_orders(
    mirrored_accounts: &[String],
    orders_by_account: &HashMap<String, Vec<Order>>,
    mirror: &MirrorState,
) -> Vec<Order> {
    let mut found = Vec::new();
    for account in mirrored_accounts {
        if let Some(orders) = orders_by_account.get(account) {
            let mut sorted_orders = orders.clone();
            sorted_orders.sort_by(|a, b| a.order_id.cmp(&b.order_id));
            for order in sorted_orders {
                if is_working_entry(&order, mirror) {
                    found.push(order);
                }
            }
        }
    }
    found
}

/// One EOD run marker event for an account.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct MarkerEvent {
    pub seq: i128,
    pub session: NaiveDate,
    pub job: String,
}

/// One batch of orders created in an `ORDERS_CREATED` event.
#[derive(Debug, Clone)]
pub struct CreatedBatch {
    pub seq: i128,
    pub orders: Vec<Order>,
}

/// Account data required to evaluate a session batch.
#[derive(Debug, Clone, Default)]
pub struct AccountBatchData {
    pub markers: Vec<MarkerEvent>,
    pub created_events: Vec<CreatedBatch>,
    pub current_orders: HashMap<String, Order>,
}

/// The mirrored accounts' entries created up to their marker for `session` that
/// `wanted(created, current)` keeps and this venue has not handled.
///
/// Refuses when an account has no matching marker for the session (I5) or has markers
/// from multiple jobs without an explicit job filter (I5).
pub fn batch<F>(
    mirrored_accounts: &[String],
    accounts_data: &HashMap<String, AccountBatchData>,
    session: NaiveDate,
    job: Option<&str>,
    morning: bool,
    wanted: F,
    mirror: &MirrorState,
) -> R<Vec<Order>>
where
    F: Fn(&Order, &Order) -> bool,
{
    let what = if morning { "morning pass" } else { "EOD run" };
    let mut found = Vec::new();
    let session_str = date_iso(&session);

    for account in mirrored_accounts {
        let data = match accounts_data.get(account) {
            Some(d) => d,
            None => {
                let job_str = match job {
                    Some(j) => format!(" (job {j})"),
                    None => String::new(),
                };
                return err(
                    SESSION,
                    format!("'{account}' has no {what} for {session_str}{job_str}; refusing to mirror a batch that may be incomplete (I5)"),
                );
            }
        };

        let markers: Vec<&MarkerEvent> = data
            .markers
            .iter()
            .filter(|m| {
                m.session == session
                    && pass_of(&m.job) == (if morning { Some("morning") } else { None })
                    && (job.is_none() || job == Some(m.job.as_str()))
            })
            .collect();

        if markers.is_empty() {
            let job_str = match job {
                Some(j) => format!(" (job {j})"),
                None => String::new(),
            };
            return err(
                SESSION,
                format!("'{account}' has no {what} for {session_str}{job_str}; refusing to mirror a batch that may be incomplete (I5)"),
            );
        }

        let jobs: BTreeSet<&str> = markers.iter().map(|m| m.job.as_str()).collect();
        if jobs.len() > 1 {
            let sorted_jobs: Vec<&str> = jobs.into_iter().collect();
            let plural = if morning { "morning passes" } else { "EOD runs" };
            return err(
                SESSION,
                format!("'{account}' has {plural} of {sorted_jobs:?} for {session_str}; name the job (I5)"),
            );
        }

        let marker = markers[0];
        for event in &data.created_events {
            if event.seq > marker.seq {
                continue;
            }
            for created in &event.orders {
                let current = data.current_orders.get(&created.order_id);
                if created.parent_order_id.is_none()
                    && current.is_some()
                    && wanted(created, current.unwrap())
                    && !mirror.handled(&created.order_id)
                {
                    found.push(current.unwrap().clone());
                }
            }
        }
    }
    Ok(found)
}

/// The EOD batch of `session` still to mirror: the D+1 entries, not yet handled.
pub fn pending_orders(
    mirrored_accounts: &[String],
    accounts_data: &HashMap<String, AccountBatchData>,
    session: NaiveDate,
    close: &DateTime,
    job: Option<&str>,
    mirror: &MirrorState,
) -> R<Vec<Order>> {
    batch(
        mirrored_accounts,
        accounts_data,
        session,
        job,
        false,
        |created, current| pending_wanted(created, current, close),
        mirror,
    )
}

/// The batch of `session`'s morning pass still to mirror, not yet handled.
pub fn morning_orders(
    mirrored_accounts: &[String],
    accounts_data: &HashMap<String, AccountBatchData>,
    session: NaiveDate,
    session_open: &DateTime,
    job: Option<&str>,
    mirror: &MirrorState,
) -> R<Vec<Order>> {
    let effective_job = job.map(|j| format!("{j}{MORNING_SUFFIX}"));
    batch(
        mirrored_accounts,
        accounts_data,
        session,
        effective_job.as_deref(),
        true,
        |created, current| morning_wanted(created, current, session_open),
        mirror,
    )
}

/// Open tickets with no proven venue Order ID: their fills cannot be read back.
pub fn unproven_tickets<'a>(mirror: &'a MirrorState) -> R<Vec<&'a MirrorTicketState>> {
    let open = mirror.open_tickets()?;
    Ok(open.into_iter().filter(|t| t.venue_order_id.is_none()).collect())
}

/// Halt the venue when an open ticket has no proven Order ID (I5): never silent.
///
/// Returns the `VenueReconcile` halt event and its derived `command_id` if any ticket is unproven.
pub fn halt_unproven(
    venue: &str,
    mirror: &MirrorState,
    as_of: &DateTime,
    phase: &str,
) -> R<Option<(VenueReconcile, String)>> {
    let unproven = unproven_tickets(mirror)?;
    if unproven.is_empty() {
        return Ok(None);
    }
    let mut symbols = BTreeSet::new();
    for t in &unproven {
        for (c, _) in ticket_contracts(&t.queued, &t.queued.quantity)?.iter() {
            symbols.insert(c.symbol()?);
        }
    }
    let drift: Vec<String> = symbols.into_iter().collect();
    let keys: Vec<&str> = unproven.iter().map(|t| t.queued.ticket_key.as_str()).collect();
    let note = format!(
        "no proven venue Order ID for open ticket(s) {}: their fills cannot be read back; venue halted (I5)",
        keys.join(", ")
    );
    let event = VenueReconcile {
        venue: venue.to_string(),
        as_of: as_of.clone(),
        reconciled: false,
        drift,
        note: Some(note),
    };
    let command_id = reconcile_command_id(venue, &format!("unproven:{phase}"), as_of, &event)?;
    Ok(Some((event, command_id)))
}

/// Derives the command id for a venue mirror event: `mirror:{venue}:{suffix}`.
pub fn mirror_command_id(venue: &str, suffix: &str) -> String {
    format!("mirror:{venue}:{suffix}")
}

/// Computes the command id for a `VenueReconcile` event with SHA-256 digest:
/// `mirror:{venue}:reconcile:{phase}:{as_of.iso()}:{digest[..16]}`.
pub fn reconcile_command_id(
    venue: &str,
    phase: &str,
    as_of: &DateTime,
    event: &VenueReconcile,
) -> R<String> {
    let payload = codec::enc_obj(&Obj::Reconcile(event.clone()))?;
    let json_str = crate::ledger::json::dumps(&payload);
    let full_hash = sha256_hex(json_str.as_bytes());
    let digest = &full_hash[..16];
    Ok(format!("mirror:{venue}:reconcile:{phase}:{}:{digest}", as_of.iso()))
}

/// The reason a run was deferred, in operator-facing words (I11).
pub fn deferred_why(doing: &str, exc_text: &str, recorded: Option<&str>) -> String {
    let kept = match recorded {
        Some(r) if !r.is_empty() => format!("{r}; "),
        _ => "nothing was sent or recorded; ".to_string(),
    };
    format!("the venue could not be asked while {doing} ({exc_text}); {kept}the next run asks again")
}

/// Deduplicate candidate orders by `order_id` (keeping first seen) and skipping already-handled orders.
pub fn filter_candidates(orders: &[Order], mirror: &MirrorState) -> Vec<Order> {
    let mut seen = BTreeSet::new();
    let mut candidates = Vec::new();
    for order in orders {
        if seen.contains(&order.order_id) || mirror.handled(&order.order_id) {
            continue;
        }
        seen.insert(order.order_id.clone());
        candidates.push(order.clone());
    }
    candidates
}

/// Maps an order to its single-leg sold asset if applicable for the cover rule.
pub fn sold_of_order(order: &Order) -> Sold {
    if order.side != Side::Sell {
        return None;
    }
    match &order.instrument {
        Instrument::Equity(_) => Some((order.instrument.clone(), order.quantity.clone())),
        Instrument::Option(c) if c.right == crate::options::Right::Call => {
            Some((order.instrument.clone(), order.quantity.clone()))
        }
        _ => None,
    }
}

/// Classifies candidate orders according to the cover rule into `(todo, waiting, refused)`.
pub fn classify_cover(
    mirror: &MirrorState,
    candidates: &[Order],
    may_wait: bool,
) -> R<(Vec<Order>, Vec<(String, String)>, Vec<(String, String)>)> {
    let mut todo = Vec::new();
    let mut waiting = Vec::new();
    let mut refused = Vec::new();
    let mut accepted_solds: Vec<Sold> = Vec::new();

    for order in candidates {
        let sold = sold_of_order(order);
        let reason = cover::cover_reason(mirror, &sold, &accepted_solds)?;
        match reason {
            None => {
                accepted_solds.push(sold);
                todo.push(order.clone());
            }
            Some(why) => {
                if may_wait {
                    waiting.push((order.order_id.clone(), why));
                } else {
                    refused.push((order.order_id.clone(), why));
                }
            }
        }
    }
    Ok((todo, waiting, refused))
}

/// One item to append to the ledger ahead of venue calls.
#[derive(Debug, Clone)]
pub enum WriteAheadItem {
    Refused(MirrorRefused, String),
    Queued(MirrorQueued, String),
}

/// Reconstructs a `MirrorQueued` event from a netted ticket.
pub fn ticket_to_queued(venue: &str, ticket: &Ticket, fallback_at: &DateTime) -> MirrorQueued {
    let at = match parse_datetime(&ticket.submitted_at) {
        Ok((dt, _)) => dt,
        Err(_) => fallback_at.clone(),
    };
    MirrorQueued {
        venue: venue.to_string(),
        ticket_key: ticket.venue_order_id.clone(),
        instrument: ticket.instrument.clone(),
        side: ticket.side,
        quantity: ticket.quantity.clone(),
        order_type: ticket.order_type,
        limit_price: ticket.limit_price.clone(),
        tif: ticket.tif,
        allocations: ticket
            .allocations
            .iter()
            .map(|(soid, acct, qty)| MirrorAllocation {
                strategy_order_id: soid.clone(),
                strategy_account: acct.clone(),
                quantity: qty.clone(),
            })
            .collect(),
        at,
    }
}

/// Prepares the write-ahead ledger items for cover refusals, batch refusals, and newly queued tickets.
pub fn prepare_write_ahead(
    venue: &str,
    accounts: &HashMap<String, String>,
    cover_refused: &[(String, String)],
    batch_refused: &[(String, String)],
    batch_venue_orders: &[Ticket],
    broker_queued_keys: &BTreeSet<String>,
    now: &DateTime,
) -> Vec<WriteAheadItem> {
    let mut items = Vec::new();

    for (order_id, reason) in cover_refused.iter().chain(batch_refused.iter()) {
        let account = accounts.get(order_id).map(|s| s.as_str()).unwrap_or("");
        let refusal = MirrorRefused {
            venue: venue.to_string(),
            strategy_order_id: order_id.clone(),
            strategy_account: account.to_string(),
            reason: reason.clone(),
            at: now.clone(),
        };
        let cmd = mirror_command_id(venue, &format!("refused:{order_id}"));
        items.push(WriteAheadItem::Refused(refusal, cmd));
    }

    for ticket in batch_venue_orders {
        if !broker_queued_keys.contains(&ticket.venue_order_id) {
            for (soid, acct, _) in &ticket.allocations {
                let refusal = MirrorRefused {
                    venue: venue.to_string(),
                    strategy_order_id: soid.clone(),
                    strategy_account: acct.clone(),
                    reason: format!(
                        "ticket {} was already used by this broker process and never recorded; refused (I3) - restart the broker from the ledger",
                        ticket.venue_order_id
                    ),
                    at: now.clone(),
                };
                let cmd = mirror_command_id(venue, &format!("refused:{soid}"));
                items.push(WriteAheadItem::Refused(refusal, cmd));
            }
            continue;
        }
        let queued = ticket_to_queued(venue, ticket, now);
        let cmd = mirror_command_id(venue, &format!("queued:{}", ticket.venue_order_id));
        items.push(WriteAheadItem::Queued(queued, cmd));
    }

    items
}

/// Builds `MirrorAck` ledger events from drain acks and proven venue order IDs.
pub fn build_drain_acks(
    venue: &str,
    acks: &[(String, String, Option<String>, DateTime)],
    proven: &HashMap<String, (String, OrderState)>,
) -> Vec<(MirrorAck, String)> {
    let mut out = Vec::new();
    for (ticket_key, status, message, ts) in acks {
        let (prov_id, book_st) = match proven.get(ticket_key) {
            Some((oid, st)) => (Some(oid.clone()), Some(*st)),
            None => (None, None),
        };
        let msg = match message {
            Some(m) if !m.is_empty() => m.clone(),
            _ => status.clone(),
        };
        let ack_ev = MirrorAck {
            venue: venue.to_string(),
            ticket_key: ticket_key.clone(),
            status: status.clone(),
            message: msg,
            at: ts.clone(),
            venue_order_id: prov_id,
            book_status: book_st,
        };
        let cmd = mirror_command_id(venue, &format!("ack:{ticket_key}"));
        out.push((ack_ev, cmd));
    }
    out
}

/// Builds a `MirrorAck` for a successful ticket cancel.
pub fn build_cancel_ack(
    venue: &str,
    ticket_key: &str,
    order_id: Option<&str>,
    ack_status: &str,
    ack_message: &str,
    ack_timestamp: &DateTime,
) -> Option<(MirrorAck, String)> {
    if ack_status == "ACCEPTED" && order_id.is_some() {
        let msg = if ack_message.is_empty() { "cancelled" } else { ack_message };
        let ack_ev = MirrorAck {
            venue: venue.to_string(),
            ticket_key: ticket_key.to_string(),
            status: "ACCEPTED".to_string(),
            message: msg.to_string(),
            at: ack_timestamp.clone(),
            venue_order_id: Some(order_id.unwrap().to_string()),
            book_status: Some(OrderState::Cancelled),
        };
        let cmd = mirror_command_id(venue, &format!("cancel:{ticket_key}"));
        Some((ack_ev, cmd))
    } else {
        None
    }
}

/// The plan of events and command IDs to lift an operator halt on a venue.
#[derive(Debug, Clone)]
pub struct ClearHaltPlan {
    pub holders: Vec<String>,
    pub events: Vec<(String, VenueHaltCleared, String)>,
}

/// Lifts `venue`'s halt on operator request (§4.5).
///
/// Refuses if reason is empty (I11), venue is not halted, or newest reconcile still drifts.
pub fn clear_halt(
    venue: &str,
    reason: &str,
    as_of: &DateTime,
    holders: &[String],
    reconciles: &[(i128, VenueReconcile)],
) -> R<ClearHaltPlan> {
    if reason.trim().is_empty() {
        return err(SESSION, "a halt is lifted only with the operator's reason (I11)");
    }
    let mut sorted_holders = holders.to_vec();
    sorted_holders.sort();
    if sorted_holders.is_empty() {
        return err(SESSION, format!("venue {venue} is not halted"));
    }
    let venue_reconciles: Vec<&(i128, VenueReconcile)> = reconciles
        .iter()
        .filter(|(_, r)| r.venue == venue)
        .collect();
    let newest = match venue_reconciles.iter().max_by_key(|(seq, _)| seq) {
        Some(entry) => *entry,
        None => return err(SESSION, format!("venue {venue} is not halted")),
    };
    if !newest.1.reconciled {
        let drift_str = format!("{:?}", newest.1.drift);
        let note_str = newest.1.note.as_deref().unwrap_or("");
        return err(
            SESSION,
            format!(
                "venue {}'s newest reconcile ({}) still drifts: {} ({}); fix it and collect again first",
                venue,
                newest.1.as_of.iso(),
                drift_str,
                note_str
            ),
        );
    }
    let mut events = Vec::new();
    for account in &sorted_holders {
        let cleared = VenueHaltCleared {
            venue: venue.to_string(),
            at: as_of.clone(),
            reason: reason.trim().to_string(),
            reconcile_seq: newest.0,
        };
        let cmd = format!("halt-cleared:{venue}:{account}:{}", newest.0);
        events.push((account.clone(), cleared, cmd));
    }
    Ok(ClearHaltPlan {
        holders: sorted_holders,
        events,
    })
}

/// What one mirror session appended or planned.
#[derive(Debug, Clone, Default)]
pub struct MirrorRunReport {
    pub venue: String,
    pub fills: Vec<MirrorFill>,
    pub closes: Vec<MirrorAck>,
    pub reconcile: Option<VenueReconcile>,
    pub refused: Vec<MirrorRefused>,
    pub queued: Vec<MirrorQueued>,
    pub acks: Vec<MirrorAck>,
    pub drain_reconcile: Option<VenueReconcile>,
    pub halted: bool,
    pub deferred: Option<String>,
    pub waiting: Vec<(String, String)>,
}

// -------------------------------------------------------------------------------------------------
// Door JSON Handlers
// -------------------------------------------------------------------------------------------------

pub fn deferred_why_op(doc: &Json) -> R<Json> {
    let doing = req_str(doc, "doing")?;
    let exc = req_str(doc, "exc")?;
    let recorded = match doc.get("recorded") {
        Some(Json::Str(s)) => Some(s.as_str()),
        _ => None,
    };
    Ok(obj(vec![("why", jstr(deferred_why(doing, exc, recorded)))]))
}

pub fn halt_unproven_op(doc: &Json) -> R<Json> {
    let venue = req_str(doc, "venue")?;
    let phase = req_str(doc, "phase")?;
    let (as_of, _) = parse_datetime(req_str(doc, "as_of")?)?;
    let mirror: MirrorState = crate::ledger::bridge::uncanon_mirror(req(doc, "mirror")?)?;
    let res = halt_unproven(venue, &mirror, &as_of, phase)?;
    match res {
        None => Ok(obj(vec![("halt", Json::Null)])),
        Some((event, cmd)) => {
            let encoded = codec::enc_obj(&Obj::Reconcile(event))?;
            Ok(obj(vec![("event", encoded), ("command_id", jstr(cmd))]))
        }
    }
}

pub fn clear_halt_op(doc: &Json) -> R<Json> {
    let venue = req_str(doc, "venue")?;
    let reason = req_str(doc, "reason")?;
    let (as_of, _) = parse_datetime(req_str(doc, "as_of")?)?;
    let holders_arr = req_arr(doc, "holders")?;
    let mut holders = Vec::new();
    for h in holders_arr {
        let Json::Str(s) = h else { return err(VALUE, "holder must be string"); };
        holders.push(s.clone());
    }
    let reconciles_arr = req_arr(doc, "reconciles")?;
    let mut reconciles = Vec::new();
    for item in reconciles_arr {
        let Json::Arr(pair) = item else { return err(VALUE, "reconcile entry must be [seq, event]"); };
        if pair.len() != 2 {
            return err(VALUE, "reconcile entry must be [seq, event]");
        }
        let seq = match &pair[0] {
            Json::Int(n) => *n,
            _ => return err(VALUE, "seq must be int"),
        };
        let val = codec::decode(&pair[1])?;
        let Val::Obj(b) = val else {
            return err(VALUE, "payload must be VenueReconcile");
        };
        let Obj::Reconcile(r) = *b else {
            return err(VALUE, "payload must be VenueReconcile");
        };
        reconciles.push((seq, r));
    }
    let plan = clear_halt(venue, reason, &as_of, &holders, &reconciles)?;
    let mut ev_json = Vec::new();
    for (acct, ev, cmd) in plan.events {
        let enc = codec::enc_obj(&Obj::HaltCleared(ev))?;
        ev_json.push(obj(vec![("account", jstr(acct)), ("event", enc), ("command_id", jstr(cmd))]));
    }
    Ok(obj(vec![
        ("holders", Json::Arr(plan.holders.into_iter().map(jstr).collect())),
        ("events", Json::Arr(ev_json)),
    ]))
}

pub fn reconcile_command_id_op(doc: &Json) -> R<Json> {
    let venue = req_str(doc, "venue")?;
    let phase = req_str(doc, "phase")?;
    let (as_of, _) = parse_datetime(req_str(doc, "as_of")?)?;
    let val = codec::decode(req(doc, "event")?)?;
    let Val::Obj(b) = val else {
        return err(VALUE, "event must be VenueReconcile");
    };
    let Obj::Reconcile(event) = *b else {
        return err(VALUE, "event must be VenueReconcile");
    };
    let cmd = reconcile_command_id(venue, phase, &as_of, &event)?;
    Ok(obj(vec![("command_id", jstr(cmd))]))
}

pub fn working_orders_op(doc: &Json) -> R<Json> {
    let mirrored_arr = req_arr(doc, "mirrored_accounts")?;
    let mut mirrored_accounts = Vec::new();
    for item in mirrored_arr {
        let Json::Str(s) = item else { return err(VALUE, "mirrored account must be str"); };
        mirrored_accounts.push(s.clone());
    }
    let mirror: MirrorState = crate::ledger::bridge::uncanon_mirror(req(doc, "mirror")?)?;
    let acct_orders_val = req(doc, "account_orders")?;
    let Json::Obj(pairs) = acct_orders_val else { return err(VALUE, "account_orders must be obj"); };
    let mut orders_by_account = HashMap::new();
    for (acct, ords_val) in pairs {
        let Json::Arr(items) = ords_val else { return err(VALUE, "orders must be arr"); };
        let mut ord_list = Vec::new();
        for item in items {
            let val = codec::decode(item)?;
            let Val::Obj(b) = val else { return err(VALUE, "order must be Obj"); };
            let Obj::Order(o) = *b else { return err(VALUE, "order must be Order"); };
            ord_list.push(o);
        }
        orders_by_account.insert(acct.clone(), ord_list);
    }
    let found = working_orders(&mirrored_accounts, &orders_by_account, &mirror);
    let mut encoded = Vec::new();
    for o in found {
        encoded.push(codec::enc_obj(&Obj::Order(o))?);
    }
    Ok(obj(vec![("orders", Json::Arr(encoded))]))
}

fn parse_account_batch_data(doc: &Json) -> R<AccountBatchData> {
    let markers_arr = req_arr(doc, "markers")?;
    let mut markers = Vec::new();
    for m in markers_arr {
        let seq = match req(m, "seq")? {
            Json::Int(n) => *n,
            _ => return err(VALUE, "marker seq must be int"),
        };
        let session = NaiveDate::parse_from_str(req_str(m, "session")?, "%Y-%m-%d")
            .map_err(|e| crate::ledger::model::LErr { kind: VALUE, msg: format!("bad date: {e}") })?;
        let job = req_str(m, "job")?.to_string();
        markers.push(MarkerEvent { seq, session, job });
    }
    let created_arr = req_arr(doc, "created_events")?;
    let mut created_events = Vec::new();
    for c in created_arr {
        let seq = match req(c, "seq")? {
            Json::Int(n) => *n,
            _ => return err(VALUE, "created event seq must be int"),
        };
        let ord_arr = req_arr(c, "orders")?;
        let mut orders = Vec::new();
        for ord_json in ord_arr {
            let val = codec::decode(ord_json)?;
            let Val::Obj(b) = val else { return err(VALUE, "order must be Obj"); };
            let Obj::Order(o) = *b else { return err(VALUE, "order must be Order"); };
            orders.push(o);
        }
        created_events.push(CreatedBatch { seq, orders });
    }
    let current_ords_val = req(doc, "current_orders")?;
    let mut current_orders = HashMap::new();
    if let Json::Arr(items) = current_ords_val {
        for ord_json in items {
            let val = codec::decode(ord_json)?;
            let Val::Obj(b) = val else { return err(VALUE, "order must be Obj"); };
            let Obj::Order(o) = *b else { return err(VALUE, "order must be Order"); };
            current_orders.insert(o.order_id.clone(), o);
        }
    } else if let Json::Obj(pairs) = current_ords_val {
        for (_, ord_json) in pairs {
            let val = codec::decode(ord_json)?;
            let Val::Obj(b) = val else { return err(VALUE, "order must be Obj"); };
            let Obj::Order(o) = *b else { return err(VALUE, "order must be Order"); };
            current_orders.insert(o.order_id.clone(), o);
        }
    } else {
        return err(VALUE, "current_orders must be arr or obj");
    }
    Ok(AccountBatchData { markers, created_events, current_orders })
}

fn parse_accounts_data_map(doc: &Json) -> R<HashMap<String, AccountBatchData>> {
    let accts_val = req(doc, "accounts_data")?;
    let Json::Obj(pairs) = accts_val else { return err(VALUE, "accounts_data must be obj"); };
    let mut map = HashMap::new();
    for (acct, data_val) in pairs {
        map.insert(acct.clone(), parse_account_batch_data(data_val)?);
    }
    Ok(map)
}

pub fn pending_orders_op(doc: &Json) -> R<Json> {
    let mirrored_arr = req_arr(doc, "mirrored_accounts")?;
    let mut mirrored_accounts = Vec::new();
    for item in mirrored_arr {
        let Json::Str(s) = item else { return err(VALUE, "mirrored account must be str"); };
        mirrored_accounts.push(s.clone());
    }
    let accounts_data = parse_accounts_data_map(doc)?;
    let session = NaiveDate::parse_from_str(req_str(doc, "session")?, "%Y-%m-%d")
        .map_err(|e| crate::ledger::model::LErr { kind: VALUE, msg: format!("bad date: {e}") })?;
    let (close, _) = parse_datetime(req_str(doc, "close")?)?;
    let job = match doc.get("job") {
        Some(Json::Str(s)) if !s.is_empty() => Some(s.as_str()),
        _ => None,
    };
    let mirror: MirrorState = crate::ledger::bridge::uncanon_mirror(req(doc, "mirror")?)?;
    let found = pending_orders(&mirrored_accounts, &accounts_data, session, &close, job, &mirror)?;
    let mut encoded = Vec::new();
    for o in found {
        encoded.push(codec::enc_obj(&Obj::Order(o))?);
    }
    Ok(obj(vec![("orders", Json::Arr(encoded))]))
}

pub fn morning_orders_op(doc: &Json) -> R<Json> {
    let mirrored_arr = req_arr(doc, "mirrored_accounts")?;
    let mut mirrored_accounts = Vec::new();
    for item in mirrored_arr {
        let Json::Str(s) = item else { return err(VALUE, "mirrored account must be str"); };
        mirrored_accounts.push(s.clone());
    }
    let accounts_data = parse_accounts_data_map(doc)?;
    let session = NaiveDate::parse_from_str(req_str(doc, "session")?, "%Y-%m-%d")
        .map_err(|e| crate::ledger::model::LErr { kind: VALUE, msg: format!("bad date: {e}") })?;
    let (session_open, _) = parse_datetime(req_str(doc, "session_open")?)?;
    let job = match doc.get("job") {
        Some(Json::Str(s)) if !s.is_empty() => Some(s.as_str()),
        _ => None,
    };
    let mirror: MirrorState = crate::ledger::bridge::uncanon_mirror(req(doc, "mirror")?)?;
    let found = morning_orders(&mirrored_accounts, &accounts_data, session, &session_open, job, &mirror)?;
    let mut encoded = Vec::new();
    for o in found {
        encoded.push(codec::enc_obj(&Obj::Order(o))?);
    }
    Ok(obj(vec![("orders", Json::Arr(encoded))]))
}

pub fn classify_cover_op(doc: &Json) -> R<Json> {
    let mirror: MirrorState = crate::ledger::bridge::uncanon_mirror(req(doc, "mirror")?)?;
    let orders_arr = req_arr(doc, "orders")?;
    let mut orders = Vec::new();
    for item in orders_arr {
        let val = codec::decode(item)?;
        let Val::Obj(b) = val else { return err(VALUE, "order must be Obj"); };
        let Obj::Order(o) = *b else { return err(VALUE, "item must be Order"); };
        orders.push(o);
    }
    let may_wait = match doc.get("may_wait") {
        Some(Json::Bool(b)) => *b,
        _ => false,
    };
    let candidates = filter_candidates(&orders, &mirror);
    let (todo, waiting, refused) = classify_cover(&mirror, &candidates, may_wait)?;
    let mut enc_todo = Vec::new();
    for o in todo {
        enc_todo.push(codec::enc_obj(&Obj::Order(o))?);
    }
    let enc_waiting: Vec<Json> = waiting
        .into_iter()
        .map(|(oid, why)| Json::Arr(vec![jstr(oid), jstr(why)]))
        .collect();
    let enc_refused: Vec<Json> = refused
        .into_iter()
        .map(|(oid, why)| Json::Arr(vec![jstr(oid), jstr(why)]))
        .collect();
    Ok(obj(vec![
        ("todo", Json::Arr(enc_todo)),
        ("waiting", Json::Arr(enc_waiting)),
        ("refused", Json::Arr(enc_refused)),
    ]))
}

pub fn prepare_write_ahead_op(doc: &Json) -> R<Json> {
    let venue = req_str(doc, "venue")?;
    let accts_val = req(doc, "accounts")?;
    let Json::Obj(acct_pairs) = accts_val else { return err(VALUE, "accounts must be obj"); };
    let mut accounts = HashMap::new();
    for (k, v) in acct_pairs {
        let Json::Str(s) = v else { return err(VALUE, "account must be str"); };
        accounts.insert(k.clone(), s.clone());
    }
    let cover_refused_arr = req_arr(doc, "cover_refused")?;
    let mut cover_refused = Vec::new();
    for pair in cover_refused_arr {
        let Json::Arr(p) = pair else { return err(VALUE, "refused entry must be pair"); };
        if p.len() != 2 { return err(VALUE, "refused entry must be pair"); }
        let (Json::Str(oid), Json::Str(reason)) = (&p[0], &p[1]) else { return err(VALUE, "refused entry must be str pair"); };
        cover_refused.push((oid.clone(), reason.clone()));
    }
    let batch_refused_arr = req_arr(doc, "batch_refused")?;
    let mut batch_refused = Vec::new();
    for pair in batch_refused_arr {
        let Json::Arr(p) = pair else { return err(VALUE, "refused entry must be pair"); };
        if p.len() != 2 { return err(VALUE, "refused entry must be pair"); }
        let (Json::Str(oid), Json::Str(reason)) = (&p[0], &p[1]) else { return err(VALUE, "refused entry must be str pair"); };
        batch_refused.push((oid.clone(), reason.clone()));
    }
    let tickets_arr = req_arr(doc, "batch_venue_orders")?;
    let mut batch_venue_orders = Vec::new();
    for t_json in tickets_arr {
        batch_venue_orders.push(super::broker::ticket_of(t_json)?);
    }
    let keys_arr = req_arr(doc, "broker_queued_keys")?;
    let mut broker_queued_keys = BTreeSet::new();
    for k in keys_arr {
        let Json::Str(s) = k else { return err(VALUE, "key must be str"); };
        broker_queued_keys.insert(s.clone());
    }
    let (now, _) = parse_datetime(req_str(doc, "now")?)?;
    let items = prepare_write_ahead(
        venue,
        &accounts,
        &cover_refused,
        &batch_refused,
        &batch_venue_orders,
        &broker_queued_keys,
        &now,
    );
    let mut enc_items = Vec::new();
    for item in items {
        match item {
            WriteAheadItem::Refused(r, cmd) => {
                let enc = codec::enc_obj(&Obj::MRefused(r))?;
                enc_items.push(obj(vec![
                    ("type", jstr("refused")),
                    ("payload", enc),
                    ("command_id", jstr(cmd)),
                ]));
            }
            WriteAheadItem::Queued(q, cmd) => {
                let enc = codec::enc_obj(&Obj::MQueued(q))?;
                enc_items.push(obj(vec![
                    ("type", jstr("queued")),
                    ("payload", enc),
                    ("command_id", jstr(cmd)),
                ]));
            }
        }
    }
    Ok(obj(vec![("items", Json::Arr(enc_items))]))
}

pub fn build_drain_acks_op(doc: &Json) -> R<Json> {
    let venue = req_str(doc, "venue")?;
    let acks_arr = req_arr(doc, "acks")?;
    let mut acks = Vec::new();
    for a in acks_arr {
        let Json::Arr(p) = a else { return err(VALUE, "ack entry must be [key, status, msg, ts]"); };
        if p.len() != 4 { return err(VALUE, "ack entry must have 4 elements"); }
        let Json::Str(key) = &p[0] else { return err(VALUE, "key must be str"); };
        let Json::Str(status) = &p[1] else { return err(VALUE, "status must be str"); };
        let msg = match &p[2] {
            Json::Str(s) if !s.is_empty() => Some(s.clone()),
            _ => None,
        };
        let (ts, _) = match &p[3] {
            Json::Str(s) => parse_datetime(s)?,
            _ => return err(VALUE, "ts must be str"),
        };
        acks.push((key.clone(), status.clone(), msg, ts));
    }
    let proven_val = req(doc, "proven")?;
    let Json::Obj(proven_pairs) = proven_val else { return err(VALUE, "proven must be obj"); };
    let mut proven = HashMap::new();
    for (k, v) in proven_pairs {
        let Json::Arr(p) = v else { return err(VALUE, "proven entry must be pair"); };
        if p.len() != 2 { return err(VALUE, "proven entry must be pair"); }
        let (Json::Str(oid), Json::Str(st_str)) = (&p[0], &p[1]) else { return err(VALUE, "proven entry must be str pair"); };
        let book_st = super::wire::state_of(st_str)?;
        proven.insert(k.clone(), (oid.clone(), book_st));
    }
    let built = build_drain_acks(venue, &acks, &proven);
    let mut enc_acks = Vec::new();
    for (ack, cmd) in built {
        let enc = codec::enc_obj(&Obj::MAck(ack))?;
        enc_acks.push(obj(vec![("payload", enc), ("command_id", jstr(cmd))]));
    }
    Ok(obj(vec![("acks", Json::Arr(enc_acks))]))
}

pub fn build_cancel_ack_op(doc: &Json) -> R<Json> {
    let venue = req_str(doc, "venue")?;
    let ticket_key = req_str(doc, "ticket_key")?;
    let order_id = match doc.get("order_id") {
        Some(Json::Str(s)) if !s.is_empty() => Some(s.as_str()),
        _ => None,
    };
    let ack_status = req_str(doc, "ack_status")?;
    let ack_message = match doc.get("ack_message") {
        Some(Json::Str(s)) => s.as_str(),
        _ => "",
    };
    let (ack_ts, _) = parse_datetime(req_str(doc, "ack_timestamp")?)?;
    let res = build_cancel_ack(venue, ticket_key, order_id, ack_status, ack_message, &ack_ts);
    match res {
        None => Ok(obj(vec![("ack", Json::Null)])),
        Some((ack, cmd)) => {
            let enc = codec::enc_obj(&Obj::MAck(ack))?;
            Ok(obj(vec![("ack", obj(vec![("payload", enc), ("command_id", jstr(cmd))]))]))
        }
    }
}

pub fn decide(op: &str, text: &str) -> Result<String, crate::ledger::model::LErr> {
    let doc = crate::ledger::json::parse(text).map_err(|e| crate::ledger::model::LErr { kind: crate::tos_paper::WIRE, msg: format!("bad door json: {}", e.message) })?;
    let out = match op {
        "session_deferred_why" | "deferred_why" => deferred_why_op(&doc)?,
        "session_halt_unproven" | "halt_unproven" => halt_unproven_op(&doc)?,
        "session_clear_halt" | "clear_halt" => clear_halt_op(&doc)?,
        "session_reconcile_command_id" | "reconcile_command_id" => reconcile_command_id_op(&doc)?,
        "session_working_orders" | "working_orders" => working_orders_op(&doc)?,
        "session_pending_orders" | "pending_orders" => pending_orders_op(&doc)?,
        "session_morning_orders" | "morning_orders" => morning_orders_op(&doc)?,
        "session_classify_cover" | "classify_cover" => classify_cover_op(&doc)?,
        "session_prepare_write_ahead" | "prepare_write_ahead" => prepare_write_ahead_op(&doc)?,
        "session_build_drain_acks" | "build_drain_acks" => build_drain_acks_op(&doc)?,
        "session_build_cancel_ack" | "build_cancel_ack" => build_cancel_ack_op(&doc)?,
        _ => return err(crate::tos_paper::WIRE, format!("unknown tos_paper_session op {op:?}")),
    };
    Ok(crate::ledger::json::dumps(&out))
}

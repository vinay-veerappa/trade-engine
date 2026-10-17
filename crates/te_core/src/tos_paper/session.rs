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

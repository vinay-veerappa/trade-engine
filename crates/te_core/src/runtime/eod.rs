use super::{need, py_strip, Plan};
use crate::ledger::codec::py_repr;
use crate::ledger::model::{derr, err, LErr, R};
use crate::ledger::ops::*;
use crate::money::Money;
use num_bigint::BigUint;

fn dec(s: &str) -> R<Money> {
    Money::parse(s).ok_or_else(|| LErr { kind: "value", msg: "invalid runtime decimal".into() })
}
fn truncate(value: &Money) -> R<String> {
    if !value.is_finite() {
        return value.trunc_i128().map(|n| n.to_string()).map_err(derr);
    }
    let exponent = value.exponent();
    let digits = value.coefficient().to_str_radix(10);
    let coefficient = if exponent < 0 {
        let places = exponent.unsigned_abs();
        if places >= digits.len() as u64 { BigUint::from(0u8) }
        else { value.coefficient() / BigUint::from(10u8).pow(places as u32) }
    } else {
        value.coefficient() * BigUint::from(10u8).pow(u32::try_from(exponent)
            .map_err(|_| LErr { kind: "overflow", msg: "runtime integer exponent out of range".into() })?)
    };
    Ok(if value.is_negative() && coefficient != BigUint::from(0u8) {
        format!("-{coefficient}")
    } else { coefficient.to_string() })
}

// Typed rules. Each one returns what its string arm used to push, or the refusal it used to raise.
// The string door (`decide` below) only checks arity and forwards the inputs.

pub fn pass_job(name: &str, session: &str) -> String { format!("{name}-{session}") }

pub fn command(a: &str, b: &str, c: &str, d: &str) -> String { format!("{a}:{b}:{c}:{d}") }

/// Config checks, in the order the job config reports them.
pub fn config(job_name_set: bool, has_broker: bool, bars_age_set: bool, bars_max_age_seconds: f64,
    settle_set: bool, settle_delay: i64) -> R<()> {
    if !job_name_set { return err("runtime_eod", "job_name must be non-empty"); }
    if !has_broker { return err("runtime_eod", "at least one broker is required"); }
    // NaN compares false with everything, so `> 0.0` must be the accepted form
    if !bars_age_set || !(bars_max_age_seconds > 0.0) { return err("runtime_eod", "bars_max_age_seconds must be positive"); }
    if !settle_set || settle_delay < 0 { return err("runtime_eod", "settle_delay must be a non-negative timedelta"); }
    Ok(())
}

pub fn session_type(value: &str, is_date: bool) -> R<()> {
    if is_date { return Ok(()); }
    err("runtime_eod", format!("session must be a date, got {value}"))
}

pub fn session(session: &str, calendar: &str, is_session: bool) -> R<()> {
    if is_session { return Ok(()); }
    err("runtime_eod", format!("{session} is not a trading session of {calendar} (I5)"))
}

/// `text` is the pass as the caller wrote it, quoted in the refusal.
pub fn pass_name(name: &str, text: &str) -> R<()> {
    if ["morning", "midday", "late"].contains(&name) { return Ok(()); }
    err("runtime_eod", format!("Unknown pass {text}; the passes are morning, midday, late"))
}

pub fn through(text: &str, is_aware: bool) -> R<()> {
    if is_aware { return Ok(()); }
    err("runtime_eod", format!("through must be a timezone-aware datetime, got {text} (I7)"))
}

/// Passes must satisfy `lower <= at < upper`.
pub fn pass_boundary(pass: &str, session: &str, from: &str, to: &str, got: &str,
    lower: i64, at: i64, upper: i64) -> R<()> {
    if lower <= at && at < upper { return Ok(()); }
    err("runtime_eod", format!("The {pass} pass of {session} must end inside the session ({from} to {to}), got {got}"))
}

/// A pass must end after the previous pass: refuses `end <= previous_end`.
pub fn resume(pass: &str, session: &str, previous: &str, ran_to: &str, got: &str,
    end: i64, previous_end: i64) -> R<()> {
    if end > previous_end { return Ok(()); }
    err("runtime_eod", format!("The {pass} pass of {session} must end after the previous pass of '{previous}', which ran to {ran_to}; got {got} (I7)"))
}

pub fn cutoff(time: i64, limit: i64) -> bool { time <= limit }

/// Index of the latest time; on a tie the earliest index wins.
pub fn latest(times: &[i64]) -> Option<usize> {
    times.iter().enumerate()
        .max_by(|a, b| a.1.cmp(b.1).then_with(|| b.0.cmp(&a.0)))
        .map(|(i, _)| i)
}

/// Index of the earliest time; on a tie the first index wins.
pub fn earliest(times: &[i64]) -> Option<usize> {
    times.iter().enumerate().min_by_key(|(_, t)| *t).map(|(i, _)| i)
}

/// Rows are (kind, text) pairs; counts the rows of `kind` whose text starts with `prefix`.
pub fn count_events(kind: &str, prefix: &str, rows: &[String]) -> i64 {
    rows.chunks_exact(2).filter(|r| r[0] == kind && r[1].starts_with(prefix)).count() as i64
}

/// What the `children` arm reports: a per-row ownership flag, the first owned STOP row, and every owned LIMIT row.
pub struct Children { pub owned: Vec<bool>, pub stop: Option<i64>, pub limits: Vec<i64> }

/// `rows` are (parent, kind, id) triples.
pub fn children(parent: &str, rows: &[String]) -> Children {
    let mut stop = None;
    let mut limits = Vec::new();
    let mut owned = Vec::new();
    for (i, row) in rows.chunks_exact(3).enumerate() {
        owned.push(row[0] == parent);
        if row[0] == parent {
            if row[1] == "STOP" && stop.is_none() { stop = Some(i as i64); }
            if row[1] == "LIMIT" { limits.push(i as i64); }
        }
    }
    Children { owned, stop, limits }
}

pub fn terminal(state: &str) -> bool { ["FILLED", "CANCELLED", "EXPIRED", "REJECTED"].contains(&state) }

pub fn filled(states: &[String]) -> i64 { states.iter().filter(|value| *value == "FILLED").count() as i64 }

pub fn exit_action(action: &str, entry: &str, account: &str, kind: &str,
    open_bracket: bool, exit_kind: bool) -> R<()> {
    if !open_bracket {
        return err("runtime_eod", format!("Exit action '{action}' names '{entry}', which is not an open bracket of '{account}' (I8)"));
    }
    if !exit_kind {
        return err("runtime_eod", format!("Strategy for '{account}' returned {kind}; exit actions are MoveStop, ClosePosition or ReducePosition"));
    }
    Ok(())
}

/// Only an earlier session's marker is history: the same session, or a later one, is not.
pub fn history(has_history: bool, marker: &str, job: &str, marker_session: &str, run_session: &str) -> bool {
    has_history && marker_session < run_session
        && (marker == job || ["morning", "midday", "late"].iter().any(|v| marker == format!("{job}-{v}")))
}

/// `voided`: a void counts as complete (EOD-VOID-1).
pub fn previous(what: &str, account: &str, previous_session: &str, marker: &str,
    has_history: bool, ran: bool, voided: bool) -> R<()> {
    if has_history && !ran && !voided {
        return err("runtime_incomplete", format!("Cannot run {what} for '{account}': the previous session {previous_session} has no {marker} marker; complete it first (I3)"));
    }
    Ok(())
}

pub fn replay_clock(what: &str, clock: &str, open: &str, clock_at: i64, open_at: i64) -> R<()> {
    if clock_at <= open_at { return Ok(()); }
    err("runtime_eod", format!("Cannot replay {what}: the injected clock reads {clock}, past the session open {open}. Replay needs a clock it can advance bar by bar (I7); inject a replay clock positioned at or before the session open"))
}

pub fn nonzero(value: &str) -> R<bool> { Ok(!eq(&dec(value)?, &zero())?) }

pub fn working(state: &str) -> bool { ["SUBMITTED", "ACCEPTED", "PARTIALLY_FILLED", "PENDING_UNKNOWN"].contains(&state) }

pub fn after(time: i64, reference: i64) -> bool { time > reference }

pub fn any_history(kinds: &[String]) -> bool { kinds.iter().any(|kind| kind == "EodRun") }

pub fn difference(a: i64, b: i64) -> i64 { a - b }

/// Equity EOD replay can only value instruments it has a bar series for.
pub fn replay_instrument(symbol: &str, supported: bool) -> R<()> {
    if supported { return Ok(()); }
    err("runtime_eod", format!("Equity EOD replay cannot value {symbol}; option marks and the lifecycle pass are wired into the EOD run by O4 (I5)"))
}

pub fn rehydrate(account: &str, order: &str, state: &str, holds_working: bool,
    venue_filled: &str, ledger_filled: &str) -> R<()> {
    if !holds_working {
        return err("runtime_eod", format!("Venue for '{account}' does not hold working order '{order}' ({state}); refusing to replay a session it could never fill in (I5)"));
    }
    if lt(&dec(venue_filled)?, &dec(ledger_filled)?)? {
        return err("runtime_eod", format!("Venue reports {venue_filled} filled for '{order}' but the ledger records {ledger_filled} (I5)"));
    }
    Ok(())
}

/// `times` are [first bar, session open, last bar, session close]; `texts` are the same four as written.
/// The series must start at the open and end at the final minute (close - 1 minute).
pub fn bars(symbol: &str, has_bars: bool, times: [i64; 4], texts: [&str; 4]) -> R<()> {
    let [first, open, last, close] = times;
    let [first_text, open_text, last_text, final_text] = texts;
    if !has_bars {
        return err("runtime_replay", format!("No one-minute bars returned for {symbol} for this session; refusing to simulate from memory (I5)"));
    }
    if first != open {
        return err("runtime_replay", format!("Bar series for {symbol} starts at {first_text}, not the session open {open_text} (I5)"));
    }
    if last != close - 60_000_000 {
        return err("runtime_replay", format!("Bar series for {symbol} ends at {last_text}, not the session's final minute {final_text} (I5)"));
    }
    Ok(())
}

pub fn regular(start: i64, at: i64, end: i64) -> bool { start <= at && at < end }

/// Whether an open position is marked: `Ok(false)` for a flat position or one already marked.
pub fn mark(quantity: &str, already_marked: bool, has_regular_bar: bool, symbol: &str, account: &str) -> R<bool> {
    if eq(&dec(quantity)?, &zero())? || already_marked { return Ok(false); }
    if !has_regular_bar {
        return err("runtime_replay", format!("No regular-session bar for open position {symbol} in '{account}'; refusing to mark without one (I5)"));
    }
    Ok(true)
}

/// The `risk` arm's outputs.
pub struct Risk { pub equity_positive: bool, pub equity: String, pub gross: String, pub base: String, pub open_positions: i64 }

/// `rows` are (quantity, price) pairs; `held[i]` says whether pair `i` is marked into equity.
pub fn risk(equity_base: &str, base_text: &str, rows: &[String], held: &[bool]) -> R<Risk> {
    if held.len() < rows.len() / 2 { return err("value", "risk arity"); }
    let mut marked = zero();
    let mut gross = zero();
    let mut count = 0;
    for (i, row) in rows.chunks_exact(2).enumerate() {
        let q = dec(&row[0])?;
        if held[i] { marked = add(&marked, &mul(&q, &dec(&row[1])?)?)?; }
        gross = add(&gross, &mul(&q.abs().map_err(derr)?, &dec(&row[1])?)?)?;
        if !eq(&q, &zero())? { count += 1; }
    }
    let equity = add(&dec(equity_base)?, &marked)?;
    let equity_positive = gt(&equity, &zero())?;
    Ok(Risk {
        equity_positive,
        equity: crate::ledger::ops::s(&equity),
        gross: crate::ledger::ops::s(&gross),
        base: truncate(&dec(base_text)?)?,
        open_positions: count,
    })
}

/// The `bracket` arm's outputs: whether the bracket is still open, and its three text values when it is.
pub struct Bracket { pub open: bool, pub text: Vec<String> }

/// Filled minus exited quantity; `tail` holds (price, quantity) pairs used for the average exit price.
pub fn bracket(filled_rows: &[String], exited_rows: &[String], tail: &[String]) -> R<Bracket> {
    let filled_total = filled_rows.iter().try_fold(zero(), |a, v| add(&a, &dec(v)?))?;
    let exited_total = exited_rows.iter().try_fold(zero(), |a, v| add(&a, &dec(v)?))?;
    let open = sub(&filled_total, &exited_total)?;
    let is_open = gt(&open, &zero())?;
    if !is_open { return Ok(Bracket { open: false, text: Vec::new() }); }
    let amount = tail.chunks_exact(2)
        .try_fold(zero(), |a, row| add(&a, &mul(&dec(&row[0])?, &dec(&row[1])?)?))?;
    Ok(Bracket { open: true, text: vec![
        crate::ledger::ops::s(&filled_total),
        crate::ledger::ops::s(&open),
        crate::ledger::ops::s(&div(&amount, &filled_total)?),
    ] })
}

pub fn subtract(a: &str, b: &str) -> R<String> {
    Ok(crate::ledger::ops::s(&sub(&dec(a)?, &dec(b)?)?))
}

pub fn intent(intent_id: &str, target: &str, produced_for: &str) -> R<()> {
    if target == produced_for { return Ok(()); }
    err("runtime_eod", format!("Intent '{intent_id}' targets account '{target}' but was produced for '{produced_for}' (I8)"))
}

pub fn equity_approved(accepted: bool, has_quantity: bool) -> bool { accepted && has_quantity }

pub fn snapshots_wiring(accounts: &str, has_chain_snapshots: bool, has_settlements: bool) -> R<()> {
    if has_chain_snapshots && has_settlements { return Ok(()); }
    err("runtime_eod", format!("Options accounts {accounts} need chain_snapshots to fill and mark options and settlements to mark shares; neither may be defaulted (I5)"))
}

/// Snapshot times must satisfy `start <= at <= end`.
pub fn snapshot(returned: &str, present: bool, kind: &str, subject: &str, session: &str,
    start: i64, at: i64, end: i64) -> R<()> {
    if !present { return err("runtime_eod", format!("chain_snapshots returned {returned} (I5)")); }
    if start <= at && at <= end { return Ok(()); }
    err("runtime_replay", format!("{kind} snapshot of {subject} is outside the {session} session (I7)"))
}

/// `missing` is the underlying with no snapshot; an empty string means nothing is missing.
pub fn coverage(missing: &str, kind: &str, holder: &str) -> R<()> {
    if missing.is_empty() { return Ok(()); }
    err("runtime_replay", format!("No {kind} chain snapshot for {missing}, which '{holder}' holds options on or has orders working on; refusing to fill or mark them from memory (I5)"))
}

pub fn lifecycle(account: &str, holds_options: bool, has_lifecycle: bool) -> R<()> {
    if holds_options && !has_lifecycle {
        return err("runtime_eod", format!("{account} hold options and no lifecycle pass is configured; expiry and assignment cannot be decided (I9)"));
    }
    Ok(())
}

pub fn preopen(time: i64, open: i64) -> bool { time < open }

pub fn dividend_source(account: &str, has_source: bool) -> R<()> {
    if has_source { return Ok(()); }
    err("runtime_eod", format!("'{account}' holds shares and no dividend source is configured; a dividend going ex would be missed (I5)"))
}

pub fn dividend_stamp(account: &str, stamped_text: &str, stamped: i64, clock: i64) -> R<()> {
    if stamped <= clock { return Ok(()); }
    err("runtime_replay", format!("{account} dividend record is stamped {stamped_text}, after the clock: look-ahead (I7)"))
}

/// The `dividend` arm's outputs: whether a dividend is paid, and [per share, total] when it is.
pub struct Dividend { pub paid: bool, pub text: Vec<String> }

/// `per_share_amounts` are summed into the per-share dividend; `shares` multiplies it.
pub fn dividend(shares: &str, per_share_amounts: &[String]) -> R<Dividend> {
    let per_share = per_share_amounts.iter().try_fold(zero(), |a, v| add(&a, &dec(v)?))?;
    if eq(&per_share, &zero())? { return Ok(Dividend { paid: false, text: Vec::new() }); }
    Ok(Dividend { paid: true, text: vec![
        crate::ledger::ops::s(&per_share),
        crate::ledger::ops::s(&mul(&dec(shares)?, &per_share)?),
    ] })
}

pub fn option_mark(price: &str, has_quote: bool, kind: &str, symbol: &str, account: &str) -> R<()> {
    if !has_quote || !gt(&dec(price)?, &zero())? {
        return err("runtime_replay", format!("No usable {kind} quote for {symbol} in '{account}'; refusing to mark it (I5)"));
    }
    Ok(())
}

pub fn unmarkable(account: &str, holding: &str) -> R<()> {
    err("runtime_eod", format!("'{account}' holds {holding}, which cannot be marked (I6)"))
}

/// `asked` and `got` are the instrument names; `asked_close` and `got_close` the close labels; `got_session` the session the venue returned.
pub fn official_identity(asked: &str, got: &str, asked_close: &str, got_close: &str, got_session: &str) -> R<()> {
    if asked == got && asked_close == got_close && got_session == "PM" { return Ok(()); }
    err("runtime_replay", format!("Asked for the {asked_close} close of {asked}, got {got} {got_session} on {got_close} (I5)"))
}

pub fn official_future(kind: &str, stamped_text: &str, stamped: i64, clock: i64, clock_text: &str) -> R<()> {
    if stamped <= clock { return Ok(()); }
    err("runtime_replay", format!("{kind} close is stamped {stamped_text}, after the clock {clock_text}: look-ahead (I7)"))
}

pub fn official_close(kind: &str, stamped_text: &str, stamped: i64, session_close: i64) -> R<()> {
    if stamped >= session_close { return Ok(()); }
    err("runtime_replay", format!("{kind} close is stamped {stamped_text}, before the session closed; it cannot be the official close (I9)"))
}

pub fn sink(name: &str, has_publish: bool) -> R<()> {
    if has_publish { return Ok(()); }
    err("runtime_eod", format!("Sink for '{name}' does not declare a publish method (I5)"))
}

/// One account named by a void, with the ledger facts the caller looked up for it.
pub struct VoidAccount<'a> { pub name: &'a str, pub is_str: bool, pub known: bool, pub ran: bool }

/// A void request. `session_repr` is the session as Python repr'd it; `session` is its text.
pub struct VoidRequest<'a> {
    pub session_repr: &'a str,
    pub session: &'a str,
    pub is_date: bool,
    pub reason: &'a str,
    pub reason_is_str: bool,
    pub owner: &'a str,
    pub owner_is_str: bool,
    pub is_session: bool,
    pub accounts: &'a [VoidAccount<'a>],
}

/// Returns the stripped (reason, owner) the void records.
pub fn void_check(req: &VoidRequest) -> R<Vec<String>> {
    if !req.is_date {
        return err("runtime_eod", format!("void session must be a date, got {}", req.session_repr));
    }
    if !req.reason_is_str || py_strip(req.reason).is_empty() {
        return err("runtime_eod", "a void needs a non-empty reason (I5)");
    }
    if !req.owner_is_str || py_strip(req.owner).is_empty() {
        return err("runtime_eod", "a void needs a non-empty owner (I5)");
    }
    if !req.is_session {
        return err("runtime_eod", format!("{} is not a trading session; nothing to void (I5)", req.session));
    }
    let accounts = req.accounts;
    if accounts.is_empty() || accounts.iter().any(|a| !a.is_str || py_strip(a.name).is_empty()) {
        return err("runtime_eod", "a void needs at least one non-empty account (I5)");
    }
    let unknown: Vec<String> = accounts.iter().filter(|a| !a.known).map(|a| py_repr(a.name)).collect();
    if !unknown.is_empty() {
        return err("runtime_eod", format!("unknown account(s) [{}]: not in the ledger (I5)", unknown.join(", ")));
    }
    if let Some(a) = accounts.iter().find(|a| a.ran) {
        return err("runtime_eod", format!("{} already ran {}: a session that ran cannot be voided (I3)", a.name, req.session));
    }
    Ok(vec![py_strip(req.reason).to_string(), py_strip(req.owner).to_string()])
}

/// One child order of the order being written off: its id, state and venue order id.
pub struct WriteOffChild<'a> { pub id: &'a str, pub state: &'a str, pub venue: &'a str }

/// A write-off request for one working order.
pub struct WriteOff<'a> {
    pub reason: &'a str,
    pub reason_is_str: bool,
    pub owner: &'a str,
    pub owner_is_str: bool,
    pub account: &'a str,
    pub account_known: bool,
    /// The write-off already ran (an idempotent repeat).
    pub already: bool,
    pub order_exists: bool,
    pub order: &'a str,
    pub order_state: &'a str,
    pub order_filled: &'a str,
    /// The parent (protective) order id, empty when there is none.
    pub parent: &'a str,
    pub parent_filled: &'a str,
    pub order_venue: &'a str,
    pub children: &'a [WriteOffChild<'a>],
    pub venue_cancel_confirmed: bool,
}

/// `None` for an idempotent repeat; otherwise the text the write-off records (the flag is true).
pub fn writeoff_check(w: &WriteOff) -> R<Option<Vec<String>>> {
    if !w.reason_is_str || py_strip(w.reason).is_empty() {
        return err("runtime_eod", "a write-off needs a non-empty reason (I5)");
    }
    if !w.owner_is_str || py_strip(w.owner).is_empty() {
        return err("runtime_eod", "a write-off needs a non-empty owner (I5)");
    }
    if !w.account_known {
        return err("runtime_eod", format!("unknown account {}: not in the ledger (I5)", py_repr(w.account)));
    }
    if w.already { return Ok(None); }
    let order = w.order;
    if !w.order_exists {
        return err("runtime_eod", format!("{} has no order {} (I5)", w.account, py_repr(order)));
    }
    if !eq(&dec(w.order_filled)?, &zero())? {
        return err("runtime_eod", format!("order {} has fills: a filled or partly filled order is a position, not a write-off (I5)", py_repr(order)));
    }
    if !w.parent.is_empty() && !eq(&dec(w.parent_filled)?, &zero())? {
        return err("runtime_eod", format!("order {} protects {}, which has fills: writing it off would leave a live position unprotected (I5)", py_repr(order), py_repr(w.parent)));
    }
    if !matches!(w.order_state, "SUBMITTED" | "ACCEPTED" | "PENDING_UNKNOWN") {
        return err("runtime_eod", format!("order {} is {}, not working; nothing to write off (I5)", py_repr(order), w.order_state));
    }
    let kept: Vec<&WriteOffChild> = w.children.iter()
        .filter(|c| !matches!(c.state, "FILLED" | "CANCELLED" | "EXPIRED" | "REJECTED"))
        .collect();
    if !w.venue_cancel_confirmed {
        let targets = std::iter::once((order, w.order_venue)).chain(kept.iter().map(|c| (c.id, c.venue)));
        for (id, venue) in targets {
            if !venue.is_empty() {
                return err("runtime_eod", format!("order {} has venue order id {}: the venue may still hold it. Cancel it at the venue first, then pass --venue-cancel-confirmed (I5)", py_repr(id), py_repr(venue)));
            }
        }
    }
    let mut text = vec![format!("Owner write-off: {} (owner: {})", py_strip(w.reason), py_strip(w.owner)), order.to_string()];
    text.extend(kept.iter().map(|c| c.id.to_string()));
    Ok(Some(text))
}

/// `snapshot_as_of` and `quote_as_of` are the two timestamps as text; `age_micros` is snapshot minus quote, in microseconds.
/// The quote is stale only when its age is strictly over `limit_seconds`.
pub fn quote_source(snapshot_as_of: &str, quote_as_of: &str, age_micros: i64, limit_seconds: f64) -> String {
    let age = age_micros as f64 / 1_000_000.0;
    let source = format!("snapshot:{snapshot_as_of}");
    if age > limit_seconds { format!("stale-quote:{quote_as_of} {source}") } else { source }
}

pub fn decide(op: &str, s: &[String], n: &[i64], b: &[bool], f: &[f64]) -> R<Plan> {
    let mut p = Plan::default();
    match op {
        "pass_job" => {
            need(op, s, n, b, f, (2, 0, 0, 0))?;
            p.text.push(pass_job(&s[0], &s[1]));
        }
        "command" => {
            need(op, s, n, b, f, (4, 0, 0, 0))?;
            p.text.push(command(&s[0], &s[1], &s[2], &s[3]));
        }
        "config" => {
            need(op, s, n, b, f, (0, 1, 4, 1))?;
            config(b[0], b[1], b[2], f[0], b[3], n[0])?;
        }
        "session_type" => {
            need(op, s, n, b, f, (1, 0, 1, 0))?;
            session_type(&s[0], b[0])?;
        }
        "session" => {
            need(op, s, n, b, f, (2, 0, 1, 0))?;
            session(&s[0], &s[1], b[0])?;
        }
        "pass_name" => {
            need(op, s, n, b, f, (2, 0, 0, 0))?;
            pass_name(&s[0], &s[1])?;
        }
        "through" => {
            need(op, s, n, b, f, (1, 0, 1, 0))?;
            through(&s[0], b[0])?;
        }
        "pass_boundary" => {
            need(op, s, n, b, f, (5, 3, 0, 0))?;
            pass_boundary(&s[0], &s[1], &s[2], &s[3], &s[4], n[0], n[1], n[2])?;
        }
        "resume" => {
            need(op, s, n, b, f, (5, 2, 0, 0))?;
            resume(&s[0], &s[1], &s[2], &s[3], &s[4], n[0], n[1])?;
        }
        "cutoff" => {
            need(op, s, n, b, f, (0, 2, 0, 0))?;
            p.flags.push(cutoff(n[0], n[1]));
        }
        "latest" => {
            need(op, s, n, b, f, (0, 0, 0, 0))?;
            if let Some(i) = latest(n) { p.numbers.push(i as i64); }
        }
        "earliest" => {
            need(op, s, n, b, f, (0, 0, 0, 0))?;
            if let Some(i) = earliest(n) { p.numbers.push(i as i64); }
        }
        "count_events" => {
            need(op, s, n, b, f, (2, 0, 0, 0))?;
            p.numbers.push(count_events(&s[0], &s[1], &s[2..]));
        }
        "children" => {
            need(op, s, n, b, f, (1, 0, 0, 0))?;
            let c = children(&s[0], &s[1..]);
            p.flags = c.owned;
            p.numbers.push(c.stop.unwrap_or(-1));
            p.numbers.extend(c.limits);
        }
        "terminal" => {
            need(op, s, n, b, f, (1, 0, 0, 0))?;
            p.flags.push(terminal(&s[0]));
        }
        "filled" => {
            need(op, s, n, b, f, (0, 0, 0, 0))?;
            p.numbers.push(filled(s));
        }
        "exit_action" => {
            need(op, s, n, b, f, (4, 0, 2, 0))?;
            exit_action(&s[0], &s[1], &s[2], &s[3], b[0], b[1])?;
        }
        "history" => {
            need(op, s, n, b, f, (4, 0, 1, 0))?;
            p.flags.push(history(b[0], &s[0], &s[1], &s[2], &s[3]));
        }
        "previous" => {
            need(op, s, n, b, f, (4, 0, 2, 0))?;
            previous(&s[0], &s[1], &s[2], &s[3], b[0], b[1], b.get(2).copied().unwrap_or(false))?;
        }
        "replay_clock" => {
            need(op, s, n, b, f, (3, 2, 0, 0))?;
            replay_clock(&s[0], &s[1], &s[2], n[0], n[1])?;
        }
        "nonzero" => {
            need(op, s, n, b, f, (1, 0, 0, 0))?;
            p.flags.push(nonzero(&s[0])?);
        }
        "working" => {
            need(op, s, n, b, f, (1, 0, 0, 0))?;
            p.flags.push(working(&s[0]));
        }
        "after" => {
            need(op, s, n, b, f, (0, 2, 0, 0))?;
            p.flags.push(after(n[0], n[1]));
        }
        "any_history" => {
            need(op, s, n, b, f, (0, 0, 0, 0))?;
            p.flags.push(any_history(s));
        }
        "difference" => {
            need(op, s, n, b, f, (0, 2, 0, 0))?;
            p.numbers.push(difference(n[0], n[1]));
        }
        "replay_instrument" => {
            need(op, s, n, b, f, (1, 0, 1, 0))?;
            replay_instrument(&s[0], b[0])?;
        }
        "rehydrate" => {
            need(op, s, n, b, f, (5, 0, 1, 0))?;
            rehydrate(&s[0], &s[1], &s[2], b[0], &s[3], &s[4])?;
        }
        "bars" => {
            need(op, s, n, b, f, (5, 4, 1, 0))?;
            bars(&s[0], b[0], [n[0], n[1], n[2], n[3]],
                [s[1].as_str(), s[2].as_str(), s[3].as_str(), s[4].as_str()])?;
        }
        "regular" => {
            need(op, s, n, b, f, (0, 3, 0, 0))?;
            p.flags.push(regular(n[0], n[1], n[2]));
        }
        "mark" => {
            need(op, s, n, b, f, (3, 0, 2, 0))?;
            p.flags.push(mark(&s[0], b[0], b[1], &s[1], &s[2])?);
        }
        "risk" => {
            need(op, s, n, b, f, (2, 0, 0, 0))?;
            let r = risk(&s[0], &s[1], &s[2..], b)?;
            p.flags.push(r.equity_positive);
            p.text = vec![r.equity, r.gross, r.base];
            p.numbers.push(r.open_positions);
        }
        "bracket" => {
            need(op, s, n, b, f, (0, 2, 0, 0))?;
            let (start, end) = (n[0], n[1]);
            if start < 0 || end < start || end > s.len() as i64 { return err("value", "bracket arity"); }
            let (start, end) = (start as usize, end as usize);
            let r = bracket(&s[..start], &s[start..end], &s[end..])?;
            p.flags.push(r.open);
            p.text = r.text;
        }
        "subtract" => {
            need(op, s, n, b, f, (2, 0, 0, 0))?;
            p.text.push(subtract(&s[0], &s[1])?);
        }
        "intent" => {
            need(op, s, n, b, f, (3, 0, 0, 0))?;
            intent(&s[2], &s[0], &s[1])?;
        }
        "equity_approved" => {
            need(op, s, n, b, f, (0, 0, 2, 0))?;
            p.flags.push(equity_approved(b[0], b[1]));
        }
        "snapshots_wiring" => {
            need(op, s, n, b, f, (1, 0, 2, 0))?;
            snapshots_wiring(&s[0], b[0], b[1])?;
        }
        "snapshot" => {
            need(op, s, n, b, f, (4, 3, 1, 0))?;
            snapshot(&s[0], b[0], &s[1], &s[2], &s[3], n[0], n[1], n[2])?;
        }
        "coverage" => {
            need(op, s, n, b, f, (3, 0, 0, 0))?;
            coverage(&s[0], &s[1], &s[2])?;
        }
        "lifecycle" => {
            need(op, s, n, b, f, (1, 0, 2, 0))?;
            lifecycle(&s[0], b[0], b[1])?;
        }
        "preopen" => {
            need(op, s, n, b, f, (0, 2, 0, 0))?;
            p.flags.push(preopen(n[0], n[1]));
        }
        "dividend_source" => {
            need(op, s, n, b, f, (1, 0, 1, 0))?;
            dividend_source(&s[0], b[0])?;
        }
        "dividend_stamp" => {
            need(op, s, n, b, f, (2, 2, 0, 0))?;
            dividend_stamp(&s[0], &s[1], n[0], n[1])?;
        }
        "dividend" => {
            need(op, s, n, b, f, (1, 0, 0, 0))?;
            let d = dividend(&s[0], &s[1..])?;
            p.flags.push(d.paid);
            p.text = d.text;
        }
        "option_mark" => {
            need(op, s, n, b, f, (4, 0, 1, 0))?;
            option_mark(&s[0], b[0], &s[1], &s[2], &s[3])?;
        }
        "unmarkable" => {
            need(op, s, n, b, f, (2, 0, 0, 0))?;
            unmarkable(&s[0], &s[1])?;
        }
        "official_identity" => {
            need(op, s, n, b, f, (5, 0, 0, 0))?;
            official_identity(&s[0], &s[1], &s[2], &s[3], &s[4])?;
        }
        "official_future" => {
            need(op, s, n, b, f, (3, 2, 0, 0))?;
            official_future(&s[0], &s[1], n[0], n[1], &s[2])?;
        }
        "official_close" => {
            need(op, s, n, b, f, (2, 2, 0, 0))?;
            official_close(&s[0], &s[1], n[0], n[1])?;
        }
        "sink" => {
            need(op, s, n, b, f, (1, 0, 1, 0))?;
            sink(&s[0], b[0])?;
        }
        "void_check" => {
            need(op, s, n, b, f, (4, 0, 4, 0))?;
            if b.len() != 4 + 3 * (s.len() - 4) { return err("value", "void_check arity"); }
            let accounts: Vec<VoidAccount> = s[4..].iter().enumerate().map(|(i, name)| VoidAccount {
                name: name.as_str(), is_str: b[4 + 3 * i], known: b[5 + 3 * i], ran: b[6 + 3 * i],
            }).collect();
            p.text = void_check(&VoidRequest {
                session_repr: s[0].as_str(), session: s[1].as_str(), is_date: b[0],
                reason: s[2].as_str(), reason_is_str: b[1], owner: s[3].as_str(), owner_is_str: b[2],
                is_session: b[3], accounts: &accounts,
            })?;
        }
        "writeoff_check" => {
            need(op, s, n, b, f, (9, 0, 6, 0))?;
            if (s.len() - 9) % 3 != 0 || b.len() != 6 { return err("value", "writeoff_check arity"); }
            let children: Vec<WriteOffChild> = (0..(s.len() - 9) / 3).map(|i| WriteOffChild {
                id: s[9 + 3 * i].as_str(), state: s[10 + 3 * i].as_str(), venue: s[11 + 3 * i].as_str(),
            }).collect();
            match writeoff_check(&WriteOff {
                reason: s[0].as_str(), reason_is_str: b[0], owner: s[1].as_str(), owner_is_str: b[1],
                account: s[2].as_str(), account_known: b[2], already: b[3], order_exists: b[4],
                order: s[3].as_str(), order_state: s[4].as_str(), order_filled: s[5].as_str(),
                parent: s[6].as_str(), parent_filled: s[7].as_str(), order_venue: s[8].as_str(),
                children: &children, venue_cancel_confirmed: b[5],
            })? {
                None => p.flags.push(false),
                Some(text) => {
                    p.flags.push(true);
                    p.text = text;
                }
            }
        }
        "quote_source" => {
            need(op, s, n, b, f, (2, 1, 0, 1))?;
            if s.len() != 2 || n.len() != 1 || f.len() != 1 { return err("value", "quote_source arity"); }
            p.text.push(quote_source(&s[0], &s[1], n[0], f[0]));
        }
        _ => return err("value", format!("Unknown EOD decision {op}")),
    }
    Ok(p)
}


#[cfg(test)]
mod tests {
    use super::*;
    use proptest::prelude::*;
    #[test]
    fn previous_rule_treats_a_void_as_complete() {
        let s: Vec<String> = ["eod", "ACC", "2026-10-16", "eod"].iter().map(|v| v.to_string()).collect();
        // history, previous never ran, not voided: refused (I3).
        assert!(decide("previous", &s, &[], &[true, false, false], &[]).is_err());
        // the legacy two-flag shape still refuses.
        assert!(decide("previous", &s, &[], &[true, false], &[]).is_err());
        // history, previous never ran, but the owner voided it: complete.
        assert!(decide("previous", &s, &[], &[true, false, true], &[]).is_ok());
        // previous ran: complete, voided or not.
        assert!(decide("previous", &s, &[], &[true, true, false], &[]).is_ok());
        assert!(decide("previous", &s, &[], &[true, true, true], &[]).is_ok());
        // no history: nothing to demand.
        assert!(decide("previous", &s, &[], &[false, false, false], &[]).is_ok());
    }
    #[test]
    fn boundary_and_refusal() {
        assert!(decide("cutoff",&[],&[5,5],&[],&[]).unwrap().flags[0]);
        assert!(decide("preopen",&[],&[4,5],&[],&[]).unwrap().flags[0]);
        assert!(!decide("preopen",&[],&[5,5],&[],&[]).unwrap().flags[0]);
        assert!(decide("config",&[],&[0],&[false,true,true,true],&[1.0]).is_err());
    }
    fn strs(v: &[&str]) -> Vec<String> { v.iter().map(|x| x.to_string()).collect() }
    /// (kind, message) of a refusal; panics on success.
    fn refusal(r: R<Plan>) -> (&'static str, String) {
        match r { Err(e) => (e.kind, e.msg), Ok(_) => panic!("expected a refusal") }
    }
    const DATE_REPR: &str = "datetime.date(2026, 9, 23)";
    const SESSION: &str = "2026-09-23";
    /// void_check with the session a trading date unless `is_session` says otherwise.
    fn void_check(is_session: bool, reason: &str, owner: &str, accounts: &[(&str, bool, bool)]) -> R<Plan> {
        let mut s = strs(&[DATE_REPR, SESSION, reason, owner]);
        let mut b = vec![true, true, true, is_session];
        for (account, known, ran) in accounts {
            s.push(account.to_string());
            b.extend([true, *known, *ran]);
        }
        decide("void_check", &s, &[], &b, &[])
    }
    /// writeoff_check on an ACCEPTED order `o1` of `acct` with no fills, no parent, no venue id.
    /// `children` are (id, state, venue) triples in insertion order.
    fn writeoff_check(children: &[(&str, &str, &str)]) -> Vec<String> {
        let mut s = strs(&["reason", "owner", "acct", "o1", "ACCEPTED", "0", "", "0", ""]);
        for (id, state, venue) in children {
            s.extend(strs(&[*id, *state, *venue]));
        }
        let b = [true, true, true, false, true, false];
        decide("writeoff_check", &s, &[], &b, &[]).map(|p| p.text).unwrap_or_else(|e| panic!("{}", e.msg))
    }
    #[test]
    fn void_check_refuses_each_rule_with_its_message() {
        let (kind, msg) = refusal(decide("void_check", &strs(&[DATE_REPR, SESSION, "r", "o"]), &[], &[false, true, true, true], &[]));
        assert_eq!((kind, msg.as_str()), ("runtime_eod", "void session must be a date, got datetime.date(2026, 9, 23)"));
        assert_eq!(refusal(void_check(true, " ", "o", &[("A", true, false)])).1, "a void needs a non-empty reason (I5)");
        assert_eq!(refusal(void_check(true, "r", "\t", &[("A", true, false)])).1, "a void needs a non-empty owner (I5)");
        assert_eq!(refusal(void_check(false, "r", "o", &[("A", true, false)])).1, "2026-09-23 is not a trading session; nothing to void (I5)");
        assert_eq!(refusal(void_check(true, "r", "o", &[])).1, "a void needs at least one non-empty account (I5)");
        assert_eq!(refusal(void_check(true, "r", "o", &[("  ", true, false)])).1, "a void needs at least one non-empty account (I5)");
        assert_eq!(refusal(void_check(true, "r", "o", &[("A", true, false), ("B", true, true)])).1,
            "B already ran 2026-09-23: a session that ran cannot be voided (I3)");
    }
    #[test]
    fn void_check_lists_unknown_accounts_in_python_repr() {
        let (kind, msg) = refusal(void_check(true, "r", "o", &[("X", false, false), ("a'b", false, false)]));
        assert_eq!(kind, "runtime_eod");
        assert_eq!(msg, "unknown account(s) ['X', \"a'b\"]: not in the ledger (I5)");
    }
    #[test]
    fn void_check_success_returns_stripped_reason_and_owner() {
        let p = void_check(true, "  why\t", "\u{1c}me\u{1f}", &[("A", true, false), ("B", true, false)]).unwrap_or_else(|e| panic!("{}", e.msg));
        assert_eq!(p.text, vec!["why".to_string(), "me".to_string()]);
    }
    #[test]
    fn void_check_counts_information_separators_as_blank() {
        assert_eq!(refusal(void_check(true, "\u{1c}", "o", &[("A", true, false)])).1, "a void needs a non-empty reason (I5)");
        assert_eq!(refusal(void_check(true, "r", "\u{1d}\u{1e}\u{1f}", &[("A", true, false)])).1, "a void needs a non-empty owner (I5)");
    }
    #[test]
    fn void_check_arity_is_refused() {
        assert_eq!(refusal(decide("void_check", &strs(&[DATE_REPR, SESSION, "r"]), &[], &[true, true, true], &[])).1, "void_check arity");
        // one account needs three flags, not two
        assert_eq!(refusal(decide("void_check", &strs(&[DATE_REPR, SESSION, "r", "o", "A"]), &[], &[true, true, true, true, true, true], &[])).1, "void_check arity");
    }
    #[test]
    fn writeoff_check_refuses_each_rule_with_its_message() {
        let base = strs(&["reason", "owner", "acct", "o1", "ACCEPTED", "0", "", "0", ""]);
        let run = |s: &[&str], b: &[bool]| refusal(decide("writeoff_check", &strs(s), &[], b, &[]));
        let ok = [true, true, true, false, true, false];
        assert_eq!(run(&["", "owner", "acct", "o1", "ACCEPTED", "0", "", "0", ""], &ok).1, "a write-off needs a non-empty reason (I5)");
        assert_eq!(run(&["reason", "\u{1c}", "acct", "o1", "ACCEPTED", "0", "", "0", ""], &ok).1, "a write-off needs a non-empty owner (I5)");
        assert_eq!(decide("writeoff_check", &base, &[], &[true, true, false, false, true, false], &[]).err().map(|e| e.msg).as_deref(),
            Some("unknown account 'acct': not in the ledger (I5)"));
        assert_eq!(decide("writeoff_check", &strs(&["reason", "owner", "a'b", "o1", "", "0", "", "0", ""]), &[], &[true, true, false, false, true, false], &[])
            .err().map(|e| e.msg).as_deref(), Some("unknown account \"a'b\": not in the ledger (I5)"));
        assert_eq!(run(&["reason", "owner", "acct", "o1", "", "0", "", "0", ""], &[true, true, true, false, false, false]).1,
            "acct has no order 'o1' (I5)");
        assert_eq!(run(&["reason", "owner", "acct", "o1", "ACCEPTED", "1", "", "0", ""], &ok).1,
            "order 'o1' has fills: a filled or partly filled order is a position, not a write-off (I5)");
        assert_eq!(run(&["reason", "owner", "acct", "o1", "ACCEPTED", "0", "p1", "2", ""], &ok).1,
            "order 'o1' protects 'p1', which has fills: writing it off would leave a live position unprotected (I5)");
        assert_eq!(run(&["reason", "owner", "acct", "o1", "FILLED", "0", "", "0", ""], &ok).1,
            "order 'o1' is FILLED, not working; nothing to write off (I5)");
        assert_eq!(run(&["reason", "owner", "acct", "o1", "EXPIRED", "0", "", "0", ""], &ok).1,
            "order 'o1' is EXPIRED, not working; nothing to write off (I5)");
        assert_eq!(run(&["reason", "owner", "acct", "o1", "ACCEPTED", "0", "", "0", "v1"], &[true, true, true, false, true, false]).1,
            "order 'o1' has venue order id 'v1': the venue may still hold it. Cancel it at the venue first, then pass --venue-cancel-confirmed (I5)");
    }
    #[test]
    fn writeoff_check_already_is_an_idempotent_repeat_even_with_blank_order_fields() {
        let s = strs(&["reason", "owner", "acct", "o1", "", "0", "", "0", ""]);
        let p = decide("writeoff_check", &s, &[], &[true, true, true, true, false, false], &[]).unwrap_or_else(|e| panic!("{}", e.msg));
        assert_eq!(p.flags, vec![false]);
        assert!(p.text.is_empty());
    }
    #[test]
    fn writeoff_check_partially_filled_with_no_fills_is_not_working() {
        let s = strs(&["reason", "owner", "acct", "o1", "PARTIALLY_FILLED", "0", "", "0", ""]);
        let (kind, msg) = refusal(decide("writeoff_check", &s, &[], &[true, true, true, false, true, false], &[]));
        assert_eq!(kind, "runtime_eod");
        assert_eq!(msg, "order 'o1' is PARTIALLY_FILLED, not working; nothing to write off (I5)");
    }
    #[test]
    fn writeoff_check_keeps_non_terminal_children_in_insertion_order() {
        let text = writeoff_check(&[("c1", "ACCEPTED", ""), ("c2", "FILLED", ""), ("c3", "PARTIALLY_FILLED", ""),
            ("c4", "CANCELLED", ""), ("c5", "SUBMITTED", "")]);
        assert_eq!(text, strs(&["Owner write-off: reason (owner: owner)", "o1", "c1", "c3", "c5"]));
    }
    #[test]
    fn writeoff_check_refuses_a_venue_id_on_a_live_child_but_not_a_terminal_one() {
        let (kind, msg) = refusal(decide("writeoff_check",
            &strs(&["reason", "owner", "acct", "o1", "ACCEPTED", "0", "", "0", "", "c1", "ACCEPTED", "v9"]),
            &[], &[true, true, true, false, true, false], &[]));
        assert_eq!(kind, "runtime_eod");
        assert_eq!(msg, "order 'c1' has venue order id 'v9': the venue may still hold it. Cancel it at the venue first, then pass --venue-cancel-confirmed (I5)");
        let text = writeoff_check(&[("c2", "FILLED", "v8")]);
        assert_eq!(text, strs(&["Owner write-off: reason (owner: owner)", "o1"]));
    }
    #[test]
    fn writeoff_check_venue_cancel_confirmed_skips_the_venue_checks() {
        let s = strs(&["reason", "owner", "acct", "o1", "ACCEPTED", "0", "", "0", "v1", "c1", "ACCEPTED", "v9"]);
        let p = decide("writeoff_check", &s, &[], &[true, true, true, false, true, true], &[]).unwrap_or_else(|e| panic!("{}", e.msg));
        assert_eq!(p.flags, vec![true]);
        assert_eq!(p.text, strs(&["Owner write-off: reason (owner: owner)", "o1", "c1"]));
    }
    #[test]
    fn writeoff_check_an_empty_parent_skips_the_parent_check() {
        let s = strs(&["reason", "owner", "acct", "o1", "ACCEPTED", "0", "", "5", ""]);
        let p = decide("writeoff_check", &s, &[], &[true, true, true, false, true, false], &[]).unwrap_or_else(|e| panic!("{}", e.msg));
        assert_eq!(p.flags, vec![true]);
    }
    #[test]
    fn writeoff_check_arity_is_refused() {
        let b = [true, true, true, false, true, false];
        assert_eq!(refusal(decide("writeoff_check", &strs(&["r", "o", "a", "o1", "ACCEPTED", "0", "", "0"]), &[], &b, &[])).1, "writeoff_check arity");
        // a child needs three fields: this one has two
        assert_eq!(refusal(decide("writeoff_check", &strs(&["r", "o", "a", "o1", "ACCEPTED", "0", "", "0", "", "c1", "ACCEPTED"]), &[], &b, &[])).1, "writeoff_check arity");
        assert_eq!(refusal(decide("writeoff_check", &strs(&["r", "o", "a", "o1", "ACCEPTED", "0", "", "0", ""]), &[], &b[..5], &[])).1, "writeoff_check arity");
    }
    #[test]
    fn quote_source_is_stale_only_strictly_over_the_limit() {
        let run = |micros: i64| decide("quote_source", &strs(&["Q1", "Q0"]), &[micros], &[], &[900.0]).unwrap_or_else(|e| panic!("{}", e.msg)).text;
        // age equal to the limit is not stale
        assert_eq!(run(900_000_000), strs(&["snapshot:Q1"]));
        // one microsecond over the limit is stale
        assert_eq!(run(900_000_001), strs(&["stale-quote:Q0 snapshot:Q1"]));
        // a negative age is not stale
        assert_eq!(run(-5_000_000), strs(&["snapshot:Q1"]));
    }
    #[test]
    fn quote_source_short_input_refuses_as_value() {
        // one string, no micros, no limit: the arity guard refuses instead of panicking
        let (kind, msg) = refusal(decide("quote_source", &strs(&["Q1"]), &[], &[], &[]));
        assert_eq!((kind, msg.as_str()), ("value", "quote_source arity"));
    }
    #[test]
    fn config_refuses_nan_bar_age() {
        // NaN compares false with everything, so `> 0.0` must be the accepted form
        let (kind, msg) = refusal(decide("config", &[], &[0], &[true, true, true, true], &[f64::NAN]));
        assert_eq!((kind, msg.as_str()), ("runtime_eod", "bars_max_age_seconds must be positive"));
        assert!(decide("config", &[], &[0], &[true, true, true, true], &[1.0]).is_ok());
    }
    proptest! {
        #[test]
        fn dividend_product(q in -10000i64..10000, a in 1i64..10000) {
            let s = vec![q.to_string(),a.to_string()];
            let p = decide("dividend",&s,&[],&[],&[]).unwrap();
            prop_assert_eq!(&p.text[1], &(q*a).to_string());
        }
    }
}

#[cfg(test)]
mod typed_tests {
    use super::*;

    const OPS: [&str; 52] = ["pass_job", "command", "config", "session_type", "session", "pass_name", "through",
        "pass_boundary", "resume", "cutoff", "latest", "earliest", "count_events", "children", "terminal", "filled",
        "exit_action", "history", "previous", "replay_clock", "nonzero", "working", "after", "any_history",
        "difference", "replay_instrument", "rehydrate", "bars", "regular", "mark", "risk", "bracket", "subtract",
        "intent", "equity_approved", "snapshots_wiring", "snapshot", "coverage", "lifecycle", "preopen",
        "dividend_source", "dividend_stamp", "dividend", "option_mark", "unmarkable", "official_identity",
        "official_future", "official_close", "sink", "void_check", "writeoff_check", "quote_source"];
    /// Ops whose string adapter reads no inputs, so an empty call must not be refused for arity.
    const NO_MINIMUM: [&str; 4] = ["latest", "earliest", "filled", "any_history"];

    fn refusal<T>(r: R<T>) -> (&'static str, String) {
        match r {
            Ok(_) => panic!("expected a refusal"),
            Err(e) => (e.kind, e.msg),
        }
    }

    #[test]
    fn every_op_refuses_short_input_with_arity_unless_it_reads_nothing() {
        assert_eq!(OPS.len(), 52);
        for op in OPS {
            let r = decide(op, &[], &[], &[], &[]);
            if NO_MINIMUM.contains(&op) {
                if let Err(e) = r { assert_ne!(e.msg, format!("{op} arity"), "op {op}"); }
            } else {
                assert_eq!(refusal(r), ("value", format!("{op} arity")), "op {op}");
            }
        }
    }

    #[test]
    fn no_op_panics_on_any_input_shape() {
        let fills = ["1", "x", ""];
        for ls in 0..=10 {
            for ln in 0..=3 {
                for lb in 0..=7 {
                    for lf in 0..=2 {
                        for fill in fills {
                            let s = vec![fill.to_string(); ls];
                            let n = vec![1i64; ln];
                            let b = vec![true; lb];
                            let f = vec![1.0f64; lf];
                            for op in OPS.into_iter().chain(["bogus"]) {
                                let _ = decide(op, &s, &n, &b, &f);
                            }
                        }
                    }
                }
            }
        }
    }

    #[test]
    fn positive_is_gone() {
        assert_eq!(refusal(decide("positive", &[], &[], &[], &[])),
            ("value", "Unknown EOD decision positive".to_string()));
    }

    #[test]
    fn session_rules() {
        assert!(session_type("2026-01-02", true).is_ok());
        assert_eq!(refusal(session_type("x", false)), ("runtime_eod", "session must be a date, got x".to_string()));
        assert!(session("S", "CME", true).is_ok());
        assert_eq!(refusal(session("S", "CME", false)),
            ("runtime_eod", "S is not a trading session of CME (I5)".to_string()));
    }

    #[test]
    fn pass_name_and_through() {
        assert!(pass_name("midday", "'midday'").is_ok());
        assert_eq!(refusal(pass_name("noon", "'noon'")),
            ("runtime_eod", "Unknown pass 'noon'; the passes are morning, midday, late".to_string()));
        assert!(through("t", true).is_ok());
        assert_eq!(refusal(through("naive", false)),
            ("runtime_eod", "through must be a timezone-aware datetime, got naive (I7)".to_string()));
    }

    #[test]
    fn pass_boundary_is_lower_inclusive_upper_exclusive() {
        assert!(pass_boundary("late", "S", "F", "T", "G", 10, 10, 20).is_ok());
        assert!(pass_boundary("late", "S", "F", "T", "G", 10, 19, 20).is_ok());
        assert_eq!(refusal(pass_boundary("late", "S", "F", "T", "G", 10, 20, 20)),
            ("runtime_eod", "The late pass of S must end inside the session (F to T), got G".to_string()));
    }

    #[test]
    fn resume_needs_a_later_end() {
        assert!(resume("midday", "S", "morning", "R", "G", 21, 20).is_ok());
        assert_eq!(refusal(resume("midday", "S", "morning", "R", "G", 20, 20)),
            ("runtime_eod", "The midday pass of S must end after the previous pass of 'morning', which ran to R; got G (I7)".to_string()));
    }

    #[test]
    fn cutoff_and_ordering() {
        assert!(cutoff(5, 5));
        assert!(!cutoff(6, 5));
        assert_eq!(latest(&[1, 3, 3]), Some(1));
        assert_eq!(latest(&[]), None);
        assert!(after(5, 4));
        assert!(!after(4, 4));
        assert!(preopen(9, 10));
        assert!(!preopen(10, 10));
        assert!(regular(10, 10, 20));
        assert!(!regular(10, 20, 20));
        assert_eq!(difference(5, 3), 2);
    }

    #[test]
    fn status_predicates() {
        assert!(terminal("FILLED"));
        assert!(!terminal("SUBMITTED"));
        assert!(working("SUBMITTED"));
        assert!(!working("FILLED"));
        assert_eq!(filled(&["FILLED".to_string(), "X".to_string(), "FILLED".to_string()]), 2);
        assert!(equity_approved(true, true));
        assert!(!equity_approved(true, false));
    }

    #[test]
    fn nonzero_parses_or_refuses() {
        assert!(!nonzero("0").unwrap());
        assert!(nonzero("1").unwrap());
        assert_eq!(refusal(nonzero("x")), ("value", "invalid runtime decimal".to_string()));
    }

    #[test]
    fn exit_action_needs_an_open_bracket_and_an_exit_kind() {
        assert!(exit_action("ACT", "E", "A", "K", true, true).is_ok());
        assert_eq!(refusal(exit_action("ACT", "E", "A", "K", false, true)),
            ("runtime_eod", "Exit action 'ACT' names 'E', which is not an open bracket of 'A' (I8)".to_string()));
        assert_eq!(refusal(exit_action("ACT", "E", "A", "K", true, false)),
            ("runtime_eod", "Strategy for 'A' returned K; exit actions are MoveStop, ClosePosition or ReducePosition".to_string()));
    }

    #[test]
    fn history_is_only_an_earlier_session() {
        assert!(history(true, "job-morning", "job", "2026-01-01", "2026-01-02"));
        assert!(!history(true, "job-morning", "job", "2026-01-02", "2026-01-02"));
        assert!(!history(true, "other", "job", "2026-01-01", "2026-01-02"));
        assert!(!history(false, "job", "job", "2026-01-01", "2026-01-02"));
    }

    #[test]
    fn previous_session_must_be_complete_unless_voided() {
        assert!(previous("EOD run", "A", "2026-01-01", "eod", true, true, false).is_ok());
        assert!(previous("EOD run", "A", "2026-01-01", "eod", true, false, true).is_ok());
        assert_eq!(refusal(previous("EOD run", "A", "2026-01-01", "eod", true, false, false)),
            ("runtime_incomplete", "Cannot run EOD run for 'A': the previous session 2026-01-01 has no eod marker; complete it first (I3)".to_string()));
    }

    #[test]
    fn replay_instrument_and_rehydrate() {
        assert!(replay_instrument("ES", true).is_ok());
        assert_eq!(refusal(replay_instrument("ES", false)),
            ("runtime_eod", "Equity EOD replay cannot value ES; option marks and the lifecycle pass are wired into the EOD run by O4 (I5)".to_string()));
        assert_eq!(refusal(rehydrate("A", "O1", "WORKING", false, "0", "0")),
            ("runtime_eod", "Venue for 'A' does not hold working order 'O1' (WORKING); refusing to replay a session it could never fill in (I5)".to_string()));
        assert_eq!(refusal(rehydrate("A", "O1", "W", true, "1", "2")),
            ("runtime_eod", "Venue reports 1 filled for 'O1' but the ledger records 2 (I5)".to_string()));
        assert!(rehydrate("A", "O1", "W", true, "2", "1").is_ok());
    }

    #[test]
    fn bars_start_at_open_and_end_at_the_final_minute() {
        let texts = ["a", "b", "c", "d"];
        assert!(bars("ES", true, [0, 0, 60_000_000, 120_000_000], texts).is_ok());
        assert_eq!(refusal(bars("ES", false, [0, 0, 60_000_000, 120_000_000], texts)),
            ("runtime_replay", "No one-minute bars returned for ES for this session; refusing to simulate from memory (I5)".to_string()));
        assert_eq!(refusal(bars("ES", true, [1, 0, 60_000_000, 120_000_000], texts)),
            ("runtime_replay", "Bar series for ES starts at a, not the session open b (I5)".to_string()));
        assert_eq!(refusal(bars("ES", true, [0, 0, 5, 120_000_000], texts)),
            ("runtime_replay", "Bar series for ES ends at c, not the session's final minute d (I5)".to_string()));
    }
}

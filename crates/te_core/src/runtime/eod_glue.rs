//! The EodRunner ledger glue, in typed Rust over a [`Ledger`] (W6-C1).
//!
//! Ports the ledger reads and writes of `src/trade_engine/eod/runner.py` (`_void_command`,
//! `void_session`, `write_off_order`, `_mark_positions`, `_open_brackets`, ...). Each Python
//! `decide("eod:x", ...)` call becomes a direct call to the typed `eod::x(...)` rule with the
//! same values, so every refusal keeps the kind and message the typed rule raises.
//!
//! Nothing here touches a plugin (broker, strategy, risk engine, market data, chain
//! snapshots, lifecycle, dividends). Those belong to the W6-C2 EodHost.

use std::collections::{BTreeSet, HashSet};

use chrono::{Datelike, NaiveDate, NaiveTime, TimeZone, Utc};

use crate::calendar::{self, utc_to_ny};
use crate::ledger::fold::{fold_account, AccountState, Position};
use crate::ledger::model::{
    err, CashFlow, DateTime, EodRun, EodVoid, Event, EventKind, Fill, Instrument, LErr, Mark, Obj,
    Order, OrderStateChange, RiskVerdict, Signal, R, SCHEMA_VERSION,
};
use crate::ledger::ops::{self, OMap};
use crate::money::Money;
use crate::runtime::eod;
use crate::sim::snapshot::underlying_of;

/// The ledger the glue reads and writes. Writes are atomic: all events land, or none do.
pub trait Ledger {
    /// The account ids, sorted.
    fn accounts(&self) -> R<Vec<String>>;
    /// One account's events, in seq order.
    fn events(&self, account: &str) -> R<Vec<Event>>;
    /// One account's folded state.
    fn state(&self, account: &str) -> R<AccountState>;
    /// The event that carries this command id, if any.
    fn event_by_command(&self, command_id: &str) -> R<Option<Event>>;
    /// Append events atomically; seqs are assigned and the stored events returned.
    fn append(&mut self, events: Vec<Event>) -> R<Vec<Event>>;
}

/// The wall clock, as an aware UTC instant.
pub trait Clock {
    fn now_utc(&self) -> DateTime;
}

/// A bracket entry that is still open: the Python `OpenBracket` dataclass (`domain/exits.py`).
#[derive(Debug, Clone)]
pub struct OpenBracket {
    pub entry_order_id: String,
    pub account_id: String,
    pub instrument: Instrument,
    pub side: crate::ledger::model::Side,
    pub entry_quantity: Money,
    pub open_quantity: Money,
    pub average_entry_price: Money,
    pub entry_filled_at: DateTime,
    pub entry_session: NaiveDate,
    pub sessions_held: i64,
    pub stop_price: Money,
    pub targets_filled: i64,
    /// `(limit_price, remaining quantity)` for each working target.
    pub open_targets: Vec<(Money, Money)>,
    pub last_close: Option<Money>,
}

/// The risk inputs the fold can supply. Every other Python `RiskContext` field is `None`:
/// the fold has no source for it, so the matching rule records UNKNOWN and refuses (I5).
#[derive(Debug, Clone)]
pub struct RiskContext {
    /// `None` unless every non-flat position is marked (the fold cannot value an unmarked one).
    pub equity: Option<Money>,
    pub gross_exposure: Money,
    pub open_positions: i64,
    pub current_position_quantity: i128,
}

// --- helpers ---------------------------------------------------------------------------

fn cal<T>(r: calendar::Result<T>) -> R<T> {
    r.map_err(|e| LErr { kind: "value", msg: e.to_string() })
}

fn dec(text: &str) -> R<Money> {
    Money::parse(text).ok_or_else(|| LErr { kind: "value", msg: "invalid runtime decimal".into() })
}

fn text(value: &Money) -> String {
    ops::s(value)
}

pub(crate) fn iso(d: NaiveDate) -> String {
    format!("{:04}-{:02}-{:02}", d.year(), d.month(), d.day())
}

/// Python's `repr(datetime.date(...))`: no zero padding.
fn repr_date(d: NaiveDate) -> String {
    format!("datetime.date({}, {}, {})", d.year(), d.month(), d.day())
}

/// The instant as one integer (microseconds since 0001-01-01 UTC): orders like the instant.
fn micros(dt: &DateTime) -> i64 {
    let (secs, us) = dt.instant();
    secs * 1_000_000 + us as i64
}

pub(crate) fn event(account: &str, kind: EventKind, payload: Obj, ts: DateTime, command: Option<String>) -> Event {
    Event {
        account: account.to_string(),
        kind,
        payload,
        ts_utc: ts,
        command_id: command,
        schema_version: SCHEMA_VERSION,
        seq: None,
    }
}

/// The ledger instant as a chrono UTC instant (the form `utc_to_ny` takes).
fn to_chrono_utc(dt: &DateTime) -> R<chrono::DateTime<Utc>> {
    let u = dt.to_utc()?;
    let Some(t) = NaiveTime::from_num_seconds_from_midnight_opt(u.sod, u.us * 1000) else {
        return err("overflow", "time value out of range");
    };
    Ok(Utc.from_utc_datetime(&u.date.and_time(t)))
}

/// `(kind, command_id or "")` pairs, flattened, for the typed `count_events` rule.
pub(crate) fn kind_rows<L: Ledger>(l: &L, account: &str) -> R<Vec<String>> {
    let mut rows = Vec::new();
    for e in l.events(account)? {
        rows.push(e.kind.value().to_string());
        rows.push(e.command_id.clone().unwrap_or_default());
    }
    Ok(rows)
}

// --- commands --------------------------------------------------------------------------

/// `eod-void:<job>:<account>:<session>`.
pub fn void_command(job: &str, account: &str, session: NaiveDate) -> R<String> {
    Ok(eod::command("eod-void", job, account, &iso(session)))
}

/// `eod:<job>:<account>:<session>`: the command a session's after-close run writes.
pub fn run_command(job: &str, account: &str, session: NaiveDate) -> R<String> {
    Ok(eod::command("eod", job, account, &iso(session)))
}

// --- the owner's void and write-off -----------------------------------------------------

/// Record the owner's void of `session` for each of `accounts` (EOD-VOID-1).
///
/// Everything is validated before anything is written (`eod::void_check`). Accounts are
/// de-duplicated in the order given. `reason` and `owner` are `None` when the caller gave
/// none, which the typed rule refuses. Returns the accounts handled.
pub fn void_session<L: Ledger, C: Clock>(
    l: &mut L,
    clock: &C,
    job: &str,
    session: NaiveDate,
    accounts: &[String],
    reason: Option<&str>,
    owner: Option<&str>,
) -> R<Vec<String>> {
    let mut chosen: Vec<String> = Vec::new();
    for account in accounts {
        if !chosen.contains(account) {
            chosen.push(account.clone());
        }
    }
    let known = l.accounts()?;
    let is_session = cal(calendar::is_session(session))?;
    let mut rows: Vec<(String, bool, bool)> = Vec::with_capacity(chosen.len());
    for account in &chosen {
        let k = known.contains(account);
        let ran = k && l.event_by_command(&run_command(job, account, session)?)?.is_some();
        rows.push((account.clone(), k, ran));
    }
    let view: Vec<eod::VoidAccount> = rows
        .iter()
        .map(|(name, k, ran)| eod::VoidAccount { name: name.as_str(), is_str: true, known: *k, ran: *ran })
        .collect();
    let repr = repr_date(session);
    let session_text = iso(session);
    let req = eod::VoidRequest {
        session_repr: &repr,
        session: &session_text,
        is_date: true,
        reason: reason.unwrap_or(""),
        reason_is_str: reason.is_some(),
        owner: owner.unwrap_or(""),
        owner_is_str: owner.is_some(),
        is_session,
        accounts: &view,
    };
    let text = eod::void_check(&req)?;
    for account in &chosen {
        let payload = Obj::EodVoid(EodVoid {
            session,
            account_id: account.clone(),
            reason: text[0].clone(),
            owner: text[1].clone(),
            job: job.to_string(),
        });
        let command = void_command(job, account, session)?;
        l.append(vec![event(account, EventKind::EodVoid, payload, clock.now_utc(), Some(command))])?;
    }
    Ok(chosen)
}

/// Record the owner's write-off of one stuck working order (SCAN-WRITEOFF-1).
///
/// The entry and each non-terminal bracket child become CANCELLED, in one atomic append.
/// A repeat (the entry's command id already in the ledger) writes nothing and returns an
/// empty list. Returns the ids cancelled, the entry first.
pub fn write_off_order<L: Ledger, C: Clock>(
    l: &mut L,
    clock: &C,
    account: &str,
    order_id: &str,
    reason: Option<&str>,
    owner: Option<&str>,
    venue_cancel_confirmed: bool,
) -> R<Vec<String>> {
    let known = l.accounts()?.iter().any(|a| a == account);
    let command = format!("eod-writeoff:{account}:{order_id}");
    let already = known && l.event_by_command(&command)?.is_some();
    // The order fields are read only when the write-off could still happen (I3 repeats write nothing).
    let mut state: Option<AccountState> = None;
    let mut order_exists = false;
    let mut order_state = String::new();
    let mut order_filled = "0".to_string();
    let mut parent_id = String::new();
    let mut parent_filled = "0".to_string();
    let mut order_venue = String::new();
    let mut child_rows: Vec<(String, String, String)> = Vec::new();
    if known && !already {
        let st = l.state(account)?;
        let zero_text = text(&ops::zero());
        let order = st.orders.get(order_id);
        order_exists = order.is_some();
        order_state = order.map(|o| o.state.value().to_string()).unwrap_or_default();
        parent_id = order.and_then(|o| o.parent_order_id.clone()).unwrap_or_default();
        order_filled = st.filled_quantity.get(order_id).map(text).unwrap_or_else(|| zero_text.clone());
        parent_filled = if parent_id.is_empty() {
            "0".to_string()
        } else {
            st.filled_quantity.get(&parent_id).map(text).unwrap_or_else(|| zero_text.clone())
        };
        order_venue = st.venue_order_ids.get(order_id).cloned().unwrap_or_default();
        for (child_id, child) in st.orders.iter() {
            if child.parent_order_id.as_deref() == Some(order_id) {
                child_rows.push((
                    child_id.clone(),
                    child.state.value().to_string(),
                    st.venue_order_ids.get(child_id).cloned().unwrap_or_default(),
                ));
            }
        }
        state = Some(st);
    }
    let children: Vec<eod::WriteOffChild> = child_rows
        .iter()
        .map(|(id, state, venue)| eod::WriteOffChild { id: id.as_str(), state: state.as_str(), venue: venue.as_str() })
        .collect();
    let w = eod::WriteOff {
        reason: reason.unwrap_or(""),
        reason_is_str: reason.is_some(),
        owner: owner.unwrap_or(""),
        owner_is_str: owner.is_some(),
        account,
        account_known: known,
        already,
        order_exists,
        order: order_id,
        order_state: &order_state,
        order_filled: &order_filled,
        parent: &parent_id,
        parent_filled: &parent_filled,
        order_venue: &order_venue,
        children: &children,
        venue_cancel_confirmed,
    };
    let Some(text) = eod::writeoff_check(&w)? else {
        return Ok(Vec::new());
    };
    let Some(st) = state else {
        return err("runtime_eod", format!("{account} has no ledger state to write off"));
    };
    let reason_text = text[0].clone();
    let cancelled: Vec<String> = text[1..].to_vec();
    let ts = clock.now_utc();
    let entry_id = cancelled[0].clone();
    let mut events = vec![event(
        account,
        EventKind::OrderCancelled,
        Obj::StateChange(OrderStateChange {
            order_id: entry_id.clone(),
            reason: Some(reason_text.clone()),
            venue_order_id: st.venue_order_ids.get(&entry_id).cloned(),
        }),
        ts.clone(),
        Some(command.clone()),
    )];
    for child_id in &cancelled[1..] {
        events.push(event(
            account,
            EventKind::OrderCancelled,
            Obj::StateChange(OrderStateChange {
                order_id: child_id.clone(),
                reason: Some(format!("{reason_text} (bracket child of {order_id})")),
                venue_order_id: st.venue_order_ids.get(child_id).cloned(),
            }),
            ts.clone(),
            Some(format!("{command}:child:{child_id}")),
        ));
    }
    l.append(events)?;
    Ok(cancelled)
}

// --- gates and counts -------------------------------------------------------------------

/// The I3 gate: every account that has history must have run, or been voided, for the
/// previous session before `session` may run. `accounts` are the sorted broker keys.
pub fn require_previous_session_complete<L: Ledger>(
    l: &L,
    job: &str,
    accounts: &[String],
    session: NaiveDate,
) -> R<()> {
    let previous = cal(calendar::previous_session(session))?;
    for account in accounts {
        if l.event_by_command(&run_command(job, account, session)?)?.is_some() {
            // This session already completed; the re-run proves idempotency itself.
            continue;
        }
        if account_has_history(l, job, account, session)? {
            // A void of the previous session counts as complete (EOD-VOID-1).
            eod::previous(
                &iso(session),
                account,
                &iso(previous),
                job,
                true,
                l.event_by_command(&run_command(job, account, previous)?)?.is_some(),
                l.event_by_command(&void_command(job, account, previous)?)?.is_some(),
            )?;
        }
    }
    Ok(())
}

/// Whether this job has marked a session before `session` for the account. Only this
/// job's own markers count, and the run session's own markers are not history.
pub fn account_has_history<L: Ledger>(l: &L, job: &str, account: &str, session: NaiveDate) -> R<bool> {
    let run_session = iso(session);
    for e in l.events(account)? {
        let (is_run, marker, marker_session) = match (e.kind, &e.payload) {
            (EventKind::EodRun, Obj::EodRun(run)) => (true, run.job.clone(), iso(run.session)),
            _ => (false, String::new(), String::new()),
        };
        if eod::history(is_run, &marker, job, &marker_session, &run_session) {
            return Ok(true);
        }
    }
    Ok(false)
}

/// How many Fill events the account has.
pub fn fill_count<L: Ledger>(l: &L, account: &str) -> R<i64> {
    Ok(eod::count_events(EventKind::Fill.value(), "", &kind_rows(l, account)?))
}

/// How many Mark events the account has for this session's EOD marks.
pub fn marks_appended<L: Ledger>(l: &L, account: &str, session: NaiveDate) -> R<i64> {
    let prefix = format!("eod:mark:{account}:{}:", iso(session));
    Ok(eod::count_events(EventKind::Mark.value(), &prefix, &kind_rows(l, account)?))
}

// --- replay and marks --------------------------------------------------------------------

/// The instruments the session can change: open positions and working orders, sorted by
/// symbol. Each one must be a supported (equity) instrument for the replay.
pub fn replay_instruments(state: &AccountState) -> R<Vec<Instrument>> {
    let mut keys: HashSet<String> = HashSet::new();
    let mut picked: Vec<Instrument> = Vec::new();
    for position in state.positions.values() {
        if eod::nonzero(&text(&position.quantity))? && keys.insert(position.instrument.hk()) {
            picked.push(position.instrument.clone());
        }
    }
    for order in state.orders.values() {
        if eod::working(order.state.value()) && keys.insert(order.instrument.hk()) {
            picked.push(order.instrument.clone());
        }
    }
    let mut ordered: Vec<(String, Instrument)> = Vec::with_capacity(picked.len());
    for instrument in picked {
        ordered.push((instrument.symbol()?, instrument));
    }
    ordered.sort_by(|a, b| a.0.cmp(&b.0));
    for (symbol, instrument) in &ordered {
        eod::replay_instrument(symbol, matches!(instrument, Instrument::Equity(_)))?;
    }
    Ok(ordered.into_iter().map(|(_, instrument)| instrument).collect())
}

/// The underlyings the account needs snapshots for: open options and working orders.
pub fn option_underlyings(state: &AccountState) -> R<BTreeSet<String>> {
    let mut needed = BTreeSet::new();
    for position in state.positions.values() {
        if matches!(position.instrument, Instrument::Option(_)) && eod::nonzero(&text(&position.quantity))? {
            needed.insert(underlying_of(&position.instrument)?);
        }
    }
    for order in state.orders.values() {
        if eod::working(order.state.value()) {
            needed.insert(underlying_of(&order.instrument)?);
        }
    }
    Ok(needed)
}

/// Write one EOD mark per open position, from the last regular-session closes. One append
/// per mark, as in Python; a mark already written for this session is skipped.
pub fn mark_positions<L: Ledger, C: Clock>(
    l: &mut L,
    clock: &C,
    job: &str,
    account: &str,
    closes: &OMap<Instrument, Money>,
    session: NaiveDate,
) -> R<()> {
    let state = l.state(account)?;
    let mut rows: Vec<(String, &Position)> = Vec::with_capacity(state.positions.len());
    for position in state.positions.values() {
        rows.push((position.instrument.symbol()?, position));
    }
    rows.sort_by(|a, b| a.0.cmp(&b.0));
    for (symbol, position) in rows {
        let quantity = text(&position.quantity);
        if !eod::nonzero(&quantity)? {
            continue;
        }
        let mark_command = eod::command("eod:mark", account, &iso(session), &symbol);
        if l.event_by_command(&mark_command)?.is_some() {
            continue;
        }
        let close = closes.get(&position.instrument.hk()).cloned();
        eod::mark(&quantity, false, close.is_some(), &symbol, account)?;
        let Some(price) = close else {
            return err("runtime_replay", format!("No regular-session bar for open position {symbol} in '{account}'; refusing to mark without one (I5)"));
        };
        let now = clock.now_utc();
        let mark = Mark {
            instrument: position.instrument.clone(),
            price,
            as_of: now.clone(),
            source: Some(format!("eod:{job}")),
        };
        l.append(vec![event(account, EventKind::Mark, Obj::Mark(mark), now, Some(mark_command))])?;
    }
    Ok(())
}

// --- options: settlement, dividends and marks ----------------------------------------------

/// One option's chain quote, as `_mark_options_account` reads it: the mid, and the times the
/// quote and the snapshot holding it were taken.
#[derive(Debug, Clone)]
pub struct OptionQuote {
    pub mid: Money,
    pub quote_as_of: DateTime,
    pub snapshot_as_of: DateTime,
}

/// The accounts, in the given order, that hold a nonzero option position (`_settle_options`).
pub fn options_holders<L: Ledger>(l: &L, accounts: &[String]) -> R<Vec<String>> {
    let mut holding = Vec::new();
    for account in accounts {
        let state = l.state(account)?;
        let mut holds = false;
        for position in state.positions.values() {
            if matches!(position.instrument, Instrument::Option(_)) && eod::nonzero(&text(&position.quantity))? {
                holds = true;
                break;
            }
        }
        if holds {
            holding.push(account.clone());
        }
    }
    Ok(holding)
}

/// `_credit_dividends`: credit (or charge) today's ex-dividends on the shares held at the open.
/// The holding is the account as it stood before the session opened. One append per holding,
/// so a later refusal leaves the earlier credits written. `fetch` gives one symbol's dividend
/// records, as `(as_of, amount)`, in the source's order.
pub fn credit_dividends<L: Ledger, C: Clock>(
    l: &mut L,
    clock: &C,
    account: &str,
    session: NaiveDate,
    session_open_micros: i64,
    has_source: bool,
    mut fetch: impl FnMut(&str) -> R<Vec<(DateTime, Money)>>,
) -> R<()> {
    let opened: Vec<Event> = l
        .events(account)?
        .into_iter()
        .filter(|e| eod::preopen(micros(&e.ts_utc), session_open_micros))
        .collect();
    let before = fold_account(&opened, account)?;
    let mut shares: Vec<(String, Money)> = Vec::new();
    for position in before.positions.values() {
        if matches!(position.instrument, Instrument::Equity(_)) && eod::nonzero(&text(&position.quantity))? {
            shares.push((position.instrument.symbol()?, position.quantity.clone()));
        }
    }
    shares.sort_by(|a, b| a.0.cmp(&b.0));
    if shares.is_empty() {
        return Ok(());
    }
    eod::dividend_source(account, has_source)?;
    let now = clock.now_utc();
    for (symbol, quantity) in shares {
        let found = fetch(&symbol)?;
        for (as_of, _) in &found {
            eod::dividend_stamp(&symbol, &as_of.iso(), micros(as_of), micros(&now))?;
        }
        let amounts: Vec<String> = found.iter().map(|(_, amount)| text(amount)).collect();
        let quantity_text = text(&quantity);
        let dividend = eod::dividend(&quantity_text, &amounts)?;
        if !dividend.paid {
            continue;
        }
        let per_share = dec(&dividend.text[0])?;
        let amount = dec(&dividend.text[1])?;
        let note = format!(
            "{symbol} ex-dividend {}: {} a share on {quantity_text} held at the open",
            iso(session),
            text(&per_share)
        );
        let flow = CashFlow { amount, kind: "dividend".into(), as_of: now.clone(), note: Some(note) };
        let command = format!("dividend:{account}:{symbol}:{}", iso(session));
        l.append(vec![event(account, EventKind::CashFlow, Obj::CashFlow(flow), now.clone(), Some(command))])?;
    }
    Ok(())
}

/// `_mark_options_account`: options at the newest snapshot's mid; shares and underlyings at the
/// official close. The underlying of every option held is marked too, held or not (O3). Every
/// refusal comes before the first append, so a refused run writes nothing. `quote` is `None`
/// when the option has no quote; `official` gives a symbol's official close and its source.
pub fn mark_options_account<L: Ledger, C: Clock>(
    l: &mut L,
    clock: &C,
    account: &str,
    session: NaiveDate,
    mut quote: impl FnMut(&Instrument) -> R<Option<OptionQuote>>,
    mut official: impl FnMut(&str) -> R<(Money, String)>,
    max_quote_age_seconds: f64,
) -> R<()> {
    let state = l.state(account)?;
    let mut rows: Vec<(String, &Position)> = Vec::with_capacity(state.positions.len());
    for position in state.positions.values() {
        rows.push((position.instrument.symbol()?, position));
    }
    rows.sort_by(|a, b| a.0.cmp(&b.0));
    // (instrument key, symbol, instrument, price, source), in Python's dict insertion order.
    let mut marks: Vec<(String, String, Instrument, Money, String)> = Vec::new();
    let mut underlyings: BTreeSet<String> = BTreeSet::new();
    for (symbol, position) in rows {
        if !eod::nonzero(&text(&position.quantity))? {
            continue;
        }
        match &position.instrument {
            Instrument::Option(contract) => {
                let underlying = underlying_of(&position.instrument)?;
                let occ = contract.occ()?.trim().to_string();
                let found = quote(&position.instrument)?;
                let Some(found) = found else {
                    return eod::option_mark("0", false, &iso(session), &occ, account);
                };
                eod::option_mark(&text(&found.mid), true, &iso(session), &occ, account)?;
                // Still the mid, but its provenance names the quote's real time.
                let age = micros(&found.snapshot_as_of) - micros(&found.quote_as_of);
                let source = eod::quote_source(
                    &found.snapshot_as_of.iso(),
                    &found.quote_as_of.iso(),
                    age,
                    max_quote_age_seconds,
                );
                marks.push((position.instrument.hk(), symbol, position.instrument.clone(), found.mid, source));
                underlyings.insert(underlying);
            }
            Instrument::Equity(name) => {
                let (price, source) = official(name)?;
                marks.push((position.instrument.hk(), symbol, position.instrument.clone(), price, source));
            }
            _ => return eod::unmarkable(account, &symbol),
        }
    }
    for underlying in &underlyings {
        let shares = Instrument::Equity(underlying.clone());
        let key = shares.hk();
        if marks.iter().any(|m| m.0 == key) {
            continue;
        }
        let (price, source) = official(underlying)?;
        marks.push((key, underlying.clone(), shares, price, source));
    }
    let now = clock.now_utc();
    marks.sort_by(|a, b| a.1.cmp(&b.1));
    for (_, symbol, instrument, price, source) in marks {
        let command = eod::command("eod:mark", account, &iso(session), &symbol);
        if l.event_by_command(&command)?.is_some() {
            continue;
        }
        let mark = Mark { instrument, price, as_of: now.clone(), source: Some(source) };
        l.append(vec![event(account, EventKind::Mark, Obj::Mark(mark), now.clone(), Some(command))])?;
    }
    Ok(())
}

/// Record that this job ran its session for the account. `command_id` defaults to the run
/// command when the caller gives none (or an empty one).
pub fn append_run_marker<L: Ledger, C: Clock>(
    l: &mut L,
    clock: &C,
    job: &str,
    account: &str,
    session: NaiveDate,
    bars_processed: i128,
    command_id: Option<&str>,
) -> R<()> {
    let now = clock.now_utc();
    let command = match command_id.filter(|c| !c.is_empty()) {
        Some(c) => c.to_string(),
        None => run_command(job, account, session)?,
    };
    let run = EodRun {
        session,
        job: job.to_string(),
        account_id: account.to_string(),
        bars_processed,
        at_close: now.clone(),
    };
    l.append(vec![event(account, EventKind::EodRun, Obj::EodRun(run), now, Some(command))])?;
    Ok(())
}

// --- brackets and risk -------------------------------------------------------------------

/// The entries whose protective stop has filled quantity and that still hold an open
/// position, with their open targets. An entry with no stop, or no fills, is skipped.
pub fn open_brackets(
    state: &AccountState,
    account: &str,
    session: NaiveDate,
    closes: &OMap<Instrument, Money>,
) -> R<Vec<OpenBracket>> {
    let orders: Vec<&Order> = state.orders.values().collect();
    let mut entries: Vec<&Order> = orders.clone();
    entries.sort_by(|a, b| a.order_id.cmp(&b.order_id));
    let mut out = Vec::new();
    for entry in entries {
        if entry.parent_order_id.is_some() {
            continue;
        }
        let mut rows: Vec<String> = Vec::with_capacity(orders.len() * 3);
        for order in &orders {
            rows.push(order.parent_order_id.clone().unwrap_or_default());
            rows.push(order.order_type.value().to_string());
            rows.push(order.state.value().to_string());
        }
        let found = eod::children(&entry.order_id, &rows);
        let children: Vec<&Order> = orders
            .iter()
            .zip(found.owned.iter())
            .filter(|(_, owned)| **owned)
            .map(|(order, _)| *order)
            .collect();
        let stop = match found.stop {
            Some(i) => orders[i as usize],
            None => continue,
        };
        let Some(stop_price) = stop.stop_price.clone() else {
            continue;
        };
        let entry_fills: Vec<&Fill> = state.fills.iter().filter(|f| f.order_id == entry.order_id).collect();
        if entry_fills.is_empty() {
            continue;
        }
        let filled_rows: Vec<String> = entry_fills.iter().map(|f| text(&f.quantity)).collect();
        let exited_rows: Vec<String> = children
            .iter()
            .map(|c| text(&state.filled_quantity.get(&c.order_id).cloned().unwrap_or_else(ops::zero)))
            .collect();
        let tail: Vec<String> = entry_fills.iter().flat_map(|f| [text(&f.price), text(&f.quantity)]).collect();
        let bracket = eod::bracket(&filled_rows, &exited_rows, &tail)?;
        if !bracket.open {
            continue;
        }
        let entry_filled = dec(&bracket.text[0])?;
        let open_quantity = dec(&bracket.text[1])?;
        let average = dec(&bracket.text[2])?;
        let targets: Vec<&Order> = found.limits.iter().map(|&i| orders[i as usize]).collect();
        let times: Vec<i64> = entry_fills.iter().map(|f| micros(&f.filled_at)).collect();
        let Some(first) = eod::earliest(&times) else {
            return err("runtime_eod", "bracket has no entry fill");
        };
        let first_fill = entry_fills[first];
        let ny_date = utc_to_ny(to_chrono_utc(&first_fill.filled_at)?).date_naive();
        let entry_session = cal(calendar::roll_to_session(ny_date, true))?;
        let sessions_held = cal(calendar::sessions_in_range(entry_session, session))?.len() as i64 - 1;
        let target_states: Vec<String> = targets.iter().map(|t| t.state.value().to_string()).collect();
        let mut open_targets: Vec<(Money, Money)> = Vec::new();
        for target in &targets {
            let Some(limit) = target.limit_price.clone() else {
                continue;
            };
            if eod::terminal(target.state.value()) {
                continue;
            }
            let quantity = text(&target.quantity);
            let got = text(&state.filled_quantity.get(&target.order_id).cloned().unwrap_or_else(ops::zero));
            open_targets.push((limit, dec(&eod::subtract(&quantity, &got)?)?));
        }
        out.push(OpenBracket {
            entry_order_id: entry.order_id.clone(),
            account_id: account.to_string(),
            instrument: entry.instrument.clone(),
            side: entry.side,
            entry_quantity: entry_filled,
            open_quantity,
            average_entry_price: average,
            entry_filled_at: first_fill.filled_at.clone(),
            entry_session,
            sessions_held,
            stop_price,
            targets_filled: eod::filled(&target_states),
            open_targets,
            last_close: closes.get(&entry.instrument.hk()).cloned(),
        });
    }
    Ok(out)
}

/// Record the signal the scan produced, under `signal:<signal_id>`.
pub fn record_signal<L: Ledger, C: Clock>(l: &mut L, clock: &C, account: &str, signal: Signal) -> R<()> {
    let command = format!("signal:{}", signal.signal_id);
    l.append(vec![event(account, EventKind::SignalSeen, Obj::Signal(signal), clock.now_utc(), Some(command))])?;
    Ok(())
}

/// The verdict an earlier, interrupted run recorded for this intent, if any (F3).
pub fn recorded_verdict<L: Ledger>(l: &L, intent_command_id: &str) -> R<Option<RiskVerdict>> {
    match l.event_by_command(&format!("risk:{intent_command_id}"))? {
        None => Ok(None),
        Some(e) => match e.payload {
            Obj::RiskVerdict(verdict) => Ok(Some(verdict)),
            other => err(
                "value",
                format!("risk:{intent_command_id} holds a {} payload, not a RiskVerdict", other.tag()),
            ),
        },
    }
}

/// Record the risk verdict for an intent, under `risk:<intent_command_id>`.
pub fn record_verdict<L: Ledger, C: Clock>(
    l: &mut L,
    clock: &C,
    account: &str,
    intent_command_id: &str,
    verdict: RiskVerdict,
) -> R<()> {
    let command = format!("risk:{intent_command_id}");
    l.append(vec![event(account, EventKind::RiskVerdict, Obj::RiskVerdict(verdict), clock.now_utc(), Some(command))])?;
    Ok(())
}

/// Fold-derived risk measurements for one intent's instrument, as `EodRunner._derive_risk_context`:
/// equity is cash plus `quantity * mark` over the marked positions, and `None` unless positive.
pub fn derive_risk_context(state: &AccountState, instrument: &Instrument) -> R<RiskContext> {
    let held_quantity = match state.positions.get(&instrument.hk()) {
        Some(p) => text(&p.quantity),
        None => "0".to_string(),
    };
    let positions: Vec<&Position> = state.positions.values().collect();
    let mut rows: Vec<String> = Vec::with_capacity(positions.len() * 2);
    let mut held: Vec<bool> = Vec::with_capacity(positions.len());
    for position in &positions {
        let key = position.instrument.hk();
        let marked = state.marks.contains(&key);
        let mark = state.marks.get(&key).cloned().unwrap_or_else(ops::zero);
        rows.push(text(&position.quantity));
        rows.push(text(&mark));
        held.push(marked);
    }
    let r = eod::risk(&text(&state.cash), &held_quantity, &rows, &held)?;
    let equity = if r.equity_positive { Some(dec(&r.equity)?) } else { None };
    let current_position_quantity = r.base.parse::<i128>().map_err(|_| LErr {
        kind: "value",
        msg: format!("current position quantity {} is not an integer", r.base),
    })?;
    Ok(RiskContext {
        equity,
        gross_exposure: dec(&r.gross)?,
        open_positions: r.open_positions,
        current_position_quantity,
    })
}

#[cfg(test)]
pub(crate) mod tests {
    use super::*;
    use crate::ledger::fold::fold_account;
    use crate::ledger::model::{
        check_event, make_equity, parse_datetime, CashFlow, OrderState, OrderType, OrdersCreated, Side, Tif,
    };
    use std::collections::HashMap;

    /// An in-memory ledger: events in seq order, and a command index.
    #[derive(Default)]
    pub(crate) struct MemLedger {
        events: Vec<Event>,
        commands: HashMap<String, usize>,
    }

    fn account_events(events: &[Event], account: &str) -> Vec<Event> {
        events.iter().filter(|e| e.account == account).cloned().collect()
    }

    impl Ledger for MemLedger {
        fn accounts(&self) -> R<Vec<String>> {
            let set: BTreeSet<String> = self.events.iter().map(|e| e.account.clone()).collect();
            Ok(set.into_iter().collect())
        }

        fn events(&self, account: &str) -> R<Vec<Event>> {
            Ok(account_events(&self.events, account))
        }

        fn state(&self, account: &str) -> R<AccountState> {
            fold_account(&account_events(&self.events, account), account)
        }

        fn event_by_command(&self, command_id: &str) -> R<Option<Event>> {
            Ok(self.commands.get(command_id).map(|&i| self.events[i].clone()))
        }

        fn append(&mut self, events: Vec<Event>) -> R<Vec<Event>> {
            let mut staged: Vec<Event> = Vec::with_capacity(events.len());
            let mut fresh: HashSet<String> = HashSet::new();
            for mut e in events {
                check_event(&e)?;
                if let Some(c) = &e.command_id {
                    if self.commands.contains_key(c) || !fresh.insert(c.clone()) {
                        return err("duplicate_command", format!("command '{c}' is already in the ledger"));
                    }
                }
                e.seq = Some((self.events.len() + staged.len() + 1) as i128);
                staged.push(e);
            }
            // Fold the trial ledger first, so a batch that breaks the fold writes nothing.
            let mut trial = self.events.clone();
            trial.extend(staged.iter().cloned());
            let accounts: BTreeSet<String> = staged.iter().map(|e| e.account.clone()).collect();
            for account in &accounts {
                fold_account(&account_events(&trial, account), account)?;
            }
            let base = self.events.len();
            for (i, e) in staged.iter().enumerate() {
                if let Some(c) = &e.command_id {
                    self.commands.insert(c.clone(), base + i);
                }
            }
            self.events.extend(staged.iter().cloned());
            Ok(staged)
        }
    }

    pub(crate) struct FixedClock(pub(crate) DateTime);

    impl Clock for FixedClock {
        fn now_utc(&self) -> DateTime {
            self.0.clone()
        }
    }

    fn at(s: &str) -> DateTime {
        parse_datetime(s).expect("iso").0
    }

    fn money(s: &str) -> Money {
        Money::parse(s).expect("decimal")
    }

    fn day(s: &str) -> NaiveDate {
        NaiveDate::parse_from_str(s, "%Y-%m-%d").expect("date")
    }

    fn clock() -> FixedClock {
        FixedClock(at("2026-11-24T12:00:00-05:00"))
    }

    fn put(l: &mut MemLedger, account: &str, kind: EventKind, payload: Obj, command: &str) {
        l.append(vec![event(account, kind, payload, at("2026-11-24T12:00:00-05:00"), Some(command.to_string()))])
            .expect("append");
    }

    fn order(
        id: &str,
        sym: &str,
        side: Side,
        order_type: OrderType,
        qty: &str,
        parent: Option<&str>,
    ) -> Order {
        Order {
            order_id: id.into(),
            account_id: "A".into(),
            instrument: make_equity(sym).expect("equity"),
            order_type,
            side,
            quantity: money(qty),
            command_id: format!("{id}-cmd"),
            created_at: at("2026-11-23T15:00:00+00:00"),
            limit_price: None,
            stop_price: None,
            trail_amount: None,
            tif: Tif::Day,
            state: OrderState::Submitted,
            parent_order_id: parent.map(str::to_string),
            oco_group: None,
        }
    }

    fn created(l: &mut MemLedger, orders: Vec<Order>) {
        let command = format!("orders:{}", orders[0].order_id);
        put(
            l,
            "A",
            EventKind::OrdersCreated,
            Obj::OrdersCreated(OrdersCreated { orders, fingerprint: "fp".into(), reason: "test".into() }),
            &command,
        );
    }

    fn fill(l: &mut MemLedger, id: &str, order_id: &str, sym: &str, side: Side, qty: &str, price: &str) {
        let f = Fill {
            fill_id: id.into(),
            order_id: order_id.into(),
            account_id: "A".into(),
            instrument: make_equity(sym).expect("equity"),
            quantity: money(qty),
            price: money(price),
            venue_env: "sim".into(),
            filled_at: at("2026-11-23T15:00:00+00:00"),
            side,
            fee: money("0"),
            leg_id: None,
            venue_order_id: None,
            venue_execution_id: None,
        };
        put(l, "A", EventKind::Fill, Obj::Fill(f), &format!("fill:{id}"));
    }

    fn deposit(l: &mut MemLedger, account: &str, amount: &str) {
        let flow = CashFlow { amount: money(amount), kind: "deposit".into(), as_of: at("2026-11-23T12:00:00-05:00"), note: None };
        put(l, account, EventKind::CashFlow, Obj::CashFlow(flow), &format!("cash:{account}:{amount}"));
    }

    /// Entry E (SPY buy 2, limit) with stop child S (95) and target child T (110), all working.
    fn entry_with_brackets(l: &mut MemLedger) {
        let entry = order("E", "SPY", Side::Buy, OrderType::Limit, "2", None);
        let mut stop = order("S", "SPY", Side::Sell, OrderType::Stop, "2", Some("E"));
        stop.stop_price = Some(money("95"));
        let mut target = order("T", "SPY", Side::Sell, OrderType::Limit, "1", Some("E"));
        target.limit_price = Some(money("110"));
        created(l, vec![entry, stop, target]);
    }

    /// Two positions: SPY 2 (open) and QQQ flat (bought 1, sold 1).
    fn positions_book(l: &mut MemLedger) {
        created(
            l,
            vec![
                order("E", "SPY", Side::Buy, OrderType::Limit, "2", None),
                order("Q", "QQQ", Side::Buy, OrderType::Limit, "1", None),
                order("QX", "QQQ", Side::Sell, OrderType::Limit, "1", None),
            ],
        );
        fill(l, "F1", "E", "SPY", Side::Buy, "2", "100");
        fill(l, "F2", "Q", "QQQ", Side::Buy, "1", "50");
        fill(l, "F3", "QX", "QQQ", Side::Sell, "1", "51");
    }

    /// The refusal `eod::void_check` gives for the same input (the void's own rule).
    fn void_expected(session: NaiveDate, accounts: &[(&str, bool, bool)], reason: Option<&str>, owner: Option<&str>) -> LErr {
        let rows: Vec<eod::VoidAccount> = accounts
            .iter()
            .map(|&(name, known, ran)| eod::VoidAccount { name, is_str: true, known, ran })
            .collect();
        let repr = repr_date(session);
        let session_text = iso(session);
        let req = eod::VoidRequest {
            session_repr: &repr,
            session: &session_text,
            is_date: true,
            reason: reason.unwrap_or(""),
            reason_is_str: reason.is_some(),
            owner: owner.unwrap_or(""),
            owner_is_str: owner.is_some(),
            is_session: calendar::is_session(session).expect("calendar"),
            accounts: &rows,
        };
        eod::void_check(&req).expect_err("expected a refusal")
    }

    fn two_accounts() -> MemLedger {
        let mut l = MemLedger::default();
        deposit(&mut l, "A", "100");
        deposit(&mut l, "B", "100");
        l
    }

    #[test]
    fn commands_match_python_strings() {
        assert_eq!(run_command("eod", "A", day("2026-11-24")).unwrap(), "eod:eod:A:2026-11-24");
        assert_eq!(void_command("eod", "A", day("2026-11-24")).unwrap(), "eod-void:eod:A:2026-11-24");
    }

    #[test]
    fn void_session_happy_path_writes_one_void_per_deduped_account() {
        let mut l = two_accounts();
        let handled = void_session(
            &mut l, &clock(), "eod", day("2026-11-24"),
            &["A".to_string(), "B".to_string(), "A".to_string()],
            Some("  broker outage "), Some(" vinay "),
        )
        .expect("void");
        assert_eq!(handled, vec!["A".to_string(), "B".to_string()]);
        let v = l.event_by_command("eod-void:eod:A:2026-11-24").expect("read").expect("void A");
        match v.payload {
            Obj::EodVoid(void) => {
                assert_eq!(void.reason, "broker outage");
                assert_eq!(void.owner, "vinay");
                assert_eq!(void.job, "eod");
            }
            other => panic!("expected EodVoid, got {}", other.tag()),
        }
        assert!(l.event_by_command("eod-void:eod:B:2026-11-24").expect("read").is_some());
    }

    #[test]
    fn void_session_refusals_equal_void_check() {
        // A Saturday is not a trading session.
        let mut l = two_accounts();
        let got = void_session(&mut l, &clock(), "eod", day("2026-11-28"), &["A".to_string()], Some("r"), Some("o"));
        assert_eq!(got.unwrap_err(), void_expected(day("2026-11-28"), &[("A", true, false)], Some("r"), Some("o")));

        // An account the ledger does not know.
        let mut l = two_accounts();
        let got = void_session(&mut l, &clock(), "eod", day("2026-11-24"), &["A".to_string(), "Z".to_string()], Some("r"), Some("o"));
        assert_eq!(
            got.unwrap_err(),
            void_expected(day("2026-11-24"), &[("A", true, false), ("Z", false, false)], Some("r"), Some("o")),
        );

        // A session that already ran cannot be voided.
        let mut l = two_accounts();
        append_run_marker(&mut l, &clock(), "eod", "A", day("2026-11-24"), 0, None).expect("run");
        let got = void_session(&mut l, &clock(), "eod", day("2026-11-24"), &["A".to_string()], Some("r"), Some("o"));
        assert_eq!(got.unwrap_err(), void_expected(day("2026-11-24"), &[("A", true, true)], Some("r"), Some("o")));

        // Missing reason, then missing owner.
        let mut l = two_accounts();
        let got = void_session(&mut l, &clock(), "eod", day("2026-11-24"), &["A".to_string()], None, Some("o"));
        assert_eq!(got.unwrap_err(), void_expected(day("2026-11-24"), &[("A", true, false)], None, Some("o")));
        let got = void_session(&mut l, &clock(), "eod", day("2026-11-24"), &["A".to_string()], Some("r"), None);
        assert_eq!(got.unwrap_err(), void_expected(day("2026-11-24"), &[("A", true, false)], Some("r"), None));
        assert!(l.event_by_command("eod-void:eod:A:2026-11-24").expect("read").is_none());
    }

    #[test]
    fn write_off_order_cancels_entry_and_children_in_one_append() {
        let mut l = MemLedger::default();
        entry_with_brackets(&mut l);
        let ids = write_off_order(&mut l, &clock(), "A", "E", Some("stuck"), Some("vinay"), false).expect("write-off");
        assert_eq!(ids, vec!["E".to_string(), "S".to_string(), "T".to_string()]);

        let cancels: Vec<Event> = l.events("A").expect("events").into_iter()
            .filter(|e| e.kind == EventKind::OrderCancelled).collect();
        assert_eq!(cancels.len(), 3);
        let reason_of = |e: &Event| match &e.payload {
            Obj::StateChange(c) => c.reason.clone().unwrap_or_default(),
            _ => String::new(),
        };
        assert_eq!(reason_of(&cancels[0]), "Owner write-off: stuck (owner: vinay)");
        assert_eq!(reason_of(&cancels[1]), "Owner write-off: stuck (owner: vinay) (bracket child of E)");
        assert_eq!(cancels[1].command_id.as_deref(), Some("eod-writeoff:A:E:child:S"));
        assert_eq!(cancels[2].command_id.as_deref(), Some("eod-writeoff:A:E:child:T"));
    }

    #[test]
    fn write_off_order_repeat_writes_nothing() {
        let mut l = MemLedger::default();
        entry_with_brackets(&mut l);
        write_off_order(&mut l, &clock(), "A", "E", Some("stuck"), Some("vinay"), false).expect("first");
        let before = l.events("A").expect("events").len();
        let again = write_off_order(&mut l, &clock(), "A", "E", Some("stuck"), Some("vinay"), false).expect("repeat");
        assert!(again.is_empty());
        assert_eq!(l.events("A").expect("events").len(), before);
    }

    /// A ledger whose writes the host refuses, as a failed store write does (kind `host`).
    struct RefusingWrites(MemLedger);

    impl Ledger for RefusingWrites {
        fn accounts(&self) -> R<Vec<String>> {
            self.0.accounts()
        }
        fn events(&self, account: &str) -> R<Vec<Event>> {
            self.0.events(account)
        }
        fn state(&self, account: &str) -> R<AccountState> {
            self.0.state(account)
        }
        fn event_by_command(&self, command_id: &str) -> R<Option<Event>> {
            self.0.event_by_command(command_id)
        }
        fn append(&mut self, _events: Vec<Event>) -> R<Vec<Event>> {
            err("host", "the store refused the write".to_string())
        }
    }

    #[test]
    fn write_off_order_refused_write_leaves_the_ledger_unchanged() {
        let mut l = RefusingWrites(MemLedger::default());
        entry_with_brackets(&mut l.0);
        let before = l.0.events("A").expect("events").len();
        let got = write_off_order(&mut l, &clock(), "A", "E", Some("stuck"), Some("vinay"), false).unwrap_err();
        assert_eq!(got.kind, "host");
        assert_eq!(l.0.events("A").expect("events").len(), before);
        assert!(l.0.event_by_command("eod-writeoff:A:E").expect("read").is_none());
    }

    #[test]
    fn write_off_order_refuses_unknown_order() {
        let mut l = MemLedger::default();
        entry_with_brackets(&mut l);
        let got = write_off_order(&mut l, &clock(), "A", "NOPE", Some("stuck"), Some("vinay"), false).unwrap_err();
        assert_eq!(got.kind, "runtime_eod");
        assert!(got.msg.contains("has no order"), "{}", got.msg);
    }

    #[test]
    fn require_previous_session_complete_covers_the_four_cases() {
        let accounts = vec!["A".to_string()];
        let session = day("2026-11-24");

        // No history: passes.
        let l = MemLedger::default();
        require_previous_session_complete(&l, "eod", &accounts, session).expect("no history");

        // History, previous session run: passes.
        let mut l = MemLedger::default();
        deposit(&mut l, "A", "100");
        append_run_marker(&mut l, &clock(), "eod", "A", day("2026-11-20"), 0, None).expect("older run");
        append_run_marker(&mut l, &clock(), "eod", "A", day("2026-11-23"), 0, None).expect("previous run");
        require_previous_session_complete(&l, "eod", &accounts, session).expect("previous run");

        // History, previous session voided: passes.
        let mut l = MemLedger::default();
        deposit(&mut l, "A", "100");
        append_run_marker(&mut l, &clock(), "eod", "A", day("2026-11-20"), 0, None).expect("older run");
        void_session(&mut l, &clock(), "eod", day("2026-11-23"), &accounts, Some("r"), Some("o")).expect("void");
        require_previous_session_complete(&l, "eod", &accounts, session).expect("voided");

        // History, neither run nor voided: refused.
        let mut l = MemLedger::default();
        deposit(&mut l, "A", "100");
        append_run_marker(&mut l, &clock(), "eod", "A", day("2026-11-20"), 0, None).expect("older run");
        let got = require_previous_session_complete(&l, "eod", &accounts, session).unwrap_err();
        assert_eq!(got.kind, "runtime_incomplete");
    }

    #[test]
    fn account_has_history_ignores_other_jobs_and_the_run_session() {
        // Another job's marker does not count.
        let mut l = MemLedger::default();
        deposit(&mut l, "A", "100");
        append_run_marker(&mut l, &clock(), "intraday", "A", day("2026-11-23"), 0, None).expect("run");
        assert!(!account_has_history(&l, "eod", "A", day("2026-11-24")).expect("history"));

        // The run session's own marker does not count.
        let mut l = MemLedger::default();
        deposit(&mut l, "A", "100");
        append_run_marker(&mut l, &clock(), "eod", "A", day("2026-11-24"), 0, None).expect("run");
        assert!(!account_has_history(&l, "eod", "A", day("2026-11-24")).expect("history"));

        // This job's earlier marker does count.
        let mut l = MemLedger::default();
        deposit(&mut l, "A", "100");
        append_run_marker(&mut l, &clock(), "eod", "A", day("2026-11-23"), 0, None).expect("run");
        assert!(account_has_history(&l, "eod", "A", day("2026-11-24")).expect("history"));
    }

    #[test]
    fn a_void_only_account_is_not_history() {
        let mut l = MemLedger::default();
        deposit(&mut l, "A", "100");
        void_session(&mut l, &clock(), "eod", day("2026-11-23"), &["A".to_string()], Some("r"), Some("o")).expect("void");
        assert!(l.event_by_command("eod-void:eod:A:2026-11-23").expect("read").is_some());
        assert!(!account_has_history(&l, "eod", "A", day("2026-11-24")).expect("history"));
    }

    #[test]
    fn fill_count_and_marks_appended_count_only_matching_rows() {
        let mut l = MemLedger::default();
        positions_book(&mut l);
        assert_eq!(fill_count(&l, "A").expect("fills"), 3);
        assert_eq!(marks_appended(&l, "A", day("2026-11-24")).expect("marks"), 0);

        let mut closes: OMap<Instrument, Money> = OMap::new();
        let spy = make_equity("SPY").expect("equity");
        closes.insert(spy.hk(), spy.clone(), money("101"));
        mark_positions(&mut l, &clock(), "eod", "A", &closes, day("2026-11-23")).expect("mark 23");
        mark_positions(&mut l, &clock(), "eod", "A", &closes, day("2026-11-24")).expect("mark 24");
        assert_eq!(marks_appended(&l, "A", day("2026-11-24")).expect("marks"), 1);
        assert_eq!(marks_appended(&l, "A", day("2026-11-23")).expect("marks"), 1);
        assert_eq!(fill_count(&l, "A").expect("fills"), 3);
    }

    #[test]
    fn mark_positions_skips_zero_and_marked_and_refuses_missing_close() {
        let mut l = MemLedger::default();
        positions_book(&mut l);
        let spy = make_equity("SPY").expect("equity");
        let mut closes: OMap<Instrument, Money> = OMap::new();
        closes.insert(spy.hk(), spy.clone(), money("101"));

        mark_positions(&mut l, &clock(), "eod", "A", &closes, day("2026-11-24")).expect("mark");
        let mark = l.event_by_command("eod:mark:A:2026-11-24:SPY").expect("read").expect("SPY marked");
        match mark.payload {
            Obj::Mark(m) => {
                assert!(ops::eq(&m.price, &money("101")).expect("cmp"));
                assert_eq!(m.source.as_deref(), Some("eod:eod"));
            }
            other => panic!("expected Mark, got {}", other.tag()),
        }
        assert!(l.event_by_command("eod:mark:A:2026-11-24:QQQ").expect("read").is_none(), "flat QQQ is not marked");

        // A second run writes nothing new.
        let before = l.events("A").expect("events").len();
        mark_positions(&mut l, &clock(), "eod", "A", &closes, day("2026-11-24")).expect("again");
        assert_eq!(l.events("A").expect("events").len(), before);

        // An open position with no close is refused.
        let mut l = MemLedger::default();
        positions_book(&mut l);
        let got = mark_positions(&mut l, &clock(), "eod", "A", &OMap::new(), day("2026-11-24")).unwrap_err();
        assert_eq!(got.kind, "runtime_replay");
    }

    use crate::ledger::model::{make_future, make_option, Val};
    use crate::options::Right;

    /// The SPY 100 call of 2026-12-18 the options tests hold.
    fn spy_call() -> Instrument {
        let contract = make_option("SPY", day("2026-12-18"), Val::Dec(money("100")), 100, Val::Right(Right::Call));
        Instrument::Option(contract.expect("option"))
    }

    /// One option buy, booked as an order then its fill (as `positions_book` books equities).
    fn option_book(l: &mut MemLedger, instrument: &Instrument, qty: &str, price: &str) {
        let contract = Order {
            order_id: "OPT".into(),
            account_id: "A".into(),
            instrument: instrument.clone(),
            order_type: OrderType::Limit,
            side: Side::Buy,
            quantity: money(qty),
            command_id: "OPT-cmd".into(),
            created_at: at("2026-11-23T15:00:00+00:00"),
            limit_price: Some(money(price)),
            stop_price: None,
            trail_amount: None,
            tif: Tif::Day,
            state: OrderState::Submitted,
            parent_order_id: None,
            oco_group: None,
        };
        created(l, vec![contract]);
        let f = Fill {
            fill_id: "FOPT".into(),
            order_id: "OPT".into(),
            account_id: "A".into(),
            instrument: instrument.clone(),
            quantity: money(qty),
            price: money(price),
            venue_env: "sim".into(),
            filled_at: at("2026-11-23T15:00:00+00:00"),
            side: Side::Buy,
            fee: money("0"),
            leg_id: None,
            venue_order_id: None,
            venue_execution_id: None,
        };
        put(l, "A", EventKind::Fill, Obj::Fill(f), "fill:FOPT");
    }

    fn quote(mid: &str, quote_as_of: &str) -> Option<OptionQuote> {
        Some(OptionQuote {
            mid: money(mid),
            quote_as_of: at(quote_as_of),
            snapshot_as_of: at("2026-11-24T11:59:00-05:00"),
        })
    }

    fn count_kind(l: &MemLedger, account: &str, kind: EventKind) -> usize {
        l.events(account).expect("events").iter().filter(|e| e.kind == kind).count()
    }

    fn open_micros(s: &str) -> i64 {
        micros(&at(s))
    }

    #[test]
    fn options_holders_keeps_accounts_with_a_nonzero_option_in_the_given_order() {
        let mut l = two_accounts();
        option_book(&mut l, &spy_call(), "1", "2.5");
        let holding = options_holders(&l, &["B".into(), "A".into()]).expect("holders");
        assert_eq!(holding, vec!["A".to_string()]);
        let none = options_holders(&l, &["B".into()]).expect("holders");
        assert!(none.is_empty());
    }

    #[test]
    fn credit_dividends_books_the_dividend_on_shares_held_before_the_open() {
        let mut l = MemLedger::default();
        positions_book(&mut l); // SPY 2 filled at 12:00; QQQ flat
        let open = open_micros("2026-11-24T12:30:00-05:00");
        credit_dividends(&mut l, &clock(), "A", day("2026-11-24"), open, true, |symbol| {
            assert_eq!(symbol, "SPY");
            Ok(vec![(at("2026-11-24T08:00:00-05:00"), money("0.50"))])
        })
        .expect("credit");
        let flow = l.event_by_command("dividend:A:SPY:2026-11-24").expect("read").expect("credited");
        match flow.payload {
            Obj::CashFlow(c) => {
                assert!(ops::eq(&c.amount, &money("1.00")).expect("cmp"));
                assert_eq!(c.kind, "dividend");
                // The per-share text is the `eod::dividend` rule's own (`ops::s`), which Python reads back: "0.5".
                assert_eq!(c.note.as_deref(), Some("SPY ex-dividend 2026-11-24: 0.5 a share on 2 held at the open"));
            }
            other => panic!("expected CashFlow, got {}", other.tag()),
        }
    }

    #[test]
    fn credit_dividends_with_nothing_held_at_the_open_needs_no_source_and_fetches_nothing() {
        let mut l = MemLedger::default();
        positions_book(&mut l);
        // The fills at 12:00 come after a 09:30 open, so nothing was held when it opened.
        let open = open_micros("2026-11-24T09:30:00-05:00");
        credit_dividends(&mut l, &clock(), "A", day("2026-11-24"), open, false, |_| {
            panic!("no shares, so no dividend lookup")
        })
        .expect("nothing to credit");
        assert_eq!(count_kind(&l, "A", EventKind::CashFlow), 0);
    }

    #[test]
    fn credit_dividends_refuses_shares_without_a_dividend_source() {
        let mut l = MemLedger::default();
        positions_book(&mut l);
        let open = open_micros("2026-11-24T12:30:00-05:00");
        let got = credit_dividends(&mut l, &clock(), "A", day("2026-11-24"), open, false, |_| Ok(vec![])).unwrap_err();
        assert_eq!(got.kind, "runtime_eod");
        assert!(got.msg.contains("no dividend source is configured"));
        assert_eq!(count_kind(&l, "A", EventKind::CashFlow), 0);
    }

    #[test]
    fn credit_dividends_skips_a_holding_with_no_dividend() {
        let mut l = MemLedger::default();
        positions_book(&mut l);
        let open = open_micros("2026-11-24T12:30:00-05:00");
        credit_dividends(&mut l, &clock(), "A", day("2026-11-24"), open, true, |_| Ok(vec![])).expect("no dividend");
        assert_eq!(count_kind(&l, "A", EventKind::CashFlow), 0);
    }

    #[test]
    fn credit_dividends_refuses_a_look_ahead_record_after_earlier_credits_are_written() {
        let mut l = MemLedger::default();
        positions_book(&mut l); // SPY 2
        created(&mut l, vec![order("I", "IWM", Side::Buy, OrderType::Limit, "3", None)]);
        fill(&mut l, "FI", "I", "IWM", Side::Buy, "3", "80");
        let open = open_micros("2026-11-24T12:30:00-05:00");
        // IWM sorts before SPY, so its credit lands before SPY's stale record refuses.
        let got = credit_dividends(&mut l, &clock(), "A", day("2026-11-24"), open, true, |symbol| {
            if symbol == "IWM" {
                Ok(vec![(at("2026-11-24T08:00:00-05:00"), money("0.10"))])
            } else {
                Ok(vec![(at("2026-11-24T13:00:00-05:00"), money("0.50"))])
            }
        })
        .unwrap_err();
        assert_eq!(got.kind, "runtime_replay");
        assert!(l.event_by_command("dividend:A:IWM:2026-11-24").expect("read").is_some(), "IWM credit written first");
        assert!(l.event_by_command("dividend:A:SPY:2026-11-24").expect("read").is_none(), "SPY refused");
        assert_eq!(count_kind(&l, "A", EventKind::CashFlow), 1);
    }

    #[test]
    fn credit_dividends_second_run_hits_the_ledgers_duplicate_command() {
        // Python does not skip an existing dividend command; the ledger's refusal surfaces.
        let mut l = MemLedger::default();
        positions_book(&mut l);
        let open = open_micros("2026-11-24T12:30:00-05:00");
        let credit = |l: &mut MemLedger| {
            credit_dividends(l, &clock(), "A", day("2026-11-24"), open, true, |_| {
                Ok(vec![(at("2026-11-24T08:00:00-05:00"), money("0.50"))])
            })
        };
        credit(&mut l).expect("first run");
        assert_eq!(credit(&mut l).unwrap_err().kind, "duplicate_command");
        assert_eq!(count_kind(&l, "A", EventKind::CashFlow), 1);
    }

    #[test]
    fn mark_options_account_marks_the_option_and_its_underlying_in_symbol_order() {
        let mut l = MemLedger::default();
        let call = spy_call();
        option_book(&mut l, &call, "1", "2.5");
        mark_options_account(
            &mut l,
            &clock(),
            "A",
            day("2026-11-24"),
            |_| Ok(quote("2.50", "2026-11-24T11:58:00-05:00")),
            |symbol| {
                assert_eq!(symbol, "SPY");
                Ok((money("100"), "official:SPY".into()))
            },
            300.0,
        )
        .expect("mark");
        let occ = call.symbol().expect("occ");
        let spy = l.event_by_command("eod:mark:A:2026-11-24:SPY").expect("read").expect("underlying marked");
        let opt = l.event_by_command(&format!("eod:mark:A:2026-11-24:{occ}")).expect("read").expect("option marked");
        // "SPY" sorts before the OCC symbol that extends it, so the underlying is appended first.
        assert!(spy.seq.expect("seq") < opt.seq.expect("seq"));
        match opt.payload {
            Obj::Mark(m) => {
                assert!(ops::eq(&m.price, &money("2.50")).expect("cmp"));
                assert_eq!(m.source.as_deref(), Some(format!("snapshot:{}", at("2026-11-24T11:59:00-05:00").iso()).as_str()));
            }
            other => panic!("expected Mark, got {}", other.tag()),
        }
        match spy.payload {
            Obj::Mark(m) => assert_eq!(m.source.as_deref(), Some("official:SPY")),
            other => panic!("expected Mark, got {}", other.tag()),
        }
    }

    #[test]
    fn mark_options_account_labels_a_quote_older_than_the_limit_as_stale() {
        let mut l = MemLedger::default();
        option_book(&mut l, &spy_call(), "1", "2.5");
        mark_options_account(
            &mut l,
            &clock(),
            "A",
            day("2026-11-24"),
            |_| Ok(quote("2.50", "2026-11-24T11:50:00-05:00")), // 540 seconds before the snapshot
            |_| Ok((money("100"), "official:SPY".into())),
            300.0,
        )
        .expect("mark");
        let occ = spy_call().symbol().expect("occ");
        let opt = l.event_by_command(&format!("eod:mark:A:2026-11-24:{occ}")).expect("read").expect("marked");
        match opt.payload {
            Obj::Mark(m) => assert!(m.source.expect("source").starts_with("stale-quote:2026-11-24T11:50:00")),
            other => panic!("expected Mark, got {}", other.tag()),
        }
    }

    #[test]
    fn mark_options_account_refuses_a_missing_or_zero_quote_and_writes_nothing() {
        for (name, found) in [("missing", None), ("zero", quote("0", "2026-11-24T11:58:00-05:00"))] {
            let mut l = MemLedger::default();
            option_book(&mut l, &spy_call(), "1", "2.5");
            let before = l.events("A").expect("events").len();
            let got = mark_options_account(
                &mut l,
                &clock(),
                "A",
                day("2026-11-24"),
                |_| Ok(found.clone()),
                |_| Ok((money("100"), "official:SPY".into())),
                300.0,
            )
            .unwrap_err();
            assert_eq!(got.kind, "runtime_replay", "{name} quote");
            assert_eq!(l.events("A").expect("events").len(), before, "{name} quote writes nothing");
        }
    }

    #[test]
    fn mark_options_account_refuses_a_stale_official_close_before_any_mark_is_written() {
        let mut l = MemLedger::default();
        option_book(&mut l, &spy_call(), "1", "2.5");
        let got = mark_options_account(
            &mut l,
            &clock(),
            "A",
            day("2026-11-24"),
            |_| Ok(quote("2.50", "2026-11-24T11:58:00-05:00")),
            |_| Err(LErr { kind: "runtime_replay", msg: "no close".into() }),
            300.0,
        )
        .unwrap_err();
        assert_eq!(got.kind, "runtime_replay");
        assert_eq!(count_kind(&l, "A", EventKind::Mark), 0);
    }

    #[test]
    fn mark_options_account_refuses_a_holding_that_cannot_be_marked() {
        let mut l = MemLedger::default();
        let future = make_future("ES").expect("future");
        option_book(&mut l, &future, "1", "5000");
        let got = mark_options_account(
            &mut l,
            &clock(),
            "A",
            day("2026-11-24"),
            |_| Ok(None),
            |_| Ok((money("5000"), "official:ES".into())),
            300.0,
        )
        .unwrap_err();
        assert_eq!(got.kind, "runtime_eod");
        assert!(got.msg.contains("cannot be marked"));
        assert_eq!(count_kind(&l, "A", EventKind::Mark), 0);
    }

    #[test]
    fn mark_options_account_skips_a_mark_already_written_for_the_session() {
        let mut l = MemLedger::default();
        let call = spy_call();
        option_book(&mut l, &call, "1", "2.5");
        let existing = Mark {
            instrument: make_equity("SPY").expect("equity"),
            price: money("99"),
            as_of: at("2026-11-24T12:00:00-05:00"),
            source: Some("old".into()),
        };
        put(&mut l, "A", EventKind::Mark, Obj::Mark(existing), "eod:mark:A:2026-11-24:SPY");
        mark_options_account(
            &mut l,
            &clock(),
            "A",
            day("2026-11-24"),
            |_| Ok(quote("2.50", "2026-11-24T11:58:00-05:00")),
            |_| Ok((money("100"), "official:SPY".into())),
            300.0,
        )
        .expect("mark");
        // The SPY mark already exists, so only the option is appended.
        assert_eq!(count_kind(&l, "A", EventKind::Mark), 2);
        let spy = l.event_by_command("eod:mark:A:2026-11-24:SPY").expect("read").expect("kept");
        match spy.payload {
            Obj::Mark(m) => assert!(ops::eq(&m.price, &money("99")).expect("cmp")),
            other => panic!("expected Mark, got {}", other.tag()),
        }
    }

    #[test]
    fn open_brackets_reports_the_open_entry_and_skips_one_without_a_stop() {
        let mut l = MemLedger::default();
        entry_with_brackets(&mut l);
        fill(&mut l, "F1", "E", "SPY", Side::Buy, "2", "100");
        // A second entry with a fill but no stop child: skipped.
        created(&mut l, vec![order("E2", "QQQ", Side::Buy, OrderType::Limit, "1", None)]);
        fill(&mut l, "F2", "E2", "QQQ", Side::Buy, "1", "50");

        let state = l.state("A").expect("state");
        let brackets = open_brackets(&state, "A", day("2026-11-24"), &OMap::new()).expect("brackets");
        assert_eq!(brackets.len(), 1);
        let b = &brackets[0];
        assert_eq!(b.entry_order_id, "E");
        assert!(ops::eq(&b.open_quantity, &money("2")).expect("cmp"));
        assert!(ops::eq(&b.entry_quantity, &money("2")).expect("cmp"));
        assert!(ops::eq(&b.average_entry_price, &money("100")).expect("cmp"));
        assert!(ops::eq(&b.stop_price, &money("95")).expect("cmp"));
        assert_eq!(b.entry_session, day("2026-11-23"));
        assert_eq!(b.sessions_held, 1);
        assert_eq!(b.targets_filled, 0);
        assert_eq!(b.open_targets.len(), 1);
        assert!(ops::eq(&b.open_targets[0].0, &money("110")).expect("cmp"));
        assert!(ops::eq(&b.open_targets[0].1, &money("1")).expect("cmp"));
        assert!(b.last_close.is_none());
    }

    #[test]
    fn derive_risk_context_values_only_marked_positions() {
        let mut l = MemLedger::default();
        positions_book(&mut l);
        deposit(&mut l, "A", "1000");
        let spy = make_equity("SPY").expect("equity");

        let state = l.state("A").expect("state");
        let unmarked = derive_risk_context(&state, &spy).expect("risk");
        // SPY has no mark yet, so equity is cash alone: 1000 - 200 - 50 + 51 = 801.
        let cash_only = unmarked.equity.expect("positive cash");
        assert!(ops::eq(&cash_only, &money("801")).expect("cmp"));
        assert_eq!(unmarked.open_positions, 1);
        assert_eq!(unmarked.current_position_quantity, 2);

        let mut closes: OMap<Instrument, Money> = OMap::new();
        closes.insert(spy.hk(), spy.clone(), money("101"));
        mark_positions(&mut l, &clock(), "eod", "A", &closes, day("2026-11-24")).expect("mark");
        let state = l.state("A").expect("state");
        let marked = derive_risk_context(&state, &spy).expect("risk");
        // Cash 1000 - 200 - 50 + 51 = 801, plus SPY 2 at 101.
        let equity = marked.equity.expect("positive equity");
        assert!(ops::eq(&equity, &money("1003")).expect("cmp"));
        assert!(ops::eq(&marked.gross_exposure, &money("202")).expect("cmp"));
    }

    #[test]
    fn derive_risk_context_signs_a_short_position() {
        let mut l = MemLedger::default();
        created(&mut l, vec![order("S", "SPY", Side::Sell, OrderType::Limit, "2", None)]);
        fill(&mut l, "F1", "S", "SPY", Side::Sell, "2", "100");
        deposit(&mut l, "A", "1000");
        let spy = make_equity("SPY").expect("equity");
        let mut closes: OMap<Instrument, Money> = OMap::new();
        closes.insert(spy.hk(), spy.clone(), money("101"));
        mark_positions(&mut l, &clock(), "eod", "A", &closes, day("2026-11-24")).expect("mark");

        let risk = derive_risk_context(&l.state("A").expect("state"), &spy).expect("risk");
        // Cash 1000 + 200 = 1200; SPY -2 at 101 subtracts 202.
        assert!(ops::eq(&risk.equity.expect("positive equity"), &money("998")).expect("cmp"));
        // Gross exposure uses |q|.
        assert!(ops::eq(&risk.gross_exposure, &money("202")).expect("cmp"));
        assert_eq!(risk.current_position_quantity, -2);
        assert_eq!(risk.open_positions, 1);
    }

    #[test]
    fn derive_risk_context_has_no_equity_when_it_is_not_positive() {
        let mut l = MemLedger::default();
        created(&mut l, vec![order("E", "SPY", Side::Buy, OrderType::Limit, "2", None)]);
        fill(&mut l, "F1", "E", "SPY", Side::Buy, "2", "100");
        deposit(&mut l, "A", "100");
        let spy = make_equity("SPY").expect("equity");

        // Unmarked: equity is cash alone, 100 - 200 = -100.
        let risk = derive_risk_context(&l.state("A").expect("state"), &spy).expect("risk");
        assert!(risk.equity.is_none());

        // Marked at 50: -100 + 2 * 50 = 0, which is not positive either.
        let mut closes: OMap<Instrument, Money> = OMap::new();
        closes.insert(spy.hk(), spy.clone(), money("50"));
        mark_positions(&mut l, &clock(), "eod", "A", &closes, day("2026-11-24")).expect("mark");
        let risk = derive_risk_context(&l.state("A").expect("state"), &spy).expect("risk");
        assert!(risk.equity.is_none());
        assert!(ops::eq(&risk.gross_exposure, &money("100")).expect("cmp"));
    }

    #[test]
    fn record_verdict_then_recorded_verdict_round_trips() {
        let mut l = MemLedger::default();
        deposit(&mut l, "A", "100");
        let verdict = RiskVerdict {
            order_intent_id: "intent-1".into(),
            accepted: true,
            evaluations: Vec::new(),
            refusal_reasons: Vec::new(),
            approved_quantity: Some(money("2")),
        };
        record_verdict(&mut l, &clock(), "A", "intent-1", verdict).expect("record");
        let got = recorded_verdict(&l, "intent-1").expect("read").expect("verdict");
        assert_eq!(got.order_intent_id, "intent-1");
        assert!(got.accepted);
        assert!(ops::eq(&got.approved_quantity.expect("qty"), &money("2")).expect("cmp"));
        assert!(recorded_verdict(&l, "intent-2").expect("read").is_none());
    }

    #[test]
    fn replay_instruments_sorts_by_symbol_and_keeps_working_entries() {
        let mut l = MemLedger::default();
        positions_book(&mut l);
        created(&mut l, vec![order("W", "AAPL", Side::Buy, OrderType::Limit, "1", None)]);
        let state = l.state("A").expect("state");
        let symbols: Vec<String> = replay_instruments(&state).expect("replay").iter()
            .map(|i| i.symbol().expect("symbol")).collect();
        assert_eq!(symbols, vec!["AAPL".to_string(), "SPY".to_string()]);
    }
}

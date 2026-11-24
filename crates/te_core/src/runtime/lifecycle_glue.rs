//! The after-close lifecycle pass in typed Rust over a [`Ledger`] (W6-F0a).
//!
//! Ports `LifecyclePass.run`, `_account`, `_price`, `_expiry`, `_early_assignment` and `_event`
//! of `src/trade_engine/lifecycle/after_close.py`. Each Python `decide(...)` call becomes a direct
//! call to the typed rule in [`crate::runtime::lifecycle`] with the same values, so every refusal
//! keeps the kind and text the rule raises.
//!
//! The settlement, dividend and quote sources are host plugins, passed in as closures
//! ([`Plugins`]). A closure's error is the caller's type, so the host can hold a Python exception
//! while the glue unwinds. Every refusal comes before the first append, so a refused pass writes
//! nothing (I2).

use chrono::{Datelike, NaiveDate, TimeZone, Utc};

use crate::ledger::fold::AccountState;
use crate::ledger::model::{
    DateTime, Event, EventKind, Instrument, LErr, Obj, OptionContract, OptionLifecycle, Side, MIRROR_ACCOUNT_PREFIX, R,
    SCHEMA_VERSION,
};
use crate::ledger::ops;
use crate::money::Money;
use crate::options::{self, OptionError, Right, SettleTime};
use crate::runtime::eod_glue::Ledger;
use crate::runtime::lifecycle as rules;
use crate::sim;

/// The settlement price a host gives for one underlying and session (the fields the pass reads).
#[derive(Debug, Clone)]
pub struct Settlement {
    pub underlying: String,
    pub session: NaiveDate,
    /// The settle time as the host states it (`SettleTime.value`). Compared as text, so a
    /// mismatch refuses as the Python identity check did (I5).
    pub settle_time: String,
    pub price: Money,
    pub source: String,
    pub as_of: DateTime,
}

/// A cash dividend per share (the fields the pass reads from a `Dividend`).
#[derive(Debug, Clone)]
pub struct Dividend {
    pub amount: Money,
    pub as_of: DateTime,
}

/// An option's latest quote (the fields the pass reads from an `OptionQuote`).
#[derive(Debug, Clone)]
pub struct Quote {
    pub bid: Money,
    pub as_of: DateTime,
}

/// The host's sources. `dividends` and `quote` are `None` when the host configured no source.
pub struct Plugins<'a, E> {
    pub settlement: &'a mut dyn FnMut(&str, NaiveDate, SettleTime) -> Result<Settlement, E>,
    pub dividends: Option<&'a mut dyn FnMut(&str, NaiveDate) -> Result<Vec<Dividend>, E>>,
    pub quote: Option<&'a mut dyn FnMut(&OptionContract, DateTime) -> Result<Quote, E>>,
}

/// `LifecyclePass.run`: settle `session`'s expiring positions and early assignments as of `now`.
/// `accounts` of `None` means every account in the ledger. Returns the events written, in order.
pub fn run<L: Ledger, E: From<LErr>>(
    l: &mut L,
    now: DateTime,
    session: NaiveDate,
    accounts: Option<&[String]>,
    mut p: Plugins<'_, E>,
) -> Result<Vec<Event>, E> {
    rules::session(sim::is_session(session)?, &iso(session))?;
    let close = sim::session_close(session)?;
    rules::close(micros(&now) < micros(&close.dt), &now.iso(), &iso(session), &close.iso)?;
    let names = match accounts {
        Some(list) => list.to_vec(),
        None => l.accounts()?,
    };
    let names = rules::accounts(&names);
    // Every account is decided before anything is written, so one refusal leaves the ledger as
    // it was and the re-run starts clean (I2).
    let mut events = Vec::new();
    for account in &names {
        events.extend(account_events(&*l, account, session, &now, &mut p)?);
    }
    if events.is_empty() {
        return Ok(Vec::new());
    }
    Ok(l.append(events)?)
}

// -- one account -----------------------------------------------------------------------

/// `_account`: the events for one account's option positions (or a mirror's book).
fn account_events<L: Ledger, E: From<LErr>>(
    l: &L,
    account: &str,
    session: NaiveDate,
    now: &DateTime,
    p: &mut Plugins<'_, E>,
) -> Result<Vec<Event>, E> {
    let state = l.state(account)?;
    let mirror = account.starts_with(MIRROR_ACCOUNT_PREFIX);
    let rows: Vec<(Instrument, Money)> = if mirror {
        mirror_rows(&state)?
    } else {
        state.positions.values().map(|position| (position.instrument.clone(), position.quantity.clone())).collect()
    };
    let occs = rows.iter().map(|(instrument, _)| occ_of(instrument)).collect::<R<Vec<String>>>()?;
    let quantities: Vec<String> = rows.iter().map(|(_, quantity)| text(quantity)).collect();
    let refs: Vec<(&str, &str, bool)> = rows
        .iter()
        .zip(occs.iter().zip(quantities.iter()))
        .map(|((instrument, _), (occ, quantity))| (occ.as_str(), quantity.as_str(), matches!(instrument, Instrument::Option(_))))
        .collect();
    let mut events = Vec::new();
    for index in rules::held(&refs)? {
        let (instrument, quantity) = &rows[index as usize];
        let Instrument::Option(contract) = instrument else { continue };
        let days = (contract.expiry - session).num_days();
        let (long, due, past, magnitude) = rules::position(&text(quantity), days)?;
        let side = if long { Side::Buy } else { Side::Sell };
        let quantity = dec(&magnitude)?;
        if due {
            settle_instant(contract)?; // refuses a contract whose expiry is not a session
            let occ = contract.occ()?;
            rules::overdue(past, occ.trim(), account, &iso(contract.expiry))?;
            events.push(expiry_event(account, contract, side, &quantity, session, now, p)?);
            continue;
        }
        // A venue's mirror books what the venue reports; it never decides an early assignment.
        if mirror {
            continue;
        }
        if let Some(event) = early_assignment(account, contract, side, &quantity, session, now, p)? {
            events.push(event);
        }
    }
    Ok(events)
}

/// `_mirror_net`: a venue mirror's holdings per option contract, its book summed over the
/// strategy accounts. Zero nets are dropped, as the rule drops them.
fn mirror_rows(state: &AccountState) -> R<Vec<(Instrument, Money)>> {
    let mut contracts: Vec<(OptionContract, Money)> = Vec::new();
    for ((_, instrument), quantity) in state.mirror.book.iter() {
        if let Instrument::Option(contract) = instrument {
            contracts.push((contract.clone(), quantity.clone()));
        }
    }
    let mut texts: Vec<(String, String)> = Vec::with_capacity(contracts.len());
    for (contract, quantity) in &contracts {
        texts.push((contract.occ()?, text(quantity)));
    }
    let refs: Vec<(&str, &str)> = texts.iter().map(|(occ, quantity)| (occ.as_str(), quantity.as_str())).collect();
    let mut rows = Vec::new();
    for (_, net, first) in rules::mirror_net(&refs)? {
        rows.push((Instrument::Option(contracts[first as usize].0.clone()), dec(&net)?));
    }
    Ok(rows)
}

// -- one settlement ------------------------------------------------------------------------

/// `_price`: the settlement the host gives for `underlying` on `session`, checked against the
/// settlement asked for (I5), the clock (I7) and the settlement instant (I9).
fn settle_price<E: From<LErr>>(
    p: &mut Plugins<'_, E>,
    underlying: &str,
    session: NaiveDate,
    settle_time: SettleTime,
    settled_at: &DateTime,
    now: &DateTime,
) -> Result<Settlement, E> {
    let price = (p.settlement)(underlying, session, settle_time)?;
    let asked_session = iso(session);
    let got_session = iso(price.session);
    rules::price_identity(
        [settle_time.code(), underlying, asked_session.as_str()],
        [price.settle_time.as_str(), price.underlying.as_str(), got_session.as_str()],
    )?;
    let stamp = price.as_of.iso();
    rules::price_clock(micros(&price.as_of) > micros(now), underlying, &stamp, &now.iso())?;
    rules::price_known(micros(&price.as_of) < micros(settled_at), underlying, &stamp, &settled_at.iso())?;
    Ok(price)
}

// -- expiry ----------------------------------------------------------------------------------

/// `_expiry`: a contract expiring this session settles on its official price. In the money by
/// the OCC threshold it is exercised (long) or assigned (short); otherwise it expires worthless.
fn expiry_event<E: From<LErr>>(
    account: &str,
    contract: &OptionContract,
    side: Side,
    quantity: &Money,
    session: NaiveDate,
    now: &DateTime,
    p: &mut Plugins<'_, E>,
) -> Result<Event, E> {
    let style = options::option_style(&contract.underlying).map_err(option_err)?;
    let settled_at = settle_instant(contract)?;
    let price = settle_price(p, &style.underlying, session, style.settle_time, &settled_at, now)?;
    let (kind, reason) = rules::expiry(
        &text(&contract.strike),
        &text(&price.price),
        style.settle_time.code(),
        &contract.underlying,
        &ymd(contract.expiry),
        matches!(contract.right, Right::Call),
        matches!(side, Side::Buy),
    )?;
    lifecycle_event(account, contract, side, quantity, &price, now, event_kind(&kind), &reason, session, false)
}

// -- early assignment ------------------------------------------------------------------------

/// `_early_assignment`: a short American call in the money at the session's close, whose shares
/// go ex-dividend next session, is assigned when the dividend is worth more than its bid less
/// intrinsic value (O2).
fn early_assignment<E: From<LErr>>(
    account: &str,
    contract: &OptionContract,
    side: Side,
    quantity: &Money,
    session: NaiveDate,
    now: &DateTime,
    p: &mut Plugins<'_, E>,
) -> Result<Option<Event>, E> {
    // Evaluated before the flag, as the Python list is: an unknown root refuses here.
    let can_exercise = options::lifecycle::can_exercise_early(&contract.wire()).map_err(option_err)?;
    let call = matches!(contract.right, Right::Call);
    if !rules::eligible(matches!(side, Side::Sell), call, can_exercise) {
        return Ok(None);
    }
    let occ = contract.occ()?;
    let underlying = options::option_style(&contract.underlying).map_err(option_err)?.underlying;
    let ex_date = sim::next_session(session)?;
    rules::dividend_source(p.dividends.is_some(), account, occ.trim())?;
    let Some(fetch) = p.dividends.as_mut() else {
        unreachable!("dividend_source refuses when no dividend source is configured")
    };
    let dividends = fetch(&underlying, ex_date)?;
    for dividend in &dividends {
        rules::dividend_time(micros(&dividend.as_of) > micros(now), &underlying)?;
    }
    let amounts: Vec<String> = dividends.iter().map(|dividend| text(&dividend.amount)).collect();
    let (total, any) = rules::sum(&amounts)?;
    if !any {
        return Ok(None);
    }
    let close_at = sim::session_close(session)?.dt;
    let close = settle_price(p, &underlying, session, SettleTime::Pm, &close_at, now)?;
    if !rules::itm(&text(&contract.strike), &text(&close.price), &contract.underlying, &ymd(contract.expiry), call)? {
        return Ok(None);
    }
    rules::quote_source(p.quote.is_some(), occ.trim(), account, &total)?;
    let Some(fetch) = p.quote.as_mut() else {
        unreachable!("quote_source refuses when no quote source is configured")
    };
    let quote = fetch(contract, now.clone())?;
    rules::quote_time(micros(&quote.as_of) > micros(now), occ.trim())?;
    let (exercise, reason) = rules::early(
        &text(&contract.strike),
        &text(&close.price),
        &text(&quote.bid),
        &total,
        &iso(ex_date),
        &contract.underlying,
        &ymd(contract.expiry),
    )?;
    if !exercise {
        return Ok(None);
    }
    lifecycle_event(account, contract, side, quantity, &close, now, EventKind::Assignment, &reason, session, true)
        .map(Some)
}

// -- events ----------------------------------------------------------------------------------

/// `_event`: the `OptionLifecycle` event for one settled position. The rule builds the command
/// id, so a re-run appends nothing (I3).
#[allow(clippy::too_many_arguments)]
fn lifecycle_event<E: From<LErr>>(
    account: &str,
    contract: &OptionContract,
    side: Side,
    quantity: &Money,
    price: &Settlement,
    now: &DateTime,
    kind: EventKind,
    reason: &str,
    session: NaiveDate,
    early: bool,
) -> Result<Event, E> {
    let occ = contract.occ()?;
    let quantity_text = text(quantity);
    let price_text = text(&price.price);
    let session_text = iso(session);
    let (fields, flag) = rules::event(
        [account, occ.as_str(), quantity_text.as_str(), price_text.as_str(), price.source.as_str(), session_text.as_str()],
        early,
    );
    let payload = Obj::Lifecycle(OptionLifecycle {
        account_id: fields[0].clone(),
        contract: contract.clone(),
        quantity: dec(&fields[2])?,
        held: side,
        underlying_price: dec(&fields[3])?,
        price_source: fields[4].clone(),
        as_of: now.clone(),
        reason: reason.to_string(),
        early: flag,
    });
    Ok(Event {
        account: fields[0].clone(),
        kind,
        payload,
        ts_utc: now.clone(),
        command_id: Some(fields[5].clone()),
        schema_version: SCHEMA_VERSION,
        seq: None,
    })
}

// -- helpers ---------------------------------------------------------------------------------

/// `settlement_instant(contract, calendar)`. A refusal from the rule becomes a lifecycle refusal
/// with the same text (I5); an unresolvable root, or an arithmetic refusal, keeps its kind.
fn settle_instant(contract: &OptionContract) -> R<DateTime> {
    let secs = options::settlement_instant(&contract.wire()).map_err(|e| {
        let err = option_err(e);
        match err.kind {
            "unresolvable" | "invalid_operation" | "overflow" => err,
            _ => LErr { kind: "lifecycle", msg: err.msg },
        }
    })?;
    let Some(at) = Utc.timestamp_opt(secs, 0).single() else {
        return Err(LErr { kind: "overflow", msg: "time value out of range".into() });
    };
    Ok(sim::Ts::utc(at).dt)
}

fn option_err(e: OptionError) -> LErr {
    LErr { kind: e.kind(), msg: e.message().to_string() }
}

fn event_kind(kind: &str) -> EventKind {
    match kind {
        "Expiry" => EventKind::Expiry,
        "Exercise" => EventKind::Exercise,
        _ => EventKind::Assignment,
    }
}

fn occ_of(instrument: &Instrument) -> R<String> {
    match instrument {
        Instrument::Option(contract) => contract.occ(),
        _ => Ok(String::new()),
    }
}

fn ymd(date: NaiveDate) -> [i64; 3] {
    [date.year() as i64, date.month() as i64, date.day() as i64]
}

fn iso(date: NaiveDate) -> String {
    format!("{:04}-{:02}-{:02}", date.year(), date.month(), date.day())
}

/// The instant as one integer (microseconds since 0001-01-01 UTC): orders like the instant.
fn micros(dt: &DateTime) -> i64 {
    let (secs, us) = dt.instant();
    secs * 1_000_000 + us as i64
}

fn dec(value: &str) -> R<Money> {
    Money::parse(value).ok_or_else(|| LErr { kind: "value", msg: "invalid runtime decimal".into() })
}

fn text(value: &Money) -> String {
    ops::s(value)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::ledger::model::{Fill, Order, OrderState, OrderType, OrdersCreated, Tif};
    use crate::runtime::eod_glue::tests::{at, money, put, MemLedger};

    fn day(s: &str) -> NaiveDate {
        NaiveDate::parse_from_str(s, "%Y-%m-%d").expect("date")
    }

    fn spy_call(expiry: &str) -> Instrument {
        let contract = crate::ledger::model::make_option(
            "SPY",
            day(expiry),
            crate::ledger::model::Val::Dec(money("100")),
            100,
            crate::ledger::model::Val::Right(Right::Call),
        );
        Instrument::Option(contract.expect("option"))
    }

    /// A settlement for `underlying` on `session` at `price`, stamped `as_of`.
    fn settlement(underlying: &'static str, price: &'static str, as_of: &'static str)
        -> impl FnMut(&str, NaiveDate, SettleTime) -> Result<Settlement, LErr>
    {
        move |_, session, time| {
            Ok(Settlement {
                underlying: underlying.into(),
                session,
                settle_time: time.code().into(),
                price: money(price),
                source: "test".into(),
                as_of: at(as_of),
            })
        }
    }

    /// One option fill for `account`, booked as an order then its fill (eod_glue's `option_book`
    /// with the account given, since that helper books into "A" only).
    fn book(l: &mut MemLedger, account: &str, instrument: &Instrument, qty: &str, price: &str, side: Side) {
        let order = Order {
            order_id: format!("OPT-{account}"),
            account_id: account.into(),
            instrument: instrument.clone(),
            order_type: OrderType::Limit,
            side,
            quantity: money(qty),
            command_id: format!("OPT-{account}-cmd"),
            created_at: at("2026-11-23T15:00:00+00:00"),
            limit_price: Some(money(price)),
            stop_price: None,
            trail_amount: None,
            tif: Tif::Day,
            state: OrderState::Submitted,
            parent_order_id: None,
            oco_group: None,
        };
        let command = format!("orders:{}", order.order_id);
        let created = OrdersCreated { orders: vec![order.clone()], fingerprint: "fp".into(), reason: "test".into() };
        put(l, account, EventKind::OrdersCreated, Obj::OrdersCreated(created), &command);
        let fill = Fill {
            fill_id: format!("F-{account}"),
            order_id: order.order_id.clone(),
            account_id: account.into(),
            instrument: instrument.clone(),
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
        put(l, account, EventKind::Fill, Obj::Fill(fill), &format!("fill:F-{account}"));
    }

    fn plugins<'a>(
        settle: &'a mut dyn FnMut(&str, NaiveDate, SettleTime) -> Result<Settlement, LErr>,
    ) -> Plugins<'a, LErr> {
        Plugins { settlement: settle, dividends: None, quote: None }
    }

    #[test]
    fn expiry_in_the_money_long_is_exercised() {
        let mut l = MemLedger::default();
        book(&mut l, "A", &spy_call("2026-12-18"), "2", "3", Side::Buy);
        let mut settle = settlement("SPY", "110", "2026-12-18T21:05:00+00:00");
        let events = run(&mut l, at("2026-12-18T22:00:00+00:00"), day("2026-12-18"), None, plugins(&mut settle))
            .expect("pass");
        assert_eq!(events.len(), 1);
        assert_eq!(events[0].kind, EventKind::Exercise);
        assert_eq!(events[0].command_id.as_deref(), Some("lifecycle:A:SPY261218C00100000:2026-12-18"));
    }

    #[test]
    fn expiry_worthless_below_the_threshold_expires() {
        let mut l = MemLedger::default();
        book(&mut l, "A", &spy_call("2026-12-18"), "2", "3", Side::Buy);
        let mut settle = settlement("SPY", "90", "2026-12-18T21:05:00+00:00");
        let events = run(&mut l, at("2026-12-18T22:00:00+00:00"), day("2026-12-18"), None, plugins(&mut settle))
            .expect("pass");
        assert_eq!(events.len(), 1);
        assert_eq!(events[0].kind, EventKind::Expiry);
    }

    #[test]
    fn an_option_expired_in_an_earlier_session_refuses() {
        let mut l = MemLedger::default();
        book(&mut l, "A", &spy_call("2026-12-11"), "1", "3", Side::Buy);
        let mut settle = settlement("SPY", "110", "2026-12-21T21:05:00+00:00");
        let err = run(&mut l, at("2026-12-21T22:00:00+00:00"), day("2026-12-21"), None, plugins(&mut settle))
            .expect_err("refusal");
        assert_eq!(err.kind, "lifecycle");
        assert!(err.msg.contains("expired on 2026-12-11 and was never settled"), "{}", err.msg);
    }

    #[test]
    fn a_price_identity_mismatch_refuses() {
        let mut l = MemLedger::default();
        book(&mut l, "A", &spy_call("2026-12-18"), "1", "3", Side::Buy);
        let mut settle = settlement("QQQ", "110", "2026-12-18T21:05:00+00:00");
        let err = run(&mut l, at("2026-12-18T22:00:00+00:00"), day("2026-12-18"), None, plugins(&mut settle))
            .expect_err("refusal");
        assert_eq!(err.kind, "lifecycle");
        assert!(err.msg.contains("settlement of SPY on 2026-12-18, got "), "{}", err.msg);
        assert!(err.msg.ends_with("QQQ on 2026-12-18 (I5)"), "{}", err.msg);
    }

    #[test]
    fn a_settlement_known_before_its_instant_refuses() {
        let mut l = MemLedger::default();
        book(&mut l, "A", &spy_call("2026-12-18"), "1", "3", Side::Buy);
        let mut settle = settlement("SPY", "110", "2026-12-18T20:00:00+00:00");
        let err = run(&mut l, at("2026-12-18T22:00:00+00:00"), day("2026-12-18"), None, plugins(&mut settle))
            .expect_err("refusal");
        assert_eq!(err.kind, "lifecycle");
        assert!(err.msg.ends_with("it cannot be the official price (I9)"), "{}", err.msg);
    }

    #[test]
    fn a_clock_before_the_close_refuses() {
        let mut l = MemLedger::default();
        let mut settle = settlement("SPY", "110", "2026-12-18T21:05:00+00:00");
        let err = run(&mut l, at("2026-12-18T20:00:00+00:00"), day("2026-12-18"), None, plugins(&mut settle))
            .expect_err("refusal");
        assert_eq!(err.kind, "lifecycle");
        assert!(err.msg.contains("expiry and assignment are decided after the close (I9)"), "{}", err.msg);
    }

    #[test]
    fn a_short_itm_call_is_assigned_before_a_dividend_worth_more_than_its_time_value() {
        let mut l = MemLedger::default();
        book(&mut l, "A", &spy_call("2027-01-15"), "1", "3", Side::Sell);
        let mut settle = settlement("SPY", "110", "2026-12-17T21:30:00+00:00");
        let mut dividends = |_: &str, _: NaiveDate| -> Result<Vec<Dividend>, LErr> {
            Ok(vec![Dividend { amount: money("2.00"), as_of: at("2026-12-16T12:00:00+00:00") }])
        };
        let mut quote = |_: &OptionContract, _: DateTime| -> Result<Quote, LErr> {
            Ok(Quote { bid: money("10.50"), as_of: at("2026-12-17T21:45:00+00:00") })
        };
        let pass = Plugins { settlement: &mut settle, dividends: Some(&mut dividends), quote: Some(&mut quote) };
        let events = run(&mut l, at("2026-12-17T22:00:00+00:00"), day("2026-12-17"), None, pass).expect("pass");
        assert_eq!(events.len(), 1);
        assert_eq!(events[0].kind, EventKind::Assignment);
        match &events[0].payload {
            Obj::Lifecycle(lifecycle) => assert!(lifecycle.early),
            other => panic!("expected an OptionLifecycle payload, got {other:?}"),
        }
    }

    #[test]
    fn a_refusal_in_a_later_account_writes_nothing() {
        let mut l = MemLedger::default();
        book(&mut l, "A", &spy_call("2026-12-18"), "1", "3", Side::Buy);
        book(&mut l, "B", &spy_call("2026-12-11"), "1", "3", Side::Buy);
        let before = l.events("A").expect("events").len();
        let mut settle = settlement("SPY", "110", "2026-12-18T21:05:00+00:00");
        let err = run(&mut l, at("2026-12-18T22:00:00+00:00"), day("2026-12-18"), None, plugins(&mut settle))
            .expect_err("refusal");
        assert_eq!(err.kind, "lifecycle");
        assert_eq!(l.events("A").expect("events").len(), before);
    }
}

//! Venue reconcile and read-back confirmation (was `tos_paper/reconcile.py`): the drift
//! check that halts a venue, and what one sent ticket's read-back proves (I5).
//!
//! Instruments cross as the wire documents of [`super::wire`], read into the ledger
//! model's `Instrument` so equality, hashing and `symbol` are the ledger's. A map crosses
//! as a list of `[instrument, "qty"]` pairs in insertion order.

use super::wire::{
    dec_of, jstr, linstr, linstr_json, obj, opt_dec, order_type_of, req, req_arr, req_dec, req_str, side_of, state_of,
    wire,
};
use crate::ledger::json::Json;
use crate::ledger::mirror::{signed_contracts, SignAt};
use crate::ledger::model::{err, opt_dec_eq, parse_datetime, Instrument, OrderState, OrderType, Side, R};
use crate::ledger::ops::{add, eq, ne, neg, s, sub, zero, OMap};
use crate::money::Money;

pub const UNREADABLE: &str = "<venue unreadable>";

/// One contract's signed total, the first key spelling kept.
pub type Book = OMap<Instrument, Money>;

/// `_signed`: a BUY is the quantity itself, a SELL its negation.
pub fn signed(side: Side, quantity: &Money) -> R<Money> {
    if side == Side::Buy {
        Ok(quantity.clone())
    } else {
        neg(quantity)
    }
}

pub struct Row {
    pub instrument: Instrument,
    pub side: Side,
    pub quantity: Money,
    pub filled: Money,
    pub order_type: OrderType,
    pub limit_price: Option<Money>,
    pub state: OrderState,
}

impl Row {
    pub fn live(&self) -> bool {
        matches!(self.state, OrderState::Submitted | OrderState::Accepted | OrderState::PartiallyFilled)
    }

    pub fn remaining(&self) -> R<Money> {
        sub(&self.quantity, &self.filled)
    }
}

pub fn row_of(j: &Json) -> R<Row> {
    Ok(Row {
        instrument: linstr(req(j, "instrument")?)?,
        side: side_of(req_str(j, "side")?)?,
        quantity: req_dec(j, "quantity")?,
        filled: req_dec(j, "filled")?,
        order_type: order_type_of(req_str(j, "order_type")?)?,
        limit_price: opt_dec(j, "limit_price")?,
        state: state_of(req_str(j, "state")?)?,
    })
}

pub fn rows_of(j: &Json, key: &str) -> R<Vec<Row>> {
    req_arr(j, key)?.iter().map(row_of).collect()
}

/// `[instrument, "qty"]` pairs.
pub fn pairs_of(j: &Json, key: &str) -> R<Book> {
    let mut book = Book::new();
    for pair in req_arr(j, key)? {
        let Json::Arr(p) = pair else { return wire(format!("{key:?} holds a non-pair")) };
        let [i, q] = p.as_slice() else { return wire(format!("{key:?} holds a non-pair")) };
        let Json::Str(q) = q else { return wire(format!("{key:?} holds a non-text quantity")) };
        let i = linstr(i)?;
        book.insert(i.hk(), i, dec_of(q)?);
    }
    Ok(book)
}

pub fn pairs_json(book: &Book) -> Json {
    Json::Arr(book.iter().map(|(i, q)| Json::Arr(vec![linstr_json(i), jstr(s(q))])).collect())
}

pub fn positions_of(j: &Json) -> R<Vec<(Instrument, Money)>> {
    let mut out = Vec::new();
    for p in req_arr(j, "positions")? {
        out.push((linstr(req(p, "instrument")?)?, req_dec(p, "quantity")?));
    }
    Ok(out)
}

/// `position_book`: the venue's positions summed per contract.
pub fn position_book(positions: &[(Instrument, Money)]) -> R<Book> {
    let mut book = Book::new();
    for (instrument, quantity) in positions {
        let hk = instrument.hk();
        let current = book.get(&hk).cloned().unwrap_or_else(zero);
        book.insert(hk, instrument.clone(), add(&current, quantity)?);
    }
    Ok(book)
}

pub fn get(book: &Book, instrument: &Instrument) -> Money {
    book.get(&instrument.hk()).cloned().unwrap_or_else(zero)
}

/// The `VenueReconcile` event, as the ledger's constructor builds it (its refusals included).
#[derive(Debug)]
pub struct ReconcileEvent {
    pub venue: String,
    pub as_of: String,
    pub reconciled: bool,
    pub drift: Vec<String>,
    pub note: Option<String>,
}

impl ReconcileEvent {
    fn json(&self) -> Json {
        obj(vec![
            ("venue", jstr(self.venue.clone())),
            ("as_of", jstr(self.as_of.clone())),
            ("reconciled", Json::Bool(self.reconciled)),
            ("drift", Json::Arr(self.drift.iter().cloned().map(Json::Str).collect())),
            ("note", self.note.clone().map_or(Json::Null, Json::Str)),
        ])
    }
}

/// The `VenueReconcile` event, with the constructor's refusals applied (its refusals included).
pub fn venue_reconcile_event(
    venue: &str,
    as_of: &str,
    reconciled: bool,
    drift: Vec<String>,
    note: Option<String>,
) -> R<ReconcileEvent> {
    if venue.is_empty() {
        return err("payload", "VenueReconcile.venue must be non-empty");
    }
    let (_, aware) = parse_datetime(as_of)?;
    if !aware {
        return err("payload", "VenueReconcile.as_of must be timezone-aware UTC datetime (I7)");
    }
    if reconciled && !drift.is_empty() {
        return err("payload", "VenueReconcile cannot be reconciled while listing drift instruments");
    }
    if !reconciled && drift.is_empty() {
        return err("payload", "VenueReconcile that is not reconciled must name the drifting instruments (I11)");
    }
    Ok(ReconcileEvent { venue: venue.to_string(), as_of: as_of.to_string(), reconciled, drift, note })
}

/// The `VenueReconcile` event as its JSON door form.
pub fn venue_reconcile(venue: &str, as_of: &str, reconciled: bool, drift: Vec<String>, note: Option<String>) -> R<Json> {
    Ok(venue_reconcile_event(venue, as_of, reconciled, drift, note)?.json())
}

/// `reconcile`: per contract, the venue position plus the live working remainder must equal
/// what the mirror expects; a contract with an unknown-state row always drifts.
pub fn reconcile_event(
    venue: &str,
    as_of: &str,
    expected: &Book,
    positions: &[(Instrument, Money)],
    working: &[Row],
) -> R<ReconcileEvent> {
    let held = position_book(positions)?;
    let mut resting = Book::new();
    let mut unknown: OMap<Instrument, ()> = OMap::new();
    for row in working {
        if row.state == OrderState::PendingUnknown {
            unknown.insert(row.instrument.hk(), row.instrument.clone(), ());
        } else if row.live() {
            let current = get(&resting, &row.instrument);
            let total = add(&current, &signed(row.side, &row.remaining()?)?)?;
            resting.insert(row.instrument.hk(), row.instrument.clone(), total);
        }
    }
    let mut contracts: OMap<Instrument, ()> = OMap::new();
    for (i, _) in expected.iter().chain(held.iter()).chain(resting.iter()) {
        contracts.insert(i.hk(), i.clone(), ());
    }
    for (i, _) in unknown.iter() {
        contracts.insert(i.hk(), i.clone(), ());
    }
    let mut drift = Vec::new();
    for (contract, _) in contracts.iter() {
        let want = get(expected, contract);
        let have = add(&get(&held, contract), &get(&resting, contract))?;
        if unknown.contains(&contract.hk()) || ne(&have, &want)? {
            drift.push(contract.symbol()?);
        }
    }
    if !drift.is_empty() {
        drift.sort();
        return venue_reconcile_event(
            venue,
            as_of,
            false,
            drift,
            Some("venue positions + working orders disagree with the mirror book; venue halted".into()),
        );
    }
    venue_reconcile_event(venue, as_of, true, Vec::new(), None)
}

/// `reconcile`: the `VenueReconcile` event as its JSON door form.
pub fn reconcile(
    venue: &str,
    as_of: &str,
    expected: &Book,
    positions: &[(Instrument, Money)],
    working: &[Row],
) -> R<Json> {
    Ok(reconcile_event(venue, as_of, expected, positions, working)?.json())
}

/// `unreadable`: drift on every contract in play, or on the unreadable marker.
pub fn unreadable_event(venue: &str, as_of: &str, contracts: &[Instrument], why: &str) -> R<ReconcileEvent> {
    let mut names: Vec<String> = Vec::new();
    for c in contracts {
        let symbol = c.symbol()?;
        if !names.contains(&symbol) {
            names.push(symbol);
        }
    }
    names.sort();
    if names.is_empty() {
        names.push(UNREADABLE.to_string());
    }
    venue_reconcile_event(
        venue,
        as_of,
        false,
        names,
        Some(format!("cannot read the venue ({why}); refusing to assume it matches (I5)")),
    )
}

/// `unreadable`: drift on every contract in play, or on the unreadable marker.
pub fn unreadable(venue: &str, as_of: &str, contracts: &[Instrument], why: &str) -> R<Json> {
    Ok(unreadable_event(venue, as_of, contracts, why)?.json())
}

pub struct Ticket {
    pub instrument: Instrument,
    pub order_type: OrderType,
    pub side: Side,
    pub quantity: Money,
    pub limit_price: Option<Money>,
}

#[cfg(test)]
fn ticket_of(j: &Json) -> R<Ticket> {
    Ok(Ticket {
        instrument: linstr(req(j, "instrument")?)?,
        order_type: order_type_of(req_str(j, "order_type")?)?,
        side: side_of(req_str(j, "side")?)?,
        quantity: req_dec(j, "quantity")?,
        limit_price: opt_dec(j, "limit_price")?,
    })
}

/// `ticket_contracts` of the reconcile module: signed contracts `units` (default all) of a ticket.
pub fn ticket_contracts(ticket: &Ticket, units: Option<&Money>) -> R<Book> {
    let units = units.unwrap_or(&ticket.quantity);
    signed_contracts(&ticket.instrument, ticket.side, units, SignAt::Last)
}

type Verdict = (&'static str, String);

/// `confirm_ticket`: `(status, reason)` the read-back proves. `claimed` holds the indexes of
/// `working` rows earlier tickets of the batch already matched.
pub fn confirm_ticket(
    ticket: &Ticket,
    before: &Book,
    positions: &[(Instrument, Money)],
    working: &[Row],
    claimed: &mut Vec<usize>,
) -> R<Verdict> {
    if matches!(ticket.instrument, Instrument::Combo(_)) {
        return confirm_combo(ticket, before, positions, working, claimed);
    }
    let limit = if ticket.order_type == OrderType::Limit { ticket.limit_price.clone() } else { None };
    for (index, row) in working.iter().enumerate() {
        if claimed.contains(&index) {
            continue;
        }
        if row.instrument.same(&ticket.instrument)
            && row.side == ticket.side
            && eq(&row.quantity, &ticket.quantity)?
            && opt_dec_eq(&row.limit_price, &limit)
            && row.order_type == ticket.order_type
        {
            claimed.push(index);
            if row.live() {
                return Ok(("ACCEPTED", format!("on the order book ({}, filled {})", row.state.value(), s(&row.filled))));
            }
            if row.state == OrderState::Filled {
                return Ok(("ACCEPTED", "filled (order book)".into()));
            }
            if row.state == OrderState::PendingUnknown {
                return Ok(("PENDING", "order book row in an unknown state".into()));
            }
            return Ok(("REJECTED", format!("venue order book shows {}", row.state.value())));
        }
    }
    let now = get(&position_book(positions)?, &ticket.instrument);
    let moved = sub(&now, &get(before, &ticket.instrument))?;
    if eq(&moved, &signed(ticket.side, &ticket.quantity)?)? {
        return Ok(("ACCEPTED", format!("filled (position moved {})", s(&moved))));
    }
    Ok(("PENDING", "not visible on the order book or in positions yet".into()))
}

/// `_confirm_combo`: a vertical is proven leg by leg, a matching row for every leg or every
/// leg's position moved. The legs of one order share its state.
fn confirm_combo(
    ticket: &Ticket,
    before: &Book,
    positions: &[(Instrument, Money)],
    working: &[Row],
    claimed: &mut Vec<usize>,
) -> R<Verdict> {
    let Instrument::Combo(legs) = &ticket.instrument else { unreachable!("checked by the caller") };
    let mut rows: Vec<usize> = Vec::new();
    let mut all = true;
    for leg in legs {
        let mut found = None;
        for (index, row) in working.iter().enumerate() {
            if !claimed.contains(&index)
                && !rows.contains(&index)
                && row.instrument.same(&leg.contract)
                && row.side == leg.side
                && eq(&row.quantity, &crate::ledger::ops::mul_i(&ticket.quantity, leg.ratio)?)?
                && row.order_type == ticket.order_type
                && opt_dec_eq(&row.limit_price, &ticket.limit_price)
            {
                found = Some(index);
                break;
            }
        }
        match found {
            Some(index) => rows.push(index),
            None => {
                all = false;
                break;
            }
        }
    }
    if all {
        claimed.extend(rows.iter().copied());
        let states: Vec<OrderState> = rows.iter().map(|&i| working[i].state).collect();
        if states.contains(&OrderState::PendingUnknown) {
            return Ok(("PENDING", "a leg's order book row is in an unknown state".into()));
        }
        let ended = states.iter().find(|st| {
            **st != OrderState::Filled
                && !matches!(st, OrderState::Submitted | OrderState::Accepted | OrderState::PartiallyFilled)
        });
        if let Some(st) = ended {
            return Ok(("REJECTED", format!("venue order book shows {}", st.value())));
        }
        let names: Vec<&str> = states.iter().map(|st| st.value()).collect();
        return Ok(("ACCEPTED", format!("on the order book, both legs ({})", names.join(", "))));
    }
    let held = position_book(positions)?;
    for (contract, moved) in ticket_contracts(ticket, None)?.iter() {
        let delta = sub(&get(&held, contract), &get(before, contract))?;
        if !eq(&delta, moved)? {
            return Ok(("PENDING", "not visible on the order book or in positions yet".into()));
        }
    }
    Ok(("ACCEPTED", "filled (every leg's position moved)".into()))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::ledger::json::parse;

    const P200: &str = r#"{"kind":"option","underlying":"AAPL","expiry":"2026-10-16","strike":"200","right":"P","multiplier":100}"#;
    const P195: &str = r#"{"kind":"option","underlying":"AAPL","expiry":"2026-10-16","strike":"195","right":"P","multiplier":100}"#;

    fn doc(text: &str) -> Json {
        parse(text).unwrap()
    }

    fn drift(out: &Json) -> Vec<String> {
        match out.get("drift") {
            Some(Json::Arr(a)) => a.iter().map(|d| if let Json::Str(s) = d { s.clone() } else { String::new() }).collect(),
            _ => panic!("no drift"),
        }
    }

    fn reconcile_of(expected: &str, positions: &str, working: &str) -> Json {
        let doc = doc(&format!(
            r#"{{"venue":"D-1","as_of":"2026-09-24T20:00:00+00:00","expected":{expected},"positions":{positions},"working":{working}}}"#
        ));
        reconcile(
            req_str(&doc, "venue").unwrap(),
            req_str(&doc, "as_of").unwrap(),
            &pairs_of(&doc, "expected").unwrap(),
            &positions_of(&doc).unwrap(),
            &rows_of(&doc, "working").unwrap(),
        )
        .unwrap()
    }

    #[test]
    fn an_empty_day_is_clean_only_when_nothing_is_expected() {
        assert_eq!(reconcile_of("[]", "[]", "[]").get("reconciled"), Some(&Json::Bool(true)));
        let out = reconcile_of(&format!(r#"[[{P200},"-1"]]"#), "[]", "[]");
        assert_eq!(out.get("reconciled"), Some(&Json::Bool(false)));
        assert_eq!(drift(&out), vec!["AAPL  261016P00200000".to_string()]);
    }

    #[test]
    fn a_resting_remainder_covers_and_an_unknown_row_always_drifts() {
        let row = |state: &str| {
            format!(
                r#"[{{"instrument":{P200},"side":"SELL","quantity":"1","filled":"0","order_type":"LIMIT","limit_price":"2.00","state":"{state}"}}]"#
            )
        };
        let positions = format!(r#"[{{"instrument":{P200},"quantity":"-1"}}]"#);
        let expected = format!(r#"[[{P200},"-2"]]"#);
        assert_eq!(reconcile_of(&expected, &positions, &row("ACCEPTED")).get("reconciled"), Some(&Json::Bool(true)));
        assert_eq!(reconcile_of(&expected, &positions, &row("CANCELLED")).get("reconciled"), Some(&Json::Bool(false)));
        let one = format!(r#"[[{P200},"-1"]]"#);
        assert_eq!(reconcile_of(&one, &positions, &row("PENDING_UNKNOWN")).get("reconciled"), Some(&Json::Bool(false)));
    }

    #[test]
    fn a_naive_instant_and_an_empty_venue_refuse_as_the_event_does() {
        let bad = |venue: &str, at: &str| {
            let doc = doc(&format!(
                r#"{{"venue":"{venue}","as_of":"{at}","expected":[],"positions":[],"working":[]}}"#
            ));
            reconcile(
                req_str(&doc, "venue").unwrap(),
                req_str(&doc, "as_of").unwrap(),
                &pairs_of(&doc, "expected").unwrap(),
                &positions_of(&doc).unwrap(),
                &rows_of(&doc, "working").unwrap(),
            )
            .unwrap_err()
        };
        assert_eq!(bad("", "2026-09-24T20:00:00+00:00").msg, "VenueReconcile.venue must be non-empty");
        assert_eq!(
            bad("D-1", "2026-09-24T20:00:00").msg,
            "VenueReconcile.as_of must be timezone-aware UTC datetime (I7)"
        );
    }

    #[test]
    fn a_vertical_is_proven_leg_by_leg_and_a_claimed_row_is_not_matched_twice() {
        let combo = format!(
            r#"{{"kind":"combo","legs":[{{"contract":{P200},"ratio":1,"side":"SELL"}},{{"contract":{P195},"ratio":1,"side":"BUY"}}]}}"#
        );
        let row = |c: &str, side: &str, qty: &str| {
            format!(
                r#"{{"instrument":{c},"side":"{side}","quantity":"{qty}","filled":"0","order_type":"LIMIT","limit_price":"1.50","state":"ACCEPTED"}}"#
            )
        };
        let confirm = |working: &str, claimed: &str| -> (&'static str, Vec<usize>) {
            let doc = doc(&format!(
                r#"{{"ticket":{{"instrument":{combo},"order_type":"LIMIT","side":"SELL","quantity":"1","limit_price":"1.50"}},
                    "before":[],"positions":[],"working":[{working}],"claimed":{claimed}}}"#
            ));
            let mut claimed: Vec<usize> = Vec::new();
            for c in req_arr(&doc, "claimed").unwrap() {
                match c {
                    Json::Int(n) if *n >= 0 => claimed.push(*n as usize),
                    _ => panic!("claimed holds a non-index"),
                }
            }
            let (status, _) = confirm_ticket(
                &ticket_of(req(&doc, "ticket").unwrap()).unwrap(),
                &pairs_of(&doc, "before").unwrap(),
                &positions_of(&doc).unwrap(),
                &rows_of(&doc, "working").unwrap(),
                &mut claimed,
            )
            .unwrap();
            claimed.sort_unstable();
            claimed.dedup();
            (status, claimed)
        };
        let both = format!("{},{}", row(P200, "SELL", "1"), row(P195, "BUY", "1"));
        let out = confirm(&both, "[]");
        assert_eq!(out.0, "ACCEPTED");
        assert_eq!(out.1, vec![0usize, 1]);
        assert_eq!(confirm(&both, "[0]").0, "PENDING");
        assert_eq!(confirm(&row(P200, "SELL", "1"), "[]").0, "PENDING");
    }

    #[test]
    fn the_two_sign_orders_agree_in_value() {
        let t = ticket_of(&doc(&format!(
            r#"{{"instrument":{P200},"order_type":"MARKET","side":"SELL","quantity":"3","limit_price":null}}"#
        )))
        .unwrap();
        let pairs = ticket_contracts(&t, None).unwrap();
        let (_, q) = pairs.iter().next().unwrap();
        assert_eq!(s(q), "-3");
    }
}

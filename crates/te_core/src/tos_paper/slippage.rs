//! Sim-vs-venue fills (was `tos_paper/slippage.py`): one netted-ticket venue fill split
//! down to strategy orders, and the slippage report that pairs the sim fill with the
//! venue fill per strategy order. Nothing is dropped or guessed (I5/I11): an order whose
//! fills cannot be paired honestly is listed with its reason and the rest of the report
//! still stands.
//!
//! The split reuses `ledger::mirror::pro_rata`, the one allocation rule for venue fills
//! (the mirror fold splits each increment with it too). `filled_at` and `as_of` are
//! carried through untouched (no clock here, I7).
//!
//! The `*_raw` typed fns take plain Rust structs and return plain Rust structs; the
//! `allocate_venue_fill`/`slippage_report` door fns are thin JSON wrappers over them.

use std::collections::HashSet;

use super::pytext::repr_str;
use super::wire::{jopt_dec, jstr, linstr, obj, req, req_arr, req_dec, req_str, side_of};
use super::SLIPPAGE;
use crate::ledger::json::Json;
use crate::ledger::mirror::pro_rata;
use crate::ledger::model::{derr, err, Instrument, Side, R};
use crate::ledger::ops::{add, div as ops_div, gt, lt, mul, sub};
use crate::money::{Money, Round};

fn slippage<T>(msg: impl Into<String>) -> R<T> {
    err(SLIPPAGE, msg)
}

/// `a / b`; a zero over a zero is `DivisionUndefined` (a kind the host maps, not a plain
/// `InvalidOperation`: the signal list in the message differs).
fn div(a: &Money, b: &Money) -> R<Money> {
    if a.is_finite() && b.is_finite() && a.is_zero() && b.is_zero() {
        return err("tos_division_undefined", String::new());
    }
    ops_div(a, b)
}

fn zero() -> Money {
    Money::zero()
}

/// `sum(values, ZERO)`: left to right, each add rounded to the context.
fn sum<'a>(values: impl IntoIterator<Item = &'a Money>) -> R<Money> {
    let mut total = zero();
    for v in values {
        total = add(&total, v)?;
    }
    Ok(total)
}

/// `d.quantize(Decimal("0.01"), rounding=ROUND_FLOOR)`.
fn floor_to_cent(d: &Money) -> R<Money> {
    d.quantize_round(-2, Round::Floor).map_err(derr)
}

/// `d.quantize(Decimal("0.0001"))`.
fn to_bps_places(d: &Money) -> R<Money> {
    d.quantize(-4).map_err(derr)
}

/// One venue fill of a netted ticket.
#[derive(Debug)]
pub struct VenueFill {
    pub venue_fill_id: String,
    pub venue_order_id: String,
    pub instrument: Instrument,
    pub side: Side,
    pub quantity: Money,
    pub price: Money,
    /// Carried through untouched (the caller already holds the instant).
    pub filled_at: String,
    pub fee: Money,
}

/// The part of a netted ticket a fill is judged against.
#[derive(Debug)]
pub struct VenueTicket {
    pub venue_order_id: String,
    pub instrument: Instrument,
    pub side: Side,
    pub quantity: Money,
    /// `(strategy_order_id, account_id, quantity)` in allocation order.
    pub allocations: Vec<(String, String, Money)>,
}

/// One strategy-order fill the allocator produces.
#[derive(Debug)]
pub struct AllocFill {
    pub fill_id: String,
    pub order_id: String,
    pub account_id: String,
    pub quantity: Money,
    pub price: Money,
    pub venue_env: &'static str,
    pub side: Side,
    pub fee: Money,
    pub venue_order_id: String,
    pub venue_execution_id: String,
}

/// `allocate_venue_fill`: one venue fill of a netted ticket -> one fill per strategy order
/// (§4.4). The fill is split pro-rata over what each order still lacks; fees split pro-rata
/// to the cent, the remainder to the first-in order.
pub fn allocate_venue_fill_raw(
    fill: &VenueFill,
    ticket: &VenueTicket,
    already_filled: &[(String, Money)],
) -> R<Vec<AllocFill>> {
    if fill.venue_order_id != ticket.venue_order_id {
        return slippage(format!(
            "fill {} is for {}, not {}",
            fill.venue_fill_id, fill.venue_order_id, ticket.venue_order_id
        ));
    }
    if !fill.instrument.same(&ticket.instrument) {
        return slippage(format!("fill {} instrument does not match the ticket (I6)", fill.venue_fill_id));
    }
    if fill.side != ticket.side {
        return slippage(format!(
            "fill {} side {} != ticket side {}",
            fill.venue_fill_id,
            fill.side.value(),
            ticket.side.value()
        ));
    }
    let done = sum(already_filled.iter().map(|(_, q)| q))?;
    let total = add(&done, &fill.quantity)?;
    if gt(&total, &ticket.quantity)? {
        return slippage(format!(
            "fill {} takes the ticket to {} of {}; overfill (I5)",
            fill.venue_fill_id,
            total.canon(),
            ticket.quantity.canon()
        ));
    }
    // `{id: quantity}`: a repeated id keeps its last quantity.
    let known = |id: &str| ticket.allocations.iter().rev().find(|(oid, _, _)| oid == id).map(|(_, _, q)| q);
    for (order_id, quantity) in already_filled {
        let bad = match known(order_id) {
            None => true,
            Some(cap) => lt(quantity, &zero())? || gt(quantity, cap)?,
        };
        if bad {
            return slippage(format!(
                "already_filled gives {order_id} {}, which ticket {} cannot have allocated (I5)",
                quantity.canon(),
                ticket.venue_order_id
            ));
        }
    }
    let mut lacking = Vec::new();
    for (oid, _, qty) in &ticket.allocations {
        let had = already_filled.iter().find(|(id, _)| *id == *oid).map(|(_, q)| q.clone()).unwrap_or_else(zero);
        lacking.push(sub(qty, &had)?);
    }
    let shares = pro_rata(&lacking, &sub(&ticket.quantity, &done)?, &fill.quantity)?;
    let mut pieces: Vec<(usize, Money)> = Vec::new();
    for (index, piece) in shares.into_iter().enumerate() {
        if gt(&piece, &zero())? {
            pieces.push((index, piece));
        }
    }
    let mut fees = Vec::new();
    for (_, piece) in &pieces {
        fees.push(floor_to_cent(&div(&mul(&fill.fee, piece)?, &fill.quantity)?)?);
    }
    if !fees.is_empty() {
        let remainder = sub(&fill.fee, &sum(fees.iter())?)?;
        fees[0] = add(&fees[0], &remainder)?;
    }
    let mut out = Vec::new();
    for ((index, piece), fee) in pieces.iter().zip(&fees) {
        let (oid, account_id, _) = &ticket.allocations[*index];
        out.push(AllocFill {
            fill_id: format!("{}:{}", fill.venue_fill_id, oid),
            order_id: oid.clone(),
            account_id: account_id.clone(),
            quantity: piece.clone(),
            price: fill.price.clone(),
            venue_env: "paper",
            side: fill.side,
            fee: fee.clone(),
            venue_order_id: fill.venue_order_id.clone(),
            venue_execution_id: fill.venue_fill_id.clone(),
        });
    }
    Ok(out)
}

/// `allocate_venue_fill`: one venue fill of a netted ticket -> one fill per strategy order
/// (§4.4). `{"fill", "ticket", "already_filled"}` -> a list of fills.
pub fn allocate_venue_fill(doc: &Json) -> R<Json> {
    let fill = req(doc, "fill")?;
    let ticket = req(doc, "ticket")?;
    let fill = VenueFill {
        venue_fill_id: req_str(fill, "venue_fill_id")?.to_string(),
        venue_order_id: req_str(fill, "venue_order_id")?.to_string(),
        instrument: linstr(req(fill, "instrument")?)?,
        side: side_of(req_str(fill, "side")?)?,
        quantity: req_dec(fill, "quantity")?,
        price: req_dec(fill, "price")?,
        filled_at: req_str(fill, "filled_at")?.to_string(),
        fee: req_dec(fill, "fee")?,
    };
    let ticket = VenueTicket {
        venue_order_id: req_str(ticket, "venue_order_id")?.to_string(),
        instrument: linstr(req(ticket, "instrument")?)?,
        side: side_of(req_str(ticket, "side")?)?,
        quantity: req_dec(ticket, "quantity")?,
        allocations: req_arr(ticket, "allocations")?
            .iter()
            .map(|a| {
                Ok((
                    req_str(a, "strategy_order_id")?.to_string(),
                    req_str(a, "account_id")?.to_string(),
                    req_dec(a, "quantity")?,
                ))
            })
            .collect::<R<Vec<_>>>()?,
    };
    let mut prior: Vec<(String, Money)> = Vec::new();
    match doc.get("already_filled") {
        None | Some(Json::Null) => {}
        Some(Json::Obj(pairs)) => {
            for (k, v) in pairs {
                let Json::Str(text) = v else {
                    return super::wire::wire("already_filled values are Decimal strings");
                };
                prior.push((k.clone(), super::wire::dec_of(text)?));
            }
        }
        Some(_) => return super::wire::wire("already_filled is a mapping or null"),
    }
    let fills = allocate_venue_fill_raw(&fill, &ticket, &prior)?;
    Ok(Json::Arr(
        fills
            .iter()
            .map(|f| {
                obj(vec![
                    ("fill_id", jstr(f.fill_id.clone())),
                    ("order_id", jstr(f.order_id.clone())),
                    ("account_id", jstr(f.account_id.clone())),
                    ("quantity", jstr(f.quantity.canon())),
                    ("price", jstr(f.price.canon())),
                    ("venue_env", jstr(f.venue_env)),
                    ("filled_at", jstr(fill.filled_at.clone())),
                    ("side", jstr(f.side.value())),
                    ("fee", jstr(f.fee.canon())),
                    ("venue_order_id", jstr(f.venue_order_id.clone())),
                    ("venue_execution_id", jstr(f.venue_execution_id.clone())),
                ])
            })
            .collect(),
    ))
}

#[derive(Debug)]
pub struct SimFill {
    pub fill_id: String,
    pub order_id: String,
    pub instrument: Instrument,
    pub side: Side,
    pub quantity: Money,
    pub price: Money,
}

fn fills_of(doc: &Json, key: &str) -> R<Vec<SimFill>> {
    let mut out = Vec::new();
    for f in req_arr(doc, key)? {
        out.push(SimFill {
            fill_id: req_str(f, "fill_id")?.to_string(),
            order_id: req_str(f, "order_id")?.to_string(),
            instrument: linstr(req(f, "instrument")?)?,
            side: side_of(req_str(f, "side")?)?,
            quantity: req_dec(f, "quantity")?,
            price: req_dec(f, "price")?,
        });
    }
    Ok(out)
}

/// `_group`: fills by strategy order (first-seen order), and the orders whose fill ids repeat.
type Grouped<'a> = (Vec<(&'a str, Vec<&'a SimFill>)>, Vec<(&'a str, String)>);

fn group(fills: &[SimFill]) -> Grouped<'_> {
    let mut by_order: Vec<(&str, Vec<&SimFill>)> = Vec::new();
    let mut bad: Vec<(&str, String)> = Vec::new();
    let mut seen: HashSet<&str> = HashSet::new();
    for fill in fills {
        if seen.contains(fill.fill_id.as_str()) {
            let message = format!("duplicate fill id {} (I3)", fill.fill_id);
            match bad.iter_mut().find(|(id, _)| *id == fill.order_id) {
                Some(slot) => slot.1 = message,
                None => bad.push((&fill.order_id, message)),
            }
        }
        seen.insert(&fill.fill_id);
        match by_order.iter_mut().find(|(id, _)| *id == fill.order_id) {
            Some(slot) => slot.1.push(fill),
            None => by_order.push((&fill.order_id, vec![fill])),
        }
    }
    (by_order, bad)
}

/// `_vwap`: total quantity and the quantity-weighted price.
fn vwap(fills: &[&SimFill]) -> R<(Money, Money)> {
    let quantity = sum(fills.iter().map(|f| &f.quantity))?;
    let mut notional = Vec::new();
    for f in fills {
        notional.push(mul(&f.price, &f.quantity)?);
    }
    let price = div(&sum(notional.iter())?, &quantity)?;
    Ok((quantity, price))
}

struct Pair {
    order_id: String,
    instrument: String,
    side: Side,
    quantity: Money,
    sim_price: Money,
    venue_price: Money,
    points: Money,
    bps: Option<Money>,
}

fn distinct_instruments(sims: &[&SimFill], venues: &[&SimFill]) -> usize {
    let mut seen: Vec<&Instrument> = Vec::new();
    for f in sims.iter().chain(venues) {
        let mut known = false;
        for s in &seen {
            if s.same(&f.instrument) {
                known = true;
                break;
            }
        }
        if !known {
            seen.push(&f.instrument);
        }
    }
    seen.len()
}

/// One strategy order's sim fill and the venue fill that mirrors it.
#[derive(Debug)]
pub struct PairOut {
    pub order_id: String,
    pub instrument: String,
    pub side: Side,
    pub quantity: Money,
    pub sim_price: Money,
    pub venue_price: Money,
    pub slippage_points: Money,
    pub slippage_bps: Option<Money>,
}

/// One venue's sim-vs-venue report over a session or batch.
#[derive(Debug)]
pub struct Report {
    pub venue: String,
    pub as_of: String,
    pub pairs: Vec<PairOut>,
    pub unmatched_sim: Vec<String>,
    pub unmatched_venue: Vec<String>,
    pub refused: Vec<(String, String)>,
    pub mean_slippage_bps: Option<Money>,
}

/// `slippage_report`: pair sim and venue fills per strategy order; refuse per order,
/// never per report.
pub fn slippage_report_raw(
    venue: &str,
    as_of: &str,
    sim_fills: &[SimFill],
    venue_fills: &[SimFill],
) -> R<Report> {
    let (sim_by, sim_bad) = group(sim_fills);
    let (venue_by, venue_bad) = group(venue_fills);
    let bad_of = |bad: &Vec<(&str, String)>, id: &str| bad.iter().find(|(o, _)| *o == id).map(|(_, m)| m.clone());
    let mut pairs: Vec<Pair> = Vec::new();
    let mut unmatched_sim: Vec<String> = Vec::new();
    let mut refused: Vec<(String, String)> = Vec::new();
    for (order_id, sims) in &sim_by {
        if let Some(message) = bad_of(&sim_bad, order_id) {
            refused.push((order_id.to_string(), format!("sim: {message}")));
            continue;
        }
        let venues = venue_by.iter().find(|(id, _)| id == order_id).map(|(_, v)| v);
        let Some(venues) = venues.filter(|v| !v.is_empty()) else {
            unmatched_sim.push(order_id.to_string());
            continue;
        };
        if let Some(message) = bad_of(&venue_bad, order_id) {
            refused.push((order_id.to_string(), format!("venue: {message}")));
            continue;
        }
        let mut sides: Vec<&str> = sims.iter().chain(venues).map(|f| f.side.value()).collect();
        sides.sort();
        sides.dedup();
        if sides.len() > 1 {
            let listed = sides.iter().map(|s| repr_str(s)).collect::<Vec<_>>().join(", ");
            refused.push((order_id.to_string(), format!("side mismatch [{listed}]; not paired")));
            continue;
        }
        if distinct_instruments(sims, venues) > 1 {
            refused.push((
                order_id.to_string(),
                "instrument mismatch between sim and venue fills; not paired (I6)".to_string(),
            ));
            continue;
        }
        let (sim_qty, sim_price) = vwap(sims)?;
        let (venue_qty, venue_price) = vwap(venues)?;
        if !venue_qty.eq_num(&sim_qty).map_err(derr)? {
            refused.push((
                order_id.to_string(),
                format!(
                    "sim filled {} but the venue filled {}; mirror drifted (I5)",
                    sim_qty.canon(),
                    venue_qty.canon()
                ),
            ));
            continue;
        }
        let side = sims[0].side;
        // Adverse-only convention: positive always costs money.
        let points = if side == Side::Buy { sub(&venue_price, &sim_price)? } else { sub(&sim_price, &venue_price)? };
        let bps = if gt(&sim_price, &zero())? {
            Some(to_bps_places(&mul(&div(&points, &sim_price)?, &Money::int(10000))?)?)
        } else {
            None
        };
        let symbol = sims[0].instrument.symbol()?;
        if le_zero(&sim_qty)? {
            return slippage("pair quantity must be strictly positive");
        }
        pairs.push(Pair {
            order_id: order_id.to_string(),
            instrument: symbol,
            side,
            quantity: sim_qty,
            sim_price,
            venue_price,
            points,
            bps,
        });
    }
    let mut unmatched_venue: Vec<String> =
        venue_by.iter().filter(|(id, _)| !sim_by.iter().any(|(s, _)| s == id)).map(|(id, _)| id.to_string()).collect();
    let priced: Vec<&Pair> = pairs.iter().filter(|p| p.bps.is_some()).collect();
    let mut mean: Option<Money> = None;
    if !priced.is_empty() {
        let weight = sum(priced.iter().map(|p| &p.quantity))?;
        let mut weighted = Vec::new();
        for p in &priced {
            weighted.push(mul(p.bps.as_ref().expect("priced"), &p.quantity)?);
        }
        mean = Some(to_bps_places(&div(&sum(weighted.iter())?, &weight)?)?);
    }
    if venue.is_empty() {
        return slippage("venue must be non-empty");
    }
    unmatched_sim.sort();
    unmatched_venue.sort();
    refused.sort();
    Ok(Report {
        venue: venue.to_string(),
        as_of: as_of.to_string(),
        pairs: pairs
            .into_iter()
            .map(|p| PairOut {
                order_id: p.order_id,
                instrument: p.instrument,
                side: p.side,
                quantity: p.quantity,
                sim_price: p.sim_price,
                venue_price: p.venue_price,
                slippage_points: p.points,
                slippage_bps: p.bps,
            })
            .collect(),
        unmatched_sim,
        unmatched_venue,
        refused,
        mean_slippage_bps: mean,
    })
}

/// `slippage_report`: pair sim and venue fills per strategy order; refuse per order,
/// never per report. `{"venue", "as_of", "sim_fills", "venue_fills"}` -> the report.
pub fn slippage_report(doc: &Json) -> R<Json> {
    let venue = req_str(doc, "venue")?;
    let as_of = req_str(doc, "as_of")?;
    let sim_fills = fills_of(doc, "sim_fills")?;
    let venue_fills = fills_of(doc, "venue_fills")?;
    let r = slippage_report_raw(venue, as_of, &sim_fills, &venue_fills)?;
    Ok(obj(vec![
        ("venue", jstr(r.venue)),
        ("as_of", jstr(r.as_of)),
        (
            "pairs",
            Json::Arr(
                r.pairs
                    .iter()
                    .map(|p| {
                        obj(vec![
                            ("order_id", jstr(p.order_id.clone())),
                            ("instrument", jstr(p.instrument.clone())),
                            ("side", jstr(p.side.value())),
                            ("quantity", jstr(p.quantity.canon())),
                            ("sim_price", jstr(p.sim_price.canon())),
                            ("venue_price", jstr(p.venue_price.canon())),
                            ("slippage_points", jstr(p.slippage_points.canon())),
                            ("slippage_bps", jopt_dec(&p.slippage_bps)),
                        ])
                    })
                    .collect(),
            ),
        ),
        ("unmatched_sim", Json::Arr(r.unmatched_sim.into_iter().map(Json::Str).collect())),
        ("unmatched_venue", Json::Arr(r.unmatched_venue.into_iter().map(Json::Str).collect())),
        (
            "refused",
            Json::Arr(r.refused.into_iter().map(|(a, b)| Json::Arr(vec![Json::Str(a), Json::Str(b)])).collect()),
        ),
        ("mean_slippage_bps", jopt_dec(&r.mean_slippage_bps)),
    ]))
}

fn le_zero(d: &Money) -> R<bool> {
    d.le(&zero()).map_err(derr)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::ledger::json::{dumps, parse};

    const INST: &str = r#"{"kind": "option", "underlying": "AAPL", "expiry": "2026-10-16", "strike": "200", "right": "P", "multiplier": 100}"#;

    fn alloc(already: &str, fill_qty: &str, fee: &str) -> R<Json> {
        allocate_venue_fill(
            &parse(&format!(
                r#"{{"fill": {{"venue_fill_id": "F1", "venue_order_id": "V1", "instrument": {INST}, "side": "SELL",
                    "quantity": "{fill_qty}", "price": "2.10", "filled_at": "2026-09-24T20:00:00+00:00", "fee": "{fee}"}},
                  "ticket": {{"venue_order_id": "V1", "instrument": {INST}, "side": "SELL", "quantity": "3",
                    "allocations": [{{"strategy_order_id": "A", "account_id": "x", "quantity": "1"}},
                                    {{"strategy_order_id": "B", "account_id": "x", "quantity": "2"}}]}},
                  "already_filled": {already}}}"#
            ))
            .unwrap(),
        )
    }

    fn column(j: &Json, key: &str) -> Vec<String> {
        let Json::Arr(items) = j else { panic!() };
        items.iter().map(|i| match i.get(key) { Some(Json::Str(s)) => s.clone(), _ => panic!() }).collect()
    }

    #[test]
    fn a_fill_splits_pro_rata_and_the_fee_remainder_goes_to_the_first_order() {
        let out = alloc("null", "3", "1.00").unwrap();
        assert_eq!(column(&out, "quantity"), ["1", "2"]);
        // 1.00 * 1/3 = 0.33 floored, 1.00 * 2/3 = 0.66 floored; the 0.01 left over goes to the first
        assert_eq!(column(&out, "fee"), ["0.34", "0.66"]);
        assert_eq!(column(&out, "fill_id"), ["F1:A", "F1:B"]);
    }

    #[test]
    fn a_partial_fill_splits_over_what_each_order_still_lacks() {
        let out = alloc(r#"{"A": "1"}"#, "2", "0").unwrap();
        assert_eq!(column(&out, "order_id"), ["B"]);
        assert_eq!(column(&out, "fee"), ["0"]);
        let refused = alloc(r#"{"A": "1"}"#, "3", "0").unwrap_err();
        assert!(refused.msg.contains("takes the ticket to 4 of 3; overfill (I5)"), "{}", refused.msg);
        let unknown = alloc(r#"{"Z": "1"}"#, "1", "0").unwrap_err();
        assert!(unknown.msg.starts_with("already_filled gives Z 1, which ticket V1 cannot"), "{}", unknown.msg);
    }

    #[test]
    fn the_report_pairs_per_order_and_signs_slippage_as_adverse() {
        let report = |sim: &str, venue: &str| {
            let fill = |id: &str, order: &str, side: &str, q: &str, p: &str| {
                format!(r#"{{"fill_id": "{id}", "order_id": "{order}", "instrument": {INST}, "side": "{side}", "quantity": "{q}", "price": "{p}"}}"#)
            };
            let _ = (sim, venue);
            slippage_report(
                &parse(&format!(
                    r#"{{"venue": "tos", "as_of": "T", "sim_fills": [{}, {}, {}], "venue_fills": [{}, {}]}}"#,
                    fill("s1", "A", "SELL", "2", "2.00"),
                    fill("s2", "B", "BUY", "1", "1.00"),
                    fill("s3", "C", "BUY", "1", "1.00"),
                    fill("v1", "A", "SELL", "2", "1.90"),
                    fill("v2", "B", "BUY", "2", "1.10"),
                ))
                .unwrap(),
            )
            .unwrap()
        };
        let r = report("", "");
        let text = dumps(&r);
        assert!(text.contains(r#""slippage_points":"0.1""#) && text.contains(r#""slippage_bps":"500""#), "{text}");
        assert!(text.contains(r#""unmatched_sim":["C"]"#));
        assert!(text.contains("sim filled 1 but the venue filled 2; mirror drifted (I5)"));
    }

    // -- the typed fns --------------------------------------------------------------------

    #[test]
    fn the_typed_allocator_agrees_with_the_door() {
        let fill = VenueFill {
            venue_fill_id: "F1".into(),
            venue_order_id: "V1".into(),
            instrument: linstr(&parse(INST).unwrap()).unwrap(),
            side: Side::Sell,
            quantity: Money::parse("3").unwrap(),
            price: Money::parse("2.10").unwrap(),
            filled_at: "2026-09-24T20:00:00+00:00".into(),
            fee: Money::parse("1.00").unwrap(),
        };
        let ticket = VenueTicket {
            venue_order_id: "V1".into(),
            instrument: linstr(&parse(INST).unwrap()).unwrap(),
            side: Side::Sell,
            quantity: Money::parse("3").unwrap(),
            allocations: vec![
                ("A".into(), "x".into(), Money::parse("1").unwrap()),
                ("B".into(), "x".into(), Money::parse("2").unwrap()),
            ],
        };
        let out = allocate_venue_fill_raw(&fill, &ticket, &[]).unwrap();
        assert_eq!(out.iter().map(|f| f.quantity.canon()).collect::<Vec<_>>(), ["1", "2"]);
        assert_eq!(out.iter().map(|f| f.fee.canon()).collect::<Vec<_>>(), ["0.34", "0.66"]);
        let over = VenueFill { quantity: Money::parse("4").unwrap(), ..fill };
        assert!(allocate_venue_fill_raw(&over, &ticket, &[]).unwrap_err().msg.contains("overfill"));
    }

    #[test]
    fn the_typed_report_agrees_with_the_door() {
        let fill = |id: &str, order: &str, side: Side, q: &str, p: &str| SimFill {
            fill_id: id.into(),
            order_id: order.into(),
            instrument: linstr(&parse(INST).unwrap()).unwrap(),
            side,
            quantity: Money::parse(q).unwrap(),
            price: Money::parse(p).unwrap(),
        };
        let r = slippage_report_raw(
            "tos",
            "T",
            &[fill("s1", "A", Side::Sell, "2", "2.00"), fill("s2", "B", Side::Buy, "1", "1.00")],
            &[fill("v1", "A", Side::Sell, "2", "1.90"), fill("v2", "B", Side::Buy, "2", "1.10")],
        )
        .unwrap();
        assert_eq!(r.pairs.len(), 1);
        assert_eq!(r.pairs[0].slippage_points.canon(), "0.1");
        assert!(r.refused.iter().any(|(_, m)| m.contains("mirror drifted")), "{:?}", r.refused);
        assert_eq!(slippage_report_raw("", "T", &[], &[]).unwrap_err().msg, "venue must be non-empty");
    }
}

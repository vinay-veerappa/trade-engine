//! The cover rule (was `tos_paper/cover.py`): an order waits iff filling it in full would
//! leave more short calls uncovered on its underlying than there are now. Cover is what the
//! mirror book proves the venue holds, pessimistically; a short call is covered by a lot of
//! `multiplier` shares or by a long call (a debit diagonal), assigned by a maximum matching.
//!
//! The mirror crosses as the ledger's canonical tree (`codec.canon(MirrorState)`), read by
//! `bridge::uncanon_mirror`; an instrument as the wire document of [`super::wire`].

use super::reconcile::{pairs_json, pairs_of};
use super::OVERFLOW_ERROR;
use super::wire::{jstr, linstr, linstr_json, obj, req, req_arr, req_dec, req_str, side_of, wire};
use crate::ledger::bridge::uncanon_mirror;
use crate::ledger::json::Json;
use crate::ledger::mirror::{ticket_contracts, MirrorState};
use crate::ledger::model::{derr, err, Instrument, OptionContract, Side, R};
use crate::ledger::ops::{abs, add, ge, gt, lt, ne, s, sub, zero, OMap};
use crate::money::Money;
use crate::options::Right;

/// The mirror book's holdings, one signed total per contract.
pub type Held = OMap<Instrument, Money>;

fn held_get(held: &Held, instrument: &Instrument) -> Money {
    held.get(&instrument.hk()).cloned().unwrap_or_else(zero)
}

/// `covers`: whether one long call can cover one short call.
pub fn covers(long: &OptionContract, short: &OptionContract) -> R<bool> {
    Ok(long.underlying == short.underlying
        && long.right == Right::Call
        && short.right == Right::Call
        && long.multiplier == short.multiplier
        && long.expiry >= short.expiry
        && long.strike.le(&short.strike).map_err(derr)?)
}

/// `_bare`: the short call units no long call unit covers, after a maximum matching (Kuhn
/// augmenting paths), as indexes into `shorts`.
pub fn bare(longs: &[OptionContract], shorts: &[OptionContract]) -> R<Vec<usize>> {
    fn augment(
        short: usize,
        seen: &mut Vec<bool>,
        owner: &mut Vec<Option<usize>>,
        longs: &[OptionContract],
        shorts: &[OptionContract],
    ) -> R<bool> {
        for index in 0..longs.len() {
            if !seen[index] && covers(&longs[index], &shorts[short])? {
                seen[index] = true;
                let free = match owner[index] {
                    None => true,
                    Some(other) => augment(other, seen, owner, longs, shorts)?,
                };
                if free {
                    owner[index] = Some(short);
                    return Ok(true);
                }
            }
        }
        Ok(false)
    }
    let mut owner: Vec<Option<usize>> = vec![None; longs.len()];
    for short in 0..shorts.len() {
        let mut seen = vec![false; longs.len()];
        augment(short, &mut seen, &mut owner, longs, shorts)?;
    }
    let covered: Vec<usize> = owner.into_iter().flatten().collect();
    Ok((0..shorts.len()).filter(|i| !covered.contains(i)).collect())
}

/// The most call units one position may expand to (a memory guard; Python would simply grind).
const MAX_UNITS: i128 = 1_000_000;

/// Push `units` copies of `c` under its underlying, keeping the underlyings in first-seen order.
fn extend(side: &mut Vec<(String, Vec<OptionContract>)>, c: &OptionContract, units: i128) {
    let at = match side.iter().position(|(u, _)| *u == c.underlying) {
        Some(at) => at,
        None => {
            side.push((c.underlying.clone(), Vec::new()));
            side.len() - 1
        }
    };
    for _ in 0..units {
        side[at].1.push(c.clone());
    }
}

/// `uncovered`: per underlying, the short call contracts nothing in `held` covers (only the
/// counts above zero), in the order the underlyings first appear among the short calls.
pub fn uncovered(held: &Held) -> R<Vec<(String, Money)>> {
    let mut shorts: Vec<(String, Vec<OptionContract>)> = Vec::new();
    let mut longs: Vec<(String, Vec<OptionContract>)> = Vec::new();
    for (instrument, quantity) in held.iter() {
        let Instrument::Option(c) = instrument else { continue };
        if c.right != Right::Call || !ne(quantity, &zero())? {
            continue;
        }
        let side = if lt(quantity, &zero())? { &mut shorts } else { &mut longs };
        if !quantity.is_finite() {
            return err(OVERFLOW_ERROR, "cannot convert Infinity to integer");
        }
        let units = abs(quantity)?.trunc_i128().map_err(derr)?;
        if units > MAX_UNITS {
            return wire("a position too large to expand into call units");
        }
        extend(side, c, units);
    }
    let mut found = Vec::new();
    for (underlying, calls) in &shorts {
        let mut shares = held_get(held, &Instrument::Equity(underlying.clone()));
        let none = Vec::new();
        let longs_here = longs.iter().find(|(u, _)| u == underlying).map_or(&none, |(_, l)| l);
        let mut left: Vec<&OptionContract> = bare(longs_here, calls)?.into_iter().map(|i| &calls[i]).collect();
        // Shares go to the larger multiplier first (a stable sort, as `sorted(..., reverse=True)`).
        left.sort_by(|a, b| b.multiplier.cmp(&a.multiplier));
        let mut count: i128 = 0;
        for call in left {
            let lot = Money::from_i128(call.multiplier).map_err(derr)?;
            if ge(&shares, &lot)? {
                shares = sub(&shares, &lot)?;
            } else {
                count += 1;
            }
        }
        if count != 0 {
            found.push((underlying.clone(), Money::from_i128(count).map_err(derr)?));
        }
    }
    Ok(found)
}

/// `holdings`: the book summed over accounts, less every open ticket's unfilled sell
/// remainder (a resting buy is not credited until it fills); the nonzero entries only.
pub fn holdings(mirror: &MirrorState) -> R<Held> {
    let mut held = Held::new();
    for ((_account, instrument), quantity) in mirror.book.iter() {
        let total = add(&held_get(&held, instrument), quantity)?;
        held.insert(instrument.hk(), instrument.clone(), total);
    }
    for ticket in mirror.open_tickets()? {
        for (instrument, quantity) in ticket_contracts(&ticket.queued, &ticket.remaining()?)?.iter() {
            if lt(quantity, &zero())? {
                let total = add(&held_get(&held, instrument), quantity)?;
                held.insert(instrument.hk(), instrument.clone(), total);
            }
        }
    }
    let mut out = Held::new();
    for (instrument, quantity) in held.iter() {
        if ne(quantity, &zero())? {
            out.insert(instrument.hk(), instrument.clone(), quantity.clone());
        }
    }
    Ok(out)
}

/// What a single-leg SELL of shares or of a call gives up.
pub type Sold = Option<(Instrument, Money)>;

/// `_sold` over an order document `{instrument, side, quantity}`.
pub fn sold_of(order: &Json) -> R<Sold> {
    let side = side_of(req_str(order, "side")?)?;
    let instrument = req(order, "instrument")?;
    let single = match req_str(instrument, "kind")? {
        "equity" => true,
        "option" => req_str(instrument, "right")? == "C",
        _ => false,
    };
    if side != Side::Sell || !single {
        return Ok(None);
    }
    Ok(Some((linstr(instrument)?, req_dec(order, "quantity")?)))
}

fn lookup(found: &[(String, Money)], underlying: &str) -> Money {
    found.iter().find(|(u, _)| u == underlying).map_or_else(zero, |(_, c)| c.clone())
}

/// `cover_reason`: `None` when the order may go now, else why it must wait. `accepted` are the
/// sells of the same batch already let through; they count as resting.
pub fn cover_reason(mirror: &MirrorState, sold: &Sold, accepted: &[Sold]) -> R<Option<String>> {
    let Some((instrument, quantity)) = sold else { return Ok(None) };
    let mut held = holdings(mirror)?;
    for gone in accepted.iter().flatten() {
        let total = sub(&held_get(&held, &gone.0), &gone.1)?;
        held.insert(gone.0.hk(), gone.0.clone(), total);
    }
    let underlying = match instrument {
        Instrument::Equity(symbol) => symbol.clone(),
        Instrument::Option(c) => c.underlying.clone(),
        Instrument::Combo(_) => return wire("a combo is never sold here"),
        Instrument::Future(_) => return wire("a future is never sold here"),
    };
    let before = lookup(&uncovered(&held)?, &underlying);
    let mut after_held = held.clone();
    let reduced = sub(&held_get(&held, instrument), quantity)?;
    after_held.insert(instrument.hk(), instrument.clone(), reduced);
    let after = lookup(&uncovered(&after_held)?, &underlying);
    if !gt(&after, &before)? {
        return Ok(None);
    }
    let shares_held = held_get(&held, &Instrument::Equity(underlying.clone()));
    // `max(held, 0)`: the first operand unless the second is strictly larger.
    let shares = if gt(&zero(), &shares_held)? { zero() } else { shares_held };
    let mut longs = zero();
    for (held_instrument, held_quantity) in held.iter() {
        if let Instrument::Option(c) = held_instrument {
            if c.right == Right::Call && c.underlying == underlying && gt(held_quantity, &zero())? {
                longs = add(&longs, held_quantity)?;
            }
        }
    }
    Ok(Some(format!(
        "selling {} {} would leave {} short {} call(s) uncovered ({} now); the mirror book proves {} share(s) and {} long call(s) held (proven venue fills only; shares from an assignment are not booked)",
        s(quantity),
        instrument.symbol()?,
        s(&after),
        underlying,
        s(&before),
        s(&shares),
        s(&longs),
    )))
}

// -- the door ------------------------------------------------------------------------------------

fn option_of(j: &Json) -> R<OptionContract> {
    match linstr(j)? {
        Instrument::Option(c) => Ok(c),
        _ => wire("expected an option contract"),
    }
}

fn options_of(doc: &Json, key: &str) -> R<Vec<OptionContract>> {
    req_arr(doc, key)?.iter().map(option_of).collect()
}

fn mirror_of(doc: &Json) -> R<MirrorState> {
    uncanon_mirror(req(doc, "mirror")?)
}

pub fn covers_op(doc: &Json) -> R<Json> {
    let result = covers(&option_of(req(doc, "long")?)?, &option_of(req(doc, "short")?)?)?;
    Ok(obj(vec![("result", Json::Bool(result))]))
}

pub fn bare_op(doc: &Json) -> R<Json> {
    let shorts = options_of(doc, "shorts")?;
    let left = bare(&options_of(doc, "longs")?, &shorts)?;
    let out = left.into_iter().map(|i| linstr_json(&Instrument::Option(shorts[i].clone()))).collect();
    Ok(obj(vec![("left", Json::Arr(out))]))
}

pub fn uncovered_op(doc: &Json) -> R<Json> {
    let found = uncovered(&pairs_of(doc, "held")?)?;
    Ok(Json::Arr(found.iter().map(|(u, c)| Json::Arr(vec![jstr(u.clone()), jstr(s(c))])).collect()))
}

pub fn holdings_op(doc: &Json) -> R<Json> {
    Ok(pairs_json(&holdings(&mirror_of(doc)?)?))
}

pub fn sold_op(doc: &Json) -> R<Json> {
    Ok(match sold_of(req(doc, "order")?)? {
        None => Json::Null,
        Some((i, q)) => Json::Arr(vec![linstr_json(&i), jstr(s(&q))]),
    })
}

pub fn cover_reason_op(doc: &Json) -> R<Json> {
    let mirror = mirror_of(doc)?;
    let sold = sold_of(req(doc, "order")?)?;
    let accepted: Vec<Sold> = req_arr(doc, "accepted")?.iter().map(sold_of).collect::<R<_>>()?;
    Ok(obj(vec![("reason", cover_reason(&mirror, &sold, &accepted)?.map_or(Json::Null, jstr))]))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::ledger::json::parse;

    fn call(strike: &str, expiry: &str, multiplier: i64) -> String {
        format!(
            r#"{{"kind":"option","underlying":"AAPL","expiry":"{expiry}","strike":"{strike}","right":"C","multiplier":{multiplier}}}"#
        )
    }

    fn found(held: &str) -> Result<Json, crate::ledger::model::LErr> {
        uncovered_op(&parse(&format!(r#"{{"held":{held}}}"#)).unwrap())
    }

    #[test]
    fn a_maximum_matching_hands_the_wide_long_on_to_the_later_short() {
        let (wide, narrow) = (call("200", "2026-12-18", 100), call("205", "2026-10-16", 100));
        let (s1, s2) = (call("210", "2026-10-16", 100), call("210", "2026-11-20", 100));
        let doc = |longs: String, shorts: String| parse(&format!(r#"{{"longs":[{longs}],"shorts":[{shorts}]}}"#)).unwrap();
        let out = bare_op(&doc(format!("{wide},{narrow}"), format!("{s1},{s2}"))).unwrap();
        assert_eq!(out.get("left"), Some(&Json::Arr(vec![])));
        // one long, two shorts: the first short is covered, the second is left
        let out = bare_op(&doc(wide.clone(), format!("{s1},{s2}"))).unwrap();
        let Some(Json::Arr(left)) = out.get("left") else { panic!("no left") };
        assert_eq!(left.len(), 1);
    }

    #[test]
    fn shares_go_to_the_larger_multiplier_first_and_a_credit_diagonal_covers_nothing() {
        let (c100, c50) = (call("210", "2026-10-16", 100), call("215", "2026-10-16", 50));
        let shares = r#"{"kind":"equity","symbol":"AAPL"}"#;
        let out = found(&format!(r#"[[{shares},"100"],[{c100},"-1"],[{c50},"-2"]]"#)).unwrap();
        assert_eq!(out, parse(r#"[["AAPL","2"]]"#).unwrap());
        let credit = call("215", "2026-12-18", 100);
        let out = found(&format!(r#"[[{credit},"1"],[{c100},"-1"]]"#)).unwrap();
        assert_eq!(out, parse(r#"[["AAPL","1"]]"#).unwrap());
        let debit = call("200", "2026-12-18", 100);
        assert_eq!(found(&format!(r#"[[{debit},"1"],[{c100},"-1"]]"#)).unwrap(), parse("[]").unwrap());
    }

    #[test]
    fn an_infinite_or_nan_quantity_is_refused_at_the_wire() {
        // P7: Money has no NaN or Infinity (I5); PyDec turned these into overflow / invalid_operation
        let c = call("210", "2026-10-16", 100);
        assert_eq!(found(&format!(r#"[[{c},"-Infinity"]]"#)).unwrap_err().kind, "tos_wire");
        assert_eq!(found(&format!(r#"[[{c},"NaN"]]"#)).unwrap_err().kind, "tos_wire");
    }
}

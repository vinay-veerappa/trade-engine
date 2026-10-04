//! The mirror's netting decisions (was `tos_paper/netting.py`). P5-T1 carries
//! `vertical_reason`, which the ticket mapping needs; the netting itself arrives with T6.

use super::wire::{
    dec_of, instrument, jopt_dec, jstr, linstr, linstr_json, obj, opt_dec, order_type_of, req, req_arr, req_dec,
    req_str, side_of, tif_of, wire, ComboLeg, Instrument,
};
use super::{NETTING, VALUE};
use crate::ledger::codec::py_repr;
use crate::ledger::json::Json;
use crate::ledger::model::{
    derr, parse_datetime, Instrument as LInstrument, LErr, OrderType, Side, Tif, R,
};
use crate::ledger::ops::{add, gt, le, mul_i, ne, neg, s, zero, OMap};
use crate::ledger::pydec::PyDec;
use crate::oms::options::sha256_hex;
use num_bigint::BigUint;
use num_traits::Zero;

/// `vertical_reason`: why `legs` is not a mirrorable 2-leg 1:1 vertical, or `None` when it is.
pub fn vertical_reason(legs: &[ComboLeg]) -> R<Option<String>> {
    let as_options: Vec<_> = legs
        .iter()
        .filter_map(|leg| match &leg.contract {
            Instrument::Option(c) => Some(c),
            _ => None,
        })
        .collect();
    if legs.len() != 2 || as_options.len() != 2 {
        let symbol = Instrument::Combo(legs.to_vec()).symbol()?;
        return Ok(Some(format!("{symbol} is not a 2-leg option combo; only verticals are mirrored")));
    }
    let (first, second) = (as_options[0], as_options[1]);
    if first.underlying != second.underlying {
        return Ok(Some("legs on two underlyings are not a vertical".into()));
    }
    if first.expiry != second.expiry {
        return Ok(Some("legs on two expiries (a calendar or diagonal) are not a vertical".into()));
    }
    if first.right != second.right {
        return Ok(Some("a call leg and a put leg are not a vertical".into()));
    }
    if first.multiplier != second.multiplier {
        return Ok(Some("legs with two multipliers are not a vertical (I6)".into()));
    }
    if first.strike.eq_num(&second.strike).map_err(derr)? {
        return Ok(Some("two legs on one strike are not a vertical".into()));
    }
    if legs[0].side == legs[1].side {
        return Ok(Some("both legs on one side are not a vertical".into()));
    }
    if legs[0].ratio != legs[1].ratio {
        return Ok(Some(format!("a {}:{} ratio spread is not a 1:1 vertical", legs[0].ratio, legs[1].ratio)));
    }
    Ok(None)
}

/// The door's `vertical_reason`: `{"instrument": <combo>}` -> `{"reason": str | null}`.
pub fn vertical_reason_op(doc: &Json) -> R<Json> {
    let Instrument::Combo(legs) = instrument(req(doc, "instrument")?)? else {
        return super::wire::wire("vertical_reason takes a combo");
    };
    Ok(obj(vec![("reason", vertical_reason(&legs)?.map_or(Json::Null, jstr))]))
}


// -- netting (P5-T6) ---------------------------------------------------------------------------------
//
// An order crosses as `{order_id, account_id, instrument, order_type, side, quantity, tif,
// limit_price}`; the mirror book (holdings) as `[[account, instrument, "qty"], ...]`. A batch
// answers `{venue_orders: [...], refused: [[order_id, reason], ...]}`.


/// `Decimal.normalize()` under the default context: round to 28 digits, strip trailing zeros.
pub fn normalize(d: &PyDec) -> R<PyDec> {
    let fixed = d.fix().map_err(derr)?;
    if !fixed.is_finite() {
        return Ok(fixed);
    }
    if fixed.is_zero() {
        return Ok(PyDec::from_parts(fixed.is_negative(), BigUint::zero(), 0));
    }
    let mut digits = fixed.coefficient().to_str_radix(10);
    let mut exp = fixed.exponent();
    while digits.ends_with('0') && exp < crate::ledger::pydec::EMAX {
        digits.pop();
        exp += 1;
    }
    let coef = BigUint::parse_bytes(digits.as_bytes(), 10).expect("digits");
    Ok(PyDec::from_parts(fixed.is_negative(), coef, exp))
}

/// `ticket_key`: the stable idempotency key of one ticket's full contents (I3).
#[allow(clippy::too_many_arguments)]
pub fn ticket_key(
    venue_account: &str,
    instrument: &LInstrument,
    side: Side,
    quantity: &PyDec,
    order_type: OrderType,
    limit_price: &Option<PyDec>,
    tif: Tif,
    order_ids: &[String],
) -> R<String> {
    let mut parts: Vec<String> = vec![
        venue_account.to_string(),
        instrument.symbol()?,
        side.value().to_string(),
        s(&normalize(quantity)?),
        order_type.value().to_string(),
        match limit_price {
            None => String::new(),
            Some(p) => s(&normalize(p)?),
        },
        tif.value().to_string(),
    ];
    let mut ids = order_ids.to_vec();
    ids.sort();
    parts.extend(ids);
    Ok(sha256_hex(parts.join("\u{1f}").as_bytes()))
}

/// One strategy order of a batch.
pub struct NOrder {
    pub order_id: String,
    pub account_id: String,
    /// The wire form (an `other` instrument has no model form).
    pub wire: Instrument,
    pub model: Option<LInstrument>,
    pub order_type: OrderType,
    pub side: Side,
    pub quantity: PyDec,
    pub tif: Tif,
    pub limit_price: Option<PyDec>,
}

pub fn norder_of(j: &Json) -> R<NOrder> {
    let wire_instrument = instrument(req(j, "instrument")?)?;
    let model = match wire_instrument {
        Instrument::Other { .. } => None,
        _ => Some(linstr(req(j, "instrument")?)?),
    };
    Ok(NOrder {
        order_id: req_str(j, "order_id")?.to_string(),
        account_id: req_str(j, "account_id")?.to_string(),
        wire: wire_instrument,
        model,
        order_type: order_type_of(req_str(j, "order_type")?)?,
        side: side_of(req_str(j, "side")?)?,
        quantity: req_dec(j, "quantity")?,
        tif: tif_of(req_str(j, "tif")?)?,
        limit_price: opt_dec(j, "limit_price")?,
    })
}

fn nerr<T>(kind: &'static str, msg: impl Into<String>) -> R<T> {
    Err(LErr { kind, msg: msg.into() })
}

/// `'x'`-style list repr of strings.
fn py_list(items: &[String]) -> String {
    format!("[{}]", items.iter().map(|i| py_repr(i)).collect::<Vec<_>>().join(", "))
}

/// `_screen`: why this single order cannot go to the venue at all, or `None`.
pub fn screen(order: &NOrder, mirrored: &[String]) -> R<Option<String>> {
    if !mirrored.contains(&order.account_id) {
        let mut sorted = mirrored.to_vec();
        sorted.sort();
        return Ok(Some(format!(
            "account {} is not mirrored on this venue (mirrors {}); refused at the venue only (§4.7)",
            order.account_id,
            py_list(&sorted)
        )));
    }
    match &order.wire {
        Instrument::Combo(legs) => {
            if let Some(reason) = vertical_reason(legs)? {
                return Ok(Some(format!("UnsupportedCapability: multi-leg combo: {reason}")));
            }
            if order.order_type != OrderType::Limit {
                return Ok(Some("UnsupportedCapability: a vertical is mirrored with one net LIMIT price only".into()));
            }
        }
        Instrument::Other { repr } => {
            return Ok(Some(format!(
                "UnsupportedCapability: instrument {repr} is not a mirrored option or share"
            )));
        }
        _ => {}
    }
    if !matches!(order.order_type, OrderType::Market | OrderType::Limit) {
        return Ok(Some(format!(
            "UnsupportedCapability: order type {} (MARKET/LIMIT only)",
            order.order_type.value()
        )));
    }
    if !matches!(order.tif, Tif::Day | Tif::Gtc) {
        return Ok(Some(format!("UnsupportedCapability: TIF {} (DAY/GTC only)", order.tif.value())));
    }
    if !order.quantity.is_integral().map_err(derr)? {
        let noun = if matches!(order.wire, Instrument::Equity { .. }) { "shares" } else { "contracts" };
        return Ok(Some(format!(
            "quantity {} is not a whole number of {noun}; refusing to round (I5)",
            s(&order.quantity)
        )));
    }
    Ok(None)
}

/// `_mixed_signs`: whether the nonzero holdings of one contract hold both signs.
pub fn mixed_signs(book: &[(String, PyDec)]) -> R<bool> {
    let mut signs: Vec<bool> = Vec::new();
    for (_, q) in book {
        if ne(q, &zero())? {
            let positive = gt(q, &zero())?;
            if !signs.contains(&positive) {
                signs.push(positive);
            }
        }
    }
    Ok(signs.len() > 1)
}

/// `_signed`.
fn signed(side: Side, quantity: &PyDec) -> R<PyDec> {
    if side == Side::Buy {
        Ok(quantity.clone())
    } else {
        neg(quantity)
    }
}

/// `_legs`: (contract, side, contracts) of every contract an order trades, legs as written.
fn legs_of(order: &NOrder) -> R<Vec<(LInstrument, Side, PyDec)>> {
    match order.model.as_ref() {
        Some(LInstrument::Combo(legs)) => {
            let mut out = Vec::new();
            for leg in legs {
                out.push((leg.contract.clone(), leg.side, mul_i(&order.quantity, leg.ratio)?));
            }
            Ok(out)
        }
        Some(other) => Ok(vec![(other.clone(), order.side, order.quantity.clone())]),
        None => wire("a screened order has a model instrument"),
    }
}

type Account = Vec<(String, PyDec)>;

pub struct Ticket {
    pub venue_order_id: String,
    pub instrument: LInstrument,
    pub order_type: OrderType,
    pub side: Side,
    pub quantity: PyDec,
    pub submitted_at: String,
    pub tif: Tif,
    pub limit_price: Option<PyDec>,
    pub allocations: Vec<(String, String, PyDec)>,
}

fn positive(q: &PyDec) -> R<bool> {
    Ok(!le(q, &zero())?)
}

/// `_ticket`: one same-side ticket, the sum of its orders, each allocated its own quantity; the
/// `VenueOrder` constructor's refusals included.
pub fn make_ticket(
    venue_account: &str,
    instrument: &LInstrument,
    order_type: OrderType,
    tif: Tif,
    limit: &Option<PyDec>,
    group: &[&NOrder],
    at: &str,
) -> R<Ticket> {
    let side = group[0].side;
    let mut quantity = zero();
    for leg in group {
        quantity = add(&quantity, &leg.quantity)?;
    }
    let ids: Vec<String> = group.iter().map(|l| l.order_id.clone()).collect();
    let key = ticket_key(venue_account, instrument, side, &quantity, order_type, limit, tif, &ids)?;
    let mut allocations = Vec::new();
    for leg in group {
        if leg.order_id.is_empty() {
            return nerr(VALUE, "strategy_order_id must be non-empty");
        }
        if leg.account_id.is_empty() {
            return nerr(VALUE, "account_id must be non-empty");
        }
        if !positive(&leg.quantity)? {
            return nerr(VALUE, format!("quantity must be positive, got {}", s(&leg.quantity)));
        }
        allocations.push((leg.order_id.clone(), leg.account_id.clone(), leg.quantity.clone()));
    }
    // VenueOrder.__post_init__
    let venue_order_id = format!("tos:{}", &key[..32]);
    if !positive(&quantity)? {
        return nerr(VALUE, format!("quantity must be positive, got {}", s(&quantity)));
    }
    let (_, aware) = parse_datetime(at)?;
    if !aware {
        return nerr(VALUE, "submitted_at must be timezone-aware UTC datetime (I7)");
    }
    match order_type {
        OrderType::Market if limit.is_some() => return nerr(VALUE, "MARKET order cannot have a limit_price"),
        OrderType::Limit if limit.is_none() => return nerr(VALUE, "LIMIT order must have a limit_price"),
        _ => {}
    }
    let mut allocated = zero();
    for (_, _, q) in &allocations {
        allocated = add(&allocated, q)?;
    }
    if ne(&allocated, &quantity)? {
        return nerr(VALUE, format!("VenueOrder allocations total {} but order quantity is {}", s(&allocated), s(&quantity)));
    }
    Ok(Ticket {
        venue_order_id,
        instrument: instrument.clone(),
        order_type,
        side,
        quantity,
        submitted_at: at.to_string(),
        tif,
        limit_price: limit.clone(),
        allocations,
    })
}

/// `_account_for_everything`: every input order ends in exactly one outcome (I11).
pub fn account_for_everything(expected: &[String], outcomes: &[String]) -> R<()> {
    let (mut a, mut b) = (outcomes.to_vec(), expected.to_vec());
    a.sort();
    b.sort();
    if a != b {
        return nerr(
            NETTING,
            format!("netting lost or duplicated an order: inputs {} vs outcomes {} (I11)", py_list(&b), py_list(&a)),
        );
    }
    Ok(())
}

pub struct Batch {
    pub venue_orders: Vec<Ticket>,
    pub refused: Vec<(String, String)>,
}

/// `net_strategy_orders`: net one batch into venue tickets; refuse what the venue cannot hold.
pub fn net_strategy_orders(
    orders: &[NOrder],
    venue_account: &str,
    mirrored: &[String],
    at: &str,
    holdings: &OMap<(String, LInstrument), PyDec>,
) -> R<Batch> {
    if orders.is_empty() {
        return nerr(NETTING, "no strategy orders to net");
    }
    let mut mirrored_set: Vec<String> = Vec::new();
    for m in mirrored {
        if !mirrored_set.contains(m) {
            mirrored_set.push(m.clone());
        }
    }
    let mut refused: Vec<(String, String)> = Vec::new();
    let mut seen_ids: Vec<&str> = Vec::new();
    let mut accepted: OMap<LInstrument, Vec<usize>> = OMap::new();
    let mut batch_side: OMap<LInstrument, (Side, String)> = OMap::new();
    let mut books: OMap<LInstrument, Account> = OMap::new();
    let mut verticals: Vec<usize> = Vec::new();

    for (index, order) in orders.iter().enumerate() {
        if seen_ids.contains(&order.order_id.as_str()) {
            refused.push((
                order.order_id.clone(),
                format!("duplicate strategy order {} in one batch (I3)", order.order_id),
            ));
            continue;
        }
        seen_ids.push(&order.order_id);
        if let Some(reason) = screen(order, &mirrored_set)? {
            refused.push((order.order_id.clone(), reason));
            continue;
        }
        let mut conflict: Option<String> = None;
        let mut trials: OMap<LInstrument, Account> = OMap::new();
        for (contract, side, contracts) in legs_of(order)? {
            let hk = contract.hk();
            if let Some((first_side, first_id)) = batch_side.get(&hk) {
                if *first_side != side {
                    conflict = Some(format!(
                        "conflict: {} {} opposes first-in {} {} on the same contract; refused at the venue only",
                        side.value(),
                        contract.symbol()?,
                        first_side.value(),
                        first_id
                    ));
                    break;
                }
            }
            let book: Account = match books.get(&hk) {
                Some(b) => b.clone(),
                None => holdings
                    .iter()
                    .filter(|((_, inst), _)| inst.hk() == hk)
                    .map(|((acct, _), qty)| (acct.clone(), qty.clone()))
                    .collect(),
            };
            let mut trial = book;
            let delta = signed(side, &contracts)?;
            match trial.iter_mut().find(|(a, _)| *a == order.account_id) {
                Some(slot) => slot.1 = add(&slot.1, &delta)?,
                None => trial.push((order.account_id.clone(), add(&zero(), &delta)?)),
            }
            if mixed_signs(&trial)? {
                let mut others: Vec<String> = Vec::new();
                for (acct, qty) in &trial {
                    if *acct != order.account_id && ne(qty, &zero())? {
                        others.push(acct.clone());
                    }
                }
                others.sort();
                conflict = Some(format!(
                    "conflict: {} {} for {} would hold the opposite side of {} in one venue account; refused at the venue only",
                    side.value(),
                    contract.symbol()?,
                    order.account_id,
                    others.join(", ")
                ));
                break;
            }
            trials.insert(hk, contract, trial);
        }
        if let Some(reason) = conflict {
            refused.push((order.order_id.clone(), reason));
            continue;
        }
        for (contract, side, _) in legs_of(order)? {
            let hk = contract.hk();
            let trial = trials.get(&hk).cloned().expect("every leg was trialled");
            books.insert(hk.clone(), contract.clone(), trial);
            if !batch_side.contains(&hk) {
                batch_side.insert(hk, contract, (side, order.order_id.clone()));
            }
        }
        if matches!(order.model, Some(LInstrument::Combo(_))) {
            verticals.push(index);
        } else {
            let model = order.model.clone().expect("screened");
            let hk = model.hk();
            match accepted.get_mut(&hk) {
                Some(list) => list.push(index),
                None => accepted.insert(hk, model, vec![index]),
            }
        }
    }

    let mut venue_orders: Vec<Ticket> = Vec::new();
    for (instrument, members) in accepted.iter() {
        let mut groups: OMap<(OrderType, Tif, Option<PyDec>), Vec<usize>> = OMap::new();
        for &m in members {
            let leg = &orders[m];
            let limit = if leg.order_type == OrderType::Limit { leg.limit_price.clone() } else { None };
            let hk = format!(
                "{}\u{1}{}\u{1}{}",
                leg.order_type.value(),
                leg.tif.value(),
                limit.as_ref().map_or("N".to_string(), |p| format!("L{}", p.num_key()))
            );
            match groups.get_mut(&hk) {
                Some(list) => list.push(m),
                None => groups.insert(hk, (leg.order_type, leg.tif, limit), vec![m]),
            }
        }
        for ((order_type, tif, limit), group) in groups.iter() {
            let legs: Vec<&NOrder> = group.iter().map(|&i| &orders[i]).collect();
            match make_ticket(venue_account, instrument, *order_type, *tif, limit, &legs, at) {
                Ok(t) => venue_orders.push(t),
                Err(e) if e.kind == VALUE => {
                    for leg in &legs {
                        refused.push((
                            leg.order_id.clone(),
                            format!("ticket for {} refused: {}", instrument.symbol()?, e.msg),
                        ));
                    }
                }
                Err(e) => return Err(e),
            }
        }
    }
    for &index in &verticals {
        let order = &orders[index];
        let model = order.model.as_ref().expect("screened");
        venue_orders.push(make_ticket(venue_account, model, order.order_type, order.tif, &order.limit_price, &[order], at)?);
    }

    let mut outcomes: Vec<String> = venue_orders.iter().flat_map(|t| t.allocations.iter().map(|a| a.0.clone())).collect();
    outcomes.extend(refused.iter().map(|r| r.0.clone()));
    let expected: Vec<String> = orders.iter().map(|o| o.order_id.clone()).collect();
    account_for_everything(&expected, &outcomes)?;
    Ok(Batch { venue_orders, refused })
}

// -- the door ---------------------------------------------------------------------------------------

fn ticket_json(t: &Ticket) -> Json {
    obj(vec![
        ("venue_order_id", jstr(t.venue_order_id.clone())),
        ("instrument", linstr_json(&t.instrument)),
        ("order_type", jstr(t.order_type.value())),
        ("side", jstr(t.side.value())),
        ("quantity", jstr(s(&t.quantity))),
        ("submitted_at", jstr(t.submitted_at.clone())),
        ("tif", jstr(t.tif.value())),
        ("limit_price", jopt_dec(&t.limit_price)),
        (
            "allocations",
            Json::Arr(
                t.allocations
                    .iter()
                    .map(|(o, a, q)| Json::Arr(vec![jstr(o.clone()), jstr(a.clone()), jstr(s(q))]))
                    .collect(),
            ),
        ),
    ])
}

fn strings_of(doc: &Json, key: &str) -> R<Vec<String>> {
    req_arr(doc, key)?
        .iter()
        .map(|v| match v {
            Json::Str(t) => Ok(t.clone()),
            _ => wire(format!("{key:?} holds a non-string")),
        })
        .collect()
}

pub fn net_op(doc: &Json) -> R<Json> {
    let orders: Vec<NOrder> = req_arr(doc, "orders")?.iter().map(norder_of).collect::<R<_>>()?;
    let mut holdings: OMap<(String, LInstrument), PyDec> = OMap::new();
    for row in req_arr(doc, "holdings")? {
        let Json::Arr(r) = row else { return wire("a holdings row is not a triple") };
        let [Json::Str(acct), inst, Json::Str(q)] = r.as_slice() else { return wire("a holdings row is not a triple") };
        let instrument = linstr(inst)?;
        let hk = crate::ledger::mirror::book_hk(acct, &instrument);
        holdings.insert(hk, (acct.clone(), instrument), dec_of(q)?);
    }
    let batch = net_strategy_orders(
        &orders,
        req_str(doc, "venue_account")?,
        &strings_of(doc, "mirrored")?,
        req_str(doc, "at")?,
        &holdings,
    )?;
    Ok(obj(vec![
        ("venue_orders", Json::Arr(batch.venue_orders.iter().map(ticket_json).collect())),
        (
            "refused",
            Json::Arr(batch.refused.iter().map(|(o, r)| Json::Arr(vec![jstr(o.clone()), jstr(r.clone())])).collect()),
        ),
    ]))
}

pub fn ticket_key_op(doc: &Json) -> R<Json> {
    let limit = opt_dec(doc, "limit_price")?;
    let key = ticket_key(
        req_str(doc, "venue_account")?,
        &linstr(req(doc, "instrument")?)?,
        side_of(req_str(doc, "side")?)?,
        &req_dec(doc, "quantity")?,
        order_type_of(req_str(doc, "order_type")?)?,
        &limit,
        tif_of(req_str(doc, "tif")?)?,
        &strings_of(doc, "order_ids")?,
    )?;
    Ok(obj(vec![("key", jstr(key))]))
}

pub fn screen_op(doc: &Json) -> R<Json> {
    let reason = screen(&norder_of(req(doc, "order")?)?, &strings_of(doc, "mirrored")?)?;
    Ok(obj(vec![("reason", reason.map_or(Json::Null, jstr))]))
}

pub fn mixed_signs_op(doc: &Json) -> R<Json> {
    let mut book: Vec<(String, PyDec)> = Vec::new();
    for row in req_arr(doc, "book")? {
        let Json::Arr(r) = row else { return wire("a book row is not a pair") };
        let [Json::Str(a), Json::Str(q)] = r.as_slice() else { return wire("a book row is not a pair") };
        book.push((a.clone(), dec_of(q)?));
    }
    Ok(obj(vec![("result", Json::Bool(mixed_signs(&book)?))]))
}

pub fn ticket_op(doc: &Json) -> R<Json> {
    let group: Vec<NOrder> = req_arr(doc, "group")?.iter().map(norder_of).collect::<R<_>>()?;
    let refs: Vec<&NOrder> = group.iter().collect();
    let t = make_ticket(
        req_str(doc, "venue_account")?,
        &linstr(req(doc, "instrument")?)?,
        order_type_of(req_str(doc, "order_type")?)?,
        tif_of(req_str(doc, "tif")?)?,
        &opt_dec(doc, "limit_price")?,
        &refs,
        req_str(doc, "at")?,
    )?;
    Ok(ticket_json(&t))
}

pub fn account_for_everything_op(doc: &Json) -> R<Json> {
    let mut outcomes = strings_of(doc, "allocated")?;
    outcomes.extend(strings_of(doc, "refused")?);
    account_for_everything(&strings_of(doc, "orders")?, &outcomes)?;
    Ok(obj(vec![("ok", Json::Bool(true))]))
}


#[cfg(test)]
mod tests {
    use super::*;
    use crate::ledger::json::parse;

    fn combo(second: &str, side2: &str, ratio2: i64) -> Json {
        parse(&format!(
            r#"{{"instrument": {{"kind": "combo", "legs": [
              {{"contract": {{"kind": "option", "underlying": "AAPL", "expiry": "2026-10-16", "strike": "200", "right": "P", "multiplier": 100}}, "ratio": 1, "side": "SELL"}},
              {{"contract": {second}, "ratio": {ratio2}, "side": "{side2}"}}]}}}}"#
        ))
        .unwrap()
    }

    fn reason(doc: &Json) -> Option<String> {
        match vertical_reason_op(doc).unwrap().get("reason").unwrap() {
            Json::Str(s) => Some(s.clone()),
            _ => None,
        }
    }

    const P195: &str = r#"{"kind": "option", "underlying": "AAPL", "expiry": "2026-10-16", "strike": "195", "right": "P", "multiplier": 100}"#;

    #[test]
    fn a_vertical_and_each_way_it_is_not() {
        assert_eq!(reason(&combo(P195, "BUY", 1)), None);
        assert_eq!(reason(&combo(P195, "SELL", 1)).unwrap(), "both legs on one side are not a vertical");
        assert_eq!(reason(&combo(P195, "BUY", 2)).unwrap(), "a 1:2 ratio spread is not a 1:1 vertical");
        let same = P195.replace("195", "200.0");
        assert_eq!(reason(&combo(&same, "BUY", 1)).unwrap(), "two legs on one strike are not a vertical");
        let call = P195.replace("\"P\"", "\"C\"");
        assert_eq!(reason(&combo(&call, "BUY", 1)).unwrap(), "a call leg and a put leg are not a vertical");
        let stock = r#"{"kind": "equity", "symbol": "AAPL"}"#;
        assert_eq!(
            reason(&combo(stock, "BUY", 1)).unwrap(),
            "SELL:1xAAPL  261016P00200000/BUY:1xAAPL is not a 2-leg option combo; only verticals are mirrored"
        );
    }

    const OPT: &str = r#"{"kind": "option", "underlying": "AAPL", "expiry": "2026-10-16", "strike": "200", "right": "P", "multiplier": 100}"#;

    fn order(id: &str, account: &str, side: &str, qty: &str) -> String {
        format!(
            r#"{{"order_id": "{id}", "account_id": "{account}", "instrument": {OPT}, "order_type": "LIMIT", "side": "{side}", "quantity": "{qty}", "tif": "DAY", "limit_price": "2.00"}}"#
        )
    }

    fn net(orders: &[String], holdings: &str) -> Result<Json, LErr> {
        let doc = parse(&format!(
            r#"{{"orders": [{}], "venue_account": "D-1", "mirrored": ["A", "B"], "at": "2026-09-24T20:00:00+00:00", "holdings": {holdings}}}"#,
            orders.join(",")
        ))
        .unwrap();
        net_op(&doc)
    }

    fn count(doc: &Json, key: &str) -> usize {
        match doc.get(key) {
            Some(Json::Arr(a)) => a.len(),
            _ => panic!("no {key}"),
        }
    }

    #[test]
    fn same_side_orders_net_into_one_ticket_and_opposing_ones_refuse() {
        let out = net(&[order("a", "A", "SELL", "1"), order("b", "B", "SELL", "3")], "[]").unwrap();
        assert_eq!((count(&out, "venue_orders"), count(&out, "refused")), (1, 0));
        let out = net(&[order("a", "A", "SELL", "1"), order("b", "B", "BUY", "3")], "[]").unwrap();
        assert_eq!((count(&out, "venue_orders"), count(&out, "refused")), (1, 1));
        let held = format!(r#"[["B", {OPT}, "2"]]"#);
        let out = net(&[order("a", "A", "SELL", "1")], &held).unwrap();
        assert_eq!((count(&out, "venue_orders"), count(&out, "refused")), (0, 1));
    }

    #[test]
    fn a_fraction_a_stranger_a_duplicate_and_an_empty_batch_refuse() {
        let out = net(&[order("a", "A", "SELL", "1.5"), order("b", "Z", "SELL", "1"), order("c", "A", "SELL", "1")], "[]").unwrap();
        assert_eq!((count(&out, "venue_orders"), count(&out, "refused")), (1, 2));
        let out = net(&[order("a", "A", "SELL", "1"), order("a", "A", "SELL", "1")], "[]").unwrap();
        assert_eq!((count(&out, "venue_orders"), count(&out, "refused")), (1, 1));
        assert_eq!(net(&[], "[]").unwrap_err().msg, "no strategy orders to net");
    }

    #[test]
    fn the_key_ignores_spelling_and_the_ledger_must_balance() {
        let one = |q: &str, l: &str| {
            let doc = parse(&format!(
                r#"{{"venue_account": "v", "instrument": {OPT}, "side": "SELL", "quantity": "{q}", "order_type": "LIMIT", "limit_price": "{l}", "tif": "DAY", "order_ids": ["a"]}}"#
            ))
            .unwrap();
            ticket_key_op(&doc).unwrap()
        };
        assert_eq!(one("2", "2.00"), one("2.0", "2"));
        assert_ne!(one("2", "2.00"), one("3", "2.00"));
        let e = account_for_everything(&["a".into()], &[]).unwrap_err();
        assert_eq!(e.msg, "netting lost or duplicated an order: inputs ['a'] vs outcomes [] (I11)");
    }
}

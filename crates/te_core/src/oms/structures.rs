//! `open_structures` and `uncovered_calls` (was `oms/options.py`): the open options
//! structures of a folded account, and the short calls no share or long call covers
//! (C3 / I8, C4, C5).
//!
//! Python semantics kept on purpose: `min`/`max` keep their first argument on a tie (so a
//! `-0` or a `2.0` survives as Python returned it), sums fold left from `Decimal("0")`,
//! and the latest-expiry-first sort is stable.

use crate::ledger::fold::AccountState;
use crate::ledger::model::{err, ComboLeg, Instrument, LErr, Order, OrderState, Side, R};
use crate::ledger::ops::{add, div, eq, mul, mul_i, neg, sub, zero};
use crate::sim::{dmax, dmin};
use crate::money::Money;
use crate::options::Right;
use crate::sim::snapshot::underlying_of;

/// One open structure: what the host cannot read off its own carriers.
#[derive(Debug, Clone)]
pub struct Open {
    pub entry_order_id: String,
    pub command_id: String,
    /// Per leg of `legs_of(entry)`, in order: the quantity still open.
    pub open_quantities: Vec<Money>,
    pub units: Money,
    pub entry_price: Money,
    /// Index in `state.fills` of the entry's first earliest fill.
    pub opened_fill: usize,
    pub target_order_id: Option<String>,
    pub closing_order_id: Option<String>,
}

fn terminal(s: OrderState) -> bool {
    matches!(s, OrderState::Filled | OrderState::Cancelled | OrderState::Rejected | OrderState::Expired)
}

fn working(o: &Order) -> bool {
    !terminal(o.state)
}

/// `is_structure`: a contract, or a combo of contracts only.
pub fn is_structure(i: &Instrument) -> bool {
    match i {
        Instrument::Option(_) => true,
        Instrument::Combo(legs) => legs.iter().all(|l| matches!(l.contract, Instrument::Option(_))),
        Instrument::Equity(_) => false,
        Instrument::Future(_) => false,
    }
}

/// `legs_of`: a combo's own legs, or one leg of a single instrument.
pub fn legs_of(i: &Instrument, side: Side) -> Vec<ComboLeg> {
    match i {
        Instrument::Combo(legs) => legs.clone(),
        other => vec![ComboLeg { contract: other.clone(), ratio: 1, side }],
    }
}

/// `Instrument.multiplier` (a mixed combo refuses, as the property did).
pub fn multiplier(i: &Instrument) -> R<i128> {
    match i {
        Instrument::Equity(_) => Ok(1),
        Instrument::Option(c) => Ok(c.multiplier),
        Instrument::Combo(legs) => {
            let mut found: Vec<i128> = Vec::new();
            for l in legs {
                let m = multiplier(&l.contract)?;
                if !found.contains(&m) {
                    found.push(m);
                }
            }
            if found.len() != 1 {
                found.sort();
                let shown: Vec<String> = found.iter().map(|m| m.to_string()).collect();
                return err(
                    "value",
                    format!("Combo has mixed leg multipliers [{}]; value it per leg (I6)", shown.join(", ")),
                );
            }
            multiplier(&legs[0].contract)
        }
        Instrument::Future(_) => err("unsupported", "futures not supported in OMS multiplier"),
    }
}

fn filled(st: &AccountState, id: &str) -> Money {
    st.filled_quantity.get(id).cloned().unwrap_or_else(zero)
}

fn target_id(entry: &str) -> String {
    format!("{entry}:target")
}

/// Net per unit in option points, positive: the credit collected or the debit paid.
fn entry_price(st: &AccountState, entry: &Order, units: &Money) -> R<Money> {
    let first = legs_of(&entry.instrument, entry.side);
    let scale = match first.first().map(|l| &l.contract) {
        Some(Instrument::Option(c)) => c.multiplier,
        Some(other) => multiplier(other)?,
        None => return err("value", "a structure with no legs"),
    };
    let mut collected = zero();
    for fill in st.fills.iter().filter(|f| f.order_id == entry.order_id) {
        let signed = if fill.side == Side::Sell { fill.price.clone() } else { neg(&fill.price)? };
        let term = mul_i(&mul(&signed, &fill.quantity)?, multiplier(&fill.instrument)?)?;
        collected = add(&collected, &term)?;
    }
    let collected = div(&collected, &mul_i(units, scale)?)?;
    if entry.side == Side::Sell {
        Ok(collected)
    } else {
        neg(&collected)
    }
}

/// Every structure with a leg still held, in entry order id order.
pub fn open_structures(st: &AccountState) -> R<Vec<Open>> {
    let mut entries: Vec<&Order> = st.orders.values().collect();
    entries.sort_by(|a, b| a.order_id.cmp(&b.order_id));
    let mut found = Vec::new();
    for entry in entries {
        if entry.parent_order_id.is_some() || !is_structure(&entry.instrument) {
            continue;
        }
        let entered = filled(st, &entry.order_id);
        if entered.cmp_int(0).map_err(crate::ledger::model::derr)?.is_le() {
            continue;
        }
        let children: Vec<&Order> =
            st.orders.values().filter(|o| o.parent_order_id.as_deref() == Some(entry.order_id.as_str())).collect();
        let mut closed = zero();
        for child in &children {
            closed = add(&closed, &filled(st, &child.order_id))?;
        }
        let units = sub(&entered, &closed)?;
        if units.cmp_int(0).map_err(crate::ledger::model::derr)?.is_le() {
            continue;
        }
        let mut open = Vec::new();
        for leg in legs_of(&entry.instrument, entry.side) {
            let held = st.positions.get(&leg.contract.hk()).map(|p| p.quantity.clone()).unwrap_or_else(zero);
            let on_side = if leg.side == Side::Buy { dmax(&held, &zero())? } else { dmax(&neg(&held)?, &zero())? };
            open.push(dmin(&mul_i(&units, leg.ratio)?, &on_side)?);
        }
        let mut all_zero = true;
        for q in &open {
            if !eq(q, &zero())? {
                all_zero = false;
                break;
            }
        }
        if all_zero {
            continue;
        }
        let tid = target_id(&entry.order_id);
        let target = children.iter().find(|c| c.order_id == tid && working(c)).map(|c| c.order_id.clone());
        let prefix = format!("{}:close:", entry.order_id);
        let closing =
            children.iter().find(|c| c.order_id.starts_with(&prefix) && working(c)).map(|c| c.order_id.clone());
        let price = entry_price(st, entry, &entered)?;
        let mut opened: Option<usize> = None;
        for (i, f) in st.fills.iter().enumerate() {
            if f.order_id != entry.order_id {
                continue;
            }
            match opened {
                Some(j) if st.fills[j].filled_at.instant() <= f.filled_at.instant() => {}
                _ => opened = Some(i),
            }
        }
        let Some(opened_fill) = opened else {
            return err("value", "min() iterable argument is empty");
        };
        found.push(Open {
            entry_order_id: entry.order_id.clone(),
            command_id: entry.order_id.strip_suffix(":entry").unwrap_or(&entry.order_id).to_string(),
            open_quantities: open,
            units,
            entry_price: price,
            opened_fill,
            target_order_id: target,
            closing_order_id: closing,
        });
    }
    Ok(found)
}

fn underlying(i: &Instrument) -> R<String> {
    underlying_of(i)
}

fn is_call(i: &Instrument) -> bool {
    matches!(i, Instrument::Option(c) if c.right == Right::Call)
}

fn expiry(i: &Instrument) -> chrono::NaiveDate {
    match i {
        Instrument::Option(c) => c.expiry,
        _ => unreachable!("only calls are booked"),
    }
}

/// Per underlying, ascending: (shares the short calls no long call covers deliver, shares free).
pub fn uncovered_calls(st: &AccountState, closing_counts: bool) -> R<Vec<(String, Money, Money)>> {
    let mut closing: Vec<Instrument> = Vec::new();
    if !closing_counts {
        for s in open_structures(st)? {
            if s.closing_order_id.is_some() {
                let entry = st.orders.get(&s.entry_order_id).ok_or_else(|| LErr { kind: "value", msg: "entry".into() })?;
                for leg in legs_of(&entry.instrument, entry.side) {
                    closing.push(leg.contract);
                }
            }
        }
    }
    type Book = Vec<(String, Vec<(Instrument, Money)>)>;
    fn book_add(book: &mut Book, key: String, item: (Instrument, Money)) {
        match book.iter_mut().find(|(k, _)| *k == key) {
            Some((_, v)) => v.push(item),
            None => book.push((key, vec![item])),
        }
    }
    let mut shorts: Book = Vec::new();
    let mut longs: Book = Vec::new();
    let mut shares: Vec<(String, Money)> = Vec::new();
    fn shares_add(shares: &mut Vec<(String, Money)>, key: &str, f: impl FnOnce(&Money) -> R<Money>) -> R<()> {
        match shares.iter_mut().find(|(k, _)| k == key) {
            Some((_, v)) => *v = f(v)?,
            None => {
                let v = f(&zero())?;
                shares.push((key.to_string(), v));
            }
        }
        Ok(())
    }
    for (instrument, position) in st.positions.iter() {
        if let Instrument::Equity(symbol) = instrument {
            let held = dmax(&position.quantity, &zero())?;
            shares_add(&mut shares, symbol, |v| add(v, &held))?;
            continue;
        }
        if !is_call(instrument) {
            continue;
        }
        let sign = position.quantity.cmp_int(0).map_err(crate::ledger::model::derr)?;
        if sign.is_eq() || (sign.is_lt() && closing.iter().any(|c| c.same(instrument))) {
            continue;
        }
        let key = underlying(instrument)?;
        let item = (instrument.clone(), crate::ledger::ops::abs(&position.quantity)?);
        if sign.is_lt() {
            book_add(&mut shorts, key, item);
        } else {
            book_add(&mut longs, key, item);
        }
    }
    for order in st.orders.values() {
        if !working(order) {
            continue;
        }
        if let (Instrument::Equity(symbol), Side::Sell) = (&order.instrument, order.side) {
            let remaining = sub(&order.quantity, &filled(st, &order.order_id))?;
            shares_add(&mut shares, symbol, |v| sub(v, &remaining))?;
        } else if order.parent_order_id.is_none() && is_structure(&order.instrument) {
            // An entry still working that will sell calls needs its cover now.
            for leg in legs_of(&order.instrument, order.side) {
                if leg.side == Side::Sell && is_call(&leg.contract) {
                    let key = underlying(&leg.contract)?;
                    let q = mul_i(&sub(&order.quantity, &filled(st, &order.order_id))?, leg.ratio)?;
                    book_add(&mut shorts, key, (leg.contract, q));
                }
            }
        }
    }
    let mut names: Vec<String> = shorts.iter().map(|(k, _)| k.clone()).collect();
    for (k, _) in &shares {
        if !names.contains(k) {
            names.push(k.clone());
        }
    }
    names.sort();
    let empty: Vec<(Instrument, Money)> = Vec::new();
    let mut result = Vec::new();
    for name in names {
        let long = longs.iter().find(|(k, _)| *k == name).map(|(_, v)| v).unwrap_or(&empty);
        let mut free: Vec<Money> = long.iter().map(|(_, q)| q.clone()).collect();
        let mut short: Vec<(Instrument, Money)> =
            shorts.iter().find(|(k, _)| *k == name).map(|(_, v)| v.clone()).unwrap_or_default();
        // Latest expiry first: a long that covers it covers every earlier short too.
        short.sort_by(|a, b| expiry(&b.0).cmp(&expiry(&a.0)));
        let mut uncovered = zero();
        for (contract, quantity) in &short {
            let mut need = quantity.clone();
            for (i, (lc, _)) in long.iter().enumerate() {
                if eq(&need, &zero())? {
                    break;
                }
                if expiry(lc) >= expiry(contract) && free[i].cmp_int(0).map_err(crate::ledger::model::derr)?.is_gt() {
                    let used = dmin(&need, &free[i])?;
                    free[i] = sub(&free[i], &used)?;
                    need = sub(&need, &used)?;
                }
            }
            let m = match contract {
                Instrument::Option(c) => c.multiplier,
                _ => 1,
            };
            uncovered = add(&uncovered, &mul_i(&need, m)?)?;
        }
        let held = shares.iter().find(|(k, _)| *k == name).map(|(_, v)| v.clone()).unwrap_or_else(zero);
        result.push((name, uncovered, dmax(&held, &zero())?));
    }
    Ok(result)
}

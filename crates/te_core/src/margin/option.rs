//! Option strategy margin (O3): LEAN's strategy matching and formulas, plus the
//! three departures from LEAN the Python module documented (best grouping,
//! diagonals, current marks). Ported rule for rule from
//! `trade_engine/metrics/option_margin.py`; read that history in git.
//!
//! Contracts are addressed by index into a table the caller supplies, so a
//! contract is never copied or compared by value; the Python shim maps the
//! indices back to its own objects.

use super::{add, dec, mul, sub, MarginError, Result, ZERO};
use chrono::NaiveDate;
use rust_decimal::prelude::ToPrimitive;
use rust_decimal::Decimal;
use std::cmp::{Ordering, Reverse};
use std::collections::HashMap;

// OptionMarginModel: a short option's floor is 10% of the strike (put) or of the
// underlying (call); the out-of-the-money test uses 20% of the underlying for
// equity options and 15% for index options.
pub const NAKED_FLOOR: Decimal = dec(10, 2);
pub const EQUITY_OTM_FRACTION: Decimal = dec(20, 2);
pub const INDEX_OTM_FRACTION: Decimal = dec(15, 2);
// Covered call initial margin, which LEAN inferred from IB's actual requirements,
// and the collar's call-side cap.
pub const COVERED_CALL_CALL_FRACTION: Decimal = dec(8, 1);
pub const COLLAR_CALL_FRACTION: Decimal = dec(25, 2);

pub const NAKED_CALL: &str = "Naked Call";
pub const NAKED_PUT: &str = "Naked Put";
pub const COVERED_CALL: &str = "Covered Call";
pub const PROTECTIVE_CALL: &str = "Protective Call";
pub const COVERED_PUT: &str = "Covered Put";
pub const PROTECTIVE_PUT: &str = "Protective Put";
pub const PROTECTIVE_COLLAR: &str = "Protective Collar";
pub const BEAR_CALL_SPREAD: &str = "Bear Call Spread";
pub const BEAR_PUT_SPREAD: &str = "Bear Put Spread";
pub const BULL_CALL_SPREAD: &str = "Bull Call Spread";
pub const BULL_PUT_SPREAD: &str = "Bull Put Spread";
pub const CALL_CALENDAR_SPREAD: &str = "Call Calendar Spread";
pub const SHORT_CALL_CALENDAR_SPREAD: &str = "Short Call Calendar Spread";
pub const PUT_CALENDAR_SPREAD: &str = "Put Calendar Spread";
pub const SHORT_PUT_CALENDAR_SPREAD: &str = "Short Put Calendar Spread";
pub const CALL_DIAGONAL_SPREAD: &str = "Call Diagonal Spread";
pub const PUT_DIAGONAL_SPREAD: &str = "Put Diagonal Spread";
pub const LONG_CALL: &str = "Long Call";
pub const LONG_PUT: &str = "Long Put";

/// Distinct books the exhaustive grouping search visits before it gives up and
/// keeps LEAN's grouping.
pub const DEFAULT_SEARCH_LIMIT: usize = 20_000;

fn refuse<T>(message: String) -> Result<T> {
    Err(MarginError::Option(message))
}

fn qty_overflow() -> MarginError {
    MarginError::Option("contract quantity arithmetic overflow (D6)".to_string())
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub enum Right {
    Call, // "C" sorts before "P", as `right.value` did
    Put,
}

impl Right {
    pub fn name(self) -> &'static str {
        match self {
            Right::Call => "call",
            Right::Put => "put",
        }
    }
}

/// What the option-roots table says about a contract's root. The table stays in
/// Python (`domain/option_roots.py`, used well beyond margin); its answer, or
/// its refusal, arrives here and is only raised if a rule actually reads it.
#[derive(Debug, Clone)]
pub enum Style {
    Resolved { underlying: String, index: bool },
    Unresolved(String),
}

#[derive(Debug, Clone)]
pub struct Contract {
    pub root: String,
    pub expiry: NaiveDate,
    pub strike: Decimal,
    pub right: Right,
    pub multiplier: i64,
    /// `contract.occ.strip()`, for messages.
    pub occ: String,
    pub style: Style,
}

fn order(a: &Contract, b: &Contract) -> Ordering {
    (a.expiry, a.strike, a.right, &a.root).cmp(&(b.expiry, b.strike, b.right, &b.root))
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Pred {
    SameExpiry,
    LaterExpiry,
    SameStrike,
    OtherStrike,
    StrikeAbove,
    StrikeBelow,
}

impl Pred {
    pub fn name(self) -> &'static str {
        match self {
            Pred::SameExpiry => "same_expiry",
            Pred::LaterExpiry => "later_expiry",
            Pred::SameStrike => "same_strike",
            Pred::OtherStrike => "other_strike",
            Pred::StrikeAbove => "strike_above",
            Pred::StrikeBelow => "strike_below",
        }
    }
    pub fn parse(s: &str) -> Option<Pred> {
        Some(match s {
            "same_expiry" => Pred::SameExpiry,
            "later_expiry" => Pred::LaterExpiry,
            "same_strike" => Pred::SameStrike,
            "other_strike" => Pred::OtherStrike,
            "strike_above" => Pred::StrikeAbove,
            "strike_below" => Pred::StrikeBelow,
            _ => return None,
        })
    }
    fn holds(self, first: &Contract, c: &Contract) -> bool {
        match self {
            Pred::SameExpiry => c.expiry == first.expiry,
            Pred::LaterExpiry => c.expiry > first.expiry,
            Pred::SameStrike => c.strike == first.strike,
            Pred::OtherStrike => c.strike != first.strike,
            Pred::StrikeAbove => c.strike > first.strike,
            Pred::StrikeBelow => c.strike < first.strike,
        }
    }
}

#[derive(Debug, Clone)]
pub struct LegDef {
    pub right: Right,
    /// Signed contracts per unit of the strategy; never zero.
    pub quantity: i64,
    pub predicates: Vec<Pred>,
}

#[derive(Debug, Clone)]
pub struct StratDef {
    pub name: String,
    /// Signed lots of the underlying per unit.
    pub underlying_lots: i64,
    pub legs: Vec<LegDef>,
}

impl StratDef {
    pub fn leg_count(&self) -> usize {
        self.legs.len() + usize::from(self.underlying_lots != 0)
    }
}

fn call(quantity: i64, predicates: &[Pred]) -> LegDef {
    LegDef { right: Right::Call, quantity, predicates: predicates.to_vec() }
}
fn put(quantity: i64, predicates: &[Pred]) -> LegDef {
    LegDef { right: Right::Put, quantity, predicates: predicates.to_vec() }
}
fn def(name: &str, underlying_lots: i64, legs: Vec<LegDef>) -> StratDef {
    StratDef { name: name.to_string(), underlying_lots, legs }
}

/// In LEAN's declaration order, which breaks ties between definitions of equal
/// leg count.
pub fn definitions() -> Vec<StratDef> {
    use Pred::*;
    vec![
        def(COVERED_CALL, 1, vec![call(-1, &[])]),
        def(PROTECTIVE_CALL, -1, vec![call(1, &[])]),
        def(COVERED_PUT, -1, vec![put(-1, &[])]),
        def(PROTECTIVE_PUT, 1, vec![put(1, &[])]),
        def(PROTECTIVE_COLLAR, 1, vec![call(-1, &[]), put(1, &[StrikeBelow, SameExpiry])]),
        def(NAKED_CALL, 0, vec![call(-1, &[])]),
        def(NAKED_PUT, 0, vec![put(-1, &[])]),
        def(BEAR_CALL_SPREAD, 0, vec![call(-1, &[]), call(1, &[StrikeAbove, SameExpiry])]),
        def(BEAR_PUT_SPREAD, 0, vec![put(1, &[]), put(-1, &[StrikeBelow, SameExpiry])]),
        def(BULL_CALL_SPREAD, 0, vec![call(1, &[]), call(-1, &[StrikeAbove, SameExpiry])]),
        def(BULL_PUT_SPREAD, 0, vec![put(-1, &[]), put(1, &[StrikeBelow, SameExpiry])]),
        def(CALL_CALENDAR_SPREAD, 0, vec![call(-1, &[]), call(1, &[SameStrike, LaterExpiry])]),
        def(SHORT_CALL_CALENDAR_SPREAD, 0, vec![call(1, &[]), call(-1, &[SameStrike, LaterExpiry])]),
        def(PUT_CALENDAR_SPREAD, 0, vec![put(-1, &[]), put(1, &[SameStrike, LaterExpiry])]),
        def(SHORT_PUT_CALENDAR_SPREAD, 0, vec![put(1, &[]), put(-1, &[SameStrike, LaterExpiry])]),
        def(CALL_DIAGONAL_SPREAD, 0, vec![call(-1, &[]), call(1, &[OtherStrike, LaterExpiry])]),
        def(PUT_DIAGONAL_SPREAD, 0, vec![put(-1, &[]), put(1, &[OtherStrike, LaterExpiry])]),
    ]
}

/// One strategy found in a book: legs in signed contracts (contract index,
/// quantity) and its underlying in signed lots, both already scaled by `quantity`.
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct Matched {
    pub name: String,
    pub quantity: i64,
    pub legs: Vec<(usize, i64)>,
    pub underlying_lots: i64,
}

/// A book: (contract index, signed contracts), each contract once.
pub type Book = Vec<(usize, i64)>;

fn qty_of(book: &Book, c: usize) -> i64 {
    book.iter().find(|(i, _)| *i == c).map_or(0, |(_, q)| *q)
}

fn held(ctx: &[Contract], book: &Book) -> Vec<usize> {
    let mut h: Vec<usize> = book.iter().filter(|(_, q)| *q != 0).map(|(i, _)| *i).collect();
    h.sort_by(|a, b| order(&ctx[*a], &ctx[*b])); // stable
    h
}

struct Walk<'a> {
    def: &'a StratDef,
    ctx: &'a [Contract],
    book: &'a Book,
    held: &'a [usize],
    limit: Option<usize>,
}

impl Walk<'_> {
    /// Returns true once `limit` matches are collected.
    fn extend(&self, chosen: &mut Vec<usize>, units: Option<i64>, out: &mut Vec<Matched>) -> Result<bool> {
        let index = chosen.len();
        if index == self.def.legs.len() {
            if let Some(found) = units.filter(|u| *u != 0) {
                let mut legs = Vec::with_capacity(chosen.len());
                for (c, leg) in chosen.iter().zip(&self.def.legs) {
                    legs.push((*c, leg.quantity.checked_mul(found).ok_or_else(qty_overflow)?));
                }
                out.push(Matched {
                    name: self.def.name.clone(),
                    quantity: found,
                    legs,
                    underlying_lots: self.def.underlying_lots.checked_mul(found).ok_or_else(qty_overflow)?,
                });
                return Ok(self.limit.is_some_and(|l| out.len() >= l));
            }
            return Ok(false);
        }
        let leg = &self.def.legs[index];
        for &c in self.held {
            let contract = &self.ctx[c];
            if chosen.contains(&c) || contract.right != leg.right {
                continue;
            }
            let quantity = qty_of(self.book, c);
            if (quantity > 0) != (leg.quantity > 0) {
                continue;
            }
            // a predicate reads the FIRST chosen leg; the first leg has none
            if !leg.predicates.iter().all(|p| chosen.first().is_none_or(|f| p.holds(&self.ctx[*f], contract))) {
                continue;
            }
            let fits = (quantity.unsigned_abs() / leg.quantity.unsigned_abs()) as i64;
            if fits == 0 {
                continue;
            }
            chosen.push(c);
            let next = Some(units.map_or(fits, |u| u.min(fits)));
            let stop = self.extend(chosen, next, out)?;
            chosen.pop();
            if stop {
                return Ok(true);
            }
        }
        Ok(false)
    }
}

/// `OptionStrategyDefinition.Match`: every way `def` fits the book, in
/// enumeration order, each taken as many times as all its legs allow. `limit`
/// stops after that many (1 = TryMatchOnce).
pub fn matches(
    def: &StratDef,
    ctx: &[Contract],
    book: &Book,
    lots: i64,
    limit: Option<usize>,
) -> Result<Vec<Matched>> {
    let held = held(ctx, book);
    if held.len() + usize::from(lots != 0) < def.leg_count() {
        return Ok(Vec::new());
    }
    let mut units = None;
    if def.underlying_lots != 0 {
        if (lots > 0) != (def.underlying_lots > 0) || lots.unsigned_abs() < def.underlying_lots.unsigned_abs() {
            return Ok(Vec::new());
        }
        units = Some((lots.unsigned_abs() / def.underlying_lots.unsigned_abs()) as i64);
    }
    let walk = Walk { def, ctx, book, held: &held, limit };
    let mut out = Vec::new();
    walk.extend(&mut Vec::new(), units, &mut out)?;
    Ok(out)
}

fn match_greedy(
    defs: &[&StratDef],
    ctx: &[Contract],
    book: &Book,
    mut lots: i64,
) -> Result<(Vec<Matched>, Book, i64)> {
    let mut remaining = book.clone();
    let mut strategies = Vec::new();
    for def in defs {
        loop {
            let Some(found) = matches(def, ctx, &remaining, lots, Some(1))?.into_iter().next() else {
                break;
            };
            for (c, q) in &found.legs {
                let slot = remaining.iter_mut().find(|(i, _)| i == c).expect("a match only names held contracts");
                slot.1 = slot.1.checked_sub(*q).ok_or_else(qty_overflow)?;
            }
            lots = lots.checked_sub(found.underlying_lots).ok_or_else(qty_overflow)?;
            strategies.push(found);
        }
        if lots == 0 && remaining.iter().all(|(_, q)| *q == 0) {
            break;
        }
    }
    remaining.retain(|(_, q)| *q != 0);
    Ok((strategies, remaining, lots))
}

fn by_leg_count(defs: &[StratDef]) -> Vec<&StratDef> {
    let mut sorted: Vec<&StratDef> = defs.iter().collect();
    sorted.sort_by_key(|d| Reverse(d.leg_count())); // stable: ties keep LEAN's order
    sorted
}

fn long(ctx: &[Contract], c: usize, quantity: i64) -> Matched {
    let name = if ctx[c].right == Right::Call { LONG_CALL } else { LONG_PUT };
    Matched { name: name.to_string(), quantity, legs: vec![(c, quantity)], underlying_lots: 0 }
}

/// Group one underlying's options (signed contracts) and lots of its shares.
/// Returns the strategies, with any leftover long option as a `Long Call`/`Long
/// Put`, and the lots no strategy used. A short option nothing covers refuses
/// (the default definitions always cover it as naked).
pub fn match_strategies(ctx: &[Contract], book: &Book, lots: i64, defs: &[StratDef]) -> Result<(Vec<Matched>, i64)> {
    let (mut strategies, unmatched, left) = match_greedy(&by_leg_count(defs), ctx, book, lots)?;
    let mut rest = unmatched;
    rest.sort_by(|a, b| order(&ctx[a.0], &ctx[b.0]));
    for (c, quantity) in rest {
        if quantity < 0 {
            return refuse(format!("Short {} matched no definition", ctx[c].occ));
        }
        strategies.push(long(ctx, c, quantity));
    }
    Ok((strategies, left))
}

// --- formulas ---------------------------------------------------------------------------

/// One matched strategy's requirements at one set of prices.
#[derive(Debug, Clone, PartialEq)]
pub struct StrategyMargin {
    pub name: String,
    pub underlying: String,
    pub quantity: i64,
    pub legs: Vec<(usize, i64)>,
    pub shares: Decimal,
    pub initial: Decimal,
    pub maintenance: Decimal,
    /// What an account without margin must hold for the option legs; None where
    /// cash cannot secure the position at all.
    pub cash_secured: Option<Decimal>,
    /// Maintenance less the credit taken in at entry; None when an entry price is unknown.
    pub net_of_credit: Option<Decimal>,
}

fn dec_i(n: i64) -> Decimal {
    Decimal::from(n)
}

fn otm(c: &Contract, price: Decimal) -> Result<Decimal> {
    let amount = if c.right == Right::Call { sub(c.strike, price)? } else { sub(price, c.strike)? };
    Ok(amount.max(ZERO))
}

fn intrinsic(c: &Contract, price: Decimal) -> Result<Decimal> {
    if price <= ZERO {
        return Err(MarginError::Value(format!("Underlying price must be positive, got {price} (I5)")));
    }
    let amount = if c.right == Right::Call { sub(price, c.strike)? } else { sub(c.strike, price)? };
    Ok(amount.max(ZERO))
}

/// OptionMarginModel for `contracts` short (negative) contracts, initial and
/// maintenance alike: the premium plus the larger of 10% of the strike (put) or
/// underlying (call), and 20% (15% for an index) of the underlying less the
/// amount out of the money. A long option needs nothing.
pub fn naked_margin(c: &Contract, contracts: i64, underlying: Decimal, mark: Decimal) -> Result<Decimal> {
    if contracts >= 0 {
        return Ok(ZERO);
    }
    let base = if c.right == Right::Put { c.strike } else { underlying };
    let index = match &c.style {
        Style::Resolved { index, .. } => *index,
        Style::Unresolved(message) => return Err(MarginError::Unresolvable(message.clone())),
    };
    let fraction = if index { INDEX_OTM_FRACTION } else { EQUITY_OTM_FRACTION };
    let band = sub(mul(fraction, underlying)?, otm(c, underlying)?)?;
    let per_share = add(mark, mul(NAKED_FLOOR, base)?.max(band))?;
    let count = dec_i(contracts.checked_neg().ok_or_else(qty_overflow)?);
    mul(mul(per_share, dec_i(c.multiplier))?, count)
}

/// The prices and fractions one pricing run reads.
pub struct Market<'a> {
    pub ctx: &'a [Contract],
    pub underlying: &'a str,
    pub price: Decimal,
    /// Per contract index.
    pub marks: &'a [Option<Decimal>],
    pub entries: Option<&'a [Option<Decimal>]>,
    pub initial_fraction: Decimal,
    pub maintenance_fraction: Decimal,
}

impl Market<'_> {
    fn value(&self, c: usize, contracts: i64) -> Result<Decimal> {
        let mark = self.marks[c].ok_or_else(|| MarginError::Option(format!("No session-close mark for {} (I5)", self.ctx[c].occ)))?;
        mul(mul(mark, dec_i(self.ctx[c].multiplier))?, dec_i(contracts))
    }

    fn leg(&self, s: &Matched, right: Right, short: Option<bool>) -> Result<(usize, i64)> {
        for (c, q) in &s.legs {
            if self.ctx[*c].right == right && short.is_none_or(|sh| (*q < 0) == sh) {
                return Ok((*c, *q));
            }
        }
        let which = match short {
            Some(true) => "short ",
            Some(false) => "long ",
            None => "",
        };
        refuse(format!("{} has no {}{} leg", s.name, which, right.name()))
    }

    /// max(long strike - short strike, 0) for calls, max(short - long, 0) for puts,
    /// per unit, in dollars.
    fn width(&self, s: &Matched, right: Right) -> Result<Decimal> {
        let (short, _) = self.leg(s, right, Some(true))?;
        let (long, _) = self.leg(s, right, Some(false))?;
        let (short, long) = (&self.ctx[short], &self.ctx[long]);
        let difference =
            if right == Right::Call { sub(long.strike, short.strike)? } else { sub(short.strike, long.strike)? };
        mul(mul(difference.max(ZERO), dec_i(short.multiplier))?, dec_i(s.quantity))
    }

    /// (initial before premium, maintenance, cash secured).
    fn formula(&self, s: &Matched, multiplier: i64) -> Result<(Decimal, Decimal, Option<Decimal>)> {
        let price = self.price;
        let name = s.name.as_str();
        let lots_shares = s.underlying_lots.checked_mul(multiplier).ok_or_else(qty_overflow)?;
        let shares = dec_i(lots_shares);
        let stock_value = mul(shares.abs(), price)?;
        let stock_initial = mul(self.initial_fraction, stock_value)?;
        let stock_maintenance = mul(self.maintenance_fraction, stock_value)?;
        let first = || s.legs.first().copied().ok_or_else(|| MarginError::Option(format!("{name} has no legs")));
        let neg = |q: i64| q.checked_neg().map(dec_i).ok_or_else(qty_overflow);

        if name == NAKED_CALL || name == NAKED_PUT {
            let (c, q) = first()?;
            let margin = naked_margin(&self.ctx[c], q, price, self.marks[c].unwrap_or(ZERO))?;
            let cash = if name == NAKED_PUT {
                Some(mul(mul(self.ctx[c].strike, dec_i(self.ctx[c].multiplier))?, neg(q)?)?)
            } else {
                None
            };
            return Ok((margin, margin, cash));
        }
        if [LONG_CALL, LONG_PUT, CALL_CALENDAR_SPREAD, PUT_CALENDAR_SPREAD].contains(&name) {
            return Ok((ZERO, ZERO, Some(ZERO)));
        }
        if [BEAR_CALL_SPREAD, BULL_CALL_SPREAD, CALL_DIAGONAL_SPREAD].contains(&name) {
            let width = self.width(s, Right::Call)?;
            return Ok((width, width, Some(width)));
        }
        if [BEAR_PUT_SPREAD, BULL_PUT_SPREAD, PUT_DIAGONAL_SPREAD].contains(&name) {
            let width = self.width(s, Right::Put)?;
            return Ok((width, width, Some(width)));
        }
        if name == SHORT_CALL_CALENDAR_SPREAD || name == SHORT_PUT_CALENDAR_SPREAD {
            let (c, q) = s
                .legs
                .iter()
                .find(|(_, q)| *q < 0)
                .copied()
                .ok_or_else(|| MarginError::Option(format!("{name} has no short leg")))?;
            let contract = &self.ctx[c];
            let margin = naked_margin(contract, q, price, self.marks[c].unwrap_or(ZERO))?;
            let cash = if contract.right == Right::Put {
                Some(mul(mul(contract.strike, dec_i(contract.multiplier))?, neg(q)?)?)
            } else {
                None
            };
            return Ok((margin, margin, cash));
        }
        if name == COVERED_CALL {
            // MAX[ITM + stock margin at min(price, strike), min(stock value, max(call value, stock margin))]
            let (c, q) = first()?;
            let contract = &self.ctx[c];
            let itm = mul(mul(intrinsic(contract, price)?, dec_i(contract.multiplier))?, neg(q)?)?;
            let hypothetical = mul(mul(self.maintenance_fraction, shares.abs())?, price.min(contract.strike))?;
            let value = self.value(c, q)?;
            let second = stock_value.min(value.max(stock_maintenance));
            let maintenance = add(itm, hypothetical)?.max(second);
            let initial = add(mul(COVERED_CALL_CALL_FRACTION, value.abs())?, stock_initial)?;
            return Ok((initial, maintenance, Some(ZERO)));
        }
        if name == PROTECTIVE_PUT || name == PROTECTIVE_CALL {
            // min(10% of strike + out-of-the-money amount, stock maintenance)
            let (c, q) = first()?;
            let contract = &self.ctx[c];
            let per_share = add(mul(NAKED_FLOOR, contract.strike)?, otm(contract, price)?)?;
            let option = mul(mul(per_share, dec_i(contract.multiplier))?, dec_i(q))?;
            let cash = if name == PROTECTIVE_PUT { Some(ZERO) } else { None };
            return Ok((stock_initial, option.min(stock_maintenance), cash));
        }
        if name == COVERED_PUT {
            let (c, q) = first()?;
            let contract = &self.ctx[c];
            let margin = add(stock_initial, mul(mul(intrinsic(contract, price)?, dec_i(contract.multiplier))?, neg(q)?)?)?;
            return Ok((margin, margin, None));
        }
        if name == PROTECTIVE_COLLAR {
            // maintenance: min(10% of put strike + put OTM, 25% of call strike);
            // initial: stock initial + call in-the-money amount
            let (pc, put_quantity) = self.leg(s, Right::Put, None)?;
            let (cc, call_quantity) = self.leg(s, Right::Call, None)?;
            let (put, call) = (&self.ctx[pc], &self.ctx[cc]);
            let per_share = add(mul(NAKED_FLOOR, put.strike)?, otm(put, price)?)?.min(mul(COLLAR_CALL_FRACTION, call.strike)?);
            let maintenance = mul(mul(per_share, dec_i(put.multiplier))?, dec_i(put_quantity))?;
            let initial = add(
                stock_initial,
                mul(mul(intrinsic(call, price)?, dec_i(call.multiplier))?, neg(call_quantity)?)?,
            )?;
            return Ok((initial, maintenance, Some(ZERO)));
        }
        refuse(format!("No margin formula for {name}"))
    }

    fn sorted_multipliers(&self, legs: impl Iterator<Item = usize>) -> Vec<i64> {
        let mut m: Vec<i64> = legs.map(|c| self.ctx[c].multiplier).collect();
        m.sort_unstable();
        m.dedup();
        m
    }
}

fn list(values: &[i64]) -> String {
    format!("[{}]", values.iter().map(i64::to_string).collect::<Vec<_>>().join(", "))
}

/// Margin one matched strategy. Initial margin also carries the net premium when
/// the strategy was bought for a debit (`OptionInitialMargin`); a credit adds
/// nothing. Entry prices (per share) give `net_of_credit`.
pub fn strategy_margin(s: &Matched, m: &Market<'_>) -> Result<StrategyMargin> {
    if m.price <= ZERO {
        return refuse(format!("No price for {} (I5)", m.underlying));
    }
    for (c, _) in &s.legs {
        match m.marks[*c] {
            Some(mark) if mark > ZERO => {}
            _ => return refuse(format!("No session-close mark for {} (I5)", m.ctx[*c].occ)),
        }
    }
    let multipliers = m.sorted_multipliers(s.legs.iter().map(|(c, _)| *c));
    if multipliers.len() != 1 {
        return refuse(format!("{} mixes contract multipliers {} (I6)", s.name, list(&multipliers)));
    }
    let multiplier = multipliers[0];
    let (initial, maintenance, cash) = m.formula(s, multiplier)?;
    let mut premium = ZERO;
    for (c, q) in &s.legs {
        premium = add(premium, m.value(*c, *q)?)?;
    }
    let mut net = None;
    if let Some(entries) = m.entries {
        if s.legs.iter().all(|(c, _)| entries[*c].is_some()) {
            let mut opened = ZERO;
            for (c, q) in &s.legs {
                let entry = entries[*c].expect("checked above");
                opened = add(opened, mul(mul(entry, dec_i(m.ctx[*c].multiplier))?, dec_i(*q))?)?;
            }
            net = Some(sub(maintenance, (-opened).max(ZERO))?);
        }
    }
    let shares = dec_i(s.underlying_lots.checked_mul(multiplier).ok_or_else(qty_overflow)?);
    Ok(StrategyMargin {
        name: s.name.clone(),
        underlying: m.underlying.to_string(),
        quantity: s.quantity,
        legs: s.legs.clone(),
        shares,
        initial: add(initial, premium.max(ZERO))?,
        maintenance,
        cash_secured: cash,
        net_of_credit: net,
    })
}

// --- grouping ---------------------------------------------------------------------------

// (naked short contracts, maintenance, initial), compared in that order
type Cost = (Decimal, Decimal, Decimal);
type Items = Vec<(usize, i64)>;
type Grouping = (Cost, Vec<Matched>, i64);

fn add_cost(a: Cost, b: Cost) -> Result<Cost> {
    Ok((add(a.0, b.0)?, add(a.1, b.1)?, add(a.2, b.2)?))
}

enum Stop {
    TooLarge,
    Err(MarginError),
}

impl From<MarginError> for Stop {
    fn from(e: MarginError) -> Stop {
        Stop::Err(e)
    }
}

struct Search<'a, 'm> {
    market: &'a Market<'m>,
    defs: &'a [StratDef],
    multiplier: i64,
    limit: usize,
    memo: HashMap<(Items, i64), Option<Grouping>>,
    priced: HashMap<Matched, StrategyMargin>,
}

impl Search<'_, '_> {
    fn figures(&mut self, s: &Matched) -> Result<StrategyMargin> {
        if let Some(found) = self.priced.get(s) {
            return Ok(found.clone());
        }
        let figures = strategy_margin(s, self.market)?;
        self.priced.insert(s.clone(), figures.clone());
        Ok(figures)
    }

    fn strategy_cost(&mut self, s: &Matched) -> Result<Cost> {
        let margin = self.figures(s)?;
        let naked = if s.name == NAKED_CALL || s.name == NAKED_PUT { dec_i(s.quantity) } else { ZERO };
        Ok((naked, margin.maintenance, margin.initial))
    }

    fn lots_cost(&self, left: i64) -> Result<Cost> {
        let value = mul(mul(dec_i(left.checked_abs().ok_or_else(qty_overflow)?), dec_i(self.multiplier))?, self.market.price)?;
        Ok((ZERO, mul(self.market.maintenance_fraction, value)?, mul(self.market.initial_fraction, value)?))
    }

    fn items(&self, book: &Book) -> Items {
        let mut items: Items = book.iter().filter(|(_, q)| *q != 0).copied().collect();
        items.sort_by(|a, b| order(&self.market.ctx[a.0], &self.market.ctx[b.0]));
        items
    }

    /// The cheapest grouping of the items. Every grouping places the first
    /// remaining contract somewhere, so each step only tries the strategies that
    /// include it.
    fn best(&mut self, items: Items, lots: i64) -> std::result::Result<Option<Grouping>, Stop> {
        let key = (items, lots);
        if let Some(found) = self.memo.get(&key) {
            return Ok(found.clone());
        }
        if self.memo.len() >= self.limit {
            return Err(Stop::TooLarge);
        }
        let items = &key.0;
        let found: Option<Grouping> = if items.is_empty() {
            Some((self.lots_cost(lots)?, Vec::new(), lots))
        } else {
            let (first, quantity) = items[0];
            let current: Book = items.clone();
            let mut candidates: Vec<Matched> = Vec::new();
            for def in self.defs {
                for found in matches(def, self.market.ctx, &current, lots, None)? {
                    if found.legs.iter().any(|(c, _)| *c == first) {
                        candidates.push(found);
                    }
                }
            }
            if quantity > 0 {
                candidates.push(long(self.market.ctx, first, quantity));
            }
            let mut best: Option<Grouping> = None;
            for candidate in candidates {
                let mut remaining = current.clone();
                for (c, taken) in &candidate.legs {
                    let slot = remaining.iter_mut().find(|(i, _)| i == c).expect("a match only names held contracts");
                    slot.1 = slot.1.checked_sub(*taken).ok_or_else(qty_overflow)?;
                }
                let next_lots = lots.checked_sub(candidate.underlying_lots).ok_or_else(qty_overflow)?;
                let next_items = self.items(&remaining);
                let Some(rest) = self.best(next_items, next_lots)? else {
                    continue;
                };
                let cost = add_cost(self.strategy_cost(&candidate)?, rest.0)?;
                if best.as_ref().is_none_or(|b| cost < b.0) {
                    let mut chosen = vec![candidate];
                    chosen.extend(rest.1);
                    best = Some((cost, chosen, rest.2));
                }
            }
            best
        };
        self.memo.insert(key, found.clone());
        Ok(found)
    }
}

/// Margin one underlying's options (signed contracts) with the account's signed
/// `shares` of it. Whole lots of shares can go into strategies, as LEAN counts
/// them.
///
/// LEAN takes its greedy grouping as found, and that grouping can be dear: two
/// bull put spreads, 95/90 and 85/80, come out as a 90/85 bear put spread and a
/// 95/80 bull put spread, 1,500 against 1,000. So every grouping is also
/// searched. The one leaving the fewest naked short contracts is kept (LEAN's
/// own objective), then the one with the least maintenance and initial margin,
/// counting the lots left over as plain stock. LEAN's grouping wins ties and
/// stands when the book is too large to search. Returns the strategies and the
/// shares none used.
#[allow(clippy::too_many_arguments)]
pub fn margin_book(
    underlying: &str,
    ctx: &[Contract],
    book: &Book,
    shares: Decimal,
    price: Decimal,
    marks: &[Option<Decimal>],
    initial_fraction: Decimal,
    maintenance_fraction: Decimal,
    entries: Option<&[Option<Decimal>]>,
    defs: &[StratDef],
    search_limit: usize,
) -> Result<(Vec<StrategyMargin>, Decimal)> {
    let mut multipliers: Vec<i64> = book.iter().map(|(c, _)| ctx[*c].multiplier).collect();
    multipliers.sort_unstable();
    multipliers.dedup();
    if multipliers.len() != 1 {
        return refuse(format!("{underlying} options mix contract multipliers {} (I6)", list(&multipliers)));
    }
    let multiplier = multipliers[0];
    // whole lots, toward zero (Python `int(shares / multiplier)`)
    let lots = shares
        .checked_div(dec_i(multiplier))
        .and_then(|q| q.trunc().to_i64())
        .ok_or_else(qty_overflow)?;

    let market = Market { ctx, underlying, price, marks, entries, initial_fraction, maintenance_fraction };
    let mut search = Search { market: &market, defs, multiplier, limit: search_limit, memo: HashMap::new(), priced: HashMap::new() };

    let (greedy, left) = match_strategies(ctx, book, lots, defs)?;
    let mut cost = search.lots_cost(left)?;
    for s in &greedy {
        cost = add_cost(cost, search.strategy_cost(s)?)?;
    }
    let (mut chosen, mut chosen_left) = (greedy, left);
    let start = search.items(book);
    match search.best(start, lots) {
        Ok(Some((searched, grouping, grouping_left))) => {
            if searched < cost {
                chosen = grouping;
                chosen_left = grouping_left;
            }
        }
        Ok(None) | Err(Stop::TooLarge) => {}
        Err(Stop::Err(e)) => return Err(e),
    }
    let used = dec_i(
        lots.checked_sub(chosen_left)
            .and_then(|n| n.checked_mul(multiplier))
            .ok_or_else(qty_overflow)?,
    );
    let mut figures = Vec::with_capacity(chosen.len());
    for s in &chosen {
        figures.push(search.figures(s)?);
    }
    Ok((figures, sub(shares, used)?))
}

#[cfg(test)]
mod tests {
    use super::*;
    use proptest::prelude::*;
    use std::str::FromStr;

    fn d(s: &str) -> Decimal {
        Decimal::from_str(s).unwrap()
    }

    fn contract(right: Right, strike: &str, month: u32, index: bool) -> Contract {
        Contract {
            root: "AAPL".to_string(),
            expiry: NaiveDate::from_ymd_opt(2026 + (month / 12) as i32, month % 12 + 1, 16).unwrap(),
            strike: d(strike),
            right,
            multiplier: 100,
            occ: format!("AAPL {right:?} {strike}"),
            style: Style::Resolved { underlying: "AAPL".to_string(), index },
        }
    }

    fn universe() -> Vec<Contract> {
        let mut u = Vec::new();
        for right in [Right::Call, Right::Put] {
            for strike in ["90", "100", "110"] {
                for month in [9, 17] {
                    u.push(contract(right, strike, month, false));
                }
            }
        }
        u
    }

    fn book_strategy() -> impl Strategy<Value = (Book, i64)> {
        (
            proptest::collection::btree_map(0usize..12, prop_oneof![-3i64..=-1, 1i64..=3], 1..5),
            -2i64..=2,
        )
            .prop_map(|(m, lots)| (m.into_iter().collect::<Book>(), lots))
    }

    fn run(book: &Book, lots: i64) -> Result<(Vec<StrategyMargin>, Decimal)> {
        let ctx = universe();
        let marks: Vec<Option<Decimal>> = ctx.iter().map(|_| Some(d("2.5"))).collect();
        margin_book(
            "AAPL",
            &ctx,
            book,
            Decimal::from(lots * 100),
            d("100"),
            &marks,
            d("0.5"),
            d("0.25"),
            None,
            &definitions(),
            DEFAULT_SEARCH_LIMIT,
        )
    }

    #[test]
    fn a_naked_put_is_premium_plus_twenty_percent_less_otm() {
        // 20% of 100 less 10 out of the money = 10, above 10% of the 90 strike (9).
        let c = contract(Right::Put, "90", 9, false);
        assert_eq!(naked_margin(&c, -1, d("100"), d("1")).unwrap(), d("1100"));
    }

    #[test]
    fn an_unresolved_root_refuses_only_when_read() {
        let mut c = contract(Right::Put, "90", 9, false);
        c.style = Style::Unresolved("no such root".to_string());
        assert_eq!(naked_margin(&c, 1, d("100"), d("1")).unwrap(), ZERO);
        assert_eq!(naked_margin(&c, -1, d("100"), d("1")).unwrap_err().kind(), "unresolvable");
    }

    #[test]
    fn a_missing_mark_refuses_with_the_python_message() {
        let ctx = universe();
        let marks = vec![None; ctx.len()];
        let e = margin_book("AAPL", &ctx, &vec![(0, -1)], ZERO, d("100"), &marks, d("0.5"), d("0.25"), None, &definitions(), 20000)
            .unwrap_err();
        assert_eq!(e.kind(), "option");
        assert!(e.message().starts_with("No session-close mark for"), "{e}");
    }

    #[test]
    fn definitions_are_seventeen_in_lean_order() {
        let names: Vec<String> = definitions().into_iter().map(|x| x.name).collect();
        assert_eq!(names.len(), 17);
        assert_eq!(names[0], "Covered Call");
    }

    proptest! {
        #[test]
        fn naked_margin_is_never_negative_and_grows_with_each_short(
            n in 1i64..50, strike in 1u32..300, under in 0u32..400, mark in 0u32..5000, put in any::<bool>(), index in any::<bool>(),
        ) {
            let c = contract(if put { Right::Put } else { Right::Call }, &strike.to_string(), 9, index);
            let (u, m) = (Decimal::from(under), Decimal::from(mark) / Decimal::from(100));
            let a = naked_margin(&c, -n, u, m).unwrap();
            let b = naked_margin(&c, -(n + 1), u, m).unwrap();
            prop_assert!(a >= ZERO);
            prop_assert!(b >= a);
            prop_assert_eq!(naked_margin(&c, n, u, m).unwrap(), ZERO);
        }

        #[test]
        fn a_book_never_needs_negative_margin_and_its_order_is_irrelevant((book, lots) in book_strategy()) {
            let first = run(&book, lots);
            let mut reversed = book.clone();
            reversed.reverse();
            let second = run(&reversed, lots);
            prop_assert_eq!(&first, &second);
            if let Ok((strategies, _)) = first {
                for s in strategies {
                    prop_assert!(s.initial >= ZERO && s.maintenance >= ZERO, "{s:?}");
                }
            }
        }

        #[test]
        fn matching_uses_every_contract_exactly_once((book, lots) in book_strategy()) {
            let ctx = universe();
            let (matched, left) = match_strategies(&ctx, &book, lots, &definitions()).unwrap();
            for (c, q) in &book {
                let used: i64 = matched.iter().flat_map(|m| m.legs.iter()).filter(|(i, _)| i == c).map(|(_, q)| *q).sum();
                prop_assert_eq!(used, *q);
            }
            let lots_used: i64 = matched.iter().map(|m| m.underlying_lots).sum();
            prop_assert_eq!(lots_used + left, lots);
        }
    }
}

//! The boundary Python crosses (docs/RUST_PORT.md D6): decimals as strings,
//! everything else plain integers, strings and JSON built from them. No rule
//! lives here; this only turns text into `margin` types and back.
//!
//! Contracts are described once in a table and referred to by index everywhere
//! else, so Python never has to describe a contract twice.

use super::option::{
    self, Contract, LegDef, Matched, Pred, Right, StratDef, Style, StrategyMargin, Market,
};
use super::{AccountInput, Kind, MarginError, Position, Result};
use chrono::NaiveDate;
use rust_decimal::Decimal;
use serde::Deserialize;
use serde_json::{json, Value};
use std::collections::HashMap;

fn bad(message: String) -> MarginError {
    MarginError::Value(message)
}

pub fn parse_dec(s: &str) -> Result<Decimal> {
    Decimal::from_str_exact(s.trim()).map_err(|_| bad(format!("{s:?} is not a decimal of at most 28 digits (D6)")))
}

fn parse_opt(s: &Option<String>) -> Result<Option<Decimal>> {
    s.as_deref().map(parse_dec).transpose()
}

fn parse_marks(v: &[Option<String>]) -> Result<Vec<Option<Decimal>>> {
    v.iter().map(parse_opt).collect()
}

fn json_in<'a, T: Deserialize<'a>>(text: &'a str) -> Result<T> {
    serde_json::from_str(text).map_err(|e| bad(format!("margin request is not valid: {e}")))
}

#[derive(Deserialize)]
struct WContract {
    root: String,
    expiry: String,
    strike: String,
    right: String,
    multiplier: i64,
    occ: String,
    /// `option_style(root)`: its underlying and whether it cash-settles, or its refusal.
    underlying: Option<String>,
    index: Option<bool>,
    style_error: Option<String>,
}

fn contracts(table: &[WContract]) -> Result<Vec<Contract>> {
    table
        .iter()
        .map(|w| {
            let style = match (&w.style_error, &w.underlying, w.index) {
                (Some(message), _, _) => Style::Unresolved(message.clone()),
                (None, Some(underlying), Some(index)) => Style::Resolved { underlying: underlying.clone(), index },
                _ => return Err(bad(format!("contract {} has neither a style nor a style_error", w.occ))),
            };
            if w.multiplier <= 0 {
                return Err(bad(format!("Multiplier must be positive, got {}", w.multiplier)));
            }
            Ok(Contract {
                root: w.root.clone(),
                expiry: NaiveDate::parse_from_str(&w.expiry, "%Y-%m-%d")
                    .map_err(|_| bad(format!("Invalid expiry {:?}", w.expiry)))?,
                strike: parse_dec(&w.strike)?,
                right: right(&w.right)?,
                multiplier: w.multiplier,
                occ: w.occ.clone(),
                style,
            })
        })
        .collect()
}

fn right(s: &str) -> Result<Right> {
    match s {
        "C" => Ok(Right::Call),
        "P" => Ok(Right::Put),
        _ => Err(bad(format!("Invalid option right: {s}"))),
    }
}

fn right_str(r: Right) -> &'static str {
    match r {
        Right::Call => "C",
        Right::Put => "P",
    }
}

#[derive(Deserialize, Clone)]
struct WLeg {
    right: String,
    quantity: i64,
    predicates: Vec<String>,
}

#[derive(Deserialize, Clone)]
struct WDef {
    name: String,
    underlying_lots: i64,
    legs: Vec<WLeg>,
}

fn defs(w: &Option<Vec<WDef>>) -> Result<Vec<StratDef>> {
    let Some(w) = w else {
        return Ok(option::definitions());
    };
    w.iter()
        .map(|d| {
            let legs = d
                .legs
                .iter()
                .map(|l| {
                    if l.quantity == 0 {
                        return Err(bad(format!("{} has a leg of zero contracts", d.name)));
                    }
                    let predicates = l
                        .predicates
                        .iter()
                        .map(|p| Pred::parse(p).ok_or_else(|| bad(format!("Unknown leg predicate {p:?}"))))
                        .collect::<Result<Vec<_>>>()?;
                    Ok(LegDef { right: right(&l.right)?, quantity: l.quantity, predicates })
                })
                .collect::<Result<Vec<_>>>()?;
            Ok(StratDef { name: d.name.clone(), underlying_lots: d.underlying_lots, legs })
        })
        .collect()
}

fn book(pairs: &[(usize, i64)], n: usize) -> Result<option::Book> {
    let mut seen = vec![false; n];
    for (i, _) in pairs {
        if *i >= n || std::mem::replace(&mut seen[*i], true) {
            return Err(bad(format!("book names contract {i} out of range or twice")));
        }
    }
    Ok(pairs.to_vec())
}

fn matched_json(m: &Matched) -> Value {
    json!({
        "name": m.name,
        "quantity": m.quantity,
        "legs": m.legs.iter().map(|(c, q)| json!([c, q])).collect::<Vec<_>>(),
        "underlying_lots": m.underlying_lots,
    })
}

fn margin_json(m: &StrategyMargin) -> Value {
    json!({
        "name": m.name,
        "underlying": m.underlying,
        "quantity": m.quantity,
        "legs": m.legs.iter().map(|(c, q)| json!([c, q])).collect::<Vec<_>>(),
        "shares": m.shares.to_string(),
        "initial": m.initial.to_string(),
        "maintenance": m.maintenance.to_string(),
        "cash_secured": m.cash_secured.map(|d| d.to_string()),
        "net_of_credit": m.net_of_credit.map(|d| d.to_string()),
    })
}

fn dump(v: Value) -> Result<String> {
    serde_json::to_string(&v).map_err(|e| bad(format!("margin response could not be written: {e}")))
}

#[derive(Deserialize)]
struct MatchReq {
    contracts: Vec<WContract>,
    book: Vec<(usize, i64)>,
    lots: i64,
    definitions: Option<Vec<WDef>>,
}

pub fn match_json(request: &str) -> Result<String> {
    let r: MatchReq = json_in(request)?;
    let ctx = contracts(&r.contracts)?;
    let book = book(&r.book, ctx.len())?;
    let (strategies, left) = option::match_strategies(&ctx, &book, r.lots, &defs(&r.definitions)?)?;
    dump(json!({ "strategies": strategies.iter().map(matched_json).collect::<Vec<_>>(), "left": left }))
}

#[derive(Deserialize)]
struct WMatched {
    name: String,
    quantity: i64,
    legs: Vec<(usize, i64)>,
    underlying_lots: i64,
}

#[derive(Deserialize)]
struct StrategyReq {
    contracts: Vec<WContract>,
    strategy: WMatched,
    underlying: String,
    underlying_price: String,
    marks: Vec<Option<String>>,
    initial_fraction: String,
    maintenance_fraction: String,
    entries: Option<Vec<Option<String>>>,
}

fn in_range(legs: &[(usize, i64)], n: usize) -> Result<()> {
    match legs.iter().find(|(c, _)| *c >= n) {
        Some((c, _)) => Err(bad(format!("strategy names contract {c} out of range"))),
        None => Ok(()),
    }
}

pub fn strategy_json(request: &str) -> Result<String> {
    let r: StrategyReq = json_in(request)?;
    let ctx = contracts(&r.contracts)?;
    in_range(&r.strategy.legs, ctx.len())?;
    let marks = parse_marks(&r.marks)?;
    let entries = r.entries.as_deref().map(parse_marks).transpose()?;
    if marks.len() != ctx.len() || entries.as_ref().is_some_and(|e| e.len() != ctx.len()) {
        return Err(bad("marks and entries must be indexed like the contract table".to_string()));
    }
    let market = Market {
        ctx: &ctx,
        underlying: &r.underlying,
        price: parse_dec(&r.underlying_price)?,
        marks: &marks,
        entries: entries.as_deref(),
        initial_fraction: parse_dec(&r.initial_fraction)?,
        maintenance_fraction: parse_dec(&r.maintenance_fraction)?,
    };
    let s = Matched {
        name: r.strategy.name,
        quantity: r.strategy.quantity,
        legs: r.strategy.legs,
        underlying_lots: r.strategy.underlying_lots,
    };
    dump(margin_json(&option::strategy_margin(&s, &market)?))
}

#[derive(Deserialize)]
struct BookReq {
    underlying: String,
    contracts: Vec<WContract>,
    book: Vec<(usize, i64)>,
    shares: String,
    underlying_price: String,
    marks: Vec<Option<String>>,
    initial_fraction: String,
    maintenance_fraction: String,
    entries: Option<Vec<Option<String>>>,
    definitions: Option<Vec<WDef>>,
    search_limit: usize,
}

pub fn book_json(request: &str) -> Result<String> {
    let r: BookReq = json_in(request)?;
    let ctx = contracts(&r.contracts)?;
    let book = book(&r.book, ctx.len())?;
    let marks = parse_marks(&r.marks)?;
    let entries = r.entries.as_deref().map(parse_marks).transpose()?;
    if marks.len() != ctx.len() || entries.as_ref().is_some_and(|e| e.len() != ctx.len()) {
        return Err(bad("marks and entries must be indexed like the contract table".to_string()));
    }
    let (figures, left) = option::margin_book(
        &r.underlying,
        &ctx,
        &book,
        parse_dec(&r.shares)?,
        parse_dec(&r.underlying_price)?,
        &marks,
        parse_dec(&r.initial_fraction)?,
        parse_dec(&r.maintenance_fraction)?,
        entries.as_deref(),
        &defs(&r.definitions)?,
        r.search_limit,
    )?;
    dump(json!({ "strategies": figures.iter().map(margin_json).collect::<Vec<_>>(), "shares_left": left.to_string() }))
}

#[derive(Deserialize)]
struct NakedReq {
    contract: WContract,
    contracts: i64,
    underlying: String,
    mark: String,
}

pub fn naked_str(request: &str) -> Result<String> {
    let r: NakedReq = json_in(request)?;
    let ctx = contracts(std::slice::from_ref(&r.contract))?;
    Ok(option::naked_margin(&ctx[0], r.contracts, parse_dec(&r.underlying)?, parse_dec(&r.mark)?)?.to_string())
}

#[derive(Deserialize)]
struct WPosition {
    symbol: String,
    kind: String,
    quantity: String,
    mark: Option<String>,
    multiplier: i64,
    flat: bool,
    contract: Option<usize>,
    avg_cost: Option<String>,
}

#[derive(Deserialize)]
struct WOverride {
    initial: String,
    maintenance: String,
}

#[derive(Deserialize)]
struct AccountReq {
    cash: String,
    positions: Vec<WPosition>,
    contracts: Vec<WContract>,
    equity_marks: HashMap<String, String>,
    underlying_prices: HashMap<String, String>,
    overrides: HashMap<String, WOverride>,
    definitions: Option<Vec<WDef>>,
    search_limit: usize,
}

fn dec_map(m: &HashMap<String, String>) -> Result<HashMap<String, Decimal>> {
    m.iter().map(|(k, v)| Ok((k.clone(), parse_dec(v)?))).collect()
}

pub fn account_json(request: &str) -> Result<String> {
    let r: AccountReq = json_in(request)?;
    let ctx = contracts(&r.contracts)?;
    let positions = r
        .positions
        .iter()
        .map(|p| {
            let kind = match p.kind.as_str() {
                "equity" => Kind::Equity,
                "option" => Kind::Option,
                "combo" => Kind::Combo,
                other => return Err(bad(format!("Unknown position kind {other:?}"))),
            };
            if let Some(c) = p.contract {
                if c >= ctx.len() {
                    return Err(bad(format!("position names contract {c} out of range")));
                }
            }
            Ok(Position {
                symbol: p.symbol.clone(),
                kind,
                quantity: parse_dec(&p.quantity)?,
                mark: parse_opt(&p.mark)?,
                multiplier: p.multiplier,
                flat: p.flat,
                contract: p.contract,
                avg_cost: parse_opt(&p.avg_cost)?,
            })
        })
        .collect::<Result<Vec<_>>>()?;
    let overrides = r
        .overrides
        .iter()
        .map(|(k, v)| Ok((k.clone(), (parse_dec(&v.initial)?, parse_dec(&v.maintenance)?))))
        .collect::<Result<HashMap<_, _>>>()?;
    let equity_marks = dec_map(&r.equity_marks)?;
    let underlying_prices = dec_map(&r.underlying_prices)?;
    let definitions = defs(&r.definitions)?;
    let m = super::account_margin(&AccountInput {
        cash: parse_dec(&r.cash)?,
        positions: &positions,
        contracts: &ctx,
        equity_marks: &equity_marks,
        underlying_prices: &underlying_prices,
        overrides: &overrides,
        definitions: &definitions,
        search_limit: r.search_limit,
    })?;
    dump(json!({
        "equity": m.equity.to_string(),
        "cash": m.cash.to_string(),
        "market_value_long": m.market_value_long.to_string(),
        "market_value_short": m.market_value_short.to_string(),
        "gross_exposure": m.gross_exposure.to_string(),
        "net_exposure": m.net_exposure.to_string(),
        "margin_initial": m.margin_initial.to_string(),
        "margin_maintenance": m.margin_maintenance.to_string(),
        "positions": m.positions.iter().map(|p| json!({
            "symbol": p.symbol,
            "quantity": p.quantity.to_string(),
            "mark": p.mark.to_string(),
            "market_value": p.requirement.market_value.to_string(),
            "initial": p.requirement.initial.to_string(),
            "maintenance": p.requirement.maintenance.to_string(),
        })).collect::<Vec<_>>(),
        "strategies": m.strategies.iter().map(margin_json).collect::<Vec<_>>(),
    }))
}

/// (market value, initial, maintenance) of one position.
pub fn requirement_str(
    quantity: &str,
    mark: &str,
    initial: Option<&str>,
    maintenance: Option<&str>,
) -> Result<(String, String, String)> {
    let fractions = match (initial, maintenance) {
        (Some(i), Some(m)) => Some((parse_dec(i)?, parse_dec(m)?)),
        (None, None) => None,
        _ => return Err(bad("an override needs both fractions".to_string())),
    };
    let r = super::margin_requirement(parse_dec(quantity)?, parse_dec(mark)?, fractions)?;
    Ok((r.market_value.to_string(), r.initial.to_string(), r.maintenance.to_string()))
}

pub fn validate_override_str(initial: &str, maintenance: &str) -> Result<()> {
    super::validate_override(parse_dec(initial)?, parse_dec(maintenance)?)
}

/// (margin used, margin available)
pub fn summary_str(equity: &str, maintenance: &str) -> Result<(String, String)> {
    let (used, available) = super::margin_summary(parse_dec(equity)?, parse_dec(maintenance)?)?;
    Ok((used.to_string(), available.to_string()))
}

/// The fractions and strategy names the Python shim exposes as constants, so the
/// numbers and labels are written once.
pub fn constants_json() -> Result<String> {
    use option::*;
    dump(json!({
        "initial_fraction": super::INITIAL_FRACTION.to_string(),
        "maintenance_fraction": super::MAINTENANCE_FRACTION.to_string(),
        "naked_floor": NAKED_FLOOR.to_string(),
        "equity_otm_fraction": EQUITY_OTM_FRACTION.to_string(),
        "index_otm_fraction": INDEX_OTM_FRACTION.to_string(),
        "covered_call_call_fraction": COVERED_CALL_CALL_FRACTION.to_string(),
        "collar_call_fraction": COLLAR_CALL_FRACTION.to_string(),
        "search_limit": DEFAULT_SEARCH_LIMIT,
        "names": {
            "NAKED_CALL": NAKED_CALL, "NAKED_PUT": NAKED_PUT, "COVERED_CALL": COVERED_CALL,
            "PROTECTIVE_CALL": PROTECTIVE_CALL, "COVERED_PUT": COVERED_PUT,
            "PROTECTIVE_PUT": PROTECTIVE_PUT, "PROTECTIVE_COLLAR": PROTECTIVE_COLLAR,
            "BEAR_CALL_SPREAD": BEAR_CALL_SPREAD, "BEAR_PUT_SPREAD": BEAR_PUT_SPREAD,
            "BULL_CALL_SPREAD": BULL_CALL_SPREAD, "BULL_PUT_SPREAD": BULL_PUT_SPREAD,
            "CALL_CALENDAR_SPREAD": CALL_CALENDAR_SPREAD,
            "SHORT_CALL_CALENDAR_SPREAD": SHORT_CALL_CALENDAR_SPREAD,
            "PUT_CALENDAR_SPREAD": PUT_CALENDAR_SPREAD,
            "SHORT_PUT_CALENDAR_SPREAD": SHORT_PUT_CALENDAR_SPREAD,
            "CALL_DIAGONAL_SPREAD": CALL_DIAGONAL_SPREAD, "PUT_DIAGONAL_SPREAD": PUT_DIAGONAL_SPREAD,
            "LONG_CALL": LONG_CALL, "LONG_PUT": LONG_PUT,
        },
    }))
}

/// The default strategy definitions, in LEAN's order, as Python's `DEFINITIONS`.
pub fn definitions_json() -> Result<String> {
    dump(Value::Array(
        option::definitions()
            .iter()
            .map(|d| {
                json!({
                    "name": d.name,
                    "underlying_lots": d.underlying_lots,
                    "legs": d.legs.iter().map(|l| json!({
                        "right": right_str(l.right),
                        "quantity": l.quantity,
                        "predicates": l.predicates.iter().map(|p| p.name()).collect::<Vec<_>>(),
                    })).collect::<Vec<_>>(),
                })
            })
            .collect(),
    ))
}

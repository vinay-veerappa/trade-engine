//! The options entry rules (was `risk_options.py`, docs/RUST_PORT.md P3a): an
//! `OptionIntent` measured against one account's `OptionRiskRules` on the account as it
//! would stand once the entry filled (O4, rules doc §6.1-§6.2, I5, I11).
//!
//! Every rule is here except the two that read the ledger (`duplicate_protection`,
//! `persistent_kill_switch`): the host appends those, last, as the Python did. The
//! configuration is validated by the Python dataclasses that carry it; this reads it.
//!
//! Python semantics kept on purpose: every comparison and sum is `Decimal`'s (`PyDec`),
//! `all()` stops at its first false, the margin figures come from `te_core::margin` as
//! `account_margin` returned them, and an attribute the Python read off an `Equity` leg
//! (`.right`, `.occ`) refuses as the `AttributeError` it raised.

use chrono::NaiveDate;
use std::collections::HashMap;

use crate::ledger::fold::{make_position, AccountState};
use crate::ledger::model::{derr, err, make_equity, ComboLeg, Instrument, LErr, OrderState, Side, R};
use crate::ledger::ops::{abs, add, div, eq, ge, gt, le, lt, mul, mul_i, neg, sub, zero, OMap};
use crate::ledger::pydec::PyDec;
use crate::margin::option::{self as mo, Contract, Style};
use crate::margin::{self as mg, AccountInput, Kind, MarginError};
use crate::oms::structures::{is_structure, legs_of, multiplier, open_structures, uncovered_calls};
use crate::options::{option_style, Right, Settlement};
use crate::sim::snapshot::underlying_of;

/// A measured value or threshold: text, or a `Decimal`.
#[derive(Debug, Clone)]
pub enum Val {
    S(String),
    D(PyDec),
}

#[derive(Debug, Clone)]
pub struct RuleResult {
    pub name: String,
    pub passed: bool,
    pub measured: Val,
    pub threshold: Val,
    pub reason: String,
}

/// `EntryQuoteRules`, already validated.
#[derive(Debug, Clone, Default)]
pub struct EntryQuote {
    pub short_put_abs_delta: Option<(PyDec, PyDec)>,
    pub min_short_bid: Option<PyDec>,
    pub short_bid_return: Option<(PyDec, PyDec)>,
    pub min_short_implied_vol: Option<PyDec>,
    pub min_open_interest: Option<i128>,
    pub max_leg_spread_frac: Option<PyDec>,
    pub min_underlying_price: Option<PyDec>,
    pub min_credit_width_frac: Option<PyDec>,
    pub min_credit_return: Option<PyDec>,
    pub max_friction_frac: Option<PyDec>,
}

/// `OptionRiskRules`, already validated.
#[derive(Debug, Clone)]
pub struct Rules {
    pub max_margin_frac: PyDec,
    pub allowed_regimes: Vec<String>,
    pub no_earnings_before_expiry: bool,
    pub max_name_margin_frac: Option<PyDec>,
    pub max_name_collateral_frac: Option<PyDec>,
    pub put_notional_frac_by_regime: Option<Vec<(String, PyDec)>>,
    pub max_loss_per_structure_frac: Option<PyDec>,
    pub max_debit_per_structure_frac: Option<PyDec>,
    pub max_total_debit_frac: Option<PyDec>,
    pub max_share_notional_frac: Option<PyDec>,
    pub entry_quote: Option<EntryQuote>,
}

#[derive(Debug, Clone)]
pub struct Intent {
    pub instrument: Instrument,
    pub side: Side,
    pub quantity: PyDec,
    pub limit_price: Option<PyDec>,
}

/// One `OptionQuote`, as far as the rules read it. `occ` is the quoted contract's.
#[derive(Debug, Clone)]
pub struct Quote {
    pub occ: String,
    pub bid: PyDec,
    pub ask: PyDec,
    pub implied_vol: Option<PyDec>,
    /// `Decimal(str(greeks.delta))`; None when the quote carries no greeks.
    pub delta: Option<PyDec>,
    pub open_interest: Option<i128>,
}

impl Quote {
    pub fn mid(&self) -> R<PyDec> {
        div(&add(&self.bid, &self.ask)?, &PyDec::from_i128(2))
    }
    pub fn spread(&self) -> R<PyDec> {
        sub(&self.ask, &self.bid)
    }
}

/// The snapshot pricing the entry's underlying (`_snapshot`): its quotes of the contracts
/// the rules may ask for.
#[derive(Debug, Clone)]
pub struct Snapshot {
    pub underlying: String,
    pub underlying_price: PyDec,
    pub quotes: Vec<Quote>,
}

impl Snapshot {
    /// `ChainSnapshot.get`: matched on `contract.occ`.
    pub fn get(&self, contract: &Instrument) -> R<Option<&Quote>> {
        let occ = match contract {
            Instrument::Option(c) => c.occ()?,
            other => return no_attr(other, "occ"),
        };
        Ok(self.quotes.iter().find(|q| q.occ == occ))
    }
}

pub struct Context<'a> {
    pub state: &'a AccountState,
    pub snapshot: Option<&'a Snapshot>,
    /// `context.snapshot is snapshot`: the entry is decided at this very snapshot.
    pub live: bool,
}

/// `regime_of(context.session)`.
pub type Regime<'a> = dyn FnMut() -> R<Option<String>> + 'a;
/// `next_earnings(underlying, session)`: None when the source cannot say (stale),
/// `Some(None)` when the name reports none.
pub type Earnings<'a> = dyn FnMut() -> R<Option<Option<NaiveDate>>> + 'a;

fn type_name(i: &Instrument) -> &'static str {
    match i {
        Instrument::Equity(_) => "Equity",
        Instrument::Option(_) => "OptionContract",
        Instrument::Combo(_) => "Combo",
        Instrument::Future(_) => "Future",
    }
}

fn no_attr<T>(i: &Instrument, attr: &str) -> R<T> {
    err("attribute", format!("'{}' object has no attribute '{}'", type_name(i), attr))
}

fn right_of(i: &Instrument) -> R<Right> {
    match i {
        Instrument::Option(c) => Ok(c.right),
        other => no_attr(other, "right"),
    }
}

fn expiry_of(i: &Instrument) -> R<NaiveDate> {
    match i {
        Instrument::Option(c) => Ok(c.expiry),
        other => no_attr(other, "expiry"),
    }
}

fn strike_of(i: &Instrument) -> R<PyDec> {
    match i {
        Instrument::Option(c) => Ok(c.strike.clone()),
        other => no_attr(other, "strike"),
    }
}

fn occ_strip(i: &Instrument) -> R<String> {
    match i {
        Instrument::Option(c) => Ok(c.occ()?.trim().to_string()),
        other => no_attr(other, "occ"),
    }
}

fn ds(d: &PyDec) -> String {
    d.to_py_string()
}

fn is_option(i: &Instrument) -> bool {
    matches!(i, Instrument::Option(_))
}

fn positive(d: &Option<PyDec>) -> R<Option<PyDec>> {
    // `equity if equity is not None and equity > 0`
    match d {
        Some(e) if gt(e, &zero())? => Ok(Some(e.clone())),
        _ => Ok(None),
    }
}

/// `equity * frac if equity is not None and equity > 0 else None`
fn cap_of(equity: &Option<PyDec>, frac: &PyDec) -> R<Option<PyDec>> {
    match positive(equity)? {
        Some(e) => Ok(Some(mul(&e, frac)?)),
        None => Ok(None),
    }
}

fn d_or(d: &Option<PyDec>, text: &str) -> Val {
    match d {
        Some(v) => Val::D(v.clone()),
        None => Val::S(text.to_string()),
    }
}

fn opt_le(a: &Option<PyDec>, b: &Option<PyDec>) -> R<bool> {
    match (a, b) {
        (Some(a), Some(b)) => le(a, b),
        _ => Ok(false),
    }
}

// -- the book -------------------------------------------------------------------------

struct Book {
    equity: Option<PyDec>,
    after: Option<AccountState>,
    error: Option<String>,
    before: Option<AccountState>,
}

fn underlying_price(contract: &Instrument, st: &AccountState, snapshot: Option<&Snapshot>) -> R<Option<PyDec>> {
    let underlying = underlying_of(contract)?;
    if let Some(snap) = snapshot {
        if snap.underlying == underlying {
            return Ok(Some(snap.underlying_price.clone()));
        }
    }
    Ok(st.marks.get(&make_equity(&underlying)?.hk()).cloned())
}

fn leg_price(leg: &ComboLeg, net: &PyDec, legs: &[ComboLeg], snapshot: Option<&Snapshot>) -> R<Option<PyDec>> {
    if legs.len() == 1 {
        return Ok(Some(net.clone()));
    }
    let Some(snap) = snapshot else { return Ok(None) };
    match snap.get(&leg.contract)? {
        Some(q) => {
            let mid = q.mid()?;
            Ok(if gt(&mid, &zero())? { Some(mid) } else { None })
        }
        None => Ok(None),
    }
}

fn book(intent: &Intent, price: &Option<PyDec>, ctx: &Context<'_>) -> R<Book> {
    let st = ctx.state;
    let mut marks: OMap<Instrument, Option<PyDec>> = OMap::default();
    for (k, v) in st.marks.iter() {
        marks.insert(k.hk(), k.clone(), Some(v.clone()));
    }
    if let (true, Some(snap)) = (ctx.live, ctx.snapshot) {
        // At a snapshot the book is worth what these quotes say, not last night's marks.
        for (instrument, _) in st.positions.iter() {
            if is_option(instrument) && underlying_of(instrument)? == snap.underlying {
                if let Some(q) = snap.get(instrument)? {
                    let mid = q.mid()?;
                    if gt(&mid, &zero())? {
                        marks.insert(instrument.hk(), instrument.clone(), Some(mid));
                    }
                }
            }
        }
        let e = make_equity(&snap.underlying)?;
        marks.insert(e.hk(), e, Some(snap.underlying_price.clone()));
    }
    let mut value = zero();
    for (instrument, position) in st.positions.iter() {
        if eq(&position.quantity, &zero())? {
            continue;
        }
        match marks.get(&instrument.hk()).cloned().flatten() {
            None => {
                let error = format!("no mark for {}", instrument.symbol()?);
                return Ok(Book { equity: None, after: None, error: Some(error), before: None });
            }
            Some(mark) => {
                value = add(&value, &mul_i(&mul(&position.quantity, &mark)?, multiplier(instrument)?)?)?;
            }
        }
    }
    let equity = add(&st.cash, &value)?;
    let Some(price) = price else {
        return Ok(Book { equity: Some(equity), after: None, error: Some("the entry has no price".into()), before: None });
    };
    let mut positions = st.positions.clone();
    let legs = legs_of(&intent.instrument, intent.side);
    let mut cash = st.cash.clone();
    for leg in &legs {
        let contracts = mul_i(&intent.quantity, leg.ratio)?;
        let signed = if leg.side == Side::Buy { contracts } else { neg(&contracts)? };
        let Some(lp) = leg_price(leg, price, &legs, ctx.snapshot)? else {
            let error = format!("no price for {}", leg.contract.symbol()?);
            return Ok(Book { equity: Some(equity), after: None, error: Some(error), before: None });
        };
        let hk = leg.contract.hk();
        let held = positions.get(&hk).map(|p| p.quantity.clone()).unwrap_or_else(zero);
        let quantity = add(&held, &signed)?;
        let position = make_position(&st.account_id, leg.contract.clone(), quantity, lp.clone(), zero(), Vec::new())?;
        positions.insert(hk.clone(), leg.contract.clone(), position);
        if !marks.contains(&hk) {
            marks.insert(hk, leg.contract.clone(), Some(lp.clone()));
        }
        cash = sub(&cash, &mul_i(&mul(&signed, &lp)?, multiplier(&leg.contract)?)?)?;
        if is_option(&leg.contract) {
            let key = make_equity(&underlying_of(&leg.contract)?)?;
            let up = underlying_price(&leg.contract, st, ctx.snapshot)?;
            if !marks.contains(&key.hk()) {
                marks.insert(key.hk(), key, up);
            }
        }
    }
    let mut kept: OMap<Instrument, PyDec> = OMap::default();
    for (k, v) in marks.iter() {
        if let Some(v) = v {
            kept.insert(k.hk(), k.clone(), v.clone());
        }
    }
    let mut after = st.clone();
    after.positions = positions;
    after.marks = kept.clone();
    after.cash = cash;
    let mut before = st.clone();
    before.marks = kept;
    Ok(Book { equity: Some(equity), after: Some(after), error: None, before: Some(before) })
}

// -- margin (was `metrics.margin.account_margin`) -------------------------------------

/// What the rules read off an `AccountMargin`.
#[derive(Debug)]
pub(crate) struct Margin {
    used: PyDec,
    /// (name, underlying, maintenance, cash_secured)
    strategies: Vec<(String, String, PyDec, Option<PyDec>)>,
    /// (symbol, maintenance)
    positions: Vec<(String, PyDec)>,
}

fn merr(e: MarginError) -> LErr {
    LErr { kind: e.kind(), msg: e.message().to_string() }
}

/// `Decimal(str)` of a margin figure.
fn back(d: &rust_decimal::Decimal) -> R<PyDec> {
    let t = d.to_string();
    PyDec::parse(&t).map_or_else(|| err("value", format!("not a Decimal: {t:?}")), Ok)
}

fn mo_parse(s: &str) -> R<rust_decimal::Decimal> {
    mg::wire::parse_dec(s).map_err(merr)
}

/// `account_margin(state)` with no overrides and no underlying prices: the request the
/// Python built, read back in the order the margin reader read it.
pub(crate) fn margin_of(st: &AccountState) -> R<Margin> {
    struct WPos {
        symbol: String,
        kind: Kind,
        quantity: String,
        mark: Option<String>,
        multiplier: i64,
        flat: bool,
        contract: Option<usize>,
        avg_cost: Option<String>,
    }
    let mut table: Vec<Instrument> = Vec::new();
    let mut index: HashMap<String, usize> = HashMap::new();
    let mut wpos = Vec::new();
    for (instrument, position) in st.positions.iter() {
        let symbol = instrument.symbol()?;
        let quantity = position.quantity.format_f();
        let mark = st.marks.get(&instrument.hk()).map(|m| m.format_f());
        let flat = eq(&position.quantity, &zero())?;
        let (kind, mult, contract, avg_cost) = match instrument {
            Instrument::Combo(_) => (Kind::Combo, 1, None, None),
            Instrument::Option(c) => {
                let hk = instrument.hk();
                let at = match index.get(&hk) {
                    Some(&i) => i,
                    None => {
                        index.insert(hk, table.len());
                        table.push(instrument.clone());
                        table.len() - 1
                    }
                };
                (Kind::Option, c.multiplier as i64, Some(at), Some(position.avg_cost.format_f()))
            }
            Instrument::Equity(_) => (Kind::Equity, 1, None, None),
            Instrument::Future(_) => return err("unsupported", "futures not supported in margin calculation"),
        };
        wpos.push(WPos { symbol, kind, quantity, mark, multiplier: mult, flat, contract, avg_cost });
    }
    // `table.wire()`
    let mut wcon = Vec::new();
    for instrument in &table {
        let Instrument::Option(c) = instrument else { continue };
        let strike = c.strike.format_f();
        let occ = c.occ()?.trim().to_string();
        let style = match option_style(&c.underlying) {
            Ok(s) => Style::Resolved { underlying: s.underlying, index: s.settlement == Settlement::Cash },
            Err(e) if e.kind() == "unresolvable" => Style::Unresolved(e.message().to_string()),
            Err(e) => return Err(LErr { kind: e.kind(), msg: e.message().to_string() }),
        };
        wcon.push((c, strike, occ, style));
    }
    let mut equity_marks_w = Vec::new();
    for (instrument, mark) in st.marks.iter() {
        if let Instrument::Equity(symbol) = instrument {
            equity_marks_w.push((symbol.clone(), mark.format_f()));
        }
    }
    let cash = st.cash.format_f();
    // -- read back, as `wire::account_json` does
    let mut contracts = Vec::new();
    for (c, strike, occ, style) in wcon {
        if c.multiplier <= 0 {
            return err("value", format!("Multiplier must be positive, got {}", c.multiplier));
        }
        contracts.push(Contract {
            root: c.underlying.clone(),
            expiry: c.expiry,
            strike: mo_parse(&strike)?,
            right: match c.right {
                Right::Call => mo::Right::Call,
                Right::Put => mo::Right::Put,
            },
            multiplier: c.multiplier as i64,
            occ,
            style,
        });
    }
    let mut positions = Vec::new();
    for p in wpos {
        positions.push(mg::Position {
            symbol: p.symbol,
            kind: p.kind,
            quantity: mo_parse(&p.quantity)?,
            mark: p.mark.as_deref().map(mo_parse).transpose()?,
            multiplier: p.multiplier,
            flat: p.flat,
            contract: p.contract,
            avg_cost: p.avg_cost.as_deref().map(mo_parse).transpose()?,
        });
    }
    let mut equity_marks = HashMap::new();
    for (symbol, mark) in equity_marks_w {
        equity_marks.insert(symbol, mo_parse(&mark)?);
    }
    let definitions = mo::definitions();
    let m = mg::account_margin(&AccountInput {
        cash: mo_parse(&cash)?,
        positions: &positions,
        contracts: &contracts,
        equity_marks: &equity_marks,
        underlying_prices: &HashMap::new(),
        overrides: &HashMap::new(),
        definitions: &definitions,
        search_limit: mo::DEFAULT_SEARCH_LIMIT,
    })
    .map_err(merr)?;
    let (used, _) = mg::margin_summary(m.equity, m.margin_maintenance).map_err(merr)?;
    Ok(Margin {
        used: back(&used)?,
        strategies: m
            .strategies
            .iter()
            .map(|s| Ok((s.name.clone(), s.underlying.clone(), back(&s.maintenance)?, s.cash_secured.as_ref().map(back).transpose()?)))
            .collect::<R<Vec<_>>>()?,
        positions: m.positions.iter().map(|p| Ok((p.symbol.clone(), back(&p.requirement.maintenance)?))).collect::<R<Vec<_>>>()?,
    })
}

/// The `except (OptionMarginError, ValueError)` the margin rule caught.
fn caught(e: &LErr) -> bool {
    matches!(e.kind, "value" | "option")
}

// -- measurements ---------------------------------------------------------------------

/// `_entry_price`: the limit, else the snapshot's mid (a share purchase: the price the
/// account is marked at).
fn entry_price(intent: &Intent, ctx: &Context<'_>) -> R<Option<PyDec>> {
    if let Some(p) = &intent.limit_price {
        return Ok(Some(p.clone()));
    }
    let snapshot = ctx.snapshot;
    if let Instrument::Equity(_) = intent.instrument {
        if let (Some(snap), true) = (snapshot, ctx.live) {
            return Ok(Some(snap.underlying_price.clone()));
        }
        return Ok(ctx.state.marks.get(&intent.instrument.hk()).cloned());
    }
    let Some(snap) = snapshot else { return Ok(None) };
    let mut net = zero();
    for leg in legs_of(&intent.instrument, intent.side) {
        let Some(q) = snap.get(&leg.contract)? else { return Ok(None) };
        let mid = q.mid()?;
        let signed = if leg.side == Side::Sell { mid } else { neg(&mid)? };
        net = add(&net, &mul_i(&signed, leg.ratio)?)?;
    }
    let net = if intent.side == Side::Sell { net } else { neg(&net)? };
    Ok(if gt(&net, &zero())? { Some(net) } else { None })
}

/// `_max_loss`: the structure's worst case at expiry, None when it has no bound.
fn max_loss(intent: &Intent, price: &PyDec) -> R<Option<PyDec>> {
    let legs = legs_of(&intent.instrument, intent.side);
    let m = multiplier(&legs[0].contract)?;
    let units = &intent.quantity;
    if let Instrument::Equity(_) = intent.instrument {
        return Ok(Some(mul(price, units)?));
    }
    if legs.len() == 1 {
        let leg = &legs[0];
        if leg.side == Side::Buy {
            return Ok(Some(mul(&mul_i(price, m)?, units)?));
        }
        if right_of(&leg.contract)? == Right::Put {
            return Ok(Some(mul(&mul_i(&sub(&strike_of(&leg.contract)?, price)?, m)?, units)?));
        }
        return Ok(None);
    }
    if legs.len() == 2 {
        let (a, b) = (&legs[0], &legs[1]);
        let same = right_of(&a.contract)? == right_of(&b.contract)?
            && expiry_of(&a.contract)? == expiry_of(&b.contract)?
            && a.ratio == b.ratio
            && b.ratio == 1;
        if same && a.side != b.side {
            let width = abs(&sub(&strike_of(&a.contract)?, &strike_of(&b.contract)?)?)?;
            if intent.side == Side::Sell {
                return Ok(Some(mul(&mul_i(&sub(&width, price)?, m)?, units)?));
            }
            return Ok(Some(mul(&mul_i(price, m)?, units)?));
        }
    }
    Ok(None)
}

fn terminal(s: OrderState) -> bool {
    matches!(s, OrderState::Filled | OrderState::Cancelled | OrderState::Rejected | OrderState::Expired)
}

// -- the verdict ----------------------------------------------------------------------

struct Out(Vec<RuleResult>);

impl Out {
    fn record(&mut self, name: &str, passed: bool, measured: Val, threshold: Val, success: String, refusal: &str) {
        let reason = if passed { success } else { refusal.to_string() };
        self.0.push(RuleResult { name: name.into(), passed, measured, threshold, reason });
    }
    fn push(&mut self, name: &str, passed: bool, measured: &str, threshold: &str, reason: String) {
        self.0.push(RuleResult {
            name: name.into(),
            passed,
            measured: Val::S(measured.into()),
            threshold: Val::S(threshold.into()),
            reason,
        });
    }
    fn not_configured(&mut self, name: &str) {
        self.push(name, true, "n/a", "n/a", format!("{name} is not configured for this account"));
    }
}

/// Every rule but the ledger's two, in the order the Python recorded them.
pub fn evaluate(
    rules: &Rules,
    intent: &Intent,
    ctx: &Context<'_>,
    regime_of: &mut Regime<'_>,
    earnings_of: &mut Earnings<'_>,
) -> R<Vec<RuleResult>> {
    let mut out = Out(Vec::new());
    let st = ctx.state;
    let underlying = underlying_of(&intent.instrument)?;
    let price = entry_price(intent, ctx)?;
    let bk = book(intent, &price, ctx)?;
    let equity = bk.equity.clone();
    let mut book_error = bk.error.clone();

    // regime
    let regime = regime_of()?;
    let truthy = regime.as_deref().filter(|r| !r.is_empty());
    let mut allowed = rules.allowed_regimes.clone();
    allowed.sort();
    out.record(
        "regime",
        regime.as_ref().is_some_and(|r| rules.allowed_regimes.contains(r)),
        Val::S(truthy.unwrap_or("UNKNOWN").to_string()),
        Val::S(allowed.join(", ")),
        "Regime permits options entries".into(),
        "Regime is unknown or not one this account enters in",
    );

    // margin, measured on the whole book with the entry in it
    let (mut margin, mut previous) = (None, None);
    if let (Some(after), Some(before)) = (&bk.after, &bk.before) {
        let r = margin_of(after).and_then(|m| Ok((m, margin_of(before)?)));
        match r {
            Ok((m, p)) => {
                margin = Some(m);
                previous = Some(p);
            }
            Err(e) if caught(&e) => {
                margin = None;
                book_error = Some(e.msg);
            }
            Err(e) => return Err(e),
        }
    }
    let cap = cap_of(&equity, &rules.max_margin_frac)?;
    let reduces = match (&margin, &previous) {
        (Some(m), Some(p)) => le(&m.used, &p.used)?,
        _ => false,
    };
    let used = margin.as_ref().map(|m| m.used.clone());
    let within = opt_le(&used, &cap)?;
    let success = if used.is_none() || cap.is_none() || within {
        "Reg-T requirement with the entry is within the account cap".to_string()
    } else {
        format!("Reg-T requirement {} is over the cap but no higher than before", ds(used.as_ref().unwrap()))
    };
    out.record(
        "margin",
        used.is_some() && cap.is_some() && (within || reduces),
        match &used {
            Some(u) => Val::D(u.clone()),
            None => Val::S(format!("UNKNOWN ({})", book_error.as_deref().unwrap_or("None"))),
        },
        d_or(&cap, "UNKNOWN"),
        success,
        "Reg-T requirement is unknown or would exceed the account cap",
    );

    // margin on this underlying: its strategies and any of its shares margined alone
    match &rules.max_name_margin_frac {
        None => out.not_configured("name_margin"),
        Some(frac) => {
            let (mut on_name, mut was) = (None, None);
            if let (Some(m), Some(p)) = (&margin, &previous) {
                let measure = |m: &Margin| -> R<PyDec> {
                    let mut a = zero();
                    for (_, u, maint, _) in &m.strategies {
                        if *u == underlying {
                            a = add(&a, maint)?;
                        }
                    }
                    let mut b = zero();
                    for (sym, maint) in &m.positions {
                        if *sym == underlying {
                            b = add(&b, maint)?;
                        }
                    }
                    add(&a, &b)
                };
                on_name = Some(measure(m)?);
                was = Some(measure(p)?);
            }
            let limit = cap_of(&equity, frac)?;
            let passed = on_name.is_some() && limit.is_some() && (opt_le(&on_name, &limit)? || opt_le(&on_name, &was)?);
            out.record(
                "name_margin",
                passed,
                d_or(&on_name, "UNKNOWN"),
                d_or(&limit, "UNKNOWN"),
                format!("Margin on {underlying} is within the per-name cap"),
                &format!("Margin on {underlying} is unknown or over the per-name cap"),
            );
        }
    }

    // cash securing this underlying's strategies
    match &rules.max_name_collateral_frac {
        None => out.not_configured("name_collateral"),
        Some(frac) => {
            let mut secured = None;
            if let Some(m) = &margin {
                let mut total = zero();
                for (_, u, _, cash) in &m.strategies {
                    if let (true, Some(c)) = (*u == underlying, cash) {
                        total = add(&total, c)?;
                    }
                }
                secured = Some(total);
            }
            let limit = cap_of(&equity, frac)?;
            out.record(
                "name_collateral",
                secured.is_some() && limit.is_some() && opt_le(&secured, &limit)?,
                d_or(&secured, "UNKNOWN"),
                d_or(&limit, "UNKNOWN"),
                format!("Cash securing {underlying} is within the per-name cap"),
                &format!("Cash securing {underlying} is unknown or over the per-name cap"),
            );
        }
    }

    // naked put notional by regime
    match &rules.put_notional_frac_by_regime {
        None => out.not_configured("put_notional"),
        Some(by_regime) => {
            let fraction = match truthy {
                Some(r) => by_regime.iter().find(|(k, _)| k == r).map(|(_, v)| v.clone()),
                None => None,
            };
            let mut notional = None;
            if let Some(m) = &margin {
                let mut total = zero();
                for (name, _, _, cash) in &m.strategies {
                    if let (true, Some(c)) = (name == mo::NAKED_PUT, cash) {
                        total = add(&total, c)?;
                    }
                }
                notional = Some(total);
            }
            let limit = match &fraction {
                Some(f) => cap_of(&equity, f)?,
                None => None,
            };
            out.record(
                "put_notional",
                notional.is_some() && limit.is_some() && opt_le(&notional, &limit)?,
                d_or(&notional, "UNKNOWN"),
                match &limit {
                    Some(l) => Val::D(l.clone()),
                    None => Val::S(format!("UNKNOWN (no fraction for {})", truthy.unwrap_or("UNKNOWN"))),
                },
                "Naked put notional is within the regime's cap".into(),
                "Naked put notional is unknown or over the regime's cap",
            );
        }
    }

    // the structure's own worst case
    match &rules.max_loss_per_structure_frac {
        None => out.not_configured("max_loss"),
        Some(frac) => {
            let loss = match &price {
                Some(p) => max_loss(intent, p)?,
                None => None,
            };
            let limit = cap_of(&equity, frac)?;
            out.record(
                "max_loss",
                loss.is_some() && limit.is_some() && opt_le(&loss, &limit)?,
                d_or(&loss, "UNBOUNDED or UNKNOWN"),
                d_or(&limit, "UNKNOWN"),
                "The structure's maximum loss is within the per-structure cap".into(),
                "The structure's maximum loss is unbounded, unknown or over the cap",
            );
        }
    }

    // debit structures
    let debit = match &price {
        Some(p) if intent.side == Side::Buy && is_structure(&intent.instrument) => {
            let first = legs_of(&intent.instrument, intent.side);
            Some(mul_i(&mul(p, &intent.quantity)?, multiplier(&first[0].contract)?)?)
        }
        Some(_) => Some(zero()),
        None => None,
    };
    match &rules.max_debit_per_structure_frac {
        None => out.not_configured("debit"),
        Some(frac) => {
            let limit = cap_of(&equity, frac)?;
            out.record(
                "debit",
                debit.is_some() && limit.is_some() && opt_le(&debit, &limit)?,
                d_or(&debit, "UNKNOWN"),
                d_or(&limit, "UNKNOWN"),
                "The structure's debit is within the per-structure cap".into(),
                "The structure's debit is unknown or over the per-structure cap",
            );
        }
    }
    match &rules.max_total_debit_frac {
        None => out.not_configured("total_debit"),
        Some(frac) => {
            let mut held = zero();
            for s in open_structures(st)? {
                let Some(entry) = st.orders.get(&s.entry_order_id) else {
                    return err("key", format!("'{}'", s.entry_order_id));
                };
                if entry.side == Side::Sell {
                    continue; // a credit structure
                }
                let first = legs_of(&entry.instrument, entry.side);
                held = add(&held, &mul_i(&mul(&s.entry_price, &s.units)?, multiplier(&first[0].contract)?)?)?;
            }
            let total = match &debit {
                Some(d) => Some(add(&held, d)?),
                None => None,
            };
            let limit = cap_of(&equity, frac)?;
            out.record(
                "total_debit",
                total.is_some() && limit.is_some() && opt_le(&total, &limit)?,
                d_or(&total, "UNKNOWN"),
                d_or(&limit, "UNKNOWN"),
                "Debit structures together are within the account cap".into(),
                "Debit structures together are unknown or over the account cap",
            );
        }
    }

    // shares bought
    match &rules.max_share_notional_frac {
        None => out.not_configured("share_notional"),
        Some(frac) if matches!(intent.instrument, Instrument::Equity(_)) && intent.side == Side::Buy => {
            let notional = match &price {
                Some(p) => Some(mul(p, &intent.quantity)?),
                None => None,
            };
            let limit = cap_of(&equity, frac)?;
            out.record(
                "share_notional",
                notional.is_some() && limit.is_some() && opt_le(&notional, &limit)?,
                d_or(&notional, "UNKNOWN"),
                d_or(&limit, "UNKNOWN"),
                "Shares bought are within the position cap".into(),
                "Shares bought are unknown or over the position cap",
            );
        }
        Some(_) => out.push("share_notional", true, "n/a", "n/a", "No shares are bought".into()),
    }

    // earnings before a short leg expires
    let short_expiries: Vec<NaiveDate> = legs_of(&intent.instrument, intent.side)
        .iter()
        .filter_map(|l| match (&l.contract, l.side) {
            (Instrument::Option(c), Side::Sell) => Some(c.expiry),
            _ => None,
        })
        .collect();
    if !rules.no_earnings_before_expiry || !is_structure(&intent.instrument) {
        out.not_configured("earnings");
    } else if short_expiries.is_empty() {
        // A long option carries no short premium through the report (a PMCC's LEAPS).
        out.push("earnings", true, "n/a", "n/a", "The entry sells no option".into());
    } else {
        let expiry = *short_expiries.iter().max().unwrap_or(&short_expiries[0]);
        let (known, earnings) = match earnings_of()? {
            Some(e) => (true, e),
            None => (false, None),
        };
        out.record(
            "earnings",
            known && earnings.is_none_or(|d| d > expiry),
            Val::S(match earnings {
                Some(d) => d.format("%Y-%m-%d").to_string(),
                None if known => "none scheduled".into(),
                None => "UNKNOWN".into(),
            }),
            Val::S(format!("after {}", expiry.format("%Y-%m-%d"))),
            "No earnings before the structure expires".into(),
            "Earnings fall before the structure expires, or the date is unknown",
        );
    }

    // the quotes the entry is made on, against the gates of the scan that chose it
    match &rules.entry_quote {
        None => out.not_configured("entry_quote"),
        Some(gates) => entry_quote(intent, &underlying, ctx.snapshot, gates, &mut out)?,
    }

    // the OMS guards, recorded (C3, C4)
    if is_structure(&intent.instrument) {
        let legs = legs_of(&intent.instrument, intent.side);
        let mut wanted: Vec<&Instrument> = Vec::new();
        for leg in &legs {
            if !wanted.iter().any(|w| w.hk() == leg.contract.hk()) {
                wanted.push(&leg.contract);
            }
        }
        let mut busy: Vec<String> = st
            .positions
            .iter()
            .filter(|(c, p)| is_option(c) && !matches!(eq(&p.quantity, &zero()), Ok(true)))
            .map(|(c, _)| c.hk())
            .collect();
        for order in st.orders.values() {
            if order.parent_order_id.is_none() && !terminal(order.state) && is_structure(&order.instrument) {
                for leg in legs_of(&order.instrument, order.side) {
                    busy.push(leg.contract.hk());
                }
            }
        }
        let mut clash = Vec::new();
        for c in wanted {
            if busy.contains(&c.hk()) {
                clash.push(occ_strip(c)?);
            }
        }
        clash.sort();
        let shown = clash.join(", ");
        out.record(
            "duplicate_entry",
            clash.is_empty(),
            Val::S(if shown.is_empty() { "none".into() } else { shown }),
            Val::S("no contract already held or being entered".into()),
            "No contract of the entry is already held or being entered (C4)".into(),
            "A contract of the entry is already held or being entered (C4)",
        );
        let mut short_calls = false;
        for leg in &legs {
            if leg.side == Side::Sell && right_of(&leg.contract)? == Right::Call {
                short_calls = true;
                break;
            }
        }
        if short_calls {
            let (needed, shares) = match &bk.after {
                Some(after) => match uncovered_calls(after, true)?.into_iter().find(|(u, _, _)| *u == underlying) {
                    Some((_, n, s)) => (Some(n), Some(s)),
                    None => (Some(zero()), Some(zero())),
                },
                None => (None, None),
            };
            out.record(
                "covered_calls",
                needed.is_some() && opt_le(&needed, &shares)?,
                match &needed {
                    Some(n) => Val::D(n.clone()),
                    None => Val::S(format!("UNKNOWN ({})", book_error.as_deref().unwrap_or("None"))),
                },
                d_or(&shares, "UNKNOWN"),
                "Every short call is covered by shares or a later long call (C3)".into(),
                "A short call would be written on shares the account does not hold (C3)",
            );
        } else {
            out.push("covered_calls", true, "n/a", "n/a", "The entry writes no call".into());
        }
    }
    Ok(out.0)
}

// -- the entry's quotes ---------------------------------------------------------------

fn ratio(v: &Option<PyDec>) -> R<Option<PyDec>> {
    match v {
        Some(d) => Ok(Some(d.quantize(-4).map_err(derr)?)),
        None => Ok(None),
    }
}

fn each(values: &[(String, Option<String>)]) -> String {
    values
        .iter()
        .map(|(occ, v)| format!("{occ} {}", v.as_deref().unwrap_or("UNKNOWN")))
        .collect::<Vec<_>>()
        .join(", ")
}

fn shown(v: &Option<PyDec>) -> Option<String> {
    v.as_ref().map(ds)
}

/// `low <= d <= high`
fn between(d: &PyDec, low: &PyDec, high: &PyDec) -> R<bool> {
    Ok(le(low, d)? && le(d, high)?)
}

fn check(out: &mut Out, name: &str, passed: bool, measured: Val, threshold: String, what: &str) {
    out.0.push(RuleResult {
        name: format!("entry_quote.{name}"),
        passed,
        measured,
        threshold: Val::S(threshold),
        reason: if passed { what.to_string() } else { format!("Not so: {what}") },
    });
}

/// `all(pred(x) for x in xs)`, stopping at the first false (and so at the first refusal
/// past it, as the generator did).
fn all<T>(xs: &[T], mut pred: impl FnMut(&T) -> R<bool>) -> R<bool> {
    for x in xs {
        if !pred(x)? {
            return Ok(false);
        }
    }
    Ok(true)
}

fn entry_quote(
    intent: &Intent,
    underlying: &str,
    snapshot: Option<&Snapshot>,
    gates: &EntryQuote,
    out: &mut Out,
) -> R<()> {
    let legs = legs_of(&intent.instrument, intent.side);
    let shorts: Vec<&ComboLeg> = legs.iter().filter(|l| l.side == Side::Sell && is_option(&l.contract)).collect();
    let mut any_call = false;
    for l in &shorts {
        if right_of(&l.contract)? != Right::Put {
            any_call = true;
            break;
        }
    }
    if shorts.is_empty() || any_call {
        out.push("entry_quote", true, "n/a", "n/a", "The entry opens no short put".into());
        return Ok(());
    }
    let Some(snap) = snapshot else {
        out.push(
            "entry_quote",
            false,
            "UNKNOWN",
            &format!("a {underlying} snapshot"),
            format!("No {underlying} snapshot to check the entry's quotes on (I5)"),
        );
        return Ok(());
    };
    // `{leg.contract: snapshot.get(leg.contract) for leg in legs}`
    let mut quotes: Vec<(&Instrument, Option<&Quote>)> = Vec::new();
    for leg in &legs {
        let q = snap.get(&leg.contract)?;
        match quotes.iter_mut().find(|(c, _)| c.hk() == leg.contract.hk()) {
            Some(slot) => slot.1 = q,
            None => quotes.push((&leg.contract, q)),
        }
    }
    let mut missing = Vec::new();
    for (c, q) in &quotes {
        if q.is_none() {
            missing.push(occ_strip(c)?);
        }
    }
    missing.sort();
    if !missing.is_empty() {
        out.push(
            "entry_quote",
            false,
            &format!("no quote for {}", missing.join(", ")),
            "every leg quoted",
            format!("The {underlying} snapshot does not quote every leg (I5)"),
        );
        return Ok(());
    }
    let quote_of = |c: &Instrument| -> &Quote {
        quotes.iter().find(|(k, _)| k.hk() == c.hk()).and_then(|(_, q)| *q).expect("every leg quoted")
    };

    if let Some(min) = &gates.min_underlying_price {
        let price = &snap.underlying_price;
        check(
            out,
            "underlying_price",
            ge(price, min)?,
            Val::D(price.clone()),
            format!(">= {}", ds(min)),
            &format!("{underlying} trades at {} or above", ds(min)),
        );
    }
    let mut short: Vec<(String, &Instrument, &Quote)> = Vec::new();
    for leg in &shorts {
        short.push((occ_strip(&leg.contract)?, &leg.contract, quote_of(&leg.contract)));
    }
    if let Some((low, high)) = &gates.short_put_abs_delta {
        let mut deltas = Vec::new();
        for (o, _, q) in &short {
            deltas.push((o.clone(), match &q.delta { Some(d) => Some(abs(d)?), None => None }));
        }
        let passed = all(&deltas, |(_, d)| match d {
            Some(d) => between(d, low, high),
            None => Ok(false),
        })?;
        let shown_: Vec<_> = deltas.iter().map(|(o, d)| (o.clone(), shown(d))).collect();
        check(
            out,
            "delta",
            passed,
            Val::S(each(&shown_)),
            format!("{}..{}", ds(low), ds(high)),
            "Each short put's |delta| is inside the scan's range",
        );
    }
    if let Some(min) = &gates.min_short_bid {
        let passed = all(&short, |(_, _, q)| gt(&q.bid, min))?;
        let shown_: Vec<_> = short.iter().map(|(o, _, q)| (o.clone(), Some(ds(&q.bid)))).collect();
        check(out, "bid", passed, Val::S(each(&shown_)), format!("> {}", ds(min)), "Each short put bids above the scan's floor");
    }
    if let Some((low, high)) = &gates.short_bid_return {
        let mut returns = Vec::new();
        for (o, c, q) in &short {
            let strike = strike_of(c)?;
            returns.push((o.clone(), if gt(&strike, &zero())? { Some(div(&q.bid, &strike)?) } else { None }));
        }
        let passed = all(&returns, |(_, r)| match r {
            Some(r) => between(r, low, high),
            None => Ok(false),
        })?;
        let mut shown_ = Vec::new();
        for (o, r) in &returns {
            shown_.push((o.clone(), shown(&ratio(r)?)));
        }
        check(
            out,
            "bid_return",
            passed,
            Val::S(each(&shown_)),
            format!("{}..{}", ds(low), ds(high)),
            "Each short put's bid / strike is inside the scan's range",
        );
    }
    if let Some(min) = &gates.min_short_implied_vol {
        let vols: Vec<_> = short.iter().map(|(o, _, q)| (o.clone(), q.implied_vol.clone())).collect();
        let passed = all(&vols, |(_, v)| match v {
            Some(v) => ge(v, min),
            None => Ok(false),
        })?;
        let shown_: Vec<_> = vols.iter().map(|(o, v)| (o.clone(), shown(v))).collect();
        check(
            out,
            "implied_vol",
            passed,
            Val::S(each(&shown_)),
            format!(">= {}", ds(min)),
            "Each short put's implied vol is at the scan's floor or above",
        );
    }
    if let Some(min) = gates.min_open_interest {
        let interest: Vec<_> = short.iter().map(|(o, _, q)| (o.clone(), q.open_interest)).collect();
        let passed = interest.iter().all(|(_, i)| i.is_some_and(|i| i >= min));
        let shown_: Vec<_> = interest.iter().map(|(o, i)| (o.clone(), i.map(|i| i.to_string()))).collect();
        check(
            out,
            "open_interest",
            passed,
            Val::S(each(&shown_)),
            format!(">= {min}"),
            "Each short put's open interest is at the scan's floor or above",
        );
    }
    if let Some(max) = &gates.max_leg_spread_frac {
        let mut spreads = Vec::new();
        for (c, q) in &quotes {
            let q = q.expect("every leg quoted");
            let mid = q.mid()?;
            spreads.push((occ_strip(c)?, if gt(&mid, &zero())? { Some(div(&q.spread()?, &mid)?) } else { None }));
        }
        let passed = all(&spreads, |(_, f)| match f {
            Some(f) => le(f, max),
            None => Ok(false),
        })?;
        let mut shown_ = Vec::new();
        for (o, f) in &spreads {
            shown_.push((o.clone(), shown(&ratio(f)?)));
        }
        check(
            out,
            "leg_spread",
            passed,
            Val::S(each(&shown_)),
            format!("<= {}", ds(max)),
            "Each leg's bid/ask spread is within the scan's limit",
        );
    }
    let vertical = gates.min_credit_width_frac.is_some() || gates.min_credit_return.is_some() || gates.max_friction_frac.is_some();
    if matches!(intent.instrument, Instrument::Combo(_)) && vertical {
        let longs: Vec<&ComboLeg> = legs.iter().filter(|l| l.side == Side::Buy).collect();
        let not_vertical = shorts.len() != 1
            || longs.len() != 1
            || right_of(&longs[0].contract)? != Right::Put
            || expiry_of(&longs[0].contract)? != expiry_of(&shorts[0].contract)?
            || ge(&strike_of(&longs[0].contract)?, &strike_of(&shorts[0].contract)?)?;
        if not_vertical {
            out.push(
                "entry_quote.vertical",
                false,
                "not a bull put vertical",
                "one short put over one long put",
                "The credit gates measure a bull put vertical only".into(),
            );
            return Ok(());
        }
        let (sq, lq) = (quote_of(&shorts[0].contract), quote_of(&longs[0].contract));
        let width = sub(&strike_of(&shorts[0].contract)?, &strike_of(&longs[0].contract)?)?;
        let credit = sub(&sq.bid, &lq.ask)?;
        if let Some(min) = &gates.min_credit_width_frac {
            let frac = div(&credit, &width)?;
            let passed = ge(&frac, min)?;
            let r = ratio(&Some(frac))?;
            check(
                out,
                "credit_width",
                passed,
                Val::D(r.expect("a ratio")),
                format!(">= {}", ds(min)),
                "The credit is a large enough part of the width",
            );
        }
        if let Some(min) = &gates.min_credit_return {
            let ret = if lt(&zero(), &credit)? && lt(&credit, &width)? { Some(div(&credit, &sub(&width, &credit)?)?) } else { None };
            let passed = match &ret {
                Some(r) => ge(r, min)?,
                None => false,
            };
            check(
                out,
                "credit_return",
                passed,
                match ratio(&ret)? {
                    Some(r) => Val::D(r),
                    None => Val::S("UNKNOWN".into()),
                },
                format!(">= {}", ds(min)),
                "The credit returns enough on the width at risk",
            );
        }
        if let Some(max) = &gates.max_friction_frac {
            let friction =
                if gt(&credit, &zero())? { Some(div(&add(&sq.spread()?, &lq.spread()?)?, &credit)?) } else { None };
            let passed = match &friction {
                Some(f) => le(f, max)?,
                None => false,
            };
            check(
                out,
                "friction",
                passed,
                match ratio(&friction)? {
                    Some(r) => Val::D(r),
                    None => Val::S("UNKNOWN".into()),
                },
                format!("<= {}", ds(max)),
                "The legs' spreads cost little enough of the credit",
            );
        }
    }
    Ok(())
}

//! The sim's exits, mirrored at a venue at each in-session pass (was `tos_paper/exits.py`,
//! P5-T7): `plan_exits` and what it is built from. Pure: the sim's accounts and the venue's mirror
//! cross as the ledger codec's canonical trees (`bridge::uncanon_account`, `uncanon_mirror`), the
//! host's `price` callback as a table of the (contract, side) quotes the pass has, and the clock as
//! the `at` text. `run_pass_mirror` (it drives a broker) stays Python until T9/T10.

use super::netting::vertical_reason;
use super::wire::{
    instrument, jstr, linstr, linstr_json, obj, req, req_arr, req_str, side_of, wire, ComboLeg,
};
use super::{EXIT_PLAN, VALUE};
use crate::ledger::bridge::{uncanon_account, uncanon_mirror};
use crate::ledger::codec::py_repr;
use crate::ledger::fold::AccountState;
use crate::ledger::json::Json;
use crate::ledger::mirror::{book_hk, ticket_contracts, MirrorState, MirrorTicketState};
use crate::ledger::model::{
    parse_datetime, Instrument as LInstrument, LErr, Order, OrderState, OrderType, Side, Tif, R,
};
use crate::ledger::ops::{div_i, eq, gt, le, lt, mul, s, sub, zero, OMap};
use crate::ledger::pydec::PyDec;

/// `eod.runner.PASSES`.
pub const PASSES: [&str; 3] = ["morning", "midday", "late"];
pub const FOLLOW_PREFIX: &str = "follow-";

fn working(state: OrderState) -> bool {
    matches!(state, OrderState::Submitted | OrderState::Accepted | OrderState::PartiallyFilled)
}

fn refuse<T>(kind: &'static str, msg: impl Into<String>) -> R<T> {
    Err(LErr { kind, msg: msg.into() })
}

/// `Decimal.copy_abs()`: the sign cleared, nothing rounded.
pub fn copy_abs(d: &PyDec) -> PyDec {
    if d.is_finite() {
        PyDec::from_parts(false, d.coefficient().clone(), d.exponent())
    } else {
        d.clone()
    }
}

/// `min(a, b)`: `a` unless `b` is strictly smaller.
fn min_of(a: &PyDec, b: &PyDec) -> R<PyDec> {
    Ok(if lt(b, a)? { b.clone() } else { a.clone() })
}

/// `max(a, b)`: `a` unless `b` is strictly larger.
fn max_of(a: &PyDec, b: &PyDec) -> R<PyDec> {
    Ok(if gt(b, a)? { b.clone() } else { a.clone() })
}

/// `close_id`.
pub fn close_id(account: &str, contract: &LInstrument, session: &str, name: &str) -> R<String> {
    Ok(format!("exit:{account}:{}:{session}:{name}", contract.symbol()?))
}

/// `target_id`.
pub fn target_id(target_order_id: &str, session: &str) -> String {
    format!("{target_order_id}@{session}")
}

/// `_refusal_id`.
pub fn refusal_id(account: &str, contract: &LInstrument, session: &str) -> R<String> {
    Ok(format!("exit:{account}:{}:{session}", contract.symbol()?))
}

/// An instrument of the ledger model, read as the wire form `vertical_reason` takes.
fn wire_legs(legs: &[crate::ledger::model::ComboLeg]) -> R<Vec<ComboLeg>> {
    match instrument(&linstr_json(&LInstrument::Combo(legs.to_vec())))? {
        super::wire::Instrument::Combo(l) => Ok(l),
        _ => wire("a combo is a combo"),
    }
}

/// The order an exit sends: `Order`'s own construction refusals, in its order.
#[derive(Debug, Clone)]
pub struct XOrder {
    pub order_id: String,
    pub account_id: String,
    pub instrument: LInstrument,
    pub side: Side,
    pub quantity: PyDec,
    pub limit_price: Option<PyDec>,
}

/// What the inputs of one planning share: the sim's accounts, the venue's mirror, the pass's quotes.
pub struct Ctx {
    pub mirror: MirrorState,
    pub accounts: OMap<String, AccountState>,
    pub empty: AccountState,
    pub mirrored: Vec<String>,
    pub session: String,
    pub prices: Vec<(String, Side, Option<PyDec>)>,
    pub at: String,
    pub aware: bool,
}

impl Ctx {
    /// `ledger.state(account)`.
    pub fn state(&self, account: &str) -> &AccountState {
        self.accounts.get(account).unwrap_or(&self.empty)
    }
}

#[derive(Debug, Default)]
pub struct Exit {
    pub cancel: Vec<String>,
    pub orders: Vec<XOrder>,
    pub refused: Vec<(String, String, String)>,
    pub waits: Vec<(String, String)>,
    /// Every `price(contract, side)` call, in order.
    pub priced: Vec<(LInstrument, Side)>,
}

pub struct Planner<'a> {
    pub ctx: &'a Ctx,
    pub out: Exit,
}

/// `_sim_orders`: the sim orders a venue ticket was sent for.
pub fn sim_orders<'a>(state: &'a AccountState, ticket: &MirrorTicketState, session: &str) -> Vec<&'a Order> {
    let suffix = format!("@{session}");
    let mut found = Vec::new();
    for allocation in &ticket.queued.allocations {
        let oid = &allocation.strategy_order_id;
        let order = state.orders.get(oid).or_else(|| {
            oid.strip_suffix(suffix.as_str()).and_then(|stem| state.orders.get(stem))
        });
        if let Some(o) = order {
            found.push(o);
        }
    }
    found
}

/// `_open_on`: the open tickets of `account` that put `contract` on the venue.
pub fn open_on<'a>(mirror: &'a MirrorState, account: &str, contract: &LInstrument) -> R<Vec<&'a MirrorTicketState>> {
    let mut out = Vec::new();
    for ticket in mirror.open_tickets()? {
        if ticket.queued.allocations.iter().any(|a| a.strategy_account == account)
            && ticket_contracts(&ticket.queued, &ticket.queued.quantity)?.contains(&contract.hk())
        {
            out.push(ticket);
        }
    }
    Ok(out)
}

/// `_in_vertical`.
pub fn in_vertical(mirror: &MirrorState, account: &str, contract: &LInstrument) -> R<bool> {
    for ticket in mirror.tickets.values() {
        if matches!(ticket.queued.instrument, LInstrument::Combo(_))
            && ticket.queued.allocations.iter().any(|a| a.strategy_account == account)
            && ticket_contracts(&ticket.queued, &ticket.queued.quantity)?.contains(&contract.hk())
        {
            return Ok(true);
        }
    }
    Ok(false)
}

/// `_flip`: the combo that closes `legs`.
pub fn flip(legs: &[crate::ledger::model::ComboLeg]) -> Vec<crate::ledger::model::ComboLeg> {
    legs.iter()
        .map(|l| crate::ledger::model::ComboLeg {
            contract: l.contract.clone(),
            ratio: l.ratio,
            side: if l.side == Side::Buy { Side::Sell } else { Side::Buy },
        })
        .collect()
}

/// `_units`: the spreads `held` (signed contracts) amounts to; `None` when the legs disagree.
pub fn units(held: &OMap<LInstrument, PyDec>, legs: &[crate::ledger::model::ComboLeg]) -> R<Option<PyDec>> {
    let mut per_leg: Vec<PyDec> = Vec::new();
    for leg in legs {
        let have = held.get(&leg.contract.hk()).cloned().unwrap_or_else(zero);
        let sign: i128 = if leg.side == Side::Buy { 1 } else { -1 };
        let one = div_i(&have, sign * leg.ratio)?;
        let mut seen = false;
        for p in &per_leg {
            if eq(p, &one)? {
                seen = true;
                break;
            }
        }
        if !seen {
            per_leg.push(one);
        }
    }
    Ok(if per_leg.len() == 1 { per_leg.pop() } else { None })
}

impl<'a> Planner<'a> {
    pub fn new(ctx: &'a Ctx) -> Planner<'a> {
        Planner { ctx, out: Exit::default() }
    }

    /// The host's `price(contract, side)`, from the pass's table (a quote it has none for is `None`).
    fn price(&mut self, contract: &LInstrument, side: Side) -> Option<PyDec> {
        self.out.priced.push((contract.clone(), side));
        let hk = contract.hk();
        self.ctx.prices.iter().find(|(k, sd, _)| *k == hk && *sd == side).and_then(|(_, _, p)| p.clone())
    }

    /// `Order(...)` as a LIMIT DAY order of the pass: its constructor's refusals.
    fn order(
        &self,
        order_id: String,
        account: &str,
        instrument: &LInstrument,
        side: Side,
        quantity: PyDec,
        limit: Option<PyDec>,
    ) -> R<XOrder> {
        if order_id.is_empty() {
            return refuse(VALUE, "order_id must be non-empty");
        }
        if account.is_empty() {
            return refuse(VALUE, "account_id must be non-empty");
        }
        if le(&quantity, &zero())? {
            return refuse(VALUE, format!("Order quantity must be positive, got {} (I5)", s(&quantity)));
        }
        if !self.ctx.aware {
            return refuse(VALUE, "Order created_at must be timezone-aware UTC datetime (I7)");
        }
        if limit.is_none() {
            return refuse(VALUE, "LIMIT order must have a limit_price");
        }
        Ok(XOrder {
            order_id,
            account_id: account.to_string(),
            instrument: instrument.clone(),
            side,
            quantity,
            limit_price: limit,
        })
    }

    /// Rest the sim's working GTC limit children on `instrument` as DAY tickets, up to `room`.
    fn rest_targets(
        &mut self,
        account: &str,
        instrument: &LInstrument,
        side: Side,
        mut room: PyDec,
    ) -> R<()> {
        let ctx = self.ctx;
        let state = ctx.state(account);
        let mut targets: Vec<&Order> = state.orders.values().collect();
        targets.sort_by(|a, b| a.order_id.cmp(&b.order_id));
        for target in targets {
            if le(&room, &zero())? {
                break;
            }
            if !(target.parent_order_id.is_some()
                && working(target.state)
                && target.instrument.same(instrument)
                && target.side == side
                && target.order_type == OrderType::Limit
                && target.tif == Tif::Gtc)
            {
                continue;
            }
            let oid = target_id(&target.order_id, &ctx.session);
            let filled = state.filled_quantity.get(&target.order_id).cloned().unwrap_or_else(zero);
            let quantity = min_of(&room, &sub(&target.quantity, &filled)?)?;
            room = sub(&room, &quantity)?;
            if le(&quantity, &zero())? || ctx.mirror.handled(&oid) {
                continue;
            }
            let order = self.order(oid, account, instrument, side, quantity, target.limit_price.clone())?;
            self.out.orders.push(order);
        }
        Ok(())
    }

    fn shared_with(resting: &[&MirrorTicketState], account: &str) -> Vec<String> {
        let mut shared: Vec<String> = Vec::new();
        for t in resting {
            for a in &t.queued.allocations {
                if a.strategy_account != account && !shared.contains(&a.strategy_account) {
                    shared.push(a.strategy_account.clone());
                }
            }
        }
        shared.sort();
        shared
    }

    /// `_verticals`: (account, the entry's combo) and every ticket on its two contracts.
    pub fn verticals(&self) -> R<Vec<(String, Vec<crate::ledger::model::ComboLeg>, Vec<&'a MirrorTicketState>)>> {
        let ctx = self.ctx;
        let mut found: OMap<(String, Vec<crate::ledger::model::ComboLeg>), ()> = OMap::new();
        for ticket in ctx.mirror.tickets.values() {
            let LInstrument::Combo(legs) = &ticket.queued.instrument else { continue };
            if vertical_reason(&wire_legs(legs)?)?.is_some() {
                continue;
            }
            for allocation in &ticket.queued.allocations {
                let account = &allocation.strategy_account;
                if !ctx.mirrored.contains(account) {
                    continue;
                }
                let order = ctx.state(account).orders.get(&allocation.strategy_order_id);
                if order.is_some_and(|o| o.parent_order_id.is_none()) {
                    let hk = format!("{}:{}{}", account.len(), account, ticket.queued.instrument.hk());
                    if !found.contains(&hk) {
                        found.insert(hk, (account.clone(), legs.clone()), ());
                    }
                }
            }
        }
        let mut out = Vec::new();
        for ((account, legs), _) in found.iter() {
            let mut pair: Vec<String> = legs.iter().map(|l| l.contract.hk()).collect();
            pair.sort();
            pair.dedup();
            let mut tickets = Vec::new();
            for t in ctx.mirror.tickets.values() {
                let LInstrument::Combo(tl) = &t.queued.instrument else { continue };
                let mut theirs: Vec<String> = tl.iter().map(|l| l.contract.hk()).collect();
                theirs.sort();
                theirs.dedup();
                if theirs == pair && t.queued.allocations.iter().any(|a| a.strategy_account == *account) {
                    tickets.push(t);
                }
            }
            out.push((account.clone(), legs.clone(), tickets));
        }
        Ok(out)
    }

    /// `_plan_verticals`: each vertical the venue holds or rests, as one structure. Returns the
    /// (account, contract) pairs it covered.
    pub fn plan_verticals(&mut self, name: &str) -> R<Vec<(String, LInstrument)>> {
        let ctx = self.ctx;
        let mut covered: Vec<(String, LInstrument)> = Vec::new();
        let mut verticals = self.verticals()?;
        let mut keyed = Vec::new();
        for v in verticals.drain(..) {
            let symbol = LInstrument::Combo(v.1.clone()).symbol()?;
            keyed.push((v.0.clone(), symbol, v));
        }
        keyed.sort_by(|a, b| (&a.0, &a.1).cmp(&(&b.0, &b.1)));
        for (_, _, (account, opening, tickets)) in keyed {
            let opening_i = LInstrument::Combo(opening.clone());
            let contracts: Vec<LInstrument> = opening.iter().map(|l| l.contract.clone()).collect();
            for c in &contracts {
                covered.push((account.clone(), c.clone()));
            }
            let state = ctx.state(&account);
            let closing_legs = flip(&opening);
            let closing_combo = LInstrument::Combo(closing_legs.clone());
            let mut venue_held: OMap<LInstrument, PyDec> = OMap::new();
            let mut sim_held: OMap<LInstrument, PyDec> = OMap::new();
            for c in &contracts {
                let v = ctx.mirror.book.get(&book_hk(&account, c)).cloned().unwrap_or_else(zero);
                venue_held.insert(c.hk(), c.clone(), v);
                let p = state.positions.get(&c.hk()).map(|p| p.quantity.clone()).unwrap_or_else(zero);
                sim_held.insert(c.hk(), c.clone(), p);
            }
            let venue = units(&venue_held, &opening)?;
            let sim = units(&sim_held, &opening)?;
            let refusal = refusal_id(&account, &opening_i, &ctx.session)?;
            let resting: Vec<&MirrorTicketState> = {
                let mut r = Vec::new();
                for t in &tickets {
                    if !t.terminal()? {
                        r.push(*t);
                    }
                }
                r
            };
            let (Some(venue), Some(sim)) = (venue, sim) else {
                if !ctx.mirror.handled(&refusal) {
                    let side = if units(&venue_held, &opening)?.is_none() { "venue" } else { "sim" };
                    self.out.refused.push((
                        refusal,
                        account.clone(),
                        format!(
                            "the {side} holds the two legs of the vertical {} in different amounts (a legged book); close it at the venue by hand",
                            opening_i.symbol()?
                        ),
                    ));
                }
                continue;
            };
            let closing = sub(&venue, &max_of(&sim, &zero())?)?;
            if gt(&closing, &zero())? {
                let oid = close_id(&account, &closing_combo, &ctx.session, name)?;
                if ctx.mirror.handled(&oid) {
                    continue;
                }
                let shared = Self::shared_with(&resting, &account);
                let limit = self.price(&closing_combo, Side::Buy);
                let reason = if !shared.is_empty() {
                    Some(format!(
                        "the venue must close {} of {}, but a resting ticket on it is shared with {}; cancel it at the venue by hand",
                        s(&closing),
                        opening_i.symbol()?,
                        shared.join(", ")
                    ))
                } else {
                    match &limit {
                        Some(l) if !le(l, &zero())? => None,
                        _ => Some(format!(
                            "the venue must close {} of {} and this pass has no price for it",
                            s(&closing),
                            opening_i.symbol()?
                        )),
                    }
                };
                if let Some(reason) = reason {
                    if !ctx.mirror.handled(&refusal) {
                        self.out.refused.push((refusal, account.clone(), reason));
                    }
                    continue;
                }
                for t in &resting {
                    self.out.cancel.push(t.queued.ticket_key.clone());
                    self.out.waits.push((t.queued.ticket_key.clone(), oid.clone()));
                }
                let order = self.order(oid, &account, &closing_combo, Side::Buy, closing, limit)?;
                self.out.orders.push(order);
                continue;
            }
            if le(&sim, &zero())? {
                for t in &resting {
                    let works = sim_orders(state, t, &ctx.session).iter().any(|o| working(o.state));
                    if t.queued.instrument.same(&opening_i) && !works {
                        self.out.cancel.push(t.queued.ticket_key.clone());
                    }
                }
                continue;
            }
            for t in &resting {
                let sims = sim_orders(state, t, &ctx.session);
                if sims.is_empty() || sims.iter().any(|o| working(o.state)) {
                    continue;
                }
                let mut filled = false;
                for o in &sims {
                    let f = state.filled_quantity.get(&o.order_id).cloned().unwrap_or_else(zero);
                    if gt(&f, &zero())? {
                        filled = true;
                        break;
                    }
                }
                if t.queued.instrument.same(&opening_i) && filled {
                    continue;
                }
                self.out.cancel.push(t.queued.ticket_key.clone());
            }
            if le(&venue, &zero())? || resting.iter().any(|t| t.queued.instrument.same(&closing_combo)) {
                continue;
            }
            self.rest_targets(&account, &closing_combo, Side::Buy, venue)?;
        }
        Ok(covered)
    }

    /// `plan_exits` after the pass name is accepted.
    pub fn plan(&mut self, name: &str) -> R<()> {
        let ctx = self.ctx;
        let covered = self.plan_verticals(name)?;
        let mut items: Vec<(&String, String, &LInstrument, &PyDec)> = Vec::new();
        for ((account, contract), held) in ctx.mirror.book.iter() {
            items.push((account, contract.symbol()?, contract, held));
        }
        items.sort_by(|a, b| (a.0, &a.1).cmp(&(b.0, &b.1)));
        for (account, _, contract, venue_held) in items {
            if !ctx.mirrored.contains(account) || matches!(contract, LInstrument::Combo(_)) {
                continue;
            }
            if covered.iter().any(|(a, c)| a == account && c.same(contract)) {
                continue;
            }
            let state = ctx.state(account);
            let sim_held = state.positions.get(&contract.hk()).map(|p| p.quantity.clone()).unwrap_or_else(zero);
            let same_side = gt(&mul(&sim_held, venue_held)?, &zero())?;
            let closing = sub(&copy_abs(venue_held), &if same_side { copy_abs(&sim_held) } else { zero() })?;
            let side = if lt(venue_held, &zero())? { Side::Buy } else { Side::Sell };
            let refusal = refusal_id(account, contract, &ctx.session)?;
            if le(&closing, &zero())? {
                if open_on(&ctx.mirror, account, contract)?.iter().any(|t| t.queued.side == side) {
                    continue;
                }
                self.rest_targets(account, contract, side, copy_abs(venue_held))?;
                continue;
            }
            let oid = close_id(account, contract, &ctx.session, name)?;
            if ctx.mirror.handled(&oid) {
                continue;
            }
            let refuse_with = |this: &mut Self, reason: String| {
                if !ctx.mirror.handled(&refusal) {
                    this.out.refused.push((refusal.clone(), account.clone(), reason));
                }
            };
            if in_vertical(&ctx.mirror, account, contract)? {
                refuse_with(
                    self,
                    format!(
                        "the sim holds {} of {} and the venue {}: a leg of a vertical no sim entry accounts for, never closed leg by leg; close it at the venue by hand",
                        s(&sim_held),
                        contract.symbol()?,
                        s(venue_held)
                    ),
                );
                continue;
            }
            let resting = open_on(&ctx.mirror, account, contract)?;
            let shared = Self::shared_with(&resting, account);
            if !shared.is_empty() {
                refuse_with(
                    self,
                    format!(
                        "the venue must close {} of {}, but a resting ticket on it is shared with {}; cancel it at the venue by hand",
                        s(&closing),
                        contract.symbol()?,
                        shared.join(", ")
                    ),
                );
                continue;
            }
            let limit = self.price(contract, side);
            let priced = match &limit {
                Some(l) => !le(l, &zero())?,
                None => false,
            };
            if !priced {
                refuse_with(
                    self,
                    format!(
                        "the venue must close {} of {} and this pass has no price for it",
                        s(&closing),
                        contract.symbol()?
                    ),
                );
                continue;
            }
            for t in &resting {
                self.out.cancel.push(t.queued.ticket_key.clone());
                self.out.waits.push((t.queued.ticket_key.clone(), oid.clone()));
            }
            let order = self.order(oid, account, contract, side, closing, limit)?;
            self.out.orders.push(order);
        }
        Ok(())
    }
}

/// `plan_exits`.
pub fn plan_exits(ctx: &Ctx, name: &str) -> R<Exit> {
    let named = name.starts_with(FOLLOW_PREFIX) && name.chars().count() > FOLLOW_PREFIX.chars().count();
    if !PASSES.contains(&name) && !named {
        return refuse(
            EXIT_PLAN,
            format!(
                "Unknown pass {}; the passes are {}, or {FOLLOW_PREFIX}<when> for a follower",
                py_repr(name),
                PASSES.join(", ")
            ),
        );
    }
    let mut planner = Planner::new(ctx);
    planner.plan(name)?;
    Ok(planner.out)
}

// -- the door ---------------------------------------------------------------------------------------

pub fn ctx_of(doc: &Json) -> R<Ctx> {
    let mut accounts = OMap::new();
    for row in req_arr(doc, "accounts")? {
        let Json::Arr(r) = row else { return wire("an account row is not a pair") };
        let [Json::Str(name), tree] = r.as_slice() else { return wire("an account row is not a pair") };
        accounts.insert(name.clone(), name.clone(), uncanon_account(tree)?);
    }
    let mut prices = Vec::new();
    for row in req_arr(doc, "prices")? {
        let Json::Arr(r) = row else { return wire("a price row is not a triple") };
        let [inst, Json::Str(sd), p] = r.as_slice() else { return wire("a price row is not a triple") };
        let p = match p {
            Json::Null => None,
            Json::Str(t) => Some(super::wire::dec_of(t)?),
            _ => return wire("a price is not text"),
        };
        prices.push((linstr(inst)?.hk(), side_of(sd)?, p));
    }
    let mut mirrored = Vec::new();
    for m in req_arr(doc, "mirrored")? {
        let Json::Str(t) = m else { return wire("a mirrored account is not text") };
        mirrored.push(t.clone());
    }
    let at = req_str(doc, "at")?.to_string();
    let (_, aware) = parse_datetime(&at)?;
    Ok(Ctx {
        mirror: uncanon_mirror(req(doc, "mirror")?)?,
        accounts,
        empty: AccountState::new(""),
        mirrored,
        session: req_str(doc, "session")?.to_string(),
        prices,
        at,
        aware,
    })
}

fn order_json(o: &XOrder, at: &str) -> Json {
    obj(vec![
        ("order_id", jstr(o.order_id.clone())),
        ("account_id", jstr(o.account_id.clone())),
        ("instrument", linstr_json(&o.instrument)),
        ("order_type", jstr(OrderType::Limit.value())),
        ("side", jstr(o.side.value())),
        ("quantity", jstr(s(&o.quantity))),
        ("command_id", jstr(o.order_id.clone())),
        ("created_at", jstr(at)),
        ("limit_price", super::wire::jopt_dec(&o.limit_price)),
        ("tif", jstr(Tif::Day.value())),
    ])
}

fn pair(a: &str, b: &str) -> Json {
    Json::Arr(vec![jstr(a), jstr(b)])
}

pub fn exit_json(e: &Exit, at: &str) -> Json {
    obj(vec![
        ("cancel", Json::Arr(e.cancel.iter().map(|k| jstr(k.clone())).collect())),
        ("orders", Json::Arr(e.orders.iter().map(|o| order_json(o, at)).collect())),
        (
            "refused",
            Json::Arr(
                e.refused
                    .iter()
                    .map(|(o, a, r)| Json::Arr(vec![jstr(o.clone()), jstr(a.clone()), jstr(r.clone())]))
                    .collect(),
            ),
        ),
        ("waits", Json::Arr(e.waits.iter().map(|(k, o)| pair(k, o)).collect())),
        (
            "priced",
            Json::Arr(
                e.priced.iter().map(|(i, sd)| Json::Arr(vec![linstr_json(i), jstr(sd.value())])).collect(),
            ),
        ),
    ])
}

pub fn plan_exits_op(doc: &Json) -> R<Json> {
    let ctx = ctx_of(doc)?;
    let out = plan_exits(&ctx, req_str(doc, "name")?)?;
    Ok(exit_json(&out, &ctx.at))
}

pub fn plan_verticals_op(doc: &Json) -> R<Json> {
    let ctx = ctx_of(doc)?;
    let mut planner = Planner::new(&ctx);
    let covered = planner.plan_verticals(req_str(doc, "name")?)?;
    let mut out = exit_json(&planner.out, &ctx.at);
    if let Json::Obj(fields) = &mut out {
        fields.push((
            "covered".to_string(),
            Json::Arr(covered.iter().map(|(a, c)| Json::Arr(vec![jstr(a.clone()), linstr_json(c)])).collect()),
        ));
    }
    Ok(out)
}

pub fn verticals_op(doc: &Json) -> R<Json> {
    let ctx = ctx_of(doc)?;
    let planner = Planner::new(&ctx);
    let mut rows = Vec::new();
    for (account, legs, tickets) in planner.verticals()? {
        rows.push(Json::Arr(vec![
            jstr(account),
            linstr_json(&LInstrument::Combo(legs)),
            Json::Arr(tickets.iter().map(|t| jstr(t.queued.ticket_key.clone())).collect()),
        ]));
    }
    Ok(Json::Arr(rows))
}

pub fn ids_op(doc: &Json) -> R<Json> {
    let session = req_str(doc, "session")?;
    let id = match req_str(doc, "which")? {
        "close" => close_id(req_str(doc, "account")?, &linstr(req(doc, "instrument")?)?, session, req_str(doc, "name")?)?,
        "target" => target_id(req_str(doc, "order_id")?, session),
        _ => refusal_id(req_str(doc, "account")?, &linstr(req(doc, "instrument")?)?, session)?,
    };
    Ok(obj(vec![("id", jstr(id))]))
}

fn held_of(doc: &Json) -> R<OMap<LInstrument, PyDec>> {
    let mut held = OMap::new();
    for row in req_arr(doc, "held")? {
        let Json::Arr(r) = row else { return wire("a held row is not a pair") };
        let [inst, Json::Str(q)] = r.as_slice() else { return wire("a held row is not a pair") };
        let i = linstr(inst)?;
        held.insert(i.hk(), i, super::wire::dec_of(q)?);
    }
    Ok(held)
}

pub fn units_op(doc: &Json) -> R<Json> {
    let LInstrument::Combo(legs) = linstr(req(doc, "combo")?)? else { return wire("units wants a combo") };
    let out = units(&held_of(doc)?, &legs)?;
    Ok(obj(vec![("units", out.map_or(Json::Null, |u| jstr(s(&u))))]))
}

pub fn flip_op(doc: &Json) -> R<Json> {
    let LInstrument::Combo(legs) = linstr(req(doc, "combo")?)? else { return wire("flip wants a combo") };
    Ok(linstr_json(&LInstrument::Combo(flip(&legs))))
}

pub fn open_on_op(doc: &Json) -> R<Json> {
    let ctx = ctx_of(doc)?;
    let contract = linstr(req(doc, "contract")?)?;
    let account = req_str(doc, "account")?;
    let (open, vertical) = if req_str(doc, "which")? == "open_on" {
        (open_on(&ctx.mirror, account, &contract)?, false)
    } else {
        (Vec::new(), in_vertical(&ctx.mirror, account, &contract)?)
    };
    Ok(obj(vec![
        ("keys", Json::Arr(open.iter().map(|t| jstr(t.queued.ticket_key.clone())).collect())),
        ("in_vertical", Json::Bool(vertical)),
    ]))
}

pub fn sim_orders_op(doc: &Json) -> R<Json> {
    let ctx = ctx_of(doc)?;
    let key = req_str(doc, "ticket")?;
    let Some(ticket) = ctx.mirror.tickets.get(key) else { return wire("no such ticket") };
    let found = sim_orders(ctx.state(req_str(doc, "account")?), ticket, &ctx.session);
    Ok(Json::Arr(found.iter().map(|o| jstr(o.order_id.clone())).collect()))
}


#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_ids_name_the_account_contract_session_and_pass() {
        let spy = LInstrument::Equity("AAPL".into());
        assert_eq!(close_id("A", &spy, "2026-10-01", "midday").unwrap(), "exit:A:AAPL:2026-10-01:midday");
        assert_eq!(target_id("t-1", "2026-10-01"), "t-1@2026-10-01");
        assert_eq!(refusal_id("A", &spy, "2026-10-01").unwrap(), "exit:A:AAPL:2026-10-01");
    }

    #[test]
    fn an_unknown_pass_refuses_and_a_bare_follow_prefix_too() {
        let ctx = Ctx {
            mirror: MirrorState::default(),
            accounts: OMap::new(),
            empty: AccountState::new(""),
            mirrored: vec![],
            session: "2026-10-01".into(),
            prices: vec![],
            at: "2026-10-01T16:00:00+00:00".into(),
            aware: true,
        };
        assert!(plan_exits(&ctx, "noon").unwrap_err().msg.starts_with("Unknown pass 'noon'; the passes are morning, midday, late"));
        assert!(plan_exits(&ctx, "follow-").is_err());
        assert!(plan_exits(&ctx, "follow-1235").is_ok());
        assert!(plan_exits(&ctx, "late").is_ok());
    }

    #[test]
    fn the_spreads_a_book_amounts_to_and_a_legged_book_has_none() {
        let leg = |strike: &str, side: Side| crate::ledger::model::ComboLeg {
            contract: LInstrument::Option(crate::ledger::model::OptionContract {
                underlying: "AAPL".into(),
                expiry: chrono::NaiveDate::from_ymd_opt(2026, 10, 16).unwrap(),
                strike: PyDec::parse(strike).unwrap(),
                right: crate::options::Right::Put,
                multiplier: 100,
            }),
            ratio: 1,
            side,
        };
        let legs = vec![leg("200", Side::Sell), leg("195", Side::Buy)];
        let mut held = OMap::new();
        held.insert(legs[0].contract.hk(), legs[0].contract.clone(), PyDec::parse("-2").unwrap());
        held.insert(legs[1].contract.hk(), legs[1].contract.clone(), PyDec::parse("2").unwrap());
        assert_eq!(s(&units(&held, &legs).unwrap().unwrap()), "2");
        held.insert(legs[1].contract.hk(), legs[1].contract.clone(), PyDec::parse("1").unwrap());
        assert!(units(&held, &legs).unwrap().is_none());
    }
}

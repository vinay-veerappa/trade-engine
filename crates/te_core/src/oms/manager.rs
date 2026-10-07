//! P3b-2a manager decisions. The host reads carriers and executes plans in order;
//! this module never reads a clock, a ledger or a venue.
//!
//! P3b-2b: every decision is a typed fn the command flow (`oms::flow`) calls directly.
//! `decide` keeps the JSON door the P3b-2a shims use: each arm parses its request and
//! calls the same fn, so there is one reader of each rule.
use crate::ledger::bridge;
use crate::ledger::codec::{enc_obj, enc_order, py_repr};
use crate::ledger::fold::AccountState;
use crate::ledger::json::{Json, dumps};
use crate::ledger::model::{
    derr, err, parse_datetime, Event, EventKind, Instrument, LErr, Obj, Order, OrderState, OrderType, Side, Tif, R,
};
use crate::ledger::ops::*;
use crate::money::{DecErr, Money, Round};
use crate::oms::options::sha256_hex;
use num_bigint::{BigInt, BigUint, Sign};
use num_traits::ToPrimitive;
use std::cmp::Ordering;

fn field<'a>(j: &'a Json, k: &str) -> R<&'a Json> {
    j.get(k).ok_or_else(|| LErr {kind: "value", msg: format!("manager request missing {k}")})
}
fn text(j: &Json) -> R<&str> {
    match j { Json::Str(s) => Ok(s), _ => err("value", "manager expected string") }
}
fn st<'a>(j: &'a Json, k: &str) -> R<&'a str> { text(field(j, k)?) }
fn array(j: &Json) -> R<&[Json]> {
    match j { Json::Arr(a) => Ok(a), _ => err("value", "manager expected array") }
}
fn flag(j: &Json, k: &str) -> R<bool> {
    match field(j,k)? { Json::Bool(b) => Ok(*b), _ => err("value", "manager expected bool") }
}
fn dec(j: &Json) -> R<Money> {
    Money::parse(text(j)?).ok_or_else(|| LErr {kind: "value", msg: "invalid decimal".into()})
}
fn d(j: &Json, k: &str) -> R<Money> { dec(field(j,k)?) }
fn optional(j: &Json, k: &str) -> R<Option<Money>> {
    match field(j,k)? { Json::Null => Ok(None), v => Ok(Some(dec(v)?)) }
}
fn order(j: &Json) -> R<Order> {
    match bridge::obj_from_text(&dumps(j))? { Obj::Order(o) => Ok(o), _ => err("value", "manager expected order") }
}
fn js(s: impl Into<String>) -> Json { Json::Str(s.into()) }
fn jd(d: &Money) -> Json { js(s(d)) }
fn jod(d: &Option<Money>) -> Json { d.as_ref().map_or(Json::Null, jd) }
fn ds(a: &[Money]) -> Json { Json::Arr(a.iter().map(jd).collect()) }
fn tuple(a: Vec<Json>) -> Json { Json::Arr(a) }

/// `OrderState` in FILLED, CANCELLED, REJECTED or EXPIRED.
pub fn terminal(s: OrderState) -> bool {
    matches!(s, OrderState::Filled | OrderState::Cancelled | OrderState::Rejected | OrderState::Expired)
}
fn sum(a: &[Money]) -> R<Money> { a.iter().try_fold(zero(), |n,v| add(&n,v)) }
fn floor(a: &Money) -> R<Money> { a.to_integral(Round::Floor).map_err(derr) }
fn quantity(equity: bool, q: &Money) -> R<()> {
    if equity && !q.is_integral().map_err(derr)? {
        return err("value",format!("Equity order quantity must be a whole number of shares, got {}",s(q)));
    }
    Ok(())
}
fn equity(i: &Instrument) -> bool { matches!(i, Instrument::Equity(_)) }
fn positive(q: &Money, message: String) -> R<()> {
    if !q.is_finite() || le(q,&zero())? { return err("value",message); }
    Ok(())
}
fn big_integer(q: &Money) -> R<BigInt> {
    if q.is_zero() { return Ok(BigInt::from(0u8)); }
    let exp = q.exponent();
    let mut exponent = exp.unsigned_abs();
    let mut factor = BigUint::from(1u8);
    let mut base = BigUint::from(10u8);
    while exponent != 0 {
        if exponent & 1 != 0 { factor *= &base; }
        exponent >>= 1;
        if exponent != 0 { base = &base * &base; }
    }
    let coefficient = if exp >= 0 { q.coefficient() * factor } else { q.coefficient() / factor };
    Ok(BigInt::from_biguint(if q.is_negative() { Sign::Minus } else { Sign::Plus }, coefficient))
}

/// Largest remainder, stable by index. Python integer products remain unbounded.
fn allocate(q: &Money, w: &[Money], equity: bool) -> R<Vec<Money>> {
    if w.is_empty() { return Ok(vec![]); }
    for v in w {
        if le(v,&zero())? { return err("value","target allocation weights must be positive"); }
    }
    let total = sum(w)?;
    if equity {
        quantity(true,q)?;
        for v in w {
            if !v.is_integral().map_err(derr)? {
                return err("value","Equity target allocation weights must be whole shares");
            }
        }
        let shares = big_integer(q)?;
        let weights = w.iter().map(big_integer).collect::<R<Vec<_>>>()?;
        let denominator: BigInt = weights.iter().sum();
        let mut a = Vec::new();
        let mut r = Vec::new();
        for v in weights {
            let product = &shares * v;
            let mut allocation = &product / &denominator;
            let mut remainder = &product % &denominator;
            if remainder.sign() == Sign::Minus {
                allocation -= 1;
                remainder += &denominator;
            }
            a.push(allocation);
            r.push(remainder);
        }
        let remaining = (shares - a.iter().sum::<BigInt>()).to_usize()
            .ok_or_else(|| LErr {kind: "overflow", msg: "manager remainder index overflow".into()})?;
        let mut keyed: Vec<(BigInt, usize)> = r.into_iter().enumerate().map(|(i,v)| (-v,i)).collect();
        keyed.sort();
        for &(_, i) in keyed.iter().take(remaining) {
            if let Some(v) = a.get_mut(i) { *v += 1; }
        }
        a.into_iter().map(|v| {
            let n = v.to_i128().ok_or(derr(DecErr::Overflow))?;
            Money::from_i128(n).map_err(derr)
        }).collect()
    } else {
        let mut portions: Vec<Money> = w.iter().map(|v| div(&mul(q,v)?,&total)).collect::<R<_>>()?;
        if let Some((last, rest)) = portions.split_last_mut() {
            *last = sub(q,&sum(rest)?)?;
        }
        Ok(portions)
    }
}

/// `_validate_quantity`: an equity quantity is whole shares.
pub fn validate_quantity(instrument: &Instrument, q: &Money) -> R<()> {
    if matches!(instrument, Instrument::Future(_)) {
        return err("unsupported", "futures not supported in OMS");
    }
    quantity(equity(instrument), q)
}

/// `_allocate_quantity`.
pub fn allocate_quantity(q: &Money, w: &[Money], instrument: &Instrument) -> R<Vec<Money>> {
    if matches!(instrument, Instrument::Future(_)) {
        return err("unsupported", "futures not supported in OMS");
    }
    allocate(q, w, equity(instrument))
}

/// `_split_quantity`: `count` equal weights, each portion positive.
pub fn split_quantity(q: &Money, count: usize, instrument: &Instrument) -> R<Vec<Money>> {
    if matches!(instrument, Instrument::Future(_)) {
        return err("unsupported", "futures not supported in OMS");
    }
    split(q, &vec![Money::int(1); count], equity(instrument))
}

fn split(q: &Money, w: &[Money], equity: bool) -> R<Vec<Money>> {
    if w.is_empty() { return Ok(vec![]); }
    quantity(equity,q)?;
    let portions=allocate(q,w,equity)?;
    for v in &portions {
        if le(v,&zero())? {
            return err("value","quantity is too small to allocate a positive amount to each target");
        }
    }
    Ok(portions)
}

/// `_fraction_quantities`: each target's fraction, the remainder a runner.
pub fn fraction_quantities(q: &Money, w: &[Money], instrument: &Instrument) -> R<Vec<Money>> {
    if matches!(instrument, Instrument::Future(_)) {
        return err("unsupported", "futures not supported in OMS");
    }
    fractions(q, w, equity(instrument))
}

fn fractions(q: &Money, w: &[Money], equity: bool) -> R<Vec<Money>> {
    quantity(equity,q)?;
    let runner=sub(&Money::int(1),&sum(w)?)?;
    let mut weights=w.to_vec();
    if gt(&runner,&zero())? { weights.push(runner); }
    let exact: Vec<Money>=weights.iter().map(|v| mul(q,v)).collect::<R<_>>()?;
    let mut portions=exact.clone();
    if equity {
        portions=exact.iter().map(floor).collect::<R<_>>()?;
        let remaining=big_integer(&sub(q,&sum(&portions)?)?)?;
        // The values here are finite, so every key compares; a stable index breaks ties.
        let mut keyed: Vec<(Money, usize)>=Vec::new();
        for (i,(a,b)) in exact.iter().zip(&portions).enumerate() { keyed.push((neg(&sub(a,b)?)?,i)); }
        keyed.sort_by(|(a,i),(b,k)| a.cmp_ord(b).unwrap_or(Ordering::Equal).then(i.cmp(k)));
        let end = if remaining.sign() == Sign::Minus {
            (BigInt::from(keyed.len()) + remaining).to_usize().unwrap_or(0)
        } else {
            remaining.to_usize().unwrap_or(keyed.len())
        };
        for &(_, i) in keyed.iter().take(end) {
            if let Some(v) = portions.get_mut(i) { *v=add(v,&Money::int(1))?; }
        }
    }
    portions.truncate(w.len());
    for v in &portions {
        if le(v,&zero())? {
            return err("value",format!("quantity {} is too small to give every target its fraction",s(q)));
        }
    }
    Ok(portions)
}

/// Whether the venue works `t` natively: a stop needs the native-stops flag as well.
pub fn supports_native(t: OrderType, supported: &[OrderType], native_stops: bool) -> bool {
    (!(t == OrderType::Stop || t == OrderType::StopLimit) || native_stops) && supported.contains(&t)
}
fn conflict<T>(id: &str, message: &str) -> R<T> {
    err("idempotency",format!("command_id '{id}' {message}"))
}
fn pending<T>(id: &str) -> R<T> {
    err("pending_reconciliation",format!("Order '{id}' is pending reconciliation"))
}
fn management<T>(message: impl Into<String>) -> R<T> { err("order_management",message) }
fn payload_equal(a: &Json, b: &Json) -> R<bool> {
    if let (Some(da), Some(db))=(a.get("d"),b.get("d")) { return eq(&dec(da)?,&dec(db)?); }
    if let (Some(ta), Some(tb))=(a.get("T"),b.get("T")) {
        return Ok(parse_datetime(text(ta)?)?.0.same(&parse_datetime(text(tb)?)?.0));
    }
    match (a,b) {
        (Json::Obj(x),Json::Obj(y)) => {
            if x.len()!=y.len() { return Ok(false); }
            for (k,v) in x {
                let Some(w)=b.get(k) else { return Ok(false); };
                if !payload_equal(v,w)? { return Ok(false); }
            }
            Ok(true)
        }
        (Json::Arr(x),Json::Arr(y)) => {
            if x.len()!=y.len() { return Ok(false); }
            for (v,w) in x.iter().zip(y) { if !payload_equal(v,w)? { return Ok(false); } }
            Ok(true)
        }
        _=>Ok(a==b),
    }
}


/// A prior event a replay is checked against (`_event(existing)`).
pub struct Prior<'a> {
    pub account: &'a str,
    pub kind: Option<EventKind>,
    pub payload: &'a Obj,
}
impl<'a> Prior<'a> {
    pub fn of(e: &'a Event) -> Prior<'a> { Prior {account: &e.account, kind: Some(e.kind), payload: &e.payload} }
}

pub fn created_replay(prior: &Prior, account: &str, fingerprint: &str, command: &str) -> R<()> {
    let matches=prior.kind==Some(EventKind::OrdersCreated) && prior.account==account &&
        matches!(prior.payload,Obj::OrdersCreated(p) if p.fingerprint==fingerprint);
    if !matches { return conflict(command,"was already used for a different OMS command"); }
    Ok(())
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ReplayMode { Pending, Noop, Local }

pub fn replace_replay(prior: &Prior, mode: ReplayMode, order: &str, account: &str, reason: &str,
                      command: &str, state: Option<OrderState>) -> R<()> {
    let matches=prior.account==account &&
        if mode==ReplayMode::Pending {
            prior.kind==Some(EventKind::OrderPending) && matches!(prior.payload,Obj::StateChange(p)
                if p.order_id==order && p.reason.as_deref()==Some(reason))
        } else {
            prior.kind==Some(EventKind::OrderUpdated) && matches!(prior.payload,Obj::OrderUpdated(p)
                if p.order.order_id==order && p.reason==reason)
        };
    if !matches {
        let what=if mode==ReplayMode::Local {"different emulated stop changes"} else {"different replace changes"};
        return conflict(command,&format!("was replayed with {what}"));
    }
    if mode==ReplayMode::Pending && state==Some(OrderState::PendingUnknown) {
        return err("pending_reconciliation",format!("Replace command '{command}' remains pending reconciliation"));
    }
    Ok(())
}

pub fn observation_replay(prior: &Prior, account: &str, order: &str, price: &Money, command: &str) -> R<()> {
    let matches=prior.kind==Some(EventKind::OrderEmulationUpdated) && prior.account==account &&
        match prior.payload {
            Obj::Emulated(p) => p.order_id==order &&
                p.observed_price.as_ref().map(|v|eq(v,price)).transpose()?.unwrap_or(false),
            _=>false,
        };
    if !matches { return conflict(command,"was replayed with a different price observation"); }
    Ok(())
}

/// `_append`'s replay check. Both payloads are encoded first, as `_wire` did.
pub fn append_replay(prior: &Prior, account: &str, kind: EventKind, payload: &Obj, command: &str) -> R<()> {
    let existing=enc_obj(prior.payload)?;
    let requested=enc_obj(payload)?;
    let matches=prior.account==account && prior.kind==Some(kind) && payload_equal(&existing,&requested)?;
    if !matches { return conflict(command,"was replayed with a different payload"); }
    Ok(())
}

/// The entry's (limit, stop) and the exits' side.
pub fn entry_terms(t: OrderType, side: Side, price: &Money, limit: Option<&Money>) -> (Option<Money>, Option<Money>, Side) {
    let exit=if side==Side::Buy {Side::Sell} else {Side::Buy};
    if t==OrderType::Limit { (Some(price.clone()),None,exit) } else { (limit.cloned(),Some(price.clone()),exit) }
}

/// `submit_mode`: whether a stop or trail is emulated locally rather than sent.
pub fn emulates(t: OrderType, types: &[OrderType], native_stops: bool) -> bool {
    matches!(t,OrderType::Stop|OrderType::StopLimit|OrderType::Trail) && !supports_native(t,types,native_stops)
}

pub fn trailing_check(t: OrderType, method: &str, order: &str, types: &[OrderType]) -> R<()> {
    if t!=OrderType::Trail {
        return err("value",format!("{method} requires a TRAIL order"));
    }
    if method=="update_trailing" && types.contains(&OrderType::Trail) {
        return management(format!("Order '{order}' is native at this venue; no local trail is running"));
    }
    Ok(())
}

pub fn emulation_check(order: &str, t: OrderType, emulated: bool, types: &[OrderType], native_stops: bool) -> R<()> {
    if !emulated {
        return management(format!("Emulated order '{order}' has not been started"));
    }
    if supports_native(t,types,native_stops) {
        return management(format!("Order '{order}' is native at this venue; no local emulation is running"));
    }
    Ok(())
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Observed { Observe, Route, Return }

pub fn observed_action(triggered: bool, state: OrderState, order: &str) -> R<Observed> {
    if !triggered { return Ok(Observed::Observe); }
    match state {
        OrderState::PendingUnknown => err("pending_reconciliation",format!("Triggered emulated order '{order}' awaits venue reconciliation")),
        OrderState::New=>Ok(Observed::Route),
        _=>Ok(Observed::Return),
    }
}

pub fn observation_reason(t: OrderType, price: &Money, triggered: bool) -> String {
    let (t,p)=(t.value(),s(price));
    if triggered {format!("{t} triggered at observed price {p}")} else {format!("Emulated {t} observed price {p}")}
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Start { Return, RefuseLimit, Limit, Trigger }

pub fn start_emulation(o: &Order, types: &[OrderType]) -> R<Start> {
    if o.state==OrderState::PendingUnknown {
        return err("pending_reconciliation",format!("Emulated order '{}' is pending reconciliation",o.order_id));
    }
    if o.state!=OrderState::New {return Ok(Start::Return);}
    if o.order_type==OrderType::StopLimit {
        if !types.contains(&OrderType::Limit) {return Ok(Start::RefuseLimit);}
        return Ok(Start::Limit);
    }
    Ok(Start::Trigger)
}

/// Whether a triggered emulated order is still NEW, so its venue order goes out.
pub fn submit_emulated(o: &Order) -> R<bool> {
    if o.state==OrderState::PendingUnknown {
        return err("pending_reconciliation",format!("Triggered order '{}' awaits venue reconciliation",o.order_id));
    }
    Ok(o.state==OrderState::New)
}

pub fn stop_rejected(t: OrderType, state: OrderState, order: &str) -> R<()> {
    if t==OrderType::Stop && state==OrderState::Rejected {
        return management(format!("Protective stop '{order}' was rejected; open quantity requires venue reconciliation"));
    }
    Ok(())
}

/// A child waits until its parent has a fill.
pub fn child_hold(parent_filled: &Money) -> R<bool> { le(parent_filled,&zero()) }

pub fn fill_match(fill: &str, order: &str, account: &str, fill_account: &str, env: &str, fill_env: &str) -> R<()> {
    if account!=fill_account || env!=fill_env {
        return management(format!("Fill '{fill}' account or venue environment does not match order '{order}'"));
    }
    Ok(())
}

/// A close order exists: refuse another command's; true when it already left NEW.
pub fn close_replay(o: &Order, entry: &str, command: &str) -> R<bool> {
    if o.command_id!=command {
        return err("idempotency",format!("Bracket '{entry}' already has close order '{}' from command '{}'",o.order_id,o.command_id));
    }
    Ok(o.state!=OrderState::New)
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum IdMode { Replace, Cancel }

/// The confirmed venue order id a replace or cancel goes to.
pub fn confirmed_id(venue: Option<&str>, order: &str, mode: IdMode) -> R<String> {
    match venue {
        Some(v) => Ok(v.to_string()),
        None => err("pending_reconciliation",if mode==IdMode::Replace {
            format!("Order '{order}' has no confirmed venue order id")
        } else {format!("Working order '{order}' has no venue id")}),
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CancelMode { Return, Local, Venue }

pub fn cancel_mode(state: OrderState, order: &str, oco: bool) -> R<CancelMode> {
    if matches!(state,OrderState::Cancelled|OrderState::Filled|OrderState::Rejected|OrderState::Expired) {return Ok(CancelMode::Return);}
    if state==OrderState::PendingUnknown {
        if oco {
            return err("oco_unknown",format!("OCO sibling '{order}' is already pending reconciliation"));
        }
        return Ok(CancelMode::Return);
    }
    Ok(if state==OrderState::New {CancelMode::Local} else {CancelMode::Venue})
}

/// The event a submit acknowledgement records, and its reason.
pub fn submit_ack(status: &str, message: &str) -> R<(EventKind, String)> {
    Ok(match status {
        "ACCEPTED"=>(EventKind::OrderAccepted,"Venue accepted order".into()),
        "REJECTED"=>(EventKind::OrderRejected,format!("Venue rejected order: {}",if message.is_empty() {"reason not supplied"}else{message})),
        "PENDING"=>(EventKind::OrderPending,format!("Venue has not resolved order: {}",if message.is_empty() {"no status message"}else{message})),
        _=>return management(format!("Unrecognized venue submit status {}",py_repr(status))),
    })
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Ack { Accepted, Rejected, Pending }

/// A replace or cancel acknowledgement: the action, its reason, and the refusal raised
/// after the action's events are recorded.
#[derive(Debug, Clone)]
pub struct AckPlan {
    pub action: Ack,
    pub reason: String,
    pub refusal: Option<LErr>,
}

pub fn replace_ack(status: &str, message: &str, order: &str, command: &str) -> AckPlan {
    // Preserved defect: any status other than PENDING/REJECTED is accepted.
    if status=="PENDING" {
        return AckPlan {action: Ack::Pending, reason: format!("Venue replace remains pending: {}",if message.is_empty() {"no status message"}else{message}), refusal: None};
    }
    if status=="REJECTED" {
        let m=if message.is_empty() {"reason not supplied"}else{message};
        return AckPlan {action: Ack::Rejected, reason: format!("Venue rejected replace: {m}"),
            refusal: Some(LErr {kind: "order_management", msg: format!("Venue rejected replace for '{order}': {m}")})};
    }
    AckPlan {action: Ack::Accepted, reason: format!("Venue confirmed replace: {command}"), refusal: None}
}

pub fn cancel_ack(status: &str, reason: &str, message: &str, order: &str, oco: bool) -> R<AckPlan> {
    match status {
        "ACCEPTED"=>Ok(AckPlan {action: Ack::Accepted, reason: reason.to_string(), refusal: None}),
        "REJECTED"=>{
            let message=format!("Venue rejected cancel for '{order}': {}",if message.is_empty() {"order may already have filled; reconcile required"}else{message});
            Ok(AckPlan {action: Ack::Rejected, reason: message.clone(),
                refusal: Some(LErr {kind: if oco {"oco_unknown"}else{"pending_reconciliation"}, msg: message})})
        }
        "PENDING"=>Ok(AckPlan {action: Ack::Pending,
            reason: format!("Venue cancel remains pending: {}",if message.is_empty() {"no status message"}else{message}),
            refusal: if oco {Some(LErr {kind: "oco_unknown", msg: format!("Venue has not confirmed cancellation of OCO sibling '{order}'")})}else{None}}),
        _=>management(format!("Unrecognized venue cancel status {}",py_repr(status))),
    }
}

/// The read-back row for an order: by venue id, else by order id.
pub fn reconcile_find(order: &str, venue: &str, ids: &[String], state: OrderState) -> R<usize> {
    match ids.iter().position(|v|v==venue||v==order) {
        Some(i)=>Ok(i),
        None=>err("order_reconciliation",format!("Venue has no read-back for order '{order}'; it remains {}",state.value())),
    }
}

pub fn reconcile_replace(order: &str, found: OrderState, unresolved: bool) -> R<()> {
    if unresolved && !terminal(found) {
        return err("order_reconciliation",format!("Pending replace terms for '{order}' cannot be resolved from status-only venue read-back"));
    }
    Ok(())
}

pub fn pending_state(state: OrderState) -> bool { state==OrderState::PendingUnknown }

/// The venue reports more filled than the ledger records.
pub fn reconcile_fills(found: &Money, recorded: &Money) -> R<bool> { gt(found,recorded) }

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Resolution { Return, Updated, Record(EventKind) }

pub fn reconcile_result(found: OrderState, state: OrderState, order: &str) -> R<Resolution> {
    match found {
        OrderState::Filled | OrderState::PartiallyFilled=>{
            if found==OrderState::Filled && state!=OrderState::Filled {
                return err("order_reconciliation",format!("Venue reports '{order}' FILLED but its fill records do not complete it"));
            }
            Ok(Resolution::Return)
        }
        OrderState::Submitted=>Ok(Resolution::Updated),
        OrderState::Accepted=>Ok(Resolution::Record(EventKind::OrderAccepted)),
        OrderState::Cancelled=>Ok(Resolution::Record(EventKind::OrderCancelled)),
        OrderState::Rejected=>Ok(Resolution::Record(EventKind::OrderRejected)),
        OrderState::Expired=>Ok(Resolution::Record(EventKind::OrderExpired)),
        _=>err("order_reconciliation",format!("Venue state {} does not resolve order '{order}'",found.value())),
    }
}

pub fn ingest_check(found: &Money, recorded: &Money, order: &str, state: OrderState) -> R<()> {
    if lt(recorded,found)? {
        return err("order_reconciliation",format!("Venue reports {} filled for '{order}' but its fill records account for {}; it remains {}",s(found),s(recorded),state.value()));
    }
    Ok(())
}

pub fn matching_ids(ids: &[String], venue: &str) -> Vec<usize> {
    ids.iter().enumerate().filter(|(_,v)|*v==venue).map(|(i,_)|i).collect()
}

/// A protective stop under a bracket entry.
pub fn protective_child(parent: Option<&str>, t: OrderType) -> bool { parent.is_some() && t==OrderType::Stop }

/// The limit price of a triggered order's venue order.
pub fn route_limit(t: OrderType, price: Option<&Money>) -> Option<Money> {
    if t==OrderType::Limit {price.cloned()} else {None}
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Refusal<'a> { Child(&'a str), Protective, Native, Limit, Trigger, Tif }

/// A refused order: the ORDER_REFUSED reason, the command-id suffix, and the refusal.
pub fn refused(o: &Order, mode: Refusal) -> (String, String, LErr) {
    let id=&o.order_id;
    let (reason,suffix,kind,message)=match mode {
        Refusal::Child(parent)=>(format!("Child held until parent '{parent}' has a fill"),
            "child-held".into(),"order_management",format!("Child order '{id}' is held until its entry fills")),
        Refusal::Protective=>("Cannot cancel the only protective stop while the bracket has open quantity".into(),
            "protective-stop-refused".into(),"order_management",format!("Cannot cancel protective stop '{id}' while its position is open")),
        Refusal::Native=>(format!("Venue does not support {}; no native order was sent",o.order_type.value()),
            "unsupported-type".into(),"unsupported_order",format!("Venue does not support order type {}",o.order_type.value())),
        Refusal::Limit=>("Emulated STOP_LIMIT requires venue LIMIT support".into(),"no-stop-limit-fallback".into(),
            "unsupported_order","Emulated STOP_LIMIT requires venue LIMIT support".into()),
        Refusal::Trigger=>("Emulated stop requires venue MARKET or LIMIT support when triggered".into(),
            "no-trigger-order".into(),"unsupported_order","Emulated stops require MARKET or LIMIT capability".into()),
        Refusal::Tif=>(format!("Venue does not support time in force {}",o.tif.value()),
            format!("unsupported-tif:{}",o.tif.value()),"unsupported_order",format!("Venue does not support time in force {}",o.tif.value())),
    };
    (reason,suffix,LErr {kind,msg: message})
}

pub fn oco_check(state: OrderState, order: &str) -> R<()> {
    if !terminal(state) {
        return err("oco_unknown",format!("Could not confirm cancellation of OCO sibling '{order}'"));
    }
    Ok(())
}

/// The replace command an ORDER_PENDING event opened, if it is one.
pub fn pending_candidate(order: &str, pending: bool, event_order: &str, reason: Option<&str>, command: Option<&str>) -> Option<String> {
    if !pending || event_order!=order || !reason.is_some_and(|r| r.starts_with("Replace pending:")) { return None; }
    command.and_then(|c| c.strip_suffix(":pending")).map(String::from)
}

pub fn positive_quantity(q: &Money) -> R<()> { positive(q,"quantity must be finite and positive".into()) }
pub fn check_price(p: &Money) -> R<()> { positive(p,format!("price must be finite and positive, got {}",s(p))) }
pub fn check_fraction(f: &Money) -> R<()> {
    if !f.is_finite() || le(f,&zero())? || ge(f,&Money::int(1))? {
        return err("value",format!("fraction must be between 0 and 1 exclusive, got {}",s(f)));
    }
    Ok(())
}

/// `_fingerprint_order`: the encoded order's digest.
pub fn fingerprint_order(o: &Order) -> R<String> { Ok(sha256_hex(dumps(&enc_order(o)?).as_bytes())) }

pub fn reduce_fingerprint(entry: &str, fraction: &Money, reason: &str) -> String {
    let p=Json::Obj(vec![("action".into(),js("reduce")),
        ("entry_order_id".into(),js(entry)),
        ("fraction".into(),jd(fraction)),
        ("reason".into(),js(reason))]);
    sha256_hex(dumps(&p).as_bytes())
}

/// `_bracket_fingerprint` over the intent's wire fields (`_wire(intent)`).
pub fn bracket_fingerprint(intent: &Json, quantity: &Money) -> R<String> {
    let mut fields=Vec::new();
    for key in ["intent_id","account_id","instrument","side","quantity_rule",
                "entry_price","stop_loss","profit_targets","reason","command_id",
                "entry_tif","exit_tif"] {
        fields.push((key.into(),field(intent,key)?.clone()));
    }
    fields.push(("quantity".into(),jd(quantity)));
    let t=st(intent,"entry_type")?;
    if t != "LIMIT" { fields.push(("entry_type".into(),js(t))); }
    if t == "STOP_LIMIT" { fields.push(("entry_limit_price".into(),
        if field(intent,"entry_limit_price")? == &Json::Null { js("None") } else { field(intent,"entry_limit_price")?.clone() })); }
    if field(intent,"target_fractions")? != &Json::Null {
        fields.push(("target_fractions".into(),field(intent,"target_fractions")?.clone()));
    }
    Ok(sha256_hex(dumps(&Json::Obj(fields)).as_bytes()))
}

pub fn bracket_capabilities(t: OrderType, entry_tif: Tif, exit_tif: Tif, types: &[OrderType], tifs: &[Tif],
                            native_stops: bool) -> R<()> {
    let mut unsupported: Vec<&str>=[entry_tif,exit_tif].iter().filter(|t| !tifs.contains(t)).map(|t| t.value()).collect();
    unsupported.sort(); unsupported.dedup();
    if !unsupported.is_empty() {
        return err("unsupported_order",format!("Venue does not support bracket time in force {}",unsupported.join(", ")));
    }
    if (t == OrderType::Stop || t == OrderType::StopLimit) && !supports_native(t,types,native_stops) {
        let name=if t == OrderType::Stop { "stop" } else { "stop-limit" };
        return err("unsupported_order",format!("Venue has no native {} orders; a {name} entry cannot be worked",t.value()));
    }
    Ok(())
}

/// The venue order type a triggered emulated stop goes out as; None refuses.
pub fn trigger_type(types: &[OrderType]) -> Option<OrderType> {
    if types.contains(&OrderType::Market) { Some(OrderType::Market) }
    else if types.contains(&OrderType::Limit) { Some(OrderType::Limit) }
    else { None } // host must append the refusal before raising
}

/// True when the order's time in force is unsupported (the host refuses it).
pub fn tif(tif: Tif, venue_type: Option<OrderType>, types: &[OrderType], tifs: &[Tif]) -> R<bool> {
    if !tifs.contains(&tif) { return Ok(true); }
    if let Some(v)=venue_type {
        if !types.contains(&v) {
            return err("unsupported_order",format!("Venue does not support trigger order type {}",v.value()));
        }
    }
    Ok(false)
}

pub fn children(entry: &str, orders: &[&Order]) -> Vec<String> {
    orders.iter().filter(|o|o.parent_order_id.as_deref()==Some(entry)).map(|o|o.order_id.clone()).collect()
}

pub fn siblings(o: &Order, orders: &[&Order]) -> Vec<String> {
    orders.iter().filter(|c| c.order_id != o.order_id && o.parent_order_id.is_some() &&
        o.oco_group.is_some() && c.parent_order_id == o.parent_order_id && c.oco_group == o.oco_group)
        .map(|c|c.order_id.clone()).collect()
}

/// A bracket's (entry, stop, targets by id); a missing entry or stop is StopIteration.
pub fn bracket(orders: &[&Order]) -> R<(String, String, Vec<String>)> {
    let entry=orders.iter().find(|o|o.parent_order_id.is_none()).ok_or_else(|| LErr {kind:"stop_iteration",msg:String::new()})?;
    let stop=orders.iter().find(|o|o.parent_order_id.as_deref()==Some(entry.order_id.as_str()) && o.order_type==OrderType::Stop)
        .ok_or_else(|| LErr {kind:"stop_iteration",msg:String::new()})?;
    let mut targets: Vec<&&Order>=orders.iter().filter(|o|o.parent_order_id.as_deref()==Some(entry.order_id.as_str()) && o.order_type==OrderType::Limit).collect();
    targets.sort_by(|a,b|a.order_id.cmp(&b.order_id));
    Ok((entry.order_id.clone(),stop.order_id.clone(),targets.iter().map(|o|o.order_id.clone()).collect()))
}

pub fn is_reduce(entry: &str, order_id: &str) -> bool { order_id.starts_with(&format!("{entry}:reduce:")) }

fn filled(state: &AccountState, id: &str) -> Money { state.filled_quantity.get(id).cloned().unwrap_or_else(zero) }
/// The entry's children in folded-state order (the `cancel_protective` arm's gate reads it).
pub fn exits<'a>(state: &'a AccountState, entry: &str) -> Vec<&'a Order> {
    state.orders.values().filter(|o|o.parent_order_id.as_deref()==Some(entry)).collect()
}
fn exited(state: &AccountState, children: &[&Order]) -> R<Money> {
    sum(&children.iter().map(|o|filled(state,&o.order_id)).collect::<Vec<_>>())
}

/// `_open_bracket_stop`: the working protective stop and the open quantity.
pub fn open_stop(state: &AccountState, entry: &Order) -> R<(String, Money)> {
    let children=exits(state,&entry.order_id);
    let stop=children.iter().find(|o|o.order_type==OrderType::Stop).copied();
    if entry.parent_order_id.is_some() {
        return management(format!("Order '{}' is not a bracket entry",entry.order_id));
    }
    let Some(stop)=stop else { return management(format!("Order '{}' has no protective stop",entry.order_id)); };
    let open=sub(&filled(state,&entry.order_id),&exited(state,&children)?)?;
    if le(&open,&zero())? { return management(format!("Bracket '{}' has no open quantity",entry.order_id)); }
    if !matches!(stop.state,OrderState::Accepted|OrderState::PartiallyFilled) {
        return management(format!("Protective stop '{}' is {}, not working at the venue; reconcile before managing the bracket",stop.order_id,stop.state.value()));
    }
    Ok((stop.order_id.clone(),open))
}

/// Whether cancelling the protective stop `o` would leave open quantity unprotected.
pub fn cancel_protective(state: &AccountState, entry: &Order, o: &Order) -> R<bool> {
    let children=exits(state,&entry.order_id);
    if !children.iter().any(|o|o.order_type==OrderType::Stop) { return Ok(false); }
    let entry_filled=filled(state,&entry.order_id);
    let exited=exited(state,&children)?;
    Ok(gt(&entry_filled,&exited)? && !terminal(o.state))
}

/// `_synchronize_bracket`'s first I/O boundary.
#[derive(Debug, Clone)]
pub struct SyncPlan {
    pub stop: String,
    pub entry_filled: Money,
    pub open: Money,
    pub stop_filled: Money,
    pub entry_terminal: bool,
    pub targets: Vec<String>,
    pub closers: Vec<String>,
}

pub fn sync(state: &AccountState, entry: &Order) -> R<Option<SyncPlan>> {
    let entry=state.orders.get(&entry.order_id).ok_or_else(|| LErr {
        kind:"key", msg:format!("Unknown order_id '{}'",entry.order_id)
    })?;
    let children=exits(state,&entry.order_id);
    let Some(stop)=children.iter().find(|o|o.order_type==OrderType::Stop).copied() else { return Ok(None); };
    let entry_filled=filled(state,&entry.order_id);
    sub(&entry_filled,&exited(state,&children)?)?;
    if eq(&entry_filled,&zero())? && !terminal(entry.state) { return Ok(None); }
    let targets: Vec<&Order>=children.iter().copied().filter(|o|o.order_type==OrderType::Limit).collect();
    let closers: Vec<&Order>=children.iter().copied().filter(|o|o.order_type==OrderType::Market).collect();
    let stop_filled=filled(state,&stop.order_id);
    let target_filled=sum(&targets.iter().chain(&closers).map(|o|filled(state,&o.order_id)).collect::<Vec<_>>())?;
    let open=sub(&sub(&entry_filled,&stop_filled)?,&target_filled)?;
    if lt(&open,&zero())? {
        return management(format!("Exit fills exceed entry fills for bracket '{}'; venue reconciliation required",entry.order_id));
    }
    // Plan the first I/O boundary only. Budget computation comes AFTER the stop
    // resize, so its possible refusal cannot move ahead of that durable operation.
    Ok(Some(SyncPlan {stop: stop.order_id.clone(), entry_filled, open, stop_filled,
        entry_terminal: terminal(entry.state),
        targets: targets.iter().map(|o|o.order_id.clone()).collect(),
        closers: closers.iter().map(|o|o.order_id.clone()).collect()}))
}

/// True when the bracket holds open quantity to protect; false when it is flat.
pub fn sync_mode(filled: &Money, open: &Money) -> R<bool> { Ok(gt(filled,&zero())? && gt(open,&zero())?) }
/// True when the targets are re-budgeted: no stop fill and the entry is done.
pub fn sync_targets(stop_filled: &Money, entry_terminal: bool) -> R<bool> { Ok(eq(stop_filled,&zero())? && entry_terminal) }
pub fn is_positive(q: &Money) -> R<bool> { gt(q,&zero()) }

/// The targets' planned weights, plus the runner the entry's plan leaves.
pub fn target_weights(weights: &[Money], planned: &Money) -> R<Vec<Money>> {
    let runner=sub(planned,&sum(weights)?)?;
    let mut weights=weights.to_vec();
    if gt(&runner,&zero())? { weights.push(runner); }
    Ok(weights)
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ChildPlan { Return, Submit, Local, Replace }

/// `_ensure_child_quantity`: the action, and the child's total quantity.
pub fn child_quantity(o: &Order, filled: &Money, q: &Money) -> R<(ChildPlan, Option<Money>)> {
    if matches!(o.state,OrderState::Cancelled|OrderState::Filled|OrderState::Rejected) {
        if o.state == OrderState::Rejected {
            if o.order_type == OrderType::Stop {
                return management(format!("Protective stop '{}' was rejected; open quantity requires venue reconciliation",o.order_id));
            }
            return Ok((ChildPlan::Return,None));
        }
        return management(format!("Protective child '{}' is terminal in state {}",o.order_id,o.state.value()));
    }
    quantity(equity(&o.instrument),q)?;
    let total=add(filled,q)?;
    let mode=if eq(&o.quantity,&total)? {
        if o.state==OrderState::New { ChildPlan::Submit } else { ChildPlan::Return }
    } else if o.state==OrderState::New { ChildPlan::Local } else { ChildPlan::Replace };
    Ok((mode,Some(total)))
}

/// True when the stop moves (a replace); false when the price is unchanged.
pub fn move_stop(o: &Order, price: &Money) -> R<bool> {
    let Some(current)=&o.stop_price else { return management(format!("Protective stop '{}' has no stop price",o.order_id)); };
    if eq(price,current)? { return Ok(false); }
    let loosens=if o.side==Side::Sell { lt(price,current)? } else { gt(price,current)? };
    if loosens {
        return management(format!("Moving stop '{}' from {} to {} would widen the bracket's risk; stops only tighten",o.order_id,s(current),s(price)));
    }
    Ok(true)
}

/// The entry's reduce orders (the `reduce` arm's working-order gate reads it).
pub fn reduces<'a>(entry: &str, children: &[&'a Order]) -> Vec<&'a Order> {
    children.iter().copied().filter(|o|o.order_id.starts_with(&format!("{entry}:reduce:"))).collect()
}

pub fn close_guard(entry: &str, children: &[&Order]) -> R<()> {
    if reduces(entry,children).iter().any(|o|!terminal(o.state)) {
        return management(format!("Bracket '{entry}' has a reduce order working; it must resolve before the bracket can be closed"));
    }
    Ok(())
}

/// The reduce quantity and its order id.
pub fn reduce(entry: &str, children: &[&Order], open: &Money, fraction: &Money) -> R<(Money, String)> {
    let reduces=reduces(entry,children);
    if children.iter().any(|o|o.order_id==format!("{entry}:close") && !terminal(o.state)) {
        return management(format!("Bracket '{entry}' has a close order working; nothing is left to reduce"));
    }
    if reduces.iter().any(|o|!terminal(o.state)) {
        return management(format!("Bracket '{entry}' already has a reduce order working"));
    }
    let q=floor(&mul(open,fraction)?)?;
    if lt(&q,&Money::int(1))? {
        return management(format!("Reducing bracket '{entry}' by {} of {} rounds down to nothing; refusing to guess a size",s(fraction),s(open)));
    }
    Ok((q,format!("{entry}:reduce:{}",reduces.len()+1)))
}

pub fn replace_quantity(q: Option<&Money>, filled: &Money, equity: bool) -> R<()> {
    if let Some(q)=q {
        if !q.is_finite() || lt(q,filled)? || le(q,&zero())? {
            return err("value",format!("replacement quantity must be finite, positive, and at least filled quantity {}",s(filled)));
        }
        quantity(equity,q)?;
    }
    Ok(())
}

pub fn replace_state(o: &Order) -> R<()> {
    if o.state==OrderState::New || terminal(o.state) {
        return management(format!("Cannot replace order '{}' in state {}",o.order_id,o.state.value()));
    }
    if o.state==OrderState::PendingUnknown { return pending(&o.order_id); }
    Ok(())
}

/// A NEW emulated stop that has not triggered is replaced locally, never at the venue.
pub fn local_replace(o: &Order, emulated: bool, triggered: bool) -> bool {
    o.state==OrderState::New && matches!(o.order_type,OrderType::Stop|OrderType::StopLimit) && emulated && !triggered
}

/// `_replacement_terms`' order with the changes applied, and whether nothing changed.
pub fn replace_terms(o: &Order, q: Option<&Money>, limit: Option<&Money>, stop: Option<&Money>) -> (Order, bool) {
    let mut updated=o.clone();
    if let Some(q)=q { updated.quantity=q.clone(); }
    if let Some(p)=limit { updated.limit_price=Some(p.clone()); }
    if let Some(p)=stop { updated.stop_price=Some(p.clone()); }
    let same=updated.same(o);
    (updated,same)
}

/// `_ensure_stored`: a stored order may only be resubmitted unchanged (a child may shrink).
pub fn stored(created: &Order, candidate: &Order) -> R<()> {
    let mut candidate=candidate.clone();
    candidate.state=OrderState::New;
    let matches=if created.parent_order_id.is_none() { candidate.same(created) } else {
        let q=candidate.quantity.clone(); candidate.quantity=created.quantity.clone();
        candidate.same(created) && le(&q,&created.quantity)?
    };
    if !matches {
        return err("idempotency",format!("order_id '{}' was reused with a different order payload",candidate.order_id));
    }
    Ok(())
}

/// True when a triggered STOP_LIMIT goes out at its own limit.
pub fn trigger_price(o: &Order, price: Option<&Money>) -> R<bool> {
    if price.is_none() {
        return management(format!("Triggered emulated order '{}' has no observed price",o.order_id));
    }
    Ok(o.order_type==OrderType::StopLimit)
}

/// The emulated stop's price and whether `price` triggers it.
pub fn stop_observation(o: &Order, price: &Money) -> R<(Money, bool)> {
    let Some(stop)=&o.stop_price else {
        return management(format!("Emulated {} order '{}' has no stop price",o.order_type.value(),o.order_id));
    };
    let triggered=if o.side==Side::Buy { ge(price,stop)? } else { le(price,stop)? };
    Ok((stop.clone(),triggered))
}

/// Each operation is a pure, narrow ownership boundary. Requests use the ledger's
/// existing exact carrier trees; returned decimal strings retain Python scale.
pub fn decide(op: &str, raw: &str) -> R<String> {
    let j = bridge::json_from_text(raw)?;
    Ok(dumps(&decision(op,&j)?))
}

fn decision(op: &str, j: &Json) -> R<Json> {
    match op {
        "quantity" => { quantity(flag(j,"equity")?, &d(j,"quantity")?)?; Ok(Json::Null) }
        "allocate" | "split" | "fractions" => {
            let q=d(j,"quantity")?;
            let equity=flag(j,"equity")?;
            let w: Vec<Money>=array(field(j,"weights")?)?.iter().map(dec).collect::<R<_>>()?;
            Ok(ds(&match op { "allocate"=>allocate(&q,&w,equity)?, "split"=>split(&q,&w,equity)?, _=>fractions(&q,&w,equity)? }))
        }
        "fingerprint" => Ok(js(sha256_hex(dumps(field(j,"payload")?).as_bytes()))),
        "reduce_fingerprint" => Ok(js(reduce_fingerprint(st(j,"entry")?,&d(j,"fraction")?,st(j,"reason")?))),
        "bracket_fingerprint" => Ok(js(bracket_fingerprint(field(j,"intent")?,&d(j,"quantity")?)?)),
        "is_reduce" => Ok(Json::Bool(is_reduce(st(j,"entry")?,st(j,"order_id")?))),
        "replace_quantity" => {
            let q=optional(j,"quantity")?;
            let filled=if q.is_some() { d(j,"filled")? } else { zero() };
            let equity=match &q { Some(v) if v.is_finite() && !lt(v,&filled)? && !le(v,&zero())? => flag(j,"equity")?, _=>false };
            replace_quantity(q.as_ref(),&filled,equity)?;
            Ok(Json::Null)
        }
        "replace_terms" => {
            let o=order(field(j,"order")?)?;
            let (q,limit,stop)=(optional(j,"quantity")?,optional(j,"limit")?,optional(j,"stop")?);
            let (updated,same)=replace_terms(&o,q.as_ref(),limit.as_ref(),stop.as_ref());
            Ok(tuple(vec![jd(&updated.quantity),jod(&updated.limit_price),jod(&updated.stop_price),Json::Bool(same)]))
        }
        _ => err("value",format!("unknown manager decision {op}")),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn p(s:&str)->Money {Money::parse(s).unwrap()}
    #[test]
    fn allocation_conserves_odd_lots_and_stable_ties() {
        assert_eq!(allocate(&p("7"),&[p("1"),p("1"),p("1")],true).unwrap().iter().map(s).collect::<Vec<_>>(),
            vec!["3","2","2"]);
    }
    #[test]
    fn fractional_last_portion_absorbs_decimal_residue() {
        let a=allocate(&p("1"),&[p("1"),p("1"),p("1")],false).unwrap();
        assert!(eq(&sum(&a).unwrap(),&p("1")).unwrap());
        assert_eq!(s(&a[2]),"0.3333333333333333333333333334");
    }
    #[test]
    fn floor_reduce_never_guesses_zero() {
        assert_eq!(s(&floor(&mul(&p("3"),&p("0.5")).unwrap()).unwrap()),"1");
    }
}

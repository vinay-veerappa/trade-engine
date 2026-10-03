//! P3b-2a manager decisions. The host reads carriers and executes plans in order;
//! this module never reads a clock, a ledger or a venue.
use crate::ledger::bridge;
use crate::ledger::json::{Json, dumps};
use crate::ledger::model::{derr, err, Instrument, LErr, Obj, Order, OrderState, OrderType, Side, R};
use crate::ledger::ops::*;
use crate::ledger::pydec::{DKind, DecErr, PyDec, Round};
use crate::oms::options::sha256_hex;
use num_bigint::{BigInt, BigUint, Sign};
use num_traits::ToPrimitive;

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
fn dec(j: &Json) -> R<PyDec> {
    PyDec::parse(text(j)?).ok_or_else(|| LErr {kind: "value", msg: "invalid decimal".into()})
}
fn d(j: &Json, k: &str) -> R<PyDec> { dec(field(j,k)?) }
fn optional(j: &Json, k: &str) -> R<Option<PyDec>> {
    match field(j,k)? { Json::Null => Ok(None), v => Ok(Some(dec(v)?)) }
}
fn order(j: &Json) -> R<Order> {
    match bridge::obj_from_text(&dumps(j))? { Obj::Order(o) => Ok(o), _ => err("value", "manager expected order") }
}
fn js(s: impl Into<String>) -> Json { Json::Str(s.into()) }
fn jd(d: &PyDec) -> Json { js(s(d)) }
fn ds(a: &[PyDec]) -> Json { Json::Arr(a.iter().map(jd).collect()) }
fn tuple(a: Vec<Json>) -> Json { Json::Arr(a) }
fn terminal(s: OrderState) -> bool {
    matches!(s, OrderState::Filled | OrderState::Cancelled | OrderState::Rejected | OrderState::Expired)
}
fn sum(a: &[PyDec]) -> R<PyDec> { a.iter().try_fold(zero(), |n,v| add(&n,v)) }
fn floor(a: &PyDec) -> R<PyDec> { a.to_integral(Round::Floor).map_err(derr) }
fn quantity(equity: bool, q: &PyDec) -> R<()> {
    if equity && q.kind() == DKind::SNan { return Err(derr(DecErr::InvalidOperation)); }
    if equity && (q.kind() == DKind::QNan || !q.is_integral().map_err(derr)?) {
        return err("value",format!("Equity order quantity must be a whole number of shares, got {}",s(q)));
    }
    Ok(())
}
fn positive(q: &PyDec, message: String) -> R<()> {
    if !q.is_finite() || le(q,&zero())? { return err("value",message); }
    Ok(())
}
fn big_integer(q: &PyDec) -> R<BigInt> {
    if !q.is_finite() {
        return if q.is_nan() { err("value","cannot convert NaN to integer") }
            else { err("manager_int_overflow","cannot convert Infinity to integer") };
    }
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
fn allocate(q: &PyDec, w: &[PyDec], equity: bool) -> R<Vec<PyDec>> {
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
        let mut indices: Vec<usize> = (0..w.len()).collect();
        indices.sort_by_key(|&i| (-r[i].clone(),i));
        for &i in indices.iter().take(remaining) { a[i] += 1; }
        Ok(a.into_iter().map(|v| PyDec::from_parts(v.sign() == Sign::Minus,
            v.magnitude().clone(), 0)).collect())
    } else {
        // The private Python allocator propagated quiet NaNs; its one-portion
        // Infinity path never subtracts Infinity from Infinity.
        if q.kind() == DKind::QNan { return Ok(vec![q.clone(); w.len()]); }
        if q.kind() == DKind::Inf {
            return if w.len() == 1 && total.is_finite() { Ok(vec![q.clone()]) }
                else { Err(derr(DecErr::InvalidOperation)) };
        }
        let mut portions: Vec<PyDec> = w.iter().map(|v| div(&mul(q,v)?,&total)).collect::<R<_>>()?;
        let last = sub(q,&sum(&portions[..portions.len()-1])?)?;
        *portions.last_mut().expect("nonempty weights") = last;
        Ok(portions)
    }
}

fn native(t: &str, supported: &[Json], native_stops: bool) -> bool {
    (!(t == "STOP" || t == "STOP_LIMIT") || native_stops) &&
        supported.iter().any(|v| v == &js(t))
}
fn conflict(id: &str, message: &str) -> R<Json> {
    err("idempotency",format!("command_id '{id}' {message}"))
}
fn pending(id: &str) -> R<Json> {
    err("pending_reconciliation",format!("Order '{id}' is pending reconciliation"))
}
fn management(message: impl Into<String>) -> R<Json> { err("order_management",message) }
fn event_payload(j: &Json) -> R<Obj> { bridge::obj_from_text(&dumps(field(j,"payload")?)) }
fn payload_equal(a: &Json, b: &Json) -> R<bool> {
    if let (Some(da), Some(db))=(a.get("d"),b.get("d")) { return eq(&dec(da)?,&dec(db)?); }
    if let (Some(ta), Some(tb))=(a.get("T"),b.get("T")) {
        return Ok(crate::ledger::model::parse_datetime(text(ta)?)?.0
            .same(&crate::ledger::model::parse_datetime(text(tb)?)?.0));
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
fn created_replay(j: &Json) -> R<Json> {
    let e=field(j,"existing")?;
    let matches=st(e,"kind")?=="ORDERS_CREATED" && st(e,"account")?==st(j,"account")? &&
        matches!(event_payload(e)?,Obj::OrdersCreated(p) if p.fingerprint==st(j,"fingerprint")?);
    if !matches { return conflict(st(j,"command")?,"was already used for a different OMS command"); }
    Ok(Json::Null)
}

/// Each operation is a pure, narrow ownership boundary. Requests use the ledger's
/// existing exact carrier trees; returned decimal strings retain Python scale.
pub fn decide(op: &str, raw: &str) -> R<String> {
    let j = bridge::json_from_text(raw)?;
    Ok(dumps(&decision(op,&j)?))
}

fn decision(op: &str, j: &Json) -> R<Json> {
    match op {
        "created_replay" => created_replay(j),
        "replace_replay" => {
            let e=field(j,"existing")?;
            let mode=st(j,"mode")?;
            let matches=st(e,"account")?==st(j,"account")? &&
                if mode=="pending" {
                    st(e,"kind")?=="ORDER_PENDING" && matches!(event_payload(e)?,Obj::StateChange(p)
                        if p.order_id==st(j,"order")? && p.reason.as_deref()==Some(st(j,"reason")?))
                } else {
                    st(e,"kind")?=="ORDER_UPDATED" && matches!(event_payload(e)?,Obj::OrderUpdated(p)
                        if p.order.order_id==st(j,"order")? && p.reason==st(j,"reason")?)
                };
            if !matches {
                let what=if mode=="local" {"different emulated stop changes"} else {"different replace changes"};
                return conflict(st(j,"command")?,&format!("was replayed with {what}"));
            }
            if mode=="pending" && st(j,"state")?=="PENDING_UNKNOWN" {
                return err("pending_reconciliation",format!("Replace command '{}' remains pending reconciliation",st(j,"command")?));
            }
            Ok(Json::Null)
        }
        "observation_replay" => {
            let e=field(j,"existing")?;
            let matches=st(e,"kind")?=="ORDER_EMULATION_UPDATED" && st(e,"account")?==st(j,"account")? &&
                match event_payload(e)? {
                    Obj::Emulated(p) => p.order_id==st(j,"order")? &&
                        p.observed_price.as_ref().map(|v|eq(v,&d(j,"price")?)).transpose()?.unwrap_or(false),
                    _=>false,
                };
            if !matches { return conflict(st(j,"command")?,"was replayed with a different price observation"); }
            Ok(Json::Null)
        }
        "append_replay" => {
            let e=field(j,"existing")?;
            let matches=st(e,"account")?==st(j,"account")? && st(e,"kind")?==st(j,"kind")? &&
                payload_equal(field(e,"payload")?,field(j,"payload")?)?;
            if !matches { return conflict(st(j,"command")?,"was replayed with a different payload"); }
            Ok(Json::Null)
        }
        "entry_terms" => {
            let limit=if st(j,"type")?=="LIMIT" { field(j,"price")? } else {field(j,"limit")?};
            let stop=if st(j,"type")?=="LIMIT" { Json::Null } else {field(j,"price")?.clone()};
            Ok(tuple(vec![limit.clone(),stop,js(if st(j,"side")?=="BUY" {"SELL"}else{"BUY"})]))
        }
        "filter_types" => {
            let orders:Vec<Order>=array(field(j,"orders")?)?.iter().map(order).collect::<R<_>>()?;
            let types=array(field(j,"types")?)?;
            Ok(tuple(orders.iter().filter(|o|types.contains(&js(o.order_type.value()))).map(|o|js(&o.order_id)).collect()))
        }
        "submit_mode" => {
            let t=st(j,"type")?;
            let emulate=matches!(t,"STOP"|"STOP_LIMIT"|"TRAIL") &&
                !native(t,array(field(j,"types")?)?,flag(j,"native_stops")?);
            Ok(js(if emulate {"emulate"}else{"native"}))
        }
        "trailing_check" => {
            if st(j,"type")?!="TRAIL" {
                return err("value",format!("{} requires a TRAIL order",st(j,"method")?));
            }
            if st(j,"method")?=="update_trailing" && array(field(j,"types")?)?.contains(&js("TRAIL")) {
                return management(format!("Order '{}' is native at this venue; no local trail is running",st(j,"order")?));
            }
            Ok(Json::Null)
        }
        "emulation_check" => {
            let id=st(j,"order")?;
            if !flag(j,"emulated")? {
                return management(format!("Emulated order '{id}' has not been started"));
            }
            if native(st(j,"type")?,array(field(j,"types")?)?,flag(j,"native_stops")?) {
                return management(format!("Order '{id}' is native at this venue; no local emulation is running"));
            }
            Ok(Json::Null)
        }
        "observed_action" => {
            if !flag(j,"triggered")? { return Ok(js("observe")); }
            let id=st(j,"order")?;
            match st(j,"state")? {
                "PENDING_UNKNOWN" => err("pending_reconciliation",format!("Triggered emulated order '{id}' awaits venue reconciliation")),
                "NEW"=>Ok(js("route")),
                _=>Ok(js("return")),
            }
        }
        "observation_reason" => {
            let t=st(j,"type")?; let p=st(j,"price")?;
            Ok(js(if flag(j,"triggered")? {format!("{t} triggered at observed price {p}")} else {format!("Emulated {t} observed price {p}")}))
        }
        "start_emulation" => {
            let o=order(field(j,"order")?)?;
            if o.state==OrderState::PendingUnknown {
                return err("pending_reconciliation",format!("Emulated order '{}' is pending reconciliation",o.order_id));
            }
            if o.state!=OrderState::New {return Ok(js("return"));}
            if o.order_type==OrderType::StopLimit {
                if !array(field(j,"types")?)?.contains(&js("LIMIT")) {return Ok(js("refuse_limit"));}
                return Ok(js("limit"));
            }
            Ok(js("trigger"))
        }
        "submit_emulated" => {
            let o=order(field(j,"order")?)?;
            if o.state==OrderState::PendingUnknown {
                return err("pending_reconciliation",format!("Triggered order '{}' awaits venue reconciliation",o.order_id));
            }
            Ok(Json::Bool(o.state==OrderState::New))
        }
        "new" => Ok(Json::Bool(st(j,"state")?=="NEW")),
        "terminal" => Ok(Json::Bool(matches!(st(j,"state")?,"FILLED"|"CANCELLED"|"REJECTED"|"EXPIRED"))),
        "stop_rejected" => {
            if st(j,"type")?=="STOP" && st(j,"state")?=="REJECTED" {
                return management(format!("Protective stop '{}' was rejected; open quantity requires venue reconciliation",st(j,"order")?));
            }
            Ok(Json::Null)
        }
        "child_hold" => Ok(Json::Bool(le(&d(j,"filled")?,&zero())?)),
        "fill_match" => {
            if st(j,"account")?!=st(j,"fill_account")? || st(j,"env")?!=st(j,"fill_env")? {
                return management(format!("Fill '{}' account or venue environment does not match order '{}'",st(j,"fill")?,st(j,"order")?));
            }
            Ok(Json::Null)
        }
        "close_replay" => {
            let o=order(field(j,"order")?)?;
            if o.command_id!=st(j,"command")? {
                return err("idempotency",format!("Bracket '{}' already has close order '{}' from command '{}'",st(j,"entry")?,o.order_id,o.command_id));
            }
            Ok(Json::Bool(o.state!=OrderState::New))
        }
        "confirmed_id" => {
            if field(j,"venue")?==&Json::Null {
                let id=st(j,"order")?;
                return err("pending_reconciliation",if st(j,"mode")?=="replace" {
                    format!("Order '{id}' has no confirmed venue order id")
                } else {format!("Working order '{id}' has no venue id")});
            }
            Ok(Json::Null)
        }
        "cancel_mode" => {
            let state=st(j,"state")?;
            if matches!(state,"CANCELLED"|"FILLED"|"REJECTED"|"EXPIRED") {return Ok(js("return"));}
            if state=="PENDING_UNKNOWN" {
                if flag(j,"oco")? {
                    return err("oco_unknown",format!("OCO sibling '{}' is already pending reconciliation",st(j,"order")?));
                }
                return Ok(js("return"));
            }
            Ok(js(if state=="NEW" {"local"} else {"venue"}))
        }
        "submit_ack" => {
            let status=st(j,"status")?; let message=st(j,"message")?;
            let (kind,reason)=match status {
                "ACCEPTED"=>("ORDER_ACCEPTED","Venue accepted order".into()),
                "REJECTED"=>("ORDER_REJECTED",format!("Venue rejected order: {}",if message.is_empty() {"reason not supplied"}else{message})),
                "PENDING"=>("ORDER_PENDING",format!("Venue has not resolved order: {}",if message.is_empty() {"no status message"}else{message})),
                _=>return management(format!("Unrecognized venue submit status {}",crate::ledger::codec::py_repr(status))),
            };
            Ok(tuple(vec![js(kind),js(reason)]))
        }
        "replace_ack" => {
            let status=st(j,"status")?; let m=st(j,"message")?;
            let id=st(j,"order")?;
            // Preserved defect: any status other than PENDING/REJECTED is accepted.
            if status=="PENDING" {
                return Ok(tuple(vec![js("pending"),js(format!("Venue replace remains pending: {}",if m.is_empty() {"no status message"}else{m})),Json::Null]));
            }
            if status=="REJECTED" {
                let m=if m.is_empty() {"reason not supplied"}else{m};
                return Ok(tuple(vec![js("rejected"),js(format!("Venue rejected replace: {m}")),
                    tuple(vec![js("order_management"),js(format!("Venue rejected replace for '{id}': {m}"))])]));
            }
            Ok(tuple(vec![js("accepted"),js(format!("Venue confirmed replace: {}",st(j,"command")?)),Json::Null]))
        }
        "cancel_ack" => {
            let status=st(j,"status")?; let m=st(j,"message")?;
            let id=st(j,"order")?; let oco=flag(j,"oco")?;
            match status {
                "ACCEPTED"=>Ok(tuple(vec![js("accepted"),field(j,"reason")?.clone(),Json::Null])),
                "REJECTED"=>{
                    let message=format!("Venue rejected cancel for '{id}': {}",if m.is_empty() {"order may already have filled; reconcile required"}else{m});
                    Ok(tuple(vec![js("rejected"),js(&message),tuple(vec![js(if oco {"oco_unknown"}else{"pending_reconciliation"}),js(message)])]))
                }
                "PENDING"=>Ok(tuple(vec![js("pending"),js(format!("Venue cancel remains pending: {}",if m.is_empty() {"no status message"}else{m})),
                    if oco {tuple(vec![js("oco_unknown"),js(format!("Venue has not confirmed cancellation of OCO sibling '{id}'"))])}else{Json::Null}])),
                _=>management(format!("Unrecognized venue cancel status {}",crate::ledger::codec::py_repr(status))),
            }
        }
        "reconcile_find" => {
            let id=st(j,"order")?; let venue=st(j,"venue")?;
            let found=array(field(j,"ids")?)?.iter().position(|v|v==&js(venue)||v==&js(id));
            match found {
                Some(i)=>Ok(Json::Int(i as i128)),
                None=>err("order_reconciliation",format!("Venue has no read-back for order '{id}'; it remains {}",st(j,"state")?)),
            }
        }
        "reconcile_replace" => {
            if flag(j,"unresolved")? && !matches!(st(j,"found")?,"FILLED"|"CANCELLED"|"REJECTED"|"EXPIRED") {
                return err("order_reconciliation",format!("Pending replace terms for '{}' cannot be resolved from status-only venue read-back",st(j,"order")?));
            }
            Ok(Json::Null)
        }
        "pending_state" => Ok(Json::Bool(st(j,"state")?=="PENDING_UNKNOWN")),
        "reconcile_fills" => Ok(Json::Bool(gt(&d(j,"found")?,&d(j,"recorded")?)?)),
        "reconcile_result" => {
            let state=st(j,"found")?; let id=st(j,"order")?;
            match state {
                "FILLED" | "PARTIALLY_FILLED"=>{
                    if state=="FILLED" && st(j,"state")?!="FILLED" {
                        return err("order_reconciliation",format!("Venue reports '{id}' FILLED but its fill records do not complete it"));
                    }
                    Ok(js("return"))
                }
                "SUBMITTED"=>Ok(js("updated")),
                "ACCEPTED"=>Ok(js("ORDER_ACCEPTED")),
                "CANCELLED"=>Ok(js("ORDER_CANCELLED")),
                "REJECTED"=>Ok(js("ORDER_REJECTED")),
                "EXPIRED"=>Ok(js("ORDER_EXPIRED")),
                _=>err("order_reconciliation",format!("Venue state {state} does not resolve order '{id}'")),
            }
        }
        "ingest_check" => {
            if lt(&d(j,"recorded")?,&d(j,"found")?)? {
                return err("order_reconciliation",format!("Venue reports {} filled for '{}' but its fill records account for {}; it remains {}",st(j,"found")?,st(j,"order")?,st(j,"recorded")?,st(j,"state")?));
            }
            Ok(Json::Null)
        }
        "matching_ids" => {
            let venue=field(j,"venue")?;
            Ok(tuple(array(field(j,"ids")?)?.iter().enumerate().filter(|(_,v)|*v==venue)
                .map(|(i,_)|Json::Int(i as i128)).collect()))
        }
        "kind_is" => Ok(Json::Bool(st(j,"kind")?==st(j,"expected")?)),
        "type_is" => Ok(Json::Bool(st(j,"type")?==st(j,"expected")?)),
        "fraction_mode" => Ok(js(if field(j,"fractions")?==&Json::Null {"split"}else{"fractions"})),
        "parent" => Ok(field(j,"parent")?.clone()),
        "protective_child" => Ok(Json::Bool(field(j,"parent")?!=&Json::Null && st(j,"type")?=="STOP")),
        "route_limit" => Ok(if st(j,"type")?=="LIMIT" {field(j,"price")?.clone()}else{Json::Null}),
        "refused" => {
            let o=order(field(j,"order")?)?;
            let id=&o.order_id;
            let (reason,suffix,kind,message)=match st(j,"mode")? {
                "child"=>(format!("Child held until parent '{}' has a fill",st(j,"parent")?),
                    "child-held".into(),"order_management",format!("Child order '{id}' is held until its entry fills")),
                "protective"=>("Cannot cancel the only protective stop while the bracket has open quantity".into(),
                    "protective-stop-refused".into(),"order_management",format!("Cannot cancel protective stop '{id}' while its position is open")),
                "native"=>(format!("Venue does not support {}; no native order was sent",o.order_type.value()),
                    "unsupported-type".into(),"unsupported_order",format!("Venue does not support order type {}",o.order_type.value())),
                "limit"=>("Emulated STOP_LIMIT requires venue LIMIT support".into(),"no-stop-limit-fallback".into(),
                    "unsupported_order","Emulated STOP_LIMIT requires venue LIMIT support".into()),
                "trigger"=>("Emulated stop requires venue MARKET or LIMIT support when triggered".into(),
                    "no-trigger-order".into(),"unsupported_order","Emulated stops require MARKET or LIMIT capability".into()),
                "tif"=>(format!("Venue does not support time in force {}",o.tif.value()),
                    format!("unsupported-tif:{}",o.tif.value()),"unsupported_order",format!("Venue does not support time in force {}",o.tif.value())),
                _=>return err("value","unknown refusal plan"),
            };
            Ok(tuple(vec![js(reason),js(suffix),tuple(vec![js(kind),js(message)])]))
        }

        "oco_check" => {
            if !matches!(st(j,"state")?,"CANCELLED"|"FILLED"|"REJECTED"|"EXPIRED") {
                return err("oco_unknown",format!("Could not confirm cancellation of OCO sibling '{}'",st(j,"order")?));
            }
            Ok(Json::Null)
        }
        "pending_candidate" => {
            let e=field(j,"event")?;
            if !flag(e,"pending")? || st(e,"order")?!=st(j,"order")? ||
                field(e,"reason")?==&Json::Null || !st(e,"reason")?.starts_with("Replace pending:") ||
                field(e,"command")?==&Json::Null || !st(e,"command")?.ends_with(":pending") {return Ok(Json::Null);}
            Ok(js(st(e,"command")?.strip_suffix(":pending").expect("checked")))
        }
        "quantity" => { quantity(flag(j,"equity")?, &d(j,"quantity")?)?; Ok(Json::Null) }
        "positive_quantity" => {
            positive(&d(j,"quantity")?,"quantity must be finite and positive".into())?;
            Ok(Json::Null)
        }
        "price" => {
            let p=d(j,"price")?;
            positive(&p,format!("price must be finite and positive, got {}",s(&p)))?;
            Ok(Json::Null)
        }
        "fraction" => {
            let f=d(j,"fraction")?;
            if !f.is_finite() || le(&f,&zero())? || ge(&f,&PyDec::from_i128(1))? {
                return err("value",format!("fraction must be between 0 and 1 exclusive, got {}",s(&f)));
            }
            Ok(Json::Null)
        }
        "allocate" | "split" | "fractions" => {
            let q=d(j,"quantity")?;
            let equity=flag(j,"equity")?;
            let w: Vec<PyDec>=array(field(j,"weights")?)?.iter().map(dec).collect::<R<_>>()?;
            if op == "allocate" { return Ok(ds(&allocate(&q,&w,equity)?)); }
            if op == "split" && w.is_empty() { return Ok(tuple(vec![])); }
            quantity(equity,&q)?;
            if op == "split" {
                let portions=allocate(&q,&w,equity)?;
                for v in &portions {
                    if le(v,&zero())? {
                        return err("value","quantity is too small to allocate a positive amount to each target");
                    }
                }
                return Ok(ds(&portions));
            }
            let runner=sub(&PyDec::from_i128(1),&sum(&w)?)?;
            let mut weights=w.clone();
            if gt(&runner,&zero())? { weights.push(runner); }
            if q.kind() == DKind::QNan {
                return if w.is_empty() { Ok(tuple(vec![])) }
                    else { Err(derr(DecErr::InvalidOperation)) };
            }
            let exact: Vec<PyDec>=weights.iter().map(|v| mul(&q,v)).collect::<R<_>>()?;
            let mut portions=exact.clone();
            if equity {
                portions=exact.iter().map(floor).collect::<R<_>>()?;
                let remaining=big_integer(&sub(&q,&sum(&portions)?)?)?;
                let remainder: Vec<PyDec>=exact.iter().zip(&portions).map(|(a,b)|sub(a,b)).collect::<R<_>>()?;
                let mut indices: Vec<usize>=(0..weights.len()).collect();
                // The values here are finite; all comparisons are checked before sorting.
                let mut keys=Vec::new();
                for &i in &indices { keys.push(neg(&remainder[i])?); }
                indices.sort_by(|&a,&b|keys[a].cmp_ord(&keys[b]).expect("finite remainders").then(a.cmp(&b)));
                let end = if remaining.sign() == Sign::Minus {
                    (BigInt::from(indices.len()) + remaining).to_usize().unwrap_or(0)
                } else {
                    remaining.to_usize().unwrap_or(indices.len())
                };
                for &i in indices.iter().take(end) { portions[i]=add(&portions[i],&PyDec::from_i128(1))?; }
            }
            portions.truncate(w.len());
            for v in &portions {
                if le(v,&zero())? {
                    return err("value",format!("quantity {} is too small to give every target its fraction",s(&q)));
                }
            }
            Ok(ds(&portions))
        }
        "fingerprint" => Ok(js(sha256_hex(dumps(field(j,"payload")?).as_bytes()))),
        "reduce_fingerprint" => {
            let p=Json::Obj(vec![("action".into(),js("reduce")),
                ("entry_order_id".into(),field(j,"entry")?.clone()),
                ("fraction".into(),field(j,"fraction")?.clone()),
                ("reason".into(),field(j,"reason")?.clone())]);
            Ok(js(sha256_hex(dumps(&p).as_bytes())))
        }
        "bracket_fingerprint" => {
            let f=field(j,"intent")?;
            let mut fields=Vec::new();
            for key in ["intent_id","account_id","instrument","side","quantity_rule",
                        "entry_price","stop_loss","profit_targets","reason","command_id",
                        "entry_tif","exit_tif"] {
                fields.push((key.into(),field(f,key)?.clone()));
            }
            fields.push(("quantity".into(),field(j,"quantity")?.clone()));
            let t=st(f,"entry_type")?;
            if t != "LIMIT" { fields.push(("entry_type".into(),js(t))); }
            if t == "STOP_LIMIT" { fields.push(("entry_limit_price".into(),
                if field(f,"entry_limit_price")? == &Json::Null { js("None") } else { field(f,"entry_limit_price")?.clone() })); }
            if field(f,"target_fractions")? != &Json::Null {
                fields.push(("target_fractions".into(),field(f,"target_fractions")?.clone()));
            }
            Ok(js(sha256_hex(dumps(&Json::Obj(fields)).as_bytes())))
        }
        "native" => Ok(Json::Bool(native(st(j,"type")?,array(field(j,"types")?)?,flag(j,"native_stops")?))),
        "bracket_capabilities" => {
            let tifs=array(field(j,"tifs")?)?;
            let mut unsupported: Vec<&str>=["entry_tif","exit_tif"].iter().map(|k|st(j,k)).collect::<R<_>>()?;
            unsupported.retain(|t| !tifs.contains(&js(*t)));
            unsupported.sort(); unsupported.dedup();
            if !unsupported.is_empty() {
                return err("unsupported_order",format!("Venue does not support bracket time in force {}",unsupported.join(", ")));
            }
            let t=st(j,"type")?;
            if (t == "STOP" || t == "STOP_LIMIT") && !native(t,array(field(j,"types")?)?,flag(j,"native_stops")?) {
                let name=if t == "STOP" { "stop" } else { "stop-limit" };
                return err("unsupported_order",format!("Venue has no native {t} orders; a {name} entry cannot be worked"));
            }
            Ok(Json::Null)
        }
        "trigger_type" => {
            let types=array(field(j,"types")?)?;
            if types.contains(&js("MARKET")) { Ok(js("MARKET")) }
            else if types.contains(&js("LIMIT")) { Ok(js("LIMIT")) }
            else { Ok(Json::Null) } // host must append the refusal before raising
        }
        "tif" => {
            if !array(field(j,"tifs")?)?.contains(&js(st(j,"tif")?)) { return Ok(js("refuse")); }
            if field(j,"venue_type")? != &Json::Null && !array(field(j,"types")?)?.contains(field(j,"venue_type")?) {
                return err("unsupported_order",format!("Venue does not support trigger order type {}",st(j,"venue_type")?));
            }
            Ok(js("ok"))
        }
        "children" | "bracket" | "siblings" => {
            let orders: Vec<Order>=array(field(j,"orders")?)?.iter().map(order).collect::<R<_>>()?;
            let id=st(j,"entry")?;
            let children: Vec<&Order>=orders.iter().filter(|o|o.parent_order_id.as_deref()==Some(id)).collect();
            if op == "children" { return Ok(tuple(children.iter().map(|o|js(&o.order_id)).collect())); }
            if op == "siblings" {
                let o=order(field(j,"order")?)?;
                return Ok(tuple(orders.iter().filter(|c| c.order_id != o.order_id && o.parent_order_id.is_some() &&
                    o.oco_group.is_some() && c.parent_order_id == o.parent_order_id && c.oco_group == o.oco_group)
                    .map(|c|js(&c.order_id)).collect()));
            }
            let entry=orders.iter().find(|o|o.parent_order_id.is_none()).ok_or_else(|| LErr {kind:"stop_iteration",msg:String::new()})?;
            let stop=orders.iter().find(|o|o.parent_order_id.as_deref()==Some(entry.order_id.as_str()) && o.order_type==OrderType::Stop)
                .ok_or_else(|| LErr {kind:"stop_iteration",msg:String::new()})?;
            let mut targets: Vec<&Order>=orders.iter().filter(|o|o.parent_order_id.as_deref()==Some(entry.order_id.as_str()) && o.order_type==OrderType::Limit).collect();
            targets.sort_by(|a,b|a.order_id.cmp(&b.order_id));
            Ok(tuple(vec![js(&entry.order_id),js(&stop.order_id),tuple(targets.iter().map(|o|js(&o.order_id)).collect())]))
        }
        "is_reduce" => Ok(Json::Bool(st(j,"order_id")?.starts_with(&format!("{}:reduce:",st(j,"entry")?)))),
        "open_stop" | "sync" | "cancel_protective" => {
            let state=bridge::uncanon_account(field(j,"state")?)?;
            let entry=order(field(j,"entry")?)?;
            let entry=if op=="sync" {
                state.orders.get(&entry.order_id).cloned().ok_or_else(|| LErr {
                    kind:"key", msg:format!("Unknown order_id '{}'",entry.order_id)
                })?
            }else{entry};
            let children: Vec<&Order>=state.orders.values().filter(|o|o.parent_order_id.as_deref()==Some(entry.order_id.as_str())).collect();
            let filled=|id:&str|state.filled_quantity.get(id).cloned().unwrap_or_else(zero);
            let stop=children.iter().find(|o|o.order_type==OrderType::Stop).copied();
            if op == "open_stop" && entry.parent_order_id.is_some() {
                return management(format!("Order '{}' is not a bracket entry",entry.order_id));
            }
            if stop.is_none() {
                if op == "open_stop" { return management(format!("Order '{}' has no protective stop",entry.order_id)); }
                return Ok(Json::Null);
            }
            let stop=stop.expect("checked");
            let entry_filled=filled(&entry.order_id);
            let exited=sum(&children.iter().map(|o|filled(&o.order_id)).collect::<Vec<_>>())?;
            if op == "cancel_protective" {
                let o=order(field(j,"order")?)?;
                return Ok(Json::Bool(gt(&entry_filled,&exited)? && !terminal(o.state)));
            }
            let open=sub(&entry_filled,&exited)?;
            if op == "open_stop" {
                if le(&open,&zero())? { return management(format!("Bracket '{}' has no open quantity",entry.order_id)); }
                if !matches!(stop.state,OrderState::Accepted|OrderState::PartiallyFilled) {
                    return management(format!("Protective stop '{}' is {}, not working at the venue; reconcile before managing the bracket",stop.order_id,stop.state.value()));
                }
                return Ok(tuple(vec![js(&stop.order_id),jd(&open)]));
            }
            if eq(&entry_filled,&zero())? && !terminal(entry.state) { return Ok(Json::Null); }
            let targets: Vec<&Order>=children.iter().copied().filter(|o|o.order_type==OrderType::Limit).collect();
            let closers: Vec<&Order>=children.iter().copied().filter(|o|o.order_type==OrderType::Market).collect();
            let stop_filled=filled(&stop.order_id);
            let target_filled=sum(&targets.iter().chain(&closers).map(|o|filled(&o.order_id)).collect::<Vec<_>>())?;
            let open=sub(&sub(&entry_filled,&stop_filled)?,&target_filled)?;
            if lt(&open,&zero())? {
                return management(format!("Exit fills exceed entry fills for bracket '{}'; venue reconciliation required",entry.order_id));
            }
            // Plan the first I/O boundary only. Budget computation comes AFTER the stop
            // resize, so its possible refusal cannot move ahead of that durable operation.
            Ok(tuple(vec![js(&stop.order_id),ds(&[entry_filled,open,stop_filled]),
                Json::Bool(terminal(entry.state)),
                tuple(targets.iter().map(|o|js(&o.order_id)).collect()),
                tuple(closers.iter().map(|o|js(&o.order_id)).collect())]))
        }
        "sync_mode" => Ok(js(if gt(&d(j,"filled")?,&zero())? && gt(&d(j,"open")?,&zero())? { "protect" } else { "flat" })),
        "sync_targets" => Ok(Json::Bool(eq(&d(j,"stop_filled")?,&zero())? && flag(j,"terminal")?)),
        "positive" => Ok(Json::Bool(gt(&d(j,"quantity")?,&zero())?)),
        "target_weights" => {
            let w:Vec<PyDec>=array(field(j,"weights")?)?.iter().map(dec).collect::<R<_>>()?;
            let runner=sub(&d(j,"planned")?,&sum(&w)?)?;
            let mut weights=w;
            if gt(&runner,&zero())? { weights.push(runner); }
            Ok(ds(&weights))
        }
        "child_quantity" => {
            let o=order(field(j,"order")?)?;
            if matches!(o.state,OrderState::Cancelled|OrderState::Filled|OrderState::Rejected) {
                if o.state == OrderState::Rejected {
                    if o.order_type == OrderType::Stop {
                        return management(format!("Protective stop '{}' was rejected; open quantity requires venue reconciliation",o.order_id));
                    }
                    return Ok(tuple(vec![js("return"),Json::Null]));
                }
                return management(format!("Protective child '{}' is terminal in state {}",o.order_id,o.state.value()));
            }
            let q=d(j,"quantity")?;
            quantity(matches!(o.instrument,Instrument::Equity(_)),&q)?;
            let total=add(&d(j,"filled")?,&q)?;
            let mode=if eq(&o.quantity,&total)? {
                if o.state==OrderState::New { "submit" } else { "return" }
            } else if o.state==OrderState::New { "local" } else { "replace" };
            Ok(tuple(vec![js(mode),jd(&total)]))
        }
        "move_stop" => {
            let o=order(field(j,"order")?)?;
            let Some(current)=o.stop_price else { return management(format!("Protective stop '{}' has no stop price",o.order_id)); };
            let p=d(j,"price")?;
            if eq(&p,&current)? { return Ok(js("return")); }
            let loosens=if o.side==Side::Sell { lt(&p,&current)? } else { gt(&p,&current)? };
            if loosens {
                return management(format!("Moving stop '{}' from {} to {} would widen the bracket's risk; stops only tighten",o.order_id,s(&current),s(&p)));
            }
            Ok(js("replace"))
        }
        "reduce" | "close_guard" => {
            let id=st(j,"entry")?;
            let children:Vec<Order>=array(field(j,"children")?)?.iter().map(order).collect::<R<_>>()?;
            let reduces:Vec<&Order>=children.iter().filter(|o|o.order_id.starts_with(&format!("{id}:reduce:"))).collect();
            if op=="close_guard" {
                if reduces.iter().any(|o|!terminal(o.state)) {
                    return management(format!("Bracket '{id}' has a reduce order working; it must resolve before the bracket can be closed"));
                }
                return Ok(Json::Null);
            }
            if children.iter().any(|o|o.order_id==format!("{id}:close") && !terminal(o.state)) {
                return management(format!("Bracket '{id}' has a close order working; nothing is left to reduce"));
            }
            if reduces.iter().any(|o|!terminal(o.state)) {
                return management(format!("Bracket '{id}' already has a reduce order working"));
            }
            let open=d(j,"open")?; let f=d(j,"fraction")?;
            let q=floor(&mul(&open,&f)?)?;
            if lt(&q,&PyDec::from_i128(1))? {
                return management(format!("Reducing bracket '{id}' by {} of {} rounds down to nothing; refusing to guess a size",s(&f),s(&open)));
            }
            Ok(tuple(vec![jd(&q),js(format!("{id}:reduce:{}",reduces.len()+1))]))
        }
        "replace_quantity" => {
            let q=optional(j,"quantity")?;
            if let Some(q)=q {
                let filled=d(j,"filled")?;
                if !q.is_finite() || lt(&q,&filled)? || le(&q,&zero())? {
                    return err("value",format!("replacement quantity must be finite, positive, and at least filled quantity {}",s(&filled)));
                }
                quantity(flag(j,"equity")?,&q)?;
            }
            Ok(Json::Null)
        }
        "replace_state" => {
            let o=order(field(j,"order")?)?;
            if o.state==OrderState::New || terminal(o.state) {
                return management(format!("Cannot replace order '{}' in state {}",o.order_id,o.state.value()));
            }
            if o.state==OrderState::PendingUnknown { return pending(&o.order_id); }
            Ok(Json::Null)
        }
        "local_replace" => {
            let o=order(field(j,"order")?)?;
            Ok(Json::Bool(o.state==OrderState::New && matches!(o.order_type,OrderType::Stop|OrderType::StopLimit) &&
                flag(j,"emulated")? && !flag(j,"triggered")?))
        }
        "replace_terms" => {
            let o=order(field(j,"order")?)?;
            let mut updated=o.clone();
            if let Some(q)=optional(j,"quantity")? { updated.quantity=q; }
            if let Some(p)=optional(j,"limit")? { updated.limit_price=Some(p); }
            if let Some(p)=optional(j,"stop")? { updated.stop_price=Some(p); }
            Ok(tuple(vec![jd(&updated.quantity),
                updated.limit_price.as_ref().map_or(Json::Null,jd),
                updated.stop_price.as_ref().map_or(Json::Null,jd),
                Json::Bool(updated.same(&o))]))
        }
        "stored" => {
            let created=order(field(j,"created")?)?;
            let mut candidate=order(field(j,"candidate")?)?;
            candidate.state=OrderState::New;
            let matches=if created.parent_order_id.is_none() { candidate.same(&created) } else {
                let q=candidate.quantity.clone(); candidate.quantity=created.quantity.clone();
                candidate.same(&created) && le(&q,&created.quantity)?
            };
            if !matches {
                return err("idempotency",format!("order_id '{}' was reused with a different order payload",candidate.order_id));
            }
            Ok(Json::Null)
        }
        "replay" => {
            if !flag(j,"matches")? {
                return conflict(st(j,"command")?,st(j,"message")?);
            }
            Ok(Json::Null)
        }
        "trigger_price" => {
            let o=order(field(j,"order")?)?;
            if field(j,"price")? == &Json::Null {
                return management(format!("Triggered emulated order '{}' has no observed price",o.order_id));
            }
            Ok(Json::Bool(o.order_type==OrderType::StopLimit))
        }
        "stop_observation" => {
            let o=order(field(j,"order")?)?;
            let Some(stop)=o.stop_price else {
                return management(format!("Emulated {} order '{}' has no stop price",o.order_type.value(),o.order_id));
            };
            let p=d(j,"price")?;
            let triggered=if o.side==Side::Buy { ge(&p,&stop)? } else { le(&p,&stop)? };
            Ok(tuple(vec![jd(&stop),Json::Bool(triggered)]))
        }
        "unresolved" => {
            for e in array(field(j,"events")?)? {
                if !flag(e,"pending")? || st(e,"order")?!=st(j,"order")? { continue; }
                if field(e,"reason")?==&Json::Null || !st(e,"reason")?.starts_with("Replace pending:") { continue; }
                if field(e,"command")?==&Json::Null || !st(e,"command")?.ends_with(":pending") { continue; }
                if !flag(e,"accepted")? && !flag(e,"rejected")? { return Ok(Json::Bool(true)); }
            }
            Ok(Json::Bool(false))
        }
        _ => err("value",format!("unknown manager decision {op}")),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn p(s:&str)->PyDec {PyDec::parse(s).unwrap()}
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
    #[test]
    fn native_stop_flag_and_trigger_preference_are_independent() {
        let types=vec![js("MARKET"),js("LIMIT"),js("STOP")];
        assert!(!native("STOP",&types,false));
        assert!(native("LIMIT",&types,false));
        assert_eq!(decide("trigger_type",r#"{"types":["MARKET","LIMIT"]}"#).unwrap(),r#""MARKET""#);
    }
}

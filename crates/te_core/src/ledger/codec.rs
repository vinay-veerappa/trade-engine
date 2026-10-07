//! The ledger codec: decodes the JSON the Python codec writes and encodes it back,
//! byte for byte (docs/RUST_PORT.md P2a). Mirrors `trade_engine/ledger/codec.py`:
//! the tag order, the kwargs-before-constructor error order, and which refusals the
//! wrapper turns into `codec` (see [`super::model::wrap`]).

use super::json::{self, Json};
use super::model::*;
use crate::money::Money;

/// `repr()` of a str.
pub fn py_repr(s: &str) -> String {
    let q = if s.contains('\'') && !s.contains('"') { '"' } else { '\'' };
    let mut o = String::new();
    o.push(q);
    for c in s.chars() {
        match c {
            '\\' => o.push_str("\\\\"),
            '\n' => o.push_str("\\n"),
            '\r' => o.push_str("\\r"),
            '\t' => o.push_str("\\t"),
            c if c == q => {
                o.push('\\');
                o.push(c);
            }
            c if (c as u32) < 0x20 || c as u32 == 0x7f => o.push_str(&format!("\\x{:02x}", c as u32)),
            c => o.push(c),
        }
    }
    o.push(q);
    o
}

fn codec<T>(msg: impl Into<String>) -> R<T> {
    err("codec", msg)
}

fn repr_json(j: &Json) -> String {
    match j {
        Json::Str(s) => py_repr(s),
        Json::Int(i) => i.to_string(),
        Json::BigInt(s) | Json::Float(s) => s.clone(),
        Json::Bool(b) => {
            if *b {
                "True".into()
            } else {
                "False".into()
            }
        }
        Json::Null => "None".into(),
        Json::Arr(_) => "[...]".into(),
        Json::Obj(_) => "{...}".into(),
    }
}

// --- decode ----------------------------------------------------------------------------

fn iso_str(j: &Json) -> R<&str> {
    match j {
        Json::Str(s) => Ok(s),
        _ => err("type", "fromisoformat: argument must be str"),
    }
}

pub fn decode(node: &Json) -> R<Val> {
    match node {
        Json::Int(i) => return Ok(Val::Int(*i)),
        Json::BigInt(_) => return err("unsupported", "an integer beyond 128 bits"),
        Json::Str(s) => return Ok(Val::Str(s.clone())),
        Json::Bool(b) => return Ok(Val::Bool(*b)),
        Json::Arr(_) => return codec("A bare JSON list is not a valid encoded value"),
        Json::Obj(_) => {}
        other => return codec(format!("Encoded value must be an object, got {}", other.type_name())),
    }
    if node.get("n").is_some() {
        return Ok(Val::None);
    }
    if let Some(d) = node.get("d") {
        let parsed = match d {
            Json::Str(s) => Money::parse(s),
            Json::Int(i) => Money::from_i128(*i).ok(),
            Json::Bool(_) | Json::Float(_) | Json::BigInt(_) => {
                return err("unsupported", "a non-string Decimal literal")
            }
            _ => None,
        };
        return match parsed {
            Some(p) => Ok(Val::Dec(p)),
            None => codec(format!("Invalid Decimal literal {}", repr_json(d))),
        };
    }
    if let Some(t) = node.get("T") {
        let (dt, aware) = parse_datetime(iso_str(t)?)?;
        if !aware {
            return codec("Decoded datetime is naive; stored events must be UTC (I7)");
        }
        return Ok(Val::DateTime(dt));
    }
    if let Some(t) = node.get("D") {
        return Ok(Val::Date(parse_date(iso_str(t)?)?));
    }
    if let Some(name) = node.get("e") {
        let name_s = match name {
            Json::Str(s) => s.as_str(),
            Json::Arr(_) | Json::Obj(_) => return err("type", "unhashable type"),
            other => return codec(format!("Unknown enum type {}", repr_json(other))),
        };
        if !["Side", "OptionRight", "OrderType", "OrderState", "TimeInForce", "EventKind"].contains(&name_s) {
            return codec(format!("Unknown enum type '{name_s}' (I5)"));
        }
        let Some(v) = node.get("v") else {
            return err("key", "'v'");
        };
        let bad = || codec(format!("Invalid {name_s} value {}", repr_json(v)));
        let Json::Str(vs) = v else { return bad() };
        let out = match name_s {
            "Side" => Side::parse(vs).map(Val::Side),
            "OptionRight" => crate::options::Right::from_code(vs).map(Val::Right),
            "OrderType" => OrderType::parse(vs).map(Val::OrderType),
            "OrderState" => OrderState::parse(vs).map(Val::OrderState),
            "TimeInForce" => Tif::parse(vs).map(Val::Tif),
            _ => EventKind::parse(vs).map(Val::Kind),
        };
        return out.map_or_else(bad, Ok);
    }
    if let Some(t) = node.get("t") {
        return match t {
            Json::Arr(items) => Ok(Val::Tuple(items.iter().map(decode).collect::<R<_>>()?)),
            Json::Str(_) | Json::Obj(_) => err("unsupported", "a tuple tag over a string or object"),
            _ => err("type", format!("'{}' object is not iterable", t.type_name())),
        };
    }
    if let Some(m) = node.get("m") {
        let items = match m {
            Json::Arr(a) => a,
            Json::Str(_) | Json::Obj(_) => return err("unsupported", "a map tag over a string or object"),
            _ => return err("type", format!("'{}' object is not iterable", m.type_name())),
        };
        let mut out: Vec<(String, Val)> = Vec::new();
        for it in items {
            let pair = match it {
                Json::Arr(p) => p,
                Json::Str(_) | Json::Obj(_) => return err("unsupported", "a map entry that is a string or object"),
                _ => return err("type", format!("cannot unpack non-iterable {} object", it.type_name())),
            };
            if pair.len() != 2 {
                return err("value", "map entry must unpack to a key and a value");
            }
            let key = match &pair[0] {
                Json::Str(s) => s.clone(),
                Json::Arr(_) | Json::Obj(_) => return err("type", "unhashable type"),
                _ => return err("unsupported", "a non-string map key"),
            };
            let v = decode(&pair[1])?;
            match out.iter_mut().find(|(k, _)| *k == key) {
                Some(slot) => slot.1 = v,
                None => out.push((key, v)),
            }
        }
        return Ok(Val::Map(out));
    }
    if let Some(tag) = node.get("dc") {
        let tag_s = match tag {
            Json::Str(s) => s.as_str(),
            Json::Arr(_) | Json::Obj(_) => return err("type", "unhashable type"),
            other => return codec(format!("Unknown payload type tag {} (I5)", repr_json(other))),
        };
        let Some(tag_static) = TAGS.iter().find(|t| **t == tag_s).copied() else {
            return codec(format!("Unknown payload type tag '{tag_s}' (I5)"));
        };
        let Some(f) = node.get("f") else {
            return err("key", "'f'");
        };
        let Json::Obj(pairs) = f else {
            return err("attribute", format!("'{}' object has no attribute 'items'", f.type_name()));
        };
        let mut fields = Vec::with_capacity(pairs.len());
        for (name, value) in pairs {
            fields.push((name.clone(), decode(value)?));
        }
        return build(tag_static, fields).map_err(|e| wrap(tag_s, e));
    }
    let mut keys: Vec<&str> = match node {
        Json::Obj(p) => p.iter().map(|(k, _)| k.as_str()).collect(),
        _ => Vec::new(),
    };
    keys.sort_unstable();
    let shown: Vec<String> = keys.iter().map(|k| py_repr(k)).collect();
    codec(format!("Unrecognised encoded object with keys [{}]", shown.join(", ")))
}

const TAGS: [&str; 27] = [
    "Equity",
    "OptionContract",
    "Combo",
    "ComboLeg",
    "Fill",
    "Lot",
    "Signal",
    "RiskVerdict",
    "RiskControlChange",
    "RiskRuleResult",
    "Order",
    "OrderStateChange",
    "OrdersCreated",
    "OrderUpdated",
    "EmulatedOrderState",
    "CashFlow",
    "Mark",
    "VenueReconcile",
    "VenueHaltCleared",
    "EodRun",
    "OptionLifecycle",
    "CorporateAction",
    "MirrorAllocation",
    "MirrorQueued",
    "MirrorRefused",
    "MirrorAck",
    "MirrorFill",
];

fn obj(o: Obj) -> Val {
    Val::Obj(Box::new(o))
}

fn build(tag: &str, f: Vec<(String, Val)>) -> R<Val> {
    Ok(match tag {
        "Equity" => obj(Obj::Instr(build_equity(f)?)),
        "OptionContract" => obj(Obj::Instr(build_option(f)?)),
        "Combo" => obj(Obj::Instr(build_combo(f)?)),
        "ComboLeg" => obj(Obj::ComboLeg(build_combo_leg(f)?)),
        "Fill" => obj(Obj::Fill(build_fill(f)?)),
        "Lot" => obj(Obj::Lot(build_lot(f)?)),
        "Signal" => obj(Obj::Signal(build_signal(f)?)),
        "RiskVerdict" => obj(Obj::RiskVerdict(build_risk_verdict(f)?)),
        "RiskControlChange" => obj(Obj::RiskControl(build_risk_control(f)?)),
        "RiskRuleResult" => obj(Obj::RiskRule(build_risk_rule(f)?)),
        "Order" => obj(Obj::Order(build_order(f)?)),
        "OrderStateChange" => obj(Obj::StateChange(build_state_change(f)?)),
        "OrdersCreated" => obj(Obj::OrdersCreated(build_orders_created(f)?)),
        "OrderUpdated" => obj(Obj::OrderUpdated(build_order_updated(f)?)),
        "EmulatedOrderState" => obj(Obj::Emulated(build_emulated(f)?)),
        "CashFlow" => obj(Obj::CashFlow(build_cash_flow(f)?)),
        "Mark" => obj(Obj::Mark(build_mark(f)?)),
        "VenueReconcile" => obj(Obj::Reconcile(build_reconcile(f)?)),
        "VenueHaltCleared" => obj(Obj::HaltCleared(build_halt_cleared(f)?)),
        "EodRun" => obj(Obj::EodRun(build_eod_run(f)?)),
        "OptionLifecycle" => obj(Obj::Lifecycle(build_lifecycle(f)?)),
        "CorporateAction" => obj(Obj::Corp(build_corporate_action(f)?)),
        "MirrorAllocation" => obj(Obj::MAlloc(build_mirror_alloc(f)?)),
        "MirrorQueued" => obj(Obj::MQueued(build_mirror_queued(f)?)),
        "MirrorRefused" => obj(Obj::MRefused(build_mirror_refused(f)?)),
        "MirrorAck" => obj(Obj::MAck(build_mirror_ack(f)?)),
        "MirrorFill" => obj(Obj::MFill(build_mirror_fill(f)?)),
        other => return codec(format!("Unknown payload type tag '{other}' (I5)")),
    })
}

// --- encode ----------------------------------------------------------------------------

pub fn tagged(k: &str, v: Json) -> Json {
    Json::Obj(vec![(k.to_string(), v)])
}

pub fn jstr(s: &str) -> Json {
    Json::Str(s.to_string())
}

pub fn jnone() -> Json {
    tagged("n", Json::Bool(true))
}

pub fn jenum(name: &str, value: &str) -> Json {
    Json::Obj(vec![("e".into(), jstr(name)), ("v".into(), jstr(value))])
}

pub fn jdec(d: &Money) -> R<Json> {
    if !d.is_finite() {
        return codec(format!("Refusing to persist a non-finite Decimal: {} (I5)", d.canon()));
    }
    Ok(tagged("d", jstr(&d.canon())))
}

pub fn jodec(d: &Option<Money>) -> R<Json> {
    d.as_ref().map_or(Ok(jnone()), jdec)
}

pub fn jdt(d: &DateTime) -> Json {
    tagged("T", jstr(&d.iso()))
}

fn jodt(d: &Option<DateTime>) -> Json {
    d.as_ref().map_or_else(jnone, jdt)
}

fn jdate(d: &chrono::NaiveDate) -> Json {
    tagged("D", jstr(&date_iso(d)))
}

pub fn jostr(s: &Option<String>) -> Json {
    s.as_ref().map_or_else(jnone, |x| jstr(x))
}

pub fn jtuple(items: Vec<Json>) -> Json {
    tagged("t", Json::Arr(items))
}

fn jstrs(items: &[String]) -> Json {
    jtuple(items.iter().map(|s| jstr(s)).collect())
}

fn jmap(items: &[(String, Val)]) -> R<Json> {
    let mut out = Vec::new();
    for (k, v) in items {
        out.push(Json::Arr(vec![jstr(k), enc_val(v)?]));
    }
    Ok(tagged("m", Json::Arr(out)))
}

pub fn dc(tag: &str, fields: Vec<(&str, Json)>) -> Json {
    Json::Obj(vec![
        ("dc".into(), jstr(tag)),
        ("f".into(), Json::Obj(fields.into_iter().map(|(k, v)| (k.to_string(), v)).collect())),
    ])
}

pub fn enc_instrument(i: &Instrument) -> R<Json> {
    Ok(match i {
        Instrument::Equity(s) => dc("Equity", vec![("symbol", jstr(s))]),
        Instrument::Option(c) => dc(
            "OptionContract",
            vec![
                ("underlying", jstr(&c.underlying)),
                ("expiry", jdate(&c.expiry)),
                ("strike", jdec(&c.strike)?),
                ("right", jenum("OptionRight", c.right.code())),
                ("multiplier", Json::Int(c.multiplier)),
            ],
        ),
        Instrument::Combo(legs) => {
            let mut items = Vec::new();
            for l in legs {
                items.push(enc_leg(l)?);
            }
            dc("Combo", vec![("legs", jtuple(items))])
        }
        Instrument::Future(_) => return err("unsupported", "futures not supported in ledger codec"),
    })
}

fn enc_leg(l: &ComboLeg) -> R<Json> {
    Ok(dc(
        "ComboLeg",
        vec![
            ("contract", enc_instrument(&l.contract)?),
            ("ratio", Json::Int(l.ratio)),
            ("side", jenum("Side", l.side.value())),
        ],
    ))
}

pub fn enc_val(v: &Val) -> R<Json> {
    Ok(match v {
        Val::None => jnone(),
        Val::Bool(b) => Json::Bool(*b),
        Val::Int(i) => Json::Int(*i),
        Val::Str(s) => jstr(s),
        Val::Dec(d) => jdec(d)?,
        Val::DateTime(d) => jdt(d),
        Val::Date(d) => jdate(d),
        Val::Side(s) => jenum("Side", s.value()),
        Val::Right(r) => jenum("OptionRight", r.code()),
        Val::OrderType(t) => jenum("OrderType", t.value()),
        Val::OrderState(s) => jenum("OrderState", s.value()),
        Val::Tif(t) => jenum("TimeInForce", t.value()),
        Val::Kind(k) => jenum("EventKind", k.value()),
        Val::Tuple(items) => jtuple(items.iter().map(enc_val).collect::<R<_>>()?),
        Val::Map(m) => jmap(m)?,
        Val::Obj(o) => enc_obj(o)?,
    })
}

pub fn enc_order(o: &Order) -> R<Json> {
    Ok(dc(
        "Order",
        vec![
            ("order_id", jstr(&o.order_id)),
            ("account_id", jstr(&o.account_id)),
            ("instrument", enc_instrument(&o.instrument)?),
            ("order_type", jenum("OrderType", o.order_type.value())),
            ("side", jenum("Side", o.side.value())),
            ("quantity", jdec(&o.quantity)?),
            ("command_id", jstr(&o.command_id)),
            ("created_at", jdt(&o.created_at)),
            ("limit_price", jodec(&o.limit_price)?),
            ("stop_price", jodec(&o.stop_price)?),
            ("trail_amount", jodec(&o.trail_amount)?),
            ("tif", jenum("TimeInForce", o.tif.value())),
            ("state", jenum("OrderState", o.state.value())),
            ("parent_order_id", jostr(&o.parent_order_id)),
            ("oco_group", jostr(&o.oco_group)),
        ],
    ))
}

fn enc_rule(r: &RiskRuleResult) -> R<Json> {
    Ok(dc(
        "RiskRuleResult",
        vec![
            ("rule_name", jstr(&r.rule_name)),
            ("passed", Json::Bool(r.passed)),
            ("measured_value", enc_val(&r.measured_value)?),
            ("threshold", enc_val(&r.threshold)?),
            ("reason", jstr(&r.reason)),
        ],
    ))
}

fn enc_alloc(a: &MirrorAllocation) -> R<Json> {
    Ok(dc(
        "MirrorAllocation",
        vec![
            ("strategy_order_id", jstr(&a.strategy_order_id)),
            ("strategy_account", jstr(&a.strategy_account)),
            ("quantity", jdec(&a.quantity)?),
        ],
    ))
}

pub fn enc_obj(o: &Obj) -> R<Json> {
    Ok(match o {
        Obj::Instr(i) => enc_instrument(i)?,
        Obj::ComboLeg(l) => enc_leg(l)?,
        Obj::Order(x) => enc_order(x)?,
        Obj::RiskRule(r) => enc_rule(r)?,
        Obj::Fill(f) => dc(
            "Fill",
            vec![
                ("fill_id", jstr(&f.fill_id)),
                ("order_id", jstr(&f.order_id)),
                ("account_id", jstr(&f.account_id)),
                ("instrument", enc_instrument(&f.instrument)?),
                ("quantity", jdec(&f.quantity)?),
                ("price", jdec(&f.price)?),
                ("venue_env", jstr(&f.venue_env)),
                ("filled_at", jdt(&f.filled_at)),
                ("side", jenum("Side", f.side.value())),
                ("fee", jdec(&f.fee)?),
                ("leg_id", jostr(&f.leg_id)),
                ("venue_order_id", jostr(&f.venue_order_id)),
                ("venue_execution_id", jostr(&f.venue_execution_id)),
            ],
        ),
        Obj::Lot(l) => dc(
            "Lot",
            vec![
                ("lot_id", jstr(&l.lot_id)),
                ("quantity", jdec(&l.quantity)?),
                ("cost_basis", jdec(&l.cost_basis)?),
                ("acquired_at", jdt(&l.acquired_at)),
                ("side", jenum("Side", l.side.value())),
            ],
        ),
        Obj::Signal(s) => dc(
            "Signal",
            vec![
                ("signal_id", jstr(&s.signal_id)),
                ("scan_id", jstr(&s.scan_id)),
                ("symbol", jstr(&s.symbol)),
                ("session_date", jdate(&s.session_date)),
                ("direction", jstr(&s.direction)),
                ("metrics", jmap(&s.metrics)?),
                ("next_earnings_date", s.next_earnings_date.as_ref().map_or_else(jnone, jdate)),
                ("created_at", jodt(&s.created_at)),
            ],
        ),
        Obj::RiskVerdict(v) => dc(
            "RiskVerdict",
            vec![
                ("order_intent_id", jstr(&v.order_intent_id)),
                ("accepted", Json::Bool(v.accepted)),
                ("evaluations", jtuple(v.evaluations.iter().map(enc_rule).collect::<R<_>>()?)),
                ("refusal_reasons", jstrs(&v.refusal_reasons)),
                ("approved_quantity", jodec(&v.approved_quantity)?),
            ],
        ),
        Obj::RiskControl(c) => dc(
            "RiskControlChange",
            vec![
                ("control_id", jstr(&c.control_id)),
                ("enabled", Json::Bool(c.enabled)),
                ("reason", jstr(&c.reason)),
                ("changed_at", jdt(&c.changed_at)),
            ],
        ),
        Obj::StateChange(s) => dc(
            "OrderStateChange",
            vec![
                ("order_id", jstr(&s.order_id)),
                ("reason", jostr(&s.reason)),
                ("venue_order_id", jostr(&s.venue_order_id)),
            ],
        ),
        Obj::OrdersCreated(c) => dc(
            "OrdersCreated",
            vec![
                ("orders", jtuple(c.orders.iter().map(enc_order).collect::<R<_>>()?)),
                ("fingerprint", jstr(&c.fingerprint)),
                ("reason", jstr(&c.reason)),
            ],
        ),
        Obj::OrderUpdated(u) => dc(
            "OrderUpdated",
            vec![
                ("order", enc_order(&u.order)?),
                ("reason", jstr(&u.reason)),
                ("venue_order_id", jostr(&u.venue_order_id)),
            ],
        ),
        Obj::Emulated(e) => dc(
            "EmulatedOrderState",
            vec![
                ("order_id", jstr(&e.order_id)),
                ("observed_price", jodec(&e.observed_price)?),
                ("extreme", jodec(&e.extreme)?),
                ("stop_price", jodec(&e.stop_price)?),
                ("triggered", Json::Bool(e.triggered)),
                ("reason", jstr(&e.reason)),
            ],
        ),
        Obj::CashFlow(c) => dc(
            "CashFlow",
            vec![
                ("amount", jdec(&c.amount)?),
                ("kind", jstr(&c.kind)),
                ("as_of", jdt(&c.as_of)),
                ("note", jostr(&c.note)),
            ],
        ),
        Obj::Mark(m) => dc(
            "Mark",
            vec![
                ("instrument", enc_instrument(&m.instrument)?),
                ("price", jdec(&m.price)?),
                ("as_of", jdt(&m.as_of)),
                ("source", jostr(&m.source)),
            ],
        ),
        Obj::Reconcile(r) => dc(
            "VenueReconcile",
            vec![
                ("venue", jstr(&r.venue)),
                ("as_of", jdt(&r.as_of)),
                ("reconciled", Json::Bool(r.reconciled)),
                ("drift", jstrs(&r.drift)),
                ("note", jostr(&r.note)),
            ],
        ),
        Obj::HaltCleared(h) => dc(
            "VenueHaltCleared",
            vec![
                ("venue", jstr(&h.venue)),
                ("at", jdt(&h.at)),
                ("reason", jstr(&h.reason)),
                ("reconcile_seq", Json::Int(h.reconcile_seq)),
            ],
        ),
        Obj::EodRun(r) => dc(
            "EodRun",
            vec![
                ("session", jdate(&r.session)),
                ("job", jstr(&r.job)),
                ("account_id", jstr(&r.account_id)),
                ("bars_processed", Json::Int(r.bars_processed)),
                ("at_close", jdt(&r.at_close)),
            ],
        ),
        Obj::Lifecycle(l) => dc(
            "OptionLifecycle",
            vec![
                ("account_id", jstr(&l.account_id)),
                ("contract", enc_instrument(&Instrument::Option(l.contract.clone()))?),
                ("quantity", jdec(&l.quantity)?),
                ("held", jenum("Side", l.held.value())),
                ("underlying_price", jdec(&l.underlying_price)?),
                ("price_source", jstr(&l.price_source)),
                ("as_of", jdt(&l.as_of)),
                ("reason", jstr(&l.reason)),
                ("early", Json::Bool(l.early)),
            ],
        ),
        Obj::Corp(c) => dc(
            "CorporateAction",
            vec![
                ("symbol", jstr(&c.symbol)),
                ("action_type", jstr(&c.action_type)),
                ("effective_date", jdate(&c.effective_date)),
                ("as_of", jdt(&c.as_of)),
                ("details", jmap(&c.details)?),
            ],
        ),
        Obj::MAlloc(a) => enc_alloc(a)?,
        Obj::MQueued(q) => dc(
            "MirrorQueued",
            vec![
                ("venue", jstr(&q.venue)),
                ("ticket_key", jstr(&q.ticket_key)),
                ("instrument", enc_instrument(&q.instrument)?),
                ("side", jenum("Side", q.side.value())),
                ("quantity", jdec(&q.quantity)?),
                ("order_type", jenum("OrderType", q.order_type.value())),
                ("limit_price", jodec(&q.limit_price)?),
                ("tif", jenum("TimeInForce", q.tif.value())),
                ("allocations", jtuple(q.allocations.iter().map(enc_alloc).collect::<R<_>>()?)),
                ("at", jdt(&q.at)),
            ],
        ),
        Obj::MRefused(r) => dc(
            "MirrorRefused",
            vec![
                ("venue", jstr(&r.venue)),
                ("strategy_order_id", jstr(&r.strategy_order_id)),
                ("strategy_account", jstr(&r.strategy_account)),
                ("reason", jstr(&r.reason)),
                ("at", jdt(&r.at)),
            ],
        ),
        Obj::MAck(a) => dc(
            "MirrorAck",
            vec![
                ("venue", jstr(&a.venue)),
                ("ticket_key", jstr(&a.ticket_key)),
                ("status", jstr(&a.status)),
                ("message", jstr(&a.message)),
                ("at", jdt(&a.at)),
                ("venue_order_id", jostr(&a.venue_order_id)),
                ("book_status", a.book_status.map_or_else(jnone, |s| jenum("OrderState", s.value()))),
            ],
        ),
        Obj::MFill(f) => dc(
            "MirrorFill",
            vec![
                ("venue", jstr(&f.venue)),
                ("ticket_key", jstr(&f.ticket_key)),
                ("venue_order_id", jstr(&f.venue_order_id)),
                ("filled", jdec(&f.filled)?),
                ("avg_price", jdec(&f.avg_price)?),
                ("at", jdt(&f.at)),
            ],
        ),
    })
}

// --- events ----------------------------------------------------------------------------

pub fn encode_event(e: &Event) -> R<Json> {
    Ok(Json::Obj(vec![
        ("account".into(), jstr(&e.account)),
        ("kind".into(), jstr(e.kind.value())),
        ("payload".into(), enc_obj(&e.payload)?),
        ("ts_utc".into(), jstr(&e.ts_utc.to_utc()?.iso())),
        ("command_id".into(), e.command_id.as_ref().map_or(Json::Null, |c| jstr(c))),
        ("schema_version".into(), Json::Int(e.schema_version)),
        ("seq".into(), e.seq.map_or(Json::Null, Json::Int)),
    ]))
}

pub fn decode_event(j: &Json) -> R<Event> {
    let Json::Obj(_) = j else {
        return err("type", format!("{} indices must be integers or slices, not str", j.type_name()));
    };
    let Some(account) = j.get("account") else {
        return err("key", "'account'");
    };
    let Some(kind) = j.get("kind") else {
        return err("key", "'kind'");
    };
    let kind = match kind {
        Json::Str(s) => EventKind::parse(s)
            .map_or_else(|| err("value", format!("{} is not a valid EventKind", py_repr(s))), Ok)?,
        other => return err("value", format!("{} is not a valid EventKind", repr_json(other))),
    };
    let Some(payload) = j.get("payload") else {
        return err("key", "'payload'");
    };
    let payload = decode(payload)?;
    let Some(ts) = j.get("ts_utc") else {
        return err("key", "'ts_utc'");
    };
    let (ts_utc, aware) = parse_datetime(iso_str(ts)?)?;
    let command_raw = j.get("command_id");
    let schema_version = match j.get("schema_version") {
        None => 1,
        Some(Json::Int(i)) => *i,
        Some(Json::Bool(b)) => *b as i128,
        Some(Json::Str(s)) => match s.trim().parse::<i128>() {
            Ok(i) => i,
            Err(_) => return err("value", format!("invalid literal for int() with base 10: {}", py_repr(s))),
        },
        Some(Json::Float(_)) | Some(Json::BigInt(_)) => return err("unsupported", "a non-integer schema_version"),
        Some(other) => {
            return err("type", format!("int() argument must be a string or a number, not '{}'", other.type_name()))
        }
    };
    let seq_raw = j.get("seq");
    // Event.__post_init__ runs its checks in this order
    if account_falsy(account) {
        return err("payload", "Event.account must be non-empty");
    }
    let Json::Str(account) = account else {
        return err("unsupported", "a non-string account");
    };
    let account = account.clone();
    if !aware {
        return err("payload", "Event.ts_utc must be timezone-aware (I7)");
    }
    let command_id = match command_raw {
        None | Some(Json::Null) => None,
        Some(Json::Str(s)) if s.is_empty() => {
            return err("payload", "Event.command_id must be non-empty when provided (I3)")
        }
        Some(Json::Str(s)) => Some(s.clone()),
        Some(other) if account_falsy(other) => {
            return err("payload", "Event.command_id must be non-empty when provided (I3)")
        }
        Some(_) => return err("unsupported", "a non-string command_id"),
    };
    if schema_version <= 0 {
        return err("payload", format!("Event.schema_version must be a positive int, got {schema_version}"));
    }
    let seq = match seq_raw {
        None | Some(Json::Null) => None,
        Some(Json::Int(i)) => Some(*i),
        Some(Json::Bool(false)) => return err("payload", "Event.seq must be a positive int, got False"),
        Some(Json::Bool(true)) => return err("unsupported", "a boolean seq"),
        Some(other) => {
            if schema_version > SCHEMA_VERSION {
                return err("payload", format!("Event.schema_version {schema_version} is newer than this engine understands ({SCHEMA_VERSION}); refusing rather than mis-reading it (I5)"));
            }
            return err("payload", format!("Event.seq must be a positive int, got {}", repr_json(other)));
        }
    };
    let payload = match payload {
        Val::Obj(o) => *o,
        other => {
            return err(
                "payload",
                format!("{} payload must be {}, got {}", kind.value(), expected_payload(kind), other.type_name()),
            )
        }
    };
    let ev = Event { account, kind, payload, ts_utc: ts_utc.to_utc()?, command_id, schema_version, seq };
    check_event(&ev)?;
    Ok(ev)
}

/// Python truthiness of a JSON value, for the `not x` checks.
fn account_falsy(j: &Json) -> bool {
    match j {
        Json::Null | Json::Bool(false) => true,
        Json::Int(0) => true,
        Json::Str(s) => s.is_empty(),
        Json::Arr(a) => a.is_empty(),
        Json::Obj(o) => o.is_empty(),
        Json::Float(f) => f.parse::<f64>().map(|x| x == 0.0).unwrap_or(false),
        _ => false,
    }
}

/// Decode one stored event (the compact JSON `json.dumps` wrote) from bytes.
pub fn event_from_bytes(bytes: &[u8]) -> R<Event> {
    let text = std::str::from_utf8(bytes).map_err(|e| LErr { kind: "value", msg: e.to_string() })?;
    let j = json::parse(text).map_err(|e| LErr {
        kind: if e.unsupported { "unsupported" } else { "json" },
        msg: e.message,
    })?;
    decode_event(&j)
}

/// Decode then encode: the bytes the Python codec would write back.
pub fn reencode(bytes: &[u8]) -> R<Vec<u8>> {
    let ev = event_from_bytes(bytes)?;
    Ok(json::dumps(&encode_event(&ev)?).into_bytes())
}

pub fn event_to_bytes(e: &Event) -> R<Vec<u8>> {
    Ok(json::dumps(&encode_event(e)?).into_bytes())
}

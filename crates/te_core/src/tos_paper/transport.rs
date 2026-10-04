//! The mirror's ticket decisions (was `tos_paper/transport.py`): what a ticket may hold
//! (the dataclass validators) and how one venue order becomes a ticket or is refused as
//! unsupported (`ticket_for`). The transport protocols and the three transport exceptions
//! are host shapes and stay Python.
//!
//! A ticket crosses as JSON: `{"kind": "option", "symbol", "side", "quantity", "order_type",
//! "limit_price", "tif", "underlying", "expiry", "strike", "right"}`, the same with
//! `"kind": "stock"` and only `symbol`..`tif`, or `{"kind": "combo", "underlying", "legs",
//! "quantity", "order_type", "limit_price", "price_effect", "tif"}`. Every field of a
//! validated ticket may be any JSON value (the validators `repr` what they refuse).

use super::netting::vertical_reason;
use super::pytext::repr;
use super::wire::{
    instrument, int_json, jopt_dec, jstr, obj, opt_dec, req, req_arr, req_dec, req_str, wire, ComboLeg, Instrument,
    OptionC,
};
use super::{UNSUPPORTED, VALUE};
use crate::ledger::json::Json;
use crate::ledger::model::{derr, err, R};
use crate::ledger::pydec::PyDec;
use crate::options::equity_symbol;

fn value<T>(msg: impl Into<String>) -> R<T> {
    err(VALUE, msg)
}

fn unsupported<T>(msg: impl Into<String>) -> R<T> {
    err(UNSUPPORTED, msg)
}

fn is_text(v: &Json, allowed: &[&str]) -> bool {
    matches!(v, Json::Str(s) if allowed.contains(&s.as_str()))
}

/// `isinstance(v, int) and not isinstance(v, bool) and v > 0`.
fn is_positive_int(v: &Json) -> bool {
    match v {
        Json::Int(n) => *n > 0,
        Json::BigInt(t) => !t.starts_with('-'),
        _ => false,
    }
}

/// `limit_price is None or limit_price <= 0` (a NaN limit refuses: `InvalidOperation`).
fn missing_or_nonpositive(limit: &Option<PyDec>) -> R<bool> {
    match limit {
        None => Ok(true),
        Some(d) => d.le(&PyDec::zero()).map_err(derr),
    }
}

fn field<'a>(doc: &'a Json, key: &str) -> R<&'a Json> {
    req(doc, key)
}

/// The checks `MirrorTicket` and `MirrorStockTicket` share, in the dataclass's order.
fn check_single(doc: &Json) -> R<()> {
    let side = field(doc, "side")?;
    if !is_text(side, &["BUY", "SELL"]) {
        return value(format!("ticket side must be BUY or SELL, got {}", repr(side)));
    }
    let quantity = field(doc, "quantity")?;
    if !is_positive_int(quantity) {
        return value(format!("ticket quantity must be a positive int, got {}", repr(quantity)));
    }
    let order_type = field(doc, "order_type")?;
    if !is_text(order_type, &["MKT", "LMT"]) {
        return value(format!("ticket order_type must be MKT or LMT, got {}", repr(order_type)));
    }
    let limit = opt_dec(doc, "limit_price")?;
    if is_text(order_type, &["LMT"]) && missing_or_nonpositive(&limit)? {
        return value("an LMT ticket needs a positive limit_price (I5)");
    }
    if is_text(order_type, &["MKT"]) && limit.is_some() {
        return value("a MKT ticket cannot carry a limit_price");
    }
    let tif = field(doc, "tif")?;
    if !is_text(tif, &["DAY", "GTC"]) {
        return value(format!("ticket tif must be DAY or GTC, got {}", repr(tif)));
    }
    Ok(())
}

fn check_stock(doc: &Json) -> R<()> {
    let symbol = field(doc, "symbol")?;
    let refused = || {
        value::<()>(format!("stock ticket symbol must be an upper-case equity symbol, got {}", repr(symbol)))
    };
    let parsed = match symbol {
        Json::Str(s) => equity_symbol(Some(s)),
        _ => equity_symbol(None),
    };
    match (parsed, symbol) {
        (Ok(p), Json::Str(s)) if &p == s => {}
        _ => return refused(),
    }
    check_single(doc)
}

fn check_leg(doc: &Json) -> R<()> {
    let side = field(doc, "side")?;
    if !is_text(side, &["BUY", "SELL"]) {
        return value(format!("leg side must be BUY or SELL, got {}", repr(side)));
    }
    let ratio = field(doc, "ratio")?;
    if !is_positive_int(ratio) {
        return value(format!("leg ratio must be a positive int, got {}", repr(ratio)));
    }
    Ok(())
}

fn check_combo(doc: &Json) -> R<()> {
    let legs = req_arr(doc, "legs")?;
    let sides: Vec<&Json> = legs.iter().filter_map(|l| l.get("side")).collect();
    let bought = sides.iter().any(|s| is_text(s, &["BUY"]));
    let sold = sides.iter().any(|s| is_text(s, &["SELL"]));
    // `{leg.side for leg in legs} != {"BUY", "SELL"}`: with two legs, one of each.
    let one_each = legs.len() == 2 && bought && sold;
    if legs.len() != 2 || !one_each {
        return value("a combo ticket is a vertical: one leg bought, one sold");
    }
    let ratios: Vec<&Json> = legs.iter().filter_map(|l| l.get("ratio")).collect();
    if ratios.iter().any(|r| *r != ratios[0]) {
        return value("a vertical's legs trade in equal ratio");
    }
    let quantity = field(doc, "quantity")?;
    if !is_positive_int(quantity) {
        return value(format!("ticket quantity must be a positive int, got {}", repr(quantity)));
    }
    let limit = opt_dec(doc, "limit_price")?;
    if !is_text(field(doc, "order_type")?, &["LMT"]) || missing_or_nonpositive(&limit)? {
        return value("a vertical ticket is LMT with a positive net limit_price (I5)");
    }
    let effect = field(doc, "price_effect")?;
    if !is_text(effect, &["CREDIT", "DEBIT"]) {
        return value(format!("price_effect must be CREDIT or DEBIT, got {}", repr(effect)));
    }
    let tif = field(doc, "tif")?;
    if !is_text(tif, &["DAY", "GTC"]) {
        return value(format!("ticket tif must be DAY or GTC, got {}", repr(tif)));
    }
    Ok(())
}

/// The door's `ticket_validate`: a ticket (or one combo leg) as the dataclass would build
/// it, or its `ValueError`. `{"kind": "option" | "stock" | "leg" | "combo", ...fields}`.
pub fn ticket_validate(doc: &Json) -> R<Json> {
    match req_str(doc, "kind")? {
        "option" => check_single(doc)?,
        "stock" => check_stock(doc)?,
        "leg" => check_leg(doc)?,
        "combo" => check_combo(doc)?,
        other => return wire(format!("unknown ticket kind {other:?}")),
    }
    Ok(obj(vec![("valid", Json::Bool(true))]))
}

fn ticket_type(order_type: &str) -> Option<&'static str> {
    match order_type {
        "MARKET" => Some("MKT"),
        "LIMIT" => Some("LMT"),
        _ => None,
    }
}

fn ticket_tif(tif: &str) -> Option<&'static str> {
    match tif {
        "DAY" => Some("DAY"),
        "GTC" => Some("GTC"),
        _ => None,
    }
}

fn whole(q: &PyDec) -> R<bool> {
    q.is_integral().map_err(derr)
}

struct Order {
    order_type: String,
    tif: String,
    quantity: PyDec,
    side: String,
    limit_price: Option<PyDec>,
}

/// The door's `ticket_for`: `{"instrument", "order_type", "tif", "quantity", "side",
/// "limit_price"}` (the venue order's `.value`s and Decimal text) -> the ticket, or
/// `UnsupportedCapability`, or the ticket's own `ValueError`.
pub fn ticket_for(doc: &Json) -> R<Json> {
    let inst = instrument(req(doc, "instrument")?)?;
    let order = Order {
        order_type: req_str(doc, "order_type")?.to_string(),
        tif: req_str(doc, "tif")?.to_string(),
        quantity: req_dec(doc, "quantity")?,
        side: req_str(doc, "side")?.to_string(),
        limit_price: opt_dec(doc, "limit_price")?,
    };
    match inst {
        Instrument::Combo(legs) => combo_ticket(&order, &legs),
        Instrument::Equity { symbol } => stock_ticket(&order, &symbol),
        Instrument::Option(contract) => option_ticket(&order, &contract),
        Instrument::Other { repr } => {
            unsupported(format!("{repr}: only single option contracts, shares and verticals are mirrored (§4.7)"))
        }
    }
}

/// The checks every single-instrument order passes before a ticket: type, TIF, whole size.
fn single_checks(order: &Order, noun: &str) -> R<()> {
    if ticket_type(&order.order_type).is_none() {
        return unsupported(format!("order type {}: MARKET/LIMIT only", order.order_type));
    }
    if ticket_tif(&order.tif).is_none() {
        return unsupported(format!("TIF {}: DAY/GTC only", order.tif));
    }
    if !whole(&order.quantity)? {
        return unsupported(format!("quantity {} is not a whole number of {noun} (I5)", order.quantity.to_py_string()));
    }
    Ok(())
}

fn limit_for(order: &Order) -> Json {
    if order.order_type == "LIMIT" {
        jopt_dec(&order.limit_price)
    } else {
        Json::Null
    }
}

fn stock_ticket(order: &Order, symbol: &str) -> R<Json> {
    single_checks(order, "shares")?;
    let ticket = obj(vec![
        ("kind", jstr("stock")),
        ("symbol", jstr(symbol)),
        ("side", jstr(order.side.clone())),
        ("quantity", int_json(&order.quantity)?),
        ("order_type", jstr(ticket_type(&order.order_type).expect("checked"))),
        ("limit_price", limit_for(order)),
        ("tif", jstr(ticket_tif(&order.tif).expect("checked"))),
    ]);
    check_stock(&ticket)?;
    Ok(ticket)
}

fn option_ticket(order: &Order, contract: &OptionC) -> R<Json> {
    single_checks(order, "contracts")?;
    let ticket = obj(vec![
        ("kind", jstr("option")),
        ("symbol", jstr(contract.occ()?)),
        ("side", jstr(order.side.clone())),
        ("quantity", int_json(&order.quantity)?),
        ("order_type", jstr(ticket_type(&order.order_type).expect("checked"))),
        ("limit_price", limit_for(order)),
        ("tif", jstr(ticket_tif(&order.tif).expect("checked"))),
        ("underlying", jstr(contract.underlying.clone())),
        ("expiry", jstr(contract.expiry.clone())),
        ("strike", jstr(contract.strike.to_py_string())),
        ("right", jstr(contract.right.code())),
    ]);
    check_single(&ticket)?;
    Ok(ticket)
}

fn combo_ticket(order: &Order, legs: &[ComboLeg]) -> R<Json> {
    if let Some(reason) = vertical_reason(legs)? {
        return unsupported(format!("multi-leg combo: {reason}"));
    }
    if order.order_type != "LIMIT" {
        return unsupported("a vertical is mirrored with one net LIMIT price only");
    }
    if ticket_tif(&order.tif).is_none() {
        return unsupported(format!("TIF {}: DAY/GTC only", order.tif));
    }
    if !whole(&order.quantity)? {
        return unsupported(format!(
            "quantity {} is not a whole number of units (I5)",
            order.quantity.to_py_string()
        ));
    }
    let mut leg_json = Vec::new();
    let mut underlying = String::new();
    for (index, leg) in legs.iter().enumerate() {
        let Instrument::Option(c) = &leg.contract else {
            return wire("a vertical's legs are options");
        };
        if index == 0 {
            underlying = c.underlying.clone();
        }
        let one = obj(vec![
            ("symbol", jstr(c.occ()?)),
            ("side", jstr(leg.side.clone())),
            ("ratio", Json::Int(leg.ratio)),
            ("expiry", jstr(c.expiry.clone())),
            ("strike", jstr(c.strike.to_py_string())),
            ("right", jstr(c.right.code())),
        ]);
        check_leg(&one)?;
        leg_json.push(one);
    }
    let ticket = obj(vec![
        ("kind", jstr("combo")),
        ("underlying", jstr(underlying)),
        ("legs", Json::Arr(leg_json)),
        ("quantity", int_json(&order.quantity)?),
        ("order_type", jstr("LMT")),
        ("limit_price", jopt_dec(&order.limit_price)),
        ("price_effect", jstr(if order.side == "SELL" { "CREDIT" } else { "DEBIT" })),
        ("tif", jstr(ticket_tif(&order.tif).expect("checked"))),
    ]);
    check_combo(&ticket)?;
    Ok(ticket)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::ledger::json::parse;

    fn refusal(r: R<Json>) -> (String, String) {
        let e = r.unwrap_err();
        (e.kind.to_string(), e.msg)
    }

    const P200: &str = r#"{"kind": "option", "underlying": "AAPL", "expiry": "2026-10-16", "strike": "200", "right": "P", "multiplier": 100}"#;

    fn order(inst: &str, order_type: &str, tif: &str, qty: &str, side: &str, limit: &str) -> Json {
        parse(&format!(
            r#"{{"instrument": {inst}, "order_type": "{order_type}", "tif": "{tif}", "quantity": "{qty}", "side": "{side}", "limit_price": {limit}}}"#
        ))
        .unwrap()
    }

    #[test]
    fn an_option_order_becomes_a_ticket() {
        let t = ticket_for(&order(P200, "LIMIT", "DAY", "2", "SELL", "\"2.00\"")).unwrap();
        assert_eq!(
            crate::ledger::json::dumps(&t),
            r#"{"expiry":"2026-10-16","kind":"option","limit_price":"2.00","order_type":"LMT","quantity":2,"right":"P","side":"SELL","strike":"200","symbol":"AAPL  261016P00200000","tif":"DAY","underlying":"AAPL"}"#
        );
        // a market order carries no limit; "1E+1" is ten contracts
        let m = ticket_for(&order(P200, "MARKET", "GTC", "1E+1", "BUY", "null")).unwrap();
        assert_eq!(m.get("quantity"), Some(&Json::Int(10)));
        assert_eq!(m.get("limit_price"), Some(&Json::Null));
    }

    #[test]
    fn what_cannot_be_mirrored_is_refused_never_approximated() {
        let r = |ot: &str, tif: &str, q: &str| refusal(ticket_for(&order(P200, ot, tif, q, "BUY", "\"1\"")));
        assert_eq!(r("STOP", "DAY", "1"), (UNSUPPORTED.into(), "order type STOP: MARKET/LIMIT only".into()));
        assert_eq!(r("LIMIT", "OPG", "1"), (UNSUPPORTED.into(), "TIF OPG: DAY/GTC only".into()));
        assert_eq!(
            r("LIMIT", "DAY", "1.5"),
            (UNSUPPORTED.into(), "quantity 1.5 is not a whole number of contracts (I5)".into())
        );
        let stock = r#"{"kind": "equity", "symbol": "AAPL"}"#;
        assert_eq!(
            refusal(ticket_for(&order(stock, "LIMIT", "DAY", "0.5", "BUY", "\"1\""))).1,
            "quantity 0.5 is not a whole number of shares (I5)"
        );
        let other = r#"{"kind": "other", "repr": "Future(ES)"}"#;
        assert!(refusal(ticket_for(&order(other, "LIMIT", "DAY", "1", "BUY", "\"1\""))).1.starts_with("Future(ES): only single"));
    }

    #[test]
    fn the_ticket_validators_say_what_python_says() {
        let v = |s: &str| refusal(ticket_validate(&parse(s).unwrap())).1;
        let base = |k: &str, ex: &str| {
            format!(r#"{{"kind": "{k}", "side": "SELL", "quantity": 1, "order_type": "LMT", "limit_price": "2", "tif": "DAY"{ex}}}"#)
        };
        assert!(ticket_validate(&parse(&base("option", "")).unwrap()).is_ok());
        assert_eq!(v(&base("option", "").replace("\"SELL\"", "\"SHORT\"")), "ticket side must be BUY or SELL, got 'SHORT'");
        assert_eq!(v(&base("option", "").replace("1,", "1.0,")), "ticket quantity must be a positive int, got 1.0");
        assert_eq!(v(&base("option", "").replace("1,", "true,")), "ticket quantity must be a positive int, got True");
        assert_eq!(v(&base("option", "").replace("\"2\"", "null")), "an LMT ticket needs a positive limit_price (I5)");
        assert_eq!(v(&base("option", "").replace("\"LMT\"", "\"MKT\"")), "a MKT ticket cannot carry a limit_price");
        assert_eq!(
            v(&base("stock", r#", "symbol": "aapl""#)),
            "stock ticket symbol must be an upper-case equity symbol, got 'aapl'"
        );
        assert!(ticket_validate(&parse(&base("stock", r#", "symbol": "BRK.B""#)).unwrap()).is_ok());
        assert_eq!(v(r#"{"kind": "leg", "side": "BUY", "ratio": 0}"#), "leg ratio must be a positive int, got 0");
    }
}

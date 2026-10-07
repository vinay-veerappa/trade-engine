//! Raw venue results into decisions (was `tos_paper/normalize.py`): what a transport's
//! place and cancel results mean, which Order Book row a send proved, and how a working
//! order, an order fill and a position row are read. Rules: a send is never a fill;
//! `SENT` and `DRY_RUN` are PENDING; anything unrecognised is PENDING; only a known
//! refusal is REJECTED; nothing here is ACCEPTED except a cancel the Order Book read back
//! as CANCELED. A row that cannot be read is a `NormalizeError`, never a guess (I5).
//!
//! The raw rows are JSON values as `json.loads` built them (`repr` of one is what the
//! refusals quote). An acknowledgement crosses as `{"status", "message"}`: the host adds
//! the order id and the instant it already holds (no clock here, I7).

use super::pytext::{repr, repr_str, str_isdigit, strip_upper, to_str, truthy};
use super::wire::{jopt_dec, jstr, obj};
use super::NORMALIZE;
use crate::ledger::json::Json;
use crate::ledger::model::{derr, err, R};
use crate::money::Money;
use crate::options::{equity_symbol, parse_occ};

fn normalize<T>(msg: impl Into<String>) -> R<T> {
    err(NORMALIZE, msg)
}

fn ack(status: &str, message: String) -> Json {
    obj(vec![("status", jstr(status)), ("message", jstr(message))])
}

fn raw_of(doc: &Json) -> R<&Json> {
    super::wire::req(doc, "raw")
}

/// `str(raw.get(key, ""))`, stripped and upper-cased.
fn status_of(raw: &Json, key: &str) -> String {
    strip_upper(&raw.get(key).map_or_else(String::new, to_str))
}

/// `raw.get(key)` as Python gives it: a missing key is `None`.
fn get<'a>(raw: &'a Json, key: &str) -> &'a Json {
    raw.get(key).unwrap_or(&Json::Null)
}

fn is_mapping(raw: &Json) -> bool {
    matches!(raw, Json::Obj(_))
}

/// `normalize_place_result`: `{"raw"}` -> an ack. Never ACCEPTED (I5).
pub fn place_result(doc: &Json) -> R<Json> {
    let raw = raw_of(doc)?;
    if !is_mapping(raw) {
        return Ok(ack("PENDING", format!("unreadable transport result {}; awaiting reconcile", repr(raw))));
    }
    let status = status_of(raw, "status");
    Ok(match status.as_str() {
        "SENT" => ack("PENDING", "sent; awaiting read-back".into()),
        "DRY_RUN" => ack("PENDING", "dry run: nothing sent; awaiting reconcile".into()),
        "REFUSED" | "REJECTED" | "INELIGIBLE" | "MISMATCH" => {
            let reason = get(raw, "reason");
            let reason = if truthy(reason) { to_str(reason) } else { status };
            ack("REJECTED", format!("venue refused: {reason}"))
        }
        _ => ack("PENDING", format!("unknown transport status {}; awaiting reconcile", repr_str(&status))),
    })
}

/// The exception a transport call raised, as the host classifies it:
/// `{"class": "refused" | "replay" | "other", "type": <class name>, "text": str(exc)}`.
fn exception(doc: &Json) -> R<(String, String, String)> {
    let e = super::wire::req(doc, "exc")?;
    Ok((
        super::wire::req_str(e, "class")?.to_string(),
        super::wire::req_str(e, "type")?.to_string(),
        super::wire::req_str(e, "text")?.to_string(),
    ))
}

/// `normalize_place_exception`: REJECTED if provably nothing was sent, else PENDING.
pub fn place_exception(doc: &Json) -> R<Json> {
    let (class, name, text) = exception(doc)?;
    Ok(match class.as_str() {
        "refused" => ack("REJECTED", format!("transport refused before send: {text}")),
        "replay" => ack("PENDING", format!("idempotency replay: {text}; confirming by reconcile (I3)")),
        _ => ack("PENDING", format!("transport error {name}: {text}; uncertain whether sent, awaiting reconcile")),
    })
}

/// `placed_order_id`: the venue Order ID a SENT result proves, or null; never a guess (I5).
pub fn placed_order_id(doc: &Json) -> R<Json> {
    let raw = raw_of(doc)?;
    let proved = || -> Option<String> {
        if !is_mapping(raw) || status_of(raw, "status") != "SENT" {
            return None;
        }
        let book = status_of(raw, "book_status");
        match raw.get("order_id") {
            Some(Json::Str(oid)) if !book.is_empty() && book != "UNKNOWN" && str_isdigit(oid) => Some(oid.clone()),
            _ => None,
        }
    };
    Ok(obj(vec![("order_id", proved().map_or(Json::Null, jstr))]))
}

/// `normalize_cancel_result`: ACCEPTED only for a CANCELED Order Book row, else PENDING.
pub fn cancel_result(doc: &Json) -> R<Json> {
    let raw = raw_of(doc)?;
    if !is_mapping(raw) {
        return Ok(ack("PENDING", format!("unreadable cancel result {}; awaiting reconcile", repr(raw))));
    }
    let status = status_of(raw, "status");
    if status == "CANCELED" {
        return Ok(ack("ACCEPTED", format!("cancelled: order {} reads CANCELED", to_str(get(raw, "order_id")))));
    }
    let note = get(raw, "note");
    let note = if truthy(note) {
        to_str(note)
    } else if !status.is_empty() {
        status
    } else {
        "no status".to_string()
    };
    Ok(ack("PENDING", format!("cancel not confirmed ({note}); awaiting reconcile")))
}

/// `normalize_cancel_exception`: REJECTED if provably nothing was clicked, else PENDING.
pub fn cancel_exception(doc: &Json) -> R<Json> {
    let (class, name, text) = exception(doc)?;
    Ok(if class == "refused" {
        ack("REJECTED", format!("transport refused the cancel: {text}"))
    } else {
        ack("PENDING", format!("cancel error {name}: {text}; uncertain whether cancelled, awaiting reconcile"))
    })
}

fn row_state(status: &str) -> &'static str {
    match status {
        "WORKING" | "OPEN" => "ACCEPTED",
        "QUEUED" => "SUBMITTED",
        "PARTIAL" => "PARTIALLY_FILLED",
        "FILLED" => "FILLED",
        "CANCELED" | "CANCELLED" => "CANCELLED",
        "REJECTED" => "REJECTED",
        "EXPIRED" => "EXPIRED",
        _ => "PENDING_UNKNOWN",
    }
}

/// `book_state`: an Order Book status -> an order state; unrecognised is PENDING_UNKNOWN.
fn book_state(raw_status: &Json) -> &'static str {
    let text = if truthy(raw_status) { to_str(raw_status) } else { String::new() };
    row_state(&strip_upper(&text))
}

pub fn book_state_op(doc: &Json) -> R<Json> {
    Ok(obj(vec![("state", jstr(book_state(super::wire::req(doc, "status")?)))]))
}

/// `_decimal`: a finite Decimal from a string or an int; a float, a bool or `None` is refused.
fn decimal(value: &Json, name: &str) -> R<Money> {
    if matches!(value, Json::Bool(_) | Json::Float(_) | Json::Null) {
        return normalize(format!("{name} must be a decimal string, got {}", repr(value)));
    }
    let Some(parsed) = Money::parse(&to_str(value)) else {
        return normalize(format!("{name} is not a number: {}", repr(value)));
    };
    if !parsed.is_finite() {
        return normalize(format!("{name} must be finite, got {}", repr(value)));
    }
    Ok(parsed)
}

enum RowInstrument {
    Stock(String),
    Option { root: String, expiry: String, strike: String, right: &'static str },
}

impl RowInstrument {
    fn json(&self) -> Json {
        match self {
            RowInstrument::Stock(symbol) => obj(vec![("kind", jstr("equity")), ("symbol", jstr(symbol.clone()))]),
            RowInstrument::Option { root, expiry, strike, right } => obj(vec![
                ("kind", jstr("option")),
                ("underlying", jstr(root.clone())),
                ("expiry", jstr(expiry.clone())),
                ("strike", jstr(strike.clone())),
                ("right", jstr(*right)),
                ("multiplier", Json::Int(100)),
            ]),
        }
    }
}

fn contract(raw: &Json) -> R<RowInstrument> {
    let symbol = get(raw, "symbol");
    match parse_occ(Some(&to_str(symbol))) {
        Ok(p) => Ok(RowInstrument::Option {
            root: p.root,
            expiry: format!("{:04}-{:02}-{:02}", p.year, p.month, p.day),
            strike: p.strike_text,
            right: p.right.code(),
        }),
        Err(_) => normalize(format!("not a mirrored option symbol: {}", repr(symbol))),
    }
}

fn row_instrument(raw: &Json) -> R<RowInstrument> {
    let Some(kind) = raw.get("kind") else {
        return contract(raw);
    };
    match kind {
        Json::Str(k) if k == "stock" => {
            let symbol = get(raw, "symbol");
            let parsed = match symbol {
                Json::Str(s) => equity_symbol(Some(s)),
                _ => equity_symbol(None),
            };
            match parsed {
                Ok(sym) => Ok(RowInstrument::Stock(sym)),
                Err(_) => normalize(format!("not a mirrored stock symbol: {}", repr(symbol))),
            }
        }
        Json::Str(k) if k == "option" => contract(raw),
        other => normalize(format!("row kind {} is neither 'stock' nor 'option'", repr(other))),
    }
}

fn whole(d: &Money) -> R<bool> {
    d.is_integral().map_err(derr)
}

/// `normalize_working_order`: one Order Book row (`{"raw"}`). An unknown status is PENDING_UNKNOWN.
pub fn working_order(doc: &Json) -> R<Json> {
    let raw = raw_of(doc)?;
    if !is_mapping(raw) {
        return super::wire::wire("a working order row is a mapping");
    }
    let side = status_of(raw, "side");
    if side != "BUY" && side != "SELL" {
        return normalize(format!("working order side {}", repr(get(raw, "side"))));
    }
    let order_type = match status_of(raw, "order_type").as_str() {
        "MKT" => "MARKET",
        "LMT" => "LIMIT",
        _ => return normalize(format!("working order type {}", repr(get(raw, "order_type")))),
    };
    let quantity = decimal(get(raw, "quantity"), "quantity")?;
    let filled_raw = raw.get("filled").cloned().unwrap_or_else(|| jstr("0"));
    let filled = decimal(&filled_raw, "filled")?;
    let zero = Money::zero();
    if quantity.le(&zero).map_err(derr)? || filled.lt(&zero).map_err(derr)? || filled.gt(&quantity).map_err(derr)? {
        return normalize(format!(
            "working order quantity {} / filled {}",
            quantity.canon(),
            filled.canon()
        ));
    }
    let instrument = row_instrument(raw)?;
    if matches!(instrument, RowInstrument::Stock(_)) {
        if !whole(&quantity)? {
            return normalize(format!("stock quantity {} is not a whole number of shares", quantity.canon()));
        }
        if !whole(&filled)? {
            return normalize(format!("stock filled {} is not a whole number of shares", filled.canon()));
        }
    }
    let limit = match get(raw, "limit_price") {
        Json::Null => None,
        Json::Str(s) if s.is_empty() => None,
        other => Some(decimal(other, "limit_price")?),
    };
    let state = row_state(&status_of(raw, "status"));
    Ok(obj(vec![
        ("instrument", instrument.json()),
        ("side", jstr(side)),
        ("quantity", jstr(quantity.canon())),
        ("filled", jstr(filled.canon())),
        ("order_type", jstr(order_type)),
        ("limit_price", jopt_dec(&limit)),
        ("state", jstr(state)),
    ]))
}

/// `normalize_order_fill`: one `read_order_fills` row. Anything ambiguous raises (I5).
pub fn order_fill(doc: &Json) -> R<Json> {
    let raw = raw_of(doc)?;
    if !is_mapping(raw) {
        return super::wire::wire("an order fill row is a mapping");
    }
    let oid_raw = get(raw, "order_id");
    let oid = match oid_raw {
        Json::Str(s) if str_isdigit(s) => s.clone(),
        _ => return normalize(format!("order fill row names no all-digit order_id: {}", repr(oid_raw))),
    };
    let filled = decimal(get(raw, "filled"), "filled")?;
    let zero = Money::zero();
    if filled.lt(&zero).map_err(derr)? || !whole(&filled)? {
        return normalize(format!("order {oid} filled {}: not a whole non-negative quantity", filled.canon()));
    }
    let price_raw = get(raw, "avg_price");
    let price = match price_raw {
        Json::Null => None,
        Json::Str(s) if s.is_empty() => None,
        other => Some(decimal(other, "avg_price")?),
    };
    let positive = filled.gt(&zero).map_err(derr)?;
    if positive {
        let bad = match &price {
            None => true,
            Some(p) => p.le(&zero).map_err(derr)?,
        };
        if bad {
            return normalize(format!(
                "order {oid} filled {} with no positive average price {}",
                filled.canon(),
                repr(price_raw)
            ));
        }
    }
    let state = book_state(get(raw, "status"));
    if state == "FILLED" && filled.eq_num(&zero).map_err(derr)? {
        return normalize(format!("order {oid} reads FILLED with nothing filled"));
    }
    Ok(obj(vec![
        ("order_id", jstr(oid)),
        ("filled", jstr(filled.canon())),
        ("avg_price", if positive { jopt_dec(&price) } else { Json::Null }),
        ("state", jstr(state)),
    ]))
}

/// `normalize_position`: one Position row (signed quantity).
pub fn position(doc: &Json) -> R<Json> {
    let raw = raw_of(doc)?;
    if !is_mapping(raw) {
        return super::wire::wire("a position row is a mapping");
    }
    let instrument = row_instrument(raw)?;
    let quantity = decimal(get(raw, "quantity"), "quantity")?;
    let avg_price = decimal(get(raw, "avg_price"), "avg_price")?;
    if matches!(instrument, RowInstrument::Stock(_)) {
        if !whole(&quantity)? {
            return normalize(format!("stock quantity {} is not a whole number of shares", quantity.canon()));
        }
        if avg_price.lt(&Money::zero()).map_err(derr)? {
            return normalize(format!("stock avg_price must not be negative, got {}", avg_price.canon()));
        }
    }
    Ok(obj(vec![
        ("instrument", instrument.json()),
        ("quantity", jstr(quantity.canon())),
        ("avg_price", jstr(avg_price.canon())),
    ]))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::ledger::json::parse;

    fn run(f: fn(&Json) -> R<Json>, raw: &str) -> String {
        let doc = parse(&format!(r#"{{"raw": {raw}}}"#)).unwrap();
        match f(&doc) {
            Ok(j) => crate::ledger::json::dumps(&j),
            Err(e) => format!("{}: {}", e.kind, e.msg),
        }
    }

    #[test]
    fn a_place_result_is_pending_or_rejected_never_accepted() {
        let ack = |s: &str| run(place_result, s);
        assert_eq!(ack(r#"{"status": "SENT"}"#), r#"{"message":"sent; awaiting read-back","status":"PENDING"}"#);
        assert_eq!(ack(r#"{"status": " sent "}"#), ack(r#"{"status": "SENT"}"#));
        assert!(ack(r#"{"status": "DRY_RUN"}"#).contains("dry run"));
        assert!(ack(r#"{"status": "REFUSED", "reason": "not eligible"}"#).contains("venue refused: not eligible"));
        assert!(ack(r#"{"status": "REJECTED"}"#).contains("venue refused: REJECTED"));
        assert!(ack(r#"{"status": "ACCEPTED"}"#).contains("unknown transport status 'ACCEPTED'"));
        assert!(ack(r#"{"status": "FILLED"}"#).contains("PENDING"));
        assert!(ack("null").contains("unreadable transport result None"));
        assert!(ack("{}").contains("unknown transport status ''"));
    }

    #[test]
    fn the_order_id_a_send_proves() {
        let id = |s: &str| run(placed_order_id, s);
        assert_eq!(id(r#"{"status": "SENT", "order_id": "5403527317", "book_status": "WORKING"}"#), r#"{"order_id":"5403527317"}"#);
        for none in [
            r#"{"status": "SENT", "order_id": "5403527317", "book_status": "UNKNOWN"}"#,
            r#"{"status": "SENT", "order_id": "5403527317"}"#,
            r#"{"status": "SENT", "order_id": 5403527317, "book_status": "WORKING"}"#,
            r#"{"status": "DRY_RUN", "order_id": "1", "book_status": "WORKING"}"#,
        ] {
            assert_eq!(id(none), r#"{"order_id":null}"#, "{none}");
        }
    }

    #[test]
    fn a_cancel_is_accepted_only_when_the_book_reads_canceled() {
        let c = |s: &str| run(cancel_result, s);
        assert!(c(r#"{"status": "canceled", "order_id": "1"}"#).contains("ACCEPTED"));
        assert!(c(r#"{"status": "UNKNOWN", "note": "row still WORKING"}"#).contains("cancel not confirmed (row still WORKING)"));
        assert!(c("{}").contains("cancel not confirmed (no status)"));
    }

    #[test]
    fn rows_are_read_or_refused() {
        let w = |s: &str| run(working_order, s);
        let ok = r#"{"symbol": "AAPL  261016P00200000", "side": "buy", "quantity": 3, "filled": "1", "order_type": "MKT", "limit_price": null, "status": "PARTIAL"}"#;
        assert!(w(ok).contains(r#""state":"PARTIALLY_FILLED""#) && w(ok).contains(r#""strike":"200""#));
        assert!(w(&ok.replace("3,", "1.0,")).contains("tos_normalize: quantity must be a decimal string, got 1.0"));
        assert!(w(&ok.replace("\"1\"", "\"4\"")).contains("working order quantity 3 / filled 4"));
        assert!(w(&ok.replace("AAPL  261016P00200000", "AAPL")).contains("not a mirrored option symbol: 'AAPL'"));
        let p = |s: &str| run(position, s);
        assert!(p(r#"{"kind": "stock", "symbol": "aapl", "quantity": "100", "avg_price": "10"}"#).contains(r#""symbol":"AAPL""#));
        assert!(p(r#"{"kind": "stock", "symbol": "AAPL", "quantity": "1.5", "avg_price": "10"}"#).contains("not a whole number of shares"));
        assert!(p(r#"{"kind": "bond", "symbol": "X"}"#).contains("row kind 'bond' is neither"));
        let f = |s: &str| run(order_fill, s);
        assert!(f(r#"{"order_id": "1", "filled": "0", "status": "FILLED"}"#).contains("reads FILLED with nothing filled"));
        assert!(f(r#"{"order_id": "1", "filled": "2", "avg_price": "0", "status": "WORKING"}"#).contains("no positive average price '0'"));
        assert!(f(r#"{"order_id": "1", "filled": "2", "avg_price": "1.05", "status": "PARTIAL"}"#).contains(r#""avg_price":"1.05""#));
    }
}

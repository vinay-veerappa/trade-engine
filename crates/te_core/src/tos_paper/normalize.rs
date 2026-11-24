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
//!
//! Each `*_raw` typed fn is the decision over a raw row (or a classified exception),
//! returning plain Rust values; the `*`/`*_op` door fns are thin JSON wrappers over them
//! so the dispatcher keeps working.

use chrono::NaiveDate;

use super::pytext::{repr, repr_str, str_isdigit, strip_upper, to_str, truthy};
use super::wire::{jopt_dec, jstr, linstr_json, obj};
use super::NORMALIZE;
use crate::ledger::json::Json;
use crate::ledger::model::{derr, err, Instrument, OrderState, OrderType, Side, R};
use crate::money::Money;
use crate::options::{equity_symbol, parse_occ};

fn normalize<T>(msg: impl Into<String>) -> R<T> {
    err(NORMALIZE, msg)
}

/// A raw venue row's normalisation result: `(status, message)`.
pub type Ack = (String, String);

/// A working order (an Order Book row) as the carrier reads it.
#[derive(Debug)]
pub struct WorkingOrder {
    pub instrument: Instrument,
    pub side: Side,
    pub quantity: Money,
    pub filled: Money,
    pub order_type: OrderType,
    pub limit_price: Option<Money>,
    pub state: OrderState,
}

/// One order's cumulative fill, read back by its venue Order ID.
#[derive(Debug)]
pub struct OrderFill {
    pub order_id: String,
    pub filled: Money,
    pub avg_price: Option<Money>,
    pub state: OrderState,
}

/// One Position row (signed quantity).
#[derive(Debug)]
pub struct Position {
    pub instrument: Instrument,
    pub quantity: Money,
    pub avg_price: Money,
}

fn ack(status: &str, message: String) -> Json {
    obj(vec![("status", jstr(status)), ("message", jstr(message))])
}

fn ack_of((status, message): Ack) -> Json {
    ack(&status, message)
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

/// `normalize_place_result`: a raw transport result -> an ack. Never ACCEPTED (I5).
pub fn place_result_raw(raw: &Json) -> R<Ack> {
    if !is_mapping(raw) {
        return Ok(("PENDING".to_string(), format!("unreadable transport result {}; awaiting reconcile", repr(raw))));
    }
    let status = status_of(raw, "status");
    Ok(match status.as_str() {
        "SENT" => ("PENDING".to_string(), "sent; awaiting read-back".to_string()),
        "DRY_RUN" => ("PENDING".to_string(), "dry run: nothing sent; awaiting reconcile".to_string()),
        "REFUSED" | "REJECTED" | "INELIGIBLE" | "MISMATCH" => {
            let reason = get(raw, "reason");
            let reason = if truthy(reason) { to_str(reason) } else { status };
            ("REJECTED".to_string(), format!("venue refused: {reason}"))
        }
        _ => ("PENDING".to_string(), format!("unknown transport status {}; awaiting reconcile", repr_str(&status))),
    })
}

/// `normalize_place_result`: `{"raw"}` -> an ack. Never ACCEPTED (I5).
pub fn place_result(doc: &Json) -> R<Json> {
    Ok(ack_of(place_result_raw(raw_of(doc)?)?))
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
pub fn place_exception_raw(class: &str, name: &str, text: &str) -> R<Ack> {
    Ok(match class {
        "refused" => ("REJECTED".to_string(), format!("transport refused before send: {text}")),
        "replay" => ("PENDING".to_string(), format!("idempotency replay: {text}; confirming by reconcile (I3)")),
        _ => ("PENDING".to_string(), format!("transport error {name}: {text}; uncertain whether sent, awaiting reconcile")),
    })
}

/// `normalize_place_exception`: REJECTED if provably nothing was sent, else PENDING.
pub fn place_exception(doc: &Json) -> R<Json> {
    let (class, name, text) = exception(doc)?;
    Ok(ack_of(place_exception_raw(&class, &name, &text)?))
}

/// `placed_order_id`: the venue Order ID a SENT result proves, or null; never a guess (I5).
pub fn placed_order_id_raw(raw: &Json) -> R<Option<String>> {
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
    Ok(proved())
}

/// `placed_order_id`: the venue Order ID a SENT result proves, or null; never a guess (I5).
pub fn placed_order_id(doc: &Json) -> R<Json> {
    Ok(obj(vec![("order_id", placed_order_id_raw(raw_of(doc)?)?.map_or(Json::Null, jstr))]))
}

/// `normalize_cancel_result`: ACCEPTED only for a CANCELED Order Book row, else PENDING.
pub fn cancel_result_raw(raw: &Json) -> R<Ack> {
    if !is_mapping(raw) {
        return Ok(("PENDING".to_string(), format!("unreadable cancel result {}; awaiting reconcile", repr(raw))));
    }
    let status = status_of(raw, "status");
    if status == "CANCELED" {
        return Ok(("ACCEPTED".to_string(), format!("cancelled: order {} reads CANCELED", to_str(get(raw, "order_id")))));
    }
    let note = get(raw, "note");
    let note = if truthy(note) {
        to_str(note)
    } else if !status.is_empty() {
        status
    } else {
        "no status".to_string()
    };
    Ok(("PENDING".to_string(), format!("cancel not confirmed ({note}); awaiting reconcile")))
}

/// `normalize_cancel_result`: ACCEPTED only for a CANCELED Order Book row, else PENDING.
pub fn cancel_result(doc: &Json) -> R<Json> {
    Ok(ack_of(cancel_result_raw(raw_of(doc)?)?))
}

/// `normalize_cancel_exception`: REJECTED if provably nothing was clicked, else PENDING.
pub fn cancel_exception_raw(class: &str, name: &str, text: &str) -> R<Ack> {
    Ok(if class == "refused" {
        ("REJECTED".to_string(), format!("transport refused the cancel: {text}"))
    } else {
        ("PENDING".to_string(), format!("cancel error {name}: {text}; uncertain whether cancelled, awaiting reconcile"))
    })
}

/// `normalize_cancel_exception`: REJECTED if provably nothing was clicked, else PENDING.
pub fn cancel_exception(doc: &Json) -> R<Json> {
    let (class, name, text) = exception(doc)?;
    Ok(ack_of(cancel_exception_raw(&class, &name, &text)?))
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
pub fn book_state_of(raw_status: &Json) -> &'static str {
    let text = if truthy(raw_status) { to_str(raw_status) } else { String::new() };
    row_state(&strip_upper(&text))
}

pub fn book_state_op(doc: &Json) -> R<Json> {
    Ok(obj(vec![("state", jstr(book_state_of(super::wire::req(doc, "status")?)))]))
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

/// The ledger-model option behind an OCC symbol row (a normalised row is always an option
/// with multiplier 100 or a stock; a future or combo never appears here).
fn contract_instrument(raw: &Json) -> R<Instrument> {
    let symbol = get(raw, "symbol");
    match parse_occ(Some(&to_str(symbol))) {
        Ok(p) => Ok(Instrument::Option(crate::ledger::model::OptionContract {
            underlying: p.root,
            expiry: NaiveDate::from_ymd_opt(p.year, p.month, p.day).expect("parse_occ validated the date"),
            strike: Money::parse(&p.strike_text).expect("parse_occ produced a decimal strike"),
            right: p.right,
            multiplier: 100,
        })),
        Err(_) => normalize(format!("not a mirrored option symbol: {}", repr(symbol))),
    }
}

fn row_instrument(raw: &Json) -> R<Instrument> {
    let Some(kind) = raw.get("kind") else {
        return contract_instrument(raw);
    };
    match kind {
        Json::Str(k) if k == "stock" => {
            let symbol = get(raw, "symbol");
            let parsed = match symbol {
                Json::Str(s) => equity_symbol(Some(s)),
                _ => equity_symbol(None),
            };
            match parsed {
                Ok(sym) => Ok(Instrument::Equity(sym)),
                Err(_) => normalize(format!("not a mirrored stock symbol: {}", repr(symbol))),
            }
        }
        Json::Str(k) if k == "option" => contract_instrument(raw),
        other => normalize(format!("row kind {} is neither 'stock' nor 'option'", repr(other))),
    }
}

fn whole(d: &Money) -> R<bool> {
    d.is_integral().map_err(derr)
}

/// `normalize_working_order`: one Order Book row. An unknown status is PENDING_UNKNOWN.
pub fn working_order_raw(raw: &Json) -> R<WorkingOrder> {
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
    if matches!(instrument, Instrument::Equity(_)) {
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
    Ok(WorkingOrder {
        instrument,
        side: Side::parse(&side).expect("side validated above"),
        quantity,
        filled,
        order_type: OrderType::parse(order_type).expect("order type validated above"),
        limit_price: limit,
        state: OrderState::parse(state).expect("state is one of the enum's"),
    })
}

/// `normalize_working_order`: one Order Book row (`{"raw"}`). An unknown status is PENDING_UNKNOWN.
pub fn working_order(doc: &Json) -> R<Json> {
    let w = working_order_raw(raw_of(doc)?)?;
    Ok(obj(vec![
        ("instrument", linstr_json(&w.instrument)),
        ("side", jstr(w.side.value())),
        ("quantity", jstr(w.quantity.canon())),
        ("filled", jstr(w.filled.canon())),
        ("order_type", jstr(w.order_type.value())),
        ("limit_price", jopt_dec(&w.limit_price)),
        ("state", jstr(w.state.value())),
    ]))
}

/// `normalize_order_fill`: one `read_order_fills` row. Anything ambiguous raises (I5).
pub fn order_fill_raw(raw: &Json) -> R<OrderFill> {
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
    let state = book_state_of(get(raw, "status"));
    if state == "FILLED" && filled.eq_num(&zero).map_err(derr)? {
        return normalize(format!("order {oid} reads FILLED with nothing filled"));
    }
    Ok(OrderFill {
        order_id: oid,
        filled,
        avg_price: if positive { price } else { None },
        state: OrderState::parse(state).expect("state is one of the enum's"),
    })
}

/// `normalize_order_fill`: one `read_order_fills` row. Anything ambiguous raises (I5).
pub fn order_fill(doc: &Json) -> R<Json> {
    let f = order_fill_raw(raw_of(doc)?)?;
    Ok(obj(vec![
        ("order_id", jstr(f.order_id)),
        ("filled", jstr(f.filled.canon())),
        ("avg_price", jopt_dec(&f.avg_price)),
        ("state", jstr(f.state.value())),
    ]))
}

/// `normalize_position`: one Position row (signed quantity).
pub fn position_raw(raw: &Json) -> R<Position> {
    if !is_mapping(raw) {
        return super::wire::wire("a position row is a mapping");
    }
    let instrument = row_instrument(raw)?;
    let quantity = decimal(get(raw, "quantity"), "quantity")?;
    let avg_price = decimal(get(raw, "avg_price"), "avg_price")?;
    if matches!(instrument, Instrument::Equity(_)) {
        if !whole(&quantity)? {
            return normalize(format!("stock quantity {} is not a whole number of shares", quantity.canon()));
        }
        if avg_price.lt(&Money::zero()).map_err(derr)? {
            return normalize(format!("stock avg_price must not be negative, got {}", avg_price.canon()));
        }
    }
    Ok(Position { instrument, quantity, avg_price })
}

/// `normalize_position`: one Position row (signed quantity).
pub fn position(doc: &Json) -> R<Json> {
    let p = position_raw(raw_of(doc)?)?;
    Ok(obj(vec![
        ("instrument", linstr_json(&p.instrument)),
        ("quantity", jstr(p.quantity.canon())),
        ("avg_price", jstr(p.avg_price.canon())),
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

    // -- the typed fns --------------------------------------------------------------------

    #[test]
    fn a_typed_place_result_agrees_with_the_door() {
        let raw = parse(r#"{"status": "SENT"}"#).unwrap();
        assert_eq!(place_result_raw(&raw).unwrap(), ("PENDING".to_string(), "sent; awaiting read-back".to_string()));
        let raw = parse("null").unwrap();
        assert_eq!(place_result_raw(&raw).unwrap().1, "unreadable transport result None; awaiting reconcile");
    }

    #[test]
    fn the_typed_exception_cancel_and_book_state_agree_with_the_door() {
        assert_eq!(
            place_exception_raw("refused", "T", "boom").unwrap(),
            ("REJECTED".to_string(), "transport refused before send: boom".to_string())
        );
        assert_eq!(
            cancel_exception_raw("other", "TimeoutError", "JAB hung").unwrap().1,
            "cancel error TimeoutError: JAB hung; uncertain whether cancelled, awaiting reconcile"
        );
        let raw = parse(r#"{"status": "SENT", "order_id": "5403527317", "book_status": "WORKING"}"#).unwrap();
        assert_eq!(placed_order_id_raw(&raw).unwrap().as_deref(), Some("5403527317"));
        let none = parse(r#"{"status": "DRY_RUN", "order_id": "1", "book_status": "WORKING"}"#).unwrap();
        assert_eq!(placed_order_id_raw(&none).unwrap(), None);
        assert_eq!(cancel_result_raw(&parse(r#"{"status": "canceled", "order_id": "1"}"#).unwrap()).unwrap().0, "ACCEPTED");
        assert_eq!(book_state_of(&parse("null").unwrap()), "PENDING_UNKNOWN");
        assert_eq!(book_state_of(&parse(r#""working""#).unwrap()), "ACCEPTED");
    }

    #[test]
    fn a_typed_working_order_reads_the_carrier_fields() {
        let raw = parse(r#"{"symbol": "AAPL  261016P00200000", "side": "SELL", "quantity": "2", "filled": "0", "order_type": "LMT", "limit_price": "2.00", "status": "WORKING"}"#).unwrap();
        let w = working_order_raw(&raw).unwrap();
        assert_eq!(w.side, Side::Sell);
        assert_eq!(w.quantity.canon(), "2");
        assert_eq!(w.limit_price.as_ref().unwrap().canon(), "2");
        assert_eq!(w.state, OrderState::Accepted);
        let bad = parse(r#"{"symbol": "AAPL", "side": "SELL", "quantity": "1", "order_type": "LMT", "status": "WORKING"}"#).unwrap();
        assert_eq!(working_order_raw(&bad).unwrap_err().msg, "not a mirrored option symbol: 'AAPL'");
    }

    #[test]
    fn a_typed_order_fill_and_position_read_the_carrier_fields() {
        let raw = parse(r#"{"order_id": "1", "filled": "2", "avg_price": "1.05", "status": "PARTIAL"}"#).unwrap();
        let f = order_fill_raw(&raw).unwrap();
        assert_eq!(f.order_id, "1");
        assert_eq!(f.filled.canon(), "2");
        assert_eq!(f.avg_price.as_ref().unwrap().canon(), "1.05");
        let raw = parse(r#"{"symbol": "AAPL  261016P00200000", "quantity": "-1", "avg_price": "2.10"}"#).unwrap();
        let p = position_raw(&raw).unwrap();
        assert_eq!(p.quantity.canon(), "-1");
        assert_eq!(p.avg_price.canon(), "2.1");
        let bad = parse(r#"{"symbol": "AAPL  261016P00200000", "quantity": null, "avg_price": "2.10"}"#).unwrap();
        assert_eq!(position_raw(&bad).unwrap_err().kind, NORMALIZE);
    }
}

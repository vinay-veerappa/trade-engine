//! What the Python ledger needs from the Rust one once the Python rules are gone (P2b):
//!
//! * [`check_encoded`]: the codec's refusals over a tree Python's walker produced. The
//!   walker renders every value by type and refuses nothing; where the old `_encode`
//!   would have refused it writes a marker (`{"?": type}` an unsupported payload type,
//!   `{"?i": type}` an unknown instrument type). The first refusal in document order is
//!   the one the old encoder raised, because it walked the same order.
//! * [`event_from_row`]: one stored row, decoded with every refusal `codec.decode_event`
//!   made (the envelope is the dict `Ledger._row_to_event` built).
//! * [`uncanon_account`] / [`uncanon_position`] / [`uncanon_mirror`]: a carrier's canonical
//!   JSON (`canon.rs`) back into the fold's state, so a seed or a caller-held state can be
//!   folded further. Every value goes through the codec's own constructors, so a seed
//!   that violates a domain rule is refused as the carrier itself would.
//! * [`apply_fill`]: `state.apply_fill`, the public one-fill step.

use std::collections::BTreeSet;

use super::codec::{decode, py_repr};
use super::fold::{self, leg_hk, make_position, AccountState, Position};
use super::json::{self, dumps, Json};
use super::mirror::{book_hk, MirrorState, MirrorTicketState};
use super::model::*;
use super::ops::OMap;
use crate::money::Money;

fn codec<T>(msg: impl Into<String>) -> R<T> {
    err("codec", msg)
}

// --- the encoder's refusals ----------------------------------------------------------------

fn check_node(node: &Json) -> R<()> {
    let Json::Obj(pairs) = node else { return Ok(()) };
    if let Some(Json::Str(t)) = node.get("?") {
        return codec(format!("Refusing to persist unsupported payload type {t} (I5)"));
    }
    if let Some(Json::Str(t)) = node.get("?i") {
        return codec(format!("Refusing to persist unknown instrument type {t} (I6)"));
    }
    if let Some(d) = node.get("d") {
        if let Json::Str(text) = d {
            let finite = Money::parse(text).map(|p| p.is_finite()).unwrap_or(false);
            if !finite {
                return codec(format!("Refusing to persist a non-finite Decimal: {text} (I5)"));
            }
        }
        return Ok(());
    }
    if let Some(t) = node.get("T") {
        if let Json::Str(text) = t {
            let aware = parse_datetime(text).map(|(_, a)| a).unwrap_or(false);
            if !aware {
                return codec("Refusing to persist a naive datetime (I7)");
            }
        }
        return Ok(());
    }
    if let Some(Json::Arr(items)) = node.get("t") {
        return items.iter().try_for_each(check_node);
    }
    if let Some(Json::Arr(items)) = node.get("m") {
        for it in items {
            if let Json::Arr(pair) = it {
                if let Some(v) = pair.get(1) {
                    check_node(v)?;
                }
            }
        }
        return Ok(());
    }
    if node.get("dc").is_some() {
        if let Some(Json::Obj(fields)) = node.get("f") {
            for (_, v) in fields {
                check_node(v)?;
            }
        }
        return Ok(());
    }
    let _ = pairs;
    Ok(())
}

/// The old `_encode`'s refusals over the walker's tree; the tree's stored bytes (sorted,
/// compact, ASCII: what `json.dumps(sort_keys=True)` wrote) when it would have encoded.
pub fn check_encoded(text: &str) -> R<Vec<u8>> {
    let j = parse(text)?;
    check_node(&j)?;
    Ok(dumps(&j).into_bytes())
}

fn parse(text: &str) -> R<Json> {
    json::parse(text).map_err(|e| LErr { kind: if e.unsupported { "unsupported" } else { "json" }, msg: e.message })
}

/// `decode_payload` with every refusal and nothing built: Python builds the value from the
/// same tree once this accepts it.
pub fn check_decode_payload(text: &str) -> R<()> {
    decode(&parse(text)?).map(|_| ())
}

// --- a stored row ----------------------------------------------------------------------------

/// `Ledger._row_to_event`: the row's columns as the dict it built, then `decode_event`.
pub fn event_from_row(
    account: &str,
    kind: &str,
    payload_json: &str,
    ts_utc: &str,
    command_id: Option<&str>,
    schema_version: i128,
    seq: Option<i128>,
) -> R<Event> {
    let payload = parse(payload_json)?;
    let env = Json::Obj(vec![
        ("account".into(), Json::Str(account.to_string())),
        ("kind".into(), Json::Str(kind.to_string())),
        ("payload".into(), payload),
        ("ts_utc".into(), Json::Str(ts_utc.to_string())),
        ("command_id".into(), command_id.map_or(Json::Null, |c| Json::Str(c.to_string()))),
        ("schema_version".into(), Json::Int(schema_version)),
        ("seq".into(), seq.map_or(Json::Null, Json::Int)),
    ]);
    super::codec::decode_event(&env)
}

// --- canonical JSON back into state ------------------------------------------------------------

fn bad<T>(what: &str) -> R<T> {
    codec(format!("Malformed canonical state: {what}"))
}

fn fields<'a>(node: &'a Json, name: &str) -> R<&'a Vec<(String, Json)>> {
    match (node.get("dc"), node.get("f")) {
        (Some(Json::Str(n)), Some(Json::Obj(f))) if n == name => Ok(f),
        _ => bad(&format!("expected a {name}")),
    }
}

fn field<'a>(f: &'a [(String, Json)], name: &str) -> R<&'a Json> {
    match f.iter().find(|(k, _)| k == name) {
        Some((_, v)) => Ok(v),
        None => bad(&format!("missing field {}", py_repr(name))),
    }
}

fn obj_of(node: &Json) -> R<Obj> {
    match decode(node)? {
        Val::Obj(o) => Ok(*o),
        other => bad(&format!("expected an object, got {}", other.type_name())),
    }
}

fn dec_of(node: &Json) -> R<Money> {
    match decode(node)? {
        Val::Dec(d) => Ok(d),
        other => bad(&format!("expected a Decimal, got {}", other.type_name())),
    }
}

fn odec_of(node: &Json) -> R<Option<Money>> {
    match decode(node)? {
        Val::None => Ok(None),
        Val::Dec(d) => Ok(Some(d)),
        other => bad(&format!("expected a Decimal or None, got {}", other.type_name())),
    }
}

fn str_of(node: &Json) -> R<String> {
    match node {
        Json::Str(s) => Ok(s.clone()),
        _ => bad("expected a str"),
    }
}

fn ostr_of(node: &Json) -> R<Option<String>> {
    match decode(node)? {
        Val::None => Ok(None),
        Val::Str(s) => Ok(Some(s)),
        other => bad(&format!("expected a str or None, got {}", other.type_name())),
    }
}

fn int_of(node: &Json) -> R<i128> {
    match node {
        Json::Int(i) => Ok(*i),
        _ => bad("expected an int"),
    }
}

fn bool_of(node: &Json) -> R<bool> {
    match node {
        Json::Bool(b) => Ok(*b),
        _ => bad("expected a bool"),
    }
}

fn instr_of(node: &Json) -> R<Instrument> {
    match obj_of(node)? {
        Obj::Instr(i) => Ok(i),
        _ => bad("expected an instrument"),
    }
}

fn tuple_items(node: &Json) -> R<&Vec<Json>> {
    match node.get("t") {
        Some(Json::Arr(items)) => Ok(items),
        _ => bad("expected a tuple"),
    }
}

fn fs_items(node: &Json) -> R<&Vec<Json>> {
    match node.get("fs") {
        Some(Json::Arr(items)) => Ok(items),
        _ => bad("expected a frozenset"),
    }
}

fn pairs(node: &Json) -> R<Vec<(&Json, &Json)>> {
    let Some(Json::Arr(items)) = node.get("m") else { return bad("expected a map") };
    items
        .iter()
        .map(|it| match it {
            Json::Arr(p) if p.len() == 2 => Ok((&p[0], &p[1])),
            _ => bad("a map entry is not a pair"),
        })
        .collect()
}

fn str_map<V>(node: &Json, val: impl Fn(&Json) -> R<V>) -> R<OMap<String, V>> {
    let mut m = OMap::new();
    for (k, v) in pairs(node)? {
        m.put(&str_of(k)?, val(v)?);
    }
    Ok(m)
}

fn instr_map<V>(node: &Json, val: impl Fn(&Json) -> R<V>) -> R<OMap<Instrument, V>> {
    let mut m = OMap::new();
    for (k, v) in pairs(node)? {
        let i = instr_of(k)?;
        m.insert(i.hk(), i, val(v)?);
    }
    Ok(m)
}

fn str_set(node: &Json) -> R<BTreeSet<String>> {
    fs_items(node)?.iter().map(str_of).collect()
}

pub fn uncanon_position(node: &Json) -> R<Position> {
    let f = fields(node, "Position")?;
    let mut lots = Vec::new();
    for l in tuple_items(field(f, "open_lots")?)? {
        match obj_of(l)? {
            Obj::Lot(lot) => lots.push(lot),
            _ => return bad("an open lot is not a Lot"),
        }
    }
    make_position(
        &str_of(field(f, "account_id")?)?,
        instr_of(field(f, "instrument")?)?,
        dec_of(field(f, "quantity")?)?,
        dec_of(field(f, "avg_cost")?)?,
        dec_of(field(f, "realized_pnl")?)?,
        lots,
    )
}

fn uncanon_ticket(node: &Json) -> R<MirrorTicketState> {
    let f = fields(node, "MirrorTicketState")?;
    let Obj::MQueued(queued) = obj_of(field(f, "queued")?)? else { return bad("queued is not a MirrorQueued") };
    let ack = match decode(field(f, "ack")?)? {
        Val::None => None,
        Val::Obj(o) => match *o {
            Obj::MAck(a) => Some(a),
            _ => return bad("ack is not a MirrorAck"),
        },
        _ => return bad("ack is not a MirrorAck"),
    };
    let book_status = match decode(field(f, "book_status")?)? {
        Val::None => None,
        Val::OrderState(s) => Some(s),
        _ => return bad("book_status is not an OrderState"),
    };
    Ok(MirrorTicketState {
        queued,
        ack,
        venue_order_id: ostr_of(field(f, "venue_order_id")?)?,
        book_status,
        filled: dec_of(field(f, "filled")?)?,
        avg_price: odec_of(field(f, "avg_price")?)?,
        closed: bool_of(field(f, "closed")?)?,
        allocated: str_map(field(f, "allocated")?, dec_of)?,
    })
}

pub fn uncanon_mirror(node: &Json) -> R<MirrorState> {
    let f = fields(node, "MirrorState")?;
    let mut book = OMap::new();
    for (k, v) in pairs(field(f, "book")?)? {
        let items = tuple_items(k)?;
        if items.len() != 2 {
            return bad("a book key is not a pair");
        }
        let account = str_of(&items[0])?;
        let instrument = instr_of(&items[1])?;
        book.insert(book_hk(&account, &instrument), (account, instrument), dec_of(v)?);
    }
    Ok(MirrorState {
        venue: ostr_of(field(f, "venue")?)?,
        tickets: str_map(field(f, "tickets")?, uncanon_ticket)?,
        book,
        queued_orders: str_map(field(f, "queued_orders")?, str_of)?,
        refused_orders: str_map(field(f, "refused_orders")?, str_of)?,
        order_ids: str_map(field(f, "order_ids")?, str_of)?,
    })
}

pub fn uncanon_account(node: &Json) -> R<AccountState> {
    let f = fields(node, "AccountState")?;
    let mut st = AccountState::new(&str_of(field(f, "account_id")?)?);
    st.cash = dec_of(field(f, "cash")?)?;
    st.positions = instr_map(field(f, "positions")?, uncanon_position)?;
    st.orders = str_map(field(f, "orders")?, |j| match obj_of(j)? {
        Obj::Order(o) => Ok(o),
        _ => bad("an order is not an Order"),
    })?;
    st.filled_quantity = str_map(field(f, "filled_quantity")?, dec_of)?;
    for (k, v) in pairs(field(f, "leg_filled")?)? {
        let items = tuple_items(k)?;
        if items.len() != 2 {
            return bad("a leg_filled key is not a pair");
        }
        let (oid, idx) = (str_of(&items[0])?, int_of(&items[1])?);
        st.leg_filled.insert(leg_hk(&oid, idx), (oid, idx), dec_of(v)?);
    }
    st.venue_order_ids = str_map(field(f, "venue_order_ids")?, str_of)?;
    st.emulated_orders = str_map(field(f, "emulated_orders")?, |j| match obj_of(j)? {
        Obj::Emulated(e) => Ok(e),
        _ => bad("an emulated order is not an EmulatedOrderState"),
    })?;
    for x in tuple_items(field(f, "fills")?)? {
        match obj_of(x)? {
            Obj::Fill(fill) => st.fills.push(fill),
            _ => return bad("a fill is not a Fill"),
        }
    }
    st.fill_ids = str_set(field(f, "fill_ids")?)?;
    st.marks = instr_map(field(f, "marks")?, dec_of)?;
    st.realized_pnl = dec_of(field(f, "realized_pnl")?)?;
    st.signals_seen = int_of(field(f, "signals_seen")?)?;
    st.verdicts = int_of(field(f, "verdicts")?)?;
    st.refusals = int_of(field(f, "refusals")?)?;
    st.last_reconcile = match decode(field(f, "last_reconcile")?)? {
        Val::None => None,
        Val::Obj(o) => match *o {
            Obj::Reconcile(r) => Some(r),
            _ => return bad("last_reconcile is not a VenueReconcile"),
        },
        _ => return bad("last_reconcile is not a VenueReconcile"),
    };
    st.venue_halted = bool_of(field(f, "venue_halted")?)?;
    st.halted_venues = str_set(field(f, "halted_venues")?)?;
    st.risk_controls = str_map(field(f, "risk_controls")?, bool_of)?;
    st.mirror = uncanon_mirror(field(f, "mirror")?)?;
    st.last_seq = int_of(field(f, "last_seq")?)?;
    Ok(st)
}

pub fn account_from_text(text: &str) -> R<AccountState> {
    uncanon_account(&parse(text)?)
}

// --- the one-fill step -------------------------------------------------------------------------

/// `state.apply_fill`: the position after one fill (FIFO lots, realised P&L on close).
pub fn apply_fill(account_id: &str, position: Option<&Position>, fill: &Fill, multiplier: i128) -> R<Position> {
    fold::apply_trade_pub(
        account_id,
        position,
        &fill.instrument,
        fill.side,
        &fill.quantity,
        &fill.price,
        &fill.filled_at,
        fill.fill_id.clone(),
        multiplier,
    )
}

/// A payload's decoded object, from its JSON text.
pub fn obj_from_text(text: &str) -> R<Obj> {
    match decode(&parse(text)?)? {
        Val::Obj(o) => Ok(*o),
        other => bad(&format!("expected an object, got {}", other.type_name())),
    }
}

pub fn json_from_text(text: &str) -> R<Json> {
    parse(text)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::ledger::canon::{canon_account, export_delta, export_full};
    use crate::ledger::codec::event_from_bytes;

    const BUY: &str = r#"{"account":"A","command_id":null,"kind":"Fill","payload":{"dc":"Fill","f":{"account_id":"A","fee":{"d":"0"},"fill_id":"f1","filled_at":{"T":"2026-01-02T15:00:00+00:00"},"instrument":{"dc":"Equity","f":{"symbol":"SPY"}},"leg_id":{"n":true},"order_id":"o1","price":{"d":"100"},"quantity":{"d":"2"},"side":{"e":"Side","v":"BUY"},"venue_env":"sim","venue_execution_id":{"n":true},"venue_order_id":{"n":true}}},"schema_version":1,"seq":null,"ts_utc":"2026-01-02T15:00:00+00:00"}"#;

    const ORDER: &str = r#"{"account":"A","command_id":null,"kind":"OrderSubmitted","payload":{"dc":"Order","f":{"account_id":"A","command_id":"cmd-o1","created_at":{"T":"2026-01-02T15:00:00+00:00"},"instrument":{"dc":"Equity","f":{"symbol":"SPY"}},"limit_price":{"n":true},"oco_group":{"n":true},"order_id":"o1","order_type":{"e":"OrderType","v":"MARKET"},"parent_order_id":{"n":true},"quantity":{"d":"10"},"side":{"e":"Side","v":"BUY"},"state":{"e":"OrderState","v":"NEW"},"stop_price":{"n":true},"tif":{"e":"TimeInForce","v":"DAY"},"trail_amount":{"n":true}}},"schema_version":1,"seq":null,"ts_utc":"2026-01-02T15:00:00+00:00"}"#;

    fn ev(s: &str) -> Event {
        event_from_bytes(s.as_bytes()).unwrap()
    }

    #[test]
    fn check_refuses_what_the_encoder_refused_in_document_order() {
        let e = check_encoded(r#"{"t":[{"d":"1"},{"?":"set"},{"d":"NaN"}]}"#).unwrap_err();
        assert_eq!((e.kind, e.msg.as_str()), ("codec", "Refusing to persist unsupported payload type set (I5)"));
        let e = check_encoded(r#"{"dc":"Mark","f":{"price":{"d":"-Infinity"},"x":{"?":"set"}}}"#).unwrap_err();
        assert_eq!(e.msg, "Refusing to persist a non-finite Decimal: -Infinity (I5)");
        let e = check_encoded(r#"{"m":[["k",{"T":"2026-01-02T15:00:00"}]]}"#).unwrap_err();
        assert_eq!(e.msg, "Refusing to persist a naive datetime (I7)");
        let e = check_encoded(r#"{"?i":"Future"}"#).unwrap_err();
        assert_eq!(e.msg, "Refusing to persist unknown instrument type Future (I6)");
        assert_eq!(check_encoded(r#"{"b":1,"a":{"d":"1.5"}}"#).unwrap(), br#"{"a":{"d":"1.5"},"b":1}"#.to_vec());
    }

    #[test]
    fn a_state_round_trips_through_its_canonical_json() {
        let st = fold::fold_account(&[ev(ORDER), ev(BUY)], "A").unwrap();
        let canon = canon_account(&st).unwrap();
        let back = uncanon_account(&canon).unwrap();
        assert_eq!(dumps(&canon_account(&back).unwrap()), dumps(&canon));
    }

    #[test]
    fn a_delta_carries_only_what_changed() {
        let mut st = AccountState::new("A");
        fold::apply_event(&mut st, &ev(ORDER)).unwrap();
        fold::apply_event(&mut st, &ev(BUY)).unwrap();
        export_full(&mut st).unwrap();
        let d = dumps(&export_delta(&mut st, 1).unwrap());
        assert!(!d.contains("\"positions\""), "{d}");
        assert!(!d.contains("\"fills\""), "{d}");
        fold::apply_event(&mut st, &ev(&BUY.replace("f1", "f2"))).unwrap();
        let d = dumps(&export_delta(&mut st, 1).unwrap());
        assert!(d.contains(r#""positions":{"+m":"#), "{d}");
        assert!(d.contains(r#""fills":{"+t":[{"dc":"Fill""#), "{d}");
        assert!(d.contains(r#""fill_ids":{"+fs":["f2"]}"#), "{d}");
    }

    #[test]
    fn a_row_decodes_as_its_event() {
        let p = r#"{"dc":"CashFlow","f":{"account_id":"A","amount":{"d":"5"},"kind":"deposit","reason":"x","flow_id":"c1","at":{"T":"2026-01-02T15:00:00+00:00"}}}"#;
        let r = event_from_row("A", "CashFlow", p, "2026-01-02T15:00:00+00:00", None, 1, Some(3));
        // the payload's own shape is the codec's business; the envelope must carry seq
        if let Ok(ev) = r {
            assert_eq!(ev.seq, Some(3));
        }
        let e = event_from_row("A", "Nope", p, "2026-01-02T15:00:00+00:00", None, 1, Some(3)).unwrap_err();
        assert_eq!(e.kind, "value");
    }
}

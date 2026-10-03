//! The canonical JSON of a folded state: the shape `tests/ledger_gen.canon` renders from
//! the Python dataclasses, produced natively so the fold parity test compares text.
//! Mappings keep insertion order, frozensets sort by their compact JSON text.
//!
//! The same shape is how a folded state reaches Python (P2b): [`export_full`] once, then
//! [`export_delta`] after appends, a patch the Python carrier builder applies without
//! knowing any rule: `{"+dc": name, "f": {...}}` replaces the named fields of the carrier it
//! patches (scalars are always sent), `{"+m": [[k, v], ...]}` sets entries of a map (the
//! touched ones, in position order), `{"+t": [...]}` appends to a tuple and `{"+fs": [...]}`
//! adds to a frozenset. A map the fold removed from is sent whole.

use super::codec::{dc, enc_instrument, enc_obj, enc_order, jdec, jenum, jnone, jodec, jostr, jstr, jtuple};
use super::fold::{AccountState, Position};
use super::json::{dumps, Json};
use super::mirror::{MirrorState, MirrorTicketState};
use super::model::{Instrument, Obj, Order, R};
use super::ops::OMap;
use super::pydec::PyDec;

fn jint(n: i128) -> Json {
    Json::Int(n)
}

fn pairs_of(pairs: Vec<(Json, Json)>) -> Json {
    Json::Arr(pairs.into_iter().map(|(k, v)| Json::Arr(vec![k, v])).collect())
}

fn map(pairs: Vec<(Json, Json)>) -> Json {
    Json::Obj(vec![("m".into(), pairs_of(pairs))])
}

fn frozenset(items: Vec<Json>) -> Json {
    let mut keyed: Vec<(String, Json)> = items.into_iter().map(|j| (dumps(&j), j)).collect();
    keyed.sort_by(|a, b| a.0.cmp(&b.0));
    Json::Obj(vec![("fs".into(), Json::Arr(keyed.into_iter().map(|(_, j)| j).collect()))])
}

// --- entry renderers: one key and one value renderer per map of the state ---------------

fn k_str(k: &String) -> R<Json> {
    Ok(jstr(k))
}

fn k_instr(k: &Instrument) -> R<Json> {
    enc_instrument(k)
}

fn k_leg(k: &(String, i128)) -> R<Json> {
    Ok(jtuple(vec![jstr(&k.0), jint(k.1)]))
}

fn k_book(k: &(String, Instrument)) -> R<Json> {
    Ok(jtuple(vec![jstr(&k.0), enc_instrument(&k.1)?]))
}

fn v_dec(v: &PyDec) -> R<Json> {
    jdec(v)
}

fn v_str(v: &String) -> R<Json> {
    Ok(jstr(v))
}

fn v_bool(v: &bool) -> R<Json> {
    Ok(Json::Bool(*v))
}

fn v_order(o: &Order) -> R<Json> {
    enc_order(o)
}

fn v_emulated(e: &super::model::EmulatedOrderState) -> R<Json> {
    enc_obj(&Obj::Emulated(e.clone()))
}

fn canon_position(p: &Position) -> R<Json> {
    let mut lots = Vec::new();
    for l in &p.open_lots {
        lots.push(enc_obj(&Obj::Lot(l.clone()))?);
    }
    Ok(dc(
        "Position",
        vec![
            ("account_id", jstr(&p.account_id)),
            ("instrument", enc_instrument(&p.instrument)?),
            ("quantity", jdec(&p.quantity)?),
            ("avg_cost", jdec(&p.avg_cost)?),
            ("realized_pnl", jdec(&p.realized_pnl)?),
            ("open_lots", jtuple(lots)),
        ],
    ))
}

fn full_map<K, V>(m: &OMap<K, V>, key: fn(&K) -> R<Json>, val: fn(&V) -> R<Json>) -> R<Json> {
    let mut out = Vec::with_capacity(m.len());
    for (k, v) in m.iter() {
        out.push((key(k)?, val(v)?));
    }
    Ok(map(out))
}

/// The map's part of a delta, and its journal reset: `None` when untouched, the whole map
/// when rebuilt, else `{"+m": touched entries}`.
fn map_part<K, V>(m: &mut OMap<K, V>, key: fn(&K) -> R<Json>, val: fn(&V) -> R<Json>) -> R<Option<Json>> {
    let (rebuilt, dirty) = m.take_journal();
    if rebuilt {
        return full_map(m, key, val).map(Some);
    }
    if dirty.is_empty() {
        return Ok(None);
    }
    let mut out = Vec::with_capacity(dirty.len());
    for i in dirty {
        let (k, v) = m.entry_at(i);
        out.push((key(k)?, val(v)?));
    }
    Ok(Some(Json::Obj(vec![("+m".into(), pairs_of(out))])))
}

fn canon_ticket(t: &MirrorTicketState) -> R<Json> {
    Ok(dc(
        "MirrorTicketState",
        vec![
            ("queued", enc_obj(&Obj::MQueued(t.queued.clone()))?),
            ("ack", match &t.ack {
                Some(a) => enc_obj(&Obj::MAck(a.clone()))?,
                None => jnone(),
            }),
            ("venue_order_id", jostr(&t.venue_order_id)),
            ("book_status", match t.book_status {
                Some(s) => jenum("OrderState", s.value()),
                None => jnone(),
            }),
            ("filled", jdec(&t.filled)?),
            ("avg_price", jodec(&t.avg_price)?),
            ("closed", Json::Bool(t.closed)),
            ("allocated", full_map(&t.allocated, k_str, v_dec)?),
        ],
    ))
}

fn canon_mirror(m: &MirrorState) -> R<Json> {
    Ok(dc(
        "MirrorState",
        vec![
            ("venue", jostr(&m.venue)),
            ("tickets", full_map(&m.tickets, k_str, canon_ticket)?),
            ("book", full_map(&m.book, k_book, v_dec)?),
            ("queued_orders", full_map(&m.queued_orders, k_str, v_str)?),
            ("refused_orders", full_map(&m.refused_orders, k_str, v_str)?),
            ("order_ids", full_map(&m.order_ids, k_str, v_str)?),
        ],
    ))
}

fn fills_json(fills: &[super::model::Fill]) -> R<Vec<Json>> {
    let mut out = Vec::with_capacity(fills.len());
    for f in fills {
        out.push(enc_obj(&Obj::Fill(f.clone()))?);
    }
    Ok(out)
}

fn last_reconcile(st: &AccountState) -> R<Json> {
    match &st.last_reconcile {
        Some(r) => enc_obj(&Obj::Reconcile(r.clone())),
        None => Ok(jnone()),
    }
}

pub fn canon_account(st: &AccountState) -> R<Json> {
    Ok(dc(
        "AccountState",
        vec![
            ("account_id", jstr(&st.account_id)),
            ("cash", jdec(&st.cash)?),
            ("positions", full_map(&st.positions, k_instr, canon_position)?),
            ("orders", full_map(&st.orders, k_str, v_order)?),
            ("filled_quantity", full_map(&st.filled_quantity, k_str, v_dec)?),
            ("leg_filled", full_map(&st.leg_filled, k_leg, v_dec)?),
            ("venue_order_ids", full_map(&st.venue_order_ids, k_str, v_str)?),
            ("emulated_orders", full_map(&st.emulated_orders, k_str, v_emulated)?),
            ("fills", jtuple(fills_json(&st.fills)?)),
            ("fill_ids", frozenset(st.fill_ids.iter().map(|s| jstr(s)).collect())),
            ("marks", full_map(&st.marks, k_instr, v_dec)?),
            ("realized_pnl", jdec(&st.realized_pnl)?),
            ("signals_seen", jint(st.signals_seen)),
            ("verdicts", jint(st.verdicts)),
            ("refusals", jint(st.refusals)),
            ("last_reconcile", last_reconcile(st)?),
            ("venue_halted", Json::Bool(st.venue_halted)),
            ("halted_venues", frozenset(st.halted_venues.iter().map(|s| jstr(s)).collect())),
            ("risk_controls", full_map(&st.risk_controls, k_str, v_bool)?),
            ("mirror", canon_mirror(&st.mirror)?),
            ("last_seq", jint(st.last_seq)),
        ],
    ))
}

/// `{"m":[[account, state], ...]}` in fold insertion order.
pub fn canon_states(states: &OMap<String, AccountState>) -> R<Json> {
    let mut out = Vec::new();
    for (k, st) in states.iter() {
        out.push((jstr(k), canon_account(st)?));
    }
    Ok(map(out))
}

// --- the export to Python (P2b) -----------------------------------------------------------

fn reset_journals(st: &mut AccountState) {
    st.positions.take_journal();
    st.orders.take_journal();
    st.filled_quantity.take_journal();
    st.leg_filled.take_journal();
    st.venue_order_ids.take_journal();
    st.emulated_orders.take_journal();
    st.marks.take_journal();
    st.risk_controls.take_journal();
    let m = &mut st.mirror;
    m.tickets.take_journal();
    m.book.take_journal();
    m.queued_orders.take_journal();
    m.refused_orders.take_journal();
    m.order_ids.take_journal();
}

/// The whole state, and every journal reset: what Python builds a fresh carrier from.
/// Python now holds `st.fills.len()` fills.
pub fn export_full(st: &mut AccountState) -> R<Json> {
    let out = canon_account(st)?;
    reset_journals(st);
    Ok(out)
}

/// Everything changed since the last export, as a patch over the carrier that export built.
/// `fills_held` is how many fills Python's carrier holds; the fold only ever appends them,
/// and `fill_ids` grows with them (one id per fill, `on_fill` is its only writer).
pub fn export_delta(st: &mut AccountState, fills_held: usize) -> R<Json> {
    let mut f: Vec<(&str, Json)> = vec![("account_id", jstr(&st.account_id)), ("cash", jdec(&st.cash)?)];
    let push = |f: &mut Vec<(&str, Json)>, name: &'static str, part: Option<Json>| {
        if let Some(p) = part {
            f.push((name, p));
        }
    };
    push(&mut f, "positions", map_part(&mut st.positions, k_instr, canon_position)?);
    push(&mut f, "orders", map_part(&mut st.orders, k_str, v_order)?);
    push(&mut f, "filled_quantity", map_part(&mut st.filled_quantity, k_str, v_dec)?);
    push(&mut f, "leg_filled", map_part(&mut st.leg_filled, k_leg, v_dec)?);
    push(&mut f, "venue_order_ids", map_part(&mut st.venue_order_ids, k_str, v_str)?);
    push(&mut f, "emulated_orders", map_part(&mut st.emulated_orders, k_str, v_emulated)?);
    if st.fills.len() > fills_held {
        let new = &st.fills[fills_held..];
        f.push(("fills", Json::Obj(vec![("+t".into(), Json::Arr(fills_json(new)?))])));
        let ids = new.iter().map(|x| jstr(&x.fill_id)).collect();
        f.push(("fill_ids", Json::Obj(vec![("+fs".into(), Json::Arr(ids))])));
    }
    push(&mut f, "marks", map_part(&mut st.marks, k_instr, v_dec)?);
    f.push(("realized_pnl", jdec(&st.realized_pnl)?));
    f.push(("signals_seen", jint(st.signals_seen)));
    f.push(("verdicts", jint(st.verdicts)));
    f.push(("refusals", jint(st.refusals)));
    f.push(("last_reconcile", last_reconcile(st)?));
    f.push(("venue_halted", Json::Bool(st.venue_halted)));
    f.push(("halted_venues", frozenset(st.halted_venues.iter().map(|s| jstr(s)).collect())));
    push(&mut f, "risk_controls", map_part(&mut st.risk_controls, k_str, v_bool)?);

    let m = &mut st.mirror;
    let mut mf: Vec<(&str, Json)> = vec![("venue", jostr(&m.venue))];
    push(&mut mf, "tickets", map_part(&mut m.tickets, k_str, canon_ticket)?);
    push(&mut mf, "book", map_part(&mut m.book, k_book, v_dec)?);
    push(&mut mf, "queued_orders", map_part(&mut m.queued_orders, k_str, v_str)?);
    push(&mut mf, "refused_orders", map_part(&mut m.refused_orders, k_str, v_str)?);
    push(&mut mf, "order_ids", map_part(&mut m.order_ids, k_str, v_str)?);
    f.push(("mirror", patch("MirrorState", mf)));
    f.push(("last_seq", jint(st.last_seq)));
    Ok(patch("AccountState", f))
}

fn patch(name: &str, fields: Vec<(&str, Json)>) -> Json {
    Json::Obj(vec![
        ("+dc".into(), jstr(name)),
        ("f".into(), Json::Obj(fields.into_iter().map(|(k, v)| (k.to_string(), v)).collect())),
    ])
}

/// A position as Python's `Position` carrier.
pub fn canon_position_pub(p: &Position) -> R<Json> {
    canon_position(p)
}

/// A mirror state as Python's `MirrorState` carrier.
pub fn canon_mirror_pub(m: &MirrorState) -> R<Json> {
    canon_mirror(m)
}

/// `ticket_contracts`' result as a `{"m": [[contract, quantity]]}` map.
pub fn canon_contracts(m: &OMap<Instrument, PyDec>) -> R<Json> {
    full_map(m, k_instr, v_dec)
}

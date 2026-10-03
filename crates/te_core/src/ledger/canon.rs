//! The canonical JSON of a folded state: the shape `tests/ledger_gen.canon` renders from
//! the Python dataclasses, produced natively so the fold parity test compares text.
//! Mappings keep insertion order, frozensets sort by their compact JSON text.

use super::codec::{dc, enc_instrument, enc_obj, enc_order, jdec, jnone, jodec, jostr, jstr, jtuple, jenum};
use super::fold::{AccountState, Position};
use super::json::{dumps, Json};
use super::mirror::{MirrorState, MirrorTicketState};
use super::model::{Obj, R};
use super::ops::OMap;
use super::pydec::PyDec;

fn jint(n: i128) -> Json {
    Json::Int(n)
}

fn map(pairs: Vec<(Json, Json)>) -> Json {
    Json::Obj(vec![("m".into(), Json::Arr(pairs.into_iter().map(|(k, v)| Json::Arr(vec![k, v])).collect()))])
}

fn frozenset(items: Vec<Json>) -> Json {
    let mut keyed: Vec<(String, Json)> = items.into_iter().map(|j| (dumps(&j), j)).collect();
    keyed.sort_by(|a, b| a.0.cmp(&b.0));
    Json::Obj(vec![("fs".into(), Json::Arr(keyed.into_iter().map(|(_, j)| j).collect()))])
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

fn dec_map<K>(m: &OMap<K, PyDec>, key: impl Fn(&K) -> R<Json>) -> R<Json> {
    let mut out = Vec::new();
    for (k, v) in m.iter() {
        out.push((key(k)?, jdec(v)?));
    }
    Ok(map(out))
}

fn str_map(m: &OMap<String, String>) -> Json {
    map(m.iter().map(|(k, v)| (jstr(k), jstr(v))).collect())
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
            ("allocated", dec_map(&t.allocated, |k| Ok(jstr(k)))?),
        ],
    ))
}

fn canon_mirror(m: &MirrorState) -> R<Json> {
    let mut tickets = Vec::new();
    for (k, t) in m.tickets.iter() {
        tickets.push((jstr(k), canon_ticket(t)?));
    }
    let book = dec_map(&m.book, |(acct, inst)| Ok(jtuple(vec![jstr(acct), enc_instrument(inst)?])))?;
    Ok(dc(
        "MirrorState",
        vec![
            ("venue", jostr(&m.venue)),
            ("tickets", map(tickets)),
            ("book", book),
            ("queued_orders", str_map(&m.queued_orders)),
            ("refused_orders", str_map(&m.refused_orders)),
            ("order_ids", str_map(&m.order_ids)),
        ],
    ))
}

pub fn canon_account(st: &AccountState) -> R<Json> {
    let mut positions = Vec::new();
    for (k, p) in st.positions.iter() {
        positions.push((enc_instrument(k)?, canon_position(p)?));
    }
    let mut orders = Vec::new();
    for (k, o) in st.orders.iter() {
        orders.push((jstr(k), enc_order(o)?));
    }
    let mut emulated = Vec::new();
    for (k, e) in st.emulated_orders.iter() {
        emulated.push((jstr(k), enc_obj(&Obj::Emulated(e.clone()))?));
    }
    let mut fills = Vec::new();
    for f in &st.fills {
        fills.push(enc_obj(&Obj::Fill(f.clone()))?);
    }
    Ok(dc(
        "AccountState",
        vec![
            ("account_id", jstr(&st.account_id)),
            ("cash", jdec(&st.cash)?),
            ("positions", map(positions)),
            ("orders", map(orders)),
            ("filled_quantity", dec_map(&st.filled_quantity, |k| Ok(jstr(k)))?),
            (
                "leg_filled",
                dec_map(&st.leg_filled, |(oid, idx)| Ok(jtuple(vec![jstr(oid), jint(*idx)])))?,
            ),
            ("venue_order_ids", str_map(&st.venue_order_ids)),
            ("emulated_orders", map(emulated)),
            ("fills", jtuple(fills)),
            ("fill_ids", frozenset(st.fill_ids.iter().map(|s| jstr(s)).collect())),
            ("marks", dec_map(&st.marks, enc_instrument)?),
            ("realized_pnl", jdec(&st.realized_pnl)?),
            ("signals_seen", jint(st.signals_seen)),
            ("verdicts", jint(st.verdicts)),
            ("refusals", jint(st.refusals)),
            ("last_reconcile", match &st.last_reconcile {
                Some(r) => enc_obj(&Obj::Reconcile(r.clone()))?,
                None => jnone(),
            }),
            ("venue_halted", Json::Bool(st.venue_halted)),
            ("halted_venues", frozenset(st.halted_venues.iter().map(|s| jstr(s)).collect())),
            (
                "risk_controls",
                map(st.risk_controls.iter().map(|(k, v)| (jstr(k), Json::Bool(*v))).collect()),
            ),
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

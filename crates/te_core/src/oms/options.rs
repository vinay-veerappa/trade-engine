//! What is left of `oms/options.py` once the manager's I/O is set aside (docs/RUST_PORT.md
//! P3b-1): the idempotency and fingerprint rules, the duplicate-entry (C4) and uncovered
//! call (C3 / I8) refusals on open, what a close needs (C5), the holding close, and which
//! children of a structure `sync` cancels or sends. The manager keeps submitting,
//! cancelling and appending; it asks these for the decision.

use crate::ledger::codec::py_repr;
use crate::ledger::fold::{make_position, AccountState};
use crate::ledger::json::{self, dumps, Json};
use crate::ledger::model::{err, Instrument, LErr, Order, OrderState, OrderType, Side, Tif, R};
use crate::ledger::ops::{add, eq, gt, mul_i, ne, zero};
use crate::money::Money;
use crate::oms::structures::{is_structure, legs_of, open_structures, uncovered_calls, Open};
use crate::options::pyrules::strip;
use crate::options::Right;
use crate::sim::snapshot::underlying_of;

// --- SHA-256 (the fingerprints; hand-written to leave the dependency set alone) ----------

const K: [u32; 64] = [
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5, 0xd807aa98,
    0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174, 0xe49b69c1, 0xefbe4786,
    0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da, 0x983e5152, 0xa831c66d, 0xb00327c8,
    0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967, 0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13,
    0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85, 0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819,
    0xd6990624, 0xf40e3585, 0x106aa070, 0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a,
    0x5b9cca4f, 0x682e6ff3, 0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7,
    0xc67178f2,
];

/// `hashlib.sha256(data).hexdigest()`.
pub fn sha256_hex(data: &[u8]) -> String {
    let mut h: [u32; 8] =
        [0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19];
    let mut msg = data.to_vec();
    msg.push(0x80);
    while msg.len() % 64 != 56 {
        msg.push(0);
    }
    msg.extend_from_slice(&((data.len() as u64) * 8).to_be_bytes());
    for chunk in msg.chunks(64) {
        let mut w = [0u32; 64];
        for (i, b) in chunk.chunks(4).enumerate() {
            w[i] = u32::from_be_bytes([b[0], b[1], b[2], b[3]]);
        }
        for i in 16..64 {
            let s0 = w[i - 15].rotate_right(7) ^ w[i - 15].rotate_right(18) ^ (w[i - 15] >> 3);
            let s1 = w[i - 2].rotate_right(17) ^ w[i - 2].rotate_right(19) ^ (w[i - 2] >> 10);
            w[i] = w[i - 16].wrapping_add(s0).wrapping_add(w[i - 7]).wrapping_add(s1);
        }
        let [mut a, mut b, mut c, mut d, mut e, mut f, mut g, mut hh] = h;
        for i in 0..64 {
            let s1 = e.rotate_right(6) ^ e.rotate_right(11) ^ e.rotate_right(25);
            let ch = (e & f) ^ (!e & g);
            let t1 = hh.wrapping_add(s1).wrapping_add(ch).wrapping_add(K[i]).wrapping_add(w[i]);
            let s0 = a.rotate_right(2) ^ a.rotate_right(13) ^ a.rotate_right(22);
            let maj = (a & b) ^ (a & c) ^ (b & c);
            let t2 = s0.wrapping_add(maj);
            hh = g;
            g = f;
            f = e;
            e = d.wrapping_add(t1);
            d = c;
            c = b;
            b = a;
            a = t1.wrapping_add(t2);
        }
        for (slot, v) in h.iter_mut().zip([a, b, c, d, e, f, g, hh]) {
            *slot = slot.wrapping_add(v);
        }
    }
    h.iter().map(|x| format!("{x:08x}")).collect()
}

// --- fingerprints and idempotency ---------------------------------------------------------

/// The terms of an `OptionIntent` the fingerprint covers. Decimals arrive as `str()`.
pub struct IntentTerms<'a> {
    pub intent_id: &'a str,
    pub account_id: &'a str,
    /// `encode_payload(intent.instrument)` as JSON text.
    pub instrument: &'a str,
    pub side: &'a str,
    pub quantity: &'a str,
    pub order_type: &'a str,
    pub limit_price: Option<&'a str>,
    pub tif: &'a str,
    pub profit_target: Option<&'a str>,
    pub reason: &'a str,
}

fn jerr(e: json::JsonError) -> LErr {
    LErr { kind: if e.unsupported { "unsupported" } else { "json" }, msg: e.message }
}

pub fn intent_fingerprint(t: &IntentTerms<'_>) -> R<String> {
    let s = |x: &str| Json::Str(x.to_string());
    let o = |x: Option<&str>| x.map_or(Json::Null, s);
    let payload = Json::Obj(vec![
        ("intent_id".into(), s(t.intent_id)),
        ("account_id".into(), s(t.account_id)),
        ("instrument".into(), json::parse(t.instrument).map_err(jerr)?),
        ("side".into(), s(t.side)),
        ("quantity".into(), s(t.quantity)),
        ("order_type".into(), s(t.order_type)),
        ("limit_price".into(), o(t.limit_price)),
        ("tif".into(), s(t.tif)),
        ("profit_target".into(), o(t.profit_target)),
        ("reason".into(), s(t.reason)),
    ]);
    Ok(sha256_hex(dumps(&payload).as_bytes()))
}

/// The fingerprint of an order's terms, not its instant: `created_at` becomes
/// `datetime.min` in the order's own offset. `order` is `encode_payload(order)` as text.
///
/// Deviation (docs/RUST_PORT.md P3b-1): the offset is the one the order's own timestamp
/// carries, where Python read the zone's offset at year 1; the two agree for UTC and for
/// any fixed-offset zone, which is what a clock returns.
pub fn order_fingerprint(order: &str) -> R<String> {
    let mut tree = json::parse(order).map_err(jerr)?;
    let Json::Obj(top) = &mut tree else { return err("value", "an order tree") };
    let fields = top.iter_mut().find(|(k, _)| k == "f").map(|(_, v)| v);
    let Some(Json::Obj(fields)) = fields else { return err("value", "an order tree has fields") };
    let slot = fields.iter_mut().find(|(k, _)| k == "created_at").map(|(_, v)| v);
    let Some(Json::Obj(stamp)) = slot else { return err("value", "an order tree has created_at") };
    let Some((_, Json::Str(iso))) = stamp.iter_mut().find(|(k, _)| k == "T") else {
        return err("value", "created_at is a timestamp");
    };
    let at = iso.find('T').map_or(0, |i| i + 1);
    let off = iso[at..].rfind(['+', '-']).map_or(iso.len(), |i| at + i);
    *iso = format!("0001-01-01T00:00:00{}", &iso[off..]);
    Ok(sha256_hex(dumps(&tree).as_bytes()))
}

fn idempotency<T>(command_id: &str) -> R<T> {
    err("idempotency", format!("command_id '{command_id}' was already used for a different command"))
}

/// A command id already on the ledger must be an ORDERS_CREATED of the same account.
pub fn replay_conflict(command_id: &str, orders_created: bool, same_account: bool) -> R<()> {
    if orders_created && same_account {
        Ok(())
    } else {
        idempotency(command_id)
    }
}

/// An `open` replay must carry the same fingerprint.
pub fn fingerprint_conflict(command_id: &str, existing: &str, expected: &str) -> R<()> {
    if existing == expected {
        Ok(())
    } else {
        idempotency(command_id)
    }
}

/// A `close` replay must have closed the same structure.
pub fn close_target_conflict(command_id: &str, closed: Option<&str>, entry_order_id: &str) -> R<()> {
    if closed == Some(entry_order_id) {
        return Ok(());
    }
    err(
        "idempotency",
        format!(
            "command_id '{command_id}' closed '{}', not '{entry_order_id}'",
            closed.unwrap_or("None")
        ),
    )
}

// --- open ---------------------------------------------------------------------------------

fn terminal(s: OrderState) -> bool {
    matches!(s, OrderState::Filled | OrderState::Cancelled | OrderState::Rejected | OrderState::Expired)
}

fn option_err<T>(msg: impl Into<String>) -> R<T> {
    err("option", msg)
}

/// C4: no entry on a contract the account holds or is already entering.
fn refuse_duplicate(st: &AccountState, account_id: &str, instrument: &Instrument, side: Side) -> R<()> {
    let wanted: Vec<Instrument> = {
        let mut seen: Vec<Instrument> = Vec::new();
        for leg in legs_of(instrument, side) {
            if !seen.iter().any(|c| c.same(&leg.contract)) {
                seen.push(leg.contract);
            }
        }
        seen
    };
    let mut busy: Vec<Instrument> = Vec::new();
    for (key, p) in st.positions.iter() {
        if matches!(key, Instrument::Option(_)) && ne(&p.quantity, &zero())? {
            busy.push(key.clone());
        }
    }
    for order in st.orders.values() {
        if order.parent_order_id.is_none() && !terminal(order.state) && is_structure(&order.instrument) {
            for leg in legs_of(&order.instrument, order.side) {
                busy.push(leg.contract);
            }
        }
    }
    let mut clash: Vec<String> = Vec::new();
    for c in &wanted {
        if busy.iter().any(|b| b.same(c)) {
            clash.push(strip(&c.symbol()?).to_string());
        }
    }
    clash.sort();
    if clash.is_empty() {
        return Ok(());
    }
    err(
        "duplicate",
        format!(
            "'{account_id}' already holds or is entering {}; a second entry on the same contract is refused (C4)",
            clash.join(", ")
        ),
    )
}

fn is_short_call(leg: &crate::ledger::model::ComboLeg) -> bool {
    leg.side == Side::Sell && matches!(&leg.contract, Instrument::Option(c) if c.right == Right::Call)
}

/// `state` as if the intent had filled, for the cover check only.
fn with_intent(st: &AccountState, instrument: &Instrument, side: Side, quantity: &Money) -> R<AccountState> {
    let mut out = st.clone();
    for leg in legs_of(instrument, side) {
        let change = mul_i(&mul_i(quantity, leg.ratio)?, if leg.side == Side::Buy { 1 } else { -1 })?;
        let current = out.positions.get(&leg.contract.hk()).map(|p| p.quantity.clone());
        let q = add(&current.unwrap_or_else(zero), &change)?;
        let position = make_position(&st.account_id, leg.contract.clone(), q, zero(), zero(), Vec::new())?;
        out.positions.insert(leg.contract.hk(), leg.contract.clone(), position);
    }
    Ok(out)
}

/// C3 / I8: a short call needs this account's shares or a long call behind it.
fn refuse_uncovered(st: &AccountState, account_id: &str, instrument: &Instrument, side: Side, quantity: &Money) -> R<()> {
    let legs = legs_of(instrument, side);
    let Some(first) = legs.iter().find(|l| is_short_call(l)) else { return Ok(()) };
    let underlying = underlying_of(&first.contract)?;
    let simulated = with_intent(st, instrument, side, quantity)?;
    let found = uncovered_calls(&simulated, true)?;
    let (needed, shares) = found
        .into_iter()
        .find(|(u, _, _)| *u == underlying)
        .map_or((zero(), zero()), |(_, n, s)| (n, s));
    if gt(&needed, &shares)? {
        return err(
            "uncovered",
            format!(
                "'{account_id}' would be short calls delivering {} {underlying} shares with {} of its own and no long call behind the rest; an account writes calls only on what it holds (C3, I8)",
                needed.canon(),
                shares.canon()
            ),
        );
    }
    Ok(())
}

/// The refusals of a new entry: duplicate (C4), then uncovered (C3), both only for an
/// options structure.
pub fn plan_open(st: &AccountState, account_id: &str, instrument: &Instrument, side: Side, quantity: &Money) -> R<()> {
    if is_structure(instrument) {
        refuse_duplicate(st, account_id, instrument, side)?;
        refuse_uncovered(st, account_id, instrument, side, quantity)?;
    }
    Ok(())
}

// --- close --------------------------------------------------------------------------------

/// What a close of one structure sends.
#[derive(Debug, Clone)]
pub struct ClosePlan {
    pub order_id: String,
    /// `single`: the one open leg's contract; `combo`: the open legs reversed.
    pub mode: &'static str,
    /// Indices into `legs_of(entry)` of the legs the close trades.
    pub legs: Vec<usize>,
    pub side: Side,
    pub quantity: Money,
    pub oco_group: String,
    pub target_order_id: Option<String>,
}

fn flip(s: Side) -> Side {
    if s == Side::Sell {
        Side::Buy
    } else {
        Side::Sell
    }
}

/// C5: a structure closes once, in whole units, and only while something is open.
pub fn plan_close(st: &AccountState, account_id: &str, entry_order_id: &str) -> R<ClosePlan> {
    let structures = open_structures(st)?;
    let Some(structure) = structures.iter().find(|s| s.entry_order_id == entry_order_id) else {
        return err(
            "closed",
            format!("'{entry_order_id}' in '{account_id}' has nothing open; it cannot be closed again (C5)"),
        );
    };
    if let Some(closing) = &structure.closing_order_id {
        return err(
            "closed",
            format!("'{entry_order_id}' already has close '{closing}' working; a second close could over-close it (C5)"),
        );
    }
    let entry = st
        .orders
        .get(&structure.entry_order_id)
        .ok_or_else(|| LErr { kind: "key", msg: py_repr(&structure.entry_order_id) })?;
    let legs = legs_of(&entry.instrument, entry.side);
    let mut open_legs: Vec<usize> = Vec::new();
    for (i, q) in structure.open_quantities.iter().enumerate() {
        if gt(q, &zero())? {
            open_legs.push(i);
        }
    }
    let (mode, side, quantity) = if open_legs.len() == 1 {
        let i = open_legs[0];
        ("single", flip(legs[i].side), structure.open_quantities[i].clone())
    } else {
        let mut skewed = false;
        for &i in &open_legs {
            let want = mul_i(&structure.units, legs[i].ratio)?;
            if ne(&structure.open_quantities[i], &want)? {
                skewed = true;
                break;
            }
        }
        if skewed {
            let mut shown: Vec<String> = Vec::new();
            for &i in &open_legs {
                let occ = legs[i].contract.symbol()?;
                shown.push(format!(
                    "({}, Decimal('{}'))",
                    py_repr(strip(&occ)),
                    structure.open_quantities[i].canon()
                ));
            }
            return option_err(format!(
                "'{}' holds its legs out of ratio ([{}]); close them one by one (I5)",
                structure.entry_order_id,
                shown.join(", ")
            ));
        }
        ("combo", flip(entry.side), structure.units.clone())
    };
    let prefix = format!("{}:close:", structure.entry_order_id);
    let number = 1 + st.orders.values().filter(|o| o.order_id.starts_with(&prefix)).count();
    Ok(ClosePlan {
        order_id: format!("{}:close:{number}", structure.entry_order_id),
        mode,
        legs: open_legs,
        side,
        quantity,
        oco_group: format!("{}:exits", structure.command_id),
        target_order_id: structure.target_order_id.clone(),
    })
}

/// The side of a holding close, or a refusal: nothing held to close (I8), or shares a
/// short call still needs (C3).
pub fn plan_holding(
    st: &AccountState,
    account_id: &str,
    instrument: &Instrument,
    quantity: &Money,
    command_id: &str,
) -> R<Side> {
    let held = st.positions.get(&instrument.hk()).map_or_else(zero, |p| p.quantity.clone());
    let symbol = instrument.symbol()?;
    if eq(&held, &zero())? || gt(quantity, &crate::ledger::ops::abs(&held)?)? {
        return option_err(format!(
            "'{account_id}' holds {} {symbol}; it cannot close {} (I8)",
            held.canon(),
            quantity.canon()
        ));
    }
    let side = if gt(&held, &zero())? { Side::Sell } else { Side::Buy };
    if side == Side::Sell {
        let mut after = st.clone();
        let (stamp, _) = crate::ledger::model::parse_datetime("2000-01-01T00:00:00+00:00")?;
        let order = Order {
            order_id: format!("{command_id}:holding"),
            account_id: account_id.to_string(),
            instrument: instrument.clone(),
            order_type: OrderType::Market,
            side,
            quantity: quantity.clone(),
            command_id: command_id.to_string(),
            created_at: stamp,
            limit_price: None,
            stop_price: None,
            trail_amount: None,
            tif: Tif::Day,
            state: OrderState::Accepted,
            parent_order_id: None,
            oco_group: None,
        };
        let id = order.order_id.clone();
        after.orders.put(&id, order);
        let found = uncovered_calls(&after, false)?;
        let (needed, shares) =
            found.into_iter().find(|(u, _, _)| *u == symbol).map_or((zero(), zero()), |(_, n, s)| (n, s));
        if gt(&needed, &shares)? {
            return err(
                "uncovered",
                format!(
                    "Selling {} {symbol} would leave short calls in '{account_id}' delivering {} shares with {} behind them; close the calls first or with it (C3, I8)",
                    quantity.canon(),
                    needed.canon(),
                    shares.canon()
                ),
            );
        }
    }
    Ok(side)
}

// --- sync ---------------------------------------------------------------------------------

/// What `sync` does to one child that still works.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Action {
    /// The structure has nothing left open: cancel the exit.
    StructureDone,
    /// A leg settled: cancel the target that would trade it.
    LegSettled,
    /// The entry filled: send the target if it has not gone yet.
    SubmitTarget,
}

impl Action {
    pub fn name(self) -> &'static str {
        match self {
            Action::StructureDone => "structure-done",
            Action::LegSettled => "leg-settled",
            Action::SubmitTarget => "submit-target",
        }
    }
}

fn open_of<'a>(opens: &'a [Open], entry: &str) -> Option<&'a Open> {
    opens.iter().find(|o| o.entry_order_id == entry)
}

fn leg_off(legs: &[crate::ledger::model::ComboLeg], s: &Open) -> R<bool> {
    for (leg, q) in legs.iter().zip(&s.open_quantities) {
        if ne(q, &mul_i(&s.units, leg.ratio)?)? {
            return Ok(true);
        }
    }
    Ok(false)
}

/// Every child of every structure entry, in `sync`'s order, with the action to take once
/// the host has re-read it and found it still working (`None`: leave it).
pub fn sync_plan(st: &AccountState) -> R<Vec<(String, Option<Action>)>> {
    let opens = open_structures(st)?;
    let mut entries: Vec<&Order> = st.orders.values().collect();
    entries.sort_by(|a, b| a.order_id.cmp(&b.order_id));
    let mut plan = Vec::new();
    for entry in entries {
        if entry.parent_order_id.is_some() || !is_structure(&entry.instrument) {
            continue;
        }
        let mut children: Vec<&Order> =
            st.orders.values().filter(|o| o.parent_order_id.as_deref() == Some(entry.order_id.as_str())).collect();
        children.sort_by(|a, b| a.order_id.cmp(&b.order_id));
        let structure = open_of(&opens, &entry.order_id);
        let filled = st.filled_quantity.get(&entry.order_id).cloned().unwrap_or_else(zero);
        let legs = legs_of(&entry.instrument, entry.side);
        for child in children {
            let is_target = child.order_id == format!("{}:target", entry.order_id);
            let action = match structure {
                None => {
                    if gt(&filled, &zero())? || terminal(entry.state) {
                        Some(Action::StructureDone)
                    } else if is_target && entry.state == OrderState::Filled {
                        Some(Action::SubmitTarget)
                    } else {
                        None
                    }
                }
                Some(s) => {
                    if is_target && leg_off(&legs, s)? {
                        Some(Action::LegSettled)
                    } else if is_target && entry.state == OrderState::Filled {
                        Some(Action::SubmitTarget)
                    } else {
                        None
                    }
                }
            };
            plan.push((child.order_id.clone(), action));
        }
    }
    Ok(plan)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sha256_known_vectors() {
        assert_eq!(sha256_hex(b""), "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855");
        assert_eq!(sha256_hex(b"abc"), "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad");
        assert_eq!(
            sha256_hex(b"abcdbcdecdefdefgefghfghighijhijkijkljklmklmnlmnomnopnopq"),
            "248d6a61d20638b8e5c026930c3e6039a33ce45964ff2167f6ecedd419db06c1"
        );
    }
}

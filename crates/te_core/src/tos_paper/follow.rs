//! The live follower's decisions (was `tos_paper/follow.py`, P5-T8): which of the sim's entries the
//! venue is sent this cycle and which it refuses, the pass name, and "the sim holds none of it".
//! Pure: the clock is the `now` and `max_age` inputs (I7). `SplitLedger` and `follow_cycle` (they
//! drive a ledger and a broker) stay Python until T9/T10.

use super::cover::{cover_reason, Sold};
use super::exits::{units, FOLLOW_PREFIX};
use super::wire::{jstr, obj, req, req_arr, req_str, wire};
use crate::ledger::bridge::{uncanon_account, uncanon_mirror};
use crate::ledger::fold::AccountState;
use crate::ledger::json::Json;
use crate::ledger::mirror::MirrorState;
use crate::ledger::model::{parse_datetime, DateTime, Instrument, Order, OrderState, Side, R};
use crate::ledger::ops::{eq, zero, OMap};
use crate::ledger::pydec::PyDec;
use crate::options::Right;
use chrono::{TimeZone, Timelike};
use chrono_tz::America::New_York;

fn ended_unfilled(state: OrderState) -> bool {
    matches!(state, OrderState::Cancelled | OrderState::Rejected | OrderState::Expired)
}

/// `a - b` in whole microseconds.
fn micros_between(a: &DateTime, b: &DateTime) -> i128 {
    let ((sa, ua), (sb, ub)) = (a.instant(), b.instant());
    (sa as i128 - sb as i128) * 1_000_000 + (ua as i128 - ub as i128)
}

/// `_flat`: the sim holds none of `order`'s instrument (a vertical by its spreads).
pub fn flat(state: &AccountState, order: &Order) -> R<bool> {
    if let Instrument::Combo(legs) = &order.instrument {
        let mut held: OMap<Instrument, PyDec> = OMap::new();
        for (_, p) in state.positions.iter() {
            held.insert(p.instrument.hk(), p.instrument.clone(), p.quantity.clone());
        }
        return match units(&held, legs)? {
            Some(u) => eq(&u, &zero()),
            None => Ok(false),
        };
    }
    match state.positions.get(&order.instrument.hk()) {
        Some(p) => eq(&p.quantity, &zero()),
        None => Ok(true),
    }
}

/// `pass_name`: `follow-HHMM` in New York.
pub fn pass_name(now: &DateTime) -> R<String> {
    let (total, _) = now.to_utc()?.instant();
    // seconds since 0001-01-01 -> unix seconds
    let unix = total - 719_163 * 86_400;
    let local = New_York.timestamp_opt(unix, 0).single();
    match local {
        Some(t) => Ok(format!("{FOLLOW_PREFIX}{:02}{:02}", t.hour(), t.minute())),
        None => wire("a pass time out of range"),
    }
}

/// What `_sold` gives up of an order: a SELL of shares or of a call.
fn sold_of(order: &Order) -> Sold {
    let single = match &order.instrument {
        Instrument::Equity(_) => true,
        Instrument::Option(c) => c.right == Right::Call,
        Instrument::Combo(_) | Instrument::Future(_) => false,
    };
    if order.side != Side::Sell || !single {
        return None;
    }
    Some((order.instrument.clone(), order.quantity.clone()))
}

pub struct Followed {
    pub send: Vec<(String, String)>,
    pub refused: Vec<(String, String, String)>,
}

/// `follow_entries`.
pub fn follow_entries(
    mirror: &MirrorState,
    accounts: &OMap<String, AccountState>,
    mirrored: &[String],
    session_open: &DateTime,
    now: &DateTime,
    max_age_us: i128,
) -> R<Followed> {
    let empty = AccountState::new("");
    let mut out = Followed { send: Vec::new(), refused: Vec::new() };
    for account in mirrored {
        let state = accounts.get(account).unwrap_or(&empty);
        let mut orders: Vec<&Order> = state.orders.values().collect();
        orders.sort_by(|a, b| a.order_id.cmp(&b.order_id));
        for order in orders {
            if order.parent_order_id.is_some()
                || order.created_at.instant() < session_open.instant()
                || ended_unfilled(order.state)
                || mirror.handled(&order.order_id)
            {
                continue;
            }
            let age = micros_between(now, &order.created_at);
            if age > max_age_us {
                let missing = cover_reason(mirror, &sold_of(order), &[])?;
                let seconds = max_age_us / 1_000_000;
                let reason = match missing {
                    None => format!(
                        "the follower first saw this entry {}s after the sim made it (more than {seconds}s); a late copy is not the sim's trade",
                        age / 1_000_000
                    ),
                    Some(m) => format!("{m}; the cover did not arrive within {seconds}s of the sim's entry"),
                };
                out.refused.push((order.order_id.clone(), account.clone(), reason));
            } else if order.state == OrderState::Filled && flat(state, order)? {
                out.refused.push((
                    order.order_id.clone(),
                    account.clone(),
                    "the sim opened and closed this entry before the follower saw it; the venue would open what the sim is out of"
                        .to_string(),
                ));
            } else {
                out.send.push((account.clone(), order.order_id.clone()));
            }
        }
    }
    Ok(out)
}

// -- the door ---------------------------------------------------------------------------------------

fn aware(doc: &Json, key: &str) -> R<DateTime> {
    let (t, is_aware) = parse_datetime(req_str(doc, key)?)?;
    if !is_aware {
        return wire(format!("{key} must be timezone-aware"));
    }
    Ok(t)
}

fn accounts_of(doc: &Json) -> R<OMap<String, AccountState>> {
    let mut accounts = OMap::new();
    for row in req_arr(doc, "accounts")? {
        let Json::Arr(r) = row else { return wire("an account row is not a pair") };
        let [Json::Str(name), tree] = r.as_slice() else { return wire("an account row is not a pair") };
        accounts.insert(name.clone(), name.clone(), uncanon_account(tree)?);
    }
    Ok(accounts)
}

pub fn follow_entries_op(doc: &Json) -> R<Json> {
    let mut mirrored = Vec::new();
    for m in req_arr(doc, "mirrored")? {
        let Json::Str(t) = m else { return wire("a mirrored account is not text") };
        mirrored.push(t.clone());
    }
    let max_age: i128 = match req(doc, "max_age_us")? {
        Json::Int(n) => *n as i128,
        _ => return wire("max_age_us is not an integer"),
    };
    let got = follow_entries(
        &uncanon_mirror(req(doc, "mirror")?)?,
        &accounts_of(doc)?,
        &mirrored,
        &aware(doc, "session_open")?,
        &aware(doc, "now")?,
        max_age,
    )?;
    Ok(obj(vec![
        ("send", Json::Arr(got.send.iter().map(|(a, o)| Json::Arr(vec![jstr(a.clone()), jstr(o.clone())])).collect())),
        (
            "refused",
            Json::Arr(
                got.refused
                    .iter()
                    .map(|(o, a, r)| Json::Arr(vec![jstr(o.clone()), jstr(a.clone()), jstr(r.clone())]))
                    .collect(),
            ),
        ),
    ]))
}

pub fn pass_name_op(doc: &Json) -> R<Json> {
    Ok(obj(vec![("name", jstr(pass_name(&aware(doc, "now")?)?))]))
}

pub fn flat_op(doc: &Json) -> R<Json> {
    let state = uncanon_account(req(doc, "account")?)?;
    let id = req_str(doc, "order_id")?;
    let Some(order) = state.orders.get(id) else { return wire("no such order") };
    Ok(obj(vec![("flat", Json::Bool(flat(&state, order)?))]))
}


#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_pass_is_named_for_the_new_york_minute() {
        let at = |t: &str| pass_name(&parse_datetime(t).unwrap().0).unwrap();
        assert_eq!(at("2026-09-28T16:35:00+00:00"), "follow-1235");
        assert_eq!(at("2026-12-01T16:35:59+00:00"), "follow-1135");
        assert_eq!(at("2026-09-28T09:35:00-04:00"), "follow-0935");
    }

    fn entry(id: &str, at: &str, parent: Option<&str>) -> Order {
        use crate::ledger::model::{OrderType, Tif};
        Order {
            order_id: id.into(),
            account_id: "A".into(),
            instrument: Instrument::Equity("AAPL".into()),
            order_type: OrderType::Limit,
            side: Side::Buy,
            quantity: PyDec::parse("1").unwrap(),
            command_id: id.into(),
            created_at: parse_datetime(at).unwrap().0,
            limit_price: Some(PyDec::parse("1").unwrap()),
            stop_price: None,
            trail_amount: None,
            tif: Tif::Day,
            state: OrderState::Accepted,
            parent_order_id: parent.map(String::from),
            oco_group: None,
        }
    }

    #[test]
    fn an_entry_at_exactly_max_age_is_sent_and_one_microsecond_older_is_refused() {
        let mut state = AccountState::new("A");
        for (id, at, parent) in [
            ("a", "2026-09-28T13:55:00+00:00", None),
            ("b", "2026-09-28T13:54:59.999999+00:00", None),
            ("c", "2026-09-28T13:00:00+00:00", Some("a")),
            ("d", "2026-09-28T09:58:00-04:00", None),
        ] {
            state.orders.insert(id.into(), id.into(), entry(id, at, parent));
        }
        let mut accounts = OMap::new();
        accounts.insert("A".to_string(), "A".to_string(), state);
        let now = parse_datetime("2026-09-28T14:00:00+00:00").unwrap().0;
        let open = parse_datetime("2026-09-28T13:00:00+00:00").unwrap().0;
        let got = follow_entries(&MirrorState::default(), &accounts, &["A".to_string()], &open, &now, 300_000_000).unwrap();
        let sent: Vec<&str> = got.send.iter().map(|(_, o)| o.as_str()).collect();
        assert_eq!(sent, vec!["a", "d"]);
        assert_eq!(got.refused.len(), 1);
        assert_eq!(got.refused[0].0, "b");
        assert_eq!(
            got.refused[0].2,
            "the follower first saw this entry 300s after the sim made it (more than 300s); a late copy is not the sim's trade"
        );
    }

    #[test]
    fn an_unaware_now_is_refused_at_the_door() {
        let doc = crate::ledger::json::parse(r#"{"now": "2026-09-28T16:35:00"}"#).unwrap();
        assert!(pass_name_op(&doc).is_err());
    }
}

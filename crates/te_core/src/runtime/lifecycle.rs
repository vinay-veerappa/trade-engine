//! After-close decisions. Observations arrive in original host order, never from a clock.
use std::collections::HashMap;

use super::{need, Plan};
use crate::ledger::model::{err, LErr, R};
use crate::ledger::ops::*;
use crate::money::Money;
use crate::options::{ContractWire, DecWire, OptionError, Right, Special};

fn dec(value: &str) -> R<Money> {
    Money::parse(value).ok_or_else(|| LErr { kind: "value", msg: "invalid lifecycle decimal".into() })
}
fn option_error(e: OptionError) -> LErr {
    LErr { kind: e.kind(), msg: e.message().into() }
}
fn contract(strike: &str, root: &str, date: &[i64], call: bool) -> R<ContractWire> {
    let k = dec(strike)?;
    Ok(ContractWire {
        underlying: root.into(), year: date[0] as i32, month: date[1] as u32, day: date[2] as u32,
        right: if call { Right::Call } else { Right::Put },
        strike: DecWire { neg: k.is_negative(), digits: k.coefficient().to_str_radix(10),
                          exp: k.exponent(), special: Special::Finite, text: strike.into() },
    })
}
fn intrinsic(c: &ContractWire, price: &str) -> R<Money> {
    // Reuse P1's existing option rule, including its accepted numeric range. P4b's
    // own arithmetic remains Money; expanding P1's range belongs to P7, not this port.
    dec(&crate::options::lifecycle::intrinsic(c,price).map_err(option_error)?)
}
/// A decimal that parses, is finite and is greater than zero. Text that does not parse is not positive.
fn positive(text: &str) -> bool {
    dec(text).map(|v| v.is_finite() && gt(&v, &zero()).unwrap_or(false)).unwrap_or(false)
}
/// Three consecutive texts of the flat input, starting at `at`.
fn triple(t: &[String], at: usize) -> [&str; 3] {
    [t[at].as_str(), t[at + 1].as_str(), t[at + 2].as_str()]
}

/// String-door adapter: each arm checks its input counts with `need`, then calls the typed rule
/// of the same name and pushes its output into the plan.
pub fn decide(op: &str, t: &[String], n: &[i64], b: &[bool], f: &[f64]) -> R<Plan> {
    let mut p = Plan::default();
    match op {
        "session" => {
            need(op, t, n, b, f, (1, 0, 1, 0))?;
            session(b[0], &t[0])?;
        }
        "compact" => {
            need(op, t, n, b, f, (1, 0, 0, 0))?;
            p.text.push(compact(&t[0]));
        }
        "accounts" => {
            need(op, t, n, b, f, (0, 0, 0, 0))?;
            p.text = accounts(t);
        }
        "close" => {
            need(op, t, n, b, f, (3, 1, 0, 0))?;
            close(n[0] < 0, &t[0], &t[1], &t[2])?;
        }
        "held" => {
            // One flag per (occ, quantity) row, so the row count is the minimum for `b`.
            need(op, t, n, b, f, (0, 0, t.len() / 2, 0))?;
            let rows: Vec<(&str, &str, bool)> = t.chunks_exact(2).enumerate()
                .map(|(i, row)| (row[0].as_str(), row[1].as_str(), b[i])).collect();
            p.numbers = held(&rows)?;
        }
        "position" => {
            need(op, t, n, b, f, (1, 1, 0, 0))?;
            let (long, due, past, quantity) = position(&t[0], n[0])?;
            p.flags = vec![long, due, past];
            p.text.push(quantity);
        }
        "overdue" => {
            need(op, t, n, b, f, (3, 1, 0, 0))?;
            overdue(n[0] < 0, &t[0], &t[1], &t[2])?;
        }
        "price_identity" => {
            need(op, t, n, b, f, (6, 0, 0, 0))?;
            price_identity(triple(t, 0), triple(t, 3))?;
        }
        "price_clock" => {
            need(op, t, n, b, f, (8, 1, 0, 0))?;
            price_clock(n[0] > 0, &t[1], &t[6], &t[7])?;
        }
        "price_known" => {
            need(op, t, n, b, f, (9, 1, 0, 0))?;
            price_known(n[0] < 0, &t[1], &t[6], &t[8])?;
        }
        "expiry" => {
            need(op, t, n, b, f, (4, 3, 2, 0))?;
            let (kind, reason) = expiry(&t[0], &t[1], &t[2], &t[3], n, b[0], b[1])?;
            p.text = vec![kind, reason];
        }
        "eligible" => {
            need(op, t, n, b, f, (0, 0, 3, 0))?;
            p.flags.push(eligible(b[0], b[1], b[2]));
        }
        "dividend_source" => {
            need(op, t, n, b, f, (2, 0, 1, 0))?;
            dividend_source(b[0], &t[0], &t[1])?;
        }
        "dividend_time" => {
            need(op, t, n, b, f, (1, 1, 0, 0))?;
            dividend_time(n[0] > 0, &t[0])?;
        }
        "sum" => {
            need(op, t, n, b, f, (0, 0, 0, 0))?;
            let (total, any) = sum(t)?;
            p.text.push(total);
            p.flags.push(any);
        }
        "itm" => {
            need(op, t, n, b, f, (3, 3, 1, 0))?;
            p.flags.push(itm(&t[0], &t[1], &t[2], n, b[0])?);
        }
        "quote_source" => {
            need(op, t, n, b, f, (3, 0, 1, 0))?;
            quote_source(b[0], &t[0], &t[1], &t[2])?;
        }
        "quote_time" => {
            need(op, t, n, b, f, (1, 1, 0, 0))?;
            quote_time(n[0] > 0, &t[0])?;
        }
        "early" => {
            need(op, t, n, b, f, (6, 3, 0, 0))?;
            let (exercise, text) = early(&t[0], &t[1], &t[2], &t[3], &t[4], &t[5], n)?;
            p.flags.push(exercise);
            p.text.push(text);
        }
        "event" => {
            need(op, t, n, b, f, (6, 0, 1, 0))?;
            let fields = [t[0].as_str(), t[1].as_str(), t[2].as_str(), t[3].as_str(), t[4].as_str(), t[5].as_str()];
            let (text, early_flag) = event(fields, b[0]);
            p.text = text;
            p.flags.push(early_flag);
        }
        "aware" => {
            need(op, t, n, b, f, (1, 0, 1, 0))?;
            aware(b[0], &t[0])?;
        }
        "value" => {
            need(op, t, n, b, f, (4, 0, 3, 0))?;
            value(b[0], &t[0], b[1], &t[1], &t[2], &t[3], b[2])?;
        }
        "action_amount" => {
            need(op, t, n, b, f, (4, 0, 1, 0))?;
            action_amount(b[0], &t[0], &t[1], &t[2], &t[3])?;
        }
        "mirror_net" => {
            need(op, t, n, b, f, (0, 0, 0, 0))?;
            let rows: Vec<(&str, &str)> = t.chunks_exact(2).map(|row| (row[0].as_str(), row[1].as_str())).collect();
            for (occ, net, first) in mirror_net(&rows)? {
                p.text.push(occ);
                p.text.push(net);
                p.numbers.push(first);
            }
        }
        "symbol_key" => {
            need(op, t, n, b, f, (1, 0, 0, 0))?;
            p.text.push(symbol_key(&t[0]));
        }
        "settlement_conflict" => {
            need(op, t, n, b, f, (3, 0, 1, 0))?;
            settlement_conflict(b[0], &t[0], &t[1], &t[2])?;
        }
        "settlement_missing" => {
            need(op, t, n, b, f, (3, 0, 0, 0))?;
            settlement_missing(&t[0], &t[1], &t[2])?;
        }
        "dividend_missing" => {
            need(op, t, n, b, f, (1, 0, 0, 0))?;
            dividend_missing(&t[0])?;
        }
        "quote_missing" => {
            need(op, t, n, b, f, (3, 0, 0, 0))?;
            quote_missing(&t[0], &t[1], &t[2])?;
        }
        _ => return err("value", format!("Unknown lifecycle decision {op}")),
    }
    Ok(p)
}

// Typed rules. Each keeps the string door's error kind and message byte-identical.

pub fn session(is_session: bool, text: &str) -> R<()> {
    if !is_session { return err("lifecycle", format!("{text} is not a session (I5)")); }
    Ok(())
}

pub fn compact(text: &str) -> String {
    text.replace(' ', "")
}

pub fn accounts(names: &[String]) -> Vec<String> {
    let mut sorted = names.to_vec();
    sorted.sort();
    sorted
}

/// Refuses a clock that reads before the session's close: expiry is decided after the close.
pub fn close(before_close: bool, clock: &str, session_name: &str, close_time: &str) -> R<()> {
    if before_close {
        return err("lifecycle", format!("The clock reads {clock}, before the {session_name} close {close_time}; expiry and assignment are decided after the close (I9)"));
    }
    Ok(())
}

/// Indices (into the input rows) of the option rows with a non-zero quantity, sorted by occ.
/// Rows are (occ, quantity, is_option); a quantity is only parsed for an option row.
pub fn held(rows: &[(&str, &str, bool)]) -> R<Vec<i64>> {
    let mut kept: Vec<(String, i64)> = Vec::new();
    for (i, &(occ, quantity, is_option)) in rows.iter().enumerate() {
        if is_option && !eq(&dec(quantity)?, &zero())? { kept.push((occ.to_string(), i as i64)); }
    }
    kept.sort_by(|a, b| a.0.cmp(&b.0));
    Ok(kept.into_iter().map(|v| v.1).collect())
}

/// `(long, due_at_expiry, past_expiry, absolute_quantity_text)`. `days_to_expiry` is the session's
/// day count to the contract's expiry.
pub fn position(quantity: &str, days_to_expiry: i64) -> R<(bool, bool, bool, String)> {
    let q = dec(quantity)?;
    Ok((gt(&q, &zero())?, days_to_expiry <= 0, days_to_expiry < 0, s(&abs(&q)?)))
}

pub fn overdue(expired_before_session: bool, occ: &str, account: &str, expiry: &str) -> R<()> {
    if expired_before_session {
        return err("lifecycle", format!("{occ} in '{account}' expired on {expiry} and was never settled; run the lifecycle pass for that session first (I9)"));
    }
    Ok(())
}

/// The settlement asked for and the one found must name the same (kind, underlying, session).
pub fn price_identity(asked: [&str; 3], got: [&str; 3]) -> R<()> {
    if asked != got {
        return err("lifecycle", format!("Asked for the {} settlement of {} on {}, got {} {} on {} (I5)",
            asked[0], asked[1], asked[2], got[0], got[1], got[2]));
    }
    Ok(())
}

pub fn price_clock(after_clock: bool, name: &str, stamp: &str, clock: &str) -> R<()> {
    if after_clock {
        return err("lifecycle", format!("{name} settlement is stamped {stamp}, after the clock {clock}: look-ahead (I7)"));
    }
    Ok(())
}

pub fn price_known(before_instant: bool, name: &str, stamp: &str, instant: &str) -> R<()> {
    if before_instant {
        return err("lifecycle", format!("{name} settlement is stamped {stamp}, before the settlement instant {instant}; it cannot be the official price (I9)"));
    }
    Ok(())
}

/// `(kind, reason)` at expiry. Worthless below the 0.01 threshold; otherwise exercised or assigned.
pub fn expiry(strike: &str, settlement: &str, label: &str, root: &str, date: &[i64], call: bool, exercised: bool) -> R<(String, String)> {
    let v = intrinsic(&contract(strike, root, date, call)?, settlement)?;
    let expired = lt(&v, &dec("0.01")?)?;
    let kind = if expired { "Expiry" } else if exercised { "Exercise" } else { "Assignment" };
    let reason = if expired {
        format!("expired worthless: {} in the money at the {} settlement {}, under the 0.01 exercise threshold", s(&v), label, settlement)
    } else {
        format!("{} at expiry: {} in the money at the {} settlement {}", if exercised { "exercised" } else { "assigned" }, s(&v), label, settlement)
    };
    Ok((kind.into(), reason))
}

pub fn eligible(short: bool, call: bool, can_exercise: bool) -> bool {
    short && call && can_exercise
}

pub fn dividend_source(configured: bool, account: &str, occ: &str) -> R<()> {
    if !configured {
        return err("lifecycle", format!("'{account}' is short the American call {occ} and no dividend source is configured, so its early assignment cannot be decided (I5)"));
    }
    Ok(())
}

pub fn dividend_time(after_clock: bool, underlying: &str) -> R<()> {
    if after_clock {
        return err("lifecycle", format!("{underlying} dividend record is from after the clock: look-ahead (I7)"));
    }
    Ok(())
}

/// `(sum_text, any)`: the Money total of the amounts, and whether there was at least one.
pub fn sum(amounts: &[String]) -> R<(String, bool)> {
    let mut total = zero();
    for v in amounts { total = add(&total, &dec(v)?)?; }
    Ok((s(&total), !amounts.is_empty()))
}

/// True when the call is in the money at the settlement, i.e. its intrinsic value clears 0.01.
pub fn itm(strike: &str, settlement: &str, root: &str, date: &[i64], call: bool) -> R<bool> {
    Ok(!lt(&intrinsic(&contract(strike, root, date, call)?, settlement)?, &dec("0.01")?)?)
}

pub fn quote_source(configured: bool, occ: &str, account: &str, dividend_amount: &str) -> R<()> {
    if !configured {
        return err("lifecycle", format!("{occ} in '{account}' is in the money before a {dividend_amount} dividend and no option quote source is configured (I5)"));
    }
    Ok(())
}

pub fn quote_time(after_clock: bool, occ: &str) -> R<()> {
    if after_clock {
        return err("stale", format!("{occ} quote is from after the clock: look-ahead (I7)"));
    }
    Ok(())
}

/// `(exercise, text)` for an early assignment. The strike, closing price, bid, dividend, ex date
/// and root come first, then the expiry date.
pub fn early(strike: &str, close_price: &str, bid: &str, dividend: &str, ex_date: &str, root: &str, date: &[i64]) -> R<(bool, String)> {
    let c = contract(strike, root, date, true)?;
    let exercise = crate::options::lifecycle::exercised_for_dividend(&c, close_price, bid, dividend).map_err(option_error)?;
    let extrinsic = sub(&dec(bid)?, &intrinsic(&c, close_price)?)?;
    Ok((exercise, format!("assigned early: goes ex a {dividend} dividend on {ex_date}, more than the call's extrinsic value {} (bid {bid}, close {close_price})", s(&extrinsic))))
}

/// `(text, early_flag)`: the first five fields as given, then a key `lifecycle:{f0}:{f1 without
/// spaces}:{f5}`, with `:early` appended when `early` is set. The flag is `early`.
pub fn event(fields: [&str; 6], early: bool) -> (Vec<String>, bool) {
    let key = format!("lifecycle:{}:{}:{}{}", fields[0], fields[1].replace(' ', ""), fields[5],
        if early { ":early" } else { "" });
    let mut text: Vec<String> = fields[..5].iter().map(|field| field.to_string()).collect();
    text.push(key);
    (text, early)
}

pub fn aware(is_aware: bool, text: &str) -> R<()> {
    if !is_aware { return err("value", format!("{text} must be timezone-aware UTC (I7)")); }
    Ok(())
}

/// Checks one settlement price or dividend record. `owner` names the record type, `shown` is the
/// value as the caller wrote it.
pub fn value(is_price: bool, underlying: &str, has_amount: bool, amount: &str, owner: &str, shown: &str, has_source: bool) -> R<()> {
    if is_price && underlying.is_empty() { return err("value", "SettlementPrice.underlying must be non-empty"); }
    if !(has_amount && positive(amount)) {
        return err("value", format!("{owner}.{} must be a positive Decimal, got {shown} (I5)", if is_price { "price" } else { "amount" }));
    }
    if !has_source { return err("value", format!("{owner}.source must be non-empty (I11)")); }
    Ok(())
}

pub fn action_amount(has_amount: bool, amount: &str, symbol: &str, ex_date: &str, raw: &str) -> R<()> {
    if !(has_amount && positive(amount)) {
        return err("stale", format!("{symbol} dividend going ex {ex_date} has no usable amount: {raw} (I5)"));
    }
    Ok(())
}

/// Each occ's quantities summed from zero with Money, in first-appearance order; a contract that
/// nets to zero leaves the book. Returns `(occ, net_text, first_row_index)` per kept occ.
pub fn mirror_net(rows: &[(&str, &str)]) -> R<Vec<(String, String, i64)>> {
    let mut slot: HashMap<&str, usize> = HashMap::new();
    let mut nets: Vec<(&str, i64, Money)> = Vec::new();
    for (i, &(occ, quantity)) in rows.iter().enumerate() {
        let q = dec(quantity)?;
        match slot.get(occ) {
            Some(&k) => {
                let total = add(&nets[k].2, &q)?;
                nets[k].2 = total;
            }
            None => {
                slot.insert(occ, nets.len());
                let total = add(&zero(), &q)?;
                nets.push((occ, i as i64, total));
            }
        }
    }
    let mut kept = Vec::new();
    for (occ, first, net) in nets {
        if !eq(&net, &zero())? { kept.push((occ.to_string(), s(&net), first)); }
    }
    Ok(kept)
}

/// Python's `str.strip()` also strips U+001C..U+001F, which Rust's trim() does not, so use the
/// shared py_strip to match the Python side exactly.
pub fn symbol_key(text: &str) -> String {
    super::py_strip(text).to_uppercase()
}

pub fn settlement_conflict(conflict: bool, kind: &str, underlying: &str, date: &str) -> R<()> {
    if conflict {
        return err("value", format!("Two different {kind} settlements for {underlying} on {date} (I5)"));
    }
    Ok(())
}

/// Always refuses: the caller asked for a settlement that the source does not hold.
pub fn settlement_missing(kind: &str, underlying: &str, date: &str) -> R<()> {
    err("stale", format!("No {kind} settlement for {underlying} on {date} (I5)"))
}

/// Always refuses: no dividend record exists for the symbol.
pub fn dividend_missing(key: &str) -> R<()> {
    err("stale", format!("No dividend record for {key} (I5)"))
}

/// Always refuses: the contract is not in the chain snapshot.
pub fn quote_missing(occ: &str, underlying: &str, as_of: &str) -> R<()> {
    err("stale", format!("{occ} is not in the {underlying} chain snapshot of {as_of} (I5)"))
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn exact_threshold_and_scale() {
        let p = decide("expiry",&["100".into(),"100.010".into(),"PM".into(),"AAPL".into()],&[2026,10,16],&[true,true],&[]).unwrap();
        assert_eq!(p.text,["Exercise","exercised at expiry: 0.01 in the money at the PM settlement 100.010"]);
        assert!(!decide("early",&["100".into(),"101".into(),"2".into(),"1".into(),"2026-10-19".into(),"AAPL".into()],&[2026,10,16],&[],&[]).unwrap().flags[0]);
    }
    fn texts(v: &[&str]) -> Vec<String> {
        v.iter().map(|x| x.to_string()).collect()
    }
    fn refuse(op: &str, t: &[&str], n: &[i64], b: &[bool]) -> LErr {
        decide(op, &texts(t), n, b, &[]).err().expect("the op refuses")
    }
    #[test]
    fn mirror_net_sums_two_rows_of_one_contract() {
        let p = decide("mirror_net", &texts(&["AAPL 1", "2", "AAPL 1", "3"]), &[], &[], &[]).unwrap();
        assert_eq!(p.text, ["AAPL 1", "5"]);
        assert_eq!(p.numbers, [0]);
    }
    #[test]
    fn mirror_net_drops_a_contract_that_nets_to_zero() {
        let p = decide("mirror_net", &texts(&["A", "2", "B", "4", "A", "-2", "C", "1"]), &[], &[], &[]).unwrap();
        assert_eq!(p.text, ["B", "4", "C", "1"]);
        assert_eq!(p.numbers, [1, 3]);
    }
    #[test]
    fn mirror_net_keeps_first_appearance_order_and_indices() {
        let p = decide("mirror_net", &texts(&["B", "1", "A", "2", "B", "3"]), &[], &[], &[]).unwrap();
        assert_eq!(p.text, ["B", "4", "A", "2"]);
        assert_eq!(p.numbers, [0, 1]);
    }
    #[test]
    fn mirror_net_with_no_rows_is_empty() {
        let p = decide("mirror_net", &[], &[], &[], &[]).unwrap();
        assert!(p.text.is_empty() && p.numbers.is_empty());
    }
    #[test]
    fn symbol_key_trims_and_uppercases() {
        assert_eq!(decide("symbol_key", &texts(&[" aapl\t"]), &[], &[], &[]).unwrap().text, ["AAPL"]);
        assert_eq!(decide("symbol_key", &texts(&["\u{a0}spx\u{3000}"]), &[], &[], &[]).unwrap().text, ["SPX"]);
    }
    #[test]
    fn symbol_key_strips_information_separators_like_python() {
        // Python's str.strip() drops U+001C..U+001F; Rust's trim() does not.
        assert_eq!(decide("symbol_key", &texts(&["\u{1c}spx\u{1f}"]), &[], &[], &[]).unwrap().text, ["SPX"]);
    }
    #[test]
    fn settlement_conflict_passes_when_the_prices_agree() {
        assert!(decide("settlement_conflict", &texts(&["PM", "AAPL", "2026-10-16"]), &[], &[false], &[]).is_ok());
    }
    #[test]
    fn settlement_conflict_refuses_as_value() {
        let e = refuse("settlement_conflict", &["PM", "AAPL", "2026-10-16"], &[], &[true]);
        assert_eq!((e.kind, e.msg.as_str()), ("value", "Two different PM settlements for AAPL on 2026-10-16 (I5)"));
    }
    #[test]
    fn missing_settlement_refuses_as_stale() {
        let e = refuse("settlement_missing", &["AM", "SPX", "2026-10-16"], &[], &[]);
        assert_eq!((e.kind, e.msg.as_str()), ("stale", "No AM settlement for SPX on 2026-10-16 (I5)"));
    }
    #[test]
    fn missing_dividend_record_refuses_as_stale() {
        let e = refuse("dividend_missing", &["AAPL"], &[], &[]);
        assert_eq!((e.kind, e.msg.as_str()), ("stale", "No dividend record for AAPL (I5)"));
    }
    #[test]
    fn missing_quote_refuses_as_stale() {
        let e = refuse("quote_missing", &["AAPL261016C00250000", "AAPL", "2026-10-15T20:00:00+00:00"], &[], &[]);
        assert_eq!((e.kind, e.msg.as_str()), ("stale",
            "AAPL261016C00250000 is not in the AAPL chain snapshot of 2026-10-15T20:00:00+00:00 (I5)"));
    }
}

#[cfg(test)]
mod typed_tests {
    use super::*;

    /// Every arm name the string door dispatches to in this file, in match order.
    const OPS: &[&str] = &[
        "session", "compact", "accounts", "close", "held", "position", "overdue",
        "price_identity", "price_clock", "price_known", "expiry", "eligible", "dividend_source",
        "dividend_time", "sum", "itm", "quote_source", "quote_time", "early", "event", "aware",
        "value", "action_amount", "mirror_net", "symbol_key", "settlement_conflict",
        "settlement_missing", "dividend_missing", "quote_missing",
    ];

    /// Each arm's minimum input counts (s, n, b, f), as its adapter's `need` call states them.
    /// `held` takes one flag per row, so its minimum is 0 for an empty input.
    const MINIMA: &[(&str, (usize, usize, usize, usize))] = &[
        ("session", (1, 0, 1, 0)), ("compact", (1, 0, 0, 0)), ("accounts", (0, 0, 0, 0)),
        ("close", (3, 1, 0, 0)), ("held", (0, 0, 0, 0)), ("position", (1, 1, 0, 0)),
        ("overdue", (3, 1, 0, 0)), ("price_identity", (6, 0, 0, 0)), ("price_clock", (8, 1, 0, 0)),
        ("price_known", (9, 1, 0, 0)), ("expiry", (4, 3, 2, 0)), ("eligible", (0, 0, 3, 0)),
        ("dividend_source", (2, 0, 1, 0)), ("dividend_time", (1, 1, 0, 0)), ("sum", (0, 0, 0, 0)),
        ("itm", (3, 3, 1, 0)), ("quote_source", (3, 0, 1, 0)), ("quote_time", (1, 1, 0, 0)),
        ("early", (6, 3, 0, 0)), ("event", (6, 0, 1, 0)), ("aware", (1, 0, 1, 0)),
        ("value", (4, 0, 3, 0)), ("action_amount", (4, 0, 1, 0)), ("mirror_net", (0, 0, 0, 0)),
        ("symbol_key", (1, 0, 0, 0)), ("settlement_conflict", (3, 0, 1, 0)),
        ("settlement_missing", (3, 0, 0, 0)), ("dividend_missing", (1, 0, 0, 0)),
        ("quote_missing", (3, 0, 0, 0)),
    ];

    #[test]
    fn every_arm_is_listed_once_and_in_order() {
        let names: Vec<&str> = MINIMA.iter().map(|(op, _)| *op).collect();
        assert_eq!(names, OPS);
    }

    #[test]
    fn no_arm_is_reported_as_unknown_on_empty_input() {
        for op in OPS {
            if let Err(e) = decide(op, &[], &[], &[], &[]) {
                assert!(!e.msg.starts_with("Unknown"), "{op} is not an arm");
            }
        }
    }

    #[test]
    fn an_arm_with_a_minimum_refuses_short_input_as_arity() {
        for (op, min) in MINIMA {
            if *min == (0, 0, 0, 0) { continue; }
            let expected = format!("{op} arity");
            match decide(op, &[], &[], &[], &[]) {
                Err(e) => assert_eq!((e.kind, e.msg.as_str()), ("value", expected.as_str()), "{op}"),
                Ok(_) => panic!("{op} must refuse input shorter than its minimum"),
            }
        }
    }

    #[test]
    fn an_arm_runs_at_exactly_its_minimum_without_panicking() {
        for (op, (s, n, b, f)) in MINIMA {
            let _ = decide(op, &vec![String::new(); *s], &vec![0; *n], &vec![false; *b], &vec![0.0; *f]);
        }
    }

    #[test]
    fn session_refuses_a_non_session_with_its_text() {
        let e = session(false, "2026-11-24").unwrap_err();
        assert_eq!((e.kind, e.msg.as_str()), ("lifecycle", "2026-11-24 is not a session (I5)"));
        assert!(session(true, "2026-11-24").is_ok());
    }

    #[test]
    fn compact_and_accounts_are_pure() {
        assert_eq!(compact("A B C"), "ABC");
        assert_eq!(accounts(&["B".to_string(), "A".to_string()]), ["A", "B"]);
    }

    #[test]
    fn close_refuses_a_clock_before_the_close() {
        let e = close(true, "10:00", "NYSE", "16:00").unwrap_err();
        assert_eq!(e.msg, "The clock reads 10:00, before the NYSE close 16:00; expiry and assignment are decided after the close (I9)");
        assert!(close(false, "17:00", "NYSE", "16:00").is_ok());
    }

    #[test]
    fn held_keeps_nonzero_option_rows_sorted_by_occ() {
        let rows = [("B", "1", true), ("A", "0", true), ("C", "2", false), ("D", "3", true)];
        assert_eq!(held(&rows).unwrap(), vec![0, 3]);
        assert!(held(&[]).unwrap().is_empty());
    }

    #[test]
    fn position_reports_long_due_past_and_absolute_quantity() {
        assert_eq!(position("3", 0).unwrap(), (true, true, false, "3".to_string()));
        assert_eq!(position("-2", -1).unwrap(), (false, true, true, "2".to_string()));
        assert_eq!(position("3", 5).unwrap(), (true, false, false, "3".to_string()));
    }

    #[test]
    fn overdue_refuses_an_unsettled_past_expiry() {
        let e = overdue(true, "OCC", "A", "2026-10-16").unwrap_err();
        assert_eq!(e.msg, "OCC in 'A' expired on 2026-10-16 and was never settled; run the lifecycle pass for that session first (I9)");
        assert!(overdue(false, "OCC", "A", "2026-10-16").is_ok());
    }

    #[test]
    fn price_identity_names_the_settlement_asked_and_found() {
        let e = price_identity(["PM", "AAPL", "d"], ["AM", "AAPL", "d"]).unwrap_err();
        assert_eq!(e.msg, "Asked for the PM settlement of AAPL on d, got AM AAPL on d (I5)");
        assert!(price_identity(["PM", "AAPL", "d"], ["PM", "AAPL", "d"]).is_ok());
    }

    #[test]
    fn price_clock_and_known_refuse_with_their_own_messages() {
        let e = price_clock(true, "PM", "t1", "t0").unwrap_err();
        assert_eq!(e.msg, "PM settlement is stamped t1, after the clock t0: look-ahead (I7)");
        let e = price_known(true, "PM", "t1", "t0").unwrap_err();
        assert_eq!(e.msg, "PM settlement is stamped t1, before the settlement instant t0; it cannot be the official price (I9)");
        assert!(price_clock(false, "PM", "t1", "t0").is_ok());
        assert!(price_known(false, "PM", "t1", "t0").is_ok());
    }

    #[test]
    fn expiry_exercises_at_the_threshold_as_the_original_test_states() {
        let (kind, reason) = expiry("100", "100.010", "PM", "AAPL", &[2026, 10, 16], true, true).unwrap();
        assert_eq!(kind, "Exercise");
        assert_eq!(reason, "exercised at expiry: 0.01 in the money at the PM settlement 100.010");
    }

    #[test]
    fn expiry_is_worthless_below_the_threshold_and_assigned_otherwise() {
        assert_eq!(expiry("100", "99", "PM", "AAPL", &[2026, 10, 16], true, false).unwrap().0, "Expiry");
        let (kind, reason) = expiry("100", "105", "PM", "AAPL", &[2026, 10, 16], true, false).unwrap();
        assert_eq!(kind, "Assignment");
        assert!(reason.starts_with("assigned at expiry:"));
    }

    #[test]
    fn eligible_needs_short_call_and_exercisable() {
        assert!(eligible(true, true, true));
        assert!(!eligible(true, true, false));
        assert!(!eligible(false, true, true));
        assert!(!eligible(true, false, true));
    }

    #[test]
    fn dividend_source_and_time_refuse_with_their_text() {
        let e = dividend_source(false, "A", "OCC").unwrap_err();
        assert_eq!(e.msg, "'A' is short the American call OCC and no dividend source is configured, so its early assignment cannot be decided (I5)");
        assert!(dividend_source(true, "A", "OCC").is_ok());
        let e = dividend_time(true, "SPX").unwrap_err();
        assert_eq!(e.msg, "SPX dividend record is from after the clock: look-ahead (I7)");
        assert!(dividend_time(false, "SPX").is_ok());
    }

    #[test]
    fn sum_adds_money_and_flags_whether_any_amount_was_given() {
        assert_eq!(sum(&["1.5".to_string(), "2".to_string()]).unwrap(), ("3.5".to_string(), true));
        assert!(!sum(&[]).unwrap().1);
    }

    #[test]
    fn itm_clears_the_0_01_threshold() {
        assert!(itm("100", "101", "AAPL", &[2026, 10, 16], true).unwrap());
        assert!(!itm("100", "100.005", "AAPL", &[2026, 10, 16], true).unwrap());
    }

    #[test]
    fn quote_source_and_time_refuse_with_their_text() {
        let e = quote_source(false, "OCC", "A", "0.5").unwrap_err();
        assert_eq!(e.msg, "OCC in 'A' is in the money before a 0.5 dividend and no option quote source is configured (I5)");
        assert!(quote_source(true, "OCC", "A", "0.5").is_ok());
        let e = quote_time(true, "OCC").unwrap_err();
        assert_eq!((e.kind, e.msg.as_str()), ("stale", "OCC quote is from after the clock: look-ahead (I7)"));
        assert!(quote_time(false, "OCC").is_ok());
    }

    #[test]
    fn early_does_not_exercise_when_the_bid_is_below_the_dividend_value() {
        let (exercise, text) = early("100", "101", "2", "1", "2026-10-19", "AAPL", &[2026, 10, 16]).unwrap();
        assert!(!exercise);
        assert!(text.starts_with("assigned early: goes ex a 1 dividend on 2026-10-19"));
    }

    #[test]
    fn event_builds_its_key_and_marks_early() {
        let fields = ["a", "b c", "d", "e", "f", "g"];
        let (text, flag) = event(fields, true);
        assert_eq!(text, ["a", "b c", "d", "e", "f", "lifecycle:a:bc:g:early"]);
        assert!(flag);
        assert_eq!(event(fields, false).0[5], "lifecycle:a:bc:g");
    }

    #[test]
    fn aware_refuses_a_naive_timestamp() {
        let e = aware(false, "ts").unwrap_err();
        assert_eq!((e.kind, e.msg.as_str()), ("value", "ts must be timezone-aware UTC (I7)"));
        assert!(aware(true, "ts").is_ok());
    }

    #[test]
    fn value_refuses_each_rule_with_its_own_message() {
        let e = value(true, "", true, "1", "SettlementPrice", "1", true).unwrap_err();
        assert_eq!(e.msg, "SettlementPrice.underlying must be non-empty");
        let e = value(false, "X", true, "0", "Dividend", "0", true).unwrap_err();
        assert_eq!(e.msg, "Dividend.amount must be a positive Decimal, got 0 (I5)");
        let e = value(true, "X", true, "1", "SP", "1", false).unwrap_err();
        assert_eq!(e.msg, "SP.source must be non-empty (I11)");
        assert!(value(true, "X", true, "1", "SP", "1", true).is_ok());
    }

    #[test]
    fn action_amount_refuses_an_unusable_amount_as_stale() {
        let e = action_amount(true, "0", "AAPL", "2026-10-19", "raw").unwrap_err();
        assert_eq!((e.kind, e.msg.as_str()), ("stale", "AAPL dividend going ex 2026-10-19 has no usable amount: raw (I5)"));
        assert!(action_amount(true, "1", "AAPL", "2026-10-19", "raw").is_ok());
    }

    #[test]
    fn mirror_net_drops_a_pair_that_cancels_out() {
        assert!(mirror_net(&[("A", "1"), ("A", "-1")]).unwrap().is_empty());
    }

    #[test]
    fn symbol_key_trims_and_uppercases() {
        assert_eq!(symbol_key(" x "), "X");
    }

    #[test]
    fn settlement_conflict_refuses_only_when_flagged() {
        let e = settlement_conflict(true, "PM", "AAPL", "d").unwrap_err();
        assert_eq!(e.msg, "Two different PM settlements for AAPL on d (I5)");
        assert!(settlement_conflict(false, "PM", "AAPL", "d").is_ok());
    }

    #[test]
    fn missing_inputs_always_refuse_as_stale() {
        assert_eq!(settlement_missing("AM", "SPX", "d").unwrap_err().msg, "No AM settlement for SPX on d (I5)");
        assert_eq!(dividend_missing("AAPL").unwrap_err().msg, "No dividend record for AAPL (I5)");
        let e = quote_missing("OCC", "SPX", "t").unwrap_err();
        assert_eq!((e.kind, e.msg.as_str()), ("stale", "OCC is not in the SPX chain snapshot of t (I5)"));
    }
}

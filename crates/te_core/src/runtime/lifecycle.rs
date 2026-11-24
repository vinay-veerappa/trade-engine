//! After-close decisions. Observations arrive in original host order, never from a clock.
use std::collections::HashMap;

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
        let (kind, reason) = expiry("100", "100.010", "PM", "AAPL", &[2026, 10, 16], true, true).unwrap();
        assert_eq!((kind, reason), ("Exercise".to_string(), "exercised at expiry: 0.01 in the money at the PM settlement 100.010".to_string()));
        assert!(!early("100", "101", "2", "1", "2026-10-19", "AAPL", &[2026, 10, 16]).unwrap().0);
    }
    #[test]
    fn mirror_net_sums_two_rows_of_one_contract() {
        assert_eq!(mirror_net(&[("AAPL 1", "2"), ("AAPL 1", "3")]).unwrap(),
            vec![("AAPL 1".to_string(), "5".to_string(), 0)]);
    }
    #[test]
    fn mirror_net_drops_a_contract_that_nets_to_zero() {
        assert_eq!(mirror_net(&[("A", "2"), ("B", "4"), ("A", "-2"), ("C", "1")]).unwrap(),
            vec![("B".to_string(), "4".to_string(), 1), ("C".to_string(), "1".to_string(), 3)]);
    }
    #[test]
    fn mirror_net_keeps_first_appearance_order_and_indices() {
        assert_eq!(mirror_net(&[("B", "1"), ("A", "2"), ("B", "3")]).unwrap(),
            vec![("B".to_string(), "4".to_string(), 0), ("A".to_string(), "2".to_string(), 1)]);
    }
    #[test]
    fn mirror_net_with_no_rows_is_empty() {
        assert!(mirror_net(&[]).unwrap().is_empty());
    }
    #[test]
    fn symbol_key_trims_and_uppercases() {
        assert_eq!(symbol_key(" aapl\t"), "AAPL");
        assert_eq!(symbol_key("\u{a0}spx\u{3000}"), "SPX");
    }
    #[test]
    fn symbol_key_strips_information_separators_like_python() {
        // Python's str.strip() drops U+001C..U+001F; Rust's trim() does not.
        assert_eq!(symbol_key("\u{1c}spx\u{1f}"), "SPX");
    }
    #[test]
    fn settlement_conflict_passes_when_the_prices_agree() {
        assert!(settlement_conflict(false, "PM", "AAPL", "2026-10-16").is_ok());
    }
    #[test]
    fn settlement_conflict_refuses_as_value() {
        let e = settlement_conflict(true, "PM", "AAPL", "2026-10-16").unwrap_err();
        assert_eq!((e.kind, e.msg.as_str()), ("value", "Two different PM settlements for AAPL on 2026-10-16 (I5)"));
    }
    #[test]
    fn missing_settlement_refuses_as_stale() {
        let e = settlement_missing("AM", "SPX", "2026-10-16").unwrap_err();
        assert_eq!((e.kind, e.msg.as_str()), ("stale", "No AM settlement for SPX on 2026-10-16 (I5)"));
    }
    #[test]
    fn missing_dividend_record_refuses_as_stale() {
        let e = dividend_missing("AAPL").unwrap_err();
        assert_eq!((e.kind, e.msg.as_str()), ("stale", "No dividend record for AAPL (I5)"));
    }
    #[test]
    fn missing_quote_refuses_as_stale() {
        let e = quote_missing("AAPL261016C00250000", "AAPL", "2026-10-15T20:00:00+00:00").unwrap_err();
        assert_eq!((e.kind, e.msg.as_str()), ("stale",
            "AAPL261016C00250000 is not in the AAPL chain snapshot of 2026-10-15T20:00:00+00:00 (I5)"));
    }
}

#[cfg(test)]
mod typed_tests {
    use super::*;

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

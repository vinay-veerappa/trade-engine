use super::Plan;
use crate::ledger::codec::py_repr;
use crate::ledger::model::{derr, err, LErr, R};
use crate::ledger::ops::*;
use crate::money::Money;
use num_bigint::BigUint;

/// Python's `str.strip()` with no argument: Rust's Unicode whitespace plus U+001C..U+001F.
fn py_strip(s: &str) -> &str {
    s.trim_matches(|c: char| c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c))
}

fn dec(s: &str) -> R<Money> {
    Money::parse(s).ok_or_else(|| LErr { kind: "value", msg: "invalid runtime decimal".into() })
}
fn truncate(value: &Money) -> R<String> {
    if !value.is_finite() {
        return value.trunc_i128().map(|n| n.to_string()).map_err(derr);
    }
    let exponent = value.exponent();
    let digits = value.coefficient().to_str_radix(10);
    let coefficient = if exponent < 0 {
        let places = exponent.unsigned_abs();
        if places >= digits.len() as u64 { BigUint::from(0u8) }
        else { value.coefficient() / BigUint::from(10u8).pow(places as u32) }
    } else {
        value.coefficient() * BigUint::from(10u8).pow(u32::try_from(exponent)
            .map_err(|_| LErr { kind: "overflow", msg: "runtime integer exponent out of range".into() })?)
    };
    Ok(if value.is_negative() && coefficient != BigUint::from(0u8) {
        format!("-{coefficient}")
    } else { coefficient.to_string() })
}
pub fn decide(op: &str, s: &[String], n: &[i64], b: &[bool], f: &[f64]) -> R<Plan> {
    let mut p = Plan::default();
    match op {
        "pass_job" => p.text.push(format!("{}-{}",s[0],s[1])),
        "command" => p.text.push(format!("{}:{}:{}:{}",s[0],s[1],s[2],s[3])),
        "config" => {
            if !b[0] { return err("runtime_eod", "job_name must be non-empty"); }
            if !b[1] { return err("runtime_eod", "at least one broker is required"); }
            if !b[2] || f[0] <= 0.0 { return err("runtime_eod", "bars_max_age_seconds must be positive"); }
            if !b[3] || n[0] < 0 { return err("runtime_eod", "settle_delay must be a non-negative timedelta"); }
        }
        "session_type" => if !b[0] { return err("runtime_eod",format!("session must be a date, got {}",s[0])); },
        "session" => if !b[0] { return err("runtime_eod",format!("{} is not a trading session of {} (I5)",s[0],s[1])); },
        "pass_name" => if !["morning","midday","late"].contains(&s[0].as_str()) {
            return err("runtime_eod",format!("Unknown pass {}; the passes are morning, midday, late",s[1]));
        },
        "through" => if !b[0] { return err("runtime_eod",format!("through must be a timezone-aware datetime, got {} (I7)",s[0])); },
        "pass_boundary" => if !(n[0] <= n[1] && n[1] < n[2]) {
            return err("runtime_eod",format!("The {} pass of {} must end inside the session ({} to {}), got {}",s[0],s[1],s[2],s[3],s[4]));
        },
        "resume" => if n[0] <= n[1] {
            return err("runtime_eod",format!("The {} pass of {} must end after the previous pass of '{}', which ran to {}; got {} (I7)",s[0],s[1],s[2],s[3],s[4]));
        },
        "cutoff" => p.flags.push(n[0] <= n[1]),
        "latest" => if let Some((i,_)) = n.iter().enumerate().max_by(|a,b| a.1.cmp(b.1).then_with(|| b.0.cmp(&a.0))) { p.numbers.push(i as i64); },
        "earliest" => if let Some((i,_)) = n.iter().enumerate().min_by_key(|(_,t)| *t) { p.numbers.push(i as i64); },
        "count_events" => p.numbers.push(s[2..].chunks_exact(2).filter(|r| r[0] == s[0] && r[1].starts_with(&s[1])).count() as i64),
        "children" => {
            let mut stop = None;
            let mut targets = Vec::new();
            for (i,row) in s[1..].chunks_exact(3).enumerate() {
                p.flags.push(row[0] == s[0]);
                if row[0] == s[0] {
                    if row[1] == "STOP" && stop.is_none() { stop = Some(i as i64); }
                    if row[1] == "LIMIT" { targets.push(i as i64); }
                }
            }
            p.numbers.push(stop.unwrap_or(-1));
            p.numbers.extend(targets);
        }
        "terminal" => p.flags.push(["FILLED","CANCELLED","EXPIRED","REJECTED"].contains(&s[0].as_str())),
        "filled" => p.numbers.push(s.iter().filter(|value| *value == "FILLED").count() as i64),
        "exit_action" => {
            if !b[0] { return err("runtime_eod",format!("Exit action '{}' names '{}', which is not an open bracket of '{}' (I8)",s[0],s[1],s[2])); }
            if !b[1] { return err("runtime_eod",format!("Strategy for '{}' returned {}; exit actions are MoveStop, ClosePosition or ReducePosition",s[2],s[3])); }
        }
        // s: marker job, this job, marker session, run session (ISO dates order as text): only an earlier session is history.
        "history" => p.flags.push(b[0] && s[2] < s[3] && (s[0] == s[1] || ["morning","midday","late"].iter().any(|v| s[0] == format!("{}-{v}",s[1])))),
        // b: has history, previous session ran, previous session voided by the owner (EOD-VOID-1: a void counts as complete).
        "previous" => if b[0] && !b[1] && !b.get(2).copied().unwrap_or(false) {
            return err("runtime_incomplete",format!("Cannot run {} for '{}': the previous session {} has no {} marker; complete it first (I3)",s[0],s[1],s[2],s[3]));
        },
        "replay_clock" => if n[0] > n[1] {
            return err("runtime_eod",format!("Cannot replay {}: the injected clock reads {}, past the session open {}. Replay needs a clock it can advance bar by bar (I7); inject a replay clock positioned at or before the session open",s[0],s[1],s[2]));
        },
        "nonzero" => p.flags.push(!eq(&dec(&s[0])?,&zero())?),
        "positive" => p.flags.push(gt(&dec(&s[0])?,&zero())?),
        "working" => p.flags.push(["SUBMITTED","ACCEPTED","PARTIALLY_FILLED","PENDING_UNKNOWN"].contains(&s[0].as_str())),
        "after" => p.flags.push(n[0] > n[1]),
        "any_history" => p.flags.push(s.iter().any(|kind| kind == "EodRun")),
        "difference" => p.numbers.push(n[0] - n[1]),
        "replay_instrument" => if !b[0] {
            return err("runtime_eod",format!("Equity EOD replay cannot value {}; option marks and the lifecycle pass are wired into the EOD run by O4 (I5)",s[0]));
        },
        "rehydrate" => {
            if !b[0] { return err("runtime_eod",format!("Venue for '{}' does not hold working order '{}' ({}); refusing to replay a session it could never fill in (I5)",s[0],s[1],s[2])); }
            if lt(&dec(&s[3])?,&dec(&s[4])?)? { return err("runtime_eod",format!("Venue reports {} filled for '{}' but the ledger records {} (I5)",s[3],s[1],s[4])); }
        }
        "bars" => {
            if !b[0] { return err("runtime_replay",format!("No one-minute bars returned for {} for this session; refusing to simulate from memory (I5)",s[0])); }
            if n[0] != n[1] { return err("runtime_replay",format!("Bar series for {} starts at {}, not the session open {} (I5)",s[0],s[1],s[2])); }
            if n[2] != n[3] - 60_000_000 { return err("runtime_replay",format!("Bar series for {} ends at {}, not the session's final minute {} (I5)",s[0],s[3],s[4])); }
        }
        "regular" => p.flags.push(n[0] <= n[1] && n[1] < n[2]),
        "mark" => {
            if eq(&dec(&s[0])?,&zero())? || b[0] { p.flags.push(false); }
            else if !b[1] { return err("runtime_replay",format!("No regular-session bar for open position {} in '{}'; refusing to mark without one (I5)",s[1],s[2])); }
            else { p.flags.push(true); }
        }
        "risk" => {
            let mut marked = zero();
            let mut gross = zero();
            let mut count = 0;
            for (i,row) in s[2..].chunks_exact(2).enumerate() {
                let q = dec(&row[0])?;
                if b[i] { marked = add(&marked,&mul(&q,&dec(&row[1])?)?)?; }
                gross = add(&gross,&mul(&q.abs().map_err(derr)?,&dec(&row[1])?)?)?;
                if !eq(&q,&zero())? { count += 1; }
            }
            let equity = add(&dec(&s[0])?,&marked)?;
            p.flags.push(gt(&equity,&zero())?);
            p.text = vec![crate::ledger::ops::s(&equity),crate::ledger::ops::s(&gross), truncate(&dec(&s[1])?)?];
            p.numbers.push(count);
        }
        "bracket" => {
            let filled = s[..n[0] as usize].iter().try_fold(zero(),|a,v| add(&a,&dec(v)?))?;
            let exited = s[n[0] as usize..n[1] as usize].iter().try_fold(zero(),|a,v| add(&a,&dec(v)?))?;
            let open = sub(&filled,&exited)?;
            p.flags.push(gt(&open,&zero())?);
            if p.flags[0] {
                let amount = s[n[1] as usize..].chunks_exact(2).try_fold(zero(),|a,row| add(&a,&mul(&dec(&row[0])?,&dec(&row[1])?)?))?;
                p.text = vec![crate::ledger::ops::s(&filled),crate::ledger::ops::s(&open),crate::ledger::ops::s(&div(&amount,&filled)?)];
            }
        }
        "subtract" => p.text.push(crate::ledger::ops::s(&sub(&dec(&s[0])?,&dec(&s[1])?)?)),
        "intent" => if s[0] != s[1] { return err("runtime_eod",format!("Intent '{}' targets account '{}' but was produced for '{}' (I8)",s[2],s[0],s[1])); },
        "equity_approved" => p.flags.push(b[0] && b[1]),
        "snapshots_wiring" => if !b[0] || !b[1] { return err("runtime_eod",format!("Options accounts {} need chain_snapshots to fill and mark options and settlements to mark shares; neither may be defaulted (I5)",s[0])); },
        "snapshot" => {
            if !b[0] { return err("runtime_eod",format!("chain_snapshots returned {} (I5)",s[0])); }
            if !(n[0] <= n[1] && n[1] <= n[2]) { return err("runtime_replay",format!("{} snapshot of {} is outside the {} session (I7)",s[1],s[2],s[3])); }
        }
        "coverage" => if !s[0].is_empty() { return err("runtime_replay",format!("No {} chain snapshot for {}, which '{}' holds options on or has orders working on; refusing to fill or mark them from memory (I5)",s[1],s[0],s[2])); },
        "lifecycle" => if b[0] && !b[1] { return err("runtime_eod",format!("{} hold options and no lifecycle pass is configured; expiry and assignment cannot be decided (I9)",s[0])); },
        "preopen" => p.flags.push(n[0] < n[1]),
        "dividend_source" => if !b[0] { return err("runtime_eod",format!("'{}' holds shares and no dividend source is configured; a dividend going ex would be missed (I5)",s[0])); },
        "dividend_stamp" => if n[0] > n[1] { return err("runtime_replay",format!("{} dividend record is stamped {}, after the clock: look-ahead (I7)",s[0],s[1])); },
        "dividend" => {
            let per_share = s[1..].iter().try_fold(zero(),|a,v| add(&a,&dec(v)?))?;
            p.flags.push(!eq(&per_share,&zero())?);
            if p.flags[0] { p.text = vec![crate::ledger::ops::s(&per_share),crate::ledger::ops::s(&mul(&dec(&s[0])?,&per_share)?)]; }
        }
        "option_mark" => if !b[0] || !gt(&dec(&s[0])?,&zero())? {
            return err("runtime_replay",format!("No usable {} quote for {} in '{}'; refusing to mark it (I5)",s[1],s[2],s[3]));
        },
        "unmarkable" => return err("runtime_eod",format!("'{}' holds {}, which cannot be marked (I6)",s[0],s[1])),
        "official_identity" => if s[0] != s[1] || s[2] != s[3] || s[4] != "PM" {
            return err("runtime_replay",format!("Asked for the {} close of {}, got {} {} on {} (I5)",s[2],s[0],s[1],s[4],s[3]));
        },
        "official_future" => if n[0] > n[1] { return err("runtime_replay",format!("{} close is stamped {}, after the clock {}: look-ahead (I7)",s[0],s[1],s[2])); },
        "official_close" => if n[0] < n[1] { return err("runtime_replay",format!("{} close is stamped {}, before the session closed; it cannot be the official close (I9)",s[0],s[1])); },
        "sink" => if !b[0] { return err("runtime_eod",format!("Sink for '{}' does not declare a publish method (I5)",s[0])); },
        // s: reason, owner, session repr, session text, then one account per entry.
        // b: is_date, reason is str, owner is str, is_session, then (is_str, known, ran) per account.
        "void_check" => {
            if s.len() < 4 || b.len() != 4 + 3 * (s.len() - 4) { return err("value", "void_check arity"); }
            let accounts = &s[4..];
            let flags = &b[4..];
            let k = accounts.len();
            if !b[0] { return err("runtime_eod", format!("void session must be a date, got {}", s[0])); }
            if !b[1] || py_strip(&s[2]).is_empty() { return err("runtime_eod", "a void needs a non-empty reason (I5)"); }
            if !b[2] || py_strip(&s[3]).is_empty() { return err("runtime_eod", "a void needs a non-empty owner (I5)"); }
            if !b[3] { return err("runtime_eod", format!("{} is not a trading session; nothing to void (I5)", s[1])); }
            if k == 0 || (0..k).any(|i| !flags[3 * i] || py_strip(&accounts[i]).is_empty()) {
                return err("runtime_eod", "a void needs at least one non-empty account (I5)");
            }
            let unknown: Vec<String> = (0..k).filter(|&i| !flags[3 * i + 1]).map(|i| py_repr(&accounts[i])).collect();
            if !unknown.is_empty() {
                return err("runtime_eod", format!("unknown account(s) [{}]: not in the ledger (I5)", unknown.join(", ")));
            }
            if let Some(i) = (0..k).find(|&i| flags[3 * i + 2]) {
                return err("runtime_eod", format!("{} already ran {}: a session that ran cannot be voided (I3)", accounts[i], s[1]));
            }
            p.text = vec![py_strip(&s[2]).to_string(), py_strip(&s[3]).to_string()];
        }
        // s: reason, owner, account, order, order state, order filled, parent id, parent filled,
        // order venue id, then (child id, child state, child venue id) per child of the order.
        // b: reason is str, owner is str, known, already, exists, venue_cancel_confirmed.
        "writeoff_check" => {
            if s.len() < 9 || (s.len() - 9) % 3 != 0 || b.len() != 6 { return err("value", "writeoff_check arity"); }
            let (account, order) = (s[2].as_str(), s[3].as_str());
            if !b[0] || py_strip(&s[0]).is_empty() { return err("runtime_eod", "a write-off needs a non-empty reason (I5)"); }
            if !b[1] || py_strip(&s[1]).is_empty() { return err("runtime_eod", "a write-off needs a non-empty owner (I5)"); }
            if !b[2] { return err("runtime_eod", format!("unknown account {}: not in the ledger (I5)", py_repr(account))); }
            if b[3] {
                p.flags.push(false);
                return Ok(p);
            }
            if !b[4] { return err("runtime_eod", format!("{account} has no order {} (I5)", py_repr(order))); }
            if !eq(&dec(&s[5])?, &zero())? {
                return err("runtime_eod", format!("order {} has fills: a filled or partly filled order is a position, not a write-off (I5)", py_repr(order)));
            }
            if !s[6].is_empty() && !eq(&dec(&s[7])?, &zero())? {
                return err("runtime_eod", format!("order {} protects {}, which has fills: writing it off would leave a live position unprotected (I5)", py_repr(order), py_repr(&s[6])));
            }
            if !matches!(s[4].as_str(), "SUBMITTED" | "ACCEPTED" | "PENDING_UNKNOWN") {
                return err("runtime_eod", format!("order {} is {}, not working; nothing to write off (I5)", py_repr(order), s[4]));
            }
            let children = 0..(s.len() - 9) / 3;
            let kept: Vec<usize> = children
                .filter(|&i| !matches!(s[10 + 3 * i].as_str(), "FILLED" | "CANCELLED" | "EXPIRED" | "REJECTED"))
                .collect();
            if !b[5] {
                let targets = std::iter::once((order, s[8].as_str()))
                    .chain(kept.iter().map(|&i| (s[9 + 3 * i].as_str(), s[11 + 3 * i].as_str())));
                for (id, venue) in targets {
                    if !venue.is_empty() {
                        return err("runtime_eod", format!("order {} has venue order id {}: the venue may still hold it. Cancel it at the venue first, then pass --venue-cancel-confirmed (I5)", py_repr(id), py_repr(venue)));
                    }
                }
            }
            p.flags.push(true);
            let mut text = vec![format!("Owner write-off: {} (owner: {})", py_strip(&s[0]), py_strip(&s[1])), order.to_string()];
            text.extend(kept.iter().map(|&i| s[9 + 3 * i].clone()));
            p.text = text;
        }
        // s: snapshot as_of, quote as_of. n: micros(snapshot.as_of - quote.as_of). f: the age limit in seconds.
        "quote_source" => {
            let age = n[0] as f64 / 1_000_000.0;
            let mut source = format!("snapshot:{}", s[0]);
            if age > f[0] { source = format!("stale-quote:{} {}", s[1], source); }
            p.text.push(source);
        }
        _ => return err("value",format!("Unknown EOD decision {op}")),
    }
    Ok(p)
}

#[cfg(test)]
mod tests {
    use super::*;
    use proptest::prelude::*;
    #[test]
    fn previous_rule_treats_a_void_as_complete() {
        let s: Vec<String> = ["eod", "ACC", "2026-10-16", "eod"].iter().map(|v| v.to_string()).collect();
        // history, previous never ran, not voided: refused (I3).
        assert!(decide("previous", &s, &[], &[true, false, false], &[]).is_err());
        // the legacy two-flag shape still refuses.
        assert!(decide("previous", &s, &[], &[true, false], &[]).is_err());
        // history, previous never ran, but the owner voided it: complete.
        assert!(decide("previous", &s, &[], &[true, false, true], &[]).is_ok());
        // previous ran: complete, voided or not.
        assert!(decide("previous", &s, &[], &[true, true, false], &[]).is_ok());
        assert!(decide("previous", &s, &[], &[true, true, true], &[]).is_ok());
        // no history: nothing to demand.
        assert!(decide("previous", &s, &[], &[false, false, false], &[]).is_ok());
    }
    #[test]
    fn boundary_and_refusal() {
        assert!(decide("cutoff",&[],&[5,5],&[],&[]).unwrap().flags[0]);
        assert!(decide("preopen",&[],&[4,5],&[],&[]).unwrap().flags[0]);
        assert!(!decide("preopen",&[],&[5,5],&[],&[]).unwrap().flags[0]);
        assert!(decide("config",&[],&[0],&[false,true,true,true],&[1.0]).is_err());
    }
    fn strs(v: &[&str]) -> Vec<String> { v.iter().map(|x| x.to_string()).collect() }
    /// (kind, message) of a refusal; panics on success.
    fn refusal(r: R<Plan>) -> (&'static str, String) {
        match r { Err(e) => (e.kind, e.msg), Ok(_) => panic!("expected a refusal") }
    }
    const DATE_REPR: &str = "datetime.date(2026, 9, 23)";
    const SESSION: &str = "2026-09-23";
    /// void_check with the session a trading date unless `is_session` says otherwise.
    fn void_check(is_session: bool, reason: &str, owner: &str, accounts: &[(&str, bool, bool)]) -> R<Plan> {
        let mut s = strs(&[DATE_REPR, SESSION, reason, owner]);
        let mut b = vec![true, true, true, is_session];
        for (account, known, ran) in accounts {
            s.push(account.to_string());
            b.extend([true, *known, *ran]);
        }
        decide("void_check", &s, &[], &b, &[])
    }
    /// writeoff_check on an ACCEPTED order `o1` of `acct` with no fills, no parent, no venue id.
    /// `children` are (id, state, venue) triples in insertion order.
    fn writeoff_check(children: &[(&str, &str, &str)]) -> Vec<String> {
        let mut s = strs(&["reason", "owner", "acct", "o1", "ACCEPTED", "0", "", "0", ""]);
        for (id, state, venue) in children {
            s.extend(strs(&[*id, *state, *venue]));
        }
        let b = [true, true, true, false, true, false];
        decide("writeoff_check", &s, &[], &b, &[]).map(|p| p.text).unwrap_or_else(|e| panic!("{}", e.msg))
    }
    #[test]
    fn void_check_refuses_each_rule_with_its_message() {
        let (kind, msg) = refusal(decide("void_check", &strs(&[DATE_REPR, SESSION, "r", "o"]), &[], &[false, true, true, true], &[]));
        assert_eq!((kind, msg.as_str()), ("runtime_eod", "void session must be a date, got datetime.date(2026, 9, 23)"));
        assert_eq!(refusal(void_check(true, " ", "o", &[("A", true, false)])).1, "a void needs a non-empty reason (I5)");
        assert_eq!(refusal(void_check(true, "r", "\t", &[("A", true, false)])).1, "a void needs a non-empty owner (I5)");
        assert_eq!(refusal(void_check(false, "r", "o", &[("A", true, false)])).1, "2026-09-23 is not a trading session; nothing to void (I5)");
        assert_eq!(refusal(void_check(true, "r", "o", &[])).1, "a void needs at least one non-empty account (I5)");
        assert_eq!(refusal(void_check(true, "r", "o", &[("  ", true, false)])).1, "a void needs at least one non-empty account (I5)");
        assert_eq!(refusal(void_check(true, "r", "o", &[("A", true, false), ("B", true, true)])).1,
            "B already ran 2026-09-23: a session that ran cannot be voided (I3)");
    }
    #[test]
    fn void_check_lists_unknown_accounts_in_python_repr() {
        let (kind, msg) = refusal(void_check(true, "r", "o", &[("X", false, false), ("a'b", false, false)]));
        assert_eq!(kind, "runtime_eod");
        assert_eq!(msg, "unknown account(s) ['X', \"a'b\"]: not in the ledger (I5)");
    }
    #[test]
    fn void_check_success_returns_stripped_reason_and_owner() {
        let p = void_check(true, "  why\t", "\u{1c}me\u{1f}", &[("A", true, false), ("B", true, false)]).unwrap_or_else(|e| panic!("{}", e.msg));
        assert_eq!(p.text, vec!["why".to_string(), "me".to_string()]);
    }
    #[test]
    fn void_check_counts_information_separators_as_blank() {
        assert_eq!(refusal(void_check(true, "\u{1c}", "o", &[("A", true, false)])).1, "a void needs a non-empty reason (I5)");
        assert_eq!(refusal(void_check(true, "r", "\u{1d}\u{1e}\u{1f}", &[("A", true, false)])).1, "a void needs a non-empty owner (I5)");
    }
    #[test]
    fn void_check_arity_is_refused() {
        assert_eq!(refusal(decide("void_check", &strs(&[DATE_REPR, SESSION, "r"]), &[], &[true, true, true], &[])).1, "void_check arity");
        // one account needs three flags, not two
        assert_eq!(refusal(decide("void_check", &strs(&[DATE_REPR, SESSION, "r", "o", "A"]), &[], &[true, true, true, true, true, true], &[])).1, "void_check arity");
    }
    #[test]
    fn writeoff_check_refuses_each_rule_with_its_message() {
        let base = strs(&["reason", "owner", "acct", "o1", "ACCEPTED", "0", "", "0", ""]);
        let run = |s: &[&str], b: &[bool]| refusal(decide("writeoff_check", &strs(s), &[], b, &[]));
        let ok = [true, true, true, false, true, false];
        assert_eq!(run(&["", "owner", "acct", "o1", "ACCEPTED", "0", "", "0", ""], &ok).1, "a write-off needs a non-empty reason (I5)");
        assert_eq!(run(&["reason", "\u{1c}", "acct", "o1", "ACCEPTED", "0", "", "0", ""], &ok).1, "a write-off needs a non-empty owner (I5)");
        assert_eq!(decide("writeoff_check", &base, &[], &[true, true, false, false, true, false], &[]).err().map(|e| e.msg).as_deref(),
            Some("unknown account 'acct': not in the ledger (I5)"));
        assert_eq!(decide("writeoff_check", &strs(&["reason", "owner", "a'b", "o1", "", "0", "", "0", ""]), &[], &[true, true, false, false, true, false], &[])
            .err().map(|e| e.msg).as_deref(), Some("unknown account \"a'b\": not in the ledger (I5)"));
        assert_eq!(run(&["reason", "owner", "acct", "o1", "", "0", "", "0", ""], &[true, true, true, false, false, false]).1,
            "acct has no order 'o1' (I5)");
        assert_eq!(run(&["reason", "owner", "acct", "o1", "ACCEPTED", "1", "", "0", ""], &ok).1,
            "order 'o1' has fills: a filled or partly filled order is a position, not a write-off (I5)");
        assert_eq!(run(&["reason", "owner", "acct", "o1", "ACCEPTED", "0", "p1", "2", ""], &ok).1,
            "order 'o1' protects 'p1', which has fills: writing it off would leave a live position unprotected (I5)");
        assert_eq!(run(&["reason", "owner", "acct", "o1", "FILLED", "0", "", "0", ""], &ok).1,
            "order 'o1' is FILLED, not working; nothing to write off (I5)");
        assert_eq!(run(&["reason", "owner", "acct", "o1", "EXPIRED", "0", "", "0", ""], &ok).1,
            "order 'o1' is EXPIRED, not working; nothing to write off (I5)");
        assert_eq!(run(&["reason", "owner", "acct", "o1", "ACCEPTED", "0", "", "0", "v1"], &[true, true, true, false, true, false]).1,
            "order 'o1' has venue order id 'v1': the venue may still hold it. Cancel it at the venue first, then pass --venue-cancel-confirmed (I5)");
    }
    #[test]
    fn writeoff_check_already_is_an_idempotent_repeat_even_with_blank_order_fields() {
        let s = strs(&["reason", "owner", "acct", "o1", "", "0", "", "0", ""]);
        let p = decide("writeoff_check", &s, &[], &[true, true, true, true, false, false], &[]).unwrap_or_else(|e| panic!("{}", e.msg));
        assert_eq!(p.flags, vec![false]);
        assert!(p.text.is_empty());
    }
    #[test]
    fn writeoff_check_partially_filled_with_no_fills_is_not_working() {
        let s = strs(&["reason", "owner", "acct", "o1", "PARTIALLY_FILLED", "0", "", "0", ""]);
        let (kind, msg) = refusal(decide("writeoff_check", &s, &[], &[true, true, true, false, true, false], &[]));
        assert_eq!(kind, "runtime_eod");
        assert_eq!(msg, "order 'o1' is PARTIALLY_FILLED, not working; nothing to write off (I5)");
    }
    #[test]
    fn writeoff_check_keeps_non_terminal_children_in_insertion_order() {
        let text = writeoff_check(&[("c1", "ACCEPTED", ""), ("c2", "FILLED", ""), ("c3", "PARTIALLY_FILLED", ""),
            ("c4", "CANCELLED", ""), ("c5", "SUBMITTED", "")]);
        assert_eq!(text, strs(&["Owner write-off: reason (owner: owner)", "o1", "c1", "c3", "c5"]));
    }
    #[test]
    fn writeoff_check_refuses_a_venue_id_on_a_live_child_but_not_a_terminal_one() {
        let (kind, msg) = refusal(decide("writeoff_check",
            &strs(&["reason", "owner", "acct", "o1", "ACCEPTED", "0", "", "0", "", "c1", "ACCEPTED", "v9"]),
            &[], &[true, true, true, false, true, false], &[]));
        assert_eq!(kind, "runtime_eod");
        assert_eq!(msg, "order 'c1' has venue order id 'v9': the venue may still hold it. Cancel it at the venue first, then pass --venue-cancel-confirmed (I5)");
        let text = writeoff_check(&[("c2", "FILLED", "v8")]);
        assert_eq!(text, strs(&["Owner write-off: reason (owner: owner)", "o1"]));
    }
    #[test]
    fn writeoff_check_venue_cancel_confirmed_skips_the_venue_checks() {
        let s = strs(&["reason", "owner", "acct", "o1", "ACCEPTED", "0", "", "0", "v1", "c1", "ACCEPTED", "v9"]);
        let p = decide("writeoff_check", &s, &[], &[true, true, true, false, true, true], &[]).unwrap_or_else(|e| panic!("{}", e.msg));
        assert_eq!(p.flags, vec![true]);
        assert_eq!(p.text, strs(&["Owner write-off: reason (owner: owner)", "o1", "c1"]));
    }
    #[test]
    fn writeoff_check_an_empty_parent_skips_the_parent_check() {
        let s = strs(&["reason", "owner", "acct", "o1", "ACCEPTED", "0", "", "5", ""]);
        let p = decide("writeoff_check", &s, &[], &[true, true, true, false, true, false], &[]).unwrap_or_else(|e| panic!("{}", e.msg));
        assert_eq!(p.flags, vec![true]);
    }
    #[test]
    fn writeoff_check_arity_is_refused() {
        let b = [true, true, true, false, true, false];
        assert_eq!(refusal(decide("writeoff_check", &strs(&["r", "o", "a", "o1", "ACCEPTED", "0", "", "0"]), &[], &b, &[])).1, "writeoff_check arity");
        // a child needs three fields: this one has two
        assert_eq!(refusal(decide("writeoff_check", &strs(&["r", "o", "a", "o1", "ACCEPTED", "0", "", "0", "", "c1", "ACCEPTED"]), &[], &b, &[])).1, "writeoff_check arity");
        assert_eq!(refusal(decide("writeoff_check", &strs(&["r", "o", "a", "o1", "ACCEPTED", "0", "", "0", ""]), &[], &b[..5], &[])).1, "writeoff_check arity");
    }
    #[test]
    fn quote_source_is_stale_only_strictly_over_the_limit() {
        let run = |micros: i64| decide("quote_source", &strs(&["Q1", "Q0"]), &[micros], &[], &[900.0]).unwrap_or_else(|e| panic!("{}", e.msg)).text;
        // age equal to the limit is not stale
        assert_eq!(run(900_000_000), strs(&["snapshot:Q1"]));
        // one microsecond over the limit is stale
        assert_eq!(run(900_000_001), strs(&["stale-quote:Q0 snapshot:Q1"]));
        // a negative age is not stale
        assert_eq!(run(-5_000_000), strs(&["snapshot:Q1"]));
    }
    proptest! {
        #[test]
        fn dividend_product(q in -10000i64..10000, a in 1i64..10000) {
            let s = vec![q.to_string(),a.to_string()];
            let p = decide("dividend",&s,&[],&[],&[]).unwrap();
            prop_assert_eq!(&p.text[1], &(q*a).to_string());
        }
    }
}

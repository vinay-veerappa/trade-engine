use super::{need, Plan};
use crate::ledger::model::{err, R};
use crate::ledger::model::{derr, LErr};
use crate::money::Money;
use num_bigint::BigInt;

// Typed routing rules. Each one returns what its string arm used to push, or the refusal it used to raise.
// The string door (`decide` below) only checks arity and forwards the inputs.

pub fn phase(named: &str, snapshot: bool) -> String {
    if !named.is_empty() { named.to_string() } else if snapshot { "snapshot".into() } else { "close".into() }
}

pub fn taken(eligible: bool, direct: bool, global: bool, entry: bool) -> bool {
    eligible && (direct || (global && entry))
}

pub fn risk_lookup(known: bool, configured: bool) -> bool { known && configured }

pub fn tally(s: &[String]) -> R<Vec<String>> {
    let values = s.iter().map(|v| v.parse::<BigInt>().map_err(|_| LErr {
        kind: "value", msg: "invalid runtime integer".into()
    })).collect::<R<Vec<_>>>()?;
    Ok((0..3).map(|i| (&values[i] + &values[i + 3]).to_string()).collect())
}

pub fn underlying(kind: &str, owner: &str, close: &str, named: &str, is_order: bool, value: &str, label: &str) -> R<String> {
    match kind {
        "intent" | "holding" => Ok(value.to_string()),
        "close" => {
            if !is_order {
                return err("runtime_eod", format!("Close '{close}' names '{named}', which is not an order of '{owner}' (I8)"));
            }
            Ok(value.to_string())
        }
        _ => err("runtime_eod", format!("Unknown options action {label}")),
    }
}

pub fn action(account: &str, kind: &str, name: &str, action_underlying: &str, snapshot: &str, scoped: bool, is_intent: bool) -> R<()> {
    if scoped && action_underlying != snapshot {
        return err("runtime_eod", format!("'{account}' returned {kind} '{name}' at the {snapshot} snapshot for another underlying"));
    }
    if !is_intent {
        return err("runtime_eod", format!("Options account '{account}' was handed {kind}; it enters with OptionIntent"));
    }
    Ok(())
}

pub fn entry(account: &str, kind: &str, intent_account: &str, intent_id: &str, is_option_intent: bool) -> R<()> {
    if !is_option_intent {
        return err("runtime_eod", format!("Options account '{account}' was handed {kind}; it enters with OptionIntent"));
    }
    if intent_account != account {
        return err("runtime_eod", format!("Intent '{intent_id}' targets account '{intent_account}' but was produced for '{account}' (I8)"));
    }
    Ok(())
}

pub fn engine(account: &str, name: &str, has_engine: bool) -> R<()> {
    if !has_engine {
        return err("runtime_eod", format!("'{account}' asked to enter '{name}' and has no options risk engine; nothing enters unchecked (I5)"));
    }
    Ok(())
}

/// Returns `(accepted, resize)`. `resize` is set only when the verdict is accepted and both amounts parse and differ.
pub fn approved(accepted: bool, has_quantity: bool, left: &str, right: &str) -> R<(bool, bool)> {
    let resize = if accepted && has_quantity {
        let left = Money::parse(left).ok_or_else(|| LErr { kind: "value", msg: "invalid runtime decimal".into() })?;
        let right = Money::parse(right).ok_or_else(|| LErr { kind: "value", msg: "invalid runtime decimal".into() })?;
        !left.eq_num(&right).map_err(derr)?
    } else { false };
    Ok((accepted, resize))
}

pub fn increment(current: i64, submitted: i64) -> i64 { current + submitted }

pub fn rounds(strategy: &str, snapshot: &str, rounds: i64) -> R<()> {
    err("runtime_eod", format!("The strategy for '{strategy}' was still acting at the {snapshot} snapshot after {rounds} rounds; refusing to loop on it"))
}

pub fn decide(op: &str, s: &[String], n: &[i64], b: &[bool]) -> R<Plan> {
    let mut p = Plan::default();
    match op {
        "phase" => {
            need(op, s, n, b, &[], (1, 0, 1, 0))?;
            p.text.push(phase(&s[0], b[0]));
        }
        "taken" => {
            need(op, s, n, b, &[], (0, 0, 4, 0))?;
            p.flags.push(taken(b[0], b[1], b[2], b[3]));
        }
        "risk_lookup" => {
            need(op, s, n, b, &[], (0, 0, 2, 0))?;
            p.flags.push(risk_lookup(b[0], b[1]));
        }
        "tally" => {
            need(op, s, n, b, &[], (6, 0, 0, 0))?;
            p.text = tally(s)?;
        }
        "underlying" => {
            need(op, s, n, b, &[], (6, 0, 1, 0))?;
            p.text.push(underlying(&s[0], &s[1], &s[2], &s[3], b[0], &s[4], &s[5])?);
        }
        "action" => {
            need(op, s, n, b, &[], (5, 0, 2, 0))?;
            action(&s[0], &s[1], &s[2], &s[3], &s[4], b[0], b[1])?;
        }
        "entry" => {
            need(op, s, n, b, &[], (4, 0, 1, 0))?;
            entry(&s[0], &s[1], &s[2], &s[3], b[0])?;
        }
        "engine" => {
            need(op, s, n, b, &[], (2, 0, 1, 0))?;
            engine(&s[0], &s[1], b[0])?;
        }
        "approved" => {
            need(op, s, n, b, &[], (2, 0, 2, 0))?;
            let (accepted, resize) = approved(b[0], b[1], &s[0], &s[1])?;
            p.flags = vec![accepted, resize];
        }
        "increment" => {
            need(op, s, n, b, &[], (0, 2, 0, 0))?;
            p.numbers.push(increment(n[0], n[1]));
        }
        "rounds" => {
            need(op, s, n, b, &[], (2, 1, 0, 0))?;
            rounds(&s[0], &s[1], n[0])?;
        }
        _ => return err("value", format!("Unknown routing decision {op}")),
    }
    Ok(p)
}

#[cfg(test)]
mod tests {
    use super::*;
    use proptest::prelude::*;
    proptest! {
        #[test]
        fn tally_addition(a in 0i64..100000, b in 0i64..100000) {
            let values = [a,a,a,b,b,b].map(|v| v.to_string());
            let p = decide("tally", &values, &[], &[]).unwrap();
            prop_assert_eq!(p.text, vec![(a+b).to_string();3]);
        }
    }
    #[test]
    fn global_taken_and_entry_risk() {
        assert!(decide("taken", &[], &[], &[true,false,true,true]).unwrap().flags[0]);
        assert!(!decide("taken", &[], &[], &[true,false,false,true]).unwrap().flags[0]);
    }
}

#[cfg(test)]
mod typed_tests {
    use super::*;

    const OPS: [&str; 11] = ["phase", "taken", "risk_lookup", "tally", "underlying", "action",
        "entry", "engine", "approved", "increment", "rounds"];

    fn refusal<T>(r: R<T>) -> (&'static str, String) {
        match r {
            Ok(_) => panic!("expected a refusal"),
            Err(e) => (e.kind, e.msg),
        }
    }

    #[test]
    fn every_op_refuses_short_input_with_arity_and_never_panics() {
        for op in OPS {
            let (kind, msg) = refusal(decide(op, &[], &[], &[]));
            assert_eq!((kind, msg.as_str()), ("value", format!("{op} arity").as_str()), "op {op}");
        }
        for ls in 0..=7 {
            for ln in 0..=3 {
                for lb in 0..=5 {
                    for fill in ["1", "x", ""] {
                        let s = vec![fill.to_string(); ls];
                        let n = vec![1i64; ln];
                        let b = vec![true; lb];
                        for op in OPS.into_iter().chain(["bogus"]) {
                            let _ = decide(op, &s, &n, &b);
                        }
                    }
                }
            }
        }
    }

    #[test]
    fn unknown_op_is_refused_by_name() {
        let (kind, msg) = refusal(decide("bogus", &[], &[], &[]));
        assert_eq!((kind, msg.as_str()), ("value", "Unknown routing decision bogus"));
    }

    #[test]
    fn phase_prefers_named_then_snapshot_then_close() {
        assert_eq!(phase("named", false), "named");
        assert_eq!(phase("", true), "snapshot");
        assert_eq!(phase("", false), "close");
    }

    #[test]
    fn taken_truth_table() {
        assert!(taken(true, true, false, false));
        assert!(taken(true, false, true, true));
        assert!(!taken(true, false, true, false));
        assert!(!taken(true, false, false, true));
        assert!(!taken(false, true, true, true));
    }

    #[test]
    fn risk_lookup_needs_both() {
        assert!(risk_lookup(true, true));
        assert!(!risk_lookup(true, false));
        assert!(!risk_lookup(false, true));
    }

    #[test]
    fn tally_adds_pairs_and_refuses_bad_integers() {
        let v: Vec<String> = ["1", "2", "3", "4", "5", "6"].iter().map(|x| x.to_string()).collect();
        assert_eq!(tally(&v).unwrap(), vec!["5", "7", "9"]);
        let bad: Vec<String> = ["1", "2", "3", "4", "5", "x"].iter().map(|x| x.to_string()).collect();
        assert_eq!(refusal(tally(&bad)), ("value", "invalid runtime integer".to_string()));
    }

    #[test]
    fn underlying_intent_and_close_and_unknown() {
        assert_eq!(underlying("intent", "A", "C", "N", false, "V", "L").unwrap(), "V");
        assert_eq!(underlying("close", "A", "C", "N", true, "V", "L").unwrap(), "V");
        assert_eq!(refusal(underlying("close", "A", "C", "N", false, "V", "L")),
            ("runtime_eod", "Close 'C' names 'N', which is not an order of 'A' (I8)".to_string()));
        assert_eq!(refusal(underlying("zap", "A", "C", "N", true, "V", "L")),
            ("runtime_eod", "Unknown options action L".to_string()));
    }

    #[test]
    fn action_checks_underlying_then_intent() {
        assert!(action("A", "K", "N", "U", "U", true, true).is_ok());
        assert_eq!(refusal(action("A", "K", "N", "U", "V", true, true)),
            ("runtime_eod", "'A' returned K 'N' at the V snapshot for another underlying".to_string()));
        assert!(action("A", "K", "N", "U", "V", false, true).is_ok());
        assert_eq!(refusal(action("A", "K", "N", "U", "U", false, false)),
            ("runtime_eod", "Options account 'A' was handed K; it enters with OptionIntent".to_string()));
    }

    #[test]
    fn entry_checks_intent_kind_then_account() {
        assert!(entry("A", "K", "A", "I", true).is_ok());
        assert_eq!(refusal(entry("A", "K", "A", "I", false)),
            ("runtime_eod", "Options account 'A' was handed K; it enters with OptionIntent".to_string()));
        assert_eq!(refusal(entry("A", "K", "B", "I", true)),
            ("runtime_eod", "Intent 'I' targets account 'B' but was produced for 'A' (I8)".to_string()));
    }

    #[test]
    fn engine_is_required() {
        assert!(engine("A", "N", true).is_ok());
        assert_eq!(refusal(engine("A", "N", false)),
            ("runtime_eod", "'A' asked to enter 'N' and has no options risk engine; nothing enters unchecked (I5)".to_string()));
    }

    #[test]
    fn approved_resizes_only_when_accepted_and_amounts_differ() {
        assert_eq!(approved(true, true, "1.0", "1.00").unwrap(), (true, false));
        assert_eq!(approved(true, true, "1", "2").unwrap(), (true, true));
        assert_eq!(approved(false, true, "1", "2").unwrap(), (false, false));
        assert_eq!(approved(true, false, "x", "y").unwrap(), (true, false));
        assert_eq!(refusal(approved(true, true, "x", "1")), ("value", "invalid runtime decimal".to_string()));
    }

    #[test]
    fn increment_adds() {
        assert_eq!(increment(2, 3), 5);
    }

    #[test]
    fn rounds_always_refuses() {
        assert_eq!(refusal(rounds("S", "close", 7)),
            ("runtime_eod", "The strategy for 'S' was still acting at the close snapshot after 7 rounds; refusing to loop on it".to_string()));
    }
}

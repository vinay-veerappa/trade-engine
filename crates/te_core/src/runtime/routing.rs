use super::Plan;
use crate::ledger::model::{err, R};
use crate::ledger::model::{derr, LErr};
use crate::money::Money;
use num_bigint::BigInt;

pub fn decide(op: &str, s: &[String], n: &[i64], b: &[bool]) -> R<Plan> {
    let mut p = Plan::default();
    match op {
        "phase" => p.text.push(if !s[0].is_empty() { s[0].clone() } else if b[0] { "snapshot".into() } else { "close".into() }),
        "taken" => p.flags.push(b[0] && (b[1] || (b[2] && b[3]))),
        "risk_lookup" => p.flags.push(b[0] && b[1]),
        "tally" => {
            let values = s.iter().map(|v| v.parse::<BigInt>().map_err(|_| LErr {
                kind: "value", msg: "invalid runtime integer".into()
            })).collect::<R<Vec<_>>>()?;
            p.text = (0..3).map(|i| (&values[i] + &values[i + 3]).to_string()).collect();
        }
        "underlying" => match s[0].as_str() {
            "intent" | "holding" => p.text.push(s[4].clone()),
            "close" => {
                if !b[0] {
                    return err("runtime_eod", format!("Close '{}' names '{}', which is not an order of '{}' (I8)", s[2], s[3], s[1]));
                }
                p.text.push(s[4].clone());
            }
            _ => return err("runtime_eod", format!("Unknown options action {}", s[5])),
        },
        "action" => {
            if b[0] && s[3] != s[4] {
                return err("runtime_eod", format!("'{}' returned {} '{}' at the {} snapshot for another underlying", s[0], s[1], s[2], s[4]));
            }
            if !b[1] {
                return err("runtime_eod", format!("Options account '{}' was handed {}; it enters with OptionIntent", s[0], s[1]));
            }
        }
        "entry" => {
            if !b[0] {
                return err("runtime_eod", format!("Options account '{}' was handed {}; it enters with OptionIntent", s[0], s[1]));
            }
            if s[2] != s[0] {
                return err("runtime_eod", format!("Intent '{}' targets account '{}' but was produced for '{}' (I8)", s[3], s[2], s[0]));
            }
        }
        "engine" => if !b[0] {
            return err("runtime_eod", format!("'{}' asked to enter '{}' and has no options risk engine; nothing enters unchecked (I5)", s[0], s[1]));
        },
        "approved" => {
            let resize = if b[0] && b[1] {
                let left = Money::parse(&s[0]).ok_or_else(|| LErr { kind: "value", msg: "invalid runtime decimal".into() })?;
                let right = Money::parse(&s[1]).ok_or_else(|| LErr { kind: "value", msg: "invalid runtime decimal".into() })?;
                !left.eq_num(&right).map_err(derr)?
            } else { false };
            p.flags = vec![b[0],resize];
        }
        "increment" => p.numbers.push(n[0] + n[1]),
        "rounds" => return err("runtime_eod", format!("The strategy for '{}' was still acting at the {} snapshot after {} rounds; refusing to loop on it", s[0], s[1], n[0])),
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

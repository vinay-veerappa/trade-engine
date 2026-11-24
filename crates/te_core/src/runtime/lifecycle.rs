//! After-close decisions. Observations arrive in original host order, never from a clock.
use std::collections::HashMap;

use super::Plan;
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
pub fn decide(op: &str, t: &[String], n: &[i64], b: &[bool], _f: &[f64]) -> R<Plan> {
    let mut p = Plan::default();
    match op {
        "session" => if !b[0] { return err("lifecycle", format!("{} is not a session (I5)", t[0])); },
        "compact" => p.text.push(t[0].replace(' ',"")),
        "accounts" => { p.text=t.to_vec(); p.text.sort(); },
        "close" => if n[0] < 0 {
            return err("lifecycle", format!("The clock reads {}, before the {} close {}; expiry and assignment are decided after the close (I9)",t[0],t[1],t[2]));
        },
        "held" => {
            let mut rows = Vec::new();
            for (i, row) in t.chunks_exact(2).enumerate() {
                if b[i] && !eq(&dec(&row[1])?, &zero())? { rows.push((row[0].clone(), i as i64)); }
            }
            rows.sort_by(|a,b| a.0.cmp(&b.0));
            p.numbers = rows.into_iter().map(|v|v.1).collect();
        },
        "position" => {
            let q = dec(&t[0])?;
            p.flags = vec![gt(&q,&zero())?, n[0] <= 0, n[0] < 0];
            p.text.push(s(&abs(&q)?));
        },
        "overdue" => if n[0] < 0 {
            return err("lifecycle", format!("{} in '{}' expired on {} and was never settled; run the lifecycle pass for that session first (I9)",t[0],t[1],t[2]));
        },
        "price_identity" | "price_clock" | "price_known" => {
            if op == "price_identity" && !(t[0] == t[3] && t[1] == t[4] && t[2] == t[5]) {
                return err("lifecycle", format!("Asked for the {} settlement of {} on {}, got {} {} on {} (I5)",t[0],t[1],t[2],t[3],t[4],t[5]));
            }
            if op == "price_clock" && n[0] > 0 {
                return err("lifecycle", format!("{} settlement is stamped {}, after the clock {}: look-ahead (I7)",t[1],t[6],t[7]));
            }
            if op == "price_known" && n[0] < 0 {
                return err("lifecycle", format!("{} settlement is stamped {}, before the settlement instant {}; it cannot be the official price (I9)",t[1],t[6],t[8]));
            }
        },
        "expiry" => {
            let v = intrinsic(&contract(&t[0],&t[3],n,b[0])?,&t[1])?;
            let expired = lt(&v,&dec("0.01")?)?;
            let kind = if expired { "Expiry" } else if b[1] { "Exercise" } else { "Assignment" };
            let reason = if expired {
                format!("expired worthless: {} in the money at the {} settlement {}, under the 0.01 exercise threshold",s(&v),t[2],t[1])
            } else {
                format!("{} at expiry: {} in the money at the {} settlement {}",if b[1] {"exercised"} else {"assigned"},s(&v),t[2],t[1])
            };
            p.text = vec![kind.into(),reason];
        },
        "eligible" => p.flags.push(b[0] && b[1] && b[2]),
        "dividend_source" => if !b[0] {
            return err("lifecycle",format!("'{}' is short the American call {} and no dividend source is configured, so its early assignment cannot be decided (I5)",t[0],t[1]));
        },
        "dividend_time" => if n[0] > 0 {
            return err("lifecycle",format!("{} dividend record is from after the clock: look-ahead (I7)",t[0]));
        },
        "sum" => {
            let mut amount = zero();
            for v in t { amount = add(&amount,&dec(v)?)?; }
            p.text.push(s(&amount));
            p.flags.push(!t.is_empty());
        },
        "itm" => p.flags.push(!lt(&intrinsic(&contract(&t[0],&t[2],n,b[0])?,&t[1])?,&dec("0.01")?)?),
        "quote_source" => if !b[0] {
            return err("lifecycle",format!("{} in '{}' is in the money before a {} dividend and no option quote source is configured (I5)",t[0],t[1],t[2]));
        },
        "quote_time" => if n[0] > 0 {
            return err("stale",format!("{} quote is from after the clock: look-ahead (I7)",t[0]));
        },
        "early" => {
            let c = contract(&t[0],&t[5],n,true)?;
            let exercise = crate::options::lifecycle::exercised_for_dividend(&c,&t[1],&t[2],&t[3]).map_err(option_error)?;
            let extrinsic = sub(&dec(&t[2])?,&intrinsic(&c,&t[1])?)?;
            p.flags.push(exercise);
            p.text.push(format!("assigned early: goes ex a {} dividend on {}, more than the call's extrinsic value {} (bid {}, close {})",t[3],t[4],s(&extrinsic),t[2],t[1]));
        },
        "event" => {
            p.text = vec![t[0].clone(),t[1].clone(),t[2].clone(),t[3].clone(),t[4].clone(),
                          format!("lifecycle:{}:{}:{}{}",t[0],t[1].replace(' ',""),t[5],if b[0] {":early"} else {""})];
            p.flags.push(b[0]);
        },
        "aware" => if !b[0] { return err("value",format!("{} must be timezone-aware UTC (I7)",t[0])); },
        "value" => {
            if b[0] && t[0].is_empty() { return err("value","SettlementPrice.underlying must be non-empty"); }
            let valid = b[1] && dec(&t[1]).map(|v|v.is_finite() && gt(&v,&zero()).unwrap_or(false)).unwrap_or(false);
            if !valid { return err("value",format!("{}.{} must be a positive Decimal, got {} (I5)",t[2],if b[0] {"price"} else {"amount"},t[3])); }
            if !b[2] { return err("value",format!("{}.source must be non-empty (I11)",t[2])); }
        },
        "action_amount" => {
            let valid = b[0] && dec(&t[0]).map(|v|v.is_finite() && gt(&v,&zero()).unwrap_or(false)).unwrap_or(false);
            if !valid {return err("stale",format!("{} dividend going ex {} has no usable amount: {} (I5)",t[1],t[2],t[3]));}
        },
        "mirror_net" => {
            // Each occ's quantities summed from zero with Money, in first-appearance order;
            // a contract that nets to zero leaves the book. `numbers` is each kept occ's
            // first input row, so the host recovers the instrument object from it.
            let mut slot: HashMap<&str, usize> = HashMap::new();
            let mut nets: Vec<(&str, i64, Money)> = Vec::new();
            for (i, row) in t.chunks_exact(2).enumerate() {
                let q = dec(&row[1])?;
                match slot.get(row[0].as_str()) {
                    Some(&k) => {
                        let total = add(&nets[k].2, &q)?;
                        nets[k].2 = total;
                    }
                    None => {
                        slot.insert(row[0].as_str(), nets.len());
                        let total = add(&zero(), &q)?;
                        nets.push((row[0].as_str(), i as i64, total));
                    }
                }
            }
            for (occ, first, net) in nets {
                if !eq(&net, &zero())? {
                    p.text.push(occ.to_string());
                    p.text.push(s(&net));
                    p.numbers.push(first);
                }
            }
        },
        // Python's str.strip() and Rust's str::trim() both drop Unicode whitespace. They differ
        // only on U+001C..U+001F, which Python's isspace() counts and Rust's White_Space omits.
        "symbol_key" => p.text.push(t[0].trim().to_uppercase()),
        "settlement_conflict" => if b[0] {
            return err("value", format!("Two different {} settlements for {} on {} (I5)", t[0], t[1], t[2]));
        },
        "settlement_missing" => return err("stale", format!("No {} settlement for {} on {} (I5)", t[0], t[1], t[2])),
        "dividend_missing" => return err("stale", format!("No dividend record for {} (I5)", t[0])),
        "quote_missing" => return err("stale", format!("{} is not in the {} chain snapshot of {} (I5)", t[0], t[1], t[2])),
        _ => return err("value",format!("Unknown lifecycle decision {op}")),
    }
    Ok(p)
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

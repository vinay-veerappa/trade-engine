//! After-close decisions. Observations arrive in original host order, never from a clock.
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
}

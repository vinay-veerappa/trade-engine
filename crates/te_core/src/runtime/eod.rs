use super::Plan;
use crate::ledger::model::{derr, err, LErr, R};
use crate::ledger::ops::*;
use crate::ledger::pydec::PyDec;
use num_bigint::BigUint;

fn dec(s: &str) -> R<PyDec> {
    PyDec::parse(s).ok_or_else(|| LErr { kind: "value", msg: "invalid runtime decimal".into() })
}
fn truncate(value: &PyDec) -> R<String> {
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
        "pass_of" => if let Some(name) = ["morning","midday","late"].iter().find(|name| s[0].ends_with(&format!("-{name}"))) {
            p.text.push((*name).into());
        },
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
        "previous" => if b[0] && !b[1] {
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
        _ => return err("value",format!("Unknown EOD decision {op}")),
    }
    Ok(p)
}

#[cfg(test)]
mod tests {
    use super::*;
    use proptest::prelude::*;
    #[test]
    fn boundary_and_refusal() {
        assert!(decide("cutoff",&[],&[5,5],&[],&[]).unwrap().flags[0]);
        assert!(decide("preopen",&[],&[4,5],&[],&[]).unwrap().flags[0]);
        assert!(!decide("preopen",&[],&[5,5],&[],&[]).unwrap().flags[0]);
        assert!(decide("config",&[],&[0],&[false,true,true,true],&[1.0]).is_err());
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

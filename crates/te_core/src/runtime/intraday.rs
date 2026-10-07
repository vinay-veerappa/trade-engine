use super::Plan;
use crate::ledger::model::{derr, err, LErr, R};
use crate::money::Money;

pub fn decide(op: &str, s: &[String], n: &[i64], b: &[bool], f: &[f64]) -> R<Plan> {
    let mut p = Plan::default();
    match op {
        "config_identity" => {
            for (i,message) in ["job_name must be non-empty","account_id must be non-empty","underlying must be non-empty","eod_job_name must name the after-close job whose marker gates the next session"].iter().enumerate() {
                if !b[i] { return err("runtime_intraday",*message); }
            }
        }
        "config_rates" => {
            if f[0] <= 0.0 || f[1] <= 0.0 { return err("runtime_intraday","quote age and tick must be positive"); }
            if f[2] <= 0.0 || f[3] <= 0.0 { return err("runtime_intraday","heartbeat ttl and close-mark window must be positive"); }
        }
        "config_wall" => {
            if n[0] > n[1] { return err("runtime_intraday","entry_end must not come after flat_at"); }
        }
        "config_early" => if !(0 < n[0] && n[0] <= n[1]) { return err("runtime_intraday","flat_before_close must be positive and no later than entry_before_close"); },
        "session" => if !b[0] { return err("runtime_intraday",format!("{} is not a trading session of {} (I5)",s[0],s[1])); },
        "gate" => p.flags = b.iter().map(|entry| b[0] || !entry).skip(1).collect(),
        "tick" => {
            let flat_due = n[0] >= n[2];
            p.flags = vec![flat_due,!b[0] && n[0] <= n[1] && !flat_due];
        }
        "snapshot_identity" => {
            if !b[0] { return err("runtime_stale",format!("The snapshot source returned {} (I5)",s[0])); }
            if s[1] != s[2] { return err("runtime_stale",format!("Asked for the {} chain, got {} (I5)",s[1],s[2])); }
        }
        "underlying_stamp" => if !b[0] { return err("runtime_stale",format!("The {} snapshot of {} does not say when {} itself was quoted; a live market cannot be assumed (I5)",s[0],s[1],s[0])); },
        "underlying_age" => if f[0] > f[1] { return err("runtime_stale",format!("{} was last quoted {}s ago ({}), over the {}s allowed (I5)",s[0],s[1],s[2],s[3])); },
        "leg" => {
            if !b[0] { return err("runtime_stale",format!("Held leg {} is not in the {} snapshot (I5)",s[0],s[1])); }
            if f[0] > f[1] { return err("runtime_stale",format!("Held leg {} was last quoted {}s ago, over the {}s allowed (I5)",s[0],s[2],s[3])); }
        }
        "fresh" => p.flags = f[1..].iter().map(|age| *age <= f[0]).collect(),
        "held" => p.flags.push(b[0] && b[1] && s[0] == s[1]),
        "flatten" => p.flags.push(!b[0] && s[0] == s[1]),
        "attempt" => p.numbers.push(1 + s.iter().filter(|id| id.starts_with(&format!("{}:close:",s[0]))).count() as i64),
        "cancel" => p.flags.push(b[0] && b[1] && ["NEW","SUBMITTED","ACCEPTED","PARTIALLY_FILLED"].contains(&s[0].as_str()) && s[1] == s[2]),
        "restart" => p.text.push(if s[0] != "NEW" { "ignore" } else if b[0] && b[1] { "cancel" } else if s[1].starts_with(&format!("{}:close:",s[2])) { "submit" } else { "ignore" }.into()),
        "drift" => {
            for row in s.chunks_exact(3) {
                let same = if row[1] == "None" || row[2] == "None" { row[1] == row[2] }
                    else {
                        let a = Money::parse(&row[1]).ok_or_else(|| LErr {kind: "value", msg: "invalid runtime decimal".into()})?;
                        let b = Money::parse(&row[2]).ok_or_else(|| LErr {kind: "value", msg: "invalid runtime decimal".into()})?;
                        a.eq_num(&b).map_err(derr)?
                    };
                if !same { p.text.push(row[0].clone()); }
            }
            p.text.sort();
        }
        "drift_refuse" => if !s[1].is_empty() { return err("runtime_intraday",format!("The venue's positions disagree with the ledger for '{}' on {}; refusing to trade on either (I11)",s[0],s[1])); },
        "deadlines" => {
            let flat = n[0].min(n[2] - n[3]);
            let entry = n[1].min(n[2] - n[4]).min(flat);
            let flat_index = if flat == n[0] { 0 } else { 1 };
            let entry_index = if entry == n[1] { 0 } else if entry == n[2] - n[4] { 1 } else { 2 };
            p.numbers = vec![entry_index,flat_index];
        }
        "close_usable" => p.flags.push(b[0] && f[0] <= f[1]),
        "close_plan" => p.flags.push(b[0] && b[1] && n[0] == n[1]),
        "heartbeat_account" => if s[0] != s[1] { return err("runtime_intraday",format!("The heartbeat file {} belongs to '{}', not '{}'; two accounts must not share one (C4)",s[2],s[0],s[1])); },
        "heartbeat" => if b[0] && f[0] <= f[1] {
            return err("runtime_intraday",format!("A heartbeat for '{}' written {} is still fresh; another instance is running this session — refusing to start a second one (C4)",s[0],s[1]));
        },
        "previous" => if b[0] { return err("runtime_intraday",format!("Cannot run {} for '{}': the previous session {} has no '{}' EOD marker ({}); complete it first (I3)",s[0],s[1],s[2],s[3],s[4])); },
        "flat_close" => p.text = vec![if s[0].is_empty() { s[1].clone() } else { s[0].clone() },format!("intraday:flat:{}:{}:{}:{}",s[2],s[1],s[3],n[0])],
        _ => return err("value",format!("Unknown intraday decision {op}")),
    }
    Ok(p)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn early_close_and_entry_tie() {
        assert_eq!(decide("deadlines",&[],&[1530,1200,1300,30,90],&[],&[]).unwrap().numbers,vec![0,1]);
        assert_eq!(decide("tick",&[],&[12,12,13],&[false],&[]).unwrap().flags,vec![false,true]);
    }
}

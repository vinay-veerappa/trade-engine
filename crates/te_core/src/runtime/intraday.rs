use crate::ledger::model::{derr, err, LErr, R};
use crate::ledger::ops::{gt, zero};
use crate::money::Money;

// Typed rules. Each keeps the string door's error kind and message byte-identical.

pub fn config_identity(job_name: bool, account_id: bool, underlying: bool, eod_job_name: bool) -> R<()> {
    let checks = [
        (job_name, "job_name must be non-empty"),
        (account_id, "account_id must be non-empty"),
        (underlying, "underlying must be non-empty"),
        (eod_job_name, "eod_job_name must name the after-close job whose marker gates the next session"),
    ];
    for (ok, message) in checks {
        if !ok { return err("runtime_intraday", message); }
    }
    Ok(())
}

pub fn config_rates(quote_age: f64, tick: f64, heartbeat_ttl: f64, close_mark_window: f64) -> R<()> {
    if !(quote_age > 0.0) || !(tick > 0.0) { return err("runtime_intraday", "quote age and tick must be positive"); }
    if !(heartbeat_ttl > 0.0) || !(close_mark_window > 0.0) {
        return err("runtime_intraday", "heartbeat ttl and close-mark window must be positive");
    }
    Ok(())
}

pub fn config_wall(entry_end: i64, flat_at: i64) -> R<()> {
    if entry_end > flat_at { return err("runtime_intraday", "entry_end must not come after flat_at"); }
    Ok(())
}

pub fn config_early(flat_before_close: i64, entry_before_close: i64) -> R<()> {
    if !(0 < flat_before_close && flat_before_close <= entry_before_close) {
        return err("runtime_intraday", "flat_before_close must be positive and no later than entry_before_close");
    }
    Ok(())
}

pub fn session(is_session: bool, session: &str, exchange: &str) -> R<()> {
    if !is_session { return err("runtime_intraday", format!("{session} is not a trading session of {exchange} (I5)")); }
    Ok(())
}

/// Per option intent, keep it when the gate is open (`allow`) or the intent is not an option.
pub fn gate(allow: bool, option_intents: &[bool]) -> R<Vec<bool>> {
    Ok(option_intents.iter().map(|intent| allow || !intent).collect())
}

/// `(flat_due, may_enter)`: the flat sweep is due once `now` reaches `flat_deadline`; entries stay
/// open until `entry_deadline` unless barred, and never once the sweep is due.
pub fn tick(now: i64, entry_deadline: i64, flat_deadline: i64, barred: bool) -> R<(bool, bool)> {
    let flat_due = now >= flat_deadline;
    Ok((flat_due, !barred && now <= entry_deadline && !flat_due))
}

pub fn snapshot_identity(is_chain: bool, source: &str, configured: &str, got: &str) -> R<()> {
    if !is_chain { return err("runtime_stale", format!("The snapshot source returned {source} (I5)")); }
    if configured != got { return err("runtime_stale", format!("Asked for the {configured} chain, got {got} (I5)")); }
    Ok(())
}

pub fn underlying_stamp(quoted: bool, underlying: &str, as_of: &str) -> R<()> {
    if !quoted {
        return err("runtime_stale", format!("The {underlying} snapshot of {as_of} does not say when {underlying} itself was quoted; a live market cannot be assumed (I5)"));
    }
    Ok(())
}

pub fn underlying_age(age: f64, limit: f64, underlying: &str, age_text: &str, quoted: &str, limit_text: &str) -> R<()> {
    if age > limit {
        return err("runtime_stale", format!("{underlying} was last quoted {age_text}s ago ({quoted}), over the {limit_text}s allowed (I5)"));
    }
    Ok(())
}

pub fn leg(quoted: bool, age: f64, limit: f64, occ: &str, underlying: &str, age_text: &str, limit_text: &str) -> R<()> {
    if !quoted { return err("runtime_stale", format!("Held leg {occ} is not in the {underlying} snapshot (I5)")); }
    if age > limit {
        return err("runtime_stale", format!("Held leg {occ} was last quoted {age_text}s ago, over the {limit_text}s allowed (I5)"));
    }
    Ok(())
}

/// One flag per age: true when that quote is no older than `limit`.
pub fn fresh(limit: f64, ages: &[f64]) -> R<Vec<bool>> {
    Ok(ages.iter().map(|age| *age <= limit).collect())
}

/// True for an option on the configured underlying that carries a quantity.
pub fn held(is_option: bool, nonzero: bool, underlying_of: &str, configured: &str) -> R<bool> {
    Ok(is_option && nonzero && underlying_of == configured)
}

/// True when the structure is on the configured underlying and no closing order is out yet.
pub fn flatten(underlying_of: &str, underlying: &str, closing_sent: bool) -> R<bool> {
    Ok(!closing_sent && underlying_of == underlying)
}

/// One more than the close orders already sent for `entry_id` among `ids`, which include the entry.
pub fn attempt(entry_id: &str, ids: &[String]) -> R<i64> {
    let prefix = format!("{entry_id}:close:");
    Ok(1 + ids.iter().filter(|id| id.starts_with(&prefix)).count() as i64)
}

/// True when a top-level structure in a live state is on the configured underlying.
pub fn cancel(top_level: bool, structure: bool, state: &str, underlying_of: &str, configured: &str) -> R<bool> {
    Ok(top_level
        && structure
        && ["NEW", "SUBMITTED", "ACCEPTED", "PARTIALLY_FILLED"].contains(&state)
        && underlying_of == configured)
}

/// What a restart does with one order: only a NEW order is acted on. A top-level structure is
/// cancelled, and an order that closes its parent is submitted. Anything else is left alone.
pub fn restart(state: &str, top_level: bool, structure: bool, order_id: &str, parent_id: &str) -> R<String> {
    let action = if state != "NEW" { "ignore" }
        else if top_level && structure { "cancel" }
        else if order_id.starts_with(&format!("{parent_id}:close:")) { "submit" }
        else { "ignore" };
    Ok(action.into())
}

/// The instruments whose ledger and venue quantities disagree, sorted. Rows come as
/// (instrument, ledger quantity, venue quantity), with "None" for a missing side.
pub fn drift(rows: &[String]) -> R<Vec<String>> {
    let mut text = Vec::new();
    for row in rows.chunks_exact(3) {
        let same = if row[1] == "None" || row[2] == "None" { row[1] == row[2] }
            else {
                let a = Money::parse(&row[1]).ok_or_else(|| LErr { kind: "value", msg: "invalid runtime decimal".into() })?;
                let b = Money::parse(&row[2]).ok_or_else(|| LErr { kind: "value", msg: "invalid runtime decimal".into() })?;
                a.eq_num(&b).map_err(derr)?
            };
        if !same { text.push(row[0].clone()); }
    }
    text.sort();
    Ok(text)
}

pub fn drift_refuse(account: &str, drift: &str) -> R<()> {
    if !drift.is_empty() {
        return err("runtime_intraday", format!("The venue's positions disagree with the ledger for '{account}' on {drift}; refusing to trade on either (I11)"));
    }
    Ok(())
}

/// `(entry_index, flat_index)`. The entry deadline is the earliest of entry_at, close minus
/// entry_before_close and the flat deadline (indices 0, 1, 2). The flat deadline is the earlier of
/// flat_at and close minus flat_before_close (indices 0, 1).
pub fn deadlines(flat_at: i64, entry_at: i64, close: i64, flat_before_close: i64, entry_before_close: i64) -> R<(i64, i64)> {
    let flat = flat_at.min(close - flat_before_close);
    let entry = entry_at.min(close - entry_before_close).min(flat);
    let flat_index = if flat == flat_at { 0 } else { 1 };
    let entry_index = if entry == entry_at { 0 } else if entry == close - entry_before_close { 1 } else { 2 };
    Ok((entry_index, flat_index))
}

/// True when a snapshot exists and its underlying age is within the mark window.
pub fn close_usable(has_snapshot: bool, age: f64, window: f64) -> R<bool> {
    Ok(has_snapshot && age <= window)
}

/// True when the close can be marked: the snapshot is usable, the quotes are fresh and every held
/// option is quoted.
pub fn close_plan(usable: bool, quotes_ok: bool, quoted: i64, held_total: i64) -> R<bool> {
    Ok(usable && quotes_ok && quoted == held_total)
}

/// True when every quote is present, has a positive mid and is no older than `max_age`. `ages`
/// holds one age per quote, and a length mismatch refuses.
pub fn close_quotes_ok(mids: &[String], max_age: f64, ages: &[f64]) -> R<bool> {
    if ages.len() != mids.len() {
        return err("value", format!("close_quotes_ok needs the max age and one age per quote ({} values); got {}", mids.len() + 1, ages.len() + 1));
    }
    for (mid, age) in mids.iter().zip(ages) {
        if mid == "None" { return Ok(false); }
        let price = Money::parse(mid).ok_or_else(|| LErr { kind: "value", msg: "invalid runtime decimal".into() })?;
        if !gt(&price, &zero())? || *age > max_age { return Ok(false); }
    }
    Ok(true)
}

pub fn heartbeat_account(file_account: &str, account: &str, path: &str) -> R<()> {
    if file_account != account {
        return err("runtime_intraday", format!("The heartbeat file {path} belongs to '{file_account}', not '{account}'; two accounts must not share one (C4)"));
    }
    Ok(())
}

pub fn heartbeat(account: &str, written: &str, same_session: bool, age: f64, ttl: f64) -> R<()> {
    if same_session && age <= ttl {
        return err("runtime_intraday", format!("A heartbeat for '{account}' written {written} is still fresh; another instance is running this session — refusing to start a second one (C4)"));
    }
    Ok(())
}

pub fn previous(has_history: bool, session: &str, account: &str, previous: &str, eod_job: &str, marker: &str) -> R<()> {
    if has_history {
        return err("runtime_intraday", format!("Cannot run {session} for '{account}': the previous session {previous} has no '{eod_job}' EOD marker ({marker}); complete it first (I3)"));
    }
    Ok(())
}

/// `(reason, command_id)` for one flatten order. The reason is `reason` when given, else `code`.
pub fn flat_close(reason: &str, code: &str, entry_id: &str, session: &str, attempt: i64) -> R<(String, String)> {
    let reason = if reason.is_empty() { code } else { reason };
    Ok((reason.to_string(), format!("intraday:flat:{entry_id}:{code}:{session}:{attempt}")))
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn config_rates_refuses_nan_in_each_float() {
        // Each of the four floats, alone set to NaN, must refuse with the message for its pair.
        let quote_pair = "quote age and tick must be positive";
        let heartbeat_pair = "heartbeat ttl and close-mark window must be positive";
        for (index, message) in [(0, quote_pair), (1, quote_pair), (2, heartbeat_pair), (3, heartbeat_pair)] {
            let mut floats = [1.0_f64; 4];
            floats[index] = f64::NAN;
            match config_rates(floats[0], floats[1], floats[2], floats[3]) {
                Err(e) => assert_eq!((e.kind, e.msg.as_str()), ("runtime_intraday", message), "float {index}"),
                Ok(_) => panic!("NaN float {index} must refuse"),
            }
        }
        assert!(config_rates(1.0, 1.0, 1.0, 1.0).is_ok());
    }
    #[test]
    fn early_close_and_entry_tie() {
        assert_eq!(deadlines(1530, 1200, 1300, 30, 90).unwrap(), (0, 1));
        assert_eq!(tick(12, 12, 13, false).unwrap(), (false, true));
    }

    fn quotes_ok(mids: &[&str], floats: &[f64]) -> R<bool> {
        let s: Vec<String> = mids.iter().map(|m| m.to_string()).collect();
        close_quotes_ok(&s, floats[0], &floats[1..])
    }

    #[test]
    fn close_quotes_ok_all_fresh_and_positive_is_true() {
        assert!(quotes_ok(&["1.5","2"],&[30.0,1.0,5.0]).unwrap());
    }

    #[test]
    fn close_quotes_ok_missing_quote_is_false_and_its_mid_is_not_parsed() {
        assert!(!quotes_ok(&["1.5","None"],&[30.0,1.0,0.0]).unwrap());
        assert!(!quotes_ok(&["None","abc"],&[30.0,0.0,0.0]).unwrap());
    }

    #[test]
    fn close_quotes_ok_non_positive_mid_is_false_whatever_its_age() {
        assert!(!quotes_ok(&["0"],&[30.0,1.0]).unwrap());
        assert!(!quotes_ok(&["-1.5"],&[30.0,1.0]).unwrap());
    }

    #[test]
    fn close_quotes_ok_age_limit_is_inclusive() {
        assert!(quotes_ok(&["1.5"],&[30.0,30.0]).unwrap());
        assert!(!quotes_ok(&["1.5"],&[30.0,30.5]).unwrap());
    }

    #[test]
    fn close_quotes_ok_empty_input_is_true() {
        assert!(quotes_ok(&[],&[30.0]).unwrap());
    }

    #[test]
    fn close_quotes_ok_length_mismatch_refuses() {
        match quotes_ok(&["1.5"],&[30.0]) {
            Err(e) => assert_eq!(e.kind, "value"),
            Ok(_) => panic!("a length mismatch must refuse"),
        }
    }

    #[test]
    fn close_quotes_ok_bad_decimal_refuses_with_the_drift_text() {
        match quotes_ok(&["abc"],&[30.0,1.0]) {
            Err(e) => assert_eq!(e.msg, "invalid runtime decimal"),
            Ok(_) => panic!("a bad decimal must refuse"),
        }
    }
}

#[cfg(test)]
mod typed_tests {
    use super::*;

    #[test]
    fn config_identity_names_the_first_blank_field() {
        let e = config_identity(true, true, false, true).unwrap_err();
        assert_eq!((e.kind, e.msg.as_str()), ("runtime_intraday", "underlying must be non-empty"));
        assert!(config_identity(true, true, true, true).is_ok());
    }

    #[test]
    fn config_rates_each_pair_has_its_own_message() {
        let e = config_rates(0.0, 1.0, 1.0, 1.0).unwrap_err();
        assert_eq!(e.msg, "quote age and tick must be positive");
        let e = config_rates(1.0, 1.0, 1.0, -1.0).unwrap_err();
        assert_eq!(e.msg, "heartbeat ttl and close-mark window must be positive");
        assert!(config_rates(1.0, 1.0, 1.0, 1.0).is_ok());
    }

    #[test]
    fn config_wall_refuses_entry_end_after_flat_at() {
        assert!(config_wall(2, 1).is_err());
        assert!(config_wall(1, 1).is_ok());
    }

    #[test]
    fn config_early_needs_a_positive_flat_no_later_than_entry() {
        assert!(config_early(0, 1).is_err());
        assert!(config_early(2, 1).is_err());
        assert!(config_early(1, 1).is_ok());
    }

    #[test]
    fn session_refuses_a_non_session_with_its_text() {
        let e = session(false, "2026-11-24", "NYSE").unwrap_err();
        assert_eq!((e.kind, e.msg.as_str()), ("runtime_intraday", "2026-11-24 is not a trading session of NYSE (I5)"));
    }

    #[test]
    fn gate_keeps_non_options_and_all_when_open() {
        assert_eq!(gate(false, &[true, false]).unwrap(), [false, true]);
        assert_eq!(gate(true, &[true]).unwrap(), [true]);
    }

    #[test]
    fn tick_flags_flat_due_and_entry_window() {
        assert_eq!(tick(5, 9, 20, false).unwrap(), (false, true));
        assert_eq!(tick(10, 9, 20, false).unwrap(), (false, false));
        assert_eq!(tick(20, 9, 20, false).unwrap(), (true, false));
        assert_eq!(tick(5, 9, 20, true).unwrap(), (false, false));
    }

    #[test]
    fn snapshot_identity_refuses_wrong_source_then_wrong_chain() {
        let e = snapshot_identity(false, "Quote", "SPX", "SPX").unwrap_err();
        assert_eq!(e.msg, "The snapshot source returned Quote (I5)");
        let e = snapshot_identity(true, "Chain", "SPX", "NDX").unwrap_err();
        assert_eq!(e.msg, "Asked for the SPX chain, got NDX (I5)");
        assert!(snapshot_identity(true, "Chain", "SPX", "SPX").is_ok());
    }

    #[test]
    fn underlying_stamp_refuses_an_unstamped_snapshot() {
        let e = underlying_stamp(false, "SPX", "2026-11-24T20:00:00+00:00").unwrap_err();
        assert_eq!(e.msg, "The SPX snapshot of 2026-11-24T20:00:00+00:00 does not say when SPX itself was quoted; a live market cannot be assumed (I5)");
    }

    #[test]
    fn underlying_age_is_inclusive_at_the_limit() {
        assert!(underlying_age(30.0, 30.0, "SPX", "30", "q", "30").is_ok());
        let e = underlying_age(31.0, 30.0, "SPX", "31", "q", "30").unwrap_err();
        assert_eq!(e.msg, "SPX was last quoted 31s ago (q), over the 30s allowed (I5)");
    }

    #[test]
    fn leg_refuses_a_missing_then_a_stale_quote() {
        let e = leg(false, 0.0, 30.0, "AAPL", "SPX", "0", "30").unwrap_err();
        assert_eq!(e.msg, "Held leg AAPL is not in the SPX snapshot (I5)");
        let e = leg(true, 31.0, 30.0, "AAPL", "SPX", "31", "30").unwrap_err();
        assert_eq!(e.msg, "Held leg AAPL was last quoted 31s ago, over the 30s allowed (I5)");
        assert!(leg(true, 1.0, 30.0, "AAPL", "SPX", "1", "30").is_ok());
    }

    #[test]
    fn fresh_flags_each_age_against_the_limit() {
        assert_eq!(fresh(30.0, &[1.0, 31.0, 30.0]).unwrap(), [true, false, true]);
    }

    #[test]
    fn held_needs_an_option_with_quantity_on_the_underlying() {
        assert!(held(true, true, "SPX", "SPX").unwrap());
        assert!(!held(true, true, "NDX", "SPX").unwrap());
        assert!(!held(true, false, "SPX", "SPX").unwrap());
    }

    #[test]
    fn flatten_needs_no_closing_order_yet() {
        assert!(flatten("SPX", "SPX", false).unwrap());
        assert!(!flatten("SPX", "SPX", true).unwrap());
    }

    #[test]
    fn attempt_counts_close_orders_for_the_entry() {
        let ids: Vec<String> = ["E1", "E1:close:1", "E1:close:2", "X"].iter().map(|s| s.to_string()).collect();
        assert_eq!(attempt("E1", &ids).unwrap(), 3);
    }

    #[test]
    fn cancel_needs_a_live_top_level_structure_on_the_underlying() {
        assert!(cancel(true, true, "NEW", "SPX", "SPX").unwrap());
        assert!(!cancel(true, true, "FILLED", "SPX", "SPX").unwrap());
        assert!(!cancel(false, true, "NEW", "SPX", "SPX").unwrap());
        assert!(!cancel(true, true, "NEW", "NDX", "SPX").unwrap());
    }

    #[test]
    fn restart_acts_only_on_new_orders() {
        assert_eq!(restart("NEW", true, true, "S1", "None").unwrap(), "cancel");
        assert_eq!(restart("FILLED", true, true, "S1", "None").unwrap(), "ignore");
        assert_eq!(restart("NEW", false, false, "P1:close:1", "P1").unwrap(), "submit");
        assert_eq!(restart("NEW", false, false, "C1", "P1").unwrap(), "ignore");
    }

    #[test]
    fn drift_lists_real_mismatches_sorted_and_ignores_equal_decimals() {
        // B agrees; A is missing on the venue; C differs; D agrees as decimals (3.0 == 3).
        let rows: Vec<String> = ["B", "1", "1", "A", "None", "2", "C", "3", "4", "D", "3.0", "3"]
            .iter().map(|s| s.to_string()).collect();
        assert_eq!(drift(&rows).unwrap(), ["A", "C"].map(String::from).to_vec());
    }

    #[test]
    fn drift_refuse_only_with_drift_text() {
        assert!(drift_refuse("acct", "").is_ok());
        assert_eq!(drift_refuse("acct", "A").unwrap_err().msg,
            "The venue's positions disagree with the ledger for 'acct' on A; refusing to trade on either (I11)");
    }

    #[test]
    fn deadlines_cap_entry_at_the_flat_deadline() {
        assert_eq!(deadlines(100, 200, 300, 10, 10).unwrap(), (2, 0));
    }

    #[test]
    fn close_usable_needs_a_snapshot_within_the_window() {
        assert!(close_usable(true, 30.0, 30.0).unwrap());
        assert!(!close_usable(true, 31.0, 30.0).unwrap());
        assert!(!close_usable(false, 0.0, 30.0).unwrap());
    }

    #[test]
    fn close_plan_needs_every_held_option_quoted() {
        assert!(close_plan(true, true, 2, 2).unwrap());
        assert!(!close_plan(true, true, 1, 2).unwrap());
    }

    #[test]
    fn heartbeat_account_refuses_a_shared_file() {
        let e = heartbeat_account("A", "B", "hb.json").unwrap_err();
        assert_eq!(e.msg, "The heartbeat file hb.json belongs to 'A', not 'B'; two accounts must not share one (C4)");
    }

    #[test]
    fn heartbeat_refuses_only_a_fresh_beat_of_this_session() {
        assert!(heartbeat("A", "t", true, 10.0, 30.0).is_err());
        assert!(heartbeat("A", "t", false, 10.0, 30.0).is_ok());
        assert!(heartbeat("A", "t", true, 31.0, 30.0).is_ok());
    }

    #[test]
    fn previous_refuses_when_the_account_has_history() {
        let e = previous(true, "S", "A", "P", "eod", "m").unwrap_err();
        assert_eq!(e.msg, "Cannot run S for 'A': the previous session P has no 'eod' EOD marker (m); complete it first (I3)");
        assert!(previous(false, "S", "A", "P", "eod", "m").is_ok());
    }

    #[test]
    fn flat_close_uses_code_when_no_reason() {
        assert_eq!(flat_close("", "code", "E", "S", 2).unwrap(), ("code".into(), "intraday:flat:E:code:S:2".into()));
        assert_eq!(flat_close("why", "code", "E", "S", 2).unwrap().0, "why");
    }
}

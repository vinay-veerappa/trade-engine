//! The intraday host's ledger glue, in typed Rust over a [`Ledger`] (W6-D1).
//!
//! Ports the ledger reads and writes of `src/trade_engine/intraday/service.py` (`_cause`,
//! `_eod_marker`, `_held_contracts`, `_record_tick`, `_deadlines`, `_session_snapshot_count`,
//! and the writes of `_close_mark`). Each Python `decide("intraday:x" / "eod:x", ...)` call
//! becomes a direct call to the typed rule with the same values, so every refusal keeps the
//! kind and message the typed rule raises.
//!
//! Nothing here touches a plugin (broker, strategy, market data, chain snapshots, the clock's
//! sleep). Those stay in Python.

use chrono::{NaiveDate, NaiveTime, TimeZone, Utc};

use crate::calendar::{self, ny_time_to_utc};
use crate::ledger::fold::AccountState;
use crate::ledger::model::{err, DateTime, EodRun, EventKind, Instrument, Mark, Obj, VenueReconcile, R};
use crate::ledger::ops;
use crate::money::Money;
use crate::runtime::eod;
use crate::runtime::eod_glue::{self, Clock, Ledger};
use crate::runtime::intraday;
use crate::sim::snapshot::underlying_of;
use crate::sim::{self, Ts};

/// A timestamp on the te_core scale: microseconds since 0001-01-01 UTC. Only ever compared with
/// values on the same scale, so the base never leaks out.
pub fn ts_micros(ts: &Ts) -> i64 {
    let (secs, us) = ts.key();
    secs * 1_000_000 + us as i64
}

/// `ts_micros` of a UTC instant.
pub fn utc_micros(t: chrono::DateTime<Utc>) -> i64 {
    ts_micros(&Ts::utc(t))
}

/// A New York wall-clock time on `session`, as te_core micros (the calendar's `ny_time_to_utc`).
pub fn wall_micros(session: NaiveDate, t: NaiveTime) -> i64 {
    utc_micros(ny_time_to_utc(session, t))
}

/// `"intraday:<job>:<account>:<session>"`: the command of this job's session (`_cause`).
pub fn cause(job: &str, account: &str, session: NaiveDate) -> R<String> {
    Ok(eod::command("intraday", job, account, &eod_glue::iso(session)))
}

/// `"eod:<eod_job>:<account>:<session>"`: the marker the after-close job writes (`_eod_marker`).
pub fn eod_marker(eod_job: &str, account: &str, session: NaiveDate) -> R<String> {
    Ok(eod::command("eod", eod_job, account, &eod_glue::iso(session)))
}

/// True when a command is already in the ledger.
pub fn present<L: Ledger>(l: &L, command: &str) -> R<bool> {
    Ok(l.event_by_command(command)?.is_some())
}

/// True when the after-close job has already written its marker for this account and session.
pub fn settled<L: Ledger>(l: &L, eod_job: &str, account: &str, session: NaiveDate) -> R<bool> {
    Ok(l.event_by_command(&eod_marker(eod_job, account, session)?)?.is_some())
}

/// True when the account has any EOD history at all (`_has_history`).
pub fn has_history<L: Ledger>(l: &L, account: &str) -> R<bool> {
    let kinds: Vec<String> = l.events(account)?.iter().map(|e| e.kind.value().to_string()).collect();
    Ok(eod::any_history(&kinds))
}

/// The fresh snapshots processed this session, across every process (`_session_snapshot_count`).
pub fn snapshot_count<L: Ledger>(l: &L, account: &str, cause: &str) -> R<i64> {
    let rows = eod_glue::kind_rows(l, account)?;
    let prefix = format!("{cause}:tick:");
    Ok(eod::count_events(EventKind::VenueReconcile.value(), &prefix, &rows))
}

/// The instruments the account holds a quantity of, sorted by symbol (`_close_plan`'s `held`).
pub fn held_instruments(state: &AccountState) -> R<Vec<Instrument>> {
    let mut held: Vec<(String, Instrument)> = Vec::new();
    for position in state.positions.values() {
        if eod::nonzero(&ops::s(&position.quantity))? {
            held.push((position.instrument.symbol()?, position.instrument.clone()));
        }
    }
    held.sort_by(|a, b| a.0.cmp(&b.0));
    Ok(held.into_iter().map(|(_, instrument)| instrument).collect())
}

/// The option positions on `underlying` that the account holds, sorted by OCC (`_held_contracts`).
pub fn held_contracts(state: &AccountState, underlying: &str) -> R<Vec<Instrument>> {
    let mut held: Vec<(String, Instrument)> = Vec::new();
    for position in state.positions.values() {
        let Instrument::Option(contract) = &position.instrument else { continue };
        let nonzero = eod::nonzero(&ops::s(&position.quantity))?;
        if intraday::held(true, nonzero, &underlying_of(&position.instrument)?, underlying)? {
            held.push((contract.occ()?, position.instrument.clone()));
        }
    }
    held.sort_by(|a, b| a.0.cmp(&b.0));
    Ok(held.into_iter().map(|(_, instrument)| instrument).collect())
}

/// One position check per fresh snapshot (`_record_tick`). A command already in the ledger is
/// the same snapshot read twice, so it writes nothing. The venue's positions come in as
/// `(symbol, quantity)`. The VENUE_RECONCILE is written before any drift refusal, as Python did.
pub fn record_tick<L: Ledger, C: Clock>(
    l: &mut L,
    clock: &C,
    account: &str,
    cause: &str,
    as_of_iso: &str,
    underlying: &str,
    venue_name: &str,
    venue_positions: &[(String, Money)],
) -> R<()> {
    let command = format!("{cause}:tick:{as_of_iso}");
    if l.event_by_command(&command)?.is_some() {
        return Ok(());
    }
    let state = l.state(account)?;
    let mut ledger_side: std::collections::BTreeMap<String, Money> = std::collections::BTreeMap::new();
    for position in state.positions.values() {
        if eod::nonzero(&ops::s(&position.quantity))? {
            ledger_side.insert(position.instrument.symbol()?, position.quantity.clone());
        }
    }
    let mut venue_side: std::collections::BTreeMap<String, Money> = std::collections::BTreeMap::new();
    for (symbol, quantity) in venue_positions {
        if eod::nonzero(&ops::s(quantity))? {
            venue_side.insert(symbol.clone(), quantity.clone());
        }
    }
    let symbols: std::collections::BTreeSet<&String> = ledger_side.keys().chain(venue_side.keys()).collect();
    let mut rows: Vec<String> = Vec::with_capacity(symbols.len() * 3);
    for symbol in symbols {
        rows.push(symbol.clone());
        rows.push(ledger_side.get(symbol).map_or_else(|| "None".to_string(), ops::s));
        rows.push(venue_side.get(symbol).map_or_else(|| "None".to_string(), ops::s));
    }
    let drift = intraday::drift(&rows)?;
    let now = clock.now_utc();
    let reconcile = VenueReconcile {
        venue: venue_name.to_string(),
        as_of: now.clone(),
        reconciled: drift.is_empty(),
        drift: drift.clone(),
        note: Some(format!("{underlying} snapshot {as_of_iso}")),
    };
    l.append(vec![eod_glue::event(account, EventKind::VenueReconcile, Obj::Reconcile(reconcile), now, Some(command))])?;
    intraday::drift_refuse(account, &drift.join(", "))
}

/// `(entry_deadline, flat_deadline)` as te_core micros, early closes included (`_deadlines`).
/// The clock times are read in New York on `session`; the durations are raw microseconds.
pub fn deadlines(
    session: NaiveDate,
    flat_at: NaiveTime,
    entry_end: NaiveTime,
    flat_before_close: i64,
    entry_before_close: i64,
) -> R<(i64, i64)> {
    let close = ts_micros(&sim::session_close(session)?);
    let flat = wall_micros(session, flat_at);
    let entry = wall_micros(session, entry_end);
    let (entry_index, flat_index) = intraday::deadlines(flat, entry, close, flat_before_close, entry_before_close)?;
    let flat_deadline = if flat_index == 0 { flat } else { close - flat_before_close };
    let entry_deadline = match entry_index {
        0 => entry,
        1 => close - entry_before_close,
        _ => flat_deadline,
    };
    Ok((entry_deadline, flat_deadline))
}

/// The New York wall-clock `HH:MM` of a flat deadline, for the flatten reason (`strftime("%H:%M")`).
pub fn flat_label(flat_micros: i64) -> R<String> {
    let secs = flat_micros.div_euclid(1_000_000);
    let us = flat_micros.rem_euclid(1_000_000) as u32;
    let unix = secs - 719_163 * 86_400;
    let Some(t) = Utc.timestamp_opt(unix, us * 1000).single() else {
        return err("overflow", "date value out of range");
    };
    Ok(calendar::utc_to_ny(t).format("%H:%M").to_string())
}

/// The close's writes (the ledger half of `intraday_close`). One MARK per priced symbol, in
/// symbol order, each skipped if its command is already present; then the session's EOD_RUN.
/// Each event is its own append, as in Python.
pub fn append_close<L: Ledger>(
    l: &mut L,
    now: &DateTime,
    account: &str,
    session: NaiveDate,
    marks: &[(Instrument, Money, String)],
    job_name: &str,
    cause: &str,
    bars_processed: i128,
) -> R<()> {
    let mut ordered: Vec<(String, &Instrument, &Money, &String)> = Vec::with_capacity(marks.len());
    for (instrument, price, source) in marks {
        ordered.push((instrument.symbol()?, instrument, price, source));
    }
    ordered.sort_by(|a, b| a.0.cmp(&b.0));
    for (symbol, instrument, price, source) in ordered {
        let command = format!("intraday:mark:{account}:{}:{symbol}", eod_glue::iso(session));
        if l.event_by_command(&command)?.is_some() {
            continue;
        }
        let mark = Mark {
            instrument: instrument.clone(),
            price: price.clone(),
            as_of: now.clone(),
            source: Some(source.clone()),
        };
        l.append(vec![eod_glue::event(account, EventKind::Mark, Obj::Mark(mark), now.clone(), Some(command))])?;
    }
    let run = EodRun {
        session,
        job: format!("intraday:{job_name}"),
        account_id: account.to_string(),
        bars_processed,
        at_close: now.clone(),
    };
    l.append(vec![eod_glue::event(account, EventKind::EodRun, Obj::EodRun(run), now.clone(), Some(cause.to_string()))])?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::ledger::model::{parse_datetime, LErr};
    use crate::runtime::eod_glue::tests::{FixedClock, MemLedger};

    fn at(s: &str) -> DateTime {
        parse_datetime(s).expect("iso").0
    }

    fn day(s: &str) -> NaiveDate {
        NaiveDate::parse_from_str(s, "%Y-%m-%d").expect("date")
    }

    fn money(s: &str) -> Money {
        Money::parse(s).expect("decimal")
    }

    fn clock() -> FixedClock {
        FixedClock(at("2026-11-24T14:00:00+00:00"))
    }

    const JOB: &str = "job";
    const EOD_JOB: &str = "eodjob";
    const ACCOUNT: &str = "acct";

    #[test]
    fn commands_match_the_python_strings() {
        assert_eq!(cause(JOB, ACCOUNT, day("2026-11-24")).unwrap(), "intraday:job:acct:2026-11-24");
        assert_eq!(eod_marker(EOD_JOB, ACCOUNT, day("2026-11-24")).unwrap(), "eod:eodjob:acct:2026-11-24");
    }

    #[test]
    fn settled_follows_the_eod_marker_and_has_history_follows_any_run() {
        let mut l = MemLedger::default();
        let session = day("2026-11-24");
        assert!(!settled(&l, EOD_JOB, ACCOUNT, session).unwrap());
        assert!(!has_history(&l, ACCOUNT).unwrap());
        let marker = eod_marker(EOD_JOB, ACCOUNT, session).unwrap();
        eod_glue::append_run_marker(&mut l, &clock(), EOD_JOB, ACCOUNT, session, 0, Some(&marker)).unwrap();
        assert!(settled(&l, EOD_JOB, ACCOUNT, session).unwrap());
        assert!(has_history(&l, ACCOUNT).unwrap());
    }

    #[test]
    fn snapshot_count_counts_each_tick_once() {
        let mut l = MemLedger::default();
        let c = clock();
        let cause = cause(JOB, ACCOUNT, day("2026-11-24")).unwrap();
        assert_eq!(snapshot_count(&l, ACCOUNT, &cause).unwrap(), 0);
        record_tick(&mut l, &c, ACCOUNT, &cause, "2026-11-24T14:00:00+00:00", "SPX", "Venue", &[]).unwrap();
        record_tick(&mut l, &c, ACCOUNT, &cause, "2026-11-24T14:00:05+00:00", "SPX", "Venue", &[]).unwrap();
        // The same snapshot read again is one tick, not two.
        record_tick(&mut l, &c, ACCOUNT, &cause, "2026-11-24T14:00:05+00:00", "SPX", "Venue", &[]).unwrap();
        assert_eq!(snapshot_count(&l, ACCOUNT, &cause).unwrap(), 2);
    }

    #[test]
    fn record_tick_writes_the_reconcile_then_refuses_drift() {
        let mut l = MemLedger::default();
        let c = clock();
        let cause = cause(JOB, ACCOUNT, day("2026-11-24")).unwrap();
        // The venue holds 2 of a symbol the ledger does not: drift, refused after the write.
        let venue = vec![("SPXW".to_string(), money("2"))];
        let refused = record_tick(&mut l, &c, ACCOUNT, &cause, "2026-11-24T14:00:00+00:00", "SPX", "Venue", &venue)
            .unwrap_err();
        assert_eq!(refused.kind, "runtime_intraday");
        assert_eq!(
            refused.msg,
            "The venue's positions disagree with the ledger for 'acct' on SPXW; refusing to trade on either (I11)"
        );
        let written = l.event_by_command(&format!("{cause}:tick:2026-11-24T14:00:00+00:00")).unwrap().expect("the reconcile is written");
        match written.payload {
            Obj::Reconcile(r) => {
                assert!(!r.reconciled);
                assert_eq!(r.drift, vec!["SPXW".to_string()]);
                assert_eq!(r.note.as_deref(), Some("SPX snapshot 2026-11-24T14:00:00+00:00"));
            }
            other => panic!("expected a VenueReconcile, got {other:?}"),
        }
    }

    #[test]
    fn record_tick_with_nothing_on_either_side_is_clean() {
        let mut l = MemLedger::default();
        let cause = cause(JOB, ACCOUNT, day("2026-11-24")).unwrap();
        record_tick(&mut l, &clock(), ACCOUNT, &cause, "2026-11-24T14:00:00+00:00", "SPX", "Venue", &[]).unwrap();
        let written = l.event_by_command(&format!("{cause}:tick:2026-11-24T14:00:00+00:00")).unwrap().expect("written");
        match written.payload {
            Obj::Reconcile(r) => assert!(r.reconciled && r.drift.is_empty()),
            other => panic!("expected a VenueReconcile, got {other:?}"),
        }
    }

    #[test]
    fn deadlines_on_a_full_day_and_pulled_in_on_an_early_close() {
        let flat_at = NaiveTime::from_hms_opt(15, 30, 0).unwrap();
        let entry_end = NaiveTime::from_hms_opt(12, 0, 0).unwrap();
        let thirty = 30 * 60 * 1_000_000;
        let ninety = 90 * 60 * 1_000_000;
        // A full day: the entry ends at 12:00 ET and the sweep at 15:30 ET.
        let full = day("2026-11-24");
        let (entry, flat) = deadlines(full, flat_at, entry_end, thirty, ninety).unwrap();
        assert_eq!(entry, utc_micros(ny_time_to_utc(full, entry_end)));
        assert_eq!(flat, utc_micros(ny_time_to_utc(full, flat_at)));
        // An early close (13:00 ET on 2026-11-27): the sweep is 12:30 and entries end at 11:30.
        let early = day("2026-11-27");
        let (entry, flat) = deadlines(early, flat_at, entry_end, thirty, ninety).unwrap();
        assert_eq!(flat, utc_micros(ny_time_to_utc(early, NaiveTime::from_hms_opt(12, 30, 0).unwrap())));
        assert_eq!(entry, utc_micros(ny_time_to_utc(early, NaiveTime::from_hms_opt(11, 30, 0).unwrap())));
        assert_eq!(flat_label(flat).unwrap(), "12:30");
    }

    #[test]
    fn append_close_writes_marks_in_symbol_order_then_the_run() {
        let mut l = MemLedger::default();
        let session = day("2026-11-24");
        let now = at("2026-11-24T21:00:00+00:00");
        let cause = cause(JOB, ACCOUNT, session).unwrap();
        let marks = vec![
            (Instrument::Equity("SPX".into()), money("6000"), "snapshot:2026-11-24T20:59:00+00:00".to_string()),
            (Instrument::Equity("AAA".into()), money("5"), "snapshot:2026-11-24T20:59:00+00:00".to_string()),
        ];
        append_close(&mut l, &now, ACCOUNT, session, &marks, JOB, &cause, 7).unwrap();
        let mark = l.event_by_command("intraday:mark:acct:2026-11-24:SPX").unwrap().expect("SPX marked");
        assert!(matches!(mark.payload, Obj::Mark(_)));
        assert!(l.event_by_command("intraday:mark:acct:2026-11-24:AAA").unwrap().is_some());
        let run = l.event_by_command(&cause).unwrap().expect("the run marker");
        match run.payload {
            Obj::EodRun(r) => {
                assert_eq!(r.job, "intraday:job");
                assert_eq!(r.bars_processed, 7);
                assert_eq!(r.session, session);
            }
            other => panic!("expected an EodRun, got {other:?}"),
        }
        // A second close writes no duplicate marks (the commands are already there).
        append_close(&mut l, &now, ACCOUNT, session, &marks, JOB, &cause, 7).unwrap_err();
    }

    #[test]
    fn err_kind_is_value_for_a_bad_overflow_is_not_hit_by_flat_label() {
        let refused: LErr = flat_label(i64::MAX).unwrap_err();
        assert_eq!(refused.kind, "overflow");
    }
}

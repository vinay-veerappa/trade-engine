//! The 1m data oracle (P6C): our own bar history checked against the Globex calendars, per root.
//!
//! Fixtures `fixtures/calendar_oracle_<ROOT>.json` are built by `tools/calendar_data_oracle.py` from the bar store
//! (read only; the header carries the store path, the file list and each file's SHA-256). A fixture holds "islands"
//! of bars, not verdicts, so a calendar change never needs a new fixture. Bars are OPEN-stamped: the bar stamped `t`
//! covers `[t, t+60s)`, so an island `[first, last]` occupies `[first, last+60s)`; a bar stamped at the session close
//! minute is already outside the session (strict).
//!
//! Per root, over the calendar's own range, the findings are:
//! - `closed`      bars on a trade date the table calls closed (check a);
//! - `after-halt`  bars after an early halt on its date (check b);
//! - `daily-halt`  bars inside the 17:00-18:00 ET halt (check b);
//! - `before-open` bars before a session's open (a late reopen), the other side of check (b);
//! - `outside`     bars outside every session otherwise, e.g. weekends (check d);
//! - `no-bars`     a session the calendar opens that has no bars at all (check c);
//! - `ends-early`  an early-halt session whose last bar ends more than 5 minutes before the halt (check e);
//! - `in-halt`     bars inside an intra-session halt (the 16:15-16:30 ET equity halt of the dated eras), one minute of
//!                 tolerance at each edge;
//! - `unmodelled-halt` a session the calendar runs through 16:15-16:30 ET whose data has bars on both sides and
//!                 none inside (a halt the eras do not know);
//! - `friday-1615` a regular Friday session whose data ends 16:15 ET (a one-off; the equity Friday 16:15 close is an
//!                 era, `EQUITY_ERAS`, not an allow-list entry);
//! - `unlisted-halt` a date with no table row whose data ends at 13:00 ET (a halt the table does not know);
//! - `short-data`  any other session whose last bar ends more than 5 minutes before the close (thin data, store
//!   gap). This is counted and printed, never failed: a thin or truncated session is not a calendar claim.
//!
//! Every deviation is explicitly allow-listed with a one-line reason. The test fails on any unlisted finding and on
//! any allow-list entry that no longer matches a finding. Set `CAL_ORACLE_DUMP=1` to print every finding.

use std::collections::BTreeMap;

use chrono::{DateTime, Datelike, Duration, NaiveDate, NaiveTime, Utc};
use te_core::calendar::globex::{GlobexCalendar, HolidayStatus};

const ENDS_EARLY_MIN: i64 = 5;

struct Fixture {
    root: &'static str,
    json: &'static str,
}

const FIXTURES: &[Fixture] = &[
    Fixture { root: "ES", json: include_str!("fixtures/calendar_oracle_ES.json") },
    Fixture { root: "NQ", json: include_str!("fixtures/calendar_oracle_NQ.json") },
    Fixture { root: "YM", json: include_str!("fixtures/calendar_oracle_YM.json") },
    Fixture { root: "RTY", json: include_str!("fixtures/calendar_oracle_RTY.json") },
    Fixture { root: "CL", json: include_str!("fixtures/calendar_oracle_CL.json") },
    Fixture { root: "GC", json: include_str!("fixtures/calendar_oracle_GC.json") },
];

#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord)]
struct Finding {
    kind: &'static str,
    date: NaiveDate,
    detail: String,
}

struct Allow {
    root: &'static str,
    kind: &'static str,
    from: (i32, u32, u32),
    to: (i32, u32, u32),
    reason: &'static str,
}

fn ymd(t: (i32, u32, u32)) -> NaiveDate {
    NaiveDate::from_ymd_opt(t.0, t.1, t.2).unwrap()
}

struct Loaded {
    islands: Vec<(i64, i64)>, // [start, end) in epoch seconds, end = last + 60
    bars: u64,
    first_bar: i64,
    last_bar_end: i64,
}

fn load(json: &str) -> Loaded {
    let v: serde_json::Value = serde_json::from_str(json).unwrap();
    let mut prev_last = 0i64;
    let mut islands = Vec::new();
    for e in v["islands"].as_array().unwrap() {
        let a = e.as_array().unwrap();
        let first = prev_last + a[0].as_i64().unwrap();
        let last = first + a[1].as_i64().unwrap();
        islands.push((first, last + 60));
        prev_last = last;
    }
    let bars = v["header"]["bars"].as_u64().unwrap();
    let first_bar = islands[0].0;
    let last_bar_end = islands.last().unwrap().1;
    Loaded { islands, bars, first_bar, last_bar_end }
}

fn utc(s: i64) -> DateTime<Utc> {
    DateTime::from_timestamp(s, 0).unwrap()
}

/// Classify a stretch of bars that lies outside every session, starting at `s`.
fn classify(cal: &GlobexCalendar, s: i64) -> (&'static str, NaiveDate) {
    use chrono_tz::America::New_York;
    let ny = utc(s).with_timezone(&New_York);
    let tod = ny.time();
    let in_halt = tod >= NaiveTime::from_hms_opt(17, 0, 0).unwrap() && tod < NaiveTime::from_hms_opt(18, 0, 0).unwrap();
    let td = if tod >= NaiveTime::from_hms_opt(18, 0, 0).unwrap() {
        ny.date_naive() + Duration::days(1)
    } else {
        ny.date_naive()
    };
    if in_halt {
        return ("daily-halt", ny.date_naive());
    }
    let table = te_core::calendar::globex::table_for(cal.group());
    let weekday = !matches!(td.weekday(), chrono::Weekday::Sat | chrono::Weekday::Sun);
    if weekday {
        if let Some(row) = table.holidays.get(&td) {
            match row.status {
                HolidayStatus::Closed => return ("closed", td),
                HolidayStatus::EarlyHalt => {
                    if let Ok(close) = cal.session_close(td) {
                        if utc(s) >= close {
                            return ("after-halt", td);
                        }
                    }
                }
            }
        }
        if let Ok(open) = cal.session_open(td) {
            if utc(s) < open {
                return ("before-open", td);
            }
        }
    }
    ("outside", td)
}

fn findings_for(root: &'static str, fx: &Loaded) -> Vec<Finding> {
    let cal = GlobexCalendar::for_root(root).unwrap();
    let dates = cal.sessions_in_range(cal.first_date(), cal.last_date()).unwrap();
    let sessions: Vec<(NaiveDate, i64, i64)> = dates
        .iter()
        .map(|&d| (d, cal.session_open(d).unwrap().timestamp(), cal.session_close(d).unwrap().timestamp()))
        .collect();
    // The spans where bars are legal: a session, minus its intra-session halt, which is shrunk by one minute at each
    // edge (a bar stamped at the halt start or at its last minute is a stamp artifact, not a calendar claim).
    // (trade date, open, close, true if this span follows the halt of the same session)
    let mut segs: Vec<(NaiveDate, i64, i64, bool)> = Vec::with_capacity(sessions.len() + 8);
    for &(d, open, close) in &sessions {
        match cal.session_halt(d) {
            Some((hs, he)) => {
                segs.push((d, open, hs.timestamp() + 60, false));
                segs.push((d, he.timestamp() - 60, close, true));
            }
            // A bar stamped at the era's Friday 16:15 close is a closing-print stamp artifact, tolerated like the halt edges.
            None if d.weekday() == chrono::Weekday::Fri && close == hour_ts(d, 16, 15) => segs.push((d, open, close + 60, false)),
            None => segs.push((d, open, close, false)),
        }
    }
    let range_lo = ymd_start(&cal);
    let range_hi = ymd_end(&cal);
    let mut out = Vec::new();

    // (a), (b), (d): the parts of each island that lie outside every session.
    for &(a, b) in &fx.islands {
        let (a, b) = (a.max(range_lo), b.min(range_hi));
        if a >= b {
            continue;
        }
        let mut cur = a;
        let mut i = segs.partition_point(|s| s.2 <= a);
        while cur < b {
            if i < segs.len() && segs[i].1 < b {
                let (sd, open, close, after_halt) = segs[i];
                if cur < open {
                    if after_halt && i > 0 && cur >= segs[i - 1].2 {
                        push_halt(&mut out, sd, cur, open.min(b));
                    } else {
                        push_frag(&cal, &mut out, cur, open.min(b));
                    }
                }
                cur = close.max(cur);
                i += 1;
            } else {
                // the rest of the island ends before the next span: inside a halt gap, or plain outside
                if i < segs.len() && segs[i].3 && i > 0 && cur >= segs[i - 1].2 {
                    push_halt(&mut out, segs[i].0, cur, b);
                } else {
                    push_frag(&cal, &mut out, cur, b);
                }
                cur = b;
            }
        }
    }

    // (c), (e): per session.
    for &(d, open, close) in &sessions {
        if open < fx.first_bar || close > fx.last_bar_end {
            continue;
        }
        let lo = fx.islands.partition_point(|x| x.1 <= open);
        let mut last_end: Option<i64> = None;
        let mut k = lo;
        while k < fx.islands.len() && fx.islands[k].0 < close {
            last_end = Some(fx.islands[k].1.min(close));
            k += 1;
        }
        // (f) a session the calendar runs straight through 16:15-16:30 ET whose data has the halt: bars on both sides,
        // none inside. Only for sessions that span the whole 16:00-16:45 ET stretch.
        if cal.session_halt(d).is_none() && cal.is_early_halt(d).unwrap() == false {
            let (w0, w1) = (hour_ts(d, 16, 15), hour_ts(d, 16, 30));
            if open <= hour_ts(d, 16, 0) && close >= hour_ts(d, 16, 45) {
                let n = |a: i64, b: i64| -> i64 {
                    let mut c = 0;
                    let mut k = fx.islands.partition_point(|x| x.1 <= a);
                    while k < fx.islands.len() && fx.islands[k].0 < b {
                        c += (fx.islands[k].1.min(b) - fx.islands[k].0.max(a)) / 60;
                        k += 1;
                    }
                    c
                };
                if n(hour_ts(d, 16, 0), w0) >= 8 && n(w1, hour_ts(d, 16, 45)) >= 8 && n(w0, w1) == 0 {
                    out.push(Finding { kind: "unmodelled-halt", date: d, detail: "bars before and after 16:15-16:30 ET, none inside".into() });
                }
            }
        }
        match last_end {
            None => out.push(Finding { kind: "no-bars", date: d, detail: format!("{} .. {}", utc(open), utc(close)) }),
            Some(e) if e < close - ENDS_EARLY_MIN * 60 => {
                use chrono_tz::America::New_York;
                let end_et = utc(e).with_timezone(&New_York);
                let t = end_et.time();
                let kind = if cal.is_early_halt(d).unwrap() {
                    "ends-early"
                } else if d.weekday() == chrono::Weekday::Fri
                    && t >= NaiveTime::from_hms_opt(16, 14, 0).unwrap()
                    && t <= NaiveTime::from_hms_opt(16, 17, 0).unwrap()
                {
                    "friday-1615"
                } else if te_core::calendar::globex::table_for(cal.group()).holidays.get(&d).is_none()
                    && t >= NaiveTime::from_hms_opt(13, 0, 0).unwrap()
                    && t <= NaiveTime::from_hms_opt(13, 1, 0).unwrap()
                {
                    "unlisted-halt"
                } else {
                    "short-data"
                };
                out.push(Finding {
                    kind,
                    date: d,
                    detail: format!("last bar ends {} but the session closes {}", utc(e), utc(close)),
                })
            }
            _ => {}
        }
    }
    out.sort();
    out
}

fn hour_ts(d: NaiveDate, h: u32, m: u32) -> i64 {
    use chrono::TimeZone;
    chrono_tz::America::New_York
        .from_local_datetime(&d.and_hms_opt(h, m, 0).unwrap())
        .single()
        .unwrap()
        .timestamp()
}

fn ny_midnight(d: NaiveDate) -> i64 {
    use chrono::TimeZone;
    chrono_tz::America::New_York
        .from_local_datetime(&d.and_hms_opt(0, 0, 0).unwrap())
        .single()
        .unwrap()
        .timestamp()
}

fn ymd_start(cal: &GlobexCalendar) -> i64 {
    ny_midnight(cal.first_date())
}

fn ymd_end(cal: &GlobexCalendar) -> i64 {
    ny_midnight(cal.last_date() + Duration::days(1))
}

fn push_halt(out: &mut Vec<Finding>, d: NaiveDate, s: i64, e: i64) {
    out.push(Finding { kind: "in-halt", date: d, detail: format!("{} .. {} ({} min)", utc(s), utc(e), (e - s) / 60) });
}

fn push_frag(cal: &GlobexCalendar, out: &mut Vec<Finding>, s: i64, e: i64) {
    let (kind, date) = classify(cal, s);
    out.push(Finding { kind, date, detail: format!("{} .. {} ({} min)", utc(s), utc(e), (e - s) / 60) });
}

fn matches(a: &Allow, root: &str, f: &Finding) -> bool {
    a.root == root && a.kind == f.kind && f.date >= ymd(a.from) && f.date <= ymd(a.to)
}

#[test]
fn data_oracle_per_root() {
    let dump = std::env::var("CAL_ORACLE_DUMP").is_ok();
    let mut failures = Vec::new();
    for fx in FIXTURES {
        let loaded = load(fx.json);
        let findings = findings_for(fx.root, &loaded);
        let cal = GlobexCalendar::for_root(fx.root).unwrap();
        let sessions = cal.sessions_in_range(cal.first_date(), cal.last_date()).unwrap().len();
        let mut used = vec![false; ALLOW.len()];
        let mut by_kind: BTreeMap<&str, usize> = BTreeMap::new();
        for f in &findings {
            if dump {
                println!("FINDING {} {} {} {}", fx.root, f.kind, f.date, f.detail);
            }
            let mut hit = false;
            for (i, a) in ALLOW.iter().enumerate() {
                if matches(a, fx.root, f) {
                    used[i] = true;
                    hit = true;
                }
            }
            if f.kind == "short-data" {
                *by_kind.entry(f.kind).or_default() += 1;
                continue;
            }
            if hit {
                *by_kind.entry(f.kind).or_default() += 1;
            } else {
                failures.push(format!("{}: unlisted {} on {}: {}", fx.root, f.kind, f.date, f.detail));
            }
        }
        for (i, a) in ALLOW.iter().enumerate() {
            if a.root == fx.root && !used[i] {
                failures.push(format!("{}: stale allow-list entry {} {:?}..{:?}: {}", fx.root, a.kind, a.from, a.to, a.reason));
            }
        }
        let entries = ALLOW.iter().filter(|a| a.root == fx.root).count();
        println!(
            "{}: range {}..{}, {} sessions checked, {} bars, {} findings ({} allow-listed entries) by kind {:?}",
            fx.root,
            cal.first_date(),
            cal.last_date(),
            sessions,
            loaded.bars,
            findings.len(),
            entries,
            by_kind
        );
    }
    assert!(failures.is_empty(), "{} calendar/data deviations:\n{}", failures.len(), failures.join("\n"));
}

/// The per-root allow-list. Each entry: root, kind, trade-date range, one-line reason.
static ALLOW: &[Allow] = &[
    Allow { root: "CL", kind: "after-halt", from: (2010, 2, 15), to: (2010, 2, 15), reason: "one-minute stamp artifact: a bar stamped at the halt minute" },
    Allow { root: "CL", kind: "after-halt", from: (2014, 12, 24), to: (2014, 12, 24), reason: "one-minute stamp artifact: a bar stamped at the halt minute" },
    Allow { root: "CL", kind: "no-bars", from: (2013, 7, 12), to: (2013, 7, 12), reason: "store gap: the session has no bars in the store" },
    Allow { root: "CL", kind: "no-bars", from: (2014, 1, 27), to: (2014, 1, 31), reason: "store gap: the session has no bars in the store" },
    Allow { root: "ES", kind: "after-halt", from: (2006, 7, 3), to: (2006, 7, 3), reason: "July 3 early halt: bars resume about 16:30 ET (reopen unstated in the CME release/PDF)" },
    Allow { root: "ES", kind: "after-halt", from: (2007, 7, 3), to: (2007, 7, 3), reason: "July 3 early halt: bars resume about 16:30 ET (reopen unstated in the CME release/PDF)" },
    Allow { root: "ES", kind: "after-halt", from: (2008, 7, 3), to: (2008, 7, 3), reason: "July 3 early halt: bars resume about 16:30 ET (reopen unstated in the CME release/PDF)" },
    Allow { root: "ES", kind: "after-halt", from: (2009, 9, 7), to: (2009, 9, 7), reason: "one-minute stamp artifact: a bar stamped at the halt minute" },
    Allow { root: "ES", kind: "after-halt", from: (2009, 11, 27), to: (2009, 11, 27), reason: "one-minute stamp artifact: a bar stamped at the halt minute" },
    Allow { root: "ES", kind: "after-halt", from: (2009, 12, 24), to: (2009, 12, 24), reason: "one-minute stamp artifact: a bar stamped at the halt minute" },
    Allow { root: "ES", kind: "after-halt", from: (2012, 7, 3), to: (2012, 7, 3), reason: "one-minute stamp artifact: a bar stamped at the halt minute" },
    Allow { root: "ES", kind: "after-halt", from: (2014, 2, 17), to: (2014, 2, 17), reason: "one-minute stamp artifact: a bar stamped at the halt minute" },
    Allow { root: "ES", kind: "after-halt", from: (2014, 12, 24), to: (2014, 12, 24), reason: "one-minute stamp artifact: a bar stamped at the halt minute" },
    Allow { root: "ES", kind: "after-halt", from: (2015, 7, 2), to: (2015, 7, 2), reason: "CME 2015 July-4 PDF says early close 13:15 ET, but the data trade to 17:00 ET (unresolved source conflict)" },
    Allow { root: "ES", kind: "before-open", from: (2007, 12, 26), to: (2007, 12, 26), reason: "one-minute stamp artifact: a bar stamped the minute before the reopen" },
    Allow { root: "ES", kind: "before-open", from: (2008, 1, 2), to: (2008, 1, 2), reason: "one-minute stamp artifact: a bar stamped the minute before the reopen" },
    Allow { root: "ES", kind: "before-open", from: (2008, 12, 26), to: (2008, 12, 26), reason: "one-minute stamp artifact: a bar stamped the minute before the reopen" },
    Allow { root: "ES", kind: "before-open", from: (2009, 1, 2), to: (2009, 1, 2), reason: "one-minute stamp artifact: a bar stamped the minute before the reopen" },
    Allow { root: "ES", kind: "closed", from: (2007, 1, 2), to: (2007, 1, 2), reason: "Ford day of mourning 2007-01-02: bars to 09:15 ET before the all-day closure; table follows the CME release (closed)" },
    Allow { root: "ES", kind: "closed", from: (2007, 1, 2), to: (2007, 1, 2), reason: "Ford day of mourning 2007-01-02: bars to 09:15 ET before the all-day closure; table follows the CME release (closed)" },
    Allow { root: "ES", kind: "no-bars", from: (2006, 2, 6), to: (2006, 2, 6), reason: "store gap: the session has no bars in the store" },
    Allow { root: "ES", kind: "no-bars", from: (2006, 3, 6), to: (2006, 3, 6), reason: "store gap: the session has no bars in the store" },
    Allow { root: "ES", kind: "no-bars", from: (2014, 1, 27), to: (2014, 1, 31), reason: "store gap: the session has no bars in the store" },
    Allow { root: "ES", kind: "unlisted-halt", from: (2023, 1, 16), to: (2023, 1, 16), reason: "equity table has no row for this date (never sourced, see cme_equity_holidays.md gaps); data halts 13:00 ET" },
    Allow { root: "ES", kind: "unlisted-halt", from: (2023, 2, 20), to: (2023, 2, 20), reason: "equity table has no row for this date (never sourced, see cme_equity_holidays.md gaps); data halts 13:00 ET" },
    Allow { root: "GC", kind: "no-bars", from: (2009, 8, 31), to: (2009, 10, 12), reason: "store gap: the session has no bars in the store" },
    Allow { root: "GC", kind: "no-bars", from: (2013, 7, 12), to: (2013, 7, 12), reason: "store gap: the session has no bars in the store" },
    Allow { root: "GC", kind: "no-bars", from: (2014, 1, 27), to: (2014, 1, 30), reason: "store gap: the session has no bars in the store" },
    Allow { root: "GC", kind: "no-bars", from: (2015, 10, 12), to: (2015, 10, 16), reason: "store gap: the session has no bars in the store" },
    Allow { root: "GC", kind: "no-bars", from: (2023, 4, 6), to: (2023, 4, 14), reason: "store gap: no bars for 2023-04-06..14 in any file of the root" },
    Allow { root: "NQ", kind: "after-halt", from: (2006, 7, 3), to: (2006, 7, 3), reason: "July 3 early halt: bars resume about 16:30 ET (reopen unstated in the CME release/PDF)" },
    Allow { root: "NQ", kind: "after-halt", from: (2007, 7, 3), to: (2007, 7, 3), reason: "July 3 early halt: bars resume about 16:30 ET (reopen unstated in the CME release/PDF)" },
    Allow { root: "NQ", kind: "after-halt", from: (2008, 7, 3), to: (2008, 7, 3), reason: "July 3 early halt: bars resume about 16:30 ET (reopen unstated in the CME release/PDF)" },
    Allow { root: "NQ", kind: "after-halt", from: (2015, 7, 2), to: (2015, 7, 2), reason: "CME 2015 July-4 PDF says early close 13:15 ET, but the data trade to 17:00 ET (unresolved source conflict)" },
    Allow { root: "NQ", kind: "before-open", from: (2007, 12, 26), to: (2007, 12, 26), reason: "one-minute stamp artifact: a bar stamped the minute before the reopen" },
    Allow { root: "NQ", kind: "before-open", from: (2008, 1, 2), to: (2008, 1, 2), reason: "one-minute stamp artifact: a bar stamped the minute before the reopen" },
    Allow { root: "NQ", kind: "before-open", from: (2008, 12, 26), to: (2008, 12, 26), reason: "one-minute stamp artifact: a bar stamped the minute before the reopen" },
    Allow { root: "NQ", kind: "before-open", from: (2009, 1, 2), to: (2009, 1, 2), reason: "one-minute stamp artifact: a bar stamped the minute before the reopen" },
    Allow { root: "NQ", kind: "closed", from: (2007, 1, 2), to: (2007, 1, 2), reason: "Ford day of mourning 2007-01-02: bars to 09:15 ET before the all-day closure; table follows the CME release (closed)" },
    Allow { root: "NQ", kind: "closed", from: (2007, 1, 2), to: (2007, 1, 2), reason: "Ford day of mourning 2007-01-02: bars to 09:15 ET before the all-day closure; table follows the CME release (closed)" },
    Allow { root: "NQ", kind: "no-bars", from: (2006, 2, 6), to: (2006, 2, 6), reason: "store gap: the session has no bars in the store" },
    Allow { root: "NQ", kind: "no-bars", from: (2006, 3, 6), to: (2006, 3, 6), reason: "store gap: the session has no bars in the store" },
    Allow { root: "NQ", kind: "no-bars", from: (2013, 7, 12), to: (2013, 7, 12), reason: "store gap: the session has no bars in the store" },
    Allow { root: "NQ", kind: "no-bars", from: (2014, 1, 27), to: (2014, 1, 31), reason: "store gap: the session has no bars in the store" },
    Allow { root: "NQ", kind: "unlisted-halt", from: (2023, 1, 16), to: (2023, 1, 16), reason: "equity table has no row for this date (never sourced, see cme_equity_holidays.md gaps); data halts 13:00 ET" },
    Allow { root: "NQ", kind: "unlisted-halt", from: (2023, 2, 20), to: (2023, 2, 20), reason: "equity table has no row for this date (never sourced, see cme_equity_holidays.md gaps); data halts 13:00 ET" },
    Allow { root: "RTY", kind: "no-bars", from: (2023, 4, 6), to: (2023, 4, 14), reason: "store gap: no bars for 2023-04-06..14 in any file of the root" },
    Allow { root: "RTY", kind: "unlisted-halt", from: (2023, 1, 16), to: (2023, 1, 16), reason: "equity table has no row for this date (never sourced, see cme_equity_holidays.md gaps); data halts 13:00 ET" },
    Allow { root: "RTY", kind: "unlisted-halt", from: (2023, 2, 20), to: (2023, 2, 20), reason: "equity table has no row for this date (never sourced, see cme_equity_holidays.md gaps); data halts 13:00 ET" },
    Allow { root: "YM", kind: "after-halt", from: (2008, 7, 3), to: (2008, 7, 3), reason: "July 3 early halt: bars resume about 16:30 ET (reopen unstated in the CME release/PDF)" },
    Allow { root: "YM", kind: "after-halt", from: (2015, 7, 2), to: (2015, 7, 2), reason: "CME 2015 July-4 PDF says early close 13:15 ET, but the data trade to 17:00 ET (unresolved source conflict)" },
    Allow { root: "YM", kind: "before-open", from: (2008, 12, 26), to: (2008, 12, 26), reason: "one-minute stamp artifact: a bar stamped the minute before the reopen" },
    Allow { root: "YM", kind: "no-bars", from: (2013, 7, 12), to: (2013, 7, 12), reason: "store gap: the session has no bars in the store" },
    Allow { root: "YM", kind: "no-bars", from: (2014, 1, 27), to: (2014, 1, 31), reason: "store gap: the session has no bars in the store" },
    Allow { root: "YM", kind: "no-bars", from: (2023, 4, 6), to: (2023, 4, 14), reason: "store gap: no bars for 2023-04-06..14 in any file of the root" },
    Allow { root: "YM", kind: "unlisted-halt", from: (2023, 1, 16), to: (2023, 1, 16), reason: "equity table has no row for this date (never sourced, see cme_equity_holidays.md gaps); data halts 13:00 ET" },
    Allow { root: "YM", kind: "unlisted-halt", from: (2023, 2, 20), to: (2023, 2, 20), reason: "equity table has no row for this date (never sourced, see cme_equity_holidays.md gaps); data halts 13:00 ET" },
    Allow { root: "ES", kind: "friday-1615", from: (2016, 10, 28), to: (2016, 10, 28), reason: "OBSERVED one-off: Friday 2016-10-28 data of ES, NQ, YM, CL and GC all end 16:15-16:17 ET; no CME notice for it in cme_raw, not an era" },
    Allow { root: "NQ", kind: "friday-1615", from: (2016, 10, 28), to: (2016, 10, 28), reason: "OBSERVED one-off: Friday 2016-10-28 data of ES, NQ, YM, CL and GC all end 16:15-16:17 ET; no CME notice for it in cme_raw, not an era" },
    Allow { root: "YM", kind: "friday-1615", from: (2016, 10, 28), to: (2016, 10, 28), reason: "OBSERVED one-off: Friday 2016-10-28 data of ES, NQ, YM, CL and GC all end 16:15-16:17 ET; no CME notice for it in cme_raw, not an era" },
    Allow { root: "CL", kind: "friday-1615", from: (2016, 10, 28), to: (2016, 10, 28), reason: "OBSERVED one-off: Friday 2016-10-28 data of ES, NQ, YM, CL and GC all end 16:15-16:17 ET; no CME notice for it in cme_raw, not an era" },
    Allow { root: "GC", kind: "friday-1615", from: (2016, 10, 28), to: (2016, 10, 28), reason: "OBSERVED one-off: Friday 2016-10-28 data of ES, NQ, YM, CL and GC all end 16:15-16:17 ET; no CME notice for it in cme_raw, not an era" },
    Allow { root: "CL", kind: "friday-1615", from: (2009, 10, 9), to: (2009, 10, 9), reason: "OBSERVED one-off: Friday 2009-10-09 CL data end 16:15 ET; no CME notice in cme_raw, not an era" },
    Allow { root: "ES", kind: "in-halt", from: (2006, 4, 27), to: (2006, 5, 22), reason: "OBSERVED: no 16:15-16:30 ET halt in the data on these 2006 days (bars every minute); the halt returns 2006-05-23; cause not sourced" },
    Allow { root: "NQ", kind: "in-halt", from: (2006, 4, 27), to: (2006, 5, 22), reason: "OBSERVED: no 16:15-16:30 ET halt in the data on these 2006 days (bars every minute); the halt returns 2006-05-23; cause not sourced" },
    Allow { root: "ES", kind: "in-halt", from: (2020, 9, 10), to: (2020, 9, 11), reason: "OBSERVED: bars inside the 16:15-16:30 ET halt on 2020-09-10/11 (from 16:21 ET) and 2020-10-19..22 on all four equity roots; cause not found in cme_raw" },
    Allow { root: "ES", kind: "in-halt", from: (2020, 10, 19), to: (2020, 10, 22), reason: "OBSERVED: bars inside the 16:15-16:30 ET halt on 2020-09-10/11 (from 16:21 ET) and 2020-10-19..22 on all four equity roots; cause not found in cme_raw" },
    Allow { root: "NQ", kind: "in-halt", from: (2020, 9, 10), to: (2020, 9, 11), reason: "OBSERVED: bars inside the 16:15-16:30 ET halt on 2020-09-10/11 (from 16:21 ET) and 2020-10-19..22 on all four equity roots; cause not found in cme_raw" },
    Allow { root: "NQ", kind: "in-halt", from: (2020, 10, 19), to: (2020, 10, 22), reason: "OBSERVED: bars inside the 16:15-16:30 ET halt on 2020-09-10/11 (from 16:21 ET) and 2020-10-19..22 on all four equity roots; cause not found in cme_raw" },
    Allow { root: "YM", kind: "in-halt", from: (2020, 9, 10), to: (2020, 9, 11), reason: "OBSERVED: bars inside the 16:15-16:30 ET halt on 2020-09-10/11 (from 16:21 ET) and 2020-10-19..22 on all four equity roots; cause not found in cme_raw" },
    Allow { root: "YM", kind: "in-halt", from: (2020, 10, 19), to: (2020, 10, 22), reason: "OBSERVED: bars inside the 16:15-16:30 ET halt on 2020-09-10/11 (from 16:21 ET) and 2020-10-19..22 on all four equity roots; cause not found in cme_raw" },
    Allow { root: "RTY", kind: "in-halt", from: (2020, 9, 10), to: (2020, 9, 11), reason: "OBSERVED: bars inside the 16:15-16:30 ET halt on 2020-09-10/11 (from 16:21 ET) and 2020-10-19..22 on all four equity roots; cause not found in cme_raw" },
    Allow { root: "RTY", kind: "in-halt", from: (2020, 10, 19), to: (2020, 10, 22), reason: "OBSERVED: bars inside the 16:15-16:30 ET halt on 2020-09-10/11 (from 16:21 ET) and 2020-10-19..22 on all four equity roots; cause not found in cme_raw" },
    Allow { root: "ES", kind: "outside", from: (2007, 3, 16), to: (2007, 3, 16), reason: "one stray bar stamped after the era's Friday 16:15 ET close (a lone print); the calendar models the close, not stray prints" },
    Allow { root: "ES", kind: "outside", from: (2009, 10, 9), to: (2009, 10, 9), reason: "one stray bar stamped after the era's Friday 16:15 ET close (a lone print); the calendar models the close, not stray prints" },
    Allow { root: "ES", kind: "outside", from: (2009, 10, 16), to: (2009, 10, 16), reason: "one stray bar stamped after the era's Friday 16:15 ET close (a lone print); the calendar models the close, not stray prints" },
    Allow { root: "ES", kind: "outside", from: (2010, 4, 16), to: (2010, 4, 16), reason: "one stray bar stamped after the era's Friday 16:15 ET close (a lone print); the calendar models the close, not stray prints" },
    Allow { root: "NQ", kind: "outside", from: (2007, 3, 16), to: (2007, 3, 16), reason: "one stray bar stamped after the era's Friday 16:15 ET close (a lone print); the calendar models the close, not stray prints" },
    Allow { root: "NQ", kind: "outside", from: (2009, 10, 16), to: (2009, 10, 16), reason: "one stray bar stamped after the era's Friday 16:15 ET close (a lone print); the calendar models the close, not stray prints" },
    Allow { root: "NQ", kind: "outside", from: (2010, 4, 16), to: (2010, 4, 16), reason: "one stray bar stamped after the era's Friday 16:15 ET close (a lone print); the calendar models the close, not stray prints" },
    Allow { root: "YM", kind: "outside", from: (2009, 9, 11), to: (2009, 9, 11), reason: "one stray bar stamped after the era's Friday 16:15 ET close (a lone print); the calendar models the close, not stray prints" },
    Allow { root: "YM", kind: "outside", from: (2009, 10, 16), to: (2009, 10, 16), reason: "one stray bar stamped after the era's Friday 16:15 ET close (a lone print); the calendar models the close, not stray prints" },
    Allow { root: "YM", kind: "outside", from: (2010, 4, 16), to: (2010, 4, 16), reason: "one stray bar stamped after the era's Friday 16:15 ET close (a lone print); the calendar models the close, not stray prints" },
];

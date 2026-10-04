//! CME Globex equity index session calendar (NQ, MNQ, ES, MES), 2006 through 2027.
//!
//! # Semantics
//!
//! - **Normal week.** A session is identified by its trade date D, a weekday:
//!   - it opens at 18:00 ET on the previous calendar evening (Sunday 18:00 for Monday);
//!   - it closes at 17:00 ET on D;
//!   - there is a daily halt 17:00-18:00 ET, and the weekend runs from Friday 17:00 to Sunday 18:00.
//! - **`early_halt` on D.** D's session closes at `halt_et` instead of 17:00.
//!   - The next session opens at `reopen_et` if given (date-qualified if not the same day).
//!   - Otherwise it opens at the normal 18:00 ET on the evening before the next trade date.
//!   - Example, MLK Monday: Sunday 18:00 to Monday 13:00, then Monday 18:00 opens Tuesday's session.
//! - **`closed` on D.** There is no session with trade date D, and the previous session closes normally.
//!   - The next session opens at `reopen_et` if given; otherwise at the standard rule, 18:00 ET on the
//!     evening before the next non-closed weekday.
//!   - Example, Good Friday closed: Thursday's session closes 17:00 Thursday, and the next opens Sunday 18:00.
//!   - A `reopen_et` that contradicts the standard rule (e.g. Christmas 2007's "all CME products open 5:00 a.m. Dec 26")
//!     wins, because the table is what CME published.
//! - **Range.** Any instant or date outside [first table year 2006-01-01, Dec 31 of the last table year 2027-12-31]
//!   refuses (I5), with a clear message, as XNYS does outside 2000-2040.
//! - **Partial years.** If `cme_equity_holidays.md` lists a year as partly sourced, the calendar still answers for it,
//!   treating missing dates as normal days. That is the table's known limitation. The deliberately unfilled missing
//!   dates recorded in `cme_equity_holidays.md` (no CME source found; deliberately not filled) are:
//!   - 2023-01-16 MLK, 2023-02-20 Presidents Day, 2023-04-07 Good Friday. Checked: no 2023 tradinghours PDFs (404 in CDX),
//!     no non-compact 2023 holiday XLS or zip (the zip is a 404, the compact MGEX/DME XLS files have no equity row), and the
//!     API snapshots covering these windows return empty events.
//!   - Jan 1 2022 (Saturday): no weekday holiday session; nothing in the sources.

use std::collections::HashMap;
use std::sync::OnceLock;

use chrono::{DateTime, Datelike, Duration, NaiveDate, NaiveTime, Utc, Weekday};

use crate::calendar::{ny_time_to_utc, ny_to_utc, utc_to_ny};

pub const FIRST: NaiveDate = match NaiveDate::from_ymd_opt(2006, 1, 1) {
    Some(d) => d,
    None => panic!(),
};

pub const LAST: NaiveDate = match NaiveDate::from_ymd_opt(2027, 12, 31) {
    Some(d) => d,
    None => panic!(),
};

const EMBEDDED_CSV: &str = include_str!("../../data/cme_equity_holidays.csv");

/// Why the CME Globex calendar cannot answer.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum GlobexError {
    /// The date is outside the years whose rules this calendar carries (I5).
    OutOfRange(NaiveDate),
    /// The instant is outside the years whose rules this calendar carries (I5).
    OutOfRangeInstant(DateTime<Utc>),
    /// The date is not a trading session of CME Globex equity indices (I5).
    NotASession(NaiveDate),
    /// A date range whose start is after its end.
    InvertedRange(NaiveDate, NaiveDate),
    /// A row in the holiday table could not be parsed.
    MalformedRow(String),
}

impl std::fmt::Display for GlobexError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::OutOfRange(d) => {
                let first = first_date();
                let last = last_date();
                write!(f, "Date {d} is outside the CME Globex calendar range {first}..{last} (I5)")
            }
            Self::OutOfRangeInstant(t) => {
                let first = first_date();
                let last = last_date();
                write!(f, "Instant {t} is outside the CME Globex calendar range {first}..{last} (I5)")
            }
            Self::NotASession(d) => write!(f, "Date {d} is not a valid trading session of CME Globex (I5)"),
            Self::InvertedRange(s, e) => write!(f, "start date {s} cannot be after end date {e}"),
            Self::MalformedRow(msg) => write!(f, "Malformed CME holiday table row: {msg}"),
        }
    }
}

impl std::error::Error for GlobexError {}

pub type Result<T> = std::result::Result<T, GlobexError>;
pub use GlobexError as CalendarError;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum HolidayStatus {
    Closed,
    EarlyHalt,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct HolidayRow {
    pub date: NaiveDate,
    pub status: HolidayStatus,
    pub halt_et: Option<NaiveTime>,
    pub reopen_date: Option<NaiveDate>,
    pub reopen_time: Option<NaiveTime>,
    pub source_url: String,
    pub source_title: String,
    pub notes: String,
}

#[derive(Debug, Clone)]
pub struct SessionInfo {
    pub trade_date: NaiveDate,
    pub open_utc: DateTime<Utc>,
    pub close_utc: DateTime<Utc>,
}

#[derive(Debug, Clone)]
pub struct HolidayTable {
    pub rows: Vec<HolidayRow>,
    pub holidays: HashMap<NaiveDate, HolidayRow>,
    pub first_date: NaiveDate,
    pub last_date: NaiveDate,
    pub trade_dates: Vec<NaiveDate>,
    pub sessions: Vec<SessionInfo>,
    pub session_map: HashMap<NaiveDate, (DateTime<Utc>, DateTime<Utc>)>,
}

fn split_csv_line(line: &str) -> std::result::Result<Vec<String>, String> {
    let mut fields = Vec::new();
    let mut current = String::new();
    let mut in_quotes = false;
    let mut chars = line.chars().peekable();
    while let Some(c) = chars.next() {
        match c {
            '"' if in_quotes => {
                if chars.peek() == Some(&'"') {
                    chars.next();
                    current.push('"');
                } else {
                    in_quotes = false;
                }
            }
            '"' => {
                in_quotes = true;
            }
            ',' if !in_quotes => {
                fields.push(current);
                current = String::new();
            }
            _ => {
                current.push(c);
            }
        }
    }
    if in_quotes {
        return Err("unclosed quote in CSV line".to_string());
    }
    fields.push(current);
    Ok(fields)
}

/// Parses the CME holiday CSV text into a validated `HolidayTable`.
pub fn parse_holiday_table(csv_text: &str) -> Result<HolidayTable> {
    let mut lines = csv_text.lines();
    let header_line = match lines.next() {
        Some(l) => l.trim(),
        None => return Err(GlobexError::MalformedRow("empty CSV text".to_string())),
    };

    let header_fields = split_csv_line(header_line).map_err(GlobexError::MalformedRow)?;
    if header_fields.len() < 4
        || header_fields[0] != "date"
        || header_fields[1] != "status"
        || header_fields[2] != "halt_et"
        || header_fields[3] != "reopen_et"
    {
        return Err(GlobexError::MalformedRow(format!(
            "unexpected header columns: {header_line}"
        )));
    }

    let mut rows = Vec::new();
    let mut holidays = HashMap::new();
    let mut min_year = i32::MAX;
    let mut max_year = i32::MIN;

    for (line_idx, raw_line) in lines.enumerate() {
        let line = raw_line.trim();
        if line.is_empty() {
            continue;
        }

        let fields = split_csv_line(line).map_err(|e| {
            GlobexError::MalformedRow(format!("line {}: {e}", line_idx + 2))
        })?;

        if fields.len() < 4 {
            return Err(GlobexError::MalformedRow(format!(
                "line {}: expected at least 4 fields, got {}",
                line_idx + 2,
                fields.len()
            )));
        }

        let date = NaiveDate::parse_from_str(fields[0].trim(), "%Y-%m-%d").map_err(|e| {
            GlobexError::MalformedRow(format!("line {}: invalid date '{}': {e}", line_idx + 2, fields[0]))
        })?;

        let status = match fields[1].trim() {
            "closed" => HolidayStatus::Closed,
            "early_halt" => HolidayStatus::EarlyHalt,
            other => {
                return Err(GlobexError::MalformedRow(format!(
                    "line {}: invalid status '{}', expected 'closed' or 'early_halt'",
                    line_idx + 2,
                    other
                )))
            }
        };

        let halt_raw = fields[2].trim();
        let halt_et = match status {
            HolidayStatus::Closed => {
                if !halt_raw.is_empty() {
                    return Err(GlobexError::MalformedRow(format!(
                        "line {}: closed status must not have halt_et, got '{halt_raw}'",
                        line_idx + 2
                    )));
                }
                None
            }
            HolidayStatus::EarlyHalt => {
                if halt_raw.is_empty() {
                    return Err(GlobexError::MalformedRow(format!(
                        "line {}: early_halt status requires halt_et",
                        line_idx + 2
                    )));
                }
                let t = NaiveTime::parse_from_str(halt_raw, "%H:%M").map_err(|e| {
                    GlobexError::MalformedRow(format!(
                        "line {}: invalid halt_et '{halt_raw}': {e}",
                        line_idx + 2
                    ))
                })?;
                Some(t)
            }
        };

        let reopen_raw = fields[3].trim();
        let (reopen_date, reopen_time) = if reopen_raw.is_empty() {
            (None, None)
        } else if reopen_raw.contains(' ') {
            let mut parts = reopen_raw.split_whitespace();
            let d_str = parts.next().unwrap();
            let t_str = match parts.next() {
                Some(t) => t,
                None => {
                    return Err(GlobexError::MalformedRow(format!(
                        "line {}: invalid reopen_et format '{reopen_raw}'",
                        line_idx + 2
                    )))
                }
            };
            if parts.next().is_some() {
                return Err(GlobexError::MalformedRow(format!(
                    "line {}: too many tokens in reopen_et '{reopen_raw}'",
                    line_idx + 2
                )));
            }
            let rd = NaiveDate::parse_from_str(d_str, "%Y-%m-%d").map_err(|e| {
                GlobexError::MalformedRow(format!(
                    "line {}: invalid date in reopen_et '{d_str}': {e}",
                    line_idx + 2
                ))
            })?;
            let rt = NaiveTime::parse_from_str(t_str, "%H:%M").map_err(|e| {
                GlobexError::MalformedRow(format!(
                    "line {}: invalid time in reopen_et '{t_str}': {e}",
                    line_idx + 2
                ))
            })?;
            (Some(rd), Some(rt))
        } else {
            let rt = NaiveTime::parse_from_str(reopen_raw, "%H:%M").map_err(|e| {
                GlobexError::MalformedRow(format!(
                    "line {}: invalid time in reopen_et '{reopen_raw}': {e}",
                    line_idx + 2
                ))
            })?;
            (Some(date), Some(rt))
        };

        if let (Some(h), Some(rd), Some(rt)) = (halt_et, reopen_date, reopen_time) {
            if rd == date && rt <= h {
                return Err(GlobexError::MalformedRow(format!(
                    "line {}: reopen_et {rt} is before or equal to halt_et {h} on {date}",
                    line_idx + 2
                )));
            }
        }

        min_year = min_year.min(date.year());
        max_year = max_year.max(date.year());

        let source_url = fields.get(4).map(|s| s.trim().to_string()).unwrap_or_default();
        let source_title = fields.get(5).map(|s| s.trim().to_string()).unwrap_or_default();
        let notes = fields.get(6).map(|s| s.trim().to_string()).unwrap_or_default();

        let row = HolidayRow {
            date,
            status,
            halt_et,
            reopen_date,
            reopen_time,
            source_url,
            source_title,
            notes,
        };

        holidays.insert(date, row.clone());
        rows.push(row);
    }

    if rows.is_empty() {
        return Err(GlobexError::MalformedRow("no rows in holiday table".to_string()));
    }

    let first_date = NaiveDate::from_ymd_opt(min_year, 1, 1).expect("valid Jan 1");
    let last_date = NaiveDate::from_ymd_opt(max_year, 12, 31).expect("valid Dec 31");

    // Compute all valid trade dates: weekdays that are not closed
    let mut trade_dates = Vec::new();
    let mut cur = first_date;
    while cur <= last_date {
        let is_weekend = matches!(cur.weekday(), Weekday::Sat | Weekday::Sun);
        if !is_weekend {
            let is_closed = holidays.get(&cur).map(|r| r.status) == Some(HolidayStatus::Closed);
            if !is_closed {
                trade_dates.push(cur);
            }
        }
        cur += Duration::days(1);
    }

    // Map next_trade_date -> (reopen_date, reopen_time).
    // For each trade date, any holiday occurring in the non-trading interval leading up to it
    // [prev_td, td) can affect its open. The latest holiday in this interval determines the
    // session's reopen. If that latest holiday specifies a reopen, it overrides the default 18:00 ET.
    let mut open_overrides: HashMap<NaiveDate, (NaiveDate, NaiveTime)> = HashMap::new();
    for (i, &td) in trade_dates.iter().enumerate() {
        let prev_td = if i > 0 {
            trade_dates[i - 1]
        } else {
            first_date - Duration::days(1)
        };
        let relevant_holidays: Vec<&HolidayRow> = rows
            .iter()
            .filter(|r| r.date >= prev_td && r.date < td)
            .collect();
        if let Some(last_h) = relevant_holidays.last() {
            if let (Some(rd), Some(rt)) = (last_h.reopen_date, last_h.reopen_time) {
                open_overrides.insert(td, (rd, rt));
            }
        }
    }

    // Build sessions
    let mut sessions = Vec::with_capacity(trade_dates.len());
    let mut session_map = HashMap::with_capacity(trade_dates.len());

    for &td in &trade_dates {
        let open_utc = if let Some(&(rd, rt)) = open_overrides.get(&td) {
            ny_time_to_utc(rd, rt)
        } else {
            ny_to_utc(td - Duration::days(1), 18, 0)
        };

        let close_utc = if let Some(row) = holidays.get(&td) {
            if row.status == HolidayStatus::EarlyHalt {
                ny_time_to_utc(td, row.halt_et.expect("early_halt has halt_et"))
            } else {
                ny_to_utc(td, 17, 0)
            }
        } else {
            ny_to_utc(td, 17, 0)
        };

        if open_utc >= close_utc {
            return Err(GlobexError::MalformedRow(format!(
                "trade date {td} has open {open_utc} >= close {close_utc}"
            )));
        }

        sessions.push(SessionInfo {
            trade_date: td,
            open_utc,
            close_utc,
        });
        session_map.insert(td, (open_utc, close_utc));
    }

    for i in 1..sessions.len() {
        if sessions[i - 1].close_utc > sessions[i].open_utc {
            return Err(GlobexError::MalformedRow(format!(
                "overlapping sessions between {} close {} and {} open {}",
                sessions[i - 1].trade_date,
                sessions[i - 1].close_utc,
                sessions[i].trade_date,
                sessions[i].open_utc
            )));
        }
    }

    Ok(HolidayTable {
        rows,
        holidays,
        first_date,
        last_date,
        trade_dates,
        sessions,
        session_map,
    })
}

static TABLE: OnceLock<HolidayTable> = OnceLock::new();

fn get_table() -> &'static HolidayTable {
    TABLE.get_or_init(|| {
        parse_holiday_table(EMBEDDED_CSV).expect("valid embedded CME holiday table")
    })
}

/// Earliest calendar date supported by the table (Jan 1 of first table year).
pub fn first_date() -> NaiveDate {
    get_table().first_date
}

/// Latest calendar date supported by the table (Dec 31 of last table year).
pub fn last_date() -> NaiveDate {
    get_table().last_date
}

/// The number of rows in the embedded holiday table.
pub fn table_row_count() -> usize {
    get_table().rows.len()
}

fn check_date(d: NaiveDate) -> Result<()> {
    let table = get_table();
    if d < table.first_date || d > table.last_date {
        return Err(GlobexError::OutOfRange(d));
    }
    Ok(())
}

fn check_instant(t: DateTime<Utc>) -> Result<()> {
    let table = get_table();
    let d = utc_to_ny(t).date_naive();
    if d < table.first_date || d > table.last_date {
        return Err(GlobexError::OutOfRangeInstant(t));
    }
    Ok(())
}

/// True when `d` is an active CME Globex equity index trade date (not a weekend and not closed).
pub fn is_trade_date(d: NaiveDate) -> Result<bool> {
    check_date(d)?;
    Ok(get_table().session_map.contains_key(&d))
}

/// Alias for `is_trade_date` matching `calendar.rs` naming.
pub fn is_session(d: NaiveDate) -> Result<bool> {
    is_trade_date(d)
}

/// True if `d` is marked `closed` in the CME holiday table.
pub fn is_closed(d: NaiveDate) -> Result<bool> {
    check_date(d)?;
    Ok(get_table().holidays.get(&d).map(|r| r.status) == Some(HolidayStatus::Closed))
}

/// True if `d` is an active trade date with an early close/halt from the CME holiday table.
pub fn is_early_halt(d: NaiveDate) -> Result<bool> {
    check_date(d)?;
    Ok(get_table().holidays.get(&d).map(|r| r.status) == Some(HolidayStatus::EarlyHalt))
}

/// Returns the session open instant in UTC for trade date `d`. A non-session refuses (I5).
pub fn session_open(d: NaiveDate) -> Result<DateTime<Utc>> {
    check_date(d)?;
    match get_table().session_map.get(&d) {
        Some(&(open, _)) => Ok(open),
        None => Err(GlobexError::NotASession(d)),
    }
}

/// Returns the session close instant in UTC for trade date `d`. A non-session refuses (I5).
pub fn session_close(d: NaiveDate) -> Result<DateTime<Utc>> {
    check_date(d)?;
    match get_table().session_map.get(&d) {
        Some(&(_, close)) => Ok(close),
        None => Err(GlobexError::NotASession(d)),
    }
}

/// Returns the session trade date active at `instant`, or `None` if in a daily halt, weekend, or closure.
pub fn session_at(t: DateTime<Utc>) -> Result<Option<NaiveDate>> {
    check_instant(t)?;
    let table = get_table();
    let idx = table.sessions.partition_point(|s| s.open_utc <= t);
    if idx == 0 {
        return Ok(None);
    }
    let prev = &table.sessions[idx - 1];
    if t < prev.close_utc {
        Ok(Some(prev.trade_date))
    } else {
        Ok(None)
    }
}

/// True when the CME Globex equity market is open at instant `t` (open <= t < close).
pub fn is_open_at(t: DateTime<Utc>) -> Result<bool> {
    Ok(session_at(t)?.is_some())
}

/// Returns the trade date of the session active at instant `t`, or if `t` falls in a
/// daily halt, weekend, or holiday closure, returns the trade date of the NEXT upcoming session.
pub fn session_or_next(t: DateTime<Utc>) -> Result<NaiveDate> {
    check_instant(t)?;
    if let Some(td) = session_at(t)? {
        return Ok(td);
    }
    let table = get_table();
    let idx = table.sessions.partition_point(|s| s.open_utc <= t);
    if idx < table.sessions.len() {
        Ok(table.sessions[idx].trade_date)
    } else {
        Err(GlobexError::OutOfRangeInstant(t))
    }
}

/// Returns the first trade date strictly after `d`.
pub fn next_session(d: NaiveDate) -> Result<NaiveDate> {
    check_date(d)?;
    let table = get_table();
    let idx = table.trade_dates.partition_point(|&td| td <= d);
    if idx < table.trade_dates.len() {
        Ok(table.trade_dates[idx])
    } else {
        Err(GlobexError::OutOfRange(d))
    }
}

/// Returns the last trade date strictly before `d`.
pub fn previous_session(d: NaiveDate) -> Result<NaiveDate> {
    check_date(d)?;
    let table = get_table();
    let idx = table.trade_dates.partition_point(|&td| td < d);
    if idx > 0 {
        Ok(table.trade_dates[idx - 1])
    } else {
        Err(GlobexError::OutOfRange(d))
    }
}

/// Returns every session from `start` through `end`, inclusive.
pub fn sessions_in_range(start: NaiveDate, end: NaiveDate) -> Result<Vec<NaiveDate>> {
    if start > end {
        return Err(GlobexError::InvertedRange(start, end));
    }
    check_date(start)?;
    check_date(end)?;
    let table = get_table();
    let start_idx = table.trade_dates.partition_point(|&td| td < start);
    let end_idx = table.trade_dates.partition_point(|&td| td <= end);
    Ok(table.trade_dates[start_idx..end_idx].to_vec())
}

#[cfg(test)]
mod tests {
    use super::*;
    use chrono::TimeZone;

    fn ymd(y: i32, m: u32, d: u32) -> NaiveDate {
        NaiveDate::from_ymd_opt(y, m, d).unwrap()
    }

    #[test]
    fn test_embedded_table_row_count() {
        assert_eq!(table_row_count(), 252);
        assert_eq!(first_date(), ymd(2006, 1, 1));
        assert_eq!(last_date(), ymd(2027, 12, 31));
    }

    #[test]
    fn test_every_table_row_reproduced() {
        let table = get_table();
        for row in &table.rows {
            match row.status {
                HolidayStatus::Closed => {
                    assert_eq!(is_trade_date(row.date).unwrap(), false);
                    assert_eq!(is_session(row.date).unwrap(), false);
                    assert_eq!(is_closed(row.date).unwrap(), true);
                    assert_eq!(is_early_halt(row.date).unwrap(), false);
                    assert!(matches!(session_open(row.date), Err(GlobexError::NotASession(_))));
                    assert!(matches!(session_close(row.date), Err(GlobexError::NotASession(_))));
                }
                HolidayStatus::EarlyHalt => {
                    assert_eq!(is_trade_date(row.date).unwrap(), true);
                    assert_eq!(is_session(row.date).unwrap(), true);
                    assert_eq!(is_closed(row.date).unwrap(), false);
                    assert_eq!(is_early_halt(row.date).unwrap(), true);
                    let close = session_close(row.date).unwrap();
                    let expected_close = ny_time_to_utc(row.date, row.halt_et.unwrap());
                    assert_eq!(close, expected_close, "Halt time mismatch for {}", row.date);
                }
            }

            if let (Some(rd), Some(rt)) = (row.reopen_date, row.reopen_time) {
                if let Ok(next_td) = next_session(row.date) {
                    let intervening = table
                        .rows
                        .iter()
                        .any(|r| r.date > row.date && r.date < next_td);
                    if !intervening {
                        let open = session_open(next_td).unwrap();
                        let expected_open = ny_time_to_utc(rd, rt);
                        assert_eq!(
                            open, expected_open,
                            "Reopen time mismatch for session following {} on {}",
                            row.date, next_td
                        );
                    }
                }
            }
        }
    }

    #[test]
    fn test_normal_grid_and_halts() {
        // Monday 2024-04-15 was a regular, non-holiday week.
        let mon = ymd(2024, 4, 15);
        assert!(is_trade_date(mon).unwrap());

        // Sunday open at 18:00 ET (22:00 UTC in EDT)
        let mon_open = session_open(mon).unwrap();
        assert_eq!(mon_open, Utc.with_ymd_and_hms(2024, 4, 14, 22, 0, 0).unwrap());

        // Monday close at 17:00 ET (21:00 UTC in EDT)
        let mon_close = session_close(mon).unwrap();
        assert_eq!(mon_close, Utc.with_ymd_and_hms(2024, 4, 15, 21, 0, 0).unwrap());

        // Before open on Sunday: not open
        let sun_1759 = Utc.with_ymd_and_hms(2024, 4, 14, 21, 59, 0).unwrap();
        assert_eq!(session_at(sun_1759).unwrap(), None);
        assert!(!is_open_at(sun_1759).unwrap());

        // At open on Sunday: session is Monday
        assert_eq!(session_at(mon_open).unwrap(), Some(mon));
        assert!(is_open_at(mon_open).unwrap());

        // Daily halt 17:00 - 18:00 ET Monday: not open
        assert_eq!(session_at(mon_close).unwrap(), None);
        assert!(!is_open_at(mon_close).unwrap());
        let halt_mid = Utc.with_ymd_and_hms(2024, 4, 15, 21, 30, 0).unwrap();
        assert_eq!(session_at(halt_mid).unwrap(), None);

        // Tuesday session opens at 18:00 ET Monday
        let tue = ymd(2024, 4, 16);
        let tue_open = session_open(tue).unwrap();
        assert_eq!(tue_open, Utc.with_ymd_and_hms(2024, 4, 15, 22, 0, 0).unwrap());
        assert_eq!(session_at(tue_open).unwrap(), Some(tue));

        // Friday 2024-04-19 close at 17:00 ET
        let fri = ymd(2024, 4, 19);
        let fri_close = session_close(fri).unwrap();
        let fri_1659 = Utc.with_ymd_and_hms(2024, 4, 19, 20, 59, 0).unwrap();
        assert_eq!(session_at(fri_1659).unwrap(), Some(fri));
        assert_eq!(session_at(fri_close).unwrap(), None);

        // Saturday midday: weekend closure
        let sat_noon = Utc.with_ymd_and_hms(2024, 4, 20, 16, 0, 0).unwrap();
        assert_eq!(session_at(sat_noon).unwrap(), None);
    }

    #[test]
    fn test_dst_transition_weeks() {
        // Spring forward 2024: Sunday 2024-03-10
        // EST before 02:00 Sunday, EDT after.
        // Sunday 18:00 EDT = 22:00 UTC.
        let mon_post_spring = ymd(2024, 3, 11);
        let open_spring = session_open(mon_post_spring).unwrap();
        assert_eq!(open_spring, Utc.with_ymd_and_hms(2024, 3, 10, 22, 0, 0).unwrap());
        let close_spring = session_close(mon_post_spring).unwrap();
        assert_eq!(close_spring, Utc.with_ymd_and_hms(2024, 3, 11, 21, 0, 0).unwrap());

        // Winter week (EST, UTC-5): e.g. 2024-01-22 (Monday)
        let mon_winter = ymd(2024, 1, 22);
        let open_winter = session_open(mon_winter).unwrap();
        assert_eq!(open_winter, Utc.with_ymd_and_hms(2024, 1, 21, 23, 0, 0).unwrap());
        let close_winter = session_close(mon_winter).unwrap();
        assert_eq!(close_winter, Utc.with_ymd_and_hms(2024, 1, 22, 22, 0, 0).unwrap());

        // Fall back 2024: Sunday 2024-11-03
        // EDT before 02:00 Sunday, EST after.
        // Sunday 18:00 EST = 23:00 UTC.
        let mon_post_fall = ymd(2024, 11, 4);
        let open_fall = session_open(mon_post_fall).unwrap();
        assert_eq!(open_fall, Utc.with_ymd_and_hms(2024, 11, 3, 23, 0, 0).unwrap());
        let close_fall = session_close(mon_post_fall).unwrap();
        assert_eq!(close_fall, Utc.with_ymd_and_hms(2024, 11, 4, 22, 0, 0).unwrap());
    }

    #[test]
    fn test_christmas_2007_published_reopen_wins() {
        // Christmas 2007: table specifies reopen on Dec 26 at 06:00 ET.
        // Standard rule would have opened Tuesday Dec 25 at 18:00 ET.
        // Published notice wins!
        let dec26 = ymd(2007, 12, 26);
        let open = session_open(dec26).unwrap();
        // 06:00 EST = 11:00 UTC
        assert_eq!(open, Utc.with_ymd_and_hms(2007, 12, 26, 11, 0, 0).unwrap());

        // At 05:59 ET Dec 26, market is not yet open
        let just_before = Utc.with_ymd_and_hms(2007, 12, 26, 10, 59, 0).unwrap();
        assert_eq!(session_at(just_before).unwrap(), None);

        // At 06:00 ET Dec 26, market is open for trade date Dec 26
        assert_eq!(session_at(open).unwrap(), Some(dec26));
    }

    #[test]
    fn test_out_of_range_refusals() {
        let before = ymd(2005, 12, 31);
        let after = ymd(2028, 1, 1);
        assert!(matches!(is_trade_date(before), Err(GlobexError::OutOfRange(_))));
        assert!(matches!(is_trade_date(after), Err(GlobexError::OutOfRange(_))));
        assert!(matches!(session_open(before), Err(GlobexError::OutOfRange(_))));
        assert!(matches!(session_close(after), Err(GlobexError::OutOfRange(_))));
        assert!(matches!(next_session(ymd(2027, 12, 31)), Err(GlobexError::OutOfRange(_))));
        assert!(matches!(previous_session(ymd(2006, 1, 1)), Err(GlobexError::OutOfRange(_))));

        let t_before = Utc.with_ymd_and_hms(2005, 12, 31, 23, 59, 59).unwrap();
        let t_after = Utc.with_ymd_and_hms(2028, 1, 1, 6, 0, 0).unwrap();
        assert!(matches!(session_at(t_before), Err(GlobexError::OutOfRangeInstant(_))));
        assert!(matches!(session_at(t_after), Err(GlobexError::OutOfRangeInstant(_))));

        // Display check includes I5
        let err = is_trade_date(before).unwrap_err();
        assert!(err.to_string().contains("(I5)"));
    }

    #[test]
    fn test_malformed_csv_parser_refusals() {
        // Empty text
        assert!(matches!(parse_holiday_table(""), Err(GlobexError::MalformedRow(_))));

        // Bad header
        assert!(matches!(
            parse_holiday_table("foo,bar,baz\n2020-01-01,closed,,\n"),
            Err(GlobexError::MalformedRow(_))
        ));

        // Invalid date
        let bad_date = "date,status,halt_et,reopen_et\nnot-a-date,closed,,\n";
        assert!(matches!(parse_holiday_table(bad_date), Err(GlobexError::MalformedRow(_))));

        // Invalid status
        let bad_status = "date,status,halt_et,reopen_et\n2020-01-01,half_day,12:00,\n";
        assert!(matches!(parse_holiday_table(bad_status), Err(GlobexError::MalformedRow(_))));

        // Closed with halt_et
        let closed_with_halt = "date,status,halt_et,reopen_et\n2020-01-01,closed,13:00,\n";
        assert!(matches!(parse_holiday_table(closed_with_halt), Err(GlobexError::MalformedRow(_))));

        // Early halt without halt_et
        let halt_no_time = "date,status,halt_et,reopen_et\n2020-01-01,early_halt,,\n";
        assert!(matches!(parse_holiday_table(halt_no_time), Err(GlobexError::MalformedRow(_))));

        // Invalid time format
        let bad_time = "date,status,halt_et,reopen_et\n2020-01-01,early_halt,25:00,\n";
        assert!(matches!(parse_holiday_table(bad_time), Err(GlobexError::MalformedRow(_))));

        // Reopen before halt time on same day
        let reopen_before_halt = "date,status,halt_et,reopen_et\n2020-01-01,early_halt,13:00,12:00\n";
        assert!(matches!(parse_holiday_table(reopen_before_halt), Err(GlobexError::MalformedRow(_))));

        // Unclosed quote
        let unclosed = "date,status,halt_et,reopen_et,notes\n2020-01-01,closed,,,\"unclosed note\n";
        assert!(matches!(parse_holiday_table(unclosed), Err(GlobexError::MalformedRow(_))));
    }

    #[test]
    fn test_navigation_and_range_queries() {
        let d = ymd(2024, 4, 15);
        assert_eq!(next_session(d).unwrap(), ymd(2024, 4, 16));
        assert_eq!(previous_session(d).unwrap(), ymd(2024, 4, 12)); // previous Friday

        let range = sessions_in_range(ymd(2024, 4, 12), ymd(2024, 4, 16)).unwrap();
        assert_eq!(range, vec![ymd(2024, 4, 12), ymd(2024, 4, 15), ymd(2024, 4, 16)]);

        assert!(matches!(
            sessions_in_range(ymd(2024, 4, 16), ymd(2024, 4, 12)),
            Err(GlobexError::InvertedRange(_, _))
        ));
    }

    #[test]
    fn test_session_or_next() {
        // 1. Inside regular Monday session (Monday 2024-04-15 10:00 ET -> 14:00 UTC)
        let t_session = ny_to_utc(ymd(2024, 4, 15), 10, 0);
        assert_eq!(session_or_next(t_session).unwrap(), ymd(2024, 4, 15));

        // 2. Daily halt (Monday 2024-04-15 17:30 ET -> 21:30 UTC): belongs to Tuesday 2024-04-16
        let t_halt = ny_to_utc(ymd(2024, 4, 15), 17, 30);
        assert_eq!(session_or_next(t_halt).unwrap(), ymd(2024, 4, 16));

        // 3. Weekend (Saturday 2024-04-13 12:00 ET): belongs to Monday 2024-04-15
        let t_weekend = ny_to_utc(ymd(2024, 4, 13), 12, 0);
        assert_eq!(session_or_next(t_weekend).unwrap(), ymd(2024, 4, 15));

        // 4. Closed day: Christmas 2023 (2023-12-25 closed all day)
        // Monday 2023-12-25 at 12:00 ET -> belongs to Tuesday 2023-12-26 session (which reopens 18:00 ET)
        let t_xmas = ny_to_utc(ymd(2023, 12, 25), 12, 0);
        assert_eq!(session_or_next(t_xmas).unwrap(), ymd(2023, 12, 26));

        // 5. Early halt day: Thanksgiving Friday 2020-11-27 (halt at 13:15 ET)
        // Inside session at 11:00 ET -> 2020-11-27
        let t_early_in = ny_to_utc(ymd(2020, 11, 27), 11, 0);
        assert_eq!(session_or_next(t_early_in).unwrap(), ymd(2020, 11, 27));
        // After early halt at 14:00 ET -> belongs to next session (Monday 2020-11-30)
        let t_early_after = ny_to_utc(ymd(2020, 11, 27), 14, 0);
        assert_eq!(session_or_next(t_early_after).unwrap(), ymd(2020, 11, 30));
    }
}

//! The NYSE (XNYS) session calendar, 2000-01-01 through 2040-12-31 (RUST_PORT P1a).
//!
//! Replaces the engine's wrapper over `exchange_calendars`, which stays as the
//! test-only oracle: `tests/test_calendar_oracle.py` compares every date in range.
//! A date outside the range refuses (I5) rather than guessing a rule that may not
//! hold there. Regular hours are 09:30-16:00 New York; an early close is 13:00.

use chrono::{DateTime, Datelike, Duration, NaiveDate, NaiveTime, TimeZone, Utc, Weekday};
use chrono_tz::America::New_York;

pub mod globex;

/// The trading venue calendar governing a simulation book.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum VenueCalendar {
    /// New York Stock Exchange (09:30-16:00 ET, early close 13:00 ET).
    Xnys,
    /// CME Globex futures (equity index, energy, metals; per-root table via `globex::GlobexCalendar::for_root`;
    /// Sunday 18:00 to Friday 17:00 ET, daily halt 17:00-18:00 ET).
    Globex,
}

/// Why the calendar cannot answer.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum CalendarError {
    /// The date is outside the years whose rules this calendar carries.
    OutOfRange(NaiveDate),
    /// The date is not a trading session, so it has no open or close (I5).
    NotASession(NaiveDate),
    /// A range whose start is after its end.
    InvertedRange(NaiveDate, NaiveDate),
    /// A non-positive session window.
    BadWindow(i64),
}

impl std::fmt::Display for CalendarError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::OutOfRange(d) => write!(f, "Date {d} is outside the XNYS calendar range {FIRST}..{LAST} (I5)"),
            Self::NotASession(d) => write!(f, "Date {d} is not a valid trading session of XNYS (I5)"),
            Self::InvertedRange(s, e) => write!(f, "start date {s} cannot be after end date {e}"),
            Self::BadWindow(w) => write!(f, "window must be positive, got {w}"),
        }
    }
}

impl std::error::Error for CalendarError {}

pub type Result<T> = std::result::Result<T, CalendarError>;

pub const FIRST: NaiveDate = match NaiveDate::from_ymd_opt(2000, 1, 1) {
    Some(d) => d,
    None => panic!(),
};
pub const LAST: NaiveDate = match NaiveDate::from_ymd_opt(2040, 12, 31) {
    Some(d) => d,
    None => panic!(),
};

const fn ymd(y: i32, m: u32, d: u32) -> NaiveDate {
    match NaiveDate::from_ymd_opt(y, m, d) {
        Some(x) => x,
        None => panic!(),
    }
}

/// Unscheduled full-day closures (events, national days of mourning).
const SPECIAL_CLOSURES: [NaiveDate; 10] = [
    ymd(2001, 9, 11), // September 11
    ymd(2001, 9, 12),
    ymd(2001, 9, 13),
    ymd(2001, 9, 14),
    ymd(2004, 6, 11), // President Reagan
    ymd(2007, 1, 2),  // President Ford
    ymd(2012, 10, 29), // Hurricane Sandy
    ymd(2012, 10, 30),
    ymd(2018, 12, 5), // President G.H.W. Bush
    ymd(2025, 1, 9),  // President Carter
];

/// Early closes outside the three standing rules.
const SPECIAL_EARLY_CLOSES: [NaiveDate; 2] = [
    ymd(2002, 7, 5),   // the Friday after a Thursday Independence Day
    ymd(2003, 12, 26), // the Friday after a Thursday Christmas
];

/// Years in which July 3 fell Monday-Thursday but the market closed at the normal time.
const NO_JULY_3_EARLY_CLOSE: [i32; 1] = [2002];

fn check(d: NaiveDate) -> Result<()> {
    if d < FIRST || d > LAST {
        return Err(CalendarError::OutOfRange(d));
    }
    Ok(())
}

fn nth_weekday(year: i32, month: u32, wd: Weekday, n: u8) -> NaiveDate {
    NaiveDate::from_weekday_of_month_opt(year, month, wd, n).expect("nth weekday exists")
}

fn last_weekday(year: i32, month: u32, wd: Weekday) -> NaiveDate {
    let first_next = if month == 12 { ymd(year + 1, 1, 1) } else { ymd(year, month + 1, 1) };
    let mut d = first_next - Duration::days(1);
    while d.weekday() != wd {
        d -= Duration::days(1);
    }
    d
}

/// Western Easter Sunday (anonymous Gregorian algorithm).
fn easter(year: i32) -> NaiveDate {
    let a = year % 19;
    let b = year / 100;
    let c = year % 100;
    let d = b / 4;
    let e = b % 4;
    let f = (b + 8) / 25;
    let g = (b - f + 1) / 3;
    let h = (19 * a + b - d - g + 15) % 30;
    let i = c / 4;
    let k = c % 4;
    let l = (32 + 2 * e + 2 * i - h - k) % 7;
    let m = (a + 11 * h + 22 * l) / 451;
    let month = (h + l - 7 * m + 114) / 31;
    let day = (h + l - 7 * m + 114) % 31 + 1;
    ymd(year, month as u32, day as u32)
}

/// A fixed-date holiday observed on Friday when it falls Saturday and Monday when Sunday.
fn observed(d: NaiveDate) -> NaiveDate {
    match d.weekday() {
        Weekday::Sat => d - Duration::days(1),
        Weekday::Sun => d + Duration::days(1),
        _ => d,
    }
}

/// The weekday holidays the exchange observes in `year`.
fn holidays(year: i32) -> Vec<NaiveDate> {
    let mut h = Vec::with_capacity(10);
    // New Year's Day: a Saturday New Year is not observed on the Friday before.
    let ny = ymd(year, 1, 1);
    match ny.weekday() {
        Weekday::Sat => {}
        Weekday::Sun => h.push(ny + Duration::days(1)),
        _ => h.push(ny),
    }
    h.push(nth_weekday(year, 1, Weekday::Mon, 3)); // Martin Luther King Jr.
    h.push(nth_weekday(year, 2, Weekday::Mon, 3)); // Washington's Birthday
    h.push(easter(year) - Duration::days(2)); // Good Friday
    h.push(last_weekday(year, 5, Weekday::Mon)); // Memorial Day
    if year >= 2022 {
        h.push(observed(ymd(year, 6, 19))); // Juneteenth
    }
    h.push(observed(ymd(year, 7, 4))); // Independence Day
    h.push(nth_weekday(year, 9, Weekday::Mon, 1)); // Labor Day
    h.push(nth_weekday(year, 11, Weekday::Thu, 4)); // Thanksgiving
    h.push(observed(ymd(year, 12, 25))); // Christmas
    h
}

fn is_weekend(d: NaiveDate) -> bool {
    matches!(d.weekday(), Weekday::Sat | Weekday::Sun)
}

fn session_unchecked(d: NaiveDate) -> bool {
    !is_weekend(d) && !SPECIAL_CLOSURES.contains(&d) && !holidays(d.year()).contains(&d)
}

/// True when `d` is a trading session.
pub fn is_session(d: NaiveDate) -> Result<bool> {
    check(d)?;
    Ok(session_unchecked(d))
}

/// True for a weekday the exchange is closed (a weekend is not a holiday).
pub fn is_holiday(d: NaiveDate) -> Result<bool> {
    check(d)?;
    Ok(!is_weekend(d) && !session_unchecked(d))
}

fn early_close_unchecked(d: NaiveDate) -> bool {
    if !session_unchecked(d) {
        return false;
    }
    let y = d.year();
    let mon_thu = matches!(d.weekday(), Weekday::Mon | Weekday::Tue | Weekday::Wed | Weekday::Thu);
    let day_after_thanksgiving = nth_weekday(y, 11, Weekday::Thu, 4) + Duration::days(1);
    (d.month() == 7 && d.day() == 3 && mon_thu && !NO_JULY_3_EARLY_CLOSE.contains(&y))
        || d == day_after_thanksgiving
        || (d.month() == 12 && d.day() == 24 && mon_thu)
        || SPECIAL_EARLY_CLOSES.contains(&d)
}

/// True for a session that closes at 13:00 New York instead of 16:00.
pub fn is_early_close(d: NaiveDate) -> Result<bool> {
    check(d)?;
    Ok(early_close_unchecked(d))
}

pub(crate) fn ny_time_to_utc(d: NaiveDate, t: NaiveTime) -> DateTime<Utc> {
    let local = d.and_time(t);
    New_York
        .from_local_datetime(&local)
        .single()
        .expect("market times in New York are never in a DST gap or overlap")
        .with_timezone(&Utc)
}

pub(crate) fn ny_to_utc(d: NaiveDate, hour: u32, minute: u32) -> DateTime<Utc> {
    let t = NaiveTime::from_hms_opt(hour, minute, 0).expect("valid time");
    ny_time_to_utc(d, t)
}

pub(crate) fn utc_to_ny(t: DateTime<Utc>) -> DateTime<chrono_tz::Tz> {
    t.with_timezone(&New_York)
}

/// The session's open, in UTC. A non-session refuses (I5).
pub fn session_open(d: NaiveDate) -> Result<DateTime<Utc>> {
    if !is_session(d)? {
        return Err(CalendarError::NotASession(d));
    }
    Ok(ny_to_utc(d, 9, 30))
}

/// The session's close, in UTC, reflecting an early close. A non-session refuses (I5).
pub fn session_close(d: NaiveDate) -> Result<DateTime<Utc>> {
    if !is_session(d)? {
        return Err(CalendarError::NotASession(d));
    }
    Ok(if early_close_unchecked(d) { ny_to_utc(d, 13, 0) } else { ny_to_utc(d, 16, 0) })
}

fn step(d: NaiveDate, forward: bool) -> Result<NaiveDate> {
    let mut x = d;
    loop {
        x += Duration::days(if forward { 1 } else { -1 });
        check(x)?;
        if session_unchecked(x) {
            return Ok(x);
        }
    }
}

/// The first session strictly after `d`.
pub fn next_session(d: NaiveDate) -> Result<NaiveDate> {
    check(d)?;
    step(d, true)
}

/// The last session strictly before `d`.
pub fn previous_session(d: NaiveDate) -> Result<NaiveDate> {
    check(d)?;
    step(d, false)
}

/// `d` if it is a session, else the nearest session in the given direction.
pub fn roll_to_session(d: NaiveDate, forward: bool) -> Result<NaiveDate> {
    if is_session(d)? {
        return Ok(d);
    }
    step(d, forward)
}

/// Every session from `start` through `end`, inclusive.
pub fn sessions_in_range(start: NaiveDate, end: NaiveDate) -> Result<Vec<NaiveDate>> {
    if start > end {
        return Err(CalendarError::InvertedRange(start, end));
    }
    check(start)?;
    check(end)?;
    Ok(start.iter_days().take_while(|d| *d <= end).filter(|d| session_unchecked(*d)).collect())
}

/// The `count` sessions ending at session `end`, oldest first.
pub fn sessions_window_back(end: NaiveDate, count: i64) -> Result<Vec<NaiveDate>> {
    if count <= 0 {
        return Err(CalendarError::BadWindow(count));
    }
    if !is_session(end)? {
        return Err(CalendarError::NotASession(end));
    }
    let mut out = vec![end];
    let mut d = end;
    while (out.len() as i64) < count {
        d = step(d, false)?;
        out.push(d);
    }
    out.reverse();
    Ok(out)
}

/// True when the market is open at `t`: open <= t < close of that session.
pub fn is_open_at(t: DateTime<Utc>) -> Result<bool> {
    let d = t.with_timezone(&New_York).date_naive();
    if !is_session(d)? {
        return Ok(false);
    }
    Ok(session_open(d)? <= t && t < session_close(d)?)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn easter_known_years() {
        assert_eq!(easter(2024), ymd(2024, 3, 31));
        assert_eq!(easter(2026), ymd(2026, 4, 5));
        assert_eq!(easter(2038), ymd(2038, 4, 25));
    }

    #[test]
    fn day_after_thanksgiving_closes_early() {
        let d = ymd(2026, 11, 27);
        assert!(is_early_close(d).unwrap());
        assert_eq!(session_close(d).unwrap(), Utc.with_ymd_and_hms(2026, 11, 27, 18, 0, 0).unwrap());
    }

    #[test]
    fn holiday_refuses_open() {
        assert_eq!(session_open(ymd(2026, 12, 25)), Err(CalendarError::NotASession(ymd(2026, 12, 25))));
    }

    #[test]
    fn out_of_range_refuses() {
        assert!(matches!(is_session(ymd(1999, 12, 31)), Err(CalendarError::OutOfRange(_))));
        assert!(matches!(next_session(ymd(2040, 12, 31)), Err(CalendarError::OutOfRange(_))));
    }

    #[test]
    fn open_at_is_half_open() {
        let open = session_open(ymd(2026, 11, 25)).unwrap();
        let close = session_close(ymd(2026, 11, 25)).unwrap();
        assert!(is_open_at(open).unwrap());
        assert!(!is_open_at(close).unwrap());
        assert!(!is_open_at(open - Duration::seconds(1)).unwrap());
    }
}

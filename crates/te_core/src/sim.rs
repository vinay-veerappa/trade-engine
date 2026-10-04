//! The simulated venues' fill rules (docs/RUST_PORT.md P3a): the one-minute-bar equity
//! venue (`broker`), the chain-snapshot option venue (`snapshot`) and the trailing-stop
//! emulator (`trailing`). Python keeps only the adapter plumbing: the clock read, the
//! carriers it hands back, and the mapping of a refusal kind to its exception class.
//!
//! No clock is read here (I7): the host passes a `now` callback, and each rule calls it
//! exactly where the Python called `self._now()`, so a clock that moves between reads
//! moves at the same points on both sides.

pub mod broker;
pub mod snapshot;
pub mod tick;
pub mod trailing;

use chrono::{Datelike, NaiveDate, TimeZone, Utc};
use chrono_tz::America::New_York;

use crate::calendar::{self as cal, CalendarError};
use crate::ledger::model::{err, parse_datetime, DateTime, LErr, R};
use crate::ledger::pydec::{DecErr, PyDec};

/// The clock: returns `clock.now_utc().isoformat()`.
pub type Clock<'a> = dyn FnMut() -> R<String> + 'a;

/// A timestamp as Python held it: the instant, and the exact `isoformat()` text the
/// host gave (returned unchanged, so the host can hand back its own object).
#[derive(Debug, Clone)]
pub struct Ts {
    pub dt: DateTime,
    pub iso: String,
}

impl Ts {
    /// An aware timestamp from its `isoformat()`; naive refuses with `"{name} must be
    /// timezone-aware"`, as `_validate_timestamp` did.
    pub fn aware(iso: &str, name: &str) -> R<Ts> {
        let (dt, aware) = parse_datetime(iso)?;
        if !aware {
            return err("value", format!("{name} must be timezone-aware"));
        }
        Ok(Ts { dt, iso: iso.to_string() })
    }

    /// An instant the calendar computed: UTC, as the Python calendar returns.
    pub fn utc(t: chrono::DateTime<Utc>) -> Ts {
        let date = t.date_naive();
        let sod = t.timestamp().rem_euclid(86400) as u32;
        let dt = DateTime { date, sod, us: t.timestamp_subsec_micros(), off: 0 };
        let iso = dt.iso();
        Ts { dt, iso }
    }

    pub fn key(&self) -> (i64, u32) {
        self.dt.instant()
    }
    pub fn lt(&self, o: &Ts) -> bool {
        self.key() < o.key()
    }
    pub fn le(&self, o: &Ts) -> bool {
        self.key() <= o.key()
    }
    pub fn gt(&self, o: &Ts) -> bool {
        self.key() > o.key()
    }
    pub fn ge(&self, o: &Ts) -> bool {
        self.key() >= o.key()
    }
    pub fn eq(&self, o: &Ts) -> bool {
        self.key() == o.key()
    }

    /// `astimezone(NEW_YORK).date()`.
    pub fn ny_date(&self) -> R<NaiveDate> {
        let (secs, us) = self.key();
        // seconds since 0001-01-01 -> since the Unix epoch
        let unix = secs - 719_163 * 86_400;
        match Utc.timestamp_opt(unix, us * 1000).single() {
            Some(t) => Ok(t.with_timezone(&New_York).date_naive()),
            None => err("overflow", "date value out of range"),
        }
    }

    /// `+ timedelta(seconds=n)` on the wall clock, keeping the offset.
    pub fn add_seconds(&self, n: i64) -> R<Ts> {
        let total = self.dt.sod as i64 + n;
        let days = total.div_euclid(86400);
        let sod = total.rem_euclid(86400) as u32;
        let date = self
            .dt
            .date
            .checked_add_signed(chrono::Duration::days(days))
            .filter(|d| (1..=9999).contains(&d.year()));
        let Some(date) = date else {
            return err("overflow", "date value out of range");
        };
        let dt = DateTime { date, sod, us: self.dt.us, off: self.dt.off };
        let iso = dt.iso();
        Ok(Ts { dt, iso })
    }

    /// `.replace(second=0, microsecond=0)`.
    pub fn floor_minute(&self) -> Ts {
        let dt = DateTime { date: self.dt.date, sod: self.dt.sod - self.dt.sod % 60, us: 0, off: self.dt.off };
        let iso = dt.iso();
        Ts { dt, iso }
    }

    /// `.second or .microsecond`.
    pub fn off_minute(&self) -> bool {
        self.dt.sod % 60 != 0 || self.dt.us != 0
    }
}

/// `max(a, b)` over timestamps: the first unless the second is later.
pub fn ts_max<'a>(a: &'a Ts, b: &'a Ts) -> &'a Ts {
    if b.gt(a) {
        b
    } else {
        a
    }
}

/// Read the clock once (`self._now()`): an aware datetime or a refusal.
pub fn now(clock: &mut Clock<'_>) -> R<Ts> {
    let iso = clock()?;
    Ts::aware(&iso, "clock.now_utc()")
}

pub fn calerr(e: CalendarError) -> LErr {
    LErr { kind: "value", msg: e.to_string() }
}

pub fn dk(e: DecErr) -> LErr {
    crate::ledger::model::derr(e)
}

pub fn is_session(d: NaiveDate) -> R<bool> {
    cal::is_session(d).map_err(calerr)
}
pub fn session_open(d: NaiveDate) -> R<Ts> {
    cal::session_open(d).map(Ts::utc).map_err(calerr)
}
pub fn session_close(d: NaiveDate) -> R<Ts> {
    cal::session_close(d).map(Ts::utc).map_err(calerr)
}
pub fn next_session(d: NaiveDate) -> R<NaiveDate> {
    cal::next_session(d).map_err(calerr)
}
pub fn roll_next(d: NaiveDate) -> R<NaiveDate> {
    cal::roll_to_session(d, true).map_err(calerr)
}

/// Python's `min(a, b)`: `a` unless `b < a`.
pub fn dmin(a: &PyDec, b: &PyDec) -> R<PyDec> {
    Ok(if b.lt(a).map_err(dk)? { b.clone() } else { a.clone() })
}

/// Python's `max(a, b)`: `a` unless `b > a`.
pub fn dmax(a: &PyDec, b: &PyDec) -> R<PyDec> {
    Ok(if b.gt(a).map_err(dk)? { b.clone() } else { a.clone() })
}

pub fn dec(s: &str) -> PyDec {
    PyDec::parse(s).expect("a literal Decimal")
}

/// `s.isdecimal()` for the ASCII digits (a non-ASCII decimal digit refuses: see the
/// deviations in docs/RUST_PORT.md P3a).
pub fn ascii_decimal(s: &str) -> bool {
    !s.is_empty() && s.bytes().all(|b| b.is_ascii_digit())
}

/// `s.rpartition(sep)`.
pub fn rpartition<'a>(s: &'a str, sep: &str) -> (&'a str, bool, &'a str) {
    match s.rfind(sep) {
        Some(i) => (&s[..i], true, &s[i + sep.len()..]),
        None => ("", false, s),
    }
}

/// `date.__str__`.
pub fn date_str(d: &NaiveDate) -> String {
    crate::ledger::model::date_iso(d)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ny_date_crosses_midnight_utc() {
        let t = Ts::aware("2026-03-03T03:00:00+00:00", "x").unwrap();
        assert_eq!(t.ny_date().unwrap(), NaiveDate::from_ymd_opt(2026, 3, 2).unwrap());
    }

    #[test]
    fn utc_calendar_instant_iso() {
        let t = session_close(NaiveDate::from_ymd_opt(2026, 11, 27).unwrap()).unwrap();
        assert_eq!(t.iso, "2026-11-27T18:00:00+00:00");
    }

    #[test]
    fn naive_refuses() {
        assert_eq!(now(&mut || Ok("2026-01-02T10:00:00".to_string())).unwrap_err().msg, "clock.now_utc() must be timezone-aware");
    }

    #[test]
    fn rpartition_matches_python() {
        assert_eq!(rpartition("a:fill:3", ":fill:"), ("a", true, "3"));
        assert_eq!(rpartition("abc", ":fill:"), ("", false, "abc"));
    }
}

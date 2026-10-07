//! Per-group Globex calendars (P6C): energy (CL, MCL), metals (GC, MGC) and the equity-table roots, each by root.
//!
//! One module per group: a normal day, a weekend, each early-halt kind the group's table carries, a closure with
//! its reopen, and the range edges of every root. Expected instants are written as New York wall-clock times so
//! they read like the CME notices the tables cite.

use chrono::{DateTime, NaiveDate, TimeZone, Utc};
use chrono_tz::America::New_York;
use te_core::calendar::globex::{supported_roots, GlobexCalendar, GlobexError, Group};

fn d(y: i32, m: u32, day: u32) -> NaiveDate {
    NaiveDate::from_ymd_opt(y, m, day).unwrap()
}

/// New York wall-clock to UTC.
fn et(y: i32, m: u32, day: u32, h: u32, min: u32) -> DateTime<Utc> {
    New_York.with_ymd_and_hms(y, m, day, h, min, 0).single().unwrap().with_timezone(&Utc)
}

fn cal(root: &str) -> GlobexCalendar {
    GlobexCalendar::for_root(root).unwrap()
}

/// What every group shares: the weekday session, the weekend, the Sunday open.
fn check_normal_and_weekend(root: &str, tue: NaiveDate) {
    let c = cal(root);
    // a normal Tuesday: opens Monday 18:00 ET, closes Tuesday 17:00 ET
    let prev = tue - chrono::Duration::days(1);
    assert!(c.is_session(tue).unwrap(), "{root}");
    assert!(!c.is_closed(tue).unwrap());
    assert!(!c.is_early_halt(tue).unwrap());
    assert_eq!(c.session_open(tue).unwrap(), et(prev.year_(), prev.month_(), prev.day_(), 18, 0), "{root} open");
    assert_eq!(c.session_close(tue).unwrap(), et(tue.year_(), tue.month_(), tue.day_(), 17, 0), "{root} close");
    // the weekend: Saturday and Sunday are not trade dates, and nothing is open on Saturday
    let sat = tue + chrono::Duration::days(4);
    let sun = sat + chrono::Duration::days(1);
    assert!(!c.is_session(sat).unwrap());
    assert!(!c.is_session(sun).unwrap());
    assert_eq!(c.session_open(sat), Err(GlobexError::NotASession(sat)));
    assert_eq!(c.session_at(et(sat.year_(), sat.month_(), sat.day_(), 12, 0)).unwrap(), None);
    // Sunday 18:00 ET opens Monday's session
    let mon = sun + chrono::Duration::days(1);
    assert_eq!(c.session_open(mon).unwrap(), et(sun.year_(), sun.month_(), sun.day_(), 18, 0));
    assert_eq!(c.session_at(et(sun.year_(), sun.month_(), sun.day_(), 18, 0)).unwrap(), Some(mon));
    // the daily maintenance halt: 17:00-18:00 ET is in no session
    assert_eq!(c.session_at(et(tue.year_(), tue.month_(), tue.day_(), 17, 30)).unwrap(), None);
    assert_eq!(c.session_at(et(tue.year_(), tue.month_(), tue.day_(), 16, 59)).unwrap(), Some(tue));
}

trait Ymd {
    fn year_(&self) -> i32;
    fn month_(&self) -> u32;
    fn day_(&self) -> u32;
}
impl Ymd for NaiveDate {
    fn year_(&self) -> i32 {
        chrono::Datelike::year(self)
    }
    fn month_(&self) -> u32 {
        chrono::Datelike::month(self)
    }
    fn day_(&self) -> u32 {
        chrono::Datelike::day(self)
    }
}

mod energy {
    use super::*;

    #[test]
    fn normal_day_and_weekend() {
        check_normal_and_weekend("CL", d(2024, 3, 12));
        check_normal_and_weekend("MCL", d(2024, 3, 12));
    }

    #[test]
    fn routes_to_the_energy_group() {
        assert_eq!(cal("CL").group(), Group::Energy);
        assert_eq!(cal("MCL").group(), Group::Energy);
    }

    /// Each early-halt kind the table carries, with its own date. The energy halt is NOT the equity 13:00.
    #[test]
    fn early_halt_kinds() {
        let c = cal("CL");
        // 13:30 ET, reopen 18:00 the same evening (MLK 2024)
        assert!(c.is_early_halt(d(2024, 1, 15)).unwrap());
        assert_eq!(c.session_close(d(2024, 1, 15)).unwrap(), et(2024, 1, 15, 13, 30));
        // 13:30 ET on July 4 2024 (equity: 13:00)
        assert_eq!(c.session_close(d(2024, 7, 4)).unwrap(), et(2024, 7, 4, 13, 30));
        // 14:30 ET (Memorial Day 2024)
        assert_eq!(c.session_close(d(2024, 5, 27)).unwrap(), et(2024, 5, 27, 14, 30));
        // 14:45 ET, no stated reopen (the day after Thanksgiving 2024)
        assert_eq!(c.session_close(d(2024, 11, 29)).unwrap(), et(2024, 11, 29, 14, 45));
        // 13:45 ET (Christmas Eve 2024)
        assert_eq!(c.session_close(d(2024, 12, 24)).unwrap(), et(2024, 12, 24, 13, 45));
        // 13:15 ET (Memorial Day 2009) and 13:00 ET (Labor Day 2023)
        assert_eq!(c.session_close(d(2009, 5, 25)).unwrap(), et(2009, 5, 25, 13, 15));
        assert_eq!(c.session_close(d(2023, 9, 4)).unwrap(), et(2023, 9, 4, 13, 0));
        // 16:15 ET (a Friday before a Monday holiday, 2009-05-22), reopen Sunday 18:00
        assert_eq!(c.session_close(d(2009, 5, 22)).unwrap(), et(2009, 5, 22, 16, 15));
        // the equity group closes July 4 2024 at 13:00: the groups differ
        assert_eq!(cal("ES").session_close(d(2024, 7, 4)).unwrap(), et(2024, 7, 4, 13, 0));
        assert_ne!(cal("ES").session_close(d(2024, 7, 4)).unwrap(), c.session_close(d(2024, 7, 4)).unwrap());
    }

    /// The halt is followed by the reopen: Christmas Eve halts early, Christmas is closed, Dec 26 opens 18:00 Dec 25.
    #[test]
    fn closure_and_reopen() {
        let c = cal("CL");
        assert!(c.is_closed(d(2024, 12, 25)).unwrap());
        assert!(!c.is_session(d(2024, 12, 25)).unwrap());
        assert_eq!(c.session_open(d(2024, 12, 26)).unwrap(), et(2024, 12, 25, 18, 0));
        // Good Friday 2024: closed; Monday opens Sunday evening
        assert!(c.is_closed(d(2024, 3, 29)).unwrap());
        assert_eq!(c.previous_session(d(2024, 4, 1)).unwrap(), d(2024, 3, 28));
        assert_eq!(c.session_open(d(2024, 4, 1)).unwrap(), et(2024, 3, 31, 18, 0));
        assert_eq!(c.next_session(d(2024, 3, 28)).unwrap(), d(2024, 4, 1));
        // nothing is open during the closure
        assert_eq!(c.session_at(et(2024, 3, 29, 10, 0)).unwrap(), None);
    }

    #[test]
    fn range_edges() {
        let cl = cal("CL");
        assert_eq!(cl.first_date(), d(2009, 1, 1));
        assert_eq!(cl.last_date(), d(2027, 12, 31));
        assert!(cl.is_closed(d(2009, 1, 1)).unwrap());
        assert!(cl.is_session(d(2009, 1, 2)).unwrap());
        assert!(matches!(cl.is_session(d(2008, 12, 31)), Err(GlobexError::RootOutOfRange { root: "CL", .. })));
        assert!(matches!(cl.is_session(d(2028, 1, 1)), Err(GlobexError::RootOutOfRange { root: "CL", .. })));
        assert!(matches!(
            cl.session_at(et(2008, 12, 31, 12, 0)),
            Err(GlobexError::RootOutOfRangeInstant { root: "CL", .. })
        ));
        // the first session cannot step back before the range
        assert!(cl.previous_session(d(2009, 1, 2)).is_err());
        // MCL's range starts at its listing (first session opens Sunday 2021-07-11 18:00 ET)
        let mcl = cal("MCL");
        assert_eq!(mcl.first_date(), d(2021, 7, 11));
        assert_eq!(mcl.last_date(), d(2027, 12, 31));
        assert!(matches!(mcl.is_session(d(2021, 7, 9)), Err(GlobexError::RootOutOfRange { root: "MCL", .. })));
        assert!(matches!(mcl.session_open(d(2021, 7, 10)), Err(GlobexError::RootOutOfRange { root: "MCL", .. })));
        assert_eq!(mcl.session_open(d(2021, 7, 12)).unwrap(), et(2021, 7, 11, 18, 0));
        assert_eq!(mcl.previous_session(d(2021, 7, 13)).unwrap(), d(2021, 7, 12));
        assert!(mcl.previous_session(d(2021, 7, 12)).is_err());
        // CL (listed long before) reaches the same dates that MCL refuses
        assert!(cl.is_session(d(2021, 7, 9)).unwrap());
    }
}

mod metals {
    use super::*;

    #[test]
    fn normal_day_and_weekend() {
        check_normal_and_weekend("GC", d(2024, 3, 12));
        check_normal_and_weekend("MGC", d(2024, 3, 12));
    }

    #[test]
    fn routes_to_the_metals_group() {
        assert_eq!(cal("GC").group(), Group::Metals);
        assert_eq!(cal("MGC").group(), Group::Metals);
    }

    #[test]
    fn early_halt_kinds() {
        let c = cal("GC");
        assert_eq!(c.session_close(d(2024, 1, 15)).unwrap(), et(2024, 1, 15, 13, 30));
        assert_eq!(c.session_close(d(2024, 7, 4)).unwrap(), et(2024, 7, 4, 13, 30));
        assert_eq!(c.session_close(d(2024, 5, 27)).unwrap(), et(2024, 5, 27, 14, 30));
        assert_eq!(c.session_close(d(2024, 11, 29)).unwrap(), et(2024, 11, 29, 14, 45));
        assert_eq!(c.session_close(d(2024, 12, 24)).unwrap(), et(2024, 12, 24, 13, 45));
        assert_eq!(c.session_close(d(2009, 5, 25)).unwrap(), et(2009, 5, 25, 13, 15));
        assert_eq!(c.session_close(d(2023, 9, 4)).unwrap(), et(2023, 9, 4, 13, 0));
        assert_eq!(c.session_close(d(2009, 5, 22)).unwrap(), et(2009, 5, 22, 16, 15));
        assert_ne!(cal("ES").session_close(d(2024, 7, 4)).unwrap(), c.session_close(d(2024, 7, 4)).unwrap());
    }

    #[test]
    fn closure_and_reopen() {
        let c = cal("GC");
        for dt in [d(2024, 12, 25), d(2024, 1, 1), d(2024, 3, 29), d(2025, 12, 25), d(2027, 12, 24)] {
            assert!(c.is_closed(dt).unwrap(), "{dt}");
            assert!(!c.is_session(dt).unwrap(), "{dt}");
        }
        assert_eq!(c.session_open(d(2024, 12, 26)).unwrap(), et(2024, 12, 25, 18, 0));
        assert_eq!(c.session_open(d(2024, 1, 2)).unwrap(), et(2024, 1, 1, 18, 0));
        assert_eq!(c.session_open(d(2024, 4, 1)).unwrap(), et(2024, 3, 31, 18, 0));
        // Christmas Day 2027 falls on a Saturday; the table closes Friday 2027-12-24
        assert_eq!(c.next_session(d(2027, 12, 23)).unwrap(), d(2027, 12, 27));
    }

    #[test]
    fn range_edges() {
        let gc = cal("GC");
        assert_eq!(gc.first_date(), d(2009, 1, 1));
        assert_eq!(gc.last_date(), d(2027, 12, 31));
        assert!(matches!(gc.is_session(d(2008, 12, 31)), Err(GlobexError::RootOutOfRange { root: "GC", .. })));
        assert!(matches!(gc.is_session(d(2028, 1, 1)), Err(GlobexError::RootOutOfRange { root: "GC", .. })));
        // MGC: first trade date 2010-10-04, first session opens Sunday 2010-10-03 18:00 ET
        let mgc = cal("MGC");
        assert_eq!(mgc.first_date(), d(2010, 10, 3));
        assert_eq!(mgc.session_open(d(2010, 10, 4)).unwrap(), et(2010, 10, 3, 18, 0));
        assert!(matches!(mgc.is_session(d(2010, 10, 1)), Err(GlobexError::RootOutOfRange { root: "MGC", .. })));
        assert!(mgc.previous_session(d(2010, 10, 4)).is_err());
        assert!(gc.is_session(d(2010, 10, 1)).unwrap());
    }
}

mod equity {
    use super::*;

    #[test]
    fn normal_day_and_weekend() {
        for root in ["ES", "MES", "NQ", "MNQ", "YM", "MYM", "RTY", "M2K"] {
            check_normal_and_weekend(root, d(2024, 3, 12));
        }
    }

    #[test]
    fn roots_route_to_equity_and_follow_the_free_functions() {
        for root in ["ES", "MES", "NQ", "MNQ", "YM", "MYM", "RTY", "M2K"] {
            let c = cal(root);
            assert_eq!(c.group(), Group::Equity, "{root}");
            // the same rows as the legacy equity functions
            for dt in [d(2024, 7, 4), d(2024, 11, 29), d(2024, 12, 24), d(2024, 12, 25), d(2024, 3, 29)] {
                assert_eq!(c.is_closed(dt).unwrap(), te_core::calendar::globex::is_closed(dt).unwrap(), "{root} {dt}");
                if c.is_session(dt).unwrap() {
                    assert_eq!(c.session_close(dt).unwrap(), te_core::calendar::globex::session_close(dt).unwrap());
                }
            }
        }
    }

    #[test]
    fn early_halt_kinds() {
        let c = cal("ES");
        assert_eq!(c.session_close(d(2024, 7, 4)).unwrap(), et(2024, 7, 4, 13, 0)); // 13:00
        assert_eq!(c.session_close(d(2024, 7, 3)).unwrap(), et(2024, 7, 3, 13, 15)); // 13:15
        assert_eq!(c.session_close(d(2024, 12, 24)).unwrap(), et(2024, 12, 24, 13, 15));
    }

    #[test]
    fn closure_and_reopen() {
        let c = cal("NQ");
        assert!(c.is_closed(d(2024, 12, 25)).unwrap());
        assert_eq!(c.session_open(d(2024, 12, 26)).unwrap(), et(2024, 12, 25, 18, 0));
        assert!(c.is_closed(d(2024, 3, 29)).unwrap());
        assert_eq!(c.session_open(d(2024, 4, 1)).unwrap(), et(2024, 3, 31, 18, 0));
    }

    #[test]
    fn range_edges() {
        for root in ["ES", "NQ"] {
            let c = cal(root);
            assert_eq!(c.first_date(), d(2006, 1, 1), "{root}");
            assert_eq!(c.last_date(), d(2027, 12, 31), "{root}");
        }
        // the micros open at their 2019-05-06 launch (Sunday 2019-05-05 18:00 ET); MNQ/MES had no floor before P6C
        for root in ["MNQ", "MES", "MYM", "M2K"] {
            let c = cal(root);
            assert_eq!(c.first_date(), d(2019, 5, 5), "{root}");
            assert_eq!(c.session_open(d(2019, 5, 6)).unwrap(), et(2019, 5, 5, 18, 0), "{root}");
            assert!(c.session_open(d(2019, 5, 3)).is_err(), "{root}");
        }
        // RTY on CME since 2017-07-10 (Sunday 2017-07-09 18:00 ET)
        let rty = cal("RTY");
        assert_eq!(rty.first_date(), d(2017, 7, 9));
        assert_eq!(rty.session_open(d(2017, 7, 10)).unwrap(), et(2017, 7, 9, 18, 0));
        assert!(matches!(rty.is_session(d(2017, 7, 7)), Err(GlobexError::RootOutOfRange { root: "RTY", .. })));
        // YM on Globex hours since 2008-01-27 (CBOT migration), not the equity table's 2006 start
        let ym = cal("YM");
        assert_eq!(ym.first_date(), d(2008, 1, 27));
        assert!(matches!(ym.is_session(d(2008, 1, 25)), Err(GlobexError::RootOutOfRange { root: "YM", .. })));
        assert_eq!(ym.session_open(d(2008, 1, 28)).unwrap(), et(2008, 1, 27, 18, 0));
        assert!(cal("ES").is_session(d(2008, 1, 25)).unwrap());
    }
}

#[test]
fn unknown_roots_refuse_and_all_supported_roots_resolve() {
    for bad in ["ZN", "6E", "SPY", "", "CLZ", "N"] {
        assert!(matches!(GlobexCalendar::for_root(bad), Err(GlobexError::UnknownRoot(_))), "{bad:?}");
    }
    let roots = supported_roots();
    assert_eq!(roots.len(), 12);
    for r in roots {
        assert!(GlobexCalendar::for_root(r).is_ok(), "{r}");
        assert!(GlobexCalendar::for_root(&r.to_lowercase()).is_ok(), "{r}");
    }
}

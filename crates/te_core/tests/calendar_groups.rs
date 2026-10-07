//! Per-group Globex calendars (P6C): energy (CL, MCL), metals (GC, MGC) and the equity-table roots, each by root.
//!
//! One module per group: a normal day, a weekend, each early-halt kind the group's table carries, a closure with
//! its reopen, and the range edges of every root. Expected instants are written as New York wall-clock times so
//! they read like the CME notices the tables cite.

use chrono::{DateTime, Datelike, NaiveDate, TimeZone, Utc};
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
        // MCL takes CL's range, not its own 2021-07-12 listing: mini data is served under the micro symbol and sims
        // size micros on mini history (P6C review)
        let mcl = cal("MCL");
        assert_eq!(mcl.first_date(), cl.first_date());
        assert_eq!(mcl.last_date(), cl.last_date());
        assert!(mcl.is_session(d(2021, 7, 9)).unwrap());
        assert!(matches!(mcl.is_session(d(2008, 12, 31)), Err(GlobexError::RootOutOfRange { root: "MCL", .. })));
        assert_eq!(mcl.session_open(d(2021, 7, 12)).unwrap(), et(2021, 7, 11, 18, 0));
        assert_eq!(mcl.previous_session(d(2009, 1, 5)).unwrap(), d(2009, 1, 2));
        assert!(mcl.previous_session(d(2009, 1, 2)).is_err());
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
        // MGC takes GC's range, not its own 2010-10-04 first trade date (mini data is served under the micro symbol)
        let mgc = cal("MGC");
        assert_eq!(mgc.first_date(), gc.first_date());
        assert_eq!(mgc.last_date(), gc.last_date());
        assert!(mgc.is_session(d(2010, 10, 1)).unwrap());
        assert_eq!(mgc.session_open(d(2010, 10, 4)).unwrap(), et(2010, 10, 3, 18, 0));
        assert!(matches!(mgc.is_session(d(2008, 12, 31)), Err(GlobexError::RootOutOfRange { root: "MGC", .. })));
        assert!(mgc.previous_session(d(2009, 1, 2)).is_err());
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
        // a micro takes its mini's range, not its 2019-05-06 listing (mini data is served under the micro symbol)
        for (micro, mini) in [("MNQ", "NQ"), ("MES", "ES"), ("MYM", "YM"), ("M2K", "RTY")] {
            let (m, n) = (cal(micro), cal(mini));
            assert_eq!(m.first_date(), n.first_date(), "{micro}");
            assert_eq!(m.last_date(), n.last_date(), "{micro}");
            assert!(m.is_session(d(2019, 5, 3)).unwrap(), "{micro}");
            assert_eq!(m.session_open(d(2019, 5, 6)).unwrap(), et(2019, 5, 5, 18, 0), "{micro}");
        }
        assert!(cal("MNQ").is_session(d(2006, 1, 3)).unwrap());
        assert!(cal("MES").is_session(d(2006, 1, 3)).unwrap());
        assert!(cal("MYM").is_session(d(2007, 12, 3)).is_err());
        assert!(cal("M2K").is_session(d(2017, 7, 7)).is_err());
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

/// The dated session-rule eras of the equity group (P6C review): a Friday 16:15 ET close to 2012-11-16, a 16:15-16:30 ET
/// halt inside weekday sessions to 2021-06-25, the default rule after. Energy and metals have none.
mod eras {
    use super::*;
    use te_core::calendar::globex::{parse_holiday_table, table_for, EQUITY_ERAS};

    #[test]
    fn friday_closes_1615_until_2012_11_16_then_1700() {
        for root in ["ES", "NQ", "MES", "YM"] {
            let c = cal(root);
            assert_eq!(c.session_close(d(2012, 11, 16)).unwrap(), et(2012, 11, 16, 16, 15), "{root}");
            assert_eq!(c.session_close(d(2010, 6, 4)).unwrap(), et(2010, 6, 4, 16, 15), "{root}");
            // the Friday after the change closes 17:00 and nothing else about it moved
            assert_eq!(c.session_close(d(2012, 11, 30)).unwrap(), et(2012, 11, 30, 17, 0), "{root}");
            assert_eq!(c.session_open(d(2012, 11, 30)).unwrap(), et(2012, 11, 29, 18, 0), "{root}");
            // a Mon-Thu session of the first era still closes 17:00
            assert_eq!(c.session_close(d(2012, 11, 15)).unwrap(), et(2012, 11, 15, 17, 0), "{root}");
        }
        // the legacy free functions are the equity calendar
        assert_eq!(te_core::calendar::globex::session_close(d(2012, 11, 16)).unwrap(), et(2012, 11, 16, 16, 15));
    }

    #[test]
    fn a_holiday_row_beats_the_era() {
        // the day after Thanksgiving 2008 was a Friday: the row's early close wins over the era's 16:15
        let c = cal("ES");
        let close = c.session_close(d(2008, 11, 28)).unwrap();
        assert!(close < et(2008, 11, 28, 16, 0), "{close}");
        assert_eq!(c.session_halt(d(2008, 11, 28)), None);
    }

    #[test]
    fn the_16_15_halt_is_inside_the_session_and_not_open() {
        let c = cal("ES");
        // Thursday of the first era, and a Wednesday of the second
        for td in [d(2012, 11, 15), d(2015, 3, 11), d(2021, 6, 24), d(2021, 6, 25)] {
            let (hs, he) = c.session_halt(td).unwrap();
            assert_eq!((hs, he), (et(td.year(), td.month(), td.day(), 16, 15), et(td.year(), td.month(), td.day(), 16, 30)));
            assert!(c.is_open_at(hs - chrono::Duration::minutes(1)).unwrap(), "{td}");
            assert!(!c.is_open_at(hs).unwrap(), "{td}");
            assert!(!c.is_open_at(he - chrono::Duration::minutes(1)).unwrap(), "{td}");
            assert!(c.is_open_at(he).unwrap(), "{td}");
            // the halt does not end the session: the trade date is still td, and an order placed in it keeps td
            assert_eq!(c.session_at(hs).unwrap(), Some(td));
            assert_eq!(c.session_or_next(hs).unwrap(), td);
            assert_eq!(c.session_close(td).unwrap(), et(td.year(), td.month(), td.day(), 17, 0));
        }
        // no halt in the first era's Friday (it has closed at 16:15), none on an early-close day
        assert_eq!(c.session_halt(d(2012, 11, 16)), None);
        assert_eq!(c.session_halt(d(2008, 11, 28)), None);
    }

    #[test]
    fn the_default_rule_is_byte_identical_after_the_last_era() {
        assert_eq!(EQUITY_ERAS.last().unwrap().to, d(2021, 6, 25));
        let plain = parse_holiday_table(include_str!("../data/cme_equity_holidays.csv")).unwrap();
        let eras = table_for(Group::Equity);
        let (mut n, mut differs_before) = (0, 0);
        for (a, b) in plain.sessions.iter().zip(eras.sessions.iter()) {
            assert_eq!(a.trade_date, b.trade_date);
            if a.trade_date > d(2021, 6, 25) {
                n += 1;
                assert_eq!((a.open_utc, a.close_utc), (b.open_utc, b.close_utc), "{}", a.trade_date);
                assert_eq!(b.halt_utc, None, "{}", a.trade_date);
            } else if a.close_utc != b.close_utc || b.halt_utc.is_some() {
                differs_before += 1;
            }
        }
        assert!(n > 1000 && differs_before > 3000, "{n} {differs_before}");
        assert_eq!(plain.sessions.len(), eras.sessions.len());
        // the era only ever moves a close earlier, to 16:15, and never touches an open
        for (a, b) in plain.sessions.iter().zip(eras.sessions.iter()) {
            assert_eq!(a.open_utc, b.open_utc, "{}", a.trade_date);
            assert!(b.close_utc <= a.close_utc, "{}", a.trade_date);
        }
    }

    #[test]
    fn energy_and_metals_have_no_eras() {
        for g in [Group::Energy, Group::Metals] {
            assert!(te_core::calendar::globex::eras_for(g).is_empty());
            assert!(table_for(g).sessions.iter().all(|s| s.halt_utc.is_none()));
        }
        // the 2016-10-28 Friday that CL and GC bars close at 16:15 is an observed one-off, not a rule: the table says 17:00
        assert_eq!(cal("CL").session_close(d(2016, 10, 28)).unwrap(), et(2016, 10, 28, 17, 0));
        assert!(cal("CL").is_open_at(et(2016, 10, 28, 16, 20)).unwrap());
        let c = cal("MES");
        assert!(!c.is_open_at(et(2012, 11, 15, 16, 20)).unwrap());
        assert!(cal("CL").is_open_at(et(2012, 11, 15, 16, 20)).unwrap());
    }
}

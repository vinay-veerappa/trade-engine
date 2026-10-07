//! Date-by-date comparison of CME Globex equity session calendar against
//! pandas_market_calendars `CME_Equity` oracle fixture (P6B plan §0.1 and T2).
//!
//! Where they differ, the CME published table wins. Every disagreement is explicitly
//! recorded in the allow-list with its CME notice source date. The test fails on any
//! unlisted difference, and fails on any allow-listed difference that has gone away.

use std::collections::{HashMap, HashSet};

use chrono::{DateTime, NaiveDate, Utc};
use te_core::calendar::globex;

const PMC_FIXTURE_CSV: &str = include_str!("fixtures/cme_pmc_fixture.csv");

struct AllowListEntry {
    date: (i32, u32, u32),
    cme_source_date: Option<(i32, u32, u32)>,
    notes: &'static str,
}

/// The explicit allow-list of all 162 holiday and special-date disagreements between
/// CME's published equity holiday schedule and `pandas_market_calendars` `CME_Equity`.
static HOLIDAY_ALLOW_LIST: &[AllowListEntry] = &[
    AllowListEntry { date: (2006, 1, 3), cme_source_date: Some((2006, 1, 2)), notes: "Reopen from CME 2006-01-02 closed reopen 18:00 ET" },
    AllowListEntry { date: (2006, 1, 16), cme_source_date: Some((2006, 1, 16)), notes: "CME early_halt halt 11:30 ET" },
    AllowListEntry { date: (2006, 2, 20), cme_source_date: Some((2006, 2, 20)), notes: "CME early_halt halt 11:30 ET reopen 18:00 ET" },
    AllowListEntry { date: (2006, 2, 21), cme_source_date: Some((2006, 2, 20)), notes: "Reopen from CME 2006-02-20 early_halt reopen 18:00 ET" },
    AllowListEntry { date: (2006, 4, 17), cme_source_date: Some((2006, 4, 14)), notes: "Reopen from CME 2006-04-14 closed reopen 2006-04-16 18:00 ET" },
    AllowListEntry { date: (2006, 5, 29), cme_source_date: Some((2006, 5, 29)), notes: "CME early_halt halt 11:30 ET reopen 18:00 ET" },
    AllowListEntry { date: (2006, 5, 30), cme_source_date: Some((2006, 5, 29)), notes: "Reopen from CME 2006-05-29 early_halt reopen 18:00 ET" },
    AllowListEntry { date: (2006, 7, 3), cme_source_date: Some((2006, 7, 3)), notes: "CME early_halt halt 13:15 ET" },
    AllowListEntry { date: (2006, 7, 4), cme_source_date: Some((2006, 7, 4)), notes: "CME early_halt halt 11:30 ET" },
    AllowListEntry { date: (2006, 9, 4), cme_source_date: Some((2006, 9, 4)), notes: "CME early_halt halt 11:30 ET reopen 18:00 ET" },
    AllowListEntry { date: (2006, 9, 5), cme_source_date: Some((2006, 9, 4)), notes: "Reopen from CME 2006-09-04 early_halt reopen 18:00 ET" },
    AllowListEntry { date: (2006, 11, 23), cme_source_date: Some((2006, 11, 23)), notes: "CME early_halt halt 11:30 ET reopen 18:00 ET" },
    AllowListEntry { date: (2006, 11, 24), cme_source_date: Some((2006, 11, 24)), notes: "CME early_halt halt 13:15 ET" },
    AllowListEntry { date: (2006, 12, 26), cme_source_date: Some((2006, 12, 25)), notes: "Reopen from CME 2006-12-25 closed reopen 18:00 ET" },
    AllowListEntry { date: (2007, 1, 15), cme_source_date: Some((2007, 1, 15)), notes: "CME early_halt halt 11:30 ET reopen 18:00 ET" },
    AllowListEntry { date: (2007, 1, 16), cme_source_date: Some((2007, 1, 15)), notes: "Reopen from CME 2007-01-15 early_halt reopen 18:00 ET" },
    AllowListEntry { date: (2007, 2, 19), cme_source_date: Some((2007, 2, 19)), notes: "CME early_halt halt 11:30 ET reopen 18:00 ET" },
    AllowListEntry { date: (2007, 2, 20), cme_source_date: Some((2007, 2, 19)), notes: "Reopen from CME 2007-02-19 early_halt reopen 18:00 ET" },
    AllowListEntry { date: (2007, 4, 6), cme_source_date: Some((2007, 4, 6)), notes: "CME early_halt halt 09:15 ET; PMC marked closed while CME table open" },
    AllowListEntry { date: (2007, 5, 28), cme_source_date: Some((2007, 5, 28)), notes: "CME early_halt halt 11:30 ET reopen 18:00 ET" },
    AllowListEntry { date: (2007, 5, 29), cme_source_date: Some((2007, 5, 28)), notes: "Reopen from CME 2007-05-28 early_halt reopen 18:00 ET" },
    AllowListEntry { date: (2007, 7, 3), cme_source_date: Some((2007, 7, 3)), notes: "CME early_halt halt 13:15 ET" },
    AllowListEntry { date: (2007, 7, 4), cme_source_date: Some((2007, 7, 4)), notes: "CME early_halt halt 11:30 ET reopen 18:00 ET" },
    AllowListEntry { date: (2007, 7, 5), cme_source_date: Some((2007, 7, 4)), notes: "Reopen from CME 2007-07-04 early_halt reopen 18:00 ET" },
    AllowListEntry { date: (2007, 9, 3), cme_source_date: Some((2007, 9, 3)), notes: "CME early_halt halt 11:30 ET reopen 18:00 ET" },
    AllowListEntry { date: (2007, 9, 4), cme_source_date: Some((2007, 9, 3)), notes: "Reopen from CME 2007-09-03 early_halt reopen 18:00 ET" },
    AllowListEntry { date: (2007, 11, 22), cme_source_date: Some((2007, 11, 22)), notes: "CME early_halt halt 11:30 ET reopen 18:00 ET" },
    AllowListEntry { date: (2007, 11, 23), cme_source_date: Some((2007, 11, 23)), notes: "CME early_halt halt 13:15 ET" },
    AllowListEntry { date: (2007, 12, 24), cme_source_date: Some((2007, 12, 24)), notes: "CME early_halt halt 13:15 ET reopen 2007-12-26 06:00 ET" },
    AllowListEntry { date: (2007, 12, 26), cme_source_date: Some((2007, 12, 24)), notes: "Reopen from CME 2007-12-24 early_halt reopen 2007-12-26 06:00 ET" },
    AllowListEntry { date: (2008, 1, 2), cme_source_date: Some((2008, 1, 1)), notes: "Reopen from CME 2008-01-01 closed reopen 2008-01-02 06:00 ET" },
    AllowListEntry { date: (2008, 1, 21), cme_source_date: Some((2008, 1, 21)), notes: "CME early_halt halt 11:30 ET" },
    AllowListEntry { date: (2008, 2, 18), cme_source_date: Some((2008, 2, 18)), notes: "CME early_halt halt 11:30 ET" },
    AllowListEntry { date: (2008, 3, 24), cme_source_date: Some((2008, 3, 21)), notes: "Reopen from CME 2008-03-21 closed reopen 2008-03-23 18:00 ET" },
    AllowListEntry { date: (2008, 5, 26), cme_source_date: Some((2008, 5, 26)), notes: "CME early_halt halt 11:30 ET" },
    AllowListEntry { date: (2008, 7, 3), cme_source_date: Some((2008, 7, 3)), notes: "CME early_halt halt 13:15 ET" },
    AllowListEntry { date: (2008, 7, 4), cme_source_date: Some((2008, 7, 4)), notes: "CME early_halt halt 11:30 ET" },
    AllowListEntry { date: (2008, 9, 1), cme_source_date: Some((2008, 9, 1)), notes: "CME early_halt halt 11:30 ET reopen 18:00 ET" },
    AllowListEntry { date: (2008, 9, 2), cme_source_date: Some((2008, 9, 1)), notes: "Reopen from CME 2008-09-01 early_halt reopen 18:00 ET" },
    AllowListEntry { date: (2008, 11, 27), cme_source_date: Some((2008, 11, 27)), notes: "CME early_halt halt 11:30 ET" },
    AllowListEntry { date: (2008, 11, 28), cme_source_date: Some((2008, 11, 28)), notes: "CME early_halt halt 13:15 ET" },
    AllowListEntry { date: (2008, 12, 24), cme_source_date: Some((2008, 12, 24)), notes: "CME early_halt halt 13:15 ET reopen 2008-12-26 06:00 ET" },
    AllowListEntry { date: (2008, 12, 26), cme_source_date: Some((2008, 12, 24)), notes: "Reopen from CME 2008-12-24 early_halt reopen 2008-12-26 06:00 ET" },
    AllowListEntry { date: (2009, 1, 2), cme_source_date: Some((2009, 1, 1)), notes: "Reopen from CME 2009-01-01 closed reopen 2009-01-02 06:00 ET" },
    AllowListEntry { date: (2009, 1, 19), cme_source_date: Some((2009, 1, 19)), notes: "CME early_halt halt 11:30 ET reopen 18:00 ET" },
    AllowListEntry { date: (2009, 1, 20), cme_source_date: Some((2009, 1, 19)), notes: "Reopen from CME 2009-01-19 early_halt reopen 18:00 ET" },
    AllowListEntry { date: (2009, 2, 16), cme_source_date: Some((2009, 2, 16)), notes: "CME early_halt halt 11:30 ET reopen 18:00 ET" },
    AllowListEntry { date: (2009, 2, 17), cme_source_date: Some((2009, 2, 16)), notes: "Reopen from CME 2009-02-16 early_halt reopen 18:00 ET" },
    AllowListEntry { date: (2009, 4, 13), cme_source_date: Some((2009, 4, 10)), notes: "Reopen from CME 2009-04-10 closed reopen 2009-04-12 18:00 ET" },
    AllowListEntry { date: (2009, 5, 25), cme_source_date: Some((2009, 5, 25)), notes: "CME early_halt halt 11:30 ET reopen 18:00 ET" },
    AllowListEntry { date: (2009, 5, 26), cme_source_date: Some((2009, 5, 25)), notes: "Reopen from CME 2009-05-25 early_halt reopen 18:00 ET" },
    AllowListEntry { date: (2009, 7, 3), cme_source_date: Some((2009, 7, 3)), notes: "CME early_halt halt 11:30 ET reopen 2009-07-05 18:00 ET" },
    AllowListEntry { date: (2009, 7, 6), cme_source_date: Some((2009, 7, 3)), notes: "Reopen from CME 2009-07-03 early_halt reopen 2009-07-05 18:00 ET" },
    AllowListEntry { date: (2009, 9, 7), cme_source_date: Some((2009, 9, 7)), notes: "CME early_halt halt 11:30 ET reopen 18:00 ET" },
    AllowListEntry { date: (2009, 9, 8), cme_source_date: Some((2009, 9, 7)), notes: "Reopen from CME 2009-09-07 early_halt reopen 18:00 ET" },
    AllowListEntry { date: (2009, 11, 26), cme_source_date: Some((2009, 11, 26)), notes: "CME early_halt halt 11:30 ET reopen 18:00 ET" },
    AllowListEntry { date: (2009, 11, 27), cme_source_date: Some((2009, 11, 27)), notes: "CME early_halt halt 13:15 ET" },
    AllowListEntry { date: (2009, 12, 24), cme_source_date: Some((2009, 12, 24)), notes: "CME early_halt halt 13:15 ET" },
    AllowListEntry { date: (2009, 12, 28), cme_source_date: Some((2009, 12, 25)), notes: "Reopen from CME 2009-12-25 closed reopen 2009-12-27 18:00 ET" },
    AllowListEntry { date: (2010, 1, 4), cme_source_date: Some((2010, 1, 1)), notes: "Reopen from CME 2010-01-01 closed reopen 2010-01-03 18:00 ET" },
    AllowListEntry { date: (2010, 1, 18), cme_source_date: Some((2010, 1, 18)), notes: "CME early_halt halt 11:30 ET reopen 18:00 ET" },
    AllowListEntry { date: (2010, 1, 19), cme_source_date: Some((2010, 1, 18)), notes: "Reopen from CME 2010-01-18 early_halt reopen 18:00 ET" },
    AllowListEntry { date: (2010, 2, 15), cme_source_date: Some((2010, 2, 15)), notes: "CME early_halt halt 11:30 ET reopen 18:00 ET" },
    AllowListEntry { date: (2010, 2, 16), cme_source_date: Some((2010, 2, 15)), notes: "Reopen from CME 2010-02-15 early_halt reopen 18:00 ET" },
    AllowListEntry { date: (2010, 4, 2), cme_source_date: Some((2010, 4, 2)), notes: "CME early_halt halt 09:15 ET reopen 2010-04-04 18:00 ET" },
    AllowListEntry { date: (2010, 4, 5), cme_source_date: Some((2010, 4, 2)), notes: "Reopen from CME 2010-04-02 early_halt reopen 2010-04-04 18:00 ET" },
    AllowListEntry { date: (2010, 5, 31), cme_source_date: Some((2010, 5, 31)), notes: "CME early_halt halt 11:30 ET reopen 18:00 ET" },
    AllowListEntry { date: (2010, 6, 1), cme_source_date: Some((2010, 5, 31)), notes: "Reopen from CME 2010-05-31 early_halt reopen 18:00 ET" },
    AllowListEntry { date: (2010, 7, 5), cme_source_date: Some((2010, 7, 5)), notes: "CME early_halt halt 11:30 ET reopen 18:00 ET" },
    AllowListEntry { date: (2010, 7, 6), cme_source_date: Some((2010, 7, 5)), notes: "Reopen from CME 2010-07-05 early_halt reopen 18:00 ET" },
    AllowListEntry { date: (2010, 9, 6), cme_source_date: Some((2010, 9, 6)), notes: "CME early_halt halt 11:30 ET reopen 18:00 ET" },
    AllowListEntry { date: (2010, 9, 7), cme_source_date: Some((2010, 9, 6)), notes: "Reopen from CME 2010-09-06 early_halt reopen 18:00 ET" },
    AllowListEntry { date: (2010, 11, 25), cme_source_date: Some((2010, 11, 25)), notes: "CME early_halt halt 11:30 ET reopen 18:00 ET" },
    AllowListEntry { date: (2010, 11, 26), cme_source_date: Some((2010, 11, 26)), notes: "CME early_halt halt 13:15 ET" },
    AllowListEntry { date: (2010, 12, 27), cme_source_date: Some((2010, 12, 24)), notes: "Reopen from CME 2010-12-24 closed reopen 2010-12-26 18:00 ET" },
    AllowListEntry { date: (2011, 1, 17), cme_source_date: Some((2011, 1, 17)), notes: "CME early_halt halt 11:30 ET reopen 18:00 ET" },
    AllowListEntry { date: (2011, 1, 18), cme_source_date: Some((2011, 1, 17)), notes: "Reopen from CME 2011-01-17 early_halt reopen 18:00 ET" },
    AllowListEntry { date: (2011, 2, 21), cme_source_date: Some((2011, 2, 21)), notes: "CME early_halt halt 11:30 ET reopen 18:00 ET" },
    AllowListEntry { date: (2011, 2, 22), cme_source_date: Some((2011, 2, 21)), notes: "Reopen from CME 2011-02-21 early_halt reopen 18:00 ET" },
    AllowListEntry { date: (2011, 4, 25), cme_source_date: Some((2011, 4, 22)), notes: "Reopen from CME 2011-04-22 closed reopen 2011-04-24 18:00 ET" },
    AllowListEntry { date: (2011, 5, 30), cme_source_date: Some((2011, 5, 30)), notes: "CME early_halt halt 11:30 ET reopen 18:00 ET" },
    AllowListEntry { date: (2011, 5, 31), cme_source_date: Some((2011, 5, 30)), notes: "Reopen from CME 2011-05-30 early_halt reopen 18:00 ET" },
    AllowListEntry { date: (2011, 7, 4), cme_source_date: Some((2011, 7, 4)), notes: "CME early_halt halt 11:30 ET reopen 18:00 ET" },
    AllowListEntry { date: (2011, 7, 5), cme_source_date: Some((2011, 7, 4)), notes: "Reopen from CME 2011-07-04 early_halt reopen 18:00 ET" },
    AllowListEntry { date: (2011, 9, 5), cme_source_date: Some((2011, 9, 5)), notes: "CME early_halt halt 11:30 ET reopen 18:00 ET" },
    AllowListEntry { date: (2011, 9, 6), cme_source_date: Some((2011, 9, 5)), notes: "Reopen from CME 2011-09-05 early_halt reopen 18:00 ET" },
    AllowListEntry { date: (2011, 11, 24), cme_source_date: Some((2011, 11, 24)), notes: "CME early_halt halt 11:30 ET reopen 18:00 ET" },
    AllowListEntry { date: (2011, 11, 25), cme_source_date: Some((2011, 11, 25)), notes: "CME early_halt halt 13:15 ET" },
    AllowListEntry { date: (2011, 12, 27), cme_source_date: Some((2011, 12, 26)), notes: "Reopen from CME 2011-12-26 closed reopen 2011-12-27 06:00 ET" },
    AllowListEntry { date: (2012, 1, 3), cme_source_date: Some((2012, 1, 2)), notes: "Reopen from CME 2012-01-02 closed reopen 2012-01-03 06:00 ET" },
    AllowListEntry { date: (2012, 1, 16), cme_source_date: Some((2012, 1, 16)), notes: "CME early_halt halt 11:30 ET reopen 18:00 ET" },
    AllowListEntry { date: (2012, 1, 17), cme_source_date: Some((2012, 1, 16)), notes: "Reopen from CME 2012-01-16 early_halt reopen 18:00 ET" },
    AllowListEntry { date: (2012, 2, 20), cme_source_date: Some((2012, 2, 20)), notes: "CME early_halt halt 11:30 ET reopen 18:00 ET" },
    AllowListEntry { date: (2012, 2, 21), cme_source_date: Some((2012, 2, 20)), notes: "Reopen from CME 2012-02-20 early_halt reopen 18:00 ET" },
    AllowListEntry { date: (2012, 4, 6), cme_source_date: Some((2012, 4, 6)), notes: "CME early_halt halt 09:15 ET reopen 2012-04-08 18:00 ET" },
    AllowListEntry { date: (2012, 4, 9), cme_source_date: Some((2012, 4, 6)), notes: "Reopen from CME 2012-04-06 early_halt reopen 2012-04-08 18:00 ET" },
    AllowListEntry { date: (2012, 5, 28), cme_source_date: Some((2012, 5, 28)), notes: "CME early_halt halt 11:30 ET reopen 18:00 ET" },
    AllowListEntry { date: (2012, 5, 29), cme_source_date: Some((2012, 5, 28)), notes: "Reopen from CME 2012-05-28 early_halt reopen 18:00 ET" },
    AllowListEntry { date: (2012, 7, 3), cme_source_date: Some((2012, 7, 3)), notes: "CME early_halt halt 13:15 ET reopen 16:30 ET" },
    AllowListEntry { date: (2012, 7, 4), cme_source_date: Some((2012, 7, 4)), notes: "CME early_halt halt 11:30 ET reopen 18:00 ET" },
    AllowListEntry { date: (2012, 7, 5), cme_source_date: Some((2012, 7, 4)), notes: "Reopen from CME 2012-07-04 early_halt reopen 18:00 ET" },
    AllowListEntry { date: (2012, 9, 3), cme_source_date: Some((2012, 9, 3)), notes: "CME early_halt halt 11:30 ET reopen 18:00 ET" },
    AllowListEntry { date: (2012, 9, 4), cme_source_date: Some((2012, 9, 3)), notes: "Reopen from CME 2012-09-03 early_halt reopen 18:00 ET" },
    AllowListEntry { date: (2012, 10, 29), cme_source_date: Some((2012, 10, 29)), notes: "CME early_halt halt 09:15 ET" },
    AllowListEntry { date: (2012, 10, 30), cme_source_date: Some((2012, 10, 30)), notes: "CME early_halt halt 09:15 ET" },
    AllowListEntry { date: (2012, 10, 31), cme_source_date: Some((2012, 10, 30)), notes: "Reopen after Hurricane Sandy early halt" },
    AllowListEntry { date: (2012, 11, 22), cme_source_date: Some((2012, 11, 22)), notes: "CME early_halt halt 11:30 ET reopen 18:00 ET" },
    AllowListEntry { date: (2012, 11, 23), cme_source_date: Some((2012, 11, 23)), notes: "CME early_halt halt 13:15 ET" },
    AllowListEntry { date: (2012, 12, 24), cme_source_date: Some((2012, 12, 24)), notes: "CME early_halt halt 13:15 ET" },
    AllowListEntry { date: (2012, 12, 26), cme_source_date: Some((2012, 12, 25)), notes: "Reopen from CME 2012-12-25 closed reopen 2012-12-26 06:00 ET" },
    AllowListEntry { date: (2013, 1, 2), cme_source_date: Some((2013, 1, 1)), notes: "Reopen from CME 2013-01-01 closed reopen 2013-01-02 06:00 ET" },
    AllowListEntry { date: (2013, 1, 21), cme_source_date: Some((2013, 1, 21)), notes: "CME early_halt halt 11:30 ET reopen 18:00 ET" },
    AllowListEntry { date: (2013, 2, 18), cme_source_date: Some((2013, 2, 18)), notes: "CME early_halt halt 11:30 ET reopen 18:00 ET" },
    AllowListEntry { date: (2013, 5, 27), cme_source_date: Some((2013, 5, 27)), notes: "CME early_halt halt 11:30 ET reopen 18:00 ET" },
    AllowListEntry { date: (2013, 7, 3), cme_source_date: Some((2013, 7, 3)), notes: "CME early_halt halt 13:15 ET reopen 18:00 ET" },
    AllowListEntry { date: (2013, 7, 4), cme_source_date: Some((2013, 7, 4)), notes: "CME early_halt halt 11:30 ET reopen 18:00 ET" },
    AllowListEntry { date: (2013, 9, 2), cme_source_date: Some((2013, 9, 2)), notes: "CME early_halt halt 11:30 ET reopen 18:00 ET" },
    AllowListEntry { date: (2013, 11, 28), cme_source_date: Some((2013, 11, 28)), notes: "CME early_halt halt 11:30 ET reopen 18:00 ET" },
    AllowListEntry { date: (2013, 11, 29), cme_source_date: Some((2013, 11, 29)), notes: "CME early_halt halt 13:15 ET" },
    AllowListEntry { date: (2013, 12, 24), cme_source_date: Some((2013, 12, 24)), notes: "CME early_halt halt 13:15 ET" },
    AllowListEntry { date: (2013, 12, 26), cme_source_date: Some((2013, 12, 25)), notes: "Reopen from CME 2013-12-25 closed reopen 2013-12-26 06:00 ET" },
    AllowListEntry { date: (2014, 1, 2), cme_source_date: Some((2014, 1, 1)), notes: "Reopen from CME 2014-01-01 closed reopen 2014-01-02 06:00 ET" },
    AllowListEntry { date: (2014, 1, 20), cme_source_date: Some((2014, 1, 20)), notes: "CME early_halt halt 11:30 ET reopen 18:00 ET" },
    AllowListEntry { date: (2014, 2, 17), cme_source_date: Some((2014, 2, 17)), notes: "CME early_halt halt 11:30 ET reopen 18:00 ET" },
    AllowListEntry { date: (2014, 7, 3), cme_source_date: Some((2014, 7, 3)), notes: "CME early_halt halt 13:15 ET reopen 18:00 ET" },
    AllowListEntry { date: (2014, 11, 28), cme_source_date: Some((2014, 11, 28)), notes: "CME early_halt halt 13:15 ET" },
    AllowListEntry { date: (2014, 12, 24), cme_source_date: Some((2014, 12, 24)), notes: "CME early_halt halt 13:15 ET" },
    AllowListEntry { date: (2015, 7, 2), cme_source_date: Some((2015, 7, 2)), notes: "CME early_halt halt 13:15 ET reopen 18:00 ET" },
    AllowListEntry { date: (2015, 11, 27), cme_source_date: Some((2015, 11, 27)), notes: "CME early_halt halt 13:15 ET" },
    AllowListEntry { date: (2015, 12, 24), cme_source_date: Some((2015, 12, 24)), notes: "CME early_halt halt 13:15 ET" },
    AllowListEntry { date: (2016, 11, 25), cme_source_date: Some((2016, 11, 25)), notes: "CME early_halt halt 13:15 ET" },
    AllowListEntry { date: (2017, 7, 3), cme_source_date: Some((2017, 7, 3)), notes: "CME early_halt halt 13:15 ET reopen 18:00 ET" },
    AllowListEntry { date: (2017, 11, 24), cme_source_date: Some((2017, 11, 24)), notes: "CME early_halt halt 13:15 ET" },
    AllowListEntry { date: (2018, 7, 3), cme_source_date: Some((2018, 7, 3)), notes: "CME early_halt halt 13:15 ET reopen 18:00 ET" },
    AllowListEntry { date: (2018, 11, 23), cme_source_date: Some((2018, 11, 23)), notes: "CME early_halt halt 13:15 ET" },
    AllowListEntry { date: (2018, 12, 5), cme_source_date: None, notes: "Deliberately unfilled missing date in CME table (MD doc); PMC marked closed while CME table open" },
    AllowListEntry { date: (2018, 12, 24), cme_source_date: Some((2018, 12, 24)), notes: "CME early_halt halt 13:15 ET reopen 2018-12-25 18:00 ET" },
    AllowListEntry { date: (2019, 7, 3), cme_source_date: Some((2019, 7, 3)), notes: "CME early_halt halt 13:15 ET reopen 18:00 ET" },
    AllowListEntry { date: (2019, 11, 29), cme_source_date: Some((2019, 11, 29)), notes: "CME early_halt halt 13:15 ET" },
    AllowListEntry { date: (2019, 12, 24), cme_source_date: Some((2019, 12, 24)), notes: "CME early_halt halt 13:15 ET reopen 2019-12-25 18:00 ET" },
    AllowListEntry { date: (2020, 11, 27), cme_source_date: Some((2020, 11, 27)), notes: "CME early_halt halt 13:15 ET" },
    AllowListEntry { date: (2020, 12, 24), cme_source_date: Some((2020, 12, 24)), notes: "CME early_halt halt 13:15 ET reopen 2020-12-27 18:00 ET" },
    AllowListEntry { date: (2021, 11, 26), cme_source_date: Some((2021, 11, 26)), notes: "CME early_halt halt 13:15 ET" },
    AllowListEntry { date: (2022, 11, 25), cme_source_date: Some((2022, 11, 25)), notes: "CME early_halt halt 13:15 ET" },
    AllowListEntry { date: (2023, 1, 16), cme_source_date: None, notes: "Deliberately unfilled missing date in CME table (MD doc)" },
    AllowListEntry { date: (2023, 2, 20), cme_source_date: None, notes: "Deliberately unfilled missing date in CME table (MD doc)" },
    AllowListEntry { date: (2023, 4, 7), cme_source_date: None, notes: "Deliberately unfilled missing date in CME table (MD doc)" },
    AllowListEntry { date: (2023, 7, 3), cme_source_date: Some((2023, 7, 3)), notes: "CME early_halt halt 13:15 ET reopen 18:00 ET" },
    AllowListEntry { date: (2023, 11, 24), cme_source_date: Some((2023, 11, 24)), notes: "CME early_halt halt 13:15 ET" },
    AllowListEntry { date: (2024, 3, 29), cme_source_date: Some((2024, 03, 29)), notes: "CME closed; PMC marked open while CME table closed" },
    AllowListEntry { date: (2024, 7, 3), cme_source_date: Some((2024, 7, 3)), notes: "CME early_halt halt 13:15 ET reopen 18:00 ET" },
    AllowListEntry { date: (2024, 11, 29), cme_source_date: Some((2024, 11, 29)), notes: "CME early_halt halt 13:15 ET" },
    AllowListEntry { date: (2024, 12, 24), cme_source_date: Some((2024, 12, 24)), notes: "CME early_halt halt 13:15 ET" },
    AllowListEntry { date: (2025, 1, 9), cme_source_date: Some((2025, 1, 9)), notes: "CME early_halt halt 13:00 ET; PMC marked closed while CME table open" },
    AllowListEntry { date: (2025, 4, 18), cme_source_date: Some((2025, 4, 18)), notes: "CME closed; PMC marked open while CME table closed" },
    AllowListEntry { date: (2025, 7, 3), cme_source_date: Some((2025, 7, 3)), notes: "CME early_halt halt 13:15 ET reopen 18:00 ET" },
    AllowListEntry { date: (2025, 11, 28), cme_source_date: Some((2025, 11, 28)), notes: "CME early_halt halt 13:15 ET" },
    AllowListEntry { date: (2025, 12, 24), cme_source_date: Some((2025, 12, 24)), notes: "CME early_halt halt 13:15 ET reopen 2025-12-25 18:00 ET" },
    AllowListEntry { date: (2026, 11, 27), cme_source_date: Some((2026, 11, 27)), notes: "CME early_halt halt 13:15 ET" },
    AllowListEntry { date: (2026, 12, 24), cme_source_date: Some((2026, 12, 24)), notes: "CME early_halt halt 13:15 ET" },
    AllowListEntry { date: (2027, 3, 26), cme_source_date: Some((2027, 3, 26)), notes: "CME closed; PMC marked open while CME table closed" },
    AllowListEntry { date: (2027, 11, 26), cme_source_date: Some((2027, 11, 26)), notes: "CME early_halt halt 13:15 ET" },
];

fn parse_fixture() -> HashMap<NaiveDate, (DateTime<Utc>, DateTime<Utc>)> {
    let mut map = HashMap::new();
    for line in PMC_FIXTURE_CSV.lines() {
        let line = line.trim();
        if line.is_empty() || line.starts_with('#') || line.starts_with("date") {
            continue;
        }
        let parts: Vec<&str> = line.split(',').collect();
        assert_eq!(parts.len(), 3, "fixture row format date,open,close");
        let date = NaiveDate::parse_from_str(parts[0], "%Y-%m-%d").expect("valid date in fixture");
        let open = DateTime::parse_from_rfc3339(parts[1])
            .expect("valid open instant")
            .with_timezone(&Utc);
        let close = DateTime::parse_from_rfc3339(parts[2])
            .expect("valid close instant")
            .with_timezone(&Utc);
        map.insert(date, (open, close));
    }
    map
}

#[test]
fn test_cme_vs_pandas_market_calendars_equity() {
    let pmc_sessions = parse_fixture();
    let first = globex::first_date();
    let last = globex::last_date();

    let all_table_dates = globex::sessions_in_range(first, last).unwrap();
    let table_date_set: HashSet<NaiveDate> = all_table_dates.iter().copied().collect();

    let mut all_dates: Vec<NaiveDate> = table_date_set.union(&pmc_sessions.keys().copied().collect()).copied().collect();
    all_dates.sort();

    // Map allow-list entries by date
    let mut allow_map: HashMap<NaiveDate, &AllowListEntry> = HashMap::new();
    for entry in HOLIDAY_ALLOW_LIST {
        let d = NaiveDate::from_ymd_opt(entry.date.0, entry.date.1, entry.date.2).unwrap();
        allow_map.insert(d, entry);
    }
    assert_eq!(allow_map.len(), HOLIDAY_ALLOW_LIST.len());

    let mut allow_list_hits = HashSet::new();
    let mut pre2012_pit_diff_count = 0;
    let mut exact_matches = 0;
    let pit_cutoff = NaiveDate::from_ymd_opt(2012, 11, 20).unwrap();

    for &d in &all_dates {
        let in_table = table_date_set.contains(&d);
        let pmc_session = pmc_sessions.get(&d).copied();

        let table_times = if in_table {
            Some((globex::session_open(d).unwrap(), globex::session_close(d).unwrap()))
        } else {
            None
        };

        let is_diff = match (table_times, pmc_session) {
            (Some((to, tc)), Some((po, pc))) => to != po || tc != pc,
            (Some(_), None) => true,
            (None, Some(_)) => true,
            (None, None) => false,
        };

        if !is_diff {
            exact_matches += 1;
            continue;
        }

        // It differs: where they differ, the CME TABLE wins.
        if let Some(entry) = allow_map.get(&d) {
            allow_list_hits.insert(d);
            // Verify entry has an explanation and record
            assert!(!entry.notes.is_empty());
            if let Some(src) = entry.cme_source_date {
                assert!(src.0 >= 2006 && src.0 <= 2027);
            }
        } else if d < pit_cutoff && in_table && pmc_session.is_some() {
            // PMC CME_Equity prior to 2012-11-20 models open-outcry pit hours (15:30 CT open / 15:15 CT close)
            // rather than modern Globex electronic hours (18:00 ET / 17:00 ET).
            pre2012_pit_diff_count += 1;
        } else {
            panic!(
                "Unlisted difference on {d}: table={:?} pmc={:?}",
                table_times, pmc_session
            );
        }
    }

    // "fails on any allow-listed difference that has gone away"
    for (&d, entry) in &allow_map {
        assert!(
            allow_list_hits.contains(&d),
            "Allow-listed difference on {:?} ({}) has gone away!",
            d,
            entry.notes
        );
    }

    assert_eq!(HOLIDAY_ALLOW_LIST.len(), 162);
    assert_eq!(allow_list_hits.len(), 162);
    assert_eq!(pre2012_pit_diff_count, 1672);
    assert_eq!(exact_matches, 3851);
}

// ---------------------------------------------------------------------------------------------------------------
// P6C: energy (CL, MCL) and metals (GC, MGC) against pandas_market_calendars `CMEGlobex_CL` / `CMEGlobex_GC`.
// ---------------------------------------------------------------------------------------------------------------

const PMC_ENERGY_CSV: &str = include_str!("fixtures/cme_pmc_fixture_energy.csv");
const PMC_METALS_CSV: &str = include_str!("fixtures/cme_pmc_fixture_metals.csv");

fn parse_csv(text: &str) -> HashMap<NaiveDate, (DateTime<Utc>, DateTime<Utc>)> {
    let mut map = HashMap::new();
    for line in text.lines() {
        let line = line.trim();
        if line.is_empty() || line.starts_with('#') || line.starts_with("date") {
            continue;
        }
        let p: Vec<&str> = line.split(',').collect();
        assert_eq!(p.len(), 3);
        let d = NaiveDate::parse_from_str(p[0], "%Y-%m-%d").unwrap();
        let o = DateTime::parse_from_rfc3339(p[1]).unwrap().with_timezone(&Utc);
        let c = DateTime::parse_from_rfc3339(p[2]).unwrap().with_timezone(&Utc);
        map.insert(d, (o, c));
    }
    map
}

/// Every date on which the root's table and pmc disagree (open instant, close instant, or one of them has no session).
fn pmc_diffs(root: &str, csv: &str) -> Vec<(NaiveDate, Option<(DateTime<Utc>, DateTime<Utc>)>, Option<(DateTime<Utc>, DateTime<Utc>)>)> {
    let cal = globex::GlobexCalendar::for_root(root).unwrap();
    let pmc = parse_csv(csv);
    let table: HashSet<NaiveDate> = cal.sessions_in_range(cal.first_date(), cal.last_date()).unwrap().into_iter().collect();
    let mut dates: Vec<NaiveDate> = table.iter().copied().chain(pmc.keys().copied()).collect::<HashSet<_>>().into_iter().collect();
    dates.sort();
    let mut out = Vec::new();
    for d in dates {
        let t = table.contains(&d).then(|| (cal.session_open(d).unwrap(), cal.session_close(d).unwrap()));
        let p = pmc.get(&d).copied();
        if t != p {
            out.push((d, t, p));
        }
    }
    out
}

/// The 72 dates on which the energy table (CL, MCL) and the metals table (GC, MGC) disagree with pmc. Both groups
/// disagree on the same dates, and every disagreement is an early-halt MINUTE (pmc carries no halt times: it uses
/// 13:00, 14:30 or a regular 17:00 close, its own rules per era). No pmc session date is missing from, or extra to,
/// the CME tables, and every session open agrees. In each case the CME table wins, and our 1m bars side with it:
/// `calendar_data_oracle.rs` finds no bar after the table's halt on any of these dates in CL or GC, apart from the
/// stamp-minute artifacts it lists. pmc is the one that is wrong.
static ENERGY_METALS_ALLOW_LIST: &[AllowListEntry] = &[
    AllowListEntry { date: (2009, 1, 19), cme_source_date: Some((2009, 1, 19)), notes: "close: CME table 17:00 ET vs pmc 13:00 ET (table row cited to CME, pmc has no halt-time data)" },
    AllowListEntry { date: (2009, 2, 16), cme_source_date: Some((2009, 2, 16)), notes: "close: CME table 17:00 ET vs pmc 13:00 ET (table row cited to CME, pmc has no halt-time data)" },
    AllowListEntry { date: (2009, 5, 22), cme_source_date: Some((2009, 5, 22)), notes: "close: CME table 16:15 ET vs pmc 17:00 ET (table row cited to CME, pmc has no halt-time data)" },
    AllowListEntry { date: (2009, 5, 25), cme_source_date: Some((2009, 5, 25)), notes: "close: CME table 13:15 ET vs pmc 13:00 ET (table row cited to CME, pmc has no halt-time data)" },
    AllowListEntry { date: (2009, 7, 3), cme_source_date: Some((2009, 7, 3)), notes: "close: CME table 13:30 ET vs pmc 13:00 ET (table row cited to CME, pmc has no halt-time data)" },
    AllowListEntry { date: (2009, 9, 4), cme_source_date: Some((2009, 9, 4)), notes: "close: CME table 16:15 ET vs pmc 17:00 ET (table row cited to CME, pmc has no halt-time data)" },
    AllowListEntry { date: (2009, 9, 7), cme_source_date: Some((2009, 9, 7)), notes: "close: CME table 13:15 ET vs pmc 13:00 ET (table row cited to CME, pmc has no halt-time data)" },
    AllowListEntry { date: (2009, 11, 26), cme_source_date: Some((2009, 11, 26)), notes: "close: CME table 13:15 ET vs pmc 13:00 ET (table row cited to CME, pmc has no halt-time data)" },
    AllowListEntry { date: (2009, 12, 24), cme_source_date: Some((2009, 12, 24)), notes: "close: CME table 13:45 ET vs pmc 17:00 ET (table row cited to CME, pmc has no halt-time data)" },
    AllowListEntry { date: (2010, 1, 15), cme_source_date: Some((2010, 1, 15)), notes: "close: CME table 16:15 ET vs pmc 17:00 ET (table row cited to CME, pmc has no halt-time data)" },
    AllowListEntry { date: (2010, 1, 18), cme_source_date: Some((2010, 1, 18)), notes: "close: CME table 13:15 ET vs pmc 13:00 ET (table row cited to CME, pmc has no halt-time data)" },
    AllowListEntry { date: (2010, 2, 12), cme_source_date: Some((2010, 2, 12)), notes: "close: CME table 16:15 ET vs pmc 17:00 ET (table row cited to CME, pmc has no halt-time data)" },
    AllowListEntry { date: (2010, 2, 15), cme_source_date: Some((2010, 2, 15)), notes: "close: CME table 13:15 ET vs pmc 13:00 ET (table row cited to CME, pmc has no halt-time data)" },
    AllowListEntry { date: (2010, 5, 28), cme_source_date: Some((2010, 5, 28)), notes: "close: CME table 16:15 ET vs pmc 17:00 ET (table row cited to CME, pmc has no halt-time data)" },
    AllowListEntry { date: (2010, 5, 31), cme_source_date: Some((2010, 5, 31)), notes: "close: CME table 13:15 ET vs pmc 13:00 ET (table row cited to CME, pmc has no halt-time data)" },
    AllowListEntry { date: (2010, 7, 2), cme_source_date: Some((2010, 7, 2)), notes: "close: CME table 16:15 ET vs pmc 17:00 ET (table row cited to CME, pmc has no halt-time data)" },
    AllowListEntry { date: (2010, 7, 5), cme_source_date: Some((2010, 7, 5)), notes: "close: CME table 13:15 ET vs pmc 13:00 ET (table row cited to CME, pmc has no halt-time data)" },
    AllowListEntry { date: (2010, 9, 3), cme_source_date: Some((2010, 9, 3)), notes: "close: CME table 16:15 ET vs pmc 17:00 ET (table row cited to CME, pmc has no halt-time data)" },
    AllowListEntry { date: (2010, 9, 6), cme_source_date: Some((2010, 9, 6)), notes: "close: CME table 13:15 ET vs pmc 13:00 ET (table row cited to CME, pmc has no halt-time data)" },
    AllowListEntry { date: (2010, 10, 8), cme_source_date: Some((2010, 10, 8)), notes: "close: CME table 16:15 ET vs pmc 17:00 ET (table row cited to CME, pmc has no halt-time data)" },
    AllowListEntry { date: (2010, 11, 25), cme_source_date: Some((2010, 11, 25)), notes: "close: CME table 13:15 ET vs pmc 13:00 ET (table row cited to CME, pmc has no halt-time data)" },
    AllowListEntry { date: (2011, 1, 14), cme_source_date: Some((2011, 1, 14)), notes: "close: CME table 16:15 ET vs pmc 17:00 ET (table row cited to CME, pmc has no halt-time data)" },
    AllowListEntry { date: (2011, 1, 17), cme_source_date: Some((2011, 1, 17)), notes: "close: CME table 13:15 ET vs pmc 13:00 ET (table row cited to CME, pmc has no halt-time data)" },
    AllowListEntry { date: (2011, 2, 21), cme_source_date: Some((2011, 2, 21)), notes: "close: CME table 13:15 ET vs pmc 13:00 ET (table row cited to CME, pmc has no halt-time data)" },
    AllowListEntry { date: (2011, 5, 30), cme_source_date: Some((2011, 5, 30)), notes: "close: CME table 13:15 ET vs pmc 13:00 ET (table row cited to CME, pmc has no halt-time data)" },
    AllowListEntry { date: (2011, 7, 4), cme_source_date: Some((2011, 7, 4)), notes: "close: CME table 13:15 ET vs pmc 13:00 ET (table row cited to CME, pmc has no halt-time data)" },
    AllowListEntry { date: (2011, 9, 5), cme_source_date: Some((2011, 9, 5)), notes: "close: CME table 13:15 ET vs pmc 13:00 ET (table row cited to CME, pmc has no halt-time data)" },
    AllowListEntry { date: (2011, 11, 24), cme_source_date: Some((2011, 11, 24)), notes: "close: CME table 13:15 ET vs pmc 13:00 ET (table row cited to CME, pmc has no halt-time data)" },
    AllowListEntry { date: (2012, 1, 16), cme_source_date: Some((2012, 1, 16)), notes: "close: CME table 13:15 ET vs pmc 13:00 ET (table row cited to CME, pmc has no halt-time data)" },
    AllowListEntry { date: (2012, 2, 20), cme_source_date: Some((2012, 2, 20)), notes: "close: CME table 13:15 ET vs pmc 13:00 ET (table row cited to CME, pmc has no halt-time data)" },
    AllowListEntry { date: (2012, 5, 28), cme_source_date: Some((2012, 5, 28)), notes: "close: CME table 13:15 ET vs pmc 13:00 ET (table row cited to CME, pmc has no halt-time data)" },
    AllowListEntry { date: (2012, 7, 4), cme_source_date: Some((2012, 7, 4)), notes: "close: CME table 13:15 ET vs pmc 13:00 ET (table row cited to CME, pmc has no halt-time data)" },
    AllowListEntry { date: (2012, 9, 3), cme_source_date: Some((2012, 9, 3)), notes: "close: CME table 13:15 ET vs pmc 13:00 ET (table row cited to CME, pmc has no halt-time data)" },
    AllowListEntry { date: (2012, 11, 22), cme_source_date: Some((2012, 11, 22)), notes: "close: CME table 13:15 ET vs pmc 13:00 ET (table row cited to CME, pmc has no halt-time data)" },
    AllowListEntry { date: (2012, 12, 24), cme_source_date: Some((2012, 12, 24)), notes: "close: CME table 13:45 ET vs pmc 17:00 ET (table row cited to CME, pmc has no halt-time data)" },
    AllowListEntry { date: (2013, 1, 21), cme_source_date: Some((2013, 1, 21)), notes: "close: CME table 13:15 ET vs pmc 13:00 ET (table row cited to CME, pmc has no halt-time data)" },
    AllowListEntry { date: (2013, 2, 18), cme_source_date: Some((2013, 2, 18)), notes: "close: CME table 13:15 ET vs pmc 13:00 ET (table row cited to CME, pmc has no halt-time data)" },
    AllowListEntry { date: (2013, 5, 27), cme_source_date: Some((2013, 5, 27)), notes: "close: CME table 13:15 ET vs pmc 13:00 ET (table row cited to CME, pmc has no halt-time data)" },
    AllowListEntry { date: (2013, 7, 4), cme_source_date: Some((2013, 7, 4)), notes: "close: CME table 13:15 ET vs pmc 13:00 ET (table row cited to CME, pmc has no halt-time data)" },
    AllowListEntry { date: (2013, 9, 2), cme_source_date: Some((2013, 9, 2)), notes: "close: CME table 13:15 ET vs pmc 13:00 ET (table row cited to CME, pmc has no halt-time data)" },
    AllowListEntry { date: (2013, 11, 28), cme_source_date: Some((2013, 11, 28)), notes: "close: CME table 13:15 ET vs pmc 13:00 ET (table row cited to CME, pmc has no halt-time data)" },
    AllowListEntry { date: (2013, 12, 24), cme_source_date: Some((2013, 12, 24)), notes: "close: CME table 13:45 ET vs pmc 17:00 ET (table row cited to CME, pmc has no halt-time data)" },
    AllowListEntry { date: (2014, 1, 20), cme_source_date: Some((2014, 1, 20)), notes: "close: CME table 13:15 ET vs pmc 13:00 ET (table row cited to CME, pmc has no halt-time data)" },
    AllowListEntry { date: (2014, 2, 17), cme_source_date: Some((2014, 2, 17)), notes: "close: CME table 13:15 ET vs pmc 13:00 ET (table row cited to CME, pmc has no halt-time data)" },
    AllowListEntry { date: (2014, 12, 24), cme_source_date: Some((2014, 12, 24)), notes: "close: CME table 13:45 ET vs pmc 17:00 ET (table row cited to CME, pmc has no halt-time data)" },
    AllowListEntry { date: (2015, 12, 24), cme_source_date: Some((2015, 12, 24)), notes: "close: CME table 13:15 ET vs pmc 17:00 ET (table row cited to CME, pmc has no halt-time data)" },
    AllowListEntry { date: (2018, 12, 24), cme_source_date: Some((2018, 12, 24)), notes: "close: CME table 13:15 ET vs pmc 17:00 ET (table row cited to CME, pmc has no halt-time data)" },
    AllowListEntry { date: (2019, 12, 24), cme_source_date: Some((2019, 12, 24)), notes: "close: CME table 13:45 ET vs pmc 17:00 ET (table row cited to CME, pmc has no halt-time data)" },
    AllowListEntry { date: (2020, 12, 24), cme_source_date: Some((2020, 12, 24)), notes: "close: CME table 13:45 ET vs pmc 17:00 ET (table row cited to CME, pmc has no halt-time data)" },
    AllowListEntry { date: (2022, 1, 17), cme_source_date: Some((2022, 1, 17)), notes: "close: CME table 13:00 ET vs pmc 14:30 ET (table row cited to CME, pmc has no halt-time data)" },
    AllowListEntry { date: (2022, 2, 21), cme_source_date: Some((2022, 2, 21)), notes: "close: CME table 13:00 ET vs pmc 14:30 ET (table row cited to CME, pmc has no halt-time data)" },
    AllowListEntry { date: (2022, 5, 30), cme_source_date: Some((2022, 5, 30)), notes: "close: CME table 13:00 ET vs pmc 14:30 ET (table row cited to CME, pmc has no halt-time data)" },
    AllowListEntry { date: (2022, 7, 4), cme_source_date: Some((2022, 7, 4)), notes: "close: CME table 13:00 ET vs pmc 14:30 ET (table row cited to CME, pmc has no halt-time data)" },
    AllowListEntry { date: (2022, 11, 24), cme_source_date: Some((2022, 11, 24)), notes: "close: CME table 13:00 ET vs pmc 14:30 ET (table row cited to CME, pmc has no halt-time data)" },
    AllowListEntry { date: (2023, 1, 16), cme_source_date: Some((2023, 1, 16)), notes: "close: CME table 13:30 ET vs pmc 14:30 ET (table row cited to CME, pmc has no halt-time data)" },
    AllowListEntry { date: (2023, 2, 20), cme_source_date: Some((2023, 2, 20)), notes: "close: CME table 13:30 ET vs pmc 14:30 ET (table row cited to CME, pmc has no halt-time data)" },
    AllowListEntry { date: (2023, 5, 29), cme_source_date: Some((2023, 5, 29)), notes: "close: CME table 13:30 ET vs pmc 14:30 ET (table row cited to CME, pmc has no halt-time data)" },
    AllowListEntry { date: (2023, 7, 4), cme_source_date: Some((2023, 7, 4)), notes: "close: CME table 13:30 ET vs pmc 14:30 ET (table row cited to CME, pmc has no halt-time data)" },
    AllowListEntry { date: (2024, 1, 15), cme_source_date: Some((2024, 1, 15)), notes: "close: CME table 13:30 ET vs pmc 14:30 ET (table row cited to CME, pmc has no halt-time data)" },
    AllowListEntry { date: (2024, 2, 19), cme_source_date: Some((2024, 2, 19)), notes: "close: CME table 13:30 ET vs pmc 14:30 ET (table row cited to CME, pmc has no halt-time data)" },
    AllowListEntry { date: (2024, 7, 4), cme_source_date: Some((2024, 7, 4)), notes: "close: CME table 13:30 ET vs pmc 14:30 ET (table row cited to CME, pmc has no halt-time data)" },
    AllowListEntry { date: (2024, 9, 2), cme_source_date: Some((2024, 9, 2)), notes: "close: CME table 14:30 ET vs pmc 13:00 ET (table row cited to CME, pmc has no halt-time data)" },
    AllowListEntry { date: (2024, 11, 29), cme_source_date: Some((2024, 11, 29)), notes: "close: CME table 14:45 ET vs pmc 13:45 ET (table row cited to CME, pmc has no halt-time data)" },
    AllowListEntry { date: (2024, 12, 24), cme_source_date: Some((2024, 12, 24)), notes: "close: CME table 13:45 ET vs pmc 17:00 ET (table row cited to CME, pmc has no halt-time data)" },
    AllowListEntry { date: (2025, 7, 4), cme_source_date: Some((2025, 7, 4)), notes: "close: CME table 13:00 ET vs pmc 14:30 ET (table row cited to CME, pmc has no halt-time data)" },
    AllowListEntry { date: (2025, 9, 1), cme_source_date: Some((2025, 9, 1)), notes: "close: CME table 14:30 ET vs pmc 13:00 ET (table row cited to CME, pmc has no halt-time data)" },
    AllowListEntry { date: (2025, 11, 27), cme_source_date: Some((2025, 11, 27)), notes: "close: CME table 13:30 ET vs pmc 14:30 ET (table row cited to CME, pmc has no halt-time data)" },
    AllowListEntry { date: (2025, 11, 28), cme_source_date: Some((2025, 11, 28)), notes: "close: CME table 14:45 ET vs pmc 13:45 ET (table row cited to CME, pmc has no halt-time data)" },
    AllowListEntry { date: (2025, 12, 24), cme_source_date: Some((2025, 12, 24)), notes: "close: CME table 13:45 ET vs pmc 17:00 ET (table row cited to CME, pmc has no halt-time data)" },
    AllowListEntry { date: (2027, 6, 18), cme_source_date: Some((2027, 6, 18)), notes: "close: CME table 13:00 ET vs pmc 14:30 ET (table row cited to CME, pmc has no halt-time data)" },
    AllowListEntry { date: (2027, 9, 6), cme_source_date: Some((2027, 9, 6)), notes: "close: CME table 14:30 ET vs pmc 13:00 ET (table row cited to CME, pmc has no halt-time data)" },
    AllowListEntry { date: (2027, 11, 26), cme_source_date: Some((2027, 11, 26)), notes: "close: CME table 14:45 ET vs pmc 13:45 ET (table row cited to CME, pmc has no halt-time data)" },
];

#[test]
fn test_cme_vs_pandas_market_calendars_energy_metals() {
    let mut allow_map: HashMap<NaiveDate, &AllowListEntry> = HashMap::new();
    for e in ENERGY_METALS_ALLOW_LIST {
        let d = NaiveDate::from_ymd_opt(e.date.0, e.date.1, e.date.2).unwrap();
        assert!(!e.notes.is_empty());
        allow_map.insert(d, e);
    }
    assert_eq!(allow_map.len(), ENERGY_METALS_ALLOW_LIST.len());

    // (root, pmc fixture) for each group; MCL / MGC have the same tables as CL / GC from their listing floor.
    for (root, csv) in [("CL", PMC_ENERGY_CSV), ("GC", PMC_METALS_CSV), ("MCL", PMC_ENERGY_CSV), ("MGC", PMC_METALS_CSV)] {
        let cal = globex::GlobexCalendar::for_root(root).unwrap();
        let pmc = parse_csv(csv);
        let mut hits = HashSet::new();
        let mut exact = 0usize;
        let sessions = cal.sessions_in_range(cal.first_date(), cal.last_date()).unwrap();
        let table: HashSet<NaiveDate> = sessions.iter().copied().collect();
        for &d in &sessions {
            let t = (cal.session_open(d).unwrap(), cal.session_close(d).unwrap());
            match pmc.get(&d) {
                Some(&p) if p == t => exact += 1,
                Some(&p) => {
                    // an open that differs, or a date pmc has no session for, is never allow-listed: only close minutes
                    assert_eq!(t.0, p.0, "{root} {d}: session open differs from pmc");
                    assert!(allow_map.contains_key(&d), "{root}: unlisted close difference on {d}: table={t:?} pmc={p:?}");
                    hits.insert(d);
                }
                None => panic!("{root}: the table has a session on {d} that pmc lacks"),
            }
        }
        // pmc sessions inside the root's range that the table does not have
        for &d in pmc.keys() {
            if d >= cal.first_date() && d <= cal.last_date() {
                assert!(table.contains(&d), "{root}: pmc has a session on {d} that the table lacks");
            }
        }
        // every allow-listed date inside this root's range must still disagree
        for (&d, e) in &allow_map {
            if d >= cal.first_date() {
                assert!(hits.contains(&d), "{root}: allow-listed difference on {d} ({}) has gone away", e.notes);
            }
        }
        println!("{root}: {} sessions, {exact} exact, {} allow-listed close-minute differences", sessions.len(), hits.len());
        match root {
            "CL" | "GC" => assert_eq!((sessions.len(), exact, hits.len()), (4902, 4830, 72)),
            _ => {}
        }
    }
}

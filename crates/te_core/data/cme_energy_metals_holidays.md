# CME energy (NYMEX) and metals (COMEX) Globex holiday tables (P6C)

Files: `cme_energy_holidays.csv` (CL, MCL), `cme_metals_holidays.csv` (GC, MGC).
Same columns and semantics as `cme_equity_holidays.csv` (`date,status,halt_et,reopen_et,source_url,source_title,notes`).
Built by `tools/build_cme_energy_metals_holidays.py` from the raw CME captures (PDF, XLS, trading-hours API JSON, Wayback copies); run with `-B`, needs xlrd.

## Coverage
- 213 rows each (55 closed, 158 early_halt), 2009-01-01 .. 2027-12-24. The two tables are identical in status and times; every source treats NYMEX and COMEX alike.
- First year is 2009, not 2008, although the CL and GC stores start in 2008. cme_raw has NO per-holiday NYMEX/COMEX notices for 2008: the 2008 annual calendars (`ext_2008GLXholidayweb.txt`, `ext_CMEGlobex2008HolidaySchedule.txt`) carry one NYMEX and COMEX section, Labor Day only (Fri 2008-08-29 and Tue 2008-09-02 regular close, Mon 2008-09-01 halt and resume), and the 2007-12-25 and 2008-01-01 notes (`te_christmas_2007.txt`, `te_2008newyears.txt`) give a reopen only. That is 1 of about 9 holidays, so a 2008 table would be mostly inferred from bars; it was not built (the review item to extend to 2008 is closed with 'sources insufficient, 2009 kept'). Last year is 2027 (the last year CME has published; final row 2027-12-24 closed).
- Eras: 2009-2016 per-holiday PDFs (NYMEX/COMEX/DME section); 2016-2022 XLS ("Energy, Metals & DME" row, CT as day fractions); 2022-2027 CME trading-hours API (CL and GC products; an empty-events weekday inside a non-empty capture is inferred closed); 2023 jumbled `tradinghours_*.pdf` plus CME Clearing advisories (holiday confirmation) and hand-keyed MANUAL rows. S-5525 confirms the 2010-12-31 1515 CT early close.

## Conflicts resolved (all in the row `notes`)
- OBSERVED-OVERRIDE: published early-halt time contradicted by dense CL and GC 1m bars; the row takes the observed time. 2009-11-27 (13:45), 2015-12-24 and 2018-12-24 (13:15), 2022-01-17/02-21/05-30/07-04/09-05/11-24 (13:00, API said 14:30), 2024-01-15/02-19/07-04 and 2025-11-27 (13:30, API said 14:30).
- DROPPED: published Friday 1515 CT early closes that the bars never show: 2011-02-18, 05-27, 07-01, 09-02, 10-07. 2013-07-03 was a PDF section bleed ("Early close for Dairy").
- REGULAR-IN-OTHER: 2009 annual calendar lists only a regular 17:15 halt for 2009-05-25, 09-07, 11-26; the per-holiday PDFs are used.
- MANUAL-VS-SOURCE: 2023-09-04 jumbled PDF says 14:30, bars and the PDF text say 13:00; 13:00 used.
- Where CME published a regular close but bars end early (2009-10-09 ends 16:15 ET) no row is written; the data oracle allow-lists it.
- The API's 13:30 ET "preopen" events conflict with bars on the dates above; bars win.

## Known gaps
- Store (not table) gaps, allow-listed by the data oracle: CL 2013-07-11/12, 2013-11-08, 2014-01-27..31; GC 2008-01-21..30, 2008-03-21, 2008-07-31..08-08, 2009-08-31..2009-10-12, 2015-10-12..16, 2023-04-06..14.
- One-minute stamp artifacts (a bar one minute past the halt) are allow-listed.

## Roots, listing dates and ranges (te_core `ROOTS`)
Range per root = [max(first date of its table, floor), last date of its table]. A micro takes its mini's range, NOT its own listing date: the web serves mini data under the micro symbol (tv `web/lib/contract-specs.ts`: "Since 2026-09-21 the spoke serves mini DATA under the micro SYMBOL") and sims size micros on mini history from 2006, so a micro floor would refuse replays that must work. The listing dates below are kept as facts, not refusals.
- NQ, ES, MNQ, MES: equity table, 2006-01-01 .. 2027-12-31. Fact: MNQ and MES (and MYM, M2K) launched 2019-05-06 (CME press release 2019-05-06, "CME Group announces launch of new Micro E-mini equity index futures"). No floor.
- RTY: floor 2017-07-09. RTY returned to CME Group 2017-07-10 (CME press release 2017-04-12, "Russell 2000 index futures and options to return to CME Group July 10"); before that it was ICE's TF, so no CME calendar applies. M2K follows RTY (floor 2017-07-09).
- YM: floor 2008-01-27 (see the verdict below). MYM follows YM.
- CL, MCL: energy table, 2009-01-01 .. 2027-12-31 (CL on Globex since 2006-09-05, before the table). Fact: MCL launched 2021-07-12 (CME press release 2021-05-17, "CME Group to launch Micro WTI crude oil futures on July 12"). No MCL floor.
- GC, MGC: metals table, 2009-01-01 .. 2027-12-31. Fact: MGC first trade date 2010-10-04 (CME Special Executive Report S-5391). No MGC floor.

## Session-rule eras (equity group; `EQUITY_ERAS` in globex.rs)
Dated rules per group, not allow-list entries. A holiday-table row always wins over an era. The default (open 18:00 ET the evening before, close 17:00 ET, no halt inside) is byte-identical from trade date 2021-06-28 on. Energy and metals have no era: CL and GC never show the 16:15 Friday close or the 16:15-16:30 ET halt as a regime (a halt-like Mon-Thu day is under 1 percent of GC days, 1 CL day).

| Trade dates | Friday close (ET) | Halt inside the session (ET) | Evidence |
|---|---|---|---|
| 2006-01-01 .. 2012-11-16 | 16:15 | 16:15-16:30 Mon-Thu (a Friday is already closed at 16:15) | 1m bars of ES, NQ, YM: nearly every non-holiday Friday of the era has its last bar at 16:14 (53 ES Fridays end 16:15-16:16 by another count, the rest 16:14), and 96 to 100 percent of Mon-Thu days have no bar 16:15-16:29. CME's 2009 notices call 1515 CT the "Regular CME Globex close" and 1530 CT the "Regular CME Globex open" for equity products (`2009-4th-of-july.txt` lines 6, 8, 21; `2009-globex-holiday-calendar.txt`; cme_raw/txt). Notices from 2013 on print 1615 CT; the bars contradict them every year (no 17:15 ET halt), so the bars win. |
| 2012-11-17 .. 2021-06-25 | 17:00 (default) | 16:15-16:30 Mon-Fri | OBSERVED in the bars: the last 16:15 Friday is 2012-11-16 and the first 17:00 Friday is 2012-11-30 (2012-11-23 is an early-halt row, so the switch inside 2012-11-17..11-30 cannot be dated further; the rule is set right after 2012-11-16). The halt persists to 2021-06-25 (the last Friday with an empty 16:15-16:30 window). |
| 2021-06-28 on | 17:00 (default) | none | OBSERVED: from Monday 2021-06-28 every weekday has bars across 16:15-16:30 ET (ES 15 of 15 minutes on 2021-06-28..07-02). No CME notice for the change is in cme_raw. |

Per root: ES, NQ and YM (from 2008-01-27) and RTY (from 2017-07-09) all show these boundaries; the micros share their mini's calendar and range.
Modelling: the halt is an intra-session gap. `session_at` and `session_or_next` keep the containing trade date (an order placed in the halt keeps its trade date); `is_open_at` is false inside it. CME's own notices roll the trade date at 1515 CT (16:15 ET) in the first era; the calendar keeps the 17:00 ET close as the session identity in the second era and does not model that roll (a documented divergence).

## OBSERVED-OVERRIDE rows (all 13; the same dates in the energy and metals tables)
The sourced value is what the notice, XLS or API said; the observed value is where the CL and GC 1m bars (identical for both) stop. Bars are what traded, so they win; the notice is cited in each row's notes.

| Date | Sourced | Observed (table) | Why the source lost |
|---|---|---|---|
| 2009-11-27 | 13:30 ET (PDF, 1230 CT) | 13:45 | both products trade 15 minutes past the notice |
| 2015-12-24 | 13:45 ET (PDF) | 13:15 | both end 30 minutes before the notice |
| 2018-12-24 | 13:45 ET (XLS) | 13:15 | both end 30 minutes before the notice |
| 2022-01-17, 02-21, 05-30, 07-04, 09-05, 11-24 | 14:30 ET (XLS "Energy, Metals & DME" row) | 13:00 | six dates with the same 90-minute error: the XLS shows the older halt time, the bars end 13:00 ET on all of them |
| 2024-01-15, 02-19, 07-04, 2025-11-27 | 14:30 ET (API "13:30 preopen" event) | 13:30 | the API event is a pre-open, not the close; bars end 13:30 |

The 2023 rows whose time was read from bars (2023-01-16, 02-20, 05-29, 06-19, 07-04, 09-04, 11-23, 11-24) are not overrides: no source states a time, or the PDF text is jumbled; see each row's notes.

## YM and RTY: equity table or their own? (verdict)
- RTY: the equity table, from 2017-07-09. Against the 1m store the RTY range has 14 findings in 2,708 sessions, all store gaps (2023-04-06..14) or the equity table's own known gaps (2023-01-16, 2023-02-20; see below). No RTY-specific rows are needed.
- YM: the equity table, from 2008-01-27 only. YM was a CBOT product on the e-cbot platform until the CBOT financial contracts moved to CME Globex on Sunday 2008-01-27 (trade date 2008-01-28), per the CBOT migration notice (CFTC rule filing rul121807cbot001 and CME "CBOT Migration Trading Hours & Ticker Symbol Changes"; the e-cbot hours were 18:15-07:00 CT-style hours, replaced by Globex hours). The 1m store confirms the divergence before that date: YM has no bars on 2006-09-04, 2007-05-28, 2007-07-04, 2007-09-03 and 2007-11-22 (all days on which the equity table has an early-halt row), has bars on 2007-01-01 and 2007-01-02 (closed in the table), reopens 18:00 ET on 2008-01-01 where the equity table reopens 06:00 ET on 2008-01-02, and runs 16:00 ET closes in April-May 2006. So YM (and MYM) get the equity table over [2008-01-27, 2027-12-31] and are refused before it (I5); no sourced YM-only date overrides were needed after that date.
- The pre-2008 equity table was never claimed for YM; ES and NQ keep their 2006 start.

## 1m data oracle findings (crates/te_core/tests/calendar_data_oracle.rs)
Every deviation is allow-listed with a reason in the test; sizes below are allow-list entries (some entries are date ranges). Allow-list entries now: ES 29, NQ 23, YM 14, RTY 5, CL 6, GC 6 (83; it was 63 with the era Fridays allow-listed). Short sessions (data ends early, no halt claim) are counted, not failed.
- Equity table, ES and NQ over 2006-2027: the 2023 rows that were never sourced (see "Known gaps" of the equity notes): 2023-01-16 and 2023-02-20 data halt at 13:00 ET (the table has no row, so it models a 17:00 close); 2023-04-07 Good Friday: ES and NQ data stop at 09:15 ET (the table has no row; counted as short data, not failed), and YM/RTY have no bars in the store that week (2023-04-06..14 store gap). The equity table is NOT changed: the only 2023 sources for these dates are the energy/metals Clearing advisories and the bars themselves, and no CME Globex hours notice for equity survives.
- The Friday 16:15 ET close and the 16:15-16:30 ET halt are modelled as dated eras (see "Session-rule eras"), not allow-listed. The earlier claim that the Friday era ran to the 2016-10-28 week was wrong: it ended 2012-11-16, and 2016-10-28 is a single Friday on which ES, NQ, YM, CL and GC all end 16:15-16:17 ET (OBSERVED one-off, five allow-list entries; CL 2009-10-09 is a sixth; none has a notice in cme_raw).
- Other era-related findings, allow-listed as OBSERVED: no halt on 2006-04-27..2006-05-22 in ES and NQ (the halt returns 2006-05-23); bars inside the halt on 2020-09-10, 2020-09-11 and 2020-10-19..22 on ES, NQ, YM and RTY; ten one-minute stray bars after a 16:15 Friday close (ES 4, NQ 3, YM 3).
- 2007-01-02 (Ford day of mourning): bars until 09:15 ET though the table (CME release) says closed.
- 2015-07-02: the CME July-4 PDF says an early close at 13:15 ET for equity, but ES, NQ and YM trade to 17:00 ET. Unresolved source conflict; the table follows the PDF.
- July 3 early halts 2006-2008: bars resume about 16:30 ET (the releases do not state the reopen).
- One-minute stamp artifacts (a bar stamped at the halt or at the reopen minute) and store gaps (2014-01-27..31 for every root, 2013-07-12, CL and GC 2009-08/09 and 2015-10) are allow-listed with those reasons.
- CL and GC are nearly clean: 6 allow-list entries each. CL/GC rows whose halt time was read from bars (see OBSERVED-OVERRIDE) agree with the bars by construction.

## pandas_market_calendars (crates/te_core/tests/cme_pmc_oracle.rs)
pmc 5.5.0 has `CMEGlobex_CL` and `CMEGlobex_GC` (and all the other CMEGlobex_* calendars from one rule set). Against both tables: the same 4,902 session dates (2009..2027), identical session opens, and 72 dates whose CLOSE minute differs, the same 72 for energy and metals. All 72 are allow-listed. pmc is the wrong one on each: it has no per-holiday halt data (uses 13:00 / 14:30 / a regular 17:00 close by era), and our bars show no trading after the table's halt on those dates.

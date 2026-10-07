# CME energy (NYMEX) and metals (COMEX) Globex holiday tables (P6C)

Files: `cme_energy_holidays.csv` (CL, MCL), `cme_metals_holidays.csv` (GC, MGC).
Same columns and semantics as `cme_equity_holidays.csv` (`date,status,halt_et,reopen_et,source_url,source_title,notes`).
Built by `tools/build_cme_energy_metals_holidays.py` from the raw CME captures (PDF, XLS, trading-hours API JSON, Wayback copies); run with `-B`, needs xlrd.

## Coverage
- 213 rows each (55 closed, 158 early_halt), 2009-01-01 .. 2027-12-24. The two tables are identical in status and times; every source treats NYMEX and COMEX alike.
- First year is 2009, not 2008: no per-holiday NYMEX/COMEX notices exist for 2008 (the 2008 annual calendar says the energy/metals entries are tentative pending NYMEX input). Last year is 2027 (the last year CME has published; final row 2027-12-24 closed).
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

## Roots, listing floors and ranges (te_core `ROOTS`)
Range per root = [max(first date of its table, listing floor), last date of its table]; the floor is the Sunday-evening open before the first trade date.
- NQ, ES: equity table, 2006-01-01 .. 2027-12-31 (listed on Globex long before).
- MNQ, MES, MYM, M2K: floor 2019-05-05. Launch 2019-05-06 (CME press release "CME Group announces launch of new Micro E-mini equity index futures", 2019-05-06). MNQ and MES had no floor before P6C; that is new behaviour.
- RTY: floor 2017-07-09. RTY returned to CME Group 2017-07-10 (CME press release 2017-04-12, "Russell 2000 index futures and options to return to CME Group July 10"); before that it was ICE's TF, so no CME calendar applies.
- YM: floor 2008-01-27 (see the verdict below). MYM inherits it and has the later 2019-05-05 floor.
- CL: energy table, 2009-01-01 .. 2027-12-31 (on Globex since 2006-09-05, before the table).
- MCL: floor 2021-07-11. Launch 2021-07-12 (CME press release 2021-05-17, "CME Group to launch Micro WTI crude oil futures on July 12").
- GC: metals table, 2009-01-01 .. 2027-12-31 (on Globex before the table; no separate citation needed).
- MGC: floor 2010-10-03. First trade date 2010-10-04 (CME Special Executive Report S-5391).

## YM and RTY: equity table or their own? (verdict)
- RTY: the equity table, from 2017-07-09. Against the 1m store the RTY range has 14 findings in 2,708 sessions, all store gaps (2023-04-06..14) or the equity table's own known gaps (2023-01-16, 2023-02-20; see below). No RTY-specific rows are needed.
- YM: the equity table, from 2008-01-27 only. YM was a CBOT product on the e-cbot platform until the CBOT financial contracts moved to CME Globex on Sunday 2008-01-27 (trade date 2008-01-28), per the CBOT migration notice (CFTC rule filing rul121807cbot001 and CME "CBOT Migration Trading Hours & Ticker Symbol Changes"; the e-cbot hours were 18:15-07:00 CT-style hours, replaced by Globex hours). The 1m store confirms the divergence before that date: YM has no bars on 2006-09-04, 2007-05-28, 2007-07-04, 2007-09-03 and 2007-11-22 (all days on which the equity table has an early-halt row), has bars on 2007-01-01 and 2007-01-02 (closed in the table), reopens 18:00 ET on 2008-01-01 where the equity table reopens 06:00 ET on 2008-01-02, and runs 16:00 ET closes in April-May 2006. So YM (and MYM) get the equity table over [2008-01-27, 2027-12-31] and are refused before it (I5); no sourced YM-only date overrides were needed after that date.
- The pre-2008 equity table was never claimed for YM; ES and NQ keep their 2006 start.

## 1m data oracle findings (crates/te_core/tests/calendar_data_oracle.rs)
Every deviation is allow-listed with a reason in the test; sizes below are allow-list entries (some entries are date ranges). Short sessions (data ends early, no halt claim) are counted, not failed.
- Equity table, ES and NQ over 2006-2027: the 2023 rows that were never sourced (see "Known gaps" of the equity notes): 2023-01-16 and 2023-02-20 data halt at 13:00 ET (the table has no row, so it models a 17:00 close); 2023-04-07 Good Friday: ES and NQ data stop at 09:15 ET (the table has no row; counted as short data, not failed), and YM/RTY have no bars in the store that week (2023-04-06..14 store gap). The equity table is NOT changed: the only 2023 sources for these dates are the energy/metals Clearing advisories and the bars themselves, and no CME Globex hours notice for equity survives.
- A regular Friday session in ES, NQ and YM closed 16:15 ET until the 2016-10-28 week (329 ES, 332 NQ, 231 YM sessions); the table models a 17:00 close. That is era hours, not a holiday row, and is allow-listed as such (no CME notice for it was found in the raw tree; the 1m data are the only evidence, and the cut-off date matches the 2016-10-31 hours change).
- 2007-01-02 (Ford day of mourning): bars until 09:15 ET though the table (CME release) says closed.
- 2015-07-02: the CME July-4 PDF says an early close at 13:15 ET for equity, but ES, NQ and YM trade to 17:00 ET. Unresolved source conflict; the table follows the PDF.
- July 3 early halts 2006-2008: bars resume about 16:30 ET (the releases do not state the reopen).
- One-minute stamp artifacts (a bar stamped at the halt or at the reopen minute) and store gaps (2014-01-27..31 for every root, 2013-07-12, CL and GC 2009-08/09 and 2015-10) are allow-listed with those reasons.
- CL and GC are nearly clean: 5 and 7 allow-list entries. CL/GC rows whose halt time was read from bars (see OBSERVED-OVERRIDE) agree with the bars by construction.

## pandas_market_calendars (crates/te_core/tests/cme_pmc_oracle.rs)
pmc 5.5.0 has `CMEGlobex_CL` and `CMEGlobex_GC` (and all the other CMEGlobex_* calendars from one rule set). Against both tables: the same 4,902 session dates (2009..2027), identical session opens, and 72 dates whose CLOSE minute differs, the same 72 for energy and metals. All 72 are allow-listed. pmc is the wrong one on each: it has no per-holiday halt data (uses 13:00 / 14:30 / a regular 17:00 close by era), and our bars show no trading after the table's halt on those dates.

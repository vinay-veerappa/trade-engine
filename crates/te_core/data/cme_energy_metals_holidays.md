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

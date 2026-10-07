"""Build crates/te_core/data/cme_energy_holidays.csv and cme_metals_holidays.csv (P6C).

This is the sibling of the equity build (plans/cme_raw/build_final.py, which wrote cme_equity_holidays.csv). It reads
the same raw CME material (a local, uncommitted ~60 MB folder of Wayback captures; pass it with --raw):

    txt/    per-holiday PDF text (2009-2016), 2008 PDFs (ext_*), 2007 notices (te_*), 2023 trading-hours PDFs
    zips/, x22/   the CME holiday XLS files (2016-2022)
    api/, g2/     CME trading-hours-by-product API captures (2022-2027, product CL and GC)
    dl_list*.txt, api_list.txt, g2/apix.txt     name -> (Wayback timestamp, original URL)

Method, in priority order per date: per-holiday PDF notice, XLS, API. Each candidate is the Energy/Metals row of that
source ("NYMEX, COMEX and DME products" in the PDFs, "Energy, Metals & DME" in the XLS, product CL / GC in the API).
Times are Central in the sources and converted to Eastern (+1 h; the US switches both zones on the same dates).

The result then goes through two reviews that are recorded in the CSV notes, never silently:
  1. MANUAL rows: dates the parsers cannot read (jumbled 2023 PDFs, 2008 notices) and sources read by hand.
  2. OBSERVED overrides (tools/calendar_data_oracle.py fixtures): where a published time is contradicted by a
     dense 1m-bar record (continuous bars to the minute, nothing after), the table keeps the CME source as the
     citation for the holiday and uses the observed halt, and says so in the notes ("[OBSERVED]").

Usage:  python tools/build_cme_energy_metals_holidays.py --raw <plans/cme_raw> [--out crates/te_core/data] [--dump]
Run it with `-B` so no bytecode is written next to the raw sources. xlrd is needed (build script only).
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import glob
import json
import os
import re
import sys

MONTHS = {m: i + 1 for i, m in enumerate("January February March April May June July August September October November December".split())}
for _k, _v in list(MONTHS.items()):
    MONTHS[_k[:3]] = _v
MONTHS["Sept"] = 9
DATE = re.compile(r"^\s*(Mon|Tues|Wednes|Thurs|Fri|Satur|Sun)day,?\s+([A-Za-z]+)\.?,?\s+(\d{1,2})\b")
TIME = re.compile(r"(\d{4})\s*CT(?:\s*/\s*(\d{4})\s*ET)?")
WD = {"Mon": 0, "Tues": 1, "Wednes": 2, "Thurs": 3, "Fri": 4, "Satur": 5, "Sun": 6}


def plus1(hhmm: str) -> str:
    h, m = int(hhmm[:2]), int(hhmm[2:4])
    return f"{(h + 1) % 24:02d}:{m:02d}"


def resolve_date(wd, mon, day, fy):
    for y in (fy, fy - 1, fy + 1):
        try:
            d = dt.date(y, mon, day)
        except ValueError:
            continue
        if d.weekday() == WD[wd]:
            return d
    return None


class Raw:
    def __init__(self, root: str):
        self.root = root
        self.dl: dict[str, tuple[str, str]] = {}
        for f in sorted(glob.glob(os.path.join(root, "dl_list*.txt"))):
            for line in open(f, encoding="utf-8", errors="replace"):
                p = line.split()
                if len(p) == 3:
                    self.dl[p[0].replace("/", "_")] = (p[1], p[2])
        self.api: dict[tuple[str, str], tuple[str, str]] = {}
        for line in open(os.path.join(root, "api_list.txt"), encoding="utf-8"):
            p = line.split()
            if len(p) == 4:
                self.api[(p[0], p[1])] = (p[2], p[3])
        self.apix: dict[str, str] = {}
        for line in open(os.path.join(root, "g2", "apix.txt"), encoding="utf-8"):
            p = line.split()
            if len(p) == 3 and p[1] == "20240708161439":
                self.apix[re.search(r"fromEventDate=([0-9-]+)", p[0]).group(1)] = p[0]

    def wb(self, name: str) -> str:
        ts, o = self.dl[name]
        return f"https://web.archive.org/web/{ts}/{o}"


# --------------------------------------------------------------------------------------------- PDFs
SECTION = re.compile(r"NYMEX|COMEX|Energy|Metals")


def pdf_events(path: str, fy: int):
    """Events of the NYMEX/COMEX/Energy/Metals section(s) of a per-holiday PDF text."""
    out, cur, sel = [], None, False
    for raw in open(path, encoding="utf-8", errors="replace"):
        line = raw.replace("\r", "").replace("\ufffd", "-").rstrip()
        if not line.strip():
            continue
        m = DATE.match(line)
        ind = len(line) - len(line.lstrip())
        if m:
            mo = MONTHS.get(m.group(2)) or MONTHS.get(m.group(2)[:3])
            cur = resolve_date(m.group(1), mo, int(m.group(3)), fy) if mo else None
            continue
        if ind <= 3 and re.search(r"Products|Futures on CME|Exchange Products|KOSPI|Bursa|Interest|Grain|Livestock|Agricult", line) and not TIME.search(line):
            sel = bool(SECTION.search(line))
            continue
        if not sel or cur is None:
            continue
        low = line.lower()
        words = low.split()
        if "tas" in words or "tam" in words or "tas-" in low or "(tas" in low or "tas/" in low or "gold" in words or "silver" in words or "copper" in words:
            continue
        t = TIME.search(line)
        if t:
            ct = t.group(1)
            et = f"{t.group(2)[:2]}:{t.group(2)[2:]}" if t.group(2) else plus1(ct)
            if "early" in low and "close" in low:
                typ = "EARLY"
            elif "resume" in low:
                typ = "OPEN"
            elif "halt" in low or ("globex close" in low and "regular" not in low):
                typ = "HALT"
            elif "open" in low:
                typ = "OPEN"
            elif "close" in low:
                typ = "CLOSE"
            else:
                typ = "OTHER"
            out.append((cur, typ, ct, et, line.strip()))
        elif "closed" in low and "exception" not in low and "remain" not in low:
            out.append((cur, "CLOSED", "", "", line.strip()))
    return out


def pdf_candidates(raw: Raw):
    """date -> list of candidate dicts, from the 2009-2016 per-holiday PDFs (annual calendars are lower priority)."""
    cands: dict[str, list[dict]] = {}
    files = [f for f in sorted(glob.glob(os.path.join(raw.root, "txt", "*.txt")))
             if re.match(r"^20(09|1[0-6])-", os.path.basename(f)) and "advisory" not in f]
    for p in files:
        b = os.path.basename(p)[:-4]
        fy = int(b[:4])
        ev = pdf_events(p, fy)
        seen = set()
        for i, (d, typ, ct, et, txt) in enumerate(ev):
            if typ not in ("EARLY", "HALT", "CLOSED") or d in seen:
                continue
            seen.add(d)
            reopen = ""
            for e in ev[i + 1:]:
                if e[1] == "OPEN" and 0 <= (e[0] - d).days <= 3 and "pre-open" not in e[4].lower() and "preopen" not in e[4].lower():
                    reopen = e[3] if e[0] == d else f"{e[0]} {e[3]}"
                    break
                if e[1] in ("EARLY", "HALT", "CLOSED"):
                    break
            halt = "" if typ == "CLOSED" else et
            if typ != "CLOSED" and halt >= "17:00":
                status = "regular"  # a "halt" at the regular close: no early halt
            else:
                status = "closed" if typ == "CLOSED" else "early_halt"
            cands.setdefault(str(d), []).append(dict(
                date=str(d), status=status, halt=halt, reopen=reopen, src=b + ".pdf",
                prio=7 if "globex-holiday-calendar" in b else 3, raw=f"{typ} {ct} CT | {txt[:80]}",
                url=raw.wb(b + ".pdf"), title=f"CME Globex holiday trading hours: {b}.pdf",
                kind="PDF (pdftotext -layout), NYMEX/COMEX/Energy-Metals section"))
    return cands


# --------------------------------------------------------------------------------------------- XLS
def xls_candidates(raw: Raw):
    import xlrd

    def tstr(c):
        if isinstance(c, float):
            m = round(c * 1440)
            return "%02d:%02d" % (m // 60, m % 60)
        return str(c).strip()

    MON = {m.lower(): i for m, i in MONTHS.items()}

    def pdate(txt, fy):
        m = re.match(r"\s*([A-Za-z]+),?\s*([A-Za-z]+)\.?\s+(\d+)", txt)
        if not m:
            return None
        wd, mo, dd = m.group(1).lower(), m.group(2).lower(), int(m.group(3))
        mo = [v for k, v in MON.items() if k.startswith(mo[:3])]
        wds = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
        if not mo or wd not in wds:
            return None
        best = None
        for y in (fy - 1, fy, fy + 1):
            try:
                d = dt.date(y, mo[0], dd)
            except ValueError:
                continue
            if d.weekday() == wds.index(wd) and (best is None or abs(y - fy) < abs(best.year - fy)):
                best = d
        return best

    paths = sorted(glob.glob(os.path.join(raw.root, "zips", "201[6-9]", "**", "*.xls"), recursive=True)
                   + glob.glob(os.path.join(raw.root, "zips", "202[01]", "**", "*.xls"), recursive=True)
                   + glob.glob(os.path.join(raw.root, "x22", "2022-*.xls")))
    cands: dict[str, list[dict]] = {}
    for p in paths:
        b = os.path.basename(p)
        if "compact" in b or "veterans" in b or "columbus" in b:
            continue
        m = re.match(r"(20\d\d)", b)
        fy = int(m.group(1)) if m else int(re.search(r"zips[\\/](20\d\d)", p).group(1))
        try:
            s = xlrd.open_workbook(p).sheet_by_index(0)
        except Exception:
            continue
        cal = hdr = en = None
        for r in range(s.nrows):
            row = s.row_values(r)
            f = str(row[0]).strip().lower()
            if f.startswith("calendar date") and cal is None:
                cal = row
            if f.startswith("products on") and hdr is None:
                hdr = row
            if f.startswith("energy") and en is None:
                en = row
        if en is None or cal is None:
            continue
        ev, d = [], None
        for i in range(1, len(en)):
            if str(cal[i]).strip():
                d = pdate(str(cal[i]), fy) or d
            v = tstr(en[i])
            if not v:
                continue
            h = tstr(hdr[i]).lower() if hdr is not None and i < len(hdr) else ""
            if "closed" in v.lower():
                ev.append((d, "CLOSED", v))
            elif not re.match(r"\d\d:\d\d", v):
                ev.append((d, "?", v))
            else:
                k = "PRE" if h.startswith("pre") else "OPEN" if "open" in h else "REG" if "regular" in h else "HALT" if ("halt" in h or "close" in h) else "?" + h
                ev.append((d, k, v))
        if p.replace("\\", "/").split("/cme_raw/")[-1].startswith("x22"):
            key = "x_" + b
            if key not in raw.dl:
                continue
            url, where = raw.wb(key), "XLS fetched directly from " + raw.dl[key][1]
        else:
            zy = re.search(r"zips[\\/](20\d\d)", p).group(1)
            key = zy + "-holiday-calendars.zip"
            url, where = raw.wb(key), f'XLS "{b}" extracted from CME zip {raw.dl[key][1]}'
        seen = set()
        for i, (d, k, v) in enumerate(ev):
            if d is None or d in seen:
                continue
            if k == "CLOSED" or (k == "HALT" and v < "16:00"):
                seen.add(d)
                reopen = ""
                for e in ev[i + 1:]:
                    if e[1] == "OPEN":
                        reopen = plus1(e[2].replace(":", "")) if e[0] == d else f"{e[0]} {plus1(e[2].replace(':', ''))}"
                        break
                cands.setdefault(str(d), []).append(dict(
                    date=str(d), status="closed" if k == "CLOSED" else "early_halt",
                    halt="" if k == "CLOSED" else plus1(v.replace(":", "")), reopen=reopen, src=b, prio=4,
                    raw=f"{k} {v} CT", url=url, title=f"CME Globex holiday schedule XLS: {b}",
                    kind=where + "; row 'Energy, Metals & DME' of the sheet (times CT, +1h to ET)"))
    return cands


# --------------------------------------------------------------------------------------------- API
def api_candidates(raw: Raw, product: str):
    cands: dict[str, list[dict]] = {}
    files = sorted(glob.glob(os.path.join(raw.root, "api", "20*.json"))) + sorted(glob.glob(os.path.join(raw.root, "g2", "b_*.json")))
    for f in files:
        base = os.path.basename(f)[:-5]
        if base.startswith("b_"):
            frm = base[2:12]
            if frm not in raw.apix:
                continue
            url = f"https://web.archive.org/web/20240708161439/{raw.apix[frm]}"
            label = f"{base[2:]} (Wayback 2024-07-08 capture)"
        else:
            key = tuple(base.split("_"))
            if key not in raw.api:
                continue
            ts, u = raw.api[key]
            url = f"https://web.archive.org/web/{ts}/{u}"
            label = base
        try:
            j = json.load(open(f, encoding="utf-8"))
        except Exception:
            continue
        pr = [p for p in j.get("products", []) if p.get("globex") == product]
        if not pr:
            continue
        sc = pr[0]["tradingHours"]["schedules"]
        byd = {s["eventDate"]: s for s in sc}
        for s in sc:
            d = s["eventDate"]
            day = dt.date.fromisoformat(d)
            if day.weekday() >= 5:
                continue
            evs = s["events"]
            evtxt = ", ".join(f"{e['eventTime']} {e['marketEventType']}" for e in evs)
            halt = [e for e in evs if "08:00" <= e["eventTime"] < "16:00" and e["marketEventType"] in ("preopen", "closed")]
            base_note = f"JSON from the CME services API (trading-hours-by-product, product {product}). Times CT, +1h to ET. Events on {d}: {evtxt}."
            if halt:
                h = halt[0]
                reopen = ""
                for e in evs:
                    if e["marketEventType"] == "open" and e["eventTime"] >= "16:00":
                        reopen = plus1(e["eventTime"].replace(":", ""))
                        break
                nd = (day + dt.timedelta(days=1)).isoformat()
                if not reopen and nd in byd and byd[nd]["events"] and byd[nd]["events"][0]["eventTime"] >= "16:00":
                    for e in byd[nd]["events"]:
                        if e["marketEventType"] == "open":
                            reopen = nd + " " + plus1(e["eventTime"].replace(":", ""))
                            break
                cands.setdefault(d, []).append(dict(
                    date=d, status="early_halt", halt=plus1(h["eventTime"].replace(":", "")), reopen=reopen, src=label, prio=6,
                    raw=evtxt, url=url, title=f"CME trading-hours-by-product API, {product}, {label}",
                    kind="API", notes=base_note, apitype=h["marketEventType"]))
            elif not evs:
                if any(x["events"] for x in sc):
                    cands.setdefault(d, []).append(dict(
                        date=d, status="closed", halt="", reopen="", src=label, prio=6, raw="(no events)", url=url,
                        title=f"CME trading-hours-by-product API, {product}, {label}", kind="API",
                        notes=base_note + " No events on a weekday while the same capture returns events for the neighbouring days: closed, INFERRED."))
                else:
                    cands.setdefault(d, []).append(dict(
                        date=d, status="closed?", halt="", reopen="", src=label, prio=9, raw="(no events)", url=url,
                        title=f"CME trading-hours-by-product API, {product}, {label}", kind="API",
                        notes=base_note + " No events returned for the whole capture: NOT evidence of closure."))
            elif not any(e["marketEventType"] == "closed" for e in evs) and evs[0]["eventTime"] >= "16:00":
                op = [e for e in evs if e["marketEventType"] == "open"]
                if op:
                    cands.setdefault(d, []).append(dict(
                        date=d, status="closed", halt="", reopen=plus1(op[0]["eventTime"].replace(":", "")), src=label, prio=6,
                        raw=evtxt, url=url, title=f"CME trading-hours-by-product API, {product}, {label}", kind="API",
                        notes=base_note + " No day session (only the evening pre-open/open): closed, INFERRED from the absence of a day session."))
    return cands




# --------------------------------------------------------------------------------------------- resolution
START = dt.date(2009, 1, 1)   # first table year: the first year whose notices carry a NYMEX/COMEX Globex section
END = dt.date(2027, 12, 31)   # last year CME has published (API captures run to 2028-01-02)

# Published early closes that the 1m bars of CL and GC do NOT show (both end at the regular 17:00 or run on): not in the
# table, recorded in the .md as conflicts.
DROP = {
    "2011-02-18": "Presidents Day Friday: notice says early close 1515 CT; CL bars run to 17:00 ET, GC to 17:00 ET",
    "2011-05-27": "Memorial Day Friday: notice says early close 1515 CT; CL bars run to 17:00 ET, GC to 16:59 ET",
    "2011-07-01": "Independence Day Friday: notice says early close 1515 CT; CL and GC bars run to 17:00 ET",
    "2011-09-02": "Labor Day Friday: notice says early close 1515 CT; CL and GC bars run to 17:00 ET",
    "2011-10-07": "Columbus Day Friday: notice says early close 1515 CT; CL and GC bars run to 17:00 ET",
    "2013-07-03": "the only 2013-07-03 line of the NYMEX section is 'Early close for Dairy' (a section-bleed from the next block); CL and GC run to 17:00 ET",
}

# Published early-halt time contradicted by a dense 1m record. The row keeps the CME notice as its citation and uses the time the bars show.
OBSERVED = {
    "2009-11-27": "13:45", "2015-12-24": "13:15", "2018-12-24": "13:15",
    "2022-01-17": "13:00", "2022-02-21": "13:00", "2022-05-30": "13:00", "2022-07-04": "13:00", "2022-09-05": "13:00", "2022-11-24": "13:00",
    "2024-01-15": "13:30", "2024-02-19": "13:30", "2024-07-04": "13:30", "2025-11-27": "13:30",
}

NOTE23 = ("The 2023 PDF is a jumbled text extraction (stacked per-product columns), so the halt time is read from the 1m bars "
          "of CL and GC (both end at the same minute), cross-checked against the 12:00 / 13:30 / 12:45 CT events the PDF lists.")
MANUAL = [
    dict(date="2009-07-03", status="early_halt", halt="13:30", reopen="2009-07-05 18:00", key="2009-4th-of-july.pdf",
         title="CME Globex holiday trading hours: 2009-4th-of-july.pdf",
         notes="PDF, NYMEX, COMEX and DME Products on CME Globex: 'Friday, Jul 3: 1230 CT CME Globex close (No TAS Contracts will be open)'; "
               "'Sunday, Jul 5: 1700 CT Regular open for trade date Monday, Jul 6'. Added by hand: the parser skips lines that mention TAS."),
    dict(date="2010-04-02", status="closed", halt="", reopen="2010-04-04 18:00", key="2010-good-friday.pdf",
         title="CME Globex holiday trading hours: 2010-good-friday.pdf",
         notes="PDF, NYMEX & COMEX and DME Products on CME Globex: 'Friday, Apr 2: No CME Globex Trading on Good Friday'; Sunday Apr 4 1700 CT open for Monday Apr 5."),
    dict(date="2011-12-26", status="closed", halt="", reopen="18:00", key="2011-christmas.pdf",
         title="CME Globex holiday trading hours: 2011-christmas.pdf",
         notes="PDF, NYMEX, COMEX and DME Products on CME Globex: 'Monday, Dec 26: 1700 CT / 1800 ET Regular CME Globex open for trade date Tuesday, Dec 27' (no day session); "
               "the equity/rate/FX sections of the same notice say 'Christmas Day Observed - Globex closed'."),
    dict(date="2012-01-02", status="closed", halt="", reopen="18:00", key="2012-new-years.pdf",
         title="CME Globex holiday trading hours: 2012-new-years.pdf",
         notes="PDF, NYMEX, COMEX and DME Products on CME Globex: 'Monday, Jan 2: 1700 CT / 1800 ET Regular CME Globex open for trade date Tuesday, Jan 3' (no day session)."),
    dict(date="2012-04-06", status="closed", halt="", reopen="2012-04-08 18:00", key="2012-good-friday.pdf",
         title="CME Globex holiday trading hours: 2012-good-friday.pdf",
         notes="PDF, NYMEX & COMEX and DME Products on CME Globex: 'Friday, Apr 6: No CME Globex Trading on Good Friday'; Sunday Apr 8 1700 CT open for Monday Apr 9."),
    dict(date="2023-01-02", status="closed", halt="", reopen="18:00", key="2023-new-years-advisory.pdf",
         title="CME Group Clearing advisory: New Year's Day January 2, 2023 (Observed)",
         notes="CME Clearing memo: no intraday or end-of-day cycle, no settlement file on 2023-01-02; it gives no Globex hours. No bars for CL on the date (GC too). "
               "No 2023 CME Globex hours notice for this date survives in the Wayback captures (API captures for 2023 are empty)."),
    dict(date="2023-01-16", status="early_halt", halt="13:30", reopen="18:00", key="2023-mlk-day-advisory.pdf",
         title="CME Group Clearing advisory: Dr. Martin Luther King Jr. Day, January 16, 2023",
         notes="CME Clearing memo: holiday processing schedule (no settlement, no ITD cycle); it gives no Globex hours. Halt time read from the 1m bars of CL and GC "
               "(both end 13:30 ET). No 2023 Globex hours notice survives (no tradinghours PDF, the XLS is compact MGEX/DME only, the 2023 API captures are empty)."),
    dict(date="2023-02-20", status="early_halt", halt="13:30", reopen="18:00", key="2023-presidents-day-advisory.pdf",
         title="CME Group Clearing advisory: Presidents Day, February 20, 2023",
         notes="CME Clearing memo: holiday processing schedule; it gives no Globex hours. Halt time read from the 1m bars of CL and GC (both end 13:30 ET). "
               "No 2023 Globex hours notice survives for this date."),
    dict(date="2023-04-07", status="closed", halt="", reopen="2023-04-09 18:00", key="2023-good-friday-advisory.pdf",
         title="CME Group Clearing advisory: Good Friday, April 7th, 2023 (second correction)",
         notes="CME Clearing memo: only equities (including Bitcoin and Ether) are listed as open for an abbreviated session on 2023-04-07; the memo lists no energy or metals trading. "
               "CL has no bars on the date. GC has no bars from 2023-04-06 to 2023-04-14 (store gap), so GC's closure is not data-checked."),
    dict(date="2023-05-29", status="early_halt", halt="13:30", reopen="18:00", key="tradinghours_memorial-day-2023.pdf",
         title="CME Group trading hours: Memorial Day 2023 (PDF)", notes=NOTE23),
    dict(date="2023-06-19", status="early_halt", halt="14:30", reopen="18:00", key="tradinghours_juneteenth-2023.pdf",
         title="CME Group trading hours: Juneteenth 2023 (PDF)", notes=NOTE23),
    dict(date="2023-07-04", status="early_halt", halt="13:30", reopen="18:00", key="tradinghours_4th-of-july-2023.pdf",
         title="CME Group trading hours: Independence Day 2023 (PDF)", notes=NOTE23),
    dict(date="2023-09-04", status="early_halt", halt="13:00", reopen="18:00", key="tradinghours_labor-day-2023.pdf",
         title="CME Group trading hours: Labor Day 2023 (PDF)",
         notes="PDF: Monday 4 Sep, Energy block '12:00 (PREOPEN) HALT, 17:00 (OPEN)' = 13:00 ET; the 1m bars of CL and GC end 13:00 ET. "
               "The CME API capture for the same date says '13:30 preopen' (14:30 ET), which the bars contradict: the PDF is used. " + NOTE23),
    dict(date="2023-11-23", status="early_halt", halt="14:30", reopen="18:00", key="tradinghours_thanksgiving-day-2023.pdf",
         title="CME Group trading hours: Thanksgiving Day 2023 (PDF)", notes=NOTE23),
    dict(date="2023-11-24", status="early_halt", halt="13:45", reopen="", key="tradinghours_thanksgiving-day-2023.pdf",
         title="CME Group trading hours: Thanksgiving Day 2023 (PDF)",
         notes="PDF: Friday 24 Nov lists '12:45 (CLOSED)' (CT) = 13:45 ET; the 1m bars of CL and GC end 13:45 ET."),
    dict(date="2024-01-01", status="closed", halt="", reopen="18:00", key="tradinghours_new-years-day-2024.pdf",
         title="CME Group trading hours: New Year's Day 2024 (PDF)",
         notes="PDF: Monday 1 Jan 2024, Energy and Metals rows show only '16:00 (PREOPEN) 17:00 (OPEN)' for trade date Tue 2 Jan (no day session on the 1st)."),
]


def pick(cands_for_date):
    live = [c for c in cands_for_date if c["status"] != "closed?"]
    return sorted(live, key=lambda c: c["prio"])


def resolve(raw: Raw, group: str):
    product = "CL" if group == "energy" else "GC"
    pdf, xls, api = pdf_candidates(raw), xls_candidates(raw), api_candidates(raw, product)
    allc: dict[str, list[dict]] = {}
    for src in (pdf, xls, api):
        for d, l in src.items():
            allc.setdefault(d, []).extend(l)
    man = {m["date"]: m for m in MANUAL}
    rows, conflicts = {}, []
    for d in sorted(set(allc) | set(man)):
        day = dt.date.fromisoformat(d)
        if day < START or day > END:
            continue
        if d in DROP:
            conflicts.append((d, "DROPPED", DROP[d]))
            continue
        if d in man:
            m = man[d]
            rows[d] = dict(date=d, status=m["status"], halt=m["halt"], reopen=m["reopen"], url=raw.wb(m["key"]), title=m["title"], notes=m["notes"])
            others = [c for c in pick(allc.get(d, [])) if c["status"] != "regular" and (c["status"], c["halt"]) != (m["status"], m["halt"])]
            for c in others:
                conflicts.append((d, "MANUAL-VS-SOURCE", f"{c['src']} says {c['status']} {c['halt']}; row uses {m['status']} {m['halt']}"))
            continue
        cs = pick(allc[d])
        if not cs:
            continue
        best = cs[0]
        if best["status"] == "regular":
            continue
        row = dict(date=d, status=best["status"], halt=best["halt"], reopen=best["reopen"], url=best["url"], title=best["title"],
                   notes=(best.get("notes") or f"{best['kind']}: {best['raw']} (times CT, +1h to ET).").strip())
        same = sorted({c["src"] for c in cs[1:] if (c["status"], c["halt"]) == (best["status"], best["halt"])})
        if same:
            row["notes"] += " Same in: " + ", ".join(same) + "."
        diff = [c for c in cs[1:] if (c["status"], c["halt"]) != (best["status"], best["halt"])]
        for c in diff:
            if c["status"] == "regular":
                conflicts.append((d, "REGULAR-IN-OTHER", f"{c['src']} lists only a regular-close halt {c['halt']} ET; row uses {best['src']}"))
            else:
                conflicts.append((d, "SOURCE-DISAGREE", f"{best['src']} {best['status']} {best['halt']} vs {c['src']} {c['status']} {c['halt']}"))
        if d in OBSERVED and best["status"] == "early_halt" and OBSERVED[d] != best["halt"]:
            row["notes"] += (f" [OBSERVED] The notice's {best['halt']} ET is contradicted by the 1m bars of CL and GC, which end at {OBSERVED[d]} ET;"
                             f" the table uses {OBSERVED[d]}.")
            conflicts.append((d, "OBSERVED-OVERRIDE", f"{best['src']} {best['halt']} ET -> bars {OBSERVED[d]} ET"))
            row["halt"] = OBSERVED[d]
        rows[d] = row
    return rows, conflicts


def validate(rows):
    for d, r in rows.items():
        assert r["status"] in ("closed", "early_halt"), (d, r)
        if r["status"] == "closed":
            assert not r["halt"], (d, r)
        else:
            assert re.fullmatch(r"\d\d:\d\d", r["halt"]) and r["halt"] < "17:00", (d, r)
        ro = r["reopen"]
        if ro and " " not in ro and r["status"] == "early_halt":
            assert ro > r["halt"], (d, r)
        assert r["url"].startswith("https://"), (d, r)


def write_csv(path, rows):
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["date", "status", "halt_et", "reopen_et", "source_url", "source_title", "notes"])
        for d in sorted(rows):
            r = rows[d]
            w.writerow([d, r["status"], r["halt"], r["reopen"], r["url"], r["title"], r["notes"]])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw", required=True)
    ap.add_argument("--out", default=None, help="directory for cme_energy_holidays.csv and cme_metals_holidays.csv")
    ap.add_argument("--dump", action="store_true", help="print candidates and stop")
    ap.add_argument("--conflicts", action="store_true", help="print the conflicts list")
    a = ap.parse_args()
    sys.dont_write_bytecode = True
    raw = Raw(a.raw)
    if a.dump:
        for name, c in (("PDF", pdf_candidates(raw)), ("XLS", xls_candidates(raw)), ("API-CL", api_candidates(raw, "CL")), ("API-GC", api_candidates(raw, "GC"))):
            print("=====", name)
            for d in sorted(c):
                for x in c[d]:
                    print(d, x["status"], x["halt"], "|", x["reopen"], "|", x["src"], "|", x["raw"][:70])
        return 0
    out = {}
    for group in ("energy", "metals"):
        rows, conflicts = resolve(raw, group)
        validate(rows)
        out[group] = rows
        n_c = sum(1 for r in rows.values() if r["status"] == "closed")
        print(f"{group}: {len(rows)} rows ({n_c} closed, {len(rows) - n_c} early_halt), {min(rows)}..{max(rows)}")
        if a.conflicts:
            for c in conflicts:
                print("  ", group, *c)
        if a.out:
            write_csv(os.path.join(a.out, f"cme_{group}_holidays.csv"), rows)
    diff = sorted(d for d in set(out["energy"]) | set(out["metals"]) if out["energy"].get(d, {}).get("status") != out["metals"].get(d, {}).get("status")
                  or out["energy"].get(d, {}).get("halt") != out["metals"].get(d, {}).get("halt"))
    print("dates where energy and metals differ:", diff)
    return 0


if __name__ == "__main__":
    sys.exit(main())

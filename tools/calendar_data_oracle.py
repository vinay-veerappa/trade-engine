"""Build the 1m-data fixtures for crates/te_core/tests/calendar_data_oracle.rs (P6C).

Reads the bar store READ ONLY (never writes into it) and writes one small fixture per root:

    crates/te_core/tests/fixtures/calendar_oracle_<ROOT>.json

The fixture is calendar-independent: it records "islands" of 1m bars, not verdicts. The Rust test
holds the calendar and does the comparing, so a calendar change never needs a new fixture.

An island is a maximal run of bars that
  - lie on the same side of the daily 17:00 and 18:00 ET boundaries (a bar stamped 17:00-17:59 ET
    is in the maintenance halt and forms its own island, never part of a session's island), and
  - have no gap longer than GAP_MINUTES between consecutive bars (10, so that the 15-minute 16:15-16:30 ET
    equity-index halt of the pre-2021-06-28 eras splits an island and any bar inside it is visible).

Bars are OPEN-stamped (MARKET_DATA_ARCHITECTURE.md 3.2.1): the bar stamped t covers [t, t+1min).
So an island [first, last] occupies the instants [first, last + 60 s).

Fixture JSON:
  header : root, store path, bar_stamp, gap_minutes, coverage_first_utc/last_utc, files[{name,bytes,rows,sha256}]
  islands: [[first_delta, span, count], ...]  all in whole seconds, UTC epoch;
           first_delta = first - (previous island's last), the first island's is absolute;
           span = last - first; count = bars in the island.

Usage:  python tools/calendar_data_oracle.py [--store DIR] [--out DIR] [ROOT ...]
Needs pyarrow and numpy (installed into the private venv for this script only; not a package dependency).
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pyarrow.parquet as pq

ROOTS = ["ES", "NQ", "YM", "RTY", "CL", "GC"]
GAP_MINUTES = 10
DEFAULT_STORE = Path(r"C:\Users\vinay\tvDownloadOHLC\data\market\bars\1m")
ET = ZoneInfo("America/New_York")


def et_offsets_by_utc_hour(h0: int, h1: int) -> np.ndarray:
    """UTC offset of America/New_York, in seconds, for every UTC hour h0..h1 (hours since epoch).

    DST changes land on a UTC hour boundary, so one value per UTC hour is exact.
    """
    out = np.empty(h1 - h0 + 1, dtype=np.int64)
    for i in range(h1 - h0 + 1):
        t = dt.datetime.fromtimestamp((h0 + i) * 3600, dt.timezone.utc)
        out[i] = int(t.astimezone(ET).utcoffset().total_seconds())
    return out


def islands_for(ts: np.ndarray) -> list[list[int]]:
    """ts: sorted unique int64 epoch seconds (bar open stamps)."""
    h0, h1 = int(ts[0] // 3600), int(ts[-1] // 3600)
    off = et_offsets_by_utc_hour(h0, h1)
    et_secs = ts + off[(ts // 3600) - h0]
    tod = et_secs % 86400
    day = et_secs // 86400
    in_halt = (tod >= 17 * 3600) & (tod < 18 * 3600)
    # session-day key: 18:00 ET opens the next trade date, so shift by 6h; halt bars get their own key.
    key = np.where(in_halt, day * 2 + 1, ((et_secs + 6 * 3600) // 86400) * 2)
    brk = np.zeros(len(ts), dtype=bool)
    brk[0] = True
    brk[1:] = (np.diff(key) != 0) | (np.diff(ts) > GAP_MINUTES * 60)
    starts = np.flatnonzero(brk)
    ends = np.append(starts[1:] - 1, len(ts) - 1)
    return [[int(ts[s]), int(ts[e]), int(e - s + 1)] for s, e in zip(starts, ends)]


def sha256_of(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def build_root(store: Path, root: str) -> dict:
    files = sorted((store / root).glob("*.parquet"))
    if not files:
        raise SystemExit(f"no parquet files for {root} under {store}")
    parts, meta = [], []
    for p in files:
        t = pq.read_table(p, columns=["ts_open"])
        a = t.column("ts_open").to_numpy().astype("datetime64[s]").astype("int64")
        parts.append(a)
        meta.append({"name": p.name, "bytes": p.stat().st_size, "rows": int(t.num_rows), "sha256": sha256_of(p)})
    ts = np.unique(np.concatenate(parts))
    isl = islands_for(ts)
    enc, prev_last = [], 0
    for first, last, n in isl:
        enc.append([first - prev_last, last - first, n])
        prev_last = last
    utc = dt.timezone.utc
    return {
        "header": {
            "root": root,
            "generator": "tools/calendar_data_oracle.py",
            "store": str(store / root),
            "bar_stamp": "open (bar t covers [t, t+60s))",
            "gap_minutes": GAP_MINUTES,
            "island_rule": "split at >gap_minutes between bars and at the 17:00 and 18:00 ET boundaries",
            "coverage_first_utc": dt.datetime.fromtimestamp(int(ts[0]), utc).isoformat(),
            "coverage_last_utc": dt.datetime.fromtimestamp(int(ts[-1]), utc).isoformat(),
            "bars": int(len(ts)),
            "files": meta,
        },
        "islands": enc,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--store", type=Path, default=DEFAULT_STORE)
    ap.add_argument("--out", type=Path, default=Path(__file__).resolve().parents[1] / "crates/te_core/tests/fixtures")
    ap.add_argument("roots", nargs="*", default=ROOTS)
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    for r in a.roots:
        fx = build_root(a.store, r)
        path = a.out / f"calendar_oracle_{r}.json"
        path.write_text(json.dumps({"header": fx["header"], "islands": fx["islands"]}, separators=(",", ":")) + "\n")
        print(f"{r}: {len(fx['islands'])} islands, {fx['header']['bars']} bars -> {path} ({path.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

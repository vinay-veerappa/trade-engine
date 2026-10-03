"""P2a gate 3 (local, read-only): the Rust ledger against the Python ledger over REAL ledgers.

    python tools/ledger_parity.py [LEDGER.db ...]

With no arguments it reads every live ledger under tvDownloadOHLC/data/trade_engine (PM-A,
PM-B, the 0DTE sim, the scan ledger ...). Each file is opened `mode=ro` through a sqlite URI,
so neither this tool nor a bug in it can write a ledger; nothing it reads is committed.

For every ledger: each event row is re-encoded by both codecs (bytes or refusal must match),
then the whole log is folded by both (canonical AccountState bytes or refusal must match).
Exit 0 = identical everywhere, 1 = a difference (printed), 2 = a ledger could not be read.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "src"))

import trade_engine_rs as rs  # a missing module is an ERROR (D5)
from ledger_gen import dumps, encoded, py_fold_all, py_reencode

from trade_engine.ledger import codec
from trade_engine.ledger.store import Ledger

DATA = Path.home() / "tvDownloadOHLC" / "data" / "trade_engine"
STRICT = {"strict", "unsupported"}


def _rs(call, *args):
    try:
        return ("ok", bytes(call(*args)))
    except ValueError as err:
        if len(err.args) != 2:
            raise
        return ("err", err.args[0], err.args[1])


def _same(py, rust) -> bool:
    if py[0] == "ok" and rust[0] == "ok":
        return py[1] == rust[1]
    if rust[0] == "err" and rust[1] in STRICT:
        return True  # sanctioned codec asymmetry (test_ledger_codec_parity), counted below
    return py[0] == rust[0] == "err" and (py[1], py[2]) == (rust[1], rust[2])


def check(path: Path) -> int:
    uri = path.resolve().as_uri().replace("file:///", "file:/") + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT * FROM events ORDER BY seq").fetchall()
    finally:
        conn.close()
    events = [Ledger._row_to_event(r) for r in rows]

    bad = strict = 0
    for e in events:
        blob = dumps(codec.encode_event(e))
        py, rust = py_reencode(blob), _rs(rs.ledger_reencode, blob)
        strict += rust[0] == "err" and rust[1] in STRICT
        if not _same(py, rust):
            bad += 1
            if bad <= 3:
                print(f"  codec differs at seq={e.seq} kind={e.kind.value}\n    py:   {py!r:.300}\n    rust: {rust!r:.300}")

    py, rust = py_fold_all(events), _rs(rs.ledger_fold_all, encoded(events))
    fold_ok = _same(py, rust)
    if not fold_ok:
        print(f"  fold differs\n    py:   {py!r:.400}\n    rust: {rust!r:.400}")
    verdict = "folded" if py[0] == "ok" else f"refused ({py[1]})"
    print(f"{path.name}: {len(events)} events, codec {len(events) - bad}/{len(events)} identical"
          f" ({strict} strict), fold {'identical' if fold_ok else 'DIFFERS'}, python {verdict}")
    return 0 if bad == 0 and fold_ok else 1


def main(argv: list[str]) -> int:
    paths = [Path(a) for a in argv] or sorted(p for p in DATA.glob("*.db") if "refusals" not in p.name)
    if not paths:
        print(f"no ledgers under {DATA}")
        return 2
    worst = 0
    for p in paths:
        try:
            worst = max(worst, check(p))
        except sqlite3.Error as err:
            print(f"{p}: unreadable ({err})")
            worst = 2
    return worst


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

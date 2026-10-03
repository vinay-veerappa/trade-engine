"""P2b gate (local, read-only): the PRODUCTION ledger path against the FROZEN Python oracle,
over REAL ledgers.

    python tools/ledger_parity.py [LEDGER.db ...]

With no arguments it reads every live ledger under tvDownloadOHLC/data/trade_engine (PM-A,
PM-B, the 0DTE sim, the scan ledger ...). The oracle side opens the file `mode=ro` through a
sqlite URI; the production side is `LedgerReader`, which opens it `mode=ro` too. Neither
can write a ledger; nothing read is committed.

The oracle is `tests/frozen_ledger` (the pre-port Python codec, fold and mirror, P2b). For
every ledger:

* codec: each row decoded by the production store (`Ledger._row_to_event`) and by the
  frozen codec must encode to identical bytes (both encodings, production and frozen), or
  both must refuse with the same kind;
* fold: for every account, `LedgerReader.state(account)` (the Rust fold handle) must equal
  the frozen `fold_account` of the frozen-decoded log, canonical bytes or refusal kind; and
  `LedgerReader.fold()` must equal the frozen `fold`.

Exit 0 = identical everywhere, 1 = a difference (printed), 2 = a ledger could not be read.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "src"))

import trade_engine_rs  # noqa: E402,F401 - a missing module is an ERROR (D5)
from frozen_ledger import codec as frozen_codec  # noqa: E402
from ledger_gen import canon, dumps, kind_of, py_fold, py_fold_all  # noqa: E402

from trade_engine.ledger import codec as prod_codec  # noqa: E402
from trade_engine.ledger.reader import LedgerReader  # noqa: E402
from trade_engine.ledger.store import Ledger  # noqa: E402

DATA = Path.home() / "tvDownloadOHLC" / "data" / "trade_engine"


def _outcome(call, *args):
    try:
        return ("ok", call(*args))
    except Exception as err:  # noqa: BLE001 - classified, never swallowed
        return ("err", kind_of(err), str(err))


def _same(a, b) -> bool:
    """Bytes identical, or both refused with the same kind (messages are Rust's since P2b)."""
    return a[0] == b[0] and (a[1] == b[1] if a[0] == "ok" else a[1] == b[1])


def _frozen_row(row) -> object:
    return frozen_codec.decode_event({
        "account": row["account"], "kind": row["kind"], "payload": json.loads(row["payload_json"]),
        "ts_utc": row["ts_utc"], "command_id": row["command_id"],
        "schema_version": row["schema_version"], "seq": row["seq"],
    })


def check(path: Path) -> int:
    uri = path.resolve().as_uri().replace("file:///", "file:/") + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT * FROM events ORDER BY seq").fetchall()
    finally:
        conn.close()

    bad = 0
    oracle_events = []
    for row in rows:
        frozen = _outcome(_frozen_row, row)
        prod = _outcome(Ledger._row_to_event, row)
        if frozen[0] == "ok":
            oracle_events.append(frozen[1])
        f_bytes = _outcome(lambda e: dumps(frozen_codec.encode_event(e)), frozen[1]) if frozen[0] == "ok" else frozen
        p_frozen = _outcome(lambda e: dumps(frozen_codec.encode_event(e)), prod[1]) if prod[0] == "ok" else prod
        p_prod = _outcome(lambda e: dumps(prod_codec.encode_event(e)), prod[1]) if prod[0] == "ok" else prod
        if not (_same(f_bytes, p_frozen) and _same(f_bytes, p_prod)):
            bad += 1
            if bad <= 3:
                print(f"  codec differs at seq={row['seq']} kind={row['kind']}\n    frozen: {f_bytes!r:.300}"
                      f"\n    prod(frozen enc): {p_frozen!r:.300}\n    prod(prod enc):   {p_prod!r:.300}")

    reader = LedgerReader(path).open()
    try:
        accounts = reader.accounts()
        acct_bad = 0
        for account in accounts:
            oracle = py_fold(oracle_events, account)
            prod = _outcome(lambda a: dumps(canon(reader.state(a))), account)
            if not _same(oracle, prod):
                acct_bad += 1
                print(f"  state({account}) differs\n    frozen: {oracle!r:.400}\n    prod:   {prod!r:.400}")
        oracle_all = py_fold_all(oracle_events)
        prod_all = _outcome(lambda: dumps(canon(reader.fold())))
        fold_ok = _same(oracle_all, prod_all)
        if not fold_ok:
            print(f"  fold differs\n    frozen: {oracle_all!r:.400}\n    prod:   {prod_all!r:.400}")
    finally:
        reader.close()

    verdict = "folded" if oracle_all[0] == "ok" else f"refused ({oracle_all[1]})"
    print(f"{path.name}: {len(rows)} events, codec {len(rows) - bad}/{len(rows)} identical, "
          f"state {len(accounts) - acct_bad}/{len(accounts)} accounts identical, "
          f"fold {'identical' if fold_ok else 'DIFFERS'}, oracle {verdict}")
    return 0 if bad == 0 and acct_bad == 0 and fold_ok else 1


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

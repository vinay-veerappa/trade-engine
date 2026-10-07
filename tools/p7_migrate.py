"""P7 stored-ledger migration: re-spell every decimal in a ledger to the canonical form.

    python tools/p7_migrate.py SRC DST              # forward: SRC is read, DST is created
    python tools/p7_migrate.py --reverse DST BACK   # reverse: BACK is rebuilt from DST

Design: docs/RUST_PORT.md "P7 verification and boundary".

* Never in place. SRC is opened `file:<path>?mode=ro` and is the backup, untouched; DST must
  not exist. Neither may be under ``tvDownloadOHLC/data`` unless ``--i-own-this-ledger`` is
  given (the owner's flag; this tool is never run against that directory by the build).
* Every `{"d": ...}` in an event payload (and an outbox payload) is re-spelled with
  `codec.canon_decimal`; a value outside the bound aborts the migration (I5).
* `OrdersCreated.fingerprint` is rehashed. A close order's fingerprint is re-derived from the
  stored order (and checked against the old value first); an intent fingerprint's inputs are
  not stored, so it becomes `sha256("p7:1:" + old)` and is flagged `opaque`. Old -> new goes
  into the table `p7_key_map(old, new, event_seq, kind)` of DST, which a replay presenting the
  old key consults (`Ledger.fingerprint_alias`).
* Balances and positions are re-folded from both files and compared BY VALUE per account.
* The original payload text of every changed row is kept in `p7_original`, so `--reverse`
  restores the source's rows byte for byte; it proves it by hashing the dumped rows against
  the digest recorded at migration time.
* Journal mode TRUNCATE on every file written (the owner's data policy denies delete/rename).
  DST is complete only when its `p7_state` says so; a failed run leaves it `incomplete` and
  `--reverse` refuses it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import trade_engine_rs  # noqa: E402,F401 - a missing extension is an ERROR (D5)

from trade_engine.ledger import _rs, codec  # noqa: E402
from trade_engine.ledger.codec import DecimalRangeError, canon_decimal  # noqa: E402
from trade_engine.ledger.reader import LedgerReader  # noqa: E402
from decimal import Decimal  # noqa: E402

PROTECTED = Path.home() / "tvDownloadOHLC" / "data"
OPAQUE_PREFIX = "p7:1:"
TOOL_VERSION = "1"


class MigrationError(RuntimeError):
    pass


# --- guards ---------------------------------------------------------------------------------


def _under(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


def guard(path: Path, *, owns: bool) -> None:
    if _under(path, PROTECTED) and not owns:
        raise MigrationError(f"{path} is under {PROTECTED}; refused without --i-own-this-ledger")


def _ro(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _new(path: Path) -> sqlite3.Connection:
    if path.exists():
        raise MigrationError(f"{path} exists; the migration never writes in place")
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    if conn.execute("PRAGMA journal_mode=TRUNCATE").fetchone()[0].lower() != "truncate":
        raise MigrationError("journal mode TRUNCATE could not be set")
    return conn


# --- digest and spelling -----------------------------------------------------------------------


def rows_digest(conn: sqlite3.Connection) -> tuple[str, dict[str, int]]:
    """sha256 of the dumped `events`, `meta` and `outbox` rows (what a ledger IS)."""
    h = hashlib.sha256()
    counts = {}
    for table, order in (("events", "seq"), ("meta", "key"), ("outbox", "id")):
        rows = [tuple(r) for r in conn.execute(f"SELECT * FROM {table} ORDER BY {order}")]
        counts[table] = len(rows)
        h.update(table.encode() + b"\n")
        for row in rows:
            h.update(json.dumps(row, separators=(",", ":"), ensure_ascii=True).encode() + b"\n")
    return h.hexdigest(), counts


def respell(node):
    """The tree with every `{"d": text}` in canonical spelling; refuses outside the bound."""
    if isinstance(node, dict):
        if set(node) == {"d"} and isinstance(node["d"], str):
            try:
                return {"d": canon_decimal(Decimal(node["d"]))}
            except (DecimalRangeError, ArithmeticError, ValueError) as err:
                raise MigrationError(f"decimal {node['d']!r} cannot be spelled canonically: {err}") from err
        return {k: respell(v) for k, v in node.items()}
    if isinstance(node, list):
        return [respell(v) for v in node]
    return node


def stored(tree) -> str:
    """The payload text exactly as the ledger stores it (Rust sorts and validates it)."""
    return _rs.call(_rs.rs.ledger_check_payload, json.dumps(tree, separators=(",", ":"))).decode("ascii")


def order_fp(order_tree) -> str:
    return _rs.call(_rs.rs.oms_order_fingerprint, json.dumps(order_tree, separators=(",", ":")))


def _first_order(tree):
    try:
        orders = tree["f"]["orders"]["t"]
    except (KeyError, TypeError):
        return None
    return orders if isinstance(orders, list) and len(orders) == 1 else None


def rehash(old_tree, new_tree) -> tuple[str, str]:
    """(new fingerprint, kind): re-derived for a one-order close, opaque otherwise."""
    old = old_tree["f"]["fingerprint"]
    orders = _first_order(old_tree)
    if orders is not None:
        try:
            if order_fp(orders[0]) == old:
                return order_fp(_first_order(new_tree)[0]), "order"
        except Exception:  # noqa: BLE001 - not derivable: opaque, below
            pass
    return hashlib.sha256((OPAQUE_PREFIX + old).encode()).hexdigest(), "opaque"


# --- forward ---------------------------------------------------------------------------------


def _fold_canon(path: Path) -> dict[str, object]:
    reader = LedgerReader(path).open()
    try:
        return {a: codec.text(codec.canon(reader.state(a))) for a in reader.accounts()}
    finally:
        reader.close()


def migrate(src: Path, dst: Path, *, owns: bool = False) -> dict:
    guard(src, owns=owns)
    guard(dst, owns=owns)
    if src.resolve() == dst.resolve():
        raise MigrationError("source and destination are the same file")
    s = _ro(src)
    try:
        digest, counts = rows_digest(s)
        ddl = [r["sql"] for r in s.execute("SELECT sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'")]
        events = s.execute("SELECT * FROM events ORDER BY seq").fetchall()
        meta = s.execute("SELECT * FROM meta ORDER BY key").fetchall()
        outbox = s.execute("SELECT * FROM outbox ORDER BY id").fetchall()
        cols_events = [r[1] for r in s.execute("PRAGMA table_info(events)")]
        cols_outbox = [r[1] for r in s.execute("PRAGMA table_info(outbox)")]
        cols_meta = [r[1] for r in s.execute("PRAGMA table_info(meta)")]
    finally:
        s.close()
    # the whole transform runs before DST exists: a refusal writes nothing
    new_events, originals, key_map = [], [], []
    for row in events:
        old_text = row["payload_json"]
        old_tree = json.loads(old_text)
        new_tree = respell(old_tree)
        if row["kind"] == "OrdersCreated":
            new_fp, kind = rehash(old_tree, new_tree)
            key_map.append((old_tree["f"]["fingerprint"], new_fp, row["seq"], kind))
            new_tree["f"]["fingerprint"] = new_fp
        new_text = stored(new_tree)
        d = dict(row)
        d["payload_json"] = new_text
        new_events.append(d)
        if new_text != old_text:
            originals.append(("events", row["seq"], old_text))
    new_outbox = []
    for row in outbox:
        d = dict(row)
        try:
            tree = json.loads(row["payload_json"])
        except (TypeError, ValueError):
            new_outbox.append(d)
            continue
        new_text = json.dumps(respell(tree), separators=(",", ":"), sort_keys=True)
        if new_text != row["payload_json"]:
            originals.append(("outbox", row["id"], row["payload_json"]))
            d["payload_json"] = new_text
        new_outbox.append(d)
    d_conn = _new(dst)
    try:
        for statement in ddl:
            d_conn.execute(statement)
        d_conn.execute("CREATE TABLE p7_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        d_conn.execute(
            "CREATE TABLE p7_key_map (old TEXT NOT NULL, new TEXT NOT NULL, event_seq INTEGER NOT NULL, "
            "kind TEXT NOT NULL, PRIMARY KEY (old, new, event_seq))"
        )
        d_conn.execute("CREATE INDEX ix_p7_key_map_old ON p7_key_map (old)")
        d_conn.execute(
            "CREATE TABLE p7_original (tbl TEXT NOT NULL, row_id INTEGER NOT NULL, payload_json TEXT NOT NULL, "
            "PRIMARY KEY (tbl, row_id))"
        )
        d_conn.execute("INSERT INTO p7_meta VALUES ('state','incomplete')")
        d_conn.commit()
        for table, cols, rows in (("events", cols_events, new_events), ("meta", cols_meta, [dict(m) for m in meta]), ("outbox", cols_outbox, new_outbox)):
            marks = ",".join("?" for _ in cols)
            d_conn.executemany(
                f"INSERT INTO {table} ({','.join(cols)}) VALUES ({marks})", [tuple(r[c] for c in cols) for r in rows]
            )
        d_conn.executemany("INSERT INTO p7_key_map VALUES (?,?,?,?)", key_map)
        d_conn.executemany("INSERT INTO p7_original VALUES (?,?,?)", originals)
        d_conn.executemany(
            "INSERT INTO p7_meta VALUES (?,?)",
            [
                ("source_sha256", digest),
                ("source_counts", json.dumps(counts)),
                ("source_ddl", json.dumps(ddl)),
                ("tool_version", TOOL_VERSION),
            ],
        )
        d_conn.commit()
        # by-value equality of every account's fold, before the migration is declared complete
        before, after = _fold_canon(src), _fold_canon_conn(d_conn, dst)
        if before != after:
            bad = sorted(a for a in set(before) | set(after) if before.get(a) != after.get(a))
            raise MigrationError(f"fold differs by value for accounts {bad}")
        d_conn.execute("UPDATE p7_meta SET value='complete' WHERE key='state'")
        d_conn.commit()
    finally:
        d_conn.close()
    return {
        "events": len(new_events),
        "respelled_rows": sum(1 for t, _i, _o in originals if t == "events"),
        "accounts": len(before),
        "value_equal": True,
        "map_total": len(key_map),
        "map_order": sum(1 for k in key_map if k[3] == "order"),
        "map_opaque": sum(1 for k in key_map if k[3] == "opaque"),
        "source_sha256": digest,
    }


def _fold_canon_conn(conn: sqlite3.Connection, path: Path) -> dict[str, object]:
    conn.commit()  # the rows are on disk for the reader below
    return _fold_canon(path)


# --- reverse ---------------------------------------------------------------------------------


def reverse(migrated: Path, back: Path, *, owns: bool = False) -> dict:
    guard(migrated, owns=owns)
    guard(back, owns=owns)
    m = _ro(migrated)
    try:
        info = {r["key"]: r["value"] for r in m.execute("SELECT key, value FROM p7_meta")}
        if info.get("state") != "complete":
            raise MigrationError(f"{migrated} is not a complete P7 migration")
        ddl = json.loads(info["source_ddl"])
        original = {(r["tbl"], r["row_id"]): r["payload_json"] for r in m.execute("SELECT * FROM p7_original")}
        tables = {}
        for table, order in (("events", "seq"), ("meta", "key"), ("outbox", "id")):
            cols = [r[1] for r in m.execute(f"PRAGMA table_info({table})")]
            tables[table] = (cols, [dict(r) for r in m.execute(f"SELECT * FROM {table} ORDER BY {order}")])
    finally:
        m.close()
    b = _new(back)
    try:
        for statement in ddl:
            b.execute(statement)
        for table, (cols, rows) in tables.items():
            key = {"events": "seq", "outbox": "id"}.get(table)
            out = []
            for r in rows:
                if key is not None and (table, r[key]) in original:
                    r = {**r, "payload_json": original[(table, r[key])]}
                out.append(tuple(r[c] for c in cols))
            marks = ",".join("?" for _ in cols)
            b.executemany(f"INSERT INTO {table} ({','.join(cols)}) VALUES ({marks})", out)
        b.commit()
        digest, counts = rows_digest(b)
    finally:
        b.close()
    ok = digest == info["source_sha256"]
    return {"restored_sha256": digest, "recorded_sha256": info["source_sha256"], "equal": ok, "counts": counts}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("first")
    ap.add_argument("second")
    ap.add_argument("--reverse", action="store_true")
    ap.add_argument("--i-own-this-ledger", action="store_true", dest="owns")
    args = ap.parse_args(argv)
    try:
        if args.reverse:
            report = reverse(Path(args.first), Path(args.second), owns=args.owns)
            print(json.dumps(report, indent=2))
            return 0 if report["equal"] else 1
        report = migrate(Path(args.first), Path(args.second), owns=args.owns)
    except MigrationError as err:
        print(f"p7_migrate: refused: {err}", file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""P7: the one-shot stored-ledger migration (tools/p7_migrate.py).

An "old" ledger is built the only honest way available now that production writes canonical
decimals: run a real desk, then rewrite the file's rows the way a pre-P7 build left them
(decimals padded with zeros, the intent fingerprint spelled over `str(Decimal)`).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from ledger_gen import norm
from test_option_oms import ACCOUNT, D, P260, P270, SESSIONS, Desk

from tools import p7_migrate as pm
from trade_engine.clock.replay import ReplayClock
from trade_engine.domain.instruments import Side
from trade_engine.domain.option_orders import CloseStructure
from trade_engine.ledger import Ledger
from trade_engine.oms.manager import IdempotencyConflictError
from trade_engine.oms.options import OptionOrderManager
from trade_engine.sim import SnapshotVenue


def pad(node):
    """Spell every decimal the way a pre-P7 build could have: trailing zeros kept."""
    if isinstance(node, dict):
        if set(node) == {"d"} and isinstance(node["d"], str):
            text = node["d"]
            return {"d": text + ("0" if "." in text else ".00")}
        return {k: pad(v) for k, v in node.items()}
    if isinstance(node, list):
        return [pad(v) for v in node]
    return node


def file_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def make_old_ledger(tmp_path) -> tuple[Path, list]:
    """A real desk's ledger rewritten as a pre-P7 file. Returns (path, intents it opened)."""
    home = tmp_path / "desk"
    home.mkdir()
    desk = Desk(home)
    intents = [
        desk.intent(P270, Side.SELL, "2", limit="10.00", target="5.00", name="csp"),
        desk.intent(P260, Side.SELL, "3", limit="6.00", target="3.00", name="bps"),
    ]
    for intent in intents:
        desk.oms.open(intent)
    desk.snapshot(SESSIONS[0])
    desk.after_close(SESSIONS[0])
    desk.oms.close(ACCOUNT, CloseStructure("csp:entry", "dte", "csp:close"))
    desk.snapshot(SESSIONS[1])
    desk.ledger.close()
    path = home / "ledger.db"
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    legacy = {i.command_id: OptionOrderManager._intent_fingerprint(i, legacy=True) for i in intents}
    for row in conn.execute("SELECT seq, kind, command_id, payload_json FROM events").fetchall():
        tree = pad(json.loads(row["payload_json"]))
        if row["kind"] == "OrdersCreated":
            if row["command_id"] in legacy:
                tree["f"]["fingerprint"] = legacy[row["command_id"]]
            else:  # a one-order close: its fingerprint is over the old-spelled tree
                tree["f"]["fingerprint"] = pm.order_fp(tree["f"]["orders"]["t"][0])
        conn.execute(
            "UPDATE events SET payload_json = ? WHERE seq = ?", (json.dumps(tree, separators=(",", ":")), row["seq"])
        )
    conn.commit()
    conn.close()
    return path, intents


@pytest.fixture
def old(tmp_path):
    path, intents = make_old_ledger(tmp_path)
    return path, intents, tmp_path


def rows(path: Path, table: str, order: str):
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        return [tuple(r) for r in conn.execute(f"SELECT * FROM {table} ORDER BY {order}")]
    finally:
        conn.close()


# --- forward ---------------------------------------------------------------------------------


def test_forward_respells_every_decimal_and_keeps_the_fold_by_value(old) -> None:
    src, _intents, tmp = old
    before = file_sha(src)
    assert any(".00" in r[5] for r in rows(src, "events", "seq"))  # the fixture is non-canonical
    report = pm.migrate(src, tmp / "m.db")
    assert file_sha(src) == before  # SRC is the backup: untouched
    assert report["value_equal"] and report["events"] == len(rows(src, "events", "seq"))
    assert report["map_opaque"] == 2 and report["map_order"] >= 1  # two intents, at least the one close
    assert report["map_total"] == report["map_opaque"] + report["map_order"]
    for row in rows(tmp / "m.db", "events", "seq"):
        assert norm(row[5].encode()) == row[5].encode()  # every decimal is canonical now
    assert pm._fold_canon(src) == pm._fold_canon(tmp / "m.db")  # by value, per account


def test_the_migrated_file_uses_the_truncate_journal(old) -> None:
    src, _i, tmp = old
    # the mode is per connection, so the proof is the tool's own: a TRUNCATE journal is
    # emptied and kept, where the default would delete it (the owner's policy denies deletes)
    conn = pm._new(tmp / "probe.db")
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "truncate"
    finally:
        conn.close()
    pm.migrate(src, tmp / "m.db")
    journal = tmp / "m.db-journal"
    assert journal.exists() and journal.stat().st_size == 0


def test_the_key_map_ties_the_old_fingerprint_to_the_new_one(old) -> None:
    src, intents, tmp = old
    pm.migrate(src, tmp / "m.db")
    old_fps = {OptionOrderManager._intent_fingerprint(i, legacy=True) for i in intents}
    mapped = {r[0]: r[1] for r in rows(tmp / "m.db", "p7_key_map", "event_seq")}
    for fp in old_fps:
        assert mapped[fp] == hashlib.sha256(("p7:1:" + fp).encode()).hexdigest()


def test_an_out_of_bound_decimal_aborts_and_writes_nothing(old) -> None:
    src, _i, tmp = old
    conn = sqlite3.connect(src)
    conn.execute(
        "UPDATE events SET payload_json = replace(payload_json, ?, ?) WHERE kind = 'CashFlow'",
        ('"d":"100000.00"', '"d":"' + "9" * 40 + '"'),
    )
    conn.commit()
    conn.close()
    with pytest.raises(pm.MigrationError, match="canonically"):
        pm.migrate(src, tmp / "m.db")
    assert not (tmp / "m.db").exists()


# --- reverse ---------------------------------------------------------------------------------


def test_reverse_restores_the_source_rows_byte_for_byte(old) -> None:
    src, _i, tmp = old
    pm.migrate(src, tmp / "m.db")
    report = pm.reverse(tmp / "m.db", tmp / "b.db")
    assert report["equal"] and report["restored_sha256"] == report["recorded_sha256"]
    for table, order in (("events", "seq"), ("meta", "key"), ("outbox", "id")):
        assert rows(tmp / "b.db", table, order) == rows(src, table, order)
    # the digest is not just self-reported: recompute it from the two files here
    back, original = sqlite3.connect(tmp / "b.db"), sqlite3.connect(src)
    try:
        assert pm.rows_digest(back)[0] == pm.rows_digest(original)[0]
    finally:
        back.close()
        original.close()


def test_reverse_refuses_an_incomplete_migration(old) -> None:
    src, _i, tmp = old
    pm.migrate(src, tmp / "m.db")
    conn = sqlite3.connect(tmp / "m.db")
    conn.execute("UPDATE p7_meta SET value = 'incomplete' WHERE key = 'state'")
    conn.commit()
    conn.close()
    with pytest.raises(pm.MigrationError, match="not a complete"):
        pm.reverse(tmp / "m.db", tmp / "b.db")
    assert not (tmp / "b.db").exists()


# --- never in place; never the data directory -----------------------------------------------------


def test_the_destination_must_not_exist(old) -> None:
    src, _i, tmp = old
    (tmp / "m.db").write_bytes(b"precious")
    with pytest.raises(pm.MigrationError, match="exists"):
        pm.migrate(src, tmp / "m.db")
    assert (tmp / "m.db").read_bytes() == b"precious"


def test_the_source_is_never_its_own_destination(old) -> None:
    src, _i, _tmp = old
    before = file_sha(src)
    with pytest.raises(pm.MigrationError):
        pm.migrate(src, src)
    assert file_sha(src) == before


def test_reverse_never_writes_over_an_existing_file(old) -> None:
    src, _i, tmp = old
    pm.migrate(src, tmp / "m.db")
    before = file_sha(src)
    with pytest.raises(pm.MigrationError, match="exists"):
        pm.reverse(tmp / "m.db", src)
    assert file_sha(src) == before


def test_the_data_directory_is_refused_without_the_owners_flag(tmp_path) -> None:
    protected = pm.PROTECTED / "x-ledger.db"
    with pytest.raises(pm.MigrationError, match="refused"):
        pm.migrate(protected, tmp_path / "m.db")
    with pytest.raises(pm.MigrationError, match="refused"):
        pm.migrate(tmp_path / "s.db", protected)
    with pytest.raises(pm.MigrationError, match="refused"):
        pm.reverse(tmp_path / "m.db", protected)
    assert not (tmp_path / "m.db").exists() and not protected.exists()


def test_the_command_line_reports_a_refusal_as_exit_2(old, capsys) -> None:
    src, _i, tmp = old
    assert pm.main([str(src), str(src)]) == 2
    assert "p7_migrate: refused" in capsys.readouterr().err
    assert pm.main([str(src), str(tmp / "m.db")]) == 0
    assert pm.main(["--reverse", str(tmp / "m.db"), str(tmp / "b.db")]) == 0


# --- replay dedupe: the old key still finds its command -----------------------------------------------


class Replayer:
    """An OptionOrderManager over an existing ledger file (no deposit appended)."""

    def __init__(self, path: Path) -> None:
        self.clock = ReplayClock(datetime(2026, 9, 29, 21, 45, tzinfo=UTC))
        self.ledger = Ledger(path).open()
        venue = SnapshotVenue(ACCOUNT, self.clock)
        venue.connect()
        self.oms = OptionOrderManager(venue, self.clock, self.ledger)

    def count(self) -> int:
        return len(rows(self.ledger.path, "events", "seq"))

    def close(self) -> None:
        self.ledger.close()


def named(intents, name):
    return next(i for i in intents if i.command_id == name)


def test_an_old_ledger_dedupes_a_replay_and_conflicts_on_other_terms(old) -> None:
    src, intents, tmp = old
    work = tmp / "old-copy.db"
    work.write_bytes(src.read_bytes())
    r = Replayer(work)
    try:
        n = r.count()
        csp = named(intents, "csp")
        assert r.oms.open(csp).order_id == "csp:entry"
        assert r.count() == n  # a replay changes nothing (I3)
        with pytest.raises(IdempotencyConflictError):
            r.oms.open(replace(csp, quantity=D("3")))
    finally:
        r.close()


def test_a_migrated_ledger_dedupes_the_old_key_through_the_map(old) -> None:
    src, intents, tmp = old
    pm.migrate(src, tmp / "m.db")
    r = Replayer(tmp / "m.db")
    try:
        n = r.count()
        for name in ("csp", "bps"):
            assert r.oms.open(named(intents, name)).order_id == f"{name}:entry"
        assert r.count() == n  # both old commands found, nothing re-placed
        csp = named(intents, "csp")
        for field, value in (("quantity", D("3")), ("limit_price", D("10.50")), ("profit_target", D("4"))):
            with pytest.raises(IdempotencyConflictError):
                r.oms.open(replace(csp, **{field: value}))
        assert r.count() == n
    finally:
        r.close()


def test_the_new_spelling_dedupes_in_a_ledger_written_after_the_migration(tmp_path) -> None:
    desk = Desk(tmp_path)
    try:
        intent = desk.intent(P270, Side.SELL, "2", limit="10.00", target="5.00", name="new")
        desk.oms.open(intent)
        n = len(rows(desk.ledger.path, "events", "seq"))
        assert desk.oms.open(intent).order_id == "new:entry"
        assert len(rows(desk.ledger.path, "events", "seq")) == n
        assert desk.ledger.fingerprint_alias("anything") is None  # an unmigrated ledger has no map
    finally:
        desk.ledger.close()

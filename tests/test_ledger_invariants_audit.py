"""The invariant audit tool (`tools/ledger_invariants_audit.py`): for every invariant one
clean ledger passes and one seeded defect fails it. The clean ledgers are written through
the production store (`Ledger.extend`, which refuses an event the fold refuses); a defect
that must survive the fold is seeded by editing the closed ledger's rows with raw sqlite,
exactly the tampering an audit of a store-written ledger is looking for. No codec of our
own: events come from the `ledger_gen` zoo helpers."""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for entry in (ROOT / "src", ROOT / "tests", ROOT / "tools"):
    sys.path.insert(0, str(entry))

from ledger_gen import T0, AAPL, _ev, _fill, _order  # noqa: E402

import ledger_invariants_audit as audit  # noqa: E402
from trade_engine.domain.orders import Side  # noqa: E402
from trade_engine.ledger import Ledger  # noqa: E402
from trade_engine.ledger.events import CashFlow, EventKind, Mark, OrderStateChange  # noqa: E402

K = EventKind
DEPOSIT = Decimal("10000")


# --- builders ---------------------------------------------------------------------------

def _write_ledger(path: Path, events: list) -> None:
    with Ledger(path) as ledger:
        ledger.extend(events)


def _sql(path: Path, statement: str, args: tuple = ()) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(statement, args)
        conn.commit()
    finally:
        conn.close()


def _patch_payload(path: Path, where: str, patch_fn) -> None:
    """Rewrite one stored row's payload_json through `patch_fn` (raw sqlite, store closed)."""
    conn = sqlite3.connect(str(path))
    try:
        (text,) = conn.execute(f"SELECT payload_json FROM events WHERE {where}").fetchone()
        node = json.loads(text)
        patch_fn(node)
        conn.execute(f"UPDATE events SET payload_json = ? WHERE {where}", (json.dumps(node),))
        conn.commit()
    finally:
        conn.close()


def _verdicts(path: Path) -> dict[str, bool]:
    lines, _status = audit.audit_file(path)
    checks = {}
    for line in lines:
        parts = line.split(maxsplit=2)
        if len(parts) >= 3 and parts[0] in ("OK", "FAIL") and parts[1].startswith("A"):
            checks[parts[1]] = parts[0] == "OK"
    return checks


def _clean_events() -> list:
    """Cash flows, two buys in acquisition order, a partial sell, a mark — folded by the
    store, so it ties out from the start; two accounts so per-account checks are per-account."""
    return [
        _ev(K.CASH_FLOW, CashFlow(DEPOSIT, "deposit", T0)),
        _ev(K.CASH_FLOW, CashFlow(DEPOSIT, "deposit", T0), account="ACC2"),
        _ev(K.ORDER_SUBMITTED, _order("o1"), command_id="cmd-o1", ts=T0),
        _ev(K.FILL, _fill("f1", "o1", quantity=Decimal("60"), price=Decimal("50"), filled_at=T0), ts=T0),
        _ev(K.ORDER_SUBMITTED, _order("o2", side=Side.SELL), command_id="cmd-o2", ts=T0 + timedelta(seconds=2)),
        _ev(K.FILL, _fill("f2", "o1", quantity=Decimal("40"), price=Decimal("55"),
                          filled_at=T0 + timedelta(seconds=1)), ts=T0 + timedelta(seconds=1)),
        _ev(K.FILL, _fill("f3", "o2", quantity=Decimal("30"), price=Decimal("60"), side=Side.SELL,
                          fee=Decimal("1"), filled_at=T0 + timedelta(seconds=3)), ts=T0 + timedelta(seconds=3)),
        _ev(K.MARK, Mark(AAPL, Decimal("101"), T0)),
        _ev(K.ORDER_SUBMITTED, _order("o3", quantity=Decimal("10")), command_id="cmd-o3",
            ts=T0 + timedelta(seconds=4)),  # submitted and never filled: replayable as Submitted
    ]


def _edit_fill_to_unknown_order(path: Path) -> None:
    _patch_payload(path, "kind = 'Fill' AND payload_json LIKE '%f1%'",
                   lambda node: node["f"].__setitem__("order_id", "nope"))


def _lifecycle_events() -> list:
    """An order partially filled, then cancelled: the folded state is terminal."""
    return [
        _ev(K.ORDER_SUBMITTED, _order("o1", quantity=Decimal("100")), command_id="cmd-o1", ts=T0),
        _ev(K.FILL, _fill("f1", "o1", quantity=Decimal("50"), price=Decimal("10"), filled_at=T0), ts=T0),
        _ev(K.ORDER_CANCELLED, OrderStateChange("o1", reason="user"), command_id="cmd-x",
            ts=T0 + timedelta(seconds=1)),
    ]


# --- A1 ---------------------------------------------------------------------------------

def test_a1_clean_ledger_passes(tmp_path):
    path = tmp_path / "a1-clean.db"
    _write_ledger(path, _clean_events())
    assert _verdicts(path)["A1"]


def test_a1_deleted_row_leaves_a_gap(tmp_path):
    path = tmp_path / "a1-gap.db"
    _write_ledger(path, _clean_events())
    _sql(path, "DELETE FROM events WHERE seq = 4")  # a middle row: the tail-end row leaves no gap
    verdicts = _verdicts(path)
    assert not verdicts["A1"]
    assert verdicts["A2"]  # the fold still replays the gapped log; A1 caught the gap alone


# --- A2 ---------------------------------------------------------------------------------

def test_a2_clean_ledger_passes(tmp_path):
    path = tmp_path / "a2-clean.db"
    _write_ledger(path, _clean_events())
    assert _verdicts(path)["A2"]


def test_a2_refuses_an_undecodable_row_and_a_fold_refusal(tmp_path):
    path = tmp_path / "a2-broken.db"
    _write_ledger(path, _clean_events())
    _sql(path, "UPDATE events SET payload_json = '{ not json' WHERE kind = 'Mark'")
    _edit_fill_to_unknown_order(path)
    assert not _verdicts(path)["A2"]


# --- A3 ---------------------------------------------------------------------------------

def test_a3_clean_ledger_passes(tmp_path):
    path = tmp_path / "a3-clean.db"
    _write_ledger(path, _clean_events())
    assert _verdicts(path)["A3"]


def test_a3_duplicated_command_id(tmp_path):
    path = tmp_path / "a3-dup.db"
    _write_ledger(path, _clean_events())
    # the intact schema already enforces A3 (`ux_events_command_id`); the seed drops it,
    # as tampering or an older schema would, then stores the replayed command twice
    _sql(path, "DROP INDEX ux_events_command_id")
    _sql(path, "INSERT INTO events (seq, ts_utc, account, kind, command_id, payload_json, schema_version) "
               "SELECT (SELECT MAX(seq) + 1 FROM events), ts_utc, account, kind, command_id, payload_json, "
               "schema_version FROM events WHERE kind = 'OrderSubmitted' AND payload_json LIKE '%o3%' LIMIT 1")
    verdicts = _verdicts(path)
    assert not verdicts["A3"]
    assert verdicts["A2"]  # the replayed command is a no-op to the fold; A3 caught it alone


# --- A4/A5 ------------------------------------------------------------------------------

def test_a4_and_a5_clean_ledger_pass(tmp_path):
    path = tmp_path / "clean.db"
    _write_ledger(path, _clean_events())
    verdicts = _verdicts(path)
    assert verdicts["A4"] and verdicts["A5"]


def test_a4_cannot_certify_when_the_fold_refuses(tmp_path):
    path = tmp_path / "a4-refused.db"
    _write_ledger(path, _clean_events())
    _edit_fill_to_unknown_order(path)
    assert not _verdicts(path)["A4"]


def test_a5_cannot_certify_when_the_fold_refuses(tmp_path):
    path = tmp_path / "a5-refused.db"
    _write_ledger(path, _clean_events())
    _edit_fill_to_unknown_order(path)
    assert not _verdicts(path)["A5"]


# --- A6 ---------------------------------------------------------------------------------

def test_a6_clean_ledger_passes(tmp_path):
    path = tmp_path / "a6-clean.db"
    _write_ledger(path, _clean_events())
    assert _verdicts(path)["A6"]


def test_a6_fill_backdated_before_an_earlier_lot(tmp_path):
    path = tmp_path / "a6-fifo.db"
    _write_ledger(path, _clean_events())
    _patch_payload(path, "payload_json LIKE '%f2%'",
                   lambda node: node["f"].__setitem__("filled_at", {"T": (T0 - timedelta(seconds=1)).isoformat()}))
    verdicts = _verdicts(path)
    assert not verdicts["A6"]
    assert verdicts["A5"] and verdicts["A2"]  # the fold replays it fine; A6 caught the order alone


# --- A7/A8 ------------------------------------------------------------------------------

def test_a7_and_a8_clean_ledger_pass(tmp_path):
    path = tmp_path / "clean.db"
    _write_ledger(path, _lifecycle_events())
    verdicts = _verdicts(path)
    assert verdicts["A7"] and verdicts["A8"]


def test_a7_cannot_certify_when_a_fill_lands_after_the_cancel(tmp_path):
    path = tmp_path / "a7-late-fill.db"
    _write_ledger(path, _lifecycle_events())
    _patch_payload(path, "kind = 'Fill'", lambda node: node["f"].__setitem__("fill_id", "f2"))
    _sql(path, "INSERT INTO events (seq, ts_utc, account, kind, command_id, payload_json, schema_version) "
               "SELECT (SELECT MAX(seq) + 1 FROM events), ts_utc, account, kind, command_id, payload_json, "
               "schema_version FROM events WHERE kind = 'Fill'")
    assert not _verdicts(path)["A7"]


def test_a8_fill_row_resequenced_after_the_terminal_row(tmp_path):
    path = tmp_path / "a8-late-fill.db"
    _write_ledger(path, _lifecycle_events())
    _sql(path, "UPDATE events SET seq = (SELECT MAX(seq) + 1 FROM events) WHERE kind = 'Fill'")
    assert not _verdicts(path)["A8"]
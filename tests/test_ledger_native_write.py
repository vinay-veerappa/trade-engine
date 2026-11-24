"""The native ledger write path (W6-C0ab): byte parity with the binding path, refusals,
rollback, and the post-commit notifier.

The Python path (`Ledger._write`) and the native path (`LedgerStore.write_encoded`) must
leave the same rows behind: the same events bytes, the same outbox payload text and
timestamps, and the same account snapshot.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from trade_engine.domain.instruments import Equity, Side
from trade_engine.domain.orders import Order, OrderType
from trade_engine.ledger import codec
from trade_engine.ledger.events import Event, EventKind
from trade_engine.ledger.store import Ledger

T0 = datetime(2026, 9, 23, 15, 0, 0, tzinfo=timezone.utc)
T1 = datetime(2026, 9, 23, 15, 1, 0, tzinfo=timezone.utc)


def _order_event(command_id: str, ts: datetime = T0) -> Event:
    order = Order(
        order_id=f"ord-{command_id}",
        account_id="ACC_A",
        instrument=Equity("AAPL"),
        order_type=OrderType.LIMIT,
        side=Side.BUY,
        quantity=Decimal("10"),
        command_id=f"cmd-ord-{command_id}",
        created_at=ts,
        limit_price=Decimal("150.00"),
    )
    return Event(
        account="ACC_A",
        kind=EventKind.ORDER_SUBMITTED,
        payload=order,
        ts_utc=ts,
        command_id=command_id,
    )


def _text(payload: dict[str, Any]) -> str:
    """The outbox payload text the binding path stores: sorted keys, compact, ASCII."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _native(ledger: Ledger, entries: list[tuple[Event, list[tuple[str, dict[str, Any]]]]]) -> int:
    return ledger._native.write_encoded(
        [
            (codec.event_bytes(event), [(dest, _text(payload)) for dest, payload in items])
            for event, items in entries
        ]
    )


def _rows(ledger: Ledger) -> tuple[list[tuple], list[tuple]]:
    events = [tuple(row) for row in ledger._conn.execute("SELECT * FROM events ORDER BY seq")]
    outbox = [tuple(row) for row in ledger._conn.execute("SELECT * FROM outbox ORDER BY id")]
    return events, outbox


def _rolled_back_batch() -> list[tuple[bytes, list[tuple[str, str]]]]:
    # The second event repeats one destination, so its second outbox row violates
    # UNIQUE(event_seq, destination) after the first event has already been inserted.
    return [
        (codec.event_bytes(_order_event("c1")), []),
        (codec.event_bytes(_order_event("c2")), [("journal", "{}"), ("journal", "{}")]),
    ]


@pytest.fixture
def py_ledger(tmp_path: Path):
    with Ledger(tmp_path / "py.db") as lg:
        yield lg


@pytest.fixture
def native_ledger(tmp_path: Path):
    with Ledger(tmp_path / "native.db") as lg:
        yield lg


def test_native_write_matches_python_write_byte_for_byte(py_ledger: Ledger, native_ledger: Ledger) -> None:
    batch = [
        (_order_event("c1"), [("journal", {"symbol": "AAPL", "qty": 10}), ("audit", {"note": "é"})]),
        (_order_event("c2", T1), []),
        # Sub-second timestamp: the ts text must carry the micros the way isoformat does.
        (_order_event("c3", T1.replace(microsecond=123456)), [("journal", {"n": 3})]),
    ]
    py_ledger._write(batch)
    assert _native(native_ledger, batch) == 3

    py_events, py_outbox = _rows(py_ledger)
    native_events, native_outbox = _rows(native_ledger)
    assert len(py_events) == 3
    assert len(py_outbox) == 3
    assert native_events == py_events
    assert native_outbox == py_outbox
    assert py_ledger.snapshot("ACC_A") == native_ledger.snapshot("ACC_A")


def test_native_write_refuses_an_event_with_seq_set(native_ledger: Ledger) -> None:
    event = dataclasses.replace(_order_event("c1"), seq=7)
    with pytest.raises(ValueError, match="Event.seq is assigned by the ledger"):
        _native(native_ledger, [(event, [])])
    assert _rows(native_ledger) == ([], [])


def test_native_write_refuses_a_blank_destination(native_ledger: Ledger) -> None:
    with pytest.raises(ValueError, match="Outbox destination must be non-empty string"):
        _native(native_ledger, [(_order_event("c1"), [("   ", {})])])
    assert _rows(native_ledger) == ([], [])


def test_native_write_refuses_a_non_dict_payload(native_ledger: Ledger) -> None:
    with pytest.raises(ValueError, match="Outbox payload for 'journal' must be a dict"):
        native_ledger._native.write_encoded(
            [(codec.event_bytes(_order_event("c1")), [("journal", "[1]")])]
        )
    assert _rows(native_ledger) == ([], [])


def test_replayed_command_appends_nothing(native_ledger: Ledger) -> None:
    assert _native(native_ledger, [(_order_event("c1"), [("journal", {"a": 1})])]) == 1
    before = _rows(native_ledger)
    assert _native(native_ledger, [(_order_event("c1"), [("journal", {"a": 2})])]) == 0
    assert _rows(native_ledger) == before


def test_mid_batch_failure_rolls_back_the_whole_batch(native_ledger: Ledger) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        native_ledger._native.write_encoded(_rolled_back_batch())
    assert _rows(native_ledger) == ([], [])
    assert native_ledger.count() == 0

    # The rolled-back seq is handed out again: the transaction left no trace.
    assert _native(native_ledger, [(_order_event("c3"), [])]) == 1
    events, _ = _rows(native_ledger)
    assert [row[0] for row in events] == [1]


def test_notifier_fires_once_with_every_new_event(native_ledger: Ledger) -> None:
    calls: list[list[Event]] = []
    native_ledger._native.set_notifier(lambda events: calls.append(list(events)))

    assert _native(native_ledger, [(_order_event("c1"), []), (_order_event("c2", T1), [])]) == 2

    assert len(calls) == 1
    assert [event.seq for event in calls[0]] == [1, 2]
    assert [event.command_id for event in calls[0]] == ["c1", "c2"]


def test_replay_and_rollback_do_not_notify(native_ledger: Ledger) -> None:
    calls: list[list[Event]] = []
    native_ledger._native.set_notifier(lambda events: calls.append(list(events)))
    _native(native_ledger, [(_order_event("c1"), [])])
    assert len(calls) == 1

    _native(native_ledger, [(_order_event("c1"), [])])  # replay: no new events
    with pytest.raises(sqlite3.IntegrityError):
        native_ledger._native.write_encoded(_rolled_back_batch())
    assert len(calls) == 1


def test_notifier_error_is_logged_and_the_commit_stands(
    native_ledger: Ledger, caplog: pytest.LogCaptureFixture
) -> None:
    def broken(events: list[Event]) -> None:
        raise RuntimeError("listener broke")

    native_ledger._native.set_notifier(broken)
    with caplog.at_level(logging.ERROR, logger="trade_engine.ledger"):
        assert _native(native_ledger, [(_order_event("c1"), [])]) == 1

    assert native_ledger.count() == 1
    records = [r for r in caplog.records if r.name == "trade_engine.ledger"]
    assert len(records) == 1
    assert records[0].levelno == logging.ERROR
    assert records[0].exc_info is not None
    assert isinstance(records[0].exc_info[1], RuntimeError)


def test_raising_listener_does_not_fail_a_native_write(native_ledger: Ledger) -> None:
    seen: list[Event] = []

    def broken(event: Event) -> None:
        raise RuntimeError("listener broke")

    native_ledger.add_listener(broken)
    native_ledger.add_listener(seen.append)
    assert _native(native_ledger, [(_order_event("c1"), [])]) == 1
    assert [event.command_id for event in seen] == ["c1"]


def test_open_wires_listeners_to_the_native_notifier(tmp_path: Path) -> None:
    seen: list[Event] = []
    with Ledger(tmp_path / "wired.db") as lg:
        lg.add_listener(seen.append)
        assert _native(lg, [(_order_event("c1"), [])]) == 1
    assert [event.command_id for event in seen] == ["c1"]


def test_binding_write_still_notifies_each_listener_once(py_ledger: Ledger) -> None:
    seen: list[Event] = []
    py_ledger.add_listener(seen.append)
    py_ledger._write([(_order_event("c1"), [])])
    assert [event.command_id for event in seen] == ["c1"]


def test_native_rewrite_of_the_eod_golden_ledgers_is_byte_identical(tmp_path: Path, monkeypatch, capsys) -> None:
    """Every event the EOD golden scenarios write (fills, orders, marks, run markers, signals,
    verdicts, voids, write-offs) re-written natively, in seq order, leaves identical rows."""
    from tests.test_eod_cli_golden import LEDGER, _transcript

    source_root = tmp_path / "golden"
    source_root.mkdir()
    _transcript(source_root, monkeypatch, capsys)
    sources = sorted(source_root.glob(f"*/{LEDGER}"))
    assert sources, "the golden scenarios wrote no ledgers"
    kinds: set[str] = set()
    for index, source in enumerate(sources):
        with Ledger(source) as src:
            events = src.events()
            accounts = src.accounts()
            expected = [tuple(row) for row in src._conn.execute("SELECT * FROM events ORDER BY seq")]
            snapshots = {account: src.snapshot(account) for account in accounts}
        kinds.update(event.kind.value for event in events)
        with Ledger(tmp_path / f"native-{index}.db") as dst:
            for event in events:
                written = dst._native.write_encoded(
                    [(codec.event_bytes(dataclasses.replace(event, seq=None)), [])]
                )
                assert written == 1, event
            assert [tuple(row) for row in dst._conn.execute("SELECT * FROM events ORDER BY seq")] == expected
            assert dst.accounts() == accounts
            assert {account: dst.snapshot(account) for account in accounts} == snapshots
    assert {"Fill", "Mark", "EodRun", "EodVoid", "OrdersCreated", "OrderCancelled"} <= kinds, sorted(kinds)

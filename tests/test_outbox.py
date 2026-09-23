"""Tests for SQLite ledger outbox and drain-in-order semantics (WP E5, Architecture §4.11, I12)."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from trade_engine.clock.replay import ReplayClock
from trade_engine.domain.instruments import Equity, Side
from trade_engine.domain.orders import Order, OrderType
from trade_engine.ledger.events import Event, EventKind
from trade_engine.ledger.outbox import OutboxItem, OutboxStatus
from trade_engine.ledger.store import Ledger

T0 = datetime(2026, 9, 23, 15, 0, 0, tzinfo=timezone.utc)
T1 = datetime(2026, 9, 23, 15, 1, 0, tzinfo=timezone.utc)
T2 = datetime(2026, 9, 23, 15, 2, 0, tzinfo=timezone.utc)


@pytest.fixture
def ledger(tmp_path: Path) -> Ledger:
    db_file = tmp_path / "test_outbox.db"
    with Ledger(db_file) as lg:
        yield lg


def _seed_event(ledger: Ledger, command_id: str) -> int:
    order = Order(
        order_id=f"ord-{command_id}",
        account_id="ACC_A",
        instrument=Equity("AAPL"),
        order_type=OrderType.LIMIT,
        side=Side.BUY,
        quantity=Decimal("10"),
        command_id=f"cmd-ord-{command_id}",
        created_at=T0,
        limit_price=Decimal("150.00"),
    )
    ev = ledger.append(
        Event(
            account="ACC_A",
            kind=EventKind.ORDER_SUBMITTED,
            payload=order,
            ts_utc=T0,
            command_id=command_id,
        )
    )
    assert ev.seq is not None
    return ev.seq


def test_enqueue_and_pending_order(ledger: Ledger) -> None:
    seq1 = _seed_event(ledger, "c1")
    seq2 = _seed_event(ledger, "c2")

    item1 = ledger.enqueue_outbox(seq1, "journal", {"symbol": "AAPL", "qty": 10}, created_at=T0)
    item2 = ledger.enqueue_outbox(seq2, "journal", {"symbol": "MSFT", "qty": 5}, created_at=T1)

    assert item1.id < item2.id
    pending = ledger.pending_outbox("journal")
    assert [it.id for it in pending] == [item1.id, item2.id]
    assert pending[0].payload["symbol"] == "AAPL"
    assert pending[1].payload["symbol"] == "MSFT"


def test_drain_all_success(ledger: Ledger) -> None:
    clock = ReplayClock(T2)
    seq1 = _seed_event(ledger, "c1")
    seq2 = _seed_event(ledger, "c2")

    ledger.enqueue_outbox(seq1, "journal", {"id": 1}, created_at=T0)
    ledger.enqueue_outbox(seq2, "journal", {"id": 2}, created_at=T1)

    delivered_items: list[int] = []

    def mock_sink(item: OutboxItem) -> bool:
        delivered_items.append(item.id)
        return True

    res = ledger.drain_outbox("journal", mock_sink, clock)
    assert res.ok
    assert res.drained_count == 2
    assert res.remaining_count == 0
    assert len(delivered_items) == 2

    # Verify rows in database are marked DELIVERED
    pending = ledger.pending_outbox("journal", include_failed=False)
    assert len(pending) == 0


def test_failing_sink_stops_at_first_failure_and_leaves_later_queued_in_order(
    ledger: Ledger,
) -> None:
    """Acceptance: A failing sink leaves later events queued and in order (I12)."""
    clock = ReplayClock(T2)
    seq1 = _seed_event(ledger, "c1")
    seq2 = _seed_event(ledger, "c2")
    seq3 = _seed_event(ledger, "c3")

    item1 = ledger.enqueue_outbox(seq1, "journal", {"id": 1}, created_at=T0)
    item2 = ledger.enqueue_outbox(seq2, "journal", {"id": 2}, created_at=T1)
    item3 = ledger.enqueue_outbox(seq3, "journal", {"id": 3}, created_at=T2)

    attempted: list[int] = []

    def failing_sink(item: OutboxItem) -> bool:
        attempted.append(item.id)
        if item.id == item2.id:
            return False  # Sink rejects item 2
        return True

    res = ledger.drain_outbox("journal", failing_sink, clock)

    # Drain stopped at item 2
    assert not res.ok
    assert res.drained_count == 1
    assert res.failed_item is not None
    assert res.failed_item.id == item2.id
    assert res.failed_item.status == OutboxStatus.FAILED
    assert res.failed_item.attempts == 1
    assert res.failed_item.last_error == "Delivery unconfirmed by sink journal"
    assert attempted == [item1.id, item2.id]  # item3 was never attempted!

    # Pending outbox contains item2 (failed) followed by item3 (pending) in exact order
    pending = ledger.pending_outbox("journal", include_failed=True)
    assert len(pending) == 2
    assert pending[0].id == item2.id
    assert pending[0].status == OutboxStatus.FAILED
    assert pending[0].attempts == 1
    assert pending[1].id == item3.id
    assert pending[1].status == OutboxStatus.PENDING
    assert pending[1].attempts == 0


def test_retry_after_failure_drains_remaining_in_order(ledger: Ledger) -> None:
    clock = ReplayClock(T2)
    seq1 = _seed_event(ledger, "c1")
    seq2 = _seed_event(ledger, "c2")

    item1 = ledger.enqueue_outbox(seq1, "journal", {"id": 1}, created_at=T0)
    item2 = ledger.enqueue_outbox(seq2, "journal", {"id": 2}, created_at=T1)

    # First attempt: item 1 fails immediately
    res1 = ledger.drain_outbox("journal", lambda item: False, clock)
    assert not res1.ok
    assert res1.drained_count == 0

    # Second attempt (after retry): sink succeeds for all items
    attempted: list[int] = []

    def succeeding_sink(item: OutboxItem) -> bool:
        attempted.append(item.id)
        return True

    res2 = ledger.drain_outbox("journal", succeeding_sink, clock)
    assert res2.ok
    assert res2.drained_count == 2
    assert attempted == [item1.id, item2.id]
    assert ledger.pending_outbox("journal", include_failed=True) == []


def test_sink_raising_exception_stops_drain_cleanly(ledger: Ledger) -> None:
    clock = ReplayClock(T2)
    seq1 = _seed_event(ledger, "c1")
    seq2 = _seed_event(ledger, "c2")

    item1 = ledger.enqueue_outbox(seq1, "journal", {"id": 1}, created_at=T0)
    item2 = ledger.enqueue_outbox(seq2, "journal", {"id": 2}, created_at=T1)

    def raising_sink(item: OutboxItem) -> bool:
        if item.id == item1.id:
            raise ConnectionError("Journal unreachable")
        return True

    res = ledger.drain_outbox("journal", raising_sink, clock)
    assert not res.ok
    assert res.drained_count == 0
    assert res.failed_item is not None and res.failed_item.id == item1.id
    assert "Journal unreachable" in (res.error or "")

    pending = ledger.pending_outbox("journal", include_failed=True)
    assert len(pending) == 2
    assert pending[0].id == item1.id
    assert pending[1].id == item2.id
    assert pending[1].status == OutboxStatus.PENDING


def test_enqueue_validations(ledger: Ledger) -> None:
    seq = _seed_event(ledger, "c1")

    with pytest.raises(ValueError, match="destination must be non-empty"):
        ledger.enqueue_outbox(seq, "", {"data": 1})

    naive_dt = datetime(2026, 9, 23, 12, 0, 0)
    with pytest.raises(ValueError, match="timezone-aware"):
        ledger.enqueue_outbox(seq, "journal", {"data": 1}, created_at=naive_dt)

    with pytest.raises(ValueError, match="Unknown event sequence"):
        ledger.enqueue_outbox(999999, "journal", {"data": 1})


def test_append_with_outbox_atomic(ledger: Ledger) -> None:
    """Must-Fix 4: Appending an event with outbox enqueues atomically in one transaction."""
    order = Order(
        order_id="ord-atomic-1",
        account_id="ACC_A",
        instrument=Equity("AAPL"),
        order_type=OrderType.LIMIT,
        side=Side.BUY,
        quantity=Decimal("10"),
        command_id="cmd-atomic-1",
        created_at=T0,
        limit_price=Decimal("150.00"),
    )
    ev = Event(
        account="ACC_A",
        kind=EventKind.ORDER_SUBMITTED,
        payload=order,
        ts_utc=T0,
        command_id="cmd-atomic-1",
    )

    appended = ledger.append(ev, outbox={"journal": {"symbol": "AAPL", "qty": 10}})
    assert appended.seq is not None

    pending = ledger.pending_outbox("journal")
    assert len(pending) == 1
    assert pending[0].event_seq == appended.seq
    assert pending[0].payload == {"symbol": "AAPL", "qty": 10}


def test_append_with_outbox_rolls_back_atomically_on_crash(ledger: Ledger) -> None:
    """Must-Fix 4: If commit fails, both the event and outbox row roll back together."""
    order = Order(
        order_id="ord-atomic-fail",
        account_id="ACC_A",
        instrument=Equity("AAPL"),
        order_type=OrderType.LIMIT,
        side=Side.BUY,
        quantity=Decimal("10"),
        command_id="cmd-atomic-fail",
        created_at=T0,
        limit_price=Decimal("150.00"),
    )
    ev = Event(
        account="ACC_A",
        kind=EventKind.ORDER_SUBMITTED,
        payload=order,
        ts_utc=T0,
        command_id="cmd-atomic-fail",
    )

    def failing_commit() -> None:
        raise RuntimeError("Simulated crash right before commit")

    ledger._commit = failing_commit  # type: ignore

    with pytest.raises(RuntimeError, match="Simulated crash"):
        ledger.append(ev, outbox={"journal": {"symbol": "AAPL"}})

    # Ledger must have 0 events and 0 outbox entries (atomic rollback)
    assert ledger.count() == 0
    assert ledger.pending_outbox("journal") == []


def test_outbox_unique_constraint_rejects_duplicate_destination(ledger: Ledger) -> None:
    """Must-Fix 4: UNIQUE(event_seq, destination) prevents queuing same event twice for destination."""
    seq = _seed_event(ledger, "c1")

    # First enqueue succeeds
    ledger.enqueue_outbox(seq, "journal", {"data": 1}, created_at=T0)

    # Second enqueue for same (event_seq, destination) must be refused
    with pytest.raises(ValueError, match="violates constraint"):
        ledger.enqueue_outbox(seq, "journal", {"data": 2}, created_at=T0)


def test_outbox_foreign_key_enforced(ledger: Ledger) -> None:
    """Should-Fix: Enforce foreign key on outbox event_seq."""
    with pytest.raises(ValueError, match="Unknown event sequence"):
        ledger.enqueue_outbox(999999, "journal", {"data": 1}, created_at=T0)


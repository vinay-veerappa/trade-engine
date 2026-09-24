"""E8b snapshot tests: session-close grouping, peak margin, heat, day-note line."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from trade_engine.domain.instruments import Equity, Side
from trade_engine.domain.orders import Order, OrderType
from trade_engine.domain.portfolio import Fill
from trade_engine.ledger import CashFlow, Event, EventKind, Mark
from trade_engine.metrics import account_margin  # noqa: F401 - import surface check
from trade_engine.metrics.snapshot import (
    AccountSnapshot,
    SnapshotError,
    daily_snapshots,
    day_note_line,
    max_drawdown,
    peak_margin,
)

D1 = datetime(2026, 9, 22, 21, 0, 0, tzinfo=timezone.utc)
D2 = datetime(2026, 9, 23, 21, 0, 0, tzinfo=timezone.utc)
AAPL = Equity("AAPL")
MSFT = Equity("MSFT")


def an_order(
    order_id: str,
    instrument: object = AAPL,
    side: Side = Side.BUY,
    quantity: str = "100",
    order_type: OrderType = OrderType.MARKET,
    stop_price: str | None = None,
) -> Order:
    return Order(
        order_id=order_id,
        account_id="ACC",
        instrument=instrument,
        order_type=order_type,
        side=side,
        quantity=Decimal(quantity),
        command_id=f"cmd-{order_id}",
        created_at=D1,
        stop_price=Decimal(stop_price) if stop_price else None,
    )


def buy_fill(fill_id: str, order_id: str, qty: str, price: str, at: datetime) -> Fill:
    return Fill(
        fill_id=fill_id, order_id=order_id, account_id="ACC", instrument=AAPL,
        quantity=Decimal(qty), price=Decimal(price), venue_env="sim",
        filled_at=at, side=Side.BUY,
    )


def events_two_days() -> list[Event]:
    """Deposit 20000, buy 100 AAPL @100, mark 110 (day 1) with a protective stop,
    mark 120 (day 2)."""
    return [
        Event(
            account="ACC",
            kind=EventKind.CASH_FLOW,
            payload=CashFlow(amount=Decimal("20000"), kind="deposit", as_of=D1),
            ts_utc=D1,
            seq=1,
        ),
        Event(account="ACC", kind=EventKind.ORDER_SUBMITTED, payload=an_order("o1"), ts_utc=D1, seq=2),
        Event(account="ACC", kind=EventKind.FILL, payload=buy_fill("f1", "o1", "100", "100", D1), ts_utc=D1, seq=3),
        Event(account="ACC", kind=EventKind.ORDER_SUBMITTED, payload=an_order(
            "o2", AAPL, Side.SELL, "100", OrderType.STOP, stop_price="95"
        ), ts_utc=D1, seq=4),
        Event(account="ACC", kind=EventKind.MARK, payload=Mark(instrument=AAPL, price=Decimal("110"), as_of=D1), ts_utc=D1, seq=5),
        Event(account="ACC", kind=EventKind.MARK, payload=Mark(instrument=AAPL, price=Decimal("120"), as_of=D2), ts_utc=D2, seq=6),
    ]


def test_one_snapshot_per_session_close() -> None:
    snaps = daily_snapshots(events_two_days(), "ACC")
    assert [s.session for s in snaps] == [D1.date(), D2.date()]
    assert snaps[0].equity == Decimal("21000")  # cash 10000 + 100x110
    assert snaps[1].equity == Decimal("22000")  # cash 10000 + 100x120


def test_peak_margin_monotone() -> None:
    snaps = daily_snapshots(events_two_days(), "ACC")
    # day 1: 0.25×11000=2750; day 2: 0.25×12000=3000 → peak 3000
    assert snaps[0].margin_used == Decimal("2750")
    assert snaps[1].margin_used == Decimal("3000")
    assert snaps[1].margin_peak == Decimal("3000")
    assert peak_margin(snaps) == Decimal("3000")


def test_heat_from_working_protective_stop() -> None:
    """Long 100 @mark 110, stop 95 → heat (110−95)×100 = 1500; day 2: 2500."""
    snaps = daily_snapshots(events_two_days(), "ACC")
    assert snaps[0].heat == Decimal("1500")
    assert snaps[1].heat == Decimal("2500")
    assert snaps[0].heat_unknown_positions == ()


def test_position_without_stop_is_listed_not_guessed() -> None:
    events = events_two_days()
    # drop the protective stop order: o2 never exists
    events = [e for e in events if not (type(e.payload) is Order and e.payload.order_id == "o2")]
    events = [e for e in events if e.seq != 4]
    snaps = daily_snapshots(events, "ACC")
    assert snaps[0].heat == Decimal("0")
    assert snaps[0].heat_unknown_positions == ("AAPL",)
    assert "AAPL" in snaps[0].__repr__()


def test_drawdown_and_duration() -> None:
    snaps = daily_snapshots(events_two_days(), "ACC")
    assert snaps[0].drawdown_from_peak == Decimal("0")
    assert snaps[1].drawdown_from_peak == Decimal("0")
    assert max_drawdown(snaps) == Decimal("0")


def test_missing_mark_refuses() -> None:
    events = [
        Event(account="ACC", kind=EventKind.ORDER_SUBMITTED, payload=an_order("o1"), ts_utc=D1, seq=1),
        Event(account="ACC", kind=EventKind.FILL, payload=buy_fill("f1", "o1", "100", "100", D1), ts_utc=D1, seq=2),
        Event(account="ACC", kind=EventKind.MARK, payload=Mark(instrument=MSFT, price=Decimal("50"), as_of=D1), ts_utc=D1, seq=3),
    ]
    with pytest.raises(SnapshotError, match="No session-close mark for open position AAPL"):
        daily_snapshots(events, "ACC")


def test_no_marks_no_snapshots() -> None:
    assert daily_snapshots([], "ACC") == []


def test_day_note_line_matches_hand_computed() -> None:
    snaps = daily_snapshots(events_two_days(), "ACC")
    line = day_note_line(snaps[0], "ACC")
    # equity 21000, gross 11000 → 52.4%; heat 1500 → 7.1%
    assert line == (
        "snapshot ACC 2026-09-22: equity 21000, gross 52.4%, "
        "margin used 2750 (peak 2750), heat 7.1%, DD 0"
    )


def test_day_note_lists_stopless_positions() -> None:
    events = [e for e in events_two_days() if e.seq != 4]
    snaps = daily_snapshots(events, "ACC")
    line = day_note_line(snaps[0], "ACC")
    assert "(no stop: AAPL)" in line


def test_snapshot_never_writes_to_ledger() -> None:
    """Pure derivation: same events in, same snapshots out, nothing mutated (I2)."""
    events = events_two_days()
    first = daily_snapshots(events, "ACC")
    second = daily_snapshots(events, "ACC")
    assert first == second
    assert len(events) == 6

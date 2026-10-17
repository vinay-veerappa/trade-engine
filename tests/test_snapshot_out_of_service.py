"""Gate test and native end-to-end behavioural tests for metrics snapshot.

Enforces:
1. Out-of-service gate: an AST scan of src/ and live entry points proves that no
   module other than the retired reference file (snapshot.py) imports snapshot.py.
2. Behavioural tests: exercise snapshot_native end-to-end through the Rust door
   with spec-derived expectations.
3. Bug fix pinning: protective stops must be on the reduce side (opposite side of position).
   A BUY stop for a long position does NOT qualify as a protective stop.
"""

from __future__ import annotations

import ast
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
import pytest

from trade_engine.domain.instruments import Equity, Side
from trade_engine.domain.orders import Order, OrderType
from trade_engine.domain.portfolio import Fill
from trade_engine.ledger import CashFlow, Event, EventKind, Mark, fold_account
from trade_engine.metrics.snapshot_native import (
    AccountSnapshot,
    SnapshotError,
    _protective_stop,
    daily_snapshots,
    day_note_line,
    max_drawdown,
    peak_margin,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = REPO_ROOT / "src"

D1 = datetime(2026, 9, 22, 21, 0, 0, tzinfo=timezone.utc)
D2 = datetime(2026, 9, 23, 21, 0, 0, tzinfo=timezone.utc)
D3 = datetime(2026, 9, 24, 21, 0, 0, tzinfo=timezone.utc)
D4 = datetime(2026, 9, 25, 21, 0, 0, tzinfo=timezone.utc)

AAPL = Equity("AAPL")
MSFT = Equity("MSFT")


def an_order(
    order_id: str,
    instrument: object = AAPL,
    side: Side = Side.BUY,
    quantity: str = "100",
    order_type: OrderType = OrderType.MARKET,
    stop_price: str | None = None,
    created_at: datetime = D1,
) -> Order:
    return Order(
        order_id=order_id,
        account_id="ACC",
        instrument=instrument,
        order_type=order_type,
        side=side,
        quantity=Decimal(quantity),
        command_id=f"cmd-{order_id}",
        created_at=created_at,
        stop_price=Decimal(stop_price) if stop_price else None,
    )


def buy_fill(fill_id: str, order_id: str, qty: str, price: str, at: datetime) -> Fill:
    return Fill(
        fill_id=fill_id,
        order_id=order_id,
        account_id="ACC",
        instrument=AAPL,
        quantity=Decimal(qty),
        price=Decimal(price),
        venue_env="sim",
        filled_at=at,
        side=Side.BUY,
    )


def events_two_days() -> list[Event]:
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
        Event(
            account="ACC",
            kind=EventKind.ORDER_SUBMITTED,
            payload=an_order("o2", AAPL, Side.SELL, "100", OrderType.STOP, stop_price="95"),
            ts_utc=D1,
            seq=4,
        ),
        Event(account="ACC", kind=EventKind.MARK, payload=Mark(instrument=AAPL, price=Decimal("110"), as_of=D1), ts_utc=D1, seq=5),
        Event(account="ACC", kind=EventKind.MARK, payload=Mark(instrument=AAPL, price=Decimal("120"), as_of=D2), ts_utc=D2, seq=6),
    ]


# =========================================================================
# Out-of-service AST Gate
# =========================================================================

def scan_file_for_old_snapshot_import(py_file: Path, text: str | None = None) -> list[str]:
    violations: list[str] = []
    if text is None:
        text = py_file.read_text(encoding="utf-8")
    tree = ast.parse(text, filename=str(py_file))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "trade_engine.metrics.snapshot":
                    violations.append(f"{py_file}:{node.lineno} imports '{alias.name}'")
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod == "trade_engine.metrics.snapshot" or (
                node.level > 0 and mod.endswith("snapshot") and not mod.endswith("snapshot_native")
            ):
                violations.append(f"{py_file}:{node.lineno} imports from '{mod}'")
    return violations


def test_old_snapshot_module_is_out_of_service() -> None:
    """Gate: verify no file in src/ (outside snapshot.py itself) imports the retired snapshot module."""
    violations: list[str] = []
    old_file = (SRC_DIR / "trade_engine" / "metrics" / "snapshot.py").resolve()

    for py_file in SRC_DIR.rglob("*.py"):
        if py_file.resolve() == old_file:
            continue
        violations.extend(scan_file_for_old_snapshot_import(py_file))

    # Also scan tools and launch entry points
    for search_dir in [REPO_ROOT / "tools", REPO_ROOT / "launch"]:
        if search_dir.exists():
            for py_file in search_dir.rglob("*.py"):
                violations.extend(scan_file_for_old_snapshot_import(py_file))

    assert not violations, (
        f"Out-of-service gate violated: retired module snapshot.py is imported:\n"
        + "\n".join(violations)
    )


def test_gate_detects_mutation_on_old_import() -> None:
    """Verify that the AST scanner correctly catches and rejects any import of the old module."""
    mutant_code = "from trade_engine.metrics.snapshot import daily_snapshots\n"
    violations = scan_file_for_old_snapshot_import(Path("mutant_test.py"), mutant_code)
    assert violations, "Gate scanner failed to detect mutant import"
    assert "trade_engine.metrics.snapshot" in violations[0]


# =========================================================================
# Spec-derived End-to-End Behavioural Tests
# =========================================================================

def test_native_daily_snapshots_two_days() -> None:
    snaps = daily_snapshots(events_two_days(), "ACC")
    assert len(snaps) == 2
    assert [s.session for s in snaps] == [D1.date(), D2.date()]
    assert snaps[0].equity == Decimal("21000")
    assert snaps[1].equity == Decimal("22000")
    assert snaps[0].cash == Decimal("10000")
    assert snaps[1].cash == Decimal("10000")
    assert snaps[0].margin_used == Decimal("2750")
    assert snaps[1].margin_used == Decimal("3000")
    assert snaps[0].heat == Decimal("1500")
    assert snaps[1].heat == Decimal("2500")
    assert snaps[0].heat_unknown_positions == ()
    assert snaps[1].heat_unknown_positions == ()


def test_native_peak_margin_monotone() -> None:
    events = events_two_days()
    events.append(
        Event(
            account="ACC",
            kind=EventKind.MARK,
            payload=Mark(instrument=AAPL, price=Decimal("80"), as_of=D3),
            ts_utc=D3,
            seq=7,
        )
    )
    snaps = daily_snapshots(events, "ACC")
    assert snaps[0].margin_used == Decimal("2750")
    assert snaps[1].margin_used == Decimal("3000")
    assert snaps[1].margin_peak == Decimal("3000")
    assert snaps[2].margin_used == Decimal("2000")
    assert snaps[2].margin_peak == Decimal("3000")
    assert peak_margin(snaps) == Decimal("3000")


def test_native_position_without_stop_is_listed_not_guessed() -> None:
    events = events_two_days()
    events = [e for e in events if e.seq != 4]  # Remove sell stop order
    snaps = daily_snapshots(events, "ACC")
    assert snaps[0].heat == Decimal("0")
    assert snaps[0].heat_unknown_positions == ("AAPL",)
    assert "AAPL" in repr(snaps[0])


def test_native_protective_stop_side_enforcement() -> None:
    """Pin the defect fix: a protective stop must be on the reduce side.

    When long AAPL, an order with Side.BUY and OrderType.STOP is a breakout buy entry,
    NOT a protective stop. The old Python code incorrectly treated buy stops as protective stops.
    The native path fixes this: heat remains 0 and AAPL is listed in heat_unknown_positions.
    """
    events = [
        Event(
            account="ACC",
            kind=EventKind.CASH_FLOW,
            payload=CashFlow(amount=Decimal("20000"), kind="deposit", as_of=D1),
            ts_utc=D1,
            seq=1,
        ),
        Event(account="ACC", kind=EventKind.ORDER_SUBMITTED, payload=an_order("o1"), ts_utc=D1, seq=2),
        Event(account="ACC", kind=EventKind.FILL, payload=buy_fill("f1", "o1", "100", "100", D1), ts_utc=D1, seq=3),
        # BUY STOP order: same side as position (long). NOT a protective stop!
        Event(
            account="ACC",
            kind=EventKind.ORDER_SUBMITTED,
            payload=an_order("o_buy_stop", AAPL, Side.BUY, "50", OrderType.STOP, stop_price="95"),
            ts_utc=D1,
            seq=4,
        ),
        Event(account="ACC", kind=EventKind.MARK, payload=Mark(instrument=AAPL, price=Decimal("110"), as_of=D1), ts_utc=D1, seq=5),
    ]

    snaps = daily_snapshots(events, "ACC")
    assert snaps[0].heat == Decimal("0")
    assert snaps[0].heat_unknown_positions == ("AAPL",)

    # Direct helper test for protective_stop
    st = fold_account(events, "ACC")
    assert _protective_stop(st, AAPL) is None

    # Adding a proper SELL stop makes it qualify
    events.append(
        Event(
            account="ACC",
            kind=EventKind.ORDER_SUBMITTED,
            payload=an_order("o_sell_stop", AAPL, Side.SELL, "100", OrderType.STOP, stop_price="90"),
            ts_utc=D1,
            seq=6,
        )
    )
    st_with_stop = fold_account(events, "ACC")
    assert _protective_stop(st_with_stop, AAPL) == Decimal("90")


def test_native_drawdown_and_duration() -> None:
    events = [
        Event(
            account="ACC",
            kind=EventKind.CASH_FLOW,
            payload=CashFlow(amount=Decimal("20000"), kind="deposit", as_of=D1),
            ts_utc=D1,
            seq=1,
        ),
        Event(account="ACC", kind=EventKind.ORDER_SUBMITTED, payload=an_order("o1"), ts_utc=D1, seq=2),
        Event(account="ACC", kind=EventKind.FILL, payload=buy_fill("f1", "o1", "100", "100", D1), ts_utc=D1, seq=3),
        # Day 1: Mark 110 -> equity = 10000 + 11000 = 21000 (peak)
        Event(account="ACC", kind=EventKind.MARK, payload=Mark(instrument=AAPL, price=Decimal("110"), as_of=D1), ts_utc=D1, seq=4),
        # Day 2: Mark 90 -> equity = 10000 + 9000 = 19000 (DD = 2000, duration = 0)
        Event(account="ACC", kind=EventKind.MARK, payload=Mark(instrument=AAPL, price=Decimal("90"), as_of=D2), ts_utc=D2, seq=5),
        # Day 3: Mark 80 -> equity = 10000 + 8000 = 18000 (DD = 3000, duration = 1)
        Event(account="ACC", kind=EventKind.MARK, payload=Mark(instrument=AAPL, price=Decimal("80"), as_of=D3), ts_utc=D3, seq=6),
        # Day 4: Mark 120 -> equity = 10000 + 12000 = 22000 (new peak, DD = 0, duration = 0)
        Event(account="ACC", kind=EventKind.MARK, payload=Mark(instrument=AAPL, price=Decimal("120"), as_of=D4), ts_utc=D4, seq=7),
    ]

    snaps = daily_snapshots(events, "ACC")
    assert len(snaps) == 4
    assert snaps[0].drawdown_from_peak == Decimal("0")
    assert snaps[0].drawdown_duration_sessions == 0

    assert snaps[1].drawdown_from_peak == Decimal("2000")
    assert snaps[1].drawdown_duration_sessions == 0

    assert snaps[2].drawdown_from_peak == Decimal("3000")
    assert snaps[2].drawdown_duration_sessions == 1

    assert snaps[3].drawdown_from_peak == Decimal("0")
    assert snaps[3].drawdown_duration_sessions == 0

    assert max_drawdown(snaps) == Decimal("3000")


def test_native_missing_mark_refuses() -> None:
    events = [
        Event(account="ACC", kind=EventKind.ORDER_SUBMITTED, payload=an_order("o1"), ts_utc=D1, seq=1),
        Event(account="ACC", kind=EventKind.FILL, payload=buy_fill("f1", "o1", "100", "100", D1), ts_utc=D1, seq=2),
        Event(account="ACC", kind=EventKind.MARK, payload=Mark(instrument=MSFT, price=Decimal("50"), as_of=D1), ts_utc=D1, seq=3),
    ]
    with pytest.raises(SnapshotError, match="No session-close mark for open position AAPL"):
        daily_snapshots(events, "ACC")


def test_native_no_marks_no_snapshots() -> None:
    assert daily_snapshots([], "ACC") == []


def test_native_day_note_line() -> None:
    snaps = daily_snapshots(events_two_days(), "ACC")
    line = day_note_line(snaps[0], "ACC")
    assert line == (
        "snapshot ACC 2026-09-22: equity 21000, gross 52.4%, "
        "margin used 2750 (peak 2750), heat 7.1%, DD 0"
    )


def test_native_day_note_lists_stopless_positions() -> None:
    events = [e for e in events_two_days() if e.seq != 4]
    snaps = daily_snapshots(events, "ACC")
    line = day_note_line(snaps[0], "ACC")
    assert "(no stop: AAPL)" in line


def test_native_empty_snapshots_refusal() -> None:
    with pytest.raises(SnapshotError, match="peak_margin over an empty snapshot list refuses"):
        peak_margin([])
    with pytest.raises(SnapshotError, match="max_drawdown over an empty snapshot list refuses"):
        max_drawdown([])


def test_native_snapshot_pure_no_mutation() -> None:
    events = events_two_days()
    first = daily_snapshots(events, "ACC")
    second = daily_snapshots(events, "ACC")
    assert first == second
    assert len(events) == 6

"""F2: a crash after a fill is committed must not leave the bracket unprotected.

The fill is durable (``fill:{id}``) before the protective stop is resized. A rerun sees the
duplicate fill and must re-run the bracket sync with the bracket's latest fill as cause; the
startup sweep does the same for brackets the rerun never reports. The journal row is the
same kind of gap: a crash between the fill and its outbox row must heal on rerun, with no
second row.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from trade_engine.domain.instruments import Side
from trade_engine.domain.orders import OrderState, OrderType
from trade_engine.domain.signals import OrderIntent
from trade_engine.interfaces.broker import VenueFill
from trade_engine.ledger import EventKind, Ledger
from trade_engine.oms.manager import OrderManager, _Host
from trade_engine.oms.reconcile import MIN_TIME, reconcile_after
from trade_engine.sim import SimBroker

from test_intraday_service import rig  # noqa: F401  (fixture)
from test_eod_runner import (  # shared runner fixtures and helpers
    ACCOUNT, CALENDAR, INSTRUMENT, PREV_EOD, SESSION, SESSION_OPEN, FakeMarketData,
    SettableClock, _bar, _seed_breakout,
)
from trade_engine.eod import EodRunner, EodRunnerConfig
from trade_engine.eod import runner as eod_runner_module

JOURNAL = "JRN-1"


def _open_bracket(tmp_path: Path):
    """A bracket whose entry the sim fills at the session open (qty 2), nothing recorded yet."""
    clock = SettableClock(SESSION_OPEN - timedelta(minutes=1))
    ledger = Ledger(tmp_path / "f2-ledger.db")
    ledger.open()
    broker = SimBroker(ACCOUNT, clock, Decimal("0"))
    broker.connect()
    manager = OrderManager(broker, clock, ledger)
    intent = OrderIntent(
        intent_id="intent-f2",
        account_id=ACCOUNT,
        instrument=INSTRUMENT,
        side=Side.BUY,
        quantity_rule="fixed_2",
        entry_price=Decimal("100"),
        stop_loss=Decimal("97"),
        profit_targets=(Decimal("110"),),
        reason="F2 bracket",
        command_id="f2-cmd",
        entry_type=OrderType.LIMIT,
    )
    bracket = manager.create_bracket(intent, Decimal("2"))
    manager.submit(bracket.entry)
    clock.set(SESSION_OPEN)
    broker.process_bar(_bar(SESSION_OPEN))
    return ledger, clock, broker, manager, bracket


def _reconcile(ledger, clock, broker, manager, *, journal_account=None) -> int:
    return reconcile_after(
        ledger, clock, broker, manager, ACCOUNT, MIN_TIME, journal_account=journal_account
    )


def _crash_after_fill_append(patch) -> None:
    """The native append commits the FILL; the process then dies before the bracket sync.

    ``after_append`` runs after the commit with the written event's bytes, so the FILL is
    durable when it raises.
    """

    def crashing(self, data: bytes) -> None:
        if json.loads(data)["kind"] == EventKind.FILL.value:
            raise RuntimeError("crash")

    patch.setattr(_Host, "after_append", crashing)


def _venue_fill(fill_id: str, quantity: str, broker) -> VenueFill:
    entry_fill = broker.fills(MIN_TIME)[0]
    return VenueFill(
        venue_fill_id=fill_id,
        venue_order_id=entry_fill.venue_order_id,
        instrument=INSTRUMENT,
        quantity=Decimal(quantity),
        price=Decimal("100"),
        filled_at=SESSION_OPEN,
        side=Side.BUY,
        fee=Decimal("0"),
    )


def test_crash_after_fill_then_rerun_resubmits_the_stop(tmp_path: Path, monkeypatch) -> None:
    ledger, clock, broker, manager, bracket = _open_bracket(tmp_path)
    fill_id = broker.fills(MIN_TIME)[0].venue_fill_id
    stop_id = bracket.stop.order_id

    with monkeypatch.context() as patch:
        _crash_after_fill_append(patch)
        with pytest.raises(RuntimeError, match="crash"):
            _reconcile(ledger, clock, broker, manager)

    # The fill is durable, but the protective stop was never sent.
    assert ledger.has_command(f"fill:{fill_id}")
    assert manager.get_order(stop_id).state is OrderState.NEW

    # The crash patch is gone: the rerun heals the bracket through the duplicate fill.
    _reconcile(ledger, clock, broker, manager)
    stop = manager.get_order(stop_id)
    assert stop.state in (OrderState.SUBMITTED, OrderState.ACCEPTED)
    assert stop.quantity == Decimal("2")

    # A further rerun is a no-op (I3).
    count = ledger.count()
    _reconcile(ledger, clock, broker, manager)
    assert ledger.count() == count
    ledger.close()


def test_startup_sweep_heals_a_bracket_the_rerun_never_reports(tmp_path: Path, monkeypatch) -> None:
    from trade_engine.oms.reconcile import resync_open_brackets

    ledger, clock, broker, manager, bracket = _open_bracket(tmp_path)
    stop_id = bracket.stop.order_id
    with monkeypatch.context() as patch:
        _crash_after_fill_append(patch)
        with pytest.raises(RuntimeError, match="crash"):
            _reconcile(ledger, clock, broker, manager)
    assert manager.get_order(stop_id).state is OrderState.NEW

    # The venue reports nothing since the crash, so reconcile never revisits the fill.
    monkeypatch.setattr(broker, "fills", lambda since: [])
    assert resync_open_brackets(ledger, manager, ACCOUNT) == 1
    stop = manager.get_order(stop_id)
    assert stop.state in (OrderState.SUBMITTED, OrderState.ACCEPTED)
    assert stop.quantity == Decimal("2")

    count = ledger.count()
    resync_open_brackets(ledger, manager, ACCOUNT)
    assert ledger.count() == count
    ledger.close()


def test_latest_fill_is_the_only_cause_after_a_second_fill_crash(tmp_path: Path, monkeypatch) -> None:
    ledger, clock, broker, manager, bracket = _open_bracket(tmp_path)
    stop_id = bracket.stop.order_id
    fill1 = _venue_fill("f1", "1", broker)
    fill2 = _venue_fill("f2", "1", broker)
    # The venue reports both partial fills; the order itself is not re-read here.
    script = [fill1]
    monkeypatch.setattr(broker, "fills", lambda since: list(script))
    monkeypatch.setattr(broker, "orders", lambda since: [])

    _reconcile(ledger, clock, broker, manager)
    assert manager.get_order(stop_id).quantity == Decimal("1")

    script[:] = [fill1, fill2]
    with monkeypatch.context() as patch:
        _crash_after_fill_append(patch)
        with pytest.raises(RuntimeError, match="crash"):
            _reconcile(ledger, clock, broker, manager)
    assert ledger.has_command("fill:f2")
    assert manager.get_order(stop_id).quantity == Decimal("1")

    causes: list[str] = []
    real_sync = getattr(OrderManager, "synchronize_bracket", None)

    def spy(self, order, cause):
        causes.append(cause)
        return real_sync(self, order, cause)

    monkeypatch.setattr(OrderManager, "synchronize_bracket", spy, raising=False)
    # No refusal: f1's replay must not re-sync with its stale cause.
    _reconcile(ledger, clock, broker, manager)

    stop = manager.get_order(stop_id)
    assert stop.quantity == Decimal("2")
    assert stop.state in (OrderState.SUBMITTED, OrderState.ACCEPTED)
    assert causes == ["f2"]
    ledger.close()


def test_journal_row_heals_once_after_a_crash_before_the_outbox(tmp_path: Path, monkeypatch) -> None:
    ledger, clock, broker, manager, bracket = _open_bracket(tmp_path)
    fill_id = broker.fills(MIN_TIME)[0].venue_fill_id
    destination = f"journal:{JOURNAL}"

    with monkeypatch.context() as patch:
        _crash_after_fill_append(patch)
        with pytest.raises(RuntimeError, match="crash"):
            _reconcile(ledger, clock, broker, manager, journal_account=JOURNAL)
    assert ledger.pending_outbox(destination) == []

    _reconcile(ledger, clock, broker, manager, journal_account=JOURNAL)
    event = ledger.event_by_command(f"fill:{fill_id}")
    rows = ledger.pending_outbox(destination)
    assert len(rows) == 1
    assert rows[0].event_seq == event.seq

    _reconcile(ledger, clock, broker, manager, journal_account=JOURNAL)
    assert len(ledger.pending_outbox(destination)) == 1
    ledger.close()


def test_eod_runner_sweeps_open_brackets_before_its_first_reconcile(tmp_path: Path, monkeypatch) -> None:
    events: list[str] = []
    real_reconcile = EodRunner._reconcile_after_bar

    def spy_sweep(ledger, manager, account_id):
        events.append(f"sweep:{account_id}")
        return 0

    def spy_reconcile(self, *args, **kwargs):
        events.append("reconcile")
        return real_reconcile(self, *args, **kwargs)

    monkeypatch.setattr(eod_runner_module, "resync_open_brackets", spy_sweep, raising=False)
    monkeypatch.setattr(EodRunner, "_reconcile_after_bar", spy_reconcile)

    # The smallest existing runner scenario: a stop entry that never triggers.
    clock = SettableClock(PREV_EOD)
    ledger = Ledger(tmp_path / "eod-f2.db")
    ledger.open()
    broker = SimBroker(ACCOUNT, clock, Decimal("0"))
    broker.connect()
    _seed_breakout(ledger, broker, clock, command_id="f2-brk", entry_type=OrderType.STOP)
    EodRunner(
        ledger, clock, CALENDAR, FakeMarketData({}),
        EodRunnerConfig(job_name="eod", brokers={ACCOUNT: broker}),
    ).run(SESSION)
    ledger.close()

    assert events[:1] == [f"sweep:{ACCOUNT}"]
    assert events.count(f"sweep:{ACCOUNT}") == 1


def test_intraday_service_sweeps_its_account_at_run_start(rig, monkeypatch) -> None:
    from trade_engine.oms import reconcile
    from test_intraday_service import SESSION, at_et, snap

    # The Rust IntradayService looks the sweep up on trade_engine.oms.reconcile at each run (intraday.rs).
    calls: list[str] = []
    monkeypatch.setattr(
        reconcile, "resync_open_brackets",
        lambda ledger, manager, account_id: calls.append(account_id) or 0,
    )
    rig.snapshots = [snap(SESSION, 15, 59)]
    rig.service().run(SESSION, start_at=at_et(SESSION, 15, 58))
    assert calls == [rig.config().account_id]


def test_bracket_resync_cause_on_an_unknown_order_raises_key_error(tmp_path: Path) -> None:
    from trade_engine.oms.reconcile import bracket_resync_cause

    ledger, clock, broker, manager, bracket = _open_bracket(tmp_path)
    with pytest.raises(KeyError):
        bracket_resync_cause(ledger, ACCOUNT, "no-such-order")
    ledger.close()


def test_resync_open_brackets_skips_a_bracket_whose_children_are_all_terminal(tmp_path: Path) -> None:
    from trade_engine.oms.reconcile import TERMINAL, resync_open_brackets

    ledger, clock, broker, manager, bracket = _open_bracket(tmp_path)
    _reconcile(ledger, clock, broker, manager)
    # A bar through the profit target (110) fills the target, which closes the position and
    # cancels the stop: every child is terminal.
    exit_at = SESSION_OPEN + timedelta(minutes=1)
    clock.set(exit_at)
    broker.process_bar(_bar(exit_at, open_="105", high="111", low="104", close="110"))
    _reconcile(ledger, clock, broker, manager)
    children = (bracket.stop, *bracket.targets)
    assert all(manager.get_order(child.order_id).state in TERMINAL for child in children)

    count = ledger.count()
    assert resync_open_brackets(ledger, manager, ACCOUNT) == 0
    assert ledger.count() == count
    ledger.close()

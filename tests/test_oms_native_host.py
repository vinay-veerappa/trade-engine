"""W6-C0c: a plain ``Ledger``'s OMS effects run natively (``oms_flow_native``), and a ledger that
is not a plain ``Ledger`` still runs the Python carriers (``oms_flow``).

The same scenario must leave the same ledger behind on both paths: the event rows, the outbox
rows, and the carriers the flow returns. Listeners see each native write exactly once, and the
refusals and the closed-ledger error look the same on both paths.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from trade_engine.domain.instruments import Side
from trade_engine.domain.orders import OrderState, OrderType, TimeInForce
from trade_engine.domain.signals import OrderIntent
from trade_engine.interfaces.broker import OrderChanges
from trade_engine.ledger import EventKind, Ledger
from trade_engine.oms import BrokerOutcomeUnknownError, IdempotencyConflictError, OrderManager
from trade_engine.oms.reconcile import MIN_TIME, reconcile_after
from trade_engine.sim import SimBroker

from test_eod_runner import ACCOUNT, INSTRUMENT, SESSION_OPEN, SettableClock, _bar
from test_oms import FakeBroker, FakeClock, make_intent

JOURNAL = "JRN-NATIVE"


class _Passthrough:
    """Not a plain ``Ledger``, so ``OrderManager._flow`` takes the Python carriers path."""

    def __init__(self, ledger: Ledger) -> None:
        self._ledger = ledger

    def __getattr__(self, name: str):
        return getattr(self._ledger, name)


def _manager_on(path: Path, broker, *, wrap: bool, clock=None):
    ledger = Ledger(path)
    ledger.open()
    manager = OrderManager(broker, clock or FakeClock(), _Passthrough(ledger) if wrap else ledger)
    return manager, ledger


def _ledger_rows(ledger: Ledger):
    events = ledger.conn.execute(
        "SELECT kind, account, payload_json, ts_utc, command_id, schema_version "
        "FROM events ORDER BY seq"
    ).fetchall()
    outbox = ledger.conn.execute(
        "SELECT event_seq, destination, payload_json, status FROM outbox ORDER BY id"
    ).fetchall()
    return events, outbox


def _fake_scenario(path: Path, *, wrap: bool):
    manager, ledger = _manager_on(path, FakeBroker(), wrap=wrap)
    bracket = manager.create_bracket(make_intent(), Decimal("10"))
    submitted = manager.submit(bracket.entry)
    replaced = manager.replace(
        bracket.entry.order_id, OrderChanges(new_quantity=Decimal("3")), command_id="replace-entry"
    )
    cancelled = manager.cancel(bracket.entry.order_id, command_id="cancel-entry")
    result = (bracket, submitted, replaced, cancelled)
    rows = _ledger_rows(ledger)
    ledger.close()
    return result, rows


def _sim_scenario(path: Path, *, wrap: bool):
    """The F2 bracket: the sim fills the entry at the open, then reconcile journals the fill."""
    clock = SettableClock(SESSION_OPEN - timedelta(minutes=1))
    broker = SimBroker(ACCOUNT, clock, Decimal("0"))
    broker.connect()
    manager, ledger = _manager_on(path, broker, wrap=wrap, clock=clock)
    intent = OrderIntent(
        intent_id="intent-native",
        account_id=ACCOUNT,
        instrument=INSTRUMENT,
        side=Side.BUY,
        quantity_rule="fixed_2",
        entry_price=Decimal("100"),
        stop_loss=Decimal("97"),
        profit_targets=(Decimal("110"),),
        reason="native parity",
        command_id="native-cmd",
        entry_type=OrderType.LIMIT,
    )
    bracket = manager.create_bracket(intent, Decimal("2"))
    manager.submit(bracket.entry)
    clock.set(SESSION_OPEN)
    broker.process_bar(_bar(SESSION_OPEN))
    reconcile_after(ledger, clock, broker, manager, ACCOUNT, MIN_TIME, journal_account=JOURNAL)
    replaced = manager.replace(
        bracket.stop.order_id, OrderChanges(new_stop_price=Decimal("96")), command_id="replace-stop"
    )
    cancelled = manager.cancel(bracket.targets[0].order_id, command_id="cancel-target")
    result = (bracket, manager.get_order(bracket.stop.order_id), replaced, cancelled)
    rows = _ledger_rows(ledger)
    ledger.close()
    return result, rows


def test_fake_broker_lifecycle_is_the_same_on_both_paths(tmp_path: Path) -> None:
    native, native_rows = _fake_scenario(tmp_path / "native.db", wrap=False)
    python, python_rows = _fake_scenario(tmp_path / "python.db", wrap=True)

    assert native == python
    assert native_rows == python_rows
    assert native_rows[0], "the scenario must write events"


def test_sim_fill_and_journal_are_the_same_on_both_paths(tmp_path: Path) -> None:
    native, native_rows = _sim_scenario(tmp_path / "native.db", wrap=False)
    python, python_rows = _sim_scenario(tmp_path / "python.db", wrap=True)

    assert native == python
    assert native_rows == python_rows
    events, outbox = native_rows
    assert any(kind == EventKind.FILL.value for kind, *_ in events)
    assert len(outbox) == 1, "the journal row for the fill must be written exactly once"


def test_listeners_see_each_native_write_exactly_once(tmp_path: Path) -> None:
    manager, ledger = _manager_on(tmp_path / "listen.db", FakeBroker(), wrap=False)
    seen = []
    ledger.add_listener(seen.append)

    bracket = manager.create_bracket(make_intent(), Decimal("10"))
    manager.submit(bracket.entry)
    manager.replace(bracket.entry.order_id, OrderChanges(new_quantity=Decimal("3")), command_id="r1")

    stored = ledger.events()
    assert [event.seq for event in seen] == [event.seq for event in stored]
    assert len({event.seq for event in seen}) == len(seen)

    # A replayed command writes nothing, so it notifies nobody.
    count = len(seen)
    manager.create_bracket(make_intent(), Decimal("10"))
    assert len(seen) == count
    ledger.close()


def test_native_write_notifies_a_ledger_whose_store_has_no_notifier(tmp_path: Path) -> None:
    """An owner-built Ledger's store has no notifier; ``_flow`` sets the ledger's listeners."""
    manager, ledger = _manager_on(tmp_path / "owner.db", FakeBroker(), wrap=False)
    seen = []
    ledger.add_listener(seen.append)
    ledger._store.set_notifier(None)

    manager.create_bracket(make_intent(), Decimal("10"))

    assert [event.seq for event in seen] == [event.seq for event in ledger.events()]
    ledger.close()


def test_conflicting_replay_refuses_with_the_same_type_and_message(tmp_path: Path) -> None:
    refusals = {}
    for wrap in (False, True):
        manager, ledger = _manager_on(tmp_path / f"conflict-{wrap}.db", FakeBroker(), wrap=wrap)
        manager.create_bracket(make_intent(), Decimal("10"))
        before = ledger.count()

        with pytest.raises(IdempotencyConflictError) as info:
            manager.create_bracket(replace(make_intent(), exit_tif=TimeInForce.DAY), Decimal("10"))

        assert ledger.count() == before
        refusals[wrap] = (type(info.value), str(info.value))
        ledger.close()

    assert refusals[False] == refusals[True]
    assert refusals[False][0] is IdempotencyConflictError


@pytest.mark.parametrize("wrap", [False, True], ids=["native", "python"])
def test_broker_unknown_refusal_chains_the_broker_error(tmp_path: Path, wrap: bool) -> None:
    broker = FakeBroker()
    manager, ledger = _manager_on(tmp_path / f"unknown-{wrap}.db", broker, wrap=wrap)
    bracket = manager.create_bracket(make_intent(), Decimal("10"))
    broker.submit_error = TimeoutError("socket timed out")

    with pytest.raises(BrokerOutcomeUnknownError) as info:
        manager.submit(bracket.entry)

    assert info.value.__cause__ is broker.submit_error
    assert manager.get_order(bracket.entry.order_id).state is OrderState.PENDING_UNKNOWN
    assert len(broker.submitted) == 1
    ledger.close()


@pytest.mark.parametrize("wrap", [False, True], ids=["native", "python"])
def test_closed_ledger_refuses_before_any_write(tmp_path: Path, wrap: bool) -> None:
    path = tmp_path / f"closed-{wrap}.db"
    manager, ledger = _manager_on(path, FakeBroker(), wrap=wrap)
    ledger.close()

    with pytest.raises(RuntimeError, match="Ledger is not open"):
        manager.create_bracket(make_intent(), Decimal("10"))

    reopened = Ledger(path)
    reopened.open()
    assert reopened.count() == 0
    reopened.close()

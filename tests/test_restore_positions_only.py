"""F10: a restored sim/snapshot venue holds the ledger's positions even with no working orders.

``EodRunner._restore_venue`` used to restore positions only when at least one working
order came back. An account holding stock, or an option with no working order, then saw
an empty venue while the ledger (I2, the source of truth) said otherwise.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from trade_engine.calendar import get_calendar
from trade_engine.clock.replay import ReplayClock
from trade_engine.domain.instruments import Equity, Side
from trade_engine.domain.orders import Order, OrderState, OrderType
from trade_engine.eod import EodRunner, EodRunnerConfig
from trade_engine.interfaces.market_data import Bar
from trade_engine.ledger import Ledger
from trade_engine.oms.manager import OrderManager
from trade_engine.sim import SimBroker, SnapshotVenue
from tests.test_eod_options import ACCOUNT as OPT_ACCOUNT
from tests.test_eod_options import P270, S1, S2, NoBars, Rig, csp

ACCOUNT = "restore-positions-account"
XYZ = Equity("XYZ")
CALENDAR = get_calendar()
PREV_EOD = datetime(2026, 9, 22, 21, 45, tzinfo=UTC)
SESSION_OPEN = datetime(2026, 9, 23, 13, 30, tzinfo=UTC)
SESSION = date(2026, 9, 23)


class SettableClock(ReplayClock):
    """Replay clock with a set() for test seeding; it only moves forward."""

    def set(self, now: datetime) -> None:
        if now < self._current_time:
            raise ValueError("cannot move the clock backwards")
        self._current_time = now.astimezone(UTC)


def _bar(timestamp: datetime) -> Bar:
    return Bar(
        instrument=XYZ,
        timestamp=timestamp,
        open=Decimal("100"),
        high=Decimal("101"),
        low=Decimal("99"),
        close=Decimal("100"),
        volume=Decimal("10000"),
        as_of=timestamp,
    )


def _seed_filled_long(ledger: Ledger, broker: SimBroker, clock: SettableClock) -> None:
    """Buy 100 XYZ with a standalone market order that fills at the open.

    No bracket, so no protective exits: the ledger holds 100 shares and every order is
    terminal. That is the case F10 is about.
    """
    order = Order(
        order_id="f10-long",
        account_id=ACCOUNT,
        instrument=XYZ,
        order_type=OrderType.MARKET,
        side=Side.BUY,
        quantity=Decimal("100"),
        command_id="f10-long",
        created_at=PREV_EOD,
    )
    manager = OrderManager(broker, clock, ledger)
    manager.submit(order)
    clock.set(SESSION_OPEN)
    broker.process_bar(_bar(SESSION_OPEN))
    manager.reconcile_order(order.order_id)


def _prepare(ledger: Ledger, clock, venue) -> None:
    runner = EodRunner(
        ledger,
        clock,
        CALENDAR,
        NoBars(),
        EodRunnerConfig(job_name="eod", brokers={ACCOUNT: venue}),
    )
    # The native prepare runs the restore through ``_restore_venue``; drive that step directly.
    venue.connect()
    runner._restore_venue(ACCOUNT, venue)


def _held(venue) -> list[tuple[Equity, Decimal]]:
    return [(position.instrument, position.quantity) for position in venue.positions()]


def test_snapshot_venue_holds_stock_restored_with_no_working_orders(tmp_path) -> None:
    clock = SettableClock(PREV_EOD)
    ledger = Ledger(tmp_path / "eod-ledger.db")
    ledger.open()
    seeder = SimBroker(ACCOUNT, clock, Decimal("0"))
    seeder.connect()
    _seed_filled_long(ledger, seeder, clock)
    assert ledger.state(ACCOUNT).positions[XYZ].quantity == Decimal("100")

    venue = SnapshotVenue(ACCOUNT, clock)
    _prepare(ledger, clock, venue)

    assert _held(venue) == [(XYZ, Decimal("100"))]
    ledger.close()


def test_simbroker_holds_stock_restored_with_no_working_orders(tmp_path) -> None:
    clock = SettableClock(PREV_EOD)
    ledger = Ledger(tmp_path / "eod-ledger.db")
    ledger.open()
    seeder = SimBroker(ACCOUNT, clock, Decimal("0"))
    seeder.connect()
    _seed_filled_long(ledger, seeder, clock)

    fresh = SimBroker(ACCOUNT, clock, Decimal("0"))
    _prepare(ledger, clock, fresh)

    assert _held(fresh) == [(XYZ, Decimal("100"))]
    ledger.close()


def test_option_held_with_no_working_order_is_restored(tmp_path) -> None:
    # A short put filled at a snapshot with no profit target: the only thing the ledger
    # holds is the position itself.
    rig = Rig(tmp_path)
    try:
        rig.strategy.entries[S1] = [csp(target=None)]
        rig.run(S1)
        rig.run(S2)
        assert rig.held(P270) == Decimal("-1")
        assert all(order.state in (OrderState.FILLED, OrderState.CANCELLED, OrderState.EXPIRED, OrderState.REJECTED)
                   for order in rig.state.orders.values())

        rig.venue = SnapshotVenue(OPT_ACCOUNT, rig.clock)  # the next run is a new process
        runner = EodRunner(rig.ledger, rig.clock, CALENDAR, NoBars(), rig.config())
        rig.venue.connect()
        runner._restore_venue(OPT_ACCOUNT, rig.venue)

        assert _held(rig.venue) == [(P270, Decimal("-1"))]
    finally:
        rig.ledger.close()


def test_flat_account_does_not_call_restore(tmp_path, monkeypatch) -> None:
    clock = SettableClock(PREV_EOD)
    ledger = Ledger(tmp_path / "eod-ledger.db")
    ledger.open()
    venue = SnapshotVenue(ACCOUNT, clock)
    calls: list[tuple] = []
    original = venue.restore

    def spy(orders, fills, positions):
        calls.append((list(orders), list(fills), list(positions)))
        return original(orders, fills, positions)

    monkeypatch.setattr(venue, "restore", spy)
    _prepare(ledger, clock, venue)

    assert calls == []
    assert _held(venue) == []
    ledger.close()

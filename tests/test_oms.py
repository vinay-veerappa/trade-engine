from datetime import datetime, timezone
from decimal import Decimal

from trade_engine.domain.instruments import Equity, Side
from trade_engine.domain.signals import OrderIntent
from trade_engine.domain.orders import Order, OrderType
from trade_engine.interfaces.broker import Capabilities, VenueAck
from trade_engine.oms import OrderManager, TrailingStopEmulator


class FakeClock:
    def now_utc(self):
        return datetime(2026, 9, 23, 20, 0, tzinfo=timezone.utc)

    def sleep(self, seconds):
        raise AssertionError("sleep is not used by OMS tests")


class FakeBroker:
    name = "fake"
    env = "sim"
    capabilities = Capabilities(
        supported_order_types=frozenset({"LIMIT", "STOP"}),
        supported_tifs=frozenset({"DAY"}),
        supports_multi_leg=False,
        supports_native_stops=True,
        supports_streaming=False,
    )

    def __init__(self):
        self.submitted = []
        self.cancelled = []

    def submit(self, order):
        self.submitted.append(order)
        return VenueAck(order.venue_order_id, "ACCEPTED", datetime.now(timezone.utc))

    def cancel(self, venue_order_id):
        self.cancelled.append(venue_order_id)
        return VenueAck(venue_order_id, "ACCEPTED", datetime.now(timezone.utc))


def _intent():
    return OrderIntent(
        intent_id="intent-1",
        account_id="account-1",
        instrument=Equity("AAPL"),
        side=Side.BUY,
        quantity_rule="fixed_10",
        entry_price=Decimal("100"),
        stop_loss=Decimal("95"),
        profit_targets=(Decimal("105"), Decimal("110")),
        reason="breakout",
        command_id="command-1",
    )


def test_bracket_is_deterministic_and_oco_cancels_siblings():
    broker = FakeBroker()
    manager = OrderManager(broker, FakeClock())
    bracket = manager.create_bracket(_intent(), Decimal("10"))
    assert manager.create_bracket(_intent(), Decimal("10")) == bracket

    manager.submit(bracket.stop)
    manager.submit(bracket.targets[0])
    resolved = manager.resolve(bracket.targets[0].order_id, filled=True)

    assert any(order.order_id == bracket.targets[0].order_id and order.state.value == "FILLED" for order in resolved)
    assert any(order.order_id == bracket.stop.order_id and order.state.value == "CANCELLED" for order in resolved)
    assert broker.cancelled == [bracket.stop.order_id]


def test_trailing_stop_emulator_tracks_extreme_and_triggers():
    emulator = TrailingStopEmulator(Side.BUY, Decimal("5"))
    assert not emulator.update(Decimal("100"))
    assert emulator.stop_price == Decimal("95")
    assert not emulator.update(Decimal("110"))
    assert emulator.stop_price == Decimal("105")
    assert emulator.update(Decimal("104"))
    assert emulator.triggered


def test_trailing_order_is_emulated_when_venue_lacks_native_support():
    broker = FakeBroker()
    manager = OrderManager(broker, FakeClock())
    order = Order(
        order_id="trail-1",
        account_id="account-1",
        instrument=Equity("AAPL"),
        order_type=OrderType.TRAIL,
        side=Side.SELL,
        quantity=Decimal("10"),
        trail_amount=Decimal("5"),
        command_id="trail-command",
        created_at=FakeClock().now_utc(),
    )

    assert manager.submit_trailing(order, (Decimal("100"), Decimal("110"), Decimal("104")))
    assert broker.submitted[-1].order_type == OrderType.STOP
    assert broker.submitted[-1].stop_price == Decimal("105")

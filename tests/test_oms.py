from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from trade_engine.domain.instruments import Equity, OptionContract, OptionRight, Side
from trade_engine.domain.orders import Order, OrderState, OrderType, TimeInForce
from trade_engine.domain.portfolio import Fill
from trade_engine.domain.signals import OrderIntent
from trade_engine.interfaces.broker import (
    Capabilities,
    OrderChanges,
    VenueAck,
    VenueFill,
    VenueOrder,
    VenueOrderState,
)
from trade_engine.ledger import EmulatedOrderState, Event, EventKind, Ledger
from trade_engine.ledger.codec import decode_payload, encode_payload
from trade_engine.oms import (
    BrokerOutcomeUnknownError,
    IdempotencyConflictError,
    OCOOutcomeUnknownError,
    OrderManagementError,
    OrderManager,
    OrderPendingReconciliationError,
    OrderReconciliationError,
    TrailingStopEmulator,
    UnsupportedOrderCapabilityError,
)

NOW = datetime(2026, 9, 23, 20, 0, tzinfo=timezone.utc)


class FakeClock:
    def now_utc(self) -> datetime:
        return NOW

    def sleep(self, seconds: float) -> None:
        raise AssertionError("OMS must not sleep")


class FakeBroker:
    name = "fake"
    env = "sim"

    def __init__(
        self,
        *,
        order_types: frozenset[OrderType] | None = None,
        tifs: frozenset[TimeInForce] | None = None,
        native_stops: bool = True,
    ) -> None:
        self.capabilities = Capabilities(
            supported_order_types=order_types
            if order_types is not None
            else frozenset({OrderType.MARKET, OrderType.LIMIT, OrderType.STOP}),
            supported_tifs=tifs
            if tifs is not None
            else frozenset({TimeInForce.DAY, TimeInForce.GTC}),
            supports_multi_leg=False,
            supports_native_stops=native_stops,
            supports_streaming=True,
        )
        self.submitted: list[VenueOrder] = []
        self.cancelled: list[str] = []
        self.replaced: list[tuple[str, OrderChanges]] = []
        self.submit_status = "ACCEPTED"
        self.cancel_status = "ACCEPTED"
        self.replace_status = "ACCEPTED"
        self.submit_error: Exception | None = None
        self.order_readback: list[VenueOrderState] = []
        self.fill_readback = []

    def connect(self):
        raise AssertionError("connect is not used in OMS tests")

    def submit(self, order: VenueOrder) -> VenueAck:
        self.submitted.append(order)
        if self.submit_error is not None:
            raise self.submit_error
        return VenueAck(order.venue_order_id, self.submit_status, NOW, "test ack")

    def cancel(self, venue_order_id: str) -> VenueAck:
        self.cancelled.append(venue_order_id)
        return VenueAck(venue_order_id, self.cancel_status, NOW, "test cancel")

    def replace(self, venue_order_id: str, changes: OrderChanges) -> VenueAck:
        self.replaced.append((venue_order_id, changes))
        return VenueAck(venue_order_id, self.replace_status, NOW, "test replace")

    def orders(self, since: datetime):
        return self.order_readback

    def fills(self, since: datetime):
        return self.fill_readback


def make_intent(
    *,
    command_id: str = "command-1",
    side: Side = Side.BUY,
    reason: str = "breakout",
    targets: tuple[Decimal, ...] = (Decimal("105"), Decimal("110")),
) -> OrderIntent:
    return OrderIntent(
        intent_id="intent-1",
        account_id="account-1",
        instrument=Equity("AAPL"),
        side=side,
        quantity_rule="fixed_10",
        entry_price=Decimal("100"),
        stop_loss=Decimal("95") if side is Side.BUY else Decimal("105"),
        profit_targets=targets
        if side is Side.BUY
        else tuple(Decimal("100") - (target - Decimal("100")) for target in targets),
        reason=reason,
        command_id=command_id,
    )


def make_manager(path, broker: FakeBroker, clock: FakeClock | None = None):
    ledger = Ledger(path)
    ledger.open()
    return OrderManager(broker, clock or FakeClock(), ledger), ledger


def make_fill(
    order: Order,
    fill_id: str,
    quantity: str,
    price: str,
) -> Fill:
    return Fill(
        fill_id=fill_id,
        order_id=order.order_id,
        account_id=order.account_id,
        instrument=order.instrument,
        quantity=Decimal(quantity),
        price=Decimal(price),
        venue_env="sim",
        filled_at=NOW,
        side=order.side,
        venue_order_id=order.order_id,
        venue_execution_id=fill_id,
    )


@pytest.fixture
def manager_factory(tmp_path):
    opened = []

    def create(broker: FakeBroker | None = None, name: str = "orders.db"):
        instance, ledger = make_manager(tmp_path / name, broker or FakeBroker())
        opened.append(ledger)
        return instance, ledger, broker or instance._broker

    yield create
    for ledger in opened:
        ledger.close()


def test_bracket_replay_is_persisted_and_payload_conflicts_are_refused(manager_factory):
    broker = FakeBroker()
    first, ledger, _ = manager_factory(broker)
    bracket = first.create_bracket(make_intent(), Decimal("10"))
    before = ledger.count()

    second = OrderManager(broker, FakeClock(), ledger)
    replay = second.create_bracket(make_intent(), Decimal("10"))

    assert replay == bracket
    assert ledger.count() == before
    with pytest.raises(IdempotencyConflictError):
        second.create_bracket(make_intent(), Decimal("50"))


def test_replaying_full_command_sequence_after_restart_is_stable(manager_factory):
    broker = FakeBroker()
    manager, ledger, _ = manager_factory(broker)
    bracket = manager.create_bracket(make_intent(), Decimal("10"))
    manager.submit(bracket.entry)
    manager.record_fill(make_fill(bracket.entry, "restart-entry-fill", "10", "100"))
    before = ledger.count()
    expected = tuple(manager.get_order(order.order_id) for order in (
        bracket.entry,
        bracket.stop,
        *bracket.targets,
    ))

    ledger.close()
    ledger.open()
    restarted = OrderManager(broker, FakeClock(), ledger)
    replayed = restarted.create_bracket(make_intent(), Decimal("10"))
    restarted.submit(replayed.entry)
    restarted.record_fill(
        make_fill(replayed.entry, "restart-entry-fill", "10", "100")
    )

    actual = tuple(
        restarted.get_order(order.order_id)
        for order in (replayed.entry, replayed.stop, *replayed.targets)
    )
    assert actual == expected
    assert ledger.count() == before
    assert len(broker.submitted) == 4


def test_children_are_held_and_target_quantity_is_split(manager_factory):
    manager, ledger, broker = manager_factory()
    bracket = manager.create_bracket(make_intent(), Decimal("10"))

    with pytest.raises(OrderManagementError, match="held"):
        manager.submit(bracket.stop)
    assert broker.submitted == []

    manager.submit(bracket.entry)
    manager.record_fill(make_fill(bracket.entry, "entry-fill", "10", "100"))

    assert [order.venue_order_id for order in broker.submitted] == [
        bracket.entry.order_id,
        bracket.stop.order_id,
        bracket.targets[0].order_id,
        bracket.targets[1].order_id,
    ]
    assert [order.quantity for order in broker.submitted[2:]] == [
        Decimal("5"),
        Decimal("5"),
    ]
    assert ledger.state("account-1").orders[bracket.targets[0].order_id].state is OrderState.ACCEPTED


def test_equity_bracket_allocates_whole_shares_and_refuses_unallocatable_targets(manager_factory):
    manager, ledger, _ = manager_factory()
    intent = make_intent(
        command_id="three-targets",
        targets=(Decimal("105"), Decimal("110"), Decimal("115")),
    )

    bracket = manager.create_bracket(intent, Decimal("10"))

    assert [target.quantity for target in bracket.targets] == [
        Decimal("4"),
        Decimal("3"),
        Decimal("3"),
    ]
    with pytest.raises(ValueError, match="too small"):
        manager.create_bracket(
            make_intent(
                command_id="too-many-targets",
                targets=(Decimal("105"), Decimal("110"), Decimal("115")),
            ),
            Decimal("2"),
        )
    with pytest.raises(ValueError, match="whole number"):
        manager.create_bracket(
            make_intent(command_id="fractional-equity"), Decimal("10.5")
        )
    assert ledger.count() == 1


def test_partial_entry_allocates_whole_shares_across_three_targets(manager_factory):
    manager, _, broker = manager_factory()
    bracket = manager.create_bracket(
        make_intent(
            command_id="partial-three-targets",
            targets=(Decimal("105"), Decimal("110"), Decimal("115")),
        ),
        Decimal("10"),
    )
    manager.submit(bracket.entry)
    manager.record_fill(make_fill(bracket.entry, "partial-three-fill", "4", "100"))

    manager.cancel(bracket.entry.order_id, command_id="partial-three-cancel")

    assert [order.quantity for order in broker.submitted[-3:]] == [
        Decimal("2"),
        Decimal("1"),
        Decimal("1"),
    ]


def test_partial_entry_protects_only_filled_quantity_and_scales_targets_on_cancel(manager_factory):
    manager, _, broker = manager_factory()
    bracket = manager.create_bracket(make_intent(), Decimal("10"))
    manager.submit(bracket.entry)
    manager.record_fill(make_fill(bracket.entry, "entry-part", "4", "100"))

    assert [item.quantity for item in broker.submitted] == [Decimal("10"), Decimal("4")]
    assert manager.get_order(bracket.stop.order_id).state is OrderState.ACCEPTED
    assert all(target.order_id not in [item.venue_order_id for item in broker.submitted] for target in bracket.targets)

    manager.cancel(bracket.entry.order_id, command_id="cancel-entry")

    assert [item.quantity for item in broker.submitted[2:]] == [
        Decimal("2"),
        Decimal("2"),
    ]


def test_partial_target_fill_reduces_stop_without_cancelling_other_target(manager_factory):
    manager, _, broker = manager_factory()
    bracket = manager.create_bracket(make_intent(), Decimal("10"))
    manager.submit(bracket.entry)
    manager.record_fill(make_fill(bracket.entry, "entry-fill", "10", "100"))

    manager.record_fill(make_fill(bracket.targets[0], "target-part", "2", "105"))

    assert manager.get_order(bracket.targets[0].order_id).state is OrderState.PARTIALLY_FILLED
    assert manager.get_order(bracket.stop.order_id).quantity == Decimal("8")
    assert broker.replaced == [
        (bracket.stop.order_id, OrderChanges(new_quantity=Decimal("8")))
    ]
    assert manager.get_order(bracket.targets[1].order_id).state is OrderState.ACCEPTED
    assert broker.cancelled == []


def test_full_target_fill_resizes_stop_and_keeps_other_target_working(manager_factory):
    manager, _, broker = manager_factory()
    bracket = manager.create_bracket(make_intent(), Decimal("10"))
    manager.submit(bracket.entry)
    manager.record_fill(make_fill(bracket.entry, "entry-fill", "10", "100"))

    manager.record_fill(make_fill(bracket.targets[0], "target-one", "5", "105"))

    assert manager.get_order(bracket.targets[0].order_id).state is OrderState.FILLED
    assert manager.get_order(bracket.stop.order_id).quantity == Decimal("5")
    assert manager.get_order(bracket.targets[1].order_id).state is OrderState.ACCEPTED
    assert broker.cancelled == []


def test_stop_fill_cancels_target_siblings_only_after_confirmed_ack(manager_factory):
    manager, _, broker = manager_factory()
    bracket = manager.create_bracket(make_intent(), Decimal("10"))
    manager.submit(bracket.entry)
    manager.record_fill(make_fill(bracket.entry, "entry-fill", "10", "100"))

    manager.record_fill(make_fill(bracket.stop, "stop-fill", "10", "95"))

    assert broker.cancelled == [target.order_id for target in bracket.targets]
    assert all(
        manager.get_order(target.order_id).state is OrderState.CANCELLED
        for target in bracket.targets
    )


def test_partial_stop_fill_keeps_stop_at_total_size_and_cancels_targets(manager_factory):
    manager, ledger, broker = manager_factory()
    bracket = manager.create_bracket(make_intent(), Decimal("10"))
    manager.submit(bracket.entry)
    manager.record_fill(make_fill(bracket.entry, "partial-stop-entry", "10", "100"))

    manager.record_fill(make_fill(bracket.stop, "partial-stop-exit", "4", "95"))

    assert manager.get_order(bracket.stop.order_id).quantity == Decimal("10")
    assert ledger.state("account-1").filled_quantity[bracket.stop.order_id] == Decimal("4")
    assert broker.replaced == []
    assert broker.cancelled == [target.order_id for target in bracket.targets]
    assert all(
        manager.get_order(target.order_id).state is OrderState.CANCELLED
        for target in bracket.targets
    )


def test_only_protective_stop_cannot_be_cancelled_while_position_is_open(manager_factory):
    manager, _, broker = manager_factory()
    bracket = manager.create_bracket(make_intent(), Decimal("10"))
    manager.submit(bracket.entry)
    manager.record_fill(make_fill(bracket.entry, "guard-entry-fill", "10", "100"))

    with pytest.raises(OrderManagementError, match="protective stop"):
        manager.cancel(bracket.stop.order_id, command_id="cancel-only-stop")

    assert manager.get_order(bracket.stop.order_id).state is OrderState.ACCEPTED
    assert broker.cancelled == []


def test_rejected_oco_cancel_remains_pending_and_surfaces_conflict(manager_factory):
    manager, ledger, broker = manager_factory()
    bracket = manager.create_bracket(make_intent(), Decimal("10"))
    manager.submit(bracket.entry)
    manager.record_fill(make_fill(bracket.entry, "entry-fill", "10", "100"))
    broker.cancel_status = "REJECTED"

    with pytest.raises(OCOOutcomeUnknownError, match="rejected cancel"):
        manager.record_fill(make_fill(bracket.stop, "stop-fill", "10", "95"))

    target_state = manager.get_order(bracket.targets[0].order_id).state
    assert target_state is OrderState.PENDING_UNKNOWN
    assert target_state is not OrderState.CANCELLED
    assert any(event.kind is EventKind.ORDER_REFUSED for event in ledger.events())


def test_rejected_child_does_not_break_target_resolution_with_illegal_transition(manager_factory):
    manager, _, broker = manager_factory()
    bracket = manager.create_bracket(make_intent(), Decimal("10"))
    manager.submit(bracket.entry)
    broker.submit_status = "REJECTED"
    with pytest.raises(OrderManagementError, match="Protective stop"):
        manager.record_fill(make_fill(bracket.entry, "entry-fill", "10", "100"))

    assert manager.get_order(bracket.stop.order_id).state is OrderState.REJECTED
    assert all(
        target.order_id not in {order.venue_order_id for order in broker.submitted}
        for target in bracket.targets
    )


def test_cancel_pending_ack_is_not_reported_cancelled(manager_factory):
    manager, _, broker = manager_factory()
    order = Order(
        order_id="standalone",
        account_id="account-1",
        instrument=Equity("AAPL"),
        order_type=OrderType.LIMIT,
        side=Side.BUY,
        quantity=Decimal("1"),
        command_id="standalone-command",
        created_at=NOW,
        limit_price=Decimal("100"),
    )
    manager.submit(order)
    broker.cancel_status = "PENDING"

    cancelled = manager.cancel(order.order_id, command_id="cancel-1")

    assert cancelled.state is OrderState.PENDING_UNKNOWN
    assert manager.get_order(order.order_id).state is OrderState.PENDING_UNKNOWN


def test_pending_entry_cancel_preserves_children_until_entry_is_terminal(manager_factory):
    manager, _, broker = manager_factory()
    bracket = manager.create_bracket(make_intent(), Decimal("10"))
    manager.submit(bracket.entry)
    broker.cancel_status = "PENDING"

    pending = manager.cancel(bracket.entry.order_id, command_id="pending-entry-cancel")

    assert pending.state is OrderState.PENDING_UNKNOWN
    assert manager.get_order(bracket.stop.order_id).state is OrderState.NEW
    assert all(
        manager.get_order(target.order_id).state is OrderState.NEW
        for target in bracket.targets
    )
    assert broker.cancelled == [bracket.entry.order_id]

    manager.record_fill(make_fill(bracket.entry, "late-entry-fill", "4", "100"))

    assert manager.get_order(bracket.stop.order_id).state is OrderState.ACCEPTED
    assert manager.get_order(bracket.stop.order_id).quantity == Decimal("4")
    assert all(
        manager.get_order(target.order_id).state is OrderState.NEW
        for target in bracket.targets
    )


def test_reconcile_confirmed_entry_cancel_cancels_unrouted_children(manager_factory):
    manager, _, broker = manager_factory()
    bracket = manager.create_bracket(make_intent(), Decimal("10"))
    manager.submit(bracket.entry)
    broker.cancel_status = "PENDING"
    manager.cancel(bracket.entry.order_id, command_id="pending-entry-cancel")
    broker.order_readback = [
        VenueOrderState(
            venue_order_id=bracket.entry.order_id,
            state=OrderState.CANCELLED,
            filled_quantity=Decimal("0"),
            remaining_quantity=Decimal("10"),
            updated_at=NOW,
        )
    ]

    assert manager.reconcile_order(bracket.entry.order_id).state is OrderState.CANCELLED
    assert manager.get_order(bracket.stop.order_id).state is OrderState.CANCELLED
    assert all(
        manager.get_order(target.order_id).state is OrderState.CANCELLED
        for target in bracket.targets
    )


def test_entry_cancel_cancels_unrouted_children_locally(manager_factory):
    manager, _, broker = manager_factory()
    bracket = manager.create_bracket(make_intent(), Decimal("10"))
    manager.submit(bracket.entry)

    manager.cancel(bracket.entry.order_id, command_id="cancel-entry")

    assert manager.get_order(bracket.entry.order_id).state is OrderState.CANCELLED
    assert manager.get_order(bracket.stop.order_id).state is OrderState.CANCELLED
    assert all(manager.get_order(target.order_id).state is OrderState.CANCELLED for target in bracket.targets)
    assert broker.cancelled == [bracket.entry.order_id]


def test_submit_exception_is_persistently_unknown_and_never_resent(manager_factory):
    manager, ledger, broker = manager_factory()
    order = Order(
        order_id="timeout-order",
        account_id="account-1",
        instrument=Equity("AAPL"),
        order_type=OrderType.MARKET,
        side=Side.BUY,
        quantity=Decimal("1"),
        command_id="timeout-command",
        created_at=NOW,
    )
    broker.submit_error = TimeoutError("socket timed out")

    with pytest.raises(BrokerOutcomeUnknownError):
        manager.submit(order)
    assert manager.get_order(order.order_id).state is OrderState.PENDING_UNKNOWN
    assert len(broker.submitted) == 1

    broker.submit_error = None
    assert manager.submit(order).state is OrderState.PENDING_UNKNOWN
    assert len(broker.submitted) == 1
    assert ledger.state("account-1").orders[order.order_id].state is OrderState.PENDING_UNKNOWN


def test_unsupported_order_type_and_tif_are_refused_before_venue_submission(manager_factory):
    broker = FakeBroker(order_types=frozenset({OrderType.LIMIT}), tifs=frozenset({TimeInForce.DAY}))
    manager, ledger, _ = manager_factory(broker)
    market = Order(
        order_id="market",
        account_id="account-1",
        instrument=Equity("AAPL"),
        order_type=OrderType.MARKET,
        side=Side.BUY,
        quantity=Decimal("1"),
        command_id="market-command",
        created_at=NOW,
    )
    with pytest.raises(UnsupportedOrderCapabilityError, match="MARKET"):
        manager.submit(market)
    assert broker.submitted == []

    gtc = replace_order_tif(
        Order(
            order_id="limit-order",
            account_id="account-1",
            instrument=Equity("AAPL"),
            order_type=OrderType.LIMIT,
            side=Side.BUY,
            quantity=Decimal("1"),
            command_id="limit-command",
            created_at=NOW,
            limit_price=Decimal("100"),
        ),
        TimeInForce.GTC,
        "gtc-order",
    )
    with pytest.raises(UnsupportedOrderCapabilityError, match="GTC"):
        manager.submit(gtc)
    assert broker.submitted == []
    assert len([event for event in ledger.events() if event.kind is EventKind.ORDER_REFUSED]) == 2


def test_trailing_stop_sell_protects_long_and_emits_market_or_limit_not_stop(manager_factory):
    broker = FakeBroker(order_types=frozenset({OrderType.LIMIT, OrderType.STOP}))
    manager, ledger, _ = manager_factory(broker)
    order = Order(
        order_id="long-trail",
        account_id="account-1",
        instrument=Equity("AAPL"),
        order_type=OrderType.TRAIL,
        side=Side.SELL,
        quantity=Decimal("10"),
        trail_amount=Decimal("5"),
        command_id="trail-command",
        created_at=NOW,
    )

    assert manager.submit_trailing(order).state is OrderState.NEW
    assert broker.submitted == []
    manager.update_trailing(order.order_id, Decimal("100"), command_id="tick-1")
    manager = OrderManager(broker, FakeClock(), ledger)
    manager.update_trailing(order.order_id, Decimal("110"), command_id="tick-2")
    triggered = manager.update_trailing(order.order_id, Decimal("104"), command_id="tick-3")

    assert triggered.state is OrderState.ACCEPTED
    assert broker.submitted[-1].order_type is OrderType.LIMIT
    assert broker.submitted[-1].limit_price == Decimal("104")
    assert broker.submitted[-1].stop_price is None
    assert ledger.state("account-1").emulated_orders[order.order_id].stop_price == Decimal("105")


def test_trailing_stop_buy_protects_short_and_native_capability_is_honored(manager_factory):
    emulator = TrailingStopEmulator(Side.BUY, Decimal("5"))
    assert not emulator.update(Decimal("100"))
    assert not emulator.update(Decimal("90"))
    assert emulator.stop_price == Decimal("95")
    assert emulator.update(Decimal("96"))

    broker = FakeBroker(
        order_types=frozenset({OrderType.TRAIL, OrderType.MARKET}),
    )
    manager, _, _ = manager_factory(broker)
    order = Order(
        order_id="native-trail",
        account_id="account-1",
        instrument=Equity("AAPL"),
        order_type=OrderType.TRAIL,
        side=Side.BUY,
        quantity=Decimal("1"),
        trail_amount=Decimal("2"),
        command_id="native-command",
        created_at=NOW,
    )

    assert manager.submit_trailing(order).state is OrderState.ACCEPTED
    assert broker.submitted[-1].order_type is OrderType.TRAIL


def test_replace_refuses_terminal_order_and_pending_result_is_not_success(manager_factory):
    manager, _, broker = manager_factory()
    order = Order(
        order_id="replace-order",
        account_id="account-1",
        instrument=Equity("AAPL"),
        order_type=OrderType.LIMIT,
        side=Side.BUY,
        quantity=Decimal("2"),
        command_id="replace-command",
        created_at=NOW,
        limit_price=Decimal("100"),
    )
    manager.submit(order)
    broker.replace_status = "PENDING"
    pending = manager.replace(
        order.order_id,
        OrderChanges(new_quantity=Decimal("3")),
        command_id="replace-pending",
    )

    assert pending.state is OrderState.PENDING_UNKNOWN
    assert pending.quantity == Decimal("2")
    assert len(broker.replaced) == 1
    with pytest.raises(OrderPendingReconciliationError):
        manager.replace(
            order.order_id,
            OrderChanges(new_quantity=Decimal("3")),
            command_id="replace-pending",
        )
    with pytest.raises(IdempotencyConflictError, match="different replace changes"):
        manager.replace(
            order.order_id,
            OrderChanges(new_quantity=Decimal("4")),
            command_id="replace-pending",
        )
    assert len(broker.replaced) == 1
    with pytest.raises(OrderPendingReconciliationError):
        manager.replace(
            order.order_id,
            OrderChanges(new_quantity=Decimal("3")),
            command_id="replace-again",
        )
    with pytest.raises(OrderPendingReconciliationError, match="pending reconciliation"):
        manager.replace(
            order.order_id,
            OrderChanges(new_quantity=Decimal("4")),
            command_id="replace-terminal",
        )


@pytest.mark.parametrize(
    "venue_state",
    [OrderState.ACCEPTED, OrderState.SUBMITTED, OrderState.PARTIALLY_FILLED],
)
def test_reconcile_does_not_accept_pending_replace_with_old_terms(manager_factory, venue_state):
    manager, _, broker = manager_factory()
    order = Order(
        order_id="pending-replace-readback",
        account_id="account-1",
        instrument=Equity("AAPL"),
        order_type=OrderType.LIMIT,
        side=Side.BUY,
        quantity=Decimal("2"),
        command_id="pending-replace-readback-command",
        created_at=NOW,
        limit_price=Decimal("100"),
    )
    manager.submit(order)
    broker.replace_status = "PENDING"
    pending = manager.replace(
        order.order_id,
        OrderChanges(new_limit_price=Decimal("101")),
        command_id="pending-limit-update",
    )
    broker.order_readback = [
        VenueOrderState(
            venue_order_id=order.order_id,
            state=venue_state,
            filled_quantity=(
                Decimal("1") if venue_state is OrderState.PARTIALLY_FILLED else Decimal("0")
            ),
            remaining_quantity=Decimal("1"),
            updated_at=NOW,
        )
    ]
    broker.fill_readback = [
        VenueFill(
            venue_fill_id="pending-replace-partial-fill",
            venue_order_id=order.order_id,
            instrument=order.instrument,
            quantity=Decimal("1"),
            price=Decimal("101"),
            filled_at=NOW,
            side=order.side,
        )
    ]

    with pytest.raises(OrderReconciliationError, match="replace terms"):
        manager.reconcile_order(order.order_id)

    assert pending.state is OrderState.PENDING_UNKNOWN
    assert manager.get_order(order.order_id).state is OrderState.PENDING_UNKNOWN
    assert manager.get_order(order.order_id).limit_price == Decimal("100")


@pytest.mark.parametrize(
    "venue_state",
    [
        OrderState.CANCELLED,
        OrderState.REJECTED,
        OrderState.EXPIRED,
        OrderState.FILLED,
    ],
)
def test_reconcile_resolves_terminal_state_after_pending_replace(
    manager_factory, venue_state
):
    manager, _, broker = manager_factory()
    order = Order(
        order_id=f"pending-replace-terminal-{venue_state.value.lower()}",
        account_id="account-1",
        instrument=Equity("AAPL"),
        order_type=OrderType.LIMIT,
        side=Side.BUY,
        quantity=Decimal("2"),
        command_id=f"pending-replace-terminal-{venue_state.value.lower()}-command",
        created_at=NOW,
        limit_price=Decimal("100"),
    )
    manager.submit(order)
    broker.replace_status = "PENDING"
    manager.replace(
        order.order_id,
        OrderChanges(new_limit_price=Decimal("101")),
        command_id=f"pending-limit-update-{venue_state.value.lower()}",
    )
    broker.order_readback = [
        VenueOrderState(
            venue_order_id=order.order_id,
            state=venue_state,
            filled_quantity=(
                Decimal("2") if venue_state is OrderState.FILLED else Decimal("0")
            ),
            remaining_quantity=(
                Decimal("0") if venue_state is OrderState.FILLED else Decimal("2")
            ),
            updated_at=NOW,
        )
    ]
    if venue_state is OrderState.FILLED:
        broker.fill_readback = [
            VenueFill(
                venue_fill_id="pending-replace-terminal-fill",
                venue_order_id=order.order_id,
                instrument=order.instrument,
                quantity=Decimal("2"),
                price=Decimal("100"),
                filled_at=NOW,
                side=order.side,
            )
        ]

    reconciled = manager.reconcile_order(order.order_id)

    assert reconciled.state is venue_state



def _cancelled_after_partial_fill(manager_factory, *, fill_records: bool):
    manager, ledger, broker = manager_factory()
    bracket = manager.create_bracket(make_intent(), Decimal("10"))
    manager.submit(bracket.entry)
    broker.cancel_status = "PENDING"
    manager.cancel(bracket.entry.order_id, command_id="cancel-entry")
    broker.cancel_status = "ACCEPTED"
    broker.order_readback = [
        VenueOrderState(
            venue_order_id=bracket.entry.order_id,
            state=OrderState.CANCELLED,
            filled_quantity=Decimal("4"),
            remaining_quantity=Decimal("0"),
            updated_at=NOW,
        )
    ]
    broker.fill_readback = (
        [
            VenueFill(
                venue_fill_id="entry-before-cancel",
                venue_order_id=bracket.entry.order_id,
                instrument=bracket.entry.instrument,
                quantity=Decimal("4"),
                price=Decimal("100"),
                filled_at=NOW,
                side=bracket.entry.side,
            )
        ]
        if fill_records
        else []
    )
    return manager, ledger, broker, bracket


def test_reconcile_cancelled_entry_records_partial_fill_and_protects_it(manager_factory):
    manager, ledger, broker, bracket = _cancelled_after_partial_fill(
        manager_factory, fill_records=True
    )

    reconciled = manager.reconcile_order(bracket.entry.order_id)

    state = ledger.fold()["account-1"]
    assert reconciled.state is OrderState.CANCELLED
    assert state.filled_quantity[bracket.entry.order_id] == Decimal("4")
    assert state.positions[bracket.entry.instrument].quantity == Decimal("4")
    stop = manager.get_order(bracket.stop.order_id)
    assert stop.state is OrderState.ACCEPTED
    assert stop.quantity == Decimal("4")
    assert [manager.get_order(t.order_id).quantity for t in bracket.targets] == [
        Decimal("2"),
        Decimal("2"),
    ]


def test_reconcile_refuses_terminal_state_when_fill_records_are_missing(manager_factory):
    manager, ledger, broker, bracket = _cancelled_after_partial_fill(
        manager_factory, fill_records=False
    )

    with pytest.raises(OrderReconciliationError, match="fill records account for 0"):
        manager.reconcile_order(bracket.entry.order_id)

    assert manager.get_order(bracket.entry.order_id).state is OrderState.PENDING_UNKNOWN
    assert manager.get_order(bracket.stop.order_id).state is OrderState.NEW

@pytest.mark.parametrize("venue_state", [OrderState.SUBMITTED, OrderState.EXPIRED])
def test_reconcile_handles_submitted_and_expired_states(manager_factory, venue_state):
    manager, _, broker = manager_factory()
    order = Order(
        order_id=f"reconcile-{venue_state.value.lower()}",
        account_id="account-1",
        instrument=Equity("AAPL"),
        order_type=OrderType.MARKET,
        side=Side.BUY,
        quantity=Decimal("1"),
        command_id=f"reconcile-{venue_state.value.lower()}-command",
        created_at=NOW,
    )
    broker.submit_status = "PENDING"
    manager.submit(order)
    broker.order_readback = [
        VenueOrderState(
            venue_order_id=order.order_id,
            state=venue_state,
            filled_quantity=Decimal("0"),
            remaining_quantity=Decimal("1"),
            updated_at=NOW,
        )
    ]

    assert manager.reconcile_order(order.order_id).state is venue_state


def test_noop_replace_command_is_idempotent_and_payload_bound(manager_factory):
    manager, _, broker = manager_factory()
    order = Order(
        order_id="noop-replace",
        account_id="account-1",
        instrument=Equity("AAPL"),
        order_type=OrderType.LIMIT,
        side=Side.BUY,
        quantity=Decimal("2"),
        command_id="noop-replace-order",
        created_at=NOW,
        limit_price=Decimal("100"),
    )
    manager.submit(order)
    changes = OrderChanges(new_quantity=Decimal("2"))

    first = manager.replace(
        order.order_id, changes, command_id="noop-replace-command"
    )
    assert broker.replaced == []
    replay = manager.replace(
        order.order_id, changes, command_id="noop-replace-command"
    )
    assert first == replay == manager.get_order(order.order_id)
    with pytest.raises(IdempotencyConflictError, match="different replace changes"):
        manager.replace(
            order.order_id,
            OrderChanges(new_quantity=Decimal("3")),
            command_id="noop-replace-command",
        )


def test_emulated_trailing_state_round_trips_through_event_codec(manager_factory):
    manager, ledger, _ = manager_factory(
        FakeBroker(order_types=frozenset({OrderType.MARKET, OrderType.LIMIT}))
    )
    order = Order(
        order_id="codec-trail",
        account_id="account-1",
        instrument=Equity("AAPL"),
        order_type=OrderType.TRAIL,
        side=Side.SELL,
        quantity=Decimal("1"),
        trail_amount=Decimal("2"),
        command_id="codec-trail-command",
        created_at=NOW,
    )
    manager.submit_trailing(order)
    manager.update_trailing(order.order_id, Decimal("10"), command_id="codec-tick")

    payload = ledger.events()[-1].payload
    assert decode_payload(encode_payload(payload)) == payload


def test_unsupported_stop_is_emulated_and_routes_limit_only_on_trigger(manager_factory):
    broker = FakeBroker(order_types=frozenset({OrderType.LIMIT}))
    manager, ledger, _ = manager_factory(broker)
    order = Order(
        order_id="emulated-stop",
        account_id="account-1",
        instrument=Equity("AAPL"),
        order_type=OrderType.STOP,
        side=Side.SELL,
        quantity=Decimal("3"),
        stop_price=Decimal("95"),
        command_id="emulated-stop-command",
        created_at=NOW,
    )

    assert manager.submit(order).state is OrderState.NEW
    assert broker.submitted == []
    assert manager.update_emulated_order(
        order.order_id, Decimal("100"), command_id="stop-tick-1"
    ).state is OrderState.NEW
    assert broker.submitted == []

    triggered = manager.update_emulated_order(
        order.order_id, Decimal("95"), command_id="stop-tick-2"
    )

    assert triggered.state is OrderState.ACCEPTED
    assert len(broker.submitted) == 1
    assert broker.submitted[0].order_type is OrderType.LIMIT
    assert broker.submitted[0].limit_price == Decimal("95")
    assert broker.submitted[0].stop_price is None
    assert ledger.state("account-1").emulated_orders[order.order_id].triggered


def test_unsupported_stop_limit_routes_original_limit_price(manager_factory):
    broker = FakeBroker(order_types=frozenset({OrderType.LIMIT}))
    manager, _, _ = manager_factory(broker)
    order = Order(
        order_id="emulated-stop-limit",
        account_id="account-1",
        instrument=Equity("AAPL"),
        order_type=OrderType.STOP_LIMIT,
        side=Side.BUY,
        quantity=Decimal("2"),
        stop_price=Decimal("105"),
        limit_price=Decimal("106"),
        command_id="emulated-stop-limit-command",
        created_at=NOW,
    )

    assert manager.submit(order).state is OrderState.NEW
    assert broker.submitted == []

    triggered = manager.update_emulated_order(
        order.order_id, Decimal("105"), command_id="stop-limit-tick"
    )

    assert triggered.state is OrderState.ACCEPTED
    assert len(broker.submitted) == 1
    assert broker.submitted[0].order_type is OrderType.LIMIT
    assert broker.submitted[0].limit_price == Decimal("106")
    assert broker.submitted[0].stop_price is None


def test_emulated_bracket_stop_can_be_replaced_and_cancels_targets_before_exit(manager_factory):
    broker = FakeBroker(order_types=frozenset({OrderType.MARKET, OrderType.LIMIT}))
    manager, ledger, _ = manager_factory(broker)
    bracket = manager.create_bracket(make_intent(), Decimal("10"))
    manager.submit(bracket.entry)
    manager.record_fill(make_fill(bracket.entry, "emulated-entry-fill", "10", "100"))

    moved = manager.replace(
        bracket.stop.order_id,
        OrderChanges(new_stop_price=Decimal("100")),
        command_id="move-emulated-stop",
    )

    assert moved.state is OrderState.NEW
    assert moved.stop_price == Decimal("100")
    assert ledger.state("account-1").emulated_orders[
        bracket.stop.order_id
    ].stop_price == Decimal("100")
    assert broker.replaced == []

    triggered = manager.update_emulated_order(
        bracket.stop.order_id, Decimal("100"), command_id="emulated-stop-trigger"
    )

    assert triggered.state is OrderState.ACCEPTED
    assert broker.cancelled == [target.order_id for target in bracket.targets]
    assert all(
        manager.get_order(target.order_id).state is OrderState.CANCELLED
        for target in bracket.targets
    )
    assert broker.submitted[-1].order_type is OrderType.MARKET


def test_supports_native_stops_flag_overrides_order_type_list(manager_factory):
    broker = FakeBroker(
        order_types=frozenset({OrderType.MARKET, OrderType.LIMIT, OrderType.STOP}),
        native_stops=False,
    )
    manager, _, _ = manager_factory(broker)
    order = Order(
        order_id="flag-emulated-stop",
        account_id="account-1",
        instrument=Equity("AAPL"),
        order_type=OrderType.STOP,
        side=Side.SELL,
        quantity=Decimal("1"),
        command_id="flag-emulated-stop-command",
        created_at=NOW,
        stop_price=Decimal("95"),
    )

    assert manager.submit(order).state is OrderState.NEW
    assert broker.submitted == []
    assert manager.update_emulated_order(
        order.order_id, Decimal("95"), command_id="flag-emulated-stop-trigger"
    ).state is OrderState.ACCEPTED
    assert broker.submitted[-1].order_type is OrderType.MARKET


def test_triggered_emulation_is_resumed_after_restart_before_submit(manager_factory):
    broker = FakeBroker(order_types=frozenset({OrderType.LIMIT}))
    manager, ledger, _ = manager_factory(broker)
    order = Order(
        order_id="resumed-stop",
        account_id="account-1",
        instrument=Equity("AAPL"),
        order_type=OrderType.STOP,
        side=Side.SELL,
        quantity=Decimal("1"),
        stop_price=Decimal("95"),
        command_id="resumed-stop-command",
        created_at=NOW,
    )
    manager.submit(order)
    ledger.append(
        Event(
            account="account-1",
            kind=EventKind.ORDER_EMULATION_UPDATED,
            payload=EmulatedOrderState(
                order_id=order.order_id,
                observed_price=Decimal("94"),
                extreme=None,
                stop_price=Decimal("95"),
                triggered=True,
                reason="trigger persisted before venue submission",
            ),
            ts_utc=NOW,
            command_id="resumed-trigger",
        )
    )
    manager = OrderManager(broker, FakeClock(), ledger)

    submitted = manager.update_emulated_order(
        order.order_id, Decimal("94"), command_id="resumed-trigger"
    )

    assert submitted.state is OrderState.ACCEPTED
    assert len(broker.submitted) == 1
    assert broker.submitted[0].order_type is OrderType.LIMIT


def test_reused_standalone_order_id_with_different_payload_is_refused(manager_factory):
    manager, _, broker = manager_factory()
    order = Order(
        order_id="payload-conflict",
        account_id="account-1",
        instrument=Equity("AAPL"),
        order_type=OrderType.LIMIT,
        side=Side.BUY,
        quantity=Decimal("1"),
        command_id="payload-conflict-command",
        created_at=NOW,
        limit_price=Decimal("100"),
    )
    manager.submit(order)
    changed = Order(
        order_id=order.order_id,
        account_id=order.account_id,
        instrument=order.instrument,
        order_type=order.order_type,
        side=order.side,
        quantity=Decimal("2"),
        command_id=order.command_id,
        created_at=order.created_at,
        limit_price=Decimal("101"),
    )

    with pytest.raises(IdempotencyConflictError, match="different order payload"):
        manager.submit(changed)
    assert len(broker.submitted) == 1


def replace_order_tif(order: Order, tif: TimeInForce, order_id: str) -> Order:
    return Order(
        order_id=order_id,
        account_id=order.account_id,
        instrument=order.instrument,
        order_type=order.order_type,
        side=order.side,
        quantity=order.quantity,
        command_id=f"{order.command_id}-{order_id}",
        created_at=order.created_at,
        limit_price=order.limit_price,
        stop_price=order.stop_price,
        trail_amount=order.trail_amount,
        tif=tif,
    )


def test_bracket_defaults_to_day_entry_and_gtc_protective_exits(manager_factory):
    manager, _, broker = manager_factory()
    bracket = manager.create_bracket(make_intent(), Decimal("10"))
    manager.submit(bracket.entry)
    manager.record_fill(make_fill(bracket.entry, "tif-entry-fill", "10", "100"))

    assert [(order.venue_order_id, order.tif) for order in broker.submitted] == [
        (bracket.entry.order_id, TimeInForce.DAY),
        (bracket.stop.order_id, TimeInForce.GTC),
        (bracket.targets[0].order_id, TimeInForce.GTC),
        (bracket.targets[1].order_id, TimeInForce.GTC),
    ]


def test_option_swing_bracket_can_rest_its_entry_gtc(manager_factory):
    manager, _, broker = manager_factory()
    contract = OptionContract("AAPL", date(2026, 11, 20), Decimal("200"), OptionRight.CALL)
    intent = OrderIntent(
        intent_id="option-swing",
        account_id="account-1",
        instrument=contract,
        side=Side.BUY,
        quantity_rule="fixed_2",
        entry_price=Decimal("4.50"),
        stop_loss=Decimal("2.25"),
        profit_targets=(Decimal("9.00"),),
        reason="options swing",
        command_id="option-swing-command",
        entry_tif=TimeInForce.GTC,
    )
    bracket = manager.create_bracket(intent, Decimal("2"))

    assert {order.tif for order in (bracket.entry, bracket.stop, *bracket.targets)} == {
        TimeInForce.GTC
    }
    manager.submit(bracket.entry)
    assert broker.submitted[0].tif is TimeInForce.GTC


def test_bracket_is_refused_before_entry_when_venue_lacks_exit_tif(manager_factory):
    broker = FakeBroker(tifs=frozenset({TimeInForce.DAY}))
    manager, ledger, _ = manager_factory(broker)

    with pytest.raises(UnsupportedOrderCapabilityError, match="GTC"):
        manager.create_bracket(make_intent(), Decimal("10"))

    assert ledger.event_by_command("command-1") is None
    assert broker.submitted == []
    day_only = replace(make_intent(command_id="day-exits"), exit_tif=TimeInForce.DAY)
    assert manager.create_bracket(day_only, Decimal("10")).stop.tif is TimeInForce.DAY


def test_bracket_replay_with_different_tif_is_an_idempotency_conflict(manager_factory):
    manager, _, _ = manager_factory()
    manager.create_bracket(make_intent(), Decimal("10"))

    with pytest.raises(IdempotencyConflictError):
        manager.create_bracket(
            replace(make_intent(), exit_tif=TimeInForce.DAY), Decimal("10")
        )


# -- stop entries (breakout triggers) -------------------------------------------------


def test_intent_entry_type_defaults_to_limit_and_refuses_other_types():
    assert make_intent().entry_type is OrderType.LIMIT
    assert replace(make_intent(), entry_type=OrderType.STOP).entry_type is OrderType.STOP
    for refused in (OrderType.MARKET, OrderType.STOP_LIMIT, OrderType.TRAIL):
        with pytest.raises(ValueError, match="entry_type"):
            replace(make_intent(), entry_type=refused)


def test_stop_entry_bracket_rests_a_stop_at_the_trigger(manager_factory):
    manager, _, broker = manager_factory()
    intent = replace(make_intent(command_id="breakout"), entry_type=OrderType.STOP)
    bracket = manager.create_bracket(intent, Decimal("10"))

    assert bracket.entry.order_type is OrderType.STOP
    assert bracket.entry.stop_price == Decimal("100")
    assert bracket.entry.limit_price is None
    # The protective stop is the entry's child, never the (also STOP) entry itself.
    assert bracket.stop.order_id == "breakout:stop"
    assert bracket.stop.stop_price == Decimal("95")
    manager.submit(bracket.entry)
    assert broker.submitted[0].order_type is OrderType.STOP
    assert broker.submitted[0].stop_price == Decimal("100")
    manager.record_fill(make_fill(bracket.entry, "breakout-fill", "10", "100.20"))
    assert [order.venue_order_id for order in broker.submitted[1:]] == [
        "breakout:stop",
        "breakout:target:1",
        "breakout:target:2",
    ]
    assert manager.create_bracket(intent, Decimal("10")) == manager.create_bracket(
        intent, Decimal("10")
    )


def test_limit_bracket_fingerprint_is_unchanged_by_the_entry_type_field():
    import hashlib
    import json

    intent = make_intent()
    legacy = {
        "intent_id": intent.intent_id,
        "account_id": intent.account_id,
        "instrument": encode_payload(intent.instrument),
        "side": intent.side.value,
        "quantity_rule": intent.quantity_rule,
        "quantity": "10",
        "entry_price": str(intent.entry_price),
        "stop_loss": str(intent.stop_loss),
        "profit_targets": [str(value) for value in intent.profit_targets],
        "reason": intent.reason,
        "command_id": intent.command_id,
        "entry_tif": intent.entry_tif.value,
        "exit_tif": intent.exit_tif.value,
    }
    raw = json.dumps(legacy, sort_keys=True, separators=(",", ":"))
    assert OrderManager._bracket_fingerprint(intent, Decimal("10")) == hashlib.sha256(
        raw.encode("utf-8")
    ).hexdigest()


def test_bracket_replay_with_a_different_entry_type_is_an_idempotency_conflict(manager_factory):
    manager, _, _ = manager_factory()
    manager.create_bracket(make_intent(), Decimal("10"))

    with pytest.raises(IdempotencyConflictError):
        manager.create_bracket(replace(make_intent(), entry_type=OrderType.STOP), Decimal("10"))


def test_stop_entry_is_refused_before_persisting_without_native_stops(manager_factory):
    manager, ledger, broker = manager_factory(FakeBroker(native_stops=False))
    intent = replace(make_intent(command_id="emulated-entry"), entry_type=OrderType.STOP)

    with pytest.raises(UnsupportedOrderCapabilityError, match="stop entry"):
        manager.create_bracket(intent, Decimal("10"))

    assert ledger.event_by_command("emulated-entry") is None
    assert broker.submitted == []
    # A limit entry at the same venue still works; its protective stop is emulated.
    assert manager.create_bracket(make_intent(command_id="limit-ok"), Decimal("10"))


# -- target fractions: partial exits leave a runner on the stop -----------------------


def _fraction_intent(fractions, targets=(Decimal("110"),), command_id="partial"):
    return replace(
        make_intent(command_id=command_id, targets=targets), target_fractions=fractions
    )


def test_target_fractions_are_validated():
    assert make_intent().target_fractions is None
    assert _fraction_intent((Decimal("1"),)).target_fractions == (Decimal("1"),)
    with pytest.raises(ValueError, match="entries for"):
        _fraction_intent((Decimal("0.5"), Decimal("0.5")))
    for bad in (Decimal("0"), Decimal("-0.1"), Decimal("NaN"), 0.5):
        with pytest.raises(ValueError, match="finite positive"):
            _fraction_intent((bad,))
    with pytest.raises(ValueError, match="more than the whole position"):
        _fraction_intent(
            (Decimal("0.6"), Decimal("0.5")), targets=(Decimal("105"), Decimal("110"))
        )


def test_a_third_at_the_target_leaves_a_runner_on_the_stop(manager_factory):
    manager, _, broker = manager_factory()
    bracket = manager.create_bracket(_fraction_intent((Decimal("1") / 3,)), Decimal("10"))

    assert [target.quantity for target in bracket.targets] == [Decimal("3")]
    assert bracket.stop.quantity == Decimal("10")
    manager.submit(bracket.entry)
    manager.record_fill(make_fill(bracket.entry, "partial-entry", "10", "100"))
    assert [(item.venue_order_id, item.quantity) for item in broker.submitted[1:]] == [
        ("partial:stop", Decimal("10")),
        ("partial:target:1", Decimal("3")),
    ]

    manager.record_fill(make_fill(bracket.targets[0], "partial-target", "3", "110"))
    assert manager.get_order("partial:target:1").state is OrderState.FILLED
    stop = manager.get_order("partial:stop")
    assert stop.quantity == Decimal("7")
    assert stop.state is OrderState.ACCEPTED
    assert broker.cancelled == []


def test_whole_share_fractions_round_by_largest_remainder(manager_factory):
    manager, _, _ = manager_factory()
    halves = manager.create_bracket(
        _fraction_intent(
            (Decimal("0.5"), Decimal("0.5")), targets=(Decimal("105"), Decimal("110"))
        ),
        Decimal("5"),
    )
    assert [target.quantity for target in halves.targets] == [Decimal("3"), Decimal("2")]
    half_and_runner = manager.create_bracket(
        _fraction_intent((Decimal("0.5"),), command_id="half"), Decimal("5")
    )
    assert [target.quantity for target in half_and_runner.targets] == [Decimal("3")]
    with pytest.raises(ValueError, match="too small"):
        manager.create_bracket(
            _fraction_intent((Decimal("0.1"),), command_id="tiny"), Decimal("4")
        )


def test_partial_entry_fill_keeps_the_runners_share(manager_factory):
    manager, _, broker = manager_factory()
    bracket = manager.create_bracket(_fraction_intent((Decimal("1") / 3,)), Decimal("9"))
    manager.submit(bracket.entry)
    manager.record_fill(make_fill(bracket.entry, "part-entry", "6", "100"))
    manager.cancel(bracket.entry.order_id, command_id="cancel-rest")

    # Planned 3 of 9; of the 6 filled the target gets its third, not all six.
    target = next(item for item in broker.submitted if item.venue_order_id == "partial:target:1")
    assert target.quantity == Decimal("2")
    assert manager.get_order("partial:stop").quantity == Decimal("6")


def test_fractions_join_the_fingerprint_only_when_set(manager_factory):
    manager, _, _ = manager_factory()
    manager.create_bracket(make_intent(targets=(Decimal("110"),)), Decimal("10"))
    with pytest.raises(IdempotencyConflictError):
        manager.create_bracket(_fraction_intent((Decimal("1"),), command_id="command-1"), Decimal("10"))

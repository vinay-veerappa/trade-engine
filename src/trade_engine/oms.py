"""Order-management orchestration built on the immutable domain order model."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal

from trade_engine.domain.instruments import Side
from trade_engine.domain.orders import Order, OrderState, OrderType
from trade_engine.domain.signals import OrderIntent
from trade_engine.interfaces.broker import (
    BrokerAdapter,
    OrderChanges,
    VenueOrder,
    VenueOrderAllocation,
)
from trade_engine.interfaces.clock import Clock


@dataclass(frozen=True)
class Bracket:
    """Entry order and its protective/target children."""

    entry: Order
    stop: Order
    targets: tuple[Order, ...]

    @property
    def orders(self) -> tuple[Order, ...]:
        return (self.entry, self.stop, *self.targets)


class TrailingStopEmulator:
    """Evaluate a trailing stop when a venue has no native trailing orders."""

    def __init__(self, side: Side, trail_amount: Decimal) -> None:
        if trail_amount <= Decimal("0"):
            raise ValueError("trail_amount must be positive")
        self._side = side
        self._trail_amount = trail_amount
        self._extreme: Decimal | None = None
        self._stop: Decimal | None = None
        self._triggered = False

    @property
    def stop_price(self) -> Decimal | None:
        return self._stop

    @property
    def triggered(self) -> bool:
        return self._triggered

    def update(self, price: Decimal) -> bool:
        """Advance the path and return whether the stop is triggered."""
        if price <= Decimal("0"):
            raise ValueError("price must be positive")
        if self._triggered:
            return True
        if self._extreme is None:
            self._extreme = price
        elif self._side == Side.BUY:
            self._extreme = max(self._extreme, price)
        else:
            self._extreme = min(self._extreme, price)

        if self._side == Side.BUY:
            self._stop = self._extreme - self._trail_amount
            self._triggered = price <= self._stop
        else:
            self._stop = self._extreme + self._trail_amount
            self._triggered = price >= self._stop
        return self._triggered


class OrderManager:
    """Create brackets, route orders 1:1, and resolve linked orders."""

    def __init__(self, broker: BrokerAdapter, clock: Clock) -> None:
        self._broker = broker
        self._clock = clock
        self._commands: dict[str, Bracket] = {}
        self._orders: dict[str, Order] = {}
        self._venue_ids: dict[str, str] = {}

    def create_bracket(self, intent: OrderIntent, quantity: Decimal) -> Bracket:
        """Create a deterministic bracket; replaying a command is a no-op."""
        if quantity <= Decimal("0"):
            raise ValueError("quantity must be positive")
        existing = self._commands.get(intent.command_id)
        if existing is not None:
            return existing

        created_at = self._utc_now()
        prefix = intent.command_id
        entry = Order(
            order_id=f"{prefix}:entry",
            account_id=intent.account_id,
            instrument=intent.instrument,
            order_type=OrderType.LIMIT,
            side=intent.side,
            quantity=quantity,
            command_id=f"{prefix}:entry",
            created_at=created_at,
            limit_price=intent.entry_price,
        )
        exit_side = Side.SELL if intent.side == Side.BUY else Side.BUY
        stop = Order(
            order_id=f"{prefix}:stop",
            account_id=intent.account_id,
            instrument=intent.instrument,
            order_type=OrderType.STOP,
            side=exit_side,
            quantity=quantity,
            command_id=f"{prefix}:stop",
            created_at=created_at,
            stop_price=intent.stop_loss,
            parent_order_id=entry.order_id,
            oco_group=f"{prefix}:exits",
        )
        targets = tuple(
            Order(
                order_id=f"{prefix}:target:{index}",
                account_id=intent.account_id,
                instrument=intent.instrument,
                order_type=OrderType.LIMIT,
                side=exit_side,
                quantity=quantity,
                command_id=f"{prefix}:target:{index}",
                created_at=created_at,
                limit_price=price,
                parent_order_id=entry.order_id,
                oco_group=f"{prefix}:exits",
            )
            for index, price in enumerate(intent.profit_targets, start=1)
        )
        bracket = Bracket(entry, stop, targets)
        self._commands[intent.command_id] = bracket
        self._orders.update({order.order_id: order for order in bracket.orders})
        return bracket

    def submit(self, order: Order) -> Order:
        """Submit one strategy order as exactly one venue order."""
        if order.order_id in self._venue_ids:
            return self._orders[order.order_id]
        self._orders.setdefault(order.order_id, order)
        venue_order = VenueOrder(
            venue_order_id=order.order_id,
            instrument=order.instrument,
            order_type=order.order_type,
            side=order.side,
            quantity=order.quantity,
            submitted_at=self._utc_now(),
            tif=order.tif,
            limit_price=order.limit_price,
            stop_price=order.stop_price,
            trail_amount=order.trail_amount,
            allocations=(VenueOrderAllocation(order.order_id, order.account_id, order.quantity),),
        )
        ack = self._broker.submit(venue_order)
        self._venue_ids[order.order_id] = ack.venue_order_id
        submitted = self._orders[order.order_id].transition_to(OrderState.SUBMITTED)
        next_state = (
            OrderState.ACCEPTED
            if ack.status == "ACCEPTED"
            else OrderState.REJECTED
            if ack.status == "REJECTED"
            else OrderState.PENDING_UNKNOWN
        )
        self._orders[order.order_id] = submitted.transition_to(next_state)
        return self._orders[order.order_id]

    def submit_trailing(self, order: Order, prices: Iterable[Decimal]) -> bool:
        """Submit a native trail or emulate it from an explicit replay price path."""
        if order.order_type != OrderType.TRAIL:
            raise ValueError("submit_trailing requires a TRAIL order")
        if OrderType.TRAIL in self._broker.capabilities.supported_order_types:
            self.submit(order)
            return False

        emulator = TrailingStopEmulator(order.side, order.trail_amount or Decimal("0"))
        for price in prices:
            if emulator.update(price):
                stop = VenueOrder(
                    venue_order_id=f"{order.order_id}:emulated",
                    instrument=order.instrument,
                    order_type=OrderType.STOP,
                    side=order.side,
                    quantity=order.quantity,
                    submitted_at=self._utc_now(),
                    tif=order.tif,
                    stop_price=emulator.stop_price,
                    allocations=(VenueOrderAllocation(order.order_id, order.account_id, order.quantity),),
                )
                ack = self._broker.submit(stop)
                if ack.status == "REJECTED":
                    raise RuntimeError(f"Venue rejected emulated trail: {ack.message or 'no message'}")
                self._venue_ids[order.order_id] = ack.venue_order_id
                self._orders[order.order_id] = order.transition_to(OrderState.SUBMITTED).transition_to(
                    OrderState.ACCEPTED if ack.status == "ACCEPTED" else OrderState.PENDING_UNKNOWN
                )
                return True
        return False

    def resolve(self, order_id: str, filled: bool) -> tuple[Order, ...]:
        """Resolve an order and cancel all active siblings in its OCO group."""
        order = self._orders[order_id]
        if not filled:
            return (order,)
        resolved = order.transition_to(OrderState.FILLED)
        self._orders[order_id] = resolved
        cancelled: list[Order] = [resolved]
        if resolved.oco_group:
            for sibling in tuple(self._orders.values()):
                if sibling.oco_group == resolved.oco_group and sibling.order_id != order_id:
                    if sibling.state not in (OrderState.CANCELLED, OrderState.FILLED):
                        updated = sibling.transition_to(OrderState.CANCELLED)
                        self._orders[sibling.order_id] = updated
                        venue_id = self._venue_ids.get(sibling.order_id)
                        if venue_id is not None:
                            self._broker.cancel(venue_id)
                        cancelled.append(updated)
        return tuple(cancelled)

    def cancel(self, order_id: str) -> Order:
        """Cancel a working order through the venue and return its new state."""
        order = self._orders[order_id]
        venue_id = self._venue_ids[order_id]
        ack = self._broker.cancel(venue_id)
        if ack.status == "REJECTED":
            raise RuntimeError(f"Venue rejected cancel for {order_id}: {ack.message or 'no message'}")
        updated = order.transition_to(OrderState.CANCELLED)
        self._orders[order_id] = updated
        return updated

    def replace(self, order_id: str, changes: OrderChanges) -> Order:
        """Replace a working order while preserving immutable local state."""
        order = self._orders[order_id]
        venue_id = self._venue_ids[order_id]
        ack = self._broker.replace(venue_id, changes)
        if ack.status == "REJECTED":
            raise RuntimeError(f"Venue rejected replace for {order_id}: {ack.message or 'no message'}")
        updated = order
        if changes.new_quantity is not None:
            updated = replace(updated, quantity=changes.new_quantity)
        if changes.new_limit_price is not None:
            updated = replace(updated, limit_price=changes.new_limit_price)
        if changes.new_stop_price is not None:
            updated = replace(updated, stop_price=changes.new_stop_price)
        self._orders[order_id] = updated
        return updated

    def _utc_now(self) -> datetime:
        value = self._clock.now_utc()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Clock returned a naive datetime")
        return value

"""Event-sourced order management, bracket orchestration, and 1:1 routing.

The command flow is Rust (``te_core::oms::flow``, P3b-2b): every method is one
``trade_engine_rs.oms_flow`` call. ``_Host`` is the flow's host: each effect (the clock,
the ledger, the broker) is one call to it, made where the flow makes it; it decides
nothing. ``trade_engine_rs`` missing is an ImportError (D5).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, fields, is_dataclass, replace
from datetime import datetime
from decimal import Decimal
from enum import Enum

import trade_engine_rs as rs

from trade_engine.domain.instruments import Equity, Instrument, Side
from trade_engine.domain.orders import Order, OrderState, OrderType, TimeInForce
from trade_engine.domain.portfolio import Fill
from trade_engine.domain.signals import OrderIntent
from trade_engine.interfaces.broker import (
    BrokerAdapter,
    OrderChanges,
    VenueOrder,
    VenueOrderAllocation,
)
from trade_engine.interfaces.clock import Clock
from trade_engine.ledger import EmulatedOrderState, Event, EventKind, Ledger, codec
from trade_engine.ledger.codec import DecimalRangeError, canon_decimal, encode_payload
from trade_engine.oms.models import Bracket
from trade_engine.sim import _rs


class OrderManagementError(RuntimeError):
    """Base class for refused or unresolved OMS operations."""


class IdempotencyConflictError(OrderManagementError):
    """A persisted command key was replayed with a different payload."""


class UnsupportedOrderCapabilityError(OrderManagementError):
    """The venue cannot accept the requested order type or time in force."""


class OrderPendingReconciliationError(OrderManagementError):
    """An earlier venue result is ambiguous; resubmission is unsafe."""


class BrokerOutcomeUnknownError(OrderManagementError):
    """The venue call failed after the durable pending state was recorded."""


class OCOOutcomeUnknownError(OrderManagementError):
    """A sibling cancellation could not be confirmed by the venue."""


class OrderReconciliationError(OrderManagementError):
    """Venue read-back could not establish an order's state."""


for _kind, _cls in (
    ("order_management", OrderManagementError),
    ("idempotency", IdempotencyConflictError),
    ("unsupported_order", UnsupportedOrderCapabilityError),
    ("pending_reconciliation", OrderPendingReconciliationError),
    ("broker_unknown", BrokerOutcomeUnknownError),
    ("oco_unknown", OCOOutcomeUnknownError),
    ("order_reconciliation", OrderReconciliationError),
    ("stop_iteration", StopIteration),
    ("manager_int_overflow", OverflowError),
):
    _rs.register(_kind, _cls)


def _wire(value):
    """Exact boundary plumbing, not OMS decisions."""
    if isinstance(value, Decimal):
        try:
            return canon_decimal(value)  # P7: one spelling, so a fingerprint does not depend on padding
        except DecimalRangeError:
            return str(value)  # outside the bound: te_core refuses it
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, OrderIntent):
        return {item.name: _wire(getattr(value, item.name)) for item in fields(value)}
    if is_dataclass(value):
        return encode_payload(value)
    if isinstance(value, dict):
        return {key: _wire(item) for key, item in value.items()}
    if isinstance(value, (tuple, list, set, frozenset)):
        return [_wire(item) for item in value]
    return value


@dataclass(frozen=True)
class _OrderContext:
    order: Order
    filled: Decimal
    venue_order_id: str | None
    emulation: EmulatedOrderState | None


def _dec(value):
    return None if value is None else Decimal(value)


def _order(tree) -> Order:
    return codec.build(tree)


def _bracket(tree) -> Bracket:
    entry, stop, targets = tree
    return Bracket(_order(entry), _order(stop), tuple(_order(item) for item in targets))


def _changes(changes: OrderChanges):
    return {
        "new_quantity": _wire(changes.new_quantity),
        "new_limit_price": _wire(changes.new_limit_price),
        "new_stop_price": _wire(changes.new_stop_price),
    }


class _Host:
    """The flow's effects, one call each.

    The ledger methods (accounts, state, event_by_command, events_of_kind, append) serve only
    a ledger that is not a plain ``Ledger``; a plain ``Ledger``'s are native (``_flow``).
    """

    after_append = None
    """Test seam: a callable that receives the written event's bytes after the native append commits."""

    def __init__(self, manager: OrderManager, broker, clock, ledger, gate: "Precheck | None" = None) -> None:
        self._manager = manager
        self._broker = broker
        self._clock = clock
        self._ledger = ledger
        self._gate = gate

    def now(self):
        return self._clock.now_utc().isoformat()

    def accounts(self):
        return list(self._ledger.accounts())

    def state(self, account):
        # The store's Rust fold is read in place; any other ledger crosses as text.
        fold_handle = getattr(self._ledger, "fold_handle", None)
        if fold_handle is not None:
            return fold_handle(account)
        return codec.text(codec.canon(self._ledger.state(account)))

    def event_by_command(self, command_id):
        event = self._ledger.event_by_command(command_id)
        return None if event is None else codec.event_bytes(event)

    def events_of_kind(self, kind):
        return [codec.event_bytes(event) for event in self._ledger.events_of_kind(EventKind(kind))]

    def append(self, text):
        data = json.loads(text)
        event = Event(
            account=data["account"],
            kind=EventKind(data["kind"]),
            payload=codec.build(data["payload"]),
            ts_utc=datetime.fromisoformat(data["ts_utc"]),
            command_id=data["command_id"],
        )
        return codec.event_bytes(self._ledger.append(event))

    def capabilities(self):
        capabilities = self._broker.capabilities
        return (
            [item.value for item in capabilities.supported_order_types],
            [item.value for item in capabilities.supported_tifs],
            capabilities.supports_native_stops,
        )

    def env(self):
        return self._broker.env

    def _bracket_prices(self, data):
        """OA-3b: the stop and first target of a bracket entry, from the folded state.

        The Rust flow links a child to its entry by ``Order.parent_order_id`` (flow.rs create_bracket);
        ``manager.rs bracket`` picks the STOP child and the LIMIT children sorted by order id.
        """
        orders = self._ledger.state(data["allocations"][0]["account_id"]).orders
        children = [item for item in orders.values()
                    if item.parent_order_id == data["venue_order_id"] and item.state is OrderState.NEW]
        stops = [item for item in children if item.order_type is OrderType.STOP]
        targets = sorted((item for item in children if item.order_type is OrderType.LIMIT),
                         key=lambda item: item.order_id)
        return (stops[0].stop_price if stops else None, targets[0].limit_price if targets else None)

    def venue_order(self, text):
        data = json.loads(text)
        stop_price = target_price = None
        # tests/flow_p3b2b.py calls this unbound with self=None to compare plain conversion
        if (self is not None and data["parent_order_id"] is None and data["allocations"]
                and getattr(self._broker, "atomic_bracket", False)):
            stop_price, target_price = self._bracket_prices(data)
        return VenueOrder(
            venue_order_id=data["venue_order_id"],
            instrument=codec.build(data["instrument"]),
            order_type=OrderType(data["order_type"]),
            side=Side(data["side"]),
            quantity=Decimal(data["quantity"]),
            submitted_at=datetime.fromisoformat(data["submitted_at"]),
            tif=TimeInForce(data["tif"]),
            limit_price=_dec(data["limit_price"]),
            stop_price=_dec(data["stop_price"]),
            trail_amount=_dec(data["trail_amount"]),
            allocations=tuple(
                VenueOrderAllocation(item["strategy_order_id"], item["account_id"], Decimal(item["quantity"]))
                for item in data["allocations"]
            ),
            parent_order_id=data["parent_order_id"],
            oco_group=data["oco_group"],
            protective_stop_price=stop_price,
            protective_target_price=target_price,
        )

    @staticmethod
    def _ack(ack):
        return ack.venue_order_id, ack.status, ack.message

    def submit(self, venue_order):
        if self._gate is not None:
            return self._ack(self._gate.submit(venue_order, self._broker.submit))
        return self._ack(self._broker.submit(venue_order))

    def cancel(self, venue_order_id):
        return self._ack(self._broker.cancel(venue_order_id))

    def replace(self, venue_order_id, text):
        changes = {key: _dec(value) for key, value in json.loads(text).items()}
        if self._gate is not None:
            return self._ack(self._gate.replace(venue_order_id, OrderChanges(**changes), self._broker.replace))
        return self._ack(self._broker.replace(venue_order_id, OrderChanges(**changes)))

    def orders(self, since):
        return [
            (item.venue_order_id, item.state.value, str(item.filled_quantity),
             str(item.remaining_quantity), item.updated_at.isoformat())
            for item in self._broker.orders(datetime.fromisoformat(since))
        ]

    def fills(self, since):
        return [
            (item.venue_fill_id, item.venue_order_id, codec.text(codec.encode_payload(item.instrument)),
             str(item.quantity), str(item.price), item.filled_at.isoformat(), item.side.value,
             str(item.fee), item.leg_id)
            for item in self._broker.fills(datetime.fromisoformat(since))
        ]

    def send_reduce(self, text, command_id):
        """``reduce_bracket`` sends its reduce through ``OrderManager._send_reduce``."""
        return codec.text(_wire(self._manager._send_reduce(_order(json.loads(text)), command_id)))

    @staticmethod
    def refusal(kind, message):
        return _rs.refusal(kind, message)


class OrderManager(rs.OrderManagerBase):
    """Manage orders using the E1 ledger as the sole source of local state."""

    def _read_back_reconciled(self, report):
        """Read back every PENDING verdict the adapter just resolved (OA-5).

        The adapter calls this from the tail of ``reconcile`` with its report; each id in
        ``accepted`` then ``rejected`` is read back through the OMS so the ledger leaves
        PENDING_UNKNOWN on the same read. One bad id must not strand the rest: the failure is
        logged and the loop continues.
        """
        logger = logging.getLogger("trade_engine.oms.manager")
        for order_id in report.accepted + report.rejected:
            try:
                self.reconcile_order(order_id)
            except Exception as exc:
                logger.error(
                    "reconcile read-back failed for %s (%s)",
                    order_id, type(exc).__name__,
                )

    # The pure decisions stay the P3b-2a shims over ``te_core::oms::manager``.
    @staticmethod
    def _is_reduce(order: Order, entry_order_id: str) -> bool:
        return _rs.call(_rs.rs.oms_manager_is_reduce, entry_order_id, order.order_id)


    @staticmethod
    def _replacement_terms(order: Order, changes: OrderChanges, filled: Decimal):
        _rs.call(_rs.rs.oms_manager_replace_quantity, changes.new_quantity, filled,
                 isinstance(order.instrument, Equity))
        quantity, limit, stop, noop = _rs.call(
            _rs.rs.oms_manager_replace_terms, order, changes.new_quantity,
            changes.new_limit_price, changes.new_stop_price,
        )
        return replace(order, quantity=quantity,
                       limit_price=limit,
                       stop_price=stop), noop


    @staticmethod
    def _bracket_fingerprint(intent: OrderIntent, quantity: Decimal) -> str:
        return _rs.call(_rs.rs.oms_manager_bracket_fingerprint,
                        codec.text(_wire({"intent": intent, "quantity": quantity})))

    @staticmethod
    def _reduce_fingerprint(entry_order_id: str, fraction: Decimal, reason: str) -> str:
        # The command, not the order: its size depends on the open quantity at the time.
        return _rs.call(_rs.rs.oms_manager_reduce_fingerprint, entry_order_id, fraction, reason)

    @staticmethod
    def _fingerprint_order(order: Order) -> str:
        return _rs.call(_rs.rs.oms_manager_fingerprint,
                        codec.text(_wire({"payload": encode_payload(order)})))

    @classmethod
    def _split_quantity(
        cls, quantity: Decimal, count: int, instrument: Instrument
    ) -> tuple[Decimal, ...]:
        return tuple(_rs.call(_rs.rs.oms_manager_split, quantity,
                              [Decimal("1")] * count, isinstance(instrument, Equity)))

    @classmethod
    def _fraction_quantities(
        cls, quantity: Decimal, fractions: tuple[Decimal, ...], instrument: Instrument
    ) -> tuple[Decimal, ...]:
        return tuple(_rs.call(_rs.rs.oms_manager_fractions, quantity,
                              fractions, isinstance(instrument, Equity)))

    @staticmethod
    def _allocate_quantity(
        quantity: Decimal, weights: tuple[Decimal, ...], instrument: Instrument
    ) -> tuple[Decimal, ...]:
        return tuple(_rs.call(_rs.rs.oms_manager_allocate, quantity,
                              weights, isinstance(instrument, Equity)))

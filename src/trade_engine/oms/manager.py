"""Event-sourced order management, bracket orchestration, and 1:1 routing.

The command flow is Rust (``te_core::oms::flow``, P3b-2b): every method is one
``trade_engine_rs.oms_flow`` call. ``_Host`` is the flow's host: each effect (the clock,
the ledger, the broker) is one call to it, made where the flow makes it; it decides
nothing. ``trade_engine_rs`` missing is an ImportError (D5).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, fields, is_dataclass, replace
from datetime import datetime
from decimal import Decimal
from enum import Enum

from trade_engine.domain.instruments import Equity, Instrument, Side
from trade_engine.domain.orders import Order, OrderType, TimeInForce
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
from trade_engine.ledger.codec import encode_payload
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
        return str(value)
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


def _plan(operation: str, **request):
    return json.loads(_rs.call(_rs.rs.oms_manager_decide, operation, codec.text(_wire(request))))


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
    """The flow's effects, one call each."""

    def __init__(self, manager: OrderManager, broker, clock, ledger) -> None:
        self._manager = manager
        self._broker = broker
        self._clock = clock
        self._ledger = ledger

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

    def venue_order(self, text):
        data = json.loads(text)
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
        )

    @staticmethod
    def _ack(ack):
        return ack.venue_order_id, ack.status, ack.message

    def submit(self, venue_order):
        return self._ack(self._broker.submit(venue_order))

    def cancel(self, venue_order_id):
        return self._ack(self._broker.cancel(venue_order_id))

    def replace(self, venue_order_id, text):
        changes = {key: _dec(value) for key, value in json.loads(text).items()}
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


class OrderManager:
    """Manage orders using the E1 ledger as the sole source of local state."""

    def __init__(self, broker: BrokerAdapter, clock: Clock, ledger: Ledger) -> None:
        self._broker = broker
        self._clock = clock
        self._ledger = ledger
        self._host = _Host(self, broker, clock, ledger)

    def _flow(self, operation: str, **request):
        try:
            out = _rs.rs.oms_flow(operation, self._host, codec.text(_wire(request)))
        except ValueError as err:
            if type(err) is ValueError and len(err.args) == 2 and all(isinstance(a, str) for a in err.args):
                raise _rs.refusal(*err.args) from err.__cause__
            raise
        return json.loads(out)

    def create_bracket(self, intent: OrderIntent, quantity: Decimal) -> Bracket:
        """Persist a deterministic bracket; conflicting command replays are refused."""
        return _bracket(self._flow("create_bracket", intent=intent, quantity=quantity))

    def get_order(self, order_id: str) -> Order:
        return _order(self._flow("get_order", order_id=order_id))

    def submit(self, order: Order) -> Order:
        """Submit one native venue order, recording ambiguity before network I/O."""
        return _order(self._flow("submit", order=order))

    def submit_trailing(self, order: Order) -> Order:
        """Start a native venue trail or persist a live emulated trail locally."""
        return _order(self._flow("submit_trailing", order=order))

    def update_trailing(
        self, order_id: str, price: Decimal, *, command_id: str
    ) -> Order:
        """Apply one live price observation to a persisted emulated trailing stop."""
        return _order(self._flow("update_trailing", order_id=order_id, price=price, command_id=command_id))

    def update_emulated_order(
        self, order_id: str, price: Decimal, *, command_id: str
    ) -> Order:
        """Evaluate one observed price against a persisted emulated order."""
        return _order(self._flow("update_emulated_order", order_id=order_id, price=price,
                                 command_id=command_id))

    def record_fill(self, fill: Fill) -> Order:
        """Persist a venue fill and synchronize bracket protection from folded state."""
        return _order(self._flow("record_fill", fill=fill))

    def cancel(self, order_id: str, *, command_id: str) -> Order:
        """Cancel a working order; only a confirmed venue ack becomes CANCELLED."""
        return _order(self._flow("cancel", order_id=order_id, command_id=command_id))

    def move_stop(self, entry_order_id: str, stop_price: Decimal, *, command_id: str) -> Order:
        """Tighten an open bracket's protective stop; loosening it refuses (I5)."""
        return _order(self._flow("move_stop", entry=entry_order_id, stop_price=stop_price,
                                 command_id=command_id))

    def close_bracket(self, entry_order_id: str, *, command_id: str, reason: str) -> Order:
        """Exit an open bracket's whole open quantity with a DAY market order.

        Entered after the close, it works the next session's open. The protective stop
        stays live until the close order fills; the fill then cancels the remaining exits.
        """
        return _order(self._flow("close_bracket", entry=entry_order_id, command_id=command_id, reason=reason))

    def reduce_bracket(
        self, entry_order_id: str, fraction: Decimal, *, command_id: str, reason: str
    ) -> Order:
        """Exit ``fraction`` of an open bracket, rounded down, with a DAY market order.

        The reduce replaces the resting profit targets and cancels them: the partial is
        taken at the target or after N days, whichever comes first. The protective stop
        stays live; once the reduce fills, it shrinks to the remaining open quantity.
        """
        return _order(self._flow("reduce_bracket", entry=entry_order_id, fraction=fraction,
                                 command_id=command_id, reason=reason))

    def replace(
        self, order_id: str, changes: OrderChanges, *, command_id: str
    ) -> Order:
        """Replace a live order; pending/unknown acknowledgements remain unknown."""
        return _order(self._flow("replace", order_id=order_id, changes=_changes(changes),
                                 command_id=command_id))

    def reconcile_order(self, order_id: str) -> Order:
        """Read back an ambiguous venue order; never resend it."""
        return _order(self._flow("reconcile_order", order_id=order_id))

    def _send_reduce(self, reduce: Order, command_id: str) -> Order:
        """Cancel the resting profit targets, then submit the reduce."""
        return _order(self._flow("send_reduce", order=reduce, command_id=command_id))

    def _bracket_children(self, entry_order_id: str) -> list[Order]:
        return [_order(item) for item in self._flow("bracket_children", entry=entry_order_id)]

    def _open_bracket_stop(self, entry_order_id: str) -> tuple[Order, Decimal]:
        stop, open_quantity = self._flow("open_bracket_stop", entry=entry_order_id)
        return _order(stop), Decimal(open_quantity)

    # The pure decisions stay the P3b-2a shims over ``te_core::oms::manager``.
    @staticmethod
    def _is_reduce(order: Order, entry_order_id: str) -> bool:
        return _plan("is_reduce", entry=entry_order_id, order_id=order.order_id)


    @staticmethod
    def _replacement_terms(order: Order, changes: OrderChanges, filled: Decimal):
        _plan("replace_quantity", quantity=changes.new_quantity, filled=filled,
              equity=isinstance(order.instrument, Equity))
        quantity, limit, stop, noop = _plan(
            "replace_terms", order=order, quantity=changes.new_quantity,
            limit=changes.new_limit_price, stop=changes.new_stop_price,
        )
        return replace(order, quantity=Decimal(quantity),
                       limit_price=None if limit is None else Decimal(limit),
                       stop_price=None if stop is None else Decimal(stop)), noop


    @staticmethod
    def _bracket_fingerprint(intent: OrderIntent, quantity: Decimal) -> str:
        return _plan("bracket_fingerprint", intent=intent, quantity=quantity)

    @staticmethod
    def _reduce_fingerprint(entry_order_id: str, fraction: Decimal, reason: str) -> str:
        # The command, not the order: its size depends on the open quantity at the time.
        return _plan("reduce_fingerprint", entry=entry_order_id, fraction=fraction, reason=reason)

    @staticmethod
    def _fingerprint_order(order: Order) -> str:
        return _plan("fingerprint", payload=encode_payload(order))

    @staticmethod
    def _validate_quantity(instrument: Instrument, quantity: Decimal) -> None:
        _plan("quantity", equity=isinstance(instrument, Equity), quantity=quantity)

    @classmethod
    def _split_quantity(
        cls, quantity: Decimal, count: int, instrument: Instrument
    ) -> tuple[Decimal, ...]:
        return tuple(map(Decimal, _plan("split", quantity=quantity,
                         equity=isinstance(instrument, Equity), weights=[Decimal("1")] * count)))

    @classmethod
    def _fraction_quantities(
        cls, quantity: Decimal, fractions: tuple[Decimal, ...], instrument: Instrument
    ) -> tuple[Decimal, ...]:
        return tuple(map(Decimal, _plan("fractions", quantity=quantity,
                         equity=isinstance(instrument, Equity), weights=fractions)))

    @staticmethod
    def _allocate_quantity(
        quantity: Decimal, weights: tuple[Decimal, ...], instrument: Instrument
    ) -> tuple[Decimal, ...]:
        return tuple(map(Decimal, _plan("allocate", quantity=quantity,
                         equity=isinstance(instrument, Equity), weights=weights)))

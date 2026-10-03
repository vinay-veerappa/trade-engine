"""Event-sourced order management, bracket orchestration, and 1:1 routing."""

from __future__ import annotations

import json
from dataclasses import dataclass, fields, is_dataclass, replace
from datetime import datetime
from decimal import Decimal
from enum import Enum

from trade_engine.domain.instruments import Equity, Instrument, Side
from trade_engine.domain.orders import Order, OrderState, OrderType, TimeInForce
from trade_engine.domain.portfolio import Fill
from trade_engine.domain.signals import OrderIntent
from trade_engine.interfaces.broker import (
    BrokerAdapter,
    OrderChanges,
    VenueAck,
    VenueOrder,
    VenueOrderAllocation,
    VenueOrderState,
)
from trade_engine.interfaces.clock import Clock
from trade_engine.ledger import (
    EmulatedOrderState,
    Event,
    EventKind,
    Ledger,
    OrderStateChange,
    OrderUpdated,
    OrdersCreated,
)
from trade_engine.ledger import codec
from trade_engine.ledger.codec import encode_payload
from trade_engine.oms.models import Bracket
from trade_engine.oms.trailing import TrailingStopEmulator
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


def _event(event: Event):
    return dict(account=event.account, kind=event.kind.name, payload=event.payload)


@dataclass(frozen=True)
class _OrderContext:
    order: Order
    filled: Decimal
    venue_order_id: str | None
    emulation: EmulatedOrderState | None


class OrderManager:
    """Manage orders using the E1 ledger as the sole source of local state."""

    def __init__(self, broker: BrokerAdapter, clock: Clock, ledger: Ledger) -> None:
        self._broker = broker
        self._clock = clock
        self._ledger = ledger

    def create_bracket(self, intent: OrderIntent, quantity: Decimal) -> Bracket:
        """Persist a deterministic bracket; conflicting command replays are refused."""
        _plan("positive_quantity", quantity=quantity)
        _plan("bracket_capabilities", type=intent.entry_type, entry_tif=intent.entry_tif,
              exit_tif=intent.exit_tif, **self._capabilities())
        fingerprint = self._bracket_fingerprint(intent, quantity)
        existing = self._ledger.event_by_command(intent.command_id)
        if existing is not None:
            _plan("created_replay", existing=_event(existing), account=intent.account_id,
                  fingerprint=fingerprint, command=intent.command_id)
            return self._bracket_from_orders(existing.payload.orders)

        now = self._utc_now()
        prefix = intent.command_id
        limit, stop_price, exit_side = _plan("entry_terms", type=intent.entry_type,
                                           side=intent.side, price=intent.entry_price,
                                           limit=intent.entry_limit_price)
        entry = Order(
            order_id=f"{prefix}:entry",
            account_id=intent.account_id,
            instrument=intent.instrument,
            order_type=intent.entry_type,
            side=intent.side,
            quantity=quantity,
            command_id=f"{prefix}:entry",
            created_at=now,
            limit_price=None if limit is None else Decimal(limit),
            stop_price=None if stop_price is None else Decimal(stop_price),
            tif=intent.entry_tif,
        )
        exit_side = Side(exit_side)
        stop = Order(
            order_id=f"{prefix}:stop",
            account_id=intent.account_id,
            instrument=intent.instrument,
            order_type=OrderType.STOP,
            side=exit_side,
            quantity=quantity,
            command_id=f"{prefix}:stop",
            created_at=now,
            stop_price=intent.stop_loss,
            tif=intent.exit_tif,
            parent_order_id=entry.order_id,
            oco_group=f"{prefix}:exits",
        )
        self._validate_quantity(intent.instrument, quantity)
        if _plan("fraction_mode", fractions=intent.target_fractions) == "split":
            target_quantities = self._split_quantity(
                quantity, len(intent.profit_targets), intent.instrument
            )
        else:
            target_quantities = self._fraction_quantities(
                quantity, intent.target_fractions, intent.instrument
            )
        targets = tuple(
            Order(
                order_id=f"{prefix}:target:{index}",
                account_id=intent.account_id,
                instrument=intent.instrument,
                order_type=OrderType.LIMIT,
                side=exit_side,
                quantity=target_quantity,
                command_id=f"{prefix}:target:{index}",
                created_at=now,
                limit_price=price,
                tif=intent.exit_tif,
                parent_order_id=entry.order_id,
                oco_group=f"{prefix}:exits",
            )
            for index, (price, target_quantity) in enumerate(
                zip(intent.profit_targets, target_quantities, strict=True), start=1
            )
        )
        batch = OrdersCreated(
            orders=(entry, stop, *targets),
            fingerprint=fingerprint,
            reason=f"Bracket created: {intent.reason}",
        )
        self._append(intent.account_id, EventKind.ORDERS_CREATED, batch, intent.command_id)
        return self._bracket_from_orders(batch.orders)

    def get_order(self, order_id: str) -> Order:
        return self._context(order_id).order

    def submit(self, order: Order) -> Order:
        """Submit one native venue order, recording ambiguity before network I/O."""
        self._ensure_stored(order)
        context = self._context(order.order_id)
        current = context.order
        parent_id = _plan("parent", parent=current.parent_order_id)
        if parent_id is not None:
            parent = self._context(parent_id)
            if _plan("child_hold", filled=self._account_state(current.account_id).filled_quantity.get(
                parent.order.order_id, Decimal("0")
            )):
                self._planned_refusal(current, "child", parent=parent.order.order_id)
        if _plan("submit_mode", type=current.order_type, **self._capabilities()) == "emulate":
            return self._start_emulation(current)
        return self._submit_native(current)

    def submit_trailing(self, order: Order) -> Order:
        """Start a native venue trail or persist a live emulated trail locally."""
        _plan("trailing_check", type=order.order_type, method="submit_trailing")
        return self.submit(order)

    def update_trailing(
        self, order_id: str, price: Decimal, *, command_id: str
    ) -> Order:
        """Apply one live price observation to a persisted emulated trailing stop."""
        order = self.get_order(order_id)
        _plan("trailing_check", type=order.order_type, method="update_trailing", order=order_id,
              types=self._broker.capabilities.supported_order_types)
        return self.update_emulated_order(order_id, price, command_id=command_id)

    def update_emulated_order(
        self, order_id: str, price: Decimal, *, command_id: str
    ) -> Order:
        """Evaluate one observed price against a persisted emulated order."""
        _plan("price", price=price)
        context = self._context(order_id)
        order = context.order
        emulation = context.emulation
        _plan("emulation_check", order=order_id, type=order.order_type,
              emulated=emulation is not None, **self._capabilities())
        prior = self._ledger.event_by_command(command_id)
        if prior is not None:
            _plan("observation_replay", existing=_event(prior), account=order.account_id,
                  order=order_id, price=price, command=command_id)
            current = self.get_order(order_id)
            action = _plan("observed_action", triggered=prior.payload.triggered,
                           state=current.state, order=order_id)
            if action == "route":
                return self._route_emulated_trigger(order, prior.payload.observed_price, command_id)
            return current
        action = _plan("observed_action", triggered=emulation.triggered,
                       state=order.state, order=order_id)
        if action == "route":
            return self._route_emulated_trigger(order, emulation.observed_price, command_id)
        if action == "return":
            return order
        if _plan("type_is", type=order.order_type, expected=OrderType.TRAIL):
            emulator = TrailingStopEmulator(
                order.side,
                order.trail_amount or Decimal("0"),
                extreme=emulation.extreme,
                stop_price=emulation.stop_price,
                triggered=emulation.triggered,
            )
            triggered = emulator.update(price)
            extreme = emulator.extreme
            stop_price = emulator.stop_price
        else:
            stop, triggered = _plan("stop_observation", order=order, price=price)
            stop_price = Decimal(stop)
            extreme = None
        new_emulation = EmulatedOrderState(
            order_id=order_id,
            observed_price=price,
            extreme=extreme,
            stop_price=stop_price,
            triggered=triggered,
            reason=_plan("observation_reason", type=order.order_type, price=price, triggered=triggered),
        )
        self._append(
            order.account_id,
            EventKind.ORDER_EMULATION_UPDATED,
            new_emulation,
            command_id,
        )
        if triggered:
            return self._route_emulated_trigger(order, price, command_id)
        return self.get_order(order_id)

    def _route_emulated_trigger(
        self, order: Order, trigger_price: Decimal | None, command_id: str
    ) -> Order:
        if _plan("trigger_price", order=order, price=trigger_price):
            venue_type = OrderType.LIMIT
            limit_price = order.limit_price
        else:
            venue_type = self._trigger_order_type(order)
            limit = _plan("route_limit", type=venue_type, price=trigger_price)
            limit_price = None if limit is None else Decimal(limit)
        self._require_tif(order, venue_type)
        self._cancel_emulated_siblings(order, command_id)
        return self._submit_emulated(
            order,
            trigger_price,
            command_id,
            venue_type=venue_type,
            limit_price=limit_price,
        )

    def _cancel_emulated_siblings(self, order: Order, command_id: str) -> None:
        state = self._account_state(order.account_id)
        ids = _plan("siblings", entry=order.parent_order_id or "", order=order,
                    orders=list(state.orders.values()))
        siblings = [state.orders[item] for item in ids]
        self._cancel_exits(siblings, f"{command_id}:{order.order_id}:trigger")

    def record_fill(self, fill: Fill) -> Order:
        """Persist a venue fill and synchronize bracket protection from folded state."""
        context = self._context(fill.order_id)
        order = context.order
        _plan("fill_match", fill=fill.fill_id, order=order.order_id, account=order.account_id,
              fill_account=fill.account_id, env=self._broker.env, fill_env=fill.venue_env)
        self._validate_quantity(order.instrument, fill.quantity)
        self._append(order.account_id, EventKind.FILL, fill, f"fill:{fill.fill_id}")
        self._synchronize_bracket(order, fill.fill_id)
        return self.get_order(order.order_id)

    def cancel(self, order_id: str, *, command_id: str) -> Order:
        """Cancel a working order; only a confirmed venue ack becomes CANCELLED."""
        order = self.get_order(order_id)
        if _plan("protective_child", parent=order.parent_order_id, type=order.order_type):
            state = self._account_state(order.account_id)
            if _plan("cancel_protective", state=codec.canon(state),
                     entry=self.get_order(order.parent_order_id), order=order):
                self._planned_refusal(order, "protective", command_id=command_id)
        result = self._cancel_order(order, command_id, "Cancelled by OMS command")
        if _plan("parent", parent=order.parent_order_id) is None:
            self._synchronize_bracket(order, command_id)
        return result

    def move_stop(self, entry_order_id: str, stop_price: Decimal, *, command_id: str) -> Order:
        """Tighten an open bracket's protective stop; loosening it refuses (I5)."""
        stop, open_quantity = self._open_bracket_stop(entry_order_id)
        if _plan("move_stop", order=stop, price=stop_price) == "return":
            return stop
        return self.replace(
            stop.order_id, OrderChanges(new_stop_price=stop_price), command_id=command_id
        )

    def close_bracket(self, entry_order_id: str, *, command_id: str, reason: str) -> Order:
        """Exit an open bracket's whole open quantity with a DAY market order.

        Entered after the close, it works the next session's open. The protective stop
        stays live until the close order fills; the fill then cancels the remaining exits.
        """
        order_id = f"{entry_order_id}:close"
        try:
            existing = self.get_order(order_id)
        except KeyError:
            existing = None
        if existing is not None and _plan("close_replay", order=existing,
                                         entry=entry_order_id, command=command_id):
            return existing
        stop, open_quantity = self._open_bracket_stop(entry_order_id)
        if existing is None:
            _plan("close_guard", entry=entry_order_id, children=self._bracket_children(entry_order_id))
        close = existing or Order(
            order_id=order_id,
            account_id=stop.account_id,
            instrument=stop.instrument,
            order_type=OrderType.MARKET,
            side=stop.side,
            quantity=open_quantity,
            command_id=command_id,
            created_at=self._utc_now(),
            tif=TimeInForce.DAY,
            parent_order_id=entry_order_id,
            oco_group=stop.oco_group,
        )
        if existing is None:
            self._append(
                close.account_id,
                EventKind.ORDERS_CREATED,
                OrdersCreated(
                    orders=(close,),
                    fingerprint=self._fingerprint_order(close),
                    reason=f"Bracket close: {reason}",
                ),
                command_id,
            )
        return self.submit(close)

    def reduce_bracket(
        self, entry_order_id: str, fraction: Decimal, *, command_id: str, reason: str
    ) -> Order:
        """Exit ``fraction`` of an open bracket, rounded down, with a DAY market order.

        The reduce replaces the resting profit targets and cancels them: the partial is
        taken at the target or after N days, whichever comes first. The protective stop
        stays live; once the reduce fills, it shrinks to the remaining open quantity.
        """
        _plan("fraction", fraction=fraction)
        entry = self.get_order(entry_order_id)
        fingerprint = self._reduce_fingerprint(entry_order_id, fraction, reason)
        existing = self._ledger.event_by_command(command_id)
        if existing is not None:
            _plan("created_replay", existing=_event(existing), account=entry.account_id,
                  fingerprint=fingerprint, command=command_id)
            # Resumes a reduce persisted before a crash; once sent (or resolved), cancelling
            # the targets and submitting are both no-ops, so a replay changes nothing (I3).
            return self._send_reduce(
                self.get_order(existing.payload.orders[0].order_id), command_id
            )
        stop, open_quantity = self._open_bracket_stop(entry_order_id)
        quantity, reduce_id = _plan("reduce", entry=entry_order_id, fraction=fraction,
                                   open=open_quantity, children=self._bracket_children(entry_order_id))
        reduce = Order(
            order_id=reduce_id,
            account_id=stop.account_id,
            instrument=stop.instrument,
            order_type=OrderType.MARKET,
            side=stop.side,
            quantity=Decimal(quantity),
            command_id=command_id,
            created_at=self._utc_now(),
            tif=TimeInForce.DAY,
            parent_order_id=entry_order_id,
            oco_group=stop.oco_group,
        )
        self._append(
            reduce.account_id,
            EventKind.ORDERS_CREATED,
            OrdersCreated(
                orders=(reduce,),
                fingerprint=fingerprint,
                reason=f"Bracket reduce: {reason}",
            ),
            command_id,
        )
        return self._send_reduce(reduce, command_id)

    def _send_reduce(self, reduce: Order, command_id: str) -> Order:
        # Cancelled before the reduce goes out, so the targets cannot also fill against it.
        children = self._bracket_children(reduce.parent_order_id)
        ids = _plan("filter_types", orders=children, types=[OrderType.LIMIT])
        by_id = {order.order_id: order for order in children}
        targets = [by_id[item] for item in ids]
        self._cancel_exits(targets, f"{command_id}:replaces-targets")
        return self.submit(reduce)

    def _bracket_children(self, entry_order_id: str) -> list[Order]:
        state = self._account_state(self.get_order(entry_order_id).account_id)
        return [state.orders[item] for item in _plan("children", entry=entry_order_id,
                                                    orders=list(state.orders.values()))]

    @staticmethod
    def _is_reduce(order: Order, entry_order_id: str) -> bool:
        return _plan("is_reduce", entry=entry_order_id, order_id=order.order_id)

    def _open_bracket_stop(self, entry_order_id: str) -> tuple[Order, Decimal]:
        entry = self.get_order(entry_order_id)
        state = self._account_state(entry.account_id)
        stop_id, open_quantity = _plan("open_stop", entry=entry, state=codec.canon(state))
        return state.orders[stop_id], Decimal(open_quantity)

    def replace(
        self, order_id: str, changes: OrderChanges, *, command_id: str
    ) -> Order:
        """Replace a live order; pending/unknown acknowledgements remain unknown."""
        order = self.get_order(order_id)
        context = self._context(order_id)
        if _plan("local_replace", order=order, emulated=context.emulation is not None,
                 triggered=False if context.emulation is None else context.emulation.triggered):
            return self._replace_emulated_stop(order, changes, command_id)
        request_reason = f"Replace pending: {changes!r}"
        pending_command_id = f"{command_id}:pending"
        prior_request = self._ledger.event_by_command(pending_command_id)
        prior_noop = self._ledger.event_by_command(f"{command_id}:noop")
        if prior_request is None and prior_noop is not None:
            expected_reason = f"No-op replace: {changes!r}"
            _plan("replace_replay", existing=_event(prior_noop), mode="noop", order=order_id,
                  account=order.account_id, reason=expected_reason, command=command_id)
            return order
        if prior_request is not None:
            _plan("replace_replay", existing=_event(prior_request), mode="pending", order=order_id,
                  account=order.account_id, reason=request_reason, command=command_id,
                  state=order.state)
            return order
        filled = context.filled
        _plan("replace_state", order=order)
        updated, noop = self._replacement_terms(order, changes, filled)
        if noop:
            self._append(
                order.account_id,
                EventKind.ORDER_UPDATED,
                OrderUpdated(order, f"No-op replace: {changes!r}"),
                f"{command_id}:noop",
            )
            return order

        previous_state = order.state
        venue_id = context.venue_order_id
        _plan("confirmed_id", venue=venue_id, order=order_id, mode="replace")
        self._append(
            order.account_id,
            EventKind.ORDER_PENDING,
            OrderStateChange(
                order_id,
                request_reason,
                context.venue_order_id,
            ),
            pending_command_id,
        )
        try:
            ack = self._broker.replace(venue_id, changes)
        except Exception as err:
            raise BrokerOutcomeUnknownError(
                f"Replace outcome for order '{order_id}' is unknown; reconciliation is required"
            ) from err
        action, ack_reason, refused = _plan("replace_ack", status=ack.status,
                                          message=ack.message or "", order=order_id,
                                          command=command_id)
        if action == "pending":
            self._append(
                order.account_id,
                EventKind.ORDER_PENDING,
                OrderStateChange(
                    order_id,
                    ack_reason,
                    ack.venue_order_id,
                ),
                f"{command_id}:venue-pending",
            )
            return self.get_order(order_id)
        if action == "rejected":
            self._refuse(
                order,
                ack_reason,
                f"{command_id}:refused",
            )
            self._restore_working_state(
                order,
                previous_state,
                ack_reason,
                ack.venue_order_id,
                f"{command_id}:rejected",
            )
            raise _rs.refusal(*refused)
        changed = replace(updated, state=previous_state)
        self._append(
            order.account_id,
            EventKind.ORDER_UPDATED,
            OrderUpdated(
                order=changed,
                reason=ack_reason,
                venue_order_id=ack.venue_order_id,
            ),
            f"{command_id}:accepted",
        )
        return self.get_order(order_id)

    def _replace_emulated_stop(
        self, order: Order, changes: OrderChanges, command_id: str
    ) -> Order:
        reason = f"Local emulated stop replaced: {changes!r}"
        local_command_id = f"{command_id}:local"
        prior = self._ledger.event_by_command(local_command_id)
        if prior is not None:
            _plan("replace_replay", existing=_event(prior), mode="local", order=order.order_id,
                  account=order.account_id, reason=reason, command=command_id)
            return self.get_order(order.order_id)
        context = self._context(order.order_id)
        updated, _ = self._replacement_terms(order, changes, context.filled)
        self._append(
            order.account_id,
            EventKind.ORDER_UPDATED,
            OrderUpdated(updated, reason),
            local_command_id,
        )
        return self.get_order(order.order_id)

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

    def reconcile_order(self, order_id: str) -> Order:
        """Read back an ambiguous venue order; never resend it."""
        context = self._context(order_id)
        order = context.order
        states = self._broker.orders(order.created_at)
        venue_id = context.venue_order_id or order_id
        index = _plan("reconcile_find", order=order_id, venue=venue_id,
                      ids=[item.venue_order_id for item in states], state=order.state)
        found = states[index]
        # Status-only read-back cannot show which terms a working order carries, so a
        # pending replace resolves only once the venue reports the order finished.
        if _plan("pending_state", state=order.state):
            _plan("reconcile_replace", order=order_id, found=found.state,
                  unresolved=self._has_unresolved_replace(order_id))
        # Fills go in before any terminal state: the ledger refuses a fill on a cancelled
        # order, so a partial fill dropped here could never be recorded later (I1).
        if _plan("reconcile_fills", found=found.filled_quantity, recorded=context.filled):
            self._ingest_venue_fills(order, found)
        order = self.get_order(order_id)
        action = _plan("reconcile_result", found=found.state, state=order.state, order=order_id)
        if action == "return":
            return order
        if action == "updated":
            self._append(
                order.account_id,
                EventKind.ORDER_UPDATED,
                OrderUpdated(
                    replace(order, state=OrderState.SUBMITTED),
                    "Venue reconciliation confirmed SUBMITTED",
                    found.venue_order_id,
                ),
                f"{order.command_id}:reconcile:{found.updated_at.isoformat()}:SUBMITTED",
            )
            resolved = self.get_order(order_id)
            self._synchronize_bracket(resolved, f"{order_id}:reconcile-submitted")
            return resolved
        self._append(
            order.account_id,
            EventKind[action],
            OrderStateChange(
                order_id=order_id,
                reason=f"Venue reconciliation confirmed {found.state.value}",
                venue_order_id=found.venue_order_id,
            ),
            f"{order.command_id}:reconcile:{found.updated_at.isoformat()}:{found.state.value}",
        )
        resolved = self.get_order(order_id)
        self._synchronize_bracket(resolved, f"{order_id}:reconcile-{found.state.value}")
        return resolved

    def _ingest_venue_fills(self, order: Order, found: VenueOrderState) -> None:
        fills = self._broker.fills(order.created_at)
        indices = _plan("matching_ids", ids=[item.venue_order_id for item in fills],
                        venue=found.venue_order_id)
        matching = [fills[index] for index in indices]
        for item in matching:
            self.record_fill(
                Fill(
                    fill_id=item.venue_fill_id,
                    order_id=order.order_id,
                    account_id=order.account_id,
                    instrument=item.instrument,
                    quantity=item.quantity,
                    price=item.price,
                    venue_env=self._broker.env,
                    filled_at=item.filled_at,
                    side=item.side,
                    fee=item.fee,
                    venue_order_id=item.venue_order_id,
                    venue_execution_id=item.venue_fill_id,
                    leg_id=item.leg_id,
                )
            )
        recorded = self._context(order.order_id).filled
        if _plan("reconcile_fills", found=found.filled_quantity, recorded=recorded):
            _plan("ingest_check", found=found.filled_quantity, recorded=recorded,
                  order=order.order_id, state=self.get_order(order.order_id).state)

    def _submit_native(self, order: Order) -> Order:
        if not _plan("new", state=order.state):
            return order
        if not self._supports_native_type(order.order_type):
            self._planned_refusal(order, "native")
        self._require_tif(order)
        submitted = replace(order, state=OrderState.SUBMITTED)
        self._append(
            order.account_id,
            EventKind.ORDER_UPDATED,
            OrderUpdated(submitted, "Order submission requested"),
            f"{order.command_id}:submit",
        )
        self._mark_pending(
            submitted,
            "Submit outcome is pending until the venue responds",
            f"{order.command_id}:submit-pending",
        )
        try:
            ack = self._broker.submit(self._venue_order(order))
        except Exception as err:
            raise BrokerOutcomeUnknownError(
                f"Submit outcome for order '{order.order_id}' is unknown; it will not be resent"
            ) from err
        self._record_submit_ack(order, ack, f"{order.command_id}:submit-result")
        return self.get_order(order.order_id)

    def _start_emulation(self, order: Order) -> Order:
        action = _plan("start_emulation", order=order,
                       types=self._broker.capabilities.supported_order_types)
        if action == "return":
            return order
        if action == "refuse_limit":
            self._planned_refusal(order, "limit")
        if action == "limit":
            trigger_type = OrderType.LIMIT
        else:
            trigger_type = self._trigger_order_type(order)
        self._require_tif(order, trigger_type)
        if self._context(order.order_id).emulation is None:
            self._append(
                order.account_id,
                EventKind.ORDER_EMULATION_UPDATED,
                EmulatedOrderState(
                    order_id=order.order_id,
                    observed_price=None,
                    extreme=None,
                    stop_price=order.stop_price,
                    triggered=False,
                    reason=f"Emulated {order.order_type.value} working; awaiting live prices",
                ),
                f"{order.command_id}:emulation-start",
            )
        return self.get_order(order.order_id)

    def _submit_emulated(
        self,
        order: Order,
        trigger_price: Decimal,
        command_id: str,
        *,
        venue_type: OrderType,
        limit_price: Decimal | None,
    ) -> Order:
        current = self.get_order(order.order_id)
        if not _plan("submit_emulated", order=current):
            return current
        self._require_tif(current, venue_type)
        submitted = replace(current, state=OrderState.SUBMITTED)
        self._append(
            current.account_id,
            EventKind.ORDER_UPDATED,
            OrderUpdated(
                submitted,
                f"Emulated {current.order_type.value} submission requested",
            ),
            f"{current.command_id}:emulated-submit",
        )
        self._mark_pending(
            submitted,
            f"Emulated {current.order_type.value} triggered at {trigger_price} by {command_id}",
            f"{current.command_id}:emulated-pending",
        )
        venue = self._venue_order(
            current,
            order_type=venue_type,
            limit_price=limit_price,
            stop_price=None,
            trail_amount=None,
        )
        try:
            ack = self._broker.submit(venue)
        except Exception as err:
            raise BrokerOutcomeUnknownError(
                f"Emulated order '{current.order_id}' submit outcome is unknown"
            ) from err
        self._record_submit_ack(current, ack, f"{current.command_id}:emulated-result")
        return self.get_order(current.order_id)

    def _record_submit_ack(self, order: Order, ack: VenueAck, command_id: str) -> None:
        kind, reason = _plan("submit_ack", status=ack.status, message=ack.message or "")
        self._append(
            order.account_id,
            EventKind[kind],
            OrderStateChange(order.order_id, reason, ack.venue_order_id),
            command_id,
        )

    def _synchronize_bracket(self, changed_order: Order, cause_id: str) -> None:
        parent_id = _plan("parent", parent=changed_order.parent_order_id)
        if parent_id is not None:
            entry = self.get_order(parent_id)
        else:
            entry = changed_order
        state = self._account_state(entry.account_id)
        plan = _plan("sync", entry=entry, state=codec.canon(state))
        if plan is None:
            return
        stop_id, quantities, entry_terminal, target_ids, closer_ids = plan
        entry_filled, open_quantity, stop_filled = map(Decimal, quantities)
        stop = state.orders[stop_id]
        targets = [state.orders[item] for item in target_ids]
        closers = [state.orders[item] for item in closer_ids]
        if _plan("sync_mode", filled=entry_filled, open=open_quantity) == "protect":
            self._ensure_child_quantity(
                stop,
                open_quantity,
                f"{cause_id}:{stop.order_id}:protective-stop",
            )
            if _plan("sync_targets", stop_filled=stop_filled, terminal=entry_terminal):
                target_weights = tuple(
                    self._planned_quantity(target.order_id) for target in targets
                )
                weights = tuple(map(Decimal, _plan("target_weights", weights=target_weights,
                                                  planned=self._planned_quantity(entry.order_id))))
                target_budgets = self._allocate_quantity(
                    entry_filled, weights, entry.instrument
                )[: len(targets)]
                for target, budget in zip(targets, target_budgets, strict=True):
                    if _plan("new", state=target.state):
                        if not _plan("positive", quantity=budget):
                            self._cancel_order(
                                target,
                                f"{cause_id}:{target.order_id}:zero-budget",
                                "Target has no allocation for the filled entry quantity",
                            )
                        else:
                            self._ensure_child_quantity(
                                target,
                                budget,
                                f"{cause_id}:{target.order_id}:target-size",
                            )
        else:
            self._cancel_exits(
                [stop, *targets, *closers],
                f"{cause_id}:{entry.order_id}:flat",
            )

        if _plan("positive", quantity=stop_filled):
            self._cancel_targets(
                [*targets, *closers],
                f"{cause_id}:{stop.order_id}:stop-fill",
            )

    def _ensure_child_quantity(self, child: Order, quantity: Decimal, command_id: str) -> None:
        context = self._context(child.order_id)
        current = context.order
        action, total = _plan("child_quantity", order=current, filled=context.filled,
                              quantity=quantity)
        if action == "return":
            return
        if action == "submit":
            self._submit_child(current)
            return
        total_quantity = Decimal(total)
        updated = replace(current, quantity=total_quantity)
        if action == "local":
            self._append(
                current.account_id,
                EventKind.ORDER_UPDATED,
                OrderUpdated(updated, "Sized to confirmed parent fills"),
                f"{command_id}:local-size",
            )
            self._submit_child(self.get_order(current.order_id))
            return
        self.replace(
            current.order_id,
            OrderChanges(new_quantity=total_quantity),
            command_id=command_id,
        )

    def _submit_child(self, child: Order) -> None:
        current = self.get_order(child.order_id)
        if _plan("new", state=current.state):
            submitted = self.submit(current)
            _plan("stop_rejected", type=child.order_type, state=submitted.state, order=child.order_id)

    def _cancel_targets(self, targets: list[Order], command_id: str) -> None:
        self._cancel_exits(targets, command_id)

    def _cancel_exits(self, exits: list[Order], command_id: str) -> None:
        for sibling in exits:
            current = self.get_order(sibling.order_id)
            if _plan("terminal", state=current.state):
                continue
            cancelled = self._cancel_order(
                current,
                f"{command_id}:cancel:{current.order_id}",
                f"Exit sibling cancelled after bracket resolution ({command_id})",
                oco=True,
            )
            _plan("oco_check", state=cancelled.state, order=current.order_id)

    def _cancel_order(
        self,
        order: Order,
        command_id: str,
        reason: str,
        *,
        oco: bool = False,
    ) -> Order:
        context = self._context(order.order_id)
        current = context.order
        action = _plan("cancel_mode", state=current.state, order=current.order_id, oco=oco)
        if action == "return":
            return current
        if action == "local":
            self._append(
                current.account_id,
                EventKind.ORDER_CANCELLED,
                OrderStateChange(current.order_id, reason),
                f"{command_id}:local",
            )
            return self.get_order(current.order_id)
        venue_id = context.venue_order_id
        _plan("confirmed_id", venue=venue_id, order=current.order_id, mode="cancel")
        self._mark_pending(current, f"Cancel pending: {reason}", f"{command_id}:pending")
        try:
            ack = self._broker.cancel(venue_id)
        except Exception as err:
            message = f"Cancel outcome for '{current.order_id}' is unknown"
            if oco:
                raise OCOOutcomeUnknownError(message) from err
            raise BrokerOutcomeUnknownError(message) from err
        action, ack_reason, refused = _plan("cancel_ack", status=ack.status, reason=reason,
                                          message=ack.message or "", order=current.order_id, oco=oco)
        if action == "accepted":
            self._append(
                current.account_id,
                EventKind.ORDER_CANCELLED,
                OrderStateChange(current.order_id, reason, ack.venue_order_id),
                f"{command_id}:accepted",
            )
            return self.get_order(current.order_id)
        if action == "rejected":
            self._refuse(
                current,
                ack_reason,
                f"{command_id}:refused",
            )
            raise _rs.refusal(*refused)
        if action == "pending":
            self._append(
                current.account_id,
                EventKind.ORDER_PENDING,
                OrderStateChange(
                    current.order_id,
                    ack_reason,
                    ack.venue_order_id,
                ),
                f"{command_id}:venue-pending",
            )
            if refused is not None:
                raise _rs.refusal(*refused)
            return self.get_order(current.order_id)

    def _restore_working_state(
        self,
        order: Order,
        previous_state: OrderState,
        reason: str,
        venue_order_id: str,
        command_id: str,
    ) -> None:
        restored = replace(order, state=previous_state)
        self._append(
            order.account_id,
            EventKind.ORDER_UPDATED,
            OrderUpdated(restored, reason, venue_order_id),
            command_id,
        )

    def _mark_pending(self, order: Order, reason: str, command_id: str) -> None:
        current = self.get_order(order.order_id)
        if _plan("pending_state", state=current.state):
            return
        self._append(
            current.account_id,
            EventKind.ORDER_PENDING,
            OrderStateChange(current.order_id, reason, self._context(current.order_id).venue_order_id),
            command_id,
        )

    def _venue_order(
        self,
        order: Order,
        *,
        order_type: OrderType | None = None,
        limit_price: Decimal | None = None,
        stop_price: Decimal | None = None,
        trail_amount: Decimal | None = None,
    ) -> VenueOrder:
        return VenueOrder(
            venue_order_id=order.order_id,
            instrument=order.instrument,
            order_type=order.order_type if order_type is None else order_type,
            side=order.side,
            quantity=order.quantity,
            submitted_at=self._utc_now(),
            tif=order.tif,
            limit_price=order.limit_price if order_type is None else limit_price,
            stop_price=order.stop_price if order_type is None else stop_price,
            trail_amount=order.trail_amount if order_type is None else trail_amount,
            allocations=(
                VenueOrderAllocation(order.order_id, order.account_id, order.quantity),
            ),
            parent_order_id=order.parent_order_id,
            oco_group=order.oco_group,
        )

    def _trigger_order_type(self, order: Order) -> OrderType:
        planned = _plan("trigger_type", types=self._broker.capabilities.supported_order_types)
        if planned is not None:
            return OrderType(planned)
        self._planned_refusal(order, "trigger")

    def _supports_native_type(self, order_type: OrderType) -> bool:
        return _plan("native", type=order_type, **self._capabilities())

    def _capabilities(self):
        capabilities = self._broker.capabilities
        return dict(types=capabilities.supported_order_types,
                    tifs=capabilities.supported_tifs,
                    native_stops=capabilities.supports_native_stops)

    def _require_tif(self, order: Order, venue_type: OrderType | None = None) -> None:
        if _plan("tif", tif=order.tif, venue_type=venue_type, **self._capabilities()) == "refuse":
            self._planned_refusal(order, "tif")

    def _planned_refusal(self, order: Order, mode: str, *, command_id: str | None = None, **values):
        reason, suffix, refusal = _plan("refused", order=order, mode=mode, **values)
        self._refuse(order, reason, f"{order.command_id if command_id is None else command_id}:{suffix}")
        raise _rs.refusal(*refusal)

    def _refuse(self, order: Order, reason: str, command_id: str) -> None:
        self._append(
            order.account_id,
            EventKind.ORDER_REFUSED,
            OrderStateChange(order.order_id, reason),
            command_id,
        )

    def _ensure_stored(self, order: Order) -> None:
        self._validate_quantity(order.instrument, order.quantity)
        try:
            self.get_order(order.order_id)
        except KeyError:
            payload = OrdersCreated(
                orders=(order,),
                fingerprint=self._fingerprint_order(order),
                reason="Standalone strategy order created",
            )
            self._append(
                order.account_id,
                EventKind.ORDERS_CREATED,
                payload,
                order.command_id,
            )
            return
        created = self._planned_order(order.order_id)
        _plan("stored", created=created, candidate=order)

    def _context(self, order_id: str) -> _OrderContext:
        # Each account's cached state (the ledger folds every append into it), in
        # first-event order: the same answer as a full fold without re-reading the log.
        for account in self._ledger.accounts():
            state = self._ledger.state(account)
            order = state.orders.get(order_id)
            if order is not None:
                return _OrderContext(
                    order=order,
                    filled=state.filled_quantity.get(order_id, Decimal("0")),
                    venue_order_id=state.venue_order_ids.get(order_id),
                    emulation=state.emulated_orders.get(order_id),
                )
        raise KeyError(f"Unknown order_id '{order_id}'")

    def _account_state(self, account_id: str):
        return self._ledger.state(account_id)

    def _planned_quantity(self, order_id: str) -> Decimal:
        return self._planned_order(order_id).quantity

    def _planned_order(self, order_id: str) -> Order:
        for event in self._ledger.events_of_kind(EventKind.ORDERS_CREATED):
            if event.kind is EventKind.ORDERS_CREATED:
                for order in event.payload.orders:
                    if order.order_id == order_id:
                        return order
        raise KeyError(f"No creation event contains order '{order_id}'")

    def _has_unresolved_replace(self, order_id: str) -> bool:
        for event in self._ledger.events_of_kind(EventKind.ORDER_PENDING):
            command_id = _plan("pending_candidate", order=order_id, event=dict(
                pending=event.kind is EventKind.ORDER_PENDING, order=event.payload.order_id,
                reason=event.payload.reason, command=event.command_id))
            if command_id is not None:
                if (self._ledger.event_by_command(f"{command_id}:accepted") is None
                        and self._ledger.event_by_command(f"{command_id}:rejected") is None):
                    return True
        return False

    def _append(
        self, account_id: str, kind: EventKind, payload: object, command_id: str
    ) -> Event:
        event = Event(
            account=account_id,
            kind=kind,
            payload=payload,
            ts_utc=self._utc_now(),
            command_id=command_id,
        )
        existing = self._ledger.event_by_command(command_id)
        if existing is not None:
            _plan("append_replay", existing=_event(existing), account=account_id,
                  kind=kind.name, payload=payload, command=command_id)
            return existing
        return self._ledger.append(event)

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

    def _bracket_from_orders(self, orders: tuple[Order, ...]) -> Bracket:
        by_id = {order.order_id: self._stored_order(order) for order in orders}
        entry, stop, targets = _plan("bracket", entry="", orders=list(by_id.values()))
        return Bracket(by_id[entry], by_id[stop], tuple(by_id[item] for item in targets))

    def _stored_order(self, order: Order) -> Order:
        try:
            return self.get_order(order.order_id)
        except KeyError:
            return order

    def _utc_now(self) -> datetime:
        value = self._clock.now_utc()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Clock returned a naive datetime")
        return value

"""Event-sourced order management, bracket orchestration, and 1:1 routing."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import ROUND_FLOOR, Decimal

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
    AccountState,
    Ledger,
    OrderStateChange,
    OrderUpdated,
    OrdersCreated,
)
from trade_engine.ledger.codec import encode_payload
from trade_engine.oms.models import Bracket
from trade_engine.oms.trailing import TrailingStopEmulator


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
        if not quantity.is_finite() or quantity <= 0:
            raise ValueError("quantity must be finite and positive")
        unsupported = sorted(
            {intent.entry_tif, intent.exit_tif} - self._broker.capabilities.supported_tifs
        )
        if unsupported:
            # Refuse before the entry can fill; a GTC stop refused later leaves an open
            # position unprotected.
            raise UnsupportedOrderCapabilityError(
                "Venue does not support bracket time in force "
                f"{', '.join(tif.value for tif in unsupported)}"
            )
        if intent.entry_type is OrderType.STOP and not self._supports_native_type(OrderType.STOP):
            # An emulated entry triggers only on prices someone feeds it; nothing watches
            # an EOD entry overnight, so the breakout would silently never trade.
            raise UnsupportedOrderCapabilityError(
                "Venue has no native STOP orders; a stop entry cannot be worked"
            )
        fingerprint = self._bracket_fingerprint(intent, quantity)
        existing = self._ledger.event_by_command(intent.command_id)
        if existing is not None:
            if (
                existing.kind is not EventKind.ORDERS_CREATED
                or existing.account != intent.account_id
                or existing.payload.fingerprint != fingerprint
            ):
                raise IdempotencyConflictError(
                    f"command_id '{intent.command_id}' was already used for a different OMS command"
                )
            return self._bracket_from_orders(existing.payload.orders)

        now = self._utc_now()
        prefix = intent.command_id
        entry = Order(
            order_id=f"{prefix}:entry",
            account_id=intent.account_id,
            instrument=intent.instrument,
            order_type=intent.entry_type,
            side=intent.side,
            quantity=quantity,
            command_id=f"{prefix}:entry",
            created_at=now,
            limit_price=intent.entry_price if intent.entry_type is OrderType.LIMIT else None,
            stop_price=intent.entry_price if intent.entry_type is OrderType.STOP else None,
            tif=intent.entry_tif,
        )
        exit_side = Side.SELL if intent.side is Side.BUY else Side.BUY
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
        if intent.target_fractions is None:
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
        if current.parent_order_id is not None:
            parent = self._context(current.parent_order_id)
            if self._account_state(current.account_id).filled_quantity.get(
                parent.order.order_id, Decimal("0")
            ) <= 0:
                self._refuse(
                    current,
                    f"Child held until parent '{parent.order.order_id}' has a fill",
                    f"{current.command_id}:child-held",
                )
                raise OrderManagementError(
                    f"Child order '{current.order_id}' is held until its entry fills"
                )
        if current.order_type in (
            OrderType.STOP,
            OrderType.STOP_LIMIT,
            OrderType.TRAIL,
        ) and not self._supports_native_type(current.order_type):
            return self._start_emulation(current)
        return self._submit_native(current)

    def submit_trailing(self, order: Order) -> Order:
        """Start a native venue trail or persist a live emulated trail locally."""
        if order.order_type is not OrderType.TRAIL:
            raise ValueError("submit_trailing requires a TRAIL order")
        return self.submit(order)

    def update_trailing(
        self, order_id: str, price: Decimal, *, command_id: str
    ) -> Order:
        """Apply one live price observation to a persisted emulated trailing stop."""
        order = self.get_order(order_id)
        if order.order_type is not OrderType.TRAIL:
            raise ValueError("update_trailing requires a TRAIL order")
        if OrderType.TRAIL in self._broker.capabilities.supported_order_types:
            raise OrderManagementError(
                f"Order '{order_id}' is native at this venue; no local trail is running"
            )
        return self.update_emulated_order(order_id, price, command_id=command_id)

    def update_emulated_order(
        self, order_id: str, price: Decimal, *, command_id: str
    ) -> Order:
        """Evaluate one observed price against a persisted emulated order."""
        if not price.is_finite() or price <= 0:
            raise ValueError(f"price must be finite and positive, got {price}")
        context = self._context(order_id)
        order = context.order
        emulation = context.emulation
        if emulation is None:
            raise OrderManagementError(f"Emulated order '{order_id}' has not been started")
        if self._supports_native_type(order.order_type):
            raise OrderManagementError(
                f"Order '{order_id}' is native at this venue; no local emulation is running"
            )
        prior = self._ledger.event_by_command(command_id)
        if prior is not None:
            if (
                prior.kind is not EventKind.ORDER_EMULATION_UPDATED
                or prior.account != order.account_id
                or prior.payload.order_id != order_id
                or prior.payload.observed_price != price
            ):
                raise IdempotencyConflictError(
                    f"command_id '{command_id}' was replayed with a different price observation"
                )
            current = self.get_order(order_id)
            if prior.payload.triggered:
                if current.state is OrderState.PENDING_UNKNOWN:
                    raise OrderPendingReconciliationError(
                        f"Triggered emulated order '{order_id}' awaits venue reconciliation"
                    )
                if current.state is OrderState.NEW:
                    return self._route_emulated_trigger(
                        order, prior.payload.observed_price, command_id
                    )
            return current
        if emulation.triggered:
            if order.state is OrderState.PENDING_UNKNOWN:
                raise OrderPendingReconciliationError(
                    f"Triggered emulated order '{order_id}' awaits venue reconciliation"
                )
            if order.state is OrderState.NEW:
                return self._route_emulated_trigger(
                    order, emulation.observed_price, command_id
                )
            return order
        if order.order_type is OrderType.TRAIL:
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
            stop_price = order.stop_price
            if stop_price is None:
                raise OrderManagementError(
                    f"Emulated {order.order_type.value} order '{order_id}' has no stop price"
                )
            extreme = None
            triggered = (
                price >= stop_price if order.side is Side.BUY else price <= stop_price
            )
        new_emulation = EmulatedOrderState(
            order_id=order_id,
            observed_price=price,
            extreme=extreme,
            stop_price=stop_price,
            triggered=triggered,
            reason=(
                f"{order.order_type.value} triggered at observed price {price}"
                if triggered
                else f"Emulated {order.order_type.value} observed price {price}"
            ),
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
        if trigger_price is None:
            raise OrderManagementError(
                f"Triggered emulated order '{order.order_id}' has no observed price"
            )
        if order.order_type is OrderType.STOP_LIMIT:
            venue_type = OrderType.LIMIT
            limit_price = order.limit_price
        else:
            venue_type = self._trigger_order_type(order)
            limit_price = trigger_price if venue_type is OrderType.LIMIT else None
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
        if order.parent_order_id is None or order.oco_group is None:
            return
        siblings = [
            candidate
            for candidate in self._account_state(order.account_id).orders.values()
            if candidate.order_id != order.order_id
            and candidate.parent_order_id == order.parent_order_id
            and candidate.oco_group == order.oco_group
        ]
        self._cancel_exits(siblings, f"{command_id}:{order.order_id}:trigger")

    def record_fill(self, fill: Fill) -> Order:
        """Persist a venue fill and synchronize bracket protection from folded state."""
        context = self._context(fill.order_id)
        order = context.order
        if fill.account_id != order.account_id or fill.venue_env != self._broker.env:
            raise OrderManagementError(
                f"Fill '{fill.fill_id}' account or venue environment does not match order '{order.order_id}'"
            )
        self._validate_quantity(order.instrument, fill.quantity)
        self._append(order.account_id, EventKind.FILL, fill, f"fill:{fill.fill_id}")
        self._synchronize_bracket(order, fill.fill_id)
        return self.get_order(order.order_id)

    def cancel(self, order_id: str, *, command_id: str) -> Order:
        """Cancel a working order; only a confirmed venue ack becomes CANCELLED."""
        order = self.get_order(order_id)
        if order.parent_order_id is not None and order.order_type is OrderType.STOP:
            state = self._account_state(order.account_id)
            entry_filled = state.filled_quantity.get(order.parent_order_id, Decimal("0"))
            exits_filled = sum(
                (
                    quantity
                    for candidate_id, quantity in state.filled_quantity.items()
                    if state.orders[candidate_id].parent_order_id == order.parent_order_id
                ),
                Decimal("0"),
            )
            if entry_filled > exits_filled and order.state not in (
                OrderState.CANCELLED,
                OrderState.FILLED,
                OrderState.REJECTED,
                OrderState.EXPIRED,
            ):
                self._refuse(
                    order,
                    "Cannot cancel the only protective stop while the bracket has open quantity",
                    f"{command_id}:protective-stop-refused",
                )
                raise OrderManagementError(
                    f"Cannot cancel protective stop '{order_id}' while its position is open"
                )
        result = self._cancel_order(order, command_id, "Cancelled by OMS command")
        if order.parent_order_id is None:
            self._synchronize_bracket(order, command_id)
        return result

    def replace(
        self, order_id: str, changes: OrderChanges, *, command_id: str
    ) -> Order:
        """Replace a live order; pending/unknown acknowledgements remain unknown."""
        order = self.get_order(order_id)
        context = self._context(order_id)
        if (
            order.state is OrderState.NEW
            and order.order_type in (OrderType.STOP, OrderType.STOP_LIMIT)
            and context.emulation is not None
            and not context.emulation.triggered
        ):
            return self._replace_emulated_stop(order, changes, command_id)
        request_reason = f"Replace pending: {changes!r}"
        pending_command_id = f"{command_id}:pending"
        prior_request = self._ledger.event_by_command(pending_command_id)
        prior_noop = self._ledger.event_by_command(f"{command_id}:noop")
        if prior_request is None and prior_noop is not None:
            expected_reason = f"No-op replace: {changes!r}"
            if (
                prior_noop.account != order.account_id
                or prior_noop.kind is not EventKind.ORDER_UPDATED
                or prior_noop.payload.order.order_id != order_id
                or prior_noop.payload.reason != expected_reason
            ):
                raise IdempotencyConflictError(
                    f"command_id '{command_id}' was replayed with different replace changes"
                )
            return order
        if prior_request is not None:
            if (
                prior_request.account != order.account_id
                or prior_request.kind is not EventKind.ORDER_PENDING
                or prior_request.payload.order_id != order_id
                or prior_request.payload.reason != request_reason
            ):
                raise IdempotencyConflictError(
                    f"command_id '{command_id}' was replayed with different replace changes"
                )
            if order.state is OrderState.PENDING_UNKNOWN:
                raise OrderPendingReconciliationError(
                    f"Replace command '{command_id}' remains pending reconciliation"
                )
            return order
        filled = context.filled
        if order.state in (
            OrderState.NEW,
            OrderState.FILLED,
            OrderState.CANCELLED,
            OrderState.EXPIRED,
            OrderState.REJECTED,
        ):
            raise OrderManagementError(
                f"Cannot replace order '{order_id}' in state {order.state.value}"
            )
        if order.state is OrderState.PENDING_UNKNOWN:
            raise OrderPendingReconciliationError(
                f"Order '{order_id}' is pending reconciliation"
            )
        if changes.new_quantity is not None and (
            not changes.new_quantity.is_finite() or changes.new_quantity < filled or changes.new_quantity <= 0
        ):
            raise ValueError(
                f"replacement quantity must be finite, positive, and at least filled quantity {filled}"
            )
        if changes.new_quantity is not None:
            self._validate_quantity(order.instrument, changes.new_quantity)
        updated = replace(
            order,
            quantity=changes.new_quantity if changes.new_quantity is not None else order.quantity,
            limit_price=changes.new_limit_price if changes.new_limit_price is not None else order.limit_price,
            stop_price=changes.new_stop_price if changes.new_stop_price is not None else order.stop_price,
        )
        if updated == order:
            self._append(
                order.account_id,
                EventKind.ORDER_UPDATED,
                OrderUpdated(order, f"No-op replace: {changes!r}"),
                f"{command_id}:noop",
            )
            return order

        previous_state = order.state
        venue_id = context.venue_order_id
        if venue_id is None:
            raise OrderPendingReconciliationError(
                f"Order '{order_id}' has no confirmed venue order id"
            )
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
        if ack.status == "PENDING":
            self._append(
                order.account_id,
                EventKind.ORDER_PENDING,
                OrderStateChange(
                    order_id,
                    f"Venue replace remains pending: {ack.message or 'no status message'}",
                    ack.venue_order_id,
                ),
                f"{command_id}:venue-pending",
            )
            return self.get_order(order_id)
        if ack.status == "REJECTED":
            self._refuse(
                order,
                f"Venue rejected replace: {ack.message or 'reason not supplied'}",
                f"{command_id}:refused",
            )
            self._restore_working_state(
                order,
                previous_state,
                f"Venue rejected replace: {ack.message or 'reason not supplied'}",
                ack.venue_order_id,
                f"{command_id}:rejected",
            )
            raise OrderManagementError(
                f"Venue rejected replace for '{order_id}': {ack.message or 'reason not supplied'}"
            )
        changed = replace(updated, state=previous_state)
        self._append(
            order.account_id,
            EventKind.ORDER_UPDATED,
            OrderUpdated(
                order=changed,
                reason=f"Venue confirmed replace: {command_id}",
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
            if (
                prior.account != order.account_id
                or prior.kind is not EventKind.ORDER_UPDATED
                or prior.payload.order.order_id != order.order_id
                or prior.payload.reason != reason
            ):
                raise IdempotencyConflictError(
                    f"command_id '{command_id}' was replayed with different emulated stop changes"
                )
            return self.get_order(order.order_id)
        context = self._context(order.order_id)
        if changes.new_quantity is not None:
            if (
                not changes.new_quantity.is_finite()
                or changes.new_quantity <= 0
                or changes.new_quantity < context.filled
            ):
                raise ValueError(
                    f"replacement quantity must be finite, positive, and at least filled quantity {context.filled}"
                )
            self._validate_quantity(order.instrument, changes.new_quantity)
        updated = replace(
            order,
            quantity=changes.new_quantity if changes.new_quantity is not None else order.quantity,
            limit_price=changes.new_limit_price if changes.new_limit_price is not None else order.limit_price,
            stop_price=changes.new_stop_price if changes.new_stop_price is not None else order.stop_price,
        )
        self._append(
            order.account_id,
            EventKind.ORDER_UPDATED,
            OrderUpdated(updated, reason),
            local_command_id,
        )
        return self.get_order(order.order_id)

    def reconcile_order(self, order_id: str) -> Order:
        """Read back an ambiguous venue order; never resend it."""
        context = self._context(order_id)
        order = context.order
        states = self._broker.orders(order.created_at)
        venue_id = context.venue_order_id or order_id
        found = next(
            (
                item
                for item in states
                if item.venue_order_id == venue_id or item.venue_order_id == order_id
            ),
            None,
        )
        if found is None:
            raise OrderReconciliationError(
                f"Venue has no read-back for order '{order_id}'; it remains {order.state.value}"
            )
        terminal = found.state in (
            OrderState.FILLED,
            OrderState.CANCELLED,
            OrderState.REJECTED,
            OrderState.EXPIRED,
        )
        # Status-only read-back cannot show which terms a working order carries, so a
        # pending replace resolves only once the venue reports the order finished.
        if (
            order.state is OrderState.PENDING_UNKNOWN
            and self._has_unresolved_replace(order_id)
            and not terminal
        ):
            raise OrderReconciliationError(
                f"Pending replace terms for '{order_id}' cannot be resolved from status-only venue read-back"
            )
        # Fills go in before any terminal state: the ledger refuses a fill on a cancelled
        # order, so a partial fill dropped here could never be recorded later (I1).
        if found.filled_quantity > context.filled:
            self._ingest_venue_fills(order, found)
        order = self.get_order(order_id)
        if found.state is OrderState.FILLED or found.state is OrderState.PARTIALLY_FILLED:
            if found.state is OrderState.FILLED and order.state is not OrderState.FILLED:
                raise OrderReconciliationError(
                    f"Venue reports '{order_id}' FILLED but its fill records do not complete it"
                )
            return order
        if found.state is OrderState.SUBMITTED:
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
        if found.state in (
            OrderState.ACCEPTED,
            OrderState.CANCELLED,
            OrderState.REJECTED,
            OrderState.EXPIRED,
        ):
            if found.state is OrderState.CANCELLED:
                kind = EventKind.ORDER_CANCELLED
            elif found.state is OrderState.REJECTED:
                kind = EventKind.ORDER_REJECTED
            elif found.state is OrderState.EXPIRED:
                kind = EventKind.ORDER_EXPIRED
            else:
                kind = EventKind.ORDER_ACCEPTED
            self._append(
                order.account_id,
                kind,
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
        raise OrderReconciliationError(
            f"Venue state {found.state.value} does not resolve order '{order_id}'"
        )

    def _ingest_venue_fills(self, order: Order, found: VenueOrderState) -> None:
        fills = self._broker.fills(order.created_at)
        matching = [item for item in fills if item.venue_order_id == found.venue_order_id]
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
                )
            )
        recorded = self._context(order.order_id).filled
        if recorded < found.filled_quantity:
            raise OrderReconciliationError(
                f"Venue reports {found.filled_quantity} filled for '{order.order_id}' but its "
                f"fill records account for {recorded}; it remains {self.get_order(order.order_id).state.value}"
            )

    def _submit_native(self, order: Order) -> Order:
        if order.state is not OrderState.NEW:
            if order.state in (OrderState.SUBMITTED, OrderState.PENDING_UNKNOWN):
                return order
            return order
        if not self._supports_native_type(order.order_type):
            self._refuse(
                order,
                f"Venue does not support {order.order_type.value}; no native order was sent",
                f"{order.command_id}:unsupported-type",
            )
            raise UnsupportedOrderCapabilityError(
                f"Venue does not support order type {order.order_type.value}"
            )
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
        if order.state is OrderState.PENDING_UNKNOWN:
            raise OrderPendingReconciliationError(
                f"Emulated order '{order.order_id}' is pending reconciliation"
            )
        if order.state is not OrderState.NEW:
            return order
        if order.order_type is OrderType.STOP_LIMIT:
            if OrderType.LIMIT not in self._broker.capabilities.supported_order_types:
                self._refuse(
                    order,
                    "Emulated STOP_LIMIT requires venue LIMIT support",
                    f"{order.command_id}:no-stop-limit-fallback",
                )
                raise UnsupportedOrderCapabilityError(
                    "Emulated STOP_LIMIT requires venue LIMIT support"
                )
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
        if current.state is not OrderState.NEW:
            if current.state is OrderState.PENDING_UNKNOWN:
                raise OrderPendingReconciliationError(
                    f"Triggered order '{order.order_id}' awaits venue reconciliation"
                )
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
        if ack.status == "ACCEPTED":
            kind = EventKind.ORDER_ACCEPTED
            reason = "Venue accepted order"
        elif ack.status == "REJECTED":
            kind = EventKind.ORDER_REJECTED
            reason = f"Venue rejected order: {ack.message or 'reason not supplied'}"
        elif ack.status == "PENDING":
            kind = EventKind.ORDER_PENDING
            reason = f"Venue has not resolved order: {ack.message or 'no status message'}"
        else:
            raise OrderManagementError(f"Unrecognized venue submit status {ack.status!r}")
        self._append(
            order.account_id,
            kind,
            OrderStateChange(order.order_id, reason, ack.venue_order_id),
            command_id,
        )

    def _synchronize_bracket(self, changed_order: Order, cause_id: str) -> None:
        if changed_order.parent_order_id is not None:
            entry = self.get_order(changed_order.parent_order_id)
        else:
            entry = changed_order
        context = self._context(entry.order_id)
        state = self._account_state(entry.account_id)
        entry_filled = state.filled_quantity.get(entry.order_id, Decimal("0"))
        children = [
            order
            for order in state.orders.values()
            if order.parent_order_id == entry.order_id
        ]
        stop = next((order for order in children if order.order_type is OrderType.STOP), None)
        targets = [order for order in children if order.order_type is OrderType.LIMIT]
        if stop is None:
            return
        entry_terminal = context.order.state in (
            OrderState.FILLED,
            OrderState.CANCELLED,
            OrderState.REJECTED,
            OrderState.EXPIRED,
        )
        if (
            state.filled_quantity.get(entry.order_id, Decimal("0")) == 0
            and not entry_terminal
        ):
            return

        stop_filled = state.filled_quantity.get(stop.order_id, Decimal("0"))
        target_filled = sum(
            (state.filled_quantity.get(target.order_id, Decimal("0")) for target in targets),
            Decimal("0"),
        )
        open_quantity = entry_filled - stop_filled - target_filled
        if open_quantity < 0:
            raise OrderManagementError(
                f"Exit fills exceed entry fills for bracket '{entry.order_id}'; venue reconciliation required"
            )

        if entry_filled > 0 and open_quantity > 0:
            self._ensure_child_quantity(
                stop,
                open_quantity,
                f"{cause_id}:{stop.order_id}:protective-stop",
            )
            if stop_filled == 0 and entry_terminal:
                target_weights = tuple(
                    self._planned_quantity(target.order_id) for target in targets
                )
                # Targets planned below the entry size leave a runner; it keeps its share
                # of a partial entry fill instead of the targets absorbing all of it.
                runner = self._planned_quantity(entry.order_id) - sum(
                    target_weights, Decimal("0")
                )
                weights = (*target_weights, runner) if runner > 0 else target_weights
                target_budgets = self._allocate_quantity(
                    entry_filled, weights, entry.instrument
                )[: len(targets)]
                for target, budget in zip(targets, target_budgets, strict=True):
                    if target.state is OrderState.NEW:
                        if budget <= 0:
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
                [stop, *targets],
                f"{cause_id}:{entry.order_id}:flat",
            )

        if stop_filled > 0:
            self._cancel_targets(
                targets,
                f"{cause_id}:{stop.order_id}:stop-fill",
            )

    def _ensure_child_quantity(self, child: Order, quantity: Decimal, command_id: str) -> None:
        context = self._context(child.order_id)
        current = context.order
        if current.state in (OrderState.CANCELLED, OrderState.FILLED, OrderState.REJECTED):
            if current.state is OrderState.REJECTED:
                if current.order_type is OrderType.STOP:
                    raise OrderManagementError(
                        f"Protective stop '{current.order_id}' was rejected; "
                        "open quantity requires venue reconciliation"
                    )
                return
            raise OrderManagementError(
                f"Protective child '{current.order_id}' is terminal in state {current.state.value}"
            )
        filled = context.filled
        self._validate_quantity(current.instrument, quantity)
        total_quantity = filled + quantity
        if current.quantity == total_quantity:
            if current.state is OrderState.NEW:
                self._submit_child(current)
            return
        updated = replace(current, quantity=total_quantity)
        if current.state is OrderState.NEW:
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
        if current.state is OrderState.NEW:
            submitted = self.submit(current)
            if child.order_type is OrderType.STOP and submitted.state is OrderState.REJECTED:
                raise OrderManagementError(
                    f"Protective stop '{child.order_id}' was rejected; "
                    "open quantity requires venue reconciliation"
                )

    def _cancel_targets(self, targets: list[Order], command_id: str) -> None:
        self._cancel_exits(targets, command_id)

    def _cancel_exits(self, exits: list[Order], command_id: str) -> None:
        for sibling in exits:
            current = self.get_order(sibling.order_id)
            if current.state in (
                OrderState.CANCELLED,
                OrderState.FILLED,
                OrderState.REJECTED,
                OrderState.EXPIRED,
            ):
                continue
            cancelled = self._cancel_order(
                current,
                f"{command_id}:cancel:{current.order_id}",
                f"Exit sibling cancelled after bracket resolution ({command_id})",
                oco=True,
            )
            if cancelled.state not in (
                OrderState.CANCELLED,
                OrderState.FILLED,
                OrderState.REJECTED,
                OrderState.EXPIRED,
            ):
                raise OCOOutcomeUnknownError(
                    f"Could not confirm cancellation of OCO sibling '{current.order_id}'"
                )

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
        if current.state in (
            OrderState.CANCELLED,
            OrderState.FILLED,
            OrderState.REJECTED,
            OrderState.EXPIRED,
        ):
            return current
        if current.state is OrderState.PENDING_UNKNOWN:
            if oco:
                raise OCOOutcomeUnknownError(
                    f"OCO sibling '{current.order_id}' is already pending reconciliation"
                )
            return current
        if current.state is OrderState.NEW:
            self._append(
                current.account_id,
                EventKind.ORDER_CANCELLED,
                OrderStateChange(current.order_id, reason),
                f"{command_id}:local",
            )
            return self.get_order(current.order_id)
        venue_id = context.venue_order_id
        if venue_id is None:
            raise OrderPendingReconciliationError(
                f"Working order '{current.order_id}' has no venue id"
            )
        self._mark_pending(current, f"Cancel pending: {reason}", f"{command_id}:pending")
        try:
            ack = self._broker.cancel(venue_id)
        except Exception as err:
            message = f"Cancel outcome for '{current.order_id}' is unknown"
            if oco:
                raise OCOOutcomeUnknownError(message) from err
            raise BrokerOutcomeUnknownError(message) from err
        if ack.status == "ACCEPTED":
            self._append(
                current.account_id,
                EventKind.ORDER_CANCELLED,
                OrderStateChange(current.order_id, reason, ack.venue_order_id),
                f"{command_id}:accepted",
            )
            return self.get_order(current.order_id)
        if ack.status == "REJECTED":
            message = (
                f"Venue rejected cancel for '{current.order_id}': "
                f"{ack.message or 'order may already have filled; reconcile required'}"
            )
            self._refuse(
                current,
                message,
                f"{command_id}:refused",
            )
            if oco:
                raise OCOOutcomeUnknownError(message)
            raise OrderPendingReconciliationError(message)
        if ack.status == "PENDING":
            self._append(
                current.account_id,
                EventKind.ORDER_PENDING,
                OrderStateChange(
                    current.order_id,
                    f"Venue cancel remains pending: {ack.message or 'no status message'}",
                    ack.venue_order_id,
                ),
                f"{command_id}:venue-pending",
            )
            if oco:
                raise OCOOutcomeUnknownError(
                    f"Venue has not confirmed cancellation of OCO sibling '{current.order_id}'"
                )
            return self.get_order(current.order_id)
        raise OrderManagementError(f"Unrecognized venue cancel status {ack.status!r}")

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
        if current.state is OrderState.PENDING_UNKNOWN:
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
        supported = self._broker.capabilities.supported_order_types
        if OrderType.MARKET in supported:
            return OrderType.MARKET
        if OrderType.LIMIT in supported:
            return OrderType.LIMIT
        self._refuse(
            order,
            "Emulated stop requires venue MARKET or LIMIT support when triggered",
            f"{order.command_id}:no-trigger-order",
        )
        raise UnsupportedOrderCapabilityError(
            "Emulated stops require MARKET or LIMIT capability"
        )

    def _supports_native_type(self, order_type: OrderType) -> bool:
        capabilities = self._broker.capabilities
        if (
            order_type in (OrderType.STOP, OrderType.STOP_LIMIT)
            and not capabilities.supports_native_stops
        ):
            return False
        return order_type in capabilities.supported_order_types

    def _require_tif(self, order: Order, venue_type: OrderType | None = None) -> None:
        if order.tif not in self._broker.capabilities.supported_tifs:
            self._refuse(
                order,
                f"Venue does not support time in force {order.tif.value}",
                f"{order.command_id}:unsupported-tif:{order.tif.value}",
            )
            raise UnsupportedOrderCapabilityError(
                f"Venue does not support time in force {order.tif.value}"
            )
        if venue_type is not None and venue_type not in self._broker.capabilities.supported_order_types:
            raise UnsupportedOrderCapabilityError(
                f"Venue does not support trigger order type {venue_type.value}"
            )

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
        candidate = replace(order, state=OrderState.NEW)
        if created.parent_order_id is None:
            matches = candidate == created
        else:
            matches = (
                replace(candidate, quantity=created.quantity) == created
                and candidate.quantity <= created.quantity
            )
        if not matches:
            raise IdempotencyConflictError(
                f"order_id '{order.order_id}' was reused with a different order payload"
            )

    def _context(self, order_id: str) -> _OrderContext:
        for state in self._ledger.fold().values():
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
        return self._ledger.fold().get(account_id, AccountState(account_id=account_id))

    def _planned_quantity(self, order_id: str) -> Decimal:
        return self._planned_order(order_id).quantity

    def _planned_order(self, order_id: str) -> Order:
        for event in self._ledger.events():
            if event.kind is EventKind.ORDERS_CREATED:
                for order in event.payload.orders:
                    if order.order_id == order_id:
                        return order
        raise KeyError(f"No creation event contains order '{order_id}'")

    def _has_unresolved_replace(self, order_id: str) -> bool:
        for event in self._ledger.events():
            if (
                event.kind is not EventKind.ORDER_PENDING
                or event.payload.order_id != order_id
                or event.payload.reason is None
                or not event.payload.reason.startswith("Replace pending:")
                or event.command_id is None
                or not event.command_id.endswith(":pending")
            ):
                continue
            command_id = event.command_id.removesuffix(":pending")
            if (
                self._ledger.event_by_command(f"{command_id}:accepted") is None
                and self._ledger.event_by_command(f"{command_id}:rejected") is None
            ):
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
            if (
                existing.account != account_id
                or existing.kind is not kind
                or existing.payload != payload
            ):
                raise IdempotencyConflictError(
                    f"command_id '{command_id}' was replayed with a different payload"
                )
            return existing
        return self._ledger.append(event)

    @staticmethod
    def _bracket_fingerprint(intent: OrderIntent, quantity: Decimal) -> str:
        payload = {
            "intent_id": intent.intent_id,
            "account_id": intent.account_id,
            "instrument": encode_payload(intent.instrument),
            "side": intent.side.value,
            "quantity_rule": intent.quantity_rule,
            "quantity": str(quantity),
            "entry_price": str(intent.entry_price),
            "stop_loss": str(intent.stop_loss),
            "profit_targets": [str(value) for value in intent.profit_targets],
            "reason": intent.reason,
            "command_id": intent.command_id,
            "entry_tif": intent.entry_tif.value,
            "exit_tif": intent.exit_tif.value,
        }
        if intent.entry_type is not OrderType.LIMIT:
            # Added only when set, so brackets persisted before stop entries keep their
            # fingerprints and still replay idempotently (I3).
            payload["entry_type"] = intent.entry_type.value
        if intent.target_fractions is not None:
            payload["target_fractions"] = [str(value) for value in intent.target_fractions]
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    @staticmethod
    def _fingerprint_order(order: Order) -> str:
        encoded = json.dumps(encode_payload(order), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @staticmethod
    def _validate_quantity(instrument: Instrument, quantity: Decimal) -> None:
        if isinstance(instrument, Equity) and quantity != quantity.to_integral_value():
            raise ValueError(f"Equity order quantity must be a whole number of shares, got {quantity}")

    @classmethod
    def _split_quantity(
        cls, quantity: Decimal, count: int, instrument: Instrument
    ) -> tuple[Decimal, ...]:
        if count == 0:
            return ()
        cls._validate_quantity(instrument, quantity)
        portions = cls._allocate_quantity(
            quantity, tuple(Decimal("1") for _ in range(count)), instrument
        )
        if any(portion <= 0 for portion in portions):
            raise ValueError("quantity is too small to allocate a positive amount to each target")
        return portions

    @classmethod
    def _fraction_quantities(
        cls, quantity: Decimal, fractions: tuple[Decimal, ...], instrument: Instrument
    ) -> tuple[Decimal, ...]:
        """Size each target to its share of the position; any remainder is the runner.

        Equities round by largest remainder over the targets plus the runner, so the
        shares always add up to the entry and no target silently rounds to zero.
        """
        cls._validate_quantity(instrument, quantity)
        runner = Decimal("1") - sum(fractions, Decimal("0"))
        weights = (*fractions, runner) if runner > 0 else fractions
        if isinstance(instrument, Equity):
            exact = [quantity * weight for weight in weights]
            portions = [value.to_integral_value(rounding=ROUND_FLOOR) for value in exact]
            remaining = int(quantity - sum(portions, Decimal("0")))
            by_remainder = sorted(
                range(len(weights)), key=lambda index: (-(exact[index] - portions[index]), index)
            )
            for index in by_remainder[:remaining]:
                portions[index] += 1
        else:
            portions = [quantity * weight for weight in weights]
        targets = tuple(portions[: len(fractions)])
        if any(portion <= 0 for portion in targets):
            raise ValueError(
                f"quantity {quantity} is too small to give every target its fraction"
            )
        return targets

    @staticmethod
    def _allocate_quantity(
        quantity: Decimal, weights: tuple[Decimal, ...], instrument: Instrument
    ) -> tuple[Decimal, ...]:
        if not weights:
            return ()
        if any(weight <= 0 for weight in weights):
            raise ValueError("target allocation weights must be positive")
        total_weight = sum(weights, Decimal("0"))
        if isinstance(instrument, Equity):
            OrderManager._validate_quantity(instrument, quantity)
            if any(weight != weight.to_integral_value() for weight in weights):
                raise ValueError("Equity target allocation weights must be whole shares")
            shares = int(quantity)
            integer_weights = tuple(int(weight) for weight in weights)
            denominator = sum(integer_weights)
            allocations, remainders = zip(
                *(divmod(shares * weight, denominator) for weight in integer_weights),
                strict=True,
            )
            remaining = shares - sum(allocations)
            order = sorted(
                range(len(weights)),
                key=lambda index: (-remainders[index], index),
            )
            adjusted = list(allocations)
            for index in order[:remaining]:
                adjusted[index] += 1
            return tuple(Decimal(value) for value in adjusted)

        portions = tuple(quantity * weight / total_weight for weight in weights)
        return (*portions[:-1], quantity - sum(portions[:-1], Decimal("0")))

    def _bracket_from_orders(self, orders: tuple[Order, ...]) -> Bracket:
        by_id = {order.order_id: self._stored_order(order) for order in orders}
        entry = next(order for order in by_id.values() if order.parent_order_id is None)
        # The protective stop is the entry's STOP child; a stop entry is STOP too.
        stop = next(
            order
            for order in by_id.values()
            if order.parent_order_id == entry.order_id and order.order_type is OrderType.STOP
        )
        targets = tuple(
            sorted(
                (
                    order
                    for order in by_id.values()
                    if order.parent_order_id == entry.order_id
                    and order.order_type is OrderType.LIMIT
                ),
                key=lambda order: order.order_id,
            )
        )
        return Bracket(entry, stop, targets)

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

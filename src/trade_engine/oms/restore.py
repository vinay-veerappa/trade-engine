"""Restore an in-memory venue's book from the ledger fold (I2, shared by both runners).

A new process starts with an empty venue while the ledger holds every working order,
fill and position. ``restorable`` hands those back; anything inconsistent refuses
rather than being patched up (I5).

A ``PENDING_UNKNOWN`` order is one whose venue answer never reached the ledger: the
process died between asking the venue and recording what it said. The EOD runner
refuses it (``pending="refuse"``). The intraday service cannot wait for an operator
mid-session, so it asks for ``pending="resolve"``: the in-memory venue that held the
order died with the process, and the rebuilt venue's book is whatever the ledger says,
so the request the ledger recorded is carried out on the new venue —

- a pending *submit* is restored as working (the order was sent; nothing filled it,
  since a fill is recorded at the snapshot that made it, before any later request);
- a pending *cancel* is restored as cancelled (the cancel was asked of a working order,
  which this venue always honours);

and the caller reads each one back through the OMS (``reconcile_order``), so the ledger
records the resolution as a venue read-back. Any other pending request is left out and
reported, and the caller refuses what depends on it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from trade_engine.domain.orders import OrderState
from trade_engine.interfaces.broker import (
    VenueFill,
    VenueOrder,
    VenueOrderAllocation,
    VenuePosition,
)
from trade_engine.ledger import EventKind
from trade_engine.ledger.state import AccountState

MIN_TIME = datetime.min.replace(tzinfo=timezone.utc)
# States in which the ledger says the venue holds the order.
VENUE_WORKING = frozenset(
    {
        OrderState.SUBMITTED,
        OrderState.ACCEPTED,
        OrderState.PARTIALLY_FILLED,
        OrderState.PENDING_UNKNOWN,
    }
)


class RestoreError(RuntimeError):
    """The ledger cannot rebuild the venue's book (I5)."""


@dataclass
class PendingResolution:
    """What ``pending="resolve"`` did with the ledger's PENDING_UNKNOWN orders."""

    resolved: list[str] = field(default_factory=list)  # read these back after restore
    unresolved: dict[str, str] = field(default_factory=dict)  # order id -> why not


def pending_request(ledger: Any, account_id: str, order_id: str) -> str:
    """Which request left ``order_id`` pending: ``submit``, ``cancel`` or ``unknown``."""
    last = None
    for event in ledger.events(account=account_id):
        if event.kind is EventKind.ORDER_PENDING and event.payload.order_id == order_id:
            last = event
    if last is None:
        return "unknown"
    command = last.command_id or ""
    if command.endswith(":submit-pending"):
        return "submit"
    if command.endswith(":pending") and str(last.payload.reason).startswith("Cancel pending"):
        return "cancel"
    return "unknown"


def restorable(
    ledger: Any,
    account_id: str,
    state: AccountState,
    *,
    pending: str = "refuse",
    resolution: PendingResolution | None = None,
) -> tuple[list[tuple[VenueOrder, Any]], list[VenueFill]]:
    """Working orders plus their brackets: a child's cap needs its parent's fills
    and its siblings' exits. NEW orders were never sent, so they stay out."""
    if pending not in ("refuse", "resolve"):
        raise ValueError(f"pending must be 'refuse' or 'resolve', got {pending!r}")
    if pending == "resolve" and resolution is None:
        raise ValueError("pending='resolve' needs a PendingResolution to report into")
    wanted: set[str] = set()
    restored_as: dict[str, OrderState] = {}
    for order in state.orders.values():
        if order.state not in VENUE_WORKING:
            continue
        if order.state is OrderState.PENDING_UNKNOWN:
            if pending == "refuse":
                raise RestoreError(
                    f"Order '{order.order_id}' for '{account_id}' is PENDING_UNKNOWN; the "
                    f"simulated venue's answer is gone, reconcile it before replay (I5)"
                )
            request = pending_request(ledger, account_id, order.order_id)
            if request == "submit":
                restored_as[order.order_id] = OrderState.ACCEPTED
            elif request == "cancel":
                restored_as[order.order_id] = OrderState.CANCELLED
            else:
                resolution.unresolved[order.order_id] = (
                    f"'{order.order_id}' is PENDING_UNKNOWN after a request that cannot be "
                    f"carried out on a rebuilt venue; it stays unresolved (I5)"
                )
                continue
            resolution.resolved.append(order.order_id)
        root = order.parent_order_id or order.order_id
        wanted.add(root)
        wanted.update(
            candidate.order_id
            for candidate in state.orders.values()
            if candidate.parent_order_id == root and candidate.state is not OrderState.NEW
        )
    orders: list[tuple[VenueOrder, Any]] = []
    for order_id in sorted(wanted):
        order = state.orders[order_id]
        if resolution is not None and order_id in resolution.unresolved:
            continue
        submission = ledger.event_by_command(f"{order.command_id}:submit")
        if submission is None:
            raise RestoreError(
                f"Order '{order_id}' for '{account_id}' has no submission event; cannot "
                f"restore when it reached the venue (I5)"
            )
        venue_state = restored_as.get(order_id, order.state)
        orders.append(
            (
                VenueOrder(
                    venue_order_id=state.venue_order_ids.get(order_id, order_id),
                    instrument=order.instrument,
                    order_type=order.order_type,
                    side=order.side,
                    quantity=order.quantity,
                    submitted_at=submission.ts_utc,
                    tif=order.tif,
                    limit_price=order.limit_price,
                    stop_price=order.stop_price,
                    trail_amount=order.trail_amount,
                    allocations=(
                        VenueOrderAllocation(order_id, order.account_id, order.quantity),
                    ),
                    parent_order_id=order.parent_order_id,
                    oco_group=order.oco_group,
                ),
                venue_state,
            )
        )
    kept = {venue_order.allocations[0].strategy_order_id for venue_order, _ in orders}
    fills = [
        VenueFill(
            venue_fill_id=fill.venue_execution_id or fill.fill_id,
            venue_order_id=state.venue_order_ids.get(fill.order_id, fill.order_id),
            instrument=fill.instrument,
            quantity=fill.quantity,
            price=fill.price,
            filled_at=fill.filled_at,
            side=fill.side,
            fee=fill.fee,
            leg_id=fill.leg_id,
        )
        for fill in state.fills
        if fill.order_id in kept
    ]
    return orders, fills


def restorable_positions(state: AccountState) -> list[VenuePosition]:
    positions = []
    for instrument, position in state.positions.items():
        if position.quantity == Decimal("0"):
            continue
        # SimBroker reads only the quantity; as_of dates it by its latest fill.
        as_of = max(
            (fill.filled_at for fill in state.fills if fill.instrument == instrument),
            default=MIN_TIME,
        )
        positions.append(
            VenuePosition(
                instrument=instrument,
                quantity=position.quantity,
                avg_price=position.avg_cost,
                as_of=as_of,
            )
        )
    return positions

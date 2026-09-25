"""Restore an in-memory venue's book from the ledger fold (I2, shared by both runners).

A new process starts with an empty venue while the ledger holds every working order,
fill and position. ``restorable_book`` hands those back; anything inconsistent refuses
rather than being patched up (I5).
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from trade_engine.domain.orders import OrderState
from trade_engine.eod.runner import EodRunnerError, _VENUE_WORKING
from trade_engine.ledger.state import AccountState
from trade_engine.interfaces.broker import (
    VenueFill,
    VenueOrder,
    VenueOrderAllocation,
    VenuePosition,
)

MIN_TIME = datetime.min.replace(tzinfo=timezone.utc)


def restorable(
    ledger: Any, account_id: str, state: AccountState
) -> tuple[list[tuple[VenueOrder, Any]], list[VenueFill]]:
    """Working orders plus their brackets: a child's cap needs its parent's fills
    and its siblings' exits. NEW orders were never sent, so they stay out."""
    wanted: set[str] = set()
    for order in state.orders.values():
        if order.state not in _VENUE_WORKING:
            continue
        if order.state is OrderState.PENDING_UNKNOWN:
            raise EodRunnerError(
                f"Order '{order.order_id}' for '{account_id}' is PENDING_UNKNOWN; the "
                f"simulated venue's answer is gone, reconcile it before replay (I5)"
            )
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
        submission = ledger.event_by_command(f"{order.command_id}:submit")
        if submission is None:
            raise EodRunnerError(
                f"Order '{order_id}' for '{account_id}' has no submission event; cannot "
                f"restore when it reached the venue (I5)"
            )
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
                order.state,
            )
        )
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
        if fill.order_id in wanted
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
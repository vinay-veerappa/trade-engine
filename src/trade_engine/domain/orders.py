"""Order definitions, state machine, and transitions."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from trade_engine.domain.instruments import Instrument, Side


class OrderType(StrEnum):
    """Order type."""

    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP = "STOP"
    STOP_LIMIT = "STOP_LIMIT"
    TRAIL = "TRAIL"


class TimeInForce(StrEnum):
    """Time-in-force instructions."""

    DAY = "DAY"
    GTC = "GTC"
    GTD = "GTD"
    OPG = "OPG"  # At the opening (Market on Open / Limit on Open)
    MOC = "MOC"  # Market on Close


class OrderState(StrEnum):
    """Order lifecycle state machine (Architecture §4.1, I10)."""

    NEW = "NEW"
    SUBMITTED = "SUBMITTED"
    ACCEPTED = "ACCEPTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    REJECTED = "REJECTED"
    PENDING_UNKNOWN = "PENDING_UNKNOWN"


class IllegalOrderStateTransitionError(Exception):
    """Raised when an illegal order state transition is attempted."""


# Valid transition map: Current state -> Allowed next states
VALID_ORDER_TRANSITIONS: dict[OrderState, frozenset[OrderState]] = {
    OrderState.NEW: frozenset(
        {
            OrderState.SUBMITTED,
            OrderState.CANCELLED,
            OrderState.REJECTED,
        }
    ),
    OrderState.SUBMITTED: frozenset(
        {
            OrderState.ACCEPTED,
            OrderState.REJECTED,
            OrderState.CANCELLED,
            OrderState.PENDING_UNKNOWN,
        }
    ),
    OrderState.ACCEPTED: frozenset(
        {
            OrderState.PARTIALLY_FILLED,
            OrderState.FILLED,
            OrderState.CANCELLED,
            OrderState.EXPIRED,
            OrderState.REJECTED,
            OrderState.PENDING_UNKNOWN,
        }
    ),
    OrderState.PARTIALLY_FILLED: frozenset(
        {
            OrderState.PARTIALLY_FILLED,
            OrderState.FILLED,
            OrderState.CANCELLED,
            OrderState.EXPIRED,
            OrderState.PENDING_UNKNOWN,
        }
    ),
    OrderState.PENDING_UNKNOWN: frozenset(
        {
            OrderState.ACCEPTED,
            OrderState.PARTIALLY_FILLED,
            OrderState.FILLED,
            OrderState.CANCELLED,
            OrderState.EXPIRED,
            OrderState.REJECTED,
        }
    ),
    # Terminal states have no valid transitions
    OrderState.FILLED: frozenset(),
    OrderState.CANCELLED: frozenset(),
    OrderState.EXPIRED: frozenset(),
    OrderState.REJECTED: frozenset(),
}


def validate_order_transition(current_state: OrderState, next_state: OrderState) -> None:
    """Validate that transition from current_state to next_state is legal.

    Raises IllegalOrderStateTransitionError if illegal.
    """
    if current_state == next_state:
        return

    allowed = VALID_ORDER_TRANSITIONS.get(current_state, frozenset())
    if next_state not in allowed:
        raise IllegalOrderStateTransitionError(
            f"Illegal order state transition from {current_state.value} to {next_state.value}"
        )



@dataclass(frozen=True)
class Order:
    """Strategy or venue order representation (frozen dataclass)."""

    order_id: str
    account_id: str
    instrument: Instrument
    order_type: OrderType
    side: Side
    quantity: Decimal
    command_id: str  # Idempotency key (I3)
    created_at: datetime
    limit_price: Decimal | None = None
    stop_price: Decimal | None = None
    trail_amount: Decimal | None = None
    tif: TimeInForce = TimeInForce.DAY
    state: OrderState = OrderState.NEW
    parent_order_id: str | None = None  # Bracket parent
    oco_group: str | None = None

    def __post_init__(self) -> None:
        if not self.order_id:
            raise ValueError("order_id must be non-empty")
        if not self.account_id:
            raise ValueError("account_id must be non-empty")
        if not self.command_id:
            raise ValueError("command_id must be non-empty (I3)")
        if self.quantity <= Decimal("0"):
            raise ValueError(f"Order quantity must be positive, got {self.quantity} (I5)")
        if self.order_type == OrderType.LIMIT and self.limit_price is None:
            raise ValueError("LIMIT order must have a limit_price")
        if self.order_type == OrderType.STOP and self.stop_price is None:
            raise ValueError("STOP order must have a stop_price")
        if self.order_type == OrderType.STOP_LIMIT and (
            self.limit_price is None or self.stop_price is None
        ):
            raise ValueError("STOP_LIMIT order must have both limit_price and stop_price")
        if self.order_type == OrderType.TRAIL and (
            self.trail_amount is None or self.trail_amount <= Decimal("0")
        ):
            raise ValueError("TRAIL order must have a positive trail_amount (I5)")
        if self.limit_price is not None and self.limit_price <= Decimal("0"):
            raise ValueError(f"limit_price must be positive, got {self.limit_price}")
        if self.stop_price is not None and self.stop_price <= Decimal("0"):
            raise ValueError(f"stop_price must be positive, got {self.stop_price}")

    def transition_to(self, next_state: OrderState) -> Order:
        """Return a new Order instance with the updated state after validating transition."""
        validate_order_transition(self.state, next_state)
        return replace(self, state=next_state)

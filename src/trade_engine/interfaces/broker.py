"""Broker adapter protocol and venue data transfer structures (Architecture §4.5)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from types import MappingProxyType
from typing import Literal, Protocol, runtime_checkable

from trade_engine.domain.instruments import Instrument, Side
from trade_engine.domain.orders import OrderState, OrderType, TimeInForce, validate_order_prices


class UnsupportedCapability(Exception):
    """The venue cannot express this order (an order type, TIF, instrument or leg count
    its adapter does not declare). Refuse with the reason; never approximate (I5, §4.5)."""


@dataclass(frozen=True)
class Capabilities:
    """Venue capabilities declared by the broker adapter."""

    supported_order_types: frozenset[OrderType]
    supported_tifs: frozenset[TimeInForce]
    supports_multi_leg: bool
    supports_native_stops: bool
    supports_streaming: bool


@dataclass(frozen=True)
class VenueIdentity:
    """Venue identity proven at connect time (I10)."""

    account_id: str
    env: Literal["sim", "paper", "live"]
    connected_at: datetime
    broker_name: str

    def __post_init__(self) -> None:
        if self.env not in ("sim", "paper", "live"):
            raise ValueError(f"Invalid venue env '{self.env}' (I10)")
        if self.connected_at.tzinfo is None or self.connected_at.tzinfo.utcoffset(self.connected_at) is None:
            raise ValueError("connected_at must be timezone-aware UTC datetime (I7)")


@dataclass(frozen=True)
class VenueOrderAllocation:
    """Allocation of a venue order portion back to a specific strategy order (Architecture §4.4)."""

    strategy_order_id: str
    account_id: str
    quantity: Decimal

    def __post_init__(self) -> None:
        if not self.strategy_order_id:
            raise ValueError("strategy_order_id must be non-empty")
        if not self.account_id:
            raise ValueError("account_id must be non-empty")
        if self.quantity <= Decimal("0"):
            raise ValueError(f"quantity must be positive, got {self.quantity}")


@dataclass(frozen=True)
class VenueOrder:
    """Order submitted to a specific venue (supports 1:1 or netted strategy orders, Architecture §4.4)."""

    venue_order_id: str
    instrument: Instrument
    order_type: OrderType
    side: Side
    quantity: Decimal
    submitted_at: datetime
    tif: TimeInForce = TimeInForce.DAY
    limit_price: Decimal | None = None
    stop_price: Decimal | None = None
    trail_amount: Decimal | None = None
    allocations: tuple[VenueOrderAllocation, ...] = ()
    parent_order_id: str | None = None
    oco_group: str | None = None

    def __post_init__(self) -> None:
        if not self.venue_order_id:
            raise ValueError("venue_order_id must be non-empty")
        if self.quantity <= Decimal("0"):
            raise ValueError(f"quantity must be positive, got {self.quantity}")
        if self.submitted_at.tzinfo is None or self.submitted_at.tzinfo.utcoffset(self.submitted_at) is None:
            raise ValueError("submitted_at must be timezone-aware UTC datetime (I7)")
        if not isinstance(self.side, Side):
            raise ValueError(f"Invalid side '{self.side}'")
        validate_order_prices(self.order_type, self.limit_price, self.stop_price, self.trail_amount)
        # Every venue order must allocate back to strategy orders, or its fills are orphaned (§4.4)
        if not self.allocations:
            raise ValueError("VenueOrder must carry at least one strategy-order allocation")
        allocated = sum((a.quantity for a in self.allocations), Decimal("0"))
        if allocated != self.quantity:
            raise ValueError(
                f"VenueOrder allocations total {allocated} but order quantity is {self.quantity}"
            )


@dataclass(frozen=True)
class VenueAck:
    """Venue acknowledgment of an order action."""

    venue_order_id: str
    status: Literal["ACCEPTED", "REJECTED", "PENDING"]
    timestamp: datetime
    message: str | None = None

    def __post_init__(self) -> None:
        if self.timestamp.tzinfo is None or self.timestamp.tzinfo.utcoffset(self.timestamp) is None:
            raise ValueError("timestamp must be timezone-aware UTC datetime (I7)")


@dataclass(frozen=True)
class OrderChanges:
    """Changes requested in an order replace/modify request."""

    new_quantity: Decimal | None = None
    new_limit_price: Decimal | None = None
    new_stop_price: Decimal | None = None


@dataclass(frozen=True)
class VenueOrderState:
    """Venue-reported order status."""

    venue_order_id: str
    state: OrderState
    filled_quantity: Decimal
    remaining_quantity: Decimal
    updated_at: datetime

    def __post_init__(self) -> None:
        if self.updated_at.tzinfo is None or self.updated_at.tzinfo.utcoffset(self.updated_at) is None:
            raise ValueError("updated_at must be timezone-aware UTC datetime (I7)")


@dataclass(frozen=True)
class VenueFill:
    """Venue-reported fill event."""

    venue_fill_id: str
    venue_order_id: str
    instrument: Instrument
    quantity: Decimal
    price: Decimal
    filled_at: datetime
    side: Side  # Required: a defaulted side would guess the direction (I5)
    fee: Decimal = Decimal("0")
    # A combo order is reported leg by leg: the index of the leg this fill executed.
    leg_id: str | None = None

    def __post_init__(self) -> None:
        if self.quantity <= Decimal("0"):
            raise ValueError(f"VenueFill quantity must be strictly positive, got {self.quantity}")
        if self.price <= Decimal("0"):
            raise ValueError(f"VenueFill price must be strictly positive, got {self.price} (I5)")
        if self.filled_at.tzinfo is None or self.filled_at.tzinfo.utcoffset(self.filled_at) is None:
            raise ValueError("filled_at must be timezone-aware UTC datetime (I7)")
        if not isinstance(self.side, Side):
            raise ValueError(f"Invalid side '{self.side}'")


@dataclass(frozen=True)
class VenuePosition:
    """Venue-reported account position."""

    instrument: Instrument
    quantity: Decimal
    avg_price: Decimal
    as_of: datetime

    def __post_init__(self) -> None:
        if self.as_of.tzinfo is None or self.as_of.tzinfo.utcoffset(self.as_of) is None:
            raise ValueError("as_of must be timezone-aware UTC datetime (I7)")


@dataclass(frozen=True)
class VenueCashEvent:
    """Venue-reported cash or corporate action event."""

    event_id: str
    event_type: str  # dividend, assignment, exercise, interest, fee
    amount: Decimal
    timestamp: datetime
    details: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.timestamp.tzinfo is None or self.timestamp.tzinfo.utcoffset(self.timestamp) is None:
            raise ValueError("timestamp must be timezone-aware UTC datetime (I7)")
        if self.details is not None and not isinstance(self.details, MappingProxyType):
            object.__setattr__(self, "details", MappingProxyType(dict(self.details)))
        elif self.details is None:
            object.__setattr__(self, "details", MappingProxyType({}))


@runtime_checkable
class BrokerAdapter(Protocol):
    """Protocol for all broker adapters (Architecture §4.5, I13).

    No strategy knowledge belongs in this adapter.
    """

    name: str
    env: Literal["sim", "paper", "live"]
    capabilities: Capabilities

    def connect(self) -> VenueIdentity:
        """Prove environment and account identity. Live requires explicit ack token."""
        ...

    def submit(self, order: VenueOrder) -> VenueAck:
        """Submit an order to the venue."""
        ...

    def cancel(self, venue_order_id: str) -> VenueAck:
        """Cancel a resting venue order."""
        ...

    def replace(self, venue_order_id: str, changes: OrderChanges) -> VenueAck:
        """Replace or modify a resting venue order."""
        ...

    def orders(self, since: datetime) -> list[VenueOrderState]:
        """Fetch venue order states updated since given timestamp."""
        ...

    def fills(self, since: datetime) -> list[VenueFill]:
        """Fetch venue fills executed since given timestamp."""
        ...

    def positions(self) -> list[VenuePosition]:
        """Fetch all positions held at the venue."""
        ...

    def cash_events(self, since: datetime) -> list[VenueCashEvent]:
        """Fetch cash events (dividends, assignments, exercises) since given timestamp."""
        ...

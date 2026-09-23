"""Broker adapter protocol and venue data transfer structures (Architecture §4.5)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Literal, Protocol, runtime_checkable

from trade_engine.domain.instruments import Instrument
from trade_engine.domain.orders import Order, OrderState, OrderType, TimeInForce


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


@dataclass(frozen=True)
class VenueOrder:
    """Order submitted to a specific venue."""

    venue_order_id: str
    order: Order
    submitted_at: datetime


@dataclass(frozen=True)
class VenueAck:
    """Venue acknowledgment of an order action."""

    venue_order_id: str
    status: Literal["ACCEPTED", "REJECTED", "PENDING"]
    timestamp: datetime
    message: str | None = None


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


@dataclass(frozen=True)
class VenueFill:
    """Venue-reported fill event."""

    venue_fill_id: str
    venue_order_id: str
    instrument: Instrument
    quantity: Decimal
    price: Decimal
    filled_at: datetime
    fee: Decimal = Decimal("0")


@dataclass(frozen=True)
class VenuePosition:
    """Venue-reported account position."""

    instrument: Instrument
    quantity: Decimal
    avg_price: Decimal
    as_of: datetime


@dataclass(frozen=True)
class VenueCashEvent:
    """Venue-reported cash or corporate action event."""

    event_id: str
    event_type: str  # dividend, assignment, exercise, interest, fee
    amount: Decimal
    timestamp: datetime
    details: dict[str, str] = field(default_factory=dict)


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

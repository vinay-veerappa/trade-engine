"""Event vocabulary for the append-only ledger (Architecture §4.2).

The ledger stores one row per event; this module defines what an event *is* and
which payload type each kind must carry. Nothing here performs I/O.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from enum import StrEnum
from typing import Any

from trade_engine.domain.instruments import Instrument
from trade_engine.domain.orders import Order
from trade_engine.domain.portfolio import Fill
from trade_engine.domain.risk import RiskVerdict
from trade_engine.domain.signals import Signal
from trade_engine.interfaces.market_data import CorporateAction

SCHEMA_VERSION = 1

# The §4.2 CashFlow parenthetical: interest, borrow, fees, deposits.
CASH_FLOW_KINDS = ("interest", "borrow", "fee", "deposit", "withdrawal")


class EventKind(StrEnum):
    """The §4.2 event vocabulary. Values are the canonical stored spellings."""

    SIGNAL_SEEN = "SignalSeen"
    RISK_VERDICT = "RiskVerdict"
    ORDERS_CREATED = "OrdersCreated"
    ORDER_SUBMITTED = "OrderSubmitted"
    ORDER_UPDATED = "OrderUpdated"
    ORDER_PENDING = "OrderPending"
    ORDER_ACCEPTED = "OrderAccepted"
    ORDER_REJECTED = "OrderRejected"
    ORDER_CANCELLED = "OrderCancelled"
    ORDER_REFUSED = "OrderRefused"
    ORDER_EXPIRED = "OrderExpired"
    ORDER_EMULATION_UPDATED = "OrderEmulationUpdated"
    FILL = "Fill"
    ASSIGNMENT = "Assignment"
    EXERCISE = "Exercise"
    EXPIRY = "Expiry"
    CORPORATE_ACTION = "CorporateAction"
    CASH_FLOW = "CashFlow"
    MARK = "Mark"
    VENUE_RECONCILE = "VenueReconcile"


class EventPayloadError(ValueError):
    """Raised when an event payload does not match its declared kind."""


class UnhandledEventError(RuntimeError):
    """Raised by fold() for a kind whose semantics are owned by a later work package.

    E1 refuses rather than inventing semantics it does not own (I5). See FOLD_OWNERS.
    """


# Kinds whose ledger semantics are deliberately out of E1's scope. Options expiry,
# assignment, exercise and corporate actions are owned by O2 (lifecycle).
FOLD_OWNERS: dict[EventKind, str] = {
    EventKind.ASSIGNMENT: "O2 (options lifecycle)",
    EventKind.EXERCISE: "O2 (options lifecycle)",
    EventKind.EXPIRY: "O2 (options lifecycle)",
    EventKind.CORPORATE_ACTION: "O2 (options lifecycle)",
}


def _require_utc(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise EventPayloadError(f"{name} must be timezone-aware UTC datetime (I7)")


def _as_decimal(value: Any, name: str) -> Decimal:
    dec = value if isinstance(value, Decimal) else Decimal(str(value))
    if not dec.is_finite():
        raise EventPayloadError(f"{name} must be finite, got {dec}")
    return dec


@dataclass(frozen=True)
class OrderStateChange:
    """A venue- or OMS-driven order state move (accepted/rejected/cancelled/expired)."""

    order_id: str
    reason: str | None = None
    venue_order_id: str | None = None

    def __post_init__(self) -> None:
        if not self.order_id:
            raise EventPayloadError("OrderStateChange.order_id must be non-empty")
        if self.reason is not None and not self.reason:
            raise EventPayloadError("OrderStateChange.reason must be non-empty when provided")


@dataclass(frozen=True)
class OrdersCreated:
    """A bracket's orders and payload fingerprint, appended atomically as one command."""

    orders: tuple[Order, ...]
    fingerprint: str
    reason: str

    def __post_init__(self) -> None:
        if not self.orders:
            raise EventPayloadError("OrdersCreated.orders must not be empty")
        if not self.fingerprint:
            raise EventPayloadError("OrdersCreated.fingerprint must be non-empty")
        if not self.reason:
            raise EventPayloadError("OrdersCreated.reason must be non-empty")
        if len({order.order_id for order in self.orders}) != len(self.orders):
            raise EventPayloadError("OrdersCreated.order_id values must be unique")
        accounts = {order.account_id for order in self.orders}
        if len(accounts) != 1:
            raise EventPayloadError("OrdersCreated orders must belong to one account")


@dataclass(frozen=True)
class OrderUpdated:
    """A complete immutable order replacement with its decision reason."""

    order: Order
    reason: str
    venue_order_id: str | None = None

    def __post_init__(self) -> None:
        if not self.reason:
            raise EventPayloadError("OrderUpdated.reason must be non-empty")


@dataclass(frozen=True)
class EmulatedOrderState:
    """Persisted working state for a locally emulated order."""

    order_id: str
    observed_price: Decimal | None
    extreme: Decimal | None
    stop_price: Decimal | None
    triggered: bool
    reason: str

    def __post_init__(self) -> None:
        if not self.order_id:
            raise EventPayloadError("EmulatedOrderState.order_id must be non-empty")
        if self.observed_price is not None:
            object.__setattr__(
                self, "observed_price", _as_decimal(self.observed_price, "observed_price")
            )
            if self.observed_price <= 0:
                raise EventPayloadError("EmulatedOrderState.observed_price must be positive")
        if self.extreme is not None:
            object.__setattr__(self, "extreme", _as_decimal(self.extreme, "extreme"))
            if self.extreme <= 0:
                raise EventPayloadError("EmulatedOrderState.extreme must be positive")
        if self.stop_price is not None:
            object.__setattr__(self, "stop_price", _as_decimal(self.stop_price, "stop_price"))
            if self.stop_price <= 0:
                raise EventPayloadError("EmulatedOrderState.stop_price must be positive")
        if not self.reason:
            raise EventPayloadError("EmulatedOrderState.reason must be non-empty")


@dataclass(frozen=True)
class CashFlow:
    """A signed cash movement that is not a fill: interest, borrow, fee, deposit."""

    amount: Decimal
    kind: str
    as_of: datetime
    note: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "amount", _as_decimal(self.amount, "CashFlow.amount"))
        if self.kind not in CASH_FLOW_KINDS:
            raise EventPayloadError(
                f"CashFlow.kind must be one of {CASH_FLOW_KINDS}, got '{self.kind}'"
            )
        _require_utc(self.as_of, "CashFlow.as_of")


@dataclass(frozen=True)
class Mark:
    """A mark-to-market price for one instrument (Architecture §4.2, Mark = daily MTM)."""

    instrument: Instrument
    price: Decimal
    as_of: datetime
    source: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.instrument, Instrument):
            raise EventPayloadError("Mark.instrument must be an Instrument (I6)")
        object.__setattr__(self, "price", _as_decimal(self.price, "Mark.price"))
        if self.price <= Decimal("0"):
            raise EventPayloadError(f"Mark.price must be positive, got {self.price} (I5)")
        _require_utc(self.as_of, "Mark.as_of")


@dataclass(frozen=True)
class VenueReconcile:
    """Result of comparing ledger positions against a venue's reported positions (§4.5)."""

    venue: str
    as_of: datetime
    reconciled: bool
    drift: tuple[str, ...] = ()
    note: str | None = None

    def __post_init__(self) -> None:
        if not self.venue:
            raise EventPayloadError("VenueReconcile.venue must be non-empty")
        _require_utc(self.as_of, "VenueReconcile.as_of")
        object.__setattr__(self, "drift", tuple(self.drift))
        if self.reconciled and self.drift:
            raise EventPayloadError(
                "VenueReconcile cannot be reconciled while listing drift instruments"
            )
        if not self.reconciled and not self.drift:
            raise EventPayloadError(
                "VenueReconcile that is not reconciled must name the drifting instruments (I11)"
            )


@dataclass(frozen=True)
class LifecycleNotice:
    """A stated position/cash effect from an option lifecycle event.

    This carries the *exact* effect the lifecycle owner computed; E1 never infers
    intrinsic value (I5). fold() refuses these kinds until O2 registers handlers.
    """

    instrument: Instrument
    quantity_delta: Decimal
    cash_delta: Decimal
    as_of: datetime
    note: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.instrument, Instrument):
            raise EventPayloadError("LifecycleNotice.instrument must be an Instrument (I6)")
        object.__setattr__(
            self, "quantity_delta", _as_decimal(self.quantity_delta, "LifecycleNotice.quantity_delta")
        )
        object.__setattr__(
            self, "cash_delta", _as_decimal(self.cash_delta, "LifecycleNotice.cash_delta")
        )
        _require_utc(self.as_of, "LifecycleNotice.as_of")


# Each kind must carry exactly this payload type; anything else is refused (I5).
PAYLOAD_TYPES: dict[EventKind, type] = {
    EventKind.SIGNAL_SEEN: Signal,
    EventKind.RISK_VERDICT: RiskVerdict,
    EventKind.ORDERS_CREATED: OrdersCreated,
    EventKind.ORDER_SUBMITTED: Order,
    EventKind.ORDER_UPDATED: OrderUpdated,
    EventKind.ORDER_PENDING: OrderStateChange,
    EventKind.ORDER_ACCEPTED: OrderStateChange,
    EventKind.ORDER_REJECTED: OrderStateChange,
    EventKind.ORDER_CANCELLED: OrderStateChange,
    EventKind.ORDER_REFUSED: OrderStateChange,
    EventKind.ORDER_EXPIRED: OrderStateChange,
    EventKind.ORDER_EMULATION_UPDATED: EmulatedOrderState,
    EventKind.FILL: Fill,
    EventKind.ASSIGNMENT: LifecycleNotice,
    EventKind.EXERCISE: LifecycleNotice,
    EventKind.EXPIRY: LifecycleNotice,
    EventKind.CORPORATE_ACTION: CorporateAction,
    EventKind.CASH_FLOW: CashFlow,
    EventKind.MARK: Mark,
    EventKind.VENUE_RECONCILE: VenueReconcile,
}


@dataclass(frozen=True)
class Event:
    """A single immutable ledger event. `seq` is assigned by the ledger on append."""

    account: str
    kind: EventKind
    payload: Any
    ts_utc: datetime
    command_id: str | None = None
    schema_version: int = SCHEMA_VERSION
    seq: int | None = None

    def __post_init__(self) -> None:
        if not self.account:
            raise EventPayloadError("Event.account must be non-empty")
        if not isinstance(self.kind, EventKind):
            try:
                object.__setattr__(self, "kind", EventKind(self.kind))
            except ValueError as err:
                raise EventPayloadError(f"Unknown event kind '{self.kind}'") from err
        _require_utc(self.ts_utc, "Event.ts_utc")
        # Aware is not enough: an ET-aware timestamp would be stored with its -04:00
        # offset, and the column is named ts_utc for a reason (I7).
        object.__setattr__(self, "ts_utc", self.ts_utc.astimezone(timezone.utc))
        if self.command_id is not None and not self.command_id:
            raise EventPayloadError("Event.command_id must be non-empty when provided (I3)")
        if not isinstance(self.schema_version, int) or self.schema_version <= 0:
            raise EventPayloadError(
                f"Event.schema_version must be a positive int, got {self.schema_version}"
            )
        if self.schema_version > SCHEMA_VERSION:
            raise EventPayloadError(
                f"Event.schema_version {self.schema_version} is newer than this engine "
                f"understands ({SCHEMA_VERSION}); refusing rather than mis-reading it (I5)"
            )
        if self.seq is not None and (not isinstance(self.seq, int) or self.seq <= 0):
            raise EventPayloadError(f"Event.seq must be a positive int, got {self.seq}")

        expected = PAYLOAD_TYPES[self.kind]
        if not isinstance(self.payload, expected):
            raise EventPayloadError(
                f"{self.kind.value} payload must be {expected.__name__}, "
                f"got {type(self.payload).__name__}"
            )

        payload_account = getattr(self.payload, "account_id", None)
        if isinstance(self.payload, OrderUpdated):
            payload_account = self.payload.order.account_id
        if payload_account is not None and payload_account != self.account:
            raise EventPayloadError(
                f"{self.kind.value} payload belongs to account '{payload_account}' but the "
                f"event is filed under '{self.account}' (I8)"
            )
        if isinstance(self.payload, OrdersCreated) and any(
            order.account_id != self.account for order in self.payload.orders
        ):
            raise EventPayloadError(
                f"{self.kind.value} contains orders for a different account (I8)"
            )

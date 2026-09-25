"""Event vocabulary for the append-only ledger (Architecture §4.2).

The ledger stores one row per event; this module defines what an event *is* and
which payload type each kind must carry. Nothing here performs I/O.

The ``Mirror*`` kinds record a venue mirror (T2, the thinkorswim paperMoney mirror,
§4.7): tickets queued, strategy orders refused at the venue only, send outcomes, and the
venue's own cumulative fills. They are filed under the venue's ledger account
(:func:`mirror_account`) and fold into a separate per-venue mirror state
(``ledger.mirror``) — never into any account's sim positions or cash.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import StrEnum
from typing import Any

from trade_engine.domain.instruments import Combo, Instrument, OptionContract, Side
from trade_engine.domain.orders import Order, OrderState, OrderType, TimeInForce
from trade_engine.domain.portfolio import Fill
from trade_engine.domain.risk import RiskControlChange, RiskVerdict
from trade_engine.domain.signals import Signal
from trade_engine.interfaces.market_data import CorporateAction

SCHEMA_VERSION = 1

# The §4.2 CashFlow parenthetical: interest, borrow, fees, deposits.
CASH_FLOW_KINDS = ("interest", "borrow", "fee", "deposit", "withdrawal", "dividend")


class EventKind(StrEnum):
    """The §4.2 event vocabulary. Values are the canonical stored spellings."""

    SIGNAL_SEEN = "SignalSeen"
    RISK_VERDICT = "RiskVerdict"
    ORDERS_CREATED = "OrdersCreated"
    RISK_CONTROL = "RiskControl"
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
    EOD_RUN = "EodRun"
    MIRROR_QUEUED = "MirrorQueued"
    MIRROR_REFUSED = "MirrorRefused"
    MIRROR_ACK = "MirrorAck"
    MIRROR_FILL = "MirrorFill"


class EventPayloadError(ValueError):
    """Raised when an event payload does not match its declared kind."""


class UnhandledEventError(RuntimeError):
    """Raised by fold() for a kind whose semantics are owned by a later work package.

    The fold refuses rather than inventing semantics nobody owns yet (I5). See FOLD_OWNERS.
    """


# Kinds whose ledger semantics no work package has taken on yet. O2 owns expiry,
# assignment and exercise (see OptionLifecycle); what a dividend, split or symbol
# change does to a holding is not modelled, so a ledger recording one refuses to fold.
FOLD_OWNERS: dict[EventKind, str] = {
    EventKind.CORPORATE_ACTION: "a later work package (dividends and splits are not modelled)",
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
class OptionLifecycle:
    """What became of an option position: it expired, was exercised or was assigned (O2).

    Filed under ``Expiry`` (worthless), ``Exercise`` (a long position, exercised) or
    ``Assignment`` (a short position, assigned). The event states the contract, how many
    contracts and which way they were held, and the underlying's price it was decided
    on; the fold works out the effect from the position's own lots (``lifecycle.rules``),
    so the effect cannot disagree with the book it applies to (I2, I9).
    """

    account_id: str
    contract: OptionContract
    quantity: Decimal  # contracts, strictly positive
    held: Side  # BUY: long contracts; SELL: short contracts
    underlying_price: Decimal  # the official settlement (or close) it was decided on
    price_source: str
    as_of: datetime
    reason: str
    early: bool = False  # assigned before expiry

    def __post_init__(self) -> None:
        if not self.account_id:
            raise EventPayloadError("OptionLifecycle.account_id must be non-empty")
        if not isinstance(self.contract, OptionContract):
            raise EventPayloadError("OptionLifecycle.contract must be an OptionContract (I6)")
        object.__setattr__(self, "quantity", _as_decimal(self.quantity, "OptionLifecycle.quantity"))
        if self.quantity <= 0 or self.quantity != self.quantity.to_integral_value():
            raise EventPayloadError(
                f"OptionLifecycle.quantity must be a positive whole number of contracts, got {self.quantity}"
            )
        if not isinstance(self.held, Side):
            raise EventPayloadError(f"OptionLifecycle.held must be a Side, got {self.held!r} (I5)")
        object.__setattr__(
            self, "underlying_price", _as_decimal(self.underlying_price, "OptionLifecycle.underlying_price")
        )
        if self.underlying_price <= 0:
            raise EventPayloadError(
                f"OptionLifecycle.underlying_price must be positive, got {self.underlying_price} (I5)"
            )
        if not self.price_source:
            raise EventPayloadError("OptionLifecycle.price_source must be non-empty (I11)")
        if not self.reason:
            raise EventPayloadError("OptionLifecycle.reason must be non-empty (I11)")
        _require_utc(self.as_of, "OptionLifecycle.as_of")
        if not isinstance(self.early, bool):
            raise EventPayloadError("OptionLifecycle.early must be a bool")


@dataclass(frozen=True)
class EodRun:
    """A completed EOD job run for one account and session (Architecture §4.9, I3).

    The marker is provenance for the scheduler, not folded state: a re-run is a no-op
    because its command id (`eod:<job>:<account>:<session>`) is already claimed, and
    "is session S complete?" is answered by reading the ledger, never by guessing.
    """

    session: date
    job: str
    account_id: str
    bars_processed: int
    at_close: datetime

    def __post_init__(self) -> None:
        if not self.job:
            raise EventPayloadError("EodRun.job must be non-empty")
        if not self.account_id:
            raise EventPayloadError("EodRun.account_id must be non-empty")
        if self.bars_processed < 0:
            raise EventPayloadError("EodRun.bars_processed must be non-negative")
        _require_utc(self.at_close, "EodRun.at_close")


# -- the venue mirror (T2, §4.7) -----------------------------------------------------

MIRROR_ACCOUNT_PREFIX = "__venue__:"
MIRROR_ACK_STATUSES = ("ACCEPTED", "REJECTED", "PENDING")
MIRROR_ORDER_TYPES = frozenset({OrderType.MARKET, OrderType.LIMIT})
MIRROR_TIFS = frozenset({TimeInForce.DAY, TimeInForce.GTC})


def mirror_account(venue: str) -> str:
    """The ledger account a venue's mirror events are filed under (one per venue).

    One account per venue lets the fold check every mirror event against that venue's
    whole mirror state on append (I2), and keeps the mirror out of every virtual
    account's sim book.
    """
    if not venue:
        raise EventPayloadError("a mirror venue must be non-empty")
    return f"{MIRROR_ACCOUNT_PREFIX}{venue}"


def _require_whole(value: Decimal, name: str, *, positive: bool) -> Decimal:
    value = _as_decimal(value, name)
    if value != value.to_integral_value() or value < 0 or (positive and value == 0):
        kind = "a positive" if positive else "a non-negative"
        raise EventPayloadError(f"{name} must be {kind} whole number of contracts, got {value} (I5)")
    return value


def _require_vertical(combo: Combo, name: str) -> None:
    """Only a 2-leg vertical is mirrored as a combo: anything else is refused (I5)."""
    legs = combo.legs
    if len(legs) != 2 or not all(isinstance(leg.contract, OptionContract) for leg in legs):
        raise EventPayloadError(f"{name}: a mirrored combo is a 2-leg option vertical, got {combo.symbol}")
    first, second = legs[0].contract, legs[1].contract
    if (
        first.underlying != second.underlying
        or first.expiry != second.expiry
        or first.right != second.right
        or first.multiplier != second.multiplier
        or first.strike == second.strike
        or legs[0].side is legs[1].side
        or legs[0].ratio != legs[1].ratio
    ):
        raise EventPayloadError(f"{name}: {combo.symbol} is not a 1:1 vertical")


@dataclass(frozen=True)
class MirrorAllocation:
    """The part of a mirror ticket one strategy order owns (§4.4).

    ``strategy_account`` is the virtual account of the strategy order — deliberately not
    ``account_id``: the event is filed under the venue's account, not this one.
    """

    strategy_order_id: str
    strategy_account: str
    quantity: Decimal

    def __post_init__(self) -> None:
        if not self.strategy_order_id:
            raise EventPayloadError("MirrorAllocation.strategy_order_id must be non-empty")
        if not self.strategy_account:
            raise EventPayloadError("MirrorAllocation.strategy_account must be non-empty")
        object.__setattr__(
            self, "quantity", _require_whole(self.quantity, "MirrorAllocation.quantity", positive=True)
        )


@dataclass(frozen=True)
class MirrorQueued:
    """A venue ticket queued for sending: written ahead of the send (I2, I3).

    ``instrument`` is one option contract, or a 2-leg vertical ``Combo`` whose legs trade
    as written; for a combo ``side`` is the price effect (SELL collects a net credit,
    BUY pays a net debit) and ``quantity`` counts spread units.
    """

    venue: str
    ticket_key: str
    instrument: Instrument
    side: Side
    quantity: Decimal
    order_type: OrderType
    limit_price: Decimal | None
    tif: TimeInForce
    allocations: tuple[MirrorAllocation, ...]
    at: datetime

    def __post_init__(self) -> None:
        if not self.venue:
            raise EventPayloadError("MirrorQueued.venue must be non-empty")
        if not self.ticket_key:
            raise EventPayloadError("MirrorQueued.ticket_key must be non-empty (I3)")
        if isinstance(self.instrument, Combo):
            _require_vertical(self.instrument, "MirrorQueued.instrument")
        elif not isinstance(self.instrument, OptionContract):
            raise EventPayloadError(
                f"MirrorQueued.instrument must be an option contract or a vertical, got {self.instrument!r} (I6)"
            )
        if not isinstance(self.side, Side):
            raise EventPayloadError(f"MirrorQueued.side must be a Side, got {self.side!r}")
        object.__setattr__(
            self, "quantity", _require_whole(self.quantity, "MirrorQueued.quantity", positive=True)
        )
        if self.order_type not in MIRROR_ORDER_TYPES:
            raise EventPayloadError(f"MirrorQueued.order_type must be MARKET or LIMIT, got {self.order_type!r}")
        if self.tif not in MIRROR_TIFS:
            raise EventPayloadError(f"MirrorQueued.tif must be DAY or GTC, got {self.tif!r}")
        if self.order_type is OrderType.LIMIT:
            if self.limit_price is None:
                raise EventPayloadError("MirrorQueued: a LIMIT ticket needs a limit_price (I5)")
            object.__setattr__(self, "limit_price", _as_decimal(self.limit_price, "MirrorQueued.limit_price"))
            if self.limit_price <= 0:
                raise EventPayloadError("MirrorQueued.limit_price must be positive (I5)")
        elif self.limit_price is not None:
            raise EventPayloadError("MirrorQueued: a MARKET ticket cannot carry a limit_price")
        object.__setattr__(self, "allocations", tuple(self.allocations))
        if not self.allocations or not all(isinstance(a, MirrorAllocation) for a in self.allocations):
            raise EventPayloadError("MirrorQueued.allocations must be MirrorAllocations, at least one (§4.4)")
        ids = [a.strategy_order_id for a in self.allocations]
        if len(set(ids)) != len(ids):
            raise EventPayloadError("MirrorQueued allocates one strategy order twice (I3)")
        total = sum((a.quantity for a in self.allocations), Decimal("0"))
        if total != self.quantity:
            raise EventPayloadError(
                f"MirrorQueued allocations total {total} but the ticket is {self.quantity} (I11)"
            )
        _require_utc(self.at, "MirrorQueued.at")


@dataclass(frozen=True)
class MirrorRefused:
    """A strategy order refused at the venue only: the I11 record (the sim still has it)."""

    venue: str
    strategy_order_id: str
    strategy_account: str
    reason: str
    at: datetime

    def __post_init__(self) -> None:
        if not self.venue:
            raise EventPayloadError("MirrorRefused.venue must be non-empty")
        if not self.strategy_order_id:
            raise EventPayloadError("MirrorRefused.strategy_order_id must be non-empty")
        if not self.strategy_account:
            raise EventPayloadError("MirrorRefused.strategy_account must be non-empty")
        if not self.reason:
            raise EventPayloadError("MirrorRefused.reason must be non-empty (I11)")
        _require_utc(self.at, "MirrorRefused.at")


@dataclass(frozen=True)
class MirrorAck:
    """What the venue proved about one ticket: a send's outcome, or its Order Book row.

    ``venue_order_id`` is the venue's own Order ID (TOS Order Book), present only when a
    read-back proved it; it is what a cancel and the fill read-back match on after a
    restart. ``book_status`` is the Order Book row's state when one was read.
    """

    venue: str
    ticket_key: str
    status: str
    message: str
    at: datetime
    venue_order_id: str | None = None
    book_status: OrderState | None = None

    def __post_init__(self) -> None:
        if not self.venue:
            raise EventPayloadError("MirrorAck.venue must be non-empty")
        if not self.ticket_key:
            raise EventPayloadError("MirrorAck.ticket_key must be non-empty")
        if self.status not in MIRROR_ACK_STATUSES:
            raise EventPayloadError(f"MirrorAck.status must be one of {MIRROR_ACK_STATUSES}, got {self.status!r}")
        if not self.message:
            raise EventPayloadError("MirrorAck.message must be non-empty (I11)")
        if self.venue_order_id is not None and (
            not isinstance(self.venue_order_id, str) or not self.venue_order_id.isdigit()
        ):
            raise EventPayloadError(f"MirrorAck.venue_order_id must be an all-digit id, got {self.venue_order_id!r}")
        if self.book_status is not None and not isinstance(self.book_status, OrderState):
            raise EventPayloadError(f"MirrorAck.book_status must be an OrderState, got {self.book_status!r}")
        _require_utc(self.at, "MirrorAck.at")


@dataclass(frozen=True)
class MirrorFill:
    """The venue's cumulative fill of one ticket, read back by its proven Order ID.

    Cumulative, so re-recording the same total is a no-op and a lower one refuses (I3);
    the fold allocates the increment pro-rata to the ticket's strategy orders. For a
    vertical, ``filled`` counts spread units and ``avg_price`` is the net price per unit.
    """

    venue: str
    ticket_key: str
    venue_order_id: str
    filled: Decimal
    avg_price: Decimal
    at: datetime

    def __post_init__(self) -> None:
        if not self.venue:
            raise EventPayloadError("MirrorFill.venue must be non-empty")
        if not self.ticket_key:
            raise EventPayloadError("MirrorFill.ticket_key must be non-empty")
        if not isinstance(self.venue_order_id, str) or not self.venue_order_id.isdigit():
            raise EventPayloadError(f"MirrorFill.venue_order_id must be an all-digit id, got {self.venue_order_id!r}")
        object.__setattr__(self, "filled", _require_whole(self.filled, "MirrorFill.filled", positive=True))
        object.__setattr__(self, "avg_price", _as_decimal(self.avg_price, "MirrorFill.avg_price"))
        if self.avg_price <= 0:
            raise EventPayloadError(f"MirrorFill.avg_price must be positive, got {self.avg_price} (I5)")
        _require_utc(self.at, "MirrorFill.at")


MIRROR_PAYLOADS = (MirrorQueued, MirrorRefused, MirrorAck, MirrorFill)


# Each kind must carry exactly this payload type; anything else is refused (I5).
PAYLOAD_TYPES: dict[EventKind, type] = {
    EventKind.SIGNAL_SEEN: Signal,
    EventKind.RISK_VERDICT: RiskVerdict,
    EventKind.ORDERS_CREATED: OrdersCreated,
    EventKind.RISK_CONTROL: RiskControlChange,
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
    EventKind.ASSIGNMENT: OptionLifecycle,
    EventKind.EXERCISE: OptionLifecycle,
    EventKind.EXPIRY: OptionLifecycle,
    EventKind.CORPORATE_ACTION: CorporateAction,
    EventKind.CASH_FLOW: CashFlow,
    EventKind.MARK: Mark,
    EventKind.VENUE_RECONCILE: VenueReconcile,
    EventKind.EOD_RUN: EodRun,
    EventKind.MIRROR_QUEUED: MirrorQueued,
    EventKind.MIRROR_REFUSED: MirrorRefused,
    EventKind.MIRROR_ACK: MirrorAck,
    EventKind.MIRROR_FILL: MirrorFill,
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
        if isinstance(self.payload, MIRROR_PAYLOADS) and self.account != mirror_account(self.payload.venue):
            raise EventPayloadError(
                f"{self.kind.value} for venue '{self.payload.venue}' must be filed under "
                f"'{mirror_account(self.payload.venue)}', not '{self.account}' (I8)"
            )
        if isinstance(self.payload, OrdersCreated) and any(
            order.account_id != self.account for order in self.payload.orders
        ):
            raise EventPayloadError(
                f"{self.kind.value} contains orders for a different account (I8)"
            )

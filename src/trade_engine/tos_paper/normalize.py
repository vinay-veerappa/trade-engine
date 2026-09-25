"""Pure normalization of raw venue results into domain objects (§4.5 adapter pattern).

Network code lives in the host transport; this module only reads what it returned, with
no I/O, so every shape is pinned by golden vectors (tests/test_tos_normalize.py).

Rules: a send is never a fill. ``SENT`` and ``DRY_RUN`` are PENDING, anything the
module does not recognise is PENDING (unknown state => pending), and only a known
refusal is REJECTED. No place result is ACCEPTED: acceptance and fills are proven
only by the Order Book and Position read-backs. A cancel is ACCEPTED only when the
transport read the Order Book row back as CANCELED. A row this module cannot read raises
:class:`NormalizeError` — the caller treats the read as failed, never as empty (I5).

Raw row shapes (the host transport's contract):

- place result: ``{"status": "SENT" | "DRY_RUN" | "REFUSED" | "REJECTED" | ..., "reason": str}``,
  and on ``SENT`` optionally ``"order_id": "5403527317", "book_status": "WORKING"``
- cancel result: ``{"status": "CANCELED" | "UNKNOWN", "order_id": str, "note": str}``
- working order: ``{"symbol": <OCC>, "side": "BUY"|"SELL", "quantity": "2", "filled": "0",
  "order_type": "LMT"|"MKT", "limit_price": "1.25" | None, "status": "WORKING" | ...}``
- position: ``{"symbol": <OCC>, "quantity": "-1", "avg_price": "2.10"}``
- order fill (``OrderFillReader``): ``{"order_id": "5403527317", "filled": "1",
  "avg_price": "1.05" | None, "status": "FILLED" | "WORKING" | "EXPIRED" | ...}``
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation

from trade_engine.domain.instruments import OptionContract, Side
from trade_engine.domain.orders import OrderState, OrderType
from trade_engine.interfaces.broker import VenueAck, VenuePosition
from trade_engine.tos_paper.transport import TransportRefused, TransportReplay


class NormalizeError(ValueError):
    """A raw venue row the adapter cannot read without guessing."""


_REFUSED = frozenset({"REFUSED", "REJECTED", "INELIGIBLE", "MISMATCH"})


def normalize_place_result(raw: object, venue_order_id: str, at: datetime) -> VenueAck:
    """A transport result → a PENDING or REJECTED ack. Never ACCEPTED (I5)."""
    if not isinstance(raw, Mapping):
        return VenueAck(venue_order_id, "PENDING", at, f"unreadable transport result {raw!r}; awaiting reconcile")
    status = str(raw.get("status", "")).strip().upper()
    if status == "SENT":
        return VenueAck(venue_order_id, "PENDING", at, "sent; awaiting read-back")
    if status == "DRY_RUN":
        return VenueAck(venue_order_id, "PENDING", at, "dry run: nothing sent; awaiting reconcile")
    if status in _REFUSED:
        reason = raw.get("reason") or status
        return VenueAck(venue_order_id, "REJECTED", at, f"venue refused: {reason}")
    return VenueAck(venue_order_id, "PENDING", at, f"unknown transport status {status!r}; awaiting reconcile")


def normalize_place_exception(exc: BaseException, venue_order_id: str, at: datetime) -> VenueAck:
    """A transport exception → REJECTED if provably nothing was sent, else PENDING."""
    if isinstance(exc, TransportRefused):
        return VenueAck(venue_order_id, "REJECTED", at, f"transport refused before send: {exc}")
    if isinstance(exc, TransportReplay):
        return VenueAck(
            venue_order_id, "PENDING", at, f"idempotency replay: {exc}; confirming by reconcile (I3)"
        )
    return VenueAck(
        venue_order_id,
        "PENDING",
        at,
        f"transport error {type(exc).__name__}: {exc}; uncertain whether sent, awaiting reconcile",
    )


def placed_order_id(raw: object) -> str | None:
    """The venue Order ID a SENT result proves, or None; never a guess (I5).

    Only a SENT result whose new Order Book row was matched to this ticket (a
    ``book_status`` other than UNKNOWN) names the order, and only an all-digit id.
    """
    if not isinstance(raw, Mapping) or str(raw.get("status", "")).strip().upper() != "SENT":
        return None
    book = str(raw.get("book_status", "")).strip().upper()
    oid = raw.get("order_id")
    if not book or book == "UNKNOWN" or not isinstance(oid, str) or not oid.isdigit():
        return None
    return oid


def normalize_cancel_result(raw: object, venue_order_id: str, at: datetime) -> VenueAck:
    """A cancel result → ACCEPTED only for a CANCELED Order Book row, else PENDING."""
    if not isinstance(raw, Mapping):
        return VenueAck(venue_order_id, "PENDING", at, f"unreadable cancel result {raw!r}; awaiting reconcile")
    status = str(raw.get("status", "")).strip().upper()
    if status == "CANCELED":
        return VenueAck(venue_order_id, "ACCEPTED", at, f"cancelled: order {raw.get('order_id')} reads CANCELED")
    note = raw.get("note") or status or "no status"
    return VenueAck(venue_order_id, "PENDING", at, f"cancel not confirmed ({note}); awaiting reconcile")


def normalize_cancel_exception(exc: BaseException, venue_order_id: str, at: datetime) -> VenueAck:
    """A cancel exception → REJECTED if provably nothing was clicked, else PENDING."""
    if isinstance(exc, TransportRefused):
        return VenueAck(venue_order_id, "REJECTED", at, f"transport refused the cancel: {exc}")
    return VenueAck(
        venue_order_id,
        "PENDING",
        at,
        f"cancel error {type(exc).__name__}: {exc}; uncertain whether cancelled, awaiting reconcile",
    )


@dataclass(frozen=True)
class WorkingOrder:
    """One Order Book row, read back from the venue."""

    instrument: OptionContract
    side: Side
    quantity: Decimal
    filled: Decimal
    order_type: OrderType
    limit_price: Decimal | None
    state: OrderState

    @property
    def remaining(self) -> Decimal:
        return self.quantity - self.filled

    @property
    def live(self) -> bool:
        return self.state in _LIVE


_ROW_STATES = {
    "WORKING": OrderState.ACCEPTED,
    "OPEN": OrderState.ACCEPTED,
    "QUEUED": OrderState.SUBMITTED,
    "PARTIAL": OrderState.PARTIALLY_FILLED,
    "FILLED": OrderState.FILLED,
    "CANCELED": OrderState.CANCELLED,
    "CANCELLED": OrderState.CANCELLED,
    "REJECTED": OrderState.REJECTED,
    "EXPIRED": OrderState.EXPIRED,
}
_LIVE = frozenset({OrderState.SUBMITTED, OrderState.ACCEPTED, OrderState.PARTIALLY_FILLED})
_ROW_TYPES = {"MKT": OrderType.MARKET, "LMT": OrderType.LIMIT}


def _decimal(value: object, name: str) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float) or value is None:
        raise NormalizeError(f"{name} must be a decimal string, got {value!r}")
    try:
        result = Decimal(str(value).strip())
    except InvalidOperation as exc:
        raise NormalizeError(f"{name} is not a number: {value!r}") from exc
    if not result.is_finite():
        raise NormalizeError(f"{name} must be finite, got {value!r}")
    return result


def _contract(raw: Mapping[str, object]) -> OptionContract:
    symbol = raw.get("symbol")
    try:
        return OptionContract.from_occ(str(symbol))
    except ValueError as exc:
        raise NormalizeError(f"not a mirrored option symbol: {symbol!r}") from exc


def normalize_working_order(raw: Mapping[str, object]) -> WorkingOrder:
    """One Order Book row → WorkingOrder. An unknown status is PENDING_UNKNOWN."""
    side_text = str(raw.get("side", "")).strip().upper()
    if side_text not in ("BUY", "SELL"):
        raise NormalizeError(f"working order side {raw.get('side')!r}")
    type_text = str(raw.get("order_type", "")).strip().upper()
    if type_text not in _ROW_TYPES:
        raise NormalizeError(f"working order type {raw.get('order_type')!r}")
    quantity = _decimal(raw.get("quantity"), "quantity")
    filled = _decimal(raw.get("filled", "0"), "filled")
    if quantity <= 0 or filled < 0 or filled > quantity:
        raise NormalizeError(f"working order quantity {quantity} / filled {filled}")
    limit_raw = raw.get("limit_price")
    limit = None if limit_raw in (None, "") else _decimal(limit_raw, "limit_price")
    status = str(raw.get("status", "")).strip().upper()
    return WorkingOrder(
        instrument=_contract(raw),
        side=Side(side_text),
        quantity=quantity,
        filled=filled,
        order_type=_ROW_TYPES[type_text],
        limit_price=limit,
        state=_ROW_STATES.get(status, OrderState.PENDING_UNKNOWN),
    )


def book_state(raw_status: object) -> OrderState:
    """An Order Book status string → OrderState; anything unrecognised is PENDING_UNKNOWN."""
    return _ROW_STATES.get(str(raw_status or "").strip().upper(), OrderState.PENDING_UNKNOWN)


@dataclass(frozen=True)
class OrderFill:
    """One Order Book order's cumulative fill, read back by its venue Order ID."""

    order_id: str
    filled: Decimal
    avg_price: Decimal | None  # None only when nothing filled
    state: OrderState


def normalize_order_fill(raw: Mapping[str, object]) -> OrderFill:
    """One ``read_order_fills`` row → OrderFill. Anything ambiguous raises (I5).

    ``filled`` is a non-negative whole number; a positive fill needs a positive average
    price; a FILLED row with nothing filled is a contradiction.
    """
    oid = raw.get("order_id")
    if not isinstance(oid, str) or not oid.isdigit():
        raise NormalizeError(f"order fill row names no all-digit order_id: {oid!r}")
    filled = _decimal(raw.get("filled"), "filled")
    if filled < 0 or filled != filled.to_integral_value():
        raise NormalizeError(f"order {oid} filled {filled}: not a whole non-negative quantity")
    price_raw = raw.get("avg_price")
    price = None if price_raw in (None, "") else _decimal(price_raw, "avg_price")
    if filled > 0 and (price is None or price <= 0):
        raise NormalizeError(f"order {oid} filled {filled} with no positive average price {price_raw!r}")
    state = book_state(raw.get("status"))
    if state is OrderState.FILLED and filled == 0:
        raise NormalizeError(f"order {oid} reads FILLED with nothing filled")
    return OrderFill(order_id=oid, filled=filled, avg_price=price if filled > 0 else None, state=state)


def normalize_position(raw: Mapping[str, object], as_of: datetime) -> VenuePosition:
    """One Position row → VenuePosition (signed quantity)."""
    return VenuePosition(
        instrument=_contract(raw),
        quantity=_decimal(raw.get("quantity"), "quantity"),
        avg_price=_decimal(raw.get("avg_price"), "avg_price"),
        as_of=as_of,
    )

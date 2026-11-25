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
from decimal import Decimal

from trade_engine.domain.instruments import OptionContract, Side
from trade_engine.domain.orders import OrderState, OrderType
from trade_engine.interfaces.broker import VenueAck, VenuePosition
from trade_engine.tos_paper import _rs
from trade_engine.tos_paper.transport import TransportRefused, TransportReplay

_LIVE = frozenset({OrderState.SUBMITTED, OrderState.ACCEPTED, OrderState.PARTIALLY_FILLED})


class NormalizeError(ValueError):
    """A raw venue row the adapter cannot read without guessing."""


def _exc(exc: BaseException) -> tuple[str, str, str]:
    kind = "refused" if isinstance(exc, TransportRefused) else "replay" if isinstance(exc, TransportReplay) else "other"
    return kind, type(exc).__name__, str(exc)


def normalize_place_result(raw: object, venue_order_id: str, at: datetime) -> VenueAck:
    """A transport result → a PENDING or REJECTED ack. Never ACCEPTED (I5)."""
    return _rs.call(_rs.rs.tos_normalize_place_result, raw, venue_order_id, at)


def normalize_place_exception(exc: BaseException, venue_order_id: str, at: datetime) -> VenueAck:
    """A transport exception → REJECTED if provably nothing was sent, else PENDING."""
    kind, name, text = _exc(exc)
    return _rs.call(_rs.rs.tos_normalize_place_exception, kind, name, text, venue_order_id, at)


def placed_order_id(raw: object) -> str | None:
    """The venue Order ID a SENT result proves, or None; never a guess (I5).

    Only a SENT result whose new Order Book row was matched to this ticket (a
    ``book_status`` other than UNKNOWN) names the order, and only an all-digit id.
    """
    return _rs.call(_rs.rs.tos_normalize_placed_order_id, raw)


def normalize_cancel_result(raw: object, venue_order_id: str, at: datetime) -> VenueAck:
    """A cancel result → ACCEPTED only for a CANCELED Order Book row, else PENDING."""
    return _rs.call(_rs.rs.tos_normalize_cancel_result, raw, venue_order_id, at)


def normalize_cancel_exception(exc: BaseException, venue_order_id: str, at: datetime) -> VenueAck:
    """A cancel exception → REJECTED if provably nothing was clicked, else PENDING."""
    kind, name, text = _exc(exc)
    return _rs.call(_rs.rs.tos_normalize_cancel_exception, kind, name, text, venue_order_id, at)


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


def normalize_working_order(raw: Mapping[str, object]) -> WorkingOrder:
    """One Order Book row → WorkingOrder. An unknown status is PENDING_UNKNOWN."""
    return _rs.call(_rs.rs.tos_normalize_working_order, raw)


def book_state(raw_status: object) -> OrderState:
    """An Order Book status string → OrderState; anything unrecognised is PENDING_UNKNOWN."""
    return OrderState(_rs.call(_rs.rs.tos_normalize_book_state, raw_status))


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
    return _rs.call(_rs.rs.tos_normalize_order_fill, raw)


def normalize_position(raw: Mapping[str, object], as_of: datetime) -> VenuePosition:
    """One Position row → VenuePosition (signed quantity)."""
    return _rs.call(_rs.rs.tos_normalize_position, raw, as_of)

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


def _exc(exc: BaseException) -> dict:
    kind = "refused" if isinstance(exc, TransportRefused) else "replay" if isinstance(exc, TransportReplay) else "other"
    return {"exc": {"class": kind, "type": type(exc).__name__, "text": str(exc)}}


def _ack(op: str, doc: dict, venue_order_id: str, at: datetime) -> VenueAck:
    out = _rs.decide(op, doc)
    return VenueAck(venue_order_id, out["status"], at, out["message"])


def normalize_place_result(raw: object, venue_order_id: str, at: datetime) -> VenueAck:
    """A transport result → a PENDING or REJECTED ack. Never ACCEPTED (I5)."""
    return _ack("place_result", {"raw": raw}, venue_order_id, at)


def normalize_place_exception(exc: BaseException, venue_order_id: str, at: datetime) -> VenueAck:
    """A transport exception → REJECTED if provably nothing was sent, else PENDING."""
    return _ack("place_exception", _exc(exc), venue_order_id, at)


def placed_order_id(raw: object) -> str | None:
    """The venue Order ID a SENT result proves, or None; never a guess (I5).

    Only a SENT result whose new Order Book row was matched to this ticket (a
    ``book_status`` other than UNKNOWN) names the order, and only an all-digit id.
    """
    return _rs.decide("placed_order_id", {"raw": raw})["order_id"]


def normalize_cancel_result(raw: object, venue_order_id: str, at: datetime) -> VenueAck:
    """A cancel result → ACCEPTED only for a CANCELED Order Book row, else PENDING."""
    return _ack("cancel_result", {"raw": raw}, venue_order_id, at)


def normalize_cancel_exception(exc: BaseException, venue_order_id: str, at: datetime) -> VenueAck:
    """A cancel exception → REJECTED if provably nothing was clicked, else PENDING."""
    return _ack("cancel_exception", _exc(exc), venue_order_id, at)


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


def working_order_of(row: Mapping) -> WorkingOrder:
    """The ``te_core`` row (``working_order``) as the carrier."""
    return WorkingOrder(_rs.unwire(row["instrument"]), Side(row["side"]), Decimal(row["quantity"]),
                        Decimal(row["filled"]), OrderType(row["order_type"]), _rs.dec(row["limit_price"]),
                        OrderState(row["state"]))


def working_order_doc(row: WorkingOrder) -> dict:
    return {"instrument": _rs.wire(row.instrument), "side": row.side.value, "quantity": str(row.quantity),
            "filled": str(row.filled), "order_type": row.order_type.value, "limit_price": _rs.text(row.limit_price),
            "state": row.state.value}


def normalize_working_order(raw: Mapping[str, object]) -> WorkingOrder:
    """One Order Book row → WorkingOrder. An unknown status is PENDING_UNKNOWN."""
    return working_order_of(_rs.decide("working_order", {"raw": raw}))


def book_state(raw_status: object) -> OrderState:
    """An Order Book status string → OrderState; anything unrecognised is PENDING_UNKNOWN."""
    return OrderState(_rs.decide("book_state", {"status": raw_status})["state"])


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
    out = _rs.decide("order_fill", {"raw": raw})
    return OrderFill(order_id=out["order_id"], filled=Decimal(out["filled"]), avg_price=_rs.dec(out["avg_price"]),
                     state=OrderState(out["state"]))


def normalize_position(raw: Mapping[str, object], as_of: datetime) -> VenuePosition:
    """One Position row → VenuePosition (signed quantity)."""
    out = _rs.decide("position", {"raw": raw})
    return VenuePosition(instrument=_rs.unwire(out["instrument"]), quantity=Decimal(out["quantity"]),
                         avg_price=Decimal(out["avg_price"]), as_of=as_of)

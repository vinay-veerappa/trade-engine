"""The mirror's transport boundary: what the host must supply (architecture §4.5, I13).

The engine never imports tos-ui-mcp. The host (the plugins repo) adapts its concrete
order-entry driver to :class:`TosOrderTransport` and, when it can read one, a balance
to :class:`BalanceReader`. Everything here is a shape, not a behaviour: the adapter
turns raw transport results into domain objects in ``normalize`` (pure, golden-vector
tested), and treats anything it cannot prove as pending (§4.5, I5).

The transport is paperMoney-only by contract: its ``connect`` proves the window is
Paper@thinkorswim, and nothing in this package calls any Schwab order endpoint.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Literal, Protocol, runtime_checkable

from trade_engine.domain.instruments import OptionContract
from trade_engine.domain.orders import OrderType, TimeInForce
from trade_engine.interfaces.broker import UnsupportedCapability, VenueOrder


class TransportRefused(Exception):
    """Raised by the host transport when it refused **before** sending anything.

    A wrong window, a ticket echo that did not match, an ineligible account: nothing
    reached the venue, so the ticket is REJECTED with this reason.
    """


class TransportReplay(Exception):
    """Raised by the host transport when an idempotency key was already used.

    The original ticket may or may not have been sent; the adapter treats the ticket as
    PENDING and lets the reconcile decide (I3).
    """


@dataclass(frozen=True)
class MirrorTicket:
    """One single-leg option ticket, as the venue driver renders and echoes it."""

    symbol: str                        # 21-char OCC, e.g. 'AAPL  261016P00200000'
    side: Literal["BUY", "SELL"]
    quantity: int
    order_type: Literal["MKT", "LMT"]
    limit_price: Decimal | None
    tif: Literal["DAY", "GTC"]
    underlying: str
    expiry: date
    strike: Decimal
    right: Literal["C", "P"]

    def __post_init__(self) -> None:
        if self.side not in ("BUY", "SELL"):
            raise ValueError(f"ticket side must be BUY or SELL, got {self.side!r}")
        if not isinstance(self.quantity, int) or isinstance(self.quantity, bool) or self.quantity <= 0:
            raise ValueError(f"ticket quantity must be a positive int, got {self.quantity!r}")
        if self.order_type not in ("MKT", "LMT"):
            raise ValueError(f"ticket order_type must be MKT or LMT, got {self.order_type!r}")
        if self.order_type == "LMT" and (self.limit_price is None or self.limit_price <= 0):
            raise ValueError("an LMT ticket needs a positive limit_price (I5)")
        if self.order_type == "MKT" and self.limit_price is not None:
            raise ValueError("a MKT ticket cannot carry a limit_price")
        if self.tif not in ("DAY", "GTC"):
            raise ValueError(f"ticket tif must be DAY or GTC, got {self.tif!r}")


_TICKET_TYPES = {OrderType.MARKET: "MKT", OrderType.LIMIT: "LMT"}
_TICKET_TIFS = {TimeInForce.DAY: "DAY", TimeInForce.GTC: "GTC"}


def ticket_for(order: VenueOrder) -> MirrorTicket:
    """The ticket for one venue order, or UnsupportedCapability — never an approximation."""
    if not isinstance(order.instrument, OptionContract):
        raise UnsupportedCapability(
            f"{order.instrument!r}: only single option contracts are mirrored (§4.7)"
        )
    if order.order_type not in _TICKET_TYPES:
        raise UnsupportedCapability(f"order type {order.order_type.value}: MARKET/LIMIT only")
    if order.tif not in _TICKET_TIFS:
        raise UnsupportedCapability(f"TIF {order.tif.value}: DAY/GTC only")
    if order.quantity != order.quantity.to_integral_value():
        raise UnsupportedCapability(
            f"quantity {order.quantity} is not a whole number of contracts (I5)"
        )
    contract = order.instrument
    return MirrorTicket(
        symbol=contract.to_occ(),
        side=order.side.value,
        quantity=int(order.quantity),
        order_type=_TICKET_TYPES[order.order_type],
        limit_price=order.limit_price if order.order_type is OrderType.LIMIT else None,
        tif=_TICKET_TIFS[order.tif],
        underlying=contract.underlying,
        expiry=contract.expiry,
        strike=contract.strike,
        right=contract.right.value,
    )


@runtime_checkable
class TosOrderTransport(Protocol):
    """The venue driver's surface, as the mirror uses it. The host adapts its driver.

    - ``connect`` proves paperMoney and returns the banner identity:
      ``{"number": "D-00000001", "type": "margin"}``.
    - ``place_order`` renders, verifies the echo, and sends one ticket; it returns a raw
      result mapping ``{"status": ..., "reason": ...}`` (see ``normalize``), raises
      :class:`TransportRefused` when nothing was sent, :class:`TransportReplay` on a
      used key. "Sent" is never a fill: confirmation comes only from the read-backs.
      A ``SENT`` result may carry the venue's own ``order_id`` with the ``book_status``
      of its Order Book row; that id is what :class:`OrderCanceller` cancels.
    - ``read_working_orders`` / ``read_positions`` return the raw Order Book and
      Position rows (see ``normalize`` for the row shapes).
    """

    def connect(self) -> Mapping[str, str]: ...
    def place_order(self, ticket: MirrorTicket, idempotency_key: str) -> Mapping[str, object]: ...
    def read_working_orders(self) -> Sequence[Mapping[str, object]]: ...
    def read_positions(self) -> Sequence[Mapping[str, object]]: ...
    def close(self) -> None: ...


@runtime_checkable
class BalanceReader(Protocol):
    """The venue account's net liquidation value, read by the host (§4.7 funding)."""

    def net_liquidation(self) -> Decimal | int | str: ...


@runtime_checkable
class OrderCanceller(Protocol):
    """Optional transport capability: cancel one resting order by the venue's Order ID.

    Separate from :class:`TosOrderTransport` so a transport without it stays a
    transport; the adapter refuses a cancel when it is absent. ``cancel_order`` returns
    ``{"status": "CANCELED" | "UNKNOWN", ...}`` (``CANCELED`` only when the Order Book
    row reads CANCELED, also for an order that already was), and raises
    :class:`TransportRefused` when it clicked nothing (not WORKING, row not found,
    menu mismatch).
    """

    def cancel_order(self, order_id: str) -> Mapping[str, object]: ...

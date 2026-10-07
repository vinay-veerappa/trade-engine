"""The mirror's transport boundary: what the host must supply (architecture §4.5, I13).

The engine never imports tos-ui-mcp. The host (the plugins repo) adapts its concrete
order-entry driver to :class:`TosOrderTransport` and, when it can read one, a balance
to :class:`BalanceReader`; the optional capabilities are :class:`OrderCanceller` (cancel
by Order ID) and :class:`OrderFillReader` (cumulative fills per Order ID, which the
mirror ledger books from). Everything here is a shape, not a behaviour: the adapter
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

from trade_engine.interfaces.broker import VenueOrder
from trade_engine.tos_paper import _rs


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


class TransportUnavailable(Exception):
    """Raised by a host transport **read** (or connect) that could not ask the venue at all.

    Nothing was sent and nothing was learned: not a refusal (no ticket was turned down), not a
    drift (no venue row contradicted the mirror), not a halt (no state is unproven that was not
    already). The mirror defers: it records nothing, sends nothing, and the next run asks
    again. Only the *absence* of an answer is this; a row that came back and cannot be read is
    an ordinary failure and halts the venue (I5).
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
        _rs.decide("ticket_validate", {
            "kind": "option", "side": self.side, "quantity": self.quantity, "order_type": self.order_type,
            "tif": self.tif, "limit_price": _rs.text(self.limit_price),
        })


@dataclass(frozen=True)
class MirrorStockTicket:
    """One order for shares of stock (no OCC symbol), as the venue driver renders and echoes it.

    Whole shares, MKT or LMT, DAY or GTC: the same limits as :class:`MirrorTicket`. S1a: the fields
    only, validation arrives with the acceptance tests in tests/test_tos_stock.py.
    """

    symbol: str                        # an equity symbol, e.g. 'AAPL' (never an OCC string)
    side: Literal["BUY", "SELL"]
    quantity: int                      # shares
    order_type: Literal["MKT", "LMT"]
    limit_price: Decimal | None
    tif: Literal["DAY", "GTC"]

    def __post_init__(self) -> None:
        _rs.decide("ticket_validate", {
            "kind": "stock", "symbol": self.symbol, "side": self.side, "quantity": self.quantity,
            "order_type": self.order_type, "tif": self.tif, "limit_price": _rs.text(self.limit_price),
        })


@dataclass(frozen=True)
class MirrorComboLeg:
    """One leg of a vertical ticket, traded as written (its own side)."""

    symbol: str                        # 21-char OCC
    side: Literal["BUY", "SELL"]
    ratio: int
    expiry: date
    strike: Decimal
    right: Literal["C", "P"]

    def __post_init__(self) -> None:
        _rs.decide("ticket_validate", {"kind": "leg", "side": self.side, "ratio": self.ratio})


@dataclass(frozen=True)
class MirrorComboTicket:
    """One 2-leg vertical ticket: both legs, one net LIMIT price, the price effect explicit.

    ``price_effect`` CREDIT collects ``limit_price`` per unit (the strategy order SELLs the
    spread), DEBIT pays it (BUYs). ``quantity`` counts spread units; a leg trades
    ``quantity * ratio`` contracts. The host renders it as one TOS vertical order.
    """

    underlying: str
    legs: tuple[MirrorComboLeg, ...]
    quantity: int
    order_type: Literal["LMT"]
    limit_price: Decimal
    price_effect: Literal["CREDIT", "DEBIT"]
    tif: Literal["DAY", "GTC"]

    def __post_init__(self) -> None:
        object.__setattr__(self, "legs", tuple(self.legs))
        _rs.decide("ticket_validate", {
            "kind": "combo", "legs": [{"side": leg.side, "ratio": leg.ratio} for leg in self.legs],
            "quantity": self.quantity, "order_type": self.order_type, "limit_price": _rs.text(self.limit_price),
            "price_effect": self.price_effect, "tif": self.tif,
        })


def ticket_for(order: VenueOrder) -> MirrorTicket | MirrorComboTicket | MirrorStockTicket:
    """The ticket for one venue order, or UnsupportedCapability — never an approximation."""
    return ticket_of(_rs.decide("ticket_for", {
        "instrument": _rs.wire(order.instrument), "order_type": order.order_type.value, "tif": order.tif.value,
        "quantity": str(order.quantity), "side": order.side.value, "limit_price": _rs.text(order.limit_price),
    }))


def ticket_of(d: Mapping) -> MirrorTicket | MirrorComboTicket | MirrorStockTicket:
    """The ticket ``te_core`` decided (``ticket_for``), as the venue driver's own carrier."""
    limit = _rs.dec(d["limit_price"])
    if d["kind"] == "stock":
        return MirrorStockTicket(symbol=d["symbol"], side=d["side"], quantity=d["quantity"],
                                 order_type=d["order_type"], limit_price=limit, tif=d["tif"])
    if d["kind"] == "option":
        return MirrorTicket(symbol=d["symbol"], side=d["side"], quantity=d["quantity"], order_type=d["order_type"],
                            limit_price=limit, tif=d["tif"], underlying=d["underlying"],
                            expiry=date.fromisoformat(d["expiry"]), strike=Decimal(d["strike"]), right=d["right"])
    legs = tuple(MirrorComboLeg(symbol=leg["symbol"], side=leg["side"], ratio=leg["ratio"],
                                expiry=date.fromisoformat(leg["expiry"]), strike=Decimal(leg["strike"]),
                                right=leg["right"]) for leg in d["legs"])
    return MirrorComboTicket(underlying=d["underlying"], legs=legs, quantity=d["quantity"],
                             order_type=d["order_type"], limit_price=limit, price_effect=d["price_effect"],
                             tif=d["tif"])


@runtime_checkable
class TosOrderTransport(Protocol):
    """The venue driver's surface, as the mirror uses it. The host adapts its driver.

    - ``connect`` proves paperMoney and returns the banner identity:
      ``{"number": "D-00000001", "type": "margin"}``.
    - ``place_order`` renders, verifies the echo, and sends one ticket — a
      :class:`MirrorTicket`, or a :class:`MirrorComboTicket` for a vertical; it returns a raw
      result mapping ``{"status": ..., "reason": ...}`` (see ``normalize``), raises
      :class:`TransportRefused` when nothing was sent, :class:`TransportReplay` on a
      used key. "Sent" is never a fill: confirmation comes only from the read-backs.
      A ``SENT`` result may carry the venue's own ``order_id`` with the ``book_status``
      of its Order Book row; that id is what :class:`OrderCanceller` cancels.
    - ``read_working_orders`` / ``read_positions`` return the raw Order Book and
      Position rows (see ``normalize`` for the row shapes). A vertical's Order Book
      order is returned as one working row **per leg** (the leg's symbol, side and
      contracts, the order's net ``limit_price`` on each), so the reconcile runs per leg.
    """

    def connect(self) -> Mapping[str, str]: ...
    def place_order(
        self, ticket: MirrorTicket | MirrorComboTicket, idempotency_key: str
    ) -> Mapping[str, object]: ...
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


@runtime_checkable
class OrderFillReader(Protocol):
    """Optional transport capability: the fill state of every Order Book row, by Order ID.

    ``read_order_fills`` returns one row per venue Order ID (today's Order Book, working
    and filled/cancelled/expired alike):
    ``{"order_id": "5403527317", "filled": "1", "avg_price": "1.05", "status": "FILLED"}``.

    - ``filled`` is the order's cumulative filled quantity — the host sums the Order
      Book's fill rows per Order ID; for a vertical it counts spread units.
    - ``avg_price`` is the quantity-weighted average fill price (a vertical: the net
      price per unit, positive, credit or debit as the order was); empty or absent when
      nothing filled.
    - ``status`` is the row's state as the Order Book shows it (WORKING, FILLED,
      CANCELED, EXPIRED, REJECTED, …).

    Without it the mirror cannot prove a venue fill and refuses to book one (I5).
    """

    def read_order_fills(self) -> Sequence[Mapping[str, object]]: ...

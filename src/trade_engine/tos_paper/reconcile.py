"""Venue reconcile and read-back confirmation, pure (architecture §4.5, P4 gate).

- :func:`reconcile` compares what the venue shows (positions plus the unfilled remainder
  of live working orders) with what the mirror book expects, per contract, and returns
  the ``VenueReconcile`` event. Any drift names the contracts and halts new orders on
  that venue (the ledger fold latches it, sticky across replay).
- :func:`confirm_ticket` decides what one sent ticket's read-back proves. Only a
  matching Order Book row or a position that moved by exactly the ticket proves the
  ticket reached the venue; otherwise it stays PENDING (I5).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import Decimal

from trade_engine.domain.instruments import Instrument, Side
from trade_engine.domain.orders import OrderState, OrderType
from trade_engine.interfaces.broker import VenueOrder, VenuePosition
from trade_engine.ledger.events import VenueReconcile
from trade_engine.tos_paper.normalize import WorkingOrder

ZERO = Decimal("0")
UNREADABLE = "<venue unreadable>"


def _signed(side: Side, quantity: Decimal) -> Decimal:
    return quantity if side is Side.BUY else -quantity


def position_book(positions: Sequence[VenuePosition]) -> dict[Instrument, Decimal]:
    book: dict[Instrument, Decimal] = {}
    for position in positions:
        book[position.instrument] = book.get(position.instrument, ZERO) + position.quantity
    return book


def reconcile(
    venue: str,
    as_of: datetime,
    expected: Mapping[Instrument, Decimal],
    positions: Sequence[VenuePosition],
    working: Sequence[WorkingOrder],
) -> VenueReconcile:
    """Per contract: venue position + live working remainder must equal expected."""
    held = position_book(positions)
    resting: dict[Instrument, Decimal] = {}
    unknown: set[Instrument] = set()
    for row in working:
        if row.state is OrderState.PENDING_UNKNOWN:
            unknown.add(row.instrument)
        elif row.live:
            resting[row.instrument] = resting.get(row.instrument, ZERO) + _signed(row.side, row.remaining)
    drift: list[str] = []
    for contract in set(expected) | set(held) | set(resting) | unknown:
        want = expected.get(contract, ZERO)
        have = held.get(contract, ZERO) + resting.get(contract, ZERO)
        if contract in unknown or have != want:
            drift.append(contract.symbol)
    if drift:
        return VenueReconcile(
            venue=venue,
            as_of=as_of,
            reconciled=False,
            drift=tuple(sorted(drift)),
            note="venue positions + working orders disagree with the mirror book; venue halted",
        )
    return VenueReconcile(venue=venue, as_of=as_of, reconciled=True)


def unreadable(venue: str, as_of: datetime, contracts: Sequence[Instrument], why: str) -> VenueReconcile:
    """A reconcile that could not read the venue: drift on every contract in play."""
    names = tuple(sorted({c.symbol for c in contracts})) or (UNREADABLE,)
    return VenueReconcile(
        venue=venue,
        as_of=as_of,
        reconciled=False,
        drift=names,
        note=f"cannot read the venue ({why}); refusing to assume it matches (I5)",
    )


def confirm_ticket(
    ticket: VenueOrder,
    before: Mapping[Instrument, Decimal],
    positions: Sequence[VenuePosition],
    working: Sequence[WorkingOrder],
    claimed: set[int],
) -> tuple[str, str]:
    """(status, reason) the read-back proves for one sent ticket.

    ``claimed`` holds indexes of ``working`` rows already matched to earlier tickets of
    this batch, so two identical tickets cannot both claim one row.
    """
    limit = ticket.limit_price if ticket.order_type is OrderType.LIMIT else None
    for index, row in enumerate(working):
        if index in claimed:
            continue
        if (
            row.instrument == ticket.instrument
            and row.side is ticket.side
            and row.quantity == ticket.quantity
            and row.limit_price == limit
            and row.order_type is ticket.order_type
        ):
            claimed.add(index)
            if row.live:
                return "ACCEPTED", f"on the order book ({row.state.value}, filled {row.filled})"
            if row.state is OrderState.FILLED:
                return "ACCEPTED", "filled (order book)"
            if row.state is OrderState.PENDING_UNKNOWN:
                return "PENDING", "order book row in an unknown state"
            return "REJECTED", f"venue order book shows {row.state.value}"
    moved = position_book(positions).get(ticket.instrument, ZERO) - before.get(ticket.instrument, ZERO)
    if moved == _signed(ticket.side, ticket.quantity):
        return "ACCEPTED", f"filled (position moved {moved})"
    return "PENDING", "not visible on the order book or in positions yet"

"""Venue reconcile and read-back confirmation, pure (architecture §4.5, P4 gate).

- :func:`reconcile` compares what the venue shows (positions plus the unfilled remainder
  of live working orders) with what the mirror book expects, per contract, and returns
  the ``VenueReconcile`` event. Any drift names the contracts and halts new orders on
  that venue (the ledger fold latches it, sticky across replay).
- :func:`confirm_ticket` decides what one sent ticket's read-back proves. Only a
  matching Order Book row or a position that moved by exactly the ticket proves the
  ticket reached the venue; otherwise it stays PENDING (I5). A vertical is proven leg by
  leg (one Order Book row per leg, or every leg's position moved).

What the mirror *expects* comes from the ledger fold (``ledger.mirror``): the mirror book
of proven venue fills plus the live remainder of every open ticket, so a DAY ticket that
expired unfilled, or one whose fill is recorded, never shows as drift.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import Decimal

from trade_engine.domain.instruments import Instrument
from trade_engine.interfaces.broker import VenueOrder, VenuePosition
from trade_engine.ledger.events import VenueReconcile
from trade_engine.tos_paper import _rs
from trade_engine.tos_paper.normalize import WorkingOrder

UNREADABLE = "<venue unreadable>"


def position_book(positions: Sequence[VenuePosition]) -> dict[Instrument, Decimal]:
    return _rs.call(_rs.rs.tos_reconcile_position_book, positions)


def reconcile(
    venue: str,
    as_of: datetime,
    expected: Mapping[Instrument, Decimal],
    positions: Sequence[VenuePosition],
    working: Sequence[WorkingOrder],
) -> VenueReconcile:
    """Per contract: venue position + live working remainder must equal expected."""
    return _rs.call(_rs.rs.tos_reconcile_reconcile, venue, as_of, expected, positions, working)


def unreadable(venue: str, as_of: datetime, contracts: Sequence[Instrument], why: str) -> VenueReconcile:
    """A reconcile that could not read the venue: drift on every contract in play."""
    return _rs.call(_rs.rs.tos_reconcile_unreadable, venue, as_of, contracts, why)


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
    status, reason, indexes = _rs.call(_rs.rs.tos_reconcile_confirm_ticket, ticket, before, positions,
                                       working, sorted(claimed))
    claimed.update(indexes)
    return status, reason


def ticket_contracts(ticket: VenueOrder, units: Decimal | None = None) -> dict[Instrument, Decimal]:
    """Signed contracts ``units`` of a ticket (default: all of it) put on the venue, per contract."""
    return _rs.call(_rs.rs.tos_reconcile_ticket_contracts, ticket, units)

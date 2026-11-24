"""Sim-vs-venue fills: allocation down to strategy orders, and the slippage report (§4.7).

The ledger records both fills for the same strategy order — the sim fill (the book of
record) and the venue fill read back from paperMoney. :func:`allocate_venue_fill` turns
one netted-ticket ``VenueFill`` into per-strategy-order fills (pro-rata over the ticket's
allocations), so the ledger can record the venue side. :func:`slippage_report` pairs the
two per strategy order and reports the adverse price difference, in points and in bps
of the sim price.

Nothing is dropped or guessed (I5/I11): an order whose fills cannot be paired honestly
(side or instrument mismatch, quantity drift, duplicate fill ids) is listed with its
reason and the rest of the report still stands.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from trade_engine.domain.instruments import Side
from trade_engine.domain.portfolio import Fill
from trade_engine.interfaces.broker import VenueFill, VenueOrder
from trade_engine.tos_paper import _rs


class SlippageError(RuntimeError):
    """A fill set the report or allocator cannot handle honestly."""


# -- allocation --------------------------------------------------------------


def allocate_venue_fill(
    fill: VenueFill,
    ticket: VenueOrder,
    *,
    already_filled: Mapping[str, Decimal] | None = None,
) -> tuple[Fill, ...]:
    """One venue fill of a netted ticket → one ``Fill`` per strategy order (§4.4).

    The ticket's allocations are all on the fill's side (netting is same-side only), so
    each order gets its pro-rata share. ``already_filled`` carries what earlier partial
    fills of this ticket gave each strategy order; the fill is split pro-rata over what
    each order still lacks — the mirror fold's rule (``ledger.mirror.on_fill``), so the
    venue fills allocated here match the mirror book exactly. Fees split pro-rata to the
    cent, remainder to the first-in order.
    """
    return tuple(_rs.call(_rs.rs.tos_slippage_allocate_venue_fill, fill, ticket, already_filled))


# -- the report --------------------------------------------------------------


@dataclass(frozen=True)
class SlippagePair:
    """One strategy order's sim fill and the venue fill that mirrors it."""

    order_id: str
    instrument: str
    side: Side
    quantity: Decimal
    sim_price: Decimal
    venue_price: Decimal
    slippage_points: Decimal     # adverse-positive: positive = the venue cost more
    slippage_bps: Decimal | None  # None when the sim price cannot scale it

    def __post_init__(self) -> None:
        if self.quantity <= 0:
            raise SlippageError("pair quantity must be strictly positive")


@dataclass(frozen=True)
class SlippageReport:
    """One venue's sim-vs-venue report over a session or batch."""

    venue: str
    as_of: datetime
    pairs: tuple[SlippagePair, ...]
    unmatched_sim: tuple[str, ...]      # order ids filled in sim with no venue fill
    unmatched_venue: tuple[str, ...]    # venue fills naming an order the sim did not fill
    refused: tuple[tuple[str, str], ...]  # (order_id, reason) that could not be paired
    mean_slippage_bps: Decimal | None   # quantity-weighted; None with no priced pairs

    def __post_init__(self) -> None:
        if not self.venue:
            raise SlippageError("venue must be non-empty")


def slippage_report(
    venue: str,
    as_of: datetime,
    sim_fills: Sequence[Fill],
    venue_fills: Sequence[Fill],
) -> SlippageReport:
    """Pair sim and venue fills per strategy order; refuse per order, never per report."""
    return _rs.call(_rs.rs.tos_slippage_report, venue, as_of, sim_fills, venue_fills)

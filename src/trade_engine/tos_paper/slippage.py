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
    out = _rs.decide("allocate_venue_fill", {
        "fill": {"venue_fill_id": fill.venue_fill_id, "venue_order_id": fill.venue_order_id,
                 "instrument": _rs.wire(fill.instrument), "side": fill.side.value, "quantity": str(fill.quantity),
                 "price": str(fill.price), "filled_at": fill.filled_at.isoformat(), "fee": str(fill.fee)},
        "ticket": {"venue_order_id": ticket.venue_order_id, "instrument": _rs.wire(ticket.instrument),
                   "side": ticket.side.value, "quantity": str(ticket.quantity),
                   "allocations": [{"strategy_order_id": a.strategy_order_id, "account_id": a.account_id,
                                    "quantity": str(a.quantity)} for a in ticket.allocations]},
        "already_filled": None if already_filled is None else {k: str(v) for k, v in already_filled.items()},
    })
    return tuple(
        Fill(
            fill_id=f["fill_id"], order_id=f["order_id"], account_id=f["account_id"], instrument=fill.instrument,
            quantity=Decimal(f["quantity"]), price=Decimal(f["price"]), venue_env=f["venue_env"],
            filled_at=fill.filled_at, side=Side(f["side"]), fee=Decimal(f["fee"]),
            venue_order_id=f["venue_order_id"], venue_execution_id=f["venue_execution_id"],
        )
        for f in out
    )


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


def _fill_doc(f: Fill) -> dict:
    return {"fill_id": f.fill_id, "order_id": f.order_id, "instrument": _rs.wire(f.instrument),
            "side": f.side.value, "quantity": str(f.quantity), "price": str(f.price)}


def slippage_report(
    venue: str,
    as_of: datetime,
    sim_fills: Sequence[Fill],
    venue_fills: Sequence[Fill],
) -> SlippageReport:
    """Pair sim and venue fills per strategy order; refuse per order, never per report."""
    r = _rs.decide("slippage_report", {
        "venue": venue, "as_of": as_of.isoformat(),
        "sim_fills": [_fill_doc(f) for f in sim_fills], "venue_fills": [_fill_doc(f) for f in venue_fills],
    })
    pairs = tuple(
        SlippagePair(p["order_id"], p["instrument"], Side(p["side"]), Decimal(p["quantity"]),
                     Decimal(p["sim_price"]), Decimal(p["venue_price"]), Decimal(p["slippage_points"]),
                     _rs.dec(p["slippage_bps"]))
        for p in r["pairs"]
    )
    return SlippageReport(r["venue"], datetime.fromisoformat(r["as_of"]), pairs, tuple(r["unmatched_sim"]),
                          tuple(r["unmatched_venue"]), tuple(tuple(x) for x in r["refused"]),
                          _rs.dec(r["mean_slippage_bps"]))

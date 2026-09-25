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
from decimal import ROUND_FLOOR, Decimal

from trade_engine.domain.instruments import Side
from trade_engine.domain.portfolio import Fill
from trade_engine.interfaces.broker import VenueFill, VenueOrder
from trade_engine.ledger.mirror import pro_rata

BPS = Decimal("10000")
ZERO = Decimal("0")
CENT = Decimal("0.01")


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
    fills of this ticket gave each strategy order: shares are computed on the cumulative
    total so a sequence of partials allocates exactly as one fill would. Fees split
    pro-rata to the cent, remainder to the first-in order.
    """
    if fill.venue_order_id != ticket.venue_order_id:
        raise SlippageError(f"fill {fill.venue_fill_id} is for {fill.venue_order_id}, not {ticket.venue_order_id}")
    if fill.instrument != ticket.instrument:
        raise SlippageError(f"fill {fill.venue_fill_id} instrument does not match the ticket (I6)")
    if fill.side is not ticket.side:
        raise SlippageError(f"fill {fill.venue_fill_id} side {fill.side.value} != ticket side {ticket.side.value}")
    prior = dict(already_filled or {})
    done = sum(prior.values(), ZERO)
    total = done + fill.quantity
    if total > ticket.quantity:
        raise SlippageError(
            f"fill {fill.venue_fill_id} takes the ticket to {total} of {ticket.quantity}; overfill (I5)"
        )
    targets = pro_rata([a.quantity for a in ticket.allocations], ticket.quantity, total)
    pieces: list[tuple[int, Decimal]] = []
    for index, (allocation, target) in enumerate(zip(ticket.allocations, targets)):
        piece = target - prior.get(allocation.strategy_order_id, ZERO)
        if piece < 0:
            raise SlippageError(
                f"cumulative allocation for {allocation.strategy_order_id} is not monotone; "
                "allocate this ticket's partials by hand (I5)"
            )
        if piece > 0:
            pieces.append((index, piece))
    fees = [(fill.fee * piece / fill.quantity).quantize(CENT, rounding=ROUND_FLOOR) for _, piece in pieces]
    if fees:
        fees[0] += fill.fee - sum(fees, ZERO)
    fills: list[Fill] = []
    for (index, piece), fee in zip(pieces, fees):
        allocation = ticket.allocations[index]
        fills.append(
            Fill(
                fill_id=f"{fill.venue_fill_id}:{allocation.strategy_order_id}",
                order_id=allocation.strategy_order_id,
                account_id=allocation.account_id,
                instrument=fill.instrument,
                quantity=piece,
                price=fill.price,
                venue_env="paper",
                filled_at=fill.filled_at,
                side=fill.side,
                fee=fee,
                venue_order_id=fill.venue_order_id,
                venue_execution_id=fill.venue_fill_id,
            )
        )
    return tuple(fills)


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


def _signed_points(side: Side, venue: Decimal, sim: Decimal) -> Decimal:
    """Adverse-only convention: positive always costs money."""
    return (venue - sim) if side is Side.BUY else (sim - venue)


def _group(fills: Sequence[Fill]) -> tuple[dict[str, list[Fill]], dict[str, str]]:
    by_order: dict[str, list[Fill]] = {}
    bad: dict[str, str] = {}
    seen: set[str] = set()
    for fill in fills:
        if fill.fill_id in seen:
            bad[fill.order_id] = f"duplicate fill id {fill.fill_id} (I3)"
        seen.add(fill.fill_id)
        by_order.setdefault(fill.order_id, []).append(fill)
    return by_order, bad


def _vwap(fills: Sequence[Fill]) -> tuple[Decimal, Decimal]:
    quantity = sum((f.quantity for f in fills), ZERO)
    return quantity, sum((f.price * f.quantity for f in fills), ZERO) / quantity


def slippage_report(
    venue: str,
    as_of: datetime,
    sim_fills: Sequence[Fill],
    venue_fills: Sequence[Fill],
) -> SlippageReport:
    """Pair sim and venue fills per strategy order; refuse per order, never per report."""
    sim_by, sim_bad = _group(sim_fills)
    venue_by, venue_bad = _group(venue_fills)
    pairs: list[SlippagePair] = []
    unmatched_sim: list[str] = []
    refused: list[tuple[str, str]] = []
    for order_id, sims in sim_by.items():
        if order_id in sim_bad:
            refused.append((order_id, f"sim: {sim_bad[order_id]}"))
            continue
        venues = venue_by.get(order_id)
        if not venues:
            unmatched_sim.append(order_id)
            continue
        if order_id in venue_bad:
            refused.append((order_id, f"venue: {venue_bad[order_id]}"))
            continue
        sides = {f.side for f in sims} | {f.side for f in venues}
        instruments = {f.instrument for f in sims} | {f.instrument for f in venues}
        if len(sides) > 1:
            refused.append((order_id, f"side mismatch {sorted(s.value for s in sides)}; not paired"))
            continue
        if len(instruments) > 1:
            refused.append((order_id, "instrument mismatch between sim and venue fills; not paired (I6)"))
            continue
        sim_qty, sim_price = _vwap(sims)
        venue_qty, venue_price = _vwap(venues)
        if venue_qty != sim_qty:
            refused.append(
                (
                    order_id,
                    f"sim filled {sim_qty} but the venue filled {venue_qty}; mirror drifted (I5)",
                )
            )
            continue
        side = sims[0].side
        points = _signed_points(side, venue_price, sim_price)
        bps = (points / sim_price * BPS).quantize(Decimal("0.0001")) if sim_price > 0 else None
        pairs.append(
            SlippagePair(
                order_id=order_id,
                instrument=sims[0].instrument.symbol,
                side=side,
                quantity=sim_qty,
                sim_price=sim_price,
                venue_price=venue_price,
                slippage_points=points,
                slippage_bps=bps,
            )
        )
    unmatched_venue = [order_id for order_id in venue_by if order_id not in sim_by]
    priced = [p for p in pairs if p.slippage_bps is not None]
    mean_bps: Decimal | None = None
    if priced:
        weight = sum((p.quantity for p in priced), ZERO)
        mean_bps = (sum((p.slippage_bps * p.quantity for p in priced), ZERO) / weight).quantize(
            Decimal("0.0001")
        )
    return SlippageReport(
        venue=venue,
        as_of=as_of,
        pairs=tuple(pairs),
        unmatched_sim=tuple(sorted(unmatched_sim)),
        unmatched_venue=tuple(sorted(unmatched_venue)),
        refused=tuple(sorted(refused)),
        mean_slippage_bps=mean_bps,
    )

"""The cover rule for a paper mirror that holds shares and long calls beside short calls (S1b, docs
TOS_STOCK_AND_LEAPS_MIRROR "S1b design").

One rule, no special case per strategy: **an order waits iff filling it in full would leave more short
calls uncovered on its underlying than there are now.** A short call with no cover creates an uncovered
short; selling the shares (or the long call) a short call rests on removes cover; buying anything, or
buying a call back, never does.

Cover is what the mirror book *proves* the venue holds (proven fills, venue-wide), made pessimistic on
both sides: supply counts proven fills only; demand counts the book's shorts plus every open ticket's
unfilled sell remainder. A short call is covered by a lot of ``multiplier`` shares, or by a long call on
the same underlying with expiry >= the short's and strike <= the short's (a debit diagonal), assigned by a
maximum matching so one long call is never counted for two shorts.

Pure functions over the mirror book; tests/test_tos_cover.py pins each rule.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal

from trade_engine.domain.instruments import Equity, Instrument, OptionContract, OptionRight, Side
from trade_engine.domain.orders import Order
from trade_engine.ledger.mirror import MirrorState, ticket_contracts

ZERO = Decimal(0)


def covers(long: OptionContract, short: OptionContract) -> bool:
    """Whether one long call can cover one short call: same underlying, both calls, same multiplier,
    long expiry >= short expiry, long strike <= short strike (a debit diagonal; a credit one covers nothing)."""
    return (
        long.underlying == short.underlying
        and long.right is OptionRight.CALL
        and short.right is OptionRight.CALL
        and long.multiplier == short.multiplier
        and long.expiry >= short.expiry
        and long.strike <= short.strike
    )


def _bare(longs: Sequence[OptionContract], shorts: Sequence[OptionContract]) -> list[OptionContract]:
    """The short call units no long call unit covers, after a maximum matching (Kuhn's augmenting paths): one
    long covers one short, and a long taken early is handed to another short when that covers more."""
    owner: dict[int, int] = {}  # long unit -> the short unit it covers

    def augment(short: int, seen: set[int]) -> bool:
        for index, long in enumerate(longs):
            if index not in seen and covers(long, shorts[short]):
                seen.add(index)
                if index not in owner or augment(owner[index], seen):
                    owner[index] = short
                    return True
        return False

    for short in range(len(shorts)):
        augment(short, set())
    covered = set(owner.values())
    return [call for index, call in enumerate(shorts) if index not in covered]


def uncovered(held: Mapping[Instrument, Decimal]) -> dict[str, Decimal]:
    """Per underlying: the short call contracts nothing in ``held`` covers (only entries above zero)."""
    shorts: dict[str, list[OptionContract]] = {}
    longs: dict[str, list[OptionContract]] = {}
    for instrument, quantity in held.items():
        if isinstance(instrument, OptionContract) and instrument.right is OptionRight.CALL and quantity != 0:
            side = shorts if quantity < 0 else longs
            side.setdefault(instrument.underlying, []).extend([instrument] * int(abs(quantity)))
    found: dict[str, Decimal] = {}
    for underlying, calls in shorts.items():
        shares = held.get(Equity(underlying), ZERO)  # short shares never reach a lot, so they cover nothing
        count = 0
        # Shares go to the larger multiplier first: with mixed multipliers that leaves the most uncovered.
        for call in sorted(_bare(longs.get(underlying, []), calls), key=lambda c: c.multiplier, reverse=True):
            if shares >= call.multiplier:
                shares -= call.multiplier
            else:
                count += 1
        if count:
            found[underlying] = Decimal(count)
    return found


def holdings(mirror: MirrorState) -> dict[Instrument, Decimal]:
    """What the mirror proves the venue holds, pessimistically: the book summed over accounts, less every
    open ticket's unfilled sell remainder (a resting buy is not credited until it fills)."""
    held: dict[Instrument, Decimal] = {}
    for (_account, instrument), quantity in mirror.book.items():
        held[instrument] = held.get(instrument, ZERO) + quantity
    for ticket in mirror.open_tickets:
        for instrument, quantity in ticket_contracts(ticket.queued, ticket.remaining).items():
            if quantity < 0:
                held[instrument] = held.get(instrument, ZERO) + quantity
    return {instrument: quantity for instrument, quantity in held.items() if quantity}


def _sold(order: Order) -> tuple[Instrument, Decimal] | None:
    """What a single-leg SELL of shares or of a call gives up, (instrument, quantity); None for any other order
    (a buy, a put, a combo)."""
    instrument = order.instrument
    if order.side is Side.SELL and (
        isinstance(instrument, Equity)
        or (isinstance(instrument, OptionContract) and instrument.right is OptionRight.CALL)
    ):
        return instrument, order.quantity
    return None


def cover_reason(mirror: MirrorState, order: Order, accepted: Sequence[Order] = ()) -> str | None:
    """None when ``order`` may go now; else why it must wait. ``accepted`` are the orders of the same
    batch already let through: their sells count as resting."""
    sold = _sold(order)
    if sold is None:
        return None
    held = holdings(mirror)
    for earlier in accepted:
        gone = _sold(earlier)
        if gone is not None:
            held[gone[0]] = held.get(gone[0], ZERO) - gone[1]
    instrument, quantity = sold
    underlying = instrument.symbol if isinstance(instrument, Equity) else instrument.underlying
    before = uncovered(held).get(underlying, ZERO)
    after = uncovered({**held, instrument: held.get(instrument, ZERO) - quantity}).get(underlying, ZERO)
    if after <= before:
        return None
    shares = max(held.get(Equity(underlying), ZERO), ZERO)
    longs = sum(
        (
            held_quantity
            for held_instrument, held_quantity in held.items()
            if isinstance(held_instrument, OptionContract)
            and held_instrument.right is OptionRight.CALL
            and held_instrument.underlying == underlying
            and held_quantity > 0
        ),
        ZERO,
    )
    return (
        f"selling {quantity} {instrument.symbol} would leave {after} short {underlying} call(s) uncovered "
        f"({before} now); the mirror book proves {shares} share(s) and {longs} long call(s) held "
        "(proven venue fills only; shares from an assignment are not booked)"
    )

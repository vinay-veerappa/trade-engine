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

S1b skeleton: signatures only, the behaviour arrives with the acceptance tests in tests/test_tos_cover.py.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal

from trade_engine.domain.instruments import Combo, Equity, Instrument, OptionContract, OptionRight, Side
from trade_engine.domain.orders import Order
from trade_engine.ledger.mirror import MirrorState, ticket_contracts


def covers(long: OptionContract, short: OptionContract) -> bool:
    """Whether one long call can cover one short call: same underlying, both calls, same multiplier,
    long expiry >= short expiry, long strike <= short strike (a debit diagonal; a credit one covers nothing)."""
    raise NotImplementedError


def uncovered(held: Mapping[Instrument, Decimal]) -> dict[str, Decimal]:
    """Per underlying: the short call contracts nothing in ``held`` covers (only entries above zero)."""
    raise NotImplementedError


def holdings(mirror: MirrorState) -> dict[Instrument, Decimal]:
    """What the mirror proves the venue holds, pessimistically: the book summed over accounts, less every
    open ticket's unfilled sell remainder (a resting buy is not credited until it fills)."""
    raise NotImplementedError


def cover_reason(mirror: MirrorState, order: Order, accepted: Sequence[Order] = ()) -> str | None:
    """None when ``order`` may go now; else why it must wait. ``accepted`` are the orders of the same
    batch already let through: their sells count as resting."""
    raise NotImplementedError

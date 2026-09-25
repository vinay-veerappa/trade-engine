"""The venue mirror's folded state: what a paperMoney mirror holds (T2, §4.7, I2).

The sim is the book of record; a venue mirror is folded separately, from the
``Mirror*`` events filed under the venue's ledger account (``events.mirror_account``),
and never touches any account's sim positions or cash. Per venue:

- ``tickets``: every queued ticket by key, with its last ack, the venue Order ID a
  read-back proved, the Order Book status last read, and its cumulative fill;
- ``book``: the mirror book ``{(strategy account, contract): signed contracts}``, built
  **only** from proven venue fills (``MirrorFill`` increments, allocated pro-rata to the
  ticket's strategy orders; a vertical books each leg);
- ``queued_orders`` / ``refused_orders``: the strategy orders already mirrored or refused
  at the venue, so a batch is never mirrored twice (I3);
- :meth:`MirrorState.expected`: the book plus the live remainder of every open ticket —
  what the venue should show, per contract. A ticket the venue rejected, cancelled or
  expired contributes nothing more; a filled one contributes only through the book.

Every fold step is pure and refuses a contradiction (I5): a fill for an unknown ticket
or another Order ID, a cumulative that goes down or past the ticket, a second queue of
a strategy order, an Order ID proven for two tickets.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from decimal import ROUND_FLOOR, Decimal
from types import MappingProxyType

from trade_engine.domain.instruments import Combo, Instrument, OptionContract, Side
from trade_engine.domain.orders import OrderState
from trade_engine.ledger.events import MirrorAck, MirrorFill, MirrorQueued, MirrorRefused, mirror_account

ZERO = Decimal("0")
# Order Book states that end a ticket without (more) fills. FILLED ends it only through
# the recorded cumulative, so a FILLED row read before its fill cannot hide the fill.
CLOSED_BOOK_STATES = frozenset({OrderState.CANCELLED, OrderState.EXPIRED, OrderState.REJECTED})


class MirrorFoldError(RuntimeError):
    """A mirror event that contradicts the mirror state (I5). Wrapped by the ledger fold."""


def pro_rata(weights: Sequence[Decimal], whole: Decimal, amount: Decimal) -> list[Decimal]:
    """Integer shares of ``amount`` by weight: floors, remainder one each to first-in.

    ``whole`` is the sum of the weights and ``0 <= amount <= whole``. The remainder goes
    one each to the first shares still under their weight, so no share passes its weight
    (a zero weight gets nothing). The one allocation rule for venue fills (§4.4): the
    mirror fold splits each fill increment over what the orders still lack, and
    ``tos_paper.slippage`` splits a ticket's total.
    """
    shares = [(w * amount / whole).to_integral_value(rounding=ROUND_FLOOR) for w in weights]
    # Each floor drops < 1, so the remainder is under the number of shares; every share
    # under its weight can take one more (floor < weight means floor + 1 <= weight).
    remainder = int(amount - sum(shares, ZERO))
    for index, weight in enumerate(weights):
        if remainder == 0:
            break
        if shares[index] < weight:
            shares[index] += 1
            remainder -= 1
    return shares


def _sign(side: Side) -> int:
    return 1 if side is Side.BUY else -1


def ticket_contracts(queued: MirrorQueued, units: Decimal) -> dict[OptionContract, Decimal]:
    """Signed contracts ``units`` of a ticket put on the venue, per contract (legs as written)."""
    instrument = queued.instrument
    if isinstance(instrument, Combo):
        return {leg.contract: _sign(leg.side) * units * leg.ratio for leg in instrument.legs}
    return {instrument: _sign(queued.side) * units}


@dataclass(frozen=True)
class MirrorTicketState:
    """One queued ticket and everything the venue has proven about it."""

    queued: MirrorQueued
    ack: MirrorAck | None = None
    venue_order_id: str | None = None
    book_status: OrderState | None = None
    filled: Decimal = ZERO
    avg_price: Decimal | None = None
    closed: bool = False  # rejected, cancelled or expired: sticky
    allocated: Mapping[str, Decimal] = field(default_factory=lambda: MappingProxyType({}))

    @property
    def key(self) -> str:
        return self.queued.ticket_key

    @property
    def terminal(self) -> bool:
        return self.closed or self.filled >= self.queued.quantity

    @property
    def remaining(self) -> Decimal:
        """Units still expected to rest or fill at the venue; zero once terminal."""
        return ZERO if self.terminal else self.queued.quantity - self.filled


@dataclass(frozen=True)
class MirrorState:
    """One venue's mirror, folded from its ``Mirror*`` events (empty for other accounts)."""

    venue: str | None = None
    tickets: Mapping[str, MirrorTicketState] = field(default_factory=lambda: MappingProxyType({}))
    book: Mapping[tuple[str, Instrument], Decimal] = field(default_factory=lambda: MappingProxyType({}))
    queued_orders: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))
    refused_orders: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))
    order_ids: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))

    def handled(self, strategy_order_id: str) -> bool:
        """Already queued or refused at this venue: never mirrored again (I3)."""
        return strategy_order_id in self.queued_orders or strategy_order_id in self.refused_orders

    @property
    def open_tickets(self) -> tuple[MirrorTicketState, ...]:
        return tuple(t for _, t in sorted(self.tickets.items()) if not t.terminal)

    def expected(self) -> dict[Instrument, Decimal]:
        """Per contract: the book plus every open ticket's live remainder."""
        expected: dict[Instrument, Decimal] = {}
        for (_account, contract), quantity in self.book.items():
            expected[contract] = expected.get(contract, ZERO) + quantity
        for ticket in self.open_tickets:
            for contract, quantity in ticket_contracts(ticket.queued, ticket.remaining).items():
                expected[contract] = expected.get(contract, ZERO) + quantity
        return expected


def _venue(state: MirrorState, venue: str) -> None:
    if state.venue is not None and state.venue != venue:
        raise MirrorFoldError(f"mirror event for venue '{venue}' folded into venue '{state.venue}' (I8)")


def _ticket(state: MirrorState, key: str, what: str) -> MirrorTicketState:
    ticket = state.tickets.get(key)
    if ticket is None:
        raise MirrorFoldError(f"{what} names ticket '{key}', which was never queued (I5)")
    return ticket


def _with_ticket(state: MirrorState, ticket: MirrorTicketState, **changes) -> MirrorState:
    tickets = dict(state.tickets)
    tickets[ticket.key] = ticket
    return replace(state, tickets=MappingProxyType(tickets), **changes)


def on_queued(state: MirrorState, queued: MirrorQueued) -> MirrorState:
    _venue(state, queued.venue)
    existing = state.tickets.get(queued.ticket_key)
    if existing is not None:
        if existing.queued != queued:
            raise MirrorFoldError(f"ticket '{queued.ticket_key}' was already queued with other contents (I3)")
        return state
    orders = dict(state.queued_orders)
    for allocation in queued.allocations:
        oid = allocation.strategy_order_id
        if oid in state.queued_orders:
            raise MirrorFoldError(
                f"strategy order '{oid}' is already on ticket '{state.queued_orders[oid]}'; "
                "refusing to mirror it twice (I3)"
            )
        if oid in state.refused_orders:
            raise MirrorFoldError(f"strategy order '{oid}' was refused at this venue; it is not queued later (I3)")
        orders[oid] = queued.ticket_key
    return _with_ticket(
        replace(state, venue=queued.venue),
        MirrorTicketState(queued=queued),
        queued_orders=MappingProxyType(orders),
    )


def on_refused(state: MirrorState, refused: MirrorRefused) -> MirrorState:
    _venue(state, refused.venue)
    oid = refused.strategy_order_id
    if oid in state.queued_orders:
        raise MirrorFoldError(
            f"strategy order '{oid}' is on ticket '{state.queued_orders[oid]}'; it cannot also be refused (I11)"
        )
    if oid in state.refused_orders:
        return state  # the first refusal stands
    orders = dict(state.refused_orders)
    orders[oid] = refused.reason
    return replace(state, venue=refused.venue, refused_orders=MappingProxyType(orders))


def on_ack(state: MirrorState, ack: MirrorAck) -> MirrorState:
    _venue(state, ack.venue)
    ticket = _ticket(state, ack.ticket_key, "MirrorAck")
    order_ids = dict(state.order_ids)
    venue_order_id = ticket.venue_order_id
    if ack.venue_order_id is not None:
        if venue_order_id is not None and venue_order_id != ack.venue_order_id:
            raise MirrorFoldError(
                f"ticket '{ticket.key}' is venue order {venue_order_id}; an ack names "
                f"{ack.venue_order_id} (I5)"
            )
        owner = order_ids.get(ack.venue_order_id)
        if owner is not None and owner != ticket.key:
            raise MirrorFoldError(
                f"venue order {ack.venue_order_id} is already ticket '{owner}'; it cannot "
                f"also be '{ticket.key}' (I5)"
            )
        venue_order_id = ack.venue_order_id
        order_ids[venue_order_id] = ticket.key
    closed = ticket.closed or ack.status == "REJECTED" or ack.book_status in CLOSED_BOOK_STATES
    updated = replace(
        ticket,
        ack=ack,
        venue_order_id=venue_order_id,
        book_status=ack.book_status if ack.book_status is not None else ticket.book_status,
        closed=closed,
    )
    return _with_ticket(state, updated, order_ids=MappingProxyType(order_ids))


def on_fill(state: MirrorState, fill: MirrorFill) -> MirrorState:
    _venue(state, fill.venue)
    ticket = _ticket(state, fill.ticket_key, "MirrorFill")
    if ticket.venue_order_id is None or ticket.venue_order_id != fill.venue_order_id:
        raise MirrorFoldError(
            f"MirrorFill for '{ticket.key}' names venue order {fill.venue_order_id}; the "
            f"ticket's proven Order ID is {ticket.venue_order_id} (I5)"
        )
    queued = ticket.queued
    if fill.filled > queued.quantity:
        raise MirrorFoldError(
            f"ticket '{ticket.key}' filled {fill.filled} of {queued.quantity}; refusing the over-fill (I5)"
        )
    if fill.filled < ticket.filled:
        raise MirrorFoldError(
            f"ticket '{ticket.key}' was filled {ticket.filled}; a cumulative {fill.filled} "
            "cannot go down (I5)"
        )
    if fill.filled == ticket.filled:
        if fill.avg_price != ticket.avg_price:
            raise MirrorFoldError(
                f"ticket '{ticket.key}' filled {fill.filled} at {ticket.avg_price}; the venue "
                f"now says {fill.avg_price} for the same fill (I5)"
            )
        return state  # the same cumulative, recorded again: a no-op (I3)
    # The increment is split pro-rata over what each strategy order still lacks. Splitting
    # the cumulative instead is not monotone (weights 2,1,2 filled 2 then 3 would take a
    # contract back from the second order), and a booked fill is never un-booked.
    allocated = dict(ticket.allocated)
    lacking = [a.quantity - allocated.get(a.strategy_order_id, ZERO) for a in queued.allocations]
    pieces = pro_rata(lacking, queued.quantity - ticket.filled, fill.filled - ticket.filled)
    book = dict(state.book)
    for allocation, piece in zip(queued.allocations, pieces):
        allocated[allocation.strategy_order_id] = allocated.get(allocation.strategy_order_id, ZERO) + piece
        for contract, quantity in ticket_contracts(queued, piece).items():
            key = (allocation.strategy_account, contract)
            total = book.get(key, ZERO) + quantity
            if total == 0:
                book.pop(key, None)
            else:
                book[key] = total
    updated = replace(
        ticket, filled=fill.filled, avg_price=fill.avg_price, allocated=MappingProxyType(allocated)
    )
    return _with_ticket(state, updated, book=MappingProxyType(book))


def mirror_states(states: Mapping[str, object], venue: str) -> MirrorState:
    """The mirror of ``venue`` from a fold's per-account states (empty if never mirrored)."""
    state = states.get(mirror_account(venue))
    return MirrorState() if state is None else state.mirror  # type: ignore[attr-defined]


__all__ = [
    "CLOSED_BOOK_STATES",
    "MirrorFoldError",
    "MirrorState",
    "MirrorTicketState",
    "mirror_states",
    "on_ack",
    "on_fill",
    "on_queued",
    "on_refused",
    "pro_rata",
    "ticket_contracts",
]

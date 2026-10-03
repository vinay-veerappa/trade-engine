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

Since P2b the fold steps (``on_queued``/``on_refused``/``on_ack``/``on_fill``),
``pro_rata`` and ``ticket_contracts`` run in Rust (``te_core::ledger``); this module keeps
the carriers and the read-only queries over them.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from types import MappingProxyType

from trade_engine.domain.instruments import Instrument, OptionContract
from trade_engine.domain.orders import OrderState
from trade_engine.ledger import _rs, codec
from trade_engine.ledger.errors import MirrorFoldError
from trade_engine.ledger.events import MirrorAck, MirrorFill, MirrorQueued, MirrorRefused, mirror_account

ZERO = Decimal("0")
# Order Book states that end a ticket without (more) fills. FILLED ends it only through
# the recorded cumulative, so a FILLED row read before its fill cannot hide the fill.
CLOSED_BOOK_STATES = frozenset({OrderState.CANCELLED, OrderState.EXPIRED, OrderState.REJECTED})


def pro_rata(weights: Sequence[Decimal], whole: Decimal, amount: Decimal) -> list[Decimal]:
    """Integer shares of ``amount`` by weight: floors, remainder one each to first-in.

    ``whole`` is the sum of the weights and ``0 <= amount <= whole``. The remainder goes
    one each to the first shares still under their weight, so no share passes its weight
    (a zero weight gets nothing). The one allocation rule for venue fills (§4.4): the
    mirror fold splits each fill increment over what the orders still lack, and
    ``tos_paper.slippage`` splits a ticket's total. The rule is Rust's (P2b).
    """
    shares = _rs.call(_rs.rs.ledger_pro_rata, [str(w) for w in weights], str(whole), str(amount))
    return [Decimal(share) for share in shares]


def ticket_contracts(queued: MirrorQueued, units: Decimal) -> dict[OptionContract, Decimal]:
    """Signed contracts ``units`` of a ticket put on the venue, per contract (legs as written)."""
    data = _rs.call(_rs.rs.ledger_ticket_contracts, codec.text(codec._encode(queued)), str(units))
    return dict(codec.build_text(data))


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

    def exposure(self) -> dict[tuple[str, Instrument], Decimal]:
        """Per (strategy account, contract): the book plus each open ticket's unfilled part.

        What each virtual account holds or has resting at the venue — the conflict screen's
        holdings, so a resting order is never opposed by another account's new one (§4.4).
        """
        exposure: dict[tuple[str, Instrument], Decimal] = dict(self.book)
        for ticket in self.open_tickets:
            for allocation in ticket.queued.allocations:
                lacking = allocation.quantity - ticket.allocated.get(allocation.strategy_order_id, ZERO)
                for contract, quantity in ticket_contracts(ticket.queued, lacking).items():
                    key = (allocation.strategy_account, contract)
                    exposure[key] = exposure.get(key, ZERO) + quantity
        return {key: quantity for key, quantity in exposure.items() if quantity != 0}

    def expected(self) -> dict[Instrument, Decimal]:
        """Per contract: the book plus every open ticket's live remainder."""
        expected: dict[Instrument, Decimal] = {}
        for (_account, contract), quantity in self.book.items():
            expected[contract] = expected.get(contract, ZERO) + quantity
        for ticket in self.open_tickets:
            for contract, quantity in ticket_contracts(ticket.queued, ticket.remaining).items():
                expected[contract] = expected.get(contract, ZERO) + quantity
        return expected


def _step(step: str, state: MirrorState, payload: object) -> MirrorState:
    """One fold step, folded by Rust (P2b): the carrier in, a new carrier out."""
    data = _rs.call(_rs.rs.ledger_mirror_step, step, codec.text(codec.canon(state)), codec.text(codec._encode(payload)))
    return codec.build_text(data)


def on_queued(state: MirrorState, queued: MirrorQueued) -> MirrorState:
    return _step("queued", state, queued)


def on_refused(state: MirrorState, refused: MirrorRefused) -> MirrorState:
    return _step("refused", state, refused)


def on_ack(state: MirrorState, ack: MirrorAck) -> MirrorState:
    return _step("ack", state, ack)


def on_fill(state: MirrorState, fill: MirrorFill) -> MirrorState:
    return _step("fill", state, fill)


def mirror_states(states: Mapping[str, object], venue: str) -> MirrorState:
    """The mirror of ``venue`` from a fold's per-account states (empty if never mirrored)."""
    state = states.get(mirror_account(venue))
    return MirrorState() if state is None else state.mirror  # type: ignore[attr-defined]


codec.register_carrier(MirrorTicketState)
codec.register_carrier(MirrorState)

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

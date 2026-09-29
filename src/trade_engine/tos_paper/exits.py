"""The sim's exits, mirrored at a venue at each in-session pass (T2, owner 2026-09-26).

The sim is the book of record and the venue follows it: at a pass (``EodRunner.run_pass``)
the host sends the venue what closes the gap between the two books, while the market is
open. Per mirrored account and contract the venue holds (the mirror book, built only from
proven venue fills):

- **The sim holds less** (it closed the position: its target filled, a rule closed it, or
  it holds none at all): the venue closes the difference. Every ticket still open on that
  account's contract is cancelled first (a resting target would buy the same contracts
  back again), then one LIMIT DAY ticket is sent at ``price(contract, side)``: the host
  prices it off the pass's snapshot, as the sim would fill it. A close the venue does not
  fill is sent again at the next pass, on that pass's quotes.
- **The sim holds as much** and works a profit target on it (a GTC child of the entry):
  the venue rests the target too, as a LIMIT DAY ticket at the target's price, sent again
  each session while the venue holds the position. The venue's tickets are DAY only.

A **vertical** the venue holds (a filled combo ticket books both legs) is followed as one
structure, never leg by leg (owner 2026-09-29: selling the long leg first leaves a naked
short, which an IRA refuses):

- the sim holds fewer spreads: every open ticket on the pair is cancelled, then one
  closing combo (the entry's legs flipped: a DEBIT vertical) LIMIT DAY is sent for the
  difference at ``price(closing_combo, BUY)``;
- the sim holds as many and works a combo profit target: the venue rests it as a DAY
  combo at the target's price;
- the sim holds none and no longer works the entry, but the venue's entry still rests:
  it is cancelled (it would open what the sim has closed).

A vertical whose two legs the venue holds in different amounts (a legged book) is refused.

Nothing the sim holds more of is chased: an entry reaches the venue only as itself. What
cannot be mirrored is refused at the venue with the reason, never approximated (I5, I11):
a contract with no price at this pass, a resting ticket shared with another account or
whose cancel the venue did not confirm. Refusals are recorded once per session.

Each order's id names the account, contract, session and pass, so a pass run again sends
nothing new (I3), and the next pass's close is a new order. A live follower
(``tos_paper.follow``) names its passes ``follow-HHMM``: an unfilled close is cancelled and
sent again, on the newest quotes, at most once a minute.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from trade_engine.domain.instruments import Combo, ComboLeg, Instrument, OptionContract, Side
from trade_engine.domain.orders import Order, OrderState, OrderType, TimeInForce
from trade_engine.eod.runner import PASSES
from trade_engine.interfaces.clock import Clock
from trade_engine.ledger import Ledger
from trade_engine.ledger.events import MirrorRefused
from trade_engine.ledger.mirror import MirrorState, MirrorTicketState, ticket_contracts
from trade_engine.tos_paper.netting import vertical_reason
from trade_engine.tos_paper.broker import MirrorBinding, TosPaperBroker
from trade_engine.tos_paper.session import (
    MirrorRunReport,
    _append,
    _with,
    cancel_ticket,
    collect_only,
    mirror_of,
    run_mirror,
)

ZERO = Decimal("0")
_WORKING = frozenset({OrderState.SUBMITTED, OrderState.ACCEPTED, OrderState.PARTIALLY_FILLED})

# A contract, or a vertical (a Combo, priced net per spread).
Price = Callable[[Instrument, Side], "Decimal | None"]
FOLLOW_PREFIX = "follow-"


class ExitPlanError(RuntimeError):
    """An exit plan the helper refuses to make (caller error)."""


@dataclass(frozen=True)
class ExitPlan:
    """What one pass does at the venue for the sim's exits."""

    cancel: tuple[str, ...] = ()  # ticket keys, cancelled before any send
    orders: tuple[Order, ...] = ()  # sent with the pass's batch
    refused: tuple[tuple[str, str, str], ...] = ()  # (order id, account, reason)
    # For each ticket to cancel, the close orders that wait on its cancel.
    waits: tuple[tuple[str, str], ...] = ()  # (ticket key, order id)


def close_id(account: str, contract: Instrument, session: date, name: str) -> str:
    return f"exit:{account}:{contract.symbol}:{session.isoformat()}:{name}"


def target_id(target: Order, session: date) -> str:
    return f"{target.order_id}@{session.isoformat()}"


def _refusal_id(account: str, contract: Instrument, session: date) -> str:
    return f"exit:{account}:{contract.symbol}:{session.isoformat()}"


def _open_on(mirror: MirrorState, account: str, contract: OptionContract) -> list[MirrorTicketState]:
    return [
        ticket
        for ticket in mirror.open_tickets
        if any(a.strategy_account == account for a in ticket.queued.allocations)
        and contract in ticket_contracts(ticket.queued, ticket.queued.quantity)
    ]


def _in_vertical(mirror: MirrorState, account: str, contract: OptionContract) -> bool:
    return any(
        isinstance(ticket.queued.instrument, Combo)
        and any(a.strategy_account == account for a in ticket.queued.allocations)
        and contract in ticket_contracts(ticket.queued, ticket.queued.quantity)
        for ticket in mirror.tickets.values()
    )


def plan_exits(
    ledger: Ledger,
    binding: MirrorBinding,
    session: date,
    *,
    name: str,
    price: Price,
    at: datetime,
) -> ExitPlan:
    """The venue's exits at the ``name`` pass of ``session`` (read-only; see above)."""
    if name not in PASSES and not (name.startswith(FOLLOW_PREFIX) and len(name) > len(FOLLOW_PREFIX)):
        raise ExitPlanError(
            f"Unknown pass {name!r}; the passes are {', '.join(PASSES)}, or {FOLLOW_PREFIX}<when> for a follower"
        )
    mirror = mirror_of(ledger, binding.venue_account)
    cancel: list[str] = []
    orders: list[Order] = []
    refused: list[tuple[str, str, str]] = []
    waits: list[tuple[str, str]] = []
    in_vertical = _plan_verticals(ledger, binding, mirror, session, name, price, at, cancel, orders, refused, waits)
    for (account, contract), venue_held in sorted(mirror.book.items(), key=lambda item: (item[0][0], item[0][1].symbol)):
        if account not in binding.mirrored_accounts or not isinstance(contract, OptionContract):
            continue
        if (account, contract) in in_vertical:
            continue  # followed as its vertical, above
        state = ledger.state(account)
        position = state.positions.get(contract)
        sim_held = ZERO if position is None else position.quantity
        same_side = sim_held * venue_held > 0
        closing = venue_held.copy_abs() - (sim_held.copy_abs() if same_side else ZERO)
        side = Side.BUY if venue_held < 0 else Side.SELL
        refusal = _refusal_id(account, contract, session)

        def refuse(reason: str) -> None:
            if not mirror.handled(refusal):
                refused.append((refusal, account, reason))

        if closing <= 0:
            # The venue holds no more than the sim: rest the sim's targets on it (a
            # vertical's target is a combo, never a leg's contract).
            if any(t.queued.side is side for t in _open_on(mirror, account, contract)):
                continue  # a target (or close) already rests
            room = venue_held.copy_abs()
            for target in sorted(state.orders.values(), key=lambda o: o.order_id):
                if room <= 0:
                    break
                if not (
                    target.parent_order_id is not None
                    and target.state in _WORKING
                    and target.instrument == contract
                    and target.side is side
                    and target.order_type is OrderType.LIMIT
                    and target.tif is TimeInForce.GTC
                ):
                    continue
                oid = target_id(target, session)
                quantity = min(room, target.quantity - state.filled_quantity.get(target.order_id, ZERO))
                room -= quantity
                if quantity <= 0 or mirror.handled(oid):
                    continue
                orders.append(
                    Order(
                        order_id=oid, account_id=account, instrument=contract, order_type=OrderType.LIMIT,
                        side=side, quantity=quantity, command_id=oid, created_at=at,
                        limit_price=target.limit_price, tif=TimeInForce.DAY,
                    )
                )
            continue
        oid = close_id(account, contract, session, name)
        if mirror.handled(oid):
            continue
        if _in_vertical(mirror, account, contract):
            refuse(
                f"the sim holds {sim_held} of {contract.symbol} and the venue {venue_held}: a leg of a vertical "
                "no sim entry accounts for, never closed leg by leg; close it at the venue by hand"
            )
            continue
        resting = _open_on(mirror, account, contract)
        shared = sorted(
            {a.strategy_account for t in resting for a in t.queued.allocations if a.strategy_account != account}
        )
        if shared:
            refuse(
                f"the venue must close {closing} of {contract.symbol}, but a resting ticket on it is shared "
                f"with {', '.join(shared)}; cancel it at the venue by hand"
            )
            continue
        limit = price(contract, side)
        if limit is None or limit <= 0:
            refuse(f"the venue must close {closing} of {contract.symbol} and this pass has no price for it")
            continue
        for ticket in resting:
            cancel.append(ticket.key)
            waits.append((ticket.key, oid))
        orders.append(
            Order(
                order_id=oid, account_id=account, instrument=contract, order_type=OrderType.LIMIT,
                side=side, quantity=closing, command_id=oid, created_at=at, limit_price=limit,
                tif=TimeInForce.DAY,
            )
        )
    return ExitPlan(cancel=tuple(cancel), orders=tuple(orders), refused=tuple(refused), waits=tuple(waits))


def _flip(combo: Combo) -> Combo:
    """The combo that closes ``combo``: the same legs, each on the other side."""
    return Combo(
        tuple(ComboLeg(leg.contract, leg.ratio, Side.SELL if leg.side is Side.BUY else Side.BUY) for leg in combo.legs)
    )


def _units(held: dict, combo: Combo) -> Decimal | None:
    """The spreads of ``combo`` that ``held`` (signed contracts) amounts to; None when its
    legs disagree (a legged book)."""
    per_leg = {held.get(leg.contract, ZERO) / ((1 if leg.side is Side.BUY else -1) * leg.ratio) for leg in combo.legs}
    return per_leg.pop() if len(per_leg) == 1 else None


def _verticals(ledger: Ledger, binding: MirrorBinding, mirror: MirrorState) -> dict[tuple[str, Combo], list]:
    """(account, the entry's combo) -> every ticket on its two contracts, for each vertical
    the venue was sent for a mirrored account. The entry's combo is the one a ticket for a
    sim ENTRY (no parent order) carried; its flip is the close."""
    found: dict[tuple[str, Combo], list] = {}
    for ticket in mirror.tickets.values():
        combo = ticket.queued.instrument
        if not isinstance(combo, Combo) or vertical_reason(combo) is not None:
            continue
        for allocation in ticket.queued.allocations:
            account = allocation.strategy_account
            if account not in binding.mirrored_accounts:
                continue
            order = ledger.state(account).orders.get(allocation.strategy_order_id)
            if order is not None and order.parent_order_id is None:
                found.setdefault((account, combo), [])
    for (account, combo), tickets in found.items():
        pair = {leg.contract for leg in combo.legs}
        tickets.extend(
            t for t in mirror.tickets.values()
            if isinstance(t.queued.instrument, Combo)
            and {leg.contract for leg in t.queued.instrument.legs} == pair
            and any(a.strategy_account == account for a in t.queued.allocations)
        )
    return found


def _plan_verticals(ledger, binding, mirror, session, name, price, at, cancel, orders, refused, waits) -> set:
    """Plan each vertical the venue holds or rests as one structure (see the module doc).
    Returns the (account, contract) pairs it covered, which the per-contract plan skips."""
    covered: set[tuple[str, OptionContract]] = set()
    verticals = sorted(_verticals(ledger, binding, mirror).items(), key=lambda kv: (kv[0][0], kv[0][1].symbol))
    for (account, opening), tickets in verticals:
        contracts = [leg.contract for leg in opening.legs]
        covered.update((account, contract) for contract in contracts)
        state = ledger.state(account)
        closing_combo = _flip(opening)
        venue = _units({c: mirror.book.get((account, c), ZERO) for c in contracts}, opening)
        sim = _units({c: state.positions[c].quantity if c in state.positions else ZERO for c in contracts}, opening)
        refusal = _refusal_id(account, opening, session)
        resting = [t for t in tickets if not t.terminal]
        if venue is None or sim is None:
            if not mirror.handled(refusal):
                side = "venue" if venue is None else "sim"
                refused.append((refusal, account, f"the {side} holds the two legs of the vertical {opening.symbol} in different "
                                                  "amounts (a legged book); close it at the venue by hand"))
            continue
        closing = venue - max(sim, ZERO)
        if closing > 0:
            oid = close_id(account, closing_combo, session, name)
            if mirror.handled(oid):
                continue
            shared = sorted(
                {a.strategy_account for t in resting for a in t.queued.allocations if a.strategy_account != account}
            )
            limit = price(closing_combo, Side.BUY)
            if shared:
                reason = (f"the venue must close {closing} of {opening.symbol}, but a resting ticket on it is "
                          f"shared with {', '.join(shared)}; cancel it at the venue by hand")
            elif limit is None or limit <= 0:
                reason = f"the venue must close {closing} of {opening.symbol} and this pass has no price for it"
            else:
                reason = None
            if reason is not None:
                if not mirror.handled(refusal):
                    refused.append((refusal, account, reason))
                continue
            for ticket in resting:
                cancel.append(ticket.key)
                waits.append((ticket.key, oid))
            orders.append(
                Order(
                    order_id=oid, account_id=account, instrument=closing_combo, order_type=OrderType.LIMIT,
                    side=Side.BUY, quantity=closing, command_id=oid, created_at=at, limit_price=limit,
                    tif=TimeInForce.DAY,
                )
            )
            continue
        if sim <= 0:
            # The sim holds none: an entry still resting at the venue would open what the
            # sim no longer has, unless the sim is still working that entry itself.
            for ticket in resting:
                works = any(
                    (o := state.orders.get(a.strategy_order_id)) is not None and o.state in _WORKING
                    for a in ticket.queued.allocations
                )
                if ticket.queued.instrument == opening and not works:
                    cancel.append(ticket.key)
            continue
        if venue <= 0 or any(t.queued.instrument == closing_combo for t in resting):
            continue  # nothing held at the venue, or a target (or close) already rests
        room = venue
        for target in sorted(state.orders.values(), key=lambda o: o.order_id):
            if room <= 0:
                break
            if not (
                target.parent_order_id is not None
                and target.state in _WORKING
                and target.instrument == closing_combo
                and target.side is Side.BUY
                and target.order_type is OrderType.LIMIT
                and target.tif is TimeInForce.GTC
            ):
                continue
            oid = target_id(target, session)
            quantity = min(room, target.quantity - state.filled_quantity.get(target.order_id, ZERO))
            room -= quantity
            if quantity <= 0 or mirror.handled(oid):
                continue
            orders.append(
                Order(
                    order_id=oid, account_id=account, instrument=closing_combo, order_type=OrderType.LIMIT,
                    side=Side.BUY, quantity=quantity, command_id=oid, created_at=at,
                    limit_price=target.limit_price, tif=TimeInForce.DAY,
                )
            )
    return covered


def run_pass_mirror(
    ledger: Ledger,
    broker: TosPaperBroker,
    entries: Sequence[Order],
    session: date,
    *,
    name: str,
    price: Price,
    clock: Clock,
) -> MirrorRunReport:
    """One pass at the venue: collect, cancel what the exits replace, then send the
    pass's ``entries`` and exits in one batch (``run_mirror``). Idempotent (I3)."""
    venue = broker.venue
    collected = collect_only(ledger, broker, clock=clock)
    plan = plan_exits(ledger, broker.binding, session, name=name, price=price, at=clock.now_utc())
    refused = list(plan.refused)
    if not collected.halted:
        accounts = {order.order_id: order.account_id for order in plan.orders}
        for key in plan.cancel:
            ack = cancel_ticket(ledger, broker, key, clock=clock)
            if ack.status != "ACCEPTED":
                refused += [
                    (oid, accounts[oid],
                     f"the resting ticket {key} was not cancelled ({ack.message or ack.status}); "
                     "nothing is sent over it")
                    for ticket, oid in plan.waits
                    if ticket == key
                ]
    # A halted venue refuses the exits in run_mirror, each with the reason (I11).
    now = clock.now_utc()
    written = _append(
        ledger,
        venue,
        clock,
        [
            (MirrorRefused(venue=venue, strategy_order_id=oid, strategy_account=account, reason=reason, at=now),
             f"refused:{oid}")
            for oid, account, reason in dict.fromkeys(refused)
        ],
    )
    # A close refused above is handled now: run_mirror skips it.
    report = run_mirror(ledger, broker, [*entries, *plan.orders], clock=clock)
    return _with(
        report,
        fills=collected.fills + report.fills,
        closes=collected.closes + report.closes,
        refused=tuple(written) + report.refused,
        halted=report.halted or collected.halted,
    )


__all__ = [
    "FOLLOW_PREFIX", "ExitPlan", "ExitPlanError", "Price", "close_id", "plan_exits", "run_pass_mirror",
    "target_id",
]

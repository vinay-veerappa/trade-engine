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

Nothing the sim holds more of is chased: an entry reaches the venue only as itself. What
cannot be mirrored is refused at the venue with the reason, never approximated (I5, I11):
a leg of a vertical (its fills cannot be read back live), a contract with no price at
this pass, a resting ticket shared with another account or whose cancel the venue did
not confirm. Refusals are recorded once per session.

Each order's id names the account, contract, session and pass, so a pass run again sends
nothing new (I3), and the next pass's close is a new order.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from trade_engine.domain.instruments import Combo, OptionContract, Side
from trade_engine.domain.orders import Order, OrderState, OrderType, TimeInForce
from trade_engine.eod.runner import PASSES
from trade_engine.interfaces.clock import Clock
from trade_engine.ledger import Ledger
from trade_engine.ledger.events import MirrorRefused
from trade_engine.ledger.mirror import MirrorState, MirrorTicketState, ticket_contracts
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

Price = Callable[[OptionContract, Side], "Decimal | None"]


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


def close_id(account: str, contract: OptionContract, session: date, name: str) -> str:
    return f"exit:{account}:{contract.symbol}:{session.isoformat()}:{name}"


def target_id(target: Order, session: date) -> str:
    return f"{target.order_id}@{session.isoformat()}"


def _refusal_id(account: str, contract: OptionContract, session: date) -> str:
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
    if name not in PASSES:
        raise ExitPlanError(f"Unknown pass {name!r}; the passes are {', '.join(PASSES)}")
    mirror = mirror_of(ledger, binding.venue_account)
    cancel: list[str] = []
    orders: list[Order] = []
    refused: list[tuple[str, str, str]] = []
    waits: list[tuple[str, str]] = []
    for (account, contract), venue_held in sorted(mirror.book.items(), key=lambda item: (item[0][0], item[0][1].symbol)):
        if account not in binding.mirrored_accounts or not isinstance(contract, OptionContract):
            continue
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
                f"the sim holds {sim_held} of {contract.symbol} and the venue {venue_held}: a vertical's "
                "exit is not mirrored (its fills cannot be read back live); close it at the venue by hand"
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


__all__ = ["ExitPlan", "ExitPlanError", "close_id", "plan_exits", "run_pass_mirror", "target_id"]

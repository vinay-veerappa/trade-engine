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
import json
from datetime import date, datetime
from decimal import Decimal

from trade_engine.domain.instruments import Instrument, Side
from trade_engine.domain.orders import Order, OrderType, TimeInForce
from trade_engine.interfaces.clock import Clock
from trade_engine.ledger import Ledger, codec
from trade_engine.ledger.events import MirrorRefused
from trade_engine.tos_paper import _rs
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

# A contract, or a vertical (a Combo, priced net per spread).
Price = Callable[[Instrument, Side], "Decimal | None"]
# The order as the venue can take it (a limit on the venue's own tick, rounded against us),
# or the reason it cannot: the venue's price grid is the host's to know.
Express = Callable[[Order], "Order | str"]
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
    return _rs.decide("exit_id", {"which": "close", "session": session.isoformat(), "account": account,
                                  "instrument": _rs.wire(contract), "name": name, "order_id": ""})["id"]


def target_id(target: Order, session: date) -> str:
    return _rs.decide("exit_id", {"which": "target", "session": session.isoformat(), "account": target.account_id,
                                  "instrument": _rs.wire(target.instrument), "name": "",
                                  "order_id": target.order_id})["id"]


def _order(d: dict) -> Order:
    return Order(order_id=d["order_id"], account_id=d["account_id"], instrument=_rs.unwire(d["instrument"]),
                 order_type=OrderType(d["order_type"]), side=Side(d["side"]), quantity=_rs.dec(d["quantity"]),
                 command_id=d["command_id"], created_at=datetime.fromisoformat(d["created_at"]),
                 limit_price=_rs.dec(d["limit_price"]), tif=TimeInForce(d["tif"]))


def plan_exits(
    ledger: Ledger,
    binding: MirrorBinding,
    session: date,
    *,
    name: str,
    price: Price,
    at: datetime,
) -> ExitPlan:
    """The venue's exits at the ``name`` pass of ``session`` (read-only; see above).

    The plan is decided in Rust over the mirror book and the sim's accounts. The quotes are the
    host's: the core names the (contract, side) it needs next, the host's ``price`` answers it
    once, and the plan is decided again with that answer, until it needs nothing more.
    """
    mirror = codec.canon(mirror_of(ledger, binding.venue_account))
    accounts = [[a, codec.canon(ledger.state(a))] for a in binding.mirrored_accounts]
    known: dict[tuple[str, str], list] = {}  # (instrument as JSON, side) -> the prices-table row
    while True:
        out = _rs.decide("plan_exits", {
            "mirror": mirror, "accounts": accounts, "mirrored": list(binding.mirrored_accounts),
            "session": session.isoformat(), "name": name, "at": at.isoformat(), "prices": list(known.values())})
        need = next(((w, side) for w, side in out["priced"] if (json.dumps(w, sort_keys=True), side) not in known),
                    None)
        if need is None:
            break
        w, side = need
        quote = price(_rs.unwire(w), Side(side))
        known[(json.dumps(w, sort_keys=True), side)] = [w, side, None if quote is None else str(quote)]
    return ExitPlan(
        cancel=tuple(out["cancel"]), orders=tuple(_order(o) for o in out["orders"]),
        refused=tuple(tuple(x) for x in out["refused"]), waits=tuple(tuple(x) for x in out["waits"]))


def run_pass_mirror(
    ledger: Ledger,
    broker: TosPaperBroker,
    entries: Sequence[Order],
    session: date,
    *,
    name: str,
    price: Price,
    clock: Clock,
    express: Express | None = None,
    may_wait: bool = False,
) -> MirrorRunReport:
    """One pass at the venue: collect, cancel what the exits replace, then send the
    pass's ``entries`` and exits in one batch (``run_mirror``). Idempotent (I3).

    ``express``, when given, turns each order into the one the venue can take; an order it
    cannot is refused with its reason, and a resting ticket that only it replaced is kept."""
    venue = broker.venue
    collected = collect_only(ledger, broker, clock=clock)
    if collected.deferred is not None:
        # The venue could not be asked: no cancel, no refusal, no send goes out over an unread venue.
        # Nothing was recorded, so the whole pass is the next run's to do (I3 keeps it once).
        return collected
    plan = plan_exits(ledger, broker.binding, session, name=name, price=price, at=clock.now_utc())
    refused = list(plan.refused)
    sending = [*entries, *plan.orders]
    cancels = list(plan.cancel)
    if express is not None:
        expressed: list[Order] = []
        for order in sending:
            out = express(order)
            if isinstance(out, str):
                refused.append((order.order_id, order.account_id, out))
            else:
                expressed.append(out)
        sending = expressed
        dropped = {oid for oid, _, _ in refused}
        cancels = [
            key for key in cancels
            if not (waiting := [oid for ticket, oid in plan.waits if ticket == key])
            or any(oid not in dropped for oid in waiting)
        ]
    if not collected.halted:
        accounts = {order.order_id: order.account_id for order in plan.orders}
        for key in cancels:
            ack = cancel_ticket(ledger, broker, key, clock=clock)
            if ack.status != "ACCEPTED":
                waiting = [oid for ticket, oid in plan.waits if ticket == key]
                refused += [
                    (oid, accounts[oid],
                     f"the resting ticket {key} was not cancelled ({ack.message or ack.status}); "
                     "nothing is sent over it")
                    for oid in waiting
                ]
                if not waiting:  # a stale entry or target: nothing waits on it, but it still rests
                    ticket = mirror_of(ledger, venue).tickets[key]
                    refused.append((
                        f"cancel:{key}@{session.isoformat()}", ticket.queued.allocations[0].strategy_account,
                        f"the resting ticket {key} was not cancelled ({ack.message or ack.status}); "
                        "it is tried again next pass",
                    ))
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
    report = run_mirror(ledger, broker, sending, clock=clock, may_wait=may_wait)
    return _with(
        report,
        fills=collected.fills + report.fills,
        closes=collected.closes + report.closes,
        refused=tuple(written) + report.refused,
        halted=report.halted or collected.halted,
    )


__all__ = [
    "FOLLOW_PREFIX", "ExitPlan", "ExitPlanError", "Express", "Price", "close_id", "plan_exits", "run_pass_mirror",
    "target_id",
]

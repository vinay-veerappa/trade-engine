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

from trade_engine.domain.instruments import Instrument, Side
from trade_engine.domain.orders import Order
from trade_engine.interfaces.clock import Clock
from trade_engine.ledger import Ledger
from trade_engine.tos_paper import _rs
from trade_engine.tos_paper.broker import MirrorBinding, TosPaperBroker
from trade_engine.tos_paper.session_native import MirrorRunReport

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
    return _rs.call(_rs.rs.tos_exits_close_id, account, contract, session.isoformat(), name)


def target_id(target: Order, session: date) -> str:
    return _rs.call(_rs.rs.tos_exits_target_id, target.order_id, session.isoformat())


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
    return _rs.call(
        _rs.rs.tos_paper_follow_plan_exits,
        ledger,
        binding,
        session,
        name,
        price,
        at,
    )


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
    settle=None,
) -> MirrorRunReport:
    """One pass at the venue: collect, cancel what the exits replace, then send the
    pass's ``entries`` and exits in one batch (``run_mirror``). Idempotent (I3).

    ``express``, when given, turns each order into the one the venue can take; an order it
    cannot is refused with its reason, and a resting ticket that only it replaced is kept."""
    return _rs.call(
        _rs.rs.tos_paper_follow_run_pass_mirror,
        ledger,
        broker,
        entries,
        session,
        name,
        price,
        clock,
        express,
        may_wait,
        settle,
    )


__all__ = [
    "FOLLOW_PREFIX", "ExitPlan", "ExitPlanError", "Express", "Price", "close_id", "plan_exits", "run_pass_mirror",
    "target_id",
]

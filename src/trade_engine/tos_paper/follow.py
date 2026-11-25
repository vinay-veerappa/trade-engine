"""A live venue mirror that follows an intraday sim without ever blocking it (T2 follow,
owner 2026-09-29).

The intraday service holds its ledger's OS lock from the open to the close (I4). A mirror
that wrote there would have to take that lock, and a venue that hangs or refuses would
then stall the sim. The owner's ruling is that the mirror exists to measure the sim
against a real venue, execution and fills, so it may never block it. The follower
therefore reads the sim through :class:`~trade_engine.ledger.reader.LedgerReader` (no
lock, no writes) and keeps the venue's state in a ledger of its own. :class:`SplitLedger`
joins the two for the mirror code, which reads one ledger:

- the binding's mirrored (strategy) accounts are read from the sim, and never written;
- every other account (the venue's ``mirror:<venue>`` account) is read from and written
  to the follower's own ledger.

Each :func:`follow_cycle` is one ``tos_paper.exits`` pass named ``follow-HHMM`` (ET):
collect, cancel what the exits replace, then send the sim's new entries and its exits in
one batch. A vertical is followed as one structure (see ``tos_paper.exits``).

An entry reaches the venue as the sim made it, or is refused with the reason (I11):

- older than ``max_age`` when the follower first sees it (the follower was down, or
  behind): a late copy is not the sim's trade, and would measure nothing;
- one the sim has already opened and closed: the venue would open what the sim is
  already out of.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from trade_engine.domain.orders import Order
from trade_engine.interfaces.clock import Clock
from trade_engine.ledger.state import AccountState
from trade_engine.tos_paper import _rs
from trade_engine.tos_paper.broker import MirrorBinding, TosPaperBroker
from trade_engine.tos_paper.exits import Express, Price
from trade_engine.tos_paper.session_native import MirrorRunReport

ET = ZoneInfo("America/New_York")
MAX_AGE = timedelta(minutes=5)


class SplitLedgerError(RuntimeError):
    """A write to a sim account, or a read that would mix two ledgers' sequence numbers."""


class SplitLedger(_rs.rs.SplitLedgerBase):
    """The sim's strategy accounts, read-only, joined to the follower's own mirror ledger."""


def follow_entries(
    ledger: SplitLedger,
    binding: MirrorBinding,
    *,
    session_open: datetime,
    now: datetime,
    max_age: timedelta = MAX_AGE,
) -> tuple[tuple[Order, ...], tuple[tuple[str, str, str], ...]]:
    """(entries to send, (order id, account, reason) refused) for this cycle.

    An entry is a sim order with no parent, created since ``session_open``, that the sim
    did not cancel, reject or expire unfilled, and this venue has not handled.
    """
    return _rs.call(
        _rs.rs.tos_paper_follow_entries,
        ledger,
        binding,
        session_open,
        now,
        max_age // timedelta(microseconds=1),
    )


def _flat(state: AccountState, order: Order) -> bool:
    """The sim holds none of ``order``'s instrument (a vertical by its spreads)."""
    return _rs.call(_rs.rs.tos_follow_flat, state, order)


def pass_name(now: datetime) -> str:
    # A naive clock reads as local time, as ``astimezone`` always did.
    when = now if now.tzinfo is not None else now.astimezone(UTC)
    return _rs.call(_rs.rs.tos_follow_pass_name, when)


def follow_cycle(
    ledger: SplitLedger,
    broker: TosPaperBroker,
    session: date,
    *,
    session_open: datetime,
    price: Price,
    clock: Clock,
    max_age: timedelta = MAX_AGE,
    express: Express | None = None,
    settle=None,
) -> MirrorRunReport:
    """One follow pass at the venue (see the module doc). Idempotent within a minute (I3)."""
    return _rs.call(
        _rs.rs.tos_paper_follow_cycle,
        ledger,
        broker,
        session,
        session_open,
        price,
        clock,
        max_age // timedelta(microseconds=1),
        express,
        settle,
    )


__all__ = ["MAX_AGE", "SplitLedger", "SplitLedgerError", "follow_cycle", "follow_entries", "pass_name"]

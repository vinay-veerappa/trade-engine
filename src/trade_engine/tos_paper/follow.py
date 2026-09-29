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

from collections.abc import Collection, Iterable, Sequence
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from trade_engine.domain.instruments import Combo
from trade_engine.domain.orders import Order, OrderState
from trade_engine.interfaces.clock import Clock
from trade_engine.ledger import Event, EventKind, Ledger
from trade_engine.ledger.events import MirrorRefused
from trade_engine.ledger.state import AccountState
from trade_engine.tos_paper.broker import MirrorBinding, TosPaperBroker
from trade_engine.tos_paper.exits import FOLLOW_PREFIX, Express, Price, _units, run_pass_mirror
from trade_engine.tos_paper.session import _ENDED_UNFILLED, MirrorRunReport, _append, mirror_of

ET = ZoneInfo("America/New_York")
MAX_AGE = timedelta(minutes=5)


class SplitLedgerError(RuntimeError):
    """A write to a sim account, or a read that would mix two ledgers' sequence numbers."""


class SplitLedger:
    """The sim's strategy accounts, read-only, joined to the follower's own mirror ledger."""

    def __init__(self, sim: Ledger, mirror: Ledger, sim_accounts: Collection[str]) -> None:
        if not sim_accounts:
            raise SplitLedgerError("a split ledger needs the sim accounts it reads")
        self.sim = sim
        self.mirror = mirror
        self.sim_accounts = frozenset(sim_accounts)

    def _of(self, account: str) -> Ledger:
        return self.sim if account in self.sim_accounts else self.mirror

    def state(self, account: str) -> AccountState:
        return self._of(account).state(account)

    def accounts(self) -> list[str]:
        return sorted(set(self.sim_accounts) | set(self.mirror.accounts()))

    def events(self, *, after: int | None = None, account: str | None = None) -> list[Event]:
        if account is None:
            raise SplitLedgerError("name the account: two ledgers' sequence numbers do not compare")
        return self._of(account).events(after=after, account=account)

    def events_of_kind(self, kind: EventKind, *, account: str | None = None) -> list[Event]:
        if account is not None:
            return self._of(account).events_of_kind(kind, account=account)
        sim = [e for e in self.sim.events_of_kind(kind) if e.account in self.sim_accounts]
        return self.mirror.events_of_kind(kind) + sim

    def _writes(self, events: Iterable[Event]) -> list[Event]:
        events = list(events)
        into_sim = sorted({e.account for e in events if e.account in self.sim_accounts})
        if into_sim:
            raise SplitLedgerError(f"the follower never writes the sim's accounts ({', '.join(into_sim)})")
        return events

    def extend(self, events: Iterable[Event]) -> list[Event]:
        return self.mirror.extend(self._writes(events))

    def append(self, event: Event, **kwargs) -> Event:
        self._writes([event])
        return self.mirror.append(event, **kwargs)


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
    mirror = mirror_of(ledger, binding.venue_account)
    send: list[Order] = []
    refused: list[tuple[str, str, str]] = []
    for account in binding.mirrored_accounts:
        state = ledger.state(account)
        for order in sorted(state.orders.values(), key=lambda o: o.order_id):
            if (
                order.parent_order_id is not None
                or order.created_at < session_open
                or order.state in _ENDED_UNFILLED
                or mirror.handled(order.order_id)
            ):
                continue
            age = now - order.created_at
            if age > max_age:
                refused.append((order.order_id, account,
                                f"the follower first saw this entry {int(age.total_seconds())}s after the sim made it "
                                f"(more than {int(max_age.total_seconds())}s); a late copy is not the sim's trade"))
            elif order.state is OrderState.FILLED and _flat(state, order):
                refused.append((order.order_id, account,
                                "the sim opened and closed this entry before the follower saw it; "
                                "the venue would open what the sim is out of"))
            else:
                send.append(order)
    return tuple(send), tuple(refused)


def _flat(state: AccountState, order: Order) -> bool:
    """The sim holds none of ``order``'s instrument (a vertical by its spreads)."""
    held = {c: p.quantity for c, p in state.positions.items()}
    if isinstance(order.instrument, Combo):
        units = _units(held, order.instrument)
        return units is not None and units == 0
    return held.get(order.instrument, 0) == 0


def pass_name(now: datetime) -> str:
    return f"{FOLLOW_PREFIX}{now.astimezone(ET):%H%M}"


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
) -> MirrorRunReport:
    """One follow pass at the venue (see the module doc). Idempotent within a minute (I3)."""
    now = clock.now_utc()
    entries, refused = follow_entries(ledger, broker.binding, session_open=session_open, now=now, max_age=max_age)
    _append(
        ledger,  # type: ignore[arg-type]
        broker.venue,
        clock,
        [
            (MirrorRefused(venue=broker.venue, strategy_order_id=oid, strategy_account=account, reason=reason, at=now),
             f"refused:{oid}")
            for oid, account, reason in refused
        ],
    )
    return run_pass_mirror(
        ledger,  # type: ignore[arg-type]
        broker,
        list(entries),
        session,
        name=pass_name(now),
        price=price,
        clock=clock,
        express=express,
    )


__all__ = ["MAX_AGE", "SplitLedger", "SplitLedgerError", "follow_cycle", "follow_entries", "pass_name"]

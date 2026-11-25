"""One mirror session, host-callable: the ledger appends around a TosPaperBroker (§4.7).

The native implementation: pure decisions (which orders form the batch, halt/unproven,
cover classification, write-ahead items, clear-halt) run in Rust through the typed
``trade_engine_rs.session_*`` pyfunctions (W6-E2: no JSON door), and the session's
orchestration (collect → reconcile → queue → drain) runs in Rust through the
``trade_engine_rs`` ``run_mirror`` / ``collect_only`` / ``cancel_ticket`` / ``clear_halt``
host functions (W7-CORE-C13). The carriers, the report, the exception and the batch
helpers stay here.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import date

import trade_engine_rs as rs

from trade_engine.domain.orders import Order, OrderState
from trade_engine.interfaces.broker import VenueAck
from trade_engine.interfaces.clock import Clock
from trade_engine.ledger import EventKind, Ledger
from trade_engine.ledger.events import (
    MirrorAck,
    MirrorFill,
    MirrorQueued,
    MirrorRefused,
    VenueReconcile,
    mirror_account,
)
from trade_engine.ledger.mirror import MirrorState
from trade_engine.ledger.state import mirror_state
from trade_engine.tos_paper import _rs
from trade_engine.tos_paper.broker import MirrorBinding, TosPaperBroker

# The sim still works these: a mirrored order must be live in the book of record.
_WORKING = frozenset({OrderState.SUBMITTED, OrderState.ACCEPTED, OrderState.PARTIALLY_FILLED})
# A morning entry the sim filled at once is still mirrored; one it refused or dropped is not.
_ENDED_UNFILLED = frozenset({OrderState.CANCELLED, OrderState.REJECTED, OrderState.EXPIRED})


class MirrorSessionError(RuntimeError):
    """A mirror session the helper refuses to start (I5)."""


@dataclass(frozen=True)
class MirrorRunReport:
    """What one mirror session appended (the events as written)."""

    venue: str
    fills: tuple[MirrorFill, ...] = ()
    closes: tuple[MirrorAck, ...] = ()
    reconcile: VenueReconcile | None = None  # after collecting, before any send
    refused: tuple[MirrorRefused, ...] = ()
    queued: tuple[MirrorQueued, ...] = ()
    acks: tuple[MirrorAck, ...] = ()
    drain_reconcile: VenueReconcile | None = None
    halted: bool = False
    # Why the venue could not be asked (TransportUnavailable): set means nothing past what is listed
    # above was recorded or sent, nothing was halted, and the orders stay pending for the next run (I11).
    deferred: str | None = None
    # Orders the cover rule held back this run (``may_wait``): (order id, why). Nothing about them was
    # recorded or sent; the caller presents them again next run, and the venue never saw one (I3).
    waiting: tuple[tuple[str, str], ...] = ()
    # Expired contracts settled out of the mirror book before collecting (``tos_paper.expiry``), and
    # the expiries that could not settle yet ("<expiry>: <why>"); those drift until they can.
    expired: tuple = ()
    unsettled: tuple[str, ...] = ()


def mirror_of(ledger: Ledger, venue: str) -> MirrorState:
    """The venue's folded mirror, from the ledger's committed state."""
    return mirror_state({mirror_account(venue): ledger.state(mirror_account(venue))}, venue)


# -- what to mirror --------------------------------------------------------------------


def _held(ledger: Ledger, orders: Sequence[Order]) -> tuple[Order, ...]:
    """Rust's chosen orders, each as the sim holds it now (Rust's copy when the sim lost it)."""
    found: list[Order] = []
    for order in orders:
        state = ledger.state(order.account_id)
        found.append(state.orders.get(order.order_id, order))
    return tuple(found)


def _accounts_data(ledger: Ledger, binding: MirrorBinding) -> dict[str, tuple]:
    """Each mirrored account's EOD markers, created-order events and current orders: the batch pyfunctions' input.

    Markers are ``(seq, session, job)``, created batches ``(seq, orders)``, and the current orders a tuple.
    """
    accounts_data = {}
    for account in binding.mirrored_accounts:
        markers = tuple(
            (e.seq, e.payload.session, e.payload.job)
            for e in ledger.events_of_kind(EventKind.EOD_RUN, account=account)
        )
        created = tuple(
            (e.seq, tuple(e.payload.orders))
            for e in ledger.events_of_kind(EventKind.ORDERS_CREATED, account=account)
        )
        current = tuple(ledger.state(account).orders.values())
        accounts_data[account] = (markers, created, current)
    return accounts_data


def working_orders(ledger: Ledger, binding: MirrorBinding) -> tuple[Order, ...]:
    """Every entry the mirrored accounts' sim still works that this venue has not handled.

    Entries only (no parent order): exits reach the venue through ``tos_paper.exits``. In account
    order, then order-id order, so the first-in rule is deterministic.
    """
    account_orders = {
        account: tuple(ledger.state(account).orders.values()) for account in binding.mirrored_accounts
    }
    found = _rs.call(
        _rs.rs.session_working_orders,
        list(binding.mirrored_accounts),
        account_orders,
        mirror_of(ledger, binding.venue_account),
    )
    return _held(ledger, found)


def pending_orders(
    ledger: Ledger,
    binding: MirrorBinding,
    session: date,
    *,
    calendar,
    job: str | None = None,
) -> tuple[Order, ...]:
    """The EOD batch of ``session`` still to mirror: the D+1 entries, not yet handled.

    An entry is in the batch when it was created after ``session``'s close and before
    the account's ``EodRun`` marker for ``session`` (``_submit_new_option_entries``
    creates them in that window), the sim still works it, and this venue has neither
    queued nor refused it. Refuses when a mirrored account has no marker for the
    session (its batch may be incomplete), or markers of two jobs and no ``job`` (I5).
    """
    found = _rs.call(
        _rs.rs.session_pending_orders,
        list(binding.mirrored_accounts),
        _accounts_data(ledger, binding),
        session,
        calendar.session_close(session),
        job,
        mirror_of(ledger, binding.venue_account),
    )
    return _held(ledger, found)


def morning_orders(
    ledger: Ledger,
    binding: MirrorBinding,
    session: date,
    *,
    calendar,
    job: str | None = None,
) -> tuple[Order, ...]:
    """The batch of ``session``'s morning pass still to mirror, not yet handled.

    An entry is in it when it was created in the session and before the account's
    morning marker (``eod:<job>-morning:...``), and the sim did not refuse, cancel or
    expire it unfilled: a morning entry fills in the sim at the snapshot it was decided
    on, and the venue is sent the same order. ``job`` names the EOD job, as for
    :func:`pending_orders`. Refuses when a mirrored account has no morning marker for the
    session (the pass may not have run, or not finished), or two jobs' and no ``job`` (I5).
    """
    found = _rs.call(
        _rs.rs.session_morning_orders,
        list(binding.mirrored_accounts),
        _accounts_data(ledger, binding),
        session,
        calendar.session_open(session),
        job,
        mirror_of(ledger, binding.venue_account),
    )
    return _held(ledger, found)


# -- the session -----------------------------------------------------------------------


def cancel_ticket(ledger: Ledger, broker: TosPaperBroker, ticket_key: str, *, clock: Clock) -> VenueAck:
    """Cancel one resting ticket at the venue and record it in the ledger (I2).

    The broker is restored from the fold first, so the cancel works after a restart and
    takes only the unfilled remainder out of the expectation. An ACCEPTED cancel is
    appended as a ``MirrorAck`` (``book_status`` CANCELLED), which closes the ticket in the
    fold for good; anything else is returned unrecorded (the ticket is still open).
    """
    return rs.cancel_ticket(ledger, broker, ticket_key, clock=clock)


def collect_only(ledger: Ledger, broker: TosPaperBroker, *, clock: Clock, settle=None) -> MirrorRunReport:
    """Fills and the reconcile, no sends: the after-close pass (and a safe probe)."""
    return rs.collect_only(ledger, broker, clock=clock, settle=settle)


def run_mirror(
    ledger: Ledger,
    broker: TosPaperBroker,
    orders: Sequence[Order],
    *,
    clock: Clock,
    may_wait: bool = False,
    settle=None,
) -> MirrorRunReport:
    """One mirror session for ``orders`` (usually :func:`pending_orders`). Idempotent (I3).

    Orders this venue already queued or refused are skipped; a repeated order id keeps
    its first occurrence. The broker must be connected.

    A call that the mirror book does not prove covered (or the shares / long call a resting short
    call rests on) is not sent: with ``may_wait`` it is held, recorded nowhere and listed in the
    report's ``waiting``; without it, it is refused at once with the cover that is missing (I11), so
    a caller that never learned about waiting can never send a naked call. See ``tos_paper.cover``.
    """
    return rs.run_mirror(ledger, broker, orders, clock=clock, may_wait=may_wait, settle=settle)


def _with(report: MirrorRunReport, **changes) -> MirrorRunReport:
    return replace(report, **changes)


def clear_halt(ledger: Ledger, venue: str, *, reason: str, clock: Clock) -> tuple[str, ...]:
    """Lift ``venue``'s halt on the operator's word (§4.5): the ledger accounts cleared.

    Refuses (``MirrorSessionError``) a venue no account has halted, and one whose newest
    reconcile is not clean: a halt lifts only on a reconcile taken after its drift that
    matched the venue (run ``collect_only`` first). ``reason`` is recorded (I11). Clearing
    again on the same clean reconcile writes nothing (I3).
    """
    return rs.clear_halt(ledger, venue, reason=reason, clock=clock)


__all__ = [
    "MirrorRunReport",
    "MirrorSessionError",
    "cancel_ticket",
    "clear_halt",
    "collect_only",
    "mirror_of",
    "morning_orders",
    "pending_orders",
    "run_mirror",
    "working_orders",
]

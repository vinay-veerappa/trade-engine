"""One mirror session, host-callable: the ledger appends around a TosPaperBroker (§4.7).

The native implementation: pure decisions (which orders form the batch, halt/unproven,
cover classification, write-ahead items, clear-halt) run in Rust through the typed
``trade_engine_rs.session_*`` pyfunctions (W6-E2: no JSON door); I/O, orchestration, and host
effects remain here.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import date

from trade_engine.domain.orders import Order, OrderState
from trade_engine.interfaces.broker import VenueAck
from trade_engine.interfaces.clock import Clock
from trade_engine.ledger import Event, EventKind, Ledger
from trade_engine.ledger.events import (
    MirrorAck,
    MirrorFill,
    MirrorQueued,
    MirrorRefused,
    VenueReconcile,
    mirror_account,
)
from trade_engine.ledger.mirror import MirrorState
from trade_engine.ledger.state import LedgerFoldError, halted_venues, mirror_state
from trade_engine.tos_paper import _rs
from trade_engine.tos_paper.broker import MirrorBinding, TosPaperBroker, VenueUnreadable
from trade_engine.tos_paper.transport import TransportUnavailable

# The sim still works these: a mirrored order must be live in the book of record.
_WORKING = frozenset({OrderState.SUBMITTED, OrderState.ACCEPTED, OrderState.PARTIALLY_FILLED})
# A morning entry the sim filled at once is still mirrored; one it refused or dropped is not.
_ENDED_UNFILLED = frozenset({OrderState.CANCELLED, OrderState.REJECTED, OrderState.EXPIRED})
_KIND = {
    MirrorQueued: EventKind.MIRROR_QUEUED,
    MirrorRefused: EventKind.MIRROR_REFUSED,
    MirrorAck: EventKind.MIRROR_ACK,
    MirrorFill: EventKind.MIRROR_FILL,
    VenueReconcile: EventKind.VENUE_RECONCILE,
}


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


def _halted(ledger: Ledger) -> frozenset[str]:
    return halted_venues({account: ledger.state(account) for account in ledger.accounts()})


# -- what to mirror --------------------------------------------------------------------


def _items(venue: str, rows: Sequence[tuple[object, str]]) -> list[tuple[object, str]]:
    """Rust's (payload, command id) rows as the (payload, command suffix) pairs :func:`_append` takes."""
    return [(payload, command.removeprefix(f"mirror:{venue}:")) for payload, command in rows]


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


def _append(ledger: Ledger, venue: str, clock: Clock, items: Sequence[tuple[object, str]]) -> list:
    """Append (payload, command suffix) pairs in one transaction: all or nothing (I2)."""
    if not items:
        return []
    now = clock.now_utc()
    events = [
        Event(
            account=mirror_account(venue),
            kind=_KIND[type(payload)],
            payload=payload,
            ts_utc=now,
            command_id=f"mirror:{venue}:{suffix}",
        )
        for payload, suffix in items
    ]
    return [event.payload for event in ledger.extend(events)]


def _reconcile(ledger: Ledger, broker: TosPaperBroker, clock: Clock, event: VenueReconcile, phase: str) -> VenueReconcile:
    # Keyed by the outcome too: the same reconcile replays (I3), but a drift found at the
    # instant of an earlier clean one is its own event, never swallowed by the replay.
    command = _rs.call(_rs.rs.session_reconcile_command_id, broker.venue, phase, event.as_of, event)
    suffix = command.removeprefix(f"mirror:{broker.venue}:")
    [written] = _append(ledger, broker.venue, clock, [(event, suffix)])
    return written


def _deferred_why(doing: str, exc: BaseException, *, recorded: str = "") -> str:
    """The reason a run was deferred, in the words the operator reads (I11). ``recorded`` says what
    the run had already written, when it is not nothing: the report never claims a clean slate it lacks."""
    return _rs.call(_rs.rs.session_deferred_why, doing, str(exc), recorded)


def _halt_unproven(ledger: Ledger, broker: TosPaperBroker, clock: Clock, phase: str) -> VenueReconcile | None:
    """Halt the venue when an open ticket has no proven Order ID (I5): never silent."""
    halt = _rs.call(
        _rs.rs.session_halt_unproven,
        broker.venue,
        phase,
        clock.now_utc(),
        mirror_of(ledger, broker.venue),
    )
    if halt is None:
        return None
    event, command = halt
    suffix = command.removeprefix(f"mirror:{broker.venue}:")
    [written] = _append(ledger, broker.venue, clock, [(event, suffix)])
    return written


def _settle_pass(ledger: Ledger, venue: str, clock: Clock, settle) -> tuple[tuple, tuple[str, ...]]:
    """This pass's expiry settle: (expired events, unsettled "<expiry>: <why>"); nothing without ``settle``."""
    if settle is None:  # (calendar, settlements): expired contracts leave the book (tos_paper.expiry)
        return (), ()
    from trade_engine.tos_paper.expiry import settle_expired

    outcome = settle_expired(ledger, venue, clock, *settle)
    return outcome.events, outcome.unsettled


def _collect(ledger: Ledger, broker: TosPaperBroker, clock: Clock, settle=None) -> MirrorRunReport:
    venue = broker.venue
    try:
        collection = broker.collect_fills(mirror_of(ledger, venue))
    except TransportUnavailable as exc:  # could not ask: record and halt nothing, ask again next run
        expired, unsettled = _settle_pass(ledger, venue, clock, settle)
        return MirrorRunReport(venue=venue, halted=broker.halted, deferred=_deferred_why("collecting fills", exc),
                               expired=expired, unsettled=unsettled)
    except VenueUnreadable as refused:
        expired, unsettled = _settle_pass(ledger, venue, clock, settle)
        written = _reconcile(ledger, broker, clock, refused.reconcile, "collect")
        return MirrorRunReport(venue=venue, reconcile=written, halted=True, expired=expired, unsettled=unsettled)
    collected: list[object] = [*collection.fills, *collection.closes]
    commands = _rs.call(
        _rs.rs.session_collect_items,
        venue,
        # str(Decimal), not the codec's canon text: the ids are idempotency keys already in ledgers
        [(f.ticket_key, str(f.filled)) for f in collection.fills],
        [(c.ticket_key, None if c.book_status is None else c.book_status.value) for c in collection.closes],
    )
    items = [(obj, command.removeprefix(f"mirror:{venue}:")) for obj, command in zip(collected, commands, strict=True)]
    try:
        written = _append(ledger, venue, clock, items)
    except LedgerFoldError as err:  # the mirror cannot book these fills honestly: halt
        expired, unsettled = _settle_pass(ledger, venue, clock, settle)
        halt = _rs.call(
            _rs.rs.session_fold_halt,
            venue,
            clock.now_utc(),
            str(err),
            mirror_of(ledger, venue),
            [(f.ticket_key, str(f.filled)) for f in collection.fills],
        )
        written_halt = _reconcile(ledger, broker, clock, halt, "collect")
        broker.restore(mirror_of(ledger, venue), halted_venues=_halted(ledger))
        return MirrorRunReport(venue=venue, reconcile=written_halt, halted=True, expired=expired, unsettled=unsettled)
    fills = tuple(p for p in written if isinstance(p, MirrorFill))
    closes = tuple(p for p in written if isinstance(p, MirrorAck))
    _halt_unproven(ledger, broker, clock, "collect")
    # The fills and closes above are in the book now, so an expiry that this pass's fills complete
    # settles here, before the reconcile sees the book (F9).
    expired, unsettled = _settle_pass(ledger, venue, clock, settle)
    broker.restore(mirror_of(ledger, venue), halted_venues=_halted(ledger))
    try:
        now_state = broker.reconcile_now(defer_unavailable=True)
    except TransportUnavailable as exc:  # the fills above are recorded; only the closing reconcile waits
        return MirrorRunReport(
            venue=venue,
            fills=fills,
            closes=closes,
            halted=broker.halted,
            deferred=_deferred_why(
                "reconciling",
                exc,
                recorded="the fills and closes listed are recorded, only the closing reconcile was not made",
            ),
            expired=expired,
            unsettled=unsettled,
        )
    check = _reconcile(ledger, broker, clock, now_state, "collect")
    return MirrorRunReport(venue=venue, fills=fills, closes=closes, reconcile=check, halted=broker.halted,
                           expired=expired, unsettled=unsettled)


def cancel_ticket(ledger: Ledger, broker: TosPaperBroker, ticket_key: str, *, clock: Clock) -> VenueAck:
    """Cancel one resting ticket at the venue and record it in the ledger (I2).

    The broker is restored from the fold first, so the cancel works after a restart and
    takes only the unfilled remainder out of the expectation. An ACCEPTED cancel is
    appended as a ``MirrorAck`` (``book_status`` CANCELLED), which closes the ticket in the
    fold for good; anything else is returned unrecorded (the ticket is still open).
    """
    venue = broker.venue
    broker.restore(mirror_of(ledger, venue), halted_venues=_halted(ledger))
    order_id = broker.proven_order_id(ticket_key)
    ack = broker.cancel(ticket_key)
    res = _rs.call(
        _rs.rs.session_build_cancel_ack,
        venue,
        ticket_key,
        order_id,
        ack.status,
        ack.message or "",
        ack.timestamp,
    )
    if res is not None:
        ack_event, command = res
        _append(ledger, venue, clock, [(ack_event, command.removeprefix(f"mirror:{venue}:"))])
    return ack


def collect_only(ledger: Ledger, broker: TosPaperBroker, *, clock: Clock, settle=None) -> MirrorRunReport:
    """Fills and the reconcile, no sends: the after-close pass (and a safe probe)."""
    return _collect(ledger, broker, clock, settle)


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
    venue = broker.venue
    collected = _collect(ledger, broker, clock, settle)
    if collected.deferred is not None:  # the venue could not be asked: send nothing over an unread venue
        return collected
    mirror = mirror_of(ledger, venue)
    todo, waiting, cover_refused = _rs.call(_rs.rs.session_classify_cover, mirror, list(orders), may_wait)
    orders_by_id = {o.order_id: o for o in orders}
    todo = [orders_by_id.get(o.order_id, o) for o in todo]
    waiting = tuple(waiting)
    accounts = {order.order_id: order.account_id for order in orders}
    if not todo:  # the cover rule held back or refused every order: nothing to send, so nothing to drain
        if not cover_refused:
            return _with(collected, waiting=waiting)
        now = clock.now_utc()
        written = _append(ledger, venue, clock, _items(venue, _rs.call(
            _rs.rs.session_prepare_write_ahead, venue, accounts, cover_refused, [], [], [], now)))
        return _with(collected, refused=tuple(written), waiting=waiting)
    if not broker.halted:
        # Read the venue BEFORE the write-ahead: a venue that cannot be asked defers the whole batch
        # while nothing is recorded, so no order is lost to a read that blinked. The drain sends
        # from this read. (A halted broker refuses each order below, with its reason.)
        try:
            broker.preflight()
        except TransportUnavailable as exc:
            return _with(
                collected,
                deferred=_deferred_why(f"reading before sending {len(todo)} order(s)", exc),
                waiting=waiting,
            )
    # A halted broker refuses the whole batch here, each order with the reason (I11).
    batch = broker.mirror_batch(todo, holdings=mirror.exposure())
    queued_keys = [ticket.venue_order_id for ticket in broker.queued]
    now = clock.now_utc()
    items = _items(venue, _rs.call(
        _rs.rs.session_prepare_write_ahead,
        venue,
        accounts,
        cover_refused,
        [tuple(refusal) for refusal in batch.refused],
        list(batch.venue_orders),
        queued_keys,
        now,
    ))
    try:
        written = _append(ledger, venue, clock, items)  # write-ahead: before any send
    except Exception:
        for ticket in broker.queued:  # never send what the ledger did not record
            broker.cancel(ticket.venue_order_id)
        raise
    report = broker.drain()
    drain_acks_input = [
        (ack.venue_order_id, ack.status, ack.message or "", ack.timestamp)
        for ack in report.acks
    ]
    proven_input = {
        key: (prov[0], prov[1].value) for key, prov in report.proven.items()
    }
    acks_items = _items(venue, _rs.call(_rs.rs.session_build_drain_acks, venue, drain_acks_input, proven_input))
    acked = _append(ledger, venue, clock, acks_items)
    if _halt_unproven(ledger, broker, clock, "drain") is not None:
        broker.restore(mirror_of(ledger, venue), halted_venues=_halted(ledger))
    drained = None
    if report.reconcile is not None:
        drained = _reconcile(ledger, broker, clock, report.reconcile, "drain")
    return _with(
        collected,
        refused=tuple(p for p in written if isinstance(p, MirrorRefused)),
        queued=tuple(p for p in written if isinstance(p, MirrorQueued)),
        acks=tuple(acked),
        drain_reconcile=drained,
        halted=broker.halted,
        waiting=waiting,
    )


def _with(report: MirrorRunReport, **changes) -> MirrorRunReport:
    return replace(report, **changes)


def clear_halt(ledger: Ledger, venue: str, *, reason: str, clock: Clock) -> tuple[str, ...]:
    """Lift ``venue``'s halt on the operator's word (§4.5): the ledger accounts cleared.

    Refuses (``MirrorSessionError``) a venue no account has halted, and one whose newest
    reconcile is not clean: a halt lifts only on a reconcile taken after its drift that
    matched the venue (run ``collect_only`` first). ``reason`` is recorded (I11). Clearing
    again on the same clean reconcile writes nothing (I3).
    """
    if not isinstance(reason, str) or not reason.strip():
        raise MirrorSessionError("a halt is lifted only with the operator's reason (I11)")
    holders = sorted(account for account in ledger.accounts() if venue in ledger.state(account).halted_venues)
    reconciles = [
        (event.seq, event.payload)
        for event in ledger.events_of_kind(EventKind.VENUE_RECONCILE)
        if event.payload.venue == venue
    ]
    now = clock.now_utc()
    cleared, plan = _rs.call(_rs.rs.session_clear_halt, venue, reason, now, holders, reconciles)
    events = [
        Event(
            account=account,
            kind=EventKind.VENUE_HALT_CLEARED,
            payload=payload,
            ts_utc=now,
            command_id=command,
        )
        for account, payload, command in plan
    ]
    ledger.extend(events)
    return tuple(cleared)


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

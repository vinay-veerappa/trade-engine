"""One mirror session, host-callable: the ledger appends around a TosPaperBroker (§4.7).

The runners stay pure: they never call the mirror. The host calls these, with the
ledger, a connected :class:`TosPaperBroker` and an injected clock (I7):

- :func:`pending_orders` — the strategy orders of the mirrored accounts' EOD batch for
  a session (the D+1 entries the runner submitted after the close) that this venue has
  neither queued nor refused yet. :func:`working_orders` is the same without the batch
  window (the intraday service's per-tick view). :func:`morning_orders` is the batch of
  a session's morning pass (``EodRunner.run_morning``): the entries it made on the
  morning's snapshots, which the sim has usually filled already.
- :func:`run_mirror` — collect fills → append ``MirrorFill``s (and Order Book closes) →
  reconcile against the fold → append ``VenueReconcile`` → if not halted,
  ``mirror_batch`` (holdings = ``MirrorState.exposure()``: the mirror book plus every
  open ticket's unfilled remainder, per allocation account, so a new order never opposes
  one still resting) → append ``MirrorRefused`` + ``MirrorQueued`` (written **ahead** of
  any send) → drain → append ``MirrorAck``s and the drain's ``VenueReconcile``. On a
  halted venue every pending order is refused at the venue with the reason instead
  (I11); nothing is queued.
- :func:`collect_only` — fills + reconcile, no sends: the after-close pass.
- :func:`cancel_ticket` — cancel one resting ticket by its proven venue Order ID and
  record it (``MirrorAck`` with ``book_status`` CANCELLED), so the cancel survives a
  restart even after the CANCELED row leaves the Order Book.
- :func:`clear_halt` — the operator lifts a venue's halt, with the reason, once the
  venue's newest reconcile is clean (``VenueHaltCleared``). A halt never lifts itself.

**Entries only.** The batches here are the sim's entries; its exits and profit targets
(orders with a parent order) are never sent as themselves. At an in-session pass,
``tos_paper.exits`` sends the venue what closes the gap between the two books instead.

Every append goes through ``Ledger.append``/``extend`` under a derived command id, so a
re-run is idempotent (I3): a second run the same day queues nothing new, and a fill
already recorded is not recorded again. The fold refuses a contradiction before it is
written (I2): a venue whose fills the mirror cannot book halts instead.

Timing (the host's schedule): the host driver proves its price lock on live quotes, which
quiet after-close quotes cannot give, so **sends happen in market hours** — the host runs
``run_mirror`` in the morning for the prior session's EOD batch (its DAY entries work
that day) and ``collect_only`` after the close, the same day, so a DAY ticket that
expired unfilled is recorded (``MirrorAck`` EXPIRED) before its row leaves the Order
Book. A ticket whose row vanished unrecorded stays expected and the reconcile halts
the venue — refused, not guessed (I5).

Every open ticket must carry a proven venue Order ID, or its fills can never be read
back and its resting remainder would mask whatever the venue did with it. A send that
proved no Order ID (a SENT result naming no row, a PENDING read-back) and a ticket
queued but never acked (a crash between the write-ahead and the send, or between the
drain and the ack append) therefore **halt the venue** with a ``VenueReconcile`` naming
the ticket's contracts. Such a ticket is never re-sent (I3); an operator resolves it.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import date

from trade_engine.domain.orders import Order, OrderState
from trade_engine.eod.runner import MORNING_SUFFIX, pass_of
from trade_engine.interfaces.clock import Clock
from trade_engine.ledger import Event, EventKind, Ledger
from trade_engine.ledger.codec import encode_payload
from trade_engine.ledger.events import (
    MirrorAck,
    MirrorAllocation,
    MirrorFill,
    MirrorQueued,
    MirrorRefused,
    VenueHaltCleared,
    VenueReconcile,
    mirror_account,
)
from trade_engine.interfaces.broker import VenueAck
from trade_engine.ledger.mirror import MirrorState, MirrorTicketState, ticket_contracts
from trade_engine.ledger.state import LedgerFoldError, halted_venues, mirror_state
from trade_engine.tos_paper.broker import MirrorBinding, TosPaperBroker, VenueUnreadable

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


def mirror_of(ledger: Ledger, venue: str) -> MirrorState:
    """The venue's folded mirror, from the ledger's committed state."""
    return mirror_state({mirror_account(venue): ledger.state(mirror_account(venue))}, venue)


def _halted(ledger: Ledger) -> frozenset[str]:
    return halted_venues({account: ledger.state(account) for account in ledger.accounts()})


# -- what to mirror --------------------------------------------------------------------


def working_orders(ledger: Ledger, binding: MirrorBinding) -> tuple[Order, ...]:
    """Every entry the mirrored accounts' sim still works that this venue has not handled.

    Entries only (no parent order): exits reach the venue through ``tos_paper.exits``. In account
    order, then order-id order, so the first-in rule is deterministic.
    """
    mirror = mirror_of(ledger, binding.venue_account)
    found: list[Order] = []
    for account in binding.mirrored_accounts:
        state = ledger.state(account)
        for order in sorted(state.orders.values(), key=lambda o: o.order_id):
            if order.parent_order_id is None and order.state in _WORKING and not mirror.handled(order.order_id):
                found.append(order)
    return tuple(found)


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
    close = calendar.session_close(session)
    return _batch(
        ledger, binding, session, job, morning=False,
        wanted=lambda created, current: created.created_at >= close and current.state in _WORKING,
    )


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
    session_open = calendar.session_open(session)
    return _batch(
        ledger, binding, session, None if job is None else job + MORNING_SUFFIX, morning=True,
        wanted=lambda created, current: created.created_at >= session_open
        and current.state not in _ENDED_UNFILLED,
    )


def _batch(ledger: Ledger, binding: MirrorBinding, session: date, job: str | None, *, morning: bool, wanted) -> tuple[Order, ...]:
    """The mirrored accounts' entries created up to their marker for ``session`` that
    ``wanted(created, current)`` keeps and this venue has not handled."""
    what = "morning pass" if morning else "EOD run"
    mirror = mirror_of(ledger, binding.venue_account)
    found: list[Order] = []
    for account in binding.mirrored_accounts:
        markers = [
            event
            for event in ledger.events_of_kind(EventKind.EOD_RUN, account=account)
            if event.payload.session == session
            and pass_of(event.payload.job) == ("morning" if morning else None)
            and (job is None or event.payload.job == job)
        ]
        if not markers:
            raise MirrorSessionError(
                f"'{account}' has no {what} for {session.isoformat()}"
                f"{'' if job is None else f' (job {job})'}; refusing to mirror a batch that "
                "may be incomplete (I5)"
            )
        jobs = sorted({event.payload.job for event in markers})
        if len(jobs) > 1:
            raise MirrorSessionError(
                f"'{account}' has {what}s of {jobs} for {session.isoformat()}; name the job (I5)"
            )
        marker = markers[0]
        state = ledger.state(account)
        for event in ledger.events_of_kind(EventKind.ORDERS_CREATED, account=account):
            if event.seq > marker.seq:
                continue
            for created in event.payload.orders:
                current = state.orders.get(created.order_id)
                if (
                    created.parent_order_id is None
                    and current is not None
                    and wanted(created, current)
                    and not mirror.handled(created.order_id)
                ):
                    found.append(current)
    return tuple(found)


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
    digest = hashlib.sha256(
        json.dumps(encode_payload(event), sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:16]
    [written] = _append(
        ledger, broker.venue, clock, [(event, f"reconcile:{phase}:{event.as_of.isoformat()}:{digest}")]
    )
    return written


def _collect(ledger: Ledger, broker: TosPaperBroker, clock: Clock) -> MirrorRunReport:
    venue = broker.venue
    try:
        collection = broker.collect_fills(mirror_of(ledger, venue))
    except VenueUnreadable as refused:
        written = _reconcile(ledger, broker, clock, refused.reconcile, "collect")
        return MirrorRunReport(venue=venue, reconcile=written, halted=True)
    items: list[tuple[object, str]] = [
        (fill, f"fill:{fill.ticket_key}:{fill.filled}") for fill in collection.fills
    ] + [
        (close, f"book:{close.ticket_key}:{close.book_status.value}") for close in collection.closes
    ]
    try:
        written = _append(ledger, venue, clock, items)
    except LedgerFoldError as err:  # the mirror cannot book these fills honestly: halt
        tickets = mirror_of(ledger, venue).tickets
        names = sorted(
            {
                contract.symbol
                for fill in collection.fills
                for contract in ticket_contracts(tickets[fill.ticket_key].queued, fill.filled)
            }
        ) or [venue]
        halt = VenueReconcile(
            venue=venue,
            as_of=clock.now_utc(),
            reconciled=False,
            drift=tuple(names),
            note=f"the mirror cannot book the venue's fills ({err}); venue halted",
        )
        written_halt = _reconcile(ledger, broker, clock, halt, "collect")
        broker.restore(mirror_of(ledger, venue), halted_venues=_halted(ledger))
        return MirrorRunReport(venue=venue, reconcile=written_halt, halted=True)
    fills = tuple(p for p in written if isinstance(p, MirrorFill))
    closes = tuple(p for p in written if isinstance(p, MirrorAck))
    _halt_unproven(ledger, broker, clock, "collect")
    broker.restore(mirror_of(ledger, venue), halted_venues=_halted(ledger))
    check = _reconcile(ledger, broker, clock, broker.reconcile_now(), "collect")
    return MirrorRunReport(venue=venue, fills=fills, closes=closes, reconcile=check, halted=broker.halted)


def _unproven(mirror: MirrorState) -> tuple[MirrorTicketState, ...]:
    """Open tickets with no proven venue Order ID: their fills cannot be read back."""
    return tuple(ticket for ticket in mirror.open_tickets if ticket.venue_order_id is None)


def _halt_unproven(ledger: Ledger, broker: TosPaperBroker, clock: Clock, phase: str) -> VenueReconcile | None:
    """Halt the venue when an open ticket has no proven Order ID (I5): never silent."""
    unproven = _unproven(mirror_of(ledger, broker.venue))
    if not unproven:
        return None
    names = sorted({c.symbol for t in unproven for c in ticket_contracts(t.queued, t.queued.quantity)})
    halt = VenueReconcile(
        venue=broker.venue,
        as_of=clock.now_utc(),
        reconciled=False,
        drift=tuple(names),
        note=(
            "no proven venue Order ID for open ticket(s) "
            + ", ".join(t.key for t in unproven)
            + ": their fills cannot be read back; venue halted (I5)"
        ),
    )
    return _reconcile(ledger, broker, clock, halt, f"unproven:{phase}")


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
    if ack.status == "ACCEPTED" and order_id is not None:
        _append(
            ledger,
            venue,
            clock,
            [
                (
                    MirrorAck(
                        venue=venue,
                        ticket_key=ticket_key,
                        status="ACCEPTED",
                        message=ack.message or "cancelled",
                        at=ack.timestamp,
                        venue_order_id=order_id,
                        book_status=OrderState.CANCELLED,
                    ),
                    f"cancel:{ticket_key}",
                )
            ],
        )
    return ack


def collect_only(ledger: Ledger, broker: TosPaperBroker, *, clock: Clock) -> MirrorRunReport:
    """Fills and the reconcile, no sends: the after-close pass (and a safe probe)."""
    return _collect(ledger, broker, clock)


def run_mirror(
    ledger: Ledger,
    broker: TosPaperBroker,
    orders: Sequence[Order],
    *,
    clock: Clock,
) -> MirrorRunReport:
    """One mirror session for ``orders`` (usually :func:`pending_orders`). Idempotent (I3).

    Orders this venue already queued or refused are skipped; a repeated order id keeps
    its first occurrence. The broker must be connected.
    """
    venue = broker.venue
    collected = _collect(ledger, broker, clock)
    mirror = mirror_of(ledger, venue)
    seen: set[str] = set()
    todo: list[Order] = []
    for order in orders:
        if order.order_id in seen or mirror.handled(order.order_id):
            continue
        seen.add(order.order_id)
        todo.append(order)
    if not todo:
        return collected
    now = clock.now_utc()
    # A halted broker refuses the whole batch here, each order with the reason (I11).
    batch = broker.mirror_batch(todo, holdings=mirror.exposure())
    accounts = {order.order_id: order.account_id for order in todo}
    queued_keys = {ticket.venue_order_id for ticket in broker.queued}
    items: list[tuple[object, str]] = [
        (
            MirrorRefused(
                venue=venue,
                strategy_order_id=order_id,
                strategy_account=accounts[order_id],
                reason=reason,
                at=now,
            ),
            f"refused:{order_id}",
        )
        for order_id, reason in batch.refused
    ]
    for ticket in batch.venue_orders:
        if ticket.venue_order_id not in queued_keys:
            # The broker already used this key in this process but the ledger never
            # recorded it (a failed write-ahead): it will not queue it again (I3), so
            # the orders are refused with the reason rather than left unrecorded (I11).
            for allocation in ticket.allocations:
                items.append(
                    (
                        MirrorRefused(
                            venue=venue,
                            strategy_order_id=allocation.strategy_order_id,
                            strategy_account=allocation.account_id,
                            reason=(
                                f"ticket {ticket.venue_order_id} was already used by this broker "
                                "process and never recorded; refused (I3) - restart the broker from the ledger"
                            ),
                            at=now,
                        ),
                        f"refused:{allocation.strategy_order_id}",
                    )
                )
            continue
        items.append((_queued(venue, ticket), f"queued:{ticket.venue_order_id}"))
    try:
        written = _append(ledger, venue, clock, items)  # write-ahead: before any send
    except Exception:
        for ticket in broker.queued:  # never send what the ledger did not record
            broker.cancel(ticket.venue_order_id)
        raise
    report = broker.drain()
    acks: list[tuple[object, str]] = []
    for ack in report.acks:
        proven = report.proven.get(ack.venue_order_id)
        acks.append(
            (
                MirrorAck(
                    venue=venue,
                    ticket_key=ack.venue_order_id,
                    status=ack.status,
                    message=ack.message or ack.status,
                    at=ack.timestamp,
                    venue_order_id=None if proven is None else proven[0],
                    book_status=None if proven is None else proven[1],
                ),
                f"ack:{ack.venue_order_id}",
            )
        )
    acked = _append(ledger, venue, clock, acks)
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
    )


def _queued(venue: str, ticket) -> MirrorQueued:
    return MirrorQueued(
        venue=venue,
        ticket_key=ticket.venue_order_id,
        instrument=ticket.instrument,
        side=ticket.side,
        quantity=ticket.quantity,
        order_type=ticket.order_type,
        limit_price=ticket.limit_price,
        tif=ticket.tif,
        allocations=tuple(
            MirrorAllocation(a.strategy_order_id, a.account_id, a.quantity) for a in ticket.allocations
        ),
        at=ticket.submitted_at,
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
    if not holders:
        raise MirrorSessionError(f"venue {venue} is not halted")
    reconciles = [
        event for event in ledger.events_of_kind(EventKind.VENUE_RECONCILE) if event.payload.venue == venue
    ]
    newest = max(reconciles, key=lambda event: event.seq)  # a halt comes from a reconcile
    if not newest.payload.reconciled:
        raise MirrorSessionError(
            f"venue {venue}'s newest reconcile ({newest.payload.as_of.isoformat()}) still drifts: "
            f"{list(newest.payload.drift)} ({newest.payload.note}); fix it and collect again first"
        )
    now = clock.now_utc()
    ledger.extend(
        [
            Event(
                account=account,
                kind=EventKind.VENUE_HALT_CLEARED,
                payload=VenueHaltCleared(venue=venue, at=now, reason=reason.strip(), reconcile_seq=newest.seq),
                ts_utc=now,
                command_id=f"halt-cleared:{venue}:{account}:{newest.seq}",
            )
            for account in holders
        ]
    )
    return tuple(holders)


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

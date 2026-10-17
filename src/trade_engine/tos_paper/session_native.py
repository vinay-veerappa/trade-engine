"""One mirror session, host-callable: the ledger appends around a TosPaperBroker (§4.7).

The native implementation: pure decisions (which orders form the batch, halt/unproven,
cover classification, write-ahead items, clear-halt) run in Rust through the
``tos_paper_session`` door; I/O, orchestration, and host effects remain here.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import date

from trade_engine.domain.orders import Order, OrderState
from trade_engine.interfaces.broker import VenueAck
from trade_engine.interfaces.clock import Clock
from trade_engine.ledger import Event, EventKind, Ledger, codec
from trade_engine.ledger.events import (
    MirrorAck,
    MirrorFill,
    MirrorQueued,
    MirrorRefused,
    VenueReconcile,
    mirror_account,
)
from trade_engine.ledger.mirror import MirrorState, ticket_contracts
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
    account_orders = {}
    for account in binding.mirrored_accounts:
        state = ledger.state(account)
        account_orders[account] = [codec.encode_payload(o) for o in state.orders.values()]
    doc = {
        "mirrored_accounts": list(binding.mirrored_accounts),
        "account_orders": account_orders,
        "mirror": codec.canon(mirror),
    }
    res = _rs.session_decide("session_working_orders", doc)
    found: list[Order] = []
    for item in res["orders"]:
        decoded = codec.decode_payload(item)
        state = ledger.state(decoded.account_id)
        found.append(state.orders.get(decoded.order_id, decoded))
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
    mirror = mirror_of(ledger, binding.venue_account)
    accounts_data = {}
    for account in binding.mirrored_accounts:
        markers = [
            {"seq": e.seq, "session": e.payload.session.isoformat(), "job": e.payload.job}
            for e in ledger.events_of_kind(EventKind.EOD_RUN, account=account)
        ]
        created_events = [
            {"seq": e.seq, "orders": [codec.encode_payload(o) for o in e.payload.orders]}
            for e in ledger.events_of_kind(EventKind.ORDERS_CREATED, account=account)
        ]
        state = ledger.state(account)
        current_orders = [codec.encode_payload(o) for o in state.orders.values()]
        accounts_data[account] = {
            "markers": markers,
            "created_events": created_events,
            "current_orders": current_orders,
        }
    doc = {
        "mirrored_accounts": list(binding.mirrored_accounts),
        "accounts_data": accounts_data,
        "session": session.isoformat(),
        "close": close.isoformat(),
        "job": job,
        "mirror": codec.canon(mirror),
    }
    res = _rs.session_decide("session_pending_orders", doc)
    found: list[Order] = []
    for item in res["orders"]:
        decoded = codec.decode_payload(item)
        state = ledger.state(decoded.account_id)
        found.append(state.orders.get(decoded.order_id, decoded))
    return tuple(found)


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
    mirror = mirror_of(ledger, binding.venue_account)
    accounts_data = {}
    for account in binding.mirrored_accounts:
        markers = [
            {"seq": e.seq, "session": e.payload.session.isoformat(), "job": e.payload.job}
            for e in ledger.events_of_kind(EventKind.EOD_RUN, account=account)
        ]
        created_events = [
            {"seq": e.seq, "orders": [codec.encode_payload(o) for o in e.payload.orders]}
            for e in ledger.events_of_kind(EventKind.ORDERS_CREATED, account=account)
        ]
        state = ledger.state(account)
        current_orders = [codec.encode_payload(o) for o in state.orders.values()]
        accounts_data[account] = {
            "markers": markers,
            "created_events": created_events,
            "current_orders": current_orders,
        }
    doc = {
        "mirrored_accounts": list(binding.mirrored_accounts),
        "accounts_data": accounts_data,
        "session": session.isoformat(),
        "session_open": session_open.isoformat(),
        "job": job,
        "mirror": codec.canon(mirror),
    }
    res = _rs.session_decide("session_morning_orders", doc)
    found: list[Order] = []
    for item in res["orders"]:
        decoded = codec.decode_payload(item)
        state = ledger.state(decoded.account_id)
        found.append(state.orders.get(decoded.order_id, decoded))
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
    doc = {
        "venue": broker.venue,
        "phase": phase,
        "as_of": event.as_of.isoformat(),
        "event": codec.encode_payload(event),
    }
    cmd = _rs.session_decide("session_reconcile_command_id", doc)["command_id"]
    suffix = cmd.removeprefix(f"mirror:{broker.venue}:")
    [written] = _append(ledger, broker.venue, clock, [(event, suffix)])
    return written


def _deferred_why(doing: str, exc: BaseException, *, recorded: str = "") -> str:
    """The reason a run was deferred, in the words the operator reads (I11). ``recorded`` says what
    the run had already written, when it is not nothing: the report never claims a clean slate it lacks."""
    doc = {"doing": doing, "exc": str(exc), "recorded": recorded}
    return _rs.session_decide("session_deferred_why", doc)["why"]


def _halt_unproven(ledger: Ledger, broker: TosPaperBroker, clock: Clock, phase: str) -> VenueReconcile | None:
    """Halt the venue when an open ticket has no proven Order ID (I5): never silent."""
    doc = {
        "venue": broker.venue,
        "phase": phase,
        "as_of": clock.now_utc().isoformat(),
        "mirror": codec.canon(mirror_of(ledger, broker.venue)),
    }
    res = _rs.session_decide("session_halt_unproven", doc)
    if not res.get("halt") and "event" not in res:
        return None
    event = codec.decode_payload(res["event"])
    cmd = res["command_id"]
    suffix = cmd.removeprefix(f"mirror:{broker.venue}:")
    [written] = _append(ledger, broker.venue, clock, [(event, suffix)])
    return written


def _collect(ledger: Ledger, broker: TosPaperBroker, clock: Clock) -> MirrorRunReport:
    venue = broker.venue
    try:
        collection = broker.collect_fills(mirror_of(ledger, venue))
    except TransportUnavailable as exc:  # could not ask: record and halt nothing, ask again next run
        return MirrorRunReport(venue=venue, halted=broker.halted, deferred=_deferred_why("collecting fills", exc))
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
        )
    check = _reconcile(ledger, broker, clock, now_state, "collect")
    return MirrorRunReport(venue=venue, fills=fills, closes=closes, reconcile=check, halted=broker.halted)


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
    doc = {
        "venue": venue,
        "ticket_key": ticket_key,
        "order_id": order_id,
        "ack_status": ack.status,
        "ack_message": ack.message or "",
        "ack_timestamp": ack.timestamp.isoformat(),
    }
    res = _rs.session_decide("session_build_cancel_ack", doc)
    if res.get("ack"):
        ack_ev = codec.decode_payload(res["ack"]["payload"])
        suffix = res["ack"]["command_id"].removeprefix(f"mirror:{venue}:")
        _append(ledger, venue, clock, [(ack_ev, suffix)])
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
    may_wait: bool = False,
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
    collected = _collect(ledger, broker, clock)
    if collected.deferred is not None:  # the venue could not be asked: send nothing over an unread venue
        return collected
    mirror = mirror_of(ledger, venue)
    doc = {
        "mirror": codec.canon(mirror),
        "orders": [codec.encode_payload(o) for o in orders],
        "may_wait": may_wait,
    }
    classified = _rs.session_decide("session_classify_cover", doc)
    todo_decoded = [codec.decode_payload(o) for o in classified["todo"]]
    orders_by_id = {o.order_id: o for o in orders}
    todo = [orders_by_id.get(o.order_id, o) for o in todo_decoded]
    waiting = tuple((oid, reason) for oid, reason in classified["waiting"])
    cover_refused = classified["refused"]
    if not todo:  # the cover rule held back or refused every order: nothing to send, so nothing to drain
        if not cover_refused:
            return _with(collected, waiting=waiting)
        now = clock.now_utc()
        accounts = {order.order_id: order.account_id for order in orders}
        doc_wa = {
            "venue": venue,
            "accounts": accounts,
            "cover_refused": cover_refused,
            "batch_refused": [],
            "batch_venue_orders": [],
            "broker_queued_keys": [],
            "now": now.isoformat(),
        }
        wa_res = _rs.session_decide("session_prepare_write_ahead", doc_wa)
        items = [
            (codec.decode_payload(it["payload"]), it["command_id"].removeprefix(f"mirror:{venue}:"))
            for it in wa_res["items"]
        ]
        written = _append(ledger, venue, clock, items)
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
    accounts = {order.order_id: order.account_id for order in orders}
    now = clock.now_utc()
    doc_wa = {
        "venue": venue,
        "accounts": accounts,
        "cover_refused": cover_refused,
        "batch_refused": list(batch.refused),
        "batch_venue_orders": [_rs.venue_order_doc(ticket) for ticket in batch.venue_orders],
        "broker_queued_keys": queued_keys,
        "now": now.isoformat(),
    }
    wa_res = _rs.session_decide("session_prepare_write_ahead", doc_wa)
    items = [
        (codec.decode_payload(it["payload"]), it["command_id"].removeprefix(f"mirror:{venue}:"))
        for it in wa_res["items"]
    ]
    try:
        written = _append(ledger, venue, clock, items)  # write-ahead: before any send
    except Exception:
        for ticket in broker.queued:  # never send what the ledger did not record
            broker.cancel(ticket.venue_order_id)
        raise
    report = broker.drain()
    drain_acks_input = [
        [ack.venue_order_id, ack.status, ack.message or "", ack.timestamp.isoformat()]
        for ack in report.acks
    ]
    proven_input = {
        key: [prov[0], prov[1].value] for key, prov in report.proven.items()
    }
    doc_drain = {
        "venue": venue,
        "acks": drain_acks_input,
        "proven": proven_input,
    }
    drain_res = _rs.session_decide("session_build_drain_acks", doc_drain)
    acks_items = [
        (codec.decode_payload(a["payload"]), a["command_id"].removeprefix(f"mirror:{venue}:"))
        for a in drain_res["acks"]
    ]
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
    holders = sorted(account for account in ledger.accounts() if venue in ledger.state(account).halted_venues)
    reconciles = [
        [event.seq, codec.encode_payload(event.payload)]
        for event in ledger.events_of_kind(EventKind.VENUE_RECONCILE)
        if event.payload.venue == venue
    ]
    now = clock.now_utc()
    doc = {
        "venue": venue,
        "reason": reason,
        "as_of": now.isoformat(),
        "holders": holders,
        "reconciles": reconciles,
    }
    plan = _rs.session_decide("session_clear_halt", doc)
    events = [
        Event(
            account=e["account"],
            kind=EventKind.VENUE_HALT_CLEARED,
            payload=codec.decode_payload(e["event"]),
            ts_utc=now,
            command_id=e["command_id"],
        )
        for e in plan["events"]
    ]
    ledger.extend(events)
    return tuple(plan["holders"])


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

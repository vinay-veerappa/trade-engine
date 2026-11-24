"""Ingest everything a venue changed at one bar or tick, immediately (E4's contract).

Shared by the EOD runner and the intraday service: reconciliation is per bar, never
per session — bars simulated before an exit arrived are gone, and SimBroker refuses a
late protective stop. Nothing here swallows that error; a raise fails the run loudly.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from trade_engine.domain.orders import Order, OrderState
from trade_engine.domain.portfolio import Fill
from trade_engine.interfaces.broker import BrokerAdapter, VenueFill
from trade_engine.interfaces.clock import Clock
from trade_engine.ledger import Event, EventKind, Ledger, codec
from trade_engine.ledger.state import AccountState
from trade_engine.oms.manager import OrderManager
from trade_engine.sim import _rs

MIN_TIME = datetime.min.replace(tzinfo=timezone.utc)
TERMINAL = frozenset(
    {
        OrderState.FILLED,
        OrderState.CANCELLED,
        OrderState.EXPIRED,
        OrderState.REJECTED,
    }
)


class ReconcileError(RuntimeError):
    """What the venue reports cannot be squared with the ledger (I5).

    Each runner re-raises it as its own refusal (``EodRunnerError``,
    ``IntradayServiceError``), so a host catches one type per runner.
    """


_rs.register("reconcile", ReconcileError)


def reconcile_after(
    ledger: Ledger,
    clock: Clock,
    broker: BrokerAdapter,
    manager: OrderManager,
    account_id: str,
    since: datetime,
    *,
    journal_account: str | None = None,
) -> int:
    """Record every venue fill at or after ``since``; re-read the states it changed.

    Returns how many fills were newly recorded. A fill whose order the ledger does not
    know refuses (I5); a replayed venue fill id is a no-op (I3).
    """
    recorded = 0
    for venue_fill in broker.fills(since):
        recorded += _record_venue_fill(ledger, clock, broker, manager, account_id, venue_fill, journal_account)
    state: Any = ledger.state(account_id)
    # Which of the venue's orders the ledger knows and has not settled is Rust's
    # (``te_core::oms::reconcile``, P3b-1); NEW children were never sent and terminal
    # states were already folded.
    venue_order_ids = [order_state.venue_order_id for order_state in broker.orders(since)]
    for order_id in _rs.call(_rs.rs.oms_orders_to_read, _state_text(state), venue_order_ids):
        manager.reconcile_order(order_id)
    return recorded


def _state_text(state: AccountState) -> str:
    return codec.text(codec.canon(state))


def _record_venue_fill(
    ledger: Ledger,
    clock: Clock,
    broker: BrokerAdapter,
    manager: OrderManager,
    account_id: str,
    venue_fill: VenueFill,
    journal_account: str | None,
) -> int:
    if ledger.has_command(f"fill:{venue_fill.venue_fill_id}"):
        # Recorded before a crash: the sync or the journal row may never have happened (F2).
        _heal_recorded_fill(ledger, clock, manager, account_id, _fill_of(broker, account_id, venue_fill), journal_account)
        return 0
    state: AccountState = ledger.state(account_id)
    _rs.call(
        _rs.rs.oms_check_known_order,
        _state_text(state),
        venue_fill.venue_fill_id,
        venue_fill.venue_order_id,
        account_id,
    )
    fill = _fill_of(broker, account_id, venue_fill)
    manager.record_fill(fill)
    if journal_account is not None:
        enqueue_journal_fill(ledger, clock, journal_account, ledger.state(account_id), fill)
    return 1


def _fill_of(broker: BrokerAdapter, account_id: str, venue_fill: VenueFill) -> Fill:
    return Fill(
        fill_id=venue_fill.venue_fill_id,
        order_id=venue_fill.venue_order_id,
        account_id=account_id,
        instrument=venue_fill.instrument,
        quantity=venue_fill.quantity,
        price=venue_fill.price,
        venue_env=broker.env,
        filled_at=venue_fill.filled_at,
        side=venue_fill.side,
        fee=venue_fill.fee,
        venue_order_id=venue_fill.venue_order_id,
        venue_execution_id=venue_fill.venue_fill_id,
        leg_id=venue_fill.leg_id,
    )


def _heal_recorded_fill(
    ledger: Ledger,
    clock: Clock,
    manager: OrderManager,
    account_id: str,
    fill: Fill,
    journal_account: str | None,
) -> None:
    """Finish what a crash after a committed fill may have left undone (F2).

    The bracket sync is re-run only for the bracket's latest fill: an older cause can
    collide with a command id already used for a different quantity. The journal row is
    re-enqueued only for the account's latest fill: reconcile records and journals one
    fill at a time, so a crash can leave only the last one unjournaled.
    """
    fill_event = ledger.event_by_command(f"fill:{fill.fill_id}")
    state = ledger.state(account_id)
    # Which bracket to re-sync, and whether this fill is the account's latest, is Rust's
    # (``te_core::oms::reconcile::heal_plan``, W5 P2).
    resync, rejournal_candidate = _rs.call(
        _rs.rs.oms_heal_plan,
        _state_text(state),
        _fill_refs(ledger, account_id),
        fill.fill_id,
        fill.order_id,
        fill_event.seq if fill_event is not None else None,
    )
    if resync is not None:
        entry_id, cause = resync
        manager.synchronize_bracket(state.orders[entry_id], cause)
    if journal_account is None or fill_event is None or not rejournal_candidate:
        return
    if ledger.outbox_row(fill_event.seq, f"journal:{journal_account}") is None:
        enqueue_journal_fill(ledger, clock, journal_account, ledger.state(account_id), fill)


def bracket_resync_cause(ledger: Ledger, account_id: str, order_id: str) -> tuple[Order, str] | None:
    """The bracket entry and the fill id of its latest fill, or None with no fills.

    ``order_id`` is the entry or one of its children. The latest fill is the last FILL
    event (by ledger seq) of the entry or of any order whose parent is the entry.
    """
    state = ledger.state(account_id)
    # The entry lookup and the cause are Rust's (``te_core::oms::reconcile``, W5 P2). An
    # unknown order raises KeyError from there, as the Python lookup did.
    found = _rs.call(
        _rs.rs.oms_bracket_resync_cause,
        _state_text(state),
        _fill_refs(ledger, account_id),
        order_id,
    )
    if found is None:
        return None
    entry_id, fill_id = found
    return state.orders[entry_id], fill_id


def resync_open_brackets(ledger: Ledger, manager: OrderManager, account_id: str) -> int:
    """Re-sync every open bracket with a filled entry, before a run's first reconcile (F2).

    A crash can leave a fill committed with its protective stop never sent, and a rerun
    may never report that fill again. Returns how many entries were synced.
    """
    state = ledger.state(account_id)
    # Which entries to re-sync, and from which fill, is Rust's (``te_core::oms::reconcile``,
    # W5 P2), in the state's order: the manager syncs them in that order.
    pairs = _rs.call(
        _rs.rs.oms_brackets_to_resync,
        _state_text(state),
        _fill_refs(ledger, account_id),
    )
    for entry_id, fill_id in pairs:
        manager.synchronize_bracket(state.orders[entry_id], fill_id)
    return len(pairs)


def _fill_refs(ledger: Ledger, account_id: str) -> list[tuple[int, str, str]]:
    """The account's FILL events as ``(seq, order_id, fill_id)``, in ledger-seq order."""
    return [(e.seq, e.payload.order_id, e.payload.fill_id) for e in ledger.events_of_kind(EventKind.FILL, account=account_id)]


def enqueue_journal_fill(
    ledger: Ledger,
    clock: Clock,
    journal_account: str,
    state: AccountState,
    fill: Fill,
) -> Event | None:
    """One journal execution per fill, with the asset class and the bracket's legs.

    Shared by both runners: the journal posts every execution with ``assetClass``,
    multiplier, stop and target, and the journal account id comes from config (I12).
    """
    event = ledger.event_by_command(f"fill:{fill.fill_id}")
    # The bracket's stop and target, the asset class and the multiplier are Rust's
    # (``te_core::oms::reconcile``, P3b-1); it refuses an unrecorded fill where Python did.
    payload = json.loads(
        _rs.call(
            _rs.rs.oms_journal_payload,
            _state_text(state),
            codec.text(codec.encode_payload(fill)),
            journal_account,
            event is not None and event.seq is not None,
        )
    )
    return ledger.enqueue_outbox(
        event.seq,
        f"journal:{journal_account}",
        payload,
        created_at=clock.now_utc(),
    )

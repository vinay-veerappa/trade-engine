"""Ingest everything a venue changed at one bar or tick, immediately (E4's contract).

Shared by the EOD runner and the intraday service: reconciliation is per bar, never
per session — bars simulated before an exit arrived are gone, and SimBroker refuses a
late protective stop. Nothing here swallows that error; a raise fails the run loudly.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from trade_engine.domain.orders import OrderState, OrderType
from trade_engine.domain.instruments import OptionContract
from trade_engine.domain.portfolio import Fill
from trade_engine.interfaces.broker import BrokerAdapter, VenueFill
from trade_engine.interfaces.clock import Clock
from trade_engine.ledger import Event, EventKind, Ledger
from trade_engine.ledger.state import AccountState
from trade_engine.oms.manager import OrderManager

MIN_TIME = datetime.min.replace(tzinfo=__import__("datetime").timezone.utc)
TERMINAL = frozenset(
    {
        OrderState.FILLED,
        OrderState.CANCELLED,
        OrderState.EXPIRED,
        OrderState.REJECTED,
    }
)


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
    for order_state in broker.orders(since):
        order_id = order_state.venue_order_id
        if order_id not in state.orders:
            continue
        ledger_state = state.orders[order_id].state
        if ledger_state is OrderState.NEW or ledger_state in TERMINAL:
            # NEW children were never sent; terminal states were already folded.
            continue
        manager.reconcile_order(order_id)
    return recorded


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
        return 0
    state: AccountState = ledger.state(account_id)
    if venue_fill.venue_order_id not in state.orders:
        raise ValueError(
            f"Venue fill '{venue_fill.venue_fill_id}' references unknown order "
            f"'{venue_fill.venue_order_id}' for '{account_id}' (I5)"
        )
    fill = Fill(
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
    manager.record_fill(fill)
    if journal_account is not None:
        enqueue_journal_fill(ledger, clock, journal_account, ledger.state(account_id), fill)
    return 1


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
    order = state.orders[fill.order_id]
    entry = state.orders[order.parent_order_id] if order.parent_order_id else order
    children = [
        candidate
        for candidate in state.orders.values()
        if candidate.parent_order_id == entry.order_id
    ]
    stop = next((child for child in children if child.order_type is OrderType.STOP), None)
    target = next((child for child in children if child.order_type is OrderType.LIMIT), None)
    event = ledger.event_by_command(f"fill:{fill.fill_id}")
    if event is None or event.seq is None:
        raise ValueError(f"Fill '{fill.fill_id}' was recorded but its ledger event is missing (I1)")
    return ledger.enqueue_outbox(
        event.seq,
        f"journal:{journal_account}",
        {
            "symbol": fill.instrument.symbol,
            "side": fill.side.value,
            "quantity": str(fill.quantity),
            "price": str(fill.price),
            "fee": str(fill.fee),
            "executed_at": fill.filled_at.isoformat(),
            "account_id": journal_account,
            "asset_class": "option" if isinstance(fill.instrument, OptionContract) else "equity",
            "multiplier": fill.instrument.multiplier,
            "stop_loss": str(stop.stop_price) if stop is not None else None,
            # A combo's target is a net price, not this leg's.
            "profit_target": (
                str(target.limit_price)
                if target is not None and target.instrument == fill.instrument
                else None
            ),
            "strategy_tag": entry.command_id.split(":")[0],
            "notes": f"trade-engine {fill.order_id}",
        },
        created_at=clock.now_utc(),
    )
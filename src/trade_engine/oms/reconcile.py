"""Ingest everything a venue changed at one bar or tick, immediately (E4's contract).

Shared by the EOD runner and the intraday service: reconciliation is per bar, never
per session — bars simulated before an exit arrived are gone, and SimBroker refuses a
late protective stop. Nothing here swallows that error; a raise fails the run loudly.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from trade_engine.domain.orders import Order, OrderState
from trade_engine.domain.portfolio import Fill
from trade_engine.interfaces.broker import BrokerAdapter, VenueFill
from trade_engine.interfaces.clock import Clock
from trade_engine.ledger import Event, Ledger, codec
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
    return _rs.call(
        _rs.rs.reconcile_after,
        ledger,
        clock,
        broker,
        manager,
        account_id,
        since,
        journal_account,
    )


def bracket_resync_cause(ledger: Ledger, account_id: str, order_id: str) -> tuple[Order, str] | None:
    """The bracket entry and the fill id of its latest fill, or None with no fills.

    ``order_id`` is the entry or one of its children. The latest fill is the last FILL
    event (by ledger seq) of the entry or of any order whose parent is the entry.
    """
    return _rs.call(_rs.rs.bracket_resync_cause, ledger, account_id, order_id)


def resync_open_brackets(ledger: Ledger, manager: OrderManager, account_id: str) -> int:
    """Re-sync every open bracket with a filled entry, before a run's first reconcile (F2).

    A crash can leave a fill committed with its protective stop never sent, and a rerun
    may never report that fill again. Returns how many entries were synced.
    """
    return _rs.call(_rs.rs.resync_open_brackets, ledger, manager, account_id)


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
    return _rs.call(_rs.rs.enqueue_journal_fill, ledger, clock, journal_account, state, fill)

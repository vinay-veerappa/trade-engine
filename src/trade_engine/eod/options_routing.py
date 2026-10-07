"""How a strategy's option actions reach the venue — shared by both runners (O4, I13).

Extracted from the EOD runner so the intraday service routes its actions through the
same rules instead of a second implementation drifting away (I13): the underlying
check at a snapshot, risk verdicts recorded under ``risk:<command_id>``, entries
through the options OMS, closes through ``OptionOrderManager``.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from trade_engine.domain.option_orders import (
    CloseHolding,
    CloseStructure,
    OpenStructure,
    OptionIntent,
)
from trade_engine.domain.risk import RiskVerdict
from trade_engine.eod.options import OptionContext
from trade_engine.interfaces.clock import Clock
from trade_engine.ledger import Event, EventKind, Ledger
from trade_engine.market_data.chains import ChainSnapshot
from trade_engine.oms.options import OptionOrderManager, open_structures
from trade_engine.sim import underlying_of
from trade_engine.eod._runtime import decide, flag
from trade_engine.sim._rs import rs

ROUNDS = 4  # how many times a strategy may act at one snapshot: a buy-write needs two


@dataclass
class RoutingTally:
    """The per-pass counters both runners accumulate."""

    orders_submitted: int = 0
    exit_actions: int = 0
    snapshots_processed: int = 0

    def __add__(self, other: "RoutingTally") -> "RoutingTally":
        return RoutingTally(
            *(int(v) for v in decide("routing:tally", text=tuple(str(v) for v in (
                self.orders_submitted, self.exit_actions, self.snapshots_processed,
                other.orders_submitted, other.exit_actions, other.snapshots_processed,
            )))[0])
        )


def _fail(message: str) -> Exception:
    """The runners' refusal type: both runners' tests and hosts catch it."""
    from trade_engine.eod.runner import EodRunnerError

    return EodRunnerError(message)


class OptionRouter:
    """Match, manage and route one options account against chain snapshots."""

    def __init__(
        self,
        ledger: Ledger,
        clock: Clock,
        *,
        brokers: Any,
        strategies: Any,
        option_risk_engines: Any,
        journal_accounts: Any | None = None,
    ) -> None:
        self._ledger = ledger
        self._clock = clock
        self._brokers = brokers
        self._strategies = strategies
        self._option_risk_engines = option_risk_engines
        self._journal_accounts = journal_accounts or {}
        self._option_managers: dict[str, OptionOrderManager] = {}

    # -- plumbing ----------------------------------------------------------------

    def manager(self, account_id: str) -> OptionOrderManager:
        manager = self._option_managers.get(account_id)
        if manager is None:
            manager = OptionOrderManager(self._brokers[account_id], self._clock, self._ledger)
            self._option_managers[account_id] = manager
        return manager

    def context(
        self,
        account_id: str,
        session: date,
        now: datetime,
        structures: tuple[OpenStructure, ...],
        snapshot: ChainSnapshot | None = None,
        snapshots: dict[str, ChainSnapshot] | None = None,
        phase: str | None = None,
    ) -> OptionContext:
        return OptionContext(
            session=session,
            account_id=account_id,
            phase=decide("routing:phase", (phase or "",), flags=(snapshot is not None,))[0][0],
            now=now,
            state=self._ledger.state(account_id),
            structures=structures,
            snapshot=snapshot,
            snapshots=dict(snapshots or {}),
        )

    def taken(self, action: Any) -> bool:
        """Whether an action was already routed: ordered, or (an entry) judged by risk."""
        command_id = getattr(action, "command_id", None)
        if not command_id:
            return False
        if flag("routing:taken", flags=(True, self._ledger.has_command(command_id), False, False)):
            return True
        if not flag("routing:risk_lookup", flags=(bool(command_id), isinstance(action, OptionIntent))):
            return False
        return flag("routing:taken", flags=(True, False, True, self._ledger.has_command(f"risk:{command_id}")))

    # -- the snapshot pass -------------------------------------------------------

    def match_snapshot(
        self, account_id: str, snapshot: ChainSnapshot, cause: str, since: datetime | None
    ) -> int:
        """Fill the account's working orders on this snapshot, then ingest immediately."""
        broker = self._brokers[account_id]
        broker.process_snapshot(snapshot)
        from trade_engine.oms.reconcile import MIN_TIME, ReconcileError, reconcile_after

        try:
            recorded = reconcile_after(
                self._ledger,
                self._clock,
                broker,
                self.manager(account_id).orders,
                account_id,
                since if since is not None else MIN_TIME,
                journal_account=self._journal_accounts.get(account_id),
            )
        except ReconcileError as err:
            raise _fail(str(err)) from err
        self.manager(account_id).sync(
            account_id,
            f"{cause}:{snapshot.underlying}",
        )
        return recorded

    def manage_at_snapshot(
        self,
        account_id: str,
        session: date,
        snapshot: ChainSnapshot,
        snapshots: dict[str, ChainSnapshot],
        cause: str,
    ) -> RoutingTally:
        """Match, then let the strategy act on these quotes until it brings nothing new."""
        return rs.options_manage(self, account_id, session, snapshot, snapshots, cause)

    def apply(
        self,
        account_id: str,
        session: date,
        actions: Iterable[Any],
        snapshot: ChainSnapshot | None,
        snapshots: dict[str, ChainSnapshot] | None,
        cause: str,
    ) -> RoutingTally:
        """Route a strategy's options actions through the risk layer and the OMS.

        At a snapshot, an action must concern that snapshot's underlying: it is matched
        against those quotes at once, and any other underlying's would not be the ones
        it was decided on. A refused guard (C3, C4, C5) fails the run loudly.
        """
        return rs.options_apply(self, account_id, session, actions, snapshot, snapshots, cause)

    def _action_underlying(self, account_id: str, action: Any) -> str:
        entry = None
        if isinstance(action, OptionIntent):
            kind, underlying = "intent", underlying_of(action.instrument)
        elif isinstance(action, CloseHolding):
            kind, underlying = "holding", action.instrument.symbol
        elif isinstance(action, CloseStructure):
            entry = self._ledger.state(account_id).orders.get(action.entry_order_id)
            kind, underlying = "close", underlying_of(entry.instrument) if entry is not None else ""
        else:
            kind, underlying = "unknown", ""
        return decide("routing:underlying", (
            kind, account_id, getattr(action, "command_id", ""), getattr(action, "entry_order_id", ""),
            underlying, type(action).__name__,
        ), flags=(entry is not None,))[0][0]

    def enter_option(
        self,
        account_id: str,
        session: date,
        intent: OptionIntent,
        snapshot: ChainSnapshot | None,
        snapshots: dict[str, ChainSnapshot] | None,
    ) -> int:
        return rs.options_enter(self, account_id, session, intent, snapshot, snapshots)

    def _record_verdict(self, account_id: str, intent: OptionIntent, verdict: RiskVerdict) -> None:
        now = self._clock.now_utc()
        self._ledger.append(
            Event(
                account=account_id,
                kind=EventKind.RISK_VERDICT,
                payload=verdict,
                ts_utc=now,
                command_id=f"risk:{intent.command_id}",
            )
        )

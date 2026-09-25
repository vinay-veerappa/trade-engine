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

ROUNDS = 4  # how many times a strategy may act at one snapshot: a buy-write needs two


@dataclass
class RoutingTally:
    """The per-pass counters both runners accumulate."""

    orders_submitted: int = 0
    exit_actions: int = 0
    snapshots_processed: int = 0

    def __add__(self, other: "RoutingTally") -> "RoutingTally":
        return RoutingTally(
            self.orders_submitted + other.orders_submitted,
            self.exit_actions + other.exit_actions,
            self.snapshots_processed + other.snapshots_processed,
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
            phase=phase or ("close" if snapshot is None else "snapshot"),
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
        if self._ledger.has_command(command_id):
            return True
        return isinstance(action, OptionIntent) and self._ledger.has_command(f"risk:{command_id}")

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
        manager = self.manager(account_id)
        tally = RoutingTally()
        self.match_snapshot(account_id, snapshot, cause, snapshot.as_of)
        tally.snapshots_processed += 1
        snapshots[snapshot.underlying] = snapshot
        manage = getattr(self._strategies.get(account_id), "manage_options", None)
        if not callable(manage):
            return tally
        for _ in range(ROUNDS):
            state = self._ledger.state(account_id)
            actions = list(
                manage(
                    self.context(
                        account_id,
                        session,
                        self._clock.now_utc(),
                        open_structures(state),
                        snapshot=snapshot,
                        snapshots=snapshots,
                    )
                )
            )
            if all(self.taken(action) for action in actions):
                return tally  # nothing new: every action is one already taken (I3)
            tally += self.apply(account_id, session, actions, snapshot, snapshots, cause)
            # Decided on these quotes, so traded on them.
            self.match_snapshot(account_id, snapshot, cause, snapshot.as_of)
        raise _fail(
            f"The strategy for '{account_id}' was still acting at the {snapshot.underlying} "
            f"snapshot after {ROUNDS} rounds; refusing to loop on it"
        )

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
        manager = self.manager(account_id)
        tally = RoutingTally()
        for action in actions:
            if snapshot is not None and self._action_underlying(account_id, action) != snapshot.underlying:
                raise _fail(
                    f"'{account_id}' returned {type(action).__name__} "
                    f"'{getattr(action, 'command_id', '?')}' at the {snapshot.underlying} "
                    f"snapshot for another underlying"
                )
            if not isinstance(action, (OptionIntent, CloseStructure, CloseHolding)):
                raise _fail(
                    f"Options account '{account_id}' was handed {type(action).__name__}; it enters "
                    "with OptionIntent"
                )
            if isinstance(action, OptionIntent):
                tally.orders_submitted += self.enter_option(account_id, session, action, snapshot, snapshots)
            elif isinstance(action, CloseStructure):
                manager.close(account_id, action)
                tally.exit_actions += 1
            elif isinstance(action, CloseHolding):
                manager.close_holding(account_id, action)
                tally.exit_actions += 1
            else:
                raise _fail(
                    f"Strategy for '{account_id}' returned {type(action).__name__}; options "
                    "actions are OptionIntent, CloseStructure or CloseHolding"
                )
        return tally

    def _action_underlying(self, account_id: str, action: Any) -> str:
        if isinstance(action, OptionIntent):
            return underlying_of(action.instrument)
        if isinstance(action, CloseHolding):
            return action.instrument.symbol
        if isinstance(action, CloseStructure):
            entry = self._ledger.state(account_id).orders.get(action.entry_order_id)
            if entry is None:
                raise _fail(
                    f"Close '{action.command_id}' names '{action.entry_order_id}', which is not "
                    f"an order of '{account_id}' (I8)"
                )
            return underlying_of(entry.instrument)
        raise _fail(f"Unknown options action {type(action).__name__}")

    def enter_option(
        self,
        account_id: str,
        session: date,
        intent: OptionIntent,
        snapshot: ChainSnapshot | None,
        snapshots: dict[str, ChainSnapshot] | None,
    ) -> int:
        if not isinstance(intent, OptionIntent):
            raise _fail(
                f"Options account '{account_id}' was handed {type(intent).__name__}; it enters "
                "with OptionIntent"
            )
        if intent.account_id != account_id:
            raise _fail(
                f"Intent '{intent.intent_id}' targets account '{intent.account_id}' but was "
                f"produced for '{account_id}' (I8)"
            )
        engine = self._option_risk_engines.get(account_id)
        if engine is None:
            raise _fail(
                f"'{account_id}' asked to enter '{intent.intent_id}' and has no options risk "
                f"engine; nothing enters unchecked (I5)"
            )
        now = self._clock.now_utc()
        context = self.context(
            account_id,
            session,
            now,
            open_structures(self._ledger.state(account_id)),
            snapshot=snapshot,
            snapshots=snapshots,
        )
        verdict = engine.evaluate(intent, context)
        self._record_verdict(account_id, intent, verdict)
        if not verdict.accepted:
            return 0
        if verdict.approved_quantity is not None and verdict.approved_quantity != intent.quantity:
            from dataclasses import replace

            intent = replace(intent, quantity=verdict.approved_quantity)
        self.manager(account_id).open(intent)
        return 1

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

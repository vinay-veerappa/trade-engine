"""End-of-day runner (E7, Architecture §4.9).

The 17:45 ET job per account and session, run through the native ``trade_engine_rs``
flow (``rs.eod_run`` / ``rs.eod_pass``). The phases, the options-account path and the
in-session passes are documented in ``docs/design/EOD_FLOW.md``.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from collections.abc import Iterable
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from trade_engine.calendar.sessions import ExchangeCalendar
from trade_engine.domain.instruments import Instrument
from trade_engine.domain.orders import OrderState
from trade_engine.domain.signals import OrderIntent
from trade_engine.interfaces.broker import (
    BrokerAdapter,
    VenueFill,
    VenueOrder,
    VenuePosition,
)
from trade_engine.interfaces.clock import Clock
from trade_engine.eod.options_routing import OptionRouter
from trade_engine.interfaces.market_data import MarketData
from trade_engine.ledger import Ledger
from trade_engine.ledger.state import AccountState
from trade_engine.market_data.chains import ChainSnapshot
from trade_engine.oms.manager import OrderManager
from trade_engine.oms.options import OptionOrderManager
from trade_engine.oms.reconcile import ReconcileError, resync_open_brackets
from trade_engine.oms.restore import RestoreError
from trade_engine.risk import RiskContext, RiskEngine
from trade_engine.sim import SimBroker, SnapshotVenue
from trade_engine.sim._rs import register, rs

MIN_TIME = datetime.min.replace(tzinfo=timezone.utc)
# The in-session passes, in the order a session runs them: the morning's entries, then
# the midday and late exits (owner, 2026-09-26: exits reach the venue the same day).
PASSES = ("morning", "midday", "late")


class EodRunnerError(RuntimeError):
    """Base class for refused EOD runs (I5)."""


class SessionIncompleteError(EodRunnerError):
    """The requested session's predecessor has not been run for every account."""


class ReplayDataError(EodRunnerError):
    """The market-data answer cannot support a deterministic session replay."""


register("runtime_eod", EodRunnerError)


def void_session(
    ledger: Ledger,
    clock: Clock,
    calendar: ExchangeCalendar,
    job: str,
    session: date,
    accounts: Iterable[str],
    *,
    reason: str,
    owner: str,
) -> tuple[str, ...]:
    """Record the owner's void of ``session`` for each of ``accounts`` (EOD-VOID-1).

    A session that can never complete (an options session whose expiry cannot be
    settled) would otherwise block every later session through the I3
    previous-session gate. A void is the owner's signed statement that the session is
    complete for that account; the gate then lets the next session through. It is NOT
    trading history: nothing folds from it and it does not count as a run.

    Everything is validated before anything is written: a non-empty reason and owner,
    a real trading session, a known account, and no EodRun already recorded for that
    account and session (a session that ran is not voided). A second void of the same
    account and session is an idempotent no-op (I3, the command id). Returns the
    accounts handled, in the order given with repeats dropped.
    """
    return tuple(rs.eod_void_session(ledger, clock, calendar, job, session, list(accounts), reason, owner))


def write_off_order(
    ledger: Ledger,
    clock: Clock,
    account_id: str,
    order_id: str,
    *,
    reason: str,
    owner: str,
    venue_cancel_confirmed: bool = False,
) -> tuple[str, ...]:
    """Record the owner's write-off of one stuck working order (SCAN-WRITEOFF-1).

    A working entry that never fills makes the session replay demand bars for its
    name every session; with none to be had, the I3 gate refuses every later run. The
    write-off is the owner's signed cancel: the entry and each non-terminal bracket child
    become CANCELLED through the fold's ordinary ORDER_CANCELLED step. No Fill, CashFlow,
    Mark or EodRun is written, so cash and realized P&L do not move.

    Everything is validated before anything is written: a non-empty reason and owner, a
    known account, an order that exists, is still working and has no fills (a filled or
    partly filled order is a position, not a write-off). A repeat (the entry's command id
    already in the ledger) is checked first and writes nothing.

    F4 refusals (also before any write): a protective child (a bracket stop or target)
    whose parent entry has fills protects a live position, so it is refused (I5). An
    order, or a child it would cancel, with a venue order id may still be live at the
    venue, so it is refused unless the owner passes ``venue_cancel_confirmed=True`` after
    cancelling it at the venue. The entry and its children are written in one
    ``Ledger.extend`` transaction, and each cancel keeps its venue order id.

    Returns the ids cancelled.
    """
    return tuple(rs.eod_write_off(ledger, clock, account_id, order_id, reason, owner, bool(venue_cancel_confirmed)))
register("runtime_incomplete", SessionIncompleteError)
register("runtime_replay", ReplayDataError)


def _normalize(mapping: Mapping | None) -> Mapping:
    return mapping if mapping is not None else {}


@dataclass(frozen=True)
class EodRunnerConfig:
    """Wiring for one runner instance, supplied entirely by the host (I5).

    Optional mappings: ``signal_adapters`` / ``strategies`` / ``risk_engines`` key
    accounts to plugin objects (L1's job); an account missing any of them gets replay,
    marks and a marker but no D+1 entries. ``sinks`` maps an outbox destination to an
    object with ``publish``; ``journal_accounts`` maps an engine account to the journal
    account id from config (never "first account", I5).

    Options accounts (a venue with ``process_snapshot``) also need: ``chain_snapshots``,
    the session's snapshots to match (the host picks them, e.g. the 15:45 pull);
    ``settlements``, the official closes that mark shares and underlyings; and, for an
    account that enters, ``option_risk_engines``. ``lifecycle`` settles expiries and
    assignments, and ``dividends`` credits dividends on shares held. ``settle_delay`` is
    how long after the close the after-close work runs. ``mark_max_quote_age_seconds``
    is how old an option's own quote may be against its snapshot before the mark is
    flagged ``stale-quote:`` (the mark is still the mid: flagged, never refused).
    """

    job_name: str
    brokers: Mapping[str, BrokerAdapter]
    risk_engines: Mapping[str, RiskEngine] | None = None
    signal_adapters: Mapping[str, Any] | None = None
    strategies: Mapping[str, Any] | None = None
    context_builder: Callable[[OrderIntent, AccountState, date], RiskContext] | None = None
    sinks: Mapping[str, Any] | None = None
    journal_accounts: Mapping[str, str] | None = None
    bars_max_age_seconds: float = 10.0**9
    chain_snapshots: Callable[[date], Sequence[ChainSnapshot]] | None = None
    settlements: Any | None = None
    lifecycle: Any | None = None
    dividends: Any | None = None
    option_risk_engines: Mapping[str, Any] | None = None
    settle_delay: timedelta = timedelta(minutes=105)
    mark_max_quote_age_seconds: float = 900.0

    @property
    def normalized(self) -> "EodRunnerConfig":
        return replace(
            self,
            risk_engines=_normalize(self.risk_engines),
            signal_adapters=_normalize(self.signal_adapters),
            strategies=_normalize(self.strategies),
            sinks=_normalize(self.sinks),
            journal_accounts=_normalize(self.journal_accounts),
            option_risk_engines=_normalize(self.option_risk_engines),
        )

    def __post_init__(self) -> None:
        limit = self.mark_max_quote_age_seconds
        if isinstance(limit, bool) or not isinstance(limit, (int, float)) or not math.isfinite(limit) or limit <= 0:
            raise ValueError(f"mark_max_quote_age_seconds must be a positive finite number, not {limit!r}")
        rs.eod_config_check(self.job_name, self.brokers, self.bars_max_age_seconds, self.settle_delay)


@dataclass(frozen=True)
class AccountRunResult:
    account_id: str
    bars_processed: int = 0
    fills_recorded: int = 0
    marks_appended: int = 0
    orders_submitted: int = 0
    exit_actions: int = 0
    snapshots_processed: int = 0


@dataclass(frozen=True)
class EodRunResult:
    session: date
    accounts: tuple[AccountRunResult, ...]


@dataclass
class _Tally:
    """Per-account counters and closes gathered while the session replays."""

    fills_before: int
    bars_processed: int = 0
    last_regular_closes: dict[Instrument, Decimal] = field(default_factory=dict)
    snapshots_processed: int = 0
    exit_actions: int = 0
    orders_submitted: int = 0
    # The newest snapshot matched per underlying (options accounts).
    snapshots: dict[str, ChainSnapshot] = field(default_factory=dict)


class EodRunner:
    """Run the EOD job for one session across the configured accounts."""

    def __init__(
        self,
        ledger: Ledger,
        clock: Clock,
        calendar: ExchangeCalendar,
        market_data: MarketData,
        config: EodRunnerConfig,
    ) -> None:
        self._ledger = ledger
        self._clock = clock
        self._calendar = calendar
        self._market_data = market_data
        self._config = config.normalized
        # The native EOD host needs a plain Ledger on the XNYS calendar; it refuses otherwise
        # (no Python fallback, W6-C3a).
        rs.eod_require_native(ledger, calendar)
        self._managers: dict[str, OrderManager] = {}
        self._option_managers: dict[str, OptionOrderManager] = {}
        self._option_routers: dict[str, OptionRouter] = {
            account_id: OptionRouter(
                ledger,
                clock,
                brokers=self._config.brokers,
                strategies=self._config.strategies,
                option_risk_engines=self._config.option_risk_engines,
                journal_accounts=self._config.journal_accounts,
            )
            for account_id in self._config.brokers
        }

    # -- entry -------------------------------------------------------------------

    def run(self, session: date) -> EodRunResult:
        self._resync_brackets()
        return rs.eod_run(self, session)

    def run_morning(self, session: date, through: datetime) -> EodRunResult:
        """The morning pass of ``session`` (``run_pass`` with ``"morning"``)."""
        return self.run_pass(session, through, "morning")

    def _resync_brackets(self) -> None:
        """Re-sync open brackets before the first reconcile, healing any crash after a fill (F2)."""
        for account_id in sorted(self._config.brokers):
            resync_open_brackets(
                self._ledger, self._manager_for(account_id, self._config.brokers[account_id]), account_id
            )

    def run_pass(self, session: date, through: datetime, name: str) -> EodRunResult:
        """Run options accounts' snapshots of ``session`` up to ``through`` (see above).

        Only options accounts take part; an account whose ``name`` pass or after-close run
        of the session is already recorded is left alone (I3). A pass must end after the
        account's previous pass of the session: the snapshots up to that one are already
        matched. The previous session must be complete, as for ``run``. A held
        underlying with no snapshot yet is not refused: nothing is marked in a pass, and
        its orders work at a later one.
        """
        self._resync_brackets()
        return rs.eod_pass(self, session, through, name)

    def void_session(self, session: date, accounts: Iterable[str], *, reason: str, owner: str) -> tuple[str, ...]:
        """Void ``session`` for ``accounts``: the owner's signed record that it is complete (I3).

        See ``void_session`` (module level), which this runs against the runner's own
        ledger, clock, calendar and job name.
        """
        return void_session(
            self._ledger, self._clock, self._calendar, self._config.job_name, session, accounts,
            reason=reason, owner=owner,
        )

    # -- per-account run ---------------------------------------------------------

    def _restore_venue(self, account_id: str, broker: BrokerAdapter) -> None:
        """Restore an empty simulated venue from the ledger.

        A SimBroker lives in memory, so a new process starts with an empty book while
        the ledger still holds yesterday's D+1 entries and GTC stops. An empty
        SimBroker is restored from the fold (I2), positions included: a held stock or
        option with no working order is still held at the venue. The native prepare
        then checks every working order against the venue (I5).
        """
        if (
            isinstance(broker, (SimBroker, SnapshotVenue))
            and not broker.orders(MIN_TIME)
            and not broker.fills(MIN_TIME)
        ):
            state = self._ledger.state(account_id)
            orders, fills = self._restorable(account_id, state)
            positions = self._restorable_positions(state)
            if orders or positions:
                broker.restore(orders, fills, positions)

    def _restorable(
        self, account_id: str, state: AccountState
    ) -> tuple[list[tuple[VenueOrder, OrderState]], list[VenueFill]]:
        """Working orders plus their brackets: a child's cap needs its parent's fills
        and its siblings' exits. NEW orders were never sent, so they stay out."""
        from trade_engine.oms.restore import restorable

        try:
            return restorable(self._ledger, account_id, state)
        except RestoreError as err:
            raise EodRunnerError(str(err)) from err

    @staticmethod
    def _restorable_positions(state: AccountState) -> list[VenuePosition]:
        from trade_engine.oms.restore import restorable_positions

        return restorable_positions(state)

    def _reconcile_after_bar(
        self,
        account_id: str,
        broker: BrokerAdapter,
        manager: OrderManager,
        bar_timestamp: datetime | None,
    ) -> int:
        """Ingest everything the venue changed at this bar, immediately (E4's contract).

        The rule lives in ``oms.reconcile`` so the intraday service ingests through the
        same code; a raise fails the whole run loudly, which is what the guard is for.
        """
        from trade_engine.oms.reconcile import MIN_TIME, reconcile_after

        try:
            return reconcile_after(
                self._ledger,
                self._clock,
                broker,
                manager,
                account_id,
                MIN_TIME if bar_timestamp is None else bar_timestamp,
                journal_account=self._config.journal_accounts.get(account_id),
            )
        except ReconcileError as err:
            raise EodRunnerError(str(err)) from err

    def _manager_for(self, account_id: str, broker: BrokerAdapter) -> OrderManager:
        if callable(getattr(self._config.brokers[account_id], "process_snapshot", None)):
            return self._option_manager(account_id).orders
        manager = self._managers.get(account_id)
        if manager is None:
            manager = OrderManager(broker, self._clock, self._ledger)
            self._managers[account_id] = manager
        return manager

    # -- options accounts (O4) ---------------------------------------------------

    def _option_manager(self, account_id: str) -> OptionOrderManager:
        manager = self._option_managers.get(account_id)
        if manager is None:
            manager = OptionOrderManager(self._config.brokers[account_id], self._clock, self._ledger)
            self._option_managers[account_id] = manager
        return manager

    def _router(self, account_id: str) -> "OptionRouter":
        return self._option_routers[account_id]

    def _enter_option(
        self,
        account_id: str,
        session: date,
        intent: Any,
        tally: "_Tally",
        snapshot: ChainSnapshot | None,
    ) -> int:
        # The risk engine sees the session's snapshots, as every entry decided at the
        # close always did: a combo's margin needs its underlying's quotes (O3).
        return self._router(account_id).enter_option(
            account_id, session, intent, snapshot, tally.snapshots
        )

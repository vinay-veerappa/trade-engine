"""The intraday service for 0DTE strategies (build plan I1, Architecture §4.9, P3).

A *live* loop, run through the native ``trade_engine_rs`` flow (``native.intraday_run``).
The tick, stale-quote, heartbeat and idempotency rules are documented in
``docs/design/INTRADAY_SERVICE.md``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from trade_engine.calendar.sessions import ExchangeCalendar
from trade_engine.domain.option_orders import OptionIntent
from trade_engine.eod.options_routing import OptionRouter, RoutingTally
from trade_engine.interfaces.clock import Clock
from trade_engine.interfaces.market_data import StaleDataError
from trade_engine.ledger import Ledger
from trade_engine.market_data.chains import ChainSnapshot
from trade_engine.oms.reconcile import resync_open_brackets
from trade_engine.sim._rs import register, rs as native

NEW_YORK = ZoneInfo("America/New_York")


class IntradayServiceError(RuntimeError):
    """A refused intraday run (I5)."""


class IntradayServiceAlert(IntradayServiceError):
    """The service stopped mid-session on an unexpected error, after trying to be flat.

    Unlike a refusal at start-up, a restart can help: the ledger holds whatever flatten
    was asked, and the restarted service fills it at its first fresh snapshot.
    """


@dataclass(frozen=True)
class Heartbeat:
    """Liveness proof written from start-up to exit; the ledger stays the only state (I2).

    ``exited`` marks the last beat of a process that stopped on its own (the close, or an
    alert): it proves nobody is running, so it never refuses a restart. ``alert`` is set
    when the service stopped on an unexpected error.
    """

    account_id: str
    session: date
    at_utc: datetime
    refusing: bool
    note: str = ""
    alert: bool = False
    exited: bool = False


register("runtime_intraday", IntradayServiceError)
register("runtime_stale", StaleDataError)


@dataclass(frozen=True)
class IntradayConfig:
    """Wiring for one service instance, supplied entirely by the host (I5).

    ``strategy`` carries the host's plugin: its ``manage_options`` runs at every fresh
    snapshot, exactly as the EOD runner calls it. ``eod_job_name`` names the after-close
    job whose marker (``eod:<job>:<account>:<session>``) must exist for the previous
    session before this one starts. ``flat_at`` and ``entry_end`` are the rules' clock
    times; on an early close the service pulls both in (see the module doc).
    """

    job_name: str
    account_id: str
    underlying: str
    broker: Any
    strategy: Any
    option_risk_engine: Any | None
    # (underlying, now) -> the snapshot as it stood at ``now``, fetched and kept.
    snapshot_source: Callable[[str, datetime], ChainSnapshot]
    eod_job_name: str
    max_quote_age_seconds: float = 30.0
    tick_seconds: float = 5.0
    flat_at: time = time(15, 30)  # ET; "flat 15:30" (rules doc §6.4)
    entry_end: time = time(12, 0)  # ET; "9:45–12:00 entry" (rules doc §6.4)
    # Early closes: flat 30 minutes before any close, as on a full day; entries end 90
    # minutes before it, so the last entry still has an hour to work before the sweep.
    flat_before_close: timedelta = timedelta(minutes=30)
    entry_before_close: timedelta = timedelta(minutes=90)
    # The close mark needs a fresh snapshot taken this near the close; otherwise the
    # after-close pass marks (O2).
    close_mark_window_seconds: float = 120.0
    heartbeat_ttl_seconds: float = 60.0
    journal_account: str | None = None

    def __post_init__(self) -> None:
        native.intraday_config_check(self)


class _EntryGate:
    """The strategy as the router sees it: entries pass only while the service allows.

    Exits always pass (a stop must work at any hour); an ``OptionIntent`` returned while
    entries are closed is dropped and counted, so no entry is made after the entry end,
    after the sweep, or while the service is refusing.
    """

    def __init__(self, strategy: Any) -> None:
        self._strategy = strategy
        self.name = getattr(strategy, "name", type(strategy).__name__)
        self.allow = False
        self.dropped = 0

    def manage_options(self, context: Any) -> list:
        manage = getattr(self._strategy, "manage_options", None)
        if not callable(manage):
            return []
        actions = list(manage(context))
        keep, self.dropped = native.intraday_gate(
            self.allow, [isinstance(action, OptionIntent) for action in actions], self.dropped)
        return [action for action, selected in zip(actions, keep) if selected]


class IntradayService:
    """Run one 0DTE account live from the session open to the close."""

    def __init__(
        self,
        ledger: Ledger,
        clock: Clock,
        calendar: ExchangeCalendar,
        config: IntradayConfig,
        *,
        heartbeat_path: Any | None = None,
    ) -> None:
        native.intraday_require_native(ledger, calendar)
        self._ledger = ledger
        self._clock = clock
        self._calendar = calendar
        self._config = config
        self._heartbeat_path = None if heartbeat_path is None else Path(heartbeat_path)
        self._gate = _EntryGate(config.strategy)
        self._router = OptionRouter(
            ledger,
            clock,
            brokers={config.account_id: config.broker},
            strategies={config.account_id: self._gate},
            option_risk_engines=(
                {config.account_id: config.option_risk_engine} if config.option_risk_engine else {}
            ),
            journal_accounts=(
                {config.account_id: config.journal_account} if config.journal_account else {}
            ),
        )

    # -- entry -------------------------------------------------------------------

    def run(
        self,
        session: date,
        *,
        start_at: datetime | None = None,
        stop_at: datetime | None = None,
    ) -> dict[str, Any]:
        """Walk one session. ``stop_at`` simulates a crash: the loop stops there
        without a marker, exactly as a killed process would leave the ledger (I2)."""
        account_id = self._config.account_id
        resync_open_brackets(self._ledger, self._router.manager(account_id).orders, account_id)
        return native.intraday_run(self, session, start_at, stop_at)

    # -- the tick ----------------------------------------------------------------

    def _tick(self, session: date, state: "_TickState") -> None:
        native.intraday_tick(self, session, state)

    def _flatten(self, session: date, code: str, reason: str) -> int:
        """Close at market whatever is open and not already closing; returns how many."""
        return native.intraday_flatten(self, session, code, reason)

    # -- the close -----------------------------------------------------------------

    def _close_mark(self, session: date, state: "_TickState") -> str:
        return native.intraday_close(self, session, state)

    # -- restart gate, heartbeat, plumbing ---------------------------------------

    def _write_heartbeat(
        self,
        session: date,
        state: "_TickState",
        note: str,
        *,
        alert: bool = False,
        exited: bool = False,
    ) -> None:
        """Written to a temporary file and moved into place: a reader never sees half."""
        native.intraday_heartbeat_write(self, session, state, note, alert, exited)

    def _read_heartbeat(self) -> Heartbeat | None:
        return native.intraday_heartbeat_read(self)

    def _pause(self) -> None:
        self._clock.sleep(self._config.tick_seconds)


@dataclass
class _TickState:
    refusing: bool = False
    barred: str | None = None  # a reason entries are refused for the whole session
    tally: RoutingTally = field(default_factory=RoutingTally)
    snapshots: dict[str, ChainSnapshot] = field(default_factory=dict)



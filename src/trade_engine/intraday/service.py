"""The intraday service for 0DTE strategies (build plan I1, Architecture §4.9, P3).

A *live* loop, run through the native ``trade_engine_rs`` flow (``native.intraday_run``).
The tick, stale-quote, heartbeat and idempotency rules are documented in
``docs/design/INTRADAY_SERVICE.md``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import trade_engine_rs as rs

from trade_engine.domain.option_orders import OptionIntent
from trade_engine.eod.options_routing import RoutingTally
from trade_engine.interfaces.market_data import StaleDataError
from trade_engine.market_data.chains import ChainSnapshot
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


class IntradayService(rs.IntradayServiceBase):
    """Run one 0DTE account live from the session open to the close."""


@dataclass
class _TickState:
    refusing: bool = False
    barred: str | None = None  # a reason entries are refused for the whole session
    tally: RoutingTally = field(default_factory=RoutingTally)
    snapshots: dict[str, ChainSnapshot] = field(default_factory=dict)

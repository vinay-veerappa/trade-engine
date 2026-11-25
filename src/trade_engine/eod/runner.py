"""End-of-day runner (E7, Architecture §4.9).

The 17:45 ET job per account and session, run through the native ``trade_engine_rs``
flow (``rs.eod_run`` / ``rs.eod_pass``). The phases, the options-account path and the
in-session passes are documented in ``docs/design/EOD_FLOW.md``.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from trade_engine.calendar.sessions import ExchangeCalendar
from trade_engine.domain.instruments import Instrument
from trade_engine.domain.signals import OrderIntent
from trade_engine.eod.options_routing import OptionRouter
from trade_engine.interfaces.broker import BrokerAdapter
from trade_engine.interfaces.clock import Clock
from trade_engine.interfaces.market_data import MarketData
from trade_engine.ledger import Ledger
from trade_engine.ledger.state import AccountState
from trade_engine.market_data.chains import ChainSnapshot
from trade_engine.oms.manager import OrderManager
from trade_engine.oms.options import OptionOrderManager
# Kept importable: EodRunnerBase._resync_brackets reaches it by name (tests monkeypatch it).
from trade_engine.oms.reconcile import resync_open_brackets  # noqa: F401
from trade_engine.risk import RiskContext, RiskEngine
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
    return rs.void_session(ledger, clock, calendar, job, session, accounts, reason=reason, owner=owner)


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
    return rs.write_off_order(
        ledger, clock, account_id, order_id,
        reason=reason, owner=owner, venue_cancel_confirmed=bool(venue_cancel_confirmed),
    )


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


class EodRunner(rs.EodRunnerBase):
    """Run the EOD job for one session across the configured accounts."""

    def __init__(
        self,
        ledger: Ledger,
        clock: Clock,
        calendar: ExchangeCalendar,
        market_data: MarketData,
        config: EodRunnerConfig,
    ) -> None:
        super().__init__()
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

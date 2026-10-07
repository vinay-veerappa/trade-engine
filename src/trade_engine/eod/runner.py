"""End-of-day runner (E7, Architecture §4.9).

The 17:45 ET job per account and session, in order:

1. Gate: a session whose previous session has no completed run for the account
   refuses (bootstrap: an account's first run has no previous marker to demand).
2. Replay: one-minute bars for every instrument an account orders or holds, merged
   into one timeline across accounts and instruments so a single clock walks the
   session minute by minute. After **every** bar the runner reconciles every order
   the venue touched that bar — fills recorded, states read back, brackets
   synchronized. SimBroker rejects a protective stop submitted after later bars were
   simulated, and the OMS raises; the runner never batches reconciliation to the end
   of the session, because the bars in between are gone.
3. Close: the clock moves to the session close, so marks are dated at the close and a
   D+1 DAY entry belongs to the next session instead of expiring at its open.
   MTM: one Mark per open position at the last regular bar's close; a missing
   regular bar refuses (I5).
   Exits: a strategy with ``manage_positions`` sees its open brackets and may tighten
   stops, close positions or reduce them at the next open (``domain.exits``); the OMS
   applies them.
4. Marker: one ``EodRun`` event per account, claimed by
   ``eod:<job>:<account>:<session>``. A re-run replays deterministically and every
   command id it derives is already claimed, so the second run appends nothing.
5. Entries for D+1: discovered signals and strategies produce intents; the risk layer
   evaluates each; approved intents become brackets whose DAY entry works the next
   session.
6. Outbox: every configured destination drains in order (I12).

Options accounts (O4) are those whose venue matches chain snapshots
(``SnapshotVenue``). They replay no bars. Instead:

- each of the session's chain snapshots (``chain_snapshots``) is matched at its own
  ``as_of`` inside the same timeline, and the strategy's ``manage_options`` runs right
  after it (``eod.options``);
- after the close the clock moves on by ``settle_delay``, to when the official close is
  known (daily bars settle at 17:00 ET). The O2 lifecycle pass then settles expiries and
  assignments, dividends going ex next session are credited on the shares held at the
  close, and positions are marked: options at the newest snapshot's mid, shares and
  underlyings at the official close (``settlements``);
- close-phase exits and D+1 entries go through the options OMS (``oms.options``), with the
  C3/C4/C5 guards, and entries through the account's options risk engine.

Equity accounts finish at the close before any of that, so their marks keep the close's
stamp.

An in-session pass (``run_pass(session, through, name)``, ``name`` one of ``PASSES``;
``run_morning`` is the morning one) runs an options account's part of the session up to
``through``, while the market is open: the session's snapshots taken since the account's
previous pass are matched and the strategy acts on them, exactly as the after-close run
would have. What the strategy decides at a pass's snapshot is decided and filled on those
quotes, so the host can send it to a venue while the market is still open. Each pass
claims its own marker (``eod:<job>-<name>:<account>:<session>``, ``at_close`` =
``through``); a later pass, and the after-close run, skip every snapshot at or before the
newest of them, taking each only as the newest quote of its underlying. Nothing is
settled, marked or entered from signals in a pass: that is still the after-close run's.

The runner owns orchestration only. Expiry/assignment semantics stay with O2, EOD exit
rules belong to strategy plugins (I13) and reach the venue only through the OMS, and
market data comes from injected providers (I5: no default source).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from trade_engine.calendar.sessions import ExchangeCalendar
from trade_engine.domain.exits import ClosePosition, MoveStop, OpenBracket, ReducePosition
from trade_engine.domain.instruments import Equity, Instrument, OptionContract
from trade_engine.domain.option_roots import SettleTime
from trade_engine.domain.orders import OrderState, OrderType
from trade_engine.domain.risk import RiskVerdict
from trade_engine.domain.signals import OrderIntent, Signal
from trade_engine.interfaces.broker import (
    BrokerAdapter,
    VenueFill,
    VenueOrder,
    VenuePosition,
)
from trade_engine.interfaces.clock import Clock
from trade_engine.eod.options_routing import OptionRouter
from trade_engine.interfaces.market_data import MarketData, StaleDataError
from trade_engine.ledger import CashFlow, EodRun, Event, EventKind, Ledger, Mark
from trade_engine.ledger.state import AccountState, fold_account
from trade_engine.market_data.chains import ChainSnapshot
from trade_engine.oms.manager import OrderManager
from trade_engine.oms.options import OptionOrderManager, open_structures
from trade_engine.oms.reconcile import ReconcileError
from trade_engine.oms.restore import RestoreError
from trade_engine.risk import RiskContext, RiskEngine
from trade_engine.sim import SimBroker, SnapshotVenue, underlying_of
from trade_engine.sim._rs import register, rs
from trade_engine.eod._runtime import decide, flag, micros

MIN_TIME = datetime.min.replace(tzinfo=timezone.utc)
NEW_YORK = ZoneInfo("America/New_York")
MORNING_SUFFIX = "-morning"
# The in-session passes, in the order a session runs them: the morning's entries, then
# the midday and late exits (owner, 2026-09-26: exits reach the venue the same day).
PASSES = ("morning", "midday", "late")


def pass_of(job: str) -> str | None:
    """The in-session pass a run marker's job names (``eod-midday``), or None."""
    names = decide("eod:pass_of", (job,))[0]
    return names[0] if names else None


class EodRunnerError(RuntimeError):
    """Base class for refused EOD runs (I5)."""


class SessionIncompleteError(EodRunnerError):
    """The requested session's predecessor has not been run for every account."""


class ReplayDataError(EodRunnerError):
    """The market-data answer cannot support a deterministic session replay."""


register("runtime_eod", EodRunnerError)
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
    how long after the close the after-close work runs.
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
        age_type = isinstance(self.bars_max_age_seconds, (int, float)) and not isinstance(self.bars_max_age_seconds, bool)
        delay_type = isinstance(self.settle_delay, timedelta)
        decide("eod:config", numbers=(micros(self.settle_delay) if delay_type else 0,),
            flags=(bool(self.job_name), bool(self.brokers), age_type, delay_type),
            floats=(self.bars_max_age_seconds if age_type else 0,))


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
        return rs.eod_run(self, session)

    def run_morning(self, session: date, through: datetime) -> EodRunResult:
        """The morning pass of ``session`` (``run_pass`` with ``"morning"``)."""
        return self.run_pass(session, through, "morning")

    def run_pass(self, session: date, through: datetime, name: str) -> EodRunResult:
        """Run options accounts' snapshots of ``session`` up to ``through`` (see above).

        Only options accounts take part; an account whose ``name`` pass or after-close run
        of the session is already recorded is left alone (I3). A pass must end after the
        account's previous pass of the session: the snapshots up to that one are already
        matched. The previous session must be complete, as for ``run``. A held
        underlying with no snapshot yet is not refused: nothing is marked in a pass, and
        its orders work at a later one.
        """
        return rs.eod_pass(self, session, through, name)

    def _pass_job(self, name: str) -> str:
        return decide("eod:pass_job", (self._config.job_name, name))[0][0]

    def _pass_command(self, account_id: str, session: date, name: str) -> str:
        return decide("eod:command", ("eod", self._pass_job(name), account_id, session.isoformat()))[0][0]

    def _passes_through(self, account_id: str, session: date) -> datetime | None:
        """Where the account's newest pass of the session stopped, if one ran."""
        ends = [
            event.payload.at_close
            for name in PASSES
            if (event := self._ledger.event_by_command(self._pass_command(account_id, session, name))) is not None
        ]
        chosen = decide("eod:latest", numbers=tuple(micros(end) for end in ends))[1]
        return ends[chosen[0]] if chosen else None

    def _is_options(self, account_id: str) -> bool:
        return callable(getattr(self._config.brokers[account_id], "process_snapshot", None))

    def _run_command(self, account_id: str, session: date) -> str:
        return decide("eod:command", ("eod", self._config.job_name, account_id, session.isoformat()))[0][0]

    def _require_previous_session_complete(self, session: date) -> None:
        previous = self._calendar.previous_session(session)
        for account_id in sorted(self._config.brokers):
            if self._ledger.event_by_command(self._run_command(account_id, session)) is not None:
                # This session already completed; the re-run proves idempotency itself.
                continue
            if self._account_has_history(account_id, session):
                decide("eod:previous", (session.isoformat(), account_id, previous.isoformat(), self._config.job_name),
                    flags=(True, self._ledger.event_by_command(self._run_command(account_id, previous)) is not None))

    def _account_has_history(self, account_id: str, session: date) -> bool:
        """Whether this job has marked a session BEFORE ``session`` for the account.

        Only this job's own markers count: an account the intraday service also runs
        carries that service's markers, and its first after-close run must not be
        refused for lacking a previous session it never had (I3). An in-session pass is
        this job's own: the session it began still needs its after-close run. But the
        run session's own markers are not history: the ledger's first session starts
        with a pass, and that session's passes and after-close run must not be refused
        for lacking a previous session it never had either.
        """
        for event in self._ledger.events(account=account_id):
            is_run = event.kind is EventKind.EOD_RUN
            if flag("eod:history",
                    (event.payload.job if is_run else "", self._config.job_name,
                     event.payload.session.isoformat() if is_run else "", session.isoformat()),
                    flags=(is_run,)):
                return True
        return False

    # -- per-account run ---------------------------------------------------------

    def _prepare_account(self, account_id: str) -> tuple[Instrument, ...]:
        broker = self._config.brokers[account_id]
        broker.connect()
        state = self._ledger.state(account_id)
        # An options account's orders fill at snapshots and its shares are marked at the
        # official close, so it replays no bars.
        instruments = () if self._is_options(account_id) else self._replay_instruments(state)
        self._rehydrate_venue(account_id, broker, state)
        return instruments

    def _replay_session(
        self,
        session: date,
        replays: Mapping[str, tuple[Instrument, ...]],
        tallies: Mapping[str, "_Tally"],
        snapshots: Sequence[ChainSnapshot] = (),
        options: Sequence[str] = (),
        mornings: Mapping[str, datetime] | None = None,
    ) -> None:
        """Feed every account's bars on one timeline, minute by minute.

        One clock serves every account, and the ledger records events in the order they
        happened: instrument by instrument, a 09:31 fill in the second symbol would land
        after a 15:00 exit in the first, stamped 15:59 (I7). Each bar goes to every
        account holding its instrument, and each account reconciles immediately. A chain
        snapshot takes its place at its own ``as_of``, ahead of the bar that opens at the
        same instant, and goes to every options account. A snapshot an account's morning
        pass already matched (``mornings``: when that pass stopped) is not matched again:
        it only stands as the newest quote of its underlying.
        """
        rs.eod_replay(self, session, replays, tallies, snapshots, options, mornings)

    def _finish_account(
        self,
        account_id: str,
        session: date,
        instruments: tuple[Instrument, ...],
        tally: "_Tally",
    ) -> AccountRunResult:
        return rs.eod_finish(self, account_id, session, instruments, tally, False)

    def _fill_count(self, account_id: str) -> int:
        return decide("eod:count_events", (EventKind.FILL.value, "", *(value
            for event in self._ledger.events(account=account_id)
            for value in (event.kind.value, event.command_id or ""))))[1][0]

    def _marks_appended(self, account_id: str, session: date) -> int:
        return decide("eod:count_events", (EventKind.MARK.value,
            f"eod:mark:{account_id}:{session.isoformat()}:", *(value
            for event in self._ledger.events(account=account_id)
            for value in (event.kind.value, event.command_id or ""))))[1][0]

    def _rehydrate_venue(
        self, account_id: str, broker: BrokerAdapter, state: AccountState
    ) -> None:
        """Make sure the venue holds every order the ledger says is working.

        A SimBroker lives in memory, so a new process starts with an empty book while
        the ledger still holds yesterday's D+1 entries and GTC stops. An empty
        SimBroker is restored from the fold (I2). Any venue is then checked: a working
        order it does not hold would silently never fill, so the run refuses (I5).
        """
        if (
            isinstance(broker, (SimBroker, SnapshotVenue))
            and not broker.orders(MIN_TIME)
            and not broker.fills(MIN_TIME)
        ):
            orders, fills = self._restorable(account_id, state)
            if orders:
                broker.restore(orders, fills, self._restorable_positions(state))
        held = {item.venue_order_id: item for item in broker.orders(MIN_TIME)}
        for order in sorted(state.orders.values(), key=lambda value: value.order_id):
            if not flag("eod:working", (order.state.value,)):
                continue
            venue_id = state.venue_order_ids.get(order.order_id, order.order_id)
            found = held.get(venue_id)
            recorded = state.filled_quantity.get(order.order_id, Decimal("0"))
            decide("eod:rehydrate", (account_id, order.order_id, order.state.value,
                str(found.filled_quantity) if found is not None else "0", str(recorded)),
                flags=(found is not None,))

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

    def _replay_instruments(self, state: AccountState) -> tuple[Instrument, ...]:
        """Instruments the session can change: open positions and working orders.

        A finished order has nothing left to fill; replaying its symbol forever would
        grow every run and let one delisted name with no bars block the account.
        """
        instruments: set[Instrument] = {
            instrument
            for instrument, position in state.positions.items()
            if flag("eod:nonzero", (str(position.quantity),))
        }
        for order in state.orders.values():
            if flag("eod:working", (order.state.value,)):
                instruments.add(order.instrument)
        ordered = tuple(sorted(instruments, key=lambda value: value.symbol))
        for instrument in ordered:
            decide("eod:replay_instrument", (instrument.symbol,), flags=(isinstance(instrument, Equity),))
        return ordered

    def _load_bars(
        self, instrument: Instrument, session_open: datetime, session_close: datetime
    ) -> list[Any]:
        raw = self._market_data.bars(
            instrument,
            "1m",
            session_open,
            session_close,
            self._config.bars_max_age_seconds,
        )
        final = session_close - timedelta(minutes=1)
        decide("eod:bars", (instrument.symbol,
            raw[0].timestamp.isoformat() if raw else "", session_open.isoformat(),
            raw[-1].timestamp.isoformat() if raw else "", final.isoformat()),
            numbers=(micros(raw[0].timestamp) if raw else 0, micros(session_open),
                micros(raw[-1].timestamp) if raw else 0, micros(session_close)), flags=(bool(raw),))
        regular = []
        for bar in raw:
            is_regular = flag("eod:regular", numbers=(micros(session_open), micros(bar.timestamp), micros(session_close)))
            regular.append((bar, is_regular))
        return regular

    def _advance_clock(self, bar_timestamp: datetime) -> None:
        advance = getattr(self._clock, "advance_to", None)
        now = self._clock.now_utc()
        if callable(advance) and now < bar_timestamp:
            advance(bar_timestamp)

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
        if self._is_options(account_id):
            return self._option_manager(account_id).orders
        manager = self._managers.get(account_id)
        if manager is None:
            manager = OrderManager(broker, self._clock, self._ledger)
            self._managers[account_id] = manager
        return manager

    def _mark_positions(
        self,
        account_id: str,
        last_regular_closes: Mapping[Instrument, Decimal],
        session: date,
    ) -> None:
        state = self._ledger.state(account_id)
        for instrument, position in sorted(
            state.positions.items(), key=lambda item: item[0].symbol
        ):
            if not flag("eod:nonzero", (str(position.quantity),)):
                continue
            mark_command = decide("eod:command", ("eod:mark",account_id,session.isoformat(),instrument.symbol))[0][0]
            if self._ledger.event_by_command(mark_command) is not None:
                continue
            close = last_regular_closes.get(instrument)
            decide("eod:mark", (str(position.quantity), instrument.symbol, account_id), flags=(False, close is not None))
            now = self._clock.now_utc()
            self._ledger.append(
                Event(
                    account=account_id,
                    kind=EventKind.MARK,
                    payload=Mark(
                        instrument=instrument,
                        price=close,
                        as_of=now,
                        source=f"eod:{self._config.job_name}",
                    ),
                    ts_utc=now,
                    command_id=mark_command,
                )
            )

    def _append_run_marker(
        self,
        account_id: str,
        session: date,
        bars_processed: int,
        *,
        job: str | None = None,
        command_id: str | None = None,
    ) -> None:
        now = self._clock.now_utc()
        self._ledger.append(
            Event(
                account=account_id,
                kind=EventKind.EOD_RUN,
                payload=EodRun(
                    session=session,
                    job=job or self._config.job_name,
                    account_id=account_id,
                    bars_processed=bars_processed,
                    at_close=now,
                ),
                ts_utc=now,
                command_id=command_id or self._run_command(account_id, session),
            )
        )

    # -- exits: strategy exit rules at the close ---------------------------------

    def _manage_positions(
        self,
        account_id: str,
        session: date,
        last_regular_closes: Mapping[Instrument, Decimal],
    ) -> int:
        """Hand the strategy its open brackets and apply the exits it asks for.

        Runs after marks and before D+1 entries. ``manage_positions`` is optional on a
        strategy; one without it keeps only its stops and targets. Every action goes
        through the OMS, which refuses a stop that would loosen (I5); a refused action
        fails the run loudly rather than leaving a position managed by half its rules.
        """
        return rs.eod_manage_positions(self, account_id, session, last_regular_closes)

    def _open_brackets(
        self,
        account_id: str,
        session: date,
        last_regular_closes: Mapping[Instrument, Decimal],
    ) -> list[OpenBracket]:
        state = self._ledger.state(account_id)
        brackets: list[OpenBracket] = []
        for entry in sorted(state.orders.values(), key=lambda order: order.order_id):
            if entry.parent_order_id is not None:
                continue
            orders = tuple(state.orders.values())
            _, indices, child_mask = decide("eod:children", (entry.order_id, *(value
                for order in orders for value in (order.parent_order_id or "", order.order_type.value, order.state.value))))
            children = [order for order, child in zip(orders, child_mask) if child]
            stop = orders[indices[0]] if indices[0] >= 0 else None
            entry_fills = [fill for fill in state.fills if fill.order_id == entry.order_id]
            if stop is None or stop.stop_price is None or not entry_fills:
                continue
            values, _, opened = decide("eod:bracket", (
                *(str(fill.quantity) for fill in entry_fills),
                *(str(state.filled_quantity.get(order.order_id, Decimal("0"))) for order in children),
                *(value for fill in entry_fills for value in (str(fill.price), str(fill.quantity))),
            ), numbers=(len(entry_fills), len(entry_fills) + len(children)))
            if not opened[0]:
                continue
            filled, open_quantity, average = map(Decimal, values)
            targets = [orders[i] for i in indices[1:]]
            first_fill = entry_fills[decide("eod:earliest", numbers=tuple(micros(fill.filled_at) for fill in entry_fills))[1][0]].filled_at
            entry_session = self._calendar.roll_to_session(
                first_fill.astimezone(NEW_YORK).date(), "next"
            )
            brackets.append(
                OpenBracket(
                    entry_order_id=entry.order_id,
                    account_id=account_id,
                    instrument=entry.instrument,
                    side=entry.side,
                    entry_quantity=filled,
                    open_quantity=open_quantity,
                    average_entry_price=average,
                    entry_filled_at=first_fill,
                    entry_session=entry_session,
                    sessions_held=len(self._calendar.sessions_in_range(entry_session, session))
                    - 1,
                    stop_price=stop.stop_price,
                    targets_filled=decide("eod:filled", tuple(order.state.value for order in targets))[1][0],
                    open_targets=tuple(
                        (
                            order.limit_price,
                            Decimal(decide("eod:subtract", (str(order.quantity),
                                str(state.filled_quantity.get(order.order_id, Decimal("0")))))[0][0]),
                        )
                        for order in targets
                        if not flag("eod:terminal", (order.state.value,)) and order.limit_price is not None
                    ),
                    last_close=last_regular_closes.get(entry.instrument),
                )
            )
        return brackets

    # -- entries for D+1 ---------------------------------------------------------

    def _submit_new_orders(self, account_id: str, session: date) -> int:
        return rs.eod_entries(self, account_id, session, None, False)

    def _record_signal(self, account_id: str, signal: Signal) -> None:
        now = self._clock.now_utc()
        self._ledger.append(
            Event(
                account=account_id,
                kind=EventKind.SIGNAL_SEEN,
                payload=signal,
                ts_utc=now,
                command_id=f"signal:{signal.signal_id}",
            )
        )

    def _record_verdict(self, account_id: str, intent: OrderIntent, verdict: RiskVerdict) -> None:
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

    def _derive_risk_context(
        self, intent: OrderIntent, state: AccountState
    ) -> RiskContext:
        """Fold-derived measurements; every input the fold cannot supply stays None,
        so the matching rule records UNKNOWN and refuses (I5), never guesses."""
        held = state.positions.get(intent.instrument)
        positions = tuple(state.positions.values())
        values, counts, positive = decide("eod:risk", (
            str(state.cash), str(held.quantity) if held is not None else "0",
            *(value for position in positions for value in (
                str(position.quantity), str(state.marks.get(position.instrument, Decimal("0"))))),
        ), flags=tuple(position.instrument in state.marks for position in positions))
        return RiskContext(
            equity=Decimal(values[0]) if positive[0] else None,
            current_price=None,
            gross_exposure=Decimal(values[1]),
            portfolio_heat=None,
            open_positions=counts[0],
            industry=None,
            industry_positions=None,
            average_dollar_volume_20d=None,
            sessions_until_earnings=None,
            regime=None,
            macro_high_risk_day=None,
            drawdown_from_peak_frac=None,
            previous_session_pnl_frac=None,
            venue_orders_today=None,
            venue_daily_pnl=None,
            current_position_quantity=int(values[2]),
        )

    # -- options accounts (O4) ---------------------------------------------------

    def _option_manager(self, account_id: str) -> OptionOrderManager:
        manager = self._option_managers.get(account_id)
        if manager is None:
            manager = OptionOrderManager(self._config.brokers[account_id], self._clock, self._ledger)
            self._option_managers[account_id] = manager
        return manager

    def _session_snapshots(
        self, session: date, options: Sequence[str], *, covered: bool = True
    ) -> list[ChainSnapshot]:
        """The session's chain snapshots, checked before anything is replayed (I5).

        Each must lie inside the session. With ``covered``, every underlying an options
        account holds an option on or has an order working on needs one: without it
        nothing could fill or be marked, and a guess is not a mark.
        """
        if not options:
            return []
        decide("eod:snapshots_wiring", (str(list(options)),),
            flags=(self._config.chain_snapshots is not None, self._config.settlements is not None))
        session_open = self._calendar.session_open(session)
        session_close = self._calendar.session_close(session)
        snapshots = sorted(
            self._config.chain_snapshots(session), key=lambda item: (item.as_of, item.underlying)
        )
        for snapshot in snapshots:
            valid = isinstance(snapshot, ChainSnapshot)
            decide("eod:snapshot", (type(snapshot).__name__, snapshot.underlying if valid else "",
                snapshot.as_of.isoformat() if valid else "", session.isoformat()),
                numbers=(micros(session_open), micros(snapshot.as_of) if valid else 0, micros(session_close)),
                flags=(valid,))
        have = {snapshot.underlying for snapshot in snapshots}
        for account_id in options if covered else ():
            missing = sorted(self._option_underlyings(self._ledger.state(account_id)) - have)
            decide("eod:coverage", (", ".join(missing), session.isoformat(), account_id))
        return snapshots

    @staticmethod
    def _option_underlyings(state: AccountState) -> set[str]:
        needed = {
            underlying_of(instrument)
            for instrument, position in state.positions.items()
            if isinstance(instrument, OptionContract) and flag("eod:nonzero", (str(position.quantity),))
        }
        needed |= {
            underlying_of(order.instrument)
            for order in state.orders.values()
            if flag("eod:working", (order.state.value,))
        }
        return needed

    def _options_at_snapshot(
        self, account_id: str, session: date, snapshot: ChainSnapshot, tally: "_Tally"
    ) -> None:
        """Match the account's working orders, then let the strategy act on these quotes."""
        tallied = self._router(account_id).manage_at_snapshot(
            account_id, session, snapshot, tally.snapshots, self._run_command(account_id, session)
        )
        tally.orders_submitted, tally.exit_actions, tally.snapshots_processed = (int(v) for v in decide("routing:tally", text=tuple(str(v) for v in (
            tally.orders_submitted, tally.exit_actions, tally.snapshots_processed,
            tallied.orders_submitted, tallied.exit_actions, tallied.snapshots_processed)))[0])

    def _router(self, account_id: str) -> "OptionRouter":
        return self._option_routers[account_id]

    def _settle_options(self, session: date, options: Sequence[str]) -> None:
        """Expiry, exercise and assignment, on the official prices (O2, I9)."""
        holding = [
            account_id
            for account_id in options
            if any(
                isinstance(instrument, OptionContract) and flag("eod:nonzero", (str(position.quantity),))
                for instrument, position in self._ledger.state(account_id).positions.items()
            )
        ]
        if not holding:
            return
        decide("eod:lifecycle", (str(holding),), flags=(bool(holding), self._config.lifecycle is not None))
        self._config.lifecycle.run(session, holding)

    def _finish_options_account(
        self, account_id: str, session: date, tally: "_Tally"
    ) -> AccountRunResult:
        return rs.eod_finish(self, account_id, session, (), tally, True)

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

    def _submit_new_option_entries(self, account_id: str, session: date, tally: "_Tally") -> None:
        rs.eod_entries(self, account_id, session, tally, True)

    def _credit_dividends(self, account_id: str, session: date) -> None:
        """Credit (or charge) today's ex-dividends on the shares held at the open.

        Whoever holds a share when it opens ex-dividend is owed the dividend, so the
        holding is the account as it stood before the session opened. A short holding
        pays it. The cash is booked on the ex-date, the day the price drops by it.
        """
        session_open = self._calendar.session_open(session)
        before = fold_account(
            (event for event in self._ledger.events(account=account_id)
                if flag("eod:preopen", numbers=(micros(event.ts_utc), micros(session_open)))),
            account_id,
        )
        shares = sorted(
            (
                (instrument, position)
                for instrument, position in before.positions.items()
                if isinstance(instrument, Equity) and flag("eod:nonzero", (str(position.quantity),))
            ),
            key=lambda item: item[0].symbol,
        )
        if not shares:
            return
        source = self._config.dividends
        decide("eod:dividend_source", (account_id,), flags=(source is not None,))
        now = self._clock.now_utc()
        for instrument, position in shares:
            try:
                found = source.dividends(instrument.symbol, session)
            except StaleDataError as err:
                raise ReplayDataError(f"'{account_id}' holds {instrument.symbol}: {err}") from err
            for dividend in found:
                decide("eod:dividend_stamp", (instrument.symbol, dividend.as_of.isoformat()),
                    numbers=(micros(dividend.as_of), micros(now)))
            values, _, credit = decide("eod:dividend", (str(position.quantity), *(str(dividend.amount) for dividend in found)))
            if not credit[0]:
                continue
            per_share, amount = map(Decimal, values)
            self._ledger.append(
                Event(
                    account=account_id,
                    kind=EventKind.CASH_FLOW,
                    payload=CashFlow(
                        amount=amount,
                        kind="dividend",
                        as_of=now,
                        note=(
                            f"{instrument.symbol} ex-dividend {session.isoformat()}: {per_share} "
                            f"a share on {position.quantity} held at the open"
                        ),
                    ),
                    ts_utc=now,
                    command_id=f"dividend:{account_id}:{instrument.symbol}:{session.isoformat()}",
                )
            )

    def _mark_options_account(self, account_id: str, session: date, tally: "_Tally") -> None:
        """Options at the newest snapshot's mid; shares and underlyings at the official close.

        The underlying of every option held is marked too, held or not: its option
        margin is measured against it (O3). Anything without a price refuses (I5).
        """
        state = self._ledger.state(account_id)
        marks: dict[Instrument, tuple[Decimal, str]] = {}
        underlyings: set[str] = set()
        for instrument, position in sorted(state.positions.items(), key=lambda item: item[0].symbol):
            if not flag("eod:nonzero", (str(position.quantity),)):
                continue
            if isinstance(instrument, OptionContract):
                underlying = underlying_of(instrument)
                snapshot = tally.snapshots.get(underlying)
                quote = None if snapshot is None else snapshot.get(instrument)
                decide("eod:option_mark", (str(quote.mid) if quote is not None else "0",
                    session.isoformat(), instrument.occ.strip(), account_id), flags=(quote is not None,))
                marks[instrument] = (quote.mid, f"snapshot:{snapshot.as_of.isoformat()}")
                underlyings.add(underlying)
            elif isinstance(instrument, Equity):
                marks[instrument] = self._official_close(instrument.symbol, session)
            else:
                decide("eod:unmarkable", (account_id, instrument.symbol))
        for underlying in sorted(underlyings):
            if Equity(underlying) not in marks:
                marks[Equity(underlying)] = self._official_close(underlying, session)
        now = self._clock.now_utc()
        for instrument, (price, source) in sorted(marks.items(), key=lambda item: item[0].symbol):
            command = decide("eod:command", ("eod:mark",account_id,session.isoformat(),instrument.symbol))[0][0]
            if self._ledger.event_by_command(command) is not None:
                continue
            self._ledger.append(
                Event(
                    account=account_id,
                    kind=EventKind.MARK,
                    payload=Mark(instrument=instrument, price=price, as_of=now, source=source),
                    ts_utc=now,
                    command_id=command,
                )
            )

    def _official_close(self, symbol: str, session: date) -> tuple[Decimal, str]:
        try:
            price = self._config.settlements.settlement(symbol, session, SettleTime.PM)
        except StaleDataError as err:
            raise ReplayDataError(f"No official {session.isoformat()} close for {symbol}: {err}") from err
        decide("eod:official_identity", (symbol, price.underlying, session.isoformat(), price.session.isoformat(), price.settle_time.value))
        now = self._clock.now_utc()
        decide("eod:official_future", (symbol, price.as_of.isoformat(), now.isoformat()),
            numbers=(micros(price.as_of), micros(now)))
        decide("eod:official_close", (symbol, price.as_of.isoformat()),
            numbers=(micros(price.as_of), micros(self._calendar.session_close(session))))
        return price.price, price.source

    # -- sinks -------------------------------------------------------------------

    def _drain_outbox(self) -> None:
        for destination, sink in sorted(self._config.sinks.items()):
            publisher = getattr(sink, "publish", None)
            decide("eod:sink", (destination,), flags=(callable(publisher),))
            # The outbox calls publisher(item); a JournalSink takes (event_seq, event).
            self._ledger.drain_outbox(
                destination,
                lambda item, _publish=publisher: _publish(item.event_seq, item.payload),
                self._clock,
            )
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

The runner owns orchestration only. Expiry/assignment semantics stay with O2, EOD exit
rules belong to strategy plugins (I13) and reach the venue only through the OMS, and
market data comes from injected providers (I5: no default source).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from trade_engine.calendar.sessions import ExchangeCalendar
from trade_engine.domain.exits import ClosePosition, MoveStop, OpenBracket, ReducePosition
from trade_engine.domain.instruments import Equity, Instrument, OptionContract
from trade_engine.domain.option_orders import CloseHolding, CloseStructure, OptionIntent
from trade_engine.domain.option_roots import SettleTime
from trade_engine.domain.orders import OrderState, OrderType
from trade_engine.domain.portfolio import Fill
from trade_engine.domain.risk import RiskVerdict
from trade_engine.domain.signals import OrderIntent, Signal
from trade_engine.interfaces.broker import (
    BrokerAdapter,
    VenueFill,
    VenueOrder,
    VenueOrderAllocation,
    VenuePosition,
)
from trade_engine.interfaces.clock import Clock
from trade_engine.eod.options import OptionContext
from trade_engine.interfaces.market_data import MarketData, StaleDataError
from trade_engine.ledger import CashFlow, EodRun, Event, EventKind, Ledger, Mark
from trade_engine.ledger.state import AccountState, fold_account
from trade_engine.market_data.chains import ChainSnapshot
from trade_engine.oms.manager import OrderManager
from trade_engine.oms.options import OptionOrderManager, open_structures
from trade_engine.risk import RiskContext, RiskEngine
from trade_engine.sim import SimBroker, SnapshotVenue, underlying_of

MIN_TIME = datetime.min.replace(tzinfo=timezone.utc)
NEW_YORK = ZoneInfo("America/New_York")
_MARK_COMMAND = "eod:mark:{account}:{session}:{symbol}"
_RUN_COMMAND = "eod:{job}:{account}:{session}"
# How many times a strategy may act at one snapshot: a buy-write needs two.
_SNAPSHOT_ROUNDS = 4
_TERMINAL = frozenset(
    {
        OrderState.FILLED,
        OrderState.CANCELLED,
        OrderState.EXPIRED,
        OrderState.REJECTED,
    }
)
# States in which the ledger says the venue holds the order.
_VENUE_WORKING = frozenset(
    {
        OrderState.SUBMITTED,
        OrderState.ACCEPTED,
        OrderState.PARTIALLY_FILLED,
        OrderState.PENDING_UNKNOWN,
    }
)


class EodRunnerError(RuntimeError):
    """Base class for refused EOD runs (I5)."""


class SessionIncompleteError(EodRunnerError):
    """The requested session's predecessor has not been run for every account."""


class ReplayDataError(EodRunnerError):
    """The market-data answer cannot support a deterministic session replay."""


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
        if not self.job_name:
            raise EodRunnerError("job_name must be non-empty")
        if not self.brokers:
            raise EodRunnerError("at least one broker is required")
        if (
            not isinstance(self.bars_max_age_seconds, (int, float))
            or isinstance(self.bars_max_age_seconds, bool)
            or self.bars_max_age_seconds <= 0
        ):
            raise EodRunnerError("bars_max_age_seconds must be positive")
        if not isinstance(self.settle_delay, timedelta) or self.settle_delay < timedelta(0):
            raise EodRunnerError("settle_delay must be a non-negative timedelta")


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

    # -- entry -------------------------------------------------------------------

    def run(self, session: date) -> EodRunResult:
        if not isinstance(session, date) or isinstance(session, datetime):
            raise EodRunnerError(f"session must be a date, got {type(session).__name__}")
        if not self._calendar.is_session(session):
            raise EodRunnerError(
                f"{session.isoformat()} is not a trading session of "
                f"{self._calendar.exchange} (I5)"
            )
        self._require_previous_session_complete(session)

        pending = [
            account_id
            for account_id in sorted(self._config.brokers)
            # A completed account is never re-driven through the venue: idempotency is
            # the contract, and everything this run could derive is already recorded.
            if self._ledger.event_by_command(self._run_command(account_id, session)) is None
        ]
        replays = {account_id: self._prepare_account(account_id) for account_id in pending}
        tallies = {
            account_id: _Tally(fills_before=self._fill_count(account_id))
            for account_id in pending
        }
        options = [account_id for account_id in pending if self._is_options(account_id)]
        snapshots = self._session_snapshots(session, options)
        self._replay_session(session, replays, tallies, snapshots, options)

        # The session is over before anything is marked or entered: a DAY entry for D+1
        # submitted while the clock still read 15:59 would belong to *this* session and
        # expire at the next open without ever working.
        close = self._calendar.session_close(session)
        self._advance_clock(close)
        results: dict[str, AccountRunResult] = {}
        for account_id in sorted(self._config.brokers):
            if account_id not in replays:
                results[account_id] = AccountRunResult(account_id=account_id)
            elif account_id not in options:
                results[account_id] = self._finish_account(
                    account_id, session, replays[account_id], tallies[account_id]
                )
        if options:
            # After-close work waits for the official close (I9).
            self._advance_clock(close + self._config.settle_delay)
            self._settle_options(session, options)
            for account_id in options:
                results[account_id] = self._finish_options_account(
                    account_id, session, tallies[account_id]
                )
        self._drain_outbox()
        return EodRunResult(
            session=session,
            accounts=tuple(results[account_id] for account_id in sorted(self._config.brokers)),
        )

    def _is_options(self, account_id: str) -> bool:
        return callable(getattr(self._config.brokers[account_id], "process_snapshot", None))

    def _run_command(self, account_id: str, session: date) -> str:
        return _RUN_COMMAND.format(
            job=self._config.job_name, account=account_id, session=session.isoformat()
        )

    def _require_previous_session_complete(self, session: date) -> None:
        previous = self._calendar.previous_session(session)
        for account_id in sorted(self._config.brokers):
            if self._ledger.event_by_command(self._run_command(account_id, session)) is not None:
                # This session already completed; the re-run proves idempotency itself.
                continue
            if self._account_has_history(account_id) and self._ledger.event_by_command(
                self._run_command(account_id, previous)
            ) is None:
                raise SessionIncompleteError(
                    f"Cannot run {session.isoformat()} for '{account_id}': the previous "
                    f"session {previous.isoformat()} has no {self._config.job_name} "
                    f"marker; complete it first (I3)"
                )

    def _account_has_history(self, account_id: str) -> bool:
        for event in self._ledger.events(account=account_id):
            if event.kind is EventKind.EOD_RUN:
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
    ) -> None:
        """Feed every account's bars on one timeline, minute by minute.

        One clock serves every account, and the ledger records events in the order they
        happened: instrument by instrument, a 09:31 fill in the second symbol would land
        after a 15:00 exit in the first, stamped 15:59 (I7). Each bar goes to every
        account holding its instrument, and each account reconciles immediately. A chain
        snapshot takes its place at its own ``as_of``, ahead of the bar that opens at the
        same instant, and goes to every options account.
        """
        holders: dict[Instrument, list[str]] = {}
        for account_id, instruments in replays.items():
            for instrument in instruments:
                holders.setdefault(instrument, []).append(account_id)
        if not holders and not snapshots:
            return
        session_open = self._calendar.session_open(session)
        session_close = self._calendar.session_close(session)
        if self._clock.now_utc() > session_open:
            raise EodRunnerError(
                f"Cannot replay {session.isoformat()}: the injected clock reads "
                f"{self._clock.now_utc().isoformat()}, past the session open "
                f"{session_open.isoformat()}. Replay needs a clock it can advance bar by "
                f"bar (I7); inject a replay clock positioned at or before the session open"
            )
        timeline = []
        for instrument in sorted(holders, key=lambda value: value.symbol):
            for bar, is_regular in self._load_bars(instrument, session_open, session_close):
                timeline.append((bar.timestamp, 1, instrument.symbol, bar, is_regular))
        for snapshot in snapshots:
            timeline.append((snapshot.as_of, 0, snapshot.underlying, snapshot, False))
        timeline.sort(key=lambda item: (item[0], item[1], item[2]))
        for timestamp, kind, _, item, is_regular in timeline:
            self._advance_clock(timestamp)
            if kind == 0:
                for account_id in options:
                    self._options_at_snapshot(account_id, session, item, tallies[account_id])
                continue
            bar = item
            for account_id in sorted(holders[bar.instrument]):
                broker = self._config.brokers[account_id]
                tally = tallies[account_id]
                broker.process_bar(bar)
                if is_regular:
                    tally.last_regular_closes[bar.instrument] = bar.close
                self._reconcile_after_bar(
                    account_id, broker, self._manager_for(account_id, broker), timestamp
                )
                tally.bars_processed += 1

    def _finish_account(
        self,
        account_id: str,
        session: date,
        instruments: tuple[Instrument, ...],
        tally: "_Tally",
    ) -> AccountRunResult:
        broker = self._config.brokers[account_id]
        if instruments:
            # A closing sweep with no bar: confirms every terminal state the venue
            # reached during the session (DAY expiry at the close, cancelled exits).
            self._reconcile_after_bar(
                account_id, broker, self._manager_for(account_id, broker), None
            )
        self._mark_positions(account_id, tally.last_regular_closes, session)
        exit_actions = self._manage_positions(account_id, session, tally.last_regular_closes)
        orders_submitted = self._submit_new_orders(account_id, session)
        self._append_run_marker(account_id, session, tally.bars_processed)
        return AccountRunResult(
            account_id=account_id,
            bars_processed=tally.bars_processed,
            # Counted from the ledger: the OMS records some fills itself, such as a
            # stop filled inside its entry's bar while the entry fill is recorded.
            fills_recorded=self._fill_count(account_id) - tally.fills_before,
            marks_appended=self._marks_appended(account_id, session),
            orders_submitted=orders_submitted,
            exit_actions=exit_actions,
        )

    def _fill_count(self, account_id: str) -> int:
        return sum(
            1 for event in self._ledger.events(account=account_id) if event.kind is EventKind.FILL
        )

    def _marks_appended(self, account_id: str, session: date) -> int:
        count = 0
        for event in self._ledger.events(account=account_id):
            if (
                event.kind is EventKind.MARK
                and event.command_id is not None
                and event.command_id.startswith(f"eod:mark:{account_id}:{session.isoformat()}:")
            ):
                count += 1
        return count

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
            if order.state not in _VENUE_WORKING:
                continue
            venue_id = state.venue_order_ids.get(order.order_id, order.order_id)
            found = held.get(venue_id)
            if found is None:
                raise EodRunnerError(
                    f"Venue for '{account_id}' does not hold working order "
                    f"'{order.order_id}' ({order.state.value}); refusing to replay a "
                    f"session it could never fill in (I5)"
                )
            recorded = state.filled_quantity.get(order.order_id, Decimal("0"))
            if found.filled_quantity < recorded:
                raise EodRunnerError(
                    f"Venue reports {found.filled_quantity} filled for '{order.order_id}' "
                    f"but the ledger records {recorded} (I5)"
                )

    def _restorable(
        self, account_id: str, state: AccountState
    ) -> tuple[list[tuple[VenueOrder, OrderState]], list[VenueFill]]:
        """Working orders plus their brackets: a child's cap needs its parent's fills
        and its siblings' exits. NEW orders were never sent, so they stay out."""
        wanted: set[str] = set()
        for order in state.orders.values():
            if order.state not in _VENUE_WORKING:
                continue
            if order.state is OrderState.PENDING_UNKNOWN:
                raise EodRunnerError(
                    f"Order '{order.order_id}' for '{account_id}' is PENDING_UNKNOWN; the "
                    f"simulated venue's answer is gone, reconcile it before replay (I5)"
                )
            root = order.parent_order_id or order.order_id
            wanted.add(root)
            wanted.update(
                candidate.order_id
                for candidate in state.orders.values()
                if candidate.parent_order_id == root and candidate.state is not OrderState.NEW
            )
        orders: list[tuple[VenueOrder, OrderState]] = []
        for order_id in sorted(wanted):
            order = state.orders[order_id]
            submission = self._ledger.event_by_command(f"{order.command_id}:submit")
            if submission is None:
                raise EodRunnerError(
                    f"Order '{order_id}' for '{account_id}' has no submission event; cannot "
                    f"restore when it reached the venue (I5)"
                )
            orders.append(
                (
                    VenueOrder(
                        venue_order_id=state.venue_order_ids.get(order_id, order_id),
                        instrument=order.instrument,
                        order_type=order.order_type,
                        side=order.side,
                        quantity=order.quantity,
                        submitted_at=submission.ts_utc,
                        tif=order.tif,
                        limit_price=order.limit_price,
                        stop_price=order.stop_price,
                        trail_amount=order.trail_amount,
                        allocations=(
                            VenueOrderAllocation(order_id, order.account_id, order.quantity),
                        ),
                        parent_order_id=order.parent_order_id,
                        oco_group=order.oco_group,
                    ),
                    order.state,
                )
            )
        fills = [
            VenueFill(
                venue_fill_id=fill.venue_execution_id or fill.fill_id,
                venue_order_id=state.venue_order_ids.get(fill.order_id, fill.order_id),
                instrument=fill.instrument,
                quantity=fill.quantity,
                price=fill.price,
                filled_at=fill.filled_at,
                side=fill.side,
                fee=fill.fee,
                leg_id=fill.leg_id,
            )
            for fill in state.fills
            if fill.order_id in wanted
        ]
        return orders, fills

    @staticmethod
    def _restorable_positions(state: AccountState) -> list[VenuePosition]:
        positions = []
        for instrument, position in state.positions.items():
            if position.quantity == Decimal("0"):
                continue
            # SimBroker reads only the quantity; as_of dates it by its latest fill.
            as_of = max(
                (fill.filled_at for fill in state.fills if fill.instrument == instrument),
                default=MIN_TIME,
            )
            positions.append(
                VenuePosition(
                    instrument=instrument,
                    quantity=position.quantity,
                    avg_price=position.avg_cost,
                    as_of=as_of,
                )
            )
        return positions

    def _replay_instruments(self, state: AccountState) -> tuple[Instrument, ...]:
        """Instruments the session can change: open positions and working orders.

        A finished order has nothing left to fill; replaying its symbol forever would
        grow every run and let one delisted name with no bars block the account.
        """
        instruments: set[Instrument] = {
            instrument
            for instrument, position in state.positions.items()
            if position.quantity != Decimal("0")
        }
        for order in state.orders.values():
            if order.state in _VENUE_WORKING:
                instruments.add(order.instrument)
        ordered = tuple(sorted(instruments, key=lambda value: value.symbol))
        for instrument in ordered:
            if not isinstance(instrument, Equity):
                raise EodRunnerError(
                    f"Equity EOD replay cannot value {instrument.symbol}; option marks "
                    f"and the lifecycle pass are wired into the EOD run by O4 (I5)"
                )
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
        if not raw:
            raise ReplayDataError(
                f"No one-minute bars returned for {instrument.symbol} for this session; "
                f"refusing to simulate from memory (I5)"
            )
        if raw[0].timestamp != session_open:
            raise ReplayDataError(
                f"Bar series for {instrument.symbol} starts at "
                f"{raw[0].timestamp.isoformat()}, not the session open "
                f"{session_open.isoformat()} (I5)"
            )
        if raw[-1].timestamp != session_close - timedelta(minutes=1):
            raise ReplayDataError(
                f"Bar series for {instrument.symbol} ends at "
                f"{raw[-1].timestamp.isoformat()}, not the session's final minute "
                f"{(session_close - timedelta(minutes=1)).isoformat()} (I5)"
            )
        regular = []
        for bar in raw:
            is_regular = (
                session_open <= bar.timestamp < session_close
            )
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
        """Ingest everything the venue changed at this bar, immediately.

        Reconciliation is per bar, never per session: bars simulated before an exit
        arrived are gone, and SimBroker refuses a late protective stop (the OMS then
        raises). Nothing here swallows that error; a raise fails the whole EOD run
        loudly, which is the contract the E4 guard was built for.
        """
        since = MIN_TIME if bar_timestamp is None else bar_timestamp
        recorded = 0
        for venue_fill in broker.fills(since):
            recorded += self._record_venue_fill(account_id, broker, manager, venue_fill)
        state = self._ledger.state(account_id)
        for order_state in broker.orders(since):
            order_id = order_state.venue_order_id
            if order_id not in state.orders:
                continue
            ledger_state = state.orders[order_id].state
            if ledger_state is OrderState.NEW or ledger_state in _TERMINAL:
                # NEW children were never sent; terminal states were already folded.
                continue
            manager.reconcile_order(order_id)
        return recorded

    def _record_venue_fill(
        self,
        account_id: str,
        broker: BrokerAdapter,
        manager: OrderManager,
        venue_fill: VenueFill,
    ) -> int:
        if self._ledger.has_command(f"fill:{venue_fill.venue_fill_id}"):
            return 0
        state = self._ledger.state(account_id)
        if venue_fill.venue_order_id not in state.orders:
            raise EodRunnerError(
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
        self._enqueue_journal_outbox(account_id, fill)
        return 1

    def _manager_for(self, account_id: str, broker: BrokerAdapter) -> OrderManager:
        if self._is_options(account_id):
            return self._option_manager(account_id).orders
        manager = self._managers.get(account_id)
        if manager is None:
            manager = OrderManager(broker, self._clock, self._ledger)
            self._managers[account_id] = manager
        return manager

    def _enqueue_journal_outbox(self, account_id: str, fill: Fill) -> None:
        journal_account = self._config.journal_accounts.get(account_id)
        if journal_account is None:
            return
        state = self._ledger.state(account_id)
        order = state.orders[fill.order_id]
        entry = state.orders[order.parent_order_id] if order.parent_order_id else order
        children = [
            candidate
            for candidate in state.orders.values()
            if candidate.parent_order_id == entry.order_id
        ]
        stop = next((child for child in children if child.order_type is OrderType.STOP), None)
        target = next((child for child in children if child.order_type is OrderType.LIMIT), None)
        event = self._ledger.event_by_command(f"fill:{fill.fill_id}")
        if event is None or event.seq is None:
            raise EodRunnerError(
                f"Fill '{fill.fill_id}' was recorded but its ledger event is missing (I1)"
            )
        self._ledger.enqueue_outbox(
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
            created_at=self._clock.now_utc(),
        )

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
            if position.quantity == Decimal("0"):
                continue
            mark_command = _MARK_COMMAND.format(
                account=account_id,
                session=session.isoformat(),
                symbol=instrument.symbol,
            )
            if self._ledger.event_by_command(mark_command) is not None:
                continue
            close = last_regular_closes.get(instrument)
            if close is None:
                raise ReplayDataError(
                    f"No regular-session bar for open position {instrument.symbol} in "
                    f"'{account_id}'; refusing to mark without one (I5)"
                )
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
        self, account_id: str, session: date, bars_processed: int
    ) -> None:
        now = self._clock.now_utc()
        self._ledger.append(
            Event(
                account=account_id,
                kind=EventKind.EOD_RUN,
                payload=EodRun(
                    session=session,
                    job=self._config.job_name,
                    account_id=account_id,
                    bars_processed=bars_processed,
                    at_close=now,
                ),
                ts_utc=now,
                command_id=self._run_command(account_id, session),
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
        strategy = self._config.strategies.get(account_id)
        manage = getattr(strategy, "manage_positions", None)
        if not callable(manage):
            return 0
        brackets = self._open_brackets(account_id, session, last_regular_closes)
        if not brackets:
            return 0
        actions = list(manage(brackets, {"session": session, "account_id": account_id}))
        by_entry = {bracket.entry_order_id: bracket for bracket in brackets}
        manager = self._manager_for(account_id, self._config.brokers[account_id])
        applied = 0
        for action in actions:
            if action.entry_order_id not in by_entry:
                raise EodRunnerError(
                    f"Exit action '{action.command_id}' names '{action.entry_order_id}', "
                    f"which is not an open bracket of '{account_id}' (I8)"
                )
            if isinstance(action, MoveStop):
                manager.move_stop(
                    action.entry_order_id, action.stop_price, command_id=action.command_id
                )
            elif isinstance(action, ClosePosition):
                order = manager.close_bracket(
                    action.entry_order_id, command_id=action.command_id, reason=action.reason
                )
                if order.state not in _TERMINAL:
                    manager.reconcile_order(order.order_id)
            elif isinstance(action, ReducePosition):
                order = manager.reduce_bracket(
                    action.entry_order_id,
                    action.fraction,
                    command_id=action.command_id,
                    reason=action.reason,
                )
                if order.state not in _TERMINAL:
                    manager.reconcile_order(order.order_id)
            else:
                raise EodRunnerError(
                    f"Strategy for '{account_id}' returned {type(action).__name__}; exit "
                    "actions are MoveStop, ClosePosition or ReducePosition"
                )
            applied += 1
        return applied

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
            children = [
                order for order in state.orders.values() if order.parent_order_id == entry.order_id
            ]
            stop = next((order for order in children if order.order_type is OrderType.STOP), None)
            entry_fills = [fill for fill in state.fills if fill.order_id == entry.order_id]
            if stop is None or stop.stop_price is None or not entry_fills:
                continue
            filled = sum((fill.quantity for fill in entry_fills), Decimal("0"))
            exited = sum(
                (state.filled_quantity.get(order.order_id, Decimal("0")) for order in children),
                Decimal("0"),
            )
            if filled - exited <= 0:
                continue
            targets = [order for order in children if order.order_type is OrderType.LIMIT]
            first_fill = min(fill.filled_at for fill in entry_fills)
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
                    open_quantity=filled - exited,
                    average_entry_price=sum(
                        (fill.price * fill.quantity for fill in entry_fills), Decimal("0")
                    )
                    / filled,
                    entry_filled_at=first_fill,
                    entry_session=entry_session,
                    sessions_held=len(self._calendar.sessions_in_range(entry_session, session))
                    - 1,
                    stop_price=stop.stop_price,
                    targets_filled=sum(
                        1 for order in targets if order.state is OrderState.FILLED
                    ),
                    open_targets=tuple(
                        (
                            order.limit_price,
                            order.quantity
                            - state.filled_quantity.get(order.order_id, Decimal("0")),
                        )
                        for order in targets
                        if order.state not in _TERMINAL and order.limit_price is not None
                    ),
                    last_close=last_regular_closes.get(entry.instrument),
                )
            )
        return brackets

    # -- entries for D+1 ---------------------------------------------------------

    def _submit_new_orders(self, account_id: str, session: date) -> int:
        adapter = self._config.signal_adapters.get(account_id)
        strategy = self._config.strategies.get(account_id)
        risk_engine = self._config.risk_engines.get(account_id)
        if adapter is None or strategy is None or risk_engine is None:
            return 0
        broker = self._config.brokers[account_id]
        manager = self._manager_for(account_id, broker)
        signals = adapter.read_signals(session)
        for signal in signals:
            self._record_signal(account_id, signal)
        intents = strategy.generate_intents(
            signals, {"session": session, "account_id": account_id}
        )
        submitted = 0
        for intent in intents:
            if intent.account_id != account_id:
                raise EodRunnerError(
                    f"Intent '{intent.intent_id}' targets account '{intent.account_id}' "
                    f"but was produced for '{account_id}' (I8)"
                )
            state = self._ledger.state(account_id)
            builder = self._config.context_builder
            context = (
                builder(intent, state, session)
                if builder is not None
                else self._derive_risk_context(intent, state)
            )
            verdict = risk_engine.evaluate(intent, context)
            self._record_verdict(account_id, intent, verdict)
            if not verdict.accepted or verdict.approved_quantity is None:
                continue
            bracket = manager.create_bracket(intent, verdict.approved_quantity)
            submitted_entry = manager.submit(bracket.entry)
            if submitted_entry.state not in _TERMINAL:
                # Confirm the venue ack in the same run, so a re-run derives the same
                # reconcile command and changes nothing the second time.
                manager.reconcile_order(bracket.entry.order_id)
            submitted += 1
        return submitted

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
        marks_value = sum(
            (
                position.quantity * state.marks[position.instrument]
                for position in state.positions.values()
                if position.instrument in state.marks
            ),
            Decimal("0"),
        )
        equity = state.cash + marks_value
        gross = sum(
            (
                abs(position.quantity) * state.marks.get(position.instrument, Decimal("0"))
                for position in state.positions.values()
            ),
            Decimal("0"),
        )
        open_positions = [
            position for position in state.positions.values() if position.quantity != Decimal("0")
        ]
        held = state.positions.get(intent.instrument)
        return RiskContext(
            equity=equity if equity > 0 else None,
            current_price=None,
            gross_exposure=gross,
            portfolio_heat=None,
            open_positions=len(open_positions),
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
            current_position_quantity=int(held.quantity) if held is not None else 0,
        )

    # -- options accounts (O4) ---------------------------------------------------

    def _option_manager(self, account_id: str) -> OptionOrderManager:
        manager = self._option_managers.get(account_id)
        if manager is None:
            manager = OptionOrderManager(self._config.brokers[account_id], self._clock, self._ledger)
            self._option_managers[account_id] = manager
        return manager

    def _session_snapshots(self, session: date, options: Sequence[str]) -> list[ChainSnapshot]:
        """The session's chain snapshots, checked before anything is replayed (I5).

        Each must lie inside the session. Every underlying an options account holds an
        option on or has an order working on needs one: without it nothing could fill
        or be marked, and a guess is not a mark.
        """
        if not options:
            return []
        if self._config.chain_snapshots is None or self._config.settlements is None:
            raise EodRunnerError(
                f"Options accounts {list(options)} need chain_snapshots to fill and mark "
                f"options and settlements to mark shares; neither may be defaulted (I5)"
            )
        session_open = self._calendar.session_open(session)
        session_close = self._calendar.session_close(session)
        snapshots = sorted(
            self._config.chain_snapshots(session), key=lambda item: (item.as_of, item.underlying)
        )
        for snapshot in snapshots:
            if not isinstance(snapshot, ChainSnapshot):
                raise EodRunnerError(f"chain_snapshots returned {type(snapshot).__name__} (I5)")
            if not session_open <= snapshot.as_of <= session_close:
                raise ReplayDataError(
                    f"{snapshot.underlying} snapshot of {snapshot.as_of.isoformat()} is outside "
                    f"the {session.isoformat()} session (I7)"
                )
        have = {snapshot.underlying for snapshot in snapshots}
        for account_id in options:
            missing = sorted(self._option_underlyings(self._ledger.state(account_id)) - have)
            if missing:
                raise ReplayDataError(
                    f"No {session.isoformat()} chain snapshot for {', '.join(missing)}, which "
                    f"'{account_id}' holds options on or has orders working on; refusing to "
                    f"fill or mark them from memory (I5)"
                )
        return snapshots

    @staticmethod
    def _option_underlyings(state: AccountState) -> set[str]:
        needed = {
            underlying_of(instrument)
            for instrument, position in state.positions.items()
            if isinstance(instrument, OptionContract) and position.quantity != 0
        }
        needed |= {
            underlying_of(order.instrument)
            for order in state.orders.values()
            if order.state in _VENUE_WORKING
        }
        return needed

    def _options_at_snapshot(
        self, account_id: str, session: date, snapshot: ChainSnapshot, tally: "_Tally"
    ) -> None:
        """Match the account's working orders, then let the strategy act on these quotes.

        What the strategy returns trades on the same snapshot, and the strategy is asked
        again once it has: a buy-write buys the shares in one round and writes the call on
        them in the next, both on the quotes it was decided on. It stops when a round
        brings nothing new; a strategy still acting after ``_SNAPSHOT_ROUNDS`` refuses.
        """
        manager = self._option_manager(account_id)
        self._match_snapshot(account_id, manager, snapshot, session)
        tally.snapshots_processed += 1
        tally.snapshots[snapshot.underlying] = snapshot
        manage = getattr(self._config.strategies.get(account_id), "manage_options", None)
        if not callable(manage):
            return
        for _ in range(_SNAPSHOT_ROUNDS):
            actions = list(manage(self._option_context(account_id, session, tally, snapshot)))
            if all(self._taken(action) for action in actions):
                return  # nothing new: every action is one already taken (I3)
            self._apply_option_actions(account_id, session, actions, tally, snapshot)
            # Decided on these quotes, so traded on them.
            self._match_snapshot(account_id, manager, snapshot, session)
        raise EodRunnerError(
            f"The strategy for '{account_id}' was still acting at the {snapshot.underlying} "
            f"snapshot after {_SNAPSHOT_ROUNDS} rounds; refusing to loop on it"
        )

    def _taken(self, action: Any) -> bool:
        """Whether an action was already routed: ordered, or (an entry) judged by risk."""
        command_id = getattr(action, "command_id", None)
        if not command_id:
            return False
        if self._ledger.has_command(command_id):
            return True
        return isinstance(action, OptionIntent) and self._ledger.has_command(f"risk:{command_id}")

    def _match_snapshot(
        self, account_id: str, manager: OptionOrderManager, snapshot: ChainSnapshot, session: date
    ) -> None:
        broker = self._config.brokers[account_id]
        broker.process_snapshot(snapshot)
        self._reconcile_after_bar(account_id, broker, manager.orders, snapshot.as_of)
        manager.sync(
            account_id,
            f"eod:{self._config.job_name}:{account_id}:{session.isoformat()}:{snapshot.underlying}",
        )

    def _settle_options(self, session: date, options: Sequence[str]) -> None:
        """Expiry, exercise and assignment, on the official prices (O2, I9)."""
        holding = [
            account_id
            for account_id in options
            if any(
                isinstance(instrument, OptionContract) and position.quantity != 0
                for instrument, position in self._ledger.state(account_id).positions.items()
            )
        ]
        if not holding:
            return
        if self._config.lifecycle is None:
            raise EodRunnerError(
                f"{holding} hold options and no lifecycle pass is configured; expiry and "
                f"assignment cannot be decided (I9)"
            )
        self._config.lifecycle.run(session, holding)

    def _finish_options_account(
        self, account_id: str, session: date, tally: "_Tally"
    ) -> AccountRunResult:
        broker = self._config.brokers[account_id]
        manager = self._option_manager(account_id)
        # A closing sweep: DAY orders lapsed at the close, settled structures' exits.
        self._reconcile_after_bar(account_id, broker, manager.orders, None)
        manager.sync(
            account_id, f"eod:{self._config.job_name}:{account_id}:{session.isoformat()}:settled"
        )
        self._credit_dividends(account_id, session)
        self._mark_options_account(account_id, session, tally)
        manage = getattr(self._config.strategies.get(account_id), "manage_options", None)
        if callable(manage):
            actions = list(manage(self._option_context(account_id, session, tally)))
            self._apply_option_actions(account_id, session, actions, tally, None)
        self._submit_new_option_entries(account_id, session, tally)
        self._append_run_marker(account_id, session, tally.bars_processed)
        return AccountRunResult(
            account_id=account_id,
            fills_recorded=self._fill_count(account_id) - tally.fills_before,
            marks_appended=self._marks_appended(account_id, session),
            orders_submitted=tally.orders_submitted,
            exit_actions=tally.exit_actions,
            snapshots_processed=tally.snapshots_processed,
        )

    def _option_context(
        self,
        account_id: str,
        session: date,
        tally: "_Tally",
        snapshot: ChainSnapshot | None = None,
    ) -> OptionContext:
        state = self._ledger.state(account_id)
        return OptionContext(
            session=session,
            account_id=account_id,
            phase="close" if snapshot is None else "snapshot",
            now=self._clock.now_utc(),
            state=state,
            structures=open_structures(state),
            snapshot=snapshot,
            snapshots=dict(tally.snapshots),
        )

    def _apply_option_actions(
        self,
        account_id: str,
        session: date,
        actions: Iterable[Any],
        tally: "_Tally",
        snapshot: ChainSnapshot | None,
    ) -> None:
        """Route a strategy's options actions through the risk layer and the OMS.

        At a snapshot, an action must concern that snapshot's underlying: it is matched
        against those quotes at once, and any other underlying's would not be the ones
        it was decided on. A refused guard (C3, C4, C5) fails the run loudly.
        """
        manager = self._option_manager(account_id)
        for action in actions:
            if snapshot is not None and self._action_underlying(account_id, action) != snapshot.underlying:
                raise EodRunnerError(
                    f"'{account_id}' returned {type(action).__name__} "
                    f"'{getattr(action, 'command_id', '?')}' at the {snapshot.underlying} "
                    f"snapshot for another underlying"
                )
            if isinstance(action, OptionIntent):
                tally.orders_submitted += self._enter_option(account_id, session, action, tally, snapshot)
            elif isinstance(action, CloseStructure):
                manager.close(account_id, action)
                tally.exit_actions += 1
            elif isinstance(action, CloseHolding):
                manager.close_holding(account_id, action)
                tally.exit_actions += 1
            else:
                raise EodRunnerError(
                    f"Strategy for '{account_id}' returned {type(action).__name__}; options "
                    "actions are OptionIntent, CloseStructure or CloseHolding"
                )

    def _action_underlying(self, account_id: str, action: Any) -> str:
        if isinstance(action, OptionIntent):
            return underlying_of(action.instrument)
        if isinstance(action, CloseHolding):
            return action.instrument.symbol
        if isinstance(action, CloseStructure):
            entry = self._ledger.state(account_id).orders.get(action.entry_order_id)
            if entry is None:
                raise EodRunnerError(
                    f"Close '{action.command_id}' names '{action.entry_order_id}', which is not "
                    f"an order of '{account_id}' (I8)"
                )
            return underlying_of(entry.instrument)
        raise EodRunnerError(f"Unknown options action {type(action).__name__}")

    def _enter_option(
        self,
        account_id: str,
        session: date,
        intent: Any,
        tally: "_Tally",
        snapshot: ChainSnapshot | None,
    ) -> int:
        if not isinstance(intent, OptionIntent):
            raise EodRunnerError(
                f"Options account '{account_id}' was handed {type(intent).__name__}; it enters "
                "with OptionIntent"
            )
        if intent.account_id != account_id:
            raise EodRunnerError(
                f"Intent '{intent.intent_id}' targets account '{intent.account_id}' but was "
                f"produced for '{account_id}' (I8)"
            )
        engine = self._config.option_risk_engines.get(account_id)
        if engine is None:
            raise EodRunnerError(
                f"'{account_id}' asked to enter '{intent.intent_id}' and has no options risk "
                f"engine; nothing enters unchecked (I5)"
            )
        verdict = engine.evaluate(intent, self._option_context(account_id, session, tally, snapshot))
        self._record_verdict(account_id, intent, verdict)
        if not verdict.accepted:
            return 0
        if verdict.approved_quantity is not None and verdict.approved_quantity != intent.quantity:
            intent = replace(intent, quantity=verdict.approved_quantity)
        self._option_manager(account_id).open(intent)
        return 1

    def _submit_new_option_entries(self, account_id: str, session: date, tally: "_Tally") -> None:
        adapter = self._config.signal_adapters.get(account_id)
        strategy = self._config.strategies.get(account_id)
        if adapter is None or strategy is None:
            return
        signals = adapter.read_signals(session)
        for signal in signals:
            self._record_signal(account_id, signal)
        for intent in strategy.generate_intents(signals, self._option_context(account_id, session, tally)):
            tally.orders_submitted += self._enter_option(account_id, session, intent, tally, None)

    def _credit_dividends(self, account_id: str, session: date) -> None:
        """Credit (or charge) today's ex-dividends on the shares held at the open.

        Whoever holds a share when it opens ex-dividend is owed the dividend, so the
        holding is the account as it stood before the session opened. A short holding
        pays it. The cash is booked on the ex-date, the day the price drops by it.
        """
        session_open = self._calendar.session_open(session)
        before = fold_account(
            (event for event in self._ledger.events(account=account_id) if event.ts_utc < session_open),
            account_id,
        )
        shares = sorted(
            (
                (instrument, position)
                for instrument, position in before.positions.items()
                if isinstance(instrument, Equity) and position.quantity != 0
            ),
            key=lambda item: item[0].symbol,
        )
        if not shares:
            return
        source = self._config.dividends
        if source is None:
            raise EodRunnerError(
                f"'{account_id}' holds shares and no dividend source is configured; a dividend "
                f"going ex would be missed (I5)"
            )
        now = self._clock.now_utc()
        for instrument, position in shares:
            try:
                found = source.dividends(instrument.symbol, session)
            except StaleDataError as err:
                raise ReplayDataError(f"'{account_id}' holds {instrument.symbol}: {err}") from err
            for dividend in found:
                if dividend.as_of > now:
                    raise ReplayDataError(
                        f"{instrument.symbol} dividend record is stamped {dividend.as_of.isoformat()}, "
                        f"after the clock: look-ahead (I7)"
                    )
            per_share = sum((dividend.amount for dividend in found), Decimal("0"))
            if per_share == 0:
                continue
            self._ledger.append(
                Event(
                    account=account_id,
                    kind=EventKind.CASH_FLOW,
                    payload=CashFlow(
                        amount=position.quantity * per_share,
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
            if position.quantity == 0:
                continue
            if isinstance(instrument, OptionContract):
                underlying = underlying_of(instrument)
                snapshot = tally.snapshots.get(underlying)
                quote = None if snapshot is None else snapshot.get(instrument)
                if quote is None or quote.mid <= 0:
                    raise ReplayDataError(
                        f"No usable {session.isoformat()} quote for {instrument.occ.strip()} in "
                        f"'{account_id}'; refusing to mark it (I5)"
                    )
                marks[instrument] = (quote.mid, f"snapshot:{snapshot.as_of.isoformat()}")
                underlyings.add(underlying)
            elif isinstance(instrument, Equity):
                marks[instrument] = self._official_close(instrument.symbol, session)
            else:
                raise EodRunnerError(f"'{account_id}' holds {instrument.symbol}, which cannot be marked (I6)")
        for underlying in sorted(underlyings):
            if Equity(underlying) not in marks:
                marks[Equity(underlying)] = self._official_close(underlying, session)
        now = self._clock.now_utc()
        for instrument, (price, source) in sorted(marks.items(), key=lambda item: item[0].symbol):
            command = _MARK_COMMAND.format(
                account=account_id, session=session.isoformat(), symbol=instrument.symbol
            )
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
        if (price.underlying, price.session, price.settle_time) != (symbol, session, SettleTime.PM):
            raise ReplayDataError(
                f"Asked for the {session.isoformat()} close of {symbol}, got {price.underlying} "
                f"{price.settle_time.value} on {price.session} (I5)"
            )
        now = self._clock.now_utc()
        if price.as_of > now:
            raise ReplayDataError(
                f"{symbol} close is stamped {price.as_of.isoformat()}, after the clock "
                f"{now.isoformat()}: look-ahead (I7)"
            )
        if price.as_of < self._calendar.session_close(session):
            raise ReplayDataError(
                f"{symbol} close is stamped {price.as_of.isoformat()}, before the session "
                f"closed; it cannot be the official close (I9)"
            )
        return price.price, price.source

    # -- sinks -------------------------------------------------------------------

    def _drain_outbox(self) -> None:
        for destination, sink in sorted(self._config.sinks.items()):
            publisher = getattr(sink, "publish", None)
            if not callable(publisher):
                raise EodRunnerError(
                    f"Sink for '{destination}' does not declare a publish method (I5)"
                )
            # The outbox calls publisher(item); a JournalSink takes (event_seq, event).
            self._ledger.drain_outbox(
                destination,
                lambda item, _publish=publisher: _publish(item.event_seq, item.payload),
                self._clock,
            )
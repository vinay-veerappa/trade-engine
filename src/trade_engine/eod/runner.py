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

The runner owns orchestration only. Expiry/assignment semantics stay with O2 (options
are refused here), EOD exit rules belong to strategy plugins (I13) and reach the venue only through the
OMS, and market data
comes from an injected provider (I5: no default source).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from trade_engine.calendar.sessions import ExchangeCalendar
from trade_engine.domain.exits import ClosePosition, MoveStop, OpenBracket, ReducePosition
from trade_engine.domain.instruments import Equity, Instrument
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
from trade_engine.interfaces.market_data import MarketData
from trade_engine.ledger import EodRun, Event, EventKind, Ledger, Mark
from trade_engine.ledger.state import AccountState
from trade_engine.oms.manager import OrderManager
from trade_engine.risk import RiskContext, RiskEngine
from trade_engine.sim import SimBroker

MIN_TIME = datetime.min.replace(tzinfo=timezone.utc)
NEW_YORK = ZoneInfo("America/New_York")
_MARK_COMMAND = "eod:mark:{account}:{session}:{symbol}"
_RUN_COMMAND = "eod:{job}:{account}:{session}"
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

    @property
    def normalized(self) -> "EodRunnerConfig":
        return EodRunnerConfig(
            job_name=self.job_name,
            brokers=self.brokers,
            risk_engines=_normalize(self.risk_engines),
            signal_adapters=_normalize(self.signal_adapters),
            strategies=_normalize(self.strategies),
            context_builder=self.context_builder,
            sinks=_normalize(self.sinks),
            journal_accounts=_normalize(self.journal_accounts),
            bars_max_age_seconds=self.bars_max_age_seconds,
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


@dataclass(frozen=True)
class AccountRunResult:
    account_id: str
    bars_processed: int = 0
    fills_recorded: int = 0
    marks_appended: int = 0
    orders_submitted: int = 0
    exit_actions: int = 0


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
        self._replay_session(session, replays, tallies)

        # The session is over before anything is marked or entered: a DAY entry for D+1
        # submitted while the clock still read 15:59 would belong to *this* session and
        # expire at the next open without ever working.
        self._advance_clock(self._calendar.session_close(session))
        results: list[AccountRunResult] = []
        for account_id in sorted(self._config.brokers):
            if account_id not in replays:
                results.append(AccountRunResult(account_id=account_id))
                continue
            results.append(
                self._finish_account(account_id, session, replays[account_id], tallies[account_id])
            )
        self._drain_outbox()
        return EodRunResult(session=session, accounts=tuple(results))

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
        instruments = self._replay_instruments(state)
        self._rehydrate_venue(account_id, broker, state)
        return instruments

    def _replay_session(
        self,
        session: date,
        replays: Mapping[str, tuple[Instrument, ...]],
        tallies: Mapping[str, "_Tally"],
    ) -> None:
        """Feed every account's bars on one timeline, minute by minute.

        One clock serves every account, and the ledger records events in the order they
        happened: instrument by instrument, a 09:31 fill in the second symbol would land
        after a 15:00 exit in the first, stamped 15:59 (I7). Each bar goes to every
        account holding its instrument, and each account reconciles immediately.
        """
        holders: dict[Instrument, list[str]] = {}
        for account_id, instruments in replays.items():
            for instrument in instruments:
                holders.setdefault(instrument, []).append(account_id)
        if not holders:
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
                timeline.append((bar.timestamp, instrument.symbol, bar, is_regular))
        timeline.sort(key=lambda item: (item[0], item[1]))
        for timestamp, _, bar, is_regular in timeline:
            self._advance_clock(timestamp)
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
            isinstance(broker, SimBroker)
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
                    f"Equity EOD replay cannot value {instrument.symbol}; options "
                    f"lifecycle is O2's (I5)"
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
        )
        manager.record_fill(fill)
        self._enqueue_journal_outbox(account_id, fill)
        return 1

    def _manager_for(self, account_id: str, broker: BrokerAdapter) -> OrderManager:
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
                "asset_class": "equity",
                "multiplier": 1,
                "stop_loss": str(stop.stop_price) if stop is not None else None,
                "profit_target": str(target.limit_price) if target is not None else None,
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
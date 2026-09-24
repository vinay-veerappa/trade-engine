"""End-of-day runner (E7, Architecture §4.9).

The 17:45 ET job per account and session, in order:

1. Gate: a session whose previous session has no completed run for the account
   refuses (bootstrap: an account's first run has no previous marker to demand).
2. Replay: one-minute bars for every instrument the account orders or holds, fed to
   the venue one bar at a time. After **every** bar the runner reconciles every order
   the venue touched that bar — fills recorded, states read back, brackets
   synchronized. SimBroker rejects a protective stop submitted after later bars were
   simulated, and the OMS raises; the runner never batches reconciliation to the end
   of the session, because the bars in between are gone.
3. MTM: one Mark per open position at the last regular bar's close; a missing
   regular bar refuses (I5).
4. Marker: one ``EodRun`` event per account, claimed by
   ``eod:<job>:<account>:<session>``. A re-run replays deterministically and every
   command id it derives is already claimed, so the second run appends nothing.
5. Entries for D+1: discovered signals and strategies produce intents; the risk layer
   evaluates each; approved intents become brackets whose DAY entry works the next
   session.
6. Outbox: every configured destination drains in order (I12).

The runner owns orchestration only. Expiry/assignment semantics stay with O2 (options
are refused here), EOD exit rules belong to strategy plugins (I13), and market data
comes from an injected provider (I5: no default source).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from trade_engine.calendar.sessions import ExchangeCalendar
from trade_engine.domain.instruments import Equity, Instrument
from trade_engine.domain.orders import OrderState, OrderType
from trade_engine.domain.portfolio import Fill
from trade_engine.domain.risk import RiskVerdict
from trade_engine.domain.signals import OrderIntent, Signal
from trade_engine.interfaces.broker import BrokerAdapter, VenueFill
from trade_engine.interfaces.clock import Clock
from trade_engine.interfaces.market_data import MarketData
from trade_engine.ledger import EodRun, Event, EventKind, Ledger, Mark
from trade_engine.ledger.state import AccountState
from trade_engine.oms.manager import OrderManager
from trade_engine.risk import RiskContext, RiskEngine

MIN_TIME = datetime.min.replace(tzinfo=timezone.utc)
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


@dataclass(frozen=True)
class EodRunResult:
    session: date
    accounts: tuple[AccountRunResult, ...]


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

        results: list[AccountRunResult] = []
        for account_id in sorted(self._config.brokers):
            results.append(self._run_account(account_id, session))
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

    def _run_account(self, account_id: str, session: date) -> AccountRunResult:
        if self._ledger.event_by_command(self._run_command(account_id, session)) is not None:
            # This session already completed for this account; re-driving the venue
            # is not the contract — idempotency is. Everything this run could derive
            # is already in the ledger and every command below replays as a no-op.
            return AccountRunResult(account_id=account_id)

        broker = self._config.brokers[account_id]
        broker.connect()
        state = self._ledger.state(account_id)
        instruments = self._replay_instruments(state)

        bars_processed = 0
        fills_recorded = 0
        last_regular_closes: dict[Instrument, Decimal] = {}
        session_open = self._calendar.session_open(session)
        session_close = self._calendar.session_close(session)
        if instruments and self._clock.now_utc() > session_open:
            raise EodRunnerError(
                f"Cannot replay {session.isoformat()} for '{account_id}': the injected "
                f"clock reads {self._clock.now_utc().isoformat()}, past the session "
                f"open {session_open.isoformat()}. Replay needs a clock it can advance "
                f"bar by bar (I7); inject a replay clock positioned at or before the "
                f"session open"
            )
        for instrument in instruments:
            manager = self._manager_for(account_id, broker)
            replay = self._load_bars(instrument, session_open, session_close)
            for bar, is_regular in replay:
                self._advance_clock(bar.timestamp)
                broker.process_bar(bar)
                if is_regular:
                    last_regular_closes[instrument] = bar.close
                fills_recorded += self._reconcile_after_bar(
                    account_id, broker, manager, bar.timestamp
                )
                bars_processed += 1
            # A closing sweep with no bar: confirms every terminal state the venue
            # reached during the session (DAY expiry at the close, cancelled exits).
            fills_recorded += self._reconcile_after_bar(account_id, broker, manager, None)

        self._mark_positions(account_id, last_regular_closes, session)
        orders_submitted = self._submit_new_orders(account_id, session)
        self._append_run_marker(account_id, session, bars_processed)
        return AccountRunResult(
            account_id=account_id,
            bars_processed=bars_processed,
            fills_recorded=fills_recorded,
            marks_appended=self._marks_appended(account_id, session),
            orders_submitted=orders_submitted,
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

    def _replay_instruments(self, state: AccountState) -> tuple[Instrument, ...]:
        instruments: set[Instrument] = set(state.positions)
        for order in state.orders.values():
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
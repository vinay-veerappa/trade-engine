"""EOD runner acceptance tests (E7, Architecture Â§4.9).

The plan's acceptance criteria:
- running the same session twice changes nothing the second time;
- running a session before the previous one is complete refuses;
- reconcile after every bar: the runner reconciles each bar's venue changes
  immediately, and SimBroker's late-exit guard (E4) stays loud if it does not.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from trade_engine.calendar.sessions import ExchangeCalendar
from trade_engine.clock.replay import ReplayClock


class SettableClock(ReplayClock):
    """Replay clock with a set() for test seeding."""

    def set(self, now: datetime) -> None:
        if now < self._current_time:
            raise ValueError("cannot move the clock backwards")
        self._current_time = now.astimezone(UTC)
from trade_engine.domain.instruments import Equity, Side
from trade_engine.domain.orders import Order, OrderState, OrderType, TimeInForce
from trade_engine.domain.portfolio import Fill
from trade_engine.domain.risk import RiskRuleResult, RiskVerdict
from trade_engine.domain.signals import OrderIntent, Signal
from trade_engine.eod import EodRunner, EodRunnerConfig, SessionIncompleteError
from trade_engine.eod.runner import EodRunnerError, ReplayDataError
from trade_engine.interfaces.broker import VenueOrder, VenueOrderAllocation
from trade_engine.interfaces.market_data import Bar
from trade_engine.ledger import EodRun, Event, EventKind, Ledger
from trade_engine.oms.manager import OrderManager
from trade_engine.risk import AccountRiskRules, RiskContext, RiskEngine, VenueRiskRails
from trade_engine.sim import SimBroker

UTC = timezone.utc
SESSION = date(2026, 9, 23)
NEXT_SESSION = date(2026, 9, 24)
PREV_SESSION = date(2026, 9, 22)
ACCOUNT = "scan-account"
SECOND_ACCOUNT = "other-account"
INSTRUMENT = Equity("AAPL")
SESSION_OPEN = datetime(2026, 9, 23, 13, 30, tzinfo=UTC)
SESSION_CLOSE = SESSION_OPEN + timedelta(minutes=390)
CALENDAR = ExchangeCalendar()
EOD_CLOCK = datetime(2026, 9, 23, 21, 45, tzinfo=UTC)  # 17:45 ET
PREV_EOD = datetime(2026, 9, 22, 21, 45, tzinfo=UTC)


def _bar(
    timestamp: datetime,
    *,
    open_: str = "100",
    high: str = "101",
    low: str = "99",
    close: str = "100",
) -> Bar:
    return Bar(
        instrument=INSTRUMENT,
        timestamp=timestamp,
        open=Decimal(open_),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=Decimal("10000"),
        as_of=timestamp,
    )


class FakeMarketData:
    """Bar provider: default flat 100s, overridden per minute index by a script."""

    def __init__(self, script: dict[int, tuple[str, str, str, str]] | None = None) -> None:
        self._script = dict(script or {})

    def bars(self, instrument, tf, start, end, max_age_seconds):
        count = int((end - start).total_seconds() // 60)
        return [
            _bar_for(instrument, start + timedelta(minutes=index), *self._script.get(
                int((start + timedelta(minutes=index) - SESSION_OPEN).total_seconds() // 60),
                ("100", "101", "99", "100"),
            ))
            for index in range(count)
        ]


def _bar_for(instrument, timestamp, open_, high, low, close) -> Bar:
    return Bar(
        instrument=instrument,
        timestamp=timestamp,
        open=Decimal(open_),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=Decimal("10000"),
        as_of=timestamp,
    )


class FixedRiskEngine:
    """Risk engine stand-in that approves every intent with a fixed quantity."""

    name = "fixed-risk"

    def __init__(self, quantity: Decimal = Decimal("2")) -> None:
        self._quantity = quantity

    def evaluate(self, intent: OrderIntent, context: RiskContext) -> RiskVerdict:
        return RiskVerdict(
            order_intent_id=intent.intent_id,
            evaluations=(
                RiskRuleResult(
                    "fixed_test_rule",
                    True,
                    "approved",
                    "approved",
                    "Test rule approves every intent",
                ),
            ),
            refusal_reasons=(),
            approved_quantity=self._quantity,
        )


class DeterministicStrategy:
    name = "deterministic"

    def __init__(self, instrument: Equity) -> None:
        self._instrument = instrument

    def generate_intents(self, signals, context) -> list[OrderIntent]:
        intents = []
        for signal in signals:
            intents.append(
                OrderIntent(
                    intent_id=f"intent-{signal.signal_id}",
                    account_id=context["account_id"],
                    instrument=self._instrument,
                    side=Side.BUY,
                    quantity_rule="fixed_2",
                    entry_price=Decimal("100"),
                    stop_loss=Decimal("95"),
                    profit_targets=(Decimal("110"),),
                    reason=f"D+1 entry for {signal.symbol}",
                    command_id=f"d1:{signal.signal_id}",
                )
            )
        return intents


class SessionSignalAdapter:
    name = "session-signals"

    def __init__(self, signals: list[Signal]) -> None:
        self._signals = signals

    def read_signals(self, session_date) -> list[Signal]:
        return list(self._signals)


class RecordingSink:
    name = "recording"

    def __init__(self) -> None:
        self.published: list[tuple[int, dict]] = []
        self.fail_on: int | None = None

    def publish(self, event_seq: int, event) -> bool:
        if self.fail_on is not None and event_seq == self.fail_on:
            return False
        self.published.append((event_seq, event))
        return True


def _seed_bracket(
    ledger: Ledger,
    broker: SimBroker,
    clock: SettableClock,
    *,
    command_id: str,
    stop_price: str = "95",
    entry_prefilled: bool = False,
) -> object:
    """A bracket submitted at the 09-22 EOD clock so its DAY entry works 09-23."""
    intent = OrderIntent(
        intent_id=f"intent-{command_id}",
        account_id=ACCOUNT,
        instrument=INSTRUMENT,
        side=Side.BUY,
        quantity_rule="fixed_2",
        entry_price=Decimal("100"),
        stop_loss=Decimal(stop_price),
        profit_targets=(Decimal("110"),),
        reason="E7 replay seed",
        command_id=command_id,
    )
    manager = OrderManager(broker, clock, ledger)
    bracket = manager.create_bracket(intent, Decimal("2"))
    manager.submit(bracket.entry)
    if entry_prefilled:
        clock.set(SESSION_OPEN)
        broker.process_bar(_bar(SESSION_OPEN))
        manager.reconcile_order(bracket.entry.order_id)
    else:
        clock.set(SESSION_OPEN - timedelta(minutes=1))
    return bracket


def _marker_command(session: date, account: str = ACCOUNT) -> str:
    return f"eod:eod:{account}:{session.isoformat()}"


# -- the bar loop: fills recorded, brackets resolved, position closed ----------------


def test_replay_records_entry_and_stop_and_resolves_bracket(tmp_path: Path) -> None:
    clock = SettableClock(PREV_EOD)
    ledger = Ledger(tmp_path / "eod-ledger.db")
    ledger.open()
    broker = SimBroker(ACCOUNT, clock, Decimal("0"))
    broker.connect()
    _seed_bracket(ledger, broker, clock, command_id="e7-stop", entry_prefilled=True)
    script = {1: ("96", "97", "94", "95")}  # stop touched on the second bar

    market_data = FakeMarketData()
    market_data._script = script
    result = EodRunner(
        ledger,
        clock,
        CALENDAR,
        market_data,
        EodRunnerConfig(job_name="eod", brokers={ACCOUNT: broker}),
    ).run(SESSION)

    state = ledger.state(ACCOUNT)
    assert all(pos.quantity == Decimal("0") for pos in state.positions.values())
    assert state.realized_pnl == Decimal("-10")  # 2 shares, 100 in, 95 out, no fees
    stop_filled = [
        order
        for order in state.orders.values()
        if order.parent_order_id is not None
        and order.order_type is OrderType.STOP
        and order.state is OrderState.FILLED
    ]
    assert len(stop_filled) == 1
    assert result.accounts[0].bars_processed == 390
    ledger.close()


def test_open_position_gets_a_mark_at_the_last_regular_close(tmp_path: Path) -> None:
    clock = SettableClock(PREV_EOD)
    ledger = Ledger(tmp_path / "eod-ledger.db")
    ledger.open()
    broker = SimBroker(ACCOUNT, clock, Decimal("0"))
    broker.connect()
    _seed_bracket(ledger, broker, clock, command_id="e7-mark", entry_prefilled=True)

    market_data = FakeMarketData()
    market_data._script = {389: ("100", "105", "99", "105")}
    result = EodRunner(
        ledger,
        clock,
        CALENDAR,
        market_data,
        EodRunnerConfig(job_name="eod", brokers={ACCOUNT: broker}),
    ).run(SESSION)

    assert result.accounts[0].marks_appended == 1
    mark = next(
        event
        for event in ledger.events(account=ACCOUNT)
        if event.kind is EventKind.MARK
        and event.command_id.startswith(f"eod:mark:{ACCOUNT}:{SESSION.isoformat()}:")
    )
    assert mark.payload.price == Decimal("105")
    assert mark.payload.instrument == INSTRUMENT
    ledger.close()


def test_run_marker_is_appended_per_account_and_session(tmp_path: Path) -> None:
    clock = SettableClock(PREV_EOD)
    ledger = Ledger(tmp_path / "eod-ledger.db")
    ledger.open()
    broker = SimBroker(ACCOUNT, clock, Decimal("0"))
    broker.connect()
    _seed_bracket(ledger, broker, clock, command_id="e7-marker", entry_prefilled=True)
    EodRunner(
        ledger,
        clock,
        CALENDAR,
        FakeMarketData(),
        EodRunnerConfig(job_name="eod", brokers={ACCOUNT: broker}),
    ).run(SESSION)

    marker = ledger.event_by_command(_marker_command(SESSION))
    assert marker is not None
    assert marker.kind is EventKind.EOD_RUN
    assert marker.payload.session == SESSION
    assert marker.payload.bars_processed == 390
    ledger.close()


# -- acceptance 1: re-running the same session changes nothing -----------------------


def test_second_run_of_the_same_session_appends_nothing(tmp_path: Path) -> None:
    clock = SettableClock(PREV_EOD)
    ledger = Ledger(tmp_path / "eod-ledger.db")
    ledger.open()
    broker = SimBroker(ACCOUNT, clock, Decimal("0"))
    broker.connect()
    _seed_bracket(ledger, broker, clock, command_id="e7-idem", entry_prefilled=True)
    script = {1: ("96", "97", "94", "95")}
    market_data = FakeMarketData()
    market_data._script = script
    EodRunner(
        ledger,
        clock,
        CALENDAR,
        market_data,
        EodRunnerConfig(job_name="eod", brokers={ACCOUNT: broker}),
    ).run(SESSION)
    before = [
        (e.seq, e.kind.value, e.command_id, e.ts_utc.isoformat())
        for e in ledger.events()
    ]

    # A fresh in-memory venue and manager, as a restarted process would have.
    fresh_broker = SimBroker(ACCOUNT, clock, Decimal("0"))
    fresh_broker.connect()
    EodRunner(
        ledger,
        clock,
        CALENDAR,
        FakeMarketData(),
        EodRunnerConfig(job_name="eod", brokers={ACCOUNT: fresh_broker}),
    ).run(SESSION)
    after = [
        (e.seq, e.kind.value, e.command_id, e.ts_utc.isoformat())
        for e in ledger.events()
    ]

    assert before == after
    ledger.close()


def test_second_run_derives_the_same_fills_deterministically(tmp_path: Path) -> None:
    clock = SettableClock(PREV_EOD)
    ledger = Ledger(tmp_path / "eod-ledger.db")
    ledger.open()
    broker = SimBroker(ACCOUNT, clock, Decimal("0"))
    broker.connect()
    _seed_bracket(ledger, broker, clock, command_id="e7-det", entry_prefilled=True)
    script = {1: ("96", "97", "94", "95")}
    market_data = FakeMarketData()
    market_data._script = script
    EodRunner(
        ledger,
        clock,
        CALENDAR,
        market_data,
        EodRunnerConfig(job_name="eod", brokers={ACCOUNT: broker}),
    ).run(SESSION)
    first = [
        (fill.order_id, fill.quantity, fill.price, fill.filled_at)
        for fill in ledger.state(ACCOUNT).fills
    ]

    fresh_broker = SimBroker(ACCOUNT, clock, Decimal("0"))
    fresh_broker.connect()
    replay_data = FakeMarketData()
    replay_data._script = script
    EodRunner(
        ledger,
        clock,
        CALENDAR,
        replay_data,
        EodRunnerConfig(job_name="eod", brokers={ACCOUNT: fresh_broker}),
    ).run(SESSION)
    second = [
        (fill.order_id, fill.quantity, fill.price, fill.filled_at)
        for fill in ledger.state(ACCOUNT).fills
    ]

    assert second == first
    assert len(first) == 2
    ledger.close()


# -- acceptance 2: a session whose predecessor is incomplete refuses -----------------


def test_running_a_session_before_its_predecessor_refuses(tmp_path: Path) -> None:
    # Account A ran 09-23; account B has a marker only for 09-22. Running 09-24
    # for both must refuse on B's missing 09-23 marker.
    clock = SettableClock(PREV_EOD)
    ledger = Ledger(tmp_path / "eod-ledger.db")
    ledger.open()
    broker_a = SimBroker(ACCOUNT, clock, Decimal("0"))
    broker_a.connect()
    ledger.append(
        Event(
            account=SECOND_ACCOUNT,
            kind=EventKind.EOD_RUN,
            payload=EodRun(
                session=PREV_SESSION_PREV,
                job="eod",
                account_id=SECOND_ACCOUNT,
                bars_processed=0,
                at_close=PREV_EOD,
            ),
            ts_utc=PREV_EOD,
            command_id=_marker_command(PREV_SESSION_PREV, SECOND_ACCOUNT),
        )
    )
    EodRunner(
        ledger,
        clock,
        CALENDAR,
        FakeMarketData(),
        EodRunnerConfig(job_name="eod", brokers={ACCOUNT: broker_a}),
    ).run(SESSION)

    clock.set(datetime(2026, 9, 24, 13, 29, tzinfo=UTC))
    broker_b = SimBroker(SECOND_ACCOUNT, clock, Decimal("0"))
    broker_b.connect()
    with pytest.raises(SessionIncompleteError, match=SECOND_ACCOUNT):
        EodRunner(
            ledger,
            clock,
            CALENDAR,
            FakeMarketData(),
            EodRunnerConfig(
                job_name="eod", brokers={ACCOUNT: broker_a, SECOND_ACCOUNT: broker_b}
            ),
        ).run(NEXT_SESSION)
    ledger.close()


def test_next_session_runs_after_the_previous_is_complete(tmp_path: Path) -> None:
    clock = SettableClock(PREV_EOD)
    ledger = Ledger(tmp_path / "eod-ledger.db")
    ledger.open()
    broker = SimBroker(ACCOUNT, clock, Decimal("0"))
    broker.connect()
    _seed_bracket(ledger, broker, clock, command_id="e7-next", entry_prefilled=True)
    runner = EodRunner(
        ledger,
        clock,
        CALENDAR,
        FakeMarketData(),
        EodRunnerConfig(job_name="eod", brokers={ACCOUNT: broker}),
    )
    runner.run(SESSION)
    clock.set(datetime(2026, 9, 24, 13, 29, tzinfo=UTC))
    result = runner.run(NEXT_SESSION)
    assert ledger.event_by_command(_marker_command(NEXT_SESSION)) is not None
    assert result.accounts[0].bars_processed == 390
    ledger.close()


def test_non_session_date_refuses(tmp_path: Path) -> None:
    clock = SettableClock(PREV_EOD)
    ledger = Ledger(tmp_path / "eod-ledger.db")
    ledger.open()
    broker = SimBroker(ACCOUNT, clock, Decimal("0"))
    broker.connect()
    with pytest.raises(EodRunnerError, match="not a trading session"):
        EodRunner(
            ledger,
            clock,
            CALENDAR,
            FakeMarketData(),
            EodRunnerConfig(job_name="eod", brokers={ACCOUNT: broker}),
        ).run(date(2026, 9, 19))  # Saturday
    ledger.close()


def test_empty_bar_series_refuses(tmp_path: Path) -> None:
    clock = SettableClock(PREV_EOD)
    ledger = Ledger(tmp_path / "eod-ledger.db")
    ledger.open()
    broker = SimBroker(ACCOUNT, clock, Decimal("0"))
    broker.connect()
    _seed_bracket(ledger, broker, clock, command_id="e7-empty", entry_prefilled=True)

    class EmptyProvider:
        def bars(self, instrument, tf, start, end, max_age_seconds):
            return []

    with pytest.raises(ReplayDataError, match="No one-minute bars"):
        EodRunner(
            ledger,
            clock,
            CALENDAR,
            EmptyProvider(),
            EodRunnerConfig(job_name="eod", brokers={ACCOUNT: broker}),
        ).run(SESSION)
    ledger.close()


def test_bar_series_starting_one_minute_late_refuses(tmp_path: Path) -> None:
    clock = SettableClock(PREV_EOD)
    ledger = Ledger(tmp_path / "eod-ledger.db")
    ledger.open()
    broker = SimBroker(ACCOUNT, clock, Decimal("0"))
    broker.connect()
    _seed_bracket(ledger, broker, clock, command_id="e7-late", entry_prefilled=True)

    class LateStartProvider:
        def bars(self, instrument, tf, start, end, max_age_seconds):
            return [_bar(start + timedelta(minutes=i)) for i in range(1, 390)]

    with pytest.raises(ReplayDataError, match="not the session open"):
        EodRunner(
            ledger,
            clock,
            CALENDAR,
            LateStartProvider(),
            EodRunnerConfig(job_name="eod", brokers={ACCOUNT: broker}),
        ).run(SESSION)
    ledger.close()


PREV_SESSION_PREV = date(2026, 9, 21)


# -- reconcile after every bar -------------------------------------------------------


def test_runner_reconciles_fills_within_their_bar(tmp_path: Path) -> None:
    """The entry fills mid-session; its protective stop must be submitted by the
    reconcile of THAT bar, so later bars find the stop resting and can fill it.

    With reconciliation deferred to the end of the session, the stop arrives after
    later bars were simulated, SimBroker REJECTS it and the OMS raises — the guard
    E7's bar loop exists to keep out of reach of.
    """
    clock = SettableClock(PREV_EOD)
    ledger = Ledger(tmp_path / "eod-ledger.db")
    ledger.open()
    broker = SimBroker(ACCOUNT, clock, Decimal("0"))
    broker.connect()
    _seed_bracket(ledger, broker, clock, command_id="e7-per-bar", entry_prefilled=False)
    script = {
        5: ("99", "101", "98", "100"),  # entry limit 100 fills here
        6: ("96", "97", "94", "95"),    # stop 95 touched one bar later
    }
    market_data = FakeMarketData()
    market_data._script = script
    EodRunner(
        ledger,
        clock,
        CALENDAR,
        market_data,
        EodRunnerConfig(job_name="eod", brokers={ACCOUNT: broker}),
    ).run(SESSION)

    state = ledger.state(ACCOUNT)
    fills = {fill.order_id: fill for fill in state.fills}
    stop_id = "e7-per-bar:stop"
    assert "e7-per-bar:entry" in fills
    assert stop_id in fills
    # The stop's fill is recorded at the bar that touched it, not after the session.
    assert fills[stop_id].filled_at == SESSION_OPEN + timedelta(minutes=6)
    assert all(pos.quantity == Decimal("0") for pos in state.positions.values())
    ledger.close()


def test_a_forgotten_mid_session_reconcile_is_caught_by_the_venue_guard() -> None:
    """The guard E7 relies on: an exit submitted after later bars is REJECTED."""
    clock = SettableClock(SESSION_OPEN)
    broker = SimBroker(ACCOUNT, clock, Decimal("0"))
    broker.connect()
    entry = VenueOrder(
        venue_order_id="guard:entry",
        instrument=INSTRUMENT,
        order_type=OrderType.MARKET,
        side=Side.BUY,
        quantity=Decimal("2"),
        submitted_at=SESSION_OPEN - timedelta(minutes=1),
        tif=TimeInForce.DAY,
        allocations=(VenueOrderAllocation("guard:entry", ACCOUNT, Decimal("2")),),
    )
    broker.submit(entry)
    clock.set(SESSION_OPEN)
    broker.process_bar(_bar(SESSION_OPEN))
    middle = SESSION_OPEN + timedelta(minutes=1)
    clock.set(middle)
    broker.process_bar(_bar(middle))
    later = SESSION_OPEN + timedelta(minutes=2)
    clock.set(later)
    broker.process_bar(_bar(later, open_="96", high="97", low="93", close="94"))

    late_stop = VenueOrder(
        venue_order_id="guard:stop",
        instrument=INSTRUMENT,
        order_type=OrderType.STOP,
        side=Side.SELL,
        quantity=Decimal("2"),
        submitted_at=later,
        tif=TimeInForce.GTC,
        stop_price=Decimal("95"),
        allocations=(VenueOrderAllocation("guard:stop", ACCOUNT, Decimal("2")),),
        parent_order_id="guard:entry",
    )
    ack = broker.submit(late_stop)
    assert ack.status == "REJECTED"
    assert "reconcile after every bar" in (ack.message or "")


# -- entries for D+1 -----------------------------------------------------------------


def _risk_engine(ledger: Ledger, clock: ReplayClock) -> RiskEngine:
    rules = AccountRiskRules.from_mapping(
        {
            "risk_per_trade": "0.75%",
            "short_risk_per_trade": "0.5%",
            "max_position_notional": "10%",
            "max_gross_exposure": "150%",
            "bull_chop_gross_exposure": "100%",
            "max_portfolio_heat": "6%",
            "max_positions": 10,
            "max_positions_per_industry": 3,
            "min_price": "5",
            "max_adv": "5%",
            "earnings_blackout_sessions": 1,
            "drawdown_half_risk": "5%",
            "drawdown_suspend": "10%",
            "drawdown_recovery": "3%",
            "daily_loss_block": "3%",
        }
    )
    rails = VenueRiskRails(venue_id="SimBroker", environment="sim")
    return RiskEngine(rules, rails, clock, ledger)


def _fixed_context_builder():
    def builder(intent, state, session) -> RiskContext:
        return RiskContext(
            equity=Decimal("10000"),
            current_price=Decimal("100"),
            gross_exposure=Decimal("0"),
            portfolio_heat=Decimal("0"),
            open_positions=0,
            industry=None,
            industry_positions=None,
            average_dollar_volume_20d=None,
            sessions_until_earnings=None,
            regime="BULL_EXPLOSIVE",
            macro_high_risk_day=False,
            drawdown_from_peak_frac=None,
            previous_session_pnl_frac=None,
            venue_orders_today=0,
            venue_daily_pnl=Decimal("0"),
            current_position_quantity=0,
        )

    return builder


def _signal() -> Signal:
    return Signal(
        signal_id="sig-1",
        scan_id="watch-breakout",
        symbol="AAPL",
        session_date=SESSION,
        direction="long",
    )


def test_new_orders_for_d_plus_one_are_risk_approved_and_submitted(tmp_path: Path) -> None:
    clock = SettableClock(EOD_CLOCK)
    ledger = Ledger(tmp_path / "eod-ledger.db")
    ledger.open()
    broker = SimBroker(ACCOUNT, clock, Decimal("0"))
    broker.connect()
    result = EodRunner(
        ledger,
        clock,
        CALENDAR,
        FakeMarketData(),
        EodRunnerConfig(
            job_name="eod",
            brokers={ACCOUNT: broker},
            risk_engines={ACCOUNT: FixedRiskEngine()},
            signal_adapters={ACCOUNT: SessionSignalAdapter([_signal()])},
            strategies={ACCOUNT: DeterministicStrategy(INSTRUMENT)},
            context_builder=_fixed_context_builder(),
        ),
    ).run(SESSION)

    assert result.accounts[0].orders_submitted == 1
    state = ledger.state(ACCOUNT)
    entries = [
        order
        for order in state.orders.values()
        if order.parent_order_id is None and order.command_id.startswith("d1:")
    ]
    assert len(entries) == 1
    assert entries[0].tif is TimeInForce.DAY
    assert ledger.event_by_command("signal:sig-1") is not None
    assert ledger.event_by_command("risk:d1:sig-1") is not None
    ledger.close()


def test_rerunning_with_entries_does_not_double_submit(tmp_path: Path) -> None:
    clock = SettableClock(EOD_CLOCK)
    ledger = Ledger(tmp_path / "eod-ledger.db")
    ledger.open()
    broker = SimBroker(ACCOUNT, clock, Decimal("0"))
    broker.connect()
    config = EodRunnerConfig(
        job_name="eod",
        brokers={ACCOUNT: broker},
        risk_engines={ACCOUNT: FixedRiskEngine()},
        signal_adapters={ACCOUNT: SessionSignalAdapter([_signal()])},
        strategies={ACCOUNT: DeterministicStrategy(INSTRUMENT)},
        context_builder=_fixed_context_builder(),
    )
    market_data = FakeMarketData()
    EodRunner(ledger, clock, CALENDAR, market_data, config).run(SESSION)
    before = [
        (e.seq, e.kind.value, e.command_id, e.ts_utc.isoformat())
        for e in ledger.events()
    ]
    EodRunner(ledger, clock, CALENDAR, FakeMarketData(), config).run(SESSION)
    after = [
        (e.seq, e.kind.value, e.command_id, e.ts_utc.isoformat())
        for e in ledger.events()
    ]

    assert before == after
    ledger.close()


def test_refused_intent_records_verdict_and_submits_nothing(tmp_path: Path) -> None:
    class RefusingRiskEngine:
        name = "refusing"

        def evaluate(self, intent, context) -> RiskVerdict:
            return RiskVerdict(
                order_intent_id=intent.intent_id,
                evaluations=(
                    RiskRuleResult(
                        "always_refuses",
                        False,
                        "x",
                        "y",
                        "The test rule refuses this intent",
                    ),
                ),
                refusal_reasons=("The test rule refuses this intent",),
                approved_quantity=None,
            )

    clock = SettableClock(EOD_CLOCK)
    ledger = Ledger(tmp_path / "eod-ledger.db")
    ledger.open()
    broker = SimBroker(ACCOUNT, clock, Decimal("0"))
    broker.connect()
    result = EodRunner(
        ledger,
        clock,
        CALENDAR,
        FakeMarketData(),
        EodRunnerConfig(
            job_name="eod",
            brokers={ACCOUNT: broker},
            risk_engines={ACCOUNT: RefusingRiskEngine()},
            signal_adapters={ACCOUNT: SessionSignalAdapter([_signal()])},
            strategies={ACCOUNT: DeterministicStrategy(INSTRUMENT)},
            context_builder=_fixed_context_builder(),
        ),
    ).run(SESSION)

    assert result.accounts[0].orders_submitted == 0
    assert ledger.event_by_command("risk:d1:sig-1") is not None
    assert not [
        order
        for order in ledger.state(ACCOUNT).orders.values()
        if order.parent_order_id is None and order.command_id.startswith("d1:")
    ]
    ledger.close()


# -- outbox --------------------------------------------------------------------------


def test_journal_outbox_is_enqueued_and_drained_in_order(tmp_path: Path) -> None:
    clock = SettableClock(PREV_EOD)
    ledger = Ledger(tmp_path / "eod-ledger.db")
    ledger.open()
    broker = SimBroker(ACCOUNT, clock, Decimal("0"))
    broker.connect()
    # The entry is left unfilled by the seed; the RUNNER's bar loop fills it,
    # records the fill and enqueues its journal outbox row.
    _seed_bracket(ledger, broker, clock, command_id="e7-outbox", entry_prefilled=False)
    sink = RecordingSink()

    EodRunner(
        ledger,
        clock,
        CALENDAR,
        FakeMarketData(),
        EodRunnerConfig(
            job_name="eod",
            brokers={ACCOUNT: broker},
            sinks={"journal:journal-acc-1": sink},
            journal_accounts={ACCOUNT: "journal-acc-1"},
        ),
    ).run(SESSION)

    fills = ledger.state(ACCOUNT).fills
    assert len(fills) >= 1
    assert len(sink.published) >= 1
    payloads = [payload for _, payload in sink.published]
    assert all(payload["account_id"] == "journal-acc-1" for payload in payloads_of(sink))
    assert all(payload["asset_class"] == "equity" for payload in payloads_of(sink))
    assert ledger.pending_outbox("journal:journal-acc-1") == []
    ledger.close()


def payloads_of(sink: RecordingSink) -> list[dict]:
    return [payload for _, payload in sink.published]


# -- invariants surfaced through the runner ------------------------------------------


def test_options_positions_refuse_eod_replay(tmp_path: Path) -> None:
    from trade_engine.domain.instruments import OptionContract, OptionRight

    contract = OptionContract(
        underlying="SPXW",
        expiry=date(2026, 9, 25),
        strike=Decimal("5700"),
        right=OptionRight.PUT,
    )
    clock = SettableClock(EOD_CLOCK)
    ledger = Ledger(tmp_path / "eod-ledger.db")
    ledger.open()
    broker = SimBroker(ACCOUNT, clock, Decimal("0"))
    broker.connect()
    order = Order(
        order_id="opt-entry",
        account_id=ACCOUNT,
        instrument=contract,
        order_type=OrderType.LIMIT,
        side=Side.BUY,
        quantity=Decimal("1"),
        command_id="opt-seed:entry",
        created_at=PREV_EOD,
        limit_price=Decimal("10"),
        tif=TimeInForce.GTC,
    )
    ledger.append(
        Event(
            account=ACCOUNT,
            kind=EventKind.ORDER_SUBMITTED,
            payload=order,
            ts_utc=PREV_EOD,
            command_id="opt-seed:entry",
        )
    )
    with pytest.raises(EodRunnerError, match="O2"):
        EodRunner(
            ledger,
            clock,
            CALENDAR,
            FakeMarketData(),
            EodRunnerConfig(job_name="eod", brokers={ACCOUNT: broker}),
        ).run(SESSION)
    ledger.close()


def test_session_must_be_a_date_not_a_datetime(tmp_path: Path) -> None:
    clock = SettableClock(EOD_CLOCK)
    ledger = Ledger(tmp_path / "eod-ledger.db")
    ledger.open()
    broker = SimBroker(ACCOUNT, clock, Decimal("0"))
    broker.connect()
    with pytest.raises(EodRunnerError, match="session must be a date"):
        EodRunner(
            ledger,
            clock,
            CALENDAR,
            FakeMarketData(),
            EodRunnerConfig(job_name="eod", brokers={ACCOUNT: broker}),
        ).run(SESSION_OPEN)  # datetime, not date
    ledger.close()

# -- a new process: the in-memory SimBroker is restored from the ledger --------------

NEXT_OPEN = datetime(2026, 9, 24, 13, 30, tzinfo=UTC)


def _minute_of(timestamp: datetime) -> int:
    """FakeMarketData scripts count minutes from SESSION_OPEN, across sessions."""
    return int((timestamp - SESSION_OPEN).total_seconds() // 60)


def test_fresh_simbroker_works_the_ledgers_d_plus_one_entry(tmp_path: Path) -> None:
    clock = SettableClock(PREV_EOD)
    ledger = Ledger(tmp_path / "eod-ledger.db")
    ledger.open()
    seeding_broker = SimBroker(ACCOUNT, clock, Decimal("0"))
    seeding_broker.connect()
    bracket = _seed_bracket(ledger, seeding_broker, clock, command_id="e7-restore")

    # The EOD job is a new process: its SimBroker has never seen the entry.
    fresh_broker = SimBroker(ACCOUNT, clock, Decimal("0"))
    result = EodRunner(
        ledger,
        clock,
        CALENDAR,
        FakeMarketData(),
        EodRunnerConfig(job_name="eod", brokers={ACCOUNT: fresh_broker}),
    ).run(SESSION)

    state = ledger.state(ACCOUNT)
    assert result.accounts[0].fills_recorded == 1
    assert state.orders[bracket.entry.order_id].state is OrderState.FILLED
    assert state.positions[INSTRUMENT].quantity == Decimal("2")
    assert {state.orders[order.order_id].state for order in (bracket.stop, *bracket.targets)} == {
        OrderState.ACCEPTED
    }
    ledger.close()


def test_fresh_simbroker_keeps_yesterdays_gtc_stop_protecting(tmp_path: Path) -> None:
    clock = SettableClock(PREV_EOD)
    ledger = Ledger(tmp_path / "eod-ledger.db")
    ledger.open()
    broker = SimBroker(ACCOUNT, clock, Decimal("0"))
    broker.connect()
    bracket = _seed_bracket(ledger, broker, clock, command_id="e7-swing")
    stop_minute = _minute_of(NEXT_OPEN + timedelta(minutes=60))
    market_data = FakeMarketData({stop_minute: ("96", "96", "94", "95")})
    EodRunner(
        ledger, clock, CALENDAR, market_data,
        EodRunnerConfig(job_name="eod", brokers={ACCOUNT: broker}),
    ).run(SESSION)
    assert ledger.state(ACCOUNT).positions[INSTRUMENT].quantity == Decimal("2")

    clock.set(NEXT_OPEN - timedelta(minutes=1))
    fresh_broker = SimBroker(ACCOUNT, clock, Decimal("0"))
    EodRunner(
        ledger, clock, CALENDAR, market_data,
        EodRunnerConfig(job_name="eod", brokers={ACCOUNT: fresh_broker}),
    ).run(NEXT_SESSION)

    state = ledger.state(ACCOUNT)
    stop, target = bracket.stop, bracket.targets[0]
    assert state.orders[stop.order_id].state is OrderState.FILLED
    assert state.orders[target.order_id].state is OrderState.CANCELLED
    assert state.positions[INSTRUMENT].quantity == Decimal("0")
    exit_fill = next(fill for fill in state.fills if fill.order_id == stop.order_id)
    assert exit_fill.filled_at == NEXT_OPEN + timedelta(minutes=60)
    assert exit_fill.price == Decimal("95")
    ledger.close()


class _ReadOnlyVenue:
    """A venue that reports a fixed book; lets a test show what the runner demands."""

    env = "paper"

    def __init__(self, states=()) -> None:
        self._states = list(states)

    def connect(self) -> None:
        return None

    def orders(self, since):
        return list(self._states)

    def fills(self, since):
        return []


def test_venue_missing_a_working_order_refuses(tmp_path: Path) -> None:
    clock = SettableClock(PREV_EOD)
    ledger = Ledger(tmp_path / "eod-ledger.db")
    ledger.open()
    seeding_broker = SimBroker(ACCOUNT, clock, Decimal("0"))
    seeding_broker.connect()
    bracket = _seed_bracket(ledger, seeding_broker, clock, command_id="e7-missing")
    with pytest.raises(EodRunnerError, match=f"does not hold working order '{bracket.entry.order_id}'"):
        EodRunner(
            ledger, clock, CALENDAR, FakeMarketData(),
            EodRunnerConfig(job_name="eod", brokers={ACCOUNT: _ReadOnlyVenue()}),
        ).run(SESSION)
    ledger.close()


def test_venue_reporting_fewer_fills_than_the_ledger_refuses(tmp_path: Path) -> None:
    from trade_engine.interfaces.broker import VenueOrderState

    clock = SettableClock(PREV_EOD)
    ledger = Ledger(tmp_path / "eod-ledger.db")
    ledger.open()
    seeding_broker = SimBroker(ACCOUNT, clock, Decimal("0"))
    seeding_broker.connect()
    bracket = _seed_bracket(ledger, seeding_broker, clock, command_id="e7-short")
    entry_id = bracket.entry.order_id
    OrderManager(seeding_broker, clock, ledger).record_fill(
        Fill(
            fill_id="partial-1",
            order_id=entry_id,
            account_id=ACCOUNT,
            instrument=INSTRUMENT,
            quantity=Decimal("1"),
            price=Decimal("100"),
            venue_env="sim",
            filled_at=clock.now_utc(),
            side=Side.BUY,
        )
    )
    assert ledger.state(ACCOUNT).orders[entry_id].state is OrderState.PARTIALLY_FILLED
    venue = _ReadOnlyVenue(
        [
            VenueOrderState(
                venue_order_id=entry_id,
                state=OrderState.ACCEPTED,
                filled_quantity=Decimal("0"),
                remaining_quantity=Decimal("2"),
                updated_at=PREV_EOD,
            )
        ]
    )
    with pytest.raises(EodRunnerError, match="reports 0 filled .* ledger records 1"):
        EodRunner(
            ledger, clock, CALENDAR, FakeMarketData(),
            EodRunnerConfig(job_name="eod", brokers={ACCOUNT: venue}),
        ).run(SESSION)
    ledger.close()


class _LostAckBroker(SimBroker):
    def submit(self, order):
        raise ConnectionError("ack lost")


def test_pending_unknown_order_is_not_restored(tmp_path: Path) -> None:
    from trade_engine.oms.manager import BrokerOutcomeUnknownError

    clock = SettableClock(PREV_EOD)
    ledger = Ledger(tmp_path / "eod-ledger.db")
    ledger.open()
    lossy = _LostAckBroker(ACCOUNT, clock, Decimal("0"))
    lossy.connect()
    with pytest.raises(BrokerOutcomeUnknownError):
        _seed_bracket(ledger, lossy, clock, command_id="e7-pending")
    clock.set(SESSION_OPEN - timedelta(minutes=1))
    with pytest.raises(EodRunnerError, match="PENDING_UNKNOWN"):
        EodRunner(
            ledger, clock, CALENDAR, FakeMarketData(),
            EodRunnerConfig(
                job_name="eod", brokers={ACCOUNT: SimBroker(ACCOUNT, clock, Decimal("0"))}
            ),
        ).run(SESSION)
    ledger.close()


def test_order_without_a_submission_event_is_not_restored(tmp_path: Path) -> None:
    clock = SettableClock(PREV_EOD)
    ledger = Ledger(tmp_path / "eod-ledger.db")
    ledger.open()
    order = Order(
        order_id="manual-entry",
        account_id=ACCOUNT,
        instrument=INSTRUMENT,
        order_type=OrderType.LIMIT,
        side=Side.BUY,
        quantity=Decimal("1"),
        command_id="manual:entry",
        created_at=PREV_EOD,
        limit_price=Decimal("100"),
        tif=TimeInForce.GTC,
    )
    ledger.append(
        Event(
            account=ACCOUNT,
            kind=EventKind.ORDER_SUBMITTED,
            payload=order,
            ts_utc=PREV_EOD,
            command_id="manual:entry",
        )
    )
    clock.set(SESSION_OPEN - timedelta(minutes=1))
    with pytest.raises(EodRunnerError, match="no submission event"):
        EodRunner(
            ledger, clock, CALENDAR, FakeMarketData(),
            EodRunnerConfig(
                job_name="eod", brokers={ACCOUNT: SimBroker(ACCOUNT, clock, Decimal("0"))}
            ),
        ).run(SESSION)
    ledger.close()

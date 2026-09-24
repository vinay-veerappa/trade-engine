"""Known-answer tests for the deterministic equities paper venue."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from trade_engine.domain.instruments import Equity, Side
from trade_engine.domain.orders import OrderState, OrderType, TimeInForce
from trade_engine.domain.signals import OrderIntent
from trade_engine.interfaces.broker import (
    OrderChanges,
    VenueFill,
    VenueOrder,
    VenueOrderAllocation,
    VenuePosition,
)
from trade_engine.interfaces.clock import Clock
from trade_engine.interfaces.market_data import Bar
from trade_engine.ledger import Ledger
from trade_engine.oms.manager import OrderManagementError, OrderManager
from trade_engine.sim import MissingBarError, SimBroker
from trade_engine.sim.broker import SimBrokerError

UTC = timezone.utc
ACCOUNT = "sim-account"
INSTRUMENT = Equity("AAPL")
START = datetime(2026, 9, 23, 13, 29, tzinfo=UTC)


class ReplayClock(Clock):
    def __init__(self, now: datetime = START) -> None:
        self.current = now

    def now_utc(self) -> datetime:
        return self.current

    def sleep(self, seconds: float) -> None:
        raise AssertionError("SimBroker must not sleep")

    def set(self, now: datetime) -> None:
        self.current = now


def bar(
    timestamp: datetime,
    *,
    open_: str = "100",
    high: str = "101",
    low: str = "99",
    close: str = "100",
    instrument: Equity = INSTRUMENT,
) -> Bar:
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


def broker_fixture(
    *,
    clock: ReplayClock | None = None,
    slippage_bps: Decimal = Decimal("0"),
) -> tuple[SimBroker, ReplayClock]:
    replay_clock = clock or ReplayClock()
    broker = SimBroker(ACCOUNT, replay_clock, slippage_bps)
    broker.connect()
    return broker, replay_clock


def place_order(
    broker: SimBroker,
    clock: ReplayClock,
    order_id: str,
    *,
    side: Side,
    order_type: OrderType,
    quantity: Decimal = Decimal("1"),
    limit_price: Decimal | None = None,
    stop_price: Decimal | None = None,
    tif: TimeInForce = TimeInForce.DAY,
    oco_group: str | None = None,
    parent_order_id: str | None = None,
) -> None:
    broker.submit(
        VenueOrder(
            venue_order_id=order_id,
            instrument=INSTRUMENT,
            order_type=order_type,
            side=side,
            quantity=quantity,
            submitted_at=clock.now_utc(),
            tif=tif,
            limit_price=limit_price,
            stop_price=stop_price,
            allocations=(VenueOrderAllocation(order_id, ACCOUNT, quantity),),
            parent_order_id=parent_order_id,
            oco_group=oco_group,
        )
    )


def feed_session(
    broker: SimBroker,
    clock: ReplayClock,
    open_at: datetime,
    *,
    minutes: int = 390,
) -> None:
    """Feed ``minutes`` flat one-minute bars from ``open_at`` (390 = a full session)."""
    for minute in range(minutes):
        timestamp = open_at + timedelta(minutes=minute)
        clock.set(timestamp)
        broker.process_bar(bar(timestamp))


def test_gap_through_stop_fills_at_open_with_configured_slippage() -> None:
    broker, clock = broker_fixture(slippage_bps=Decimal("10"))
    place_order(
        broker,
        clock,
        "stop",
        side=Side.SELL,
        order_type=OrderType.STOP,
        stop_price=Decimal("95"),
    )

    fills = broker.process_bar(
        bar(START + timedelta(minutes=1), open_="90", high="92", low="88", close="91")
    )

    assert len(fills) == 1
    assert fills[0].price == Decimal("89.910")
    assert fills[0].quantity == Decimal("1")


def test_same_orders_and_bars_replay_to_identical_fills() -> None:
    def replay():
        broker, clock = broker_fixture(slippage_bps=Decimal("5"))
        place_order(
            broker,
            clock,
            "deterministic-market",
            side=Side.BUY,
            order_type=OrderType.MARKET,
        )
        matched = broker.process_bar(
            bar(
                START + timedelta(minutes=1),
                open_="100",
                high="101",
                low="99",
                close="100",
            )
        )
        return matched, tuple(broker.fills(START))

    assert replay() == replay()


def test_stop_wins_when_stop_and_target_are_inside_the_same_bar(tmp_path: Path) -> None:
    clock = ReplayClock()
    broker, _ = broker_fixture(clock=clock)
    intent = OrderIntent(
        intent_id="same-bar-exit",
        account_id=ACCOUNT,
        instrument=INSTRUMENT,
        side=Side.BUY,
        quantity_rule="fixed_2",
        entry_price=Decimal("100"),
        stop_loss=Decimal("95"),
        profit_targets=(Decimal("105"),),
        reason="Known-answer same-bar exit",
        command_id="same-bar-command",
    )

    with Ledger(tmp_path / "ledger.db") as ledger:
        manager = OrderManager(broker, clock, ledger)
        bracket = manager.create_bracket(intent, Decimal("2"))
        manager.submit(bracket.entry)

        entry_bar = bar(START + timedelta(minutes=1))
        clock.set(entry_bar.timestamp)
        broker.process_bar(entry_bar)
        manager.reconcile_order(bracket.entry.order_id)

        exit_bar = bar(
            START + timedelta(minutes=2),
            open_="100",
            high="106",
            low="94",
            close="101",
        )
        clock.set(exit_bar.timestamp)
        fills = broker.process_bar(exit_bar)
        assert [fill.venue_order_id for fill in fills] == [bracket.stop.order_id]
        assert fills[0].price == Decimal("95")
        manager.reconcile_order(bracket.stop.order_id)

        assert manager.get_order(bracket.targets[0].order_id).state is OrderState.CANCELLED

    assert broker.positions() == []


def test_partial_target_then_trailing_stop_closes_remaining_position(tmp_path: Path) -> None:
    clock = ReplayClock()
    broker, _ = broker_fixture(clock=clock)
    intent = OrderIntent(
        intent_id="partial-exit",
        account_id=ACCOUNT,
        instrument=INSTRUMENT,
        side=Side.BUY,
        quantity_rule="fixed_6",
        entry_price=Decimal("100"),
        stop_loss=Decimal("95"),
        profit_targets=(Decimal("110"), Decimal("115")),
        reason="Known-answer partial exit",
        command_id="partial-exit-command",
    )

    with Ledger(tmp_path / "ledger.db") as ledger:
        manager = OrderManager(broker, clock, ledger)
        bracket = manager.create_bracket(intent, Decimal("6"))
        manager.submit(bracket.entry)

        entry_bar = bar(START + timedelta(minutes=1), open_="99", high="101", low="98")
        clock.set(entry_bar.timestamp)
        broker.process_bar(entry_bar)
        manager.reconcile_order(bracket.entry.order_id)

        target_bar = bar(
            START + timedelta(minutes=2),
            open_="105",
            high="111",
            low="104",
            close="110",
        )
        clock.set(target_bar.timestamp)
        broker.process_bar(target_bar)
        manager.reconcile_order(bracket.targets[0].order_id)

        reduced_stop = manager.get_order(bracket.stop.order_id)
        assert reduced_stop.quantity == Decimal("3")

        clock.set(target_bar.timestamp + timedelta(minutes=1))
        manager.replace(
            bracket.stop.order_id,
            OrderChanges(new_stop_price=Decimal("108")),
            command_id="trail-stop",
        )
        assert manager.get_order(bracket.stop.order_id).stop_price == Decimal("108")

        stop_bar = bar(
            START + timedelta(minutes=3),
            open_="109",
            high="110",
            low="106",
            close="107",
        )
        clock.set(stop_bar.timestamp)
        broker.process_bar(stop_bar)
        manager.reconcile_order(bracket.stop.order_id)

        assert manager.get_order(bracket.stop.order_id).state is OrderState.FILLED
        assert manager.get_order(bracket.targets[1].order_id).state is OrderState.CANCELLED

    assert broker.positions() == []


def test_exit_fill_cannot_reverse_position_before_oms_reconciliation(tmp_path: Path) -> None:
    clock = ReplayClock()
    broker, _ = broker_fixture(clock=clock)
    intent = OrderIntent(
        intent_id="unreconciled-exit",
        account_id=ACCOUNT,
        instrument=INSTRUMENT,
        side=Side.BUY,
        quantity_rule="fixed_6",
        entry_price=Decimal("100"),
        stop_loss=Decimal("95"),
        profit_targets=(Decimal("110"), Decimal("115")),
        reason="Exit fills must not reverse the position",
        command_id="unreconciled-exit-command",
    )

    with Ledger(tmp_path / "ledger.db") as ledger:
        manager = OrderManager(broker, clock, ledger)
        bracket = manager.create_bracket(intent, Decimal("6"))
        manager.submit(bracket.entry)

        entry_bar = bar(START + timedelta(minutes=1), open_="99", high="101", low="98")
        clock.set(entry_bar.timestamp)
        broker.process_bar(entry_bar)
        manager.reconcile_order(bracket.entry.order_id)

        target_bar = bar(
            START + timedelta(minutes=2),
            open_="105",
            high="111",
            low="104",
            close="110",
        )
        clock.set(target_bar.timestamp)
        target_fills = broker.process_bar(target_bar)
        assert [fill.venue_order_id for fill in target_fills] == [
            bracket.targets[0].order_id
        ]
        assert broker.positions()[0].quantity == Decimal("3")

        stop_bar = bar(
            START + timedelta(minutes=3),
            open_="94",
            high="96",
            low="93",
            close="95",
        )
        clock.set(stop_bar.timestamp)
        stop_fills = broker.process_bar(stop_bar)
        manager.reconcile_order(bracket.targets[0].order_id)
        manager.reconcile_order(bracket.stop.order_id)
        assert manager.get_order(bracket.stop.order_id).state is OrderState.FILLED

    assert [fill.venue_order_id for fill in stop_fills] == [bracket.stop.order_id]
    assert stop_fills[0].quantity == Decimal("3")
    assert broker.positions() == []


def test_target_fill_leaves_position_open_when_protective_stop_is_untouched(
    tmp_path: Path,
) -> None:
    clock = ReplayClock()
    broker, _ = broker_fixture(clock=clock)
    intent = OrderIntent(
        intent_id="partial-target-without-stop",
        account_id=ACCOUNT,
        instrument=INSTRUMENT,
        side=Side.BUY,
        quantity_rule="fixed_6",
        entry_price=Decimal("100"),
        stop_loss=Decimal("95"),
        profit_targets=(Decimal("110"), Decimal("115")),
        reason="A valid partial target must remain open",
        command_id="partial-target-without-stop-command",
    )

    with Ledger(tmp_path / "ledger.db") as ledger:
        manager = OrderManager(broker, clock, ledger)
        bracket = manager.create_bracket(intent, Decimal("6"))
        manager.submit(bracket.entry)

        entry_bar = bar(START + timedelta(minutes=1))
        clock.set(entry_bar.timestamp)
        broker.process_bar(entry_bar)
        manager.reconcile_order(bracket.entry.order_id)

        target_bar = bar(
            START + timedelta(minutes=2),
            open_="105",
            high="111",
            low="104",
            close="110",
        )
        clock.set(target_bar.timestamp)
        fills = broker.process_bar(target_bar)

    assert [fill.venue_order_id for fill in fills] == [bracket.targets[0].order_id]
    assert fills[0].quantity == Decimal("3")
    assert broker.positions()[0].quantity == Decimal("3")


def test_both_targets_fill_when_touched_in_the_same_bar(tmp_path: Path) -> None:
    clock = ReplayClock()
    broker, _ = broker_fixture(clock=clock)
    intent = OrderIntent(
        intent_id="same-bar-targets",
        account_id=ACCOUNT,
        instrument=INSTRUMENT,
        side=Side.BUY,
        quantity_rule="fixed_6",
        entry_price=Decimal("100"),
        stop_loss=Decimal("95"),
        profit_targets=(Decimal("110"), Decimal("115")),
        reason="Both touched targets should execute",
        command_id="same-bar-targets-command",
    )

    with Ledger(tmp_path / "ledger.db") as ledger:
        manager = OrderManager(broker, clock, ledger)
        bracket = manager.create_bracket(intent, Decimal("6"))
        manager.submit(bracket.entry)

        entry_bar = bar(START + timedelta(minutes=1))
        clock.set(entry_bar.timestamp)
        broker.process_bar(entry_bar)
        manager.reconcile_order(bracket.entry.order_id)

        target_bar = bar(
            START + timedelta(minutes=2),
            open_="105",
            high="116",
            low="104",
            close="115",
        )
        clock.set(target_bar.timestamp)
        fills = broker.process_bar(target_bar)
        manager.reconcile_order(bracket.targets[0].order_id)
        manager.reconcile_order(bracket.targets[1].order_id)
        assert manager.get_order(bracket.stop.order_id).state is OrderState.CANCELLED

    assert [fill.venue_order_id for fill in fills] == [
        bracket.targets[0].order_id,
        bracket.targets[1].order_id,
    ]
    assert [fill.quantity for fill in fills] == [Decimal("3"), Decimal("3")]
    assert broker.positions() == []


def test_numeric_target_order_wins_when_available_position_is_limited() -> None:
    broker, clock = broker_fixture()
    place_order(
        broker,
        clock,
        "numeric-target-entry",
        side=Side.BUY,
        order_type=OrderType.MARKET,
        quantity=Decimal("1"),
    )
    broker.process_bar(bar(START + timedelta(minutes=1)))
    place_order(
        broker,
        clock,
        "numeric-target:target:10",
        side=Side.SELL,
        order_type=OrderType.LIMIT,
        quantity=Decimal("1"),
        limit_price=Decimal("105"),
        parent_order_id="numeric-target-entry",
        oco_group="numeric-target:exits",
    )
    place_order(
        broker,
        clock,
        "numeric-target:target:2",
        side=Side.SELL,
        order_type=OrderType.LIMIT,
        quantity=Decimal("1"),
        limit_price=Decimal("110"),
        parent_order_id="numeric-target-entry",
        oco_group="numeric-target:exits",
    )

    target_bar = bar(
        START + timedelta(minutes=2),
        open_="100",
        high="116",
        low="99",
        close="115",
    )
    clock.set(target_bar.timestamp)
    fills = broker.process_bar(target_bar)

    assert [fill.venue_order_id for fill in fills] == [
        "numeric-target:target:2"
    ]
    assert fills[0].quantity == Decimal("1")
    assert broker.positions() == []


def test_time_stop_market_on_open_fills_at_next_session_open() -> None:
    clock = ReplayClock(datetime(2026, 9, 22, 20, 0, tzinfo=UTC))
    broker, _ = broker_fixture(clock=clock)
    place_order(
        broker,
        clock,
        "time-stop-exit",
        side=Side.SELL,
        order_type=OrderType.MARKET,
        quantity=Decimal("5"),
        tif=TimeInForce.OPG,
    )

    fills = broker.process_bar(
        bar(
            datetime(2026, 9, 23, 13, 30, tzinfo=UTC),
            open_="102",
            high="103",
            low="101",
            close="102",
        )
    )

    assert len(fills) == 1
    assert fills[0].price == Decimal("102")
    assert fills[0].quantity == Decimal("5")


def test_opening_order_ignores_after_hours_bar_until_next_session_open() -> None:
    broker, clock = broker_fixture()
    session_open = datetime(2026, 9, 23, 13, 30, tzinfo=UTC)
    broker.process_bar(bar(session_open))

    last_before_order = session_open + timedelta(hours=8, minutes=15)
    minute_count = int((last_before_order - session_open).total_seconds() // 60)
    for minute in range(1, minute_count + 1):
        timestamp = session_open + timedelta(minutes=minute)
        clock.set(timestamp)
        broker.process_bar(bar(timestamp))

    place_order(
        broker,
        clock,
        "after-hours-time-stop",
        side=Side.SELL,
        order_type=OrderType.MARKET,
        quantity=Decimal("1"),
        tif=TimeInForce.OPG,
    )

    after_hours = last_before_order + timedelta(minutes=15)
    for minute in range(1, 16):
        timestamp = last_before_order + timedelta(minutes=minute)
        clock.set(timestamp)
        fills = broker.process_bar(bar(timestamp))
        if timestamp == after_hours:
            assert fills == ()

    assert broker.orders(START)[0].state is OrderState.ACCEPTED

    next_open = bar(
        datetime(2026, 9, 24, 13, 30, tzinfo=UTC),
        open_="102",
        high="103",
        low="101",
        close="102",
    )
    clock.set(next_open.timestamp)
    fills = broker.process_bar(next_open)

    assert len(fills) == 1
    assert fills[0].venue_order_id == "after-hours-time-stop"
    assert fills[0].price == Decimal("102")


def test_limit_that_is_never_touched_does_not_fill() -> None:
    broker, clock = broker_fixture()
    place_order(
        broker,
        clock,
        "untouched-limit",
        side=Side.BUY,
        order_type=OrderType.LIMIT,
        limit_price=Decimal("95"),
    )

    assert not broker.process_bar(
        bar(START + timedelta(minutes=1), open_="100", high="102", low="98", close="101")
    )
    state = broker.orders(START)[0]
    assert state.state is OrderState.ACCEPTED
    assert state.filled_quantity == Decimal("0")
    assert broker.fills(START) == []


def test_day_order_stays_working_through_its_session() -> None:
    broker, clock = broker_fixture()
    place_order(
        broker,
        clock,
        "day-limit-same-session",
        side=Side.BUY,
        order_type=OrderType.LIMIT,
        limit_price=Decimal("95"),
    )

    assert broker.process_bar(
        bar(START + timedelta(minutes=1), open_="100", high="102", low="98", close="101")
    ) == ()
    assert broker.orders(START)[0].state is OrderState.ACCEPTED

    fill_bar = bar(
        START + timedelta(minutes=2),
        open_="96",
        high="97",
        low="94",
        close="95",
    )
    clock.set(fill_bar.timestamp)
    fills = broker.process_bar(fill_bar)

    assert len(fills) == 1
    assert fills[0].venue_order_id == "day-limit-same-session"
    assert broker.orders(START)[0].state is OrderState.FILLED


def test_day_order_expires_before_it_can_fill_next_session() -> None:
    broker, clock = broker_fixture()
    place_order(
        broker,
        clock,
        "day-limit-next-session",
        side=Side.BUY,
        order_type=OrderType.LIMIT,
        limit_price=Decimal("95"),
    )
    feed_session(broker, clock, START + timedelta(minutes=1))

    next_session_open = bar(
        datetime(2026, 9, 24, 13, 30, tzinfo=UTC),
        open_="94",
        high="96",
        low="93",
        close="95",
    )
    clock.set(next_session_open.timestamp)
    fills = broker.process_bar(next_session_open)

    assert fills == ()
    assert broker.orders(START)[0].state is OrderState.EXPIRED
    assert broker.fills(START) == []


def test_first_bar_must_be_the_exchange_session_open() -> None:
    broker, _ = broker_fixture()

    with pytest.raises(MissingBarError, match="session opening"):
        broker.process_bar(
            bar(datetime(2026, 9, 23, 13, 31, tzinfo=UTC))
        )


def test_missing_open_after_a_prior_session_is_refused() -> None:
    broker, clock = broker_fixture()
    feed_session(broker, clock, datetime(2026, 9, 23, 13, 30, tzinfo=UTC))

    with pytest.raises(MissingBarError, match="session opening"):
        broker.process_bar(
            bar(datetime(2026, 9, 24, 13, 31, tzinfo=UTC))
        )


def test_missing_entire_exchange_session_is_refused() -> None:
    broker, clock = broker_fixture()
    feed_session(broker, clock, datetime(2026, 9, 23, 13, 30, tzinfo=UTC))

    with pytest.raises(MissingBarError, match="Missing session bars"):
        broker.process_bar(
            bar(datetime(2026, 9, 25, 13, 30, tzinfo=UTC))
        )


def test_valid_open_bars_on_consecutive_sessions_are_accepted() -> None:
    broker, clock = broker_fixture()
    feed_session(broker, clock, datetime(2026, 9, 23, 13, 30, tzinfo=UTC))
    following = bar(datetime(2026, 9, 24, 13, 30, tzinfo=UTC))

    assert broker.process_bar(following) == ()


def test_missing_closing_bars_are_refused_before_the_next_session() -> None:
    broker, clock = broker_fixture()
    # 09:30-15:30 ET only: the last 29 minutes of 2026-09-23 never arrive.
    feed_session(broker, clock, datetime(2026, 9, 23, 13, 30, tzinfo=UTC), minutes=361)

    with pytest.raises(MissingBarError, match="Missing closing one-minute bars"):
        broker.process_bar(bar(datetime(2026, 9, 24, 13, 30, tzinfo=UTC)))


def test_early_close_session_ends_at_the_early_close() -> None:
    broker, clock = broker_fixture()
    # 2026-11-27 closes at 13:00 ET: 210 bars from 09:30 ET is the full session.
    feed_session(broker, clock, datetime(2026, 11, 27, 14, 30, tzinfo=UTC), minutes=210)

    assert broker.process_bar(bar(datetime(2026, 11, 30, 14, 30, tzinfo=UTC))) == ()


def test_after_hours_bar_does_not_trigger_a_resting_stop() -> None:
    broker, clock = broker_fixture()
    place_order(
        broker,
        clock,
        "gtc-stop",
        side=Side.SELL,
        order_type=OrderType.STOP,
        stop_price=Decimal("95"),
        tif=TimeInForce.GTC,
    )
    feed_session(broker, clock, START + timedelta(minutes=1))

    after_close = datetime(2026, 9, 23, 20, 0, tzinfo=UTC)
    clock.set(after_close)
    assert broker.process_bar(
        bar(after_close, open_="94", high="95", low="93", close="94")
    ) == ()
    assert broker.orders(START)[0].state is OrderState.ACCEPTED

    clock.set(datetime(2026, 9, 24, 13, 30, tzinfo=UTC))
    fills = broker.process_bar(
        bar(datetime(2026, 9, 24, 13, 30, tzinfo=UTC), open_="94", high="95", low="93", close="94")
    )
    assert [(fill.venue_order_id, fill.price) for fill in fills] == [("gtc-stop", Decimal("94"))]


def test_day_order_entered_after_the_close_works_the_next_session() -> None:
    clock = ReplayClock(datetime(2026, 9, 23, 21, 45, tzinfo=UTC))  # 17:45 ET EOD job
    broker, _ = broker_fixture(clock=clock)
    place_order(
        broker,
        clock,
        "next-day-entry",
        side=Side.BUY,
        order_type=OrderType.LIMIT,
        limit_price=Decimal("95"),
    )

    next_open = datetime(2026, 9, 24, 13, 30, tzinfo=UTC)
    feed_session(broker, clock, next_open, minutes=2)
    assert broker.orders(START)[0].state is OrderState.ACCEPTED

    touch = next_open + timedelta(minutes=2)
    clock.set(touch)
    fills = broker.process_bar(bar(touch, open_="96", high="97", low="94", close="95"))

    assert [(fill.venue_order_id, fill.price) for fill in fills] == [
        ("next-day-entry", Decimal("95"))
    ]


def test_day_order_entered_after_the_close_expires_after_the_next_session() -> None:
    clock = ReplayClock(datetime(2026, 9, 23, 21, 45, tzinfo=UTC))
    broker, _ = broker_fixture(clock=clock)
    place_order(
        broker,
        clock,
        "next-day-entry",
        side=Side.BUY,
        order_type=OrderType.LIMIT,
        limit_price=Decimal("95"),
    )
    feed_session(broker, clock, datetime(2026, 9, 24, 13, 30, tzinfo=UTC))
    assert broker.orders(START)[0].state is OrderState.ACCEPTED

    later = datetime(2026, 9, 25, 13, 30, tzinfo=UTC)
    clock.set(later)
    assert broker.process_bar(bar(later, open_="94", high="96", low="93", close="95")) == ()
    assert broker.orders(START)[0].state is OrderState.EXPIRED


SESSION_CLOSE = datetime(2026, 9, 23, 20, 0, tzinfo=UTC)


def _unfilled_day_limit_through_the_session(tif: TimeInForce = TimeInForce.DAY):
    broker, clock = broker_fixture()
    place_order(
        broker,
        clock,
        "unfilled-day",
        side=Side.BUY,
        order_type=OrderType.LIMIT,
        limit_price=Decimal("95"),
        tif=tif,
    )
    feed_session(broker, clock, START + timedelta(minutes=1))
    return broker, clock


def test_day_order_expires_at_the_close_by_the_clock_without_a_bar() -> None:
    broker, clock = _unfilled_day_limit_through_the_session()

    clock.set(SESSION_CLOSE)  # the EOD closing sweep: no bar at or after the close

    [state] = broker.orders(START)
    assert state.state is OrderState.EXPIRED
    assert state.updated_at == SESSION_CLOSE


def test_day_order_expiry_is_stamped_at_the_close_when_observed_later() -> None:
    broker, clock = _unfilled_day_limit_through_the_session()

    clock.set(datetime(2026, 9, 23, 21, 45, tzinfo=UTC))

    [state] = broker.orders(START)
    assert state.state is OrderState.EXPIRED
    assert state.updated_at == SESSION_CLOSE


def test_day_order_is_still_working_one_minute_before_the_close() -> None:
    broker, clock = _unfilled_day_limit_through_the_session()

    assert clock.now_utc() == SESSION_CLOSE - timedelta(minutes=1)
    assert broker.orders(START)[0].state is OrderState.ACCEPTED


def test_day_order_is_not_expired_by_the_clock_for_bars_never_simulated() -> None:
    broker, clock = broker_fixture()
    place_order(
        broker,
        clock,
        "unsimulated-day",
        side=Side.BUY,
        order_type=OrderType.LIMIT,
        limit_price=Decimal("95"),
    )
    feed_session(broker, clock, START + timedelta(minutes=1), minutes=389)

    # The 15:59 bar was never fed, so nothing proves the order went unfilled (I5).
    clock.set(SESSION_CLOSE)
    assert broker.orders(START)[0].state is OrderState.ACCEPTED


def test_day_order_entered_after_the_close_survives_until_the_next_close() -> None:
    broker, clock = broker_fixture()
    feed_session(broker, clock, START + timedelta(minutes=1))
    clock.set(datetime(2026, 9, 23, 21, 45, tzinfo=UTC))  # 17:45 ET EOD job
    place_order(
        broker,
        clock,
        "next-day-entry",
        side=Side.BUY,
        order_type=OrderType.LIMIT,
        limit_price=Decimal("95"),
    )
    assert broker.orders(START)[0].state is OrderState.ACCEPTED

    next_open = datetime(2026, 9, 24, 13, 30, tzinfo=UTC)
    feed_session(broker, clock, next_open)
    assert broker.orders(START)[0].state is OrderState.ACCEPTED

    next_close = next_open + timedelta(minutes=390)
    clock.set(next_close)
    [state] = broker.orders(START)
    assert state.state is OrderState.EXPIRED
    assert state.updated_at == next_close


def test_gtc_order_is_not_expired_by_the_clock() -> None:
    broker, clock = _unfilled_day_limit_through_the_session(TimeInForce.GTC)

    clock.set(SESSION_CLOSE)
    assert broker.orders(START)[0].state is OrderState.ACCEPTED
    clock.set(datetime(2026, 9, 24, 21, 45, tzinfo=UTC))
    assert broker.orders(START)[0].state is OrderState.ACCEPTED


def test_cancel_after_the_close_sees_the_order_expired() -> None:
    broker, clock = _unfilled_day_limit_through_the_session()

    clock.set(SESSION_CLOSE)
    ack = broker.cancel("unfilled-day")

    assert ack.status == "REJECTED"
    assert ack.message == "Order is EXPIRED"
    [state] = broker.orders(START)
    assert state.state is OrderState.EXPIRED
    assert state.updated_at == SESSION_CLOSE


def test_opening_order_expires_when_its_session_open_was_never_simulated() -> None:
    clock = ReplayClock(datetime(2026, 9, 22, 20, 30, tzinfo=UTC))
    broker, _ = broker_fixture(clock=clock)
    place_order(
        broker,
        clock,
        "stale-opening",
        side=Side.SELL,
        order_type=OrderType.MARKET,
        tif=TimeInForce.OPG,
    )

    # The AAPL stream starts on 09-24, after the 09-23 open this order was for.
    later_open = datetime(2026, 9, 24, 13, 30, tzinfo=UTC)
    clock.set(later_open)
    assert broker.process_bar(bar(later_open)) == ()
    assert broker.orders(START)[0].state is OrderState.EXPIRED


def test_missing_bar_is_refused_and_never_filled_from_last_price() -> None:
    broker, clock = broker_fixture()
    first = bar(START + timedelta(minutes=1))
    broker.process_bar(first)
    place_order(
        broker,
        clock,
        "missing-bar-limit",
        side=Side.BUY,
        order_type=OrderType.LIMIT,
        limit_price=Decimal("95"),
    )

    with pytest.raises(MissingBarError, match="without an observed bar"):
        broker.process_bar(None)
    with pytest.raises(MissingBarError, match="Missing one-minute bar"):
        broker.process_bar(
            bar(
                START + timedelta(minutes=3),
                open_="96",
                high="97",
                low="94",
                close="95",
            )
        )

    assert broker.fills(START) == []
    state = broker.orders(START)[0]
    assert state.state is OrderState.ACCEPTED
    assert state.filled_quantity == Decimal("0")


def test_opening_order_refuses_a_missing_opening_bar() -> None:
    broker, clock = broker_fixture()
    place_order(
        broker,
        clock,
        "missed-opening",
        side=Side.BUY,
        order_type=OrderType.MARKET,
        tif=TimeInForce.OPG,
    )

    late_open_bar = bar(
        datetime(2026, 9, 23, 13, 31, tzinfo=UTC),
        open_="100",
        high="101",
        low="99",
        close="100",
    )
    clock.set(late_open_bar.timestamp)
    with pytest.raises(MissingBarError, match="session opening"):
        broker.process_bar(late_open_bar)
    assert broker.orders(START)[0].state is OrderState.ACCEPTED
    assert broker.fills(START) == []


def test_gtc_bracket_stop_survives_the_close_and_fires_next_session(tmp_path: Path) -> None:
    clock = ReplayClock()
    broker, _ = broker_fixture(clock=clock)
    intent = OrderIntent(
        intent_id="overnight-swing",
        account_id=ACCOUNT,
        instrument=INSTRUMENT,
        side=Side.BUY,
        quantity_rule="fixed_4",
        entry_price=Decimal("100"),
        stop_loss=Decimal("95"),
        profit_targets=(Decimal("120"),),
        reason="Known-answer overnight stop",
        command_id="overnight-swing-command",
    )

    with Ledger(tmp_path / "ledger.db") as ledger:
        manager = OrderManager(broker, clock, ledger)
        bracket = manager.create_bracket(intent, Decimal("4"))
        manager.submit(bracket.entry)

        open_at = START + timedelta(minutes=1)
        clock.set(open_at)
        broker.process_bar(bar(open_at))
        manager.reconcile_order(bracket.entry.order_id)
        feed_session(broker, clock, open_at + timedelta(minutes=1), minutes=389)

        next_open = datetime(2026, 9, 24, 13, 30, tzinfo=UTC)
        clock.set(next_open)
        fills = broker.process_bar(
            bar(next_open, open_="93", high="94", low="92", close="93")
        )

        assert [(fill.venue_order_id, fill.price) for fill in fills] == [
            (bracket.stop.order_id, Decimal("93"))
        ]
        manager.reconcile_order(bracket.stop.order_id)
        assert manager.get_order(bracket.stop.order_id).state is OrderState.FILLED

    assert broker.positions() == []


def test_exit_cannot_close_another_brackets_shares_in_the_same_symbol() -> None:
    broker, clock = broker_fixture()
    for entry in ("bracket-a:entry", "bracket-b:entry"):
        place_order(
            broker, clock, entry, side=Side.BUY, order_type=OrderType.MARKET, quantity=Decimal("3")
        )
    entry_bar = bar(START + timedelta(minutes=1))
    clock.set(entry_bar.timestamp)
    broker.process_bar(entry_bar)

    # Bracket A's target filled at 105; the OMS has not yet cancelled A's stop.
    place_order(
        broker, clock, "bracket-a:target:1", side=Side.SELL, order_type=OrderType.LIMIT,
        quantity=Decimal("3"), limit_price=Decimal("105"),
        parent_order_id="bracket-a:entry", oco_group="bracket-a:exits",
    )
    place_order(
        broker, clock, "bracket-a:stop", side=Side.SELL, order_type=OrderType.STOP,
        quantity=Decimal("3"), stop_price=Decimal("95"),
        parent_order_id="bracket-a:entry", oco_group="bracket-a:exits",
    )
    target_bar = bar(START + timedelta(minutes=2), open_="104", high="106", low="103", close="105")
    clock.set(target_bar.timestamp)
    assert [fill.venue_order_id for fill in broker.process_bar(target_bar)] == [
        "bracket-a:target:1"
    ]

    stop_bar = bar(START + timedelta(minutes=3), open_="96", high="96", low="94", close="95")
    clock.set(stop_bar.timestamp)

    assert broker.process_bar(stop_bar) == ()
    assert [(p.instrument, p.quantity) for p in broker.positions()] == [
        (INSTRUMENT, Decimal("3"))
    ]


def test_exit_fills_its_own_brackets_open_quantity() -> None:
    broker, clock = broker_fixture()
    place_order(
        broker, clock, "solo:entry", side=Side.BUY, order_type=OrderType.MARKET, quantity=Decimal("3")
    )
    entry_bar = bar(START + timedelta(minutes=1))
    clock.set(entry_bar.timestamp)
    broker.process_bar(entry_bar)
    place_order(
        broker, clock, "solo:stop", side=Side.SELL, order_type=OrderType.STOP,
        quantity=Decimal("3"), stop_price=Decimal("95"),
        parent_order_id="solo:entry", oco_group="solo:exits",
    )

    stop_bar = bar(START + timedelta(minutes=2), open_="96", high="96", low="94", close="95")
    clock.set(stop_bar.timestamp)
    fills = broker.process_bar(stop_bar)

    assert [(fill.venue_order_id, fill.quantity, fill.price) for fill in fills] == [
        ("solo:stop", Decimal("3"), Decimal("95"))
    ]
    assert broker.positions() == []


def test_exit_without_a_parent_held_by_the_venue_is_refused() -> None:
    broker, clock = broker_fixture()

    with pytest.raises(ValueError, match="not held by this SimBroker"):
        place_order(
            broker, clock, "orphan:stop", side=Side.SELL, order_type=OrderType.STOP,
            stop_price=Decimal("95"), parent_order_id="orphan:entry",
        )
    assert broker.orders(START) == []


def _entry_bar_bracket(
    tmp_path: Path,
    entry_bar_prices: dict[str, str],
    *,
    side: Side = Side.BUY,
) -> tuple[OrderManager, object, Ledger, SimBroker, ReplayClock]:
    clock = ReplayClock()
    broker, _ = broker_fixture(clock=clock)
    long = side is Side.BUY
    intent = OrderIntent(
        intent_id="entry-bar",
        account_id=ACCOUNT,
        instrument=INSTRUMENT,
        side=side,
        quantity_rule="fixed_2",
        entry_price=Decimal("100"),
        stop_loss=Decimal("95") if long else Decimal("105"),
        profit_targets=(Decimal("105") if long else Decimal("95"),),
        reason="Known-answer entry-bar exit",
        command_id="entry-bar-command",
    )
    ledger = Ledger(tmp_path / "ledger.db")
    ledger.open()
    manager = OrderManager(broker, clock, ledger)
    bracket = manager.create_bracket(intent, Decimal("2"))
    manager.submit(bracket.entry)
    entry_bar = bar(START + timedelta(minutes=1), **entry_bar_prices)
    clock.set(entry_bar.timestamp)
    broker.process_bar(entry_bar)
    manager.reconcile_order(bracket.entry.order_id)
    return manager, bracket, ledger, broker, clock


def test_long_stop_touched_in_the_entry_bar_fills_in_that_bar(tmp_path: Path) -> None:
    manager, bracket, ledger, broker, _ = _entry_bar_bracket(
        tmp_path, {"open_": "101", "high": "101", "low": "94", "close": "96"}
    )
    try:
        manager.reconcile_order(bracket.stop.order_id)
        assert [(f.venue_order_id, f.price, f.filled_at) for f in broker.fills(START)] == [
            (bracket.entry.order_id, Decimal("100"), START + timedelta(minutes=1)),
            (bracket.stop.order_id, Decimal("95"), START + timedelta(minutes=1)),
        ]
        assert manager.get_order(bracket.stop.order_id).state is OrderState.FILLED
        assert manager.get_order(bracket.targets[0].order_id).state is OrderState.CANCELLED
    finally:
        ledger.close()
    assert broker.positions() == []


def test_short_stop_touched_in_the_entry_bar_fills_in_that_bar(tmp_path: Path) -> None:
    manager, bracket, ledger, broker, _ = _entry_bar_bracket(
        tmp_path,
        {"open_": "99", "high": "106", "low": "98", "close": "104"},
        side=Side.SELL,
    )
    try:
        stop_fills = [f for f in broker.fills(START) if f.venue_order_id == bracket.stop.order_id]
        assert [(f.side, f.price) for f in stop_fills] == [(Side.BUY, Decimal("105"))]
    finally:
        ledger.close()
    assert broker.positions() == []


def test_entry_already_through_the_stop_exits_at_the_entry_price(tmp_path: Path) -> None:
    manager, bracket, ledger, broker, _ = _entry_bar_bracket(
        tmp_path, {"open_": "93", "high": "94", "low": "92", "close": "93"}
    )
    try:
        assert [(f.venue_order_id, f.price) for f in broker.fills(START)] == [
            (bracket.entry.order_id, Decimal("93")),
            (bracket.stop.order_id, Decimal("93")),
        ]
    finally:
        ledger.close()


def test_target_reached_in_the_entry_bar_waits_for_a_later_bar(tmp_path: Path) -> None:
    manager, bracket, ledger, broker, clock = _entry_bar_bracket(
        tmp_path, {"open_": "99", "high": "106", "low": "98", "close": "104"}
    )
    try:
        assert [f.venue_order_id for f in broker.fills(START)] == [bracket.entry.order_id]
        assert manager.get_order(bracket.stop.order_id).state is OrderState.ACCEPTED

        next_bar = bar(START + timedelta(minutes=2), open_="104", high="106", low="103", close="105")
        clock.set(next_bar.timestamp)
        fills = broker.process_bar(next_bar)
        assert [(f.venue_order_id, f.price) for f in fills] == [
            (bracket.targets[0].order_id, Decimal("105"))
        ]
    finally:
        ledger.close()


def test_exit_submitted_after_later_bars_is_rejected_not_silently_late(tmp_path: Path) -> None:
    clock = ReplayClock()
    broker, _ = broker_fixture(clock=clock)
    intent = OrderIntent(
        intent_id="late-exit",
        account_id=ACCOUNT,
        instrument=INSTRUMENT,
        side=Side.BUY,
        quantity_rule="fixed_2",
        entry_price=Decimal("100"),
        stop_loss=Decimal("95"),
        profit_targets=(Decimal("105"),),
        reason="Known-answer late reconciliation",
        command_id="late-exit-command",
    )

    with Ledger(tmp_path / "ledger.db") as ledger:
        manager = OrderManager(broker, clock, ledger)
        bracket = manager.create_bracket(intent, Decimal("2"))
        manager.submit(bracket.entry)
        entry_bar = bar(START + timedelta(minutes=1), open_="101", high="101", low="94", close="96")
        clock.set(entry_bar.timestamp)
        broker.process_bar(entry_bar)
        # The runner processes another bar before reconciling the entry.
        later = START + timedelta(minutes=2)
        clock.set(later)
        broker.process_bar(bar(later, open_="96", high="97", low="93", close="94"))

        with pytest.raises(OrderManagementError, match="Protective stop"):
            manager.reconcile_order(bracket.entry.order_id)

        assert manager.get_order(bracket.stop.order_id).state is OrderState.REJECTED
    stop_state = next(
        state for state in broker.orders(START) if state.venue_order_id == bracket.stop.order_id
    )
    assert stop_state.state is OrderState.REJECTED
    assert [fill.venue_order_id for fill in broker.fills(START)] == [bracket.entry.order_id]


def test_rejected_late_exit_stays_rejected_when_resubmitted() -> None:
    broker, clock = broker_fixture()
    place_order(broker, clock, "late:entry", side=Side.BUY, order_type=OrderType.MARKET)
    feed_session(broker, clock, START + timedelta(minutes=1), minutes=2)

    for _ in range(2):
        ack = broker.submit(
            VenueOrder(
                venue_order_id="late:stop",
                instrument=INSTRUMENT,
                order_type=OrderType.STOP,
                side=Side.SELL,
                quantity=Decimal("1"),
                submitted_at=START,
                stop_price=Decimal("95"),
                allocations=(VenueOrderAllocation("late:stop", ACCOUNT, Decimal("1")),),
                parent_order_id="late:entry",
            )
        )
        assert ack.status == "REJECTED"
        state = next(s for s in broker.orders(START) if s.venue_order_id == "late:stop")
        assert state.state is OrderState.REJECTED


# -- restore: an empty simulator reloaded from the ledger's fold ---------------------


def _venue_order(
    order_id: str,
    *,
    side: Side,
    order_type: OrderType,
    quantity: str = "2",
    limit_price: str | None = None,
    stop_price: str | None = None,
    parent_order_id: str | None = None,
    oco_group: str | None = None,
    submitted_at: datetime = START,
) -> VenueOrder:
    return VenueOrder(
        venue_order_id=order_id,
        instrument=INSTRUMENT,
        order_type=order_type,
        side=side,
        quantity=Decimal(quantity),
        submitted_at=submitted_at,
        tif=TimeInForce.GTC,
        limit_price=None if limit_price is None else Decimal(limit_price),
        stop_price=None if stop_price is None else Decimal(stop_price),
        allocations=(VenueOrderAllocation(order_id, ACCOUNT, Decimal(quantity)),),
        parent_order_id=parent_order_id,
        oco_group=oco_group,
    )


def _venue_fill(order_id: str, number: int, quantity: str, side: Side) -> VenueFill:
    return VenueFill(
        venue_fill_id=f"{order_id}:fill:{number}",
        venue_order_id=order_id,
        instrument=INSTRUMENT,
        quantity=Decimal(quantity),
        price=Decimal("100"),
        filled_at=START + timedelta(minutes=1),
        side=side,
    )


def _held_bracket() -> tuple[list[tuple[VenueOrder, OrderState]], list[VenueFill]]:
    """A filled 2-share entry whose stop has already sold 1 share."""
    entry = _venue_order("b:entry", side=Side.BUY, order_type=OrderType.LIMIT, limit_price="100")
    stop = _venue_order(
        "b:stop",
        side=Side.SELL,
        order_type=OrderType.STOP,
        stop_price="95",
        parent_order_id="b:entry",
        oco_group="b:exits",
    )
    return (
        [(stop, OrderState.PARTIALLY_FILLED), (entry, OrderState.FILLED)],
        [_venue_fill("b:entry", 1, "2", Side.BUY), _venue_fill("b:stop", 1, "1", Side.SELL)],
    )


def _position(quantity: str) -> VenuePosition:
    return VenuePosition(
        instrument=INSTRUMENT,
        quantity=Decimal(quantity),
        avg_price=Decimal("100"),
        as_of=START + timedelta(minutes=1),
    )


def test_restored_bracket_keeps_working_and_continues_fill_numbering() -> None:
    clock = ReplayClock(datetime(2026, 9, 24, 13, 29, tzinfo=UTC))
    broker, _ = broker_fixture(clock=clock)
    orders, fills = _held_bracket()
    broker.restore(orders, fills, [_position("1")])

    session_open = datetime(2026, 9, 24, 13, 30, tzinfo=UTC)
    clock.set(session_open)
    produced = broker.process_bar(bar(session_open, open_="96", high="96", low="94", close="95"))

    # The stop sells the one share still held, under a fresh id: ":fill:1" is taken.
    assert [(fill.venue_fill_id, fill.quantity) for fill in produced] == [
        ("b:stop:fill:2", Decimal("1"))
    ]
    assert {state.venue_order_id: state.state for state in broker.orders(START)} == {
        "b:entry": OrderState.FILLED,
        "b:stop": OrderState.FILLED,
    }
    assert broker.positions() == []


def test_restore_refuses_a_simulator_that_already_holds_orders() -> None:
    broker, clock = broker_fixture()
    place_order(
        broker, clock, "live", side=Side.BUY, order_type=OrderType.LIMIT,
        limit_price=Decimal("100"),
    )
    orders, fills = _held_bracket()
    with pytest.raises(SimBrokerError, match="empty SimBroker"):
        broker.restore(orders, fills, [])


@pytest.mark.parametrize("state", [OrderState.NEW, OrderState.SUBMITTED, OrderState.PENDING_UNKNOWN])
def test_restore_refuses_orders_the_venue_never_confirmed(state: OrderState) -> None:
    broker, _ = broker_fixture()
    entry = _venue_order("e", side=Side.BUY, order_type=OrderType.LIMIT, limit_price="100")
    with pytest.raises(SimBrokerError, match=f"state {state.value}"):
        broker.restore([(entry, state)], [], [])


def test_restore_refuses_an_order_given_twice() -> None:
    broker, _ = broker_fixture()
    entry = _venue_order("e", side=Side.BUY, order_type=OrderType.LIMIT, limit_price="100")
    with pytest.raises(SimBrokerError, match="restored twice"):
        broker.restore([(entry, OrderState.ACCEPTED), (entry, OrderState.ACCEPTED)], [], [])


def test_restore_refuses_a_fill_for_an_order_it_was_not_given() -> None:
    broker, _ = broker_fixture()
    with pytest.raises(SimBrokerError, match="unrestored order"):
        broker.restore([], [_venue_fill("ghost", 1, "1", Side.BUY)], [])


def test_restore_refuses_a_fill_id_outside_the_simulator_numbering() -> None:
    broker, _ = broker_fixture()
    entry = _venue_order("e", side=Side.BUY, order_type=OrderType.LIMIT, limit_price="100")
    foreign = replace(_venue_fill("e", 1, "2", Side.BUY), venue_fill_id="exec-123")
    with pytest.raises(SimBrokerError, match="not a SimBroker fill id"):
        broker.restore([(entry, OrderState.FILLED)], [foreign], [])


def test_restore_refuses_a_fill_on_the_wrong_side() -> None:
    broker, _ = broker_fixture()
    entry = _venue_order("e", side=Side.BUY, order_type=OrderType.LIMIT, limit_price="100")
    with pytest.raises(SimBrokerError, match="does not match order"):
        broker.restore([(entry, OrderState.FILLED)], [_venue_fill("e", 1, "2", Side.SELL)], [])


@pytest.mark.parametrize(
    ("state", "filled"),
    [
        (OrderState.ACCEPTED, "1"),
        (OrderState.PARTIALLY_FILLED, "2"),
        (OrderState.FILLED, "1"),
        (OrderState.CANCELLED, "3"),
    ],
)
def test_restore_refuses_a_state_its_fills_contradict(state: OrderState, filled: str) -> None:
    broker, _ = broker_fixture()
    entry = _venue_order("e", side=Side.BUY, order_type=OrderType.LIMIT, limit_price="100")
    fills = [_venue_fill("e", number + 1, "1", Side.BUY) for number in range(int(filled))]
    with pytest.raises(SimBrokerError, match=f"is {state.value} with {filled}"):
        broker.restore([(entry, state)], fills, [])


def test_restore_refuses_a_position_given_twice() -> None:
    broker, _ = broker_fixture()
    orders, fills = _held_bracket()
    with pytest.raises(SimBrokerError, match="Position AAPL restored twice"):
        broker.restore(orders, fills, [_position("1"), _position("1")])

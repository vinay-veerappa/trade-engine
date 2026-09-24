"""Known-answer tests for the deterministic equities paper venue."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from trade_engine.domain.instruments import Equity, Side
from trade_engine.domain.orders import OrderState, OrderType, TimeInForce
from trade_engine.domain.signals import OrderIntent
from trade_engine.interfaces.broker import (
    OrderChanges,
    VenueOrder,
    VenueOrderAllocation,
)
from trade_engine.interfaces.clock import Clock
from trade_engine.interfaces.market_data import Bar
from trade_engine.ledger import Ledger
from trade_engine.oms.manager import OrderManager
from trade_engine.sim import MissingBarError, SimBroker

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

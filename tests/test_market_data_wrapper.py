"""Tests for StampingMarketDataWrapper and StaleData enforcement (Architecture §4.8, I5, I7)."""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from trade_engine.clock import ReplayClock
from trade_engine.domain.instruments import Equity, OptionContract
from trade_engine.interfaces.market_data import (
    Bar,
    CorporateAction,
    MarketData,
    OptionQuote,
    Quote,
    StaleData,
    StaleDataError,
)
from trade_engine.market_data import MarketDataWrapper, StampingMarketDataWrapper


class FakeProvider:
    """Mock underlying provider."""

    def __init__(self) -> None:
        self._bars: list[Bar] = []
        self._quote: Quote | None = None
        self._chain: list[OptionQuote] = []
        self._actions: list[CorporateAction] = []

    def bars(self, instrument, tf, start, end, max_age_seconds):
        return self._bars

    def quote(self, instrument, max_age_seconds):
        if self._quote is None:
            raise RuntimeError("No quote configured")
        return self._quote

    def chain(self, underlying, expiry_start, expiry_end, max_age_seconds):
        return self._chain

    def corporate_actions(self, symbol, max_age_seconds):
        return self._actions


def test_market_data_wrapper_protocol_compliance() -> None:
    t0 = datetime(2026, 9, 23, 14, 30, tzinfo=timezone.utc)
    clock = ReplayClock(t0)
    provider = FakeProvider()
    wrapper = StampingMarketDataWrapper(provider, clock=clock)
    assert isinstance(wrapper, MarketData)


def test_quote_fresh_and_stale_raises() -> None:
    """Acceptance: a stale answer raises StaleDataError / StaleData."""
    t0 = datetime(2026, 9, 23, 14, 30, 0, tzinfo=timezone.utc)
    clock = ReplayClock(t0)
    provider = FakeProvider()
    wrapper = StampingMarketDataWrapper(provider, clock=clock)

    eq = Equity("SPY")

    # 1. Fresh quote (age 1.0s <= max_age 5.0s) -> passes
    q_as_of = t0 - timedelta(seconds=1)
    provider._quote = Quote(
        instrument=eq,
        bid=Decimal("500.00"),
        ask=Decimal("500.10"),
        bid_size=Decimal("100"),
        ask_size=Decimal("200"),
        as_of=q_as_of,
    )
    result = wrapper.quote(eq, max_age_seconds=5.0)
    assert result.as_of == q_as_of
    assert result.mid == Decimal("500.05")

    # 2. Advance clock by 10s: now age is 11s > max_age 5s -> raises StaleDataError
    clock.advance_by(10.0)
    with pytest.raises(StaleDataError, match="is stale: age 11.000s exceeds allowed max_age 5.000s"):
        wrapper.quote(eq, max_age_seconds=5.0)

    # 3. Verify StaleData is an alias of StaleDataError
    with pytest.raises(StaleData):
        wrapper.quote(eq, max_age_seconds=5.0)


def test_bars_fresh_and_stale_raises() -> None:
    t0 = datetime(2026, 9, 23, 14, 30, 0, tzinfo=timezone.utc)
    clock = ReplayClock(t0)
    provider = FakeProvider()
    wrapper = StampingMarketDataWrapper(provider, clock=clock)

    eq = Equity("AAPL")
    bar_ts = t0 - timedelta(minutes=5)
    provider._bars = [
        Bar(
            instrument=eq,
            timestamp=bar_ts,
            open=Decimal("220.00"),
            high=Decimal("221.00"),
            low=Decimal("219.50"),
            close=Decimal("220.50"),
            volume=Decimal("50000"),
            as_of=t0 - timedelta(seconds=30),  # 30 seconds old
        )
    ]

    # Fresh within 60s
    res = wrapper.bars(
        instrument=eq,
        tf="1m",
        start=bar_ts,
        end=t0,
        max_age_seconds=60.0,
    )
    assert len(res) == 1

    # Stale when caller only accepts max_age 10s
    with pytest.raises(StaleDataError, match="is stale"):
        wrapper.bars(
            instrument=eq,
            tf="1m",
            start=bar_ts,
            end=t0,
            max_age_seconds=10.0,
        )


def test_option_chain_fresh_and_stale_raises() -> None:
    t0 = datetime(2026, 9, 23, 14, 30, 0, tzinfo=timezone.utc)
    clock = ReplayClock(t0)
    provider = FakeProvider()
    wrapper = StampingMarketDataWrapper(provider, clock=clock)

    contract = OptionContract("SPY", date(2026, 10, 16), Decimal("500"), "C")
    provider._chain = [
        OptionQuote(
            contract=contract,
            bid=Decimal("5.20"),
            ask=Decimal("5.40"),
            bid_size=Decimal("10"),
            ask_size=Decimal("10"),
            as_of=t0 - timedelta(seconds=20),
        )
    ]

    # Passes with max_age 30s
    res = wrapper.chain("SPY", date(2026, 10, 1), date(2026, 10, 30), max_age_seconds=30.0)
    assert len(res) == 1

    # Raises when max_age is 15s
    with pytest.raises(StaleDataError, match="is stale"):
        wrapper.chain("SPY", date(2026, 10, 1), date(2026, 10, 30), max_age_seconds=15.0)


def test_corporate_actions_fresh_and_stale_raises() -> None:
    t0 = datetime(2026, 9, 23, 14, 30, 0, tzinfo=timezone.utc)
    clock = ReplayClock(t0)
    provider = FakeProvider()
    wrapper = StampingMarketDataWrapper(provider, clock=clock)

    provider._actions = [
        CorporateAction(
            symbol="AAPL",
            action_type="DIVIDEND",
            effective_date=date(2026, 10, 1),
            as_of=t0 - timedelta(seconds=50),
        )
    ]

    res = wrapper.corporate_actions("AAPL", max_age_seconds=100.0)
    assert len(res) == 1

    with pytest.raises(StaleDataError, match="is stale"):
        wrapper.corporate_actions("AAPL", max_age_seconds=20.0)


def test_future_timestamp_rejected_as_lookahead() -> None:
    """Refuse future timestamps (I5)."""
    t0 = datetime(2026, 9, 23, 14, 30, 0, tzinfo=timezone.utc)
    clock = ReplayClock(t0)
    provider = FakeProvider()
    wrapper = StampingMarketDataWrapper(provider, clock=clock)

    eq = Equity("SPY")
    future_as_of = t0 + timedelta(seconds=60)  # 60s in the future
    provider._quote = Quote(
        instrument=eq,
        bid=Decimal("500.00"),
        ask=Decimal("500.10"),
        bid_size=Decimal("100"),
        ask_size=Decimal("200"),
        as_of=future_as_of,
    )

    with pytest.raises(ValueError, match="timestamp is in the future"):
        wrapper.quote(eq, max_age_seconds=10.0)


def test_missing_as_of_rejected() -> None:
    """Finding 1: Data items missing as_of must be refused (no inventing timestamps)."""
    t0 = datetime(2026, 9, 23, 14, 30, 0, tzinfo=timezone.utc)
    clock = ReplayClock(t0)

    class RawQuoteItem:
        def __init__(self, eq):
            self.instrument = eq
            self.bid = Decimal("100")
            self.ask = Decimal("101")
            self.bid_size = Decimal("10")
            self.ask_size = Decimal("10")
            self.as_of = None

    class RawProvider:
        def quote(self, instrument, max_age_seconds):
            return RawQuoteItem(instrument)

    wrapper = StampingMarketDataWrapper(RawProvider(), clock=clock)
    with pytest.raises(ValueError, match="has no as_of timestamp"):
        wrapper.quote(Equity("TEST"), max_age_seconds=5.0)


def test_max_age_nan_and_inf_rejected() -> None:
    """Finding 2: max_age_seconds of NaN, inf, or <=0 must be rejected."""
    t0 = datetime(2026, 9, 23, 14, 30, 0, tzinfo=timezone.utc)
    clock = ReplayClock(t0)
    provider = FakeProvider()
    wrapper = StampingMarketDataWrapper(provider, clock=clock)
    eq = Equity("SPY")

    for bad_age in [float("nan"), float("inf"), float("-inf"), 0.0, -10.0]:
        with pytest.raises(ValueError, match="finite positive number"):
            wrapper.quote(eq, max_age_seconds=bad_age)


def test_bars_lookahead_and_future_bars_rejected() -> None:
    """Finding 3: Bars from future or requests with start in the future must be rejected."""
    t0 = datetime(2026, 9, 23, 14, 30, 0, tzinfo=timezone.utc)
    clock = ReplayClock(t0)
    provider = FakeProvider()
    wrapper = StampingMarketDataWrapper(provider, clock=clock)
    eq = Equity("SPY")

    # 1. Start in the future relative to clock
    future_start = t0 + timedelta(minutes=10)
    future_end = t0 + timedelta(minutes=20)
    with pytest.raises(ValueError, match="is in the future relative to clock"):
        wrapper.bars(eq, "1m", future_start, future_end, max_age_seconds=60.0)

    # 2. Returned bar has timestamp in future relative to clock
    past_start = t0 - timedelta(minutes=10)
    provider._bars = [
        Bar(
            instrument=eq,
            timestamp=t0 + timedelta(minutes=5),  # 5 minutes in future!
            open=Decimal("500"),
            high=Decimal("501"),
            low=Decimal("499"),
            close=Decimal("500.5"),
            volume=Decimal("100"),
            as_of=t0,
        )
    ]
    with pytest.raises(ValueError, match="Bar timestamp .* is in the future"):
        wrapper.bars(eq, "1m", past_start, t0 + timedelta(minutes=10), max_age_seconds=60.0)


def test_bars_start_after_end_rejected() -> None:
    """Finding 8: start > end in bars() must be rejected."""
    t0 = datetime(2026, 9, 23, 14, 30, 0, tzinfo=timezone.utc)
    clock = ReplayClock(t0)
    provider = FakeProvider()
    wrapper = StampingMarketDataWrapper(provider, clock=clock)
    eq = Equity("SPY")

    t_start = t0 - timedelta(minutes=5)
    t_end = t0 - timedelta(minutes=10)
    with pytest.raises(ValueError, match="cannot be after end datetime"):
        wrapper.bars(eq, "1m", t_start, t_end, max_age_seconds=60.0)


def test_chain_expiry_start_after_expiry_end_rejected() -> None:
    """Finding 8: expiry_start > expiry_end in chain() must be rejected."""
    t0 = datetime(2026, 9, 23, 14, 30, 0, tzinfo=timezone.utc)
    clock = ReplayClock(t0)
    provider = FakeProvider()
    wrapper = StampingMarketDataWrapper(provider, clock=clock)

    with pytest.raises(ValueError, match="expiry_start .* cannot be after expiry_end"):
        wrapper.chain("SPY", date(2026, 10, 30), date(2026, 10, 1), max_age_seconds=60.0)


def test_future_tolerance_zero_slack() -> None:
    """Finding 7: Default future tolerance is 0.0s slack (zero lookahead)."""
    t0 = datetime(2026, 9, 23, 14, 30, 0, tzinfo=timezone.utc)
    clock = ReplayClock(t0)
    provider = FakeProvider()
    wrapper = StampingMarketDataWrapper(provider, clock=clock)

    eq = Equity("SPY")
    # Even 1 millisecond in the future raises ValueError
    provider._quote = Quote(
        instrument=eq,
        bid=Decimal("500.00"),
        ask=Decimal("500.10"),
        bid_size=Decimal("100"),
        ask_size=Decimal("200"),
        as_of=t0 + timedelta(milliseconds=1),
    )
    with pytest.raises(ValueError, match="timestamp is in the future"):
        wrapper.quote(eq, max_age_seconds=10.0)


def test_wrapper_requires_clock() -> None:
    provider = FakeProvider()
    with pytest.raises(ValueError, match="requires an injected Clock"):
        StampingMarketDataWrapper(provider, clock=None)  # type: ignore[arg-type]


def test_market_data_wrapper_alias() -> None:
    assert MarketDataWrapper is StampingMarketDataWrapper


def test_multi_bar_historical_series_freshness() -> None:
    """Historical bar series where older bars have past as_of, but latest bar is fresh."""
    t0 = datetime(2026, 9, 23, 14, 30, 0, tzinfo=timezone.utc)
    clock = ReplayClock(t0)
    eq = Equity("AAPL")

    class MultiBarProvider:
        def bars(self, instrument, tf, start, end, max_age_seconds):
            return [
                Bar(
                    eq,
                    t0 - timedelta(minutes=15),
                    Decimal("100"),
                    Decimal("101"),
                    Decimal("99"),
                    Decimal("100.5"),
                    Decimal("1000"),
                    as_of=t0 - timedelta(minutes=15),
                ),
                Bar(
                    eq,
                    t0 - timedelta(minutes=5),
                    Decimal("100.5"),
                    Decimal("102"),
                    Decimal("100"),
                    Decimal("101.5"),
                    Decimal("1000"),
                    as_of=t0 - timedelta(minutes=5),
                ),
                Bar(
                    eq,
                    t0 - timedelta(seconds=10),
                    Decimal("101.5"),
                    Decimal("103"),
                    Decimal("101"),
                    Decimal("102.5"),
                    Decimal("1000"),
                    as_of=t0 - timedelta(seconds=10),  # 10s old <= 60s
                ),
            ]

    wrapper = MarketDataWrapper(MultiBarProvider(), clock=clock)
    bars = wrapper.bars(eq, "1m", t0 - timedelta(minutes=20), t0, max_age_seconds=60.0)
    assert len(bars) == 3


def test_multi_bar_historical_series_stale_latest_bar() -> None:
    """Historical bar series where the newest bar is stale raises StaleDataError."""
    t0 = datetime(2026, 9, 23, 14, 30, 0, tzinfo=timezone.utc)
    clock = ReplayClock(t0)
    eq = Equity("AAPL")

    class StaleLatestBarProvider:
        def bars(self, instrument, tf, start, end, max_age_seconds):
            return [
                Bar(
                    eq,
                    t0 - timedelta(minutes=15),
                    Decimal("100"),
                    Decimal("101"),
                    Decimal("99"),
                    Decimal("100.5"),
                    Decimal("1000"),
                    as_of=t0 - timedelta(minutes=15),
                ),
                Bar(
                    eq,
                    t0 - timedelta(minutes=5),
                    Decimal("100.5"),
                    Decimal("102"),
                    Decimal("100"),
                    Decimal("101.5"),
                    Decimal("1000"),
                    as_of=t0 - timedelta(minutes=5),  # 300s old > 60s
                ),
            ]

    wrapper = MarketDataWrapper(StaleLatestBarProvider(), clock=clock)
    with pytest.raises(StaleDataError, match="is stale: latest as_of"):
        wrapper.bars(eq, "1m", t0 - timedelta(minutes=20), t0, max_age_seconds=60.0)


def test_provider_returning_none_refused() -> None:
    """Underlying provider returning None is strictly refused (I5: refuse, never guess)."""
    t0 = datetime(2026, 9, 23, 14, 30, 0, tzinfo=timezone.utc)
    clock = ReplayClock(t0)

    class NoneProvider:
        def bars(self, instrument, tf, start, end, max_age_seconds):
            return None

        def quote(self, instrument, max_age_seconds):
            return None

        def chain(self, underlying, expiry_start, expiry_end, max_age_seconds):
            return None

        def corporate_actions(self, symbol, max_age_seconds):
            return None

    wrapper = MarketDataWrapper(NoneProvider(), clock=clock)
    eq = Equity("AAPL")

    with pytest.raises(ValueError, match="Provider returned None for bars"):
        wrapper.bars(eq, "1m", t0 - timedelta(minutes=5), t0, max_age_seconds=60.0)

    with pytest.raises(ValueError, match="Provider returned None for quote"):
        wrapper.quote(eq, max_age_seconds=60.0)

    with pytest.raises(ValueError, match="Provider returned None for chain"):
        wrapper.chain("AAPL", date(2026, 10, 1), date(2026, 10, 31), max_age_seconds=60.0)

    with pytest.raises(ValueError, match="Provider returned None for corporate_actions"):
        wrapper.corporate_actions("AAPL", max_age_seconds=60.0)



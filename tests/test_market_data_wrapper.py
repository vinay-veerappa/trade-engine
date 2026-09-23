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
from trade_engine.market_data import StampingMarketDataWrapper


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


def test_wrapper_auto_stamp_mode() -> None:
    """Auto-stamp missing as_of using injected clock when configured."""
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

    wrapper = StampingMarketDataWrapper(RawProvider(), clock=clock, auto_stamp=True)
    res = wrapper.quote(Equity("TEST"), max_age_seconds=5.0)
    assert res.as_of == t0


def test_wrapper_validates_inputs() -> None:
    t0 = datetime(2026, 9, 23, 14, 30, 0, tzinfo=timezone.utc)
    clock = ReplayClock(t0)
    provider = FakeProvider()

    with pytest.raises(ValueError, match="requires an injected Clock"):
        StampingMarketDataWrapper(provider, clock=None)  # type: ignore[arg-type]

    wrapper = StampingMarketDataWrapper(provider, clock=clock)
    with pytest.raises(ValueError, match="strictly positive"):
        wrapper.quote(Equity("SPY"), max_age_seconds=0.0)

    with pytest.raises(ValueError, match="strictly positive"):
        wrapper.quote(Equity("SPY"), max_age_seconds=-5.0)

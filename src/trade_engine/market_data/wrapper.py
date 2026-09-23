"""Market data wrapper stamping as_of and enforcing freshness (Architecture §4.8, I5, I7)."""

from __future__ import annotations

from dataclasses import is_dataclass, replace
from datetime import date, datetime
from typing import Any, Sequence, TypeVar

from trade_engine.domain.instruments import Instrument
from trade_engine.interfaces.clock import Clock
from trade_engine.interfaces.market_data import (
    Bar,
    CorporateAction,
    MarketData,
    OptionQuote,
    Quote,
    StaleData,
    StaleDataError,
)

T = TypeVar("T", Bar, Quote, OptionQuote, CorporateAction)


class StampingMarketDataWrapper(MarketData):
    """Market data provider wrapper that verifies freshness and stamps timestamps.

    Enforces:
    - I5: Refuse, never guess. Stale data (older than max_age_seconds) raises StaleDataError.
    - I7: Injected clock. Current time is ALWAYS queried via the injected Clock.
    - Negative age (data from future relative to clock) is refused.
    """

    def __init__(
        self,
        provider: Any,
        clock: Clock,
        auto_stamp: bool = False,
    ) -> None:
        if clock is None or not isinstance(clock, Clock):
            raise ValueError("StampingMarketDataWrapper requires an injected Clock protocol instance (I7)")
        self._provider = provider
        self._clock = clock
        self._auto_stamp = auto_stamp

    @property
    def clock(self) -> Clock:
        return self._clock

    @property
    def provider(self) -> Any:
        return self._provider

    def _verify_item(self, item: T, max_age_seconds: float) -> T:
        now = self._clock.now_utc()
        if now.tzinfo is None or now.tzinfo.utcoffset(now) is None:
            raise ValueError("Clock.now_utc() returned a naive datetime (I7)")

        target_item = item
        if self._auto_stamp:
            as_of_val = getattr(target_item, "as_of", None)
            if as_of_val is None:
                if is_dataclass(target_item):
                    target_item = replace(target_item, as_of=now)
                else:
                    try:
                        setattr(target_item, "as_of", now)
                    except Exception:
                        pass

        as_of = getattr(target_item, "as_of", None)
        if as_of is None:
            raise ValueError(f"Market data item {target_item!r} has no as_of timestamp (I5)")
        if as_of.tzinfo is None or as_of.tzinfo.utcoffset(as_of) is None:
            raise ValueError(f"as_of timestamp must be timezone-aware UTC datetime (I7): {as_of!r}")

        age = (now - as_of).total_seconds()
        if age > max_age_seconds:
            symbol = getattr(
                target_item,
                "instrument",
                getattr(target_item, "contract", getattr(target_item, "symbol", repr(target_item))),
            )
            raise StaleDataError(
                f"Market data for {symbol} is stale: age {age:.3f}s exceeds allowed max_age {max_age_seconds:.3f}s "
                f"(as_of={as_of.isoformat()}, now={now.isoformat()}) (I5)"
            )

        if age < -1.0:
            raise ValueError(
                f"Market data timestamp is in the future: as_of={as_of.isoformat()} > now={now.isoformat()} (I5)"
            )

        return target_item

    def _validate_max_age(self, max_age_seconds: float) -> None:
        if max_age_seconds <= 0:
            raise ValueError(f"max_age_seconds must be strictly positive (I5): {max_age_seconds}")

    def bars(
        self,
        instrument: Instrument,
        tf: str,
        start: datetime,
        end: datetime,
        max_age_seconds: float,
    ) -> list[Bar]:
        self._validate_max_age(max_age_seconds)
        if start.tzinfo is None or start.tzinfo.utcoffset(start) is None:
            raise ValueError("start datetime must be timezone-aware (I7)")
        if end.tzinfo is None or end.tzinfo.utcoffset(end) is None:
            raise ValueError("end datetime must be timezone-aware (I7)")
        if start > end:
            raise ValueError(f"start datetime {start} cannot be after end datetime {end}")

        raw_bars: Sequence[Bar] = self._provider.bars(
            instrument=instrument,
            tf=tf,
            start=start,
            end=end,
            max_age_seconds=max_age_seconds,
        )
        return [self._verify_item(b, max_age_seconds) for b in raw_bars]

    def quote(
        self,
        instrument: Instrument,
        max_age_seconds: float,
    ) -> Quote:
        self._validate_max_age(max_age_seconds)
        raw_quote: Quote = self._provider.quote(
            instrument=instrument,
            max_age_seconds=max_age_seconds,
        )
        return self._verify_item(raw_quote, max_age_seconds)

    def chain(
        self,
        underlying: str,
        expiry_start: date,
        expiry_end: date,
        max_age_seconds: float,
    ) -> list[OptionQuote]:
        self._validate_max_age(max_age_seconds)
        if expiry_start > expiry_end:
            raise ValueError(f"expiry_start {expiry_start} cannot be after expiry_end {expiry_end}")

        raw_chain: Sequence[OptionQuote] = self._provider.chain(
            underlying=underlying,
            expiry_start=expiry_start,
            expiry_end=expiry_end,
            max_age_seconds=max_age_seconds,
        )
        return [self._verify_item(q, max_age_seconds) for q in raw_chain]

    def corporate_actions(
        self,
        symbol: str,
        max_age_seconds: float,
    ) -> list[CorporateAction]:
        self._validate_max_age(max_age_seconds)
        raw_actions: Sequence[CorporateAction] = self._provider.corporate_actions(
            symbol=symbol,
            max_age_seconds=max_age_seconds,
        )
        return [self._verify_item(a, max_age_seconds) for a in raw_actions]

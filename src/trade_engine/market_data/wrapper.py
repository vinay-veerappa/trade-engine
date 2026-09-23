"""Market data wrapper verifying as_of freshness and preventing lookahead (Architecture §4.8, I5, I7)."""

from __future__ import annotations

from datetime import date, datetime
import math
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
    """Market data provider wrapper that enforces freshness and prevents lookahead.

    Enforces:
    - I5: Refuse, never guess. Stale data (older than max_age_seconds) raises StaleDataError.
    - I7: Injected clock. Current time is ALWAYS queried via the injected Clock.
    - No lookahead: Data timestamps and as_of timestamps after clock.now_utc() are refused.
    - Missing as_of timestamp on any data item is strictly refused (I5: no invented timestamps).
    """

    def __init__(
        self,
        provider: Any,
        clock: Clock,
        future_tolerance_seconds: float = 0.0,
    ) -> None:
        if clock is None or not isinstance(clock, Clock):
            raise ValueError("StampingMarketDataWrapper requires an injected Clock protocol instance (I7)")
        if not isinstance(future_tolerance_seconds, (int, float)) or isinstance(future_tolerance_seconds, bool) or not math.isfinite(future_tolerance_seconds) or future_tolerance_seconds < 0:
            raise ValueError(f"future_tolerance_seconds must be a finite non-negative number: {future_tolerance_seconds!r}")
        self._provider = provider
        self._clock = clock
        self._future_tolerance_seconds = float(future_tolerance_seconds)

    @property
    def clock(self) -> Clock:
        return self._clock

    @property
    def provider(self) -> Any:
        return self._provider

    def _verify_item(self, item: T, max_age_seconds: float | None = None) -> T:
        if item is None:
            raise ValueError("Market data item is None (I5: refuse, never guess)")

        now = self._clock.now_utc()
        if now.tzinfo is None or now.tzinfo.utcoffset(now) is None:
            raise ValueError("Clock.now_utc() returned a naive datetime (I7)")

        as_of = getattr(item, "as_of", None)
        if as_of is None:
            raise ValueError(f"Market data item {item!r} has no as_of timestamp (I5)")
        if as_of.tzinfo is None or as_of.tzinfo.utcoffset(as_of) is None:
            raise ValueError(f"as_of timestamp must be timezone-aware UTC datetime (I7): {as_of!r}")

        age = (now - as_of).total_seconds()
        if age < -self._future_tolerance_seconds:
            raise ValueError(
                f"Market data timestamp is in the future relative to clock: as_of={as_of.isoformat()} > now={now.isoformat()} (I5)"
            )

        if max_age_seconds is not None and age > max_age_seconds:
            symbol = getattr(
                item,
                "instrument",
                getattr(item, "contract", getattr(item, "symbol", repr(item))),
            )
            raise StaleDataError(
                f"Market data for {symbol} is stale: age {age:.3f}s exceeds allowed max_age {max_age_seconds:.3f}s "
                f"(as_of={as_of.isoformat()}, now={now.isoformat()}) (I5)"
            )

        return item

    def _validate_max_age(self, max_age_seconds: float) -> None:
        if not isinstance(max_age_seconds, (int, float)) or isinstance(max_age_seconds, bool) or not math.isfinite(max_age_seconds) or max_age_seconds <= 0:
            raise ValueError(f"max_age_seconds must be a finite positive number (I5), got: {max_age_seconds!r}")

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
            raise ValueError(f"start datetime {start.isoformat()} cannot be after end datetime {end.isoformat()} (I5)")

        now = self._clock.now_utc()
        if start > now:
            raise ValueError(
                f"Requested bar start {start.isoformat()} is in the future relative to clock {now.isoformat()} (I5)"
            )

        effective_end = min(end, now)
        raw_bars: Sequence[Bar] | None = self._provider.bars(
            instrument=instrument,
            tf=tf,
            start=start,
            end=effective_end,
            max_age_seconds=max_age_seconds,
        )

        if raw_bars is None:
            raise ValueError(f"Provider returned None for bars({instrument}, {tf}) (I5: refuse, never guess)")

        verified: list[Bar] = []
        for b in raw_bars:
            if b is None:
                raise ValueError(f"Provider returned None item in bars for {instrument} (I5: refuse, never guess)")
            if b.timestamp > now:
                raise ValueError(
                    f"Bar timestamp {b.timestamp.isoformat()} is in the future relative to clock {now.isoformat()} (I5)"
                )
            verified.append(self._verify_item(b, max_age_seconds=None))

        if verified:
            newest_as_of = max(b.as_of for b in verified)
            age = (now - newest_as_of).total_seconds()
            if age > max_age_seconds:
                symbol = getattr(instrument, "symbol", repr(instrument))
                raise StaleDataError(
                    f"Market data for {symbol} ({tf}) is stale: latest as_of {newest_as_of.isoformat()} "
                    f"is {age:.3f}s old, exceeds allowed max_age {max_age_seconds:.3f}s (now={now.isoformat()}) (I5)"
                )

        return verified

    def quote(
        self,
        instrument: Instrument,
        max_age_seconds: float,
    ) -> Quote:
        self._validate_max_age(max_age_seconds)
        raw_quote: Quote | None = self._provider.quote(
            instrument=instrument,
            max_age_seconds=max_age_seconds,
        )
        if raw_quote is None:
            raise ValueError(f"Provider returned None for quote({instrument}) (I5: refuse, never guess)")
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
            raise ValueError(f"expiry_start {expiry_start} cannot be after expiry_end {expiry_end} (I5)")

        raw_chain: Sequence[OptionQuote] | None = self._provider.chain(
            underlying=underlying,
            expiry_start=expiry_start,
            expiry_end=expiry_end,
            max_age_seconds=max_age_seconds,
        )
        if raw_chain is None:
            raise ValueError(f"Provider returned None for chain({underlying}) (I5: refuse, never guess)")
        return [self._verify_item(q, max_age_seconds) for q in raw_chain]

    def corporate_actions(
        self,
        symbol: str,
        max_age_seconds: float,
    ) -> list[CorporateAction]:
        self._validate_max_age(max_age_seconds)
        raw_actions: Sequence[CorporateAction] | None = self._provider.corporate_actions(
            symbol=symbol,
            max_age_seconds=max_age_seconds,
        )
        if raw_actions is None:
            raise ValueError(f"Provider returned None for corporate_actions({symbol}) (I5: refuse, never guess)")
        return [self._verify_item(a, max_age_seconds) for a in raw_actions]

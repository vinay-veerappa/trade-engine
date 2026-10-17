"""Market data wrapper verifying as_of freshness and preventing lookahead (Architecture §4.8, I5, I7).

Native Rust-backed implementation calling te_core verifiers via trade_engine_rs doors.
"""

from __future__ import annotations

from datetime import date, datetime
import logging
import math
import os
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
from trade_engine.market_data import _rs

_LOGGER = logging.getLogger("trade_engine.market_data")

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
        strict: bool | None = None,
    ) -> None:
        if clock is None or not isinstance(clock, Clock):
            raise ValueError("StampingMarketDataWrapper requires an injected Clock protocol instance (I7)")
        if not isinstance(future_tolerance_seconds, (int, float)) or isinstance(future_tolerance_seconds, bool) or not math.isfinite(future_tolerance_seconds) or future_tolerance_seconds < 0:
            raise ValueError(f"future_tolerance_seconds must be a finite non-negative number: {future_tolerance_seconds!r}")

        self._provider = provider
        self._clock = clock
        self._future_tolerance_seconds = float(future_tolerance_seconds)
        self._strict_override = strict
        self._warning_count: int = 0

        # Validate future_tolerance via Rust door
        _rs.call(_rs.rs.market_data_validate_future_tolerance, self._future_tolerance_seconds)

    @property
    def clock(self) -> Clock:
        return self._clock

    @property
    def provider(self) -> Any:
        return self._provider

    @property
    def warning_count(self) -> int:
        return self._warning_count

    @property
    def is_strict(self) -> bool:
        if self._strict_override is not None:
            return self._strict_override
        return os.environ.get("TE_MARKET_DATA_STRICT") == "1"

    def _record_warning(self, msg: str) -> None:
        self._warning_count += 1
        _LOGGER.warning(msg)

    def _validate_max_age(self, max_age_seconds: float) -> None:
        if not isinstance(max_age_seconds, (int, float)) or isinstance(max_age_seconds, bool) or not math.isfinite(max_age_seconds) or max_age_seconds <= 0:
            raise ValueError(f"max_age_seconds must be a finite positive number (I5), got: {max_age_seconds!r}")
        _rs.call(_rs.rs.market_data_validate_max_age, float(max_age_seconds))

    def _symbol_of(self, item: Any) -> str:
        raw = getattr(
            item,
            "instrument",
            getattr(item, "contract", getattr(item, "symbol", None)),
        )
        if hasattr(raw, "symbol"):
            return str(raw.symbol)
        if raw is not None:
            return str(raw)
        return repr(item)

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

        symbol = self._symbol_of(item)
        _rs.call(
            _rs.rs.market_data_verify_item,
            symbol,
            as_of.isoformat(),
            float(max_age_seconds) if max_age_seconds is not None else None,
            now.isoformat(),
            self._future_tolerance_seconds,
        )
        return item

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

        now = self._clock.now_utc()
        if now.tzinfo is None or now.tzinfo.utcoffset(now) is None:
            raise ValueError("Clock.now_utc() returned a naive datetime (I7)")

        effective_end_iso = _rs.call(
            _rs.rs.market_data_validate_bars_request,
            start.isoformat(),
            end.isoformat(),
            float(max_age_seconds),
            now.isoformat(),
        )
        effective_end = datetime.fromisoformat(effective_end_iso)

        raw_bars: Sequence[Bar] | None = self._provider.bars(
            instrument=instrument,
            tf=tf,
            start=start,
            end=effective_end,
            max_age_seconds=max_age_seconds,
        )

        if raw_bars is None:
            raise ValueError(f"Provider returned None for bars({instrument}, {tf}) (I5: refuse, never guess)")

        bars_in = []
        for b in raw_bars:
            if b is None:
                raise ValueError(f"Provider returned None item in bars for {instrument} (I5: refuse, never guess)")
            if b.timestamp > now:
                raise ValueError(
                    f"Bar timestamp {b.timestamp.isoformat()} is in the future relative to clock {now.isoformat()} (I5)"
                )
            self._verify_item(b, max_age_seconds=None)
            b_sym = getattr(b.instrument, "symbol", str(b.instrument))
            bars_in.append((
                b_sym,
                b.timestamp.isoformat(),
                str(b.open),
                str(b.high),
                str(b.low),
                str(b.close),
                str(b.volume),
                b.as_of.isoformat(),
            ))

        req_sym = getattr(instrument, "symbol", str(instrument))
        warnings = _rs.call(
            _rs.rs.market_data_verify_bars,
            req_sym,
            tf,
            start.isoformat(),
            effective_end.isoformat(),
            float(max_age_seconds),
            now.isoformat(),
            self._future_tolerance_seconds,
            bars_in,
            self.is_strict,
        )

        for w in warnings:
            self._record_warning(w)

        return list(raw_bars)

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

        self._verify_item(raw_quote, max_age_seconds)

        now = self._clock.now_utc()
        sym = getattr(instrument, "symbol", str(instrument))
        if hasattr(raw_quote, "bid") and hasattr(raw_quote, "ask") and hasattr(raw_quote, "bid_size") and hasattr(raw_quote, "ask_size"):
            _rs.call(
                _rs.rs.market_data_verify_quote,
                sym,
                str(raw_quote.bid),
                str(raw_quote.ask),
                str(raw_quote.bid_size),
                str(raw_quote.ask_size),
                raw_quote.as_of.isoformat(),
                float(max_age_seconds),
                now.isoformat(),
                self._future_tolerance_seconds,
            )

        return raw_quote

    def chain(
        self,
        underlying: str,
        expiry_start: date,
        expiry_end: date,
        max_age_seconds: float,
    ) -> list[OptionQuote]:
        self._validate_max_age(max_age_seconds)
        _rs.call(
            _rs.rs.market_data_validate_chain_request,
            expiry_start.isoformat(),
            expiry_end.isoformat(),
            float(max_age_seconds),
        )

        raw_chain: Sequence[OptionQuote] | None = self._provider.chain(
            underlying=underlying,
            expiry_start=expiry_start,
            expiry_end=expiry_end,
            max_age_seconds=max_age_seconds,
        )
        if raw_chain is None:
            raise ValueError(f"Provider returned None for chain({underlying}) (I5: refuse, never guess)")

        now = self._clock.now_utc()
        quotes_in = []
        for q in raw_chain:
            self._verify_item(q, max_age_seconds)
            c = q.contract
            gk_tuple = None
            if q.greeks is not None:
                gk_tuple = (
                    float(q.greeks.delta),
                    float(q.greeks.gamma),
                    float(q.greeks.theta),
                    float(q.greeks.vega),
                    float(q.greeks.rho),
                    str(q.greeks.source),
                )
            contract_in = (
                str(c.underlying),
                c.expiry.isoformat() if hasattr(c.expiry, "isoformat") else str(c.expiry),
                str(c.strike),
                str(c.right.value if hasattr(c.right, "value") else c.right),
                int(c.multiplier),
            )
            quote_in = (
                str(q.bid),
                str(q.ask),
                str(q.bid_size),
                str(q.ask_size),
                q.as_of.isoformat(),
                str(q.underlying_price) if q.underlying_price is not None else None,
                str(q.implied_vol) if q.implied_vol is not None else None,
                gk_tuple,
                int(q.open_interest) if q.open_interest is not None else None,
            )
            quotes_in.append((contract_in, quote_in))

        warnings = _rs.call(
            _rs.rs.market_data_verify_chain,
            underlying,
            expiry_start.isoformat(),
            expiry_end.isoformat(),
            float(max_age_seconds),
            now.isoformat(),
            self._future_tolerance_seconds,
            quotes_in,
            self.is_strict,
        )

        for w in warnings:
            self._record_warning(w)

        return list(raw_chain)

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

        now = self._clock.now_utc()
        actions_in = []
        for a in raw_actions:
            self._verify_item(a, max_age_seconds)
            eff_date = a.effective_date.isoformat() if hasattr(a.effective_date, "isoformat") else str(a.effective_date)
            actions_in.append((
                str(a.symbol),
                str(a.action_type),
                eff_date,
                a.as_of.isoformat(),
            ))

        warnings = _rs.call(
            _rs.rs.market_data_verify_corporate_actions,
            symbol,
            float(max_age_seconds),
            now.isoformat(),
            self._future_tolerance_seconds,
            actions_in,
            self.is_strict,
        )

        for w in warnings:
            self._record_warning(w)

        return list(raw_actions)


__all__ = [
    "Bar",
    "CorporateAction",
    "MarketData",
    "OptionQuote",
    "Quote",
    "StaleData",
    "StaleDataError",
    "StampingMarketDataWrapper",
]

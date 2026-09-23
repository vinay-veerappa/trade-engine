"""Market data protocol and price structures (Architecture §4.8)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Protocol, runtime_checkable

from trade_engine.domain.instruments import Instrument, OptionContract


class StaleDataError(Exception):
    """Raised when market data is older than the caller's accepted max age (I5)."""


@dataclass(frozen=True)
class Bar:
    """OHLCV market bar stamped with as_of timestamp."""

    instrument: Instrument
    timestamp: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    as_of: datetime


@dataclass(frozen=True)
class Quote:
    """Top-of-book market quote stamped with as_of timestamp."""

    instrument: Instrument
    bid: Decimal
    ask: Decimal
    bid_size: Decimal
    ask_size: Decimal
    as_of: datetime

    @property
    def mid(self) -> Decimal:
        return (self.bid + self.ask) / Decimal("2")

    @property
    def spread(self) -> Decimal:
        return self.ask - self.bid


@dataclass(frozen=True)
class CorporateAction:
    """Corporate action record (dividend, split, spinoff, symbol change)."""

    symbol: str
    action_type: str
    effective_date: date
    as_of: datetime
    details: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class MarketData(Protocol):
    """Protocol for market data providers (Architecture §4.8, I5).

    Every method takes an optional max_age_seconds; data older than that raises StaleDataError.
    """

    def bars(
        self,
        instrument: Instrument,
        tf: str,
        start: datetime,
        end: datetime,
        max_age_seconds: float | None = None,
    ) -> list[Bar]:
        """Fetch historical bars within the time window."""
        ...

    def quote(
        self,
        instrument: Instrument,
        max_age_seconds: float | None = None,
    ) -> Quote:
        """Fetch current quote for an instrument."""
        ...

    def chain(
        self,
        underlying: str,
        expiry_start: date,
        expiry_end: date,
        max_age_seconds: float | None = None,
    ) -> list[OptionContract]:
        """Fetch option chain for an underlying over an expiration range."""
        ...

    def corporate_actions(
        self,
        symbol: str,
        max_age_seconds: float | None = None,
    ) -> list[CorporateAction]:
        """Fetch corporate actions for a symbol."""
        ...

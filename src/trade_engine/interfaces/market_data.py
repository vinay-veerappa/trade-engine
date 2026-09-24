"""Market data protocol and price structures (Architecture §4.8)."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from types import MappingProxyType
from typing import Any, Protocol, runtime_checkable

from trade_engine.domain.instruments import Instrument, OptionContract


class StaleDataError(Exception):
    """Raised when market data is older than the caller's accepted max age (I5)."""


StaleData = StaleDataError


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

    def __post_init__(self) -> None:
        if self.timestamp.tzinfo is None or self.timestamp.tzinfo.utcoffset(self.timestamp) is None:
            raise ValueError("Bar timestamp must be timezone-aware UTC datetime (I7)")
        if self.as_of.tzinfo is None or self.as_of.tzinfo.utcoffset(self.as_of) is None:
            raise ValueError("Bar as_of must be timezone-aware UTC datetime (I7)")
        if self.open <= Decimal("0") or self.high <= Decimal("0") or self.low <= Decimal("0") or self.close <= Decimal("0"):
            raise ValueError("Bar prices must be strictly positive (I5)")
        if self.high < self.low:
            raise ValueError(f"Bar high ({self.high}) cannot be lower than low ({self.low})")
        if not (self.low <= self.open <= self.high and self.low <= self.close <= self.high):
            raise ValueError("Bar open and close must lie within [low, high]")
        if self.volume < Decimal("0"):
            raise ValueError("Bar volume must be non-negative")


@dataclass(frozen=True)
class Quote:
    """Top-of-book market quote stamped with as_of timestamp."""

    instrument: Instrument
    bid: Decimal
    ask: Decimal
    bid_size: Decimal
    ask_size: Decimal
    as_of: datetime

    def __post_init__(self) -> None:
        if self.as_of.tzinfo is None or self.as_of.tzinfo.utcoffset(self.as_of) is None:
            raise ValueError("Quote as_of must be timezone-aware UTC datetime (I7)")
        if self.bid < Decimal("0") or self.ask < Decimal("0"):
            raise ValueError("Quote bid and ask must be non-negative")
        if self.bid > self.ask:
            raise ValueError(f"Quote bid ({self.bid}) cannot exceed ask ({self.ask})")

    @property
    def mid(self) -> Decimal:
        return (self.bid + self.ask) / Decimal("2")

    @property
    def spread(self) -> Decimal:
        return self.ask - self.bid


@dataclass(frozen=True)
class Greeks:
    """Option sensitivities: theta per calendar day, vega per vol point, rho per rate point.

    ``source`` says who computed them: ``"vendor"`` (published with the quote) or
    ``"model"`` (Black-Scholes-Merton over the snapshot's own inputs).
    """

    delta: float
    gamma: float
    theta: float
    vega: float
    rho: float
    source: str

    def __post_init__(self) -> None:
        for name in ("delta", "gamma", "theta", "vega", "rho"):
            value = getattr(self, name)
            if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
                raise ValueError(f"Greek {name} must be a finite number, got {value!r} (I5)")
        if not -1.0 <= self.delta <= 1.0:
            raise ValueError(f"Delta {self.delta} is outside [-1, 1] (I5)")
        if self.source not in ("vendor", "model"):
            raise ValueError(f"Greeks source must be 'vendor' or 'model', got {self.source!r}")


@dataclass(frozen=True)
class OptionQuote:
    """Top-of-book quote for an option contract stamped with as_of timestamp (Architecture §4.8)."""

    contract: OptionContract
    bid: Decimal
    ask: Decimal
    bid_size: Decimal
    ask_size: Decimal
    as_of: datetime
    underlying_price: Decimal | None = None
    implied_vol: Decimal | None = None
    greeks: Greeks | None = None
    open_interest: int | None = None

    def __post_init__(self) -> None:
        if self.as_of.tzinfo is None or self.as_of.tzinfo.utcoffset(self.as_of) is None:
            raise ValueError("OptionQuote as_of must be timezone-aware UTC datetime (I7)")
        if self.bid < Decimal("0") or self.ask < Decimal("0"):
            raise ValueError("OptionQuote bid and ask must be non-negative")
        if self.bid > self.ask:
            raise ValueError(f"OptionQuote bid ({self.bid}) cannot exceed ask ({self.ask})")

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
    details: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.as_of.tzinfo is None or self.as_of.tzinfo.utcoffset(self.as_of) is None:
            raise ValueError("CorporateAction as_of must be timezone-aware UTC datetime (I7)")
        if self.details is not None and not isinstance(self.details, MappingProxyType):
            object.__setattr__(self, "details", MappingProxyType(dict(self.details)))
        elif self.details is None:
            object.__setattr__(self, "details", MappingProxyType({}))


@runtime_checkable
class MarketData(Protocol):
    """Protocol for market data providers (Architecture §4.8, I5).

    Every method requires caller-specified max_age_seconds; data older than that raises StaleDataError.
    """

    def bars(
        self,
        instrument: Instrument,
        tf: str,
        start: datetime,
        end: datetime,
        max_age_seconds: float,
    ) -> list[Bar]:
        """Fetch historical bars within the time window."""
        ...

    def quote(
        self,
        instrument: Instrument,
        max_age_seconds: float,
    ) -> Quote:
        """Fetch current quote for an instrument."""
        ...

    def chain(
        self,
        underlying: str,
        expiry_start: date,
        expiry_end: date,
        max_age_seconds: float,
    ) -> list[OptionQuote]:
        """Fetch option chain with quotes for an underlying over an expiration range."""
        ...

    def corporate_actions(
        self,
        symbol: str,
        max_age_seconds: float,
    ) -> list[CorporateAction]:
        """Fetch corporate actions for a symbol."""
        ...

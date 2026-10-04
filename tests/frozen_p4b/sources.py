"""What the lifecycle pass is told: settlement prices, dividends, option quotes (O2, I5).

Each is a small protocol the host fills. The engine ships fixed-value implementations
for tests and hand-entered values, and adapters over the engine's own data (a
``MarketData`` provider's corporate actions, the O1 chain snapshot store). None of them
answers from a default: a price, dividend or quote that is not known raises
``StaleDataError`` and the pass refuses (I5).

Why the official close is an input rather than read from one-minute bars: the close
of the 15:59 bar is the last trade before the closing auction, not the official close
(I9), and the providers here serve one-minute bars only. The official close is the
settled daily bar, known after 17:00 ET. An AM-settled contract (``SPX`` monthlies)
settles on the special opening quotation, which is not the index's first print either,
so it has to be supplied too.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Protocol

from trade_engine.domain.instruments import OptionContract
from trade_engine.domain.option_roots import SettleTime, option_style
from trade_engine.interfaces.market_data import OptionQuote, StaleDataError
from trade_engine.market_data.chains import ChainSnapshotStore


def _aware(stamp: datetime, what: str) -> None:
    if stamp.tzinfo is None or stamp.tzinfo.utcoffset(stamp) is None:
        raise ValueError(f"{what} must be timezone-aware UTC (I7)")


@dataclass(frozen=True)
class SettlementPrice:
    """The price a session's expiring contracts settle on, and when it became known."""

    underlying: str
    session: date
    settle_time: SettleTime
    price: Decimal
    source: str
    as_of: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "underlying", self.underlying.strip().upper())
        if not self.underlying:
            raise ValueError("SettlementPrice.underlying must be non-empty")
        if not isinstance(self.price, Decimal) or not self.price.is_finite() or self.price <= 0:
            raise ValueError(f"SettlementPrice.price must be a positive Decimal, got {self.price!r} (I5)")
        if not self.source:
            raise ValueError("SettlementPrice.source must be non-empty (I11)")
        _aware(self.as_of, "SettlementPrice.as_of")


class Settlements(Protocol):
    def settlement(self, underlying: str, session: date, settle_time: SettleTime) -> SettlementPrice:
        """The ``settle_time`` settlement of ``underlying`` on ``session``; unknown raises
        ``StaleDataError``."""
        ...


class FixedSettlements:
    """Settlement prices given up front, e.g. entered by hand or read by the host."""

    def __init__(self, prices: Mapping[tuple[str, date, SettleTime], SettlementPrice] | list[SettlementPrice]) -> None:
        items = prices.values() if isinstance(prices, Mapping) else prices
        self._prices: dict[tuple[str, date, SettleTime], SettlementPrice] = {}
        for price in items:
            key = (price.underlying, price.session, price.settle_time)
            if key in self._prices and self._prices[key] != price:
                raise ValueError(f"Two different {key[2].value} settlements for {key[0]} on {key[1]} (I5)")
            self._prices[key] = price

    def settlement(self, underlying: str, session: date, settle_time: SettleTime) -> SettlementPrice:
        key = (underlying.strip().upper(), session, settle_time)
        found = self._prices.get(key)
        if found is None:
            raise StaleDataError(
                f"No {settle_time.value} settlement for {key[0]} on {session.isoformat()} (I5)"
            )
        return found


@dataclass(frozen=True)
class Dividend:
    """A cash dividend per share, by the date the shares go ex."""

    symbol: str
    ex_date: date
    amount: Decimal
    source: str
    as_of: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", self.symbol.strip().upper())
        if not isinstance(self.amount, Decimal) or not self.amount.is_finite() or self.amount <= 0:
            raise ValueError(f"Dividend.amount must be a positive Decimal, got {self.amount!r} (I5)")
        if not self.source:
            raise ValueError("Dividend.source must be non-empty (I11)")
        _aware(self.as_of, "Dividend.as_of")


class Dividends(Protocol):
    def dividends(self, symbol: str, ex_date: date) -> tuple[Dividend, ...]:
        """Every cash dividend on ``symbol`` going ex on ``ex_date``: an empty tuple when
        there is none, ``StaleDataError`` when the source cannot say."""
        ...


class FixedDividends:
    """Dividends given up front. A symbol not listed is unknown and refuses; list it
    with no dividends to say it pays none."""

    def __init__(self, by_symbol: Mapping[str, list[Dividend] | tuple[Dividend, ...]]) -> None:
        self._by_symbol = {symbol.strip().upper(): tuple(items) for symbol, items in by_symbol.items()}

    def dividends(self, symbol: str, ex_date: date) -> tuple[Dividend, ...]:
        key = symbol.strip().upper()
        if key not in self._by_symbol:
            raise StaleDataError(f"No dividend record for {key} (I5)")
        return tuple(d for d in self._by_symbol[key] if d.ex_date == ex_date)


class CorporateActionDividends:
    """Dividends from a ``MarketData`` provider's ``corporate_actions``.

    Reads each ``action_type == "dividend"`` action's ``effective_date`` as the ex-date
    and ``details["amount"]`` as the cash per share. A dividend action without a
    readable positive amount refuses rather than counting as none (I5).
    """

    def __init__(self, market_data, max_age_seconds: float) -> None:
        self._market_data = market_data
        self._max_age = max_age_seconds

    def dividends(self, symbol: str, ex_date: date) -> tuple[Dividend, ...]:
        found = []
        for action in self._market_data.corporate_actions(symbol, self._max_age):
            if action.action_type != "dividend" or action.effective_date != ex_date:
                continue
            raw = action.details.get("amount")
            try:
                amount = Decimal(str(raw)) if raw is not None else None
            except (InvalidOperation, ValueError):
                amount = None
            if amount is None or not amount.is_finite() or amount <= 0:
                raise StaleDataError(f"{symbol} dividend going ex {ex_date} has no usable amount: {raw!r} (I5)")
            found.append(Dividend(symbol, ex_date, amount, "corporate_actions", action.as_of))
        return tuple(found)


class OptionQuotes(Protocol):
    def quote(self, contract: OptionContract, now: datetime) -> OptionQuote:
        """The contract's latest quote known at ``now``; unknown or stale raises
        ``StaleDataError``."""
        ...


class SnapshotQuotes:
    """Quotes from the O1 chain snapshot store: the newest snapshot at or before ``now``
    within ``max_age_seconds``."""

    def __init__(self, store: ChainSnapshotStore, max_age_seconds: float) -> None:
        self._store = store
        self._max_age = max_age_seconds

    def quote(self, contract: OptionContract, now: datetime) -> OptionQuote:
        underlying = option_style(contract.underlying).underlying
        snapshot = self._store.latest(underlying, now, self._max_age)
        found = snapshot.get(contract)
        if found is None:
            raise StaleDataError(
                f"{contract.occ.strip()} is not in the {underlying} chain snapshot of "
                f"{snapshot.as_of.isoformat()} (I5)"
            )
        return found

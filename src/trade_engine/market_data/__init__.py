"""Market data providers and freshness wrappers (Architecture §4.8)."""

from trade_engine.interfaces.market_data import (
    Bar,
    CorporateAction,
    MarketData,
    OptionQuote,
    Quote,
    StaleData,
    StaleDataError,
)
from trade_engine.market_data.wrapper import StampingMarketDataWrapper

MarketDataWrapper = StampingMarketDataWrapper

__all__ = [
    "Bar",
    "CorporateAction",
    "MarketData",
    "MarketDataWrapper",
    "OptionQuote",
    "Quote",
    "StaleData",
    "StaleDataError",
    "StampingMarketDataWrapper",
]

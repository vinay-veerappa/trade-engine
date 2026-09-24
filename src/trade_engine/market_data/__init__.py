"""Market data providers and freshness wrappers (Architecture §4.8)."""

from trade_engine.interfaces.market_data import (
    Bar,
    CorporateAction,
    Greeks,
    MarketData,
    OptionQuote,
    Quote,
    StaleData,
    StaleDataError,
)
from trade_engine.market_data.chains import ChainSnapshot, ChainSnapshotStore
from trade_engine.market_data.wrapper import StampingMarketDataWrapper

MarketDataWrapper = StampingMarketDataWrapper

__all__ = [
    "Bar",
    "ChainSnapshot",
    "ChainSnapshotStore",
    "CorporateAction",
    "Greeks",
    "MarketData",
    "MarketDataWrapper",
    "OptionQuote",
    "Quote",
    "StaleData",
    "StaleDataError",
    "StampingMarketDataWrapper",
]

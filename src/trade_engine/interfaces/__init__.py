"""Interfaces and protocols for trade_engine."""

from trade_engine.interfaces.broker import (
    AccountEvent,
    AccountRecord,
    AccountSnapshot,
    BrokerAdapter,
    Route,
    Capabilities,
    OrderChanges,
    VenueAck,
    VenueCashEvent,
    VenueFill,
    VenueIdentity,
    VenueOrder,
    VenueOrderAllocation,
    VenueOrderState,
    VenuePosition,
)
from trade_engine.interfaces.clock import Clock
from trade_engine.interfaces.market_data import (
    Bar,
    CorporateAction,
    Greeks,
    MarketData,
    OptionQuote,
    Quote,
    StaleDataError,
)
from trade_engine.interfaces.signals import SignalAdapter
from trade_engine.interfaces.sinks import Sink
from trade_engine.interfaces.strategy import Strategy

__all__ = [
    "AccountEvent",
    "AccountRecord",
    "AccountSnapshot",
    "Bar",
    "BrokerAdapter",
    "Capabilities",
    "Clock",
    "CorporateAction",
    "Greeks",
    "MarketData",
    "OptionQuote",
    "OrderChanges",
    "Quote",
    "Route",
    "SignalAdapter",
    "Sink",
    "StaleDataError",
    "Strategy",
    "VenueAck",
    "VenueCashEvent",
    "VenueFill",
    "VenueIdentity",
    "VenueOrder",
    "VenueOrderAllocation",
    "VenueOrderState",
    "VenuePosition",
]

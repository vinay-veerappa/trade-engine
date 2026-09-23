"""Interfaces and protocols for trade_engine."""

from trade_engine.interfaces.broker import (
    BrokerAdapter,
    Capabilities,
    OrderChanges,
    VenueAck,
    VenueCashEvent,
    VenueFill,
    VenueIdentity,
    VenueOrder,
    VenueOrderState,
    VenuePosition,
)
from trade_engine.interfaces.clock import Clock
from trade_engine.interfaces.market_data import (
    Bar,
    CorporateAction,
    MarketData,
    Quote,
    StaleDataError,
)
from trade_engine.interfaces.signals import SignalAdapter
from trade_engine.interfaces.sinks import Sink
from trade_engine.interfaces.strategy import Strategy

__all__ = [
    "Bar",
    "BrokerAdapter",
    "Capabilities",
    "Clock",
    "CorporateAction",
    "MarketData",
    "OrderChanges",
    "Quote",
    "SignalAdapter",
    "Sink",
    "StaleDataError",
    "Strategy",
    "VenueAck",
    "VenueCashEvent",
    "VenueFill",
    "VenueIdentity",
    "VenueOrder",
    "VenueOrderState",
    "VenuePosition",
]

"""EOD runner and plugin discovery (E7, Architecture §4.9)."""

from trade_engine.eod.plugins import (
    MARKET_DATA_GROUP,
    SIGNAL_GROUP,
    STRATEGY_GROUP,
    DiscoveredPlugins,
    PluginDiscoveryError,
    discover_plugins,
)
from trade_engine.eod.runner import (
    EodRunner,
    EodRunnerConfig,
    EodRunnerError,
    SessionIncompleteError,
)

__all__ = [
    "DiscoveredPlugins",
    "EodRunner",
    "EodRunnerError",
    "EodRunnerConfig",
    "MARKET_DATA_GROUP",
    "PluginDiscoveryError",
    "SIGNAL_GROUP",
    "STRATEGY_GROUP",
    "SessionIncompleteError",
    "discover_plugins",
]
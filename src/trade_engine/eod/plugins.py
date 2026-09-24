"""Plugin discovery through Python entry points (E7, Architecture §3).

Plugins live in the consuming repository and register against three groups, so the
engine discovers them without importing that repo:

- ``trade_engine.strategies``  -> objects carrying a ``generate_intents`` method
- ``trade_engine.signals``     -> objects carrying a ``read_signals`` method
- ``trade_engine.marketdata``  -> objects carrying a ``bars`` method

Discovery refuses rather than guesses (I5): a plugin that fails to load or lacks the
group's method is a refusal, never a silent skip.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from importlib.metadata import entry_points
from typing import Any, Protocol, runtime_checkable

from trade_engine.interfaces.signals import SignalAdapter
from trade_engine.interfaces.strategy import Strategy

STRATEGY_GROUP = "trade_engine.strategies"
SIGNAL_GROUP = "trade_engine.signals"
MARKET_DATA_GROUP = "trade_engine.marketdata"

_REQUIRED_ATTRIBUTE: dict[str, str] = {
    STRATEGY_GROUP: "generate_intents",
    SIGNAL_GROUP: "read_signals",
    MARKET_DATA_GROUP: "bars",
}


class PluginDiscoveryError(RuntimeError):
    """A registered plugin could not be loaded or does not declare the group's shape."""


@runtime_checkable
class _EntryPointLike(Protocol):
    """The one attribute pair discovery needs from an importlib entry point."""

    name: str

    def load(self) -> Any: ...


@dataclass(frozen=True)
class DiscoveredPlugins:
    """The plugin instances found for one engine process."""

    strategies: tuple[Any, ...]
    signal_adapters: tuple[Any, ...]
    market_data: tuple[Any, ...]


def _load_group(
    group: str,
    reader: Callable[[str], Iterable[_EntryPointLike]],
) -> tuple[Any, ...]:
    instances: list[Any] = []
    for ep in reader(group):
        try:
            instance = ep.load()
        except Exception as err:
            raise PluginDiscoveryError(
                f"Plugin '{ep.name}' in group '{group}' failed to load: {err}"
            ) from err
        required = _REQUIRED_ATTRIBUTE.get(group)
        if required is not None and not callable(getattr(instance, required, None)):
            raise PluginDiscoveryError(
                f"Plugin '{ep.name}' in group '{group}' does not declare a callable "
                f"'{required}' method"
            )
        instances.append(instance)
    return tuple(instances)


def discover_plugins(
    *,
    entry_point_reader: Callable[[str], Iterable[_EntryPointLike]] | None = None,
) -> DiscoveredPlugins:
    """Load every registered plugin instance for the three engine groups.

    ``entry_point_reader`` is the injection seam for tests: a callable
    ``(group) -> iterable of entry points``. Omitted, it reads the installed
    environment via ``importlib.metadata``.
    """
    reader = entry_point_reader or (lambda group: entry_points(group=group))
    return DiscoveredPlugins(
        strategies=_load_group(STRATEGY_GROUP, reader),
        signal_adapters=_load_group(SIGNAL_GROUP, reader),
        market_data=_load_group(MARKET_DATA_GROUP, reader),
    )
"""Plugin discovery tests (E7, Architecture §3)."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from trade_engine.eod import (
    MARKET_DATA_GROUP,
    SIGNAL_GROUP,
    STRATEGY_GROUP,
    PluginDiscoveryError,
    discover_plugins,
)


@dataclass
class FakeEntryPoint:
    name: str
    target: object | None = None
    load_error: Exception | None = None

    def load(self) -> object:
        if self.load_error is not None:
            raise self.load_error
        if self.target is None:
            raise RuntimeError(f"'{self.name}' has no target")
        return self.target


class GoodStrategy:
    def generate_intents(self, signals, context):
        return []


class NotAStrategy:
    pass


def _reader(group: str, mapping: dict[str, list[FakeEntryPoint]]):
    return lambda group_name: mapping.get(group_name, [])


def test_discovers_plugins_across_the_three_groups() -> None:
    strategy = GoodStrategy()
    signal_adapter = SessionSignals()
    market_data = Bars()
    mapping = {
        STRATEGY_GROUP: [FakeEntryPoint("s", strategy)],
        SIGNAL_GROUP: [FakeEntryPoint("a", signal_adapter)],
        MARKET_DATA_GROUP: [FakeEntryPoint("m", market_data)],
    }
    found = discover_plugins(entry_point_reader=_reader(STRATEGY_GROUP, mapping))

    assert found.strategies == (strategy,)
    assert found.signal_adapters == (signal_adapter,)
    assert found.market_data == (market_data,)


def test_a_plugin_that_fails_to_load_refuses() -> None:
    broken = FakeEntryPoint("broken", load_error=ImportError("module missing"))
    mapping = {STRATEGY_GROUP: [broken], SIGNAL_GROUP: [], MARKET_DATA_GROUP: []}
    with pytest.raises(PluginDiscoveryError, match="broken"):
        discover_plugins(entry_point_reader=_reader(STRATEGY_GROUP, mapping))


def test_a_plugin_missing_the_group_method_refuses() -> None:
    mapping = {
        STRATEGY_GROUP: [FakeEntryPoint("bad", NotAStrategy())],
        SIGNAL_GROUP: [],
        MARKET_DATA_GROUP: [],
    }
    with pytest.raises(PluginDiscoveryError, match="generate_intents"):
        discover_plugins(entry_point_reader=_reader(STRATEGY_GROUP, mapping))


def test_empty_groups_discover_nothing() -> None:
    mapping = {STRATEGY_GROUP: [], SIGNAL_GROUP: [], MARKET_DATA_GROUP: []}
    found = discover_plugins(entry_point_reader=_reader(STRATEGY_GROUP, mapping))
    assert found.strategies == ()
    assert found.signal_adapters == ()
    assert found.market_data == ()


class SessionSignals:
    def read_signals(self, session_date):
        return []


class Bars:
    def bars(self, instrument, tf, start, end, max_age_seconds):
        return []
"""Trusted factories receive non-owning reads of the existing owner handle.

Factories return configuration, strategies and named adapters, never a runner or
another ledger. The native loader owns provenance checks and construction order.
This protocol is not a sandbox for untrusted Python or arbitrary SQLite access.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import trade_engine_rs

from trade_engine.interfaces.clock import Clock
from trade_engine.ledger.store import Ledger

FactoryContext = trade_engine_rs.FactoryContext


@dataclass(frozen=True)
class FactoryResult:
    config: Mapping[str, Any]
    strategies: tuple[Any, ...] = ()
    adapters: Mapping[str, Any] | None = None


def load_factory(
    module: str,
    factory: str,
    plugin_paths: Sequence[str | Path],
    *,
    ledger: Ledger,
    clock: Clock,
    config: Mapping[str, Any],
) -> FactoryResult:
    return trade_engine_rs.runtime_load_factory(
        module, factory, [str(path) for path in plugin_paths], ledger, clock, config
    )

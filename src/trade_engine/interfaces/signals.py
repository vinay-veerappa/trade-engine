"""SignalAdapter protocol (Architecture §4)."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from trade_engine.domain.signals import Signal


@runtime_checkable
class SignalAdapter(Protocol):
    """Protocol for reading and adapting external signals (e.g. scan CSVs, CSP ranks) into typed Signals."""

    name: str

    def read_signals(self, source: Any) -> list[Signal]:
        """Read and adapt signals from external data source into domain Signal objects."""
        ...

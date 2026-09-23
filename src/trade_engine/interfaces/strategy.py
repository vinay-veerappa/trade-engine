"""Strategy protocol (Architecture §2, I13, §4)."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from trade_engine.domain.signals import OrderIntent, Signal


@runtime_checkable
class Strategy(Protocol):
    """Protocol for trading strategies.

    Strategies evaluate signals and produce order intents.
    They know nothing about brokers or order routing (I13).
    """

    name: str

    def generate_intents(self, signals: list[Signal], context: Any) -> list[OrderIntent]:
        """Generate order intents from a collection of signals and account/market context."""
        ...

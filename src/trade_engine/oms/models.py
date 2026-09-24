"""Public immutable OMS value objects."""

from __future__ import annotations

from dataclasses import dataclass

from trade_engine.domain.orders import Order


@dataclass(frozen=True)
class Bracket:
    """Entry order with its held stop and profit-target children."""

    entry: Order
    stop: Order
    targets: tuple[Order, ...]

    @property
    def orders(self) -> tuple[Order, ...]:
        return (self.entry, self.stop, *self.targets)

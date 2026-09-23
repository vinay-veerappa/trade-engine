"""Persistent-state-friendly trailing-stop price-path evaluation."""

from __future__ import annotations

from decimal import Decimal

from trade_engine.domain.instruments import Side


class TrailingStopEmulator:
    """Track a protective stop using the side of the exit order."""

    def __init__(
        self,
        side: Side,
        trail_amount: Decimal,
        *,
        extreme: Decimal | None = None,
        stop_price: Decimal | None = None,
        triggered: bool = False,
    ) -> None:
        if trail_amount <= 0:
            raise ValueError("trail_amount must be positive")
        self._side = side
        self._trail_amount = trail_amount
        self._extreme = extreme
        self._stop_price = stop_price
        self._triggered = triggered

    @property
    def extreme(self) -> Decimal | None:
        return self._extreme

    @property
    def stop_price(self) -> Decimal | None:
        return self._stop_price

    @property
    def triggered(self) -> bool:
        return self._triggered

    def update(self, price: Decimal) -> bool:
        """Advance one observed price and report a stop trigger."""
        if not price.is_finite() or price <= 0:
            raise ValueError(f"price must be finite and positive, got {price}")
        if self._triggered:
            return True

        if self._side is Side.SELL:
            self._extreme = price if self._extreme is None else max(self._extreme, price)
            self._stop_price = self._extreme - self._trail_amount
            if self._stop_price <= 0:
                raise ValueError("trail amount produces a non-positive protective stop")
            self._triggered = price <= self._stop_price
        else:
            self._extreme = price if self._extreme is None else min(self._extreme, price)
            self._stop_price = self._extreme + self._trail_amount
            self._triggered = price >= self._stop_price
        return self._triggered

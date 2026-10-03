"""Persistent-state-friendly trailing-stop price-path evaluation.

The rule lives in Rust (``te_core::sim::trailing``, docs/RUST_PORT.md P3a); this class
holds the persisted state and hands it across.
"""

from __future__ import annotations

from decimal import Decimal

from trade_engine.domain.instruments import Side
from trade_engine.sim import _rs


def _opt(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


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
        _rs.call(_rs.rs.trail_check_amount, str(trail_amount))
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
        result, (extreme, stop_price, triggered), refused = _rs.call(
            _rs.rs.trail_update,
            self._side.value,
            str(self._trail_amount),
            (_opt(self._extreme), _opt(self._stop_price), bool(self._triggered)),
            str(price),
        )
        # The state may move before a refusal (the SELL stop is set, then checked).
        self._extreme = self._keep(self._extreme, extreme, price)
        self._stop_price = self._keep(self._stop_price, stop_price, None)
        self._triggered = triggered
        if refused is not None:
            raise _rs.refusal(*refused)
        return bool(result)

    @staticmethod
    def _keep(old: Decimal | None, new: str | None, price: Decimal | None) -> Decimal | None:
        """The held object when Rust kept the value, else the new value."""
        if new is None:
            return None
        if old is not None and str(old) == new:
            return old
        if price is not None and str(price) == new:
            return price
        return Decimal(new)

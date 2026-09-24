"""EOD exit management: what a strategy sees of its open brackets and may ask for.

A strategy's optional ``manage_positions(brackets, context)`` runs once per account at
the close, after marks and before D+1 entries (Architecture §4.9, I13). It returns
exit actions; the engine applies them through the OMS, so the strategy still never
touches a broker. Only three actions exist, and none can widen risk:

- ``MoveStop`` tightens the protective stop (breakeven, trailing). Loosening refuses.
- ``ClosePosition`` sells (or covers) the whole open quantity at the next open with a
  DAY market order, for time stops and day-N exits. The protective stop stays live
  until that order fills.
- ``ReducePosition`` sells (or covers) a fraction of the open quantity at the next open
  with a DAY market order, for time-based partial profits. It replaces the resting
  profit targets; once it fills the protective stop shrinks to what remains.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from trade_engine.domain.instruments import Instrument, Side


@dataclass(frozen=True)
class OpenBracket:
    """One bracket with open quantity, as folded from the ledger at the close."""

    entry_order_id: str
    account_id: str
    instrument: Instrument
    side: Side  # the entry's side: BUY for a long
    entry_quantity: Decimal  # filled
    open_quantity: Decimal
    average_entry_price: Decimal
    entry_filled_at: datetime  # first entry fill
    entry_session: date
    sessions_held: int  # sessions after the entry session, up to and including today
    stop_price: Decimal
    targets_filled: int
    open_targets: tuple[tuple[Decimal, Decimal], ...]  # (limit price, unfilled quantity)
    last_close: Decimal | None  # the session's last regular close, None if no bar


@dataclass(frozen=True)
class MoveStop:
    """Tighten a bracket's protective stop to ``stop_price``."""

    entry_order_id: str
    stop_price: Decimal
    reason: str
    command_id: str

    def __post_init__(self) -> None:
        _require(self.entry_order_id, "entry_order_id")
        _require(self.reason, "reason")
        _require(self.command_id, "command_id")
        if not isinstance(self.stop_price, Decimal) or not self.stop_price.is_finite():
            raise ValueError(f"stop_price must be a finite Decimal, got {self.stop_price!r}")
        if self.stop_price <= 0:
            raise ValueError(f"stop_price must be positive, got {self.stop_price}")


@dataclass(frozen=True)
class ClosePosition:
    """Exit a bracket's whole open quantity at the next session's open."""

    entry_order_id: str
    reason: str
    command_id: str

    def __post_init__(self) -> None:
        _require(self.entry_order_id, "entry_order_id")
        _require(self.reason, "reason")
        _require(self.command_id, "command_id")


@dataclass(frozen=True)
class ReducePosition:
    """Exit ``fraction`` of a bracket's open quantity, rounded down, at the next open."""

    entry_order_id: str
    fraction: Decimal
    reason: str
    command_id: str

    def __post_init__(self) -> None:
        _require(self.entry_order_id, "entry_order_id")
        _require(self.reason, "reason")
        _require(self.command_id, "command_id")
        if not isinstance(self.fraction, Decimal) or not self.fraction.is_finite():
            raise ValueError(f"fraction must be a finite Decimal, got {self.fraction!r}")
        if not Decimal("0") < self.fraction < Decimal("1"):
            # The whole position is ClosePosition; a fraction above 1 would reverse it.
            raise ValueError(f"fraction must be between 0 and 1 exclusive, got {self.fraction}")


ExitAction = MoveStop | ClosePosition | ReducePosition


def _require(value: str, name: str) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty string")

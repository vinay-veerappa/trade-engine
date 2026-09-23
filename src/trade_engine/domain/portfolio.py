"""Portfolio, Fill, Position, and Account domain models."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Literal

from trade_engine.domain.instruments import Instrument, Side

VenueEnv = Literal["sim", "paper", "live"]


@dataclass(frozen=True)
class Fill:
    """Execution fill event reported by a venue simulator or broker adapter."""

    fill_id: str
    order_id: str
    account_id: str
    instrument: Instrument
    quantity: Decimal  # Executed quantity (strictly positive)
    price: Decimal
    venue_env: VenueEnv  # Must be proven at connect (I10)
    filled_at: datetime
    side: Side = Side.BUY
    fee: Decimal = Decimal("0")
    leg_id: str | None = None
    venue_order_id: str | None = None
    venue_execution_id: str | None = None

    def __post_init__(self) -> None:
        if not self.fill_id:
            raise ValueError("fill_id must be non-empty")
        if not self.order_id:
            raise ValueError("order_id must be non-empty")
        if not self.account_id:
            raise ValueError("account_id must be non-empty")
        if self.quantity <= Decimal("0"):
            raise ValueError(f"Fill quantity must be strictly positive, got {self.quantity}")
        if self.price <= Decimal("0"):
            raise ValueError(f"Fill price must be strictly positive, got {self.price} (I5)")
        if self.venue_env not in ("sim", "paper", "live"):
            raise ValueError(f"Invalid venue_env '{self.venue_env}'")
        if not isinstance(self.side, Side):
            raise ValueError(f"Invalid side '{self.side}'")
        if self.filled_at.tzinfo is None or self.filled_at.tzinfo.utcoffset(self.filled_at) is None:
            raise ValueError("Fill filled_at must be timezone-aware UTC datetime (I7)")


@dataclass(frozen=True)
class Lot:
    """An individual tax/accounting lot for FIFO tracking and MFE/MAE calculation."""

    lot_id: str
    quantity: Decimal
    cost_basis: Decimal
    acquired_at: datetime
    side: Side = Side.BUY

    def __post_init__(self) -> None:
        if not self.lot_id:
            raise ValueError("lot_id must be non-empty")
        if self.quantity <= Decimal("0"):
            raise ValueError(f"Lot quantity must be positive, got {self.quantity}")
        if self.cost_basis <= Decimal("0"):
            raise ValueError(f"Lot cost_basis must be positive, got {self.cost_basis}")
        if not isinstance(self.side, Side):
            raise ValueError(f"Invalid side '{self.side}'")
        if self.acquired_at.tzinfo is None or self.acquired_at.tzinfo.utcoffset(self.acquired_at) is None:
            raise ValueError("Lot acquired_at must be timezone-aware UTC datetime (I7)")


@dataclass(frozen=True)
class Position:
    """Account position for a specific resolved instrument (Architecture §4.1)."""

    account_id: str
    instrument: Instrument
    quantity: Decimal  # Signed quantity: > 0 long, < 0 short, 0 flat
    avg_cost: Decimal
    realized_pnl: Decimal = Decimal("0")
    open_lots: tuple[Lot, ...] = field(default_factory=tuple)

    @property
    def is_long(self) -> bool:
        return self.quantity > Decimal("0")

    @property
    def is_short(self) -> bool:
        return self.quantity < Decimal("0")

    @property
    def is_flat(self) -> bool:
        return self.quantity == Decimal("0")


@dataclass(frozen=True)
class AccountConfig:
    """Configuration for a virtual or mirrored trading account."""

    account_id: str
    starting_cash: Decimal
    margin_model: str  # e.g., "reg_t", "cash", "portfolio"
    rules_profile: str  # Rule profile identifier
    venue_binding: str  # Venue identifier e.g. "sim", "tos_paper", "schwab_live"

    def __post_init__(self) -> None:
        if not self.account_id:
            raise ValueError("account_id must be non-empty")
        if self.starting_cash <= Decimal("0"):
            raise ValueError(f"starting_cash must be positive, got {self.starting_cash}")
        if not self.margin_model:
            raise ValueError("margin_model must be non-empty")
        if not self.rules_profile:
            raise ValueError("rules_profile must be non-empty")
        if not self.venue_binding:
            raise ValueError("venue_binding must be non-empty")

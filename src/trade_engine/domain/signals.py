"""Signal and OrderIntent domain definitions."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

from trade_engine.domain.instruments import Instrument, Side


@dataclass(frozen=True)
class Signal:
    """A raw or enriched signal from an external scan adapter (Architecture §4.1)."""

    signal_id: str
    scan_id: str
    symbol: str
    session_date: date
    direction: str  # "long" or "short"
    metrics: dict[str, Decimal] = field(default_factory=dict)
    next_earnings_date: date | None = None  # None when unknown, NEVER a guessed date (I5)
    created_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.signal_id:
            raise ValueError("signal_id must be non-empty")
        if not self.scan_id:
            raise ValueError("scan_id must be non-empty")
        if not self.symbol:
            raise ValueError("symbol must be non-empty")
        if self.direction not in ("long", "short"):
            raise ValueError(f"direction must be 'long' or 'short', got '{self.direction}'")


@dataclass(frozen=True)
class OrderIntent:
    """Strategy-generated trade intent before risk evaluation and sizing (Architecture §4.1)."""

    intent_id: str
    account_id: str
    instrument: Instrument
    side: Side
    quantity_rule: str  # e.g., "risk_0.75pct", "fixed_1"
    entry_price: Decimal
    stop_loss: Decimal
    profit_targets: tuple[Decimal, ...]
    reason: str
    command_id: str  # Idempotency key (I3)

    def __post_init__(self) -> None:
        if not self.intent_id:
            raise ValueError("intent_id must be non-empty")
        if not self.account_id:
            raise ValueError("account_id must be non-empty")
        if not self.command_id:
            raise ValueError("command_id must be non-empty (I3)")
        if self.entry_price <= Decimal("0"):
            raise ValueError(f"entry_price must be positive, got {self.entry_price}")
        if self.stop_loss <= Decimal("0"):
            raise ValueError(f"stop_loss must be positive, got {self.stop_loss}")
        for pt in self.profit_targets:
            if pt <= Decimal("0"):
                raise ValueError(f"profit_target must be positive, got {pt}")
        if not self.reason:
            raise ValueError("reason must be non-empty (I11)")

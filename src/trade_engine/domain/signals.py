"""Signal and OrderIntent domain definitions."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from types import MappingProxyType

from trade_engine.domain.instruments import Instrument, Side
from trade_engine.domain.orders import OrderType, TimeInForce

# Bracket orders rest until filled or cancelled; OPG/MOC/GTD need terms an intent does not carry.
BRACKET_TIFS = frozenset({TimeInForce.DAY, TimeInForce.GTC})
# LIMIT buys at or below entry_price; STOP buys only once price trades up through it
# (a breakout trigger), so an untriggered breakout never fills.
BRACKET_ENTRY_TYPES = frozenset({OrderType.LIMIT, OrderType.STOP})


@dataclass(frozen=True)
class Signal:
    """A raw or enriched signal from an external scan adapter (Architecture §4.1)."""

    signal_id: str
    scan_id: str
    symbol: str
    session_date: date
    direction: str  # "long" or "short"
    metrics: Mapping[str, Decimal] = field(default_factory=dict)
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
        if self.created_at is not None and (
            self.created_at.tzinfo is None or self.created_at.tzinfo.utcoffset(self.created_at) is None
        ):
            raise ValueError("Signal created_at must be timezone-aware UTC datetime (I7)")

        if self.metrics is not None and not isinstance(self.metrics, MappingProxyType):
            object.__setattr__(self, "metrics", MappingProxyType(dict(self.metrics)))
        elif self.metrics is None:
            object.__setattr__(self, "metrics", MappingProxyType({}))

    def __hash__(self) -> int:
        metrics_tuple = tuple(sorted(self.metrics.items())) if self.metrics else ()
        return hash(
            (
                self.signal_id,
                self.scan_id,
                self.symbol,
                self.session_date,
                self.direction,
                metrics_tuple,
                self.next_earnings_date,
                self.created_at,
            )
        )


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
    # The entry works one session; protective exits stay live until the position closes,
    # so a multi-day (equity or options) swing trade keeps its stop overnight.
    entry_tif: TimeInForce = TimeInForce.DAY
    exit_tif: TimeInForce = TimeInForce.GTC
    entry_type: OrderType = OrderType.LIMIT

    def __post_init__(self) -> None:
        if not self.intent_id:
            raise ValueError("intent_id must be non-empty")
        if self.entry_type not in BRACKET_ENTRY_TYPES:
            raise ValueError(
                "entry_type must be one of "
                f"{sorted(value.value for value in BRACKET_ENTRY_TYPES)}, got {self.entry_type!r}"
            )
        for name in ("entry_tif", "exit_tif"):
            tif = getattr(self, name)
            if tif not in BRACKET_TIFS:
                raise ValueError(
                    f"{name} must be one of "
                    f"{sorted(value.value for value in BRACKET_TIFS)}, got {tif!r}"
                )
        if not self.account_id:
            raise ValueError("account_id must be non-empty")
        if not self.command_id:
            raise ValueError("command_id must be non-empty (I3)")
        if not isinstance(self.side, Side):
            raise ValueError(f"Invalid side '{self.side}'")
        if self.entry_price <= Decimal("0"):
            raise ValueError(f"entry_price must be positive, got {self.entry_price}")
        if self.stop_loss <= Decimal("0"):
            raise ValueError(f"stop_loss must be positive, got {self.stop_loss}")

        if self.side == Side.BUY:
            if self.stop_loss >= self.entry_price:
                raise ValueError(
                    f"For BUY intent, stop_loss ({self.stop_loss}) must be strictly below entry_price ({self.entry_price})"
                )
            for pt in self.profit_targets:
                if pt <= Decimal("0"):
                    raise ValueError(f"profit_target must be positive, got {pt}")
                if pt <= self.entry_price:
                    raise ValueError(
                        f"For BUY intent, profit target ({pt}) must be strictly above entry_price ({self.entry_price})"
                    )
        elif self.side == Side.SELL:
            if self.stop_loss <= self.entry_price:
                raise ValueError(
                    f"For SELL intent, stop_loss ({self.stop_loss}) must be strictly above entry_price ({self.entry_price})"
                )
            for pt in self.profit_targets:
                if pt <= Decimal("0"):
                    raise ValueError(f"profit_target must be positive, got {pt}")
                if pt >= self.entry_price:
                    raise ValueError(
                        f"For SELL intent, profit target ({pt}) must be strictly below entry_price ({self.entry_price})"
                    )

        if not self.reason:
            raise ValueError("reason must be non-empty (I11)")

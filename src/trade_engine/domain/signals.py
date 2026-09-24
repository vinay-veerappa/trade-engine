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
# (a breakout trigger), so an untriggered breakout never fills. STOP_LIMIT triggers like
# STOP, then buys no higher than entry_limit_price, so a gap past that chase limit does
# not fill.
BRACKET_ENTRY_TYPES = frozenset({OrderType.LIMIT, OrderType.STOP, OrderType.STOP_LIMIT})


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
    quantity_rule: str  # "risk_<x>pct", "notional_<x>pct" or "fixed_<n>", e.g. "risk_0.75pct"
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
    # The limit of a STOP_LIMIT entry, whose stop trigger is entry_price: at or above it
    # for a BUY, at or below it for a SELL. Required for STOP_LIMIT, refused otherwise.
    entry_limit_price: Decimal | None = None
    # Share of the position each profit target exits, in target order. None splits the
    # whole position evenly across the targets. Fractions summing below 1 leave a runner
    # that only the protective stop (or a strategy exit) closes.
    target_fractions: tuple[Decimal, ...] | None = None

    def __post_init__(self) -> None:
        if not self.intent_id:
            raise ValueError("intent_id must be non-empty")
        if self.target_fractions is not None:
            if len(self.target_fractions) != len(self.profit_targets):
                raise ValueError(
                    f"target_fractions has {len(self.target_fractions)} entries for "
                    f"{len(self.profit_targets)} profit targets"
                )
            for fraction in self.target_fractions:
                if not isinstance(fraction, Decimal) or not fraction.is_finite() or fraction <= 0:
                    raise ValueError(
                        f"target_fractions must be finite positive Decimals, got {fraction!r}"
                    )
            if sum(self.target_fractions, Decimal("0")) > 1:
                raise ValueError(
                    f"target_fractions sum to {sum(self.target_fractions, Decimal('0'))}, "
                    "more than the whole position"
                )
        if self.entry_type not in BRACKET_ENTRY_TYPES:
            raise ValueError(
                "entry_type must be one of "
                f"{sorted(value.value for value in BRACKET_ENTRY_TYPES)}, got {self.entry_type!r}"
            )
        if self.entry_type is OrderType.STOP_LIMIT:
            limit = self.entry_limit_price
            if not isinstance(limit, Decimal) or not limit.is_finite() or limit <= 0:
                raise ValueError(
                    "A STOP_LIMIT entry requires entry_limit_price as a finite positive "
                    f"Decimal, got {limit!r}"
                )
            if self.side is Side.BUY and limit < self.entry_price:
                raise ValueError(
                    f"For BUY intent, entry_limit_price ({limit}) must be at or above the "
                    f"stop trigger entry_price ({self.entry_price})"
                )
            if self.side is Side.SELL and limit > self.entry_price:
                raise ValueError(
                    f"For SELL intent, entry_limit_price ({limit}) must be at or below the "
                    f"stop trigger entry_price ({self.entry_price})"
                )
        elif self.entry_limit_price is not None:
            raise ValueError(
                f"entry_limit_price is only for STOP_LIMIT entries, not {self.entry_type.value}"
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

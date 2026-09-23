"""Sink protocol and journal execution definitions (Architecture §2, I5, I12, §4.11)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Protocol, runtime_checkable

from trade_engine.domain.instruments import Side


@dataclass(frozen=True)
class JournalExecution:
    """Execution payload formatted for journal recording.

    Every execution sets assetClass, multiplier, stop, target, strategy tag (Architecture §4.11).
    """

    symbol: str
    side: Side
    quantity: Decimal
    price: Decimal
    fee: Decimal
    executed_at: datetime
    account_id: str  # Journal account id from config, never "first account" (I5)
    asset_class: str  # e.g. "equity", "option"
    multiplier: int = 1
    stop_loss: Decimal | None = None
    profit_target: Decimal | None = None
    strategy_tag: str | None = None
    notes: str | None = None
    fill_id: str | None = None

    def __post_init__(self) -> None:
        if not self.symbol or not self.symbol.strip():
            raise ValueError("symbol must be non-empty string")
        if not isinstance(self.side, Side):
            raise ValueError(f"side must be Side enum, got {self.side}")
        if self.quantity <= Decimal("0"):
            raise ValueError(f"quantity must be positive, got {self.quantity}")
        if self.price <= Decimal("0"):
            raise ValueError(f"price must be positive, got {self.price}")
        if not self.account_id or not self.account_id.strip():
            raise ValueError("account_id must be non-empty string from config (I5)")
        if not self.asset_class or not self.asset_class.strip():
            raise ValueError("asset_class must be non-empty string")
        if self.multiplier <= 0:
            raise ValueError("multiplier must be positive integer")
        if self.executed_at.tzinfo is None or self.executed_at.tzinfo.utcoffset(self.executed_at) is None:
            raise ValueError("executed_at must be timezone-aware UTC datetime (I7)")
        if self.fill_id is not None and not self.fill_id.strip():
            raise ValueError("fill_id must be non-empty string if provided")


@runtime_checkable
class Sink(Protocol):
    """Protocol for event sinks (Journal, Metrics, Reports, WebSockets).

    Outbox drains in order; delivery must be confirmed (I12).
    """

    name: str

    def publish(self, event_seq: int, event: Any) -> bool:
        """Publish an event with its sequence number to the destination sink. Returns True if accepted."""
        ...

    def confirm_delivery(self, event_seq: int) -> bool:
        """Verify delivery confirmation for an event sequence number."""
        ...


@runtime_checkable
class JournalSink(Sink, Protocol):
    """Protocol for trade journal sinks (e.g. :3300).

    Parameterised by base URL and journal account id from config (I5).
    """

    base_url: str
    account_id: str

    def publish_execution(self, event_seq: int, execution: JournalExecution) -> bool:
        """Publish execution, set annotations, and confirm delivery by read-back."""
        ...

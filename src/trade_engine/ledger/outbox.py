"""Outbox data structures and drain results (Architecture §4.11, I12)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any


class OutboxStatus(StrEnum):
    """Lifecycle status of an outbox entry."""

    PENDING = "PENDING"
    DELIVERED = "DELIVERED"
    FAILED = "FAILED"


@dataclass(frozen=True)
class OutboxItem:
    """An outbox entry representing an event pending delivery to an external sink."""

    id: int
    event_seq: int
    destination: str
    payload: dict[str, Any]
    status: OutboxStatus
    attempts: int
    created_at: datetime
    last_error: str | None = None
    delivered_at: datetime | None = None


@dataclass(frozen=True)
class DrainResult:
    """Result of an outbox drain operation for a destination sink."""

    drained_count: int
    failed_item: OutboxItem | None
    error: str | None
    remaining_count: int

    @property
    def ok(self) -> bool:
        """True if all candidate items were delivered without failure."""
        return self.failed_item is None and self.error is None

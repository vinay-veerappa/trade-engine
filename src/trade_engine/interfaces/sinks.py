"""Sink protocol for confirmed event publishing (Architecture §2, I12, §4.11)."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


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

"""Injected clock protocol (Architecture §2, I7)."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable


@runtime_checkable
class Clock(Protocol):
    """Protocol for injected time providers.

    All engine time must be queried through this protocol, never direct datetime.now() (I7).
    """

    def now_utc(self) -> datetime:
        """Return the current time as a UTC-aware datetime."""
        ...

    def sleep(self, seconds: float) -> None:
        """Sleep or advance clock by the given number of seconds."""
        ...

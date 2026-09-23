"""Wall clock implementation reading the host system clock (Architecture §2, I7)."""

from __future__ import annotations

from datetime import datetime, timezone
import time

from trade_engine.interfaces.clock import Clock


class WallClock(Clock):
    """Wall-clock time provider for live/paper trading.

    This is the ONLY class in the trade_engine codebase permitted to read the system clock (I7).
    """

    def now_utc(self) -> datetime:
        """Return the current time as a timezone-aware UTC datetime."""
        return datetime.now(timezone.utc)

    def sleep(self, seconds: float) -> None:
        """Sleep for the given number of seconds."""
        if seconds < 0:
            raise ValueError(f"sleep seconds cannot be negative: {seconds}")
        if seconds > 0:
            time.sleep(seconds)

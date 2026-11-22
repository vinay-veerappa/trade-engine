"""Wall clock implementation reading the host system clock (Architecture §2, I7)."""

from __future__ import annotations

import time
from datetime import datetime

import trade_engine_rs

from trade_engine.interfaces.clock import Clock


class WallClock(Clock):
    """Wall-clock time provider for live/paper trading.

    The native owner clock is the only system-clock source (I7).
    """

    def __init__(self) -> None:
        self._native = trade_engine_rs.NativeWallClock()

    def now_utc(self) -> datetime:
        """Return the current time as a timezone-aware UTC datetime."""
        return self._native.now_utc()

    def sleep(self, seconds: float) -> None:
        """Sleep for the given number of seconds."""
        self._native.sleep(seconds)


def monotonic_seconds() -> float:
    """Elapsed-time reads for stop budgets and heartbeats (not a wall-time read).

    Lives here because this file is the one I7 allowlists for host-clock access."""
    return time.monotonic()


def pause(seconds: float) -> None:
    """A short host sleep for retry back-off outside any Clock (I7 allowlisted)."""
    time.sleep(seconds)

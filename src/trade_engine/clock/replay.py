"""Deterministic replay clock for simulation and backtesting (Architecture §2, I7)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Union

from trade_engine.interfaces.clock import Clock


class ReplayClock(Clock):
    """Deterministic simulated clock.

    Time only advances via advance_to, advance_by, or sleep.
    Time cannot move backwards (I5 / causal replay).
    """

    def __init__(self, initial_time: datetime) -> None:
        if initial_time.tzinfo is None or initial_time.tzinfo.utcoffset(initial_time) is None:
            raise ValueError("initial_time must be a timezone-aware UTC datetime (I7)")
        self._current_time: datetime = initial_time.astimezone(timezone.utc)

    def now_utc(self) -> datetime:
        """Return the current simulated time as a timezone-aware UTC datetime."""
        return self._current_time

    def advance_to(self, target: datetime) -> None:
        """Advance simulated time to target datetime.

        Refuses to move backwards (I5).
        """
        if target.tzinfo is None or target.tzinfo.utcoffset(target) is None:
            raise ValueError("target time must be a timezone-aware UTC datetime (I7)")
        target_utc = target.astimezone(timezone.utc)
        if target_utc < self._current_time:
            raise ValueError(
                f"Cannot advance clock backwards in time: target {target_utc.isoformat()} < current {self._current_time.isoformat()} (I5)"
            )
        self._current_time = target_utc

    def advance_by(self, duration: Union[timedelta, float, int]) -> None:
        """Advance simulated time by a timedelta or float/int seconds."""
        if isinstance(duration, (float, int)):
            if duration < 0:
                raise ValueError(f"Cannot advance clock by negative duration: {duration}")
            delta = timedelta(seconds=duration)
        elif isinstance(duration, timedelta):
            if duration.total_seconds() < 0:
                raise ValueError(f"Cannot advance clock by negative duration: {duration}")
            delta = duration
        else:
            raise TypeError(f"Expected timedelta, float or int, got {type(duration).__name__}")
        self._current_time += delta

    def sleep(self, seconds: float) -> None:
        """Simulate sleep by advancing the clock forward by seconds.

        In replay, sleep does not block wall-clock execution; it advances simulated time.
        """
        if seconds < 0:
            raise ValueError(f"sleep seconds cannot be negative: {seconds}")
        self.advance_by(seconds)

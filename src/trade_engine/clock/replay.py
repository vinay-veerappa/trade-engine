"""Deterministic replay clock for simulation and backtesting (Architecture §2, I7)."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Union

import trade_engine_rs

from trade_engine.interfaces.clock import Clock


class ReplayClock(Clock):
    """Deterministic simulated clock.

    Time only advances via advance_to, advance_by, or sleep.
    Time cannot move backwards (I5 / causal replay).
    """

    def __init__(self, initial_time: datetime) -> None:
        self._native = trade_engine_rs.NativeReplayClock(initial_time)

    @property
    def _current_time(self) -> datetime:
        return self._native.current

    @_current_time.setter
    def _current_time(self, value: datetime) -> None:
        self._native.current = value

    def now_utc(self) -> datetime:
        """Return the current simulated time as a timezone-aware UTC datetime."""
        return self._native.now_utc()

    def advance_to(self, target: datetime) -> None:
        """Advance simulated time to target datetime.

        Refuses to move backwards (I5).
        """
        self._native.advance_to(target)

    def advance_by(self, duration: Union[timedelta, float, int]) -> None:
        """Advance simulated time by a timedelta or float/int seconds."""
        self._native.advance_by(duration)

    def sleep(self, seconds: float) -> None:
        """Simulate sleep by advancing the clock forward by seconds.

        In replay, sleep does not block wall-clock execution; it advances simulated time.
        """
        trade_engine_rs.clock_replay_sleep(self, seconds)

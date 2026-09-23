"""Tests for Clock implementations (Architecture §2, I7)."""

from datetime import datetime, timedelta, timezone
import time

import pytest

from trade_engine.clock import Clock, ReplayClock, WallClock


def test_wall_clock_protocol_and_utc_aware() -> None:
    clock = WallClock()
    assert isinstance(clock, Clock)

    now = clock.now_utc()
    assert now.tzinfo is not None
    assert now.tzinfo.utcoffset(now) == timedelta(0)


def test_wall_clock_sleep_and_negative_rejection() -> None:
    clock = WallClock()
    t0 = time.perf_counter()
    clock.sleep(0.01)
    t1 = time.perf_counter()
    assert t1 - t0 >= 0.005

    with pytest.raises(ValueError, match="cannot be negative"):
        clock.sleep(-0.5)


def test_replay_clock_requires_utc_aware() -> None:
    naive_dt = datetime(2026, 11, 27, 9, 30, 0)
    with pytest.raises(ValueError, match="timezone-aware"):
        ReplayClock(naive_dt)


def test_replay_clock_advance_to_and_backward_refusal() -> None:
    t0 = datetime(2026, 11, 27, 14, 30, 0, tzinfo=timezone.utc)
    clock = ReplayClock(t0)
    assert isinstance(clock, Clock)
    assert clock.now_utc() == t0

    t1 = datetime(2026, 11, 27, 15, 0, 0, tzinfo=timezone.utc)
    clock.advance_to(t1)
    assert clock.now_utc() == t1

    # Backward movement refused (I5)
    t_past = datetime(2026, 11, 27, 14, 45, 0, tzinfo=timezone.utc)
    with pytest.raises(ValueError, match="Cannot advance clock backwards"):
        clock.advance_to(t_past)

    # Naive target refused (I7)
    with pytest.raises(ValueError, match="timezone-aware"):
        clock.advance_to(datetime(2026, 11, 27, 16, 0, 0))


def test_replay_clock_advance_by() -> None:
    t0 = datetime(2026, 11, 27, 14, 30, 0, tzinfo=timezone.utc)
    clock = ReplayClock(t0)

    clock.advance_by(60)  # 60 seconds
    assert clock.now_utc() == t0 + timedelta(seconds=60)

    clock.advance_by(timedelta(minutes=15))
    assert clock.now_utc() == t0 + timedelta(seconds=60) + timedelta(minutes=15)

    with pytest.raises(ValueError, match="negative"):
        clock.advance_by(-10)

    with pytest.raises(ValueError, match="negative"):
        clock.advance_by(timedelta(seconds=-5))

    with pytest.raises(TypeError):
        clock.advance_by("invalid")  # type: ignore[arg-type]


def test_replay_clock_sleep_advances_simulated_time() -> None:
    t0 = datetime(2026, 11, 27, 14, 30, 0, tzinfo=timezone.utc)
    clock = ReplayClock(t0)

    clock.sleep(120.5)
    assert clock.now_utc() == t0 + timedelta(seconds=120.5)

    with pytest.raises(ValueError, match="cannot be negative"):
        clock.sleep(-1.0)

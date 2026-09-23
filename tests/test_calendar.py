"""Tests for exchange calendar and session helpers (Architecture §4.9, E2)."""

from datetime import date, datetime, timezone

import pytest

from trade_engine.calendar import ExchangeCalendar, get_calendar


def test_early_close_handled_2026_11_27() -> None:
    """Acceptance: 2026-11-27 (day after Thanksgiving early close) handled."""
    cal = get_calendar("XNYS")
    d = "2026-11-27"

    assert cal.is_session(d) is True
    assert cal.is_early_close(d) is True
    assert cal.is_holiday(d) is False

    open_dt = cal.session_open(d)
    close_dt = cal.session_close(d)

    # 9:30 AM ET -> 14:30 UTC
    assert open_dt == datetime(2026, 11, 27, 14, 30, tzinfo=timezone.utc)
    # Early close at 1:00 PM ET -> 18:00 UTC (not 21:00 UTC regular close)
    assert close_dt == datetime(2026, 11, 27, 18, 0, tzinfo=timezone.utc)

    # Open during session
    assert cal.is_open_at(datetime(2026, 11, 27, 15, 0, tzinfo=timezone.utc)) is True
    # Closed after 18:00 UTC early close
    assert cal.is_open_at(datetime(2026, 11, 27, 19, 0, tzinfo=timezone.utc)) is False


def test_holiday_handled_2026_12_25() -> None:
    """Acceptance: 2026-12-25 (Christmas Day holiday) handled."""
    cal = get_calendar("XNYS")
    d = "2026-12-25"

    assert cal.is_session(d) is False
    assert cal.is_holiday(d) is True
    assert cal.is_early_close(d) is False

    # Attempting to query session open/close on a holiday raises ValueError (I5 refuse, never guess)
    with pytest.raises(ValueError, match="not a valid trading session"):
        cal.session_open(d)

    with pytest.raises(ValueError, match="not a valid trading session"):
        cal.session_close(d)

    assert cal.is_open_at(datetime(2026, 12, 25, 15, 0, tzinfo=timezone.utc)) is False


def test_regular_session_negative_controls() -> None:
    """Negative controls: regular trading day and non-holiday weekend."""
    cal = get_calendar("XNYS")

    # Regular session
    reg_d = "2026-11-25"
    assert cal.is_session(reg_d) is True
    assert cal.is_early_close(reg_d) is False
    assert cal.is_holiday(reg_d) is False
    assert cal.session_close(reg_d) == datetime(2026, 11, 25, 21, 0, tzinfo=timezone.utc)

    # Weekend (Saturday) is not a session and not an official exchange holiday
    sat = "2026-12-26"
    assert cal.is_session(sat) is False
    assert cal.is_holiday(sat) is False
    assert cal.is_early_close(sat) is False


def test_session_navigation_around_holidays_and_weekends() -> None:
    cal = get_calendar("XNYS")

    # From 2026-12-24 (Thursday), next session skips 25th (Friday holiday) and 26-27 (weekend) to 28th (Monday)
    assert cal.next_session("2026-12-24") == date(2026, 12, 28)
    assert cal.next_session("2026-12-25") == date(2026, 12, 28)

    # From 2026-12-28 (Monday), previous session jumps back to 2026-12-24
    assert cal.previous_session("2026-12-28") == date(2026, 12, 24)
    assert cal.previous_session("2026-12-25") == date(2026, 12, 24)

    # Roll to session
    assert cal.roll_to_session("2026-12-24") == date(2026, 12, 24)
    assert cal.roll_to_session("2026-12-25", direction="next") == date(2026, 12, 28)
    assert cal.roll_to_session("2026-12-25", direction="previous") == date(2026, 12, 24)

    # Sessions in range (Thanksgiving week: 2026-11-26 is closed)
    sessions = cal.sessions_in_range("2026-11-25", "2026-11-30")
    assert sessions == [
        date(2026, 11, 25),
        date(2026, 11, 27),
        date(2026, 11, 30),
    ]


def test_calendar_input_types_and_invariants() -> None:
    cal = ExchangeCalendar("XNYS")

    # date object
    assert cal.is_session(date(2026, 11, 27)) is True

    # aware datetime in UTC
    dt_utc = datetime(2026, 11, 27, 14, 30, tzinfo=timezone.utc)
    assert cal.is_session(dt_utc) is True

    # naive datetime rejected (I7)
    with pytest.raises(ValueError, match="timezone-aware"):
        cal.is_session(datetime(2026, 11, 27, 14, 30))

    with pytest.raises(ValueError, match="timezone-aware"):
        cal.is_open_at(datetime(2026, 11, 27, 14, 30))

    # Invalid string rejected
    with pytest.raises(ValueError, match="Invalid date"):
        cal.is_session("not-a-date")

    # Invalid range start > end
    with pytest.raises(ValueError, match="cannot be after"):
        cal.sessions_in_range("2026-11-30", "2026-11-25")

    # Unknown exchange rejected
    with pytest.raises(ValueError, match="Unknown or unsupported exchange"):
        ExchangeCalendar("NONEXISTENT_EXCHANGE")


def test_leaps_expiry_handled_and_date_range_independent_of_wall_clock() -> None:
    """Finding 4: Ensure future dates (e.g. 2028 LEAPS) do not raise DateOutOfBounds."""
    cal = get_calendar("XNYS")
    # 2028-01-21 is a Friday third-Friday LEAPS expiration session
    assert cal.is_session("2028-01-21") is True
    close_2028 = cal.session_close("2028-01-21")
    assert close_2028 == datetime(2028, 1, 21, 21, 0, tzinfo=timezone.utc)
    # Check MLK Day holiday in 2028
    assert cal.is_session("2028-01-17") is False
    assert cal.is_holiday("2028-01-17") is True


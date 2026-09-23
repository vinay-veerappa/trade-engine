"""Exchange calendar and session helpers (Architecture §4.9, E2)."""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Literal, Union
import zoneinfo

import exchange_calendars as xcals
import pandas as pd

DateLike = Union[date, datetime, str]


class ExchangeCalendar:
    """Exchange session calendar wrapping exchange_calendars (XNYS by default).

    Provides trading session schedules, holiday checks, early-close detection,
    and session navigation. All returned timestamps are timezone-aware UTC.
    """

    def __init__(self, exchange: str = "XNYS") -> None:
        self.exchange = exchange
        try:
            self._cal = xcals.get_calendar(exchange)
        except Exception as e:
            raise ValueError(f"Unknown or unsupported exchange: {exchange!r}") from e
        cal_tz = self._cal.tz
        if isinstance(cal_tz, zoneinfo.ZoneInfo):
            self._tz = cal_tz
        elif hasattr(cal_tz, "zone"):
            self._tz = zoneinfo.ZoneInfo(cal_tz.zone)
        else:
            self._tz = zoneinfo.ZoneInfo(str(cal_tz))

    def _to_date(self, d: DateLike) -> date:
        """Parse DateLike into a pure calendar date in exchange local timezone."""
        if isinstance(d, str):
            try:
                # Handle ISO date string or timestamp string
                if "T" in d or " " in d:
                    dt = datetime.fromisoformat(d)
                    return self._to_date(dt)
                return date.fromisoformat(d)
            except ValueError as e:
                raise ValueError(f"Invalid date string format: {d!r}") from e
        elif isinstance(d, datetime):
            if d.tzinfo is None or d.tzinfo.utcoffset(d) is None:
                raise ValueError(f"Datetime must be timezone-aware (I7): {d!r}")
            # Convert to exchange local timezone and extract date
            return d.astimezone(self._tz).date()
        elif isinstance(d, date):
            return d
        else:
            raise TypeError(f"Expected date, datetime, or str, got {type(d).__name__}")

    def is_session(self, d: DateLike) -> bool:
        """Return True if the given date is an open trading session."""
        session_date = self._to_date(d)
        iso_str = session_date.isoformat()
        return bool(self._cal.is_session(iso_str))

    def is_holiday(self, d: DateLike) -> bool:
        """Return True if the date is an exchange holiday.

        Specifically returns True for official holidays (including weekdays closed for holidays).
        Regular weekend days that are not official holidays return False.
        """
        session_date = self._to_date(d)
        ts = pd.Timestamp(session_date)

        # Check regular holiday calendar for this date
        try:
            is_reg = len(self._cal.regular_holidays.holidays(ts, ts)) > 0
        except Exception:
            is_reg = False

        if is_reg:
            return True

        # Check adhoc holidays
        if ts in self._cal.adhoc_holidays:
            return True

        return False

    def is_early_close(self, d: DateLike) -> bool:
        """Return True if the session closes earlier than regular hours (e.g. 2026-11-27)."""
        session_date = self._to_date(d)
        if not self.is_session(session_date):
            return False
        ts = pd.Timestamp(session_date)
        return ts in self._cal.early_closes

    def session_open(self, d: DateLike) -> datetime:
        """Return market open timestamp as timezone-aware UTC datetime.

        Raises ValueError if date is not a session (I5: refuse, never guess).
        """
        session_date = self._to_date(d)
        iso_str = session_date.isoformat()
        if not self.is_session(session_date):
            raise ValueError(f"Date {iso_str} is not a valid trading session of {self.exchange} (I5)")
        open_ts = self._cal.session_open(iso_str)
        return open_ts.to_pydatetime()

    def session_close(self, d: DateLike) -> datetime:
        """Return market close timestamp as timezone-aware UTC datetime.

        Reflects early close if applicable (e.g. 18:00 UTC / 13:00 ET on 2026-11-27).
        Raises ValueError if date is not a session (I5: refuse, never guess).
        """
        session_date = self._to_date(d)
        iso_str = session_date.isoformat()
        if not self.is_session(session_date):
            raise ValueError(f"Date {iso_str} is not a valid trading session of {self.exchange} (I5)")
        close_ts = self._cal.session_close(iso_str)
        return close_ts.to_pydatetime()

    def next_session(self, d: DateLike) -> date:
        """Return the next active trading session strictly after d."""
        target_date = self._to_date(d)
        iso_str = target_date.isoformat()
        if self.is_session(target_date):
            next_ts = self._cal.next_session(iso_str)
            return next_ts.date()
        # Non-session: find earliest session strictly following target_date
        next_ts = self._cal.date_to_session(iso_str, direction="next")
        return next_ts.date()

    def previous_session(self, d: DateLike) -> date:
        """Return the previous active trading session strictly before d."""
        target_date = self._to_date(d)
        iso_str = target_date.isoformat()
        if self.is_session(target_date):
            prev_ts = self._cal.previous_session(iso_str)
            return prev_ts.date()
        # Non-session: find latest session strictly preceding target_date
        prev_ts = self._cal.date_to_session(iso_str, direction="previous")
        return prev_ts.date()

    def roll_to_session(
        self,
        d: DateLike,
        direction: Literal["next", "previous"] = "next",
    ) -> date:
        """Return d if it is a session; otherwise roll forward/backward to nearest session."""
        target_date = self._to_date(d)
        if self.is_session(target_date):
            return target_date
        iso_str = target_date.isoformat()
        rolled = self._cal.date_to_session(iso_str, direction=direction)
        return rolled.date()

    def sessions_in_range(self, start: DateLike, end: DateLike) -> list[date]:
        """Return list of active session dates between start and end (inclusive)."""
        start_d = self._to_date(start)
        end_d = self._to_date(end)
        if start_d > end_d:
            raise ValueError(f"start date {start_d} cannot be after end date {end_d}")
        sessions = self._cal.sessions_in_range(start_d.isoformat(), end_d.isoformat())
        return [ts.date() for ts in sessions]

    def is_open_at(self, dt: datetime) -> bool:
        """Return True if the market is open at the specified UTC datetime."""
        if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
            raise ValueError(f"Datetime must be timezone-aware (I7): {dt!r}")
        ts = pd.Timestamp(dt)
        return bool(self._cal.is_open_at_time(ts))

"""Exchange calendar and session helpers (Architecture §4.9, E2).

The rules live in Rust (`te_core::calendar`, docs/RUST_PORT.md P1a); this class
only converts Python dates and datetimes to and from what `trade_engine_rs`
takes. No calendar rule may be written here: that would be a second reader.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Iterable, Literal, Union
import zoneinfo

import trade_engine_rs as _rs

DateLike = Union[date, datetime, str]

_SUPPORTED = ("XNYS",)
_MICROSECOND = timedelta(microseconds=1)
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


class ExchangeCalendar:
    """The XNYS session calendar, 2000-01-01 through 2040-12-31.

    Provides trading session schedules, holiday checks, early-close detection,
    and session navigation. All returned timestamps are timezone-aware UTC. A
    date outside the range refuses (I5).
    """

    def __init__(self, exchange: str = "XNYS") -> None:
        if exchange not in _SUPPORTED:
            raise ValueError(f"Unknown or unsupported exchange: {exchange!r}")
        self.exchange = exchange
        self._tz = zoneinfo.ZoneInfo("America/New_York")

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

    def _iso(self, d: DateLike) -> str:
        return self._to_date(d).isoformat()

    def is_session(self, d: DateLike) -> bool:
        """Return True if the given date is an open trading session."""
        return _rs.calendar_is_session(self._iso(d))

    def is_holiday(self, d: DateLike) -> bool:
        """Return True for a weekday closed for an exchange holiday; weekends return False."""
        return _rs.calendar_is_holiday(self._iso(d))

    def is_early_close(self, d: DateLike) -> bool:
        """Return True if the session closes earlier than regular hours (e.g. 2026-11-27)."""
        return _rs.calendar_is_early_close(self._iso(d))

    def session_open(self, d: DateLike) -> datetime:
        """Return market open as timezone-aware UTC; a non-session raises ValueError (I5)."""
        return datetime.fromtimestamp(_rs.calendar_session_open(self._iso(d)), tz=timezone.utc)

    def session_close(self, d: DateLike) -> datetime:
        """Return market close as timezone-aware UTC, reflecting an early close.

        A non-session raises ValueError (I5: refuse, never guess).
        """
        return datetime.fromtimestamp(_rs.calendar_session_close(self._iso(d)), tz=timezone.utc)

    def next_session(self, d: DateLike) -> date:
        """Return the next active trading session strictly after d."""
        return date.fromisoformat(_rs.calendar_next_session(self._iso(d)))

    def previous_session(self, d: DateLike) -> date:
        """Return the previous active trading session strictly before d."""
        return date.fromisoformat(_rs.calendar_previous_session(self._iso(d)))

    def roll_to_session(
        self,
        d: DateLike,
        direction: Literal["next", "previous"] = "next",
    ) -> date:
        """Return d if it is a session; otherwise roll forward/backward to nearest session."""
        if direction not in ("next", "previous"):
            raise ValueError(f"direction must be 'next' or 'previous', got {direction!r}")
        return date.fromisoformat(_rs.calendar_roll_to_session(self._iso(d), direction == "next"))

    def sessions_in_range(self, start: DateLike, end: DateLike) -> list[date]:
        """Return list of active session dates between start and end (inclusive)."""
        return [date.fromisoformat(s) for s in _rs.calendar_sessions_in_range(self._iso(start), self._iso(end))]

    def is_open_at(self, dt: datetime) -> bool:
        """Return True if the market is open at the specified UTC datetime."""
        if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
            raise ValueError(f"Datetime must be timezone-aware (I7): {dt!r}")
        return _rs.calendar_is_open_at((dt - _EPOCH) // _MICROSECOND)

    def missing_sessions(
        self,
        dates: Iterable[DateLike],
        as_of_session: DateLike,
        window: int = 60,
    ) -> list[date]:
        """Return exchange sessions missing from dates within the last window sessions up to as_of_session.

        Counts only from the first date present (a newly listed instrument is not missing
        sessions prior to its inception).
        """
        if window <= 0:
            raise ValueError(f"window must be positive, got {window}")
        target_session = self.roll_to_session(as_of_session, direction="previous")
        have = {self._to_date(d) for d in dates}
        if not have:
            return []
        first = min(have)
        wanted = [date.fromisoformat(s) for s in _rs.calendar_sessions_window_back(target_session.isoformat(), window)]
        return [d for d in wanted if d >= first and d not in have]

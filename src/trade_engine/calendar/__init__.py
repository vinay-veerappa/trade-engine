"""Exchange calendar and session helpers (Architecture §4.9, E2)."""

from functools import lru_cache

from trade_engine.calendar.sessions import ExchangeCalendar

__all__ = ["ExchangeCalendar", "get_calendar"]


@lru_cache(maxsize=8)
def get_calendar(
    exchange: str = "XNYS",
    start: str = "2000-01-01",
    end: str = "2040-12-31",
) -> ExchangeCalendar:
    """Return a cached ExchangeCalendar instance for the requested exchange."""
    return ExchangeCalendar(exchange=exchange, start=start, end=end)

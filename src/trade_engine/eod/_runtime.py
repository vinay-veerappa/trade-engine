"""Plain-value transport for runtime decisions; no host observations or effects."""
from datetime import datetime, timedelta, timezone

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def micros(value: datetime | timedelta) -> int:
    delta = value if isinstance(value, timedelta) else value - _EPOCH
    return (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds

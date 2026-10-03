"""Plain-value transport for runtime decisions; no host observations or effects."""
from datetime import datetime, timedelta, timezone

from trade_engine.sim._rs import call, rs

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def micros(value: datetime | timedelta) -> int:
    delta = value if isinstance(value, timedelta) else value - _EPOCH
    return (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds


def decide(op: str, text=(), numbers=(), flags=(), floats=()):
    return call(rs.runtime_decide, op, list(text), list(numbers), list(flags), list(floats))


def flag(op: str, text=(), numbers=(), flags=(), floats=()) -> bool:
    return decide(op, text, numbers, flags, floats)[2][0]

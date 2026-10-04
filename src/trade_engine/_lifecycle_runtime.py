"""The single P4b door: values in, carriers out; no clock or network ownership."""
from trade_engine.sim._rs import call, register, rs


def decide(op, text=(), numbers=(), flags=(), floats=()):
    return call(rs.runtime_decide, "lifecycle:" + op, list(text), list(numbers),
                list(flags), list(floats))


def flag(op, text=(), numbers=(), flags=()):
    return decide(op, text, numbers, flags)[2][0]


def aware(stamp, what):
    decide("aware", [what], flags=[
        stamp.tzinfo is not None and stamp.tzinfo.utcoffset(stamp) is not None])


def journal(op, *args):
    return call(rs.runtime_journal, op, args)

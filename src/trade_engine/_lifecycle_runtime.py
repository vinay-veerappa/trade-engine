"""The single P4b door: values in, carriers out; no clock or network ownership."""
from trade_engine.sim._rs import call, register, rs


def journal(op, *args):
    return call(rs.runtime_journal, op, args)

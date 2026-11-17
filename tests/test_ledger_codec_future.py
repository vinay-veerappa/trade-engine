"""LEDGER-FUT-1: a Future round-trips Python <-> Rust codec as {root, year, month}."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import trade_engine_rs as rs  # a missing module is an ERROR, never a skip
from trade_engine.domain.instruments import Future
from trade_engine.ledger import codec


def test_future_round_trip():
    fut = Future("MES", 2026, 12)
    data = codec.payload_text(fut).encode()
    assert json.loads(data) == {"dc": "Future", "f": {"root": "MES", "year": 2026, "month": 12}}
    assert bytes(rs.ledger_reencode(data)) == data
    assert codec.build_text(data) == fut

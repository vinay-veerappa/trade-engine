"""LEDGER-FUT-1: a Future round-trips Python <-> Rust codec as {root, year, month}."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import trade_engine_rs as rs  # a missing module is an ERROR, never a skip
from ledger_gen import _ev, _order, dumps

from trade_engine.domain.instruments import Future
from trade_engine.ledger import codec
from trade_engine.ledger.events import EventKind


def test_future_wire_form():
    fut = Future("MES", 2026, 12)
    data = codec.payload_text(fut).encode()
    assert json.loads(data) == {"dc": "Future", "f": {"root": "MES", "year": 2026, "month": 12}}
    assert codec.build_text(data) == fut


def test_future_event_round_trip():
    # ledger_reencode takes a whole event, never a bare instrument.
    fut = Future("MES", 2026, 12)
    event = _ev(EventKind.ORDER_SUBMITTED, _order("of1", instrument=fut))
    data = codec.text(codec.encode_event(event)).encode()
    assert '"Future"' in data.decode()
    # Parity-suite comparison, but against the production codec: the frozen oracle
    # in ledger_gen.py predates Future and refuses it (I5), by design.
    out = bytes(rs.ledger_reencode(data))
    assert dumps(codec.encode_event(codec.decode_event(json.loads(data)))) == out
    assert bytes(rs.ledger_reencode(out)) == out
    assert codec.decode_event(json.loads(out)).payload.instrument == fut

"""W6-F0b-3b: `plain_of`/`plain_py` parity with `json.dumps(default=str)`/`json.loads`.

The two converters (`crates/te_py/src/plain.rs`) are `pub(crate)` — no Python surface — so they
are driven here through `TosBroker`: a host's `place_order` result carries the edge value as
its `reason`; the core reads it with `plain_of` (whose fallback is `default=str`) and answers
the ack with `plain_py`. The ack message must equal `str(json.loads(json.dumps(value,
default=str)))`, proving Decimal -> str, datetime -> `str()` (a space, not `isoformat`'s `T`),
tuple -> list, int keys -> str keys, float -> `repr`, and StrEnum -> its value, with no JSON
text crossing the PyO3 boundary.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from enum import StrEnum

import pytest

import trade_engine_rs as rs


class Color(StrEnum):
    RED = "red"


TICKET = {
    "venue_order_id": "tos:1",
    "instrument": {"kind": "equity", "symbol": "AAPL"},
    "order_type": "LIMIT",
    "side": "SELL",
    "quantity": "100",
    "submitted_at": "2026-09-24T20:00:00+00:00",
    "tif": "DAY",
    "limit_price": "2.00",
    "allocations": [["so-1", "OPT_CSP", "100"]],
}


class _RefusingHost:
    """A `PyBrokerHost` host (the tagged protocol): `place_order` refuses with `reason`."""

    def __init__(self, reason: object) -> None:
        self.reason = reason
        self.spec: object = None

    def now(self) -> str:
        return "2026-09-24T20:00:00+00:00"

    def place_order(self, spec: object, key: str):
        self.spec = spec
        return ("ok", {"status": "REFUSED", "reason": self.reason}, "", "")


class _PositionsHost:
    """A `PyBrokerHost` host whose one position row carries a Decimal quantity."""

    def __init__(self, quantity: object) -> None:
        self.quantity = quantity

    def now(self) -> str:
        return "2026-09-24T20:00:00+00:00"

    def read_positions(self):
        return ("ok", [{"kind": "stock", "symbol": "AAPL", "quantity": self.quantity,
                        "avg_price": "2.00"}], "", "")


def _connected() -> rs.TosBroker:
    broker = rs.TosBroker("D-00000001", ["OPT_CSP"], False)
    broker.mark_connected()
    return broker


@pytest.mark.parametrize(
    "value",
    [
        Decimal("1.50"),
        datetime(2026, 9, 24, 20, 0, tzinfo=timezone.utc),
        (1, 2),
        {5: "x"},
        1e20,
        Color.RED,
    ],
    ids=["Decimal", "datetime", "tuple", "int-keyed-dict", "float-1e20", "StrEnum"],
)
def test_a_refusal_reason_crosses_as_json_dumps_default_str_would(value: object) -> None:
    host = _RefusingHost(value)
    ack = _connected().submit(host, TICKET)
    expected = str(json.loads(json.dumps(value, default=str)))
    assert ack["status"] == "REJECTED"
    assert ack["message"] == f"venue refused: {expected}"


def test_the_ticket_spec_crosses_as_a_python_object_not_json_text() -> None:
    host = _RefusingHost("x")
    _connected().submit(host, TICKET)
    # `plain_py` of the ticket_for spec: an int (not text), a str, and a str limit price.
    assert isinstance(host.spec, dict)
    assert host.spec["kind"] == "stock"
    assert host.spec["symbol"] == "AAPL"
    assert host.spec["quantity"] == 100
    assert host.spec["limit_price"] == "2"  # the canonical money text, as a str


def test_a_decimal_position_row_crosses_as_its_text() -> None:
    # `str(Decimal("100"))` is "100"; `repr` would be "Decimal('100')" (which the core's
    # money parse refuses). Reaching "100" proves `default=str`, not `repr`.
    positions = _connected().positions(_PositionsHost(Decimal("100")))
    assert positions[0]["quantity"] == "100"

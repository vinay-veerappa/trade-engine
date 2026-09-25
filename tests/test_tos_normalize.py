"""T2 golden vectors: raw transport results → domain objects, no network (§4.5).

Each vector is a recorded raw shape and the exact domain object it must become. Unknown
state is pending; nothing normalizes to ACCEPTED; an unreadable row raises (I5).
"""

import sys
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pytest

from trade_engine.domain.instruments import OptionContract, Side
from trade_engine.domain.orders import OrderState, OrderType
from trade_engine.interfaces.broker import VenueAck, VenuePosition
from trade_engine.tos_paper.normalize import (
    NormalizeError,
    WorkingOrder,
    normalize_place_exception,
    normalize_place_result,
    normalize_position,
    normalize_working_order,
)
from trade_engine.tos_paper.transport import MirrorTicket, TransportRefused, TransportReplay

T = datetime(2026, 9, 24, 20, 0, tzinfo=timezone.utc)
P200 = OptionContract(underlying="AAPL", expiry=date(2026, 10, 16), strike=Decimal("200"), right="P")
OCC = "AAPL  261016P00200000"

PLACE_VECTORS = [
    ({"status": "SENT", "echo": {"side": "SELL"}, "idempotency_key": "tos:x"}, ("PENDING", "sent; awaiting read-back")),
    ({"status": "DRY_RUN", "echo": {}}, ("PENDING", "dry run: nothing sent; awaiting reconcile")),
    ({"status": "sent"}, ("PENDING", "sent; awaiting read-back")),
    ({"status": "REFUSED", "reason": "not eligible"}, ("REJECTED", "venue refused: not eligible")),
    ({"status": "REJECTED"}, ("REJECTED", "venue refused: REJECTED")),
    ({"status": "FILLED"}, ("PENDING", "unknown transport status 'FILLED'; awaiting reconcile")),
    ({"status": "ACCEPTED"}, ("PENDING", "unknown transport status 'ACCEPTED'; awaiting reconcile")),
    ({}, ("PENDING", "unknown transport status ''; awaiting reconcile")),
    (None, ("PENDING", "unreadable transport result None; awaiting reconcile")),
]


@pytest.mark.parametrize("raw,expected", PLACE_VECTORS)
def test_place_result_golden_vectors(raw, expected) -> None:
    assert normalize_place_result(raw, "tos:k", T) == VenueAck("tos:k", expected[0], T, expected[1])


def test_no_place_result_is_ever_accepted() -> None:
    assert {normalize_place_result(raw, "tos:k", T).status for raw, _ in PLACE_VECTORS} == {"PENDING", "REJECTED"}


def test_place_exceptions_map_to_rejected_only_when_nothing_was_sent() -> None:
    assert normalize_place_exception(TransportRefused("echo mismatch"), "tos:k", T).status == "REJECTED"
    replay = normalize_place_exception(TransportReplay("key used"), "tos:k", T)
    assert replay.status == "PENDING" and "idempotency replay" in replay.message
    other = normalize_place_exception(TimeoutError("JAB hung"), "tos:k", T)
    assert other.status == "PENDING" and "TimeoutError" in other.message


WORKING_VECTORS = [
    (
        {"symbol": OCC, "side": "SELL", "quantity": "2", "filled": "0", "order_type": "LMT",
         "limit_price": "2.00", "status": "WORKING"},
        WorkingOrder(P200, Side.SELL, Decimal("2"), Decimal("0"), OrderType.LIMIT, Decimal("2.00"), OrderState.ACCEPTED),
    ),
    (
        {"symbol": OCC, "side": "buy", "quantity": 3, "filled": "1", "order_type": "MKT",
         "limit_price": None, "status": "PARTIAL"},
        WorkingOrder(P200, Side.BUY, Decimal("3"), Decimal("1"), OrderType.MARKET, None, OrderState.PARTIALLY_FILLED),
    ),
    (
        {"symbol": OCC, "side": "SELL", "quantity": "1", "order_type": "LMT", "limit_price": "1.5",
         "status": "FILLED"},
        WorkingOrder(P200, Side.SELL, Decimal("1"), Decimal("0"), OrderType.LIMIT, Decimal("1.5"), OrderState.FILLED),
    ),
    (
        {"symbol": OCC, "side": "SELL", "quantity": "1", "order_type": "LMT", "limit_price": "1.5",
         "status": "TRIGGERED?"},
        WorkingOrder(P200, Side.SELL, Decimal("1"), Decimal("0"), OrderType.LIMIT, Decimal("1.5"), OrderState.PENDING_UNKNOWN),
    ),
]


@pytest.mark.parametrize("raw,expected", WORKING_VECTORS)
def test_working_order_golden_vectors(raw, expected) -> None:
    assert normalize_working_order(raw) == expected


@pytest.mark.parametrize(
    "bad",
    [
        {"symbol": "AAPL", "side": "SELL", "quantity": "1", "order_type": "LMT", "status": "WORKING"},
        {"symbol": OCC, "side": "SHORT", "quantity": "1", "order_type": "LMT", "status": "WORKING"},
        {"symbol": OCC, "side": "SELL", "quantity": "1", "order_type": "STOP", "status": "WORKING"},
        {"symbol": OCC, "side": "SELL", "quantity": 1.0, "order_type": "LMT", "status": "WORKING"},
        {"symbol": OCC, "side": "SELL", "quantity": "x", "order_type": "LMT", "status": "WORKING"},
        {"symbol": OCC, "side": "SELL", "quantity": "NaN", "order_type": "LMT", "status": "WORKING"},
        {"symbol": OCC, "side": "SELL", "quantity": "0", "order_type": "LMT", "status": "WORKING"},
        {"symbol": OCC, "side": "SELL", "quantity": "1", "filled": "2", "order_type": "LMT", "status": "WORKING"},
        {"symbol": OCC, "side": "SELL", "quantity": "1", "filled": "-1", "order_type": "LMT", "status": "WORKING"},
    ],
)
def test_unreadable_working_rows_raise_never_guess(bad) -> None:
    with pytest.raises(NormalizeError):
        normalize_working_order(bad)


def test_position_golden_vector() -> None:
    assert normalize_position({"symbol": OCC, "quantity": "-1", "avg_price": "2.10"}, T) == VenuePosition(
        instrument=P200, quantity=Decimal("-1"), avg_price=Decimal("2.10"), as_of=T
    )


def test_unreadable_position_raises() -> None:
    with pytest.raises(NormalizeError):
        normalize_position({"symbol": OCC, "quantity": None, "avg_price": "2.10"}, T)


def _ticket(**changes) -> MirrorTicket:
    fields = dict(
        symbol=OCC, side="SELL", quantity=1, order_type="LMT", limit_price=Decimal("2.00"), tif="DAY",
        underlying="AAPL", expiry=date(2026, 10, 16), strike=Decimal("200"), right="P",
    )
    fields.update(changes)
    return MirrorTicket(**fields)


def test_a_valid_ticket_builds() -> None:
    assert _ticket().quantity == 1
    assert _ticket(order_type="MKT", limit_price=None, tif="GTC", side="BUY").order_type == "MKT"


@pytest.mark.parametrize(
    "changes",
    [
        {"side": "SHORT"},
        {"quantity": 0},
        {"quantity": 1.0},
        {"quantity": True},
        {"order_type": "STOP"},
        {"limit_price": None},
        {"limit_price": Decimal("0")},
        {"order_type": "MKT"},
        {"tif": "GTD"},
    ],
)
def test_ticket_refuses_what_the_driver_cannot_echo(changes) -> None:
    with pytest.raises(ValueError):
        _ticket(**changes)

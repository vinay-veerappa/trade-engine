"""ToS broker and transport behaviour (P0a-T5): order ticket text, normalisation, refusals.

Every assertion is a literal value written from the venue rules (I3 idempotency, I5 nothing
ACCEPTED without proof, I7 timezone-aware UTC, I10 venue env), reached through the public
functions. There is no frozen oracle and no second implementation to compare against.
"""

from __future__ import annotations

import re
import sys
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from trade_engine.domain.instruments import Combo, ComboLeg, Equity, OptionContract, OptionRight, Side  # noqa: E402
from trade_engine.domain.orders import OrderType, TimeInForce  # noqa: E402
from trade_engine.interfaces.broker import (  # noqa: E402
    UnsupportedCapability, VenueAck, VenueIdentity, VenueOrder, VenueOrderAllocation, VenuePosition,
)
from trade_engine.tos_paper import transport as PT  # noqa: E402
from trade_engine.tos_paper.normalize import NormalizeError, normalize_cancel_result  # noqa: E402
from trade_engine.tos_paper.normalize import normalize_place_exception, normalize_place_result  # noqa: E402
from trade_engine.tos_paper.normalize import normalize_position, placed_order_id  # noqa: E402

T = datetime(2026, 9, 24, 20, 0, tzinfo=timezone.utc)
OCC_200P = "AAPL  261016P00200000"
OCC_195P = "AAPL  261016P00195000"


def opt(strike: str = "200", right: str = "P") -> OptionContract:
    return OptionContract(underlying="AAPL", expiry=date(2026, 10, 16), strike=Decimal(strike),
                          right=OptionRight(right), multiplier=100)


P200, P195 = opt(), opt("195")


def venue_order(instrument, side: Side, quantity: str, *, limit: str | None = "1.05",
                order_type: OrderType = OrderType.LIMIT, submitted_at: datetime = T) -> VenueOrder:
    return VenueOrder(
        venue_order_id="tos:t1", instrument=instrument, order_type=order_type, side=side,
        quantity=Decimal(quantity), submitted_at=submitted_at, tif=TimeInForce.DAY,
        limit_price=None if limit is None else Decimal(limit),
        allocations=(VenueOrderAllocation("so-1", "OPT_CSP", Decimal(quantity)),),
    )


# -- the venue guards in interfaces/broker.py ----------------------------------------------------


def test_identity_accepts_an_aware_connect_time() -> None:
    ident = VenueIdentity("OPT_CSP", "paper", T, "TosPaperBroker")
    assert (ident.account_id, ident.env, ident.broker_name) == ("OPT_CSP", "paper", "TosPaperBroker")


def test_identity_refuses_a_naive_connect_time() -> None:
    with pytest.raises(ValueError, match=re.escape("connected_at must be timezone-aware UTC datetime (I7)")):
        VenueIdentity("OPT_CSP", "paper", datetime(2026, 9, 24, 20, 0), "TosPaperBroker")


def test_identity_refuses_an_unknown_venue_env() -> None:
    with pytest.raises(ValueError, match=re.escape("Invalid venue env 'demo' (I10)")):
        VenueIdentity("OPT_CSP", "demo", T, "TosPaperBroker")


def test_order_refuses_a_naive_submit_time() -> None:
    with pytest.raises(ValueError, match=re.escape("submitted_at must be timezone-aware UTC datetime (I7)")):
        venue_order(P200, Side.SELL, "1", submitted_at=datetime(2026, 9, 24, 20, 0))


def test_order_refuses_a_zero_quantity() -> None:
    with pytest.raises(ValueError, match=re.escape("quantity must be positive, got 0")):
        venue_order(P200, Side.SELL, "0")


# -- order ticket text (transport.ticket_for) -----------------------------------------------------


def test_a_limit_option_order_becomes_the_occ_ticket() -> None:
    assert PT.ticket_for(venue_order(P200, Side.SELL, "2", limit="1.05")) == PT.MirrorTicket(
        symbol=OCC_200P, side="SELL", quantity=2, order_type="LMT", limit_price=Decimal("1.05"), tif="DAY",
        underlying="AAPL", expiry=date(2026, 10, 16), strike=Decimal("200"), right="P",
    )


def test_a_market_option_order_carries_no_limit_price() -> None:
    assert PT.ticket_for(venue_order(P200, Side.SELL, "1", limit=None, order_type=OrderType.MARKET)) == PT.MirrorTicket(
        symbol=OCC_200P, side="SELL", quantity=1, order_type="MKT", limit_price=None, tif="DAY",
        underlying="AAPL", expiry=date(2026, 10, 16), strike=Decimal("200"), right="P",
    )


def test_an_equity_limit_order_becomes_a_stock_ticket() -> None:
    assert PT.ticket_for(venue_order(Equity("AAPL"), Side.BUY, "100", limit="1.05")) == PT.MirrorStockTicket(
        symbol="AAPL", side="BUY", quantity=100, order_type="LMT", limit_price=Decimal("1.05"), tif="DAY",
    )


def test_a_credit_vertical_becomes_one_combo_ticket_with_its_price_effect() -> None:
    vertical = Combo([ComboLeg(P200, 1, Side.SELL), ComboLeg(P195, 1, Side.BUY)])
    assert PT.ticket_for(venue_order(vertical, Side.SELL, "1", limit="1.05")) == PT.MirrorComboTicket(
        underlying="AAPL",
        legs=(
            PT.MirrorComboLeg(symbol=OCC_200P, side="SELL", ratio=1, expiry=date(2026, 10, 16),
                              strike=Decimal("200"), right="P"),
            PT.MirrorComboLeg(symbol=OCC_195P, side="BUY", ratio=1, expiry=date(2026, 10, 16),
                              strike=Decimal("195"), right="P"),
        ),
        quantity=1, order_type="LMT", limit_price=Decimal("1.05"), price_effect="CREDIT", tif="DAY",
    )


def test_a_ratio_spread_is_refused_as_unsupported_never_approximated() -> None:
    ratio = Combo([ComboLeg(P200, 1, Side.SELL), ComboLeg(P195, 2, Side.BUY)])
    with pytest.raises(UnsupportedCapability,
                       match=re.escape("multi-leg combo: a 1:2 ratio spread is not a 1:1 vertical")):
        PT.ticket_for(venue_order(ratio, Side.SELL, "1"))


# -- normalisation of venue results (normalize.py) ------------------------------------------------


def test_a_refused_placement_before_send_is_rejected() -> None:
    ack = normalize_place_exception(PT.TransportRefused("echo mismatch"), "tos:k", T)
    assert ack == VenueAck("tos:k", "REJECTED", T, "transport refused before send: echo mismatch")


def test_a_placement_replay_stays_pending_until_reconciled() -> None:
    ack = normalize_place_exception(PT.TransportReplay("key used"), "tos:k", T)
    assert ack == VenueAck("tos:k", "PENDING", T,
                           "idempotency replay: key used; confirming by reconcile (I3)")


def test_a_sent_placement_is_pending_never_accepted() -> None:
    raw = {"status": "SENT", "echo": {"side": "SELL"}, "idempotency_key": "tos:x"}
    assert normalize_place_result(raw, "tos:k", T) == VenueAck("tos:k", "PENDING", T, "sent; awaiting read-back")


def test_a_canceled_book_row_is_the_only_accepted_cancel() -> None:
    assert normalize_cancel_result({"status": "CANCELED"}, "tos:k", T).status == "ACCEPTED"


def test_a_placed_order_id_is_the_digits_the_book_matched() -> None:
    raw = {"status": "SENT", "order_id": "12345", "book_status": "WORKING", "echo": {}}
    assert placed_order_id(raw) == "12345"


def test_a_placed_order_id_is_withheld_when_the_book_row_is_unknown() -> None:
    raw = {"status": "SENT", "order_id": "12345", "book_status": "UNKNOWN", "echo": {}}
    assert placed_order_id(raw) is None


def test_a_position_row_keeps_its_signed_quantity() -> None:
    row = {"symbol": OCC_200P, "quantity": "-1", "avg_price": "2.10"}
    assert normalize_position(row, T) == VenuePosition(P200, Decimal("-1"), Decimal("2.10"), T)


def test_a_position_row_without_a_quantity_is_refused_not_guessed() -> None:
    with pytest.raises(NormalizeError, match=re.escape("quantity must be a decimal string, got None")):
        normalize_position({"symbol": OCC_200P, "quantity": None, "avg_price": "2.10"}, T)

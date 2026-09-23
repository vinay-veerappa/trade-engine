"""Tests for venue-facing contract types (Architecture §4.4, §4.5)."""

from datetime import datetime, timezone
from decimal import Decimal

import pytest
from trade_engine.domain.instruments import Equity, Side
from trade_engine.domain.orders import OrderType
from trade_engine.domain.portfolio import Lot, Position
from trade_engine.interfaces.broker import VenueFill, VenueIdentity, VenueOrder, VenueOrderAllocation
from trade_engine.interfaces.market_data import Bar

T = datetime(2026, 9, 23, 20, 0, tzinfo=timezone.utc)


def _venue_order(**overrides: object) -> VenueOrder:
    kwargs: dict[str, object] = dict(
        venue_order_id="v-1",
        instrument=Equity("AAPL"),
        order_type=OrderType.LIMIT,
        side=Side.BUY,
        quantity=Decimal("30"),
        submitted_at=T,
        limit_price=Decimal("150"),
        allocations=(
            VenueOrderAllocation("so-1", "acc-1", Decimal("10")),
            VenueOrderAllocation("so-2", "acc-2", Decimal("20")),
        ),
    )
    kwargs.update(overrides)
    return VenueOrder(**kwargs)  # type: ignore[arg-type]


def test_netted_venue_order_allocations_must_sum_to_quantity() -> None:
    assert _venue_order().quantity == Decimal("30")  # not-fire
    with pytest.raises(ValueError, match="allocations total 10"):
        _venue_order(allocations=(VenueOrderAllocation("so-1", "acc-1", Decimal("10")),))
    with pytest.raises(ValueError, match="at least one strategy-order allocation"):
        _venue_order(allocations=())


def test_venue_order_prices_match_order_type() -> None:
    with pytest.raises(ValueError, match="LIMIT order must have a limit_price"):
        _venue_order(limit_price=None)
    with pytest.raises(ValueError, match="MARKET order cannot have a limit_price"):
        _venue_order(order_type=OrderType.MARKET)
    assert _venue_order(order_type=OrderType.MARKET, limit_price=None).limit_price is None


def test_venue_fill_requires_side_and_positive_qty_price() -> None:
    ok = VenueFill("f-1", "v-1", Equity("AAPL"), Decimal("5"), Decimal("150"), T, Side.SELL)
    assert ok.side == Side.SELL
    with pytest.raises(TypeError):
        VenueFill("f-1", "v-1", Equity("AAPL"), Decimal("5"), Decimal("150"), T)  # type: ignore[call-arg]
    with pytest.raises(ValueError, match="quantity must be strictly positive"):
        VenueFill("f-1", "v-1", Equity("AAPL"), Decimal("0"), Decimal("150"), T, Side.BUY)
    with pytest.raises(ValueError, match="price must be strictly positive"):
        VenueFill("f-1", "v-1", Equity("AAPL"), Decimal("5"), Decimal("0"), T, Side.BUY)


def test_venue_identity_env_validated() -> None:
    assert VenueIdentity("acc", "paper", T, "sim").env == "paper"
    with pytest.raises(ValueError, match="Invalid venue env"):
        VenueIdentity("acc", "sandbox", T, "sim")  # type: ignore[arg-type]


def test_position_quantity_must_match_open_lots() -> None:
    long_lot = Lot("l-1", Decimal("100"), Decimal("150"), T, Side.BUY)
    short_lot = Lot("l-2", Decimal("30"), Decimal("150"), T, Side.SELL)
    pos = Position("acc-1", Equity("AAPL"), Decimal("70"), Decimal("150"), open_lots=(long_lot, short_lot))
    assert pos.quantity == Decimal("70")
    with pytest.raises(ValueError, match="disagrees with open lots"):
        Position("acc-1", Equity("AAPL"), Decimal("100"), Decimal("150"), open_lots=(long_lot, short_lot))


def test_bar_open_close_within_range() -> None:
    def bar(o: str, c: str) -> Bar:
        return Bar(Equity("AAPL"), T, Decimal(o), Decimal("155"), Decimal("149"), Decimal(c), Decimal("1"), T)

    assert bar("150", "154").close == Decimal("154")
    with pytest.raises(ValueError, match=r"within \[low, high\]"):
        bar("156", "154")
    with pytest.raises(ValueError, match=r"within \[low, high\]"):
        bar("150", "148")


def test_fill_and_lot_side_is_required() -> None:
    """A defaulted side would silently turn a sell into a buy (I5)."""
    from trade_engine.domain.portfolio import Fill

    with pytest.raises(TypeError):
        Fill("f-1", "o-1", "acc-1", Equity("AAPL"), Decimal("5"), Decimal("150"), "sim", T)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        Lot("l-1", Decimal("5"), Decimal("150"), T)  # type: ignore[call-arg]
    assert Fill("f-1", "o-1", "acc-1", Equity("AAPL"), Decimal("5"), Decimal("150"), "sim", T, Side.SELL).side == Side.SELL

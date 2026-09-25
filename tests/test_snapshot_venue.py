"""O4: the options paper venue fills against chain snapshots (rules doc §3, §5.1)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from trade_engine.clock.replay import ReplayClock
from trade_engine.domain.instruments import Combo, ComboLeg, Equity, OptionContract, OptionRight, Side
from trade_engine.domain.orders import OrderState, OrderType, TimeInForce
from trade_engine.interfaces.broker import VenueOrder, VenueOrderAllocation
from trade_engine.interfaces.market_data import OptionQuote
from trade_engine.market_data.chains import ChainSnapshot
from trade_engine.sim import SnapshotVenue, SnapshotVenueError

D = Decimal
ACCOUNT = "OPT_PUT_SPREAD"
EXPIRY = date(2026, 10, 30)
AFTER_CLOSE = datetime(2026, 9, 24, 21, 45, tzinfo=UTC)  # 17:45 ET, the EOD pass
SNAP = datetime(2026, 9, 25, 19, 45, tzinfo=UTC)  # 15:45 ET the next session
CLOSE = datetime(2026, 9, 25, 20, 0, tzinfo=UTC)
P270 = OptionContract("COHR", EXPIRY, D("270"), OptionRight.PUT)
P260 = OptionContract("COHR", EXPIRY, D("260"), OptionRight.PUT)
P250 = OptionContract("COHR", EXPIRY, D("250"), OptionRight.PUT)
COHR = Equity("COHR")
BULL_PUT = Combo((ComboLeg(P270, 1, Side.SELL), ComboLeg(P260, 1, Side.BUY)))
BEAR_PUT = Combo((ComboLeg(P270, 1, Side.BUY), ComboLeg(P260, 1, Side.SELL)))


def quote(contract, bid: str, ask: str, at: datetime = SNAP) -> OptionQuote:
    return OptionQuote(contract, D(bid), D(ask), D(10), D(10), at)


def snapshot(*quotes: OptionQuote, at: datetime = SNAP, price: str = "300") -> ChainSnapshot:
    if not quotes:
        # 270P: mid 10.35, spread 0.50 -> sells at 10.225, buys at 10.475.
        # 260P: mid 7.25, spread 1.10 -> sells at 6.975, buys at 7.525.
        # 250P: mid 4.20, spread 0.40 -> buys at 4.30.
        quotes = (quote(P270, "10.10", "10.60", at), quote(P260, "6.70", "7.80", at), quote(P250, "4.00", "4.40", at))
    return ChainSnapshot("COHR", at, D(price), quotes, None, None, "test")


def order(
    instrument,
    side: Side,
    quantity: str = "1",
    limit: str | None = None,
    tif: TimeInForce = TimeInForce.DAY,
    name: str = "o1",
    at: datetime = AFTER_CLOSE,
) -> VenueOrder:
    return VenueOrder(
        venue_order_id=name,
        instrument=instrument,
        order_type=OrderType.MARKET if limit is None else OrderType.LIMIT,
        side=side,
        quantity=D(quantity),
        submitted_at=at,
        tif=tif,
        limit_price=None if limit is None else D(limit),
        allocations=(VenueOrderAllocation(name, ACCOUNT, D(quantity)),),
    )


@pytest.fixture
def clock():
    return ReplayClock(AFTER_CLOSE)


@pytest.fixture
def venue(clock):
    v = SnapshotVenue(ACCOUNT, clock)
    v.connect()
    return v


def at_snapshot(venue, clock, *orders_: VenueOrder, snap: ChainSnapshot | None = None):
    for o in orders_:
        assert venue.submit(o).status == "ACCEPTED"
    clock.advance_to(SNAP)
    return venue.process_snapshot(snap or snapshot())


def state(venue, name: str = "o1") -> OrderState:
    return next(s.state for s in venue.orders(datetime.min.replace(tzinfo=UTC)) if s.venue_order_id == name)


# -- the fill model: halfway between mid and natural --------------------------------


def test_a_market_sale_fills_a_quarter_spread_below_mid(venue, clock) -> None:
    [fill] = at_snapshot(venue, clock, order(P270, Side.SELL, "2"))
    assert fill.price == D("10.225") and fill.quantity == 2 and fill.side is Side.SELL
    assert fill.fee == D("1.30")  # $0.65 a contract
    assert fill.leg_id is None and fill.filled_at == SNAP


def test_a_market_purchase_fills_a_quarter_spread_above_mid(venue, clock) -> None:
    [fill] = at_snapshot(venue, clock, order(P270, Side.BUY))
    assert fill.price == D("10.475")


def test_a_share_order_fills_at_the_underlying_price_less_slippage(venue, clock) -> None:
    bought, sold = at_snapshot(venue, clock, order(COHR, Side.BUY, "100"), order(COHR, Side.SELL, "100", name="o2"))
    assert bought.price == D("300.15") and bought.fee == 0  # 5 bps against, shares trade free
    assert sold.price == D("299.85")


def test_a_sell_limit_fills_at_its_limit_once_the_model_is_through_it(venue, clock) -> None:
    [fill] = at_snapshot(venue, clock, order(P270, Side.SELL, limit="10.20"))
    assert fill.price == D("10.20")


def test_a_sell_limit_above_the_model_does_not_fill(venue, clock) -> None:
    assert at_snapshot(venue, clock, order(P270, Side.SELL, limit="10.30")) == ()
    assert state(venue) is OrderState.ACCEPTED


def test_a_buy_limit_fills_at_its_limit_and_one_below_the_model_does_not(venue, clock) -> None:
    fills = at_snapshot(
        venue, clock, order(P270, Side.BUY, limit="10.50"), order(P270, Side.BUY, limit="10.40", name="o2")
    )
    assert [(f.venue_order_id, f.price) for f in fills] == [("o1", D("10.50"))]


# -- combos -------------------------------------------------------------------------


def test_a_credit_spread_at_its_limit_collects_exactly_the_limit(venue, clock) -> None:
    short, long = at_snapshot(venue, clock, order(BULL_PUT, Side.SELL, "3", limit="2.30"))
    # Model credit 10.225 - 7.525 = 2.70; the sold leg is shaded to land on 2.30.
    assert (short.instrument, short.side, short.price, short.leg_id) == (P270, Side.SELL, D("9.825"), "0")
    assert (long.instrument, long.side, long.price, long.leg_id) == (P260, Side.BUY, D("7.525"), "1")
    assert short.quantity == long.quantity == 3
    assert short.fee == long.fee == D("1.95")
    assert state(venue) is OrderState.FILLED


def test_a_credit_spread_limit_above_the_model_credit_does_not_fill(venue, clock) -> None:
    assert at_snapshot(venue, clock, order(BULL_PUT, Side.SELL, limit="2.80")) == ()


def test_a_debit_spread_at_its_limit_pays_exactly_the_limit(venue, clock) -> None:
    long, short = at_snapshot(venue, clock, order(BEAR_PUT, Side.BUY, limit="3.60"))
    # Model debit 10.475 - 6.975 = 3.50; the bought leg absorbs the extra 0.10.
    assert (long.price, short.price) == (D("10.575"), D("6.975"))


def test_a_debit_limit_under_the_model_debit_does_not_fill(venue, clock) -> None:
    assert at_snapshot(venue, clock, order(BEAR_PUT, Side.BUY, limit="3.40")) == ()


def test_a_market_combo_fills_every_leg_at_the_model(venue, clock) -> None:
    short, long = at_snapshot(venue, clock, order(BULL_PUT, Side.SELL))
    assert (short.price, long.price) == (D("10.225"), D("7.525"))


def test_rounding_residue_lands_on_a_shaded_leg_so_the_net_is_the_limit(venue, clock) -> None:
    three = Combo((ComboLeg(P270, 1, Side.SELL), ComboLeg(P260, 1, Side.SELL), ComboLeg(P250, 1, Side.BUY)))
    fills = at_snapshot(venue, clock, order(three, Side.SELL, limit="12.00"))
    net = sum((f.price if f.side is Side.SELL else -f.price for f in fills), D(0))
    assert net == D("12.00")
    assert all(f.price == f.price.quantize(D("0.0001")) for f in fills)
    assert fills[2].price == D("4.30")  # the bought leg keeps its model price


def test_a_residue_that_does_not_cancel_is_put_back_on_the_largest_leg(venue, clock) -> None:
    # Three legs sold, 21.30 model credit scaled to 15.01: they round to 15.0099.
    sold = Combo((ComboLeg(P270, 1, Side.SELL), ComboLeg(P260, 1, Side.SELL), ComboLeg(P250, 1, Side.SELL)))
    fills = at_snapshot(venue, clock, order(sold, Side.SELL, limit="15.01"))
    assert sum((f.price for f in fills), D(0)) == D("15.01")
    assert [f.price for f in fills] == [D("7.2056"), D("4.9152"), D("2.8892")]


def test_a_ratio_leg_fills_its_ratio_per_unit(venue, clock) -> None:
    ratio = Combo((ComboLeg(P270, 1, Side.SELL), ComboLeg(P250, 2, Side.BUY)))
    short, long = at_snapshot(venue, clock, order(ratio, Side.SELL, "2", limit="1.50"))
    assert short.quantity == 2 and long.quantity == 4
    assert short.price - 2 * long.price == D("1.50")


# -- what refuses, rejects or waits (I5) ---------------------------------------------


def test_a_market_order_the_snapshot_cannot_price_is_rejected_not_guessed(venue, clock) -> None:
    missing = OptionContract("COHR", EXPIRY, D("240"), OptionRight.PUT)
    assert at_snapshot(venue, clock, order(missing, Side.BUY)) == ()
    assert state(venue) is OrderState.REJECTED


def test_a_limit_the_snapshot_cannot_price_keeps_working(venue, clock) -> None:
    missing = OptionContract("COHR", EXPIRY, D("240"), OptionRight.PUT)
    assert at_snapshot(venue, clock, order(missing, Side.BUY, limit="1.00", tif=TimeInForce.GTC)) == ()
    assert state(venue) is OrderState.ACCEPTED


def test_a_stale_quote_prices_nothing(venue, clock) -> None:
    stale = snapshot(quote(P270, "10.10", "10.60", SNAP - timedelta(hours=1)))
    assert at_snapshot(venue, clock, order(P270, Side.SELL), snap=stale) == ()
    assert state(venue) is OrderState.REJECTED


def test_a_fresh_quote_inside_the_age_limit_prices(venue, clock) -> None:
    fresh = snapshot(quote(P270, "10.10", "10.60", SNAP - timedelta(minutes=10)))
    assert len(at_snapshot(venue, clock, order(P270, Side.SELL), snap=fresh)) == 1


def test_a_sale_with_no_bid_is_not_priced_at_nothing(venue, clock) -> None:
    worthless = snapshot(quote(P270, "0", "0"))
    assert at_snapshot(venue, clock, order(P270, Side.SELL), snap=worthless) == ()
    assert state(venue) is OrderState.REJECTED


def test_a_snapshot_after_the_clock_refuses(venue, clock) -> None:
    venue.submit(order(P270, Side.SELL))
    clock.advance_to(SNAP - timedelta(minutes=1))
    with pytest.raises(SnapshotVenueError, match="look-ahead"):
        venue.process_snapshot(snapshot())


def test_an_order_placed_after_the_snapshot_is_not_filled_by_it(venue, clock) -> None:
    clock.advance_to(SNAP + timedelta(minutes=1))
    venue.submit(order(P270, Side.SELL, at=SNAP + timedelta(minutes=1)))
    assert venue.process_snapshot(snapshot()) == ()


def test_another_underlyings_snapshot_leaves_the_order_alone(venue, clock) -> None:
    other = ChainSnapshot("NVDA", SNAP, D("180"), (), None, None, "test")
    assert at_snapshot(venue, clock, order(P270, Side.SELL), snap=other) == ()
    assert state(venue) is OrderState.ACCEPTED


@pytest.mark.parametrize(
    ("instrument", "message"),
    [
        (Combo((ComboLeg(P270, 1, Side.SELL), ComboLeg(COHR, 100, Side.BUY))), "options only"),
        (
            Combo((ComboLeg(P270, 1, Side.SELL), ComboLeg(OptionContract("NVDA", EXPIRY, D("150"), "P"), 1, Side.BUY))),
            "spans underlyings",
        ),
    ],
)
def test_a_combo_the_venue_cannot_price_as_one_is_refused(venue, instrument, message) -> None:
    with pytest.raises(ValueError, match=message):
        venue.submit(order(instrument, Side.SELL, limit="1.00"))


def test_stop_orders_are_not_supported(venue) -> None:
    stop = VenueOrder(
        venue_order_id="s", instrument=P270, order_type=OrderType.STOP, side=Side.BUY, quantity=D(1),
        submitted_at=AFTER_CLOSE, stop_price=D("20"), allocations=(VenueOrderAllocation("s", ACCOUNT, D(1)),),
    )
    with pytest.raises(ValueError, match="does not support STOP"):
        venue.submit(stop)


# -- time in force --------------------------------------------------------------------


def test_a_day_order_that_missed_its_snapshot_expires_at_the_close(venue, clock) -> None:
    at_snapshot(venue, clock, order(P270, Side.SELL, limit="11.00"))
    clock.advance_to(CLOSE)
    assert state(venue) is OrderState.EXPIRED


def test_a_day_order_whose_session_had_no_snapshot_is_not_declared_unfilled(venue, clock) -> None:
    venue.submit(order(P270, Side.SELL, limit="11.00"))
    clock.advance_to(CLOSE)
    assert state(venue) is OrderState.ACCEPTED


def test_a_gtc_order_works_until_a_later_snapshot_fills_it(venue, clock) -> None:
    at_snapshot(venue, clock, order(P270, Side.BUY, limit="5.00", tif=TimeInForce.GTC))
    clock.advance_to(CLOSE)
    assert state(venue) is OrderState.ACCEPTED
    later = datetime(2026, 9, 28, 19, 45, tzinfo=UTC)
    clock.advance_to(later)
    [fill] = venue.process_snapshot(snapshot(quote(P270, "4.60", "4.80", later), at=later))
    assert fill.price == D("5.00")


def test_a_cancelled_order_does_not_fill(venue, clock) -> None:
    venue.submit(order(P270, Side.SELL))
    assert venue.cancel("o1").status == "ACCEPTED"
    clock.advance_to(SNAP)
    assert venue.process_snapshot(snapshot()) == ()


# -- restore (a new process each EOD run, I2) -----------------------------------------


def test_restore_rebuilds_the_book_and_continues_the_fill_numbering(venue, clock) -> None:
    at_snapshot(venue, clock, order(BULL_PUT, Side.SELL, "2", limit="2.30"), order(P250, Side.BUY, limit="1.00", tif=TimeInForce.GTC, name="o2"))
    since = datetime.min.replace(tzinfo=UTC)
    held = {s.venue_order_id: s.state for s in venue.orders(since)}
    fresh = SnapshotVenue(ACCOUNT, clock)
    fresh.connect()
    fresh.restore(
        [(order(BULL_PUT, Side.SELL, "2", limit="2.30"), held["o1"]), (order(P250, Side.BUY, limit="1.00", tif=TimeInForce.GTC, name="o2"), held["o2"])],
        venue.fills(since),
        venue.positions(),
    )
    assert fresh.orders(since) == venue.orders(since)
    assert [(p.instrument, p.quantity) for p in fresh.positions()] == [(p.instrument, p.quantity) for p in venue.positions()]


def test_restore_refuses_a_fill_the_order_could_not_have_made(venue, clock) -> None:
    [fill] = at_snapshot(venue, clock, order(P270, Side.SELL))
    fresh = SnapshotVenue(ACCOUNT, clock)
    fresh.connect()
    with pytest.raises(SnapshotVenueError, match="does not match"):
        # The recorded fill is on the 270 put; the order restored beside it is for the 260.
        fresh.restore([(order(P260, Side.SELL), OrderState.FILLED)], [fill], [])


def test_restore_refuses_a_filled_order_without_its_fills(venue, clock) -> None:
    fresh = SnapshotVenue(ACCOUNT, clock)
    fresh.connect()
    with pytest.raises(SnapshotVenueError, match="FILLED with 0"):
        fresh.restore([(order(P270, Side.SELL), OrderState.FILLED)], [], [])

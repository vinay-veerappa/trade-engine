"""P0a-T2: simulator and snapshot-venue behaviour, each expectation written out as a literal.

Replaces the venue parts of the parity oracles (test_p3a_parity, test_p3b_parity). Expected
values come from the rules and the arithmetic, not from a frozen oracle.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from trade_engine.calendar.sessions import ExchangeCalendar
from trade_engine.clock.replay import ReplayClock
from trade_engine.domain.instruments import Combo, ComboLeg, Equity, OptionContract, OptionRight, Side
from trade_engine.domain.orders import OrderType, TimeInForce
from trade_engine.interfaces.broker import OrderChanges, VenueOrder, VenueOrderAllocation
from trade_engine.interfaces.market_data import OptionQuote
from trade_engine.market_data.chains import ChainSnapshot
from trade_engine.sim import SnapshotVenue, SnapshotVenueError
from trade_engine.sim.broker import SimBroker, UnknownVenueOrderError
from trade_engine.sim.snapshot_venue import underlying_of

D = Decimal
ACCOUNT = "P0A-T2"
START = datetime(2026, 9, 25, 15, 0, tzinfo=UTC)
SNAP = datetime(2026, 9, 25, 19, 45, tzinfo=UTC)  # 15:45 ET
EXPIRY = date(2026, 10, 30)
XYZ = Equity("XYZ")
COHR = Equity("COHR")
P270 = OptionContract("COHR", EXPIRY, D("270"), OptionRight.PUT)
P260 = OptionContract("COHR", EXPIRY, D("260"), OptionRight.PUT)
P250 = OptionContract("COHR", EXPIRY, D("250"), OptionRight.PUT)
BULL_PUT = Combo((ComboLeg(P270, 1, Side.SELL), ComboLeg(P260, 1, Side.BUY)))
NY = ZoneInfo("America/New_York")
CAL = ExchangeCalendar()


# -- SimBroker: replace and cancel refusals, cash events ------------------------------------


def _limit(name: str, limit: str, quantity: str = "10") -> VenueOrder:
    return VenueOrder(
        venue_order_id=name, instrument=XYZ, order_type=OrderType.LIMIT, side=Side.BUY,
        quantity=D(quantity), submitted_at=START, tif=TimeInForce.GTC, limit_price=D(limit),
        allocations=(VenueOrderAllocation(name, ACCOUNT, D(quantity)),),
    )


@pytest.fixture
def sim():
    broker = SimBroker(ACCOUNT, ReplayClock(START), D("0"))
    broker.connect()
    return broker


def test_a_submitted_sim_order_can_be_replaced_to_a_new_quantity(sim) -> None:
    assert sim.submit(_limit("o1", "10")).status == "ACCEPTED"
    ack = sim.replace("o1", OrderChanges(new_quantity=D("12")))
    assert ack.status == "ACCEPTED"


def test_a_zero_replacement_quantity_is_rejected_with_its_message(sim) -> None:
    sim.submit(_limit("o1", "10"))
    ack = sim.replace("o1", OrderChanges(new_quantity=D("0")))
    assert (ack.status, ack.message) == ("REJECTED", "Replacement quantity must be positive")


def test_a_negative_limit_price_on_replace_is_rejected_with_its_message(sim) -> None:
    sim.submit(_limit("o1", "10"))
    ack = sim.replace("o1", OrderChanges(new_limit_price=D("-1")))
    assert (ack.status, ack.message) == ("REJECTED", "limit_price must be positive, got -1")


def test_a_cancelled_sim_order_cannot_be_replaced(sim) -> None:
    sim.submit(_limit("o1", "10"))
    assert sim.cancel("o1").status == "ACCEPTED"
    ack = sim.replace("o1", OrderChanges(new_quantity=D("12")))
    assert (ack.status, ack.message) == ("REJECTED", "Order is CANCELLED")


def test_replacing_an_order_the_simulator_never_held_raises(sim) -> None:
    with pytest.raises(UnknownVenueOrderError):
        sim.replace("zzz", OrderChanges(new_quantity=D("1")))


def test_the_simulator_reports_no_cash_events(sim) -> None:
    assert sim.cash_events(START) == []


# -- SnapshotVenue: the underlying, replace, cash events -------------------------------------


def test_an_equity_or_option_or_combo_is_priced_from_its_own_underlying() -> None:
    assert underlying_of(XYZ) == "XYZ"
    assert underlying_of(P270) == "COHR"
    assert underlying_of(BULL_PUT) == "COHR"


@pytest.mark.parametrize("instrument", ["XYZ", None])
def test_a_non_instrument_has_no_underlying_to_price_from(instrument) -> None:
    with pytest.raises(ValueError, match="Cannot price"):
        underlying_of(instrument)


def _quote(contract, bid: str, ask: str) -> OptionQuote:
    return OptionQuote(contract, D(bid), D(ask), D(10), D(10), SNAP)


def _snapshot() -> ChainSnapshot:
    # 270P: bid 10.10 ask 10.60 (mid 10.35). 250P: bid 4.00 ask 4.40 (mid 4.20).
    quotes = (_quote(P270, "10.10", "10.60"), _quote(P260, "6.70", "7.80"), _quote(P250, "4.00", "4.40"))
    return ChainSnapshot("COHR", SNAP, D("300"), quotes, None, None, "test")


@pytest.fixture
def snap_venue():
    clock = ReplayClock(START)
    venue = SnapshotVenue(ACCOUNT, clock)
    venue.connect()
    return venue


def _snap_order(name: str = "o1") -> VenueOrder:
    return VenueOrder(
        venue_order_id=name, instrument=P270, order_type=OrderType.LIMIT, side=Side.SELL,
        quantity=D("1"), submitted_at=START, tif=TimeInForce.DAY, limit_price=D("11"),
        allocations=(VenueOrderAllocation(name, ACCOUNT, D("1")),),
    )


def test_replacing_a_submitted_snapshot_order_is_rejected(snap_venue) -> None:
    assert snap_venue.submit(_snap_order()).status == "ACCEPTED"
    ack = snap_venue.replace("o1", OrderChanges(new_limit_price=D("12")))
    assert (ack.status, ack.message) == ("REJECTED", "SnapshotVenue does not replace orders")


def test_replacing_an_unknown_snapshot_order_raises(snap_venue) -> None:
    with pytest.raises(SnapshotVenueError, match="Unknown SnapshotVenue order 'nope'"):
        snap_venue.replace("nope", OrderChanges(new_quantity=D("1")))


def test_the_snapshot_venue_reports_no_cash_events(snap_venue) -> None:
    assert snap_venue.cash_events(START) == []


# -- SnapshotVenue.model_price: the mid less or plus a quarter of the spread ---------------


def test_a_sell_of_the_270_put_fills_at_the_model_bid(snap_venue) -> None:
    assert snap_venue.model_price(P270, Side.SELL, _snapshot()) == D("10.225")


def test_a_buy_of_the_270_put_fills_at_the_model_ask(snap_venue) -> None:
    assert snap_venue.model_price(P270, Side.BUY, _snapshot()) == D("10.475")


def test_a_buy_of_the_250_put_fills_at_its_model_ask(snap_venue) -> None:
    assert snap_venue.model_price(P250, Side.BUY, _snapshot()) == D("4.30")


def test_a_combo_has_no_model_price_from_the_snapshot(snap_venue) -> None:
    assert snap_venue.model_price(BULL_PUT, Side.BUY, _snapshot()) is None


# -- keeper: the session calendar covers early closes and both DST regimes ------------------

SESSIONS = [
    d
    for d in (
        date(2026, 9, 23), date(2026, 9, 25), date(2026, 3, 6), date(2026, 3, 9), date(2026, 10, 30),
        date(2026, 11, 2), date(2026, 11, 25), date(2026, 11, 27), date(2026, 12, 24), date(2026, 12, 31),
        date(2026, 7, 2), date(2026, 4, 2), date(2027, 1, 15),
    )
    if CAL.is_session(d)
]
EARLY = [d for d in SESSIONS if CAL.session_close(d) - CAL.session_open(d) < timedelta(hours=6)]


def test_the_sessions_cover_early_closes_and_dst() -> None:
    assert len(SESSIONS) >= 10
    assert EARLY, "no early close among the sessions"
    offsets = {CAL.session_open(d).astimezone(NY).utcoffset() for d in SESSIONS}
    assert len(offsets) == 2, "both DST regimes must be covered"

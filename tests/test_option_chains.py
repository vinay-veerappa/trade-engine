"""O1: option roots, chain snapshots and their store, greeks."""

from __future__ import annotations

import gzip
import math
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from trade_engine.calendar import get_calendar
from trade_engine.domain.instruments import OptionContract, OptionRight, UnresolvableInstrumentError
from trade_engine.domain.option_roots import (
    Exercise,
    Settlement,
    SettleTime,
    chain_roots,
    last_trade_date,
    option_style,
    settlement_instant,
)
from trade_engine.interfaces.market_data import Greeks, OptionQuote, StaleDataError
from trade_engine.market_data.chains import ChainSnapshot, ChainSnapshotStore
from trade_engine.market_data.greeks import (
    GreeksUnavailable,
    implied_vol,
    model_greeks,
    model_price,
    years_to_settlement,
)

CAL = get_calendar()
T0 = datetime(2026, 9, 24, 19, 45, tzinfo=UTC)  # 15:45 ET


def contract(root="SPY", expiry=date(2026, 10, 16), strike="600", right=OptionRight.PUT) -> OptionContract:
    return OptionContract(underlying=root, expiry=expiry, strike=Decimal(strike), right=right)


def quote(c: OptionContract, bid="1.00", ask="1.10", as_of=T0, **extra) -> OptionQuote:
    return OptionQuote(
        contract=c, bid=Decimal(bid), ask=Decimal(ask), bid_size=Decimal(10), ask_size=Decimal(10), as_of=as_of, **extra
    )


def snapshot(underlying="SPY", as_of=T0, quotes=None, price="610") -> ChainSnapshot:
    if quotes is None:
        quotes = (quote(contract(underlying if underlying != "SPX" else "SPXW"), as_of=as_of),)
    return ChainSnapshot(
        underlying=underlying,
        as_of=as_of,
        underlying_price=Decimal(price),
        quotes=tuple(quotes),
        rate=Decimal("0.04"),
        dividend_yield=Decimal("0.01"),
        source="test",
    )


# -- roots ---------------------------------------------------------------------


def test_spx_and_spxw_are_european_cash_settled_am_and_pm() -> None:
    spx, spxw = option_style("SPX"), option_style("spxw")
    assert (spx.underlying, spx.exercise, spx.settlement, spx.settle_time) == (
        "SPX", Exercise.EUROPEAN, Settlement.CASH, SettleTime.AM
    )
    assert (spxw.underlying, spxw.exercise, spxw.settlement, spxw.settle_time) == (
        "SPX", Exercise.EUROPEAN, Settlement.CASH, SettleTime.PM
    )
    assert chain_roots("SPX") == ("SPX", "SPXW")


def test_an_equity_root_is_american_physical_pm() -> None:
    style = option_style("AAPL")
    assert (style.underlying, style.exercise, style.settlement, style.settle_time) == (
        "AAPL", Exercise.AMERICAN, Settlement.PHYSICAL, SettleTime.PM
    )
    assert chain_roots("AAPL") == ("AAPL",)


@pytest.mark.parametrize("root", ["NDX", "RUTW", "VIX", "XSP"])
def test_an_unmodelled_index_root_refuses(root) -> None:
    with pytest.raises(UnresolvableInstrumentError):
        option_style(root)
    with pytest.raises(UnresolvableInstrumentError):
        chain_roots(root)


def test_settlement_instant_is_the_open_for_am_and_the_close_for_pm() -> None:
    third_friday = date(2026, 10, 16)
    assert settlement_instant(contract("SPX", third_friday), CAL) == datetime(2026, 10, 16, 13, 30, tzinfo=UTC)
    assert settlement_instant(contract("SPXW", third_friday), CAL) == datetime(2026, 10, 16, 20, 0, tzinfo=UTC)
    assert settlement_instant(contract("AAPL", third_friday), CAL) == datetime(2026, 10, 16, 20, 0, tzinfo=UTC)
    # the day after Thanksgiving closes at 13:00 ET
    assert settlement_instant(contract("SPY", date(2026, 11, 27)), CAL) == datetime(2026, 11, 27, 18, 0, tzinfo=UTC)


def test_an_expiry_that_is_not_a_session_refuses() -> None:
    with pytest.raises(ValueError, match="not a session"):
        settlement_instant(contract("SPY", date(2026, 12, 25)), CAL)
    with pytest.raises(ValueError, match="not a session"):
        last_trade_date(contract("SPY", date(2026, 12, 25)), CAL)


def test_am_settled_spx_last_trades_the_session_before_expiry() -> None:
    assert last_trade_date(contract("SPX", date(2026, 10, 16)), CAL) == date(2026, 10, 15)
    assert last_trade_date(contract("SPXW", date(2026, 10, 16)), CAL) == date(2026, 10, 16)
    assert last_trade_date(contract("SPY", date(2026, 10, 16)), CAL) == date(2026, 10, 16)


# -- snapshot ------------------------------------------------------------------


def test_a_fresh_snapshot_passes_and_a_stale_one_refuses() -> None:
    snap = snapshot()
    assert snap.require_fresh(T0 + timedelta(seconds=300), max_age_seconds=300) is snap
    with pytest.raises(StaleDataError, match="301s old"):
        snap.require_fresh(T0 + timedelta(seconds=301), max_age_seconds=300)


def test_a_snapshot_from_after_now_refuses() -> None:
    with pytest.raises(ValueError, match="look-ahead"):
        snapshot().require_fresh(T0 - timedelta(seconds=1), max_age_seconds=300)


def test_a_quote_after_its_snapshot_refuses() -> None:
    later = quote(contract(), as_of=T0 + timedelta(milliseconds=1))
    with pytest.raises(ValueError, match="look-ahead"):
        snapshot(quotes=(later,))
    assert snapshot(quotes=(quote(contract(), as_of=T0),)).quotes  # at the instant is fine


def test_a_quote_on_another_root_refuses() -> None:
    with pytest.raises(ValueError, match="not listed under SPX"):
        snapshot("SPX", quotes=(quote(contract("SPY")),))
    with pytest.raises(ValueError, match="appears twice"):
        snapshot(quotes=(quote(contract()), quote(contract())))


def test_spx_and_spxw_on_the_same_strike_and_date_are_different_contracts() -> None:
    am, pm = contract("SPX", strike="7700"), contract("SPXW", strike="7700")
    snap = snapshot("SPX", quotes=(quote(am, "10", "11"), quote(pm, "12", "13")), price="7705")
    assert am.occ != pm.occ
    assert snap.get(am).bid == Decimal("10") and snap.get(pm).bid == Decimal("12")
    assert [q.contract.occ for q in snap.select(date(2026, 10, 16), OptionRight.PUT, root="SPXW")] == [pm.occ]
    assert len(snap.select(date(2026, 10, 16), OptionRight.PUT)) == 2


def test_quotes_nobody_updated_split_out_as_stale() -> None:
    fresh_c, stale_c = contract(strike="600"), contract(strike="500")
    snap = snapshot(quotes=(quote(fresh_c, as_of=T0 - timedelta(seconds=60)), quote(stale_c, as_of=T0 - timedelta(hours=20))))
    fresh, stale = snap.split_by_quote_age(max_quote_age_seconds=60)
    assert [q.contract for q in fresh] == [fresh_c]
    assert [q.contract for q in stale] == [stale_c]


def test_snapshot_json_round_trips() -> None:
    greeks = Greeks(delta=-0.38, gamma=0.073, theta=-1.382, vega=0.155, rho=-0.008, source="vendor")
    q = quote(contract(), underlying_price=Decimal("610.5"), implied_vol=Decimal("0.129"), greeks=greeks, open_interest=7936)
    snap = snapshot(quotes=(q, quote(contract(strike="590.5", right=OptionRight.CALL))))
    again = ChainSnapshot.from_json(snap.to_json())
    assert again == snap
    assert again.to_json() == snap.to_json()


def test_greeks_refuse_non_finite_or_impossible_values() -> None:
    with pytest.raises(ValueError, match="finite"):
        Greeks(delta=float("nan"), gamma=0, theta=0, vega=0, rho=0, source="vendor")
    with pytest.raises(ValueError, match="outside"):
        Greeks(delta=-999.0, gamma=0, theta=0, vega=0, rho=0, source="vendor")  # Schwab's "no greek"
    with pytest.raises(ValueError, match="source"):
        Greeks(delta=0.5, gamma=0, theta=0, vega=0, rho=0, source="guess")


# -- store ---------------------------------------------------------------------


def test_store_returns_the_newest_snapshot_at_or_before_now(tmp_path) -> None:
    store = ChainSnapshotStore(tmp_path)
    early, late = snapshot(as_of=T0 - timedelta(minutes=5)), snapshot(as_of=T0)
    store.put(early)
    store.put(late)
    store.put(snapshot(as_of=T0 + timedelta(minutes=10)))  # later in the replayed day
    assert store.latest("spy", T0 + timedelta(minutes=1), max_age_seconds=600) == late
    assert store.latest("SPY", T0 - timedelta(minutes=1), max_age_seconds=600) == early


def test_store_refuses_when_nothing_was_taken_yet_or_it_is_stale(tmp_path) -> None:
    store = ChainSnapshotStore(tmp_path)
    with pytest.raises(StaleDataError, match="No SPY chain snapshot"):
        store.latest("SPY", T0, max_age_seconds=600)
    store.put(snapshot(as_of=T0))
    with pytest.raises(StaleDataError, match="No SPY chain snapshot"):
        store.latest("SPY", T0 - timedelta(seconds=1), max_age_seconds=600)
    with pytest.raises(StaleDataError, match="old"):
        store.latest("SPY", T0 + timedelta(seconds=601), max_age_seconds=600)


def test_store_put_is_idempotent_and_never_overwrites(tmp_path) -> None:
    store = ChainSnapshotStore(tmp_path)
    path = store.put(snapshot())
    assert store.put(snapshot()) == path
    with pytest.raises(ValueError, match="already stored"):
        store.put(snapshot(price="611"))
    assert store.load("SPY", T0).underlying_price == Decimal("610")



def test_store_keeps_each_snapshot_gzipped_with_no_timestamp_in_the_header(tmp_path) -> None:
    store = ChainSnapshotStore(tmp_path)
    path = store.put(snapshot())
    assert path.name == "20260924T194500000000Z.json.gz"
    raw = path.read_bytes()
    assert raw[:2] == bytes((0x1F, 0x8B))  # the gzip magic number
    assert gzip.decompress(raw).decode("utf-8") == snapshot().to_json()
    assert raw[4:8] == bytes(4)  # MTIME 0: the bytes depend on the snapshot alone
    (path.parent / "20260924T200000000000Z.json").write_text(snapshot().to_json(), encoding="utf-8")
    (path.parent / "20260924T201500000000Z.json.tmp").write_bytes(raw)  # a write that crashed
    assert store.stamps("SPY") == (T0,)  # only the store's own finished files count

# -- greeks --------------------------------------------------------------------


def test_years_to_settlement_counts_to_the_settlement_instant() -> None:
    c = contract("SPXW", date(2026, 9, 25))
    assert years_to_settlement(c, T0, CAL) == pytest.approx(24.25 / (365 * 24))
    with pytest.raises(GreeksUnavailable, match="settled"):
        years_to_settlement(c, datetime(2026, 9, 25, 20, 0, tzinfo=UTC), CAL)


def test_implied_vol_recovers_the_volatility_that_priced_it() -> None:
    c = contract("SPY", date(2026, 10, 16), "600", OptionRight.PUT)
    args = (Decimal("610"), T0, Decimal("0.04"), Decimal("0.01"), CAL)
    premium = model_price(c, 0.18, *args)
    assert implied_vol(c, Decimal(str(premium)), *args) == pytest.approx(0.18, abs=1e-6)


def test_model_greeks_known_answer() -> None:
    # An ATM put exactly 21 calendar days before a PM expiry; the delta by hand is
    # N(d1) - 1 with d1 = (r + sigma^2/2) t / (sigma sqrt t).
    t = 21 / 365
    d1 = (0.04 + 0.5 * 0.20**2) * t / (0.20 * math.sqrt(t))
    by_hand = 0.5 * (1 + math.erf(d1 / math.sqrt(2))) - 1
    c = contract("SPY", date(2026, 10, 16), "610", OptionRight.PUT)
    g = model_greeks(c, 0.20, Decimal("610"), datetime(2026, 9, 25, 20, 0, tzinfo=UTC), Decimal("0.04"), Decimal("0"), CAL)
    assert g.source == "model"
    assert g.delta == pytest.approx(by_hand, abs=1e-9)
    assert by_hand == pytest.approx(-0.47132, abs=1e-5)
    assert g.gamma > 0 and g.vega > 0 and g.theta < 0 and g.rho < 0
    call = model_greeks(contract("SPY", date(2026, 10, 16), "610", OptionRight.CALL), 0.20, Decimal("610"),
                        datetime(2026, 9, 25, 20, 0, tzinfo=UTC), Decimal("0.04"), Decimal("0"), CAL)
    assert call.delta - g.delta == pytest.approx(1.0, abs=1e-9)  # put-call parity with q = 0


def test_implied_vol_below_intrinsic_refuses() -> None:
    c = contract("SPY", date(2026, 10, 16), "650", OptionRight.PUT)  # 40 in the money
    with pytest.raises(GreeksUnavailable, match="below"):
        implied_vol(c, Decimal("30"), Decimal("610"), T0, Decimal("0.04"), Decimal("0.01"), CAL)


def test_missing_rate_refuses_rather_than_defaulting() -> None:
    with pytest.raises(GreeksUnavailable, match="No rate"):
        implied_vol(contract(), Decimal("5"), Decimal("610"), T0, None, Decimal("0.01"), CAL)
    with pytest.raises(GreeksUnavailable, match="No underlying_price"):
        model_greeks(contract(), 0.2, None, T0, Decimal("0.04"), Decimal("0.01"), CAL)

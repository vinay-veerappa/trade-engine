"""T2 slippage tests: venue-fill allocation, pairing, per-order refusals, weighted mean."""

import sys
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pytest

from trade_engine.domain.instruments import OptionContract, Side
from trade_engine.domain.orders import OrderType
from trade_engine.domain.portfolio import Fill
from trade_engine.interfaces.broker import VenueFill, VenueOrder, VenueOrderAllocation
from trade_engine.tos_paper.slippage import (
    SlippageError,
    SlippagePair,
    SlippageReport,
    allocate_venue_fill,
    slippage_report,
)

T = datetime(2026, 9, 24, 20, 0, tzinfo=timezone.utc)
P200 = OptionContract(underlying="AAPL", expiry=date(2026, 10, 16), strike=Decimal("200"), right="P")
P190 = OptionContract(underlying="AAPL", expiry=date(2026, 10, 16), strike=Decimal("190"), right="P")


def _fill(oid: str, side: Side, qty: str, price: str, env: str = "sim", *, instrument=P200, fid: str | None = None) -> Fill:
    return Fill(
        fill_id=fid or f"{oid}:{env}:{qty}:{price}",
        order_id=oid,
        account_id="OPT_CSP",
        instrument=instrument,
        quantity=Decimal(qty),
        price=Decimal(price),
        venue_env=env,
        filled_at=T,
        side=side,
    )


def _report(sim, venue):
    return slippage_report("TosPaperBroker", T, sim, venue)


# -- pairing ---------------------------------------------------------------------------


def test_buy_venue_above_sim_is_positive_adverse() -> None:
    report = _report([_fill("o-1", Side.BUY, "1", "3.00")], [_fill("o-1", Side.BUY, "1", "3.10", "paper")])
    pair = report.pairs[0]
    assert pair.slippage_points == Decimal("0.10") and pair.slippage_bps == Decimal("333.3333")
    assert report.mean_slippage_bps == Decimal("333.3333")
    assert report.unmatched_sim == () and report.unmatched_venue == () and report.refused == ()


def test_sell_venue_below_sim_is_positive_adverse() -> None:
    report = _report([_fill("o-1", Side.SELL, "1", "3.00")], [_fill("o-1", Side.SELL, "1", "2.90", "paper")])
    assert report.pairs[0].slippage_points == Decimal("0.10")


def test_buy_venue_below_sim_is_negative_better_fill() -> None:
    report = _report([_fill("o-1", Side.BUY, "1", "3.00")], [_fill("o-1", Side.BUY, "1", "2.95", "paper")])
    assert report.pairs[0].slippage_points == Decimal("-0.05")


def test_partial_venue_fills_pair_at_their_vwap() -> None:
    report = _report(
        [_fill("o-1", Side.BUY, "2", "3.00")],
        [_fill("o-1", Side.BUY, "1", "3.00", "paper"), _fill("o-1", Side.BUY, "1", "3.20", "paper")],
    )
    assert report.pairs[0].venue_price == Decimal("3.10")


def test_quantity_drift_refuses_that_order_only() -> None:
    report = _report(
        [_fill("o-1", Side.BUY, "1", "3.00"), _fill("o-2", Side.BUY, "1", "3.00")],
        [_fill("o-1", Side.BUY, "2", "3.00", "paper"), _fill("o-2", Side.BUY, "1", "3.00", "paper")],
    )
    assert [p.order_id for p in report.pairs] == ["o-2"]
    assert "mirror drifted" in dict(report.refused)["o-1"]


def test_side_mismatch_is_refused_not_paired() -> None:
    report = _report([_fill("o-1", Side.BUY, "1", "3.00")], [_fill("o-1", Side.SELL, "1", "3.00", "paper")])
    assert report.pairs == () and "side mismatch" in dict(report.refused)["o-1"]


def test_instrument_mismatch_is_refused_not_paired() -> None:
    report = _report(
        [_fill("o-1", Side.BUY, "1", "3.00")],
        [_fill("o-1", Side.BUY, "1", "3.00", "paper", instrument=P190)],
    )
    assert report.pairs == () and "instrument mismatch" in dict(report.refused)["o-1"]


def test_duplicate_sim_fill_refuses_that_order() -> None:
    dup = _fill("o-1", Side.BUY, "1", "3.00", fid="f-1")
    report = _report([dup, dup, _fill("o-2", Side.BUY, "1", "3.00")], [_fill("o-1", Side.BUY, "2", "3.00", "paper"), _fill("o-2", Side.BUY, "1", "3.00", "paper")])
    assert "sim: duplicate fill id" in dict(report.refused)["o-1"]
    assert [p.order_id for p in report.pairs] == ["o-2"]


def test_duplicate_venue_fill_refuses_that_order() -> None:
    dup = _fill("o-1", Side.BUY, "1", "3.00", "paper", fid="v-1")
    report = _report([_fill("o-1", Side.BUY, "2", "3.00")], [dup, dup])
    assert "venue: duplicate fill id" in dict(report.refused)["o-1"]


def test_distinct_partial_sim_fills_are_not_duplicates() -> None:
    report = _report(
        [_fill("o-1", Side.BUY, "1", "3.00", fid="s-1"), _fill("o-1", Side.BUY, "1", "3.00", fid="s-2")],
        [_fill("o-1", Side.BUY, "2", "3.00", "paper")],
    )
    assert report.refused == () and report.pairs[0].quantity == Decimal("2")


def test_unmatched_fills_listed_not_dropped() -> None:
    report = _report(
        [_fill("o-1", Side.BUY, "1", "3.00"), _fill("o-2", Side.BUY, "1", "3.00")],
        [_fill("o-1", Side.BUY, "1", "3.10", "paper"), _fill("o-9", Side.BUY, "1", "3.00", "paper")],
    )
    assert report.unmatched_sim == ("o-2",) and report.unmatched_venue == ("o-9",)
    assert len(report.pairs) == 1


def test_no_pairs_mean_is_none_not_zero() -> None:
    assert _report([_fill("o-1", Side.BUY, "1", "3.00")], []).mean_slippage_bps is None


def test_mean_is_quantity_weighted() -> None:
    report = _report(
        [_fill("a", Side.BUY, "1", "1.00"), _fill("b", Side.BUY, "3", "1.00")],
        [_fill("a", Side.BUY, "1", "1.10", "paper"), _fill("b", Side.BUY, "3", "1.00", "paper")],
    )
    assert report.mean_slippage_bps == Decimal("250.0000")  # (1000*1 + 0*3)/4, not 500


def test_zero_sim_price_gives_no_bps_and_is_left_out_of_the_mean() -> None:
    zero = _fill("z", Side.BUY, "1", "1.00")
    object.__setattr__(zero, "price", Decimal("0"))  # Fill forbids it; the report must not divide anyway
    report = _report([zero, _fill("a", Side.BUY, "1", "1.00")], [_fill("z", Side.BUY, "1", "0.05", "paper"), _fill("a", Side.BUY, "1", "1.01", "paper")])
    by_id = {p.order_id: p for p in report.pairs}
    assert by_id["z"].slippage_bps is None
    assert report.mean_slippage_bps == Decimal("100.0000")


def test_pair_quantity_must_be_positive() -> None:
    with pytest.raises(SlippageError, match="strictly positive"):
        SlippagePair("o", "X", Side.BUY, Decimal("0"), Decimal("1"), Decimal("1"), Decimal("0"), Decimal("0"))
    SlippagePair("o", "X", Side.BUY, Decimal("1"), Decimal("1"), Decimal("1"), Decimal("0"), Decimal("0"))


def test_report_refuses_empty_venue_name() -> None:
    with pytest.raises(SlippageError, match="venue must be non-empty"):
        SlippageReport(venue="", as_of=T, pairs=(), unmatched_sim=(), unmatched_venue=(), refused=(), mean_slippage_bps=None)


# -- allocation of a netted venue fill ---------------------------------------------------


def _ticket(*alloc: tuple[str, str]) -> VenueOrder:
    allocations = tuple(VenueOrderAllocation(oid, f"ACC_{oid}", Decimal(q)) for oid, q in alloc)
    return VenueOrder(
        venue_order_id="tos:t1", instrument=P200, order_type=OrderType.LIMIT, side=Side.SELL,
        quantity=sum((a.quantity for a in allocations), Decimal("0")), submitted_at=T,
        limit_price=Decimal("2.00"), allocations=allocations,
    )


def _vfill(qty: str, *, fid: str = "vf-1", side: Side = Side.SELL, instrument=P200, fee: str = "0", order: str = "tos:t1") -> VenueFill:
    return VenueFill(fid, order, instrument, Decimal(qty), Decimal("2.05"), T, side, fee=Decimal(fee))


def test_a_full_fill_allocates_each_order_its_quantity() -> None:
    fills = allocate_venue_fill(_vfill("4", fee="1.01"), _ticket(("csp", "1"), ("sp", "3")))
    assert [(f.order_id, f.account_id, f.quantity, f.side, f.venue_env) for f in fills] == [
        ("csp", "ACC_csp", Decimal("1"), Side.SELL, "paper"),
        ("sp", "ACC_sp", Decimal("3"), Side.SELL, "paper"),
    ]
    assert [f.fee for f in fills] == [Decimal("0.26"), Decimal("0.75")]  # the cent remainder to first-in
    assert fills[0].venue_order_id == "tos:t1" and fills[0].fill_id == "vf-1:csp"


def test_partials_allocate_pro_rata_on_the_cumulative_total() -> None:
    ticket = _ticket(("a", "1"), ("b", "1"), ("c", "1"))
    first = allocate_venue_fill(_vfill("1", fid="p1"), ticket)
    assert [(f.order_id, f.quantity) for f in first] == [("a", Decimal("1"))]
    second = allocate_venue_fill(_vfill("2", fid="p2"), ticket, already_filled={"a": Decimal("1")})
    assert [(f.order_id, f.quantity) for f in second] == [("b", Decimal("1")), ("c", Decimal("1"))]


def test_allocation_is_same_side_only() -> None:
    with pytest.raises(SlippageError, match="side"):
        allocate_venue_fill(_vfill("1", side=Side.BUY), _ticket(("a", "1")))


def test_allocation_refuses_a_foreign_fill_or_instrument() -> None:
    with pytest.raises(SlippageError, match="is for"):
        allocate_venue_fill(_vfill("1", order="tos:other"), _ticket(("a", "1")))
    with pytest.raises(SlippageError, match="instrument"):
        allocate_venue_fill(_vfill("1", instrument=P190), _ticket(("a", "1")))


def test_allocation_refuses_an_overfill() -> None:
    with pytest.raises(SlippageError, match="overfill"):
        allocate_venue_fill(_vfill("2"), _ticket(("a", "1"), ("b", "1")), already_filled={"a": Decimal("1")})
    allocate_venue_fill(_vfill("1"), _ticket(("a", "1"), ("b", "1")), already_filled={"a": Decimal("1")})


def test_allocation_refuses_a_non_monotone_history() -> None:
    with pytest.raises(SlippageError, match="not monotone"):
        allocate_venue_fill(_vfill("1"), _ticket(("a", "1"), ("b", "1"), ("c", "1")), already_filled={"c": Decimal("1")})
    allocate_venue_fill(_vfill("1"), _ticket(("a", "1"), ("b", "1"), ("c", "1")), already_filled={"a": Decimal("1")})


def test_allocated_venue_fills_feed_the_report() -> None:
    ticket = _ticket(("csp", "1"), ("sp", "3"))
    venue = allocate_venue_fill(_vfill("4"), ticket)
    sim = [_fill("csp", Side.SELL, "1", "2.10"), _fill("sp", Side.SELL, "3", "2.10")]
    report = _report(sim, list(venue))
    assert [p.slippage_points for p in report.pairs] == [Decimal("0.05"), Decimal("0.05")]

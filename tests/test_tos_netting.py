"""T2 netting tests: same-side netting, cross-account conflicts, refusals (§4.4, §4.7).

Every guard has a firing test and a non-firing test (§0.2); mutation-checked in the
handback. Every input order must end in exactly one outcome (I11).
"""

import sys
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pytest

from trade_engine.domain.instruments import Combo, ComboLeg, Equity, OptionContract, Side
from trade_engine.domain.orders import Order, OrderType, TimeInForce
from trade_engine.tos_paper import netting
from trade_engine.tos_paper.netting import NettingError, net_strategy_orders, ticket_key

T = datetime(2026, 9, 24, 20, 0, tzinfo=timezone.utc)
VENUE = "D-00000001"
MIRRORED = ("OPT_CSP", "OPT_PUT_SPREAD")
P200 = OptionContract(underlying="AAPL", expiry=date(2026, 10, 16), strike=Decimal("200"), right="P")
P190 = OptionContract(underlying="AAPL", expiry=date(2026, 10, 16), strike=Decimal("190"), right="P")


def _order(
    oid: str,
    account: str,
    side: Side,
    qty: str = "1",
    *,
    instrument=P200,
    order_type: OrderType = OrderType.LIMIT,
    limit: str | None = "2.00",
    stop: str | None = None,
    tif: TimeInForce = TimeInForce.DAY,
) -> Order:
    return Order(
        order_id=oid,
        account_id=account,
        instrument=instrument,
        order_type=order_type,
        side=side,
        quantity=Decimal(qty),
        command_id=oid,
        created_at=T,
        limit_price=Decimal(limit) if limit is not None else None,
        stop_price=Decimal(stop) if stop is not None else None,
        tif=tif,
    )


def _net(orders, holdings=None):
    return net_strategy_orders(
        orders, venue_account=VENUE, mirrored_accounts=MIRRORED, at=T, holdings=holdings
    )


def _outcomes(batch) -> list[str]:
    ids = [a.strategy_order_id for vo in batch.venue_orders for a in vo.allocations]
    return sorted(ids + [oid for oid, _ in batch.refused])


def _reason(batch, oid: str) -> str:
    return dict(batch.refused)[oid]


# -- same side nets ---------------------------------------------------------------


def test_same_side_orders_from_two_accounts_sum_into_one_ticket() -> None:
    batch = _net([
        _order("csp-1", "OPT_CSP", Side.SELL, "1"),
        _order("sp-1", "OPT_PUT_SPREAD", Side.SELL, "3"),
    ])
    assert batch.refused == ()
    (ticket,) = batch.venue_orders
    assert ticket.side is Side.SELL and ticket.quantity == Decimal("4")
    assert ticket.limit_price == Decimal("2.00") and ticket.submitted_at == T
    assert [(a.strategy_order_id, a.quantity) for a in ticket.allocations] == [
        ("csp-1", Decimal("1")),
        ("sp-1", Decimal("3")),
    ]


def test_differing_limits_are_separate_tickets_never_averaged() -> None:
    batch = _net([
        _order("csp-1", "OPT_CSP", Side.SELL, "1", limit="2.00"),
        _order("sp-1", "OPT_PUT_SPREAD", Side.SELL, "1", limit="3.00"),
    ])
    assert batch.refused == ()
    assert sorted(vo.limit_price for vo in batch.venue_orders) == [Decimal("2.00"), Decimal("3.00")]
    assert all(vo.quantity == Decimal("1") for vo in batch.venue_orders)


def test_differing_tif_or_type_on_one_side_are_separate_tickets() -> None:
    batch = _net([
        _order("a", "OPT_CSP", Side.SELL, tif=TimeInForce.DAY),
        _order("b", "OPT_PUT_SPREAD", Side.SELL, tif=TimeInForce.GTC),
        _order("c", "OPT_PUT_SPREAD", Side.SELL, order_type=OrderType.MARKET, limit=None),
    ])
    assert batch.refused == () and len(batch.venue_orders) == 3
    market = [vo for vo in batch.venue_orders if vo.order_type is OrderType.MARKET]
    assert market[0].limit_price is None


def test_separate_contracts_are_separate_tickets() -> None:
    batch = _net([
        _order("a", "OPT_CSP", Side.SELL, instrument=P200),
        _order("b", "OPT_PUT_SPREAD", Side.BUY, instrument=P190),
    ])
    assert batch.refused == () and len(batch.venue_orders) == 2


# -- conflicts across virtual accounts ----------------------------------------------


def test_opposite_sides_across_accounts_first_in_wins_later_refused() -> None:
    """The spec's case: a CSP short put and a spread's long leg on the same contract."""
    batch = _net([
        _order("csp-1", "OPT_CSP", Side.SELL, "1"),
        _order("sp-long", "OPT_PUT_SPREAD", Side.BUY, "1"),
    ])
    (ticket,) = batch.venue_orders  # the first-in winner is still sent, not dropped
    assert ticket.side is Side.SELL and ticket.allocations[0].strategy_order_id == "csp-1"
    assert list(dict(batch.refused)) == ["sp-long"]
    assert "conflict" in _reason(batch, "sp-long") and "csp-1" in _reason(batch, "sp-long")
    assert _outcomes(batch) == ["csp-1", "sp-long"]


def test_opposite_side_within_one_account_is_also_refused() -> None:
    batch = _net([
        _order("a1", "OPT_CSP", Side.SELL),
        _order("a2", "OPT_CSP", Side.BUY),
    ])
    assert [a.strategy_order_id for a in batch.venue_orders[0].allocations] == ["a1"]
    assert "conflict" in _reason(batch, "a2")


def test_later_same_side_orders_still_net_after_a_refusal() -> None:
    batch = _net([
        _order("a", "OPT_CSP", Side.SELL, "1"),
        _order("b", "OPT_PUT_SPREAD", Side.BUY, "5"),
        _order("c", "OPT_PUT_SPREAD", Side.SELL, "2"),
    ])
    (ticket,) = batch.venue_orders
    assert ticket.side is Side.SELL and ticket.quantity == Decimal("3")  # never gets b's BUY
    assert {a.strategy_order_id for a in ticket.allocations} == {"a", "c"}
    assert list(dict(batch.refused)) == ["b"]


def test_order_against_another_accounts_holding_is_a_conflict() -> None:
    """Today's spread long leg vs a CSP short already held at the venue."""
    batch = _net(
        [_order("sp-long", "OPT_PUT_SPREAD", Side.BUY, "1")],
        holdings={("OPT_CSP", P200): Decimal("-1")},
    )
    assert batch.venue_orders == ()
    assert "OPT_CSP" in _reason(batch, "sp-long")


def test_closing_ones_own_holding_is_not_a_conflict() -> None:
    batch = _net(
        [_order("csp-close", "OPT_CSP", Side.BUY, "1")],
        holdings={("OPT_CSP", P200): Decimal("-1"), ("OPT_PUT_SPREAD", P190): Decimal("1")},
    )
    assert batch.refused == () and batch.venue_orders[0].side is Side.BUY


def test_same_sign_holding_of_another_account_is_not_a_conflict() -> None:
    batch = _net(
        [_order("csp-2", "OPT_CSP", Side.SELL, "1")],
        holdings={("OPT_PUT_SPREAD", P200): Decimal("-2")},
    )
    assert batch.refused == ()


def test_a_flat_holding_is_not_a_side() -> None:
    batch = _net(
        [_order("sp-long", "OPT_PUT_SPREAD", Side.BUY, "1")],
        holdings={("OPT_CSP", P200): Decimal("0")},
    )
    assert batch.refused == ()


def test_a_position_conflict_refused_first_does_not_set_the_batch_side() -> None:
    batch = _net(
        [
            _order("sp-long", "OPT_PUT_SPREAD", Side.BUY, "1"),
            _order("csp-2", "OPT_CSP", Side.SELL, "1"),
        ],
        holdings={("OPT_CSP", P200): Decimal("-1")},
    )
    assert list(dict(batch.refused)) == ["sp-long"]
    assert batch.venue_orders[0].allocations[0].strategy_order_id == "csp-2"


# -- orders the venue never sees ----------------------------------------------------


def test_unmirrored_account_is_refused_so_csp_never_reaches_the_ira() -> None:
    batch = net_strategy_orders(
        [_order("csp-1", "OPT_CSP", Side.SELL), _order("pcs-1", "OPT_0DTE_PCS_SPX", Side.SELL)],
        venue_account="D-00000002",
        mirrored_accounts=("OPT_0DTE_PCS_SPX",),
        at=T,
    )
    assert "not mirrored" in _reason(batch, "csp-1")
    assert [a.strategy_order_id for a in batch.venue_orders[0].allocations] == ["pcs-1"]


def test_equities_are_not_mirrored() -> None:
    batch = _net([
        _order("eq", "OPT_CSP", Side.BUY, "100", instrument=Equity("AAPL")),
        _order("opt", "OPT_CSP", Side.SELL),
    ])
    assert "equities are not mirrored" in _reason(batch, "eq")
    assert len(batch.venue_orders) == 1


# -- verticals: one combo ticket each, never netted, screened per leg ------------------

SPREAD = Combo((ComboLeg(P200, 1, Side.SELL), ComboLeg(P190, 1, Side.BUY)))  # a put credit spread
P210 = OptionContract(underlying="AAPL", expiry=date(2026, 10, 16), strike=Decimal("210"), right="P")
C200 = OptionContract(underlying="AAPL", expiry=date(2026, 10, 16), strike=Decimal("200"), right="C")
P190_NOV = OptionContract(underlying="AAPL", expiry=date(2026, 11, 20), strike=Decimal("190"), right="P")
MSFT_P190 = OptionContract(underlying="MSFT", expiry=date(2026, 10, 16), strike=Decimal("190"), right="P")
P190_MINI = OptionContract(
    underlying="AAPL", expiry=date(2026, 10, 16), strike=Decimal("190"), right="P", multiplier=10
)


def _spread(oid: str, account: str = "OPT_PUT_SPREAD", qty: str = "1", *, side=Side.SELL, combo=SPREAD, **kw):
    kw.setdefault("limit", "1.00")
    return _order(oid, account, side, qty, instrument=combo, **kw)


def _allocated(batch) -> list[str]:
    return [a.strategy_order_id for t in batch.venue_orders for a in t.allocations]


def test_a_vertical_is_mirrored_as_one_combo_ticket() -> None:
    batch = _net([_spread("sp", qty="2")])
    assert batch.refused == ()
    (ticket,) = batch.venue_orders
    assert ticket.instrument == SPREAD and ticket.side is Side.SELL and ticket.quantity == Decimal("2")
    assert ticket.limit_price == Decimal("1.00") and ticket.order_type is OrderType.LIMIT
    assert [(a.strategy_order_id, a.quantity) for a in ticket.allocations] == [("sp", Decimal("2"))]


def test_two_identical_verticals_are_never_netted() -> None:
    batch = _net([_spread("sp-1"), _spread("sp-2")])
    assert batch.refused == () and len(batch.venue_orders) == 2
    assert [[a.strategy_order_id for a in t.allocations] for t in batch.venue_orders] == [["sp-1"], ["sp-2"]]
    assert batch.venue_orders[0].venue_order_id != batch.venue_orders[1].venue_order_id


def test_a_vertical_and_a_single_on_one_contract_are_not_netted_together() -> None:
    batch = _net([_order("csp", "OPT_CSP", Side.SELL), _spread("sp")])
    assert batch.refused == () and len(batch.venue_orders) == 2
    assert {type(t.instrument) for t in batch.venue_orders} == {OptionContract, Combo}


def test_a_vertical_leg_opposing_a_first_in_single_is_refused() -> None:
    # The CSP buys P200 first; the spread would sell P200: its short leg conflicts.
    batch = _net([_order("csp", "OPT_CSP", Side.BUY), _spread("sp")])
    assert "conflict" in _reason(batch, "sp") and P200.symbol in _reason(batch, "sp")
    assert _allocated(batch) == ["csp"]


def test_a_single_opposing_a_first_in_vertical_leg_is_refused() -> None:
    # The spread buys P190 first; a CSP sale of P190 opposes that leg.
    batch = _net([_spread("sp"), _order("csp", "OPT_CSP", Side.SELL, instrument=P190)])
    assert "conflict" in _reason(batch, "csp") and P190.symbol in _reason(batch, "csp")
    assert _allocated(batch) == ["sp"]


def test_a_vertical_whose_long_leg_opposes_holdings_is_refused() -> None:
    batch = _net([_spread("sp")], holdings={("OPT_CSP", P190): Decimal("-1")})
    assert "opposite side of OPT_CSP" in _reason(batch, "sp")


def test_a_vertical_on_the_same_side_as_holdings_is_not_a_conflict() -> None:
    batch = _net([_spread("sp")], holdings={("OPT_CSP", P200): Decimal("-1")})
    assert batch.refused == () and len(batch.venue_orders) == 1


def test_a_refused_vertical_does_not_claim_its_first_leg() -> None:
    # sp's short P200 leg passes, its P190 leg conflicts with holdings; the later
    # single BUY of P200 must not be refused against the refused spread's leg.
    batch = _net(
        [_spread("sp"), _order("csp", "OPT_CSP", Side.BUY)],
        holdings={("OPT_CSP", P190): Decimal("-1")},
    )
    assert "conflict" in _reason(batch, "sp")
    assert _allocated(batch) == ["csp"]


def test_a_refused_vertical_does_not_move_the_book_for_later_orders() -> None:
    # sp is refused on its P190 leg; had its P200 leg (-1) been booked, the later
    # OPT_CSP BUY of P200 against OPT_PUT_SPREAD's holding would read as mixed signs.
    batch = _net(
        [_spread("sp"), _order("buy", "OPT_PUT_SPREAD", Side.BUY)],
        holdings={("OPT_CSP", P190): Decimal("-1"), ("OPT_PUT_SPREAD", P200): Decimal("1")},
    )
    assert "conflict" in _reason(batch, "sp") and _allocated(batch) == ["buy"]


@pytest.mark.parametrize(
    "legs,why",
    [
        ((ComboLeg(P200, 1, Side.SELL),), "not a 2-leg"),
        ((ComboLeg(P200, 1, Side.SELL), ComboLeg(P190, 1, Side.BUY), ComboLeg(P210, 1, Side.BUY)), "not a 2-leg"),
        ((ComboLeg(P200, 1, Side.SELL), ComboLeg(Equity("AAPL"), 1, Side.BUY)), "not a 2-leg"),
        ((ComboLeg(Equity("AAPL"), 1, Side.BUY), ComboLeg(P200, 1, Side.SELL)), "not a 2-leg"),
        ((ComboLeg(P200, 1, Side.SELL), ComboLeg(MSFT_P190, 1, Side.BUY)), "two underlyings"),
        ((ComboLeg(P200, 1, Side.SELL), ComboLeg(P190_NOV, 1, Side.BUY)), "two expiries"),
        ((ComboLeg(P200, 1, Side.SELL), ComboLeg(C200, 1, Side.BUY)), "call leg and a put leg"),
        ((ComboLeg(P200, 1, Side.SELL), ComboLeg(P190_MINI, 1, Side.BUY)), "two multipliers"),
        ((ComboLeg(P200, 1, Side.SELL), ComboLeg(P200, 1, Side.BUY)), "one strike"),
        ((ComboLeg(P200, 1, Side.SELL), ComboLeg(P190, 1, Side.SELL)), "one side"),
        ((ComboLeg(P200, 1, Side.SELL), ComboLeg(P190, 2, Side.BUY)), "ratio spread"),
    ],
)
def test_any_other_multi_leg_order_is_refused_with_its_reason(legs, why) -> None:
    batch = _net([_spread("bad", combo=Combo(legs)), _order("ok", "OPT_CSP", Side.SELL, instrument=P210)])
    reason = _reason(batch, "bad")
    assert "UnsupportedCapability" in reason and "multi-leg" in reason and why in reason
    assert _allocated(batch) == ["ok"]


def test_a_2x2_vertical_is_still_a_vertical() -> None:
    combo = Combo((ComboLeg(P200, 2, Side.SELL), ComboLeg(P190, 2, Side.BUY)))
    batch = _net([_spread("sp", combo=combo)])
    assert batch.refused == () and batch.venue_orders[0].instrument == combo


def test_a_market_vertical_is_refused() -> None:
    batch = _net([_spread("sp", order_type=OrderType.MARKET, limit=None)])
    assert "one net LIMIT price only" in _reason(batch, "sp")


def test_a_vertical_with_a_bad_tif_or_fractional_units_is_refused() -> None:
    batch = _net([_spread("opg", tif=TimeInForce.OPG), _spread("frac", qty="1.5")])
    assert "TIF" in _reason(batch, "opg") and "whole number" in _reason(batch, "frac")


@pytest.mark.parametrize(
    "order_type,limit,stop",
    [(OrderType.STOP, None, "1.00"), (OrderType.STOP_LIMIT, "1.00", "1.10")],
)
def test_unsupported_order_types_refuse_without_crashing_the_batch(order_type, limit, stop) -> None:
    batch = _net([
        _order("stop", "OPT_CSP", Side.SELL, order_type=order_type, limit=limit, stop=stop),
        _order("ok", "OPT_CSP", Side.SELL),
    ])
    assert "UnsupportedCapability" in _reason(batch, "stop") and "order type" in _reason(batch, "stop")
    assert batch.venue_orders[0].allocations[0].strategy_order_id == "ok"


def test_other_instrument_kinds_refuse_as_unsupported() -> None:
    from trade_engine.domain.instruments import Instrument

    class Future(Instrument):
        @property
        def symbol(self) -> str:
            return "/ES"

    batch = _net([_order("fut", "OPT_CSP", Side.BUY, instrument=Future())])
    assert "UnsupportedCapability" in _reason(batch, "fut") and "not a mirrored option" in _reason(batch, "fut")


def test_unsupported_tif_refuses() -> None:
    batch = _net([_order("moc", "OPT_CSP", Side.SELL, tif=TimeInForce.OPG), _order("ok", "OPT_CSP", Side.SELL)])
    assert "TIF" in _reason(batch, "moc") and len(batch.venue_orders) == 1


def test_fractional_quantity_refuses_never_truncates() -> None:
    batch = _net([_order("frac", "OPT_CSP", Side.SELL, "1.5"), _order("ok", "OPT_CSP", Side.SELL, "2")])
    assert "whole number" in _reason(batch, "frac")
    assert batch.venue_orders[0].quantity == Decimal("2")


def test_duplicate_order_id_in_a_batch_refuses_the_second() -> None:
    batch = _net([_order("a", "OPT_CSP", Side.SELL), _order("a", "OPT_CSP", Side.SELL)])
    assert batch.venue_orders[0].quantity == Decimal("1")
    assert "duplicate" in _reason(batch, "a")
    assert _outcomes(batch) == ["a", "a"]


def test_empty_batch_refuses() -> None:
    with pytest.raises(NettingError, match="no strategy orders"):
        _net([])


def test_a_ticket_error_refuses_its_orders_not_the_batch(monkeypatch) -> None:
    real = netting._ticket

    def flaky(venue_account, instrument, *args):
        if instrument == P190:
            raise ValueError("boom")
        return real(venue_account, instrument, *args)

    monkeypatch.setattr(netting, "_ticket", flaky)
    batch = _net([_order("a", "OPT_CSP", Side.SELL, instrument=P200), _order("b", "OPT_CSP", Side.SELL, instrument=P190)])
    assert "boom" in _reason(batch, "b")
    assert batch.venue_orders[0].allocations[0].strategy_order_id == "a"


def test_every_order_ends_in_exactly_one_outcome() -> None:
    orders = [
        _order("a", "OPT_CSP", Side.SELL),
        _order("b", "OPT_PUT_SPREAD", Side.BUY),
        _order("c", "OPT_WHEEL_CORE", Side.SELL),
        _order("d", "OPT_PUT_SPREAD", Side.SELL, instrument=P190),
        _order("e", "OPT_PUT_SPREAD", Side.SELL, "0.5", instrument=P190),
    ]
    batch = _net(orders)
    assert _outcomes(batch) == sorted(o.order_id for o in orders)


def test_the_outcome_invariant_itself_fires() -> None:
    with pytest.raises(NettingError, match="lost or duplicated"):
        netting._account_for_everything([_order("a", "OPT_CSP", Side.SELL)], [], [])
    netting._account_for_everything([_order("a", "OPT_CSP", Side.SELL)], [], [("a", "x")])


# -- idempotency key ----------------------------------------------------------------


def test_the_same_batch_gives_the_same_ticket_id() -> None:
    first = _net([_order("a", "OPT_CSP", Side.SELL), _order("b", "OPT_PUT_SPREAD", Side.SELL)])
    again = _net([_order("a", "OPT_CSP", Side.SELL), _order("b", "OPT_PUT_SPREAD", Side.SELL)])
    assert first.venue_orders[0].venue_order_id == again.venue_orders[0].venue_order_id
    assert first.venue_orders[0].venue_order_id.startswith("tos:")


@pytest.mark.parametrize(
    "change",
    ["venue", "contract", "side", "qty", "type", "limit", "tif", "orders"],
)
def test_any_content_change_gives_a_new_key(change) -> None:
    base = dict(
        venue_account=VENUE, instrument=P200, side=Side.SELL, quantity=Decimal("2"),
        order_type=OrderType.LIMIT, limit_price=Decimal("2.00"), tif=TimeInForce.DAY,
        order_ids=["a", "b"],
    )
    changed = dict(base)
    changed.update({
        "venue": {"venue_account": "D-00000002"},
        "contract": {"instrument": P190},
        "side": {"side": Side.BUY},
        "qty": {"quantity": Decimal("3")},
        "type": {"order_type": OrderType.MARKET, "limit_price": None},
        "limit": {"limit_price": Decimal("2.05")},
        "tif": {"tif": TimeInForce.GTC},
        "orders": {"order_ids": ["a", "c"]},
    }[change])
    assert ticket_key(**base) != ticket_key(**changed)
    assert ticket_key(**base) == ticket_key(**{**base, "order_ids": ["b", "a"]})


def test_a_conflict_names_the_first_in_order_not_a_later_one() -> None:
    batch = _net([
        _order("first", "OPT_CSP", Side.SELL),
        _order("second", "OPT_PUT_SPREAD", Side.SELL),
        _order("late", "OPT_CSP", Side.BUY),
    ])
    assert "first-in SELL first" in _reason(batch, "late")

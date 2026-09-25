"""O4: a combo order folds leg by leg (Architecture §4.1, I1, I5)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from trade_engine.domain.instruments import Combo, ComboLeg, OptionContract, OptionRight, Side
from trade_engine.domain.orders import Order, OrderState, OrderType
from trade_engine.domain.portfolio import Fill
from trade_engine.ledger import CashFlow, Event, EventKind, LedgerFoldError, fold
from trade_engine.ledger.state import LedgerFillMismatchError

AT = datetime(2026, 9, 25, 19, 45, tzinfo=UTC)
EXPIRY = date(2026, 10, 30)
D = Decimal
SHORT = OptionContract("COHR", EXPIRY, D("270"), OptionRight.PUT)
LONG = OptionContract("COHR", EXPIRY, D("260"), OptionRight.PUT)
# A bull put spread: sell the 270 put, buy the 260 put, for a net credit.
SPREAD = Combo((ComboLeg(SHORT, 1, Side.SELL), ComboLeg(LONG, 1, Side.BUY)))


def _order(quantity: str = "2", instrument=SPREAD, side: Side = Side.SELL) -> Order:
    return Order(
        order_id="entry",
        account_id="OPT_PUT_SPREAD",
        instrument=instrument,
        order_type=OrderType.LIMIT,
        side=side,
        quantity=D(quantity),
        command_id="c1",
        created_at=AT,
        limit_price=D("2.30"),
    )


def _fill(n: int, leg: str | None, contract, side: Side, quantity: str, price: str) -> Fill:
    return Fill(
        fill_id=f"f{n}",
        order_id="entry",
        account_id="OPT_PUT_SPREAD",
        instrument=contract,
        quantity=D(quantity),
        price=D(price),
        venue_env="sim",
        filled_at=AT,
        side=side,
        fee=D("0.65") * D(quantity),
        leg_id=leg,
    )


def _events(order: Order, *fills: Fill) -> list[Event]:
    events = [
        Event(account="OPT_PUT_SPREAD", kind=EventKind.CASH_FLOW, ts_utc=AT, command_id="deposit",
              payload=CashFlow(amount=D("50000"), kind="deposit", as_of=AT)),
        Event(account="OPT_PUT_SPREAD", kind=EventKind.ORDER_SUBMITTED, payload=order, ts_utc=AT,
              command_id="c1:submit"),
    ]
    events += [
        Event(account="OPT_PUT_SPREAD", kind=EventKind.FILL, payload=fill, ts_utc=AT, command_id=f"fill:{fill.fill_id}")
        for fill in fills
    ]
    return events


def _state(order: Order, *fills: Fill):
    return fold(_events(order, *fills))["OPT_PUT_SPREAD"]


def test_a_spread_fills_leg_by_leg_into_one_position_per_contract() -> None:
    state = _state(
        _order(),
        _fill(1, "0", SHORT, Side.SELL, "2", "10.00"),
        _fill(2, "1", LONG, Side.BUY, "2", "7.70"),
    )
    assert state.positions[SHORT].quantity == -2 and state.positions[LONG].quantity == 2
    assert state.positions[SHORT].avg_cost == D("10.00") and state.positions[LONG].avg_cost == D("7.70")
    # Two units at a 2.30 credit, less four contracts of fees.
    assert state.cash == D("50000") + D("2000") - D("1540") - D("2.60")
    assert state.filled_quantity["entry"] == 2
    assert state.orders["entry"].state is OrderState.FILLED
    assert state.leg_filled[("entry", 0)] == 2 and state.leg_filled[("entry", 1)] == 2


def test_a_combo_is_only_filled_once_every_leg_is() -> None:
    state = _state(_order(), _fill(1, "0", SHORT, Side.SELL, "2", "10.00"))
    assert state.filled_quantity["entry"] == 0  # the long leg has not traded
    assert state.orders["entry"].state is OrderState.PARTIALLY_FILLED


def test_legs_filled_at_different_rates_count_the_units_all_legs_completed() -> None:
    state = _state(
        _order(),
        _fill(1, "0", SHORT, Side.SELL, "2", "10.00"),
        _fill(2, "1", LONG, Side.BUY, "1", "7.70"),
    )
    assert state.filled_quantity["entry"] == 1


def test_a_ratio_leg_fills_ratio_contracts_per_unit() -> None:
    ratio = Combo((ComboLeg(SHORT, 1, Side.SELL), ComboLeg(LONG, 2, Side.BUY)))
    state = _state(
        _order("1", ratio),
        _fill(1, "0", SHORT, Side.SELL, "1", "10.00"),
        _fill(2, "1", LONG, Side.BUY, "2", "3.00"),
    )
    assert state.positions[LONG].quantity == 2
    assert state.filled_quantity["entry"] == 1
    assert state.orders["entry"].state is OrderState.FILLED


def test_a_leg_takes_its_own_side_whatever_the_combo_is_paid_or_collected() -> None:
    # A debit (BUY) order over the same legs still sells the short leg.
    state = _state(
        _order(side=Side.BUY),
        _fill(1, "0", SHORT, Side.SELL, "2", "10.00"),
        _fill(2, "1", LONG, Side.BUY, "2", "7.70"),
    )
    assert state.positions[SHORT].quantity == -2


def test_a_single_contract_order_still_folds_as_before() -> None:
    order = _order("1", SHORT)
    state = _state(order, _fill(1, None, SHORT, Side.SELL, "1", "10.00"))
    assert state.positions[SHORT].quantity == -1
    assert state.filled_quantity["entry"] == 1
    assert state.leg_filled == {}


@pytest.mark.parametrize(
    ("fill", "message"),
    [
        (_fill(1, None, SHORT, Side.SELL, "2", "10.00"), "must name one of its 2 legs"),
        (_fill(1, "2", SHORT, Side.SELL, "2", "10.00"), "must name one of its 2 legs"),
        (_fill(1, "x", SHORT, Side.SELL, "2", "10.00"), "must name one of its 2 legs"),
        (_fill(1, "1", SHORT, Side.SELL, "2", "10.00"), "which is COHR"),
        (_fill(1, "0", SHORT, Side.BUY, "2", "10.00"), "refusing the wrong direction"),
    ],
)
def test_a_combo_fill_that_contradicts_its_leg_refuses(fill: Fill, message: str) -> None:
    with pytest.raises(LedgerFillMismatchError, match=message):
        _state(_order(), fill)


def test_a_leg_filled_past_its_ordered_contracts_refuses() -> None:
    with pytest.raises(LedgerFoldError, match="refusing the over-fill"):
        _state(
            _order(),
            _fill(1, "0", SHORT, Side.SELL, "2", "10.00"),
            _fill(2, "0", SHORT, Side.SELL, "1", "10.00"),
        )


def test_a_single_contract_fill_on_another_contract_still_refuses() -> None:
    with pytest.raises(LedgerFillMismatchError, match="contradicts"):
        _state(_order("1", SHORT), _fill(1, None, LONG, Side.SELL, "1", "10.00"))

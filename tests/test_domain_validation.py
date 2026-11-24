"""P0a-T8: direct constructor validation for the domain dataclasses.

Each refusal row builds one otherwise-valid object through its public constructor, changes
exactly one field, and asserts the exact exception type and a stable message fragment. The
rows cover the raises in domain/instruments.py, orders.py, portfolio.py, risk.py, signals.py
and interfaces/broker.py (Route) that the P0 gate's coverage run lost (T8_LOST_LINES.txt).
The positive controls at the bottom build each valid type, so a broken fixture cannot make
every refusal row pass vacuously.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from trade_engine.accounts.errors import AccountRuleError
from trade_engine.domain.instruments import Combo, ComboLeg, Equity, OptionContract, OptionRight, Side
from trade_engine.domain.orders import Order, OrderType
from trade_engine.domain.portfolio import Fill
from trade_engine.domain.risk import RiskControlChange, RiskRuleResult, RiskVerdict
from trade_engine.domain.signals import Signal
from trade_engine.interfaces.broker import Route

T0 = datetime(2026, 11, 20, 15, 0, tzinfo=timezone.utc)
AAPL = Equity("AAPL")
AAPL_CALL = OptionContract("AAPL", date(2026, 12, 18), Decimal("200"), OptionRight.CALL)


# -- valid fixtures (one per type) -----------------------------------------------------------


def _order(**overrides) -> Order:
    fields = dict(
        order_id="o1",
        account_id="ACC",
        instrument=AAPL,
        order_type=OrderType.MARKET,
        side=Side.BUY,
        quantity=Decimal("10"),
        command_id="c1",
        created_at=T0,
    )
    fields.update(overrides)
    return Order(**fields)


def _fill(**overrides) -> Fill:
    fields = dict(
        fill_id="f1",
        order_id="o1",
        account_id="ACC",
        instrument=AAPL,
        quantity=Decimal("10"),
        price=Decimal("100"),
        venue_env="sim",
        filled_at=T0,
        side=Side.BUY,
    )
    fields.update(overrides)
    return Fill(**fields)


PASSING = RiskRuleResult("max_qty", True, Decimal("5"), Decimal("10"), "within limit")
FAILING = RiskRuleResult("max_qty", False, Decimal("50"), Decimal("10"), "over limit")


def _verdict(**overrides) -> RiskVerdict:
    fields = dict(order_intent_id="oi-1", evaluations=(PASSING,))
    fields.update(overrides)
    return RiskVerdict(**fields)


def _control(**overrides) -> RiskControlChange:
    fields = dict(control_id="venue:halt", enabled=True, reason="drift", changed_at=T0)
    fields.update(overrides)
    return RiskControlChange(**fields)


def _signal(**overrides) -> Signal:
    fields = dict(
        signal_id="s1",
        scan_id="scan-1",
        symbol="AAPL",
        session_date=date(2026, 11, 20),
        direction="long",
        metrics={"score": Decimal("0.9")},
    )
    fields.update(overrides)
    return Signal(**fields)


def _route(**overrides) -> Route:
    fields = dict(platform="tos", local_name="ACCT-TEST-194")
    fields.update(overrides)
    return Route(**fields)


def _leg(**overrides) -> ComboLeg:
    fields = dict(contract=AAPL_CALL, ratio=1, side=Side.BUY)
    fields.update(overrides)
    return ComboLeg(**fields)


def _refusal(factory, exc_type: type[BaseException], message: str) -> None:
    """The factory must refuse with exactly `exc_type` (not a subclass) and this message."""
    with pytest.raises(exc_type, match=re.escape(message)) as info:
        factory()
    assert type(info.value) is exc_type


# -- domain/instruments.py: ComboLeg and Combo ----------------------------------------------


@pytest.mark.parametrize(
    "factory, message",
    [
        pytest.param(
            lambda: _leg(contract="AAPL"),
            "Combo leg contract must be OptionContract or Equity, got <class 'str'>",
            id="combo-leg-contract-not-instrument",
        ),
        pytest.param(lambda: _leg(ratio=0), "Combo leg ratio must be positive, got 0", id="combo-leg-ratio-zero"),
        pytest.param(lambda: _leg(ratio=-2), "Combo leg ratio must be positive, got -2", id="combo-leg-ratio-negative"),
        pytest.param(lambda: _leg(side="BUY"), "Invalid side BUY", id="combo-leg-side-plain-string"),
    ],
)
def test_combo_leg_refuses_a_bad_field(factory, message) -> None:
    _refusal(factory, ValueError, message)


@pytest.mark.parametrize(
    "factory",
    [
        pytest.param(lambda: Combo([]), id="combo-no-legs-list"),
        pytest.param(lambda: Combo(()), id="combo-no-legs-tuple"),
    ],
)
def test_combo_refuses_no_legs(factory) -> None:
    _refusal(factory, ValueError, "Combo must have at least one leg")


# -- domain/orders.py: Order ----------------------------------------------------------------


@pytest.mark.parametrize(
    "factory, message",
    [
        pytest.param(
            lambda: _order(order_type=OrderType.STOP_LIMIT, limit_price=Decimal("101"), stop_price=None),
            "STOP_LIMIT order must have both limit_price and stop_price",
            id="stop-limit-missing-stop-price",
        ),
        pytest.param(
            lambda: _order(order_type=OrderType.STOP_LIMIT, limit_price=None, stop_price=Decimal("99")),
            "STOP_LIMIT order must have both limit_price and stop_price",
            id="stop-limit-missing-limit-price",
        ),
        pytest.param(
            lambda: _order(
                order_type=OrderType.STOP_LIMIT,
                limit_price=Decimal("101"),
                stop_price=Decimal("99"),
                trail_amount=Decimal("1"),
            ),
            "STOP_LIMIT order cannot have a trail_amount",
            id="stop-limit-with-trail-amount",
        ),
        pytest.param(
            lambda: _order(order_type=OrderType.STOP, stop_price=Decimal("0")),
            "stop_price must be positive, got 0",
            id="stop-price-zero",
        ),
        pytest.param(
            lambda: _order(order_type=OrderType.STOP, stop_price=Decimal("-5")),
            "stop_price must be positive, got -5",
            id="stop-price-negative",
        ),
        pytest.param(lambda: _order(order_id=""), "order_id must be non-empty", id="order-empty-order-id"),
        pytest.param(lambda: _order(account_id=""), "account_id must be non-empty", id="order-empty-account-id"),
    ],
)
def test_order_refuses_a_bad_field(factory, message) -> None:
    _refusal(factory, ValueError, message)


# -- domain/portfolio.py: Fill --------------------------------------------------------------


@pytest.mark.parametrize(
    "factory, message",
    [
        pytest.param(lambda: _fill(fill_id=""), "fill_id must be non-empty", id="fill-empty-fill-id"),
        pytest.param(lambda: _fill(order_id=""), "order_id must be non-empty", id="fill-empty-order-id"),
        pytest.param(lambda: _fill(account_id=""), "account_id must be non-empty", id="fill-empty-account-id"),
        pytest.param(
            lambda: _fill(quantity=Decimal("0")),
            "Fill quantity must be strictly positive, got 0",
            id="fill-quantity-zero",
        ),
        pytest.param(
            lambda: _fill(quantity=Decimal("-3")),
            "Fill quantity must be strictly positive, got -3",
            id="fill-quantity-negative",
        ),
        pytest.param(lambda: _fill(side="BUY"), "Invalid side 'BUY'", id="fill-side-plain-string"),
    ],
)
def test_fill_refuses_a_bad_field(factory, message) -> None:
    _refusal(factory, ValueError, message)


# -- domain/risk.py: RiskRuleResult, RiskVerdict, RiskControlChange -------------------------


def test_rule_result_refuses_an_empty_rule_name() -> None:
    _refusal(
        lambda: RiskRuleResult("", True, Decimal("5"), Decimal("10"), "ok"),
        ValueError,
        "rule_name must be non-empty",
    )


@pytest.mark.parametrize(
    "factory, message",
    [
        pytest.param(
            lambda: _verdict(order_intent_id=""),
            "order_intent_id must be non-empty",
            id="verdict-empty-order-intent-id",
        ),
        pytest.param(
            lambda: _verdict(approved_quantity=Decimal("0")),
            "approved_quantity must be a positive finite Decimal",
            id="verdict-approved-quantity-zero",
        ),
        pytest.param(
            lambda: _verdict(approved_quantity=Decimal("-1")),
            "approved_quantity must be a positive finite Decimal",
            id="verdict-approved-quantity-negative",
        ),
        pytest.param(
            lambda: _verdict(approved_quantity=Decimal("NaN")),
            "approved_quantity must be a positive finite Decimal",
            id="verdict-approved-quantity-nan",
        ),
        pytest.param(
            lambda: _verdict(approved_quantity=Decimal("Infinity")),
            "approved_quantity must be a positive finite Decimal",
            id="verdict-approved-quantity-infinity",
        ),
        pytest.param(
            lambda: _verdict(approved_quantity=1.0),
            "approved_quantity must be a positive finite Decimal",
            id="verdict-approved-quantity-float",
        ),
        pytest.param(
            lambda: _verdict(evaluations=(FAILING,), refusal_reasons=("too big",), approved_quantity=Decimal("1")),
            "A refused RiskVerdict cannot include an approved quantity",
            id="verdict-refused-with-approved-quantity",
        ),
    ],
)
def test_verdict_refuses_a_bad_field(factory, message) -> None:
    _refusal(factory, ValueError, message)


@pytest.mark.parametrize(
    "factory, message",
    [
        pytest.param(lambda: _control(control_id=""), "control_id must be non-empty", id="control-empty-id"),
        pytest.param(lambda: _control(enabled="yes"), "enabled must be a boolean", id="control-enabled-not-bool"),
        pytest.param(lambda: _control(reason=""), "reason must be non-empty", id="control-empty-reason"),
    ],
)
def test_risk_control_change_refuses_a_bad_field(factory, message) -> None:
    _refusal(factory, ValueError, message)


# -- domain/signals.py: Signal --------------------------------------------------------------


@pytest.mark.parametrize(
    "factory, message",
    [
        pytest.param(lambda: _signal(signal_id=""), "signal_id must be non-empty", id="signal-empty-signal-id"),
        pytest.param(lambda: _signal(scan_id=""), "scan_id must be non-empty", id="signal-empty-scan-id"),
        pytest.param(lambda: _signal(symbol=""), "symbol must be non-empty", id="signal-empty-symbol"),
        pytest.param(
            lambda: _signal(direction="sideways"),
            "direction must be 'long' or 'short', got 'sideways'",
            id="signal-direction-not-long-or-short",
        ),
    ],
)
def test_signal_refuses_a_bad_field(factory, message) -> None:
    _refusal(factory, ValueError, message)


# -- interfaces/broker.py: Route (AccountRuleError, kind bad_shape) -------------------------


@pytest.mark.parametrize(
    "factory, message",
    [
        pytest.param(
            lambda: _route(platform="bogus"),
            'route.platform must be one of ["nt8", "tradingview", "tradovate", "tos", "ibkr"], got "bogus"',
            id="route-platform-not-in-allowed-set",
        ),
        pytest.param(lambda: _route(local_name=""), "route.local_name must be non-empty", id="route-empty-local-name"),
    ],
)
def test_route_refuses_a_bad_field_as_bad_shape(factory, message) -> None:
    with pytest.raises(AccountRuleError, match=re.escape(message)) as info:
        factory()
    assert type(info.value) is AccountRuleError
    assert info.value.kind == "bad_shape"


# -- positive controls: one valid construction per type -------------------------------------


@pytest.mark.parametrize(
    "build, expected_type",
    [
        pytest.param(lambda: _leg(), ComboLeg, id="valid-combo-leg"),
        pytest.param(lambda: Combo([_leg()]), Combo, id="valid-combo"),
        pytest.param(lambda: _order(), Order, id="valid-order-market"),
        pytest.param(
            lambda: _order(order_type=OrderType.LIMIT, limit_price=Decimal("100")),
            Order,
            id="valid-order-limit",
        ),
        pytest.param(lambda: _fill(), Fill, id="valid-fill"),
        pytest.param(lambda: PASSING, RiskRuleResult, id="valid-rule-result"),
        pytest.param(lambda: _verdict(), RiskVerdict, id="valid-verdict-accepted"),
        pytest.param(
            lambda: _verdict(evaluations=(FAILING,), refusal_reasons=("too big",)),
            RiskVerdict,
            id="valid-verdict-refused",
        ),
        pytest.param(lambda: _control(), RiskControlChange, id="valid-risk-control-change"),
        pytest.param(lambda: _signal(), Signal, id="valid-signal"),
        pytest.param(lambda: _route(), Route, id="valid-route"),
    ],
)
def test_valid_domain_objects_construct(build, expected_type) -> None:
    assert isinstance(build(), expected_type)


def test_positive_controls_keep_the_fields_they_were_given() -> None:
    order = _order(order_type=OrderType.LIMIT, limit_price=Decimal("100"))
    assert order.limit_price == Decimal("100") and order.stop_price is None
    verdict = _verdict(evaluations=(FAILING,), refusal_reasons=("too big",))
    assert verdict.accepted is False and verdict.approved_quantity is None
    assert _verdict().accepted is True
    assert _route().as_contract() == {"platform": "tos", "local_name": "ACCT-TEST-194"}

"""Tests for OrderState machine, transitions, and immutability."""

import itertools
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from trade_engine.domain.instruments import Equity, Side
from trade_engine.domain.orders import (
    VALID_ORDER_TRANSITIONS,
    IllegalOrderStateTransitionError,
    Order,
    OrderState,
    OrderType,
    TimeInForce,
    validate_order_transition,
)


def _make_order(
    state: OrderState = OrderState.NEW,
    order_type: OrderType = OrderType.LIMIT,
    limit_price: Decimal | None = Decimal("150.00"),
    stop_price: Decimal | None = None,
    trail_amount: Decimal | None = None,
) -> Order:
    return Order(
        order_id="ord-001",
        account_id="acc-test",
        instrument=Equity("AAPL"),
        order_type=order_type,
        side=Side.BUY,
        quantity=Decimal("100"),
        limit_price=limit_price,
        stop_price=stop_price,
        trail_amount=trail_amount,
        command_id="cmd-001",
        created_at=datetime.now(timezone.utc),
        state=state,
    )


def test_valid_order_state_transitions() -> None:
    """Test standard life cycle transitions are permitted."""
    order = _make_order(OrderState.NEW)

    order_submitted = order.transition_to(OrderState.SUBMITTED)
    assert order_submitted.state == OrderState.SUBMITTED
    assert order.state == OrderState.NEW  # Original unchanged (I2)

    order_accepted = order_submitted.transition_to(OrderState.ACCEPTED)
    assert order_accepted.state == OrderState.ACCEPTED

    order_partial = order_accepted.transition_to(OrderState.PARTIALLY_FILLED)
    assert order_partial.state == OrderState.PARTIALLY_FILLED

    order_filled = order_partial.transition_to(OrderState.FILLED)
    assert order_filled.state == OrderState.FILLED


def test_submitted_direct_fill_transitions() -> None:
    """Test SUBMITTED can transition directly to FILLED or PARTIALLY_FILLED for fast venue executions."""
    order = _make_order(OrderState.SUBMITTED)
    filled = order.transition_to(OrderState.FILLED)
    assert filled.state == OrderState.FILLED

    order2 = _make_order(OrderState.SUBMITTED)
    partial = order2.transition_to(OrderState.PARTIALLY_FILLED)
    assert partial.state == OrderState.PARTIALLY_FILLED


def test_pending_unknown_recovery() -> None:
    """Test transitions into and out of PENDING_UNKNOWN (I10)."""
    order = _make_order(OrderState.SUBMITTED)
    unknown = order.transition_to(OrderState.PENDING_UNKNOWN)
    assert unknown.state == OrderState.PENDING_UNKNOWN

    # Broker reconciles that order was accepted
    recovered = unknown.transition_to(OrderState.ACCEPTED)
    assert recovered.state == OrderState.ACCEPTED

    # Or directly filled
    filled = unknown.transition_to(OrderState.FILLED)
    assert filled.state == OrderState.FILLED


def test_exhaustive_order_state_transition_matrix() -> None:
    """Exhaustively verify every (from_state, to_state) pair in OrderState x OrderState."""
    expected_allowed = {
        OrderState.NEW: {
            OrderState.SUBMITTED,
            OrderState.CANCELLED,
            OrderState.REJECTED,
        },
        OrderState.SUBMITTED: {
            OrderState.ACCEPTED,
            OrderState.PARTIALLY_FILLED,
            OrderState.FILLED,
            OrderState.REJECTED,
            OrderState.CANCELLED,
            OrderState.PENDING_UNKNOWN,
        },
        OrderState.ACCEPTED: {
            OrderState.PARTIALLY_FILLED,
            OrderState.FILLED,
            OrderState.CANCELLED,
            OrderState.EXPIRED,
            OrderState.REJECTED,
            OrderState.PENDING_UNKNOWN,
        },
        OrderState.PARTIALLY_FILLED: {
            OrderState.PARTIALLY_FILLED,
            OrderState.FILLED,
            OrderState.CANCELLED,
            OrderState.EXPIRED,
            OrderState.PENDING_UNKNOWN,
        },
        OrderState.PENDING_UNKNOWN: {
            OrderState.SUBMITTED,
            OrderState.ACCEPTED,
            OrderState.PARTIALLY_FILLED,
            OrderState.FILLED,
            OrderState.CANCELLED,
            OrderState.EXPIRED,
            OrderState.REJECTED,
        },
        OrderState.FILLED: set(),
        OrderState.CANCELLED: set(),
        OrderState.EXPIRED: set(),
        OrderState.REJECTED: set(),
    }

    # Verify exact match with declared transition table
    for state in OrderState:
        assert VALID_ORDER_TRANSITIONS[state] == frozenset(expected_allowed[state]), f"Mismatch for {state}"

    # Test all 9 x 9 = 81 pairs
    for from_state, to_state in itertools.product(OrderState, OrderState):
        is_legal = (from_state == to_state) or (to_state in expected_allowed[from_state])
        if is_legal:
            validate_order_transition(from_state, to_state)
        else:
            with pytest.raises(IllegalOrderStateTransitionError):
                validate_order_transition(from_state, to_state)


def test_order_requires_tz_aware_utc_datetime() -> None:
    """Assert naive datetime in Order.created_at is refused (I7)."""
    with pytest.raises(ValueError, match="must be timezone-aware UTC datetime"):
        Order(
            order_id="ord-naive",
            account_id="acc-test",
            instrument=Equity("AAPL"),
            order_type=OrderType.LIMIT,
            side=Side.BUY,
            quantity=Decimal("10"),
            limit_price=Decimal("150.00"),
            command_id="cmd-naive",
            created_at=datetime(2026, 9, 23, 12, 0, 0),  # Naive
        )


def test_order_requires_positive_quantity() -> None:
    """Assert order creation with zero or negative quantity is refused (I5)."""
    with pytest.raises(ValueError, match="quantity must be positive"):
        Order(
            order_id="ord-bad",
            account_id="acc-test",
            instrument=Equity("AAPL"),
            order_type=OrderType.MARKET,
            side=Side.BUY,
            quantity=Decimal("0"),
            command_id="cmd-bad",
            created_at=datetime.now(timezone.utc),
        )


def test_order_requires_command_id() -> None:
    """Assert order creation without command_id is refused (I3)."""
    with pytest.raises(ValueError, match="command_id must be non-empty"):
        Order(
            order_id="ord-bad",
            account_id="acc-test",
            instrument=Equity("AAPL"),
            order_type=OrderType.MARKET,
            side=Side.BUY,
            quantity=Decimal("10"),
            command_id="",
            created_at=datetime.now(timezone.utc),
        )


def test_market_order_validation() -> None:
    """Assert MARKET order rejects limit_price, stop_price, or trail_amount."""
    with pytest.raises(ValueError, match="MARKET order cannot have a limit_price"):
        _make_order(order_type=OrderType.MARKET, limit_price=Decimal("150.00"))

    with pytest.raises(ValueError, match="MARKET order cannot have a stop_price"):
        _make_order(order_type=OrderType.MARKET, limit_price=None, stop_price=Decimal("140.00"))

    with pytest.raises(ValueError, match="MARKET order cannot have a trail_amount"):
        _make_order(order_type=OrderType.MARKET, limit_price=None, trail_amount=Decimal("5.00"))

    # Valid market order
    mkt = _make_order(order_type=OrderType.MARKET, limit_price=None)
    assert mkt.order_type == OrderType.MARKET
    assert mkt.limit_price is None


def test_limit_order_validation() -> None:
    """Assert LIMIT order requires limit_price and rejects stop_price or trail_amount."""
    with pytest.raises(ValueError, match="LIMIT order must have a limit_price"):
        _make_order(order_type=OrderType.LIMIT, limit_price=None)

    with pytest.raises(ValueError, match="LIMIT order cannot have a stop_price"):
        _make_order(order_type=OrderType.LIMIT, limit_price=Decimal("150.00"), stop_price=Decimal("140.00"))

    with pytest.raises(ValueError, match="LIMIT order cannot have a trail_amount"):
        _make_order(order_type=OrderType.LIMIT, limit_price=Decimal("150.00"), trail_amount=Decimal("5.00"))


def test_stop_order_validation() -> None:
    """Assert STOP order requires stop_price and rejects limit_price or trail_amount."""
    with pytest.raises(ValueError, match="STOP order must have a stop_price"):
        _make_order(order_type=OrderType.STOP, limit_price=None, stop_price=None)

    with pytest.raises(ValueError, match="STOP order cannot have a limit_price"):
        _make_order(order_type=OrderType.STOP, limit_price=Decimal("150.00"), stop_price=Decimal("140.00"))

    # Valid stop order
    stop_ord = _make_order(order_type=OrderType.STOP, limit_price=None, stop_price=Decimal("140.00"))
    assert stop_ord.stop_price == Decimal("140.00")


def test_trail_order_validation() -> None:
    """Assert TRAIL order requires positive trail_amount (I5) and rejects limit/stop prices."""
    with pytest.raises(ValueError, match="TRAIL order must have a positive trail_amount"):
        _make_order(order_type=OrderType.TRAIL, limit_price=None, trail_amount=None)

    with pytest.raises(ValueError, match="TRAIL order must have a positive trail_amount"):
        _make_order(order_type=OrderType.TRAIL, limit_price=None, trail_amount=Decimal("0"))

    with pytest.raises(ValueError, match="TRAIL order cannot have a limit_price"):
        _make_order(order_type=OrderType.TRAIL, limit_price=Decimal("150.00"), trail_amount=Decimal("2.50"))

    with pytest.raises(ValueError, match="TRAIL order cannot have a stop_price"):
        _make_order(order_type=OrderType.TRAIL, limit_price=None, stop_price=Decimal("140.00"), trail_amount=Decimal("2.50"))

    # Valid TRAIL order
    trail_ord = _make_order(order_type=OrderType.TRAIL, limit_price=None, trail_amount=Decimal("2.50"))
    assert trail_ord.trail_amount == Decimal("2.50")

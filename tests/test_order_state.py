"""Tests for OrderState machine, transitions, and immutability."""

from datetime import datetime, timezone
from decimal import Decimal

import pytest
from trade_engine.domain.instruments import Equity, Side
from trade_engine.domain.orders import (
    IllegalOrderStateTransitionError,
    Order,
    OrderState,
    OrderType,
    TimeInForce,
    validate_order_transition,
)


def _make_order(state: OrderState = OrderState.NEW) -> Order:
    return Order(
        order_id="ord-001",
        account_id="acc-test",
        instrument=Equity("AAPL"),
        order_type=OrderType.LIMIT,
        side=Side.BUY,
        quantity=Decimal("100"),
        limit_price=Decimal("150.00"),
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


@pytest.mark.parametrize(
    "from_state,to_state",
    [
        (OrderState.FILLED, OrderState.NEW),
        (OrderState.FILLED, OrderState.CANCELLED),
        (OrderState.CANCELLED, OrderState.SUBMITTED),
        (OrderState.CANCELLED, OrderState.FILLED),
        (OrderState.REJECTED, OrderState.ACCEPTED),
        (OrderState.EXPIRED, OrderState.FILLED),
        (OrderState.NEW, OrderState.FILLED),  # Cannot jump straight from NEW to FILLED
        (OrderState.NEW, OrderState.PARTIALLY_FILLED),
    ],
)
def test_illegal_order_state_transitions_raise(
    from_state: OrderState, to_state: OrderState
) -> None:
    """Assert illegal order transitions raise IllegalOrderStateTransitionError."""
    with pytest.raises(IllegalOrderStateTransitionError, match="Illegal order state transition"):
        validate_order_transition(from_state, to_state)


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

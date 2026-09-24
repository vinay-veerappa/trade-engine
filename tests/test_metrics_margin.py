"""E8a margin tests: hand-computed Reg-T fixtures, overrides, refuse-don't-guess."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from trade_engine.domain.instruments import Equity, Side
from trade_engine.domain.orders import Order, OrderState, OrderType
from trade_engine.domain.portfolio import Fill
from trade_engine.ledger import Event, EventKind, Mark, fold_account
from trade_engine.metrics import (
    INITIAL_FRACTION,
    MAINTENANCE_FRACTION,
    MarginOverride,
    account_margin,
    margin_requirement,
)

TS = datetime(2026, 9, 23, 21, 0, 0, tzinfo=timezone.utc)
AAPL = Equity("AAPL")
MSFT = Equity("MSFT")
NVDA = Equity("NVDA")


def an_order(
    order_id: str,
    instrument: object = AAPL,
    side: Side = Side.BUY,
    quantity: str = "100",
) -> Order:
    return Order(
        order_id=order_id,
        account_id="ACC",
        instrument=instrument,
        order_type=OrderType.MARKET,
        side=side,
        quantity=Decimal(quantity),
        command_id=f"cmd-{order_id}",
        created_at=TS,
    )


def fill_and_mark_events() -> list[Event]:
    """Long 100 AAPL @100 (cash −10000), short 200 MSFT @50 (cash +10000)."""
    return [
        Event(account="ACC", kind=EventKind.ORDER_SUBMITTED, payload=an_order("o1"), ts_utc=TS, seq=1),
        Event(
            account="ACC",
            kind=EventKind.FILL,
            payload=Fill(
                fill_id="f1", order_id="o1", account_id="ACC", instrument=AAPL,
                quantity=Decimal("100"), price=Decimal("100"), venue_env="sim",
                filled_at=TS, side=Side.BUY,
            ),
            ts_utc=TS,
            seq=2,
        ),
        Event(account="ACC", kind=EventKind.ORDER_SUBMITTED, payload=an_order("o2", MSFT, Side.SELL, "200"), ts_utc=TS, seq=3),
        Event(
            account="ACC",
            kind=EventKind.FILL,
            payload=Fill(
                fill_id="f2", order_id="o2", account_id="ACC", instrument=MSFT,
                quantity=Decimal("200"), price=Decimal("50"), venue_env="sim",
                filled_at=TS, side=Side.SELL,
            ),
            ts_utc=TS,
            seq=4,
        ),
        Event(
            account="ACC",
            kind=EventKind.MARK,
            payload=Mark(instrument=AAPL, price=Decimal("110"), as_of=TS),
            ts_utc=TS,
            seq=5,
        ),
        Event(
            account="ACC",
            kind=EventKind.MARK,
            payload=Mark(instrument=MSFT, price=Decimal("48"), as_of=TS),
            ts_utc=TS,
            seq=6,
        ),
    ]


def test_default_fractions_are_reg_t() -> None:
    assert INITIAL_FRACTION == Decimal("0.50")
    assert MAINTENANCE_FRACTION == Decimal("0.25")


def test_margin_requirement_long_hand_computed() -> None:
    """Long 100 @48 → MV 4800; initial 2400, maintenance 1200."""
    req = margin_requirement(AAPL, Decimal("100"), Decimal("48"))
    assert req.market_value == Decimal("4800")
    assert req.initial == Decimal("2400")
    assert req.maintenance == Decimal("1200")


def test_margin_requirement_short_uses_magnitude() -> None:
    """Short 200 @50 → MV −10000; requirements on the |MV|, initial 5000, maint 2500."""
    req = margin_requirement(MSFT, Decimal("-200"), Decimal("50"))
    assert req.market_value == Decimal("-10000")
    assert req.initial == Decimal("5000")
    assert req.maintenance == Decimal("2500")


def test_margin_override_replaces_fractions() -> None:
    """A hard-to-borrow name at 75/50: initial 7500, maintenance 5000 on MV 10000."""
    req = margin_requirement(NVDA, Decimal("100"), Decimal("100"), MarginOverride(
        initial=Decimal("0.75"), maintenance=Decimal("0.50")
    ))
    assert req.initial == Decimal("7500")
    assert req.maintenance == Decimal("5000")


def test_margin_override_validation() -> None:
    with pytest.raises(ValueError, match="0 < maintenance"):
        MarginOverride(initial=Decimal("0.25"), maintenance=Decimal("0.50"))


def test_account_margin_hand_computed() -> None:
    """Cash −10000+10000=0; equity 0+11000−9600 = 1400; gross 20600; net 1400.
    Initial 0.5×20600=10300; maintenance 0.25×20600=5150."""
    state = fold_account(fill_and_mark_events(), "ACC")
    margin = account_margin(state)
    assert margin.cash == Decimal("0")
    assert margin.market_value_long == Decimal("11000")
    assert margin.market_value_short == Decimal("-9600")
    assert margin.gross_exposure == Decimal("20600")
    assert margin.net_exposure == Decimal("1400")
    assert margin.equity == Decimal("1400")
    assert margin.margin_initial == Decimal("10300")
    assert margin.margin_used == Decimal("5150")
    assert margin.margin_available == Decimal("-3750")


def test_missing_mark_for_open_position_refuses() -> None:
    """An open position without a session-close mark refuses; a guessed mark would
    silently understate the requirement (I5)."""
    state = fold_account(fill_and_mark_events()[:4], "ACC")
    with pytest.raises(ValueError, match="No session-close mark"):
        account_margin(state)


def test_flat_account_has_no_margin() -> None:
    state = fold_account([], "ACC")
    margin = account_margin(state)
    assert margin.equity == Decimal("0")
    assert margin.margin_used == Decimal("0")
    assert margin.margin_available == Decimal("0")
    assert margin.positions == ()


def test_flat_positions_are_skipped() -> None:
    state = fold_account(fill_and_mark_events(), "ACC")
    zeroed = state.positions.get(AAPL)
    assert zeroed is not None
    margin = account_margin(state)
    assert all(req.quantity != Decimal("0") for req in margin.positions)
    assert {req.symbol for req in margin.positions} == {"AAPL", "MSFT"}


def test_order_state_never_touched() -> None:
    state = fold_account(fill_and_mark_events(), "ACC")
    assert state.orders["o1"].state is OrderState.FILLED
    account_margin(state)
    assert state.orders["o1"].state is OrderState.FILLED


"""Tests for Portfolio, Signal, and Risk domain models."""

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from trade_engine.domain.instruments import Equity, Side
from trade_engine.domain.portfolio import AccountConfig, Fill, Lot, Position
from trade_engine.domain.risk import RiskRuleResult, RiskVerdict
from trade_engine.domain.signals import OrderIntent, Signal


def test_fill_creation_and_validation() -> None:
    now = datetime.now(timezone.utc)
    fill = Fill(
        fill_id="fill-1",
        order_id="ord-1",
        account_id="acc-1",
        instrument=Equity("AAPL"),
        quantity=Decimal("50"),
        price=Decimal("150.25"),
        venue_env="sim",
        filled_at=now,
    )
    assert fill.quantity == Decimal("50")
    assert fill.price == Decimal("150.25")
    assert fill.venue_env == "sim"

    with pytest.raises(ValueError, match="Fill price must be strictly positive"):
        Fill(
            fill_id="fill-bad",
            order_id="ord-1",
            account_id="acc-1",
            instrument=Equity("AAPL"),
            quantity=Decimal("50"),
            price=Decimal("0"),
            venue_env="sim",
            filled_at=now,
        )


def test_position_long_short_flat() -> None:
    eq = Equity("MSFT")
    pos_long = Position(account_id="acc-1", instrument=eq, quantity=Decimal("100"), avg_cost=Decimal("400"))
    assert pos_long.is_long
    assert not pos_long.is_short
    assert not pos_long.is_flat

    pos_short = Position(account_id="acc-1", instrument=eq, quantity=Decimal("-50"), avg_cost=Decimal("400"))
    assert pos_short.is_short
    assert not pos_short.is_long
    assert not pos_short.is_flat

    pos_flat = Position(account_id="acc-1", instrument=eq, quantity=Decimal("0"), avg_cost=Decimal("0"))
    assert pos_flat.is_flat


def test_signal_and_order_intent() -> None:
    sig = Signal(
        signal_id="sig-01",
        scan_id="scan-breakout",
        symbol="GOOG",
        session_date=date(2026, 9, 23),
        direction="long",
        metrics={"close": Decimal("165.50"), "atr14": Decimal("3.20")},
        next_earnings_date=None,  # None when unknown, never guessed (I5)
    )
    assert sig.direction == "long"
    assert sig.next_earnings_date is None

    intent = OrderIntent(
        intent_id="intent-01",
        account_id="SCAN_BREAKOUT",
        instrument=Equity("GOOG"),
        side=Side.BUY,
        quantity_rule="risk_0.75pct",
        entry_price=Decimal("166.00"),
        stop_loss=Decimal("162.80"),
        profit_targets=(Decimal("172.40"),),
        reason="Breakout above 20-day high with expanding volume",
        command_id="cmd-intent-01",
    )
    assert intent.side == Side.BUY
    assert intent.entry_price == Decimal("166.00")


def test_risk_verdict_records_all_evaluations() -> None:
    """Test RiskVerdict contains every rule evaluation without short-circuiting (I11)."""
    eval1 = RiskRuleResult(
        rule_name="max_position_size",
        passed=True,
        measured_value=Decimal("8000"),
        threshold=Decimal("10000"),
        reason="Position within 20% equity cap",
    )
    eval2 = RiskRuleResult(
        rule_name="regime_filter",
        passed=False,
        measured_value="UNKNOWN",
        threshold="NOT_UNKNOWN",
        reason="Regime UNKNOWN blocks new entries",
    )

    verdict = RiskVerdict(
        order_intent_id="intent-01",
        accepted=False,
        evaluations=(eval1, eval2),
        refusal_reasons=("Regime UNKNOWN blocks new entries",),
    )

    assert not verdict.accepted
    assert len(verdict.evaluations) == 2
    assert "Regime UNKNOWN blocks new entries" in verdict.refusal_reasons

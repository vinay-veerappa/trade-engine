"""Tests for Portfolio, Signal, and Risk domain models."""

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from trade_engine.domain.instruments import Equity, Side
from trade_engine.domain.orders import TimeInForce
from trade_engine.domain.portfolio import AccountConfig, Fill, Lot, Position
from trade_engine.domain.risk import RiskRuleResult, RiskVerdict
from trade_engine.domain.signals import OrderIntent, Signal
from trade_engine.interfaces.market_data import Bar, CorporateAction, OptionQuote, Quote


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
        side=Side.BUY,
    )
    assert fill.quantity == Decimal("50")
    assert fill.price == Decimal("150.25")
    assert fill.venue_env == "sim"
    assert fill.side == Side.BUY

    # Refuses price <= 0 (fire test)
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
            side=Side.SELL,
        )

    # Refuses invalid venue_env (fire test)
    with pytest.raises(ValueError, match="Invalid venue_env 'sandbox'"):
        Fill(
            fill_id="fill-bad-env",
            order_id="ord-1",
            account_id="acc-1",
            instrument=Equity("AAPL"),
            quantity=Decimal("50"),
            price=Decimal("150.00"),
            venue_env="sandbox",  # type: ignore[arg-type]
            filled_at=now,
            side=Side.SELL,
        )

    # Refuses naive datetime (fire test)
    naive_dt = datetime(2026, 9, 23, 12, 0, 0)
    with pytest.raises(ValueError, match="must be timezone-aware UTC datetime"):
        Fill(
            fill_id="fill-bad-tz",
            order_id="ord-1",
            account_id="acc-1",
            instrument=Equity("AAPL"),
            quantity=Decimal("50"),
            price=Decimal("150.00"),
            venue_env="sim",
            filled_at=naive_dt,
            side=Side.SELL,
        )


def test_lot_validation() -> None:
    now = datetime.now(timezone.utc)
    lot = Lot(
        lot_id="lot-1",
        quantity=Decimal("100"),
        cost_basis=Decimal("150.00"),
        acquired_at=now,
        side=Side.BUY,
    )
    assert lot.lot_id == "lot-1"
    assert lot.side == Side.BUY

    # Short lot support
    short_lot = Lot(
        lot_id="lot-short-1",
        quantity=Decimal("100"),
        cost_basis=Decimal("150.00"),
        acquired_at=now,
        side=Side.SELL,
    )
    assert short_lot.side == Side.SELL

    # Refuses naive datetime (fire test)
    with pytest.raises(ValueError, match="must be timezone-aware UTC datetime"):
        Lot(
            lot_id="lot-bad-tz",
            quantity=Decimal("100"),
            cost_basis=Decimal("150.00"),
            acquired_at=datetime(2026, 9, 23, 12, 0, 0),
            side=Side.BUY,
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
    now = datetime.now(timezone.utc)
    sig = Signal(
        signal_id="sig-01",
        scan_id="scan-breakout",
        symbol="GOOG",
        session_date=date(2026, 9, 23),
        direction="long",
        metrics={"close": Decimal("165.50"), "atr14": Decimal("3.20")},
        next_earnings_date=None,  # None when unknown, never guessed (I5)
        created_at=now,
    )
    assert sig.direction == "long"
    assert sig.next_earnings_date is None
    assert sig.metrics["close"] == Decimal("165.50")

    # Immutability: direct item assignment to metrics must raise TypeError
    with pytest.raises(TypeError):
        sig.metrics["close"] = Decimal("200.00")  # type: ignore[index]

    # Refuses naive created_at datetime
    with pytest.raises(ValueError, match="must be timezone-aware UTC datetime"):
        Signal(
            signal_id="sig-bad-tz",
            scan_id="scan-breakout",
            symbol="GOOG",
            session_date=date(2026, 9, 23),
            direction="long",
            created_at=datetime(2026, 9, 23, 12, 0, 0),
        )

    # Hashability: Signal must be hashable and usable in sets/dicts
    sig2 = Signal(
        signal_id="sig-01",
        scan_id="scan-breakout",
        symbol="GOOG",
        session_date=date(2026, 9, 23),
        direction="long",
        metrics={"close": Decimal("165.50"), "atr14": Decimal("3.20")},
        next_earnings_date=None,
        created_at=now,
    )
    assert hash(sig) == hash(sig2)
    assert sig == sig2
    signal_set = {sig, sig2}
    assert len(signal_set) == 1

    # Valid BUY order intent
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

    # Invalid BUY: stop_loss >= entry_price (fire test)
    with pytest.raises(ValueError, match="For BUY intent, stop_loss .* must be strictly below entry_price"):
        OrderIntent(
            intent_id="intent-bad-stop",
            account_id="SCAN_BREAKOUT",
            instrument=Equity("GOOG"),
            side=Side.BUY,
            quantity_rule="risk_0.75pct",
            entry_price=Decimal("100.00"),
            stop_loss=Decimal("110.00"),
            profit_targets=(Decimal("120.00"),),
            reason="Bad stop",
            command_id="cmd-bad-stop",
        )

    # Invalid BUY: profit_target <= entry_price (fire test)
    with pytest.raises(ValueError, match="For BUY intent, profit target .* must be strictly above entry_price"):
        OrderIntent(
            intent_id="intent-bad-target",
            account_id="SCAN_BREAKOUT",
            instrument=Equity("GOOG"),
            side=Side.BUY,
            quantity_rule="risk_0.75pct",
            entry_price=Decimal("100.00"),
            stop_loss=Decimal("95.00"),
            profit_targets=(Decimal("98.00"),),
            reason="Bad target",
            command_id="cmd-bad-target",
        )

    # Valid SELL order intent
    sell_intent = OrderIntent(
        intent_id="intent-sell-01",
        account_id="SCAN_SHORT",
        instrument=Equity("GOOG"),
        side=Side.SELL,
        quantity_rule="risk_0.5pct",
        entry_price=Decimal("100.00"),
        stop_loss=Decimal("105.00"),
        profit_targets=(Decimal("90.00"),),
        reason="Parabolic short breakdown",
        command_id="cmd-sell-01",
    )
    assert sell_intent.side == Side.SELL

    # Invalid SELL: stop_loss <= entry_price (fire test)
    with pytest.raises(ValueError, match="For SELL intent, stop_loss .* must be strictly above entry_price"):
        OrderIntent(
            intent_id="intent-bad-sell-stop",
            account_id="SCAN_SHORT",
            instrument=Equity("GOOG"),
            side=Side.SELL,
            quantity_rule="risk_0.5pct",
            entry_price=Decimal("100.00"),
            stop_loss=Decimal("95.00"),
            profit_targets=(Decimal("90.00"),),
            reason="Bad sell stop",
            command_id="cmd-bad-sell-stop",
        )

    # Invalid SELL: profit_target >= entry_price (fire test)
    with pytest.raises(ValueError, match="For SELL intent, profit target .* must be strictly below entry_price"):
        OrderIntent(
            intent_id="intent-bad-sell-target",
            account_id="SCAN_SHORT",
            instrument=Equity("GOOG"),
            side=Side.SELL,
            quantity_rule="risk_0.5pct",
            entry_price=Decimal("100.00"),
            stop_loss=Decimal("105.00"),
            profit_targets=(Decimal("102.00"),),
            reason="Bad sell target",
            command_id="cmd-bad-sell-target",
        )


def test_risk_verdict_records_all_evaluations_and_prevents_contradictions() -> None:
    """Test RiskVerdict contains every rule evaluation without short-circuiting and prevents contradiction (I11)."""
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

    # Valid refused verdict with automatically derived accepted=False
    verdict = RiskVerdict(
        order_intent_id="intent-01",
        evaluations=(eval1, eval2),
        refusal_reasons=("Regime UNKNOWN blocks new entries",),
    )
    assert not verdict.accepted
    assert len(verdict.evaluations) == 2
    assert "Regime UNKNOWN blocks new entries" in verdict.refusal_reasons

    # Contradiction: passing accepted=True when rule failed raises ValueError (fire test)
    with pytest.raises(ValueError, match="Contradictory RiskVerdict"):
        RiskVerdict(
            order_intent_id="intent-01",
            accepted=True,
            evaluations=(eval1, eval2),
            refusal_reasons=("Regime UNKNOWN blocks new entries",),
        )

    # Refused verdict with empty refusal reasons raises ValueError (fire test)
    with pytest.raises(ValueError, match="A refused RiskVerdict must include at least one refusal reason"):
        RiskVerdict(
            order_intent_id="intent-01",
            evaluations=(eval1, eval2),
            refusal_reasons=(),
        )

    # Empty evaluations raises ValueError (fire test)
    with pytest.raises(ValueError, match="RiskVerdict must contain at least one evaluation"):
        RiskVerdict(
            order_intent_id="intent-01",
            evaluations=(),
        )

    # Valid accepted verdict
    verdict_accepted = RiskVerdict(
        order_intent_id="intent-02",
        evaluations=(eval1,),
    )
    assert verdict_accepted.accepted
    assert len(verdict_accepted.refusal_reasons) == 0


def test_market_data_structures_validation() -> None:
    now = datetime.now(timezone.utc)
    eq = Equity("AAPL")

    # Bar validation
    bar = Bar(
        instrument=eq,
        timestamp=now,
        open=Decimal("150.00"),
        high=Decimal("155.00"),
        low=Decimal("149.00"),
        close=Decimal("154.00"),
        volume=Decimal("10000"),
        as_of=now,
    )
    assert bar.open == Decimal("150.00")

    # Bar rejects naive datetime (fire test)
    with pytest.raises(ValueError, match="must be timezone-aware UTC datetime"):
        Bar(
            instrument=eq,
            timestamp=datetime(2026, 9, 23, 12, 0, 0),
            open=Decimal("150.00"),
            high=Decimal("155.00"),
            low=Decimal("149.00"),
            close=Decimal("154.00"),
            volume=Decimal("10000"),
            as_of=now,
        )

    # Quote validation
    quote = Quote(
        instrument=eq,
        bid=Decimal("150.00"),
        ask=Decimal("150.10"),
        bid_size=Decimal("100"),
        ask_size=Decimal("200"),
        as_of=now,
    )
    assert quote.mid == Decimal("150.05")
    assert quote.spread == Decimal("0.10")

    # CorporateAction details immutability
    ca = CorporateAction(
        symbol="AAPL",
        action_type="dividend",
        effective_date=date(2026, 10, 1),
        as_of=now,
        details={"amount": "0.25"},
    )
    with pytest.raises(TypeError):
        ca.details["amount"] = "0.30"  # type: ignore[index]


@pytest.mark.parametrize("tif", [TimeInForce.OPG, TimeInForce.MOC, TimeInForce.GTD])
@pytest.mark.parametrize("field_name", ["entry_tif", "exit_tif"])
def test_order_intent_refuses_time_in_force_a_bracket_cannot_honour(
    field_name: str, tif: TimeInForce
) -> None:
    with pytest.raises(ValueError, match=field_name):
        OrderIntent(
            intent_id="i",
            account_id="a",
            instrument=Equity("AAPL"),
            side=Side.BUY,
            quantity_rule="fixed_1",
            entry_price=Decimal("100"),
            stop_loss=Decimal("95"),
            profit_targets=(),
            reason="r",
            command_id="c",
            **{field_name: tif},
        )


def test_order_intent_defaults_to_day_entry_and_gtc_exits() -> None:
    intent = OrderIntent(
        intent_id="i",
        account_id="a",
        instrument=Equity("AAPL"),
        side=Side.BUY,
        quantity_rule="fixed_1",
        entry_price=Decimal("100"),
        stop_loss=Decimal("95"),
        profit_targets=(),
        reason="r",
        command_id="c",
    )

    assert intent.entry_tif is TimeInForce.DAY
    assert intent.exit_tif is TimeInForce.GTC

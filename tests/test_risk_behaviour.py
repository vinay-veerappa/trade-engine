"""Risk engine behaviour: the verdict's rule order, configuration refusals, the market-hours
clock, and a sized long's approved quantity. Expected values are written out."""

from __future__ import annotations

from datetime import datetime, time, timezone
from decimal import Decimal as D

import pytest

from trade_engine.domain.instruments import Equity, Side
from trade_engine.domain.signals import OrderIntent
from trade_engine.interfaces.clock import Clock
from trade_engine.ledger import Ledger
from trade_engine.risk import (
    AccountRiskRules,
    RiskConfigurationError,
    RiskContext,
    RiskEngine,
    TradingHours,
    VenueRiskRails,
)

NY = ("America/New_York", (0, 1, 2, 3, 4), time(9, 30), time(16))
WED_OPEN = datetime(2026, 9, 23, 14, 0, tzinfo=timezone.utc)  # Wednesday 10:00 ET

RULES = {
    "risk_per_trade_frac": D("0.0075"),
    "short_risk_per_trade_frac": D("0.005"),
    "max_position_notional_frac": D("0.20"),
    "max_gross_exposure_frac": D("1.50"),
    "bull_chop_gross_exposure_frac": D("1.00"),
    "max_portfolio_heat_frac": D("0.06"),
    "max_positions": 10,
    "max_positions_per_industry": 3,
    "min_price": D("5"),
    "max_adv_frac": D("0.01"),
    "earnings_blackout_sessions": 5,
    "drawdown_half_risk_frac": D("0.08"),
    "drawdown_suspend_frac": D("0.15"),
    "drawdown_recovery_frac": D("0.05"),
    "daily_loss_block_frac": D("0.03"),
}
CONTEXT = {
    "equity": D("50000"),
    "current_price": D("100"),
    "gross_exposure": D("0"),
    "portfolio_heat": D("0"),
    "open_positions": 0,
    "industry": "Technology",
    "industry_positions": 0,
    "average_dollar_volume_20d": D("1000000000"),
    "sessions_until_earnings": 6,
    "regime": "BULL_EXPLOSIVE",
    "macro_high_risk_day": False,
    "drawdown_from_peak_frac": D("0.02"),
    "previous_session_pnl_frac": D("0"),
    "venue_orders_today": 0,
    "venue_daily_pnl": D("0"),
    "current_position_quantity": 0,
}
RAILS = {
    "venue_id": "paper-main",
    "environment": "paper",
    "allowed_symbols": frozenset({"AAPL", "MSFT"}),
    "max_position_quantity": 1000,
    "max_orders_per_day": 100,
    "max_daily_loss": D("5000"),
    "duplicate_protection": True,
    "persistent_kill_switch": True,
}
MAPPING = {
    "risk_per_trade": "0.75%",
    "short_risk_per_trade": "0.5%",
    "max_position_notional": "20%",
    "max_gross_exposure": "150%",
    "bull_chop_gross_exposure": "100%",
    "max_portfolio_heat": "6%",
    "max_positions": 10,
    "max_positions_per_industry": 3,
    "min_price": "5",
    "max_adv": "1%",
    "earnings_blackout_sessions": 5,
    "drawdown_half_risk": "8%",
    "drawdown_suspend": "15%",
    "drawdown_recovery": "5%",
    "daily_loss_block": "3%",
}
# The verdict always carries these rules, in this order.
RULE_NAMES = (
    "risk_per_trade",
    "quantity_rule",
    "max_position",
    "gross_exposure",
    "portfolio_heat",
    "max_positions",
    "per_industry",
    "min_price",
    "adv_pct",
    "earnings_blackout",
    "regime",
    "macro_high_risk_day",
    "drawdown_brake",
    "daily_loss",
    "allowlist",
    "venue_max_position",
    "orders_per_day",
    "venue_daily_loss",
    "trading_hours",
    "duplicate_protection",
    "persistent_kill_switch",
)


class FixedClock(Clock):
    def __init__(self, now: datetime) -> None:
        self._now = now

    def now_utc(self) -> datetime:
        return self._now

    def sleep(self, seconds: float) -> None:
        raise AssertionError("the risk engine must not sleep")


def _rules() -> AccountRiskRules:
    return AccountRiskRules(**RULES)


def _rails() -> VenueRiskRails:
    return VenueRiskRails(**RAILS, trading_hours=TradingHours(*NY))


def _intent(**overrides) -> OrderIntent:
    fields = {
        "intent_id": "intent-1",
        "account_id": "account-1",
        "instrument": Equity("AAPL"),
        "side": Side.BUY,
        "quantity_rule": "risk_0.75pct",
        "entry_price": D("100"),
        "stop_loss": D("95"),
        "profit_targets": (D("110"),),
        "reason": "behaviour",
        "command_id": "command-1",
        **overrides,
    }
    return OrderIntent(**fields)


def _evaluate(tmp_path, intent: OrderIntent, context: dict = CONTEXT):
    with Ledger(tmp_path / "risk.db") as ledger:
        engine = RiskEngine(_rules(), _rails(), FixedClock(WED_OPEN), ledger)
        return engine.evaluate(intent, RiskContext(**context))


# --- sizing -----------------------------------------------------------------------------


def test_a_sized_long_is_approved_at_the_risk_budget(tmp_path):
    # 0.75% of 50,000 is 375; a 5-point stop risks 5 a share, so 75 shares.
    verdict = _evaluate(tmp_path, _intent())
    assert verdict.accepted is True
    assert verdict.approved_quantity == 75
    assert verdict.refusal_reasons == ()


# --- verdict shape ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "change",
    [{}, {"macro_high_risk_day": True}, {"sessions_until_earnings": 1}, {"current_price": D("1")}],
)
def test_a_verdict_lists_every_rule_in_order(tmp_path, change):
    verdict = _evaluate(tmp_path, _intent(), {**CONTEXT, **change})
    assert tuple(row.rule_name for row in verdict.evaluations) == RULE_NAMES


# --- configuration: the same refusal, the same words ------------------------------------


def test_a_float_fraction_is_refused_not_guessed():
    with pytest.raises(RiskConfigurationError, match="positive finite Decimal"):
        AccountRiskRules(**{**RULES, "risk_per_trade_frac": 0.01})


def test_trading_hours_boundaries_must_be_clock_times():
    with pytest.raises(RiskConfigurationError, match="datetime.time boundaries"):
        TradingHours("America/New_York", (0, 1, 2, 3, 4), "09:30", time(16))


def test_trading_hours_weekdays_must_be_a_list_of_integers():
    with pytest.raises(RiskConfigurationError, match="integers from 0 to 6"):
        TradingHours("America/New_York", "01234", time(9, 30), time(16))


def test_rule_keys_must_be_strings():
    with pytest.raises(RiskConfigurationError, match="keys must be strings"):
        AccountRiskRules.from_mapping({1: 2})


def test_allowed_symbols_must_be_a_collection():
    with pytest.raises(RiskConfigurationError, match="must be a collection of symbols"):
        VenueRiskRails(**{**RAILS, "trading_hours": TradingHours(*NY), "allowed_symbols": "AAPL"})


@pytest.mark.parametrize("value", ["٣", 10**30])
def test_a_non_ascii_digit_or_a_wide_integer_is_refused(value):
    # Documented deviation (RUST_PORT.md P1d): Rust refuses where Python guessed.
    with pytest.raises(RiskConfigurationError):
        AccountRiskRules.from_mapping({**MAPPING, "max_positions": value})


# --- the market-hours clock -------------------------------------------------------------


def test_trading_hours_are_open_on_a_weekday_session_only():
    hours = TradingHours(*NY)
    assert hours.is_open(WED_OPEN) is True
    assert hours.is_open(datetime(2026, 9, 26, 14, 0, tzinfo=timezone.utc)) is False  # Saturday


def test_is_open_refuses_a_naive_instant():
    with pytest.raises(ValueError, match=r"timezone-aware \(I7\)"):
        TradingHours(*NY).is_open(datetime(2026, 9, 23, 14, 0))


# --- intent refusal ---------------------------------------------------------------------


def test_non_equity_intent_is_refused_before_anything_is_written(tmp_path):
    class NotEquity:
        symbol = "X"

    intent = _intent()
    object.__setattr__(intent, "instrument", NotEquity())
    with Ledger(tmp_path / "risk.db") as ledger:
        engine = RiskEngine(_rules(), _rails(), FixedClock(WED_OPEN), ledger)
        with pytest.raises(ValueError, match="equity intents only"):
            engine.evaluate(intent, RiskContext(**CONTEXT))
        assert ledger.events() == []

"""E6 account-risk rules, venue rails, and persistent safety controls."""

from __future__ import annotations

from datetime import datetime, time, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from trade_engine.domain.instruments import Equity, Side
from trade_engine.domain.risk import RiskRuleResult, RiskVerdict
from trade_engine.domain.signals import OrderIntent
from trade_engine.interfaces.clock import Clock
from trade_engine.ledger import Event, EventKind, Ledger
from trade_engine.risk import (
    AccountRiskRules,
    RiskConfigurationError,
    RiskContext,
    RiskEngine,
    TradingHours,
    VenueRiskRails,
)

NOW = datetime(2026, 9, 23, 14, 0, tzinfo=timezone.utc)


class FixedClock(Clock):
    def __init__(self, now: datetime = NOW) -> None:
        self._now = now

    def now_utc(self) -> datetime:
        return self._now

    def sleep(self, seconds: float) -> None:
        raise AssertionError("Risk evaluation must not sleep")


def rules(**overrides: object) -> AccountRiskRules:
    values: dict[str, object] = {
        "risk_per_trade_frac": Decimal("0.0075"),
        "short_risk_per_trade_frac": Decimal("0.005"),
        "max_position_notional_frac": Decimal("0.20"),
        "max_gross_exposure_frac": Decimal("1.50"),
        "bull_chop_gross_exposure_frac": Decimal("1.00"),
        "max_portfolio_heat_frac": Decimal("0.06"),
        "max_positions": 10,
        "max_positions_per_industry": 3,
        "min_price": Decimal("5"),
        "max_adv_frac": Decimal("0.01"),
        "earnings_blackout_sessions": 5,
        "drawdown_half_risk_frac": Decimal("0.08"),
        "drawdown_suspend_frac": Decimal("0.15"),
        "drawdown_recovery_frac": Decimal("0.05"),
        "daily_loss_block_frac": Decimal("0.03"),
    }
    values.update(overrides)
    return AccountRiskRules(**values)  # type: ignore[arg-type]


def rule_config() -> dict[str, object]:
    return {
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


def rails(**overrides: object) -> VenueRiskRails:
    values: dict[str, object] = {
        "venue_id": "paper-main",
        "environment": "paper",
        "allowed_symbols": frozenset({"AAPL", "MSFT"}),
        "max_position_quantity": 1000,
        "max_orders_per_day": 100,
        "max_daily_loss": Decimal("5000"),
        "trading_hours": TradingHours(
            "America/New_York", (0, 1, 2, 3, 4), time(9, 30), time(16)
        ),
        "duplicate_protection": True,
        "persistent_kill_switch": True,
    }
    values.update(overrides)
    return VenueRiskRails(**values)  # type: ignore[arg-type]


def context(**overrides: object) -> RiskContext:
    values: dict[str, object] = {
        "equity": Decimal("50000"),
        "current_price": Decimal("100"),
        "gross_exposure": Decimal("0"),
        "portfolio_heat": Decimal("0"),
        "open_positions": 0,
        "industry": "Technology",
        "industry_positions": 0,
        "average_dollar_volume_20d": Decimal("1000000000"),
        "sessions_until_earnings": 6,
        "regime": "BULL_EXPLOSIVE",
        "macro_high_risk_day": False,
        "drawdown_from_peak_frac": Decimal("0.02"),
        "previous_session_pnl_frac": Decimal("0"),
        "venue_orders_today": 0,
        "venue_daily_pnl": Decimal("0"),
        "current_position_quantity": 0,
    }
    values.update(overrides)
    return RiskContext(**values)  # type: ignore[arg-type]


def intent(**overrides: object) -> OrderIntent:
    values: dict[str, object] = {
        "intent_id": "intent-1",
        "account_id": "account-1",
        "instrument": Equity("AAPL"),
        "side": Side.BUY,
        "quantity_rule": "risk_0.75pct",
        "entry_price": Decimal("100"),
        "stop_loss": Decimal("95"),
        "profit_targets": (Decimal("110"),),
        "reason": "Test entry",
        "command_id": "command-1",
    }
    values.update(overrides)
    return OrderIntent(**values)  # type: ignore[arg-type]


def evaluate(
    ledger: Ledger,
    *,
    risk_rules: AccountRiskRules | None = None,
    venue_rails: VenueRiskRails | None = None,
    risk_context: RiskContext | None = None,
    order_intent: OrderIntent | None = None,
    clock: Clock | None = None,
):
    return RiskEngine(
        risk_rules or rules(),
        venue_rails or rails(),
        clock or FixedClock(),
        ledger,
    ).evaluate(order_intent or intent(), risk_context or context())


def test_valid_trade_is_sized_and_verdict_lists_every_rule(tmp_path: Path) -> None:
    with Ledger(tmp_path / "ledger.db") as ledger:
        verdict = evaluate(ledger)

    assert verdict.accepted
    assert verdict.approved_quantity == Decimal("75")
    assert len(verdict.evaluations) == 21
    assert all(result.passed for result in verdict.evaluations)
    assert len(verdict.refusal_reasons) == 0


@pytest.mark.parametrize(
    ("rule_name", "context_changes", "rule_changes", "rail_changes", "intent_changes", "extra"),
    [
        ("risk_per_trade", {}, {"risk_per_trade_frac": Decimal("0.000001")}, {}, {}, {}),
        ("max_position", {}, {}, {}, {"stop_loss": Decimal("99.9")}, {}),
        ("gross_exposure", {"gross_exposure": Decimal("75000")}, {}, {}, {}, {}),
        ("portfolio_heat", {"portfolio_heat": Decimal("2900")}, {}, {}, {}, {}),
        ("max_positions", {"open_positions": 10}, {}, {}, {}, {}),
        ("per_industry", {"industry_positions": 3}, {}, {}, {}, {}),
        ("min_price", {"current_price": Decimal("4.99")}, {}, {}, {}, {}),
        ("adv_pct", {"average_dollar_volume_20d": Decimal("100000")}, {}, {}, {}, {}),
        ("earnings_blackout", {"sessions_until_earnings": 5}, {}, {}, {}, {}),
        ("regime", {"regime": "UNKNOWN"}, {}, {}, {}, {}),
        ("macro_high_risk_day", {"macro_high_risk_day": True}, {}, {}, {}, {}),
        ("drawdown_brake", {"drawdown_from_peak_frac": Decimal("0.15")}, {}, {}, {}, {}),
        ("daily_loss", {"previous_session_pnl_frac": Decimal("-0.03")}, {}, {}, {}, {}),
        ("allowlist", {}, {}, {"allowed_symbols": frozenset({"MSFT"})}, {}, {}),
        ("venue_max_position", {}, {}, {"max_position_quantity": 50}, {}, {}),
        ("orders_per_day", {"venue_orders_today": 100}, {}, {}, {}, {}),
        ("venue_daily_loss", {"venue_daily_pnl": Decimal("-5000")}, {}, {}, {}, {}),
        ("trading_hours", {}, {}, {}, {}, {"closed": True}),
        ("duplicate_protection", {}, {}, {}, {}, {"duplicate": True}),
        ("persistent_kill_switch", {}, {}, {}, {}, {"kill": True}),
        ("quantity_rule", {}, {}, {}, {"quantity_rule": "risk_2pct"}, {}),
    ],
)
def test_each_rule_refuses_its_violating_case(
    tmp_path: Path,
    rule_name: str,
    context_changes: dict[str, object],
    rule_changes: dict[str, object],
    rail_changes: dict[str, object],
    intent_changes: dict[str, object],
    extra: dict[str, bool],
) -> None:
    order = intent(**intent_changes)
    venue = rails(**rail_changes)
    clock: Clock = (
        FixedClock(datetime(2026, 9, 23, 22, 0, tzinfo=timezone.utc))
        if extra.get("closed")
        else FixedClock()
    )
    with Ledger(tmp_path / "ledger.db") as ledger:
        if extra.get("duplicate"):
            ledger.append(
                Event(
                    account="account-1",
                    kind=EventKind.RISK_VERDICT,
                    payload=RiskVerdict(
                        order_intent_id="previous",
                        evaluations=(
                            RiskRuleResult("previous", True, Decimal("0"), Decimal("1"), "ok"),
                        ),
                    ),
                    ts_utc=NOW,
                    command_id=order.command_id,
                )
            )
        engine = RiskEngine(rules(**rule_changes), venue, clock, ledger)
        if extra.get("kill"):
            engine.engage_kill_switch("halt-1", "operator halt")
        verdict = engine.evaluate(order, context(**context_changes))

    failed = {result.rule_name for result in verdict.evaluations if not result.passed}
    assert rule_name in failed
    assert not verdict.accepted
    assert verdict.approved_quantity is None


def test_missing_measurements_fail_closed(tmp_path: Path) -> None:
    missing = {
        "equity": None,
        "current_price": None,
        "gross_exposure": None,
        "portfolio_heat": None,
        "open_positions": None,
        "industry": None,
        "industry_positions": None,
        "average_dollar_volume_20d": None,
        "sessions_until_earnings": None,
        "regime": None,
        "macro_high_risk_day": None,
        "drawdown_from_peak_frac": None,
        "previous_session_pnl_frac": None,
        "venue_orders_today": None,
        "venue_daily_pnl": None,
        "current_position_quantity": None,
    }
    with Ledger(tmp_path / "ledger.db") as ledger:
        verdict = evaluate(ledger, risk_context=context(**missing))

    failed = {result.rule_name for result in verdict.evaluations if not result.passed}
    assert {
        "risk_per_trade",
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
        "venue_max_position",
        "orders_per_day",
        "venue_daily_loss",
    } <= failed


def test_verdict_contains_every_rule_when_many_fail(tmp_path: Path) -> None:
    failed_context = context(
        current_price=Decimal("4"),
        gross_exposure=Decimal("75000"),
        industry_positions=3,
        sessions_until_earnings=2,
        regime="UNKNOWN",
        macro_high_risk_day=True,
        drawdown_from_peak_frac=Decimal("0.15"),
        previous_session_pnl_frac=Decimal("-0.04"),
        venue_orders_today=100,
        venue_daily_pnl=Decimal("-6000"),
    )
    with Ledger(tmp_path / "ledger.db") as ledger:
        verdict = evaluate(ledger, risk_context=failed_context)

    assert len(verdict.evaluations) == 21
    assert sum(not result.passed for result in verdict.evaluations) >= 8
    assert len(verdict.refusal_reasons) == sum(
        not result.passed for result in verdict.evaluations
    )


def test_paper_venue_refuses_missing_rails() -> None:
    with pytest.raises(RiskConfigurationError, match="missing required rails: allowed_symbols"):
        VenueRiskRails("paper-main", "paper")


def test_rules_load_from_config_mapping_and_validate_units() -> None:
    config = rule_config()

    loaded = AccountRiskRules.from_mapping(config)

    assert loaded == rules()
    assert loaded.risk_per_trade_frac == Decimal("0.0075")
    config["risk_per_trade"] = "0.75%"
    config["unknown_rule"] = "1%"
    with pytest.raises(RiskConfigurationError, match="Unknown account risk rule keys"):
        AccountRiskRules.from_mapping(config)
    incomplete = rule_config()
    incomplete.pop("daily_loss_block")
    with pytest.raises(RiskConfigurationError, match="Missing account risk rule keys"):
        AccountRiskRules.from_mapping(incomplete)


@pytest.mark.parametrize(
    "key",
    [
        "risk_per_trade",
        "short_risk_per_trade",
        "max_position_notional",
        "max_gross_exposure",
        "bull_chop_gross_exposure",
        "max_portfolio_heat",
        "max_adv",
        "drawdown_half_risk",
        "drawdown_suspend",
        "drawdown_recovery",
        "daily_loss_block",
    ],
)
def test_config_percentage_fields_require_explicit_units(key: str) -> None:
    config = rule_config()
    config[key] = Decimal("0.0075")

    with pytest.raises(RiskConfigurationError, match="explicit percentage string"):
        AccountRiskRules.from_mapping(config)


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("risk_per_trade_frac", Decimal("0.0501")),
        ("short_risk_per_trade_frac", Decimal("0.0501")),
        ("max_position_notional_frac", Decimal("1.01")),
        ("max_gross_exposure_frac", Decimal("2.01")),
        ("bull_chop_gross_exposure_frac", Decimal("2.01")),
        ("max_portfolio_heat_frac", Decimal("0.2501")),
        ("max_adv_frac", Decimal("1.01")),
        ("drawdown_recovery_frac", Decimal("0.5001")),
        ("drawdown_half_risk_frac", Decimal("0.5001")),
        ("drawdown_suspend_frac", Decimal("0.5001")),
        ("daily_loss_block_frac", Decimal("0.5001")),
    ],
)
def test_account_rule_sanity_caps(field_name: str, value: Decimal) -> None:
    with pytest.raises(RiskConfigurationError, match="sanity cap"):
        rules(**{field_name: value})


def test_quantity_rule_controls_size_and_cannot_exceed_account_risk(
    tmp_path: Path,
) -> None:
    with Ledger(tmp_path / "ledger.db") as ledger:
        engine = RiskEngine(rules(), rails(), FixedClock(), ledger)
        smaller_risk = engine.evaluate(
            intent(intent_id="small-risk", command_id="small-risk-cmd", quantity_rule="risk_0.25pct"),
            context(),
        )
        fixed_size = engine.evaluate(
            intent(intent_id="fixed", command_id="fixed-cmd", quantity_rule="fixed_10"),
            context(),
        )
        too_much_risk = engine.evaluate(
            intent(intent_id="too-much", command_id="too-much-cmd", quantity_rule="risk_2pct"),
            context(),
        )
        fixed_too_large = engine.evaluate(
            intent(intent_id="fixed-large", command_id="fixed-large-cmd", quantity_rule="fixed_100"),
            context(),
        )

    assert smaller_risk.accepted
    assert smaller_risk.approved_quantity == Decimal("25")
    assert fixed_size.accepted
    assert fixed_size.approved_quantity == Decimal("10")
    assert not too_much_risk.accepted
    assert too_much_risk.approved_quantity is None
    assert not fixed_too_large.accepted
    assert fixed_too_large.approved_quantity is None


def test_drawdown_suspension_latches_until_recovery_and_rejects_negative_values(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="drawdown_from_peak_frac must be non-negative"):
        context(drawdown_from_peak_frac=Decimal("-0.20"))

    path = tmp_path / "ledger.db"
    with Ledger(path) as ledger:
        engine = RiskEngine(rules(), rails(), FixedClock(), ledger)
        suspended = engine.evaluate(
            intent(intent_id="suspend", command_id="suspend-cmd"),
            context(drawdown_from_peak_frac=Decimal("0.16")),
        )

    with Ledger(path) as ledger:
        engine = RiskEngine(rules(), rails(), FixedClock(), ledger)
        still_suspended = engine.evaluate(
            intent(intent_id="still-suspended", command_id="still-suspended-cmd"),
            context(drawdown_from_peak_frac=Decimal("0.14")),
        )
        recovered = engine.evaluate(
            intent(intent_id="recovered", command_id="recovered-cmd"),
            context(drawdown_from_peak_frac=Decimal("0.05")),
        )

    assert not suspended.accepted
    assert not still_suspended.accepted
    assert recovered.accepted
    assert recovered.approved_quantity == Decimal("75")


def test_minimum_price_boundary_passes(tmp_path: Path) -> None:
    with Ledger(tmp_path / "ledger.db") as ledger:
        verdict = evaluate(ledger, risk_context=context(current_price=Decimal("5")))

    assert next(result for result in verdict.evaluations if result.rule_name == "min_price").passed


def test_venue_position_cap_includes_existing_position(tmp_path: Path) -> None:
    reducing_order = intent(
        intent_id="reduce",
        command_id="reduce-cmd",
        side=Side.SELL,
        quantity_rule="risk_0.5pct",
        stop_loss=Decimal("105"),
        profit_targets=(Decimal("90"),),
    )
    with Ledger(tmp_path / "ledger.db") as ledger:
        engine = RiskEngine(rules(), rails(max_position_quantity=1000), FixedClock(), ledger)
        increasing = engine.evaluate(intent(), context(current_position_quantity=950))
        reducing = engine.evaluate(
            reducing_order,
            context(current_position_quantity=950),
        )

    assert not increasing.accepted
    assert reducing.accepted
    assert reducing.approved_quantity == Decimal("50")


def test_trading_hours_respect_exchange_holidays_and_early_closes() -> None:
    hours = rails().trading_hours
    assert hours is not None

    thanksgiving_open = datetime(2026, 11, 26, 15, 0, tzinfo=timezone.utc)
    early_close_before = datetime(2026, 11, 27, 16, 59, tzinfo=timezone.utc)
    early_close_after = datetime(2026, 11, 27, 18, 30, tzinfo=timezone.utc)

    assert not hours.is_open(thanksgiving_open)
    assert hours.is_open(early_close_before)
    assert not hours.is_open(early_close_after)
    with pytest.raises(ValueError, match="must be timezone-aware"):
        hours.is_open(datetime(2026, 11, 27, 16, 0))


def test_choppy_regime_and_drawdown_brake_scale_risk_with_recovery(
    tmp_path: Path,
) -> None:
    with Ledger(tmp_path / "ledger.db") as ledger:
        engine = RiskEngine(rules(), rails(), FixedClock(), ledger)
        choppy = engine.evaluate(intent(), context(regime="BULL_CHOPIER"))
        assert choppy.approved_quantity == Decimal("37")
        assert next(
            r for r in choppy.evaluations if r.rule_name == "gross_exposure"
        ).threshold == Decimal("50000.00")

        triggered = engine.evaluate(
            intent(intent_id="dd-1", command_id="dd-cmd-1"),
            context(drawdown_from_peak_frac=Decimal("0.08")),
        )
        held = engine.evaluate(
            intent(intent_id="dd-2", command_id="dd-cmd-2"),
            context(drawdown_from_peak_frac=Decimal("0.06")),
        )
        recovered = engine.evaluate(
            intent(intent_id="dd-3", command_id="dd-cmd-3"),
            context(drawdown_from_peak_frac=Decimal("0.05")),
        )
    assert triggered.approved_quantity == Decimal("37")
    assert held.approved_quantity == Decimal("37")
    assert recovered.approved_quantity == Decimal("75")


def test_drawdown_brake_reengages_when_an_intent_command_is_replayed(
    tmp_path: Path,
) -> None:
    with Ledger(tmp_path / "ledger.db") as ledger:
        engine = RiskEngine(rules(), rails(), FixedClock(), ledger)
        high_drawdown = engine.evaluate(
            intent(intent_id="dd-1", command_id="dd-cmd-1"),
            context(drawdown_from_peak_frac=Decimal("0.09")),
        )
        recovered = engine.evaluate(
            intent(intent_id="dd-2", command_id="dd-cmd-2"),
            context(drawdown_from_peak_frac=Decimal("0.04")),
        )
        replayed = engine.evaluate(
            intent(intent_id="dd-1", command_id="dd-cmd-1"),
            context(drawdown_from_peak_frac=Decimal("0.09")),
        )
        # The re-engaged brake must be persisted, not just applied to this verdict: at 6%
        # (between recovery and the half-risk threshold) the latch keeps half size.
        assert ledger.snapshot("account-1").risk_controls["drawdown_brake"] is True
        latched = engine.evaluate(
            intent(intent_id="dd-3", command_id="dd-cmd-3"),
            context(drawdown_from_peak_frac=Decimal("0.06")),
        )

    assert high_drawdown.approved_quantity == Decimal("37")
    assert recovered.approved_quantity == Decimal("75")
    assert replayed.approved_quantity == Decimal("37")
    assert latched.approved_quantity == Decimal("37")


def test_kill_switch_survives_restart_and_can_be_released(tmp_path: Path) -> None:
    path = tmp_path / "ledger.db"
    with Ledger(path) as ledger:
        RiskEngine(rules(), rails(), FixedClock(), ledger).engage_kill_switch(
            "halt-1", "operator halt"
        )

    with Ledger(path) as ledger:
        engine = RiskEngine(rules(), rails(), FixedClock(), ledger)
        stopped = engine.evaluate(intent(), context())
        assert not next(
            r for r in stopped.evaluations if r.rule_name == "persistent_kill_switch"
        ).passed
        engine.release_kill_switch("release-1", "operator cleared")
        assert engine.evaluate(intent(), context()).accepted


def test_kill_switch_rejects_reused_command_id_with_opposite_state(
    tmp_path: Path,
) -> None:
    with Ledger(tmp_path / "ledger.db") as ledger:
        engine = RiskEngine(rules(), rails(), FixedClock(), ledger)
        engine.release_kill_switch("op-1", "operator cleared")

        with pytest.raises(ValueError, match="different event or state"):
            engine.engage_kill_switch("op-1", "operator halt")

        assert engine.evaluate(intent(), context()).accepted


def test_position_notional_at_exact_cap_is_accepted(tmp_path: Path) -> None:
    order = intent(quantity_rule="risk_1pct")
    account_rules = rules(risk_per_trade_frac=Decimal("0.01"))
    with Ledger(tmp_path / "ledger.db") as ledger:
        verdict = evaluate(
            ledger,
            risk_rules=account_rules,
            order_intent=order,
        )

    max_position = next(
        result for result in verdict.evaluations if result.rule_name == "max_position"
    )
    assert verdict.accepted
    assert verdict.approved_quantity == Decimal("100")
    assert (
        max_position.measured_value
        == max_position.threshold
        == Decimal("10000.00")
    )


def test_bear_market_blocks_longs_but_permits_parabolic_short(tmp_path: Path) -> None:
    short = intent(
        intent_id="short-1",
        command_id="short-cmd",
        side=Side.SELL,
        quantity_rule="risk_0.5pct",
        stop_loss=Decimal("105"),
        profit_targets=(Decimal("90"),),
    )
    with Ledger(tmp_path / "ledger.db") as ledger:
        long_verdict = evaluate(ledger, risk_context=context(regime="BEAR_PROTECTIVE"))
        short_verdict = evaluate(
            ledger,
            risk_context=context(regime="BEAR_PROTECTIVE"),
            order_intent=short,
        )
    assert not long_verdict.accepted
    assert short_verdict.accepted
    assert short_verdict.approved_quantity == Decimal("50")


def test_short_intent_cannot_request_long_side_risk(tmp_path: Path) -> None:
    """Parabolic shorts are capped at 0.5%; a short asking for 0.75% is refused."""
    short = intent(
        side=Side.SELL,
        quantity_rule="risk_0.75pct",
        stop_loss=Decimal("105"),
        profit_targets=(Decimal("90"),),
    )
    with Ledger(tmp_path / "ledger.db") as ledger:
        verdict = evaluate(ledger, order_intent=short)

    quantity_rule = next(r for r in verdict.evaluations if r.rule_name == "quantity_rule")
    assert not verdict.accepted
    assert not quantity_rule.passed


def _rule(verdict: RiskVerdict, name: str) -> RiskRuleResult:
    return next(result for result in verdict.evaluations if result.rule_name == name)


# A 0.1 stop at 99 risk-sizes to 3750 shares (371,250 notional); the 20% cap on 50,000
# equity is 10,000, i.e. floor(10000 / 99) = 101 shares.
OVERSIZE = {"entry_price": Decimal("99"), "stop_loss": Decimal("98.9")}


def test_oversize_risk_intent_is_refused_on_max_position_by_default(tmp_path: Path) -> None:
    assert rules().clamp_to_position_cap is False
    with Ledger(tmp_path / "ledger.db") as ledger:
        verdict = evaluate(ledger, order_intent=intent(**OVERSIZE))

    max_position = _rule(verdict, "max_position")
    assert not verdict.accepted
    assert verdict.approved_quantity is None
    assert not max_position.passed
    assert max_position.measured_value == Decimal("3750") * Decimal("99")
    assert max_position.reason == (
        "Position notional or equity is unknown or exceeds the account cap"
    )


def test_clamp_reduces_oversize_risk_intent_to_the_position_cap(tmp_path: Path) -> None:
    with Ledger(tmp_path / "ledger.db") as ledger:
        verdict = evaluate(
            ledger,
            risk_rules=rules(clamp_to_position_cap=True),
            order_intent=intent(**OVERSIZE),
        )

    max_position = _rule(verdict, "max_position")
    risk = _rule(verdict, "risk_per_trade")
    assert verdict.accepted
    assert verdict.approved_quantity == Decimal("101")
    assert risk.measured_value == Decimal("101") * Decimal("0.1")
    assert max_position.passed
    assert max_position.measured_value == Decimal("101") * Decimal("99")
    assert max_position.threshold == Decimal("10000.00")
    assert "reduced from 3750 to 101" in max_position.reason
    assert _rule(verdict, "adv_pct").measured_value == Decimal("101") * Decimal("99")


def test_clamp_leaves_an_under_cap_intent_unchanged(tmp_path: Path) -> None:
    with Ledger(tmp_path / "ledger.db") as ledger:
        verdict = evaluate(ledger, risk_rules=rules(clamp_to_position_cap=True))

    max_position = _rule(verdict, "max_position")
    assert verdict.accepted
    assert verdict.approved_quantity == Decimal("75")
    assert max_position.reason == "Position notional is within the account cap"


def test_clamp_never_shrinks_a_fixed_size(tmp_path: Path) -> None:
    with Ledger(tmp_path / "ledger.db") as ledger:
        verdict = evaluate(
            ledger,
            risk_rules=rules(clamp_to_position_cap=True),
            order_intent=intent(quantity_rule="fixed_200"),
        )

    assert not verdict.accepted
    assert verdict.approved_quantity is None
    assert not _rule(verdict, "max_position").passed


def test_clamp_with_unknown_equity_still_refuses(tmp_path: Path) -> None:
    with Ledger(tmp_path / "ledger.db") as ledger:
        verdict = evaluate(
            ledger,
            risk_rules=rules(clamp_to_position_cap=True),
            order_intent=intent(**OVERSIZE),
            risk_context=context(equity=None),
        )

    assert not verdict.accepted
    assert verdict.approved_quantity is None
    assert not _rule(verdict, "max_position").passed
    assert not _rule(verdict, "risk_per_trade").passed


def test_clamp_config_key_is_optional_and_must_be_a_real_boolean() -> None:
    assert AccountRiskRules.from_mapping(rule_config()).clamp_to_position_cap is False
    config = rule_config()
    config["clamp_to_position_cap"] = True
    assert AccountRiskRules.from_mapping(config) == rules(clamp_to_position_cap=True)
    for bad in ("true", 1, None):
        config["clamp_to_position_cap"] = bad
        with pytest.raises(RiskConfigurationError, match="clamp_to_position_cap must be a boolean"):
            AccountRiskRules.from_mapping(config)
    with pytest.raises(RiskConfigurationError, match="clamp_to_position_cap must be a boolean"):
        rules(clamp_to_position_cap="true")


def test_notional_rule_sizes_by_equity_weight(tmp_path: Path) -> None:
    order = intent(
        quantity_rule="notional_5pct",
        entry_price=Decimal("99"),
        stop_loss=Decimal("94"),
    )
    with Ledger(tmp_path / "ledger.db") as ledger:
        verdict = evaluate(ledger, order_intent=order)

    # floor(50000 * 0.05 / 99) = floor(25.25) = 25
    assert verdict.accepted
    assert verdict.approved_quantity == Decimal("25")
    assert _rule(verdict, "risk_per_trade").measured_value == Decimal("125")
    assert "notional_<x>pct" in _rule(verdict, "quantity_rule").threshold


def test_notional_rule_at_the_position_cap_is_accepted(tmp_path: Path) -> None:
    order = intent(quantity_rule="notional_20pct", stop_loss=Decimal("98"))
    with Ledger(tmp_path / "ledger.db") as ledger:
        verdict = evaluate(ledger, order_intent=order)

    assert verdict.accepted
    assert verdict.approved_quantity == Decimal("100")


@pytest.mark.parametrize(
    "quantity_rule",
    [
        "notional_20.01pct",
        "notional_0pct",
        "notional_-5pct",
        "notional_abcpct",
        "notional_pct",
        "notional_NaNpct",
        "notional_Infinitypct",
        "notional_5",
    ],
)
def test_notional_rule_refuses_invalid_or_over_cap_weights(
    tmp_path: Path, quantity_rule: str
) -> None:
    with Ledger(tmp_path / "ledger.db") as ledger:
        verdict = evaluate(ledger, order_intent=intent(quantity_rule=quantity_rule))

    assert not verdict.accepted
    assert verdict.approved_quantity is None
    assert not _rule(verdict, "quantity_rule").passed


def test_notional_rule_is_refused_when_its_stop_risk_exceeds_the_budget(
    tmp_path: Path,
) -> None:
    # 20% weight = 100 shares; a 5.00 stop risks 500 against a 375 budget.
    order = intent(quantity_rule="notional_20pct")
    with Ledger(tmp_path / "ledger.db") as ledger:
        verdict = evaluate(ledger, order_intent=order)

    risk = _rule(verdict, "risk_per_trade")
    assert not verdict.accepted
    assert not risk.passed
    assert risk.measured_value == Decimal("500")
    assert risk.threshold == Decimal("375")
    assert _rule(verdict, "quantity_rule").passed
    assert _rule(verdict, "max_position").passed


def test_notional_rule_risk_budget_follows_the_regime_scale(tmp_path: Path) -> None:
    # 10% weight = 50 shares risking 250: inside the 375 bull budget, outside the
    # halved 187.5 choppy budget.
    with Ledger(tmp_path / "ledger.db") as ledger:
        engine = RiskEngine(rules(), rails(), FixedClock(), ledger)
        bull = engine.evaluate(intent(quantity_rule="notional_10pct"), context())
        choppy = engine.evaluate(
            intent(intent_id="chop", command_id="chop-cmd", quantity_rule="notional_10pct"),
            context(regime="BULL_CHOPIER"),
        )

    assert bull.accepted
    assert bull.approved_quantity == Decimal("50")
    assert not choppy.accepted
    assert not _rule(choppy, "risk_per_trade").passed
    assert _rule(choppy, "risk_per_trade").threshold == Decimal("187.5")


def test_notional_rule_with_unknown_equity_refuses(tmp_path: Path) -> None:
    with Ledger(tmp_path / "ledger.db") as ledger:
        verdict = evaluate(
            ledger,
            order_intent=intent(quantity_rule="notional_5pct"),
            risk_context=context(equity=None),
        )

    assert not verdict.accepted
    assert not _rule(verdict, "risk_per_trade").passed

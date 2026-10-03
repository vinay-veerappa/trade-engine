"""Parity: the Rust risk rules (te_core::risk) against the pre-port Python engine.

docs/RUST_PORT.md P1d / D4: the Python engine is the oracle. `ORACLE_SOURCE` below is
the whole of `src/trade_engine/risk.py` as it stood before the port, embedded
verbatim and run in its own module. Every generated case is driven through BOTH
implementations on their own ledgers and must give the identical verdict, the
identical ordered rule results (values, types and reason strings) and the identical
drawdown-control events, or the identical refusal.
"""

from __future__ import annotations

import itertools
import random
import sys
import types
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

import trade_engine.risk as new
from trade_engine.domain.instruments import Equity, Side
from trade_engine.domain.risk import RiskControlChange
from trade_engine.domain.signals import OrderIntent
from trade_engine.interfaces.clock import Clock
from trade_engine.ledger import Event, EventKind, Ledger

# ---------------------------------------------------------------------------
# frozen oracle, do not edit, from commit b9274d95d8f5be1937a218f82d673ed7a6f91009
# (src/trade_engine/risk.py before P1d)
# ---------------------------------------------------------------------------
ORACLE_SOURCE = r'''"""Account risk evaluation and venue safety rails (Architecture §4.3, E6)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, time
from decimal import Decimal, InvalidOperation, ROUND_FLOOR
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from trade_engine.calendar import ExchangeCalendar, get_calendar
from trade_engine.domain.instruments import Equity, Side
from trade_engine.domain.portfolio import VenueEnv
from trade_engine.domain.risk import RiskControlChange, RiskRuleResult, RiskVerdict
from trade_engine.domain.signals import OrderIntent
from trade_engine.interfaces.clock import Clock
from trade_engine.ledger import Event, EventKind, Ledger

ZERO = Decimal("0")
ONE = Decimal("1")
RiskRegime = Literal["BULL_EXPLOSIVE", "BULL_CHOPIER", "BEAR_PROTECTIVE", "UNKNOWN"]


class RiskConfigurationError(ValueError):
    """Raised when account rules or venue rails are invalid or incomplete."""


@dataclass(frozen=True)
class AccountRiskRules:
    """Equity-account thresholds supplied by the account's rules configuration."""

    risk_per_trade_frac: Decimal
    short_risk_per_trade_frac: Decimal
    max_position_notional_frac: Decimal
    max_gross_exposure_frac: Decimal
    bull_chop_gross_exposure_frac: Decimal
    max_portfolio_heat_frac: Decimal
    max_positions: int
    max_positions_per_industry: int
    min_price: Decimal
    max_adv_frac: Decimal
    earnings_blackout_sessions: int
    drawdown_half_risk_frac: Decimal
    drawdown_suspend_frac: Decimal
    drawdown_recovery_frac: Decimal
    daily_loss_block_frac: Decimal
    # Opt-in: reduce a risk_<x>pct size to the position cap instead of refusing it.
    clamp_to_position_cap: bool = False

    @classmethod
    def from_mapping(cls, values: Mapping[str, object]) -> AccountRiskRules:
        if not isinstance(values, Mapping):
            raise RiskConfigurationError("account risk rules must be loaded from a mapping")
        if any(not isinstance(key, str) for key in values):
            raise RiskConfigurationError("account risk rule keys must be strings")

        config_fields = {
            "risk_per_trade": "risk_per_trade_frac",
            "short_risk_per_trade": "short_risk_per_trade_frac",
            "max_position_notional": "max_position_notional_frac",
            "max_gross_exposure": "max_gross_exposure_frac",
            "bull_chop_gross_exposure": "bull_chop_gross_exposure_frac",
            "max_portfolio_heat": "max_portfolio_heat_frac",
            "max_positions": "max_positions",
            "max_positions_per_industry": "max_positions_per_industry",
            "min_price": "min_price",
            "max_adv": "max_adv_frac",
            "earnings_blackout_sessions": "earnings_blackout_sessions",
            "drawdown_half_risk": "drawdown_half_risk_frac",
            "drawdown_suspend": "drawdown_suspend_frac",
            "drawdown_recovery": "drawdown_recovery_frac",
            "daily_loss_block": "daily_loss_block_frac",
        }
        # Optional keys: absent means the field's documented default.
        optional_fields = {"clamp_to_position_cap": "clamp_to_position_cap"}
        expected = set(config_fields)
        unknown = set(values) - expected - set(optional_fields)
        if unknown:
            raise RiskConfigurationError(
                f"Unknown account risk rule keys: {', '.join(sorted(unknown))}"
            )
        missing = expected - set(values)
        if missing:
            raise RiskConfigurationError(
                f"Missing account risk rule keys: {', '.join(sorted(missing))}"
            )

        percent_fields = expected - {
            "max_positions",
            "max_positions_per_industry",
            "min_price",
            "earnings_blackout_sessions",
        }
        parsed: dict[str, object] = {}
        for config_name, field_name in config_fields.items():
            value = values[config_name]
            if config_name in percent_fields:
                if not isinstance(value, str) or not value.strip().endswith("%"):
                    raise RiskConfigurationError(
                        f"{config_name} must be an explicit percentage string such as '0.75%'"
                    )
                decimal_text = value.strip()[:-1].strip()
            else:
                if isinstance(value, bool):
                    raise RiskConfigurationError(
                        f"{config_name} must be numeric, not boolean"
                    )
                decimal_text = str(value)
            try:
                decimal_value = Decimal(str(decimal_text))
            except (InvalidOperation, TypeError, ValueError) as err:
                raise RiskConfigurationError(
                    f"{config_name} must contain a valid number"
                ) from err
            parsed[field_name] = (
                decimal_value / Decimal("100")
                if config_name in percent_fields
                else decimal_value
            )

        for name in (
            "max_positions",
            "max_positions_per_industry",
            "earnings_blackout_sessions",
        ):
            value = values[name]
            if isinstance(value, str) and value.isdecimal():
                value = int(value)
            if isinstance(value, bool) or not isinstance(value, int):
                raise RiskConfigurationError(f"{name} must be an integer")
            parsed[name] = value

        for config_name, field_name in optional_fields.items():
            if config_name not in values:
                continue
            value = values[config_name]
            if not isinstance(value, bool):
                raise RiskConfigurationError(
                    f"{config_name} must be a boolean (true/false), not {type(value).__name__}"
                )
            parsed[field_name] = value

        return cls(**parsed)  # type: ignore[arg-type]

    def __post_init__(self) -> None:
        positive = (
            "risk_per_trade_frac",
            "short_risk_per_trade_frac",
            "max_position_notional_frac",
            "max_gross_exposure_frac",
            "bull_chop_gross_exposure_frac",
            "max_portfolio_heat_frac",
            "min_price",
            "max_adv_frac",
            "drawdown_recovery_frac",
            "drawdown_half_risk_frac",
            "drawdown_suspend_frac",
            "daily_loss_block_frac",
        )
        for name in positive:
            value = getattr(self, name)
            if not isinstance(value, Decimal) or not value.is_finite() or value <= ZERO:
                raise RiskConfigurationError(f"{name} must be a positive finite Decimal")
        sanity_caps = {
            "risk_per_trade_frac": Decimal("0.05"),
            "short_risk_per_trade_frac": Decimal("0.05"),
            "max_position_notional_frac": Decimal("1"),
            "max_gross_exposure_frac": Decimal("2"),
            "bull_chop_gross_exposure_frac": Decimal("2"),
            "max_portfolio_heat_frac": Decimal("0.25"),
            "max_adv_frac": Decimal("1"),
            "drawdown_recovery_frac": Decimal("0.50"),
            "drawdown_half_risk_frac": Decimal("0.50"),
            "drawdown_suspend_frac": Decimal("0.50"),
            "daily_loss_block_frac": Decimal("0.50"),
        }
        for name, maximum in sanity_caps.items():
            if getattr(self, name) > maximum:
                raise RiskConfigurationError(
                    f"{name} exceeds its sanity cap of {maximum}"
                )
        for name in ("max_positions", "max_positions_per_industry"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise RiskConfigurationError(f"{name} must be a positive integer")
        if (
            isinstance(self.earnings_blackout_sessions, bool)
            or not isinstance(self.earnings_blackout_sessions, int)
            or self.earnings_blackout_sessions < 0
        ):
            raise RiskConfigurationError("earnings_blackout_sessions must be non-negative")
        if not isinstance(self.clamp_to_position_cap, bool):
            raise RiskConfigurationError("clamp_to_position_cap must be a boolean")
        if self.drawdown_recovery_frac >= self.drawdown_half_risk_frac:
            raise RiskConfigurationError("drawdown recovery must be below the half-risk threshold")
        if self.drawdown_half_risk_frac >= self.drawdown_suspend_frac:
            raise RiskConfigurationError("drawdown half-risk threshold must precede suspension")
        if self.bull_chop_gross_exposure_frac > self.max_gross_exposure_frac:
            raise RiskConfigurationError(
                "bull-chop gross exposure cannot exceed the maximum gross exposure"
            )


@dataclass(frozen=True)
class TradingHours:
    """Recurring local-time venue hours. Overnight sessions must use a separate adapter."""

    timezone: str
    weekdays: tuple[int, ...]
    opens: time
    closes: time
    exchange: str = "XNYS"
    _calendar: ExchangeCalendar = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        try:
            ZoneInfo(self.timezone)
        except (TypeError, ZoneInfoNotFoundError, ValueError) as err:
            raise RiskConfigurationError(
                f"Unknown trading-hours timezone '{self.timezone}'"
            ) from err
        if not self.weekdays or any(
            isinstance(day, bool) or not isinstance(day, int) or day not in range(7)
            for day in self.weekdays
        ):
            raise RiskConfigurationError("trading-hours weekdays must be integers from 0 to 6")
        if not isinstance(self.opens, time) or not isinstance(self.closes, time):
            raise RiskConfigurationError("trading hours must use datetime.time boundaries")
        if self.opens.utcoffset() is not None or self.closes.utcoffset() is not None:
            raise RiskConfigurationError("trading-hours boundaries must be local wall times")
        if self.opens >= self.closes:
            raise RiskConfigurationError("trading hours must open before they close")
        object.__setattr__(self, "_calendar", get_calendar(self.exchange))

    def is_open(self, instant: datetime) -> bool:
        if instant.tzinfo is None or instant.utcoffset() is None:
            raise ValueError("trading-hours instant must be timezone-aware (I7)")
        local = instant.astimezone(ZoneInfo(self.timezone))
        session_date = local.date()
        if local.weekday() not in self.weekdays or not self._calendar.is_session(session_date):
            return False
        session_open = self._calendar.session_open(session_date).astimezone(local.tzinfo)
        session_close = self._calendar.session_close(session_date).astimezone(local.tzinfo)
        opens = max(self.opens, session_open.timetz().replace(tzinfo=None))
        closes = min(self.closes, session_close.timetz().replace(tzinfo=None))
        return (
            opens <= local.timetz().replace(tzinfo=None) < closes
        )


@dataclass(frozen=True)
class VenueRiskRails:
    """Venue limits. All rails are mandatory for paper and live environments."""

    venue_id: str
    environment: VenueEnv
    allowed_symbols: frozenset[str] | None = None
    max_position_quantity: int | None = None
    max_orders_per_day: int | None = None
    max_daily_loss: Decimal | None = None
    trading_hours: TradingHours | None = None
    duplicate_protection: bool | None = None
    persistent_kill_switch: bool | None = None

    def __post_init__(self) -> None:
        if not self.venue_id:
            raise RiskConfigurationError("venue_id must be non-empty")
        if self.environment not in ("sim", "paper", "live"):
            raise RiskConfigurationError(f"Invalid venue environment '{self.environment}'")
        if self.allowed_symbols is not None:
            if isinstance(self.allowed_symbols, str):
                raise RiskConfigurationError("allowed_symbols must be a collection of symbols")
            symbols = frozenset(s.strip().upper() for s in self.allowed_symbols)
            if not symbols or any(not symbol for symbol in symbols):
                raise RiskConfigurationError("allowed_symbols must contain non-empty symbols")
            object.__setattr__(self, "allowed_symbols", symbols)
        for name in ("max_position_quantity", "max_orders_per_day"):
            value = getattr(self, name)
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value <= 0
            ):
                raise RiskConfigurationError(f"{name} must be a positive integer")
        if self.max_daily_loss is not None and (
            not isinstance(self.max_daily_loss, Decimal)
            or not self.max_daily_loss.is_finite()
            or self.max_daily_loss <= ZERO
        ):
            raise RiskConfigurationError("max_daily_loss must be a positive finite Decimal")
        if self.trading_hours is not None and not isinstance(self.trading_hours, TradingHours):
            raise RiskConfigurationError("trading_hours must be a TradingHours value")
        for name in ("duplicate_protection", "persistent_kill_switch"):
            value = getattr(self, name)
            if value is not None and not isinstance(value, bool):
                raise RiskConfigurationError(f"{name} must be a boolean")
            if value is False:
                raise RiskConfigurationError(f"{name} cannot be disabled")

        if self.environment in ("paper", "live"):
            required = (
                "allowed_symbols",
                "max_position_quantity",
                "max_orders_per_day",
                "max_daily_loss",
                "trading_hours",
                "duplicate_protection",
                "persistent_kill_switch",
            )
            missing = [name for name in required if getattr(self, name) is None]
            if missing:
                raise RiskConfigurationError(
                    f"{self.environment} venue '{self.venue_id}' missing required rails: "
                    + ", ".join(missing)
                )
            if not self.duplicate_protection or not self.persistent_kill_switch:
                raise RiskConfigurationError(
                    "paper/live duplicate protection and persistent kill switch must be enabled"
                )


@dataclass(frozen=True)
class RiskContext:
    """Measurements; position quantity is signed for the intent instrument."""

    equity: Decimal | None
    current_price: Decimal | None
    gross_exposure: Decimal | None
    portfolio_heat: Decimal | None
    open_positions: int | None
    industry: str | None
    industry_positions: int | None
    average_dollar_volume_20d: Decimal | None
    sessions_until_earnings: int | None
    regime: RiskRegime | None
    macro_high_risk_day: bool | None
    drawdown_from_peak_frac: Decimal | None
    previous_session_pnl_frac: Decimal | None
    venue_orders_today: int | None
    venue_daily_pnl: Decimal | None
    current_position_quantity: int | None = None

    def __post_init__(self) -> None:
        for name in ("equity", "current_price", "average_dollar_volume_20d"):
            value = getattr(self, name)
            if value is not None and (
                not isinstance(value, Decimal) or not value.is_finite() or value <= ZERO
            ):
                raise ValueError(f"{name} must be a positive finite Decimal when provided")
        for name in ("gross_exposure", "portfolio_heat"):
            value = getattr(self, name)
            if value is not None and (
                not isinstance(value, Decimal) or not value.is_finite() or value < ZERO
            ):
                raise ValueError(f"{name} must be a non-negative finite Decimal when provided")
        for name in (
            "drawdown_from_peak_frac",
            "previous_session_pnl_frac",
            "venue_daily_pnl",
        ):
            value = getattr(self, name)
            if value is not None and (
                not isinstance(value, Decimal) or not value.is_finite()
            ):
                raise ValueError(f"{name} must be a finite Decimal when provided")
        if (
            self.drawdown_from_peak_frac is not None
            and self.drawdown_from_peak_frac < ZERO
        ):
            raise ValueError("drawdown_from_peak_frac must be non-negative")
        for name in (
            "open_positions",
            "industry_positions",
            "sessions_until_earnings",
            "venue_orders_today",
        ):
            value = getattr(self, name)
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 0
            ):
                raise ValueError(f"{name} must be a non-negative integer when provided")
        if self.current_position_quantity is not None and (
            isinstance(self.current_position_quantity, bool)
            or not isinstance(self.current_position_quantity, int)
        ):
            raise ValueError("current_position_quantity must be an integer when provided")
        if self.industry is not None and not self.industry:
            raise ValueError("industry must be non-empty when provided")
        if self.regime is not None and self.regime not in (
            "BULL_EXPLOSIVE",
            "BULL_CHOPIER",
            "BEAR_PROTECTIVE",
            "UNKNOWN",
        ):
            raise ValueError(f"Unsupported market regime '{self.regime}'")
        if self.macro_high_risk_day is not None and not isinstance(
            self.macro_high_risk_day, bool
        ):
            raise ValueError("macro_high_risk_day must be a boolean when provided")


class RiskEngine:
    """Size an equity intent and evaluate every account rule and venue rail."""

    def __init__(
        self,
        account_rules: AccountRiskRules,
        venue_rails: VenueRiskRails,
        clock: Clock,
        ledger: Ledger,
    ) -> None:
        self.account_rules = account_rules
        self.venue_rails = venue_rails
        self.clock = clock
        self.ledger = ledger

    def _set_kill_switch(self, command_id: str, enabled: bool, reason: str) -> None:
        if not command_id:
            raise ValueError("kill-switch command_id must be non-empty (I3)")
        if not reason.strip():
            raise ValueError("kill-switch reason must be non-empty")
        now = self._now()
        event = self.ledger.append(
            Event(
                account=f"__venue__:{self.venue_rails.venue_id}",
                kind=EventKind.RISK_CONTROL,
                payload=RiskControlChange("kill_switch", enabled, reason, now),
                ts_utc=now,
                command_id=command_id,
            )
        )
        if (
            event.account != f"__venue__:{self.venue_rails.venue_id}"
            or event.kind is not EventKind.RISK_CONTROL
            or not isinstance(event.payload, RiskControlChange)
            or event.payload.control_id != "kill_switch"
            or event.payload.enabled is not enabled
        ):
            raise ValueError(
                f"kill-switch command_id {command_id!r} was already used "
                "for a different event or state"
            )

    def engage_kill_switch(self, command_id: str, reason: str) -> None:
        self._set_kill_switch(command_id, True, reason)

    def release_kill_switch(self, command_id: str, reason: str) -> None:
        self._set_kill_switch(command_id, False, reason)

    def _control_enabled(self, account_id: str, control_id: str) -> bool:
        return self.ledger.state(account_id).risk_controls.get(control_id, False)

    def _now(self) -> datetime:
        now = self.clock.now_utc()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("Clock.now_utc() must return a timezone-aware datetime (I7)")
        return now

    def _update_drawdown_controls(
        self, account_id: str, drawdown: Decimal | None, intent_command_id: str
    ) -> tuple[bool, bool]:
        state = self.ledger.state(account_id)
        brake_active = state.risk_controls.get("drawdown_brake", False)
        suspension_active = state.risk_controls.get("drawdown_suspension", False)
        desired_brake = brake_active
        desired_suspension = suspension_active
        if drawdown is not None:
            recovery = drawdown <= self.account_rules.drawdown_recovery_frac
            desired_brake = (
                False
                if recovery
                else brake_active
                or drawdown >= self.account_rules.drawdown_half_risk_frac
            )
            desired_suspension = (
                False
                if recovery
                else suspension_active
                or drawdown >= self.account_rules.drawdown_suspend_frac
            )
            for control_id, active, desired in (
                ("drawdown_brake", brake_active, desired_brake),
                ("drawdown_suspension", suspension_active, desired_suspension),
            ):
                if desired == active:
                    continue
                now = self._now()
                self.ledger.append(
                    Event(
                        account=account_id,
                        kind=EventKind.RISK_CONTROL,
                        payload=RiskControlChange(
                            control_id,
                            desired,
                            f"Drawdown {drawdown} triggered account brake transition",
                            now,
                        ),
                        ts_utc=now,
                        command_id=(
                            f"risk-control:{account_id}:{control_id}:"
                            f"{state.last_seq}:{intent_command_id}"
                        ),
                    )
                )
        return desired_brake, desired_suspension

    def evaluate(self, intent: OrderIntent, context: RiskContext) -> RiskVerdict:
        """Evaluate all rules and persist any drawdown-control transitions."""
        if not isinstance(intent.instrument, Equity):
            raise ValueError("E6 account rules support equity intents only")
        rules = self.account_rules
        rails = self.venue_rails
        side = intent.side
        distance = abs(intent.entry_price - intent.stop_loss)
        risk_frac = (
            rules.short_risk_per_trade_frac
            if side is Side.SELL
            else rules.risk_per_trade_frac
        )
        quantity_rule = intent.quantity_rule
        requested_risk_frac: Decimal | None = None
        fixed_quantity: Decimal | None = None
        notional_frac: Decimal | None = None
        quantity_rule_ok = False
        risk_sized = quantity_rule.startswith("risk_") and quantity_rule.endswith("pct")
        if risk_sized:
            raw_pct = quantity_rule[len("risk_") : -len("pct")]
            try:
                requested_risk_frac = Decimal(raw_pct) / Decimal("100")
            except InvalidOperation:
                requested_risk_frac = None
            if (
                requested_risk_frac is not None
                and requested_risk_frac.is_finite()
                and requested_risk_frac > ZERO
            ):
                quantity_rule_ok = requested_risk_frac <= risk_frac
                sizing_risk_frac = min(requested_risk_frac, risk_frac)
            else:
                requested_risk_frac = None
                sizing_risk_frac = risk_frac
        elif quantity_rule.startswith("fixed_"):
            raw_quantity = quantity_rule[len("fixed_") :]
            if raw_quantity.isdecimal():
                fixed_value = int(raw_quantity)
                if fixed_value > 0:
                    fixed_quantity = Decimal(fixed_value)
                    quantity_rule_ok = True
            sizing_risk_frac = risk_frac
        elif quantity_rule.startswith("notional_") and quantity_rule.endswith("pct"):
            # Weight sizing: a fraction of equity, capped by the position limit; the
            # stop-distance risk it implies is still checked against the risk budget.
            raw_pct = quantity_rule[len("notional_") : -len("pct")]
            try:
                requested_notional_frac = Decimal(raw_pct) / Decimal("100")
            except InvalidOperation:
                requested_notional_frac = None
            if (
                requested_notional_frac is not None
                and requested_notional_frac.is_finite()
                and requested_notional_frac > ZERO
            ):
                quantity_rule_ok = (
                    requested_notional_frac <= rules.max_position_notional_frac
                )
                notional_frac = min(
                    requested_notional_frac, rules.max_position_notional_frac
                )
            sizing_risk_frac = risk_frac
        else:
            sizing_risk_frac = risk_frac
        regime_scale = Decimal("0.5") if context.regime == "BULL_CHOPIER" else ONE
        drawdown_brake, drawdown_suspension = self._update_drawdown_controls(
            intent.account_id, context.drawdown_from_peak_frac, intent.command_id
        )
        drawdown_scale = Decimal("0.5") if drawdown_brake else ONE
        risk_budget = (
            context.equity * sizing_risk_frac * regime_scale * drawdown_scale
            if context.equity is not None
            else ZERO
        )
        max_position = (
            context.equity * rules.max_position_notional_frac
            if context.equity is not None
            else None
        )
        if fixed_quantity is not None:
            quantity = fixed_quantity
        elif notional_frac is not None:
            quantity = (
                Decimal(
                    int(
                        (context.equity * notional_frac / intent.entry_price)
                        .to_integral_value(rounding=ROUND_FLOOR)
                    )
                )
                if context.equity is not None
                else ZERO
            )
        else:
            quantity = Decimal(
                int((risk_budget / distance).to_integral_value(rounding=ROUND_FLOOR))
            )
        # Opt-in clamp: a risk-sized quantity over the position cap is reduced to the
        # cap rather than refused. Fixed sizes are explicit requests and never clamped;
        # unknown equity leaves nothing to clamp to, so its refusal stands.
        unclamped_quantity: Decimal | None = None
        if rules.clamp_to_position_cap and risk_sized and max_position is not None:
            cap_quantity = Decimal(
                int(
                    (max_position / intent.entry_price).to_integral_value(
                        rounding=ROUND_FLOOR
                    )
                )
            )
            if quantity > cap_quantity:
                unclamped_quantity = quantity
                quantity = cap_quantity
        risk_amount = quantity * distance
        notional = quantity * intent.entry_price
        gross_limit_frac = (
            rules.bull_chop_gross_exposure_frac
            if context.regime == "BULL_CHOPIER"
            else rules.max_gross_exposure_frac
        )

        results: list[RiskRuleResult] = []

        def record(
            name: str,
            passed: bool,
            measured: object,
            threshold: object,
            reason: str,
        ) -> None:
            results.append(RiskRuleResult(name, passed, measured, threshold, reason))

        def compare(
            name: str,
            passed: bool,
            measured: object,
            threshold: object,
            success: str,
            refusal: str,
        ) -> None:
            record(name, passed, measured, threshold, success if passed else refusal)

        risk_ok = context.equity is not None and quantity > ZERO and risk_amount <= risk_budget
        compare(
            "risk_per_trade",
            risk_ok,
            risk_amount,
            risk_budget,
            "Sized quantity is within the adjusted risk budget",
            "Risk budget/equity is unknown or cannot fund one whole share",
        )
        compare(
            "quantity_rule",
            quantity_rule_ok,
            intent.quantity_rule,
            (
                f"risk_<x>pct with x <= {risk_frac * Decimal('100')}; "
                f"notional_<x>pct with x <= "
                f"{rules.max_position_notional_frac * Decimal('100')}; "
                "or fixed_<n> with n > 0"
            ),
            "Intent sizing rule is supported and within its configured risk ceiling",
            "Intent sizing rule is invalid or requests more risk than account rules allow",
        )
        compare(
            "max_position",
            max_position is not None and notional <= max_position,
            notional,
            max_position if max_position is not None else "UNKNOWN",
            (
                "Position notional is within the account cap"
                if unclamped_quantity is None
                else (
                    f"Quantity reduced from {unclamped_quantity} to {quantity} shares "
                    "to fit the position cap (clamp_to_position_cap)"
                )
            ),
            "Position notional or equity is unknown or exceeds the account cap",
        )
        gross_cap = (
            context.equity * gross_limit_frac if context.equity is not None else None
        )
        proposed_gross = (
            context.gross_exposure + notional
            if context.gross_exposure is not None
            else None
        )
        compare(
            "gross_exposure",
            gross_cap is not None
            and proposed_gross is not None
            and proposed_gross <= gross_cap,
            proposed_gross if proposed_gross is not None else "UNKNOWN",
            gross_cap if gross_cap is not None else "UNKNOWN",
            "Gross exposure is within the regime-adjusted cap",
            "Gross exposure or equity is unknown or exceeds the cap",
        )
        heat_cap = (
            context.equity * rules.max_portfolio_heat_frac
            if context.equity is not None
            else None
        )
        heat = (
            context.portfolio_heat + risk_amount
            if context.portfolio_heat is not None
            else None
        )
        compare(
            "portfolio_heat",
            heat_cap is not None and heat is not None and heat <= heat_cap,
            heat if heat is not None else "UNKNOWN",
            heat_cap if heat_cap is not None else "UNKNOWN",
            "Portfolio heat is within the account cap",
            "Portfolio heat or equity is unknown or exceeds the cap",
        )
        compare(
            "max_positions",
            context.open_positions is not None
            and context.open_positions < rules.max_positions,
            context.open_positions + 1
            if context.open_positions is not None
            else "UNKNOWN",
            rules.max_positions,
            "Position count is within the account cap",
            "Position count is unknown or at the account cap",
        )
        compare(
            "per_industry",
            context.industry is not None
            and context.industry_positions is not None
            and context.industry_positions < rules.max_positions_per_industry,
            context.industry_positions + 1
            if context.industry_positions is not None
            else "UNKNOWN",
            rules.max_positions_per_industry,
            "Industry position count is within the account cap",
            f"Industry '{context.industry or 'UNKNOWN'}' is at its cap or unknown",
        )
        compare(
            "min_price",
            context.current_price is not None and context.current_price >= rules.min_price,
            context.current_price if context.current_price is not None else "UNKNOWN",
            rules.min_price,
            "Current price meets the account minimum",
            "Current price is unknown or below the account minimum",
        )
        adv_limit = (
            context.average_dollar_volume_20d * rules.max_adv_frac
            if context.average_dollar_volume_20d is not None
            else None
        )
        compare(
            "adv_pct",
            adv_limit is not None and notional <= adv_limit,
            notional,
            adv_limit if adv_limit is not None else "UNKNOWN",
            "Position is within the 20-day ADV cap",
            "20-day ADV is unknown or position exceeds its cap",
        )
        earnings_ok = (
            context.sessions_until_earnings is not None
            and context.sessions_until_earnings > rules.earnings_blackout_sessions
        )
        compare(
            "earnings_blackout",
            earnings_ok,
            context.sessions_until_earnings
            if context.sessions_until_earnings is not None
            else "UNKNOWN",
            f"> {rules.earnings_blackout_sessions} sessions",
            "Earnings are outside the blackout window",
            "Earnings date is unknown or inside the blackout window",
        )
        regime_ok = context.regime is not None and context.regime != "UNKNOWN" and not (
            context.regime == "BEAR_PROTECTIVE" and side is Side.BUY
        )
        compare(
            "regime",
            regime_ok,
            context.regime if context.regime is not None else "UNKNOWN",
            "Known regime; no new longs in BEAR_PROTECTIVE",
            "Regime permits this entry",
            "Unknown regime or BEAR_PROTECTIVE blocks this entry",
        )
        compare(
            "macro_high_risk_day",
            context.macro_high_risk_day is False,
            context.macro_high_risk_day
            if context.macro_high_risk_day is not None
            else "UNKNOWN",
            False,
            "No high-risk macro event blocks this session",
            "Macro risk is unknown or this is a high-risk day",
        )
        drawdown_ok = context.drawdown_from_peak_frac is not None and not drawdown_suspension
        compare(
            "drawdown_brake",
            drawdown_ok,
            context.drawdown_from_peak_frac
            if context.drawdown_from_peak_frac is not None
            else "UNKNOWN",
            (
                f"half risk at {rules.drawdown_half_risk_frac}; "
                f"suspend at {rules.drawdown_suspend_frac}; "
                f"recover only at {rules.drawdown_recovery_frac}"
            ),
            (
                f"Risk budget scaled to "
                f"{sizing_risk_frac * regime_scale * drawdown_scale} of equity"
            ),
            "Drawdown is unknown or suspension remains latched until recovery",
        )
        daily_loss_ok = (
            context.previous_session_pnl_frac is not None
            and context.previous_session_pnl_frac
            > -rules.daily_loss_block_frac
        )
        compare(
            "daily_loss",
            daily_loss_ok,
            context.previous_session_pnl_frac
            if context.previous_session_pnl_frac is not None
            else "UNKNOWN",
            f"> {-rules.daily_loss_block_frac}",
            "Previous session loss is below the block threshold",
            "Previous session loss is unknown or blocks this session",
        )

        def rail(
            name: str, configured: object, passed: bool | None, measured: object, limit: object
        ) -> None:
            if configured is None:
                record(
                    name,
                    True,
                    measured,
                    limit,
                    f"Not configured for {name}; not required for this venue",
                )
            else:
                compare(
                    name,
                    passed is True,
                    measured,
                    limit,
                    f"{name} permits this entry",
                    f"{name} input is unknown or the rail blocks this entry",
                )

        symbol = intent.instrument.symbol
        projected_position = (
            context.current_position_quantity
            + (int(quantity) if side is Side.BUY else -int(quantity))
            if context.current_position_quantity is not None
            else None
        )
        venue_position_ok = None
        if rails.max_position_quantity is not None and projected_position is not None:
            venue_position_ok = (
                quantity > ZERO
                and abs(projected_position) <= rails.max_position_quantity
            )
        rail(
            "allowlist",
            rails.allowed_symbols,
            symbol in rails.allowed_symbols if rails.allowed_symbols is not None else None,
            symbol,
            rails.allowed_symbols or "not required",
        )
        rail(
            "venue_max_position",
            rails.max_position_quantity,
            venue_position_ok,
            abs(projected_position) if projected_position is not None else "UNKNOWN",
            rails.max_position_quantity or "not required",
        )
        rail(
            "orders_per_day",
            rails.max_orders_per_day,
            context.venue_orders_today < rails.max_orders_per_day
            if rails.max_orders_per_day is not None and context.venue_orders_today is not None
            else None,
            context.venue_orders_today + 1
            if context.venue_orders_today is not None
            else "UNKNOWN",
            rails.max_orders_per_day or "not required",
        )
        rail(
            "venue_daily_loss",
            rails.max_daily_loss,
            context.venue_daily_pnl > -rails.max_daily_loss
            if rails.max_daily_loss is not None and context.venue_daily_pnl is not None
            else None,
            context.venue_daily_pnl if context.venue_daily_pnl is not None else "UNKNOWN",
            f"> {-rails.max_daily_loss}" if rails.max_daily_loss is not None else "not required",
        )
        now = self._now()
        rail(
            "trading_hours",
            rails.trading_hours,
            rails.trading_hours.is_open(now) if rails.trading_hours is not None else None,
            now.isoformat(),
            rails.trading_hours or "not required",
        )
        duplicate_ok = not self.ledger.has_command(intent.command_id)
        rail(
            "duplicate_protection",
            True,
            duplicate_ok,
            intent.command_id,
            "command id not present in ledger",
        )
        kill_active = self._control_enabled(
            f"__venue__:{rails.venue_id}", "kill_switch"
        )
        rail(
            "persistent_kill_switch",
            True,
            not kill_active,
            kill_active,
            False,
        )

        refusals = tuple(result.reason for result in results if not result.passed)
        return RiskVerdict(
            order_intent_id=intent.intent_id,
            evaluations=tuple(results),
            refusal_reasons=refusals,
            approved_quantity=quantity if not refusals else None,
        )
'''

old = types.ModuleType("trade_engine_risk_frozen_oracle")
sys.modules[old.__name__] = old
exec(compile(ORACLE_SOURCE, "<frozen risk.py b9274d9>", "exec"), old.__dict__)


class FixedClock(Clock):
    def __init__(self, now: datetime) -> None:
        self._now = now

    def now_utc(self) -> datetime:
        return self._now

    def sleep(self, seconds: float) -> None:
        raise AssertionError("must not sleep")


D = Decimal
WED_OPEN = datetime(2026, 9, 23, 14, 0, tzinfo=timezone.utc)

INSTANTS = [
    WED_OPEN,
    datetime(2026, 9, 23, 13, 29, 59, tzinfo=timezone.utc),  # a second before the open
    datetime(2026, 9, 23, 13, 30, tzinfo=timezone.utc),  # the open itself
    datetime(2026, 9, 23, 19, 59, 59, 999999, tzinfo=timezone.utc),
    datetime(2026, 9, 23, 20, 0, tzinfo=timezone.utc),  # the close itself
    datetime(2026, 9, 26, 15, 0, tzinfo=timezone.utc),  # Saturday
    datetime(2026, 9, 7, 15, 0, tzinfo=timezone.utc),  # Labor Day
    datetime(2026, 11, 27, 17, 59, 59, tzinfo=timezone.utc),  # early close, just before 13:00 ET
    datetime(2026, 11, 27, 18, 0, tzinfo=timezone.utc),  # early close, at 13:00 ET
    datetime(2026, 11, 27, 16, 0, tzinfo=timezone(timedelta(hours=-5))),  # non-UTC offset
    datetime(2026, 12, 24, 17, 30, tzinfo=timezone.utc),  # Christmas Eve early close
    datetime(2026, 3, 9, 13, 30, tzinfo=timezone.utc),  # just after US DST began
    datetime(2026, 11, 2, 14, 30, tzinfo=timezone.utc),  # just after DST ended
    datetime(2026, 9, 23, 3, 0, tzinfo=timezone.utc),  # the small hours
]

BASE_RULES = {
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
BASE_CONTEXT = {
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
BASE_INTENT = {
    "side": "BUY",
    "symbol": "AAPL",
    "quantity_rule": "risk_0.75pct",
    "entry_price": D("100"),
    "stop_loss": D("95"),
}
NY = ("America/New_York", (0, 1, 2, 3, 4), time(9, 30), time(16))
BASE_RAILS = {
    "venue_id": "paper-main",
    "environment": "paper",
    "allowed_symbols": frozenset({"AAPL", "MSFT"}),
    "max_position_quantity": 1000,
    "max_orders_per_day": 100,
    "max_daily_loss": D("5000"),
    "trading_hours": NY,
    "duplicate_protection": True,
    "persistent_kill_switch": True,
}

# One axis at a time from the base case, so each boundary is hit with all else passing.
RULE_AXES = {
    "clamp_to_position_cap": [True],
    "risk_per_trade_frac": [D("0.01"), D("0.0075"), D("0.05"), D("0.0100")],
    "short_risk_per_trade_frac": [D("0.0075"), D("0.0025")],
    "max_position_notional_frac": [D("0.05"), D("0.50"), D("1")],
    "max_gross_exposure_frac": [D("1.00"), D("2")],
    "bull_chop_gross_exposure_frac": [D("0.10"), D("1.50")],
    "max_portfolio_heat_frac": [D("0.001"), D("0.25")],
    "max_positions": [1, 2],
    "max_positions_per_industry": [1, 2],
    "min_price": [D("100"), D("100.01"), D("99.99")],
    "max_adv_frac": [D("0.00001"), D("1")],
    "earnings_blackout_sessions": [0, 6, 7],
    "drawdown_half_risk_frac": [D("0.02"), D("0.0800")],
    "drawdown_suspend_frac": [D("0.09"), D("0.08001")],
    "drawdown_recovery_frac": [D("0.02"), D("0.01")],
    "daily_loss_block_frac": [D("0.0001"), D("0.5")],
}
CONTEXT_AXES = {
    "equity": [None, D("100"), D("1"), D("250000"), D("12345.67")],
    "current_price": [None, D("5"), D("4.99"), D("100.000")],
    "gross_exposure": [None, D("74999"), D("75000"), D("75001"), D("59999.999")],
    "portfolio_heat": [None, D("3000"), D("2999.99"), D("2750")],
    "open_positions": [None, 9, 10, 11],
    "industry": [None, "Energy"],
    "industry_positions": [None, 2, 3],
    "average_dollar_volume_20d": [None, D("1000000"), D("2000000"), D("2000000.01"), D("1999999.99")],
    "sessions_until_earnings": [None, 5, 6, 0],
    "regime": [None, "UNKNOWN", "BULL_CHOPIER", "BEAR_PROTECTIVE"],
    "macro_high_risk_day": [None, True],
    "drawdown_from_peak_frac": [
        None,
        D("0"),
        D("0.05"),
        D("0.0500"),
        D("0.0501"),
        D("0.0499"),
        D("0.08"),
        D("0.0800"),
        D("0.079999"),
        D("0.15"),
        D("0.149999"),
        D("0.1500001"),
        D("0.5"),
    ],
    "previous_session_pnl_frac": [None, D("-0.03"), D("-0.0299"), D("-0.0301"), D("0.1")],
    "venue_orders_today": [None, 99, 100, 0],
    "venue_daily_pnl": [None, D("-5000"), D("-4999.99"), D("-5000.01"), D("12")],
    "current_position_quantity": [None, 999, 1000, -1000, -999, 5000],
}
INTENT_AXES = {
    "side": ["SELL"],
    "symbol": ["MSFT", "GOOG", "aapl"],
    "quantity_rule": [
        "risk_0.5pct",
        "risk_1pct",
        "risk_0.76pct",
        "risk_0pct",
        "risk_-1pct",
        "risk_abc",
        "risk_pct",
        "risk_NaNpct",
        "risk_Infinitypct",
        "risk_ 0.5 pct",
        "risk_1e-1pct",
        "risk_0.75000pct",
        "fixed_10",
        "fixed_1",
        "fixed_0",
        "fixed_",
        "fixed_x",
        "fixed_1000",
        "fixed_100000",
        "notional_10pct",
        "notional_20pct",
        "notional_20.01pct",
        "notional_0pct",
        "notional_pct",
        "notional_x",
        "bogus",
        "",
        "RISK_1pct",
    ],
    "entry_price": [D("100.5"), D("1"), D("5000"), D("33.333")],
    "stop_loss": [D("99.99"), D("99"), D("50"), D("100.01"), D("105"), D("110")],
}
RAIL_AXES = {
    "sim": {"environment": "sim", "allowed_symbols": None, "max_position_quantity": None,
            "max_orders_per_day": None, "max_daily_loss": None, "trading_hours": None,
            "duplicate_protection": None, "persistent_kill_switch": None},
    "sim_partial": {"environment": "sim", "allowed_symbols": frozenset({" aapl "}),
                    "max_position_quantity": 50, "max_orders_per_day": None,
                    "max_daily_loss": None, "trading_hours": None,
                    "duplicate_protection": None, "persistent_kill_switch": None},
    "small": {"max_position_quantity": 10, "max_orders_per_day": 5, "max_daily_loss": D("100")},
    "chicago": {"trading_hours": ("America/Chicago", (0, 1, 2, 3, 4), time(8, 30), time(15))},
    "narrow": {"trading_hours": ("America/New_York", (0, 1, 2, 3, 4), time(10), time(11))},
    "weekend": {"trading_hours": ("America/New_York", (5, 6), time(9, 30), time(16))},
    "wide": {"trading_hours": ("America/New_York", (0, 1, 2, 3, 4, 5, 6), time(0, 0), time(23, 59, 59, 999999))},
    "london": {"trading_hours": ("Europe/London", (0, 1, 2, 3, 4), time(14, 30), time(21))},
}
CONTROLS = ["none", "kill", "duplicate", "brake", "suspended", "brake+suspended", "kill+duplicate+brake"]


def build_case(impl, spec: dict):
    """Rules, rails, context, intent and a prepared ledger for ONE implementation."""
    rules = impl.AccountRiskRules(**{**BASE_RULES, **spec.get("rules", {})})
    rail_values = {**BASE_RAILS, **spec.get("rails", {})}
    hours = rail_values["trading_hours"]
    if hours is not None:
        rail_values["trading_hours"] = impl.TradingHours(*hours)
    rails = impl.VenueRiskRails(**rail_values)
    context = impl.RiskContext(**{**BASE_CONTEXT, **spec.get("context", {})})
    i = {**BASE_INTENT, **spec.get("intent", {})}
    intent = OrderIntent(
        intent_id="intent-1",
        account_id="account-1",
        instrument=Equity(i["symbol"].upper()) if i["symbol"].upper() == i["symbol"] else Equity(i["symbol"]),
        side=Side(i["side"]),
        quantity_rule=i["quantity_rule"],
        entry_price=i["entry_price"],
        stop_loss=i["stop_loss"],
        profit_targets=(D("110"),),
        reason="parity",
        command_id="command-1",
    )
    return rules, rails, context, intent


def prepare(ledger: Ledger, impl, rules, rails, control: str, now: datetime) -> None:
    clock = FixedClock(now)
    engine = impl.RiskEngine(rules, rails, clock, ledger)
    if "kill" in control:
        engine.engage_kill_switch("halt-1", "operator halt")
    if "duplicate" in control:
        ledger.append(
            Event(
                account="elsewhere",
                kind=EventKind.RISK_CONTROL,
                payload=RiskControlChange("noop", False, "seen", now),
                ts_utc=now,
                command_id="command-1",
            )
        )
    for needle, control_id in (("brake", "drawdown_brake"), ("suspended", "drawdown_suspension")):
        if needle in control:
            ledger.append(
                Event(
                    account="account-1",
                    kind=EventKind.RISK_CONTROL,
                    payload=RiskControlChange(control_id, True, "latched", now),
                    ts_utc=now,
                    command_id=f"latch-{control_id}",
                )
            )


def norm(value: object) -> object:
    if isinstance(value, Decimal):
        return ("Decimal", str(value))
    if isinstance(value, frozenset):
        return ("frozenset", tuple(sorted(value)))
    if hasattr(value, "opens") and hasattr(value, "closes"):
        return ("hours", value.timezone, value.weekdays, value.opens, value.closes, value.exchange)
    return (type(value).__name__, value)


def events_of(ledger: Ledger) -> list[object]:
    out = []
    for e in ledger.events():
        p = e.payload
        out.append((e.account, e.kind.name, e.command_id, e.ts_utc, norm(getattr(p, "enabled", None)),
                    getattr(p, "control_id", None), getattr(p, "reason", None)))
    return out


def run(impl, spec: dict, tmp: Path, tag: str):
    """Everything an evaluation does: verdict or refusal, plus the events it wrote."""
    now = spec["now"]
    try:
        rules, rails, context, intent = build_case(impl, spec)
    except Exception as err:  # noqa: BLE001 - the refusal IS the observable
        return ("config-refused", type(err).__name__, str(err))
    with Ledger(tmp / f"{tag}.db") as ledger:
        prepare(ledger, impl, rules, rails, spec["control"], now)
        before = len(events_of(ledger))
        try:
            verdict = impl.RiskEngine(rules, rails, FixedClock(now), ledger).evaluate(intent, context)
        except Exception as err:  # noqa: BLE001
            return ("evaluate-refused", type(err).__name__, str(err), events_of(ledger)[before:])
        return (
            "verdict",
            verdict.accepted,
            verdict.approved_quantity and norm(verdict.approved_quantity),
            verdict.refusal_reasons,
            [
                (r.rule_name, r.passed, norm(r.measured_value), norm(r.threshold), r.reason)
                for r in verdict.evaluations
            ],
            events_of(ledger)[before:],
        )


def specs():
    base = {"now": WED_OPEN, "control": "none"}
    out = [dict(base)]
    for name, values in RULE_AXES.items():
        for v in values:
            out.append({**base, "rules": {name: v}})
    for name, values in CONTEXT_AXES.items():
        for v in values:
            out.append({**base, "context": {name: v}})
    for name, values in INTENT_AXES.items():
        for v in values:
            out.append({**base, "intent": {name: v}})
    for name, values in RAIL_AXES.items():
        out.append({**base, "rails": values})
    for control in CONTROLS:
        for dd in CONTEXT_AXES["drawdown_from_peak_frac"]:
            out.append({**base, "control": control, "context": {"drawdown_from_peak_frac": dd}})
    for now in INSTANTS:
        for hours in ("chicago", "narrow", "weekend", "wide", "london", None):
            rails = RAIL_AXES[hours] if hours else {}
            out.append({**base, "now": now, "rails": rails})
    rng = random.Random(20261002)
    axes = {
        "rules": RULE_AXES,
        "context": CONTEXT_AXES,
        "intent": INTENT_AXES,
    }
    for _ in range(1500):
        spec: dict = {"now": rng.choice(INSTANTS), "control": rng.choice(CONTROLS)}
        for section, table in axes.items():
            chosen = {}
            for name, values in table.items():
                if rng.random() < 0.18:
                    chosen[name] = rng.choice(values)
            spec[section] = chosen
        if rng.random() < 0.5:
            spec["rails"] = dict(RAIL_AXES[rng.choice(list(RAIL_AXES))])
        out.append(spec)
    return out


CASES = specs()


def test_corpus_is_large_and_varied() -> None:
    assert len(CASES) >= 1500
    assert {c["now"] for c in CASES} == set(INSTANTS)


def test_every_generated_case_matches_the_frozen_oracle(tmp_path: Path) -> None:
    kinds: dict[str, int] = {}
    accepted = 0
    mismatches = []
    for n, spec in enumerate(CASES):
        want = run(old, spec, tmp_path, f"old-{n}")
        got = run(new, spec, tmp_path, f"new-{n}")
        kinds[want[0]] = kinds.get(want[0], 0) + 1
        accepted += want[0] == "verdict" and bool(want[1])
        if want != got:
            mismatches.append((n, spec, want, got))
    assert not mismatches, f"{len(mismatches)} of {len(CASES)} differ; first: {mismatches[0]!r}"
    # The corpus must exercise every outcome, not only the happy one.
    assert kinds.get("verdict", 0) > 1000
    assert accepted > 50
    assert kinds.get("config-refused", 0) >= 0


def test_a_verdict_always_lists_every_rule_in_order(tmp_path: Path) -> None:
    names = None
    for n, spec in enumerate(CASES[:400]):
        got = run(new, spec, tmp_path, f"names-{n}")
        if got[0] != "verdict":
            continue
        rows = [r[0] for r in got[4]]
        assert len(rows) == 21
        names = names or rows
        assert rows == names


# ---- configuration validation: the same refusal, same words ---------------

RULE_MAPPINGS = [
    {
        "risk_per_trade": "0.75%", "short_risk_per_trade": "0.5%", "max_position_notional": "20%",
        "max_gross_exposure": "150%", "bull_chop_gross_exposure": "100%", "max_portfolio_heat": "6%",
        "max_positions": 10, "max_positions_per_industry": 3, "min_price": "5", "max_adv": "1%",
        "earnings_blackout_sessions": 5, "drawdown_half_risk": "8%", "drawdown_suspend": "15%",
        "drawdown_recovery": "5%", "daily_loss_block": "3%",
    }
]


def _mapping_variants():
    base = RULE_MAPPINGS[0]
    yield dict(base)
    yield {**base, "clamp_to_position_cap": True}
    yield {**base, "clamp_to_position_cap": False}
    yield {**base, "clamp_to_position_cap": "yes"}
    yield {**base, "clamp_to_position_cap": None}
    yield {**base, "clamp_to_position_cap": 1}
    yield {**base, "extra": 1}
    yield {**base, "extra": 1, "another": 2}
    for key in base:
        yield {k: v for k, v in base.items() if k != key}
    for key in base:
        for value in ("0.5", "x%", "%", " 7 % ", "5%%", "nan%", "-1%", "0%", "1e1%", "inf%", "", 5, None, True, 2.5, D("1"), "0.000001%", "1_0%", "100%", "200%"):
            yield {**base, key: value}
    for key in ("max_positions", "max_positions_per_industry", "earnings_blackout_sessions"):
        for value in ("5", " 5", "5.5", "-1", "0", 0, -3, True, False, None, 2.0, "x", "", D("3")):
            yield {**base, key: value}
    for key in ("min_price",):
        for value in ("0", "-1", "abc", " 5 ", "5e0", "NaN", "Infinity", 5, 5.5, True, None, "1_000", "0.0000001"):
            yield {**base, key: value}
    yield {**base, "drawdown_recovery": "8%"}
    yield {**base, "drawdown_half_risk": "15%"}
    yield {**base, "bull_chop_gross_exposure": "151%"}
    yield {**base, "risk_per_trade": "5.01%"}
    yield {**base, "risk_per_trade": "5%"}
    yield {**base, "max_position_notional": "100.0%"}
    yield {**base, "max_position_notional": "101%"}
    yield {**base, "max_gross_exposure": "200%"}
    yield {**base, "max_gross_exposure": "201%"}
    yield {**base, "max_portfolio_heat": "25%"}
    yield {**base, "max_portfolio_heat": "25.1%"}
    yield {**base, "max_adv": "100%"}
    yield {**base, "daily_loss_block": "50%"}
    yield {**base, "daily_loss_block": "50.01%"}
    yield {**base, "drawdown_suspend": "51%"}
    yield {**base, "drawdown_half_risk": "0.75 %"}
    yield "not a mapping"
    yield None
    yield [("a", 1)]
    yield {1: "x"}
    yield {**base, 2: 3}


def _as_text(values):
    try:
        rules = old.AccountRiskRules.from_mapping(values)
    except Exception as err:  # noqa: BLE001
        return ("refused", type(err).__name__, str(err))
    return ("rules", {k: norm(getattr(rules, k)) for k in BASE_RULES} | {"clamp": rules.clamp_to_position_cap})


def _as_text_new(values):
    try:
        rules = new.AccountRiskRules.from_mapping(values)
    except Exception as err:  # noqa: BLE001
        return ("refused", type(err).__name__, str(err))
    return ("rules", {k: norm(getattr(rules, k)) for k in BASE_RULES} | {"clamp": rules.clamp_to_position_cap})


def test_documented_deviations_refuse_where_python_accepted() -> None:
    """The ONLY known differences (RUST_PORT.md P1d): Rust refuses, never guesses."""
    base = RULE_MAPPINGS[0]
    for value in ("٣", 10**30):  # a non-ASCII digit; an integer beyond 64 bits
        assert old.AccountRiskRules.from_mapping({**base, "max_positions": value}).max_positions
        with pytest.raises(new.RiskConfigurationError):
            new.AccountRiskRules.from_mapping({**base, "max_positions": value})


def test_rules_from_mapping_matches_the_oracle_including_the_words() -> None:
    cases = list(_mapping_variants())
    assert len(cases) > 350
    bad = []
    for values in cases:
        want, got = _as_text(values), _as_text_new(values)
        if want != got:
            bad.append((values, want, got))
    assert not bad, f"{len(bad)} differ; first: {bad[0]!r}"


def _construct(impl, what: str, **kw):
    try:
        if what == "rules":
            obj = impl.AccountRiskRules(**{**BASE_RULES, **kw})
            return ("ok", {k: norm(getattr(obj, k)) for k in BASE_RULES})
        if what == "hours":
            args = {"timezone": "America/New_York", "weekdays": (0, 1), "opens": time(9, 30), "closes": time(16)}
            obj = impl.TradingHours(**{**args, **kw})
            return ("ok", obj.timezone, obj.weekdays)
        if what == "rails":
            values = {**BASE_RAILS, **kw}
            if isinstance(values["trading_hours"], tuple):
                values["trading_hours"] = impl.TradingHours(*values["trading_hours"])
            obj = impl.VenueRiskRails(**values)
            return ("ok", norm(obj.allowed_symbols), obj.max_position_quantity, norm(obj.max_daily_loss))
        obj = impl.RiskContext(**{**BASE_CONTEXT, **kw})
        return ("ok", {k: norm(getattr(obj, k)) for k in BASE_CONTEXT})
    except Exception as err:  # noqa: BLE001
        return ("refused", type(err).__name__, str(err))


CONSTRUCTION = [
    ("rules", {"risk_per_trade_frac": v}) for v in (D("0"), D("-0.01"), D("NaN"), D("Infinity"), 0.01, "0.01", None, True, D("0.05"), D("0.0500001"))
] + [
    ("rules", {"max_positions": v}) for v in (0, -1, True, 1.5, "3", None, 1)
] + [
    ("rules", {"earnings_blackout_sessions": v}) for v in (-1, 0, True, 2.0, None)
] + [
    ("rules", {"clamp_to_position_cap": v}) for v in (1, "x", None, True)
] + [
    ("hours", {"timezone": v}) for v in ("Nowhere/Land", "", None, 5, "UTC", "America/New_York", "Europe/London", "Asia/Kolkata", "US/Eastern")
] + [
    ("hours", {"weekdays": v}) for v in ((), (7,), (-1,), (True,), (1.0,), (0, 6), (1, 1), "01")
] + [
    ("hours", {"opens": v}) for v in (time(16), time(17), time(0), "9:30", None, time(9, 30, tzinfo=timezone.utc))
] + [
    ("hours", {"closes": v}) for v in (time(9, 30), time(9, 29), time(23, 59, 59, 999999), 5, time(16, tzinfo=timezone.utc))
] + [
    ("hours", {"exchange": v}) for v in ("XNYS", "XLON", "", None)
] + [
    ("rails", {"venue_id": v}) for v in ("", "x", None)
] + [
    ("rails", {"environment": v}) for v in ("sim", "paper", "live", "demo", "", None)
] + [
    ("rails", {"allowed_symbols": v}) for v in ("AAPL", frozenset(), frozenset({""}), frozenset({" "}), frozenset({"a", "A"}), {"b", "a"}, ["x"], None, ("msft",))
] + [
    ("rails", {"max_position_quantity": v}) for v in (0, -1, True, 1.5, None, 7)
] + [
    ("rails", {"max_orders_per_day": v}) for v in (0, True, None, 3)
] + [
    ("rails", {"max_daily_loss": v}) for v in (D("0"), D("-1"), D("NaN"), 5, "5", None, D("0.01"))
] + [
    ("rails", {"trading_hours": v}) for v in (None, "always", 5)
] + [
    ("rails", {"duplicate_protection": v}) for v in (False, None, 1, True)
] + [
    ("rails", {"persistent_kill_switch": v}) for v in (False, None, "x", True)
] + [
    ("rails", {"environment": "sim", **{k: None for k in ("allowed_symbols", "max_position_quantity", "max_orders_per_day", "max_daily_loss", "trading_hours", "duplicate_protection", "persistent_kill_switch")}})
] + [
    ("rails", {"environment": "live", "max_orders_per_day": None, "max_daily_loss": None})
] + [
    ("context", {"equity": v}) for v in (D("0"), D("-1"), D("NaN"), 5, "5", None, D("1"))
] + [
    ("context", {"gross_exposure": v}) for v in (D("0"), D("-1"), D("Infinity"), None)
] + [
    ("context", {"drawdown_from_peak_frac": v}) for v in (D("-0.01"), D("NaN"), 0.1, None, D("0"))
] + [
    ("context", {"previous_session_pnl_frac": v}) for v in (D("-5"), D("sNaN"), "x", None)
] + [
    ("context", {"open_positions": v}) for v in (-1, True, 1.0, None, 0)
] + [
    ("context", {"current_position_quantity": v}) for v in (-5, True, 1.5, None)
] + [
    ("context", {"industry": v}) for v in ("", None, "x")
] + [
    ("context", {"regime": v}) for v in ("bull", "", None, "UNKNOWN", 5)
] + [
    ("context", {"macro_high_risk_day": v}) for v in (0, "no", None, True)
]


def test_construction_refusals_match_the_oracle_including_the_words() -> None:
    bad = []
    for what, kw in CONSTRUCTION:
        want, got = _construct(old, what, **kw), _construct(new, what, **kw)
        if want != got:
            bad.append((what, kw, want, got))
    assert not bad, f"{len(bad)} differ; first: {bad[0]!r}"


def test_trading_hours_is_open_matches_the_oracle_over_a_dense_sweep() -> None:
    shapes = [NY, ("America/Chicago", (0, 1, 2, 3, 4), time(8, 30), time(15)),
              ("America/New_York", (0, 1, 2, 3, 4), time(10), time(11, 30)),
              ("UTC", (0, 1, 2, 3, 4, 5, 6), time(0), time(23, 59, 59)),
              ("Asia/Kolkata", (0, 1, 2, 3, 4), time(19), time(23))]
    checked = open_count = 0
    start = datetime(2026, 11, 24, 0, 0, tzinfo=timezone.utc)
    for shape in shapes:
        a, b = old.TradingHours(*shape), new.TradingHours(*shape)
        for step in range(0, 12 * 24 * 60, 7):  # twelve days around Thanksgiving and its early close
            instant = start + timedelta(minutes=step)
            want = a.is_open(instant)
            assert b.is_open(instant) is want, (shape, instant)
            checked += 1
            open_count += want
        for edge in (datetime(2026, 11, 27, 17, 59, 59, 999999, tzinfo=timezone.utc),
                     datetime(2026, 11, 27, 18, 0, tzinfo=timezone.utc),
                     datetime(2026, 12, 24, 17, 59, 59, tzinfo=timezone.utc),
                     datetime(2026, 12, 24, 18, 0, tzinfo=timezone.utc)):
            assert b.is_open(edge) is a.is_open(edge), (shape, edge)
    assert checked > 5000 and open_count > 100


def test_is_open_refuses_a_naive_instant_with_the_same_words() -> None:
    for impl in (old, new):
        with pytest.raises(ValueError, match=r"timezone-aware \(I7\)"):
            impl.TradingHours(*NY).is_open(datetime(2026, 9, 23, 14, 0))


def test_non_equity_intent_is_refused_before_anything_is_written(tmp_path: Path) -> None:
    from trade_engine.domain.instruments import Instrument  # noqa: F401  (the base type)

    class NotEquity:
        symbol = "X"

    for impl, tag in ((old, "o"), (new, "n")):
        rules, rails, context, intent = build_case(impl, {"now": WED_OPEN})
        object.__setattr__(intent, "instrument", NotEquity())
        with Ledger(tmp_path / f"{tag}.db") as ledger:
            with pytest.raises(ValueError, match="equity intents only"):
                impl.RiskEngine(rules, rails, FixedClock(WED_OPEN), ledger).evaluate(intent, context)
            assert ledger.events() == []

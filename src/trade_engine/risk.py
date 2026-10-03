"""Account risk evaluation and venue safety rails (Architecture §4.3, E6).

The rules live in Rust (`te_core::risk`, docs/RUST_PORT.md P1d): configuration
validation, the 21-rule evaluation and the drawdown-control decision. This module
reads the ledger, writes the control events and converts to and from the one JSON
document `trade_engine_rs` takes. No risk rule may be written here: that would be a
second reader. Ledger writes stay Python until P2.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal, DivisionByZero
from typing import Any, Literal

import trade_engine_rs as _rs

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


_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_MICROSECOND = timedelta(microseconds=1)


def _tag(value: object) -> object:
    """A Python value as te_core reads it: Decimal -> {"d"}, anything exotic -> text."""
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, int):
        return value if -(2**63) <= value < 2**63 else {"text": str(value), "type": "int"}
    if isinstance(value, Decimal):
        return {"d": str(value)}
    return {"text": str(value), "type": type(value).__name__}


def _text(value: object) -> object:
    """A value that is a string, or the text of whatever it is instead."""
    return value if isinstance(value, str) else {"text": str(value), "type": type(value).__name__}


def _call(fn: Any, document: object) -> Any:
    """Run a te_core entry point; a refusal raises what Python's rules raised."""
    reply = json.loads(fn(json.dumps(document)))
    if "ok" in reply:
        return reply["ok"]
    error = reply["error"]
    if error["kind"] == "config":
        raise RiskConfigurationError(error["message"])
    if "division by zero" in error["message"]:
        raise DivisionByZero(error["message"])
    raise ValueError(error["message"])


_RULE_FIELDS = (
    "risk_per_trade_frac",
    "short_risk_per_trade_frac",
    "max_position_notional_frac",
    "max_gross_exposure_frac",
    "bull_chop_gross_exposure_frac",
    "max_portfolio_heat_frac",
    "max_positions",
    "max_positions_per_industry",
    "min_price",
    "max_adv_frac",
    "earnings_blackout_sessions",
    "drawdown_half_risk_frac",
    "drawdown_suspend_frac",
    "drawdown_recovery_frac",
    "daily_loss_block_frac",
    "clamp_to_position_cap",
)
_CONTEXT_FIELDS = (
    "equity",
    "current_price",
    "gross_exposure",
    "portfolio_heat",
    "open_positions",
    "industry",
    "industry_positions",
    "average_dollar_volume_20d",
    "sessions_until_earnings",
    "regime",
    "macro_high_risk_day",
    "drawdown_from_peak_frac",
    "previous_session_pnl_frac",
    "venue_orders_today",
    "venue_daily_pnl",
    "current_position_quantity",
)


def _time_doc(value: object) -> object:
    if not isinstance(value, time):
        return {"text": str(value), "type": type(value).__name__}
    micros = ((value.hour * 60 + value.minute) * 60 + value.second) * 1_000_000 + value.microsecond
    return {"aware_micros": micros} if value.utcoffset() is not None else {"micros": micros}


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
        is_mapping = isinstance(values, Mapping)
        keys_are_strings = is_mapping and all(isinstance(key, str) for key in values)
        fields = _call(
            _rs.risk_rules_from_mapping,
            {
                "is_mapping": is_mapping,
                "keys_are_strings": keys_are_strings,
                "values": {key: _tag(value) for key, value in values.items()}
                if keys_are_strings
                else {},
            },
        )
        parsed = {
            name: Decimal(value["d"]) if isinstance(value, dict) else value
            for name, value in fields.items()
        }
        return cls(**parsed)  # type: ignore[arg-type]

    def __post_init__(self) -> None:
        _call(_rs.risk_validate_rules, {name: _tag(getattr(self, name)) for name in _RULE_FIELDS})


@dataclass(frozen=True)
class TradingHours:
    """Recurring local-time venue hours. Overnight sessions must use a separate adapter."""

    timezone: str
    weekdays: tuple[int, ...]
    opens: time
    closes: time
    exchange: str = "XNYS"

    def _doc(self) -> dict[str, object]:
        weekdays = self.weekdays
        return {
            "timezone": _text(self.timezone),
            "weekdays": [_tag(day) for day in weekdays]
            if isinstance(weekdays, (tuple, list))
            else None,
            "opens": _time_doc(self.opens),
            "closes": _time_doc(self.closes),
            "exchange": _text(self.exchange),
        }

    def __post_init__(self) -> None:
        _call(_rs.risk_validate_hours, self._doc())

    def is_open(self, instant: datetime) -> bool:
        if instant.tzinfo is None or instant.utcoffset() is None:
            raise ValueError("trading-hours instant must be timezone-aware (I7)")
        return _call(
            _rs.risk_hours_is_open,
            {"hours": self._doc(), "epoch_us": (instant - _EPOCH) // _MICROSECOND},
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

    def _doc(self) -> dict[str, object]:
        symbols = self.allowed_symbols
        if symbols is None:
            allowed: object = None
        elif isinstance(symbols, str):
            allowed = {"is_str": True}
        else:
            allowed = {"items": list(symbols)}
        hours = self.trading_hours
        return {
            "venue_id": _text(self.venue_id) if self.venue_id else None,
            "environment": _text(self.environment),
            "allowed_symbols": allowed,
            "max_position_quantity": _tag(self.max_position_quantity),
            "max_orders_per_day": _tag(self.max_orders_per_day),
            "max_daily_loss": _tag(self.max_daily_loss),
            "trading_hours": {"hours": hours._doc()}
            if isinstance(hours, TradingHours)
            else _tag(hours),
            "duplicate_protection": _tag(self.duplicate_protection),
            "persistent_kill_switch": _tag(self.persistent_kill_switch),
        }

    def __post_init__(self) -> None:
        normalised = _call(_rs.risk_validate_rails, self._doc())["allowed_symbols"]
        if normalised is not None:
            object.__setattr__(self, "allowed_symbols", frozenset(normalised))


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

    def _doc(self) -> dict[str, object]:
        return {name: _tag(getattr(self, name)) for name in _CONTEXT_FIELDS}

    def __post_init__(self) -> None:
        _call(_rs.risk_validate_context, self._doc())


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

    def _rules_doc(self) -> dict[str, object]:
        return {name: _tag(getattr(self.account_rules, name)) for name in _RULE_FIELDS}

    def _update_drawdown_controls(
        self,
        account_id: str,
        drawdown: Decimal | None,
        intent_command_id: str,
        is_equity: bool = True,
    ) -> tuple[bool, bool]:
        state = self.ledger.state(account_id)
        brake_active = state.risk_controls.get("drawdown_brake", False)
        suspension_active = state.risk_controls.get("drawdown_suspension", False)
        decision = _call(
            _rs.risk_drawdown_controls,
            {
                "is_equity": is_equity,
                "rules": self._rules_doc(),
                "drawdown": None if drawdown is None else str(drawdown),
                "brake_active": brake_active,
                "suspension_active": suspension_active,
            },
        )
        for change in decision["changes"]:
            now = self._now()
            self.ledger.append(
                Event(
                    account=account_id,
                    kind=EventKind.RISK_CONTROL,
                    payload=RiskControlChange(
                        change["control_id"], change["enabled"], change["reason"], now
                    ),
                    ts_utc=now,
                    command_id=(
                        f"risk-control:{account_id}:{change['control_id']}:"
                        f"{state.last_seq}:{intent_command_id}"
                    ),
                )
            )
        return decision["brake"], decision["suspension"]

    def evaluate(self, intent: OrderIntent, context: RiskContext) -> RiskVerdict:
        """Evaluate all rules and persist any drawdown-control transitions."""
        is_equity = isinstance(intent.instrument, Equity)
        drawdown_brake, drawdown_suspension = self._update_drawdown_controls(
            intent.account_id, context.drawdown_from_peak_frac, intent.command_id, is_equity
        )
        rails = self.venue_rails
        now = self._now()
        reply = _call(
            _rs.risk_evaluate,
            {
                "rules": self._rules_doc(),
                "rails": rails._doc(),
                "context": context._doc(),
                "intent": {
                    "is_equity": is_equity,
                    "symbol": intent.instrument.symbol if is_equity else "",
                    "side": str(intent.side.value),
                    "quantity_rule": intent.quantity_rule,
                    "entry_price": str(intent.entry_price),
                    "stop_loss": str(intent.stop_loss),
                    "command_id": intent.command_id,
                },
                "state": {
                    "now_us": (now - _EPOCH) // _MICROSECOND,
                    "now_label": now.isoformat(),
                    "command_known": self.ledger.has_command(intent.command_id),
                    "kill_switch": self._control_enabled(
                        f"__venue__:{rails.venue_id}", "kill_switch"
                    ),
                    "drawdown_brake": drawdown_brake,
                    "drawdown_suspension": drawdown_suspension,
                },
            },
        )
        results = tuple(
            RiskRuleResult(
                row["rule"],
                row["passed"],
                self._value(row["measured"]),
                self._value(row["threshold"]),
                row["reason"],
            )
            for row in reply["results"]
        )
        quantity = reply["approved_quantity"]
        return RiskVerdict(
            order_intent_id=intent.intent_id,
            evaluations=results,
            refusal_reasons=tuple(reply["refusals"]),
            approved_quantity=Decimal(quantity) if quantity is not None else None,
        )

    def _value(self, tagged: dict[str, Any]) -> object:
        """Rebuild the typed value a rule measured or compared against."""
        ((kind, value),) = tagged.items()
        if kind == "d":
            return Decimal(value)
        if kind == "i":
            return int(value)
        if kind == "set":
            return frozenset(value)
        if kind == "hours":
            return self.venue_rails.trading_hours
        return value

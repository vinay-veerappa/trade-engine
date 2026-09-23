"""Risk layer models and decision verdict records."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class RiskRuleResult:
    """The result of evaluating a single risk rule against an order intent."""

    rule_name: str
    passed: bool
    measured_value: Any
    threshold: Any
    reason: str

    def __post_init__(self) -> None:
        if not self.rule_name:
            raise ValueError("rule_name must be non-empty")


@dataclass(frozen=True)
class RiskVerdict:
    """Complete risk verdict listing every rule evaluated without short-circuiting (I11)."""

    order_intent_id: str
    accepted: bool
    evaluations: tuple[RiskRuleResult, ...]
    refusal_reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.order_intent_id:
            raise ValueError("order_intent_id must be non-empty")
        if not self.accepted and not self.refusal_reasons:
            raise ValueError("A refused RiskVerdict must include at least one refusal reason")

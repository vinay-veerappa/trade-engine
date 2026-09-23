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
    accepted: bool | None = None
    evaluations: tuple[RiskRuleResult, ...] = ()
    refusal_reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.order_intent_id:
            raise ValueError("order_intent_id must be non-empty")
        if not self.evaluations:
            raise ValueError("RiskVerdict must contain at least one evaluation (I11)")

        has_failed_evals = any(not e.passed for e in self.evaluations)
        derived_accepted = (not has_failed_evals) and len(self.refusal_reasons) == 0

        if self.accepted is not None and self.accepted != derived_accepted:
            raise ValueError(
                f"Contradictory RiskVerdict: accepted={self.accepted} but evaluations "
                f"(failed_rules={has_failed_evals}, refusal_reasons={len(self.refusal_reasons)}) "
                f"evaluates to accepted={derived_accepted} (I11)"
            )

        if not derived_accepted and not self.refusal_reasons:
            raise ValueError("A refused RiskVerdict must include at least one refusal reason")

        object.__setattr__(self, "accepted", derived_accepted)

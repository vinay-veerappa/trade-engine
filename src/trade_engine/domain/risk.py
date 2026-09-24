"""Risk layer models and decision verdict records."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
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
    approved_quantity: Decimal | None = None

    def __post_init__(self) -> None:
        if not self.order_intent_id:
            raise ValueError("order_intent_id must be non-empty")
        if self.approved_quantity is not None and (
            not isinstance(self.approved_quantity, Decimal)
            or not self.approved_quantity.is_finite()
            or self.approved_quantity <= 0
        ):
            raise ValueError("approved_quantity must be a positive finite Decimal")
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
        if not derived_accepted and self.approved_quantity is not None:
            raise ValueError("A refused RiskVerdict cannot include an approved quantity")

        object.__setattr__(self, "accepted", derived_accepted)


@dataclass(frozen=True)
class RiskControlChange:
    """Append-only change to a venue or account risk-control latch."""

    control_id: str
    enabled: bool
    reason: str
    changed_at: datetime

    def __post_init__(self) -> None:
        if not self.control_id:
            raise ValueError("control_id must be non-empty")
        if not isinstance(self.enabled, bool):
            raise ValueError("enabled must be a boolean")
        if not self.reason:
            raise ValueError("reason must be non-empty")
        if self.changed_at.tzinfo is None or self.changed_at.utcoffset() is None:
            raise ValueError("changed_at must be timezone-aware (I7)")

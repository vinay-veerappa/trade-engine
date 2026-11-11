"""The refusal a `te_core::accounts` rule raises (I5).

An unknown key, an empty or whitespace account id, a display that is not the mask, a
snapshot over the cap, an interval under 30 s or a stale_after under zero is refused
with a `kind`; this class carries the message and keeps the kind visible as its first
argument, exactly as the other ported rules do.
"""

from __future__ import annotations


class AccountRuleError(ValueError):
    """A refused account rule input (the design's I5 refusal, never an approximation)."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(kind, message)
        self.kind = kind
        self.message = message

    def __str__(self) -> str:
        return self.message


__all__ = ["AccountRuleError"]

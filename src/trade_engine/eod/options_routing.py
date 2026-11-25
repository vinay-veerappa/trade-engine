"""How a strategy's option actions reach the venue — shared by both runners (O4, I13).

Extracted from the EOD runner so the intraday service routes its actions through the
same rules instead of a second implementation drifting away (I13): the underlying
check at a snapshot, risk verdicts recorded under ``risk:<command_id>``, entries
through the options OMS, closes through ``OptionOrderManager``.
"""

from __future__ import annotations

from dataclasses import dataclass

from trade_engine.sim._rs import rs


@dataclass
class RoutingTally:
    """The per-pass counters both runners accumulate."""

    orders_submitted: int = 0
    exit_actions: int = 0
    snapshots_processed: int = 0

    def __add__(self, other: "RoutingTally") -> "RoutingTally":
        return RoutingTally(
            self.orders_submitted + other.orders_submitted,
            self.exit_actions + other.exit_actions,
            self.snapshots_processed + other.snapshots_processed,
        )


def _fail(message: str) -> Exception:
    """The runners' refusal type: both runners' tests and hosts catch it."""
    from trade_engine.eod.runner import EodRunnerError

    return EodRunnerError(message)


class OptionRouter(rs.OptionRouterBase):
    """Match, manage and route one options account against chain snapshots."""

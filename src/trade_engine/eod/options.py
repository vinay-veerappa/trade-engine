"""What an options strategy is shown during the EOD run (O4, I13).

An options account's strategy is called twice a session, each time with an
``OptionContext``:

- ``phase == "snapshot"``: at a chain snapshot's own instant (the 15:45 ET pull), after the
  orders working on that underlying have been matched against it. ``manage_options`` may
  return closes, share sales and new entries (re-selling a covered call, say). They are
  matched against the same snapshot at once, which is what a trader acting on the 15:45
  quotes would get.
- ``phase == "close"``: after the close, once expiry, assignment and dividends are booked
  and the positions are marked. ``manage_options`` decides the rules that need the
  official close (an underlying closing through a strike or an average), and
  ``generate_intents`` turns the session's signals into entries. Both work at the next
  session's snapshot.

The context holds the folded state and the snapshots only. Nothing in it can reach a
broker.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from types import MappingProxyType
from typing import Any, Literal

from trade_engine.domain.option_orders import OpenStructure
from trade_engine.ledger.state import AccountState
from trade_engine.market_data.chains import ChainSnapshot


@dataclass(frozen=True)
class OptionContext:
    session: date
    account_id: str
    phase: Literal["snapshot", "close"]
    now: datetime
    state: AccountState
    structures: tuple[OpenStructure, ...]
    snapshot: ChainSnapshot | None = None  # the snapshot just matched ("snapshot" phase)
    # The newest snapshot matched this session, per underlying.
    snapshots: Mapping[str, ChainSnapshot] = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        if self.phase not in ("snapshot", "close"):
            raise ValueError(f"phase must be 'snapshot' or 'close', got {self.phase!r}")
        if (self.phase == "snapshot") != (self.snapshot is not None):
            raise ValueError("A snapshot-phase context carries its snapshot; a close-phase one does not")
        if not isinstance(self.snapshots, MappingProxyType):
            object.__setattr__(self, "snapshots", MappingProxyType(dict(self.snapshots)))

    def get(self, key: str, default: Any = None) -> Any:
        """Dict-style access to ``session`` and ``account_id``, as equity strategies read them."""
        return getattr(self, key, default) if key in ("session", "account_id", "phase") else default

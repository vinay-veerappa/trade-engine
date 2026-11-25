"""Restore an in-memory venue's book from the ledger fold (I2, shared by both runners).

A new process starts with an empty venue while the ledger holds every working order,
fill and position. ``restorable`` hands those back; anything inconsistent refuses
rather than being patched up (I5).

A ``PENDING_UNKNOWN`` order is one whose venue answer never reached the ledger: the
process died between asking the venue and recording what it said. The EOD runner
refuses it (``pending="refuse"``). The intraday service cannot wait for an operator
mid-session, so it asks for ``pending="resolve"``: the in-memory venue that held the
order died with the process, and the rebuilt venue's book is whatever the ledger says,
so the request the ledger recorded is carried out on the new venue —

- a pending *submit* is restored as working (the order was sent; nothing filled it,
  since a fill is recorded at the snapshot that made it, before any later request);
- a pending *cancel* is restored as cancelled (the cancel was asked of a working order,
  which this venue always honours);

and the caller reads each one back through the OMS (``reconcile_order``), so the ledger
records the resolution as a venue read-back. Any other pending request is left out and
reported, and the caller refuses what depends on it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from trade_engine.domain.orders import OrderState
from trade_engine.interfaces.broker import VenueFill, VenueOrder, VenuePosition
from trade_engine.ledger.state import AccountState
from trade_engine.sim import _rs

MIN_TIME = datetime.min.replace(tzinfo=timezone.utc)
# States in which the ledger says the venue holds the order.
VENUE_WORKING = frozenset(
    {
        OrderState.SUBMITTED,
        OrderState.ACCEPTED,
        OrderState.PARTIALLY_FILLED,
        OrderState.PENDING_UNKNOWN,
    }
)


class RestoreError(RuntimeError):
    """The ledger cannot rebuild the venue's book (I5)."""


_rs.register("restore", RestoreError)


@dataclass
class PendingResolution:
    """What ``pending="resolve"`` did with the ledger's PENDING_UNKNOWN orders."""

    resolved: list[str] = field(default_factory=list)  # read these back after restore
    unresolved: dict[str, str] = field(default_factory=dict)  # order id -> why not


def pending_request(ledger: Any, account_id: str, order_id: str) -> str:
    """Which request left ``order_id`` pending: ``submit``, ``cancel`` or ``unknown``."""
    return _rs.call(_rs.rs.pending_request, ledger, account_id, order_id)


def restorable(
    ledger: Any,
    account_id: str,
    state: AccountState,
    *,
    pending: str = "refuse",
    resolution: PendingResolution | None = None,
) -> tuple[list[tuple[VenueOrder, Any]], list[VenueFill]]:
    """Working orders plus their brackets: a child's cap needs its parent's fills
    and its siblings' exits. NEW orders were never sent, so they stay out.

    Which orders, fills and refusals is Rust's (``te_core::oms::restore``, P3b-1); the
    ledger's events and the submission events are read natively, and the venue carriers
    are built by Rust. A ``resolve`` resolution is reported back as ``(resolved,
    unresolved)`` and applied here.
    """
    return _rs.call(_rs.rs.restorable, ledger, account_id, state, pending, resolution)


def restorable_positions(state: AccountState) -> list[VenuePosition]:
    """The venue's held positions, in state order, each dated by its latest fill."""
    return _rs.call(_rs.rs.restorable_positions, state)

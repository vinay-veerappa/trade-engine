"""The IBKR accounts adapter over the phase 2 port (BROKER_PLATFORM phase 6b).

READ-ONLY, and **off by default**: nothing here connects to TWS or IB Gateway, and no
socket or HTTP client is imported (I7, D3). This is the I/O shell stub: the caller hands
one read as text through :meth:`feed`, and the rules (both source shapes, the key, the
mask, the paper prefix, the cap) are ``te_core::ibkr``'s.

Rules it carries:

- the switch: ``enabled`` is false unless the caller turns it on; a read while off is
  refused with ``ibkr_disabled`` and no snapshot is invented;
- a fed read is at most 8 MiB (``MAX_TEXT_BYTES``); over it the read is refused and
  counted, never buffered without bound (B7);
- a refused read keeps the last good snapshot and emits nothing (B3); one last-good
  snapshot is kept, replaced on each good read;
- ``DU...`` accounts are paper and the rest are live (the Rust rule, never parsed here);
- the order methods refuse with ``UnsupportedCapability``: no order path exists in this
  slice (B6).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from trade_engine.accounts import _rs as _accounts_rs
from trade_engine.accounts.errors import AccountRuleError
from trade_engine.accounts.poll import PollAndDiff
from trade_engine.interfaces.broker import (
    AccountRecord,
    AccountSnapshot,
    Capabilities,
    Route,
    UnsupportedCapability,
)

log = logging.getLogger(__name__)

MAX_TEXT_BYTES = 8 * 1024 * 1024
_READ_ONLY = "this adapter is read-only: it carries no orders"
_DISABLED = "the IBKR accounts adapter is off; its switch defaults to off"


@dataclass
class AdapterStats:
    """Counters: nothing the adapter drops or refuses is silent."""

    reads: int = 0
    read_failures: int = 0
    refused_snapshots: int = 0
    over_cap_refused: int = 0
    disabled_reads: int = 0
    fed: int = 0
    fed_bytes: int = 0


def _snapshot(data: dict) -> AccountSnapshot:
    """The Rust snapshot's contract form as the Python carriers (no rules here)."""
    records = tuple(
        AccountRecord(
            key=row["key"],
            display=row["display"],
            env=row["env"],
            status=row["status"],
            class_=row["class"],
            firm=row["firm"],
            account_type=row["account_type"],
            api_ref=None,
            routes=tuple(Route(route["platform"], route["local_name"]) for route in row["routes"]),
            source=row["source"],
            confirmed_at=datetime.fromisoformat(row["confirmed_at"].replace("Z", "+00:00")),
        )
        for row in data["records"]
    )
    confirmed_at = datetime.fromisoformat(data["confirmed_at"].replace("Z", "+00:00"))
    return AccountSnapshot(records=records, confirmed_at=confirmed_at)


class IbkrAccountsAdapter:
    """The IBKR account port's I/O shell. Read-only and off unless switched on."""

    name = "ibkr"
    env = "paper"
    stale_after = 120

    def __init__(self, *, clock, enabled: bool = False, ledger=None, on_poll=None) -> None:
        self._clock = clock
        self._enabled = bool(enabled)
        self._ledger = ledger
        self._on_poll = on_poll
        self._text: str | None = None
        self._last: AccountSnapshot | None = None
        self._poller: PollAndDiff | None = None
        self.stats = AdapterStats()
        # The broker protocol's capabilities: no order types, nothing to send.
        self.capabilities = Capabilities(
            supported_order_types=frozenset(),
            supported_tifs=frozenset(),
            supports_multi_leg=False,
            supports_native_stops=False,
            supports_streaming=False,
        )

    # -- the switch -----------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        """The switch, off by default."""
        return self._enabled

    def set_enabled(self, on: bool) -> None:
        """Turn the adapter on or off; no read happens here."""
        self._enabled = bool(on)

    # -- the I/O shell stub ---------------------------------------------------------

    def feed(self, text: str) -> None:
        """Hand the adapter one read as text (TWS messages or Client Portal JSON).

        This slice makes no connection: the caller's bridge supplies the text. A read
        over ``MAX_TEXT_BYTES`` is refused and counted.
        """
        if not isinstance(text, str):
            raise AccountRuleError("bad_shape", "an IBKR read is text")
        size = len(text.encode("utf-8"))
        if size > MAX_TEXT_BYTES:
            self.stats.over_cap_refused += 1
            raise AccountRuleError(
                "cap_exceeded",
                f"the IBKR read is {size} bytes, over the {MAX_TEXT_BYTES}-byte cap (B7)",
            )
        self._text = text
        self.stats.fed += 1
        self.stats.fed_bytes = size

    # -- the account port -----------------------------------------------------------

    def accounts(self) -> AccountSnapshot:
        """Parse the fed read through Rust; a refused read keeps the last good list (B3)."""
        self.stats.reads += 1
        if not self._enabled:
            self.stats.disabled_reads += 1
            raise AccountRuleError("ibkr_disabled", _DISABLED)
        if self._text is None:
            self.stats.read_failures += 1
            raise AccountRuleError(
                "bad_shape", "no IBKR read has been fed; this slice connects to nothing"
            )
        try:
            snapshot = _snapshot(_accounts_rs.ibkr_snapshot(self._text, self._clock.now_utc()))
        except AccountRuleError as exc:
            self.stats.refused_snapshots += 1
            if exc.kind == "cap_exceeded":
                self.stats.over_cap_refused += 1
            log.warning("IBKR read refused (%s); keeping the last good list", exc.kind)
            if self._last is not None:
                return self._last
            raise
        self._last = snapshot
        return snapshot

    def account_events(self, since: datetime):
        """The poll helper's diff over :meth:`accounts`; membership diffs per read."""
        return self.poller.account_events(since)

    @property
    def poller(self) -> PollAndDiff:
        if self._poller is None:
            self._poller = PollAndDiff(
                self,
                clock=self._clock,
                ledger=self._ledger,
                stale_after=self.stale_after,
                on_poll=self._on_poll,
            )
        return self._poller

    def is_stale(self, now: datetime) -> bool:
        """True before the first good read and from ``stale_after`` on (B4)."""
        return self._last is None or self._last.is_stale(now, self.stale_after)

    # -- the protocol's remaining reads and the refused writes ---------------------

    def connect(self):
        """No connection exists in this slice; a later brief owns the TWS client."""
        raise UnsupportedCapability("the IBKR adapter is an account-only stub: it connects to nothing")

    def orders(self, since: datetime) -> list:
        return []

    def fills(self, since: datetime) -> list:
        return []

    def positions(self) -> list:
        return []

    def cash_events(self, since: datetime) -> list:
        return []

    def submit(self, order):
        raise UnsupportedCapability(_READ_ONLY)

    def cancel(self, venue_order_id):
        raise UnsupportedCapability(_READ_ONLY)

    def replace(self, venue_order_id, changes):
        raise UnsupportedCapability(_READ_ONLY)


__all__ = ["AdapterStats", "IbkrAccountsAdapter", "MAX_TEXT_BYTES"]

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

The method bodies now live in ``IbkrAccountsAdapterBase`` (``crates/te_py/src/ibkr_adapter.rs``),
reached through ``trade_engine_rs``; this module keeps only the public class (a thin
subclass), its class attributes, the counters dataclass and the byte cap.
"""

from __future__ import annotations

from dataclasses import dataclass

import trade_engine_rs as rs

from trade_engine.interfaces.broker import Capabilities

MAX_TEXT_BYTES = 8 * 1024 * 1024


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


class IbkrAccountsAdapter(rs.IbkrAccountsAdapterBase):
    """The IBKR account port's I/O shell. Read-only and off unless switched on."""

    name = "ibkr"
    env = "paper"
    stale_after = 120

    # The broker protocol's capabilities: no order types, nothing to send.
    capabilities = Capabilities(
        supported_order_types=frozenset(),
        supported_tifs=frozenset(),
        supports_multi_leg=False,
        supports_native_stops=False,
        supports_streaming=False,
    )

    def __init__(self, *, clock, enabled: bool = False, ledger=None, on_poll=None) -> None:
        # The Rust base owns the method bodies; it reads this instance state back through
        # attribute lookup, so a subclass or monkeypatched attribute keeps working.
        super().__init__()
        self._clock = clock
        self._enabled = bool(enabled)
        self._ledger = ledger
        self._on_poll = on_poll
        self._text = None
        self._last = None
        self._poller = None
        self.stats = AdapterStats()


__all__ = ["AdapterStats", "IbkrAccountsAdapter", "MAX_TEXT_BYTES"]

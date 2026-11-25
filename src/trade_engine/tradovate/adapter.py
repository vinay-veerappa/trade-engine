"""The Tradovate adapter over ntx: accounts, positions, fills (BROKER_PLATFORM phase 3).

Orders (OA-3): `submit`, `cancel` and `replace` go to the ntx daemon through `NtxClient`:
one HTTP call per action, no thread, one INFO log line per action (account masked to its
last 3). A submit needs a protective stop and a riskguard verdict, or it is refused locally
with no call (`no_stop`, `verdict_required`). The clOrdId is the venue order id; an
unanswered call is `PENDING` and is never resent (the next snapshot or event reconciles it).
Exits go through ntx flatten, which is out of scope here. The order-verb gate test allows
the three order paths only and still refuses liquidate and flatten.

Reconcile (OA-5): on connect and on every stream reconnect, one `/snapshot` read rebuilds the
entry -> {stopId, targetId} map for the entries still open in the ledger's folded state. A
bracket is matched by the clOrdId ntx keeps for it (D-5; the snapshot row's `brackets` list),
never by price or quantity. An entry with no match stays unmapped (logged once) and its
children still refuse `unknown_parent`. A PENDING entry is resolved from the same read:
listed means ACCEPTED with its ntx ids, absent means REJECTED `not_at_venue`; it is never
resent. A row without a `brackets` list carries no venue truth, so nothing is mapped or
rejected from it (fail closed). Rebuilt ids get a per-session modify/cancel sequence base
(the clock in ms) so a post-restart clOrdId never replays an earlier one from the ntx store.

It is an I/O shell (D2, D3). Membership is decided by `te_core::accounts` through
`PollAndDiff`; the key, the mask, the staleness and the diff are Rust's. This module maps
what the ntx `/snapshot` carries onto the account carriers and decides nothing about them.

Rules it carries:
- one `VenueIdentity` per ntx identity; the env of a record is the env its identity's
  connect reported, never parsed from the account name; a conflicting or unknown env refuses;
- one record per ntx account, key `tradovate:<ntx name>`, `api_ref` the numeric Tradovate
  id (engine-only, D-13), `source` `ntx`, routes `{nt8, <name>}` and `{tradingview, D<id>}`;
- membership is read from a `/snapshot` at connect, at reconnect and on a limited early
  re-read (an account-entity SSE event is only a hint); never one snapshot per event;
- a failed read (daemon down, 401, 500, bad shape) keeps the last good list with no event
  (B3); the list is stale from 120 s on (B4);
- an identity the daemon reports as degraded keeps its last good records unchanged, so no
  `removed` is emitted for them, and they are reported stale;
- a missing account is reported as `removed`, never closed;
- money crosses as `Decimal` via the shortest `repr`; NaN and infinity refuse;
- an unknown root or month refuses with a `kind` (I5).

No clock is read (I7): the clock, the sleep, the random source and the trading-day
mapping are injected.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

import trade_engine_rs as rs

from trade_engine.domain.orders import OrderState, OrderType, TimeInForce
from trade_engine.interfaces.broker import AccountRecord, Capabilities
from trade_engine.tradovate.ntx_client import NtxClient, ReconnectBackoff, RefreshLimiter

BROKER = "tradovate"
SOURCE = "ntx"
ACCOUNT_CAP = 2_000  # the design's hard cap on broker accounts (6.8a)
# BP-4e: riskguard calls a broker figure stale after this many seconds (pnl_source.rs).
FIGURE_FRESH_SECONDS = 10
_ENVS = ("sim", "paper", "live")
ORDER_ID_CAP = 512  # venue order ids kept for replace/cancel; the oldest is dropped
_MAX_QTY = 1000


@dataclass
class AdapterStats:
    """Counters: nothing the adapter drops or refuses is silent."""

    reads: int = 0
    read_failures: int = 0
    refused_snapshots: int = 0
    over_cap_refused: int = 0
    degraded_carried: int = 0


@dataclass(frozen=True)
class _Account:
    """What one read kept of one ntx account (the raw row, replaced on each read)."""

    record: AccountRecord
    identity: str
    raw: dict[str, Any]


class TradovateAdapter(rs.TradovateAdapterBase):
    """`BrokerAdapter` for the Tradovate accounts ntx reads and orders (OA-3)."""

    name = "tradovate"
    env = "paper"
    stale_after = 120
    # OA-3b: entry, stop and first target go out as one order; the OMS host fills them on the entry.
    # A plain attribute, not a Capabilities field: that dataclass is serialized into the frozen p4c tape.
    atomic_bracket = True

    def __init__(
        self,
        client: NtxClient,
        *,
        clock,
        ledger=None,
        trading_day: Callable[[datetime], date] | None = None,
        on_poll=None,
        account_bindings: Mapping[str, Any] | None = None,
        live_ack: bool = False,
    ) -> None:
        super().__init__()
        self._client = client
        # engine account id -> binding whose account_key["brokerAccountId"] is the ntx account
        self._bindings = dict(account_bindings or {})
        # I10: a live account's place or modify is refused without the acknowledgement; cancel is not.
        self._live_ack = live_ack
        self._orders: OrderedDict[str, dict[str, Any]] = OrderedDict()
        # OA-5: reconcile verdicts for PENDING entries, read back by the OMS through `orders()`.
        self._resolved: OrderedDict[str, Any] = OrderedDict()
        # OA-5: called at the tail of `reconcile` with the report, so the OMS reads back the
        # PENDING verdicts the adapter just resolved. None means no OMS is wired (read-only).
        self._on_reconciled: Callable[[ReconcileReport], None] | None = None
        self._logged_unmatched: set[str] = set()
        self._last_payload: dict[str, Any] | None = None  # the last good /snapshot, for reconcile
        self._on_poll = on_poll
        self._clock = clock
        self._ledger = ledger
        self._logged_no_ledger = False
        self._trading_day = trading_day
        self._last_accounts = None
        self._kept: dict[str, _Account] = {}  # key -> account; replaced per read, <= ACCOUNT_CAP
        self._identities: dict[str, Any] = {}
        self._reference: date | None = None
        self._poller = None
        # BP-4e: the oldest stamped figure of the last pnl pass (None: no figures).
        self.last_figure_as_of: datetime | None = None
        self.stats = AdapterStats()
        # Market and limit entries with a native protective stop, day orders.
        self.capabilities = Capabilities(
            supported_order_types=frozenset({OrderType.MARKET, OrderType.LIMIT, OrderType.STOP}),
            supported_tifs=frozenset({TimeInForce.DAY}),
            supports_multi_leg=False,
            supports_native_stops=True,
            supports_streaming=False,
        )


_LIVE = frozenset({
    OrderState.NEW, OrderState.SUBMITTED, OrderState.ACCEPTED,
    OrderState.PARTIALLY_FILLED, OrderState.PENDING_UNKNOWN,
})


@dataclass
class ReconcileReport:
    """What one reconcile did, by engine order id."""

    mapped: list[str] = field(default_factory=list)
    unmatched: list[str] = field(default_factory=list)
    accepted: list[str] = field(default_factory=list)  # PENDING -> ACCEPTED
    rejected: list[str] = field(default_factory=list)  # PENDING -> REJECTED not_at_venue
    read_failed: bool = False


def _bracket_index(row: dict[str, Any] | None) -> tuple[dict[str, dict[str, Any]] | None, bool]:
    """`(clOrdId -> ntx ids, authoritative)` from one snapshot account row.

    The index is None when the row carries no bracket truth at all (an ntx without `brackets`).
    Order rows are read first (`clOrdId` plus `brackets {parentId, stopId, targetId?}`, OA-5b),
    then the account-level `brackets` list fills any id the rows did not give. `authoritative` is
    False when ntx says it has a placement of unknown outcome (`bracketsAuthoritative: false`):
    an entry missing from the index is then not proof it is absent from the venue.
    """
    return rs._bracket_index(row)


class AccountWatcher(rs.AccountWatcherBase):
    """Hold one `/events` socket and turn its hints into bounded re-reads.

    On every (re)connect: open the stream, then one fresh `/snapshot` through the poller,
    which diffs it. While connected, an account-entity `props` event or a `sync` event is a
    hint: at most one early re-read per 5 s (`RefreshLimiter`), the rest coalesced into one
    trailing re-read that fires at the next frame or keepalive. Never a snapshot per event.
    A dropped stream backs off 1 s to 30 s with jitter; only a stream that stayed up for a
    minute resets the backoff. Time, sleep and random are injected (I7).
    """

    def __init__(
        self,
        adapter: TradovateAdapter,
        client: NtxClient,
        *,
        clock,
        sleep: Callable[[float], None],
        rand: Callable[[], float],
        limiter: RefreshLimiter | None = None,
        backoff: ReconnectBackoff | None = None,
        read: Callable[[], None] | None = None,
    ) -> None:
        super().__init__()
        self._adapter = adapter
        self._client = client
        self._clock = clock
        self._sleep = sleep
        # BP-4e: where a read goes. None polls the adapter's poller on this thread (BP-3).
        self._read_fn = read
        self.limiter = limiter if limiter is not None else RefreshLimiter()
        self.backoff = backoff if backoff is not None else ReconnectBackoff(rand)
        self.snapshots = 0
        self.connects = 0
        # REGFEED-STOP-1: the open stream, so `wake()` (another thread, on stop) can reach it.
        self._lock = threading.Lock()
        self._stream = None
        self._stopping = False


__all__ = ["ACCOUNT_CAP", "AccountWatcher", "AdapterStats", "ReconcileReport", "TradovateAdapter"]

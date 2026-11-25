"""TosPaperBroker — the thinkorswim paperMoney mirror adapter (architecture §4.7).

A **slow venue**, never on the simulator's critical path: ``mirror_batch`` nets a batch
and queues its tickets without touching the transport, so the sim book never waits.
The host drains the queue off the critical path (``drain``): one ticket at a time, each
read back from the Order Book and positions before the next, then a reconcile. Drift
halts the venue (a ``VenueReconcile`` event the host appends; the ledger fold latches
the halt per venue, sticky across replay).

The mirror's memory is the ledger (``ledger.mirror``, T2): ``restore`` loads a venue's
folded mirror — the book of proven venue fills, the open tickets and the venue Order
IDs their sends proved — so a cancel and the fill read-back work after a restart, and
the reconcile expects exactly what the fold says the venue should hold.
``collect_fills`` reads the venue's cumulative fills per Order ID and returns the
``MirrorFill`` increments (and the Order Book closes) for the host to append. The
session order and the ledger appends live in ``tos_paper.session_native``.

Every decision (the submit queue, the expected book, the sent tickets, the cancelled set,
the keys, the sticky halt, what to call next) lives in Rust: ``te_core::tos_paper::broker``,
owned by ``trade_engine_rs.TosPaperBrokerBase``. This module keeps the public class (a thin
subclass), its class attributes, the carriers and the module-level helpers the rest of the
engine imports; the transport and balance reader are protocols the host wires. Nothing here
places a real order or calls any Schwab endpoint. Unknown venue state is PENDING, never
ACCEPTED (§4.5, I5); every strategy order is either allocated to a ticket or refused with a
reason (I11).
"""

from __future__ import annotations

import json
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Literal

import trade_engine_rs as rs

from trade_engine.domain.orders import OrderState, OrderType, TimeInForce
from trade_engine.interfaces.broker import (
    AccountRecord,
    BrokerAdapter,
    Capabilities,
    Route,
    VenueAck,
    VenueOrder,
    VenueOrderAllocation,
)
from trade_engine.ledger.events import MirrorAck, MirrorFill, MirrorQueued, VenueReconcile
from trade_engine.tos_paper.transport import (
    BalanceReader,
    OrderCanceller,
    OrderFillReader,
    TosOrderTransport,
    TransportRefused,
    TransportReplay,
    TransportUnavailable,
    ticket_of,
)


class TosPaperBrokerError(RuntimeError):
    """An invalid mirror operation or a connect-time refusal."""


class VenueUnreadable(TosPaperBrokerError):
    """The venue could not be read, or contradicted the mirror: nothing is booked (I5).

    ``reconcile`` is the drifting ``VenueReconcile`` the host appends; the broker is
    already halted.
    """

    def __init__(self, reconcile: VenueReconcile) -> None:
        super().__init__(reconcile.note or "venue unreadable")
        self.reconcile = reconcile


def _as_decimal(value: object, name: str) -> Decimal:
    if isinstance(value, (bool, float)) or value is None:
        raise TosPaperBrokerError(f"{name} must be a Decimal, int or decimal string, got {value!r}")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value).strip())
    except InvalidOperation as exc:
        raise TosPaperBrokerError(f"{name} is not a number: {value!r}") from exc
    if not result.is_finite():
        raise TosPaperBrokerError(f"{name} must be finite, got {value!r}")
    return result


@dataclass(frozen=True)
class MirrorBinding:
    """One paperMoney account and the virtual accounts it mirrors 1:1."""

    venue_account: str          # the paperMoney id, e.g. 'D-00000001' (configured by the host)
    account_type: Literal["ira", "margin", "cash"]  # checked against the banner at connect
    mirrored_accounts: tuple[str, ...]  # virtual account ids, e.g. ('OPT_CSP', 'OPT_PUT_SPREAD')
    minimum_balance: Decimal    # §4.7 funding: PM-A ≥ $100k, PM-B ≥ $50k

    def __post_init__(self) -> None:
        if not self.venue_account.startswith("D-"):
            raise TosPaperBrokerError("venue_account must be a paperMoney id ('D-…')")
        if self.account_type not in ("ira", "margin", "cash"):
            raise TosPaperBrokerError(f"account_type must be ira, margin or cash, got {self.account_type!r}")
        object.__setattr__(self, "mirrored_accounts", tuple(self.mirrored_accounts))
        if not self.mirrored_accounts:
            raise TosPaperBrokerError("a mirror binding must name at least one virtual account")
        minimum = _as_decimal(self.minimum_balance, "minimum_balance")
        if minimum <= 0:
            raise TosPaperBrokerError("minimum_balance must be positive (§4.7)")
        object.__setattr__(self, "minimum_balance", minimum)


@dataclass(frozen=True)
class DrainReport:
    """What one drain of the submit queue proved."""

    acks: tuple[VenueAck, ...]
    reconcile: VenueReconcile | None  # None only when the queue was empty
    # ticket key -> (venue Order ID, its Order Book state) for sends that proved one
    proven: Mapping[str, tuple[str, OrderState]] = field(default_factory=dict)


@dataclass(frozen=True)
class FillCollection:
    """What one fill read-back proved, as mirror events for the host to append."""

    fills: tuple[MirrorFill, ...]   # cumulative increments only
    closes: tuple[MirrorAck, ...]   # tickets the Order Book shows ended (cancelled/expired/rejected/filled)


class _Host:
    """The clock and the transport, answered as the core asks. No decision lives here."""

    def __init__(self, broker: "TosPaperBroker") -> None:
        self._b = broker
        self._last: BaseException | None = None

    def now(self) -> str:
        return self._b._clock.now_utc().isoformat()

    def can_read_fills(self) -> bool:
        return isinstance(self._b.transport, OrderFillReader)

    def can_cancel(self) -> bool:
        return isinstance(self._b.transport, OrderCanceller)

    def _ask(self, call):
        try:
            # The row crosses as-is; the core reads it with `default=str` semantics (plain_of).
            return ("ok", call(), "", "")
        except Exception as exc:  # noqa: BLE001 - the core classifies; a BaseException unwinds as itself
            self._last = exc
            kind = ("refused" if isinstance(exc, TransportRefused) else "replay" if isinstance(exc, TransportReplay)
                    else "unavailable" if isinstance(exc, TransportUnavailable) else "other")
            return ("exc", kind, type(exc).__name__, str(exc))

    def place_order(self, spec: Mapping, key: str):
        return self._ask(lambda: self._b.transport.place_order(ticket_of(spec), key))

    def cancel_order(self, order_id: str):
        return self._ask(lambda: self._b.transport.cancel_order(order_id))

    def read_positions(self):
        return self._ask(lambda: list(self._b.transport.read_positions()))

    def read_working_orders(self):
        return self._ask(lambda: list(self._b.transport.read_working_orders()))

    def read_order_fills(self):
        return self._ask(lambda: list(self._b.transport.read_order_fills()))

    def reraise(self) -> None:
        raise self._last  # the very exception object the transport raised (TransportUnavailable)


class TosPaperBroker(rs.TosPaperBrokerBase, BrokerAdapter):
    """BrokerAdapter for one paperMoney mirror account.

    Every decision (the submit queue, the expected book, the sent tickets, the cancelled set, the
    keys, the sticky halt, what to call next) is ``trade_engine_rs.TosBrokerBase``'s (the Rust
    ``te_core::tos_paper::broker::Broker``); this class is the I/O around it: the transport's
    sends, reads and cancels, ``connect`` and ``balance``, and the carriers the rest of the
    engine takes back.
    """

    name = "TosPaperBroker"
    env: Literal["paper"] = "paper"
    stale_after = 120

    def __init__(
        self,
        transport: TosOrderTransport,
        binding: MirrorBinding,
        *,
        clock,
        balance_reader: BalanceReader | None = None,
        balance_unproven_ok: bool = False,
        halted_venues: Collection[str] = (),
        mirror_path: str | Path | None = None,
        account_ledger=None,
    ) -> None:
        # The Rust base's __new__ built the core (venue, mirrored accounts, sticky halt).
        super().__init__()
        self.transport = transport
        self.binding = binding
        self._clock = clock
        # The fixed account list: `mirror.json` when the host provides its path (each
        # paperMoney entry becomes one record), else the one account this mirror binds.
        self._mirror_path = None if mirror_path is None else Path(mirror_path)
        self._account_ledger = account_ledger
        self._last_accounts = None
        self._poller = None
        self._balance_reader = balance_reader
        self._balance_unproven_ok = balance_unproven_ok
        self._identity = None
        self.balance_proven: bool | None = None  # recorded at connect
        self._host = _Host(self)
        self.capabilities = Capabilities(
            supported_order_types=frozenset({OrderType.MARKET, OrderType.LIMIT}),
            supported_tifs=frozenset({TimeInForce.DAY, TimeInForce.GTC}),
            supports_multi_leg=False,
            supports_native_stops=False,
            supports_streaming=False,
        )


def load_mirror_accounts(path: str | Path, *, clock) -> list[AccountRecord]:
    """Read `mirror.json`'s `venues[]` as the fixed paperMoney account list.

    The file is the host's hand-maintained list (`venue_account`, `account_type`,
    `mirrored_accounts`, `minimum_balance`). Every entry becomes one record with
    `env: paper`, `source: tos_paper`, `status: active` and a `tos` route. A malformed
    file refuses (I5); the adapter keeps its last good list (B3).
    """
    try:
        data = json.loads(Path(path).read_text(encoding="utf8"))
    except (OSError, ValueError) as exc:
        raise TosPaperBrokerError(f"cannot read the account mirror list ({type(exc).__name__})") from exc
    venues = data.get("venues") if isinstance(data, dict) else None
    if not isinstance(venues, list) or not venues:
        raise TosPaperBrokerError("the account mirror list must hold a non-empty 'venues' list")
    confirmed_at = clock.now_utc()
    records: list[AccountRecord] = []
    for entry in venues:
        if not isinstance(entry, dict):
            raise TosPaperBrokerError("every 'venues' entry must be an object")
        venue_account = entry.get("venue_account")
        account_type = entry.get("account_type")
        if not isinstance(venue_account, str) or not venue_account.startswith("D-"):
            raise TosPaperBrokerError("venue_account must be a paperMoney id ('D-…')")
        if not isinstance(account_type, str) or account_type not in ("ira", "margin", "cash"):
            raise TosPaperBrokerError(f"account_type must be ira, margin or cash, got {account_type!r}")
        records.append(
            AccountRecord.of(
                "tos_paper",
                venue_account,
                env="paper",
                status="active",
                account_type=account_type,
                routes=(Route("tos", venue_account),),
                source="tos_paper",
                confirmed_at=confirmed_at,
            )
        )
    return records


def venue_order_of(queued: MirrorQueued) -> VenueOrder:
    """The venue order a ``MirrorQueued`` event recorded, rebuilt exactly."""
    return VenueOrder(
        venue_order_id=queued.ticket_key,
        instrument=queued.instrument,
        order_type=queued.order_type,
        side=queued.side,
        quantity=queued.quantity,
        submitted_at=queued.at,
        tif=queued.tif,
        limit_price=queued.limit_price,
        allocations=tuple(
            VenueOrderAllocation(a.strategy_order_id, a.strategy_account, a.quantity)
            for a in queued.allocations
        ),
    )

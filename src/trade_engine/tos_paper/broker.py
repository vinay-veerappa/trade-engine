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
session order and the ledger appends live in ``tos_paper.session``.

The engine never imports tos-ui-mcp: the transport and balance reader are protocols
(``transport``) the host wires. Nothing here places a real order or calls any Schwab
endpoint. Unknown venue state is PENDING, never ACCEPTED (§4.5, I5); every strategy
order is either allocated to a ticket or refused with a reason (I11).
"""

from __future__ import annotations

import json
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Literal

from trade_engine.domain.instruments import Instrument
from trade_engine.domain.orders import Order, OrderState, OrderType, TimeInForce
from trade_engine.interfaces.broker import (
    BrokerAdapter,
    Capabilities,
    OrderChanges,
    VenueAck,
    VenueCashEvent,
    VenueFill,
    VenueIdentity,
    VenueOrder,
    VenueOrderAllocation,
    VenueOrderState,
    VenuePosition,
)
from trade_engine.ledger import codec
from trade_engine.ledger.events import MirrorAck, MirrorFill, MirrorQueued, VenueReconcile
from trade_engine.ledger.mirror import MirrorState
from trade_engine.tos_paper import _rs
from trade_engine.tos_paper.netting import NettedBatch
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
            # ``default=str``: a row holding a value JSON cannot carry reaches the core as its text.
            return ("ok", json.dumps(call(), default=str), "", "")
        except Exception as exc:  # noqa: BLE001 - the core classifies; a BaseException unwinds as itself
            self._last = exc
            kind = ("refused" if isinstance(exc, TransportRefused) else "replay" if isinstance(exc, TransportReplay)
                    else "unavailable" if isinstance(exc, TransportUnavailable) else "other")
            return ("exc", kind, type(exc).__name__, str(exc))

    def place_order(self, spec: str, key: str):
        return self._ask(lambda: self._b.transport.place_order(ticket_of(json.loads(spec)), key))

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


class TosPaperBroker(BrokerAdapter):
    """BrokerAdapter for one paperMoney mirror account.

    Every decision (the submit queue, the expected book, the sent tickets, the cancelled set, the
    keys, the sticky halt, what to call next) is ``trade_engine_rs.TosBroker``'s; this class is the
    I/O around it: the transport's sends, reads and cancels, ``connect`` and ``balance``, and the
    carriers the rest of the engine takes back.
    """

    name = "TosPaperBroker"
    env: Literal["paper"] = "paper"

    def __init__(
        self,
        transport: TosOrderTransport,
        binding: MirrorBinding,
        *,
        clock,
        balance_reader: BalanceReader | None = None,
        balance_unproven_ok: bool = False,
        halted_venues: Collection[str] = (),
    ) -> None:
        self.transport = transport
        self.binding = binding
        self._clock = clock
        self._balance_reader = balance_reader
        self._balance_unproven_ok = balance_unproven_ok
        self._identity: VenueIdentity | None = None
        self.balance_proven: bool | None = None  # recorded at connect
        # Restored from the ledger fold (``ledger.state.halted_venues``): a halt survives replay.
        self._core = _rs.rs.TosBroker(binding.venue_account, list(binding.mirrored_accounts),
                                      binding.venue_account in frozenset(halted_venues))
        self._host = _Host(self)
        self.capabilities = Capabilities(
            supported_order_types=frozenset({OrderType.MARKET, OrderType.LIMIT}),
            supported_tifs=frozenset({TimeInForce.DAY, TimeInForce.GTC}),
            supports_multi_leg=False,
            supports_native_stops=False,
            supports_streaming=False,
        )

    @property
    def venue(self) -> str:
        """The venue key a VenueReconcile names and the halt is latched under."""
        return self.binding.venue_account

    @property
    def halted(self) -> bool:
        return self._core.halted

    @property
    def queued(self) -> tuple[VenueOrder, ...]:
        return tuple(_rs.venue_order(d) for d in json.loads(self._core.state())["queued"])

    def _go(self, method, *args):
        return _rs.call(method, self._host, *args)

    # -- connection ----------------------------------------------------------

    def connect(self) -> VenueIdentity:
        """Prove the account number AND type (I10), then the balance (§4.7).

        The transport proves the window is paperMoney. Here the banner must name the
        configured account and type — a CSP order must never reach the IRA — and the
        balance must cover the mirrored accounts. With no balance reader the connect
        refuses unless the host explicitly passed ``balance_unproven_ok=True``, which is
        recorded as ``balance_proven = False``.
        """
        identity = self.transport.connect()
        number = str(identity.get("number", ""))
        kind = str(identity.get("type", "")).strip().lower()
        if number != self.binding.venue_account:
            raise TosPaperBrokerError(
                f"transport connected {number!r}, binding is {self.binding.venue_account!r} "
                "— refusing to mirror"
            )
        if kind != self.binding.account_type:
            raise TosPaperBrokerError(
                f"{number} is a {kind or 'unknown'!r} account, binding needs "
                f"{self.binding.account_type!r} — refusing to mirror"
            )
        balance = self.balance()
        if balance is None:
            if not self._balance_unproven_ok:
                raise TosPaperBrokerError(
                    f"{number}: no balance reader, so the §4.7 minimum "
                    f"{self.binding.minimum_balance} is unproven — refusing to mirror "
                    "(pass balance_unproven_ok=True to accept that explicitly)"
                )
            self.balance_proven = False
        else:
            if balance < self.binding.minimum_balance:
                raise TosPaperBrokerError(
                    f"{number} balance {balance} is under the §4.7 minimum "
                    f"{self.binding.minimum_balance}; refusing to mirror"
                )
            self.balance_proven = True
        self._identity = VenueIdentity(
            account_id=self.binding.venue_account,
            env=self.env,
            connected_at=self._clock.now_utc(),
            broker_name=self.name,
        )
        self._core.mark_connected()
        return self._identity

    def balance(self) -> Decimal | None:
        """Net liquidation from the host's reader; None when no reader is wired."""
        if self._balance_reader is None:
            return None
        return _as_decimal(self._balance_reader.net_liquidation(), "net liquidation")

    def _require_connected(self, what: str) -> None:
        if self._identity is None:
            raise TosPaperBrokerError(f"{what} before connect; prove the venue first (I10)")

    # -- the mirror layer (sim critical path: no transport calls) ------------

    def mirror_batch(
        self,
        strategy_orders: Sequence[Order],
        *,
        holdings: Mapping[tuple[str, Instrument], Decimal],
    ) -> NettedBatch:
        """Net a batch and queue its tickets. Never calls the transport.

        ``holdings`` is what each mirrored virtual account holds or has resting on this
        venue, signed contracts keyed ``(account_id, instrument)`` — conflicts are screened
        against it (``MirrorState.exposure()``: the mirror book plus every open ticket's
        unfilled remainder, per allocation). Before ``restore`` it is also the expected
        venue book; after ``restore`` the expectation is the fold's (``MirrorState.expected``),
        so an open ticket is never counted twice. The sim book takes every order; refusals
        here are venue refusals the caller records (I11).
        """
        self._require_connected("mirror_batch")
        out = json.loads(self._go(
            self._core.mirror_batch,
            json.dumps([_rs.order_doc(o) for o in list(strategy_orders)], default=str),
            json.dumps(_rs.holdings_doc(holdings))))
        return NettedBatch(venue_orders=tuple(_rs.venue_order(d) for d in out["venue_orders"]),
                           refused=tuple((o, r) for o, r in out["refused"]))

    # -- the ledger's memory (restart-safe) ------------------------------------

    def restore(self, mirror: MirrorState, *, halted_venues: Collection[str] = ()) -> None:
        """Load the venue's folded mirror: the fold, not this process, is the memory (I2).

        - expected = the mirror book (proven venue fills) + every open ticket's live
          remainder; a ticket that was rejected, cancelled or expired adds nothing;
        - every ticket key the fold holds is used (never queued again, I3);
        - the venue Order IDs sends proved make a cancel possible after a restart;
        - a halt in ``halted_venues`` (``ledger.state.halted_venues``) is kept.

        Refuses over an undrained queue: the queue would be lost or sent twice.
        """
        _rs.call(self._core.restore, json.dumps(codec.canon(mirror)), list(halted_venues))

    def collect_fills(self, mirror: MirrorState) -> FillCollection:
        """Read the venue's cumulative fills per Order ID: the ``MirrorFill`` increments, and the
        Order Book closes, for the host to append (see ``tos_paper.session``)."""
        out = json.loads(self._go(self._core.collect_fills, json.dumps(codec.canon(mirror))))
        return FillCollection(
            fills=tuple(MirrorFill(venue=f["venue"], ticket_key=f["ticket_key"], venue_order_id=f["venue_order_id"],
                                   filled=Decimal(f["filled"]), avg_price=_rs.dec(f["avg_price"]),
                                   at=datetime.fromisoformat(f["at"])) for f in out["fills"]),
            closes=tuple(MirrorAck(venue=c["venue"], ticket_key=c["ticket_key"], status=c["status"],
                                   message=c["message"], at=datetime.fromisoformat(c["at"]),
                                   venue_order_id=c["venue_order_id"], book_status=OrderState(c["book_status"]))
                         for c in out["closes"]))

    # -- the slow path (host-driven, off the sim critical path) --------------

    def preflight(self) -> None:
        """Read the venue's positions now, before anything is recorded, for the next drain.

        The session calls this BEFORE its write-ahead: a venue that cannot be asked
        (:class:`TransportUnavailable`) raises here, while nothing is queued in the ledger, so the
        run defers whole and loses nothing. The drain then sends from this read instead of
        reading again, so no read sits between the write-ahead and the send.
        """
        self._go(self._core.preflight)

    def drain(self) -> DrainReport:
        """Send the queue one ticket at a time, each read back before the next, then reconcile."""
        out = json.loads(self._go(self._core.drain))
        return DrainReport(
            acks=tuple(_rs.ack(a) for a in out["acks"]),
            reconcile=None if out["reconcile"] is None else _rs.event(out["reconcile"]),
            proven={k: (oid, OrderState(state)) for k, (oid, state) in out["proven"]})

    def reconcile_now(self, *, defer_unavailable: bool = False) -> VenueReconcile:
        """Read the venue and compare it with what the mirror expects (drift halts the venue)."""
        return _rs.event(json.loads(self._go(self._core.reconcile_now, defer_unavailable)))

    def submit(self, order: VenueOrder) -> VenueAck:
        return _rs.ack(json.loads(self._go(self._core.submit, json.dumps(_rs.venue_order_doc(order)))))

    def cancel(self, venue_order_id: str) -> VenueAck:
        return _rs.ack(json.loads(self._go(self._core.cancel, venue_order_id)))

    def proven_order_id(self, ticket_key: str) -> str | None:
        """The venue Order ID a send (or the restored fold) proved for a ticket, or None."""
        return self._core.proven_order_id(ticket_key)

    def replace(self, venue_order_id: str, changes: OrderChanges) -> VenueAck:
        raise TosPaperBrokerError(
            "replace (TOS Cancel/replace order) is not mapped over JAB yet; "
            "cancel() and submit a new ticket instead"
        )

    def orders(self, since: datetime) -> list[VenueOrderState]:
        raise TosPaperBrokerError(
            "venue order states cannot be read per venue order id over JAB yet; "
            "use reconcile_now() (Order Book vs mirror book)"
        )

    def fills(self, since: datetime) -> list[VenueFill]:
        raise TosPaperBrokerError(
            "venue fills are not read per fill id (not wired); use collect_fills(mirror), "
            "which reads cumulative fills per venue Order ID into the mirror ledger"
        )

    def positions(self) -> list[VenuePosition]:
        self._require_connected("positions")
        return [VenuePosition(_rs.unwire(p["instrument"]), Decimal(p["quantity"]), Decimal(p["avg_price"]),
                              datetime.fromisoformat(p["as_of"]))
                for p in json.loads(self._go(self._core.positions))]

    def cash_events(self, since: datetime) -> list[VenueCashEvent]:
        raise TosPaperBrokerError("venue cash events (assignment/exercise) cannot be read yet")


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

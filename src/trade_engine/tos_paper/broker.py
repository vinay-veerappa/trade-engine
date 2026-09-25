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
    UnsupportedCapability,
    VenueAck,
    VenueCashEvent,
    VenueFill,
    VenueIdentity,
    VenueOrder,
    VenueOrderAllocation,
    VenueOrderState,
    VenuePosition,
)
from trade_engine.ledger.events import MirrorAck, MirrorFill, MirrorQueued, VenueReconcile
from trade_engine.ledger.mirror import MirrorState
from trade_engine.ledger.mirror import ticket_contracts as fold_contracts
from trade_engine.tos_paper import normalize as norm
from trade_engine.tos_paper.netting import NettedBatch, net_strategy_orders
from trade_engine.tos_paper.reconcile import (
    confirm_ticket,
    position_book,
    reconcile,
    ticket_contracts,
    unreadable,
)
from trade_engine.tos_paper.transport import (
    BalanceReader,
    OrderCanceller,
    OrderFillReader,
    TosOrderTransport,
    ticket_for,
)

ZERO = Decimal("0")


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


class TosPaperBroker(BrokerAdapter):
    """BrokerAdapter for one paperMoney mirror account."""

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
        # Restored from the ledger fold (``ledger.state.halted_venues``): a halt survives replay.
        self._halted = binding.venue_account in frozenset(halted_venues)
        self._identity: VenueIdentity | None = None
        self.balance_proven: bool | None = None  # recorded at connect
        self._queue: list[VenueOrder] = []
        self._keys: set[str] = set()  # ticket keys queued or sent (I3)
        self._expected: dict[Instrument, Decimal] = {}
        # Open tickets' live remainder restored from the ledger fold (``restore``).
        self._restored_open: dict[Instrument, Decimal] = {}
        # ticket key -> (ticket, venue Order ID) for sends whose Order Book row was matched,
        # restored from the fold after a restart; with no proven id a cancel refuses.
        self._sent: dict[str, tuple[VenueOrder, str]] = {}
        self._proven: dict[str, tuple[str, OrderState]] = {}  # this drain's proven ids
        self._cancelled: set[str] = set()  # ticket keys a cancel proved (idempotent)
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
        return self._halted

    @property
    def queued(self) -> tuple[VenueOrder, ...]:
        return tuple(self._queue)

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

        ``holdings`` is the sim-derived mirror book: signed contracts per mirrored
        virtual account on this venue, keyed ``(account_id, instrument)``. The sim book
        takes every order; refusals here are venue refusals the caller records (I11).
        """
        self._require_connected("mirror_batch")
        orders = list(strategy_orders)
        if self._halted:
            return NettedBatch(
                venue_orders=(),
                refused=tuple(
                    (o.order_id, f"venue {self.venue} is halted by a reconcile drift; refused")
                    for o in orders
                ),
            )
        batch = net_strategy_orders(
            orders,
            venue_account=self.venue,
            mirrored_accounts=self.binding.mirrored_accounts,
            at=self._clock.now_utc(),
            holdings=holdings,
        )
        expected: dict[Instrument, Decimal] = dict(self._restored_open)  # open tickets from the fold
        for (_account, instrument), quantity in holdings.items():
            expected[instrument] = expected.get(instrument, ZERO) + quantity
        for queued in self._queue:  # still unsent: part of what the venue should end up holding
            _add(expected, queued, 1)
        for ticket in batch.venue_orders:
            if ticket.venue_order_id in self._keys:
                continue  # the same ticket was already queued or sent (I3)
            self._keys.add(ticket.venue_order_id)
            self._queue.append(ticket)
            _add(expected, ticket, 1)
        self._expected = expected
        return batch

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
        if mirror.venue is not None and mirror.venue != self.venue:
            raise TosPaperBrokerError(f"cannot restore venue {mirror.venue}'s mirror into {self.venue} (I8)")
        if self._queue:
            raise TosPaperBrokerError("restore over an undrained queue; drain (or cancel) it first")
        if self.venue in frozenset(halted_venues):
            self._halted = True
        open_remainder: dict[Instrument, Decimal] = {}
        sent: dict[str, tuple[VenueOrder, str]] = {}
        for ticket in mirror.open_tickets:
            for contract, quantity in fold_contracts(ticket.queued, ticket.remaining).items():
                open_remainder[contract] = open_remainder.get(contract, ZERO) + quantity
            if ticket.venue_order_id is not None:
                sent[ticket.key] = (venue_order_of(ticket.queued), ticket.venue_order_id)
        self._restored_open = open_remainder
        self._sent = sent
        self._keys |= set(mirror.tickets)
        self._cancelled |= {
            key for key, ticket in mirror.tickets.items() if ticket.book_status is OrderState.CANCELLED
        }
        self._expected = mirror.expected()

    def collect_fills(self, mirror: MirrorState) -> FillCollection:
        """Read the venue's cumulative fills per Order ID; return the increments (I3).

        Matched to tickets by the venue Order ID the fold holds; an Order ID the fold
        does not know is not ours and is ignored. A ticket whose Order Book row now reads
        CANCELED, EXPIRED, REJECTED or FILLED gets a closing ``MirrorAck`` (after its fill,
        so a partial fill before a cancel is booked). Never sends anything.

        Refuses (``VenueUnreadable``, and the venue halts) when the rows cannot be read,
        when one Order ID has two rows, or when the venue contradicts the fold — a
        cumulative lower than recorded, more than the ticket, FILLED short of it, or the
        recorded cumulative at another average price.
        A transport without :class:`OrderFillReader` cannot prove a fill: refused.
        """
        self._require_connected("collect_fills")
        if mirror.venue is not None and mirror.venue != self.venue:
            raise TosPaperBrokerError(f"venue {mirror.venue}'s mirror is not {self.venue}'s (I8)")
        if not isinstance(self.transport, OrderFillReader):
            raise TosPaperBrokerError(
                "the transport cannot read order fills (OrderFillReader); refusing to guess the mirror book (I5)"
            )
        now = self._clock.now_utc()
        tracked = [t for _, t in sorted(mirror.tickets.items()) if t.venue_order_id is not None]
        contracts = [c for t in tracked for c in fold_contracts(t.queued, t.queued.quantity)]
        try:
            rows = [norm.normalize_order_fill(row) for row in self.transport.read_order_fills()]
        except Exception as exc:  # noqa: BLE001 — a failed read is a refusal, not a crash
            raise self._unreadable(unreadable(self.venue, now, contracts, f"fill read-back failed: {exc}"))
        by_id: dict[str, norm.OrderFill] = {}
        for row in rows:
            if row.order_id in by_id:
                raise self._unreadable(
                    unreadable(self.venue, now, contracts, f"two fill rows for order {row.order_id}")
                )
            by_id[row.order_id] = row
        fills: list[MirrorFill] = []
        closes: list[MirrorAck] = []
        contradicted: list[tuple[MirrorQueued, str]] = []
        for ticket in tracked:
            row = by_id.get(ticket.venue_order_id)
            if row is None:
                continue  # not on today's book; the reconcile judges what the venue holds
            quantity = ticket.queued.quantity
            if row.filled < ticket.filled or row.filled > quantity:
                contradicted.append(
                    (
                        ticket.queued,
                        f"order {row.order_id} reads filled {row.filled}; the mirror has "
                        f"{ticket.filled} of {quantity}",
                    )
                )
                continue
            if row.state is OrderState.FILLED and row.filled != quantity:
                contradicted.append(
                    (ticket.queued, f"order {row.order_id} reads FILLED at {row.filled} of {quantity}")
                )
                continue
            if row.filled == ticket.filled and row.filled > 0 and row.avg_price != ticket.avg_price:
                contradicted.append(
                    (
                        ticket.queued,
                        f"order {row.order_id} reads filled {row.filled} at {row.avg_price}; the "
                        f"mirror booked it at {ticket.avg_price}",
                    )
                )
                continue
            if row.filled > ticket.filled:
                fills.append(
                    MirrorFill(
                        venue=self.venue,
                        ticket_key=ticket.key,
                        venue_order_id=row.order_id,
                        filled=row.filled,
                        avg_price=row.avg_price,
                        at=now,
                    )
                )
            ended = row.state is OrderState.FILLED or row.state in _CLOSED
            if ended and not ticket.closed and ticket.book_status is not row.state:
                closes.append(
                    MirrorAck(
                        venue=self.venue,
                        ticket_key=ticket.key,
                        status="REJECTED" if row.state is OrderState.REJECTED else "ACCEPTED",
                        message=(
                            f"order book: order {row.order_id} reads {row.state.value}, "
                            f"filled {row.filled} of {quantity}"
                        ),
                        at=now,
                        venue_order_id=row.order_id,
                        book_status=row.state,
                    )
                )
        if contradicted:
            names = sorted(
                {c.symbol for queued, _ in contradicted for c in fold_contracts(queued, queued.quantity)}
            )
            raise self._unreadable(
                VenueReconcile(
                    venue=self.venue,
                    as_of=now,
                    reconciled=False,
                    drift=tuple(names),
                    note="venue fills contradict the mirror ("
                    + "; ".join(why for _, why in contradicted)
                    + "); venue halted",
                )
            )
        return FillCollection(fills=tuple(fills), closes=tuple(closes))

    def _unreadable(self, event: VenueReconcile) -> VenueUnreadable:
        self._halted = True
        return VenueUnreadable(event)

    # -- the slow path (host-driven, off the sim critical path) --------------

    def drain(self) -> DrainReport:
        """Send queued tickets one at a time, each read back, then reconcile.

        Never raises out of the batch for a venue problem: every ticket ends with an
        ack (REJECTED with a reason, PENDING, or ACCEPTED only when read back), and the
        batch ends with a VenueReconcile the host appends to the ledger.
        """
        self._require_connected("drain")
        if not self._queue:
            return DrainReport(acks=(), reconcile=None)
        tickets, self._queue = self._queue, []
        self._proven = {}
        contracts = [c for t in tickets for c in ticket_contracts(t)]
        acks: list[VenueAck] = []
        try:
            before = position_book(self._read_positions())
        except Exception as exc:  # noqa: BLE001 — a failed read is a refusal, not a crash
            for ticket in tickets:
                acks.append(self._ack(ticket, "REJECTED", f"cannot read the venue before sending: {exc}"))
                self._unexpect(ticket)
            return self._finish(acks, contracts, f"pre-send read failed: {exc}")
        claimed: set[int] = set()
        for ticket in tickets:
            if self._halted:
                acks.append(self._ack(ticket, "REJECTED", f"venue {self.venue} is halted; not sent"))
                self._unexpect(ticket)
                continue
            ack = self._send(ticket)
            if ack.status == "REJECTED":
                self._unexpect(ticket)
                acks.append(ack)
                continue
            try:
                positions = self._read_positions()
                working = self._read_working()
            except Exception as exc:  # noqa: BLE001
                acks.append(self._ack(ticket, "PENDING", f"{ack.message}; read-back failed: {exc}"))
                continue
            status, reason = confirm_ticket(ticket, before, positions, working, claimed)
            if status == "REJECTED":
                self._unexpect(ticket)
            acks.append(self._ack(ticket, status, f"{ack.message}; {reason}" if status == "PENDING" else reason))
            before = position_book(positions)
        return self._finish(acks, contracts, None)

    def _finish(self, acks: list[VenueAck], contracts: list[Instrument], failed: str | None) -> DrainReport:
        now = self._clock.now_utc()
        if failed is not None:
            event = unreadable(self.venue, now, contracts + list(self._expected), failed)
        else:
            event = self.reconcile_now()
        if not event.reconciled:
            self._halted = True
        return DrainReport(acks=tuple(acks), reconcile=event, proven=dict(self._proven))

    def reconcile_now(self) -> VenueReconcile:
        """Compare the venue with the mirror book; drift (or an unreadable venue) halts."""
        now = self._clock.now_utc()
        try:
            positions = self._read_positions()
            working = self._read_working()
        except Exception as exc:  # noqa: BLE001
            event = unreadable(self.venue, now, list(self._expected), str(exc))
        else:
            event = reconcile(self.venue, now, self._expected, positions, working)
        if not event.reconciled:
            self._halted = True
        return event

    def _send(self, ticket: VenueOrder) -> VenueAck:
        try:
            spec = ticket_for(ticket)
        except UnsupportedCapability as exc:
            return self._ack(ticket, "REJECTED", f"UnsupportedCapability: {exc}")
        try:
            raw = self.transport.place_order(spec, ticket.venue_order_id)
        except Exception as exc:  # noqa: BLE001 — mapped, never propagated out of a batch
            return norm.normalize_place_exception(exc, ticket.venue_order_id, self._clock.now_utc())
        order_id = norm.placed_order_id(raw)
        if order_id is not None:
            self._sent[ticket.venue_order_id] = (ticket, order_id)
            self._proven[ticket.venue_order_id] = (order_id, norm.book_state(raw.get("book_status")))
        return norm.normalize_place_result(raw, ticket.venue_order_id, self._clock.now_utc())

    def _ack(self, ticket: VenueOrder, status: str, message: str) -> VenueAck:
        return VenueAck(ticket.venue_order_id, status, self._clock.now_utc(), message)

    def _read_positions(self) -> list[VenuePosition]:
        now = self._clock.now_utc()
        return [norm.normalize_position(row, now) for row in self.transport.read_positions()]

    def _read_working(self) -> list[norm.WorkingOrder]:
        return [norm.normalize_working_order(row) for row in self.transport.read_working_orders()]

    # -- the adapter contract -------------------------------------------------

    def submit(self, order: VenueOrder) -> VenueAck:
        """Send one venue order directly (the OMS path). PENDING until read back."""
        self._require_connected("submit")
        if self._halted:
            return self._ack(order, "REJECTED", f"venue {self.venue} is halted by a reconcile drift")
        ticket_for(order)  # UnsupportedCapability for anything the venue cannot express
        return self._send(order)

    def cancel(self, venue_order_id: str) -> VenueAck:
        """Cancel one ticket by its key. Allowed while halted: a cancel only lowers risk.

        A still-queued ticket is dropped from the queue (nothing reached the venue). A
        sent one is cancelled by the venue Order ID its send proved; with no proven id,
        or a transport without :class:`OrderCanceller`, the cancel is REJECTED (I5).
        ACCEPTED only when the Order Book row reads CANCELED; then the ticket leaves the
        mirror book's expectation (a partial fill before the cancel shows as drift).
        """
        self._require_connected("cancel")
        now = self._clock.now_utc()
        if venue_order_id in self._cancelled:
            return VenueAck(venue_order_id, "ACCEPTED", now, "already cancelled")
        for index, queued in enumerate(self._queue):
            if queued.venue_order_id == venue_order_id:
                del self._queue[index]
                self._unexpect(queued)
                self._cancelled.add(venue_order_id)
                return VenueAck(venue_order_id, "ACCEPTED", now, "cancelled before send: dropped from the queue")
        sent = self._sent.get(venue_order_id)
        if sent is None:
            return VenueAck(
                venue_order_id, "REJECTED", now,
                "no venue Order ID was proven for this ticket; refusing to guess the row (I5)",
            )
        if not isinstance(self.transport, OrderCanceller):
            return VenueAck(venue_order_id, "REJECTED", now, "the transport cannot cancel; refusing")
        ticket, order_id = sent
        try:
            raw = self.transport.cancel_order(order_id)
        except Exception as exc:  # noqa: BLE001 — mapped, never propagated
            return norm.normalize_cancel_exception(exc, venue_order_id, self._clock.now_utc())
        ack = norm.normalize_cancel_result(raw, venue_order_id, self._clock.now_utc())
        if ack.status == "ACCEPTED":
            del self._sent[venue_order_id]
            self._unexpect(ticket)
            self._cancelled.add(venue_order_id)
        return ack

    def _unexpect(self, ticket: VenueOrder) -> None:
        _add(self._expected, ticket, -1)

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
        return self._read_positions()

    def cash_events(self, since: datetime) -> list[VenueCashEvent]:
        raise TosPaperBrokerError("venue cash events (assignment/exercise) cannot be read yet")


_CLOSED = frozenset({OrderState.CANCELLED, OrderState.EXPIRED, OrderState.REJECTED})


def _add(expected: dict[Instrument, Decimal], ticket: VenueOrder, sign: int) -> None:
    for contract, quantity in ticket_contracts(ticket).items():
        expected[contract] = expected.get(contract, ZERO) + sign * quantity


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

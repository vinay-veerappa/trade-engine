"""TosPaperBroker — the thinkorswim paperMoney mirror adapter (architecture §4.7).

A **slow venue**, never on the simulator's critical path: ``mirror_batch`` nets a batch
and queues its tickets without touching the transport, so the sim book never waits.
The host drains the queue off the critical path (``drain``): one ticket at a time, each
read back from the Order Book and positions before the next, then a reconcile. Drift
halts the venue (a ``VenueReconcile`` event the host appends; the ledger fold latches
the halt per venue, sticky across replay).

The engine never imports tos-ui-mcp: the transport and balance reader are protocols
(``transport``) the host wires. Nothing here places a real order or calls any Schwab
endpoint. Unknown venue state is PENDING, never ACCEPTED (§4.5, I5); every strategy
order is either allocated to a ticket or refused with a reason (I11).
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Literal

from trade_engine.domain.instruments import Instrument, Side
from trade_engine.domain.orders import Order, OrderType, TimeInForce
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
    VenueOrderState,
    VenuePosition,
)
from trade_engine.ledger.events import VenueReconcile
from trade_engine.tos_paper import normalize as norm
from trade_engine.tos_paper.netting import NettedBatch, net_strategy_orders
from trade_engine.tos_paper.reconcile import confirm_ticket, position_book, reconcile, unreadable
from trade_engine.tos_paper.transport import (
    BalanceReader,
    OrderCanceller,
    TosOrderTransport,
    ticket_for,
)

ZERO = Decimal("0")


class TosPaperBrokerError(RuntimeError):
    """An invalid mirror operation or a connect-time refusal."""


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
        # ticket key -> (ticket, venue Order ID) for sends whose Order Book row was matched.
        # In memory only: after a restart a cancel refuses rather than guess the row.
        self._sent: dict[str, tuple[VenueOrder, str]] = {}
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
        expected: dict[Instrument, Decimal] = {}
        for (_account, instrument), quantity in holdings.items():
            expected[instrument] = expected.get(instrument, ZERO) + quantity
        for queued in self._queue:  # still unsent: part of what the venue should end up holding
            expected[queued.instrument] = expected.get(queued.instrument, ZERO) + _signed(queued)
        for ticket in batch.venue_orders:
            if ticket.venue_order_id in self._keys:
                continue  # the same ticket was already queued or sent (I3)
            self._keys.add(ticket.venue_order_id)
            self._queue.append(ticket)
            expected[ticket.instrument] = expected.get(ticket.instrument, ZERO) + _signed(ticket)
        self._expected = expected
        return batch

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
        acks: list[VenueAck] = []
        try:
            before = position_book(self._read_positions())
        except Exception as exc:  # noqa: BLE001 — a failed read is a refusal, not a crash
            for ticket in tickets:
                acks.append(self._ack(ticket, "REJECTED", f"cannot read the venue before sending: {exc}"))
                self._expected[ticket.instrument] = self._expected.get(ticket.instrument, ZERO) - _signed(ticket)
            return self._finish(acks, [t.instrument for t in tickets], f"pre-send read failed: {exc}")
        claimed: set[int] = set()
        for ticket in tickets:
            if self._halted:
                acks.append(self._ack(ticket, "REJECTED", f"venue {self.venue} is halted; not sent"))
                self._expected[ticket.instrument] = self._expected.get(ticket.instrument, ZERO) - _signed(ticket)
                continue
            ack = self._send(ticket)
            if ack.status == "REJECTED":
                self._expected[ticket.instrument] = self._expected.get(ticket.instrument, ZERO) - _signed(ticket)
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
                self._expected[ticket.instrument] = self._expected.get(ticket.instrument, ZERO) - _signed(ticket)
            acks.append(self._ack(ticket, status, f"{ack.message}; {reason}" if status == "PENDING" else reason))
            before = position_book(positions)
        return self._finish(acks, [t.instrument for t in tickets], None)

    def _finish(self, acks: list[VenueAck], contracts: list[Instrument], failed: str | None) -> DrainReport:
        now = self._clock.now_utc()
        if failed is not None:
            event = unreadable(self.venue, now, contracts + list(self._expected), failed)
        else:
            event = self.reconcile_now()
        if not event.reconciled:
            self._halted = True
        return DrainReport(acks=tuple(acks), reconcile=event)

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
        self._expected[ticket.instrument] = self._expected.get(ticket.instrument, ZERO) - _signed(ticket)

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
        raise TosPaperBrokerError("venue fill read-back is not wired yet (Monitor tab/JAB or RTD)")

    def positions(self) -> list[VenuePosition]:
        self._require_connected("positions")
        return self._read_positions()

    def cash_events(self, since: datetime) -> list[VenueCashEvent]:
        raise TosPaperBrokerError("venue cash events (assignment/exercise) cannot be read yet")


def _signed(order: VenueOrder) -> Decimal:
    return order.quantity if order.side is Side.BUY else -order.quantity

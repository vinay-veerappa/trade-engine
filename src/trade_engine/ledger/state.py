"""AccountState and the pure fold over ledger events (Architecture §4.2, I2).

`fold()` is a pure function: events in, state out, nothing mutated in place. A restart
replays the log and arrives at the same state, which is the point of I2. A snapshot cache
is only ever an optimisation — correctness is defined by `fold()`.

Option expiry, exercise and assignment fold here from ``OptionLifecycle`` events (O2,
``domain.option_lifecycle``). A combo order folds leg by leg (O4): each fill names its
leg and becomes a position in that leg's contract. Dividends arrive as ``CashFlow``
events. Kinds no work package owns yet (other corporate actions) are refused rather
than guessed (I5): a ledger containing one cannot be folded, loudly.

A venue mirror's ``Mirror*`` events (T2) fold into ``AccountState.mirror`` of the
venue's own ledger account (``ledger.mirror``), never into sim positions or cash: only
``Fill`` events move a book of record.

Since P2b the fold is Rust's (``te_core::ledger::fold``, one rule set, no Python twin).
The dataclasses here are carriers: built from Rust's state when it is read, holding no
rule. An incremental fold (the store, the reader, :class:`FoldCache`) holds its state in
a Rust handle and applies each event ONCE; Python patches its carrier from the delta.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from decimal import Decimal
from types import MappingProxyType
from typing import Any, Callable

from trade_engine.domain.instruments import Instrument
from trade_engine.domain.orders import Order
from trade_engine.domain.portfolio import Fill, Position
from trade_engine.ledger import _rs, codec
from trade_engine.ledger import mirror as _mirror
from trade_engine.ledger.errors import (
    LedgerDuplicateFillError,
    LedgerFillMismatchError,
    LedgerFoldError,
)
from trade_engine.ledger.events import (
    EmulatedOrderState,
    Event,
    EventKind,
    VenueReconcile,
)
from trade_engine.ledger.mirror import MirrorState

ZERO = Decimal("0")


@dataclass(frozen=True)
class AccountState:
    """Folded state for one account. Every field is replaced, never mutated (I2)."""

    account_id: str
    cash: Decimal = ZERO
    positions: Mapping[Instrument, Position] = field(default_factory=lambda: MappingProxyType({}))
    orders: Mapping[str, Order] = field(default_factory=lambda: MappingProxyType({}))
    filled_quantity: Mapping[str, Decimal] = field(default_factory=lambda: MappingProxyType({}))
    # Contracts filled per (combo order id, leg index). A combo's filled_quantity is the
    # number of whole units every leg has completed.
    leg_filled: Mapping[tuple[str, int], Decimal] = field(
        default_factory=lambda: MappingProxyType({})
    )
    venue_order_ids: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))
    emulated_orders: Mapping[str, EmulatedOrderState] = field(default_factory=lambda: MappingProxyType({}))
    fills: tuple[Fill, ...] = ()
    fill_ids: frozenset[str] = frozenset()
    marks: Mapping[Instrument, Decimal] = field(default_factory=lambda: MappingProxyType({}))
    realized_pnl: Decimal = ZERO
    signals_seen: int = 0
    verdicts: int = 0
    refusals: int = 0
    last_reconcile: VenueReconcile | None = None
    venue_halted: bool = False
    # Venues (by venue key, e.g. a paperMoney account) a drifting reconcile has halted.
    # Sticky: a later clean reconcile never clears it (§4.5).
    halted_venues: frozenset[str] = frozenset()
    risk_controls: Mapping[str, bool] = field(default_factory=lambda: MappingProxyType({}))
    # A venue mirror (T2): populated only for a venue's ledger account (``mirror_account``).
    mirror: MirrorState = field(default_factory=MirrorState)
    last_seq: int = 0




codec.register_carrier(AccountState)


def apply_fill(
    account_id: str,
    position: Position | None,
    fill: Fill,
    multiplier: int,
) -> Position:
    """Return a new Position after applying one fill (FIFO lots, realized P&L on close)."""
    data = _rs.call(
        _rs.rs.ledger_apply_fill,
        account_id,
        None if position is None else codec.text(codec.canon(position)),
        codec.text(codec._encode(fill)),
        multiplier,
    )
    return codec.build_text(data)


def apply_event(state: AccountState, event: Event) -> AccountState:
    """Fold one event into its account's state: the single step fold(), FoldCache and
    Ledger.append share, so all three agree by construction. Pure (I2)."""
    data = _rs.call(_rs.rs.ledger_apply_event, codec.text(codec.canon(state)), codec.event_bytes(event))
    return codec.build_text(data)


def _rust_handler(state: AccountState, event: Event) -> AccountState:
    """A kind's fold step without the seq stamp ``apply_event`` adds (what a handler was)."""
    return replace(apply_event(state, event), last_seq=state.last_seq)


# Event kinds the fold knows. Everything else refuses (see FOLD_OWNERS). Read-only since
# P2b: every value is the Rust fold, which owns the rule for each kind.
HANDLERS: Mapping[EventKind, Callable[[AccountState, Event], AccountState]] = MappingProxyType(
    {
        kind: _rust_handler
        for kind in (
            EventKind.SIGNAL_SEEN,
            EventKind.RISK_VERDICT,
            EventKind.ORDERS_CREATED,
            EventKind.RISK_CONTROL,
            EventKind.ORDER_SUBMITTED,
            EventKind.ORDER_UPDATED,
            EventKind.ORDER_PENDING,
            EventKind.ORDER_ACCEPTED,
            EventKind.ORDER_REJECTED,
            EventKind.ORDER_CANCELLED,
            EventKind.ORDER_REFUSED,
            EventKind.ORDER_EXPIRED,
            EventKind.ORDER_EMULATION_UPDATED,
            EventKind.FILL,
            EventKind.CASH_FLOW,
            EventKind.MARK,
            EventKind.VENUE_RECONCILE,
            EventKind.VENUE_HALT_CLEARED,
            EventKind.EOD_RUN,
            EventKind.EXPIRY,
            EventKind.EXERCISE,
            EventKind.ASSIGNMENT,
            EventKind.MIRROR_QUEUED,
            EventKind.MIRROR_REFUSED,
            EventKind.MIRROR_ACK,
            EventKind.MIRROR_FILL,
        )
    }
)


def register_handler(kind: EventKind, handler: Callable[[AccountState, Event], AccountState]) -> None:
    """Refused since P2b: a fold rule is Rust's, and a Python handler would be a second
    fold the store, the reader and every restart would not run."""
    if kind in HANDLERS:
        raise LedgerFoldError(f"A handler for {kind.value} is already registered")
    raise LedgerFoldError(
        f"Cannot register a Python fold handler for {kind.value}: the fold is Rust's "
        f"(te_core::ledger::fold) since P2b; add the kind there (I5)"
    )


def halted_venues(states: Mapping[str, AccountState]) -> frozenset[str]:
    """Every venue any account's reconcile has halted: the halt is per venue, not per
    ledger account, so a drift recorded under one account halts the venue for all (§4.5)."""
    halted: frozenset[str] = frozenset()
    for state in states.values():
        halted |= state.halted_venues
    return halted


def mirror_state(states: Mapping[str, AccountState], venue: str) -> MirrorState:
    """The venue's mirror from a fold (T2): empty when the venue was never mirrored."""
    return _mirror.mirror_states(states, venue)


def fold(events: Iterable[Event]) -> dict[str, AccountState]:
    """Fold a full event log into per-account state. Pure (I2)."""
    data = _rs.call(_rs.rs.ledger_fold_all, [codec.event_bytes(e) for e in events])
    return {codec.build(k): codec.build(v) for k, v in json.loads(data)["m"]}


def fold_account(events: Iterable[Event], account: str) -> AccountState:
    """Fold events for one account (ignoring any others). Pure (I2)."""
    data = _rs.call(_rs.rs.ledger_fold, [codec.event_bytes(e) for e in events], account)
    return codec.build_text(data)


class IncrementalFold:
    """Folded state per account held in Rust (``trade_engine_rs.LedgerFold``), each event
    applied once, with the Python carrier materialised only when read (P2b).

    ``atomic``: a refused event leaves its account as it was (``FoldCache``). Otherwise a
    refused account is dropped, and its owner reloads it from the log (the store, whose
    transaction rolled back). The carrier cache follows the handle: an account the handle
    lost is never served from it.
    """

    __slots__ = ("_handle", "_carriers", "_stale")

    def __init__(self, *, atomic: bool) -> None:
        self._handle = _rs.LedgerFold(atomic)
        self._carriers: dict[str, AccountState] = {}
        self._stale: set[str] = set()

    def has(self, account: str) -> bool:
        return self._handle.has(account)

    @property
    def handle(self):
        """The Rust fold itself, for Rust callers that read a state without a carrier."""
        return self._handle

    def accounts(self) -> list[str]:
        return self._handle.accounts()

    def drop(self, account: str) -> None:
        self._handle.drop(account)
        self._carriers.pop(account, None)
        self._stale.discard(account)

    def clear(self) -> None:
        self._handle.clear()
        self._carriers.clear()
        self._stale.clear()

    def seed(self, account: str, state: AccountState) -> None:
        self.drop(account)
        _rs.call(self._handle.seed, account, codec.text(codec.canon(state)))
        self._carriers[account] = state

    def _guarded(self, account: str, fn: Callable[..., None], *args: Any) -> None:
        self._stale.add(account)
        try:
            _rs.call(fn, *args)
        finally:
            if not self._handle.has(account):
                self._carriers.pop(account, None)
                self._stale.discard(account)

    def load(self, account: str, rows: list[tuple]) -> None:
        """(Re)start an account from its stored rows, in seq order."""
        self._carriers.pop(account, None)
        self._guarded(account, self._handle.load, account, rows)

    def apply_rows(self, account: str, rows: list[tuple]) -> None:
        self._guarded(account, self._handle.apply_rows, account, rows)

    def apply_row(
        self, account: str, kind: str, payload_json: str, ts_utc: str, command_id: str | None, schema_version: int, seq: int
    ) -> None:
        self._guarded(account, self._handle.apply_row1, account, kind, payload_json, ts_utc, command_id, schema_version, seq)

    def apply_event(self, event: Event) -> None:
        self._guarded(event.account, self._handle.apply_event, codec.event_bytes(event))

    def state(self, account: str) -> AccountState:
        """The account's carrier; built (or patched) from Rust only if it moved."""
        carrier = self._carriers.get(account)
        if carrier is not None and account not in self._stale:
            return carrier
        whole, data = _rs.call(self._handle.export, account, carrier is None)
        tree = json.loads(data)
        carrier = codec.build(tree) if whole else codec.patch(carrier, tree)
        self._carriers[account] = carrier
        self._stale.discard(account)
        return carrier


class FoldCache:
    """Incremental snapshot of folded state, provably equal to a full fold.

    The cache is an optimisation only: `FoldCache.states()` must always equal
    `fold(ledger.events())`. `verify()` asserts exactly that.
    """

    def __init__(
        self,
        events: Iterable[Event] = (),
        *,
        seed: Mapping[str, AccountState] | None = None,
        base_seq: int = 0,
    ) -> None:
        self._events: list[Event] = []
        self._base_seq = base_seq
        self._fold = IncrementalFold(atomic=True)
        for account, state in (seed or {}).items():
            self._fold.seed(account, state)
        self.extend(events)

    @property
    def base_seq(self) -> int:
        """Highest seq already folded into the seed, if the cache was seeded."""
        return self._base_seq

    def extend(self, events: Iterable[Event]) -> None:
        for event in events:
            if event.seq is not None and event.seq <= self._base_seq:
                raise LedgerFoldError(
                    f"Event seq {event.seq} is already folded into the seed "
                    f"(base_seq={self._base_seq}); replaying it would double-apply "
                    f"the event (I3)"
                )
            self._events.append(event)
            self._fold.apply_event(event)

    @property
    def accounts(self) -> tuple[str, ...]:
        return tuple(sorted(self._fold.accounts()))

    def state(self, account: str) -> AccountState:
        if not self._fold.has(account):
            return AccountState(account_id=account)
        return self._fold.state(account)

    def states(self) -> dict[str, AccountState]:
        return {account: self._fold.state(account) for account in self._fold.accounts()}

    def verify(self, full_log: Iterable[Event] | None = None) -> None:
        """Raise AssertionError unless the cache equals a full fold.

        With a seeded cache the incremental events alone are not the whole log, so
        callers that seeded must pass `full_log`.
        """
        source = self._events if full_log is None else full_log
        full = fold(source)
        if full != self.states():
            raise AssertionError("FoldCache drifted from fold(events) (I2)")


__all__ = [
    "AccountState",
    "FoldCache",
    "HANDLERS",
    "LedgerDuplicateFillError",
    "LedgerFillMismatchError",
    "LedgerFoldError",
    "apply_event",
    "apply_fill",
    "fold",
    "fold_account",
    "halted_venues",
    "mirror_state",
    "register_handler",
]

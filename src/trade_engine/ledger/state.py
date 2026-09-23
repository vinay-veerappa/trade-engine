"""AccountState and the pure fold over ledger events (Architecture §4.2, I2).

`fold()` is a pure function: events in, state out, nothing mutated in place. A restart
replays the log and arrives at the same state, which is the point of I2. A snapshot cache
is only ever an optimisation — correctness is defined by `fold()`.

Kinds whose semantics belong to a later work package (option expiry, assignment,
exercise, corporate actions) are refused rather than guessed (I5). O2 registers real
handlers; until then a ledger containing one of those events cannot be folded, loudly.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from types import MappingProxyType
from typing import Any

from trade_engine.domain.instruments import Instrument, Side
from trade_engine.domain.orders import Order, OrderState
from trade_engine.domain.portfolio import Fill, Lot, Position
from trade_engine.ledger.events import (
    FOLD_OWNERS,
    CashFlow,
    Event,
    EventKind,
    LifecycleNotice,
    Mark,
    OrderStateChange,
    UnhandledEventError,
    VenueReconcile,
)

ZERO = Decimal("0")


class LedgerFoldError(RuntimeError):
    """Raised when a recorded event cannot be applied to state (I5)."""


@dataclass(frozen=True)
class AccountState:
    """Folded state for one account. Every field is replaced, never mutated (I2)."""

    account_id: str
    cash: Decimal = ZERO
    positions: Mapping[Instrument, Position] = field(default_factory=lambda: MappingProxyType({}))
    orders: Mapping[str, Order] = field(default_factory=lambda: MappingProxyType({}))
    filled_quantity: Mapping[str, Decimal] = field(default_factory=lambda: MappingProxyType({}))
    marks: Mapping[Instrument, Decimal] = field(default_factory=lambda: MappingProxyType({}))
    realized_pnl: Decimal = ZERO
    signals_seen: int = 0
    verdicts: int = 0
    refusals: int = 0
    last_reconcile: VenueReconcile | None = None
    venue_halted: bool = False
    last_seq: int = 0


def _multiplier(instrument: Instrument) -> int:
    try:
        return instrument.multiplier
    except ValueError as err:
        raise LedgerFoldError(
            f"Cannot value a mixed-multiplier combo in E1; per-leg accounting is O4's: {err}"
        ) from err


def _consume_lots(lots: list[Lot], quantity: Decimal) -> list[Lot]:
    """Remove `quantity` from the front of the lot list (FIFO), dropping emptied lots."""
    remaining = quantity
    kept: list[Lot] = []
    for lot in lots:
        if remaining <= ZERO:
            kept.append(lot)
            continue
        if lot.quantity <= remaining:
            remaining -= lot.quantity
        else:
            kept.append(
                Lot(
                    lot_id=lot.lot_id,
                    quantity=lot.quantity - remaining,
                    cost_basis=lot.cost_basis,
                    acquired_at=lot.acquired_at,
                    side=lot.side,
                )
            )
            remaining = ZERO
    if remaining != ZERO:
        raise LedgerFoldError(f"Open lots ran short by {remaining}; ledger is inconsistent (I2)")
    return kept


def apply_fill(
    account_id: str,
    position: Position | None,
    fill: Fill,
    multiplier: int,
) -> Position:
    """Return a new Position after applying one fill (FIFO lots, realized P&L on close)."""
    signed_delta = fill.quantity if fill.side == Side.BUY else -fill.quantity

    if position is None or position.quantity == ZERO:
        lot = Lot(
            lot_id=fill.fill_id,
            quantity=fill.quantity,
            cost_basis=fill.price,
            acquired_at=fill.filled_at,
            side=fill.side,
        )
        return Position(
            account_id=account_id,
            instrument=fill.instrument,
            quantity=signed_delta,
            avg_cost=fill.price,
            realized_pnl=ZERO,
            open_lots=(lot,),
        )

    old_qty = position.quantity
    old_lots = list(position.open_lots)
    avg = position.avg_cost
    realized = position.realized_pnl
    increasing = (old_qty > ZERO) == (signed_delta > ZERO)

    if increasing:
        new_qty = old_qty + signed_delta
        new_lot = Lot(
            lot_id=fill.fill_id,
            quantity=fill.quantity,
            cost_basis=fill.price,
            acquired_at=fill.filled_at,
            side=fill.side,
        )
        weighted = avg * abs(old_qty) + fill.price * fill.quantity
        new_avg = weighted / abs(new_qty)
        lots = [*old_lots, new_lot]
    else:
        closing = min(abs(signed_delta), abs(old_qty))
        if old_qty > ZERO:
            realized += (fill.price - avg) * closing * multiplier
        else:
            realized += (avg - fill.price) * closing * multiplier

        lots = _consume_lots(old_lots, closing)
        new_qty = old_qty + signed_delta

        if abs(signed_delta) > abs(old_qty):
            flip_qty = abs(signed_delta) - abs(old_qty)
            new_lot = Lot(
                lot_id=fill.fill_id,
                quantity=flip_qty,
                cost_basis=fill.price,
                acquired_at=fill.filled_at,
                side=fill.side,
            )
            lots = [new_lot]
            new_avg = fill.price
        elif new_qty == ZERO:
            new_avg = ZERO
        else:
            new_avg = avg

    return Position(
        account_id=account_id,
        instrument=fill.instrument,
        quantity=new_qty,
        avg_cost=new_avg,
        realized_pnl=realized,
        open_lots=tuple(lots),
    )


def _with_positions(state: AccountState, positions: Mapping[Instrument, Position]) -> AccountState:
    return _replace(state, positions=MappingProxyType(dict(positions)))


def _replace(state: AccountState, **changes: Any) -> AccountState:
    data = {
        "account_id": state.account_id,
        "cash": state.cash,
        "positions": state.positions,
        "orders": state.orders,
        "filled_quantity": state.filled_quantity,
        "marks": state.marks,
        "realized_pnl": state.realized_pnl,
        "signals_seen": state.signals_seen,
        "verdicts": state.verdicts,
        "refusals": state.refusals,
        "last_reconcile": state.last_reconcile,
        "venue_halted": state.venue_halted,
        "last_seq": state.last_seq,
    }
    data.update(changes)
    return AccountState(**data)


def _require_order(state: AccountState, order_id: str, kind: EventKind) -> Order:
    order = state.orders.get(order_id)
    if order is None:
        raise LedgerFoldError(
            f"{kind.value} references unknown order '{order_id}' in account "
            f"'{state.account_id}'; refusing to invent one (I5)"
        )
    return order


def _on_cash_flow(state: AccountState, event: Event) -> AccountState:
    payload: CashFlow = event.payload
    return _replace(state, cash=state.cash + payload.amount)


def _on_fill(state: AccountState, event: Event) -> AccountState:
    fill: Fill = event.payload
    multiplier = _multiplier(fill.instrument)
    gross = fill.quantity * fill.price * multiplier
    cash_delta = (gross if fill.side == Side.SELL else -gross) - fill.fee

    position = state.positions.get(fill.instrument)
    updated = apply_fill(state.account_id, position, fill, multiplier)
    positions = dict(state.positions)
    positions[fill.instrument] = updated

    filled = dict(state.filled_quantity)
    filled[fill.order_id] = filled.get(fill.order_id, ZERO) + fill.quantity

    orders = dict(state.orders)
    order = orders.get(fill.order_id)
    if order is None:
        raise LedgerFoldError(
            f"Fill {fill.fill_id} references unknown order '{fill.order_id}' (I5)"
        )
    total = filled[fill.order_id]
    if total > order.quantity:
        raise LedgerFoldError(
            f"Fill {fill.fill_id} would take order '{fill.order_id}' to {total} filled against "
            f"an order size of {order.quantity}; refusing the over-fill rather than inventing "
            f"an oversized order (I5)"
        )
    if order.state in (
        OrderState.CANCELLED,
        OrderState.EXPIRED,
        OrderState.REJECTED,
    ):
        raise LedgerFoldError(
            f"Fill {fill.fill_id} arrived for order '{fill.order_id}' already in terminal state "
            f"{order.state.value}; the log contradicts the order state (I5)"
        )
    if order.state is not OrderState.FILLED:
        next_state = OrderState.FILLED if total >= order.quantity else OrderState.PARTIALLY_FILLED
        if next_state != order.state:
            orders[fill.order_id] = order.transition_to(next_state)

    return _replace(
        state,
        cash=state.cash + cash_delta,
        positions=MappingProxyType(positions),
        orders=MappingProxyType(orders),
        filled_quantity=MappingProxyType(filled),
        realized_pnl=state.realized_pnl,
    )


def _on_order_submitted(state: AccountState, event: Event) -> AccountState:
    order: Order = event.payload
    if order.state == OrderState.NEW:
        # The event *is* the submission, so fold applies the NEW → SUBMITTED step. An
        # order already reported at a later state (e.g. a fast-fill venue) is kept as-is.
        order = order.transition_to(OrderState.SUBMITTED)
    orders = dict(state.orders)
    existing = orders.get(order.order_id)
    if existing is not None and existing.command_id != order.command_id:
        raise LedgerFoldError(
            f"Order '{order.order_id}' was already submitted under command "
            f"'{existing.command_id}'; refusing to overwrite it with '{order.command_id}' (I3)"
        )
    orders[order.order_id] = order
    return _replace(state, orders=MappingProxyType(orders))


def _order_state_handler(target: OrderState) -> Callable[[AccountState, Event], AccountState]:
    def handler(state: AccountState, event: Event) -> AccountState:
        change: OrderStateChange = event.payload
        order = _require_order(state, change.order_id, event.kind)
        orders = dict(state.orders)
        orders[change.order_id] = order.transition_to(target)
        return _replace(state, orders=MappingProxyType(orders))

    return handler


def _on_mark(state: AccountState, event: Event) -> AccountState:
    mark: Mark = event.payload
    marks = dict(state.marks)
    marks[mark.instrument] = mark.price
    return _replace(state, marks=MappingProxyType(marks))


def _on_venue_reconcile(state: AccountState, event: Event) -> AccountState:
    notice: VenueReconcile = event.payload
    return _replace(
        state,
        last_reconcile=notice,
        venue_halted=state.venue_halted or not notice.reconciled,
    )


def _on_signal_seen(state: AccountState, event: Event) -> AccountState:
    return _replace(state, signals_seen=state.signals_seen + 1)


def _on_risk_verdict(state: AccountState, event: Event) -> AccountState:
    accepted = bool(event.payload.accepted)
    return _replace(
        state,
        verdicts=state.verdicts + 1,
        refusals=state.refusals + (0 if accepted else 1),
    )


# Event kinds E1 knows how to fold. Everything else refuses (see FOLD_OWNERS).
HANDLERS: dict[EventKind, Callable[[AccountState, Event], AccountState]] = {
    EventKind.SIGNAL_SEEN: _on_signal_seen,
    EventKind.RISK_VERDICT: _on_risk_verdict,
    EventKind.ORDER_SUBMITTED: _on_order_submitted,
    EventKind.ORDER_ACCEPTED: _order_state_handler(OrderState.ACCEPTED),
    EventKind.ORDER_REJECTED: _order_state_handler(OrderState.REJECTED),
    EventKind.ORDER_CANCELLED: _order_state_handler(OrderState.CANCELLED),
    EventKind.ORDER_EXPIRED: _order_state_handler(OrderState.EXPIRED),
    EventKind.FILL: _on_fill,
    EventKind.CASH_FLOW: _on_cash_flow,
    EventKind.MARK: _on_mark,
    EventKind.VENUE_RECONCILE: _on_venue_reconcile,
}


def register_handler(kind: EventKind, handler: Callable[[AccountState, Event], AccountState]) -> None:
    """Register a fold handler for a kind another work package owns (e.g. O2 lifecycle)."""
    if kind in HANDLERS:
        raise LedgerFoldError(f"A handler for {kind.value} is already registered")
    HANDLERS[kind] = handler


def _dispatch(state: AccountState, event: Event) -> AccountState:
    handler = HANDLERS.get(event.kind)
    if handler is not None:
        return handler(state, event)

    owner = FOLD_OWNERS.get(event.kind)
    if owner is not None:
        raise UnhandledEventError(
            f"{event.kind.value} is owned by {owner}; E1 refuses to invent its ledger "
            f"semantics (I5)"
        )
    raise UnhandledEventError(f"No fold handler registered for {event.kind.value} (I5)")


def fold(events: Iterable[Event]) -> dict[str, AccountState]:
    """Fold a full event log into per-account state. Pure (I2)."""
    ordered = _ordered(events)
    states: dict[str, AccountState] = {}
    for event in ordered:
        current = states.get(event.account, AccountState(account_id=event.account))
        state = _dispatch(current, event)
        states[event.account] = _replace(
            state, last_seq=event.seq if event.seq is not None else state.last_seq
        )
    return states


def fold_account(events: Iterable[Event], account: str) -> AccountState:
    """Fold events for one account (ignoring any others). Pure (I2)."""
    subset = [e for e in _ordered(events) if e.account == account]
    states = fold(subset)
    return states.get(account, AccountState(account_id=account))


def _ordered(events: Iterable[Event]) -> list[Event]:
    materialised = list(events)
    if all(e.seq is not None for e in materialised):
        return sorted(materialised, key=lambda e: e.seq)
    return materialised


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
        self._states: dict[str, AccountState] = dict(seed or {})
        self.extend(events)

    @property
    def base_seq(self) -> int:
        """Highest seq already folded into the seed, if the cache was seeded."""
        return self._base_seq

    def extend(self, events: Iterable[Event]) -> None:
        for event in events:
            self._events.append(event)
            current = self._states.get(event.account, AccountState(account_id=event.account))
            state = _dispatch(current, event)
            self._states[event.account] = _replace(
                state, last_seq=event.seq if event.seq is not None else state.last_seq
            )

    @property
    def accounts(self) -> tuple[str, ...]:
        return tuple(sorted(self._states))

    def state(self, account: str) -> AccountState:
        return self._states.get(account, AccountState(account_id=account))

    def states(self) -> dict[str, AccountState]:
        return dict(self._states)

    def verify(self, full_log: Iterable[Event] | None = None) -> None:
        """Raise AssertionError unless the cache equals a full fold.

        With a seeded cache the incremental events alone are not the whole log, so
        callers that seeded must pass `full_log`.
        """
        source = self._events if full_log is None else full_log
        full = fold(source)
        if full != self._states:
            raise AssertionError("FoldCache drifted from fold(events) (I2)")


__all__ = [
    "AccountState",
    "FoldCache",
    "HANDLERS",
    "LedgerFoldError",
    "apply_fill",
    "fold",
    "fold_account",
    "register_handler",
]

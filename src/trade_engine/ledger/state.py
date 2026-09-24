"""AccountState and the pure fold over ledger events (Architecture §4.2, I2).

`fold()` is a pure function: events in, state out, nothing mutated in place. A restart
replays the log and arrives at the same state, which is the point of I2. A snapshot cache
is only ever an optimisation — correctness is defined by `fold()`.

Option expiry, exercise and assignment fold here from ``OptionLifecycle`` events (O2,
``domain.option_lifecycle``). Kinds no work package owns yet (corporate actions) are
refused rather than guessed (I5): a ledger containing one cannot be folded, loudly.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from decimal import Decimal
from types import MappingProxyType
from typing import Any

from trade_engine.domain.instruments import Instrument, Side, UnresolvableInstrumentError
from trade_engine.domain.option_lifecycle import (
    EXERCISE_THRESHOLD,
    can_exercise_early,
    deliverable,
    delivery,
    intrinsic,
    is_cash_settled,
)
from trade_engine.domain.orders import (
    IllegalOrderStateTransitionError,
    Order,
    OrderState,
    OrderType,
    validate_order_transition,
)
from trade_engine.domain.portfolio import Fill, Lot, Position
from trade_engine.ledger.events import (
    FOLD_OWNERS,
    CashFlow,
    EmulatedOrderState,
    Event,
    EventKind,
    Mark,
    OptionLifecycle,
    OrderUpdated,
    OrderStateChange,
    OrdersCreated,
    RiskControlChange,
    UnhandledEventError,
    VenueReconcile,
)

ZERO = Decimal("0")


class LedgerFoldError(RuntimeError):
    """Raised when a recorded event cannot be applied to state (I5)."""


class LedgerFillMismatchError(LedgerFoldError):
    """A fill that contradicts its order's instrument or side (I1, I5)."""


class LedgerDuplicateFillError(LedgerFoldError):
    """A venue fill replayed under a new command id (I3)."""


@dataclass(frozen=True)
class AccountState:
    """Folded state for one account. Every field is replaced, never mutated (I2)."""

    account_id: str
    cash: Decimal = ZERO
    positions: Mapping[Instrument, Position] = field(default_factory=lambda: MappingProxyType({}))
    orders: Mapping[str, Order] = field(default_factory=lambda: MappingProxyType({}))
    filled_quantity: Mapping[str, Decimal] = field(default_factory=lambda: MappingProxyType({}))
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
    risk_controls: Mapping[str, bool] = field(default_factory=lambda: MappingProxyType({}))
    last_seq: int = 0


def _require_finite(value: Decimal, name: str) -> Decimal:
    """Refuse a non-finite quantity, price or fee before it poisons the fold.

    A NaN or Infinity in a fill would propagate into cash and realised P&L, and NaN
    breaks state equality itself (NaN != NaN), so snapshot == fold could never be
    asserted again. The domain objects guard sign, not finiteness, and only the codec
    refuses non-finite values, so in-memory folds were unguarded (I5).
    """
    if not isinstance(value, Decimal) or not value.is_finite():
        raise LedgerFoldError(f"{name} must be a finite Decimal, got {value} (I5)")
    return value


def _multiplier(instrument: Instrument) -> int:
    try:
        return instrument.multiplier
    except ValueError as err:
        raise LedgerFoldError(
            f"Cannot value a mixed-multiplier combo in E1; per-leg accounting is O4's: {err}"
        ) from err


def _check_fill_finite(fill: Fill) -> None:
    _require_finite(fill.quantity, f"Fill {fill.fill_id} quantity")
    _require_finite(fill.price, f"Fill {fill.fill_id} price")
    _require_finite(fill.fee, f"Fill {fill.fill_id} fee")


def _consume_lots(lots: list[Lot], quantity: Decimal, exit_price: Decimal, multiplier: int) -> tuple[list[Lot], Decimal]:
    """Close `quantity` from the front of the lot list (FIFO), returning kept lots and
    the realised P&L of the closed quantity.

    Realised P&L must come from the *consumed lots'* own cost bases, not the position's
    average cost: the lots are FIFO, so an average-cost figure splits the same total
    profit differently across partial closes, and every per-lot R / MFE-MAE number
    computed downstream (E8) inherits the error (I11).
    """
    remaining = quantity
    realized = ZERO
    kept: list[Lot] = []
    for lot in lots:
        if remaining <= ZERO:
            kept.append(lot)
            continue
        if lot.quantity <= remaining:
            closed = lot.quantity
            remaining -= closed
        else:
            closed = remaining
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
        if lot.side is Side.BUY:
            realized += (exit_price - lot.cost_basis) * closed * multiplier
        else:
            realized += (lot.cost_basis - exit_price) * closed * multiplier
    if remaining != ZERO:
        raise LedgerFoldError(f"Open lots ran short by {remaining}; ledger is inconsistent (I2)")
    return kept, realized


def apply_fill(
    account_id: str,
    position: Position | None,
    fill: Fill,
    multiplier: int,
) -> Position:
    """Return a new Position after applying one fill (FIFO lots, realized P&L on close)."""
    return _apply_trade(
        account_id,
        position,
        fill.instrument,
        fill.side,
        fill.quantity,
        fill.price,
        fill.filled_at,
        fill.fill_id,
        multiplier,
    )


def _apply_trade(
    account_id: str,
    position: Position | None,
    instrument: Instrument,
    side: Side,
    quantity: Decimal,
    price: Decimal,
    at: datetime,
    lot_id: str,
    multiplier: int,
) -> Position:
    """One trade into a position: a fill, or the share delivery of an exercise or
    assignment. New quantity opens a lot named ``lot_id``; opposite quantity closes the
    oldest lots first."""
    signed_delta = quantity if side == Side.BUY else -quantity

    if position is None or position.quantity == ZERO:
        # A flat position keeps its realised history: P&L already booked must survive
        # the next round trip, or the account total drifts away from cash (I11) and a
        # strategy that closes and re-opens looks less profitable than it is.
        carried_realized = position.realized_pnl if position is not None else ZERO
        lot = Lot(
            lot_id=lot_id,
            quantity=quantity,
            cost_basis=price,
            acquired_at=at,
            side=side,
        )
        return Position(
            account_id=account_id,
            instrument=instrument,
            quantity=signed_delta,
            avg_cost=price,
            realized_pnl=carried_realized,
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
            lot_id=lot_id,
            quantity=quantity,
            cost_basis=price,
            acquired_at=at,
            side=side,
        )
        weighted = avg * abs(old_qty) + price * quantity
        new_avg = weighted / abs(new_qty)
        lots = [*old_lots, new_lot]
    else:
        closing = min(abs(signed_delta), abs(old_qty))
        lots, close_pnl = _consume_lots(old_lots, closing, price, multiplier)
        realized += close_pnl
        new_qty = old_qty + signed_delta

        if abs(signed_delta) > abs(old_qty):
            flip_qty = abs(signed_delta) - abs(old_qty)
            new_lot = Lot(
                lot_id=lot_id,
                quantity=flip_qty,
                cost_basis=price,
                acquired_at=at,
                side=side,
            )
            lots = [new_lot]
            new_avg = price
        elif new_qty == ZERO:
            new_avg = ZERO
        else:
            # Remaining lots keep their own bases; the position average is the weighted
            # mean of what is actually still open, so it never contradicts the lots.
            new_avg = sum((lot.cost_basis * lot.quantity for lot in lots), ZERO) / abs(new_qty)

    return Position(
        account_id=account_id,
        instrument=instrument,
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
        "venue_order_ids": state.venue_order_ids,
        "emulated_orders": state.emulated_orders,
        "fills": state.fills,
        "fill_ids": state.fill_ids,
        "marks": state.marks,
        "realized_pnl": state.realized_pnl,
        "signals_seen": state.signals_seen,
        "verdicts": state.verdicts,
        "refusals": state.refusals,
        "last_reconcile": state.last_reconcile,
        "venue_halted": state.venue_halted,
        "risk_controls": state.risk_controls,
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
    order = state.orders.get(fill.order_id)
    if order is None:
        raise LedgerFoldError(
            f"Fill {fill.fill_id} references unknown order '{fill.order_id}' (I5)"
        )

    # A fill that disagrees with its order would silently invent or reverse a position,
    # the exact corruption the ledger exists to make impossible (I1). The venue ids are
    # provenance; the order is the record of intent.
    if fill.instrument != order.instrument:
        raise LedgerFillMismatchError(
            f"Fill {fill.fill_id} on instrument {fill.instrument.symbol} was filed against "
            f"order '{fill.order_id}' for {order.instrument.symbol}; the log contradicts "
            f"the order (I5)"
        )
    if fill.side is not order.side:
        raise LedgerFillMismatchError(
            f"Fill {fill.fill_id} is a {fill.side.value} but its order '{fill.order_id}' is a "
            f"{order.side.value}; refusing to apply the wrong direction (I5)"
        )

    if fill.fill_id in state.fill_ids:
        raise LedgerDuplicateFillError(
            f"Fill '{fill.fill_id}' is already in the ledger for account "
            f"'{state.account_id}'; a replayed venue fill is a no-op, not a second "
            f"position (I3)"
        )

    _check_fill_finite(fill)

    multiplier = _multiplier(fill.instrument)
    gross = fill.quantity * fill.price * multiplier
    cash_delta = (gross if fill.side == Side.SELL else -gross) - fill.fee

    position = state.positions.get(fill.instrument)
    prior_realized = position.realized_pnl if position is not None else ZERO
    updated = apply_fill(state.account_id, position, fill, multiplier)
    # Fees are a realised cost of the trade that produced them, so they reduce realised
    # P&L alongside the cash they already reduced. Without this, E8's profit factor
    # overstates by every fee (I11: the cash and the P&L must tell the same story).
    updated = replace(updated, realized_pnl=updated.realized_pnl - fill.fee)
    positions = dict(state.positions)
    positions[fill.instrument] = updated

    filled = dict(state.filled_quantity)
    filled[fill.order_id] = filled.get(fill.order_id, ZERO) + fill.quantity

    orders = dict(state.orders)
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
        realized_pnl=state.realized_pnl + (updated.realized_pnl - prior_realized),
        fills=state.fills + (fill,),
        fill_ids=state.fill_ids | {fill.fill_id},
    )


def _take_lots(lots: tuple[Lot, ...], quantity: Decimal) -> tuple[list[Lot], list[Lot]]:
    """Split ``quantity`` off the front of the lots (FIFO): (kept, taken)."""
    remaining = quantity
    kept: list[Lot] = []
    taken: list[Lot] = []
    for lot in lots:
        if remaining <= ZERO:
            kept.append(lot)
            continue
        part = min(lot.quantity, remaining)
        taken.append(replace(lot, quantity=part))
        if part < lot.quantity:
            kept.append(replace(lot, quantity=lot.quantity - part))
        remaining -= part
    if remaining != ZERO:
        raise LedgerFoldError(f"Open lots ran short by {remaining}; ledger is inconsistent (I2)")
    return kept, taken


def _remaining(position: Position, lots: list[Lot], realized: Decimal) -> Position:
    """The position left holding ``lots`` (flat when none), its realised P&L updated."""
    quantity = sum((lot.quantity if lot.side is Side.BUY else -lot.quantity for lot in lots), ZERO)
    avg = (
        sum((lot.cost_basis * lot.quantity for lot in lots), ZERO) / abs(quantity)
        if quantity != ZERO
        else ZERO
    )
    return replace(
        position, quantity=quantity, avg_cost=avg, realized_pnl=realized, open_lots=tuple(lots)
    )


def _lifecycle_handler(kind: EventKind) -> Callable[[AccountState, Event], AccountState]:
    """Fold an expiry, exercise or assignment (``domain.option_lifecycle``; I9).

    The event names the contract, the contracts settled and the price decided on; the
    effect comes from the position's own lots, so it cannot disagree with the book. An
    event that contradicts the book or the rules refuses (I5): no position or the wrong
    side, more contracts than are held, an in-the-money contract expiring worthless, an
    out-of-the-money one exercised or assigned, a European contract assigned early.
    """

    def handler(state: AccountState, event: Event) -> AccountState:
        notice: OptionLifecycle = event.payload
        contract = notice.contract
        label = f"{kind.value} of {contract.occ.strip()} in '{state.account_id}'"
        position = state.positions.get(contract)
        if position is None or position.quantity == ZERO:
            raise LedgerFoldError(f"{label}: no open position to settle (I5)")
        held = Side.BUY if position.quantity > ZERO else Side.SELL
        if held is not notice.held:
            raise LedgerFoldError(
                f"{label}: the event says the contracts are held {notice.held.value} but the "
                f"book holds them {held.value} (I5)"
            )
        if notice.quantity > abs(position.quantity):
            raise LedgerFoldError(
                f"{label}: {notice.quantity} contracts settled but {abs(position.quantity)} "
                f"are held (I5)"
            )
        try:
            value = intrinsic(contract, notice.underlying_price)
            cash_settled = is_cash_settled(contract)
            american = can_exercise_early(contract)
        except (ValueError, UnresolvableInstrumentError) as err:
            raise LedgerFoldError(f"{label}: {err}") from err

        if kind is EventKind.EXPIRY:
            if notice.early:
                raise LedgerFoldError(f"{label}: a contract expires only at its expiry (I9)")
            if value >= EXERCISE_THRESHOLD:
                raise LedgerFoldError(
                    f"{label}: {value} in the money at {notice.underlying_price}; it is "
                    f"exercised or assigned, not expired worthless (I9)"
                )
        else:
            wanted = Side.BUY if kind is EventKind.EXERCISE else Side.SELL
            if held is not wanted:
                raise LedgerFoldError(
                    f"{label}: {kind.value} applies to "
                    f"{'long' if wanted is Side.BUY else 'short'} contracts; these are held "
                    f"{held.value} (I5)"
                )
            if value < EXERCISE_THRESHOLD:
                raise LedgerFoldError(
                    f"{label}: not in the money at {notice.underlying_price}; nobody "
                    f"exercises it (I9)"
                )
            if notice.early and not american:
                raise LedgerFoldError(f"{label}: a European contract cannot be assigned early (I5)")

        multiplier = _multiplier(contract)
        positions = dict(state.positions)
        cash = state.cash
        realized = ZERO
        if kind is EventKind.EXPIRY or cash_settled:
            # Worthless at zero; cash-settled at its intrinsic value, paid in cash.
            exit_price = ZERO if kind is EventKind.EXPIRY else value
            kept, closed_pnl = _consume_lots(
                list(position.open_lots), notice.quantity, exit_price, multiplier
            )
            realized += closed_pnl
            positions[contract] = _remaining(position, kept, position.realized_pnl + closed_pnl)
            amount = exit_price * notice.quantity * multiplier
            cash += amount if held is Side.BUY else -amount
        else:
            # Physical: each lot's premium goes into the price of the shares it delivers,
            # and the option leg closes with no P&L of its own (domain.option_lifecycle).
            kept, taken = _take_lots(position.open_lots, notice.quantity)
            positions[contract] = _remaining(position, kept, position.realized_pnl)
            try:
                shares_instrument = deliverable(contract)
            except ValueError as err:
                raise LedgerFoldError(f"{label}: {err}") from err
            for lot in taken:
                try:
                    side, price = delivery(contract, held, lot.cost_basis)
                except ValueError as err:
                    raise LedgerFoldError(f"{label}: {err}") from err
                shares = lot.quantity * multiplier
                shares_position = positions.get(shares_instrument)
                prior = shares_position.realized_pnl if shares_position is not None else ZERO
                updated = _apply_trade(
                    state.account_id,
                    shares_position,
                    shares_instrument,
                    side,
                    shares,
                    price,
                    notice.as_of,
                    f"{kind.value}:{notice.as_of.isoformat()}:{lot.lot_id}",
                    _multiplier(shares_instrument),
                )
                positions[shares_instrument] = updated
                realized += updated.realized_pnl - prior
                paid = contract.strike * shares
                cash += -paid if side is Side.BUY else paid
        return _replace(
            state,
            cash=cash,
            positions=MappingProxyType(positions),
            realized_pnl=state.realized_pnl + realized,
        )

    return handler


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
    if existing is not None and existing.state is not OrderState.SUBMITTED:
        # A second OrderSubmitted for an order the log has already moved on (accepted,
        # filled, cancelled) would reset it to SUBMITTED while its fills and position
        # stay, so the order record would contradict the rest of the state (I2).
        raise LedgerFoldError(
            f"Order '{order.order_id}' is already {existing.state.value}; refusing a second "
            f"OrderSubmitted that would reset it (I2)"
        )
    orders[order.order_id] = order
    return _replace(state, orders=MappingProxyType(orders))


def _on_orders_created(state: AccountState, event: Event) -> AccountState:
    created: OrdersCreated = event.payload
    orders = dict(state.orders)
    for order in created.orders:
        existing = orders.get(order.order_id)
        if existing is not None and existing != order:
            raise LedgerFoldError(
                f"Order '{order.order_id}' already exists with a different payload; "
                "refusing to overwrite it (I3)"
            )
        orders[order.order_id] = order
    return _replace(state, orders=MappingProxyType(orders))


def _on_order_updated(state: AccountState, event: Event) -> AccountState:
    update: OrderUpdated = event.payload
    previous = _require_order(state, update.order.order_id, event.kind)
    current = update.order
    identity_fields = ("account_id", "instrument", "side", "command_id", "parent_order_id", "oco_group")
    if any(getattr(previous, name) != getattr(current, name) for name in identity_fields):
        raise LedgerFoldError(
            f"OrderUpdated changed immutable identity fields for '{current.order_id}' (I5)"
        )
    if current.state != previous.state:
        try:
            validate_order_transition(previous.state, current.state)
        except IllegalOrderStateTransitionError as err:
            raise LedgerFoldError(str(err)) from err
    filled = state.filled_quantity.get(current.order_id, ZERO)
    if current.quantity < filled:
        raise LedgerFoldError(
            f"OrderUpdated quantity {current.quantity} is below already-filled quantity "
            f"{filled} for '{current.order_id}'"
        )
    orders = dict(state.orders)
    orders[current.order_id] = current
    venue_ids = dict(state.venue_order_ids)
    if update.venue_order_id is not None:
        venue_ids[current.order_id] = update.venue_order_id
    emulated_orders = dict(state.emulated_orders)
    emulation = emulated_orders.get(current.order_id)
    if (
        emulation is not None
        and current.order_type in (OrderType.STOP, OrderType.STOP_LIMIT)
        and current.stop_price != previous.stop_price
    ):
        if emulation.triggered or current.stop_price is None:
            raise LedgerFoldError(
                f"Cannot change stop price for triggered emulated order '{current.order_id}' (I5)"
            )
        emulated_orders[current.order_id] = EmulatedOrderState(
            order_id=current.order_id,
            observed_price=emulation.observed_price,
            extreme=None,
            stop_price=current.stop_price,
            triggered=False,
            reason=update.reason,
        )
    return _replace(
        state,
        orders=MappingProxyType(orders),
        venue_order_ids=MappingProxyType(venue_ids),
        emulated_orders=MappingProxyType(emulated_orders),
    )


def _order_state_handler(target: OrderState) -> Callable[[AccountState, Event], AccountState]:
    def handler(state: AccountState, event: Event) -> AccountState:
        change: OrderStateChange = event.payload
        order = _require_order(state, change.order_id, event.kind)
        orders = dict(state.orders)
        orders[change.order_id] = order.transition_to(target)
        venue_ids = dict(state.venue_order_ids)
        if change.venue_order_id is not None:
            venue_ids[change.order_id] = change.venue_order_id
        return _replace(
            state,
            orders=MappingProxyType(orders),
            venue_order_ids=MappingProxyType(venue_ids),
        )
    return handler


def _on_order_refused(state: AccountState, event: Event) -> AccountState:
    change: OrderStateChange = event.payload
    _require_order(state, change.order_id, event.kind)
    return _replace(state, refusals=state.refusals + 1)


def _on_emulated_order_updated(state: AccountState, event: Event) -> AccountState:
    emulation: EmulatedOrderState = event.payload
    _require_order(state, emulation.order_id, event.kind)
    emulated_orders = dict(state.emulated_orders)
    emulated_orders[emulation.order_id] = emulation
    return _replace(state, emulated_orders=MappingProxyType(emulated_orders))


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


def _on_risk_control(state: AccountState, event: Event) -> AccountState:
    control: RiskControlChange = event.payload
    controls = dict(state.risk_controls)
    controls[control.control_id] = control.enabled
    return _replace(state, risk_controls=MappingProxyType(controls))


def _on_eod_run(state: AccountState, event: Event) -> AccountState:
    # Scheduler provenance only: "was this session run?" is answered by reading the
    # ledger, so the fold has nothing to add. Returning state unchanged keeps the event
    # foldable without inventing state for it (I2, I5).
    return state


# Event kinds E1 knows how to fold. Everything else refuses (see FOLD_OWNERS).
HANDLERS: dict[EventKind, Callable[[AccountState, Event], AccountState]] = {
    EventKind.SIGNAL_SEEN: _on_signal_seen,
    EventKind.RISK_VERDICT: _on_risk_verdict,
    EventKind.ORDERS_CREATED: _on_orders_created,
    EventKind.RISK_CONTROL: _on_risk_control,
    EventKind.ORDER_SUBMITTED: _on_order_submitted,
    EventKind.ORDER_UPDATED: _on_order_updated,
    EventKind.ORDER_PENDING: _order_state_handler(OrderState.PENDING_UNKNOWN),
    EventKind.ORDER_ACCEPTED: _order_state_handler(OrderState.ACCEPTED),
    EventKind.ORDER_REJECTED: _order_state_handler(OrderState.REJECTED),
    EventKind.ORDER_CANCELLED: _order_state_handler(OrderState.CANCELLED),
    EventKind.ORDER_REFUSED: _on_order_refused,
    EventKind.ORDER_EXPIRED: _order_state_handler(OrderState.EXPIRED),
    EventKind.ORDER_EMULATION_UPDATED: _on_emulated_order_updated,
    EventKind.FILL: _on_fill,
    EventKind.CASH_FLOW: _on_cash_flow,
    EventKind.MARK: _on_mark,
    EventKind.VENUE_RECONCILE: _on_venue_reconcile,
    EventKind.EOD_RUN: _on_eod_run,
    EventKind.EXPIRY: _lifecycle_handler(EventKind.EXPIRY),
    EventKind.EXERCISE: _lifecycle_handler(EventKind.EXERCISE),
    EventKind.ASSIGNMENT: _lifecycle_handler(EventKind.ASSIGNMENT),
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


def apply_event(state: AccountState, event: Event) -> AccountState:
    """Fold one event into its account's state: the single step fold(), FoldCache and
    Ledger.append share, so all three agree by construction. Pure (I2)."""
    new_state = _dispatch(state, event)
    return _replace(
        new_state, last_seq=event.seq if event.seq is not None else new_state.last_seq
    )


def fold(events: Iterable[Event]) -> dict[str, AccountState]:
    """Fold a full event log into per-account state. Pure (I2)."""
    states: dict[str, AccountState] = {}
    for event in _ordered(events):
        current = states.get(event.account, AccountState(account_id=event.account))
        states[event.account] = apply_event(current, event)
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
            if event.seq is not None and event.seq <= self._base_seq:
                raise LedgerFoldError(
                    f"Event seq {event.seq} is already folded into the seed "
                    f"(base_seq={self._base_seq}); replaying it would double-apply "
                    f"the event (I3)"
                )
            self._events.append(event)
            current = self._states.get(event.account, AccountState(account_id=event.account))
            self._states[event.account] = apply_event(current, event)

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
    "LedgerDuplicateFillError",
    "LedgerFillMismatchError",
    "LedgerFoldError",
    "apply_event",
    "apply_fill",
    "fold",
    "fold_account",
    "register_handler",
]

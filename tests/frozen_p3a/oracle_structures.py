# FROZEN ORACLE, DO NOT EDIT. Verbatim from commit b3ea2f7: src/trade_engine/oms/options.py
# (lines 61-62 and 81-222: open_structures, uncovered_calls and their private helpers, the pre-port
# Python implementation, P3a). The import block is new: only the names these lines use.
from __future__ import annotations

from decimal import Decimal

from trade_engine.domain.instruments import Equity, Instrument, OptionContract, OptionRight, Side
from trade_engine.domain.option_orders import OpenStructure, StructureLeg, is_structure, legs_of
from trade_engine.domain.option_roots import option_style
from trade_engine.domain.orders import Order, OrderState
from trade_engine.ledger.state import AccountState

ZERO = Decimal("0")
_TERMINAL = frozenset({OrderState.FILLED, OrderState.CANCELLED, OrderState.REJECTED, OrderState.EXPIRED})


def _target_id(entry_order_id: str) -> str:
    return f"{entry_order_id}:target"


def _underlying(instrument: Instrument) -> str:
    if isinstance(instrument, Equity):
        return instrument.symbol
    return option_style(instrument.underlying).underlying


def _entry_price(state: AccountState, entry: Order, units: Decimal) -> Decimal:
    """Net per unit in option points, positive: the credit collected or the debit paid."""
    fills = [fill for fill in state.fills if fill.order_id == entry.order_id]
    scale = next(leg.contract.multiplier for leg in legs_of(entry.instrument, entry.side))
    collected = sum(
        (
            (fill.price if fill.side is Side.SELL else -fill.price) * fill.quantity * fill.instrument.multiplier
            for fill in fills
        ),
        ZERO,
    ) / (units * scale)
    return collected if entry.side is Side.SELL else -collected


def open_structures(state: AccountState) -> tuple[OpenStructure, ...]:
    """Every structure in ``state`` with a leg still held, in entry order id order.

    A leg is open for the units its structure has not closed through its own orders, and
    never more than the account still holds on that side. An expiry or assignment settles
    the contract itself (O2), so a settled leg drops out here without a closing order.
    Each option contract belongs to at most one open structure (C4), so the position of
    a contract is its structure's.
    """
    found: list[OpenStructure] = []
    for entry in sorted(state.orders.values(), key=lambda order: order.order_id):
        if entry.parent_order_id is not None or not is_structure(entry.instrument):
            continue
        entered = state.filled_quantity.get(entry.order_id, ZERO)
        if entered <= 0:
            continue
        children = [order for order in state.orders.values() if order.parent_order_id == entry.order_id]
        closed = sum((state.filled_quantity.get(child.order_id, ZERO) for child in children), ZERO)
        units = entered - closed
        if units <= 0:
            continue
        legs = []
        for leg in legs_of(entry.instrument, entry.side):
            position = state.positions.get(leg.contract)
            held = ZERO if position is None else position.quantity
            on_side = max(held, ZERO) if leg.side is Side.BUY else max(-held, ZERO)
            legs.append(StructureLeg(leg.contract, leg.side, leg.ratio, min(units * leg.ratio, on_side)))
        if all(leg.open_quantity == 0 for leg in legs):
            continue
        target = next(
            (c for c in children if c.order_id == _target_id(entry.order_id) and c.state not in _TERMINAL),
            None,
        )
        closing = next(
            (c for c in children if c.order_id.startswith(f"{entry.order_id}:close:") and c.state not in _TERMINAL),
            None,
        )
        found.append(
            OpenStructure(
                entry_order_id=entry.order_id,
                account_id=entry.account_id,
                command_id=entry.order_id.removesuffix(":entry"),
                instrument=entry.instrument,
                side=entry.side,
                legs=tuple(legs),
                units=units,
                entry_price=_entry_price(state, entry, entered),
                opened_at=min(fill.filled_at for fill in state.fills if fill.order_id == entry.order_id),
                target_order_id=None if target is None else target.order_id,
                target_price=None if target is None else target.limit_price,
                closing_order_id=None if closing is None else closing.order_id,
            )
        )
    return tuple(found)


def _working(order: Order) -> bool:
    return order.state not in _TERMINAL


def uncovered_calls(state: AccountState, *, closing_counts: bool) -> dict[str, tuple[Decimal, Decimal]]:
    """Per underlying: (shares the short calls no long call covers deliver, shares free).

    A long call covers a short call on the same underlying expiring no later than it.
    Shares count only while no working order is selling them. With ``closing_counts``
    False, a short call whose structure has a close working needs no cover: its close
    and the share sale trade at the same snapshot.
    """
    closing: set[OptionContract] = set()
    if not closing_counts:
        for structure in open_structures(state):
            if structure.closing_order_id is not None:
                closing.update(leg.contract for leg in structure.legs)
    shorts: dict[str, list[tuple[OptionContract, Decimal]]] = {}
    longs: dict[str, list[tuple[OptionContract, Decimal]]] = {}
    shares: dict[str, Decimal] = {}
    for instrument, position in state.positions.items():
        if isinstance(instrument, Equity):
            shares[instrument.symbol] = shares.get(instrument.symbol, ZERO) + max(position.quantity, ZERO)
            continue
        if not isinstance(instrument, OptionContract) or instrument.right is not OptionRight.CALL:
            continue
        book = shorts if position.quantity < 0 else longs
        if position.quantity == 0 or (position.quantity < 0 and instrument in closing):
            continue
        book.setdefault(_underlying(instrument), []).append((instrument, abs(position.quantity)))
    for order in state.orders.values():
        if not _working(order):
            continue
        if isinstance(order.instrument, Equity) and order.side is Side.SELL:
            symbol = order.instrument.symbol
            remaining = order.quantity - state.filled_quantity.get(order.order_id, ZERO)
            shares[symbol] = shares.get(symbol, ZERO) - remaining
        elif order.parent_order_id is None and is_structure(order.instrument):
            # An entry still working that will sell calls needs its cover now.
            for leg in legs_of(order.instrument, order.side):
                if leg.side is Side.SELL and leg.contract.right is OptionRight.CALL:
                    shorts.setdefault(_underlying(leg.contract), []).append(
                        (leg.contract, (order.quantity - state.filled_quantity.get(order.order_id, ZERO)) * leg.ratio)
                    )
    result: dict[str, tuple[Decimal, Decimal]] = {}
    for underlying in sorted(set(shorts) | set(shares)):
        free = {id(item): item[1] for item in longs.get(underlying, [])}
        uncovered = ZERO
        # Latest expiry first: a long that covers it covers every earlier short too.
        for contract, quantity in sorted(shorts.get(underlying, []), key=lambda item: item[0].expiry, reverse=True):
            need = quantity
            for item in longs.get(underlying, []):
                if need == 0:
                    break
                if item[0].expiry >= contract.expiry and free[id(item)] > 0:
                    used = min(need, free[id(item)])
                    free[id(item)] -= used
                    need -= used
            uncovered += need * contract.multiplier
        result[underlying] = (uncovered, max(shares.get(underlying, ZERO), ZERO))
    return result

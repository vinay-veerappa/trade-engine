"""Options structures through the OMS: open, rest a profit target, close once (O4).

A structure is one entry order over a contract or an options combo. Its children are the
resting profit target (a GTC limit that reverses every leg) and at most one working close
order. Venue calls go through ``OrderManager``, so submission, acknowledgement, fills and
reconciliation keep the equity path's guarantees (I3, I10).

The guards the old options engine lacked (rules doc §7.1) are structural here:

- **C3 / I8: no call is written on shares the account does not hold.** A short call must
  be covered by this account's own shares (100 per contract) or by a long call on the
  same underlying that expires no earlier (a diagonal, the PMCC). Shares a working order
  is selling do not count. ``close_holding`` refuses to sell shares that still cover a
  short call, unless that call's close is already working.
- **C4: duplicate entry is impossible.** A command id opens one structure, and a replay
  returns it (I3). A new entry on a contract the account already holds, or already has
  an entry working for, refuses.
- **C5: a structure closes once.** A close on a structure with nothing open refuses, and
  so does a second close while one is working. A close replayed by its command id
  returns the same order.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from types import MappingProxyType

from trade_engine.domain.instruments import (
    Combo,
    ComboLeg,
    Equity,
    Instrument,
    OptionContract,
    OptionRight,
    Side,
)
from trade_engine.domain.option_orders import (
    CloseHolding,
    CloseStructure,
    OpenStructure,
    OptionIntent,
    StructureLeg,
    is_structure,
    legs_of,
    reverse,
)
from trade_engine.domain.option_roots import option_style
from trade_engine.domain.orders import Order, OrderState, OrderType, TimeInForce
from trade_engine.domain.portfolio import Position
from trade_engine.interfaces.broker import BrokerAdapter
from trade_engine.interfaces.clock import Clock
from trade_engine.ledger import Event, EventKind, Ledger, OrdersCreated
from trade_engine.ledger.codec import encode_payload
from trade_engine.ledger.state import AccountState
from trade_engine.oms.manager import IdempotencyConflictError, OrderManagementError, OrderManager

ZERO = Decimal("0")
_TERMINAL = frozenset({OrderState.FILLED, OrderState.CANCELLED, OrderState.REJECTED, OrderState.EXPIRED})


class OptionOrderError(OrderManagementError):
    """An options OMS command refused (I5)."""


class DuplicateEntryError(OptionOrderError):
    """C4: the account already holds, or is already entering, one of the contracts."""


class UncoveredCallError(OptionOrderError):
    """C3 / I8: a short call without this account's shares or long call behind it."""


class StructureClosedError(OptionOrderError):
    """C5: nothing of the structure is open, or its close is already working."""


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


class OptionOrderManager:
    """Open, protect and close options structures for the accounts of one ledger."""

    def __init__(self, broker: BrokerAdapter, clock: Clock, ledger: Ledger) -> None:
        self._clock = clock
        self._ledger = ledger
        self.orders = OrderManager(broker, clock, ledger)

    # -- open ---------------------------------------------------------------------------

    def open(self, intent: OptionIntent) -> Order:
        """Persist and submit the entry, with its profit target waiting for the fill."""
        fingerprint = self._intent_fingerprint(intent)
        existing = self._ledger.event_by_command(intent.command_id)
        if existing is not None:
            if (
                existing.kind is not EventKind.ORDERS_CREATED
                or existing.account != intent.account_id
                or existing.payload.fingerprint != fingerprint
            ):
                raise IdempotencyConflictError(
                    f"command_id '{intent.command_id}' was already used for a different command"
                )
            return self._submit(existing.payload.orders[0])  # a replay changes nothing (I3)
        state = self._ledger.state(intent.account_id)
        if is_structure(intent.instrument):
            self._refuse_duplicate(state, intent)
            self._refuse_uncovered(state, intent)
        now = self._now()
        entry = Order(
            order_id=f"{intent.command_id}:entry",
            account_id=intent.account_id,
            instrument=intent.instrument,
            order_type=intent.order_type,
            side=intent.side,
            quantity=intent.quantity,
            command_id=f"{intent.command_id}:entry",
            created_at=now,
            limit_price=intent.limit_price,
            tif=intent.tif,
        )
        orders = [entry]
        if intent.profit_target is not None:
            instrument, side = reverse(intent.instrument, intent.side)
            orders.append(
                Order(
                    order_id=_target_id(entry.order_id),
                    account_id=intent.account_id,
                    instrument=instrument,
                    order_type=OrderType.LIMIT,
                    side=side,
                    quantity=intent.quantity,
                    command_id=_target_id(entry.order_id),
                    created_at=now,
                    limit_price=intent.profit_target,
                    tif=TimeInForce.GTC,
                    parent_order_id=entry.order_id,
                    oco_group=f"{intent.command_id}:exits",
                )
            )
        self._append(
            intent.account_id,
            OrdersCreated(orders=tuple(orders), fingerprint=fingerprint, reason=f"Options entry: {intent.reason}"),
            intent.command_id,
        )
        return self._submit(entry)

    def _refuse_duplicate(self, state: AccountState, intent: OptionIntent) -> None:
        wanted = {leg.contract for leg in legs_of(intent.instrument, intent.side)}
        held = {c for c, p in state.positions.items() if isinstance(c, OptionContract) and p.quantity != 0}
        entering = {
            leg.contract
            for order in state.orders.values()
            if order.parent_order_id is None and _working(order) and is_structure(order.instrument)
            for leg in legs_of(order.instrument, order.side)
        }
        clash = sorted(c.occ.strip() for c in wanted & (held | entering))
        if clash:
            raise DuplicateEntryError(
                f"'{intent.account_id}' already holds or is entering {', '.join(clash)}; a second "
                f"entry on the same contract is refused (C4)"
            )

    def _refuse_uncovered(self, state: AccountState, intent: OptionIntent) -> None:
        added = [
            leg
            for leg in legs_of(intent.instrument, intent.side)
            if leg.side is Side.SELL and leg.contract.right is OptionRight.CALL
        ]
        if not added:
            return
        # The intent's own long calls cover its short calls (a diagonal entered as one).
        underlying = _underlying(added[0].contract)
        simulated = self._with_intent(state, intent)
        needed, shares = uncovered_calls(simulated, closing_counts=True).get(underlying, (ZERO, ZERO))
        if needed > shares:
            raise UncoveredCallError(
                f"'{intent.account_id}' would be short calls delivering {needed} {underlying} "
                f"shares with {shares} of its own and no long call behind the rest; an account "
                f"writes calls only on what it holds (C3, I8)"
            )

    @staticmethod
    def _with_intent(state: AccountState, intent: OptionIntent) -> AccountState:
        """``state`` as if the intent had filled, for the cover check only."""
        positions = dict(state.positions)
        for leg in legs_of(intent.instrument, intent.side):
            change = intent.quantity * leg.ratio * (1 if leg.side is Side.BUY else -1)
            current = positions.get(leg.contract)
            quantity = (ZERO if current is None else current.quantity) + change
            positions[leg.contract] = Position(
                account_id=state.account_id, instrument=leg.contract, quantity=quantity, avg_cost=ZERO
            )
        return replace(state, positions=MappingProxyType(positions))

    # -- close --------------------------------------------------------------------------

    def close(self, account_id: str, action: CloseStructure) -> Order:
        """Close what is open of a structure; its resting target is cancelled first."""
        existing = self._ledger.event_by_command(action.command_id)
        if existing is not None:
            if existing.kind is not EventKind.ORDERS_CREATED or existing.account != account_id:
                raise IdempotencyConflictError(
                    f"command_id '{action.command_id}' was already used for a different command"
                )
            [order] = existing.payload.orders
            if order.parent_order_id != action.entry_order_id:
                raise IdempotencyConflictError(
                    f"command_id '{action.command_id}' closed '{order.parent_order_id}', not "
                    f"'{action.entry_order_id}'"
                )
            return self._submit(order)
        state = self._ledger.state(account_id)
        structure = next(
            (s for s in open_structures(state) if s.entry_order_id == action.entry_order_id), None
        )
        if structure is None:
            raise StructureClosedError(
                f"'{action.entry_order_id}' in '{account_id}' has nothing open; it cannot be "
                f"closed again (C5)"
            )
        if structure.closing_order_id is not None:
            raise StructureClosedError(
                f"'{action.entry_order_id}' already has close '{structure.closing_order_id}' "
                f"working; a second close could over-close it (C5)"
            )
        instrument, side, quantity = self._closing_terms(structure)
        number = 1 + sum(
            1 for order in state.orders.values() if order.order_id.startswith(f"{structure.entry_order_id}:close:")
        )
        close = Order(
            order_id=f"{structure.entry_order_id}:close:{number}",
            account_id=account_id,
            instrument=instrument,
            order_type=OrderType.MARKET if action.limit_price is None else OrderType.LIMIT,
            side=side,
            quantity=quantity,
            command_id=action.command_id,
            created_at=self._now(),
            limit_price=action.limit_price,
            tif=TimeInForce.DAY,
            parent_order_id=structure.entry_order_id,
            oco_group=f"{structure.command_id}:exits",
        )
        if structure.target_order_id is not None:
            # Both would buy back the same contracts at the same snapshot.
            self.orders.cancel(structure.target_order_id, command_id=f"{action.command_id}:replaces-target")
        self._append(
            account_id,
            OrdersCreated(orders=(close,), fingerprint=self._order_fingerprint(close), reason=f"Options close: {action.reason}"),
            action.command_id,
        )
        return self._submit(close)

    @staticmethod
    def _closing_terms(structure: OpenStructure) -> tuple[Instrument, Side, Decimal]:
        """The order that reverses every leg still held, in whole units."""
        open_legs = [leg for leg in structure.legs if leg.open_quantity > 0]
        if len(open_legs) == 1:
            leg = open_legs[0]
            return leg.contract, Side.BUY if leg.side is Side.SELL else Side.SELL, leg.open_quantity
        units = structure.units
        if any(leg.open_quantity != units * leg.ratio for leg in open_legs):
            raise OptionOrderError(
                f"'{structure.entry_order_id}' holds its legs out of ratio "
                f"({[(leg.contract.occ.strip(), leg.open_quantity) for leg in open_legs]}); "
                f"close them one by one (I5)"
            )
        instrument, side = reverse(
            Combo(tuple(ComboLeg(leg.contract, leg.ratio, leg.side) for leg in open_legs)),
            structure.side,
        )
        return instrument, side, units

    def close_holding(self, account_id: str, action: CloseHolding) -> Order:
        """Sell (or cover) shares held outside any structure, at market."""
        existing = self._ledger.event_by_command(action.command_id)
        if existing is not None:
            if existing.kind is not EventKind.ORDERS_CREATED or existing.account != account_id:
                raise IdempotencyConflictError(
                    f"command_id '{action.command_id}' was already used for a different command"
                )
            return self._submit(existing.payload.orders[0])
        state = self._ledger.state(account_id)
        position = state.positions.get(action.instrument)
        held = ZERO if position is None else position.quantity
        if held == 0 or action.quantity > abs(held):
            raise OptionOrderError(
                f"'{account_id}' holds {held} {action.instrument.symbol}; it cannot close "
                f"{action.quantity} (I8)"
            )
        side = Side.SELL if held > 0 else Side.BUY
        order = Order(
            order_id=f"{action.command_id}:holding",
            account_id=account_id,
            instrument=action.instrument,
            order_type=OrderType.MARKET,
            side=side,
            quantity=action.quantity,
            command_id=action.command_id,
            created_at=self._now(),
            tif=TimeInForce.DAY,
        )
        if side is Side.SELL:
            after = self._with_order(state, order)
            needed, shares = uncovered_calls(after, closing_counts=False).get(
                action.instrument.symbol, (ZERO, ZERO)
            )
            if needed > shares:
                raise UncoveredCallError(
                    f"Selling {action.quantity} {action.instrument.symbol} would leave short calls "
                    f"in '{account_id}' delivering {needed} shares with {shares} behind them; "
                    f"close the calls first or with it (C3, I8)"
                )
        self._append(
            account_id,
            OrdersCreated(orders=(order,), fingerprint=self._order_fingerprint(order), reason=f"Holding close: {action.reason}"),
            action.command_id,
        )
        return self._submit(order)

    @staticmethod
    def _with_order(state: AccountState, order: Order) -> AccountState:
        orders = dict(state.orders)
        orders[order.order_id] = replace(order, state=OrderState.ACCEPTED)
        return replace(state, orders=MappingProxyType(orders))

    # -- keeping children in step ---------------------------------------------------------

    def sync(self, account_id: str, cause: str) -> None:
        """Bring every structure's children in line with what has filled and settled.

        - A filled entry sends its resting profit target.
        - An entry that ended unfilled cancels the target it never needed.
        - A structure with nothing left open (closed, target filled, expired, assigned)
          cancels whatever of its exits still works.
        - A structure one of whose legs has settled cancels its target, which would trade
          contracts it no longer holds.
        """
        state = self._ledger.state(account_id)
        open_by_entry = {s.entry_order_id: s for s in open_structures(state)}
        for entry in sorted(state.orders.values(), key=lambda order: order.order_id):
            if entry.parent_order_id is not None or not is_structure(entry.instrument):
                continue
            children = sorted(
                (order for order in state.orders.values() if order.parent_order_id == entry.order_id),
                key=lambda order: order.order_id,
            )
            structure = open_by_entry.get(entry.order_id)
            filled = state.filled_quantity.get(entry.order_id, ZERO)
            for child in children:
                current = self.orders.get_order(child.order_id)
                if current.state in _TERMINAL:
                    continue
                is_target = child.order_id == _target_id(entry.order_id)
                if structure is None and (filled > 0 or entry.state in _TERMINAL):
                    self.orders.cancel(child.order_id, command_id=f"{cause}:{child.order_id}:structure-done")
                elif structure is not None and is_target and any(
                    leg.open_quantity != structure.units * leg.ratio for leg in structure.legs
                ):
                    self.orders.cancel(child.order_id, command_id=f"{cause}:{child.order_id}:leg-settled")
                elif is_target and current.state is OrderState.NEW and entry.state is OrderState.FILLED:
                    self._submit(current)

    # -- plumbing -------------------------------------------------------------------------

    def _submit(self, order: Order) -> Order:
        current = self.orders.get_order(order.order_id)
        if current.state is not OrderState.NEW:
            return current
        submitted = self.orders.submit(current)
        if submitted.state not in _TERMINAL:
            # Confirm the acknowledgement now, so a re-run derives the same commands.
            submitted = self.orders.reconcile_order(order.order_id)
        return submitted

    def _append(self, account_id: str, payload: OrdersCreated, command_id: str) -> Event:
        now = self._now()
        return self._ledger.append(
            Event(account=account_id, kind=EventKind.ORDERS_CREATED, payload=payload, ts_utc=now, command_id=command_id)
        )

    def _now(self) -> datetime:
        now = self._clock.now_utc()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("Clock returned a naive datetime")
        return now

    @staticmethod
    def _intent_fingerprint(intent: OptionIntent) -> str:
        payload = {
            "intent_id": intent.intent_id,
            "account_id": intent.account_id,
            "instrument": encode_payload(intent.instrument),
            "side": intent.side.value,
            "quantity": str(intent.quantity),
            "order_type": intent.order_type.value,
            "limit_price": None if intent.limit_price is None else str(intent.limit_price),
            "tif": intent.tif.value,
            "profit_target": None if intent.profit_target is None else str(intent.profit_target),
            "reason": intent.reason,
        }
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    @staticmethod
    def _order_fingerprint(order: Order) -> str:
        # The terms, not the instant: a replay at another clock reading is the same command.
        terms = replace(order, created_at=datetime.min.replace(tzinfo=order.created_at.tzinfo))
        raw = json.dumps(encode_payload(terms), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

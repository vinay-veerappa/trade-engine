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

import contextlib
from datetime import datetime
from decimal import Decimal

from trade_engine.domain.instruments import (
    Combo,
    ComboLeg,
    Instrument,
    Side,
)
from trade_engine.domain.option_orders import (
    CloseHolding,
    CloseStructure,
    OpenStructure,
    OptionIntent,
    StructureLeg,
    legs_of,
    reverse,
)
from trade_engine.domain.orders import Order, OrderState, OrderType, TimeInForce
from trade_engine.interfaces.broker import BrokerAdapter
from trade_engine.interfaces.clock import Clock
from trade_engine.ledger import Event, EventKind, Ledger, OrdersCreated
from trade_engine.ledger import codec
from trade_engine.ledger.codec import DecimalRangeError, canon_decimal, encode_payload, legacy_spelling
from trade_engine.ledger.state import AccountState
from trade_engine.oms.manager import IdempotencyConflictError, OrderManagementError, OrderManager
from trade_engine.sim import _rs

_TERMINAL = frozenset({OrderState.FILLED, OrderState.CANCELLED, OrderState.REJECTED, OrderState.EXPIRED})


class OptionOrderError(OrderManagementError):
    """An options OMS command refused (I5)."""


class DuplicateEntryError(OptionOrderError):
    """C4: the account already holds, or is already entering, one of the contracts."""


class UncoveredCallError(OptionOrderError):
    """C3 / I8: a short call without this account's shares or long call behind it."""


class StructureClosedError(OptionOrderError):
    """C5: nothing of the structure is open, or its close is already working."""


# The kinds Rust's refusals carry (te_core::oms), and the exception each one raises.
_rs.register("option", OptionOrderError)
_rs.register("duplicate", DuplicateEntryError)
_rs.register("uncovered", UncoveredCallError)
_rs.register("closed", StructureClosedError)
_rs.register("idempotency", IdempotencyConflictError)


def _target_id(entry_order_id: str) -> str:
    return f"{entry_order_id}:target"


def _tree(value: object) -> str:
    return codec.text(encode_payload(value))


def _state_text(state: AccountState) -> str:
    return codec.text(codec.canon(state))


def open_structures(state: AccountState) -> tuple[OpenStructure, ...]:
    """Every structure in ``state`` with a leg still held, in entry order id order.

    A leg is open for the units its structure has not closed through its own orders, and
    never more than the account still holds on that side. An expiry or assignment settles
    the contract itself (O2), so a settled leg drops out here without a closing order.
    Each option contract belongs to at most one open structure (C4), so the position of
    a contract is its structure's. The rule is Rust's (``te_core::oms::structures``, P3a);
    this builds the carriers from the state's own objects.
    """
    found: list[OpenStructure] = []
    for entry_id, command_id, open_qty, units, price, opened, target_id, closing_id in _rs.call(
        _rs.rs.oms_open_structures, _state_text(state)
    ):
        entry = state.orders[entry_id]
        target = None if target_id is None else state.orders[target_id]
        found.append(
            OpenStructure(
                entry_order_id=entry_id,
                account_id=entry.account_id,
                command_id=command_id,
                instrument=entry.instrument,
                side=entry.side,
                legs=tuple(
                    StructureLeg(leg.contract, leg.side, leg.ratio, Decimal(quantity))
                    for leg, quantity in zip(legs_of(entry.instrument, entry.side), open_qty, strict=True)
                ),
                units=Decimal(units),
                entry_price=Decimal(price),
                opened_at=state.fills[opened].filled_at,
                target_order_id=target_id,
                target_price=None if target is None else target.limit_price,
                closing_order_id=closing_id,
            )
        )
    return tuple(found)


def uncovered_calls(state: AccountState, *, closing_counts: bool) -> dict[str, tuple[Decimal, Decimal]]:
    """Per underlying: (shares the short calls no long call covers deliver, shares free).

    A long call covers a short call on the same underlying expiring no later than it.
    Shares count only while no working order is selling them. With ``closing_counts``
    False, a short call whose structure has a close working needs no cover: its close
    and the share sale trade at the same snapshot. The rule is Rust's (P3a).
    """
    return {
        underlying: (Decimal(uncovered), Decimal(free))
        for underlying, uncovered, free in _rs.call(
            _rs.rs.oms_uncovered_calls, _state_text(state), closing_counts
        )
    }


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
            _rs.call(
                _rs.rs.oms_replay_conflict,
                intent.command_id,
                existing.kind is EventKind.ORDERS_CREATED,
                existing.account == intent.account_id,
            )
            presented = self._presented_fingerprint(intent, existing.payload.fingerprint, fingerprint)
            _rs.call(_rs.rs.oms_fingerprint_conflict, intent.command_id, existing.payload.fingerprint, presented)
            return self._submit(existing.payload.orders[0])  # a replay changes nothing (I3)
        state = self._ledger.state(intent.account_id)
        _rs.call(
            _rs.rs.oms_plan_open,
            _state_text(state),
            intent.account_id,
            _tree(intent.instrument),
            intent.side.value,
            str(intent.quantity),
        )
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

    # -- close --------------------------------------------------------------------------

    def close(self, account_id: str, action: CloseStructure) -> Order:
        """Close what is open of a structure; its resting target is cancelled first."""
        existing = self._ledger.event_by_command(action.command_id)
        if existing is not None:
            _rs.call(
                _rs.rs.oms_replay_conflict,
                action.command_id,
                existing.kind is EventKind.ORDERS_CREATED,
                existing.account == account_id,
            )
            [order] = existing.payload.orders
            _rs.call(_rs.rs.oms_close_target_conflict, action.command_id, order.parent_order_id, action.entry_order_id)
            return self._submit(order)
        state = self._ledger.state(account_id)
        order_id, mode, leg_indices, side_value, quantity, oco_group, target_id = _rs.call(
            _rs.rs.oms_plan_close, _state_text(state), account_id, action.entry_order_id
        )
        entry = state.orders[action.entry_order_id]
        held = legs_of(entry.instrument, entry.side)
        open_legs = [held[i] for i in leg_indices]
        if mode == "single":
            instrument: Instrument = open_legs[0].contract
        else:
            instrument = reverse(
                Combo(tuple(ComboLeg(leg.contract, leg.ratio, leg.side) for leg in open_legs)), entry.side
            )[0]
        close = Order(
            order_id=order_id,
            account_id=account_id,
            instrument=instrument,
            order_type=OrderType.MARKET if action.limit_price is None else OrderType.LIMIT,
            side=Side(side_value),
            quantity=Decimal(quantity),
            command_id=action.command_id,
            created_at=self._now(),
            limit_price=action.limit_price,
            tif=TimeInForce.DAY,
            parent_order_id=action.entry_order_id,
            oco_group=oco_group,
        )
        if target_id is not None:
            # Both would buy back the same contracts at the same snapshot.
            self.orders.cancel(target_id, command_id=f"{action.command_id}:replaces-target")
        self._append(
            account_id,
            OrdersCreated(orders=(close,), fingerprint=self._order_fingerprint(close), reason=f"Options close: {action.reason}"),
            action.command_id,
        )
        return self._submit(close)

    def close_holding(self, account_id: str, action: CloseHolding) -> Order:
        """Sell (or cover) shares held outside any structure, at market."""
        existing = self._ledger.event_by_command(action.command_id)
        if existing is not None:
            _rs.call(
                _rs.rs.oms_replay_conflict,
                action.command_id,
                existing.kind is EventKind.ORDERS_CREATED,
                existing.account == account_id,
            )
            return self._submit(existing.payload.orders[0])
        state = self._ledger.state(account_id)
        side = Side(
            _rs.call(
                _rs.rs.oms_plan_holding,
                _state_text(state),
                account_id,
                _tree(action.instrument),
                str(action.quantity),
                action.command_id,
            )
        )
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
        self._append(
            account_id,
            OrdersCreated(orders=(order,), fingerprint=self._order_fingerprint(order), reason=f"Holding close: {action.reason}"),
            action.command_id,
        )
        return self._submit(order)

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
        for child_id, action in _rs.call(_rs.rs.oms_sync_plan, _state_text(state)):
            current = self.orders.get_order(child_id)
            if current.state in _TERMINAL:
                continue
            if action in ("structure-done", "leg-settled"):
                self.orders.cancel(child_id, command_id=f"{cause}:{child_id}:{action}")
            elif action == "submit-target" and current.state is OrderState.NEW:
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

    def _presented_fingerprint(self, intent: OptionIntent, stored: str, current: str) -> str:
        """The fingerprint to hold against the stored one when a command id replays (P7).

        A command written before the migration carries the pre-P7 fingerprint (the same
        terms spelled as ``str(Decimal)``), or, once `p7_migrate` ran, an opaque one that
        `p7_key_map` ties to it. The replay is the same command exactly when its terms,
        spelled the old way, are the stored fingerprint or the one the map aliases to it;
        any other terms keep the stored fingerprint and still conflict (I3)."""
        if stored == current:
            return current
        legacy = self._intent_fingerprint(intent, legacy=True)
        if stored == legacy or self._ledger.fingerprint_alias(legacy) == stored:
            return stored
        return current

    @staticmethod
    def _intent_fingerprint(intent: OptionIntent, legacy: bool = False) -> str:
        def spell(value: Decimal | None) -> str | None:
            if value is None:
                return None
            if legacy:
                return str(value)
            try:
                return canon_decimal(value)
            except DecimalRangeError:
                return str(value)

        with legacy_spelling() if legacy else contextlib.nullcontext():
            return _rs.call(
                _rs.rs.oms_intent_fingerprint,
                intent.intent_id,
                intent.account_id,
                _tree(intent.instrument),
                intent.side.value,
                spell(intent.quantity),
                intent.order_type.value,
                spell(intent.limit_price),
                intent.tif.value,
                spell(intent.profit_target),
                intent.reason,
            )

    @staticmethod
    def _order_fingerprint(order: Order) -> str:
        # The terms, not the instant: a replay at another clock reading is the same command.
        return _rs.call(_rs.rs.oms_order_fingerprint, _tree(order))

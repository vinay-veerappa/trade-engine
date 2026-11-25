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
from decimal import Decimal

import trade_engine_rs as rs

from trade_engine.domain.option_orders import (
    OpenStructure,
    OptionIntent,
    StructureLeg,
    legs_of,
)
from trade_engine.domain.orders import Order
from trade_engine.ledger import codec
from trade_engine.ledger.codec import DecimalRangeError, canon_decimal, encode_payload, legacy_spelling
from trade_engine.ledger.state import AccountState
from trade_engine.oms.manager import IdempotencyConflictError, OrderManagementError
from trade_engine.sim import _rs


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


class OptionOrderManager(rs.OptionOrderManagerBase):
    """Open, protect and close options structures for the accounts of one ledger."""

    # The sequencing (open, close, close_holding, sync, and the _submit/_append/_now
    # plumbing) lives in `trade_engine_rs.OptionOrderManagerBase`. The fingerprint helpers
    # stay here: they are codec + `_rs.call` glue over the P7 legacy-spelling context, and
    # `test_p7_migrate` calls `_intent_fingerprint(..., legacy=True)` directly on the class.

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

"""P3b-2b, TEST ONLY: the internals the parity tests reach, on the Rust command flow.

Production ``oms/manager.py`` is the flow (``trade_engine_rs.oms_flow``, ``te_core::oms::flow``):
its public API and host are what ``FlowManager`` inherits. The flow's internal operations
(``_submit_native``, ``_cancel_exits``, ...) have no production caller; this subclass reaches
them so the parity tests can drive each one against the frozen oracle.
``trade_engine_rs`` missing is an ImportError (D5).
"""
from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal

import trade_engine_rs as rs  # noqa: F401 - D5: missing is an error, never a skip

from trade_engine.domain.orders import OrderType
from trade_engine.ledger import Event, EventKind, codec
from trade_engine.oms import manager as production
from trade_engine.oms.manager import _bracket, _changes, _order


def _event(tree):
    return Event(
        account=tree["account"],
        kind=EventKind(tree["kind"]),
        payload=codec.build(tree["payload"]),
        ts_utc=datetime.fromisoformat(tree["ts_utc"]),
        command_id=tree["command_id"],
        schema_version=tree["schema_version"],
        seq=tree["seq"],
    )


def _venue_order(tree):
    return production._Host.venue_order(None, json.dumps(tree))


class FlowManager(production.OrderManager):
    """Production ``OrderManager`` plus the flow internals the parity tests reach."""

    # --- internals the parity tests reach -------------------------------------------------

    def _route_emulated_trigger(self, order, trigger_price, command_id):
        return _order(self._flow("route_emulated_trigger", order=order, price=trigger_price, command_id=command_id))

    def _cancel_emulated_siblings(self, order, command_id):
        self._flow("cancel_emulated_siblings", order=order, command_id=command_id)

    def _replace_emulated_stop(self, order, changes, command_id):
        return _order(self._flow("replace_emulated_stop", order=order, changes=_changes(changes),
                                 command_id=command_id))

    def _ingest_venue_fills(self, order, found):
        self._flow("ingest_venue_fills", order=order, found=dict(
            venue_order_id=found.venue_order_id, state=found.state, filled_quantity=found.filled_quantity,
            remaining_quantity=found.remaining_quantity, updated_at=found.updated_at.isoformat()))

    def _submit_native(self, order):
        return _order(self._flow("submit_native", order=order))

    def _start_emulation(self, order):
        return _order(self._flow("start_emulation", order=order))

    def _submit_emulated(self, order, trigger_price, command_id, *, venue_type, limit_price):
        return _order(self._flow("submit_emulated", order=order, trigger_price=trigger_price,
                                 command_id=command_id, venue_type=venue_type, limit_price=limit_price))

    def _synchronize_bracket(self, changed_order, cause_id):
        self._flow("synchronize_bracket", order=changed_order, cause=cause_id)

    def _ensure_child_quantity(self, child, quantity, command_id):
        self._flow("ensure_child_quantity", order=child, quantity=quantity, command_id=command_id)

    def _submit_child(self, child):
        self._flow("submit_child", order=child)

    def _cancel_targets(self, targets, command_id):
        self._cancel_exits(targets, command_id)

    def _cancel_exits(self, exits, command_id):
        self._flow("cancel_exits", orders=list(exits), command_id=command_id)

    def _cancel_order(self, order, command_id, reason, *, oco=False):
        return _order(self._flow("cancel_order", order=order, command_id=command_id, reason=reason, oco=oco))

    def _mark_pending(self, order, reason, command_id):
        self._flow("mark_pending", order=order, reason=reason, command_id=command_id)

    def _ensure_stored(self, order):
        self._flow("ensure_stored", order=order)

    def _has_unresolved_replace(self, order_id):
        return self._flow("has_unresolved_replace", order_id=order_id)

    # --- plumbing (ported) ------------------------------------------------------------------

    def _utc_now(self):
        return datetime.fromisoformat(self._flow("utc_now")["T"])

    def _append(self, account_id, kind, payload, command_id):
        return _event(self._flow("append", account=account_id, kind=kind.value,
                                 payload=codec.encode_payload(payload), command_id=command_id))

    def _context(self, order_id):
        out = self._flow("context", order_id=order_id)
        return production._OrderContext(
            order=_order(out["order"]),
            filled=Decimal(out["filled"]),
            venue_order_id=out["venue_order_id"],
            emulation=None if out["emulation"] is None else codec.build(out["emulation"]),
        )

    def _planned_order(self, order_id):
        return _order(self._flow("planned_order", order_id=order_id))

    def _planned_quantity(self, order_id):
        return self._planned_order(order_id).quantity

    def _refuse(self, order, reason, command_id):
        self._flow("refuse", order=order, reason=reason, command_id=command_id)

    def _planned_refusal(self, order, mode, *, command_id=None, **values):
        self._flow("planned_refusal", order=order, mode=mode, command_id=command_id, **values)

    def _venue_order(self, order, *, order_type=None, limit_price=None, stop_price=None, trail_amount=None):
        return _venue_order(self._flow("venue_order", order=order, order_type=order_type, limit_price=limit_price))

    def _trigger_order_type(self, order):
        return OrderType(self._flow("trigger_order_type", order=order))

    def _supports_native_type(self, order_type):
        return self._flow("supports_native_type", type=order_type)

    def _require_tif(self, order, venue_type=None):
        self._flow("require_tif", order=order, venue_type=venue_type)

    def _bracket_from_orders(self, orders):
        return _bracket(self._flow("bracket_from_orders", orders=list(orders)))


__all__ = ["FlowManager"]

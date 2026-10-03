"""P3b-2b, TEST ONLY: ``OrderManager``'s public API driven through the Rust command flow
(``trade_engine_rs.oms_flow``, ``te_core::oms::flow``). Production ``oms/manager.py`` is
not switched; this is what the parity tests drive against the frozen oracle.

``_FlowHost`` is the flow's host: every effect is one call to it, made where the flow
makes it (the clock, the ledger, the broker). It decides nothing; values cross as plain
JSON-ish data and codec trees. ``trade_engine_rs`` missing is an ImportError (D5).
"""
from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal

import trade_engine_rs as rs  # noqa: F401 - D5: missing is an error, never a skip

from trade_engine.domain.instruments import Side
from trade_engine.domain.orders import OrderType, TimeInForce
from trade_engine.interfaces.broker import OrderChanges, VenueOrder, VenueOrderAllocation
from trade_engine.ledger import Event, EventKind, codec
from trade_engine.oms import manager as production
from trade_engine.oms.models import Bracket
from trade_engine.sim import _rs


def _dec(value):
    return None if value is None else Decimal(value)


class _FlowHost:
    """The flow's effects, one call each."""

    def __init__(self, broker, clock, ledger) -> None:
        self._broker = broker
        self._clock = clock
        self._ledger = ledger

    def now(self):
        return self._clock.now_utc().isoformat()

    def accounts(self):
        return list(self._ledger.accounts())

    def state(self, account):
        return codec.text(codec.canon(self._ledger.state(account)))

    def event_by_command(self, command_id):
        event = self._ledger.event_by_command(command_id)
        return None if event is None else codec.event_bytes(event)

    def events_of_kind(self, kind):
        return [codec.event_bytes(event) for event in self._ledger.events_of_kind(EventKind(kind))]

    def append(self, text):
        data = json.loads(text)
        event = Event(
            account=data["account"],
            kind=EventKind(data["kind"]),
            payload=codec.build(data["payload"]),
            ts_utc=datetime.fromisoformat(data["ts_utc"]),
            command_id=data["command_id"],
        )
        return codec.event_bytes(self._ledger.append(event))

    def capabilities(self):
        capabilities = self._broker.capabilities
        return (
            [item.value for item in capabilities.supported_order_types],
            [item.value for item in capabilities.supported_tifs],
            capabilities.supports_native_stops,
        )

    def env(self):
        return self._broker.env

    def venue_order(self, text):
        data = json.loads(text)
        return VenueOrder(
            venue_order_id=data["venue_order_id"],
            instrument=codec.build(data["instrument"]),
            order_type=OrderType(data["order_type"]),
            side=Side(data["side"]),
            quantity=Decimal(data["quantity"]),
            submitted_at=datetime.fromisoformat(data["submitted_at"]),
            tif=TimeInForce(data["tif"]),
            limit_price=_dec(data["limit_price"]),
            stop_price=_dec(data["stop_price"]),
            trail_amount=_dec(data["trail_amount"]),
            allocations=tuple(
                VenueOrderAllocation(item["strategy_order_id"], item["account_id"], Decimal(item["quantity"]))
                for item in data["allocations"]
            ),
            parent_order_id=data["parent_order_id"],
            oco_group=data["oco_group"],
        )

    @staticmethod
    def _ack(ack):
        return ack.venue_order_id, ack.status, ack.message

    def submit(self, venue_order):
        return self._ack(self._broker.submit(venue_order))

    def cancel(self, venue_order_id):
        return self._ack(self._broker.cancel(venue_order_id))

    def replace(self, venue_order_id, text):
        changes = {key: _dec(value) for key, value in json.loads(text).items()}
        return self._ack(self._broker.replace(venue_order_id, OrderChanges(**changes)))

    def orders(self, since):
        return [
            (item.venue_order_id, item.state.value, str(item.filled_quantity),
             str(item.remaining_quantity), item.updated_at.isoformat())
            for item in self._broker.orders(datetime.fromisoformat(since))
        ]

    def fills(self, since):
        return [
            (item.venue_fill_id, item.venue_order_id, codec.text(codec.encode_payload(item.instrument)),
             str(item.quantity), str(item.price), item.filled_at.isoformat(), item.side.value,
             str(item.fee), item.leg_id)
            for item in self._broker.fills(datetime.fromisoformat(since))
        ]

    @staticmethod
    def refusal(kind, message):
        return _rs.refusal(kind, message)


def _order(tree):
    return codec.build(tree)


def _bracket(tree):
    entry, stop, targets = tree
    return Bracket(_order(entry), _order(stop), tuple(_order(item) for item in targets))


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


def _changes(changes: OrderChanges):
    return {
        "new_quantity": production._wire(changes.new_quantity),
        "new_limit_price": production._wire(changes.new_limit_price),
        "new_stop_price": production._wire(changes.new_stop_price),
    }


def _venue_order(tree):
    return _FlowHost.venue_order(None, json.dumps(tree))


class FlowManager:
    """``OrderManager``'s API (and the internals the parity tests reach) on the Rust flow."""

    # The pure helpers did not move: they are the P3b-2a shims.
    _bracket_fingerprint = staticmethod(production.OrderManager._bracket_fingerprint)
    _reduce_fingerprint = staticmethod(production.OrderManager._reduce_fingerprint)
    _fingerprint_order = staticmethod(production.OrderManager._fingerprint_order)
    _validate_quantity = staticmethod(production.OrderManager._validate_quantity)
    _allocate_quantity = staticmethod(production.OrderManager._allocate_quantity)
    _split_quantity = production.OrderManager._split_quantity
    _fraction_quantities = production.OrderManager._fraction_quantities
    _is_reduce = staticmethod(production.OrderManager._is_reduce)
    _replacement_terms = staticmethod(production.OrderManager._replacement_terms)

    def __init__(self, broker, clock, ledger) -> None:
        self._broker = broker
        self._clock = clock
        self._ledger = ledger
        self._host = _FlowHost(broker, clock, ledger)

    def _flow(self, operation: str, **request):
        try:
            out = rs.oms_flow(operation, self._host, codec.text(production._wire(request)))
        except ValueError as err:
            if type(err) is ValueError and len(err.args) == 2 and all(isinstance(a, str) for a in err.args):
                raise _rs.refusal(*err.args) from err.__cause__
            raise
        return json.loads(out)

    # --- public API ---------------------------------------------------------------------

    def create_bracket(self, intent, quantity):
        return _bracket(self._flow("create_bracket", intent=intent, quantity=quantity))

    def get_order(self, order_id):
        return _order(self._flow("get_order", order_id=order_id))

    def submit(self, order):
        return _order(self._flow("submit", order=order))

    def submit_trailing(self, order):
        return _order(self._flow("submit_trailing", order=order))

    def update_trailing(self, order_id, market_price, *, command_id):
        return _order(self._flow("update_trailing", order_id=order_id, price=market_price, command_id=command_id))

    def update_emulated_order(self, order_id, market_price, *, command_id):
        return _order(self._flow("update_emulated_order", order_id=order_id, price=market_price, command_id=command_id))

    def record_fill(self, fill):
        return _order(self._flow("record_fill", fill=fill))

    def cancel(self, order_id, *, command_id):
        return _order(self._flow("cancel", order_id=order_id, command_id=command_id))

    def move_stop(self, entry_order_id, stop_price, *, command_id):
        return _order(self._flow("move_stop", entry=entry_order_id, stop_price=stop_price, command_id=command_id))

    def close_bracket(self, entry_order_id, *, command_id, reason):
        return _order(self._flow("close_bracket", entry=entry_order_id, command_id=command_id, reason=reason))

    def reduce_bracket(self, entry_order_id, fraction, *, command_id, reason):
        return _order(self._flow("reduce_bracket", entry=entry_order_id, fraction=fraction,
                                 command_id=command_id, reason=reason))

    def replace(self, order_id, changes, *, command_id):
        return _order(self._flow("replace", order_id=order_id, changes=_changes(changes), command_id=command_id))

    def reconcile_order(self, order_id):
        return _order(self._flow("reconcile_order", order_id=order_id))

    # --- internals the parity tests reach -------------------------------------------------

    def _route_emulated_trigger(self, order, trigger_price, command_id):
        return _order(self._flow("route_emulated_trigger", order=order, price=trigger_price, command_id=command_id))

    def _cancel_emulated_siblings(self, order, command_id):
        self._flow("cancel_emulated_siblings", order=order, command_id=command_id)

    def _send_reduce(self, reduce, command_id):
        return _order(self._flow("send_reduce", order=reduce, command_id=command_id))

    def _bracket_children(self, entry_order_id):
        return [_order(item) for item in self._flow("bracket_children", entry=entry_order_id)]

    def _open_bracket_stop(self, entry_order_id):
        stop, open_quantity = self._flow("open_bracket_stop", entry=entry_order_id)
        return _order(stop), Decimal(open_quantity)

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

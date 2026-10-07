"""The one door from ``tos_paper`` into Rust (docs/RUST_PORT.md P5, D3: no second reader).

``te_core::tos_paper`` owns every decision the mirror makes: ticket validation, normalization,
slippage, reconcile, the cover rule, netting, the exit plan, the follower's entries and the
broker's state machine. The modules of this package are shims: each builds the JSON document a
decision takes, calls it through :func:`decide` (or, for the broker, ``trade_engine_rs.TosBroker``
through :func:`call`), and turns the answer back into the carriers the rest of the engine uses.
Nothing here decides anything.

A Rust refusal crosses as ``ValueError((kind, message))`` and :func:`refusal` maps the kind to the
pre-port exception type, message unchanged:

========================  ==================================================  ===================
kind                      exception                                           module
========================  ==================================================  ===================
``tos_normalize``         ``NormalizeError``                                  ``normalize``
``tos_slippage``          ``SlippageError``                                   ``slippage``
``tos_unsupported``       ``UnsupportedCapability``                           ``interfaces.broker``
``tos_netting_error``     ``NettingError``                                    ``netting``
``tos_exit_plan_error``   ``ExitPlanError``                                   ``exits``
``tos_broker_error``      ``TosPaperBrokerError``                             ``broker``
``tos_venue_unreadable``  ``VenueUnreadable`` (message = the event as JSON)    ``broker``
``tos_overflow_error``    ``OverflowError``                                   builtin
``tos_division_undefined``  ``decimal.InvalidOperation([DivisionUndefined])``  builtin
========================  ==================================================  ===================

Every other kind is the ledger's (``value`` is ``ValueError``; the event-payload kinds are
``EventPayloadError``): ``trade_engine.ledger._rs``. An exception a host callback raised (the
transport's ``TransportUnavailable``, a quote lookup) crosses back as itself.

``trade_engine_rs`` missing is an ImportError, never a fallback (D5).
"""

from __future__ import annotations

import decimal
import importlib
import json
from collections.abc import Mapping
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import trade_engine_rs as rs  # noqa: F401 - D5: missing is an error, never a skip

from trade_engine.domain.instruments import Combo, ComboLeg, Equity, Instrument, OptionContract, OptionRight, Side
from trade_engine.domain.orders import Order, OrderType, TimeInForce
from trade_engine.interfaces.broker import VenueAck, VenueOrder, VenueOrderAllocation
from trade_engine.ledger.events import VenueReconcile
from trade_engine.sim import _rs as bridge

# -- the kinds the core's refusals carry --------------------------------------------------------


def _lazy(module: str, name: str):
    """The exception class, resolved on first refusal: its module imports this door."""
    def make(message: str) -> BaseException:
        return getattr(importlib.import_module(module), name)(message)
    return make


def _unreadable(message: str) -> BaseException:
    broker = importlib.import_module("trade_engine.tos_paper.broker")
    return broker.VenueUnreadable(event(json.loads(message)))


for _kind, _module, _name in (
    ("tos_normalize", "trade_engine.tos_paper.normalize", "NormalizeError"),
    ("tos_slippage", "trade_engine.tos_paper.slippage", "SlippageError"),
    ("tos_unsupported", "trade_engine.interfaces.broker", "UnsupportedCapability"),
    ("tos_netting_error", "trade_engine.tos_paper.netting", "NettingError"),
    ("tos_exit_plan_error", "trade_engine.tos_paper.exits", "ExitPlanError"),
    ("tos_broker_error", "trade_engine.tos_paper.broker", "TosPaperBrokerError"),
):
    bridge.register(_kind, _lazy(_module, _name))
bridge.register("tos_venue_unreadable", _unreadable)
bridge.register("tos_overflow_error", OverflowError)
bridge.register("tos_division_undefined", lambda _m: decimal.InvalidOperation([decimal.DivisionUndefined]))

refusal = bridge.refusal
call = bridge.call


def decide(op: str, doc: Mapping[str, Any]) -> Any:
    """``op`` over ``doc`` through the one pyo3 door; the answer as parsed JSON.

    ``default=str``: a transport row holding a value JSON cannot carry (the host's own
    ``Decimal`` or ``date``) reaches the core as its text, never as a crash on the host.
    """
    return json.loads(call(rs.tos_paper_decide, op, json.dumps(doc, default=str)))


# -- the wire: domain objects <-> the core's JSON ------------------------------------------------


def wire(i: Instrument) -> dict:
    if isinstance(i, Equity):
        return {"kind": "equity", "symbol": i.symbol}
    if isinstance(i, OptionContract):
        return {"kind": "option", "underlying": i.underlying, "expiry": i.expiry.isoformat(),
                "strike": str(i.strike), "right": i.right.value, "multiplier": i.multiplier}
    if isinstance(i, Combo):
        return {"kind": "combo", "legs": [{"contract": wire(l.contract), "ratio": l.ratio, "side": l.side.value}
                                           for l in i.legs]}
    return {"kind": "other", "repr": repr(i)}


def unwire(w: Mapping) -> Instrument:
    if w["kind"] == "equity":
        return Equity(w["symbol"])
    if w["kind"] == "combo":
        return Combo([ComboLeg(unwire(l["contract"]), l["ratio"], Side(l["side"])) for l in w["legs"]])
    return OptionContract(underlying=w["underlying"], expiry=date.fromisoformat(w["expiry"]),
                          strike=Decimal(w["strike"]), right=OptionRight(w["right"]), multiplier=w["multiplier"])


def dec(text: str | None) -> Decimal | None:
    return None if text is None else Decimal(text)


def text(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def pairs(mapping: Mapping[Instrument, Decimal]) -> list:
    return [[wire(k), str(v)] for k, v in mapping.items()]


def unpairs(doc: list) -> dict[Instrument, Decimal]:
    return {unwire(i): Decimal(q) for i, q in doc}


def holdings_doc(holdings: Mapping[tuple[str, Instrument], Decimal] | None) -> list:
    return [[a, wire(i), str(q)] for (a, i), q in (holdings or {}).items()]


def order_doc(o: Order) -> dict:
    return {"order_id": o.order_id, "account_id": o.account_id, "instrument": wire(o.instrument),
            "order_type": o.order_type.value, "side": o.side.value, "quantity": str(o.quantity),
            "tif": o.tif.value, "limit_price": text(o.limit_price)}


def venue_order_doc(v: VenueOrder) -> dict:
    return {"venue_order_id": v.venue_order_id, "instrument": wire(v.instrument), "order_type": v.order_type.value,
            "side": v.side.value, "quantity": str(v.quantity), "submitted_at": v.submitted_at.isoformat(),
            "tif": v.tif.value, "limit_price": text(v.limit_price),
            "allocations": [[a.strategy_order_id, a.account_id, str(a.quantity)] for a in v.allocations]}


def venue_order(d: Mapping) -> VenueOrder:
    return VenueOrder(
        venue_order_id=d["venue_order_id"], instrument=unwire(d["instrument"]), order_type=OrderType(d["order_type"]),
        side=Side(d["side"]), quantity=Decimal(d["quantity"]), submitted_at=datetime.fromisoformat(d["submitted_at"]),
        tif=TimeInForce(d["tif"]), limit_price=dec(d["limit_price"]),
        allocations=tuple(VenueOrderAllocation(o, a, Decimal(q)) for o, a, q in d["allocations"]))


def ticket_doc(t: VenueOrder) -> dict:
    """The part of a ticket a read-back is judged against."""
    return {"instrument": wire(t.instrument), "order_type": t.order_type.value, "side": t.side.value,
            "quantity": str(t.quantity), "limit_price": text(t.limit_price)}


def event(d: Mapping) -> VenueReconcile:
    return VenueReconcile(venue=d["venue"], as_of=datetime.fromisoformat(d["as_of"]), reconciled=d["reconciled"],
                          drift=tuple(d["drift"]), note=d["note"])


def ack(d: Mapping) -> VenueAck:
    return VenueAck(d["venue_order_id"], d["status"], datetime.fromisoformat(d["at"]), d["message"])


__all__ = [
    "ack", "call", "dec", "decide", "event", "holdings_doc", "order_doc", "pairs", "refusal", "rs", "text",
    "ticket_doc", "unpairs", "unwire", "venue_order", "venue_order_doc", "wire",
]

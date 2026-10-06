"""A test-side host adapter for ``trade_engine_rs.TosBroker``: the DRAFT of the P5-T10 shim.

``TosBroker`` (``te_core::tos_paper::broker``) owns every decision: the submit queue, the
expected book, the sent tickets, the cancelled set, the keys and the sticky halt, and what to
call next. This adapter is the thin sequencer around it. It keeps the I/O (the transport's
sends, reads and cancels, ``connect`` and ``balance``), answers the broker's callbacks, and
turns JSON back into the carriers production code uses (``VenueAck``, ``DrainReport``,
``FillCollection``, ``NettedBatch``, ``VenueReconcile``...). It holds no decision.

The shape T10's shim takes is this class's: a ``TosPaperBroker`` whose decision methods are
one-line calls into ``self._core`` through ``bridge.call`` and whose private state is gone (the
``del`` below is the draft of that deletion: a stray touch of the old state is an
``AttributeError``, not a silent second model).

Not here, and why: ``connect``/``balance`` stay production's own (identity and balance gates
are I/O and string checks, ``mark_connected`` is the only thing the core needs from them).
"""
from __future__ import annotations

import decimal
import json
from collections.abc import Collection, Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal

import trade_engine_rs as rs

from trade_engine.domain.instruments import Combo, ComboLeg, Equity, Instrument, OptionContract, OptionRight, Side
from trade_engine.domain.orders import Order, OrderState, OrderType, TimeInForce
from trade_engine.interfaces.broker import UnsupportedCapability, VenueAck, VenueOrder, VenueOrderAllocation, VenuePosition
from trade_engine.ledger import codec
from trade_engine.ledger.events import MirrorAck, MirrorFill, VenueReconcile
from trade_engine.ledger.mirror import MirrorState
from trade_engine.sim import _rs as bridge
from trade_engine.tos_paper import normalize as N
from trade_engine.tos_paper.broker import (
    DrainReport, FillCollection, MirrorBinding, TosPaperBroker, TosPaperBrokerError, VenueUnreadable,
)
from trade_engine.tos_paper.netting import NettedBatch, NettingError
from trade_engine.tos_paper.transport import (
    MirrorComboLeg, MirrorComboTicket, MirrorStockTicket, MirrorTicket, OrderCanceller, OrderFillReader,
    TransportRefused, TransportReplay, TransportUnavailable,
)

# -- the kinds the core's refusals carry --------------------------------------------------------


def _unreadable(message: str) -> VenueUnreadable:
    return VenueUnreadable(_event(json.loads(message)))


bridge.register("tos_broker_error", TosPaperBrokerError)
bridge.register("tos_venue_unreadable", _unreadable)
bridge.register("tos_normalize", N.NormalizeError)
bridge.register("tos_unsupported", UnsupportedCapability)
bridge.register("tos_netting_error", NettingError)
bridge.register("tos_overflow_error", OverflowError)
bridge.register("tos_division_undefined", lambda _m: decimal.InvalidOperation([decimal.DivisionUndefined]))

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


def _dec(text: str | None) -> Decimal | None:
    return None if text is None else Decimal(text)


def order_doc(o: Order) -> dict:
    return {"order_id": o.order_id, "account_id": o.account_id, "instrument": wire(o.instrument),
            "order_type": o.order_type.value, "side": o.side.value, "quantity": str(o.quantity),
            "tif": o.tif.value, "limit_price": None if o.limit_price is None else str(o.limit_price)}


def venue_order_doc(v: VenueOrder) -> dict:
    return {"venue_order_id": v.venue_order_id, "instrument": wire(v.instrument), "order_type": v.order_type.value,
            "side": v.side.value, "quantity": str(v.quantity), "submitted_at": v.submitted_at.isoformat(),
            "tif": v.tif.value, "limit_price": None if v.limit_price is None else str(v.limit_price),
            "allocations": [[a.strategy_order_id, a.account_id, str(a.quantity)] for a in v.allocations]}


def venue_order(d: Mapping) -> VenueOrder:
    return VenueOrder(
        venue_order_id=d["venue_order_id"], instrument=unwire(d["instrument"]), order_type=OrderType(d["order_type"]),
        side=Side(d["side"]), quantity=Decimal(d["quantity"]), submitted_at=datetime.fromisoformat(d["submitted_at"]),
        tif=TimeInForce(d["tif"]), limit_price=_dec(d["limit_price"]),
        allocations=tuple(VenueOrderAllocation(o, a, Decimal(q)) for o, a, q in d["allocations"]))


def _ticket(d: Mapping):
    """The ticket ``transport::ticket_for`` decided, as the venue driver's own carrier."""
    limit = _dec(d["limit_price"])
    if d["kind"] == "stock":
        return MirrorStockTicket(symbol=d["symbol"], side=d["side"], quantity=d["quantity"],
                                 order_type=d["order_type"], limit_price=limit, tif=d["tif"])
    if d["kind"] == "option":
        return MirrorTicket(symbol=d["symbol"], side=d["side"], quantity=d["quantity"], order_type=d["order_type"],
                            limit_price=limit, tif=d["tif"], underlying=d["underlying"],
                            expiry=date.fromisoformat(d["expiry"]), strike=Decimal(d["strike"]), right=d["right"])
    legs = tuple(MirrorComboLeg(symbol=l["symbol"], side=l["side"], ratio=l["ratio"],
                                expiry=date.fromisoformat(l["expiry"]), strike=Decimal(l["strike"]), right=l["right"])
                 for l in d["legs"])
    return MirrorComboTicket(underlying=d["underlying"], legs=legs, quantity=d["quantity"],
                             order_type=d["order_type"], limit_price=limit, price_effect=d["price_effect"],
                             tif=d["tif"])


def _event(d: Mapping) -> VenueReconcile:
    return VenueReconcile(venue=d["venue"], as_of=datetime.fromisoformat(d["as_of"]), reconciled=d["reconciled"],
                          drift=tuple(d["drift"]), note=d["note"])


def _ack(d: Mapping) -> VenueAck:
    return VenueAck(d["venue_order_id"], d["status"], datetime.fromisoformat(d["at"]), d["message"])


# -- the host the core calls back ---------------------------------------------------------------


class _Host:
    """The clock and the transport, answered as the core asks. No decision lives here."""

    def __init__(self, broker: "RustTosPaperBroker") -> None:
        self._b = broker
        self._last: BaseException | None = None

    def now(self) -> str:
        return self._b._clock.now_utc().isoformat()

    def can_read_fills(self) -> bool:
        return isinstance(self._b.transport, OrderFillReader)

    def can_cancel(self) -> bool:
        return isinstance(self._b.transport, OrderCanceller)

    def _ask(self, call):
        try:
            return ("ok", json.dumps(call()), "", "")
        except Exception as exc:  # noqa: BLE001 - the core classifies; a BaseException unwinds as itself
            self._last = exc
            kind = ("refused" if isinstance(exc, TransportRefused) else "replay" if isinstance(exc, TransportReplay)
                    else "unavailable" if isinstance(exc, TransportUnavailable) else "other")
            return ("exc", kind, type(exc).__name__, str(exc))

    def place_order(self, spec: str, key: str):
        return self._ask(lambda: self._b.transport.place_order(_ticket(json.loads(spec)), key))

    def cancel_order(self, order_id: str):
        return self._ask(lambda: self._b.transport.cancel_order(order_id))

    def read_positions(self):
        return self._ask(lambda: list(self._b.transport.read_positions()))

    def read_working_orders(self):
        return self._ask(lambda: list(self._b.transport.read_working_orders()))

    def read_order_fills(self):
        return self._ask(lambda: list(self._b.transport.read_order_fills()))

    def reraise(self) -> None:
        raise self._last  # the very exception object the transport raised (TransportUnavailable)


# -- the broker -----------------------------------------------------------------------------------

_DEAD = ("_halted", "_queue", "_keys", "_expected", "_restored", "_sent", "_proven", "_preflight_book", "_cancelled")


class RustTosPaperBroker(TosPaperBroker):
    """``TosPaperBroker`` with its decision state in ``trade_engine_rs.TosBroker``."""

    def __init__(self, transport, binding: MirrorBinding, *, clock, balance_reader=None,
                 balance_unproven_ok: bool = False, halted_venues: Collection[str] = ()) -> None:
        super().__init__(transport, binding, clock=clock, balance_reader=balance_reader,
                         balance_unproven_ok=balance_unproven_ok, halted_venues=halted_venues)
        for name in _DEAD:
            delattr(self, name)
        self._core = rs.TosBroker(binding.venue_account, list(binding.mirrored_accounts),
                                  binding.venue_account in frozenset(halted_venues))
        self._host = _Host(self)

    def _go(self, method, *args):
        return bridge.call(method, self._host, *args)

    # state the host reads
    @property
    def halted(self) -> bool:
        return self._core.halted

    @property
    def queued(self) -> tuple[VenueOrder, ...]:
        return tuple(venue_order(d) for d in json.loads(self._core.state())["queued"])

    def proven_order_id(self, ticket_key: str) -> str | None:
        return self._core.proven_order_id(ticket_key)

    # connection: production's own gates, then the one fact the core needs
    def connect(self):
        identity = super().connect()
        self._core.mark_connected()
        return identity

    # the mirror layer
    def mirror_batch(self, strategy_orders: Sequence[Order], *, holdings: Mapping[tuple[str, Instrument], Decimal]):
        out = json.loads(self._go(
            self._core.mirror_batch,
            json.dumps([order_doc(o) for o in list(strategy_orders)]),
            json.dumps([[a, wire(i), str(q)] for (a, i), q in holdings.items()])))
        return NettedBatch(venue_orders=tuple(venue_order(d) for d in out["venue_orders"]),
                           refused=tuple((o, r) for o, r in out["refused"]))

    def restore(self, mirror: MirrorState, *, halted_venues: Collection[str] = ()) -> None:
        bridge.call(self._core.restore, json.dumps(codec.canon(mirror)), list(halted_venues))

    def collect_fills(self, mirror: MirrorState) -> FillCollection:
        out = json.loads(self._go(self._core.collect_fills, json.dumps(codec.canon(mirror))))
        return FillCollection(
            fills=tuple(MirrorFill(venue=f["venue"], ticket_key=f["ticket_key"], venue_order_id=f["venue_order_id"],
                                   filled=Decimal(f["filled"]), avg_price=_dec(f["avg_price"]),
                                   at=datetime.fromisoformat(f["at"])) for f in out["fills"]),
            closes=tuple(MirrorAck(venue=c["venue"], ticket_key=c["ticket_key"], status=c["status"],
                                   message=c["message"], at=datetime.fromisoformat(c["at"]),
                                   venue_order_id=c["venue_order_id"], book_status=OrderState(c["book_status"]))
                         for c in out["closes"]))

    # the slow path
    def preflight(self) -> None:
        self._go(self._core.preflight)

    def drain(self) -> DrainReport:
        out = json.loads(self._go(self._core.drain))
        return DrainReport(
            acks=tuple(_ack(a) for a in out["acks"]),
            reconcile=None if out["reconcile"] is None else _event(out["reconcile"]),
            proven={k: (oid, OrderState(state)) for k, (oid, state) in out["proven"]})

    def reconcile_now(self, *, defer_unavailable: bool = False) -> VenueReconcile:
        return _event(json.loads(self._go(self._core.reconcile_now, defer_unavailable)))

    def submit(self, order: VenueOrder) -> VenueAck:
        return _ack(json.loads(self._go(self._core.submit, json.dumps(venue_order_doc(order)))))

    def cancel(self, venue_order_id: str) -> VenueAck:
        return _ack(json.loads(self._go(self._core.cancel, venue_order_id)))

    def positions(self) -> list[VenuePosition]:
        return [VenuePosition(unwire(p["instrument"]), Decimal(p["quantity"]), Decimal(p["avg_price"]),
                              datetime.fromisoformat(p["as_of"]))
                for p in json.loads(self._go(self._core.positions))]

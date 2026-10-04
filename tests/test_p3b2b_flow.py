"""P3b-2b: the frozen c878969 manager versus the Rust command flow (``te_core::oms::flow``).

The flow is driven through the TEST-ONLY ``FlowManager`` (``tests/flow_p3b2b.py``); the
production manager is not switched. Two worlds have separate ledgers, venues and clocks;
every step compares the return or the refusal (type, message, and the type and message of
its ``__cause__``), the events, the outbox and the venue's records, and each world ends by
comparing the canonical folded state. Coverage is counted, refusals and successes both.

The plumbing tests cover the helpers the scaffolding ported. Each family test drives one
ticket's family, using only that family and the ones before it (agent/tickets_p3b2b.json):
T1 submit, T2 replace, T3 cancel and fills, T4 emulation, T5 reconcile, T6 brackets.
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import trade_engine_rs  # noqa: F401 - D5, not importorskip

sys.path.insert(0, str(Path(__file__).resolve().parent))
from flow_p3b2b import FlowManager
from frozen_p3b2.oracle_manager import OrderManager as Oracle
from test_p3a_parity import canon
from test_p3b2_parity import (
    ACC, NOW, OPT, SEEDS, XYZ, FaultBroker, World, bracket_walk, intent, real_venue,
)
from trade_engine.domain.instruments import Side
from trade_engine.domain.orders import Order, OrderState, OrderType, TimeInForce
from trade_engine.domain.portfolio import Fill
from trade_engine.interfaces.broker import OrderChanges, VenueFill, VenueOrderState
from trade_engine.interfaces.market_data import Bar
from trade_engine.ledger import EventKind, OrderStateChange, OrderUpdated, codec
from trade_engine.market_data.chains import ChainSnapshot

D = Decimal


def run(fn, *args):
    """The result, or the refusal with its cause (a broker failure must stay attached)."""
    try:
        return ("ok", canon(fn(*args)))
    except Exception as err:  # noqa: BLE001 - the refusal itself is what is compared
        cause = err.__cause__
        return ("raise", type(err).__name__, str(err),
                None if cause is None else (type(cause).__name__, str(cause)))


class FlowWorld(World):
    def __init__(self, root, cls, venue):
        super().__init__(root, cls, venue)
        # Seeds a state the step under test reads, on either side, through the oracle.
        self.seed = Oracle(self.broker, self.clock, self.ledger)

    def venue_records(self):
        return canon(tuple(getattr(self.broker, name, None) for name in ("submitted", "cancelled", "replaced")))

    def set_clock(self, clock):
        self.manager._clock = clock
        if hasattr(self.manager, "_host"):
            self.manager._host._clock = clock


class FlowPair:
    def __init__(self, root, tally, venue=lambda clock: FaultBroker(), oracle=Oracle):
        self.o = FlowWorld(root / "oracle", oracle, venue)
        self.p = FlowWorld(root / "flow", FlowManager, venue)
        self.tally = tally
        self.n = 0

    def do(self, label, fn):
        self.n += 1
        a, b = run(fn, self.o), run(fn, self.p)
        assert a == b, f"{label} step {self.n}\noracle: {a!r:.1500}\nflow:   {b!r:.1500}"
        self.tally["steps"] += 1
        self.tally[f"{label}:{a[0]}"] += 1
        if a[0] == "raise":
            self.tally[f"exception:{a[1]}"] += 1
        assert self.o.events() == self.p.events(), (label, "events", self.n)
        assert self.o.outbox() == self.p.outbox(), (label, "outbox", self.n)
        assert self.o.venue_records() == self.p.venue_records(), (label, "venue", self.n)
        return a

    def both(self, fn):
        fn(self.o)
        fn(self.p)

    def finish(self):
        try:
            if sys.exc_info()[1] is not None:
                return  # a step already failed: keep its message (e.g. "unported") as THE failure
            assert codec.text(codec.canon(self.o.ledger.state(ACC))) == codec.text(
                codec.canon(self.p.ledger.state(ACC)))
        finally:
            self.o.ledger.close()
            self.p.ledger.close()


PLANNED_GOLDEN = Path(__file__).resolve().parent / "frozen_p3b2b" / "planned_refusal.json"


class RecordedPair:
    """One world against a recorded reference: each step's result, events, outbox and
    venue records, then the folded state. ``golden=None`` records instead of comparing."""

    def __init__(self, root, tally, cls, golden=None, venue=lambda clock: FaultBroker()):
        self.p = FlowWorld(root, cls, venue)
        self.tally = tally
        self.golden = None if golden is None else json.loads(golden.read_text(encoding="utf-8"))
        self.steps = []

    def do(self, label, fn):
        result = run(fn, self.p)
        step = json.loads(json.dumps([label, result, self.p.events(), self.p.outbox(), self.p.venue_records()]))
        if self.golden is not None:
            n = len(self.steps)
            assert step == self.golden["steps"][n], (
                f"{label} step {n + 1}\nrecorded: {self.golden['steps'][n]!r:.1500}\nflow:     {step!r:.1500}")
        self.steps.append(step)
        self.tally["steps"] += 1
        self.tally[f"{label}:{result[0]}"] += 1
        if result[0] == "raise":
            self.tally[f"exception:{result[1]}"] += 1
        return result

    def both(self, fn):
        fn(self.p)

    def state(self):
        return codec.text(codec.canon(self.p.ledger.state(ACC)))

    def finish(self):
        try:
            if sys.exc_info()[1] is not None or self.golden is None:
                return
            assert len(self.steps) == len(self.golden["steps"])
            assert self.state() == self.golden["state"]
        finally:
            self.p.ledger.close()


def planned_refusal_steps(pair):
    pair.both(lambda w: w.seed.create_bracket(intent(), D("7")))
    for mode, values in (("child", {"parent": "bracket:entry"}), ("protective", {}), ("native", {}),
                         ("limit", {}), ("trigger", {}), ("tif", {})):
        for command in (None, "caller"):
            pair.do("planned-refusal", lambda w, mode=mode, values=values, command=command:
                    w.manager._planned_refusal(w.manager.get_order("bracket:stop"), mode, command_id=command, **values))
    pair.do("planned-refusal-replay", lambda w: w.manager._planned_refusal(
        w.manager.get_order("bracket:stop"), "tif"))


def check(tally, expected):
    """Exact success and refusal counts per label."""
    for label, successes, refusals in expected:
        assert (tally[label + ":ok"], tally[label + ":raise"]) == (successes, refusals), (label, dict(tally))


def assert_steps(tally, steps):
    assert tally["steps"] == steps, (tally["steps"], steps)


class NaiveClock:
    def now_utc(self):
        return datetime(2026, 9, 23, 13, 30)


class ShiftedClock:
    """Aware, but not UTC: the event converts, a venue order keeps the offset."""
    def now_utc(self):
        return NOW.astimezone(timezone(timedelta(hours=5, minutes=30)))


class RaisingClock:
    def now_utc(self):
        raise RuntimeError("clock unavailable")


def venue_fill(fill_id, order_id, quantity, price=D("100"), side=Side.SELL):
    return VenueFill(fill_id, order_id, XYZ, D(quantity), price, NOW, side)


# --- plumbing (green at baseline) ---------------------------------------------------------


def test_plumbing_context_planned_and_venue_order(tmp_path):
    tally = Counter()
    for types in (frozenset(OrderType), frozenset({OrderType.LIMIT}), frozenset({OrderType.STOP})):
        pair = FlowPair(tmp_path / "-".join(sorted(t.value for t in types)), tally,
                        lambda clock, types=types: FaultBroker(order_types=types))
        try:
            pair.both(lambda w: w.seed.create_bracket(intent(fractions=(D(".25"), D(".25"))), D("7")))
            pair.both(lambda w: w.seed.submit(w.seed.get_order("bracket:entry")) if OrderType.LIMIT in types else None)
            pair.both(lambda w: setattr(w.broker, "capabilities", replace(
                w.broker.capabilities, supported_tifs=frozenset({TimeInForce.DAY}))))
            for oid in ("bracket:entry", "bracket:stop", "bracket:target:1", "missing"):
                pair.do("get-order", lambda w, oid=oid: w.manager.get_order(oid))
                pair.do("context", lambda w, oid=oid: w.manager._context(oid))
                pair.do("planned-order", lambda w, oid=oid: w.manager._planned_order(oid))
                pair.do("planned-quantity", lambda w, oid=oid: w.manager._planned_quantity(oid))
            for oid in ("bracket:entry", "bracket:stop", "bracket:target:2"):
                pair.do("venue-order", lambda w, oid=oid: w.manager._venue_order(w.manager.get_order(oid)))
                pair.do("venue-order-market", lambda w, oid=oid: w.manager._venue_order(
                    w.manager.get_order(oid), order_type=OrderType.MARKET, limit_price=None))
                pair.do("venue-order-limit", lambda w, oid=oid: w.manager._venue_order(
                    w.manager.get_order(oid), order_type=OrderType.LIMIT, limit_price=D("101.50")))
                pair.do("trigger-type", lambda w, oid=oid: w.manager._trigger_order_type(w.manager.get_order(oid)))
                for venue_type in (None, OrderType.MARKET, OrderType.LIMIT):
                    pair.do("require-tif", lambda w, oid=oid, vt=venue_type: w.manager._require_tif(
                        w.manager.get_order(oid), vt))
                    pair.do("require-tif-gtc", lambda w, oid=oid, vt=venue_type: w.manager._require_tif(
                        replace(w.manager.get_order(oid), tif=TimeInForce.GTC), vt))
            for typ in (OrderType.STOP, OrderType.TRAIL, OrderType.LIMIT):
                pair.do("native-type", lambda w, typ=typ: w.manager._supports_native_type(typ))
            pair.do("bracket-orders", lambda w: w.manager._bracket_from_orders(tuple(
                w.manager.get_order(oid) for oid in ("bracket:target:1", "bracket:stop", "bracket:entry"))))
            pair.do("bracket-orders-unstored", lambda w: w.manager._bracket_from_orders((
                w.manager.get_order("bracket:entry"), w.manager.get_order("bracket:stop"),
                replace(w.manager.get_order("bracket:target:1"), order_id="loose", quantity=D("1")))))
            pair.do("bracket-orders-duplicate", lambda w: w.manager._bracket_from_orders((
                w.manager.get_order("bracket:entry"), w.manager.get_order("bracket:stop"),
                w.manager.get_order("bracket:entry"))))
            pair.do("bracket-orders-no-stop", lambda w: w.manager._bracket_from_orders((
                w.manager.get_order("bracket:entry"),)))
            pair.both(lambda w: w.set_clock(ShiftedClock()))
            pair.do("venue-order-offset", lambda w: w.manager._venue_order(w.manager.get_order("bracket:entry")))
            pair.do("utc-now-offset", lambda w: w.manager._utc_now())
            for clock in (NaiveClock(), RaisingClock()):
                pair.both(lambda w, clock=clock: w.set_clock(clock))
                pair.do("utc-now-bad", lambda w: w.manager._utc_now())
                pair.do("venue-order-bad-clock", lambda w: w.manager._venue_order(w.manager.get_order("bracket:entry")))
        finally:
            pair.finish()
    check(tally, (
        ("get-order", 9, 3), ("context", 9, 3), ("planned-order", 9, 3), ("planned-quantity", 9, 3),
        ("venue-order", 9, 0), ("venue-order-market", 9, 0), ("venue-order-limit", 9, 0),
        ("trigger-type", 6, 3), ("require-tif", 6, 21), ("require-tif-gtc", 0, 27),
        ("native-type", 9, 0), ("bracket-orders", 3, 0), ("bracket-orders-unstored", 3, 0),
        ("bracket-orders-duplicate", 3, 0), ("bracket-orders-no-stop", 0, 3),
        ("venue-order-offset", 3, 0), ("utc-now-offset", 3, 0), ("utc-now-bad", 0, 6),
        ("venue-order-bad-clock", 0, 6),
    ))
    assert_steps(tally, 177)
    print("\nplumbing tally:", dict(sorted(tally.items())))


def test_plumbing_append_refuse_and_planned_refusal(tmp_path):
    tally = Counter()
    pair = FlowPair(tmp_path, tally)
    try:
        pair.both(lambda w: w.seed.create_bracket(intent(), D("7")))
        refused = OrderStateChange("bracket:entry", "refused for the test")
        pair.do("append", lambda w: w.manager._append(ACC, EventKind.ORDER_REFUSED, refused, "append:1"))
        pair.do("append-replay", lambda w: w.manager._append(ACC, EventKind.ORDER_REFUSED, refused, "append:1"))
        pair.do("append-conflict", lambda w: w.manager._append(
            ACC, EventKind.ORDER_REFUSED, OrderStateChange("bracket:entry", "another reason"), "append:1"))
        pair.do("append-conflict", lambda w: w.manager._append(ACC, EventKind.ORDER_PENDING, refused, "append:1"))
        pair.do("append-conflict", lambda w: w.manager._append("OTHER", EventKind.ORDER_REFUSED, refused, "append:1"))
        pair.do("append", lambda w: w.manager._append(
            ACC, EventKind.ORDER_UPDATED, OrderUpdated(w.manager.get_order("bracket:entry"), "updated", None), "append:2"))
        pair.do("append-empty-account", lambda w: w.manager._append("", EventKind.ORDER_REFUSED, refused, "append:3"))
        pair.do("append-empty-command", lambda w: w.manager._append(ACC, EventKind.ORDER_REFUSED, refused, ""))
        pair.do("append-wrong-payload", lambda w: w.manager._append(ACC, EventKind.ORDERS_CREATED, refused, "append:4"))
        pair.do("refuse", lambda w: w.manager._refuse(w.manager.get_order("bracket:stop"), "no", "refuse:1"))
        pair.do("refuse", lambda w: w.manager._refuse(w.manager.get_order("bracket:stop"), "no", "refuse:1"))
        pair.do("refuse-conflict", lambda w: w.manager._refuse(w.manager.get_order("bracket:stop"), "yes", "refuse:1"))
        for clock in (ShiftedClock(), NaiveClock(), RaisingClock()):
            pair.both(lambda w, clock=clock: w.set_clock(clock))
            pair.do("append-clock", lambda w, clock=clock: w.manager._append(
                ACC, EventKind.ORDER_REFUSED, refused, f"clock:{type(clock).__name__}"))
            pair.do("append-clock-replay", lambda w: w.manager._append(ACC, EventKind.ORDER_REFUSED, refused, "append:1"))
    finally:
        pair.finish()
    # The frozen oracle has no _planned_refusal (P3b-2a factored it out of six inline
    # refusals, each parity-tested there). Its reference is the P3b-2a manager, recorded
    # at 29bd759 before the switch deleted it (D3): tests/frozen_p3b2b/planned_refusal.json.
    pair = RecordedPair(tmp_path / "planned", tally, FlowManager, golden=PLANNED_GOLDEN)
    try:
        planned_refusal_steps(pair)
    finally:
        pair.finish()
    check(tally, (
        ("append", 2, 0), ("append-replay", 1, 0), ("append-conflict", 0, 3),
        ("append-empty-account", 0, 1), ("append-empty-command", 0, 1), ("append-wrong-payload", 0, 1),
        ("refuse", 2, 0), ("refuse-conflict", 0, 1), ("planned-refusal", 0, 12),
        ("planned-refusal-replay", 0, 1), ("append-clock", 1, 2), ("append-clock-replay", 1, 2),
    ))
    assert_steps(tally, 31)
    print("\nappend tally:", dict(sorted(tally.items())))


# --- ticket families (red with "unported" at baseline) -------------------------------------


def test_t1_submit_native_and_acks(tmp_path):
    """T1: submit, submit_trailing, _ensure_stored, _submit_native, _record_submit_ack."""
    tally = Counter()
    for status in ("ACCEPTED", "REJECTED", "PENDING", "unexpected"):
        pair = FlowPair(tmp_path / status, tally)
        try:
            pair.both(lambda w: setattr(w.broker, "submit_status", status))
            pair.do("submit", lambda w: w.manager.submit(w.standalone(limit_price=D("100"))))
            pair.do("submit-replay", lambda w: w.manager.submit(w.standalone(limit_price=D("100"))))
            pair.do("stored-conflict", lambda w: w.manager.submit(replace(w.standalone(limit_price=D("100")), quantity=D("8"))))
            pair.do("submit-market", lambda w: w.manager.submit(w.standalone(name="market", typ=OrderType.MARKET)))
            pair.do("submit-zero", lambda w: w.manager.submit(replace(w.standalone(name="zero", limit_price=D("100")), quantity=D("0"))))
            pair.do("native-trail", lambda w: w.manager.submit_trailing(w.standalone(name="trail-limit", limit_price=D("100"))))
        finally:
            pair.finish()
    for types, tifs in ((frozenset({OrderType.LIMIT}), frozenset({TimeInForce.DAY})),
                        (frozenset(OrderType), frozenset({TimeInForce.DAY, TimeInForce.GTC}))):
        pair = FlowPair(tmp_path / f"caps-{len(types)}", tally,
                        lambda clock, types=types, tifs=tifs: FaultBroker(order_types=types, tifs=tifs))
        try:
            pair.do("unsupported-market", lambda w: w.manager.submit(w.standalone(name="m", typ=OrderType.MARKET)))
            pair.do("tif-standalone", lambda w: w.manager.submit(w.standalone(limit_price=D("100"), tif=TimeInForce.GTC)))
            if OrderType.TRAIL in types:  # a LIMIT-only venue would emulate it: that is T4
                pair.do("trail-native", lambda w: w.manager.submit_trailing(w.standalone(
                    name="trail", typ=OrderType.TRAIL, trail_amount=D("2"))))
        finally:
            pair.finish()
    pair = FlowPair(tmp_path / "network", tally)
    try:
        pair.both(lambda w: setattr(w.broker, "submit_error", OSError("socket lost")))
        pair.do("network-unknown", lambda w: w.manager.submit(w.standalone(limit_price=D("100"))))
        pair.do("network-replay", lambda w: w.manager.submit(w.standalone(limit_price=D("100"))))
        pair.both(lambda w: setattr(w.broker, "submit_error", KeyError("not an OSError")))
        pair.do("network-other", lambda w: w.manager.submit(w.standalone(name="other", limit_price=D("100"))))
        pair.both(lambda w: w.set_clock(NaiveClock()))
        pair.do("naive-clock", lambda w: w.manager.submit(w.standalone(name="naive", limit_price=D("100"))))
    finally:
        pair.finish()
    check(tally, (
        ("submit", 3, 1), ("submit-replay", 4, 0), ("stored-conflict", 0, 4), ("submit-market", 3, 1),
        ("submit-zero", 0, 4), ("native-trail", 0, 4), ("unsupported-market", 1, 1),
        ("tif-standalone", 1, 1), ("trail-native", 1, 0), ("network-unknown", 0, 1),
        ("network-replay", 1, 0), ("network-other", 0, 1), ("naive-clock", 0, 1),
    ))
    assert_steps(tally, 33)
    print("\nT1 tally:", dict(sorted(tally.items())))


def test_t2_replace(tmp_path):
    """T2: replace and _restore_working_state (plus T1 to put an order at the venue)."""
    tally = Counter()
    for status in ("ACCEPTED", "REJECTED", "PENDING", "unexpected"):
        pair = FlowPair(tmp_path / status, tally)
        try:
            pair.do("standalone-submit", lambda w: w.manager.submit(w.standalone(limit_price=D("100"))))
            pair.do("replace-zero", lambda w: w.manager.replace("standalone", OrderChanges(new_quantity=D("0")), command_id="zero"))
            pair.do("replace-fractional", lambda w: w.manager.replace("standalone", OrderChanges(new_quantity=D("1.5")), command_id="fraction"))
            pair.do("replace-nan", lambda w: w.manager.replace("standalone", OrderChanges(new_quantity=D("NaN")), command_id="nan"))
            pair.do("replace-missing", lambda w: w.manager.replace("missing", OrderChanges(new_quantity=D("6")), command_id="missing"))
            pair.do("replace-noop", lambda w: w.manager.replace("standalone", OrderChanges(), command_id="noop"))
            pair.do("replace-noop-replay", lambda w: w.manager.replace("standalone", OrderChanges(), command_id="noop"))
            pair.do("replace-noop-conflict", lambda w: w.manager.replace("standalone", OrderChanges(new_quantity=D("6")), command_id="noop"))
            pair.both(lambda w: setattr(w.broker, "replace_status", status))
            pair.do("replace-status", lambda w: w.manager.replace("standalone", OrderChanges(new_quantity=D("6")), command_id="replace"))
            pair.do("replace-status-replay", lambda w: w.manager.replace("standalone", OrderChanges(new_quantity=D("6")), command_id="replace"))
            pair.do("replace-status-conflict", lambda w: w.manager.replace("standalone", OrderChanges(new_quantity=D("5")), command_id="replace"))
            pair.do("replace-limit", lambda w: w.manager.replace("standalone", OrderChanges(new_limit_price=D("99.5")), command_id="limit"))
        finally:
            pair.finish()
    pair = FlowPair(tmp_path / "network", tally)
    try:
        pair.do("network-setup", lambda w: w.manager.submit(w.standalone(limit_price=D("100"))))
        pair.both(lambda w: setattr(w.broker, "replace_error", OSError("socket lost")))
        pair.do("network-unknown", lambda w: w.manager.replace("standalone", OrderChanges(new_quantity=D("6")), command_id="lost"))
        pair.do("network-pending", lambda w: w.manager.replace("standalone", OrderChanges(new_quantity=D("5")), command_id="other"))
        pair.do("network-replay", lambda w: w.manager.replace("standalone", OrderChanges(new_quantity=D("6")), command_id="lost"))
    finally:
        pair.finish()
    check(tally, (
        ("standalone-submit", 4, 0), ("replace-zero", 0, 4), ("replace-fractional", 0, 4),
        ("replace-nan", 0, 4), ("replace-missing", 0, 4), ("replace-noop", 4, 0),
        ("replace-noop-replay", 4, 0), ("replace-noop-conflict", 0, 4), ("replace-status", 3, 1),
        ("replace-status-replay", 3, 1), ("replace-status-conflict", 0, 4), ("replace-limit", 2, 2),
        ("network-setup", 1, 0), ("network-unknown", 0, 1), ("network-pending", 0, 1), ("network-replay", 0, 1),
    ))
    assert_steps(tally, 52)
    print("\nT2 tally:", dict(sorted(tally.items())))


def test_t3_cancel_and_fills(tmp_path):
    """T3: cancel, _cancel_order, _cancel_exits, record_fill, _synchronize_bracket,
    _ensure_child_quantity, _submit_child (standalone orders; T1 and T2 to set up)."""
    tally = Counter()
    for status in ("ACCEPTED", "PENDING", "REJECTED", "bad-status"):
        pair = FlowPair(tmp_path / status, tally)
        try:
            pair.do("cancel-submit", lambda w: w.manager.submit(w.standalone(limit_price=D("100"))))
            pair.do("partial-fill", lambda w: w.fill("standalone", "2", "partial"))
            pair.do("fill-replay", lambda w: w.fill("standalone", "2", "partial"))
            pair.do("fill-conflict", lambda w: w.fill("standalone", "3", "partial"))
            pair.do("fill-environment", lambda w: w.manager.record_fill(Fill(
                fill_id="wrong", order_id="standalone", account_id=ACC, instrument=XYZ,
                quantity=D("1"), price=D("100"), venue_env="paper", filled_at=NOW, side=Side.SELL)))
            pair.do("fill-account", lambda w: w.manager.record_fill(Fill(
                fill_id="account", order_id="standalone", account_id="OTHER", instrument=XYZ,
                quantity=D("1"), price=D("100"), venue_env="sim", filled_at=NOW, side=Side.SELL)))
            pair.do("fill-unknown", lambda w: w.manager.record_fill(Fill(
                fill_id="unknown", order_id="missing", account_id=ACC, instrument=XYZ,
                quantity=D("1"), price=D("100"), venue_env="sim", filled_at=NOW, side=Side.SELL)))
            pair.both(lambda w: setattr(w.broker, "cancel_status", status))
            pair.do("cancel-result", lambda w: w.manager.cancel("standalone", command_id="cancel"))
            pair.do("cancel-replay", lambda w: w.manager.cancel("standalone", command_id="cancel"))
            pair.do("oco-pending", lambda w: w.manager._cancel_exits([w.manager.get_order("standalone")], "oco"))
            pair.do("cancel-missing", lambda w: w.manager.cancel("missing", command_id="missing"))
            pair.do("fill-after-cancel", lambda w: w.fill("standalone", "1", "late"))
        finally:
            pair.finish()
    for side in Side:
        pair = FlowPair(tmp_path / f"full-{side.value}", tally)
        try:
            pair.do("full-submit", lambda w: w.manager.submit(w.standalone(side=side, limit_price=D("100"))))
            pair.do("full-fill", lambda w: w.fill("standalone", "7", "full"))
            pair.do("over-fill", lambda w: w.fill("standalone", "1", "over"))
            pair.do("cancel-filled", lambda w: w.manager.cancel("standalone", command_id="filled"))
        finally:
            pair.finish()
    # A bracket the oracle created on both sides (create_bracket is T6): the entry's fills
    # size the protection, target fills and a stop fill cancel the rest.
    for status in ("ACCEPTED", "PENDING", "REJECTED", "unrecognized"):
        pair = FlowPair(tmp_path / f"bracket-{status}", tally)
        try:
            pair.both(lambda w: w.seed.create_bracket(intent(fractions=(D(".25"), D(".25"))), D("7")))
            pair.do("held-child", lambda w: w.manager.submit(w.manager.get_order("bracket:stop")))
            pair.do("entry-submit", lambda w: w.manager.submit(w.manager.get_order("bracket:entry")))
            pair.both(lambda w: setattr(w.broker, "submit_status", status))
            pair.do("entry-partial", lambda w: w.fill("bracket:entry", "3", "entry-partial"))
            pair.do("entry-rest", lambda w: w.fill("bracket:entry", "4", "entry-rest"))
            pair.do("protective-cancel", lambda w: w.manager.cancel("bracket:stop", command_id="unsafe"))
            pair.do("terminal-child", lambda w: w.manager._ensure_child_quantity(w.manager.get_order("bracket:entry"), D("2"), "child"))
            pair.do("child-resize", lambda w: w.manager._ensure_child_quantity(w.manager.get_order("bracket:stop"), D("5"), "resize"))
            pair.do("target-fill", lambda w: w.fill("bracket:target:1", "1", "target", D("105")))
            pair.do("stop-fill", lambda w: w.fill("bracket:stop", "2", "stop", D("95")))
            pair.do("exit-cancel", lambda w: w.manager._cancel_exits(
                [w.manager.get_order(oid) for oid in ("bracket:target:1", "bracket:target:2")], "exits"))
            pair.do("entry-cancel", lambda w: w.manager.cancel("bracket:entry", command_id="entry-cancel"))
        finally:
            pair.finish()
    pair = FlowPair(tmp_path / "network", tally)
    try:
        pair.do("network-setup", lambda w: w.manager.submit(w.standalone(limit_price=D("100"))))
        pair.both(lambda w: setattr(w.broker, "cancel_error", OSError("socket lost")))
        pair.do("network-unknown", lambda w: w.manager.cancel("standalone", command_id="lost"))
        pair.do("oco-unknown", lambda w: w.manager._cancel_exits([w.manager.get_order("standalone")], "oco"))
        pair.do("network-pending", lambda w: w.manager.replace("standalone", OrderChanges(new_quantity=D("5")), command_id="other"))
    finally:
        pair.finish()
    check(tally, (
        ("cancel-submit", 4, 0), ("partial-fill", 4, 0), ("fill-replay", 4, 0), ("fill-conflict", 0, 4),
        ("fill-environment", 0, 4), ("fill-account", 0, 4), ("fill-unknown", 0, 4),
        ("cancel-result", 2, 2), ("cancel-replay", 4, 0), ("oco-pending", 1, 3), ("cancel-missing", 0, 4),
        ("fill-after-cancel", 3, 1), ("full-submit", 2, 0), ("full-fill", 2, 0), ("over-fill", 0, 2),
        ("cancel-filled", 2, 0), ("network-setup", 1, 0), ("network-unknown", 0, 1),
        ("oco-unknown", 0, 1), ("network-pending", 0, 1), ("held-child", 0, 4), ("entry-submit", 4, 0),
        ("entry-partial", 2, 2), ("entry-rest", 1, 3), ("protective-cancel", 1, 3), ("terminal-child", 0, 4),
        ("child-resize", 1, 3), ("target-fill", 1, 3), ("stop-fill", 2, 2), ("exit-cancel", 4, 0),
        ("entry-cancel", 2, 2),
    ))
    assert_steps(tally, 104)
    print("\nT3 tally:", dict(sorted(tally.items())))


def test_t4_emulation(tmp_path):
    """T4: _start_emulation, update_emulated_order, update_trailing, _route_emulated_trigger,
    _cancel_emulated_siblings, _submit_emulated, _replace_emulated_stop."""
    tally = Counter()
    for native in (False, True):
        for typ in (OrderType.STOP, OrderType.STOP_LIMIT, OrderType.TRAIL):
            for side in Side:
                pair = FlowPair(tmp_path / f"{native}-{typ.value}-{side.value}", tally,
                                lambda clock, native=native: FaultBroker(
                                    order_types=frozenset(OrderType) if native else frozenset({OrderType.MARKET, OrderType.LIMIT}),
                                    native_stops=native))
                try:
                    args = dict(stop_price=D("100")) if typ is not OrderType.TRAIL else dict(trail_amount=D("2"))
                    if typ is OrderType.STOP_LIMIT:
                        args["limit_price"] = D("101")
                    pair.do("stop-submit", lambda w: w.manager.submit(w.standalone(typ=typ, side=side, **args)))
                    pair.do("stop-replay", lambda w: w.manager.submit(w.standalone(typ=typ, side=side, **args)))
                    pair.do("local-replace", lambda w: w.manager.replace("standalone", OrderChanges(new_stop_price=D("99")), command_id="local"))
                    pair.do("local-replace-replay", lambda w: w.manager.replace("standalone", OrderChanges(new_stop_price=D("99")), command_id="local"))
                    pair.do("local-replace-conflict", lambda w: w.manager.replace("standalone", OrderChanges(new_stop_price=D("98")), command_id="local"))
                    for n, price in enumerate(("98", "103", "100", "99", "105")):
                        pair.do("observe", lambda w, n=n, price=price: w.manager.update_emulated_order("standalone", D(price), command_id=f"price:{n}"))
                        pair.do("observe-replay", lambda w, n=n, price=price: w.manager.update_emulated_order("standalone", D(price), command_id=f"price:{n}"))
                    pair.do("observe-conflict", lambda w: w.manager.update_emulated_order("standalone", D("111"), command_id="price:0"))
                    pair.do("observe-invalid", lambda w: w.manager.update_emulated_order("standalone", D("NaN"), command_id="invalid"))
                    pair.do("trail-api", lambda w: w.manager.update_trailing("standalone", D("98"), command_id="trail"))
                finally:
                    pair.finish()
    for side in Side:
        pair = FlowPair(tmp_path / f"touch-{side.value}", tally,
                        lambda clock: FaultBroker(order_types=frozenset({OrderType.LIMIT}), native_stops=False))
        try:
            pair.do("touch-submit", lambda w: w.manager.submit(w.standalone(typ=OrderType.STOP, side=side, stop_price=D("100"))))
            pair.do("stop-touch", lambda w: w.manager.update_emulated_order("standalone", D("100"), command_id="touch"))
            pair.do("stop-touch-replay", lambda w: w.manager.update_emulated_order("standalone", D("100"), command_id="touch"))
            pair.do("route-no-price", lambda w: w.manager._route_emulated_trigger(w.manager.get_order("standalone"), None, "none"))
        finally:
            pair.finish()
    for types in (frozenset({OrderType.LIMIT}), frozenset({OrderType.MARKET}), frozenset()):
        pair = FlowPair(tmp_path / f"caps-{len(types)}-{sorted(t.value for t in types)}", tally,
                        lambda clock, types=types: FaultBroker(order_types=types, native_stops=False))
        try:
            pair.do("capability-stop", lambda w: w.manager.submit(w.standalone(typ=OrderType.STOP, stop_price=D("100"))))
            pair.do("capability-observe", lambda w: w.manager.update_emulated_order("standalone", D("99"), command_id="observe"))
        finally:
            pair.finish()
    # A bracket on an emulating venue: the entry fill starts the stop's emulation, a touch
    # cancels the target siblings before the triggered stop is sent.
    for side in Side:
        pair = FlowPair(tmp_path / f"bracket-{side.value}", tally,
                        lambda clock: FaultBroker(order_types=frozenset({OrderType.MARKET, OrderType.LIMIT}), native_stops=False))
        try:
            pair.both(lambda w: w.seed.create_bracket(replace(intent(), side=side,
                      stop_loss=D("95") if side is Side.BUY else D("105"),
                      profit_targets=(D("105"), D("110")) if side is Side.BUY else (D("95"), D("90"))), D("7")))
            pair.do("bracket-entry", lambda w: w.manager.submit(w.manager.get_order("bracket:entry")))
            pair.do("bracket-fill", lambda w: w.fill("bracket:entry", "7", "entry"))
            for n, price in enumerate(("100", "96", "104", "95", "105")):
                pair.do("bracket-observe", lambda w, n=n, price=price: w.manager.update_emulated_order(
                    "bracket:stop", D(price), command_id=f"bracket:{n}"))
            pair.do("bracket-siblings", lambda w: w.manager._cancel_emulated_siblings(w.manager.get_order("bracket:stop"), "siblings"))
        finally:
            pair.finish()
    pair = FlowPair(tmp_path / "network", tally)
    try:
        pair.do("network-setup", lambda w: w.manager.submit(w.standalone(limit_price=D("100"))))
        pair.both(lambda w: setattr(w.broker, "replace_error", OSError("socket lost")))
        pair.do("network-unknown", lambda w: w.manager.replace("standalone", OrderChanges(new_quantity=D("6")), command_id="lost"))
        pair.do("pending-emulation", lambda w: w.manager._start_emulation(w.manager.get_order("standalone")))
        pair.do("pending-trigger", lambda w: w.manager._submit_emulated(
            w.manager.get_order("standalone"), D("100"), "trigger", venue_type=OrderType.MARKET, limit_price=None))
    finally:
        pair.finish()
    check(tally, (
        ("observe", 30, 30), ("local-replace", 8, 4), ("trail-api", 2, 10), ("observe-invalid", 0, 12),
        ("stop-touch", 2, 0), ("stop-touch-replay", 2, 0), ("route-no-price", 0, 2),
        ("capability-stop", 2, 1), ("pending-emulation", 0, 1), ("pending-trigger", 0, 1),
        ("bracket-observe", 10, 0), ("bracket-siblings", 2, 0),
    ))
    assert_steps(tally, 12 * 18 + 8 + 6 + 2 * 8 + 4)
    print("\nT4 tally:", dict(sorted(tally.items())))


def test_t5_reconcile(tmp_path):
    """T5: reconcile_order, _ingest_venue_fills, _has_unresolved_replace (T1-T3 to set up)."""
    tally = Counter()
    for status in ("ACCEPTED", "PENDING"):
        pair = FlowPair(tmp_path / f"replace-{status}", tally)
        try:
            pair.do("standalone-submit", lambda w: w.manager.submit(w.standalone(limit_price=D("100"))))
            pair.both(lambda w: setattr(w.broker, "replace_status", status))
            pair.do("replace-status", lambda w: w.manager.replace("standalone", OrderChanges(new_quantity=D("6")), command_id="replace"))
            pair.both(lambda w: setattr(w.broker, "order_readback", [
                VenueOrderState("standalone", OrderState.ACCEPTED, D("0"), D("6"), NOW)]))
            pair.do("reconcile-replace", lambda w: w.manager.reconcile_order("standalone"))
            pair.both(lambda w: setattr(w.broker, "order_readback", [
                VenueOrderState("standalone", OrderState.CANCELLED, D("0"), D("6"), NOW)]))
            pair.do("reconcile-terminal", lambda w: w.manager.reconcile_order("standalone"))
            pair.do("reconcile-terminal-replay", lambda w: w.manager.reconcile_order("standalone"))
        finally:
            pair.finish()
    for status in ("ACCEPTED", "PENDING"):
        pair = FlowPair(tmp_path / f"fills-{status}", tally)
        try:
            pair.both(lambda w: setattr(w.broker, "submit_status", status))
            pair.do("standalone-submit", lambda w: w.manager.submit(w.standalone(limit_price=D("100"))))
            pair.do("reconcile-missing", lambda w: w.manager.reconcile_order("standalone"))
            pair.do("reconcile-unknown-order", lambda w: w.manager.reconcile_order("missing"))
            pair.both(lambda w: setattr(w.broker, "order_readback", [
                VenueOrderState("standalone", OrderState.ACCEPTED, D("0"), D("7"), NOW)]))
            pair.do("reconcile-accepted", lambda w: w.manager.reconcile_order("standalone"))
            pair.both(lambda w: setattr(w.broker, "order_readback", [
                VenueOrderState("standalone", OrderState.PARTIALLY_FILLED, D("3"), D("4"), NOW + timedelta(seconds=1))]))
            pair.do("reconcile-missing-fills", lambda w: w.manager.reconcile_order("standalone"))
            pair.both(lambda w: setattr(w.broker, "fill_readback", [
                venue_fill("v1", "standalone", "1"), venue_fill("v2", "other", "5"), venue_fill("v3", "standalone", "2")]))
            pair.do("reconcile-ingest", lambda w: w.manager.reconcile_order("standalone"))
            pair.both(lambda w: setattr(w.broker, "order_readback", [
                VenueOrderState("standalone", OrderState.FILLED, D("7"), D("0"), NOW + timedelta(seconds=2))]))
            pair.do("reconcile-incomplete", lambda w: w.manager.reconcile_order("standalone"))
            pair.both(lambda w: setattr(w.broker, "fill_readback", w.broker.fill_readback + [
                venue_fill("v4", "standalone", "4")]))
            pair.do("reconcile-filled", lambda w: w.manager.reconcile_order("standalone"))
            pair.both(lambda w: setattr(w.broker, "order_readback", [
                VenueOrderState("standalone", OrderState.NEW, D("0"), D("7"), NOW)]))
            pair.do("reconcile-after-terminal", lambda w: w.manager.reconcile_order("standalone"))
        finally:
            pair.finish()
    # The protective stop of a bracket the oracle created, read back in each state.
    pair = FlowPair(tmp_path / "bracket", tally)
    try:
        pair.both(lambda w: w.seed.create_bracket(intent(), D("7")))
        pair.do("bracket-entry", lambda w: w.manager.submit(w.manager.get_order("bracket:entry")))
        pair.do("bracket-fill", lambda w: w.fill("bracket:entry", "7", "full"))
        for state, filled, remaining, label in (
            (OrderState.ACCEPTED, "0", "2", "reconcile-stop-accepted"),
            (OrderState.FILLED, "2", "0", "reconcile-stop-incomplete"),
            (OrderState.PARTIALLY_FILLED, "1", "1", "reconcile-stop-missing-fills"),
            (OrderState.NEW, "0", "2", "reconcile-stop-unresolved"),
        ):
            pair.both(lambda w, s=state, f=filled, r=remaining: setattr(w.broker, "order_readback", [
                VenueOrderState("bracket:stop", s, D(f), D(r), NOW)]))
            pair.do(label, lambda w: w.manager.reconcile_order("bracket:stop"))
    finally:
        pair.finish()
    pair = FlowPair(tmp_path / "unresolved", tally)
    try:
        pair.do("standalone-submit", lambda w: w.manager.submit(w.standalone(limit_price=D("100"))))
        pair.both(lambda w: setattr(w.broker, "order_readback", [
            VenueOrderState("standalone", OrderState.NEW, D("0"), D("7"), NOW)]))
        pair.do("reconcile-unresolved-state", lambda w: w.manager.reconcile_order("standalone"))
        pair.do("has-unresolved-replace", lambda w: w.manager._has_unresolved_replace("standalone"))
    finally:
        pair.finish()
    check(tally, (
        ("reconcile-replace", 1, 1), ("reconcile-terminal", 2, 0), ("reconcile-terminal-replay", 2, 0),
        ("reconcile-missing", 0, 2), ("reconcile-unknown-order", 0, 2), ("reconcile-accepted", 2, 0),
        ("reconcile-missing-fills", 0, 2), ("reconcile-ingest", 2, 0), ("reconcile-incomplete", 0, 2),
        ("reconcile-filled", 2, 0), ("reconcile-after-terminal", 0, 2), ("reconcile-unresolved-state", 0, 1),
        ("has-unresolved-replace", 1, 0), ("reconcile-stop-accepted", 1, 0), ("reconcile-stop-incomplete", 0, 1),
        ("reconcile-stop-missing-fills", 0, 1), ("reconcile-stop-unresolved", 0, 1),
    ))
    assert_steps(tally, 37)
    print("\nT5 tally:", dict(sorted(tally.items())))


def test_t6_brackets(tmp_path):
    """T6: create_bracket, move_stop, close_bracket, reduce_bracket, _send_reduce,
    _bracket_children, _open_bracket_stop: every family together, as the parity suite walks."""
    tally = Counter()
    for seed in range(SEEDS):
        pair = FlowPair(tmp_path / str(seed), tally)
        try:
            bracket_walk(pair, seed)
        finally:
            pair.finish()
    for label in ("create", "submit", "partial-fill", "entry-cancel", "move", "move-noop",
                  "reduce", "reduce-replay", "reduce-fill", "close", "close-replay", "close-fill"):
        assert tally[label + ":ok"] == SEEDS, (label, tally)
    for kind in ("sim", "snapshot"):
        for seed in range(4):
            pair = FlowPair(tmp_path / f"{kind}-{seed}", tally, lambda clock, kind=kind: real_venue(kind, clock))
            try:
                value = intent(command=f"real:{seed}", targets=() if seed % 2 else (D("105"),))
                entry = f"real:{seed}:entry"
                pair.do("venue-create", lambda w: w.manager.create_bracket(value, D("7")))
                pair.do("venue-submit", lambda w: w.manager.submit(w.manager.get_order(entry)))
                for n, price in enumerate((D("99"), D("101"), D("106"), D("94"))):
                    at = NOW + timedelta(minutes=n)
                    pair.both(lambda w, at=at: w.clock.advance_to(at))
                    if kind == "sim":
                        bar = Bar(XYZ, at, price, price + 1, price - 1, price, 1000, at)
                        pair.do("venue-price", lambda w, bar=bar: w.broker.process_bar(bar))
                    else:
                        snapshot = ChainSnapshot(XYZ.symbol, at, price, (), None, None, "parity")
                        pair.do("venue-price", lambda w, snapshot=snapshot: w.broker.process_snapshot(snapshot))
                    pair.do("venue-reconcile", lambda w: w.manager.reconcile_order(entry))
                    pair.do("venue-stop", lambda w: w.manager.update_emulated_order(f"real:{seed}:stop", price, command_id=f"real:{n}"))
                pair.do("venue-close", lambda w: w.manager.close_bracket(entry, command_id="close", reason="real"))
            finally:
                pair.finish()
    for instrument in (XYZ, OPT):
        pair = FlowPair(tmp_path / f"runner-{type(instrument).__name__}", tally)
        try:
            value = replace(intent(fractions=(D(".25"), D(".25"))), instrument=instrument)
            pair.do("runner-create", lambda w: w.manager.create_bracket(value, D("7")))
            pair.do("runner-children", lambda w: w.manager._bracket_children("bracket:entry"))
            pair.do("unfilled-stop", lambda w: w.manager._open_bracket_stop("bracket:entry"))
            pair.do("not-entry", lambda w: w.manager._open_bracket_stop("bracket:stop"))
            pair.do("runner-submit", lambda w: w.manager.submit(w.manager.get_order("bracket:entry")))
            pair.do("stored-conflict", lambda w: w.manager.submit(replace(w.manager.get_order("bracket:entry"), quantity=D("8"))))
            pair.do("runner-full-entry", lambda w: w.fill("bracket:entry", "7", "entry"))
            pair.do("terminal-child", lambda w: w.manager._ensure_child_quantity(w.manager.get_order("bracket:entry"), D("2"), "child"))
            pair.do("route-no-price", lambda w: w.manager._route_emulated_trigger(w.manager.get_order("bracket:stop"), None, "none"))
            target_size = "2" if instrument == XYZ else "1.75"
            pair.do("runner-target", lambda w: w.fill("bracket:target:1", target_size, "target", D("105")))
            pair.do("runner-stop", lambda w: w.fill("bracket:stop", str(D("7") - D(target_size)), "stop", D("95")))
            pair.do("runner-flat", lambda w: w.manager.close_bracket("bracket:entry", command_id="flat", reason="flat"))
        finally:
            pair.finish()
    for index, types in enumerate((frozenset({OrderType.LIMIT}), frozenset(OrderType))):
        pair = FlowPair(tmp_path / f"entries-{index}", tally,
                        lambda clock, types=types: FaultBroker(order_types=types, native_stops=bool(index),
                                                               tifs=frozenset({TimeInForce.DAY})))
        try:
            pair.do("stop-entry", lambda w: w.manager.create_bracket(replace(intent(), entry_type=OrderType.STOP), D("7")))
            pair.do("stoplimit-entry", lambda w: w.manager.create_bracket(
                replace(intent(command="sl"), entry_type=OrderType.STOP_LIMIT, entry_limit_price=D("101")), D("7")))
            pair.do("tif-bracket", lambda w: w.manager.create_bracket(
                replace(intent(command="gtc"), entry_tif=TimeInForce.GTC, exit_tif=TimeInForce.GTC), D("7")))
            pair.do("invalid-fraction", lambda w: w.manager.reduce_bracket("missing", D("NaN"), command_id="invalid", reason="invalid"))
            pair.do("invalid-quantity", lambda w: w.manager.create_bracket(intent(command="q"), D("0")))
        finally:
            pair.finish()
    for label in ("runner-create", "runner-submit", "runner-full-entry", "runner-target", "runner-stop"):
        assert tally[label + ":ok"] == 2, (label, dict(tally))
    for label in ("not-entry", "stored-conflict", "route-no-price", "runner-flat", "invalid-fraction", "invalid-quantity"):
        assert tally[label + ":raise"] == 2, (label, dict(tally))
    assert tally["venue-price:ok"] == 32 and tally["venue-reconcile:ok"] == 32
    assert_steps(tally, SEEDS * 38 + 8 * 15 + 2 * 12 + 2 * 5)
    print("\nT6 tally:", {k: v for k, v in sorted(tally.items()) if not k.startswith("exception:")})


def test_flow_refuses_unknown_operation_and_bad_request():
    """The door itself: an unknown operation and a malformed request are refusals."""
    from trade_engine.oms.manager import _Host

    host = _Host(None, FaultBroker(), None, None)
    for op, request in (("no_such_operation", "{}"), ("get_order", "{}"), ("get_order", "not json")):
        try:
            trade_engine_rs.oms_flow(op, host, request)
        except ValueError as err:
            assert len(err.args) == 2 and err.args[0] in ("value", "json", "unsupported"), err.args
        else:
            raise AssertionError((op, request))

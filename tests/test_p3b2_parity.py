"""P3b-2a: the frozen c878969 manager versus the Rust decision shims.

No optional extension import. Two worlds have separate ledgers, venues and clocks.
Every command compares exact refusals/returns, events and outbox; each walk ends by
comparing the canonical folded state. Randomness is seeded and coverage is counted.
"""
from __future__ import annotations

import json
import random
import sys
from collections import Counter
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import trade_engine_rs  # noqa: F401 - D5, not importorskip

sys.path.insert(0, str(Path(__file__).resolve().parent))
from frozen_p3b2.oracle_manager import OrderManager as Oracle
from test_oms import FakeBroker
from test_p3a_parity import CAL, canon, run
from trade_engine.clock.replay import ReplayClock
from trade_engine.domain.instruments import Equity, OptionContract, OptionRight, Side
from trade_engine.domain.orders import Order, OrderState, OrderType, TimeInForce
from trade_engine.domain.portfolio import Fill
from trade_engine.domain.signals import OrderIntent
from trade_engine.interfaces.broker import OrderChanges, VenueAck, VenueOrderState
from trade_engine.interfaces.market_data import Bar
from trade_engine.ledger import Event, EventKind, Ledger, OrderStateChange, codec
from trade_engine.market_data.chains import ChainSnapshot
from trade_engine.oms.manager import OrderManager as Production
from trade_engine.sim import SimBroker, SnapshotVenue

D = Decimal
ACC = "P3B2"
XYZ = Equity("XYZ")
OPT = OptionContract("XYZ", date(2026, 12, 18), D("100"), OptionRight.CALL)
NOW = datetime(2026, 9, 23, 13, 30, tzinfo=UTC)
SEEDS = 48


def intent(command="bracket", *, targets=(D("105"), D("110")), fractions=None, **kw):
    return OrderIntent(
        intent_id="intent", account_id=ACC, instrument=XYZ, side=Side.BUY,
        quantity_rule="odd lot", entry_price=D("100"), stop_loss=D("95"),
        profit_targets=targets, target_fractions=fractions,
        reason="évidence 漢", command_id=command, **kw,
    )


class FaultBroker(FakeBroker):
    """Scripted ambiguous network boundaries, in addition to the real venues."""
    def __init__(self, **kw):
        super().__init__(**kw)
        self.cancel_error = self.replace_error = None

    def cancel(self, venue_order_id):
        if self.cancel_error:
            raise self.cancel_error
        return super().cancel(venue_order_id)

    def replace(self, venue_order_id, changes):
        if self.replace_error:
            raise self.replace_error
        return super().replace(venue_order_id, changes)


class World:
    def __init__(self, root, cls, venue):
        root.mkdir(parents=True, exist_ok=True)
        self.clock = ReplayClock(NOW)
        self.ledger = Ledger(root / "ledger.db").open()
        self.broker = venue(self.clock)
        self.manager = cls(self.broker, self.clock, self.ledger)

    def events(self):
        return [(e.seq, e.account, e.kind.value, e.command_id, e.ts_utc.isoformat(),
                 codec.text(codec.encode_payload(e.payload))) for e in self.ledger.events()]

    def outbox(self):
        return [(i.id, i.event_seq, i.destination, json.dumps(i.payload, sort_keys=True),
                 i.status.value, i.created_at.isoformat()) for i in self.ledger.pending_outbox()]

    def fill(self, order_id, quantity, fill_id, price=D("100")):
        order = self.manager.get_order(order_id)
        return self.manager.record_fill(Fill(
            fill_id=fill_id, order_id=order_id, account_id=order.account_id,
            instrument=order.instrument, quantity=D(quantity), price=price,
            venue_env=self.broker.env, filled_at=self.clock.now_utc(), side=order.side,
            venue_order_id=order_id, venue_execution_id=fill_id,
        ))

    def standalone(self, name="standalone", typ=OrderType.LIMIT, side=Side.SELL, **kw):
        return Order(order_id=name, account_id=ACC, instrument=XYZ, order_type=typ,
                     side=side, quantity=D("7"), command_id=name,
                     created_at=self.clock.now_utc(), **kw)


class Pair:
    def __init__(self, root, tally, venue=lambda clock: FaultBroker()):
        self.o = World(root / "oracle", Oracle, venue)
        self.p = World(root / "production", Production, venue)
        self.tally = tally
        self.n = 0

    def do(self, label, fn):
        self.n += 1
        a, b = run(fn, self.o), run(fn, self.p)
        assert a == b, (label, self.n, a, b)
        self.tally["steps"] += 1
        self.tally[f"{label}:{a[0]}"] += 1
        if a[0] == "raise":
            self.tally[f"exception:{a[1]}"] += 1
            self.tally[f"refusal:{a[1]}:{a[2]}"] += 1
        assert self.o.events() == self.p.events(), (label, "events", self.n)
        assert self.o.outbox() == self.p.outbox(), (label, "outbox", self.n)
        return a

    def both(self, fn):
        fn(self.o)
        fn(self.p)

    def finish(self):
        try:
            assert codec.text(codec.canon(self.o.ledger.state(ACC))) == codec.text(
                codec.canon(self.p.ledger.state(ACC)))
        finally:
            self.o.ledger.close()
            self.p.ledger.close()


def test_quantity_and_fingerprint_grid():
    tally = Counter()
    for inst in (XYZ, OPT):
        for q in ("0", "-1", "1", "2", "3", "7", "11", "101", "1.5", "7.00", "0.001"):
            for weights in ((), ("1",), ("1", "1"), ("1", "2", "1"), ("0", "2"),
                            ("-1", "2"), ("1.5", "2"), ("0.1", "0.2", "0.7")):
                args = D(q), tuple(map(D, weights)), inst
                a = run(Oracle._allocate_quantity, *args)
                b = run(Production._allocate_quantity, *args)
                assert a == b, (args, a, b)
                tally["allocate:" + a[0]] += 1
            for count in range(5):
                a, b = run(Oracle._split_quantity, D(q), count, inst), run(Production._split_quantity, D(q), count, inst)
                assert a == b, (inst, q, count, a, b)
                tally["split:" + a[0]] += 1
            for weights in ((), ("1",), ("0.3", "0.3"), ("0.1", "0.2", "0.7"), ("0.25", "0.25"), ("0", "1")):
                args = D(q), tuple(map(D, weights)), inst
                a, b = run(Oracle._fraction_quantities, *args), run(Production._fraction_quantities, *args)
                assert a == b, (args, a, b)
                tally["fractions:" + a[0]] += 1
    for i in range(100):
        r = random.Random(i)
        order_intent = intent(str(i), targets=(D("105"),), fractions=(D("0.30"),),
                              entry_type=OrderType.LIMIT)
        for typ in (OrderType.LIMIT, OrderType.STOP, OrderType.STOP_LIMIT):
            value = replace(order_intent, entry_type=typ,
                            entry_limit_price=D("101.00") if typ is OrderType.STOP_LIMIT else None)
            assert Oracle._bracket_fingerprint(value, D("7.00")) == Production._bracket_fingerprint(value, D("7.00"))
            tally["bracket_fingerprint"] += 1
        assert Oracle._reduce_fingerprint(str(i), D("0.30"), "漢") == Production._reduce_fingerprint(str(i), D("0.30"), "漢")
        tally["reduce_fingerprint"] += 1
        o = Order(order_id=str(i), account_id=ACC, instrument=XYZ, order_type=OrderType.LIMIT,
                  side=r.choice(list(Side)), quantity=D("7"), command_id=str(i),
                  created_at=NOW + timedelta(seconds=i), limit_price=D("99.50"))
        assert Oracle._fingerprint_order(o) == Production._fingerprint_order(o)
        tally["order_fingerprint"] += 1
    for name, successes, refusals in (("allocate", 104, 72), ("split", 80, 30), ("fractions", 77, 55)):
        assert tally[name + ":ok"] == successes
        assert tally[name + ":raise"] == refusals
    assert tally["bracket_fingerprint"] == 300 and tally["order_fingerprint"] == 100
    assert tally["reduce_fingerprint"] == 100
    print("\nquantity/fingerprint tally:", dict(tally))


def bracket_walk(pair, seed):
    r = random.Random(seed)
    q = D(r.choice([7, 9, 11, 13]))
    targets = () if seed % 3 == 0 else (D("105"), D("110"))
    fractions = None if seed % 2 or not targets else (D("0.25"), D("0.25"))
    value = intent(targets=targets, fractions=fractions)
    pair.do("create", lambda w: w.manager.create_bracket(value, q))
    pair.do("create-replay", lambda w: w.manager.create_bracket(value, q))
    pair.do("create-conflict", lambda w: w.manager.create_bracket(replace(value, reason="conflict"), q))
    pair.do("held-child", lambda w: w.manager.submit(w.manager.get_order("bracket:stop")))
    pair.do("submit", lambda w: w.manager.submit(w.manager.get_order("bracket:entry")))
    pair.do("partial-fill", lambda w: w.fill("bracket:entry", "3", "partial"))
    pair.do("protective-cancel", lambda w: w.manager.cancel("bracket:stop", command_id="unsafe"))
    pair.do("entry-cancel", lambda w: w.manager.cancel("bracket:entry", command_id="entry-cancel"))
    pair.do("fill-replay", lambda w: w.fill("bracket:entry", "3", "partial"))
    pair.do("move-loosen", lambda w: w.manager.move_stop("bracket:entry", D("94"), command_id="loosen"))
    pair.do("move", lambda w: w.manager.move_stop("bracket:entry", D("96"), command_id="tighten"))
    pair.do("move-noop", lambda w: w.manager.move_stop("bracket:entry", D("96"), command_id="same"))
    pair.do("reduce-zero", lambda w: w.manager.reduce_bracket("bracket:entry", D("0.1"), command_id="zero", reason="tiny"))
    pair.do("reduce", lambda w: w.manager.reduce_bracket("bracket:entry", D("0.5"), command_id="reduce", reason="partial"))
    pair.do("reduce-replay", lambda w: w.manager.reduce_bracket("bracket:entry", D("0.5"), command_id="reduce", reason="partial"))
    pair.do("reduce-conflict", lambda w: w.manager.reduce_bracket("bracket:entry", D("0.6"), command_id="reduce", reason="partial"))
    pair.do("reduce-working", lambda w: w.manager.reduce_bracket("bracket:entry", D("0.5"), command_id="other", reason="overlap"))
    pair.do("close-reduce-working", lambda w: w.manager.close_bracket("bracket:entry", command_id="close", reason="overlap"))
    pair.do("reduce-fill", lambda w: w.fill("bracket:entry:reduce:1", "1", "reduce-fill"))
    pair.do("close", lambda w: w.manager.close_bracket("bracket:entry", command_id="close", reason="done"))
    pair.do("close-replay", lambda w: w.manager.close_bracket("bracket:entry", command_id="close", reason="ignored on replay"))
    pair.do("close-conflict", lambda w: w.manager.close_bracket("bracket:entry", command_id="new-close", reason="done"))
    pair.do("reduce-close-working", lambda w: w.manager.reduce_bracket("bracket:entry", D("0.5"), command_id="after-close", reason="overlap"))
    pair.do("close-fill", lambda w: w.fill("bracket:entry:close", "2", "close-fill"))
    pair.do("flat-close", lambda w: w.manager.close_bracket("bracket:entry", command_id="close", reason="done"))
    pair.do("flat-reduce", lambda w: w.manager.reduce_bracket("bracket:entry", D("0.5"), command_id="flat", reason="flat"))
    for index in range(12):
        op = r.choice(["replace", "reduce", "move", "cancel", "reconcile"])
        oid = r.choice(["bracket:entry", "bracket:stop", "bracket:entry:close"])
        key = f"walk:{index}"
        if op == "replace":
            replacement_quantity = D(r.randrange(1, 10))
            pair.do("walk-replace", lambda w: w.manager.replace(oid, OrderChanges(new_quantity=replacement_quantity), command_id=key))
        elif op == "reduce":
            fraction = D(r.choice(["0", "1", "NaN", "0.3", "0.5"]))
            pair.do("walk-reduce", lambda w: w.manager.reduce_bracket("bracket:entry", fraction, command_id=key, reason=key))
        elif op == "move":
            pair.do("walk-move", lambda w: w.manager.move_stop("bracket:entry", D("97"), command_id=key))
        elif op == "cancel":
            pair.do("walk-cancel", lambda w: w.manager.cancel(oid, command_id=key))
        else:
            pair.do("walk-reconcile", lambda w: w.manager.reconcile_order(oid))


def test_seeded_bracket_command_walks(tmp_path):
    tally = Counter()
    for seed in range(SEEDS):
        pair = Pair(tmp_path / str(seed), tally)
        try:
            bracket_walk(pair, seed)
        finally:
            pair.finish()
    for label in ("create", "submit", "partial-fill", "entry-cancel", "move", "move-noop",
                  "reduce", "reduce-replay", "reduce-fill", "close", "close-replay", "close-fill"):
        assert tally[label + ":ok"] == SEEDS, (label, tally)
    for label in ("create-conflict", "held-child", "protective-cancel", "move-loosen",
                  "reduce-zero", "reduce-conflict", "reduce-working", "close-reduce-working",
                  "close-conflict", "reduce-close-working", "flat-reduce"):
        assert tally[label + ":raise"] == SEEDS, (label, tally)
    assert tally["steps"] == SEEDS * 38
    print("\nbracket walk tally:", {k: v for k, v in sorted(tally.items()) if not k.startswith("refusal:")})


def test_emulated_and_native_price_paths(tmp_path):
    tally = Counter()
    for native in (False, True):
        for typ in (OrderType.STOP, OrderType.STOP_LIMIT, OrderType.TRAIL):
            for side in Side:
                pair = Pair(tmp_path / f"{native}-{typ.value}-{side.value}", tally,
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
    for label, successes, refusals in (("observe", 30, 30), ("local-replace", 8, 4), ("trail-api", 2, 10)):
        assert tally[label + ":ok"] == successes and tally[label + ":raise"] == refusals, (label, tally)
    assert tally["observe-invalid:raise"] == 12
    assert tally["steps"] == 12 * 18
    print("\nprice path tally:", {k: v for k, v in sorted(tally.items()) if not k.startswith("refusal:")})


def real_venue(kind, clock):
    venue = SimBroker(ACC, clock, D("0")) if kind == "sim" else SnapshotVenue(ACC, clock)
    venue.connect()
    return venue


def test_real_venues_lockstep(tmp_path):
    tally = Counter()
    for kind in ("sim", "snapshot"):
        for seed in range(12):
            pair = Pair(tmp_path / f"{kind}-{seed}", tally, lambda clock, kind=kind: real_venue(kind, clock))
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
    assert tally["venue-price:ok"] == 96
    assert tally["venue-reconcile:ok"] == 96
    assert tally["venue-stop:ok"] == 38 and tally["venue-stop:raise"] == 58
    assert tally["steps"] == 360
    print("\nreal venue tally:", {k: v for k, v in sorted(tally.items()) if not k.startswith("refusal:")})


def test_refusals_and_ambiguous_replace(tmp_path):
    tally = Counter()
    for status in ("ACCEPTED", "REJECTED", "PENDING", "unexpected"):
        pair = Pair(tmp_path / status, tally)
        try:
            pair.do("standalone-submit", lambda w: w.manager.submit(w.standalone(limit_price=D("100"))))
            pair.do("replace-zero", lambda w: w.manager.replace("standalone", OrderChanges(new_quantity=D("0")), command_id="zero"))
            pair.do("replace-fractional", lambda w: w.manager.replace("standalone", OrderChanges(new_quantity=D("1.5")), command_id="fraction"))
            pair.do("replace-noop", lambda w: w.manager.replace("standalone", OrderChanges(), command_id="noop"))
            pair.do("replace-noop-replay", lambda w: w.manager.replace("standalone", OrderChanges(), command_id="noop"))
            pair.do("replace-noop-conflict", lambda w: w.manager.replace("standalone", OrderChanges(new_quantity=D("6")), command_id="noop"))
            pair.both(lambda w: setattr(w.broker, "replace_status", status))
            pair.do("replace-status", lambda w: w.manager.replace("standalone", OrderChanges(new_quantity=D("6")), command_id="replace"))
            pair.do("replace-status-replay", lambda w: w.manager.replace("standalone", OrderChanges(new_quantity=D("6")), command_id="replace"))
            pair.do("replace-status-conflict", lambda w: w.manager.replace("standalone", OrderChanges(new_quantity=D("5")), command_id="replace"))
            pair.both(lambda w: setattr(w.broker, "order_readback", [
                VenueOrderState("standalone", OrderState.ACCEPTED, D("0"), D("6"), NOW)]))
            pair.do("reconcile-replace", lambda w: w.manager.reconcile_order("standalone"))
            pair.both(lambda w: setattr(w.broker, "order_readback", [
                VenueOrderState("standalone", OrderState.CANCELLED, D("0"), D("6"), NOW)]))
            pair.do("reconcile-terminal", lambda w: w.manager.reconcile_order("standalone"))
        finally:
            pair.finish()
    for index, types in enumerate((frozenset({OrderType.LIMIT}), frozenset({OrderType.MARKET}), frozenset())):
        pair = Pair(tmp_path / f"types-{index}", tally,
                    lambda clock, types=types: FaultBroker(order_types=types, native_stops=False))
        try:
            pair.do("unsupported-stop-entry", lambda w: w.manager.create_bracket(
                replace(intent(), entry_type=OrderType.STOP), D("7")))
            pair.do("unsupported-stoplimit-entry", lambda w: w.manager.create_bracket(
                replace(intent(), entry_type=OrderType.STOP_LIMIT, entry_limit_price=D("101")), D("7")))
            pair.do("capability-stop", lambda w: w.manager.submit(w.standalone(typ=OrderType.STOP, stop_price=D("100"))))
            pair.do("capability-observe", lambda w: w.manager.update_emulated_order("standalone", D("99"), command_id="observe"))
        finally:
            pair.finish()
    for fraction in (D("-1"), D("0"), D("1"), D("3"), D("Infinity"), D("NaN")):
        pair = Pair(tmp_path / f"fraction-{str(fraction)}", tally)
        try:
            pair.do("invalid-fraction", lambda w: w.manager.reduce_bracket("missing", fraction, command_id="invalid", reason="invalid"))
            pair.do("invalid-quantity", lambda w: w.manager.create_bracket(intent(), fraction))
            pair.do("missing-order", lambda w: w.manager.get_order("missing"))
        finally:
            pair.finish()
    for label in ("replace-status", "replace-status-replay", "reconcile-replace",
                  "capability-observe", "invalid-quantity"):
        assert tally[label + ":ok"] > 0 and tally[label + ":raise"] > 0, (label, tally)
    for label in ("replace-zero", "replace-fractional", "replace-noop-conflict",
                  "replace-status-conflict", "unsupported-stop-entry", "unsupported-stoplimit-entry",
                  "invalid-fraction", "missing-order"):
        assert tally[label + ":raise"] > 0, (label, tally)
    assert tally["steps"] == 74
    assert tally["capability-stop:ok"] == 2
    assert tally["capability-stop:raise"] == 1
    print("\nrefusal campaign tally:", {k: v for k, v in sorted(tally.items()) if not k.startswith("refusal:")})


def test_boundary_refusal_counterparts(tmp_path):
    tally = Counter()
    for seed in range(2):
        pair = Pair(tmp_path / f"boundary-{seed}", tally)
        try:
            pair.do("boundary-create", lambda w: w.manager.create_bracket(intent(), D("7")))
            pair.do("unfilled-stop", lambda w: w.manager._open_bracket_stop("bracket:entry"))
            pair.do("not-entry", lambda w: w.manager._open_bracket_stop("bracket:stop"))
            pair.do("boundary-submit", lambda w: w.manager.submit(w.manager.get_order("bracket:entry")))
            pair.do("stored-conflict", lambda w: w.manager.submit(replace(w.manager.get_order("bracket:entry"), quantity=D("8"))))
            pair.do("boundary-fill", lambda w: w.fill("bracket:entry", "7", "full"))
            pair.do("unfilled-stop", lambda w: w.manager._open_bracket_stop("bracket:entry"))
            pair.do("fill-environment", lambda w: w.manager.record_fill(Fill(
                fill_id="wrong", order_id="bracket:entry", account_id=ACC, instrument=XYZ,
                quantity=D("1"), price=D("100"), venue_env="paper", filled_at=NOW, side=Side.BUY)))
            pair.do("terminal-child", lambda w: w.manager._ensure_child_quantity(w.manager.get_order("bracket:stop"), D("2"), "child"))
            pair.do("terminal-child", lambda w: w.manager._ensure_child_quantity(w.manager.get_order("bracket:entry"), D("2"), "child"))
            pair.do("replace-below-fill", lambda w: w.manager.replace("bracket:stop", OrderChanges(new_quantity=D("NaN")), command_id="bad"))
            pair.do("route-no-price", lambda w: w.manager._route_emulated_trigger(w.manager.get_order("bracket:stop"), None, "none"))
            pair.do("native-trail", lambda w: w.manager.submit_trailing(w.standalone(limit_price=D("100"))))
            pair.both(lambda w: setattr(w.broker, "order_readback", [
                VenueOrderState("bracket:stop", OrderState.ACCEPTED, D("0"), D("2"), NOW)]))
            pair.do("reconcile-accepted", lambda w: w.manager.reconcile_order("bracket:stop"))
            pair.both(lambda w: setattr(w.broker, "order_readback", [
                VenueOrderState("bracket:stop", OrderState.FILLED, D("2"), D("0"), NOW)]))
            pair.do("reconcile-incomplete", lambda w: w.manager.reconcile_order("bracket:stop"))
            pair.both(lambda w: setattr(w.broker, "order_readback", [
                VenueOrderState("bracket:stop", OrderState.PARTIALLY_FILLED, D("1"), D("1"), NOW)]))
            pair.do("reconcile-missing-fills", lambda w: w.manager.reconcile_order("bracket:stop"))
            pair.both(lambda w: setattr(w.broker, "order_readback", [
                VenueOrderState("bracket:stop", OrderState.NEW, D("0"), D("2"), NOW)]))
            pair.do("reconcile-unresolved-state", lambda w: w.manager.reconcile_order("bracket:stop"))
        finally:
            pair.finish()
    for seed, status in enumerate(("ACCEPTED", "PENDING", "REJECTED", "bad-status")):
        pair = Pair(tmp_path / f"cancel-{seed}", tally)
        try:
            pair.do("cancel-submit", lambda w: w.manager.submit(w.standalone(limit_price=D("100"))))
            pair.both(lambda w: setattr(w.broker, "cancel_status", status))
            pair.do("cancel-result", lambda w: w.manager.cancel("standalone", command_id="cancel"))
            pair.do("oco-pending", lambda w: w.manager._cancel_exits([w.manager.get_order("standalone")], "oco"))
        finally:
            pair.finish()
    for method in ("submit", "replace", "cancel"):
        pair = Pair(tmp_path / f"network-{method}", tally)
        try:
            if method != "submit":
                pair.do("network-setup", lambda w: w.manager.submit(w.standalone(limit_price=D("100"))))
            pair.both(lambda w: setattr(w.broker, method + "_error", OSError("socket lost")))
            if method == "submit":
                fn = lambda w: w.manager.submit(w.standalone(limit_price=D("100")))
            elif method == "replace":
                fn = lambda w: w.manager.replace("standalone", OrderChanges(new_quantity=D("6")), command_id="lost")
            else:
                fn = lambda w: w.manager.cancel("standalone", command_id="lost")
            pair.do("network-unknown", fn)
            pair.do("network-pending", lambda w: w.manager.replace("standalone", OrderChanges(new_quantity=D("5")), command_id="other"))
            pair.do("pending-emulation", lambda w: w.manager._start_emulation(w.manager.get_order("standalone")))
            pair.do("pending-trigger", lambda w: w.manager._submit_emulated(
                w.manager.get_order("standalone"), D("100"), "trigger", venue_type=OrderType.MARKET, limit_price=None))
        finally:
            pair.finish()
    for tif in (TimeInForce.DAY, TimeInForce.GTC):
        pair = Pair(tmp_path / tif.value, tally,
                    lambda clock: FaultBroker(tifs=frozenset({TimeInForce.DAY})))
        try:
            pair.do("tif-bracket", lambda w: w.manager.create_bracket(
                replace(intent(), entry_tif=tif, exit_tif=tif), D("7")))
            pair.do("tif-standalone", lambda w: w.manager.submit(
                w.standalone(limit_price=D("100"), tif=tif)))
        finally:
            pair.finish()
    for side in Side:
        pair = Pair(tmp_path / f"touch-{side.value}", tally,
                    lambda clock: FaultBroker(order_types=frozenset({OrderType.LIMIT}), native_stops=False))
        try:
            pair.do("touch-submit", lambda w: w.manager.submit(w.standalone(
                typ=OrderType.STOP, side=side, stop_price=D("100"))))
            pair.do("stop-touch", lambda w: w.manager.update_emulated_order("standalone", D("100"), command_id="touch"))
        finally:
            pair.finish()
    for label, successes, refusals in (
        ("unfilled-stop", 2, 2), ("terminal-child", 2, 2), ("cancel-result", 2, 2),
        ("oco-pending", 1, 3), ("tif-bracket", 1, 1), ("tif-standalone", 1, 1),
    ):
        assert tally[label + ":ok"] == successes and tally[label + ":raise"] == refusals, (label, tally)
    for label in ("not-entry", "stored-conflict", "fill-environment", "replace-below-fill",
                  "route-no-price", "native-trail", "reconcile-incomplete", "reconcile-missing-fills",
                  "reconcile-unresolved-state", "network-unknown", "network-pending",
                  "pending-emulation", "pending-trigger"):
        assert tally[label + ":raise"] > 0, (label, tally)
    assert tally["stop-touch:ok"] == 2
    assert tally["reconcile-accepted:ok"] == 2
    assert tally["steps"] == 68
    print("\nboundary tally:", {k: v for k, v in sorted(tally.items()) if not k.startswith("refusal:")})


def test_unbounded_integer_and_datetime_carriers(tmp_path):
    checks = 0
    for q in ("100000000000000000000000000000000000000000001", "-100000000000000000000000000000000000000000001"):
        for weights in (("1", "2", "1"), ("100000000000000000000000000000001", "100000000000000000000000000000002")):
            args = D(q), tuple(map(D, weights)), XYZ
            assert run(Oracle._allocate_quantity, *args) == run(Production._allocate_quantity, *args)
            checks += 1
        args = D(q), (D(".3"), D(".3")), XYZ
        assert run(Oracle._fraction_quantities, *args) == run(Production._fraction_quantities, *args)
        checks += 1
    args = D("0E+999999999999999999"), (D("1"), D("2")), XYZ
    assert run(Oracle._allocate_quantity, *args) == run(Production._allocate_quantity, *args)
    checks += 1
    pair = Pair(tmp_path, Counter())
    try:
        pair.do("carrier-submit", lambda w: w.manager.submit(w.standalone(limit_price=D("100"))))
        pair.do("same-instant-replay", lambda w: w.manager.submit(replace(w.standalone(limit_price=D("100")),
            created_at=NOW.astimezone(timezone(timedelta(hours=5, minutes=30))))))
    finally:
        pair.finish()
    assert checks == 7


def test_partial_exit_runner_and_ack_paths(tmp_path):
    tally = Counter()
    for instrument in (XYZ, OPT):
        pair = Pair(tmp_path / type(instrument).__name__, tally)
        try:
            value = replace(intent(fractions=(D(".25"), D(".25"))), instrument=instrument)
            pair.do("runner-create", lambda w: w.manager.create_bracket(value, D("7")))
            pair.do("runner-submit", lambda w: w.manager.submit(w.manager.get_order("bracket:entry")))
            pair.do("runner-partial-entry", lambda w: w.fill("bracket:entry", "3", "entry-partial"))
            pair.do("runner-full-entry", lambda w: w.fill("bracket:entry", "4", "entry-rest"))
            target_size = "2" if instrument == XYZ else "1.75"
            pair.do("runner-target-partial", lambda w: w.fill("bracket:target:1", "1", "target-partial", D("105")))
            pair.do("replace-filled-bound", lambda w: w.manager.replace(
                "bracket:target:1", OrderChanges(new_quantity=D(".5")), command_id="below"))
            rest = str(D(target_size) - 1)
            pair.do("runner-target-rest", lambda w: w.fill("bracket:target:1", rest, "target-rest", D("105")))
            pair.do("runner-stop-partial", lambda w: w.fill("bracket:stop", "2", "stop-partial", D("95")))
            remaining = str(D("7") - D(target_size) - 2)
            pair.do("runner-stop-rest", lambda w: w.fill("bracket:stop", remaining, "stop-rest", D("95")))
            pair.do("runner-flat", lambda w: w.manager.close_bracket("bracket:entry", command_id="flat", reason="flat"))
        finally:
            pair.finish()
    for status in ("ACCEPTED", "PENDING", "REJECTED", "unrecognized"):
        pair = Pair(tmp_path / status, tally)
        try:
            pair.do("ack-create", lambda w: w.manager.create_bracket(intent(targets=()), D("7")))
            pair.do("ack-entry-submit", lambda w: w.manager.submit(w.manager.get_order("bracket:entry")))
            pair.both(lambda w: setattr(w.broker, "submit_status", status))
            pair.do("ack-protection", lambda w: w.fill("bracket:entry", "7", "full"))
            pair.do("ack-open-stop", lambda w: w.manager._open_bracket_stop("bracket:entry"))
            pair.do("ack-submit-replay", lambda w: w.manager.submit(w.manager.get_order("bracket:stop")))
        finally:
            pair.finish()
    for label in ("runner-create", "runner-submit", "runner-partial-entry", "runner-full-entry",
                  "runner-target-partial", "runner-target-rest", "runner-stop-partial", "runner-stop-rest"):
        assert tally[label + ":ok"] == 2, (label, tally)
    assert tally["replace-filled-bound:raise"] == 2
    assert tally["runner-flat:raise"] == 2
    for label in ("ack-protection", "ack-open-stop"):
        assert tally[label + ":ok"] > 0 and tally[label + ":raise"] > 0, (label, tally)
    assert tally["ack-submit-replay:ok"] == 4
    assert tally["steps"] == 40
    print("\nrunner/ack tally:", {k: v for k, v in sorted(tally.items()) if not k.startswith("refusal:")})


def test_native_stops_flag_independent_of_advertised_types(tmp_path):
    tally = Counter()
    for native in (False, True):
        pair = Pair(tmp_path / str(native), tally, lambda clock, native=native: FaultBroker(
            order_types=frozenset(OrderType), native_stops=native))
        try:
            supported = pair.do("native-flag", lambda w: w.manager._supports_native_type(OrderType.STOP))
            assert supported == ("ok", ("B", native))
            pair.do("flag-stop-submit", lambda w: w.manager.submit(
                w.standalone(typ=OrderType.STOP, stop_price=D("100"))))
            pair.do("flag-stop-observe", lambda w: w.manager.update_emulated_order(
                "standalone", D("100"), command_id="touch"))
            pair.do("flag-stop-entry", lambda w: w.manager.create_bracket(
                replace(intent(), entry_type=OrderType.STOP), D("7")))
        finally:
            pair.finish()
    for label in ("flag-stop-observe", "flag-stop-entry"):
        assert tally[label + ":ok"] == tally[label + ":raise"] == 1
    assert tally["steps"] == 8


def test_nonfinite_quantity_helper_refusals():
    checks = 0
    for instrument in (XYZ, OPT):
        for value in ("NaN", "sNaN", "Infinity", "-Infinity"):
            for method, args in (
                ("_validate_quantity", (instrument, D(value))),
                ("_allocate_quantity", (D(value), (D("1"),), instrument)),
                ("_fraction_quantities", (D(value), (D(".5"),), instrument)),
            ):
                assert run(getattr(Oracle, method), *args) == run(getattr(Production, method), *args), (
                    instrument, method, value)
                checks += 1
    assert checks == 24

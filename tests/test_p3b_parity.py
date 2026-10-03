"""P3b-1 gate 1: reconcile, restore and the options-OMS decisions in Rust against the frozen
pre-port Python.

`tests/frozen_p3b/` holds the three modules each shim replaced, verbatim from 7a8a62b (only
the header differs). Every step below drives TWO worlds with the SAME inputs: the oracle
world (the frozen modules, its own ledger, clock and snapshot venue) and the production
world (the shims over Rust). Every step must agree: the return value with Decimals compared
by ``str`` (so scale matters), datetimes by ``isoformat``, or the refusal by exception type
name AND message. After each step the two ledgers' events and outboxes must be equal, and at
the end of a walk the folded state byte for byte.

Two generators:

* **Books**: a random folded book (``BookGen``: shares, options, structures with targets
  and closes, partial fills, cancels, expiries) loaded into both ledgers, then random
  ``open`` (new, replayed, conflicting), ``close``, ``close_holding`` and ``sync`` commands,
  ``restorable`` / ``restorable_positions`` in both pending modes, and journal fills (recorded
  or not, orders present or not).
* **Flows**: a real walk over sessions on a ``SnapshotVenue`` per world: entries with and
  without targets, snapshots that fill them, ``reconcile_after`` (with and without the
  journal), ``sync``, closes, venue requests left pending, restarts that rebuild the venue
  from the ledger (refuse and resolve), a venue order the ledger never knew, and orders whose
  parent the ledger lacks.

A missing ``trade_engine_rs`` is an ERROR (D5): it is imported unconditionally.
"""

from __future__ import annotations

import dataclasses
import json
import random
import sys
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import pytest
import trade_engine_rs  # noqa: F401 - a missing module is an ERROR, never a skip (D5)

from frozen_p3b import oracle_options as OOPT
from frozen_p3b import oracle_reconcile as OREC
from frozen_p3b import oracle_restore as ORES
from test_p3a_parity import (  # noqa: E402 - the shared generators and comparison
    CAL,
    NOW,
    OPTS,
    XYZ,
    BookGen,
    Tally,
    canon,
    cents,
    random_intent,
    random_snapshot,
    run,
)
from trade_engine.clock.replay import ReplayClock
from trade_engine.domain.instruments import Equity, Side
from trade_engine.domain.option_orders import CloseHolding, CloseStructure, is_structure
from trade_engine.domain.orders import Order, OrderState, OrderType
from trade_engine.domain.portfolio import Fill
from trade_engine.interfaces.broker import VenueOrder, VenueOrderAllocation
from trade_engine.ledger import CashFlow, Event, EventKind, Ledger, OrdersCreated, codec
from trade_engine.ledger.events import OrderStateChange
from trade_engine.oms import options as POPT
from trade_engine.oms import reconcile as PREC
from trade_engine.oms import restore as PRES
from trade_engine.sim import SnapshotVenue

D = Decimal
ACC = "P3A"
MIN = datetime.min.replace(tzinfo=UTC)
ORACLE = SimpleNamespace(opt=OOPT, rec=OREC, res=ORES)
PROD = SimpleNamespace(opt=POPT, rec=PREC, res=PRES)

BOOK_SEEDS = 700
FLOW_SEEDS = 220
FLOW_STEPS = 34

SESSIONS: list[date] = []
_d = date(2026, 9, 25)
while len(SESSIONS) < 24:
    if CAL.is_session(_d):
        SESSIONS.append(_d)
    _d += timedelta(days=1)


def snap_at(session: date) -> datetime:
    return CAL.session_close(session) - timedelta(minutes=15)


# -- the two worlds -----------------------------------------------------------------------


class World:
    """One account: a ledger, a clock, a snapshot venue and the options OMS over them."""

    def __init__(self, tmp: Path, mods, start: datetime) -> None:
        tmp.mkdir(parents=True, exist_ok=True)
        self.mods = mods
        self.clock = ReplayClock(start)
        self.ledger = Ledger(tmp / "ledger.db").open()
        self.rebuild_venue()

    def rebuild_venue(self) -> None:
        self.venue = SnapshotVenue(ACC, self.clock)
        self.venue.connect()
        self.oms = self.mods.opt.OptionOrderManager(self.venue, self.clock, self.ledger)

    def log(self) -> list:
        return [
            (e.seq, e.kind.value, e.command_id, e.ts_utc.isoformat(), codec.text(codec.encode_payload(e.payload)))
            for e in self.ledger.events(account=ACC)
        ]

    def outbox(self) -> list:
        return [
            (i.id, i.event_seq, i.destination, json.dumps(i.payload, sort_keys=True), i.status.value, i.created_at.isoformat())
            for i in self.ledger.pending_outbox()
        ]

    def state_bytes(self) -> str:
        return codec.text(codec.canon(self.ledger.state(ACC)))

    def close(self) -> None:
        self.ledger.close()


class Pair:
    """The oracle world and the production world, driven in lockstep."""

    def __init__(self, tmp: Path, start: datetime, what: str, tally: Tally) -> None:
        self.o = World(tmp / "oracle", ORACLE, start)
        self.p = World(tmp / "prod", PROD, start)
        self.what, self.tally, self.n = what, tally, 0

    def both(self, fn) -> None:
        """Run a setup action on both worlds; it must not raise."""
        fn(self.o)
        fn(self.p)

    def do(self, label: str, fn):
        self.n += 1
        a, b = run(fn, self.o), run(fn, self.p)
        assert a == b, f"{self.what} step {self.n} ({label})\noracle: {a!r:.3000}\nprod:   {b!r:.3000}"
        self.tally["steps"] += 1
        kind = a[0] if a[0] == "ok" else a[1]
        if kind == "RuntimeError" and "|" in a[2]:
            kind = a[2].split("|", 1)[0]  # the wrapped refusal of restorable
        self.tally[f"{label}:{kind}"] += 1
        la, lb = self.o.log(), self.p.log()
        assert la == lb, f"{self.what} step {self.n} ({label}) ledger events differ\noracle: {la[-4:]!r:.2000}\nprod:   {lb[-4:]!r:.2000}"
        oa, ob = self.o.outbox(), self.p.outbox()
        assert oa == ob, f"{self.what} step {self.n} ({label}) outbox differs\noracle: {oa[-3:]!r:.2000}\nprod:   {ob[-3:]!r:.2000}"
        return a

    def inject(self, ev) -> None:
        """Append an event to both worlds; an illegal transition must be refused by both alike."""
        a, b = run(lambda w: w.ledger.append(ev), self.o), run(lambda w: w.ledger.append(ev), self.p)
        assert a == b, f"{self.what}: inject differs {a!r} {b!r}"
        self.tally["inject:" + a[0]] += 1

    def finish(self) -> None:
        assert self.o.state_bytes() == self.p.state_bytes(), f"{self.what}: folded states differ"
        self.o.close()
        self.p.close()


# -- shared command generators ------------------------------------------------------------

PENDING_REASONS = [
    "Venue has not resolved order: slow",
    "Cancel pending: venue slow",
    "Venue cancel remains pending: slow",
    "Cancel pending for a replace",
    "something else",
]
PENDING_SUFFIXES = [":submit-pending", ":submit-pending", ":pending", ":pending", ":venue-pending", ":other"]


class Cmds:
    """Random commands, built from the oracle world's state (the worlds agree by assertion)."""

    def __init__(self, r: random.Random) -> None:
        self.r = r
        self.n = 0
        self.intents: list = []
        self.used: list[str] = []

    def fresh(self, prefix: str) -> str:
        self.n += 1
        return f"{prefix}{self.n}"

    def intent(self):
        r = self.r
        self.n += 1
        if self.intents and r.random() < 0.18:
            old = r.choice(self.intents)
            roll = r.random()
            if roll < 0.55:
                return old  # a replay
            try:
                if roll < 0.7:
                    return dataclasses.replace(old, quantity=old.quantity + 1)
                if roll < 0.85:
                    return dataclasses.replace(old, reason="changed")
                return dataclasses.replace(old, account_id="OTHER")
            except ValueError:
                return old
        intent = random_intent(r, self.n)
        if isinstance(intent.instrument, Equity) and r.random() < 0.3:
            intent = dataclasses.replace(intent, quantity=D(r.choice([100, 200, 300])), side=Side.BUY)
        if is_structure(intent.instrument) and r.random() < 0.5:
            try:
                intent = dataclasses.replace(intent, profit_target=cents(r.randint(5, 300)))
            except ValueError:
                pass
        self.intents.append(intent)
        self.used.append(intent.command_id)
        return intent

    def close_structure(self, state):
        r = self.r
        ids = sorted(state.orders)
        entry = r.choice(ids) if ids and r.random() < 0.9 else "nope"
        if self.used and r.random() < 0.25:
            command = r.choice(self.used)
        else:
            command = self.fresh("cl")
            self.used.append(command)
        limit = None if r.random() < 0.5 else cents(r.randint(1, 600))
        account = "OTHER" if r.random() < 0.05 else ACC
        return account, CloseStructure(entry_order_id=entry, reason="close", command_id=command, limit_price=limit)

    def close_holding(self):
        r = self.r
        if self.used and r.random() < 0.2:
            command = r.choice(self.used)
        else:
            command = self.fresh("ch")
            self.used.append(command)
        account = "OTHER" if r.random() < 0.05 else ACC
        return account, CloseHolding(
            instrument=r.choice([XYZ, XYZ, Equity("ABC")]),
            quantity=D(r.choice([1, 50, 100, 100, 200, 300, 1000])),
            reason="holding",
            command_id=command,
        )

    def pending_event(self, state, now: datetime):
        r = self.r
        ids = sorted(state.orders)
        if not ids:
            return None
        order_id = r.choice(ids)
        order = state.orders[order_id]
        suffix = r.choice(PENDING_SUFFIXES)
        base = order.command_id if r.random() < 0.7 else order_id
        self.n += 1
        return Event(
            account=ACC,
            kind=EventKind.ORDER_PENDING,
            payload=OrderStateChange(order_id, r.choice(PENDING_REASONS), None),
            ts_utc=now,
            command_id=f"{base}{suffix}.{self.n}" if r.random() < 0.15 else f"{base}{suffix}",
        )

    def phantom_fill(self, state, now: datetime):
        r = self.r
        self.n += 1
        ids = sorted(state.orders)
        order_id = r.choice(ids) if ids and r.random() < 0.8 else "ghost-order"
        inst = state.orders[order_id].instrument if order_id in state.orders else XYZ
        if isinstance(inst, Equity) is False and not hasattr(inst, "occ"):
            inst = XYZ
        return Fill(
            fill_id=f"phantom{self.n}",
            order_id=order_id,
            account_id=ACC,
            instrument=inst,
            quantity=D(1),
            price=cents(r.randint(5, 900)),
            venue_env="sim",
            filled_at=now,
            side=r.choice([Side.BUY, Side.SELL]),
            fee=cents(r.randint(0, 100)),
        )


def op_open(cmds: Cmds):
    intent = cmds.intent()
    return lambda w: w.oms.open(intent)


def op_close(cmds: Cmds, state):
    account, action = cmds.close_structure(state)
    return lambda w: w.oms.close(account, action)


def op_close_holding(cmds: Cmds):
    account, action = cmds.close_holding()
    return lambda w: w.oms.close_holding(account, action)


def op_sync(cmds: Cmds):
    cause = cmds.fresh("sync")
    return lambda w: w.oms.sync(ACC, cause)


def op_restorable(r: random.Random):
    """The restore decisions, in both modes, with the guard cases."""
    roll = r.random()
    if roll < 0.35:
        mode, resolve = "refuse", False
    elif roll < 0.85:
        mode, resolve = "resolve", True
    elif roll < 0.92:
        mode, resolve = "resolve", False  # needs a resolution
    else:
        mode, resolve = r.choice(["bad", "", "RESOLVE", None, 3, ("resolve",)]), r.random() < 0.5
    seeded = r.random() < 0.15

    def fn(w):
        state = w.ledger.state(ACC)
        resolution = None
        if resolve:
            resolution = w.mods.res.PendingResolution()
            if seeded and state.orders:
                resolution.unresolved[sorted(state.orders)[0]] = "earlier"
        kwargs = {} if mode == "refuse" and r is None else {"pending": mode, "resolution": resolution}
        try:
            orders, fills = w.mods.res.restorable(w.ledger, ACC, state, **kwargs)
        except Exception as err:  # noqa: BLE001 - the resolution at the refusal is compared too
            raise RuntimeError(f"{type(err).__name__}|{err}|{canon(resolution)!r}") from None
        return orders, fills, w.mods.res.restorable_positions(state), resolution

    return fn


# -- books ---------------------------------------------------------------------------------


def book_events(seed: int):
    r = random.Random(7 * 10**6 + seed)
    g = BookGen(r)
    try:
        g.build()
    except Exception:  # noqa: BLE001 - an unfoldable book is not a case
        return None
    return g.events


def test_the_options_oms_matches_the_frozen_oracle_on_books(tmp_path):
    tally = Tally()
    for seed in range(BOOK_SEEDS):
        events = book_events(seed)
        if events is None:
            tally["unfoldable"] += 1
            continue
        pair = Pair(tmp_path / f"b{seed}", NOW, f"book seed={seed}", tally)
        try:
            for e in events:
                pair.p.ledger.append(e)
        except Exception:  # noqa: BLE001 - a book the ledger refuses is not a case
            tally["unappendable"] += 1
            pair.o.close()
            pair.p.close()
            continue
        for e in events:
            pair.o.ledger.append(e)
        r = random.Random(11 * 10**6 + seed)
        cmds = Cmds(r)
        for _ in range(r.randint(4, 14)):
            x = r.random()
            state = pair.o.ledger.state(ACC)
            if x < 0.28:
                pair.do("open", op_open(cmds))
            elif x < 0.5:
                pair.do("close", op_close(cmds, state))
            elif x < 0.6:
                pair.do("close_holding", op_close_holding(cmds))
            elif x < 0.7:
                pair.do("sync", op_sync(cmds))
            elif x < 0.85:
                fn = op_restorable(r)
                pair.do("restorable", fn)
            else:
                pair.do("journal", book_journal(cmds, state, pair.o.clock.now_utc()))
            if x < 0.5 and r.random() < 0.25:
                ev = cmds.pending_event(pair.o.ledger.state(ACC), pair.o.clock.now_utc())
                if ev is not None:
                    pair.inject(ev)
                    tally["pending_injected"] += 1
        pair.finish()
        tally["books"] += 1
    print(f"\nbooks: {tally['books']} books, {tally['steps']} compared steps")
    for key in sorted(tally):
        print(f"  {key}: {tally[key]}")
    check_books(tally)


def book_journal(cmds: Cmds, state, now: datetime):
    r = cmds.r
    journal = r.choice(["JRN-1", "JRN-2"])
    if state.fills and r.random() < 0.5:
        fill = r.choice(state.fills)
    else:
        fill = cmds.phantom_fill(state, now)
    return lambda w: w.mods.rec.enqueue_journal_fill(w.ledger, w.clock, journal, w.ledger.state(ACC), fill)


def check_books(tally: Tally) -> None:
    assert tally["books"] > BOOK_SEEDS // 2
    for key in (
        "open:ok", "open:DuplicateEntryError", "open:UncoveredCallError", "open:IdempotencyConflictError",
        "close:ok", "close:StructureClosedError", "close:IdempotencyConflictError",
        "close_holding:ok", "close_holding:OptionOrderError", "close_holding:UncoveredCallError",
        "sync:ok", "restorable:ok", "restorable:RestoreError", "journal:ReconcileError", "journal:KeyError",
    ):
        assert tally[key] > 0, f"never happens: {key}"


# -- flows ---------------------------------------------------------------------------------


class Flow:
    def __init__(self, tmp: Path, seed: int, tally: Tally) -> None:
        self.r = random.Random(5 * 10**6 + seed)
        self.tally = tally
        self.pair = Pair(tmp, datetime(2026, 9, 24, 21, 45, tzinfo=UTC), f"flow seed={seed}", tally)
        self.cmds = Cmds(self.r)
        self.session = 0
        self.since = MIN
        self.kept: dict = {}
        self.foreign = 0
        deposit = Event(
            account=ACC, kind=EventKind.CASH_FLOW, ts_utc=datetime(2026, 9, 24, 21, 45, tzinfo=UTC), command_id="deposit",
            payload=CashFlow(amount=D(self.r.choice(["20000", "250000", "250000"])), kind="deposit", as_of=datetime(2026, 9, 24, 21, 45, tzinfo=UTC)),
        )
        self.pair.both(lambda w: w.ledger.append(deposit))

    def state(self):
        return self.pair.o.ledger.state(ACC)

    def snapshot(self) -> None:
        r = self.r
        if self.session >= len(SESSIONS):
            return
        at = snap_at(SESSIONS[self.session])
        self.session += 1
        chains = [random_snapshot(r, "XYZ", at, keep=self.kept or None)]
        self.kept = {q.contract: q for q in chains[0].quotes}
        if r.random() < 0.2:
            chains.append(random_snapshot(r, "ABC", at))

        def fn(w):
            w.clock.advance_to(at)
            return [w.venue.process_snapshot(c) for c in chains]

        self.pair.do("snapshot", fn)
        self.reconcile(at)

    def reconcile(self, at: datetime) -> None:
        r = self.r
        journal = r.choice([None, None, "JRN-1"])
        since = self.since if r.random() < 0.6 else MIN
        if r.random() < 0.25:
            since = at
        self.since = at

        def fn(w):
            return w.mods.rec.reconcile_after(w.ledger, w.clock, w.venue, w.oms.orders, ACC, since, journal_account=journal)

        self.pair.do("reconcile", fn)
        if r.random() < 0.7:
            self.pair.do("sync", op_sync(self.cmds))

    def foreign_order(self) -> None:
        """A venue order the ledger never saw: its fill must refuse (I5)."""
        self.foreign += 1
        name = f"ghost{self.foreign}"

        def fn(w):
            now = w.clock.now_utc()
            return w.venue.submit(
                VenueOrder(
                    venue_order_id=name,
                    instrument=XYZ,
                    order_type=OrderType.MARKET,
                    side=Side.BUY,
                    quantity=D(10),
                    submitted_at=now,
                    allocations=(VenueOrderAllocation(name, ACC, D(10)),),
                )
            )

        self.pair.do("foreign", fn)

    def restart(self) -> None:
        """A new process: an empty venue, rebuilt from the ledger the way the runners do."""
        r = self.r
        mode = r.choice(["refuse", "resolve", "resolve"])

        def fn(w):
            w.rebuild_venue()
            state = w.ledger.state(ACC)
            resolution = w.mods.res.PendingResolution() if mode == "resolve" else None
            try:
                orders, fills = w.mods.res.restorable(w.ledger, ACC, state, pending=mode, resolution=resolution)
            except Exception as err:  # noqa: BLE001
                raise RuntimeError(f"{type(err).__name__}|{err}|{canon(resolution)!r}") from None
            positions = w.mods.res.restorable_positions(state)
            if orders or fills or positions:
                w.venue.restore(orders, fills, positions)
            read = [w.oms.orders.reconcile_order(oid) for oid in (resolution.resolved if resolution else [])]
            return orders, fills, positions, resolution, read

        self.pair.do("restart", fn)

    def orphan(self) -> None:
        """An order whose parent the ledger lacks, then every decision that reads it."""
        self.cmds.n += 1
        oid = f"orph{self.cmds.n}"
        now = self.pair.o.clock.now_utc()
        order = Order(
            order_id=oid, account_id=ACC, instrument=XYZ, order_type=OrderType.MARKET, side=Side.BUY, quantity=D(5),
            command_id=oid, created_at=now, parent_order_id="ghost-parent",
        )
        events = [
            Event(account=ACC, kind=EventKind.ORDER_SUBMITTED, payload=order, ts_utc=now, command_id=f"{oid}:submit"),
            Event(account=ACC, kind=EventKind.ORDER_ACCEPTED, payload=OrderStateChange(oid, "ok", None), ts_utc=now, command_id=f"{oid}:accepted"),
        ]

        def add(w):
            for e in events:
                w.ledger.append(e)

        self.pair.both(add)
        fill = Fill(fill_id=f"of{oid}", order_id=oid, account_id=ACC, instrument=XYZ, quantity=D(1), price=D("50"), venue_env="sim", filled_at=now, side=Side.BUY)
        self.pair.do("journal", lambda w: w.mods.rec.enqueue_journal_fill(w.ledger, w.clock, "JRN-1", w.ledger.state(ACC), fill))
        self.pair.do("restorable", op_restorable(self.r))
        self.pair.do("sync", op_sync(self.cmds))

    def run(self) -> None:
        r = self.r
        pair = self.pair
        orphaned = r.random() < 0.12
        for step in range(FLOW_STEPS):
            x = r.random()
            if step < 3 and x < 0.7:
                pair.do("open", op_open(self.cmds))
            elif x < 0.28:
                pair.do("open", op_open(self.cmds))
            elif x < 0.58:
                self.snapshot()
            elif x < 0.64:
                pair.do("sync", op_sync(self.cmds))
            elif x < 0.72:
                pair.do("close", op_close(self.cmds, self.state()))
            elif x < 0.76:
                pair.do("close_holding", op_close_holding(self.cmds))
            elif x < 0.82:
                ev = self.cmds.pending_event(self.state(), pair.o.clock.now_utc())
                if ev is not None:
                    pair.inject(ev)
                    self.tally["pending_injected"] += 1
            elif x < 0.9:
                self.restart()
            elif x < 0.95:
                pair.do("journal", book_journal(self.cmds, self.state(), pair.o.clock.now_utc()))
            elif self.foreign < 1:
                self.foreign_order()
        if orphaned:
            self.orphan()
        pair.finish()


def test_the_options_oms_matches_the_frozen_oracle_in_flows(tmp_path):
    tally = Tally()
    for seed in range(FLOW_SEEDS):
        Flow(tmp_path / f"f{seed}", seed, tally).run()
    print(f"\nflows: {FLOW_SEEDS} walks, {tally['steps']} compared steps")
    for key in sorted(tally):
        print(f"  {key}: {tally[key]}")
    check_flows(tally)


def check_flows(tally: Tally) -> None:
    for key in (
        "open:ok", "open:DuplicateEntryError", "open:UncoveredCallError", "open:IdempotencyConflictError",
        "close:ok", "close:StructureClosedError", "sync:ok", "snapshot:ok", "reconcile:ok",
        "reconcile:ReconcileError", "restart:ok", "restart:RestoreError", "journal:ok",
    ):
        assert tally[key] > 0, f"never happens: {key}"


def test_reconcile_after_rereads_the_same_orders_as_the_oracle(tmp_path):
    """The venue lists orders of every ledger state (and unknown ones): which are re-read, in order."""
    tally = Tally()
    for seed in range(BOOK_SEEDS):
        events = book_events(seed)
        if events is None:
            continue
        r = random.Random(13 * 10**6 + seed)
        seen = []
        for name, mods in (("oracle", ORACLE), ("prod", PROD)):
            ledger = Ledger(tmp_path / f"r{seed}{name}.db").open()
            try:
                for e in events:
                    ledger.append(e)
            except Exception:  # noqa: BLE001 - not a case
                ledger.close()
                seen = None
                break
            new_orders = tuple(
                Order(
                    order_id=f"new{k}", account_id=ACC, instrument=XYZ, order_type=OrderType.MARKET, side=Side.BUY,
                    quantity=D(1), command_id=f"new{k}", created_at=NOW,
                )
                for k in range(2)
            )
            ledger.append(
                Event(
                    account=ACC, kind=EventKind.ORDERS_CREATED, ts_utc=NOW, command_id="new-batch",
                    payload=OrdersCreated(orders=new_orders, fingerprint="fp", reason="NEW children never sent"),
                )
            )
            ids = sorted(ledger.state(ACC).orders)
            rr = random.Random(seed)
            listed = [i for i in ids if rr.random() < 0.8] + ["unknown-order"]
            rr.shuffle(listed)
            read: list[str] = []
            broker = SimpleNamespace(
                fills=lambda since: [],
                orders=lambda since, listed=listed: [SimpleNamespace(venue_order_id=i) for i in listed],
            )
            manager = SimpleNamespace(reconcile_order=read.append)
            n = mods.rec.reconcile_after(ledger, ReplayClock(NOW), broker, manager, ACC, MIN)
            seen.append((n, read, [(e.seq, e.command_id) for e in ledger.events(account=ACC)]))
            ledger.close()
        if seen is None:
            continue
        assert seen[0] == seen[1], f"seed={seed} oracle: {seen[0]!r:.1500} prod: {seen[1]!r:.1500}"
        tally["books"] += 1
        tally["reads"] += len(seen[0][1])
    assert tally["books"] > BOOK_SEEDS // 2 and tally["reads"] > 100


@pytest.mark.parametrize("name", ["oms_restorable", "oms_sync_plan", "oms_plan_close", "oms_journal_payload"])
def test_the_rust_module_is_imported_not_skipped(name):
    assert getattr(trade_engine_rs, name)

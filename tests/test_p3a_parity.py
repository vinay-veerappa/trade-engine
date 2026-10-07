"""P3a gate 1: the fill and option-risk rules in Rust against the frozen pre-port Python.

`tests/frozen_p3a/` holds the Python each shim replaced, verbatim from b3ea2f7 (only the
imports of its frozen siblings differ). Every case below drives the oracle and the
production class with the SAME inputs, step by step, and every step must agree: the
return value with Decimals compared by ``str`` (so scale matters: ``89.910`` is not
``89.91``), datetimes by ``isoformat``, or the refusal by exception type name AND message.

Five seeded generators, each covering the rules of one module:

* ``SimBroker``: random walks over sessions (regular, DST changes, early closes, holiday
  eves) of MARKET / LIMIT / STOP / STOP_LIMIT orders with DAY / GTC / OPG (and the
  unsupported TIFs and TRAIL, which refuse), both sides, brackets and OCO groups, gapped
  bars, bars touching or just missing a working price, partial exits, replaces, cancels,
  clock jumps past the close, malformed bars, and a restore (sometimes perturbed).
* ``SnapshotVenue``: the same over chain snapshots: singles, shares and combos, stale and
  missing quotes, limits set exactly on the model price, look-ahead, DAY expiry, restore.
* ``TrailingStopEmulator``: both sides, seeded state, non-finite and non-positive prices.
* ``open_structures`` / ``uncovered_calls``: books folded from entries, partial fills,
  targets, closes, cancels and expiries.
* ``OptionRiskEngine.evaluate``: random rules (every optional rule on or off, every entry
  gate), intents, snapshots, regimes and earnings sources, on those books.

A missing ``trade_engine_rs`` is an ERROR (D5): it is imported unconditionally.
"""

from __future__ import annotations

import collections
import dataclasses
import random
import re
import sys
from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from enum import Enum
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import pytest
import trade_engine_rs  # noqa: F401 - a missing module is an ERROR, never a skip (D5)

from frozen_p3a import oracle_broker as OB
from frozen_p3a import oracle_risk_options as OR
from frozen_p3a import oracle_snapshot_venue as OS
from frozen_p3a import oracle_structures as OST
from frozen_p3a import oracle_trailing as OT
from trade_engine.ledger.codec import DecimalRangeError, canon_decimal
from trade_engine import risk_options as PR
from trade_engine.calendar.sessions import ExchangeCalendar
from trade_engine.domain.instruments import Combo, ComboLeg, Equity, OptionContract, OptionRight, Side
from trade_engine.domain.option_orders import OptionIntent
from trade_engine.domain.orders import Order, OrderState, OrderType, TimeInForce
from trade_engine.domain.portfolio import Fill
from trade_engine.eod.options import OptionContext
from trade_engine.interfaces.broker import OrderChanges, VenueFill, VenueOrder, VenueOrderAllocation, VenuePosition
from trade_engine.interfaces.market_data import Bar, Greeks, OptionQuote, StaleDataError
from trade_engine.ledger import CashFlow, Event, EventKind, Ledger, Mark, fold
from trade_engine.ledger.events import OptionLifecycle, OrderStateChange
from trade_engine.market_data.chains import ChainSnapshot
from trade_engine.oms import options as PST
from trade_engine.oms import trailing as PT
from trade_engine.sim import broker as PB
from trade_engine.sim import snapshot_venue as PS

D = Decimal
NY = ZoneInfo("America/New_York")
CAL = ExchangeCalendar()
ACC = "P3A"

BROKER_SEEDS = 900
VENUE_SEEDS = 2000
TRAIL_SEEDS = 1500
BOOK_SEEDS = 1000
INTENTS_PER_BOOK = 6


def cents(n: int) -> Decimal:
    return D(n).scaleb(-2)


# -- comparison ---------------------------------------------------------------------------


from p7_compare import FINE, first_diff, by_value, deep_respell, no_fingerprint, respell_text, respelled, wire_refused  # noqa: E402,F401


def canon(v):
    """A comparable rendering: Decimals by str, datetimes by isoformat, enums by value."""
    if isinstance(v, Decimal):
        # by value (P7): the oracle keeps the spelling it was given, production is canonical
        return ("D", by_value(v))
    if isinstance(v, Enum):
        return ("E", type(v).__name__, v.value)
    if isinstance(v, bool):
        return ("B", v)
    if isinstance(v, str):
        return respell_text(v)       # text quoting a decimal (P7: the oracle quotes it as given)
    if v is None or isinstance(v, int):
        return v
    if isinstance(v, float):
        return ("F", repr(v))
    if isinstance(v, datetime):
        return ("T", v.isoformat())
    if isinstance(v, date):
        return ("d", v.isoformat())
    if dataclasses.is_dataclass(v):
        return (type(v).__name__, tuple((f.name, canon(getattr(v, f.name))) for f in dataclasses.fields(v)))
    if isinstance(v, Mapping):
        return ("M", tuple((canon(k), canon(x)) for k, x in v.items()))
    if isinstance(v, (set, frozenset)):
        return ("S", tuple(sorted(repr(canon(x)) for x in v)))
    if isinstance(v, (list, tuple)):
        return tuple(canon(x) for x in v)
    raise TypeError(f"cannot canonicalise {type(v).__name__}")


def run(fn, *args):
    try:
        return ("ok", canon(fn(*args)))
    except Exception as err:  # noqa: BLE001 - the refusal itself is what is compared
        return ("raise", type(err).__name__, respell_text(str(err)))


class Tally(collections.Counter):
    pass


class Twin:
    """The oracle and the production object, driven in lockstep."""

    def __init__(self, oracle, prod, what: str, tally: Tally) -> None:
        self.o, self.p, self.what, self.tally, self.n = oracle, prod, what, tally, 0

    def do(self, label: str, fn):
        self.n += 1
        a, b = run(fn, self.o), run(fn, self.p)
        if wire_refused(b):
            self.tally["wire_refused"] += 1
        else:
            assert a == b, f"{self.what} step {self.n} ({label})\noracle: {a!r:.2000}\nprod:   {b!r:.2000}"
        self.tally["steps"] += 1
        self.tally[f"{label}:{a[0]}"] += 1
        return a


def build(make_oracle, make_prod, what: str, tally: Tally):
    """Construct both; a refusal must be the same refusal. None when both refused."""
    a, b = run(lambda: make_oracle() and None), run(lambda: make_prod() and None)
    assert a == b, f"{what} construction\noracle: {a!r}\nprod:   {b!r}"
    tally["build:" + a[0]] += 1
    if a[0] == "raise":
        return None
    return make_oracle(), make_prod()


class Clk:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def now_utc(self) -> datetime:
        return self.now

    def sleep(self, seconds: float) -> None:
        raise AssertionError("a venue must not sleep")


# sessions: a plain week, DST changes, holiday eves, early closes, a year end
SESSIONS = [
    d
    for d in (
        date(2026, 9, 23), date(2026, 9, 25), date(2026, 3, 6), date(2026, 3, 9), date(2026, 10, 30),
        date(2026, 11, 2), date(2026, 11, 25), date(2026, 11, 27), date(2026, 12, 24), date(2026, 12, 31),
        date(2026, 7, 2), date(2026, 4, 2), date(2027, 1, 15),
    )
    if CAL.is_session(d)
]
EARLY = [d for d in SESSIONS if CAL.session_close(d) - CAL.session_open(d) < timedelta(hours=6)]


def test_the_sessions_cover_early_closes_and_dst():
    assert len(SESSIONS) >= 10
    assert EARLY, "no early close among the sessions"
    offsets = {CAL.session_open(d).astimezone(NY).utcoffset() for d in SESSIONS}
    assert len(offsets) == 2, "both DST regimes must be covered"


def session_on_or_before(ts):
    d = ts.astimezone(NY).date()
    while not CAL.is_session(d):
        d -= timedelta(days=1)
    return d


# -- SimBroker ----------------------------------------------------------------------------

AAPL, MSFT = Equity("AAPL"), Equity("MSFT")
BAR_OPTION = OptionContract("AAPL", date(2026, 12, 18), D("200"), OptionRight.CALL)


def make_bar(inst, ts, o, h, lo, c):
    return Bar(instrument=inst, timestamp=ts, open=o, high=h, low=lo, close=c, volume=D(1000), as_of=ts)


class BrokerWalk:
    def __init__(self, seed: int, tally: Tally) -> None:
        self.r = r = random.Random(seed)
        self.tally = tally
        self.what = f"broker seed={seed}"
        self.day = r.choice(SESSIONS)
        self.clock = Clk(CAL.session_open(self.day) - timedelta(minutes=r.choice([1, 5, 30, 600])))
        self.slip = D(r.choice(["0", "5", "10", "2.5", "0.37", "1", "-1" if r.random() < 0.03 else "3"]))
        self.full = r.random() < 0.3
        self.twin = None
        self.ids: list[str] = []
        self.orders: dict[str, VenueOrder] = {}
        self.entries: list[str] = []
        self.price = {AAPL: cents(r.randint(2000, 30000)), MSFT: cents(r.randint(2000, 30000))}
        self.n = 0

    def start(self) -> bool:
        built = build(
            lambda: OB.SimBroker(ACC, self.clock, self.slip),
            lambda: PB.SimBroker(ACC, self.clock, self.slip),
            self.what,
            self.tally,
        )
        if built is None:
            return False
        self.twin = Twin(*built, self.what, self.tally)
        if self.r.random() < 0.99:
            self.twin.do("connect", lambda b: b.connect())
        return True

    # bars ------------------------------------------------------------------------------

    def last(self, inst):
        bar = self.twin.o._last_bars.get(inst)
        return None if bar is None else bar

    def next_ts(self, inst):
        last = self.last(inst)
        if last is None:
            return CAL.session_open(self.day)
        cur = last.timestamp
        d = session_on_or_before(cur)
        nxt = cur + timedelta(minutes=1)
        close = CAL.session_close(d)
        if nxt < close:
            return nxt
        if nxt < close + timedelta(minutes=3) and self.r.random() < 0.4:
            return nxt  # extended hours: in sequence, never fills
        return CAL.session_open(CAL.next_session(d))

    def touch_prices(self, inst):
        out = []
        for w in self.twin.o._orders.values():
            if w.order.instrument == inst and w.state in (OrderState.ACCEPTED, OrderState.PARTIALLY_FILLED):
                out += [p for p in (w.order.limit_price, w.order.stop_price) if p is not None]
        return out

    def good_bar(self, inst, ts):
        r = self.r
        p = self.last(inst).close if self.last(inst) is not None else self.price[inst]
        gap = D(0)
        if r.random() < 0.15:
            gap = r.choice([-1, 1]) * cents(r.randint(30, 600))
        o = max(p + gap, D("1.00"))
        c = max(o + cents(r.randint(-80, 80)), D("0.50"))
        h = max(o, c) + cents(r.choice([0, 0, 1, 5, 20, 80]))
        lo = max(min(o, c) - cents(r.choice([0, 0, 1, 5, 20, 80])), D("0.01"))
        touch = self.touch_prices(inst)
        if touch and r.random() < 0.45:
            t = r.choice(touch) + r.choice([D(0), D(0), D(0), cents(1), cents(-1)])
            if t > 0:
                if t > h:
                    h = t
                elif t < lo:
                    lo = t
                elif r.random() < 0.5 and t >= max(o, c):
                    h = t
                elif t <= min(o, c):
                    lo = t
            if r.random() < 0.2 and t > 0:
                o = t  # opening exactly on a working price
                h, lo = max(h, o), min(lo, o)
        return make_bar(inst, ts, o, h, lo, c)

    def feed(self, inst, ts=None, bar=None):
        ts = ts or self.next_ts(inst)
        bar = bar or self.good_bar(inst, ts)
        self.clock.now = max(self.clock.now, ts + timedelta(seconds=self.r.choice([0, 0, 0, 1, 30, 59])))
        before = len(self.twin.o._fills)
        self.twin.do("bar", lambda b: b.process_bar(bar))
        # the OMS sends children right after it sees an entry fill: before the next bar
        for f in self.twin.o._fills[before:]:
            if f.venue_order_id in self.entries and self.r.random() < 0.7:
                self.bracket(f.venue_order_id, bar)

    def bracket(self, parent, bar):
        r = self.r
        po = self.orders[parent]
        side = Side.SELL if po.side is Side.BUY else Side.BUY
        qty = self.twin.o._orders[parent].filled_quantity
        if r.random() < 0.3:
            qty = max(qty / 2, D(1)) if r.random() < 0.5 else qty
        sign = -1 if side is Side.SELL else 1  # the stop sits beyond the entry, against it
        levels = [bar.low, bar.high, bar.open, bar.close]
        legs = []
        stop = r.choice(levels) + r.choice([D(0), cents(1), cents(-1), sign * cents(r.randint(1, 100))])
        if stop > 0:
            legs.append((OrderType.STOP, {"stop_price": stop}, f"{parent}:stop"))
        for k in range(r.choice([0, 1, 1, 2, 3])):
            target = r.choice(levels) - sign * cents(r.choice([0, 1, 5, 20, 100]))
            if target > 0:
                legs.append((OrderType.LIMIT, {"limit_price": target}, f"{parent}:target:{k + 1}"))
        if r.random() < 0.1:
            legs.append((OrderType.MARKET, {}, f"{parent}:close"))
        r.shuffle(legs)
        oco = f"{parent}:oco" if r.random() < 0.85 else None
        for ot, kw, strategy in legs:
            self.n += 1
            vid = f"v{self.n}"
            try:
                order = VenueOrder(
                    venue_order_id=vid, instrument=po.instrument, order_type=ot, side=side, quantity=qty,
                    submitted_at=self.clock.now, tif=r.choice([TimeInForce.GTC, TimeInForce.DAY]),
                    allocations=(VenueOrderAllocation(strategy, ACC, qty),), parent_order_id=parent, oco_group=oco, **kw,
                )
            except ValueError:
                self.tally["gen_skip"] += 1
                continue
            res = self.twin.do("bracket", lambda b: b.submit(order))
            if res[0] == "ok":
                self.ids.append(vid)
                self.orders[vid] = order

    def bad_bar(self):
        r = self.r
        inst = r.choice([AAPL, MSFT])
        last = self.last(inst)
        kind = r.choice(["none", "gap", "dup_same", "dup_diff", "seconds", "option", "back", "nonsession", "late_open"])
        if kind == "none":
            self.twin.do("bar", lambda b: b.process_bar(None))
            return
        if last is None:
            ts = CAL.session_open(self.day)
        else:
            ts = last.timestamp
        if kind == "gap":
            ts = self.next_ts(inst) + timedelta(minutes=r.choice([1, 2, 30]))
            bar = self.good_bar(inst, ts)
        elif kind == "dup_same" and last is not None:
            bar = last
        elif kind == "dup_diff" and last is not None:
            bar = make_bar(inst, ts, last.open, last.high + 1, last.low, last.close)
        elif kind == "seconds":
            bar = self.good_bar(inst, self.next_ts(inst) + timedelta(seconds=30))
        elif kind == "option":
            bar = make_bar(BAR_OPTION, self.next_ts(inst), D(5), D(6), D(4), D(5))
        elif kind == "back" and last is not None:
            bar = self.good_bar(inst, ts - timedelta(minutes=r.choice([1, 60])))
        elif kind == "nonsession":
            d = self.day
            while CAL.is_session(d):
                d += timedelta(days=1)
            bar = self.good_bar(inst, datetime(d.year, d.month, d.day, 14, 30, tzinfo=UTC))
        else:
            bar = self.good_bar(inst, CAL.session_open(self.day) + timedelta(minutes=r.choice([1, 7])))
        self.clock.now = max(self.clock.now, bar.timestamp)
        self.twin.do("badbar", lambda b: b.process_bar(bar))

    # orders ------------------------------------------------------------------------------

    def order(self):
        r = self.r
        self.n += 1
        inst = AAPL if r.random() < 0.8 else MSFT
        parent = None
        side = r.choice([Side.BUY, Side.SELL])
        if self.entries and r.random() < 0.4:
            parent = r.choice(self.entries)
            po = self.orders[parent]
            inst = po.instrument if r.random() < 0.95 else (MSFT if po.instrument == AAPL else AAPL)
            side = (Side.SELL if po.side is Side.BUY else Side.BUY) if r.random() < 0.93 else po.side
            if r.random() < 0.03:
                parent = "ghost"
        kinds = [OrderType.MARKET, OrderType.LIMIT, OrderType.STOP, OrderType.STOP_LIMIT]
        ot = r.choice(kinds) if r.random() < 0.98 else OrderType.TRAIL
        opg = r.random() < 0.08
        tif = r.choices(
            [TimeInForce.DAY, TimeInForce.GTC, TimeInForce.OPG, TimeInForce.GTD, TimeInForce.MOC], [6, 4, 0.6, 0.2, 0.2]
        )[0]
        if opg:
            ot, tif = (OrderType.MARKET if r.random() < 0.9 else ot), TimeInForce.OPG
        last = self.last(inst)
        p = last.close if last is not None else self.price[inst]
        kw = {}
        if ot in (OrderType.LIMIT, OrderType.STOP_LIMIT):
            kw["limit_price"] = max(p + cents(r.randint(-200, 200)), D("0.01"))
        if ot in (OrderType.STOP, OrderType.STOP_LIMIT):
            kw["stop_price"] = max(p + cents(r.randint(-200, 200)), D("0.01"))
            if ot is OrderType.STOP_LIMIT and r.random() < 0.5:
                kw["limit_price"] = kw["stop_price"] + cents(r.randint(-50, 50))
        if ot is OrderType.TRAIL:
            kw["trail_amount"] = D(1)
        qty = D(r.choice(["1", "1", "2", "3", "5", "10", "0.5"]))
        vid = f"v{self.n}"
        if self.ids and r.random() < 0.05:
            vid = r.choice(self.ids)
            if r.random() < 0.6:
                same = self.orders[vid]
                self.twin.do("submit", lambda b: b.submit(same))
                return
        strategy = vid
        oco = None
        if parent is not None:
            if r.random() < 0.6:
                oco = f"{parent}:oco"
            if ot is OrderType.LIMIT and r.random() < 0.7:
                strategy = f"{parent}:target:{r.choice(['1', '2', '3', 'x'])}"
        account = ACC if r.random() < 0.97 else "other"
        allocations = (VenueOrderAllocation(strategy, account, qty),)
        if r.random() < 0.01:
            half = qty / 2
            allocations = (VenueOrderAllocation(strategy, ACC, half), VenueOrderAllocation(strategy + "b", ACC, qty - half))
        submitted = self.clock.now - (timedelta(minutes=r.choice([1, 5])) if r.random() < 0.05 else timedelta())
        try:
            order = VenueOrder(
                venue_order_id=vid, instrument=inst, order_type=ot, side=side, quantity=qty, submitted_at=submitted,
                tif=tif, allocations=allocations, parent_order_id=parent, oco_group=oco, **kw,
            )
        except ValueError:
            self.tally["gen_skip"] += 1
            return
        res = self.twin.do("submit", lambda b: b.submit(order))
        if res[0] == "ok" and vid not in self.orders:
            self.ids.append(vid)
            self.orders[vid] = order
            if parent is None:
                self.entries.append(vid)

    def replace(self):
        r = self.r
        vid = r.choice(self.ids + ["ghost"])
        pick = lambda options: r.choice(options)  # noqa: E731
        changes = OrderChanges(
            new_quantity=pick([None, None, D(1), D(2), D(4), D(10), D("0.5"), D(0), D(-1), D("NaN"), D("Infinity")]),
            new_limit_price=pick([None, None, D("0.01"), cents(r.randint(1000, 30000)), D(-1)]),
            new_stop_price=pick([None, None, cents(r.randint(1000, 30000)), D(0)]),
        )
        self.twin.do("replace", lambda b: b.replace(vid, changes))

    def query(self):
        r = self.r
        since = r.choice(
            [datetime.min.replace(tzinfo=UTC), self.clock.now - timedelta(minutes=r.choice([1, 30, 600])),
             self.clock.now.replace(tzinfo=None) if r.random() < 0.1 else self.clock.now]
        )
        what = r.choice(["orders", "fills", "positions", "cash"])
        if what == "orders":
            self.twin.do("orders", lambda b: b.orders(since))
        elif what == "fills":
            self.twin.do("fills", lambda b: b.fills(since))
        elif what == "positions":
            self.twin.do("positions", lambda b: b.positions())
        else:
            self.twin.do("cash", lambda b: b.cash_events(since))

    def jump(self):
        r = self.r
        last = max((b.timestamp for b in self.twin.o._last_bars.values()), default=self.clock.now)
        d = session_on_or_before(last)
        target = r.choice(
            [CAL.session_close(d) + timedelta(minutes=r.choice([0, 1, 105])),
             CAL.session_close(d) - timedelta(minutes=1),
             CAL.session_open(CAL.next_session(d)) + timedelta(minutes=r.choice([-1, 0, 1, 5])),
             self.clock.now + timedelta(days=r.choice([1, 3]))]
        )
        self.clock.now = max(self.clock.now, target)
        if r.random() < 0.6:
            self.query()

    def to_close(self):
        """Simulate an instrument through its last regular bar, then read the book AT the close.

        The instant a DAY order expires is the close itself (and an OPG order's, its open):
        only a read with the clock exactly there tells ``>=`` from ``>``.
        """
        r = self.r
        inst = AAPL if r.random() < 0.75 else MSFT
        last = self.last(inst)
        d = self.day if last is None else session_on_or_before(last.timestamp)
        close = CAL.session_close(d)
        if last is not None and last.timestamp >= close - timedelta(minutes=1):
            return
        if r.random() < 0.5:
            self.order()  # something DAY may be working into the close
        while (last := self.last(inst)) is None or last.timestamp < close - timedelta(minutes=1):
            ts = self.next_ts(inst)
            if ts >= close:
                break
            self.feed(inst, ts)
            if self.last(inst) is last:
                return  # the bar was refused (not connected, a session gap): no close to reach
        if self.clock.now > close:
            return
        self.clock.now = close
        self.twin.do("orders", lambda b: b.orders(datetime.min.replace(tzinfo=UTC)))
        if r.random() < 0.3:
            self.clock.now = close + timedelta(seconds=r.choice([1, 60]))
            self.query()

    # restore ------------------------------------------------------------------------------

    def restore(self):
        r = self.r
        o = self.twin.o
        orders = [(w.order, w.state) for w in o._orders.values()]
        fills = list(o._fills)
        positions = [VenuePosition(i, q, a, t) for i, (q, a, t) in o._positions.items()]
        if r.random() < 0.35:
            what = r.choice(["drop_fill", "dup_order", "bad_id", "state", "dup_pos", "orphan", "mismatch"])
            if what == "drop_fill" and fills:
                fills.pop(r.randrange(len(fills)))
            elif what == "dup_order" and orders:
                orders.append(r.choice(orders))
            elif what == "bad_id" and fills:
                i = r.randrange(len(fills))
                fills[i] = dataclasses.replace(fills[i], venue_fill_id=fills[i].venue_fill_id.replace(":fill:", ":f:"))
            elif what == "state" and orders:
                i = r.randrange(len(orders))
                orders[i] = (orders[i][0], r.choice(list(OrderState)))
            elif what == "dup_pos" and positions:
                positions.append(positions[0])
            elif what == "orphan" and fills:
                fills.append(dataclasses.replace(fills[0], venue_order_id="ghost", venue_fill_id="ghost:fill:1"))
            elif what == "mismatch" and fills:
                i = r.randrange(len(fills))
                fills[i] = dataclasses.replace(fills[i], side=Side.SELL if fills[i].side is Side.BUY else Side.BUY)
        last = max((b.timestamp for b in o._last_bars.values()), default=self.clock.now)
        self.day = CAL.next_session(session_on_or_before(last))
        if r.random() < 0.7:
            self.clock.now = max(self.clock.now, CAL.session_open(self.day) - timedelta(minutes=r.choice([1, 60, 900])))
        if not self.start():
            return False
        self.twin.do("restore", lambda b: b.restore(orders, fills, positions))
        return True

    # the walk ----------------------------------------------------------------------------

    def walk(self) -> None:
        r = self.r
        if not self.start():
            return
        steps = r.randint(400, 900) if self.full else r.randint(15, 120)
        restore_at = r.randint(5, steps) if r.random() < 0.25 else -1
        for step in range(steps):
            if step == restore_at and not self.restore():
                return
            x = r.random()
            if x < (0.62 if self.full else 0.4):
                inst = AAPL if r.random() < 0.75 else MSFT
                for _ in range(r.choice([1, 1, 1, 3, 10]) if not self.full else 1):
                    self.feed(inst)
            elif x < 0.72:
                self.order()
            elif x < 0.76 and self.ids:
                vid = r.choice(self.ids + ["ghost"])
                self.twin.do("cancel", lambda b: b.cancel(vid))
            elif x < 0.80 and self.ids:
                self.replace()
            elif x < 0.88:
                self.query()
            elif x < 0.92:
                self.jump()
            elif x < 0.95:
                self.bad_bar()
            elif x < 0.954:
                self.to_close()
        self.twin.do("orders", lambda b: b.orders(datetime.min.replace(tzinfo=UTC)))
        self.twin.do("fills", lambda b: b.fills(datetime.min.replace(tzinfo=UTC)))
        self.twin.do("positions", lambda b: b.positions())


def test_sim_broker_matches_the_frozen_oracle():
    tally = Tally()
    for seed in range(BROKER_SEEDS):
        BrokerWalk(seed, tally).walk()
    print(f"\nSimBroker: {BROKER_SEEDS} seeds, {tally['steps']} compared steps")
    fills = sum(1 for k in tally if k.startswith("bar:ok"))
    assert tally["steps"] > 50_000 and fills
    # the refusal paths run too, and every operation is reached
    for label in ("submit", "bar", "badbar", "cancel", "replace", "orders", "fills", "positions", "restore"):
        assert tally[f"{label}:ok"] > 0, label
    for label in ("submit", "badbar", "restore", "replace"):
        assert tally[f"{label}:raise"] > 0, label
    assert tally["build:raise"] > 0


# -- SnapshotVenue -------------------------------------------------------------------------

E1, E2 = date(2026, 10, 30), date(2026, 12, 18)
XYZ, ABC, SPX = Equity("XYZ"), Equity("ABC"), Equity("SPX")


def oc(root, expiry, strike, right, mult=100):
    return OptionContract(root, expiry, D(strike), right, mult)


XYZ_OPTS = [oc("XYZ", e, k, rt) for e in (E1, E2) for k in ("40", "45", "50", "55", "60") for rt in (OptionRight.PUT, OptionRight.CALL)]
ABC_OPTS = [oc("ABC", E1, k, OptionRight.PUT) for k in ("20", "25")]
SPX_OPTS = [oc("SPXW", E1, k, OptionRight.CALL) for k in ("5000", "5050")] + [oc("SPX", E2, "4900", OptionRight.PUT)]
MINI = oc("XYZ", E1, "47.5", OptionRight.PUT, 10)
OPTS = {"XYZ": XYZ_OPTS + [MINI], "ABC": ABC_OPTS, "SPX": SPX_OPTS}
UPRICE = {"XYZ": (4000, 6000), "ABC": (1500, 3000), "SPX": (480000, 520000)}


def random_combo(r, pool):
    for _ in range(10):
        n = min(r.choice([2, 2, 2, 3, 4]), len(pool))
        legs = r.sample(pool, n)
        try:
            return Combo(tuple(ComboLeg(c, r.choice([1, 1, 1, 2]), r.choice([Side.BUY, Side.SELL])) for c in legs))
        except ValueError:
            continue
    return None


def random_quote(r, contract, as_of, age):
    bid = cents(r.randint(0, 1500)) if r.random() < 0.95 else D(0)
    ask = bid + cents(r.choice([0, 1, 5, 10, 25, 50, 110, 200]))
    greeks = None if r.random() < 0.2 else Greeks(r.uniform(-0.9, 0.9), 0.01, -0.02, 0.1, 0.01, "vendor")
    iv = None if r.random() < 0.15 else D(r.randint(5, 150)).scaleb(-2)
    oi = None if r.random() < 0.15 else r.randint(0, 5000)
    return OptionQuote(contract, bid, ask, D(10), D(10), as_of - timedelta(seconds=age), None, iv, greeks, oi)


def random_snapshot(r, underlying, as_of, keep=None):
    lo, hi = UPRICE[underlying]
    quotes = []
    for c in OPTS[underlying]:
        if r.random() < 0.85:
            if keep is not None and c in keep and r.random() < 0.7:
                q = keep[c]
                quotes.append(dataclasses.replace(q, as_of=as_of))
            else:
                age = r.choice([0, 0, 0, 0, 300, 899, 900, 901, 3600])
                quotes.append(random_quote(r, c, as_of, age))
    return ChainSnapshot(underlying, as_of, cents(r.randint(lo, hi)), tuple(quotes), None, None, "test")


class VenueWalk:
    def __init__(self, seed: int, tally: Tally) -> None:
        self.r = r = random.Random(10**6 + seed)
        self.tally = tally
        self.what = f"venue seed={seed}"
        self.day = r.choice(SESSIONS)
        prev = self.day - timedelta(days=1)
        while not CAL.is_session(prev):
            prev -= timedelta(days=1)
        self.clock = Clk(CAL.session_close(prev) + timedelta(minutes=r.choice([0, 105, 180])))
        self.kw = dict(
            fill_fraction=D(r.choice(["0.25", "0.25", "0", "0.5", "0.1", "0.6" if r.random() < 0.05 else "0.2"])),
            fee_per_contract=D(r.choice(["0.65", "0.65", "0", "1.00", "0.5", "-1" if r.random() < 0.03 else "0.7"])),
            equity_slippage_bps=D(r.choice(["5", "0", "7.5", "12"])),
            max_quote_age_seconds=r.choice([900.0, 900.0, 60, 1800.5, 900, 0 if r.random() < 0.05 else 600.0]),
        )
        self.twin = None
        self.ids: list[str] = []
        self.orders: dict[str, VenueOrder] = {}
        self.last_snap: dict[str, ChainSnapshot] = {}
        self.n = 0

    def start(self) -> bool:
        built = build(
            lambda: OS.SnapshotVenue(ACC, self.clock, **self.kw),
            lambda: PS.SnapshotVenue(ACC, self.clock, **self.kw),
            self.what,
            self.tally,
        )
        if built is None:
            return False
        self.twin = Twin(*built, self.what, self.tally)
        if self.r.random() < 0.99:
            self.twin.do("connect", lambda b: b.connect())
        return True

    def instrument(self):
        r = self.r
        u = r.choices(["XYZ", "ABC", "SPX"], [8, 1, 1])[0]
        x = r.random()
        if x < 0.2:
            return {"XYZ": XYZ, "ABC": ABC, "SPX": SPX}[u]
        if x < 0.55:
            return r.choice(OPTS[u])
        if x < 0.58:
            return random_combo(r, XYZ_OPTS[:4] + ABC_OPTS)  # spans underlyings
        return random_combo(r, OPTS[u] if u != "XYZ" else XYZ_OPTS)

    def model_limit(self, inst, side):
        """The oracle's model net for the last snapshot: a limit exactly on (or a tick off) it."""
        o = self.twin.o
        try:
            u = OS.underlying_of(inst)
        except ValueError:
            return None
        snap = self.last_snap.get(u)
        if snap is None:
            return None
        legs = inst.legs if isinstance(inst, Combo) else (ComboLeg(inst, 1, side),)
        prices = []
        for leg in legs:
            p = o.model_price(leg.contract, leg.side, snap)
            if p is None:
                return None
            prices.append(p)
        if not isinstance(inst, Combo):
            return prices[0]
        mult = legs[0].contract.multiplier
        total = sum(((1 if leg.side is side else -1) * leg.ratio * p * leg.contract.multiplier for leg, p in zip(legs, prices)), D(0))
        return total / mult

    def order(self):
        r = self.r
        inst = self.instrument()
        if inst is None:
            return
        self.n += 1
        side = r.choice([Side.BUY, Side.SELL])
        ot = r.choices([OrderType.MARKET, OrderType.LIMIT, OrderType.STOP], [4, 6, 0.2])[0]
        tif = r.choices([TimeInForce.DAY, TimeInForce.GTC, TimeInForce.OPG], [6, 4, 0.2])[0]
        kw = {}
        if ot is OrderType.LIMIT:
            model = self.model_limit(inst, side) if r.random() < 0.6 else None
            if model is not None:
                limit = model + r.choice([D(0), D(0), cents(1), cents(-1), D("0.005")])
            elif isinstance(inst, Equity):
                limit = cents(r.randint(*UPRICE[OS.underlying_of(inst)]))
            else:
                limit = cents(r.randint(-300, 1500))
            if limit <= 0:
                limit = D("0.05")
            kw["limit_price"] = limit
        if ot is OrderType.STOP:
            kw["stop_price"] = D(10)
        qty = D(r.choice([1, 1, 2, 3, 5, 100]))
        vid = f"s{self.n}"
        if self.ids and r.random() < 0.04:
            vid = r.choice(self.ids)
            if r.random() < 0.6:
                same = self.orders[vid]
                self.twin.do("submit", lambda b: b.submit(same))
                return
        account = ACC if r.random() < 0.97 else "other"
        try:
            order = VenueOrder(
                venue_order_id=vid, instrument=inst, order_type=ot, side=side, quantity=qty,
                submitted_at=self.clock.now, tif=tif, allocations=(VenueOrderAllocation(vid, account, qty),), **kw,
            )
        except ValueError:
            self.tally["gen_skip"] += 1
            return
        res = self.twin.do("submit", lambda b: b.submit(order))
        if res[0] == "ok" and vid not in self.orders:
            self.ids.append(vid)
            self.orders[vid] = order

    def snap(self):
        r = self.r
        u = r.choices(["XYZ", "ABC", "SPX"], [8, 1, 1])[0]
        open_, close = CAL.session_open(self.day), CAL.session_close(self.day)
        t = r.choice([close - timedelta(minutes=15), open_ + timedelta(minutes=r.randint(1, 300)), close - timedelta(minutes=1)])
        t = max(t, self.clock.now - timedelta(minutes=r.choice([0, 0, 5])))
        self.clock.now = max(self.clock.now, t)
        as_of = t if r.random() < 0.96 else self.clock.now + timedelta(minutes=1)  # look-ahead refuses
        keep = {q.contract: q for q in self.last_snap[u].quotes} if u in self.last_snap else None
        snapshot = random_snapshot(r, u, as_of, keep)
        res = self.twin.do("snapshot", lambda v: v.process_snapshot(snapshot))
        if res[0] == "ok":
            self.last_snap[u] = snapshot

    def next_day(self):
        r = self.r
        close = CAL.session_close(self.day)
        self.day = CAL.next_session(self.day)
        self.clock.now = max(self.clock.now, close + timedelta(minutes=r.choice([-1, 0, 0, 105, 600])))

    def query(self):
        r = self.r
        since = r.choice([datetime.min.replace(tzinfo=UTC), self.clock.now - timedelta(minutes=r.choice([1, 600]))])
        what = r.choice(["orders", "fills", "positions", "cash"])
        if what == "orders":
            self.twin.do("orders", lambda b: b.orders(since))
        elif what == "fills":
            self.twin.do("fills", lambda b: b.fills(since))
        elif what == "positions":
            self.twin.do("positions", lambda b: b.positions())
        else:
            self.twin.do("cash", lambda b: b.cash_events(since))

    def restore(self):
        r = self.r
        o = self.twin.o
        orders = [(w.order, w.state) for w in o._orders.values()]
        fills = list(o._fills)
        positions = [VenuePosition(i, q, D(0), self.clock.now) for i, q in o._positions.items()]
        if r.random() < 0.4:
            what = r.choice(["drop_fill", "dup_order", "bad_id", "state", "leg", "orphan", "mismatch", "dup_pos"])
            if what == "drop_fill" and fills:
                fills.pop(r.randrange(len(fills)))
            elif what == "dup_order" and orders:
                orders.append(r.choice(orders))
            elif what == "bad_id" and fills:
                i = r.randrange(len(fills))
                fills[i] = dataclasses.replace(fills[i], venue_fill_id=fills[i].venue_fill_id + "x")
            elif what == "state" and orders:
                i = r.randrange(len(orders))
                orders[i] = (orders[i][0], r.choice(list(OrderState)))
            elif what == "leg" and fills:
                i = r.randrange(len(fills))
                fills[i] = dataclasses.replace(fills[i], leg_id=r.choice([None, "0", "1", "9", "x", "-1"]))
            elif what == "orphan" and fills:
                fills.append(dataclasses.replace(fills[0], venue_order_id="ghost", venue_fill_id="ghost:fill:1"))
            elif what == "mismatch" and fills:
                i = r.randrange(len(fills))
                fills[i] = dataclasses.replace(fills[i], side=Side.SELL if fills[i].side is Side.BUY else Side.BUY)
            elif what == "dup_pos" and positions:
                positions.append(positions[0])
        if not self.start():
            return False
        self.last_snap = {}
        self.twin.do("restore", lambda b: b.restore(orders, fills, positions))
        return True

    def walk(self) -> None:
        r = self.r
        if not self.start():
            return
        steps = r.randint(10, 70)
        restore_at = r.randint(3, steps) if r.random() < 0.3 else -1
        for step in range(steps):
            if step == restore_at and not self.restore():
                return
            x = r.random()
            if x < 0.4:
                self.order()
            elif x < 0.62:
                self.snap()
            elif x < 0.67 and self.ids:
                vid = r.choice(self.ids + ["ghost"])
                self.twin.do("cancel", lambda b: b.cancel(vid))
            elif x < 0.69 and self.ids:
                vid = r.choice(self.ids + ["ghost"])
                self.twin.do("replace", lambda b: b.replace(vid, OrderChanges(new_quantity=D(1))))
            elif x < 0.80:
                self.query()
            elif x < 0.88:
                self.next_day()
            elif x < 0.9:
                inst = self.instrument()
                u = OS.underlying_of(XYZ)
                if inst is not None and u in self.last_snap:
                    snap, side = self.last_snap[u], r.choice([Side.BUY, Side.SELL])
                    self.twin.do("model_price", lambda v: v.model_price(inst, side, snap))
        self.twin.do("orders", lambda b: b.orders(datetime.min.replace(tzinfo=UTC)))
        self.twin.do("fills", lambda b: b.fills(datetime.min.replace(tzinfo=UTC)))
        self.twin.do("positions", lambda b: b.positions())


def test_snapshot_venue_matches_the_frozen_oracle():
    tally = Tally()
    for seed in range(VENUE_SEEDS):
        VenueWalk(seed, tally).walk()
    print(f"\nSnapshotVenue: {VENUE_SEEDS} seeds, {tally['steps']} compared steps")
    assert tally["steps"] > 20_000
    for label in ("submit", "snapshot", "cancel", "replace", "orders", "fills", "positions", "restore", "model_price"):
        assert tally[f"{label}:ok"] > 0, label
    for label in ("submit", "snapshot", "restore"):
        assert tally[f"{label}:raise"] > 0, label
    assert tally["build:raise"] > 0


def test_underlying_of_matches_the_frozen_oracle():
    tally = Tally()
    pool = [XYZ, ABC, SPX, *XYZ_OPTS, *ABC_OPTS, *SPX_OPTS, MINI, "XYZ", None]
    r = random.Random(7)
    for _ in range(300):
        pool.append(random_combo(r, XYZ_OPTS + ABC_OPTS + SPX_OPTS))
    for inst in pool:
        a, b = run(OS.underlying_of, inst), run(PS.underlying_of, inst)
        assert a == b, (inst, a, b)
        tally[a[0]] += 1
    assert tally["ok"] and tally["raise"]


# -- TrailingStopEmulator ---------------------------------------------------------------------


def test_trailing_stop_matches_the_frozen_oracle():
    tally = Tally()
    for seed in range(TRAIL_SEEDS):
        r = random.Random(2 * 10**6 + seed)
        side = r.choice([Side.BUY, Side.SELL])
        trail = r.choice([cents(r.randint(1, 800)), cents(r.randint(1, 800)), D(0), D(-1), D("0.001")])
        kw = {}
        if r.random() < 0.3:
            kw["extreme"] = cents(r.randint(5000, 15000))
        if r.random() < 0.2:
            kw["stop_price"] = cents(r.randint(5000, 15000))
        if r.random() < 0.05:
            kw["triggered"] = True
        built = build(lambda: OT.TrailingStopEmulator(side, trail, **kw), lambda: PT.TrailingStopEmulator(side, trail, **kw),
                      f"trail seed={seed}", tally)
        if built is None:
            continue
        twin = Twin(*built, f"trail seed={seed}", tally)
        p = cents(r.randint(100, 15000))
        for _ in range(r.randint(1, 40)):
            p = max(p + cents(r.randint(-300, 300)), cents(1))
            price = p if r.random() < 0.95 else r.choice([D(0), D(-1), D("NaN"), D("Infinity"), D("-Infinity")])
            twin.do("update", lambda t: (t.update(price), t.extreme, t.stop_price, t.triggered))
    print(f"\ntrailing: {TRAIL_SEEDS} seeds, {tally['steps']} compared steps")
    assert tally["update:ok"] > 10_000 and tally["update:raise"] > 0 and tally["build:raise"] > 0


# -- books: open_structures, uncovered_calls, and the option risk gate ------------------------

NOW = datetime(2026, 9, 25, 21, 45, tzinfo=UTC)
SNAP_AT = datetime(2026, 9, 25, 19, 45, tzinfo=UTC)
SESSION = date(2026, 9, 25)
LEAPS = date(2027, 6, 17)
P = {k: oc("XYZ", E1, k, OptionRight.PUT) for k in ("35", "40", "45", "50", "55")}
C = {k: oc("XYZ", E1, k, OptionRight.CALL) for k in ("50", "55", "60")}
C_LEAPS = oc("XYZ", LEAPS, "40", OptionRight.CALL)
C_E2 = oc("XYZ", E2, "55", OptionRight.CALL)
P_ABC = oc("ABC", E1, "25", OptionRight.PUT)
BOOK_OPTS = [*P.values(), *C.values(), C_LEAPS, C_E2, P_ABC]


def leg(c, side, ratio=1):
    return ComboLeg(c, ratio, side)


STRUCTURES = [
    Combo((leg(P["45"], Side.SELL), leg(P["40"], Side.BUY))),  # bull put vertical
    Combo((leg(P["50"], Side.SELL), leg(P["40"], Side.BUY))),
    Combo((leg(P["40"], Side.SELL), leg(P["45"], Side.BUY))),  # long strike above the short: not a bull put
    Combo((leg(C["55"], Side.SELL), leg(C["60"], Side.BUY))),  # bear call
    Combo((leg(C_LEAPS, Side.BUY), leg(C["55"], Side.SELL))),  # PMCC
    Combo((leg(C_LEAPS, Side.BUY), leg(C_E2, Side.SELL))),
    Combo((leg(P["35"], Side.BUY), leg(P["40"], Side.SELL), leg(C["55"], Side.SELL), leg(C["60"], Side.BUY))),  # condor
    Combo((leg(P["45"], Side.SELL, 2), leg(P["40"], Side.BUY))),  # ratio
    Combo((leg(P["45"], Side.SELL), leg(C["55"], Side.SELL))),  # strangle
    Combo((leg(P["45"], Side.SELL), leg(oc("XYZ", E2, "40", OptionRight.PUT), Side.BUY))),  # diagonal
]


class BookGen:
    def __init__(self, r: random.Random) -> None:
        self.r = r
        self.events: list[Event] = []
        self.n = 0
        self.held_opts: list[tuple[OptionContract, Side]] = []

    def ev(self, kind, payload, cid=None):
        self.n += 1
        self.events.append(Event(account=ACC, kind=kind, payload=payload, ts_utc=NOW, command_id=cid or f"c{self.n}"))

    def mark(self, inst, price):
        self.ev(EventKind.MARK, Mark(instrument=inst, price=price, as_of=NOW))

    def order(self, oid, inst, side, qty, limit=None, parent=None):
        o = Order(order_id=oid, account_id=ACC, instrument=inst, order_type=OrderType.MARKET if limit is None else OrderType.LIMIT,
                  side=side, quantity=qty, command_id=oid, created_at=NOW, limit_price=limit, parent_order_id=parent)
        self.ev(EventKind.ORDER_SUBMITTED, o, cid=f"submit:{oid}")
        return o

    def fill(self, oid, inst, side, units, legs=None):
        r = self.r
        if legs is None:
            self.n += 1
            price = cents(r.randint(5, 900)) if not isinstance(inst, Equity) else cents(r.randint(3000, 7000))
            self.ev(EventKind.FILL, Fill(fill_id=f"f{self.n}", order_id=oid, account_id=ACC, instrument=inst, quantity=units,
                                         price=price, venue_env="sim", filled_at=NOW, side=side))
            return
        for i, lg in enumerate(legs):
            self.n += 1
            self.ev(EventKind.FILL, Fill(fill_id=f"f{self.n}", order_id=oid, account_id=ACC, instrument=lg.contract,
                                         quantity=units * lg.ratio, price=cents(r.randint(5, 900)), venue_env="sim",
                                         filled_at=NOW, side=lg.side, leg_id=str(i)))

    def trade(self, t):
        r = self.r
        x = r.random()
        eid = f"t{t}:entry"
        if x < 0.2:
            inst, side = r.choice([XYZ, XYZ, Equity("ABC")]), r.choice([Side.BUY, Side.BUY, Side.SELL])
            qty = D(r.choice([50, 100, 200, 300]))
            self.order(eid, inst, side, qty)
            if r.random() < 0.85:
                self.fill(eid, inst, side, qty if r.random() < 0.8 else qty / 2)
            return
        if x < 0.45:
            inst, side = r.choice(BOOK_OPTS), r.choice([Side.BUY, Side.SELL])
            qty = D(r.randint(1, 5))
            self.order(eid, inst, side, qty, limit=cents(r.randint(10, 500)) if r.random() < 0.5 else None)
            if r.random() < 0.85:
                self.fill(eid, inst, side, qty)
                self.held_opts.append((inst, side))
            return
        inst, side = r.choice(STRUCTURES), r.choice([Side.SELL, Side.SELL, Side.BUY])
        qty = D(r.randint(1, 4))
        self.order(eid, inst, side, qty, limit=cents(r.randint(10, 500)))
        legs = inst.legs if side is Side.BUY else tuple(ComboLeg(lg.contract, lg.ratio, Side.SELL if lg.side is Side.BUY else Side.BUY) for lg in inst.legs)
        legs = tuple(ComboLeg(lg.contract, lg.ratio, lg.side) for lg in inst.legs) if side is Side.BUY else legs
        # an order's legs as traded: the combo as written for BUY, reversed for SELL? use the OMS reading
        from trade_engine.domain.option_orders import legs_of

        traded = legs_of(inst, side)
        filled = D(0)
        y = r.random()
        if y < 0.15:
            pass  # still working
        elif y < 0.3:
            filled = D(r.randint(1, int(qty)))
            self.fill(eid, inst, side, filled, legs=traded)
            if r.random() < 0.5:
                self.ev(EventKind.ORDER_CANCELLED, OrderStateChange(eid, reason="x"))
        else:
            filled = qty
            self.fill(eid, inst, side, qty, legs=traded)
        if filled > 0 and r.random() < 0.6:
            close_side = Side.BUY if side is Side.SELL else Side.SELL
            back = legs_of(inst, close_side)
            if r.random() < 0.6:
                tid = f"{eid}:target"
                self.order(tid, inst, close_side, filled, limit=cents(r.randint(5, 300)), parent=eid)
                z = r.random()
                if z < 0.3:
                    self.ev(EventKind.ORDER_CANCELLED, OrderStateChange(tid, reason="x"))
                elif z < 0.5:
                    self.fill(tid, inst, close_side, D(r.randint(1, int(filled))), legs=back)
            if r.random() < 0.4:
                cid = f"{eid}:close:1"
                self.order(cid, inst, close_side, filled, parent=eid)
                if r.random() < 0.3:
                    self.fill(cid, inst, close_side, D(r.randint(1, int(filled))), legs=back)
        for lg in traded:
            self.held_opts.append((lg.contract, lg.side))

    def build(self):
        r = self.r
        self.ev(EventKind.CASH_FLOW, CashFlow(amount=D(r.choice(["2000", "20000", "50000", "50000", "250000"])), kind="deposit", as_of=NOW))
        if r.random() < 0.92:
            self.mark(XYZ, cents(r.randint(3500, 6500)))
        if r.random() < 0.6:
            self.mark(Equity("ABC"), cents(r.randint(1500, 3000)))
        for t in range(r.randint(0, 6)):
            self.trade(t)
        for c in BOOK_OPTS:
            if r.random() < 0.85:
                self.mark(c, cents(r.randint(5, 900)))
        if self.held_opts and r.random() < 0.15:
            contract, side = r.choice(self.held_opts)
            self.ev(EventKind.EXPIRY, OptionLifecycle(account_id=ACC, contract=contract, quantity=D(1), held=side,
                                                     underlying_price=D("47.5"), price_source="eod", as_of=NOW, reason="expiry"))
        return fold(self.events)[ACC]


def random_book(seed: int):
    r = random.Random(3 * 10**6 + seed)
    try:
        return BookGen(r).build()
    except Exception:  # noqa: BLE001 - an unfoldable book is not a case; the fold has its own parity
        return None


def opt(r, values, p_none=0.4):
    return None if r.random() < p_none else D(r.choice(values))


def random_rules(r, mod):
    gates = None
    if r.random() < 0.6:
        pair = lambda lo, hi: None if r.random() < 0.4 else tuple(sorted((D(r.choice(lo)), D(r.choice(hi)))))  # noqa: E731
        gates = mod.EntryQuoteRules(
            short_put_abs_delta=pair(["0.1", "0.2", "0.3"], ["0.3", "0.4", "0.9"]),
            min_short_bid=opt(r, ["0", "0.5", "1", "2", "5"]),
            short_bid_return=pair(["0", "0.005", "0.02"], ["0.02", "0.05", "0.5"]),
            min_short_implied_vol=opt(r, ["0.2", "0.5", "0.8"]),
            min_open_interest=None if r.random() < 0.4 else r.choice([0, 100, 1000, 3000]),
            max_leg_spread_frac=opt(r, ["0.05", "0.1", "0.3", "1"]),
            min_underlying_price=opt(r, ["10", "40", "50", "60"]),
            min_credit_width_frac=opt(r, ["0.1", "0.2", "0.33"]),
            min_credit_return=opt(r, ["0.1", "0.25", "0.5"]),
            max_friction_frac=opt(r, ["0.1", "0.3", "1"]),
        )
    regimes = sorted(OR.REGIMES)
    put_notional = None
    if r.random() < 0.6:
        put_notional = {g: D(r.choice(["0", "0.5", "1", "2"])) for g in r.sample(regimes, r.randint(1, 3))}
    return mod.OptionRiskRules(
        max_margin_frac=D(r.choice(["0.5", "0.5", "0.1", "0.9", "0", "2"])),
        allowed_regimes=frozenset(r.sample(regimes, r.randint(1, 3))),
        no_earnings_before_expiry=r.random() < 0.5,
        max_name_margin_frac=opt(r, ["0.1", "0.25", "0.5", "1"]),
        max_name_collateral_frac=opt(r, ["0.1", "0.25", "0.5", "1"]),
        put_notional_frac_by_regime=put_notional,
        max_loss_per_structure_frac=opt(r, ["0.02", "0.05", "0.2", "1"]),
        max_debit_per_structure_frac=opt(r, ["0.02", "0.05", "0.3"]),
        max_total_debit_frac=opt(r, ["0.1", "0.3", "1"]),
        max_share_notional_frac=opt(r, ["0.1", "0.2", "1"]),
        entry_quote=gates,
    )


def random_intent(r, n):
    x = r.random()
    if x < 0.15:
        inst = r.choice([XYZ, XYZ, Equity("ABC")])
        side, qty = r.choice([Side.BUY, Side.BUY, Side.SELL]), D(r.choice([1, 50, 100, 300, 1000]))
    elif x < 0.45:
        inst = r.choice(BOOK_OPTS + [MINI])
        side, qty = r.choice([Side.SELL, Side.SELL, Side.BUY]), D(r.randint(1, 20))
    else:
        inst = r.choice(STRUCTURES)
        side, qty = r.choice([Side.SELL, Side.SELL, Side.BUY]), D(r.randint(1, 20))
    limit = None if r.random() < 0.35 else cents(r.randint(1, 1500 if isinstance(inst, Equity) else 600)) * (
        100 if isinstance(inst, Equity) and r.random() < 0.5 else 1
    )
    return OptionIntent(
        intent_id=f"i{n}", account_id=ACC, instrument=inst, side=side, quantity=qty, reason="parity",
        command_id=f"i{n}", order_type=OrderType.MARKET if limit is None else OrderType.LIMIT, limit_price=limit,
    )


def book_snapshot(r, underlying="XYZ"):
    quotes = []
    pool = [c for c in BOOK_OPTS + [MINI, oc("XYZ", E2, "40", OptionRight.PUT)] if OS.underlying_of(c) == underlying]
    for c in pool:
        if r.random() < 0.9:
            quotes.append(random_quote(r, c, SNAP_AT, 0))
    lo, hi = (3500, 6500) if underlying == "XYZ" else (1500, 3000)
    return ChainSnapshot(underlying, SNAP_AT, cents(r.randint(lo, hi)), tuple(quotes), None, None, "test")


class Earn:
    def __init__(self, found, unknown):
        self.found, self.unknown = found, unknown

    def next_earnings(self, symbol, session):
        if self.unknown:
            raise StaleDataError(f"no earnings date for {symbol}")
        return self.found


def test_open_structures_and_uncovered_calls_match_the_frozen_oracle():
    tally = Tally()
    for seed in range(BOOK_SEEDS):
        state = random_book(seed)
        if state is None:
            tally["unfoldable"] += 1
            continue
        for name, fo, fp in (
            ("open_structures", lambda s: OST.open_structures(s), lambda s: PST.open_structures(s)),
            ("uncovered_calls", lambda s: OST.uncovered_calls(s, closing_counts=True), lambda s: PST.uncovered_calls(s, closing_counts=True)),
            ("uncovered_open", lambda s: OST.uncovered_calls(s, closing_counts=False), lambda s: PST.uncovered_calls(s, closing_counts=False)),
        ):
            a, b = run(fo, state), run(fp, state)
            assert a == b, f"{name} seed={seed}\noracle: {a!r:.2000}\nprod:   {b!r:.2000}"
            tally[f"{name}:{a[0]}"] += 1
            if name == "open_structures" and a[0] == "ok" and a[1]:
                tally["books_with_structures"] += 1
    print(f"\nstructures: {BOOK_SEEDS} books, {sum(v for k, v in tally.items() if ':' in k)} compared calls")
    assert tally["books_with_structures"] > BOOK_SEEDS // 4
    assert tally["unfoldable"] < BOOK_SEEDS // 4


def test_option_risk_engine_matches_the_frozen_oracle(tmp_path):
    tally = Tally()
    ledger = Ledger(tmp_path / "ledger.db").open()
    try:
        n = 0
        for seed in range(BOOK_SEEDS):
            state = random_book(seed)
            if state is None:
                continue
            r = random.Random(4 * 10**6 + seed)
            structures = PST.open_structures(state)
            for _ in range(INTENTS_PER_BOOK):
                n += 1
                rr = random.Random(r.random())
                try:
                    intent = random_intent(rr, n)
                except ValueError:
                    tally["gen_skip"] += 1
                    continue
                rules_seed = rr.random()
                rules = [run(lambda m=m: random_rules(random.Random(rules_seed), m)) for m in (OR, PR)]
                assert rules[0] == rules[1], rules
                if rules[0][0] == "raise":
                    tally["rules:raise"] += 1
                    continue
                o_rules, p_rules = random_rules(random.Random(rules_seed), OR), random_rules(random.Random(rules_seed), PR)
                regime = rr.choice([*sorted(OR.REGIMES), None, "UNKNOWN"])
                earnings = None
                if rr.random() < 0.8:
                    earnings = Earn(rr.choice([None, date(2026, 10, 1), E1, date(2026, 11, 5), E2]), rr.random() < 0.15)
                y = rr.random()
                snap = book_snapshot(rr) if y < 0.85 else book_snapshot(rr, "ABC")
                if y < 0.45:
                    context = OptionContext(SESSION, ACC, "snapshot", SNAP_AT, state, structures, snapshot=snap,
                                            snapshots={snap.underlying: snap})
                elif y < 0.75:
                    context = OptionContext(SESSION, ACC, "close", NOW, state, structures, snapshots={snap.underlying: snap})
                elif y < 0.9:
                    context = OptionContext(SESSION, ACC, "close", NOW, state, structures)
                else:
                    context = SimpleNamespace(state=state, session=SESSION)  # a context with no snapshot attributes
                engines = build(
                    lambda: OR.OptionRiskEngine(o_rules, Clk(NOW), ledger, venue_id="sim", regime_of=lambda s: regime, earnings=earnings),
                    lambda: PR.OptionRiskEngine(p_rules, Clk(NOW), ledger, venue_id="sim", regime_of=lambda s: regime, earnings=earnings),
                    f"risk seed={seed} n={n}", tally,
                )
                if engines is None:
                    continue
                a, b = run(lambda: engines[0].evaluate(intent, context)), run(lambda: engines[1].evaluate(intent, context))
                assert a == b, f"risk seed={seed} n={n} diff={first_diff(a, b)!r:.1500}"
                tally[f"evaluate:{a[0]}"] += 1
                if a[0] == "ok":
                    for ev in dict(a[1][1])["evaluations"]:
                        fields = dict(ev[1])
                        tally[f"rule:{fields['rule_name']}:{fields['passed'][1]}"] += 1
    finally:
        ledger.close()
    print(f"\noption risk: {tally['evaluate:ok'] + tally['evaluate:raise']} compared verdicts")
    assert tally["evaluate:ok"] > 2500
    # every rule both passes and fails somewhere
    for rule in ("regime", "margin", "name_margin", "name_collateral", "put_notional", "max_loss", "debit",
                 "total_debit", "share_notional", "earnings", "duplicate_entry", "covered_calls",
                 "entry_quote", "entry_quote.delta", "entry_quote.bid", "entry_quote.bid_return",
                 "entry_quote.implied_vol", "entry_quote.open_interest", "entry_quote.leg_spread",
                 "entry_quote.underlying_price", "entry_quote.credit_width", "entry_quote.credit_return",
                 "entry_quote.friction", "entry_quote.vertical"):
        assert tally[f"rule:{rule}:True"] + tally[f"rule:{rule}:False"] > 0, rule
        if rule != "entry_quote.vertical":
            assert tally[f"rule:{rule}:False"] > 0, f"{rule} never fails"
            assert tally[f"rule:{rule}:True"] > 0, f"{rule} never passes"


@pytest.mark.parametrize("rule", ["regime"])
def test_the_rust_module_is_imported_not_skipped(rule):
    assert trade_engine_rs.option_risk_evaluate and trade_engine_rs.oms_open_structures

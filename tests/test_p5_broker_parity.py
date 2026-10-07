"""P5-T9/T10 lockstep: the frozen ``TosPaperBroker`` against production ``TosPaperBroker`` (a shim over the Rust core).

Both are driven by the same op sequence over the same fake transport. The Rust side runs against the
real fake venue and every transport call is recorded (name, arguments, answer or exception); the
frozen oracle then runs the same op against a replay of that tape, which refuses (``ReplayMismatch``,
a ``BaseException`` the oracle's own ``except Exception`` cannot swallow) any call that is not the
next one recorded. So the two make the same calls, in the same order, with the same arguments, or the
step fails. The clock is a stepping clock each side owns; its ticks are logged in order with the
transport calls, so a clock read in the wrong place is a mismatch too.

After every op the gate compares: the return (dataclasses field by field, Decimals by ``str``,
datetimes by ``isoformat``) or the refusal (exception type NAME and message, and a ``VenueUnreadable``'s
event); the call log; ``halted``, ``queued`` and the whole decision state (keys, expected book as ordered
pairs with their spelling, sent tickets, cancelled set, proven ids, the preflight read).
"""
from __future__ import annotations

import copy
import dataclasses
import inspect
import json
import random
import sys
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
from pathlib import Path
from types import MappingProxyType

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from trade_engine.tos_paper import _rs as H  # noqa: E402
from frozen_p5 import broker as FB  # noqa: E402
from frozen_p5 import transport as FT  # noqa: E402
from test_p5_parity import AAPL, C200, P195, P200, TALLY, combo, family, opt, parametrized, settle  # noqa: E402
from trade_engine.domain.instruments import Combo, Side  # noqa: E402
from trade_engine.domain.orders import Order, OrderState, OrderType, TimeInForce  # noqa: E402
from trade_engine.interfaces.broker import UnsupportedCapability, VenueOrder, VenueOrderAllocation  # noqa: E402
from trade_engine.ledger.events import MirrorAllocation, MirrorQueued  # noqa: E402
from trade_engine.ledger.mirror import MirrorState, MirrorTicketState  # noqa: E402
from trade_engine.tos_paper import broker as PB  # noqa: E402
from trade_engine.tos_paper import transport as PT  # noqa: E402

D = Decimal
UTC = timezone.utc
T0 = datetime(2026, 9, 24, 20, 0, tzinfo=UTC)
PM_A, PM_B = "D-00000001", "D-00000002"
MIRRORED = ("OPT_CSP", "OPT_PUT_SPREAD")
P190 = opt(strike="190")
TRANSPORT = ("connect", "place_order", "read_positions", "read_working_orders", "read_order_fills",
             "cancel_order", "close")
OPS: Counter = Counter()        # op name -> steps
OUTCOMES: dict[str, Counter] = {}   # op name -> ok / raise


class ReplayMismatch(BaseException):
    """The oracle did not make the call the Rust side made. A BaseException: ``except Exception`` passes it."""


# -- comparison ------------------------------------------------------------------------------------


def ser(x):
    """A comparable form: dataclasses by field, Decimals by str, datetimes by isoformat, enums by name."""
    if isinstance(x, Decimal):
        return "D:" + str(x)
    if isinstance(x, (datetime, date)):
        return x.isoformat()
    if isinstance(x, Enum):
        return f"{type(x).__name__}.{x.name}"
    if dataclasses.is_dataclass(x) and not isinstance(x, type):
        return {"_": type(x).__name__, **{f.name: ser(getattr(x, f.name)) for f in dataclasses.fields(x)}}
    if isinstance(x, (MappingProxyType, dict)):
        return [[ser(k), ser(v)] for k, v in x.items()]
    if isinstance(x, (list, tuple)):
        return [ser(i) for i in x]
    if isinstance(x, (set, frozenset)):
        return sorted(repr(ser(i)) for i in x)
    return x


def to_frozen(exc: BaseException) -> BaseException:
    """The oracle's own class for a production transport exception (the frozen classes are distinct)."""
    for cls in type(exc).__mro__:
        frozen = {"TransportRefused": FT.TransportRefused, "TransportReplay": FT.TransportReplay,
                  "TransportUnavailable": FT.TransportUnavailable}.get(cls.__name__)
        if frozen is not None and cls.__module__.startswith("trade_engine"):
            return frozen(*exc.args)
    return exc


def _method(name, real, log, tape, replay):
    def call(self, *args):
        shown = ser(args)
        if replay:
            if not tape:
                raise ReplayMismatch(f"the oracle made an extra {name}{shown}")
            n, a, kind, payload = tape.pop(0)
            if (n, a) != (name, shown):
                raise ReplayMismatch(f"the oracle called {name}{shown}; the Rust side called {n}{a}")
            if kind == "exc":
                log.append((name, shown, "raise", type(payload).__name__, str(payload)))
                raise to_frozen(payload)
            log.append((name, shown, "ok", ser(payload)))
            return copy.deepcopy(payload)
        try:
            out = getattr(real, name)(*args)
        except (Exception, KeyboardInterrupt) as exc:
            tape.append((name, shown, "exc", exc))
            log.append((name, shown, "raise", type(exc).__name__, str(exc)))
            raise
        tape.append((name, shown, "ok", copy.deepcopy(out)))
        log.append((name, shown, "ok", ser(out)))
        return out
    return call


def _proxy(real, log, tape, replay):
    """Only the methods the real transport has: ``isinstance(.., OrderCanceller)`` must read the same."""
    ns = {name: _method(name, real, log, tape, replay) for name in TRANSPORT if hasattr(real, name)}
    return type("Replay" if replay else "Record", (), ns)()


class LogClock:
    """Each tick is logged in order with the transport calls. The Rust side reads the real clock and tapes
    the answer; the oracle's replays it, so a test that moves its clock mid-scenario moves both."""

    def __init__(self, inner, log, ticks, replay) -> None:
        self._inner, self._log, self._ticks, self._replay = inner, log, ticks, replay

    def now_utc(self):
        self._log.append(("now",))
        if self._replay:
            if not self._ticks:
                raise ReplayMismatch("the oracle read the clock more often than the Rust side")
            return self._ticks.pop(0)
        t = self._inner.now_utc()
        self._ticks.append(t)
        return t

    def sleep(self, seconds) -> None: ...


class StepClock:
    """Each call is one tick later than the last; a naive one reads naive."""

    def __init__(self, start=T0, step=timedelta(seconds=1), naive=False) -> None:
        self.t, self.step, self.naive = start, step, naive

    def now_utc(self):
        t = self.t
        self.t += self.step
        return t.replace(tzinfo=None) if self.naive else t


def snap_rust(b) -> dict:
    return json.loads(b._core.state())


def snap_oracle(b) -> dict:
    book = lambda m: [[H.wire(i), str(q)] for i, q in m.items()]  # noqa: E731
    return {
        "halted": b._halted, "restored": b._restored,
        "queued": [H.venue_order_doc(v) for v in b._queue],
        "keys": sorted(b._keys),
        "expected": book(b._expected),
        "sent": [[k, H.venue_order_doc(t), oid, str(r)] for k, (t, oid, r) in b._sent.items()],
        "proven": [[k, [oid, st.value]] for k, (oid, st) in b._proven.items()],
        "cancelled": sorted(b._cancelled),
        "preflight": None if b._preflight_book is None else [b._preflight_book[0].isoformat(),
                                                             book(b._preflight_book[1])],
    }


def outcome_of(exc: BaseException):
    extra = ser(exc.reconcile) if isinstance(exc, PB.VenueUnreadable) or isinstance(exc, FB.VenueUnreadable) else None
    return ("raise", type(exc).__name__, str(exc), extra)


class Lock:
    """One Rust-backed adapter and one frozen oracle, advanced op by op and compared after each."""

    def __init__(self, transport, binding: PB.MirrorBinding, clock, *, balance_reader=None,
                 balance_unproven_ok=False, halted_venues=(), tally="broker") -> None:
        self.tally = tally
        self.rlog, self.olog, self.tape, self.ticks = [], [], [], []
        self.rust = PB.TosPaperBroker(
            _proxy(transport, self.rlog, self.tape, False), binding, clock=LogClock(clock, self.rlog, self.ticks, False),
            balance_reader=balance_reader, balance_unproven_ok=balance_unproven_ok, halted_venues=halted_venues)
        frozen_binding = FB.MirrorBinding(
            venue_account=binding.venue_account, account_type=binding.account_type,
            mirrored_accounts=binding.mirrored_accounts, minimum_balance=binding.minimum_balance)
        self.oracle = FB.TosPaperBroker(
            _proxy(transport, self.olog, self.tape, True), frozen_binding,
            clock=LogClock(None, self.olog, self.ticks, True), balance_reader=balance_reader,
            balance_unproven_ok=balance_unproven_ok, halted_venues=halted_venues)

    @staticmethod
    def _run(broker, name, args, kwargs):
        try:
            return ("ok", getattr(broker, name)(*args, **kwargs))
        except ReplayMismatch as mismatch:
            raise AssertionError(str(mismatch)) from None
        except (Exception, KeyboardInterrupt) as exc:  # noqa: BLE001 - the refusal is the datum
            return ("raise", exc)

    def op(self, name, *args, **kwargs):
        self.rlog.clear(), self.olog.clear(), self.tape.clear(), self.ticks.clear()
        r = self._run(self.rust, name, args, kwargs)
        o = self._run(self.oracle, name, args, kwargs)
        label = (self.tally, name, len(OPS))
        assert not self.ticks, (label, "the oracle read the clock less often", self.ticks)
        assert not self.tape, (label, "the oracle left recorded calls unmade", self.tape)
        assert self.rlog == self.olog, (label, "call logs differ", self.rlog, self.olog)
        if r[0] == "ok" and o[0] == "ok":
            assert ser(r[1]) == ser(o[1]), (label, ser(r[1]), ser(o[1]))
            key = "ok"
        else:
            a = outcome_of(r[1]) if r[0] == "raise" else ("ok", ser(r[1]))
            b = outcome_of(o[1]) if o[0] == "raise" else ("ok", ser(o[1]))
            assert a == b, (label, a, b)
            key = (a[1], family(a[2]))
        assert snap_rust(self.rust) == snap_oracle(self.oracle), (label, snap_rust(self.rust), snap_oracle(self.oracle))
        assert self.rust.halted == self.oracle.halted and ser(self.rust.queued) == ser(self.oracle.queued), label
        OPS[name] += 1
        TALLY.setdefault(self.tally, Counter())[key] += 1
        OUTCOMES.setdefault(name, Counter())["ok" if r[0] == "ok" else "raise"] += 1
        if r[0] == "raise":
            raise r[1]
        return r[1]

    def attempt(self, name, *args, **kwargs):
        """``op``, the refusal returned rather than raised."""
        try:
            return self.op(name, *args, **kwargs)
        except (Exception, KeyboardInterrupt) as exc:  # noqa: BLE001
            if isinstance(exc, AssertionError):
                raise
            return exc


# -- the fake venue --------------------------------------------------------------------------------


class Venue:
    """An in-memory paperMoney that behaves as the seed says, never as the broker hopes."""

    def __init__(self, rng: random.Random) -> None:
        self.rng = rng
        self.positions: list[dict] = []
        self.working: list[dict] = []
        self.fill_rows: list = []
        self.ids: dict[str, dict] = {}
        self.send_plan: list[str] = []
        self.read_plan: list = []
        self.cancel_plan: list[str] = []
        self.chaos = True
        self.next_id = 5400000001
        self.calls: list[str] = []

    def connect(self):
        return {"number": PM_A, "type": "margin"}

    def _row(self, ticket):
        if isinstance(ticket, PT.MirrorComboTicket) or isinstance(ticket, FT.MirrorComboTicket):
            return None
        base = {"symbol": ticket.symbol, "side": ticket.side, "quantity": str(ticket.quantity),
                "order_type": ticket.order_type,
                "limit_price": None if ticket.limit_price is None else str(ticket.limit_price)}
        if isinstance(ticket, (PT.MirrorStockTicket, FT.MirrorStockTicket)):
            base["kind"] = "stock"
        return base

    def place_order(self, ticket, key):
        self.calls.append("place")
        kinds = ["rest"] * 6 + ["fill"] * 2 + ["rest_noid", "bad_id", "unk_book", "rejected_row"]
        chaos = ["vanish", "dry", "refused", "refused_bare", "unknown", "garbage", "raise_refused", "raise_replay",
                 "raise_runtime", "raise_unavailable"]
        kind = self.send_plan.pop(0) if self.send_plan else self.rng.choice(kinds + (chaos if self.chaos else []))
        row = self._row(ticket)
        if kind == "raise_refused":
            raise PT.TransportRefused("echo mismatch")
        if kind == "raise_replay":
            raise PT.TransportReplay("key already used")
        if kind == "raise_runtime":
            raise RuntimeError("JAB hung")
        if kind == "raise_unavailable":
            raise PT.TransportUnavailable("no window")
        if kind == "interrupt":
            raise KeyboardInterrupt("stop")
        if kind == "vanish":
            return {"status": "SENT"}
        if kind == "dry":
            return {"status": "DRY_RUN", "echo": {}}
        if kind == "refused":
            return {"status": "REFUSED", "reason": "account not eligible"}
        if kind == "refused_bare":
            return {"status": "INELIGIBLE"}
        if kind == "unknown":
            return {"status": "???"}
        if kind == "garbage":
            return self.rng.choice(["ok", None, ["SENT"], 5])
        out = {"status": "SENT"}
        oid = str(self.next_id)
        self.next_id += 1
        if kind == "fill" and row is not None:
            signed = ticket.quantity if ticket.side == "BUY" else -ticket.quantity
            self.positions.append({k: v for k, v in {"kind": row.get("kind"), "symbol": ticket.symbol,
                                                     "quantity": str(signed), "avg_price": "2.00"}.items()
                                   if v is not None})
            out.update(order_id=oid, book_status="FILLED")
            return out
        if row is not None:
            status = "REJECTED" if kind == "rejected_row" else "WORKING"
            live = {**row, "filled": "0", "status": status}
            self.working.append(live)
            self.ids[oid] = live
        if kind in ("rest", "rejected_row"):
            out.update(order_id=oid, book_status="WORKING")
        elif kind == "bad_id":
            out.update(order_id="abc", book_status="WORKING")
        elif kind == "unk_book":
            out.update(order_id=oid, book_status="UNKNOWN")
        return out

    def _read(self, rows):
        if self.read_plan:
            fault = self.read_plan.pop(0)
            if fault is not None:
                raise fault
        return [dict(r) for r in rows]

    def read_positions(self):
        self.calls.append("read_positions")
        return self._read(self.positions)

    def read_working_orders(self):
        self.calls.append("read_working")
        return self._read(self.working)

    def close(self) -> None: ...


class CancelMixin:
    def cancel_order(self, order_id):
        self.calls.append("cancel")
        kind = self.cancel_plan.pop(0) if self.cancel_plan else "ok"
        if kind == "refused":
            raise PT.TransportRefused(f"order {order_id} is FILLED, not WORKING")
        if kind == "timeout":
            raise TimeoutError("JAB hung")
        if kind == "unknown":
            return {"status": "UNKNOWN", "order_id": order_id, "note": "row still WORKING"}
        if kind == "garbage":
            return "x"
        if order_id in self.ids:
            self.ids[order_id]["status"] = "CANCELED"
        return {"status": "CANCELED", "order_id": order_id, "book_status": "CANCELED"}


class FillsMixin:
    def read_order_fills(self):
        self.calls.append("read_fills")
        return self._read(self.fill_rows)


class V0(Venue): ...
class VC(CancelMixin, Venue): ...
class VF(FillsMixin, Venue): ...
class VCF(CancelMixin, FillsMixin, Venue): ...


# -- the generators ----------------------------------------------------------------------------------

VERT = lambda a, b: combo((a, 1, Side.SELL), (b, 1, Side.BUY))  # noqa: E731
INSTS = [P200, P195, P190, C200, P200, P195, P190, VERT(P200, P195), VERT(P195, P190), AAPL]


def mk_order(rng: random.Random, oid: str) -> Order:
    inst = rng.choice(INSTS)
    account = rng.choice(["OPT_CSP", "OPT_CSP", "OPT_PUT_SPREAD", "OPT_PUT_SPREAD", "OPT_PUT_SPREAD", "OPT_WHEEL"])
    side = rng.choice([Side.SELL, Side.SELL, Side.BUY])
    qty = rng.choice(["1", "1", "2", "3", "2", "1.5"]) if not isinstance(inst, Combo) else rng.choice(["1", "2"])
    if inst == AAPL:
        qty = rng.choice(["100", "50", "1.5"])
    otype = OrderType.MARKET if (rng.random() < .12 and not isinstance(inst, Combo)) else OrderType.LIMIT
    tif = rng.choice([TimeInForce.DAY, TimeInForce.DAY, TimeInForce.GTC, TimeInForce.GTD])
    return Order(order_id=oid, account_id=account, instrument=inst, order_type=otype, side=side, quantity=D(qty),
                 command_id=oid, created_at=T0, limit_price=None if otype is OrderType.MARKET else D(
                     rng.choice(["2.00", "1.50", "0.75"])), tif=tif)


def mq(v: VenueOrder, venue=PM_A) -> MirrorQueued:
    return MirrorQueued(venue=venue, ticket_key=v.venue_order_id, instrument=v.instrument, side=v.side,
                        quantity=v.quantity, order_type=v.order_type, limit_price=v.limit_price, tif=v.tif,
                        allocations=tuple(MirrorAllocation(a.strategy_order_id, a.account_id, a.quantity)
                                          for a in v.allocations), at=v.submitted_at)


class Ctx:
    """One seeded walk: the op chooser and the inputs it builds from what the broker has done."""

    def __init__(self, seed: int, profile: dict, tally: str) -> None:
        rng = self.rng = random.Random(seed)
        self.seed, self.profile = seed, profile
        cls = rng.choice([V0, VC, VCF, VCF, VC, VF])
        self.venue = cls(rng)
        self.venue.chaos = rng.random() < profile.get("chaos", .5)
        step = rng.choice([0, 0, 1, 7, 20, 31])
        self.naive = rng.random() < profile.get("naive", .04)
        clock = StepClock(step=timedelta(seconds=step), naive=self.naive)
        halted = (PM_A,) if rng.random() < .08 else ((PM_B,) if rng.random() < .1 else ())
        binding = PB.MirrorBinding(PM_A, "margin", MIRRORED, D("100000"))
        self.lock = Lock(self.venue, binding, clock, balance_reader=_Balance("150000"), halted_venues=halted,
                         tally=tally)
        self.known: list[VenueOrder] = []
        self.orders: list[Order] = []
        self.n = 0
        self.connected = False

    # -- inputs --
    def orders_batch(self) -> list[Order]:
        out = []
        for _ in range(self.rng.choice([1, 1, 2, 3, 4])):
            if self.orders and self.rng.random() < .2:
                out.append(self.rng.choice(self.orders))      # the same ticket again: queued once (I3)
                continue
            self.n += 1
            out.append(mk_order(self.rng, f"so-{self.seed}-{self.n}"))
        self.orders += out
        return out

    def holdings(self) -> dict:
        if self.rng.random() < .75:
            return {}
        return {(self.rng.choice(MIRRORED), self.rng.choice([P200, P195, P190, C200])): D(
            self.rng.choice(["0", "1", "-1", "-2", "1.0"])) for _ in range(self.rng.choice([1, 2]))}

    def key(self) -> str:
        pool = [v.venue_order_id for v in self.known]
        return self.rng.choice(pool + ["tos:never", "tos:never", ""] if pool else ["tos:never"])

    def mirror(self, venue=PM_A) -> MirrorState:
        tickets, book = [], {}
        for v in self.rng.sample(self.known, k=min(len(self.known), self.rng.choice([0, 1, 2, 3, 5]))):
            proven = self.lock.rust.proven_order_id(v.venue_order_id)
            oid = proven or self.rng.choice([None, str(self.rng.randint(5400000100, 5400000199))])
            qty = int(v.quantity)
            filled = self.rng.randint(0, qty) if self.rng.random() < .5 else 0
            avg = None if filled == 0 else D(self.rng.choice(["2", "2.00", "2.05"]))
            closed = self.rng.random() < .15
            status = self.rng.choice([None, None, OrderState.ACCEPTED, OrderState.CANCELLED, OrderState.PARTIALLY_FILLED]
                                     ) if oid else None
            tickets.append(MirrorTicketState(queued=mq(v, venue), venue_order_id=oid, book_status=status,
                                             filled=D(filled), avg_price=avg, closed=closed))
        for _ in range(self.rng.choice([0, 0, 1, 2])):
            book[(self.rng.choice(MIRRORED), self.rng.choice([P200, P195, C200]))] = D(
                self.rng.choice(["1", "-1", "-2", "2"]))
        return MirrorState(venue=venue, tickets=MappingProxyType({t.key: t for t in tickets}),
                           book=MappingProxyType(book))

    def fill_rows(self, mirror: MirrorState) -> list:
        """Cumulative rows against the fold: grow, repeat, shrink, over, FILLED short, ended, unknown ids."""
        rows = []
        for t in mirror.tickets.values():
            if t.venue_order_id is None or self.rng.random() < .1:
                continue
            qty, have = int(t.queued.quantity), int(t.filled)
            mode = self.rng.choice(["grow", "grow", "repeat", "repeat", "shrink", "over", "short", "end", "price",
                                    "poison"])
            filled, status, avg = have, "WORKING", None if have == 0 else str(t.avg_price)
            if mode == "grow":
                filled = self.rng.randint(have, qty)
                avg = None if filled == 0 else self.rng.choice(["2.05", "2", "1.95"])
                status = "FILLED" if filled == qty and filled > 0 else "PARTIAL" if filled else "WORKING"
            elif mode == "shrink":
                filled = max(have - 1, 0)
                avg = None if filled == 0 else (avg or "2")
            elif mode == "over":
                filled, avg = qty + 1, "2.00"
            elif mode == "short":
                filled, status, avg = max(qty - 1, 1), "FILLED", "2.00"
            elif mode == "end":
                status, avg = self.rng.choice(["CANCELED", "EXPIRED", "REJECTED", "FILLED"]), avg
                if status == "FILLED":
                    filled, avg = qty, "2.00"
            elif mode == "price":
                avg = "2.10" if have else avg
            row = {"order_id": t.venue_order_id, "filled": str(filled), "avg_price": avg, "status": status}
            if mode == "poison":
                row["filled"] = self.rng.choice(["x", "-1", "1.5", None])
            rows.append(row)
            if self.rng.random() < .06:
                rows.append(dict(row))                   # two rows for one order
        if self.rng.random() < .2:
            rows.append({"order_id": "5499999999", "filled": "1", "avg_price": "2.00", "status": "WORKING"})
        return rows

    def venue_order(self) -> VenueOrder:
        kind = self.rng.choice(["ok", "ok", "gtd", "stop", "frac"])
        inst = self.rng.choice([P200, P190, AAPL])
        kw = dict(venue_order_id=f"tos:direct-{self.seed}-{self.n}", instrument=inst, order_type=OrderType.LIMIT,
                  side=Side.SELL, quantity=D("1.5") if kind == "frac" else D("1"), submitted_at=T0,
                  tif=TimeInForce.GTD if kind == "gtd" else TimeInForce.DAY, limit_price=D("2.00"),
                  allocations=(VenueOrderAllocation("so-1", "OPT_CSP", D("1.5") if kind == "frac" else D("1")),))
        if kind == "stop":
            kw.update(order_type=OrderType.STOP, limit_price=None, stop_price=D("1.00"))
        self.n += 1
        return VenueOrder(**kw)

    # -- ops --
    def plan_reads(self, k: int | None = None) -> None:
        if self.rng.random() < self.profile.get("read_fault", .15):
            fault = self.rng.choice([RuntimeError("JAB tree gone"), PT.TransportUnavailable("no window"),
                                     TimeoutError("slow"), RuntimeError("JAB tree gone")])
            self.venue.read_plan = [None] * self.rng.randint(0, k if k is not None else 6) + [fault]
        else:
            self.venue.read_plan = []

    def step(self) -> None:
        rng, lock = self.rng, self.lock
        op = rng.choices(list(self.profile["ops"]), weights=list(self.profile["ops"].values()))[0]
        if op == "mirror_batch":
            batch = lock.attempt("mirror_batch", self.orders_batch(), holdings=self.holdings())
            if not isinstance(batch, BaseException):
                self.known += [v for v in batch.venue_orders if v not in self.known]
        elif op == "drain":
            self.venue.send_plan = ["interrupt" if (rng.random() < self.profile.get("interrupt", .05) and i == 1)
                                    else rng.choice(["rest", "rest", "fill"]) for i in range(rng.randint(0, 3))
                                    ] if rng.random() < .35 else []
            self.plan_reads()
            lock.attempt("drain")
        elif op == "preflight":
            self.plan_reads(2)
            lock.attempt("preflight")
        elif op == "reconcile_now":
            self.plan_reads(2)
            lock.attempt("reconcile_now", defer_unavailable=rng.random() < .5)
        elif op == "cancel":
            self.venue.cancel_plan = [rng.choice(["ok", "ok", "unknown", "refused", "timeout", "garbage"])
                                      for _ in range(2)]
            lock.attempt("cancel", self.key())
        elif op == "proven":
            lock.attempt("proven_order_id", self.key())
        elif op == "restore":
            venue = rng.choice([PM_A, PM_A, PM_A, PM_B, "D-00000009"])
            mirror = self.mirror(venue)
            halted = rng.choice([(), (), (PM_A,), (PM_B,), (PM_A, PM_B)])
            lock.attempt("restore", mirror, halted_venues=halted)
        elif op == "collect":
            mirror = self.mirror(rng.choice([PM_A] * 7 + [PM_B, "D-00000009"]))
            self.venue.fill_rows = self.fill_rows(mirror)
            if self.venue.rng.random() < .05:
                self.venue.fill_rows = [3, "x"]
            self.plan_reads(0)
            lock.attempt("collect_fills", mirror)
        elif op == "submit":
            self.plan_reads(0)
            lock.attempt("submit", self.venue_order())
        elif op == "positions":
            self.plan_reads(0)
            lock.attempt("positions")
        else:
            raise AssertionError(op)

    def run(self) -> None:
        rng = self.rng
        if rng.random() < .15:                                   # before connect: every op refuses
            pick = rng.choice(["mirror_batch", "drain", "preflight", "cancel", "positions", "submit"])
            args = {"mirror_batch": ([mk_order(rng, "pre")],), "cancel": ("tos:x",),
                    "submit": (self.venue_order(),)}.get(pick, ())
            self.lock.attempt(pick, *args, **({"holdings": {}} if pick == "mirror_batch" else {}))
        if isinstance(self.lock.attempt("connect"), BaseException):
            return
        for _ in range(rng.randint(self.profile.get("min", 6), self.profile.get("max", 16))):
            self.step()


class _Balance:
    def __init__(self, value) -> None:
        self.value = value

    def net_liquidation(self):
        return self.value


def walk(seeds, profile, tally):
    for seed in seeds:
        Ctx(seed, profile, tally).run()


def seen_outcome(tally: str, pattern: str) -> bool:
    return any(pattern in f"{k[0]}: {k[1]}" for k in TALLY[tally] if k != "ok")


# -- the lockstep walks --------------------------------------------------------------------------------

SEND = {"mirror_batch": 6, "drain": 6, "preflight": 3, "reconcile_now": 1.5, "cancel": 1.5, "proven": 1.5,
        "positions": .3, "submit": .6, "collect": .5, "restore": .3}
RESTART = {"mirror_batch": 5, "drain": 4, "restore": 3, "proven": 2, "collect": 1.5, "cancel": 1,
           "reconcile_now": 1, "preflight": 1}
FILLS = {"mirror_batch": 4, "drain": 4, "collect": 6, "restore": 1.5, "proven": 1, "cancel": 1,
         "reconcile_now": 1, "preflight": .5}
CANCELS = {"mirror_batch": 4, "drain": 3, "cancel": 7, "proven": 1.5, "restore": .7, "collect": .7,
           "reconcile_now": 1, "preflight": .5, "submit": .3, "positions": .2}


def test_p5_t9_send_lockstep() -> None:
    """mirror_batch -> drain with SENT / DRY_RUN / FAILED / unknown / raising sends, the preflight, the reconcile."""
    start = sum(OPS.values())
    walk(range(9000, 9900), {"ops": SEND, "chaos": .6, "interrupt": .06}, "broker_send")
    assert sum(OPS.values()) - start >= 5000, sum(OPS.values()) - start
    c = TALLY["broker_send"]
    for want in ("mirror_batch before connect", "drain before connect", "preflight before connect",
                 "KeyboardInterrupt", "TransportUnavailable", "timezone-aware", "UnsupportedCapability",
                 "restore over an undrained queue"):
        assert seen_outcome("broker_send", want) or want in ("restore over an undrained queue",), (want, sorted(map(str, c)))
    assert c["ok"] > 3000


def test_p5_t9_restart_lockstep() -> None:
    """A partial drain (interrupted mid-batch), then restore with and without halted_venues."""
    start = sum(OPS.values())
    walk(range(10000, 10900), {"ops": RESTART, "chaos": .5, "interrupt": .25}, "broker_restart")
    assert sum(OPS.values()) - start >= 5000, sum(OPS.values()) - start
    for want in ("restore over an undrained queue", "cannot restore venue", "KeyboardInterrupt"):
        assert seen_outcome("broker_restart", want), (want, sorted(map(str, TALLY["broker_restart"])))


def test_p5_t9_fills_lockstep() -> None:
    """collect_fills with cumulative rows that grow, repeat, shrink (halt), or name unknown tickets."""
    start = sum(OPS.values())
    walk(range(11000, 11900), {"ops": FILLS, "chaos": .2, "interrupt": 0, "naive": .0, "read_fault": .1}, "broker_fills")
    assert sum(OPS.values()) - start >= 4500, sum(OPS.values()) - start
    for want in ("venue fills contradict the mirror", "read-back failed", "fill rows for order",
                 "cannot read order fills", "mirror is not", "cannot restore venue"):
        assert seen_outcome("broker_fills", want), (want, sorted(map(str, TALLY["broker_fills"])))


def test_p5_t9_cancel_lockstep() -> None:
    """Cancels of sent, unsent, unknown and already-cancelled tickets, on a halted venue too."""
    start = sum(OPS.values())
    walk(range(12000, 12900), {"ops": CANCELS, "chaos": .3, "interrupt": 0.02}, "broker_cancel")
    assert sum(OPS.values()) - start >= 5000, sum(OPS.values()) - start
    assert OUTCOMES["cancel"]["ok"] > 400


FAMILIES = ("before connect", "restore over an undrained queue", "cannot restore venue", "is not",
            "the transport cannot read order fills", "venue fills contradict the mirror", "read-back failed",
            "fill rows for order", "TransportUnavailable", "no window", "TIF GTD", "order type STOP",
            "whole number of contracts", "timezone-aware", "KeyboardInterrupt")


def test_zz_p5_t9_op_and_family_coverage() -> None:
    """Runs after the walks (file order): every op both succeeded and refused somewhere, every refusal family
    refused, the rest of the family's op succeeded, and the step count is the one the walks promised."""
    for op in ("mirror_batch", "restore", "collect_fills", "preflight", "drain", "cancel", "submit",
               "reconcile_now", "positions", "connect"):
        got = OUTCOMES.get(op, Counter())
        assert got["ok"] > 0 and got["raise"] > 0, (op, got)
    assert OPS["proven_order_id"] > 300
    refusals = Counter()
    for tally, counter in TALLY.items():
        if not tally.startswith("broker"):
            continue
        for key, n in counter.items():
            if key != "ok":
                refusals[f"{key[0]}: {key[1]}"] += n
    for fam in FAMILIES:
        assert any(fam in k for k in refusals), (fam, sorted(refusals))
    assert sum(OPS.values()) >= 20000, sum(OPS.values())
    assert any("contradict the mirror" in k for k in refusals)    # the halt latched (the snapshot compared it)


# -- test_tos_broker.py's scenarios through both sides ---------------------------------------------------


class LockstepBroker:
    """``TosPaperBroker``'s signature; every operation runs through a ``Lock``. Attributes it does not
    wrap (``venue``, ``binding``, ...) read from the Rust-backed adapter."""

    def __init__(self, transport, binding, *, clock=None, balance_reader=None, balance_unproven_ok=False,
                 halted_venues=()) -> None:
        self._lock = Lock(transport, binding, clock, balance_reader=balance_reader,
                          balance_unproven_ok=balance_unproven_ok, halted_venues=halted_venues, tally="broker_scenarios")

    def __getattr__(self, name):
        return getattr(self._lock.rust, name)

    @property
    def halted(self):
        return self._lock.rust.halted

    @property
    def queued(self):
        return self._lock.rust.queued


def _wrap(name):
    def call(self, *a, **k):
        return self._lock.op(name, *a, **k)
    call.__name__ = name
    return call


for _name in ("connect", "balance", "mirror_batch", "restore", "collect_fills", "preflight", "drain",
              "reconcile_now", "submit", "cancel", "positions", "proven_order_id", "replace", "orders", "fills",
              "cash_events"):
    setattr(LockstepBroker, _name, _wrap(_name))


def test_p5_t9_the_broker_test_module_runs_on_both_sides(monkeypatch) -> None:
    import test_tos_broker as V
    from test_p5_parity import run_module_tests

    plain = {"test_the_fake_is_a_transport_and_the_broker_an_adapter"}   # an isinstance check on the class
    monkeypatch.setattr(V, "TosPaperBroker", LockstepBroker)
    names = {n for n in vars(V) if n.startswith("test_")} - plain
    before = sum(OPS.values())
    ran = run_module_tests(V, only=names)
    assert ran >= 65, ran
    assert sum(OPS.values()) - before >= 150, sum(OPS.values()) - before
    monkeypatch.setattr(V, "TosPaperBroker", PB.TosPaperBroker)
    assert run_module_tests(V, only=plain) == 1

"""P5 lockstep (T1 transport, T2 normalize, T3 slippage): the frozen Python oracle against the
Rust door ``trade_engine_rs.tos_paper_decide(op, json)`` on identical inputs.

Returns are compared as Decimal text, refusals by exception type NAME and message (the frozen
and the production classes are distinct objects with one name). Every comparison is a step; each
test asserts its step count and that every refusal family both refuses and succeeds somewhere.
"""
from __future__ import annotations

import copy
import dataclasses
import decimal
import inspect
import json
import os
import random
import sys
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
import trade_engine_rs as rs  # D5: a missing extension is an error.

sys.path.insert(0, str(Path(__file__).resolve().parent))
from frozen_p5 import normalize as FN, slippage as FS, transport as FT
from frozen_p5.netting import vertical_reason as frozen_vertical_reason
from trade_engine.domain.instruments import Combo, ComboLeg, Equity, OptionContract, OptionRight, Side
from trade_engine.domain.orders import OrderState, OrderType, TimeInForce
from trade_engine.domain.portfolio import Fill
from trade_engine.interfaces.broker import (
    UnsupportedCapability, VenueAck, VenueFill, VenueOrder, VenueOrderAllocation, VenuePosition,
)
from trade_engine.sim import _rs as sim_rs
from trade_engine.tos_paper import normalize as PN, slippage as PS

D = Decimal
UTC = timezone.utc
T = datetime(2026, 9, 24, 20, 0, tzinfo=UTC)
TALLY: dict[str, Counter] = {}

# The kinds the door's refusals carry. Production names them nowhere yet (T9/T10 do), so the test
# registers them; the names are what is compared.
sim_rs.register("tos_normalize", PN.NormalizeError)
sim_rs.register("tos_slippage", PS.SlippageError)
sim_rs.register("tos_unsupported", UnsupportedCapability)
sim_rs.register("tos_overflow_error", OverflowError)
sim_rs.register("tos_division_undefined", lambda _m: decimal.InvalidOperation([decimal.DivisionUndefined]))


def door(op: str, doc: dict) -> dict:
    return json.loads(sim_rs.call(rs.tos_paper_decide, op, json.dumps(doc)))


def outcome(fn):
    try:
        return ("ok", fn())
    except Exception as exc:  # noqa: BLE001 - the oracle's refusal is the datum
        return ("raise", type(exc).__name__, str(exc))


def step(tally: str, label, oracle, rust):
    a, b = outcome(oracle), outcome(rust)
    assert a == b, (label, a, b)
    TALLY.setdefault(tally, Counter())[a[0] if a[0] == "ok" else (a[1], family(a[2]))] += 1
    return a


def family(message: str) -> str:
    out, i = [], 0
    for word in message.split():
        out.append("#" if any(ch.isdigit() for ch in word) or word[:1] in "'\"[{(" else word)
    return " ".join(out[:10])


def settle(tally: str, steps: int, refusals: list[str]) -> None:
    """Step count, every named refusal family seen refusing, and success seen too."""
    counter = TALLY[tally]
    if os.environ.get('P5_DUMP'):
        print(tally, sum(counter.values()), sorted(map(str, counter)))
    assert sum(counter.values()) >= steps, (tally, sum(counter.values()))
    assert counter["ok"] > 0, (tally, "no success")
    seen = [message for key in counter if key != "ok" for message in [f"{key[0]}: {key[1]}"]]
    for want in refusals:
        assert any(want in s for s in seen), (tally, want, sorted(seen))


# -- instruments and the door wire -----------------------------------------------------------


class FutureLike:
    def __repr__(self) -> str:
        return "Future(ES)"


def wire(i) -> dict:
    if isinstance(i, Equity):
        return {"kind": "equity", "symbol": i.symbol}
    if isinstance(i, OptionContract):
        return {"kind": "option", "underlying": i.underlying, "expiry": i.expiry.isoformat(),
                "strike": str(i.strike), "right": i.right.value, "multiplier": i.multiplier}
    if isinstance(i, Combo):
        return {"kind": "combo", "legs": [{"contract": wire(l.contract), "ratio": l.ratio, "side": l.side.value}
                                           for l in i.legs]}
    return {"kind": "other", "repr": repr(i)}


def opt(underlying="AAPL", expiry=date(2026, 10, 16), strike="200", right="P", multiplier=100):
    return OptionContract(underlying=underlying, expiry=expiry, strike=D(strike),
                          right=OptionRight(right), multiplier=multiplier)


P200, P195, C200 = opt(), opt(strike="195"), opt(strike="200", right="C")
AAPL = Equity("AAPL")


def combo(*legs):
    return Combo([ComboLeg(c, r, s) for c, r, s in legs])


def instruments() -> list:
    out = [AAPL, Equity("BRK.B"), P200, P195, C200, opt(strike="0.5"), opt(strike="1234.567"),
           opt(underlying="SPX", strike="5000"), opt(expiry=date(2027, 1, 15)), opt(multiplier=10),
           FutureLike()]
    B, S = Side.BUY, Side.SELL
    out += [
        combo((P200, 1, S), (P195, 1, B)),                          # a vertical
        combo((P200, 1, B), (P195, 1, S)),
        combo((P200, 2, S), (P195, 2, B)),                          # 2:2 is still 1:1 in ratio
        combo((P200, 1, S), (P195, 2, B)),                          # ratio spread
        combo((P200, 1, S), (P200, 1, B)),                          # one strike
        combo((P200, 1, S), (P195, 1, S)),                          # one side
        combo((P200, 1, S), (opt(strike="195", expiry=date(2026, 11, 20)), 1, B)),   # calendar
        combo((P200, 1, S), (C200, 1, B)),                          # call and put
        combo((P200, 1, S), (opt(underlying="MSFT", strike="195"), 1, B)),
        combo((P200, 1, S), (opt(strike="195", multiplier=10), 1, B)),
        combo((P200, 1, S)),                                        # one leg
        combo((P200, 1, S), (P195, 1, B), (opt(strike="190"), 1, B)),
        combo((AAPL, 100, B), (C200, 1, S)),                        # buy-write
    ]
    return out


# -- T1 transport ----------------------------------------------------------------------------


def ticket_json(t) -> dict:
    dec = lambda d: None if d is None else str(d)  # noqa: E731
    if isinstance(t, FT.MirrorStockTicket):
        return {"kind": "stock", "symbol": t.symbol, "side": t.side, "quantity": t.quantity,
                "order_type": t.order_type, "limit_price": dec(t.limit_price), "tif": t.tif}
    if isinstance(t, FT.MirrorTicket):
        return {"kind": "option", "symbol": t.symbol, "side": t.side, "quantity": t.quantity,
                "order_type": t.order_type, "limit_price": dec(t.limit_price), "tif": t.tif,
                "underlying": t.underlying, "expiry": t.expiry.isoformat(), "strike": str(t.strike), "right": t.right}
    return {"kind": "combo", "underlying": t.underlying,
            "legs": [{"symbol": l.symbol, "side": l.side, "ratio": l.ratio, "expiry": l.expiry.isoformat(),
                      "strike": str(l.strike), "right": l.right} for l in t.legs],
            "quantity": t.quantity, "order_type": t.order_type, "limit_price": dec(t.limit_price),
            "price_effect": t.price_effect, "tif": t.tif}


def order_doc(o) -> dict:
    return {"instrument": wire(o.instrument), "order_type": o.order_type.value, "tif": o.tif.value,
            "quantity": str(o.quantity), "side": o.side.value,
            "limit_price": None if o.limit_price is None else str(o.limit_price)}


QUANTITIES = ["1", "2", "100", "2.0", "1E+2", "0.5", "1.5", "0", "-1", "-2.0", "0E+3", "NaN", "Infinity",
              "-Infinity", "1E+30", "123456789012345678901234567890", "0.0000001", "7"]
LIMITS = [None, "1.05", "0", "-1", "0.01", "150.25", "NaN", "1E+2", "0.000"]


def check_ticket_for(label, order) -> None:
    step("ticket_for", label,
         lambda: ticket_json(FT.ticket_for(order)),
         lambda: door("ticket_for", order_doc(order)))


def test_p5_t1_ticket_for_lockstep() -> None:
    rng = random.Random(5001)
    insts = instruments()
    for index, inst in enumerate(insts):               # the edge grid
        for ot in OrderType:
            for tif in (TimeInForce.DAY, TimeInForce.GTC, TimeInForce.GTD, TimeInForce.MOC):
                for q in ("1", "2.5", "0", "Infinity", "NaN", "-3"):
                    for side in Side:
                        for limit in (None, "1.05", "0"):
                            order = SimpleNamespace(instrument=inst, order_type=ot, tif=tif, quantity=D(q),
                                                    side=side, limit_price=None if limit is None else D(limit))
                            check_ticket_for((index, ot, tif, q, side, limit), order)
    for n in range(4000):                               # seeded
        order = SimpleNamespace(
            instrument=rng.choice(insts), order_type=rng.choice(list(OrderType)),
            tif=rng.choice(list(TimeInForce)), quantity=D(rng.choice(QUANTITIES)),
            side=rng.choice(list(Side)),
            limit_price=None if (lim := rng.choice(LIMITS)) is None else D(lim))
        check_ticket_for(n, order)
    settle("ticket_for", 5000, [
        "UnsupportedCapability: order type", "UnsupportedCapability: TIF", "is not a whole number",
        "UnsupportedCapability: Future(ES)", "UnsupportedCapability: multi-leg combo",
        "a vertical is mirrored with one", "ValueError: ticket quantity must be",
        "ValueError: an LMT ticket needs", "ValueError: a vertical ticket is LMT", "OverflowError", "InvalidOperation"])


def test_p5_t1_vertical_reason_lockstep() -> None:
    for inst in instruments():
        if isinstance(inst, Combo):
            step("vertical_reason", repr(inst), lambda: {"reason": frozen_vertical_reason(inst)},
                 lambda: door("vertical_reason", {"instrument": wire(inst)}))
    c = TALLY["vertical_reason"]
    assert sum(c.values()) >= 12 and c["ok"] == 13


NAMES = ["AAPL", "aapl", " AAPL", "BRK.B", "", None, 5, "AAPL  261016P00200000", "A" * 20, "ÄPL", "ß", "AAPL\n"]
SIDES = ["BUY", "SELL", "buy", "SHORT", "", None, 1, True]
QTYS = [1, 2, 100, 0, -1, True, False, 1.0, 1.5, "1", None, 10 ** 30, [1], {"a": 1}]
TYPES = ["MKT", "LMT", "STOP", "lmt", "", None, 3]
TIFS = ["DAY", "GTC", "GTD", "day", None, 0]
LIMS = [None, "1.05", "0", "-1", "NaN", "1E+2", "0.000", "150.25"]
EFFECTS = ["CREDIT", "DEBIT", "EVEN", "credit", None]
RATIOS = [1, 2, 0, -1, True, 1.0, "1", None, 10 ** 25]


def dec_or_none(x):
    return None if x is None else D(x)


def py_ticket(kind: str, f: dict):
    limit = dec_or_none(f.get("limit_price"))
    if kind == "option":
        return FT.MirrorTicket(symbol="AAPL  261016P00200000", side=f["side"], quantity=f["quantity"],
                               order_type=f["order_type"], limit_price=limit, tif=f["tif"], underlying="AAPL",
                               expiry=date(2026, 10, 16), strike=D(200), right="P")
    if kind == "stock":
        return FT.MirrorStockTicket(symbol=f["symbol"], side=f["side"], quantity=f["quantity"],
                                    order_type=f["order_type"], limit_price=limit, tif=f["tif"])
    if kind == "leg":
        return FT.MirrorComboLeg(symbol="A", side=f["side"], ratio=f["ratio"], expiry=date(2026, 10, 16),
                                 strike=D(1), right="P")
    legs = tuple(FT.MirrorComboLeg(symbol="A", side=s, ratio=r, expiry=date(2026, 10, 16), strike=D(1), right="P")
                 for s, r in f["legs"])
    return FT.MirrorComboTicket(underlying="AAPL", legs=legs, quantity=f["quantity"], order_type=f["order_type"],
                                limit_price=limit, price_effect=f["price_effect"], tif=f["tif"])


def validate_step(label, kind: str, f: dict) -> None:
    doc = {"kind": kind, **{k: v for k, v in f.items() if k != "legs"}}
    if kind == "combo":
        doc["legs"] = [{"side": s, "ratio": r} for s, r in f["legs"]]
    step("ticket_validate", label, lambda: (py_ticket(kind, f), {"valid": True})[1],
         lambda: door("ticket_validate", doc))


def test_p5_t1_ticket_validation_lockstep() -> None:
    rng = random.Random(5002)
    base = {"side": "SELL", "quantity": 1, "order_type": "LMT", "limit_price": "2.00", "tif": "DAY"}
    for kind in ("option", "stock"):                    # the grid: one field off a good ticket
        for key, pool in (("side", SIDES), ("quantity", QTYS), ("order_type", TYPES), ("tif", TIFS),
                          ("limit_price", LIMS)):
            for value in pool:
                f = dict(base, symbol="AAPL", **{key: value})
                validate_step((kind, key, value), kind, f)
        if kind == "stock":
            for name in NAMES:
                validate_step((kind, "symbol", name), kind, dict(base, symbol=name))
        for ot, lim in (("MKT", None), ("MKT", "1"), ("LMT", None), ("LMT", "0")):
            validate_step((kind, ot, lim), kind, dict(base, symbol="AAPL", order_type=ot, limit_price=lim))
    for side in SIDES:
        for ratio in RATIOS:
            validate_step(("leg", side, ratio), "leg", {"side": side, "ratio": ratio})
    sides = ["BUY", "SELL"]
    for _ in range(1500):                               # seeded
        kind = rng.choice(["option", "stock", "leg", "combo"])
        f = {"side": rng.choice(SIDES), "quantity": rng.choice(QTYS), "order_type": rng.choice(TYPES),
             "tif": rng.choice(TIFS), "limit_price": rng.choice(LIMS), "symbol": rng.choice(NAMES),
             "ratio": rng.choice(RATIOS), "price_effect": rng.choice(EFFECTS)}
        if kind == "combo":
            f["legs"] = [(rng.choice(sides), rng.choice([1, 1, 2])) for _ in range(rng.choice([1, 2, 2, 2, 3]))]
            if rng.random() < 0.7:
                f["order_type"], f["tif"] = "LMT", rng.choice(["DAY", "GTC", "GTD"])
        validate_step(("seeded", _), kind, f)
    settle("ticket_validate", 700, [
        "ValueError: ticket side must be", "ValueError: ticket quantity must be", "ValueError: ticket order_type",
        "ValueError: an LMT ticket needs", "ValueError: a MKT ticket cannot", "ValueError: ticket tif must be",
        "ValueError: stock ticket symbol", "ValueError: leg side must be", "ValueError: leg ratio must be",
        "ValueError: a combo ticket is a vertical", "ValueError: a vertical's legs trade",
        "ValueError: a vertical ticket is LMT", "ValueError: price_effect must be"])


# -- T2 normalize ----------------------------------------------------------------------------

OCCS = ["AAPL  261016P00200000", "AAPL261016P00200000", "SPX   261016C05000000", "AAPL  261016C00200500",
        "AAPL", "", "AAPL  261016X00200000", "AAPL  261316P00200000", "aapl  261016P00200000",
        "AAPL  261016P0020000", "AAPL  261016P00200000 ", None, 5, "AAPL  261016P00000000"]
STOCKS = ["AAPL", "BRK.B", "aapl", "", None, 5, "AAPL  261016P00200000", " AAPL"]
KINDS = [None, "stock", "option", "bond", 5, "STOCK"]
STATUSES = ["SENT", "sent", " Sent ", "ſent", "DRY_RUN", "REFUSED", "rejected", "INELIGIBLE", "MISMATCH", "FILLED",
            "ACCEPTED", "UNKNOWN", "WORKING", "OPEN", "QUEUED", "PARTIAL", "CANCELED", "CANCELLED", "EXPIRED",
            "triggered?", "", None, 5, True, 1.5, ["SENT"], {"a": 1}, 0, "ß"]
REASONS = ["not eligible", "", None, 0, "é\n", 5, [], "x'y", {"k": None}, 1.5e-7, 10 ** 20, 1e22, True]
NUMS = ["2", "2.5", "0", "-1", "1e2", "1E+2", "NaN", "Infinity", "x", "", "  3 ", "٣", "1_0", "0x1", 1, 2.0, None, True,
        10 ** 30, "1.00", "-0", "9" * 30, "0.0000001", [1], {"a": 1}]
PRICES = [None, "", "1.25", "0", "-1", "x", 1.5, 0, " 2 ", "NaN", "1e3", "٣", "1" * 30, False]
OIDS = ["5403527317", "", "12x", 5403527317, None, "٣", "²", "½", "0", "00", " 1", True]


def sample(rng, pool):
    return rng.choice(pool)


def place_raw(rng):
    if rng.random() < 0.15:
        return rng.choice([None, "x", 5, [], [1], 1.5, True, "SENT", {}, ""])
    raw = {}
    for key, pool in (("status", STATUSES), ("reason", REASONS), ("order_id", OIDS + ["5403527317"] * 3),
                      ("book_status", STATUSES + ["WORKING", "UNKNOWN"]), ("note", REASONS), ("echo", [{}, None])):
        if rng.random() < 0.7:
            raw[key] = sample(rng, pool)
    return raw


def ack_json(a: VenueAck) -> dict:
    return {"status": a.status, "message": a.message}


def raw_edge_rows() -> list:
    rows = [None, "x", 5, [], {}, True, 1.5, float("nan"), float("inf")]
    for s in STATUSES:
        for extra in ({}, {"reason": "r"}, {"order_id": "5403527317", "book_status": "WORKING"},
                      {"order_id": "5403527317", "book_status": "UNKNOWN"}, {"order_id": "5403527317"},
                      {"note": "n", "order_id": "9"}, {"order_id": 7, "book_status": "w"}):
            rows.append({"status": s, **extra})
    return rows


def test_p5_t2_place_and_cancel_lockstep() -> None:
    rng = random.Random(5003)
    rows = raw_edge_rows() + [place_raw(rng) for _ in range(3000)]
    for n, raw in enumerate(rows):
        step("place_result", (n, raw), lambda: ack_json(FN.normalize_place_result(raw, "k", T)),
             lambda: door("place_result", {"raw": raw}))
        step("cancel_result", (n, raw), lambda: ack_json(FN.normalize_cancel_result(raw, "k", T)),
             lambda: door("cancel_result", {"raw": raw}))
        step("placed_order_id", (n, raw), lambda: {"order_id": FN.placed_order_id(raw)},
             lambda: door("placed_order_id", {"raw": raw}))
    said = {FN.normalize_place_result(r, "k", T).message.split(":")[0].split(" ")[0] for r in rows}
    assert {"unreadable", "unknown", "venue", "dry", "sent;"} <= said, said
    assert {"ok"} == set(TALLY["place_result"]) and sum(TALLY["place_result"].values()) >= 3000
    assert {"ok"} == set(TALLY["cancel_result"])
    assert {"ok"} == set(TALLY["placed_order_id"])


class Boom(Exception):
    pass


class RefusedSub(FT.TransportRefused):
    pass


def test_p5_t2_exceptions_lockstep() -> None:
    texts = ["echo mismatch", "", "é\n'q'", "key used", "x" * 200]
    excs = []
    for text in texts:
        excs += [FT.TransportRefused(text), RefusedSub(text), FT.TransportReplay(text), TimeoutError(text),
                 ValueError(text), Boom(text), KeyError(text), OSError(2, text), FT.TransportUnavailable(text)]
    excs += [Boom(), Boom(1, 2), KeyError("a"), RuntimeError(None)]
    for exc in excs:
        kind = "refused" if isinstance(exc, FT.TransportRefused) else "replay" if isinstance(exc, FT.TransportReplay) else "other"
        e = {"class": kind, "type": type(exc).__name__, "text": str(exc)}
        step("place_exception", repr(exc), lambda: ack_json(FN.normalize_place_exception(exc, "k", T)),
             lambda: door("place_exception", {"exc": e}))
        step("cancel_exception", repr(exc), lambda: ack_json(FN.normalize_cancel_exception(exc, "k", T)),
             lambda: door("cancel_exception", {"exc": e}))
    assert {"ok"} == set(TALLY["place_exception"]) and sum(TALLY["place_exception"].values()) >= 45
    assert sum(TALLY["cancel_exception"].values()) >= 45


def row_json(w) -> dict:
    return {"instrument": wire(w.instrument), "side": w.side.value, "quantity": str(w.quantity),
            "filled": str(w.filled), "order_type": w.order_type.value,
            "limit_price": None if w.limit_price is None else str(w.limit_price), "state": w.state.value}


def working_raw(rng):
    raw = {}
    if rng.random() < 0.6:
        raw["symbol"] = sample(rng, OCCS)
    else:
        raw["kind"] = sample(rng, KINDS)
        raw["symbol"] = sample(rng, STOCKS + OCCS)
    for key, pool in (("side", SIDES + ["BUY", "SELL", " sell "] * 2), ("order_type", TYPES + ["MKT", "LMT"] * 2),
                      ("quantity", NUMS + ["2", "3", "100"] * 3), ("filled", NUMS + ["0", "1"] * 3),
                      ("limit_price", PRICES), ("status", STATUSES + ["WORKING", "PARTIAL", "FILLED"] * 2)):
        if rng.random() < 0.85:
            raw[key] = sample(rng, pool)
    return raw


def test_p5_t2_working_order_lockstep() -> None:
    rng = random.Random(5004)
    rows = [working_raw(rng) for _ in range(6000)]
    good = {"symbol": OCCS[0], "side": "SELL", "quantity": "2", "filled": "0", "order_type": "LMT",
            "limit_price": "2.00", "status": "WORKING"}
    for key, pool in (("symbol", OCCS), ("side", SIDES), ("order_type", TYPES), ("quantity", NUMS),
                      ("filled", NUMS), ("limit_price", PRICES), ("status", STATUSES)):
        rows += [dict(good, **{key: v}) for v in pool]
    stock = {"kind": "stock", "symbol": "AAPL", "side": "BUY", "quantity": "100", "filled": "0", "order_type": "LMT",
             "limit_price": "150.25", "status": "WORKING"}
    for key, pool in (("symbol", STOCKS), ("kind", KINDS), ("quantity", NUMS), ("filled", NUMS)):
        rows += [dict(stock, **{key: v}) for v in pool]
    rows += [dict(good, quantity="1", filled="1"), dict(good, quantity="1", filled="1.5"),
             {k: v for k, v in good.items() if k != "filled"}, {k: v for k, v in good.items() if k != "limit_price"}]
    for n, raw in enumerate(rows):
        step("working_order", (n, raw), lambda: row_json(FN.normalize_working_order(raw)),
             lambda: door("working_order", {"raw": raw}))
    settle("working_order", 6000, [
        "NormalizeError: working order side", "NormalizeError: working order type",
        "NormalizeError: quantity must be a decimal", "NormalizeError: quantity is not a number",
        "NormalizeError: quantity must be finite", "NormalizeError: working order quantity",
        "NormalizeError: not a mirrored option symbol", "NormalizeError: not a mirrored stock symbol",
        "NormalizeError: row kind", "NormalizeError: stock quantity", "NormalizeError: stock filled"])


def test_p5_t2_book_state_lockstep() -> None:
    for s in STATUSES + [" working ", "Canceled", [], 0.0, " open ", "OPEN\t"]:
        step("book_state", s, lambda: {"state": FN.book_state(s).value}, lambda: door("book_state", {"status": s}))
    assert sum(TALLY["book_state"].values()) >= 30 and set(TALLY["book_state"]) == {"ok"}


def fill_raw(rng):
    raw = {}
    for key, pool in (("order_id", OIDS + ["5403527317"] * 4), ("filled", NUMS + ["0", "1", "2"] * 3),
                      ("avg_price", PRICES + ["1.05"] * 3), ("status", STATUSES + ["FILLED", "WORKING"] * 3)):
        if rng.random() < 0.88:
            raw[key] = sample(rng, pool)
    return raw


def test_p5_t2_order_fill_lockstep() -> None:
    rng = random.Random(5005)
    rows = [fill_raw(rng) for _ in range(6000)]
    good = {"order_id": "5403527317", "filled": "1", "avg_price": "1.05", "status": "FILLED"}
    for key, pool in (("order_id", OIDS), ("filled", NUMS), ("avg_price", PRICES), ("status", STATUSES)):
        rows += [dict(good, **{key: v}) for v in pool]
    rows += [dict(good, filled="0", status="FILLED"), dict(good, filled="0", avg_price=None, status="WORKING"),
             dict(good, filled="0", avg_price="9", status="WORKING"), dict(good, filled="2.0")]
    for n, raw in enumerate(rows):
        step("order_fill", (n, raw),
             lambda: (lambda f: {"order_id": f.order_id, "filled": str(f.filled),
                                 "avg_price": None if f.avg_price is None else str(f.avg_price),
                                 "state": f.state.value})(FN.normalize_order_fill(raw)),
             lambda: door("order_fill", {"raw": raw}))
    settle("order_fill", 6000, [
        "NormalizeError: order fill row names", "NormalizeError: filled must be a decimal",
        "NormalizeError: filled is not a number", "NormalizeError: filled must be finite", "not a whole non-negative",
        "with no positive average price", "reads FILLED with nothing filled", "NormalizeError: avg_price"])


def position_raw(rng):
    raw = {}
    if rng.random() < 0.6:
        raw["symbol"] = sample(rng, OCCS)
    else:
        raw["kind"] = sample(rng, KINDS)
        raw["symbol"] = sample(rng, STOCKS + OCCS)
    for key, pool in (("quantity", NUMS + ["-1", "100"] * 3), ("avg_price", NUMS + ["2.10", "150.25"] * 3)):
        if rng.random() < 0.9:
            raw[key] = sample(rng, pool)
    return raw


def test_p5_t2_position_lockstep() -> None:
    rng = random.Random(5006)
    rows = [position_raw(rng) for _ in range(6000)]
    good = {"symbol": OCCS[0], "quantity": "-1", "avg_price": "2.10"}
    for key, pool in (("symbol", OCCS), ("quantity", NUMS), ("avg_price", NUMS)):
        rows += [dict(good, **{key: v}) for v in pool]
    stock = {"kind": "stock", "symbol": "AAPL", "quantity": "100", "avg_price": "150.25"}
    for key, pool in (("symbol", STOCKS), ("kind", KINDS), ("quantity", NUMS), ("avg_price", NUMS + ["-0.01"])):
        rows += [dict(stock, **{key: v}) for v in pool]
    for n, raw in enumerate(rows):
        step("position", (n, raw),
             lambda: (lambda p: {"instrument": wire(p.instrument), "quantity": str(p.quantity),
                                 "avg_price": str(p.avg_price)})(FN.normalize_position(raw, T)),
             lambda: door("position", {"raw": raw}))
    settle("position", 6000, [
        "NormalizeError: quantity must be a decimal", "NormalizeError: not a mirrored option symbol",
        "NormalizeError: not a mirrored stock symbol", "NormalizeError: row kind",
        "NormalizeError: stock quantity", "NormalizeError: stock avg_price"])


def test_p5_text_tables_are_pythons() -> None:
    """The generated Unicode tables (pytables.rs) against the running interpreter, every code point."""
    codes = [c for c in range(0x110000) if not 0xD800 <= c <= 0xDFFF]
    for start in range(0, len(codes), 120_000):
        chars = "".join(map(chr, codes[start:start + 120_000]))
        got = door("text_probe", {"chars": chars})
        want = [[c.isdigit(), c.isprintable(), c.isspace()] for c in chars]
        assert got == want, [(hex(ord(c)), g, w) for c, g, w in zip(chars, got, want) if g != w][:5]


# -- T3 slippage -----------------------------------------------------------------------------


def fill_obj(fid, oid, inst, side, qty, price, env="sim") -> Fill:
    return Fill(fill_id=fid, order_id=oid, account_id="OPT_CSP", instrument=inst, quantity=D(qty),
                price=D(price), venue_env=env, filled_at=T, side=side)


def sfill_doc(f) -> dict:
    return {"fill_id": f.fill_id, "order_id": f.order_id, "instrument": wire(f.instrument), "side": f.side.value,
            "quantity": str(f.quantity), "price": str(f.price)}


def report_json(r) -> dict:
    dec = lambda d: None if d is None else str(d)  # noqa: E731
    return {"venue": r.venue, "as_of": r.as_of.isoformat(),
            "pairs": [{"order_id": p.order_id, "instrument": p.instrument, "side": p.side.value,
                       "quantity": str(p.quantity), "sim_price": str(p.sim_price),
                       "venue_price": str(p.venue_price), "slippage_points": str(p.slippage_points),
                       "slippage_bps": dec(p.slippage_bps)} for p in r.pairs],
            "unmatched_sim": list(r.unmatched_sim), "unmatched_venue": list(r.unmatched_venue),
            "refused": [list(x) for x in r.refused], "mean_slippage_bps": dec(r.mean_slippage_bps)}


PRICE_POOL = ["1.00", "3.00", "3.10", "2.95", "0.05", "100.5", "0.0001", "1234.5678", "2.10", "1E+1"]
QTY_POOL = ["1", "2", "3", "0.5", "10", "100", "1.5", "7"]
SLIP_INSTS = [P200, P195, AAPL, C200]


def report_step(label, venue, sims, venues) -> None:
    step("slippage_report", label,
         lambda: report_json(FS.slippage_report(venue, T, sims, venues)),
         lambda: door("slippage_report", {"venue": venue, "as_of": T.isoformat(),
                                          "sim_fills": [sfill_doc(f) for f in sims],
                                          "venue_fills": [sfill_doc(f) for f in venues]}))


def zero_price(f: Fill) -> Fill:
    object.__setattr__(f, "price", D(0))     # Fill forbids it; the report must not divide anyway
    return f


def test_p5_t3_slippage_report_lockstep() -> None:
    rng = random.Random(5007)
    B, S = Side.BUY, Side.SELL
    for side in (B, S):                               # the grid: one order, every price pairing
        for sp in PRICE_POOL:
            for vp in PRICE_POOL:
                for q in ("1", "2", "0.5"):
                    report_step((side, sp, vp, q), "TosPaperBroker",
                                [fill_obj("s", "o", P200, side, q, sp)], [fill_obj("v", "o", P200, side, q, vp, "paper")])
    for venue in ("tos", ""):                         # empty venue refuses the whole report, last
        report_step(("venue", venue), venue, [fill_obj("s", "o", P200, B, "1", "1")], [])
        report_step(("venue-empty", venue), venue, [], [])
    for sp_zero in (True, False):                     # zero sim price: no bps, left out of the mean
        sim = fill_obj("s", "z", P200, B, "1", "1")
        if sp_zero:
            zero_price(sim)
        report_step(("zero", sp_zero), "tos", [sim, fill_obj("s2", "a", P200, B, "1", "1")],
                    [fill_obj("v", "z", P200, B, "1", "0.05", "paper"), fill_obj("v2", "a", P200, B, "1", "1.01", "paper")])
    zeroq = fill_obj("s", "q", P200, B, "1", "1")     # a zero quantity cannot pair (and cannot be a vwap)
    object.__setattr__(zeroq, "quantity", D(0))
    report_step("zero-qty", "tos", [zeroq], [zeroq])
    dup = fill_obj("f-1", "o", P200, B, "1", "3")     # duplicate fill ids, sim then venue, across orders
    report_step("dup-sim", "tos", [dup, dup, fill_obj("g", "p", P200, B, "1", "3")],
                [fill_obj("v", "o", P200, B, "2", "3", "paper"), fill_obj("w", "p", P200, B, "1", "3", "paper")])
    vd = fill_obj("v-1", "o", P200, B, "1", "3", "paper")
    report_step("dup-venue", "tos", [fill_obj("s", "o", P200, B, "2", "3")], [vd, vd])
    report_step("dup-across-orders", "tos", [fill_obj("same", "a", P200, B, "1", "1"), fill_obj("same", "b", P200, B, "1", "1")],
                [fill_obj("v1", "a", P200, B, "1", "1", "paper"), fill_obj("v2", "b", P200, B, "1", "1", "paper")])
    for n in range(2500):                             # seeded
        orders = [f"o-{rng.randrange(6)}" for _ in range(rng.randrange(0, 5))]
        sims, venues = [], []
        k = 0
        for env, bucket in (("sim", sims), ("paper", venues)):
            for oid in orders + [f"o-{rng.randrange(6)}" for _ in range(rng.randrange(0, 3))]:
                if rng.random() < 0.85:
                    k += 1
                    fid = f"{env}{rng.randrange(1, 4) if rng.random() < 0.12 else k}"
                    f = fill_obj(fid, oid, rng.choice(SLIP_INSTS[:2] if rng.random() < 0.9 else SLIP_INSTS),
                                 rng.choice([B, B, B, S]) if rng.random() < 0.9 else B, rng.choice(QTY_POOL),
                                 rng.choice(PRICE_POOL), env)
                    if rng.random() < 0.04:
                        zero_price(f)
                    bucket.append(f)
        # make most orders pairable so pairs and means are exercised
        if rng.random() < 0.6:
            venues = [copy.copy(f) for f in sims if rng.random() < 0.9] + venues[:1]
            for v in venues[:-1] if venues else ():
                object.__setattr__(v, "venue_env", "paper")
                object.__setattr__(v, "fill_id", "v" + v.fill_id)
        report_step(n, rng.choice(["tos", "TosPaperBroker", "é"]), sims, venues)
    settle("slippage_report", 2600, ["SlippageError: venue must be", "InvalidOperation"])


def test_p5_t3_report_per_order_refusal_families() -> None:
    """Each per-order refusal reason, seen refusing and the report still standing (not a raise)."""
    B, S = Side.BUY, Side.SELL
    cases = {
        "sim: duplicate fill id": ([fill_obj("d", "o", P200, B, "1", "1")] * 2, [fill_obj("v", "o", P200, B, "2", "1", "paper")]),
        "venue: duplicate fill id": ([fill_obj("s", "o", P200, B, "2", "1")], [fill_obj("d", "o", P200, B, "1", "1", "paper")] * 2),
        "side mismatch": ([fill_obj("s", "o", P200, B, "1", "1")], [fill_obj("v", "o", P200, S, "1", "1", "paper")]),
        "instrument mismatch": ([fill_obj("s", "o", P200, B, "1", "1")], [fill_obj("v", "o", P195, B, "1", "1", "paper")]),
        "mirror drifted": ([fill_obj("s", "o", P200, B, "1", "1")], [fill_obj("v", "o", P200, B, "2", "1", "paper")]),
    }
    for name, (sims, venues) in cases.items():
        a = outcome(lambda: report_json(FS.slippage_report("tos", T, sims, venues)))
        assert a[0] == "ok" and name in json.dumps(a[1]["refused"]), (name, a)
        report_step(name, "tos", sims, venues)
    ok = ([fill_obj("s", "o", P200, B, "1", "1")], [fill_obj("v", "o", P200, B, "1", "1", "paper")])
    assert outcome(lambda: report_json(FS.slippage_report("tos", T, *ok)))[1]["refused"] == []
    report_step("paired", "tos", *ok)
    report_step("empty-venue-name", "", *ok)
    assert any(k != "ok" and k[0] == "SlippageError" for k in TALLY["slippage_report"])


TICKET_QTYS = ["4", "3", "1", "1.5", "0.5", "2"]


def vfill(qty, *, fid="vf-1", side=Side.SELL, inst=P200, fee="0", order="tos:t1", price="2.05") -> VenueFill:
    return VenueFill(fid, order, inst, D(qty), D(price), T, side, fee=D(fee))


def ticket_obj(allocs, *, qty=None, side=Side.SELL, inst=P200, order="tos:t1") -> SimpleNamespace:
    allocations = tuple(VenueOrderAllocation(oid, f"ACC_{oid}", D(q)) for oid, q in allocs)
    total = qty if qty is not None else sum((a.quantity for a in allocations), D(0))
    return SimpleNamespace(venue_order_id=order, instrument=inst, side=side, quantity=D(total), allocations=allocations)


def alloc_doc(fill, ticket, prior) -> dict:
    return {"fill": {"venue_fill_id": fill.venue_fill_id, "venue_order_id": fill.venue_order_id,
                     "instrument": wire(fill.instrument), "side": fill.side.value, "quantity": str(fill.quantity),
                     "price": str(fill.price), "filled_at": fill.filled_at.isoformat(), "fee": str(fill.fee)},
            "ticket": {"venue_order_id": ticket.venue_order_id, "instrument": wire(ticket.instrument),
                       "side": ticket.side.value, "quantity": str(ticket.quantity),
                       "allocations": [{"strategy_order_id": a.strategy_order_id, "account_id": a.account_id,
                                        "quantity": str(a.quantity)} for a in ticket.allocations]},
            "already_filled": None if prior is None else {k: str(v) for k, v in prior.items()}}


def fills_json(fills) -> list:
    return [{"fill_id": f.fill_id, "order_id": f.order_id, "account_id": f.account_id, "quantity": str(f.quantity),
             "price": str(f.price), "venue_env": f.venue_env, "filled_at": f.filled_at.isoformat(),
             "side": f.side.value, "fee": str(f.fee), "venue_order_id": f.venue_order_id,
             "venue_execution_id": f.venue_execution_id} for f in fills]


def alloc_step(label, fill, ticket, prior) -> None:
    step("allocate_venue_fill", label,
         lambda: fills_json(FS.allocate_venue_fill(fill, ticket, already_filled=prior)),
         lambda: door("allocate_venue_fill", alloc_doc(fill, ticket, prior)))


def test_p5_t3_allocation_lockstep() -> None:
    rng = random.Random(5008)
    # the edge grid: fractional shares and contracts, residue, drift, fees that do not split to the cent
    shapes = [[("a", "1"), ("b", "3")], [("a", "1"), ("b", "1"), ("c", "1")], [("a", "2"), ("b", "1"), ("c", "2")],
              [("a", "0.5"), ("b", "1.5")], [("a", "1")], [("a", "1"), ("b", "2"), ("c", "3")],
              [("a", "100"), ("b", "250")], [("a", "0.3"), ("b", "0.3"), ("c", "0.4")]]
    fees = ["0", "0.01", "1.00", "1.01", "0.02", "7.77", "0.005", "13.337", "100"]
    for allocs in shapes:
        ticket = ticket_obj(allocs)
        total = sum((D(q) for _, q in allocs), D(0))
        for qty in {str(total), "1", "2", "0.5", "0.3", str(total / 2), str(total + 1), "3", "4"}:
            for fee in fees:
                alloc_step((allocs, qty, fee), vfill(qty, fee=fee), ticket, None)
        for prior in ({"a": allocs[0][1]}, {"a": "0"}, {"z": "1"}, {"a": "-1"}, {"a": str(D(allocs[0][1]) + 1)},
                      {allocs[-1][0]: allocs[-1][1]}, {}):
            for qty in ("1", "2", "0.5", str(total)):
                alloc_step((allocs, "prior", prior, qty), vfill(qty, fee="1.01"), ticket,
                           {k: D(v) for k, v in prior.items()})
    one = ticket_obj([("a", "1"), ("b", "3")])
    for label, fill in (("foreign", vfill("1", order="tos:other")), ("instrument", vfill("1", inst=P195)),
                        ("equity", vfill("1", inst=AAPL)), ("side", vfill("1", side=Side.BUY)),
                        ("price", vfill("4", price="0.0001")), ("big", vfill("4", fee="9999999.99", price="123456.789"))):
        alloc_step(label, fill, one, None)
    drift = ticket_obj([("a", "1"), ("b", "3")], qty="5")      # ticket quantity drifted from its allocations
    for qty in ("1", "2", "4", "5"):
        alloc_step(("drift", qty), vfill(qty), drift, None)
    drift = ticket_obj([("a", "1"), ("b", "3")], qty="2")
    for qty in ("1", "2", "3"):
        alloc_step(("drift-low", qty), vfill(qty), drift, {"a": D(1)} if qty == "1" else None)
    for n in range(2500):                                       # seeded
        k = rng.randrange(1, 5)
        allocs = [(f"o{i}", rng.choice(QTY_POOL)) for i in range(k)]
        ticket = ticket_obj(allocs, qty=rng.choice([None, None, None, rng.choice(TICKET_QTYS)]))
        prior = None
        if rng.random() < 0.6:
            prior = {f"o{i}": D(rng.choice(["0", "0", "1", "0.5", "2", "-1"])) for i in range(rng.randrange(0, k + 2))}
        fill = vfill(rng.choice(QTY_POOL), fee=rng.choice(fees), price=rng.choice(PRICE_POOL),
                     order=rng.choice(["tos:t1"] * 9 + ["x"]), side=rng.choice([Side.SELL] * 9 + [Side.BUY]),
                     inst=rng.choice([P200] * 9 + [P195]))
        alloc_step(n, fill, ticket, prior)
    settle("allocate_venue_fill", 2700, ["SlippageError: fill # is for", "instrument does not match",
                                         "SlippageError: fill # side", "takes the ticket to", "already_filled gives"])


# -- the existing pure-data vectors, through the Rust door ------------------------------------


def parametrized(fn):
    marks = [m for m in getattr(fn, "pytestmark", []) if m.name == "parametrize"]
    assert len(marks) <= 1, fn
    if not marks:
        return [()]
    names = [n.strip() for n in marks[0].args[0].split(",")]
    values = list(marks[0].args[1])
    return [(v,) if len(names) == 1 else tuple(v) for v in values]


def run_module_tests(module, only=None) -> int:
    ran = 0
    for name, fn in sorted(vars(module).items()):
        if not name.startswith("test_") or not inspect.isfunction(fn) or (only and name not in only):
            continue
        for args in parametrized(fn):
            fn(*args)
            ran += 1
    return ran


def instrument_of(w: dict):
    if w["kind"] == "equity":
        return Equity(w["symbol"])
    return OptionContract(underlying=w["underlying"], expiry=date.fromisoformat(w["expiry"]),
                          strike=D(w["strike"]), right=OptionRight(w["right"]), multiplier=w["multiplier"])


def test_p5_existing_normalize_vectors_through_the_door(monkeypatch) -> None:
    import test_tos_normalize as V

    def place(raw, vid, at):
        r = door("place_result", {"raw": raw})
        return VenueAck(vid, r["status"], at, r["message"])

    def cancel(raw, vid, at):
        r = door("cancel_result", {"raw": raw})
        return VenueAck(vid, r["status"], at, r["message"])

    def exc_doc(exc):
        kind = "refused" if isinstance(exc, V.TransportRefused) else "replay" if isinstance(exc, V.TransportReplay) else "other"
        return {"exc": {"class": kind, "type": type(exc).__name__, "text": str(exc)}}

    def place_exc(exc, vid, at):
        r = door("place_exception", exc_doc(exc))
        return VenueAck(vid, r["status"], at, r["message"])

    def cancel_exc(exc, vid, at):
        r = door("cancel_exception", exc_doc(exc))
        return VenueAck(vid, r["status"], at, r["message"])

    def working(raw):
        r = door("working_order", {"raw": raw})
        return V.WorkingOrder(instrument_of(r["instrument"]), Side(r["side"]), D(r["quantity"]), D(r["filled"]),
                              OrderType(r["order_type"]), None if r["limit_price"] is None else D(r["limit_price"]),
                              OrderState(r["state"]))

    def position(raw, at):
        r = door("position", {"raw": raw})
        return VenuePosition(instrument_of(r["instrument"]), D(r["quantity"]), D(r["avg_price"]), at)

    class DoorTicket(V.MirrorTicket):
        def __post_init__(self) -> None:
            door("ticket_validate", {"kind": "option", "side": self.side, "quantity": self.quantity,
                                     "order_type": self.order_type, "tif": self.tif,
                                     "limit_price": None if self.limit_price is None else str(self.limit_price)})

    monkeypatch.setattr(V, "normalize_place_result", place)
    monkeypatch.setattr(V, "normalize_cancel_result", cancel)
    monkeypatch.setattr(V, "normalize_place_exception", place_exc)
    monkeypatch.setattr(V, "normalize_cancel_exception", cancel_exc)
    monkeypatch.setattr(V, "normalize_working_order", working)
    monkeypatch.setattr(V, "normalize_position", position)
    monkeypatch.setattr(V, "placed_order_id", lambda raw: door("placed_order_id", {"raw": raw})["order_id"])
    monkeypatch.setattr(V, "MirrorTicket", DoorTicket)
    assert run_module_tests(V) == 52


def test_p5_existing_slippage_vectors_through_the_door(monkeypatch) -> None:
    import test_tos_slippage as V

    def report(venue, as_of, sims, venues):
        r = door("slippage_report", {"venue": venue, "as_of": as_of.isoformat(),
                                     "sim_fills": [sfill_doc(f) for f in sims],
                                     "venue_fills": [sfill_doc(f) for f in venues]})
        pairs = tuple(V.SlippagePair(p["order_id"], p["instrument"], Side(p["side"]), D(p["quantity"]),
                                     D(p["sim_price"]), D(p["venue_price"]), D(p["slippage_points"]),
                                     None if p["slippage_bps"] is None else D(p["slippage_bps"])) for p in r["pairs"])
        return V.SlippageReport(r["venue"], datetime.fromisoformat(r["as_of"]), pairs, tuple(r["unmatched_sim"]),
                                tuple(r["unmatched_venue"]), tuple(tuple(x) for x in r["refused"]),
                                None if r["mean_slippage_bps"] is None else D(r["mean_slippage_bps"]))

    def allocate(fill, ticket, *, already_filled=None):
        out = door("allocate_venue_fill", alloc_doc(fill, ticket, already_filled))
        return tuple(Fill(fill_id=f["fill_id"], order_id=f["order_id"], account_id=f["account_id"],
                          instrument=fill.instrument, quantity=D(f["quantity"]), price=D(f["price"]),
                          venue_env=f["venue_env"], filled_at=fill.filled_at, side=Side(f["side"]), fee=D(f["fee"]),
                          venue_order_id=f["venue_order_id"], venue_execution_id=f["venue_execution_id"])
                     for f in out)

    monkeypatch.setattr(V, "slippage_report", report)
    monkeypatch.setattr(V, "allocate_venue_fill", allocate)
    # the pair/report dataclass tests build their own objects and never reach the door
    assert run_module_tests(V) >= 15


def test_p5_existing_ticket_vectors_through_the_door(monkeypatch) -> None:
    import test_tos_mirror as M
    import test_tos_stock as K

    def ticket_cls(real, kind):
        class Door(real):
            def __post_init__(self) -> None:
                f = {k: getattr(self, k) for k in ("side", "quantity", "order_type", "tif", "limit_price", "symbol",
                                                   "ratio", "price_effect") if hasattr(self, k)}
                if "limit_price" in f and f["limit_price"] is not None:
                    f["limit_price"] = str(f["limit_price"])
                if kind == "combo":
                    f["legs"] = [{"side": l.side, "ratio": l.ratio} for l in self.legs]
                door("ticket_validate", {"kind": kind, **f})
        Door.__name__ = real.__name__
        return Door

    def ticket_for(module):
        stock, single, vertical, leg = (ticket_cls(module.MirrorStockTicket, "stock") if hasattr(module, "MirrorStockTicket") else None,
                                        ticket_cls(module.MirrorTicket, "option"),
                                        ticket_cls(module.MirrorComboTicket, "combo") if hasattr(module, "MirrorComboTicket") else None,
                                        ticket_cls(module.MirrorComboLeg, "leg") if hasattr(module, "MirrorComboLeg") else None)

        def build(order: VenueOrder):
            t = door("ticket_for", order_doc(order))
            lim = None if t["limit_price"] is None else D(t["limit_price"])
            if t["kind"] == "stock":
                return stock(symbol=t["symbol"], side=t["side"], quantity=t["quantity"], order_type=t["order_type"],
                             limit_price=lim, tif=t["tif"])
            if t["kind"] == "option":
                return single(symbol=t["symbol"], side=t["side"], quantity=t["quantity"], order_type=t["order_type"],
                              limit_price=lim, tif=t["tif"], underlying=t["underlying"],
                              expiry=date.fromisoformat(t["expiry"]), strike=D(t["strike"]), right=t["right"])
            legs = tuple(leg(symbol=l["symbol"], side=l["side"], ratio=l["ratio"], expiry=date.fromisoformat(l["expiry"]),
                             strike=D(l["strike"]), right=l["right"]) for l in t["legs"])
            return vertical(underlying=t["underlying"], legs=legs, quantity=t["quantity"], order_type="LMT",
                            limit_price=lim, price_effect=t["price_effect"], tif=t["tif"])
        return build, stock, single, vertical, leg

    build, stock, single, vertical, leg = ticket_for(M)
    monkeypatch.setattr(M, "ticket_for", build)
    monkeypatch.setattr(M, "MirrorTicket", single)
    monkeypatch.setattr(M, "MirrorComboTicket", vertical)
    monkeypatch.setattr(M, "MirrorComboLeg", leg)
    ran = run_module_tests(M, only={
        "test_a_vertical_becomes_one_combo_ticket_with_an_explicit_price_effect",
        "test_a_single_leg_ticket_is_unchanged", "test_a_combo_the_venue_cannot_express_is_unsupported",
        "test_a_combo_ticket_validates_itself", "test_a_combo_leg_validates_itself"})
    assert ran >= 15
    build, stock, single, vertical, leg = ticket_for(K)
    monkeypatch.setattr(K, "ticket_for", build)
    monkeypatch.setattr(K, "MirrorStockTicket", stock)
    monkeypatch.setattr(K, "MirrorTicket", single)
    ran = run_module_tests(K, only={
        "test_a_valid_stock_ticket_constructs_and_cannot_be_edited_afterwards",
        "test_a_stock_ticket_the_venue_could_misread_is_refused_at_construction",
        "test_an_equity_order_becomes_a_stock_ticket_exactly", "test_an_option_order_is_still_an_option_ticket",
        "test_a_stock_order_the_venue_cannot_express_is_unsupported_never_approximated"})
    assert ran >= 19


# -- T4 reconcile ----------------------------------------------------------------------------

from frozen_p5 import reconcile as FR  # noqa: E402
from trade_engine.ledger.events import VenueReconcile  # noqa: E402

VENUE = "D-00000001"
SEEN: dict[str, Counter] = {}


def seen(tally: str, key) -> None:
    SEEN.setdefault(tally, Counter())[key] += 1


def pos(instrument, quantity) -> VenuePosition:
    return VenuePosition(instrument, D(quantity), D("2.00"), T)


def pos_docs(positions) -> list:
    return [{"instrument": wire(p.instrument), "quantity": str(p.quantity)} for p in positions]


def pairs_doc(mapping) -> list:
    return [[wire(k), str(v)] for k, v in mapping.items()]


def work(instrument, side, qty="1", filled="0", state=OrderState.ACCEPTED, limit="2.00", order_type=None):
    ot = order_type or (OrderType.LIMIT if limit else OrderType.MARKET)
    return FR.WorkingOrder(instrument, side, D(qty), D(filled), ot, D(limit) if limit else None, state)


def work_doc(rows) -> list:
    return [row_json(r) for r in rows]


def rec_json(e) -> dict:
    return {"venue": e.venue, "as_of": e.as_of.isoformat(), "reconciled": e.reconciled, "drift": list(e.drift),
            "note": e.note}


RCONTRACTS = [P200, P195, C200, AAPL, opt(strike="200.0"), opt(multiplier=10), opt(strike="190"), opt(strike="1234.567")]
RQTYS = ["0", "1", "-1", "2", "-2", "3", "1.0", "-1.0", "0.50", "100", "-100", "1E+1", "-0"]
STATES = list(OrderState)
LIVE = [OrderState.SUBMITTED, OrderState.ACCEPTED, OrderState.PARTIALLY_FILLED]


def reconcile_case(rng):
    """A mirror book, and what the venue shows of it: right, drifted, missing, unknown, empty."""
    contracts = rng.sample(RCONTRACTS, rng.randint(0, 4))
    expected = {c: D(rng.choice(RQTYS)) for c in contracts}
    positions, working = [], []
    mode = rng.choice(["clean", "clean", "drift", "missing", "extra", "unknown", "empty", "split", "wild"])
    for c, want in expected.items():
        have = want
        if mode == "drift" and rng.random() < 0.5:
            have = want + rng.choice([D(1), D(-1), D("0.5")])
        if mode == "missing" and rng.random() < 0.5:
            have = D(0)
        if mode == "empty":
            have = D(0)
        if rng.random() < 0.4:                      # part of it rests on the book
            rest = D(rng.choice(["1", "2", "-1", "-2"]))
            have = have - rest
            side = Side.BUY if rest > 0 else Side.SELL
            filled = D(rng.choice(["0", "0", "1"]))
            working.append(FR.WorkingOrder(c, side, abs(rest) + filled, filled, OrderType.LIMIT, D("2.00"),
                                           rng.choice(LIVE)))
        if have != 0 or rng.random() < 0.3:
            if mode == "split" and rng.random() < 0.7:
                positions += [pos(c, str(have / 2)), pos(c, str(have - have / 2))]
            else:
                positions.append(pos(c, str(have)))
    if mode == "extra":
        positions.append(pos(rng.choice(RCONTRACTS), rng.choice(["1", "-1", "5"])))
    if mode in ("unknown", "wild"):
        working.append(work(rng.choice(RCONTRACTS), rng.choice(list(Side)), state=OrderState.PENDING_UNKNOWN))
    if mode == "wild":
        for _ in range(rng.randint(1, 4)):
            working.append(work(rng.choice(RCONTRACTS), rng.choice(list(Side)), qty=rng.choice(["1", "2", "3"]),
                                filled=rng.choice(["0", "1", "2"]), state=rng.choice(STATES)))
        for _ in range(rng.randint(0, 3)):
            positions.append(pos(rng.choice(RCONTRACTS), rng.choice(RQTYS)))
    rng.shuffle(working)
    return expected, positions, working


def check_reconcile(label, venue, as_of, expected, positions, working) -> None:
    got = step("reconcile", label,
               lambda: rec_json(FR.reconcile(venue, as_of, expected, positions, working)),
               lambda: door("reconcile", {"venue": venue, "as_of": as_of.isoformat(), "expected": pairs_doc(expected),
                                          "positions": pos_docs(positions), "working": work_doc(working)}))
    if got[0] == "ok":
        seen("reconcile", (got[1]["reconciled"], bool(got[1]["drift"])))
    step("position_book", label, lambda: pairs_doc(FR.position_book(positions)),
         lambda: door("position_book", {"positions": pos_docs(positions)}))


def test_p5_t4_reconcile_lockstep() -> None:
    rng = random.Random(5101)
    naive = datetime(2026, 9, 24, 20, 0)
    # the edge grid: an empty day ({} from the web driver), then each way a contract can drift
    check_reconcile("empty-clean", VENUE, T, {}, [], [])
    check_reconcile("empty-day", VENUE, T, {P200: D(-1)}, [], [])
    check_reconcile("equal-strike-spellings", VENUE, T, {P200: D(-1)}, [pos(opt(strike="200.0"), "-1")], [])
    check_reconcile("one-share-drift", VENUE, T, {AAPL: D(100)}, [pos(AAPL, "99")], [])
    check_reconcile("one-contract-drift", VENUE, T, {P200: D(-2)}, [pos(P200, "-1")], [])
    check_reconcile("sell-rest", VENUE, T, {P200: D(-2)}, [pos(P200, "-1")], [work(P200, Side.SELL, "1")])
    check_reconcile("buy-rest-wrong-way", VENUE, T, {P200: D(-2)}, [pos(P200, "-1")], [work(P200, Side.BUY, "1")])
    check_reconcile("partial-rest", VENUE, T, {P200: D(-2)}, [pos(P200, "-1")],
                    [work(P200, Side.SELL, "2", "1", OrderState.PARTIALLY_FILLED)])
    check_reconcile("unknown-row", VENUE, T, {P200: D(-1)}, [pos(P200, "-1")],
                    [work(P200, Side.SELL, state=OrderState.PENDING_UNKNOWN)])
    check_reconcile("unknown-alone", VENUE, T, {}, [], [work(P200, Side.SELL, state=OrderState.PENDING_UNKNOWN)])
    for state in STATES:
        check_reconcile(("state", state), VENUE, T, {P200: D(-1)}, [], [work(P200, Side.SELL, state=state)])
    check_reconcile("empty-venue", "", T, {}, [], [])
    check_reconcile("empty-venue-drift", "", T, {P200: D(-1)}, [], [])
    check_reconcile("naive", VENUE, naive, {}, [], [])
    check_reconcile("naive-drift", VENUE, naive, {P200: D(-1)}, [], [])
    check_reconcile("offset", "D-2", datetime(2026, 9, 24, 15, 0, tzinfo=timezone(timedelta(hours=-5))), {}, [], [])
    for n in range(2500):                                   # seeded
        expected, positions, working = reconcile_case(rng)
        venue = "" if rng.random() < 0.01 else rng.choice([VENUE, "D-2"])
        check_reconcile(n, venue, naive if rng.random() < 0.01 else T, expected, positions, working)
    c = SEEN["reconcile"]
    assert c[(True, False)] > 100 and c[(False, True)] > 300, c
    settle("reconcile", 2500, ["EventPayloadError: VenueReconcile.venue must be non-empty",
                               "EventPayloadError: VenueReconcile.as_of must be timezone-aware"])
    assert sum(TALLY["position_book"].values()) >= 2500


def check_unreadable(label, venue, as_of, contracts, why) -> None:
    step("unreadable", label, lambda: rec_json(FR.unreadable(venue, as_of, contracts, why)),
         lambda: door("unreadable", {"venue": venue, "as_of": as_of.isoformat(),
                                     "contracts": [wire(c) for c in contracts], "why": why}))


def test_p5_t4_unreadable_lockstep() -> None:
    rng = random.Random(5102)
    check_unreadable("none", VENUE, T, [], "JAB down")
    check_unreadable("dupes", VENUE, T, [P200, P195, P200, opt(strike="200.0")], "x")
    check_unreadable("no-venue", "", T, [P200], "x")
    check_unreadable("naive", VENUE, datetime(2026, 9, 24), [], "x")
    for n in range(400):
        check_unreadable(n, rng.choice([VENUE, "D-2"]), T, [rng.choice(RCONTRACTS) for _ in range(rng.randint(0, 5))],
                         rng.choice(["JAB down", "", "é\n", "timeout after 30s", "x" * 80]))
    settle("unreadable", 400, ["EventPayloadError: VenueReconcile.venue must be non-empty",
                               "EventPayloadError: VenueReconcile.as_of must be timezone-aware"])


def vorder(instrument, side, qty, limit, order_type=None, stop=None):
    ot = order_type or (OrderType.LIMIT if limit else OrderType.MARKET)
    return VenueOrder(venue_order_id="tos:abc", instrument=instrument, order_type=ot, side=side, quantity=D(qty),
                      submitted_at=T, limit_price=D(limit) if limit else None,
                      stop_price=D(stop) if stop else None,
                      allocations=(VenueOrderAllocation("so-1", "OPT", D(qty)),))


def ticket_doc(t) -> dict:
    return {"instrument": wire(t.instrument), "order_type": t.order_type.value, "side": t.side.value,
            "quantity": str(t.quantity), "limit_price": None if t.limit_price is None else str(t.limit_price)}


VERTICALS = [
    combo((P200, 1, Side.SELL), (P195, 1, Side.BUY)),
    combo((P200, 1, Side.BUY), (P195, 1, Side.SELL)),
    combo((C200, 1, Side.SELL), (opt(strike="205", right="C"), 1, Side.BUY)),
    combo((AAPL, 100, Side.BUY), (C200, 1, Side.SELL)),
    combo((P200, 2, Side.SELL), (P195, 2, Side.BUY)),
]


def confirm_case(rng):
    if rng.random() < 0.5:
        ticket_instrument = rng.choice(VERTICALS)
    else:
        ticket_instrument = rng.choice([P200, P195, C200, AAPL, opt(strike="200.0")])
    side = rng.choice(list(Side))
    qty = rng.choice(["1", "1", "2", "3", "100", "1.0"])
    if isinstance(ticket_instrument, Combo):
        order_type, limit = rng.choice([(OrderType.LIMIT, "1.50"), (OrderType.LIMIT, "0.8"), (OrderType.MARKET, None)])
    else:
        order_type, limit = rng.choice([(OrderType.LIMIT, "2.00"), (OrderType.MARKET, None), (OrderType.LIMIT, "2.0")])
    stop = None
    if rng.random() < 0.03:
        order_type, limit, stop = OrderType.STOP, None, "1.0"
    ticket = vorder(ticket_instrument, side, qty, limit, order_type, stop)
    rows, positions, before = [], [], {}
    legs = ([(l.contract, l.side, D(qty) * l.ratio) for l in ticket_instrument.legs]
            if isinstance(ticket_instrument, Combo) else [(ticket_instrument, side, D(qty))])
    for contract, leg_side, leg_qty in legs:                # the rows the ticket should have made
        if rng.random() < 0.85:
            row_qty = leg_qty + rng.choice([0, 0, 0, 0, 1, -1])
            row_limit = ticket.limit_price if (isinstance(ticket_instrument, Combo) or order_type is OrderType.LIMIT) else None
            row_ot = rng.choice([OrderType.LIMIT, OrderType.MARKET]) if rng.random() < 0.05 else (
                OrderType.LIMIT if row_limit is not None else OrderType.MARKET)
            if row_qty > 0:
                rows.append(FR.WorkingOrder(contract, leg_side if rng.random() < 0.95 else
                                            (Side.BUY if leg_side is Side.SELL else Side.SELL), row_qty, D(0),
                                            row_ot, row_limit, rng.choice(STATES)))
    for _ in range(rng.choice([0, 0, 1, 2])):
        rows.append(work(rng.choice(RCONTRACTS), rng.choice(list(Side)), state=rng.choice(STATES)))
    if rng.random() < 0.3 and rows:                          # a twin: two identical tickets, one row
        rows.append(rng.choice(rows))
    rng.shuffle(rows)
    if rng.random() < 0.5:                                   # positions: moved by the ticket, or not
        for contract, leg_side, leg_qty in legs:
            was = D(rng.choice(["0", "5", "-3", "100"]))
            before[contract] = was
            moved = leg_qty if leg_side is Side.BUY else -leg_qty
            if rng.random() < 0.3:
                moved = moved + rng.choice([D(1), D(-1)])
            if rng.random() < 0.15:
                moved = D(0)
            positions.append(pos(contract, str(was + moved)))
            if rng.random() < 0.2:
                positions.append(pos(contract, "0"))
    claimed = {i for i in range(len(rows)) if rng.random() < 0.15}
    return ticket, before, positions, rows, claimed


def check_confirm(label, ticket, before, positions, rows, claimed) -> None:
    def oracle():
        mine = set(claimed)
        status, reason = FR.confirm_ticket(ticket, before, positions, rows, mine)
        return {"status": status, "reason": reason, "claimed": sorted(mine)}
    got = step("confirm_ticket", label, oracle,
               lambda: door("confirm_ticket", {"ticket": ticket_doc(ticket), "before": pairs_doc(before),
                                               "positions": pos_docs(positions), "working": work_doc(rows),
                                               "claimed": sorted(claimed)}))
    if got[0] == "ok":
        seen("confirm", (got[1]["status"], got[1]["reason"]))
    step("ticket_contracts", label, lambda: pairs_doc(FR.ticket_contracts(ticket)),
         lambda: door("ticket_contracts", {"ticket": ticket_doc(ticket), "units": None}))


def test_p5_t4_confirm_ticket_lockstep() -> None:
    rng = random.Random(5103)
    sell = vorder(P200, Side.SELL, "1", "2.00")
    vertical = vorder(VERTICALS[0], Side.SELL, "1", "1.50")
    for state in STATES:                                     # the grid: each row state, single and vertical
        check_confirm(("single", state), sell, {}, [], [work(P200, Side.SELL, state=state)], set())
        legs = [work(P200, Side.SELL, state=state, limit="1.50"),
                work(P195, Side.BUY, state=OrderState.ACCEPTED, limit="1.50")]
        check_confirm(("vertical-a", state), vertical, {}, [], legs, set())
        check_confirm(("vertical-b", state), vertical, {}, [], list(reversed(legs)), set())
    check_confirm("moved", sell, {P200: D(0)}, [pos(P200, "-1")], [], set())
    check_confirm("moved-wrong-way", sell, {P200: D(0)}, [pos(P200, "1")], [], set())
    check_confirm("moved-by-two", sell, {P200: D(0)}, [pos(P200, "-2")], [], set())
    check_confirm("claimed-row", sell, {}, [], [work(P200, Side.SELL)], {0})
    check_confirm("twin-rows", sell, {}, [], [work(P200, Side.SELL), work(P200, Side.SELL)], {0})
    check_confirm("vertical-moved", vertical, {}, [pos(P200, "-1"), pos(P195, "1")], [], set())
    check_confirm("vertical-one-leg-moved", vertical, {}, [pos(P200, "-1")], [], set())
    check_confirm("vertical-leg-row-missing", vertical, {}, [], [work(P200, Side.SELL, limit="1.50")], set())
    check_confirm("vertical-one-share-off", vertical, {}, [],
                  [work(P200, Side.SELL, limit="1.50"), work(P195, Side.BUY, qty="2", limit="1.50")], set())
    for n in range(3500):                                    # seeded
        check_confirm(n, *confirm_case(rng))
    c = SEEN["confirm"]
    assert {s for s, _ in c} == {"ACCEPTED", "PENDING", "REJECTED"}, c
    reasons = [r for _, r in c]
    for want in ("on the order book (", "filled (order book)", "order book row in an", "venue order book shows",
                 "filled (position moved", "not visible on the order book", "a leg's order book row",
                 "on the order book, both legs", "filled (every leg's position moved)"):
        assert any(r.startswith(want) for r in reasons), (want, sorted(reasons))
    settle("confirm_ticket", 3500, [])
    assert sum(TALLY["ticket_contracts"].values()) >= 3500


def test_p5_t4_ticket_contracts_units_lockstep() -> None:
    rng = random.Random(5104)
    for n in range(600):
        ticket, *_ = confirm_case(rng)
        units = rng.choice([None, "1", "2", "0", "0.5", "-1", "1E+2", "7", "12345678901234567890123456789"])
        step("ticket_contracts_units", (n, units), lambda: pairs_doc(FR.ticket_contracts(
                 ticket, None if units is None else D(units))),
             lambda: door("ticket_contracts", {"ticket": ticket_doc(ticket), "units": units}))
    assert sum(TALLY["ticket_contracts_units"].values()) == 600


def test_p5_existing_reconcile_vectors_through_the_door(monkeypatch) -> None:
    import test_tos_reconcile as V

    def event(r):
        return VenueReconcile(r["venue"], datetime.fromisoformat(r["as_of"]), r["reconciled"], tuple(r["drift"]),
                              r["note"])

    def reconcile(venue, as_of, expected, positions, working):
        return event(door("reconcile", {"venue": venue, "as_of": as_of.isoformat(), "expected": pairs_doc(expected),
                                        "positions": pos_docs(positions), "working": work_doc(working)}))

    def unreadable(venue, as_of, contracts, why):
        return event(door("unreadable", {"venue": venue, "as_of": as_of.isoformat(),
                                         "contracts": [wire(c) for c in contracts], "why": why}))

    def confirm(ticket, before, positions, working, claimed):
        r = door("confirm_ticket", {"ticket": ticket_doc(ticket), "before": pairs_doc(before),
                                    "positions": pos_docs(positions), "working": work_doc(working),
                                    "claimed": sorted(claimed)})
        claimed.update(r["claimed"])
        return r["status"], r["reason"]

    monkeypatch.setattr(V, "reconcile", reconcile)
    monkeypatch.setattr(V, "unreadable", unreadable)
    monkeypatch.setattr(V, "confirm_ticket", confirm)
    assert run_module_tests(V) >= 20


# -- T5 cover --------------------------------------------------------------------------------

from types import MappingProxyType  # noqa: E402

from frozen_p5 import cover as FC  # noqa: E402
from trade_engine.domain.orders import Order  # noqa: E402
from trade_engine.ledger import codec as LCODEC  # noqa: E402
from trade_engine.ledger.events import MirrorAllocation, MirrorQueued  # noqa: E402
from trade_engine.ledger.mirror import MirrorState, MirrorTicketState  # noqa: E402

OCT, NOV, DEC, JAN27 = date(2026, 10, 16), date(2026, 11, 20), date(2026, 12, 18), date(2027, 1, 15)
CCA, PMA, WHL = "OPT_COVERED_CALL", "OPT_PMCC", "OPT_WHEEL"
MSFT = Equity("MSFT")


def ccall(strike="210", expiry=OCT, und="AAPL", right="C", multiplier=100):
    return opt(und, expiry, strike, right, multiplier)


def queued(instrument, side, qty, key, account=CCA) -> MirrorQueued:
    return MirrorQueued(
        venue=VENUE, ticket_key=key, instrument=instrument, side=side, quantity=D(qty), order_type=OrderType.LIMIT,
        limit_price=D("1.00"), tif=TimeInForce.DAY, allocations=(MirrorAllocation("so-" + key, account, D(qty)),), at=T)


def tstate(instrument, side, qty, key="tos:a", filled="0", closed=False, account=CCA) -> MirrorTicketState:
    return MirrorTicketState(queued=queued(instrument, side, qty, key, account), filled=D(filled), closed=closed)


def mstate(book=None, tickets=()) -> MirrorState:
    return MirrorState(venue=VENUE, tickets=MappingProxyType({t.key: t for t in tickets}),
                       book=MappingProxyType({k: D(v) for k, v in (book or {}).items()}))


def corder(instrument, side, qty="1", oid="so-1", account=CCA) -> Order:
    return Order(order_id=oid, account_id=account, instrument=instrument, order_type=OrderType.LIMIT, side=side,
                 quantity=D(qty), command_id=oid, created_at=T, limit_price=D("1.00"), tif=TimeInForce.DAY)


def corder_doc(o) -> dict:
    return {"instrument": wire(o.instrument), "side": o.side.value, "quantity": str(o.quantity)}


def found_doc(found) -> list:
    return [[u, str(c)] for u, c in found.items()]


def check_uncovered(label, held) -> dict:
    got = step("uncovered", label, lambda: found_doc(FC.uncovered(held)),
               lambda: door("uncovered", {"held": pairs_doc(held)}))
    if got[0] == "ok":
        seen("uncovered", len(got[1]))
    return got


def check_cover(label, mirror, order, accepted=()) -> None:
    canon = LCODEC.canon(mirror)
    step("holdings", label, lambda: pairs_doc(FC.holdings(mirror)), lambda: door("holdings", {"mirror": canon}))
    got = step("cover_reason", label, lambda: FC.cover_reason(mirror, order, list(accepted)),
               lambda: door("cover_reason", {"mirror": canon, "order": corder_doc(order),
                                             "accepted": [corder_doc(a) for a in accepted]})["reason"])
    if got[0] == "ok":
        seen("cover_reason", got[1] is None)
    step("sold", label, lambda: (lambda r: None if r is None else [wire(r[0]), str(r[1])])(FC._sold(order)),
         lambda: door("sold", {"order": corder_doc(order)}))


def check_bare(label, longs, shorts) -> list:
    got = step("bare", label, lambda: [wire(c) for c in FC._bare(longs, shorts)],
               lambda: door("bare", {"longs": [wire(c) for c in longs], "shorts": [wire(c) for c in shorts]})["left"])
    return got


def greedy_bare(longs, shorts):
    """What a greedy matcher would leave bare: the first free long that covers, never handed on."""
    taken, left = set(), []
    for short in shorts:
        for index, long in enumerate(longs):
            if index not in taken and FC.covers(long, short):
                taken.add(index)
                break
        else:
            left.append(short)
    return left


STRIKES = ["190", "200", "200.0", "205", "210", "215", "1E+2", "0.5"]
EXPIRIES = [OCT, NOV, DEC, JAN27]
CALLS = [ccall(k, e, u, r, m) for k in ("200", "205", "210", "215") for e in (OCT, NOV, DEC)
         for u in ("AAPL", "MSFT") for r in ("C",) for m in (100,)] + [
    ccall("200", DEC, multiplier=10), ccall("210", OCT, multiplier=10), ccall("205", NOV, multiplier=50),
    ccall("210", OCT, right="P"), ccall("200", DEC, right="P"), ccall("200.0", DEC), ccall("210.0", OCT)]


def test_p5_t5_covers_lockstep() -> None:
    grid = CALLS + [ccall(k, e) for k in STRIKES for e in EXPIRIES]
    n = 0
    for long in grid:
        for short in grid:
            got = step("covers", n, lambda: FC.covers(long, short),
                       lambda: door("covers", {"long": wire(long), "short": wire(short)})["result"])
            seen("covers", got[1])
            n += 1
    c = SEEN["covers"]
    assert c[True] > 200 and c[False] > 2000, c
    assert sum(TALLY["covers"].values()) == n >= 3900


def uncovered_case(rng) -> dict:
    held = {}
    for _ in range(rng.randint(0, 7)):
        und = rng.choice(["AAPL", "AAPL", "AAPL", "MSFT"])
        if rng.random() < 0.25:
            held[Equity(und)] = D(rng.choice(["50", "100", "200", "250", "99", "-100", "0", "300", "10", "1E+2"]))
        else:
            call = ccall(rng.choice(["200", "205", "210", "215"]), rng.choice([OCT, NOV, DEC]), und,
                         rng.choice(["C", "C", "C", "P"]), rng.choice([100, 100, 100, 10, 50]))
            held[call] = D(rng.choice(["1", "2", "3", "-1", "-2", "-3", "-1", "0", "1.5", "-1.5", "-2.0", "0.5", "-0.5"]))
    return held


def test_p5_t5_uncovered_and_bare_lockstep() -> None:
    rng = random.Random(5201)
    l1, l2 = ccall("200", DEC), ccall("205", OCT)
    s1, s2 = ccall("210", OCT), ccall("210", NOV)
    # the hand grid
    check_uncovered("nothing", {})
    check_uncovered("bare-short", {ccall(): D(-1)})
    check_uncovered("debit-diagonal", {ccall("200", DEC): D(1), ccall(): D(-1)})
    check_uncovered("credit-diagonal", {ccall("215", DEC): D(1), ccall(): D(-1)})
    check_uncovered("shorter-long", {ccall("200", OCT): D(1), ccall("210", NOV): D(-1)})
    check_uncovered("mixed-multipliers", {AAPL: D(100), ccall(): D(-1), ccall("215", multiplier=50): D(-2)})
    check_uncovered("mixed-10-100", {AAPL: D(100), ccall(multiplier=10): D(-3), ccall("215"): D(-1)})
    check_uncovered("mixed-longs", {ccall("200", DEC, multiplier=10): D(1), ccall(): D(-1)})
    check_uncovered("wide-narrow", {l1: D(1), l2: D(1), s1: D(-1), s2: D(-1)})
    check_uncovered("short-shares", {AAPL: D(-100), ccall(): D(-1)})
    check_uncovered("a-put", {ccall(right="P"): D(-1), AAPL: D(0)})
    check_uncovered("fraction", {ccall(): D("-1.5"), AAPL: D(100)})
    check_uncovered("half", {ccall(): D("-0.5")})
    check_uncovered("infinity", {ccall(): D("-Infinity")})
    check_uncovered("infinity-long", {ccall(): D("Infinity"), ccall("215"): D(-1)})
    check_uncovered("nan-short", {ccall(): D("NaN")})
    check_uncovered("nan-shares", {AAPL: D("NaN"), ccall(): D(-1)})
    for n in range(2500):                                   # seeded
        check_uncovered(n, uncovered_case(rng))
    c = SEEN["uncovered"]
    assert c[0] > 300 and c[1] > 300 and c[2] > 100, c
    # the matcher: a maximum matching, never a greedy one
    differs = 0
    for n in range(2500):
        shorts = [rng.choice(CALLS[:24] + [ccall("210.0", OCT)]) for _ in range(rng.randint(0, 6))]
        longs = [rng.choice(CALLS[:24] + [ccall("205.0", NOV)]) for _ in range(rng.randint(0, 6))]
        got = check_bare(n, longs, shorts)
        assert got[0] == "ok"
        if got[1] != [wire(c) for c in greedy_bare(longs, shorts)]:
            differs += 1
    assert differs > 40, differs                            # the generator would catch a greedy matcher
    assert check_bare("wide-narrow", [l1, l2], [s1, s2])[1] == []
    assert len(greedy_bare([l1, l2], [s1, s2])) == 1     # greedy strands the November short
    check_bare("empty", [], [])
    settle("uncovered", 2500, ["OverflowError: cannot convert Infinity to integer", "InvalidOperation"])
    settle("bare", 2500, [])


def cover_case(rng):
    """A mirror book with resting tickets, an order, and the batch already let through."""
    def shares(und):
        return Equity(und)

    def pick():
        und = rng.choice(["AAPL", "AAPL", "AAPL", "MSFT"])
        kind = rng.random()
        if kind < 0.3:
            return shares(und)
        if kind < 0.45:
            return ccall(rng.choice(["200", "205", "210", "215"]), rng.choice([OCT, NOV, DEC]), und, "P")
        return ccall(rng.choice(["200", "205", "210", "215", "210.0"]), rng.choice([OCT, NOV, DEC]), und, "C",
                     rng.choice([100, 100, 100, 10]))

    book = {}
    for _ in range(rng.randint(0, 6)):
        instrument = pick()
        qty = rng.choice(["100", "200", "50", "-100"]) if isinstance(instrument, Equity) else \
            rng.choice(["1", "2", "-1", "-2", "3", "-3", "0", "1.5"])
        book[(rng.choice([CCA, PMA, WHL]), instrument)] = qty
    tickets = []
    for t in range(rng.randint(0, 3)):
        instrument = pick()
        if rng.random() < 0.15:
            instrument = combo((ccall("210"), 1, Side.SELL), (ccall("215"), 1, Side.BUY))
        qty = rng.choice(["100", "200", "50"]) if isinstance(instrument, Equity) else rng.choice(["1", "2", "3"])
        filled = rng.choice(["0", "0", "0", str(int(qty) // 2), qty])
        tickets.append(tstate(instrument, rng.choice(list(Side)), qty, key=f"tos:{t}", filled=filled,
                              closed=rng.random() < 0.1, account=rng.choice([CCA, PMA])))
    state = mstate(book, tickets)

    def order(oid):
        instrument = pick()
        if rng.random() < 0.08:
            instrument = combo((ccall("210"), 1, Side.SELL), (ccall("215"), 1, Side.BUY))
        qty = rng.choice(["100", "50", "99", "101", "200", "0.5"]) if isinstance(instrument, Equity) else \
            rng.choice(["1", "2", "3", "0.5"])
        return corder(instrument, rng.choice([Side.SELL, Side.SELL, Side.SELL, Side.BUY]), qty, oid)

    accepted = [order(f"so-a{i}") for i in range(rng.randint(0, 3))]
    return state, order("so-x"), accepted


def test_p5_t5_cover_reason_lockstep() -> None:
    rng = random.Random(5202)
    c210, c215, leaps, mini = ccall("210"), ccall("215"), ccall("200", DEC), ccall("215", multiplier=10)
    S, B = Side.SELL, Side.BUY
    # the edge grid: a short call with nothing behind it; shares; debit and credit diagonals; mixed multipliers
    check_cover("bare-short", mstate(), corder(c210, S))
    check_cover("lot", mstate({(CCA, AAPL): 100}), corder(c210, S))
    check_cover("99", mstate({(CCA, AAPL): 99}), corder(c210, S))
    check_cover("two-in-batch", mstate({(CCA, AAPL): 100}), corder(c210, S, oid="so-2"),
                [corder(c210, S, oid="so-1")])
    check_cover("two-lots", mstate({(CCA, AAPL): 200}), corder(c210, S, oid="so-2"), [corder(c210, S, oid="so-1")])
    check_cover("debit", mstate({(PMA, leaps): 1}), corder(c210, S, account=PMA))
    check_cover("credit", mstate({(PMA, ccall("215", DEC)): 1}), corder(c210, S, account=PMA))
    check_cover("sell-shares-under-short", mstate({(CCA, AAPL): 100, (CCA, c210): -1}), corder(AAPL, S, "100"))
    check_cover("sell-spare-shares", mstate({(CCA, AAPL): 200, (CCA, c210): -1}), corder(AAPL, S, "101"))
    check_cover("sell-long-under-short", mstate({(PMA, leaps): 1, (PMA, c210): -1}), corder(leaps, S, account=PMA))
    check_cover("mixed-multipliers", mstate({(CCA, AAPL): 100, (CCA, mini): -2}), corder(c210, S))
    check_cover("mixed-multipliers-after", mstate({(CCA, AAPL): 100, (CCA, c210): -1}), corder(mini, S))
    check_cover("resting-buy", mstate({}, [tstate(AAPL, B, "100")]), corder(c210, S))
    check_cover("resting-sell", mstate({(CCA, AAPL): 100}, [tstate(AAPL, S, "100")]), corder(c210, S))
    check_cover("resting-short", mstate({(CCA, AAPL): 100}, [tstate(c210, S, "1")]), corder(c210, S, oid="so-2"))
    check_cover("resting-vertical", mstate({}, [tstate(combo((c210, 1, S), (c215, 1, B)), S, "1")]),
                corder(c210, S))
    check_cover("buy-to-close", mstate({(CCA, AAPL): 100, (CCA, c210): -1}), corder(c210, B))
    check_cover("a-put", mstate(), corder(ccall(right="P"), S))
    check_cover("a-vertical", mstate(), corder(combo((c210, 1, S), (c215, 1, B)), S))
    check_cover("short-shares", mstate({(CCA, AAPL): -100}), corder(c210, S))
    check_cover("other-underlying", mstate({(CCA, AAPL): 100}), corder(ccall("400", und="MSFT"), S))
    check_cover("equal-spellings", mstate({(CCA, ccall("210.0")): -1, (PMA, c210): -1, (CCA, AAPL): 100}),
                corder(AAPL, S, "100"))
    check_cover("fraction", mstate({(CCA, AAPL): 100}), corder(c210, S, "0.5"))
    check_cover("fraction-shares", mstate({(CCA, AAPL): 100, (CCA, c210): -1}), corder(AAPL, S, "0.5"))
    for n in range(2200):                                   # seeded
        state, order, accepted = cover_case(rng)
        check_cover(n, state, order, accepted)
    c = SEEN["cover_reason"]
    assert c[True] > 300 and c[False] > 300, c
    settle("cover_reason", 2200, [])
    settle("holdings", 2200, [])
    settle("sold", 2200, [])


def test_p5_existing_cover_vectors_through_the_door(monkeypatch) -> None:
    import test_tos_cover as V

    def instrument_back(w):
        return instrument_of(w)

    def held_back(doc):
        return {instrument_back(i): D(q) for i, q in doc}

    def covers(long, short):
        return door("covers", {"long": wire(long), "short": wire(short)})["result"]

    def uncovered(held):
        return {u: D(c) for u, c in door("uncovered", {"held": pairs_doc(held)})}

    def holdings(mirror):
        return held_back(door("holdings", {"mirror": LCODEC.canon(mirror)}))

    def cover_reason(mirror, order, accepted=()):
        return door("cover_reason", {"mirror": LCODEC.canon(mirror), "order": corder_doc(order),
                                     "accepted": [corder_doc(a) for a in accepted]})["reason"]

    monkeypatch.setattr(V, "covers", covers)
    monkeypatch.setattr(V, "uncovered", uncovered)
    monkeypatch.setattr(V, "holdings", holdings)
    monkeypatch.setattr(V, "cover_reason", cover_reason)
    assert run_module_tests(V) >= 40


# -- T6 netting ------------------------------------------------------------------------------

from frozen_p5 import netting as FN6  # noqa: E402
from trade_engine.domain.orders import Order  # noqa: E402
from trade_engine.interfaces.broker import VenueOrderAllocation  # noqa: E402
from trade_engine.tos_paper import netting as PNET  # noqa: E402

sim_rs.register("tos_netting_error", PNET.NettingError)

NMIRROR = ("OPT_CSP", "OPT_PUT_SPREAD")
NACCTS = ["OPT_CSP", "OPT_PUT_SPREAD", "OPT_WHEEL_CORE", "O'Q"]
NMOST = ["OPT_CSP", "OPT_PUT_SPREAD"] * 8 + ["OPT_WHEEL_CORE", "O'Q"]
NQTYS = ["1", "2", "3", "5", "1.5", "0.5", "2.0", "1E+1", "100", "7"]
NLIMITS = ["2.00", "2", "2.0", "3.00", "0.05", "1E+1"]
NHOLD = ["0", "1", "-1", "2", "-2", "1.0", "-3", "-0", "0.5"]
NINSTS = [AAPL, P200, P195, C200, opt(strike="200.0"), opt(strike="190"), opt(multiplier=10)]
NVERT = [
    combo((P200, 1, Side.SELL), (P195, 1, Side.BUY)), combo((P200, 1, Side.BUY), (P195, 1, Side.SELL)),
    combo((P200, 2, Side.SELL), (P195, 2, Side.BUY)), combo((P200, 1, Side.SELL), (P195, 2, Side.BUY)),
    combo((P200, 1, Side.SELL), (opt(strike="200.0"), 1, Side.BUY)), combo((P200, 1, Side.SELL), (C200, 1, Side.BUY)),
    combo((P195, 1, Side.SELL), (P200, 1, Side.BUY)), combo((P200, 1, Side.SELL)),
    combo((AAPL, 100, Side.BUY), (C200, 1, Side.SELL)),
]
N_AT = [T, T, T, T, datetime(2026, 9, 24, 20, 0), datetime(2026, 9, 24, 15, 0, tzinfo=timezone(timedelta(hours=-5)))]


def norder(oid, account, instrument, side, qty="1", otype=OrderType.LIMIT, limit="2.00", tif=TimeInForce.DAY,
           stop=None):
    return Order(order_id=oid, account_id=account, instrument=instrument, order_type=otype, side=side,
                 quantity=D(qty), command_id=oid, created_at=T,
                 limit_price=None if limit is None else D(limit),
                 stop_price=None if stop is None else D(stop), tif=tif)


def nopen(o) -> dict:
    return {"order_id": o.order_id, "account_id": o.account_id, "instrument": wire(o.instrument),
            "order_type": o.order_type.value, "side": o.side.value, "quantity": str(o.quantity),
            "tif": o.tif.value, "limit_price": None if o.limit_price is None else str(o.limit_price)}


def vo_doc(v) -> dict:
    return {"venue_order_id": v.venue_order_id, "instrument": wire(v.instrument), "order_type": v.order_type.value,
            "side": v.side.value, "quantity": str(v.quantity), "submitted_at": v.submitted_at.isoformat(),
            "tif": v.tif.value, "limit_price": None if v.limit_price is None else str(v.limit_price),
            "allocations": [[a.strategy_order_id, a.account_id, str(a.quantity)] for a in v.allocations]}


def batch_doc(b) -> dict:
    return {"venue_orders": [vo_doc(v) for v in b.venue_orders], "refused": [list(r) for r in b.refused]}


def holdings_doc(holdings) -> list:
    return [[a, wire(i), str(q)] for (a, i), q in (holdings or {}).items()]


def net_doc(orders, venue_account, mirrored, at, holdings) -> dict:
    return {"orders": [nopen(o) for o in orders], "venue_account": venue_account, "mirrored": list(mirrored),
            "at": at.isoformat(), "holdings": holdings_doc(holdings)}


def check_net(label, orders, holdings=None, mirrored=NMIRROR, at=T, venue=VENUE):
    got = step("net", label,
               lambda: batch_doc(FN6.net_strategy_orders(orders, venue_account=venue, mirrored_accounts=mirrored,
                                                         at=at, holdings=holdings)),
               lambda: door("net_strategy_orders", net_doc(orders, venue, mirrored, at, holdings)))
    if got[0] == "ok":
        for _, reason in got[1]["refused"]:
            for tag in ("is not mirrored", "multi-leg combo", "one net LIMIT", "order type", "TIF", "not a whole number",
                        "duplicate", "opposes first-in", "opposite side of", "is not a mirrored option"):
                if tag in reason:
                    seen("net", tag)
        if len(got[1]["venue_orders"]) > 1:
            seen("net", "several tickets")
        if any(len(v["allocations"]) > 1 for v in got[1]["venue_orders"]):
            seen("net", "netted")
        if any(isinstance(o.instrument, Combo) for o in orders) and any(
                v["instrument"]["kind"] == "combo" for v in got[1]["venue_orders"]):
            seen("net", "vertical ticket")
    else:
        seen("net", got[1])
    return got


def net_case(rng):
    n = rng.choice([1, 2, 2, 3, 3, 4, 5, 6])
    orders = []
    for k in range(n):
        oid = f"o{rng.randrange(n + 1)}" if rng.random() < 0.12 else f"o{k}"
        roll = rng.random()
        if roll < 0.16:
            inst = rng.choice(NVERT)
        elif roll < 0.18:
            inst = FutureLike()
        else:
            inst = rng.choice(NINSTS)
        otype = rng.choice([OrderType.LIMIT] * 8 + [OrderType.MARKET] * 3 + [OrderType.STOP, OrderType.STOP_LIMIT])
        limit = None if otype is OrderType.MARKET else rng.choice(NLIMITS)
        stop = "1.00" if otype in (OrderType.STOP, OrderType.STOP_LIMIT) else None
        if otype is OrderType.STOP:
            limit = None
        tif = rng.choice([TimeInForce.DAY] * 6 + [TimeInForce.GTC] * 3 + [TimeInForce.OPG, TimeInForce.GTD])
        orders.append(norder(oid, rng.choice(NMOST), inst, rng.choice([Side.BUY, Side.SELL]), rng.choice(NQTYS),
                             otype, limit, tif, stop))
    holdings = {}
    for _ in range(rng.choice([0, 0, 1, 2, 3, 4])):
        holdings[(rng.choice(NMOST), rng.choice(NINSTS))] = D(rng.choice(NHOLD))
    mirrored = rng.choice([NMIRROR] * 6 + [NMIRROR + ("O'Q",), ("OPT_CSP",), NMIRROR[::-1]])
    return orders, holdings, mirrored, rng.choice(N_AT)


def test_p5_t6_net_strategy_orders_lockstep() -> None:
    rng = random.Random(6006)
    S_, B_ = Side.SELL, Side.BUY
    csp, psp = "OPT_CSP", "OPT_PUT_SPREAD"
    # the plan's edge grid
    check_net("same-side-sum", [norder("a", csp, P200, S_, "1"), norder("b", psp, P200, S_, "3")])
    check_net("opposing-same-batch", [norder("a", csp, P200, S_), norder("b", psp, P200, B_)])
    check_net("opposing-same-account", [norder("a", csp, P200, S_), norder("b", csp, P200, B_)])
    check_net("opposing-vs-book", [norder("a", csp, P200, S_)], {(psp, P200): D("2")})
    check_net("opposing-vs-book-flat", [norder("a", csp, P200, S_)], {(psp, P200): D("0")})
    check_net("same-account-closes", [norder("a", csp, P200, B_)], {(csp, P200): D("-2")})
    check_net("across-accounts-book", [norder("a", csp, P200, B_), norder("b", psp, P200, B_)],
              {(csp, P200): D("-1"), (psp, P200): D("-1")})
    check_net("across-accounts-flip", [norder("a", csp, P200, S_), norder("b", psp, P200, S_)],
              {(csp, P200): D("1")})
    check_net("fractional-contracts", [norder("a", csp, P200, S_, "1.5"), norder("b", csp, P200, S_, "2")])
    check_net("fractional-shares", [norder("a", csp, AAPL, B_, "0.5", OrderType.MARKET, None)])
    check_net("whole-shares-as-decimal", [norder("a", csp, AAPL, B_, "1E+2", OrderType.MARKET, None)])
    check_net("stop", [norder("a", csp, P200, S_, "1", OrderType.STOP, None, stop="1.00")])
    check_net("stop-limit", [norder("a", csp, P200, S_, "1", OrderType.STOP_LIMIT, "2.00", stop="1.00")])
    for tif in (TimeInForce.GTD, TimeInForce.OPG, TimeInForce.MOC, TimeInForce.GTC):
        check_net(f"tif-{tif}", [norder("a", csp, P200, S_, tif=tif), norder("b", csp, P195, S_)])
    check_net("other-kind", [norder("a", csp, FutureLike(), B_)])
    check_net("not-mirrored", [norder("a", "OPT_WHEEL_CORE", P200, S_)])
    check_net("not-mirrored-quote", [norder("a", "O'Q", P200, S_)], mirrored=("A'B", "z", "O'R"))
    check_net("duplicate", [norder("a", csp, P200, S_), norder("a", csp, P200, S_)])
    check_net("duplicate-refused-first", [norder("a", "OPT_WHEEL_CORE", P200, S_), norder("a", csp, P200, S_)])
    for q in ("1", "2"):
        check_net("vertical", [norder("v", psp, NVERT[0], S_, q)])
        check_net("vertical-buy", [norder("v", psp, NVERT[1], B_, q)])
    check_net("vertical-market", [norder("v", psp, NVERT[0], S_, "1", OrderType.MARKET, None)])
    check_net("vertical-shapes", [norder(f"v{k}", psp, v, S_) for k, v in enumerate(NVERT)])
    check_net("vertical-then-leg", [norder("v", psp, NVERT[0], S_), norder("l", csp, P200, B_)])
    check_net("leg-then-vertical", [norder("l", csp, P200, S_), norder("v", psp, NVERT[0], S_)])
    check_net("refused-vertical-claims-nothing", [norder("v", psp, NVERT[0], S_, "1", OrderType.MARKET, None),
                                                  norder("l", csp, P200, B_)])
    check_net("vertical-vs-book", [norder("v", psp, NVERT[0], S_)], {(csp, P195): D("-1")})
    check_net("vertical-second-leg-conflict", [norder("v", psp, NVERT[0], S_)], {(csp, P195): D("-1")})
    check_net("vertical-ratio-2", [norder("v", psp, NVERT[2], S_, "3")])
    check_net("empty", [])
    check_net("naive-at", [norder("a", csp, P200, S_)], at=datetime(2026, 9, 24, 20, 0))
    check_net("naive-at-vertical", [norder("v", psp, NVERT[0], S_)], at=datetime(2026, 9, 24, 20, 0))
    check_net("offset-at", [norder("a", csp, P200, S_)], at=datetime(2026, 9, 24, 15, 0, tzinfo=timezone(timedelta(hours=-5))))
    check_net("limits-differ", [norder("a", csp, P200, S_, limit="2.00"), norder("b", psp, P200, S_, limit="3.00")])
    check_net("limits-spell-alike", [norder("a", csp, P200, S_, limit="2.00"), norder("b", psp, P200, S_, limit="2")])
    check_net("equal-strikes", [norder("a", csp, P200, S_), norder("b", psp, opt(strike="200.0"), S_)])
    check_net("hold-equal-spelling", [norder("a", csp, P200, S_)], {(psp, opt(strike="200.0")): D("1")})
    check_net("market-and-limit", [norder("a", csp, P200, S_, otype=OrderType.MARKET, limit=None),
                                   norder("b", psp, P200, S_)])
    for k in range(3200):                                   # seeded
        orders, holdings, mirrored, at = net_case(rng)
        check_net(k, orders, holdings, mirrored, at)
    c = SEEN["net"]
    for tag in ("is not mirrored", "multi-leg combo", "one net LIMIT", "order type", "TIF", "not a whole number",
                "duplicate", "opposes first-in", "opposite side of", "is not a mirrored option", "several tickets",
                "netted", "vertical ticket"):
        assert c[tag] > 20, (tag, c)
    settle("net", 3200, ["ValueError: submitted_at must be timezone-aware", "NettingError: no strategy orders to net"])


def test_p5_t6_ticket_key_lockstep() -> None:
    rng = random.Random(6007)
    qs = ["1", "2", "100", "1E+2", "1.50", "0", "-1", "1234567890123456789012345678901", "0.0000001", "Infinity",
          "-0.00", "1E-30", "12345678901234567890123456.78"]
    ls = [None, "2.00", "2", "0", "3.1400", "1E+1", "-1", "1E+27"]
    insts = [AAPL, Equity("BRK"), P200, opt(strike="200.0"), opt(multiplier=10)] + NVERT[:3]
    accts = [VENUE, "", "x\x1fy", "É", "D-2"]
    idsets = [["a"], ["b", "a"], [], ["a", "a"], ["é", "e", "Z"], ["", "x"], ["1", "10", "2"]]
    n = 0
    for _ in range(2500):
        args = (rng.choice(accts), rng.choice(insts), rng.choice([Side.BUY, Side.SELL]), D(rng.choice(qs)),
                rng.choice(list(OrderType)), None if (lim := rng.choice(ls)) is None else D(lim),
                rng.choice(list(TimeInForce)), rng.choice(idsets))
        va, inst, side, q, ot, lim, tif, ids = args
        step("ticket_key", n,
             lambda: FN6.ticket_key(*args),
             lambda: door("ticket_key", {"venue_account": va, "instrument": wire(inst), "side": side.value,
                                         "quantity": str(q), "order_type": ot.value,
                                         "limit_price": None if lim is None else str(lim), "tif": tif.value,
                                         "order_ids": ids})["key"])
        n += 1
    # the key is sensitive to every field, and spelling-blind where Decimal.normalize is
    base = door("ticket_key", {"venue_account": "v", "instrument": wire(P200), "side": "SELL", "quantity": "2",
                               "order_type": "LIMIT", "limit_price": "2.00", "tif": "DAY", "order_ids": ["a"]})["key"]
    assert base == FN6.ticket_key("v", P200, Side.SELL, D("2"), OrderType.LIMIT, D("2.00"), TimeInForce.DAY, ["a"])
    assert base == door("ticket_key", {"venue_account": "v", "instrument": wire(P200), "side": "SELL",
                                       "quantity": "2.0", "order_type": "LIMIT", "limit_price": "2", "tif": "DAY",
                                       "order_ids": ["a"]})["key"]
    settle("ticket_key", 2500, [])


def test_p5_t6_screen_mixed_signs_and_invariant_lockstep() -> None:
    rng = random.Random(6008)
    insts = NINSTS + NVERT + [FutureLike()]
    for k in range(2400):
        otype = rng.choice(list(OrderType))
        limit = rng.choice(NLIMITS)
        stop = "1.00"
        if otype is OrderType.MARKET:
            limit, stop = None, None
        elif otype is OrderType.LIMIT:
            stop = None
        elif otype is OrderType.STOP:
            limit = None
        elif otype is OrderType.TRAIL:
            continue
        o = norder("o", rng.choice(NMOST), rng.choice(insts), rng.choice([Side.BUY, Side.SELL]),
                   rng.choice(NQTYS), otype, limit, rng.choice(list(TimeInForce)), stop)
        mirrored = rng.choice([NMIRROR] * 8 + [("OPT_CSP",), ("O'Q", "a"), ()])
        got = step("screen", k, lambda: FN6._screen(o, frozenset(mirrored)),
                   lambda: door("screen", {"order": nopen(o), "mirrored": list(mirrored)})["reason"])
        seen("screen", got[1] is None)
        for tag in ("is not mirrored", "multi-leg combo", "a vertical is mirrored", "order type", "TIF",
                    "not a whole number", "not a mirrored option"):
            if got[1] and tag in got[1]:
                seen("screen", tag)
    assert SEEN["screen"][True] > 100 and SEEN["screen"][False] > 800, SEEN["screen"]
    for k in range(1500):
        book = {rng.choice(["a", "b", "c", "d"]): D(rng.choice(NHOLD)) for _ in range(rng.choice([0, 1, 2, 3, 4]))}
        got = step("mixed_signs", k, lambda: FN6._mixed_signs(book),
                   lambda: door("mixed_signs", {"book": [[a, str(q)] for a, q in book.items()]})["result"])
        seen("mixed", got[1])
    assert SEEN["mixed"][True] > 100 and SEEN["mixed"][False] > 100, SEEN["mixed"]
    ids = ["a", "b", "c", "é", "d'e"]
    for k in range(1200):
        orders = [SimpleNamespace(order_id=rng.choice(ids)) for _ in range(rng.choice([0, 1, 2, 3]))]
        allocated = [rng.choice(ids) for _ in range(rng.choice([0, 1, 2]))]
        refused = [rng.choice(ids) for _ in range(rng.choice([0, 1, 2]))]
        vos = [SimpleNamespace(allocations=[SimpleNamespace(strategy_order_id=i) for i in allocated])]
        step("afe", k, lambda: FN6._account_for_everything(orders, vos, [(r, "x") for r in refused]),
             lambda: door("account_for_everything", {"orders": [o.order_id for o in orders], "allocated": allocated,
                                                       "refused": refused}) and None)
    for tag in ("is not mirrored", "multi-leg combo", "a vertical is mirrored", "order type", "TIF",
                "not a whole number", "not a mirrored option"):
        assert SEEN["screen"][tag] > 20, (tag, SEEN["screen"])
    settle("screen", 1800, [])
    settle("mixed_signs", 1500, [])
    settle("afe", 1200, ["NettingError: netting lost or duplicated an order: inputs"])


def test_p5_t6_ticket_lockstep() -> None:
    rng = random.Random(6009)
    for k in range(1800):
        inst = rng.choice(NINSTS + NVERT[:3])
        side = rng.choice([Side.BUY, Side.SELL])
        otype = rng.choice([OrderType.LIMIT, OrderType.MARKET])
        limit = rng.choice(NLIMITS + [None])
        if otype is OrderType.MARKET:
            order_limit = None
        else:
            order_limit = limit or "2.00"
        group = [norder(f"g{i}", rng.choice(NACCTS), inst, side, rng.choice(NQTYS), otype, order_limit,
                        TimeInForce.DAY) for i in range(rng.choice([1, 1, 2, 3]))]
        tif = rng.choice([TimeInForce.DAY, TimeInForce.GTC])
        at = rng.choice(N_AT)
        # the (type, limit) the ticket is built with need not match its orders': the constructor decides
        lim = None if limit is None else D(limit)
        step("ticket", k, lambda: vo_doc(FN6._ticket(VENUE, inst, otype, tif, lim, group, at)),
             lambda: door("ticket", {"venue_account": VENUE, "instrument": wire(inst), "order_type": otype.value,
                                     "tif": tif.value, "limit_price": None if lim is None else str(lim),
                                     "group": [nopen(o) for o in group], "at": at.isoformat()}))
    settle("ticket", 1800, ["ValueError: submitted_at must be timezone-aware", "ValueError: MARKET order cannot",
                            "ValueError: LIMIT order must"])


def test_p5_existing_netting_vectors_through_the_door(monkeypatch) -> None:
    import test_tos_netting as V

    def inst_back(w):
        if w["kind"] == "combo":
            return Combo([ComboLeg(inst_back(l["contract"]), l["ratio"], Side(l["side"])) for l in w["legs"]])
        return instrument_of(w)

    def venue_back(d):
        return VenueOrder(
            venue_order_id=d["venue_order_id"], instrument=inst_back(d["instrument"]),
            order_type=OrderType(d["order_type"]), side=Side(d["side"]), quantity=D(d["quantity"]),
            submitted_at=datetime.fromisoformat(d["submitted_at"]), tif=TimeInForce(d["tif"]),
            limit_price=None if d["limit_price"] is None else D(d["limit_price"]),
            allocations=tuple(VenueOrderAllocation(o, a, D(q)) for o, a, q in d["allocations"]))

    def net(orders, *, venue_account, mirrored_accounts, at, holdings=None):
        r = door("net_strategy_orders", net_doc(orders, venue_account, mirrored_accounts, at, holdings))
        return PNET.NettedBatch(venue_orders=tuple(venue_back(v) for v in r["venue_orders"]),
                                refused=tuple((o, why) for o, why in r["refused"]))

    def key(venue_account, instrument, side, quantity, order_type, limit_price, tif, order_ids):
        return door("ticket_key", {"venue_account": venue_account, "instrument": wire(instrument),
                                   "side": side.value, "quantity": str(quantity), "order_type": order_type.value,
                                   "limit_price": None if limit_price is None else str(limit_price),
                                   "tif": tif.value, "order_ids": list(order_ids)})["key"]

    def afe(orders, venue_orders, refused):
        door("account_for_everything", {"orders": [o.order_id for o in orders],
                                        "allocated": [a.strategy_order_id for v in venue_orders for a in v.allocations],
                                        "refused": [o for o, _ in refused]})

    monkeypatch.setattr(V, "net_strategy_orders", net)
    monkeypatch.setattr(V, "ticket_key", key)
    monkeypatch.setattr(PNET, "_account_for_everything", afe)
    ran = run_module_tests(V, only={n for n in vars(V) if n.startswith("test_")} - {
        "test_a_ticket_error_refuses_its_orders_not_the_batch"})   # that one patches Python's own _ticket
    assert ran >= 20


# -- T7 exits --------------------------------------------------------------------------------

from frozen_p5 import exits as FE  # noqa: E402
from frozen_p5.broker import MirrorBinding  # noqa: E402
from trade_engine.domain.portfolio import Position  # noqa: E402
from trade_engine.ledger.state import AccountState  # noqa: E402
from trade_engine.tos_paper import exits as PE  # noqa: E402

sim_rs.register("tos_exit_plan_error", PE.ExitPlanError)

XS = date(2026, 9, 28)
XAT = datetime(2026, 9, 28, 16, 35, tzinfo=UTC)
XMIRROR = ("OPT_CSP", "OPT_PUT_SPREAD")
XOTHER = "OPT_WHEEL_CORE"
P205C = opt(strike="205", right="C")
P200E = opt(strike="200.0")
XVERTS = [
    combo((P200, 1, Side.SELL), (P195, 1, Side.BUY)),
    combo((P200, 1, Side.BUY), (P195, 1, Side.SELL)),
    combo((P200, 2, Side.SELL), (P195, 2, Side.BUY)),
    combo((C200, 1, Side.SELL), (P205C, 1, Side.BUY)),
]
XFLAT = [P200, P195, C200, AAPL, P205C]
XSTATES = [OrderState.ACCEPTED, OrderState.ACCEPTED, OrderState.SUBMITTED, OrderState.PARTIALLY_FILLED,
           OrderState.FILLED, OrderState.CANCELLED, OrderState.REJECTED, OrderState.EXPIRED, OrderState.NEW]
XUNITS = ["0", "1", "1", "2", "3", "-1", "-2", "1.0", "0.5", "-0"]
XNAMES = ["morning", "midday", "late", "follow-1235", "follow-0931", "noon", "follow-", "", "Follow-1", "late "]
XPRICES = ["0.60", "1.25", "0.60", "2", "0.05", "0", "-1", None, None, "1E+1"]


class XLedger:
    def __init__(self, mirror, states):
        self.mirror, self.states = mirror, states

    def state(self, account):
        return self.states.get(account) or AccountState(account)


def xorder(oid, account, instrument, side, qty="1", state=OrderState.ACCEPTED, parent=None, tif=TimeInForce.GTC,
           otype=OrderType.LIMIT, limit="1.00"):
    return Order(order_id=oid, account_id=account, instrument=instrument, order_type=otype, side=side,
                 quantity=D(qty), command_id=oid, created_at=T,
                 limit_price=None if otype is OrderType.MARKET else D(limit),
                 tif=tif, state=state, parent_order_id=parent)


def xticket(key, instrument, side, qty, allocs, filled="0", closed=False) -> MirrorTicketState:
    """allocs: [(strategy order id, account, quantity)]."""
    queued = MirrorQueued(
        venue=VENUE, ticket_key=key, instrument=instrument, side=side, quantity=D(qty), order_type=OrderType.LIMIT,
        limit_price=D("1.00"), tif=TimeInForce.DAY,
        allocations=tuple(MirrorAllocation(o, a, D(q)) for o, a, q in allocs), at=T)
    return MirrorTicketState(queued=queued, filled=D(filled), closed=closed)


def xstate(account, positions=(), orders=(), filled=None) -> AccountState:
    return AccountState(
        account_id=account,
        positions=MappingProxyType({i: Position(account_id=account, instrument=i, quantity=D(q), avg_cost=D("1"))
                                    for i, q in positions}),
        orders=MappingProxyType({o.order_id: o for o in orders}),
        filled_quantity=MappingProxyType({k: D(v) for k, v in (filled or {}).items()}))


def xmirror(book=(), tickets=(), queued=(), refused=()) -> MirrorState:
    return MirrorState(venue=VENUE, tickets=MappingProxyType({t.key: t for t in tickets}),
                       book=MappingProxyType({(a, i): D(q) for a, i, q in book}),
                       queued_orders=MappingProxyType({k: "tos:x" for k in queued}),
                       refused_orders=MappingProxyType({k: "why" for k in refused}))


def inst_back(w):
    if w["kind"] == "combo":
        return Combo([ComboLeg(inst_back(l["contract"]), l["ratio"], Side(l["side"])) for l in w["legs"]])
    return instrument_of(w)


def xdoc(mirror, states, mirrored, session, name, prices, at) -> dict:
    return {"mirror": LCODEC.canon(mirror), "accounts": [[a, LCODEC.canon(s)] for a, s in states.items()],
            "mirrored": list(mirrored), "session": session.isoformat(), "name": name,
            "prices": [[wire(c), side.value, None if p is None else str(p)] for (c, side), p in prices.items()],
            "at": at.isoformat()}


def xorder_doc(o) -> dict:
    return {"order_id": o.order_id, "account_id": o.account_id, "instrument": wire(o.instrument),
            "order_type": o.order_type.value, "side": o.side.value, "quantity": str(o.quantity),
            "command_id": o.command_id, "created_at": o.created_at.isoformat(),
            "limit_price": None if o.limit_price is None else str(o.limit_price), "tif": o.tif.value}


def xplan_doc(p) -> dict:
    return {"cancel": list(p.cancel), "orders": [xorder_doc(o) for o in p.orders],
            "refused": [list(r) for r in p.refused], "waits": [list(w) for w in p.waits]}


def check_exit(label, mirror, states, prices, name="midday", at=XAT, mirrored=XMIRROR, session=XS):
    calls = []

    def price(contract, side):
        calls.append([wire(contract), side.value])
        return prices.get((contract, side))

    def oracle():
        FE.mirror_of = lambda ledger, venue: ledger.mirror
        plan = FE.plan_exits(XLedger(mirror, states), MirrorBinding(VENUE, "margin", tuple(mirrored), D("1000")),
                             session, name=name, price=price, at=at)
        return {**xplan_doc(plan), "priced": calls}

    got = step("exit", label, oracle,
               lambda: door("plan_exits", xdoc(mirror, states, mirrored, session, name, prices, at)))
    if got[0] == "ok":
        plan = got[1]
        seen("exit", "plan")
        for key in ("cancel", "orders", "refused", "waits", "priced"):
            if plan[key]:
                seen("exit", key)
        for o in plan["orders"]:
            seen("exit", "combo order" if o["instrument"]["kind"] == "combo" else "leg order")
            seen("exit", "target" if "@" in o["order_id"] else "close")
        for _, _, reason in plan["refused"]:
            for tag in ("a leg of a vertical", "shared with", "no price for it", "a legged book"):
                if tag in reason:
                    seen("exit", tag)
            if reason.startswith("the venue holds the two"):
                seen("exit", "legged venue")
            if reason.startswith("the sim holds the two"):
                seen("exit", "legged sim")
    else:
        seen("exit", got[1] + ": " + family(got[2]))
    return got


def xleg_units(vertical, n) -> list:
    """(contract, signed quantity) for n spreads of the vertical."""
    return [(l.contract, D(n) * l.ratio * (1 if l.side is Side.BUY else -1)) for l in vertical.legs]


def x_case(rng):
    """One session's state: a mirror, the sim's accounts, and the pass's quotes."""
    book, positions, orders, tickets = [], {a: [] for a in XMIRROR}, {a: [] for a in XMIRROR}, []
    filled = {a: {} for a in XMIRROR}
    queued, refused, ids = [], [], []
    keys = iter(f"tos:{n}" for n in range(100))
    for account in XMIRROR:
        for _ in range(rng.choice([0, 1, 1, 2])):
            mode = rng.choice(["leg", "leg", "vertical", "vertical", "vertical"])
            if mode == "leg":
                c = rng.choice(XFLAT)
                venue_q, sim_q = rng.choice(XUNITS), rng.choice(XUNITS + ["same"] * 4)
                sim_q = venue_q if sim_q == "same" else sim_q
                book.append((account, c, venue_q))
                if rng.random() < 0.85:
                    positions[account].append((c, sim_q))
                entry = xorder(f"e-{account}-{c.symbol[-8:]}", account, c,
                               Side.SELL if rng.random() < 0.7 else Side.BUY,
                               state=rng.choice([OrderState.FILLED, OrderState.ACCEPTED]))
                orders[account].append(entry)
                for t in range(rng.choice([0, 1, 1, 2])):
                    side = rng.choice([Side.BUY, Side.SELL])
                    target = xorder(f"t-{account}-{t}-{c.symbol[-8:]}", account, c, side,
                                    qty=rng.choice(["1", "2", "3", "0.5"]),
                                    state=rng.choice(XSTATES), parent=entry.order_id,
                                    tif=rng.choice([TimeInForce.GTC] * 4 + [TimeInForce.DAY]),
                                    otype=rng.choice([OrderType.LIMIT] * 6 + [OrderType.MARKET]),
                                    limit=rng.choice(["1.00", "0.95", "1E+0"]))
                    orders[account].append(target)
                    if rng.random() < 0.5:
                        filled[account][target.order_id] = rng.choice(["0", "0", "1", "0.5", "5"])
                    ids.append(target)
                for _ in range(rng.choice([0, 0, 1, 2])):
                    oid = entry.order_id if rng.random() < 0.6 else rng.choice(orders[account]).order_id
                    allocs = [(oid + rng.choice(["", "@" + XS.isoformat(), "@2026-09-29"]), account, "1")]
                    if rng.random() < 0.25:
                        allocs.append(("x-other", XOTHER, "1"))
                    tickets.append(xticket(next(keys), c, rng.choice([Side.BUY, Side.SELL]), str(len(allocs)), allocs,
                                           filled=rng.choice(["0", "0", "1"]), closed=rng.random() < 0.2))
            else:
                v = rng.choice(XVERTS)
                opening, closing = v, FE._flip(v)
                nv, ns = rng.choice(XUNITS), rng.choice(XUNITS + ["same"] * 5)
                ns = nv if ns == "same" else ns
                for contract, q in xleg_units(v, nv):
                    if rng.random() < 0.07:
                        q = q + 1                        # a legged book
                    book.append((account, contract, q))
                for contract, q in xleg_units(v, ns):
                    if rng.random() < 0.07:
                        q = q - 1
                    if rng.random() < 0.9:
                        positions[account].append((contract, q))
                entry = xorder(f"ve-{account}-{len(orders[account])}", account, opening, Side.SELL,
                               state=rng.choice(XSTATES))
                if rng.random() < 0.9:
                    orders[account].append(entry)
                if rng.random() < 0.5:
                    filled[account][entry.order_id] = rng.choice(["0", "1", "2"])
                for t in range(rng.choice([0, 1, 1, 2])):
                    target = xorder(f"vt-{account}-{t}", account, closing, rng.choice([Side.BUY] * 5 + [Side.SELL]),
                                    qty=rng.choice(["1", "2", "3"]), state=rng.choice(XSTATES), parent=entry.order_id,
                                    tif=rng.choice([TimeInForce.GTC] * 4 + [TimeInForce.DAY]))
                    orders[account].append(target)
                    if rng.random() < 0.4:
                        filled[account][target.order_id] = rng.choice(["0", "1", "0.5"])
                    ids.append(target)
                for inst in rng.sample([opening, closing, opening], rng.choice([0, 1, 1, 2])):
                    pool = [o.order_id for o in orders[account]] or ["none"]
                    oid = rng.choice(pool)
                    allocs = [(oid + rng.choice(["", "@" + XS.isoformat()]), account, "1")]
                    if rng.random() < 0.2:
                        allocs.append(("x-other", XOTHER, "1"))
                    tickets.append(xticket(next(keys), inst, Side.SELL if inst == opening else Side.BUY,
                                           str(len(allocs)), allocs, filled=rng.choice(["0", "0", "1"]),
                                           closed=rng.random() < 0.2))
    name = rng.choice(XNAMES[:4] * 3 + XNAMES)
    # handled ids: closes, refusals and rested targets this session already sent
    for account, c, _ in list(book):
        if rng.random() < 0.15:
            (queued if rng.random() < 0.6 else refused).append(
                FE.close_id(account, c, XS, name) if rng.random() < 0.6 else FE._refusal_id(account, c, XS))
    for account in XMIRROR:
        for o in orders[account]:
            if rng.random() < 0.1:
                queued.append(FE.target_id(o, XS))
    for account in XMIRROR:
        for v in XVERTS:
            if rng.random() < 0.08:
                queued.append(FE.close_id(account, FE._flip(v), XS, name))
            if rng.random() < 0.08:
                refused.append(FE._refusal_id(account, v, XS))
    prices = {}
    for c in XFLAT + XVERTS + [FE._flip(v) for v in XVERTS]:
        for side in (Side.BUY, Side.SELL):
            if rng.random() < 0.9:
                p = rng.choice(XPRICES)
                prices[(c, side)] = None if p is None else D(p)
    mirror = xmirror(book, tickets, queued, refused)
    states = {a: xstate(a, positions[a], orders[a], filled[a]) for a in XMIRROR if rng.random() < 0.93}
    return mirror, states, prices, name


def test_p5_t7_plan_exits_lockstep() -> None:
    rng = random.Random(7001)
    spread, closing = XVERTS[0], FE._flip(XVERTS[0])
    prices = {(P200, Side.BUY): D("0.60"), (P200, Side.SELL): D("0.60"), (P195, Side.SELL): D("0.60"),
              (P195, Side.BUY): D("0.60"), (spread, Side.BUY): D("1.10"), (closing, Side.BUY): D("1.10"),
              (AAPL, Side.SELL): D("190"), (AAPL, Side.BUY): D("190")}
    csp, ps = "OPT_CSP", "OPT_PUT_SPREAD"
    # the edge grid
    check_exit("empty", xmirror(), {}, {})
    check_exit("bad-name", xmirror(), {}, {}, name="noon")
    check_exit("bare-follow", xmirror(), {}, {}, name="follow-")
    check_exit("naive-at-empty", xmirror(), {}, {}, at=datetime(2026, 9, 28, 16, 35))
    check_exit("close", xmirror([(csp, P200, "-1")]), {}, prices)
    check_exit("close-naive", xmirror([(csp, P200, "-1")]), {}, prices, at=datetime(2026, 9, 28, 16, 35))
    check_exit("close-no-price", xmirror([(csp, P200, "-1")]), {csp: xstate(csp)}, {})
    check_exit("close-zero-price", xmirror([(csp, P200, "-1")]), {}, {(P200, Side.BUY): D("0")})
    check_exit("long-closes-by-sale", xmirror([(csp, AAPL, "100")]), {csp: xstate(csp, [(AAPL, "40")])}, prices)
    check_exit("sim-opposite", xmirror([(csp, AAPL, "100")]), {csp: xstate(csp, [(AAPL, "-40")])}, prices)
    check_exit("unmirrored-account", xmirror([(XOTHER, P200, "-1")]), {}, prices)
    entry = xorder("e1", csp, P200, Side.SELL, state=OrderState.FILLED)
    target = xorder("t1", csp, P200, Side.BUY, parent="e1")
    held = xmirror([(csp, P200, "-3")])
    sim = {csp: xstate(csp, [(P200, "-3")], [entry, target], {"t1": "1"})}
    check_exit("rest-target", held, sim, prices)
    check_exit("rest-target-sorted", held,
               {csp: xstate(csp, [(P200, "-3")], [entry, target, xorder("a0", csp, P200, Side.BUY, parent="e1", qty="5")])},
               prices)
    check_exit("target-handled", xmirror([(csp, P200, "-3")], queued=[FE.target_id(target, XS)]), sim, prices)
    check_exit("target-overfilled", held, {csp: xstate(csp, [(P200, "-3")], [entry, target], {"t1": "9"})}, prices)
    check_exit("target-room-caps", xmirror([(csp, P200, "-1")]),
               {csp: xstate(csp, [(P200, "-1")], [entry, xorder("t1", csp, P200, Side.BUY, qty="5", parent="e1")])},
               prices)
    check_exit("two-targets-share-room", xmirror([(csp, P200, "-3")]),
               {csp: xstate(csp, [(P200, "-3")], [entry, xorder("t1", csp, P200, Side.BUY, qty="2", parent="e1"),
                                                  xorder("t2", csp, P200, Side.BUY, qty="2", parent="e1")])}, prices)
    for label, o in [("tif-day", xorder("t1", csp, P200, Side.BUY, parent="e1", tif=TimeInForce.DAY)),
                     ("market", xorder("t1", csp, P200, Side.BUY, parent="e1", otype=OrderType.MARKET)),
                     ("wrong-side", xorder("t1", csp, P200, Side.SELL, parent="e1")),
                     ("no-parent", xorder("t1", csp, P200, Side.BUY)),
                     ("cancelled", xorder("t1", csp, P200, Side.BUY, parent="e1", state=OrderState.CANCELLED)),
                     ("other-contract", xorder("t1", csp, P195, Side.BUY, parent="e1")),
                     ("equal-contract", xorder("t1", csp, P200E, Side.BUY, parent="e1"))]:
        check_exit("not-a-target-" + label, held, {csp: xstate(csp, [(P200, "-3")], [entry, o])}, prices)
    resting = xticket("tos:r", P200, Side.BUY, "1", [("t1@2026-09-28", csp, "1")])
    check_exit("target-already-rests", xmirror([(csp, P200, "-3")], [resting]), sim, prices)
    check_exit("opposite-ticket-rests",
               xmirror([(csp, P200, "-3")], [xticket("tos:r", P200, Side.SELL, "1", [("z", csp, "1")])]), sim, prices)
    check_exit("close-cancels-resting", xmirror([(csp, P200, "-1")], [resting]),
               {csp: xstate(csp, [], [entry, target])}, prices)
    shared = xticket("tos:s", P200, Side.BUY, "3", [("a", csp, "1"), ("b", XOTHER, "1"), ("c", ps, "1")])
    check_exit("close-shared", xmirror([(csp, P200, "-1")], [shared]), {}, prices)
    check_exit("close-done-ticket", xmirror([(csp, P200, "-1")],
                                            [xticket("tos:d", P200, Side.BUY, "1", [("a", csp, "1")], closed=True)]),
               {}, prices)
    check_exit("close-handled", xmirror([(csp, P200, "-1")], queued=[FE.close_id(csp, P200, XS, "midday")]), {}, prices)
    check_exit("close-refused-once", xmirror([(csp, P200, "-1")], refused=[FE._refusal_id(csp, P200, XS)]), {}, {})
    check_exit("close-other-pass", xmirror([(csp, P200, "-1")], queued=[FE.close_id(csp, P200, XS, "morning")]),
               {}, prices)
    # verticals
    legs = [(csp, c, q) for c, q in xleg_units(spread, "2")]
    ventry = xorder("ve", csp, spread, Side.SELL, state=OrderState.FILLED)
    vticket = xticket("tos:v", spread, Side.SELL, "2", [("ve", csp, "2")], filled="2")

    def vsim(n, *orders, filled=None):
        return {csp: xstate(csp, xleg_units(spread, n), [ventry, *orders], filled)}

    vtarget = xorder("vt", csp, closing, Side.BUY, qty="2", parent="ve")
    check_exit("vertical-held", xmirror(legs, [vticket]), vsim("2"), prices)
    check_exit("vertical-target-rests", xmirror(legs, [vticket]), vsim("2", vtarget), prices)
    check_exit("vertical-sim-closed", xmirror(legs, [vticket]), vsim("0", vtarget), prices)
    check_exit("vertical-sim-half", xmirror(legs, [vticket]), vsim("1", vtarget), prices)
    check_exit("vertical-sim-short", xmirror(legs, [vticket]), vsim("-1"), prices)
    check_exit("vertical-no-price", xmirror(legs, [vticket]), vsim("0"), {})
    check_exit("vertical-zero-price", xmirror(legs, [vticket]), vsim("0"), {(closing, Side.BUY): D("0")})
    check_exit("vertical-shared",
               xmirror(legs, [vticket, xticket("tos:w", closing, Side.BUY, "2", [("p", csp, "1"), ("q", XOTHER, "1")])]),
               vsim("0"), prices)
    check_exit("vertical-legged-venue", xmirror([legs[0], (csp, legs[1][1], legs[1][2] + 1)], [vticket]),
               vsim("2"), prices)
    check_exit("vertical-legged-sim", xmirror(legs, [vticket]),
               {csp: xstate(csp, [(P200, "-2"), (P195, "1")], [ventry])}, prices)
    check_exit("vertical-leg-position-missing", xmirror(legs, [vticket]),
               {csp: xstate(csp, [(P200, "-2")], [ventry])}, prices)
    check_exit("vertical-close-handled",
               xmirror(legs, [vticket], queued=[FE.close_id(csp, closing, XS, "midday")]), vsim("0"), prices)
    entry_ticket = xticket("tos:v", spread, Side.SELL, "2", [("ve", csp, "2")])
    check_exit("vertical-entry-unworked", xmirror([], [entry_ticket]),
               {csp: xstate(csp, [], [xorder("ve", csp, spread, Side.SELL, state=OrderState.CANCELLED)])}, prices)
    check_exit("vertical-entry-worked", xmirror([], [entry_ticket]),
               {csp: xstate(csp, [], [xorder("ve", csp, spread, Side.SELL, state=OrderState.ACCEPTED)])}, prices)
    check_exit("vertical-entry-ended-unfilled", xmirror([], [entry_ticket]),
               {csp: xstate(csp, xleg_units(spread, "1"),
                            [xorder("ve", csp, spread, Side.SELL, state=OrderState.CANCELLED)])}, prices)
    check_exit("vertical-entry-filled-keeps", xmirror([], [entry_ticket]),
               {csp: xstate(csp, xleg_units(spread, "1"),
                            [xorder("ve", csp, spread, Side.SELL, state=OrderState.CANCELLED)], {"ve": "1"})}, prices)
    vt_ticket = xticket("tos:t", closing, Side.BUY, "2", [("vt@2026-09-28", csp, "2")])
    check_exit("vertical-target-cancelled", xmirror(legs, [vticket, vt_ticket]),
               vsim("2", xorder("vt", csp, closing, Side.BUY, qty="2", parent="ve", state=OrderState.CANCELLED)), prices)
    check_exit("vertical-target-rests-already", xmirror(legs, [vticket, vt_ticket]), vsim("2", vtarget), prices)
    check_exit("vertical-no-entry-order", xmirror(legs, [vticket]),
               {csp: xstate(csp, xleg_units(spread, "2"), [])}, prices)
    check_exit("vertical-entry-is-a-child", xmirror(legs, [vticket]),
               {csp: xstate(csp, xleg_units(spread, "2"), [xorder("ve", csp, spread, Side.SELL, parent="p")])}, prices)
    check_exit("vertical-unmirrored", xmirror([(XOTHER, c, q) for c, q in xleg_units(spread, "2")],
                                              [xticket("tos:v", spread, Side.SELL, "2", [("ve", XOTHER, "2")])]),
               {}, prices)
    check_exit("vertical-leg-alone", xmirror([(csp, P200, "-2")], [vticket]), vsim("0"), prices)
    check_exit("two-accounts-one-pair", xmirror(legs + [(ps, c, q) for c, q in xleg_units(spread, "1")],
                                                [vticket, xticket("tos:v2", spread, Side.SELL, "1", [("ve2", ps, "1")])]),
               {csp: vsim("0")[csp], ps: xstate(ps, [], [xorder("ve2", ps, spread, Side.SELL)])}, prices)
    check_exit("leg-in-vertical-no-entry", xmirror([(csp, P200, "-1"), (csp, P195, "1")],
                                                   [xticket("tos:v", spread, Side.SELL, "1", [("zz", csp, "1")])]),
               {}, prices)
    check_exit("vertical-follow", xmirror(legs, [vticket]), vsim("0"), prices, name="follow-1235")
    for n in range(4200):                                    # seeded
        mirror, states, quotes, name = x_case(rng)
        check_exit(n, mirror, states, quotes, name=name,
                   at=XAT if rng.random() < 0.97 else datetime(2026, 9, 28, 16, 35))
    c = SEEN["exit"]
    for want in ("plan", "cancel", "orders", "refused", "waits", "priced", "combo order", "leg order", "target", "close",
                 "a leg of a vertical", "shared with", "no price for it", "a legged book", "legged venue",
                 "legged sim"):
        assert c[want] > 25, (want, sorted(c.items()))
    settle("exit", 4200, ["ExitPlanError: Unknown pass", "ValueError: Order created_at must be timezone-aware"])


def test_p5_t7_helpers_lockstep() -> None:
    rng = random.Random(7002)
    csp, ps = "OPT_CSP", "OPT_PUT_SPREAD"
    for n in range(300):                                     # ids
        account, c = rng.choice(["OPT_CSP", "O'Q", "é"]), rng.choice(XFLAT + XVERTS)
        name = rng.choice(XNAMES)
        o = xorder(rng.choice(["t-1", "x@y", "e"]), csp, P200, Side.BUY)
        for which, py in (("close", FE.close_id(account, c, XS, name)), ("target", FE.target_id(o, XS)),
                          ("refusal", FE._refusal_id(account, c, XS))):
            doc = {"which": which, "session": XS.isoformat(), "account": account, "instrument": wire(c), "name": name,
                   "order_id": o.order_id}
            assert door("exit_id", doc) == {"id": py}, (which, n)
            seen("exit_id", which)
    odd = [combo((P200, 1, Side.SELL), (P195, 2, Side.BUY)), combo((AAPL, 100, Side.BUY), (C200, 1, Side.SELL))]
    for v in XVERTS + odd:                                   # _flip
        got = step("exit_flip", v, lambda v=v: wire(FE._flip(v)), lambda v=v: door("exit_flip", {"combo": wire(v)}))
        assert got[0] == "ok"
    ones = ["0", "1", "-1", "2", "-2", "1.0", "1.00", "0.5", "-0", "3", "100", "-100"]
    for n in range(1200):                                    # _units
        v = rng.choice(XVERTS + odd + [combo((P200, 1, Side.SELL), (P200E, 1, Side.BUY))])
        held = {}
        for l in v.legs:
            if rng.random() < 0.9:
                q = D(rng.choice(ones))
                held[l.contract] = q if rng.random() < 0.5 else q * l.ratio * (1 if l.side is Side.BUY else -1)
        got = step("exit_units", n, lambda: (lambda u: None if u is None else str(u))(FE._units(held, v)),
                   lambda: door("exit_units", {"combo": wire(v), "held": pairs_doc(held)})["units"])
        seen("exit_units", "none" if got[1] is None else "units")
    assert SEEN["exit_units"]["units"] > 100 and SEEN["exit_units"]["none"] > 100, SEEN["exit_units"]
    settle("exit_flip", 6, [])
    settle("exit_units", 1200, [])
    for n in range(1500):          # _open_on, _in_vertical, _sim_orders, _verticals, _plan_verticals on states
        mirror, states, prices, name = x_case(rng)
        base = xdoc(mirror, states, XMIRROR, XS, name, prices, XAT)
        account = rng.choice([csp, ps, XOTHER])
        contract = rng.choice(XFLAT + [P200E])
        want = [t.key for t in FE._open_on(mirror, account, contract)]
        got = door("exit_open_on", {**base, "which": "open_on", "account": account, "contract": wire(contract)})
        assert got["keys"] == want, (n, got, want)
        got = door("exit_open_on", {**base, "which": "in_vertical", "account": account, "contract": wire(contract)})
        assert got["in_vertical"] == FE._in_vertical(mirror, account, contract), n
        seen("exit_open", (bool(want), got["in_vertical"]))
        for key, ticket in mirror.tickets.items():
            state = XLedger(mirror, states).state(account)
            want = [o.order_id for o in FE._sim_orders(state, ticket, XS)]
            assert door("exit_sim_orders", {**base, "ticket": key, "account": account}) == want, (n, key)
            seen("exit_sim", bool(want))
        ledger = XLedger(mirror, states)
        binding = MirrorBinding(VENUE, "margin", XMIRROR, D("1000"))
        want = [[a, wire(c), [t.key for t in ts]] for (a, c), ts in FE._verticals(ledger, binding, mirror).items()]
        assert door("exit_verticals", base) == want, n
        seen("exit_verticals", bool(want))
        cancel, orders, refused, waits, calls = [], [], [], [], []

        def price(c, side):
            calls.append([wire(c), side.value])
            return prices.get((c, side))

        covered = FE._plan_verticals(ledger, binding, mirror, XS, name, price, XAT, cancel, orders, refused, waits)
        got = door("plan_verticals", base)
        assert got["cancel"] == cancel and got["refused"] == [list(r) for r in refused], n
        assert got["waits"] == [list(w) for w in waits] and got["orders"] == [xorder_doc(o) for o in orders], n
        assert got["priced"] == calls, n
        key = lambda row: json.dumps(row, sort_keys=True)  # noqa: E731
        assert sorted({key(r) for r in got["covered"]}) == sorted({key([a, wire(c)]) for a, c in covered}), n
    for tally, want in (("exit_id", 3), ("exit_open", 3), ("exit_sim", 2), ("exit_verticals", 2)):
        assert len(SEEN[tally]) >= want, (tally, SEEN[tally])
    assert SEEN["exit_open"][(True, False)] > 20 and SEEN["exit_open"][(False, True)] > 20, SEEN["exit_open"]


def door_plan_exits(ledger, binding, session, *, name, price, at):
    mirror = PE.mirror_of(ledger, binding.venue_account)
    accounts = {a: ledger.state(a) for a in binding.mirrored_accounts}
    asked = [c for (_, c) in mirror.book]
    for t in mirror.tickets.values():
        asked.append(t.queued.instrument)
        if isinstance(t.queued.instrument, Combo):
            asked.append(FE._flip(t.queued.instrument))
    quotes = {}
    for c in asked:
        for side in (Side.BUY, Side.SELL):
            quotes[(c, side)] = price(c, side)
    if hasattr(price, "seen"):
        price.seen.clear()
    r = door("plan_exits", xdoc(mirror, accounts, binding.mirrored_accounts, session, name, quotes, at))
    for w, side in r["priced"]:
        price(inst_back(w), Side(side))
    orders = tuple(Order(order_id=o["order_id"], account_id=o["account_id"], instrument=inst_back(o["instrument"]),
                         order_type=OrderType(o["order_type"]), side=Side(o["side"]), quantity=D(o["quantity"]),
                         command_id=o["command_id"], created_at=datetime.fromisoformat(o["created_at"]),
                         limit_price=None if o["limit_price"] is None else D(o["limit_price"]),
                         tif=TimeInForce(o["tif"])) for o in r["orders"])
    return PE.ExitPlan(cancel=tuple(r["cancel"]), orders=orders, refused=tuple(tuple(x) for x in r["refused"]),
                       waits=tuple(tuple(x) for x in r["waits"]))


def run_module_with_ledger(module, tmp_path, only=None) -> int:
    """``run_module_tests`` for a module whose tests take the ``ledger`` fixture (a fresh one each)."""
    from trade_engine.ledger import Ledger

    ran = 0
    for name, fn in sorted(vars(module).items()):
        if not name.startswith("test_") or not inspect.isfunction(fn) or (only and name not in only):
            continue
        fixtures = [p for p in inspect.signature(fn).parameters if p in ("ledger", "tmp_path", "books")]
        assert fixtures in ([], ["ledger"], ["tmp_path"], ["books"]), (name, fixtures)
        for args in parametrized(fn):
            ran += 1
            if fixtures == ["ledger"]:
                with Ledger(tmp_path / f"{ran}.db") as lg:
                    fn(lg, *args)
            elif fixtures == ["books"]:      # test_tos_follow's: the sim, the split view, the follower's own
                d = tmp_path / str(ran)
                d.mkdir()
                with Ledger(d / "sim.db") as sim, Ledger(d / "mirror.db") as own:
                    reader = module.LedgerReader(d / "sim.db").open()
                    try:
                        fn((sim, module.SplitLedger(reader, own, module.ACCOUNTS), own), *args)
                    finally:
                        reader.close()
            elif fixtures == ["tmp_path"]:
                (tmp_path / str(ran)).mkdir()
                fn(tmp_path / str(ran), *args)
            else:
                fn(*args)
    return ran


def test_p5_existing_exits_vectors_through_the_door(monkeypatch, tmp_path) -> None:
    import test_tos_mirror_exits as V

    monkeypatch.setattr(V, "plan_exits", door_plan_exits)
    monkeypatch.setattr(PE, "plan_exits", door_plan_exits)
    ran = run_module_with_ledger(V, tmp_path)
    assert ran >= 30, ran


# -- T8 follow -------------------------------------------------------------------------------

from frozen_p5 import follow as FF  # noqa: E402
from trade_engine.tos_paper import follow as PF  # noqa: E402

FMIRROR = (CCA, PMA)
FBASE = datetime(2026, 9, 28, 14, 0, tzinfo=UTC)
FOPEN = FBASE - timedelta(hours=1)
FAGES = [300_000_000, 300_000_000, 0, 1_000_000, 60_500_000, 300_000_001, 299_999_999, -1_500_000, 10 ** 12, 301_999_999]
FTZ = [UTC, UTC, timezone(timedelta(hours=-4)), timezone(timedelta(hours=5, minutes=30)),
       timezone(timedelta(hours=-5, seconds=-17))]


def us(n):
    return timedelta(microseconds=n)


def forder(oid, account, instrument, side, qty="1", state=OrderState.ACCEPTED, parent=None, at=FBASE):
    return Order(order_id=oid, account_id=account, instrument=instrument, order_type=OrderType.LIMIT, side=side,
                 quantity=D(qty), command_id=oid, created_at=at, limit_price=D("1.00"), tif=TimeInForce.DAY,
                 state=state, parent_order_id=parent)


def fdoc(mirror, states, now, session_open, max_age, mirrored=FMIRROR) -> dict:
    return {"mirror": LCODEC.canon(mirror), "accounts": [[a, LCODEC.canon(s)] for a, s in states.items()],
            "mirrored": list(mirrored), "now": now.isoformat(), "session_open": session_open.isoformat(),
            "max_age_us": max_age // timedelta(microseconds=1)}


def check_follow(label, mirror, states, now, session_open=FOPEN, max_age=timedelta(minutes=5), mirrored=FMIRROR):
    def oracle():
        FF.mirror_of = lambda ledger, venue: ledger.mirror
        send, refused = FF.follow_entries(XLedger(mirror, states), MirrorBinding(VENUE, "margin", tuple(mirrored), D("1000")),
                                          session_open=session_open, now=now, max_age=max_age)
        return {"send": [[o.account_id, o.order_id] for o in send], "refused": [list(r) for r in refused]}

    got = step("follow", label, oracle,
               lambda: door("follow_entries", fdoc(mirror, states, now, session_open, max_age, mirrored)))
    if got[0] == "ok":
        if got[1]["send"]:
            seen("follow", "sent")
        for _, _, reason in got[1]["refused"]:
            for tag in ("a late copy", "did not arrive within", "opened and closed"):
                if tag in reason:
                    seen("follow", tag)
    return got


def f_case(rng):
    now = FBASE.astimezone(rng.choice(FTZ)) + us(rng.choice([0, 0, 123456, 999999]))
    max_age = us(rng.choice(FAGES[:8] + [300_000_000]))
    books, tickets, positions, orders = [], [], {a: [] for a in FMIRROR}, {a: [] for a in FMIRROR}
    queued, refused = [], []
    c210, c215, leaps = ccall("210"), ccall("215"), ccall("200", DEC)
    pool = [AAPL, c210, c210, c215, leaps, MSFT, ccall("400", und="MSFT"), P200, XVERTS[0], XVERTS[1], combo((c210, 1, Side.SELL), (c215, 1, Side.BUY))]
    for account in FMIRROR:
        if rng.random() < 0.7:
            books.append((account, AAPL, rng.choice(["0", "50", "100", "200", "99", "-100"])))
        if rng.random() < 0.3:
            books.append((account, c210, rng.choice(["-1", "1", "-2"])))
        if rng.random() < 0.2:
            books.append((account, leaps, "1"))
        for k in range(rng.choice([0, 1, 2, 2, 3, 4])):
            inst = rng.choice(pool)
            base = max_age if rng.random() < 0.6 else us(rng.choice(FAGES))
            delta = base + us(rng.choice([0, 0, -1, 1, 1_000_000, -1_000_000, 0]))
            if rng.random() < 0.1:
                delta = timedelta(hours=2)               # before the session open
            created = (now - delta).astimezone(rng.choice(FTZ))
            order = forder(f"o{k}", account, inst, rng.choice([Side.SELL] * 3 + [Side.BUY]),
                           qty=rng.choice(["1", "1", "2", "100", "0.5"]),
                           state=rng.choice([OrderState.ACCEPTED] * 2 + XSTATES),
                           parent="p" if rng.random() < 0.1 else None, at=created)
            orders[account].append(order)
            if rng.random() < 0.7:
                held = rng.choice(["0", "1", "2", "-1", "100", "0.0"])
                if isinstance(inst, Combo):
                    positions[account] += [(c, D(held) * l.ratio * (1 if l.side is Side.BUY else -1))
                                           for l in inst.legs for c in [l.contract]]
                else:
                    positions[account].append((inst, held))
            if rng.random() < 0.12:
                (queued if rng.random() < 0.6 else refused).append(order.order_id)
        if rng.random() < 0.3:
            tickets.append(tstate(AAPL, Side.BUY, "100", key=f"tos:{account}", account=account))
    mirror = xmirror(books, tickets, queued, refused)
    states = {a: xstate(a, positions[a], orders[a]) for a in FMIRROR if rng.random() < 0.95}
    return mirror, states, now, max_age


def test_p5_t8_follow_entries_lockstep() -> None:
    rng = random.Random(8001)
    csp, ps = "OPT_CSP", "OPT_PUT_SPREAD"
    c210 = ccall("210")
    S, B = Side.SELL, Side.BUY
    # the edge grid: the max_age boundary to the microsecond, the session open, every ended state
    for edge in (0, 1, -1, 999_999, 1_000_000, -1_000_000):
        order = forder("o1", CCA, AAPL, B, at=FBASE - us(300_000_000 + edge))
        check_follow(f"age-{edge}", xmirror(), {CCA: xstate(CCA, [], [order])}, FBASE)
    check_follow("age-zero-max", xmirror(), {CCA: xstate(CCA, [], [forder("o1", CCA, AAPL, B)])}, FBASE, max_age=us(0))
    check_follow("age-negative-max", xmirror(), {CCA: xstate(CCA, [], [forder("o1", CCA, AAPL, B, at=FBASE - us(2_000_000))])},
                 FBASE, max_age=us(-1_500_000))
    check_follow("age-huge", xmirror(), {CCA: xstate(CCA, [], [forder("o1", CCA, AAPL, B, at=FBASE - timedelta(days=400))])}, FBASE)
    check_follow("future-order", xmirror(), {CCA: xstate(CCA, [], [forder("o1", CCA, AAPL, B, at=FBASE + us(5))])}, FBASE)
    check_follow("open-boundary", xmirror(), {CCA: xstate(CCA, [], [forder("o1", CCA, AAPL, B, at=FOPEN),
                                                                       forder("o2", CCA, AAPL, B, at=FOPEN - us(1))])},
                 FBASE, session_open=FOPEN, max_age=timedelta(days=1))
    for state in OrderState:
        check_follow("state-" + state.value, xmirror(), {CCA: xstate(CCA, [], [forder("o1", CCA, AAPL, B, state=state)])}, FBASE)
    check_follow("child", xmirror(), {CCA: xstate(CCA, [], [forder("o1", CCA, AAPL, B, parent="p")])}, FBASE)
    check_follow("handled", xmirror(queued=["o1"], refused=["o2"]),
                 {CCA: xstate(CCA, [], [forder("o1", CCA, AAPL, B), forder("o2", CCA, AAPL, B), forder("o3", CCA, AAPL, B)])}, FBASE)
    check_follow("sorted", xmirror(), {CCA: xstate(CCA, [], [forder("b", CCA, AAPL, B), forder("a", CCA, AAPL, B)]),
                                       PMA: xstate(PMA, [], [forder("a", PMA, AAPL, B)])}, FBASE)
    check_follow("no-state", xmirror(), {}, FBASE)
    check_follow("unmirrored", xmirror(), {"OPT_X": xstate("OPT_X", [], [forder("a", "OPT_X", AAPL, B)])}, FBASE)
    # flat: a filled entry with no position, with one, a vertical by its spreads, a legged vertical
    spread = XVERTS[0]
    for label, positions in (("none", []), ("held", [(AAPL, "100")]), ("zero", [(AAPL, "0")]), ("neg-zero", [(AAPL, "-0")])):
        check_follow("flat-" + label, xmirror(), {CCA: xstate(CCA, positions, [forder("o1", CCA, AAPL, B, state=OrderState.FILLED)])}, FBASE)
    for label, positions in (("none", []), ("held", xleg_units(spread, "2")), ("closed", xleg_units(spread, "0")),
                             ("legged", [(P200, "-2"), (P195, "1")]), ("one-leg", [(P200, "-2")])):
        check_follow("vflat-" + label, xmirror(),
                     {CCA: xstate(CCA, positions, [forder("o1", CCA, spread, S, state=OrderState.FILLED)])}, FBASE)
    # the late entry's own reason: what the cover lacked
    late = FBASE - timedelta(minutes=10)
    check_follow("late-bare-call", xmirror(), {CCA: xstate(CCA, [], [forder("o1", CCA, c210, S, at=late)])}, FBASE)
    check_follow("late-covered-call", xmirror([(CCA, AAPL, "100")]), {CCA: xstate(CCA, [], [forder("o1", CCA, c210, S, at=late)])}, FBASE)
    check_follow("late-put", xmirror(), {CCA: xstate(CCA, [], [forder("o1", CCA, P200, S, at=late)])}, FBASE)
    check_follow("late-shares", xmirror([(CCA, AAPL, "100"), (CCA, c210, "-1")]),
                 {CCA: xstate(CCA, [], [forder("o1", CCA, AAPL, S, "100", at=late)])}, FBASE)
    for n in range(2600):                                    # seeded
        mirror, states, now, max_age = f_case(rng)
        check_follow(n, mirror, states, now, max_age=max_age)
    c = SEEN["follow"]
    for want in ("sent", "a late copy", "did not arrive within", "opened and closed"):
        assert c[want] > 40, (want, sorted(c.items()))
    settle("follow", 2600, [])


def test_p5_t8_pass_name_and_flat_lockstep() -> None:
    rng = random.Random(8002)
    edges = [datetime(2026, 3, 8, 6, 59, 59, tzinfo=UTC), datetime(2026, 3, 8, 7, 0, tzinfo=UTC),
             datetime(2026, 11, 1, 5, 59, 59, tzinfo=UTC), datetime(2026, 11, 1, 6, 0, tzinfo=UTC),
             datetime(2026, 11, 1, 5, 30, tzinfo=UTC), datetime(2026, 11, 1, 6, 30, tzinfo=UTC),
             datetime(2026, 12, 31, 23, 59, 59, 999999, tzinfo=UTC), datetime(1, 1, 1, 12, tzinfo=UTC),
             datetime(9999, 12, 31, 12, tzinfo=UTC), datetime(2026, 9, 28, 13, 30, tzinfo=UTC)]
    for n in range(1200):
        base = datetime(2024, 1, 1, tzinfo=UTC) + timedelta(seconds=rng.randrange(0, 3 * 365 * 86400),
                                                              microseconds=rng.choice([0, 999999, 5]))
        now = (edges[n] if n < len(edges) else base).astimezone(rng.choice(FTZ))
        got = step("pass_name", n, lambda: FF.pass_name(now), lambda: door("pass_name", {"now": now.isoformat()})["name"])
    settle("pass_name", 1200, [])
    with pytest.raises(Exception) as caught:                  # a naive clock is a host bug, refused at the door
        door("pass_name", {"now": "2026-09-28T16:35:00"})
    assert "timezone-aware" in str(caught.value)
    for n in range(1500):
        mirror, states, now, max_age = f_case(rng)
        for account, state in states.items():
            for order in state.orders.values():
                step("flat", (n, order.order_id), lambda: FF._flat(state, order),
                     lambda: door("follow_flat", {"account": LCODEC.canon(state), "order_id": order.order_id})["flat"])
                seen("flat", FF._flat(state, order) if not isinstance(order.instrument, Combo) else "combo-" + str(FF._flat(state, order)))
    c = SEEN["flat"]
    assert c[True] > 100 and c[False] > 100 and c["combo-True"] > 20 and c["combo-False"] > 20, c
    settle("flat", 1500, [])


def test_p5_existing_follow_vectors_through_the_door(monkeypatch, tmp_path) -> None:
    import test_tos_follow as V

    def follow_entries(ledger, binding, *, session_open, now, max_age=PF.MAX_AGE):
        mirror = PE.mirror_of(ledger, binding.venue_account)
        states = {a: ledger.state(a) for a in binding.mirrored_accounts}
        r = door("follow_entries", fdoc(mirror, states, now, session_open, max_age, binding.mirrored_accounts))
        send = tuple(states[a].orders[o] for a, o in r["send"])
        return send, tuple(tuple(x) for x in r["refused"])

    def pass_name(now):
        return door("pass_name", {"now": now.isoformat()})["name"]

    monkeypatch.setattr(V, "follow_entries", follow_entries)
    monkeypatch.setattr(V, "pass_name", pass_name)
    monkeypatch.setattr(PF, "follow_entries", follow_entries)
    monkeypatch.setattr(PF, "pass_name", pass_name)
    monkeypatch.setattr(PE, "plan_exits", door_plan_exits)
    ran = run_module_with_ledger(V, tmp_path)
    assert ran >= 20, ran

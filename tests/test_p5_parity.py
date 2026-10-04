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
from datetime import date, datetime, timezone
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

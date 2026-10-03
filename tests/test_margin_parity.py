"""P1c parity: the Rust margin rules against the frozen pre-port Python, on a dense grid.

`tests/frozen_margin/` is the frozen oracle, do not edit, from commit 72cd586 (the
pre-port `metrics/margin.py` and `option_margin.py`). Every case runs through the oracle
and through the shim over `trade_engine_rs`; results must be EXACTLY equal (Decimal
equality, same exception type and message on refusal). A missing `trade_engine_rs` is
an error, never a skip.
"""

from __future__ import annotations

import itertools
import random
from dataclasses import fields, is_dataclass
from datetime import date
from decimal import Decimal
from types import MappingProxyType

import pytest
from frozen_margin import oracle_margin as old
from frozen_margin import oracle_option_margin as old_opt

from trade_engine.domain.instruments import Combo, ComboLeg, Equity, OptionContract, OptionRight, Side
from trade_engine.domain.portfolio import Position
from trade_engine.ledger.state import AccountState
from trade_engine.metrics import margin as new
from trade_engine.metrics import option_margin as new_opt

D = Decimal
NEAR, MID, FAR = date(2026, 10, 16), date(2027, 1, 15), date(2027, 6, 17)
ROOTS = ("AAPL", "SPX", "ZZZZ")  # equity option, cash-settled index, a root nobody knows


def norm(x):
    if is_dataclass(x) and not isinstance(x, type):
        return (type(x).__name__, tuple(norm(getattr(x, f.name)) for f in fields(x)))
    if isinstance(x, (tuple, list)):
        return tuple(norm(i) for i in x)
    return x


def outcome(fn):
    try:
        return ("ok", norm(fn()))
    except Exception as err:  # noqa: BLE001 - the type and message ARE the contract
        return ("raise", type(err).__name__, str(err))


def same(old_fn, new_fn):
    a, b = outcome(old_fn), outcome(new_fn)
    assert a == b


def opt(right, strike, expiry=NEAR, root="AAPL", mult=100):
    return OptionContract(root, expiry, D(strike), OptionRight(right), mult)


def state(holdings, marks, cash="0", entries=None):
    entries = entries or {}
    positions = {i: Position("ACC", i, D(str(q)), D(entries.get(i, "1"))) for i, q in holdings.items()}
    return AccountState(
        account_id="ACC",
        cash=D(cash),
        positions=MappingProxyType(positions),
        marks=MappingProxyType({i: D(str(p)) for i, p in marks.items()}),
    )


def random_state(rng: random.Random):
    root = rng.choice(ROOTS)
    under = Equity(root)
    contracts = {
        opt(rng.choice("CP"), str(rng.choice((80, 90, 95, 100, 105, 110, 120))), rng.choice((NEAR, MID, FAR)), root)
        for _ in range(rng.randint(0, 5))
    }
    holdings = {c: rng.choice((-3, -2, -1, 1, 2, 3, 0)) for c in contracts}
    marks = {c: rng.choice(("0.5", "1.25", "3", "7.5", "12.05")) for c in contracts if rng.random() < 0.95}
    if rng.random() < 0.7:
        holdings[under] = rng.choice((-300, -100, 100, 200, 300, 150, "0.5"))
        if rng.random() < 0.95:
            marks[under] = rng.choice(("97.5", "100", "104.25", "88"))
    if rng.random() < 0.1:
        holdings[Equity("MSFT")] = 10
        marks[Equity("MSFT")] = "400"
    entries = {c: rng.choice(("0.25", "2", "9")) for c in contracts}
    prices = rng.choice((None, {root: D("100")}, {root: D("93.5")}))
    return state(holdings, marks, cash=rng.choice(("0", "5000", "-2500.5")), entries=entries), prices


@pytest.mark.parametrize("seed", range(12))
def test_account_margin_matches_the_oracle(seed):
    rng = random.Random(seed)
    for _ in range(400):
        st, prices = random_state(rng)
        ov_new = {"AAPL": new.MarginOverride(D("0.4"), D("0.3"))} if rng.random() < 0.2 else None
        ov_old = {"AAPL": old.MarginOverride(D("0.4"), D("0.3"))} if ov_new else None
        same(lambda: old.account_margin(st, ov_old, prices), lambda: new.account_margin(st, ov_new, prices))


def test_margin_used_and_available_match():
    rng = random.Random(99)
    checked = 0
    for _ in range(300):
        st, prices = random_state(rng)
        try:
            a = old.account_margin(st, None, prices)
        except Exception:  # noqa: BLE001
            continue
        b = new.account_margin(st, None, prices)
        assert (a.margin_used, a.margin_available) == (b.margin_used, b.margin_available)
        checked += 1
    assert checked > 50


QUANTITIES = ("0", "1", "-1", "100", "-250", "3.5", "-0.01", "123456.789")
MARKS = ("0", "0.01", "1", "99.99", "1234.5678", "-5")


@pytest.mark.parametrize(
    "override",
    [None, (D("0.5"), D("0.25")), (D("1"), D("0")), (D("0.3"), D("0.4")), (D("-1"), D("0.1")), (D("0.2"), D("1.5"))],
)
def test_margin_requirement_grid(override):
    eq = Equity("AAPL")
    for q, m in itertools.product(QUANTITIES, MARKS):
        same(
            lambda: old.margin_requirement(eq, D(q), D(m), None if override is None else old.MarginOverride(*override)),
            lambda: new.margin_requirement(eq, D(q), D(m), None if override is None else new.MarginOverride(*override)),
        )


def test_override_validation_matches():
    vals = [D(v) for v in ("-1", "0", "0.1", "0.5", "1", "1.01")]
    for i, m in itertools.product(vals, vals):
        same(lambda: old.MarginOverride(i, m), lambda: new.MarginOverride(i, m))


def _shape(defs):
    return [(d.name, d.underlying_lots, d.leg_count, [(lg.right.value, lg.quantity, len(lg.predicates)) for lg in d.legs]) for d in defs]


def test_constants_and_definitions_match():
    assert (old.INITIAL_FRACTION, old.MAINTENANCE_FRACTION) == (new.INITIAL_FRACTION, new.MAINTENANCE_FRACTION)
    assert _shape(old_opt.DEFINITIONS) == _shape(new_opt.DEFINITIONS)
    for n in (
        "NAKED_FLOOR",
        "EQUITY_OTM_FRACTION",
        "INDEX_OTM_FRACTION",
        "COVERED_CALL_CALL_FRACTION",
        "COLLAR_CALL_FRACTION",
        "SEARCH_LIMIT",
    ):
        assert getattr(old_opt, n) == getattr(new_opt, n), n


def test_naked_margin_grid():
    for root, right, strike, n, under, mark in itertools.product(
        ROOTS, "CP", ("50", "100", "150"), (-3, -1, 0, 2), ("0", "99", "100", "180"), ("0", "0.5", "20")
    ):
        c = opt(right, strike, root=root)
        same(lambda: old_opt.naked_margin(c, n, D(under), D(mark)), lambda: new_opt.naked_margin(c, n, D(under), D(mark)))


def book_cases():
    legs = [opt(r, s, e) for r in "CP" for s in ("90", "100", "110") for e in (NEAR, FAR)]
    rng = random.Random(7)
    for _ in range(500):
        chosen = rng.sample(legs, rng.randint(1, 4))
        yield {c: rng.choice((-2, -1, 1, 2)) for c in chosen}, rng.choice((0, 0, 1, -1, 2))


def test_match_strategies_matches():
    for book, lots in book_cases():
        same(lambda: old_opt.match_strategies(book, lots), lambda: new_opt.match_strategies(book, lots))


def test_match_strategies_with_a_custom_definition_table_matches():
    own_old = tuple(d for d in old_opt.DEFINITIONS if d.leg_count == 1)
    own_new = tuple(d for d in new_opt.DEFINITIONS if d.leg_count == 1)
    for book, lots in book_cases():
        same(lambda: old_opt.match_strategies(book, lots, own_old), lambda: new_opt.match_strategies(book, lots, own_new))


def test_margin_book_matches_at_each_search_limit(monkeypatch):
    for limit in (20000, 1, 5):
        monkeypatch.setattr(old_opt, "SEARCH_LIMIT", limit)
        monkeypatch.setattr(new_opt, "SEARCH_LIMIT", limit)
        for n, (book, lots) in enumerate(book_cases()):
            marks = {c: D("2.5") for c in book}
            entries = {c: D("1.5") for c in book} if n % 2 else None
            shares = D(lots * 100)
            same(
                lambda: old_opt.margin_book("AAPL", book, shares, D("100"), marks, D("0.5"), D("0.25"), entries),
                lambda: new_opt.margin_book("AAPL", book, shares, D("100"), marks, D("0.5"), D("0.25"), entries),
            )


def test_margin_book_refusals_match():
    c = opt("C", "100")
    mixed = opt("C", "100", mult=10)
    z = opt("C", "100", root="ZZZZ")
    cases = [
        ({}, "AAPL", {}),
        ({c: -1}, "AAPL", {}),
        ({c: -1, mixed: 1}, "AAPL", {c: D("1"), mixed: D("1")}),
        ({z: -1}, "ZZZZ", {z: D("1")}),
    ]
    for book, under, marks in cases:
        same(
            lambda: old_opt.margin_book(under, book, D("0"), D("100"), marks, D("0.5"), D("0.25")),
            lambda: new_opt.margin_book(under, book, D("0"), D("100"), marks, D("0.5"), D("0.25")),
        )


def test_account_refusals_match():
    c = opt("C", "100")
    aapl = Equity("AAPL")
    combo = Combo([ComboLeg(opt("P", "95"), 1, Side.SELL), ComboLeg(opt("P", "90"), 1, Side.BUY)])
    cases = [
        state({aapl: 10}, {}),
        state({c: -1}, {c: 1}),
        state({c: -1}, {aapl: 100}),
        state({c: -1}, {c: 1, aapl: 100}, entries={c: "2"}),
        state({aapl: 0}, {}),
        state({combo: 1}, {combo: 5}),
    ]
    for st in cases:
        same(lambda: old.account_margin(st), lambda: new.account_margin(st))

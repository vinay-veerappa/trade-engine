"""P1e gate: the Rust option rules agree with the frozen pre-port Python, exactly.

`tests/frozen_option_rules/` is the frozen oracle, do not edit: the three domain modules
verbatim from commit 0f26fb3 (`instruments`, `option_roots`, `option_lifecycle`). The
new modules are shims over `trade_engine_rs`; everything here runs both and demands
EQUALITY of the outcome: the value (Decimal compared by `str`, so scale counts; datetimes;
enums by value) or the refusal (exception type name AND message). A caller catches
`ValueError` and `UnresolvableInstrumentError` apart, so the type is part of the contract.

Hand mutants of the Rust that this file kills are listed in docs/RUST_PORT.md (P1e row).
What is deliberately NOT identical is pinned at the bottom (`test_narrowing_*`).
"""

from __future__ import annotations

import itertools
import random
import sys
import unicodedata
from datetime import date, datetime, timedelta
from decimal import Decimal
from enum import Enum

import pytest

from frozen_option_rules import instruments as oi
from frozen_option_rules import option_lifecycle as ol
from frozen_option_rules import option_roots as orr
from trade_engine.calendar.sessions import ExchangeCalendar
from trade_engine.domain import instruments as ni
from trade_engine.domain import option_lifecycle as nl
from trade_engine.domain import option_roots as nr

CAL = ExchangeCalendar()


def norm(x):
    """A value as plain data, so old-module and new-module objects compare equal."""
    if isinstance(x, Decimal):
        return ("Decimal", str(x))
    if isinstance(x, datetime):
        return ("datetime", x.isoformat())
    if isinstance(x, date):
        return ("date", x.isoformat())
    if isinstance(x, Enum):
        return (type(x).__name__, x.value)
    if hasattr(x, "strike") and hasattr(x, "underlying"):
        return ("OptionContract", x.underlying, x.expiry.isoformat(), norm(x.strike), x.right.value, x.multiplier, x.occ)
    if hasattr(x, "settle_time"):
        return ("OptionStyle", x.root, x.underlying, norm(x.exercise), norm(x.settlement), norm(x.settle_time))
    if hasattr(x, "symbol"):
        return (type(x).__name__, x.symbol)
    if isinstance(x, (tuple, list)):
        return tuple(norm(i) for i in x)
    return x


def run(f, *args, **kw):
    try:
        return ("ok", norm(f(*args, **kw)))
    except Exception as e:  # noqa: BLE001 - the refusal IS the thing compared
        return ("err", type(e).__name__, str(e))


def same(old, new, *args, **kw):
    a, b = run(old, *args, **kw), run(new, *args, **kw)
    assert a == b, f"{args!r} {kw!r}\n  frozen: {a}\n  rust:   {b}"
    return a


def contracts(und, expiry, strike, right, multiplier=100):
    return (
        oi.OptionContract(und, expiry, strike, oi.OptionRight(right), multiplier),
        ni.OptionContract(und, expiry, strike, ni.OptionRight(right), multiplier),
    )


# --- OCC parsing ----------------------------------------------------------------------


def _occ_strings() -> list:
    roots = ["A", "AB", "ABC", "AAPL", "SPXW", "BRKB", "ABCDEF", "abc", "aApL", "A1", "123456", "SPX", "ABCDEFG", ""]
    dates = [
        "260918", "240229", "250229", "000229", "000101", "991231", "680101", "690101", "700101", "680229",
        "690229", "260000", "260100", "261301", "260132", "260431", "260931", "260230", "261231", "20260918",
        "26091", "26-0918", "2609 18", "000000", "999999", "1", "", "26Sep18",
    ]
    strikes = ["00150000", "00000000", "00000001", "99999999", "12345678", "00001500", "0015000", "001500000", "0015000X"]
    out = []
    for r, d, right, k in itertools.product(roots[:12], dates, "CPcpX", strikes[:3]):
        out.append(f"{r}{d}{right}{k}")
        out.append(f"{r:<6}{d}{right}{k}")
        out.append(f"{r}  {d}{right}{k}")
    for r, d, k in itertools.product(roots, dates[:8], strikes):
        out.append(f"{r:<6}{d}C{k}")
        out.append(f" \t{r}{d}P{k}\n")
    out += [
        "", " ", "   ", "AAPL", "AAPL  260918C0015000", "AAPL  260918C001500000", "AAPL  260918Z00150000",
        "AAPL 260918C00150000", "AAPL\t260918C00150000", "AAPL 260918C00150000", "AAPL\u001c260918C00150000",
        "\u001cAAPL260918C00150000\u001f", "AAPL260918C00150000 x", "x AAPL260918C00150000",
        "AAPL  260918C00150000 ", "٣" * 6, "AAPL٢٦٠٩١٨C00150000",
        "AAPL２６０９１８C00150000", "AAPL260918C٠٠١٥٠٠٠٠",
        "ÀAPL260918C00150000", "AAPLé260918C00150000", None, 5, b"AAPL260918C00150000", [], 0, False,
    ]
    return out


@pytest.mark.parametrize("multiplier", [100, 10])
def test_from_occ_matches(multiplier) -> None:
    for s in _occ_strings():
        same(oi.OptionContract.from_occ, ni.OptionContract.from_occ, s, multiplier)


def test_from_occ_two_digit_year_pivot() -> None:
    # Preserved defect: CPython's POSIX pivot makes 69 -> 1969 and 70 -> 1970 (and 68 -> 2068).
    for yy, year in (("68", 2068), ("69", 1969), ("70", 1970), ("00", 2000), ("99", 1999)):
        a = same(oi.OptionContract.from_occ, ni.OptionContract.from_occ, f"A{yy}0101C00001000")
        assert a[0] == "ok" and a[1][2] == f"{year}-01-01", a


def test_from_occ_every_date_text() -> None:
    """strptime's regex alternation and its error text, over every MMDD for many years,
    and a seeded sample of any six digits."""
    for yy in ("00", "01", "24", "28", "29", "68", "69", "70", "99"):
        for mm in range(100):
            for dd in range(100):
                same(oi.OptionContract.from_occ, ni.OptionContract.from_occ, f"A{yy}{mm:02d}{dd:02d}C00001000")
    rnd = random.Random(7)
    for _ in range(20000):
        same(oi.OptionContract.from_occ, ni.OptionContract.from_occ, f"A{rnd.randrange(10**6):06d}P00001000")


def _sweep_chars() -> list[str]:
    """Code points to sweep. Those unassigned in the oracle interpreter's Unicode version
    are left out: Rust's std knows newer case mappings (e.g. Garay, Unicode 16) than
    CPython 3.13's 15.1, which only changes the echoed text of a refusal. The Rust tables
    for space and Nd are pinned to 15.1; a newer interpreter fails this sweep loudly."""
    cps: set[int] = set(range(0x100))
    for cp in range(0x110000):
        if 0xD800 <= cp <= 0xDFFF:
            continue
        c = chr(cp)
        if c.isspace() or c.isdecimal() or c.upper().isascii():
            cps.update(x for x in (cp - 1, cp, cp + 1) if 0 <= x < 0x110000 and not 0xD800 <= x <= 0xDFFF)
    cps.update(range(0, 0x110000, 211))
    return [
        chr(c) for c in sorted(cps) if not 0xD800 <= c <= 0xDFFF and unicodedata.category(chr(c)) != "Cn"
    ]


def test_text_semantics_match_cpython_for_whitespace_digits_and_case() -> None:
    """`str.strip`, `\\s`, `\\d`, `int()` and `str.upper()` as the old code used them."""
    old, new = oi.OptionContract.from_occ, ni.OptionContract.from_occ
    for c in _sweep_chars():
        for s in (
            f"{c}AAPL260918C00150000",
            f"AAPL{c}260918C00150000",
            f"AAPL26091{c}C00150000",
            f"AAPL260918C0015000{c}",
            f"AAPL{c}60918C00150000",
            f"{c}260918C00150000",
        ):
            same(old, new, s)
        same(oi.Equity, ni.Equity, c)
        same(oi.Equity, ni.Equity, f"A{c}")
        same(oi.Equity, ni.Equity, f" {c} ")
        same(lambda u: oi.OptionContract(u, date(2026, 9, 18), Decimal("1"), oi.OptionRight.CALL),
             lambda u: ni.OptionContract(u, date(2026, 9, 18), Decimal("1"), ni.OptionRight.CALL), c)
        same(orr.option_style, nr.option_style, c)
        same(orr.chain_roots, nr.chain_roots, f"{c}pX")


# --- Constructor ----------------------------------------------------------------------

_STRIKES = [
    150, 1, 0, -1, 99999, 100000, 150.0, 0.5, 1.2345, 0.001, 0.0001, 99999.999, 99999.9995, 1e-5, 1e5, -0.5,
    float("nan"), float("inf"), float("-inf"), "150", "1.5", "abc", "", "1e2", " 1 ", "0x10", "1_0",
    Decimal("150"), Decimal("150.000"), Decimal("150.0000"), Decimal("0.001"), Decimal("0.0010"), Decimal("0.0001"),
    Decimal("0.00049"), Decimal("99999.999"), Decimal("99999.9990"), Decimal("99999.9995"), Decimal("100000"),
    Decimal("100000.000"), Decimal("1E+2"), Decimal("1E+5"), Decimal("1E+4"), Decimal("1E-3"), Decimal("1E-4"),
    Decimal("10E-4"), Decimal("-0"), Decimal("0E+3"), Decimal("0.000"), Decimal("-1.5"), Decimal("NaN"),
    Decimal("sNaN"), Decimal("-NaN"), Decimal("Infinity"), Decimal("-Infinity"), Decimal("1E+27"), Decimal("1E-999999"), Decimal("1E-1000"), Decimal("5E-1010"),
    Decimal("1.0000000000000000000000000001"), Decimal("1.00000000000000000000000000001"),
    Decimal("1.0000000000000000000000000000001"), Decimal("99999.99900000000000000000000001"),
    Decimal("0.0010000000000000000000000000001"), Decimal("12345.6789012345678901234567890123"),
    Decimal("9" * 25 + ".999"), Decimal("9" * 29), Decimal("0." + "0" * 40 + "1"), Decimal("0.00" + "9" * 40),
    Decimal("0.000" + "5" * 30), Decimal("0.0005"), Decimal("0.00050"), Decimal("0.0015"), Decimal("0.0025"),
    Decimal("99999.9995"), Decimal("99999.99950"), Decimal("99999.9994999"), Decimal("99999.99949999999"),
]
_RIGHTS = [
    "C", "P", "c", "p", "call", "CALL", "Call", "put", "PUT", "Put", "X", "", " C", "CC", "ſ", "ß", None, 1, 1.5,
    "ǰ", "ﬃ", "ı",
]
_UNDERLYINGS = [
    "AAPL", "aapl", " aapl ", "", "  ", "ABCDEF", "abcdefg", "ABC.D", "A-B", "A B", "SPXW", "ß", "ﬃ", "ı", "ſ", "ǰ",
    "٣", "ABé", "A ", "\u001cA", "123456", "1234567", "a1",
]


# Python's own `int()` of these builds a 400,000-digit integer, so only the cheap ones run, once.
_HUGE = [Decimal("1E+999999"), Decimal("1E+999998"), Decimal("1E+999997"), Decimal("1E+100000"), Decimal("1E-999999")]


def test_constructor_huge_exponents() -> None:
    for strike in _HUGE:
        same(
            lambda s: oi.OptionContract("AAPL", date(2026, 9, 18), s, oi.OptionRight.CALL),
            lambda s: ni.OptionContract("AAPL", date(2026, 9, 18), s, ni.OptionRight.CALL),
            strike,
        )


def test_constructor_strikes_and_multipliers() -> None:
    for strike, mult in itertools.product(_STRIKES, [100, 1, 0, -1, 10, True, 2**70]):
        same(
            lambda *a: oi.OptionContract(a[0], a[1], a[2], oi.OptionRight.CALL, a[3]),
            lambda *a: ni.OptionContract(a[0], a[1], a[2], ni.OptionRight.CALL, a[3]),
            "AAPL", date(2026, 9, 18), strike, mult,
        )


def test_constructor_rights_and_underlyings() -> None:
    for und, right in itertools.product(_UNDERLYINGS, _RIGHTS):
        same(
            lambda u, r: oi.OptionContract(u, date(2026, 9, 18), Decimal("1.5"), r),
            lambda u, r: ni.OptionContract(u, date(2026, 9, 18), Decimal("1.5"), r),
            und, right,
        )


def test_constructor_reports_the_first_failure_in_the_same_order() -> None:
    bad = {"und": ["AAPL", "", "toolong1"], "strike": [Decimal("1"), Decimal("0"), Decimal("0.0001")],
           "mult": [100, 0], "right": ["C", "Z"]}
    for und, strike, mult, right in itertools.product(*bad.values()):
        same(
            lambda *a: oi.OptionContract(a[0], date(2026, 9, 18), a[1], a[3], a[2]),
            lambda *a: ni.OptionContract(a[0], date(2026, 9, 18), a[1], a[3], a[2]),
            und, strike, mult, right,
        )


def test_to_occ_and_properties_match() -> None:
    for strike in [s for s in _STRIKES if isinstance(s, (Decimal, int, float, str))]:
        for und, right in (("AAPL", "C"), ("a", "P"), ("SPXW", "C"), ("ABCDEF", "P")):
            for expiry in (date(2026, 9, 18), date(2000, 1, 1), date(2068, 12, 31), date(1969, 1, 2), date(2100, 3, 4)):
                a = run(lambda: oi.OptionContract(und, expiry, strike, oi.OptionRight(right)).to_occ())
                b = run(lambda: ni.OptionContract(und, expiry, strike, ni.OptionRight(right)).to_occ())
                assert a == b, (strike, und, expiry, a, b)


def test_roundtrip_canonical_symbols() -> None:
    for s in ("AAPL  260918C00150000", "SPXW  261016P05000000", "A     000101C00000001", "ABCDEF991231P99999999"):
        a = same(oi.OptionContract.from_occ, ni.OptionContract.from_occ, s)
        assert a[0] == "ok" and a[1][6] == s


def test_equity_symbols() -> None:
    for s in [
        "AAPL", "aapl", " brk.b ", "BRK.B", "BRK.BB", "A.BCD", ".A", "A.", "A..B", "A.B.C", "/NQ", "AAPL260918C00150000",
        "ABCDEFGHIJ", "ABCDEFGHIJK", "A B", "", " ", "\t", "A1", "1", "1.A", "A.1", "a.b", "٣", "ß", "ﬃ", "ıı",
        None, 5, b"AAPL", ["A"], 0, False,
    ]:
        same(oi.Equity, ni.Equity, s)


# --- Option roots ---------------------------------------------------------------------

_ROOTS = [
    "SPX", "SPXW", "spx", " spxw ", "NDX", "NDXP", "RUT", "RUTW", "MRUT", "XSP", "XND", "DJX", "OEX", "XEO", "VIX",
    "VIXW", "vix", "AAPL", "SPY", "QQQ", "", " ", "SPXQ", "SPXWW", "SPX1", "A", "ABCDEF", "ß", "ſpx", "ｓｐｘ",
]


@pytest.mark.parametrize("root", _ROOTS)
def test_option_style_and_chain_roots(root) -> None:
    same(orr.option_style, nr.option_style, root)
    same(orr.chain_roots, nr.chain_roots, root)


def test_every_index_table_root_is_covered() -> None:
    # the unmodelled set and the modelled roots, as the old module wrote them
    for root in sorted(orr._UNMODELLED_INDEX_ROOTS) + sorted(orr._INDEX_ROOTS):
        a = same(orr.option_style, nr.option_style, root)
        assert a[0] == ("err" if root in orr._UNMODELLED_INDEX_ROOTS else "ok") or a[0] == "ok"
        same(orr.chain_roots, nr.chain_roots, root)
    for style in orr._INDEX_ROOTS.values():
        same(orr.chain_roots, nr.chain_roots, style.underlying)


def test_settlement_and_last_trade_over_every_day_2000_2040() -> None:
    d = date(1999, 12, 30)
    end = date(2041, 1, 3)
    # one contract per (root, day): the construction is itself identical on both sides
    while d <= end:
        for root in ("SPX", "SPXW", "AAPL"):
            old, new = contracts(root, d, Decimal("100"), "C")
            a = run(orr.settlement_instant, old, CAL)
            b = run(nr.settlement_instant, new, CAL)
            assert a == b, (root, d, a, b)
            a = run(orr.last_trade_date, old, CAL)
            b = run(nr.last_trade_date, new, CAL)
            assert a == b, (root, d, a, b)
        d += timedelta(days=1)


def test_settlement_on_known_days() -> None:
    new = ni.OptionContract
    r = ni.OptionRight.CALL
    third_friday = date(2026, 10, 16)
    assert nr.settlement_instant(new("SPX", third_friday, Decimal("1"), r), CAL).isoformat() == "2026-10-16T13:30:00+00:00"
    assert nr.settlement_instant(new("SPXW", third_friday, Decimal("1"), r), CAL).isoformat() == "2026-10-16T20:00:00+00:00"
    assert nr.settlement_instant(new("SPY", date(2026, 11, 27), Decimal("1"), r), CAL).isoformat() == "2026-11-27T18:00:00+00:00"
    assert nr.last_trade_date(new("SPX", third_friday, Decimal("1"), r), CAL) == date(2026, 10, 15)


def test_unmodelled_root_refuses_with_the_domain_type() -> None:
    with pytest.raises(ni.UnresolvableInstrumentError):
        nr.option_style("NDX")
    with pytest.raises(ni.UnresolvableInstrumentError):
        nr.chain_roots("")
    with pytest.raises(ni.UnresolvableInstrumentError):
        nr.settlement_instant(ni.OptionContract("VIX", date(2026, 10, 21), Decimal("20"), ni.OptionRight.CALL), CAL)


# --- Lifecycle ------------------------------------------------------------------------

_LSTRIKES = ["100", "100.5", "0.001", "99999.999", "5000", "150.00", "1"]
_PRICES = [
    "0.01", "0.001", "0.5", "1", "99.99", "100", "100.00", "100.01", "100.5", "100.50", "101", "110", "150", "5000",
    "5000.01", "0", "0.00", "-1", "-0.01", "1000000", "12345.678",
]
_ROOT_SAMPLE = ("AAPL", "SPX", "SPXW", "NDX")


def _grid(strikes=_LSTRIKES, roots=_ROOT_SAMPLE):
    for root, strike, right in itertools.product(roots, strikes, "CP"):
        yield (*contracts(root, date(2026, 10, 16), Decimal(strike), right), root, strike, right)


def test_intrinsic_and_outcome_over_the_grid() -> None:
    for old, new, root, strike, right in _grid():
        for price in _PRICES:
            p = Decimal(price)
            assert run(ol.intrinsic, old, p) == run(nl.intrinsic, new, p), (root, strike, right, price)
            for side in ("BUY", "SELL"):
                a = run(ol.expiry_outcome, old, oi.Side(side), p)
                b = run(nl.expiry_outcome, new, ni.Side(side), p)
                assert a == b, (root, strike, right, price, side, a, b)


def test_style_predicates_and_deliverable() -> None:
    for old, new, root, strike, right in _grid(strikes=["100"]):
        assert run(ol.is_cash_settled, old) == run(nl.is_cash_settled, new)
        assert run(ol.can_exercise_early, old) == run(nl.can_exercise_early, new)
        assert run(ol.deliverable, old) == run(nl.deliverable, new)
    for root in ("AAPL", "BRK", "A1", "SPY", "123456"):
        old, new = contracts(root, date(2026, 10, 16), Decimal("100"), "C")
        assert run(ol.deliverable, old) == run(nl.deliverable, new)


def test_delivery_over_the_grid() -> None:
    for old, new, root, strike, right in _grid():
        for premium in ("0", "0.00", "0.01", "2.5", "2.50", "99.99", "100", "100.5", "1000", "-0.01", "-1", "0.001"):
            for side in ("BUY", "SELL"):
                a = run(ol.delivery, old, oi.Side(side), Decimal(premium))
                b = run(nl.delivery, new, ni.Side(side), Decimal(premium))
                assert a == b, (root, strike, right, premium, side, a, b)


def test_exercised_for_dividend_over_the_grid() -> None:
    closes = ["0", "50", "99.99", "100", "100.005", "100.01", "105", "110", "150", "5100"]
    bids = ["0", "0.5", "5", "10", "10.2", "10.20", "10.5", "10.50", "11", "20", "-0.01", "-1", "100"]
    divs = ["0.5", "0.50", "0.2", "1", "0", "0.00", "-1", "0.001", "10"]
    for old, new, root, strike, right in _grid(strikes=["100", "100.5"], roots=("AAPL", "SPX")):
        for close, bid, div in itertools.product(closes, bids, divs):
            args = (Decimal(close), Decimal(bid), Decimal(div))
            a = run(ol.exercised_for_dividend, old, *args)
            b = run(nl.exercised_for_dividend, new, *args)
            assert a == b, (root, strike, right, args, a, b)


def test_dividend_extrinsic_tie_is_not_exercised() -> None:
    # bid - intrinsic == dividend exactly: `<`, not `<=`
    old, new = contracts("AAPL", date(2026, 10, 16), Decimal("100"), "C")
    assert run(ol.exercised_for_dividend, old, Decimal("110"), Decimal("10.50"), Decimal("0.50")) == ("ok", False)
    assert run(nl.exercised_for_dividend, new, Decimal("110"), Decimal("10.50"), Decimal("0.50")) == ("ok", False)
    assert run(nl.exercised_for_dividend, new, Decimal("110"), Decimal("10.49"), Decimal("0.50")) == ("ok", True)


def test_nan_orders_raise_invalid_operation_like_decimal() -> None:
    old, new = contracts("AAPL", date(2026, 10, 16), Decimal("100"), "C")
    nan = Decimal("NaN")
    for args in ((nan,),):
        assert run(ol.intrinsic, old, *args) == run(nl.intrinsic, new, *args)
    assert run(ol.delivery, old, oi.Side.BUY, nan) == run(nl.delivery, new, ni.Side.BUY, nan)
    for a in itertools.product((Decimal("110"), nan), (Decimal("1"), nan), (Decimal("1"), nan)):
        assert run(ol.exercised_for_dividend, old, *a) == run(nl.exercised_for_dividend, new, *a), a


def test_ints_are_money_too() -> None:
    old, new = contracts("AAPL", date(2026, 10, 16), Decimal("100"), "P")
    for n in (0, 1, 95, 100, 105):
        assert run(ol.intrinsic, old, n) == run(nl.intrinsic, new, n)
        assert run(ol.delivery, old, oi.Side.SELL, n) == run(nl.delivery, new, ni.Side.SELL, n)


def test_exercise_threshold_and_outcomes() -> None:
    assert str(nl.EXERCISE_THRESHOLD) == str(ol.EXERCISE_THRESHOLD)
    assert [o.value for o in nl.Outcome] == [o.value for o in ol.Outcome]


# --- What is deliberately not identical -----------------------------------------------


def test_narrowing_a_non_int_multiplier_refuses() -> None:
    # pre-port: any number passed `multiplier <= 0` (100.0, Decimal(100)); it then broke
    # every consumer. The rule is now `int` only.
    for m in (100.0, Decimal(100), "100", None):
        with pytest.raises(ValueError, match="Multiplier must be an integer"):
            ni.OptionContract("AAPL", date(2026, 9, 18), Decimal("1"), ni.OptionRight.CALL, m)


def test_narrowing_floats_are_not_money() -> None:
    c = ni.OptionContract("AAPL", date(2026, 9, 18), Decimal("100"), ni.OptionRight.CALL)
    with pytest.raises(TypeError):
        nl.intrinsic(c, 101.5)


def test_narrowing_settlement_needs_the_xnys_engine_calendar() -> None:
    c = ni.OptionContract("SPY", date(2026, 10, 16), Decimal("100"), ni.OptionRight.CALL)

    class Fake:
        def is_session(self, d):
            return True

    with pytest.raises(ValueError, match="XNYS ExchangeCalendar"):
        nr.settlement_instant(c, Fake())
    with pytest.raises(ValueError, match="XNYS ExchangeCalendar"):
        nr.last_trade_date(c, Fake())


def test_refusal_types_are_the_new_modules_own() -> None:
    with pytest.raises(ni.UnresolvableInstrumentError):
        nr.option_style("RUT")
    with pytest.raises(ValueError) as e:
        ni.OptionContract("", date(2026, 9, 18), Decimal("1"), ni.OptionRight.CALL)
    assert type(e.value) is ValueError
    assert sys.modules["frozen_option_rules.instruments"] is not sys.modules["trade_engine.domain.instruments"]

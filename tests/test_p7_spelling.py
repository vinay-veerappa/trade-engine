"""P7: Python and Rust spell every decimal byte-identically (docs/RUST_PORT.md S1), and a
value outside the bound is refused by both, never rounded (I5)."""

from __future__ import annotations

import random
from decimal import Decimal

import pytest
import trade_engine_rs as rs

from trade_engine.ledger.codec import DecimalRangeError, canon_decimal, payload_text


def rust(text: str) -> str:
    return rs.money_canon(text)


TABLE = [
    ("1.10", "1.1"),
    ("1E+2", "100"),
    ("-0", "0"),
    ("0.000", "0"),
    ("0E+3", "0"),
    ("7.77E+3", "7770"),
    ("1E-7", "0.0000001"),
    ("-0.50", "-0.5"),
    ("100", "100"),
    ("33.33333333333333333333333333", "33.33333333333333333333333333"),
    ("79228162514264337593543950335", "79228162514264337593543950335"),
    ("0.0000000000000000000000000001", "0.0000000000000000000000000001"),
]


@pytest.mark.parametrize(("raw", "spelled"), TABLE)
def test_the_design_table(raw: str, spelled: str) -> None:
    assert canon_decimal(Decimal(raw)) == spelled
    assert rust(raw) == spelled
    assert canon_decimal(Decimal(spelled)) == spelled


@pytest.mark.parametrize(
    "raw",
    ["79228162514264337593543950336", "1E+30", "1E-29", "NaN", "Infinity", "-Infinity", "sNaN"],
)
def test_outside_the_bound_both_refuse(raw: str) -> None:
    with pytest.raises(DecimalRangeError):
        canon_decimal(Decimal(raw))
    with pytest.raises(ValueError):
        rust(raw)


def test_random_values_are_spelled_byte_identically() -> None:
    rng = random.Random(7)
    for _ in range(20000):
        digits = "".join(rng.choice("0123456789") for _ in range(rng.randint(1, 31)))
        exponent = rng.randint(-34, 12)
        sign = rng.choice(["", "-"])
        raw = f"{sign}{digits}E{exponent}"
        try:
            mine = canon_decimal(Decimal(raw))
        except DecimalRangeError:
            with pytest.raises(ValueError):
                rust(raw)
            continue
        assert rust(raw) == mine, raw
        assert Decimal(mine) == Decimal(raw)


def test_an_out_of_bound_decimal_in_a_payload_is_refused_by_the_codec() -> None:
    with pytest.raises(ValueError):
        payload_text({"x": Decimal("1E+30")})
    assert payload_text({"x": Decimal("1.10")}) == payload_text({"x": Decimal("1.1")})

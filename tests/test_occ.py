"""Tests for OCC option symbology generation, parsing, and round-tripping."""

from datetime import date
from decimal import Decimal

import pytest
from trade_engine.domain.instruments import (
    OptionContract,
    OptionRight,
)


def test_occ_roundtrip_standard() -> None:
    """Test standard OCC symbols roundtrip accurately."""
    contract = OptionContract(
        underlying="AAPL",
        expiry=date(2026, 9, 18),
        strike=Decimal("150.00"),
        right=OptionRight.CALL,
    )
    occ = contract.to_occ()
    assert occ == "AAPL  260918C00150000"
    assert len(occ) == 21

    parsed = OptionContract.from_occ(occ)
    assert parsed.underlying == "AAPL"
    assert parsed.expiry == date(2026, 9, 18)
    assert parsed.strike == Decimal("150")
    assert parsed.right == OptionRight.CALL
    assert parsed.multiplier == 100
    assert parsed.to_occ() == occ


def test_occ_roundtrip_spxw_and_index() -> None:
    """Test SPXW weekly and other index contracts."""
    contract = OptionContract(
        underlying="SPXW",
        expiry=date(2026, 12, 31),
        strike=Decimal("5800"),
        right=OptionRight.PUT,
    )
    occ = contract.to_occ()
    assert occ == "SPXW  261231P05800000"
    assert len(occ) == 21

    parsed = OptionContract.from_occ(occ)
    assert parsed.underlying == "SPXW"
    assert parsed.expiry == date(2026, 12, 31)
    assert parsed.strike == Decimal("5800")
    assert parsed.right == OptionRight.PUT
    assert parsed.to_occ() == occ


def test_occ_roundtrip_decimal_strikes() -> None:
    """Test fractional and decimal strikes (e.g. 5900.5, 23.5, 125.25)."""
    test_cases = [
        ("SPXW", date(2026, 10, 15), Decimal("5900.5"), OptionRight.CALL, "SPXW  261015C05900500"),
        ("SOFI", date(2026, 11, 20), Decimal("23.5"), OptionRight.PUT, "SOFI  261120P00023500"),
        ("IWM", date(2026, 6, 19), Decimal("215.25"), OptionRight.CALL, "IWM   260619C00215250"),
    ]

    for und, exp, strike, right, expected_occ in test_cases:
        contract = OptionContract(underlying=und, expiry=exp, strike=strike, right=right)
        occ = contract.to_occ()
        assert occ == expected_occ
        parsed = OptionContract.from_occ(occ)
        assert parsed.underlying == und
        assert parsed.expiry == exp
        assert parsed.strike == strike
        assert parsed.right == right
        assert parsed.to_occ() == expected_occ


def test_occ_compact_parsing() -> None:
    """Test parsing compact OCC symbols without right-padding spaces."""
    compact_occ = "AAPL260918C00150000"
    parsed = OptionContract.from_occ(compact_occ)
    assert parsed.underlying == "AAPL"
    assert parsed.expiry == date(2026, 9, 18)
    assert parsed.strike == Decimal("150")
    assert parsed.right == OptionRight.CALL
    # Canonical output always has 21 characters with spaces
    assert parsed.to_occ() == "AAPL  260918C00150000"


@pytest.mark.parametrize(
    "invalid_occ",
    [
        "",
        "   ",
        "AAPL",
        "AAPL  260918X00150000",  # Invalid right 'X'
        "AAPL  261318C00150000",  # Invalid month 13
        "AAPL  260932C00150000",  # Invalid day 32
        "AAPL  260918C00000000",  # Strike zero
        "AAPL  260918C-0150000",  # Negative strike
        "AAPL  260918C0015000",   # Too short (7 strike digits)
        "TOOLONGTICKER260918C00150000",  # Ticker > 6 chars
    ],
)
def test_invalid_occ_rejected(invalid_occ: str) -> None:
    """Assert invalid OCC strings are rejected with ValueError."""
    with pytest.raises(ValueError):
        OptionContract.from_occ(invalid_occ)


def test_option_contract_validation() -> None:
    """Assert invalid direct OptionContract instantiation raises ValueError."""
    with pytest.raises(ValueError, match="underlying must be non-empty"):
        OptionContract(
            underlying="",
            expiry=date(2026, 9, 18),
            strike=Decimal("150"),
            right=OptionRight.CALL,
        )

    with pytest.raises(ValueError, match="Strike must be positive"):
        OptionContract(
            underlying="AAPL",
            expiry=date(2026, 9, 18),
            strike=Decimal("-10"),
            right=OptionRight.CALL,
        )

    with pytest.raises(ValueError, match="at most 6 characters"):
        OptionContract(
            underlying="TOOLONGTICKER",
            expiry=date(2026, 9, 18),
            strike=Decimal("150"),
            right=OptionRight.CALL,
        )

    with pytest.raises(ValueError, match="must be alphanumeric"):
        OptionContract(
            underlying="BRK.B",
            expiry=date(2026, 9, 18),
            strike=Decimal("450"),
            right=OptionRight.CALL,
        )

    with pytest.raises(ValueError, match="Multiplier must be positive"):
        OptionContract(
            underlying="AAPL",
            expiry=date(2026, 9, 18),
            strike=Decimal("150"),
            right=OptionRight.CALL,
            multiplier=0,
        )


def test_option_contract_multiplier_custom() -> None:
    """Assert multiplier argument can be customized (Architecture §4.1)."""
    contract = OptionContract(
        underlying="AAPL",
        expiry=date(2026, 9, 18),
        strike=Decimal("150"),
        right=OptionRight.CALL,
        multiplier=50,
    )
    assert contract.multiplier == 50


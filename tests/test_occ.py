"""Tests for OCC option symbology generation, parsing, and round-tripping."""

from datetime import date
from decimal import Decimal

import pytest
from trade_engine.domain.instruments import (
    Combo,
    ComboLeg,
    Equity,
    OptionContract,
    OptionRight,
    Side,
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
    """Test fractional and decimal strikes (e.g. 5900.5, 23.5, 125.25, 150.125)."""
    test_cases = [
        ("SPXW", date(2026, 10, 15), Decimal("5900.5"), OptionRight.CALL, "SPXW  261015C05900500"),
        ("SOFI", date(2026, 11, 20), Decimal("23.5"), OptionRight.PUT, "SOFI  261120P00023500"),
        ("IWM", date(2026, 6, 19), Decimal("215.25"), OptionRight.CALL, "IWM   260619C00215250"),
        ("SPY", date(2026, 9, 18), Decimal("150.125"), OptionRight.CALL, "SPY   260918C00150125"),
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

    # Underlying length boundaries (fire + pass)
    # Length 6 passes
    contract_len6 = OptionContract(
        underlying="SPXW12",
        expiry=date(2026, 9, 18),
        strike=Decimal("150"),
        right=OptionRight.CALL,
    )
    assert contract_len6.underlying == "SPXW12"

    # Length 7 fails (fire test)
    with pytest.raises(ValueError, match="at most 6 characters"):
        OptionContract(
            underlying="ABCDEFG",
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

    # Refuse strikes with > 3 decimal places (I5) (fire tests)
    with pytest.raises(ValueError, match="cannot have more than 3 decimal places"):
        OptionContract(
            underlying="SPY",
            expiry=date(2026, 9, 18),
            strike=Decimal("150.0005"),
            right=OptionRight.CALL,
        )

    with pytest.raises(ValueError, match="cannot have more than 3 decimal places"):
        OptionContract(
            underlying="SPY",
            expiry=date(2026, 9, 18),
            strike=Decimal("0.0004"),
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


def test_combo_multiplier_derivation_and_mixed_refusal() -> None:
    """Combo derives a uniform multiplier; a stock + option combo is buildable but has none (I6)."""
    leg1 = ComboLeg(
        contract=OptionContract("AAPL", date(2026, 9, 18), Decimal("150"), OptionRight.CALL, multiplier=100),
        ratio=1,
        side=Side.BUY,
    )
    leg2 = ComboLeg(
        contract=OptionContract("AAPL", date(2026, 9, 18), Decimal("160"), OptionRight.CALL, multiplier=100),
        ratio=1,
        side=Side.SELL,
    )
    combo = Combo(legs=(leg1, leg2))
    assert combo.multiplier == 100

    # Buy-write (stock + short call) is a legal combo...
    stock_leg = ComboLeg(
        contract=Equity("AAPL"),
        ratio=100,
        side=Side.BUY,
    )
    short_call = ComboLeg(contract=leg2.contract, ratio=1, side=Side.SELL)
    buy_write = Combo(legs=(stock_leg, short_call))
    assert len(buy_write.legs) == 2

    # ...but has no single multiplier: asking for one refuses (fire test)
    with pytest.raises(ValueError, match="mixed leg multipliers"):
        _ = buy_write.multiplier


def test_equity_symbol_validation() -> None:
    """Test Equity enforces valid ticker symbols and rejects slashes/OCC formats."""
    eq = Equity("AAPL")
    assert eq.symbol == "AAPL"

    with pytest.raises(ValueError, match="alphanumeric without slashes"):
        Equity("/NQ")

    with pytest.raises(ValueError, match="alphanumeric without slashes"):
        Equity("A B")

    with pytest.raises(ValueError, match="exceeds maximum length"):
        Equity("SPY   260918C00150000")

    with pytest.raises(ValueError, match="exceeds maximum length"):
        Equity("VERYLONGTICKERNAME")



def test_equity_symbol_share_class_and_ascii() -> None:
    """Class shares are real tickers; non-ASCII look-alikes are not (I6)."""
    assert Equity("brk.b").symbol == "BRK.B"
    for bad in ("BRK..B", ".B", "BRK.", "AAPL²", "ÄPPL"):
        with pytest.raises(ValueError):
            Equity(bad)


def test_option_strike_must_be_finite() -> None:
    for bad in ("Infinity", "NaN"):
        with pytest.raises(ValueError, match="positive and finite"):
            OptionContract("SPY", date(2026, 9, 18), Decimal(bad), OptionRight.CALL)

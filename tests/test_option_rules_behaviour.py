"""Option rules (domain.instruments, option_lifecycle, option_roots): explicit values and refusals.

Each refusal names the type a caller catches on: ValueError for bad input,
UnresolvableInstrumentError for an unmodelled index root, and decimal.InvalidOperation or
decimal.Overflow for the Decimal rules.
"""

from __future__ import annotations

import decimal
from datetime import date
from decimal import Decimal as D

import pytest

from trade_engine.calendar.sessions import ExchangeCalendar
from trade_engine.domain.instruments import OptionContract, OptionRight, Side, UnresolvableInstrumentError
from trade_engine.domain.option_lifecycle import deliverable, delivery, exercised_for_dividend, intrinsic, is_cash_settled
from trade_engine.domain.option_roots import chain_roots, last_trade_date, option_style, settlement_instant

CAL = ExchangeCalendar()
NEAR = date(2026, 10, 16)


def _contract(root: str = "AAPL", strike: str = "100", right: OptionRight = OptionRight.CALL, multiplier: int = 100):
    return OptionContract(root, NEAR, D(strike), right, multiplier)


# --- OCC symbols ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "symbol",
    ["AAPL  260918C00150000", "SPXW  261016P05000000", "A     000101C00000001", "ABCDEF991231P99999999"],
)
def test_a_canonical_occ_symbol_round_trips(symbol):
    assert OptionContract.from_occ(symbol).occ == symbol


@pytest.mark.parametrize(("yy", "year"), [("68", 2068), ("69", 1969), ("70", 1970), ("00", 2000), ("99", 1999)])
def test_two_digit_years_pivot_at_68_69_and_70(yy, year):
    # Preserved behaviour: CPython's POSIX pivot, so 69 is 1969 and 70 is 1970.
    assert OptionContract.from_occ(f"A{yy}0101C00001000").expiry == date(year, 1, 1)


# --- construction -----------------------------------------------------------------------


def test_a_non_int_multiplier_is_refused():
    # Any number used to pass `multiplier <= 0`; the rule is now int only.
    for multiplier in (100.0, D(100), "100", None):
        with pytest.raises(ValueError, match="Multiplier must be an integer"):
            OptionContract("AAPL", NEAR, D("1"), OptionRight.CALL, multiplier)


def test_a_strike_beyond_the_decimal_range_overflows():
    with pytest.raises(decimal.Overflow):
        OptionContract("AAPL", NEAR, D("1E+999999"), OptionRight.CALL)


def test_an_empty_underlying_is_a_value_error_not_a_domain_error():
    with pytest.raises(ValueError) as err:
        OptionContract("", NEAR, D("1"), OptionRight.CALL)
    assert type(err.value) is ValueError


# --- money --------------------------------------------------------------------------------


def test_intrinsic_value_is_never_negative():
    call = _contract()
    put = _contract(right=OptionRight.PUT)
    assert intrinsic(call, D("110")) == D("10")
    assert intrinsic(call, D("90")) == D("0")
    assert intrinsic(put, D("90")) == D("10")


def test_an_int_price_is_money_too():
    assert intrinsic(_contract(), 110) == D("10")


def test_a_float_or_a_bool_is_not_money():
    call = _contract()
    with pytest.raises(TypeError, match="got float"):
        intrinsic(call, 101.5)
    with pytest.raises(TypeError, match="got bool"):
        intrinsic(call, True)


def test_a_nan_price_raises_invalid_operation():
    call = _contract()
    nan = D("NaN")
    with pytest.raises(decimal.InvalidOperation):
        intrinsic(call, nan)
    with pytest.raises(decimal.InvalidOperation):
        delivery(call, Side.BUY, nan)
    with pytest.raises(decimal.InvalidOperation):
        exercised_for_dividend(call, D("110"), nan, D("1"))


# --- lifecycle --------------------------------------------------------------------------


def test_a_cash_settled_index_delivers_no_shares():
    spx = _contract("SPX")
    assert is_cash_settled(spx) is True
    with pytest.raises(ValueError, match="settles in cash"):
        deliverable(spx)
    assert is_cash_settled(_contract("AAPL")) is False
    assert deliverable(_contract("AAPL")).symbol == "AAPL"


def test_delivery_shares_and_price_follow_the_held_side():
    # A long call exercised buys shares at strike plus premium; a short put assigned buys them
    # at strike less premium.
    assert delivery(_contract(), Side.BUY, D("2.5")) == (Side.BUY, D("102.5"))
    assert delivery(_contract(right=OptionRight.PUT), Side.SELL, D("2.5")) == (Side.BUY, D("97.5"))


def test_a_dividend_exercise_is_not_taken_on_an_exact_tie():
    # bid - intrinsic == dividend exactly: `<`, not `<=`
    call = _contract()
    assert exercised_for_dividend(call, D("110"), D("10.50"), D("0.50")) is False
    assert exercised_for_dividend(call, D("110"), D("10.49"), D("0.50")) is True


# --- option roots -----------------------------------------------------------------------


def test_settlement_and_last_trade_on_known_days():
    third_friday = date(2026, 10, 16)
    spx = OptionContract("SPX", third_friday, D("1"), OptionRight.CALL)
    spxw = OptionContract("SPXW", third_friday, D("1"), OptionRight.CALL)
    spy = OptionContract("SPY", date(2026, 11, 27), D("1"), OptionRight.CALL)
    assert settlement_instant(spx, CAL).isoformat() == "2026-10-16T13:30:00+00:00"
    assert settlement_instant(spxw, CAL).isoformat() == "2026-10-16T20:00:00+00:00"
    assert settlement_instant(spy, CAL).isoformat() == "2026-11-27T18:00:00+00:00"
    assert last_trade_date(spx, CAL) == date(2026, 10, 15)


def test_an_unmodelled_index_root_refuses_with_the_domain_type():
    with pytest.raises(UnresolvableInstrumentError):
        option_style("RUT")
    with pytest.raises(UnresolvableInstrumentError):
        chain_roots("")
    with pytest.raises(UnresolvableInstrumentError):
        settlement_instant(OptionContract("VIX", date(2026, 10, 21), D("20"), OptionRight.CALL), CAL)


def test_settlement_needs_the_xnys_engine_calendar():
    spy = OptionContract("SPY", NEAR, D("100"), OptionRight.CALL)

    class NotXnys:
        def is_session(self, day):
            return True

    with pytest.raises(ValueError, match="XNYS ExchangeCalendar"):
        settlement_instant(spy, NotXnys())
    with pytest.raises(ValueError, match="XNYS ExchangeCalendar"):
        last_trade_date(spy, NotXnys())

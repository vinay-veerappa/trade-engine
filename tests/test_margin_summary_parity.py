"""margin_summary parity: AccountMargin's used/available come from trade_engine_rs.margin_summary.

`margin.rs` defines margin_summary(equity, maintenance) = (maintenance, equity - maintenance).
No Python code computes used or available any more; this pins the Rust call to the plain
Decimal arithmetic on hand-picked cases, including negative availability (an account under
its maintenance requirement) and values near the 28-digit Decimal limit.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
import trade_engine_rs as _rs

from trade_engine.metrics.margin import AccountMargin

D = Decimal

CASES = [
    ("1400", "5150"),  # negative availability: -3750
    ("5150", "5150"),  # exactly at maintenance: zero
    ("0", "0"),
    ("-0.00", "0"),
    ("-250.75", "100.25"),  # negative equity, negative availability
    ("10000.50", "0.25"),
    ("9999999999.9999999991", "1234.5678"),
    ("1234567890123456789.12345678", "987654321.00000001"),
    ("42", "0.000001"),
]


@pytest.mark.parametrize("equity,maintenance", CASES)
def test_rust_margin_summary_is_maintenance_and_equity_less_maintenance(equity, maintenance):
    used, available = _rs.margin_summary(equity, maintenance)
    assert D(used) == D(maintenance)
    assert D(available) == D(equity) - D(maintenance)


@pytest.mark.parametrize("equity,maintenance", CASES)
def test_account_margin_properties_equal_plain_decimal_arithmetic(equity, maintenance):
    margin = AccountMargin(
        equity=D(equity),
        cash=D("0"),
        market_value_long=D("0"),
        market_value_short=D("0"),
        gross_exposure=D("0"),
        net_exposure=D("0"),
        margin_initial=D("0"),
        margin_maintenance=D(maintenance),
        positions=(),
    )
    assert margin.margin_used == D(maintenance)
    assert margin.margin_available == D(equity) - D(maintenance)


def test_negative_availability_is_reported_not_clamped():
    margin = AccountMargin(
        equity=D("1400"),
        cash=D("0"),
        market_value_long=D("0"),
        market_value_short=D("0"),
        gross_exposure=D("0"),
        net_exposure=D("0"),
        margin_initial=D("0"),
        margin_maintenance=D("5150"),
        positions=(),
    )
    assert margin.margin_available == D("-3750")

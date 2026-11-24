"""The option margin book (metrics.option_margin.margin_book): explicit values and refusals.

The naked-call figures are the values the engine produces for a single short ATM call
(strike 100, 100-share multiplier, mark 2.5). `net_of_credit` is the initial margin less
the 1.5 entry credit on 100 shares (2250 - 150 = 2100).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal as D

import pytest

from trade_engine.domain.instruments import OptionContract, OptionRight
from trade_engine.metrics.option_margin import OptionMarginError, margin_book

NEAR = date(2026, 10, 16)


def _call(strike: str = "100", multiplier: int = 100) -> OptionContract:
    return OptionContract("AAPL", NEAR, D(strike), OptionRight("C"), multiplier)


def test_a_naked_call_without_an_entry_price_has_no_net_of_credit():
    call = _call()
    strategies, shares_left = margin_book(
        "AAPL", {call: -1}, D("0"), D("100"), {call: D("2.5")}, D("0.5"), D("0.25"), None
    )
    (strategy,) = strategies
    assert strategy.name == "Naked Call"
    assert strategy.initial == D("2250.00")
    assert strategy.maintenance == D("2250.00")
    assert strategy.net_of_credit is None
    assert shares_left == D("0")


def test_an_entry_price_nets_the_credit_from_the_initial_margin():
    call = _call()
    (strategy,), _ = margin_book(
        "AAPL", {call: -1}, D("0"), D("100"), {call: D("2.5")}, D("0.5"), D("0.25"), {call: D("1.5")}
    )
    assert strategy.initial == D("2250.00")
    assert strategy.net_of_credit == D("2100.00")


def test_an_empty_book_is_refused_with_the_multiplier_rule():
    with pytest.raises(OptionMarginError, match=r"mix contract multipliers \[\] \(I6\)"):
        margin_book("AAPL", {}, D("0"), D("100"), {}, D("0.5"), D("0.25"))


def test_options_with_different_multipliers_cannot_share_a_book():
    call = _call()
    mixed = _call(multiplier=10)
    with pytest.raises(OptionMarginError, match="mix contract multipliers"):
        margin_book(
            "AAPL", {call: -1, mixed: 1}, D("0"), D("100"), {call: D("1"), mixed: D("1")}, D("0.5"), D("0.25")
        )

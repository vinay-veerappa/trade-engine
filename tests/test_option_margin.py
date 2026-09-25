"""O3 option margin: LEAN's strategy matching and formulas, hand-computed per structure.

Every fixture prices AAPL at 100 unless it says otherwise, with Reg-T stock fractions
of 50% initial and 25% maintenance. Figures are in dollars for the whole position.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from types import MappingProxyType

import pytest

from trade_engine.domain.instruments import Combo, ComboLeg, Equity, OptionContract, OptionRight, Side
from trade_engine.domain.portfolio import Position
from trade_engine.ledger.state import AccountState
from trade_engine.metrics import MarginOverride, account_margin, option_margin
from trade_engine.metrics.option_margin import (
    DEFINITIONS,
    OptionMarginError,
    StrategyDefinition,
    match_strategies,
    naked_margin,
    strategy_margin,
)

D = Decimal
NEAR = date(2026, 10, 16)
FAR = date(2027, 6, 17)
AAPL = Equity("AAPL")
GOOG = Equity("GOOG")


def opt(right: str, strike: str, expiry: date = NEAR, root: str = "AAPL", multiplier: int = 100) -> OptionContract:
    return OptionContract(root, expiry, D(strike), OptionRight(right), multiplier)


def state(holdings: dict, marks: dict, cash: str = "0") -> AccountState:
    positions = {
        instrument: Position("ACC", instrument, D(str(quantity)), D("1"))
        for instrument, quantity in holdings.items()
    }
    return AccountState(
        account_id="ACC",
        cash=D(cash),
        positions=MappingProxyType(positions),
        marks=MappingProxyType({instrument: D(price) for instrument, price in marks.items()}),
    )


def only(margin):
    assert len(margin.strategies) == 1, [s.name for s in margin.strategies]
    return margin.strategies[0]


# --- naked options ------------------------------------------------------------------------


def test_a_naked_put_is_premium_plus_twenty_percent_less_out_of_the_money() -> None:
    """95P at 2.00: max(20 - 5, 9.5) = 15, plus 2.00 = 17 a share, 1,700.
    The rules doc's max(20%S - OTM + premium, 10%K + premium) gives the same."""
    put = opt("P", "95")
    margin = account_margin(state({put: -1}, {put: "2.00", AAPL: "100"}))
    naked = only(margin)
    assert naked.name == "Naked Put"
    assert naked.initial == naked.maintenance == D("1700.00")
    assert naked.cash_secured == D("9500")
    assert margin.margin_maintenance == D("1700.00")


def test_a_far_out_of_the_money_put_is_held_to_ten_percent_of_its_strike() -> None:
    """70P at 0.10: 20 - 30 is negative, so 10% of 70 = 7, plus 0.10 = 710."""
    put = opt("P", "70")
    naked = only(account_margin(state({put: -1}, {put: "0.10", AAPL: "100"})))
    assert naked.maintenance == D("710.00")


def test_a_naked_call_floors_at_ten_percent_of_the_underlying_and_cash_cannot_secure_it() -> None:
    """Two 130C at 0.50: 20 - 30 < 0, so 10% of S = 10, plus 0.50 = 10.50 x 200 = 2,100."""
    call = opt("C", "130")
    naked = only(account_margin(state({call: -2}, {call: "0.50", AAPL: "100"})))
    assert naked.name == "Naked Call"
    assert naked.maintenance == D("2100.00")
    assert naked.cash_secured is None


def test_an_index_option_uses_fifteen_percent() -> None:
    """SPXW 4900P at 10 with SPX at 5000: max(750 - 100, 490) = 650, plus 10 = 66,000."""
    put = opt("P", "4900", root="SPXW")
    margin = account_margin(state({put: -1}, {put: "10"}), underlying_prices={"SPX": D("5000")})
    assert only(margin).maintenance == D("66000")


def test_the_premium_is_the_current_mark_not_the_sale_price() -> None:
    """Sold at 2.00, now 6.00: 15 + 6 = 21 a share. LEAN keeps the sale price."""
    put = opt("P", "95")
    assert only(account_margin(state({put: -1}, {put: "6.00", AAPL: "100"}))).maintenance == D("2100.00")


def test_a_long_option_needs_its_premium_up_front_and_nothing_after() -> None:
    call = opt("C", "105")
    long = only(account_margin(state({call: 3}, {call: "1.20", AAPL: "100"})))
    assert long.name == "Long Call"
    assert long.initial == D("360.00")
    assert long.maintenance == D("0")
    assert long.cash_secured == D("0")


def test_naked_margin_is_nothing_for_a_long_contract() -> None:
    assert naked_margin(opt("P", "95"), 1, D("100"), D("2")) == D("0")


# --- verticals ----------------------------------------------------------------------------


def test_a_bull_put_spread_is_its_width() -> None:
    """Three 95/90 put spreads sold for 1.20: width 5 x 100 x 3 = 1,500. The credit sits
    in cash, so this is the rules doc's width - credit (380 each) plus that credit."""
    short, long = opt("P", "95"), opt("P", "90")
    margin = account_margin(state({short: -3, long: 3}, {short: "2.00", long: "0.80", AAPL: "100"}))
    spread = only(margin)
    assert spread.name == "Bull Put Spread"
    assert spread.quantity == 3
    assert spread.initial == spread.maintenance == spread.cash_secured == D("1500")


def test_a_bear_call_spread_is_its_width() -> None:
    short, long = opt("C", "105"), opt("C", "110")
    spread = only(account_margin(state({short: -1, long: 1}, {short: "1.50", long: "0.50", AAPL: "100"})))
    assert spread.name == "Bear Call Spread"
    assert spread.maintenance == D("500")


def test_a_debit_spread_needs_only_its_debit() -> None:
    """Long 100C 3.00, short 105C 1.00: nothing at risk beyond the 200 paid."""
    long, short = opt("C", "100"), opt("C", "105")
    spread = only(account_margin(state({long: 1, short: -1}, {long: "3.00", short: "1.00", AAPL: "100"})))
    assert spread.name == "Bull Call Spread"
    assert spread.maintenance == D("0")
    assert spread.initial == D("200.00")
    assert spread.cash_secured == D("0")


def test_a_bear_put_debit_spread() -> None:
    long, short = opt("P", "100"), opt("P", "95")
    spread = only(account_margin(state({long: 2, short: -2}, {long: "3.00", short: "1.50", AAPL: "100"})))
    assert spread.name == "Bear Put Spread"
    assert (spread.initial, spread.maintenance) == (D("300.00"), D("0"))


def test_legs_of_different_expiries_are_not_a_vertical() -> None:
    short, long = opt("P", "95"), opt("P", "90", FAR)
    names = [s.name for s in account_margin(state({short: -1, long: 1}, {short: "2", long: "3", AAPL: "100"})).strategies]
    assert names == ["Put Diagonal Spread"]


# --- stock and options --------------------------------------------------------------------


def test_an_out_of_the_money_covered_call_is_the_shares_maintenance() -> None:
    """100 shares, short 105C at 1.20: maintenance max(0 + 25% x 100 x 100, 2,500) = 2,500;
    initial 0.8 x 120 + 50% x 10,000 = 5,096 (LEAN's IB-inferred formula)."""
    call = opt("C", "105")
    margin = account_margin(state({AAPL: 100, call: -1}, {AAPL: "100", call: "1.20"}))
    covered = only(margin)
    assert covered.name == "Covered Call"
    assert covered.shares == D("100")
    assert covered.maintenance == D("2500.00")
    assert covered.initial == D("5096.000")
    assert covered.cash_secured == D("0")
    assert margin.positions == ()  # the shares are inside the strategy
    assert margin.margin_maintenance == D("2500.00")


def test_an_in_the_money_covered_call_adds_its_intrinsic_value() -> None:
    """Short 95C at 6.00: ITM 5 x 100 = 500, plus 25% of 100 shares at min(100, 95) = 2,375:
    2,875 beats 2,500."""
    call = opt("C", "95")
    covered = only(account_margin(state({AAPL: 100, call: -1}, {AAPL: "100", call: "6.00"})))
    assert covered.maintenance == D("2875.00")
    assert covered.initial == D("5480.000")


def test_shares_beyond_the_covered_calls_are_margined_as_stock() -> None:
    """GOOG 1,620 with five short calls: 500 shares cover the calls, 1,120 are plain stock,
    maintenance 25% x 1,120 x 200 = 56,000 on top of the calls' 25,000."""
    call = opt("C", "220", root="GOOG")
    margin = account_margin(state({GOOG: 1620, call: -5}, {GOOG: "200", call: "3.00"}))
    covered = only(margin)
    assert (covered.name, covered.quantity, covered.shares) == ("Covered Call", 5, D("500"))
    assert covered.maintenance == D("25000.00")
    (stock,) = margin.positions
    assert (stock.symbol, stock.quantity, stock.maintenance) == ("GOOG", D("1120"), D("56000.00"))
    assert margin.market_value_long == D("324000")
    assert margin.market_value_short == D("-1500.00")


def test_calls_beyond_the_shares_are_naked() -> None:
    """150 shares cover one call; the second is naked."""
    call = opt("C", "105")
    margin = account_margin(state({AAPL: 150, call: -2}, {AAPL: "100", call: "1.00"}))
    assert sorted(s.name for s in margin.strategies) == ["Covered Call", "Naked Call"]
    (stock,) = margin.positions
    assert stock.quantity == D("50")


def test_a_protective_put_is_the_lesser_of_ten_percent_plus_otm_and_the_shares() -> None:
    """100 shares + 95P at 1.00: min(9.5 + 5 = 14.5 x 100, 2,500) = 1,450;
    initial 5,000 + the 100 paid."""
    put = opt("P", "95")
    hedge = only(account_margin(state({AAPL: 100, put: 1}, {AAPL: "100", put: "1.00"})))
    assert hedge.name == "Protective Put"
    assert hedge.maintenance == D("1450.0")
    assert hedge.initial == D("5100.00")


def test_a_collar_is_the_lesser_of_the_put_side_and_a_quarter_of_the_call_strike() -> None:
    """100 shares, short 110C at 1.00, long 90P at 1.50: min(9 + 10, 27.5) x 100 = 1,900;
    initial 5,000 + no call ITM + 50 net debit."""
    call, put = opt("C", "110"), opt("P", "90")
    collar = only(account_margin(state({AAPL: 100, call: -1, put: 1}, {AAPL: "100", call: "1.00", put: "1.50"})))
    assert collar.name == "Protective Collar"
    assert collar.maintenance == D("1900.0")
    assert collar.initial == D("5050.00")


def test_a_collar_needs_the_put_below_the_call() -> None:
    call, put = opt("C", "95"), opt("P", "105")
    names = sorted(s.name for s in account_margin(
        state({AAPL: 100, call: -1, put: 1}, {AAPL: "100", call: "6", put: "6"})).strategies)
    assert names == ["Covered Call", "Long Put"]


def test_a_covered_put_is_the_short_shares_initial_plus_the_puts_intrinsic() -> None:
    """Short 100 shares, short 105P at 6: 5,000 + 500. LEAN charges the shares' initial
    margin even for maintenance, so the account keeps the cheaper split instead: a naked
    put (max(10.5, 20) + 6 = 2,600) and the short shares as stock (2,500)."""
    put = opt("P", "105")
    (matched,), _ = match_strategies({put: -1}, -1)
    covered = strategy_margin(matched, "AAPL", D("100"), {put: D("6")}, D("0.5"), D("0.25"))
    assert covered.name == "Covered Put"
    assert covered.initial == covered.maintenance == D("5500.00")
    assert covered.cash_secured is None
    margin = account_margin(state({AAPL: -100, put: -1}, {AAPL: "100", put: "6"}))
    assert [s.name for s in margin.strategies] == ["Naked Put"]
    assert margin.margin_maintenance == D("5100.00")


def test_a_protective_call_on_short_shares() -> None:
    call = opt("C", "105")
    hedge = only(account_margin(state({AAPL: -100, call: 1}, {AAPL: "100", call: "1"})))
    assert hedge.name == "Protective Call"
    assert hedge.maintenance == D("1550.0")  # min(10.5 + 5 = 15.5 x 100, 2,500)


def test_a_symbol_override_changes_the_stock_inside_a_strategy() -> None:
    call = opt("C", "105")
    override = {"AAPL": MarginOverride(initial=D("0.60"), maintenance=D("0.40"))}
    covered = only(account_margin(state({AAPL: 100, call: -1}, {AAPL: "100", call: "1.20"}), override))
    assert covered.maintenance == D("4000.00")


# --- diagonals and calendars --------------------------------------------------------------


def test_a_poor_mans_covered_call_is_recognised_as_a_diagonal() -> None:
    """Long Jun-2027 80C at 25, short Oct 110C at 1: the long is deeper in the money and
    outlives the short, so nothing is at risk beyond the 2,400 net debit (rules doc §6.1)."""
    leaps, short = opt("C", "80", FAR), opt("C", "110")
    margin = account_margin(state({leaps: 1, short: -1}, {leaps: "25", short: "1", AAPL: "100"}))
    diagonal = only(margin)
    assert diagonal.name == "Call Diagonal Spread"
    assert diagonal.legs == ((short, -1), (leaps, 1))
    assert diagonal.maintenance == D("0")
    assert diagonal.initial == D("2400")


def test_lean_alone_would_split_a_diagonal_into_a_naked_call_and_a_long_call() -> None:
    """Without the diagonal definition the short 110C is charged naked margin."""
    leaps, short = opt("C", "80", FAR), opt("C", "110")
    lean = tuple(d for d in DEFINITIONS if "Diagonal" not in d.name)
    matched, _ = match_strategies({leaps: 1, short: -1}, 0, lean)
    assert sorted(s.name for s in matched) == ["Long Call", "Naked Call"]


def test_a_diagonal_whose_long_is_further_out_of_the_money_is_charged_the_width() -> None:
    """Short Oct 105C, long Jun 115C at 3: width 10 x 100 = 1,000, plus 100 net debit."""
    short, long = opt("C", "105"), opt("C", "115", FAR)
    diagonal = only(account_margin(state({short: -1, long: 1}, {short: "2", long: "3", AAPL: "100"})))
    assert diagonal.maintenance == D("1000")
    assert diagonal.initial == D("1100")


def test_a_long_that_expires_before_its_short_covers_nothing() -> None:
    """Long Oct 80C, short Jun 110C: the short is naked once October passes."""
    long, short = opt("C", "80"), opt("C", "110", FAR)
    names = sorted(s.name for s in account_margin(
        state({long: 1, short: -1}, {long: "21", short: "4", AAPL: "100"})).strategies)
    assert names == ["Long Call", "Naked Call"]


def test_a_put_diagonal() -> None:
    """Short Oct 95P, long Jun 90P: width 5 x 100."""
    short, long = opt("P", "95"), opt("P", "90", FAR)
    diagonal = only(account_margin(state({short: -1, long: 1}, {short: "2", long: "4", AAPL: "100"})))
    assert diagonal.name == "Put Diagonal Spread"
    assert diagonal.maintenance == D("500")


def test_a_same_strike_pair_is_a_calendar_not_a_diagonal() -> None:
    short, long = opt("C", "100"), opt("C", "100", FAR)
    calendar = only(account_margin(state({short: -1, long: 1}, {short: "2", long: "6", AAPL: "100"})))
    assert calendar.name == "Call Calendar Spread"
    assert (calendar.initial, calendar.maintenance) == (D("400"), D("0"))


def test_a_short_calendar_is_its_short_legs_naked_margin() -> None:
    """Long Oct 100P, short Jun 100P at 6: max(20, 10) + 6 = 26 x 100."""
    long, short = opt("P", "100"), opt("P", "100", FAR)
    calendar = only(account_margin(state({long: 1, short: -1}, {long: "2", short: "6", AAPL: "100"})))
    assert calendar.name == "Short Put Calendar Spread"
    assert calendar.maintenance == D("2600")
    assert calendar.cash_secured == D("10000")


# --- matching -----------------------------------------------------------------------------


def test_greedy_matching_takes_every_unit_that_fits() -> None:
    """Five short puts and three longs: three spreads, two naked."""
    short, long = opt("P", "95"), opt("P", "90")
    matched, _ = match_strategies({short: -5, long: 3})
    assert [(s.name, s.quantity) for s in matched] == [("Bull Put Spread", 3), ("Naked Put", 2)]


def test_the_collar_is_tried_before_the_covered_call() -> None:
    """Three legs outrank two, so the shares go to the collar, not a covered call."""
    call, put = opt("C", "110"), opt("P", "90")
    matched, left = match_strategies({call: -1, put: 1}, 1)
    assert [s.name for s in matched] == ["Protective Collar"]
    assert left == 0


def test_the_same_book_always_matches_the_same_way() -> None:
    book = {opt("C", "105"): -1, opt("C", "110"): 1, opt("C", "115"): 1, opt("C", "100"): -1}
    first, _ = match_strategies(book)
    again, _ = match_strategies(dict(reversed(list(book.items()))))
    assert first == again


def test_only_one_leg_definitions_leave_a_short_uncovered() -> None:
    """Why LEAN's second matching order isn't ported: it moves the definitions leaving a
    short leg uncovered to the end, and here those are only the one-leg naked ones, which
    come last anyway. A ladder or backspread added later breaks this and needs it."""

    def uncovered(d: StrategyDefinition) -> bool:
        calls = sum(leg.quantity for leg in d.legs if leg.right is OptionRight.CALL)
        puts = sum(leg.quantity for leg in d.legs if leg.right is OptionRight.PUT)
        return -calls > max(0, d.underlying_lots) or -puts > max(0, -d.underlying_lots)

    assert {d.name for d in DEFINITIONS if uncovered(d)} == {"Naked Call", "Naked Put"}
    assert all(d.leg_count == 1 for d in DEFINITIONS if uncovered(d))


def test_spx_and_spxw_legs_pair_on_the_one_index() -> None:
    short, long = opt("P", "4900", root="SPX"), opt("P", "4850", root="SPXW")
    margin = account_margin(state({short: -1, long: 1}, {short: "10", long: "6"}), underlying_prices={"SPX": D("5000")})
    assert only(margin).name == "Bull Put Spread"


# --- account figures and refusals ---------------------------------------------------------


def test_option_market_value_uses_the_contract_multiplier() -> None:
    """Short one 95P at 2.00 with 10,000 cash: equity 10,000 - 200."""
    put = opt("P", "95")
    margin = account_margin(state({put: -1}, {put: "2.00", AAPL: "100"}, cash="10000"))
    assert margin.market_value_short == D("-200.00")
    assert margin.equity == D("9800.00")
    assert margin.margin_available == D("8100.00")


def test_options_without_an_underlying_price_refuse() -> None:
    put = opt("P", "95")
    with pytest.raises(OptionMarginError, match="No underlying price for AAPL"):
        account_margin(state({put: -1}, {put: "2.00"}))


def test_an_explicit_underlying_price_wins_over_the_shares_mark() -> None:
    put = opt("P", "95")
    margin = account_margin(state({put: -1}, {put: "2.00", AAPL: "100"}), underlying_prices={"AAPL": D("90")})
    assert only(margin).maintenance == D("2000.00")  # max(18 - 0, 9.5) + 2.00


def test_an_option_without_a_mark_refuses() -> None:
    put = opt("P", "95")
    with pytest.raises(ValueError, match="No session-close mark"):
        account_margin(state({put: -1}, {AAPL: "100"}))


def test_a_fractional_contract_refuses() -> None:
    put = opt("P", "95")
    with pytest.raises(OptionMarginError, match="fractional"):
        account_margin(state({put: "-1.5"}, {put: "2", AAPL: "100"}))


def test_mixed_contract_multipliers_refuse() -> None:
    standard, mini = opt("P", "95"), opt("P", "90", multiplier=10)
    with pytest.raises(OptionMarginError, match="mix contract multipliers"):
        account_margin(state({standard: -1, mini: 1}, {standard: "2", mini: "1", AAPL: "100"}))


def test_a_combo_position_refuses() -> None:
    combo = Combo([ComboLeg(opt("P", "95"), 1, Side.SELL), ComboLeg(opt("P", "90"), 1, Side.BUY)])
    with pytest.raises(OptionMarginError, match="per leg"):
        account_margin(state({combo: -1}, {combo: "1.2", AAPL: "100"}))


def test_a_book_of_only_shares_is_unchanged() -> None:
    margin = account_margin(state({AAPL: 100}, {AAPL: "100"}))
    assert margin.strategies == ()
    assert margin.margin_maintenance == D("2500.00")



def test_short_shares_do_not_cover_a_short_call() -> None:
    call = opt("C", "105")
    margin = account_margin(state({AAPL: -100, call: -1}, {AAPL: "100", call: "1"}))
    assert [s.name for s in margin.strategies] == ["Naked Call"]
    (stock,) = margin.positions
    assert stock.quantity == D("-100")


LADDER = {opt("P", "95"): -1, opt("P", "90"): 1, opt("P", "85"): -1, opt("P", "80"): 1}
LADDER_MARKS = {opt("P", "95"): "3", opt("P", "90"): "2", opt("P", "85"): "1.2", opt("P", "80"): "0.6", AAPL: "100"}


def test_leans_greedy_pairing_of_two_put_spreads_is_the_dear_one() -> None:
    """Bear Put Spread comes before Bull Put Spread, so the long 90P pairs with the short
    85P, leaving a 95/80 spread 15 wide."""
    matched, _ = match_strategies(LADDER)
    assert [(s.name, s.legs[0][0].strike, s.legs[1][0].strike) for s in matched] == [
        ("Bear Put Spread", D("90"), D("85")),
        ("Bull Put Spread", D("95"), D("80")),
    ]


def test_the_account_is_charged_the_cheapest_grouping() -> None:
    """Two 5-wide bull put spreads: 1,000, not LEAN's 1,500."""
    margin = account_margin(state(LADDER, LADDER_MARKS))
    assert [s.name for s in margin.strategies] == ["Bull Put Spread", "Bull Put Spread"]
    assert margin.margin_maintenance == D("1000")


def test_a_book_too_large_to_search_keeps_leans_grouping(monkeypatch) -> None:
    monkeypatch.setattr(option_margin, "SEARCH_LIMIT", 1)
    margin = account_margin(state(LADDER, LADDER_MARKS))
    assert margin.margin_maintenance == D("1500")


def test_a_tie_keeps_leans_grouping() -> None:
    """Short 95P and 90P against longs at 85 and 80: 95/85 + 90/80 or 95/80 + 90/85
    both cost 2,000. LEAN pairs each short with the first long below it in strike order."""
    book = {opt("P", "95"): -1, opt("P", "90"): -1, opt("P", "85"): 1, opt("P", "80"): 1}
    marks = {c: "1" for c in book} | {AAPL: "100"}
    greedy, _ = match_strategies(book)
    margin = account_margin(state(book, marks))
    assert margin.margin_maintenance == D("2000")
    assert [s.legs for s in margin.strategies] == [s.legs for s in greedy]


def test_leftover_shares_count_in_the_comparison() -> None:
    """100 shares and a short 105C: a covered call (2,500) beats a naked call plus the
    shares as stock (2,000 + 2,500)."""
    call = opt("C", "105")
    margin = account_margin(state({AAPL: 100, call: -1}, {AAPL: "100", call: "1"}))
    assert [s.name for s in margin.strategies] == ["Covered Call"]


def test_the_diagonal_definition_needs_a_different_strike() -> None:
    """A same-strike pair is a calendar; the diagonal alone must not take it."""
    (diagonal,) = (d for d in DEFINITIONS if d.name == "Call Diagonal Spread")
    with pytest.raises(OptionMarginError, match="matched no definition"):
        match_strategies({opt("C", "100"): -1, opt("C", "100", FAR): 1}, 0, (diagonal,))


def test_strategy_margin_refuses_what_it_cannot_price() -> None:
    put = opt("P", "95")
    matched, _ = match_strategies({put: -1})
    fractions = (D("0.5"), D("0.25"))
    with pytest.raises(OptionMarginError, match="No session-close mark"):
        strategy_margin(matched[0], "AAPL", D("100"), {}, *fractions)
    with pytest.raises(OptionMarginError, match="No price for AAPL"):
        strategy_margin(matched[0], "AAPL", D("0"), {put: D("2")}, *fractions)


def test_a_protective_put_far_below_is_held_to_the_shares_maintenance() -> None:
    """Long 60P: 6 + 40 = 46 x 100 = 4,600 exceeds the shares' 2,500."""
    put = opt("P", "60")
    hedge = only(account_margin(state({AAPL: 100, put: 1}, {AAPL: "100", put: "0.05"})))
    assert hedge.name == "Protective Put"
    assert hedge.maintenance == D("2500.00")


def test_a_wide_collar_is_held_to_a_quarter_of_the_call_strike() -> None:
    """Long 60P, short 95C: min(6 + 40, 23.75) x 100 = 2,375, less than the covered
    call's 500 + 2,375. (With the call out of the money a covered call and a long put
    are cheaper, and that grouping is kept instead.)"""
    call, put = opt("C", "95"), opt("P", "60")
    collar = only(account_margin(state({AAPL: 100, call: -1, put: 1}, {AAPL: "100", call: "6", put: "0.05"})))
    assert collar.name == "Protective Collar"
    assert collar.maintenance == D("2375.00")


def test_a_collar_with_the_call_in_the_money_adds_its_intrinsic_to_initial() -> None:
    """Short 95C at 6, long 90P at 1: 5,000 + 500 in the money; the net credit adds nothing."""
    call, put = opt("C", "95"), opt("P", "90")
    collar = only(account_margin(state({AAPL: 100, call: -1, put: 1}, {AAPL: "100", call: "6", put: "1"})))
    assert collar.name == "Protective Collar"
    assert collar.initial == D("5500")


def test_short_lots_never_match_a_long_lot_definition() -> None:
    call = opt("C", "105")
    matched, left = match_strategies({call: -1}, -1)
    assert [s.name for s in matched] == ["Naked Call"]
    assert left == -1


def test_the_search_places_a_lone_long_option_too() -> None:
    """The ladder plus an unrelated long call: still 1,000 for the spreads."""
    extra = opt("C", "120")
    margin = account_margin(state(LADDER | {extra: 1}, LADDER_MARKS | {extra: "0.40"}))
    assert sorted(s.name for s in margin.strategies) == ["Bull Put Spread", "Bull Put Spread", "Long Call"]
    assert margin.margin_maintenance == D("1000")


def test_an_equal_cost_grouping_does_not_replace_leans() -> None:
    """Short 90C, long 95C, short 105C: LEAN's 90/95 bear call spread and a naked 105C
    (500 + 1,600) cost the same as a naked 90C and a 95/105 bull call spread (2,100 + 0)."""
    book = {opt("C", "90"): -1, opt("C", "95"): 1, opt("C", "105"): -1}
    margin = account_margin(state(book, {c: "1" for c in book} | {AAPL: "100"}))
    assert [s.name for s in margin.strategies] == ["Bear Call Spread", "Naked Call"]
    assert margin.margin_maintenance == D("2100")

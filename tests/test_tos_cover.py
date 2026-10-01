"""S1b: the cover rule (docs TOS_STOCK_AND_LEAPS_MIRROR, "S1b design").

An order waits iff filling it in full would leave MORE short calls uncovered on its underlying than there
are now. A lot of shares or a qualifying long call covers a short call; the mirror book (proven fills) is
the supply, pessimistically; resting sells are demand. Every guard has a firing and a non-firing test (§0.2).
"""

import sys
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pytest

from trade_engine.domain.instruments import Combo, ComboLeg, Equity, OptionContract, Side
from trade_engine.domain.orders import Order, OrderType, TimeInForce
from trade_engine.ledger.events import MirrorAllocation, MirrorQueued
from trade_engine.ledger.mirror import MirrorState, MirrorTicketState
from trade_engine.tos_paper.cover import cover_reason, covers, holdings, uncovered

T = datetime(2026, 9, 30, 14, 0, tzinfo=timezone.utc)
PM_A = "D-00000001"
CC = "OPT_COVERED_CALL"
PMCC = "OPT_PMCC"
OCT, NOV, DEC, JAN27 = date(2026, 10, 16), date(2026, 11, 20), date(2026, 12, 18), date(2027, 1, 15)
AAPL = Equity("AAPL")
MSFT = Equity("MSFT")


def _call(strike="210", expiry=OCT, und="AAPL", right="C", multiplier=100) -> OptionContract:
    return OptionContract(underlying=und, expiry=expiry, strike=Decimal(strike), right=right, multiplier=multiplier)


C210 = _call("210")
D = Decimal


# -- covers: one long call against one short call -----------------------------------------------


@pytest.mark.parametrize(
    "long,short,expected",
    [
        (_call("200", DEC), _call("210", OCT), True),   # a debit diagonal: longer, lower
        (_call("210", OCT), _call("210", OCT), True),   # equal strike and expiry still cover (a calendar of one)
        (_call("210", NOV), _call("210", OCT), True),   # longer, equal strike
        (_call("200", OCT), _call("210", OCT), True),   # equal expiry, lower strike
        (_call("215", DEC), _call("210", OCT), False),  # a credit diagonal: the long strike is above
        (_call("200", OCT), _call("210", NOV), False),  # the long expires first
        (_call("200", DEC, und="MSFT"), _call("210", OCT), False),  # another underlying
        (_call("200", DEC, right="P"), _call("210", OCT), False),   # a put covers no call
        (_call("200", DEC, multiplier=10), _call("210", OCT), False),  # a mini is not a lot of 100
    ],
)
def test_a_long_call_covers_a_short_call_only_as_a_debit_diagonal(long, short, expected) -> None:
    assert covers(long, short) is expected


# -- uncovered: the matching ---------------------------------------------------------------------


def test_nothing_held_leaves_nothing_uncovered() -> None:
    assert uncovered({}) == {}


def test_a_short_call_with_nothing_behind_it_is_uncovered() -> None:
    assert uncovered({C210: D(-1)}) == {"AAPL": D(1)}


@pytest.mark.parametrize(
    "shares,shorts,left",
    [(100, 1, 0), (99, 1, 1), (0, 1, 1), (200, 2, 0), (200, 3, 1), (150, 2, 1), (500, 1, 0)],
)
def test_a_lot_of_shares_covers_one_short_call(shares, shorts, left) -> None:
    held = {AAPL: D(shares), C210: D(-shorts)} if shares else {C210: D(-shorts)}
    assert uncovered(held) == ({"AAPL": D(left)} if left else {})


def test_short_shares_cover_nothing() -> None:
    assert uncovered({AAPL: D(-100), C210: D(-1)}) == {"AAPL": D(1)}


def test_a_long_call_covers_a_short_call_and_one_long_never_covers_two() -> None:
    leaps = _call("200", DEC)
    assert uncovered({leaps: D(1), C210: D(-1)}) == {}
    assert uncovered({leaps: D(1), C210: D(-2)}) == {"AAPL": D(1)}
    assert uncovered({leaps: D(2), C210: D(-2)}) == {}


def test_a_credit_diagonal_and_a_longer_short_are_uncovered() -> None:
    assert uncovered({_call("215", DEC): D(1), C210: D(-1)}) == {"AAPL": D(1)}
    assert uncovered({_call("200", OCT): D(1), _call("210", NOV): D(-1)}) == {"AAPL": D(1)}


def test_the_matching_is_a_maximum_one_not_a_greedy_one() -> None:
    s1, s2 = _call("210", OCT), _call("210", NOV)
    wide = _call("200", DEC)    # covers both shorts
    narrow = _call("205", OCT)  # covers only the October short
    assert covers(wide, s1) and covers(wide, s2) and covers(narrow, s1) and not covers(narrow, s2)
    # Giving `wide` to the October short first would strand the November one; a maximum matching covers both.
    assert uncovered({wide: D(1), narrow: D(1), s1: D(-1), s2: D(-1)}) == {}


def test_shares_and_a_long_call_cover_together() -> None:
    leaps = _call("200", DEC)
    held = {AAPL: D(100), leaps: D(1), C210: D(-2)}
    assert uncovered(held) == {}
    assert uncovered({**held, C210: D(-3)}) == {"AAPL": D(1)}


def test_each_underlying_is_covered_by_its_own_shares_only() -> None:
    msft = _call("400", und="MSFT")
    assert uncovered({AAPL: D(100), C210: D(-1), msft: D(-1)}) == {"MSFT": D(1)}
    assert uncovered({AAPL: D(100), MSFT: D(100), C210: D(-1), msft: D(-1)}) == {}
    assert uncovered({C210: D(-1), msft: D(-1)}) == {"AAPL": D(1), "MSFT": D(1)}


def test_a_short_put_and_a_zero_row_are_not_short_calls() -> None:
    assert uncovered({_call("200", right="P"): D(-1), C210: D(0), AAPL: D(0)}) == {}


# -- holdings: the mirror book, pessimistically --------------------------------------------------


def _queued(instrument, side, qty, key="tos:a", account=CC) -> MirrorQueued:
    return MirrorQueued(
        venue=PM_A, ticket_key=key, instrument=instrument, side=side, quantity=D(qty), order_type=OrderType.LIMIT,
        limit_price=D("1.00"), tif=TimeInForce.DAY, allocations=(MirrorAllocation("so-" + key, account, D(qty)),), at=T,
    )


def _ticket(instrument, side, qty, *, filled="0", closed=False, key="tos:a") -> MirrorTicketState:
    return MirrorTicketState(queued=_queued(instrument, side, qty, key), filled=D(filled), closed=closed)


def _state(book=None, tickets=()) -> MirrorState:
    return MirrorState(
        venue=PM_A,
        tickets=MappingProxyType({t.key: t for t in tickets}),
        book=MappingProxyType({key: D(qty) for key, qty in (book or {}).items()}),
    )


def test_an_empty_mirror_holds_nothing() -> None:
    assert holdings(_state()) == {}


def test_the_book_is_summed_over_accounts() -> None:
    state = _state({(CC, AAPL): 100, (PMCC, AAPL): 200, (CC, C210): -1})
    assert holdings(state) == {AAPL: D(300), C210: D(-1)}


def test_a_resting_sell_leaves_the_holdings_but_a_resting_buy_is_not_credited() -> None:
    selling = _state({(CC, AAPL): 100}, [_ticket(AAPL, Side.SELL, "100")])
    assert holdings(selling).get(AAPL, D(0)) == D(0)
    buying = _state({}, [_ticket(AAPL, Side.BUY, "100")])
    assert holdings(buying).get(AAPL, D(0)) == D(0)  # supply is proven fills only
    short = _state({}, [_ticket(C210, Side.SELL, "1")])
    assert holdings(short) == {C210: D(-1)}  # a resting short call is demand already


def test_only_the_unfilled_remainder_of_a_resting_sell_counts() -> None:
    # 40 of 100 sold: the book already holds 60, and the 60 still resting are leaving too.
    state = _state({(CC, AAPL): 60}, [_ticket(AAPL, Side.SELL, "100", filled="40")])
    assert holdings(state).get(AAPL, D(0)) == D(0)


def test_a_filled_or_closed_ticket_adds_nothing_beyond_the_book() -> None:
    done = _ticket(AAPL, Side.SELL, "100", filled="100", key="tos:done")
    dead = _ticket(C210, Side.SELL, "1", closed=True, key="tos:dead")
    assert holdings(_state({(CC, AAPL): 100}, [done, dead])) == {AAPL: D(100)}


# -- cover_reason: the gate ----------------------------------------------------------------------


def _order(instrument, side, qty="1", oid="so-1", account=CC, limit="1.00") -> Order:
    return Order(
        order_id=oid, account_id=account, instrument=instrument, order_type=OrderType.LIMIT, side=side,
        quantity=D(qty), command_id=oid, created_at=T, limit_price=D(limit), tif=TimeInForce.DAY,
    )


@pytest.mark.parametrize(
    "instrument,side,qty",
    [
        (AAPL, Side.BUY, "100"),
        (C210, Side.BUY, "1"),                         # a buy, or a buy-to-close, never removes cover
        (_call("200", right="P"), Side.SELL, "1"),     # a short put is bounded and out of scope
        (_call("200", right="P"), Side.BUY, "1"),
        (Combo((ComboLeg(C210, 1, Side.SELL), ComboLeg(_call("215"), 1, Side.BUY))), Side.SELL, "1"),  # a vertical
    ],
)
def test_an_order_that_cannot_remove_cover_or_open_a_short_call_never_waits(instrument, side, qty) -> None:
    assert cover_reason(_state(), _order(instrument, side, qty)) is None


def test_a_short_call_waits_without_proven_cover_and_says_what_is_missing() -> None:
    reason = cover_reason(_state(), _order(C210, Side.SELL))
    assert reason is not None
    assert "uncovered" in reason and "AAPL" in reason and "proven venue fills only" in reason


def test_a_short_call_goes_when_a_lot_of_shares_is_proven() -> None:
    assert cover_reason(_state({(CC, AAPL): 100}), _order(C210, Side.SELL)) is None


@pytest.mark.parametrize("shares", [0, 50, 99])
def test_a_partial_stock_fill_covers_no_contract(shares) -> None:
    book = {(CC, AAPL): shares} if shares else {}
    assert cover_reason(_state(book), _order(C210, Side.SELL)) is not None


def test_a_resting_buy_of_the_shares_is_not_cover_yet() -> None:
    state = _state({}, [_ticket(AAPL, Side.BUY, "100")])
    assert cover_reason(state, _order(C210, Side.SELL)) is not None


def test_shares_already_leaving_do_not_cover_a_new_short_call() -> None:
    state = _state({(CC, AAPL): 100}, [_ticket(AAPL, Side.SELL, "100")])
    assert cover_reason(state, _order(C210, Side.SELL)) is not None


def test_a_short_call_resting_already_uses_the_cover() -> None:
    state = _state({(CC, AAPL): 100}, [_ticket(C210, Side.SELL, "1")])
    assert cover_reason(state, _order(C210, Side.SELL, oid="so-2")) is not None


def test_two_short_calls_in_one_batch_need_two_lots() -> None:
    state = _state({(CC, AAPL): 100})
    first, second = _order(C210, Side.SELL, oid="so-1"), _order(C210, Side.SELL, oid="so-2")
    assert cover_reason(state, first) is None and cover_reason(state, second) is None  # alone, each is covered
    assert cover_reason(state, second, accepted=[first]) is not None  # after the first went, the cover is spent
    assert cover_reason(_state({(CC, AAPL): 200}), second, accepted=[first]) is None


def test_an_accepted_buy_does_not_spend_cover() -> None:
    state = _state({(CC, AAPL): 100})
    buy = _order(AAPL, Side.BUY, "100", oid="so-b")
    assert cover_reason(state, _order(C210, Side.SELL, oid="so-2"), accepted=[buy]) is None


def test_a_long_call_covers_a_pmcc_short_call_and_a_credit_diagonal_does_not() -> None:
    leaps = _call("200", DEC)
    assert cover_reason(_state({(PMCC, leaps): 1}), _order(C210, Side.SELL, account=PMCC)) is None
    credit = _call("215", DEC)
    assert cover_reason(_state({(PMCC, credit): 1}), _order(C210, Side.SELL, account=PMCC)) is not None


def test_one_accounts_shares_cover_another_accounts_call_on_the_same_venue() -> None:
    state = _state({(CC, AAPL): 100})
    assert cover_reason(state, _order(C210, Side.SELL, account="OPT_WHEEL")) is None


def test_selling_the_shares_waits_while_a_short_call_rests_on_them() -> None:
    state = _state({(CC, AAPL): 100, (CC, C210): -1})
    reason = cover_reason(state, _order(AAPL, Side.SELL, "100"))
    assert reason is not None and "uncovered" in reason and "AAPL" in reason


def test_selling_only_the_spare_shares_goes() -> None:
    state = _state({(CC, AAPL): 200, (CC, C210): -1})
    assert cover_reason(state, _order(AAPL, Side.SELL, "100")) is None
    assert cover_reason(state, _order(AAPL, Side.SELL, "101")) is not None


def test_selling_all_the_shares_goes_when_no_short_call_rests_on_them() -> None:
    assert cover_reason(_state({(CC, AAPL): 100}), _order(AAPL, Side.SELL, "100")) is None


def test_the_close_order_buy_the_call_back_first_then_the_shares() -> None:
    # D7: the buy-to-close goes at once; the share sale waits until the book proves the call is gone.
    state = _state({(CC, AAPL): 100, (CC, C210): -1})
    assert cover_reason(state, _order(C210, Side.BUY, oid="btc")) is None
    assert cover_reason(state, _order(AAPL, Side.SELL, "100", oid="stc")) is not None
    after = _state({(CC, AAPL): 100})  # the buy-to-close filled and is in the book
    assert cover_reason(after, _order(AAPL, Side.SELL, "100", oid="stc")) is None


def test_a_resting_buy_to_close_is_not_credited_until_it_fills() -> None:
    state = _state({(CC, AAPL): 100, (CC, C210): -1}, [_ticket(C210, Side.BUY, "1")])
    assert cover_reason(state, _order(AAPL, Side.SELL, "100")) is not None


def test_selling_the_long_call_waits_while_a_short_call_rests_on_it() -> None:
    leaps = _call("200", DEC)
    state = _state({(PMCC, leaps): 1, (PMCC, C210): -1})
    assert cover_reason(state, _order(leaps, Side.SELL, account=PMCC)) is not None
    assert cover_reason(_state({(PMCC, leaps): 1}), _order(leaps, Side.SELL, account=PMCC)) is None


def test_a_short_call_that_was_already_uncovered_does_not_block_an_unrelated_order_but_a_new_one_waits() -> None:
    state = _state({(CC, C210): -1})  # uncovered from the start
    assert cover_reason(state, _order(AAPL, Side.BUY, "100")) is None
    assert cover_reason(state, _order(_call("205"), Side.SELL, oid="so-2")) is not None


def test_the_cover_of_one_underlying_never_covers_another() -> None:
    state = _state({(CC, AAPL): 100})
    assert cover_reason(state, _order(_call("400", und="MSFT"), Side.SELL)) is not None
    assert cover_reason(_state({(CC, MSFT): 100}), _order(_call("400", und="MSFT"), Side.SELL)) is None


def test_short_shares_alone_invent_no_uncovered_call_and_selling_short_never_waits() -> None:
    assert uncovered({AAPL: D(-100)}) == {}  # a negative lot count must floor at zero, never add cover or demand
    assert cover_reason(_state(), _order(AAPL, Side.SELL, "100")) is None
    assert cover_reason(_state({(CC, AAPL): -100}), _order(AAPL, Side.SELL, "100")) is None

"""O2: expiry, exercise and assignment after the close, on known answers (I9)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from trade_engine.calendar import get_calendar
from trade_engine.clock.replay import ReplayClock
from trade_engine.domain.instruments import Equity, OptionContract, OptionRight, Side
from trade_engine.domain.option_lifecycle import (
    Outcome,
    delivery,
    exercised_for_dividend,
    expiry_outcome,
    intrinsic,
)
from trade_engine.domain.option_roots import SettleTime
from trade_engine.domain.orders import Order, OrderType
from trade_engine.domain.portfolio import Fill
from trade_engine.interfaces.market_data import OptionQuote, StaleDataError
from trade_engine.ledger import CashFlow, Event, EventKind, Ledger, LedgerFoldError, OptionLifecycle, fold
from trade_engine.lifecycle import (
    Dividend,
    FixedDividends,
    FixedSettlements,
    LifecycleError,
    LifecyclePass,
    SettlementPrice,
)

CAL = get_calendar()
EXPIRY = date(2026, 10, 16)  # a Friday, the October monthly
OPENED = datetime(2026, 9, 24, 14, 0, tzinfo=UTC)
CLOSE = CAL.session_close(EXPIRY)  # 20:00 UTC
AFTER = CLOSE + timedelta(minutes=105)  # 17:45 ET, the EOD pass
START = Decimal("100000")
AAPL = Equity("AAPL")
D = Decimal


def option(right: str, strike: str, root: str = "AAPL", expiry: date = EXPIRY) -> OptionContract:
    return OptionContract(root, expiry, D(strike), OptionRight(right))


def pm(price: str, underlying: str = "AAPL", session: date = EXPIRY, at: datetime | None = None) -> SettlementPrice:
    return SettlementPrice(underlying, session, SettleTime.PM, D(price), "official close", at or CLOSE + timedelta(minutes=70))


class Book:
    """A ledger with one account's opening trades, as fills of submitted orders."""

    def __init__(self, tmp_path, account: str = "OPT_CSP") -> None:
        self.account = account
        self.ledger = Ledger(tmp_path / "ledger.db").open()
        self.n = 0
        self.ledger.append(
            Event(account=account, kind=EventKind.CASH_FLOW, ts_utc=OPENED, command_id=f"{account}:deposit",
                  payload=CashFlow(amount=START, kind="deposit", as_of=OPENED))
        )

    def trade(self, instrument, side: Side, quantity: str, price: str, account: str | None = None) -> None:
        account = account or self.account
        self.n += 1
        order = Order(
            order_id=f"o{self.n}", account_id=account, instrument=instrument, order_type=OrderType.MARKET,
            side=side, quantity=D(quantity), command_id=f"c{self.n}", created_at=OPENED,
        )
        fill = Fill(
            fill_id=f"f{self.n}", order_id=order.order_id, account_id=account, instrument=instrument,
            quantity=D(quantity), price=D(price), venue_env="sim", filled_at=OPENED, side=side,
        )
        self.ledger.extend([
            Event(account=account, kind=EventKind.ORDER_SUBMITTED, payload=order, ts_utc=OPENED, command_id=f"c{self.n}:submit"),
            Event(account=account, kind=EventKind.FILL, payload=fill, ts_utc=OPENED, command_id=f"c{self.n}:fill"),
        ])

    def settle(self, *prices: SettlementPrice, at: datetime = AFTER, session: date = EXPIRY, **sources):
        lifecycle = LifecyclePass(self.ledger, ReplayClock(at), CAL, FixedSettlements(list(prices)), **sources)
        return lifecycle.run(session)

    @property
    def state(self):
        return self.ledger.state(self.account)

    def qty(self, instrument) -> Decimal:
        position = self.state.positions.get(instrument)
        return position.quantity if position is not None else D(0)


@pytest.fixture
def book(tmp_path):
    b = Book(tmp_path)
    yield b
    b.ledger.close()


def assert_folds_the_same(book: Book) -> None:
    assert fold(book.ledger.events())[book.account] == book.state  # I2


# -- the known answers (build plan O2) --------------------------------------------


def test_a_cash_secured_put_assigned_in_the_money_holds_shares_at_strike_less_credit(book) -> None:
    put = option("P", "250")
    book.trade(put, Side.SELL, "1", "3.00")
    result = book.settle(pm("240"))
    [event] = result.events
    assert event.kind is EventKind.ASSIGNMENT and event.payload.held is Side.SELL
    shares = book.state.positions[AAPL]
    assert shares.quantity == 100
    assert [lot.cost_basis for lot in shares.open_lots] == [D("247.00")]  # strike - credit
    assert book.qty(put) == 0
    assert book.state.cash == START + 300 - 25000  # the credit, then the strike paid
    assert book.state.realized_pnl == 0  # the credit is in the shares' basis
    assert_folds_the_same(book)


def test_a_put_spread_through_both_strikes_loses_exactly_its_maximum(book) -> None:
    short, long = option("P", "250"), option("P", "245")
    book.trade(short, Side.SELL, "1", "3.00")
    book.trade(long, Side.BUY, "1", "1.00")
    result = book.settle(pm("230"))
    assert sorted(e.kind for e in result.events) == [EventKind.ASSIGNMENT, EventKind.EXERCISE]
    max_loss = (D(250) - D(245) - (D("3.00") - D("1.00"))) * 100  # width - net credit
    assert book.state.realized_pnl == -max_loss == D(-300)
    assert book.qty(AAPL) == 0 and book.qty(short) == 0 and book.qty(long) == 0
    assert book.state.cash == START - max_loss
    assert_folds_the_same(book)


def test_a_covered_call_in_the_money_is_called_away(book) -> None:
    call = option("C", "250")
    book.trade(AAPL, Side.BUY, "100", "240")
    book.trade(call, Side.SELL, "1", "2.00")
    book.settle(pm("255"))
    assert book.qty(AAPL) == 0 and book.qty(call) == 0
    assert book.state.realized_pnl == (D(250) + D(2) - D(240)) * 100  # strike + credit - basis
    assert book.state.cash == START + 1200
    assert_folds_the_same(book)


@pytest.mark.parametrize("side,price,pnl", [(Side.SELL, "3.00", 300), (Side.BUY, "2.00", -200)])
def test_an_out_of_the_money_option_expires_worthless(book, side, price, pnl) -> None:
    put = option("P", "250")
    book.trade(put, side, "1", price)
    [event] = book.settle(pm("260")).events
    assert event.kind is EventKind.EXPIRY
    assert book.qty(put) == 0 and book.qty(AAPL) == 0
    assert book.state.realized_pnl == pnl
    assert book.state.cash == START + pnl
    assert_folds_the_same(book)


def test_cash_settled_spxw_settles_to_cash_on_the_close(book) -> None:
    put = option("P", "5000", root="SPXW")
    book.trade(put, Side.SELL, "1", "10.00")
    [event] = book.settle(pm("4980", underlying="SPX")).events
    assert event.kind is EventKind.ASSIGNMENT
    assert book.qty(put) == 0 and Equity("SPX") not in book.state.positions
    assert book.state.cash == START + 1000 - 2000  # credit, then 20 intrinsic x 100
    assert book.state.realized_pnl == -1000
    assert_folds_the_same(book)


def test_am_settled_spx_settles_on_the_opening_settlement_not_the_close(book) -> None:
    call = option("C", "5000", root="SPX")
    book.trade(call, Side.BUY, "1", "20.00")
    am = SettlementPrice("SPX", EXPIRY, SettleTime.AM, D("5030.50"), "SET", CAL.session_open(EXPIRY) + timedelta(minutes=30))
    [event] = book.settle(am).events  # no PM price given: none is needed
    assert event.kind is EventKind.EXERCISE and event.payload.underlying_price == D("5030.50")
    assert book.state.cash == START - 2000 + 3050
    assert book.state.realized_pnl == 1050


# -- more of the same rules ---------------------------------------------------------


def test_an_exercised_long_call_buys_shares_at_strike_plus_premium(book) -> None:
    book.trade(option("C", "250"), Side.BUY, "2", "2.00")
    book.settle(pm("260"))
    shares = book.state.positions[AAPL]
    assert shares.quantity == 200 and shares.avg_cost == D("252.00")
    assert book.state.cash == START - 400 - 50000


def test_each_lot_keeps_its_own_premium_in_the_shares(book) -> None:
    put = option("P", "250")
    book.trade(put, Side.SELL, "1", "3.00")
    book.trade(put, Side.SELL, "1", "3.50")
    book.settle(pm("240"))
    assert [(lot.quantity, lot.cost_basis) for lot in book.state.positions[AAPL].open_lots] == [
        (100, D("247.00")),
        (100, D("246.50")),
    ]


@pytest.mark.parametrize("close,kind", [("250.009", EventKind.EXPIRY), ("250.01", EventKind.EXERCISE)])
def test_exercise_by_exception_starts_at_one_cent_in_the_money(book, close, kind) -> None:
    book.trade(option("C", "250"), Side.BUY, "1", "0.50")
    assert book.settle(pm(close)).events[0].kind is kind


def test_a_rerun_appends_nothing(book) -> None:
    book.trade(option("P", "250"), Side.SELL, "1", "3.00")
    first = book.settle(pm("240"))
    count = book.ledger.count()
    again = book.settle(pm("240"))
    assert again.events == () and book.ledger.count() == count
    assert first.events[0].command_id == "lifecycle:OPT_CSP:AAPL261016P00250000:2026-10-16"


def test_positions_that_do_not_expire_this_session_are_left_alone(book) -> None:
    later = option("P", "250", expiry=date(2026, 11, 20))
    book.trade(later, Side.SELL, "1", "3.00")
    assert book.settle().events == ()  # no price asked for, none given
    assert book.qty(later) == -1


# -- refusals of the pass ---------------------------------------------------------------


def test_the_pass_refuses_before_the_close(book) -> None:
    book.trade(option("P", "250"), Side.SELL, "1", "3.00")
    with pytest.raises(LifecycleError, match="before the 2026-10-16 close"):
        book.settle(pm("240"), at=CLOSE - timedelta(seconds=1))
    assert book.settle(pm("240", at=CLOSE), at=CLOSE).events  # at the close is after it


def test_a_settlement_price_known_before_the_settlement_refuses(book) -> None:
    book.trade(option("P", "250"), Side.SELL, "1", "3.00")
    with pytest.raises(LifecycleError, match="before the settlement instant"):
        book.settle(pm("240", at=CLOSE - timedelta(minutes=1)))


def test_a_settlement_price_from_after_the_clock_refuses(book) -> None:
    book.trade(option("P", "250"), Side.SELL, "1", "3.00")
    with pytest.raises(LifecycleError, match="look-ahead"):
        book.settle(pm("240", at=AFTER + timedelta(seconds=1)))


def test_a_missing_settlement_price_refuses_and_writes_nothing(book) -> None:
    book.trade(option("P", "250"), Side.SELL, "1", "3.00")
    count = book.ledger.count()
    with pytest.raises(StaleDataError, match="No PM settlement for AAPL"):
        book.settle(pm("240", underlying="MSFT"))
    assert book.ledger.count() == count


def test_one_account_refusing_writes_nothing_for_any(book) -> None:
    book.trade(option("P", "250"), Side.SELL, "1", "3.00")
    book.trade(option("P", "600", root="MSFT"), Side.SELL, "1", "5.00", account="OPT_OTHER")
    count = book.ledger.count()
    with pytest.raises(StaleDataError, match="MSFT"):
        book.settle(pm("240"))
    assert book.ledger.count() == count


def test_an_expiry_nobody_settled_refuses_a_later_session(book) -> None:
    book.trade(option("P", "250"), Side.SELL, "1", "3.00")
    monday = CAL.next_session(EXPIRY)
    with pytest.raises(LifecycleError, match="was never settled"):
        book.settle(at=CAL.session_close(monday) + timedelta(hours=1), session=monday)


def test_the_expected_answer_needs_the_right_settle_time(book) -> None:
    book.trade(option("C", "5000", root="SPX"), Side.BUY, "1", "20.00")
    with pytest.raises(StaleDataError, match="No AM settlement for SPX"):
        book.settle(pm("5030", underlying="SPX"))  # the close is not SPX's settlement


# -- early assignment before an ex-dividend date --------------------------------------

THURSDAY = date(2026, 10, 15)
NOVEMBER = date(2026, 11, 20)
THU_AFTER = CAL.session_close(THURSDAY) + timedelta(minutes=105)


def quotes(bid: str, contract: OptionContract):
    class Quotes:
        def quote(self, wanted, now):
            assert wanted == contract
            return OptionQuote(contract, D(bid), D(bid) + D("0.20"), D(10), D(10), THU_AFTER - timedelta(hours=2))
    return Quotes()


def dividend(amount: str = "0.26") -> FixedDividends:
    return FixedDividends({"AAPL": [Dividend("AAPL", EXPIRY, D(amount), "test", OPENED)]})


def settle_thursday(book, call, bid="10.20", close="260", **sources):
    sources.setdefault("dividends", dividend())
    sources.setdefault("quotes", quotes(bid, call))
    return book.settle(pm(close, session=THURSDAY, at=CAL.session_close(THURSDAY) + timedelta(hours=1)),
                       at=THU_AFTER, session=THURSDAY, **sources)


def test_a_short_call_is_assigned_early_when_the_dividend_beats_its_extrinsic(book) -> None:
    call = option("C", "250", expiry=NOVEMBER)
    book.trade(AAPL, Side.BUY, "100", "240")
    book.trade(call, Side.SELL, "1", "11.00")
    [event] = settle_thursday(book, call, bid="10.20").events  # extrinsic 0.20 < 0.26
    assert event.kind is EventKind.ASSIGNMENT and event.payload.early
    assert event.command_id.endswith(":2026-10-15:early")
    assert book.qty(AAPL) == 0 and book.qty(call) == 0
    assert book.state.realized_pnl == (D(250) + D(11) - D(240)) * 100


@pytest.mark.parametrize("bid,close", [("10.26", "260"), ("10.50", "260")])
def test_a_short_call_whose_extrinsic_covers_the_dividend_is_not_assigned(book, bid, close) -> None:
    call = option("C", "250", expiry=NOVEMBER)
    book.trade(call, Side.SELL, "1", "11.00")
    assert settle_thursday(book, call, bid=bid, close=close).events == ()
    assert book.qty(call) == -1


def test_no_early_assignment_without_a_dividend_or_out_of_the_money(book) -> None:
    call = option("C", "250", expiry=NOVEMBER)
    book.trade(call, Side.SELL, "1", "11.00")
    none = FixedDividends({"AAPL": []})
    assert settle_thursday(book, call, dividends=none).events == ()

    class NoQuotes:
        def quote(self, contract, now):
            raise AssertionError("an out-of-the-money call needs no quote")
    assert settle_thursday(book, call, close="249", quotes=NoQuotes()).events == ()


def test_early_assignment_refuses_without_the_data_to_decide(book) -> None:
    call = option("C", "250", expiry=NOVEMBER)
    book.trade(call, Side.SELL, "1", "11.00")
    with pytest.raises(LifecycleError, match="no dividend source"):
        settle_thursday(book, call, dividends=None)
    with pytest.raises(StaleDataError, match="No dividend record for AAPL"):
        settle_thursday(book, call, dividends=FixedDividends({}))
    with pytest.raises(LifecycleError, match="no option quote source"):
        settle_thursday(book, call, quotes=None)


def test_european_and_long_calls_are_never_assigned_early(book) -> None:
    book.trade(option("C", "5000", root="SPXW", expiry=NOVEMBER), Side.SELL, "1", "30.00")
    book.trade(option("C", "250", expiry=NOVEMBER), Side.BUY, "1", "11.00")
    assert settle_thursday(book, option("C", "250", expiry=NOVEMBER), dividends=None, quotes=None).events == ()


# -- the rules, pure ----------------------------------------------------------------


def test_the_rules_on_their_own() -> None:
    call, put = option("C", "250"), option("P", "250")
    assert intrinsic(call, D(260)) == 10 and intrinsic(call, D(240)) == 0
    assert intrinsic(put, D(240)) == 10 and intrinsic(put, D(260)) == 0
    assert expiry_outcome(put, Side.SELL, D(240)) is Outcome.ASSIGN
    assert expiry_outcome(put, Side.BUY, D(240)) is Outcome.EXERCISE
    assert expiry_outcome(put, Side.SELL, D(250)) is Outcome.EXPIRE
    assert delivery(put, Side.SELL, D(3)) == (Side.BUY, D(247))
    assert delivery(put, Side.BUY, D(3)) == (Side.SELL, D(247))
    assert delivery(call, Side.SELL, D(2)) == (Side.SELL, D(252))
    assert delivery(call, Side.BUY, D(2)) == (Side.BUY, D(252))
    assert exercised_for_dividend(option("C", "250", expiry=NOVEMBER), D(260), D("10.20"), D("0.26"))
    assert not exercised_for_dividend(option("C", "250", expiry=NOVEMBER), D(260), D("10.26"), D("0.26"))
    assert not exercised_for_dividend(option("C", "5000", root="SPXW", expiry=NOVEMBER), D(5100), D(100), D(1))
    # out of the money: nothing to exercise, however small the bid
    assert not exercised_for_dividend(option("C", "250", expiry=NOVEMBER), D(240), D("0.05"), D("0.26"))


# -- the fold refuses an event that contradicts the book or the rules ---------------------


def notice(contract, held=Side.SELL, price="240", quantity="1", early=False) -> OptionLifecycle:
    return OptionLifecycle("OPT_CSP", contract, D(quantity), held, D(price), "test", AFTER, "test", early)


@pytest.mark.parametrize(
    "kind,make,match",
    [
        (EventKind.EXPIRY, lambda put: notice(put, price="240"), "not expired worthless"),
        (EventKind.ASSIGNMENT, lambda put: notice(put, price="260"), "nobody exercises it"),
        (EventKind.EXERCISE, lambda put: notice(put), "applies to long contracts"),
        (EventKind.ASSIGNMENT, lambda put: notice(put, held=Side.BUY), "held BUY but the book holds them SELL"),
        (EventKind.ASSIGNMENT, lambda put: notice(put, quantity="2"), "2 contracts settled but 1 are held"),
        (EventKind.EXPIRY, lambda put: notice(put, price="260", early=True), "expires only at its expiry"),
        (EventKind.ASSIGNMENT, lambda put: notice(option("P", "250", root="MSFT")), "no open position"),
    ],
)
def test_the_fold_refuses_a_lifecycle_event_that_contradicts_the_book(book, kind, make, match) -> None:
    put = option("P", "250")
    book.trade(put, Side.SELL, "1", "3.00")
    event = Event(account="OPT_CSP", kind=kind, payload=make(put), ts_utc=AFTER, command_id="bad")
    with pytest.raises(LedgerFoldError, match=match):
        book.ledger.append(event)
    assert book.qty(put) == -1


def test_the_fold_refuses_early_assignment_of_a_european_contract(book) -> None:
    call = option("C", "5000", root="SPXW", expiry=NOVEMBER)
    book.trade(call, Side.SELL, "1", "30.00")
    event = Event(account="OPT_CSP", kind=EventKind.ASSIGNMENT, payload=notice(call, price="5100", early=True),
                  ts_utc=AFTER, command_id="bad")
    with pytest.raises(LedgerFoldError, match="European contract cannot be assigned early"):
        book.ledger.append(event)


# -- look-ahead and misbehaving sources ------------------------------------------------


def test_a_dividend_or_quote_recorded_after_the_clock_refuses(book) -> None:
    call = option("C", "250", expiry=NOVEMBER)
    book.trade(call, Side.SELL, "1", "11.00")
    future = FixedDividends({"AAPL": [Dividend("AAPL", EXPIRY, D("0.26"), "test", THU_AFTER + timedelta(seconds=1))]})
    with pytest.raises(LifecycleError, match="dividend record is from after the clock"):
        settle_thursday(book, call, dividends=future)

    class Later:
        def quote(self, contract, now):
            return OptionQuote(contract, D("10.2"), D("10.4"), D(1), D(1), now + timedelta(seconds=1))
    with pytest.raises(StaleDataError, match="quote is from after the clock"):
        settle_thursday(book, call, quotes=Later())


def test_a_source_answering_for_another_session_refuses(book) -> None:
    book.trade(option("P", "250"), Side.SELL, "1", "3.00")

    class Wrong:
        def settlement(self, underlying, session, settle_time):
            return pm("240", session=CAL.previous_session(EXPIRY), at=CLOSE)
    lifecycle = LifecyclePass(book.ledger, ReplayClock(AFTER), CAL, Wrong())
    with pytest.raises(LifecycleError, match="Asked for the PM settlement of AAPL on 2026-10-16"):
        lifecycle.run(EXPIRY)


def test_a_contract_dated_on_a_holiday_refuses_rather_than_moving(book) -> None:
    thanksgiving = date(2026, 11, 26)
    book.trade(option("P", "250", expiry=thanksgiving), Side.SELL, "1", "3.00")
    friday = date(2026, 11, 27)
    with pytest.raises(LifecycleError, match="which is not a session"):
        book.settle(at=CAL.session_close(friday) + timedelta(hours=1), session=friday)


def test_a_session_that_is_not_one_refuses(book) -> None:
    with pytest.raises(LifecycleError, match="is not a session"):
        book.settle(session=date(2026, 10, 17))


# -- the data adapters -----------------------------------------------------------------


def test_corporate_action_dividends_read_the_ex_date_and_amount() -> None:
    from trade_engine.interfaces.market_data import CorporateAction
    from trade_engine.lifecycle import CorporateActionDividends

    class Provider:
        def __init__(self, actions):
            self.actions = actions

        def corporate_actions(self, symbol, max_age_seconds):
            return self.actions

    paid = CorporateAction("AAPL", "dividend", EXPIRY, OPENED, {"amount": "0.26"})
    split = CorporateAction("AAPL", "split", EXPIRY, OPENED, {"ratio": "4:1"})
    other_day = CorporateAction("AAPL", "dividend", THURSDAY, OPENED, {"amount": "0.26"})
    [found] = CorporateActionDividends(Provider([paid, split, other_day]), 3600).dividends("AAPL", EXPIRY)
    assert (found.ex_date, found.amount) == (EXPIRY, D("0.26"))
    for bad in ({}, {"amount": "n/a"}, {"amount": "0"}):
        broken = CorporateAction("AAPL", "dividend", EXPIRY, OPENED, bad)
        with pytest.raises(StaleDataError, match="no usable amount"):
            CorporateActionDividends(Provider([broken]), 3600).dividends("AAPL", EXPIRY)


def test_snapshot_quotes_read_the_latest_chain_and_refuse_a_missing_contract(tmp_path) -> None:
    from trade_engine.lifecycle import SnapshotQuotes
    from trade_engine.market_data.chains import ChainSnapshot, ChainSnapshotStore

    call = option("C", "250", expiry=NOVEMBER)
    taken = THU_AFTER - timedelta(hours=2)
    quote = OptionQuote(call, D("10.2"), D("10.4"), D(1), D(1), taken)
    store = ChainSnapshotStore(tmp_path)
    store.put(ChainSnapshot("AAPL", taken, D("260"), (quote,), None, None, "test"))
    quotes = SnapshotQuotes(store, max_age_seconds=3 * 3600)
    assert quotes.quote(call, THU_AFTER) == quote
    with pytest.raises(StaleDataError, match="is not in the AAPL chain snapshot"):
        quotes.quote(option("C", "255", expiry=NOVEMBER), THU_AFTER)
    with pytest.raises(StaleDataError, match="old"):
        SnapshotQuotes(store, max_age_seconds=3600).quote(call, THU_AFTER)


def test_a_position_already_settled_refuses_a_second_settlement(book) -> None:
    put = option("P", "250")
    book.trade(put, Side.SELL, "1", "3.00")
    book.settle(pm("240"))
    again = Event(account="OPT_CSP", kind=EventKind.ASSIGNMENT, payload=notice(put), ts_utc=AFTER, command_id="again")
    with pytest.raises(LedgerFoldError, match="no open position"):
        book.ledger.append(again)

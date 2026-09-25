"""O4: options structures through the OMS — targets, closes, and the C3/C4/C5 guards."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from trade_engine.calendar import get_calendar
from trade_engine.clock.replay import ReplayClock
from trade_engine.domain.instruments import Combo, ComboLeg, Equity, OptionContract, OptionRight, Side
from trade_engine.domain.option_orders import CloseHolding, CloseStructure, OptionIntent
from trade_engine.domain.option_roots import SettleTime
from trade_engine.domain.orders import OrderState, OrderType
from trade_engine.domain.portfolio import Fill
from trade_engine.interfaces.market_data import OptionQuote
from trade_engine.ledger import CashFlow, Event, EventKind, Ledger
from trade_engine.lifecycle import FixedSettlements, LifecyclePass, SettlementPrice
from trade_engine.market_data.chains import ChainSnapshot
from trade_engine.oms.manager import IdempotencyConflictError
from trade_engine.oms.options import (
    DuplicateEntryError,
    OptionOrderManager,
    StructureClosedError,
    UncoveredCallError,
    open_structures,
)
from trade_engine.sim import SnapshotVenue

D = Decimal
CAL = get_calendar()
ACCOUNT = "OPT_TEST"
EXPIRY = date(2026, 10, 30)
LEAPS = date(2027, 6, 17)
EARLY = date(2026, 10, 16)
START = datetime(2026, 9, 24, 21, 45, tzinfo=UTC)  # 17:45 ET
SESSIONS = [date(2026, 9, 25), date(2026, 9, 28), date(2026, 9, 29), date(2026, 9, 30)]
P270 = OptionContract("COHR", EXPIRY, D("270"), OptionRight.PUT)
P260 = OptionContract("COHR", EXPIRY, D("260"), OptionRight.PUT)
C330 = OptionContract("COHR", EXPIRY, D("330"), OptionRight.CALL)
C340 = OptionContract("COHR", EXPIRY, D("340"), OptionRight.CALL)
C250_LEAPS = OptionContract("COHR", LEAPS, D("250"), OptionRight.CALL)
C250_EARLY = OptionContract("COHR", EARLY, D("250"), OptionRight.CALL)
COHR = Equity("COHR")
SPREAD = Combo((ComboLeg(P270, 1, Side.SELL), ComboLeg(P260, 1, Side.BUY)))
MIN = datetime.min.replace(tzinfo=UTC)


def snap_at(session: date) -> datetime:
    return CAL.session_close(session) - timedelta(minutes=15)


def chain(at: datetime, **marks: tuple[str, str]) -> ChainSnapshot:
    book = {
        P270: ("10.10", "10.60"), P260: ("6.70", "7.80"), C330: ("4.00", "4.20"), C340: ("2.00", "2.20"),
        C250_LEAPS: ("70.00", "72.00"), C250_EARLY: ("55.00", "56.00"),
    }
    names = {"P270": P270, "P260": P260, "C330": C330, "C340": C340}
    for name, quote in marks.items():
        book[names[name]] = quote
    quotes = tuple(OptionQuote(c, D(b), D(a), D(10), D(10), at) for c, (b, a) in book.items())
    return ChainSnapshot("COHR", at, D("300"), quotes, None, None, "test")


class Desk:
    """One account on a snapshot venue, with the runner's fill bookkeeping."""

    def __init__(self, tmp_path) -> None:
        self.clock = ReplayClock(START)
        self.ledger = Ledger(tmp_path / "ledger.db").open()
        self.ledger.append(Event(account=ACCOUNT, kind=EventKind.CASH_FLOW, ts_utc=START, command_id="deposit",
                                 payload=CashFlow(amount=D("100000"), kind="deposit", as_of=START)))
        self.venue = SnapshotVenue(ACCOUNT, self.clock)
        self.venue.connect()
        self.oms = OptionOrderManager(self.venue, self.clock, self.ledger)
        self.n = 0

    def intent(self, instrument, side: Side, quantity: str = "1", limit: str | None = None, target: str | None = None, name: str | None = None) -> OptionIntent:
        self.n += 1
        name = name or f"i{self.n}"
        return OptionIntent(
            intent_id=name, account_id=ACCOUNT, instrument=instrument, side=side, quantity=D(quantity),
            reason="test", command_id=name, order_type=OrderType.MARKET if limit is None else OrderType.LIMIT,
            limit_price=None if limit is None else D(limit), profit_target=None if target is None else D(target),
        )

    def snapshot(self, session: date, **marks) -> None:
        at = snap_at(session)
        self.clock.advance_to(at)
        self.venue.process_snapshot(chain(at, **marks))
        self.book(at)

    def book(self, since: datetime) -> None:
        for vf in self.venue.fills(since):
            if self.ledger.has_command(f"fill:{vf.venue_fill_id}"):
                continue
            self.oms.orders.record_fill(Fill(
                fill_id=vf.venue_fill_id, order_id=vf.venue_order_id, account_id=ACCOUNT, instrument=vf.instrument,
                quantity=vf.quantity, price=vf.price, venue_env="sim", filled_at=vf.filled_at, side=vf.side,
                fee=vf.fee, venue_order_id=vf.venue_order_id, venue_execution_id=vf.venue_fill_id, leg_id=vf.leg_id,
            ))
        state = self.ledger.state(ACCOUNT)
        for vs in self.venue.orders(since):
            order = state.orders.get(vs.venue_order_id)
            if order is not None and order.state not in (OrderState.NEW, OrderState.FILLED, OrderState.CANCELLED, OrderState.EXPIRED, OrderState.REJECTED):
                self.oms.orders.reconcile_order(vs.venue_order_id)
        self.oms.sync(ACCOUNT, f"sync:{self.clock.now_utc().isoformat()}")

    def after_close(self, session: date) -> None:
        self.clock.advance_to(CAL.session_close(session) + timedelta(minutes=105))
        self.book(MIN)

    @property
    def state(self):
        return self.ledger.state(ACCOUNT)

    def order(self, order_id: str):
        return self.state.orders[order_id]

    def held(self, instrument) -> Decimal:
        position = self.state.positions.get(instrument)
        return D(0) if position is None else position.quantity


@pytest.fixture
def desk(tmp_path):
    d = Desk(tmp_path)
    yield d
    d.ledger.close()


def buy_shares(desk: Desk, quantity: str = "100", session: date = SESSIONS[0]) -> None:
    desk.oms.open(desk.intent(COHR, Side.BUY, quantity))
    desk.snapshot(session)


# -- a structure's life: entry, resting target, close -------------------------------


def test_a_filled_entry_sends_its_resting_profit_target(desk) -> None:
    entry = desk.oms.open(desk.intent(P270, Side.SELL, "2", limit="10.00", target="5.00", name="csp"))
    assert entry.state is OrderState.ACCEPTED
    assert desk.order("csp:entry:target").state is OrderState.NEW  # waits for the fill
    desk.snapshot(SESSIONS[0])
    assert desk.held(P270) == -2
    target = desk.order("csp:entry:target")
    assert target.state is OrderState.ACCEPTED and target.side is Side.BUY and target.limit_price == D("5.00")
    [structure] = open_structures(desk.state)
    assert structure.entry_price == D("10.00") and structure.units == 2 and structure.credit
    assert structure.target_order_id == "csp:entry:target" and structure.closing_order_id is None


def test_the_target_fills_at_a_later_snapshot_and_the_structure_is_gone(desk) -> None:
    desk.oms.open(desk.intent(P270, Side.SELL, limit="10.00", target="5.00", name="csp"))
    desk.snapshot(SESSIONS[0])
    desk.after_close(SESSIONS[0])
    desk.snapshot(SESSIONS[1], P270=("4.60", "4.80"))  # buys back at 4.75 model <= 5.00
    assert desk.held(P270) == 0
    assert desk.order("csp:entry:target").state is OrderState.FILLED
    assert open_structures(desk.state) == ()
    assert desk.state.realized_pnl == D("500") - 2 * D("0.65")


def test_an_entry_that_never_filled_cancels_its_target(desk) -> None:
    desk.oms.open(desk.intent(P270, Side.SELL, limit="11.00", target="5.00", name="csp"))
    desk.snapshot(SESSIONS[0])
    desk.after_close(SESSIONS[0])  # the DAY entry lapsed at the close
    assert desk.order("csp:entry").state is OrderState.EXPIRED
    assert desk.order("csp:entry:target").state is OrderState.CANCELLED


def test_a_spread_enters_for_its_credit_and_closes_as_one_order(desk) -> None:
    desk.oms.open(desk.intent(SPREAD, Side.SELL, "3", limit="2.30", target="1.15", name="bps"))
    desk.snapshot(SESSIONS[0])
    [structure] = open_structures(desk.state)
    assert structure.entry_price == D("2.30")
    assert [(leg.contract, leg.open_quantity) for leg in structure.legs] == [(P270, 3), (P260, 3)]
    desk.after_close(SESSIONS[0])
    close = desk.oms.close(ACCOUNT, CloseStructure("bps:entry", "defensive", "bps:close"))
    assert desk.order("bps:entry:target").state is OrderState.CANCELLED  # cancelled before the close went out
    assert isinstance(close.instrument, Combo) and close.side is Side.BUY
    assert [(leg.contract, leg.side) for leg in close.instrument.legs] == [(P270, Side.BUY), (P260, Side.SELL)]
    desk.snapshot(SESSIONS[1])
    assert desk.held(P270) == 0 and desk.held(P260) == 0
    assert open_structures(desk.state) == ()


# -- C4: duplicate entry impossible ------------------------------------------------


def test_a_replayed_entry_command_opens_nothing_new(desk) -> None:
    intent = desk.intent(P270, Side.SELL, limit="10.00", name="csp")
    first = desk.oms.open(intent)
    count = len(desk.ledger.events())
    assert desk.oms.open(intent) == first
    assert len(desk.ledger.events()) == count


def test_a_command_id_reused_for_other_terms_refuses(desk) -> None:
    desk.oms.open(desk.intent(P270, Side.SELL, limit="10.00", name="csp"))
    with pytest.raises(IdempotencyConflictError):
        desk.oms.open(desk.intent(P270, Side.SELL, limit="9.00", name="csp"))


def test_a_second_entry_on_a_contract_being_entered_refuses(desk) -> None:
    desk.oms.open(desk.intent(P270, Side.SELL, limit="10.00"))
    with pytest.raises(DuplicateEntryError, match="C4"):
        desk.oms.open(desk.intent(SPREAD, Side.SELL, limit="2.00"))


def test_a_second_entry_on_a_held_contract_refuses(desk) -> None:
    desk.oms.open(desk.intent(P270, Side.SELL, limit="10.00"))
    desk.snapshot(SESSIONS[0])
    with pytest.raises(DuplicateEntryError, match="C4"):
        desk.oms.open(desk.intent(P270, Side.SELL, limit="10.00"))


def test_an_entry_on_another_contract_is_not_a_duplicate(desk) -> None:
    desk.oms.open(desk.intent(P270, Side.SELL, limit="10.00"))
    desk.oms.open(desk.intent(P260, Side.SELL, limit="6.00"))


def test_the_same_contract_can_be_entered_again_once_the_first_is_closed(desk) -> None:
    desk.oms.open(desk.intent(P270, Side.SELL, limit="10.00", name="first"))
    desk.snapshot(SESSIONS[0])
    desk.after_close(SESSIONS[0])
    desk.oms.close(ACCOUNT, CloseStructure("first:entry", "done", "first:close"))
    desk.snapshot(SESSIONS[1])
    desk.oms.open(desk.intent(P270, Side.SELL, limit="10.00", name="second"))


# -- C5: a structure closes once ----------------------------------------------------


def test_a_second_close_while_one_works_refuses(desk) -> None:
    desk.oms.open(desk.intent(P270, Side.SELL, limit="10.00", name="csp"))
    desk.snapshot(SESSIONS[0])
    desk.after_close(SESSIONS[0])
    desk.oms.close(ACCOUNT, CloseStructure("csp:entry", "dte", "close-a"))
    with pytest.raises(StructureClosedError, match="C5"):
        desk.oms.close(ACCOUNT, CloseStructure("csp:entry", "defensive", "close-b"))


def test_closing_a_closed_structure_refuses(desk) -> None:
    desk.oms.open(desk.intent(P270, Side.SELL, limit="10.00", name="csp"))
    desk.snapshot(SESSIONS[0])
    desk.after_close(SESSIONS[0])
    desk.oms.close(ACCOUNT, CloseStructure("csp:entry", "dte", "close-a"))
    desk.snapshot(SESSIONS[1])
    desk.after_close(SESSIONS[1])
    with pytest.raises(StructureClosedError, match="nothing open"):
        desk.oms.close(ACCOUNT, CloseStructure("csp:entry", "dte", "close-b"))


def test_a_replayed_close_returns_the_same_order(desk) -> None:
    desk.oms.open(desk.intent(P270, Side.SELL, limit="10.00", name="csp"))
    desk.snapshot(SESSIONS[0])
    desk.after_close(SESSIONS[0])
    first = desk.oms.close(ACCOUNT, CloseStructure("csp:entry", "dte", "close-a"))
    assert desk.oms.close(ACCOUNT, CloseStructure("csp:entry", "dte", "close-a")) == first


def test_a_close_that_failed_can_be_tried_again(desk) -> None:
    desk.oms.open(desk.intent(P270, Side.SELL, limit="10.00", name="csp"))
    desk.snapshot(SESSIONS[0])
    desk.after_close(SESSIONS[0])
    desk.oms.close(ACCOUNT, CloseStructure("csp:entry", "take", "close-a", limit_price=D("1.00")))
    desk.snapshot(SESSIONS[1])
    desk.after_close(SESSIONS[1])  # the limit never traded; the DAY close lapsed
    assert desk.order("csp:entry:close:1").state is OrderState.EXPIRED
    again = desk.oms.close(ACCOUNT, CloseStructure("csp:entry", "take", "close-b"))
    assert again.order_id == "csp:entry:close:2"


# -- C3 / I8: calls only on shares the account holds -------------------------------


def test_a_call_on_shares_the_account_does_not_hold_refuses(desk) -> None:
    with pytest.raises(UncoveredCallError, match="C3"):
        desk.oms.open(desk.intent(C330, Side.SELL, limit="4.00"))


def test_a_call_on_100_held_shares_is_covered(desk) -> None:
    buy_shares(desk)
    desk.oms.open(desk.intent(C330, Side.SELL, limit="4.00"))


def test_a_second_call_on_the_same_100_shares_refuses(desk) -> None:
    buy_shares(desk)
    desk.oms.open(desk.intent(C330, Side.SELL, limit="4.00"))
    with pytest.raises(UncoveredCallError, match="C3"):
        desk.oms.open(desk.intent(C340, Side.SELL, limit="2.00"))


def test_two_calls_need_two_hundred_shares(desk) -> None:
    buy_shares(desk, "150")
    with pytest.raises(UncoveredCallError):
        desk.oms.open(desk.intent(C330, Side.SELL, "2", limit="4.00"))


def test_a_later_long_call_covers_a_short_call(desk) -> None:
    desk.oms.open(desk.intent(C250_LEAPS, Side.BUY, limit="72.00"))
    desk.snapshot(SESSIONS[0])
    [leaps] = open_structures(desk.state)
    assert leaps.entry_price == D("72.00") and not leaps.credit  # a debit paid, as a positive price
    desk.oms.open(desk.intent(C330, Side.SELL, limit="4.00"))  # the PMCC


def test_a_long_call_expiring_first_covers_nothing(desk) -> None:
    desk.oms.open(desk.intent(C250_EARLY, Side.BUY, limit="56.00"))
    desk.snapshot(SESSIONS[0])
    with pytest.raises(UncoveredCallError):
        desk.oms.open(desk.intent(C330, Side.SELL, limit="4.00"))


def test_a_diagonal_entered_as_one_combo_covers_itself(desk) -> None:
    diagonal = Combo((ComboLeg(C250_LEAPS, 1, Side.BUY), ComboLeg(C330, 1, Side.SELL)))
    desk.oms.open(desk.intent(diagonal, Side.BUY, limit="70.00"))


def test_shares_a_working_order_is_selling_cover_nothing(desk) -> None:
    buy_shares(desk)
    desk.after_close(SESSIONS[0])
    desk.oms.close_holding(ACCOUNT, CloseHolding(COHR, D(100), "exit", "sell-shares"))
    with pytest.raises(UncoveredCallError):
        desk.oms.open(desk.intent(C330, Side.SELL, limit="4.00"))


def test_shares_behind_a_short_call_cannot_be_sold_on_their_own(desk) -> None:
    buy_shares(desk)
    desk.after_close(SESSIONS[0])
    desk.oms.open(desk.intent(C330, Side.SELL, limit="4.00", name="cc"))
    desk.snapshot(SESSIONS[1])
    desk.after_close(SESSIONS[1])
    with pytest.raises(UncoveredCallError, match="close the calls first"):
        desk.oms.close_holding(ACCOUNT, CloseHolding(COHR, D(100), "exit", "sell-shares"))


def test_shares_can_be_sold_with_the_call_being_closed_beside_them(desk) -> None:
    buy_shares(desk)
    desk.after_close(SESSIONS[0])
    desk.oms.open(desk.intent(C330, Side.SELL, limit="4.00", name="cc"))
    desk.snapshot(SESSIONS[1])
    desk.after_close(SESSIONS[1])
    desk.oms.close(ACCOUNT, CloseStructure("cc:entry", "close both", "cc:close"))
    desk.oms.close_holding(ACCOUNT, CloseHolding(COHR, D(100), "close both", "sell-shares"))
    desk.snapshot(SESSIONS[2])
    assert desk.held(COHR) == 0 and desk.held(C330) == 0


def test_shares_above_what_the_calls_need_can_be_sold(desk) -> None:
    buy_shares(desk, "150")
    desk.after_close(SESSIONS[0])
    desk.oms.open(desk.intent(C330, Side.SELL, limit="4.00"))
    desk.snapshot(SESSIONS[1])
    desk.after_close(SESSIONS[1])
    desk.oms.close_holding(ACCOUNT, CloseHolding(COHR, D(50), "odd lot", "sell-50"))


def test_closing_more_shares_than_held_refuses(desk) -> None:
    buy_shares(desk)
    desk.after_close(SESSIONS[0])
    with pytest.raises(Exception, match="I8"):
        desk.oms.close_holding(ACCOUNT, CloseHolding(COHR, D(200), "exit", "sell-200"))


# -- settlement: an expired or assigned structure stops its exits (O2) ---------------


def test_an_assigned_put_cancels_its_resting_target_and_leaves_shares(desk, tmp_path) -> None:
    desk.oms.open(desk.intent(P270, Side.SELL, limit="10.00", target="5.00", name="csp"))
    desk.snapshot(SESSIONS[0])
    close = CAL.session_close(EXPIRY)
    desk.clock.advance_to(close + timedelta(minutes=105))
    price = SettlementPrice("COHR", EXPIRY, SettleTime.PM, D("250"), "official close", close + timedelta(minutes=70))
    LifecyclePass(desk.ledger, desk.clock, CAL, FixedSettlements([price])).run(EXPIRY, [ACCOUNT])
    desk.book(MIN)
    assert desk.held(P270) == 0 and desk.held(COHR) == 100
    assert desk.order("csp:entry:target").state is OrderState.CANCELLED
    assert open_structures(desk.state) == ()


# -- what an intent may ask for -------------------------------------------------------


def _intent(**changes) -> OptionIntent:
    values = dict(intent_id="i", account_id=ACCOUNT, instrument=P270, side=Side.SELL, quantity=D(1), reason="r",
                  command_id="c", order_type=OrderType.LIMIT, limit_price=D("10.00"))
    values.update(changes)
    return OptionIntent(**values)


def test_a_credit_target_must_buy_back_for_less_than_the_credit() -> None:
    _intent(profit_target=D("5.00"))
    with pytest.raises(ValueError, match="below the credit"):
        _intent(profit_target=D("10.00"))


def test_a_debit_target_must_sell_for_more_than_the_debit() -> None:
    _intent(side=Side.BUY, profit_target=D("15.00"))
    with pytest.raises(ValueError, match="above the debit"):
        _intent(side=Side.BUY, profit_target=D("9.00"))


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        (dict(quantity=D("1.5")), "whole positive"),
        (dict(limit_price=None), "needs a limit_price"),
        (dict(order_type=OrderType.MARKET), "cannot carry a limit_price"),
        (dict(order_type=OrderType.STOP), "MARKET or LIMIT"),
        (dict(instrument=COHR, profit_target=D("5.00")), "Only an options structure"),
        (dict(instrument=Combo((ComboLeg(P270, 1, Side.SELL), ComboLeg(COHR, 100, Side.BUY)))), "options only"),
    ],
)
def test_an_intent_that_cannot_be_ordered_refuses(changes, message) -> None:
    with pytest.raises(ValueError, match=message):
        _intent(**changes)

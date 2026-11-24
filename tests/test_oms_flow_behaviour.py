"""P0a-T2: OMS flow behaviour, each expectation written out as a literal.

Replaces the flow parity oracles (test_p3a_parity, test_p3b_parity, test_p3b2_parity,
test_p3b2b_flow). Expected values come from the rules and the arithmetic, not from a frozen
oracle.
"""

from __future__ import annotations

import decimal
from datetime import date
from decimal import Decimal

import pytest
import trade_engine_rs

import trade_engine.ledger._rs as _rs
from trade_engine.domain.instruments import Equity, OptionContract, OptionRight, Side
from trade_engine.domain.orders import Order, OrderType
from trade_engine.ledger import EventKind, Ledger, fold
from trade_engine.oms.manager import OrderManager, _Host
from trade_engine.oms.reconcile import MIN_TIME, reconcile_after
from trade_engine.oms.restore import restorable
from trade_engine.oms.trailing import TrailingStopEmulator

from test_option_oms import ACCOUNT, COHR, SESSIONS, Desk
from test_option_risk import NOW, Book
from test_oms import FakeBroker

D = Decimal
EQUITY = Equity("XYZ")
OPTION = OptionContract("XYZ", date(2026, 10, 30), D("45"), OptionRight.PUT)


# -- trailing stop (oms/trailing.py) -------------------------------------------------------


def test_a_sell_trail_follows_the_high_and_triggers_once_the_price_reaches_the_stop() -> None:
    trail = TrailingStopEmulator(Side.SELL, D("1"))
    assert trail.update(D("10")) is False
    assert (trail.extreme, trail.stop_price, trail.triggered) == (D("10"), D("9"), False)
    assert trail.update(D("9.5")) is False
    assert (trail.extreme, trail.stop_price, trail.triggered) == (D("10"), D("9"), False)
    assert trail.update(D("9")) is True
    assert (trail.extreme, trail.stop_price, trail.triggered) == (D("10"), D("9"), True)


def test_a_buy_trail_follows_the_low_and_triggers_once_the_price_reaches_the_stop() -> None:
    trail = TrailingStopEmulator(Side.BUY, D("1"))
    assert trail.update(D("10")) is False
    assert (trail.extreme, trail.stop_price, trail.triggered) == (D("10"), D("11"), False)
    assert trail.update(D("11")) is True
    assert trail.triggered is True


def test_a_price_that_is_not_a_decimal_is_refused_and_leaves_no_state() -> None:
    trail = TrailingStopEmulator(Side.SELL, D("1"))
    with pytest.raises(ValueError, match='not a Decimal: "NaN"'):
        trail.update(D("NaN"))
    assert trail.extreme is None and trail.stop_price is None


def test_a_zero_price_is_refused_with_the_finite_positive_message() -> None:
    trail = TrailingStopEmulator(Side.SELL, D("1"))
    with pytest.raises(ValueError, match="price must be finite and positive, got 0"):
        trail.update(D("0"))


def test_a_zero_trail_amount_is_refused_at_construction() -> None:
    with pytest.raises(ValueError, match="trail_amount must be positive"):
        TrailingStopEmulator(Side.SELL, D("0"))


# -- the Rust refusal glue (ledger/_rs.py: call and refusal) -------------------------------


def _refuse(kind: str, message: str):
    def boom():
        raise ValueError(kind, message)

    return boom


@pytest.mark.parametrize(
    ("kind", "signal"),
    [("invalid_operation", decimal.InvalidOperation), ("division_by_zero", decimal.DivisionByZero), ("overflow", decimal.Overflow)],
)
def test_a_decimal_refusal_from_rust_raises_the_decimal_signal(kind, signal) -> None:
    with pytest.raises(signal):
        _rs.call(_refuse(kind, "bad"))


@pytest.mark.parametrize("message", ["'v'", "v"])
def test_a_key_refusal_from_rust_raises_key_error_with_the_bare_key(message) -> None:
    with pytest.raises(KeyError) as err:
        _rs.call(_refuse("key", message))
    assert err.value.args == ("v",)


# -- manager quantity helpers (oms/manager.py: dead in src, pinned by value here) ----------


def test_split_gives_whole_equity_shares_with_the_larger_lots_first() -> None:
    assert OrderManager._split_quantity(D("10"), 3, EQUITY) == (D("4"), D("3"), D("3"))
    assert OrderManager._split_quantity(D("10"), 4, EQUITY) == (D("3"), D("3"), D("2"), D("2"))


def test_split_of_fractional_equity_shares_is_refused() -> None:
    with pytest.raises(ValueError, match="whole number of shares, got 10.5"):
        OrderManager._split_quantity(D("10.5"), 2, EQUITY)


def test_split_of_an_option_may_be_fractional() -> None:
    assert OrderManager._split_quantity(D("3"), 2, OPTION) == (D("1.5"), D("1.5"))


def test_split_into_no_lots_is_empty() -> None:
    assert OrderManager._split_quantity(D("10"), 0, EQUITY) == ()


def test_fractions_of_equity_shares_are_taken_whole() -> None:
    assert OrderManager._fraction_quantities(D("10"), (D("0.5"), D("0.5")), EQUITY) == (D("5"), D("5"))
    assert OrderManager._fraction_quantities(D("10"), (D("0.3"), D("0.7")), EQUITY) == (D("3"), D("7"))


def test_fractions_of_an_option_stay_fractional() -> None:
    assert OrderManager._fraction_quantities(D("3"), (D("0.5"), D("0.5")), OPTION) == (D("1.5"), D("1.5"))


def test_allocation_rounds_by_largest_remainder_and_ties_go_to_the_earlier_weight() -> None:
    # 10 over weights 1:1:2 is 2.5, 2.5, 5; the two halves tie and the first takes the extra share.
    assert OrderManager._allocate_quantity(D("10"), (D("1"), D("1"), D("2")), EQUITY) == (D("3"), D("2"), D("5"))
    # 10 over 1:1:1 is 3.33 each; the leftover share goes to the first weight.
    assert OrderManager._allocate_quantity(D("10"), (D("1"), D("1"), D("1")), EQUITY) == (D("4"), D("3"), D("3"))


def test_allocation_over_a_zero_weight_is_refused() -> None:
    with pytest.raises(ValueError, match="target allocation weights must be positive"):
        OrderManager._allocate_quantity(D("10"), (D("0"),), EQUITY)


def test_allocation_of_an_option_may_be_fractional() -> None:
    assert OrderManager._allocate_quantity(D("1"), (D("1"), D("1")), OPTION) == (D("0.5"), D("0.5"))


def _order(quantity: str) -> Order:
    return Order(
        order_id="o1", account_id=ACCOUNT, instrument=EQUITY, order_type=OrderType.MARKET,
        side=Side.BUY, quantity=D(quantity), command_id="c1", created_at=NOW,
    )


def test_an_order_fingerprint_is_stable_and_changes_with_its_terms() -> None:
    assert OrderManager._fingerprint_order(_order("10")) == OrderManager._fingerprint_order(_order("10"))
    assert OrderManager._fingerprint_order(_order("10")) != OrderManager._fingerprint_order(_order("11"))


# -- restore refuses a pending mode that is not text (oms/restore.py) -----------------------


def test_restore_refuses_a_pending_mode_that_is_not_text(tmp_path) -> None:
    ledger = Ledger(tmp_path / "ledger.db").open()
    try:
        state = fold(Book().events)[ACCOUNT]
        with pytest.raises(ValueError, match="pending must be 'refuse' or 'resolve', got None"):
            restorable(ledger, ACCOUNT, state, pending=None)
    finally:
        ledger.close()


# -- replaying an older recorded fill with a journal account (oms/reconcile.py) -----------


def test_a_replayed_older_fill_is_healed_without_journaling_it_again(tmp_path) -> None:
    desk = Desk(tmp_path)
    try:
        desk.oms.open(desk.intent(COHR, Side.BUY, "100"))
        desk.oms.open(desk.intent(COHR, Side.BUY, "50"))
        desk.snapshot(SESSIONS[0])
        fills = desk.ledger.events_of_kind(EventKind.FILL, account=ACCOUNT)
        assert [f.command_id for f in fills] == ["fill:i1:entry:fill:1", "fill:i2:entry:fill:1"]
        older, latest = fills

        first = reconcile_after(desk.ledger, desk.clock, desk.venue, desk.oms.orders, ACCOUNT, MIN_TIME, journal_account="JRN")
        assert first == 0
        assert desk.ledger.outbox_row(older.seq, "journal:JRN") is None
        assert desk.ledger.outbox_row(latest.seq, "journal:JRN") is not None

        count = desk.ledger.count()
        again = reconcile_after(desk.ledger, desk.clock, desk.venue, desk.oms.orders, ACCOUNT, MIN_TIME, journal_account="JRN")
        assert again == 0
        assert desk.ledger.count() == count
    finally:
        desk.ledger.close()


# -- keepers from the parity files: explicit literals, no oracle ---------------------------


@pytest.mark.parametrize("name", ["oms_native_restorable", "oms_sync_plan", "oms_plan_close", "oms_native_journal_payload"])
def test_the_rust_flow_entry_points_are_imported_not_skipped(name) -> None:
    assert getattr(trade_engine_rs, name)


def test_flow_refuses_unknown_operation_and_bad_request() -> None:
    """The door itself: an unknown operation and a malformed request are refusals."""
    host = _Host(None, FakeBroker(), None, None)
    for op, request in (("no_such_operation", "{}"), ("get_order", "{}"), ("get_order", "not json")):
        with pytest.raises(ValueError) as err:
            trade_engine_rs.oms_flow(op, host, request)
        assert len(err.value.args) == 2 and err.value.args[0] in ("value", "json", "unsupported"), err.value.args


@pytest.mark.parametrize("native", [False, True])
def test_native_stop_flag_is_reported_independently_of_the_advertised_types(native) -> None:
    broker = FakeBroker(order_types=frozenset(OrderType), native_stops=native)
    types, tifs, native_flag = _Host(None, broker, None, None).capabilities()
    assert set(types) == {t.value for t in OrderType}
    assert native_flag is native
    assert set(tifs) == {t.value for t in broker.capabilities.supported_tifs}

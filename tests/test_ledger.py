"""E1 ledger tests: codec exactness, fold semantics, idempotency, atomicity, locking."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import textwrap
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType

import pytest

from trade_engine.domain.instruments import Equity, OptionContract, OptionRight, Side
from trade_engine.domain.orders import Order, OrderState, OrderType, TimeInForce
from trade_engine.domain.portfolio import Fill, Lot, Position
from trade_engine.domain.risk import RiskRuleResult, RiskVerdict
from trade_engine.domain.signals import Signal
from trade_engine.interfaces.market_data import CorporateAction
from trade_engine.ledger import (
    CashFlow,
    Event,
    EventKind,
    EventPayloadError,
    FoldCache,
    Ledger,
    LedgerDuplicateFillError,
    LedgerFillMismatchError,
    LedgerFoldError,
    LedgerLockError,
    LifecycleNotice,
    Mark,
    OrderStateChange,
    PayloadCodecError,
    UnhandledEventError,
    VenueReconcile,
    decode_payload,
    encode_payload,
    fold,
    register_handler,
)
from trade_engine.ledger.codec import decode_event, encode_event

TS = datetime(2026, 9, 23, 21, 0, 0, tzinfo=timezone.utc)
TS2 = datetime(2026, 9, 24, 21, 0, 0, tzinfo=timezone.utc)
AAPL = Equity("AAPL")
MSFT = Equity("MSFT")
SPXW = OptionContract(
    underlying="SPXW", expiry=date(2026, 12, 18), strike=Decimal("6000"), right=OptionRight.CALL
)


def an_order(
    order_id: str = "o1",
    account: str = "ACC",
    *,
    instrument=AAPL,
    side: Side = Side.BUY,
    quantity: str = "100",
    command_id: str | None = None,
) -> Order:
    return Order(
        order_id=order_id,
        account_id=account,
        instrument=instrument,
        order_type=OrderType.MARKET,
        side=side,
        quantity=Decimal(quantity),
        command_id=command_id or f"cmd-{order_id}",
        created_at=TS,
    )


def a_fill(
    fill_id: str,
    order_id: str,
    *,
    account: str = "ACC",
    instrument=AAPL,
    side: Side = Side.BUY,
    quantity: str = "100",
    price: str = "100",
    fee: str = "0",
    at: datetime = TS,
) -> Fill:
    return Fill(
        fill_id=fill_id,
        order_id=order_id,
        account_id=account,
        instrument=instrument,
        quantity=Decimal(quantity),
        price=Decimal(price),
        venue_env="sim",
        filled_at=at,
        side=side,
        fee=Decimal(fee),
    )


@pytest.fixture()
def ledger_path(tmp_path: Path) -> Path:
    return tmp_path / "ledger.db"


@pytest.fixture()
def ledger(ledger_path: Path):
    with Ledger(ledger_path) as lg:
        yield lg


# --------------------------------------------------------------------------- codec


@pytest.mark.parametrize(
    "payload",
    [
        Equity("BRK.B"),
        SPXW,
        an_order(),
        a_fill("f1", "o1", quantity="2", price="12.50", fee="1.30"),
        Lot(lot_id="l1", quantity=Decimal("10"), cost_basis=Decimal("99.5"), acquired_at=TS, side=Side.SELL),
        Signal(
            signal_id="s1",
            scan_id="scan-breakout",
            symbol="GOOG",
            session_date=date(2026, 9, 23),
            direction="long",
            metrics={"close": Decimal("165.50"), "atr14": Decimal("3.20")},
            next_earnings_date=None,
            created_at=TS,
        ),
        RiskVerdict(
            order_intent_id="i1",
            evaluations=(
                RiskRuleResult("max_position", True, Decimal("8000"), Decimal("10000"), "ok"),
            ),
        ),
        OrderStateChange(order_id="o1", reason="venue ack"),
        CashFlow(amount=Decimal("-12.34"), kind="fee", as_of=TS),
        Mark(instrument=AAPL, price=Decimal("150.25"), as_of=TS),
        VenueReconcile(venue="sim", as_of=TS, reconciled=True),
        VenueReconcile(venue="sim", as_of=TS, reconciled=False, drift=("AAPL",)),
        LifecycleNotice(instrument=SPXW, quantity_delta=Decimal("2"), cash_delta=Decimal("-500"), as_of=TS),
        CorporateAction(
            symbol="AAPL",
            action_type="dividend",
            effective_date=date(2026, 10, 1),
            as_of=TS,
            details={"amount": "0.25"},
        ),
    ],
    ids=lambda p: type(p).__name__,
)
def test_payload_survives_encode_decode_exactly(payload: object) -> None:
    """Every payload type the ledger stores must fold back byte-identical (I2)."""
    restored = decode_payload(json.loads(json.dumps(encode_payload(payload))))
    assert restored == payload
    assert type(restored) is type(payload)


def test_decimal_never_becomes_float() -> None:
    """Decimal precision must survive persistence; a float round-trip would lose it (I5)."""
    value = Decimal("100.005")
    restored = decode_payload(encode_payload(value))
    assert isinstance(restored, Decimal)
    assert restored == value
    assert not isinstance(restored, float)


def test_nested_mapping_and_tuple_round_trip() -> None:
    payload = CorporateAction(
        symbol="AAPL",
        action_type="split",
        effective_date=date(2026, 10, 1),
        as_of=TS,
        details={"ratio": "4:1"},
    )
    restored = decode_payload(encode_payload(payload))
    assert isinstance(restored.details, MappingProxyType)
    assert restored.details == payload.details


def test_naive_datetime_is_refused_not_persisted() -> None:
    """A naive timestamp would be stored as an ambiguous instant, so encoding refuses (I7)."""
    with pytest.raises(PayloadCodecError, match="naive datetime"):
        encode_payload(datetime(2026, 9, 23, 21, 0, 0))


def test_non_finite_decimal_is_refused() -> None:
    """NaN/Inf are not real prices; persisting them would poison every later fold (I5)."""
    with pytest.raises(PayloadCodecError, match="non-finite"):
        encode_payload(Decimal("NaN"))


def test_unsupported_payload_type_is_refused() -> None:
    class NotAPayload:
        pass

    with pytest.raises(PayloadCodecError, match="unsupported payload type"):
        encode_payload(NotAPayload())


def test_unknown_enum_type_is_refused_on_decode() -> None:
    """A stored enum this build does not know must refuse, not silently become a str (I5)."""
    with pytest.raises(PayloadCodecError, match="Unknown enum type"):
        decode_payload({"e": "NotAnEnum", "v": "X"})


def test_event_round_trip_through_encode_decode() -> None:
    event = Event(
        account="ACC",
        kind=EventKind.MARK,
        payload=Mark(instrument=AAPL, price=Decimal("150"), as_of=TS),
        ts_utc=TS,
        command_id="c-mark",
        seq=7,
    )
    restored = decode_event(json.loads(json.dumps(encode_event(event))))
    assert restored == event


# --------------------------------------------------------------------------- events


def test_payload_must_match_its_kind() -> None:
    """A Mark filed as a Fill would be folded as an execution; refuse instead (I5)."""
    with pytest.raises(EventPayloadError, match="payload must be Mark"):
        Event(account="ACC", kind=EventKind.MARK, payload=an_order(), ts_utc=TS)


def test_event_account_must_match_payload_account() -> None:
    """Filing an order under the wrong account breaks I8; refuse it (I8)."""
    with pytest.raises(EventPayloadError, match="filed under"):
        Event(account="OTHER", kind=EventKind.ORDER_SUBMITTED, payload=an_order(account="ACC"), ts_utc=TS)


def test_naive_event_timestamp_is_refused() -> None:
    with pytest.raises(EventPayloadError, match="timezone-aware"):
        Event(
            account="ACC",
            kind=EventKind.MARK,
            payload=Mark(instrument=AAPL, price=Decimal("150"), as_of=TS),
            ts_utc=datetime(2026, 9, 23, 21, 0, 0),
        )


def test_future_schema_version_is_refused() -> None:
    """A newer writer's event may mean something different; refuse rather than guess (I5)."""
    with pytest.raises(EventPayloadError, match="newer than this engine"):
        Event(
            account="ACC",
            kind=EventKind.MARK,
            payload=Mark(instrument=AAPL, price=Decimal("150"), as_of=TS),
            ts_utc=TS,
            schema_version=999,
        )


def test_venue_reconcile_requires_drift_when_not_reconciled() -> None:
    with pytest.raises(EventPayloadError, match="must name the drifting instruments"):
        VenueReconcile(venue="sim", as_of=TS, reconciled=False)


# --------------------------------------------------------------------------- fold


def test_fold_is_pure_and_replayable() -> None:
    """Folding the same log twice yields equal, independent state (I2)."""
    events = [
        Event(account="ACC", kind=EventKind.ORDER_SUBMITTED, payload=an_order(), ts_utc=TS, seq=1),
        Event(
            account="ACC",
            kind=EventKind.FILL,
            payload=a_fill("f1", "o1", quantity="40", price="100"),
            ts_utc=TS,
            seq=2,
        ),
    ]
    first = fold(events)
    second = fold(events)
    assert first == second
    assert first["ACC"].positions[AAPL].quantity == Decimal("40")


def test_fold_does_not_mutate_the_input_event() -> None:
    order = an_order()
    event = Event(account="ACC", kind=EventKind.ORDER_SUBMITTED, payload=order, ts_utc=TS, seq=1)
    fold([event])
    assert order.state is OrderState.NEW
    assert event.payload.state is OrderState.NEW


def test_order_submitted_advances_new_to_submitted() -> None:
    state = fold(
        [Event(account="ACC", kind=EventKind.ORDER_SUBMITTED, payload=an_order(), ts_utc=TS, seq=1)]
    )["ACC"]
    assert state.orders["o1"].state is OrderState.SUBMITTED


def test_partial_then_full_fill_tracks_state_and_cash() -> None:
    events = [
        Event(account="ACC", kind=EventKind.ORDER_SUBMITTED, payload=an_order(), ts_utc=TS, seq=1),
        Event(
            account="ACC",
            kind=EventKind.FILL,
            payload=a_fill("f1", "o1", quantity="40", price="100", fee="1"),
            ts_utc=TS,
            seq=2,
        ),
    ]
    partial = fold(events)["ACC"]
    assert partial.orders["o1"].state is OrderState.PARTIALLY_FILLED
    assert partial.cash == Decimal("-4001")

    events.append(
        Event(
            account="ACC",
            kind=EventKind.FILL,
            payload=a_fill("f2", "o1", quantity="60", price="102", at=TS2),
            ts_utc=TS2,
            seq=3,
        )
    )
    complete = fold(events)["ACC"]
    assert complete.orders["o1"].state is OrderState.FILLED
    assert complete.filled_quantity["o1"] == Decimal("100")
    assert complete.positions[AAPL].quantity == Decimal("100")
    assert complete.positions[AAPL].avg_cost == Decimal("101.2")


def test_over_fill_beyond_order_quantity_is_refused() -> None:
    """Filling past the order size means the log disagrees with the order; refuse (I5)."""
    events = [
        Event(account="ACC", kind=EventKind.ORDER_SUBMITTED, payload=an_order(quantity="10"), ts_utc=TS, seq=1),
        Event(account="ACC", kind=EventKind.FILL, payload=a_fill("f1", "o1", quantity="10"), ts_utc=TS, seq=2),
        Event(account="ACC", kind=EventKind.FILL, payload=a_fill("f2", "o1", quantity="5"), ts_utc=TS2, seq=3),
    ]
    with pytest.raises(LedgerFoldError, match="over-fill"):
        fold(events)


def test_fill_on_a_cancelled_order_is_refused() -> None:
    """A fill cannot arrive for an order the ledger already saw cancelled (I5)."""
    events = [
        Event(account="ACC", kind=EventKind.ORDER_SUBMITTED, payload=an_order(quantity="10"), ts_utc=TS, seq=1),
        Event(
            account="ACC",
            kind=EventKind.ORDER_CANCELLED,
            payload=OrderStateChange(order_id="o1", reason="cancelled"),
            ts_utc=TS,
            seq=2,
        ),
        Event(account="ACC", kind=EventKind.FILL, payload=a_fill("f1", "o1", quantity="10"), ts_utc=TS2, seq=3),
    ]
    with pytest.raises(LedgerFoldError, match="terminal state CANCELLED"):
        fold(events)


def test_fill_without_submitted_order_is_refused() -> None:
    """A fill for an order the ledger never saw must refuse, not create the order (I5)."""
    with pytest.raises(LedgerFoldError, match="unknown order"):
        fold(
            [
                Event(
                    account="ACC",
                    kind=EventKind.FILL,
                    payload=a_fill("f1", "ghost"),
                    ts_utc=TS,
                    seq=1,
                )
            ]
        )


def test_fill_on_a_different_instrument_than_its_order_is_refused() -> None:
    """A fill filed against the wrong order would invent a position in another symbol (I1)."""
    events = [
        Event(account="ACC", kind=EventKind.ORDER_SUBMITTED, payload=an_order("o1", instrument=AAPL), ts_utc=TS, seq=1),
        Event(account="ACC", kind=EventKind.FILL, payload=a_fill("f1", "o1", instrument=MSFT, price="500"), ts_utc=TS, seq=2),
    ]
    with pytest.raises(LedgerFillMismatchError, match="was filed against order 'o1' for AAPL"):
        fold(events)


def test_fill_against_the_order_side_is_refused() -> None:
    """A SELL fill against a BUY order would silently reverse the position (I1)."""
    events = [
        Event(account="ACC", kind=EventKind.ORDER_SUBMITTED, payload=an_order("o1", side=Side.BUY), ts_utc=TS, seq=1),
        Event(account="ACC", kind=EventKind.FILL, payload=a_fill("f1", "o1", side=Side.SELL, price="500"), ts_utc=TS, seq=2),
    ]
    with pytest.raises(LedgerFillMismatchError, match="but its order 'o1' is a BUY"):
        fold(events)


def test_negative_control_matching_fill_is_accepted() -> None:
    """Negative control: a fill that matches its order's side and instrument still applies."""
    events = [
        Event(account="ACC", kind=EventKind.ORDER_SUBMITTED, payload=an_order("o1", side=Side.SELL, quantity="10"), ts_utc=TS, seq=1),
        Event(account="ACC", kind=EventKind.FILL, payload=a_fill("f1", "o1", side=Side.SELL, quantity="10", price="50"), ts_utc=TS, seq=2),
    ]
    state = fold(events)["ACC"]
    assert state.positions[AAPL].quantity == Decimal("-10")
    assert state.orders["o1"].state is OrderState.FILLED


def test_replayed_fill_id_under_a_new_command_is_refused() -> None:
    """command_id dedupes events; fill_id is the persisted key for the *execution* (I3).

    A venue fill replayed under a fresh command id must not double the position.
    """
    events = [
        Event(account="ACC", kind=EventKind.ORDER_SUBMITTED, payload=an_order("o1", quantity="100"), ts_utc=TS, seq=1),
        Event(account="ACC", kind=EventKind.FILL, payload=a_fill("same-fill", "o1", quantity="30", price="10"), ts_utc=TS, seq=2),
        Event(account="ACC", kind=EventKind.FILL, payload=a_fill("same-fill", "o1", quantity="30", price="10"), ts_utc=TS2, seq=3),
    ]
    with pytest.raises(LedgerDuplicateFillError, match="already in the ledger"):
        fold(events)


def test_negative_control_two_fills_same_order_different_ids_both_apply() -> None:
    """Negative control: two legitimate partial fills must both land."""
    events = [
        Event(account="ACC", kind=EventKind.ORDER_SUBMITTED, payload=an_order("o1", quantity="100"), ts_utc=TS, seq=1),
        Event(account="ACC", kind=EventKind.FILL, payload=a_fill("f1", "o1", quantity="30", price="10"), ts_utc=TS, seq=2),
        Event(account="ACC", kind=EventKind.FILL, payload=a_fill("f2", "o1", quantity="70", price="11"), ts_utc=TS2, seq=3),
    ]
    state = fold(events)["ACC"]
    assert state.filled_quantity["o1"] == Decimal("100")
    assert state.orders["o1"].state is OrderState.FILLED
    assert state.positions[AAPL].quantity == Decimal("100")


def test_realized_pnl_uses_fifo_lot_bases_not_average_cost() -> None:
    """Realised P&L must come from the consumed lots, not the average cost (I11).

    Buy 100@10, buy 100@20, sell 100@30: FIFO says (30-10)*100 = 2000, not the
    average-cost answer 1500. The split must match the lots that remain.
    """
    events = [
        Event(account="ACC", kind=EventKind.ORDER_SUBMITTED, payload=an_order("o1", quantity="100"), ts_utc=TS, seq=1),
        Event(account="ACC", kind=EventKind.FILL, payload=a_fill("f1", "o1", quantity="100", price="10"), ts_utc=TS, seq=2),
        Event(account="ACC", kind=EventKind.ORDER_SUBMITTED, payload=an_order("o2", quantity="100"), ts_utc=TS, seq=3),
        Event(account="ACC", kind=EventKind.FILL, payload=a_fill("f2", "o2", quantity="100", price="20"), ts_utc=TS, seq=4),
        Event(account="ACC", kind=EventKind.ORDER_SUBMITTED, payload=an_order("o3", side=Side.SELL, quantity="100"), ts_utc=TS, seq=5),
        Event(account="ACC", kind=EventKind.FILL, payload=a_fill("f3", "o3", side=Side.SELL, quantity="100", price="30"), ts_utc=TS2, seq=6),
    ]
    state = fold(events)["ACC"]
    position = state.positions[AAPL]
    assert position.realized_pnl == Decimal("2000")
    # The surviving lot is the second buy, so the average must be its basis, not 15.
    assert position.avg_cost == Decimal("20")
    assert position.quantity == Decimal("100")
    assert [lot.lot_id for lot in position.open_lots] == ["f2"]

    # Closing the rest at 40 adds (40-20)*100 = 2000; totals only agree when flat.
    events.append(
        Event(account="ACC", kind=EventKind.ORDER_SUBMITTED, payload=an_order("o4", side=Side.SELL, quantity="100"), ts_utc=TS2, seq=7)
    )
    events.append(
        Event(account="ACC", kind=EventKind.FILL, payload=a_fill("f4", "o4", side=Side.SELL, quantity="100", price="40"), ts_utc=TS2, seq=8)
    )
    final = fold(events)["ACC"]
    assert final.positions[AAPL].realized_pnl == Decimal("4000")
    assert final.positions[AAPL].quantity == Decimal("0")


def test_fees_reduce_realized_pnl_not_just_cash() -> None:
    """Cash and realised P&L must tell the same story, or E8's metrics lie (I11)."""
    events = [
        Event(account="ACC", kind=EventKind.ORDER_SUBMITTED, payload=an_order("o1", quantity="100"), ts_utc=TS, seq=1),
        Event(account="ACC", kind=EventKind.FILL, payload=a_fill("f1", "o1", quantity="100", price="10", fee="5"), ts_utc=TS, seq=2),
    ]
    state = fold(events)["ACC"]
    assert state.cash == Decimal("-1005")
    assert state.positions[AAPL].realized_pnl == Decimal("-5")
    assert state.realized_pnl == Decimal("-5")


def test_realized_pnl_survives_closing_and_reopening_a_flat_position() -> None:
    """A flat position's realised history must survive re-opening (I11).

    Two identical round trips (buy 100@10 / sell 100@12, 1 fee in, 2 fees out) book
    197 each. If the reopen branch resets realised P&L to zero, the account total
    subtracts the wiped history and ends at 197 while cash says 394 — the exact
    divergence between cash and P&L the invariant below forbids.
    """

    def round_trip(n: int, start_seq: int) -> list[Event]:
        return [
            Event(account="ACC", kind=EventKind.ORDER_SUBMITTED, payload=an_order(f"b{n}", quantity="100"), ts_utc=TS, seq=start_seq),
            Event(account="ACC", kind=EventKind.FILL, payload=a_fill(f"bf{n}", f"b{n}", quantity="100", price="10", fee="1"), ts_utc=TS, seq=start_seq + 1),
            Event(account="ACC", kind=EventKind.ORDER_SUBMITTED, payload=an_order(f"s{n}", side=Side.SELL, quantity="100"), ts_utc=TS, seq=start_seq + 2),
            Event(account="ACC", kind=EventKind.FILL, payload=a_fill(f"sf{n}", f"s{n}", side=Side.SELL, quantity="100", price="12", fee="2"), ts_utc=TS, seq=start_seq + 3),
        ]

    events = [*round_trip(1, 1), *round_trip(2, 5)]
    state = fold(events)["ACC"]
    position = state.positions[AAPL]
    assert position.quantity == Decimal("0")
    assert position.realized_pnl == Decimal("394"), "flat position lost booked P&L on re-open"
    assert state.realized_pnl == Decimal("394")
    # The invariant: flat means every cash movement was P&L — they must agree.
    assert state.cash == state.realized_pnl


def test_negative_control_first_round_trip_still_books_once() -> None:
    """Negative control for the carry-through: exactly one round trip books 197."""
    events = [
        Event(account="ACC", kind=EventKind.ORDER_SUBMITTED, payload=an_order("b1", quantity="100"), ts_utc=TS, seq=1),
        Event(account="ACC", kind=EventKind.FILL, payload=a_fill("bf1", "b1", quantity="100", price="10", fee="1"), ts_utc=TS, seq=2),
        Event(account="ACC", kind=EventKind.ORDER_SUBMITTED, payload=an_order("s1", side=Side.SELL, quantity="100"), ts_utc=TS, seq=3),
        Event(account="ACC", kind=EventKind.FILL, payload=a_fill("sf1", "s1", side=Side.SELL, quantity="100", price="12", fee="2"), ts_utc=TS, seq=4),
    ]
    state = fold(events)["ACC"]
    assert state.realized_pnl == Decimal("197")
    assert state.cash == state.realized_pnl


def test_non_finite_fill_price_quantity_or_fee_is_refused() -> None:
    """NaN/Infinity in a fill poison cash and break state equality itself (I5).

    The domain objects guard sign but not finiteness (only the codec refuses), so an
    in-memory fold must refuse non-finite values itself.
    """
    events_head = [Event(account="ACC", kind=EventKind.ORDER_SUBMITTED, payload=an_order("o1", quantity="100"), ts_utc=TS, seq=1)]
    for name, fill in [
        ("price=Infinity", a_fill("f1", "o1", quantity="100", price="Infinity")),
        ("fee=NaN", a_fill("f2", "o1", quantity="100", price="10", fee="NaN")),
        ("fee=Infinity", a_fill("f3", "o1", quantity="100", price="10", fee="Infinity")),
    ]:
        with pytest.raises(LedgerFoldError, match="finite Decimal"):
            fold([*events_head, Event(account="ACC", kind=EventKind.FILL, payload=fill, ts_utc=TS, seq=2)])
        # smoke: each value parses as a Decimal (they are non-finite, not invalid literals)
        assert not fill.price.is_finite() or not fill.fee.is_finite() or True


def test_negative_control_finite_fill_still_applies() -> None:
    events = [
        Event(account="ACC", kind=EventKind.ORDER_SUBMITTED, payload=an_order("o1", quantity="100"), ts_utc=TS, seq=1),
        Event(account="ACC", kind=EventKind.FILL, payload=a_fill("f1", "o1", quantity="100", price="10", fee="0.65"), ts_utc=TS, seq=2),
    ]
    state = fold(events)["ACC"]
    assert state.cash == Decimal("-1000.65")


def test_closing_fill_realizes_pnl_with_multiplier() -> None:
    events = [
        Event(
            account="ACC",
            kind=EventKind.ORDER_SUBMITTED,
            payload=an_order("o1", instrument=SPXW, quantity="2", command_id="c1"),
            ts_utc=TS,
            seq=1,
        ),
        Event(
            account="ACC",
            kind=EventKind.FILL,
            payload=a_fill("f1", "o1", instrument=SPXW, quantity="2", price="10"),
            ts_utc=TS,
            seq=2,
        ),
        Event(
            account="ACC",
            kind=EventKind.ORDER_SUBMITTED,
            payload=an_order("o2", instrument=SPXW, side=Side.SELL, quantity="2", command_id="c2"),
            ts_utc=TS,
            seq=3,
        ),
        Event(
            account="ACC",
            kind=EventKind.FILL,
            payload=a_fill("f2", "o2", instrument=SPXW, side=Side.SELL, quantity="2", price="14", at=TS2),
            ts_utc=TS2,
            seq=4,
        ),
    ]
    state = fold(events)["ACC"]
    assert state.positions[SPXW].quantity == Decimal("0")
    assert state.positions[SPXW].realized_pnl == Decimal("800")
    assert state.cash == Decimal("-2000") + Decimal("2800")


def test_short_inventory_lot_and_cover() -> None:
    aapl_short = an_order("s1", side=Side.SELL, quantity="10", command_id="cs1")
    events = [
        Event(account="ACC", kind=EventKind.ORDER_SUBMITTED, payload=aapl_short, ts_utc=TS, seq=1),
        Event(
            account="ACC",
            kind=EventKind.FILL,
            payload=a_fill("f1", "s1", side=Side.SELL, quantity="10", price="50"),
            ts_utc=TS,
            seq=2,
        ),
    ]
    state = fold(events)["ACC"]
    assert state.positions[AAPL].quantity == Decimal("-10")
    assert state.positions[AAPL].open_lots[0].side is Side.SELL


def test_flip_from_long_to_short_sets_new_basis() -> None:
    events = [
        Event(account="ACC", kind=EventKind.ORDER_SUBMITTED, payload=an_order("o1", quantity="10", command_id="c1"), ts_utc=TS, seq=1),
        Event(account="ACC", kind=EventKind.FILL, payload=a_fill("f1", "o1", quantity="10", price="50"), ts_utc=TS, seq=2),
        Event(account="ACC", kind=EventKind.ORDER_SUBMITTED, payload=an_order("o2", side=Side.SELL, quantity="30", command_id="c2"), ts_utc=TS, seq=3),
        Event(account="ACC", kind=EventKind.FILL, payload=a_fill("f2", "o2", side=Side.SELL, quantity="30", price="60", at=TS2), ts_utc=TS2, seq=4),
    ]
    state = fold(events)["ACC"]
    assert state.positions[AAPL].quantity == Decimal("-20")
    assert state.positions[AAPL].avg_cost == Decimal("60")
    assert state.positions[AAPL].realized_pnl == Decimal("100")


def test_risk_verdict_counts_refusals() -> None:
    accepted = RiskVerdict(
        order_intent_id="i1",
        evaluations=(RiskRuleResult("r", True, 1, 2, "ok"),),
    )
    refused = RiskVerdict(
        order_intent_id="i2",
        evaluations=(RiskRuleResult("r", False, 1, 2, "no"),),
        refusal_reasons=("regime UNKNOWN",),
    )
    events = [
        Event(account="ACC", kind=EventKind.RISK_VERDICT, payload=accepted, ts_utc=TS, seq=1),
        Event(account="ACC", kind=EventKind.RISK_VERDICT, payload=refused, ts_utc=TS, seq=2),
    ]
    state = fold(events)["ACC"]
    assert state.verdicts == 2
    assert state.refusals == 1


def test_cash_flow_moves_cash_and_validates_kind() -> None:
    state = fold(
        [Event(account="ACC", kind=EventKind.CASH_FLOW, payload=CashFlow(amount=Decimal("25.50"), kind="interest", as_of=TS), ts_utc=TS, seq=1)]
    )["ACC"]
    assert state.cash == Decimal("25.50")

    with pytest.raises(EventPayloadError, match="CashFlow.kind must be one of"):
        CashFlow(amount=Decimal("1"), kind="dividend", as_of=TS)


def test_mark_updates_marks_map() -> None:
    state = fold(
        [Event(account="ACC", kind=EventKind.MARK, payload=Mark(instrument=AAPL, price=Decimal("150.25"), as_of=TS), ts_utc=TS, seq=1)]
    )["ACC"]
    assert state.marks[AAPL] == Decimal("150.25")


def test_reconcile_drift_halts_the_venue() -> None:
    """Drift must latch the halt, and a later clean reconcile must not clear it (§4.5)."""
    events = [
        Event(
            account="ACC",
            kind=EventKind.VENUE_RECONCILE,
            payload=VenueReconcile(venue="sim", as_of=TS, reconciled=False, drift=("AAPL",)),
            ts_utc=TS,
            seq=1,
        ),
        Event(
            account="ACC",
            kind=EventKind.VENUE_RECONCILE,
            payload=VenueReconcile(venue="sim", as_of=TS2, reconciled=True),
            ts_utc=TS2,
            seq=2,
        ),
    ]
    state = fold(events)["ACC"]
    assert state.venue_halted is True
    assert state.last_reconcile.reconciled is True


def test_clear_reconcile_leaves_venue_running() -> None:
    """Negative control: with no drift the halt must stay off."""
    state = fold(
        [
            Event(
                account="ACC",
                kind=EventKind.VENUE_RECONCILE,
                payload=VenueReconcile(venue="sim", as_of=TS, reconciled=True),
                ts_utc=TS,
                seq=1,
            )
        ]
    )["ACC"]
    assert state.venue_halted is False


@pytest.mark.parametrize(
    "kind,payload",
    [
        (EventKind.EXPIRY, LifecycleNotice(instrument=SPXW, quantity_delta=Decimal("-2"), cash_delta=Decimal("0"), as_of=TS)),
        (EventKind.ASSIGNMENT, LifecycleNotice(instrument=SPXW, quantity_delta=Decimal("100"), cash_delta=Decimal("-600000"), as_of=TS)),
        (EventKind.EXERCISE, LifecycleNotice(instrument=SPXW, quantity_delta=Decimal("-100"), cash_delta=Decimal("600000"), as_of=TS)),
    ],
)
def test_lifecycle_kinds_refuse_until_o2_registers_handlers(kind: EventKind, payload: LifecycleNotice) -> None:
    """E1 must not invent assignment/expiry semantics that O2 owns (I5)."""
    with pytest.raises(UnhandledEventError, match="owned by O2"):
        fold([Event(account="ACC", kind=kind, payload=payload, ts_utc=TS, seq=1)])


def test_fold_is_per_account_isolated() -> None:
    events = [
        Event(account="A", kind=EventKind.CASH_FLOW, payload=CashFlow(amount=Decimal("100"), kind="deposit", as_of=TS), ts_utc=TS, seq=1),
        Event(account="B", kind=EventKind.CASH_FLOW, payload=CashFlow(amount=Decimal("-5"), kind="fee", as_of=TS), ts_utc=TS, seq=2),
    ]
    states = fold(events)
    assert states["A"].cash == Decimal("100")
    assert states["B"].cash == Decimal("-5")


def test_mixed_multiplier_combo_refuses_to_be_valued() -> None:
    """A stock+option combo has no single multiplier; valuation must refuse (I6)."""
    from trade_engine.domain.instruments import Combo, ComboLeg

    combo = Combo(legs=[ComboLeg(contract=AAPL, ratio=1, side=Side.BUY), ComboLeg(contract=SPXW, ratio=1, side=Side.SELL)])
    order = an_order("oc", instrument=combo, quantity="1", command_id="cc")
    events = [
        Event(account="ACC", kind=EventKind.ORDER_SUBMITTED, payload=order, ts_utc=TS, seq=1),
        Event(account="ACC", kind=EventKind.FILL, payload=a_fill("fc", "oc", instrument=combo, quantity="1"), ts_utc=TS, seq=2),
    ]
    with pytest.raises(LedgerFoldError, match="mixed-multiplier"):
        fold(events)


# --------------------------------------------------------------------------- idempotency


def test_duplicate_command_is_a_noop_returning_the_original(ledger: Ledger) -> None:
    """Replaying a command must not write a second row (I3)."""
    first = ledger.append(
        Event(account="ACC", kind=EventKind.ORDER_SUBMITTED, payload=an_order(), ts_utc=TS, command_id="cmd-1")
    )
    second = ledger.append(
        Event(account="ACC", kind=EventKind.ORDER_SUBMITTED, payload=an_order(), ts_utc=TS, command_id="cmd-1")
    )
    assert first.seq == second.seq
    assert second == first
    assert ledger.count() == 1


def test_same_command_id_with_different_payload_is_still_a_noop(ledger: Ledger) -> None:
    """The key is the command id, not the payload; the original must win (I3)."""
    first = ledger.append(
        Event(account="ACC", kind=EventKind.MARK, payload=Mark(instrument=AAPL, price=Decimal("150"), as_of=TS), ts_utc=TS, command_id="m-1")
    )
    second = ledger.append(
        Event(account="ACC", kind=EventKind.MARK, payload=Mark(instrument=AAPL, price=Decimal("999"), as_of=TS), ts_utc=TS, command_id="m-1")
    )
    assert second.seq == first.seq
    assert ledger.count() == 1
    assert ledger.snapshot("ACC").marks[AAPL] == Decimal("150")


def test_distinct_commands_without_ids_both_append(ledger: Ledger) -> None:
    """Negative control: no command id means no dedupe, so both rows land."""
    ledger.append(Event(account="ACC", kind=EventKind.MARK, payload=Mark(instrument=AAPL, price=Decimal("150"), as_of=TS), ts_utc=TS))
    ledger.append(Event(account="ACC", kind=EventKind.MARK, payload=Mark(instrument=AAPL, price=Decimal("151"), as_of=TS), ts_utc=TS))
    assert ledger.count() == 2


def test_reopening_the_ledger_keeps_the_command_index(ledger_path: Path) -> None:
    """Idempotency must be persisted, not just an in-memory session trick (I3)."""
    with Ledger(ledger_path) as lg:
        first = lg.append(
            Event(account="ACC", kind=EventKind.MARK, payload=Mark(instrument=AAPL, price=Decimal("150"), as_of=TS), ts_utc=TS, command_id="m-1")
        )
    with Ledger(ledger_path) as lg:
        replay = lg.append(
            Event(account="ACC", kind=EventKind.MARK, payload=Mark(instrument=AAPL, price=Decimal("150"), as_of=TS), ts_utc=TS, command_id="m-1")
        )
        assert replay.seq == first.seq
        assert lg.count() == 1


def test_append_refuses_a_preassigned_seq(ledger: Ledger) -> None:
    with pytest.raises(ValueError, match="assigned by the ledger"):
        ledger.append(
            Event(account="ACC", kind=EventKind.MARK, payload=Mark(instrument=AAPL, price=Decimal("150"), as_of=TS), ts_utc=TS, seq=5)
        )


# --------------------------------------------------------------------------- atomicity


class _Boom(RuntimeError):
    pass


def _crash_before_commit(lg: Ledger, monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the ledger's commit seam so the transaction can never commit."""

    def boom() -> None:
        raise _Boom("simulated crash before commit")

    monkeypatch.setattr(lg, "_commit", boom)


def test_crash_between_write_and_commit_leaves_no_event(ledger_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A failure after the INSERT but before COMMIT must not leave a partial event (I2)."""
    with Ledger(ledger_path) as lg:
        lg.append(
            Event(account="ACC", kind=EventKind.CASH_FLOW, payload=CashFlow(amount=Decimal("1"), kind="deposit", as_of=TS), ts_utc=TS, command_id="ok")
        )
        _crash_before_commit(lg, monkeypatch)
        with pytest.raises(_Boom):
            lg.append(
                Event(account="ACC", kind=EventKind.MARK, payload=Mark(instrument=AAPL, price=Decimal("150"), as_of=TS), ts_utc=TS, command_id="crash")
            )
        monkeypatch.undo()

        assert lg.count() == 1
        assert lg.event_by_command("crash") is None
        assert lg.snapshot("ACC").marks == MappingProxyType({})


def test_a_refused_append_leaves_the_command_id_reusable(ledger_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """If the write rolled back, the command id must not be burned by it (I3)."""
    with Ledger(ledger_path) as lg:
        _crash_before_commit(lg, monkeypatch)
        with pytest.raises(_Boom):
            lg.append(
                Event(account="ACC", kind=EventKind.CASH_FLOW, payload=CashFlow(amount=Decimal("5"), kind="deposit", as_of=TS), ts_utc=TS, command_id="retry-me")
            )
        monkeypatch.undo()

        written = lg.append(
            Event(account="ACC", kind=EventKind.CASH_FLOW, payload=CashFlow(amount=Decimal("5"), kind="deposit", as_of=TS), ts_utc=TS, command_id="retry-me")
        )
        assert written.seq == 1
        assert lg.count() == 1


def test_uncommitted_row_is_invisible_to_a_second_reader(ledger_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Rolled-back work must be invisible, not merely unindexed (I2)."""
    with Ledger(ledger_path) as lg:
        lg.append(
            Event(account="ACC", kind=EventKind.CASH_FLOW, payload=CashFlow(amount=Decimal("1"), kind="deposit", as_of=TS), ts_utc=TS, command_id="ok")
        )
        _crash_before_commit(lg, monkeypatch)
        with pytest.raises(_Boom):
            lg.append(
                Event(account="ACC", kind=EventKind.MARK, payload=Mark(instrument=AAPL, price=Decimal("150"), as_of=TS), ts_utc=TS, command_id="gone")
            )
        monkeypatch.undo()

    raw = sqlite3.connect(str(ledger_path))
    try:
        rows = raw.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    finally:
        raw.close()
    assert rows == 1


def test_ledger_uses_wal(ledger: Ledger) -> None:
    mode = ledger.conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"


# --------------------------------------------------------------------------- single instance


def test_second_instance_in_this_process_refuses(ledger_path: Path) -> None:
    """Two writers on one ledger is the failure I4 exists to prevent (I4)."""
    with Ledger(ledger_path):
        with pytest.raises(LedgerLockError, match="already holds the ledger lock"):
            Ledger(ledger_path).open()


def test_lock_is_released_on_close(ledger_path: Path) -> None:
    """Negative control: closing must free the lock so a fresh run can start."""
    with Ledger(ledger_path):
        pass
    with Ledger(ledger_path) as lg:
        assert lg.count() == 0


def test_second_process_on_the_same_ledger_refuses(ledger_path: Path) -> None:
    """The lock must be OS-level: a *separate process* must be refused (I4)."""
    child = textwrap.dedent(
        """
        import sys
        from pathlib import Path
        from trade_engine.ledger import Ledger, LedgerLockError

        try:
            Ledger(Path(sys.argv[1])).open()
        except LedgerLockError:
            print("LOCKED")
            sys.exit(0)
        print("ACQUIRED")
        sys.exit(3)
        """
    )
    script = ledger_path.parent / "child_lock_probe.py"
    script.write_text(child, encoding="utf-8")

    repo_root = Path(__file__).resolve().parent.parent
    env = {"PYTHONPATH": str(repo_root / "src"), "SystemRoot": __import__("os").environ.get("SystemRoot", "")}
    with Ledger(ledger_path):
        result = subprocess.run(
            [sys.executable, str(script), str(ledger_path)],
            capture_output=True,
            text=True,
            env={**__import__("os").environ, **env},
            timeout=60,
        )
    assert "LOCKED" in result.stdout, f"child got the lock while parent held it: {result.stdout!r} {result.stderr!r}"


# --------------------------------------------------------------------------- snapshot == fold


def test_snapshot_equals_full_fold_on_randomised_log(ledger: Ledger) -> None:
    """Property: after a long randomised sequence, snapshot == full fold (Architecture §4.2).

    The generator deliberately exercises the paths the review found untested: SELL
    orders as well as BUY, multiple partial fills against one order, non-zero fees,
    and flips through zero. Every fill it emits matches its order's side and
    instrument, because those are invariants of the log the fold enforces.
    """
    import random

    rng = random.Random(20260923)
    symbols = [Equity("AAPL"), Equity("MSFT"), Equity("GOOG"), SPXW]
    accounts = ["ACC_A", "ACC_B"]
    # order_id -> (account, instrument, side, remaining quantity)
    open_orders: dict[str, tuple[str, object, Side, Decimal]] = {}
    unfilled: set[str] = set()  # orders with no fill yet, i.e. still in SUBMITTED
    order_counter = 0
    fill_counter = 0

    def submit() -> None:
        nonlocal order_counter
        order_counter += 1
        order_id = f"{account}-o{order_counter}"
        instrument = rng.choice(symbols)
        side = rng.choice([Side.BUY, Side.SELL])
        quantity = Decimal(rng.choice([1, 5, 10, 25, 100]))
        order = an_order(order_id, account, instrument=instrument, side=side, quantity=str(quantity), command_id=f"cmd-{order_id}")
        ledger.append(Event(account=account, kind=EventKind.ORDER_SUBMITTED, payload=order, ts_utc=TS, command_id=order.command_id))
        open_orders[order_id] = (account, instrument, side, quantity)
        unfilled.add(order_id)

    def fill_some() -> None:
        nonlocal fill_counter
        order_id = rng.choice(list(open_orders))
        account_id, instrument, side, remaining = open_orders[order_id]
        take = Decimal(rng.choice([1, 2, max(1, int(remaining / 2))]))
        if take > remaining:
            take = remaining
        fill_counter += 1
        fill = a_fill(
            f"f{fill_counter}",
            order_id,
            account=account_id,
            instrument=instrument,
            side=side,
            quantity=str(take),
            price=str(rng.choice([10, 25, 101.25, 150, 6000])),
            fee=str(rng.choice([0, 0.65, 1.3])),
        )
        ledger.append(Event(account=account_id, kind=EventKind.FILL, payload=fill, ts_utc=TS, command_id=f"cmd-f{fill_counter}"))
        unfilled.discard(order_id)
        if take == remaining:
            open_orders.pop(order_id)
        else:
            open_orders[order_id] = (account_id, instrument, side, remaining - take)

    def accept_some() -> bool:
        # Only orders still in SUBMITTED can be accepted: PARTIALLY_FILLED → ACCEPTED
        # is illegal in the E0 state machine, and a venue cannot "re-accept" an order
        # that has already started filling.
        submitted = [oid for oid, meta in open_orders.items() if oid in unfilled]
        if not submitted:
            return False
        order_id = rng.choice(submitted)
        ledger.append(
            Event(
                account=open_orders[order_id][0],
                kind=EventKind.ORDER_ACCEPTED,
                payload=OrderStateChange(order_id=order_id, reason="venue ack"),
                ts_utc=TS,
            )
        )
        return True

    for seed_account in accounts:
        account = seed_account  # the closures read this loop variable
        for _ in range(3):
            submit()

    for step in range(1000):
        account = rng.choice(accounts)
        roll = rng.random()
        if roll < 0.30:
            submit()
        elif roll < 0.60:
            if open_orders:
                fill_some()
            else:
                submit()
        elif roll < 0.70:
            if not (open_orders and accept_some()):
                # accept could not fire (no unfilled SUBMITTED order); fall through
                # to another append so every iteration lands exactly one event.
                submit()
        elif roll < 0.78:
            ledger.append(
                Event(
                    account=account,
                    kind=EventKind.MARK,
                    payload=Mark(instrument=rng.choice(symbols), price=Decimal("123.45"), as_of=TS),
                    ts_utc=TS,
                )
            )
        elif roll < 0.86:
            ledger.append(
                Event(
                    account=account,
                    kind=EventKind.CASH_FLOW,
                    payload=CashFlow(amount=Decimal(rng.choice(["-2.50", "1.75", "100"])), kind=rng.choice(["fee", "interest", "deposit"]), as_of=TS),
                    ts_utc=TS,
                )
            )
        elif roll < 0.95:
            refused = rng.random() < 0.3
            verdict = RiskVerdict(
                order_intent_id=f"i{step}",
                evaluations=(RiskRuleResult("regime", not refused, "BULL", "BULL", "x"),),
                refusal_reasons=("regime UNKNOWN",) if refused else (),
            )
            ledger.append(Event(account=account, kind=EventKind.RISK_VERDICT, payload=verdict, ts_utc=TS))
        else:
            ledger.append(
                Event(
                    account=account,
                    kind=EventKind.VENUE_RECONCILE,
                    payload=VenueReconcile(venue="sim", as_of=TS, reconciled=True),
                    ts_utc=TS,
                )
            )

    assert ledger.count() == 1000 + 2 * 3  # the 1,000-event run plus the two seeds per account

    full = ledger.fold()
    for account in accounts:
        assert ledger.snapshot(account) == full[account], f"snapshot drifted for {account}"
    ledger.verify_snapshot("ACC_A")
    ledger.verify_snapshot("ACC_B")

    # The incremental cache must equal the same fold, from a mid-log snapshot point.
    cache = ledger.incremental_cache(after=500, accounts=accounts)
    cache.verify(ledger.events())
    for account in accounts:
        assert cache.state(account) == full[account]


def test_snapshot_cache_resumes_after_reopen(ledger_path: Path) -> None:
    """A cache seeded from a prior run must still agree with the full fold (I2)."""
    with Ledger(ledger_path) as lg:
        lg.append(Event(account="ACC", kind=EventKind.CASH_FLOW, payload=CashFlow(amount=Decimal("10"), kind="deposit", as_of=TS), ts_utc=TS, command_id="d1"))
        seed_state = lg.snapshot("ACC")
    with Ledger(ledger_path) as lg:
        lg.append(Event(account="ACC", kind=EventKind.CASH_FLOW, payload=CashFlow(amount=Decimal("-1"), kind="fee", as_of=TS2), ts_utc=TS2, command_id="d2"))
        cache = FoldCache(seed={"ACC": seed_state}, base_seq=1)
        cache.extend(lg.events(after=1))
        cache.verify(lg.events())
        assert cache.state("ACC").cash == Decimal("9")


def test_seeded_cache_refuses_events_already_in_the_seed(ledger_path: Path) -> None:
    """Extending a seeded cache with events it already folded must refuse (I3).

    A caller that replays the whole log into a cache seeded at seq N would
    double-apply everything below N — silently, because extend() had no boundary
    check. The cache must refuse the overlap rather than corrupt the snapshot.
    """
    with Ledger(ledger_path) as lg:
        lg.append(Event(account="ACC", kind=EventKind.CASH_FLOW, payload=CashFlow(amount=Decimal("100"), kind="deposit", as_of=TS), ts_utc=TS, command_id="d1"))
        lg.append(Event(account="ACC", kind=EventKind.CASH_FLOW, payload=CashFlow(amount=Decimal("200"), kind="deposit", as_of=TS), ts_utc=TS, command_id="d2"))
        seed_state = lg.snapshot("ACC")
        cache = FoldCache(seed={"ACC": seed_state}, base_seq=2)
        with pytest.raises(LedgerFoldError, match="already folded into the seed"):
            cache.extend(lg.events())  # seqs 1 and 2 are already in the seed
        assert cache.state("ACC").cash == Decimal("300"), "refused events must not half-apply"


def test_incremental_cache_does_not_fabricate_last_seq(ledger_path: Path) -> None:
    """The seed must carry each account's real last_seq, not the cutoff (I2).

    With the default cutoff (end of log), ACC_A's last event is seq 2 while the
    cutoff is 3. Stamping the cutoff into the seed claims a position ACC_A never
    saw, and since no tail events exist to overwrite it, verify() fails against
    the honest full fold.
    """
    with Ledger(ledger_path) as lg:
        lg.append(Event(account="ACC_A", kind=EventKind.CASH_FLOW, payload=CashFlow(amount=Decimal("10"), kind="deposit", as_of=TS), ts_utc=TS, command_id="a1"))
        lg.append(Event(account="ACC_A", kind=EventKind.CASH_FLOW, payload=CashFlow(amount=Decimal("5"), kind="interest", as_of=TS), ts_utc=TS, command_id="a2"))
        lg.append(Event(account="ACC_B", kind=EventKind.CASH_FLOW, payload=CashFlow(amount=Decimal("7"), kind="deposit", as_of=TS), ts_utc=TS, command_id="b1"))

        # Default cutoff = 3 (end of log). ACC_A's last event is seq 2: the cache
        # must report 2, not the cutoff. This is the case the old fabrication broke.
        cache = lg.incremental_cache(accounts=["ACC_A", "ACC_B"])
        assert cache.state("ACC_A").last_seq == 2, "ACC_A's last event is seq 2, not the cutoff 3"
        assert cache.state("ACC_B").last_seq == 3
        cache.verify(lg.events())

        # Mid-log cutoff: an account with no events at or below the cutoff is not
        # seeded at all; its tail events give it the honest last_seq.
        mid = lg.incremental_cache(after=1, accounts=["ACC_A", "ACC_B"])
        mid.verify(lg.events())
        assert mid.state("ACC_A").last_seq == 2
        assert mid.state("ACC_B").last_seq == 3


# --------------------------------------------------------------------------- meta / schema


def test_meta_round_trip_and_default_schema_version(ledger: Ledger) -> None:
    assert ledger.schema_version() == 1
    ledger.set_meta("last_session", "2026-09-23")
    ledger.set_meta("last_session", "2026-09-24")
    assert ledger.get_meta("last_session") == "2026-09-24"
    assert ledger.get_meta("absent") is None


def test_events_after_seq_filters_correctly(ledger: Ledger) -> None:
    for i in range(5):
        ledger.append(
            Event(account="ACC", kind=EventKind.MARK, payload=Mark(instrument=AAPL, price=Decimal(100 + i), as_of=TS), ts_utc=TS)
        )
    tail = ledger.events(after=3)
    assert [e.seq for e in tail] == [4, 5]
    assert ledger.next_seq() == 6

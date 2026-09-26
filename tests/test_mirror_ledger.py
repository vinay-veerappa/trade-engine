"""T2 mirror ledger tests: Mirror* events, their codec, and the per-venue mirror fold.

The mirror is folded under the venue's own ledger account and never touches a sim
account's positions or cash. Every guard has a firing and a non-firing test (§0.2).
"""

import sys
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pytest

from trade_engine.domain.instruments import Combo, ComboLeg, Equity, OptionContract, Side
from trade_engine.domain.orders import OrderState, OrderType, TimeInForce
from trade_engine.ledger import Ledger, codec
from trade_engine.ledger.events import (
    CashFlow,
    Event,
    EventKind,
    EventPayloadError,
    MirrorAck,
    MirrorAllocation,
    MirrorFill,
    MirrorQueued,
    MirrorRefused,
    VenueReconcile,
    mirror_account,
)
from trade_engine.ledger.mirror import MirrorState, pro_rata, ticket_contracts
from trade_engine.ledger.state import LedgerFoldError, fold, halted_venues, mirror_state

T = datetime(2026, 9, 24, 20, 0, tzinfo=timezone.utc)
VENUE = "D-00000001"
ACCT = mirror_account(VENUE)
P200 = OptionContract(underlying="AAPL", expiry=date(2026, 10, 16), strike=Decimal("200"), right="P")
P190 = OptionContract(underlying="AAPL", expiry=date(2026, 10, 16), strike=Decimal("190"), right="P")
SPREAD = Combo((ComboLeg(P200, 1, Side.SELL), ComboLeg(P190, 1, Side.BUY)))


def _alloc(oid: str = "so-1", account: str = "OPT_CSP", qty: str = "1") -> MirrorAllocation:
    return MirrorAllocation(oid, account, Decimal(qty))


def _queued(key: str = "tos:a", *, instrument=P200, side=Side.SELL, qty: str = "1", allocations=None, **kw) -> MirrorQueued:
    fields = dict(
        venue=VENUE,
        ticket_key=key,
        instrument=instrument,
        side=side,
        quantity=Decimal(qty),
        order_type=OrderType.LIMIT,
        limit_price=Decimal("2.00"),
        tif=TimeInForce.DAY,
        allocations=tuple(allocations or (_alloc(qty=qty),)),
        at=T,
    )
    fields.update(kw)
    return MirrorQueued(**fields)


def _ack(key: str = "tos:a", status: str = "ACCEPTED", oid: str | None = "5400000001", book=OrderState.ACCEPTED, **kw) -> MirrorAck:
    return MirrorAck(venue=VENUE, ticket_key=key, status=status, message="read back", at=T,
                     venue_order_id=oid, book_status=book, **kw)


def _fill(key: str = "tos:a", filled: str = "1", price: str = "2.00", oid: str = "5400000001") -> MirrorFill:
    return MirrorFill(venue=VENUE, ticket_key=key, venue_order_id=oid, filled=Decimal(filled),
                      avg_price=Decimal(price), at=T)


def _refused(oid: str = "so-9", reason: str = "conflict") -> MirrorRefused:
    return MirrorRefused(venue=VENUE, strategy_order_id=oid, strategy_account="OPT_CSP", reason=reason, at=T)


_KIND = {MirrorQueued: EventKind.MIRROR_QUEUED, MirrorRefused: EventKind.MIRROR_REFUSED,
         MirrorAck: EventKind.MIRROR_ACK, MirrorFill: EventKind.MIRROR_FILL}


def _event(payload, seq: int | None = None, account: str = ACCT) -> Event:
    return Event(account=account, kind=_KIND[type(payload)], payload=payload, ts_utc=T, seq=seq)


def _fold(*payloads) -> MirrorState:
    states = fold([_event(p, seq=i + 1) for i, p in enumerate(payloads)])
    return mirror_state(states, VENUE)


def _refuses(*payloads, match: str) -> None:
    with pytest.raises(LedgerFoldError, match=match):
        _fold(*payloads)


# -- the events ------------------------------------------------------------------------


def test_mirror_account_is_the_venue_pseudo_account() -> None:
    assert mirror_account(VENUE) == "__venue__:D-00000001"
    with pytest.raises(EventPayloadError):
        mirror_account("")


def test_mirror_events_must_be_filed_under_their_venue_account() -> None:
    _event(_queued())  # the venue's own account: fine
    with pytest.raises(EventPayloadError, match="must be filed under"):
        _event(_queued(), account="OPT_CSP")
    with pytest.raises(EventPayloadError, match="must be filed under"):
        _event(_fill(), account=mirror_account("D-00000002"))


def test_a_queued_ticket_takes_a_single_option_or_a_vertical() -> None:
    assert _queued(instrument=SPREAD, qty="2").quantity == Decimal("2")
    with pytest.raises(EventPayloadError, match="option contract or a vertical"):
        _queued(instrument=Equity("AAPL"))


@pytest.mark.parametrize(
    "legs",
    [
        (ComboLeg(P200, 1, Side.SELL),),
        (ComboLeg(P200, 1, Side.SELL), ComboLeg(Equity("AAPL"), 1, Side.BUY)),
        (ComboLeg(P200, 1, Side.SELL), ComboLeg(P190, 1, Side.SELL)),
        (ComboLeg(P200, 1, Side.SELL), ComboLeg(P200, 1, Side.BUY)),
        (ComboLeg(P200, 1, Side.SELL), ComboLeg(P190, 2, Side.BUY)),
        (ComboLeg(P200, 1, Side.SELL), ComboLeg(OptionContract("MSFT", date(2026, 10, 16), Decimal("190"), "P"), 1, Side.BUY)),
        (ComboLeg(P200, 1, Side.SELL), ComboLeg(OptionContract("AAPL", date(2026, 11, 20), Decimal("190"), "P"), 1, Side.BUY)),
        (ComboLeg(P200, 1, Side.SELL), ComboLeg(OptionContract("AAPL", date(2026, 10, 16), Decimal("190"), "C"), 1, Side.BUY)),
        (ComboLeg(P200, 1, Side.SELL), ComboLeg(OptionContract("AAPL", date(2026, 10, 16), Decimal("190"), "P", multiplier=10), 1, Side.BUY)),
    ],
)
def test_a_queued_combo_that_is_not_a_vertical_refuses(legs) -> None:
    with pytest.raises(EventPayloadError, match="vertical"):
        _queued(instrument=Combo(legs))


@pytest.mark.parametrize("qty", ["0", "-1", "1.5"])
def test_a_queued_quantity_is_a_positive_whole_number(qty) -> None:
    with pytest.raises(EventPayloadError, match="whole number"):
        _queued(qty=qty, allocations=(_alloc(qty="1"),))


def test_queued_order_type_tif_and_limit_are_checked() -> None:
    _queued(order_type=OrderType.MARKET, limit_price=None)
    with pytest.raises(EventPayloadError, match="MARKET or LIMIT"):
        _queued(order_type=OrderType.STOP)
    with pytest.raises(EventPayloadError, match="DAY or GTC"):
        _queued(tif=TimeInForce.GTD)
    with pytest.raises(EventPayloadError, match="needs a limit_price"):
        _queued(limit_price=None)
    with pytest.raises(EventPayloadError, match="must be positive"):
        _queued(limit_price=Decimal("0"))
    with pytest.raises(EventPayloadError, match="cannot carry a limit_price"):
        _queued(order_type=OrderType.MARKET)


def test_queued_allocations_must_add_up_and_be_unique() -> None:
    _queued(qty="3", allocations=(_alloc("a", qty="1"), _alloc("b", qty="2")))
    with pytest.raises(EventPayloadError, match="total 2 but the ticket is 3"):
        _queued(qty="3", allocations=(_alloc("a", qty="1"), _alloc("b", qty="1")))
    with pytest.raises(EventPayloadError, match="twice"):
        _queued(qty="2", allocations=(_alloc("a"), _alloc("a")))
    with pytest.raises(EventPayloadError, match="at least one"):
        MirrorQueued(**{**_queued().__dict__, "allocations": ()})
    with pytest.raises(EventPayloadError, match="whole number"):
        _alloc(qty="0.5")


def test_queued_needs_a_utc_time() -> None:
    with pytest.raises(EventPayloadError):
        _queued(at=datetime(2026, 9, 24, 20, 0))


def test_an_ack_names_only_an_all_digit_order_id_and_a_known_status() -> None:
    assert _ack(oid=None, book=None).venue_order_id is None
    with pytest.raises(EventPayloadError, match="all-digit"):
        _ack(oid="54-01")
    with pytest.raises(EventPayloadError, match="status"):
        _ack(status="FILLED")
    with pytest.raises(EventPayloadError, match="OrderState"):
        _ack(book="WORKING")
    with pytest.raises(EventPayloadError, match="message"):
        MirrorAck(venue=VENUE, ticket_key="tos:a", status="PENDING", message="", at=T)


def test_a_fill_is_a_positive_whole_cumulative_at_a_positive_price() -> None:
    assert _fill(filled="3").filled == Decimal("3")
    for filled in ("0", "1.5"):
        with pytest.raises(EventPayloadError, match="whole number"):
            _fill(filled=filled)
    with pytest.raises(EventPayloadError, match="positive"):
        _fill(price="0")
    with pytest.raises(EventPayloadError, match="all-digit"):
        _fill(oid="x1")


def test_a_refusal_needs_a_reason() -> None:
    with pytest.raises(EventPayloadError, match="reason"):
        _refused(reason="")


@pytest.mark.parametrize(
    "payload",
    [
        _queued(),
        _queued("tos:v", instrument=SPREAD, qty="2", order_type=OrderType.LIMIT, limit_price=Decimal("1.05")),
        _queued("tos:m", order_type=OrderType.MARKET, limit_price=None, tif=TimeInForce.GTC),
        _refused(),
        _ack(),
        _ack(oid=None, book=None, status="PENDING"),
        _ack(book=OrderState.EXPIRED),
        _fill(filled="2", price="1.0500"),
    ],
)
def test_mirror_events_round_trip_through_the_codec(payload) -> None:
    event = _event(payload, seq=7)
    assert codec.decode_event(codec.encode_event(event)) == event


# -- the fold --------------------------------------------------------------------------


def test_queue_ack_fill_books_the_fill_and_nothing_else_is_expected() -> None:
    mirror = _fold(_queued(), _ack(), _fill())
    assert mirror.venue == VENUE and mirror.handled("so-1")
    assert dict(mirror.book) == {("OPT_CSP", P200): Decimal("-1")}
    assert mirror.open_tickets == () and mirror.expected() == {P200: Decimal("-1")}
    assert mirror.order_ids == {"5400000001": "tos:a"}


def test_an_open_ticket_is_expected_by_its_live_remainder() -> None:
    mirror = _fold(_queued(qty="3", allocations=(_alloc(qty="3"),)), _ack(), _fill(filled="1"))
    assert mirror.expected() == {P200: Decimal("-3")}  # 1 in the book + 2 resting
    assert dict(mirror.book) == {("OPT_CSP", P200): Decimal("-1")}
    assert mirror.tickets["tos:a"].remaining == Decimal("2")


@pytest.mark.parametrize("book", [OrderState.EXPIRED, OrderState.CANCELLED, OrderState.REJECTED])
def test_an_expired_cancelled_or_rejected_ticket_contributes_nothing_more(book) -> None:
    mirror = _fold(_queued(qty="3", allocations=(_alloc(qty="3"),)), _ack(), _fill(filled="1"), _ack(book=book))
    assert mirror.expected() == {P200: Decimal("-1")} and mirror.open_tickets == ()


def test_a_rejected_ack_closes_the_ticket_and_a_filled_row_does_not() -> None:
    assert _fold(_queued(), _ack(status="REJECTED", oid=None, book=None)).expected() == {}
    filled_row_first = _fold(_queued(), _ack(book=OrderState.FILLED))
    assert filled_row_first.expected() == {P200: Decimal("-1")}  # ends only through its fill
    assert filled_row_first.open_tickets != ()


def test_a_closed_ticket_stays_closed() -> None:
    mirror = _fold(_queued(), _ack(book=OrderState.EXPIRED), _ack(book=OrderState.ACCEPTED))
    assert mirror.tickets["tos:a"].closed and mirror.expected() == {}


def test_a_pending_ack_without_an_order_id_keeps_the_ticket_open() -> None:
    mirror = _fold(_queued(), _ack(status="PENDING", oid=None, book=None))
    assert mirror.expected() == {P200: Decimal("-1")} and mirror.tickets["tos:a"].venue_order_id is None


def test_the_same_cumulative_twice_is_a_no_op() -> None:
    once = _fold(_queued(qty="2", allocations=(_alloc(qty="2"),)), _ack(), _fill(filled="1"))
    twice = _fold(_queued(qty="2", allocations=(_alloc(qty="2"),)), _ack(), _fill(filled="1"), _fill(filled="1"))
    assert once == twice


def test_the_same_cumulative_at_another_price_refuses() -> None:
    _refuses(_queued(), _ack(), _fill(), _fill(price="2.05"), match="same fill")


def test_a_lower_cumulative_refuses() -> None:
    q = _queued(qty="3", allocations=(_alloc(qty="3"),))
    _fold(q, _ack(), _fill(filled="1"), _fill(filled="2"))
    _refuses(q, _ack(), _fill(filled="2"), _fill(filled="1"), match="cannot go down")


def test_an_over_fill_refuses() -> None:
    _fold(_queued(qty="2", allocations=(_alloc(qty="2"),)), _ack(), _fill(filled="2"))
    _refuses(_queued(), _ack(), _fill(filled="2"), match="over-fill")


def test_a_fill_needs_the_tickets_proven_order_id() -> None:
    _refuses(_queued(), _fill(), match="proven Order ID is None")
    _refuses(_queued(), _ack(), _fill(oid="5400000002"), match="proven Order ID is 5400000001")


def test_a_fill_or_ack_for_an_unknown_ticket_refuses() -> None:
    _refuses(_ack(), match="never queued")
    _refuses(_queued(), _ack(), _fill(key="tos:b"), match="never queued")


def test_an_order_id_belongs_to_one_ticket() -> None:
    _refuses(_queued(), _ack(), _ack(oid="5400000002"), match="an ack names 5400000002")
    b = _queued("tos:b", allocations=(_alloc("so-2"),))
    _fold(_queued(), b, _ack(), _ack("tos:b", oid="5400000002"))
    _refuses(_queued(), b, _ack(), _ack("tos:b"), match="already ticket 'tos:a'")
    _fold(_queued(), _ack(), _ack())  # the same id again: fine


def test_the_increment_is_allocated_pro_rata_and_never_taken_back() -> None:
    # Weights 2,1,2 filled 2 then 3: a split of the cumulative would move a contract
    # from so-b to so-c; the increment split never un-books a fill.
    q = _queued(qty="5", allocations=(_alloc("so-a", "OPT_CSP", "2"), _alloc("so-b", "OPT_PUT_SPREAD", "1"),
                                      _alloc("so-c", "OPT_CSP", "2")))
    after_2 = _fold(q, _ack(), _fill(filled="2"))
    assert dict(after_2.tickets["tos:a"].allocated) == {"so-a": 1, "so-b": 1, "so-c": 0}
    after_3 = _fold(q, _ack(), _fill(filled="2"), _fill(filled="3"))
    allocated = dict(after_3.tickets["tos:a"].allocated)
    assert allocated == {"so-a": 2, "so-b": 1, "so-c": 0}
    assert dict(after_3.book) == {("OPT_CSP", P200): Decimal("-2"), ("OPT_PUT_SPREAD", P200): Decimal("-1")}
    full = _fold(q, _ack(), _fill(filled="2"), _fill(filled="3"), _fill(filled="5"))
    assert dict(full.tickets["tos:a"].allocated) == {"so-a": 2, "so-b": 1, "so-c": 2}
    assert dict(full.book) == {("OPT_CSP", P200): Decimal("-4"), ("OPT_PUT_SPREAD", P200): Decimal("-1")}


def test_pro_rata_never_passes_a_weight_and_skips_full_shares() -> None:
    assert pro_rata([Decimal(0), Decimal(1), Decimal(1)], Decimal(2), Decimal(1)) == [0, 1, 0]
    assert pro_rata([Decimal(2), Decimal(1), Decimal(2)], Decimal(5), Decimal(2)) == [1, 1, 0]
    assert pro_rata([Decimal(1), Decimal(3)], Decimal(4), Decimal(4)) == [1, 3]
    for weights in ([1, 1, 1], [2, 1, 2], [0, 3, 1], [1, 0, 0, 2]):
        w = [Decimal(x) for x in weights]
        whole = sum(w)
        for amount in range(int(whole) + 1):
            shares = pro_rata(w, whole, Decimal(amount))
            assert sum(shares) == amount and all(0 <= s <= x for s, x in zip(shares, w))


def test_a_vertical_fill_books_each_leg() -> None:
    q = _queued(instrument=SPREAD, qty="2", allocations=(_alloc("sp", "OPT_PUT_SPREAD", "2"),))
    mirror = _fold(q, _ack(), _fill(filled="2", price="1.05"))
    assert dict(mirror.book) == {("OPT_PUT_SPREAD", P200): Decimal("-2"), ("OPT_PUT_SPREAD", P190): Decimal("2")}
    assert ticket_contracts(q, Decimal("1")) == {P200: Decimal("-1"), P190: Decimal("1")}
    half = _fold(q, _ack(), _fill(filled="1", price="1.05"))
    assert half.expected() == {P200: Decimal("-2"), P190: Decimal("2")}


def test_offsetting_fills_drop_the_book_entry() -> None:
    sell = _queued()
    buy = _queued("tos:b", side=Side.BUY, allocations=(_alloc("so-2"),))
    mirror = _fold(sell, buy, _ack(), _fill(), _ack("tos:b", oid="5400000002"), _fill("tos:b", oid="5400000002"))
    assert dict(mirror.book) == {}


def test_a_ticket_key_queued_twice_is_a_no_op_or_a_refusal() -> None:
    assert _fold(_queued(), _queued()) == _fold(_queued())
    _refuses(_queued(), _queued(qty="2", allocations=(_alloc(qty="2"),)), match="other contents")


def test_a_strategy_order_is_mirrored_at_most_once() -> None:
    _refuses(_queued(), _queued("tos:b"), match="refusing to mirror it twice")
    _refuses(_refused("so-1"), _queued(), match="was refused at this venue")
    _refuses(_queued(), _refused("so-1"), match="cannot also be refused")
    first = _fold(_refused("so-9", "first"), _refused("so-9", "second"))
    assert first.refused_orders == {"so-9": "first"} and first.handled("so-9")
    assert not first.handled("so-1")


def test_a_venue_account_folds_one_venue_only() -> None:
    # Event filing already keeps venues apart; the fold steps check it again (I8).
    from trade_engine.ledger import mirror as m

    other = MirrorQueued(**{**_queued("tos:x").__dict__, "venue": "D-00000002"})
    m.on_queued(_fold(), other)  # an empty state takes any venue

    with pytest.raises(m.MirrorFoldError, match="I8"):
        m.on_queued(_fold(_queued()), other)
    for step, payload in ((m.on_refused, MirrorRefused(**{**_refused().__dict__, "venue": "D-00000002"})),
                          (m.on_ack, MirrorAck(**{**_ack().__dict__, "venue": "D-00000002"})),
                          (m.on_fill, MirrorFill(**{**_fill().__dict__, "venue": "D-00000002"}))):
        with pytest.raises(m.MirrorFoldError, match="I8"):
            step(_fold(_queued(), _ack()), payload)


# -- with a real ledger ----------------------------------------------------------------


def test_the_ledger_refuses_a_contradicting_mirror_event_and_writes_nothing(tmp_path: Path) -> None:
    with Ledger(tmp_path / "l.db") as ledger:
        ledger.extend([_event(_queued()), _event(_ack()), _event(_fill())])
        count = ledger.count()
        with pytest.raises(LedgerFoldError, match="over-fill"):
            ledger.append(_event(_fill(filled="2")))
        assert ledger.count() == count
        ledger.verify_snapshot(ACCT)


def test_mirror_events_never_touch_sim_positions_or_cash(tmp_path: Path) -> None:
    with Ledger(tmp_path / "l.db") as ledger:
        ledger.append(Event(account="OPT_CSP", kind=EventKind.CASH_FLOW,
                            payload=CashFlow(amount=Decimal("10000"), kind="deposit", as_of=T),
                            ts_utc=T))
        before = ledger.state("OPT_CSP")
        ledger.extend([_event(_queued()), _event(_ack()), _event(_fill())])
        assert ledger.state("OPT_CSP") == before
        venue_state = ledger.state(ACCT)
        assert venue_state.cash == 0 and dict(venue_state.positions) == {}
        assert dict(venue_state.mirror.book) == {("OPT_CSP", P200): Decimal("-1")}
        assert before.mirror == MirrorState()


def test_an_old_ledger_without_mirror_events_still_loads(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    with Ledger(path) as ledger:
        ledger.append(Event(account="OPT_CSP", kind=EventKind.CASH_FLOW,
                            payload=CashFlow(amount=Decimal("5"), kind="deposit", as_of=T),
                            ts_utc=T))
        ledger.append(Event(account=ACCT, kind=EventKind.VENUE_RECONCILE,
                            payload=VenueReconcile(venue=VENUE, as_of=T, reconciled=True), ts_utc=T))
    with Ledger(path) as reopened:
        states = reopened.fold()
        assert states["OPT_CSP"].cash == Decimal("5")
        assert mirror_state(states, VENUE) == MirrorState()
        assert mirror_state(states, "D-00000009") == MirrorState()
        assert halted_venues(states) == frozenset()


def test_a_mirror_log_replays_to_the_same_state(tmp_path: Path) -> None:
    path = tmp_path / "l.db"
    with Ledger(path) as ledger:
        ledger.extend([_event(_queued(qty="2", allocations=(_alloc(qty="2"),))), _event(_ack()),
                       _event(_fill(filled="1")), _event(_refused())])
        live = ledger.state(ACCT)
    with Ledger(path) as reopened:
        assert reopened.state(ACCT) == live
        reopened.verify_snapshot(ACCT)


def test_an_ack_without_a_book_status_keeps_the_last_one_read() -> None:
    mirror = _fold(_queued(), _ack(book=OrderState.ACCEPTED), _ack(status="PENDING", oid=None, book=None))
    assert mirror.tickets["tos:a"].book_status is OrderState.ACCEPTED
    assert mirror.tickets["tos:a"].venue_order_id == "5400000001"


def test_exposure_is_the_book_plus_each_open_tickets_unfilled_part_per_account() -> None:
    q = _queued(qty="3", allocations=(_alloc("a", "OPT_CSP", "2"), _alloc("b", "OPT_PUT_SPREAD", "1")))
    assert _fold(q, _ack()).exposure() == {("OPT_CSP", P200): Decimal(-2), ("OPT_PUT_SPREAD", P200): Decimal(-1)}
    partly = _fold(q, _ack(), _fill(filled="1"))  # a gets the first contract
    assert partly.exposure() == {("OPT_CSP", P200): Decimal(-2), ("OPT_PUT_SPREAD", P200): Decimal(-1)}
    assert dict(partly.book) == {("OPT_CSP", P200): Decimal(-1)}
    closed = _fold(q, _ack(), _fill(filled="1"), _ack(book=OrderState.CANCELLED))
    assert closed.exposure() == {("OPT_CSP", P200): Decimal(-1)}


def test_exposure_of_a_vertical_is_per_leg_and_drops_zeros() -> None:
    q = _queued(instrument=SPREAD, qty="1", allocations=(_alloc("sp", "OPT_PUT_SPREAD", "1"),))
    assert _fold(q, _ack()).exposure() == {("OPT_PUT_SPREAD", P200): Decimal(-1), ("OPT_PUT_SPREAD", P190): Decimal(1)}
    buy_back = _queued("tos:b", side=Side.BUY, allocations=(_alloc("so-2", "OPT_PUT_SPREAD", "1"),))
    both = _fold(q, buy_back, _ack(), _ack("tos:b", oid="5400000002"))
    assert both.exposure() == {("OPT_PUT_SPREAD", P190): Decimal(1)}

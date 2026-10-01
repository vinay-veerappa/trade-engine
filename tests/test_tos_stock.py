"""S1a: a share of stock is a legal mirror instrument (covered call / PMCC, docs TOS_STOCK_AND_LEAPS_MIRROR D1, D2).

The mirror used to refuse every ``Equity`` order and could not read a share row, so the first assignment
(or a covered call's 100 shares) would have made the reconcile lie. This file pins the whole path for a
stock: the ticket, the venue's own row shape, the ledger event, the netting screen, the send and read-back,
and the reconcile. A stock row is marked ``"kind": "stock"``; an option row is unmarked and must name an OCC
symbol, so a row can never be mistaken for the other kind (I5). Every guard has a firing and a non-firing
test (§0.2).
"""

import sys
from dataclasses import FrozenInstanceError
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pytest
from test_tos_mirror import MORNING, Clock, Venue, _kinds, _submit, ledger  # noqa: F401

from trade_engine.domain.instruments import Combo, ComboLeg, Equity, Instrument, OptionContract, Side
from trade_engine.domain.orders import Order, OrderState, OrderType, TimeInForce
from trade_engine.interfaces.broker import UnsupportedCapability, VenueOrder, VenueOrderAllocation
from trade_engine.ledger import codec
from trade_engine.ledger.events import (
    Event,
    EventKind,
    EventPayloadError,
    MirrorAck,
    MirrorAllocation,
    MirrorFill,
    MirrorQueued,
    mirror_account,
)
from trade_engine.ledger.mirror import ticket_contracts
from trade_engine.ledger.state import fold, mirror_state
from trade_engine.tos_paper import normalize as norm
from trade_engine.tos_paper.broker import MirrorBinding, TosPaperBroker
from trade_engine.tos_paper.netting import net_strategy_orders
from trade_engine.tos_paper.reconcile import confirm_ticket, reconcile
from trade_engine.tos_paper.session import mirror_of, run_mirror
from trade_engine.tos_paper.transport import MirrorStockTicket, MirrorTicket, ticket_for

T = datetime(2026, 9, 24, 20, 0, tzinfo=timezone.utc)
PM_A = "D-00000001"
ACCT = mirror_account(PM_A)
AAPL = Equity("AAPL")
C210 = OptionContract(underlying="AAPL", expiry=date(2026, 10, 16), strike=Decimal("210"), right="C")
MIRRORED = ("OPT_COVERED_CALL", "OPT_PMCC", "OPT_CSP")


def _order(oid="so-1", account="OPT_COVERED_CALL", side=Side.BUY, qty="100", *, instrument=AAPL,
           order_type=OrderType.LIMIT, limit="150.25", tif=TimeInForce.DAY) -> Order:
    return Order(
        order_id=oid, account_id=account, instrument=instrument, order_type=order_type, side=side,
        quantity=Decimal(qty), command_id=oid, created_at=T,
        limit_price=Decimal(limit) if limit is not None else None, tif=tif,
    )


def _venue_order(instrument=AAPL, side=Side.BUY, qty="100", *, order_type=OrderType.LIMIT, limit="150.25",
                 tif=TimeInForce.DAY) -> VenueOrder:
    return VenueOrder(
        venue_order_id="tos:stock", instrument=instrument, order_type=order_type, side=side,
        quantity=Decimal(qty), submitted_at=T, tif=tif,
        limit_price=None if limit is None else Decimal(limit),
        allocations=(VenueOrderAllocation("so-1", "OPT_COVERED_CALL", Decimal(qty)),),
    )


# -- the ticket (D1) ------------------------------------------------------------------------


def _ticket(**changes) -> MirrorStockTicket:
    fields = dict(symbol="AAPL", side="BUY", quantity=100, order_type="LMT", limit_price=Decimal("150.25"), tif="DAY")
    fields.update(changes)
    return MirrorStockTicket(**fields)


def test_a_valid_stock_ticket_constructs_and_cannot_be_edited_afterwards() -> None:
    """The non-firing control for the guards below: a good ticket is accepted, MKT and LMT, and is frozen."""
    assert _ticket().quantity == 100 and _ticket().limit_price == Decimal("150.25")
    market = _ticket(order_type="MKT", limit_price=None, side="SELL", tif="GTC")
    assert (market.order_type, market.limit_price, market.side, market.tif) == ("MKT", None, "SELL", "GTC")
    with pytest.raises(FrozenInstanceError):
        market.quantity = 1  # type: ignore[misc]


@pytest.mark.parametrize(
    "changes,match",
    [
        (dict(symbol="AAPL  261016C00210000"), "stock ticket symbol must be an upper-case equity symbol"),
        (dict(symbol=""), "stock ticket symbol must be an upper-case equity symbol"),
        (dict(symbol="aapl"), "stock ticket symbol must be an upper-case equity symbol"),
        (dict(side="HOLD"), "ticket side must be BUY or SELL"),
        (dict(quantity=0), "ticket quantity must be a positive int"),
        (dict(quantity=-100), "ticket quantity must be a positive int"),
        (dict(quantity=True), "ticket quantity must be a positive int"),
        (dict(quantity=1.5), "ticket quantity must be a positive int"),
        (dict(order_type="STP"), "ticket order_type must be MKT or LMT"),
        (dict(limit_price=None), "an LMT ticket needs a positive limit_price"),
        (dict(limit_price=Decimal("0")), "an LMT ticket needs a positive limit_price"),
        (dict(order_type="MKT"), "a MKT ticket cannot carry a limit_price"),
        (dict(tif="IOC"), "ticket tif must be DAY or GTC"),
    ],
)
def test_a_stock_ticket_the_venue_could_misread_is_refused_at_construction(changes, match) -> None:
    with pytest.raises(ValueError, match=match):
        _ticket(**changes)


def test_an_equity_order_becomes_a_stock_ticket_exactly() -> None:
    ticket = ticket_for(_venue_order())
    assert ticket == _ticket()
    sell = ticket_for(_venue_order(side=Side.SELL, order_type=OrderType.MARKET, limit=None, tif=TimeInForce.GTC, qty="250"))
    assert sell == _ticket(side="SELL", quantity=250, order_type="MKT", limit_price=None, tif="GTC")


def test_an_option_order_is_still_an_option_ticket() -> None:
    ticket = ticket_for(_venue_order(instrument=C210, qty="1", limit="2.00"))
    assert isinstance(ticket, MirrorTicket) and not isinstance(ticket, MirrorStockTicket)


def _stop_venue_order() -> VenueOrder:
    return VenueOrder(
        venue_order_id="tos:stock", instrument=AAPL, order_type=OrderType.STOP, side=Side.SELL,
        quantity=Decimal("100"), submitted_at=T, tif=TimeInForce.DAY, stop_price=Decimal("140"),
        allocations=(VenueOrderAllocation("so-1", "OPT_COVERED_CALL", Decimal("100")),),
    )


@pytest.mark.parametrize(
    "order,match",
    [
        (_venue_order(qty="1.5"), "whole number of shares"),
        (_venue_order(tif=TimeInForce.GTD), "TIF"),
        (_stop_venue_order(), "order type"),
    ],
)
def test_a_stock_order_the_venue_cannot_express_is_unsupported_never_approximated(order, match) -> None:
    with pytest.raises(UnsupportedCapability, match=match):
        ticket_for(order)


# -- the venue's rows (D2) ------------------------------------------------------------------


def _pos(**changes) -> dict:
    row = {"kind": "stock", "symbol": "AAPL", "quantity": "100", "avg_price": "150.25"}
    row.update(changes)
    return row


def test_a_stock_position_row_is_a_signed_share_count() -> None:
    long = norm.normalize_position(_pos(), T)
    assert (long.instrument, long.quantity, long.avg_price, long.as_of) == (AAPL, Decimal("100"), Decimal("150.25"), T)
    short = norm.normalize_position(_pos(quantity="-100"), T)  # an assignment against a call with no shares behind it
    assert short.instrument == AAPL and short.quantity == Decimal("-100")


def test_a_zero_average_price_and_a_zero_quantity_are_legal_stock_rows() -> None:
    """The reconcile reads shares, never their price; a closed row (0 shares) is not an error."""
    assert norm.normalize_position(_pos(avg_price="0"), T).avg_price == Decimal("0")
    closed = norm.normalize_position(_pos(quantity="0"), T)
    assert closed.quantity == Decimal("0")
    assert reconcile(PM_A, T, {}, [closed], []).reconciled


def test_an_option_position_row_is_unchanged_and_needs_no_marker() -> None:
    row = norm.normalize_position({"symbol": C210.to_occ(), "quantity": "-1", "avg_price": "2.10"}, T)
    assert row.instrument == C210 and row.quantity == Decimal("-1")


@pytest.mark.parametrize(
    "raw,match",
    [
        ({"symbol": "AAPL", "quantity": "100", "avg_price": "150.25"}, "not a mirrored option symbol"),  # no marker, no OCC
        (_pos(symbol=C210.to_occ()), "not a mirrored stock symbol"),  # marked stock but names an option
        (_pos(kind="bond"), "neither 'stock' nor 'option'"),
        (_pos(kind=None), "neither 'stock' nor 'option'"),
        (_pos(quantity="0.5"), "not a whole number of shares"),
        (_pos(quantity="x"), "not a number"),
        (_pos(avg_price=None), "avg_price must be a decimal string"),
        (_pos(avg_price="-1"), "stock avg_price must not be negative"),
        (_pos(symbol=""), "not a mirrored stock symbol"),
    ],
)
def test_a_stock_row_that_cannot_be_read_without_guessing_raises(raw, match) -> None:
    with pytest.raises(norm.NormalizeError, match=match):
        norm.normalize_position(raw, T)


def _row(**changes) -> dict:
    row = {"kind": "stock", "symbol": "AAPL", "side": "BUY", "quantity": "100", "filled": "0",
           "order_type": "LMT", "limit_price": "150.25", "status": "WORKING"}
    row.update(changes)
    return row


def test_a_stock_working_order_row_reads_back_as_an_equity_order() -> None:
    assert norm.normalize_working_order(_row()) == norm.WorkingOrder(
        AAPL, Side.BUY, Decimal("100"), Decimal("0"), OrderType.LIMIT, Decimal("150.25"), OrderState.ACCEPTED)
    partial = norm.normalize_working_order(_row(filled="40", status="PARTIAL"))
    assert partial.remaining == Decimal("60") and partial.state is OrderState.PARTIALLY_FILLED


@pytest.mark.parametrize(
    "changes,match",
    [
        (dict(kind=None), "neither 'stock' nor 'option'"),
        (dict(symbol="AAPL  261016C00210000"), "not a mirrored stock symbol"),
        (dict(quantity="0.5", filled="0"), "stock quantity 0.5 is not a whole number of shares"),
        (dict(filled="0.5"), "stock filled 0.5 is not a whole number of shares"),
        (dict(side="HOLD"), "working order side"),
    ],
)
def test_a_stock_working_row_that_cannot_be_read_raises(changes, match) -> None:
    with pytest.raises(norm.NormalizeError, match=match):
        norm.normalize_working_order(_row(**changes))


def test_an_unmarked_bare_symbol_working_row_is_not_read_as_stock() -> None:
    raw = {k: v for k, v in _row().items() if k != "kind"}
    with pytest.raises(norm.NormalizeError, match="not a mirrored option symbol"):
        norm.normalize_working_order(raw)


# -- the ledger event and the mirror fold --------------------------------------------------


def _queued(instrument=AAPL, side=Side.BUY, qty="100", key="tos:s", **kw) -> MirrorQueued:
    fields = dict(
        venue=PM_A, ticket_key=key, instrument=instrument, side=side, quantity=Decimal(qty),
        order_type=OrderType.LIMIT, limit_price=Decimal("150.25"), tif=TimeInForce.DAY,
        allocations=(MirrorAllocation("so-1", "OPT_COVERED_CALL", Decimal(qty)),), at=T,
    )
    fields.update(kw)
    return MirrorQueued(**fields)


_KIND = {MirrorQueued: EventKind.MIRROR_QUEUED, MirrorAck: EventKind.MIRROR_ACK, MirrorFill: EventKind.MIRROR_FILL}


def _event(payload, seq=None) -> Event:
    return Event(account=ACCT, kind=_KIND[type(payload)], payload=payload, ts_utc=T, seq=seq)


def test_a_queued_stock_ticket_is_a_legal_event_and_survives_the_codec() -> None:
    queued = _queued()
    event = _event(queued, seq=1)
    assert codec.decode_event(codec.encode_event(event)).payload == queued


class _Bond(Instrument):
    @property
    def symbol(self) -> str:
        return "BOND"


def test_a_queued_ticket_still_refuses_what_is_neither_option_nor_stock() -> None:
    with pytest.raises(EventPayloadError, match="option contract, a vertical or an equity"):
        _queued(instrument=_Bond())
    with pytest.raises(EventPayloadError):
        _queued(instrument=Combo((ComboLeg(C210, 1, Side.SELL), ComboLeg(AAPL, 1, Side.BUY))))


def test_the_mirror_book_holds_shares_beside_options_on_one_underlying() -> None:
    fill = MirrorFill(venue=PM_A, ticket_key="tos:s", venue_order_id="5400000001", filled=Decimal("100"),
                      avg_price=Decimal("150.25"), at=T)
    short_call = _queued(C210, Side.SELL, "1", key="tos:c", limit_price=Decimal("2.00"),
                         allocations=(MirrorAllocation("so-2", "OPT_COVERED_CALL", Decimal("1")),))
    ack = MirrorAck(venue=PM_A, ticket_key="tos:s", status="ACCEPTED", message="read back", at=T,
                    venue_order_id="5400000001", book_status=OrderState.ACCEPTED)
    payloads = [_queued(), ack, fill, short_call]
    state = mirror_state(fold([_event(p, seq=i + 1) for i, p in enumerate(payloads)]), PM_A)
    assert state.book[("OPT_COVERED_CALL", AAPL)] == Decimal("100")
    assert state.expected() == {AAPL: Decimal("100"), C210: Decimal("-1")}  # the shares and the call never merge
    assert ticket_contracts(_queued(side=Side.SELL, qty="50", allocations=(MirrorAllocation("x", "OPT_CSP", Decimal("50")),)),
                            Decimal("50")) == {AAPL: Decimal("-50")}


# -- netting (D1: the screen lets a share through, with the option screen's own limits) ------


def _net(orders, holdings=None):
    return net_strategy_orders(orders, venue_account=PM_A, mirrored_accounts=MIRRORED, at=T, holdings=holdings)


def test_a_mirrored_account_may_buy_shares() -> None:
    batch = _net([_order()])
    assert batch.refused == ()
    (ticket,) = batch.venue_orders
    assert ticket.instrument == AAPL and ticket.side is Side.BUY and ticket.quantity == Decimal("100")
    assert ticket.limit_price == Decimal("150.25") and ticket.allocations[0].account_id == "OPT_COVERED_CALL"


def test_two_accounts_buying_the_same_shares_at_one_limit_net_into_one_ticket() -> None:
    batch = _net([_order("a", "OPT_COVERED_CALL", qty="100"), _order("b", "OPT_CSP", qty="200")])
    (ticket,) = batch.venue_orders
    assert ticket.quantity == Decimal("300") and len(ticket.allocations) == 2


def test_shares_and_a_call_on_the_same_name_are_two_tickets_not_one_conflict() -> None:
    batch = _net([_order("sh"), _order("call", side=Side.SELL, qty="1", instrument=C210, limit="2.00")])
    assert batch.refused == () and {vo.instrument for vo in batch.venue_orders} == {AAPL, C210}


def test_a_gtc_share_order_keeps_its_tif_and_never_nets_with_a_day_one() -> None:
    batch = _net([_order("day", tif=TimeInForce.DAY), _order("gtc", "OPT_CSP", tif=TimeInForce.GTC)])
    assert {(vo.tif, vo.quantity) for vo in batch.venue_orders} == {
        (TimeInForce.DAY, Decimal("100")), (TimeInForce.GTC, Decimal("100"))}


def test_an_account_the_venue_does_not_mirror_never_gets_shares() -> None:
    batch = _net([_order(account="OPT_0DTE_PCS_SPX")])
    assert "not mirrored" in dict(batch.refused)["so-1"] and batch.venue_orders == ()


def test_opposite_sides_on_the_same_shares_still_conflict() -> None:
    batch = _net([_order("buy", "OPT_COVERED_CALL"), _order("sell", "OPT_CSP", side=Side.SELL)])
    assert "conflict" in dict(batch.refused)["sell"] and len(batch.venue_orders) == 1


@pytest.mark.parametrize(
    "kwargs,match",
    [
        (dict(qty="1.5"), "whole number"),
        (dict(tif=TimeInForce.GTD), "TIF"),
    ],
)
def test_a_stock_order_the_screen_cannot_express_is_refused_with_its_reason(kwargs, match) -> None:
    batch = _net([_order(**kwargs)])
    assert match in dict(batch.refused)["so-1"] and batch.venue_orders == ()


def test_a_stock_stop_is_refused_never_approximated() -> None:
    stop = Order(order_id="so-1", account_id="OPT_COVERED_CALL", instrument=AAPL, order_type=OrderType.STOP,
                 side=Side.SELL, quantity=Decimal("100"), command_id="so-1", created_at=T, stop_price=Decimal("140"))
    assert "order type" in dict(_net([stop]).refused)["so-1"]


def test_shares_are_still_not_a_leg_of_a_vertical() -> None:
    legs = Combo((ComboLeg(C210, 1, Side.SELL), ComboLeg(AAPL, 1, Side.BUY)))
    batch = _net([_order("cc", instrument=legs, qty="1", side=Side.SELL)])
    assert "not a 2-leg" in dict(batch.refused)["cc"]


# -- the broker: send, read back, reconcile ------------------------------------------------------


class StockVenue(Venue):
    """The shared in-memory venue, with its stock rows marked as the host marks them."""

    @staticmethod
    def _marked(rows):
        return [{**row, "kind": "stock"} if " " not in row["symbol"] else row for row in rows]

    def read_working_orders(self):
        return self._marked(super().read_working_orders())

    def read_positions(self):
        return self._marked(super().read_positions())


def _binding() -> MirrorBinding:
    return MirrorBinding(venue_account=PM_A, account_type="margin", mirrored_accounts=MIRRORED,
                         minimum_balance=Decimal("100000"))


def _broker(venue=None, clock=None) -> tuple[TosPaperBroker, StockVenue]:
    venue = venue or StockVenue()
    broker = TosPaperBroker(venue, _binding(), clock=clock or Clock(), balance_unproven_ok=True)
    broker.connect()
    return broker, venue


def test_a_stock_order_is_sent_as_a_stock_ticket_and_read_back_off_the_book() -> None:
    broker, venue = _broker()
    broker.mirror_batch([_order()], holdings={})
    report = broker.drain()
    assert venue.placed == [_ticket()]
    assert [a.status for a in report.acks] == ["ACCEPTED"] and "on the order book" in report.acks[0].message
    assert report.reconcile.reconciled and not broker.halted


def test_submit_sends_a_stock_order_pending_until_read_back() -> None:
    broker, venue = _broker()
    ack = broker.submit(_venue_order())
    assert ack.status == "PENDING" and venue.placed == [_ticket()]


def test_a_filled_stock_order_is_proven_by_the_position_that_moved() -> None:
    broker, venue = _broker()
    broker.mirror_batch([_order()], holdings={})
    broker.drain()
    (oid,) = venue.orders
    venue.fill(oid, 100, "150.25")  # the book row now reads FILLED and the position is 100 shares
    assert broker.reconcile_now().reconciled


def test_shares_nobody_expected_are_drift_and_halt_the_venue() -> None:
    broker, venue = _broker()
    venue.positions["AAPL"] = Decimal("100")
    event = broker.reconcile_now()
    assert not event.reconciled and event.drift == ("AAPL",) and broker.halted


def test_a_short_share_row_is_read_not_hidden() -> None:
    broker, venue = _broker()
    venue.positions["AAPL"] = Decimal("-100")  # an assignment against a short call with no shares behind it
    event = broker.reconcile_now()
    assert not event.reconciled and event.drift == ("AAPL",)


def test_shares_and_an_option_on_one_name_reconcile_separately() -> None:
    held = [norm.normalize_position(_pos(), T),
            norm.normalize_position({"symbol": C210.to_occ(), "quantity": "-1", "avg_price": "2.00"}, T)]
    assert reconcile(PM_A, T, {AAPL: Decimal("100"), C210: Decimal("-1")}, held, []).reconciled
    off = reconcile(PM_A, T, {AAPL: Decimal("100"), C210: Decimal("-2")}, held, [])
    assert not off.reconciled and off.drift == (C210.symbol,)  # the shares are fine; only the call is off


def test_a_resting_share_order_is_expected_and_an_unreadable_status_is_drift() -> None:
    resting = norm.normalize_working_order(_row())
    assert reconcile(PM_A, T, {AAPL: Decimal("100")}, [], [resting]).reconciled
    assert not reconcile(PM_A, T, {}, [], [resting]).reconciled  # a resting order nobody expected
    unknown = norm.normalize_working_order(_row(status="WEIRD"))
    assert reconcile(PM_A, T, {AAPL: Decimal("100")}, [], [unknown]).drift == ("AAPL",)


def test_confirm_ticket_proves_a_stock_ticket_by_its_book_row_or_its_position() -> None:
    order = _venue_order()
    row = norm.normalize_working_order(_row())
    assert confirm_ticket(order, {}, [], [row], set())[0] == "ACCEPTED"
    moved = norm.normalize_position(_pos(), T)
    assert confirm_ticket(order, {}, [moved], [], set()) == ("ACCEPTED", "filled (position moved 100)")
    assert confirm_ticket(order, {}, [], [], set())[0] == "PENDING"
    other = norm.normalize_working_order(_row(quantity="50"))
    assert confirm_ticket(order, {}, [], [other], set())[0] == "PENDING"  # a different size is not this ticket


# -- the session: queue ahead, send, book the fill -----------------------------------------------


def test_run_mirror_books_a_stock_fill_into_the_mirror_book(ledger) -> None:
    order = _order()
    _submit(ledger, order)
    broker, venue = _broker(clock=MORNING)
    first = run_mirror(ledger, broker, [order], clock=MORNING)
    assert not first.halted and len(first.queued) == 1 and venue.placed == [_ticket()]
    (oid,) = venue.orders
    venue.fill(oid, 100, "150.25")
    second = run_mirror(ledger, broker, [order], clock=MORNING)
    assert not second.halted and second.reconcile.reconciled
    mirror = mirror_of(ledger, PM_A)
    assert mirror.book == {("OPT_COVERED_CALL", AAPL): Decimal("100")}
    assert mirror.expected() == {AAPL: Decimal("100")}
    assert _kinds(ledger).index("MirrorQueued") < _kinds(ledger).index("MirrorAck")


def test_a_partial_stock_fill_books_the_shares_filled_and_still_expects_the_rest(ledger) -> None:
    order = _order()
    _submit(ledger, order)
    broker, venue = _broker(clock=MORNING)
    run_mirror(ledger, broker, [order], clock=MORNING)
    (oid,) = venue.orders
    venue.fill(oid, 40, "150.25")
    report = run_mirror(ledger, broker, [order], clock=MORNING)
    assert not report.halted and report.reconcile.reconciled
    mirror = mirror_of(ledger, PM_A)
    assert mirror.book == {("OPT_COVERED_CALL", AAPL): Decimal("40")}
    assert mirror.expected() == {AAPL: Decimal("100")}  # 40 held + the 60 still resting


def test_running_the_same_stock_order_twice_sends_it_once(ledger) -> None:
    order = _order()
    _submit(ledger, order)
    broker, venue = _broker(clock=MORNING)
    run_mirror(ledger, broker, [order], clock=MORNING)
    again = run_mirror(ledger, broker, [order], clock=MORNING)
    assert len(venue.placed) == 1 and again.queued == () and not again.halted

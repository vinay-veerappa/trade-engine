"""The sim's exits mirrored at the venue at each in-session pass (T2, owner 2026-09-26).

Same in-memory paperMoney as ``test_tos_mirror``: no network, no Schwab endpoint. Every
guard has a firing and a non-firing test (§0.2).
"""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from test_tos_mirror import (  # noqa: F401  (ledger is a fixture)
    P190,
    P200,
    PM_A,
    SPREAD,
    Clock,
    Venue,
    _binding,
    _broker,
    _mirror,
    _order,
    ledger,
)
from trade_engine.domain.instruments import Combo, Side
from trade_engine.domain.orders import Order, OrderState, OrderType, TimeInForce
from trade_engine.domain.portfolio import Fill
from trade_engine.ledger import Ledger
from trade_engine.ledger.events import Event, EventKind, OrdersCreated, OrderUpdated, VenueReconcile, mirror_account
from trade_engine.tos_paper.exits import ExitPlanError, close_id, plan_exits, run_pass_mirror, target_id
from trade_engine.tos_paper.session import run_mirror

S = date(2026, 9, 28)
S_NEXT = date(2026, 9, 29)
OPEN = datetime(2026, 9, 28, 13, 30, tzinfo=timezone.utc)
MORNING = Clock(OPEN + timedelta(minutes=20))  # 09:50 ET
MIDDAY = Clock(OPEN + timedelta(hours=3, minutes=5))  # 12:35 ET
LATE = Clock(OPEN + timedelta(hours=6, minutes=20))  # 15:50 ET
D = Decimal


def _book(ledger: Ledger, order: Order, *, fill: str | None = None, at: datetime = MORNING.now) -> None:
    """The sim's order: created, submitted and, with ``fill``, filled at that price."""
    ledger.append(Event(account=order.account_id, kind=EventKind.ORDERS_CREATED,
                        payload=OrdersCreated(orders=(order,), fingerprint=order.order_id, reason="test"),
                        ts_utc=at, command_id=f"create:{order.order_id}"))
    submitted = order.transition_to(OrderState.SUBMITTED)
    ledger.append(Event(account=order.account_id, kind=EventKind.ORDER_UPDATED,
                        payload=OrderUpdated(order=submitted, reason="submit"),
                        ts_utc=at, command_id=f"submit:{order.order_id}"))
    if fill is not None:
        _fill(ledger, order, fill, at)


def _fill(ledger: Ledger, order: Order, price: str, at: datetime) -> None:
    """The order's whole fill; a combo's leg by leg, each at ``price``."""
    if isinstance(order.instrument, Combo):
        legs = [(str(i), leg.contract, leg.side, order.quantity * leg.ratio) for i, leg in enumerate(order.instrument.legs)]
    else:
        legs = [(None, order.instrument, order.side, order.quantity)]
    for leg_id, instrument, side, quantity in legs:
        ledger.append(Event(account=order.account_id, kind=EventKind.FILL,
                            payload=Fill(fill_id=f"f:{order.order_id}:{leg_id}", order_id=order.order_id,
                                         account_id=order.account_id, instrument=instrument, quantity=quantity,
                                         price=D(price), venue_env="sim", filled_at=at, side=side, leg_id=leg_id),
                            ts_utc=at, command_id=f"fill:{order.order_id}:{leg_id}"))


def _target(entry: Order, limit: str = "1.00", qty: str | None = None) -> Order:
    return Order(order_id=f"{entry.order_id}:target", account_id=entry.account_id, instrument=entry.instrument,
                 order_type=OrderType.LIMIT, side=Side.BUY if entry.side is Side.SELL else Side.SELL,
                 quantity=D(qty) if qty else entry.quantity, command_id=f"{entry.order_id}:target",
                 created_at=entry.created_at, limit_price=D(limit), tif=TimeInForce.GTC,
                 parent_order_id=entry.order_id)


def _price(value: str | None = "0.60"):
    seen: list = []

    def price(contract, side):
        seen.append((contract, side))
        return None if value is None else D(value)

    price.seen = seen  # type: ignore[attr-defined]
    return price


def _held_at_venue(ledger: Ledger, *entries: Order, venue: Venue | None = None, fill: bool = True) -> Venue:
    """Mirror ``entries`` (already in the sim) in the morning and let the venue fill them."""
    broker, venue = _broker(venue, clock=MORNING)
    run_mirror(ledger, broker, list(entries), clock=MORNING)
    if fill:
        for oid, order in list(venue.orders.items()):
            venue.fill(oid, order["ticket"].quantity, "2.00")
    return venue


def _csp(qty: str = "1", account: str = "OPT_CSP", oid: str = "csp-1", instrument=P200) -> Order:
    return _order(oid, account, qty=qty, created=MORNING.now, instrument=instrument)


def _plan(ledger, name="midday", price=None, clock=MIDDAY, session=S):
    return plan_exits(ledger, _binding(), session, name=name, price=price or _price(), at=clock.now_utc())


def _pass(ledger, venue, name="midday", price=None, clock=MIDDAY, entries=(), session=S):
    broker, _ = _broker(venue, clock=clock)
    return run_pass_mirror(ledger, broker, list(entries), session, name=name, price=price or _price(), clock=clock)


def _collect(ledger, venue, clock=MIDDAY):
    from trade_engine.tos_paper.session import collect_only

    return collect_only(ledger, _broker(venue, clock=clock)[0], clock=clock)


# -- the target rests at the venue while both books hold the position -----------------------


def test_the_sims_target_rests_at_the_venue_as_a_day_ticket(ledger) -> None:
    entry = _csp()
    _book(ledger, entry, fill="2.00")
    _book(ledger, _target(entry))
    venue = _held_at_venue(ledger, entry)
    _collect(ledger, venue)
    plan = _plan(ledger)
    [order] = plan.orders
    assert order.order_id == target_id(_target(entry), S) and plan.cancel == () and plan.refused == ()
    assert (order.side, order.quantity, order.limit_price, order.tif) == (Side.BUY, D(1), D("1.00"), TimeInForce.DAY)
    report = _pass(ledger, venue)
    assert len(report.queued) == 1 and len(venue.placed) == 2 and not report.halted
    assert report.drain_reconcile.reconciled


def test_a_resting_target_is_not_sent_again_and_the_pass_rerun_sends_nothing(ledger) -> None:
    entry = _csp()
    _book(ledger, entry, fill="2.00")
    _book(ledger, _target(entry))
    venue = _held_at_venue(ledger, entry)
    _pass(ledger, venue, "morning", clock=MORNING)
    count = ledger.count()
    assert _plan(ledger).orders == ()  # it rests (another pass)
    _pass(ledger, venue, "morning", clock=MORNING)  # the same pass again
    assert len(venue.placed) == 2 and ledger.count() - count <= 2  # at most the reconcile of each collect


def test_the_target_is_sent_again_the_next_session_after_it_expired(ledger) -> None:
    entry = _csp()
    _book(ledger, entry, fill="2.00")
    _book(ledger, _target(entry))
    venue = _held_at_venue(ledger, entry)
    _pass(ledger, venue, "morning", clock=MORNING)
    venue.end("5400000002", "EXPIRED")
    _collect(ledger, venue, LATE)
    tomorrow = Clock(MORNING.now + timedelta(days=1))
    [order] = _plan(ledger, "morning", clock=tomorrow, session=S_NEXT).orders
    assert order.order_id == target_id(_target(entry), S_NEXT)


def test_no_target_rests_for_a_position_the_venue_never_filled(ledger) -> None:
    entry = _csp()
    _book(ledger, entry, fill="2.00")
    _book(ledger, _target(entry))
    venue = _held_at_venue(ledger, entry, fill=False)
    _collect(ledger, venue)
    assert _plan(ledger) == type(_plan(ledger))()  # the book is empty: nothing to follow


def test_a_target_is_capped_at_what_the_venue_holds(ledger) -> None:
    entry = _csp("2")
    _book(ledger, entry, fill="2.00")
    _book(ledger, _target(entry))
    broker, venue = _broker(clock=MORNING)
    run_mirror(ledger, broker, [entry], clock=MORNING)
    venue.fill("5400000001", 1, "2.00")  # one of two
    venue.end("5400000001", "CANCELED")
    _collect(ledger, venue)
    [order] = _plan(ledger).orders
    assert order.quantity == D(1)  # the sim holds 2, the venue 1: no close, a target of 1


def test_only_a_working_gtc_limit_child_on_the_contract_is_a_target(ledger) -> None:
    entry = _csp()
    _book(ledger, entry, fill="2.00")
    day_child = Order(order_id="csp-1:close:1", account_id="OPT_CSP", instrument=P200, order_type=OrderType.LIMIT,
                      side=Side.BUY, quantity=D(1), command_id="c", created_at=MORNING.now, limit_price=D("1"),
                      tif=TimeInForce.DAY, parent_order_id="csp-1")
    _book(ledger, day_child)  # a close still working in the sim is not a target
    _book(ledger, Order(order_id="loose", account_id="OPT_CSP", instrument=P200, order_type=OrderType.LIMIT,
                        side=Side.BUY, quantity=D(1), command_id="loose", created_at=MORNING.now,
                        limit_price=D("1"), tif=TimeInForce.GTC))  # no parent: an entry, not a target
    venue = _held_at_venue(ledger, entry)
    _collect(ledger, venue)
    assert _plan(ledger).orders == ()


# -- the sim closed: the venue follows ---------------------------------------------------------


def _closed_in_sim(ledger, *, target_rests=True):
    """The sim sold 1 P200, the venue holds it too; then the sim's target filled at midday."""
    entry = _csp()
    target = _target(entry)
    _book(ledger, entry, fill="2.00")
    _book(ledger, target)
    venue = _held_at_venue(ledger, entry)
    if target_rests:
        _pass(ledger, venue, "morning", clock=MORNING)
    _fill(ledger, target, "1.00", MIDDAY.now)
    return venue


def test_a_position_the_sim_closed_is_closed_at_the_venue_after_its_target_is_cancelled(ledger) -> None:
    venue = _closed_in_sim(ledger)
    price = _price("0.95")
    report = _pass(ledger, venue, price=price)
    assert venue.orders["5400000002"]["status"] == "CANCELED"  # the resting target
    [close] = [q for q in report.queued]
    assert close.allocations[0].strategy_order_id == close_id("OPT_CSP", P200, S, "midday")
    assert (close.side, close.quantity, close.limit_price, close.tif) == (Side.BUY, D(1), D("0.95"), TimeInForce.DAY)
    assert price.seen == [(P200, Side.BUY)]
    assert report.drain_reconcile.reconciled and not report.halted
    venue.fill("5400000003", 1, "0.95")
    _collect(ledger, venue, LATE)
    assert _mirror(ledger).book == {} and _plan(ledger, "late", clock=LATE) == type(_plan(ledger))()


def test_an_unfilled_close_is_replaced_at_the_next_pass_on_its_quotes(ledger) -> None:
    venue = _closed_in_sim(ledger)
    _pass(ledger, venue, price=_price("0.95"))
    plan = _plan(ledger, "late", price=_price("0.80"), clock=LATE)
    assert plan.cancel == (_mirror(ledger).queued_orders[close_id("OPT_CSP", P200, S, "midday")],)
    [order] = plan.orders
    assert order.order_id == close_id("OPT_CSP", P200, S, "late") and order.limit_price == D("0.80")


def test_rerunning_a_pass_after_its_close_was_sent_changes_nothing(ledger) -> None:
    venue = _closed_in_sim(ledger)
    _pass(ledger, venue)
    placed = len(venue.placed)
    assert _plan(ledger) == type(_plan(ledger))()
    _pass(ledger, venue)
    assert len(venue.placed) == placed and venue.orders["5400000003"]["status"] == "WORKING"


def test_a_partly_closed_position_closes_only_the_difference(ledger) -> None:
    entry = _csp("2")
    _book(ledger, entry, fill="2.00")
    venue = _held_at_venue(ledger, entry)
    part = Order(order_id="csp-1:close:1", account_id="OPT_CSP", instrument=P200, order_type=OrderType.MARKET,
                 side=Side.BUY, quantity=D(1), command_id="part", created_at=MIDDAY.now, tif=TimeInForce.DAY,
                 parent_order_id="csp-1")
    _book(ledger, part, fill="1.10", at=MIDDAY.now)
    _collect(ledger, venue)
    [order] = _plan(ledger).orders
    assert (order.side, order.quantity) == (Side.BUY, D(1))


def test_a_long_the_sim_sold_is_sold_at_the_venue(ledger) -> None:
    entry = _order("leaps-1", side=Side.BUY, created=MORNING.now)
    _book(ledger, entry, fill="2.00")
    venue = _held_at_venue(ledger, entry)
    _book(ledger, Order(order_id="leaps-1:close:1", account_id="OPT_CSP", instrument=P200,
                        order_type=OrderType.MARKET, side=Side.SELL, quantity=D(1), command_id="x",
                        created_at=MIDDAY.now, parent_order_id="leaps-1"), fill="2.50", at=MIDDAY.now)
    _collect(ledger, venue)
    [order] = _plan(ledger).orders
    assert (order.side, order.quantity) == (Side.SELL, D(1))


def test_a_close_with_no_price_at_this_pass_is_refused_once_a_session(ledger) -> None:
    venue = _closed_in_sim(ledger)
    report = _pass(ledger, venue, price=_price(None))
    [refused] = report.refused
    assert "no price" in refused.reason and refused.strategy_account == "OPT_CSP"
    assert venue.orders["5400000002"]["status"] == "WORKING"  # nothing cancelled for a close never sent
    again = _pass(ledger, venue, "late", price=_price(None), clock=LATE)
    assert again.refused == ()  # the first refusal of the session stands
    assert len(_plan(ledger, "late", clock=LATE).orders) == 1  # a price later in the day still closes it


def test_a_vertical_leg_is_refused_with_the_reason(ledger) -> None:
    entry = _order("sp-1", "OPT_PUT_SPREAD", instrument=SPREAD, limit="1.05", created=MORNING.now)
    _book(ledger, entry, fill="1.05")
    venue = _held_at_venue(ledger, entry)
    _collect(ledger, venue)
    assert _plan(ledger).orders == ()  # both books hold it: nothing
    close = Order(order_id="sp-1:close:1", account_id="OPT_PUT_SPREAD", instrument=SPREAD.legs[0].contract,
                  order_type=OrderType.MARKET, side=Side.BUY, quantity=D(1), command_id="y", created_at=MIDDAY.now,
                  parent_order_id="sp-1")
    _book(ledger, close, fill="1.50", at=MIDDAY.now)
    plan = _plan(ledger)
    assert plan.orders == () and [r[1] for r in plan.refused] == ["OPT_PUT_SPREAD"]
    assert "vertical" in plan.refused[0][2]


def test_a_resting_ticket_shared_with_another_account_is_refused(ledger) -> None:
    csp = _csp()
    other = _order("sp-1", "OPT_PUT_SPREAD", created=MORNING.now)  # the same contract, same side
    _book(ledger, csp, fill="2.00")
    _book(ledger, other, fill="2.00")
    broker, venue = _broker(clock=MORNING)
    run_mirror(ledger, broker, [csp, other], clock=MORNING)  # one netted ticket of 2
    venue.fill("5400000001", 1, "2.00")  # one of two: the ticket still rests
    _collect(ledger, venue)
    _book(ledger, Order(order_id="csp-1:close:1", account_id="OPT_CSP", instrument=P200,
                        order_type=OrderType.MARKET, side=Side.BUY, quantity=D(1), command_id="z",
                        created_at=MIDDAY.now, parent_order_id="csp-1"), fill="1.00", at=MIDDAY.now)
    plan = _plan(ledger)
    booked = dict(_mirror(ledger).book)
    closing = [r for r in plan.refused if "shared" in r[2]]
    assert booked and closing and plan.cancel == ()


def test_a_cancel_the_venue_did_not_confirm_blocks_the_close(ledger) -> None:
    venue = _closed_in_sim(ledger)
    venue.cancel_order = lambda order_id: {"status": "UNKNOWN", "order_id": order_id}
    report = _pass(ledger, venue)
    assert report.queued == () and len(venue.placed) == 2
    [refused] = report.refused
    assert "was not cancelled" in refused.reason
    assert _mirror(ledger).handled(close_id("OPT_CSP", P200, S, "midday"))


def test_a_halted_venue_cancels_nothing_and_refuses_the_exits(ledger) -> None:
    venue = _closed_in_sim(ledger)
    ledger.append(Event(account=mirror_account(PM_A), kind=EventKind.VENUE_RECONCILE,
                        payload=VenueReconcile(venue=PM_A, as_of=MIDDAY.now, reconciled=False, drift=("X",)),
                        ts_utc=MIDDAY.now))
    report = _pass(ledger, venue)
    assert report.halted and report.queued == ()
    assert venue.orders["5400000002"]["status"] == "WORKING"
    assert [r.strategy_order_id for r in report.refused] == [close_id("OPT_CSP", P200, S, "midday")]


def test_an_unknown_pass_refuses(ledger) -> None:
    with pytest.raises(ExitPlanError, match="Unknown pass"):
        _plan(ledger, "evening")


def test_an_entry_the_sim_still_holds_more_of_is_not_chased(ledger) -> None:
    entry = _csp("2")
    _book(ledger, entry, fill="2.00")
    broker, venue = _broker(clock=MORNING)
    run_mirror(ledger, broker, [entry], clock=MORNING)
    venue.fill("5400000001", 1, "2.00")
    venue.end("5400000001", "CANCELED")
    _collect(ledger, venue)
    assert _plan(ledger) == type(_plan(ledger))()  # no target in the sim, and nothing to close


def test_the_passes_entries_and_exits_go_in_one_batch(ledger) -> None:
    venue = _closed_in_sim(ledger)
    new = _order("csp-2", instrument=P190, created=MIDDAY.now)
    _book(ledger, new, fill="1.50", at=MIDDAY.now)
    report = _pass(ledger, venue, entries=[new])
    assert sorted(a.strategy_order_id for q in report.queued for a in q.allocations) == sorted(
        ["csp-2", close_id("OPT_CSP", P200, S, "midday")]
    )


# -- the filters, each one alone -------------------------------------------------------------


def _held_with(ledger, *children: Order) -> None:
    entry = _csp()
    _book(ledger, entry, fill="2.00")
    for child in children:
        _book(ledger, child)
    _collect(ledger, _held_at_venue(ledger, entry))


def _child(oid: str = "csp-1:target", *, instrument=P200, side=Side.BUY, order_type=OrderType.LIMIT,
           tif=TimeInForce.GTC, parent: str | None = "csp-1") -> Order:
    return Order(order_id=oid, account_id="OPT_CSP", instrument=instrument, order_type=order_type, side=side,
                 quantity=D(1), command_id=oid, created_at=MORNING.now,
                 limit_price=D("1.00") if order_type is OrderType.LIMIT else None, tif=tif, parent_order_id=parent)


@pytest.mark.parametrize(
    "child",
    [
        _child(instrument=P190),  # another contract's
        _child(side=Side.SELL),  # it would add to the position
        _child(order_type=OrderType.MARKET),
        _child(tif=TimeInForce.DAY),
        _child(parent=None),
    ],
    ids=["contract", "side", "market", "day", "no-parent"],
)
def test_a_child_that_is_not_the_positions_target_rests_nothing(ledger, child) -> None:
    _held_with(ledger, child)
    assert _plan(ledger).orders == ()


def test_the_positions_target_rests(ledger) -> None:
    _held_with(ledger, _child())
    assert [o.order_id for o in _plan(ledger).orders] == [target_id(_child(), S)]


def test_a_cancelled_target_rests_nothing(ledger) -> None:
    from trade_engine.ledger.events import OrderStateChange

    _held_with(ledger, _child())
    ledger.append(Event(account="OPT_CSP", kind=EventKind.ORDER_CANCELLED,
                        payload=OrderStateChange("csp-1:target", "replaced by a close"), ts_utc=MIDDAY.now))
    assert _plan(ledger).orders == ()


def test_a_target_the_venue_refused_today_is_not_planned_again(ledger) -> None:
    from trade_engine.ledger.events import MirrorRefused

    _held_with(ledger, _child())
    oid = target_id(_child(), S)
    ledger.append(Event(account=mirror_account(PM_A), kind=EventKind.MIRROR_REFUSED,
                        payload=MirrorRefused(venue=PM_A, strategy_order_id=oid, strategy_account="OPT_CSP",
                                              reason="test", at=MIDDAY.now), ts_utc=MIDDAY.now))
    assert _plan(ledger).orders == ()


def test_a_ticket_the_fold_still_holds_open_blocks_a_second_target(ledger) -> None:
    # Yesterday's DAY target expired at the venue, but no collect recorded it: the fold
    # still expects it, so today's is not sent over it.
    entry = _csp()
    _book(ledger, entry, fill="2.00")
    _book(ledger, _target(entry))
    venue = _held_at_venue(ledger, entry)
    _pass(ledger, venue, "morning", clock=MORNING)
    tomorrow = Clock(MORNING.now + timedelta(days=1))
    assert _plan(ledger, "morning", clock=tomorrow, session=S_NEXT).orders == ()


def test_a_position_the_sim_holds_on_the_other_side_is_closed_whole(ledger) -> None:
    entry = _order("leaps-1", side=Side.BUY, created=MORNING.now)  # the venue and the sim: long 1
    _book(ledger, entry, fill="2.00")
    venue = _held_at_venue(ledger, entry)
    _collect(ledger, venue)
    flip = Order(order_id="leaps-1:close:1", account_id="OPT_CSP", instrument=P200, order_type=OrderType.MARKET,
                 side=Side.SELL, quantity=D(2), command_id="flip", created_at=MIDDAY.now, parent_order_id="leaps-1")
    _book(ledger, flip, fill="2.50", at=MIDDAY.now)  # the sim is short 1 now
    [order] = _plan(ledger).orders
    assert (order.side, order.quantity) == (Side.SELL, D(1))  # out of the long; the short is an entry's


@pytest.mark.parametrize("value", ["0", "-0.05"])
def test_a_close_priced_at_nothing_is_refused(ledger, value) -> None:
    _closed_in_sim(ledger)
    plan = _plan(ledger, price=_price(value))
    assert plan.orders == () and "no price" in plan.refused[0][2]


def test_another_accounts_resting_ticket_is_not_this_accounts(ledger) -> None:
    venue = _closed_in_sim(ledger, target_rests=False)
    other = _order("sp-1", "OPT_PUT_SPREAD", created=MIDDAY.now)  # rests unfilled on the same contract
    _book(ledger, other)
    run_mirror(ledger, _broker(venue, clock=MIDDAY)[0], [other], clock=MIDDAY)
    plan = _plan(ledger)
    assert plan.cancel == () and plan.refused == ()
    assert [o.order_id for o in plan.orders] == [close_id("OPT_CSP", P200, S, "midday")]

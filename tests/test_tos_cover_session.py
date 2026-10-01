"""S1b-2: the cover rule at work in the session, the pass and the follower (docs TOS_STOCK_AND_LEAPS_MIRROR, "S1b design").

``tos_paper.cover`` says when an order must wait; this file pins what the mirror DOES with that answer:

- ``run_mirror`` gates every order through ``cover_reason``. An order that would wait is refused at once, with the cover that
  is missing, unless the caller said ``may_wait=True`` -- then it is held, recorded nowhere, and listed in
  ``MirrorRunReport.waiting``. The default is fail-closed, so a caller that never learned about waiting cannot send a
  naked call. Buys, puts and verticals are never gated.
- ``run_pass_mirror`` threads ``may_wait`` through; ``follow_cycle`` always holds (the follower is the one that waits).
- ``follow_entries`` refuses an entry aged past ``max_age`` that is STILL waiting with the cover's reason, not "late copy".
- ``plan_exits`` follows shares: shares the sim no longer holds are sold at the venue, and the same rule makes that sale
  wait while a short call rests on them (D7).

Every guard has a firing and a non-firing test (§0.2).
"""

import sys
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pytest
from test_tos_mirror import P200, Clock, _kinds, ledger  # noqa: F401  (ledger is a fixture)
from test_tos_mirror_exits import MORNING as DAY
from test_tos_mirror_exits import OPEN, S, _book, _price
from test_tos_stock import AAPL, C210, MIRRORED, PM_A, StockVenue, _binding, _broker
from test_tos_unavailable import Flaky

from trade_engine.domain.instruments import OptionContract, Side
from trade_engine.domain.orders import Order, OrderType, TimeInForce
from trade_engine.ledger import Ledger
from trade_engine.ledger.reader import LedgerReader
from trade_engine.tos_paper.exits import close_id, plan_exits, run_pass_mirror
from trade_engine.tos_paper.follow import SplitLedger, follow_cycle, follow_entries
from trade_engine.tos_paper.session import collect_only, mirror_of, run_mirror

D = Decimal
LEAPS = OptionContract(underlying="AAPL", expiry=date(2027, 11, 19), strike=D("180"), right="C")
CC = "OPT_COVERED_CALL"


def _o(oid, side, qty, instrument, limit, *, account=CC, created=None) -> Order:
    return Order(order_id=oid, account_id=account, instrument=instrument, order_type=OrderType.LIMIT, side=side,
                 quantity=D(qty), command_id=oid, created_at=created or DAY.now, limit_price=D(limit),
                 tif=TimeInForce.DAY)


def _shares(created=None) -> Order:
    return _o("cc-shares", Side.BUY, "100", AAPL, "150.25", created=created)


def _call(oid="cc-call", qty="1", created=None) -> Order:
    return _o(oid, Side.SELL, qty, C210, "2.00", created=created)


def _sent(ledger, venue, broker, *orders, clock=DAY, may_wait=False):
    """Send ``orders``, then let the venue fill every ticket it holds in full."""
    for order in orders:
        _book(ledger, order, fill=None)
    report = run_mirror(ledger, broker, list(orders), clock=clock, may_wait=may_wait)
    for oid, held in venue.orders.items():
        venue.fill(oid, held["ticket"].quantity, "2.00")
    return report


# -- run_mirror: a short call needs proven cover --------------------------------------------------


def test_a_short_call_with_no_cover_is_refused_at_once_by_default(ledger) -> None:
    call = _call()
    _book(ledger, call, fill=None)
    broker, venue = _broker(clock=DAY)
    report = run_mirror(ledger, broker, [call], clock=DAY)
    assert venue.placed == [] and report.queued == () and report.waiting == () and not report.halted
    [refused] = report.refused
    assert refused.strategy_order_id == "cc-call" and "uncovered" in refused.reason and "AAPL" in refused.reason
    assert mirror_of(ledger, PM_A).handled("cc-call")
    again = run_mirror(ledger, broker, [call], clock=DAY)  # I3: a refused order is never sent later
    assert again.refused == () and venue.placed == []


def test_a_short_call_waits_when_the_caller_allows_it_and_nothing_is_recorded(ledger) -> None:
    call = _call()
    _book(ledger, call, fill=None)
    broker, venue = _broker(clock=DAY)
    report = run_mirror(ledger, broker, [call], clock=DAY, may_wait=True)
    [(oid, reason)] = report.waiting
    assert oid == "cc-call" and "uncovered" in reason and "proven venue fills only" in reason
    assert report.refused == () and report.queued == () and venue.placed == [] and not report.halted
    assert not mirror_of(ledger, PM_A).handled("cc-call")
    assert "MirrorQueued" not in _kinds(ledger) and "MirrorRefused" not in _kinds(ledger)
    again = run_mirror(ledger, broker, [call], clock=DAY, may_wait=True)  # still held, still not sent
    assert [w[0] for w in again.waiting] == ["cc-call"] and venue.placed == []


def test_the_call_goes_in_the_run_after_its_shares_are_proven_filled(ledger) -> None:
    shares, call = _shares(), _call()
    _book(ledger, shares, fill=None)
    _book(ledger, call, fill=None)
    broker, venue = _broker(clock=DAY)
    first = run_mirror(ledger, broker, [shares, call], clock=DAY, may_wait=True)
    assert [q.instrument for q in first.queued] == [AAPL]  # the resting buy is not cover
    assert [w[0] for w in first.waiting] == ["cc-call"] and len(venue.placed) == 1
    (oid,) = venue.orders
    venue.fill(oid, 100, "150.25")
    second = run_mirror(ledger, broker, [shares, call], clock=DAY, may_wait=True)
    assert second.waiting == () and [q.instrument for q in second.queued] == [C210]
    assert len(venue.placed) == 2 and not second.halted


def test_a_partial_fill_of_the_shares_does_not_cover_the_call(ledger) -> None:
    shares, call = _shares(), _call()
    _book(ledger, shares, fill=None)
    _book(ledger, call, fill=None)
    broker, venue = _broker(clock=DAY)
    run_mirror(ledger, broker, [shares], clock=DAY)
    (oid,) = venue.orders
    venue.fill(oid, 40, "150.25")
    report = run_mirror(ledger, broker, [call], clock=DAY, may_wait=True)
    assert [w[0] for w in report.waiting] == ["cc-call"] and len(venue.placed) == 1


def test_one_lot_of_shares_lets_one_call_through_and_holds_the_second(ledger) -> None:
    shares, first, second = _shares(), _call("cc-call-1"), _call("cc-call-2")
    broker, venue = _broker(clock=DAY)
    _sent(ledger, venue, broker, shares)
    for order in (first, second):
        _book(ledger, order, fill=None)
    report = run_mirror(ledger, broker, [first, second], clock=DAY, may_wait=True)
    assert [q.allocations[0].strategy_order_id for q in report.queued] == ["cc-call-1"]
    assert [w[0] for w in report.waiting] == ["cc-call-2"] and len(venue.placed) == 2


@pytest.mark.parametrize("may_wait", [True, False])
def test_a_call_that_cannot_go_does_not_stop_the_orders_beside_it(ledger, may_wait) -> None:
    put = _o("csp-1", Side.SELL, "1", P200, "2.00", account="OPT_CSP")
    call = _call()
    _book(ledger, put, fill=None)
    _book(ledger, call, fill=None)
    broker, venue = _broker(clock=DAY)
    report = run_mirror(ledger, broker, [put, call], clock=DAY, may_wait=may_wait)
    assert [q.instrument for q in report.queued] == [P200] and len(venue.placed) == 1
    assert [w[0] for w in report.waiting] == (["cc-call"] if may_wait else [])
    assert [r.strategy_order_id for r in report.refused] == ([] if may_wait else ["cc-call"])


class FlakyStock(Flaky, StockVenue):
    """A venue that drops after the collect's one positions read, and marks its stock rows."""


def test_a_deferred_run_still_lists_the_orders_the_cover_rule_held_back(ledger) -> None:
    put = _o("csp-1", Side.SELL, "1", P200, "2.00", account="OPT_CSP")
    call = _call()
    _book(ledger, put, fill=None)
    _book(ledger, call, fill=None)
    venue = FlakyStock()
    broker, _ = _broker(venue, clock=DAY)
    venue.positions_allowed = 1  # the collect's reconcile reads positions once; the preflight is the read that fails
    report = run_mirror(ledger, broker, [put, call], clock=DAY, may_wait=True)
    assert report.deferred is not None and "dropped the session" in report.deferred
    assert [w[0] for w in report.waiting] == ["cc-call"]  # still the caller's to present again
    assert venue.placed == [] and report.queued == () and report.refused == () and not report.halted
    assert set(_kinds(ledger)) == {"VenueReconcile"}  # the collect's proven read, and not one Queued/Ack/Refused
    assert not mirror_of(ledger, PM_A).handled("cc-call") and not mirror_of(ledger, PM_A).handled("csp-1")


def test_a_put_and_a_bought_call_are_never_gated(ledger) -> None:
    """The non-firing control: only a short call (or shares / a long call a short call rests on) can wait."""
    put = _o("csp-1", Side.SELL, "1", P200, "2.00", account="OPT_CSP")
    long_call = _o("pm-long", Side.BUY, "1", LEAPS, "30.00", account="OPT_PMCC")
    _book(ledger, put, fill=None)
    _book(ledger, long_call, fill=None)
    broker, venue = _broker(clock=DAY)
    report = run_mirror(ledger, broker, [put, long_call], clock=DAY)  # default: no waiting allowed
    assert report.refused == () and report.waiting == () and len(report.queued) == 2 and len(venue.placed) == 2


@pytest.mark.parametrize(
    "long_strike,sent",
    [("180", True), ("210", True), ("220", False)],  # a debit diagonal (or equal strikes) covers; a credit one does not
)
def test_a_long_call_covers_a_short_call_only_as_a_debit_diagonal(ledger, long_strike, sent) -> None:
    long_call = _o("pm-long", Side.BUY, "1", OptionContract("AAPL", date(2027, 11, 19), D(long_strike), "C"), "30.00",
                   account="OPT_PMCC")
    short = _o("pm-short", Side.SELL, "1", C210, "2.00", account="OPT_PMCC")
    broker, venue = _broker(clock=DAY)
    _sent(ledger, venue, broker, long_call)
    _book(ledger, short, fill=None)
    report = run_mirror(ledger, broker, [short], clock=DAY)
    if sent:
        assert len(report.queued) == 1 and report.refused == () and len(venue.placed) == 2
    else:
        assert report.queued == () and "uncovered" in report.refused[0].reason and len(venue.placed) == 1


# -- run_mirror: a long leg is never sold while a short call needs it (D7) ----------------------


def _covered_at_the_venue(ledger):
    """100 shares and one short call, both filled at the venue and in the mirror book."""
    broker, venue = _broker(clock=DAY)
    _sent(ledger, venue, broker, _shares())
    _sent(ledger, venue, broker, _call())
    collect_only(ledger, broker, clock=DAY)
    assert mirror_of(ledger, PM_A).book == {(CC, AAPL): D(100), (CC, C210): D(-1)}
    return broker, venue


def test_selling_the_shares_is_refused_while_a_short_call_rests_on_them(ledger) -> None:
    broker, venue = _covered_at_the_venue(ledger)
    sale = _o("cc-sell", Side.SELL, "100", AAPL, "149.00")
    _book(ledger, sale, fill=None)
    report = run_mirror(ledger, broker, [sale], clock=DAY)
    assert report.queued == () and len(venue.placed) == 2
    assert "uncovered" in report.refused[0].reason and "AAPL" in report.refused[0].reason


def test_selling_the_shares_waits_while_a_short_call_rests_on_them_when_allowed(ledger) -> None:
    broker, venue = _covered_at_the_venue(ledger)
    sale = _o("cc-sell", Side.SELL, "100", AAPL, "149.00")
    _book(ledger, sale, fill=None)
    report = run_mirror(ledger, broker, [sale], clock=DAY, may_wait=True)
    assert [w[0] for w in report.waiting] == ["cc-sell"] and report.refused == () and len(venue.placed) == 2
    assert not mirror_of(ledger, PM_A).handled("cc-sell")


def test_selling_shares_nothing_rests_on_goes_through(ledger) -> None:
    """The non-firing control for the two tests above: no short call, no wait."""
    broker, venue = _broker(clock=DAY)
    _sent(ledger, venue, broker, _shares())
    sale = _o("cc-sell", Side.SELL, "100", AAPL, "149.00")
    _book(ledger, sale, fill=None)
    report = run_mirror(ledger, broker, [sale], clock=DAY)
    assert report.waiting == () and report.refused == () and len(report.queued) == 1 and len(venue.placed) == 2


# -- run_pass_mirror threads the bound ------------------------------------------------------------


def test_a_pass_refuses_a_call_with_no_cover_by_default_and_holds_it_when_allowed(ledger) -> None:
    call = _call()
    _book(ledger, call, fill=None)
    broker, venue = _broker(clock=DAY)
    refused = run_pass_mirror(ledger, broker, [call], S, name="follow-0950", price=_price(), clock=DAY)
    assert "uncovered" in refused.refused[0].reason and refused.waiting == () and venue.placed == []
    other = _call("cc-call-2")
    _book(ledger, other, fill=None)
    held = run_pass_mirror(ledger, broker, [other], S, name="follow-0951", price=_price(), clock=DAY, may_wait=True)
    assert [w[0] for w in held.waiting] == ["cc-call-2"] and held.refused == () and venue.placed == []


# -- plan_exits follows shares --------------------------------------------------------------------


def test_plan_exits_sells_the_shares_the_sim_no_longer_holds(ledger) -> None:
    broker, venue = _broker(clock=DAY)
    _sent(ledger, venue, broker, _shares(created=DAY.now), clock=DAY)
    collect_only(ledger, broker, clock=DAY)
    price = _price("149.00")
    plan = plan_exits(ledger, _binding(), S, name="follow-0950", price=price, at=DAY.now)
    [order] = plan.orders
    assert order.order_id == close_id(CC, AAPL, S, "follow-0950") and plan.refused == () and plan.cancel == ()
    assert (order.instrument, order.side, order.quantity) == (AAPL, Side.SELL, D(100))
    assert (order.order_type, order.limit_price, order.tif) == (OrderType.LIMIT, D("149.00"), TimeInForce.DAY)
    assert price.seen == [(AAPL, Side.SELL)]


def test_plan_exits_leaves_shares_the_sim_still_holds(ledger) -> None:
    """The non-firing control: the venue holds what the sim holds, so there is nothing to close."""
    shares = _shares(created=DAY.now)
    _book(ledger, shares, fill="150.25")
    broker, venue = _broker(clock=DAY)
    run_mirror(ledger, broker, [shares], clock=DAY)
    for oid, held in venue.orders.items():
        venue.fill(oid, held["ticket"].quantity, "150.25")
    collect_only(ledger, broker, clock=DAY)
    price = _price("149.00")
    plan = plan_exits(ledger, _binding(), S, name="follow-0950", price=price, at=DAY.now)
    assert plan.orders == () and plan.refused == () and price.seen == []


def test_the_share_close_waits_behind_the_calls_buy_back_then_goes(ledger) -> None:
    """D7 through the pass: the sim closed both legs; the venue buys the call back first, the shares go next pass."""
    broker, venue = _broker(clock=DAY)
    _sent(ledger, venue, broker, _shares(created=DAY.now), clock=DAY)
    _sent(ledger, venue, broker, _call(created=DAY.now), clock=DAY)
    collect_only(ledger, broker, clock=DAY)
    first = run_pass_mirror(ledger, broker, [], S, name="follow-0950", price=_price("1.00"), clock=DAY, may_wait=True)
    assert len(venue.placed) == 3 and venue.placed[2].side == "BUY"  # the call's buy-to-close, only
    [(waiting_id, reason)] = first.waiting
    assert waiting_id == close_id(CC, AAPL, S, "follow-0950") and "uncovered" in reason and first.refused == ()
    (oid,) = [key for key, held in venue.orders.items() if held["status"] == "WORKING"]
    venue.fill(oid, 1, "1.00")  # the buy-back fills
    later = Clock(DAY.now + timedelta(minutes=1))
    second = run_pass_mirror(ledger, broker, [], S, name="follow-0951", price=_price("1.00"), clock=later, may_wait=True)
    assert second.waiting == () and len(venue.placed) == 4
    assert venue.placed[3].symbol == "AAPL" and venue.placed[3].side == "SELL"


# -- the follower: it waits, and says so when the wait ran out ------------------------------------


@pytest.fixture()
def cc_books(tmp_path: Path):
    """(the sim's writer, the follower's split view, the follower's own ledger), for the covered-call accounts."""
    with Ledger(tmp_path / "sim.db") as sim, Ledger(tmp_path / "mirror.db") as own:
        reader = LedgerReader(tmp_path / "sim.db").open()
        try:
            yield sim, SplitLedger(reader, own, MIRRORED), own
        finally:
            reader.close()


def _cycle(split, venue, clock=DAY):
    broker, _ = _broker(venue, clock=clock)
    return follow_cycle(split, broker, S, session_open=OPEN, price=_price("1.40"), clock=clock)


def test_a_follow_cycle_holds_a_call_for_its_shares_and_sends_it_once_they_are_proven(cc_books) -> None:
    sim, split, own = cc_books
    _book(sim, _shares(created=DAY.now), fill="150.25")
    _book(sim, _call(created=DAY.now), fill="2.00")
    venue = StockVenue()
    first = _cycle(split, venue)
    assert [t.symbol for t in venue.placed] == ["AAPL"] and first.refused == ()
    assert [w[0] for w in first.waiting] == ["cc-call"] and not mirror_of(own, PM_A).handled("cc-call")
    (oid,) = venue.orders
    venue.fill(oid, 100, "150.25")
    second = _cycle(split, venue, Clock(DAY.now + timedelta(minutes=1)))
    assert second.waiting == () and second.refused == () and len(venue.placed) == 2
    assert venue.placed[1].side == "SELL" and mirror_of(own, PM_A).handled("cc-call")


def test_an_entry_still_waiting_past_max_age_is_refused_with_the_cover_reason(cc_books) -> None:
    sim, split, _ = cc_books
    _book(sim, _call(created=DAY.now), fill="2.00")
    entries, refused = follow_entries(split, _binding(), session_open=OPEN, now=DAY.now + timedelta(minutes=6))
    assert entries == ()
    [(oid, account, reason)] = refused
    assert (oid, account) == ("cc-call", CC)
    assert "uncovered" in reason and "did not arrive within 300s" in reason and "late copy" not in reason
    entries, refused = follow_entries(split, _binding(), session_open=OPEN, now=DAY.now + timedelta(minutes=4))
    assert len(entries) == 1 and refused == ()  # inside the bound it is still sent on (the session holds it)


def test_an_aged_call_whose_cover_is_proven_is_still_a_late_copy(cc_books) -> None:
    """The non-firing control: the cover is not what is missing, so the age is the reason."""
    sim, split, _ = cc_books
    _book(sim, _shares(created=DAY.now), fill="150.25")
    venue = StockVenue()
    _cycle(split, venue)
    (oid,) = venue.orders
    venue.fill(oid, 100, "150.25")
    collect_only(split, _broker(venue, clock=DAY)[0], clock=DAY)  # the follower's book now proves the shares
    _book(sim, _call(created=DAY.now), fill="2.00")
    entries, refused = follow_entries(split, _binding(), session_open=OPEN, now=DAY.now + timedelta(minutes=6))
    assert entries == () and [r[0] for r in refused] == ["cc-call"]
    assert "late copy" in refused[0][2] and "uncovered" not in refused[0][2]

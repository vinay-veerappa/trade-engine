"""A venue the mirror could not ASK is not a venue that refused, drifted or vanished.

``TransportUnavailable`` is the transport's word for "I could not reach or read the venue; nothing was sent".
The mirror then defers: it records nothing, halts nothing and sends nothing, and the orders stay pending for
the next run. A read that came back and was wrong (a bad row, a contradiction) is unchanged: that halts (I5).
Every guard has a firing and a non-firing test (§0.2).
"""

import sys
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pytest
from test_tos_broker import Balance, FakeVenue, _connected
from test_tos_broker import _binding as _broker_binding
from test_tos_broker import _order as _broker_order
from test_tos_mirror import MORNING, P190, Calendar, Clock, Venue, _binding, _broker, _kinds, _order, _submit, ledger  # noqa: F401

from trade_engine.domain.instruments import Side
from trade_engine.tos_paper.broker import TosPaperBroker
from trade_engine.tos_paper.exits import run_pass_mirror
from trade_engine.tos_paper.session import collect_only, pending_orders, run_mirror
from trade_engine.tos_paper.transport import TransportRefused, TransportReplay, TransportUnavailable

SESSION = date(2026, 9, 24)


class Flaky(Venue):
    """A venue whose reads can be unavailable: every read, or positions once ``positions_allowed`` have answered."""

    def __init__(self) -> None:
        super().__init__()
        self.down: str | None = None
        self.positions_allowed: int | None = None
        self.positions_then: Exception = TransportUnavailable("the gateway dropped the session")
        self.fills_down: str | None = None  # only the fill read is unavailable: positions still answer
        self.position_reads = 0

    def _ask(self) -> None:
        if self.down is not None:
            raise TransportUnavailable(self.down)

    def read_positions(self):
        self._ask()
        if self.positions_allowed is not None and self.position_reads >= self.positions_allowed:
            raise self.positions_then
        self.position_reads += 1
        return super().read_positions()

    def read_working_orders(self):
        self._ask()
        return super().read_working_orders()

    def read_order_fills(self):
        self._ask()
        if self.fills_down is not None:
            raise TransportUnavailable(self.fills_down)
        return super().read_order_fills()


def _pending(ledger):
    return pending_orders(ledger, _binding(), SESSION, calendar=Calendar())


def _written(ledger) -> set[str]:
    return set(_kinds(ledger))


# -- the type itself ----------------------------------------------------------------------


def test_unavailable_is_its_own_kind_not_a_refusal_or_a_replay() -> None:
    assert issubclass(TransportUnavailable, Exception)
    assert not issubclass(TransportUnavailable, (TransportRefused, TransportReplay))
    assert not issubclass(TransportRefused, TransportUnavailable)


# -- the broker: preflight and drain -------------------------------------------------------


def test_preflight_raises_unavailable_and_leaves_the_queue_and_the_venue_alone() -> None:
    venue = FakeVenue()
    venue.read_raises = TransportUnavailable("gateway down")
    broker, _ = _connected(venue)
    broker.mirror_batch([_broker_order("a", "OPT_CSP", Side.SELL)], holdings={})
    with pytest.raises(TransportUnavailable, match="gateway down"):
        broker.preflight()
    assert len(broker.queued) == 1 and venue.placed == [] and not broker.halted
    venue.read_raises = None  # it comes back: the same queue is sent once
    report = broker.drain()
    assert len(venue.placed) == 1 and [a.status for a in report.acks] == ["ACCEPTED"]


def test_preflight_lets_an_unreadable_row_be_the_drains_to_judge() -> None:
    venue = FakeVenue()
    venue.read_raises = RuntimeError("JAB tree gone")
    broker, _ = _connected(venue)
    broker.mirror_batch([_broker_order("a", "OPT_CSP", Side.SELL)], holdings={})
    broker.preflight()  # not the unavailable kind: no raise here
    report = broker.drain()  # the drain judges it exactly as before: sent nothing, refused, halted
    assert venue.placed == [] and report.acks[0].status == "REJECTED" and broker.halted


def test_the_drain_uses_the_preflight_read_instead_of_reading_again() -> None:
    venue = FakeVenue()
    broker, _ = _connected(venue)
    broker.mirror_batch([_broker_order("a", "OPT_CSP", Side.SELL)], holdings={})
    venue.calls.clear()
    broker.preflight()
    broker.drain()
    assert venue.calls[:2] == ["read_positions", "place"], venue.calls  # one read, then straight to the send


def test_a_preflight_read_is_used_by_one_drain_only() -> None:
    venue = FakeVenue()
    broker, _ = _connected(venue)
    broker.mirror_batch([_broker_order("a", "OPT_CSP", Side.SELL)], holdings={})
    broker.preflight()
    broker.drain()
    broker.mirror_batch([_broker_order("b", "OPT_CSP", Side.SELL)], holdings={})
    venue.calls.clear()
    broker.drain()  # a later batch reads the venue afresh
    assert venue.calls[0] == "read_positions"


def test_a_failed_preflight_leaves_no_earlier_read_behind_for_the_drain() -> None:
    venue = FakeVenue()
    broker, _ = _connected(venue)
    broker.mirror_batch([_broker_order("a", "OPT_CSP", Side.SELL)], holdings={})
    broker.preflight()  # a good read, never drained
    venue.read_raises = TransportUnavailable("gateway down")
    with pytest.raises(TransportUnavailable):
        broker.preflight()  # the second ask fails: the first read must not stand in for it
    venue.read_raises = None
    venue.calls.clear()
    broker.drain()
    assert venue.calls[0] == "read_positions"


def test_a_stale_preflight_read_is_not_used() -> None:
    clock = Clock()  # one whose time moves
    venue = FakeVenue()
    broker = TosPaperBroker(venue, _broker_binding(), clock=clock, balance_reader=Balance("150000"))
    broker.connect()
    broker.mirror_batch([_broker_order("a", "OPT_CSP", Side.SELL)], holdings={})
    broker.preflight()
    clock.now += timedelta(seconds=31)  # a write-ahead that took this long is not "just read"
    venue.calls.clear()
    broker.drain()
    assert venue.calls[0] == "read_positions"
    clock2 = Clock()
    venue2 = FakeVenue()
    fresh = TosPaperBroker(venue2, _broker_binding(), clock=clock2, balance_reader=Balance("150000"))
    fresh.connect()
    fresh.mirror_batch([_broker_order("a", "OPT_CSP", Side.SELL)], holdings={})
    fresh.preflight()
    clock2.now += timedelta(seconds=30)  # the bound itself is still fresh
    venue2.calls.clear()
    fresh.drain()
    assert venue2.calls[0] == "place"  # the bound itself is still fresh: the pre-send read is reused, not repeated


def test_a_drain_without_a_preflight_reads_as_before() -> None:
    venue = FakeVenue()
    broker, _ = _connected(venue)
    broker.mirror_batch([_broker_order("a", "OPT_CSP", Side.SELL)], holdings={})
    venue.calls.clear()
    broker.drain()
    assert venue.calls[:2] == ["read_positions", "place"]


# -- the session: a batch defers, nothing is recorded --------------------------------------


def test_an_unavailable_venue_defers_the_whole_batch_and_records_nothing(ledger) -> None:
    _submit(ledger, _order("csp-1"), _order("csp-2", instrument=P190))
    venue = Flaky()
    venue.down = "gateway down"
    broker, _ = _broker(venue, clock=MORNING)
    before = ledger.count()
    report = run_mirror(ledger, broker, _pending(ledger), clock=MORNING)
    assert report.deferred is not None and "gateway down" in report.deferred and "nothing was sent or recorded" in report.deferred
    assert venue.placed == [] and report.queued == () and report.acks == () and report.refused == ()
    assert not report.halted
    assert ledger.count() == before  # not one event: no Queued, no Ack, no Refused, no halting reconcile
    assert {o.order_id for o in _pending(ledger)} == {"csp-1", "csp-2"}  # both still the next run's to send


def test_a_venue_that_answers_the_collect_but_drops_before_the_send_defers_too(ledger) -> None:
    _submit(ledger, _order("csp-1"))
    venue = Flaky()
    broker, _ = _broker(venue, clock=MORNING)
    venue.positions_allowed = 1  # the collect's reconcile reads positions once; the preflight is the read that fails
    report = run_mirror(ledger, broker, _pending(ledger), clock=MORNING)
    assert report.deferred is not None and "dropped the session" in report.deferred
    assert venue.placed == [] and "MirrorQueued" not in _written(ledger) and not report.halted
    assert [o.order_id for o in _pending(ledger)] == ["csp-1"]


def test_a_deferred_collect_sends_nothing_even_when_the_venue_could_take_the_orders(ledger) -> None:
    _submit(ledger, _order("csp-1"))
    venue = Flaky()
    venue.fills_down = "fill table unreachable"  # positions and working orders still answer: a send would go through
    broker, _ = _broker(venue, clock=MORNING)
    report = run_mirror(ledger, broker, _pending(ledger), clock=MORNING)
    assert report.deferred is not None and "collecting fills" in report.deferred
    assert venue.placed == [] and "MirrorQueued" not in _written(ledger)  # no order over a venue whose fills are unread
    venue.fills_down = None
    assert len(run_mirror(ledger, broker, _pending(ledger), clock=MORNING).queued) == 1


def test_a_deferred_batch_is_sent_once_when_the_venue_is_back(ledger) -> None:
    _submit(ledger, _order("csp-1"))
    venue = Flaky()
    venue.down = "gateway down"
    broker, _ = _broker(venue, clock=MORNING)
    assert run_mirror(ledger, broker, _pending(ledger), clock=MORNING).deferred is not None
    venue.down = None
    second = run_mirror(ledger, broker, _pending(ledger), clock=MORNING)
    assert second.deferred is None and len(second.queued) == 1 and len(venue.placed) == 1
    third = run_mirror(ledger, broker, _pending(ledger), clock=MORNING)
    assert third.queued == () and len(venue.placed) == 1  # idempotent (I3)


def test_an_unavailable_collect_does_not_halt_the_venue(ledger) -> None:
    _submit(ledger, _order("csp-1"))
    venue = Flaky()
    broker, _ = _broker(venue, clock=MORNING)
    run_mirror(ledger, broker, _pending(ledger), clock=MORNING)  # one ticket is working on the venue
    count = ledger.count()
    venue.down = "gateway down"
    report = collect_only(ledger, _broker(venue, clock=MORNING)[0], clock=MORNING)
    assert report.deferred is not None and not report.halted and report.reconcile is None
    assert ledger.count() == count and "VenueReconcile" in _written(ledger)  # the earlier, clean one only


def test_a_collect_whose_closing_reconcile_cannot_ask_keeps_its_fills_and_defers(ledger) -> None:
    _submit(ledger, _order("csp-1"))
    venue = Flaky()
    broker, _ = _broker(venue, clock=MORNING)
    run_mirror(ledger, broker, _pending(ledger), clock=MORNING)
    oid = next(iter(venue.orders))
    venue.fill(oid, 1, "2.00")  # the venue filled it; the fill read still answers
    venue.positions_allowed = venue.position_reads  # the positions read, for the reconcile, no longer does
    report = collect_only(ledger, _broker(venue, clock=MORNING)[0], clock=MORNING)
    assert len(report.fills) == 1 and report.reconcile is None  # the fill is recorded, only the reconcile waits
    assert report.deferred is not None and "reconciling" in report.deferred and not report.halted
    assert "only the closing reconcile was not made" in report.deferred and "nothing was sent or recorded" not in report.deferred
    assert "MirrorFill" in _written(ledger)
    venue.positions_allowed = None  # back: the next collect reconciles and finds nothing left to book
    healed = collect_only(ledger, _broker(venue, clock=MORNING)[0], clock=MORNING)
    assert healed.deferred is None and healed.reconcile is not None and healed.reconcile.reconciled


def test_an_unreadable_venue_that_is_not_unavailable_still_halts(ledger) -> None:
    _submit(ledger, _order("csp-1"))
    venue = Flaky()
    broker, _ = _broker(venue, clock=MORNING)
    run_mirror(ledger, broker, _pending(ledger), clock=MORNING)
    venue.fill_read_raises = RuntimeError("fill table unreadable")
    report = collect_only(ledger, _broker(venue, clock=MORNING)[0], clock=MORNING)
    assert report.halted and report.deferred is None  # a read that came back wrong is drift, not absence


def test_a_closing_reconcile_that_reads_wrong_still_halts_rather_than_defers(ledger) -> None:
    _submit(ledger, _order("csp-1"))
    venue = Flaky()
    broker, _ = _broker(venue, clock=MORNING)
    run_mirror(ledger, broker, _pending(ledger), clock=MORNING)
    venue.positions_allowed = venue.position_reads
    venue.positions_then = RuntimeError("positions table unreadable")  # an answer that cannot be read, not no answer
    report = collect_only(ledger, _broker(venue, clock=MORNING)[0], clock=MORNING)
    assert report.halted and report.deferred is None and report.reconcile is not None


def test_a_halted_venue_is_refused_with_its_reason_not_deferred(ledger) -> None:
    _submit(ledger, _order("csp-1"))
    venue = Flaky()
    broker, _ = _broker(venue, clock=MORNING)
    run_mirror(ledger, broker, _pending(ledger), clock=MORNING)
    venue.fill_read_raises = RuntimeError("fill table unreadable")
    collect_only(ledger, _broker(venue, clock=MORNING)[0], clock=MORNING)  # now halted, on the record
    venue.fill_read_raises = None
    _submit(ledger, _order("csp-2"), session=None)
    report = run_mirror(ledger, _broker(venue, clock=MORNING)[0], [ledger.state("OPT_CSP").orders["csp-2"]], clock=MORNING)
    assert report.halted and report.deferred is None
    assert [r.strategy_order_id for r in report.refused] == ["csp-2"]  # the halt's own refusal, with its reason


# -- a pass at the venue (exits + entries) defers as one -----------------------------------


def test_a_pass_defers_before_it_cancels_or_refuses_anything(ledger) -> None:
    _submit(ledger, _order("csp-1"))
    venue = Flaky()
    venue.down = "gateway down"
    broker, _ = _broker(venue, clock=MORNING)
    before = ledger.count()
    report = run_pass_mirror(ledger, broker, _pending(ledger), SESSION, name="midday",
                             price=lambda contract, side: Decimal("1.00"), clock=MORNING,
                             express=lambda order: "no price on the venue's grid")  # a refusal it would record
    assert report.deferred is not None and "gateway down" in report.deferred
    assert venue.placed == [] and report.refused == () and not report.halted
    assert ledger.count() == before  # the express refusal was NOT written: it waits with everything else

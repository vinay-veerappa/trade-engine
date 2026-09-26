"""Lifting a venue's halt (§4.5): only on the operator's recorded word, after a clean reconcile.

A drifting reconcile halts its venue for good until ``clear_halt`` writes a
``VenueHaltCleared``. Every guard has a firing and a non-firing test (§0.2).
"""

import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pytest

from trade_engine.ledger import Ledger
from trade_engine.ledger.events import Event, EventKind, EventPayloadError, VenueHaltCleared, VenueReconcile
from trade_engine.ledger.state import LedgerFoldError, halted_venues
from trade_engine.tos_paper.session import (
    MirrorSessionError,
    clear_halt,
    collect_only,
    pending_orders,
    run_mirror,
)
from test_tos_mirror import (  # noqa: E402
    MORNING,
    PM_A,
    T,
    VENUE_ACCT,
    Calendar,
    Clock,
    Venue,
    _binding,
    _broker,
    _order,
    _submit,
)

PM_B = "D-00000002"


@pytest.fixture()
def ledger(tmp_path: Path):
    with Ledger(tmp_path / "ledger.db") as lg:
        yield lg


def _halted(ledger: Ledger) -> frozenset[str]:
    return halted_venues({a: ledger.state(a) for a in ledger.accounts()})


def _reconcile(ledger: Ledger, *, clean: bool, at=T, venue: str = PM_A, account: str = VENUE_ACCT) -> None:
    payload = VenueReconcile(venue=venue, as_of=at, reconciled=clean, drift=() if clean else ("<venue unreadable>",),
                             note=None if clean else "cannot read the venue")
    ledger.append(Event(account=account, kind=EventKind.VENUE_RECONCILE, payload=payload, ts_utc=at,
                        command_id=f"test:reconcile:{venue}:{account}:{at.isoformat()}:{clean}"))


def _unreadable_then_clean(ledger: Ledger) -> None:
    """What 2026-09-26 left: a read-back that failed, then one that matched."""
    venue = Venue()
    venue.fill_read_raises = RuntimeError("order_events has no orders list: {}")
    assert collect_only(ledger, _broker(venue, clock=MORNING)[0], clock=MORNING).halted
    later = Clock(MORNING.now + timedelta(minutes=3))
    assert collect_only(ledger, _broker(Venue(), clock=later)[0], clock=later).halted  # a halt outlives it
    assert PM_A in _halted(ledger)


def test_a_halt_outlives_a_clean_reconcile_until_cleared(ledger) -> None:
    _unreadable_then_clean(ledger)
    later = Clock(MORNING.now + timedelta(minutes=5))
    assert clear_halt(ledger, PM_A, reason="the web read-back of an empty book is fixed", clock=later) == (VENUE_ACCT,)
    assert PM_A not in _halted(ledger) and not ledger.state(VENUE_ACCT).venue_halted
    [cleared] = ledger.events_of_kind(EventKind.VENUE_HALT_CLEARED)
    assert cleared.payload.reason == "the web read-back of an empty book is fixed" and cleared.payload.at == later.now


def test_a_cleared_venue_sends_again(ledger) -> None:
    _unreadable_then_clean(ledger)
    clear_halt(ledger, PM_A, reason="fixed", clock=MORNING)
    _submit(ledger, _order("csp-1"))
    broker, venue = _broker(clock=MORNING)
    report = run_mirror(ledger, broker, pending_orders(ledger, _binding(), date(2026, 9, 24), calendar=Calendar()),
                        clock=MORNING)
    assert not report.halted and len(report.queued) == 1 and len(venue.placed) == 1


def test_a_halt_whose_newest_reconcile_still_drifts_is_not_cleared(ledger) -> None:
    _reconcile(ledger, clean=True, at=T)
    _reconcile(ledger, clean=False, at=T + timedelta(minutes=1))
    with pytest.raises(MirrorSessionError, match="still drifts"):
        clear_halt(ledger, PM_A, reason="fixed", clock=MORNING)
    assert PM_A in _halted(ledger) and ledger.events_of_kind(EventKind.VENUE_HALT_CLEARED) == []


def test_a_venue_that_is_not_halted_refuses(ledger) -> None:
    _reconcile(ledger, clean=True)
    with pytest.raises(MirrorSessionError, match="is not halted"):
        clear_halt(ledger, PM_A, reason="fixed", clock=MORNING)


@pytest.mark.parametrize("reason", ["", "   ", None])
def test_a_halt_is_not_lifted_without_a_reason(ledger, reason) -> None:
    _reconcile(ledger, clean=False)
    _reconcile(ledger, clean=True, at=T + timedelta(minutes=1))
    with pytest.raises(MirrorSessionError, match="reason"):
        clear_halt(ledger, PM_A, reason=reason, clock=MORNING)
    assert PM_A in _halted(ledger)


def test_clearing_twice_on_the_same_clean_reconcile_writes_nothing(ledger) -> None:
    _unreadable_then_clean(ledger)
    clear_halt(ledger, PM_A, reason="fixed", clock=MORNING)
    count = ledger.count()
    with pytest.raises(MirrorSessionError, match="is not halted"):
        clear_halt(ledger, PM_A, reason="fixed", clock=MORNING)
    assert ledger.count() == count


def test_a_new_drift_after_a_clear_halts_again(ledger) -> None:
    _unreadable_then_clean(ledger)
    clear_halt(ledger, PM_A, reason="fixed", clock=MORNING)
    _reconcile(ledger, clean=False, at=MORNING.now + timedelta(hours=1))
    assert PM_A in _halted(ledger)


def test_only_the_named_venue_is_cleared(ledger) -> None:
    _reconcile(ledger, clean=False, venue=PM_A)
    _reconcile(ledger, clean=False, venue=PM_B, account="__venue__:" + PM_B)
    _reconcile(ledger, clean=True, venue=PM_A, at=T + timedelta(minutes=1))
    clear_halt(ledger, PM_A, reason="fixed", clock=MORNING)
    assert _halted(ledger) == {PM_B}


def test_a_halt_recorded_under_another_account_is_cleared_there_too(ledger) -> None:
    _reconcile(ledger, clean=False, account="OPT_CSP")
    _reconcile(ledger, clean=True, at=T + timedelta(minutes=1))
    assert set(clear_halt(ledger, PM_A, reason="fixed", clock=MORNING)) == {"OPT_CSP"}
    assert PM_A not in _halted(ledger)


def test_one_venue_cleared_leaves_the_accounts_other_halt(ledger) -> None:
    _reconcile(ledger, clean=False, venue=PM_A)
    _reconcile(ledger, clean=False, venue=PM_B)
    _reconcile(ledger, clean=True, venue=PM_A, at=T + timedelta(minutes=1))
    clear_halt(ledger, PM_A, reason="fixed", clock=MORNING)
    assert ledger.state(VENUE_ACCT).halted_venues == {PM_B} and ledger.state(VENUE_ACCT).venue_halted


def test_the_fold_refuses_a_clear_for_a_venue_the_account_has_not_halted(ledger) -> None:
    with pytest.raises(LedgerFoldError, match="has not halted"):
        ledger.append(Event(account=VENUE_ACCT, kind=EventKind.VENUE_HALT_CLEARED,
                            payload=VenueHaltCleared(venue=PM_A, at=T, reason="x", reconcile_seq=1), ts_utc=T))


@pytest.mark.parametrize("kwargs,match", [
    (dict(venue=""), "venue"),
    (dict(reason=" "), "reason"),
    (dict(reconcile_seq=0), "seq"),
    (dict(reconcile_seq=True), "seq"),
    (dict(at=T.replace(tzinfo=None)), "at"),
])
def test_the_payload_refuses_what_it_cannot_record(kwargs, match) -> None:
    base = dict(venue=PM_A, at=T, reason="fixed", reconcile_seq=1)
    with pytest.raises(EventPayloadError, match=match):
        VenueHaltCleared(**{**base, **kwargs})


def test_the_clear_survives_a_reopen(tmp_path) -> None:
    path = tmp_path / "ledger.db"
    with Ledger(path) as lg:
        _unreadable_then_clean(lg)
        clear_halt(lg, PM_A, reason="fixed", clock=MORNING)
    with Ledger(path) as lg:
        assert PM_A not in _halted(lg)
        lg.verify_snapshot(VENUE_ACCT)


def test_another_venues_newer_drift_does_not_block_the_clear(ledger) -> None:
    _reconcile(ledger, clean=False, venue=PM_A)
    _reconcile(ledger, clean=True, venue=PM_A, at=T + timedelta(minutes=1))
    _reconcile(ledger, clean=False, venue=PM_B, account="__venue__:" + PM_B, at=T + timedelta(minutes=2))
    assert clear_halt(ledger, PM_A, reason="fixed", clock=MORNING) == (VENUE_ACCT,)
    assert _halted(ledger) == {PM_B}

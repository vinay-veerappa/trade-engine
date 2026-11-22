"""A venue mirror settles its expired contracts before it reconciles (PM-B, 2026-11-22).

10-13: a REJECTED close left two SPXW spreads on PM-B's mirror book. The contracts expired
and left the venue, the book kept them, and every reconcile after drifted and halted.
``collect_only(..., settle=(calendar, settlements))`` takes expired contracts out of the book
through the lifecycle pass first; a settlement not known yet is reported, never raised.
"""

import sys
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest

from trade_engine.calendar import get_calendar
from trade_engine.domain.instruments import Combo, ComboLeg, OptionContract, Side
from trade_engine.domain.option_roots import SettleTime
from trade_engine.ledger import Ledger
from trade_engine.lifecycle.sources import FixedSettlements, SettlementPrice
from trade_engine.tos_paper.expiry import settle_expired
from trade_engine.tos_paper.session import collect_only, pending_orders, run_mirror

from test_tos_mirror import (  # noqa: E402
    MORNING,
    P190,
    P200,
    PM_A,
    SPREAD,
    VENUE_ACCT,
    Calendar,
    Clock,
    _binding,
    _broker,
    _mirror,
    _order,
    _submit,
)

CAL = get_calendar()
EXPIRY = P200.expiry  # 2026-10-16, a Friday
AFTER = Clock(CAL.session_close(EXPIRY) + timedelta(hours=2))


@pytest.fixture()
def ledger(tmp_path: Path):
    with Ledger(tmp_path / "ledger.db") as lg:
        yield lg


S7770 = OptionContract(underlying="SPXW", expiry=EXPIRY, strike=Decimal("7770"), right="P")
S7775 = OptionContract(underlying="SPXW", expiry=EXPIRY, strike=Decimal("7775"), right="P")
SPX_SPREAD = Combo((ComboLeg(S7775, 1, Side.SELL), ComboLeg(S7770, 1, Side.BUY)))


def _settlements(price: str, underlying: str = "AAPL") -> FixedSettlements:
    close = CAL.session_close(EXPIRY)
    return FixedSettlements([SettlementPrice(underlying, EXPIRY, SettleTime.PM, Decimal(price), "official close",
                                             close + timedelta(minutes=70))])


def _booked_spx_spread(ledger: Ledger):
    """The real PM-B book: two cash-settled SPXW put spreads the venue no longer reports."""
    _submit(ledger, _order("sx-1", "OPT_PUT_SPREAD", qty="2", instrument=SPX_SPREAD, limit="1.05"))
    broker, venue = _broker(clock=MORNING)
    run_mirror(ledger, broker, pending_orders(ledger, _binding(), date(2026, 9, 24), calendar=Calendar()), clock=MORNING)
    venue.fill("5400000001", 2, "1.05")
    noon = Clock(MORNING.now + timedelta(hours=2))
    assert collect_only(ledger, _broker(venue, clock=noon)[0], clock=noon).reconcile.reconciled
    assert len(_mirror(ledger).book) == 2
    venue.positions.clear()
    return venue


def _booked_spread(ledger: Ledger):
    """Two AAPL put spreads filled at the venue, then expired off it."""
    _submit(ledger, _order("sp-1", "OPT_PUT_SPREAD", qty="2", instrument=SPREAD, limit="1.05"))
    broker, venue = _broker(clock=MORNING)
    run_mirror(ledger, broker, pending_orders(ledger, _binding(), date(2026, 9, 24), calendar=Calendar()), clock=MORNING)
    venue.fill("5400000001", 2, "1.05")
    noon = Clock(MORNING.now + timedelta(hours=2))
    assert collect_only(ledger, _broker(venue, clock=noon)[0], clock=noon).reconcile.reconciled
    assert dict(_mirror(ledger).book) == {("OPT_PUT_SPREAD", P200): Decimal(-2), ("OPT_PUT_SPREAD", P190): Decimal(2)}
    venue.positions.clear()  # expired: the venue no longer reports them
    return venue


def test_an_expired_book_without_settling_drifts_and_halts(ledger) -> None:
    venue = _booked_spread(ledger)
    report = collect_only(ledger, _broker(venue, clock=AFTER)[0], clock=AFTER)
    assert not report.reconcile.reconciled and report.halted


def test_expired_otm_contracts_leave_the_book_and_the_reconcile_is_clean(ledger) -> None:
    venue = _booked_spread(ledger)
    settle = (CAL, _settlements("210"))
    report = collect_only(ledger, _broker(venue, clock=AFTER)[0], clock=AFTER, settle=settle)
    assert report.unsettled == () and len(report.expired) == 2
    assert {e.account for e in report.expired} == {VENUE_ACCT}
    assert report.reconcile.reconciled and not report.halted
    assert dict(_mirror(ledger).book) == {}
    count = ledger.count()
    again = collect_only(ledger, _broker(venue, clock=AFTER)[0], clock=AFTER, settle=settle)
    assert again.expired == () and again.reconcile.reconciled and ledger.count() == count


@pytest.mark.parametrize("price", ["7800", "7772", "7700"])  # both OTM, short leg ITM, both ITM
def test_cash_settled_spxw_leaves_the_book_at_any_settlement(ledger, price) -> None:
    venue = _booked_spx_spread(ledger)
    settle = (CAL, _settlements(price, "SPX"))
    report = collect_only(ledger, _broker(venue, clock=AFTER)[0], clock=AFTER, settle=settle)
    assert report.unsettled == () and len(report.expired) == 2
    assert report.reconcile.reconciled and not report.halted
    assert dict(_mirror(ledger).book) == {}


@pytest.mark.parametrize("price", ["195", "185"])  # short leg ITM, both ITM: shares would be delivered
def test_a_delivered_exercise_is_reported_and_settles_nothing(ledger, price) -> None:
    venue = _booked_spread(ledger)
    report = collect_only(ledger, _broker(venue, clock=AFTER)[0], clock=AFTER, settle=(CAL, _settlements(price)))
    assert report.expired == () and len(report.unsettled) == 1
    assert report.unsettled[0].startswith("2026-10-16: LedgerFoldError")
    assert len(_mirror(ledger).book) == 2 and report.halted


def test_an_unknown_settlement_is_reported_and_settles_nothing(ledger) -> None:
    venue = _booked_spread(ledger)
    report = collect_only(ledger, _broker(venue, clock=AFTER)[0], clock=AFTER, settle=(CAL, FixedSettlements([])))
    assert report.expired == () and len(report.unsettled) == 1
    assert report.unsettled[0].startswith("2026-10-16: StaleDataError")
    assert len(_mirror(ledger).book) == 2


def test_nothing_settles_before_the_expiry_session_closes(ledger) -> None:
    _booked_spread(ledger)
    before = Clock(CAL.session_close(EXPIRY) - timedelta(hours=1))
    outcome = settle_expired(ledger, PM_A, before, CAL, _settlements("210"))
    assert outcome.events == () and outcome.unsettled == ()
    assert len(_mirror(ledger).book) == 2


def test_a_strategy_account_is_untouched_by_the_mirror_settle(ledger) -> None:
    _booked_spread(ledger)
    strategy = len(list(ledger.events(account="OPT_PUT_SPREAD")))
    settle_expired(ledger, PM_A, AFTER, CAL, _settlements("210"))
    assert len(list(ledger.events(account="OPT_PUT_SPREAD"))) == strategy

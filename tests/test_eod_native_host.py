"""W6-C3a: the EOD host is native-only. A book it cannot run natively refuses at construction.

There is no Python fallback any more, so the runner refuses a wrapped ledger, a non-XNYS
calendar or a closed ledger when it is built (``rs.eod_require_native``), not on its first run.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from test_eod_runner import ACCOUNT, CALENDAR, PREV_EOD, FakeMarketData, SettableClock

from trade_engine.calendar.sessions import ExchangeCalendar
from trade_engine.eod import EodRunner, EodRunnerConfig, EodRunnerError
from trade_engine.ledger import Ledger
from trade_engine.sim import SimBroker

REFUSAL_PLAIN = "EOD needs a plain Ledger on the XNYS ExchangeCalendar"
REFUSAL_CLOSED = "the ledger is closed"


class PassThrough:
    """A ledger behind a wrapper that is not a ``Ledger``: the runner must refuse it."""

    def __init__(self, ledger: Ledger) -> None:
        self._ledger = ledger

    def __getattr__(self, name: str):
        return getattr(self._ledger, name)


class OtherCalendar(ExchangeCalendar):
    """An ``ExchangeCalendar`` subclass, not the exact XNYS calendar: the runner must refuse it."""


def _construct(ledger, calendar) -> EodRunner:
    venue = SimBroker(ACCOUNT, SettableClock(PREV_EOD), Decimal("0"))
    venue.connect()
    return EodRunner(
        ledger, SettableClock(PREV_EOD), calendar, FakeMarketData(),
        EodRunnerConfig(job_name="eod", brokers={ACCOUNT: venue}),
    )


def test_a_wrapped_ledger_refuses_at_construction(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "book.db").open()
    try:
        with pytest.raises(EodRunnerError) as caught:
            _construct(PassThrough(ledger), CALENDAR)
        assert str(caught.value) == REFUSAL_PLAIN
    finally:
        ledger.close()


def test_a_non_xnys_calendar_refuses_at_construction(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "book.db").open()
    try:
        with pytest.raises(EodRunnerError) as caught:
            _construct(ledger, OtherCalendar())
        assert str(caught.value) == REFUSAL_PLAIN
    finally:
        ledger.close()


def test_a_closed_ledger_refuses_at_construction(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "book.db").open()
    ledger.close()
    with pytest.raises(EodRunnerError) as caught:
        _construct(ledger, CALENDAR)
    assert str(caught.value) == REFUSAL_CLOSED

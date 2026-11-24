"""Behaviour tests for the EOD runner and its routing helpers, asserting explicit values.

Replaces the lockstep oracle comparison in `test_p4a_parity.py`. Each test states the value the
production code must produce, and the scenarios come from the existing explicit-value suites
(`test_intraday_service.Rig`, `test_eod_options.csp`), not from a frozen copy of the old code.
"""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal as D
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from test_eod_options import csp
from test_intraday_service import SESSION, Rig, snap
from trade_engine.calendar.sessions import ExchangeCalendar
from trade_engine.domain.instruments import Equity
from trade_engine.domain.portfolio import Position
from trade_engine.eod.options_routing import OptionRouter, RoutingTally
from trade_engine.eod.runner import (
    EodRunner,
    EodRunnerConfig,
    EodRunnerError,
    ReplayDataError,
    SessionIncompleteError,
)
from trade_engine.interfaces.market_data import StaleDataError
from trade_engine.intraday.service import IntradayServiceError
from trade_engine.ledger.state import AccountState

AFTER = datetime(2026, 10, 16, 21, 45, tzinfo=timezone.utc)
NEW_YORK = ZoneInfo("America/New_York")
AAA, BBB, CCC = Equity("AAA"), Equity("BBB"), Equity("CCC")


# --- derived risk context (eod/runner.py 880-908) ----------------------------------------
# Live in production: te_py's equity_flow.rs calls `_derive_risk_context` (about line 129), so it
# is not dead code. It is called here directly, with `self` passed as None, because the derivation
# is the behaviour the function promises.


def _derive(state: AccountState, instrument: Equity):
    return EodRunner._derive_risk_context(None, SimpleNamespace(instrument=instrument), state)


def test_risk_context_for_a_held_long_marks_only_priced_positions() -> None:
    state = AccountState(
        "dense",
        cash=D("100"),
        positions={
            AAA: Position("dense", AAA, D("5"), D("100")),
            BBB: Position("dense", BBB, D("-2"), D("100")),
        },
        marks={AAA: D("10")},
    )
    ctx = _derive(state, AAA)
    # Equity is cash 100 plus AAA marked at 5 x 10. BBB has no mark, so it adds nothing.
    assert ctx.equity == D("150")
    assert ctx.gross_exposure == D("50")
    assert ctx.open_positions == 2
    assert ctx.current_position_quantity == 5
    assert ctx.current_price is None


def test_risk_context_reports_the_signed_quantity_of_a_short() -> None:
    state = AccountState(
        "dense",
        cash=D("100"),
        positions={
            AAA: Position("dense", AAA, D("5"), D("100")),
            BBB: Position("dense", BBB, D("-2"), D("100")),
        },
        marks={AAA: D("10")},
    )
    ctx = _derive(state, BBB)
    assert ctx.current_position_quantity == -2
    assert ctx.equity == D("150")
    assert ctx.open_positions == 2


def test_risk_context_for_an_instrument_not_held_is_flat() -> None:
    state = AccountState(
        "dense",
        cash=D("100"),
        positions={AAA: Position("dense", AAA, D("5"), D("100"))},
        marks={AAA: D("10")},
    )
    assert _derive(state, CCC).current_position_quantity == 0


def test_risk_context_for_an_empty_account_reports_no_equity_when_cash_is_negative() -> None:
    state = AccountState("dense", cash=D("-0.5"), positions={}, marks={})
    ctx = _derive(state, AAA)
    assert ctx.equity is None
    assert ctx.gross_exposure == D("0")
    assert ctx.open_positions == 0
    assert ctx.current_position_quantity == 0


# --- EodRunnerConfig refusals (eod/runner.py 305 onward) ----------------------------------


def _config(**changes):
    return EodRunnerConfig("eod", {"dense": object()}, **changes)


def test_config_refuses_an_empty_job_name() -> None:
    with pytest.raises(EodRunnerError, match="job_name must be non-empty"):
        EodRunnerConfig("", {"dense": object()})


def test_config_refuses_no_brokers() -> None:
    with pytest.raises(EodRunnerError, match="at least one broker is required"):
        EodRunnerConfig("eod", {})


@pytest.mark.parametrize("value", [False, -1, 0, "x"])
def test_config_refuses_a_non_positive_bar_age(value: object) -> None:
    with pytest.raises(EodRunnerError, match="bars_max_age_seconds must be positive"):
        _config(bars_max_age_seconds=value)


def test_config_refuses_a_negative_settle_delay() -> None:
    with pytest.raises(EodRunnerError, match="settle_delay must be a non-negative timedelta"):
        _config(settle_delay=timedelta(microseconds=-1))


def test_config_keeps_a_valid_bar_age_and_settle_delay() -> None:
    cfg = _config(bars_max_age_seconds=3.5, settle_delay=timedelta(microseconds=5))
    assert cfg.bars_max_age_seconds == 3.5
    assert cfg.settle_delay == timedelta(microseconds=5)


def test_config_refuses_a_nan_bar_age() -> None:
    with pytest.raises(EodRunnerError, match="bars_max_age_seconds must be positive"):
        _config(bars_max_age_seconds=float("nan"))


def test_refusal_kinds_all_derive_from_the_runner_error() -> None:
    assert issubclass(SessionIncompleteError, EodRunnerError)
    assert issubclass(ReplayDataError, EodRunnerError)
    assert issubclass(EodRunnerError, RuntimeError)


# --- intraday fresh view and config (intraday/service.py) ---------------------------------


@pytest.fixture
def intraday(tmp_path):
    rig = Rig(tmp_path)
    rig.minutes((9, 30), (10, 0))
    yield rig
    rig.ledger.close()


@pytest.mark.parametrize(
    ("age_seconds", "kept"),
    [(0, 2), (29.999999, 2), (30, 2), (30.000001, 0), (31, 0), (90, 0)],
)
def test_quotes_older_than_thirty_seconds_are_dropped(intraday: Rig, age_seconds: float, kept: int) -> None:
    base = snap(SESSION, 10, 0)
    quoted = base.as_of - timedelta(seconds=age_seconds)
    stale = replace(base, quotes=tuple(replace(q, as_of=quoted) for q in base.quotes))
    view = intraday.service()._fresh_view(stale, base.as_of)
    assert len(view.quotes) == kept
    assert view.underlying_as_of is not None


def test_snapshot_without_the_underlying_quote_time_is_stale(intraday: Rig) -> None:
    base = snap(SESSION, 10, 0)
    with pytest.raises(StaleDataError, match="does not say when SPX itself was quoted"):
        intraday.service()._fresh_view(replace(base, underlying_as_of=None), base.as_of)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"job_name": ""}, "job_name must be non-empty"),
        ({"account_id": ""}, "account_id must be non-empty"),
        ({"underlying": ""}, "underlying must be non-empty"),
        ({"eod_job_name": ""}, "eod_job_name must name the after-close job whose marker gates the next session"),
        ({"max_quote_age_seconds": 0}, "quote age and tick must be positive"),
        ({"max_quote_age_seconds": float("nan")}, "quote age and tick must be positive"),
        ({"heartbeat_ttl_seconds": 0}, "heartbeat ttl and close-mark window must be positive"),
        ({"entry_end": time(16)}, "entry_end must not come after flat_at"),
        ({"flat_before_close": timedelta(0)}, "flat_before_close must be positive and no later than entry_before_close"),
    ],
)
def test_intraday_config_refuses_each_bad_value(intraday: Rig, changes: dict, message: str) -> None:
    cfg = intraday.service()._config
    with pytest.raises(IntradayServiceError, match=re.escape(message)):
        replace(cfg, **changes)


def test_intraday_config_defaults_place_the_entry_and_flat_windows(intraday: Rig) -> None:
    cfg = intraday.service()._config
    assert cfg.entry_end == time(12, 0)
    assert cfg.flat_at == time(15, 30)
    assert cfg.flat_before_close == timedelta(minutes=30)
    assert cfg.entry_before_close == timedelta(minutes=90)


# --- session length in New York time (calendar) -------------------------------------------
# 2026-07-02 is left out on purpose: whether it is an early close is unresolved, so pinning
# the calendar's current answer would encode a guess. See the report.


@pytest.mark.parametrize(
    ("day", "open_et", "close_et", "minutes"),
    [
        (date(2026, 1, 2), time(9, 30), time(16, 0), 390),
        (date(2026, 3, 9), time(9, 30), time(16, 0), 390),
        (date(2026, 9, 25), time(9, 30), time(16, 0), 390),
        (date(2026, 11, 27), time(9, 30), time(13, 0), 210),
        (date(2026, 12, 24), time(9, 30), time(13, 0), 210),
    ],
)
def test_session_window_in_new_york_time(day: date, open_et: time, close_et: time, minutes: int) -> None:
    calendar = ExchangeCalendar()
    opened = calendar.session_open(day).astimezone(NEW_YORK)
    closed = calendar.session_close(day).astimezone(NEW_YORK)
    assert opened.time() == open_et
    assert closed.time() == close_et
    assert int((closed - opened).total_seconds() // 60) == minutes


# --- options routing (eod/options_routing.py) ----------------------------------------------


class _Ledger:
    """Answers the two questions the router asks of the ledger."""

    def __init__(self, commands=(), event=None) -> None:
        self._commands = set(commands)
        self._event = event

    def has_command(self, key: str) -> bool:
        return key in self._commands

    def event_by_command(self, key: str):
        return self._event


def _verdict(accepted: bool):
    return SimpleNamespace(payload=SimpleNamespace(accepted=accepted))


@pytest.mark.parametrize(
    ("commands", "event", "taken"),
    [
        ((), None, False),  # nothing recorded for this command yet
        (("entry",), None, True),  # the command is already in the ledger
        ((), _verdict(False), True),  # no command, but a refused verdict was recorded
        ((), _verdict(True), False),  # an accepted verdict does not count as taken
    ],
)
def test_router_reports_whether_an_intent_was_already_taken(commands, event, taken: bool) -> None:
    intent = replace(csp("entry"), account_id="OPT_CSP")
    router = OptionRouter(
        _Ledger(commands, event),
        SimpleNamespace(now_utc=lambda: AFTER),
        brokers={},
        strategies={},
        option_risk_engines={},
    )
    assert router.taken(intent) is taken


# --- routing tally addition (eod/options_routing.py 36-45) --------------------------------


def test_routing_tallies_add_field_by_field() -> None:
    total = RoutingTally(orders_submitted=1, exit_actions=2, snapshots_processed=3) + RoutingTally(
        orders_submitted=4, exit_actions=5, snapshots_processed=6
    )
    assert total == RoutingTally(orders_submitted=5, exit_actions=7, snapshots_processed=9)

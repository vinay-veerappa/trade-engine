"""Behaviour tests for the EOD runner and its routing helpers, asserting explicit values.

Replaces the lockstep oracle comparison in `test_p4a_parity.py`. Each test states the value the
production code must produce, and the scenarios come from the existing explicit-value suites
(`test_intraday_service.Rig`, `test_eod_options.csp`), not from a frozen copy of the old code.
"""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import date, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from test_intraday_service import SESSION, Rig, snap
from trade_engine.calendar.sessions import ExchangeCalendar
from trade_engine.eod.options_routing import RoutingTally
from trade_engine.eod.runner import (
    EodRunnerConfig,
    EodRunnerError,
    ReplayDataError,
    SessionIncompleteError,
)
from trade_engine.interfaces.market_data import StaleDataError
from trade_engine.intraday.service import IntradayServiceError
from trade_engine.sim._rs import rs as native

NEW_YORK = ZoneInfo("America/New_York")

# The derived risk context (`derive_risk_context`) moved to te_core; its values are pinned by
# `derive_risk_context_values_only_marked_positions` in crates/te_core/src/runtime/eod_glue.rs.
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
    view = native.intraday_fresh_view(intraday.service(), stale, base.as_of)
    assert len(view.quotes) == kept
    assert view.underlying_as_of is not None


def test_snapshot_without_the_underlying_quote_time_is_stale(intraday: Rig) -> None:
    base = snap(SESSION, 10, 0)
    with pytest.raises(StaleDataError, match="does not say when SPX itself was quoted"):
        native.intraday_fresh_view(intraday.service(), replace(base, underlying_as_of=None), base.as_of)


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


# --- routing tally addition (eod/options_routing.py 36-45) --------------------------------


def test_routing_tallies_add_field_by_field() -> None:
    total = RoutingTally(orders_submitted=1, exit_actions=2, snapshots_processed=3) + RoutingTally(
        orders_submitted=4, exit_actions=5, snapshots_processed=6
    )
    assert total == RoutingTally(orders_submitted=5, exit_actions=7, snapshots_processed=9)

"""P1a oracle (docs/RUST_PORT.md): the Rust XNYS calendar against exchange_calendars.

exchange_calendars is a test-only dependency now; it is the oracle the Rust
calendar replaced. Every date 2000-01-01..2040-12-31 must agree on session,
holiday, early close, open and close; navigation is checked from every date.
"""

from datetime import date, timedelta

import exchange_calendars as xcals
import pandas as pd
import trade_engine_rs as rs  # a missing module is an ERROR, never a skip (D5)

FIRST, LAST = date(2000, 1, 1), date(2040, 12, 31)


def _oracle():
    cal = xcals.get_calendar("XNYS", start=FIRST.isoformat(), end=LAST.isoformat())
    sched = cal.schedule
    opens = {ts.date(): int(o.timestamp()) for ts, o in sched["open"].items()}
    closes = {ts.date(): int(c.timestamp()) for ts, c in sched["close"].items()}
    early = {ts.date() for ts in cal.early_closes if FIRST <= ts.date() <= LAST}
    return opens, closes, early


def _days():
    d = FIRST
    while d <= LAST:
        yield d
        d += timedelta(days=1)


def test_every_date_matches_the_oracle() -> None:
    opens, closes, early = _oracle()
    diffs = []
    for d in _days():
        iso = d.isoformat()
        want_session = d in opens
        if rs.calendar_is_session(iso) != want_session:
            diffs.append((iso, "session", want_session))
            continue
        if rs.calendar_is_holiday(iso) != (not want_session and d.weekday() < 5):
            diffs.append((iso, "holiday"))
        if rs.calendar_is_early_close(iso) != (d in early):
            diffs.append((iso, "early", d in early))
        if want_session:
            if rs.calendar_session_open(iso) != opens[d]:
                diffs.append((iso, "open"))
            if rs.calendar_session_close(iso) != closes[d]:
                diffs.append((iso, "close"))
    assert diffs == []


def test_navigation_matches_the_oracle() -> None:
    opens, _, _ = _oracle()
    sessions = sorted(opens)
    idx = {d: i for i, d in enumerate(sessions)}
    diffs = []
    for d in _days():
        iso = d.isoformat()
        nxt = next((s for s in sessions[idx.get(d, 0):] if s > d), None)
        prv = next((s for s in reversed(sessions[: idx.get(d, len(sessions)) + 1]) if s < d), None)
        if nxt is not None and rs.calendar_next_session(iso) != nxt.isoformat():
            diffs.append((iso, "next"))
        if prv is not None and rs.calendar_previous_session(iso) != prv.isoformat():
            diffs.append((iso, "previous"))
    assert diffs == []


def test_is_open_at_matches_the_oracle_around_each_boundary() -> None:
    cal = xcals.get_calendar("XNYS", start=FIRST.isoformat(), end=LAST.isoformat())
    for ts, row in cal.schedule.loc["2024-01-01":"2027-12-31"].iterrows():
        for t in (row["open"], row["close"]):
            for off in (-1, 0, 1):
                probe = t + pd.Timedelta(seconds=off)
                want = bool(cal.is_open_at_time(probe))
                assert rs.calendar_is_open_at(int(probe.timestamp() * 1_000_000)) is want, (ts, off)


def test_out_of_range_refuses_rather_than_guessing() -> None:
    import pytest

    with pytest.raises(ValueError, match="outside the XNYS calendar range"):
        rs.calendar_is_session("1999-12-31")
    with pytest.raises(ValueError, match="outside the XNYS calendar range"):
        rs.calendar_next_session("2040-12-31")

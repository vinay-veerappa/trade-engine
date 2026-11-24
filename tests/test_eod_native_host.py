"""W6-C2a: the native EOD host against the Python callbacks.

Path A is a plain ``Ledger`` on the XNYS ``ExchangeCalendar``: the runner takes the native
``eod_glue`` path. Path B is the same ledger behind a pass-through that is not a ``Ledger``
subclass, so the runner takes the Python callbacks. Both paths must leave the same raw rows,
the same outbox rows and the same results or refusals.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from decimal import Decimal
from functools import partial
from pathlib import Path
from types import SimpleNamespace

from test_eod_options import (
    ACCOUNT as OPTIONS_ACCOUNT,
)
from test_eod_options import (
    CAL,
    COHR,
    EXPIRY,
    S1,
    S2,
    S3,
    SESSIONS,
    Rig,
    close_price,
    csp,
    dividend,
    midday_time,
    new_process,
    passes_rig,
    run_morning,
    run_pass,
    snap_time,
)
from test_eod_runner import (
    ACCOUNT,
    CALENDAR,
    EOD_CLOCK,
    INSTRUMENT,
    NEXT_SESSION,
    PREV_EOD,
    PREV_SESSION_PREV,
    SECOND_ACCOUNT,
    SESSION,
    DeterministicStrategy,
    FakeMarketData,
    FixedRiskEngine,
    SessionSignalAdapter,
    SettableClock,
    _fixed_context_builder,
    _marker_command,
    _seed_bracket,
    _signal,
)

from trade_engine.calendar.sessions import ExchangeCalendar
from trade_engine.domain.instruments import Side
from trade_engine.domain.option_orders import OptionIntent
from trade_engine.domain.orders import OrderType
from trade_engine.eod import EodRunner, EodRunnerConfig
from trade_engine.interfaces.market_data import StaleDataError
from trade_engine.ledger import Ledger
from trade_engine.lifecycle import FixedDividends
from trade_engine.market_data.chains import ChainSnapshot
from trade_engine.sim import SimBroker

ROWS = "SELECT kind, account, payload_json, ts_utc, command_id, schema_version FROM events ORDER BY seq"
OUTBOX = "SELECT * FROM outbox ORDER BY id"


class PassThrough:
    """The ledger behind a wrapper that is not a ``Ledger``: the runner takes the Python callbacks."""

    def __init__(self, ledger: Ledger) -> None:
        self._ledger = ledger

    def __getattr__(self, name: str):
        return getattr(self._ledger, name)


class OtherCalendar(ExchangeCalendar):
    """An ``ExchangeCalendar`` subclass: the gate's exact-type check sends it to the Python callbacks."""


def _book(root: Path, *, native: bool, clock_time: datetime, seed=None) -> SimpleNamespace:
    root.mkdir(parents=True, exist_ok=True)
    ledger = Ledger(root / "book.db").open()
    clock = SettableClock(clock_time)
    venue = SimBroker(ACCOUNT, clock, Decimal("0"))
    venue.connect()
    if seed is not None:
        seed(ledger, venue, clock)
    return SimpleNamespace(
        ledger=ledger if native else PassThrough(ledger),
        raw=ledger,
        clock=clock,
        venue=venue,
        db=root / "book.db",
    )


def _stored(db: Path) -> tuple[list, list]:
    """The raw ``events`` and ``outbox`` rows of a closed ledger's database."""
    con = sqlite3.connect(db)
    try:
        return con.execute(ROWS).fetchall(), con.execute(OUTBOX).fetchall()
    finally:
        con.close()


def _capture(action):
    """The result of ``action``, or the refusal it raised, as comparable data."""
    try:
        return ("ok", action())
    except Exception as error:  # the refusal's type and message are the contract
        return ("error", type(error).__name__, str(error))


def _seed_open_position(ledger, venue, clock) -> None:
    _seed_bracket(ledger, venue, clock, command_id="entry", stop_price="95", entry_prefilled=True)


def _equity_marks_run(root: Path, native: bool):
    """An open equity position at the close: fill, marks and the run marker."""
    book = _book(root, native=native, clock_time=PREV_EOD, seed=_seed_open_position)
    runner = EodRunner(
        book.ledger, book.clock, CALENDAR, FakeMarketData(),
        EodRunnerConfig(job_name="eod", brokers={ACCOUNT: book.venue}),
    )
    outcome = _capture(lambda: runner.run(SESSION))
    book.raw.close()
    return outcome, _stored(book.db)


def _signal_entry_run(root: Path, native: bool, *, context_builder: bool):
    """The D+1 signal path: signal, risk verdict and the entry bracket submitted."""
    book = _book(root, native=native, clock_time=EOD_CLOCK)
    config = EodRunnerConfig(
        job_name="eod",
        brokers={ACCOUNT: book.venue},
        risk_engines={ACCOUNT: FixedRiskEngine()},
        signal_adapters={ACCOUNT: SessionSignalAdapter([_signal()])},
        strategies={ACCOUNT: DeterministicStrategy(INSTRUMENT)},
        context_builder=_fixed_context_builder() if context_builder else None,
    )
    runner = EodRunner(book.ledger, book.clock, CALENDAR, FakeMarketData(), config)
    outcome = _capture(lambda: runner.run(SESSION))
    book.raw.close()
    return outcome, _stored(book.db)


def _refusal_run(root: Path, native: bool):
    """The previous session's marker is missing for one account: the next session refuses."""
    clock = SettableClock(PREV_EOD)
    ledger = Ledger(root / "book.db").open()
    wrapped = ledger if native else PassThrough(ledger)
    broker_a = SimBroker(ACCOUNT, clock, Decimal("0"))
    broker_a.connect()
    from trade_engine.ledger import EodRun, Event, EventKind

    ledger.append(Event(
        account=SECOND_ACCOUNT,
        kind=EventKind.EOD_RUN,
        payload=EodRun(
            session=PREV_SESSION_PREV, job="eod", account_id=SECOND_ACCOUNT,
            bars_processed=0, at_close=PREV_EOD,
        ),
        ts_utc=PREV_EOD,
        command_id=_marker_command(PREV_SESSION_PREV, SECOND_ACCOUNT),
    ))
    EodRunner(wrapped, clock, CALENDAR, FakeMarketData(),
              EodRunnerConfig(job_name="eod", brokers={ACCOUNT: broker_a})).run(SESSION)

    clock.set(datetime(2026, 9, 24, 13, 29, tzinfo=PREV_EOD.tzinfo))
    broker_b = SimBroker(SECOND_ACCOUNT, clock, Decimal("0"))
    broker_b.connect()
    outcome = _capture(lambda: EodRunner(
        wrapped, clock, CALENDAR, FakeMarketData(),
        EodRunnerConfig(job_name="eod", brokers={ACCOUNT: broker_a, SECOND_ACCOUNT: broker_b}),
    ).run(NEXT_SESSION))
    ledger.close()
    return outcome, _stored(root / "book.db")


def _rerun_run(root: Path, native: bool):
    """The same session run twice: the second run finds its marker and appends nothing."""
    book = _book(root, native=native, clock_time=PREV_EOD, seed=_seed_open_position)
    runner = EodRunner(
        book.ledger, book.clock, CALENDAR, FakeMarketData(),
        EodRunnerConfig(job_name="eod", brokers={ACCOUNT: book.venue}),
    )
    first = _capture(lambda: runner.run(SESSION))
    second = _capture(lambda: runner.run(SESSION))
    book.raw.close()
    return (first, second), _stored(book.db)


def _options_pass_run(root: Path, native: bool):
    """An options account: a morning pass, a midday pass, then the after-close run resumes."""
    rig = Rig(root)
    if not native:
        rig.ledger = PassThrough(rig.ledger)
    passes_rig(rig)
    morning = _capture(lambda: run_morning(rig, S2))
    midday = _capture(lambda: run_pass(rig, S2, "midday", midday_time(S2) + timedelta(minutes=5)))
    new_process(rig, S2)
    close = _capture(lambda: rig.run(S2))
    rig.ledger.close()
    return (morning, midday, close), _stored(root / "ledger.db")


def _assert_parity(tmp_path: Path, run, *args, **kwargs) -> tuple:
    native = run(tmp_path / "native", True, *args, **kwargs)
    python = run(tmp_path / "python", False, *args, **kwargs)
    assert native[0] == python[0], "results or refusals differ"
    assert native[1][0] == python[1][0], "raw events rows differ"
    assert native[1][1] == python[1][1], "outbox rows differ"
    return native, python


# -- 1. parity: rows, outbox and results over the close scenarios --------------------------


def test_equity_fill_marks_and_run_marker_match_the_python_callbacks(tmp_path: Path) -> None:
    (outcome, (rows, _outbox)), _python = _assert_parity(tmp_path, _equity_marks_run)
    assert outcome[0] == "ok", outcome
    kinds = {row[0] for row in rows}
    assert {"Fill", "Mark", "EodRun"} <= kinds, kinds
    assert any(row[4] == _marker_command(SESSION) for row in rows)


def test_signal_verdict_and_bracket_match_with_the_risk_context_derived(tmp_path: Path) -> None:
    (outcome, (rows, _outbox)), _python = _assert_parity(tmp_path, _signal_entry_run, context_builder=False)
    assert outcome[0] == "ok", outcome
    assert any(row[4] == "signal:sig-1" for row in rows)
    assert any(row[4] == "risk:d1:sig-1" for row in rows)


def test_signal_verdict_and_bracket_match_with_a_context_builder(tmp_path: Path) -> None:
    (outcome, (rows, _outbox)), _python = _assert_parity(tmp_path, _signal_entry_run, context_builder=True)
    assert outcome[0] == "ok", outcome
    assert any(row[4] == "risk:d1:sig-1" for row in rows)


def test_options_morning_midday_and_resumed_close_match(tmp_path: Path) -> None:
    (outcomes, (rows, _outbox)), _python = _assert_parity(tmp_path, _options_pass_run)
    assert all(outcome[0] == "ok" for outcome in outcomes), outcomes
    assert any(row[0] == "EodRun" for row in rows)


# -- 2. refusal parity ----------------------------------------------------------------------


def test_missing_previous_session_refuses_identically(tmp_path: Path) -> None:
    (outcome, _rows), (python_outcome, _python_rows) = _assert_parity(tmp_path, _refusal_run)
    assert outcome[0] == "error", outcome
    assert outcome[1] == "SessionIncompleteError"
    assert SECOND_ACCOUNT in outcome[2]
    assert python_outcome == outcome


def test_an_already_completed_rerun_matches_on_both_paths(tmp_path: Path) -> None:
    (outcomes, _stored_rows), _python = _assert_parity(tmp_path, _rerun_run)
    # The second run of a completed session is a no-op, not a refusal: its marker is found.
    assert outcomes[0][0] == "ok" and outcomes[1][0] == "ok", outcomes


# -- 3. calendar gate ----------------------------------------------------------------------


def test_the_calendar_gate_sends_only_a_non_xnys_or_non_plain_book_to_python(
    tmp_path: Path, monkeypatch
) -> None:
    calls: list[str] = []
    original = EodRunner._fill_count

    def counting(self, account_id: str) -> int:
        calls.append(account_id)
        return original(self, account_id)

    monkeypatch.setattr(EodRunner, "_fill_count", counting)

    def run(calendar, native: bool, root: Path) -> None:
        book = _book(root, native=native, clock_time=PREV_EOD, seed=_seed_open_position)
        EodRunner(
            book.ledger, book.clock, calendar, FakeMarketData(),
            EodRunnerConfig(job_name="eod", brokers={ACCOUNT: book.venue}),
        ).run(SESSION)
        book.raw.close()

    run(CALENDAR, True, tmp_path / "native")
    native_calls = len(calls)
    calls.clear()
    run(OtherCalendar(), True, tmp_path / "subclass")
    subclass_calls = len(calls)

    assert native_calls == 0
    assert subclass_calls > 0


# -- 4. the options book: dividends, marks, settlement and snapshot coverage ---------------


def _buy_shares() -> OptionIntent:
    return OptionIntent(
        intent_id="buy", account_id=OPTIONS_ACCOUNT, instrument=COHR, side=Side.BUY, quantity=Decimal(100),
        reason="buy-write", command_id="buy", order_type=OrderType.MARKET,
    )


class StaleDividends:
    """A dividend source whose feed stops before the session it is asked for."""

    def dividends(self, symbol, session):
        raise StaleDataError(f"the dividend feed for {symbol} stops before {session}")


def _options_run(root: Path, native: bool, steps, **rig_options):
    """A fresh options rig on the native or the Python path; ``steps`` returns the captured runs."""
    root.mkdir(parents=True, exist_ok=True)
    rig = Rig(root, **rig_options)
    if not native:
        rig.ledger = PassThrough(rig.ledger)
    outcomes = steps(rig)
    rig.ledger.close()
    return outcomes, _stored(root / "ledger.db")


def _options_book(rig) -> tuple:
    """Shares bought at S1's snapshot, a put sold at S1's close, then six sessions to expiry."""
    rig.strategy.at_snapshot[S1] = [_buy_shares()]
    rig.strategy.entries[S1] = [csp()]
    return tuple(_capture(lambda session=session: rig.run(session)) for session in SESSIONS)


def _no_dividend_source(rig) -> tuple:
    rig.strategy.at_snapshot[S1] = [_buy_shares()]
    first = _capture(lambda: rig.run(S1))
    rig.dividends = None
    rig.lifecycle = None
    return first, _capture(lambda: rig.run(S2))


def _stale_dividend_source(rig) -> tuple:
    rig.strategy.at_snapshot[S1] = [_buy_shares()]
    first = _capture(lambda: rig.run(S1))
    rig.dividends = StaleDividends()
    rig.lifecycle = None
    return first, _capture(lambda: rig.run(S2))


def _entered_then_run(rig) -> tuple:
    """The put is entered at S1's close and held from S2's snapshot."""
    rig.strategy.entries[S1] = [csp()]
    return _capture(lambda: rig.run(S1)), _capture(lambda: rig.run(S2))


def _unquoted_option(rig) -> tuple:
    rig.strategy.entries[S1] = [csp(target=None)]
    first = _capture(lambda: rig.run(S1))
    second = _capture(lambda: rig.run(S2))
    bare = ChainSnapshot("COHR", snap_time(S3), Decimal("300"), (), None, None, "test")
    return first, second, _capture(lambda: rig.run(S3, chain_snapshots=lambda s: [bare]))


def _missing_snapshot(rig) -> tuple:
    rig.strategy.entries[S1] = [csp()]
    first = _capture(lambda: rig.run(S1))
    second = _capture(lambda: rig.run(S2))
    return first, second, _capture(lambda: rig.run(S3, chain_snapshots=lambda s: []))


def _no_lifecycle(rig) -> tuple:
    rig.strategy.entries[S1] = [csp()]
    first = _capture(lambda: rig.run(S1))
    rig.lifecycle = None
    return first, _capture(lambda: rig.run(S2))


def _refused(outcome, kind: str, text: str) -> None:
    assert outcome[0] == "error" and outcome[1] == kind and text in outcome[2], outcome


def test_the_options_book_matches_the_python_callbacks(tmp_path: Path) -> None:
    closes = [close_price(s) for s in SESSIONS[:-1]] + [close_price(EXPIRY, "250")]
    run = partial(
        _options_run, steps=_options_book,
        dividends=FixedDividends({"COHR": [dividend(S2)]}), closes=closes,
    )
    (outcomes, (rows, _outbox)), _python = _assert_parity(tmp_path, run)
    assert all(outcome[0] == "ok" for outcome in outcomes), outcomes
    kinds = [row[0] for row in rows]
    assert kinds.count("Mark") > 0, kinds
    assert any("dividend" in row[2] for row in rows), "the dividend was not booked"
    assert any(row[0] == "Fill" for row in rows), "the assignment did not fill"


# -- 5. refusal parity for the options callbacks -------------------------------------------


def test_a_missing_dividend_source_refuses_identically(tmp_path: Path) -> None:
    run = partial(_options_run, steps=_no_dividend_source)
    (outcomes, _rows), _python = _assert_parity(tmp_path, run)
    _refused(outcomes[1], "EodRunnerError", "no dividend source")


def test_a_stale_dividend_feed_refuses_identically(tmp_path: Path) -> None:
    run = partial(_options_run, steps=_stale_dividend_source)
    (outcomes, _rows), _python = _assert_parity(tmp_path, run)
    assert outcomes[1][0] == "error" and outcomes[1][1] == "ReplayDataError", outcomes


def test_a_missing_option_quote_refuses_identically(tmp_path: Path) -> None:
    run = partial(_options_run, steps=_unquoted_option)
    (outcomes, _rows), _python = _assert_parity(tmp_path, run)
    _refused(outcomes[2], "ReplayDataError", "refusing to mark it")


def test_a_stale_official_close_refuses_identically(tmp_path: Path) -> None:
    run = partial(_options_run, steps=_entered_then_run, closes=[close_price(s) for s in SESSIONS if s != S2])
    (outcomes, _rows), _python = _assert_parity(tmp_path, run)
    _refused(outcomes[1], "ReplayDataError", "No official 2026-09-28 close for COHR")


def test_a_look_ahead_official_close_refuses_identically(tmp_path: Path) -> None:
    late = [close_price(s, at=CAL.session_close(s) + timedelta(hours=3)) for s in SESSIONS]
    run = partial(_options_run, steps=_entered_then_run, closes=late)
    (outcomes, _rows), _python = _assert_parity(tmp_path, run)
    _refused(outcomes[1], "ReplayDataError", "look-ahead")


def test_a_missing_snapshot_refuses_identically(tmp_path: Path) -> None:
    run = partial(_options_run, steps=_missing_snapshot)
    (outcomes, _rows), _python = _assert_parity(tmp_path, run)
    _refused(outcomes[2], "ReplayDataError", "No 2026-09-29 chain snapshot for COHR")


def test_no_lifecycle_pass_refuses_identically(tmp_path: Path) -> None:
    run = partial(_options_run, steps=_no_lifecycle)
    (outcomes, _rows), _python = _assert_parity(tmp_path, run)
    _refused(outcomes[1], "EodRunnerError", "no lifecycle pass")

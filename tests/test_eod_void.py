"""EOD-VOID-1: the owner-signed void of one session unblocks the I3 previous-session gate.

A session that can never complete (options 2026-10-16) would otherwise block every later
session. A void is the owner's signed record that it is complete for an account; it is
not trading history (the history probe, the fold and the TOS paper session ignore it).
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from trade_engine.calendar.sessions import ExchangeCalendar
from trade_engine.cli import main
from trade_engine.clock.replay import ReplayClock
from trade_engine.eod import EodRunner, EodRunnerConfig, SessionIncompleteError
from trade_engine.eod.runner import EodRunnerError, _void_command, void_session
from trade_engine.interfaces.market_data import Bar
from trade_engine.ledger import EodRun, EodVoid, Event, EventKind, Ledger
from trade_engine.ledger.codec import decode_event, encode_event
from trade_engine.sim import SimBroker

UTC = timezone.utc
VOIDED = date(2026, 9, 23)
NEXT = date(2026, 9, 24)
BEFORE = date(2026, 9, 22)
A = "acct-a"
B = "acct-b"
CALENDAR = ExchangeCalendar()
NOW = datetime(2026, 9, 23, 21, 45, tzinfo=UTC)
REASON = "options expiry session cannot complete"


class SettableClock(ReplayClock):
    def set(self, now: datetime) -> None:
        self._current_time = now.astimezone(UTC)


class NoBars:
    def bars(self, instrument, tf, start, end, max_age_seconds):
        return []


def _run_marker(ledger: Ledger, account: str, session: date) -> None:
    ledger.append(
        Event(
            account=account,
            kind=EventKind.EOD_RUN,
            payload=EodRun(
                session=session, job="eod", account_id=account, bars_processed=0, at_close=NOW
            ),
            ts_utc=NOW,
            command_id=f"eod:eod:{account}:{session.isoformat()}",
        )
    )


@pytest.fixture
def ledger(tmp_path: Path):
    with Ledger(tmp_path / "void.db") as led:
        # A ran both earlier sessions; B ran 09-22 only: 09-23 can never complete for B.
        _run_marker(led, A, BEFORE)
        _run_marker(led, A, VOIDED)
        _run_marker(led, B, BEFORE)
        yield led


def _runner(ledger: Ledger, clock: SettableClock) -> EodRunner:
    brokers = {}
    for account in (A, B):
        broker = SimBroker(account, clock, Decimal("0"))
        broker.connect()
        brokers[account] = broker
    return EodRunner(
        ledger, clock, CALENDAR, NoBars(), EodRunnerConfig(job_name="eod", brokers=brokers)
    )


def _void(ledger: Ledger, accounts=(B,), **over) -> tuple[str, ...]:
    args = dict(reason=REASON, owner="owner")
    args.update(over)
    return void_session(ledger, SettableClock(NOW), CALENDAR, "eod", VOIDED, accounts, **args)


def _voids(ledger: Ledger) -> list[Event]:
    return list(ledger.events_of_kind(EventKind.EOD_VOID))


@pytest.mark.parametrize("kw", [{"reason": ""}, {"reason": "  "}, {"owner": ""}, {"owner": " "}])
def test_void_refuses_empty_reason_or_owner(ledger: Ledger, kw) -> None:
    with pytest.raises(EodRunnerError):
        _void(ledger, **kw)
    assert _voids(ledger) == []


def test_void_refuses_when_the_account_already_ran_that_session(ledger: Ledger) -> None:
    with pytest.raises(EodRunnerError, match="already ran"):
        _void(ledger, accounts=(A, B))
    assert _voids(ledger) == []  # B is fine, but nothing is written when any account refuses


def test_second_void_is_a_no_op(ledger: Ledger) -> None:
    assert _void(ledger) == (B,)
    first = [(e.seq, e.command_id) for e in _voids(ledger)]
    assert _void(ledger, reason="changed my mind", owner="someone else") == (B,)
    assert [(e.seq, e.command_id) for e in _voids(ledger)] == first
    assert first[0][1] == f"eod-void:eod:{B}:{VOIDED.isoformat()}"
    assert _voids(ledger)[0].payload == EodVoid(VOIDED, B, REASON, "owner", "eod")


def test_void_refuses_unknown_account_and_non_session(ledger: Ledger) -> None:
    with pytest.raises(EodRunnerError, match="unknown"):
        _void(ledger, accounts=(B, "no-such"))
    with pytest.raises(EodRunnerError, match="at least one"):
        _void(ledger, accounts=())
    with pytest.raises(EodRunnerError, match="not a trading session"):
        void_session(ledger, SettableClock(NOW), CALENDAR, "eod", date(2026, 9, 26), (B,),
                     reason=REASON, owner="owner")
    assert _voids(ledger) == []


def test_void_names_every_unknown_account_in_a_python_list_repr(ledger: Ledger) -> None:
    with pytest.raises(EodRunnerError) as info:
        _void(ledger, accounts=("no-such", "a'b"))
    assert str(info.value) == "unknown account(s) ['no-such', \"a'b\"]: not in the ledger (I5)"
    assert _voids(ledger) == []


def test_void_refuses_a_datetime_session(ledger: Ledger) -> None:
    with pytest.raises(EodRunnerError, match="void session must be a date"):
        void_session(ledger, SettableClock(NOW), CALENDAR, "eod", datetime(2026, 9, 23, 21, 45, tzinfo=UTC),
                     (B,), reason=REASON, owner="owner")
    assert _voids(ledger) == []


def test_a_duplicate_account_is_voided_once_and_returned_once(ledger: Ledger) -> None:
    assert _void(ledger, accounts=(B, B)) == (B,)
    assert len(_voids(ledger)) == 1


def test_a_void_unblocks_the_previous_session_gate(ledger: Ledger) -> None:
    clock = SettableClock(datetime(2026, 9, 24, 13, 29, tzinfo=UTC))
    runner = _runner(ledger, clock)
    with pytest.raises(SessionIncompleteError, match=B):
        runner.run(NEXT)
    assert runner.void_session(VOIDED, [B], reason=REASON, owner="owner") == (B,)
    runner.run(NEXT)  # no longer refused
    assert ledger.event_by_command(f"eod:eod:{B}:{NEXT.isoformat()}") is not None
    # the void is B's only record for 09-23: no EodRun was invented for it
    assert ledger.event_by_command(f"eod:eod:{B}:{VOIDED.isoformat()}") is None


def test_a_void_is_not_history(ledger: Ledger) -> None:
    with Ledger(ledger.path.parent / "fresh.db") as fresh:
        _run_marker(fresh, B, VOIDED)  # B exists in the ledger, but only on the void session
        clock = SettableClock(NOW)
        runner = _runner(fresh, clock)
        before = runner._account_has_history(B, VOIDED + timedelta(days=1))
        fresh.append(
            Event(
                account=B, kind=EventKind.EOD_VOID,
                payload=EodVoid(BEFORE, B, REASON, "owner", "eod"),
                ts_utc=NOW, command_id=_void_command("eod", B, BEFORE),
            )
        )
        assert runner._account_has_history(B, VOIDED + timedelta(days=1)) is before
        assert runner._account_has_history(B, VOIDED) is False


def test_the_fold_ignores_a_void(ledger: Ledger) -> None:
    before = ledger.state(B)
    _void(ledger)
    after = ledger.state(B)
    # Only the sequence cursor moves (as for every event); no position, fill or cash does.
    assert after.last_seq == before.last_seq + 1
    assert replace(after, last_seq=before.last_seq) == before
    assert ledger.fold()[B] == after


def test_void_round_trips_through_the_codec(ledger: Ledger) -> None:
    _void(ledger)
    (event,) = _voids(ledger)
    assert decode_event(encode_event(event)) == event
    with pytest.raises(Exception):
        EodVoid(VOIDED, B, "", "owner", "eod")


def test_cli_void_writes_and_reports(ledger: Ledger, capsys) -> None:
    path = str(ledger.path)
    ledger.close()
    argv = ["eod", "--void", "--session", VOIDED.isoformat(), "--ledger", path,
            "--accounts", B, "--reason", REASON, "--owner", "owner"]
    assert main(argv) == 0
    assert f"{B}: voided {VOIDED.isoformat()}" in capsys.readouterr().out
    assert main(argv) == 0  # a repeat is an idempotent no-op
    assert f"{B}: already voided {VOIDED.isoformat()}" in capsys.readouterr().out
    with Ledger(path) as led:
        assert len(_voids(led)) == 1


@pytest.mark.parametrize(
    "extra",
    [
        ["--accounts", "no-such", "--reason", REASON, "--owner", "owner"],
        ["--accounts", B, "--reason", "", "--owner", "owner"],
        ["--accounts", B, "--reason", REASON, "--owner", ""],
        ["--reason", REASON, "--owner", "owner"],  # a void never defaults to every account
        ["--accounts", A, "--reason", REASON, "--owner", "owner"],  # A already ran 09-23
    ],
)
def test_cli_void_refusals_write_nothing(ledger: Ledger, extra, capsys) -> None:
    path = str(ledger.path)
    ledger.close()
    code = main(["eod", "--void", "--session", VOIDED.isoformat(), "--ledger", path, *extra])
    assert code == 2
    assert "eod:" in capsys.readouterr().err
    with Ledger(path) as led:
        assert _voids(led) == []

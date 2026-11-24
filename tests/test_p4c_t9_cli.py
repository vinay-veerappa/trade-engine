"""P4C-T9 CLI thin client: the one-shot actor's explicit behaviour.

The EOD CLI's observable bytes (flags, refusals, counters, exit codes) are pinned by
``tests/test_eod_cli_golden.py``. This file asserts the remaining explicit contracts:
re-runs are admitted durably, runner refusals and crashes are classified as recorded
terminal states, and a mid-run refusal keeps its exact text and exit code.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, timedelta
from decimal import Decimal

from trade_engine.cli import main
from trade_engine.domain.instruments import Equity
from trade_engine.eod import DiscoveredPlugins
from trade_engine.interfaces.market_data import Bar
from trade_engine.ledger import Ledger

UTC_SESSION = date(2026, 9, 23)


class CliMarketData:
    """Serves one bar per request so the runner records the session."""

    name = "cli-fake"

    def bars(self, instrument, tf, start, end, max_age_seconds):
        return [
            Bar(
                instrument=instrument,
                timestamp=start,
                open=Decimal("100"),
                high=Decimal("101"),
                low=Decimal("99"),
                close=Decimal("100"),
                volume=Decimal("10000"),
                as_of=start,
            )
        ]


def _journal_rows(ledger_path, *, values=False):
    connection = sqlite3.connect(ledger_path)
    try:
        if values:
            return connection.execute(
                "SELECT value FROM meta WHERE key LIKE 'te.runtime.jobs.v1:%'"
            ).fetchall()
        return [
            row[0]
            for row in connection.execute(
                "SELECT key FROM meta WHERE key LIKE 'te.runtime.jobs.v1:%'"
            )
        ]
    finally:
        connection.close()


def test_one_shot_actor_rerun_is_admitted_twice_and_both_runs_succeed(
    monkeypatch, tmp_path, capsys
):
    plugin = CliMarketData()
    monkeypatch.setattr(
        "trade_engine.eod.cli.discover_plugins",
        lambda: DiscoveredPlugins(strategies=(), signal_adapters=(), market_data=(plugin,)),
    )
    ledger_path = tmp_path / "cli.db"
    with Ledger(ledger_path) as ledger:
        ledger.set_meta("probed", "1")
    args = [
        "eod",
        "--session",
        UTC_SESSION.isoformat(),
        "--ledger",
        str(ledger_path),
        "--accounts",
        "cli-account",
    ]
    assert main(args) == 0
    assert capsys.readouterr().out == "cli-account: bars=0 fills=0 marks=0 orders=0\n"
    # Each invocation is a distinct request: the runner, with no recorded bars,
    # re-runs the session cleanly and prints the same counters.
    assert main(args) == 0
    assert capsys.readouterr().out == "cli-account: bars=0 fills=0 marks=0 orders=0\n"
    # The journal persisted both one-shot admissions durably.
    assert len(_journal_rows(ledger_path)) == 2


def test_one_shot_actor_classifies_runner_refusals_durable(tmp_path):
    """The EodRunnerError family is a recorded refusal, not a host failure."""
    import trade_engine_rs

    from trade_engine.eod.runner import EodRunnerError

    class RefusingRunner:
        def run(self, session):
            raise EodRunnerError("synthetic refusal for classification")

    class FailingRunner:
        def run(self, session):
            raise KeyError("synthetic crash for classification")

    session = date(2026, 9, 23)

    def record_for(runner, request_id):
        ledger_path = tmp_path / f"{request_id}.db"
        with Ledger(ledger_path) as ledger:
            ledger.set_meta("probed", "1")
            return trade_engine_rs.eod_once(ledger, runner, session, request_id)

    refused = record_for(RefusingRunner(), "classify-refused")
    assert refused["state"] == "refused"
    assert refused["error"]["type"] == "EodRunnerError"
    assert refused["error"]["message"] == "synthetic refusal for classification"
    assert refused["result"] is None

    failed = record_for(FailingRunner(), "classify-failed")
    assert failed["state"] == "failed"
    assert failed["error"]["type"] == "KeyError"


def test_one_shot_actor_preserves_mid_run_refusals_exactly(
    monkeypatch, tmp_path, capsys
):
    """A runner refusal inside the actor keeps its text and exit code.

    Run one session with recorded bars, then skip the next session and run the
    one after: its predecessor was never run, so the runner refuses with
    SessionIncompleteError. The CLI must print ``eod: refused:`` with the
    predecessor date and exit 2, and the refusal must be the recorded terminal
    state of its own admission.
    """
    plugin = CliMarketData()
    monkeypatch.setattr(
        "trade_engine.eod.cli.discover_plugins",
        lambda: DiscoveredPlugins(strategies=(), signal_adapters=(), market_data=(plugin,)),
    )
    first = date(2026, 9, 23)
    skipped = date(2026, 9, 24)
    after_skipped = date(2026, 9, 25)
    assert after_skipped - skipped == timedelta(days=1)
    ledger_path = tmp_path / "refused.db"
    with Ledger(ledger_path) as ledger:
        ledger.set_meta("probed", "1")
    base = ["eod", "--ledger", str(ledger_path), "--accounts", "cli-account"]
    assert main([*base, "--session", first.isoformat()]) == 0
    assert capsys.readouterr().out == "cli-account: bars=0 fills=0 marks=0 orders=0\n"

    code = main([*base, "--session", after_skipped.isoformat()])
    captured = capsys.readouterr()
    assert code == 2
    assert captured.out == ""
    assert captured.err == (
        f"eod: refused: Cannot run {after_skipped.isoformat()} for 'cli-account': "
        f"the previous session {skipped.isoformat()} has no eod marker; "
        "complete it first (I3)\n"
    )

    rows = _journal_rows(ledger_path, values=True)
    assert rows, "the refusing invocation must still be durably admitted"
    record = json.loads(rows[-1][0])
    assert record["state"] == "refused"
    assert record["error"]["type"] == "SessionIncompleteError"
    assert "no eod marker" in record["error"]["message"]


def test_thin_client_refuses_without_market_data_with_exact_text(
    monkeypatch, tmp_path, capsys
):
    monkeypatch.setattr(
        "trade_engine.eod.cli.discover_plugins",
        lambda: DiscoveredPlugins(strategies=(), signal_adapters=(), market_data=()),
    )
    code = main(["eod", "--session", "2026-09-23", "--ledger", str(tmp_path / "l.db")])
    captured = capsys.readouterr()
    assert code == 2
    assert captured.out == ""
    assert captured.err == (
        "eod: refused: No trade_engine.marketdata plugins are installed; "
        "cannot source bars (I5)\n"
    )

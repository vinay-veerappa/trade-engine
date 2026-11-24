"""EOD CLI golden transcript: flags, refusals, stdout counters and exit codes.

The golden at ``tests/golden/eod_cli/transcript.golden.txt`` is a SNAPSHOT of the
production CLI's observable output, recorded from the current implementation
(``trade_engine.cli.main``). It is not a second implementation: nothing in this
file re-derives the expected output, it only replays the commands and compares the
bytes that production prints.

An intended change to the CLI (a new flag, a changed refusal sentence, a new counter)
updates the golden in the SAME commit. Regenerate it with::

    TE_RECORD_EOD_CLI_GOLDEN=1 pytest -q tests/test_eod_cli_golden.py

then review the diff of ``transcript.golden.txt`` before committing it.
"""

from __future__ import annotations

import os

from decimal import Decimal
from pathlib import Path

import pytest

from trade_engine.cli import main
from trade_engine.eod import DiscoveredPlugins
from trade_engine.interfaces.market_data import Bar
from trade_engine.ledger import Ledger

GOLDEN = Path(__file__).resolve().parent / "golden" / "eod_cli" / "transcript.golden.txt"
RECORD_ENV = "TE_RECORD_EOD_CLI_GOLDEN"
SESSION = "2026-09-23"
NEXT_SESSION = "2026-09-25"
LEDGER = "ledger.db"
DISCOVER = "trade_engine.eod.cli.discover_plugins"


class _Plugin:
    """Serves one fixed bar per request so the runner records the session."""

    def __init__(self, name: str) -> None:
        self.name = name

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


def _discover(*plugins):
    return lambda: DiscoveredPlugins(
        strategies=(), signal_adapters=(), market_data=tuple(plugins)
    )


def _eod(*extra: str) -> list[str]:
    return ["eod", "--ledger", LEDGER, *extra]


def _run(argv: list[str], capsys) -> str:
    """Run one production invocation and render its exact observable output."""
    try:
        code = main(argv)
    except SystemExit as exit_request:  # argparse exits with 2 on a usage error
        code = exit_request.code
    captured = capsys.readouterr()
    return (
        f"$ trade_engine {' '.join(argv)}\n"
        f"exit: {code}\n"
        f"stdout:\n{captured.out}"
        f"stderr:\n{captured.err}"
    )


def _scenarios():
    """(name, plugins, steps). A step is an argv list or a callable taking (monkeypatch)."""
    one = _Plugin("cli-fake")
    other = _Plugin("other-fake")
    recording = _Plugin("cli-fake")
    admitted_only = lambda mp: mp.setattr(  # noqa: E731
        "trade_engine.eod.cli.rs_call", lambda *args, **kwargs: {"state": "admitted"}
    )
    return [
        (
            "usage-errors",
            [one],
            [
                ["eod", "--session", SESSION],
                ["eod", "--session", "2026-13-01", "--ledger", LEDGER],
                ["eod", "--session", SESSION, "--ledger", LEDGER, "--slippage-bps", "-1"],
                ["eod", "--session", SESSION, "--ledger", LEDGER, "--slippage-bps", "abc"],
            ],
        ),
        (
            "no-market-data",
            [],
            [_eod("--session", SESSION, "--accounts", "cli-account")],
        ),
        (
            "market-data-selection",
            [one, other],
            [
                _eod("--session", SESSION, "--accounts", "cli-account"),
                _eod("--session", SESSION, "--accounts", "cli-account", "--market-data", "nope"),
                _eod(
                    "--session", SESSION, "--accounts", "cli-account",
                    "--market-data", "other-fake",
                ),
            ],
        ),
        (
            "no-accounts-anywhere",
            [one],
            [_eod("--session", SESSION)],
        ),
        (
            "run-and-rerun",
            [recording],
            [
                _eod("--session", SESSION, "--accounts", "cli-account", "--slippage-bps", "1.5"),
                _eod("--session", SESSION, "--accounts", "cli-account", "--slippage-bps", "1.5"),
            ],
        ),
        (
            "skipped-predecessor-refusal",
            [recording],
            [
                _eod("--session", SESSION, "--accounts", "cli-account"),
                _eod("--session", NEXT_SESSION, "--accounts", "cli-account"),
            ],
        ),
        (
            "accounts-from-ledger",
            [recording],
            [
                _eod("--session", SESSION, "--accounts", "cli-account"),
                _eod("--session", "2026-09-24"),
            ],
        ),
        (
            "admitted-not-terminal",
            [recording],
            [admitted_only, _eod("--session", SESSION, "--accounts", "cli-account")],
        ),
        (
            "void",
            [one],
            [
                # The account must exist in the ledger before a void can name it.
                _eod("--session", SESSION, "--accounts", "cli-account"),
                _eod("--session", SESSION, "--void", "--accounts", "cli-account"),
                _eod(
                    "--session", NEXT_SESSION, "--void", "--accounts", "cli-account",
                    "--reason", "feed outage", "--owner", "ops",
                ),
                _eod(
                    "--session", NEXT_SESSION, "--void", "--accounts", "cli-account",
                    "--reason", "feed outage", "--owner", "ops",
                ),
                _eod(
                    "--session", SESSION, "--void", "--accounts", "cli-account",
                    "--reason", "feed outage", "--owner", "ops",
                ),
                _eod("--session", NEXT_SESSION, "--void", "--reason", "x", "--owner", "ops"),
            ],
        ),
        (
            "write-off",
            [one],
            [
                _eod("--session", SESSION, "--accounts", "cli-account"),
                _eod("--session", SESSION, "--write-off", "--accounts", "cli-account"),
                _eod(
                    "--session", SESSION, "--write-off", "--accounts", "a,b",
                    "--order", "ord-1", "--reason", "stuck", "--owner", "ops",
                ),
                _eod(
                    "--session", SESSION, "--write-off", "--accounts", "cli-account",
                    "--order", "ord-1", "--reason", "stuck", "--owner", "ops",
                ),
            ],
        ),
    ]


def _transcript(root: Path, monkeypatch, capsys) -> str:
    monkeypatch.setenv("COLUMNS", "80")
    blocks: list[str] = []
    for name, plugins, steps in _scenarios():
        scenario_dir = root / name
        scenario_dir.mkdir()
        with Ledger(scenario_dir / LEDGER) as ledger:
            ledger.set_meta("probed", "1")
        with pytest.MonkeyPatch.context() as scoped:
            scoped.chdir(scenario_dir)
            scoped.setattr(DISCOVER, _discover(*plugins))
            blocks.append(f"### scenario: {name}\n")
            for step in steps:
                if callable(step):
                    step(scoped)
                    continue
                blocks.append(_run(step, capsys))
    return "".join(blocks).replace(str(root), "<TMP>")


def test_eod_cli_transcript_matches_golden(tmp_path, monkeypatch, capsys):
    transcript = _transcript(tmp_path, monkeypatch, capsys)
    if os.environ.get(RECORD_ENV) == "1":
        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN.write_bytes(transcript.encode("utf-8"))
    assert GOLDEN.exists(), f"missing golden {GOLDEN}; record it with {RECORD_ENV}=1"
    expected = GOLDEN.read_bytes().decode("utf-8")
    assert transcript == expected

"""P4C-T9 CLI thin-client parity: the native one-shot actor preserves the frozen CLI.

The frozen oracle (`tests/frozen_p4c/t9_cli.py` + `t9_eod_cli.py`) pins the
pre-port CLI's flags, refusals, stdout counters and exit codes. The new thin
client routes the one session through `trade_engine_rs.eod_once` (durable
admission, no resend, exact refusal text) and must be indistinguishable from
the oracle on every observable path.
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path
import types

import pytest

from trade_engine.cli import main
from trade_engine.domain.instruments import Equity
from trade_engine.eod import DiscoveredPlugins
from trade_engine.interfaces.market_data import Bar
from trade_engine.ledger import Ledger

ROOT = Path(__file__).resolve().parent.parent
FROZEN_DIR = ROOT / "tests" / "frozen_p4c"
UTC_SESSION = date(2026, 9, 23)


def _load_frozen_cli() -> types.ModuleType:
    """Load the frozen main CLI with its EOD import rewired to the frozen peer."""
    spec = importlib.util.spec_from_file_location("t9_frozen_cli", FROZEN_DIR / "t9_cli.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class CliMarketData:
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


class RecordingMarketData:
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


def test_frozen_cli_bytes_match_the_oracle_manifest():
    import json

    manifest = json.loads((FROZEN_DIR / "t9_oracle.json").read_text(encoding="utf-8"))
    for entry, key in (("cli", "t9_cli.py"), ("eod_cli", "t9_eod_cli.py")):
        frozen = FROZEN_DIR / key
        expected = manifest[entry]["frozen_sha256"]
        actual = __import__("hashlib").sha256(frozen.read_bytes()).hexdigest()
        assert actual == expected, f"{key} no longer matches the frozen oracle"


def test_thin_client_flags_refusals_and_counts_match_the_frozen_oracle(
    monkeypatch, tmp_path
):
    frozen = _load_frozen_cli()
    plugin = CliMarketData()

    def discovered():
        return DiscoveredPlugins(strategies=(), signal_adapters=(), market_data=(plugin,))

    # The frozen CLI carries its own eod module: patch both surfaces.
    monkeypatch.setattr("trade_engine.eod.cli.discover_plugins", discovered)
    frozen_eod = sys.modules.get("tests.frozen_p4c.t9_eod_cli")
    if frozen_eod is None:
        spec = importlib.util.spec_from_file_location(
            "tests.frozen_p4c.t9_eod_cli", FROZEN_DIR / "t9_eod_cli.py"
        )
        frozen_eod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = frozen_eod
        spec.loader.exec_module(frozen_eod)
    frozen.discover_plugins = frozen_eod.discover_plugins
    monkeypatch.setattr(frozen_eod, "discover_plugins", discovered)

    oracle_path = tmp_path / "oracle.db"
    with Ledger(oracle_path) as ledger:
        ledger.set_meta("probed", "1")
    frozen_code = frozen.main(
        [
            "eod",
            "--session",
            UTC_SESSION.isoformat(),
            "--ledger",
            str(oracle_path),
            "--accounts",
            "cli-account",
        ]
    )
    # Seed the thin-client ledger the same way for a fair comparison.
    thin_path = tmp_path / "thin.db"
    with Ledger(thin_path) as ledger:
        ledger.set_meta("probed", "1")
    thin_code = main(
        [
            "eod",
            "--session",
            UTC_SESSION.isoformat(),
            "--ledger",
            str(thin_path),
            "--accounts",
            "cli-account",
        ]
    )
    assert thin_code == frozen_code == 0


def test_thin_client_refusals_match_the_frozen_oracle(monkeypatch, tmp_path, capsys):
    frozen = _load_frozen_cli()

    def empty():
        return DiscoveredPlugins(strategies=(), signal_adapters=(), market_data=())

    monkeypatch.setattr("trade_engine.eod.cli.discover_plugins", empty)
    code = main(["eod", "--session", "2026-09-23", "--ledger", str(tmp_path / "l.db")])
    assert code == 2
    assert "No trade_engine.marketdata plugins" in capsys.readouterr().err

    frozen_eod = sys.modules.get("tests.frozen_p4c.t9_eod_cli")
    if frozen_eod is None:
        spec = importlib.util.spec_from_file_location(
            "tests.frozen_p4c.t9_eod_cli", FROZEN_DIR / "t9_eod_cli.py"
        )
        frozen_eod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = frozen_eod
        spec.loader.exec_module(frozen_eod)
    monkeypatch.setattr(frozen_eod, "discover_plugins", empty)
    code = frozen.main(["eod", "--session", "2026-09-23", "--ledger", str(tmp_path / "o.db")])
    assert code == 2
    assert "No trade_engine.marketdata plugins" in capsys.readouterr().err


def test_one_shot_actor_rerun_matches_the_preport_oracle(
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
    assert "cli-account: bars=" in capsys.readouterr().out
    # A second invocation admits a fresh request and lets the runner's own
    # command-id machinery decide, exactly as the pre-port CLI did: with no
    # recorded bars the oracle re-runs cleanly (verified against the frozen
    # peer); with recorded state it would refuse.
    assert main(args) == 0
    assert "cli-account: bars=" in capsys.readouterr().out
    # The journal persisted both one-shot admissions durably.
    import json
    import sqlite3

    connection = sqlite3.connect(ledger_path)
    keys = [
        row[0]
        for row in connection.execute(
            "SELECT key FROM meta WHERE key LIKE 'te.runtime.jobs.v1:%'"
        )
    ]
    connection.close()
    assert len(keys) == 2, keys


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
            record = trade_engine_rs.eod_once(
                ledger, runner, session, request_id
            )
        return record

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
    """A runner refusal inside the actor keeps the pre-port text and exit code.

    Run one session with recorded bars, then skip the next session and run the
    one after: its predecessor was never run, so the runner refuses with
    SessionIncompleteError. The thin client must print the same
    ``eod: refused:`` line the frozen oracle prints and exit 2, and the
    refusal must be the recorded terminal state of its own admission.
    """
    from datetime import timedelta

    plugin = RecordingMarketData()
    monkeypatch.setattr(
        "trade_engine.eod.cli.discover_plugins",
        lambda: DiscoveredPlugins(strategies=(), signal_adapters=(), market_data=(plugin,)),
    )
    first = date(2026, 9, 23)
    skipped = date(2026, 9, 24)
    after_skipped = date(2026, 9, 25)
    ledger_path = tmp_path / "refused.db"
    with Ledger(ledger_path) as ledger:
        ledger.set_meta("probed", "1")
    base = ["eod", "--ledger", str(ledger_path), "--accounts", "cli-account"]
    # The first session records its bars; the account now has a marker.
    assert main([*base, "--session", first.isoformat()]) == 0
    assert "cli-account: bars=" in capsys.readouterr().out
    # The session after the skipped one demands its predecessor.
    code = main([*base, "--session", after_skipped.isoformat()])
    captured = capsys.readouterr()
    assert code == 2
    assert captured.err.startswith("eod: refused: ")
    assert str(skipped) in captured.err
    assert "no eod marker" in captured.err

    # The frozen oracle refuses identically on its own ledger.
    frozen = _load_frozen_cli()
    frozen_eod = sys.modules.get("tests.frozen_p4c.t9_eod_cli")
    if frozen_eod is None:
        spec = importlib.util.spec_from_file_location(
            "tests.frozen_p4c.t9_eod_cli", FROZEN_DIR / "t9_eod_cli.py"
        )
        frozen_eod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = frozen_eod
        spec.loader.exec_module(frozen_eod)
    monkeypatch.setattr(
        frozen_eod, "discover_plugins",
        lambda: DiscoveredPlugins(strategies=(), signal_adapters=(), market_data=(plugin,)),
    )
    oracle_path = tmp_path / "oracle-refused.db"
    with Ledger(oracle_path) as ledger:
        ledger.set_meta("probed", "1")
    oracle_base = ["eod", "--ledger", str(oracle_path), "--accounts", "cli-account"]
    assert frozen.main([*oracle_base, "--session", first.isoformat()]) == 0
    capsys.readouterr()
    frozen_code = frozen.main([*oracle_base, "--session", after_skipped.isoformat()])
    oracle_captured = capsys.readouterr()
    assert frozen_code == 2
    # The thin client's refusal text matches the oracle's exactly.
    assert captured.err == oracle_captured.err

    # The refusal was recorded as the admission's terminal state.
    import json
    import sqlite3

    connection = sqlite3.connect(ledger_path)
    rows = connection.execute(
        "SELECT value FROM meta WHERE key LIKE 'te.runtime.jobs.v1:%'"
    ).fetchall()
    connection.close()
    assert rows, "the refusing invocation must still be durably admitted"
    record = json.loads(rows[-1][0])
    assert record["state"] == "refused"
    assert "no eod marker" in record["error"]["message"]
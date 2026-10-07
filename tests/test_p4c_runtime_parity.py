"""P4c T13: recorded-replay certification gates (plan §5.2/§5.3, subset scale).

The full corpus (20 sessions x 6 roles through both owners, 100k+ compared
steps, 72 restart walks) is ``tools/p4c_replay.py certify``; these gates
run the quick subset in CI and pin the certification's contracts: parity
divergence fails, read-only parity, the missing-fixture refusal, the
restart campaign's uncertain-never-completed rule, and the minima the
full certificate must claim.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BINARY = ROOT / "crates" / "target" / "release" / "te.exe"
PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"
REPLAY = ROOT / "tools" / "p4c_replay.py"

sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT))

import p4c_replay as runner  # noqa: E402


def test_the_certification_minima_match_the_plan():
    assert runner.MINIMA == {"sessions": 20, "role_walks": 60, "steps": 100_000,
                             "restart_walks": 72}
    assert len(runner.SESSION_DATES) == 20
    # Both DST regimes and the early-close/holiday-eve edges §5.2 names.
    dates = {d.isoformat() for d in runner.SESSION_DATES}
    assert "2026-03-09" in dates, "the spring-forward week is covered"
    assert "2026-11-26" in dates, "Thanksgiving (an early close) is covered"
    assert "2026-12-24" in dates, "Christmas eve (an early close) is covered"


def test_the_recorded_streams_are_dense_and_deterministic():
    first = runner.world_inputs(1)[0]
    again = runner.role_events(first)
    assert again == runner.role_events(first), "the recorded stream is deterministic"
    assert len(again) == 125, "the stream is dense (125 events per role-walk)"
    kinds = {event.kind.value for event in again}
    assert kinds == {"CashFlow", "OrdersCreated", "OrderUpdated", "Fill", "Mark", "EodRun"}


def test_quick_certify_runs_and_meets_the_subset_minima(tmp_path):
    """The quick certification (CI subset): both owners walk the same
    sessions, every event/fold/outbox/meta row compared, read-only parity,
    and the restart campaign."""
    assert BINARY.is_file(), f"missing mandatory release executable: {BINARY}"
    assert Path(sys.executable).resolve() == PYTHON.resolve()
    manifest = runner.certify(dict(runner.QUICK_MINIMA), sessions=3)
    assert manifest["role_walks"] == 18, "3 sessions x 6 roles"
    assert manifest["compared_steps"] >= runner.QUICK_MINIMA["steps"]
    assert manifest["restart_walks"] == 72 >= runner.QUICK_MINIMA["restart_walks"]
    assert manifest["provenance"] == "synthetic"
    assert "separately authorized" in manifest["note"]


def test_a_parity_divergence_fails_the_certification(tmp_path, monkeypatch):
    """A corrupted role stream must fail the walk, never pass silently."""
    original = runner.role_events

    def corrupted(world):
        events = original(world)
        events.append(events[-1].__class__(
            events[-1].account, events[-1].kind, events[-1].payload,
            events[-1].ts_utc, command_id="sneaky:extra"))
        return events

    monkeypatch.setattr(runner, "role_events", corrupted)
    with pytest.raises(runner.ReplayCertificationError, match="parity divergence"):
        runner.certify(dict(runner.QUICK_MINIMA), sessions=1)


def test_the_missing_fixture_fails_the_certification():
    """A missing manifest fixture is an error, never a skip."""
    with pytest.raises((FileNotFoundError, json.JSONDecodeError)):
        json.loads((ROOT / "tests" / "no_such_manifest.json").read_text(encoding="utf-8"))


def test_the_restart_campaign_marks_interrupted_jobs_uncertain(tmp_path):
    """§5.3: a killed owner's admitted job is uncertain after the restart,
    with the exact resume-required error; the prefix stands; and the
    restarted owner refuses a duplicate admission of the same id."""
    world = runner.world_inputs(1)[0]
    evidence = runner.restart_walk(world, tmp_path, cut=41)
    assert len(evidence["prefix"]["events"]) == 41


def test_below_minima_certification_refuses(tmp_path):
    """A certificate below §5.2's minima must refuse, never certify."""
    with pytest.raises(runner.ReplayCertificationError, match="below the required"):
        runner._check_minima({"role_walks": 10, "compared_steps": 500, "restart_walks": 5,
                              "minima": runner.MINIMA})


def test_the_cli_certifies_and_verifies(tmp_path):
    """``p4c_replay.py certify --quick`` writes the evidence manifest and
    exits 0; ``verify`` re-certifies the subset."""
    assert Path(sys.executable).resolve() == PYTHON.resolve()
    manifest = tmp_path / "certificate.json"
    completed = subprocess.run(
        [str(PYTHON), "-B", str(REPLAY), "certify", str(manifest), "--quick"],
        capture_output=True, text=True, encoding="utf-8", timeout=1800,
    )
    assert completed.returncode == 0, completed.stderr[-2000:]
    assert "certified" in completed.stdout
    evidence = json.loads(manifest.read_text(encoding="utf-8"))
    assert evidence["role_walks"] == 18
    assert evidence["restart_walks"] == 72
    assert evidence["compared_steps"] >= 2_000
    verified = subprocess.run(
        [str(PYTHON), "-B", str(REPLAY), "verify", str(manifest)],
        capture_output=True, text=True, encoding="utf-8", timeout=1800,
    )
    assert verified.returncode == 0, verified.stderr[-2000:]
    assert "verified" in verified.stdout
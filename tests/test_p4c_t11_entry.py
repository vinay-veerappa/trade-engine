"""P4C-T11 runtime pass/through forwarding: the configured entry sees them.

The one entry signature stays ``(ledger, session, options)``; a pass job's
name and through-time ride inside the options the entry receives. This pins
the ``factory_job_result`` payload composition without any trading client.
"""

from __future__ import annotations

import json
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BINARY = ROOT / "crates" / "target" / "release" / "te.exe"
PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"


def test_pass_and_through_ride_in_the_entry_options(tmp_path: Path) -> None:
    assert BINARY.is_file(), f"missing mandatory release executable: {BINARY}"
    assert Path(sys.executable).resolve() == PYTHON.resolve()
    capability = tmp_path / "capability.txt"
    capability.write_text("t11-forward-capability", encoding="utf-8")
    offline = tmp_path / "offline"
    offline.mkdir()
    probes = tmp_path / "probes"
    probes.mkdir()
    # The entry echoes (session, options) and the injected ledger's path so
    # the test proves what the owner handed over, with no trading client.
    (probes / "t11_echo_entry.py").write_text(
        "def echo_entry(ledger, session, options):\n"
        "    return {'session': session.isoformat(), 'options': options, 'ledger': ledger.path.name}\n",
        encoding="utf-8",
    )
    config = {
        "mode": "runtime-owner",
        "python_home": sys.base_prefix,
        "python_dll": str(BINARY.parent / "python313.dll"),
        "python_executable": str(sys.executable),
        "site_packages": str(ROOT / ".venv" / "Lib" / "site-packages"),
        "engine_source": str(ROOT / "src"),
        "plugin_paths": [str(probes)],
        "plugin_module": "t11_echo_entry",
        "plugin_factory": "missing_factory_is_never_called",
        "plugin_config": {},
        "owner": {"ledger_path": str(offline / "echo.db"), "clock": "wall"},
        "runtime": {
            "role": "eod",
            "jobs": ["daily"],
            "record_limit": 64,
            "stop_timeout_seconds": 30.0,
            "capability": str(capability),
            "port": 0,
            "entry_module": "t11_echo_entry",
            "entry": "echo_entry",
        },
    }
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    proc = subprocess.Popen(
        [str(BINARY), "serve", "--config", str(path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    try:
        ready = json.loads(proc.stdout.readline())
        assert ready["serving"] is True
        port = ready["port"]

        def post(body: dict) -> dict:
            request = urllib.request.Request(
                f"http://127.0.0.1:{port}/v1/runtime/jobs",
                data=json.dumps(body).encode("utf-8"),
                method="POST",
                headers={
                    "Host": "127.0.0.1",
                    "X-TE-Capability": "t11-forward-capability",
                },
            )
            return json.loads(urllib.request.urlopen(request, timeout=30).read())

        admitted = post(
            {
                "version": 1,
                "request_id": "echo-1",
                "generation": ready["generation"],
                "role": "eod",
                "job": "daily",
                "session": "2026-10-05",
                "pass": "midday",
                "through": "2026-10-05T12:45:00+00:00",
                "options": {"accounts": ["OPT_CSP"]},
            }
        )
        assert admitted["request_id"] == "echo-1"
        import time as time_module

        for _ in range(200):
            record = json.loads(
                urllib.request.urlopen(
                    urllib.request.Request(
                        f"http://127.0.0.1:{port}/v1/runtime/jobs/echo-1",
                        headers={
                            "Host": "127.0.0.1",
                            "X-TE-Capability": "t11-forward-capability",
                        },
                    ),
                    timeout=30,
                ).read()
            )
            if record["state"] in ("completed", "refused", "failed", "uncertain"):
                break
            time_module.sleep(0.05)
        assert record["state"] == "completed", record
        result = record["result"]
        assert result["session"] == "2026-10-05"
        # The injected ledger reports its real configured path.
        assert result["ledger"] == "echo.db"
        # The pass name and the through-time ride inside the options the
        # entry received, next to the job's own options.
        assert result["options"]["pass"] == "midday"
        assert result["options"]["through"] == "2026-10-05T12:45:00+00:00"
        assert result["options"]["accounts"] == ["OPT_CSP"]
    finally:
        proc.stdin.close()
        proc.wait(timeout=60)
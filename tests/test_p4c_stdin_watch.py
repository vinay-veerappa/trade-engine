"""The owner's wait for stdin to close must not park a read on the pipe.

A synchronous read held on the stdin pipe blocks every other synchronous call on it, so a job
that touches fd 0 through the C runtime (numpy's OpenBLAS load does: ``fstat`` ``PeekNamedPipe``s
a pipe) hung in ``LoadLibrary`` for good. The probe makes that exact call from a job thread.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BINARY = ROOT / "crates" / "target" / "release" / "te.exe"
PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"
CAPABILITY = "stdin-watch-capability"


def _config(tmp_path: Path) -> Path:
    capability = tmp_path / "capability.txt"
    capability.write_text(CAPABILITY, encoding="utf-8")
    offline = tmp_path / "offline"
    offline.mkdir()
    probes = tmp_path / "probes"
    probes.mkdir()
    (probes / "stdin_probe_entry.py").write_text(
        "def probe_entry(ledger, session, options):\n"
        "    import ctypes\n"
        "    buffer = ctypes.create_string_buffer(256)\n"
        "    return {'fstat_stdin': ctypes.cdll.ucrtbase._fstat64(0, buffer)}\n",
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
        "plugin_module": "stdin_probe_entry",
        "plugin_factory": "missing_factory_is_never_called",
        "plugin_config": {},
        "owner": {"ledger_path": str(offline / "probe.db"), "clock": "wall"},
        "runtime": {
            "role": "eod",
            "jobs": ["daily"],
            "record_limit": 64,
            "stop_timeout_seconds": 5.0,
            "capability": str(capability),
            "port": 0,
            "entry_module": "stdin_probe_entry",
            "entry": "probe_entry",
        },
    }
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    return path


def _get(port: int, route: str) -> dict:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{route}",
        headers={"Host": "127.0.0.1", "X-TE-Capability": CAPABILITY},
    )
    return json.loads(urllib.request.urlopen(request, timeout=30).read())


def test_a_job_can_touch_stdin_while_the_owner_waits_on_it(tmp_path: Path) -> None:
    assert BINARY.is_file(), f"missing mandatory release executable: {BINARY}"
    assert Path(sys.executable).resolve() == PYTHON.resolve()
    proc = subprocess.Popen(
        [str(BINARY), "serve", "--config", str(_config(tmp_path))],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    try:
        ready = json.loads(proc.stdout.readline())
        port = ready["port"]
        submit = urllib.request.Request(
            f"http://127.0.0.1:{port}/v1/runtime/jobs",
            data=json.dumps(
                {
                    "version": 1,
                    "request_id": "probe-1",
                    "generation": ready["generation"],
                    "role": "eod",
                    "job": "daily",
                    "session": "2026-10-05",
                    "options": {},
                }
            ).encode("utf-8"),
            method="POST",
            headers={"Host": "127.0.0.1", "X-TE-Capability": CAPABILITY},
        )
        urllib.request.urlopen(submit, timeout=30).read()
        deadline = time.monotonic() + 30
        record = _get(port, "/v1/runtime/jobs/probe-1")
        while record["state"] not in ("completed", "refused", "failed", "uncertain"):
            assert time.monotonic() < deadline, f"the job hung on stdin: {record}"
            time.sleep(0.1)
            record = _get(port, "/v1/runtime/jobs/probe-1")
        assert record["state"] == "completed", record
        assert record["result"] == {"fstat_stdin": 0}
        # Closing stdin still stops the owner cleanly.
        proc.stdin.close()
        assert proc.wait(timeout=30) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=30)


def test_stdin_that_is_not_a_pipe_still_ends_the_serve(tmp_path: Path) -> None:
    assert BINARY.is_file(), f"missing mandatory release executable: {BINARY}"
    proc = subprocess.Popen(
        [str(BINARY), "serve", "--config", str(_config(tmp_path))],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    try:
        assert proc.wait(timeout=60) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=30)

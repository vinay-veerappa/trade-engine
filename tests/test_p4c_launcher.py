"""The owner launcher hosts a real owner: it holds stdin open, publishes the address, stops on request.

A scheduled task gives a process NUL for stdin, which ends the serve at once, so the launcher is
what keeps an owner alive. This runs the launcher itself against a throwaway owner config.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

from test_p4c_stdin_watch import BINARY, ROOT, _config

LAUNCHER = ROOT / "launch" / "runtime" / "run_runtime_owner.ps1"


def _wait_for(path: Path, timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists():
        assert time.monotonic() < deadline, f"{path.name} never appeared"
        time.sleep(0.2)


def test_the_launcher_holds_the_owner_up_publishes_its_address_and_stops_it(tmp_path: Path) -> None:
    assert BINARY.is_file(), f"missing mandatory release executable: {BINARY}"
    config = _config(tmp_path)
    environment = {**os.environ, "TE_BINARY": str(BINARY)}
    launcher = subprocess.Popen(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(LAUNCHER),
         "-Config", str(config), "-Role", "probe"],
        stdin=subprocess.DEVNULL,  # what a scheduled task gives it
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=environment,
    )
    try:
        endpoints = tmp_path / "logs" / "trade_engine" / "runtime-owners"
        _wait_for(endpoints / "probe.json")
        endpoint = json.loads((endpoints / "probe.json").read_text(encoding="utf-8-sig"))
        assert endpoint["role"] == "probe"
        assert endpoint["port"] > 0
        assert endpoint["generation"].startswith("gen:")
        assert endpoint["config"] == str(config)
        # NUL stdin did not end the serve: the launcher holds the owner's pipe.
        time.sleep(2)
        assert launcher.poll() is None, "the owner ended on its own"

        (endpoints / "probe.stop").write_text("", encoding="utf-8")
        assert launcher.wait(timeout=60) == 0
        assert not (endpoints / "probe.json").exists(), "a stopped owner leaves no address behind"
        assert not (endpoints / "probe.stop").exists()
        log = next((tmp_path / "logs" / "trade_engine").glob("RuntimeOwner_probe_*.log"))
        text = log.read_text(encoding="utf-8-sig")
        assert '"serving":true' in text and "runtime-owner exit 0 role=probe" in text
    finally:
        if launcher.poll() is None:
            launcher.kill()
            launcher.wait(timeout=30)

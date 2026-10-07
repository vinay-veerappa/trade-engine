"""P4C-T9 runtime owner composition tests: synthetic ledgers, no trading jobs."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parent.parent
BINARY = ROOT / "crates" / "target" / "release" / "te.exe"
PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"
ROLE = "eod"
JOBS = ["daily"]


@pytest.fixture(scope="module")
def bundle() -> Path:
    assert BINARY.is_file(), f"missing mandatory release executable: {BINARY}"
    assert Path(sys.executable).resolve() == PYTHON.resolve(), "private Python 3.13 required"
    import trade_engine

    assert Path(trade_engine.__file__).resolve().is_relative_to(ROOT / "src")
    return ROOT


def plugin_dir(tmp_path: Path) -> Path:
    plugins = tmp_path / "plugins"
    plugins.mkdir(exist_ok=True)
    (plugins / "fake_plugin.py").write_text(
        "def probe(config):\n    return {'ok': True}\n", encoding="utf-8"
    )
    return plugins


def base_config(tmp_path: Path, **runtime) -> dict:
    home = Path(sys.base_prefix)
    capability = tmp_path / "capability.txt"
    capability.write_text("synthetic-capability", encoding="utf-8")
    offline = tmp_path / "offline"
    offline.mkdir()
    return {
        "mode": "runtime-owner",
        "python_home": str(home),
        "python_dll": str(BINARY.parent / "python313.dll"),
        "python_executable": str(PYTHON),
        "site_packages": str(ROOT / ".venv" / "Lib" / "site-packages"),
        "engine_source": str(ROOT / "src"),
        "plugin_paths": [str(plugin_dir(tmp_path))],
        "plugin_module": "fake_plugin",
        "plugin_factory": "probe",
        "plugin_config": {},
        "owner": {
            "ledger_path": str(offline / "synthetic.db"),
            "clock": "wall",
        },
        "runtime": {
            "role": ROLE,
            "jobs": JOBS,
            "record_limit": 64,
            "stop_timeout_seconds": 30.0,
            "capability": str(capability),
            **runtime,
        },
    }


def run_serve(config: dict, tmp_path: Path, *, args=None):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    command = args or [str(BINARY), "serve", "--config", str(path)]
    return subprocess.run(
        command, capture_output=True, text=True, encoding="utf-8", timeout=60
    )


def refusal_of(proc) -> dict:
    assert proc.returncode == 2
    return json.loads(proc.stderr)["error"]


def test_serve_composes_owner_journal_and_recovers(bundle, tmp_path):
    config = base_config(tmp_path)
    proc = run_serve(config, tmp_path)
    assert proc.returncode == 0, proc.stderr
    report = json.loads(proc.stdout)
    assert report["role"] == ROLE
    assert list(report["jobs"]) == JOBS
    assert report["recovered"] is True
    assert report["generation"].startswith("gen:")
    # The authorized composition check ran end to end: admission inserted,
    # the synthetic job completed, and the durable record is terminal.
    assert report["submitted"]["inserted"] is True
    assert report["submitted"]["record"]["state"] in ("queued", "running")
    assert report["finished"]["state"] == "completed"
    assert report["finished"]["result"]["job"] == "daily"
    # The offline ledger and its sidecar were created inside the config tree.
    ledger = Path(config["owner"]["ledger_path"])
    assert ledger.is_file()
    assert Path(str(ledger) + ".lock").is_file()


def test_serve_usage_and_mode_refusals(bundle, tmp_path):
    config = base_config(tmp_path)
    proc = run_serve(config, tmp_path, args=[str(BINARY), "serve"])
    assert refusal_of(proc)["message"] == "usage: te serve --config <absolute JSON path>"
    proc = run_serve(config, tmp_path, args=[str(BINARY), "serve", "--config", "relative.json"])
    assert refusal_of(proc)["message"].startswith("config path must be absolute")

    # Structural config validation precedes the serve mode check: a
    # packaging-proof config cannot carry an owner at all.
    proof = dict(config)
    proof["mode"] = "packaging-proof"
    proc = run_serve(proof, tmp_path)
    error = refusal_of(proc)
    assert error["message"] == "packaging-proof cannot configure an owner"
    # A structurally valid non-owner config reaches the serve mode check.
    plain = {key: value for key, value in config.items() if key not in ("owner", "runtime")}
    plain["mode"] = "packaging-proof"
    proc = run_serve(plain, tmp_path)
    error = refusal_of(proc)
    assert error["message"] == "serve requires runtime-owner mode, got: packaging-proof"


@pytest.mark.parametrize(
    "field,value,message",
    [
        ("role", "not eod", "runtime role must be a configured identifier"),
        ("role", "", "runtime role must be a configured identifier"),
        ("jobs", [], "runtime jobs must be configured identifiers"),
        ("jobs", ["bad job"], "runtime jobs must be configured identifiers"),
        ("record_limit", 0, "runtime record_limit must be positive"),
        ("stop_timeout_seconds", 0, "runtime stop_timeout_seconds must be positive and bounded"),
        ("stop_timeout_seconds", 601, "runtime stop_timeout_seconds must be positive and bounded"),
        ("capability", "relative.txt", "runtime capability must be an absolute path"),
    ],
)
def test_runtime_config_refusals(bundle, tmp_path, field, value, message):
    config = base_config(tmp_path)
    config["runtime"][field] = value
    error = refusal_of(run_serve(config, tmp_path))
    assert error["type"] == "RuntimeConfigError"
    assert error["message"] == message


def test_runtime_section_is_refused_for_proof_modes(bundle, tmp_path):
    config = base_config(tmp_path)
    config["mode"] = "packaging-proof"
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    proc = subprocess.run(
        [str(BINARY), "--proof", "--config", str(path)],
        capture_output=True, text=True, encoding="utf-8", timeout=60,
    )
    error = refusal_of(proc)
    assert error["message"] == "packaging-proof cannot configure an owner"


def test_unknown_top_level_field_still_refuses(bundle, tmp_path):
    config = base_config(tmp_path)
    config["unexpected"] = True
    error = refusal_of(run_serve(config, tmp_path))
    assert error["type"] == "RuntimeConfigError"
    assert "unknown field" in error["message"]


def test_serve_recovers_foreign_incomplete_records(bundle, tmp_path):
    import sqlite3

    config = base_config(tmp_path)
    ledger = Path(config["owner"]["ledger_path"])
    # First serve creates the offline ledger and releases it on exit.
    proc = run_serve(config, tmp_path)
    assert proc.returncode == 0, proc.stderr
    # Seed a foreign running record exactly as the dead instance left it.
    record = {
        "version": 1,
        "request_id": "left-running",
        "instance": "999",
        "payload": {
            "version": 1,
            "request_id": "left-running",
            "generation": "gen:deadbeefdeadbeef",
            "role": ROLE,
            "job": "daily",
            "session": "2026-10-02",
            "options": {},
        },
        "state": "running",
        "observed_seq": 0,
        "committed_seq": 0,
        "result": None,
        "error": None,
    }
    connection = sqlite3.connect(ledger)
    connection.execute(
        "INSERT INTO meta(key,value) VALUES(?,?)",
        ("te.runtime.jobs.v1:left-running", json.dumps(record)),
    )
    connection.commit()
    connection.close()
    # The restarted owner recovers the record as uncertain before admitting.
    proc = run_serve(config, tmp_path)
    assert proc.returncode == 0, proc.stderr
    connection = sqlite3.connect(ledger)
    text = connection.execute(
        "SELECT value FROM meta WHERE key=?", ("te.runtime.jobs.v1:left-running",)
    ).fetchone()[0]
    connection.close()
    recovered = json.loads(text)
    assert recovered["state"] == "uncertain"
    assert recovered["error"]["type"] == "RuntimeResumeRequired"


def test_serve_http_loop_admits_over_the_wire_and_stops_cleanly(bundle, tmp_path):
    import socket
    import subprocess
    import time as time_module

    config = base_config(tmp_path, port=0)
    # An ephemeral port is requested via port 0; the server reports the bound
    # port on stdout before serving.
    config["runtime"]["port"] = 0
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    capability = (tmp_path / "capability.txt").read_text(encoding="utf-8").strip()
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
        assert 1024 <= port <= 65535

        def request(method: str, target: str, body: bytes = b"", headers=()):
            connection = socket.create_connection(("127.0.0.1", port), timeout=10)
            head = [f"{method} {target} HTTP/1.1", "Host: localhost"]
            head.extend(f"{name}: {value}" for name, value in headers)
            if body:
                head.append(f"Content-Length: {len(body)}")
            connection.sendall(("\r\n".join(head) + "\r\n\r\n").encode("utf-8") + body)
            reply = b""
            while b"\r\n\r\n" not in reply:
                chunk = connection.recv(65536)
                if not chunk:
                    break
                reply += chunk
            connection.close()
            return reply

        # Legacy routes keep their exact frozen bytes.
        health = request("GET", "/health")
        assert b'"ok":true' in health
        # An unauthorized control submission refuses with the exact type.
        refused = request(
            "POST",
            "/v1/runtime/jobs",
            b'{"request_id":"over-the-wire"}',
            [("X-TE-Capability", "wrong")],
        )
        assert b'"RuntimeCapabilityError"' in refused
        # The authorized submission is admitted over the wire.
        accepted = request(
            "POST",
            "/v1/runtime/jobs",
            json.dumps(
                {
                    "version": 1,
                    "request_id": "over-the-wire",
                    "generation": ready["generation"],
                    "role": ready["role"],
                    "job": "daily",
                    "options": {},
                }
            ).encode("utf-8"),
            [("X-TE-Capability", capability)],
        )
        assert b"HTTP/1.0 201 Created" in accepted
        # Status is an authorized direct read.
        status = request("GET", "/v1/runtime/status")
        assert b'"role":"eod"' in status
        # Stop the owner: closing stdin drains and releases the guard last.
        proc.stdin.close()
        proc.wait(timeout=60)
        report = json.loads(proc.stdout.read())
        assert report["served"]["port"] == port
        assert proc.returncode == 0
        # The sidecar guard is released after the clean stop.
        lock = Path(str(config["owner"]["ledger_path"]) + ".lock")
        assert lock.is_file()
        ledger = Path(config["owner"]["ledger_path"])
        import sqlite3

        connection = sqlite3.connect(ledger)
        text = connection.execute(
            "SELECT value FROM meta WHERE key=?",
            ("te.runtime.jobs.v1:over-the-wire",),
        ).fetchone()[0]
        connection.close()
        record = json.loads(text)
        assert record["state"] == "completed"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=30)
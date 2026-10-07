"""Regression proof for the approved timing client, without modifying the oracle."""
import os
import json
from pathlib import Path
import subprocess
import sys

import pytest

from tools import time_p4c_t2 as timing


def test_connect_is_blocking_then_read_timeout_is_restored(monkeypatch):
    seen = []
    class Peer:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            seen.append("closed")
        def setblocking(self, blocking):
            seen.append(("blocking", blocking))
        def connect(self, address):
            seen.append(("connect", address))
        def settimeout(self, timeout):
            seen.append(("timeout", timeout))
    peer = Peer()
    monkeypatch.setattr(timing.socket, "socket", lambda *args: peer)
    assert timing.blocking_connect(("127.0.0.1", 50000), timeout=5) is peer
    assert seen == [("blocking", True), ("connect", ("127.0.0.1", 50000)), ("timeout", 5)]


def test_failed_connect_closes_socket_and_preserves_error(monkeypatch):
    seen = []
    class Peer:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            seen.append("closed")
        def setblocking(self, blocking):
            pass
        def connect(self, address):
            raise ConnectionRefusedError("diagnostic refusal")
    monkeypatch.setattr(timing.socket, "socket", lambda *args: Peer())
    with pytest.raises(ConnectionRefusedError, match="diagnostic refusal"):
        timing.blocking_connect(("127.0.0.1", 50000), timeout=5)
    assert seen == ["closed"]


@pytest.mark.parametrize("address", [("0.0.0.0", 50000), ("127.0.0.1", 3410),
    ("127.0.0.1", 3411), ("127.0.0.1", 8097)])
def test_timing_client_refuses_live_or_non_loopback_ports(address):
    with pytest.raises(ValueError, match="isolated loopback"):
        timing.blocking_connect(address, timeout=5)


def test_watchdog_reaps_hung_child(tmp_path):
    pid_file = tmp_path / "watchdog.pid"
    child = [
        sys.executable, "-B", "-c",
        "import os,time;from pathlib import Path;"
        "Path(__import__('sys').argv[1]).write_text(str(os.getpid()));time.sleep(60)",
        str(pid_file),
    ]
    with pytest.raises(RuntimeError, match="killed and reaped") as error:
        timing.bounded(child, timeout=1)
    assert isinstance(error.value.__cause__, subprocess.TimeoutExpired)
    assert pid_file.is_file()
    pid = int(pid_file.read_text())
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.GetExitCodeProcess.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        handle = kernel.OpenProcess(0x1000, False, pid)
        if handle:
            try:
                code = wintypes.DWORD()
                assert kernel.GetExitCodeProcess(handle, ctypes.byref(code))
                assert code.value != 259
            finally:
                assert kernel.CloseHandle(handle)
        else:
            assert ctypes.get_last_error() == 87
    else:
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)


def test_watchdog_child_keeps_private_interpreter_and_engine():
    result = timing.bounded([sys.executable, "-B", "-c",
        "import sys,json,trade_engine,trade_engine_rs;"
        "print(json.dumps([sys.executable,sys.prefix,trade_engine.__file__,trade_engine_rs.__file__]))"])
    assert result.returncode == 0, result.stderr
    executable, prefix, engine, extension = json.loads(result.stdout)
    assert Path(executable).resolve() == Path(sys.executable).resolve()
    assert Path(prefix).resolve() == timing.ROOT / ".venv"
    assert Path(engine).resolve().is_relative_to(timing.ROOT / "src")
    assert Path(extension).resolve().is_relative_to(timing.ROOT / ".venv")


def test_timing_worker_refuses_unmanaged_fixture_root(tmp_path):
    with pytest.raises(ValueError, match="parent-managed temporary"):
        timing.measured(tmp_path)
    assert not list(tmp_path.iterdir())

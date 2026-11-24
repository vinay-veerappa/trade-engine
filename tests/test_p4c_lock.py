"""Single-instance ledger lock: explicit behaviour of ``trade_engine.ledger.lock``.

The lock is a sidecar ``<ledger>.lock`` held by the OS for the life of the process. These
tests assert literal outcomes: acquisition, the I4 refusal text, release, context-manager
hand-over, PID recording, stale sidecars, and invalid paths. Child processes exercise the
same contract across real process boundaries.
"""

from __future__ import annotations

import gc
import json
import os
from pathlib import Path
import signal
import subprocess
import sys

import pytest
import trade_engine_rs  # noqa: F401 - missing extension is an error, not a skip

from trade_engine.ledger.lock import SingleInstanceLock
from trade_engine.ledger.store import Ledger

ROOT = Path(__file__).resolve().parent.parent
CHILD = """
import json, os, sys
from pathlib import Path
from trade_engine.ledger.lock import SingleInstanceLock
lock = SingleInstanceLock(Path(sys.argv[1]))
if sys.argv[2] == 'race':
    print('ready', flush=True)
    sys.stdin.readline()
try:
    lock.acquire()
    print(json.dumps(['ok', lock.held, os.getpid()]), flush=True)
except Exception as exc:
    print(json.dumps(['err', type(exc).__name__, str(exc)]), flush=True)
if sys.argv[2] in ('hold', 'race'):
    sys.stdin.readline()
lock.release()
"""


def outcome(call):
    try:
        return ("ok", call())
    except Exception as exc:
        return ("err", type(exc).__name__, str(exc))


def child(path, mode="probe"):
    return subprocess.Popen(
        [sys.executable, "-B", "-c", CHILD, str(path), mode],
        cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True,
    )


def probe(path):
    proc = child(path)
    try:
        out, err = proc.communicate(timeout=15)
        assert proc.returncode == 0, err
        return json.loads(out)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=15)


def refused(path):
    return ["err", "LedgerLockError",
            f"Another process already holds the ledger lock at {path}.lock (I4). "
            "Refusing to start a second writer."]


def test_contention_release_and_hand_over_in_one_process(tmp_path):
    path = tmp_path / "book.db"
    first, second, third = (SingleInstanceLock(path) for _ in range(3))

    assert not first.held
    first.acquire()
    assert first.held

    assert outcome(second.acquire) == tuple(refused(path))
    assert not second.held

    assert outcome(third.acquire) == tuple(refused(path))
    assert not third.held

    first.release()
    assert not first.held

    second.acquire()
    assert second.held
    assert outcome(first.acquire) == tuple(refused(path))
    assert not first.held

    second.release()
    with third:
        assert third.held
    assert not third.held


@pytest.mark.parametrize("death", [False, True])
def test_process_release_and_death_hand_over(tmp_path, death):
    path = tmp_path / "book.db"
    proc = child(path, mode="hold")
    actual_pid = None
    try:
        ready = json.loads(proc.stdout.readline())
        assert ready[:2] == ["ok", True]
        actual_pid = ready[2]  # Windows venv launcher has a different PID.
        assert probe(path) == refused(path)
        if death:
            os.kill(actual_pid, signal.SIGTERM)
            proc.wait(timeout=15)
        else:
            proc.communicate("\n", timeout=15)
            assert proc.returncode == 0
        assert probe(path)[:2] == ["ok", True]
    finally:
        if proc.poll() is None:
            if actual_pid is not None:
                os.kill(actual_pid, signal.SIGTERM)
            else:
                proc.kill()
            proc.wait(timeout=15)


@pytest.mark.parametrize("contenders", [2, 3])
def test_simultaneous_acquisition_races(tmp_path, contenders):
    ok = err = 0
    for seed in range(8):
        path = tmp_path / str(seed) / "nested" / "book.db"
        processes = [child(path, mode="race") for _ in range(contenders)]
        try:
            for proc in processes:
                assert proc.stdout.readline().strip() == "ready"
            for proc in processes:
                proc.stdin.write("\n")
                proc.stdin.flush()
            results = [json.loads(proc.stdout.readline()) for proc in processes]
            # Exactly one winner per race; every loser gets the I4 refusal text.
            assert sorted(result[0] for result in results) == ["err"] * (contenders - 1) + ["ok"]
            assert next(result for result in results if result[0] == "ok")[1] is True
            for result in results:
                if result[0] == "err":
                    assert result[:2] == ["err", "LedgerLockError"]
                    assert result[2].startswith(f"Another process already holds the ledger lock at {path}.lock")
            ok += sum(result[0] == "ok" for result in results)
            err += sum(result[0] == "err" for result in results)
        finally:
            for proc in processes:
                if proc.poll() is None:
                    proc.communicate("\n", timeout=15)
                assert proc.returncode == 0
        with SingleInstanceLock(path):
            pass
    assert (ok, err) == (8, 8 * (contenders - 1))


def test_aliases_pid_stale_and_context(tmp_path):
    path = tmp_path / "book.db"
    path.with_suffix(".db.lock").write_text("pid=999999999\nstale trailing data\n")
    owner = SingleInstanceLock(path)
    assert not owner.held
    assert owner.__enter__() is owner
    try:
        owner.acquire()
        aliases = [path, tmp_path / "." / "book.db", tmp_path / "x" / ".." / "book.db"]
        if os.path.splitdrive(str(path))[0].lower() == os.path.splitdrive(str(ROOT))[0].lower():
            # A relative spelling exists only on the checkout's own drive (a hosted runner: temp on C:, checkout on D:).
            aliases.append(Path(os.path.relpath(path, ROOT)))
        for alias in aliases:
            (tmp_path / "x").mkdir(exist_ok=True)
            contender = SingleInstanceLock(alias)
            assert outcome(contender.acquire) == tuple(refused(alias))
            assert not contender.held
    finally:
        owner.__exit__(None, None, None)
    assert not owner.held
    assert path.with_suffix(".db.lock").read_bytes() == f"pid={os.getpid()}\n".encode()
    owner.release()
    with SingleInstanceLock(path) as again:
        assert again.held
    assert not again.held


def test_same_file_hardlink_and_windows_case_alias(tmp_path):
    path = tmp_path / "book.db"
    with SingleInstanceLock(path):
        alias = tmp_path / "alias.db"
        os.link(str(path) + ".lock", str(alias) + ".lock")
        assert outcome(SingleInstanceLock(alias).acquire) == tuple(refused(alias))
        if os.name == "nt":
            case = tmp_path / "BOOK.DB"
            assert outcome(SingleInstanceLock(case).acquire) == tuple(refused(case))
    with SingleInstanceLock(alias):
        pass


def test_pid_capture_and_gc_release(tmp_path):
    lock = SingleInstanceLock(tmp_path / "pid.db")
    lock._pid = 123456
    lock.acquire()
    del lock
    gc.collect()
    assert (tmp_path / "pid.db.lock").read_bytes() == b"pid=123456\n"
    with SingleInstanceLock(tmp_path / "pid.db"):
        pass


def test_invalid_paths_exact_errors(tmp_path):
    file = tmp_path / "file"
    file.write_bytes(b"file")
    directory = tmp_path / "dir.db.lock"
    directory.mkdir()
    # (path, exception type on Windows, exception type on POSIX). Only Windows was run on the
    # author's host; the POSIX names are the OS's standard errno classes for these shapes.
    cases = [
        (file / "child" / "book.db", "FileExistsError", "NotADirectoryError"),  # a parent is a file
        (tmp_path / "dir.db", "PermissionError", "IsADirectoryError"),  # the sidecar is a directory
        (tmp_path / "nul\0.db", "ValueError", "ValueError"),  # embedded NUL
    ]
    if os.name == "nt":
        cases += [
            (tmp_path / "invalid*.db", "OSError", None),  # reserved Windows characters
            (tmp_path / "invalid?" / "book.db", "OSError", None),
        ]
    for path, windows_error, posix_error in cases:
        lock = SingleInstanceLock(path)
        result = outcome(lock.acquire)
        expected = windows_error if os.name == "nt" else posix_error
        assert result[:2] == ("err", expected), (path, result)
        assert not lock.held
        lock.release()

    # A missing parent is created: the one valid path acquires.
    nested = SingleInstanceLock(tmp_path / "missing" / "nested" / "book.db")
    assert outcome(nested.acquire) == ("ok", None)
    assert nested.held
    nested.release()
    assert not nested.held


def test_refusal_precedes_any_writable_db_open(tmp_path, monkeypatch):
    path = tmp_path / "book.db"
    owner = SingleInstanceLock(path)
    owner.acquire()
    ledger = Ledger(path)
    calls = []

    def forbidden(*args, **kwargs):
        calls.append(args)
        raise AssertionError("writable sqlite open before lock")

    with monkeypatch.context() as patch:
        patch.setattr("trade_engine.ledger.store.sqlite3.connect", forbidden)
        try:
            assert outcome(ledger.open) == tuple(refused(path))
            assert calls == []
            assert not path.exists()
            assert not ledger._lock.held
        finally:
            ledger.close()
            owner.release()

    # A real SQLite-open failure happens after the guard is acquired, not via an injection seam.
    path.mkdir()
    try:
        assert outcome(ledger.open) == ("err", "OperationalError", "unable to open database file")
        assert not ledger._lock.held
        with SingleInstanceLock(path) as new:
            assert new.held
    finally:
        ledger.close()
        path.rmdir()
    with ledger:
        assert ledger._lock.held
        assert path.is_file()
    assert not ledger._lock.held


def test_failed_open_does_not_poison_handle(tmp_path):
    path = tmp_path / "book.db"
    lock = SingleInstanceLock(path)
    lock.lock_path.mkdir()
    assert outcome(lock.acquire)[0] == "err"
    assert not lock.held
    lock.lock_path.rmdir()
    with lock:
        assert lock.held
    assert not lock.held


def test_open_permission_error_exact(tmp_path):
    path = tmp_path / "readonly.db"
    sidecar = Path(str(path) + ".lock")
    sidecar.write_bytes(b"stale")
    sidecar.chmod(0o444)
    try:
        lock = SingleInstanceLock(path)
        result = outcome(lock.acquire)
        assert not lock.held
        lock.release()
        if os.name == "nt":
            assert result[0:2] == ("err", "PermissionError")
        else:
            assert result[0] in {"ok", "err"}  # POSIX root ignores 0o444; not asserted further
    finally:
        sidecar.chmod(0o666)


def test_unicode_paths_and_null_error_order(tmp_path):
    names = ["日本語.db", "astral-\U0001f680.db"]
    if os.name == "nt":
        names.append("surrogate-\ud800.db")
    for name in names:
        path = tmp_path / name
        with SingleInstanceLock(path):
            assert outcome(SingleInstanceLock(path).acquire) == tuple(refused(path))
    # The directory is created before the NUL is seen, so a valid parent survives a NUL basename.
    for parent_has_null in (False, True):
        root = tmp_path / f"null-{parent_has_null}"
        path = root / ("bad\0parent" if parent_has_null else "valid") / "null\0.db"
        lock = SingleInstanceLock(path)
        assert outcome(lock.acquire)[0:2] == ("err", "ValueError")
        assert not lock.held
        assert (root / "valid").exists() is (not parent_has_null)


def test_windows_held_lock_cannot_be_deleted_or_replaced(tmp_path):
    if os.name != "nt":
        # POSIX flock has always allowed unlink; do not invent stronger behavior.
        return
    path = tmp_path / "book.db"
    with SingleInstanceLock(path) as lock:
        replacement = tmp_path / "replacement.lock"
        replacement.write_bytes(b"replacement")
        unlink = outcome(lock.lock_path.unlink)
        replace = outcome(lambda: replacement.replace(lock.lock_path))
    assert unlink[0:2] == ("err", "PermissionError")
    assert replace[0:2] == ("err", "PermissionError")

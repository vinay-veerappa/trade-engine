"""T1 lockstep: frozen Python and mandatory native lock, synthetic paths only."""
from __future__ import annotations

from collections import Counter
import gc
import json
import os
from pathlib import Path
import random
import signal
import subprocess
import sys

import pytest
import trade_engine_rs  # noqa: F401 - missing extension is an error, not a skip

from frozen_p4c.lock import SingleInstanceLock as OldLock
from trade_engine.ledger.lock import SingleInstanceLock as NewLock
from trade_engine.ledger.store import Ledger

ROOT = Path(__file__).resolve().parent.parent
CHILD = """
import json, os, sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd() / 'tests'))
if sys.argv[1] == 'old':
    from frozen_p4c.lock import SingleInstanceLock
else:
    from trade_engine.ledger.lock import SingleInstanceLock
lock = SingleInstanceLock(Path(sys.argv[2]))
if sys.argv[3] == 'race':
    print('ready', flush=True)
    sys.stdin.readline()
try:
    lock.acquire()
    print(json.dumps(['ok', lock.held, os.getpid()]), flush=True)
except Exception as exc:
    print(json.dumps(['err', type(exc).__name__, str(exc)]), flush=True)
if sys.argv[3] in ('hold', 'race'):
    sys.stdin.readline()
lock.release()
"""


def outcome(call):
    try:
        return ("ok", call())
    except Exception as exc:
        return ("err", type(exc).__name__, str(exc))


def child(kind, path, hold=False, race=False):
    return subprocess.Popen(
        [sys.executable, "-B", "-c", CHILD, kind, str(path),
         "race" if race else "hold" if hold else "probe"],
        cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True,
    )


def probe(kind, path):
    proc = child(kind, path)
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


def test_generated_lockstep(tmp_path):
    tally = Counter()
    rng = random.Random(4101)
    for seed in range(80):
        # Separate oracle/native roots, normalized ONLY in error text.
        roots = [tmp_path / f"old-{seed}", tmp_path / f"new-{seed}"]
        locks = [[cls(root / "nested" / "book.db") for _ in range(3)]
                 for cls, root in zip((OldLock, NewLock), roots)]
        for step in range(100):
            index = rng.randrange(3)
            op = rng.choice(("acquire", "release", "held", "acquire"))
            values = []
            for group, root in zip(locks, roots):
                lock = group[index]
                value = outcome(lambda: getattr(lock, op)() if op != "held" else lock.held)
                if value[0] == "err":
                    value = (value[0], value[1], value[2].replace(str(root), "<root>"))
                values.append(value)
            assert values[0] == values[1], (seed, step, op, values)
            assert [x.held for x in locks[0]] == [x.held for x in locks[1]]
            tally[values[0][0]] += 1
        for group in locks:
            for lock in group:
                lock.release()
    assert tally == {"ok": 5722, "err": 2278}, tally
    print(f"generated lockstep {dict(tally)}")


@pytest.mark.parametrize("owner,contender", [
    ("old", "new"), ("new", "old"), ("new", "new"), ("old", "old"),
])
@pytest.mark.parametrize("death", [False, True])
def test_mixed_processes_release_and_death(tmp_path, owner, contender, death):
    path = tmp_path / "book.db"
    proc = child(owner, path, hold=True)
    actual_pid = None
    try:
        ready = json.loads(proc.stdout.readline())
        assert ready[:2] == ["ok", True]
        actual_pid = ready[2]  # Windows venv launcher has a different PID.
        assert probe(contender, path) == refused(path)
        if death:
            os.kill(actual_pid, signal.SIGTERM)
            proc.wait(timeout=15)
        else:
            proc.communicate("\n", timeout=15)
            assert proc.returncode == 0
        assert probe(contender, path)[:2] == ["ok", True]
    finally:
        if proc.poll() is None:
            if actual_pid is not None:
                os.kill(actual_pid, signal.SIGTERM)
            else:
                proc.kill()
            proc.wait(timeout=15)


@pytest.mark.parametrize("kinds", [("old", "new"), ("new", "old"), ("new", "new")])
def test_simultaneous_acquisition_races(tmp_path, kinds):
    tally = Counter()
    for seed in range(8):
        path = tmp_path / str(seed) / "nested" / "book.db"
        processes = [child(kind, path, race=True) for kind in kinds]
        try:
            for proc in processes:
                assert proc.stdout.readline().strip() == "ready"
            for proc in processes:
                proc.stdin.write("\n")
                proc.stdin.flush()
            results = [json.loads(proc.stdout.readline()) for proc in processes]
            assert sorted(result[0] for result in results) == ["err", "ok"], results
            assert next(result for result in results if result[0] == "err") == refused(path)
            tally.update(result[0] for result in results)
        finally:
            for proc in processes:
                if proc.poll() is None:
                    proc.communicate("\n", timeout=15)
                assert proc.returncode == 0
        with NewLock(path):
            pass
    assert tally == {"ok": 8, "err": 8}


@pytest.mark.parametrize("owner_cls,contender_cls", [
    (OldLock, NewLock), (NewLock, OldLock), (NewLock, NewLock),
])
def test_aliases_pid_stale_and_context(tmp_path, owner_cls, contender_cls):
    path = tmp_path / "book.db"
    path.with_suffix(".db.lock").write_text("pid=999999999\nstale trailing data\n")
    owner = owner_cls(path)
    assert not owner.held
    assert owner.__enter__() is owner
    try:
        owner.acquire()
        for alias in (path, tmp_path / "." / "book.db", tmp_path / "x" / ".." / "book.db",
                      Path(os.path.relpath(path, ROOT))):
            (tmp_path / "x").mkdir(exist_ok=True)
            contender = contender_cls(alias)
            value = outcome(contender.acquire)
            assert value == tuple(refused(alias))
            assert not contender.held
        # Read with the SAME owning handle is tested by the native unit test.
    finally:
        owner.__exit__(None, None, None)
    assert not owner.held
    assert path.with_suffix(".db.lock").read_bytes() == f"pid={os.getpid()}\n".encode()
    owner.release()
    with contender_cls(path) as again:
        assert again.held
    assert not again.held


def test_same_file_hardlink_and_windows_case_alias(tmp_path):
    path = tmp_path / "book.db"
    with NewLock(path):
        alias = tmp_path / "alias.db"
        os.link(str(path) + ".lock", str(alias) + ".lock")
        for cls in (OldLock, NewLock):
            assert outcome(cls(alias).acquire) == tuple(refused(alias))
            if os.name == "nt":
                case = tmp_path / "BOOK.DB"
                assert outcome(cls(case).acquire) == tuple(refused(case))
    with NewLock(alias):
        pass


def test_pid_capture_and_gc_release(tmp_path):
    for cls in (OldLock, NewLock):
        lock = cls(tmp_path / "pid.db")
        lock._pid = 123456
        lock.acquire()
        del lock
        gc.collect()
        assert (tmp_path / "pid.db.lock").read_bytes() == b"pid=123456\n"
        with NewLock(tmp_path / "pid.db"):
            pass


def test_invalid_paths_exact_errors(tmp_path):
    file = tmp_path / "file"
    file.write_bytes(b"file")
    directory = tmp_path / "dir.db.lock"
    directory.mkdir()
    paths = [file / "child" / "book.db", tmp_path / "dir.db",
             tmp_path / "nul\0.db", tmp_path / "missing" / "nested" / "book.db"]
    if os.name == "nt":
        paths.extend([tmp_path / "invalid*.db", tmp_path / "invalid?" / "book.db"])
    tally = Counter()
    for path in paths:
        locks = [cls(path) for cls in (OldLock, NewLock)]
        values = []
        for lock in locks:
            values.append(outcome(lock.acquire))
            lock.release()
        assert values[0] == values[1], (path, values)
        tally[values[0][0]] += 1
        for lock in locks:
            lock.release()
    assert tally["err"] >= 3 and tally["ok"] == 1


def test_refusal_precedes_any_writable_db_open(tmp_path, monkeypatch):
    path = tmp_path / "book.db"
    owner = OldLock(path)
    owner.acquire()
    ledger = Ledger(path)
    calls = []
    def forbidden(*args, **kwargs):
        calls.append(args)
        raise AssertionError("writable sqlite open before lock")
    monkeypatch.setattr("trade_engine.ledger.store.sqlite3.connect", forbidden)
    try:
        assert outcome(ledger.open) == tuple(refused(path))
        assert calls == []
        assert not path.exists()
        assert not ledger._lock.held
    finally:
        ledger.close()
        owner.release()
    assert outcome(ledger.open) == ("err", "AssertionError", "writable sqlite open before lock")
    assert not ledger._lock.held


def test_failed_open_does_not_poison_handle(tmp_path):
    path = tmp_path / "book.db"
    lock = NewLock(path)
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
        results = []
        for cls in (OldLock, NewLock):
            lock = cls(path)
            results.append(outcome(lock.acquire))
            assert not lock.held
            lock.release()
        assert results[0] == results[1]
        if os.name == "nt":
            assert results[0][0:2] == ("err", "PermissionError")
    finally:
        sidecar.chmod(0o666)


def test_unicode_paths_and_null_error_order(tmp_path):
    names = ["日本語.db", "astral-\U0001f680.db"]
    if os.name == "nt":
        names.append("surrogate-\ud800.db")
    for name in names:
        path = tmp_path / name
        with OldLock(path):
            assert outcome(NewLock(path).acquire) == tuple(refused(path))
        with NewLock(path):
            assert outcome(OldLock(path).acquire) == tuple(refused(path))
    for parent_has_null in (False, True):
        outcomes = []
        created = []
        for side, cls in (("old", OldLock), ("new", NewLock)):
            root = tmp_path / f"{side}-{parent_has_null}"
            path = root / ("bad\0parent" if parent_has_null else "valid") / "null\0.db"
            lock = cls(path)
            outcomes.append(outcome(lock.acquire))
            created.append((root / "valid").exists())
            assert not lock.held
        assert outcomes[0] == outcomes[1]
        assert created == [not parent_has_null, not parent_has_null]


def test_windows_held_lock_cannot_be_deleted_or_replaced(tmp_path):
    if os.name != "nt":
        # POSIX flock has always allowed unlink; do not invent stronger behavior.
        return
    outcomes = []
    for cls in (OldLock, NewLock):
        path = tmp_path / "book.db"
        with cls(path) as lock:
            replacement = tmp_path / "replacement.lock"
            replacement.write_bytes(b"replacement")
            outcomes.append((outcome(lock.lock_path.unlink),
                             outcome(lambda: replacement.replace(lock.lock_path))))
    assert outcomes[0] == outcomes[1]
    assert all(item[0:2] == ("err", "PermissionError") for item in outcomes[0])

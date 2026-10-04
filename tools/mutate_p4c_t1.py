"""Compiling T1 hand mutants; baseline, assertion-only kills, finally rebuild."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent.parent
PY = Path(sys.executable).resolve()
ENV = dict(os.environ, PATH=str(Path.home() / ".cargo" / "bin") + os.pathsep + os.environ["PATH"],
           PYTHONPATH=str(ROOT / "src"), PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8",
           PYO3_PYTHON=str(PY), CARGO_TARGET_DIR=str(ROOT / "crates" / "target"),
           TMPDIR=str(ROOT / ".ci-local" / "temp"), TEMP=str(ROOT / ".ci-local" / "temp"),
           TMP=str(ROOT / ".ci-local" / "temp"))
ENV.pop("PYTEST_ADDOPTS", None)
ASSERTION_RUNNER = """
import pytest, sys
class Reporter:
    def pytest_runtest_makereport(self, item, call):
        if call.when == 'call' and call.excinfo is not None:
            if issubclass(call.excinfo.type, AssertionError):
                print('P4C_ASSERTION_KILL=' + item.nodeid, flush=True)
raise SystemExit(pytest.main(sys.argv[1:], plugins=[Reporter()]))
"""
TEST = [str(PY), "-B", "-c", ASSERTION_RUNNER, str(ROOT / "tests" / "test_p4c_lock.py"),
        "-x", "-q", "--tb=short", "-p", "no:cacheprovider"]
HOST = "crates/te_host/src/lock.rs"
BINDING = "crates/te_py/src/lock.rs"
MUTANTS = (
    ("guard-dropped-before-return", HOST, "std::mem::forget(guard);", "drop(guard);"),
    ("pid-not-truncated", HOST, "guard.set_len(0)?;", "let _ = 0;"),
    ("pid-prefix-lost", HOST, 'format!("pid={pid}\\n")', 'format!("{pid}\\n")'),
    ("held-file-delete-sharing", HOST, "options.share_mode(0x1 | 0x2);",
     "options.share_mode(0x1 | 0x2 | 0x4);"),
    ("parent-creation-lost", HOST, "\n        mkdir(parent)?;\n", "\n        if false { mkdir(parent)?; }\n"),
    ("truncate-before-lock", HOST, ".write(true).create(true);",
     ".write(true).create(true).truncate(true);"),
    ("contention-accepted", BINDING, "Err(LockError::Contended) => Ok(false)",
     "Err(LockError::Contended) => Ok(true)"),
    ("held-always-false", BINDING, "fn held(&self) -> bool {\n        self.guard.is_some()\n    }",
     "fn held(&self) -> bool {\n        false\n    }"),
    ("release-leaks-descriptor", BINDING, "guard.release();", "std::mem::forget(guard);"),
    ("reacquire-not-idempotent", BINDING, "if self.guard.is_some() {", "if false {"),
    ("pid-capture-ignored", BINDING, "SingleInstanceGuard::acquire(&path, pid)",
     'SingleInstanceGuard::acquire(&path, "0")'),
    ("open-error-filename-lost", BINDING, "(errno, message, name.unbind())", "(errno, message)"),
)


def paths():
    assert PY == (ROOT / ".venv" / "Scripts" / "python.exe").resolve()
    assert ROOT.name == "p4c-t1"
    out = subprocess.check_output([str(PY), "-B", "-c",
        "import sys,trade_engine,trade_engine_rs;print(sys.executable);print(trade_engine.__file__);print(trade_engine_rs.__file__)"],
        cwd=ROOT, env=ENV, text=True).strip().splitlines()
    assert Path(out[0]).resolve() == PY
    assert Path(out[1]).resolve().is_relative_to(ROOT / "src")
    assert Path(out[2]).resolve().is_relative_to(ROOT / ".venv")


def build():
    start = time.perf_counter()
    subprocess.run([str(PY), "-m", "pip", "install", "--no-deps", "--force-reinstall", "-q",
                    str(ROOT / "crates" / "te_py")], cwd=ROOT, env=ENV, check=True, capture_output=True)
    paths()
    return time.perf_counter() - start


def tests():
    return subprocess.run(TEST, cwd=ROOT, env=ENV, capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=120)


def main():
    (ROOT / ".ci-local" / "temp").mkdir(parents=True, exist_ok=True)
    selected = set(sys.argv[1:])
    assert selected <= {m[0] for m in MUTANTS}, selected
    originals = {ROOT / file: (ROOT / file).read_bytes() for _, file, _, _ in MUTANTS}
    kills, failures = [], []
    baseline_green = restored_green = False
    try:
        seconds = build()
        baseline = tests()
        baseline_green = baseline.returncode == 0
        print(f"baseline exit={baseline.returncode} build={seconds:.3f}s\n{baseline.stdout[-2000:]}", flush=True)
        if baseline_green:
            for name, file, anchor, replacement in MUTANTS:
                if selected and name not in selected:
                    continue
                source = ROOT / file
                text = originals[source].decode("utf-8")
                if "\r\n" in text:
                    anchor = anchor.replace("\n", "\r\n")
                    replacement = replacement.replace("\n", "\r\n")
                assert text.count(anchor) == 1, (name, text.count(anchor))
                try:
                    source.write_bytes(text.replace(anchor, replacement).encode("utf-8"))
                    try:
                        seconds = build()
                    except subprocess.CalledProcessError as exc:
                        failures.append((name, "compile failed"))
                        print(f"INVALID {name}: {exc.stderr[-2000:]!r}", flush=True)
                        continue
                    proc = tests()
                    failed = [line for line in proc.stdout.splitlines() if line.startswith("FAILED")]
                    assertion = (proc.returncode == 1 and failed and "P4C_ASSERTION_KILL=" in proc.stdout
                                 and "ERROR collecting" not in proc.stdout and "ImportError" not in proc.stdout)
                    if assertion:
                        kills.append((name, failed[0]))
                        print(f"KILLED {name} build={seconds:.3f}s {failed[0]}", flush=True)
                    else:
                        failures.append((name, "survived" if proc.returncode == 0 else "not assertion"))
                        print(f"INVALID {name} exit={proc.returncode}\n{proc.stdout[-5000:]}", flush=True)
                finally:
                    source.write_bytes(originals[source])
    finally:
        for source, original in originals.items():
            source.write_bytes(original)
        seconds = build()
        restored = tests()
        restored_green = restored.returncode == 0
        print(f"restored exit={restored.returncode} build={seconds:.3f}s\n{restored.stdout[-2000:]}", flush=True)
        for source, original in originals.items():
            assert source.read_bytes() == original
            print(f"restored SHA256 {source.relative_to(ROOT)} {hashlib.sha256(original).hexdigest()}", flush=True)
        paths()
    print(f"killed={len(kills)} failures={failures}", flush=True)
    return 0 if baseline_green and restored_green and not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())

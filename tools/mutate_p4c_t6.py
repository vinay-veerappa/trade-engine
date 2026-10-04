"""T6 compiling hand mutants; restore bytes and rebuild even on failure."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent
PY = Path(sys.executable).resolve()
ENV = dict(os.environ, PATH=str(Path.home() / ".cargo" / "bin") + os.pathsep + os.environ["PATH"],
           PYO3_PYTHON=str(PY), CARGO_TARGET_DIR=str(ROOT / "crates" / "target"),
           PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8")
ENV.pop("PYTEST_ADDOPTS", None)
HOST = r"crates\te_host\src\clock.rs"
CLOCK = r"crates\te_py\src\clock.rs"
PLUGIN = r"crates\te_py\src\plugins.rs"
CONFIG = r"crates\te_runtime\src\config.rs"
MUTANTS = (
    ("backwards-allowed", HOST, "if compare(&target, &self.current)? {", "if false {"),
    ("advance-lost", HOST, "let target = add(&self.current)?;\n        self.current = target;",
     "let _target = add(&self.current)?;"),
    ("utc-normalization-lost", CLOCK, 'value.call_method1("astimezone", (zone,))', "Ok(value.clone())"),
    ("finite-check-lost", CLOCK, "if !finite_nonnegative(duration)? {", "if false {"),
    ("bool-duration-accepted", CLOCK, "if numeric(duration) && !duration.is_instance_of::<PyBool>() {",
     "if numeric(duration) {"),
    ("negative-timedelta-allowed", CLOCK, '.call_method0("total_seconds")?', '.call_method0("__abs__")?.call_method0("total_seconds")?'),
    ("sleep-override-bypassed", CLOCK, 'owner.call_method1("advance_by", (seconds,))?;',
     'owner.getattr("_native")?.call_method1("advance_by", (seconds,))?;'),
    ("zero-wall-sleep-called", CLOCK, "seconds.rich_compare(0, CompareOp::Gt)?",
     "seconds.rich_compare(0, CompareOp::Ge)?"),
    ("factory-writer-allowed", PLUGIN, "FACTORIES.load(Ordering::SeqCst) != 0",
     "FACTORIES.load(Ordering::SeqCst) == usize::MAX"),
    ("source-check-after-import", PLUGIN, "if !allowed.contains(&resolved(&origin)?) {", "if false {"),
    ("factory-carrier-not-checked", PLUGIN, "if !result.is_instance(&carrier)? {", "if false {"),
    ("offline-confinement-lost", CONFIG, "if !actual.starts_with(&root) {", "if false {"),
)
RUNNER = """
import pytest,sys
class Reporter:
    def pytest_runtest_makereport(self,item,call):
        if call.when=='call' and call.excinfo is not None:
            kind='ASSERTION' if issubclass(call.excinfo.type,AssertionError) else 'NON_ASSERTION'
            print('T6_'+kind+'_FAILURE='+item.nodeid,flush=True)
raise SystemExit(pytest.main(sys.argv[1:],plugins=[Reporter()]))
"""


def build():
    proc = subprocess.run([str(PY), "-B", str(ROOT / "tools" / "build_p4c_t3.py")],
                          cwd=ROOT, env=ENV, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=600)
    if proc.returncode:
        raise RuntimeError("rebuild failed\n" + proc.stdout[-1000:] + proc.stderr[-5000:])


def tests():
    return subprocess.run([str(PY), "-B", "-c", RUNNER, "tests/test_p4c_clock.py",
                           "-k", "not seeded or prefixes[0]", "-x", "-q", "--tb=short",
                           "-p", "no:cacheprovider"], cwd=ROOT, env=ENV, capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=180)


def main():
    assert PY == (ROOT / ".venv" / "Scripts" / "python.exe").resolve()
    selected = set(sys.argv[1:])
    assert selected <= {name for name, *_ in MUTANTS}
    originals = {ROOT / file: (ROOT / file).read_bytes() for _, file, *_ in MUTANTS}
    killed, invalid = [], []
    baseline_green = restored_green = False
    try:
        build()
        proc = tests()
        baseline_green = proc.returncode == 0
        print(f"BASELINE exit={proc.returncode}\n{proc.stdout[-2000:]}", flush=True)
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
                    build()
                    proc = tests()
                    failures = [line for line in proc.stdout.splitlines() if line.startswith("FAILED")]
                    if (proc.returncode == 1 and failures and "T6_ASSERTION_FAILURE=" in proc.stdout
                            and "T6_NON_ASSERTION_FAILURE=" not in proc.stdout
                            and "ERROR collecting" not in proc.stdout):
                        killed.append(name)
                        print(f"KILLED {name} {failures[0]}", flush=True)
                    else:
                        invalid.append(name)
                        print(f"INVALID {name} exit={proc.returncode}\n{proc.stdout[-5000:]}", flush=True)
                except (RuntimeError, subprocess.TimeoutExpired) as error:
                    invalid.append(name)
                    print(f"INVALID {name}: {error}", flush=True)
                finally:
                    source.write_bytes(originals[source])
    finally:
        for source, content in originals.items():
            source.write_bytes(content)
        build()
        proc = tests()
        restored_green = proc.returncode == 0
        print(f"RESTORED exit={proc.returncode}\n{proc.stdout[-2000:]}", flush=True)
        for source, content in originals.items():
            assert source.read_bytes() == content
            print("RESTORED_SHA256", source.relative_to(ROOT), hashlib.sha256(content).hexdigest(), flush=True)
    expected = len(selected) if selected else len(MUTANTS)
    print(f"SUMMARY killed={len(killed)} expected={expected} invalid={invalid}", flush=True)
    return 0 if baseline_green and restored_green and len(killed) == expected and not invalid else 1


if __name__ == "__main__":
    raise SystemExit(main())

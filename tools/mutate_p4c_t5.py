"""T5 single-bug compiling mutants, assertion-only kills, unconditional restoration."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent
PY = Path(sys.executable).resolve()
ENV = dict(os.environ, PATH=str(Path.home() / ".cargo" / "bin") + os.pathsep + os.environ["PATH"],
           PYTHONDONTWRITEBYTECODE="1", PYO3_PYTHON=str(PY), PYTHONIOENCODING="utf-8",
           CARGO_TARGET_DIR=str(ROOT / "crates" / "target"))
ENV.pop("PYTEST_ADDOPTS", None)
HOST = r"crates\te_host\src\outbox.rs"
BINDING = r"crates\te_py\src\outbox.rs"
MUTANTS = (
    ("fifo-reversed", HOST, 'query.push_str(" ORDER BY id ASC");', 'query.push_str(" ORDER BY id DESC");'),
    ("failed-filter-lost", HOST, 'query.push_str("status != \'DELIVERED\'");', 'query.push_str("status = \'PENDING\'");'),
    ("attempt-not-incremented", HOST, "attempts = attempts + 1", "attempts = attempts"),
    ("failure-error-lost", HOST, "last_error = ? WHERE", "last_error = CASE WHEN ? IS NULL THEN NULL ELSE NULL END WHERE"),
    ("delivery-stamp-lost", HOST, "delivered_at = ? WHERE", "delivered_at = CASE WHEN ? IS NULL THEN NULL ELSE NULL END WHERE"),
    ("duplicate-ignored", HOST, "VALUES (?, ?, ?, 'PENDING', 0, ?)", "VALUES (?, ?, ?, 'PENDING', 0, ?) ON CONFLICT DO NOTHING"),
    ("meta-update-lost", HOST, "ON CONFLICT(key) DO UPDATE SET value = excluded.value", "ON CONFLICT(key) DO NOTHING"),
    ("rollback-lost", HOST, "if begun && host.is_exception(&error)", "if false"),
    ("baseexception-rolled-back", HOST, "if begun && host.is_exception(&error)", "if begun"),
    ("fifo-failure-skipped", HOST, "            break;", "            continue;"),
    ("strip-lost", BINDING, '    value.call_method0("strip")', "    Ok(value.clone())"),
    ("json-sort-lost", BINDING, 'kwargs.set_item("sort_keys", true)?;', 'kwargs.set_item("sort_keys", false)?;'),
    ("timestamp-precision-lost", BINDING, 'created_at.call_method0("isoformat")?', 'created_at.call_method1("isoformat", ("T", "seconds"))?'),
    ("constraint-wrapper-lost", BINDING, "wrapped.set_cause(py, Some(error));\n                return Err(wrapped);", "return Err(error);"),
    ("baseexception-swallowed", BINDING, "if error.is_instance_of::<PyException>(self.owner.py())", "if true"),
)
RUNNER = """
import pytest,sys
class Reporter:
    def pytest_runtest_makereport(self,item,call):
        if call.when=='call' and call.excinfo is not None:
            kind='ASSERTION' if issubclass(call.excinfo.type,AssertionError) else 'NON_ASSERTION'
            print('T5_'+kind+'_FAILURE='+item.nodeid,flush=True)
raise SystemExit(pytest.main(sys.argv[1:],plugins=[Reporter()]))
"""


def build():
    proc = subprocess.run([str(PY), "-B", str(ROOT / "tools" / "build_p4c_t3.py")],
                          cwd=ROOT, env=ENV, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=600)
    if proc.returncode:
        raise RuntimeError("rebuild failed\n" + proc.stdout[-1000:] + proc.stderr[-5000:])


def tests():
    return subprocess.run([str(PY), "-B", "-c", RUNNER, "tests/test_p4c_outbox.py",
                           "-k", "not seeded or lockstep[0]", "-x", "-q", "--tb=short",
                           "-p", "no:cacheprovider"],
                          cwd=ROOT, env=ENV, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=180)


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
        print(f"BASELINE exit={proc.returncode}\n{proc.stdout[-1600:]}", flush=True)
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
                    if (proc.returncode == 1 and failures and "T5_ASSERTION_FAILURE=" in proc.stdout
                            and "T5_NON_ASSERTION_FAILURE=" not in proc.stdout
                            and "ERROR collecting" not in proc.stdout):
                        killed.append(name)
                        print(f"KILLED {name} {failures[0]}", flush=True)
                    else:
                        invalid.append(name)
                        print(f"INVALID {name} exit={proc.returncode}\n{proc.stdout[-4000:]}", flush=True)
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
        print(f"RESTORED exit={proc.returncode}\n{proc.stdout[-1600:]}", flush=True)
        for source, content in originals.items():
            assert source.read_bytes() == content
            print("RESTORED_SHA256", source.relative_to(ROOT), hashlib.sha256(content).hexdigest(), flush=True)
    print(f"SUMMARY killed={len(killed)} invalid={invalid}", flush=True)
    return 0 if baseline_green and restored_green and not invalid else 1


if __name__ == "__main__":
    raise SystemExit(main())

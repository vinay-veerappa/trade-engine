"""T9 compiling hand mutants using the established byte-restoring campaign."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = Path(sys.executable).resolve()
ENV = dict(
    os.environ,
    PATH=str(Path.home() / ".cargo" / "bin") + os.pathsep + os.environ["PATH"],
    PYO3_PYTHON=str(PY),
    CARGO_TARGET_DIR=str(ROOT / "crates" / "target"),
    PYTHONDONTWRITEBYTECODE="1",
    PYTHONIOENCODING="utf-8",
)
ENV.pop("PYTEST_ADDOPTS", None)

ACTOR = r"crates\te_host\src\actor.rs"
HTTP = r"crates\te_host\src\http.rs"
JOBS = r"crates\te_runtime\src\jobs.rs"
ROUTES = r"crates\te_runtime\src\routes.rs"
CONTROL = r"crates\te_runtime\src\control.rs"
ONCE = r"crates\te_py\src\eod_once.rs"

MUTANTS = (
    # actor.rs lifecycle invariants
    ("stopping-not-checked-before-dequeue", ACTOR,
     "if owner.stopping.load(Ordering::SeqCst) {\n                                done_sender.send(true).ok();\n                                return;\n                            }",
     "if false {\n                                done_sender.send(true).ok();\n                                return;\n                            }"),
    ("stop-timeout-fabricates-success", ACTOR,
     "return Err(\"owner stop blocked: job still running after timeout\".into());",
     "return Ok(());"),
    ("store-not-closed-on-stop", ACTOR,
     "// The connection closes before the guard; Store::close does both in order.\n        self.store.close();\n        Ok(())",
     "let _ = &self.store;\n        Ok(())"),
    ("panic-type-fabricated", ACTOR,
     "r#type: \"RuntimeHostPanic\".into(),",
     "r#type: \"RuntimeError\".into(),"),
    ("record-read-fabricated-empty", ACTOR,
     "pub fn get(&self, id: &str) -> Result<Option<Record>, Error> {\n        self.journal.get(id)\n    }",
     "pub fn get(&self, id: &str) -> Result<Option<Record>, Error> {\n        Ok(None)\n    }"),
    # http.rs control authorization
    ("control-host-check-skipped", HTTP,
     "if !matches!(host.as_str(), \"127.0.0.1\" | \"localhost\") {\n                    return Ok(error(\n                        403,\n                        \"Forbidden: Invalid Host header\".into(),\n                        \"Request forbidden -- authorization will not help\",\n                        false,\n                    ));\n                }\n                if let Some(origin) = raw.header(\"Origin\") {",
     "if let Some(origin) = raw.header(\"Origin\") {"),
    ("control-cross-origin-allowed", HTTP,
     "if !api.origin.is_match(origin.trim_matches(whitespace)) {",
     "if false {"),
    ("control-status-route-post-allowed", HTTP,
     "(\"GET\", path) if path == \"/status\" => {",
     "(_, path) if path == \"/status\" => {"),
    ("control-refusal-kind-dropped", HTTP,
     "(\"type\", string(failure.kind)),",
     "(\"type\", string(\"RuntimeError\")),"),
    # runtime owner capability authorization
    ("capability-compared-leniently", JOBS,
     "if capability != Some(self.capability.as_str()) {",
     "if false {"),
    # routes.rs terminal reads
    ("wait-terminal-state-ignored", ROUTES,
     "if matches!(\n                value[\"state\"].as_str(),\n                Some(\"completed\" | \"refused\" | \"failed\" | \"uncertain\")\n            ) {",
     "if true {"),
    # control.rs trusted dispatch
    ("synthetic-result-job-fabricated", CONTROL,
     "\"job\": job.job,",
     "\"job\": \"fabricated\","),
    # eod_once.rs CLI one-shot client
    ("completed-result-accounts-dropped", ONCE,
     "\"accounts\": accounts,",
     "\"accounts\": Vec::<serde_json::Value>::new(),"),
    ("refusal-not-classified", ONCE,
     "let is_refusal = match &refusal {\n        Some(base) => error.is_instance(py, base),\n        None => false,\n    };",
     "let is_refusal = false;"),
)

RUST_TEST = """
import subprocess, sys, os
env = dict(os.environ)
proc = subprocess.run(["cargo", "test", "--manifest-path", r"crates\\Cargo.toml",
                       "-p", "te_host", "--lib"],
                      capture_output=True, text=True, encoding="utf-8", errors="replace",
                      env=env, timeout=900)
print(proc.stdout[-4000:])
if proc.returncode != 0:
    print("T9_ASSERTION_FAILURE=rust-te-host-tests")
    sys.exit(1)
"""

PY_RUNNER = """
import pytest,sys
class Reporter:
    def pytest_runtest_makereport(self,item,call):
        if call.when=='call' and call.excinfo is not None:
            kind='ASSERTION' if issubclass(call.excinfo.type,AssertionError) else 'NON_ASSERTION'
            print('T9_'+kind+'_FAILURE='+item.nodeid,flush=True)
raise SystemExit(pytest.main(sys.argv[1:],plugins=[Reporter()]))
"""


def build():
    proc = subprocess.run(
        [str(PY), "-B", str(ROOT / "tools" / "build_p4c_t3.py")],
        cwd=ROOT, env=ENV, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=900,
    )
    if proc.returncode:
        raise RuntimeError(
            "rebuild failed\n" + proc.stdout[-1000:] + proc.stderr[-5000:]
        )


def tests():
    # The native te_host surface is a first-class T9 gate: a cargo test
    # failure means a native assertion killed the mutant.
    rust = subprocess.run(
        [str(PY), "-B", "-c", RUST_TEST],
        cwd=ROOT, env=ENV, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=900,
    )
    if rust.returncode != 0:
        class Result:
            returncode = 1
            stdout = rust.stdout + "\nFAILED rust-te-host-tests\nT9_ASSERTION_FAILURE=cargo\n"
        return Result()
    return subprocess.run(
        [str(PY), "-B", "-c", PY_RUNNER, "tests/test_p4c_runtime.py",
         "tests/test_p4c_t9_cli.py", "-x", "-q", "--tb=short",
         "-p", "no:cacheprovider"],
        cwd=ROOT, env=ENV, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=900,
    )


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
                if text.count(anchor) != 1:
                    invalid.append(name)
                    print(f"INVALID {name}: anchor count {text.count(anchor)}", flush=True)
                    continue
                try:
                    source.write_bytes(
                        text.replace(anchor, replacement).encode("utf-8")
                    )
                    build()
                    proc = tests()
                    failures = [
                        line
                        for line in proc.stdout.splitlines()
                        if line.startswith("FAILED")
                    ]
                    if (
                        proc.returncode == 1
                        and failures
                        and "T9_ASSERTION_FAILURE=" in proc.stdout
                        and "T9_NON_ASSERTION_FAILURE=" not in proc.stdout
                        and "ERROR collecting" not in proc.stdout
                    ):
                        killed.append(name)
                        print(f"KILLED {name} {failures[0]}", flush=True)
                    else:
                        invalid.append(name)
                        print(
                            f"INVALID {name} exit={proc.returncode}\n{proc.stdout[-5000:]}",
                            flush=True,
                        )
                except (RuntimeError, subprocess.TimeoutExpired) as error:
                    invalid.append(name)
                    print(f"INVALID {name}: {error}", flush=True)
                finally:
                    source.write_bytes(originals[source])
        build()
        proc = tests()
        restored_green = proc.returncode == 0
        print(f"RESTORED exit={proc.returncode}", flush=True)
    finally:
        for source, original in originals.items():
            source.write_bytes(original)
    print(f"\nkilled={len(killed)} invalid={len(invalid)} baseline={baseline_green} restored={restored_green}")
    print("KILLED: " + " ".join(killed))
    print("INVALID: " + " ".join(invalid))
    ok = baseline_green and restored_green and not invalid and len(killed) == len(MUTANTS)
    print("T9_MUTATION_CAMPAIGN=" + ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
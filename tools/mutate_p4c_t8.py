"""T8 compiling single-bug mutations, unconditional byte restoration and rebuild."""
from __future__ import annotations

import hashlib
import subprocess
import sys

from mutate_p4c_t6 import build, ENV, PY, ROOT, RUNNER as REPORTER

CORE = r"crates\te_core\src\runtime\intraday_flow.rs"
BINDING = r"crates\te_py\src\intraday_flow.rs"
HEARTBEAT = r"crates\te_host\src\heartbeat.rs"
MUTANTS = (
    ("admission-omitted", CORE, "host.admit()?;", ""),
    ("settled-session-redriven", CORE, "if host.settled()? {", "if false {"),
    ("starting-beat-exited-early", CORE, 'host.beat("starting", false, false)?;', 'host.beat("starting", false, true)?;'),
    ("preopen-wait-omitted", CORE, "host.wait()?;", ""),
    ("post-fetch-clock-not-reread", CORE, "let observed = host.now()?;", "let observed = requested;"),
    ("flatten-deadline-ignored", CORE, 'if flat_due { host.flatten("flat-sweep")?; }', ""),
    ("working-entry-cancel-omitted", CORE, 'host.cancel(if flat_due { "flat-sweep" } else { "entry-end" })?;', ""),
    ("entry-gate-reversed", CORE, "host.gate(entries_open)?;", "host.gate(!entries_open)?;"),
    ("tick-reconcile-omitted", CORE, "host.record(&view)?;", ""),
    ("stop-boundary-ignored", CORE, "if host.stopped()? {", "if false {"),
    ("stale-refusal-not-latched", BINDING, 'host.state.setattr("refusing", true)?;\n    host.cancel("stale-quote")?;',
     'host.state.setattr("refusing", false)?;\n    host.cancel("stale-quote")?;'),
    ("venue-book-not-restored", BINDING, 'broker.call_method1("restore", (orders, fills, positions))?;', ""),
    ("orphan-entry-not-cancelled", BINDING, '"cancel" => {', '"never-cancel" => {'),
    ("emergency-fresh-check-omitted", BINDING, "host.fresh(snapshot, &now)?;", "let _ = snapshot;"),
    ("heartbeat-not-atomically-replaced", HEARTBEAT, "fs::rename(temporary, path)", "fs::copy(temporary, path).map(|_| ())"),
)
RUNNER = REPORTER.replace("T6_", "T8_")


def tests():
    return subprocess.run([str(PY), "-B", "-c", RUNNER, "tests/test_p4c_intraday.py",
                           "-k", "not seeded and not stop_restore", "-x", "-q", "--tb=short",
                           "-p", "no:cacheprovider"], cwd=ROOT, env=ENV, capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=300)


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
                    if (proc.returncode == 1 and failures and "T8_ASSERTION_FAILURE=" in proc.stdout
                            and "T8_NON_ASSERTION_FAILURE=" not in proc.stdout and "ERROR collecting" not in proc.stdout):
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
        for source, original in originals.items():
            source.write_bytes(original)
        build()
        proc = tests()
        restored_green = proc.returncode == 0
        print(f"RESTORED exit={proc.returncode}\n{proc.stdout[-2000:]}", flush=True)
        for source, original in originals.items():
            assert source.read_bytes() == original
            print("RESTORED_SHA256", source.relative_to(ROOT), hashlib.sha256(original).hexdigest(), flush=True)
    expected = len(selected) if selected else len(MUTANTS)
    print(f"SUMMARY killed={len(killed)} expected={expected} invalid={invalid}", flush=True)
    return 0 if baseline_green and restored_green and len(killed) == expected and not invalid else 1


if __name__ == "__main__":
    raise SystemExit(main())

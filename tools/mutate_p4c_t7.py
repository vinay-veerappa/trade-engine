"""T7 compiling single-bug mutants; restore bytes and rebuild unconditionally."""
from __future__ import annotations

import hashlib
from pathlib import Path
import subprocess
import sys

from mutate_p4c_t6 import build, ENV, PY, ROOT, RUNNER as CLOCK_RUNNER

CORE = r"crates\te_core\src\runtime\eod_flow.rs"
MUTANTS = (
    ("validation-omitted", "host.validate(mode)?;", ""),
    ("completed-account-redriven", "if !host.completed(id, mode)? {", "if true {"),
    ("resume-guard-omitted", "host.validate_resume(id, end)?;", ""),
    ("settlement-wait-omitted", "host.boundary(Boundary::Settled)?;", ""),
    ("lifecycle-omitted", "host.settle(&options)?;", ""),
    ("timeline-clock-not-advanced", "host.advance(&row.at)?;", ""),
    ("covered-snapshot-rematched", "if host.covered(&row.item, end)? {", "if false {"),
    ("marks-omitted", "host.finish(account, Finish::Marks)?;", ""),
    ("marker-before-entries", "orders = host.finish(account, Finish::Entries)?;",
     "host.finish(account, Finish::Marker)?;\n        orders = host.finish(account, Finish::Entries)?;"),
    ("match-after-actions-omitted", "exits += closed;\n        host.match_snapshot()?;", "exits += closed;"),
    ("first-action-not-tested", "for action in &actions {", "for action in actions.iter().skip(1) {"),
    ("risk-refusal-ignored", "if !accepted { return Ok(0); }", "if false { return Ok(0); }"),
    ("resize-ignored", "let intent = if resize { host.resize(&intent, &verdict)? } else { intent };",
     "let intent = intent;"),
    ("signal-not-recorded", "host.record_signal(&signal)?;", ""),
    ("entry-ack-not-reconciled", "if !host.terminal(&submitted)? {", "if host.terminal(&submitted)? {"),
)
RUNNER = CLOCK_RUNNER.replace("T6_", "T7_")


def tests():
    return subprocess.run([str(PY), "-B", "-c", RUNNER, "tests/test_p4c_eod.py",
                           "-k", "not seeded or seeded_pass_resume_and_close[0]",
                           "-x", "-q", "--tb=short", "-p", "no:cacheprovider"],
                          cwd=ROOT, env=ENV, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=180)


def main():
    assert PY == (ROOT / ".venv" / "Scripts" / "python.exe").resolve()
    selected = set(sys.argv[1:])
    assert selected <= {name for name, *_ in MUTANTS}
    source = ROOT / CORE
    original = source.read_bytes()
    killed, invalid = [], []
    baseline_green = restored_green = False
    try:
        build()
        proc = tests()
        baseline_green = proc.returncode == 0
        print(f"BASELINE exit={proc.returncode}\n{proc.stdout[-2000:]}", flush=True)
        if baseline_green:
            for name, anchor, replacement in MUTANTS:
                if selected and name not in selected:
                    continue
                text = original.decode("utf-8")
                if "\r\n" in text:
                    anchor = anchor.replace("\n", "\r\n")
                    replacement = replacement.replace("\n", "\r\n")
                assert text.count(anchor) == 1, (name, text.count(anchor))
                try:
                    source.write_bytes(text.replace(anchor, replacement).encode("utf-8"))
                    build()
                    proc = tests()
                    failures = [line for line in proc.stdout.splitlines() if line.startswith("FAILED")]
                    if (proc.returncode == 1 and failures and "T7_ASSERTION_FAILURE=" in proc.stdout
                            and "T7_NON_ASSERTION_FAILURE=" not in proc.stdout
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
                    source.write_bytes(original)
    finally:
        source.write_bytes(original)
        build()
        proc = tests()
        restored_green = proc.returncode == 0
        print(f"RESTORED exit={proc.returncode}\n{proc.stdout[-2000:]}", flush=True)
        assert source.read_bytes() == original
        print("RESTORED_SHA256", source.relative_to(ROOT), hashlib.sha256(original).hexdigest(), flush=True)
    expected = len(selected) if selected else len(MUTANTS)
    print(f"SUMMARY killed={len(killed)} expected={expected} invalid={invalid}", flush=True)
    return 0 if baseline_green and restored_green and len(killed) == expected and not invalid else 1


if __name__ == "__main__":
    raise SystemExit(main())

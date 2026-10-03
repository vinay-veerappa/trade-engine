"""Compiling runtime hand mutants; isolated build, assertion kill, byte restoration."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent.parent
PY = Path(sys.executable).resolve()
EXPECTED = ROOT / ".venv-p4a" / "Scripts" / "python.exe"
SOURCES = ROOT / "crates" / "te_core" / "src" / "runtime"
ENV = dict(os.environ, PATH=str(Path.home() / ".cargo" / "bin") + os.pathsep + os.environ["PATH"],
           PYTHONPATH=str(ROOT / "src"), PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8")
TEST = [str(PY), "-B", "-m", "pytest", str(ROOT / "tests" / "test_p4a_parity.py"),
        "-x", "-q", "-s", "--tb=short", "-p", "no:cacheprovider", "--basetemp", str(ROOT / ".p4a-mutant-tests")]
MUTANTS = (
    ("pass-cutoff-exclusive", "eod.rs", '"cutoff" => p.flags.push(n[0] <= n[1])',
     '"cutoff" => p.flags.push(n[0] < n[1])'),
    ("stale-boundary-exclusive", "intraday.rs", '"underlying_age" => if f[0] > f[1]',
     '"underlying_age" => if f[0] >= f[1]'),
    ("preopen-inclusive", "eod.rs", '"preopen" => p.flags.push(n[0] < n[1])',
     '"preopen" => p.flags.push(n[0] <= n[1])'),
    ("dividend-double-amount", "eod.rs", 'crate::ledger::ops::s(&mul(&dec(&s[0])?,&per_share)?)',
     'crate::ledger::ops::s(&mul(&dec(&s[0])?,&per_share)?.mul_i128(2).map_err(derr)?)'),
    ("early-close-ignored", "intraday.rs", "let flat = n[0].min(n[2] - n[3]);",
     "let flat = n[0];"),
    ("heartbeat-exclusive", "intraday.rs", '"heartbeat" => if b[0] && f[0] <= f[1]',
     '"heartbeat" => if b[0] && f[0] < f[1]'),
    ("tally-subtraction", "routing.rs", "(&values[i] + &values[i + 3])",
     "(&values[i] - &values[i + 3])"),
    ("taken-forgets-entry-risk", "routing.rs", 'b[0] && (b[1] || (b[2] && b[3]))',
     'b[0] && b[1]'),
    ("previous-session-disabled", "eod.rs", '"previous" => if b[0] && !b[1]',
     '"previous" => if false'),
)


def paths():
    assert PY == EXPECTED.resolve(), (PY, EXPECTED)
    assert ROOT.name == "rust-p4a"
    output = subprocess.check_output([str(PY), "-B", "-c",
        "import sys,trade_engine,trade_engine_rs;print(sys.executable);print(trade_engine.__file__);print(trade_engine_rs.__file__)"],
        cwd=ROOT, env=ENV, text=True)
    lines = output.strip().splitlines()
    assert Path(lines[0]).resolve() == PY
    assert Path(lines[1]).resolve().is_relative_to(ROOT / "src")
    assert Path(lines[2]).resolve().is_relative_to(ROOT / ".venv-p4a")


def build():
    start = time.perf_counter()
    subprocess.run([str(PY), "-m", "pip", "install", "--no-deps", "--force-reinstall", "-q",
        str(ROOT / "crates" / "te_py")], cwd=ROOT, env=ENV, check=True, capture_output=True)
    paths()
    return time.perf_counter() - start


def tests(mutant=False):
    start = time.perf_counter()
    command=TEST + (["-k", "not full_session"] if mutant else [])
    proc = subprocess.run(command, cwd=ROOT, env=ENV, capture_output=True, text=True, encoding="utf-8",errors="replace")
    return proc, time.perf_counter() - start


def main():
    paths()
    selected = set(sys.argv[1:])
    assert selected <= {m[0] for m in MUTANTS}, selected
    originals = {SOURCES / filename: (SOURCES / filename).read_bytes() for _,filename,_,_ in MUTANTS}
    failures = []
    kills = []
    baseline_green = restored_green = False
    try:
        build_seconds = build()
        baseline, test_seconds = tests()
        baseline_green = baseline.returncode == 0
        print(f"baseline exit={baseline.returncode} build={build_seconds:.3f}s parity={test_seconds:.3f}s", flush=True)
        if not baseline_green:
            print(baseline.stdout[-5000:], flush=True)
        else:
            for name,filename,anchor,replacement in MUTANTS:
                if selected and name not in selected:
                    continue
                source=SOURCES / filename
                text=originals[source].decode("utf-8")
                assert text.count(anchor) == 1, (name,text.count(anchor))
                try:
                    source.write_bytes(text.replace(anchor,replacement).encode("utf-8"))
                    try:
                        build_seconds=build()
                    except subprocess.CalledProcessError as err:
                        failures.append((name,"compile failed"))
                        print(f"INVALID {name}: compile failed {err.stderr[-1500:]!r}",flush=True)
                        continue
                    proc,test_seconds=tests(mutant=True)
                    lines=[line for line in proc.stdout.splitlines() if line.startswith("FAILED")]
                    assertion=proc.returncode == 1 and "AssertionError" in proc.stdout and lines and "ERROR collecting" not in proc.stdout
                    if assertion:
                        kills.append(name)
                        print(f"KILLED {name} build={build_seconds:.3f}s parity={test_seconds:.3f}s {lines[0]}",flush=True)
                    else:
                        failures.append((name,"survived" if proc.returncode == 0 else "not an assertion kill"))
                        print(f"INVALID {name} exit={proc.returncode}\n{proc.stdout[-4000:]}",flush=True)
                finally:
                    source.write_bytes(originals[source])
    finally:
        for source,original in originals.items():
            source.write_bytes(original)
        build_seconds=build()
        restored,test_seconds=tests()
        restored_green=restored.returncode == 0
        print(f"restored exit={restored.returncode} build={build_seconds:.3f}s parity={test_seconds:.3f}s",flush=True)
        if not restored_green:
            print(restored.stdout[-5000:],flush=True)
        for source,original in originals.items():
            assert source.read_bytes() == original, source
            print(f"restored SHA256 {source.name} {hashlib.sha256(original).hexdigest()}",flush=True)
        paths()
    print(f"killed={len(kills)} failures={failures}",flush=True)
    return 0 if baseline_green and restored_green and not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())

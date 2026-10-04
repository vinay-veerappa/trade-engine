"""Compiling P4b hand mutants: assertion kills only, finally restore, final green build."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent.parent
PY = Path(sys.executable).resolve()
SOURCES = ROOT / "crates" / "te_core" / "src" / "runtime"
ENV = dict(os.environ, PATH=str(Path.home() / ".cargo" / "bin") + os.pathsep + os.environ["PATH"],
           PYTHONPATH=str(ROOT / "src"), PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8",
           CARGO_TARGET_DIR=str(ROOT / "crates" / "target"))
TEST = [str(PY), "-B", "-m", "pytest", str(ROOT / "tests" / "test_p4b_parity.py"),
        "-x", "-q", "--tb=short", "-p", "no:cacheprovider",
        "--basetemp", str(ROOT / ".ci-local" / "p4b-mutant-tests")]
MUTANTS = (
    ("before-close-allowed", "lifecycle.rs", '"close" => if n[0] < 0', '"close" => if false'),
    ("source-identity-ignored", "lifecycle.rs",
     'op == "price_identity" && !(t[0] == t[3] && t[1] == t[4] && t[2] == t[5])',
     'op == "price_identity" && false'),
    ("price-known-too-early-allowed", "lifecycle.rs",
     'op == "price_known" && n[0] < 0', 'op == "price_known" && false'),
    ("threshold-exclusive", "lifecycle.rs", 'let expired = lt(&v,&dec("0.01")?)?',
     'let expired = le(&v,&dec("0.01")?)?'),
    ("early-dividend-tie-assigns", "lifecycle.rs", 'p.flags.push(exercise)',
     'p.flags.push(exercise || eq(&extrinsic,&dec(&t[3])?)?)'),
    ("dividend-sum-double", "lifecycle.rs", 'amount = add(&amount,&dec(v)?)?',
     'amount = add(&amount,&mul_i(&dec(v)?,2)?)?'),
    ("long-call-early-assigns", "lifecycle.rs", 'b[0] && b[1] && b[2]', 'b[1] && b[2]'),
    ("early-command-suffix-dropped", "lifecycle.rs", 'if b[0] {":early"} else {""}',
     'if b[0] {""} else {""}'),
    ("source-zero-price-accepted", "lifecycle.rs",
     'let valid = b[1] && dec(&t[1]).map(|v|v.is_finite() && gt(&v,&zero()).unwrap_or(false)).unwrap_or(false)',
     'let valid = b[1] && dec(&t[1]).map(|v|v.is_finite() && ge(&v,&zero()).unwrap_or(false)).unwrap_or(false)'),
    ("journal-seq-residue-dropped", "journal.rs", '(micro / 1000) * 1000 + residue',
     '(micro / 1000) * 1000'),
    ("journal-skipped-accepted", "journal.rs", 'any_stored && !skipped', 'any_stored'),
    ("journal-cross-account-accepted", "journal.rs", 'pub fn account_matches(equal: bool) -> bool { equal }',
     'pub fn account_matches(equal: bool) -> bool { true }'),
    ("journal-list-tags-ignored", "journal.rs", 'if is_list {0} else if is_json_string {1} else {2}',
     'if false {0} else if is_json_string {1} else {2}'),
    ("journal-quantity-tolerance-inclusive", "journal.rs", 'delta.abs() < 1e-6',
     'delta.abs() <= 1e-6'),
)


def paths():
    assert PY == (ROOT / ".venv" / "Scripts" / "python.exe").resolve()
    assert ROOT.name == "rust-p4b"
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
                          text=True, encoding="utf-8", errors="replace")


def main():
    (ROOT / ".ci-local").mkdir(exist_ok=True)
    selected = set(sys.argv[1:])
    assert selected <= {m[0] for m in MUTANTS}, selected
    originals = {SOURCES / filename: (SOURCES / filename).read_bytes() for _, filename, _, _ in MUTANTS}
    kills, failures = [], []
    baseline_green = restored_green = False
    try:
        seconds = build()
        baseline = tests()
        baseline_green = baseline.returncode == 0
        print(f"baseline exit={baseline.returncode} build={seconds:.3f}s\n{baseline.stdout[-2000:]}", flush=True)
        if baseline_green:
            for name, filename, anchor, replacement in MUTANTS:
                if selected and name not in selected:
                    continue
                source = SOURCES / filename
                text = originals[source].decode("utf-8")
                assert text.count(anchor) == 1, (name, text.count(anchor))
                try:
                    source.write_bytes(text.replace(anchor, replacement).encode("utf-8"))
                    try:
                        seconds = build()
                    except subprocess.CalledProcessError as exc:
                        failures.append((name, "compile failed"))
                        print(f"INVALID {name}: compile failed {exc.stderr[-2000:]!r}", flush=True)
                        continue
                    proc = tests()
                    failed = [line for line in proc.stdout.splitlines() if line.startswith("FAILED")]
                    assertion = (proc.returncode == 1 and failed and "AssertionError" in proc.stdout
                                 and "ERROR collecting" not in proc.stdout and "ImportError" not in proc.stdout)
                    if assertion:
                        kills.append((name, failed[0]))
                        print(f"KILLED {name} build={seconds:.3f}s {failed[0]}", flush=True)
                    else:
                        failures.append((name, "survived" if proc.returncode == 0 else "not an assertion kill"))
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
            print(f"restored SHA256 {source.name} {hashlib.sha256(original).hexdigest()}", flush=True)
        paths()
    print(f"killed={len(kills)} failures={failures}", flush=True)
    return 0 if baseline_green and restored_green and not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())

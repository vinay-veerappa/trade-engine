"""Compiling P5 (T1 transport, T2 normalize, T3 slippage) hand mutants: assertion kills only, finally restore, final green build."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent.parent
PY = Path(sys.executable).resolve()
SOURCES = ROOT / "crates" / "te_core" / "src" / "tos_paper"
ENV = dict(os.environ, PATH=str(Path.home() / ".cargo" / "bin") + os.pathsep + os.environ["PATH"],
           PYTHONPATH=str(ROOT / "src"), PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8",
           CARGO_TARGET_DIR=str(ROOT / "crates" / "target"))
TEST = [str(PY), "-B", "-m", "pytest", str(ROOT / "tests" / "test_p5_parity.py"),
        "-x", "-q", "--tb=short", "-p", "no:cacheprovider",
        "--basetemp", str(ROOT / ".ci-local" / "p5-mutant-tests")]
MUTANTS = (
    ("fee-rounds-nearest-not-floor", "slippage.rs", "d.quantize_round(-2, Round::Floor)",
     "d.quantize_round(-2, Round::HalfEven)"),
    ("fee-remainder-dropped", "slippage.rs", "fees[0] = add(&fees[0], &remainder)?;",
     "fees[0] = fees[0].clone();"),
    ("zero-share-gets-a-fill", "slippage.rs", "if gt(&piece, &zero())? {", "if !lt(&piece, &zero())? {"),
    ("adverse-sign-flipped", "slippage.rs",
     'if side == "BUY" { sub(&venue_price, &sim_price)? } else { sub(&sim_price, &venue_price)? }',
     'if side == "BUY" { sub(&sim_price, &venue_price)? } else { sub(&venue_price, &sim_price)? }'),
    ("zero-sim-price-gets-bps", "slippage.rs", "if gt(&sim_price, &zero())? {", "if !lt(&sim_price, &zero())? {"),
    ("sent-marked-accepted", "normalize.rs", '"SENT" => ack("PENDING", "sent; awaiting read-back".into()),',
     '"SENT" => ack("ACCEPTED", "sent; awaiting read-back".into()),'),
    ("dry-run-marked-accepted", "normalize.rs", '"DRY_RUN" => ack("PENDING"', '"DRY_RUN" => ack("ACCEPTED"'),
    ("unknown-status-marked-accepted", "normalize.rs", '_ => ack("PENDING", format!("unknown transport status',
     '_ => ack("ACCEPTED", format!("unknown transport status'),
    ("order-id-read-from-any-status", "normalize.rs", 'status_of(raw, "status") != "SENT"',
     'status_of(raw, "status") == "SENT_"'),
    ("fractional-shares-accepted", "normalize.rs", "d.is_integral().map_err(derr)", "Ok(true)"),
    ("status-not-stripped", "pytext.rs", "strip(s).to_uppercase()", "s.to_uppercase()"),
    ("whole-contracts-check-off", "transport.rs", "q.is_integral().map_err(derr)", "Ok(true)"),
    ("zero-limit-accepted", "transport.rs", "Some(d) => d.le(&PyDec::zero()).map_err(derr),",
     "Some(d) => d.lt(&PyDec::zero()).map_err(derr),"),
    ("one-strike-vertical", "netting.rs", "if first.strike.eq_num(&second.strike).map_err(derr)? {", "if false {"),
    ("ratio-spread-vertical", "netting.rs", "if legs[0].ratio != legs[1].ratio {", "if false {"),
    ("same-side-vertical", "netting.rs", "if legs[0].side == legs[1].side {", "if false {"),
)


def paths():
    assert PY == (ROOT / ".venv" / "Scripts" / "python.exe").resolve()
    assert ROOT.name == "rust-p5"
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

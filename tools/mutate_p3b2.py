"""Hand mutants of P3b-2a. Build and green baseline FIRST, rebuild EACH mutant.

Run with this worktree's interpreter: python -B tools\\mutate_p3b2.py [names ...].
Every mutation must compile and fail the frozen parity tests. Original bytes are
restored in finally, followed unconditionally by a rebuild and green parity run.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "crates" / "te_core" / "src" / "oms" / "manager.rs"
PY = sys.executable
ENV = dict(os.environ, PYTHONPATH=str(ROOT / "src"))
TEST = [PY, "-B", "-m", "pytest", str(ROOT / "tests" / "test_p3b2_parity.py"),
        "-x", "-q", "-p", "no:cacheprovider"]
MUTANTS = [
    ("reduce-rounds-nearest", "a.to_integral(Round::Floor)", "a.to_integral(Round::HalfEven)"),
    ("allocation-smallest-remainder-first", "indices.sort_by_key(|&i| (-r[i].clone(),i));",
     "indices.sort_by_key(|&i| (r[i].clone(),i));"),
    ("allocation-tie-last-first", "indices.sort_by_key(|&i| (-r[i].clone(),i));",
     "indices.sort_by_key(|&i| (-r[i].clone(),usize::MAX-i));"),
    ("option-allocation-loses-residue", "let last = sub(q,&sum(&portions[..portions.len()-1])?)?;",
     'let last = portions.last().expect("nonempty").clone();'),
    ("fractions-forget-runner", 'let runner=sub(&PyDec::from_i128(1),&sum(&w)?)?;',
     "let runner=zero();"),
    ("fingerprint-forgets-quantity", 'let t=st(f,"entry_type")?;',
     'fields.retain(|(k,_)|k!="quantity"); let t=st(f,"entry_type")?;'),
    ("fingerprint-forgets-entry-type", 'if t != "LIMIT" { fields.push(("entry_type".into(),js(t))); }',
     'if false { fields.push(("entry_type".into(),js(t))); }'),
    ("fingerprint-forgets-stoplimit-limit", 'if t == "STOP_LIMIT" { fields.push(("entry_limit_price".into(),',
     'if false { fields.push(("entry_limit_price".into(),'),
    ("native-stop-ignores-capability", '(!(t == "STOP" || t == "STOP_LIMIT") || native_stops)',
     "(true || native_stops)"),
    ("trigger-prefers-limit", 'if types.contains(&js("MARKET")) { Ok(js("MARKET")) }',
     'if types.contains(&js("MARKET")) { Ok(js("LIMIT")) }'),
    ("tif-check-disabled", 'if !array(field(j,"tifs")?)?.contains(&js(st(j,"tif")?)) { return Ok(js("refuse")); }',
     'if false { return Ok(js("refuse")); }'),
    ("stop-touch-does-not-trigger", 'let triggered=if o.side==Side::Buy { ge(&p,&stop)? } else { le(&p,&stop)? };',
     'let triggered=if o.side==Side::Buy { gt(&p,&stop)? } else { lt(&p,&stop)? };'),
    ("sync-activates-targets-before-entry-terminal",
     'eq(&d(j,"stop_filled")?,&zero())? && flag(j,"terminal")?',
     'eq(&d(j,"stop_filled")?,&zero())?'),
    ("sync-forgets-runner-budget", 'let runner=sub(&d(j,"planned")?,&sum(&w)?)?;',
     'let runner=zero();'),
    ("reduce-ignores-working-close", 'o.order_id==format!("{id}:close") && !terminal(o.state)',
     'o.order_id==format!("{id}:close") && false'),
    ("replace-status-resolves-pending", 'if status=="PENDING" {',
     'if false {'),
]


def build():
    subprocess.run([PY, "-m", "pip", "install", "--no-deps", "--force-reinstall", "-q",
                    str(ROOT / "crates" / "te_py")],
                   cwd=ROOT, env=ENV, check=True, capture_output=True)


def tests():
    return subprocess.run(TEST, cwd=ROOT, env=ENV, capture_output=True, text=True)


def main():
    selected = set(sys.argv[1:])
    unknown = selected - {name for name, _, _ in MUTANTS}
    if unknown:
        raise ValueError(f"unknown mutants: {sorted(unknown)}")
    originals = SOURCE.read_bytes()
    failures = []
    killed = []
    baseline_green = False
    final_green = False
    try:
        build()
        baseline = tests()
        baseline_green = baseline.returncode == 0
        if not baseline_green:
            print("BASELINE RED\n" + baseline.stdout[-4000:], flush=True)
        else:
            print("baseline green", flush=True)
            for name, anchor, replacement in MUTANTS:
                if selected and name not in selected:
                    continue
                text = originals.decode("utf-8")
                if text.count(anchor) != 1:
                    failures.append((name, f"anchor {text.count(anchor)}x"))
                    continue
                try:
                    SOURCE.write_bytes(text.replace(anchor, replacement).encode("utf-8"))
                    try:
                        build()
                    except subprocess.CalledProcessError as error:
                        failures.append((name, "does not build"))
                        print(f"BUILD FAILURE: {name}\n{error.stderr[-1600:]!r}", flush=True)
                        continue
                    result = tests()
                    if result.returncode == 0:
                        failures.append((name, "survived"))
                        print("SURVIVED: " + name, flush=True)
                    else:
                        lines = [line for line in result.stdout.splitlines() if line.startswith("FAILED")]
                        if not lines:
                            failures.append((name, "no assertion failure evidence"))
                            print(f"INVALID KILL: {name}\n{result.stdout[-1600:]}", flush=True)
                        else:
                            killed.append(name)
                            print(f"killed: {name} <- {lines[0]}", flush=True)
                finally:
                    SOURCE.write_bytes(originals)
    finally:
        SOURCE.write_bytes(originals)
        build()
        result = tests()
        final_green = result.returncode == 0
        print("final restored parity: " + ("green" if final_green else "RED"), flush=True)
        if not final_green:
            print(result.stdout[-4000:], flush=True)
        assert SOURCE.read_bytes() == originals, "original bytes not restored"
    print(f"{len(killed)} killed; failures: {failures or 'none'}", flush=True)
    return 0 if baseline_green and final_green and not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Hand mutants of the P3b-1 Rust (docs/RUST_PORT.md): each must be KILLED by tests/test_p3b_parity.py.

Run:  python -B tools/mutate_p3b.py [name ...]

Each mutant is one exact-once anchor replaced in one file; the crate is rebuilt through pip
(the way the gate builds it), the parity test runs under ``python -B`` (a same-size mutant
must not reuse a stale .pyc), and the ORIGINAL BYTES are restored in a finally. A final
unconditional restore plus rebuild leaves the tree and the installed module clean. A baseline
run first proves the test is green unmutated (a red test file kills everything it aims at).
Exit 0 only if the baseline is green and every mutant is killed.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OMS = ROOT / "crates" / "te_core" / "src" / "oms"
PY = sys.executable
TEST = [PY, "-B", "-m", "pytest", "tests/test_p3b_parity.py", "-x", "-q", "-p", "no:cacheprovider"]

# name, file, anchor (must occur exactly once), replacement
MUTANTS = [
    ("restore-keeps-new-children", "restore.rs",
     "c.parent_order_id.as_deref() == Some(root.as_str()) && c.state != OrderState::New",
     "c.parent_order_id.as_deref() == Some(root.as_str())"),
    ("restore-cancel-becomes-accepted", "restore.rs",
     '"cancel" => restored_as.push((order.order_id.clone(), OrderState::Cancelled.value()))',
     '"cancel" => restored_as.push((order.order_id.clone(), OrderState::Accepted.value()))'),
    ("pending-cancel-ignores-reason", "restore.rs",
     'command.ends_with(":pending") && last.reason.starts_with("Cancel pending")',
     'command.ends_with(":pending")'),
    ("restore-forgets-earlier-unresolved", "restore.rs",
     "(pre_unresolved.iter().any(|u| u == id) || unresolved.iter().any(|(u, _)| u == id))",
     "(unresolved.iter().any(|(u, _)| u == id))"),
    ("journal-combo-target-on-every-leg", "reconcile.rs",
     "Some(t) if t.instrument.same(&fill.instrument) => ",
     "Some(t) => "),
    ("reconcile-reads-new-orders", "reconcile.rs",
     "Some(o) => o.state != OrderState::New && !terminal(o.state),",
     "Some(o) => !terminal(o.state),"),
    ("positions-tie-takes-last", "reconcile.rs",
     "XX_NOT_HERE", "XX"),  # replaced below: the tie rule lives in restore.rs
    ("positions-latest-fill-is-earliest", "restore.rs",
     "st.fills[k].filled_at.instant() >= f.filled_at.instant()",
     "st.fills[k].filled_at.instant() <= f.filled_at.instant()"),
    ("sync-cancels-non-target-on-leg-off", "options.rs",
     "if is_target && leg_off(&legs, s)? {",
     "if leg_off(&legs, s)? {"),
    ("sync-done-ignores-terminal-entry", "options.rs",
     "if gt(&filled, &zero())? || terminal(entry.state) {",
     "if gt(&filled, &zero())? {"),
    ("holding-skips-cover-check", "options.rs",
     "if side == Side::Sell {\n        let mut after",
     "if false {\n        let mut after"),
    ("holding-overclose-uses-signed-held", "options.rs",
     "gt(quantity, &crate::ledger::ops::abs(&held)?)?",
     "gt(quantity, &held)?"),
    ("duplicate-ignores-flat-positions", "options.rs",
     "if matches!(key, Instrument::Option(_)) && ne(&p.quantity, &zero())? {",
     "if matches!(key, Instrument::Option(_)) {"),
    ("duplicate-counts-terminal-entries", "options.rs",
     "order.parent_order_id.is_none() && !terminal(order.state) && is_structure(&order.instrument)",
     "order.parent_order_id.is_none() && is_structure(&order.instrument)"),
    ("close-replay-any-target", "options.rs",
     "if closed == Some(entry_order_id) {",
     "if closed.is_some() {"),
    ("close-number-starts-at-zero", "options.rs",
     "let number = 1 + st.orders.values()",
     "let number = st.orders.values()"),
    ("close-skew-check-off", "options.rs",
     "if skewed {",
     "if false {"),
]
MUTANTS = [m for m in MUTANTS if m[2] != "XX_NOT_HERE"]


def build() -> None:
    subprocess.run(
        [PY, "-m", "pip", "install", "--no-deps", "--force-reinstall", "-q", "./crates/te_py"],
        cwd=ROOT, check=True, capture_output=True,
    )


def tests() -> subprocess.CompletedProcess:
    return subprocess.run(TEST, cwd=ROOT, capture_output=True, text=True)


def main() -> int:
    only = set(sys.argv[1:])
    build()
    base = tests()
    if base.returncode != 0:
        print("BASELINE RED - every mutant would be 'killed' by that; fix the tests first")
        print(base.stdout[-3000:])
        return 3
    print("baseline green")
    survivors: list[str] = []
    originals = {f: (OMS / f).read_bytes() for f in {m[1] for m in MUTANTS}}
    try:
        for name, fname, anchor, repl in MUTANTS:
            if only and name not in only:
                continue
            path = OMS / fname
            original = originals[fname]
            text = original.decode("utf-8")
            if text.count(anchor) != 1:
                print(f"ANCHOR MISMATCH ({text.count(anchor)}x): {name}")
                survivors.append(name + " (anchor)")
                continue
            try:
                path.write_bytes(text.replace(anchor, repl).encode("utf-8"))
                try:
                    build()
                except subprocess.CalledProcessError as err:
                    print(f"DOES NOT BUILD: {name}\n{(err.stderr or b'')[-1500:]}")
                    survivors.append(name + " (build)")
                    continue
                result = tests()
                if result.returncode == 0:
                    print(f"SURVIVED: {name}")
                    survivors.append(name)
                else:
                    failed = [ln for ln in result.stdout.splitlines() if ln.startswith("FAILED")]
                    print(f"killed: {name} <- {failed[0][:140] if failed else 'error'}")
            finally:
                path.write_bytes(original)
    finally:
        for f, data in originals.items():
            (OMS / f).write_bytes(data)
        build()
    print(f"{len(MUTANTS)} mutants, survivors: {survivors or 'none'}")
    return 1 if survivors else 0


if __name__ == "__main__":
    raise SystemExit(main())

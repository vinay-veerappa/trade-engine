"""P6C hand mutants for the per-root Globex calendars: assertion kills only, always restored, final green run.

Needs PYO3_PYTHON and CARGO_TARGET_DIR in the environment (a private target dir). Usage:
    python tools/mutate_p6c.py [mutant-name ...]
A mutant counts as KILLED only when `cargo test -p te_core` compiles and a test fails on an assertion; a mutant that
fails to compile, or survives, is reported INVALID and the script exits non-zero.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CORE = ROOT / "crates" / "te_core"
GLOBEX = CORE / "src" / "calendar" / "globex.rs"
ENERGY = CORE / "data" / "cme_energy_holidays.csv"
METALS = CORE / "data" / "cme_metals_holidays.csv"
CMD = ["cargo", "test", "--manifest-path", str(ROOT / "crates" / "Cargo.toml"), "-p", "te_core", "--no-fail-fast"]


def replace_once(anchor: str, repl: str):
    def f(text: str) -> str:
        assert text.count(anchor) == 1, (anchor, text.count(anchor))
        return text.replace(anchor, repl)
    return f


def drop_row(date: str):
    def f(text: str) -> str:
        lines = text.splitlines(keepends=True)
        keep = [l for l in lines if not l.startswith(f"{date},")]
        assert len(keep) == len(lines) - 1, date
        return "".join(keep)
    return f


def row_halt(date: str, old: str, new: str):
    def f(text: str) -> str:
        pat = re.compile(rf"^({re.escape(date)},early_halt,){re.escape(old)}(,)", re.M)
        out, n = pat.subn(rf"\g<1>{new}\g<2>", text)
        assert n == 1, (date, n)
        return out
    return f


MUTANTS = (
    ("cl-routed-to-equity", GLOBEX,
     replace_once('RootInfo { root: "CL", group: Group::Energy,', 'RootInfo { root: "CL", group: Group::Equity,')),
    ("energy-early-halt-read-as-1300", ENERGY, row_halt("2024-07-04", "13:30", "13:00")),
    ("metals-closure-dropped", METALS, drop_row("2024-12-25")),
    ("mcl-listing-check-removed", GLOBEX,
     replace_once('RootInfo { root: "MCL", group: Group::Energy, listed_from: Some(d(2021, 7, 11)) }',
                  'RootInfo { root: "MCL", group: Group::Energy, listed_from: None }')),
    ("ym-own-floor-ignored", GLOBEX,
     replace_once('RootInfo { root: "YM", group: Group::Equity, listed_from: Some(d(2008, 1, 27)) }',
                  'RootInfo { root: "YM", group: Group::Equity, listed_from: None }')),
    ("mgc-listing-check-removed", GLOBEX,
     replace_once('RootInfo { root: "MGC", group: Group::Metals, listed_from: Some(d(2010, 10, 3)) }',
                  'RootInfo { root: "MGC", group: Group::Metals, listed_from: None }')),
    ("rty-listing-check-removed", GLOBEX,
     replace_once('RootInfo { root: "RTY", group: Group::Equity, listed_from: Some(d(2017, 7, 9)) }',
                  'RootInfo { root: "RTY", group: Group::Equity, listed_from: None }')),
    ("metals-early-halt-read-as-1300", METALS, row_halt("2024-05-27", "14:30", "13:00")),
)


def run():
    return subprocess.run(CMD, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace")


def main() -> int:
    assert ROOT.name == "rust-p6c", ROOT
    assert os.environ.get("PYO3_PYTHON") and os.environ.get("CARGO_TARGET_DIR"), "set PYO3_PYTHON and CARGO_TARGET_DIR"
    selected = set(sys.argv[1:])
    assert selected <= {m[0] for m in MUTANTS}, selected
    originals = {p: p.read_bytes() for p in {m[1] for m in MUTANTS}}
    bad = []
    try:
        base = run()
        print(f"baseline exit={base.returncode}", flush=True)
        if base.returncode != 0:
            print(base.stdout[-3000:], base.stderr[-3000:])
            return 1
        for name, path, mutate in MUTANTS:
            if selected and name not in selected:
                continue
            text = originals[path].decode("utf-8")
            try:
                path.write_bytes(mutate(text).encode("utf-8"))
                t0 = time.perf_counter()
                proc = run()
                secs = time.perf_counter() - t0
                out = proc.stdout + proc.stderr
                failed = [l.strip() for l in out.splitlines() if l.startswith("test ") and l.rstrip().endswith("FAILED")]
                if "could not compile" in out or "error[E" in out:
                    bad.append(name)
                    print(f"INVALID {name}: does not compile", flush=True)
                elif proc.returncode != 0 and "panicked at" in out and failed:
                    print(f"KILLED {name} ({secs:.0f}s, {len(failed)} failing) first: {failed[0]}", flush=True)
                else:
                    bad.append(name)
                    print(f"INVALID {name}: survived or not an assertion kill (exit {proc.returncode})", flush=True)
            finally:
                path.write_bytes(originals[path])
    finally:
        for p, b in originals.items():
            p.write_bytes(b)
        final = run()
        print(f"restored exit={final.returncode}", flush=True)
        if final.returncode != 0:
            bad.append("restore")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())

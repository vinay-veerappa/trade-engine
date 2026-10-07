"""P7 hand mutants (canonical spelling, bound, migration, reverse, alias dedupe): kills only, always restore.

Rust mutants (money.rs) are built into the worktree venv and run against the Python tests that compare
Rust with the Python canon (`test_p7_spelling.py`); Python mutants (codec, options, migration tool) need
no build. A mutant counts as KILLED only when the run fails through a test (exit 1, a FAILED line, no
collection/import error); one that passes is a SURVIVOR and the run exits 1. CRLF checkouts: multi-line
anchors take the file's line ending. The final restore is unconditional and is verified by sha256, then
rebuilt and run green.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = Path(sys.executable).resolve()
ENV = dict(os.environ, PATH=str(Path.home() / ".cargo" / "bin") + os.pathsep + os.environ["PATH"],
           PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8", PYO3_PYTHON=str(PY),
           CARGO_TARGET_DIR=os.environ.get("CARGO_TARGET_DIR", str(ROOT / "crates" / "target")))
TEST = [str(PY), "-B", "-m", "pytest", str(ROOT / "tests" / "test_p7_spelling.py"),
        str(ROOT / "tests" / "test_p7_migrate.py"), "-x", "-q", "--tb=short", "-p", "no:cacheprovider",
        "--basetemp", str(ROOT / ".ci-local" / "p7-mutant-tests")]
MONEY = "crates/te_core/src/money.rs"
CODEC = "src/trade_engine/ledger/codec.py"
OPTIONS = "src/trade_engine/oms/options.py"
MIGRATE = "tools/p7_migrate.py"

# (name, kind, file, [(anchor, replacement), ...])
MUTANTS = (
    ("rust-zero-spelled-negative", "rust", MONEY,
     [("        if scale == 0 {\n            out.push_str(&digits);",
       "        if scale == 0 {\n            if m == 0 { out.push('-'); }\n            out.push_str(&digits);")]),
    ("rust-trailing-zeros-kept", "rust", MONEY,
     [("        while coef % 10 == 0 {\n            coef /= 10;\n            exp += 1;\n        }\n        if exp > 0 {",
       "        while false && coef % 10 == 0 {\n            coef /= 10;\n            exp += 1;\n        }\n        if exp > 0 {")]),
    ("rust-overflow-becomes-rounding", "rust", MONEY,
     [("        let scale = -exp;\n        if scale > MAX_SCALE as i64 || coef >= MANTISSA_LIMIT {",
       "        let mut scale = -exp;\n        if scale > MAX_SCALE as i64 && scale - (MAX_SCALE as i64) < 38 {\n"
       "            coef /= POW10[(scale - MAX_SCALE as i64) as usize];\n            scale = MAX_SCALE as i64;\n        }\n"
       "        if scale > MAX_SCALE as i64 || coef >= MANTISSA_LIMIT {")]),
    ("rust-mantissa-bound-off-by-one", "rust", MONEY,
     [("        if scale > MAX_SCALE as i64 || coef >= MANTISSA_LIMIT {",
       "        if scale > MAX_SCALE as i64 || coef > MANTISSA_LIMIT {")]),
    ("python-zero-spelled-negative", "py", CODEC,
     [('    if coefficient == 0:\n        return "0"', '    if coefficient == 0:\n        return "-0" if sign else "0"')]),
    ("python-trailing-zeros-kept", "py", CODEC,
     [("    while coefficient % 10 == 0:\n        coefficient //= 10", "    while False:\n        coefficient //= 10")]),
    ("python-overflow-becomes-rounding", "py", CODEC,
     [("    if scale > _MAX_SCALE or coefficient >= _MANTISSA_LIMIT:", "    if coefficient >= _MANTISSA_LIMIT:")]),
    ("python-exponent-spelling", "py", CODEC,
     [("    if exponent > 0:\n        coefficient *= 10**exponent\n        exponent = 0\n",
       "    if exponent > 0 and False:\n        coefficient *= 10**exponent\n        exponent = 0\n")]),
    ("alias-map-not-consulted-on-dedupe", "py", OPTIONS,
     [("if stored == legacy or self._ledger.fingerprint_alias(legacy) == stored:", "if stored == legacy:")]),
    ("legacy-fingerprint-not-recomputed", "py", OPTIONS,
     [("        legacy = self._intent_fingerprint(intent, legacy=True)\n", "        legacy = current\n")]),
    ("alias-accepts-any-terms", "py", OPTIONS,
     [("if stored == legacy or self._ledger.fingerprint_alias(legacy) == stored:", "if True:")]),
    ("reverse-does-not-restore-original-bytes", "py", MIGRATE,
     [('                    r = {**r, "payload_json": original[(table, r[key])]}', "                    r = r")]),
    ("reverse-accepts-an-incomplete-migration", "py", MIGRATE,
     [('        if info.get("state") != "complete":', "        if False:")]),
    ("migration-writes-in-place", "py", MIGRATE,
     [('    if path.exists():\n        raise MigrationError(f"{path} exists; the migration never writes in place")', "    if False:\n        pass"),
      ('    if src.resolve() == dst.resolve():\n        raise MigrationError("source and destination are the same file")', "    if False:\n        pass")]),
    ("migration-overwrites-an-existing-destination", "py", MIGRATE,
     [('    if path.exists():\n        raise MigrationError(f"{path} exists; the migration never writes in place")', "    if False:\n        pass")]),
    ("data-directory-not-guarded", "py", MIGRATE,
     [("    if _under(path, PROTECTED) and not owns:", "    if False:")]),
    ("journal-mode-not-truncate", "py", MIGRATE,
     [('PRAGMA journal_mode=TRUNCATE")', 'PRAGMA journal_mode=DELETE")')]),
    ("respell-rounds-to-integer", "py", MIGRATE,
     [('return {"d": canon_decimal(Decimal(node["d"]))}', 'return {"d": canon_decimal(Decimal(node["d"]).quantize(Decimal("1")))}')]),
    ("respell-rounds-and-value-check-off", "py", MIGRATE,
     [('return {"d": canon_decimal(Decimal(node["d"]))}', 'return {"d": canon_decimal(Decimal(node["d"]).quantize(Decimal("1")))}'),
      ("        if before != after:", "        if False:")]),
    ("fingerprint-not-rehashed", "py", MIGRATE,
     [('            new_tree["f"]["fingerprint"] = new_fp', '            new_tree["f"]["fingerprint"] = old_tree["f"]["fingerprint"]')]),
    ("key-map-not-recorded", "py", MIGRATE,
     [('            key_map.append((old_tree["f"]["fingerprint"], new_fp, row["seq"], kind))', "            pass")]),
    ("out-of-bound-literal-kept", "py", MIGRATE,
     [('                raise MigrationError(f"decimal {node[\'d\']!r} cannot be spelled canonically: {err}") from err',
       "                return node")]),
)


def build():
    start = time.perf_counter()
    subprocess.run([str(PY), "-m", "pip", "install", "--no-deps", "--force-reinstall", "-q",
                    str(ROOT / "crates" / "te_py")], cwd=ROOT, env=ENV, check=True, capture_output=True)
    return time.perf_counter() - start


def tests():
    return subprocess.run(TEST, cwd=ROOT, env=ENV, capture_output=True, text=True, encoding="utf-8", errors="replace")


def cargo_money():
    """The Rust arithmetic proptests (Money against the PyDec oracle): a second kill door for money.rs."""
    return subprocess.run(["cargo", "test", "-q", "-p", "te_core", "--lib", "money"], cwd=ROOT / "crates", env=ENV,
                          capture_output=True, text=True, encoding="utf-8", errors="replace")


def killed(proc):
    failed = [line for line in proc.stdout.splitlines() if line.startswith("FAILED")]
    ok = (proc.returncode == 1 and failed and "ERROR collecting" not in proc.stdout
          and "ImportError" not in proc.stdout and "SyntaxError" not in proc.stdout)
    return failed[0] if ok else None


def main():
    assert PY == (ROOT / ".venv" / "Scripts" / "python.exe").resolve()
    (ROOT / ".ci-local").mkdir(exist_ok=True)
    selected = set(sys.argv[1:])
    assert selected <= {m[0] for m in MUTANTS}, selected
    files = sorted({ROOT / m[2] for m in MUTANTS})
    originals = {f: f.read_bytes() for f in files}
    kills, failures = [], []
    baseline_green = restored_green = False
    try:
        build()
        baseline = tests()
        baseline_green = baseline.returncode == 0
        print(f"baseline exit={baseline.returncode}\n{baseline.stdout[-600:]}", flush=True)
        if baseline_green:
            for name, kind, rel, edits in MUTANTS:
                if selected and name not in selected:
                    continue
                source = ROOT / rel
                text = originals[source].decode("utf-8")
                crlf = "\r\n" in text
                for anchor, replacement in edits:
                    if crlf:
                        anchor, replacement = anchor.replace("\n", "\r\n"), replacement.replace("\n", "\r\n")
                    assert text.count(anchor) == 1, (name, anchor, text.count(anchor))
                    text = text.replace(anchor, replacement)
                try:
                    source.write_bytes(text.encode("utf-8"))
                    if kind == "rust":
                        try:
                            build()
                        except subprocess.CalledProcessError as exc:
                            failures.append((name, "compile failed"))
                            print(f"INVALID {name}: compile failed {exc.stderr[-1500:]!r}", flush=True)
                            continue
                    proc = tests()
                    kill = killed(proc)
                    if not kill and kind == "rust" and proc.returncode == 0:
                        proc = cargo_money()
                        kill = f"cargo test money: {proc.stdout.count('FAILED') or 1} failed" if proc.returncode != 0 and "panicked" in proc.stdout else None
                    if kill:
                        kills.append((name, kill))
                        print(f"KILLED {name}: {kill}", flush=True)
                    else:
                        failures.append((name, "survived" if proc.returncode == 0 else "not a test kill"))
                        print(f"INVALID {name} exit={proc.returncode}\n{proc.stdout[-2500:]}", flush=True)
                finally:
                    source.write_bytes(originals[source])
                    if kind == "rust":
                        build()
    finally:
        for source, original in originals.items():
            source.write_bytes(original)
        build()
        restored = tests()
        restored_green = restored.returncode == 0
        print(f"restored exit={restored.returncode}\n{restored.stdout[-600:]}", flush=True)
        for source, original in originals.items():
            assert source.read_bytes() == original
            print(f"restored SHA256 {source.name} {hashlib.sha256(original).hexdigest()}", flush=True)
    print(f"killed={len(kills)} failures={failures}", flush=True)
    return 0 if baseline_green and restored_green and not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Authoritative local CI gate for trade-engine.

Runs the same checks as GitHub Actions on this machine before pushing.
"""

from __future__ import annotations

import argparse
import ast
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = REPO_ROOT / "src"


def say(msg: str) -> None:
    print(f"[ci_local] {msg}", flush=True)


def run_command(cmd: list[str], cwd: Path = REPO_ROOT) -> tuple[int, str]:
    say(f"Running: {' '.join(cmd)}")
    p = subprocess.run(
        cmd,
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    output = (p.stdout or "") + (p.stderr or "")
    return p.returncode, output


def check_git_clean(include_uncommitted: bool) -> bool:
    code, out = run_command(["git", "status", "--porcelain"])
    dirty = [line.strip() for line in out.splitlines() if line.strip()]
    if dirty:
        if include_uncommitted:
            say(f"Working tree is dirty ({len(dirty)} files), continuing because --include-uncommitted is set.")
            return True
        else:
            say("FAIL: Working tree has uncommitted changes:")
            for f in dirty:
                print(f"  {f}")
            say("Commit your changes or pass --include-uncommitted.")
            return False
    return True


def check_invariants() -> bool:
    say("Checking invariants (I7: no uncontrolled clock reads in src/)...")
    violations: list[str] = []
    for py_file in SRC_DIR.rglob("*.py"):
        code = py_file.read_text(encoding="utf-8")
        try:
            tree = ast.parse(code, filename=str(py_file))
        except SyntaxError as e:
            violations.append(f"{py_file}: SyntaxError: {e}")
            continue

        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Attribute) and node.func.attr == "now":
                    violations.append(f"{py_file.name}:{node.lineno} calls datetime.now()")
                if isinstance(node.func, ast.Attribute) and node.func.attr == "time":
                    if isinstance(node.func.value, ast.Name) and node.func.value.id == "time":
                        violations.append(f"{py_file.name}:{node.lineno} calls time.time()")

    if violations:
        say("FAIL: Invariant violations found:")
        for v in violations:
            print(f"  {v}")
        return False

    say("Invariants check passed.")
    return True


def check_version() -> bool:
    say("Checking python -m trade_engine --version...")
    code, out = run_command([sys.executable, "-m", "trade_engine", "--version"])
    print(out.strip())
    if code != 0:
        say(f"FAIL: --version returned exit code {code}")
        return False
    if "trade-engine" not in out or "from" not in out:
        say("FAIL: --version output did not include version and resolved path")
        return False
    say("Version check passed.")
    return True


def run_tests() -> bool:
    say("Running test suite (pytest -q)...")
    code, out = run_command([sys.executable, "-m", "pytest", "-q"])
    print(out.strip())
    if code != 0:
        say(f"FAIL: pytest returned exit code {code}")
        return False
    say("All tests passed.")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Authoritative local CI runner")
    parser.add_argument(
        "--include-uncommitted",
        action="store_true",
        help="Allow running tests with uncommitted working tree changes",
    )
    args = parser.parse_args()

    say(f"Starting local CI check in {REPO_ROOT}...")

    if not check_git_clean(args.include_uncommitted):
        return 1

    if not check_invariants():
        return 1

    if not check_version():
        return 1

    if not run_tests():
        return 1

    say("ALL CI CHECKS PASSED (exit code 0).")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Authoritative local CI gate for trade-engine.

Runs the same checks as GitHub Actions on this machine before pushing.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = REPO_ROOT / "src"

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.invariant_checks import check_i7_invariants


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
    say("Checking invariants (I7: no uncontrolled clock reads in src/ outside WallClock)...")
    violations = check_i7_invariants(SRC_DIR, allowlist={"trade_engine/clock/wall.py"})
    if violations:
        say("FAIL: Invariant violations found:")
        for v in violations:
            print(f"  {v}")
        return False

    say("Invariants check passed.")
    return True


def resolve_python() -> str:
    """Resolve the python interpreter to use, prioritizing local .venv if current interpreter lacks package."""
    res = subprocess.run(
        [sys.executable, "-c", "import trade_engine"],
        capture_output=True,
        text=True,
    )
    if res.returncode == 0:
        return sys.executable

    windows_venv = REPO_ROOT / ".venv" / "Scripts" / "python.exe"
    if windows_venv.is_file():
        return str(windows_venv)

    posix_venv = REPO_ROOT / ".venv" / "bin" / "python"
    if posix_venv.is_file():
        return str(posix_venv)

    return sys.executable


def check_version() -> bool:
    py_exe = resolve_python()
    say(f"Checking {py_exe} -m trade_engine --version...")
    code, out = run_command([py_exe, "-m", "trade_engine", "--version"])
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
    py_exe = resolve_python()
    say(f"Running test suite ({py_exe} -m pytest -q)...")
    code, out = run_command([py_exe, "-m", "pytest", "-q"])
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

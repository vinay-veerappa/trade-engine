"""Authoritative local CI gate for trade-engine.

Runs the same checks as GitHub Actions on this machine before pushing.
"""

from __future__ import annotations

import argparse
import os
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


RUST_WORKSPACE = REPO_ROOT / "crates"

# I7: only the designated owner clock may read system time.
RUST_CLOCK_READS = ("Utc::now", "Local::now", "SystemTime::now", "Instant::now", "OffsetDateTime::now")


def check_rust_invariants() -> bool:
    say("Checking invariants (I7 in Rust: no clock reads under crates/*/src)...")
    hits = []
    for path in sorted(RUST_WORKSPACE.glob("*/src/**/*.rs")):
        if path.relative_to(RUST_WORKSPACE).as_posix() == "te_host/src/clock.rs":
            continue
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if any(read in line for read in RUST_CLOCK_READS):
                hits.append(f"{path.relative_to(REPO_ROOT)}:{n}: {line.strip()}")
    if hits:
        say("FAIL: clock reads in Rust:")
        for h in hits:
            print(f"  {h}")
        return False
    say("Rust invariants check passed.")
    return True


def run_rust_tests() -> bool:
    # Default features link embedding normally; maturin alone enables extension mode.
    os.environ["PYO3_PYTHON"] = resolve_python()
    os.environ["CARGO_TARGET_DIR"] = str(RUST_WORKSPACE / "target")
    code, out = run_command(["cargo", "test", "--manifest-path", str(RUST_WORKSPACE / "Cargo.toml"), "--workspace", "-q"])
    print(out.strip()[-2000:])
    if code != 0:
        say(f"FAIL: cargo test returned exit code {code}")
        return False
    say("Rust tests passed.")
    return True


def build_runtime() -> bool:
    """Missing release binary or its private DLL is an error, never a test skip."""
    code, out = run_command([resolve_python(), "-B", str(REPO_ROOT / "tools" / "build_p4c_t3.py"),
                             "--native-only"])
    print(out.strip()[-4000:])
    if code != 0:
        say("FAIL: building the embedded release runtime failed")
        return False
    say("Embedded release runtime built.")
    return True


def build_extension() -> bool:
    """Build and install trade_engine_rs (docs/RUST_PORT.md D5).

    pip drives maturin through crates/te_py/pyproject.toml, which works with or
    without a virtualenv. A failed build fails the gate: the Python tests import
    the module and must never run against a stale or missing one.
    """
    py_exe = resolve_python()
    code, out = run_command([py_exe, "-m", "pip", "install", "--no-deps", "--force-reinstall", "-q", str(RUST_WORKSPACE / "te_py")])
    if code != 0:
        print(out.strip()[-4000:])
        say(f"FAIL: building trade_engine_rs returned exit code {code}")
        return False
    code, out = run_command([py_exe, "-c", "import trade_engine_rs; print(trade_engine_rs.__file__)"])
    print(out.strip())
    if code != 0:
        say("FAIL: trade_engine_rs did not import after building")
        return False
    say("trade_engine_rs built.")
    return True


# Tests that need files only the owner's machine has. They stay mandatory there (a missing
# input is a failure, never a skip); a GitHub-hosted runner does not run them.
HOSTED_RUNNER_EXCLUDED = (
    # The flip drives the client repo's scan_engine over the recorded SPX chain tape.
    "tests/test_p4c_flip.py",
)


def run_tests() -> bool:
    py_exe = resolve_python()
    pytest_args = ["-q"]
    if os.environ.get("GITHUB_ACTIONS") == "true":
        for path in HOSTED_RUNNER_EXCLUDED:
            pytest_args.append(f"--ignore={path}")
        say(f"Hosted runner: not running {', '.join(HOSTED_RUNNER_EXCLUDED)} (needs the owner's machine).")
    say(f"Running test suite ({py_exe} -m pytest {' '.join(pytest_args)})...")
    code, out = run_command([py_exe, "-m", "pytest", *pytest_args])
    print(out.strip())
    if code != 0:
        say(f"FAIL: pytest returned exit code {code}")
        return False
    say("All tests passed.")
    return True


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
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

    if not check_rust_invariants():
        return 1

    if not check_version():
        return 1

    if not run_rust_tests():
        return 1

    if not build_runtime():
        return 1

    if not build_extension():
        return 1

    if not run_tests():
        return 1

    say("ALL CI CHECKS PASSED (exit code 0).")
    return 0


if __name__ == "__main__":
    sys.exit(main())

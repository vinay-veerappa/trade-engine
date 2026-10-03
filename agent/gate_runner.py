"""
gate_runner.py -- the commands agent-loop's gates run for the `rust-trade-engine` profile.

agent-loop 0.6.7 reads only the NT8 runner (`[FAIL] msg` + `RESULTS: Passed = N, Failed = M`),
pytest's summary, and MSBuild's `error CS1234`. This script runs cargo / pytest and
rewrites their output into those shapes, so a red Rust test, a red pytest node and a Rust
compile error each reach the gates as a line `expect_green` can match.

    python -m agent.gate_runner build
    python -m agent.gate_runner pycheck F ...
    python -m agent.gate_runner test  [--pytest ID ...]    full gate: cargo unit + the named pytest ids
    python -m agent.gate_runner focused NAME ...           NAME is a pytest node id or a cargo test name

Gates
  compile    `cargo build -p te_core --tests`, own CARGO_TARGET_DIR (never shared with a dev build).
             Rust errors are echoed as `error RS0425: msg at file:line -- detail`.
  lint       py_compile of the touched Python files (the src/ shims); `.rs` files are ignored here.
  unit       `cargo test -p te_core --no-fail-fast` (never the workspace: te_py links pyo3, which
             refuses a Python newer than it supports -- docs/RUST_PORT.md "Working rules").
             Each failed test -> `[FAIL] <path::of::test>`; per-binary results are SUMMED into one
             RESULTS line.
  extension  when pytest ids are named: rebuild `trade_engine_rs` (crates/te_py) into a PER-RUN
             directory, put it first on PYTHONPATH, run exactly those ids. It is never
             `pip install`ed into the shared venv: that would swap the extension under every other
             worktree's tests while the loop runs. Each failed id -> `[FAIL] <node id>`.

Environment
  AGENT_LOOP_CARGO_TARGET      cargo target dir for build+test (default: a cache outside the worktree;
                               the loop makes a fresh worktree per run and an in-tree target would
                               rebuild every dependency every ticket)
  AGENT_LOOP_CARGO_TARGET_EXT  cargo target dir for the pyo3 extension build
  TE_PYTHON                    interpreter for pip/pytest (default: the engine's own .venv)
Exit code is the first non-zero of the stages that ran.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

_CACHE = Path.home() / ".cache"
TARGET = os.environ.get("AGENT_LOOP_CARGO_TARGET", str(_CACHE / "agent-loop-te-core-target"))
TARGET_EXT = os.environ.get("AGENT_LOOP_CARGO_TARGET_EXT", str(_CACHE / "agent-loop-te-ext-target"))
PYTHON = os.environ.get("TE_PYTHON", r"C:\Users\vinay\trade-engine\.venv\Scripts\python.exe")
MANIFEST = "crates/Cargo.toml"

_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_FAILED = re.compile(r"^test (?P<name>\S+) \.\.\. FAILED", re.MULTILINE)
_LISTED = re.compile(r"^    (?P<name>\S+)$")
_RESULT = re.compile(r"^test result: \w+\. (?P<p>\d+) passed; (?P<f>\d+) failed;", re.MULTILINE)
_ERROR = re.compile(r"^error(?:\[E(?P<code>\d+)\])?: (?P<msg>.+)$")
_LOC = re.compile(r"^\s*--> (?P<loc>\S+)")
_PRIMARY = re.compile(r"^\s*(?:\d+\s*)?\|.*?\^+ (?P<t>\S.*)$")
_HELP = re.compile(r"^\s*= help: (?P<t>.+)$")
_PYFAIL = re.compile(r"^(?:FAILED|ERROR) (?P<id>\S+::\S+?)(?: - .*)?$", re.MULTILINE)
_PYCOUNT = re.compile(r"(\d+) (passed|failed|errors?)\b")


def is_pytest_id(name: str) -> bool:
    """`tests/test_x.py::test_y` (or a bare `tests/test_x.py`); anything else is a cargo test name."""
    return ".py" in name


def _run(cmd: list[str], env_extra: dict[str, str] | None = None) -> tuple[int, str]:
    env = dict(os.environ)
    env.update(env_extra or {})
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", env=env)
    return p.returncode, _ANSI.sub("", (p.stdout or "") + (p.stderr or ""))


def _cargo(args: list[str], target: str = "") -> tuple[int, str]:
    return _run(["cargo", *args], {"CARGO_TARGET_DIR": target or TARGET})


def _detail(lines: list[str], start: int) -> str:
    parts: list[str] = []
    for ln in lines[start:]:
        if not ln.strip() or _ERROR.match(ln):
            break
        m = _PRIMARY.match(ln) or _HELP.match(ln)
        if m and m.group("t") not in parts:
            parts.append(m.group("t"))
    return f" -- {'; '.join(parts)}"[:400] if parts else ""


def rust_errors(out: str) -> list[str]:
    lines = out.splitlines()
    found = []
    for i, ln in enumerate(lines):
        m = _ERROR.match(ln)
        if not m:
            continue
        msg = m.group("msg")
        if msg.startswith(("could not compile", "test failed", "aborting")) or "target failed" in msg or "targets failed" in msg:
            continue
        loc = ""
        for nxt in lines[i + 1 : i + 4]:
            lm = _LOC.match(nxt)
            if lm:
                loc = f" at {lm.group('loc')}"
                break
        found.append(f"error RS{m.group('code') or '0000'}: {msg}{loc}{_detail(lines, i + 1)}")
    return found


def failed_names(out: str) -> list[str]:
    """Progress lines plus libtest's closing `failures:` list (a binary that logs outside
    libtest's capture can interleave a log line into a progress line)."""
    names = [m.group("name") for m in _FAILED.finditer(out)]
    block: list[str] | None = None
    for ln in out.splitlines():
        if ln == "failures:":
            block = []
        elif ln.startswith("test result:"):
            names.extend(block or [])
            block = None
        elif block is not None:
            m = _LISTED.match(ln)
            if m:
                block.append(m.group("name"))
    return list(dict.fromkeys(names))


def _emit(text: str) -> None:
    print(text, flush=True)


def cargo_unit(filters: list[str] | None = None) -> tuple[int, int, int, bool]:
    """-> (exit code, passed, failed, ran). Prints raw output, then the translated lines."""
    args = ["test", "--manifest-path", MANIFEST, "-p", "te_core", "--no-fail-fast"]
    if filters:
        args += ["--", *filters]
    code, out = _cargo(args)
    _emit(out)
    for e in rust_errors(out):
        _emit(e)
    for name in failed_names(out):
        _emit(f"[FAIL] {name}")
    results = list(_RESULT.finditer(out))
    ran = bool(results) and "could not compile" not in out
    p = sum(int(m.group("p")) for m in results)
    f = sum(int(m.group("f")) for m in results)
    return code, p, f, ran


def build_extension() -> tuple[int, str, str]:
    """-> (exit code, output, directory holding trade_engine_rs). Per-run dir: see module doc."""
    dest = tempfile.mkdtemp(prefix="te_ext_")
    code, out = _run(
        [PYTHON, "-m", "pip", "install", "--no-deps", "--force-reinstall", "--no-build-isolation",
         "--target", dest, "./crates/te_py"],
        # --no-build-isolation: maturin comes from the engine's venv, which must be on PATH
        # (pip finds `maturin` as an executable, not as a module).
        {"CARGO_TARGET_DIR": TARGET_EXT, "PATH": str(Path(PYTHON).parent) + os.pathsep + os.environ.get("PATH", "")},
    )
    return code, out, dest


def pytest_ids(ids: list[str]) -> tuple[int, int, int, bool]:
    code, out, ext = build_extension()
    if code != 0:
        _emit(out)
        for e in rust_errors(out):
            _emit(e)
        # The extension did not build: report every named id as failed, so expect_green
        # keeps matching instead of the run reading as "ran, nothing red".
        for i in ids:
            _emit(f"[FAIL] {i}")
        _emit("[FAIL] trade_engine_rs extension build")
        return code, 0, len(ids), True
    code, out = _run(
        [PYTHON, "-m", "pytest", *ids, "-q", "--tb=short", "-p", "no:cacheprovider", "-rfE", "-o", "pythonpath=src"],
        {"PYTHONPATH": ext + os.pathsep + os.environ.get("PYTHONPATH", ""), "PYTHONDONTWRITEBYTECODE": "1"},
    )
    _emit(out)
    for m in dict.fromkeys(x.group("id") for x in _PYFAIL.finditer(out)):
        _emit(f"[FAIL] {m}")
    counts: dict[str, int] = {}
    for line in reversed(out.splitlines()):
        found = _PYCOUNT.findall(line)
        if found:
            for n, kind in found:
                counts["errors" if kind.startswith("error") else kind] = int(n)
            break
    ran = bool(counts.keys() & {"passed", "failed", "errors"})
    return code, counts.get("passed", 0), counts.get("failed", 0) + counts.get("errors", 0), ran


def cmd_build() -> int:
    code, out = _cargo(["build", "--manifest-path", MANIFEST, "-p", "te_core", "--tests"])
    _emit(out)
    for e in rust_errors(out):
        _emit(e)
    return code


def cmd_pycheck(files: list[str]) -> int:
    rc = 0
    for f in files:
        if not f.endswith(".py"):
            continue
        p = subprocess.run([PYTHON, "-m", "py_compile", f], capture_output=True, text=True, encoding="utf-8", errors="replace")
        if p.returncode:
            rc = 1
            _emit(f"error: {f}: {(p.stderr or p.stdout).strip()[-600:]}")
    return rc


def _summary(parts: list[tuple[int, int, int, bool]]) -> int:
    # A stage that never ran prints no RESULTS line: "did not run", not a vacuous pass.
    if all(p[3] for p in parts):
        _emit(f"RESULTS: Passed = {sum(p[1] for p in parts)}, Failed = {sum(p[2] for p in parts)}")
    return next((p[0] for p in parts if p[0]), 0)


def cmd_test(ids: list[str]) -> int:
    parts = [cargo_unit()]
    if ids:
        parts.append(pytest_ids(ids))
    return _summary(parts)


def cmd_focused(names: list[str]) -> int:
    py = [n for n in names if is_pytest_id(n)]
    rs = [n for n in names if not is_pytest_id(n)]
    parts = []
    if rs:
        parts.append(cargo_unit(rs))
    if py:
        parts.append(pytest_ids(py))
    return _summary(parts)


def main(argv: list[str]) -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if not argv:
        print(__doc__, file=sys.stderr)
        return 2
    mode, rest = argv[0], argv[1:]
    if mode == "build":
        return cmd_build()
    if mode == "pycheck":
        return cmd_pycheck(rest)
    if mode == "test":
        return cmd_test([a for a in rest if a != "--pytest"])
    if mode == "focused":
        return cmd_focused(rest)
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

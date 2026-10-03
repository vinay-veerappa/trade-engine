"""
trade_engine_rust.py -- the `rust-trade-engine` profile: agent-loop implementing the Rust port
tickets of docs/RUST_PORT.md in THIS repo (crates/te_core, crates/te_py, the Python shims in src/).

Run a ticket (from the repo root, which must be a git checkout with the ticket file COMMITTED:
the loop builds its worktree from HEAD):

    cd C:\\Users\\vinay\\trade-engine
    C:\\Users\\vinay\\tvDownloadOHLC\\.venv\\Scripts\\python.exe -m agent_loop `
        --profile rust-trade-engine --profile-module agent.trade_engine_rust `
        --tickets agent/tickets_<x>.json --ticket T1

`python -m agent_loop` is the tvDownloadOHLC .venv's editable install of C:\\Users\\vinay\\agent-loop;
`--profile-module` is imported with the CWD on sys.path, so `agent.trade_engine_rust` resolves
against this repo. Add `--list` to validate a ticket file and read the region line ranges
without spending a model call.

Gates (every command goes through agent/gate_runner.py, which rewrites cargo/pytest output
into the `[FAIL] name` / `RESULTS:` / `error RS1234` shapes the loop reads):

  compile    cargo build -p te_core --tests          (own CARGO_TARGET_DIR)
  lint       py_compile of touched src/ shims
  unit       cargo test -p te_core --no-fail-fast    (NEVER the workspace: te_py links pyo3)
  extension  rebuild trade_engine_rs into a per-run dir, run the pytest node ids the ticket names

TICKET CONVENTIONS
* `expect_green` entries are either a cargo test path (`sim::trailing::tests::foo`; any suffix of
  it works, the match is on whole identifiers) or a pytest node id (`tests/test_x.py::test_y`).
  An id containing `.py` is a pytest id. Both must be RED at baseline (test-first), and the
  baseline is captured by the full command, so this module reads the selected ticket's pytest ids
  from `--tickets/--ticket` in sys.argv and bakes them into the command line (the loop gives
  the baseline command no ticket, only the profile). If `--ticket` is omitted, every ticket in
  the file contributes.
* Rust unit tests live INSIDE the file under change (`#[cfg(test)] mod tests`), which is a region
  the implementer may rewrite: for a test-first ticket put the acceptance tests in
  `crates/te_core/tests/*.rs` (protected), against a committed compiling stub.
* Regions: Rust under crates/*/src/, shims under src/trade_engine/. `max_region_lines` is 150:
  split anything larger (te_core's files are far bigger; anchor a function, not a file).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from agent_loop.profiles import Profile, register

_RUNNER = f'"{sys.executable}" -m agent.gate_runner'


def _argv_value(flag: str) -> str:
    for i, a in enumerate(sys.argv):
        if a == flag and i + 1 < len(sys.argv):
            return sys.argv[i + 1]
        if a.startswith(flag + "="):
            return a.split("=", 1)[1]
    return ""


def pytest_ids_from_argv() -> list[str]:
    """The pytest node ids named by the ticket(s) this run will execute ([] when none)."""
    path, want = _argv_value("--tickets"), _argv_value("--ticket")
    if not path or not Path(path).is_file():
        return []
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    tickets = data.get("tickets", data) if isinstance(data, dict) else data
    ids: list[str] = []
    for t in tickets or []:
        if not isinstance(t, dict) or (want and t.get("id") != want):
            continue
        ids += [n for n in t.get("expect_green", ()) if ".py" in n]
    return list(dict.fromkeys(ids))


_IDS = " ".join(f'"{i}"' for i in pytest_ids_from_argv())

RUST_TRADE_ENGINE = Profile(
    name="rust-trade-engine",
    language="rust",
    file_suffixes=(".rs", ".py"),
    line_comment="//",
    block_comment=("/*", "*/"),
    block_kind="decl",
    preprocessor_directives=(),
    build_cmd=f"{_RUNNER} build",
    lint_cmd=f"{_RUNNER} pycheck {{files}}",
    test_cmd=f"{_RUNNER} test" + (f" --pytest {_IDS}" if _IDS else ""),
    focused_test_cmd=f"{_RUNNER} focused {{tests}}",
    lock_name="",
    risk_calls=(),
    file_scope_whitelist=("crates/te_core/src/", "crates/te_py/src/", "src/trade_engine/"),
    protected=(
        "crates/*/tests/*",
        "crates/*/tests/*/*",
        "crates/Cargo.toml",
        "crates/Cargo.lock",
        "crates/*/Cargo.toml",
        "crates/te_py/pyproject.toml",
        "pyproject.toml",
        "tests/*",
        "tools/*",
        "agent/*",
        "docs/*",
        "*.csproj",
    ),
    test_sources=("crates/te_core/tests/*.rs", "tests/test_*.py"),
    context_token_budget=3000,
    round_input_token_budget=40000,
    graph_project="",
    implementer_rules="""\
You are a senior Rust engineer porting a Python trading engine's rule code into `te_core`, a pure
Rust crate that must reproduce the Python's behaviour EXACTLY (docs/RUST_PORT.md): the same
value, or the same refusal with the same type and message. Parity with the frozen Python oracle
is the specification; where the doc comment on a function names the Python it replaces, that
Python is the contract.

HARD CONSTRAINTS:
1. Rust 2021, stable. Add no dependency (Cargo.toml is protected).
2. te_core is pure: no Python, no I/O, NO CLOCK READ (invariant I7: time arrives as an argument),
   no randomness, no global mutable state, no `unsafe`.
3. Money is `rust_decimal` / `PyDec`, never f64, unless the Python was a float. Do not round,
   reorder or "simplify" arithmetic: operation order is part of parity.
4. No `unwrap()`/`expect()`/indexing that can panic on a caller-controlled value: return the
   crate's error (`err("value", ...)`, `MarginError`, ...). Fail closed (I5): a value that cannot be
   read is a refusal, never a default.
5. Do not change a public signature you were given, and do not touch te_py's surface unless the
   ticket says so. te_py is a thin wrapper: no rule logic there.
6. Existing tests (in `#[cfg(test)]` blocks outside your regions, `crates/*/tests/`, `tests/`) must
   still pass, and the acceptance tests must pass. Do not edit, weaken or delete tests.
7. Python shim regions (src/trade_engine/): Python 3.13, keep the existing style; a shim only
   converts types and calls `trade_engine_rs`, it carries no rule.
8. Keep the doc comments' conventions: a function that ports Python says which Python.""",
    reviewer_priorities="""\
You are an adversarial reviewer of a Rust patch to a parity port of a trading engine. Assume the
implementer is confident and wrong.

Check, in priority order:
1. PARITY: does every path return the value the Python oracle returns, or refuse with the same
   kind and message? Compare operation order, rounding, comparison direction (< vs <=), and
   which side of a boundary is inclusive.
2. REFUSALS: malformed or hostile input must refuse, never panic (unwrap, indexing, slicing,
   unchecked arithmetic on Decimal) and never default.
3. CLOCK / I/O: any read of the wall clock, environment or filesystem inside te_core.
4. STATE: a refusal part-way through that leaves state moved when the Python left it unmoved (or
   the reverse).
5. TEST ADEQUACY: would the acceptance tests pass WITHOUT the fix? Does the patch special-case
   a test's inputs?
6. SCOPE: edits outside the regions, public signature changes, new dependencies.

Be specific. Cite the offending line text and give the input that goes wrong.""",
    arbiter_rules="""\
You are the arbiter for a patch to the Rust port of a trading engine's rules. The Rust replaces
Python that decides real orders; a rule that differs from the oracle by one boundary is a
wrong order, which is worse than a crash.

An UPHELD finding must name a concrete input (or input sequence) for which the patched code
returns a different value than the Python oracle, panics, reads the clock, or leaves state moved
differently from the Python. Style, naming, "could be clearer", micro-performance, additional
defensive checks the Python never made, and refactors outside the regions do NOT qualify. A
finding that asks for behaviour the Python does not have is a parity defect, not an improvement.""",
    settled=(
        "te_core's decimal type is PyDec (CPython decimal semantics); do not replace it with "
        "rust_decimal::Decimal inside the ledger/sim/oms modules.",
        "`cargo test -p te_core` is the unit gate and the workspace is never tested whole: te_py links "
        "pyo3, which refuses a Python newer than it supports.",
    ),
)

register(RUST_TRADE_ENGINE)

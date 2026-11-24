"""P6 gate: the retired Python engine logic is out of service (RUST_PORT.md P6, "What retire Python means").

Retire means the file stays at its path as reference and nothing live imports it. This test enforces that in
five ways, each of which fails on its own mutant:

1. Header ledger. Every module under ``src/trade_engine`` that carries the one-line ``# RETIRED (P6)`` header is
   in ``RETIRED`` below, and every ``RETIRED`` module carries it. The file is never deleted or moved.
2. In-repo live entry points (CLI, EOD CLI, HTTP server, runtime plugins, lifecycle runtime) are imported in a
   fresh interpreter. No retired module may be in ``sys.modules`` afterwards.
3. Client API. The staged runtime-owner plugin shim (the daemon door) and every ``scan_engine`` module that
   imports ``trade_engine`` are imported in a fresh interpreter. No retired module may be loaded.
4. AST scan of ``src/``, ``tools/``, ``launch/``, ``staging/`` and the client source (read-only) finds no import
   of a retired module. Tests are scanned separately against the exception list below.
5. The scanner itself is tested against mutants (an appended import must be flagged).

STOPPED (``STOPPED`` below) is the mechanism for a Python module that stays live on the client path with no parity
gate: its client importers are pinned, and the day one stops importing it, this test says to retire it. It is empty
now. The tos_paper session and metrics snapshot modules that used it are deleted (P0b): the client (``scan_engine``)
imports their native twins, and no test or live file imports the old names.

Exceptions (tests and oracle tools may still import them): ``TEST_IMPORT_EXCEPTIONS``. There are no oracle tools
importing a retired or stopped module (the ``tools/`` scan proves it).
"""

from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import re
import subprocess
import sys

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = REPO_ROOT / "src"
PKG_DIR = SRC_DIR / "trade_engine"
SELF = Path(__file__).resolve()

# Retired in place: module -> the stem of the Rust door that is the live path (``trade_engine_rs::<stem>*``).
RETIRED: dict[str, str] = {
    "trade_engine.market_data.wrapper": "market_data_verify",
}

# Still in service on the client path. Module -> client modules that import it directly (pinned).
# Empty: the two modules that were here (tos_paper session, metrics snapshot) are deleted, and their client
# importers now use the native twins. Add an entry here only when a client module must keep importing a
# still-live Python module with no parity gate.
STOPPED: dict[str, frozenset[str]] = {}

# Tests that may import a retired or stopped module (oracle use: the old code is the behaviour the tests pin).
# Empty: no test imports a retired or stopped module any more.
TEST_IMPORT_EXCEPTIONS: dict[str, frozenset[str]] = {}

# In-repo live entry points: importing any of these must not load a retired (or stopped) module.
ENTRY_POINTS = (
    "trade_engine.cli",
    "trade_engine.eod.cli",
    "trade_engine.server.http",
    "trade_engine.runtime.plugins",
    "trade_engine._lifecycle_runtime",
)

HEADER_RE = re.compile(
    r"^# RETIRED \(P6\): reference only; live path is trade_engine_rs::(?P<x>\w+)\. Do not import\.$"
)
STAGED_SHIM = REPO_ROOT / "staging" / "p4c" / "runtime_owner_entry.py"
LIVE_SCAN_DIRS = ("src", "tools", "launch", "staging")

ALL_GUARDED = {**{m: frozenset() for m in RETIRED}, **STOPPED}


# --------------------------------------------------------------------------------------------------------------
# The scanner
# --------------------------------------------------------------------------------------------------------------


def module_name_of(path: Path) -> str | None:
    """Dotted module name of a file under ``src/``, or None for a file outside the package."""
    try:
        rel = path.resolve().relative_to(SRC_DIR)
    except ValueError:
        return None
    parts = list(rel.with_suffix("").parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def imported_modules(tree: ast.AST, own_module: str | None = None, is_package: bool = False) -> set[str]:
    """Every dotted module this tree imports.

    ``from a.b import c`` yields ``a.b`` and ``a.b.c`` (``c`` may be a submodule). Relative imports are resolved
    against ``own_module``. Without it a relative import cannot name a trade_engine module and is skipped.
    """
    found: set[str] = set()
    package = None
    if own_module is not None:
        package = own_module if is_package else own_module.rpartition(".")[0]
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                if package is None:
                    continue
                base = package.split(".")
                if node.level - 1 > len(base) - 1:
                    continue
                base = base[: len(base) - (node.level - 1)]
                module = ".".join(base + ([node.module] if node.module else []))
            else:
                module = node.module or ""
            if not module:
                continue
            found.add(module)
            found.update(f"{module}.{alias.name}" for alias in node.names if alias.name != "*")
    return found


def string_literals(tree: ast.AST) -> set[str]:
    return {n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)}


def guarded_hits(names: set[str], guarded: dict[str, object]) -> set[str]:
    """The guarded modules (or a submodule of one) that appear in ``names``."""
    return {g for g in guarded if any(n == g or n.startswith(g + ".") for n in names)}


def scan_source(text: str, path: Path, guarded: dict[str, object], *, strings: bool = True) -> set[str]:
    """Guarded modules a source text imports (or names in a string literal, the importlib spelling)."""
    tree = ast.parse(text)
    own = module_name_of(path)
    names = imported_modules(tree, own, is_package=path.name == "__init__.py")
    hits = guarded_hits(names, guarded)
    if strings:
        hits |= {g for g in guarded if g in string_literals(tree)}
    return hits


def _py_files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*.py") if "__pycache__" not in p.parts)


def _retired_files() -> set[Path]:
    return {(SRC_DIR / (m.replace(".", "/") + ".py")).resolve() for m in RETIRED}


def _client_src() -> Path | None:
    override = os.environ.get("TE_P6_CLIENT_SRC")
    if override:
        candidate = Path(override)
    else:
        match = re.search(r'_CLIENT_SRC\s*=\s*Path\(r"([^"]+)"\)', STAGED_SHIM.read_text(encoding="utf-8"))
        if not match:
            return None
        candidate = Path(match.group(1))
    return candidate if (candidate / "scan_engine").is_dir() else None


def _client_modules_importing_trade_engine(client_src: Path) -> dict[str, set[str]]:
    """scan_engine module name -> the trade_engine modules it imports (static, read-only)."""
    out: dict[str, set[str]] = {}
    for path in _py_files(client_src / "scan_engine"):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        parts = list(path.relative_to(client_src).with_suffix("").parts)
        is_package = parts[-1] == "__init__"
        if is_package:
            parts = parts[:-1]
        dotted = ".".join(parts)
        names = imported_modules(tree, dotted, is_package=is_package)
        te = {n for n in names if n == "trade_engine" or n.startswith("trade_engine.")}
        if te:
            out[dotted] = te
    return out


# --------------------------------------------------------------------------------------------------------------
# Fresh-interpreter probe
# --------------------------------------------------------------------------------------------------------------

_PROBE = r"""
import importlib, json, sys
errors = {}
for name in json.loads(sys.argv[1]):
    try:
        importlib.import_module(name)
    except BaseException as err:
        errors[name] = [type(err).__name__, getattr(err, "name", None) or "", str(err)[:200]]
loaded = sorted(m for m in sys.modules if m == "trade_engine" or m.startswith("trade_engine."))
print("P6PROBE " + json.dumps({"loaded": loaded, "errors": errors}))
"""


def fresh_import(names: list[str], extra_paths: list[Path], cwd: Path) -> dict:
    """Import ``names`` in a new interpreter (no bytecode written) and report the loaded trade_engine modules."""
    paths = [str(SRC_DIR)] + [str(p) for p in extra_paths]
    inherited = os.environ.get("PYTHONPATH")
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    env["PYTHONPATH"] = os.pathsep.join(paths + ([inherited] if inherited else []))
    proc = subprocess.run(
        [sys.executable, "-B", "-c", _PROBE, json.dumps(names)],
        capture_output=True,
        text=True,
        env=env,
        cwd=cwd,
        timeout=300,
    )
    lines = [ln for ln in proc.stdout.splitlines() if ln.startswith("P6PROBE ")]
    assert lines, f"probe produced no result (exit {proc.returncode}):\n{proc.stderr[-2000:]}"
    return json.loads(lines[-1][len("P6PROBE "):])


def _loaded_guarded(loaded: list[str]) -> set[str]:
    return {m for m in loaded if m in ALL_GUARDED}


# --------------------------------------------------------------------------------------------------------------
# 1. Header ledger
# --------------------------------------------------------------------------------------------------------------


def test_header_ledger_matches_retired_set():
    marked: dict[str, str] = {}
    for path in _py_files(PKG_DIR):
        first = path.read_text(encoding="utf-8").split("\n", 1)[0].rstrip("\r")
        match = HEADER_RE.match(first)
        if match:
            marked[module_name_of(path)] = match.group("x")
        else:
            assert "RETIRED (P6)" not in first, f"{path}: malformed RETIRED header on line 1: {first!r}"
    assert marked == RETIRED, (
        "modules carrying the RETIRED (P6) header differ from RETIRED in this test:\n"
        f"  header only: {sorted(set(marked) - set(RETIRED))}\n"
        f"  declared only: {sorted(set(RETIRED) - set(marked))}\n"
        f"  stem mismatches: {sorted(m for m in set(marked) & set(RETIRED) if marked[m] != RETIRED[m])}"
    )


def test_retired_files_are_kept_never_deleted():
    for module in RETIRED:
        path = SRC_DIR / (module.replace(".", "/") + ".py")
        assert path.is_file() and path.stat().st_size > 0, f"{path} must stay in place as reference"


def test_stopped_modules_are_in_service_and_unmarked():
    for module in STOPPED:
        path = SRC_DIR / (module.replace(".", "/") + ".py")
        assert path.is_file() and path.stat().st_size > 0, f"{path} must stay in place"
        first = path.read_text(encoding="utf-8").split("\n", 1)[0]
        assert "RETIRED" not in first, f"{module} is still imported by the client; do not mark it retired"
    assert not set(STOPPED) & set(RETIRED)


def test_named_live_path_exists_in_the_rust_module():
    import trade_engine_rs as rs  # D5: missing is an error, never a skip

    exported = dir(rs)
    for module, stem in RETIRED.items():
        assert any(name.startswith(stem) for name in exported), (
            f"{module} says its live path is trade_engine_rs::{stem}*, but the module has no such function"
        )


# --------------------------------------------------------------------------------------------------------------
# 2. In-repo live entry points, fresh interpreter
# --------------------------------------------------------------------------------------------------------------


def test_in_repo_entry_points_load_no_retired_or_stopped_module(tmp_path):
    result = fresh_import(list(ENTRY_POINTS), [], tmp_path)
    assert not result["errors"], f"entry points failed to import: {result['errors']}"
    bad = _loaded_guarded(result["loaded"])
    assert not bad, f"live entry points pulled in retired or stopped modules: {sorted(bad)}"
    # Positive control: the probe is not vacuous. The live replacement of the wrapper is what got loaded.
    assert "trade_engine.market_data.wrapper_native" in result["loaded"]
    assert "trade_engine.market_data.wrapper" not in result["loaded"]


# --------------------------------------------------------------------------------------------------------------
# 3. The scan_engine client API and the daemon door, fresh interpreter
# --------------------------------------------------------------------------------------------------------------


def test_client_static_importers_are_pinned():
    client_src = _client_src()
    if client_src is None:
        pytest.skip("client source (tvDownloadOHLC) not found; set TE_P6_CLIENT_SRC")
    importers: dict[str, set[str]] = {m: set() for m in ALL_GUARDED}
    for dotted, names in _client_modules_importing_trade_engine(client_src).items():
        for module in guarded_hits(names, ALL_GUARDED):
            importers[module].add(dotted)
    for module in RETIRED:
        assert not importers[module], f"client modules import retired {module}: {sorted(importers[module])}"
    for module, expected in STOPPED.items():
        assert importers[module] == set(expected), (
            f"client importers of {module} changed: found {sorted(importers[module])}, "
            f"pinned {sorted(expected)}. If the client no longer imports it, retire it "
            "(header, RETIRED entry, drop it from STOPPED); if a new one appeared, a parity gate is needed first."
        )


def test_client_api_and_daemon_door_load_no_retired_module(tmp_path):
    client_src = _client_src()
    if client_src is None:
        pytest.skip("client source (tvDownloadOHLC) not found; set TE_P6_CLIENT_SRC")
    modules = sorted(_client_modules_importing_trade_engine(client_src))
    assert modules, "no scan_engine module imports trade_engine; the client path moved?"
    names = ["runtime_owner_entry"] + modules  # the staged shim first: it is the daemon door
    result = fresh_import(names, [STAGED_SHIM.parent, client_src], tmp_path)
    errors = result["errors"]
    missing_deps = {
        n: e for n, e in errors.items()
        if e[0] == "ModuleNotFoundError" and not e[1].startswith(("trade_engine", "scan_engine"))
    }
    if missing_deps:
        pytest.skip(f"client dependencies are not installed in this interpreter: {sorted(set(e[1] for e in missing_deps.values()))}")
    assert not errors, f"client modules failed to import: {errors}"
    loaded = set(result["loaded"])
    retired_loaded = loaded & set(RETIRED)
    assert not retired_loaded, f"the client path loads retired modules: {sorted(retired_loaded)}"
    # The pinned STOPPED set is what actually loads, no more and no less.
    expected_loaded = {m for m, who in STOPPED.items() if who}
    assert loaded & set(STOPPED) == expected_loaded
    # Positive control: the client path runs the Rust-door wrapper.
    assert "trade_engine.market_data.wrapper_native" in loaded


# --------------------------------------------------------------------------------------------------------------
# 4. AST scans
# --------------------------------------------------------------------------------------------------------------


def _live_violations() -> list[str]:
    skip = _retired_files() | {SELF}
    out: list[str] = []
    for top in LIVE_SCAN_DIRS:
        root = REPO_ROOT / top
        if not root.is_dir():
            continue
        for path in _py_files(root):
            if path.resolve() in skip:
                continue
            for hit in sorted(scan_source(path.read_text(encoding="utf-8"), path, ALL_GUARDED)):
                out.append(f"{path.relative_to(REPO_ROOT).as_posix()} imports {hit}")
    return out


def test_no_live_repo_file_imports_a_retired_or_stopped_module():
    violations = _live_violations()
    assert not violations, "live code imports retired or stopped modules:\n" + "\n".join(violations)


def test_test_importers_are_exactly_the_listed_exceptions():
    actual: dict[str, set[str]] = {}
    for path in _py_files(REPO_ROOT / "tests"):
        if path.resolve() == SELF:
            continue
        try:
            text = path.read_text(encoding="utf-8")
            hits = scan_source(text, path, ALL_GUARDED, strings=False)
        except SyntaxError:
            continue
        if hits:
            actual[path.relative_to(REPO_ROOT).as_posix()] = hits
    declared = {k: set(v) for k, v in TEST_IMPORT_EXCEPTIONS.items()}
    assert actual == declared, (
        "tests importing retired or stopped modules differ from TEST_IMPORT_EXCEPTIONS:\n"
        f"  unlisted: {sorted(set(actual) - set(declared))}\n"
        f"  stale: {sorted(set(declared) - set(actual))}\n"
        f"  changed: {sorted(k for k in set(actual) & set(declared) if actual[k] != declared[k])}"
    )


# --------------------------------------------------------------------------------------------------------------
# 5. Mutants: the scanner and the header check must catch what they exist for
# --------------------------------------------------------------------------------------------------------------

LIVE_FILE = PKG_DIR / "cli.py"
CLEAN = "import json\nfrom trade_engine.market_data import wrapper_native\n"


@pytest.mark.parametrize(
    "line",
    [
        "import trade_engine.market_data.wrapper\n",
        "from trade_engine.market_data.wrapper import StampingMarketDataWrapper\n",
        "from trade_engine.market_data import wrapper\n",
        "import importlib\nimportlib.import_module('trade_engine.market_data.wrapper')\n",
    ],
)
def test_mutant_import_of_a_retired_module_is_flagged(line):
    assert scan_source(CLEAN, LIVE_FILE, ALL_GUARDED) == set()
    assert scan_source(CLEAN + line, LIVE_FILE, ALL_GUARDED) == {"trade_engine.market_data.wrapper"}


def test_mutant_relative_imports_inside_the_package_are_flagged():
    sibling = PKG_DIR / "market_data" / "chains.py"
    assert scan_source("from .wrapper import X\n", sibling, ALL_GUARDED) == {"trade_engine.market_data.wrapper"}
    assert scan_source("from . import wrapper\n", sibling, ALL_GUARDED) == {"trade_engine.market_data.wrapper"}
    assert scan_source("from ..market_data.wrapper import X\n", PKG_DIR / "eod" / "cli.py", ALL_GUARDED) == {
        "trade_engine.market_data.wrapper"
    }
    assert scan_source("from .wrapper_native import X\n", sibling, ALL_GUARDED) == set()


def test_mutant_both_import_forms_of_a_guarded_module_are_flagged():
    # ``from x.y import z`` and ``from x import y`` both resolve to the guarded module ``x.y``.
    assert scan_source("from trade_engine.market_data.wrapper import StampingMarketDataWrapper\n", LIVE_FILE, ALL_GUARDED) == {
        "trade_engine.market_data.wrapper"
    }
    assert scan_source("from trade_engine.market_data import wrapper\n", LIVE_FILE, ALL_GUARDED) == {
        "trade_engine.market_data.wrapper"
    }
    assert scan_source("from trade_engine.market_data import wrapper_native\n", LIVE_FILE, ALL_GUARDED) == set()


def test_mutant_live_file_with_appended_import_would_fail_the_scan():
    mutated = LIVE_FILE.read_text(encoding="utf-8") + "\nfrom trade_engine.market_data.wrapper import StampingMarketDataWrapper\n"
    assert scan_source(LIVE_FILE.read_text(encoding="utf-8"), LIVE_FILE, ALL_GUARDED) == set()
    assert scan_source(mutated, LIVE_FILE, ALL_GUARDED) == {"trade_engine.market_data.wrapper"}


def test_header_regex_rejects_near_misses():
    ok = "# RETIRED (P6): reference only; live path is trade_engine_rs::market_data_verify. Do not import."
    assert HEADER_RE.match(ok)
    assert not HEADER_RE.match(ok.replace("RETIRED", "retired"))
    assert not HEADER_RE.match(ok.replace(". Do not import.", ""))
    assert not HEADER_RE.match("# RETIRED (P6): reference only; live path is trade_engine_rs::. Do not import.")

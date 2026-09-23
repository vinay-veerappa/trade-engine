"""Invariant checks for trade-engine (Architecture §2, I7).

Scans source code for clock reads outside the injected Clock. Import aliases are
resolved, so `import time as t; t.time()` and `from datetime import datetime as dt;
dt.now()` are caught the same as the plain spellings.
"""

from __future__ import annotations

import ast
from pathlib import Path

# Fully-qualified clock reads.
BANNED_QUALIFIED: frozenset[str] = frozenset(
    {
        "time.time",
        "time.time_ns",
        "time.monotonic",
        "time.monotonic_ns",
        "time.perf_counter",
        "time.perf_counter_ns",
        "time.localtime",
        "time.gmtime",
        "time.ctime",
        "time.sleep",
        "datetime.datetime.now",
        "datetime.datetime.utcnow",
        "datetime.datetime.today",
        "datetime.date.today",
    }
)

# Default allowlist: only WallClock is permitted to read host time and sleep
DEFAULT_I7_ALLOWLIST: frozenset[str] = frozenset({"trade_engine/clock/wall.py"})

# Methods that read the wall clock on any receiver (e.g. pandas Timestamp.now()).
# The Clock protocol exposes now_utc(), which does not collide with these.
BANNED_ANY_RECEIVER: frozenset[str] = frozenset({"now", "utcnow", "today"})


def _import_aliases(tree: ast.AST) -> dict[str, str]:
    """Map local names to the fully-qualified names they were imported as."""
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.asname:
                    aliases[a.asname] = a.name
                else:
                    top = a.name.split(".")[0]
                    aliases[top] = top
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            for a in node.names:
                aliases[a.asname or a.name] = f"{node.module}.{a.name}"
    return aliases


def _qualified_name(func: ast.expr, aliases: dict[str, str]) -> str | None:
    """Resolve a call target like `dt.now` to `datetime.datetime.now`, or None."""
    parts: list[str] = []
    node = func
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    base = aliases.get(node.id)
    if base is None:
        return None
    return ".".join([base, *reversed(parts)])


def check_i7_invariants(src_dir: Path, allowlist: set[str] | frozenset[str] | None = None) -> list[str]:
    """Return clock-read violations in src_dir.

    allowlist holds POSIX paths relative to src_dir (e.g. "trade_engine/clock/wall.py"),
    so only the real Clock implementation can be exempted, not every file of that name.
    """
    violations: list[str] = []
    allow = DEFAULT_I7_ALLOWLIST if allowlist is None else set(allowlist)

    for py_file in sorted(src_dir.rglob("*.py")):
        rel = py_file.relative_to(src_dir).as_posix()
        if rel in allow:
            continue

        code = py_file.read_text(encoding="utf-8")
        try:
            tree = ast.parse(code, filename=str(py_file))
        except SyntaxError as e:
            violations.append(f"{rel}: SyntaxError: {e}")
            continue

        aliases = _import_aliases(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            qualified = _qualified_name(node.func, aliases)
            if qualified in BANNED_QUALIFIED:
                violations.append(f"{rel}:{node.lineno} calls {qualified}()")
            elif isinstance(node.func, ast.Attribute) and node.func.attr in BANNED_ANY_RECEIVER:
                violations.append(f"{rel}:{node.lineno} calls .{node.func.attr}()")

    return violations

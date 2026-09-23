"""Invariant checks for trade-engine (Architecture §2, I7).

Scans source code for forbidden clock and time calls.
"""

from __future__ import annotations

import ast
from pathlib import Path


BANNED_CALLS: dict[str, set[str]] = {
    "datetime": {"now", "utcnow"},
    "date": {"today"},
    "time": {
        "time",
        "monotonic",
        "monotonic_ns",
        "perf_counter",
        "perf_counter_ns",
    },
}


def check_i7_invariants(src_dir: Path, allowlist: set[str] | None = None) -> list[str]:
    """Check that no forbidden clock calls exist in src_dir outside allowlisted files."""
    violations: list[str] = []
    allow = allowlist or set()

    for py_file in src_dir.rglob("*.py"):
        if py_file.name in allow:
            continue

        code = py_file.read_text(encoding="utf-8")
        try:
            tree = ast.parse(code, filename=str(py_file))
        except SyntaxError as e:
            violations.append(f"{py_file}: SyntaxError: {e}")
            continue

        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                # Matches module.func(...) e.g. datetime.now(), time.time(), date.today()
                if isinstance(func, ast.Attribute):
                    method_name = func.attr
                    # Check value
                    module_name = ""
                    if isinstance(func.value, ast.Name):
                        module_name = func.value.id
                    elif isinstance(func.value, ast.Attribute):
                        module_name = func.value.attr

                    if module_name in BANNED_CALLS and method_name in BANNED_CALLS[module_name]:
                        violations.append(
                            f"{py_file.name}:{node.lineno} calls {module_name}.{method_name}()"
                        )
                # Matches direct imported function calls e.g. monotonic(), time()
                elif isinstance(func, ast.Name):
                    for mod, methods in BANNED_CALLS.items():
                        if func.id in methods:
                            violations.append(
                                f"{py_file.name}:{node.lineno} calls direct time function {func.id}()"
                            )

    return violations

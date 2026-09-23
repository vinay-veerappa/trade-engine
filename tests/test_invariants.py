"""AST / Static invariant tests (Architecture §2, I7)."""

import ast
from pathlib import Path


def test_no_uncontrolled_clock_reads_in_src() -> None:
    """Assert no datetime.now() or time.time() calls in src/ outside Clock implementations (I7)."""
    src_dir = Path(__file__).resolve().parent.parent / "src"
    violations: list[str] = []

    for py_file in src_dir.rglob("*.py"):
        # When Clock implementation is created in E2, clock.py will be allowed
        code = py_file.read_text(encoding="utf-8")
        tree = ast.parse(code, filename=str(py_file))

        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                # Check for datetime.now(...)
                if isinstance(node.func, ast.Attribute) and node.func.attr == "now":
                    violations.append(f"{py_file.name}:{node.lineno} calls datetime.now()")
                # Check for time.time(...)
                if isinstance(node.func, ast.Attribute) and node.func.attr == "time":
                    if isinstance(node.func.value, ast.Name) and node.func.value.id == "time":
                        violations.append(f"{py_file.name}:{node.lineno} calls time.time()")

    assert not violations, f"Forbidden uncontrolled clock reads found:\n" + "\n".join(violations)

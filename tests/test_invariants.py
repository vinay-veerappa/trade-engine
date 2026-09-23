"""AST / Static invariant tests (Architecture §2, I7)."""

import ast
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.invariant_checks import check_i7_invariants


def test_no_uncontrolled_clock_reads_in_src() -> None:
    """Assert no datetime.now(), date.today(), time.monotonic(), etc. calls in src/ outside WallClock (I7)."""
    src_dir = REPO_ROOT / "src"
    violations = check_i7_invariants(src_dir, allowlist={"trade_engine/clock/wall.py"})
    assert not violations, "Forbidden uncontrolled clock reads found:\n" + "\n".join(violations)


def test_datetime_now_only_in_wallclock() -> None:
    """Acceptance: no datetime.now anywhere in src/ outside WallClock (a test greps)."""
    src_dir = REPO_ROOT / "src"
    violations: list[str] = []
    wall_file = (src_dir / "trade_engine" / "clock" / "wall.py").resolve()

    for py_file in src_dir.rglob("*.py"):
        if py_file.resolve() == wall_file:
            continue
        text = py_file.read_text(encoding="utf-8")
        if "now(" in text or ".now" in text or "utcnow" in text or "today(" in text:
            # Parse AST to ensure it's not a comment or unrelated attribute
            tree = ast.parse(text, filename=str(py_file))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                    if node.func.attr in {"now", "utcnow", "today"}:
                        rel = py_file.relative_to(src_dir).as_posix()
                        violations.append(f"{rel}:{node.lineno} calls .{node.func.attr}()")

    assert not violations, "Found datetime.now / clock calls outside WallClock:\n" + "\n".join(violations)


def _scan(tmp_path: Path, source: str, allowlist: set[str] | None = None) -> list[str]:
    pkg = tmp_path / "pkg"
    pkg.mkdir(exist_ok=True)
    (pkg / "mod.py").write_text(source, encoding="utf-8")
    return check_i7_invariants(tmp_path, allowlist)



@pytest.mark.parametrize(
    "source",
    [
        "import time\ntime.time()\n",
        "import time as t\nt.monotonic()\n",
        "from time import perf_counter as pc\npc()\n",
        "from datetime import datetime\ndatetime.now()\n",
        "from datetime import datetime as dt\ndt.utcnow()\n",
        "import datetime\ndatetime.datetime.now()\n",
        "from datetime import date\ndate.today()\n",
        "import pandas as pd\npd.Timestamp.now()\n",
    ],
)
def test_i7_checker_fires_on_clock_reads(tmp_path: Path, source: str) -> None:
    assert _scan(tmp_path, source), f"checker missed: {source!r}"


@pytest.mark.parametrize(
    "source",
    [
        "def f(clock):\n    return clock.now_utc()\n",
        "from datetime import datetime, timezone\ndatetime(2026, 1, 1, tzinfo=timezone.utc)\n",
        "import time\ntime.sleep\n",
        "def time():\n    return 1\ntime()\n",
    ],
)
def test_i7_checker_does_not_fire_on_clean_code(tmp_path: Path, source: str) -> None:
    assert _scan(tmp_path, source) == []


def test_i7_allowlist_is_by_path_not_basename(tmp_path: Path) -> None:
    source = "import time\ntime.time()\n"
    assert _scan(tmp_path, source, {"pkg/mod.py"}) == []
    # Same basename elsewhere is not exempt
    assert _scan(tmp_path, source, {"other/mod.py"})

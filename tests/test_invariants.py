"""AST / Static invariant tests (Architecture §2, I7)."""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.invariant_checks import check_i7_invariants


def test_no_uncontrolled_clock_reads_in_src() -> None:
    """Assert no datetime.now(), date.today(), time.monotonic(), etc. calls in src/ (I7)."""
    src_dir = REPO_ROOT / "src"
    violations = check_i7_invariants(src_dir)
    assert not violations, "Forbidden uncontrolled clock reads found:\n" + "\n".join(violations)

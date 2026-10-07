"""P7 timing: the same workloads before (PyDec) and after (rust_decimal Money).

Run from a tree's root with that tree's `src` and `tests` on the path and that tree's
`trade_engine_rs` first on PYTHONPATH:

    python tools/time_p7.py OUT.json LEDGER_COPY.db [BASELINE.json]

Workloads (each sampled several times, median reported; construction is outside the timer):
  fold    cold fold of every account of a real ledger copy, through LedgerReader
  append  2000 CashFlow + fill-shaped appends to a fresh ledger (codec + decimal + store)
  bracket 48 seeded OMS bracket walks (tests/test_p3b2_parity.bracket_walk)

With a BASELINE the ratio new/old is printed per workload; the gate is <= 1.25.
"""
from __future__ import annotations

import json
import statistics
import sys
import tempfile
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

ROOT = Path.cwd()
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "src"))


def fold(copy: Path, samples: int = 9) -> list[float]:
    from trade_engine.ledger.reader import LedgerReader

    out = []
    for _ in range(samples):
        reader = LedgerReader(copy).open()
        try:
            accounts = list(reader.accounts())
            start = time.perf_counter()
            for account in accounts:
                reader.state(account)
            out.append(time.perf_counter() - start)
        finally:
            reader.close()
    return out


def append(samples: int = 5) -> list[float]:
    from trade_engine.ledger import CashFlow, Event, EventKind, Ledger

    at = datetime(2026, 9, 24, 21, 45, tzinfo=UTC)
    out = []
    for _ in range(samples):
        with tempfile.TemporaryDirectory() as d:
            with Ledger(Path(d) / "l.db") as ledger:
                events = [
                    Event(account="A", kind=EventKind.CASH_FLOW, ts_utc=at, command_id=f"c{i}",
                          payload=CashFlow(amount=Decimal("100.25") + i, kind="deposit", as_of=at))
                    for i in range(2000)
                ]
                start = time.perf_counter()
                for event in events:
                    ledger.append(event)
                out.append(time.perf_counter() - start)
    return out


def bracket(samples: int = 5) -> list[float]:
    import test_p3b2_parity as t
    from trade_engine.oms.manager import OrderManager

    class Solo:
        def __init__(self, root):
            self.w = t.World(root, OrderManager, lambda clock: t.FaultBroker())

        def do(self, label, fn):
            try:
                return fn(self.w)
            except Exception:  # noqa: BLE001 - the walk provokes refusals on purpose
                return None

        def both(self, fn):
            fn(self.w)

    out = []
    for _ in range(samples):
        with tempfile.TemporaryDirectory() as d:
            start = time.perf_counter()
            for seed in range(t.SEEDS):
                s = Solo(Path(d) / str(seed))
                t.bracket_walk(s, seed)
                s.w.ledger.close()
            out.append(time.perf_counter() - start)
    return out


def main() -> None:
    import trade_engine_rs

    report = {"extension": str(Path(trade_engine_rs.__file__).resolve()), "tree": str(ROOT)}
    for name, run in (("fold", lambda: fold(Path(sys.argv[2]))), ("append", append), ("bracket", bracket)):
        samples = run()
        report[name] = {"samples": samples, "median": statistics.median(samples)}
    if len(sys.argv) > 3:
        base = json.loads(Path(sys.argv[3]).read_text(encoding="utf-8"))
        for name in ("fold", "append", "bracket"):
            report[name]["baseline_median"] = base[name]["median"]
            report[name]["ratio"] = report[name]["median"] / base[name]["median"]
    print(json.dumps({k: (v if k in ("extension", "tree") else {x: y for x, y in v.items() if x != "samples"}) for k, v in report.items()}, indent=1))
    Path(sys.argv[1]).write_text(json.dumps(report, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()

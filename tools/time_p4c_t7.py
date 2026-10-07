"""Nine paired synthetic books per role; fixture construction is outside timing."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, time
from decimal import Decimal
import json
from pathlib import Path
import statistics
import sys
import tempfile
from time import perf_counter

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from test_p4c_eod import O, P, CAL, NY, stored, outcome
from test_eod_options import ACCOUNT as OA, BuyWrite, NoBars, Rig, S1, S2, S3, chain
from test_eod_runner import ACCOUNT, FakeMarketData, PREV_EOD, SESSION, SettableClock, _seed_bracket
from trade_engine.ledger import Ledger
from trade_engine.sim import SimBroker


def build(root, module, role, index):
    root.mkdir()
    if role == "equity":
        ledger = Ledger(root / "book.db").open()
        clock = SettableClock(PREV_EOD)
        broker = SimBroker(ACCOUNT, clock, Decimal("0"))
        broker.connect()
        _seed_bracket(ledger, broker, clock, command_id="entry", stop_price="95")
        runner = module.EodRunner(ledger, clock, CAL, FakeMarketData({
            60 + index: ("100", "101", "94", "95")
        }), module.EodRunnerConfig("eod", {ACCOUNT: broker}))
        def walk():
            results = [outcome(lambda: runner.run(SESSION)), outcome(lambda: runner.run(SESSION))]
            ledger.fold()
            return results
        return ledger, walk
    rig = Rig(root)
    rig.strategy = BuyWrite()
    for session in (S1, S2, S3):
        base = chain(session)
        rig.snapshots[session] = [
            replace(base, as_of=stamp, quotes=tuple(replace(q, as_of=stamp) for q in base.quotes))
            for hour, minute in ((9, 45), (12, 30), (15, 45))
            for stamp in (datetime.combine(session, time(hour, minute), NY),)
        ]
    # Bootstrap with the close run, then exercise successful named passes and close
    # resume on two further sessions, not merely identical replay-clock refusals.
    def walk():
        results = []
        for session in (S1, S2, S3):
            if session != S1:
                for name, hour, minute in (("morning", 9, 45), ("midday", 12, 30), ("late", 15, 45)):
                    rig.clock._current_time = CAL.session_open(session)
                    runner = module.EodRunner(rig.ledger, rig.clock, CAL, NoBars(),
                        module.EodRunnerConfig(**vars(rig.config())))
                    through = datetime.combine(session, time(hour, minute), NY)
                    results.append(outcome(lambda: runner.run_pass(session, through, name)))
            rig.clock._current_time = CAL.session_open(session)
            runner = module.EodRunner(rig.ledger, rig.clock, CAL, NoBars(),
                module.EodRunnerConfig(**vars(rig.config())))
            results.append(outcome(lambda: runner.run(session)))
        rig.ledger.fold()
        return results
    return rig.ledger, walk


def main():
    assert Path(sys.executable).resolve() == (ROOT / ".venv" / "Scripts" / "python.exe").resolve()
    report = {}
    with tempfile.TemporaryDirectory(prefix="p4c-t7-timing-") as folder:
        for role in ("equity", "options"):
            samples = []
            for index in range(9):
                books = [build(Path(folder) / f"{role}-{index}-{label}", module, role, index)
                         for label, module in (("oracle", O), ("native", P))]
                elapsed, results = {}, {}
                try:
                    for which in ((0, 1) if index % 2 == 0 else (1, 0)):
                        start = perf_counter()
                        results[which] = books[which][1]()
                        elapsed[which] = perf_counter() - start
                    assert results[0] == results[1]
                    assert all(result[0] == "ok" for result in results[0]), results[0]
                    assert stored(books[0][0]) == stored(books[1][0])
                    samples.append((elapsed[0], elapsed[1]))
                finally:
                    for ledger, _ in books:
                        ledger.close()
            old, new = zip(*samples)
            ratio = statistics.median(new) / statistics.median(old)
            report[role] = {
                "provenance": "synthetic", "pairs": len(samples),
                "oracle_median_seconds": statistics.median(old),
                "native_median_seconds": statistics.median(new),
                "oracle_tail_seconds": max(old), "native_tail_seconds": max(new),
                "median_ratio": ratio, "samples": samples,
            }
    print(json.dumps(report, indent=2), flush=True)
    assert all(role["median_ratio"] <= 1.25 for role in report.values()), report


if __name__ == "__main__":
    main()

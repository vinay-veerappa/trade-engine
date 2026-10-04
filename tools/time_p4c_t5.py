"""Nine paired populated synthetic books; excludes fixture/build time, not effects."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import statistics
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
from frozen_p4c.t5_store import Ledger as FrozenLedger
from p4c_store_fixture import populated_book
from test_p4c_outbox import AT, Clock, event, stored
from trade_engine.ledger.store import Ledger


def measure(action):
    start = time.perf_counter_ns()
    value = action()
    return (time.perf_counter_ns() - start) / 1e6, value


def main():
    assert Path(sys.executable).resolve() == (ROOT / ".venv" / "Scripts" / "python.exe").resolve()
    root = ROOT / ".ci-local" / "t5-timing" / str(uuid.uuid4())
    root.mkdir(parents=True)
    seed = populated_book()
    samples = {key: {"frozen": [], "native": []} for key in ("enqueue_meta", "pending", "drain")}
    try:
        for sample in range(9):
            classes = [("frozen", FrozenLedger), ("native", Ledger)]
            if sample % 2:
                classes.reverse()
            outputs = {}
            for name, cls in classes:
                with cls(root / f"{sample}-{name}.db") as ledger:
                    ledger.extend(seed)
                    for account in ledger.accounts():
                        ledger.state(account)
                    events = [ledger.append(event(f"hot-{i}")) for i in range(120)]
                    def enqueue():
                        for i, ev in enumerate(events):
                            ledger.enqueue_outbox(ev.seq, " journal ", {"i": i, "text": "\u03bb", "scale": "1.2300"})
                            ledger.set_meta("cursor", str(i))
                            assert ledger.get_meta("cursor") == str(i)
                    elapsed, _ = measure(enqueue)
                    samples["enqueue_meta"][name].append(elapsed)
                    elapsed, pending = measure(lambda: ledger.pending_outbox("journal"))
                    assert len(pending) == 120
                    samples["pending"][name].append(elapsed)
                    trace = []
                    def publisher(item):
                        trace.append(item.id)
                        ledger.state("A")  # Nested callback on the same native handle.
                        return True
                    elapsed, result = measure(lambda: ledger.drain_outbox("journal", publisher, Clock([])))
                    assert result.drained_count == 120 and result.remaining_count == 0
                    samples["drain"][name].append(elapsed)
                    outputs[name] = stored(ledger), trace
            assert outputs["frozen"] == outputs["native"]
    finally:
        shutil.rmtree(root)
    report = {"samples": 9, "seed_events": len(seed), "hot_items": 120, "timings": {}}
    for key, values in samples.items():
        old, new = statistics.median(values["frozen"]), statistics.median(values["native"])
        report["timings"][key] = {**values, "frozen_median_ms": old, "native_median_ms": new,
                                 "ratio": new / old, "frozen_max_ms": max(values["frozen"]),
                                 "native_max_ms": max(values["native"])}
    print(json.dumps(report, indent=2))
    assert all(row["ratio"] <= 1.25 for row in report["timings"].values()), report


if __name__ == "__main__":
    main()

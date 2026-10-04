"""Nine paired synthetic provider/clock/store walks; no live adapter is imported."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import shutil
import statistics
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))

from frozen_p4c.t6_replay import ReplayClock as FrozenReplay
from trade_engine.clock import ReplayClock
from trade_engine.ledger.events import CashFlow, Event, EventKind
from trade_engine.ledger.store import Ledger

AT = datetime(2026, 10, 2, 13, 30, tzinfo=timezone.utc)


def walk(path, clock_type):
    clock = clock_type(AT)
    with Ledger(path) as ledger:
        for account in ("A", "B", "C"):
            ledger.append(Event(account=account, kind=EventKind.CASH_FLOW,
                                payload=CashFlow(Decimal("100000.00"), "deposit", AT),
                                ts_utc=AT, command_id="initial-" + account))
        def provider(account):
            state = ledger.state(account)
            quotes = tuple(Decimal(100 + index) + state.cash / Decimal("10000000")
                           for index in range(120))
            return clock.now_utc(), quotes
        start = time.perf_counter_ns()
        for index in range(360):
            account = ("A", "B", "C")[index % 3]
            clock.advance_to(AT + timedelta(seconds=index))
            observed, quotes = provider(account)
            assert observed == clock.now_utc() and len(quotes) == 120
            clock.sleep(0.25)
            clock.advance_by(timedelta(microseconds=1))
            at = clock.now_utc()
            ledger.append(Event(account=account, kind=EventKind.CASH_FLOW,
                                payload=CashFlow(Decimal("0.0100"), "deposit", at),
                                ts_utc=at, command_id=f"tick-{index}"))
            assert ledger.state(account).cash > 0
        elapsed = (time.perf_counter_ns() - start) / 1e6
        assert ledger.count() == 363
        return elapsed


def main():
    assert Path(sys.executable).resolve() == (ROOT / ".venv" / "Scripts" / "python.exe").resolve()
    root = ROOT / ".ci-local" / ("t6-timing-" + uuid.uuid4().hex)
    root.mkdir(parents=True)
    samples = {"frozen": [], "native": []}
    try:
        for pair in range(9):
            types = [("frozen", FrozenReplay), ("native", ReplayClock)]
            if pair % 2:
                types.reverse()
            for name, clock_type in types:
                samples[name].append(walk(root / f"{pair}-{name}.db", clock_type))
        medians = {key: statistics.median(values) for key, values in samples.items()}
        ratio = medians["native"] / medians["frozen"]
        print(json.dumps({"pairs": 9, "ticks_per_book": 360, "accounts": 3,
                          "quotes_per_observation": 120, "median_ms": medians,
                          "max_ms": {key: max(values) for key, values in samples.items()},
                          "ratio": ratio, "samples_ms": samples}, sort_keys=True), flush=True)
        assert ratio <= 1.25, f"T6 hot-path slowdown: {ratio:.6f}x > 1.25x"
    finally:
        shutil.rmtree(root)


if __name__ == "__main__":
    main()

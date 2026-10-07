"""Synthetic lock/open/close timing; this startup lifecycle is not a tick hot path."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
import statistics
import sys
import time

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
from frozen_p4c.lock import SingleInstanceLock as OldLock
from trade_engine.ledger.lock import SingleInstanceLock as NewLock
from trade_engine.ledger import CashFlow, Event, EventKind
from trade_engine.ledger.store import Ledger


def old_open_close(path):
    ledger = Ledger(path)
    ledger._lock = OldLock(path)
    with ledger:
        pass


def new_open_close(path):
    with Ledger(path):
        pass


def main():
    assert Path(sys.executable).resolve() == (ROOT / ".venv" / "Scripts" / "python.exe").resolve()
    import trade_engine, trade_engine_rs
    assert Path(trade_engine.__file__).resolve().is_relative_to(ROOT / "src")
    assert Path(trade_engine_rs.__file__).resolve().is_relative_to(ROOT / ".venv")
    parent = ROOT / ".ci-local" / "t1-timing"
    parent.mkdir(parents=True, exist_ok=True)
    result = {}
    for name, callbacks in (("lock", (
        lambda path: OldLock(path).__enter__().release(),
        lambda path: NewLock(path).__enter__().release())),
        ("lock_open_close", (old_open_close, new_open_close))):
        samples = [[], []]
        paths = [[parent / f"{name}-{side}-{seed}.db" for seed in range(9)] for side in range(2)]
        try:
            for seed in range(9):
                for side in range(2):
                    if name == "lock_open_close":
                        at = datetime(2026, 9, 23, 21, tzinfo=timezone.utc)
                        with Ledger(paths[side][seed]) as ledger:
                            ledger.extend([
                                Event(account=f"SYNTH-{account}", kind=EventKind.CASH_FLOW,
                                      payload=CashFlow(amount=Decimal(seed + index + 1),
                                                       kind="deposit", as_of=at),
                                      ts_utc=at, command_id=f"{account}-{index}")
                                for account in range(3) for index in range(100)
                            ])
                            assert ledger.conn.execute("SELECT count(*) FROM events").fetchone()[0] == 300
                    callbacks[side](paths[side][seed])
                for side in ((0, 1) if seed % 2 else (1, 0)):
                    start = time.perf_counter()
                    for _ in range(50):
                        callbacks[side](paths[side][seed])
                    samples[side].append((time.perf_counter() - start) * 1000 / 50)
            old, new = (statistics.median(s) for s in samples)
            result[name] = dict(old_median_ms=old, new_median_ms=new, ratio=new / old,
                                old_tail_ms=max(samples[0]), new_tail_ms=max(samples[1]),
                                books=9, iterations=50,
                                events_per_book=300 if name == "lock_open_close" else 0)
        finally:
            for side in paths:
                for path in side:
                    for suffix in ("", ".lock", "-wal", "-shm"):
                        Path(str(path) + suffix).unlink(missing_ok=True)
    parent.rmdir()
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()

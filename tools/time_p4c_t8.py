"""Nine independent paired synthetic full sessions, no fixture/build time."""
from __future__ import annotations

import json
from pathlib import Path
import statistics
import sys
import tempfile
from time import perf_counter

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from test_p4c_intraday import O, P, DATES, complete_quotes
from test_intraday_service import Rig, Scripted, CAL, at_et, spread
from test_p4c_eod import stored
from test_p4a_parity import norm


def build(root, module, index, role):
    root.mkdir()
    session = DATES[index % len(DATES)]
    rig = Rig(root, session=session)
    complete_quotes(rig)
    rig.snapshots = [s for s in rig.snapshots if not (at_et(session, 10, 3) <= s.as_of < at_et(session, 10, 6))]
    rig.strategy.actions[at_et(session, 9, 45)] = [spread(f"entry-{index}", session=session, target="0.20")]
    rig.strategy.at_or_after[at_et(session, 10, 7)] = [spread(f"recovery-{index}", session=session)]
    config = module.IntradayConfig(**vars(rig.config()))
    def service():
        return module.IntradayService(rig.ledger, rig.clock, CAL, config, heartbeat_path=rig.heartbeat)
    def walk():
        results = []
        if role == "restart":
            results.append(norm(service().run(session, stop_at=at_et(session, 10, 2))))
            # Simulate only a known-dead process, never a running service.
            rig.heartbeat.unlink()
            rig.ledger.close()
            from trade_engine.ledger import Ledger
            from trade_engine.sim import SnapshotVenue
            rig.ledger = Ledger(rig.path).open()
            rig.venue = SnapshotVenue(config.account_id, rig.clock)
            old = rig.strategy
            rig.strategy = Scripted(rig.ledger)
            rig.strategy.actions, rig.strategy.at_or_after = old.actions, old.at_or_after
            resumed = module.IntradayConfig(**(vars(config) | {"broker": rig.venue, "strategy": rig.strategy}))
            runner = module.IntradayService(rig.ledger, rig.clock, CAL, resumed, heartbeat_path=rig.heartbeat)
        else:
            runner = service()
        results.append(norm(runner.run(session)))
        rig.ledger.fold()
        return results
    return rig, walk


def main():
    assert Path(sys.executable).resolve() == (ROOT / ".venv" / "Scripts" / "python.exe").resolve()
    report = {}
    with tempfile.TemporaryDirectory(prefix="p4c-t8-timing-") as folder:
        for role in ("session", "restart"):
            samples = []
            for index in range(9):
                books = [build(Path(folder) / f"{role}-{index}-{name}", module, index, role)
                         for name, module in (("oracle", O), ("native", P))]
                elapsed, results = {}, {}
                try:
                    for which in ((0, 1) if index % 2 == 0 else (1, 0)):
                        start = perf_counter()
                        results[which] = books[which][1]()
                        elapsed[which] = perf_counter() - start
                    assert results[0] == results[1]
                    assert stored(books[0][0].ledger) == stored(books[1][0].ledger)
                    assert books[0][0].heartbeat.read_bytes() == books[1][0].heartbeat.read_bytes()
                    assert dict(results[0][-1])["orders_submitted"] == (1 if role == "restart" else 2)
                    samples.append((elapsed[0], elapsed[1]))
                finally:
                    for rig, _ in books:
                        rig.ledger.close()
            old, new = zip(*samples)
            report[role] = {
                "provenance": "synthetic", "pairs": len(samples),
                "oracle_median_seconds": statistics.median(old), "native_median_seconds": statistics.median(new),
                "oracle_tail_seconds": max(old), "native_tail_seconds": max(new),
                "median_ratio": statistics.median(new) / statistics.median(old), "samples": samples,
            }
    print(json.dumps(report, indent=2), flush=True)
    assert all(role["median_ratio"] <= 1.25 for role in report.values()), report


if __name__ == "__main__":
    main()

"""Real ledger lifecycle timing; construction and seeding are outside the timer."""
from __future__ import annotations

import json
from pathlib import Path
import statistics
import sys
import time
from datetime import timedelta

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
from test_option_lifecycle import Book, CAL, EXPIRY, AFTER, CLOSE, option, pm
from trade_engine.domain.instruments import Side
from trade_engine.lifecycle import LifecyclePass, FixedSettlements, FixedDividends
from trade_engine.clock.replay import ReplayClock


def main():
    import trade_engine
    import trade_engine_rs
    assert Path(sys.executable).resolve() == (ROOT / ".venv" / "Scripts" / "python.exe").resolve()
    assert Path(trade_engine.__file__).resolve().is_relative_to(ROOT / "src")
    assert Path(trade_engine_rs.__file__).resolve().is_relative_to(ROOT / ".venv")
    root = ROOT / ".ci-local" / "p4b-timing"
    root.mkdir(parents=True, exist_ok=True)
    samples = []
    counts = []
    for i in range(9):
        path = root / str(i)
        path.mkdir(exist_ok=True)
        db = path / "ledger.db"
        if db.exists():
            db.unlink()
        book = Book(path)
        try:
            for account in ("alpha", "beta", "gamma"):
                for j in range(32):
                    root_symbol = ("AAPL", "SPXW", "SPX")[j % 3]
                    contract = option("C" if j % 2 else "P", str(220 + j),
                                      root=root_symbol)
                    book.trade(contract, Side.SELL if j % 3 else Side.BUY,
                               str(1 + j % 4), "2.50", account)
                for j in range(8):
                    book.trade(option("C", str(260 + j), expiry=CAL.next_session(EXPIRY)),
                               Side.SELL, "1", "1.25", account)
            from trade_engine.lifecycle import SettlementPrice
            from trade_engine.domain.option_roots import SettleTime
            from decimal import Decimal
            prices = [pm("240.00"), pm("245.00", underlying="SPX"),
                      SettlementPrice("SPX", EXPIRY, SettleTime.AM, Decimal("242.00"),
                                      "official open", CAL.session_open(EXPIRY) + timedelta(minutes=1))]
            lifecycle = LifecyclePass(book.ledger, ReplayClock(AFTER), CAL,
                                      FixedSettlements(prices),
                                      dividends=FixedDividends({"AAPL": []}))
            start = time.perf_counter()
            result = lifecycle.run(EXPIRY)
            samples.append(time.perf_counter() - start)
            counts.append(len(result.events))
        finally:
            book.ledger.close()
    assert counts == [96] * 9, counts
    report = {"positions": 120, "accounts": 3, "cash_only_fixture_accounts": 1, "events": 96,
              "samples_seconds": samples, "median_seconds": statistics.median(samples)}
    if len(sys.argv) > 2:
        baseline = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
        report["baseline_median_seconds"] = baseline["median_seconds"]
        report["ratio"] = report["median_seconds"] / baseline["median_seconds"]
        assert report["ratio"] <= 1.25, report
    print(json.dumps(report))
    if len(sys.argv) > 1:
        Path(sys.argv[1]).write_text(json.dumps(report, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()

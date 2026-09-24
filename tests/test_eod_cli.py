"""CLI tests for the `trade_engine eod` subcommand (E7)."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from trade_engine.cli import main
from trade_engine.domain.instruments import Equity
from trade_engine.eod import DiscoveredPlugins
from trade_engine.interfaces.market_data import Bar
from trade_engine.ledger import Ledger

UTC = timezone.utc
SESSION = date(2026, 9, 23)
ACCOUNT = "cli-account"
INSTRUMENT = Equity("AAPL")
SESSION_OPEN = datetime(2026, 9, 23, 13, 30, tzinfo=UTC)


def _bar(timestamp: datetime) -> Bar:
    return Bar(
        instrument=INSTRUMENT,
        timestamp=timestamp,
        open=Decimal("100"),
        high=Decimal("101"),
        low=Decimal("99"),
        close=Decimal("100"),
        volume=Decimal("10000"),
        as_of=timestamp,
    )


class CliMarketData:
    name = "cli-fake"

    def bars(self, instrument, tf, start, end, max_age_seconds):
        count = int((end - start).total_seconds() // 60)
        return [_bar(start + timedelta(minutes=i)) for i in range(count)]


def test_eod_requires_a_market_data_plugin(monkeypatch, tmp_path: Path) -> None:
    from trade_engine.eod import DiscoveredPlugins

    monkeypatch.setattr(
        "trade_engine.eod.cli.discover_plugins",
        lambda: DiscoveredPlugins(strategies=(), signal_adapters=(), market_data=()),
    )
    code = main(["eod", "--session", SESSION.isoformat(), "--ledger", str(tmp_path / "l.db")])
    assert code == 2


def test_eod_cli_runs_a_session_and_reports_counts(monkeypatch, tmp_path: Path) -> None:
    plugin = CliMarketData()
    monkeypatch.setattr(
        "trade_engine.eod.cli.discover_plugins",
        lambda: DiscoveredPlugins(strategies=(), signal_adapters=(), market_data=(plugin,)),
    )
    ledger_path = tmp_path / "cli.db"
    with Ledger(ledger_path) as ledger:
        ledger.set_meta("probed", "1")  # creates the file so --accounts has an account

    code = main(
        [
            "eod",
            "--session",
            SESSION.isoformat(),
            "--ledger",
            str(ledger_path),
            "--accounts",
            ACCOUNT,
        ]
    )
    assert code == 0
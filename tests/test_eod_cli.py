"""CLI tests for the `trade_engine eod` subcommand (E7)."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

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

@pytest.mark.parametrize(
    ("extra", "message"),
    [
        (["--session", "2026-13-01"], "--session: expected YYYY-MM-DD"),
        (["--session", SESSION.isoformat(), "--slippage-bps", "abc"], "expected a number"),
        (["--session", SESSION.isoformat(), "--slippage-bps", "-1"], "non-negative"),
        (["--session", SESSION.isoformat(), "--slippage-bps", "NaN"], "must be finite"),
    ],
)
def test_eod_rejects_malformed_arguments(extra, message, tmp_path: Path, capsys) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["eod", "--ledger", str(tmp_path / "l.db"), *extra])
    assert exit_info.value.code == 2
    assert message in capsys.readouterr().err


def test_eod_refuses_when_a_plugin_fails_to_load(monkeypatch, tmp_path: Path, capsys) -> None:
    from trade_engine.eod import PluginDiscoveryError

    def broken():
        raise PluginDiscoveryError("Plugin 'bad' in group 'trade_engine.marketdata' failed to load")

    monkeypatch.setattr("trade_engine.eod.cli.discover_plugins", broken)
    code = main(["eod", "--session", SESSION.isoformat(), "--ledger", str(tmp_path / "l.db")])
    assert code == 2
    assert "refused: Plugin 'bad'" in capsys.readouterr().err

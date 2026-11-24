"""EOD CLI golden transcript: flags, refusals, stdout counters and exit codes.

The golden at ``tests/golden/eod_cli/transcript.golden.txt`` is a SNAPSHOT of the
production CLI's observable output, recorded from the current implementation
(``trade_engine.cli.main``). It is not a second implementation: nothing in this
file re-derives the expected output, it only replays the commands and compares the
bytes that production prints.

An intended change to the CLI (a new flag, a changed refusal sentence, a new counter)
updates the golden in the SAME commit. Regenerate it with::

    TE_RECORD_EOD_CLI_GOLDEN=1 pytest -q tests/test_eod_cli_golden.py

then review the diff of ``transcript.golden.txt`` before committing it.
"""

from __future__ import annotations

import os

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from trade_engine.cli import main
from trade_engine.clock.replay import ReplayClock
from trade_engine.domain.instruments import Equity, Side
from trade_engine.domain.orders import Order, OrderState, OrderType
from trade_engine.domain.signals import OrderIntent
from trade_engine.eod import DiscoveredPlugins
from trade_engine.interfaces.market_data import Bar
from trade_engine.ledger import Ledger
from trade_engine.oms.manager import OrderManager
from trade_engine.sim import SimBroker

GOLDEN = Path(__file__).resolve().parent / "golden" / "eod_cli" / "transcript.golden.txt"
RECORD_ENV = "TE_RECORD_EOD_CLI_GOLDEN"
SESSION = "2026-09-23"
NEXT_SESSION = "2026-09-25"
LEDGER = "ledger.db"
DISCOVER = "trade_engine.eod.cli.discover_plugins"
ACCOUNT = "cli-account"
AAPL = Equity("AAPL")
FOLLOWING_SESSION = "2026-09-24"
SEED_AT = datetime(2026, 9, 22, 21, 45, tzinfo=timezone.utc)  # 17:45 ET, the prior session's EOD clock
NEW_YORK = ZoneInfo("America/New_York")
FLAT_BAR = ("100", "101", "99", "100")


class _Plugin:
    """Serves one fixed bar per request so the runner records the session."""

    def __init__(self, name: str) -> None:
        self.name = name

    def bars(self, instrument, tf, start, end, max_age_seconds):
        return [
            Bar(
                instrument=instrument,
                timestamp=start,
                open=Decimal("100"),
                high=Decimal("101"),
                low=Decimal("99"),
                close=Decimal("100"),
                volume=Decimal("10000"),
                as_of=start,
            )
        ]


def _discover(*plugins):
    return lambda: DiscoveredPlugins(
        strategies=(), signal_adapters=(), market_data=tuple(plugins)
    )


class _SessionBars:
    """One-minute bars per session: flat unless a (symbol, session, minute) is scripted.

    Minute 0 is the 09:30 ET bar; minute 389 is the 15:59 ET bar whose close the
    session's mark reads. Scripted values are (open, high, low, close) strings.
    """

    def __init__(self, name: str, scripts: dict[tuple[str, str, int], tuple[str, str, str, str]]) -> None:
        self.name = name
        self._scripts = scripts

    def bars(self, instrument, tf, start, end, max_age_seconds):
        session = start.astimezone(NEW_YORK).date().isoformat()
        count = int((end - start).total_seconds() // 60)
        bars = []
        for minute in range(count):
            stamp = start + timedelta(minutes=minute)
            open_, high, low, close = self._scripts.get((instrument.symbol, session, minute), FLAT_BAR)
            bars.append(
                Bar(
                    instrument=instrument,
                    timestamp=stamp,
                    open=Decimal(open_),
                    high=Decimal(high),
                    low=Decimal(low),
                    close=Decimal(close),
                    volume=Decimal("10000"),
                    as_of=stamp,
                )
            )
        return bars


def _seed_market_buy(_monkeypatch) -> None:
    """Before the run: a working DAY MARKET buy of 2 AAPL, submitted on the prior EOD clock."""
    clock = ReplayClock(SEED_AT)
    with Ledger(LEDGER) as ledger:
        broker = SimBroker(ACCOUNT, clock, Decimal("0"))
        broker.connect()
        OrderManager(broker, clock, ledger).submit(
            Order(
                order_id="cli-buy",
                account_id=ACCOUNT,
                instrument=AAPL,
                order_type=OrderType.MARKET,
                side=Side.BUY,
                quantity=Decimal("2"),
                command_id="cli-buy:entry",
                created_at=SEED_AT,
            )
        )


def _seed_entry_bracket(_monkeypatch) -> None:
    """Before the run: a DAY limit entry at 100 for 2 AAPL, stop 95 and target 110 as children."""
    clock = ReplayClock(SEED_AT)
    intent = OrderIntent(
        intent_id="cli-bracket",
        account_id=ACCOUNT,
        instrument=AAPL,
        side=Side.BUY,
        quantity_rule="fixed_2",
        entry_price=Decimal("100"),
        stop_loss=Decimal("95"),
        profit_targets=(Decimal("110"),),
        reason="golden bracket seed",
        command_id="cli-bracket",
    )
    with Ledger(LEDGER) as ledger:
        broker = SimBroker(ACCOUNT, clock, Decimal("0"))
        broker.connect()
        manager = OrderManager(broker, clock, ledger)
        bracket = manager.create_bracket(intent, Decimal("2"))
        manager.submit(bracket.entry)


def _assert_held(mark: str):
    """A silent step: the ledger holds 2 AAPL bought at 100 and marked at ``mark``."""

    def check(_monkeypatch) -> None:
        with Ledger(LEDGER) as ledger:
            state = ledger.state(ACCOUNT)
        position = state.positions[AAPL]
        assert (position.quantity, position.avg_cost) == (Decimal("2"), Decimal("100"))
        assert state.marks[AAPL] == Decimal(mark)
        assert [(f.price, f.quantity) for f in state.fills] == [(Decimal("100"), Decimal("2"))]

    return check


def _assert_stopped_out(_monkeypatch) -> None:
    """A silent step: entry 100 and stop 95 filled, the 110 target was cancelled (OCO), flat, -10 realized."""
    with Ledger(LEDGER) as ledger:
        state = ledger.state(ACCOUNT)
    assert [(f.side, f.price) for f in state.fills] == [(Side.BUY, Decimal("100")), (Side.SELL, Decimal("95"))]
    by_price = {(o.limit_price, o.stop_price): o.state for o in state.orders.values()}
    assert by_price == {
        (Decimal("100"), None): OrderState.FILLED,
        (None, Decimal("95")): OrderState.FILLED,
        (Decimal("110"), None): OrderState.CANCELLED,
    }
    position = state.positions.get(AAPL)
    assert position is None or position.quantity == 0
    assert state.realized_pnl == Decimal("-10")


def _eod(*extra: str) -> list[str]:
    return ["eod", "--ledger", LEDGER, *extra]


def _run(argv: list[str], capsys) -> str:
    """Run one production invocation and render its exact observable output."""
    try:
        code = main(argv)
    except SystemExit as exit_request:  # argparse exits with 2 on a usage error
        code = exit_request.code
    captured = capsys.readouterr()
    return (
        f"$ trade_engine {' '.join(argv)}\n"
        f"exit: {code}\n"
        f"stdout:\n{captured.out}"
        f"stderr:\n{captured.err}"
    )


def _scenarios():
    """(name, plugins, steps). A step is an argv list or a callable taking (monkeypatch)."""
    one = _Plugin("cli-fake")
    other = _Plugin("other-fake")
    recording = _Plugin("cli-fake")
    admitted_only = lambda mp: mp.setattr(  # noqa: E731
        "trade_engine.eod.cli.rs_call", lambda *args, **kwargs: {"state": "admitted"}
    )
    return [
        (
            "usage-errors",
            [one],
            [
                ["eod", "--session", SESSION],
                ["eod", "--session", "2026-13-01", "--ledger", LEDGER],
                ["eod", "--session", SESSION, "--ledger", LEDGER, "--slippage-bps", "-1"],
                ["eod", "--session", SESSION, "--ledger", LEDGER, "--slippage-bps", "abc"],
            ],
        ),
        (
            "no-market-data",
            [],
            [_eod("--session", SESSION, "--accounts", "cli-account")],
        ),
        (
            "market-data-selection",
            [one, other],
            [
                _eod("--session", SESSION, "--accounts", "cli-account"),
                _eod("--session", SESSION, "--accounts", "cli-account", "--market-data", "nope"),
                _eod(
                    "--session", SESSION, "--accounts", "cli-account",
                    "--market-data", "other-fake",
                ),
            ],
        ),
        (
            "no-accounts-anywhere",
            [one],
            [_eod("--session", SESSION)],
        ),
        (
            "run-and-rerun",
            [recording],
            [
                _eod("--session", SESSION, "--accounts", "cli-account", "--slippage-bps", "1.5"),
                _eod("--session", SESSION, "--accounts", "cli-account", "--slippage-bps", "1.5"),
            ],
        ),
        (
            "skipped-predecessor-refusal",
            [recording],
            [
                _eod("--session", SESSION, "--accounts", "cli-account"),
                _eod("--session", NEXT_SESSION, "--accounts", "cli-account"),
            ],
        ),
        (
            "accounts-from-ledger",
            [recording],
            [
                _eod("--session", SESSION, "--accounts", "cli-account"),
                _eod("--session", "2026-09-24"),
            ],
        ),
        (
            "admitted-not-terminal",
            [recording],
            [admitted_only, _eod("--session", SESSION, "--accounts", "cli-account")],
        ),
        (
            "void",
            [one],
            [
                # The account must exist in the ledger before a void can name it.
                _eod("--session", SESSION, "--accounts", "cli-account"),
                _eod("--session", SESSION, "--void", "--accounts", "cli-account"),
                _eod(
                    "--session", NEXT_SESSION, "--void", "--accounts", "cli-account",
                    "--reason", "feed outage", "--owner", "ops",
                ),
                _eod(
                    "--session", NEXT_SESSION, "--void", "--accounts", "cli-account",
                    "--reason", "feed outage", "--owner", "ops",
                ),
                _eod(
                    "--session", SESSION, "--void", "--accounts", "cli-account",
                    "--reason", "feed outage", "--owner", "ops",
                ),
                _eod("--session", NEXT_SESSION, "--void", "--reason", "x", "--owner", "ops"),
            ],
        ),
        (
            "write-off",
            [one],
            [
                _eod("--session", SESSION, "--accounts", "cli-account"),
                _eod("--session", SESSION, "--write-off", "--accounts", "cli-account"),
                _eod(
                    "--session", SESSION, "--write-off", "--accounts", "a,b",
                    "--order", "ord-1", "--reason", "stuck", "--owner", "ops",
                ),
                _eod(
                    "--session", SESSION, "--write-off", "--accounts", "cli-account",
                    "--order", "ord-1", "--reason", "stuck", "--owner", "ops",
                ),
            ],
        ),
        (
            # A held position: the MARKET buy fills at the 09:30 open, the close's mark reads 105.
            "fills-and-marks",
            [_SessionBars("cli-fake", {("AAPL", SESSION, 389): ("100", "105", "99", "105")})],
            [_seed_market_buy, _eod("--session", SESSION, "--accounts", ACCOUNT), _assert_held("105")],
        ),
        (
            # Entry fills at 100 on the first bar; the 09:31 bar opens 96 and trades to 94,
            # so the stop at 95 fills at min(open, stop) = 95 and the target at 110 never trades.
            "bracket",
            [_SessionBars("cli-fake", {("AAPL", SESSION, 1): ("96", "96", "94", "95")})],
            [_seed_entry_bracket, _eod("--session", SESSION, "--accounts", ACCOUNT), _assert_stopped_out],
        ),
        (
            # The position carried out of SESSION is re-marked at the next session's close (98, not 105).
            "next-session-mark",
            [
                _SessionBars(
                    "cli-fake",
                    {
                        ("AAPL", SESSION, 389): ("100", "105", "99", "105"),
                        ("AAPL", FOLLOWING_SESSION, 389): ("98", "98", "97", "98"),
                    },
                )
            ],
            [
                _seed_market_buy,
                _eod("--session", SESSION, "--accounts", ACCOUNT),
                _assert_held("105"),
                _eod("--session", FOLLOWING_SESSION, "--accounts", ACCOUNT),
                _assert_held("98"),
            ],
        ),
    ]


def _transcript(root: Path, monkeypatch, capsys) -> str:
    monkeypatch.setenv("COLUMNS", "80")
    blocks: list[str] = []
    for name, plugins, steps in _scenarios():
        scenario_dir = root / name
        scenario_dir.mkdir()
        with Ledger(scenario_dir / LEDGER) as ledger:
            ledger.set_meta("probed", "1")
        with pytest.MonkeyPatch.context() as scoped:
            scoped.chdir(scenario_dir)
            scoped.setattr(DISCOVER, _discover(*plugins))
            blocks.append(f"### scenario: {name}\n")
            for step in steps:
                if callable(step):
                    step(scoped)
                    continue
                blocks.append(_run(step, capsys))
    return "".join(blocks).replace(str(root), "<TMP>")


def test_eod_cli_transcript_matches_golden(tmp_path, monkeypatch, capsys):
    transcript = _transcript(tmp_path, monkeypatch, capsys)
    if os.environ.get(RECORD_ENV) == "1":
        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN.write_bytes(transcript.encode("utf-8"))
    assert GOLDEN.exists(), f"missing golden {GOLDEN}; record it with {RECORD_ENV}=1"
    expected = GOLDEN.read_bytes().decode("utf-8")
    assert transcript == expected

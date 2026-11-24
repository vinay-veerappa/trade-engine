"""W6-F4a: an options account sees only the snapshots its strategy and holdings want.

A strategy may narrow which chain snapshots its ``manage_options`` runs on by exposing
``option_universe(session)``; the account's held option and share underlyings always keep
their snapshots. An account whose strategy has no hook, or whose hook returns ``None``,
sees every snapshot, as before.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

from trade_engine.calendar import get_calendar
from trade_engine.clock.replay import ReplayClock
from trade_engine.domain.instruments import Equity, OptionContract, OptionRight, Side
from trade_engine.domain.option_orders import OptionIntent
from trade_engine.domain.option_roots import SettleTime
from trade_engine.domain.orders import OrderType
from trade_engine.domain.risk import RiskRuleResult, RiskVerdict
from trade_engine.eod import EodRunner, EodRunnerConfig
from trade_engine.interfaces.market_data import OptionQuote
from trade_engine.ledger import CashFlow, Event, EventKind, Ledger
from trade_engine.lifecycle import FixedDividends, FixedSettlements, LifecyclePass, SettlementPrice
from trade_engine.market_data.chains import ChainSnapshot
from trade_engine.sim import SnapshotVenue

D = Decimal
CAL = get_calendar()
ACCOUNT = "OPT_U"
S1 = date(2026, 9, 25)
S2 = date(2026, 9, 28)
EXPIRY = date(2026, 10, 2)

QQQ = Equity("QQQ")
QPUT = OptionContract("QQQ", EXPIRY, D("500"), OptionRight.PUT)
QPUT_QUOTES = {QPUT: ("5.10", "5.50")}


def snap_time(session: date) -> datetime:
    return CAL.session_close(session) - timedelta(minutes=15)


def snapshot(session: date, underlying: str, price: str, quotes: dict | None = None) -> ChainSnapshot:
    at = snap_time(session)
    rows = tuple(
        OptionQuote(c, D(b), D(a), D(10), D(10), at)
        for c, (b, a) in (quotes or {}).items()
        if c.underlying == underlying
    )
    return ChainSnapshot(underlying, at, D(price), rows, None, None, "test")


def close_price(session: date, symbol: str, price: str) -> SettlementPrice:
    return SettlementPrice(symbol, session, SettleTime.PM, D(price), "official close",
                           CAL.session_close(session) + timedelta(minutes=70))


def spx_and_qqq(session: date, qqq_quotes: dict | None = None) -> list[ChainSnapshot]:
    """One QQQ and one SPX snapshot for the session (sorted by underlying in the timeline)."""
    return [snapshot(session, "QQQ", "500", qqq_quotes), snapshot(session, "SPX", "6000")]


class Approve:
    name = "approve"

    def evaluate(self, intent, context) -> RiskVerdict:
        rule = RiskRuleResult("test", True, "x", "x", "test rule")
        return RiskVerdict(intent.intent_id, evaluations=(rule,), refusal_reasons=())


class NoSignals:
    name = "none"

    def read_signals(self, session):
        return []


class NoBars:
    def bars(self, *args, **kwargs):
        raise AssertionError("an options account replays no bars")


class Universe:
    """``option_universe(session)`` returns ``universe`` (or ``None``), and records the
    underlying of every snapshot shown at the snapshot phase."""

    name = "universe"

    def __init__(self, universe=None) -> None:
        self.universe = universe
        self.seen: list[str] = []

    def option_universe(self, session):
        return self.universe

    def manage_options(self, context):
        if context.phase == "snapshot":
            self.seen.append(context.snapshot.underlying)
        return []

    def generate_intents(self, signals, context):
        return []


class NoHook:
    """No ``option_universe`` attribute: sees every snapshot."""

    name = "no-hook"

    def __init__(self) -> None:
        self.seen: list[str] = []

    def manage_options(self, context):
        if context.phase == "snapshot":
            self.seen.append(context.snapshot.underlying)
        return []

    def generate_intents(self, signals, context):
        return []


def qqq_put() -> OptionIntent:
    return OptionIntent(
        intent_id="qqq-put", account_id=ACCOUNT, instrument=QPUT, side=Side.SELL, quantity=D(1),
        reason="test", command_id="qqq-put", order_type=OrderType.LIMIT, limit_price=D("5.00"),
        profit_target=None,
    )


class OpenQqq:
    """Writes the QQQ put at S1's QQQ snapshot, so S2 holds it."""

    name = "open-qqq"

    def manage_options(self, context):
        if context.phase == "snapshot" and context.snapshot.underlying == "QQQ":
            return [qqq_put()]
        return []

    def generate_intents(self, signals, context):
        return []


class OpenShares:
    """Buys QQQ shares at S1's QQQ snapshot, so S2 holds them."""

    name = "open-shares"

    def manage_options(self, context):
        if context.phase == "snapshot" and context.snapshot.underlying == "QQQ":
            return [OptionIntent(
                intent_id="buy-qqq", account_id=ACCOUNT, instrument=QQQ, side=Side.BUY,
                quantity=D(100), reason="wheel", command_id="buy-qqq", order_type=OrderType.MARKET,
            )]
        return []

    def generate_intents(self, signals, context):
        return []


class Rig:
    def __init__(self, tmp_path, strategy) -> None:
        self.clock = ReplayClock(CAL.session_open(S1))
        self.ledger = Ledger(tmp_path / "ledger.db").open()
        self.ledger.append(Event(
            account=ACCOUNT, kind=EventKind.CASH_FLOW, ts_utc=self.clock.now_utc(),
            command_id="deposit", payload=CashFlow(amount=D("50000"), kind="deposit", as_of=self.clock.now_utc()),
        ))
        self.venue = SnapshotVenue(ACCOUNT, self.clock)
        self.strategy = strategy
        self.snapshots: dict[date, list] = {}
        self.settlements = FixedSettlements(
            [close_price(s, sym, price) for s in (S1, S2) for sym, price in (("QQQ", "500"), ("SPX", "6000"))]
        )
        self.dividends = FixedDividends({"QQQ": [], "SPX": []})
        self.lifecycle = LifecyclePass(self.ledger, self.clock, CAL, self.settlements, dividends=self.dividends)

    def config(self) -> EodRunnerConfig:
        return EodRunnerConfig(
            job_name="eod",
            brokers={ACCOUNT: self.venue},
            signal_adapters={ACCOUNT: NoSignals()},
            strategies={ACCOUNT: self.strategy},
            chain_snapshots=lambda session: self.snapshots.get(session, []),
            settlements=self.settlements,
            lifecycle=self.lifecycle,
            dividends=self.dividends,
            option_risk_engines={ACCOUNT: Approve()},
        )

    def run(self, session: date):
        if self.clock.now_utc() < CAL.session_open(session):
            self.clock.advance_to(CAL.session_open(session))
        return EodRunner(self.ledger, self.clock, CAL, NoBars(), self.config()).run(session)

    def held(self, instrument) -> Decimal:
        position = self.ledger.state(ACCOUNT).positions.get(instrument)
        return D(0) if position is None else position.quantity


def test_a_universe_narrows_the_snapshots_the_strategy_sees(tmp_path) -> None:
    # (a) universe {"SPX"} and no holdings: only the SPX snapshot reaches manage_options.
    strategy = Universe({"SPX"})
    rig = Rig(tmp_path, strategy)
    rig.snapshots[S1] = spx_and_qqq(S1)
    rig.run(S1)
    assert strategy.seen == ["SPX"]


def test_a_held_option_keeps_its_snapshots(tmp_path) -> None:
    # (b) universe {"SPX"}, but S2 holds a QQQ put: QQQ snapshots still arrive.
    opener = OpenQqq()
    rig = Rig(tmp_path, opener)
    rig.snapshots[S1] = spx_and_qqq(S1, QPUT_QUOTES)
    rig.run(S1)
    assert rig.held(QPUT) == -1

    strategy = Universe({"SPX"})
    rig.strategy = strategy
    rig.snapshots[S2] = spx_and_qqq(S2, QPUT_QUOTES)
    rig.run(S2)
    assert strategy.seen == ["QQQ", "SPX"]


def test_held_shares_keep_their_snapshots(tmp_path) -> None:
    # (b) universe {"SPX"}, but S2 holds QQQ shares: QQQ snapshots still arrive.
    opener = OpenShares()
    rig = Rig(tmp_path, opener)
    rig.snapshots[S1] = spx_and_qqq(S1)
    rig.run(S1)
    assert rig.held(QQQ) == 100

    strategy = Universe({"SPX"})
    rig.strategy = strategy
    rig.snapshots[S2] = spx_and_qqq(S2)
    rig.run(S2)
    assert strategy.seen == ["QQQ", "SPX"]


def test_a_strategy_with_no_hook_sees_every_snapshot(tmp_path) -> None:
    # (c) no option_universe: every snapshot, as today.
    strategy = NoHook()
    rig = Rig(tmp_path, strategy)
    rig.snapshots[S1] = spx_and_qqq(S1)
    rig.run(S1)
    assert strategy.seen == ["QQQ", "SPX"]


def test_a_strategy_whose_universe_is_none_sees_every_snapshot(tmp_path) -> None:
    # (c) option_universe returns None: every snapshot, as today.
    strategy = Universe(None)
    rig = Rig(tmp_path, strategy)
    rig.snapshots[S1] = spx_and_qqq(S1)
    rig.run(S1)
    assert strategy.seen == ["QQQ", "SPX"]


def test_a_universe_returned_as_a_bare_str_is_refused(tmp_path) -> None:
    # "SPX" would iterate as "S", "P", "X" and silently drop every snapshot: refuse instead.
    rig = Rig(tmp_path, Universe("SPX"))
    rig.snapshots[S1] = spx_and_qqq(S1)
    with pytest.raises(TypeError, match="returned a str"):
        rig.run(S1)

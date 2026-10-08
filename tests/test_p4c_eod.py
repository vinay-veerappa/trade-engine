"""T7 synthetic worlds: exact outcomes, callback order and every ledger prefix."""
from __future__ import annotations

from collections import Counter
from dataclasses import replace
from datetime import date, datetime, time, timedelta
import hashlib
import json
from pathlib import Path
import random
import socket
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
import trade_engine_rs  # D5: the native flow is mandatory.

from trade_engine.eod import runner as P, options_routing as PR
from trade_engine.sim import _rs

_registry = dict(_rs._KINDS)
try:
    from frozen_p4c.t7 import runner as O, options_routing as OR
finally:
    _rs._KINDS.clear()
    _rs._KINDS.update(_registry)

from trade_engine.calendar.sessions import ExchangeCalendar
from trade_engine.clock.replay import ReplayClock
from trade_engine.ledger import EodRun, Event, EventKind, Ledger, codec
from trade_engine.sim import SimBroker, SnapshotVenue
from test_p4c_embed import package as native_package, run as native_run, PLUGIN as PACKAGING_PLUGIN
from test_eod_options import (
    ACCOUNT as OA, Approve, BuyWrite, C330, COHR, NoBars, NoSignals, P270,
    Rig, Runaway, S1, S2, S3, S6, chain, csp,
)
from test_eod_runner import (
    ACCOUNT, DeterministicStrategy, EOD_CLOCK, ExitStrategy, FakeMarketData,
    FixedRiskEngine, INSTRUMENT, PREV_EOD, SESSION, SessionSignalAdapter,
    SettableClock, _fixed_context_builder, _seed_bracket, _signal,
)
from test_p4a_parity import norm

CAL = ExchangeCalendar()
NY = ZoneInfo("America/New_York")
DATES = (date(2026, 1, 2), date(2026, 3, 9), date(2026, 7, 2),
         date(2026, 9, 25), date(2026, 11, 27), date(2026, 12, 24))
_OUTCOMES = Counter()
_TRACES = []


@pytest.fixture(scope="session", autouse=True)
def measured_comparisons():
    _OUTCOMES.clear()
    _TRACES.clear()
    yield
    print("T7_LOCKSTEP", json.dumps({
        "outer_steps": sum(_OUTCOMES.values()) // 2,
        "outcomes": {key: count // 2 for key, count in sorted(_OUTCOMES.items())},
        "unique_event_fold_outbox_meta_prefixes": sum(len(trace.prefixes) for trace in _TRACES) // 2,
        "ordered_callback_observations": sum(len(trace.calls) for trace in _TRACES) // 2,
        "provenance": "synthetic",
    }, sort_keys=True))


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("T7 synthetic worlds must not reach a network venue")
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)


def outcome(fn):
    try:
        value = norm(fn())
        _OUTCOMES["ok"] += 1
        return ("ok", value)
    except Exception as error:
        _OUTCOMES[type(error).__name__] += 1
        cause = error.__cause__
        return ("raise", type(error).__name__, str(error),
                None if cause is None else (type(cause).__name__, str(cause)))


def stored(ledger):
    return (
        tuple(codec.event_bytes(e) for e in ledger.events()),
        codec.text(codec.canon(ledger.fold())),
        tuple(tuple(row) for row in ledger.conn.execute("SELECT * FROM outbox ORDER BY id")),
        tuple(tuple(row) for row in ledger.conn.execute("SELECT * FROM meta ORDER BY key")),
    )


class Trace:
    def __init__(self, rig):
        self.calls = []
        self.prefixes = []
        self.rig = rig
        _TRACES.append(self)
        self.wrap(rig.clock, "clock", ("now_utc", "advance_to"))
        self.wrap(rig.venue, "venue", (
            "connect", "submit", "cancel", "replace", "orders", "fills", "positions",
            "process_snapshot", "process_bar",
        ))
        self.wrap(rig.strategy, "strategy", ("manage_options", "generate_intents"))
        self.wrap(rig.risk, "risk", ("evaluate",))
        for destination, sink in sorted(getattr(rig, "sinks", {}).items()):
            self.wrap(sink, f"sink:{destination}", ("publish",))
        for name in ("append", "extend"):
            original = getattr(rig.ledger, name)
            def write(*args, _original=original, **kwargs):
                result = _original(*args, **kwargs)
                self.prefixes.append(stored(rig.ledger))
                return result
            setattr(rig.ledger, name, write)

    def wrap(self, owner, label, names):
        for name in names:
            original = getattr(owner, name, None)
            if not callable(original):
                continue
            def invoke(*args, _name=name, _original=original, **kwargs):
                self.calls.append((label, _name, norm(args), norm(kwargs)))
                value = _original(*args, **kwargs)
                return value
            setattr(owner, name, invoke)


class Pair:
    def __init__(self, root, configure=lambda r: None):
        self.rigs = []
        self.counts = Counter()
        for label, module in (("oracle", O), ("native", P)):
            path = root / label
            path.mkdir(parents=True)
            rig = Rig(path)
            configure(rig)
            previous = CAL.previous_session(rig.clock.now_utc().astimezone(NY).date())
            at = CAL.session_close(previous)
            rig.ledger.append(Event(account=OA, kind=EventKind.EOD_RUN, ts_utc=at,
                command_id=f"eod:eod:{OA}:{previous.isoformat()}",
                payload=EodRun(previous, "eod", OA, 0, at)))
            rig.module = module
            rig.trace = Trace(rig)
            self.rigs.append(rig)

    def runner(self, rig, changes):
        config = rig.module.EodRunnerConfig(**(
            vars(rig.config()) | changes
        ))
        return rig.module.EodRunner(rig.ledger, rig.clock, CAL, NoBars(), config)

    def step(self, session, *, name=None, through=None, changes=None, family="run"):
        results = []
        for rig in self.rigs:
            rig.clock._current_time = CAL.session_open(session)
            runner = self.runner(rig, changes or {})
            results.append(outcome(lambda: runner.run(session) if name is None
                else runner.run_pass(session, through, name)))
        assert results[0] == results[1], (family, results)
        assert self.rigs[0].trace.calls == self.rigs[1].trace.calls, family
        assert self.rigs[0].trace.prefixes == self.rigs[1].trace.prefixes, family
        assert stored(self.rigs[0].ledger) == stored(self.rigs[1].ledger), family
        self.counts[(family, results[0][0])] += 1
        return results[0]

    def close(self):
        for rig in self.rigs:
            rig.ledger.close()


def test_oracle_provenance():
    folder = Path(__file__).parent / "frozen_p4c" / "t7"
    for name, digest in (
        ("runner.py", "27ad03ab1c90e677ec6fe68c13c575f5c6d01ad81492b63f71367ae2af7dc75a"),
        ("options_routing.py", "bab9c2d76df58d5f0aeb263fc7870ec46fe541507e4d837cdd429a089c6f8c0d"),
    ):
        assert hashlib.sha256((folder / name).read_bytes()).hexdigest() == digest
    for name in ("eod_run", "eod_pass", "eod_replay", "eod_finish",
                 "options_manage", "options_apply", "options_enter"):
        assert callable(getattr(trade_engine_rs, name))


@pytest.mark.parametrize("seed", range(9))
def test_seeded_pass_resume_and_close(tmp_path, seed):
    rng = random.Random(seed)
    def configure(rig):
        rig.strategy.at_snapshot[S1] = [csp("entry")]
        for session in (S1, S2, S3):
            base = chain(session)
            rig.snapshots[session] = [
                replace(base, as_of=stamp,
                    quotes=tuple(replace(quote, as_of=stamp) for quote in base.quotes))
                for hour, minute in ((9, 45), (12, 30), (15, 45))
                for stamp in (datetime.combine(session, time(hour, minute), NY),)
            ]
    pair = Pair(tmp_path, configure)
    try:
        for session in (S1, S2, S3):
            for name, hour, minute in (("morning", 9, 45), ("midday", 12, 30), ("late", 15, 45)):
                through = datetime.combine(session, time(hour, minute), NY)
                if rng.choice((False, True)) or name == "morning":
                    result = pair.step(session, name=name, through=through)
                    assert result[0] == "ok", result
                    assert pair.step(session, name=name, through=through, family="replay")[0] == "ok"
            assert pair.step(session)[0] == "ok"
            assert pair.step(session, family="replay")[0] == "ok"
        assert pair.counts[("run", "ok")] >= 6
        assert pair.counts[("replay", "ok")] >= 6
        assert pair.rigs[0].trace.prefixes
    finally:
        pair.close()


@pytest.mark.parametrize("scenario", ("entry", "risk-refused", "resize", "buy-write", "runaway", "wrong-underlying"))
def test_snapshot_rounds_and_risk_order(tmp_path, scenario):
    def configure(rig):
        if scenario == "buy-write":
            rig.strategy = BuyWrite()
        elif scenario == "runaway":
            rig.strategy = Runaway()
            from trade_engine.domain.instruments import OptionContract, OptionRight
            from decimal import Decimal
            rig.quotes[S1] = {OptionContract("COHR", S6, Decimal(200 + n), OptionRight.PUT): ("1.00", "1.10")
                              for n in range(1, 6)}
        else:
            intent = csp("entry")
            if scenario == "wrong-underlying":
                intent = replace(intent, instrument=replace(P270, underlying="OTHER"))
            rig.strategy.at_snapshot[S1] = [intent]
            if scenario == "risk-refused":
                rig.risk = Approve(accept=False)
            if scenario == "resize":
                from decimal import Decimal
                class Resize(Approve):
                    def evaluate(self, intent, context):
                        return replace(super().evaluate(intent, context), approved_quantity=Decimal("1"))
                rig.strategy.at_snapshot[S1] = [replace(intent, quantity=Decimal("2"))]
                rig.risk = Resize()
    pair = Pair(tmp_path, configure)
    try:
        result = pair.step(S1)
        assert result[0] == ("raise" if scenario in ("runaway", "wrong-underlying") else "ok")
        if scenario == "risk-refused":
            assert len(pair.rigs[0].risk.seen) == 1
        if scenario == "buy-write":
            assert pair.rigs[0].held(COHR) == 100
            assert pair.rigs[0].held(C330) == -1
    finally:
        pair.close()

@pytest.mark.parametrize("site", ("connect", "manage_options", "evaluate"))
def test_callback_two_string_value_error_is_not_a_native_refusal(tmp_path, site):
    def fail(*args, **kwargs):
        raise ValueError("runtime_eod", "host exception must remain itself")
    def configure(rig):
        rig.strategy.at_snapshot[S1] = [csp("entry")]
        owner = rig.venue if site == "connect" else rig.risk if site == "evaluate" else rig.strategy
        setattr(owner, site, fail)
    pair = Pair(tmp_path, configure)
    try:
        result = pair.step(S1)
        assert result[0:2] == ("raise", "ValueError"), result
    finally:
        pair.close()


@pytest.mark.parametrize("session", DATES)
def test_pass_boundaries_and_refusal_counterparts(tmp_path, session):
    pair = Pair(tmp_path, lambda rig: setattr(rig.clock, "_current_time", CAL.session_open(session)))
    try:
        open_ = CAL.session_open(session)
        close = CAL.session_close(session)
        cases = (
            ("bad-name", "unknown", open_, "raise"),
            ("before-open", "morning", open_ - timedelta(microseconds=1), "raise"),
            ("at-close", "morning", close, "raise"),
            ("inside", "morning", open_, "ok"),
            ("resume", "midday", open_, "raise"),
            ("resume", "midday", open_ + timedelta(microseconds=1), "ok"),
        )
        for family, name, through, expected in cases:
            assert pair.step(session, name=name, through=through, family=family)[0] == expected
        assert pair.counts[("resume", "raise")] == pair.counts[("resume", "ok")] == 1
    finally:
        pair.close()


def test_equity_timeline_and_every_append_prefix(tmp_path):
    worlds = []
    try:
        from decimal import Decimal
        for label, module in (("oracle", O), ("native", P)):
            path = tmp_path / label
            path.mkdir()
            ledger = Ledger(path / "book.db").open()
            clock = SettableClock(PREV_EOD)
            venue = SimBroker(ACCOUNT, clock, Decimal("0"))
            venue.connect()
            _seed_bracket(ledger, venue, clock, command_id="entry", stop_price="95")
            rig = SimpleNamespace(ledger=ledger, clock=clock, venue=venue,
                                  strategy=object(), risk=object())
            rig.trace = Trace(rig)
            config = module.EodRunnerConfig("eod", {ACCOUNT: venue})
            rig.runner = module.EodRunner(ledger, clock, CAL,
                FakeMarketData({100: ("100", "101", "94", "95")}), config)
            worlds.append(rig)
        for _ in range(2):
            results = [outcome(lambda: world.runner.run(SESSION)) for world in worlds]
            assert results[0] == results[1]
            assert results[0][0] == "ok"
            assert worlds[0].trace.calls == worlds[1].trace.calls
            assert worlds[0].trace.prefixes == worlds[1].trace.prefixes
            assert stored(worlds[0].ledger) == stored(worlds[1].ledger)
    finally:
        for world in worlds:
            world.ledger.close()


@pytest.mark.parametrize("mode", ("entry", "refused", "signal-error", "intent-error",
                                 "move", "close", "reduce", "bad-exit"))
def test_equity_entry_and_exit_flow(tmp_path, mode):
    from decimal import Decimal
    from trade_engine.domain.exits import ClosePosition, MoveStop, ReducePosition
    worlds = []
    try:
        for label, module in (("oracle", O), ("native", P)):
            path = tmp_path / label
            path.mkdir()
            ledger = Ledger(path / "book.db").open()
            entry_mode = mode in ("entry", "refused", "signal-error", "intent-error")
            clock = SettableClock(EOD_CLOCK if entry_mode else PREV_EOD)
            venue = SimBroker(ACCOUNT, clock, Decimal("0"))
            venue.connect()
            if not entry_mode:
                _seed_bracket(ledger, venue, clock, command_id="held", entry_prefilled=True)
                def exits(brackets, context):
                    b = brackets[0]
                    if mode == "move":
                        return [MoveStop(b.entry_order_id, b.average_entry_price, "test", "move")]
                    if mode == "close":
                        return [ClosePosition(b.entry_order_id, "test", "close")]
                    if mode == "reduce":
                        return [ReducePosition(b.entry_order_id, Decimal("0.5"), "test", "reduce")]
                    return [ClosePosition("absent", "test", "bad")]
                strategy = ExitStrategy(exits)
            else:
                strategy = DeterministicStrategy(INSTRUMENT)
            adapter = SessionSignalAdapter([_signal()])
            if mode == "signal-error":
                def signals(session):
                    yield _signal()
                    raise ValueError("runtime_eod", "signal iterator failed")
                adapter.read_signals = signals
            if mode == "intent-error":
                original = strategy.generate_intents
                def intents(signals, context, _original=original):
                    yield from _original(signals, context)
                    raise ValueError("runtime_eod", "intent iterator failed")
                strategy.generate_intents = intents
            risk = FixedRiskEngine()
            if mode == "refused":
                original = risk.evaluate
                def refuse(intent, context, _original=original):
                    return replace(_original(intent, context), accepted=False,
                        approved_quantity=None, refusal_reasons=("synthetic refusal",))
                risk.evaluate = refuse
            rig = SimpleNamespace(ledger=ledger, clock=clock, venue=venue, strategy=strategy, risk=risk)
            rig.trace = Trace(rig)
            rig.trace.wrap(strategy, "strategy", ("manage_positions",))
            config = module.EodRunnerConfig("eod", {ACCOUNT: venue}, strategies={ACCOUNT: strategy},
                risk_engines={ACCOUNT: risk}, signal_adapters={ACCOUNT: adapter},
                context_builder=_fixed_context_builder())
            rig.runner = module.EodRunner(ledger, clock, CAL, FakeMarketData(), config)
            worlds.append(rig)
        results = [outcome(lambda: world.runner.run(SESSION)) for world in worlds]
        assert results[0] == results[1], results
        assert results[0][0] == ("raise" if mode in ("bad-exit", "signal-error", "intent-error") else "ok"), results
        assert worlds[0].trace.calls == worlds[1].trace.calls
        assert worlds[0].trace.prefixes == worlds[1].trace.prefixes
        assert stored(worlds[0].ledger) == stored(worlds[1].ledger)
    finally:
        for world in worlds:
            world.ledger.close()


def test_settlement_assignment_world(tmp_path):
    from trade_engine.lifecycle import FixedSettlements, LifecyclePass
    from test_eod_options import close_price
    def configure(rig):
        rig.strategy.entries[S1] = [csp("entry")]
        rig.settlements = FixedSettlements(
            [close_price(session, "250") for session in (S1, S2, S3, S6)])
        rig.lifecycle = LifecyclePass(rig.ledger, rig.clock, CAL, rig.settlements, dividends=rig.dividends)
    pair = Pair(tmp_path, configure)
    try:
        for session in (S1, S2, S3):
            assert pair.step(session)[0] == "ok"
        # A distinct fixture starts at expiry with the preceding marker and existing
        # option position. Missing intervening markers remain an explicit refusal.
        assert pair.step(S6)[0] == "raise"
        for rig in pair.rigs:
            prev = CAL.previous_session(S6)
            at = CAL.session_close(prev)
            rig.ledger.append(Event(account=OA, kind=EventKind.EOD_RUN, ts_utc=at,
                command_id=f"eod:eod:{OA}:{prev.isoformat()}", payload=EodRun(prev, "eod", OA, 0, at)))
        result = pair.step(S6)
        assert result[0] == "ok", result
        assert pair.rigs[0].held(P270) == 0
        assert pair.rigs[0].held(COHR) == 100
    finally:
        pair.close()

@pytest.mark.parametrize("mode", ("paid", "missing", "future"))
def test_dividend_refusal_and_paid_counterparts(tmp_path, mode):
    from trade_engine.domain.instruments import Side
    from trade_engine.domain.option_orders import OptionIntent
    from trade_engine.domain.orders import OrderType
    from trade_engine.lifecycle import FixedDividends
    from test_eod_options import dividend, dividends_booked
    from decimal import Decimal
    def configure(rig):
        record = dividend(S2, at=CAL.session_close(S2) + timedelta(hours=3)) if mode == "future" else dividend(S2)
        rig.dividends = None if mode == "missing" else FixedDividends({"COHR": [record]})
        rig.strategy.at_snapshot[S1] = [OptionIntent(intent_id="buy", account_id=OA, instrument=COHR,
            side=Side.BUY, quantity=Decimal("100"), reason="synthetic", command_id="buy", order_type=OrderType.MARKET)]
    pair = Pair(tmp_path, configure)
    try:
        assert pair.step(S1)[0] == "ok"
        result = pair.step(S2)
        assert result[0] == ("ok" if mode == "paid" else "raise"), result
        if mode == "paid":
            assert [cash.amount for cash in dividends_booked(pair.rigs[0])] == [Decimal("26.00")]
    finally:
        pair.close()


@pytest.mark.parametrize("name", (None, 2, "", "unknown"))
def test_pass_name_conversion_is_exact(tmp_path, name):
    pair = Pair(tmp_path)
    try:
        results = []
        for rig in pair.rigs:
            runner = pair.runner(rig, {})
            results.append(outcome(lambda: runner.run_pass(S1, CAL.session_open(S1), name)))
        assert results[0] == results[1], results
        assert results[0][0] == "raise"
    finally:
        pair.close()


def test_mixed_accounts_share_one_snapshot_before_bar_timeline(tmp_path):
    from decimal import Decimal
    from trade_engine.ledger import CashFlow
    from trade_engine.lifecycle import FixedDividends, FixedSettlements, LifecyclePass
    from test_eod_options import close_price
    worlds = []
    try:
        for label, module in (("oracle", O), ("native", P)):
            path = tmp_path / label
            path.mkdir()
            ledger = Ledger(path / "book.db").open()
            clock = SettableClock(PREV_EOD)
            ledger.append(Event(account=OA, kind=EventKind.CASH_FLOW, ts_utc=PREV_EOD,
                command_id="deposit", payload=CashFlow(Decimal("50000"), "deposit", PREV_EOD)))
            equity = SimBroker(ACCOUNT, clock, Decimal("0"))
            equity.connect()
            _seed_bracket(ledger, equity, clock, command_id="mixed")
            options = SnapshotVenue(OA, clock)
            strategy = BuyWrite()
            risk = Approve()
            rig = SimpleNamespace(ledger=ledger, clock=clock, venue=options, strategy=strategy, risk=risk)
            rig.trace = Trace(rig)
            rig.trace.wrap(equity, "equity", ("process_bar", "submit", "orders", "fills", "connect"))
            at = CAL.session_open(SESSION)
            base = chain(SESSION)
            snapshot = replace(base, as_of=at, quotes=tuple(replace(q, as_of=at) for q in base.quotes))
            closes = FixedSettlements([close_price(SESSION)])
            dividends = FixedDividends({"COHR": []})
            lifecycle = LifecyclePass(ledger, clock, CAL, closes, dividends=dividends)
            config = module.EodRunnerConfig("eod", {ACCOUNT: equity, OA: options},
                strategies={OA: strategy}, option_risk_engines={OA: risk},
                chain_snapshots=lambda session, _snapshot=snapshot: [_snapshot],
                settlements=closes, dividends=dividends, lifecycle=lifecycle)
            rig.runner = module.EodRunner(ledger, clock, CAL, FakeMarketData(), config)
            worlds.append(rig)
        results = [outcome(lambda: world.runner.run(SESSION)) for world in worlds]
        assert results[0] == results[1], results
        assert results[0][0] == "ok", results
        assert worlds[0].trace.calls == worlds[1].trace.calls
        assert worlds[0].trace.prefixes == worlds[1].trace.prefixes
        assert stored(worlds[0].ledger) == stored(worlds[1].ledger)
        calls = [(label, method) for label, method, *_ in worlds[0].trace.calls]
        assert calls.index(("venue", "process_snapshot")) < calls.index(("equity", "process_bar"))
    finally:
        for world in worlds:
            world.ledger.close()


def test_release_builtin_module_executes_eod_flow(native_package):
    config, _, _, plugins = native_package
    source = PACKAGING_PLUGIN + """
_packaging_probe = probe
def probe(config):
    from datetime import date
    from decimal import Decimal
    from trade_engine.calendar.sessions import ExchangeCalendar
    from trade_engine.clock.replay import ReplayClock
    from trade_engine.eod import EodRunner, EodRunnerConfig
    from trade_engine.ledger import Ledger
    from trade_engine.sim import SimBroker
    report = _packaging_probe(config)
    calendar = ExchangeCalendar()
    session = date(2026, 9, 25)
    clock = ReplayClock(calendar.session_open(session))
    with Ledger(config["synthetic_ledger"]) as ledger:
        broker = SimBroker("synthetic", clock, Decimal("0"))
        runner = EodRunner(ledger, clock, calendar, object(),
            EodRunnerConfig("eod", {"synthetic": broker}))
        result = runner.run(session)
        replay = runner.run(session)
        report["flow"] = [result.accounts[0].account_id, len(ledger.events()),
                          replay.accounts[0].orders_submitted]
    return report
"""
    (plugins / "fake_plugin.py").write_text(source, encoding="utf-8")
    config["plugin_config"]["synthetic_ledger"] = str(plugins / "synthetic.db")
    code, report = native_run(native_package)
    assert code == 0, report
    assert report["result"]["flow"] == ["synthetic", 1, 0]
    assert report["builtin_module_count"] == 1
    assert report["result"]["modules"] == ["trade_engine_rs"]
    assert not any(path.endswith(".pyd") and "trade_engine_rs" in path
                   for path in report["result"]["loaded"])


@pytest.mark.parametrize("failure", ("false", "exception", "success"))
def test_outbox_delivery_and_completed_run_retry(tmp_path, failure):
    class Sink:
        def __init__(self):
            self.failure = failure
        def publish(self, seq, payload):
            if self.failure == "exception":
                raise ValueError("synthetic sink failure")
            return self.failure != "false"
    def configure(rig):
        rig.strategy = BuyWrite()
        rig.sinks = {"journal:synthetic-journal": Sink()}
        original = rig.config
        def config(**changes):
            return original(sinks=rig.sinks, journal_accounts={OA: "synthetic-journal"}, **changes)
        rig.config = config
    pair = Pair(tmp_path, configure)
    try:
        assert pair.step(S1)[0] == "ok"
        pending = pair.rigs[0].ledger.pending_outbox()
        if failure == "success":
            assert not pending
        else:
            assert pending and pending[0].attempts == 1
        for rig in pair.rigs:
            rig.sinks["journal:synthetic-journal"].failure = "success"
        assert pair.step(S1, family="outbox-retry")[0] == "ok"
        assert not pair.rigs[0].ledger.pending_outbox()
    finally:
        pair.close()

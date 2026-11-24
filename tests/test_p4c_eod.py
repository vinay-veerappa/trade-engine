"""T7 synthetic EOD worlds: explicit outcomes, callback order and ledger state.

Each world is a synthetic book driven through the production runner (``trade_engine.eod``).
The expectations are literal: an outcome, the order in which a venue or strategy is called,
or the ledger state a run leaves behind.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, time, timedelta
from decimal import Decimal
import random
import socket
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
import trade_engine_rs  # D5: the native flow is mandatory.

from trade_engine.eod import runner as P
from trade_engine.calendar.sessions import ExchangeCalendar
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

CAL = ExchangeCalendar()
NY = ZoneInfo("America/New_York")
DATES = (date(2026, 1, 2), date(2026, 3, 9), date(2026, 7, 2),
         date(2026, 9, 25), date(2026, 11, 27), date(2026, 12, 24))


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("T7 synthetic worlds must not reach a network venue")
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)


def outcome(fn):
    """("ok", value) for a return, else ("raise", type, message, (cause type, message) or None)."""
    try:
        return ("ok", fn())
    except Exception as error:
        cause = error.__cause__
        return ("raise", type(error).__name__, str(error),
                None if cause is None else (type(cause).__name__, str(cause)))


def stored(ledger):
    """The ledger's full stored state: events, folded state, outbox and meta rows."""
    return (
        tuple(codec.event_bytes(e) for e in ledger.events()),
        codec.text(codec.canon(ledger.fold())),
        tuple(tuple(row) for row in ledger.conn.execute("SELECT * FROM outbox ORDER BY id")),
        tuple(tuple(row) for row in ledger.conn.execute("SELECT * FROM meta ORDER BY key")),
    )


def markers(ledger, account=OA):
    """The command ids of the after-close markers recorded for ``account``."""
    return {e.command_id for e in ledger.events(account=account) if e.kind is EventKind.EOD_RUN}


class Trace:
    """Records, for one book, the order of calls it receives and the event count after each write."""

    def __init__(self, rig):
        self.calls = []
        self.event_counts = []
        self.wrap(rig.clock, "clock", ("now_utc", "advance_to"))
        self.wrap(rig.venue, "venue", (
            "connect", "submit", "cancel", "replace", "orders", "fills", "positions",
            "process_snapshot", "process_bar",
        ))
        self.wrap(rig.strategy, "strategy", ("manage_options", "generate_intents"))
        self.wrap(rig.risk, "risk", ("evaluate",))
        for destination, sink in sorted(getattr(rig, "sinks", {}).items()):
            self.wrap(sink, f"sink:{destination}", ("publish",))
        # The ledger's after-commit listener sees every write, Python or native, once per event.
        rig.ledger.add_listener(lambda _event: self.event_counts.append(len(rig.ledger.events())))

    def wrap(self, owner, label, names):
        for name in names:
            original = getattr(owner, name, None)
            if not callable(original):
                continue
            def invoke(*args, _name=name, _original=original, **kwargs):
                self.calls.append((label, _name))
                return _original(*args, **kwargs)
            setattr(owner, name, invoke)


class World:
    """One synthetic options book with the previous session's close already recorded."""

    def __init__(self, root, configure=lambda rig: None):
        self.rig = Rig(root)
        configure(self.rig)
        previous = CAL.previous_session(self.rig.clock.now_utc().astimezone(NY).date())
        at = CAL.session_close(previous)
        self.rig.ledger.append(Event(account=OA, kind=EventKind.EOD_RUN, ts_utc=at,
            command_id=f"eod:eod:{OA}:{previous.isoformat()}",
            payload=EodRun(previous, "eod", OA, 0, at)))
        self.rig.trace = Trace(self.rig)

    def runner(self, changes=None):
        config = P.EodRunnerConfig(**(vars(self.rig.config()) | (changes or {})))
        return P.EodRunner(self.rig.ledger, self.rig.clock, CAL, NoBars(), config)

    def step(self, session, *, name=None, through=None):
        self.rig.clock._current_time = CAL.session_open(session)
        runner = self.runner()
        return outcome(lambda: runner.run(session) if name is None
                       else runner.run_pass(session, through, name))

    def close(self):
        self.rig.ledger.close()


def test_native_bindings_exposed():
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
    world = World(tmp_path, configure)
    try:
        for session in (S1, S2, S3):
            for name, hour, minute in (("morning", 9, 45), ("midday", 12, 30), ("late", 15, 45)):
                through = datetime.combine(session, time(hour, minute), NY)
                if rng.choice((False, True)) or name == "morning":
                    assert world.step(session, name=name, through=through)[0] == "ok"
                    before = stored(world.rig.ledger)
                    # A repeated pass is admitted and adds nothing to the ledger.
                    assert world.step(session, name=name, through=through)[0] == "ok"
                    assert stored(world.rig.ledger) == before
            assert world.step(session)[0] == "ok"
            before = stored(world.rig.ledger)
            assert world.step(session)[0] == "ok"
            assert stored(world.rig.ledger) == before
        assert {f"eod:eod:{OA}:{s.isoformat()}" for s in (S1, S2, S3)} <= markers(world.rig.ledger)
    finally:
        world.close()


@pytest.mark.parametrize("scenario", ("entry", "risk-refused", "resize", "buy-write", "runaway", "wrong-underlying"))
def test_snapshot_rounds_and_risk_order(tmp_path, scenario):
    def configure(rig):
        if scenario == "buy-write":
            rig.strategy = BuyWrite()
        elif scenario == "runaway":
            rig.strategy = Runaway()
            from trade_engine.domain.instruments import OptionContract, OptionRight
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
                class Resize(Approve):
                    def evaluate(self, intent, context):
                        return replace(super().evaluate(intent, context), approved_quantity=Decimal("1"))
                rig.strategy.at_snapshot[S1] = [replace(intent, quantity=Decimal("2"))]
                rig.risk = Resize()
    world = World(tmp_path, configure)
    try:
        result = world.step(S1)
        assert result[0] == ("raise" if scenario in ("runaway", "wrong-underlying") else "ok")
        if scenario == "risk-refused":
            assert len(world.rig.risk.seen) == 1
        if scenario == "buy-write":
            assert world.rig.held(COHR) == 100
            assert world.rig.held(C330) == -1
    finally:
        world.close()


@pytest.mark.parametrize("site", ("connect", "manage_options", "evaluate"))
def test_callback_two_string_value_error_is_not_a_native_refusal(tmp_path, site):
    def fail(*args, **kwargs):
        raise ValueError("runtime_eod", "host exception must remain itself")
    def configure(rig):
        rig.strategy.at_snapshot[S1] = [csp("entry")]
        owner = rig.venue if site == "connect" else rig.risk if site == "evaluate" else rig.strategy
        setattr(owner, site, fail)
    world = World(tmp_path, configure)
    try:
        result = world.step(S1)
        assert result[0:3] == ("raise", "ValueError",
                               "('runtime_eod', 'host exception must remain itself')"), result
    finally:
        world.close()


@pytest.mark.parametrize("session", DATES)
def test_pass_boundaries_and_refusal_counterparts(tmp_path, session):
    world = World(tmp_path, lambda rig: setattr(rig.clock, "_current_time", CAL.session_open(session)))
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
        for _family, name, through, expected in cases:
            assert world.step(session, name=name, through=through)[0] == expected
    finally:
        world.close()


def equity_book(root, clock_time, *, configure_ledger=None):
    """A stock account with one bracket on the ledger, driven by the production runner."""
    ledger = Ledger(root / "book.db").open()
    clock = SettableClock(clock_time)
    venue = SimBroker(ACCOUNT, clock, Decimal("0"))
    venue.connect()
    if configure_ledger is not None:
        configure_ledger(ledger, venue, clock)
    return SimpleNamespace(ledger=ledger, clock=clock, venue=venue, strategy=object(), risk=object())


def test_equity_timeline_and_every_append_prefix(tmp_path):
    book = equity_book(tmp_path, PREV_EOD,
        configure_ledger=lambda ledger, venue, clock: _seed_bracket(
            ledger, venue, clock, command_id="entry", stop_price="95"))
    try:
        base = len(book.ledger.events())
        book.trace = Trace(book)
        runner = P.EodRunner(book.ledger, book.clock, CAL,
            FakeMarketData({100: ("100", "101", "94", "95")}),
            P.EodRunnerConfig("eod", {ACCOUNT: book.venue}))
        assert outcome(lambda: runner.run(SESSION))[0] == "ok"
        counts = [base, *book.trace.event_counts]
        assert book.trace.event_counts, "the run wrote nothing"
        # Every append adds exactly one event: each prefix is a complete, decodable ledger.
        assert all(later - earlier == 1 for earlier, later in zip(counts, counts[1:]))
        before = stored(book.ledger)
        assert outcome(lambda: runner.run(SESSION))[0] == "ok"
        assert stored(book.ledger) == before
    finally:
        book.ledger.close()


@pytest.mark.parametrize("mode", ("entry", "refused", "signal-error", "intent-error",
                                 "move", "close", "reduce", "bad-exit"))
def test_equity_entry_and_exit_flow(tmp_path, mode):
    from trade_engine.domain.exits import ClosePosition, MoveStop, ReducePosition
    entry_mode = mode in ("entry", "refused", "signal-error", "intent-error")
    def configure_ledger(ledger, venue, clock):
        if not entry_mode:
            _seed_bracket(ledger, venue, clock, command_id="held", entry_prefilled=True)
    book = equity_book(tmp_path, EOD_CLOCK if entry_mode else PREV_EOD, configure_ledger=configure_ledger)
    try:
        if not entry_mode:
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
        book.strategy, book.risk = strategy, risk
        book.trace = Trace(book)
        book.trace.wrap(strategy, "strategy", ("manage_positions",))
        config = P.EodRunnerConfig("eod", {ACCOUNT: book.venue}, strategies={ACCOUNT: strategy},
            risk_engines={ACCOUNT: risk}, signal_adapters={ACCOUNT: adapter},
            context_builder=_fixed_context_builder())
        runner = P.EodRunner(book.ledger, book.clock, CAL, FakeMarketData(), config)
        result = outcome(lambda: runner.run(SESSION))
        assert result[0] == ("raise" if mode in ("bad-exit", "signal-error", "intent-error") else "ok"), result
    finally:
        book.ledger.close()


def test_settlement_assignment_world(tmp_path):
    from trade_engine.lifecycle import FixedSettlements, LifecyclePass
    from test_eod_options import close_price
    def configure(rig):
        rig.strategy.entries[S1] = [csp("entry")]
        rig.settlements = FixedSettlements(
            [close_price(session, "250") for session in (S1, S2, S3, S6)])
        rig.lifecycle = LifecyclePass(rig.ledger, rig.clock, CAL, rig.settlements, dividends=rig.dividends)
    world = World(tmp_path, configure)
    try:
        for session in (S1, S2, S3):
            assert world.step(session)[0] == "ok"
        # A distinct fixture starts at expiry with the preceding marker and existing
        # option position. Missing intervening markers remain an explicit refusal.
        assert world.step(S6)[0] == "raise"
        prev = CAL.previous_session(S6)
        at = CAL.session_close(prev)
        world.rig.ledger.append(Event(account=OA, kind=EventKind.EOD_RUN, ts_utc=at,
            command_id=f"eod:eod:{OA}:{prev.isoformat()}", payload=EodRun(prev, "eod", OA, 0, at)))
        result = world.step(S6)
        assert result[0] == "ok", result
        assert world.rig.held(P270) == 0
        assert world.rig.held(COHR) == 100
    finally:
        world.close()


@pytest.mark.parametrize("mode", ("paid", "missing", "future"))
def test_dividend_refusal_and_paid_counterparts(tmp_path, mode):
    from trade_engine.domain.instruments import Side
    from trade_engine.domain.option_orders import OptionIntent
    from trade_engine.domain.orders import OrderType
    from trade_engine.lifecycle import FixedDividends
    from test_eod_options import dividend, dividends_booked
    def configure(rig):
        record = dividend(S2, at=CAL.session_close(S2) + timedelta(hours=3)) if mode == "future" else dividend(S2)
        rig.dividends = None if mode == "missing" else FixedDividends({"COHR": [record]})
        rig.strategy.at_snapshot[S1] = [OptionIntent(intent_id="buy", account_id=OA, instrument=COHR,
            side=Side.BUY, quantity=Decimal("100"), reason="synthetic", command_id="buy", order_type=OrderType.MARKET)]
    world = World(tmp_path, configure)
    try:
        assert world.step(S1)[0] == "ok"
        result = world.step(S2)
        assert result[0] == ("ok" if mode == "paid" else "raise"), result
        if mode == "paid":
            assert [cash.amount for cash in dividends_booked(world.rig)] == [Decimal("26.00")]
    finally:
        world.close()


@pytest.mark.parametrize("name, error", ((None, "TypeError"), (2, "TypeError"),
                                         ("", "EodRunnerError"), ("unknown", "EodRunnerError")))
def test_pass_name_conversion_is_exact(tmp_path, name, error):
    world = World(tmp_path)
    try:
        result = outcome(lambda: world.runner().run_pass(S1, CAL.session_open(S1), name))
        assert result[0:2] == ("raise", error), result
    finally:
        world.close()


def test_mixed_accounts_share_one_snapshot_before_bar_timeline(tmp_path):
    from trade_engine.ledger import CashFlow
    from trade_engine.lifecycle import FixedDividends, FixedSettlements, LifecyclePass
    from test_eod_options import close_price
    ledger = Ledger(tmp_path / "book.db").open()
    try:
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
        config = P.EodRunnerConfig("eod", {ACCOUNT: equity, OA: options},
            strategies={OA: strategy}, option_risk_engines={OA: risk},
            chain_snapshots=lambda session, _snapshot=snapshot: [_snapshot],
            settlements=closes, dividends=dividends, lifecycle=lifecycle)
        runner = P.EodRunner(ledger, clock, CAL, FakeMarketData(), config)
        assert outcome(lambda: runner.run(SESSION))[0] == "ok"
        calls = [(label, method) for label, method in rig.trace.calls]
        assert calls.index(("venue", "process_snapshot")) < calls.index(("equity", "process_bar"))
    finally:
        ledger.close()


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
    world = World(tmp_path, configure)
    try:
        assert world.step(S1)[0] == "ok"
        pending = world.rig.ledger.pending_outbox()
        if failure == "success":
            assert not pending
        else:
            assert pending and pending[0].attempts == 1
        world.rig.sinks["journal:synthetic-journal"].failure = "success"
        assert world.step(S1)[0] == "ok"
        assert not world.rig.ledger.pending_outbox()
    finally:
        world.close()

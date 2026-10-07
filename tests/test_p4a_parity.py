"""P4a independent frozen worlds, dense boundaries and stateful runtime walks."""
from __future__ import annotations

import dataclasses
import random
import sys
from collections import Counter
from collections.abc import Mapping
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from enum import Enum
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import trade_engine_rs  # noqa: F401 - D5
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from frozen_p4a import runner as O, options_routing as OR, service as OS
from trade_engine.eod import runner as P
from trade_engine.eod import options_routing as PR
from trade_engine.intraday import service as PS
from trade_engine.calendar.sessions import ExchangeCalendar
from trade_engine.clock.replay import ReplayClock
from trade_engine.domain.instruments import Equity, Side
from trade_engine.domain.portfolio import Position, Fill
from trade_engine.domain.signals import OrderIntent
from trade_engine.oms.manager import OrderManager
from trade_engine.lifecycle import FixedDividends, Dividend
from trade_engine.ledger import Ledger, Event, EventKind, EodRun, CashFlow, codec
from trade_engine.ledger.state import AccountState
from trade_engine.sim import SimBroker, SnapshotVenue
from test_eod_runner import FakeMarketData, _seed_bracket, SettableClock, PREV_EOD, SESSION, ACCOUNT
from test_eod_options import Rig as OptionRig, NoBars, NoSignals, chain, S1, S2, S3, csp, ACCOUNT as OA
from test_p3a_parity import by_value, no_fingerprint, respell_text
from test_intraday_service import Rig as IntradayRig, Scripted, snap, spread, at_et, Approve, ACCOUNT as IA, SESSION as IS

D = Decimal
CAL = ExchangeCalendar()
NY = ZoneInfo("America/New_York")
DATES = (date(2026, 1, 2), date(2026, 3, 9), date(2026, 7, 2),
         date(2026, 9, 25), date(2026, 11, 27), date(2026, 12, 24))


def norm(v):
    if isinstance(v, Decimal):
        return ("Decimal", by_value(v))
    if dataclasses.is_dataclass(v):
        return tuple((f.name, norm(getattr(v, f.name))) for f in dataclasses.fields(v))
    if isinstance(v, Mapping):
        return sorted(((norm(k), norm(x)) for k, x in v.items()), key=repr)
    if isinstance(v, (tuple, list)):
        return tuple(map(norm, v))
    if isinstance(v, (set, frozenset)):
        return tuple(sorted(map(norm, v), key=repr))
    if isinstance(v, datetime):
        return v.isoformat()
    if isinstance(v, Enum):
        return v.value
    return v


def result(fn):
    try:
        return ("ok", norm(fn()))
    except Exception as err:
        return ("raise", type(err).__name__, respell_text(str(err)))


def events(ledger):
    return [(e.seq, e.account, e.kind.value, e.command_id, e.ts_utc.isoformat(),
             respell_text(no_fingerprint(codec.text(codec.encode_payload(e.payload))))) for e in ledger.events()]


class Pair:
    def __init__(self, root, account="dense"):
        root.mkdir(parents=True, exist_ok=True)
        self.worlds = []
        self.counts = Counter()
        self.account = account
        for label, eod, routing, intraday in (("oracle", O, OR, OS), ("production", P, PR, PS)):
            path = root / label
            path.mkdir()
            ledger = Ledger(path / "ledger.db").open()
            clock = ReplayClock(CAL.session_open(DATES[0]))
            broker = SnapshotVenue(account, clock)
            config = eod.EodRunnerConfig("eod", {account: broker})
            runner = eod.EodRunner(ledger, clock, CAL, FakeMarketData(), config)
            ic = intraday.IntradayConfig("intra", account, "SPX", broker, SimpleNamespace(),
                None, lambda underlying, now: None, "eod")
            service = intraday.IntradayService(ledger, clock, CAL, ic)
            self.worlds.append(SimpleNamespace(eod=eod, routing=routing, intraday=intraday,
                ledger=ledger, clock=clock, broker=broker, runner=runner, service=service))

    def do(self, name, fn):
        a, b = [result(lambda: fn(w)) for w in self.worlds]
        if b[0] == "raise" and b[2] == "invalid runtime decimal":
            self.counts["bound"] += 1  # P7: beyond the 96-bit bound the wire refuses, with its own words
        else:
            assert a == b, (name, self.counts["steps"], a, b)
        self.counts["steps"] += 1
        self.counts[name] += 1
        self.counts[name + ":" + a[0]] += 1
        if a[0] == "raise":
            self.counts["refusal:" + a[1]] += 1
        assert events(self.worlds[0].ledger) == events(self.worlds[1].ledger), name
        return a

    def close(self):
        try:
            assert codec.text(codec.canon(self.worlds[0].ledger.state(self.account))) == codec.text(codec.canon(self.worlds[1].ledger.state(self.account)))
        finally:
            for w in self.worlds:
                w.ledger.close()


def test_dense_runtime_campaign(tmp_path):
    pair = Pair(tmp_path / "dense")
    rng = random.Random(0x5044A)
    try:
        for i in range(45000):
            quantities = [D(rng.randint(-100000, 100000)).scaleb(-rng.randrange(4)) for _ in range(3)]
            prices = [D(rng.randrange(1, 100000)).scaleb(-rng.randrange(4)) for _ in range(3)]
            cash = D(rng.randint(-100000, 100000)).scaleb(-rng.randrange(4))
            marks = rng.randrange(8)
            def risk(w):
                instruments = tuple(Equity(s) for s in ("AAA", "BBB", "CCC"))
                state = AccountState("dense", cash=cash,
                    positions={instrument: Position("dense", instrument, q, D("100"))
                        for instrument, q in zip(instruments, quantities)},
                    marks={instrument: price for j, (instrument, price) in enumerate(zip(instruments, prices)) if marks & (1 << j)})
                return w.runner._derive_risk_context(SimpleNamespace(instrument=instruments[i % 3]), state)
            pair.do("risk", risk)
        for i in range(20000):
            session = DATES[i % len(DATES)]
            entry = time(rng.randrange(9, 14), rng.randrange(60))
            flat = time(rng.randrange(13, 16), rng.randrange(60))
            delay = timedelta(seconds=rng.randrange(1, 7200), microseconds=rng.randrange(1000000))
            def deadlines(w):
                config = dataclasses.replace(w.service._config, entry_end=entry, flat_at=flat,
                    flat_before_close=delay, entry_before_close=delay + timedelta(minutes=i % 60))
                service = w.intraday.IntradayService(w.ledger, w.clock, CAL, config)
                return service._deadlines(session)
            pair.do("deadlines", deadlines)
        for i in range(25000):
            session = DATES[i % len(DATES)]
            snapshot = snap(session, 10, 0)
            snapshot = dataclasses.replace(snapshot, as_of=snapshot.as_of + timedelta(seconds=i))
            age = rng.choice((0, 29.999999, 30, 30.000001, 31, 90)) if i % 10 == 0 else rng.uniform(0, 90)
            quoted = snapshot.as_of - timedelta(seconds=age)
            snapshot = dataclasses.replace(snapshot,
                underlying_as_of=None if i % 17 == 0 else snapshot.as_of if i % 3 == 0 else quoted,
                quotes=tuple(dataclasses.replace(q, as_of=quoted) for q in snapshot.quotes))
            pair.do("fresh", lambda w: w.service._fresh_view(snapshot, snapshot.as_of))
        for i in range(32):
            bad = i % 9
            values = dict(job_name="" if bad == 0 else "eod", brokers={} if bad == 1 else {"x": object()},
                bars_max_age_seconds=(False, -1, 0, 1, float("nan"), 3.5, "x", 10, 30)[bad],
                settle_delay=timedelta(microseconds=-1 if bad == 8 else i))
            pair.do("config", lambda w: w.eod.EodRunnerConfig(**values))
        for i in range(10000):
            numbers = tuple(rng.randrange(-100000, 100000) for _ in range(6))
            pair.do("tally", lambda w: w.routing.RoutingTally(*numbers[:3]) + w.routing.RoutingTally(*numbers[3:]))
        for quantity in (D("1E+50"), D("-1E+50"), D("-0.999"), D("1.234567890123456789012345678901")):
            pair.do("risk_unbounded",lambda w: w.runner._derive_risk_context(
                SimpleNamespace(instrument=Equity("AAA")),
                AccountState("dense",cash=D("100"),positions={
                    Equity("AAA"):Position("dense",Equity("AAA"),quantity,D("100"))})))
        pair.do("tally_unbounded",lambda w: w.routing.RoutingTally(2**200,-2**200,2**200)
                + w.routing.RoutingTally(2**200,2**200,-2**200))
        for i in range(32):
            command = f"entry-{i}"
            is_entry = i % 2 == 0
            direct, risk = bool(i & 4), bool(i & 8)
            def taken(w):
                ledger = SimpleNamespace(has_command=lambda key: direct if key == command else risk)
                router = w.routing.OptionRouter(ledger, w.clock, brokers={}, strategies={}, option_risk_engines={})
                action = dataclasses.replace(spread(command), account_id="dense") if is_entry else SimpleNamespace(command_id=command)
                return router.taken(action)
            pair.do("taken", taken)
        assert pair.counts["steps"] == 100069
        assert pair.counts["fresh:raise"] > 1000
        assert pair.counts["fresh:ok"] > 1000
        assert pair.counts["config:raise"] > 0
        assert pair.counts["config:ok"] > 0
        print("P4A dense counters", dict(sorted(pair.counts.items())))
    finally:
        pair.close()


def test_runtime_refusal_kinds_and_counterparts(tmp_path):
    pair = Pair(tmp_path / "refusals")
    try:
        session = DATES[0]
        pair.do("config-refuse", lambda w: w.eod.EodRunnerConfig("", {"dense":w.broker}))
        pair.do("config-success", lambda w: w.eod.EodRunnerConfig("eod", {"dense":w.broker}).job_name)
        previous = CAL.previous_session(session)
        for w in pair.worlds:
            w.ledger.append(Event(account="dense",kind=EventKind.EOD_RUN,
                ts_utc=w.clock.now_utc(),command_id="old",
                payload=EodRun(CAL.previous_session(previous),"eod","dense",0,w.clock.now_utc())))
        pair.do("previous-refuse", lambda w: w.runner._require_previous_session_complete(session))
        for w in pair.worlds:
            w.ledger.append(Event(account="dense",kind=EventKind.EOD_RUN,
                ts_utc=w.clock.now_utc(),command_id=w.runner._run_command("dense",previous),
                payload=EodRun(previous,"eod","dense",0,w.clock.now_utc())))
        pair.do("previous-success", lambda w: w.runner._require_previous_session_complete(session))
        for w in pair.worlds:
            w.runner._market_data=SimpleNamespace(bars=lambda *args:[])
        pair.do("bars-refuse", lambda w: w.runner._load_bars(Equity("AAA"),CAL.session_open(session),CAL.session_close(session)))
        for w in pair.worlds:
            w.runner._market_data=FakeMarketData()
        pair.do("bars-success", lambda w: w.runner._load_bars(Equity("AAA"),CAL.session_open(session),CAL.session_close(session)))
        for w in pair.worlds:
            w.service._read_heartbeat=lambda w=w:w.intraday.Heartbeat("dense",session,w.clock.now_utc() - timedelta(seconds=60),False)
        pair.do("heartbeat-refuse", lambda w:w.service._refuse_other_live_instance(session))
        for w in pair.worlds:
            w.service._read_heartbeat=lambda w=w:w.intraday.Heartbeat("dense",session,w.clock.now_utc() - timedelta(seconds=60,microseconds=1),False)
        pair.do("heartbeat-success", lambda w:w.service._refuse_other_live_instance(session))
        snapshot = snap(session,10,0)
        pair.do("fresh-refuse", lambda w:w.service._fresh_view(dataclasses.replace(snapshot,underlying_as_of=None),snapshot.as_of))
        pair.do("fresh-success", lambda w:w.service._fresh_view(snapshot,snapshot.as_of))
        expected = {"EodRunnerError","SessionIncompleteError","ReplayDataError","IntradayServiceError","StaleDataError"}
        assert {key.removeprefix("refusal:") for key in pair.counts if key.startswith("refusal:")} == expected
        for name in ("config","previous","bars","heartbeat","fresh"):
            assert pair.counts[name+"-refuse:raise"] == 1
            assert pair.counts[name+"-success:ok"] == 1
        print("P4A refusal counterparts",dict(sorted(pair.counts.items())))
    finally:
        pair.close()


def test_preopen_dividend_boundary_and_amount(tmp_path):
    for offset in (-1,0,1):
        pair=Pair(tmp_path / str(offset))
        try:
            session=DATES[0]
            instrument=Equity("AAA")
            for w in pair.worlds:
                at=w.clock.now_utc()
                w.clock._current_time=at-timedelta(days=1)
                venue=SimBroker("dense",w.clock,D("0"))
                venue.connect()
                manager=OrderManager(venue,w.clock,w.ledger)
                intent=OrderIntent("entry","dense",instrument,Side.BUY,"fixed",D("100"),D("95"),(D("110"),),"test","entry")
                bracket=manager.create_bracket(intent,D("3.00"))
                manager.submit(bracket.entry)
                w.clock.advance_to(at+timedelta(microseconds=offset))
                manager.record_fill(Fill("fill",bracket.entry.order_id,"dense",instrument,D("3.00"),D("100"),
                    "sim",w.clock.now_utc(),Side.BUY))
                source=FixedDividends({"AAA":[Dividend("AAA",session,D("1.20"),"test",w.clock.now_utc()-timedelta(days=1))]})
                w.runner._config=dataclasses.replace(w.runner._config,dividends=source)
            pair.do("dividend",lambda w:w.runner._credit_dividends("dense",session))
            flows=[e.payload.amount for e in pair.worlds[1].ledger.events() if e.kind is EventKind.CASH_FLOW]
            assert flows == ([D("3.6000")] if offset < 0 else [])
        finally:
            pair.close()


def test_eod_options_lockstep_sessions_and_passes(tmp_path):
    rigs = []
    counts = Counter()
    try:
        for name, module in (("oracle", O), ("production", P)):
            root = tmp_path / name
            root.mkdir()
            r = OptionRig(root)
            r.module = module
            rigs.append(r)
        for r in rigs:
            r.strategy.entries[S1] = [csp("entry")]
            for session in (S1,S2,S3):
                base=chain(session,r.quotes.get(session))
                r.snapshots[session]=[]
                for hh,mm in ((9,45),(12,30),(15,45)):
                    stamp=datetime.combine(session,time(hh,mm),tzinfo=NY)
                    r.snapshots[session].append(dataclasses.replace(base,as_of=stamp,
                        underlying_as_of=stamp,
                        quotes=tuple(dataclasses.replace(q,as_of=stamp) for q in base.quotes)))
        for session in (S1, S2, S3):
            for name, hh, mm in (("morning",9,45), ("midday",12,30), ("late",15,45), (None,0,0)):
                def step(r):
                    if r.clock.now_utc() < CAL.session_open(session):
                        r.clock.advance_to(CAL.session_open(session))
                    kwargs = dict(job_name="eod", brokers={OA:r.venue}, signal_adapters={OA:NoSignals()},
                        strategies={OA:r.strategy}, chain_snapshots=lambda session:r.snapshots.get(session,[chain(session,r.quotes.get(session))]),
                        settlements=r.settlements,lifecycle=r.lifecycle,dividends=r.dividends,option_risk_engines={OA:r.risk})
                    config = r.module.EodRunnerConfig(**kwargs)
                    runner = r.module.EodRunner(r.ledger, r.clock, CAL, NoBars(), config)
                    if name is None:
                        return runner.run(session)
                    return runner.run_pass(session, datetime.combine(session, time(hh,mm), tzinfo=NY), name)
                a, b = [result(lambda: step(r)) for r in rigs]
                assert a == b, (session,name,a,b)
                assert events(rigs[0].ledger) == events(rigs[1].ledger)
                counts["steps"] += 1
        assert codec.text(codec.canon(rigs[0].state)) == codec.text(codec.canon(rigs[1].state))
        print("P4A options counters", dict(counts))
    finally:
        for r in rigs:
            r.ledger.close()


def test_equity_lockstep_replay(tmp_path):
    worlds = []
    try:
        for name, module in (("oracle",O),("production",P)):
            root = tmp_path / name
            root.mkdir()
            ledger = Ledger(root / "ledger.db").open()
            clock = SettableClock(PREV_EOD)
            broker = SimBroker(ACCOUNT,clock,D("0"))
            broker.connect()
            _seed_bracket(ledger,broker,clock,command_id="entry",stop_price="95")
            runner = module.EodRunner(ledger,clock,CAL,FakeMarketData({100:("100","101","94","95")}),
                module.EodRunnerConfig("eod",{ACCOUNT:broker}))
            worlds.append((ledger,runner))
        for session in (SESSION, SESSION):
            a,b = [result(lambda: r.run(session)) for _,r in worlds]
            assert a == b
            assert events(worlds[0][0]) == events(worlds[1][0])
        assert codec.text(codec.canon(worlds[0][0].state(ACCOUNT))) == codec.text(codec.canon(worlds[1][0].state(ACCOUNT)))
    finally:
        for ledger,_ in worlds:
            ledger.close()


@pytest.mark.parametrize("failure",("fresh","held-stale","missing-held","underlying-stale"))
def test_intraday_lockstep_ticks_and_restart(tmp_path,failure):
    rigs = []
    try:
        for name, module in (("oracle",OS),("production",PS)):
            root = tmp_path / name
            root.mkdir()
            r = IntradayRig(root)
            r.module = module
            r.minutes((9,30),(10,0))
            snapshot=r.snapshots[17]
            if failure == "held-stale":
                r.snapshots[17]=dataclasses.replace(snapshot,quotes=tuple(
                    dataclasses.replace(q,as_of=q.as_of-timedelta(seconds=31)) for q in snapshot.quotes))
            elif failure == "missing-held":
                r.snapshots[17]=dataclasses.replace(snapshot,quotes=snapshot.quotes[:1])
            elif failure == "underlying-stale":
                r.snapshots[17]=dataclasses.replace(snapshot,
                    underlying_as_of=snapshot.as_of-timedelta(seconds=31))
            r.strategy.actions[at_et(IS,9,45)] = [spread("entry")]
            r.venue.connect()
            rigs.append(r)
        states = [r.module._TickState() for r in rigs]
        for minute in range(31):
            for r in rigs:
                r.clock.advance_to(at_et(IS,9,30) + timedelta(minutes=minute))
            values=[]
            for r,state in zip(rigs,states):
                kwargs = dict(job_name="intraday",account_id=IA,underlying="SPX",broker=r.venue,
                    strategy=r.strategy,option_risk_engine=Approve(),snapshot_source=r.source,
                    eod_job_name="options",max_quote_age_seconds=30.0,tick_seconds=60.0)
                service = r.module.IntradayService(r.ledger,r.clock,CAL,r.module.IntradayConfig(**kwargs))
                if minute == 0:
                    service._rehydrate(IS,state)
                if minute == 20:
                    r.ledger.close()
                    r.ledger=Ledger(r.path).open()
                    r.venue=SnapshotVenue(IA,r.clock)
                    r.venue.connect()
                    actions,later=r.strategy.actions,r.strategy.at_or_after
                    r.strategy=Scripted(r.ledger)
                    r.strategy.actions,r.strategy.at_or_after=actions,later
                    service = r.module.IntradayService(r.ledger,r.clock,CAL,
                        r.module.IntradayConfig(**(kwargs | {"broker":r.venue,"strategy":r.strategy})))
                    service._rehydrate(IS,state)
                values.append(result(lambda: service._tick(IS,state)))
            assert values[0] == values[1], (minute,values)
            assert norm(states[0]) == norm(states[1]), minute
            if minute == 17 and failure != "fresh":
                assert states[0].refusing, failure
            assert events(rigs[0].ledger) == events(rigs[1].ledger), minute
        assert codec.text(codec.canon(rigs[0].ledger.state(IA))) == codec.text(codec.canon(rigs[1].ledger.state(IA)))
        assert states[0].tally.orders_submitted >= 1
    finally:
        for r in rigs:
            r.ledger.close()


def test_ordered_intraday_configuration(tmp_path):
    pair=Pair(tmp_path/"ordered-config")
    cases=(
        {"job_name":"","entry_end":None,"flat_before_close":None},
        {"account_id":"","entry_end":None},
        {"underlying":"","entry_end":None},
        {"eod_job_name":"","entry_end":None},
        {"max_quote_age_seconds":0,"entry_end":None},
        {"heartbeat_ttl_seconds":0,"entry_end":None},
        {"entry_end":time(16),"flat_before_close":None},
        {"flat_before_close":timedelta(0)},
        {},
    )
    try:
        for changes in cases:
            def config(w):
                value=dataclasses.replace(w.service._config,**changes)
                return tuple((field.name,getattr(value,field.name)) for field in dataclasses.fields(value)
                    if field.name not in ("broker","strategy","option_risk_engine","snapshot_source"))
            outcome=pair.do("intraday-config",config)
            assert outcome[0] == ("raise" if changes else "ok"),outcome
        print("P4A ordered intraday config steps=9 refusal=8 success=1")
    finally:
        pair.close()


@pytest.mark.parametrize("session", DATES)
def test_intraday_full_session_with_resume(tmp_path,session):
    rigs=[]
    frames=[[],[]]
    try:
        for index,(name,module) in enumerate((("oracle",OS),("production",PS))):
            root=tmp_path/name
            root.mkdir()
            r=IntradayRig(root,session=session)
            r.module=module
            closing=CAL.session_close(session).astimezone(NY)
            r.minutes((9,30),(closing.hour,closing.minute))
            r.strategy.actions[at_et(session,9,45)]=[spread("full-entry",session=session)]
            rigs.append(r)
        def service(r,index):
            config=r.module.IntradayConfig("intraday",IA,"SPX",r.venue,r.strategy,Approve(),r.source,
                "options",tick_seconds=60)
            instance=r.module.IntradayService(r.ledger,r.clock,CAL,config)
            tick=instance._tick
            def observed(s,state):
                outcome=result(lambda:tick(s,state))
                frames[index].append((outcome,events(r.ledger)))
                if outcome[0] == "raise":
                    raise RuntimeError(str(outcome))
            instance._tick=observed
            return instance
        stopped=[result(lambda r=r,i=i:service(r,i).run(session,stop_at=at_et(session,10,0))) for i,r in enumerate(rigs)]
        assert stopped[0] == stopped[1]
        assert frames[0] == frames[1]
        assert events(rigs[0].ledger) == events(rigs[1].ledger)
        assert any(position.quantity for position in rigs[0].state.positions.values())
        for r in rigs:
            r.ledger.close()
            r.ledger=Ledger(r.path).open()
            r.venue=SnapshotVenue(IA,r.clock)
            actions,later=r.strategy.actions,r.strategy.at_or_after
            r.strategy=Scripted(r.ledger)
            r.strategy.actions,r.strategy.at_or_after=actions,later
        finished=[result(lambda r=r,i=i:service(r,i).run(session)) for i,r in enumerate(rigs)]
        assert finished[0] == finished[1]
        assert frames[0] == frames[1]
        assert finished[0][0] == "ok", finished
        assert events(rigs[0].ledger) == events(rigs[1].ledger)
        assert codec.text(codec.canon(rigs[0].state)) == codec.text(codec.canon(rigs[1].state))
        assert all(position.quantity == 0 for position in rigs[0].state.positions.values())
        assert len(frames[0]) == int((CAL.session_close(session)-CAL.session_open(session)).total_seconds()/60)
        print(f"P4A full intraday {session} compared_ticks={len(frames[0])} outer_steps=2")
    finally:
        for r in rigs:
            r.ledger.close()

"""T6 exact clock walks and new trusted-factory contract on synthetic books."""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone, tzinfo
from decimal import Decimal
import importlib.util
from pathlib import Path
import random
import sys
import time
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
import trade_engine_rs

from frozen_p4c.t6_replay import ReplayClock as FrozenReplay
from frozen_p4c.t6_wall import WallClock as FrozenWall
from trade_engine.clock import ReplayClock, WallClock
from trade_engine.ledger.codec import canon, event_bytes
from trade_engine.ledger.events import CashFlow, Event, EventKind
from trade_engine.ledger.store import Ledger
from trade_engine.runtime.plugins import FactoryResult, load_factory
from test_p4c_embed import package as native_package, run as native_run, PLUGIN as PACKAGING_PLUGIN
from tools import ci_local

AT = datetime(2026, 10, 2, 13, 45, 1, 987654, tzinfo=timezone.utc)
ET = ZoneInfo("America/New_York")


def outcome(action):
    try:
        value = action()
        if isinstance(value, datetime):
            value = (type(value).__name__, value.isoformat(), str(value.tzinfo), value.fold)
        return "ok", value
    except BaseException as error:
        return "refusal", type(error).__name__, str(error)


def same(old, new, method, value):
    a = outcome(lambda: getattr(old, method)(value))
    b = outcome(lambda: getattr(new, method)(value))
    assert a == b
    assert outcome(old.now_utc) == outcome(new.now_utc)
    return a


class NoOffset(tzinfo):
    def utcoffset(self, dt):
        return None


BAD = [None, True, False, "1", Decimal("1"), [], -1, -0.000001,
       float("nan"), float("inf"), float("-inf"), 10**400]


@pytest.mark.parametrize("initial", [
    AT, AT.astimezone(ET), AT.replace(tzinfo=None),
    AT.replace(tzinfo=NoOffset()), None, "bad", datetime.min.replace(tzinfo=timezone.utc),
    datetime.max.replace(tzinfo=timezone.utc),
    datetime(2026, 11, 1, 1, 30, fold=0, tzinfo=ET),
    datetime(2026, 11, 1, 1, 30, fold=1, tzinfo=ET),
    datetime(2026, 3, 8, 1, 59, 59, tzinfo=ET),
    datetime(2026, 3, 8, 3, 0, tzinfo=ET),
])
def test_constructor_exact(initial):
    assert outcome(lambda: FrozenReplay(initial).now_utc()) == outcome(
        lambda: ReplayClock(initial).now_utc()
    )


@pytest.mark.parametrize("method", ["sleep", "advance_by"])
@pytest.mark.parametrize("duration", BAD + [
    0, -0.0, 1, 0.0000005, 0.0000015, 0.9999995,
    timedelta(0), timedelta(microseconds=-1), timedelta(days=-1, seconds=1),
    timedelta(days=999999999), timedelta(microseconds=123456),
])
def test_duration_exact(method, duration):
    same(FrozenReplay(AT), ReplayClock(AT), method, duration)


@pytest.mark.parametrize("target", [
    AT, AT.astimezone(ET), AT - timedelta(microseconds=1), AT + timedelta(microseconds=1),
    AT.replace(tzinfo=None), AT.replace(tzinfo=NoOffset()), None, "bad",
])
def test_target_exact(target):
    same(FrozenReplay(AT), ReplayClock(AT), "advance_to", target)


@pytest.mark.parametrize("at,seconds", [
    (datetime.max.replace(tzinfo=timezone.utc), 0.000001),
    (datetime.min.replace(tzinfo=timezone.utc), -1),
])
def test_overflow_preserves_current(at, seconds):
    same(FrozenReplay(at), ReplayClock(at), "advance_by", seconds)


def test_refusal_success_tally():
    pairs = {
        "awareness": (lambda: ReplayClock(AT), lambda: ReplayClock(AT.replace(tzinfo=None))),
        "backwards": (lambda: ReplayClock(AT).advance_to(AT),
                      lambda: ReplayClock(AT).advance_to(AT - timedelta(microseconds=1))),
        "numeric": (lambda: ReplayClock(AT).advance_by(1),
                    lambda: ReplayClock(AT).advance_by(float("nan"))),
        "timedelta": (lambda: ReplayClock(AT).advance_by(timedelta(seconds=1)),
                      lambda: ReplayClock(AT).advance_by(timedelta(microseconds=-1))),
        "type": (lambda: ReplayClock(AT).advance_by(1), lambda: ReplayClock(AT).advance_by(True)),
        "sleep": (lambda: ReplayClock(AT).sleep(0), lambda: ReplayClock(AT).sleep(True)),
        "overflow": (lambda: ReplayClock(AT).advance_by(1),
                     lambda: ReplayClock(datetime.max.replace(tzinfo=timezone.utc)).advance_by(1)),
        "wall": (lambda: WallClock().sleep(0), lambda: WallClock().sleep(-1)),
    }
    tally = Counter()
    for kind, actions in pairs.items():
        for action in actions:
            tally[kind, outcome(action)[0]] += 1
    assert tally == Counter({(kind, result): 1 for kind in pairs for result in ("ok", "refusal")})
    print("T6_CLOCK_REFUSAL_TALLY", dict(tally))


@pytest.mark.parametrize("seed", range(40))
def test_seeded_clock_prefixes(seed):
    rng = random.Random(seed)
    dates = [
        datetime(2026, 3, 6, 9, 45, tzinfo=ET), datetime(2026, 3, 9, 9, 45, tzinfo=ET),
        datetime(2026, 10, 30, 12, 30, tzinfo=ET), datetime(2026, 11, 2, 15, 45, tzinfo=ET),
        datetime(2026, 11, 27, 12, 30, tzinfo=ET), datetime(2026, 12, 24, 9, 45, tzinfo=ET),
    ]
    old, new = FrozenReplay(dates[seed % len(dates)]), ReplayClock(dates[seed % len(dates)])
    counts = Counter()
    for index in range(250):
        method = rng.choice(["advance_to", "advance_by", "sleep"])
        if index % 11 == 0:
            value = old.now_utc() - timedelta(microseconds=1) if method == "advance_to" else -1
        elif method == "advance_to":
            value = (old.now_utc() + timedelta(seconds=rng.randrange(60))).astimezone(ET)
        else:
            value = rng.choice([0, -0.0, 0.0000005, 0.0000015, 0.25, 59, 120.5])
        counts[same(old, new, method, value)[0]] += 1
    assert counts == Counter(ok=227, refusal=23)
    print("T6_CLOCK_PREFIXES", seed, sum(counts.values()), dict(counts))


def test_reset_and_overridden_advance_by():
    class Frozen(FrozenReplay):
        def advance_by(self, duration):
            self.seen = duration
            super().advance_by(duration + 1)

    class Native(ReplayClock):
        def advance_by(self, duration):
            self.seen = duration
            super().advance_by(duration + 1)

    old, new = Frozen(AT), Native(AT)
    old._current_time = new._current_time = AT - timedelta(days=1)
    same(old, new, "sleep", 0.5)
    assert old.seen == new.seen == 0.5
    assert new.now_utc() == AT - timedelta(days=1) + timedelta(seconds=1.5)


def test_datetime_subclass_inplace_add():
    class Custom(datetime):
        def __iadd__(self, value):
            return self + value + timedelta(seconds=1)
    initial = Custom(2026, 10, 2, tzinfo=timezone.utc)
    same(FrozenReplay(initial), ReplayClock(initial), "advance_by", 0.5)


@pytest.mark.parametrize("mode", ["add", "offset", "compare"])
def test_reentrant_datetime_callbacks(mode):
    holder, traces = [], []
    class Nested(datetime):
        def __iadd__(self, delta):
            traces.append(holder[0].now_utc().isoformat())
            holder[0]._current_time = AT + timedelta(days=1)
            return self + delta
        def __lt__(self, other):
            traces.append(holder[0].now_utc().isoformat())
            holder[0]._current_time = AT + timedelta(days=1)
            return True
    class Offset(tzinfo):
        def utcoffset(self, dt):
            traces.append(holder[0].now_utc().isoformat())
            return timedelta(hours=-4)
        def dst(self, dt):
            return timedelta(0)
    results = []
    for clock_type in (FrozenReplay, ReplayClock):
        clock = clock_type(AT)
        holder[:] = [clock]
        traces.clear()
        if mode == "add":
            clock._current_time = Nested(2026, 10, 2, tzinfo=timezone.utc)
            result = outcome(lambda: clock.advance_by(1))
        elif mode == "offset":
            result = outcome(lambda: clock.advance_to(AT.replace(tzinfo=Offset())))
        else:
            result = outcome(lambda: clock.advance_to(Nested(2026, 10, 2, tzinfo=timezone.utc)))
        results.append((result, outcome(clock.now_utc), traces[:]))
    assert results[0] == results[1]


def test_wall_sleeper_resolved_at_effect_boundary(monkeypatch):
    old, new = FrozenWall(), WallClock()
    calls = []
    monkeypatch.setattr(time, "sleep", lambda seconds: calls.append(seconds))
    assert outcome(lambda: old.sleep(0.01)) == outcome(lambda: new.sleep(0.01))
    assert calls == [0.01, 0.01]


@pytest.mark.parametrize("seconds", BAD + [0, -0.0, 0.000001, 0.01, True])
def test_wall_injected_sleep_exact(monkeypatch, seconds):
    import frozen_p4c.t6_wall as frozen
    calls = []
    monkeypatch.setattr(frozen.time, "sleep", lambda value: calls.append(value))
    old = outcome(lambda: FrozenWall().sleep(seconds))
    expected = calls[:]
    calls.clear()
    native = trade_engine_rs.NativeWallClock(sleeper=lambda value: calls.append(value))
    assert old == outcome(lambda: native.sleep(seconds))
    assert expected == calls


def test_wall_real_and_injected_observations():
    before = FrozenWall().now_utc()
    now = WallClock().now_utc()
    after = FrozenWall().now_utc()
    assert before <= now <= after
    assert now.tzinfo is timezone.utc
    started = time.perf_counter()
    WallClock().sleep(0.01)
    assert time.perf_counter() - started >= 0.005
    native = trade_engine_rs.NativeWallClock(now_reader=lambda: AT)
    assert native.now_utc() is AT
    error = ValueError("injected clock failure")
    def fail():
        raise error
    assert outcome(lambda: trade_engine_rs.NativeWallClock(now_reader=fail).now_utc()) == (
        "refusal", "ValueError", "injected clock failure"
    )
    assert outcome(lambda: trade_engine_rs.NativeWallClock(sleeper=lambda _: fail()).sleep(1)) == (
        "refusal", "ValueError", "injected clock failure"
    )


def test_i7_only_owner_clock_is_allowed(tmp_path, monkeypatch):
    monkeypatch.setattr(ci_local, "RUST_WORKSPACE", tmp_path)
    monkeypatch.setattr(ci_local, "REPO_ROOT", tmp_path)
    owner = tmp_path / "te_host" / "src"
    core = tmp_path / "te_core" / "src"
    owner.mkdir(parents=True)
    core.mkdir(parents=True)
    (owner / "clock.rs").write_text("SystemTime::now();", encoding="ascii")
    assert ci_local.check_rust_invariants()
    (core / "bad.rs").write_text("SystemTime::now();", encoding="ascii")
    assert not ci_local.check_rust_invariants()
    (core / "bad.rs").unlink()
    (owner / "store.rs").write_text("SystemTime::now();", encoding="ascii")
    assert not ci_local.check_rust_invariants()


PLUGIN = '''
from trade_engine.runtime.plugins import FactoryResult
from trade_engine.ledger.store import Ledger
from trade_engine_rs import LedgerStore
from pathlib import Path
import threading

IMPORTS = 1
NOT_CALLABLE = 1

def build(context, config):
    trace = []
    def observe(account):
        trace.append(("clock", context.clock.now_utc().isoformat()))
        state = context.ledger.state(account)
        trace.append(("state", account, str(state.cash)))
        trace.append(("events", tuple(ev.command_id for ev in context.ledger.events(account=account))))
        return str(state.cash), context.ledger.next_seq(), context.ledger.get_meta("seed")
    mode = config.get("mode")
    if mode == "open":
        Ledger(config["other"]).open()
    if mode == "raw":
        LedgerStore(config["other"], config["other"] + ".lock", "synthetic")
    if mode == "thread":
        errors = []
        def open_other():
            try:
                Ledger(config["other"]).open()
            except BaseException as e:
                errors.append((type(e).__name__, str(e)))
        thread = threading.Thread(target=open_other)
        thread.start()
        thread.join(5)
        assert not thread.is_alive()
        return FactoryResult({"errors": errors})
    if mode == "refuse":
        raise ValueError("fake factory refused")
    if mode == "exit":
        raise SystemExit(7)
    if mode == "bad":
        return {"config": {}}
    if mode == "bad_config":
        return FactoryResult([])
    if mode == "bad_strategies":
        return FactoryResult({}, [])
    if mode == "bad_adapters":
        return FactoryResult({}, (), [])
    if mode == "owner":
        return FactoryResult({}, (Ledger(config["other"]),))
    if mode == "adapter_owner":
        return FactoryResult({}, (), {"owner": Ledger(config["other"])})
    observed = observe("A")
    return FactoryResult({"observed": observed, "count": context.ledger.count(),
                          "accounts": context.ledger.accounts(), "imports": IMPORTS,
                          "read_only": not hasattr(context.ledger, "append")
                                       and not hasattr(context.ledger, "conn")
                                       and not hasattr(context.ledger, "close")
                                       and not hasattr(context.clock, "advance_to")},
                         (), {"observe": observe, "trace": trace})
'''


@pytest.fixture
def plugin(tmp_path, monkeypatch):
    name = "t6_fake_plugin"
    path = tmp_path / (name + ".py")
    path.write_text(PLUGIN, encoding="utf-8")
    monkeypatch.delitem(sys.modules, name, raising=False)
    yield name, tmp_path
    sys.modules.pop(name, None)


def seed(ledger, clock):
    ledger.append(Event(account="A", kind=EventKind.CASH_FLOW,
                        payload=CashFlow(Decimal("1.2300"), "deposit", clock.now_utc()),
                        ts_utc=clock.now_utc(), command_id="seed"))
    ledger.set_meta("seed", "synthetic")


def load(plugin, ledger, clock, config):
    name, path = plugin
    return load_factory(name, "build", [path], ledger=ledger, clock=clock, config=config)


def test_factory_lockstep_nested_callbacks_and_state(plugin, tmp_path):
    name, path = plugin
    old_clock, new_clock = FrozenReplay(AT), ReplayClock(AT)
    with Ledger(tmp_path / "old.db") as old, Ledger(tmp_path / "new.db") as new:
        seed(old, old_clock)
        seed(new, new_clock)
        before = sys.path
        native = load(plugin, new, new_clock, {})
        assert sys.path is before
        module = sys.modules[name]
        reference = module.build(SimpleNamespace(ledger=old, clock=old_clock), {})
        for key in ("observed", "count", "accounts", "imports"):
            assert native.config[key] == reference.config[key]
        assert native.config["read_only"] is True
        old.add_listener(lambda event: reference.adapters["observe"](event.account))
        new.add_listener(lambda event: native.adapters["observe"](event.account))
        for index in range(100):
            old_clock.advance_by(0.5)
            new_clock.advance_by(0.5)
            for ledger, clock in ((old, old_clock), (new, new_clock)):
                ledger.append(Event(account="A", kind=EventKind.CASH_FLOW,
                                    payload=CashFlow(Decimal("0.0100"), "deposit", clock.now_utc()),
                                    ts_utc=clock.now_utc(), command_id=f"step-{index}"))
            assert native.adapters["observe"]("A") == reference.adapters["observe"]("A")
            assert native.adapters["trace"] == reference.adapters["trace"]
            assert [event_bytes(ev) for ev in old.events()] == [event_bytes(ev) for ev in new.events()]
            assert canon(old.fold()) == canon(new.fold())
        assert new._native.held
        assert new.count() == 101
        assert not (path / "other.db").exists()
    print("T6_FACTORY_PREFIXES", 100, "callback/event/state exact")


@pytest.mark.parametrize("mode,kind,message", [
    ("open", "RuntimeError", "Plugin factories cannot open an engine writer; use the injected owner view (I4)"),
    ("raw", "RuntimeError", "Plugin factories cannot open an engine writer; use the injected owner view (I4)"),
    ("refuse", "ValueError", "fake factory refused"),
    ("exit", "SystemExit", "7"),
    ("bad", "TypeError", "plugin factory must return FactoryResult"),
    ("bad_config", "TypeError", "FactoryResult.config must be a mapping"),
    ("bad_strategies", "TypeError", "FactoryResult.strategies must be a tuple"),
    ("bad_adapters", "TypeError", "FactoryResult.adapters must be a mapping or None"),
    ("owner", "TypeError", "FactoryResult cannot contain an owning ledger or runner"),
    ("adapter_owner", "TypeError", "FactoryResult cannot contain an owning ledger or runner"),
])
def test_factory_refusals_and_scope_release(plugin, tmp_path, mode, kind, message):
    other = tmp_path / "other.db"
    with Ledger(tmp_path / "owner.db") as owner:
        result = outcome(lambda: load(plugin, owner, ReplayClock(AT), {"mode": mode, "other": str(other)}))
        assert result == ("refusal", kind, message)
        assert not other.exists()
        assert owner._native.held
        assert load(plugin, owner, ReplayClock(AT), {}).config["read_only"] is True
    with Ledger(other) as writer:
        assert writer._native.held


def test_factory_import_side_effect_cannot_open_writer(plugin, tmp_path):
    name, path = plugin
    other = tmp_path / "other.db"
    (path / (name + ".py")).write_text(
        f"from trade_engine.ledger.store import Ledger\nLedger({str(other)!r}).open()\n" + PLUGIN,
        encoding="utf-8",
    )
    with Ledger(tmp_path / "owner.db") as owner:
        assert outcome(lambda: load(plugin, owner, ReplayClock(AT), {})) == (
            "refusal", "RuntimeError",
            "Plugin factories cannot open an engine writer; use the injected owner view (I4)",
        )
        assert not other.exists()


def test_factory_cross_thread_writer_refused(plugin, tmp_path):
    other = tmp_path / "other.db"
    with Ledger(tmp_path / "owner.db") as owner:
        result = load(plugin, owner, ReplayClock(AT), {"mode": "thread", "other": str(other)})
        assert result.config["errors"] == [(
            "RuntimeError", "Plugin factories cannot open an engine writer; use the injected owner view (I4)"
        )]
        assert not other.exists()


@pytest.mark.parametrize("change,kind,message", [
    ({"module": "trade_engine"}, "ValueError", "plugin_module must be a non-reserved plain identifier"),
    ({"module": "a.b"}, "ValueError", "plugin_module must be a non-reserved plain identifier"),
    ({"factory": "a.b"}, "ValueError", "plugin_factory must be a plain identifier"),
    ({"paths": []}, "ValueError", "plugin_paths must not be empty"),
    ({"paths": ["relative"]}, "ValueError", "plugin_path must be an absolute directory"),
    ({"config": []}, "TypeError", "plugin_config must be a mapping"),
    ({"ledger": object()}, "TypeError", "factory ledger must be the open owner Ledger"),
    ({"factory": "NOT_CALLABLE"}, "TypeError", "configured plugin factory is not callable"),
])
def test_factory_input_refusals(plugin, tmp_path, change, kind, message):
    name, path = plugin
    with Ledger(tmp_path / "owner.db") as owner:
        args = dict(module=name, factory="build", plugin_paths=[path],
                    ledger=owner, clock=ReplayClock(AT), config={})
        args.update({("plugin_paths" if key == "paths" else key): value for key, value in change.items()})
        original = sys.path
        assert outcome(lambda: load_factory(**args)) == ("refusal", kind, message)
        assert sys.path is original
        assert load(plugin, owner, ReplayClock(AT), {}).config["read_only"] is True


def test_factory_unopened_owner_and_clock_refusals(plugin, tmp_path):
    with pytest.raises(RuntimeError, match="Ledger is not open"):
        load(plugin, Ledger(tmp_path / "closed.db"), ReplayClock(AT), {})
    with Ledger(tmp_path / "owner.db") as owner:
        assert outcome(lambda: load(plugin, owner, SimpleNamespace(now_utc=None, sleep=None), {})) == (
            "refusal", "TypeError", "factory clock.now_utc must be callable"
        )
        assert outcome(lambda: load(plugin, owner, SimpleNamespace(now_utc=lambda: AT, sleep=None), {})) == (
            "refusal", "TypeError", "factory clock.sleep must be callable"
        )


def test_factory_source_refused_before_execution(plugin, tmp_path, monkeypatch):
    name, path = plugin
    forbidden = tmp_path / "forbidden"
    forbidden.mkdir()
    marker = forbidden / "executed"
    (forbidden / (name + ".py")).write_text(f"open({str(marker)!r}, 'w').close()", encoding="utf-8")
    monkeypatch.syspath_prepend(str(forbidden))
    with Ledger(tmp_path / "owner.db") as owner:
        # A cached module outside the configured paths is also refused.
        spec = importlib.util.spec_from_file_location(name, forbidden / (name + ".py"))
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, name, module)
        assert outcome(lambda: load(plugin, owner, ReplayClock(AT), {})) == (
            "refusal", "RuntimeError", "plugin source is outside configured plugin_paths"
        )
        assert not marker.exists()
        sys.modules.pop(name)
        assert load(plugin, owner, ReplayClock(AT), {}).config["read_only"] is True


def test_factory_uncached_source_refused_before_execution(plugin, tmp_path, monkeypatch):
    name, path = plugin
    (path / (name + ".py")).unlink()
    forbidden = tmp_path / "ambient"
    forbidden.mkdir()
    marker = forbidden / "executed"
    (forbidden / (name + ".py")).write_text(
        f"open({str(marker)!r}, 'w').close()\n" + PLUGIN, encoding="utf-8"
    )
    monkeypatch.syspath_prepend(str(forbidden))
    with Ledger(tmp_path / "owner.db") as owner:
        assert outcome(lambda: load(plugin, owner, ReplayClock(AT), {})) == (
            "refusal", "RuntimeError", "plugin source is outside configured plugin_paths"
        )
        assert not marker.exists()
        (path / (name + ".py")).write_text(PLUGIN, encoding="utf-8")
        importlib.invalidate_caches()
        assert load(plugin, owner, ReplayClock(AT), {}).config["read_only"] is True


@pytest.mark.parametrize("kind", ["missing", "corrupt", "factory"])
def test_factory_import_errors_exact(plugin, tmp_path, kind):
    name, path = plugin
    if kind == "missing":
        (path / (name + ".py")).unlink()
    elif kind == "corrupt":
        (path / (name + ".py")).write_text("def broken(:\n", encoding="utf-8")
    with Ledger(tmp_path / "owner.db") as owner:
        factory = "absent" if kind == "factory" else "build"
        native = outcome(lambda: load_factory(
            name, factory, [path], ledger=owner, clock=ReplayClock(AT), config={}
        ))
        def reference():
            previous = sys.path
            sys.path = [str(path), *previous]
            try:
                module = __import__(name)
                return getattr(module, factory)
            finally:
                sys.path = previous
        assert native == outcome(reference)
        assert native[0] == "refusal"
        (path / (name + ".py")).write_text(PLUGIN, encoding="utf-8")
        importlib.invalidate_caches()
        sys.modules.pop(name, None)
        assert load(plugin, owner, ReplayClock(AT), {}).config["read_only"] is True


def test_factory_refusal_success_tally(plugin, tmp_path):
    tally = Counter()
    with Ledger(tmp_path / "owner.db") as owner:
        for mode in ("open", "raw", "refuse", "exit", "bad", "bad_config",
                     "bad_strategies", "bad_adapters", "owner", "adapter_owner"):
            cfg = {"mode": mode, "other": str(tmp_path / "other.db")}
            tally[mode, outcome(lambda: load(plugin, owner, ReplayClock(AT), cfg))[0]] += 1
            tally[mode, outcome(lambda: load(plugin, owner, ReplayClock(AT), {}))[0]] += 1
        assert tally == Counter({
            (mode, status): 1 for mode in ("open", "raw", "refuse", "exit", "bad", "bad_config",
                                          "bad_strategies", "bad_adapters", "owner", "adapter_owner")
            for status in ("ok", "refusal")
        })
    assert not (tmp_path / "other.db").exists()
    print("T6_FACTORY_REFUSAL_TALLY", dict(tally))


@pytest.mark.parametrize("clock", ["replay", "wall"])
def test_release_native_factory_proof(native_package, clock):
    cfg, _, _, plugins = native_package
    (plugins / "fake_plugin.py").write_text(PLUGIN, encoding="utf-8")
    cfg.update(mode="factory-proof", plugin_factory="build")
    cfg["owner"] = {"ledger_path": str(plugins / "offline.db"), "clock": clock,
                    "initial_time": AT.isoformat() if clock == "replay" else None}
    before = WallClock().now_utc()
    code, result = native_run(native_package)
    assert code == 0, result
    assert result["module_origin"] == "built-in" and result["module_file"] is None
    assert result["builtin_module_count"] == 1
    assert result["result"]["read_only"] is True
    assert result["owner_held"] and result["event_count"] == 0
    if clock == "replay":
        assert result["clock_now"] == AT.isoformat()
    else:
        assert before <= datetime.fromisoformat(result["clock_now"]) <= WallClock().now_utc()
    with Ledger(plugins / "offline.db") as reopened:
        assert reopened._native.held


def test_release_factory_module_provenance(native_package):
    cfg, _, _, plugins = native_package
    source = PLUGIN + PACKAGING_PLUGIN + '''
def build(context, config):
    import sqlite3
    def forbidden(*args, **kwargs):
        raise AssertionError("Python SQLite writer opened")
    sqlite3.connect = forbidden
    result = probe(config)
    result["owner_read"] = str(context.ledger.state("A").cash)
    return FactoryResult(result)
'''
    (plugins / "fake_plugin.py").write_text(source, encoding="utf-8")
    cfg.update(mode="factory-proof", plugin_factory="build")
    cfg["owner"] = {"ledger_path": str(plugins / "offline.db"), "clock": "replay",
                    "initial_time": AT.isoformat()}
    code, report = native_run(native_package)
    assert code == 0, report
    assert report["result"]["modules"] == ["trade_engine_rs"]
    assert report["result"]["owner_read"] == "0"
    assert not any("trade_engine_rs" in path and path.endswith(".pyd")
                   for path in report["result"]["loaded"])
    assert report["builtin_module_count"] == 1
    assert report["owner_held"]


@pytest.mark.parametrize("patch,message", [
    ({}, "factory-proof requires owner config"),
    ({"ledger_path": "relative", "clock": "replay", "initial_time": AT.isoformat()},
     "owner.ledger_path must be an absolute offline path"),
    ({"ledger_path": None, "clock": "bad", "initial_time": None}, "owner.clock must be replay or wall"),
    ({"ledger_path": None, "clock": "replay", "initial_time": None}, "replay clock requires initial_time"),
    ({"ledger_path": None, "clock": "wall", "initial_time": AT.isoformat()},
     "wall clock cannot configure initial_time"),
])
def test_release_owner_config_refusals(native_package, patch, message):
    cfg, _, _, plugins = native_package
    cfg["mode"] = "factory-proof"
    if patch:
        cfg["owner"] = dict(patch)
        if patch["ledger_path"] is None:
            cfg["owner"]["ledger_path"] = str(plugins / "offline.db")
    code, result = native_run(native_package)
    assert code == 2
    assert result == {"error": {"type": "RuntimeConfigError", "message": message}}
    assert not (plugins / "offline.db").exists()
    (plugins / "fake_plugin.py").write_text(PLUGIN, encoding="utf-8")
    cfg["plugin_factory"] = "build"
    cfg["owner"] = {"ledger_path": str(plugins / "offline.db"), "clock": "replay",
                    "initial_time": AT.isoformat()}
    code, result = native_run(native_package)
    assert code == 0, result
    assert result["owner_held"]


def test_release_offline_path_confinement(native_package):
    cfg, path, _, plugins = native_package
    cfg["mode"] = "factory-proof"
    outside = path.parent.parent / "t6-forbidden.db"
    assert not outside.exists()
    cfg["owner"] = {"ledger_path": str(outside), "clock": "replay", "initial_time": AT.isoformat()}
    try:
        code, result = native_run(native_package)
        assert code == 2
        assert result == {"error": {"type": "RuntimeConfigError",
                                    "message": "factory-proof ledger must be inside the offline config directory"}}
        assert not outside.exists()
    finally:
        for suffix in ("", ".lock", "-wal", "-shm"):
            Path(str(outside) + suffix).unlink(missing_ok=True)


@pytest.mark.parametrize("initial", ["invalid", AT.replace(tzinfo=None).isoformat()])
def test_release_invalid_clock_precedes_writer(native_package, initial):
    cfg, _, _, plugins = native_package
    cfg.update(mode="factory-proof", plugin_factory="build")
    cfg["owner"] = {"ledger_path": str(plugins / "offline.db"), "clock": "replay", "initial_time": initial}
    code, result = native_run(native_package)
    assert code == 2
    assert result["error"]["type"] == "ValueError"
    assert not (plugins / "offline.db").exists()

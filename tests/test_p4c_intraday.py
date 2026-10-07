"""T8 synthetic full sessions: exact effects, heartbeat bytes and ledger prefixes."""
from __future__ import annotations

from collections import Counter
from dataclasses import replace
from datetime import date, timedelta
import hashlib
import json
from pathlib import Path
import socket

import pytest
import trade_engine_rs

from trade_engine.intraday import service as P
from trade_engine.sim import _rs

_registry = dict(_rs._KINDS)
try:
    from frozen_p4c.t8 import service as O
finally:
    _rs._KINDS.clear()
    _rs._KINDS.update(_registry)

from trade_engine.ledger import EodRun, Event, EventKind, Ledger
from trade_engine.sim import SnapshotVenue
from test_intraday_service import ACCOUNT, CAL, EARLY, SESSION, Rig, Scripted, at_et, snap, spread, history
from test_p4a_parity import norm
from test_p4c_eod import stored
from test_p4c_embed import package as native_package, run as native_run, PLUGIN as PACKAGING_PLUGIN

DATES = (date(2026, 3, 6), date(2026, 3, 9), date(2026, 7, 2),
         SESSION, EARLY, date(2026, 12, 24))
_COUNTS = Counter()
_TRACES = []


@pytest.fixture(scope="session", autouse=True)
def measurements():
    _COUNTS.clear()
    _TRACES.clear()
    yield
    print("T8_LOCKSTEP", json.dumps({
        "provenance": "synthetic",
        "outer_steps": sum(_COUNTS.values()) // 2,
        "outcomes": {key: count // 2 for key, count in sorted(_COUNTS.items())},
        "event_fold_outbox_meta_prefixes": sum(len(t.prefixes) for t in _TRACES) // 2,
        "ordered_callback_observations": sum(len(t.calls) for t in _TRACES) // 2,
        "heartbeat_observations": sum(len(t.beats) for t in _TRACES) // 2,
    }, sort_keys=True))


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("T8 worlds cannot contact a live venue")
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)


def outcome(call):
    try:
        result = ("ok", norm(call()))
        _COUNTS["ok"] += 1
        return result
    except Exception as error:
        _COUNTS[type(error).__name__] += 1
        cause = error.__cause__
        return ("raise", type(error).__name__, str(error),
                None if cause is None else (type(cause).__name__, str(cause)))


class Trace:
    def __init__(self, rig):
        self.calls, self.prefixes, self.beats = [], [], []
        _TRACES.append(self)
        self.wrap(rig.clock, "clock", ("now_utc", "sleep", "advance_to"))
        self.wrap(rig.venue, "venue", ("connect", "orders", "fills", "positions", "restore",
                                     "submit", "cancel", "replace", "process_snapshot"))
        self.wrap(rig.strategy, "strategy", ("manage_options",))
        self.wrap(rig.risk, "risk", ("evaluate",))
        original = rig.source
        def source(*args, _original=original):
            self.calls.append(("source", norm(args)))
            return _original(*args)
        rig.source = source
        for name in ("append", "extend"):
            original = getattr(rig.ledger, name)
            def write(*args, _original=original, **kwargs):
                value = _original(*args, **kwargs)
                self.prefixes.append(stored(rig.ledger))
                return value
            setattr(rig.ledger, name, write)

    def wrap(self, owner, label, names):
        for name in names:
            original = getattr(owner, name, None)
            if not callable(original):
                continue
            def invoke(*args, _original=original, _name=name, **kwargs):
                self.calls.append((label, _name, norm(args), norm(kwargs)))
                return _original(*args, **kwargs)
            setattr(owner, name, invoke)


class Pair:
    def __init__(self, root, configure=lambda r: None, session=SESSION):
        self.rigs = []
        self.path = root / "heartbeat.json"
        for name, module in (("oracle", O), ("native", P)):
            folder = root / name
            folder.mkdir(parents=True)
            rig = Rig(folder, session=session)
            rig.heartbeat = self.path
            rig.module = module
            rig.beat = None
            rig.risk = __import__("test_intraday_service").Approve()
            configure(rig)
            rig.trace = Trace(rig)
            self.rigs.append(rig)

    def service(self, rig, changes):
        config = rig.module.IntradayConfig(**(vars(rig.config()) | {"option_risk_engine": rig.risk} | changes))
        service = rig.module.IntradayService(rig.ledger, rig.clock, CAL, config, heartbeat_path=rig.heartbeat)
        original = service._write_heartbeat
        def beat(*args, **kwargs):
            rig.trace.calls.append(("heartbeat", norm(args), norm(kwargs)))
            original(*args, **kwargs)
            rig.trace.beats.append(None if not rig.heartbeat.exists() else rig.heartbeat.read_bytes())
        service._write_heartbeat = beat
        return service

    def step(self, action=None, *, changes=None, **run):
        results = []
        for rig in self.rigs:
            self.path.unlink(missing_ok=True)
            if rig.beat is not None:
                self.path.write_bytes(rig.beat)
            service = self.service(rig, changes or {})
            results.append(outcome(lambda: service.run(rig.session, **run) if action is None else action(rig, service)))
            rig.beat = self.path.read_bytes() if self.path.is_file() else None
        assert results[0] == results[1], results
        a, b = self.rigs
        assert a.trace.calls == b.trace.calls
        assert a.trace.prefixes == b.trace.prefixes
        assert a.trace.beats == b.trace.beats
        assert a.beat == b.beat
        assert stored(a.ledger) == stored(b.ledger)
        assert norm(a.strategy.contexts) == norm(b.strategy.contexts)
        return results[0]

    def close(self):
        for rig in self.rigs:
            rig.ledger.close()


def complete_quotes(rig):
    open_at = CAL.session_open(rig.session).astimezone(P.NEW_YORK)
    final = (CAL.session_close(rig.session) - timedelta(minutes=1)).astimezone(P.NEW_YORK)
    rig.minutes((open_at.hour, open_at.minute), (final.hour, final.minute))


def seed_previous(rig):
    previous = CAL.previous_session(rig.session)
    at = CAL.session_close(previous)
    rig.ledger.append(Event(account=ACCOUNT, kind=EventKind.EOD_RUN, ts_utc=at,
        command_id=f"eod:options:{ACCOUNT}:{previous.isoformat()}",
        payload=EodRun(previous, "options", ACCOUNT, 0, at)))


def test_oracle_and_mandatory_bindings():
    oracle = Path(__file__).parent / "frozen_p4c" / "t8" / "service.py"
    assert hashlib.sha256(oracle.read_bytes()).hexdigest() == "03aad7a69bac0fe99e2c805a383d66a2ffd8839ddbdeeed7c224254ea31cef38"
    for name in ("run", "tick", "wait", "advance", "refuse", "emergency", "flatten", "cancel",
                 "restore", "close", "live_guard", "previous_guard", "heartbeat_write", "heartbeat_read"):
        assert callable(getattr(trade_engine_rs, f"intraday_{name}"))


@pytest.mark.parametrize("seed", range(9))
def test_seeded_full_session(tmp_path, seed):
    session = DATES[seed % len(DATES)]
    def configure(rig):
        complete_quotes(rig)
        rig.strategy.actions[at_et(session, 9, 45)] = [spread(f"entry-{seed}", session=session, target="0.20")]
        # A stale interval forces cancellation/flatten and then fresh recovery.
        rig.snapshots = [s for s in rig.snapshots if not (at_et(session, 10, 3) <= s.as_of < at_et(session, 10, 6))]
        rig.strategy.at_or_after[at_et(session, 10, 7)] = [spread(f"recovery-{seed}", session=session)]
    pair = Pair(tmp_path, configure, session)
    try:
        result = pair.step()
        assert result[0] == "ok", result
        assert dict(result[1])["orders_submitted"] == 2
        assert json.loads(pair.rigs[0].beat)["exited"]
        for rig in pair.rigs:
            rig.beat = None
            rig.clock._current_time = CAL.session_open(session)
        assert pair.step()[0] == "ok"
    finally:
        pair.close()


def test_stop_restore_resume(tmp_path):
    def configure(rig):
        complete_quotes(rig)
        rig.strategy.actions[at_et(SESSION, 9, 40)] = [spread("entry", target="0.20")]
    pair = Pair(tmp_path, configure)
    try:
        assert pair.step(stop_at=at_et(SESSION, 9, 43))[0] == "ok"
        for rig in pair.rigs:
            rig.beat = None
            rig.ledger.close()
            rig.ledger = Ledger(rig.path).open()
            rig.venue = SnapshotVenue(ACCOUNT, rig.clock)
            rig.strategy = Scripted(rig.ledger)
            rig.trace = Trace(rig)
        assert pair.step()[0] == "ok"
        assert all(any(c[0:2] == ("venue", "restore") for c in r.trace.calls) for r in pair.rigs)
    finally:
        pair.close()


@pytest.mark.parametrize("mode", ["fresh", "ttl", "exited", "wrong-account", "torn", "previous", "settled"])
def test_admission_refusal_and_counterpart(tmp_path, mode):
    def configure(rig):
        rig.snapshots = [snap(SESSION, 9, 30)]
        beat = dict(account_id=ACCOUNT, session=SESSION.isoformat(),
                    at_utc=rig.clock.now_utc().isoformat(), refusing=False)
        if mode == "ttl":
            beat["at_utc"] = (rig.clock.now_utc() - timedelta(seconds=61)).isoformat()
        if mode == "exited":
            beat["exited"] = True
        if mode == "wrong-account":
            beat["account_id"] = "OTHER"
        if mode in ("fresh", "ttl", "exited", "wrong-account", "torn"):
            rig.beat = b"{" if mode == "torn" else json.dumps(beat).encode()
        if mode == "previous":
            history(rig.ledger, rig.clock)
        if mode == "settled":
            at = CAL.session_close(SESSION)
            rig.ledger.append(Event(account=ACCOUNT, kind=EventKind.EOD_RUN, ts_utc=at,
                command_id=f"eod:options:{ACCOUNT}:{SESSION.isoformat()}",
                payload=EodRun(SESSION, "options", ACCOUNT, 0, at)))
    pair = Pair(tmp_path, configure)
    try:
        result = pair.step(stop_at=at_et(SESSION, 9, 31))
        refused = mode in ("fresh", "wrong-account", "torn", "previous")
        assert result[0] == ("raise" if refused else "ok")
        if refused:
            assert result[1] == "IntradayServiceError"
            for rig in pair.rigs:
                rig.beat = None
                seed_previous(rig)
            assert pair.step(stop_at=at_et(SESSION, 9, 31))[0] == "ok"
    finally:
        pair.close()


class WallLike:
    def __init__(self, start):
        self.current = start
    def now_utc(self):
        return self.current
    def sleep(self, seconds):
        self.current += timedelta(seconds=seconds)


def test_wall_preopen_wait_and_fresh_fetch_clock(tmp_path):
    def configure(rig):
        rig.clock = WallLike(at_et(SESSION, 9, 28))
        rig.venue = SnapshotVenue(ACCOUNT, rig.clock)
        def source(underlying, requested):
            rig.clock.current += timedelta(seconds=1)
            base = snap(SESSION, 9, 30)
            stamp = rig.clock.current
            return replace(base, as_of=stamp, underlying_as_of=stamp,
                quotes=tuple(replace(q, as_of=stamp) for q in base.quotes))
        rig.source = source
    pair = Pair(tmp_path, configure)
    try:
        result = pair.step(stop_at=at_et(SESSION, 9, 31))
        assert result[0] == "ok"
        beats = [json.loads(b) for b in pair.rigs[0].trace.beats]
        assert any("waiting for the open" in b["note"] for b in beats)
        assert any(b["note"] == "ok" for b in beats)
    finally:
        pair.close()


@pytest.mark.parametrize("session", [SESSION, EARLY])
def test_working_entry_cancel_and_late_gate(tmp_path, session):
    end = at_et(session, 11, 30) if session == EARLY else at_et(session, 12, 0)
    before = end - timedelta(minutes=1)
    def configure(rig):
        rig.snapshots = [snap(session, at.hour, at.minute) for at in (before, end, end + timedelta(minutes=1))]
        rig.strategy.actions[before] = [spread("resting", credit="5.00", session=session)]
        original = rig.strategy.manage_options
        def manage(context):
            if context.now > end:
                return [spread("too-late", session=session)]
            return original(context)
        rig.strategy.manage_options = manage
    pair = Pair(tmp_path, configure, session)
    try:
        result = pair.step(start_at=before, stop_at=end + timedelta(minutes=2))
        assert result[0] == "ok", result
        assert dict(result[1])["orders_submitted"] == 1
        assert dict(result[1])["entries_dropped"] >= 1
        from trade_engine.domain.orders import OrderState
        assert all(r.state.orders["resting:entry"].state == OrderState.CANCELLED for r in pair.rigs)
        assert all("too-late:entry" not in r.state.orders for r in pair.rigs)
    finally:
        pair.close()


def test_early_close_sweep_orders_flat_before_manage(tmp_path):
    from datetime import time
    def configure(rig):
        rig.snapshots = [snap(EARLY, 12, minute) for minute in (29, 30, 31)]
        rig.strategy.actions[at_et(EARLY, 12, 29)] = [spread("near-sweep", session=EARLY)]
    pair = Pair(tmp_path, configure, EARLY)
    try:
        result = pair.step(start_at=at_et(EARLY, 12, 29), stop_at=at_et(EARLY, 12, 32),
            changes={"entry_end": time(12, 40), "entry_before_close": timedelta(minutes=30)})
        assert result[0] == "ok", result
        assert dict(result[1])["exit_actions"] == 1
        assert all(not any(p.quantity != 0 for p in r.state.positions.values()) for r in pair.rigs)
    finally:
        pair.close()


@pytest.mark.parametrize("mode", ["strategy", "drift", "no-fresh", "emergency-stale", "flatten-fails", "tuple-value", "connect", "base"])
def test_failure_emergency_and_exit_contract(tmp_path, mode):
    class Death(BaseException):
        pass
    def configure(rig):
        rig.snapshots = [snap(SESSION, 9, 30)]
        def fail(*args, **kwargs):
            raise ValueError("runtime_intraday", "plugin failure") if mode == "tuple-value" else RuntimeError("plugin failure")
        if mode == "base":
            def death(*args, **kwargs):
                raise Death("process death")
            rig.strategy.manage_options = death
        elif mode == "connect":
            rig.venue.connect = fail
        elif mode == "drift":
            rig.venue.positions = lambda: [__import__("types").SimpleNamespace(instrument=spread("x").instrument, quantity=__import__("decimal").Decimal("1"))]
        else:
            rig.strategy.manage_options = fail
        if mode in ("no-fresh", "emergency-stale"):
            original = rig.source
            calls = 0
            def source(*args):
                nonlocal calls
                calls += 1
                if calls > 1:
                    if mode == "no-fresh":
                        raise ValueError("no emergency quote")
                    return snap(SESSION, 9, 30, underlying_quoted=at_et(SESSION, 9, 29))
                return original(*args)
            rig.source = source
    pair = Pair(tmp_path, configure)
    try:
        if mode == "flatten-fails":
            def action(rig, service):
                service._flatten = lambda *args: (_ for _ in ()).throw(RuntimeError("flatten broken"))
                return service.run(SESSION, stop_at=at_et(SESSION, 9, 31))
            result = pair.step(action)
        elif mode == "base":
            def action(rig, service):
                try:
                    service.run(SESSION, stop_at=at_et(SESSION, 9, 31))
                except Death as error:
                    return (type(error).__name__, str(error))
            result = pair.step(action)
            assert result == ("ok", ("Death", "process death"))
            assert not json.loads(pair.rigs[0].beat).get("exited")
            return
        else:
            result = pair.step(stop_at=at_et(SESSION, 9, 31))
        assert result[0] == "raise"
        if mode == "connect":
            assert result[1] == "RuntimeError"
            assert not json.loads(pair.rigs[0].beat)["exited"]
        else:
            assert result[1] == "IntradayServiceAlert"
            beat = json.loads(pair.rigs[0].beat)
            assert beat["alert"] and beat["exited"] and beat["refusing"]
    finally:
        pair.close()


@pytest.mark.parametrize("expected", [0, 1, 2])
def test_existing_caller_exit_classification(tmp_path, expected):
    def configure(rig):
        rig.snapshots = [snap(SESSION, 9, 30)]
        if expected == 1:
            rig.beat = json.dumps(dict(account_id=ACCOUNT, session=SESSION.isoformat(),
                at_utc=rig.clock.now_utc().isoformat(), refusing=False)).encode()
        elif expected == 2:
            def failure(context):
                raise RuntimeError("strategy failed")
            rig.strategy.manage_options = failure
    pair = Pair(tmp_path, configure)
    try:
        def caller(rig, service):
            # Same catch order as zero_dte.main; frozen classes are relocated.
            try:
                service.run(SESSION, stop_at=at_et(SESSION, 9, 31))
            except (O.IntradayServiceAlert, P.IntradayServiceAlert):
                return 2
            except (O.IntradayServiceError, P.IntradayServiceError):
                return 1
            except Exception:
                return 2
            return 0
        assert pair.step(caller) == ("ok", expected)
    finally:
        pair.close()


@pytest.mark.parametrize("phase", ["before-fetch", "after-fetch"])
def test_clock_value_error_boundary(tmp_path, phase):
    pair = Pair(tmp_path, lambda r: setattr(r, "snapshots", [snap(SESSION, 9, 30)]))
    try:
        def action(rig, service):
            original_now = rig.clock.now_utc
            armed = False
            fired = False
            def now():
                nonlocal armed, fired
                if armed and not fired:
                    armed, fired = False, True
                    raise ValueError("clock callback failed")
                return original_now()
            rig.clock.now_utc = now
            if phase == "before-fetch":
                original_tick = service._tick
                def tick(*args):
                    nonlocal armed
                    armed = True
                    return original_tick(*args)
                service._tick = tick
            else:
                original_source = service._config.snapshot_source
                def source(*args):
                    nonlocal armed
                    value = original_source(*args)
                    armed = True
                    return value
                service._config = replace(service._config, snapshot_source=source)
            return service.run(SESSION, stop_at=at_et(SESSION, 9, 31))
        result = pair.step(action)
        if phase == "before-fetch":
            assert result[0:2] == ("raise", "IntradayServiceAlert")
            assert result[3] == ("ValueError", "clock callback failed")
        else:
            assert result[0] == "ok", result
            beat = json.loads(pair.rigs[0].beat)
            assert not beat["alert"] and not beat["exited"] and beat["refusing"]
            assert beat["note"] == "stale quote: clock callback failed"
    finally:
        pair.close()


@pytest.mark.parametrize("mode", ["entry", "close", "pending-close"])
def test_orphan_and_pending_restart(tmp_path, mode):
    from trade_engine.domain.orders import OrderState
    from test_intraday_service import _Crash
    def configure(rig):
        rig.snapshots = [snap(SESSION, 9, 40)]
        rig.strategy.actions[at_et(SESSION, 9, 40)] = [spread("entry")]
    pair = Pair(tmp_path, configure)
    try:
        def crash(rig, service):
            manager = service._router.manager(ACCOUNT)
            if mode == "pending-close":
                owner, name = rig.venue, "submit"
            else:
                owner, name = manager, "_submit"
            original = getattr(owner, name)
            def submit(order):
                id = getattr(order, "order_id", None) or order.venue_order_id
                if (mode == "entry" and id == "entry:entry") or (mode != "entry" and ":close:" in id):
                    raise _Crash("submit")
                return original(order)
            setattr(owner, name, submit)
            try:
                service.run(SESSION, start_at=at_et(SESSION, 9, 40), stop_at=at_et(SESSION, 9, 43))
            except _Crash as error:
                return str(error)
            raise AssertionError("synthetic crash was not reached")
        assert pair.step(crash) == ("ok", "submit")
        for rig in pair.rigs:
            rig.beat = None
            rig.ledger.close()
            rig.ledger = Ledger(rig.path).open()
            rig.venue = SnapshotVenue(ACCOUNT, rig.clock)
            rig.strategy = Scripted(rig.ledger)
            rig.snapshots.append(snap(SESSION, 9, 45))
            rig.clock._current_time = at_et(SESSION, 9, 45)
            rig.trace = Trace(rig)
        assert pair.step(stop_at=at_et(SESSION, 9, 46))[0] == "ok"
        for rig in pair.rigs:
            if mode == "entry":
                assert rig.state.orders["entry:entry"].state == OrderState.CANCELLED
            else:
                assert rig.state.orders["entry:entry:close:1"].state == OrderState.FILLED
    finally:
        pair.close()


def test_unresolved_request_bars_entries_and_heartbeat(tmp_path):
    from trade_engine.ledger import OrderStateChange
    def configure(rig):
        rig.snapshots = [snap(SESSION, 9, 40)]
        rig.strategy.actions[at_et(SESSION, 9, 40)] = [spread("entry", credit="5.00")]
    pair = Pair(tmp_path, configure)
    try:
        assert pair.step(start_at=at_et(SESSION, 9, 40), stop_at=at_et(SESSION, 9, 41))[0] == "ok"
        for rig in pair.rigs:
            rig.ledger.append(Event(account=ACCOUNT, kind=EventKind.ORDER_PENDING, ts_utc=rig.clock.now_utc(),
                command_id="manual:replace-pending", payload=OrderStateChange("entry:entry", "Replace pending")))
            rig.beat = None
            rig.ledger.close()
            rig.ledger = Ledger(rig.path).open()
            rig.venue = SnapshotVenue(ACCOUNT, rig.clock)
            rig.strategy = Scripted(rig.ledger)
            rig.strategy.always = [spread("barred")]
            rig.minutes((9, 42), (9, 44))
            rig.trace = Trace(rig)
        result = pair.step(stop_at=at_et(SESSION, 9, 44))
        assert result[0] == "ok", result
        assert dict(result[1])["entries_dropped"] >= 1
        assert "entries barred" in json.loads(pair.rigs[0].beat)["note"]
        assert all("barred:entry" not in r.state.orders for r in pair.rigs)
    finally:
        pair.close()


def test_optional_heartbeat_is_no_effect(tmp_path):
    pair = Pair(tmp_path)
    try:
        def action(rig, service):
            service._heartbeat_path = None
            service._write_heartbeat(SESSION, rig.module._TickState(), "no file")
            return service._read_heartbeat()
        assert pair.step(action) == ("ok", None)
        assert all(not any(c[0] == "clock" for c in r.trace.calls) for r in pair.rigs)
        assert not pair.path.exists()
    finally:
        pair.close()


@pytest.mark.parametrize("body", [b"{", b"[]", b"null", b"{}", b'{"account_id":"x","session":4}',
                                 b'{"account_id":"x","session":"2026-09-25","at_utc":"bad"}',
                                 b'{"account_id":"x","session":"2026-09-25","at_utc":"2026-09-25"}',
                                 b"\xff", b"\xef\xbb\xbf{}"])
def test_heartbeat_decode_refusals(tmp_path, body):
    pair = Pair(tmp_path)
    try:
        for rig in pair.rigs:
            rig.beat = body
        result = pair.step(lambda r, s: s._read_heartbeat())
        assert result[0] == "raise"
        for rig in pair.rigs:
            rig.beat = json.dumps(dict(account_id=ACCOUNT, session=SESSION.isoformat(),
                at_utc=at_et(SESSION, 9, 30).isoformat(), refusing=False)).encode()
        assert pair.step(lambda r, s: s._read_heartbeat())[0] == "ok"
    finally:
        pair.close()


@pytest.mark.parametrize("mode", ["unicode", "missing-parent", "replace-directory", "read-directory", "barred"])
def test_atomic_heartbeat_and_io_failures(tmp_path, mode):
    pair = Pair(tmp_path)
    try:
        if mode == "missing-parent":
            for rig in pair.rigs:
                rig.heartbeat = tmp_path / "missing" / "beat.json"
        elif mode == "replace-directory":
            pair.path.mkdir()
        elif mode == "read-directory":
            directory = tmp_path / "directory"
            directory.mkdir()
            for rig in pair.rigs:
                rig.heartbeat = directory
        def action(rig, service):
            if mode == "read-directory":
                return service._read_heartbeat()
            state = rig.module._TickState(barred="pending" if mode == "barred" else None)
            service._write_heartbeat(SESSION, state, "quote \u00e9 \U0001f680\nok")
            return service._read_heartbeat()
        if mode == "replace-directory":
            # Keep a directory target intact; bypass step's file-reset helper.
            results = [outcome(lambda r=r: action(r, pair.service(r, {}))) for r in pair.rigs]
            assert results[0] == results[1]
            assert results[0][0] == "raise"
            assert pair.path.is_dir()
        else:
            result = pair.step(action)
            assert result[0] == ("raise" if mode in ("missing-parent", "read-directory") else "ok")
            if result[0] == "ok":
                assert pair.rigs[0].beat is not None
                assert not pair.path.with_name(pair.path.name + ".tmp").exists()
                if mode == "barred":
                    assert json.loads(pair.rigs[0].beat)["refusing"]
    finally:
        pair.close()


def test_native_release_session(native_package):
    config, _, _, plugins = native_package
    extra = r'''
_packaging_probe = probe
def probe(config):
    import socket
    from pathlib import Path
    from datetime import date, datetime, timedelta
    from zoneinfo import ZoneInfo
    from trade_engine.calendar import get_calendar
    from trade_engine.clock import ReplayClock
    from trade_engine.intraday import IntradayConfig, IntradayService
    from trade_engine.ledger import Ledger
    from trade_engine.market_data.chains import ChainSnapshot
    from trade_engine.sim import SnapshotVenue
    from decimal import Decimal
    def forbidden(*args, **kwargs):
        raise AssertionError("native synthetic session cannot reach a live venue")
    socket.create_connection = forbidden
    socket.socket.connect = forbidden
    report = _packaging_probe(config)
    session = date(2026, 11, 27)
    calendar = get_calendar()
    clock = ReplayClock(calendar.session_open(session))
    class Strategy:
        def manage_options(self, context):
            return []
    def source(underlying, now):
        return ChainSnapshot(underlying, now, Decimal("100"), (), None, None, "synthetic", underlying_as_of=now)
    with Ledger(Path(config["root"]) / "synthetic.db") as ledger:
        cfg = IntradayConfig("smoke", "SIM", "SPX", SnapshotVenue("SIM", clock),
            Strategy(), None, source, "eod", tick_seconds=60)
        service = IntradayService(ledger, clock, calendar, cfg,
            heartbeat_path=Path(config["root"]) / "beat.json")
        result = service.run(session, start_at=calendar.session_close(session)-timedelta(minutes=2))
        assert result["snapshots_processed"] == 2, result
        assert len(ledger.events()) == 3
    report["session"] = str(result["session"])
    return report
'''
    (plugins / "fake_plugin.py").write_text(PACKAGING_PLUGIN + extra, encoding="utf-8")
    config["plugin_config"]["root"] = str(plugins)
    code, report = native_run(native_package)
    assert code == 0, report
    assert report["result"]["session"] == EARLY.isoformat()
    assert report["builtin_module_count"] == 1
    assert not any("trade_engine_rs" in p and p.endswith(".pyd") for p in report["result"]["loaded"])

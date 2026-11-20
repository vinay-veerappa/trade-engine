"""REGFEED-STOP-1: registry_feed stops within the budget on Ctrl+C, and a persist survives AV locks.

Fakes only: the BP-3 loopback ntx daemon and the BP-4a fake riskguard. No real token, no service.
"""

# ruff: noqa: F811 - the BP-3 fixtures are re-imported by name, as in test_bp4d_registry_feed.py

from __future__ import annotations

import errno
import logging
import threading
import time
from pathlib import Path

from tests.test_bp3_tradovate_ntx import clock, daemon, make_client  # noqa: F401 - fixtures
from tests.test_bp4a_registry_sink import FakeRg, make_sink, seed, token_file
from trade_engine.sinks import riskguard_registry
from trade_engine.sinks.riskguard_registry import RiskguardRegistrySink
from trade_engine.tradovate.registry_feed import run

SENTINEL = "SENTINEL-RG-TOKEN-do-not-log-regfeed-stop"


class BlockingStream:
    """A `/events` stand-in: `events()` blocks until `wake()` or `close()`, like a quiet socket."""

    def __init__(self) -> None:
        self.opened = threading.Event()
        self._released = threading.Event()
        self.closed_calls = 0

    def events(self):
        self.opened.set()
        self._released.wait(30)
        return iter(())

    def wake(self) -> None:
        self._released.set()

    def close(self) -> None:
        self.closed_calls += 1
        self._released.set()


class SlowRg(FakeRg):
    """A riskguard whose first answer takes `delay` seconds: the sink is mid-post at stop time."""

    def __init__(self, token: str, delay: float) -> None:
        super().__init__(token=token)
        self.delay = delay
        self.entered = threading.Event()

    def post(self, url, body, headers, timeout):
        self.entered.set()
        time.sleep(self.delay)
        return super().post(url, body, headers, timeout)


def env_for(tmp_path: Path, sub: str = "a") -> dict:
    base = tmp_path / sub
    base.mkdir(exist_ok=True)
    return {
        "TE_RISKGUARD_REGISTRY_SINK": "on",
        "TE_RISKGUARD_URL": "http://127.0.0.1:9",
        "TE_RISKGUARD_TOKEN_FILE": str(token_file(base, SENTINEL)),
        "TE_RISKGUARD_OUTBOX_PATH": str(base / "outbox.json"),
    }


def start_run(tmp_path, daemon, clock, stop, *, client=None, http_post=None):
    result: list[int] = []
    make = (lambda: client) if client is not None else (lambda: make_client(daemon))
    t = threading.Thread(
        target=lambda: result.append(
            run(
                env_for(tmp_path),
                clock=clock,
                rand=lambda: 0.0,
                stop_event=stop,
                make_client=make,
                http_post=http_post,
                heartbeat_seconds=0.05,
                pump_tick_seconds=0.05,
            )
        ),
        daemon=True,
    )
    t.start()
    return t, result


def test_stop_wakes_a_blocked_stream_read_within_1_5s(tmp_path, daemon, clock):
    client = make_client(daemon)
    stream = BlockingStream()
    client.open_events = lambda: stream  # the watcher's one socket: quiet, never ends on its own
    stop = threading.Event()
    t, result = start_run(tmp_path, daemon, clock, stop, client=client)
    assert stream.opened.wait(5)
    t0 = time.monotonic()
    stop.set()
    t.join(timeout=1.5)
    assert not t.is_alive(), f"run() still blocked {time.monotonic() - t0:.2f}s after stop"
    assert result == [0]
    assert stream.closed_calls == 1  # the stop path woke the read; the watcher closed its socket


def test_stop_ends_a_real_quiet_events_socket_within_1_5s(tmp_path, daemon, clock):
    # The loopback daemon opens /events, then sends nothing for 15 s: a real blocked socket read.
    daemon.event_scripts.append([lambda: time.sleep(15)])
    stop = threading.Event()
    t, result = start_run(tmp_path, daemon, clock, stop)
    deadline = time.monotonic() + 5
    while daemon.events_opened < 1 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert daemon.events_opened >= 1
    time.sleep(0.2)  # the watcher is now inside readline()
    t0 = time.monotonic()
    stop.set()
    t.join(timeout=1.5)
    assert not t.is_alive(), f"run() still blocked {time.monotonic() - t0:.2f}s after stop"
    assert result == [0]


def test_stop_returns_within_3s_while_a_riskguard_post_is_in_flight(tmp_path, daemon, clock):
    rg = SlowRg(SENTINEL, delay=4.0)
    stop = threading.Event()
    t, result = start_run(tmp_path, daemon, clock, stop, http_post=rg.post)
    assert rg.entered.wait(5)
    t0 = time.monotonic()
    stop.set()
    t.join(timeout=10)
    elapsed = time.monotonic() - t0
    assert not t.is_alive()
    assert elapsed <= 3.0, f"stop took {elapsed:.2f}s"
    assert result == [0]


def test_stop_logs_the_signal_line_and_returns_0(tmp_path, daemon, clock, caplog, monkeypatch):
    # main_loop's contract: a stop from the signal handler logs the quantd-facing line, rc 0.
    import signal

    from trade_engine.tradovate import registry_feed

    handlers: dict[int, object] = {}
    monkeypatch.setattr(signal, "signal", lambda sig, h: handlers.__setitem__(sig, h))
    env = env_for(tmp_path)
    monkeypatch.setattr(registry_feed, "WallClock", lambda: clock)

    def fake_run(environ, **kw):
        handlers[signal.SIGINT](signal.SIGINT, None)  # Ctrl+C arrives while the feed runs
        assert kw["stop_event"].is_set()
        return 0

    monkeypatch.setattr(registry_feed, "run", fake_run)
    caplog.set_level(logging.INFO, logger="trade_engine.tradovate.registry_feed")
    assert registry_feed.main_loop(env) == 0
    assert "registry_feed: stopped (signal)" in caplog.text


# --- persist: one retry after 50 ms on PermissionError -------------------------------------


def _dirty_sink(tmp_path, clock) -> RiskguardRegistrySink:
    sink = make_sink(tmp_path, None, clock)
    seed(sink, 3)
    return sink


def _denied(path, winerror: int = 5) -> PermissionError:
    exc = PermissionError(errno.EACCES, "Access is denied", str(path))
    exc.winerror = winerror
    return exc


def test_persist_retries_once_after_a_permission_error(tmp_path, clock, monkeypatch):
    sink = _dirty_sink(tmp_path, clock)
    real_replace = riskguard_registry.os.replace
    calls: list[int] = []

    def flaky(src, dst):
        calls.append(1)
        if len(calls) == 1:
            raise _denied(dst)
        return real_replace(src, dst)

    monkeypatch.setattr(riskguard_registry.os, "replace", flaky)
    sink._persist()
    assert len(calls) == 2
    assert sink.counters["persist_failures"] == 0
    assert (tmp_path / "outbox.json").exists()
    assert sink._dirty is False


def test_persist_counts_a_failure_and_logs_winerror_after_two_denials(tmp_path, clock, monkeypatch, caplog):
    sink = _dirty_sink(tmp_path, clock)

    def denied(src, dst):
        raise _denied(dst, winerror=32)

    monkeypatch.setattr(riskguard_registry.os, "replace", denied)
    caplog.set_level(logging.WARNING, logger="trade_engine.sinks.riskguard_registry")
    sink._persist()
    assert sink.counters["persist_failures"] == 1
    assert sink._dirty is True
    assert "winerror=32" in caplog.text
    assert "errno=" in caplog.text
    assert str(tmp_path / "outbox.json") in caplog.text
    assert SENTINEL not in caplog.text


def test_persist_base_gets_the_same_one_retry(tmp_path, clock, monkeypatch):
    sink = make_sink(tmp_path, None, clock)
    real_replace = riskguard_registry.os.replace
    calls: list[int] = []

    def flaky(src, dst):
        calls.append(1)
        if len(calls) == 1:
            raise _denied(dst)
        return real_replace(src, dst)

    monkeypatch.setattr(riskguard_registry.os, "replace", flaky)
    sink._seq_base = 7
    sink._persist_base()
    assert len(calls) == 2
    assert sink.counters["persist_failures"] == 0
    assert (tmp_path / "outbox.json.base").exists()

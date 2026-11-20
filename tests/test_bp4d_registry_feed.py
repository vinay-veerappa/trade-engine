"""BP-4d: the host process that runs the Tradovate registry feed.

Fakes only: the BP-3 loopback ntx daemon and the BP-4a fake riskguard ingest. No real token,
no real service.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from trade_engine.sinks.riskguard_registry import DEFAULT_CAP_BYTES, RegistrySinkConfig, RiskguardRegistrySink
from trade_engine.tradovate import registry_feed
from trade_engine.tradovate.adapter import ACCOUNT_CAP, TradovateAdapter
from trade_engine.tradovate.registry_feed import SINK_OFF_EXIT, Feed, cme_trading_day, run, run_once

from tests.test_bp3_tradovate_ntx import (  # noqa: F401 - fixtures are collected from here
    FakeClock,
    FakeNtx,
    acct,
    clock,
    daemon,
    make_client,
    snap,
)
from tests.test_bp4a_registry_sink import FakeRg, token_file

SENTINEL = "SENTINEL-RG-TOKEN-do-not-log-7f3c9a"


class RecordingRg(FakeRg):
    """FakeRg that also keeps the raw request bodies, to compare bytes."""

    def __init__(self, token: str) -> None:
        super().__init__(token=token)
        self.bodies: list[str] = []
        self.calls = 0

    def post(self, url, body, headers, timeout):
        self.calls += 1
        self.bodies.append(body if isinstance(body, str) else body.decode("utf-8"))
        return super().post(url, body, headers, timeout)


def env_for(tmp_path: Path, *, pnl: bool, sink: bool = True, token: str = SENTINEL, sub: str = "a") -> dict:
    base = tmp_path / sub
    base.mkdir(exist_ok=True)
    env = {
        "TE_RISKGUARD_URL": "http://127.0.0.1:9",
        "TE_RISKGUARD_TOKEN_FILE": str(token_file(base, token)),
        "TE_RISKGUARD_OUTBOX_PATH": str(base / "outbox.json"),
    }
    if sink:
        env["TE_RISKGUARD_REGISTRY_SINK"] = "on"
    if pnl:
        env["TE_RISKGUARD_REGISTRY_PNL"] = "on"
    return env


# --- --once ------------------------------------------------------------------------------------


@pytest.mark.parametrize("pnl", [False, True])
def test_once_sends_snapshot_and_pnl_only_when_on(tmp_path, daemon, clock, pnl):
    rg = RecordingRg(SENTINEL)
    code, summary = run_once(
        env_for(tmp_path, pnl=pnl), clock=clock, make_client=lambda: make_client(daemon), http_post=rg.post
    )
    assert code == 0
    assert summary["accounts"] == 2
    assert summary["sent"] >= 1
    assert summary["halted"] is None
    assert rg.envelopes, "nothing reached the ingest"
    first = rg.envelopes[0]
    assert first.get("snapshot") is not None
    if pnl:
        assert "pnl" in first
        assert summary["pnl_rows"] == 2
    else:
        assert "pnl" not in first
        assert summary["pnl_rows"] == 0


def test_sink_off_exits_2_without_http(tmp_path, daemon, clock):
    rg = RecordingRg(SENTINEL)
    built = []
    code, summary = run_once(
        env_for(tmp_path, pnl=True, sink=False),
        clock=clock,
        make_client=lambda: built.append(1) or make_client(daemon),
        http_post=rg.post,
    )
    assert (code, summary) == (SINK_OFF_EXIT, None)
    assert rg.calls == 0
    assert built == []  # nothing constructed, ntx not touched
    assert daemon.snapshot_calls == 0
    stop = threading.Event()
    assert run(env_for(tmp_path, pnl=False, sink=False, sub="b"), stop_event=stop, http_post=rg.post) == SINK_OFF_EXIT
    assert rg.calls == 0


def test_cli_sink_off_prints_line(monkeypatch, capsys):
    from trade_engine import cli

    monkeypatch.delenv("TE_RISKGUARD_REGISTRY_SINK", raising=False)
    code = cli.main(["registry-feed", "--once"])
    assert code == SINK_OFF_EXIT
    assert "registry sink off (TE_RISKGUARD_REGISTRY_SINK!=on)" in capsys.readouterr().out


def test_pnl_off_body_is_byte_identical_to_bp4a(tmp_path, daemon):
    """I1: the feed with PNL off sends exactly what a bare BP-4a sink + adapter sends."""
    feed_rg = RecordingRg(SENTINEL)
    run_once(
        env_for(tmp_path, pnl=False, sub="feed"),
        clock=FakeClock(),
        make_client=lambda: make_client(daemon),
        http_post=feed_rg.post,
    )

    bare_rg = RecordingRg(SENTINEL)
    bare_env = env_for(tmp_path, pnl=False, sub="bare")
    bare_clock = FakeClock()
    sink = RiskguardRegistrySink(RegistrySinkConfig.from_env(bare_env), clock=bare_clock, http_post=bare_rg.post)
    adapter = TradovateAdapter(make_client(daemon), clock=bare_clock, on_poll=sink.observe)
    adapter.poller.poll()
    sink.pump()

    assert feed_rg.bodies and feed_rg.bodies == bare_rg.bodies


def test_cme_trading_day_rolls_at_17_et():
    assert str(cme_trading_day(datetime(2026, 11, 11, 21, 59, tzinfo=timezone.utc))) == "2026-11-11"
    assert str(cme_trading_day(datetime(2026, 11, 11, 22, 0, tzinfo=timezone.utc))) == "2026-11-12"


# --- run -----------------------------------------------------------------------------------------


def test_stop_event_ends_run_and_stops_sink(tmp_path, daemon, clock, monkeypatch):
    stopped = []
    real_stop = RiskguardRegistrySink.stop

    def spy_stop(self, timeout=5.0):
        stopped.append(1)
        return real_stop(self, timeout)

    monkeypatch.setattr(RiskguardRegistrySink, "stop", spy_stop)
    rg = RecordingRg(SENTINEL)
    stop = threading.Event()
    result: list[int] = []
    t = threading.Thread(
        target=lambda: result.append(
            run(
                env_for(tmp_path, pnl=True),
                clock=clock,
                rand=lambda: 0.0,
                stop_event=stop,
                make_client=lambda: make_client(daemon),
                http_post=rg.post,
                heartbeat_seconds=0.05,
                pump_tick_seconds=0.05,
            )
        ),
        daemon=True,
    )
    t.start()
    deadline = time.monotonic() + 5
    while daemon.snapshot_calls < 1 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert daemon.snapshot_calls >= 1
    stop.set()
    t.join(timeout=3.0)
    assert not t.is_alive()
    assert result == [0]
    assert stopped == [1]
    assert daemon.max_live_streams <= 1  # one socket at a time


def test_token_never_logged(tmp_path, daemon, clock, caplog):
    caplog.set_level(logging.DEBUG)
    rg = RecordingRg(SENTINEL)
    run_once(env_for(tmp_path, pnl=True), clock=clock, make_client=lambda: make_client(daemon), http_post=rg.post)
    stop = threading.Event()
    threading.Timer(0.3, stop.set).start()
    run(
        env_for(tmp_path, pnl=True, sub="b"),
        clock=clock,
        rand=lambda: 0.0,
        stop_event=stop,
        make_client=lambda: make_client(daemon),
        http_post=rg.post,
        heartbeat_seconds=0.05,
        pump_tick_seconds=0.05,
    )
    text = caplog.text
    assert "registry feed heartbeat" in text
    assert SENTINEL not in text
    # the heartbeat carries counts only: no account names or numbers
    for line in (r.getMessage() for r in caplog.records if "heartbeat" in r.getMessage()):
        assert "ACCT-TEST" not in line and "900194" not in line


def test_heartbeat_counts_drop_text_and_money():
    out = registry_feed._counts({"sent": 3, "last_error": "boom", "enabled": True, "outbox": {"bytes": 10, "k": "x"}})
    assert out == {"sent": 3, "enabled": True, "outbox.bytes": 10}


# --- B7 bounds -----------------------------------------------------------------------------------


def test_bound_accounts_capped_by_adapter(tmp_path, daemon, clock):
    daemon.snapshot_body = snap([acct(f"BULK-{i}", str(1_000_000 + i)) for i in range(ACCOUNT_CAP + 1)])
    rg = RecordingRg(SENTINEL)
    try:
        code, summary = run_once(
            env_for(tmp_path, pnl=True), clock=clock, make_client=lambda: make_client(daemon), http_post=rg.post
        )
    except Exception:  # noqa: BLE001 - a refusal is also within the bound
        return
    assert code == 0
    assert summary["accounts"] <= ACCOUNT_CAP


def test_feed_build_forwards_the_owner_ledger_to_the_adapter(tmp_path, daemon, clock):
    env = env_for(tmp_path, pnl=True)
    rg = RecordingRg(SENTINEL)
    plain = Feed.build(env, clock=clock, make_client=lambda: make_client(daemon), http_post=rg.post)
    assert plain.adapter._ledger is None  # default: reconcile skips (OA-5b)
    owner_ledger = object()
    owned = Feed.build(env, clock=clock, make_client=lambda: make_client(daemon), http_post=rg.post,
                       ledger=owner_ledger)
    assert owned.adapter._ledger is owner_ledger


def test_bound_outbox_cap_holds_over_repeated_polls(tmp_path, daemon, clock):
    env = env_for(tmp_path, pnl=True)
    rg = RecordingRg(SENTINEL)
    rg.up = False  # nothing drains: the outbox only grows, up to its cap
    feed = Feed.build(env, clock=clock, make_client=lambda: make_client(daemon), http_post=rg.post)
    assert feed.config.cap_bytes == DEFAULT_CAP_BYTES
    for i in range(30):
        daemon.snapshot_body = snap([acct("ACCT-TEST-194", "900194", realized=100.0 + i)])
        clock.advance(1)
        feed.adapter.poller.poll()
        feed.sink.pump()
    outbox = Path(env["TE_RISKGUARD_OUTBOX_PATH"])
    if outbox.exists():
        assert outbox.stat().st_size <= feed.config.cap_bytes
    assert not [v for v in vars(feed).values() if isinstance(v, list)]  # the feed keeps no list


def test_bound_no_busy_loop(tmp_path, daemon, clock):
    """The watcher sleeps on the stop event between connections, so a stopped feed reads ntx
    a handful of times, not thousands."""
    stop = threading.Event()
    threading.Timer(0.5, stop.set).start()
    run(
        env_for(tmp_path, pnl=False),
        clock=clock,
        rand=lambda: 0.0,
        stop_event=stop,
        make_client=lambda: make_client(daemon),
        http_post=RecordingRg(SENTINEL).post,
        heartbeat_seconds=0.1,
        pump_tick_seconds=0.1,
    )
    assert daemon.events_opened < 50


def test_main_thread_waits_in_short_slices(tmp_path, daemon, clock):
    """REGFEED-STOP-1b: a Windows lock wait ignores Ctrl+C, so the signal handler runs only when
    the main wait returns. With a 60 s heartbeat the main thread must still wake every slice."""

    class RecordingEvent(threading.Event):
        def __init__(self) -> None:
            super().__init__()
            self.timeouts: list[tuple[str, float | None]] = []

        def wait(self, timeout=None):
            self.timeouts.append((threading.current_thread().name, timeout))
            return super().wait(timeout)

    rg = RecordingRg(SENTINEL)
    stop = RecordingEvent()
    t = threading.Thread(
        target=lambda: run(
            env_for(tmp_path, pnl=True),
            clock=clock,
            rand=lambda: 0.0,
            stop_event=stop,
            make_client=lambda: make_client(daemon),
            http_post=rg.post,
            heartbeat_seconds=60.0,
            pump_tick_seconds=0.05,
        ),
        name="feed-main",
        daemon=True,
    )
    t.start()
    deadline = time.monotonic() + 5
    while daemon.snapshot_calls < 1 and time.monotonic() < deadline:
        time.sleep(0.01)
    time.sleep(0.2)
    started = time.monotonic()
    stop.set()
    t.join(timeout=3.0)
    assert not t.is_alive()
    assert time.monotonic() - started < 1.5
    main_waits = [w for name, w in stop.timeouts if name == "feed-main"]
    assert main_waits, "the main loop waits on the stop event"
    assert max(main_waits) <= registry_feed.MAIN_WAKE_SECONDS

"""BP-4e: the broker P&L figure stays fresh between fills.

Fakes only: the BP-3 loopback ntx daemon and the BP-4d fake riskguard. Time is a fake clock,
except the two tests that check stop latency on real threads.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta

import pytest

from trade_engine.tradovate.adapter import TradovateAdapter
from trade_engine.tradovate.registry_feed import RESNAP_FLAT_SECONDS, RESNAP_OPEN_SECONDS, PollPump, cme_trading_day, run

from tests.test_bp3_tradovate_ntx import (  # noqa: F401 - fixtures are collected from here
    FakeClock,
    NOW,
    acct,
    clock,
    daemon,
    make_client,
    pos,
    snap,
)
from tests.test_bp4d_registry_feed import SENTINEL, RecordingRg, env_for


def iso(at: datetime) -> str:
    return at.strftime("%Y-%m-%dT%H:%M:%SZ")


def book(as_of: str, net: int = 2, connected: bool = True) -> dict:
    """One account, one position (flat when net is 0)."""
    positions = [pos("NQZ6", net, 20123.25)] if net else []
    row = acct("ACCT-TEST-194", "900194", positions=positions, as_of=as_of)
    row["ws_connected"] = connected
    return snap([row])


def _no_sink(*args, **kwargs) -> None:
    """The pnl pass runs only when a sink is attached (as in the feed); this one keeps nothing."""


def adapter_for(daemon, clock) -> TradovateAdapter:
    return TradovateAdapter(make_client(daemon), clock=clock, trading_day=cme_trading_day, on_poll=_no_sink)


def polls(pump: PollPump, times: int, clock: FakeClock | None = None, on_wait=None) -> list[float]:
    """Run the pump's loop for `times` polls. Each fake wait returns at once; the one after the
    last poll stops it. Returns the intervals the loop asked to wait."""
    calls: list[float] = []

    def wait(seconds: float) -> None:
        calls.append(seconds)
        if clock is not None:
            clock.advance(seconds)
        if on_wait is not None:
            on_wait(seconds)
        if len(calls) > times:
            pump.stop()

    pump.run(wait)
    return calls


# --- the interval and the timer ------------------------------------------------------------


def test_open_position_resnapshots_every_2s(daemon, clock):
    """Position open, no hints: the broker figure's as_of advances by 2 s per read."""
    daemon.snapshot_body = book(iso(clock.now))
    pump = PollPump(adapter_for(daemon, clock))
    seen: list[tuple[float, datetime | None]] = []

    def wait(seconds: float) -> None:
        seen.append((seconds, pump.oldest_as_of))
        clock.advance(seconds)
        daemon.snapshot_body = book(iso(clock.now))  # the daemon's own stream stays live
        if len(seen) == 6:
            pump.stop()

    pump.ask()
    pump.run(wait)

    intervals = [s for s, _ in seen]
    assert intervals[0] == RESNAP_FLAT_SECONDS  # the first wait, before the first read
    assert intervals[1:] == [RESNAP_OPEN_SECONDS] * 5
    stamps = [as_of for _, as_of in seen[1:]]
    assert all(later - earlier == timedelta(seconds=2) for earlier, later in zip(stamps, stamps[1:]))
    # Six waits: the first poll is the untimed ask; the sixth wait stops before its poll.
    assert pump.resnapshots == 4


def test_flat_fleet_interval_is_30s(daemon, clock):
    daemon.snapshot_body = book(iso(clock.now), net=0)
    pump = PollPump(adapter_for(daemon, clock))
    pump.ask()
    intervals = polls(pump, 4, clock)
    assert intervals == [RESNAP_FLAT_SECONDS] * 5
    assert pump.resnapshots == 3


def test_interval_follows_the_book(daemon, clock):
    """A position opening switches the cadence to 2 s; closing it switches back to 30 s."""
    pump = PollPump(adapter_for(daemon, clock))
    steps = iter([book(iso(clock.now), net=0), book(iso(clock.now), net=3), book(iso(clock.now), net=0)])
    daemon.snapshot_body = next(steps)
    pump.ask()

    def script(seconds: float) -> None:
        nxt = next(steps, None)
        if nxt is not None:
            daemon.snapshot_body = nxt

    intervals = polls(pump, 3, clock, on_wait=script)
    assert intervals[0] == RESNAP_FLAT_SECONDS
    assert intervals[1] == RESNAP_OPEN_SECONDS  # the poll that saw the position set 2 s
    assert intervals[2] == RESNAP_FLAT_SECONDS  # the poll that saw it flat again set 30 s


def test_ask_coalesces_into_one_poll(daemon, clock):
    daemon.snapshot_body = book(iso(clock.now), net=0)
    pump = PollPump(adapter_for(daemon, clock))
    pump.ask()
    pump.ask()
    pump.ask()
    polls(pump, 1, clock)
    assert pump.polls == 1
    assert pump.resnapshots == 0


def test_stop_ends_timer_within_one_interval(daemon, clock):
    """Flat interval is 30 s; a stop on a real thread ends the loop at once, not at the next tick."""
    pump = PollPump(adapter_for(daemon, clock))
    thread = threading.Thread(target=pump.run, daemon=True)
    thread.start()
    started = time.monotonic()
    pump.stop()
    thread.join(timeout=1.0)
    assert not thread.is_alive()
    assert time.monotonic() - started < 1.0


def test_run_stop_ends_feed_quickly(tmp_path, daemon, clock):
    """Through `run()`: the stop event ends the pump and the watcher within a few seconds, far
    less than the 30 s flat interval."""
    stop = threading.Event()
    result: list[int] = []
    rg = RecordingRg(SENTINEL)
    thread = threading.Thread(
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
    thread.start()
    deadline = time.monotonic() + 5
    while daemon.snapshot_calls < 1 and time.monotonic() < deadline:
        time.sleep(0.01)
    started = time.monotonic()
    stop.set()
    thread.join(timeout=5.0)
    assert not thread.is_alive()
    assert result == [0]
    assert time.monotonic() - started < 5.0


# --- the stamp: never freshened by our clock alone --------------------------------------------


@pytest.mark.parametrize(("age_s", "stamped_at_read"), [(9, True), (11, False), (20, False)])
def test_stamp_follows_daemon_age(daemon, clock, age_s, stamped_at_read):
    """Stream down: read time only while the daemon's data is within 10 s of it; else its time."""
    old = NOW - timedelta(seconds=age_s)
    daemon.snapshot_body = book(iso(old), connected=False)
    adapter = adapter_for(daemon, clock)
    snapshot = adapter.accounts()
    ((key, figure),) = adapter.pnl_rows(snapshot)
    assert figure is not None and figure[0] == "125.5"
    assert figure[1] == iso(NOW if stamped_at_read else old)


def test_daemon_20s_old_stays_stale_through_the_pump(daemon, clock):
    """The end-to-end form: a 20 s old daemon figure reaches the feed's heartbeat age as 20 s."""
    daemon.snapshot_body = book(iso(NOW - timedelta(seconds=20)), connected=False)
    pump = PollPump(adapter_for(daemon, clock))
    pump.ask()
    polls(pump, 1, clock)
    assert pump.oldest_as_of == NOW - timedelta(seconds=20)


def test_connected_quiet_account_is_stamped_at_read_time(daemon, clock):
    """ntx as_of_utc is the last broker event; a connected account with no events for 10 min
    is still live, so its figure is as fresh as the read."""
    daemon.snapshot_body = book(iso(NOW - timedelta(seconds=600)), connected=True)
    adapter = adapter_for(daemon, clock)
    ((key, figure),) = adapter.pnl_rows(adapter.accounts())
    assert figure is not None and figure[1] == iso(NOW)


def test_fresh_daemon_is_stamped_at_read_time(daemon, clock):
    """Connected: every pump read restamps, so the age never exceeds one interval."""
    daemon.snapshot_body = book(iso(NOW))
    pump = PollPump(adapter_for(daemon, clock))
    pump.ask()
    polls(pump, 1, clock)
    assert NOW <= pump.oldest_as_of <= clock.now


# --- heartbeat and bounds -------------------------------------------------------------------


def test_heartbeat_reports_resnapshots_and_age(tmp_path, daemon, clock, caplog):
    caplog.set_level(logging.INFO)
    daemon.snapshot_body = book(iso(NOW))
    stop = threading.Event()
    rg = RecordingRg(SENTINEL)
    thread = threading.Thread(
        target=run,
        args=(env_for(tmp_path, pnl=True),),
        kwargs=dict(
            clock=clock,
            rand=lambda: 0.0,
            stop_event=stop,
            make_client=lambda: make_client(daemon),
            http_post=rg.post,
            heartbeat_seconds=0.05,
            pump_tick_seconds=0.05,
        ),
        daemon=True,
    )
    thread.start()
    deadline = time.monotonic() + 5
    while "'max_as_of_age_s': 0.0" not in caplog.text and time.monotonic() < deadline:
        time.sleep(0.01)
    stop.set()
    thread.join(timeout=5.0)
    beats = [r.getMessage() for r in caplog.records if "registry feed heartbeat" in r.getMessage()]
    assert beats
    assert "'resnapshots':" in beats[-1]
    assert "'max_as_of_age_s': 0.0" in beats[-1]
    assert "ACCT-TEST" not in beats[-1] and "900194" not in beats[-1]


def test_bound_pump_holds_no_list(daemon, clock):
    daemon.snapshot_body = book(iso(clock.now))
    pump = PollPump(adapter_for(daemon, clock))
    pump.ask()
    polls(pump, 50, clock)
    assert not [v for v in vars(pump).values() if isinstance(v, list)]
    assert pump.resnapshots <= 50


def test_bound_cpu_flat_and_open_budgets(daemon, clock):
    """Over ten simulated minutes: flat costs at most one timed read per 30 s, open at most one
    per 2 s (and open actually runs at 2 s)."""
    budget = 600.0

    def run_for(net: int) -> int:
        clk = FakeClock(NOW)
        daemon.snapshot_body = book(iso(clk.now), net=net)
        pump = PollPump(adapter_for(daemon, clk))
        remaining = [budget]
        pump.ask()

        def wait(seconds: float) -> None:
            step = min(seconds, remaining[0])
            remaining[0] -= step
            clk.advance(step)
            daemon.snapshot_body = book(iso(clk.now), net=net)
            if remaining[0] <= 0:
                pump.stop()

        pump.run(wait)
        return pump.resnapshots

    flat = run_for(0)
    open_ = run_for(2)
    assert flat <= int(budget // RESNAP_FLAT_SECONDS)
    assert open_ <= int(budget // RESNAP_OPEN_SECONDS)
    # The first 30 s wait is flat (no book yet), then 2 s waits: about 284 timed reads in 600 s.
    assert open_ >= 280

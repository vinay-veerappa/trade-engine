"""BP-4d: the host process that runs the Tradovate registry feed.

It wires parts that already exist and adds no logic of its own:

    NtxClient -> TradovateAdapter(on_poll=sink.observe) -> AccountWatcher
                                            \\-> RiskguardRegistrySink (outbox + pump thread)

The adapter's poller calls `sink.observe(snapshot, events, seq[, pnl=...])` on every good read.
Every poll runs on one thread, the `PollPump` (BP-4e): the watcher asks it for a read on connect
and on hints, and it re-reads on its own timer, 2 s while a position is open and 30 s when flat,
so a broker figure's `as_of` stays fresh between fills. `pnl` rows flow only when
`TE_RISKGUARD_REGISTRY_PNL=on`; with it off the adapter gets no trading-day mapping and the
envelope is byte-identical to BP-4a (I1).

B7: memory stays bounded by the adapter's `ACCOUNT_CAP` and the outbox `cap_bytes`; this module
keeps no list of its own. CPU: the watcher blocks on the `/events` stream and its backoff sleep;
the pump blocks on its condition for at most one interval; the main thread blocks on
`stop_event.wait(HEARTBEAT_SECONDS)`. The poller's event ring is `unsendable` (pyo3), so the
pump is the only thread that calls `poll()`.
"""

from __future__ import annotations

import logging
import random
import threading
from collections.abc import Callable, Mapping
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from trade_engine.clock.wall import WallClock
from trade_engine.sinks.riskguard_registry import ENV_SWITCH, RegistrySinkConfig, RiskguardRegistrySink
from trade_engine.tradovate.adapter import AccountWatcher, TradovateAdapter
from trade_engine.tradovate.ntx_client import NtxClient, default_token_path, read_token

log = logging.getLogger(__name__)

HEARTBEAT_SECONDS = 60.0
RESNAP_OPEN_SECONDS = 2.0  # BP-4e: re-read while any account holds a position
RESNAP_FLAT_SECONDS = 30.0  # BP-4e: re-read while every account is flat
SINK_OFF_EXIT = 2
SINK_OFF_LINE = f"registry sink off ({ENV_SWITCH}!=on)"
_ET = ZoneInfo("America/New_York")


def cme_trading_day(at: datetime) -> date:
    """The CME trading day of `at`: the session rolls at 17:00 New York time."""
    return (at.astimezone(_ET) + timedelta(hours=7)).date()


def _counts(stats: Mapping[str, Any]) -> dict[str, Any]:
    """Counts and flags only, flattened one level: never money, ids or text."""
    out: dict[str, Any] = {}
    for key, value in stats.items():
        if isinstance(value, Mapping):
            for sub, inner in value.items():
                if isinstance(inner, (bool, int, float)):
                    out[f"{key}.{sub}"] = inner
        elif isinstance(value, (bool, int, float)):
            out[key] = value
    return out


class PollPump:
    """BP-4e: the one thread that calls `poller.poll()`.

    A read runs when the watcher asks (`ask()`, on connect and on hints) or when the interval
    passes. `ask()` only sets a flag and never blocks on a read. The interval is recomputed after
    each poll: `open_seconds` while any account holds a position, else `flat_seconds`. Memory is
    two flags and four counters; no list.
    """

    def __init__(
        self,
        adapter,
        *,
        open_seconds: float = RESNAP_OPEN_SECONDS,
        flat_seconds: float = RESNAP_FLAT_SECONDS,
    ) -> None:
        self._adapter = adapter
        self._open_seconds = open_seconds
        self._flat_seconds = flat_seconds
        self._cond = threading.Condition()
        self._asked = False
        self._stopped = False
        self._open = False
        self.polls = 0
        self.resnapshots = 0  # timer-driven polls only
        self.accounts = 0
        self.oldest_as_of: datetime | None = None  # the oldest stamped figure of the last poll

    def ask(self) -> None:
        with self._cond:
            self._asked = True
            self._cond.notify()

    def stop(self) -> None:
        with self._cond:
            self._stopped = True
            self._cond.notify()

    def interval(self) -> float:
        return self._open_seconds if self._open else self._flat_seconds

    def _wait_real(self, seconds: float) -> None:
        with self._cond:
            if not (self._asked or self._stopped):
                self._cond.wait(seconds)

    def run(self, wait: Callable[[float], None] | None = None) -> None:
        """Poll until `stop()`. `wait(seconds)` returns on an ask, a stop or the interval."""
        wait = self._wait_real if wait is None else wait
        while True:
            wait(self.interval())
            with self._cond:
                if self._stopped:
                    return
                asked, self._asked = self._asked, False
            self._poll(timed=not asked)

    def _poll(self, *, timed: bool) -> None:
        self._adapter.poller.poll()
        self.polls += 1
        if timed:
            self.resnapshots += 1
        snapshot = self._adapter.poller.last_good
        self.accounts = 0 if snapshot is None else len(snapshot.records)
        self._open = self._adapter.has_open_position()
        self.oldest_as_of = self._adapter.last_figure_as_of


class Feed:
    """The constructed parts. `build` returns None when the sink is switched off."""

    def __init__(self, config: RegistrySinkConfig, sink: RiskguardRegistrySink, client, adapter) -> None:
        self.config = config
        self.sink = sink
        self.client = client
        self.adapter = adapter

    @classmethod
    def build(
        cls,
        environ: Mapping[str, str],
        *,
        clock,
        make_client: Callable[[], Any] | None = None,
        http_post=None,
    ) -> Feed | None:
        config = RegistrySinkConfig.from_env(dict(environ))
        if not config.enabled:
            return None
        kwargs = {} if http_post is None else {"http_post": http_post}
        sink = RiskguardRegistrySink(config, clock=clock, **kwargs)
        if make_client is not None:
            client = make_client()
        else:
            client = NtxClient(read_token(default_token_path(dict(environ))))
        adapter = TradovateAdapter(
            client,
            clock=clock,
            on_poll=sink.observe,
            trading_day=cme_trading_day if config.pnl_enabled else None,
        )
        return cls(config, sink, client, adapter)

    def accounts(self) -> int:
        snapshot = self.adapter.poller.last_good
        return 0 if snapshot is None else len(snapshot.records)

    def pnl_rows(self) -> int:
        snapshot = self.adapter.poller.last_good
        if snapshot is None or not self.config.pnl_enabled:
            return 0
        return sum(1 for _, figure in self.adapter.pnl_rows(snapshot) if figure is not None)


def run_once(
    environ: Mapping[str, str],
    *,
    clock=None,
    make_client: Callable[[], Any] | None = None,
    http_post=None,
) -> tuple[int, dict[str, Any] | None]:
    """One snapshot, one pump. Returns `(exit_code, summary)`; summary is None when off."""
    feed = Feed.build(environ, clock=clock or WallClock(), make_client=make_client, http_post=http_post)
    if feed is None:
        log.warning(SINK_OFF_LINE)
        return SINK_OFF_EXIT, None
    feed.adapter.poller.poll()
    sent = feed.sink.pump()
    summary = {
        "accounts": feed.accounts(),
        "pnl_rows": feed.pnl_rows(),
        "sent": sent,
        "halted": feed.sink.halted,
    }
    return 0, summary


def run(
    environ: Mapping[str, str],
    *,
    clock=None,
    sleep: Callable[[float], None] | None = None,
    rand: Callable[[], float] | None = None,
    stop_event: threading.Event,
    make_client: Callable[[], Any] | None = None,
    http_post=None,
    heartbeat_seconds: float = HEARTBEAT_SECONDS,
    pump_tick_seconds: float = 5.0,
) -> int:
    """Run the feed until `stop_event` is set. Exit 2 at once when the sink is off."""
    clock = clock or WallClock()
    feed = Feed.build(environ, clock=clock, make_client=make_client, http_post=http_post)
    if feed is None:
        log.warning(SINK_OFF_LINE)
        return SINK_OFF_EXIT
    pump = PollPump(feed.adapter)
    watcher = AccountWatcher(
        feed.adapter,
        feed.client,
        clock=clock,
        sleep=sleep if sleep is not None else stop_event.wait,
        rand=rand if rand is not None else random.random,
        read=pump.ask,
    )
    feed.sink.start(tick_seconds=pump_tick_seconds)
    log.info("registry feed started (pnl=%s)", "on" if feed.config.pnl_enabled else "off")

    def watch() -> None:
        try:
            watcher.run(stop_event.is_set)
        except Exception as exc:  # noqa: BLE001 - a crashed watcher ends the process, not silently
            log.error("registry feed watcher stopped (%s)", type(exc).__name__)
            stop_event.set()

    def poll() -> None:
        try:
            pump.run()
        except Exception as exc:  # noqa: BLE001 - a crashed poll thread ends the process, not silently
            log.error("registry feed poll stopped (%s)", type(exc).__name__)
            stop_event.set()

    thread = threading.Thread(target=watch, name="registry-feed-watcher", daemon=True)
    pump_thread = threading.Thread(target=poll, name="registry-feed-poll", daemon=True)
    pump_thread.start()
    thread.start()
    try:
        while not stop_event.wait(heartbeat_seconds):
            oldest = pump.oldest_as_of
            now = clock.now_utc()
            log.info(
                "registry feed heartbeat %s",
                {
                    "snapshots": watcher.snapshots,
                    "connects": watcher.connects,
                    "accounts": pump.accounts,
                    "resnapshots": pump.resnapshots,
                    "max_as_of_age_s": None if oldest is None else round((now - oldest).total_seconds(), 1),
                    **_counts(feed.sink.stats()),
                },
            )
    finally:
        pump.stop()
        feed.sink.stop()
        # The watcher may be blocked on the stream; it is a daemon thread and checks the
        # stop flag after each connection, so a short join is enough.
        thread.join(timeout=1.0)
        pump_thread.join(timeout=1.0)
        log.info("registry feed stopped")
    return 0


def main_loop(environ: Mapping[str, str]) -> int:
    """The CLI entry: SIGINT/SIGTERM set the stop event; exit 0 on a clean stop."""
    import signal

    stop_event = threading.Event()

    def on_signal(signum, frame) -> None:  # noqa: ARG001
        stop_event.set()

    signal.signal(signal.SIGINT, on_signal)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, on_signal)
    return run(environ, clock=WallClock(), sleep=stop_event.wait, rand=random.random, stop_event=stop_event)


__all__ = [
    "HEARTBEAT_SECONDS",
    "RESNAP_FLAT_SECONDS",
    "RESNAP_OPEN_SECONDS",
    "SINK_OFF_EXIT",
    "SINK_OFF_LINE",
    "Feed",
    "PollPump",
    "cme_trading_day",
    "run",
    "run_once",
]

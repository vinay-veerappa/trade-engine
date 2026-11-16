"""BP-4d: the host process that runs the Tradovate registry feed.

It wires parts that already exist and adds no logic of its own:

    NtxClient -> TradovateAdapter(on_poll=sink.observe) -> AccountWatcher
                                            \\-> RiskguardRegistrySink (outbox + pump thread)

The adapter's poller calls `sink.observe(snapshot, events, seq[, pnl=...])` on every good read,
so there is exactly one poll loop (the watcher's). `pnl` rows flow only when
`TE_RISKGUARD_REGISTRY_PNL=on`; with it off the adapter gets no trading-day mapping and the
envelope is byte-identical to BP-4a (I1).

B7: memory stays bounded by the adapter's `ACCOUNT_CAP` and the outbox `cap_bytes`; this module
keeps no list of its own. CPU: the watcher blocks on the `/events` stream and its backoff sleep;
the main thread blocks on `stop_event.wait(HEARTBEAT_SECONDS)`.
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
    watcher = AccountWatcher(
        feed.adapter,
        feed.client,
        clock=clock,
        sleep=sleep if sleep is not None else stop_event.wait,
        rand=rand if rand is not None else random.random,
    )
    feed.sink.start(tick_seconds=pump_tick_seconds)
    log.info("registry feed started (pnl=%s)", "on" if feed.config.pnl_enabled else "off")

    def watch() -> None:
        try:
            watcher.run(stop_event.is_set)
        except Exception as exc:  # noqa: BLE001 - a crashed watcher ends the process, not silently
            log.error("registry feed watcher stopped (%s)", type(exc).__name__)
            stop_event.set()

    thread = threading.Thread(target=watch, name="registry-feed-watcher", daemon=True)
    thread.start()
    try:
        while not stop_event.wait(heartbeat_seconds):
            log.info(
                "registry feed heartbeat %s",
                {
                    "snapshots": watcher.snapshots,
                    "connects": watcher.connects,
                    "accounts": feed.accounts(),
                    **_counts(feed.sink.stats()),
                },
            )
    finally:
        feed.sink.stop()
        # The watcher may be blocked on the stream; it is a daemon thread and checks the
        # stop flag after each connection, so a short join is enough.
        thread.join(timeout=1.0)
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


__all__ = ["HEARTBEAT_SECONDS", "SINK_OFF_EXIT", "SINK_OFF_LINE", "Feed", "cme_trading_day", "run", "run_once"]

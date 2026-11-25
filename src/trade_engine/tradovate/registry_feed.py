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

BP-5c hosts the Schwab accounts adapter here too, on its own switch (`TE_SCHWAB_ACCOUNTS`, off by
default) and its own broker-scoped sink and outbox:

    HubAccountsClient -> SchwabAccountsAdapter(on_poll=schwab_sink.observe) -> SchwabPoller
                        \\-> RiskguardRegistrySink(broker="schwab")

Its one thread reads the hub's two account endpoints when the adapter's 60 s floor is due, hands
the response text to `adapter.feed(...)`, polls, and backs off 30 s -> 300 s (jittered) on a
failed read. With the switch off nothing Schwab is built and no thread starts, and the Tradovate
sink, its outbox and its bytes on the wire are untouched (I1).

B7: memory stays bounded by the adapter's `ACCOUNT_CAP` and the outbox `cap_bytes`; this module
keeps no list of its own. CPU: the watcher blocks on the `/events` stream and its backoff sleep;
the pump blocks on its condition for at most one interval; the Schwab thread blocks on the stop
event for at most one tick (1 s); the main thread blocks on
`stop_event.wait(HEARTBEAT_SECONDS)`. The pump is the only thread that calls `poll()`
(one per broker, never shared) because `PollAndDiff` keeps unlocked Python state (the last
snapshot and the seq counter). The Rust event ring is no longer `unsendable` (TE-RING-1), so
building or dropping a poller on another thread is safe; concurrent `poll()` calls are not.

The method bodies (`SchwabPoller`, `SchwabFeed`, `PollPump`, `Feed` and the module functions)
live in Rust (`trade_engine_rs`); this module keeps the import path, the public names, the
docstrings, the constants, the `__init__` / `__repr__` methods, the `main_loop` signal wiring,
`_accept_console_ctrl_c`, and the tiny thread-body helpers (`_watch`, `_poll`, `_poll_schwab`,
`_listen_hub`).
"""

from __future__ import annotations

import logging
import random
import sys
import threading
from collections.abc import Callable, Mapping
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import trade_engine_rs as rs

from trade_engine.clock.wall import WallClock, monotonic_seconds
from trade_engine.live_ack import live_acknowledged
from trade_engine.schwab.adapter import SchwabAccountsAdapter, SchwabAccountsConfig
from trade_engine.schwab.hub_client import KINDS as HUB_FAILURE_KINDS
from trade_engine.schwab.hub_client import HubAccountsClient, HubReadError, hub_base_url
from trade_engine.schwab.hub_ws import WS_KINDS, HubHintListener
from trade_engine.sinks.riskguard_registry import (
    ENV_SWITCH,
    RegistrySinkConfig,
    RiskguardRegistrySink,
)
from trade_engine.tradovate.adapter import AccountWatcher, TradovateAdapter
from trade_engine.tradovate.ntx_client import NtxClient, default_token_path, read_token

log = logging.getLogger(__name__)

HEARTBEAT_SECONDS = 60.0
STOP_BUDGET_SECONDS = 1.0  # REGFEED-STOP-1: all the stop waits together, one shared deadline
# REGFEED-STOP-1b: on Windows a lock wait is not interrupted by Ctrl+C, so the Python signal
# handler runs only when the main thread's wait returns. The main thread waits in short slices.
MAIN_WAKE_SECONDS = 0.5
RESNAP_OPEN_SECONDS = 2.0  # BP-4e: re-read while any account holds a position
RESNAP_FLAT_SECONDS = 30.0  # BP-4e: re-read while every account is flat
SINK_OFF_EXIT = 2
SINK_OFF_LINE = f"registry sink off ({ENV_SWITCH}!=on)"
# BP-5c: the Schwab half's own outbox and its read-failure backoff.
SCHWAB_OUTBOX_ENV = "TE_SCHWAB_OUTBOX_PATH"
SCHWAB_BROKER = "schwab"
SCHWAB_TICK_SECONDS = 1.0
SCHWAB_BACKOFF_START_SECONDS = 30.0
SCHWAB_BACKOFF_CAP_SECONDS = 300.0
SCHWAB_BACKOFF_JITTER = 0.1  # +-10%, from the injected `rand`
# BP-5e: the hub /ws hint. On by default with TE_SCHWAB_ACCOUNTS; `0` or `off` turns it off.
ENV_HINT = "TE_SCHWAB_ACCOUNTS_HINT"
_ET = ZoneInfo("America/New_York")


def schwab_hint_on(environ: Mapping[str, str]) -> bool:
    """`TE_SCHWAB_ACCOUNTS_HINT`: unset or anything but `0`/`off` is on (BP-5e)."""
    return rs.schwab_hint_on(environ)


def cme_trading_day(at: datetime) -> date:
    """The CME trading day of `at`: the session rolls at 17:00 New York time."""
    return rs.cme_trading_day(at)


def _counts(stats: Mapping[str, Any]) -> dict[str, Any]:
    """Counts and flags only, flattened one level: never money, ids or text."""
    return rs._counts(stats)


# --- BP-5c: the Schwab half ----------------------------------------------------------------


def schwab_outbox_path(environ: Mapping[str, str], tradovate_outbox: str) -> str:
    """`TE_SCHWAB_OUTBOX_PATH`, else the Tradovate outbox's name with `_schwab` appended.

    The default Tradovate name `registry_outbox.json` therefore becomes
    `registry_outbox_schwab.json` in the same directory: two brokers, two files, no shared seq.
    """
    return rs.schwab_outbox_path(environ, tradovate_outbox)


class SchwabPoller(rs.SchwabPollerBase):
    """BP-5c: the one driver that reads the hub and feeds the Schwab adapter.

    `tick(now)` is the whole loop body, so the timer is testable with a fake clock: a read
    happens only when the adapter's 60 s floor is due *and* the read-failure backoff has
    elapsed. A failed read never calls `feed`, never emits an account event (B3) and never
    raises: it is counted by kind and the next attempt moves out 30 s, 60 s, ... capped at
    300 s, jittered by the injected `rand`. Counters only; no list and no response text is
    kept (B7).
    """

    def __init__(
        self,
        adapter,
        client,
        *,
        clock,
        rand: Callable[[], float] | None = None,
        tick_seconds: float = SCHWAB_TICK_SECONDS,
        backoff_start: float = SCHWAB_BACKOFF_START_SECONDS,
        backoff_cap: float = SCHWAB_BACKOFF_CAP_SECONDS,
    ) -> None:
        super().__init__()
        self._adapter = adapter
        self._client = client
        self._clock = clock
        self._rand = rand or random.random
        self.tick_seconds = tick_seconds
        self._backoff_start = backoff_start
        self._backoff_cap = backoff_cap
        self.reads_ok = 0
        self.failures: dict[str, int] = dict.fromkeys(HUB_FAILURE_KINDS, 0)
        self.backoff_seconds = 0.0
        self._consecutive_failures = 0
        self._next_attempt_at: datetime | None = None
        # BP-5e: one flag, set by the hub listener's thread and read by this thread (no queue).
        self._cond = threading.Condition()
        self._asked = False
        self.asks = 0

    def __repr__(self) -> str:
        return (
            f"SchwabPoller(reads_ok={self.reads_ok}, "
            f"failures={sum(self.failures.values())}, backoff_s={round(self.backoff_seconds, 1)})"
        )

    # The method bodies (`accounts`, `ask`, `due`, `tick`, `_next_backoff`, `attempt`,
    # `_wait_for_tick`, `run`, `once`) live in `rs.SchwabPollerBase`.


class SchwabFeed(rs.SchwabFeedBase):
    """BP-5c: the Schwab adapter, its hub client and its own broker-scoped sink.

    Built only when `TE_SCHWAB_ACCOUNTS=on`; with the switch off `build` returns None and the
    Tradovate feed is exactly the BP-4d/4e one (I1).
    """

    def __init__(
        self,
        config: SchwabAccountsConfig,
        adapter,
        sink,
        client,
        *,
        clock,
        rand=None,
        listener: HubHintListener | None = None,
    ) -> None:
        super().__init__()
        self.config = config
        self.adapter = adapter
        self.sink = sink
        self.client = client
        self.clock = clock
        self.poller = SchwabPoller(adapter, client, clock=clock, rand=rand)
        # BP-5e: None unless TE_SCHWAB_ACCOUNTS_HINT is on. Its thread only calls on_accounts_hint.
        self.listener = listener

    # The method bodies (`on_accounts_hint`, `build`, `once`, `counts`) live in
    # `rs.SchwabFeedBase`.


class PollPump(rs.PollPumpBase):
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
        super().__init__()
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

    # The method bodies (`ask`, `stop`, `interval`, `_wait_real`, `run`, `_poll`) live in
    # `rs.PollPumpBase`.


class Feed(rs.FeedBase):
    """The constructed parts. `build` returns None when the sink is switched off."""

    def __init__(
        self,
        config: RegistrySinkConfig,
        sink: RiskguardRegistrySink,
        client,
        adapter,
        *,
        schwab: SchwabFeed | None = None,
    ) -> None:
        super().__init__()
        self.config = config
        self.sink = sink
        self.client = client
        self.adapter = adapter
        self.schwab = schwab  # None unless TE_SCHWAB_ACCOUNTS=on (BP-5c)

    # The method bodies (`build`, `accounts`, `pnl_rows`) live in `rs.FeedBase`.


def run_once(
    environ: Mapping[str, str],
    *,
    clock=None,
    make_client: Callable[[], Any] | None = None,
    http_post=None,
    hub_client=None,
) -> tuple[int, dict[str, Any] | None]:
    """One snapshot, one pump. Returns `(exit_code, summary)`; summary is None when off."""
    return rs.run_once(
        environ,
        clock=clock,
        make_client=make_client,
        http_post=http_post,
        hub_client=hub_client,
    )


def run(
    environ: Mapping[str, str],
    *,
    clock=None,
    sleep: Callable[[float], None] | None = None,
    rand: Callable[[], float] | None = None,
    stop_event: threading.Event,
    make_client: Callable[[], Any] | None = None,
    http_post=None,
    hub_client=None,
    heartbeat_seconds: float = HEARTBEAT_SECONDS,
    pump_tick_seconds: float = 5.0,
    ws_connect=None,
    ledger=None,
) -> int:
    """Run the feed until `stop_event` is set. Exit 2 at once when the sink is off.

    `ledger` is the owner's open ledger (OA-5b): the adapter then reconciles the open brackets and
    PENDING entries from one snapshot on start and on every reconnect. None skips reconcile.
    """
    return rs.run(
        environ,
        clock=clock,
        sleep=sleep,
        rand=rand,
        stop_event=stop_event,
        make_client=make_client,
        http_post=http_post,
        hub_client=hub_client,
        heartbeat_seconds=heartbeat_seconds,
        pump_tick_seconds=pump_tick_seconds,
        ws_connect=ws_connect,
        ledger=ledger,
    )


# --- the thread bodies (the Rust `run` binds these with functools.partial) ------------------


def _watch(watcher: AccountWatcher, stop_event: threading.Event) -> None:
    try:
        watcher.run(stop_event.is_set)
    except Exception as exc:  # noqa: BLE001 - a crashed watcher ends the process, not silently
        log.error("registry feed watcher stopped (%s)", type(exc).__name__)
        stop_event.set()


def _poll(pump: PollPump, stop_event: threading.Event) -> None:
    try:
        pump.run()
    except Exception as exc:  # noqa: BLE001 - a crashed poll thread ends the process, not silently
        log.error("registry feed poll stopped (%s)", type(exc).__name__)
        stop_event.set()


def _poll_schwab(feed: Feed, stop_event: threading.Event) -> None:
    try:
        # No injected wait: the poller wakes at once on a BP-5e hint (ask-aware wait).
        feed.schwab.poller.run(stop_event)
    except Exception as exc:  # noqa: BLE001 - a crashed Schwab thread ends the process
        log.error("registry feed schwab poll stopped (%s)", type(exc).__name__)
        stop_event.set()


def _listen_hub(listener: HubHintListener, stop_event: threading.Event) -> None:
    try:
        listener.run(stop_event)
    except Exception as exc:  # noqa: BLE001 - counted in the log, poll unaffected
        log.error("registry feed hub ws listener stopped (%s)", type(exc).__name__)


def _accept_console_ctrl_c(kernel32=None) -> bool:
    """Clear an inherited "ignore Ctrl+C" flag so quantd's console event 0 reaches SIGINT.

    REGFEED-STOP-1c: under quantd every Ctrl+C stop was forced at 10 s while event 1 (SIGBREAK)
    stopped clean, so the process had inherited the ignore flag. Windows only; never raises.
    """
    if kernel32 is None:
        if sys.platform != "win32":
            return False
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
        except Exception:  # noqa: BLE001  # pragma: no cover - ctypes always present on CPython/Windows
            return False
    try:
        return bool(kernel32.SetConsoleCtrlHandler(None, False))
    except Exception:  # noqa: BLE001 - a console API failure must never stop the feed
        return False


def main_loop(environ: Mapping[str, str], *, ledger=None) -> int:
    """The CLI entry: SIGINT/SIGTERM (and SIGBREAK on Windows) set the stop event; exit 0 on a clean stop."""
    import signal

    stop_event = threading.Event()
    signalled: list[int] = []

    def on_signal(signum, frame) -> None:
        signalled.append(signum)
        stop_event.set()

    _accept_console_ctrl_c()
    signal.signal(signal.SIGINT, on_signal)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, on_signal)
    if hasattr(signal, "SIGBREAK"):  # Windows: quantd's fallback when the Ctrl+C event is refused
        signal.signal(signal.SIGBREAK, on_signal)
    code = run(
        environ,
        clock=WallClock(),
        sleep=stop_event.wait,
        rand=random.random,
        stop_event=stop_event,
        ledger=ledger,
    )
    if signalled:
        log.info("registry_feed: stopped (signal)")
    return code


__all__ = [
    "HEARTBEAT_SECONDS",
    "RESNAP_FLAT_SECONDS",
    "RESNAP_OPEN_SECONDS",
    "SCHWAB_BACKOFF_CAP_SECONDS",
    "SCHWAB_BACKOFF_START_SECONDS",
    "SCHWAB_BROKER",
    "SCHWAB_OUTBOX_ENV",
    "SINK_OFF_EXIT",
    "SINK_OFF_LINE",
    "Feed",
    "PollPump",
    "SchwabFeed",
    "SchwabPoller",
    "cme_trading_day",
    "run",
    "run_once",
    "schwab_hint_on",
    "schwab_outbox_path",
]

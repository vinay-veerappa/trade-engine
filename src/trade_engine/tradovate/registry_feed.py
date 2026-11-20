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
"""

from __future__ import annotations

import logging
import random
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from trade_engine.clock.wall import WallClock
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
    return (environ.get(ENV_HINT) or "on").strip().lower() not in ("0", "off")


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


# --- BP-5c: the Schwab half ----------------------------------------------------------------


def schwab_outbox_path(environ: Mapping[str, str], tradovate_outbox: str) -> str:
    """`TE_SCHWAB_OUTBOX_PATH`, else the Tradovate outbox's name with `_schwab` appended.

    The default Tradovate name `registry_outbox.json` therefore becomes
    `registry_outbox_schwab.json` in the same directory: two brokers, two files, no shared seq.
    """
    explicit = (environ.get(SCHWAB_OUTBOX_ENV) or "").strip()
    if explicit:
        return explicit
    if not tradovate_outbox:
        return "registry_outbox_schwab.json"
    path = Path(tradovate_outbox)
    return str(path.with_name(f"{path.stem}_schwab{path.suffix}"))


class SchwabPoller:
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

    def accounts(self) -> int:
        """How many accounts the last good Schwab snapshot holds.

        Reads the adapter's own `last_good`, never `adapter.poller`: the poller's state is
        unlocked, so only the thread that polls may touch it.
        """
        snapshot = self._adapter.last_good
        return 0 if snapshot is None else len(snapshot.records)

    # -- the timer ------------------------------------------------------------------------

    def ask(self) -> None:
        """BP-5e: an early read is wanted (an ACCOUNTS_CHANGED hint). Sets a flag; never blocks.

        The flag waits out a failure backoff: the read still happens no earlier than
        `_next_attempt_at`. The 60 s floor is not needed while the flag is set.
        """
        with self._cond:
            self._asked = True
            self.asks += 1
            self._cond.notify()

    def due(self, now: datetime) -> bool:
        """True when a read may run now: past the backoff, and either asked or past the 60 s floor."""
        if self._next_attempt_at is not None and now < self._next_attempt_at:
            return False
        with self._cond:
            asked = self._asked
        return asked or self._adapter.next_poll_due(now)

    def tick(self, now: datetime) -> None:
        if self.due(now):
            self.attempt(now)

    def _next_backoff(self) -> float:
        """30 s, 60 s, 120 s, ... capped at 300 s, jittered by at most +-10%."""
        self._consecutive_failures += 1
        base = min(self._backoff_cap, self._backoff_start * (2 ** (self._consecutive_failures - 1)))
        jitter = 1.0 + ((self._rand() * 2.0) - 1.0) * SCHWAB_BACKOFF_JITTER
        return min(self._backoff_cap, base * jitter)

    def attempt(self, now: datetime) -> bool:
        """One hub read pair, then feed + poll. True on a good read; never raises `HubReadError`."""
        with self._cond:
            self._asked = False  # a hint that lands during this read asks again for the next tick
        try:
            numbers, accounts = self._client.read()
        except HubReadError as exc:
            self.failures[exc.kind] = self.failures.get(exc.kind, 0) + 1
            self.backoff_seconds = self._next_backoff()
            self._next_attempt_at = now + timedelta(seconds=self.backoff_seconds)
            log.warning(
                "schwab hub read failed (%s); next read in %ss",
                exc.kind,
                round(self.backoff_seconds, 1),
            )
            return False
        self._consecutive_failures = 0
        self.backoff_seconds = 0.0
        self._next_attempt_at = None
        self._adapter.feed(numbers, accounts)
        self._adapter.poller.poll()
        self.reads_ok += 1
        return True

    def _wait_for_tick(self, stop_event: threading.Event) -> None:
        """Block up to one tick, waking early when a hint is ready (asked and past the backoff).

        A hint inside a backoff does not wake the wait, or the loop would spin until the
        backoff ends; it is read on the first tick after `_next_attempt_at`.
        """
        with self._cond:
            if stop_event.is_set():
                return
            if self._asked and (
                self._next_attempt_at is None or self._clock.now_utc() >= self._next_attempt_at
            ):
                return
            self._cond.wait(self.tick_seconds)

    def run(self, stop_event: threading.Event, wait: Callable[[float], None] | None = None) -> None:
        """Tick every `tick_seconds` until `stop_event`, or sooner on a ready hint. Never busy.

        `wait`, when given, replaces the blocking wait (tests); the hint wake applies only when
        it is None.
        """
        while not stop_event.is_set():
            if wait is None:
                self._wait_for_tick(stop_event)
            else:
                wait(self.tick_seconds)
            if stop_event.is_set():
                return
            self.tick(self._clock.now_utc())

    def once(self) -> bool:
        """`--once`: one attempt at the clock's now, with no timer. True on a good read."""
        return self.attempt(self._clock.now_utc())


class SchwabFeed:
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
        self.config = config
        self.adapter = adapter
        self.sink = sink
        self.client = client
        self.clock = clock
        self.poller = SchwabPoller(adapter, client, clock=clock, rand=rand)
        # BP-5e: None unless TE_SCHWAB_ACCOUNTS_HINT is on. Its thread only calls on_accounts_hint.
        self.listener = listener

    def on_accounts_hint(self) -> bool:
        """BP-5e: one ACCOUNTS_CHANGED frame. Runs on the listener thread; never raises.

        `on_activity` owns the 10 s debounce and touches only the hint counters; `ask` sets the
        poll thread's flag. The message is None, so no frame content reaches the adapter (B5).
        """
        acted = self.adapter.on_activity(self.clock.now_utc(), None)
        if acted:
            self.poller.ask()
        return acted

    @classmethod
    def build(
        cls,
        environ: Mapping[str, str],
        *,
        clock,
        tradovate_outbox: str,
        http_post=None,
        hub_client=None,
        rand: Callable[[], float] | None = None,
        ws_connect=None,
    ) -> SchwabFeed | None:
        config = SchwabAccountsConfig.from_env(dict(environ))
        if not config.enabled:
            return None
        base = RegistrySinkConfig.from_env(dict(environ))
        # The adapter has no pnl source, so this sink never carries the PS-4a list even when
        # the Tradovate one does; its own file keeps the two brokers' sequences apart.
        sink_config = replace(
            base,
            broker=SCHWAB_BROKER,
            outbox_path=schwab_outbox_path(environ, tradovate_outbox),
            pnl_enabled=False,
        )
        kwargs = {} if http_post is None else {"http_post": http_post}
        sink = RiskguardRegistrySink(sink_config, clock=clock, **kwargs)
        client = hub_client if hub_client is not None else HubAccountsClient.from_env(environ)
        adapter = SchwabAccountsAdapter(config, clock=clock, on_poll=sink.observe)
        feed = cls(config, adapter, sink, client, clock=clock, rand=rand)
        if schwab_hint_on(environ):
            # BP-5e: the hub /ws listener. Loopback only (hub_base_url refuses anything else).
            feed.listener = HubHintListener(
                hub_base_url(environ),
                on_hint=feed.on_accounts_hint,
                connect=ws_connect,
                rand=rand,
            )
        return feed

    def once(self) -> int:
        """`--once`: one hub read pair, one poll, one pump. Returns envelopes acked."""
        if not self.poller.once():
            return 0
        return self.sink.pump()

    def counts(self) -> dict[str, Any]:
        """The heartbeat's Schwab keys: counts only, no account numbers, ids or money."""
        out: dict[str, Any] = {
            "schwab_accounts": self.poller.accounts(),
            "schwab_reads_ok": self.poller.reads_ok,
            "schwab_hints_acted": self.adapter.stats.hints,
            "schwab_hints_suppressed": self.adapter.stats.hints_suppressed,
        }
        for kind, count in self.poller.failures.items():
            out[f"schwab_read_fail_{kind}"] = count
        listener = self.listener
        if listener is not None:
            out["schwab_hints_received"] = listener.hints_received
            out["schwab_hint_errors"] = listener.hint_errors
            out["schwab_ws_connects"] = listener.connects
            out["schwab_ws_frames_oversized"] = listener.frames_oversized
            out["schwab_ws_frames_malformed"] = listener.frames_malformed
            for kind in WS_KINDS:
                out[f"schwab_ws_fail_{kind}"] = listener.failures[kind]
        for key, value in _counts(self.sink.stats()).items():
            out[f"schwab_sink.{key}"] = value
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

    def __init__(
        self,
        config: RegistrySinkConfig,
        sink: RiskguardRegistrySink,
        client,
        adapter,
        *,
        schwab: SchwabFeed | None = None,
    ) -> None:
        self.config = config
        self.sink = sink
        self.client = client
        self.adapter = adapter
        self.schwab = schwab  # None unless TE_SCHWAB_ACCOUNTS=on (BP-5c)

    @classmethod
    def build(
        cls,
        environ: Mapping[str, str],
        *,
        clock,
        make_client: Callable[[], Any] | None = None,
        http_post=None,
        hub_client=None,
        rand: Callable[[], float] | None = None,
        ws_connect=None,
        ledger=None,
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
            ledger=ledger,
        )
        schwab = SchwabFeed.build(
            environ,
            clock=clock,
            tradovate_outbox=config.outbox_path,
            http_post=http_post,
            hub_client=hub_client,
            rand=rand,
            ws_connect=ws_connect,
        )
        return cls(config, sink, client, adapter, schwab=schwab)

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
    hub_client=None,
) -> tuple[int, dict[str, Any] | None]:
    """One snapshot, one pump. Returns `(exit_code, summary)`; summary is None when off."""
    feed = Feed.build(
        environ,
        clock=clock or WallClock(),
        make_client=make_client,
        http_post=http_post,
        hub_client=hub_client,
    )
    if feed is None:
        log.warning(SINK_OFF_LINE)
        return SINK_OFF_EXIT, None
    feed.adapter.poller.poll()
    sent = feed.sink.pump()
    summary: dict[str, Any] = {
        "accounts": feed.accounts(),
        "pnl_rows": feed.pnl_rows(),
        "sent": sent,
        "halted": feed.sink.halted,
    }
    if feed.schwab is not None:
        # BP-5c: one Schwab read pair, one poll, one pump, after the Tradovate bytes (I1).
        schwab_sent = feed.schwab.once()
        summary["schwab_accounts"] = feed.schwab.poller.accounts()
        summary["schwab_sent"] = schwab_sent
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
    clock = clock or WallClock()
    feed = Feed.build(
        environ,
        clock=clock,
        make_client=make_client,
        http_post=http_post,
        hub_client=hub_client,
        rand=rand,
        ws_connect=ws_connect,
        ledger=ledger,
    )
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
    log.info(
        "registry feed started (pnl=%s, schwab=%s)",
        "on" if feed.config.pnl_enabled else "off",
        "on" if feed.schwab is not None else "off",
    )

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
    # BP-5c: one Schwab thread, same stop event, only when the switch built the half.
    schwab_thread = None
    hint_thread = None
    if feed.schwab is not None:
        feed.schwab.sink.start(tick_seconds=pump_tick_seconds)

        def poll_schwab() -> None:
            try:
                # No injected wait: the poller wakes at once on a BP-5e hint (ask-aware wait).
                feed.schwab.poller.run(stop_event)
            except Exception as exc:  # noqa: BLE001 - a crashed Schwab thread ends the process
                log.error("registry feed schwab poll stopped (%s)", type(exc).__name__)
                stop_event.set()

        schwab_thread = threading.Thread(
            target=poll_schwab, name="registry-feed-schwab", daemon=True
        )
        schwab_thread.start()
        if feed.schwab.listener is not None:
            # BP-5e: the hint listener. It never sets the stop event: a dead listener only
            # means the 60 s poll runs alone (the poll is the contract).
            listener = feed.schwab.listener

            def listen_hub() -> None:
                try:
                    listener.run(stop_event)
                except Exception as exc:  # noqa: BLE001 - counted in the log, poll unaffected
                    log.error("registry feed hub ws listener stopped (%s)", type(exc).__name__)

            hint_thread = threading.Thread(
                target=listen_hub, name="registry-feed-hub-ws", daemon=True
            )
            hint_thread.start()
    try:
        next_beat = time.monotonic() + heartbeat_seconds
        while not stop_event.wait(min(MAIN_WAKE_SECONDS, heartbeat_seconds)):
            if time.monotonic() < next_beat:
                continue
            next_beat = time.monotonic() + heartbeat_seconds
            oldest = pump.oldest_as_of
            now = clock.now_utc()
            payload: dict[str, Any] = {
                "snapshots": watcher.snapshots,
                "connects": watcher.connects,
                "accounts": pump.accounts,
                "resnapshots": pump.resnapshots,
                "max_as_of_age_s": None if oldest is None else round((now - oldest).total_seconds(), 1),
                **_counts(feed.sink.stats()),
            }
            if feed.schwab is not None:
                payload.update(feed.schwab.counts())
            log.info("registry feed heartbeat %s", payload)
    finally:
        # REGFEED-STOP-1: every stop wait shares one deadline (STOP_BUDGET_SECONDS). A thread
        # still busy at the deadline is a daemon and is left to finish; exit does not wait on it.
        deadline = time.monotonic() + STOP_BUDGET_SECONDS

        def left() -> float:
            return max(0.0, deadline - time.monotonic())

        watcher.wake()  # a read blocked on /events returns now (the stop flag is already set)
        pump.stop()
        feed.sink.stop(timeout=left())
        if feed.schwab is not None:
            feed.schwab.sink.stop(timeout=left())
        for worker in (thread, pump_thread, schwab_thread, hint_thread):
            if worker is not None:
                worker.join(timeout=left())
        log.info("registry feed stopped")
    return 0


def main_loop(environ: Mapping[str, str], *, ledger=None) -> int:
    """The CLI entry: SIGINT/SIGTERM (and SIGBREAK on Windows) set the stop event; exit 0 on a clean stop."""
    import signal

    stop_event = threading.Event()
    signalled: list[int] = []

    def on_signal(signum, frame) -> None:
        signalled.append(signum)
        stop_event.set()

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

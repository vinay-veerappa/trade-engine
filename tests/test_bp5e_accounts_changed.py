"""BP-5e: an ACCOUNTS_CHANGED frame on the hub's /ws triggers an early Schwab re-read.

Fakes only: a fake clock, a fake hub `/request` (BP-5c's `FakeHub`), a fake riskguard ingest and a
fake websocket socket that speaks the server side of RFC 6455 for the handshake and text frames.
Nothing here dials the real hub (port 8081), Schwab or riskguard.

Enforcing tests, by rule:
- a hint leads to exactly one early read; a second hint inside 10 s is suppressed:
  `test_an_accounts_changed_hint_*`, `test_a_hint_inside_ten_seconds_*`, `test_the_wire_frame_*`;
- a hint inside a failure backoff does not read before `_next_attempt_at`, and does not spin:
  `test_a_hint_during_a_failure_backoff_*`, `test_a_ready_hint_wake_*`;
- no hints keeps the 60 s floor: `test_no_hints_*`;
- other services call nothing; malformed and oversized frames are counted, never raised:
  `test_other_services_*`, `test_a_malformed_*`, `test_an_oversized_*` (B7 cap at 1 MiB);
- a listener connect failure backs off 5 s, 10 s, ... capped at 120 s, and the poll keeps running:
  `test_a_listener_connect_failure_*`, `test_the_backoff_*`, `test_the_poll_keeps_running_*`;
- the switches: `test_the_hint_switch_off_*`, `test_with_the_schwab_switch_off_*`;
- the stop event ends the listener thread within one second: `test_the_stop_event_*`.
"""

from __future__ import annotations

import json
import logging
import re
import struct
import threading
import time

import pytest

from tests.test_bp3_tradovate_ntx import (  # noqa: F401 - fixtures are collected from here
    FakeClock,
    clock,
    daemon,
    make_client,
)
from tests.test_bp4d_registry_feed import RecordingRg
from tests.test_bp5c_schwab_host import SENTINEL, FakeHub, env_for
from trade_engine.schwab.hub_ws import (
    ACCOUNTS_CHANGED,
    MAX_FRAME_BYTES,
    HubHintListener,
    _accept_key,
)
from trade_engine.tradovate.registry_feed import (
    ENV_HINT,
    Feed,
    SchwabPoller,
    run,
    schwab_hint_on,
)

HUB_URL = "http://127.0.0.1:8081"


# --- fakes -----------------------------------------------------------------------------------


def server_frame(payload: bytes, opcode: int = 0x1, fin: bool = True) -> bytes:
    """An unmasked server frame (RFC 6455 section 5.2): the server never masks."""
    b0 = (0x80 if fin else 0) | opcode
    n = len(payload)
    if n < 126:
        head = bytes([b0, n])
    elif n < 65536:
        head = bytes([b0, 126]) + struct.pack(">H", n)
    else:
        head = bytes([b0, 127]) + struct.pack(">Q", n)
    return head + payload


def hub_frame(service: str) -> bytes:
    """The hub's envelope for one `/ws` event (schwab_hub.py `_handle_stream_event`)."""
    body = {
        "timestamp": "2026-11-16T12:00:00+00:00",
        "data": {"service": service, "content": [{"changed": True}]},
        "provider": "SchwabStreamer",
    }
    return server_frame(json.dumps(body).encode())


class FakeWs:
    """A socket-like peer: answers the handshake, then delivers `frames`, then closes or stalls."""

    def __init__(self, frames: tuple[bytes, ...] = (), *, stall: bool = False) -> None:
        self._in = bytearray()
        self._frames = frames
        self._stall = stall
        self._handshaken = False
        self.sent = bytearray()
        self.timeouts = 0
        self.closed = False

    def settimeout(self, seconds: float) -> None:
        pass

    def sendall(self, data: bytes) -> None:
        self.sent += data
        if not self._handshaken and b"Sec-WebSocket-Key:" in data:
            self._handshaken = True
            key = re.search(rb"Sec-WebSocket-Key: (\S+)", data).group(1).decode()
            head = (
                "HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Accept: {_accept_key(key)}\r\n\r\n"
            )
            self._in += head.encode("ascii")
            for frame in self._frames:
                self._in += frame

    def recv(self, n: int) -> bytes:
        if not self._in:
            if self._stall:
                self.timeouts += 1
                raise TimeoutError("the hub is silent")
            return b""  # the hub closed the socket
        out = bytes(self._in[:n])
        del self._in[:n]
        return out

    def close(self) -> None:
        self.closed = True


class FakeNet:
    """The injected `connect`: each call pops the next scripted socket or raises the next error."""

    def __init__(self, *script) -> None:
        self._script = list(script)
        self.calls: list[tuple[str, int]] = []

    def __call__(self, host: str, port: int, timeout: float):
        self.calls.append((host, port))
        if not self._script:
            raise ConnectionRefusedError("no hub")
        step = self._script.pop(0)
        if isinstance(step, BaseException):
            raise step
        return step


class WaitRecorder:
    """Stands in for `stop_event.wait` in the listener: records each backoff and reports stop."""

    def __init__(self, stop_after: int) -> None:
        self.delays: list[float] = []
        self._stop_after = stop_after

    def __call__(self, seconds: float) -> bool:
        self.delays.append(seconds)
        return len(self.delays) >= self._stop_after


def make_feed(tmp_path, clock, *, hub=None, ws_connect=None, hint: bool = True, sub: str = "a"):
    env = env_for(tmp_path, schwab=True, sub=sub)
    if hint:
        env[ENV_HINT] = "on"
    else:
        env[ENV_HINT] = "0"
    feed = Feed.build(
        env,
        clock=clock,
        make_client=lambda: None,
        http_post=RecordingRg(SENTINEL).post,
        hub_client=(hub or FakeHub()).client(),
        rand=lambda: 0.5,
        ws_connect=ws_connect,
    )
    assert feed is not None and feed.schwab is not None
    return feed


# --- the hint and the early read -------------------------------------------------------------


def test_an_accounts_changed_hint_leads_to_exactly_one_early_read(tmp_path, clock):
    feed = make_feed(tmp_path, clock)
    schwab = feed.schwab
    schwab.poller.tick(clock.now)  # the first read: no attempt yet, so it is due
    assert schwab.poller.reads_ok == 1

    clock.advance(1)  # well inside the 60 s floor
    assert schwab.on_accounts_hint() is True
    schwab.poller.tick(clock.now)
    assert schwab.poller.reads_ok == 2, "the hint did not lead to an early read"

    schwab.poller.tick(clock.now)  # the hint is spent: no second read
    assert schwab.poller.reads_ok == 2


def test_a_hint_inside_ten_seconds_is_suppressed_and_a_later_one_reads_again(tmp_path, clock):
    feed = make_feed(tmp_path, clock)
    schwab = feed.schwab
    schwab.poller.tick(clock.now)
    clock.advance(1)
    assert schwab.on_accounts_hint() is True
    schwab.poller.tick(clock.now)
    assert schwab.poller.reads_ok == 2

    clock.advance(2)  # 3 s after the acted hint: inside the 10 s window
    assert schwab.on_accounts_hint() is False
    schwab.poller.tick(clock.now)
    assert schwab.poller.reads_ok == 2, "a suppressed hint caused a read"
    assert schwab.adapter.stats.hints_suppressed == 1

    clock.advance(10)  # 13 s after the acted hint: the window has passed
    assert schwab.on_accounts_hint() is True
    schwab.poller.tick(clock.now)
    assert schwab.poller.reads_ok == 3


def test_the_wire_frame_leads_to_one_early_read(tmp_path, clock):
    """Bytes on the socket, through the listener, into the poller: one read, no content used."""
    hub = FakeHub()
    net = FakeNet(FakeWs((hub_frame(ACCOUNTS_CHANGED), hub_frame("LEVELONE_FUTURES"))))
    feed = make_feed(tmp_path, clock, hub=hub, ws_connect=net)
    schwab = feed.schwab
    schwab.poller.tick(clock.now)
    assert schwab.poller.reads_ok == 1

    clock.advance(1)
    stop = threading.Event()
    schwab.listener.run(stop, wait=lambda s: True)  # one connection, then the fake closes
    assert schwab.listener.hints_received == 1
    assert schwab.adapter.stats.hints == 1
    schwab.poller.tick(clock.now)
    assert schwab.poller.reads_ok == 2


def test_a_hint_during_a_failure_backoff_does_not_read_before_next_attempt(tmp_path, clock):
    hub = FakeHub()
    feed = make_feed(tmp_path, clock, hub=hub)
    schwab = feed.schwab
    hub.failure = "timeout"
    schwab.poller.tick(clock.now)  # fails: backoff 30 s (jitter 0 with rand 0.5)
    assert schwab.poller.failures["timeout"] == 1
    calls_after_failure = hub.calls
    hub.failure = None

    clock.advance(5)
    assert schwab.on_accounts_hint() is True
    schwab.poller.tick(clock.now)
    assert hub.calls == calls_after_failure, "a hint read before the backoff ended"

    clock.advance(26)  # 31 s after the failure
    schwab.poller.tick(clock.now)
    assert schwab.poller.reads_ok == 1
    assert hub.calls > calls_after_failure


def test_a_ready_hint_wake_does_not_spin_during_a_backoff(tmp_path, clock):
    """A hint parked in a backoff must not turn the poll loop into a busy loop."""
    hub = FakeHub()
    feed = make_feed(tmp_path, clock, hub=hub)
    schwab = feed.schwab
    hub.failure = "unreachable"
    schwab.poller.tick(clock.now)  # the backoff now runs to clock.now + 30 s
    poller = SchwabPoller(
        schwab.adapter, schwab.client, clock=clock, rand=lambda: 0.5, tick_seconds=0.05
    )
    poller._next_attempt_at = schwab.poller._next_attempt_at
    ticks: list[object] = []
    real_tick = poller.tick
    poller.tick = lambda now: (ticks.append(now), real_tick(now))[1]
    stop = threading.Event()
    worker = threading.Thread(target=poller.run, args=(stop,), daemon=True)
    worker.start()
    poller.ask()
    time.sleep(0.5)
    stop.set()
    worker.join(timeout=2.0)
    assert not worker.is_alive()
    assert len(ticks) <= 25, f"{len(ticks)} ticks in 0.5 s: the wait spun"
    assert poller.reads_ok == 0


def test_no_hints_keeps_the_sixty_second_floor(tmp_path, clock):
    feed = make_feed(tmp_path, clock)
    schwab = feed.schwab
    schwab.poller.tick(clock.now)
    clock.advance(59)
    schwab.poller.tick(clock.now)
    assert schwab.poller.reads_ok == 1
    clock.advance(1)
    schwab.poller.tick(clock.now)
    assert schwab.poller.reads_ok == 2


# --- the listener: what it reads and what it refuses ----------------------------------------


def _listener(tmp_path, clock, on_hint):
    return HubHintListener(HUB_URL, on_hint=on_hint, connect=FakeNet(), rand=lambda: 0.5)


def test_other_services_call_nothing(tmp_path, clock):
    calls: list[int] = []
    listener = _listener(tmp_path, clock, lambda: calls.append(1))
    for service in ("LEVELONE_FUTURES", "ACCT_ACTIVITY", "CHART_FUTURES", "NASDAQ_BOOK"):
        listener._handle(json.dumps({"data": {"service": service}}).encode())
    listener._handle(json.dumps({"service": "LEVELONE_FUTURES"}).encode())
    listener._handle(b'{"data": {"content": [{"changed": true}]}}')  # no service at all
    assert calls == []
    assert listener.hints_received == 0
    assert listener.frames_malformed == 0


def test_a_hub_frame_with_the_service_name_is_a_hint(tmp_path, clock):
    calls: list[int] = []
    listener = _listener(tmp_path, clock, lambda: calls.append(1))
    listener._handle(json.dumps({"data": {"service": ACCOUNTS_CHANGED}}).encode())
    assert calls == [1]
    assert listener.hints_received == 1


def test_a_malformed_json_frame_is_counted_and_ignored(tmp_path, clock):
    calls: list[int] = []
    listener = _listener(tmp_path, clock, lambda: calls.append(1))
    listener._handle(b'{"data": {"service": "ACCOUNTS_CHANGED", broken')
    listener._handle(b'["ACCOUNTS_CHANGED"]')  # JSON, not an object: also malformed
    assert calls == []
    assert listener.frames_malformed == 2


def test_a_raising_hint_callback_is_counted_never_raised(tmp_path, clock):
    def boom() -> None:
        raise RuntimeError("callback bug")

    listener = _listener(tmp_path, clock, boom)
    listener._handle(json.dumps({"data": {"service": ACCOUNTS_CHANGED}}).encode())
    assert listener.hint_errors == 1 and listener.hints_received == 1


def test_an_oversized_frame_is_dropped_and_counted_and_the_next_frame_still_counts(
    tmp_path, clock
):
    """B7: one message is at most 1 MiB. A bigger one is discarded in chunks and counted."""
    big = b'{"data": {"service": "LEVELONE_FUTURES", "pad": "' + b"x" * MAX_FRAME_BYTES + b'"}}'
    assert len(big) > MAX_FRAME_BYTES
    calls: list[int] = []
    net = FakeNet(FakeWs((server_frame(big), hub_frame(ACCOUNTS_CHANGED))))
    listener = HubHintListener(HUB_URL, on_hint=lambda: calls.append(1), connect=net, rand=lambda: 0.5)
    listener.run(threading.Event(), wait=lambda s: True)
    assert listener.frames_oversized == 1
    assert calls == [1], "the frame after the oversized one was not read"


def test_a_frame_exactly_at_the_cap_is_read(tmp_path, clock):
    """The cap is inclusive: a message of exactly MAX_FRAME_BYTES is still a message."""
    head = b'{"data": {"service": "LEVELONE_FUTURES", "pad": "'
    tail = b'"}}'
    exact = head + b"x" * (MAX_FRAME_BYTES - len(head) - len(tail)) + tail
    assert len(exact) == MAX_FRAME_BYTES
    net = FakeNet(FakeWs((server_frame(exact),)))
    listener = HubHintListener(HUB_URL, on_hint=lambda: None, connect=net, rand=lambda: 0.5)
    listener.run(threading.Event(), wait=lambda s: True)
    assert listener.frames_oversized == 0


# --- the listener: reconnects, backoff and the poll ----------------------------------------


def test_a_listener_connect_failure_reconnects_with_backoff(tmp_path, clock):
    net = FakeNet(
        ConnectionRefusedError("down"),
        ConnectionRefusedError("down"),
        ConnectionRefusedError("down"),
        FakeWs(),  # the fourth connect is accepted and closed at once
    )
    listener = HubHintListener(HUB_URL, on_hint=lambda: None, connect=net, rand=lambda: 0.5)
    waits = WaitRecorder(stop_after=4)
    listener.run(threading.Event(), wait=waits)
    assert listener.failures["refused"] == 3
    assert listener.failures["closed"] == 1
    assert listener.connects == 1
    assert waits.delays[:3] == [5.0, 10.0, 20.0]


def test_the_backoff_doubles_from_five_seconds_and_caps_at_120(tmp_path, clock):
    listener = HubHintListener(HUB_URL, on_hint=lambda: None, connect=FakeNet(), rand=lambda: 0.5)
    delays = [listener._next_delay() for _ in range(8)]
    assert delays == [5.0, 10.0, 20.0, 40.0, 80.0, 120.0, 120.0, 120.0]


def test_the_backoff_jitter_stays_within_ten_percent(tmp_path, clock):
    low = HubHintListener(HUB_URL, on_hint=lambda: None, connect=FakeNet(), rand=lambda: 0.0)
    high = HubHintListener(HUB_URL, on_hint=lambda: None, connect=FakeNet(), rand=lambda: 0.999999)
    assert 4.49 <= low._next_delay() <= 4.51
    assert 5.49 <= high._next_delay() <= 5.5


def test_the_poll_keeps_running_while_the_listener_is_refused(tmp_path, daemon, clock, caplog):
    """The poll is the contract: a hub /ws that refuses every connect costs the reads nothing."""
    caplog.set_level(logging.INFO, logger="trade_engine.tradovate.registry_feed")
    hub = FakeHub()
    rg = RecordingRg(SENTINEL)
    env = env_for(tmp_path, schwab=True, sub="poll")
    env[ENV_HINT] = "on"
    stop = threading.Event()
    net = FakeNet()  # every connect is refused
    result: list[int] = []
    thread = threading.Thread(
        target=lambda: result.append(
            run(
                env,
                clock=clock,
                rand=lambda: 0.5,
                stop_event=stop,
                make_client=lambda: make_client(daemon),
                http_post=rg.post,
                hub_client=hub.client(),
                heartbeat_seconds=0.05,
                pump_tick_seconds=0.05,
                ws_connect=net,
            )
        ),
        daemon=True,
    )
    thread.start()
    deadline = time.monotonic() + 6.0
    while time.monotonic() < deadline:
        if [e for e in rg.envelopes if e.get("broker") == "schwab"] and "schwab_ws_fail_refused" in caplog.text:
            break
        time.sleep(0.02)
    stop.set()
    thread.join(timeout=4.0)
    assert result == [0]
    assert [e for e in rg.envelopes if e.get("broker") == "schwab"], "the poll stopped"
    assert net.calls, "the listener never tried to connect"
    assert "schwab_ws_fail_refused" in caplog.text


# --- the switches and the stop event ---------------------------------------------------------


def test_the_hint_switch_off_builds_no_listener_and_dials_nothing(tmp_path, clock):
    net = FakeNet()
    feed = make_feed(tmp_path, clock, ws_connect=net, hint=False)
    assert feed.schwab.listener is None
    assert net.calls == []
    assert "schwab_ws_connects" not in feed.schwab.counts()


@pytest.mark.parametrize("value", ["0", "off", " OFF "])
def test_the_hint_switch_accepts_zero_and_off_as_off(value):
    assert schwab_hint_on({ENV_HINT: value}) is False


@pytest.mark.parametrize("environ", [{}, {ENV_HINT: "on"}, {ENV_HINT: ""}])
def test_the_hint_switch_is_on_by_default(environ):
    assert schwab_hint_on(environ) is True


def test_with_the_schwab_switch_off_nothing_starts(tmp_path, clock):
    env = env_for(tmp_path, schwab=False, sub="off")
    net = FakeNet()
    feed = Feed.build(
        env,
        clock=clock,
        make_client=lambda: None,
        http_post=RecordingRg(SENTINEL).post,
        hub_client=FakeHub().client(),
        ws_connect=net,
    )
    assert feed is not None and feed.schwab is None
    assert net.calls == []


def test_the_hint_listener_refuses_a_non_loopback_hub(tmp_path, clock):
    with pytest.raises(ValueError):
        HubHintListener("http://10.0.0.5:8081", on_hint=lambda: None)
    with pytest.raises(ValueError):
        HubHintListener("https://127.0.0.1:8081", on_hint=lambda: None)


def test_the_stop_event_ends_the_listener_within_one_second(tmp_path, clock):
    stalled = FakeWs(stall=True)
    net = FakeNet(stalled)
    listener = HubHintListener(HUB_URL, on_hint=lambda: None, connect=net, rand=lambda: 0.5)
    stop = threading.Event()
    worker = threading.Thread(target=listener.run, args=(stop,), daemon=True)
    worker.start()
    deadline = time.monotonic() + 3.0
    while stalled.timeouts < 2 and time.monotonic() < deadline:
        time.sleep(0.01)  # wait until the listener is inside its read loop
    started = time.monotonic()
    stop.set()
    worker.join(timeout=1.0)
    assert not worker.is_alive(), "the listener did not stop within one second"
    assert time.monotonic() - started < 1.0
    assert stalled.closed


def test_the_listener_frames_never_carry_a_body_into_a_log(tmp_path, clock, caplog):
    caplog.set_level(logging.DEBUG, logger="trade_engine.schwab.hub_ws")
    secret = "ACCT-TEST-194-SECRET-BODY"
    frame = server_frame(
        json.dumps({"data": {"service": ACCOUNTS_CHANGED, "note": secret}}).encode()
    )
    net = FakeNet(FakeWs((frame,)))
    listener = HubHintListener(HUB_URL, on_hint=lambda: None, connect=net, rand=lambda: 0.5)
    listener.run(threading.Event(), wait=lambda s: True)
    assert secret not in caplog.text



def test_another_services_frame_that_mentions_the_name_is_not_a_hint(tmp_path, clock):
    """The byte prefilter lets a frame through when the name appears anywhere; `service` decides."""
    calls: list[int] = []
    listener = _listener(tmp_path, clock, lambda: calls.append(1))
    listener._handle(
        json.dumps({"data": {"service": "LEVELONE_FUTURES", "note": ACCOUNTS_CHANGED}}).encode()
    )
    listener._handle(
        json.dumps({"data": {"service": "ACCT_ACTIVITY"}, "note": ACCOUNTS_CHANGED}).encode()
    )
    assert calls == []
    assert listener.hints_received == 0

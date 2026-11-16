"""BP-5e: the hub `/ws` listener that turns an `ACCOUNTS_CHANGED` frame into an early re-read.

The hub (`tvDownloadOHLC/scripts/streaming/schwab_hub.py`) sends every client every market frame
on `/ws`, with no subscription. An ACCT_ACTIVITY notice arrives there as a content-free frame:

    {"timestamp": "...", "data": {"service": "ACCOUNTS_CHANGED", "content": [{"changed": true}]}, ...}

This module reads that socket and calls `on_hint()` for each such frame. Nothing else is parsed:
a frame whose bytes do not contain the service name is skipped before any JSON work, so the
market data on the same socket costs one byte search per frame. The hint is only an optimisation;
the 60 s poll stays the contract. A failure here never reaches the poll.

Bounds (B7): one frame buffer of at most `MAX_FRAME_BYTES` (1 MiB). A bigger message is read and
discarded in bounded chunks and counted (`frames_oversized`). No queue, no list of frames.

The client is a stdlib RFC 6455 client for loopback text frames only. The standing decision D-9
allows no outside runtime dependency, and loopback is the only peer, so there is no TLS, no
extensions and no fragmentation beyond what the hub sends. Client frames are masked (RFC 6455
section 5.3); a masked server frame is a protocol error.

Counters only. No frame body, account number or hash is logged.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import random
import socket
import struct
import threading
from collections.abc import Callable
from urllib.parse import urlsplit

from trade_engine.schwab.hub_client import check_loopback

log = logging.getLogger(__name__)

ACCOUNTS_CHANGED = "ACCOUNTS_CHANGED"
WS_PATH = "/ws"
MAX_FRAME_BYTES = 1 << 20  # 1 MiB: one message, reassembled or discarded (B7)
HANDSHAKE_MAX_BYTES = 8192
CONNECT_TIMEOUT_S = 2.0
READ_TIMEOUT_S = 0.5  # the stop event is checked at least this often
BACKOFF_START_SECONDS = 5.0
BACKOFF_CAP_SECONDS = 120.0
BACKOFF_JITTER = 0.1  # +-10%, from the injected `rand`
#: The failure kinds the host counts (`schwab_ws_fail_<kind>`).
WS_KINDS = ("refused", "timeout", "unreachable", "handshake", "closed", "protocol")

_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_PREFILTER = b'"' + ACCOUNTS_CHANGED.encode() + b'"'
_OP_CONT, _OP_TEXT, _OP_BINARY, _OP_CLOSE, _OP_PING, _OP_PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA


class WsError(RuntimeError):
    """One failed connection. `kind` is in `WS_KINDS`; the message carries no frame body."""

    def __init__(self, kind: str, detail: str = "") -> None:
        if kind not in WS_KINDS:
            raise ValueError(f"unknown ws failure kind {kind!r}")
        self.kind = kind
        super().__init__(f"hub ws {kind}" + (f": {detail}" if detail else ""))


class _Stopped(Exception):
    """The stop event is set while a read is waiting; the listener returns."""


def _classify_connect(exc: BaseException) -> str:
    if isinstance(exc, ConnectionRefusedError):
        return "refused"
    if isinstance(exc, TimeoutError):
        return "timeout"
    return "unreachable"


def _accept_key(key: str) -> str:
    return base64.b64encode(hashlib.sha1((key + _GUID).encode()).digest()).decode()


class _Conn:
    """One socket plus its pushback buffer (bytes the handshake read past the header)."""

    def __init__(self, sock, stop: threading.Event) -> None:
        self.sock = sock
        self._stop = stop
        self._pending = bytearray()

    def recv_exact(self, n: int) -> bytes:
        while len(self._pending) < n:
            self._fill()
        out = bytes(self._pending[:n])
        del self._pending[:n]
        return out

    def discard(self, n: int) -> None:
        """Read and drop `n` bytes in bounded chunks: memory stays at one chunk."""
        remaining = n
        while remaining > 0:
            take = min(remaining, 65536)
            self.recv_exact(take)
            remaining -= take

    def read_head(self) -> bytes:
        """Bytes up to and including the blank line; anything after it stays pending."""
        while b"\r\n\r\n" not in self._pending:
            if len(self._pending) > HANDSHAKE_MAX_BYTES:
                raise WsError("handshake", "response head too large")
            self._fill()
        end = self._pending.index(b"\r\n\r\n") + 4
        head = bytes(self._pending[:end])
        del self._pending[:end]
        return head

    def _fill(self) -> None:
        try:
            chunk = self.sock.recv(65536)
        except TimeoutError:
            if self._stop.is_set():
                raise _Stopped() from None
            return
        if not chunk:
            raise WsError("closed", "hub closed the socket")
        self._pending += chunk

    def send_frame(self, opcode: int, payload: bytes = b"") -> None:
        """A masked client frame. Control frames only (payload under 126 bytes)."""
        if len(payload) >= 126:
            raise ValueError("control frames carry under 126 bytes")
        mask = os.urandom(4)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(bytes([0x80 | opcode, 0x80 | len(payload)]) + mask + masked)


class HubHintListener:
    """Reads the hub's `/ws` and calls `on_hint()` once per `ACCOUNTS_CHANGED` frame.

    `on_hint()` returns nothing the listener uses; its own exceptions are counted, never raised.
    `connect(host, port, timeout)` returns a socket-like object (`sendall`, `recv`, `settimeout`,
    `close`). Tests inject a fake; production uses `socket.create_connection`.
    """

    def __init__(
        self,
        base_url: str,
        *,
        on_hint: Callable[[], object],
        connect: Callable[[str, int, float], object] | None = None,
        rand: Callable[[], float] | None = None,
        backoff_start: float = BACKOFF_START_SECONDS,
        backoff_cap: float = BACKOFF_CAP_SECONDS,
    ) -> None:
        check_loopback(base_url)
        parts = urlsplit(base_url.strip())
        if parts.scheme != "http":
            raise ValueError("the hub /ws listener speaks plain ws on loopback only")
        self.host = parts.hostname or "127.0.0.1"
        self.port = parts.port or 80
        self._on_hint = on_hint
        self._connect = connect or socket.create_connection
        self._rand = rand or random.random
        self._backoff_start = backoff_start
        self._backoff_cap = backoff_cap
        self._consecutive = 0
        self.hints_received = 0
        self.hint_errors = 0
        self.connects = 0
        self.frames_oversized = 0
        self.frames_malformed = 0
        self.failures: dict[str, int] = dict.fromkeys(WS_KINDS, 0)

    def __repr__(self) -> str:
        return (
            f"HubHintListener(connects={self.connects}, hints={self.hints_received}, "
            f"failures={sum(self.failures.values())})"
        )

    # -- the loop ------------------------------------------------------------------------

    def run(self, stop_event: threading.Event, wait: Callable[[float], bool] | None = None) -> None:
        """Connect, read, and reconnect with jittered backoff until `stop_event`. Never raises."""
        wait = stop_event.wait if wait is None else wait
        while not stop_event.is_set():
            conn = None
            try:
                conn = self._open(stop_event)
                self.connects += 1
                self._consecutive = 0
                self._read_loop(conn, stop_event)
            except _Stopped:
                return
            except WsError as exc:
                self.failures[exc.kind] += 1
                log.warning("hub ws listener failed (%s)", exc.kind)
            except OSError as exc:
                kind = _classify_connect(exc)
                self.failures[kind] += 1
                log.warning("hub ws listener failed (%s)", kind)
            finally:
                if conn is not None:
                    try:
                        conn.sock.close()
                    except OSError:
                        pass
            if stop_event.is_set():
                return
            if wait(self._next_delay()):
                return

    def _open(self, stop_event: threading.Event) -> _Conn:
        sock = self._connect(self.host, self.port, CONNECT_TIMEOUT_S)
        sock.settimeout(READ_TIMEOUT_S)
        conn = _Conn(sock, stop_event)
        key = base64.b64encode(os.urandom(16)).decode()
        request = (
            f"GET {WS_PATH} HTTP/1.1\r\n"
            f"Host: {self.host}:{self.port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        try:
            sock.sendall(request.encode("ascii"))
            head = conn.read_head().decode("latin-1")
        except BaseException:
            sock.close()
            raise
        lines = head.split("\r\n")
        if not lines[0].startswith("HTTP/1.1 101"):
            sock.close()
            raise WsError("handshake", "hub did not switch protocols")
        accept = None
        for line in lines[1:]:
            name, _, value = line.partition(":")
            if name.strip().lower() == "sec-websocket-accept":
                accept = value.strip()
        if accept != _accept_key(key):
            sock.close()
            raise WsError("handshake", "bad Sec-WebSocket-Accept")
        return conn

    def _next_delay(self) -> float:
        """5 s, 10 s, 20 s, ... capped at 120 s, jittered by at most +-10%."""
        self._consecutive += 1
        base = min(self._backoff_cap, self._backoff_start * (2 ** (self._consecutive - 1)))
        jitter = 1.0 + ((self._rand() * 2.0) - 1.0) * BACKOFF_JITTER
        return min(self._backoff_cap, base * jitter)

    def _read_loop(self, conn: _Conn, stop_event: threading.Event) -> None:
        while not stop_event.is_set():
            opcode, body = self._read_message(conn)
            if body is None:
                self.frames_oversized += 1
                continue
            if opcode == _OP_TEXT:
                self._handle(body)

    # -- one message ---------------------------------------------------------------------

    def _read_message(self, conn: _Conn) -> tuple[int, bytes | None]:
        """One data message as `(opcode, body)`; body is None when it was over the cap."""
        opcode: int | None = None
        parts = bytearray()
        total = 0
        over = False
        while True:
            b0, b1 = conn.recv_exact(2)
            fin = bool(b0 & 0x80)
            op = b0 & 0x0F
            if b0 & 0x70:
                raise WsError("protocol", "reserved bits set")
            if b1 & 0x80:
                raise WsError("protocol", "server frame is masked")
            length = b1 & 0x7F
            if length == 126:
                (length,) = struct.unpack(">H", conn.recv_exact(2))
            elif length == 127:
                (length,) = struct.unpack(">Q", conn.recv_exact(8))
            if op >= 0x8:  # control frame: never fragmented, at most 125 bytes
                if not fin or length > 125:
                    raise WsError("protocol", "bad control frame")
                payload = conn.recv_exact(length)
                if op == _OP_PING:
                    conn.send_frame(_OP_PONG, payload)
                elif op == _OP_CLOSE:
                    raise WsError("closed", "hub sent a close frame")
                continue
            if op == _OP_CONT:
                if opcode is None:
                    raise WsError("protocol", "continuation without a start")
            elif opcode is None:
                opcode = op
            else:
                raise WsError("protocol", "new message inside a fragmented one")
            total += length
            if not over and total <= MAX_FRAME_BYTES:
                parts += conn.recv_exact(length)
            else:
                over = True
                conn.discard(length)
            if fin:
                if over:
                    return opcode, None
                return opcode, bytes(parts)

    def _handle(self, body: bytes) -> None:
        """Only `data.service` is read; a frame without the name is skipped before any parsing."""
        if _PREFILTER not in body:
            return
        try:
            obj = json.loads(body)
        except ValueError:
            self.frames_malformed += 1
            return
        if not isinstance(obj, dict):
            self.frames_malformed += 1
            return
        data = obj.get("data")
        service = data.get("service") if isinstance(data, dict) else obj.get("service")
        if service != ACCOUNTS_CHANGED:
            return
        self.hints_received += 1
        try:
            self._on_hint()
        except Exception as exc:  # noqa: BLE001 - the hint never reaches the poll
            self.hint_errors += 1
            log.warning("accounts hint callback failed (%s)", type(exc).__name__)


__all__ = [
    "ACCOUNTS_CHANGED",
    "MAX_FRAME_BYTES",
    "WS_KINDS",
    "HubHintListener",
    "WsError",
]

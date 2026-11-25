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

The RFC 6455 I/O (handshake, `Sec-WebSocket-Accept` check, masking, framing, ping/pong, close and
the jittered reconnect/backoff) lives in Rust (`HubHintListenerBase`); this module keeps the
public class (a thin subclass), the constants and the exceptions other modules import.
"""

from __future__ import annotations

import base64
import hashlib
import logging
from collections.abc import Callable

import trade_engine_rs as rs

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


class WsError(RuntimeError):
    """One failed connection. `kind` is in `WS_KINDS`; the message carries no frame body."""

    def __init__(self, kind: str, detail: str = "") -> None:
        if kind not in WS_KINDS:
            raise ValueError(f"unknown ws failure kind {kind!r}")
        self.kind = kind
        super().__init__(f"hub ws {kind}" + (f": {detail}" if detail else ""))


def _accept_key(key: str) -> str:
    return base64.b64encode(hashlib.sha1((key + _GUID).encode()).digest()).decode()


class HubHintListener(rs.HubHintListenerBase):
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
        super().__init__()

    def __repr__(self) -> str:
        return (
            f"HubHintListener(connects={self.connects}, hints={self.hints_received}, "
            f"failures={sum(self.failures.values())})"
        )


__all__ = [
    "ACCOUNTS_CHANGED",
    "MAX_FRAME_BYTES",
    "WS_KINDS",
    "HubHintListener",
    "WsError",
]

"""The ntx-daemon client: a loopback I/O shell (BROKER_PLATFORM phase 3, OA-3 orders).

It reads three things from the ntx daemon at ``127.0.0.1:9234``: ``GET /health``,
``GET /snapshot`` and the ``GET /events`` server-sent stream. It carries data and does I/O
only (D2, D3): which account was added or removed is `te_core::accounts`' decision, never
made here.

OA-3 adds the three order verbs, ``place``, ``modify`` and ``cancel`` (one POST each, no
retry, no thread). The order gate test allows exactly these three POST paths. A POST that
may have been written but got no answer raises ``NtxError("pending")``: the caller must
never resend it (the clOrdId is idempotent at the daemon, but the outcome is unknown).

Rules it carries:
- the bearer token is held in :class:`NtxToken`, which never shows its text in `repr`,
  `str`, a pickle, a log line or an exception (the test scans with a fake token);
- the host is loopback or the client refuses to build (the token never leaves the box);
- the SSE reader holds at most 1 MB per line; an oversize line is discarded to its end,
  counted (`oversize_dropped`) and never buffered;
- the reconnect delay starts at 1 s and doubles to a 30 s cap with jitter, and a flapping
  daemon cannot reset it: the attempt counter resets only after a connection stayed up for
  `stable_after` seconds. One socket is open at a time;
- :class:`RefreshLimiter` allows one early re-read per 5 s; extra triggers are coalesced
  into one trailing re-read and counted.

No clock is read (I7): `now` is an argument and sleeping goes through an injected callable.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime
from http.client import HTTPConnection, HTTPException
from pathlib import Path
from typing import Any

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 9234
TOKEN_MIN_LENGTH = 16
TOKEN_FILE = Path("secrets") / "daemon-token.txt"

# Budgets (BROKER_PLATFORM design 6.8a; BP-3 brief).
MAX_LINE_BYTES = 1_048_576  # SSE line buffer cap: 1 MB
MAX_BODY_BYTES = 16 * 1_048_576  # one /snapshot or /health body
ORDER_TIMEOUT_SECONDS = 2.5  # ntx gives itself 2 s
MAX_ORDER_BODY_BYTES = 16 * 1024  # one order request
MAX_ORDER_RESPONSE_BYTES = 64 * 1024  # one order answer
READ_TIMEOUT_SECONDS = 45.0  # the daemon's keepalive is 15 s
BACKOFF_FIRST = 1.0
BACKOFF_CAP = 30.0
BACKOFF_JITTER = 0.2
STABLE_AFTER_SECONDS = 60.0
REFRESH_MIN_INTERVAL_SECONDS = 5.0

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


class NtxError(Exception):
    """An ntx client refusal. `kind` is the machine-readable reason (I5)."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(f"{kind}: {message}")
        self.kind = kind
        self.message = message


class NtxToken:
    """The daemon's bearer token. Its text never shows in `repr`, `str` or a pickle."""

    __slots__ = ("_text",)

    def __init__(self, text: str) -> None:
        if not isinstance(text, str):
            raise NtxError("bad_token", "the token must be text")
        text = text.strip()
        if len(text) < TOKEN_MIN_LENGTH:
            raise NtxError("bad_token", f"the token is shorter than {TOKEN_MIN_LENGTH} characters")
        if any(ch in text for ch in "\r\n\x00"):
            raise NtxError("bad_token", "the token holds a control character")
        object.__setattr__(self, "_text", text)

    def header(self) -> dict[str, str]:
        """The one place the text is used: the Authorization header."""
        return {"Authorization": "Bearer " + self._text}

    def __repr__(self) -> str:
        return "NtxToken(<redacted>)"

    __str__ = __repr__

    def __reduce__(self):
        raise TypeError("an NtxToken is never pickled")

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("an NtxToken is immutable")

    def __eq__(self, other: object) -> bool:
        return self is other

    __hash__ = object.__hash__


def default_token_path(environ: dict[str, str] | None = None) -> Path:
    """`<NTX_STORE or %LOCALAPPDATA%\\ntx_tradovate>\\secrets\\daemon-token.txt`."""
    env = os.environ if environ is None else environ
    store = env.get("NTX_STORE")
    if store:
        return Path(store) / TOKEN_FILE
    local = env.get("LOCALAPPDATA")
    if not local:
        raise NtxError("no_token_path", "neither NTX_STORE nor LOCALAPPDATA is set")
    return Path(local) / "ntx_tradovate" / TOKEN_FILE


def read_token(path: str | Path) -> NtxToken:
    """Read the token file. A failure never carries the token text or the file's content."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise NtxError("token_unreadable", f"cannot read the token file ({type(exc).__name__})") from None
    except UnicodeDecodeError:
        raise NtxError("bad_token", "the token file is not text") from None
    return NtxToken(text)


@dataclass
class SseStats:
    """Counters for the SSE reader (a dropped frame is never silent)."""

    frames: int = 0
    keepalives: int = 0
    malformed: int = 0
    oversize_dropped: int = 0
    reconnects: int = 0
    sockets_opened: int = 0


class SseReader:
    """Parse a server-sent stream from a binary file-like, one bounded line at a time.

    The buffer is one line, never more than `max_line` bytes: `readline(max_line + 1)`
    returns at most that many, and a longer line is discarded in `max_line`-sized pieces
    until its newline (the memory held is one piece, the drop is counted once).
    """

    def __init__(self, stream, stats: SseStats, *, max_line: int = MAX_LINE_BYTES) -> None:
        self._stream = stream
        self._stats = stats
        self._max_line = max_line

    def _line(self) -> bytes | None:
        """The next line without its newline; b"" at a blank line; None at end of stream.

        An oversize line is consumed to its end and reported as the sentinel `_OVERSIZE`.
        """
        raw = self._stream.readline(self._max_line + 1)
        if not raw:
            return None
        if len(raw) > self._max_line and not raw.endswith(b"\n"):
            while True:
                piece = self._stream.readline(self._max_line + 1)
                if not piece or piece.endswith(b"\n"):
                    break
            self._stats.oversize_dropped += 1
            return _OVERSIZE
        return raw.rstrip(b"\r\n")

    def events(self) -> Iterator[dict[str, Any] | None]:
        """Yield each `state_update` payload, and `None` for each keepalive line.

        The `None` lets a caller run its timers while the stream is quiet. Malformed and
        oversize frames are counted and skipped."""
        data: list[bytes] = []
        event = ""
        poisoned = False
        while True:
            line = self._line()
            if line is None:
                return
            if line is _OVERSIZE:
                poisoned = True
                continue
            if line == b"":
                if poisoned:
                    pass  # the oversize drop was already counted
                elif data and event in ("", "state_update"):
                    payload = self._parse(b"\n".join(data))
                    if payload is not None:
                        self._stats.frames += 1
                        yield payload
                poisoned = False
                data = []
                event = ""
                continue
            if line.startswith(b":"):
                self._stats.keepalives += 1
                yield None
            elif line.startswith(b"event:"):
                event = line[6:].strip().decode("utf-8", "replace")
            elif line.startswith(b"data:"):
                data.append(line[5:].lstrip(b" "))

    def _parse(self, body: bytes) -> dict[str, Any] | None:
        try:
            payload = json.loads(body)
        except ValueError:
            self._stats.malformed += 1
            return None
        if not isinstance(payload, dict):
            self._stats.malformed += 1
            return None
        return payload


_OVERSIZE = b"\x00oversize"


class ReconnectBackoff:
    """1 s doubling to 30 s with jitter; reset only by a connection that stayed up.

    `delay()` is always at least `BACKOFF_FIRST`, so a daemon that accepts and drops
    instantly cannot make this a tight loop. `connected(now)` starts the stability
    timer; `dropped(now)` resets the attempt counter only if that connection lasted
    `stable_after` seconds. `rand` is injected (a `() -> float` in [0, 1)).
    """

    def __init__(
        self,
        rand: Callable[[], float],
        *,
        first: float = BACKOFF_FIRST,
        cap: float = BACKOFF_CAP,
        jitter: float = BACKOFF_JITTER,
        stable_after: float = STABLE_AFTER_SECONDS,
    ) -> None:
        if first <= 0 or cap < first or not 0 <= jitter < 1 or stable_after < 0:
            raise ValueError("bad backoff bounds (I5)")
        self._rand = rand
        self._first = first
        self._cap = cap
        self._jitter = jitter
        self._stable_after = stable_after
        self.attempt = 0
        self._up_since: datetime | None = None

    def delay(self) -> float:
        base = min(self._cap, self._first * (2 ** min(self.attempt, 16)))
        spread = max(0.0, min(1.0, float(self._rand())))
        self.attempt += 1
        return min(self._cap, base * (1.0 + self._jitter * spread))

    def connected(self, now: datetime) -> None:
        self._up_since = now

    def dropped(self, now: datetime) -> None:
        if self._up_since is not None and (now - self._up_since).total_seconds() >= self._stable_after:
            self.attempt = 0
        self._up_since = None


class RefreshLimiter:
    """At most one early re-read per `min_interval`; the rest are coalesced and counted.

    `trigger(now)` is an SSE hint that the account list may have changed: it returns True
    when a re-read may run now. Otherwise the hint is coalesced into one pending trailing
    re-read, which `due(now)` releases once the interval has passed. The state is two
    numbers, never a queue.
    """

    def __init__(self, min_interval: float = REFRESH_MIN_INTERVAL_SECONDS) -> None:
        if min_interval < 0:
            raise ValueError("min_interval must not be negative (I5)")
        self._min = min_interval
        self._last: datetime | None = None
        self._pending = False
        self.allowed = 0
        self.coalesced = 0

    def _open(self, now: datetime) -> bool:
        return self._last is None or (now - self._last).total_seconds() >= self._min

    def trigger(self, now: datetime) -> bool:
        if self._open(now):
            self._last = now
            self._pending = False
            self.allowed += 1
            return True
        self.coalesced += 1
        self._pending = True
        return False

    def due(self, now: datetime) -> bool:
        """True once, when a coalesced trigger is waiting and the interval has passed."""
        if self._pending and self._open(now):
            self._last = now
            self._pending = False
            self.allowed += 1
            return True
        return False

    def note_read(self, now: datetime) -> None:
        """A full read happened for another reason (connect, reconnect): restart the window."""
        self._last = now
        self._pending = False

    @property
    def pending(self) -> bool:
        return self._pending


_ORDER_OUTCOMES = frozenset({"SENT", "PENDING", "REFUSED"})
_ORDER_RESPONSE_KEYS = frozenset(
    {"account", "clOrdId", "op", "outcome", "code", "reason", "orderIds", "commandId"}
)
_ORDER_RESPONSE_REQUIRED = frozenset({"account", "clOrdId", "op", "outcome"})
_ORDER_ID_KEYS = frozenset({"entryId", "stopId", "targetId"})


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _valid_order_response(body: object, op: str, cl_ord_id: object) -> bool:
    """The strict OrderResponse check (submit.rs): exact keys and types, op and clOrdId echoed."""
    if not isinstance(body, dict) or not _ORDER_RESPONSE_REQUIRED <= body.keys() <= _ORDER_RESPONSE_KEYS:
        return False
    if not all(isinstance(body[k], str) for k in ("account", "clOrdId", "op", "outcome")):
        return False
    if body["outcome"] not in _ORDER_OUTCOMES or body["op"] != op or body["clOrdId"] != cl_ord_id:
        return False
    for key in ("code", "reason"):
        if key in body and not isinstance(body[key], str):
            return False
    if "commandId" in body and not _is_int(body["commandId"]):
        return False
    ids = body.get("orderIds")
    if "orderIds" in body:
        if not isinstance(ids, dict) or not {"entryId", "stopId"} <= ids.keys() <= _ORDER_ID_KEYS:
            return False
        if not all(_is_int(v) for v in ids.values()):
            return False
    return True


class NtxClient:
    """HTTP client for the ntx daemon: `health()`, `snapshot()`, `open_events()` and the
    three order verbs `place()`, `modify()`, `cancel()`.

    One connection per call; `open_events()` returns the only long-lived socket, which the
    caller closes. The daemon is on loopback; any other host refuses.
    """

    def __init__(
        self,
        token: NtxToken,
        *,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        timeout: float = 10.0,
        stats: SseStats | None = None,
    ) -> None:
        if host not in _LOOPBACK_HOSTS:
            raise NtxError("not_loopback", "the ntx daemon is reached on loopback only")
        if not isinstance(token, NtxToken):
            raise NtxError("bad_token", "a NtxToken is required")
        self._token = token
        self._host = host
        self._port = port
        self._timeout = timeout
        self.stats = stats if stats is not None else SseStats()

    def __repr__(self) -> str:
        return f"NtxClient(host={self._host!r}, port={self._port})"

    def _connect(self, timeout: float) -> HTTPConnection:
        return HTTPConnection(self._host, self._port, timeout=timeout)

    def _get_json(self, path: str, accept: tuple[int, ...]) -> tuple[int, dict[str, Any]]:
        conn = self._connect(self._timeout)
        try:
            try:
                conn.request("GET", path, headers=self._token.header())
                resp = conn.getresponse()
                body = resp.read(MAX_BODY_BYTES + 1)
                status = resp.status
            except (OSError, HTTPException) as exc:
                raise NtxError("unavailable", f"the daemon did not answer ({type(exc).__name__})") from None
        finally:
            conn.close()
        if status == 401:
            raise NtxError("unauthorized", "the daemon refused the token (401)")
        if status not in accept:
            raise NtxError("http_status", f"the daemon answered {status}")
        if len(body) > MAX_BODY_BYTES:
            raise NtxError("oversize_body", "the daemon's answer is over the body cap")
        try:
            payload = json.loads(body)
        except ValueError:
            raise NtxError("protocol", "the daemon's answer is not JSON") from None
        if not isinstance(payload, dict):
            raise NtxError("protocol", "the daemon's answer is not an object")
        return status, payload

    def _post_order(self, path: str, op: str, body: dict[str, Any]) -> dict[str, Any]:
        """One order POST. Returns the validated OrderResponse dict.

        Raises NtxError: `network` (nothing was written), `pending` (written, no answer),
        `unauthorized` (401), `invalid_body` (HTTP 400; message is the daemon's code) or
        `invalid` (an answer that is not a valid OrderResponse; the order's fate is unknown).
        """
        raw = json.dumps(body, separators=(",", ":")).encode()
        if len(raw) > MAX_ORDER_BODY_BYTES:
            raise NtxError("oversize_body", "the order body is over the body cap")
        headers = dict(self._token.header())
        headers["Content-Type"] = "application/json"
        conn = self._connect(ORDER_TIMEOUT_SECONDS)
        try:
            try:
                conn.connect()
            except (OSError, HTTPException) as exc:
                raise NtxError("network", f"the daemon was not reached ({type(exc).__name__})") from None
            try:
                conn.request("POST", path, body=raw, headers=headers)
                resp = conn.getresponse()
                answer = resp.read(MAX_ORDER_RESPONSE_BYTES + 1)
                status = resp.status
            except (OSError, HTTPException) as exc:
                raise NtxError("pending", f"no answer after the write ({type(exc).__name__})") from None
        finally:
            conn.close()
        if status == 401:
            raise NtxError("unauthorized", "the daemon refused the token (401)")
        if len(answer) > MAX_ORDER_RESPONSE_BYTES:
            raise NtxError("invalid", "the daemon's answer is over the body cap")
        try:
            payload = json.loads(answer)
        except ValueError:
            payload = None
        if not _valid_order_response(payload, op, body.get("clOrdId")):
            if status == 400:
                raise NtxError("invalid_body", "invalid_body")
            raise NtxError("invalid", f"the daemon's answer ({status}) is not an order response")
        if status == 400:
            raise NtxError("invalid_body", payload.get("code") or "invalid_body")
        return payload

    def place(self, body: dict[str, Any]) -> dict[str, Any]:
        """`POST /v1/orders`: a bracketed entry. Never resend after `NtxError("pending")`."""
        return self._post_order("/v1/orders", "place", body)

    def modify(self, body: dict[str, Any]) -> dict[str, Any]:
        """`POST /v1/orders/modify`."""
        return self._post_order("/v1/orders/modify", "modify", body)

    def cancel(self, body: dict[str, Any]) -> dict[str, Any]:
        """`POST /v1/orders/cancel`."""
        return self._post_order("/v1/orders/cancel", "cancel", body)

    def health(self) -> tuple[int, dict[str, Any]]:
        """`(status, body)` from `GET /health`: 200, or 207 when an identity is degraded."""
        return self._get_json("/health", (200, 207))

    def snapshot(self) -> dict[str, Any]:
        """`GET /snapshot`: every identity's accounts (one read, never per event)."""
        return self._get_json("/snapshot", (200,))[1]

    def open_events(self) -> "NtxStream":
        """Open `GET /events` and return the stream once the daemon has accepted.

        Raises NtxError on a refused or failed connect. The caller closes the stream;
        one stream at a time is the caller's rule (the watcher keeps exactly one).
        """
        conn = self._connect(READ_TIMEOUT_SECONDS)
        try:
            conn.request("GET", "/events", headers=self._token.header())
            resp = conn.getresponse()
        except (OSError, HTTPException) as exc:
            conn.close()
            raise NtxError("unavailable", f"the daemon did not answer ({type(exc).__name__})") from None
        self.stats.sockets_opened += 1
        if resp.status == 401:
            conn.close()
            raise NtxError("unauthorized", "the daemon refused the token (401)")
        if resp.status != 200:
            conn.close()
            raise NtxError("http_status", f"the daemon answered {resp.status}")
        return NtxStream(conn, resp, self.stats)


class NtxStream:
    """One open `/events` socket. `events()` yields payloads (and `None` per keepalive)."""

    def __init__(self, conn: HTTPConnection, resp, stats: SseStats) -> None:
        self._conn = conn
        self._reader = SseReader(resp, stats)

    def events(self) -> Iterator[dict[str, Any] | None]:
        try:
            yield from self._reader.events()
        except (OSError, HTTPException) as exc:
            raise NtxError("unavailable", f"the stream broke ({type(exc).__name__})") from None

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "NtxStream":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()


__all__ = [
    "BACKOFF_CAP",
    "BACKOFF_FIRST",
    "MAX_LINE_BYTES",
    "MAX_ORDER_BODY_BYTES",
    "MAX_ORDER_RESPONSE_BYTES",
    "NtxClient",
    "NtxError",
    "NtxStream",
    "NtxToken",
    "ReconnectBackoff",
    "ORDER_TIMEOUT_SECONDS",
    "RefreshLimiter",
    "SseReader",
    "SseStats",
    "default_token_path",
    "read_token",
]

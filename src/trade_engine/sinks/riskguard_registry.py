"""Registry sink: the engine's accounts to riskguard's `POST /v1/registry/ingest` (BP-4a, I12).

I/O only. Every rule (ordering, ack, gap, compaction, cap, halt) is `te_core::registry_outbox`
(D3); this module reads a token file, POSTs the envelopes Rust built, hands the answers back
to Rust and persists the state Rust serialises.

- **Default off.** `RegistrySinkConfig.enabled` is False unless the environment says `on`;
  an off sink reads no token, opens no socket and writes no file (rollback = off).
- **Delivery is confirmed by the ack body** (`applied_seq`), never by a bare 200: Rust decides.
- **The token** is read by path at each delivery (a rotated file is picked up), held only for
  the request, and never stored on the sink, printed, logged, put in an exception, a repr or
  the outbox file. Every failure is reported as an exception *type* or a refusal *kind*; a
  response body is never echoed.
- **Nothing on the order path waits on this.** `observe` only updates memory under a lock;
  the network and the disk are touched by `pump`, which runs on the sink's own worker thread
  (or the caller's, in tests). A failure here never fails the account poll.
- **Account ids** reach logs masked only (`*194`); the sink logs counts and kinds.

The sink never reads a clock: `now` comes from the injected clock (I7).

The delivery sequencing and the POST socket I/O now live in `trade_engine_rs`
(`RiskguardRegistrySinkBase` / `registry_http_post`); this module keeps the import path, the
public names, the config dataclass, the token/persist helpers, and the thin state helpers the
Rust base reaches through attribute lookup.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from trade_engine.accounts import _rs
from trade_engine.accounts.errors import AccountRuleError
from trade_engine.clock.wall import pause

log = logging.getLogger(__name__)

ENV_SWITCH = "TE_RISKGUARD_REGISTRY_SINK"
ENV_URL = "TE_RISKGUARD_URL"
ENV_TOKEN_FILE = "TE_RISKGUARD_TOKEN_FILE"
ENV_OUTBOX = "TE_RISKGUARD_OUTBOX_PATH"
ENV_PNL = "TE_RISKGUARD_REGISTRY_PNL"  # PS-4a: only `on` sends the broker realized P&L list

DEFAULT_CAP_BYTES = 50 * 1024 * 1024
DEFAULT_MAX_TAIL_EVENTS = 100_000
MAX_ENVELOPES_PER_PUMP = 64
MAX_RESPONSE_BYTES = 65_536
TOKEN_FILE_MAX_BYTES = 1_048_576
REQUEST_TIMEOUT_S = 5.0
REBASE_MIN_INTERVAL_S = 60.0  # a second ack_ahead inside this window halts (BP-4a-SEQ)
REPLACE_RETRY_SECONDS = 0.05  # REGFEED-STOP-1: one retry after PermissionError (AV or indexer lock)

# (status, body text); status 0 is "no answer".
HttpPost = Callable[[str, bytes, Mapping[str, str], float], tuple[int, str]]


@dataclass(frozen=True)
class RegistrySinkConfig:
    """Where and whether to deliver. `enabled` is False by default and only `on` enables it."""

    enabled: bool = False
    base_url: str = ""
    token_file: str = ""
    outbox_path: str = ""
    broker: str = "tradovate"
    stale_after_s: int = 120
    cap_bytes: int = DEFAULT_CAP_BYTES
    max_tail_events: int = DEFAULT_MAX_TAIL_EVENTS
    pnl_enabled: bool = False  # PS-4a; ignored when `enabled` is False

    @classmethod
    def from_env(cls, environ: Mapping[str, str], **overrides: Any) -> "RegistrySinkConfig":
        enabled = environ.get(ENV_SWITCH, "off").strip().lower() == "on"
        pnl_enabled = environ.get(ENV_PNL, "off").strip().lower() == "on"
        return cls(
            enabled=enabled,
            pnl_enabled=pnl_enabled,
            base_url=environ.get(ENV_URL, ""),
            token_file=environ.get(ENV_TOKEN_FILE, ""),
            outbox_path=environ.get(ENV_OUTBOX, ""),
            **overrides,
        )

    def __repr__(self) -> str:  # the token file's path is not a secret, its contents are
        return (
            f"RegistrySinkConfig(enabled={self.enabled}, pnl_enabled={self.pnl_enabled}, "
            f"broker={self.broker!r}, cap_bytes={self.cap_bytes})"
        )


def _replace_with_retry(tmp: Path, path: Path) -> None:
    """`os.replace`, retried once after 50 ms on PermissionError (Windows AV or indexer sharing)."""
    try:
        os.replace(tmp, path)
    except PermissionError:
        pause(REPLACE_RETRY_SECONDS)
        os.replace(tmp, path)


def _log_write_failure(what: str, exc: OSError, path: Path) -> None:
    """The exception type, errno, winerror and the path. Never the file's contents."""
    log.warning(
        "%s (%s, errno=%s, winerror=%s, path=%s)",
        what,
        type(exc).__name__,
        exc.errno,
        getattr(exc, "winerror", None),
        path,
    )


class TokenUnavailable(RuntimeError):
    """The connector token could not be read; the message never names the file's contents."""


def _default_post(url: str, body: bytes, headers: Mapping[str, str], timeout: float) -> tuple[int, str]:
    """POST to the registry; the socket I/O lives in `trade_engine_rs`."""
    return _rs.rs.registry_http_post(url, body, headers, timeout)


def read_token(path: str) -> str:
    """The connector token from `path`: a raw token, or a token document's connector entry.

    Raises `TokenUnavailable` without any file content in the message.
    """
    try:
        p = Path(path)
        if p.stat().st_size > TOKEN_FILE_MAX_BYTES:
            raise TokenUnavailable("token file is too large")
        text = p.read_text(encoding="utf-8").strip()
    except TokenUnavailable:
        raise
    except OSError:
        raise TokenUnavailable("token file cannot be read") from None
    if text.startswith("{"):
        try:
            doc = json.loads(text)
            rows = doc.get("tokens") if isinstance(doc, dict) else None
            for row in rows or []:
                if isinstance(row, dict) and row.get("role") == "connector" and isinstance(row.get("token"), str):
                    text = row["token"].strip()
                    break
            else:
                raise TokenUnavailable("token file has no connector token")
        except ValueError:
            raise TokenUnavailable("token file is not valid JSON") from None
    if not text:
        raise TokenUnavailable("token file is empty")
    return text


def _worker_run(sink: "RiskguardRegistrySink", tick_seconds: float) -> None:
    """The worker loop `start` runs on a daemon thread: on each observe, and every tick."""
    while not sink._stop.is_set():
        sink._wake.wait(tick_seconds)
        sink._wake.clear()
        if sink._stop.is_set():
            break
        try:
            sink.pump()
        except Exception as exc:  # noqa: BLE001 - the worker must outlive any one failure
            sink.last_error_kind = type(exc).__name__
            log.warning("registry sink pump failed (%s)", type(exc).__name__)


class RiskguardRegistrySink(_rs.rs.RiskguardRegistrySinkBase):
    """Account snapshots and events to riskguard, through the Rust outbox."""

    def __init__(
        self,
        config: RegistrySinkConfig,
        *,
        clock,
        http_post: HttpPost | None = None,
    ) -> None:
        super().__init__()
        self._config = config
        self._clock = clock
        self._post = http_post or _default_post
        self._lock = threading.RLock()
        self._pump_lock = threading.Lock()  # one delivery at a time; `observe` never takes it
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._dirty = False
        self._outbox = None
        self._seq_base = 0  # BP-4a-SEQ: added to every poller seq; persisted in `<outbox>.base`
        self._last_rebase = None
        self.counters: dict[str, int] = {
            "observed": 0,
            "observe_refused": 0,
            "reseeds": 0,
            "rebases": 0,
            "posts": 0,
            "post_failures": 0,
            "token_failures": 0,
            "acks": 0,
            "ack_refused": 0,
            "snapshots_requested": 0,
            "halts": 0,
            "persist_failures": 0,
            "state_discarded": 0,
            "oversize_refused": 0,
            "pnl_refused": 0,
        }
        self.last_error_kind: str | None = None
        if not config.enabled:
            return
        if not (config.base_url and config.token_file and config.outbox_path):
            raise ValueError("an enabled registry sink needs base_url, token_file and outbox_path")
        self._seq_base = self._load_base()
        self._outbox = self._load()
        if config.pnl_enabled:
            self._outbox.set_pnl_enabled(True)

    def __repr__(self) -> str:
        return f"RiskguardRegistrySink(enabled={self.enabled}, halted={self.halted!r})"

    # -- state ------------------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return self._outbox is not None

    @property
    def halted(self) -> str | None:
        if self._outbox is None:
            return None
        with self._lock:
            return self._outbox.halted()

    def stats(self) -> dict[str, Any]:
        out: dict[str, Any] = dict(self.counters)
        out["enabled"] = self.enabled
        out["seq_base"] = self._seq_base
        if self._outbox is not None:
            with self._lock:
                out["outbox"] = json.loads(self._outbox.stats())
        return out

    def _new_outbox(self):
        c = self._config
        return _rs.rs.RegistryOutbox(c.broker, c.stale_after_s, c.cap_bytes, c.max_tail_events)

    def _load(self):
        c = self._config
        path = Path(c.outbox_path)
        try:
            size = path.stat().st_size
        except OSError:
            return self._new_outbox()
        try:
            if size > c.cap_bytes:
                raise AccountRuleError("cap_exceeded", "the outbox file is over the cap")
            text = path.read_text(encoding="utf-8")
            return _rs._call(
                _rs.rs.RegistryOutbox.from_json, text, c.broker, c.stale_after_s, c.cap_bytes, c.max_tail_events
            )
        except (AccountRuleError, OSError, UnicodeDecodeError) as exc:
            kind = exc.kind if isinstance(exc, AccountRuleError) else type(exc).__name__
            log.warning("registry outbox state discarded (%s); starting from the next snapshot", kind)
            self.counters["state_discarded"] += 1
            return self._new_outbox()

    def _base_path(self) -> Path:
        return Path(self._config.outbox_path + ".base")

    def _load_base(self) -> int:
        """The persisted sequence base; a missing or bad file is 0."""
        path = self._base_path()
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return 0
        except (OSError, UnicodeDecodeError) as exc:
            log.warning("registry seq base unreadable (%s); using 0", type(exc).__name__)
            return 0
        try:
            value = json.loads(text).get("seq_base")
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                return value
        except (ValueError, AttributeError):
            pass
        log.warning("registry seq base file is corrupt; using 0")
        return 0

    def _persist_base(self) -> None:
        """Write `{"seq_base":N}` with tmp + os.replace (rare, one tiny file)."""
        path = self._base_path()
        tmp = path.with_name(path.name + ".tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps({"seq_base": self._seq_base}), encoding="utf-8")
            _replace_with_retry(tmp, path)
        except OSError as exc:
            self.counters["persist_failures"] += 1
            _log_write_failure("registry seq base persist failed", exc, path)

    def _rebased_events(self, events: list[dict[str, Any]]) -> str:
        base = self._seq_base
        if base:
            events = [{**e, "seq": e["seq"] + base} if isinstance(e.get("seq"), int) else e for e in events]
        return json.dumps(events, separators=(",", ":"))

    def _persist(self) -> None:
        """Write the outbox to disk. Serialising is memory-only and runs under the lock; the
        file write runs outside it, so the poll path never waits on the disk."""
        if self._outbox is None:
            return
        with self._lock:
            if not self._dirty:
                return
            text = self._outbox.to_json()
            self._dirty = False
        if len(text.encode("utf-8")) > self._config.cap_bytes:
            self.counters["oversize_refused"] += 1
            self._dirty = True
            return
        path = Path(self._config.outbox_path)
        tmp = path.with_name(path.name + ".tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(text, encoding="utf-8")
            _replace_with_retry(tmp, path)
        except OSError as exc:
            self._dirty = True
            self.counters["persist_failures"] += 1
            _log_write_failure("registry outbox persist failed", exc, path)

    # -- thin helpers the Rust base reaches through attribute lookup ---------------------

    def _observe_locked(self, snap_text: str, ev_text: str, seq: int, confirmed: str) -> None:
        _rs._call(self._outbox.observe, snap_text, ev_text, seq, confirmed)

    def _forward_pnl(self, rows: Sequence[tuple[str, tuple[str, str] | None]]) -> None:
        for key, figure in rows:
            try:
                if figure is None:
                    self._outbox.clear_pnl(key)
                else:
                    _rs._call(self._outbox.set_pnl, key, figure[0], figure[1])
            except AccountRuleError as exc:
                self.counters["pnl_refused"] += 1
                self.last_error_kind = exc.kind
                # Kind only: the key is an account id and stays out of the log (masked elsewhere).
                log.warning("registry sink refused a pnl figure (%s)", exc.kind)

    def _refused(self, kind: str) -> None:
        self.counters["observe_refused"] += 1
        self.last_error_kind = kind
        log.warning("registry sink refused a poll (%s)", kind)

    def _halted(self, kind: str) -> None:
        self.counters["halts"] += 1
        self.last_error_kind = kind
        log.error("registry sink halted (%s); the owner must look before it is resumed", kind)


__all__ = [
    "ENV_PNL",
    "ENV_SWITCH",
    "RegistrySinkConfig",
    "RiskguardRegistrySink",
    "TokenUnavailable",
    "read_token",
]

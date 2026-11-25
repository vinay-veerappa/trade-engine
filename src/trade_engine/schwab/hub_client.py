"""BP-5c: the registry feed's client for the Schwab hub's two account reads.

The hub (`tvDownloadOHLC/scripts/streaming/schwab_hub.py`) is the only Schwab client (D3): it
holds the session and the REST budget. This module asks it for the two reads the BP-5b adapter
maps and hands back their text, exactly as the hub's `data` carries them:

    POST http://127.0.0.1:8081/request  {"method": "get_account_numbers"}
    POST http://127.0.0.1:8081/request  {"method": "get_accounts"}

Nothing else goes in the body: `providers/rest_policy.py` refuses any parameter of these two
reads (403), so the client never sends `accountHash`, `fields` or `priority`.

Rules it carries:

- **Loopback only.** `TE_SCHWAB_HUB_URL` (default `http://127.0.0.1:8081`) must name `127.0.0.1`,
  `localhost` or `::1`; anything else is a `ValueError` at construction, the same "bad env"
  class of failure as a sink without a URL (I5: refuse, never approximate).
- **Success is `{"status": "success", "data": ...}` and nothing else.** A `status` that is not
  `success`, a `code` of `timeout` or `403`, an HTTP 403/4xx/5xx, no answer at all, or a body
  that is not that object is one `HubReadError(kind)`. `kind` is one of `refused`, `timeout`,
  `unreachable`, `bad_shape` so the host can count what went wrong without parsing text.
- **A response body is never logged.** The error message carries the method and the kind only:
  the hub's `data` holds account numbers and `hashValue`.
- **One read is two calls** (the pair), bounded by `MAX_RESPONSE_BYTES` and `timeout_s`; the
  steady cost is 2 calls/min of the hub's shared 120/min, because the adapter's 60 s floor and
  the host's backoff decide when `read()` is called (B7).

No clock is read here (I7): the timeout is passed to the socket, not measured.

The HTTP I/O lives in Rust (`HubAccountsClientBase`); this module keeps the public class (a
thin subclass), the constants and the exceptions other modules import.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import urlsplit

import trade_engine_rs as rs

log = logging.getLogger(__name__)

ENV_HUB_URL = "TE_SCHWAB_HUB_URL"
DEFAULT_HUB_URL = "http://127.0.0.1:8081"
REQUEST_PATH = "/request"
#: The two account reads, in the order one poll makes them (membership first, fields second).
READ_METHODS = ("get_account_numbers", "get_accounts")
DEFAULT_TIMEOUT_S = 10.0
MAX_RESPONSE_BYTES = 1_048_576
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
#: The failure kinds the host counts (`schwab_read_fail_<kind>`).
KINDS = ("refused", "timeout", "unreachable", "bad_shape")

# (status, body text); status 0 is "no answer". May raise on a socket failure.
Post = Callable[[str, bytes, Mapping[str, str], float], tuple[int, str]]


class HubReadError(RuntimeError):
    """One failed read. `kind` is in `KINDS`; the message never carries a body."""

    def __init__(self, kind: str, detail: str = "") -> None:
        if kind not in KINDS:
            raise ValueError(
                f"unknown hub read kind {kind!r}; want one of {list(KINDS)}"
            )
        self.kind = kind
        super().__init__(
            f"hub read failed ({kind})" + (f": {detail}" if detail else "")
        )


def check_loopback(url: str) -> None:
    """Refuse anything but a loopback http(s) URL (ValueError). The host is not a secret."""
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError(f"{ENV_HUB_URL} must be an http URL on loopback, got no host")
    if parts.hostname not in LOOPBACK_HOSTS:
        raise ValueError(
            f"{ENV_HUB_URL} must be a loopback host ({', '.join(sorted(LOOPBACK_HOSTS))}); "
            "the hub is local (D3)"
        )


def hub_base_url(environ: Mapping[str, str]) -> str:
    """`TE_SCHWAB_HUB_URL` or the loopback default, checked at config time."""
    url = (environ.get(ENV_HUB_URL) or "").strip() or DEFAULT_HUB_URL
    check_loopback(url)
    return url


class HubAccountsClient(rs.HubAccountsClientBase):
    """The hub's `/request` door for the two account reads. One pair per `read()`."""

    def __init__(
        self,
        base_url: str = DEFAULT_HUB_URL,
        *,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        post: Post | None = None,
    ) -> None:
        super().__init__()

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str],
        *,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        post: Post | None = None,
    ) -> "HubAccountsClient":
        """The configured client; a non-loopback `TE_SCHWAB_HUB_URL` raises ValueError."""
        return cls(hub_base_url(environ), timeout_s=timeout_s, post=post)

    def __repr__(self) -> str:
        return (
            f"HubAccountsClient(base_url={self.base_url!r}, timeout_s={self.timeout_s})"
        )


__all__ = [
    "DEFAULT_HUB_URL",
    "DEFAULT_TIMEOUT_S",
    "ENV_HUB_URL",
    "KINDS",
    "LOOPBACK_HOSTS",
    "READ_METHODS",
    "HubAccountsClient",
    "HubReadError",
    "check_loopback",
    "hub_base_url",
]

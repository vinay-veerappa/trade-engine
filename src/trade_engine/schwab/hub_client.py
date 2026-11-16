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
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import urlsplit

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


def _default_post(
    url: str, body: bytes, headers: Mapping[str, str], timeout: float
) -> tuple[int, str]:
    """POST and return `(status, text)`; a socket failure is raised for the caller to classify."""
    req = urllib.request.Request(url, data=body, headers=dict(headers), method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read(MAX_RESPONSE_BYTES).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, exc.read(MAX_RESPONSE_BYTES).decode("utf-8", "replace")
        finally:
            exc.close()


class HubAccountsClient:
    """The hub's `/request` door for the two account reads. One pair per `read()`."""

    def __init__(
        self,
        base_url: str = DEFAULT_HUB_URL,
        *,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        post: Post | None = None,
    ) -> None:
        check_loopback(base_url)
        self.base_url = base_url.strip().rstrip("/")
        self.timeout_s = timeout_s
        self._post = post or _default_post
        self.reads = 0  # pairs completed, for the host's own accounting

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str],
        *,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        post: Post | None = None,
    ) -> HubAccountsClient:
        """The configured client; a non-loopback `TE_SCHWAB_HUB_URL` raises ValueError."""
        return cls(hub_base_url(environ), timeout_s=timeout_s, post=post)

    def __repr__(self) -> str:
        return (
            f"HubAccountsClient(base_url={self.base_url!r}, timeout_s={self.timeout_s})"
        )

    def read(self) -> tuple[str, str]:
        """The two reads as `(account_numbers_json, accounts_json)` text, in that order."""
        numbers = self._one(READ_METHODS[0])
        accounts = self._one(READ_METHODS[1])
        self.reads += 1
        return json.dumps(numbers), json.dumps(accounts)

    def _one(self, method: str) -> Any:
        body = json.dumps({"method": method}).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        try:
            status, text = self._post(
                self.base_url + REQUEST_PATH, body, headers, self.timeout_s
            )
        except TimeoutError:
            raise HubReadError(
                "timeout", f"{method} was not answered in time"
            ) from None
        except urllib.error.URLError as exc:
            kind = "timeout" if isinstance(exc.reason, TimeoutError) else "unreachable"
            raise HubReadError(kind, f"{method} could not reach the hub") from None
        except OSError:
            raise HubReadError(
                "unreachable", f"{method} could not reach the hub"
            ) from None
        if status == 0:
            raise HubReadError("unreachable", f"{method} got no answer")
        if status == 403:
            raise HubReadError("refused", f"{method} was refused")
        if status >= 500:
            raise HubReadError("unreachable", f"{method} got HTTP {status}")
        if status != 200:
            raise HubReadError("refused", f"{method} got HTTP {status}")
        return self._data(method, text)

    @staticmethod
    def _data(method: str, text: str) -> Any:
        """The `data` of a success answer; every other shape is a failure with a kind."""
        try:
            answer = json.loads(text)
        except ValueError:
            raise HubReadError(
                "bad_shape", f"{method} did not answer with JSON"
            ) from None
        if not isinstance(answer, dict):
            raise HubReadError("bad_shape", f"{method} did not answer with an object")
        code = answer.get("code")
        if str(code).strip().lower() == "timeout":
            raise HubReadError("timeout", f"{method} timed out in the hub")
        if code in (403, "403", "refused"):
            raise HubReadError("refused", f"{method} is not an allowed hub read")
        status = answer.get("status")
        if status is None:
            raise HubReadError("bad_shape", f"{method} answered without a status")
        if status != "success":
            raise HubReadError("refused", f"{method} answered status {status!r}")
        if "data" not in answer:
            raise HubReadError("bad_shape", f"{method} answered success without data")
        return answer["data"]


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

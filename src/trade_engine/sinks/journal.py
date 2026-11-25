"""HTTP implementation of the JournalSink protocol (Architecture §2, I5, I12, §4.11).

Posts fills as executions to the trade journal (:3300), configures multiplier, stop, target,
and strategy tag, and confirms delivery by reading back rather than trusting HTTP 200.

Written against the journal's routes in `third_party/trade-journal/apps/web/src/app/api`:

- `POST /api/executions` (source "manual") refuses `importMetadata`, and de-duplicates on a
  hash of symbol, side, quantity, price and the raw `executedAt` string. Two genuine fills
  with identical fields would collapse into one row, so the sub-millisecond digits of
  `executedAt` carry the ledger seq (`journal_executed_at`): distinct events stay distinct,
  a retry of the same event is the same row, and the journal (which orders by
  millisecond) sees the real time.
- `GET /api/settings` returns `multipliers` at the top level, and `PATCH` *replaces* the
  whole map and rebuilds every account, so a multiplier is only ever written as a merge
  over a map that was just read, and read back after.
- `GET /api/trades/[key]` returns the raw trade row: tags are `tagsJson`, a JSON string.
- A zero fee is replaced by the journal's default fee rule, if one matches.
- With a password configured every route answers 401; that raises `JournalAuthError` so
  the outbox records why it is stuck instead of retrying silently.

The delivery sequencing now lives in `trade_engine_rs` (`HttpJournalSinkBase`); this module
keeps the import path, the public names, the urllib I/O shell (`_default_http_request`) and
the stamp helpers the tests still read.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import datetime
from typing import Any, Callable

from trade_engine.sim._rs import call, rs

HttpHandler = Callable[[str, str, dict[str, Any] | None], tuple[int, dict[str, Any]]]


class JournalAuthError(RuntimeError):
    """The journal refused the request for lack of authentication (401/403)."""


def journal_executed_at(executed_at: datetime, event_seq: int) -> str:
    """The `executedAt` string sent for this event: UTC, real milliseconds, and the ledger
    seq in the three sub-millisecond digits (see module docstring)."""
    return call(rs.journal_stamp, executed_at, event_seq)


def _parse_instant(value: Any) -> datetime | None:
    return call(rs.journal_parse, value)


def _trade_tags(trade: dict[str, Any]) -> list[str]:
    """Tags from a list-view trade (`tags`) or a detail row (`tagsJson`, a JSON string)."""
    return call(rs.journal_tags, trade)


def _default_http_request(
    url: str,
    method: str = "GET",
    body: dict[str, Any] | None = None,
    timeout: float = 10.0,
) -> tuple[int, dict[str, Any]]:
    """Default HTTP request handler using standard library urllib (the I/O shell)."""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json"} if body is not None else {}
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.status
            content = resp.read().decode("utf-8")
            parsed = json.loads(content) if content else {}
            return status, parsed
    except urllib.error.HTTPError as exc:
        content = exc.read().decode("utf-8")
        try:
            parsed = json.loads(content) if content else {}
        except ValueError:
            parsed = {"error": content}
        return exc.code, parsed


class HttpJournalSink(rs.HttpJournalSinkBase):
    """HTTP Journal sink connecting to the trade-journal (:3300) API."""

    def __init__(
        self,
        base_url: str,
        account_id: str,
        *,
        http_client: HttpHandler | None = None,
        name: str = "journal",
    ) -> None:
        super().__init__()
        if not base_url or not base_url.strip():
            raise ValueError("base_url must be non-empty string")
        if not account_id or not account_id.strip():
            raise ValueError("journal account_id must be non-empty string from config (I5)")

        self.base_url = base_url.rstrip("/")
        self.account_id = account_id.strip()
        self.name = name
        self._http = http_client or _default_http_request

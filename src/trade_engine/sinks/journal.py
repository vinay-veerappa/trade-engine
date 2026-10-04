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
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Any, Callable

from trade_engine.interfaces.sinks import JournalExecution, JournalSink
from trade_engine._lifecycle_runtime import journal

HttpHandler = Callable[[str, str, dict[str, Any] | None], tuple[int, dict[str, Any]]]


class JournalAuthError(RuntimeError):
    """The journal refused the request for lack of authentication (401/403)."""


def journal_executed_at(executed_at: datetime, event_seq: int) -> str:
    """The `executedAt` string sent for this event: UTC, real milliseconds, and the ledger
    seq in the three sub-millisecond digits (see module docstring)."""
    return journal("stamp", executed_at, event_seq)


def _parse_instant(value: Any) -> datetime | None:
    return journal("parse", value)


def _trade_tags(trade: dict[str, Any]) -> list[str]:
    """Tags from a list-view trade (`tags`) or a detail row (`tagsJson`, a JSON string)."""
    return journal("tags", trade)


def _default_http_request(
    url: str,
    method: str = "GET",
    body: dict[str, Any] | None = None,
    timeout: float = 10.0,
) -> tuple[int, dict[str, Any]]:
    """Default HTTP request handler using standard library urllib."""
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


class HttpJournalSink(JournalSink):
    """HTTP Journal sink connecting to the trade-journal (:3300) API."""

    def __init__(
        self,
        base_url: str,
        account_id: str,
        *,
        http_client: HttpHandler | None = None,
        name: str = "journal",
    ) -> None:
        if not base_url or not base_url.strip():
            raise ValueError("base_url must be non-empty string")
        if not account_id or not account_id.strip():
            raise ValueError("journal account_id must be non-empty string from config (I5)")

        self.base_url = base_url.rstrip("/")
        self.account_id = account_id.strip()
        self.name = name
        self._http = http_client or _default_http_request

    def _request(self, path: str, method: str, body: dict[str, Any] | None) -> tuple[int, dict[str, Any]]:
        status, parsed = self._http(f"{self.base_url}{path}", method, body)
        if status in (401, 403):
            raise JournalAuthError(
                f"Journal at {self.base_url} refused {method} {path} with {status}: it has a "
                "password configured and this sink does not authenticate"
            )
        return status, parsed if isinstance(parsed, dict) else {}

    def publish(self, event_seq: int, event: Any) -> bool:
        """Publish an event to the journal.

        Converts dictionaries or domain events to JournalExecution and calls publish_execution.
        """
        if isinstance(event, JournalExecution):
            return self.publish_execution(event_seq, event)
        if isinstance(event, dict):
            execution = self._dict_to_execution(event)
            return self.publish_execution(event_seq, execution)
        return False

    def publish_execution(self, event_seq: int, execution: JournalExecution) -> bool:
        """Publish execution, annotate stop/target/tag, and confirm delivery by reading back.

        Raises JournalAuthError when the journal demands a login; returns False for every
        other failure, so the outbox keeps the item queued (I12).
        """
        # 0. Invariant I8: refuse cross-account delivery
        if not journal("account", execution.account_id, self.account_id):
            return False

        # 1. Multiplier first, so the journal's rebuild on insert already prices the fill
        # with it.
        if journal("derivative", execution.multiplier) and not self._ensure_multiplier(execution):
            return False

        # 2. Post the execution
        payload = journal("payload", self.account_id, event_seq, execution)

        try:
            status, body = self._request("/api/executions", "POST", payload)
        except JournalAuthError:
            raise
        except Exception:
            return False

        if not journal("accepted", status):
            return False

        # Verify the journal actually stored or deduplicated the row
        if not journal("stored", body):
            return False

        # 3. Patch trade annotations (stopLoss, profitTarget, strategy tag)
        self._patch_trade_annotations(execution)

        # 4. Invariant I12: Confirm delivery by reading back, not by HTTP 200
        return self.confirm_delivery(event_seq, execution)

    def _read_multipliers(self) -> dict[str, Any] | None:
        status, body = self._request("/api/settings", "GET", None)
        multipliers = body.get("multipliers")
        if status != 200 or not isinstance(multipliers, dict):
            return None
        return multipliers

    def _ensure_multiplier(self, execution: JournalExecution) -> bool:
        """Set `symbol -> multiplier` without dropping anyone else's entry.

        PATCH replaces the whole map, so it is only sent as a merge over a map read just
        before, and the result is read back. A map that cannot be read is never written.
        """
        symbol = journal("symbol", execution.symbol)
        try:
            existing = self._read_multipliers()
            plan = journal("multiplier", existing, symbol, execution.multiplier)
            if plan["action"] == "refuse":
                return False
            if plan["action"] == "done":
                return True
            merged = plan["multipliers"]
            status, _ = self._request("/api/settings", "PATCH", {"multipliers": merged})
            if not journal("accepted", status):
                return False
            return self._read_multipliers() == merged
        except JournalAuthError:
            raise
        except Exception:
            return False

    def confirm_delivery(self, event_seq: int, execution: JournalExecution | None = None) -> bool:
        """Verify delivery confirmation for an execution by reading back from the journal (I12)."""
        if execution is None:
            return False

        symbol = journal("symbol", execution.symbol)
        sent_at = _parse_instant(journal_executed_at(execution.executed_at, event_seq))
        try:
            query = urllib.parse.urlencode({"accounts": self.account_id})
            status, body = self._request(f"/api/trades?{query}", "GET", None)
            if status != 200:
                return False

            trades = body.get("trades", [])
            # Search candidate trades newest first to minimize HTTP lookups
            candidate_trades = journal("candidates", trades, symbol)
            for trade in candidate_trades:
                trade_key = trade.get("key")
                if not trade_key:
                    continue

                # Query detailed trade record with executions
                key_encoded = urllib.parse.quote(trade_key, safe="")
                dt_status, dt_body = self._request(f"/api/trades/{key_encoded}", "GET", None)
                if dt_status != 200:
                    continue

                for fill in dt_body.get("executions", []):
                    if journal("match", execution, fill, dt_body, sent_at):
                        return True

            return False
        except JournalAuthError:
            raise
        except Exception:
            return False

    def _patch_trade_annotations(self, execution: JournalExecution) -> None:
        """Find the matching trade and patch stopLoss, profitTarget, and strategy tags."""
        if not journal("annotations", execution.stop_loss, execution.profit_target, execution.strategy_tag):
            return

        symbol = journal("symbol", execution.symbol)
        try:
            query = urllib.parse.urlencode({"accounts": self.account_id})
            status, body = self._request(f"/api/trades?{query}", "GET", None)
            if status != 200:
                return

            trades = body.get("trades", [])
            # Prioritize open trades first, latest first (reversed)
            matching_trade = journal("annotation_trade", trades, symbol)
            if not matching_trade:
                return

            trade_key = matching_trade.get("key")
            if not trade_key:
                return

            patch_data = journal("patch", execution, matching_trade)

            if patch_data:
                key_encoded = urllib.parse.quote(trade_key, safe="")
                self._request(f"/api/trades/{key_encoded}", "PATCH", patch_data)
        except JournalAuthError:
            raise
        except Exception:
            # The read-back in confirm_delivery decides; a failed PATCH just fails it.
            pass

    def _dict_to_execution(self, d: dict[str, Any]) -> JournalExecution:
        """Convert a dictionary payload into a JournalExecution domain instance (I5: Refuse, never guess)."""
        return JournalExecution(**journal("execution", d))

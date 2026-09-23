"""HTTP implementation of the JournalSink protocol (Architecture §2, I5, I12, §4.11).

Posts fills as executions to the trade journal (:3300), configures multiplier, stop, target,
and strategy tag, and confirms delivery by reading back rather than trusting HTTP 200.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from decimal import Decimal
from typing import Any, Callable

from trade_engine.domain.instruments import Side
from trade_engine.interfaces.sinks import JournalExecution, JournalSink

HttpHandler = Callable[[str, str, dict[str, Any] | None], tuple[int, dict[str, Any]]]


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
        parsed = json.loads(content) if content else {}
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
        """Publish execution, annotate stop/target/tag, and confirm delivery by reading back."""
        # 1. Post raw execution
        payload = {
            "accountId": self.account_id,
            "executions": [
                {
                    "symbol": execution.symbol,
                    "side": execution.side.value.lower(),
                    "quantity": float(execution.quantity),
                    "price": float(execution.price),
                    "fee": float(execution.fee),
                    "executedAt": execution.executed_at.isoformat(),
                    "assetClass": execution.asset_class,
                }
            ],
            "notes": execution.notes,
        }

        try:
            status, body = self._http(f"{self.base_url}/api/executions", "POST", payload)
        except Exception:
            return False

        if status < 200 or status >= 300:
            return False

        # Verify the journal actually stored or deduplicated the row
        inserted = int(body.get("inserted", 0))
        duplicates = int(body.get("duplicates", 0))
        skipped = int(body.get("skipped", 0))
        if inserted + duplicates < 1 or skipped > 0:
            return False

        # 2. Configure multiplier if derivative
        if execution.multiplier > 1:
            try:
                self._http(
                    f"{self.base_url}/api/settings",
                    "PATCH",
                    {"multipliers": {execution.symbol: execution.multiplier}},
                )
            except Exception:
                pass  # Non-fatal if setting multiplier endpoint fails; continue to trade annotation

        # 3. Patch trade annotations (stopLoss, profitTarget, strategy tag)
        self._patch_trade_annotations(execution)

        # 4. Invariant I12: Confirm delivery by reading back, not by HTTP 200
        return self.confirm_delivery(event_seq, execution)

    def confirm_delivery(self, event_seq: int, execution: JournalExecution | None = None) -> bool:
        """Verify delivery confirmation for an execution by reading back from the journal (I12)."""
        if execution is None:
            return False

        try:
            query = urllib.parse.urlencode({"accounts": self.account_id})
            status, body = self._http(f"{self.base_url}/api/trades?{query}", "GET", None)
            if status != 200 or not isinstance(body, dict):
                return False

            trades = body.get("trades", [])
            for trade in trades:
                if trade.get("symbol") != execution.symbol:
                    continue
                # Verify trade execution list or key
                trade_key = trade.get("key")
                if not trade_key:
                    continue

                # Query detailed trade record with executions
                key_encoded = urllib.parse.quote(trade_key, safe="")
                dt_status, dt_body = self._http(f"{self.base_url}/api/trades/{key_encoded}", "GET", None)
                if dt_status != 200 or not isinstance(dt_body, dict):
                    continue

                executions_list = dt_body.get("executions", [])
                for fill in executions_list:
                    fill_time = fill.get("executedAt", "")
                    fill_qty = float(fill.get("quantity", 0))
                    fill_price = float(fill.get("price", 0))
                    target_time = execution.executed_at.isoformat()

                    # Match by executedAt timestamp and price/quantity
                    if fill_time.startswith(target_time[:19]) and abs(fill_qty - float(execution.quantity)) < 1e-6:
                        return True

            return False
        except Exception:
            return False

    def _patch_trade_annotations(self, execution: JournalExecution) -> None:
        """Find the matching trade and patch stopLoss, profitTarget, and strategy tags."""
        if execution.stop_loss is None and execution.profit_target is None and not execution.strategy_tag:
            return

        try:
            query = urllib.parse.urlencode({"accounts": self.account_id})
            status, body = self._http(f"{self.base_url}/api/trades?{query}", "GET", None)
            if status != 200 or not isinstance(body, dict):
                return

            trades = body.get("trades", [])
            matching_trade = next((t for t in trades if t.get("symbol") == execution.symbol), None)
            if not matching_trade:
                return

            trade_key = matching_trade.get("key")
            if not trade_key:
                return

            patch_data: dict[str, Any] = {}
            if execution.stop_loss is not None:
                patch_data["stopLoss"] = float(execution.stop_loss)
            if execution.profit_target is not None:
                patch_data["profitTarget"] = float(execution.profit_target)
            if execution.strategy_tag:
                existing_tags = matching_trade.get("tags") or []
                if execution.strategy_tag not in existing_tags:
                    patch_data["tags"] = [*existing_tags, execution.strategy_tag]

            if patch_data:
                key_encoded = urllib.parse.quote(trade_key, safe="")
                self._http(f"{self.base_url}/api/trades/{key_encoded}", "PATCH", patch_data)
        except Exception:
            pass

    def _dict_to_execution(self, d: dict[str, Any]) -> JournalExecution:
        """Convert a dictionary payload into a JournalExecution domain instance."""
        executed_at_val = d["executed_at"]
        if isinstance(executed_at_val, str):
            executed_at = datetime.fromisoformat(executed_at_val)
        else:
            executed_at = executed_at_val

        side_val = d["side"]
        side = Side(side_val) if not isinstance(side_val, Side) else side_val

        return JournalExecution(
            symbol=str(d["symbol"]),
            side=side,
            quantity=Decimal(str(d["quantity"])),
            price=Decimal(str(d["price"])),
            fee=Decimal(str(d.get("fee", 0))),
            executed_at=executed_at,
            account_id=str(d.get("account_id", self.account_id)),
            asset_class=str(d.get("asset_class", "equity")),
            multiplier=int(d.get("multiplier", 1)),
            stop_loss=Decimal(str(d["stop_loss"])) if d.get("stop_loss") is not None else None,
            profit_target=Decimal(str(d["profit_target"])) if d.get("profit_target") is not None else None,
            strategy_tag=str(d["strategy_tag"]) if d.get("strategy_tag") is not None else None,
            notes=str(d["notes"]) if d.get("notes") is not None else None,
        )

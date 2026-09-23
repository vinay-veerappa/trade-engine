"""Tests for HttpJournalSink, delivery confirmation by read-back, and trade annotations (WP E5, I5, I12)."""

from __future__ import annotations

import json
import threading
import urllib.parse
from datetime import datetime, timezone
from decimal import Decimal
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest

from trade_engine.clock.replay import ReplayClock
from trade_engine.domain.instruments import Side
from trade_engine.interfaces.sinks import JournalExecution
from trade_engine.ledger.store import Ledger
from trade_engine.sinks.journal import HttpJournalSink

T0 = datetime(2026, 9, 23, 15, 30, 0, tzinfo=timezone.utc)


class _FakeJournalHandler(BaseHTTPRequestHandler):
    """Mock HTTP server simulating the :3300 trade-journal API."""

    def do_POST(self) -> None:
        if self.path == "/api/executions":
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
            server: _FakeJournalServer = self.server  # type: ignore

            if server.mode == "error":
                self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "DB crash"})
                return

            if server.mode == "skipped":
                self._send_json(HTTPStatus.OK, {"inserted": 0, "duplicates": 0, "skipped": 1, "skippedReasons": ["Invalid fee"]})
                return

            executions = body.get("executions", [])
            server.posted_executions.extend(executions)

            # Store trade for read-back unless mode is read_back_miss
            if server.mode != "read_back_miss":
                for ex in executions:
                    sym = ex["symbol"]
                    trade_key = f"trade-{sym}"
                    server.trades[trade_key] = {
                        "key": trade_key,
                        "symbol": sym,
                        "accountId": body.get("accountId"),
                        "status": "open",
                        "tags": [],
                        "stopLoss": None,
                        "profitTarget": None,
                    }
                    if server.mode == "price_mismatch":
                        server.trade_executions[trade_key] = [{**ex, "price": 999.0}]
                    else:
                        server.trade_executions[trade_key] = [ex]

            self._send_json(HTTPStatus.OK, {"inserted": len(executions), "duplicates": 0, "skipped": 0})
        else:
            self.send_error(HTTPStatus.NOT_FOUND)

    def do_PATCH(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
        server: _FakeJournalServer = self.server  # type: ignore

        if server.mode == "patch_fail":
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "Annotation write failed"})
            return

        if self.path == "/api/settings":
            server.settings_multipliers.update(body.get("multipliers", {}))
            self._send_json(HTTPStatus.OK, {"updated": True})
        elif self.path.startswith("/api/trades/"):
            trade_key = urllib.parse.unquote(self.path.split("/")[-1])
            if trade_key in server.trades:
                trade = server.trades[trade_key]
                if "stopLoss" in body:
                    trade["stopLoss"] = body["stopLoss"]
                if "profitTarget" in body:
                    trade["profitTarget"] = body["profitTarget"]
                if "tags" in body:
                    trade["tags"] = body["tags"]
                self._send_json(HTTPStatus.OK, {"updated": True})
            else:
                self.send_error(HTTPStatus.NOT_FOUND)
        else:
            self.send_error(HTTPStatus.NOT_FOUND)

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        server: _FakeJournalServer = self.server  # type: ignore

        if parsed.path == "/api/trades":
            self._send_json(HTTPStatus.OK, {"trades": list(server.trades.values())})
        elif parsed.path.startswith("/api/trades/"):
            trade_key = urllib.parse.unquote(parsed.path.split("/")[-1])
            if trade_key in server.trades:
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "trade": server.trades[trade_key],
                        "executions": server.trade_executions.get(trade_key, []),
                    },
                )
            else:
                self.send_error(HTTPStatus.NOT_FOUND)
        else:
            self.send_error(HTTPStatus.NOT_FOUND)

    def _send_json(self, status: HTTPStatus, body: Any) -> None:
        encoded = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, format: str, *args: Any) -> None:
        """Suppress stdout/stderr log spam."""


class _FakeJournalServer(HTTPServer):
    def __init__(self, host: str = "127.0.0.1", port: int = 0) -> None:
        super().__init__((host, port), _FakeJournalHandler)
        self.mode = "normal"
        self.posted_executions: list[dict[str, Any]] = []
        self.trades: dict[str, dict[str, Any]] = {}
        self.trade_executions: dict[str, list[dict[str, Any]]] = {}
        self.settings_multipliers: dict[str, int] = {}


@pytest.fixture
def fake_journal() -> _FakeJournalServer:
    server = _FakeJournalServer()
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        t.join(timeout=2.0)


def test_journal_sink_config_requires_non_empty_account_id() -> None:
    with pytest.raises(ValueError, match="account_id must be non-empty"):
        HttpJournalSink(base_url="http://localhost:3300", account_id="")

    with pytest.raises(ValueError, match="account_id must be non-empty"):
        HttpJournalSink(base_url="http://localhost:3300", account_id="   ")

    with pytest.raises(ValueError, match="base_url must be non-empty"):
        HttpJournalSink(base_url="", account_id="acc_valid")


def test_journal_sink_delivery_success_confirmed_by_readback(
    fake_journal: _FakeJournalServer,
) -> None:
    """Acceptance: Delivery is confirmed by reading back; sets assetClass, multiplier, stop, target, strategy tag."""
    port = fake_journal.server_port
    sink = HttpJournalSink(base_url=f"http://127.0.0.1:{port}", account_id="acc_breakout_50k")

    execution = JournalExecution(
        symbol="AAPL",
        side=Side.BUY,
        quantity=Decimal("100"),
        price=Decimal("150.25"),
        fee=Decimal("1.00"),
        executed_at=T0,
        account_id="acc_breakout_50k",
        asset_class="equity",
        multiplier=100,  # e.g. option multiplier or derivative
        stop_loss=Decimal("145.00"),
        profit_target=Decimal("160.00"),
        strategy_tag="breakout_ep",
        notes="Gap breakout setup",
    )

    ok = sink.publish(1, execution)
    assert ok is True

    # Verify executions were posted with correct fields
    assert len(fake_journal.posted_executions) == 1
    raw = fake_journal.posted_executions[0]
    assert raw["symbol"] == "AAPL"
    assert raw["side"] == "buy"
    assert raw["quantity"] == 100.0
    assert raw["price"] == 150.25
    assert raw["fee"] == 1.0
    assert raw["assetClass"] == "equity"
    assert raw["executedAt"] == T0.isoformat()

    # Verify multiplier was configured
    assert fake_journal.settings_multipliers.get("AAPL") == 100

    # Verify trade was annotated with stop, target, strategy tag
    trade = fake_journal.trades.get("trade-AAPL")
    assert trade is not None
    assert trade["stopLoss"] == 145.0
    assert trade["profitTarget"] == 160.0
    assert "breakout_ep" in trade["tags"]


def test_journal_sink_delivery_refused_on_skipped_even_with_http_200(
    fake_journal: _FakeJournalServer,
) -> None:
    """Invariant I12: HTTP 200 with skipped > 0 must not be treated as success."""
    fake_journal.mode = "skipped"
    port = fake_journal.server_port
    sink = HttpJournalSink(base_url=f"http://127.0.0.1:{port}", account_id="acc_breakout_50k")

    execution = JournalExecution(
        symbol="AAPL",
        side=Side.BUY,
        quantity=Decimal("10"),
        price=Decimal("150.00"),
        fee=Decimal("0.50"),
        executed_at=T0,
        account_id="acc_breakout_50k",
        asset_class="equity",
    )

    ok = sink.publish(1, execution)
    assert ok is False


def test_journal_sink_delivery_refused_when_readback_fails_despite_http_200(
    fake_journal: _FakeJournalServer,
) -> None:
    """Acceptance: Delivery is confirmed by reading back, NOT by HTTP 200."""
    fake_journal.mode = "read_back_miss"
    port = fake_journal.server_port
    sink = HttpJournalSink(base_url=f"http://127.0.0.1:{port}", account_id="acc_breakout_50k")

    execution = JournalExecution(
        symbol="MSFT",
        side=Side.BUY,
        quantity=Decimal("10"),
        price=Decimal("400.00"),
        fee=Decimal("0.50"),
        executed_at=T0,
        account_id="acc_breakout_50k",
        asset_class="equity",
    )

    ok = sink.publish(1, execution)
    # Even though POST returned 200 with inserted=1, read-back could not find it!
    assert ok is False


def test_journal_sink_unreachable_endpoint_handled_gracefully() -> None:
    # Port 1 is not running an HTTP server
    sink = HttpJournalSink(base_url="http://127.0.0.1:1", account_id="acc_breakout_50k")
    execution = JournalExecution(
        symbol="AAPL",
        side=Side.BUY,
        quantity=Decimal("10"),
        price=Decimal("150.00"),
        fee=Decimal("0"),
        executed_at=T0,
        account_id="acc_breakout_50k",
        asset_class="equity",
    )
    ok = sink.publish(1, execution)
    assert ok is False


def test_outbox_drain_with_journal_sink_stops_on_failure(
    tmp_path: Path, fake_journal: _FakeJournalServer
) -> None:
    """End-to-end integration: Ledger outbox drains with HttpJournalSink."""
    clock = ReplayClock(T0)
    db_file = tmp_path / "test_ledger_journal.db"
    port = fake_journal.server_port

    sink = HttpJournalSink(base_url=f"http://127.0.0.1:{port}", account_id="acc_50k")

    with Ledger(db_file) as lg:
        # Seed an event
        ev = lg.append(
            JournalExecution(  # Use any payload
                symbol="AAPL",
                side=Side.BUY,
                quantity=Decimal("10"),
                price=Decimal("100"),
                fee=Decimal("0"),
                executed_at=T0,
                account_id="acc_50k",
                asset_class="equity",
            )
        ) if False else None

        # Seed two events properly
        from trade_engine.domain.orders import Order, OrderType
        from trade_engine.domain.instruments import Equity
        from trade_engine.ledger.events import Event, EventKind

        o1 = Order("o1", "acc_50k", Equity("AAPL"), OrderType.LIMIT, Side.BUY, Decimal("10"), "c1", T0, limit_price=Decimal("150"))
        o2 = Order("o2", "acc_50k", Equity("MSFT"), OrderType.LIMIT, Side.BUY, Decimal("10"), "c2", T0, limit_price=Decimal("400"))
        ev1 = lg.append(Event("acc_50k", EventKind.ORDER_SUBMITTED, o1, T0, command_id="c1"))
        ev2 = lg.append(Event("acc_50k", EventKind.ORDER_SUBMITTED, o2, T0, command_id="c2"))

        item1 = lg.enqueue_outbox(
            ev1.seq,
            "journal",
            {
                "symbol": "AAPL",
                "side": "BUY",
                "quantity": "10",
                "price": "150.00",
                "fee": "0",
                "executed_at": T0.isoformat(),
                "account_id": "acc_50k",
                "asset_class": "equity",
            },
        )
        item2 = lg.enqueue_outbox(
            ev2.seq,
            "journal",
            {
                "symbol": "MSFT",
                "side": "BUY",
                "quantity": "5",
                "price": "400.00",
                "fee": "0",
                "executed_at": T0.isoformat(),
                "account_id": "acc_50k",
                "asset_class": "equity",
            },
        )

        # 1. Drain successfully
        res = lg.drain_outbox("journal", lambda it: sink.publish(it.event_seq, it.payload), clock)
        assert res.ok
        assert res.drained_count == 2
        assert lg.pending_outbox("journal", include_failed=True) == []

        # 2. Add another item, but make journal fail
        fake_journal.mode = "skipped"
        item3 = lg.enqueue_outbox(
            ev1.seq,
            "journal",
            {
                "symbol": "TSLA",
                "side": "BUY",
                "quantity": "2",
                "price": "200.00",
                "fee": "0",
                "executed_at": T0.isoformat(),
                "account_id": "acc_50k",
                "asset_class": "equity",
            },
        )
        item4 = lg.enqueue_outbox(
            ev2.seq,
            "journal",
            {
                "symbol": "GOOG",
                "side": "BUY",
                "quantity": "4",
                "price": "170.00",
                "fee": "0",
                "executed_at": T0.isoformat(),
                "account_id": "acc_50k",
                "asset_class": "equity",
            },
        )

        res2 = lg.drain_outbox("journal", lambda it: sink.publish(it.event_seq, it.payload), clock)
        assert not res2.ok
        assert res2.drained_count == 0
        assert res2.failed_item is not None and res2.failed_item.id == item3.id
        # item4 is still queued!
        pending = lg.pending_outbox("journal", include_failed=True)
        assert len(pending) == 2
        assert pending[0].id == item3.id
        assert pending[1].id == item4.id


def test_journal_sink_delivery_refused_on_account_mismatch(fake_journal: _FakeJournalServer) -> None:
    """Invariant I8: Execution account must match configured sink account."""
    port = fake_journal.server_port
    sink = HttpJournalSink(base_url=f"http://127.0.0.1:{port}", account_id="acc_main")

    execution = JournalExecution(
        symbol="AAPL",
        side=Side.BUY,
        quantity=Decimal("10"),
        price=Decimal("150.00"),
        fee=Decimal("0"),
        executed_at=T0,
        account_id="acc_other",  # Mismatch!
        asset_class="equity",
    )
    assert sink.publish(1, execution) is False
    assert len(fake_journal.posted_executions) == 0


def test_journal_sink_delivery_refused_when_readback_price_mismatches(fake_journal: _FakeJournalServer) -> None:
    """Delivery confirmation must verify execution price, not just timestamp and quantity."""
    fake_journal.mode = "price_mismatch"
    port = fake_journal.server_port
    sink = HttpJournalSink(base_url=f"http://127.0.0.1:{port}", account_id="acc_50k")

    execution = JournalExecution(
        symbol="AAPL",
        side=Side.BUY,
        quantity=Decimal("10"),
        price=Decimal("150.00"),
        fee=Decimal("0"),
        executed_at=T0,
        account_id="acc_50k",
        asset_class="equity",
    )
    assert sink.publish(1, execution) is False


def test_journal_sink_annotates_open_trade_when_symbol_has_historical_closed_trade(fake_journal: _FakeJournalServer) -> None:
    """When an account has an old closed trade in AAPL, annotations must patch the new open trade."""
    port = fake_journal.server_port
    # Seed historical closed trade first
    fake_journal.trades["trade-AAPL-old"] = {
        "key": "trade-AAPL-old",
        "symbol": "AAPL",
        "accountId": "acc_50k",
        "status": "closed",
        "tags": ["old_strat"],
        "stopLoss": 100.0,
        "profitTarget": 120.0,
    }
    sink = HttpJournalSink(base_url=f"http://127.0.0.1:{port}", account_id="acc_50k")

    execution = JournalExecution(
        symbol="AAPL",
        side=Side.BUY,
        quantity=Decimal("50"),
        price=Decimal("155.00"),
        fee=Decimal("1.00"),
        executed_at=T0,
        account_id="acc_50k",
        asset_class="equity",
        stop_loss=Decimal("150.00"),
        profit_target=Decimal("170.00"),
        strategy_tag="new_strat",
    )
    ok = sink.publish(1, execution)
    assert ok is True

    # Old trade must be untouched
    old_trade = fake_journal.trades["trade-AAPL-old"]
    assert old_trade["stopLoss"] == 100.0
    assert old_trade["tags"] == ["old_strat"]

    # New open trade must receive annotations
    new_trade = fake_journal.trades["trade-AAPL"]
    assert new_trade["stopLoss"] == 150.0
    assert new_trade["profitTarget"] == 170.0
    assert "new_strat" in new_trade["tags"]


def test_journal_sink_delivery_refused_when_annotation_readback_mismatches(fake_journal: _FakeJournalServer) -> None:
    """Delivery confirmation must verify stopLoss/target annotations when requested."""
    fake_journal.mode = "patch_fail"
    port = fake_journal.server_port
    sink = HttpJournalSink(base_url=f"http://127.0.0.1:{port}", account_id="acc_50k")

    execution = JournalExecution(
        symbol="AAPL",
        side=Side.BUY,
        quantity=Decimal("10"),
        price=Decimal("150.00"),
        fee=Decimal("0"),
        executed_at=T0,
        account_id="acc_50k",
        asset_class="equity",
        stop_loss=Decimal("145.00"),
    )
    # PATCH fails, so stopLoss is not applied -> confirm_delivery must reject!
    assert sink.publish(1, execution) is False


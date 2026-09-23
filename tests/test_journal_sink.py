"""Tests for HttpJournalSink, delivery confirmation by read-back, and trade annotations (WP E5, I5, I12)."""

from __future__ import annotations

import hashlib
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
            inserted = 0
            duplicates = 0
            for ex in executions:
                h = server.calc_hash(ex)
                if h in server.hashes:
                    duplicates += 1
                else:
                    server.hashes.add(h)
                    inserted += 1
                    server.posted_executions.append(ex)

                    # Store trade for read-back unless mode is read_back_miss
                    if server.mode != "read_back_miss":
                        sym = ex["symbol"]
                        trade_key = f"trade-{sym}"
                        if trade_key not in server.trades:
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
                            server.trade_executions.setdefault(trade_key, []).append({**ex, "price": 999.0})
                        elif server.mode == "fee_mismatch":
                            server.trade_executions.setdefault(trade_key, []).append({**ex, "fee": 999.0})
                        elif server.mode == "asset_class_mismatch":
                            server.trade_executions.setdefault(trade_key, []).append({**ex, "assetClass": "crypto"})
                        else:
                            server.trade_executions.setdefault(trade_key, []).append(ex)

            self._send_json(HTTPStatus.OK, {"inserted": inserted, "duplicates": duplicates, "skipped": 0})
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
            # Real journal replaces the entire map!
            server.settings_multipliers = dict(body.get("multipliers", {}))
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

        if parsed.path == "/api/settings":
            self._send_json(HTTPStatus.OK, {"settings": {"multipliers": server.settings_multipliers}})
        elif parsed.path == "/api/trades":
            self._send_json(HTTPStatus.OK, {"trades": list(server.trades.values())})
        elif parsed.path.startswith("/api/trades/"):
            trade_key = urllib.parse.unquote(parsed.path.split("/")[-1])
            if trade_key in server.trades:
                raw_trade = dict(server.trades[trade_key])
                # Real journal detail endpoint returns tagsJson as JSON string and contractMultiplier
                tags = raw_trade.pop("tags", [])
                raw_trade["tagsJson"] = json.dumps(tags)
                raw_trade["contractMultiplier"] = server.settings_multipliers.get(raw_trade.get("symbol"), 1)
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "trade": raw_trade,
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
        self.hashes: set[str] = set()

    def calc_hash(self, ex: dict[str, Any]) -> str:
        meta = ex.get("importMetadata")
        parts = [
            str(ex.get("symbol")),
            str(ex.get("side")),
            f"{float(ex.get('quantity', 0)):.12g}",
            f"{float(ex.get('price', 0)):.12g}",
            str(ex.get("executedAt")),
        ]
        if meta and "id" in meta:
            parts.extend(["history", meta.get("group", ""), str(meta["id"])])
        return hashlib.sha256("|".join(parts).encode()).hexdigest()[:32]


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
        o3 = Order("o3", "acc_50k", Equity("TSLA"), OrderType.LIMIT, Side.BUY, Decimal("2"), "c3", T0, limit_price=Decimal("200"))
        o4 = Order("o4", "acc_50k", Equity("GOOG"), OrderType.LIMIT, Side.BUY, Decimal("4"), "c4", T0, limit_price=Decimal("170"))
        ev1 = lg.append(Event("acc_50k", EventKind.ORDER_SUBMITTED, o1, T0, command_id="c1"))
        ev2 = lg.append(Event("acc_50k", EventKind.ORDER_SUBMITTED, o2, T0, command_id="c2"))
        ev3 = lg.append(Event("acc_50k", EventKind.ORDER_SUBMITTED, o3, T0, command_id="c3"))
        ev4 = lg.append(Event("acc_50k", EventKind.ORDER_SUBMITTED, o4, T0, command_id="c4"))

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
                "multiplier": 1,
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
                "multiplier": 1,
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
            ev3.seq,
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
                "multiplier": 1,
            },
        )
        item4 = lg.enqueue_outbox(
            ev4.seq,
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
                "multiplier": 1,
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


def test_journal_sink_delivery_refused_when_strategy_tag_mismatches_on_readback(
    fake_journal: _FakeJournalServer,
) -> None:
    """Pins mutation: Removing strategy_tag check from readback MUST fail."""
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
        strategy_tag="breakout_vol",
    )
    # PATCH fails, so tag is not applied -> confirm_delivery must reject!
    assert sink.publish(1, execution) is False


def test_journal_sink_preserves_existing_multipliers_on_update(
    fake_journal: _FakeJournalServer,
) -> None:
    """Must-Fix 1: Setting a multiplier must merge with existing multipliers, not wipe them."""
    # Pre-populate settings multipliers in the fake journal
    fake_journal.settings_multipliers = {"ES": 50, "NQ": 20}
    port = fake_journal.server_port
    sink = HttpJournalSink(base_url=f"http://127.0.0.1:{port}", account_id="acc_50k")

    execution = JournalExecution(
        symbol="AAPL260116C150",
        side=Side.BUY,
        quantity=Decimal("5"),
        price=Decimal("12.50"),
        fee=Decimal("2.00"),
        executed_at=T0,
        account_id="acc_50k",
        asset_class="option",
        multiplier=100,
    )

    ok = sink.publish(1, execution)
    assert ok is True

    # Multipliers map must still have ES and NQ preserved, plus the new option multiplier
    assert fake_journal.settings_multipliers == {
        "ES": 50,
        "NQ": 20,
        "AAPL260116C150": 100,
    }


def test_journal_sink_identical_fills_with_distinct_fill_ids_both_succeed(
    fake_journal: _FakeJournalServer,
) -> None:
    """Must-Fix 3: Two identical fills at the same time with distinct fill_ids both insert."""
    port = fake_journal.server_port
    sink = HttpJournalSink(base_url=f"http://127.0.0.1:{port}", account_id="acc_50k")

    ex1 = JournalExecution(
        symbol="AAPL",
        side=Side.BUY,
        quantity=Decimal("10"),
        price=Decimal("150.00"),
        fee=Decimal("1.00"),
        executed_at=T0,
        account_id="acc_50k",
        asset_class="equity",
        fill_id="fill-001",
    )
    ex2 = JournalExecution(
        symbol="AAPL",
        side=Side.BUY,
        quantity=Decimal("10"),
        price=Decimal("150.00"),
        fee=Decimal("1.00"),
        executed_at=T0,
        account_id="acc_50k",
        asset_class="equity",
        fill_id="fill-002",
    )

    assert sink.publish(1, ex1) is True
    assert sink.publish(2, ex2) is True
    assert len(fake_journal.posted_executions) == 2


def test_journal_sink_duplicate_fill_without_distinct_id_collides(
    fake_journal: _FakeJournalServer,
) -> None:
    """Two executions with identical fill_id collide in hash deduplication."""
    port = fake_journal.server_port
    sink = HttpJournalSink(base_url=f"http://127.0.0.1:{port}", account_id="acc_50k")

    ex1 = JournalExecution(
        symbol="AAPL",
        side=Side.BUY,
        quantity=Decimal("10"),
        price=Decimal("150.00"),
        fee=Decimal("1.00"),
        executed_at=T0,
        account_id="acc_50k",
        asset_class="equity",
        fill_id="fill-same",
    )
    ex2 = JournalExecution(
        symbol="AAPL",
        side=Side.BUY,
        quantity=Decimal("10"),
        price=Decimal("150.00"),
        fee=Decimal("1.00"),
        executed_at=T0,
        account_id="acc_50k",
        asset_class="equity",
        fill_id="fill-same",
    )

    assert sink.publish(1, ex1) is True
    assert len(fake_journal.posted_executions) == 1
    # Second publish is detected as duplicate by hash, and confirm_delivery still verifies it
    assert sink.publish(2, ex2) is True
    assert len(fake_journal.posted_executions) == 1


def test_dict_payload_missing_required_fields_refused_i5() -> None:
    """Must-Fix 7: Missing fields in dict payload must raise ValueError (Invariant I5: Refuse, never guess)."""
    sink = HttpJournalSink(base_url="http://127.0.0.1:3300", account_id="acc_50k")

    incomplete_dict = {
        "symbol": "AAPL",
        "side": "BUY",
        "quantity": "10",
        "price": "150.00",
        "fee": "0",
        "executed_at": T0.isoformat(),
        # Missing account_id, asset_class, multiplier!
    }

    with pytest.raises(ValueError, match="Missing required field 'account_id'"):
        sink.publish(1, incomplete_dict)

    incomplete_dict["account_id"] = "acc_50k"
    with pytest.raises(ValueError, match="Missing required field 'asset_class'"):
        sink.publish(1, incomplete_dict)

    incomplete_dict["asset_class"] = "equity"
    with pytest.raises(ValueError, match="Missing required field 'multiplier'"):
        sink.publish(1, incomplete_dict)


def test_dict_payload_cross_account_refused_i8(fake_journal: _FakeJournalServer) -> None:
    """Must-Fix 7 / Invariant I8: Cross-account dict payload is refused."""
    port = fake_journal.server_port
    sink = HttpJournalSink(base_url=f"http://127.0.0.1:{port}", account_id="acc_50k")

    dict_payload = {
        "symbol": "AAPL",
        "side": "BUY",
        "quantity": "10",
        "price": "150.00",
        "fee": "0",
        "executed_at": T0.isoformat(),
        "account_id": "other_account",
        "asset_class": "equity",
        "multiplier": 1,
    }

    assert sink.publish(1, dict_payload) is False
    assert len(fake_journal.posted_executions) == 0


def test_readback_verifies_fee_and_asset_class(fake_journal: _FakeJournalServer) -> None:
    """Should-Fix: Readback verification checks fee and assetClass."""
    port = fake_journal.server_port
    sink = HttpJournalSink(base_url=f"http://127.0.0.1:{port}", account_id="acc_50k")

    execution = JournalExecution(
        symbol="AAPL",
        side=Side.BUY,
        quantity=Decimal("10"),
        price=Decimal("150.00"),
        fee=Decimal("1.50"),
        executed_at=T0,
        account_id="acc_50k",
        asset_class="equity",
    )

    fake_journal.mode = "fee_mismatch"
    assert sink.publish(1, execution) is False

    fake_journal.mode = "asset_class_mismatch"
    assert sink.publish(1, execution) is False



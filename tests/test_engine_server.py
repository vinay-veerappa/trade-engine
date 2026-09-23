"""Tests for EngineHttpServer (WP E5, Architecture §4.11).

Matches web/engine/server.ts endpoints:
  GET /health
  GET /snapshot
  GET /events?after=<seq> (SSE stream)
"""

from __future__ import annotations

import json
import threading
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from trade_engine.domain.instruments import Equity, Side
from trade_engine.domain.orders import Order, OrderType
from trade_engine.ledger.events import Event, EventKind
from trade_engine.ledger.store import Ledger
from trade_engine.server.http import EngineHttpServer

T0 = datetime(2026, 9, 23, 16, 0, 0, tzinfo=timezone.utc)
T1 = datetime(2026, 9, 23, 16, 1, 0, tzinfo=timezone.utc)


@pytest.fixture
def ledger(tmp_path: Path) -> Ledger:
    db_file = tmp_path / "test_server_ledger.db"
    with Ledger(db_file) as lg:
        yield lg


@pytest.fixture
def server(ledger: Ledger) -> EngineHttpServer:
    srv = EngineHttpServer(ledger, host="127.0.0.1", port=0, ping_interval=2.0)
    srv.start()
    try:
        yield srv
    finally:
        srv.stop()


def _seed_events(ledger: Ledger) -> list[Event]:
    o1 = Order("o1", "ACC_A", Equity("AAPL"), OrderType.LIMIT, Side.BUY, Decimal("10"), "c1", T0, limit_price=Decimal("150.00"))
    o2 = Order("o2", "ACC_A", Equity("MSFT"), OrderType.LIMIT, Side.BUY, Decimal("20"), "c2", T1, limit_price=Decimal("400.00"))
    ev1 = ledger.append(Event("ACC_A", EventKind.ORDER_SUBMITTED, o1, T0, command_id="c1"))
    ev2 = ledger.append(Event("ACC_A", EventKind.ORDER_SUBMITTED, o2, T1, command_id="c2"))
    return [ev1, ev2]


def test_server_bind_to_non_localhost_raises(ledger: Ledger) -> None:
    with pytest.raises(ValueError, match="localhost only"):
        EngineHttpServer(ledger, host="0.0.0.0", port=3410)

    with pytest.raises(ValueError, match="localhost only"):
        EngineHttpServer(ledger, host="192.168.1.100", port=3410)


def test_health_endpoint(server: EngineHttpServer, ledger: Ledger) -> None:
    _seed_events(ledger)
    url = f"http://127.0.0.1:{server.port}/health"

    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=5.0) as resp:
        assert resp.status == 200
        assert resp.headers.get("Content-Type") == "application/json"
        data = json.loads(resp.read().decode("utf-8"))
        assert data["ok"] is True
        assert data["seq"] == 2
        assert data["count"] == 2


def test_snapshot_endpoint(server: EngineHttpServer, ledger: Ledger) -> None:
    _seed_events(ledger)
    url = f"http://127.0.0.1:{server.port}/snapshot"

    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=5.0) as resp:
        assert resp.status == 200
        assert resp.headers.get("Content-Type") == "application/json"
        data = json.loads(resp.read().decode("utf-8"))
        assert data["seq"] == 2
        assert "ACC_A" in data["accounts"]
        acc = data["accounts"]["ACC_A"]
        assert acc["account_id"] == "ACC_A"
        assert "AAPL" in acc["orders"]["o1"]["instrument"]
        assert "MSFT" in acc["orders"]["o2"]["instrument"]


def test_events_sse_endpoint_backlog_and_reconnect(
    server: EngineHttpServer, ledger: Ledger
) -> None:
    _seed_events(ledger)
    # Request events after seq 1 (should return event 2 only)
    url = f"http://127.0.0.1:{server.port}/events?after=1"

    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=5.0) as resp:
        assert resp.status == 200
        assert "text/event-stream" in resp.headers.get("Content-Type", "")

        # Read line by line until we receive event 2
        lines: list[str] = []
        for _ in range(10):
            line = resp.readline().decode("utf-8")
            lines.append(line.strip())
            if line.startswith("id: 2"):
                break

        assert "retry: 1000" in lines
        assert "id: 2" in lines

    # Reconnect using Last-Event-ID header winning over 'after=0'
    url_reconnect = f"http://127.0.0.1:{server.port}/events?after=0"
    req_reconnect = urllib.request.Request(url_reconnect, headers={"Last-Event-ID": "1"})
    with urllib.request.urlopen(req_reconnect, timeout=5.0) as resp:
        lines_reconnect: list[str] = []
        for _ in range(10):
            line = resp.readline().decode("utf-8")
            lines_reconnect.append(line.strip())
            if line.startswith("id: 2"):
                break
        assert "id: 2" in lines_reconnect
        assert "id: 1" not in lines_reconnect


def test_events_sse_live_streaming(server: EngineHttpServer, ledger: Ledger) -> None:
    """Live events broadcast to connected SSE clients."""
    url = f"http://127.0.0.1:{server.port}/events?after=0"

    received_frames: list[str] = []
    stop_client = threading.Event()

    def sse_client():
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=10.0) as resp:
            while not stop_client.is_set():
                line = resp.readline().decode("utf-8")
                if not line:
                    break
                received_frames.append(line.strip())
                if "id: 1" in line:
                    break

    client_thread = threading.Thread(target=sse_client, daemon=True)
    client_thread.start()

    # Give client a moment to connect
    import time
    time.sleep(0.1)

    # Append an event and broadcast it
    o = Order("o1", "ACC_LIVE", Equity("NVDA"), OrderType.LIMIT, Side.BUY, Decimal("5"), "c_live", T0, limit_price=Decimal("120.00"))
    ev = ledger.append(Event("ACC_LIVE", EventKind.ORDER_SUBMITTED, o, T0, command_id="c_live"))
    server.broadcast(ev)

    client_thread.join(timeout=3.0)
    stop_client.set()

    assert any(frame.startswith("id: 1") for frame in received_frames)


def test_server_worker_threads_are_daemon(server: EngineHttpServer) -> None:
    """Worker threads must be daemonic so lingering connections never block process shutdown."""
    assert server._server is not None
    assert server._server.daemon_threads is True


def test_events_sse_deduplication_and_no_drop_during_handover(server: EngineHttpServer, ledger: Ledger) -> None:
    """Events broadcast concurrently during backlog replay must be delivered without drops."""
    _seed_events(ledger)  # events 1 and 2

    # Connect client from seq 0
    url = f"http://127.0.0.1:{server.port}/events?after=0"
    received_ids: list[int] = []
    stop_client = threading.Event()

    def client_worker():
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=10.0) as resp:
            while not stop_client.is_set():
                line = resp.readline().decode("utf-8")
                if not line:
                    break
                line_str = line.strip()
                if line_str.startswith("id: "):
                    ev_id = int(line_str.split(":", 1)[1].strip())
                    received_ids.append(ev_id)
                    if ev_id >= 3:
                        break

    thread = threading.Thread(target=client_worker, daemon=True)
    thread.start()

    # Append and broadcast event 3 immediately
    o3 = Order("o3", "ACC_A", Equity("TSLA"), OrderType.LIMIT, Side.BUY, Decimal("1"), "c3", T1, limit_price=Decimal("200"))
    ev3 = ledger.append(Event("ACC_A", EventKind.ORDER_SUBMITTED, o3, T1, command_id="c3"))
    server.broadcast(ev3)

    thread.join(timeout=4.0)
    stop_client.set()

    assert 1 in received_ids
    assert 2 in received_ids
    assert 3 in received_ids
    # Ensure no duplicates: each event id received at most once
    assert len(received_ids) == len(set(received_ids))


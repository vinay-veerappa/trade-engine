"""Localhost HTTP/SSE server exposing snapshot and events streams (Architecture §4.11).

Binds strictly to 127.0.0.1 (localhost). Matches the shape of web/engine/server.ts:
  GET /health                  JSON status { ok: true, seq: ... }
  GET /snapshot                JSON folded state + seq
  GET /events?after=<seq>      Server-Sent Events from seq (backlog then live)
"""

from __future__ import annotations

import json
import queue
import socket
import threading
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from trade_engine.ledger import codec
from trade_engine.ledger.events import Event
from trade_engine.ledger.state import AccountState
from trade_engine.ledger.store import Ledger


def _state_to_dict(state: AccountState) -> dict[str, Any]:
    """Serialize AccountState to JSON-safe dictionary."""
    return {
        "account_id": state.account_id,
        "cash": str(state.cash),
        "realized_pnl": str(state.realized_pnl),
        "last_seq": state.last_seq,
        "positions": {
            inst.symbol: {
                "symbol": inst.symbol,
                "quantity": str(pos.quantity),
                "avg_cost": str(pos.avg_cost),
                "realized_pnl": str(pos.realized_pnl),
            }
            for inst, pos in state.positions.items()
        },
        "orders": {
            order_id: {
                "order_id": order.order_id,
                "instrument": order.instrument.symbol,
                "side": order.side.value,
                "state": order.state.value,
                "quantity": str(order.quantity),
                "limit_price": str(order.limit_price) if order.limit_price is not None else None,
                "stop_price": str(order.stop_price) if order.stop_price is not None else None,
            }
            for order_id, order in state.orders.items()
        },
    }


class _EngineHandler(BaseHTTPRequestHandler):
    """HTTP request handler for trade engine API."""

    server: _EngineServerInternal

    def do_GET(self) -> None:
        parsed_url = urllib.parse.urlparse(self.path)
        path = parsed_url.path
        query = urllib.parse.parse_qs(parsed_url.query)

        if path == "/health":
            self._handle_health()
        elif path == "/snapshot":
            self._handle_snapshot()
        elif path == "/events":
            self._handle_events(query)
        else:
            self.send_error(HTTPStatus.NOT_FOUND, "Not Found")

    def _handle_health(self) -> None:
        next_seq = self.server.ledger.next_seq()
        current_seq = next_seq - 1
        data = {
            "ok": True,
            "seq": current_seq,
            "count": self.server.ledger.count(),
        }
        self._send_json(HTTPStatus.OK, data)

    def _handle_snapshot(self) -> None:
        next_seq = self.server.ledger.next_seq()
        current_seq = next_seq - 1
        folded = self.server.ledger.fold()
        data = {
            "seq": current_seq,
            "accounts": {
                acc_id: _state_to_dict(st)
                for acc_id, st in folded.items()
            },
        }
        self._send_json(HTTPStatus.OK, data)

    def _handle_events(self, query: dict[str, list[str]]) -> None:
        # Reconnect header 'Last-Event-ID' wins over 'after' query param
        last_event_id = self.headers.get("Last-Event-ID")
        if last_event_id is not None and last_event_id.isdigit():
            after_seq = int(last_event_id)
        else:
            after_param = query.get("after", ["0"])[0]
            after_seq = int(after_param) if after_param.isdigit() else 0

        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

        # Send reconnection retry advice
        self.wfile.write(b"retry: 1000\n\n")
        self.wfile.flush()

        # 1. Backlog events from ledger
        backlog = self.server.ledger.events(after=after_seq)
        for ev in backlog:
            self._write_sse_event(ev)

        # 2. Register for live events
        sub_queue: queue.Queue[Event | None] = queue.Queue()
        self.server.add_subscriber(sub_queue)

        try:
            while self.server.is_running:
                try:
                    event = sub_queue.get(timeout=self.server.ping_interval)
                    if event is None:  # Shutdown signal
                        break
                    self._write_sse_event(event)
                except queue.Empty:
                    # Heartbeat frame to detect disconnected clients
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, socket.error):
            pass
        finally:
            self.server.remove_subscriber(sub_queue)

    def _write_sse_event(self, event: Event) -> None:
        encoded = codec.encode_event(event)
        payload_str = json.dumps(encoded, separators=(",", ":"), sort_keys=True)
        frame = f"id: {event.seq}\ndata: {payload_str}\n\n".encode("utf-8")
        self.wfile.write(frame)
        self.wfile.flush()

    def _send_json(self, status: HTTPStatus, body: Any) -> None:
        encoded = json.dumps(body, separators=(",", ":"), sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, format: str, *args: Any) -> None:
        """Suppress default stderr request logging."""


class _EngineServerInternal(ThreadingHTTPServer):
    """Internal ThreadingHTTPServer holding ledger and SSE subscriber registry."""

    def __init__(
        self,
        server_address: tuple[str, int],
        RequestHandlerClass: type[BaseHTTPRequestHandler],
        ledger: Ledger,
        ping_interval: float = 15.0,
    ) -> None:
        super().__init__(server_address, RequestHandlerClass)
        self.ledger = ledger
        self.ping_interval = ping_interval
        self.is_running = True
        self._subscribers: set[queue.Queue[Event | None]] = set()
        self._lock = threading.Lock()

    def add_subscriber(self, q: queue.Queue[Event | None]) -> None:
        with self._lock:
            self._subscribers.add(q)

    def remove_subscriber(self, q: queue.Queue[Event | None]) -> None:
        with self._lock:
            self._subscribers.discard(q)

    def broadcast(self, event: Event) -> None:
        with self._lock:
            for q in tuple(self._subscribers):
                q.put(event)

    def shutdown_subscribers(self) -> None:
        with self._lock:
            for q in tuple(self._subscribers):
                q.put(None)
            self._subscribers.clear()


class EngineHttpServer:
    """Public wrapper managing the localhost Engine HTTP/SSE server."""

    def __init__(
        self,
        ledger: Ledger,
        *,
        host: str = "127.0.0.1",
        port: int = 3410,
        ping_interval: float = 15.0,
    ) -> None:
        if host not in ("127.0.0.1", "localhost"):
            raise ValueError(f"Engine server must bind to localhost only (127.0.0.1), got {host}")

        self.ledger = ledger
        self.host = host
        self.requested_port = port
        self.ping_interval = ping_interval
        self._server: _EngineServerInternal | None = None
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        """Resolved bound port (especially useful when port 0 is used in tests)."""
        if self._server is None:
            return self.requested_port
        return int(self._server.server_port)

    def start(self) -> EngineHttpServer:
        """Start the server in a background daemon thread."""
        if self._server is not None:
            return self

        self._server = _EngineServerInternal(
            (self.host, self.requested_port),
            _EngineHandler,
            self.ledger,
            ping_interval=self.ping_interval,
        )
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def broadcast(self, event: Event) -> None:
        """Broadcast newly committed event to all active SSE subscribers."""
        if self._server is not None:
            self._server.broadcast(event)

    def stop(self) -> None:
        """Stop server, disconnect subscribers, and close listening socket."""
        if self._server is not None:
            self._server.is_running = False
            self._server.shutdown_subscribers()
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def __enter__(self) -> EngineHttpServer:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()

"""Localhost HTTP/SSE server exposing snapshot and events streams (Architecture §4.11).

Binds strictly to 127.0.0.1 (localhost). Matches the shape of web/engine/server.ts:
  GET /health                  JSON status { ok: true, seq: ... }
  GET /snapshot                JSON folded state + seq
  GET /events?after=<seq>      Server-Sent Events from seq (backlog then live)
"""

from __future__ import annotations

import json
import queue
import re
import socket
import sqlite3
import threading
import urllib.parse
from contextlib import contextmanager
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from trade_engine.ledger import codec
from trade_engine.ledger.events import Event
from trade_engine.ledger.state import AccountState, fold
from trade_engine.ledger.store import Ledger

ALLOWED_ORIGIN = re.compile(r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$", re.IGNORECASE)


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

    @contextmanager
    def _open_reader(self):
        conn = sqlite3.connect(self.server.ledger_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only = ON")
        try:
            yield conn
        finally:
            conn.close()

    def _validate_host(self) -> bool:
        host_header = self.headers.get("Host", "")
        host_name = host_header.split(":")[0].strip().lower() if host_header else ""
        if host_name not in ("127.0.0.1", "localhost"):
            self.send_error(HTTPStatus.FORBIDDEN, "Forbidden: Invalid Host header")
            return False
        return True

    def _get_allowed_origin(self) -> str | None:
        origin = self.headers.get("Origin")
        if origin and ALLOWED_ORIGIN.match(origin.strip()):
            return origin.strip()
        return None

    def do_OPTIONS(self) -> None:
        if not self._validate_host():
            return
        allowed_origin = self._get_allowed_origin()
        self.send_response(HTTPStatus.NO_CONTENT)
        if allowed_origin:
            self.send_header("Access-Control-Allow-Origin", allowed_origin)
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, Last-Event-ID")
        self.end_headers()

    def do_GET(self) -> None:
        if not self._validate_host():
            return
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
        with self._open_reader() as conn:
            row = conn.execute("SELECT COALESCE(MAX(seq), 0) AS max_seq, COUNT(*) AS cnt FROM events").fetchone()
            current_seq = int(row["max_seq"])
            count = int(row["cnt"])
        data = {
            "ok": True,
            "seq": current_seq,
            "count": count,
        }
        self._send_json(HTTPStatus.OK, data)

    def _handle_snapshot(self) -> None:
        with self._open_reader() as conn:
            rows = conn.execute("SELECT * FROM events ORDER BY seq ASC").fetchall()
            events = [Ledger._row_to_event(r) for r in rows]
            current_seq = events[-1].seq if events and events[-1].seq is not None else 0
            folded = fold(events)

        data = {
            "seq": current_seq,
            "accounts": {
                acc_id: _state_to_dict(st)
                for acc_id, st in folded.items()
            },
        }
        self._send_json(HTTPStatus.OK, data)

    def _handle_events(self, query: dict[str, list[str]]) -> None:
        # Validate 'after' query parameter
        after_param = query.get("after")
        if after_param is not None:
            val = after_param[0]
            if not val.isdigit():
                self.send_error(HTTPStatus.BAD_REQUEST, "Bad Request: 'after' parameter must be a non-negative integer")
                return
            after_seq = int(val)
        else:
            after_seq = 0

        # Reconnect header 'Last-Event-ID' wins over 'after' query param
        last_event_id = self.headers.get("Last-Event-ID")
        if last_event_id is not None:
            if not last_event_id.isdigit():
                self.send_error(HTTPStatus.BAD_REQUEST, "Bad Request: 'Last-Event-ID' header must be a non-negative integer")
                return
            after_seq = int(last_event_id)

        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        allowed_origin = self._get_allowed_origin()
        if allowed_origin:
            self.send_header("Access-Control-Allow-Origin", allowed_origin)
        self.end_headers()

        # Send reconnection retry advice
        self.wfile.write(b"retry: 1000\n\n")
        self.wfile.flush()

        # 1. Register for live events first to prevent race-condition drops
        sub_queue: queue.Queue[Event | None] = queue.Queue()
        self.server.add_subscriber(sub_queue)
        max_seq_sent = after_seq

        try:
            # 2. Backlog events from dedicated reader connection
            with self._open_reader() as conn:
                rows = conn.execute("SELECT * FROM events WHERE seq > ? ORDER BY seq ASC", (after_seq,)).fetchall()
                backlog = [Ledger._row_to_event(r) for r in rows]

            for ev in backlog:
                self._write_sse_event(ev)
                if ev.seq is not None and ev.seq > max_seq_sent:
                    max_seq_sent = ev.seq

            # 3. Stream live events from queue
            while self.server.is_running:
                try:
                    event = sub_queue.get(timeout=self.server.ping_interval)
                    if event is None:  # Shutdown signal
                        break
                    # Deduplicate any event already delivered via backlog
                    if event.seq is not None and event.seq <= max_seq_sent:
                        continue
                    self._write_sse_event(event)
                    if event.seq is not None and event.seq > max_seq_sent:
                        max_seq_sent = event.seq
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
        allowed_origin = self._get_allowed_origin()
        if allowed_origin:
            self.send_header("Access-Control-Allow-Origin", allowed_origin)
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, format: str, *args: Any) -> None:
        """Suppress default stderr request logging."""


class _EngineServerInternal(ThreadingHTTPServer):
    """Internal ThreadingHTTPServer holding ledger and SSE subscriber registry."""

    daemon_threads = True

    def __init__(
        self,
        server_address: tuple[str, int],
        RequestHandlerClass: type[BaseHTTPRequestHandler],
        ledger: Ledger,
        ping_interval: float = 15.0,
    ) -> None:
        super().__init__(server_address, RequestHandlerClass)
        self.ledger = ledger
        self.ledger_path = str(ledger.path)
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
        self.ledger.add_listener(self.broadcast)
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
            self.ledger.remove_listener(self.broadcast)
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

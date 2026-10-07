"""Localhost HTTP/SSE compatibility facade; Rust owns transport, readers and streams."""

from __future__ import annotations

from trade_engine.ledger import _rs
from trade_engine.ledger.events import Event
from trade_engine.ledger.store import Ledger


class _EngineHandler:
    """Non-owning cursor compatibility entry point; no Python request workers."""

    def _handle_events(
        self, query: dict[str, list[str]], last_event_id: str | None = None
    ) -> int | None:
        return _rs.rs.http_cursor(query.get("after"), last_event_id)


class EngineHttpServer:
    """The unchanged local server API over the single native module."""

    def __init__(
        self,
        ledger: Ledger,
        *,
        host: str = "127.0.0.1",
        port: int = 3410,
        ping_interval: float = 15.0,
    ) -> None:
        _rs.rs.http_validate_host(host)
        self.ledger = ledger
        self.host = host
        self.requested_port = port
        self.ping_interval = ping_interval
        self._server: _rs.rs.HttpServer | None = None

    @property
    def port(self) -> int:
        return self.requested_port if self._server is None else self._server.server_port

    def start(self) -> EngineHttpServer:
        if self._server is None:
            self._server = _rs.rs.HttpServer(
                str(self.ledger.path), self.host, self.requested_port, self.ping_interval
            )
            self.ledger.add_listener(self.broadcast)
        return self

    def broadcast(self, event: Event) -> None:
        if self._server is not None:
            self._server.broadcast(event)

    def stop(self) -> None:
        if self._server is not None:
            self.ledger.remove_listener(self.broadcast)
            self._server.stop()
            self._server = None

    def __enter__(self) -> EngineHttpServer:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()

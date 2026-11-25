"""W7-CORE-C4: `RiskGuardClient`'s HTTP check now lives in `trade_engine_rs.riskguard_check`.

A local `http.server` on `127.0.0.1:0` stands in for the RiskGuard service (never the real
one), with made-up tokens only. Every case runs `RiskGuardClient.check`, the public seam.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote

import pytest

from trade_engine.oms.precheck import CHECK_TIMEOUT_S, CheckResult, RiskGuardClient

ALLOWED = {"allowed": True, "isReducing": False, "reasonCode": "OK"}
REFUSED = {"allowed": False, "isReducing": False, "reasonCode": "MAX_QTY"}


class Fake:
    """A loopback fake of POST /v1/check that records the path, headers and body."""

    def __init__(self, *, status: int = 200, body=ALLOWED, delay: float = 0.0):
        self.status = status
        self.body = body
        self.delay = delay
        self.requests: list[dict] = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length)
                fake.requests.append(
                    {
                        "path": self.path,
                        "authorization": self.headers.get("Authorization"),
                        "content_type": self.headers.get("Content-Type"),
                        "raw": raw,
                    }
                )
                if fake.delay:
                    time.sleep(fake.delay)
                payload = fake.body if isinstance(fake.body, bytes) else json.dumps(fake.body).encode()
                try:
                    self.send_response(fake.status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def log_message(self, *args) -> None:
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self.base_url = f"http://127.0.0.1:{self._server.server_address[1]}"
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture
def guard():
    server = Fake()
    yield server
    server.close()


def check(guard, core_account: str = "core-1", request: dict | None = None) -> CheckResult:
    client = RiskGuardClient(guard.base_url, "check-token")
    body = request or {
        "accountKey": {"venue": "tradovate", "env": "demo", "brokerAccountId": "123"},
        "instrument": "AAPL",
        "side": "long",
        "qty": 2,
        "isReducing": False,
        "origin": "strategy",
    }
    return client.check(core_account, body)


def test_allowed(guard):
    result = check(guard)
    assert result.allowed is True
    assert result.kind == "allowed"


def test_allowed_returns_the_verdict_revision_and_checked_at(guard):
    guard.body = {**ALLOWED, "verdictRevision": 9, "checkedAt": "2026-11-17T15:00:01.000Z"}
    assert check(guard) == CheckResult(True, "allowed", 9, "2026-11-17T15:00:01.000Z")


def test_refused_with_a_reason_code(guard):
    guard.body = REFUSED
    assert check(guard) == CheckResult(False, "MAX_QTY")


def test_http_500(guard):
    guard.status = 500
    assert check(guard) == CheckResult(False, "http_500")


def test_a_non_json_body_is_invalid(guard):
    guard.body = b"not json"
    assert check(guard) == CheckResult(False, "invalid")


def test_json_failing_validation_is_invalid(guard):
    guard.body = {"allowed": True}
    assert check(guard) == CheckResult(False, "invalid")


def test_a_server_that_sleeps_past_the_timeout_is_a_timeout(guard):
    guard.delay = CHECK_TIMEOUT_S + 0.5
    started = time.monotonic()
    result = check(guard)
    elapsed = time.monotonic() - started
    assert result == CheckResult(False, "timeout")
    assert elapsed < CHECK_TIMEOUT_S + 0.5


def test_a_dropped_connection_is_a_network_error():
    # A listener that accepts the connection and drops it: a reset, not a timeout.
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]

    def drop_first_connection() -> None:
        peer, _ = listener.accept()
        peer.close()

    threading.Thread(target=drop_first_connection, daemon=True).start()
    try:
        client = RiskGuardClient(f"http://127.0.0.1:{port}", "check-token")
        result = client.check(
            "core-1", {"accountKey": {"venue": "tradovate", "env": "demo", "brokerAccountId": "123"}}
        )
    finally:
        listener.close()
    assert result == CheckResult(False, "network")


def test_exactly_one_request_per_check(guard):
    check(guard)
    assert len(guard.requests) == 1


def test_the_path_account_encoding_and_headers(guard):
    account = "core/a b&c"
    check(guard, core_account=account)
    assert len(guard.requests) == 1
    sent = guard.requests[0]
    assert sent["path"] == f"/v1/check?account={quote(account, safe='')}"
    assert sent["authorization"] == "Bearer check-token"
    assert sent["content_type"] == "application/json"
    assert json.loads(sent["raw"])["instrument"] == "AAPL"

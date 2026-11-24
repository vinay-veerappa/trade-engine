"""Localhost HTTP/SSE wire contract on synthetic books, never live ports.

Every expectation is an explicit literal: a status line, a frame, a JSON body or a
refusal message. The retired frozen handler is no longer compared against; its
recorded bytes live only in the synthetic golden (tests/fixtures/p4c).
"""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
from decimal import Decimal
import gc
import json
from pathlib import Path
import socket
import time
import urllib.parse

import pytest
from test_p4c_embed import package as native_package, run as native_run, PLUGIN as PACKAGING_PLUGIN

from trade_engine.server import http as P
from trade_engine.domain.instruments import Equity, Side
from trade_engine.domain.orders import Order, OrderType
from trade_engine.domain.portfolio import Fill
from trade_engine.ledger.events import CashFlow, Event, EventKind
from trade_engine.ledger.store import Ledger

T = datetime(2026, 9, 23, 16, tzinfo=timezone.utc)

OK = b"HTTP/1.0 200 OK"
NO_CONTENT = b"HTTP/1.0 204 No Content"
NOT_FOUND = b"HTTP/1.0 404 Not Found"
FORBIDDEN_HOST = b"HTTP/1.0 403 Forbidden: Invalid Host header"
HTML_ERROR = b"<!DOCTYPE HTML>"
AFTER_400 = b"HTTP/1.0 400 Bad Request: 'after' parameter must be a non-negative integer"
LAST_EVENT_400 = b"HTTP/1.0 400 Bad Request: 'Last-Event-ID' header must be a non-negative integer"
EMPTY = b""


def seed(ledger, count=3):
    result = [ledger.append(Event("ACC-é", EventKind.CASH_FLOW,
        CashFlow(Decimal("100000.00"), "deposit", T), T, command_id="fund"))]
    for index in range(count):
        order = Order(f"order-{index}", "ACC-é", Equity("AAPL"), OrderType.LIMIT,
            Side.BUY, Decimal("10"), f"c-{index}", T, limit_price=Decimal("150.00"))
        result.append(ledger.append(Event("ACC-é", EventKind.ORDER_SUBMITTED, order, T, command_id=f"c-{index}")))
    return result


@contextmanager
def book(root, count=3, ping=0.02):
    with ExitStack() as stack:
        ledger = stack.enter_context(Ledger(root / "native.db"))
        events = seed(ledger, count)
        server = stack.enter_context(P.EngineHttpServer(ledger, port=0, ping_interval=ping))
        assert server.port not in (3410, 3411, 8097)
        yield ledger, server, events


@pytest.fixture(scope="module")
def native_book(tmp_path_factory):
    with book(tmp_path_factory.mktemp("http-book")) as result:
        yield result


def request(target="/health", method="GET", headers=("Host: localhost",), version="HTTP/1.1"):
    return (f"{method} {target} {version}\r\n" + "\r\n".join(headers) + "\r\n\r\n").encode("latin-1")


def normalize(response):
    return b"\r\n".join(b"Date: <transport-Date>" if line.startswith(b"Date: ") else line
        for line in response.split(b"\r\n"))


def first_line(response):
    return response.splitlines()[0] if response else b""


@contextmanager
def stream(server, raw):
    with socket.create_connection(("127.0.0.1", server.port), timeout=5) as sock:
        sock.sendall(raw)
        with sock.makefile("rb") as reader:
            header = bytearray()
            while line := reader.readline():
                header.extend(line)
                if line == b"\r\n":
                    break
            yield sock, reader, normalize(bytes(header))


def exchange(server, raw):
    with socket.create_connection(("127.0.0.1", server.port), timeout=5) as sock:
        sock.sendall(raw)
        sock.shutdown(socket.SHUT_WR)
        result = bytearray()
        while chunk := sock.recv(65536):
            result.extend(chunk)
        return normalize(bytes(result))


def next_frame(reader):
    result = bytearray()
    while line := reader.readline():
        result.extend(line)
        if line == b"\n":
            break
    return bytes(result)


def next_event(reader):
    """The next frame that is not an idle keep-alive. A busy machine can take longer than the
    fixture's 20 ms ping interval between two reads of a burst, and a ping then lands between events
    (seen on a GitHub-hosted runner); the tests that count events are not about the ping."""
    while (frame := next_frame(reader)) == b": ping\n\n":
        pass
    return frame


def wait(predicate):
    deadline = time.monotonic() + 5
    while not predicate():
        assert time.monotonic() < deadline, "native lifecycle condition not reached"
        time.sleep(0.001)


STATIC = [
    request(), request("/snapshot"), request("/missing"), request("/health;ignored"),
    request("//health"), request("http://localhost/health"),
    request("/health", headers=()), request("/health", headers=("Host: attacker.test",)),
    request("/health", headers=("Host: LOCALHOST :ignored",)),
    request("/health", headers=("Host: localhost", "Host: attacker.test")),
    request("/health", headers=("Host: attacker.test", "Host: localhost")),
    *[request("/snapshot", headers=("Host: localhost", f"Origin: {origin}"))
        for origin in ("http://localhost:3000", "HTTPS://LOCALHOST", "https://127.0.0.1:123",
                       "http://localhost.evil", "http://localhost/", "null", "http://localhost:")],
    request("/snapshot", headers=("Host: localhost", "Origin: http://localhost",
        "Origin: https://attacker.test")),
    request("/snapshot", headers=("Host: localhost", "Origin: http://localhost", "\t:3000")),
    *[request("/snapshot", method=method, headers=("Host: localhost", "Origin: http://localhost:3000"))
        for method in ("OPTIONS", "HEAD", "POST", "DELETE", "PATCH", "get", "<tag>")],
    *[request("/health", version=version) for version in
        ("HTTP/1.0", "HTTP/1.2", "HTTP/01.01", "HTTP/0.9", "HTTP/2.0",
         "HTTP/9.9", "HTTP/1.x", "HTTP/1.2.3", "HTTP/12345678901.1", "HTP/1.1")],
    request("/health", headers=("Host: localhost", "Bad Header")),
    request("/health", headers=("Bad Header", "Host: localhost")),
    b"GET /health\r\n", b"POST /health\r\n", b"\r\n",
    b"garbage\r\n", b"GET /health extra HTTP/1.1\r\n\r\n",
    b"GET /health HTTP/1.1\nHost: localhost\n\n",
    request("/health", headers=tuple(["Host: localhost"] + ["X-Test: x"] * 99)),
    request("/health", headers=("Host: localhost", "X-Test: " + "a" * 65536)),
    request("/health", headers=("Host: localhost", "X-Test: " + "a" * 65536), version="HTTP/0.9"),
    request("/health", headers=tuple(["Host: localhost"] + ["X-Test: x"] * 99), version="HTTP/0.9"),
    request("/health", headers=("Host: localhost", "X-Test: " + "a" * 65536), method="HEAD"),
    request("http://[", headers=("Host: attacker.test",)),
    request("http://[", method="POST"),
    *[request("/health", headers=("Host: localhost", header))
        for header in ("X-Test: \t  value\t ", "X-Test:", "x-test: one:two",
            "X-Test: a\x00b", "X-Test: caf\xe9", "X Test: value", "X-Test : value",
            "X-Test: first", "X-Test: first\r\n\tsecond", ": bad", "!#$%&'*+-.^_`|~: value")],
    b"GET /" + b"a" * 65536 + b" HTTP/1.1\r\n\r\n",
]

# The status line (first line) each STATIC request must receive, in the same order.
STATIC_LINES = [
    OK, OK, NOT_FOUND, OK, OK, OK,                     # targets: only the Host header decides the gate
    FORBIDDEN_HOST, FORBIDDEN_HOST, OK, OK, FORBIDDEN_HOST,  # no Host / attacker Host / LOCALHOST :port / duplicates
    *([OK] * 7), OK, OK,                               # Origin variants on /snapshot never change the status
    NO_CONTENT,                                        # OPTIONS preflight
    *[b"HTTP/1.0 501 Unsupported method ('%s')" % method for method in
        (b"HEAD", b"POST", b"DELETE", b"PATCH", b"get", b"<tag>")],
    OK, OK, OK,                                        # HTTP/1.0, HTTP/1.2, HTTP/01.01 are served
    b'{"count":4,"ok":true,"seq":4}',                  # HTTP/0.9 simple request: body only, no status line
    b"HTTP/1.0 505 Invalid HTTP version (2.0)",
    b"HTTP/1.0 505 Invalid HTTP version (9.9)",
    b"HTTP/1.0 400 Bad request version ('HTTP/1.x')",
    b"HTTP/1.0 400 Bad request version ('HTTP/1.2.3')",
    b"HTTP/1.0 400 Bad request version ('HTTP/12345678901.1')",
    b"HTTP/1.0 400 Bad request version ('HTP/1.1')",
    OK, FORBIDDEN_HOST,
    HTML_ERROR,                                        # HTTP/0.9 GET without Host: HTML error page
    b"HTTP/1.0 400 Bad HTTP/0.9 request type ('POST')",
    EMPTY,                                             # a bare CRLF is no request
    b"HTTP/1.0 400 Bad request syntax ('garbage')",
    b"HTTP/1.0 400 Bad request syntax ('GET /health extra HTTP/1.1')",
    OK,                                                # bare LF line endings are accepted
    b"HTTP/1.0 431 Too many headers",
    b"HTTP/1.0 431 Line too long",
    HTML_ERROR,
    HTML_ERROR,
    b"HTTP/1.0 431 Line too long",
    FORBIDDEN_HOST,
    b"HTTP/1.0 501 Unsupported method ('POST')",
    *([OK] * 11),                                      # header-name and value variants are accepted
    b"HTTP/1.0 414 URI Too Long",
]
assert len(STATIC_LINES) == len(STATIC)


@pytest.mark.parametrize("raw,line", list(zip(STATIC, STATIC_LINES)),
    ids=[f"wire-{i}" for i in range(len(STATIC))])
def test_static_wire_contract(native_book, raw, line):
    _, server, _ = native_book
    assert first_line(exchange(server, raw)) == line


CURSORS = [
    ("", (), 0), ("?after=0", (), 0), ("?after=1", (), 1),
    ("?after=", (), 0), ("?after=&after=2", (), 2),
    ("?after=1&after=3", (), 1), ("?%61fter=2", (), 2),
    ("?after=" + urllib.parse.quote("٢"), (), 2),
    ("?after=" + urllib.parse.quote("２"), (), 2),
    ("?after=0", ("Last-Event-ID: 2",), 2),
    ("?after=3", ("Last-Event-ID: 1", "Last-Event-ID: 2"), 1),
    ("?after=0002", (), 2),
]


@pytest.mark.parametrize("query,headers,after", CURSORS)
def test_cursor_frames_stream_after_the_cursor(native_book, query, headers, after):
    _, server, _ = native_book
    with stream(server, request("/events" + query, headers=("Host: localhost", "Origin: http://localhost", *headers))) as (_, reader, header):
        assert header.startswith(OK + b"\r\n")
        assert next_frame(reader) == b"retry: 1000\n\n"
        frames = [next_frame(reader) for _ in range(4 - after)]
        assert next_frame(reader) == b": ping\n\n"
    assert [int(frame.split(b"\n", 1)[0][4:]) for frame in frames] == list(range(after + 1, 5))


BAD_CURSORS = [
    ("?after=-1", (), AFTER_400), ("?after=+1", (), AFTER_400), ("?after=%2B1", (), AFTER_400),
    ("?after=1.0", (), AFTER_400),
    ("?after=bad", ("Last-Event-ID: 1",), AFTER_400),
    ("?after=0", ("Last-Event-ID: -1",), LAST_EVENT_400),
    ("", ("Last-Event-ID:",), LAST_EVENT_400), ("", ("Last-Event-ID: bad",), LAST_EVENT_400),
    ("?after=bad", ("Last-Event-ID: bad",), AFTER_400),
    # Non-ASCII digits and an over-long digit string are not answered: the connection closes.
    ("?after=" + urllib.parse.quote("²"), (), EMPTY),
    ("?after=" + "9" * 4301, (), EMPTY),
]


@pytest.mark.parametrize("query,headers,line", BAD_CURSORS)
def test_cursor_refusal_wire_contract(native_book, query, headers, line):
    _, server, _ = native_book
    assert first_line(exchange(server, request("/events" + query, headers=("Host: localhost", *headers)))) == line


def test_cursor_overflow_is_refused_before_stream(native_book):
    _, server, _ = native_book
    # The native server answers 400 with a JSON body before any stream is reserved.
    response = exchange(server, request("/events?after=9223372036854775808"))
    head, _, payload = response.partition(b"\r\n\r\n")
    assert head.split(b"\r\n")[0].split(b" ")[1] == b"400"
    assert b"Content-Type: application/json" in head
    assert b"text/event-stream" not in head
    assert json.loads(payload) == {"error": {"type": "RuntimeRequestError", "message": "event cursor out of range"}}
    assert payload == b'{"error":{"message":"event cursor out of range","type":"RuntimeRequestError"}}'
    assert not any("OverflowError" in error for error in server._server.errors)


def test_lifecycle_handover_dedup_shutdown(tmp_path):
    with book(tmp_path) as (ledger, native, events):
        view = native._server
        view.pause_backlog(True)
        with stream(native, request("/events")) as (_, reader, header):
            assert next_frame(reader) == b"retry: 1000\n\n"
            wait(lambda: view.subscriber_count == 1)
            order = Order("race", "ACC-é", Equity("MSFT"), OrderType.STOP,
                Side.BUY, Decimal("1"), "race", T, stop_price=Decimal("200.0"))
            event = ledger.append(Event("ACC-é", EventKind.ORDER_SUBMITTED, order, T, command_id="race"))
            native.broadcast(events[0])
            view.pause_backlog(False)
            frames = [next_event(reader) for _ in range(5)]
            assert [int(frame.split(b"\n", 1)[0][4:]) for frame in frames] == [1, 2, 3, 4, 5]
            native.broadcast(event)
            assert next_frame(reader) == b": ping\n\n"
            port = native.port
            assert native.start() is native and native.port == port
            native.stop()
            assert reader.read() == b""
        assert view.subscriber_count == 0
        with pytest.raises(OSError):
            socket.create_connection(("127.0.0.1", port), timeout=1)
        native.stop()
        native.start()
        assert exchange(native, request()).endswith(b'{"count":5,"ok":true,"seq":5}')


def test_slow_subscriber_and_reconnect(tmp_path):
    with book(tmp_path, count=60) as (ledger, native, _):
        with stream(native, request("/events?after=0")) as (_, reader, header):
            assert next_frame(reader) == b"retry: 1000\n\n"
            wait(lambda: native._server.subscriber_count == 1)
            for index in range(61, 121):
                order = Order(f"live-{index}", "ACC-é", Equity("AAPL"), OrderType.MARKET,
                    Side.BUY, Decimal("1"), f"live-{index}", T)
                ledger.append(Event("ACC-é", EventKind.ORDER_SUBMITTED, order, T, command_id=f"live-{index}"))
            frames = [next_event(reader) for _ in range(121)]
            assert [int(frame.split(b"\n", 1)[0][4:]) for frame in frames] == list(range(1, 122))
        wait(lambda: native._server.subscriber_count == 0)
        with stream(native, request("/events?after=119")) as (_, reader, header):
            assert next_frame(reader) == b"retry: 1000\n\n"
            assert next_frame(reader).startswith(b"id: 120\n")
            assert next_frame(reader).startswith(b"id: 121\n")
            assert next_frame(reader) == b": ping\n\n"


def test_committed_snapshot_ignores_uncommitted_rows(tmp_path):
    with book(tmp_path, count=0) as (ledger, server, _):
        ledger.conn.execute("BEGIN IMMEDIATE")
        ledger.conn.execute("UPDATE events SET account='DIRTY'")
        try:
            response = exchange(server, request("/snapshot"))
        finally:
            ledger.conn.execute("ROLLBACK")
        body = json.loads(response.split(b"\r\n\r\n")[1])
        assert body == {"seq": 1, "accounts": {"ACC-é": {
            "account_id": "ACC-é", "cash": "100000", "realized_pnl": "0",
            "last_seq": 1, "positions": {}, "orders": {},
        }}}


def test_readonly_missing_database(tmp_path):
    path = tmp_path / "missing.db"
    ledger = Ledger(path)
    with P.EngineHttpServer(ledger, port=0) as server:
        assert exchange(server, request()) == b""
        assert not path.exists()
        assert not Path(str(path) + ".lock").exists()
        assert server._server.errors


def test_constructor_refuses_non_localhost_binds_and_cursor_facade(tmp_path):
    with Ledger(tmp_path / "synthetic.db") as ledger:
        for host in ("0.0.0.0", "127.0.0.2", "LOCALHOST", "::1"):
            with pytest.raises(ValueError) as refused:
                P.EngineHttpServer(ledger, host=host, port=0)
            assert str(refused.value) == (
                f"Engine server must bind to localhost only (127.0.0.1), got {host}")
    handler = P._EngineHandler()
    assert handler._handle_events({"after": ["1", "2"]}, "3") == 3
    assert handler._handle_events({}) == 0


def test_native_drop_releases_listener(tmp_path):
    import trade_engine_rs
    with Ledger(tmp_path / "synthetic.db") as ledger:
        native = trade_engine_rs.HttpServer(str(ledger.path), "127.0.0.1", 0, 0.02)
        port = native.server_port
        class View:
            pass
        view = View()
        view.port = port
        assert exchange(view, request()).endswith(b'{"count":0,"ok":true,"seq":0}')
        del native
        gc.collect()
        with pytest.raises(OSError):
            socket.create_connection(("127.0.0.1", port), timeout=1)


def test_native_builtin_http_without_dynamic_extension(native_package, tmp_path):
    config, _, _, plugins = native_package
    config["plugin_config"]["ledger"] = str(tmp_path / "builtin-http.db")
    (plugins / "fake_plugin.py").write_text(PACKAGING_PLUGIN + """
base_probe = probe
def probe(config):
    import json, sqlite3, urllib.request
    from trade_engine.ledger import Ledger
    from trade_engine.server.http import EngineHttpServer
    def forbidden(*args, **kwargs):
        raise AssertionError("Python SQLite is forbidden in the native HTTP host")
    sqlite3.connect = forbidden
    with Ledger(config["ledger"]) as ledger:
        with EngineHttpServer(ledger, port=0) as server:
            assert server.port not in (3410, 3411, 8097)
            with urllib.request.urlopen(f"http://127.0.0.1:{server.port}/health", timeout=5) as response:
                health = json.load(response)
            with urllib.request.urlopen(f"http://127.0.0.1:{server.port}/snapshot", timeout=5) as response:
                snapshot = json.load(response)
            assert server._server.daemon_threads
    report = base_probe(config)
    report["http"] = [health, snapshot]
    return report
""", encoding="utf-8")
    code, report = native_run(native_package)
    assert code == 0, report
    assert report["builtin_module_count"] == 1
    assert report["result"]["modules"] == ["trade_engine_rs"]
    assert report["result"]["http"] == [{"count": 0, "ok": True, "seq": 0}, {"accounts": {}, "seq": 0}]
    assert not any("trade_engine_rs" in path and path.endswith(".pyd") for path in report["result"]["loaded"])


def test_positions_and_orders_decimal_snapshot(tmp_path):
    with book(tmp_path, count=1) as (ledger, server, _):
        fill = Fill("fill", "order-0", "ACC-é", Equity("AAPL"),
            Decimal("3"), Decimal("140.0100"), "sim", T, Side.BUY, fee=Decimal("0.10"))
        ledger.append(Event("ACC-é", EventKind.FILL, fill, T, command_id="fill"))
        market = Order("market", "ACC-é", Equity("MSFT"), OrderType.MARKET,
            Side.SELL, Decimal("2"), "market", T)
        ledger.append(Event("ACC-é", EventKind.ORDER_SUBMITTED, market, T, command_id="market"))
        account = json.loads(exchange(server, request("/snapshot")).split(b"\r\n\r\n")[1])["accounts"]["ACC-é"]
        assert account["positions"]["AAPL"]["quantity"] == "3"
        assert account["orders"]["market"]["limit_price"] is None
        assert account["orders"]["market"]["stop_price"] is None


def test_immediate_stop_before_owner_thread_schedules(tmp_path):
    with Ledger(tmp_path / "synthetic.db") as ledger:
        for _ in range(30):
            server = P.EngineHttpServer(ledger, port=0).start()
            server.stop()
            assert server._server is None

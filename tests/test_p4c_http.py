"""Frozen/native transport bytes on independent synthetic books, never live ports."""
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

from tests.frozen_p4c import http as O
from trade_engine.server import http as P
from trade_engine.domain.instruments import Equity, Side
from trade_engine.domain.orders import Order, OrderType
from trade_engine.domain.portfolio import Fill
from trade_engine.ledger.events import CashFlow, Event, EventKind
from trade_engine.ledger.store import Ledger

T = datetime(2026, 9, 23, 16, tzinfo=timezone.utc)


def seed(ledger, count=3):
    result = [ledger.append(Event("ACC-\u00e9", EventKind.CASH_FLOW,
        CashFlow(Decimal("100000.00"), "deposit", T), T, command_id="fund"))]
    for index in range(count):
        order = Order(f"order-{index}", "ACC-\u00e9", Equity("AAPL"), OrderType.LIMIT,
            Side.BUY, Decimal("10"), f"c-{index}", T, limit_price=Decimal("150.00"))
        result.append(ledger.append(Event("ACC-\u00e9", EventKind.ORDER_SUBMITTED, order, T, command_id=f"c-{index}")))
    return result


@contextmanager
def books(root, count=3, ping=0.02):
    with ExitStack() as stack:
        ledgers = [stack.enter_context(Ledger(root / f"{name}.db")) for name in ("oracle", "native")]
        events = [seed(ledger, count) for ledger in ledgers]
        servers = [stack.enter_context(module.EngineHttpServer(ledger, port=0, ping_interval=ping))
            for module, ledger in zip((O, P), ledgers)]
        assert all(server.port not in (3410, 3411, 8097) for server in servers)
        yield ledgers, servers, events


@pytest.fixture(scope="module")
def paired(tmp_path_factory):
    with books(tmp_path_factory.mktemp("http-pairs")) as result:
        yield result


def request(target="/health", method="GET", headers=("Host: localhost",), version="HTTP/1.1"):
    return (f"{method} {target} {version}\r\n" + "\r\n".join(headers) + "\r\n\r\n").encode("latin-1")


def normalize(response):
    return b"\r\n".join(b"Date: <transport-Date>" if line.startswith(b"Date: ") else line
        for line in response.split(b"\r\n"))


@contextmanager
def stream(server, raw, *, connect=None):
    connector = socket.create_connection if connect is None else connect
    with connector(("127.0.0.1", server.port), timeout=5) as sock:
        sock.sendall(raw)
        with sock.makefile("rb") as reader:
            header = bytearray()
            while line := reader.readline():
                header.extend(line)
                if line == b"\r\n":
                    break
            yield sock, reader, normalize(bytes(header))


def exchange(server, raw, *, connect=None):
    connector = socket.create_connection if connect is None else connect
    with connector(("127.0.0.1", server.port), timeout=5) as sock:
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


@pytest.mark.parametrize("raw", STATIC, ids=[f"wire-{i}" for i in range(len(STATIC))])
def test_static_wire_parity(paired, raw):
    _, servers, _ = paired
    expected, actual = [exchange(server, raw) for server in servers]
    assert actual == expected


CURSORS = [
    ("", (), 0), ("?after=0", (), 0), ("?after=1", (), 1),
    ("?after=", (), 0), ("?after=&after=2", (), 2),
    ("?after=1&after=3", (), 1), ("?%61fter=2", (), 2),
    ("?after=" + urllib.parse.quote("\u0662"), (), 2),
    ("?after=" + urllib.parse.quote("\uff12"), (), 2),
    ("?after=0", ("Last-Event-ID: 2",), 2),
    ("?after=3", ("Last-Event-ID: 1", "Last-Event-ID: 2"), 1),
    ("?after=0002", (), 2),
]


@pytest.mark.parametrize("query,headers,after", CURSORS)
def test_cursor_frames_parity(paired, query, headers, after):
    ledgers, servers, _ = paired
    outputs = []
    for server, ledger in zip(servers, ledgers):
        with stream(server, request("/events" + query, headers=("Host: localhost", "Origin: http://localhost", *headers))) as (_, reader, header):
            retry = next_frame(reader)
            frames = [next_frame(reader) for _ in range(4 - after)]
            ping = next_frame(reader)
            outputs.append((header, retry, frames, ping))
    assert outputs[0] == outputs[1]
    assert outputs[1][1] == b"retry: 1000\n\n"
    assert outputs[1][-1] == b": ping\n\n"
    assert [int(frame.split(b"\n", 1)[0][4:]) for frame in outputs[1][2]] == list(range(after + 1, 5))


BAD_CURSORS = [
    ("?after=-1", ()), ("?after=+1", ()), ("?after=%2B1", ()), ("?after=1.0", ()),
    ("?after=bad", ("Last-Event-ID: 1",)), ("?after=0", ("Last-Event-ID: -1",)),
    ("", ("Last-Event-ID:",)), ("", ("Last-Event-ID: bad",)),
    ("?after=bad", ("Last-Event-ID: bad",)),
    ("?after=" + urllib.parse.quote("\u00b2"), ()),
    ("?after=" + "9" * 4301, ()),
]


@pytest.mark.parametrize("query,headers", BAD_CURSORS)
def test_cursor_refusal_wire_parity(paired, query, headers):
    _, servers, _ = paired
    assert exchange(servers[1], request("/events" + query, headers=("Host: localhost", *headers))) == exchange(
        servers[0], request("/events" + query, headers=("Host: localhost", *headers)))


def test_sqlite_cursor_overflow_after_retry(paired):
    _, servers, _ = paired
    for server in servers:
        with stream(server, request("/events?after=9223372036854775808")) as (_, reader, header):
            assert header.startswith(b"HTTP/1.0 200 OK")
            assert next_frame(reader) == b"retry: 1000\n\n"
            assert reader.read() == b""
    assert any(error == "OverflowError: Python int too large to convert to SQLite INTEGER" for error in servers[1]._server.errors)


def test_lifecycle_handover_dedup_shutdown(tmp_path):
    with books(tmp_path) as (ledgers, servers, events):
        native = servers[1]
        view = native._server
        view.pause_backlog(True)
        with stream(native, request("/events")) as (_, reader, header):
            assert next_frame(reader) == b"retry: 1000\n\n"
            wait(lambda: view.subscriber_count == 1)
            order = Order("race", "ACC-\u00e9", Equity("MSFT"), OrderType.STOP,
                Side.BUY, Decimal("1"), "race", T, stop_price=Decimal("200.0"))
            event = ledgers[1].append(Event("ACC-\u00e9", EventKind.ORDER_SUBMITTED, order, T, command_id="race"))
            native.broadcast(events[1][0])
            view.pause_backlog(False)
            frames = [next_frame(reader) for _ in range(5)]
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
    with books(tmp_path, count=60) as (ledgers, servers, _):
        native = servers[1]
        with stream(native, request("/events?after=0")) as (_, reader, header):
            assert next_frame(reader) == b"retry: 1000\n\n"
            wait(lambda: native._server.subscriber_count == 1)
            for index in range(61, 121):
                order = Order(f"live-{index}", "ACC-\u00e9", Equity("AAPL"), OrderType.MARKET,
                    Side.BUY, Decimal("1"), f"live-{index}", T)
                ledgers[1].append(Event("ACC-\u00e9", EventKind.ORDER_SUBMITTED, order, T, command_id=f"live-{index}"))
            frames = [next_frame(reader) for _ in range(121)]
            assert [int(frame.split(b"\n", 1)[0][4:]) for frame in frames] == list(range(1, 122))
        wait(lambda: native._server.subscriber_count == 0)
        with stream(native, request("/events?after=119")) as (_, reader, header):
            assert next_frame(reader) == b"retry: 1000\n\n"
            assert next_frame(reader).startswith(b"id: 120\n")
            assert next_frame(reader).startswith(b"id: 121\n")
            assert next_frame(reader) == b": ping\n\n"


def test_committed_snapshot_and_projection(tmp_path):
    with books(tmp_path, count=0) as (ledgers, servers, _):
        outputs = []
        for ledger, server in zip(ledgers, servers):
            ledger.conn.execute("BEGIN IMMEDIATE")
            ledger.conn.execute("UPDATE events SET account='DIRTY'")
            try:
                outputs.append(exchange(server, request("/snapshot")))
            finally:
                ledger.conn.execute("ROLLBACK")
        assert outputs[0] == outputs[1]
        body = json.loads(outputs[1].split(b"\r\n\r\n")[1])
        assert body == {"seq": 1, "accounts": {"ACC-\u00e9": {
            "account_id": "ACC-\u00e9", "cash": "100000", "realized_pnl": "0",
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


def test_constructor_and_nonowning_cursor_facade(tmp_path):
    with Ledger(tmp_path / "synthetic.db") as ledger:
        for host in ("0.0.0.0", "127.0.0.2", "LOCALHOST", "::1"):
            with pytest.raises(ValueError) as frozen:
                O.EngineHttpServer(ledger, host=host, port=0)
            # Compare independently so each constructor really executes.
            try:
                P.EngineHttpServer(ledger, host=host, port=0)
            except ValueError as actual:
                assert str(actual) == str(frozen.value)
            else:
                assert False, "native accepted an invalid bind"
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


def test_positions_orders_decimal_snapshot_parity(tmp_path):
    with books(tmp_path, count=1) as (ledgers, servers, _):
        for ledger in ledgers:
            fill = Fill("fill", "order-0", "ACC-\u00e9", Equity("AAPL"),
                Decimal("3"), Decimal("140.0100"), "sim", T, Side.BUY, fee=Decimal("0.10"))
            ledger.append(Event("ACC-\u00e9", EventKind.FILL, fill, T, command_id="fill"))
            market = Order("market", "ACC-\u00e9", Equity("MSFT"), OrderType.MARKET,
                Side.SELL, Decimal("2"), "market", T)
            ledger.append(Event("ACC-\u00e9", EventKind.ORDER_SUBMITTED, market, T, command_id="market"))
        replies = [exchange(server, request("/snapshot")) for server in servers]
        assert replies[0] == replies[1]
        account = json.loads(replies[1].split(b"\r\n\r\n")[1])["accounts"]["ACC-\u00e9"]
        assert account["positions"]["AAPL"]["quantity"] == "3"
        assert account["orders"]["market"]["limit_price"] is None
        assert account["orders"]["market"]["stop_price"] is None


def test_immediate_stop_before_owner_thread_schedules(tmp_path):
    with Ledger(tmp_path / "synthetic.db") as ledger:
        for _ in range(30):
            server = P.EngineHttpServer(ledger, port=0).start()
            server.stop()
            assert server._server is None

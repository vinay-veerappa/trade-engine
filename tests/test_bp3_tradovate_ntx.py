"""BP-3: the Tradovate adapter over ntx, read-only (BROKER_PLATFORM phase 3).

Fixtures and a local fake ntx daemon only: nothing here reads the live ntx install, the
live riskguard, NT8 or a broker. Account ids are fakes (`ACCT-TEST-194`, numeric `900194`).

Enforcing tests, by rule:
- G1/G2 against the fake HTTP+SSE daemon: snapshot -> added; SSE account event -> early
  re-read -> added/removed; daemon down -> stale after 120 s, known accounts unchanged;
  reconnect -> one fresh snapshot -> diff; one identity 401 -> no `removed`, its accounts stale.
- read-only: `test_order_verb_gate_*` (scans the adapter and the client for the verbs).
- the token never leaks: `test_token_never_appears_*`.
- mapping: `test_route_*`, `test_env_*`, `test_unknown_instrument_*`, `test_nan_*`, money.
- budgets: `test_budget_oversize_frame_*`, `test_budget_reconnect_storm_*`,
  `test_budget_early_reread_limiter_*`, `test_budget_account_cap_*`, `test_budget_one_raw_snapshot_*`,
  `test_budget_per_event_parse_cost`.
"""

from __future__ import annotations

import ast
import gc
import io
import json
import logging
import pickle
import re
import threading
import time
import tracemalloc
from collections import deque
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from trade_engine.accounts import _rs
from trade_engine.accounts.errors import AccountRuleError
from trade_engine.domain.instruments import Side
from trade_engine.interfaces.broker import BrokerAdapter, UnsupportedCapability
from trade_engine.tradovate import adapter as adapter_mod
from trade_engine.tradovate import ntx_client as client_mod
from trade_engine.tradovate.adapter import ACCOUNT_CAP, AccountWatcher, TradovateAdapter
from trade_engine.tradovate.ntx_client import (
    MAX_LINE_BYTES,
    NtxClient,
    NtxError,
    NtxToken,
    ReconnectBackoff,
    RefreshLimiter,
    SseReader,
    SseStats,
    default_token_path,
    read_token,
)

NOW = datetime(2026, 11, 11, 18, 0, tzinfo=timezone.utc)
FAKE_TOKEN = "FAKE-NTX-TOKEN-0123456789abcdef"
IDENT_A = "ident-fake-A"
IDENT_B = "ident-fake-B"
FAKE_NAME = "ACCT-TEST-194"
FAKE_NUM = "900194"


class FakeClock:
    def __init__(self, now: datetime = NOW) -> None:
        self.now = now

    def now_utc(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


# --- fixtures: what the ntx /snapshot carries ------------------------------------------


def acct(
    name: str = FAKE_NAME,
    num: str = FAKE_NUM,
    *,
    identity: str = IDENT_A,
    env: str = "paper",
    positions: list | None = None,
    trades: list | None = None,
    realized: object = 125.5,
    as_of: str = "2026-11-11T17:59:30Z",
) -> dict:
    return {
        "id": num,
        "native_id": int(num) if num.isdigit() else None,
        "name": name,
        "currency": "USD",
        "equity": 50000.0,
        "cash": 49000.0,
        "realized_pnl": realized,
        "environment": env,
        "ws_connected": True,
        "as_of_utc": as_of,
        "session": {"identity": identity, "expires": "2026-11-12T00:00:00Z"},
        "positions": positions or [],
        "orders": [],
        "trades": trades or [],
    }


def snap(accounts: list[dict], errors: list[dict] | None = None, fetched: str = "2026-11-11T18:00:00Z") -> dict:
    return {"broker": "ntx", "fetchedAt": fetched, "accounts": accounts, "identityErrors": errors or []}


def two_accounts() -> list[dict]:
    return [
        acct("ACCT-TEST-194", "900194", identity=IDENT_A),
        acct("ACCT-TEST-277", "900277", identity=IDENT_B),
    ]


# --- the fake ntx daemon (loopback HTTP + SSE) -------------------------------------------


class FakeNtx:
    """A loopback fake of the ntx daemon. Scripts for `/events` are lists of items:
    `bytes` (written as is), `("wait_snapshots", n)` (wait until n snapshot reads were
    served) or a callable (run on the server thread)."""

    def __init__(self, token: str = FAKE_TOKEN) -> None:
        self.token = token
        self.snapshot_body: dict = snap(two_accounts())
        self.snapshot_status = 200
        self.health_status = 200
        self.health_body: dict = {"status": "ok", "identities": []}
        self.snapshot_calls = 0
        self.events_opened = 0
        self.live_streams = 0
        self.max_live_streams = 0
        self.event_scripts: deque[list] = deque()
        self._lock = threading.Lock()
        daemon = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *args):  # noqa: D401 - silence
                pass

            def _json(self, status: int, body: dict) -> None:
                raw = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):  # noqa: N802
                if self.headers.get("Authorization") != "Bearer " + daemon.token:
                    self._json(401, {"error": "unauthorized"})
                    return
                if self.path == "/health":
                    self._json(daemon.health_status, daemon.health_body)
                elif self.path == "/snapshot":
                    with daemon._lock:
                        daemon.snapshot_calls += 1
                    self._json(daemon.snapshot_status, daemon.snapshot_body)
                elif self.path == "/events":
                    self._events()
                else:
                    self._json(404, {})

            def _events(self):
                with daemon._lock:
                    daemon.events_opened += 1
                    daemon.live_streams += 1
                    daemon.max_live_streams = max(daemon.max_live_streams, daemon.live_streams)
                    script = daemon.event_scripts.popleft() if daemon.event_scripts else []
                try:
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    self.wfile.flush()
                    for item in script:
                        if isinstance(item, bytes):
                            self.wfile.write(item)
                            self.wfile.flush()
                        elif isinstance(item, tuple):
                            deadline = time.monotonic() + 10
                            while daemon.snapshot_calls < item[1] and time.monotonic() < deadline:
                                time.sleep(0.005)
                        else:
                            item()
                except (BrokenPipeError, ConnectionError, OSError):
                    pass
                finally:
                    with daemon._lock:
                        daemon.live_streams -= 1

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def frame(payload: dict) -> bytes:
    return b"event: state_update\ndata: " + json.dumps(payload).encode() + b"\n\n"


KEEPALIVE = b": keepalive\n\n"
HINT = frame({"type": "props", "identity": IDENT_A, "seq": 1, "event_type": "Created", "entity_type": "accounts"})


@pytest.fixture
def daemon():
    d = FakeNtx()
    yield d
    try:
        d.stop()
    except Exception:  # noqa: BLE001 - already stopped by the test
        pass
    gc.collect()  # the Rust ring is thread-bound: free it here, not on a server thread


@pytest.fixture
def clock():
    return FakeClock()


def make_client(d: FakeNtx, token: str = FAKE_TOKEN) -> NtxClient:
    return NtxClient(NtxToken(token), port=d.port, timeout=3.0)


def make_adapter(d: FakeNtx, clock, **kw) -> TradovateAdapter:
    return TradovateAdapter(make_client(d), clock=clock, **kw)


def make_watcher(adapter: TradovateAdapter, clock, sleeps: list | None = None, **kw) -> AccountWatcher:
    def sleep(seconds: float) -> None:
        if sleeps is not None:
            sleeps.append(seconds)
        clock.advance(seconds)

    return AccountWatcher(adapter, adapter._client, clock=clock, sleep=sleep, rand=lambda: 0.0, **kw)


def kinds(events) -> list[tuple[str, str]]:
    return sorted((e.kind, e.key) for e in events)


# =====================================================================================
# G1 / G2: the account port against the fake daemon
# =====================================================================================


def test_protocol_shape(daemon, clock):
    adapter = make_adapter(daemon, clock)
    assert isinstance(adapter, BrokerAdapter)
    assert adapter.name == "tradovate"


def test_g1_snapshot_yields_added(daemon, clock):
    adapter = make_adapter(daemon, clock)
    events = adapter.account_events(NOW - timedelta(hours=1))
    assert kinds(events) == [("added", "tradovate:ACCT-TEST-194"), ("added", "tradovate:ACCT-TEST-277")]
    assert all(e.display.startswith("*") for e in events)
    assert daemon.snapshot_calls == 1
    # a second poll with nothing changed yields no event and does not re-add
    assert adapter.account_events(NOW + timedelta(seconds=1)) == []


def test_g1_record_shape(daemon, clock):
    adapter = make_adapter(daemon, clock)
    snapshot = adapter.accounts()
    by_key = {r.key: r for r in snapshot.records}
    record = by_key["tradovate:ACCT-TEST-194"]
    assert record.source == "ntx"
    assert record.api_ref == FAKE_NUM  # engine-only (D-13)
    assert "api_ref" not in repr(record)  # D-13: the numeric id is not a repr field
    assert record.status == "active"
    assert record.class_ is None and record.firm is None
    assert record.display == _rs.mask(FAKE_NAME)


def test_g1_sse_account_event_triggers_early_reread_added_and_removed(daemon, clock):
    adapter = make_adapter(daemon, clock)
    seen: list = []
    poller = adapter.poller
    orig = poller.poll
    poller.poll = lambda: (lambda ev: (seen.extend(ev), ev)[1])(orig())

    watcher = make_watcher(adapter, clock)

    def change():
        # the connect read must have restarted the window before the clock moves
        deadline = time.monotonic() + 10
        while watcher.limiter._last is None and time.monotonic() < deadline:
            time.sleep(0.005)
        daemon.snapshot_body = snap(
            [acct("ACCT-TEST-194", "900194", identity=IDENT_A), acct("ACCT-TEST-301", "900301", identity=IDENT_B)]
        )
        clock.advance(6)

    daemon.event_scripts.append([("wait_snapshots", 1), change, HINT])
    assert watcher.run_once() == "stream_ended"
    assert daemon.snapshot_calls == 2  # the connect read, plus exactly one early re-read
    assert kinds([e for e in seen if e.kind != "added" or e.key.endswith("301")]) == [
        ("added", "tradovate:ACCT-TEST-301"),
        ("removed", "tradovate:ACCT-TEST-277"),
    ]
    assert watcher.limiter.allowed == 1


def test_g1_removed_is_reported_never_closed(daemon, clock):
    adapter = make_adapter(daemon, clock)
    adapter.account_events(NOW - timedelta(hours=1))
    daemon.snapshot_body = snap([acct("ACCT-TEST-194", "900194", identity=IDENT_A)])
    events = adapter.account_events(NOW - timedelta(hours=1))
    removed = [e for e in events if e.kind == "removed"]
    assert [e.key for e in removed] == ["tradovate:ACCT-TEST-277"]
    assert all(e.changes == () or e.kind != "removed" for e in removed)
    assert not [e for e in events if e.kind == "changed"]  # no status change to "closed"


def test_g2_daemon_down_keeps_last_list_and_goes_stale_after_120s(daemon, clock):
    adapter = make_adapter(daemon, clock)
    first = adapter.account_events(NOW - timedelta(hours=1))
    assert len(first) == 2
    daemon.stop()

    clock.advance(60)
    assert adapter.poller.poll() == []  # B3: a failed read emits nothing
    assert [r.key for r in adapter.accounts().records] == ["tradovate:ACCT-TEST-194", "tradovate:ACCT-TEST-277"]
    assert not adapter.is_stale(clock.now_utc())
    assert adapter.stale_keys(clock.now_utc()) == []

    clock.advance(59)  # 119 s: still fresh
    assert not adapter.is_stale(clock.now_utc())
    assert adapter.stale_keys(clock.now_utc()) == []

    clock.advance(1.5)  # 120.5 s after the last good read: past the 120 s line (not 121)
    assert adapter.is_stale(clock.now_utc())  # B4
    assert sorted(adapter.stale_keys(clock.now_utc())) == ["tradovate:ACCT-TEST-194", "tradovate:ACCT-TEST-277"]
    assert [r.key for r in adapter.accounts().records] == ["tradovate:ACCT-TEST-194", "tradovate:ACCT-TEST-277"]
    assert adapter.stats.read_failures >= 2


def test_g2_reconnect_reads_one_fresh_snapshot_and_diffs(daemon, clock):
    adapter = make_adapter(daemon, clock)
    seen: list = []
    poller = adapter.poller
    orig = poller.poll
    poller.poll = lambda: (lambda ev: (seen.extend(ev), ev)[1])(orig())
    watcher = make_watcher(adapter, clock)

    assert watcher.run_once() == "stream_ended"  # empty script: the daemon drops the stream
    assert daemon.snapshot_calls == 1
    assert len(seen) == 2

    daemon.snapshot_body = snap([acct("ACCT-TEST-194", "900194", identity=IDENT_A)])
    assert watcher.run_once() == "stream_ended"  # reconnect
    assert daemon.snapshot_calls == 2  # exactly one fresh snapshot per reconnect
    assert kinds(seen[2:]) == [("removed", "tradovate:ACCT-TEST-277")]
    assert daemon.events_opened == 2


def test_g2_one_identity_401_emits_no_removed_and_marks_its_accounts_stale(daemon, clock):
    adapter = make_adapter(daemon, clock)
    adapter.account_events(NOW - timedelta(hours=1))
    assert len(adapter.accounts().records) == 2

    # identity A degrades (401 inside the daemon): its accounts vanish from `accounts`
    clock.advance(60)
    daemon.snapshot_body = snap(
        [acct("ACCT-TEST-277", "900277", identity=IDENT_B)],
        errors=[{"identity": IDENT_A, "error": "401 unauthorized"}],
    )
    events = adapter.poller.poll()
    assert events == []
    assert len(adapter.accounts().records) == 2
    assert adapter.stats.degraded_carried >= 1

    clock.advance(60.5)  # 120.5 s after A's last good confirmation
    daemon.snapshot_body = snap(
        [acct("ACCT-TEST-277", "900277", identity=IDENT_B)],
        errors=[{"identity": IDENT_A, "error": "401 unauthorized"}],
    )
    assert [e for e in adapter.poller.poll() if e.kind == "removed"] == []
    assert adapter.stale_keys(clock.now_utc()) == ["tradovate:ACCT-TEST-194"]  # only A's account

    # A recovers: nothing removed or added, A is fresh again
    daemon.snapshot_body = snap(two_accounts())
    assert adapter.poller.poll() == []
    assert adapter.stale_keys(clock.now_utc()) == []


def test_g2_whole_daemon_401_keeps_list_and_emits_nothing(daemon, clock):
    adapter = make_adapter(daemon, clock)
    adapter.account_events(NOW - timedelta(hours=1))
    daemon.token = "ANOTHER-FAKE-TOKEN-0123456789"  # every call now answers 401
    assert adapter.poller.poll() == []
    assert len(adapter.accounts().records) == 2


def test_g2_first_read_failure_raises_not_empty(daemon, clock):
    daemon.snapshot_status = 500
    adapter = make_adapter(daemon, clock)
    with pytest.raises(NtxError) as err:
        adapter.accounts()
    assert err.value.kind == "http_status"
    assert adapter.account_events(NOW) == []  # the poll helper never raises


def test_watcher_events_open_unauthorized_reports_kind_and_backs_off(daemon, clock):
    adapter = make_adapter(daemon, clock)
    daemon.token = "ANOTHER-FAKE-TOKEN-0123456789"
    watcher = make_watcher(adapter, clock)
    assert watcher.run_once() == "unauthorized"
    assert daemon.snapshot_calls == 0  # no live /snapshot behind a refused stream
    assert watcher.connects == 0


# =====================================================================================
# mapping: identities, env, routes, instruments, money
# =====================================================================================


def test_route_nt8_local_name_is_the_ntx_account_name(daemon, clock):
    adapter = make_adapter(daemon, clock)
    record = {r.key: r for r in adapter.accounts().records}["tradovate:ACCT-TEST-194"]
    assert ("nt8", FAKE_NAME) in {(r.platform, r.local_name) for r in record.routes}


def test_route_tradingview_local_name_is_d_plus_numeric_id(daemon, clock):
    adapter = make_adapter(daemon, clock)
    record = {r.key: r for r in adapter.accounts().records}["tradovate:ACCT-TEST-194"]
    assert ("tradingview", "D" + FAKE_NUM) in {(r.platform, r.local_name) for r in record.routes}
    assert len(record.routes) == 2


def test_env_comes_from_the_identity_connect_not_the_account_name(daemon, clock):
    daemon.snapshot_body = snap(
        [
            acct("DEMO123456", "900001", identity=IDENT_A, env="live"),  # name says demo, connect says live
            acct("LIVE654321", "900002", identity=IDENT_B, env="paper"),  # name says live, connect says paper
        ]
    )
    adapter = make_adapter(daemon, clock)
    envs = {r.key: r.env for r in adapter.accounts().records}
    assert envs == {"tradovate:DEMO123456": "live", "tradovate:LIVE654321": "paper"}
    assert {i.account_id: i.env for i in adapter.identities()} == {IDENT_A: "live", IDENT_B: "paper"}
    identity = adapter.connect()
    assert identity.env == "live" and adapter.env == "live"
    assert identity.broker_name == "tradovate"


def test_env_conflict_inside_one_identity_is_refused(daemon, clock):
    daemon.snapshot_body = snap(
        [acct("A-1", "900001", identity=IDENT_A, env="paper"), acct("A-2", "900002", identity=IDENT_A, env="live")]
    )
    adapter = make_adapter(daemon, clock)
    with pytest.raises(AccountRuleError) as err:
        adapter.accounts()
    assert err.value.kind == "env_conflict"


def test_env_unknown_value_is_refused(daemon, clock):
    daemon.snapshot_body = snap([acct("A-1", "900001", env="demo")])
    with pytest.raises(AccountRuleError) as err:
        make_adapter(daemon, clock).accounts()
    assert err.value.kind == "bad_env"


def test_non_numeric_id_is_refused(daemon, clock):
    daemon.snapshot_body = snap([acct("A-1", "NOT-NUMERIC")])
    with pytest.raises(AccountRuleError) as err:
        make_adapter(daemon, clock).accounts()
    assert err.value.kind == "bad_shape"


def test_duplicate_account_names_are_refused(daemon, clock):
    daemon.snapshot_body = snap([acct("A-1", "900001"), acct("A-1", "900002")])
    with pytest.raises(AccountRuleError) as err:
        make_adapter(daemon, clock).accounts()
    assert err.value.kind == "bad_shape"


def pos(symbol: str = "NQZ6", net: object = 2, avg: object = 20123.25) -> dict:
    return {"symbol": symbol, "side": "long", "quantity": abs(net), "net_pos": net, "avg_price": avg, "net_price": avg}


def trade(tid: int = 1, symbol: str = "NQZ6", side: str = "buy", qty: object = 1, price: object = 20100.5,
          at: str = "2026-11-11T14:31:00Z", fee: object = 2.1) -> dict:
    return {"id": tid, "symbol": symbol, "side": side, "quantity": qty, "price": price, "executedAt": at, "fee": fee}


def test_positions_map_to_canonical_futures_and_decimal(daemon, clock):
    daemon.snapshot_body = snap([acct(positions=[pos("NQZ6", -3, 20123.25), pos("ESH7", 1, 0.1)])])
    adapter = make_adapter(daemon, clock)
    adapter.accounts()
    got = adapter.positions("tradovate:" + FAKE_NAME)
    assert [(p.instrument.symbol, p.quantity, p.avg_price) for p in got] == [
        ("NQZ6", Decimal("-3"), Decimal("20123.25")),
        ("ESH7", Decimal("1"), Decimal("0.1")),  # shortest repr, not 0.1000000000000000055...
    ]
    assert got[0].as_of == datetime(2026, 11, 11, 17, 59, 30, tzinfo=timezone.utc)
    assert adapter.positions() == got  # one account known: the key may be omitted
    assert daemon.snapshot_calls == 1  # served from the last read, no extra /snapshot


def test_positions_need_a_key_when_more_than_one_account_is_known(daemon, clock):
    adapter = make_adapter(daemon, clock)
    adapter.accounts()
    with pytest.raises(AccountRuleError) as err:
        adapter.positions()
    assert err.value.kind == "account_required"
    with pytest.raises(AccountRuleError) as err:
        adapter.positions("tradovate:NO-SUCH")
    assert err.value.kind == "unknown_account"


def test_unknown_instrument_root_is_refused_with_a_kind(daemon, clock):
    daemon.snapshot_body = snap([acct(positions=[pos("ZZZ6")])])
    adapter = make_adapter(daemon, clock)
    adapter.accounts()
    with pytest.raises(AccountRuleError) as err:
        adapter.positions()
    assert err.value.kind == "unresolvable"


def test_unknown_instrument_month_is_refused_with_a_kind(daemon, clock):
    daemon.snapshot_body = snap([acct(trades=[trade(symbol="NQA6")])])  # A is no month code
    adapter = make_adapter(daemon, clock)
    adapter.accounts()
    with pytest.raises(AccountRuleError) as err:
        adapter.fills(NOW - timedelta(days=1))
    assert err.value.kind == "unresolvable"


def test_continuous_symbol_is_refused(daemon, clock):
    daemon.snapshot_body = snap([acct(positions=[pos("NQ")])])
    adapter = make_adapter(daemon, clock)
    adapter.accounts()
    with pytest.raises(AccountRuleError):
        adapter.positions()


def test_fills_map_side_fee_and_filter_by_since(daemon, clock):
    daemon.snapshot_body = snap(
        [
            acct(
                trades=[
                    trade(1, side="buy", at="2026-11-11T14:31:00Z", fee=2.1),
                    trade(2, side="sell", qty=2, price=20110.75, at="2026-11-11T15:00:00Z", fee=None),
                    trade(3, at="2026-11-10T14:31:00Z"),
                ]
            )
        ]
    )
    adapter = make_adapter(daemon, clock)
    adapter.accounts()
    got = adapter.fills(datetime(2026, 11, 11, 0, 0, tzinfo=timezone.utc))
    assert [f.venue_fill_id for f in got] == ["1", "2"]
    assert got[0].side is Side.BUY and got[1].side is Side.SELL
    assert got[0].fee == Decimal("2.1") and got[1].fee == Decimal(0)
    assert got[1].quantity == Decimal(2) and got[1].price == Decimal("20110.75")
    assert all(f.venue_order_id == "" for f in got)  # ntx trades carry no order id
    assert got[0].instrument.symbol == "NQZ6"


def test_nan_money_is_refused(daemon, clock):
    # json.loads accepts the NaN token; the mapping must not let it become a Decimal.
    daemon.snapshot_body = snap([acct(positions=[pos(avg=float("nan"))])])
    adapter = make_adapter(daemon, clock)
    adapter.accounts()
    with pytest.raises(AccountRuleError) as err:
        adapter.positions()
    assert err.value.kind == "bad_money"


def test_infinite_money_is_refused(daemon, clock):
    daemon.snapshot_body = snap([acct(trades=[trade(price=float("inf"))])])
    adapter = make_adapter(daemon, clock)
    adapter.accounts()
    with pytest.raises(AccountRuleError) as err:
        adapter.fills(NOW - timedelta(days=1))
    assert err.value.kind == "bad_money"


def test_nan_realized_pnl_is_refused(daemon, clock):
    daemon.snapshot_body = snap([acct(realized=float("nan"))])
    adapter = make_adapter(daemon, clock, trading_day=lambda at: at.date())
    adapter.accounts()
    with pytest.raises(AccountRuleError) as err:
        adapter.realized_pnl("tradovate:" + FAKE_NAME, date(2026, 11, 11))
    assert err.value.kind == "bad_money"


def test_realized_pnl_is_decimal_for_the_trading_day_only(daemon, clock):
    daemon.snapshot_body = snap([acct(realized=-1234.5)])
    adapter = make_adapter(daemon, clock, trading_day=lambda at: at.date())
    adapter.accounts()
    key = "tradovate:" + FAKE_NAME
    assert adapter.realized_pnl(key, date(2026, 11, 11)) == Decimal("-1234.5")
    assert adapter.realized_pnl(key, date(2026, 11, 10)) is None  # another day: unknown
    assert adapter.realized_pnl("tradovate:NO-SUCH", date(2026, 11, 11)) is None


def test_realized_pnl_without_a_day_mapping_is_none(daemon, clock):
    adapter = make_adapter(daemon, clock)
    adapter.accounts()
    assert adapter.realized_pnl("tradovate:" + FAKE_NAME, date(2026, 11, 11)) is None


def test_no_float_money_reaches_the_carriers(daemon, clock):
    daemon.snapshot_body = snap(
        [acct(positions=[pos()], trades=[trade()], realized=10.25)]
    )
    adapter = make_adapter(daemon, clock, trading_day=lambda at: at.date())
    adapter.accounts()
    values = [adapter.positions()[0].quantity, adapter.positions()[0].avg_price]
    fill = adapter.fills(NOW - timedelta(days=1))[0]
    values += [fill.quantity, fill.price, fill.fee, adapter.realized_pnl("tradovate:" + FAKE_NAME, date(2026, 11, 11))]
    assert all(isinstance(v, Decimal) for v in values)


def test_order_methods_refuse_and_send_nothing(daemon, clock):
    adapter = make_adapter(daemon, clock)
    before = (daemon.snapshot_calls, daemon.events_opened)
    for call in (lambda: adapter.submit(object()), lambda: adapter.cancel("X"), lambda: adapter.replace("X", {})):
        with pytest.raises(UnsupportedCapability):
            call()
    assert (daemon.snapshot_calls, daemon.events_opened) == before
    assert adapter.orders(NOW) == [] and adapter.cash_events(NOW) == []
    assert not adapter.capabilities.supported_order_types


# =====================================================================================
# the order-verb gate (BP-3 is read-only)
# =====================================================================================

VERB = re.compile(r"(place|cancel|modify|liquidate|flatten)", re.IGNORECASE)
VERB_WORD = re.compile(r"\b(place|cancel|modify|liquidate|flatten)", re.IGNORECASE)
GATED = sorted((Path(adapter_mod.__file__).parent).glob("*.py"))


def verb_findings(source: str) -> list[str]:
    """Order-verb calls, imports and strings in `source` (docstrings are prose, not code)."""
    tree = ast.parse(source)
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant):
                docstrings.add(id(body[0].value))
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if VERB.match(name):
                found.append(f"call {name}")
        elif isinstance(node, ast.Attribute) and VERB.match(node.attr):
            found.append(f"attribute {node.attr}")
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                if VERB.match(alias.name.split(".")[-1]):
                    found.append(f"import {alias.name}")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
            if VERB_WORD.search(node.value) or re.search(r"/(place|cancel|modify|liquidate|flatten)", node.value, re.I):
                found.append(f"string {node.value[:40]!r}")
    return found


def test_order_verb_gate_adapter_and_client_carry_no_order_verb():
    assert [p.name for p in GATED] == ["__init__.py", "adapter.py", "ntx_client.py"]
    for path in GATED:
        assert verb_findings(path.read_text(encoding="utf-8")) == [], path.name


@pytest.mark.parametrize(
    "mutant",
    [
        "client.place_order(x)",
        "self._client.placeOrder(x)",
        "client.cancel_order(1)",
        "requests.post('http://127.0.0.1:9234/orders/modify')",
        "c.liquidatePosition(1)",
        "c.flatten_all()",
        "from ntx import place_order",
        "PATH = '/cancelorder'",
    ],
)
def test_order_verb_gate_catches_each_verb(mutant):
    assert verb_findings(f"def f(client, c, x, requests, self):\n    {mutant}\n") != [] or mutant.startswith(("from", "PATH"))
    assert verb_findings(mutant) != [] or "client." in mutant or "c." in mutant or "requests" in mutant


def test_order_verb_gate_ignores_prose_and_def_names():
    ok = '"""Never place or cancel."""\n\ndef cancel(self, x):\n    """Refuse to modify."""\n    raise ValueError("read-only")\n'
    assert verb_findings(ok) == []


def test_only_get_requests_are_made():
    source = Path(client_mod.__file__).read_text(encoding="utf-8")
    methods = re.findall(r"\.request\(\s*\"([A-Z]+)\"", source)
    assert methods and set(methods) == {"GET"}
    assert not re.search(r"\.(post|put|patch|delete)\(", source, re.IGNORECASE)


# =====================================================================================
# the token never appears anywhere
# =====================================================================================


def test_token_never_appears_in_logs_exceptions_or_reprs(daemon, clock, caplog, tmp_path):
    caplog.set_level(logging.DEBUG)
    token = NtxToken(FAKE_TOKEN)
    seen: list[str] = [repr(token), str(token), f"{token!r}", f"{token}"]

    client = NtxClient(token, port=daemon.port, timeout=3.0)
    seen += [repr(client), str(client)]
    adapter = TradovateAdapter(client, clock=clock)
    seen += [repr(adapter)]
    adapter.account_events(NOW - timedelta(hours=1))

    # a 401, a 500, an unreachable daemon, a bad token file: every failure path
    daemon.token = "ANOTHER-FAKE-TOKEN-0123456789"
    for call in (client.snapshot, client.health, client.open_events):
        with pytest.raises(NtxError) as err:
            call()
        seen += [str(err.value), repr(err.value), err.value.kind, err.value.message, repr(err.value.__cause__)]
        assert err.value.__cause__ is None
        seen += [repr(err.value.__context__)]
    assert adapter.poller.poll() == []  # the adapter logs the refused read and keeps its list
    daemon.token = FAKE_TOKEN
    daemon.snapshot_status = 500
    with pytest.raises(NtxError) as err:
        client.snapshot()
    seen += [str(err.value)]
    daemon.stop()
    with pytest.raises(NtxError) as err:
        client.snapshot()
    seen += [str(err.value), repr(err.value.__cause__)]

    bad = tmp_path / "token.txt"
    bad.write_text(FAKE_TOKEN[:5])  # too short
    with pytest.raises(NtxError) as err:
        read_token(bad)
    seen += [str(err.value)]
    with pytest.raises(NtxError) as err:
        read_token(tmp_path)  # a directory
    seen += [str(err.value)]
    with pytest.raises(NtxError) as err:
        NtxToken(FAKE_TOKEN[:3] + "\n" + FAKE_TOKEN)
    seen += [str(err.value)]

    with pytest.raises(TypeError):
        pickle.dumps(token)
    with pytest.raises(AttributeError):
        token._text = "x"

    for text in seen + [r.getMessage() for r in caplog.records] + [caplog.text]:
        assert FAKE_TOKEN not in text
        assert "FAKE-NTX-TOKEN" not in text
    assert any("ntx" in r.getMessage() for r in caplog.records)  # the scan saw real log lines


def test_token_file_is_read_trimmed_and_header_only_carries_it(tmp_path):
    path = tmp_path / "secrets" / "daemon-token.txt"
    path.parent.mkdir()
    path.write_text(FAKE_TOKEN + "\r\n", encoding="utf-8")
    token = read_token(path)
    assert token.header() == {"Authorization": "Bearer " + FAKE_TOKEN}
    assert default_token_path({"NTX_STORE": str(tmp_path)}) == path
    assert default_token_path({"LOCALAPPDATA": str(tmp_path)}) == tmp_path / "ntx_tradovate" / "secrets" / "daemon-token.txt"
    with pytest.raises(NtxError) as err:
        default_token_path({})
    assert err.value.kind == "no_token_path"


def test_wrong_token_is_unauthorized(daemon):
    client = NtxClient(NtxToken("WRONG-FAKE-TOKEN-0123456789"), port=daemon.port, timeout=3.0)
    with pytest.raises(NtxError) as err:
        client.snapshot()
    assert err.value.kind == "unauthorized"
    with pytest.raises(NtxError) as err:
        client.open_events()
    assert err.value.kind == "unauthorized"


def test_client_refuses_a_non_loopback_host():
    with pytest.raises(NtxError) as err:
        NtxClient(NtxToken(FAKE_TOKEN), host="192.0.2.1")
    assert err.value.kind == "not_loopback"
    with pytest.raises(NtxError) as err:
        NtxClient("not-a-token")  # type: ignore[arg-type]
    assert err.value.kind == "bad_token"


def test_health_reports_207_for_a_degraded_identity(daemon):
    daemon.health_status = 207
    daemon.health_body = {"status": "degraded", "identities": [{"ident": IDENT_A, "degraded": True}]}
    status, body = make_client(daemon).health()
    assert status == 207 and body["status"] == "degraded"


def test_oversize_http_body_is_refused(daemon, monkeypatch):
    monkeypatch.setattr(client_mod, "MAX_BODY_BYTES", 200)
    with pytest.raises(NtxError) as err:
        make_client(daemon).snapshot()
    assert err.value.kind == "oversize_body"


def test_non_json_answer_is_a_protocol_refusal(daemon):
    daemon.snapshot_body = None  # json.dumps(None) == "null": not an object
    with pytest.raises(NtxError) as err:
        make_client(daemon).snapshot()
    assert err.value.kind == "protocol"


def test_a_log_line_never_carries_a_full_account_id_or_name(daemon, clock, caplog):
    caplog.set_level(logging.DEBUG)
    adapter = make_adapter(daemon, clock)
    adapter.account_events(NOW - timedelta(hours=1))
    daemon.snapshot_body = snap(
        [acct("ACCT-TEST-277", "900277", identity=IDENT_B)], errors=[{"identity": IDENT_A, "error": "401"}]
    )
    events = adapter.account_events(NOW - timedelta(hours=1))
    daemon.stop()
    adapter.account_events(NOW)
    text = caplog.text + repr(events) + repr(adapter.accounts())
    assert "api_ref" not in text
    for secret in (FAKE_NAME, "ACCT-TEST-277", FAKE_NUM, "900277", IDENT_A):
        assert secret not in caplog.text, secret
    assert FAKE_NUM not in caplog.text and "900277" not in caplog.text  # the id is never logged (D-13)


# =====================================================================================
# budgets (BP-COMMON): every bound has a cap, a drop policy, a counter and a test
# =====================================================================================


class SizedStream(io.BytesIO):
    """A BytesIO that records the largest single read it ever returned."""

    biggest = 0

    def readline(self, size=-1):
        out = super().readline(size)
        SizedStream.biggest = max(SizedStream.biggest, len(out))
        return out


def good_frame(n: int) -> bytes:
    return frame({"type": "sync", "identity": IDENT_A, "seq": n})


def test_budget_oversize_frame_is_dropped_and_counted_with_bounded_memory():
    huge = b"event: state_update\ndata: " + b"x" * (3 * MAX_LINE_BYTES) + b"\n\n"
    stream = SizedStream(good_frame(1) + huge + good_frame(2) + huge + good_frame(3))
    SizedStream.biggest = 0
    stats = SseStats()
    got = [p for p in SseReader(stream, stats).events() if p is not None]
    assert [p["seq"] for p in got] == [1, 2, 3]  # both oversize frames skipped, the stream survives
    assert stats.oversize_dropped == 2
    assert SizedStream.biggest <= MAX_LINE_BYTES + 1  # one bounded line is the most ever held


def test_budget_oversize_frame_through_the_daemon_does_not_end_the_watcher(daemon, clock):
    adapter = make_adapter(daemon, clock)
    huge = b"event: state_update\ndata: " + b"y" * (MAX_LINE_BYTES + 10) + b"\n\n"
    daemon.event_scripts.append([huge, KEEPALIVE, HINT])
    watcher = make_watcher(adapter, clock)
    assert watcher.run_once() == "stream_ended"
    assert adapter._client.stats.oversize_dropped == 1
    assert watcher.limiter.coalesced == 1  # the hint after the oversize frame was still seen


def test_budget_malformed_frames_are_counted_not_fatal():
    raw = b"event: state_update\ndata: {not json\n\nevent: state_update\ndata: [1]\n\n" + good_frame(7)
    stats = SseStats()
    got = [p for p in SseReader(io.BytesIO(raw), stats).events() if p is not None]
    assert [p["seq"] for p in got] == [7]
    assert stats.malformed == 2


def test_budget_reconnect_storm_one_socket_backoff_bounded_memory(daemon, clock):
    """A daemon that accepts and drops at once: delays >= 1 s and never reset, one socket
    at a time, one /snapshot per connect, and flat memory."""
    adapter = make_adapter(daemon, clock)
    sleeps: list[float] = []
    watcher = make_watcher(adapter, clock, sleeps)
    measured: list[int] = []

    def stop() -> bool:
        if len(sleeps) == 10:
            gc.collect()
            measured.append(tracemalloc.get_traced_memory()[0])
        return len(sleeps) >= 60

    tracemalloc.start()
    try:
        watcher.run(stop)
        gc.collect()
        end = tracemalloc.get_traced_memory()[0]
    finally:
        tracemalloc.stop()

    assert len(sleeps) == 60
    assert all(1.0 <= d <= 30.0 for d in sleeps)  # never a tight loop, never past the cap
    assert sleeps[:6] == [1.0, 2.0, 4.0, 8.0, 16.0, 30.0]
    assert sleeps[6:] == [30.0] * 54
    assert watcher.backoff.attempt == 60  # short connections never reset the attempt counter
    assert daemon.max_live_streams == 1  # one socket at a time
    assert daemon.events_opened == adapter._client.stats.sockets_opened == watcher.connects == 60
    assert daemon.snapshot_calls == 60  # one fresh /snapshot per reconnect, nothing else
    assert end - measured[-1] < 300_000  # flat: 50 more reconnects leave no growth
    assert len(adapter.poller.ring) <= 60  # the ring is bounded (Rust) and holds only these


def test_budget_reconnect_storm_stable_connection_resets_backoff_only_when_it_stayed_up():
    backoff = ReconnectBackoff(lambda: 0.0, stable_after=60.0)
    t0 = NOW
    for _ in range(4):
        backoff.delay()
    backoff.connected(t0)
    backoff.dropped(t0 + timedelta(seconds=59))
    assert backoff.attempt == 4  # 59 s is not stable
    backoff.connected(t0)
    backoff.dropped(t0 + timedelta(seconds=61))
    assert backoff.attempt == 0
    assert backoff.delay() == 1.0
    with pytest.raises(ValueError):
        ReconnectBackoff(lambda: 0.0, first=0)


def test_budget_reconnect_backoff_jitter_is_bounded():
    low = ReconnectBackoff(lambda: 0.0)
    high = ReconnectBackoff(lambda: 1.0)
    for _ in range(12):
        a, b = low.delay(), high.delay()
        assert 1.0 <= a <= b <= 30.0


class StubStream:
    def __init__(self, items, clock, steps):
        self._items, self._clock, self._steps = items, clock, steps

    def events(self):
        for item, step in zip(self._items, self._steps):
            self._clock.advance(step)
            yield item

    def close(self):
        pass


class StubClient:
    def __init__(self, stream):
        self._stream = stream
        self.stats = SseStats()

    def open_events(self):
        return self._stream


class StubPoller:
    def __init__(self):
        self.polls = 0

    def poll(self):
        self.polls += 1
        return []


class StubAdapter:
    def __init__(self):
        self.poller = StubPoller()


def account_hint(i: int = 0) -> dict:
    return {"type": "props", "identity": IDENT_A, "seq": i, "event_type": "Updated", "entity_type": "accounts"}


def test_budget_early_reread_limiter_at_most_one_per_5s_extras_coalesced_and_counted(clock):
    # 200 account hints in the same second, then a quiet keepalive 6 s later, then 40 more hints
    items = [account_hint(i) for i in range(200)] + [None] + [account_hint(i) for i in range(40)]
    steps = [0.0] * 200 + [6.0] + [0.0] * 40
    adapter = StubAdapter()
    stream = StubStream(items, clock, steps)
    watcher = AccountWatcher(
        adapter, StubClient(stream), clock=clock, sleep=lambda s: None, rand=lambda: 0.0  # type: ignore[arg-type]
    )
    watcher.run_once()
    # 1 connect read + 1 trailing re-read at +6 s (which also opens the window for the next 40,
    # that are coalesced again). Never one read per event.
    assert adapter.poller.polls == 2
    assert watcher.limiter.allowed == 1
    assert watcher.limiter.coalesced == 200 + 40
    assert watcher.limiter.pending  # the last burst waits for the next frame after 5 s


def test_budget_early_reread_limiter_default_window_is_5s_through_the_watcher(clock):
    assert client_mod.REFRESH_MIN_INTERVAL_SECONDS == 5.0
    assert RefreshLimiter()._min == 5.0
    # a hint every second for 10 s: with the 5 s window that is the connect read plus a
    # trailing read at 5 s and at 10 s, never one read per hint
    items = [account_hint(i) for i in range(10)]
    adapter = StubAdapter()
    watcher = AccountWatcher(
        adapter, StubClient(StubStream(items, clock, [1.0] * 10)), clock=clock, sleep=lambda s: None, rand=lambda: 0.0  # type: ignore[arg-type]
    )
    watcher.run_once()
    assert adapter.poller.polls <= 3


def test_budget_early_reread_limiter_releases_the_trailing_read_after_the_interval():
    limiter = RefreshLimiter(5)
    t0 = NOW
    limiter.note_read(t0)
    assert not limiter.trigger(t0 + timedelta(seconds=1))
    assert not limiter.trigger(t0 + timedelta(seconds=2))
    assert not limiter.due(t0 + timedelta(seconds=4.9))
    assert limiter.due(t0 + timedelta(seconds=5))
    assert not limiter.due(t0 + timedelta(seconds=11))  # released once
    assert limiter.trigger(t0 + timedelta(seconds=11))  # a fresh hint after a quiet window runs at once
    assert (limiter.allowed, limiter.coalesced) == (2, 2)
    with pytest.raises(ValueError):
        RefreshLimiter(-1)


def test_budget_only_account_and_sync_hints_trigger_a_read(clock):
    others = [
        {"type": "props", "identity": IDENT_A, "seq": 1, "entity_type": "fills"},
        {"type": "props", "identity": IDENT_A, "seq": 2, "entity_type": "positions"},
        {"type": "heartbeat"},
        None,
    ]
    adapter = StubAdapter()
    watcher = AccountWatcher(
        adapter, StubClient(StubStream(others, clock, [10.0] * 4)), clock=clock, sleep=lambda s: None, rand=lambda: 0.0  # type: ignore[arg-type]
    )
    watcher.run_once()
    assert adapter.poller.polls == 1  # the connect read only
    assert watcher.limiter.allowed == 0 and watcher.limiter.coalesced == 0


def test_budget_events_flood_through_the_daemon_never_reads_a_snapshot_per_event(daemon, clock):
    adapter = make_adapter(daemon, clock)
    daemon.event_scripts.append([HINT] * 500 + [frame({"type": "sync", "identity": IDENT_A, "seq": 9})] * 500)
    watcher = make_watcher(adapter, clock)
    assert watcher.run_once() == "stream_ended"
    assert daemon.snapshot_calls == 1  # 1000 events in one instant: only the connect read
    assert watcher.limiter.coalesced == 1000


def test_budget_account_cap_refuses_the_snapshot_with_a_kind_and_a_counter(daemon, clock):
    adapter = make_adapter(daemon, clock)
    adapter.account_events(NOW - timedelta(hours=1))  # a good list of 2 first
    daemon.snapshot_body = snap([acct(f"BULK-{i}", str(1_000_000 + i)) for i in range(ACCOUNT_CAP + 1)])
    snapshot = adapter.accounts()
    assert len(snapshot.records) == 2  # the last good list is kept
    assert adapter.stats.over_cap_refused == 1 and adapter.stats.refused_snapshots == 1
    assert adapter.poller.poll() == []  # no mass `added`, no mass `removed`
    assert len(adapter._kept) == 2


def test_budget_account_cap_is_checked_before_any_row_is_mapped(daemon, clock):
    # an over-cap snapshot whose first row is also malformed: the cap answers first, so a
    # hostile 20,000-row body never costs 20,000 record builds
    rows = [acct("BAD", "not-a-number")] + [acct(f"BULK-{i}", str(1_000_000 + i)) for i in range(ACCOUNT_CAP)]
    daemon.snapshot_body = snap(rows)
    with pytest.raises(AccountRuleError) as err:
        make_adapter(daemon, clock).accounts()
    assert err.value.kind == "over_cap"


def test_budget_account_cap_counts_the_degraded_carry(daemon, clock):
    # 2,000 accounts on identity A; then A degrades and one new account appears on B:
    # carry 2,000 + 1 new = 2,001 > cap. Refused with a kind; the last good list is kept.
    daemon.snapshot_body = snap([acct(f"BULK-{i}", str(1_000_000 + i), identity=IDENT_A) for i in range(ACCOUNT_CAP)])
    adapter = make_adapter(daemon, clock)
    assert len(adapter.accounts().records) == ACCOUNT_CAP
    clock.advance(60)
    daemon.snapshot_body = snap(
        [acct("NEW-B", "3000001", identity=IDENT_B)],
        errors=[{"identity": IDENT_A, "error": "401 unauthorized"}],
    )
    assert len(adapter.accounts().records) == ACCOUNT_CAP  # the last good list is kept
    assert adapter.stats.over_cap_refused == 1
    assert len(adapter._kept) == ACCOUNT_CAP


def test_budget_account_cap_at_the_cap_is_accepted(daemon, clock):
    daemon.snapshot_body = snap([acct(f"BULK-{i}", str(1_000_000 + i)) for i in range(ACCOUNT_CAP)])
    adapter = make_adapter(daemon, clock)
    assert len(adapter.accounts().records) == ACCOUNT_CAP
    assert adapter.stats.over_cap_refused == 0


def test_budget_account_cap_with_no_prior_list_raises_a_kind(daemon, clock):
    daemon.snapshot_body = snap([acct(f"BULK-{i}", str(1_000_000 + i)) for i in range(ACCOUNT_CAP + 1)])
    with pytest.raises(AccountRuleError) as err:
        make_adapter(daemon, clock).accounts()
    assert err.value.kind == "over_cap"


def test_budget_one_raw_snapshot_is_kept_and_replaced_each_read(daemon, clock):
    adapter = make_adapter(daemon, clock)
    for n in range(1, 6):
        daemon.snapshot_body = snap([acct(f"RAW-{i}", str(2_000_000 + i)) for i in range(n)])
        adapter.accounts()
        assert len(adapter._kept) == n  # replaced, never accumulated
    daemon.snapshot_body = snap([])
    adapter.accounts()
    assert adapter._kept == {}


def test_budget_the_degraded_carry_does_not_grow(daemon, clock):
    adapter = make_adapter(daemon, clock)
    adapter.accounts()
    degraded = snap([acct("ACCT-TEST-277", "900277", identity=IDENT_B)], errors=[{"identity": IDENT_A, "error": "401"}])
    for _ in range(25):
        daemon.snapshot_body = degraded
        adapter.accounts()
    assert len(adapter._kept) == 2 and len(adapter.accounts().records) == 2


def test_budget_per_event_parse_cost():
    """The SSE path parses frames; it does not rebuild anything per event."""
    count = 4000
    raw = b"".join(good_frame(i) for i in range(count))
    stats = SseStats()
    start = time.perf_counter()
    got = sum(1 for p in SseReader(io.BytesIO(raw), stats).events() if p is not None)
    per_event = (time.perf_counter() - start) / count
    assert got == count and stats.frames == count
    assert per_event < 0.0005  # generous: the real cost is a few microseconds


# -- tools/g3_ntx_accounts.py: the pure parts (the live run itself is not a test) -------------


def _g3():
    import importlib.util

    path = Path(__file__).resolve().parents[1] / "tools" / "g3_ntx_accounts.py"
    spec = importlib.util.spec_from_file_location("g3_ntx_accounts", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _HealthStub:
    def __init__(self, status, identities):
        self._answer = (status, {"identities": identities})

    def health(self):
        return self._answer


def test_g3_mask_keeps_the_last_three_characters_only():
    g3 = _g3()
    assert g3.mask("ACCT-TEST-194") == "***194"
    assert g3.mask("ab") == "***"
    assert "ACCT" not in g3.mask("ACCT-TEST-194")


def test_g3_health_gate_requires_200_and_every_identity_not_degraded():
    g3 = _g3()
    ok, _ = g3.health_gate(_HealthStub(200, [{"ident": "i1", "degraded": False}, {"ident": "i2", "degraded": False}]))
    assert ok
    for status, idents in (
        (207, [{"ident": "i1", "degraded": False}]),
        (200, [{"ident": "i1", "degraded": False}, {"ident": "i2", "degraded": True}]),
        (200, [{"ident": "i1"}]),  # no explicit false: not healthy
        (200, []),
    ):
        ok, why = g3.health_gate(_HealthStub(status, idents))
        assert not ok and "i2" not in why.replace("***", "")  # masked, never the whole ident


def test_g3_compare_matches_by_name_id_or_d_form_in_any_venue_and_never_double_counts():
    g3 = _g3()
    engine = [("ACCT-A", "111", "paper"), ("ACCT-B", "222", "paper"), ("ACCT-C", "333", "paper")]
    fleet = [
        {"venue": "apex", "env": "live", "id": "ACCT-A"},
        {"venue": "lucid", "env": "live", "id": "222"},
        {"venue": "fundednext", "env": "live", "id": "D333"},
        {"venue": "tpt", "env": "live", "id": "OTHER-1"},
    ]
    result = g3.compare(engine, fleet)
    assert result["count_ok"] and result["keys_ok"] and result["matched_rows"] == 3
    assert result["fleet_unmatched"] == 1
    # a missing row and a duplicated row both fail
    missing = g3.compare(engine, fleet[:2])
    assert not missing["keys_ok"] and not missing["count_ok"] and missing["engine_only"] == ["ACCT-C"]
    doubled = g3.compare(engine, fleet + [{"venue": "x", "env": "live", "id": "ACCT-A"}])
    assert not doubled["keys_ok"] and doubled["ambiguous"] == ["ACCT-A"]


def test_g3_is_read_only_loopback_and_never_prints_a_token():
    source = (Path(__file__).resolve().parents[1] / "tools" / "g3_ntx_accounts.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    verbs = {"POST", "PUT", "DELETE", "PATCH"}
    strings = {n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    assert not (strings & verbs)
    assert not {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} & {"kill", "terminate"}
    assert "taskkill" not in source and "Stop-Process" not in source
    # every print goes through masked values: no print argument names a token variable
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "print":
            names = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
            assert not names & {"token", "rg_token"}


@pytest.mark.parametrize(
    "status,identities",
    [
        (207, [{"ident": IDENT_A, "degraded": True}]),
        (200, [{"ident": IDENT_A, "degraded": False}, {"ident": IDENT_B, "degraded": True}]),
        (401, []),
    ],
)
def test_g3_blocked_on_an_unhealthy_identity_never_touches_snapshot_or_events(
    daemon, tmp_path, monkeypatch, capsys, status, identities
):
    store = tmp_path / "store" / "secrets"
    store.mkdir(parents=True)
    (store / "daemon-token.txt").write_text(FAKE_TOKEN, encoding="utf-8")
    monkeypatch.setenv("NTX_STORE", str(tmp_path / "store"))
    daemon.health_status = status
    daemon.health_body = {"identities": identities}
    code = _g3().main(["--ntx-port", str(daemon.port)])
    out = capsys.readouterr().out
    assert code == 2 and "G3 blocked on ntx" in out
    assert daemon.snapshot_calls == 0 and daemon.events_opened == 0
    assert FAKE_TOKEN not in out

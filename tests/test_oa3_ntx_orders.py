"""OA-3: trade-engine sends Tradovate orders through ntx.

A loopback fake of the ntx order endpoints only. Nothing here reaches a real daemon, riskguard,
NT8 or a broker. Accounts are fakes (`ACCT-TEST-194`).
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from trade_engine.domain.instruments import Future, Side
from trade_engine.domain.orders import OrderType, TimeInForce
from trade_engine.interfaces.broker import (
    OrderChanges,
    RiskguardVerdict,
    UnsupportedCapability,
    VenueAck,
    VenueOrder,
    VenueOrderAllocation,
)
from trade_engine.ledger import Ledger
from trade_engine.oms import OrderManager
from trade_engine.oms.precheck import (
    AccountBinding,
    PrecheckConfig,
    PrecheckConfigError,
    valid_response,
)
from trade_engine.tradovate.adapter import ORDER_ID_CAP, TradovateAdapter
from trade_engine.tradovate.ntx_client import NtxClient, NtxError, NtxToken

from test_oms import FakeBroker, FakeClock as OmsClock
from test_oms_precheck import ALLOWED, GuardServer, order as oms_order

NOW = datetime(2026, 11, 17, 15, 0, tzinfo=timezone.utc)
TOKEN = "FAKE-NTX-TOKEN-0123456789abcdef"
NTX_ACCOUNT = "ACCT-TEST-194"
ENGINE_ACCOUNT = "engine-1"
CHECKED_AT = "2026-11-17T15:00:00.000Z"
VERDICT = RiskguardVerdict(True, 7, CHECKED_AT)


class Clock:
    def now_utc(self) -> datetime:
        return NOW


class Binding:
    def __init__(self, broker_account_id: str) -> None:
        self.account_key = {"venue": "tradovate", "env": "demo", "brokerAccountId": broker_account_id}


def sent_response(path_op: str, cl: str, *, ids=None) -> dict:
    return {
        "account": "***194",
        "clOrdId": cl,
        "op": path_op,
        "outcome": "SENT",
        "orderIds": ids if ids is not None else {"entryId": 11, "stopId": 12, "targetId": 13},
        "commandId": 5,
    }


class FakeOrders:
    """A loopback fake of the three ntx order endpoints. Every POST is recorded."""

    OPS = {"/v1/orders": "place", "/v1/orders/modify": "modify", "/v1/orders/cancel": "cancel"}

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.status = 200
        self.override: dict | bytes | None = None  # a fixed answer body
        self.delay = 0.0
        self.hang = False  # read the request, then close without answering
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                body = json.loads(raw)
                fake.requests.append(
                    {"path": self.path, "auth": self.headers.get("Authorization"), "raw": raw, "body": body}
                )
                if fake.hang:
                    self.close_connection = True
                    time.sleep(fake.delay)
                    return
                op = FakeOrders.OPS[self.path]
                answer = fake.override if fake.override is not None else sent_response(op, body["clOrdId"])
                if isinstance(answer, dict) and "clOrdId" not in answer:
                    answer = {**answer, "clOrdId": body["clOrdId"], "op": op}
                payload = answer if isinstance(answer, bytes) else json.dumps(answer).encode()
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
        self.port = self._server.server_address[1]
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture
def ntx():
    fake = FakeOrders()
    yield fake
    fake.close()


def client_for(fake: FakeOrders) -> NtxClient:
    return NtxClient(NtxToken(TOKEN), port=fake.port, timeout=3.0)


def adapter_for(fake: FakeOrders) -> TradovateAdapter:
    return TradovateAdapter(
        client_for(fake), clock=Clock(), account_bindings={ENGINE_ACCOUNT: Binding(NTX_ACCOUNT)}
    )


def venue_order(
    venue_id: str = "cmd-1",
    *,
    order_type: OrderType = OrderType.MARKET,
    limit: str | None = None,
    stop: str | None = "21990.25",
    target: str | None = None,
    verdict: RiskguardVerdict | None = VERDICT,
    accounts: tuple[str, ...] = (ENGINE_ACCOUNT,),
    quantity: str = "2",
    side: Side = Side.BUY,
    tif: TimeInForce = TimeInForce.DAY,
) -> VenueOrder:
    allocations = tuple(VenueOrderAllocation(f"s-{i}", account, Decimal(quantity)) for i, account in enumerate(accounts))
    return VenueOrder(
        venue_order_id=venue_id,
        instrument=Future(root="MNQ", year=2026, month=12),
        order_type=order_type,
        side=side,
        quantity=Decimal(quantity) * len(accounts),
        submitted_at=NOW,
        tif=tif,
        limit_price=None if limit is None else Decimal(limit),
        allocations=allocations,
        protective_stop_price=None if stop is None else Decimal(stop),
        protective_target_price=None if target is None else Decimal(target),
        riskguard_verdict=verdict,
    )


# ---- the client ---------------------------------------------------------------------------


def test_client_round_trips_place_modify_and_cancel(ntx):
    client = client_for(ntx)
    place = {"account": NTX_ACCOUNT, "clOrdId": "c1", "symbol": "MNQZ6"}
    assert client.place(place)["op"] == "place"
    assert client.modify({"account": NTX_ACCOUNT, "clOrdId": "c2", "orderId": 11})["op"] == "modify"
    assert client.cancel({"account": NTX_ACCOUNT, "clOrdId": "c3", "orderId": 11})["op"] == "cancel"
    assert [r["path"] for r in ntx.requests] == ["/v1/orders", "/v1/orders/modify", "/v1/orders/cancel"]
    assert all(r["auth"] == f"Bearer {TOKEN}" for r in ntx.requests)


def test_client_maps_http_400_to_invalid_body_with_the_daemon_code(ntx):
    ntx.status = 400
    ntx.override = {"account": "", "outcome": "REFUSED", "code": "qty_invalid", "reason": "bad qty"}
    with pytest.raises(NtxError) as err:
        client_for(ntx).place({"account": NTX_ACCOUNT, "clOrdId": "c1"})
    assert err.value.kind == "invalid_body" and err.value.message == "qty_invalid"


def test_client_maps_an_unreadable_http_400_to_invalid_body(ntx):
    ntx.status = 400
    ntx.override = b"not json"
    with pytest.raises(NtxError) as err:
        client_for(ntx).place({"account": NTX_ACCOUNT, "clOrdId": "c1"})
    assert err.value.kind == "invalid_body" and err.value.message == "invalid_body"


def test_client_timeout_after_the_write_is_pending():
    fake = FakeOrders()
    fake.hang = True
    fake.delay = 1.0
    client = NtxClient(NtxToken(TOKEN), port=fake.port, timeout=3.0)
    try:
        import trade_engine.tradovate.ntx_client as mod

        original = mod.ORDER_TIMEOUT_SECONDS
        mod.ORDER_TIMEOUT_SECONDS = 0.2
        try:
            with pytest.raises(NtxError) as err:
                client.place({"account": NTX_ACCOUNT, "clOrdId": "c1"})
        finally:
            mod.ORDER_TIMEOUT_SECONDS = original
        assert err.value.kind == "pending"
        assert len(fake.requests) == 1
    finally:
        fake.close()


def test_client_failure_before_any_write_is_network():
    fake = FakeOrders()
    port = fake.port
    fake.close()
    with pytest.raises(NtxError) as err:
        NtxClient(NtxToken(TOKEN), port=port).place({"account": NTX_ACCOUNT, "clOrdId": "c1"})
    assert err.value.kind == "network"


@pytest.mark.parametrize(
    "answer",
    [
        {"outcome": "SENT"},  # missing keys
        {"account": "***194", "outcome": "MAYBE", "op": "place"},
        {"account": "***194", "clOrdId": "c1", "outcome": "SENT", "op": "cancel"},  # op not echoed
        {"account": "***194", "outcome": "SENT", "op": "place", "extra": 1},
        {"account": "***194", "outcome": "SENT", "op": "place", "orderIds": {"entryId": "x", "stopId": 1}},
        b"not json",
        b"[1]",
    ],
)
def test_client_bad_shape_is_invalid(ntx, answer):
    ntx.override = answer
    with pytest.raises(NtxError) as err:
        client_for(ntx).place({"account": NTX_ACCOUNT, "clOrdId": "c1"})
    assert err.value.kind == "invalid"


def test_client_refuses_an_oversize_body_without_writing(ntx):
    with pytest.raises(NtxError) as err:
        client_for(ntx).place({"account": NTX_ACCOUNT, "clOrdId": "c1", "pad": "x" * 20_000})
    assert err.value.kind == "oversize_body"
    assert ntx.requests == []


# ---- the adapter: place -------------------------------------------------------------------


def test_capabilities_are_market_limit_stop_day_with_native_stops(ntx):
    caps = adapter_for(ntx).capabilities
    assert caps.supported_order_types == frozenset(
        {OrderType.MARKET, OrderType.LIMIT, OrderType.STOP}
    )
    assert caps.supported_tifs == frozenset({TimeInForce.DAY})
    assert caps.supports_native_stops is True


def test_market_place_body_matches_the_wire_byte_for_byte(ntx):
    ack = adapter_for(ntx).submit(venue_order(target="22040.50"))

    assert ack.status == "ACCEPTED" and ack.venue_order_id == "cmd-1"
    expected = (
        '{"account":"ACCT-TEST-194","clOrdId":"cmd-1","symbol":"MNQZ6","side":"Buy","qty":2,'
        '"entry":{"type":"market"},"stop":"21990.25","target":"22040.50",'
        '"verdict":{"allowed":true,"verdictRevision":7,"checkedAt":"2026-11-17T15:00:00.000Z"}}'
    )
    assert ntx.requests[0]["raw"].decode() == expected


def test_limit_place_body_matches_the_wire_byte_for_byte(ntx):
    ack = adapter_for(ntx).submit(
        venue_order("cmd-2", order_type=OrderType.LIMIT, limit="22000", side=Side.SELL, quantity="3")
    )

    assert ack.status == "ACCEPTED"
    expected = (
        '{"account":"ACCT-TEST-194","clOrdId":"cmd-2","symbol":"MNQZ6","side":"Sell","qty":3,'
        '"entry":{"type":"limit","price":"22000"},"stop":"21990.25",'
        '"verdict":{"allowed":true,"verdictRevision":7,"checkedAt":"2026-11-17T15:00:00.000Z"}}'
    )
    assert ntx.requests[0]["raw"].decode() == expected


def test_a_submit_without_a_stop_is_refused_with_no_call(ntx):
    ack = adapter_for(ntx).submit(venue_order(stop=None))
    assert (ack.status, ack.message) == ("REJECTED", "no_stop")
    assert ntx.requests == []


@pytest.mark.parametrize(
    "verdict",
    [None, RiskguardVerdict(True, 7, None), RiskguardVerdict(True, None, CHECKED_AT)],
)
def test_a_submit_without_a_usable_verdict_is_refused_with_no_call(ntx, verdict):
    ack = adapter_for(ntx).submit(venue_order(verdict=verdict))
    assert (ack.status, ack.message) == ("REJECTED", "verdict_required")
    assert ntx.requests == []


def test_a_multi_account_order_raises_and_sends_nothing(ntx):
    with pytest.raises(UnsupportedCapability, match="netted orders not supported on tradovate"):
        adapter_for(ntx).submit(venue_order(accounts=(ENGINE_ACCOUNT, "engine-2")))
    assert ntx.requests == []


def test_an_unsupported_order_type_or_tif_raises(ntx):
    adapter = adapter_for(ntx)
    with pytest.raises(UnsupportedCapability):
        adapter.submit(
            VenueOrder(
                venue_order_id="cmd-s",
                instrument=Future(root="MNQ", year=2026, month=12),
                order_type=OrderType.STOP,
                side=Side.BUY,
                quantity=Decimal("1"),
                submitted_at=NOW,
                tif=TimeInForce.DAY,
                stop_price=Decimal("1"),
                allocations=(VenueOrderAllocation("s-0", ENGINE_ACCOUNT, Decimal("1")),),
                protective_stop_price=Decimal("1"),
                riskguard_verdict=VERDICT,
            )
        )
    with pytest.raises(UnsupportedCapability):
        adapter.submit(venue_order(tif=TimeInForce.GTC))
    assert ntx.requests == []


def test_an_unmapped_account_is_refused_with_no_call(ntx):
    ack = adapter_for(ntx).submit(venue_order(accounts=("engine-9",)))
    assert (ack.status, ack.message) == ("REJECTED", "unmapped_account")
    assert ntx.requests == []


def test_the_opening_leg_suffix_is_spelled_with_a_dot_on_the_wire(ntx):
    adapter = adapter_for(ntx)
    ack = adapter.submit(venue_order("exit-5:open"))
    assert ack.venue_order_id == "exit-5:open" and ack.status == "ACCEPTED"
    assert ntx.requests[0]["body"]["clOrdId"] == "exit-5.open"


def test_a_timeout_makes_exactly_one_post_and_is_pending():
    fake = FakeOrders()
    fake.hang = True
    fake.delay = 1.0
    import trade_engine.tradovate.ntx_client as mod

    original = mod.ORDER_TIMEOUT_SECONDS
    mod.ORDER_TIMEOUT_SECONDS = 0.2
    try:
        adapter = adapter_for(fake)
        ack = adapter.submit(venue_order("cmd-t"))
        assert (ack.status, ack.message) == ("PENDING", "outcome_unknown")
        time.sleep(0.3)
        assert len(fake.requests) == 1
        # the unanswered order is not known to the adapter, and nothing resends it
        assert adapter.cancel("cmd-t").message == "unknown_order"
        assert len(fake.requests) == 1
    finally:
        mod.ORDER_TIMEOUT_SECONDS = original
        fake.close()


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (403, "account_not_allowlisted"),
        (422, "no_stop"),
        (422, "instrument_not_allowed"),
        (409, "FLATTEN_IN_PROGRESS"),
        (409, "account_busy"),
        (429, "order_budget"),
        (503, "idempotency_unavailable"),
        (200, "verdict_stale"),
    ],
)
def test_each_refused_code_passes_through_as_the_message(ntx, status, code):
    ntx.status = status
    ntx.override = {"account": "***194", "outcome": "REFUSED", "code": code, "reason": "r"}
    ack = adapter_for(ntx).submit(venue_order())
    assert (ack.status, ack.message) == ("REJECTED", code)
    assert len(ntx.requests) == 1


def test_http_400_is_rejected_with_the_daemon_code(ntx):
    ntx.status = 400
    ntx.override = {"account": "", "outcome": "REFUSED", "code": "invalid_body"}
    ack = adapter_for(ntx).submit(venue_order())
    assert (ack.status, ack.message) == ("REJECTED", "invalid_body")


def test_a_202_pending_answer_is_pending(ntx):
    ntx.status = 202
    ntx.override = {"account": "***194", "outcome": "PENDING", "code": "outcome_unknown"}
    ack = adapter_for(ntx).submit(venue_order())
    assert (ack.status, ack.message) == ("PENDING", "outcome_unknown")


def test_an_unreadable_answer_is_pending_and_never_resent(ntx):
    ntx.override = b"garbage"
    ack = adapter_for(ntx).submit(venue_order())
    assert ack.status == "PENDING"
    assert len(ntx.requests) == 1


def test_a_down_daemon_is_a_network_rejection():
    fake = FakeOrders()
    adapter = adapter_for(fake)
    fake.close()
    ack = adapter.submit(venue_order())
    assert (ack.status, ack.message) == ("REJECTED", "network")


# ---- the adapter: modify and cancel -------------------------------------------------------


def placed(ntx) -> TradovateAdapter:
    adapter = adapter_for(ntx)
    assert adapter.submit(venue_order("cmd-1", order_type=OrderType.LIMIT, limit="22000")).status == "ACCEPTED"
    ntx.requests.clear()
    return adapter


def test_a_stop_modify_uses_the_stored_stop_id_and_carries_no_verdict(ntx):
    adapter = placed(ntx)

    ack = adapter.replace("cmd-1", OrderChanges(new_stop_price=Decimal("21995.5")))

    assert ack.status == "ACCEPTED"
    request = ntx.requests[0]
    assert request["path"] == "/v1/orders/modify"
    assert request["raw"].decode() == (
        '{"account":"ACCT-TEST-194","clOrdId":"cmd-1.m0","orderId":12,"qty":2,'
        '"to":{"type":"stop","price":"21995.5"}}'
    )


def test_a_limit_modify_that_opens_risk_uses_the_entry_id_and_carries_the_verdict(ntx):
    adapter = placed(ntx)

    adapter.replace("cmd-1", OrderChanges(new_limit_price=Decimal("21950"), riskguard_verdict=VERDICT))

    body = ntx.requests[0]["body"]
    assert body["orderId"] == 11 and body["to"] == {"type": "limit", "price": "21950"}
    assert body["verdict"] == {"allowed": True, "verdictRevision": 7, "checkedAt": CHECKED_AT}


def test_each_modify_gets_a_fresh_cl_ord_id(ntx):
    adapter = placed(ntx)
    adapter.replace("cmd-1", OrderChanges(new_stop_price=Decimal("21991")))
    adapter.replace("cmd-1", OrderChanges(new_stop_price=Decimal("21992")))
    assert [r["body"]["clOrdId"] for r in ntx.requests] == ["cmd-1.m0", "cmd-1.m1"]


def test_cancel_uses_the_stored_entry_id_and_forgets_the_order(ntx):
    adapter = placed(ntx)

    ack = adapter.cancel("cmd-1")

    assert ack.status == "ACCEPTED"
    request = ntx.requests[0]
    assert request["path"] == "/v1/orders/cancel"
    assert request["raw"].decode() == '{"account":"ACCT-TEST-194","clOrdId":"cmd-1.c0","orderId":11}'
    assert adapter.cancel("cmd-1").message == "unknown_order"
    assert len(ntx.requests) == 1


def test_unknown_order_on_replace_and_cancel_makes_no_call(ntx):
    adapter = adapter_for(ntx)
    assert adapter.cancel("nope").message == "unknown_order"
    ack = adapter.replace("nope", OrderChanges(new_stop_price=Decimal("1")))
    assert (ack.status, ack.message) == ("REJECTED", "unknown_order")
    assert ntx.requests == []


def test_a_refused_cancel_keeps_the_order(ntx):
    adapter = placed(ntx)
    ntx.status = 409
    ntx.override = {"account": "***194", "outcome": "REFUSED", "code": "account_busy"}
    assert adapter.cancel("cmd-1").message == "account_busy"
    ntx.status = 200
    ntx.override = None
    assert adapter.cancel("cmd-1").status == "ACCEPTED"


def test_the_order_id_map_is_bounded_and_drops_the_oldest(ntx):
    adapter = adapter_for(ntx)
    for index in range(ORDER_ID_CAP + 3):
        assert adapter.submit(venue_order(f"c{index}")).status == "ACCEPTED"
    ntx.requests.clear()
    assert adapter.cancel("c0").message == "unknown_order"
    assert adapter.cancel("c2").message == "unknown_order"
    assert adapter.cancel(f"c{ORDER_ID_CAP + 2}").status == "ACCEPTED"


def test_logs_carry_the_masked_account_and_never_the_token(ntx, caplog):
    caplog.set_level(logging.INFO, logger="trade_engine.tradovate.adapter")
    adapter = adapter_for(ntx)
    adapter.submit(venue_order())
    adapter.submit(venue_order("cmd-2", stop=None))
    text = "\n".join(record.getMessage() for record in caplog.records)
    assert "***194" in text and NTX_ACCOUNT not in text and TOKEN not in text
    assert len(caplog.records) == 2


# ---- the precheck pass-through ------------------------------------------------------------


@pytest.fixture
def guard():
    server = GuardServer()
    yield server
    server.close()


@pytest.fixture
def ledger(tmp_path):
    opened = Ledger(tmp_path / "orders.db")
    opened.open()
    return opened


def guard_config(guard: GuardServer, mode: str = "enforce") -> PrecheckConfig:
    return PrecheckConfig(
        mode=mode,
        base_url=guard.base_url,
        token="check-token",
        accounts={"acct-1": AccountBinding("core-1", {"venue": "tradovate", "env": "demo", "brokerAccountId": "123"})},
    )


def test_an_allowed_check_puts_the_verdict_on_the_order_handed_to_send(ledger, guard):
    guard.body = {**ALLOWED, "verdictRevision": 9, "checkedAt": "2026-11-17T15:00:01.000Z"}
    broker = FakeBroker()
    manager = OrderManager(broker, OmsClock(), ledger, precheck=guard_config(guard))

    manager.submit(oms_order("entry-2", Side.BUY, "2"))

    assert broker.submitted[0].riskguard_verdict == RiskguardVerdict(True, 9, "2026-11-17T15:00:01.000Z")


def test_checked_at_is_optional_in_the_response(ledger, guard):
    assert valid_response({**ALLOWED, "checkedAt": "2026-11-17T15:00:01.000Z"})
    assert valid_response(ALLOWED)
    assert not valid_response({**ALLOWED, "checkedAt": 5})
    broker = FakeBroker()
    manager = OrderManager(broker, OmsClock(), ledger, precheck=guard_config(guard))
    manager.submit(oms_order("entry-3", Side.BUY, "1"))  # an old service answer still passes the check
    assert broker.submitted[0].riskguard_verdict == RiskguardVerdict(True, None, None)


def test_a_refused_check_sends_no_verdict_and_nothing_else(ledger, guard):
    guard.body = {"allowed": False, "isReducing": False, "reasonCode": "MAX_QTY"}
    broker = FakeBroker()
    manager = OrderManager(broker, OmsClock(), ledger, precheck=guard_config(guard))
    manager.submit(oms_order("entry-4", Side.BUY, "1"))
    assert broker.submitted == []


def test_warn_mode_with_the_tradovate_adapter_fails_at_construction(ledger, guard, ntx):
    with pytest.raises(PrecheckConfigError):
        OrderManager(adapter_for(ntx), OmsClock(), ledger, precheck=guard_config(guard, "warn"))
    with pytest.raises(PrecheckConfigError):
        OrderManager(adapter_for(ntx), OmsClock(), ledger)
    OrderManager(adapter_for(ntx), OmsClock(), ledger, precheck=guard_config(guard, "enforce"))
    assert ntx.requests == []

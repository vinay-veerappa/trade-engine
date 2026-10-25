"""RiskGuard pre-check at the OMS chokepoint: the split, the client, and the wrapper's modes."""

from __future__ import annotations

import json
import logging
import socket
import threading
import time
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest

from trade_engine.domain.instruments import Equity, Side
from trade_engine.domain.orders import Order, OrderState, OrderType
from trade_engine.interfaces.broker import OrderChanges, VenueAck, VenueOrder, VenueOrderAllocation
from trade_engine.ledger import Ledger
from trade_engine.oms import OrderManagementError, OrderManager
from trade_engine.oms.precheck import (
    AccountBinding,
    Precheck,
    PrecheckConfig,
    PrecheckConfigError,
    RiskGuardClient,
    split_quantity,
)
from test_oms import FakeBroker, FakeClock, make_fill

NOW = datetime(2026, 10, 24, 15, 0, tzinfo=timezone.utc)
ACCOUNT = "acct-1"
ACCOUNT_KEY = {"venue": "tradovate", "env": "demo", "brokerAccountId": "123"}
ACCOUNT_B = "acct-2"
ACCOUNT_KEY_B = {"venue": "tradovate", "env": "demo", "brokerAccountId": "456"}
TWO_ACCOUNTS = {
    ACCOUNT: AccountBinding("core-1", ACCOUNT_KEY),
    ACCOUNT_B: AccountBinding("core-2", ACCOUNT_KEY_B),
}
ALLOWED = {"allowed": True, "isReducing": False, "reasonCode": "OK"}
REFUSED = {"allowed": False, "isReducing": False, "reasonCode": "MAX_QTY"}


class GuardServer:
    """A loopback fake of POST /v1/check. Every request is recorded, even one answered late.

    ``answers`` overrides ``body`` for one core account, so one account can be refused alone.
    """

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.status = 200
        self.body: bytes | dict = ALLOWED
        self.answers: dict[str, dict] = {}
        self.delay = 0.0
        guard = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length)
                guard.requests.append({
                    "path": self.path,
                    "auth": self.headers.get("Authorization"),
                    "body": json.loads(raw),
                })
                if guard.delay:
                    time.sleep(guard.delay)
                core = parse_qs(urlsplit(self.path).query).get("account", [""])[0]
                answer = guard.answers.get(core, guard.body)
                payload = answer if isinstance(answer, bytes) else json.dumps(answer).encode()
                try:
                    self.send_response(guard.status)
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
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture
def guard():
    server = GuardServer()
    yield server
    server.close()


def config_for(guard: GuardServer, mode: str = "enforce", accounts=None) -> PrecheckConfig:
    if accounts is None:
        accounts = {ACCOUNT: AccountBinding("core-1", ACCOUNT_KEY)}
    return PrecheckConfig(mode=mode, base_url=guard.base_url, token="check-token", accounts=accounts)


@pytest.fixture
def ledger(tmp_path):
    opened = Ledger(tmp_path / "orders.db")
    opened.open()
    yield opened


def order(order_id: str, side: Side, quantity: str, *, instrument=None, account: str = ACCOUNT) -> Order:
    return Order(
        order_id=order_id,
        account_id=account,
        instrument=instrument or Equity("AAPL"),
        order_type=OrderType.MARKET,
        side=side,
        quantity=Decimal(quantity),
        command_id=f"cmd-{order_id}",
        created_at=NOW,
    )


def seed_position(ledger: Ledger, side: Side, quantity: str, *, account: str = ACCOUNT) -> None:
    """Fill one order through a manager with no precheck, so the ledger holds the position."""
    broker = FakeBroker()
    manager = OrderManager(broker, FakeClock(), ledger)
    seed_id = f"seed-{account}-{side.value}-{quantity}"
    seeded = manager.submit(order(seed_id, side, quantity, account=account))
    manager.record_fill(make_fill(seeded, f"fill-{seed_id}", quantity, "100"))


def netted(venue_id: str, side: Side, parts: list[tuple[str, str, str]]) -> VenueOrder:
    """One venue order from ``(strategy_order_id, account, quantity)`` allocations, on AAPL."""
    allocations = tuple(VenueOrderAllocation(sid, account, Decimal(qty)) for sid, account, qty in parts)
    return VenueOrder(
        venue_order_id=venue_id, instrument=Equity("AAPL"), order_type=OrderType.MARKET, side=side,
        quantity=sum((item.quantity for item in allocations), Decimal("0")), submitted_at=NOW,
        allocations=allocations,
    )


def accepting(sent: list[VenueOrder]):
    """A ``send`` that records every venue order it is given and accepts it."""
    def send(item: VenueOrder) -> VenueAck:
        sent.append(item)
        return VenueAck(item.venue_order_id, "ACCEPTED", NOW, "test ack")
    return send


def gated(ledger: Ledger, config: PrecheckConfig) -> tuple[OrderManager, FakeBroker]:
    broker = FakeBroker()
    return OrderManager(broker, FakeClock(), ledger, precheck=config), broker


# ---- the split (pure) -------------------------------------------------------------------

@pytest.mark.parametrize(
    ("quantity", "side", "position", "expected"),
    [
        (Decimal("5"), Side.SELL, Decimal("1"), (Decimal("1"), Decimal("4"))),
        (Decimal("2"), Side.BUY, Decimal("0"), (Decimal("0"), Decimal("2"))),
        (Decimal("1"), Side.SELL, Decimal("3"), (Decimal("1"), Decimal("0"))),
        (Decimal("3"), Side.BUY, Decimal("-5"), (Decimal("3"), Decimal("0"))),
        (Decimal("4"), Side.SELL, Decimal("-2"), (Decimal("0"), Decimal("4"))),
    ],
)
def test_split_quantity_closes_the_opposite_side_first(quantity, side, position, expected):
    assert split_quantity(quantity, side, position) == expected


# ---- configuration -----------------------------------------------------------------------

@pytest.mark.parametrize(
    "section",
    [
        {"mode": "maybe", "baseUrl": "http://127.0.0.1:9", "token": "t"},
        {"mode": "warn", "baseUrl": "http://example.com:9", "token": "t"},
        {"mode": "warn", "baseUrl": "http://127.0.0.1", "token": "t"},
        {"mode": "warn", "baseUrl": "https://127.0.0.1:9", "token": "t"},
        {"mode": "warn", "baseUrl": "http://127.0.0.1:9", "token": ""},
        {"mode": "warn", "baseUrl": "http://127.0.0.1:9", "token": "t",
         "accounts": {ACCOUNT: {"coreAccount": "core-1",
                                "accountKey": {**ACCOUNT_KEY, "env": "paper"}}}},
        {"mode": "warn", "baseUrl": "http://127.0.0.1:9", "token": "t",
         "accounts": {ACCOUNT: {"coreAccount": "", "accountKey": ACCOUNT_KEY}}},
    ],
)
def test_config_refuses_malformed_sections(section):
    with pytest.raises(PrecheckConfigError):
        PrecheckConfig.from_mapping(section)


def test_config_repr_never_shows_the_token():
    config = PrecheckConfig(mode="warn", base_url="http://127.0.0.1:9", token="secret-token")
    assert "secret-token" not in repr(config)


# ---- the client --------------------------------------------------------------------------

def check_once(guard: GuardServer, request_body: dict | None = None) -> object:
    client = RiskGuardClient(guard.base_url, "check-token")
    body = request_body or {
        "accountKey": ACCOUNT_KEY, "instrument": "AAPL", "side": "long", "qty": 2,
        "isReducing": False, "origin": "strategy",
    }
    return client.check("core-1", body)


def test_client_sends_bearer_token_account_query_and_the_single_request(guard):
    guard.body = ALLOWED
    result = check_once(guard)
    assert result.allowed is True
    sent = guard.requests[0]
    assert sent["path"] == "/v1/check?account=core-1"
    assert sent["auth"] == "Bearer check-token"
    assert sent["body"]["instrument"] == "AAPL"


def test_client_reports_the_service_reason_on_refusal(guard):
    guard.body = REFUSED
    result = check_once(guard)
    assert (result.allowed, result.kind) == (False, "MAX_QTY")


@pytest.mark.parametrize(
    ("status", "body", "kind"),
    [
        (500, {"error": "boom"}, "http_500"),
        (401, {"error": "auth"}, "http_401"),
        (200, {**ALLOWED, "extra": 1}, "invalid"),
        (200, {"allowed": True, "isReducing": False}, "invalid"),
        (200, {**ALLOWED, "reasonCode": "MADE_UP"}, "invalid"),
        (200, {**ALLOWED, "allowed": "yes"}, "invalid"),
        (200, b"not json", "invalid"),
    ],
)
def test_client_fails_closed_on_bad_answers(guard, status, body, kind):
    guard.status = status
    guard.body = body
    result = check_once(guard)
    assert (result.allowed, result.kind) == (False, kind)


def test_client_times_out_at_the_250_ms_budget(guard):
    guard.body = ALLOWED
    guard.delay = 0.3
    started = time.monotonic()
    result = check_once(guard)
    elapsed = time.monotonic() - started
    assert (result.allowed, result.kind) == (False, "timeout")
    assert elapsed < 0.6


def test_client_reports_a_network_error_when_the_connection_is_dropped():
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)

    def drop_first_connection() -> None:
        peer, _ = listener.accept()
        peer.close()

    threading.Thread(target=drop_first_connection, daemon=True).start()
    try:
        client = RiskGuardClient(f"http://127.0.0.1:{listener.getsockname()[1]}", "check-token")
        result = client.check("core-1", {"accountKey": ACCOUNT_KEY})
    finally:
        listener.close()
    assert (result.allowed, result.kind) == (False, "network")


# ---- the chokepoint: submit --------------------------------------------------------------

def test_sell_five_against_long_one_sends_the_reducing_part_even_when_the_check_times_out(ledger, guard):
    seed_position(ledger, Side.BUY, "1")
    guard.body = ALLOWED
    guard.delay = 0.3
    manager, broker = gated(ledger, config_for(guard))

    result = manager.submit(order("exit-5", Side.SELL, "5"))

    assert [item.quantity for item in broker.submitted] == [Decimal("1")]
    assert broker.submitted[0].venue_order_id == "exit-5"
    assert len(guard.requests) == 1
    assert guard.requests[0]["body"]["qty"] == 4
    assert guard.requests[0]["body"]["isReducing"] is False
    assert result.state is not OrderState.REJECTED


def test_sell_five_against_long_one_sends_the_opening_part_when_the_check_allows(ledger, guard):
    seed_position(ledger, Side.BUY, "1")
    guard.body = ALLOWED
    manager, broker = gated(ledger, config_for(guard))

    manager.submit(order("exit-5", Side.SELL, "5"))

    sent = {item.venue_order_id: item.quantity for item in broker.submitted}
    assert sent == {"exit-5": Decimal("1"), "exit-5:open": Decimal("4")}


def test_buy_two_when_flat_is_refused_and_nothing_is_sent(ledger, guard):
    guard.body = REFUSED
    manager, broker = gated(ledger, config_for(guard))

    result = manager.submit(order("entry-2", Side.BUY, "2"))

    assert broker.submitted == []
    assert len(guard.requests) == 1
    assert result.state is OrderState.REJECTED


def test_buy_two_when_flat_is_sent_when_allowed_with_one_check(ledger, guard):
    guard.body = ALLOWED
    manager, broker = gated(ledger, config_for(guard))

    manager.submit(order("entry-2", Side.BUY, "2"))

    assert [item.quantity for item in broker.submitted] == [Decimal("2")]
    assert len(guard.requests) == 1


def test_sell_one_against_long_three_is_wholly_reducing_and_never_checked(ledger, guard):
    seed_position(ledger, Side.BUY, "3")
    guard.body = REFUSED
    manager, broker = gated(ledger, config_for(guard))

    manager.submit(order("exit-1", Side.SELL, "1"))

    assert [item.quantity for item in broker.submitted] == [Decimal("1")]
    assert guard.requests == []


def test_cancel_never_calls_the_check(ledger, guard):
    seed_position(ledger, Side.BUY, "3")
    guard.body = REFUSED
    manager, broker = gated(ledger, config_for(guard))
    manager.submit(order("exit-1", Side.SELL, "1"))

    manager.cancel("exit-1", command_id="cmd-cancel-exit-1")

    assert broker.cancelled == ["exit-1"]
    assert guard.requests == []


def test_an_unmapped_account_refuses_an_opening_order_without_calling_the_service(ledger, guard):
    manager, broker = gated(ledger, config_for(guard, accounts={}))

    result = manager.submit(order("entry-2", Side.BUY, "2"))

    assert broker.submitted == []
    assert guard.requests == []
    assert result.state is OrderState.REJECTED


def test_warn_mode_sends_every_order_unchanged_and_logs_the_would_be_refusal(ledger, guard, caplog):
    seed_position(ledger, Side.BUY, "1")
    guard.body = REFUSED
    manager, broker = gated(ledger, config_for(guard, mode="warn"))

    with caplog.at_level(logging.WARNING, logger="trade_engine.oms.precheck"):
        result = manager.submit(order("exit-5", Side.SELL, "5"))

    assert [item.quantity for item in broker.submitted] == [Decimal("5")]
    assert result.state is not OrderState.REJECTED
    assert "would refuse (warn)" in caplog.text
    assert "check-token" not in caplog.text


def test_off_mode_installs_no_gate(ledger, guard):
    manager, broker = gated(ledger, config_for(guard, mode="off"))

    manager.submit(order("entry-2", Side.BUY, "2"))

    assert [item.quantity for item in broker.submitted] == [Decimal("2")]
    assert guard.requests == []


# ---- the chokepoint: replace -------------------------------------------------------------

def resting_entry(ledger: Ledger, guard: GuardServer) -> tuple[OrderManager, FakeBroker]:
    guard.body = ALLOWED
    manager, broker = gated(ledger, config_for(guard))
    manager.submit(order("entry-2", Side.BUY, "2"))
    guard.requests.clear()
    return manager, broker


def test_growing_an_opening_order_is_checked_as_the_full_new_quantity(ledger, guard):
    manager, broker = resting_entry(ledger, guard)

    manager.replace("entry-2", OrderChanges(new_quantity=Decimal("3")), command_id="cmd-grow")

    assert guard.requests[0]["body"]["qty"] == 3
    assert broker.replaced[-1][0] == "entry-2"


def test_shrinking_an_opening_order_is_never_checked(ledger, guard):
    manager, broker = resting_entry(ledger, guard)

    manager.replace("entry-2", OrderChanges(new_quantity=Decimal("1")), command_id="cmd-shrink")

    assert guard.requests == []
    assert broker.replaced[-1][0] == "entry-2"


def test_a_refused_growth_is_rejected_and_not_sent(ledger, guard):
    manager, broker = resting_entry(ledger, guard)
    guard.body = REFUSED
    replaced_before = len(broker.replaced)

    with pytest.raises(OrderManagementError):
        manager.replace("entry-2", OrderChanges(new_quantity=Decimal("3")), command_id="cmd-refused")

    assert len(broker.replaced) == replaced_before
    assert len(guard.requests) == 1


def test_a_price_change_on_a_reducing_order_passes_without_a_check(ledger, guard):
    seed_position(ledger, Side.BUY, "5")
    guard.body = REFUSED
    manager, broker = gated(ledger, config_for(guard))
    stop = replace(order("stop-5", Side.SELL, "5"), order_type=OrderType.STOP, stop_price=Decimal("100"))
    manager.submit(stop)

    manager.replace("stop-5", OrderChanges(new_stop_price=Decimal("95")), command_id="cmd-stop")

    assert guard.requests == []
    assert broker.replaced[-1][0] == "stop-5"


# ---- the chokepoint: netted and multi-allocation submits ----------------------------------

def test_a_netted_order_that_only_reduces_is_sent_once_and_never_checked(ledger, guard):
    seed_position(ledger, Side.BUY, "2")
    seed_position(ledger, Side.BUY, "2", account=ACCOUNT_B)
    guard.body = REFUSED
    guard.delay = 0.3
    sent: list[VenueOrder] = []
    gate = Precheck(config_for(guard), FakeClock(), ledger)  # account B is not mapped
    venue_order = netted("net-4", Side.SELL, [("s-a", ACCOUNT, "2"), ("s-b", ACCOUNT_B, "2")])

    ack = gate.submit(venue_order, accepting(sent))

    assert sent == [venue_order]
    assert guard.requests == []
    assert ack.status == "ACCEPTED"


def test_several_allocations_in_one_account_that_only_reduce_are_sent_unchanged(ledger, guard):
    seed_position(ledger, Side.BUY, "3")
    guard.body = REFUSED
    sent: list[VenueOrder] = []
    gate = Precheck(config_for(guard), FakeClock(), ledger)
    venue_order = netted("exit-3", Side.SELL, [("s1", ACCOUNT, "1"), ("s2", ACCOUNT, "2")])

    gate.submit(venue_order, accepting(sent))

    assert sent == [venue_order]
    assert guard.requests == []


def test_a_netted_mixed_order_sends_the_reducing_part_before_checking_the_opening(ledger, guard):
    seed_position(ledger, Side.BUY, "2")
    guard.body = ALLOWED
    sent: list[VenueOrder] = []
    seen_at_send: list[tuple[str, int]] = []
    accept = accepting(sent)

    def send(item: VenueOrder) -> VenueAck:
        seen_at_send.append((item.venue_order_id, len(guard.requests)))
        return accept(item)

    gate = Precheck(config_for(guard, accounts=TWO_ACCOUNTS), FakeClock(), ledger)

    ack = gate.submit(netted("net-4", Side.SELL, [("s-a", ACCOUNT, "2"), ("s-b", ACCOUNT_B, "2")]), send)

    assert [(item.venue_order_id, item.quantity) for item in sent] == [
        ("net-4", Decimal("2")), ("net-4:open", Decimal("2")),
    ]
    assert [item.account_id for item in sent[0].allocations] == [ACCOUNT]
    assert [item.account_id for item in sent[1].allocations] == [ACCOUNT_B]
    assert seen_at_send == [("net-4", 0), ("net-4:open", 1)]
    assert [(r["path"], r["body"]["qty"], r["body"]["isReducing"]) for r in guard.requests] == [
        ("/v1/check?account=core-2", 2, False),
    ]
    assert ack.status == "ACCEPTED"


def test_a_refused_opening_account_sends_only_the_reducing_part(ledger, guard):
    seed_position(ledger, Side.BUY, "2")
    guard.answers = {"core-2": REFUSED}
    sent: list[VenueOrder] = []
    gate = Precheck(config_for(guard, accounts=TWO_ACCOUNTS), FakeClock(), ledger)

    ack = gate.submit(netted("net-4", Side.SELL, [("s-a", ACCOUNT, "2"), ("s-b", ACCOUNT_B, "2")]), accepting(sent))

    assert [item.venue_order_id for item in sent] == ["net-4"]
    assert ack.status == "ACCEPTED"
    assert ack.message == "opening 2 refused: MAX_QTY"


def test_an_allocation_cut_at_the_boundary_builds_two_valid_parts(ledger, guard):
    seed_position(ledger, Side.BUY, "1")
    guard.body = ALLOWED
    sent: list[VenueOrder] = []
    gate = Precheck(config_for(guard), FakeClock(), ledger)

    gate.submit(netted("cut-3", Side.SELL, [("s1", ACCOUNT, "2"), ("s2", ACCOUNT, "1")]), accepting(sent))

    parts = {item.venue_order_id: (item.quantity, [(a.strategy_order_id, a.quantity) for a in item.allocations])
             for item in sent}
    assert parts == {
        "cut-3": (Decimal("1"), [("s1", Decimal("1"))]),
        "cut-3:open": (Decimal("2"), [("s1", Decimal("1")), ("s2", Decimal("1"))]),
    }
    assert [r["body"]["qty"] for r in guard.requests] == [2]


def test_a_netted_opening_order_is_sent_once_when_every_account_allows(ledger, guard):
    guard.body = ALLOWED
    sent: list[VenueOrder] = []
    gate = Precheck(config_for(guard, accounts=TWO_ACCOUNTS), FakeClock(), ledger)
    venue_order = netted("buy-4", Side.BUY, [("s-a", ACCOUNT, "2"), ("s-b", ACCOUNT_B, "2")])

    gate.submit(venue_order, accepting(sent))

    assert sent == [venue_order]
    assert [r["path"] for r in guard.requests] == ["/v1/check?account=core-1", "/v1/check?account=core-2"]


def test_a_netted_opening_order_is_refused_when_one_account_is_refused(ledger, guard):
    guard.answers = {"core-2": REFUSED}
    sent: list[VenueOrder] = []
    gate = Precheck(config_for(guard, accounts=TWO_ACCOUNTS), FakeClock(), ledger)

    ack = gate.submit(netted("buy-4", Side.BUY, [("s-a", ACCOUNT, "2"), ("s-b", ACCOUNT_B, "2")]), accepting(sent))

    assert sent == []
    assert ack.status == "REJECTED"
    assert ack.message == "riskguard precheck refused: MAX_QTY"


def test_the_opening_part_on_an_unmapped_account_is_refused_and_never_sent_to_the_service(ledger, guard):
    seed_position(ledger, Side.BUY, "2")
    guard.body = ALLOWED
    sent: list[VenueOrder] = []
    gate = Precheck(config_for(guard), FakeClock(), ledger)  # account B is not mapped

    ack = gate.submit(netted("net-4", Side.SELL, [("s-a", ACCOUNT, "2"), ("s-b", ACCOUNT_B, "2")]), accepting(sent))

    assert [(item.venue_order_id, item.quantity) for item in sent] == [("net-4", Decimal("2"))]
    assert guard.requests == []
    assert ack.message == "opening 2 refused: unmapped_account"


def test_warn_on_a_netted_mixed_order_logs_one_refusal_naming_the_account(ledger, guard, caplog):
    seed_position(ledger, Side.BUY, "2")
    guard.answers = {"core-2": REFUSED}
    sent: list[VenueOrder] = []
    gate = Precheck(config_for(guard, mode="warn", accounts=TWO_ACCOUNTS), FakeClock(), ledger)
    venue_order = netted("net-4", Side.SELL, [("s-a", ACCOUNT, "2"), ("s-b", ACCOUNT_B, "2")])

    with caplog.at_level(logging.WARNING, logger="trade_engine.oms.precheck"):
        gate.submit(venue_order, accepting(sent))

    assert sent == [venue_order]
    assert caplog.text.count("would refuse (warn)") == 1
    assert "account=acct-2" in caplog.text


def test_warn_on_a_netted_order_that_only_reduces_logs_nothing(ledger, guard, caplog):
    seed_position(ledger, Side.BUY, "2")
    seed_position(ledger, Side.BUY, "2", account=ACCOUNT_B)
    guard.body = REFUSED
    gate = Precheck(config_for(guard, mode="warn", accounts=TWO_ACCOUNTS), FakeClock(), ledger)

    with caplog.at_level(logging.WARNING, logger="trade_engine.oms.precheck"):
        gate.submit(netted("net-4", Side.SELL, [("s-a", ACCOUNT, "2"), ("s-b", ACCOUNT_B, "2")]), accepting([]))

    assert guard.requests == []
    assert "would refuse" not in caplog.text


class NettingBroker(FakeBroker):
    """Acks every submit under one shared venue id, as a netted venue order does."""

    def submit(self, order: VenueOrder) -> VenueAck:
        self.submitted.append(order)
        return VenueAck("net-1", "ACCEPTED", NOW, "test ack")


def test_a_replace_of_a_netted_order_is_sent_unchecked_and_logged(ledger, guard, caplog):
    guard.body = ALLOWED
    broker = NettingBroker()
    manager = OrderManager(broker, FakeClock(), ledger, precheck=config_for(guard, accounts=TWO_ACCOUNTS))
    for order_id, account in (("n-a", ACCOUNT), ("n-b", ACCOUNT_B)):
        priced = replace(order(order_id, Side.BUY, "2", account=account), order_type=OrderType.LIMIT,
                         limit_price=Decimal("100"))
        manager.submit(priced)
    guard.requests.clear()
    guard.body = REFUSED

    with caplog.at_level(logging.WARNING, logger="trade_engine.oms.precheck"):
        manager.replace("n-a", OrderChanges(new_quantity=Decimal("3")), command_id="cmd-shrink")
        manager.replace("n-a", OrderChanges(new_limit_price=Decimal("101")), command_id="cmd-price")

    assert guard.requests == []
    assert [changes.new_quantity for _, changes in broker.replaced] == [Decimal("3"), None]
    assert [changes.new_limit_price for _, changes in broker.replaced] == [None, Decimal("101")]
    assert "replace of a netted order not checked: venue_order=net-1 accounts=acct-1,acct-2" in caplog.text

"""The RiskGuard pre-check client and gate against a spawned ``riskguard-service``.

This is the offline proof the merged pre-check (W7-VELA ``911994b``, the K-W7-1 fix
``0d1fe64``) lacked: every case runs the engine's own objects -- ``RiskGuardClient``,
``PrecheckConfig.from_mapping`` and, once, ``OrderManager(..., precheck=...)`` -- against
a real service the test spawns on an ephemeral loopback port, and pins the hand-mirrored
wire constants in ``trade_engine.oms.precheck`` to the contract schemas.

What this file deliberately does NOT prove (case f is out of scope): an ALLOWED opening
through the real service. Registering the account, its observations and its heartbeat in
the service's wall-clock time was not derived for this slice, so the allowed-opening path
is proven only against ``GuardServer`` in ``tests/test_oms_precheck.py`` and at the desk
check before ``enforce`` sends an opening.

The whole file skips unless ``RISKGUARD_SERVICE_EXE`` names a built service binary; the
schema test additionally skips unless ``RISKGUARD_REPO`` names a checkout holding
``contracts/schemas/check.schema.json``. The service gets test-only tokens and its own
journal directory. No broker and no NinjaTrader is involved.
"""

from __future__ import annotations

import http.client
import json
import os
import queue
import subprocess
import threading
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from urllib.parse import quote, urlsplit

import pytest

from trade_engine.domain.instruments import Equity, Side
from trade_engine.domain.orders import Order, OrderState, OrderType
from trade_engine.interfaces.broker import VenueAck, VenueOrder, VenueOrderAllocation
from trade_engine.ledger import Ledger
from trade_engine.oms import OrderManager, precheck
from trade_engine.oms.precheck import (
    CHECK_TIMEOUT_S,
    CheckResult,
    Precheck,
    PrecheckConfig,
    RiskGuardClient,
)
from test_oms import FakeBroker, FakeClock, make_fill
from test_oms_precheck import GuardServer

NOW = datetime(2026, 10, 27, 15, 0, tzinfo=timezone.utc)
ACCOUNT = "acct-1"
ACCOUNT_KEY = {"venue": "tradovate", "env": "demo", "brokerAccountId": "123"}
# A core account the freshly spawned service has never been told about.
UNREGISTERED = "vela-test-unregistered"

# Test constants, never real secrets.
CHECK_TOKEN = "vela-test-check-token"
DESK_TOKEN = "vela-test-desk-token"

SERVICE_EXE = os.environ.get("RISKGUARD_SERVICE_EXE", "")
RISKGUARD_REPO = os.environ.get("RISKGUARD_REPO", "")
HAS_SERVICE = bool(SERVICE_EXE) and os.path.isfile(SERVICE_EXE)
HAS_SCHEMA = bool(RISKGUARD_REPO) and os.path.isfile(
    os.path.join(RISKGUARD_REPO, "contracts", "schemas", "check.schema.json")
)

pytestmark = pytest.mark.skipif(
    not HAS_SERVICE, reason="RISKGUARD_SERVICE_EXE does not name a built riskguard-service"
)


# ---- the spawned service ----------------------------------------------------------------

def _spawn(journal_dir: Path) -> tuple[subprocess.Popen, str]:
    """Start the service on an ephemeral port and read its ``listening on`` line.

    The environment is a copy of this one with every ``RISKGUARD_SERVICE_*`` key removed,
    so nothing a developer exported (a token, a port, a journal dir) leaks in.
    """
    env = {key: value for key, value in os.environ.items() if not key.startswith("RISKGUARD_SERVICE_")}
    env.update(
        {
            "RISKGUARD_SERVICE_PORT": "0",
            "RISKGUARD_SERVICE_JOURNAL_DIR": str(journal_dir),
            "RISKGUARD_SERVICE_TOKENS": json.dumps(
                {
                    "version": 1,
                    "tokens": [
                        {"token": CHECK_TOKEN, "role": "check", "principal": "vela-test-check"},
                        {"token": DESK_TOKEN, "role": "desk", "principal": "vela-test-desk"},
                    ],
                }
            ),
        }
    )
    process = subprocess.Popen(
        [SERVICE_EXE], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    # Drain both pipes so a chatty failure cannot block the child.
    errors: list[str] = []
    lines: queue.Queue[str] = queue.Queue()

    def drain_stderr() -> None:
        errors.extend(process.stderr)

    def drain_stdout() -> None:
        for line in process.stdout:
            lines.put(line)

    threading.Thread(target=drain_stderr, daemon=True).start()
    threading.Thread(target=drain_stdout, daemon=True).start()

    deadline = time.monotonic() + 10.0
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AssertionError(
                f"riskguard-service printed no 'listening on' line within 10 s: {''.join(errors)}"
            )
        if process.poll() is not None:
            raise AssertionError(
                f"riskguard-service exited with {process.returncode}: {''.join(errors)}"
            )
        try:
            line = lines.get(timeout=min(remaining, 0.1))
        except queue.Empty:
            continue
        if line.startswith("listening on "):
            # "listening on 127.0.0.1:54321 clock=system"
            return process, line.split()[2]


@pytest.fixture(scope="module")
def service(tmp_path_factory) -> str:
    """One real ``riskguard-service`` for the file, on an ephemeral loopback port."""
    journal_dir = tmp_path_factory.mktemp("riskguard-service-journal")
    process, address = _spawn(journal_dir)
    try:
        yield f"http://127.0.0.1:{address.rsplit(':', 1)[1]}"
    finally:
        process.terminate()
        try:
            process.wait(5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(5)


# ---- engine objects ---------------------------------------------------------------------

@pytest.fixture
def ledger(tmp_path):
    opened = Ledger(tmp_path / "orders.db")
    opened.open()
    yield opened


def config_for(service: str, mode: str = "enforce", *, token: str = CHECK_TOKEN) -> PrecheckConfig:
    """An ``enforce`` config whose engine account maps to an unregistered core account."""
    return PrecheckConfig.from_mapping(
        {
            "mode": mode,
            "baseUrl": service,
            "token": token,
            "accounts": {ACCOUNT: {"coreAccount": UNREGISTERED, "accountKey": ACCOUNT_KEY}},
        }
    )


def order(order_id: str, side: Side, quantity: str, *, account: str = ACCOUNT) -> Order:
    return Order(
        order_id=order_id,
        account_id=account,
        instrument=Equity("AAPL"),
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
        venue_order_id=venue_id,
        instrument=Equity("AAPL"),
        order_type=OrderType.MARKET,
        side=side,
        quantity=sum((item.quantity for item in allocations), Decimal("0")),
        submitted_at=NOW,
        allocations=allocations,
    )


def accepting(sent: list[VenueOrder]):
    """A ``send`` that records every venue order it is given and accepts it."""

    def send(item: VenueOrder) -> VenueAck:
        sent.append(item)
        return VenueAck(item.venue_order_id, "ACCEPTED", NOW, "test ack")

    return send


def _request(*, is_reducing: bool, qty: int = 5, side: str = "long") -> dict:
    return {
        "accountKey": ACCOUNT_KEY,
        "instrument": "AAPL",
        "side": side,
        "qty": qty,
        "isReducing": is_reducing,
        "origin": "strategy",
    }


def _raw_check(base_url: str, core_account: str, request: dict, token: str) -> tuple[int, dict]:
    """The one raw HTTP call here: the unparsed 200 body ``_valid_response`` is given."""
    parts = urlsplit(base_url)
    connection = http.client.HTTPConnection(parts.hostname, parts.port, timeout=5)
    try:
        connection.request(
            "POST",
            f"/v1/check?account={quote(core_account, safe='')}",
            body=json.dumps(request).encode(),
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


# ---- the real service -------------------------------------------------------------------

def test_an_opening_on_an_unregistered_account_is_blocked(service, ledger):
    gate = Precheck(config_for(service), FakeClock(), ledger)

    result = gate._check(ACCOUNT, Equity("AAPL"), Side.BUY, Decimal("2"))

    assert result == CheckResult(False, "ACCOUNT_BLOCKED")


def test_a_reducing_intent_is_allowed_on_an_unregistered_account(service):
    config = config_for(service)
    request = _request(is_reducing=True)

    result = RiskGuardClient(config.base_url, config.token).check(UNREGISTERED, request)

    assert result == CheckResult(True, "allowed")
    status, body = _raw_check(config.base_url, UNREGISTERED, request, config.token)
    assert status == 200
    assert body["reasonCode"] == "OK_REDUCING"
    assert precheck._valid_response(body)


def test_a_wrong_token_is_401_and_a_desk_token_is_403(service, ledger):
    wrong = Precheck(config_for(service, token="vela-test-not-a-token"), FakeClock(), ledger)
    assert wrong._check(ACCOUNT, Equity("AAPL"), Side.BUY, Decimal("2")) == CheckResult(False, "http_401")

    desk = Precheck(config_for(service, token=DESK_TOKEN), FakeClock(), ledger)
    assert desk._check(ACCOUNT, Equity("AAPL"), Side.BUY, Decimal("2")) == CheckResult(False, "http_403")


def test_sell_five_against_long_one_sends_one_and_refuses_the_opening_four(service, ledger):
    seed_position(ledger, Side.BUY, "1")
    config = config_for(service)
    broker = FakeBroker()
    manager = OrderManager(broker, FakeClock(), ledger, precheck=config)

    result = manager.submit(order("exit-5", Side.SELL, "5"))

    # The reducing part went at once, under the strategy order's own venue id.
    assert [(item.venue_order_id, item.quantity) for item in broker.submitted] == [
        ("exit-5", Decimal("1"))
    ]
    assert result.state is OrderState.ACCEPTED
    # OrderManager records the ack, not its message, so the gate's ack is read at the seam
    # it is made at: same config, same ledger mirror, a recording send.
    sent: list[VenueOrder] = []
    gate_ack = Precheck(config, FakeClock(), ledger).submit(
        netted("exit-5b", Side.SELL, [("s1", ACCOUNT, "5")]), accepting(sent)
    )
    assert [(item.venue_order_id, item.quantity) for item in sent] == [("exit-5b", Decimal("1"))]
    assert gate_ack.status == "ACCEPTED"
    assert gate_ack.message == "opening 4 refused: ACCOUNT_BLOCKED"


def test_the_answer_arrives_inside_the_time_budget(service):
    config = config_for(service)
    client = RiskGuardClient(config.base_url, config.token)
    client.check(UNREGISTERED, _request(is_reducing=True))  # warm-up, discarded

    started = time.monotonic()
    result = client.check(UNREGISTERED, _request(is_reducing=False))
    elapsed = time.monotonic() - started

    assert result == CheckResult(False, "ACCOUNT_BLOCKED")
    assert elapsed < CHECK_TIMEOUT_S


# ---- the contract schemas ---------------------------------------------------------------

def _schema(name: str) -> dict:
    path = Path(RISKGUARD_REPO) / "contracts" / "schemas" / name
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.skipif(not HAS_SCHEMA, reason="RISKGUARD_REPO does not hold contracts/schemas/check.schema.json")
def test_the_hand_mirrored_wire_constants_match_the_contract_schemas(ledger):
    check_schema = _schema("check.schema.json")
    common = _schema("common.schema.json")
    defs = check_schema["$defs"]
    common_defs = common["$defs"]

    assert precheck.REASON_CODES == set(common_defs["ReasonCode"]["enum"])
    assert precheck.RESPONSE_KEYS == set(defs["singleResponse"]["properties"])
    assert precheck.REQUIRED_RESPONSE_KEYS == set(defs["singleResponse"]["required"])
    assert precheck.ROOT_PATTERN.pattern == common_defs["instrumentRoot"]["pattern"]
    assert list(precheck.ACCOUNT_ENVS) == common_defs["accountKey"]["properties"]["env"]["enum"]
    assert precheck.ORIGIN in common_defs["origin"]["enum"]

    # The request Precheck._check builds, captured at the fake's door rather than reasoned about.
    guard = GuardServer()
    try:
        config = PrecheckConfig.from_mapping(
            {
                "mode": "enforce",
                "baseUrl": guard.base_url,
                "token": "schema-token",
                "accounts": {ACCOUNT: {"coreAccount": "core-1", "accountKey": ACCOUNT_KEY}},
            }
        )
        Precheck(config, FakeClock(), ledger)._check(ACCOUNT, Equity("AAPL"), Side.BUY, Decimal("2"))
    finally:
        guard.close()

    assert len(guard.requests) == 1
    assert set(guard.requests[0]["body"]) == set(defs["singleRequest"]["required"])

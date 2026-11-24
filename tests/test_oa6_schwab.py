"""OA-6: Schwab order submit through the hub. No network: a fake hub transport stands in."""

from __future__ import annotations

import json
import time
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from trade_engine.domain import Equity, OptionContract, OrderType, Side, TimeInForce
from trade_engine.domain.instruments import OptionRight
from trade_engine.interfaces.broker import OrderChanges, UnsupportedCapability, VenueOrder, VenueOrderAllocation
from trade_engine.oms.precheck import Precheck
from trade_engine.schwab.order_adapter import ORDER_ID_CAP, SchwabOrderAdapter
from trade_engine.schwab.orders import HubOrdersClient, build_order_json, match_pending, schwab_time
from test_oms import NOW as CLOCK_NOW, FakeClock
from test_oms_precheck import (  # noqa: F401 - fixtures used by name
    ACCOUNT,
    NOW,
    REFUSED,
    config_for,
    guard,
    ledger,
    seed_position,
)

FIXTURES = Path(__file__).parent / "fixtures" / "oa6"
HUB = "http://127.0.0.1:8090"
HASH = "HASH-ABC123"
AAPL = Equity("AAPL")
CALL = OptionContract("AAPL", date(2026, 12, 18), Decimal("200"), OptionRight.CALL)


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def venue_order(vid: str = "cmd-1", *, side: Side = Side.BUY, qty: str = "10", order_type=OrderType.LIMIT,
                instrument=AAPL, tif=TimeInForce.DAY, limit=Decimal("187.25"), stop=None,
                account: str = ACCOUNT, **extra) -> VenueOrder:
    return VenueOrder(
        venue_order_id=vid, instrument=instrument, order_type=order_type, side=side,
        quantity=Decimal(qty), submitted_at=NOW, tif=tif, limit_price=limit, stop_price=stop,
        allocations=(VenueOrderAllocation(f"s-{vid}", account, Decimal(qty)),), **extra,
    )


class FakeHub:
    """Records every hub call; answers from a queue (or the default), or raises an exception."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict | None]] = []
        self.queue: list = []
        self.default = (201, json.dumps({"status": 201, "order_id": "1001", "body": ""}))

    def __call__(self, method, url, body, headers, timeout):
        self.calls.append((method, url, json.loads(body) if body else None))
        answer = self.queue.pop(0) if self.queue else self.default
        if isinstance(answer, BaseException):
            raise answer
        return answer


def listing(orders: list[dict]) -> tuple[int, str]:
    return 200, json.dumps({"status": 200, "order_id": None, "body": json.dumps(orders)})


def listed(order_id: str, *, symbol="AAPL", instruction="BUY", qty=10, entered=CLOCK_NOW) -> dict:
    return {"orderId": int(order_id), "enteredTime": entered.strftime("%Y-%m-%dT%H:%M:%S+0000"),
            "orderLegCollection": [{"instruction": instruction, "quantity": qty,
                                    "instrument": {"symbol": symbol}}]}


def adapter(hub: FakeHub, *, position=Decimal("0"), precheck=None, hashes=None, journal=None) -> SchwabOrderAdapter:
    client = HubOrdersClient(HUB, transport=hub)
    extra = {} if journal is None else {"journal": journal}
    return SchwabOrderAdapter(client, hashes if hashes is not None else {ACCOUNT: HASH}, FakeClock(),
                              lambda account, instrument: position, precheck=precheck, **extra)


# --- the pure builder ------------------------------------------------------------------------------

def test_equity_limit_matches_fixture():
    assert build_order_json(venue_order()) == fixture("equity_limit.json")


def test_option_buy_to_open_matches_fixture():
    order = venue_order(instrument=CALL, qty="2", tif=TimeInForce.GTC, limit=Decimal("5.10"))
    assert build_order_json(order) == fixture("option_bto.json")


def test_bracket_matches_fixture():
    order = venue_order(qty="100", protective_stop_price=Decimal("185.00"),
                        protective_target_price=Decimal("192.50"))
    assert build_order_json(order) == fixture("bracket.json")


def test_equity_market_and_stop_builds():
    market = build_order_json(venue_order(order_type=OrderType.MARKET, limit=None))
    assert market["orderType"] == "MARKET" and "price" not in market and "stopPrice" not in market
    stop = build_order_json(venue_order(side=Side.SELL, order_type=OrderType.STOP, limit=None,
                                        stop=Decimal("180.5")), position=Decimal("10"))
    assert stop["orderType"] == "STOP" and stop["stopPrice"] == "180.5"
    assert stop["orderLegCollection"][0]["instruction"] == "SELL"


def test_short_and_option_close_instructions():
    short = build_order_json(venue_order(side=Side.SELL))
    assert short["orderLegCollection"][0]["instruction"] == "SELL_SHORT"
    cover = build_order_json(venue_order(), position=Decimal("-10"))
    assert cover["orderLegCollection"][0]["instruction"] == "BUY_TO_COVER"
    stc = build_order_json(venue_order(side=Side.SELL, instrument=CALL, qty="2"), position=Decimal("2"))
    assert stc["orderLegCollection"][0]["instruction"] == "SELL_TO_CLOSE"


def test_unsupported_tif_and_multi_account_raise():
    with pytest.raises(UnsupportedCapability):
        build_order_json(venue_order(tif=TimeInForce.MOC))
    two = VenueOrder(
        venue_order_id="n", instrument=AAPL, order_type=OrderType.MARKET, side=Side.BUY,
        quantity=Decimal("2"), submitted_at=NOW,
        allocations=(VenueOrderAllocation("a", ACCOUNT, Decimal("1")),
                     VenueOrderAllocation("b", "acct-2", Decimal("1"))),
    )
    with pytest.raises(UnsupportedCapability):
        adapter(FakeHub()).submit(two)


def test_client_refuses_a_non_loopback_hub():
    with pytest.raises(ValueError):
        HubOrdersClient("http://example.com")


# --- submit / cancel / replace ---------------------------------------------------------------------

def test_submit_posts_once_and_records_the_schwab_id():
    hub = FakeHub()
    venue = adapter(hub)
    ack = venue.submit(venue_order())
    assert ack.status == "ACCEPTED" and ack.message is None
    assert hub.calls == [("POST", f"{HUB}/accounts/{HASH}/orders", fixture("equity_limit.json"))]
    assert venue.schwab_order_id("cmd-1") == "1001"


def test_cancel_and_replace_keep_the_instruction():
    hub = FakeHub()
    venue = adapter(hub, position=Decimal("10"))
    venue.submit(venue_order(side=Side.SELL, order_type=OrderType.STOP, limit=None, stop=Decimal("180")))
    hub.queue = [(200, json.dumps({"status": 201, "order_id": "1002", "body": ""}))]
    ack = venue.replace("cmd-1", OrderChanges(new_stop_price=Decimal("181")))
    assert ack.status == "ACCEPTED"
    method, url, body = hub.calls[-1]
    assert (method, url) == ("PUT", f"{HUB}/accounts/{HASH}/orders/1001")
    assert body["stopPrice"] == "181" and body["orderLegCollection"][0]["instruction"] == "SELL"
    assert venue.schwab_order_id("cmd-1") == "1002"
    hub.queue = [(200, json.dumps({"status": 200, "order_id": "1002", "body": ""}))]
    assert venue.cancel("cmd-1").status == "ACCEPTED"
    assert hub.calls[-1][:2] == ("DELETE", f"{HUB}/accounts/{HASH}/orders/1002")


def test_venue_reject_and_hub_refusal():
    hub = FakeHub()
    hub.queue = [(200, json.dumps({"status": 400, "order_id": None, "body": json.dumps({"message": "no funds"})})),
                 (403, json.dumps({"error": "orders_disabled"}))]
    venue = adapter(hub)
    first = venue.submit(venue_order("a"))
    assert (first.status, first.message) == ("REJECTED", "venue_rejected:400:no funds")
    second = venue.submit(venue_order("b"))
    assert (second.status, second.message) == ("REJECTED", "hub_refused:orders_disabled")


def test_unmapped_account_is_refused_without_a_call():
    hub = FakeHub()
    ack = adapter(hub, hashes={}).submit(venue_order())
    assert (ack.status, ack.message) == ("REJECTED", "unmapped_account")
    assert hub.calls == []


def test_cancel_unknown_and_unresolved():
    hub = FakeHub()
    venue = adapter(hub)
    assert venue.cancel("nope").message == "unknown_order"
    hub.queue = [TimeoutError()]
    venue.submit(venue_order())
    assert venue.cancel("cmd-1").message == "unresolved_order"
    assert len(hub.calls) == 1


# --- D-5 -------------------------------------------------------------------------------------------

def test_timeout_leaves_pending_and_a_repeat_never_resends():
    hub = FakeHub()
    hub.queue = [TimeoutError()]
    venue = adapter(hub)
    ack = venue.submit(venue_order())
    assert (ack.status, ack.message) == ("PENDING", "outcome_unknown")
    again = venue.submit(venue_order())
    assert again.status == "PENDING"
    assert len(hub.calls) == 1


def pending_adapter(hub: FakeHub) -> SchwabOrderAdapter:
    hub.queue = [TimeoutError()]
    venue = adapter(hub)
    venue.submit(venue_order())
    return venue


def test_resolve_one_match_is_accepted():
    hub = FakeHub()
    venue = pending_adapter(hub)
    hub.queue = [listing([listed("2001"), listed("2002", qty=5)])]
    [ack] = venue.resolve_pending()
    assert ack.status == "ACCEPTED" and venue.schwab_order_id("cmd-1") == "2001"
    method, url, _ = hub.calls[-1]
    assert method == "GET" and url.startswith(f"{HUB}/accounts/{HASH}/orders?fromEnteredTime=")
    assert venue.resolve_pending() == []  # nothing left pending, no further read
    assert len(hub.calls) == 2


def test_resolve_none_is_not_at_venue():
    hub = FakeHub()
    venue = pending_adapter(hub)
    hub.queue = [listing([listed("2001", entered=CLOCK_NOW - timedelta(hours=1))])]
    [ack] = venue.resolve_pending()
    assert (ack.status, ack.message) == ("REJECTED", "not_at_venue")


def test_resolve_ambiguous_stays_pending(caplog):
    hub = FakeHub()
    venue = pending_adapter(hub)
    hub.queue = [listing([listed("2001"), listed("2002")])]
    [ack] = venue.resolve_pending()
    assert ack.status == "PENDING"
    assert "more than one" in caplog.text
    assert [c[0] for c in hub.calls] == ["POST", "GET"]


def test_failed_list_read_leaves_pending():
    hub = FakeHub()
    venue = pending_adapter(hub)
    hub.queue = [TimeoutError()]
    assert venue.resolve_pending() == []
    hub.queue = [listing([listed("2001")])]
    assert venue.resolve_pending()[0].status == "ACCEPTED"


def test_one_list_read_per_account_per_resolve():
    hub = FakeHub()
    hub.queue = [TimeoutError(), TimeoutError()]
    venue = adapter(hub)
    venue.submit(venue_order("a"))
    venue.submit(venue_order("b", qty="5"))
    hub.queue = [listing([listed("2001"), listed("2002", qty=5)])]
    acks = venue.resolve_pending()
    assert {a.status for a in acks} == {"ACCEPTED"}
    assert [c[0] for c in hub.calls].count("GET") == 1


def test_match_pending_excludes_owned_ids():
    window = (CLOCK_NOW - timedelta(seconds=5), CLOCK_NOW + timedelta(seconds=120))
    orders = [listed("2001"), listed("2002")]
    assert match_pending(orders, symbol="AAPL", instruction="BUY", quantity=Decimal("10"),
                         window=window, exclude={"2001"}) == ("accepted", "2002")


def test_schwab_time_format():
    assert schwab_time(NOW) == "2026-10-24T15:00:00.000Z"


def test_order_id_cap():
    hub = FakeHub()
    venue = adapter(hub)
    for i in range(ORDER_ID_CAP + 10):
        venue.submit(venue_order(f"c{i}"))
    assert venue.schwab_order_id("c0") is None
    assert venue.schwab_order_id(f"c{ORDER_ID_CAP + 9}") == "1001"
    assert len(venue._orders) == ORDER_ID_CAP


# --- riskguard -------------------------------------------------------------------------------------

def test_refused_opening_makes_no_hub_call(ledger, guard):
    guard.body = REFUSED
    hub = FakeHub()
    venue = adapter(hub, precheck=Precheck(config_for(guard), FakeClock(), ledger))
    ack = venue.submit(venue_order(order_type=OrderType.MARKET, limit=None))
    assert ack.status == "REJECTED" and (ack.message or "").startswith("riskguard precheck refused")
    assert hub.calls == []
    assert len(guard.requests) == 1


def test_allowed_opening_is_sent(ledger, guard):
    hub = FakeHub()
    venue = adapter(hub, precheck=Precheck(config_for(guard), FakeClock(), ledger))
    assert venue.submit(venue_order(order_type=OrderType.MARKET, limit=None)).status == "ACCEPTED"
    assert len(guard.requests) == 1 and len(hub.calls) == 1


def test_close_is_not_gated(ledger, guard):
    seed_position(ledger, Side.BUY, "5")
    guard.body = REFUSED
    hub = FakeHub()
    venue = adapter(hub, position=Decimal("5"), precheck=Precheck(config_for(guard), FakeClock(), ledger))
    ack = venue.submit(venue_order(side=Side.SELL, qty="5", order_type=OrderType.MARKET, limit=None))
    assert ack.status == "ACCEPTED"
    assert guard.requests == []
    assert hub.calls[0][2]["orderLegCollection"][0]["instruction"] == "SELL"


# --- perf ------------------------------------------------------------------------------------------

def test_submit_cost_per_order(capsys):
    hub = FakeHub()
    venue = adapter(hub)
    n = 2000
    start = time.perf_counter()
    for i in range(n):
        venue.submit(venue_order(f"p{i}"))
    per = (time.perf_counter() - start) / n * 1e6
    with capsys.disabled():
        print(f"\nOA-6 perf: {per:.1f} us per submit (build + record + fake hub, cap {ORDER_ID_CAP})")
    assert per < 2000


# --- F14 / OA-6 hardening: replace timeouts, narrower match, durable journal -----------------------

def submitted_then_replace_timeout(hub: FakeHub) -> SchwabOrderAdapter:
    venue = adapter(hub)
    assert venue.submit(venue_order()).status == "ACCEPTED"  # Schwab id 1001
    hub.queue = [TimeoutError()]
    ack = venue.replace("cmd-1", OrderChanges(new_limit_price=Decimal("188")))
    assert (ack.status, ack.message) == ("PENDING", "outcome_unknown")
    return venue


def test_replace_timeout_then_resolve_adopts_the_replacing_order():
    hub = FakeHub()
    venue = submitted_then_replace_timeout(hub)
    assert venue.schwab_order_id("cmd-1") == "1001"
    old = {**listed("1001"), "status": "REPLACED", "replacingOrderCollection": [{"orderId": 1002}]}
    hub.queue = [listing([old])]
    [ack] = venue.resolve_pending()
    assert ack.status == "ACCEPTED"
    assert venue.schwab_order_id("cmd-1") == "1002"
    assert [c[0] for c in hub.calls].count("GET") == 1
    hub.queue = [(200, json.dumps({"status": 200, "order_id": None, "body": ""}))]
    assert venue.cancel("cmd-1").status == "ACCEPTED"
    assert hub.calls[-1][:2] == ("DELETE", f"{HUB}/accounts/{HASH}/orders/1002")


def test_replace_timeout_matches_the_replacing_order_when_no_link():
    hub = FakeHub()
    venue = submitted_then_replace_timeout(hub)
    old = {**listed("1001"), "status": "REPLACED"}
    new = {**listed("1002"), "orderType": "LIMIT", "price": "188"}
    hub.queue = [listing([old, new])]
    [ack] = venue.resolve_pending()
    assert ack.status == "ACCEPTED"
    assert venue.schwab_order_id("cmd-1") == "1002"


def test_replace_timeout_old_still_working_rejects_the_replace():
    hub = FakeHub()
    venue = submitted_then_replace_timeout(hub)
    hub.queue = [listing([{**listed("1001"), "status": "WORKING"}])]
    [ack] = venue.resolve_pending()
    assert (ack.status, ack.message) == ("REJECTED", "replace_not_at_venue")
    assert venue.schwab_order_id("cmd-1") == "1001"
    # the replace is cleared, so the old order is cancellable again
    hub.queue = [(200, json.dumps({"status": 200, "order_id": None, "body": ""}))]
    assert venue.cancel("cmd-1").status == "ACCEPTED"
    assert hub.calls[-1][:2] == ("DELETE", f"{HUB}/accounts/{HASH}/orders/1001")


def test_replace_timeout_unknown_stays_pending(caplog):
    hub = FakeHub()
    venue = submitted_then_replace_timeout(hub)
    hub.queue = [listing([])]  # the old order is not in the list
    [ack] = venue.resolve_pending()
    assert ack.status == "PENDING"
    assert venue.schwab_order_id("cmd-1") == "1001"
    assert "unresolved" in caplog.text
    hub.queue = [listing([])]
    assert venue.resolve_pending()[0].status == "PENDING"  # still unknown, still never guessed
    assert len(hub.calls) == 3


def test_cancel_and_replace_refuse_while_replace_unresolved():
    hub = FakeHub()
    venue = submitted_then_replace_timeout(hub)
    calls = len(hub.calls)
    cancel = venue.cancel("cmd-1")
    assert (cancel.status, cancel.message) == ("REJECTED", "unresolved_replace")
    replace = venue.replace("cmd-1", OrderChanges(new_limit_price=Decimal("189")))
    assert (replace.status, replace.message) == ("REJECTED", "unresolved_replace")
    assert len(hub.calls) == calls


def test_match_pending_uses_price_and_type():
    window = (CLOCK_NOW - timedelta(seconds=5), CLOCK_NOW + timedelta(seconds=120))
    kwargs = dict(symbol="AAPL", instruction="BUY", quantity=Decimal("10"), window=window)
    orders = [{**listed("2001"), "orderType": "LIMIT", "price": "190"},
              {**listed("2002"), "orderType": "LIMIT", "price": "187.25"}]
    assert match_pending(orders, **kwargs) == ("ambiguous", None)  # same size: the old, loose match
    assert match_pending(orders, **kwargs, order_type="LIMIT",
                         price=Decimal("187.25")) == ("accepted", "2002")
    assert match_pending(orders, **kwargs, order_type="STOP", price=Decimal("187.25")) == ("not_at_venue", None)
    assert match_pending([listed("2003")], **kwargs, price=Decimal("187.25")) == ("not_at_venue", None)

    hub = FakeHub()
    venue = pending_adapter(hub)  # sent LIMIT at 187.25
    hub.queue = [listing(orders)]
    [ack] = venue.resolve_pending()
    assert ack.status == "ACCEPTED" and venue.schwab_order_id("cmd-1") == "2002"


def test_restart_with_sqlite_journal_never_resends(tmp_path):
    from trade_engine.schwab.order_adapter import SqliteOrderJournal

    path = tmp_path / "j.db"
    first = FakeHub()
    first.queue = [TimeoutError()]
    before = adapter(first, journal=SqliteOrderJournal(path))
    assert before.submit(venue_order()).status == "PENDING"
    assert SqliteOrderJournal(path).load()["cmd-1"]["status"] == "PENDING"

    second = FakeHub()  # the process restarted: a new adapter, a fresh hub
    after = adapter(second, journal=SqliteOrderJournal(path))
    again = after.submit(venue_order())
    assert again.status == "PENDING"
    assert second.calls == []

    second.queue = [listing([listed("2001")])]
    [ack] = after.resolve_pending()
    assert ack.status == "ACCEPTED" and after.schwab_order_id("cmd-1") == "2001"
    assert SqliteOrderJournal(path).load()["cmd-1"]["schwab_id"] == "2001"

    # a restored record can be cancelled, but not replaced (no VenueOrder to rebuild from)
    second.queue = [(200, json.dumps({"status": 200, "order_id": None, "body": ""}))]
    assert after.cancel("cmd-1").status == "ACCEPTED"
    assert after.replace("cmd-1", OrderChanges(new_limit_price=Decimal("188"))).message == "restored_order"
    assert [c[0] for c in second.calls] == ["GET", "DELETE"]


def test_cap_never_evicts_pending():
    hub = FakeHub()
    hub.queue = [TimeoutError()]
    venue = adapter(hub)
    venue.submit(venue_order("pending-1"))  # the oldest, and unsettled
    for i in range(ORDER_ID_CAP + 10):
        venue.submit(venue_order(f"c{i}"))
    assert len(venue._orders) == ORDER_ID_CAP
    assert "pending-1" in venue._orders
    assert venue.schwab_order_id("c0") is None  # the oldest settled order went instead
    calls = len(hub.calls)
    assert venue.submit(venue_order("pending-1")).status == "PENDING"
    assert len(hub.calls) == calls

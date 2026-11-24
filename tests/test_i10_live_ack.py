"""I10: live trading needs an explicit acknowledgement.

The acknowledgement is the environment variable ``TRADE_ENGINE_LIVE_ACK`` set to the exact phrase.
An adapter that would send to a live account without it refuses ``submit`` and ``replace`` before
any client call and remembers nothing. ``cancel`` stays allowed: it reduces risk.

Fakes: the Schwab hub fake from ``test_oa6_schwab`` and the ntx loopback helpers from
``test_oa3_ntx_orders`` / ``test_bp3_tradovate_ntx``. No network, no daemon.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from trade_engine.domain.instruments import Future, Side
from trade_engine.domain.orders import OrderType, TimeInForce
from trade_engine.interfaces.broker import OrderChanges, VenueOrder, VenueOrderAllocation
from trade_engine.live_ack import LIVE_ACK_ENV, LIVE_ACK_PHRASE, LiveNotAcknowledged, live_acknowledged
from trade_engine.schwab.order_adapter import SchwabOrderAdapter
from trade_engine.schwab.orders import HubOrdersClient
from trade_engine.tradovate.adapter import TradovateAdapter
from test_bp3_tradovate_ntx import acct, snap
from test_oa3_ntx_orders import (
    ENGINE_ACCOUNT,
    NOW,
    NTX_ACCOUNT,
    Binding,
    Clock,
    sent_response,
)
from test_oa3_ntx_orders import venue_order as ntx_order
from test_oa6_schwab import ACCOUNT, HASH, HUB, FakeHub, venue_order
from test_oms import FakeClock


# ---- the switch ---------------------------------------------------------------------------


def test_the_env_name_and_phrase_are_fixed():
    assert LIVE_ACK_ENV == "TRADE_ENGINE_LIVE_ACK"
    assert LIVE_ACK_PHRASE == "I_ACKNOWLEDGE_LIVE_TRADING"


@pytest.mark.parametrize(
    ("environ", "expected"),
    [
        ({}, False),  # missing
        ({LIVE_ACK_ENV: ""}, False),  # empty
        ({LIVE_ACK_ENV: "   "}, False),  # whitespace only
        ({LIVE_ACK_ENV: "yes"}, False),  # wrong value
        ({LIVE_ACK_ENV: "1"}, False),
        ({LIVE_ACK_ENV: "i_acknowledge_live_trading"}, False),  # case matters
        ({LIVE_ACK_ENV: LIVE_ACK_PHRASE + "x"}, False),  # not exact
        ({LIVE_ACK_ENV: "x" + LIVE_ACK_PHRASE}, False),
        ({LIVE_ACK_ENV: "I ACKNOWLEDGE LIVE TRADING"}, False),
        ({"OTHER": LIVE_ACK_PHRASE}, False),  # right phrase, wrong name
        ({LIVE_ACK_ENV: LIVE_ACK_PHRASE}, True),  # exact
        ({LIVE_ACK_ENV: f"  {LIVE_ACK_PHRASE}\n"}, True),  # surrounding whitespace is stripped
        ({LIVE_ACK_ENV: f"\t{LIVE_ACK_PHRASE}"}, True),
    ],
)
def test_live_acknowledged_truth_table(environ, expected):
    assert live_acknowledged(environ) is expected


def test_live_not_acknowledged_is_an_exception():
    assert issubclass(LiveNotAcknowledged, Exception)


# ---- Schwab -------------------------------------------------------------------------------


def schwab(hub: FakeHub, *, env: str = "paper", live_ack: bool = False) -> SchwabOrderAdapter:
    client = HubOrdersClient(HUB, transport=hub)
    return SchwabOrderAdapter(
        client, {ACCOUNT: HASH}, FakeClock(), lambda account, instrument: Decimal(0),
        env=env, live_ack=live_ack,
    )


def test_schwab_live_without_ack_refuses_submit_with_no_client_call():
    hub = FakeHub()
    venue = schwab(hub, env="live", live_ack=False)
    ack = venue.submit(venue_order("cmd-1"))
    assert ack.status == "REJECTED"
    assert ack.message == "live_not_acknowledged"
    assert hub.calls == []


def test_schwab_live_refusal_remembers_nothing():
    hub = FakeHub()
    venue = schwab(hub, env="live", live_ack=False)
    venue.submit(venue_order("cmd-1"))
    assert venue.schwab_order_id("cmd-1") is None
    assert venue.resolve_pending() == []
    assert hub.calls == []


def test_schwab_live_without_ack_refuses_replace_with_no_client_call():
    hub = FakeHub()
    venue = schwab(hub, env="live", live_ack=False)
    ack = venue.replace("cmd-1", OrderChanges(new_limit_price=Decimal("188.00")))
    assert ack.status == "REJECTED"
    assert ack.message == "live_not_acknowledged"
    assert hub.calls == []


def test_schwab_live_with_ack_places():
    hub = FakeHub()
    venue = schwab(hub, env="live", live_ack=True)
    ack = venue.submit(venue_order("cmd-1"))
    assert len(hub.calls) == 1
    assert ack.status == "ACCEPTED"
    assert ack.message != "live_not_acknowledged"


def test_schwab_paper_is_unchanged_without_ack():
    hub = FakeHub()
    venue = schwab(hub, env="paper", live_ack=False)
    ack = venue.submit(venue_order("cmd-1"))
    assert len(hub.calls) == 1
    assert ack.status == "ACCEPTED"


def test_schwab_defaults_are_unchanged():
    hub = FakeHub()
    client = HubOrdersClient(HUB, transport=hub)
    venue = SchwabOrderAdapter(client, {ACCOUNT: HASH}, FakeClock(), lambda a, i: Decimal(0))
    assert venue.submit(venue_order("cmd-1")).status == "ACCEPTED"
    assert len(hub.calls) == 1


def test_schwab_cancel_is_still_sent_while_unacknowledged(monkeypatch):
    hub = FakeHub()
    venue = schwab(hub, env="live", live_ack=True)
    assert venue.submit(venue_order("cmd-1")).status == "ACCEPTED"
    sent_before = len(hub.calls)
    # The ack is off from here on: the order is already at the venue, and cancelling reduces risk.
    monkeypatch.setattr(venue, "_live_ack", False)
    ack = venue.cancel("cmd-1")
    assert len(hub.calls) == sent_before + 1
    assert hub.calls[-1][0] == "DELETE"
    assert ack.message != "live_not_acknowledged"


def test_schwab_env_and_ack_are_keyword_only_and_env_is_validated():
    hub = FakeHub()
    client = HubOrdersClient(HUB, transport=hub)
    with pytest.raises(TypeError):
        SchwabOrderAdapter(client, {ACCOUNT: HASH}, FakeClock(), lambda a, i: Decimal(0), "live")
    with pytest.raises(ValueError):
        schwab(hub, env="production")


# ---- Tradovate ----------------------------------------------------------------------------


class StubNtx:
    """The ntx client surface the adapter calls: one snapshot, then order posts, all recorded."""

    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.calls: list[tuple[str, dict]] = []

    def snapshot(self) -> dict:
        return snap(self.rows)

    def place(self, body: dict) -> dict:
        self.calls.append(("place", body))
        return sent_response("place", body["clOrdId"])

    def modify(self, body: dict) -> dict:
        self.calls.append(("modify", body))
        return sent_response("modify", body["clOrdId"])

    def cancel(self, body: dict) -> dict:
        self.calls.append(("cancel", body))
        return sent_response("cancel", body["clOrdId"])


def tradovate(env: str, *, live_ack: bool) -> tuple[TradovateAdapter, StubNtx]:
    """An adapter whose one engine account reads as ``env`` from a snapshot."""
    stub = StubNtx([acct(NTX_ACCOUNT, "900194", identity="IDENT-TEST", env=env)])
    venue = TradovateAdapter(
        stub, clock=Clock(), account_bindings={ENGINE_ACCOUNT: Binding(NTX_ACCOUNT)}, live_ack=live_ack
    )
    venue.accounts()
    return venue, stub


def child_stop(parent: str, *, qty: str = "2") -> VenueOrder:
    return VenueOrder(
        venue_order_id=f"{parent}.stop",
        instrument=Future(root="MNQ", year=2026, month=12),
        order_type=OrderType.STOP,
        side=Side.SELL,
        quantity=Decimal(qty),
        submitted_at=NOW,
        tif=TimeInForce.DAY,
        stop_price=Decimal("21980"),
        allocations=(VenueOrderAllocation(f"s-{parent}-stop", ENGINE_ACCOUNT, Decimal(qty)),),
        parent_order_id=parent,
    )


def test_tradovate_live_without_ack_refuses_submit_with_no_client_call():
    venue, stub = tradovate("live", live_ack=False)
    ack = venue.submit(ntx_order("cmd-1"))
    assert ack.status == "REJECTED"
    assert ack.message == "live_not_acknowledged"
    assert stub.calls == []


def test_tradovate_live_refusal_remembers_nothing():
    venue, stub = tradovate("live", live_ack=False)
    venue.submit(ntx_order("cmd-1"))
    ack = venue.cancel("cmd-1")
    assert ack.message == "unknown_order"
    assert stub.calls == []


def test_tradovate_live_without_ack_refuses_adopted_child_with_no_client_call():
    venue, stub = tradovate("live", live_ack=True)
    assert venue.submit(ntx_order("cmd-1")).status == "ACCEPTED"
    placed = len(stub.calls)
    venue._live_ack = False  # the ack is off from here on; only the child is refused
    ack = venue.submit(child_stop("cmd-1", qty="1"))
    assert ack.status == "REJECTED"
    assert ack.message == "live_not_acknowledged"
    assert len(stub.calls) == placed


def test_tradovate_live_without_ack_refuses_replace_with_no_client_call():
    venue, stub = tradovate("live", live_ack=True)
    assert venue.submit(ntx_order("cmd-1")).status == "ACCEPTED"
    placed = len(stub.calls)
    venue._live_ack = False
    ack = venue.replace("cmd-1", OrderChanges(new_quantity=Decimal("1")))
    assert ack.status == "REJECTED"
    assert ack.message == "live_not_acknowledged"
    assert len(stub.calls) == placed


def test_tradovate_live_with_ack_places():
    venue, stub = tradovate("live", live_ack=True)
    ack = venue.submit(ntx_order("cmd-1"))
    assert [name for name, _ in stub.calls] == ["place"]
    assert ack.status == "ACCEPTED"


def test_tradovate_paper_is_unchanged_without_ack():
    venue, stub = tradovate("paper", live_ack=False)
    ack = venue.submit(ntx_order("cmd-1"))
    assert [name for name, _ in stub.calls] == ["place"]
    assert ack.status == "ACCEPTED"


def test_tradovate_cancel_is_still_sent_while_unacknowledged():
    venue, stub = tradovate("live", live_ack=True)
    assert venue.submit(ntx_order("cmd-1")).status == "ACCEPTED"
    venue._live_ack = False
    ack = venue.cancel("cmd-1")
    assert [name for name, _ in stub.calls] == ["place", "cancel"]
    assert ack.message != "live_not_acknowledged"

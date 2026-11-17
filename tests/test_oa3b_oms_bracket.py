"""OA-3b: an OMS bracket rides one ntx place call and the protective children adopt its ids.

Loopback fake of ntx and of the riskguard check only. Accounts are fakes.
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from trade_engine.domain.instruments import Equity, Future, Side
from trade_engine.domain.orders import OrderState, OrderType, TimeInForce
from trade_engine.interfaces.broker import OrderChanges
from trade_engine.oms import OrderManager
from trade_engine.oms.precheck import AccountBinding, PrecheckConfig
from trade_engine.tradovate.adapter import TradovateAdapter

from test_oa3_ntx_orders import (  # noqa: F401  (fixtures)
    FakeOrders, NOW, NTX_ACCOUNT, OmsClock, Binding, client_for, Clock, guard, ledger, ntx,
    sent_response, venue_order,
)
from test_oms import make_fill, make_intent
from test_oms_precheck import ALLOWED

ENGINE = "account-1"


def build(ledger, guard, ntx, monkeypatch, *, side=Side.BUY, targets=(Decimal("22010"), Decimal("22020"))):
    # The Rust ledger codec persists only Equity instruments (bridge.rs unknown instrument type), so an
    # OMS bracket cannot carry a Future today. The test keeps the Equity the OMS accepts and maps it to
    # a Tradovate symbol, the only thing the adapter asks of the instrument.
    monkeypatch.setattr(Equity, "to_platform", lambda self, platform: "MNQZ6", raising=False)
    guard.body = {**ALLOWED, "verdictRevision": 9, "checkedAt": "2026-11-17T15:00:01.000Z"}
    adapter = TradovateAdapter(client_for(ntx), clock=Clock(), account_bindings={ENGINE: Binding(NTX_ACCOUNT)})
    config = PrecheckConfig(
        mode="enforce", base_url=guard.base_url, token="check-token",
        accounts={ENGINE: AccountBinding("core-1", {"venue": "tradovate", "env": "demo", "brokerAccountId": "123"})},
    )
    manager = OrderManager(adapter, OmsClock(), ledger, precheck=config)
    intent = replace(
        make_intent(side=side, targets=targets),
        entry_price=Decimal("22000"), exit_tif=TimeInForce.DAY,
        stop_loss=Decimal("21990") if side is Side.BUY else Decimal("22010"),
        profit_targets=targets,
    )
    bracket = manager.create_bracket(intent, Decimal("2"))
    return manager, adapter, bracket


def fill(adapter, entry, qty):
    return replace(make_fill(entry, "f1", qty, "22000"), venue_env=adapter.env)


def children(manager, bracket):
    return manager.get_order(bracket.stop.order_id), [manager.get_order(t.order_id) for t in bracket.targets]


def place_entry(manager, bracket):
    return manager.submit(bracket.entry)


def requests(ntx, op):
    path = {"place": "/v1/orders", "modify": "/v1/orders/modify", "cancel": "/v1/orders/cancel"}[op]
    return [r["body"] for r in ntx.requests if r["path"] == path]


def test_entry_carries_stop_target_and_verdict_then_children_adopt(ledger, guard, ntx, monkeypatch):
    manager, adapter, bracket = build(ledger, guard, ntx, monkeypatch, targets=(Decimal("22010"),))
    entry = place_entry(manager, bracket)
    assert entry.state is OrderState.ACCEPTED
    places = requests(ntx, "place")
    assert len(places) == 1
    body = places[0]
    assert Decimal(body["stop"]) == Decimal("21990")
    assert Decimal(body["target"]) == Decimal("22010")
    assert body["verdict"]["verdictRevision"] == 9

    manager.record_fill(fill(adapter, entry, "2"))
    assert len(requests(ntx, "place")) == 1
    assert requests(ntx, "modify") == []
    stop, targets = children(manager, bracket)
    assert stop.state is OrderState.ACCEPTED
    assert targets[0].state is OrderState.ACCEPTED
    assert adapter._orders[stop.order_id]["stopId"] == 12
    assert adapter._orders[targets[0].order_id]["entryId"] == 13


def test_partial_fill_sends_one_quantity_modify(ledger, guard, ntx, monkeypatch):
    manager, adapter, bracket = build(ledger, guard, ntx, monkeypatch)
    entry = place_entry(manager, bracket)
    manager.record_fill(fill(adapter, entry, "1"))
    mods = requests(ntx, "modify")
    assert len(requests(ntx, "place")) == 1
    assert mods and all(m["qty"] == 1 for m in mods)
    assert {m["orderId"] for m in mods} <= {12, 13}


def test_unknown_parent_is_rejected_without_a_call(ntx):
    adapter = TradovateAdapter(client_for(ntx), clock=Clock(), account_bindings={"engine-1": Binding(NTX_ACCOUNT)})
    child = replace(stop_child("21990"), venue_order_id="cmd-9:stop", parent_order_id="cmd-9:entry")
    ack = adapter.submit(child)
    assert ack.status == "REJECTED" and ack.message == "unknown_parent"
    assert ntx.requests == []


def adopt_pair(ntx):
    adapter = TradovateAdapter(client_for(ntx), clock=Clock(), account_bindings={"engine-1": Binding(NTX_ACCOUNT)})
    entry = venue_order("cmd-1:entry", target="22010", stop="21990")
    assert adapter.submit(entry).status == "ACCEPTED"
    return adapter


def _child(name, order_type, side, qty, **prices):
    base = venue_order(name, order_type=OrderType.MARKET, stop=None, verdict=None, quantity=qty, side=side)
    return replace(base, order_type=order_type, parent_order_id="cmd-1:entry", **prices)


def stop_child(price, side=Side.SELL, qty="2"):
    return _child("cmd-1:stop", OrderType.STOP, side, qty, stop_price=Decimal(price))


def target_child(name, price, qty="2"):
    return _child(name, OrderType.LIMIT, Side.SELL, qty, limit_price=Decimal(price))


def test_tighter_stop_one_modify_looser_rejected(ntx):
    adapter = adopt_pair(ntx)
    ack = adapter.submit(stop_child("21995"))
    assert ack.status == "ACCEPTED"
    mods = requests(ntx, "modify")
    assert len(mods) == 1 and mods[0]["orderId"] == 12 and mods[0]["to"]["type"] == "stop"
    # replace of the adopted child uses the mapped id and may not loosen
    loose = adapter.replace("cmd-1:stop", OrderChanges(new_stop_price=Decimal("21980")))
    assert loose.status == "REJECTED" and loose.message == "stop_loosen"
    assert len(requests(ntx, "modify")) == 1
    tight = adapter.replace("cmd-1:stop", OrderChanges(new_stop_price=Decimal("21998")))
    assert tight.status == "ACCEPTED"
    assert requests(ntx, "modify")[-1]["orderId"] == 12


def test_looser_stop_child_rejected(ntx):
    adapter = adopt_pair(ntx)
    ack = adapter.submit(stop_child("21980"))
    assert ack.status == "REJECTED" and ack.message == "stop_loosen"
    assert requests(ntx, "modify") == []


def test_second_target_rejected_extra_target(ntx):
    adapter = adopt_pair(ntx)
    assert adapter.submit(target_child("cmd-1:target:1", "22010")).status == "ACCEPTED"
    ack = adapter.submit(target_child("cmd-1:target:2", "22020"))
    assert ack.status == "REJECTED" and ack.message == "extra_target"
    assert len(requests(ntx, "place")) == 1 and requests(ntx, "modify") == []


def test_cancel_of_adopted_child_uses_mapped_id(ntx):
    adapter = adopt_pair(ntx)
    adapter.submit(target_child("cmd-1:target:1", "22010"))
    adapter.cancel("cmd-1:target:1")
    assert requests(ntx, "cancel")[0]["orderId"] == 13


def test_other_adapters_unchanged(ledger):
    from test_oms import FakeBroker
    assert getattr(FakeBroker(), "atomic_bracket", False) is False

"""W6-F2: the typed `oms_manager_*` pyfunctions pin their persisted fingerprints.

The three fingerprint values below were computed on the base commit (main 2a5de7d)
BEFORE the JSON door (`_plan` / `oms_manager_decide`) was retired, and must stay
byte-identical: they are persisted command keys. A change to the hash input breaks
replay deduplication for every already-written command.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from trade_engine.domain.instruments import Equity, Side
from trade_engine.domain.orders import Order, OrderType
from trade_engine.domain.signals import OrderIntent
from trade_engine.oms.manager import OrderManager

NOW = datetime(2026, 9, 23, 20, 0, tzinfo=timezone.utc)


def _order(quantity: str) -> Order:
    return Order(
        order_id="o1",
        account_id="account-1",
        instrument=Equity("XYZ"),
        order_type=OrderType.MARKET,
        side=Side.BUY,
        quantity=Decimal(quantity),
        command_id="c1",
        created_at=NOW,
    )


def _intent() -> OrderIntent:
    return OrderIntent(
        intent_id="intent-1",
        account_id="account-1",
        instrument=Equity("AAPL"),
        side=Side.BUY,
        quantity_rule="fixed_10",
        entry_price=Decimal("100"),
        stop_loss=Decimal("95"),
        profit_targets=(Decimal("105"), Decimal("110")),
        reason="breakout",
        command_id="command-1",
    )


def test_fingerprint_order_pins_the_base_commit_value() -> None:
    assert (
        OrderManager._fingerprint_order(_order("10"))
        == "043bc548472319a23f57b12b22630c446a7c49906ab6f8622a00b6ed0c93a22e"
    )


def test_bracket_fingerprint_pins_the_base_commit_value() -> None:
    assert (
        OrderManager._bracket_fingerprint(_intent(), Decimal("10"))
        == "c0411bd8786134410eb965fa7a8bcb52b497902fbf6f99839b01cef8bda93c7a"
    )


def test_reduce_fingerprint_pins_the_base_commit_value() -> None:
    assert (
        OrderManager._reduce_fingerprint("entry-1", Decimal("0.5"), "r")
        == "bbe8681def362bcf0af9414108a79956e8dd029d01c65598627583cb4a34d29d"
    )

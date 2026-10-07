"""Net strategy orders into venue tickets for one paperMoney account (§4.4, §4.7).

One paperMoney account holds several virtual accounts. The venue holds one net number
per contract, so it cannot be long for one virtual account and short for another. The
rules, in order, for each strategy order of a batch (first-in = list order):

1. The order's virtual account must be one this venue account mirrors — a CSP order can
   never reach the IRA (§4.7).
2. Single option contracts are mirrored, and 2-leg verticals (same underlying, expiry,
   right and multiplier, two strikes, one leg bought and one sold, equal ratios), and
   shares of an equity (a covered call's 100 shares, the shares an assignment leaves).
   An equity is never a leg of a combo; any other combo is refused (UnsupportedCapability).
3. MARKET and LIMIT only (a vertical: LIMIT only, one net price), DAY or GTC only,
   whole-contract (shares: whole-share) quantities only; anything else is refused,
   never approximated (I5).
4. Conflicts are across virtual accounts on the same contract, leg by leg for a
   vertical: once a first-in order sets the contract's side for the batch, a later
   order on the opposite side is refused. An order that would leave one virtual account
   long while another is short the same contract (against the mirror book's current
   holdings) is refused too.

Refusals happen **at the venue only** — the sim book of record still takes every order.
Surviving single-contract orders are all on one side per contract, so netting is a plain
sum: one ticket per (contract, order type, TIF, limit). Differing limits are separate
tickets, never averaged. A vertical is mirrored 1:1 — its own combo ticket carrying its
net limit (a SELL collects a credit, a BUY pays a debit), never netted with anything.
Every input order ends in exactly one place: a ticket allocation or a refusal with its
reason (I11).
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from trade_engine.domain.instruments import Combo, Instrument, Side
from trade_engine.domain.orders import Order, OrderType, TimeInForce
from trade_engine.interfaces.broker import VenueOrder
from trade_engine.tos_paper import _rs

MIRRORED_ORDER_TYPES = frozenset({OrderType.MARKET, OrderType.LIMIT})
MIRRORED_TIFS = frozenset({TimeInForce.DAY, TimeInForce.GTC})


class NettingError(RuntimeError):
    """A batch the netting layer cannot even start on (caller error)."""


@dataclass(frozen=True)
class NettedBatch:
    """What one batch of strategy orders becomes at the venue."""

    venue_orders: tuple[VenueOrder, ...]
    refused: tuple[tuple[str, str], ...]  # (strategy order_id, reason)


def ticket_key(
    venue_account: str,
    instrument: Instrument,
    side: Side,
    quantity: Decimal,
    order_type: OrderType,
    limit_price: Decimal | None,
    tif: TimeInForce,
    order_ids: Sequence[str],
) -> str:
    """Stable idempotency key of one ticket's full contents (I3).

    The same allocation always hashes to the same key, and any change — account,
    contract, side, size, price, TIF or the constituent strategy orders — to a new one.
    """
    return _rs.decide("ticket_key", {
        "venue_account": venue_account, "instrument": _rs.wire(instrument), "side": side.value,
        "quantity": str(quantity), "order_type": order_type.value, "limit_price": _rs.text(limit_price),
        "tif": tif.value, "order_ids": list(order_ids)})["key"]


def vertical_reason(combo: Combo) -> str | None:
    """Why ``combo`` is not a mirrorable 2-leg 1:1 vertical, or None when it is."""
    return _rs.decide("vertical_reason", {"instrument": _rs.wire(combo)})["reason"]


def net_strategy_orders(
    orders: Sequence[Order],
    *,
    venue_account: str,
    mirrored_accounts: Collection[str],
    at: datetime,
    holdings: Mapping[tuple[str, Instrument], Decimal] | None = None,
) -> NettedBatch:
    """Net one batch into venue tickets; refuse what the venue cannot hold.

    ``holdings`` is the mirror book: the signed contracts each mirrored virtual account
    currently holds on this venue, keyed ``(account_id, instrument)``. ``at`` stamps the
    tickets (injected clock, I7).
    """
    out = _rs.decide("net_strategy_orders", {
        "orders": [_rs.order_doc(o) for o in orders], "venue_account": venue_account,
        "mirrored": list(mirrored_accounts), "at": at.isoformat(), "holdings": _rs.holdings_doc(holdings)})
    return NettedBatch(venue_orders=tuple(_rs.venue_order(d) for d in out["venue_orders"]),
                       refused=tuple((o, r) for o, r in out["refused"]))


def _account_for_everything(
    orders: Sequence[Order],
    venue_orders: list[VenueOrder],
    refused: list[tuple[str, str]],
) -> None:
    """Every input order ends in exactly one outcome: allocated or refused (I11)."""
    _rs.decide("account_for_everything", {
        "orders": [o.order_id for o in orders],
        "allocated": [a.strategy_order_id for vo in venue_orders for a in vo.allocations],
        "refused": [oid for oid, _ in refused]})

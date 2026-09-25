"""Net strategy orders into venue tickets for one paperMoney account (§4.4, §4.7).

One paperMoney account holds several virtual accounts. The venue holds one net number
per contract, so it cannot be long for one virtual account and short for another. The
rules, in order, for each strategy order of a batch (first-in = list order):

1. The order's virtual account must be one this venue account mirrors — a CSP order can
   never reach the IRA (§4.7).
2. Single option contracts are mirrored, and 2-leg verticals (same underlying, expiry,
   right and multiplier, two strikes, one leg bought and one sold, equal ratios).
   Equities are not (§4.7); any other combo is refused (UnsupportedCapability).
3. MARKET and LIMIT only (a vertical: LIMIT only, one net price), DAY or GTC only,
   whole-contract quantities only; anything else is refused, never approximated (I5).
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

import hashlib
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from trade_engine.domain.instruments import Combo, Equity, Instrument, OptionContract, Side
from trade_engine.domain.orders import Order, OrderType, TimeInForce
from trade_engine.interfaces.broker import VenueOrder, VenueOrderAllocation

MIRRORED_ORDER_TYPES = frozenset({OrderType.MARKET, OrderType.LIMIT})
MIRRORED_TIFS = frozenset({TimeInForce.DAY, TimeInForce.GTC})
ZERO = Decimal("0")


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
    parts = (
        venue_account,
        instrument.symbol,
        side.value,
        str(quantity.normalize()),
        order_type.value,
        "" if limit_price is None else str(limit_price.normalize()),
        tif.value,
        *sorted(order_ids),
    )
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()


def _signed(side: Side, quantity: Decimal) -> Decimal:
    return quantity if side is Side.BUY else -quantity


def vertical_reason(combo: Combo) -> str | None:
    """Why ``combo`` is not a mirrorable 2-leg 1:1 vertical, or None when it is."""
    legs = combo.legs
    if len(legs) != 2 or not all(isinstance(leg.contract, OptionContract) for leg in legs):
        return f"{combo.symbol} is not a 2-leg option combo; only verticals are mirrored"
    first, second = legs[0].contract, legs[1].contract
    if first.underlying != second.underlying:
        return "legs on two underlyings are not a vertical"
    if first.expiry != second.expiry:
        return "legs on two expiries (a calendar or diagonal) are not a vertical"
    if first.right != second.right:
        return "a call leg and a put leg are not a vertical"
    if first.multiplier != second.multiplier:
        return "legs with two multipliers are not a vertical (I6)"
    if first.strike == second.strike:
        return "two legs on one strike are not a vertical"
    if legs[0].side is legs[1].side:
        return "both legs on one side are not a vertical"
    if legs[0].ratio != legs[1].ratio:
        return f"a {legs[0].ratio}:{legs[1].ratio} ratio spread is not a 1:1 vertical"
    return None


def _legs(order: Order) -> tuple[tuple[OptionContract, Side, Decimal], ...]:
    """(contract, side, contracts) of every contract an order trades: legs as written."""
    if isinstance(order.instrument, Combo):
        return tuple((leg.contract, leg.side, order.quantity * leg.ratio) for leg in order.instrument.legs)
    return ((order.instrument, order.side, order.quantity),)


def _screen(order: Order, mirrored: frozenset[str]) -> str | None:
    """Reason this single order cannot go to the venue at all, or None."""
    if order.account_id not in mirrored:
        return (
            f"account {order.account_id} is not mirrored on this venue "
            f"(mirrors {sorted(mirrored)}); refused at the venue only (§4.7)"
        )
    if isinstance(order.instrument, Equity):
        return f"{order.instrument.symbol}: equities are not mirrored (§4.7)"
    if isinstance(order.instrument, Combo):
        reason = vertical_reason(order.instrument)
        if reason is not None:
            return f"UnsupportedCapability: multi-leg combo: {reason}"
        if order.order_type is not OrderType.LIMIT:
            return "UnsupportedCapability: a vertical is mirrored with one net LIMIT price only"
    elif not isinstance(order.instrument, OptionContract):
        return f"UnsupportedCapability: instrument {order.instrument!r} is not a mirrored option"
    if order.order_type not in MIRRORED_ORDER_TYPES:
        return f"UnsupportedCapability: order type {order.order_type.value} (MARKET/LIMIT only)"
    if order.tif not in MIRRORED_TIFS:
        return f"UnsupportedCapability: TIF {order.tif.value} (DAY/GTC only)"
    if order.quantity != order.quantity.to_integral_value():
        return f"quantity {order.quantity} is not a whole number of contracts; refusing to round (I5)"
    # A LIMIT's price is positive by Order's own validation; tickets pass it through
    # unchanged (never averaged), so no ticket can carry a price <= 0.
    return None


def _mixed_signs(book: Mapping[str, Decimal]) -> bool:
    signs = {q > 0 for q in book.values() if q != 0}
    return len(signs) > 1


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
    if not orders:
        raise NettingError("no strategy orders to net")
    mirrored = frozenset(mirrored_accounts)
    holdings = dict(holdings or {})
    refused: list[tuple[str, str]] = []
    seen_ids: set[str] = set()
    accepted: dict[Instrument, list[Order]] = {}
    batch_side: dict[Instrument, tuple[Side, str]] = {}
    books: dict[Instrument, dict[str, Decimal]] = {}
    verticals: list[Order] = []  # one combo ticket each, never netted

    for order in orders:  # list order = first-in
        if order.order_id in seen_ids:
            refused.append((order.order_id, f"duplicate strategy order {order.order_id} in one batch (I3)"))
            continue
        seen_ids.add(order.order_id)
        reason = _screen(order, mirrored)
        if reason is not None:
            refused.append((order.order_id, reason))
            continue
        conflict: str | None = None
        trials: dict[Instrument, dict[str, Decimal]] = {}
        for contract, side, contracts in _legs(order):  # a vertical is screened leg by leg
            first = batch_side.get(contract)
            if first is not None and first[0] is not side:
                conflict = (
                    f"conflict: {side.value} {contract.symbol} opposes first-in "
                    f"{first[0].value} {first[1]} on the same contract; refused at the venue only"
                )
                break
            book = books.get(contract)
            if book is None:
                book = {acct: qty for (acct, inst), qty in holdings.items() if inst == contract}
            trial = dict(book)
            trial[order.account_id] = trial.get(order.account_id, ZERO) + _signed(side, contracts)
            if _mixed_signs(trial):
                others = sorted(
                    acct for acct, qty in trial.items()
                    if acct != order.account_id and qty != 0
                )
                conflict = (
                    f"conflict: {side.value} {contract.symbol} for {order.account_id} would "
                    f"hold the opposite side of {', '.join(others)} in one venue account; "
                    "refused at the venue only"
                )
                break
            trials[contract] = trial
        if conflict is not None:
            refused.append((order.order_id, conflict))
            continue
        for contract, side, _contracts in _legs(order):
            books[contract] = trials[contract]
            batch_side.setdefault(contract, (side, order.order_id))
        if isinstance(order.instrument, Combo):
            verticals.append(order)
        else:
            accepted.setdefault(order.instrument, []).append(order)

    venue_orders: list[VenueOrder] = []
    for instrument, legs in accepted.items():
        groups: dict[tuple[OrderType, TimeInForce, Decimal | None], list[Order]] = {}
        for leg in legs:
            limit = leg.limit_price if leg.order_type is OrderType.LIMIT else None
            groups.setdefault((leg.order_type, leg.tif, limit), []).append(leg)
        for (order_type, tif, limit), group in groups.items():
            try:
                venue_orders.append(_ticket(venue_account, instrument, order_type, tif, limit, group, at))
            except (ValueError, ArithmeticError) as exc:
                for leg in group:
                    refused.append((leg.order_id, f"ticket for {instrument.symbol} refused: {exc}"))
    for order in verticals:
        venue_orders.append(
            _ticket(venue_account, order.instrument, order.order_type, order.tif, order.limit_price, [order], at)
        )

    _account_for_everything(orders, venue_orders, refused)
    return NettedBatch(venue_orders=tuple(venue_orders), refused=tuple(refused))


def _ticket(
    venue_account: str,
    instrument: Instrument,
    order_type: OrderType,
    tif: TimeInForce,
    limit: Decimal | None,
    group: list[Order],
    at: datetime,
) -> VenueOrder:
    """One same-side ticket: the sum of its orders, each allocated its own quantity."""
    side = group[0].side  # one side per contract after conflict screening
    quantity = sum((leg.quantity for leg in group), ZERO)
    key = ticket_key(
        venue_account, instrument, side, quantity, order_type, limit, tif,
        [leg.order_id for leg in group],
    )
    return VenueOrder(
        venue_order_id=f"tos:{key[:32]}",
        instrument=instrument,
        order_type=order_type,
        side=side,
        quantity=quantity,
        submitted_at=at,
        tif=tif,
        limit_price=limit,
        allocations=tuple(
            VenueOrderAllocation(leg.order_id, leg.account_id, leg.quantity) for leg in group
        ),
    )


def _account_for_everything(
    orders: Sequence[Order],
    venue_orders: list[VenueOrder],
    refused: list[tuple[str, str]],
) -> None:
    """Every input order ends in exactly one outcome: allocated or refused (I11)."""
    outcomes = [a.strategy_order_id for vo in venue_orders for a in vo.allocations]
    outcomes += [oid for oid, _ in refused]
    expected = [o.order_id for o in orders]
    if sorted(outcomes) != sorted(expected):
        raise NettingError(
            "netting lost or duplicated an order: "
            f"inputs {sorted(expected)} vs outcomes {sorted(outcomes)} (I11)"
        )

"""Deterministic options paper venue driven by chain snapshots (O4, rules doc §3).

There are no historical intraday option quotes, so option orders are matched against one
chain snapshot per session (the 15:45 ET pull). The EOD runner replays that snapshot at
its own ``as_of`` inside the session's timeline, the same way one-minute bars replay
equity fills: re-runnable, and never using a price from after the clock (I7).

Fill model (owner's choice, 2026-09-24): a leg trades halfway between mid and natural,
so a sale gets ``mid − fill_fraction × spread`` and a purchase ``mid + fill_fraction ×
spread``, with ``fill_fraction`` 0.25 by default. That is always inside the quote.

- A MARKET order fills at those prices. If a leg has no usable price in the snapshot, the
  order is rejected rather than filled at a guess (I5).
- A LIMIT order fills at its limit once the model price is at or through it: the model
  credit at or above a sell limit, the model debit at or below a buy limit. It is taken
  as having rested all session, as a real resting limit fills at its price when the
  market crosses it. An order the snapshot cannot price stays working.
- A combo trades its legs as written. SELL means it collects a net credit and BUY that it
  pays a net debit; the limit is that net amount per unit, in option points. At a limit
  fill the legs on the favourable side are scaled so the legs add up to the limit
  exactly.
- A quote older than ``max_quote_age_seconds`` at the snapshot's instant prices nothing.
- A share order on the snapshot's underlying fills at ``underlying_price``, moved against
  the order by ``equity_slippage_bps``.
- Orders fill all or nothing, so a combo never leaves a leg behind.
- Fees are ``fee_per_contract`` per option contract. Shares trade free (rules doc §5.1).
- A DAY order lapses at its session's close, but only once a snapshot of its underlying
  from that session has been processed. Only a snapshot proves it did not fill.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Literal
from zoneinfo import ZoneInfo

from trade_engine.calendar.sessions import ExchangeCalendar
from trade_engine.domain.instruments import (
    Combo,
    ComboLeg,
    Equity,
    Instrument,
    OptionContract,
    Side,
)
from trade_engine.domain.option_roots import option_style
from trade_engine.domain.orders import OrderState, OrderType, TimeInForce
from trade_engine.interfaces.broker import (
    BrokerAdapter,
    Capabilities,
    OrderChanges,
    VenueAck,
    VenueCashEvent,
    VenueFill,
    VenueIdentity,
    VenueOrder,
    VenueOrderState,
    VenuePosition,
)
from trade_engine.interfaces.clock import Clock
from trade_engine.market_data.chains import ChainSnapshot

ZERO = Decimal("0")
BPS = Decimal("10000")
TICK = Decimal("0.0001")
NEW_YORK = ZoneInfo("America/New_York")
_WORKING = (OrderState.ACCEPTED, OrderState.PARTIALLY_FILLED)


class SnapshotVenueError(RuntimeError):
    """An invalid operation on the snapshot venue."""


def underlying_of(instrument: Instrument) -> str:
    """The underlying whose chain snapshot prices ``instrument``."""
    if isinstance(instrument, Equity):
        return instrument.symbol
    if isinstance(instrument, OptionContract):
        return option_style(instrument.underlying).underlying
    if isinstance(instrument, Combo):
        found = {underlying_of(leg.contract) for leg in instrument.legs}
        if len(found) != 1:
            raise ValueError(f"Combo {instrument.symbol} spans underlyings {sorted(found)}")
        return found.pop()
    raise ValueError(f"Cannot price {type(instrument).__name__} from a chain snapshot")


@dataclass
class _Working:
    order: VenueOrder
    state: OrderState
    filled_quantity: Decimal
    updated_at: datetime


class SnapshotVenue(BrokerAdapter):
    """Single-account simulator for options and shares, filled only from chain snapshots."""

    name = "SnapshotVenue"
    env = "sim"
    capabilities = Capabilities(
        supported_order_types=frozenset({OrderType.MARKET, OrderType.LIMIT}),
        supported_tifs=frozenset({TimeInForce.DAY, TimeInForce.GTC}),
        supports_multi_leg=True,
        supports_native_stops=False,
        supports_streaming=False,
    )

    def __init__(
        self,
        account_id: str,
        clock: Clock,
        *,
        fill_fraction: Decimal = Decimal("0.25"),
        fee_per_contract: Decimal = Decimal("0.65"),
        equity_slippage_bps: Decimal = Decimal("5"),
        max_quote_age_seconds: float = 900.0,
        calendar: ExchangeCalendar | None = None,
    ) -> None:
        if not account_id:
            raise ValueError("account_id must be non-empty")
        for name, value in (
            ("fill_fraction", fill_fraction),
            ("fee_per_contract", fee_per_contract),
            ("equity_slippage_bps", equity_slippage_bps),
        ):
            if not isinstance(value, Decimal) or not value.is_finite() or value < ZERO:
                raise ValueError(f"{name} must be a finite, non-negative Decimal")
        if (
            not isinstance(max_quote_age_seconds, (int, float))
            or isinstance(max_quote_age_seconds, bool)
            or not max_quote_age_seconds > 0
        ):
            raise ValueError("max_quote_age_seconds must be positive")
        if fill_fraction > Decimal("0.5"):
            raise ValueError("fill_fraction above 0.5 would trade outside the quote")
        self.account_id = account_id
        self._clock = clock
        self._fraction = fill_fraction
        self._fee = fee_per_contract
        self._slippage = equity_slippage_bps
        self._max_quote_age = max_quote_age_seconds
        self._calendar = calendar or ExchangeCalendar()
        self._connected = False
        self._orders: dict[str, _Working] = {}
        self._fills: list[VenueFill] = []
        self._fill_counts: dict[str, int] = {}
        self._positions: dict[Instrument, Decimal] = {}
        # The newest snapshot processed per underlying: proof a DAY order had its chance.
        self._seen: dict[str, datetime] = {}

    # -- the BrokerAdapter contract -------------------------------------------------

    def connect(self) -> VenueIdentity:
        self._connected = True
        return VenueIdentity(
            account_id=self.account_id, env=self.env, connected_at=self._now(), broker_name=self.name
        )

    def restore(
        self,
        orders: Iterable[tuple[VenueOrder, OrderState]],
        fills: Iterable[VenueFill],
        positions: Iterable[VenuePosition],
    ) -> None:
        """Load what the ledger says this venue holds into an empty venue (I2).

        Each run is a new process and this venue keeps its book in memory, so the host
        folds the ledger and hands back the working orders, their fills and the positions.
        Anything inconsistent refuses rather than being patched up (I5).
        """
        if self._orders or self._fills or self._positions:
            raise SnapshotVenueError("restore() requires an empty SnapshotVenue")
        for order, state in orders:
            if state not in (*_WORKING, OrderState.FILLED, OrderState.CANCELLED, OrderState.EXPIRED, OrderState.REJECTED):
                raise SnapshotVenueError(f"Cannot restore '{order.venue_order_id}' in state {state.value}")
            if order.venue_order_id in self._orders:
                raise SnapshotVenueError(f"Order '{order.venue_order_id}' restored twice")
            self._validate(order)
            self._orders[order.venue_order_id] = _Working(order, state, ZERO, order.submitted_at)
        per_leg: dict[tuple[str, str | None], Decimal] = {}
        for fill in sorted(fills, key=lambda item: (item.filled_at, item.venue_fill_id)):
            working = self._orders.get(fill.venue_order_id)
            if working is None:
                raise SnapshotVenueError(
                    f"Fill '{fill.venue_fill_id}' references unrestored order '{fill.venue_order_id}'"
                )
            prefix, separator, number = fill.venue_fill_id.rpartition(":fill:")
            if prefix != fill.venue_order_id or not separator or not number.isdecimal():
                raise SnapshotVenueError(f"Fill id '{fill.venue_fill_id}' is not a SnapshotVenue fill id")
            leg = self._leg_of(working.order, fill)
            if fill.instrument != leg.contract or fill.side is not leg.side:
                raise SnapshotVenueError(
                    f"Fill '{fill.venue_fill_id}' does not match order '{fill.venue_order_id}'"
                )
            self._fill_counts[fill.venue_order_id] = max(
                self._fill_counts.get(fill.venue_order_id, 0), int(number)
            )
            key = (fill.venue_order_id, fill.leg_id)
            per_leg[key] = per_leg.get(key, ZERO) + fill.quantity
            working.updated_at = max(working.updated_at, fill.filled_at)
            self._fills.append(fill)
        for venue_order_id, working in self._orders.items():
            combo = self._is_combo(working.order)
            working.filled_quantity = min(
                per_leg.get((venue_order_id, str(index) if combo else None), ZERO) / leg.ratio
                for index, leg in enumerate(self._legs(working.order))
            )
            quantity = working.order.quantity
            consistent = {
                OrderState.ACCEPTED: working.filled_quantity == ZERO,
                OrderState.FILLED: working.filled_quantity == quantity,
            }.get(working.state, working.filled_quantity <= quantity)
            if not consistent:
                raise SnapshotVenueError(
                    f"Order '{venue_order_id}' is {working.state.value} with "
                    f"{working.filled_quantity} of {quantity} filled"
                )
        for position in positions:
            if position.instrument in self._positions:
                raise SnapshotVenueError(f"Position {position.instrument.symbol} restored twice")
            if position.quantity != ZERO:
                self._positions[position.instrument] = position.quantity

    def submit(self, order: VenueOrder) -> VenueAck:
        self._require_connected()
        self._expire_due()
        self._validate(order)
        existing = self._orders.get(order.venue_order_id)
        if existing is not None:
            if existing.order != order:
                raise SnapshotVenueError(
                    f"venue_order_id '{order.venue_order_id}' was reused with different terms"
                )
            if existing.state in (OrderState.CANCELLED, OrderState.REJECTED):
                return self._ack(order.venue_order_id, "REJECTED", f"Order is {existing.state.value}")
            return self._ack(order.venue_order_id, "ACCEPTED")
        self._orders[order.venue_order_id] = _Working(order, OrderState.ACCEPTED, ZERO, self._now())
        return self._ack(order.venue_order_id, "ACCEPTED")

    def cancel(self, venue_order_id: str) -> VenueAck:
        self._expire_due()
        working = self._require(venue_order_id)
        if working.state is OrderState.CANCELLED:
            return self._ack(venue_order_id, "ACCEPTED")
        if working.state not in _WORKING:
            return self._ack(venue_order_id, "REJECTED", f"Order is {working.state.value}")
        working.state = OrderState.CANCELLED
        working.updated_at = self._now()
        return self._ack(venue_order_id, "ACCEPTED")

    def replace(self, venue_order_id: str, changes: OrderChanges) -> VenueAck:
        # Nothing here replaces an option order; a changed price is a cancel and a new order.
        self._require(venue_order_id)
        return self._ack(venue_order_id, "REJECTED", "SnapshotVenue does not replace orders")

    def orders(self, since: datetime) -> list[VenueOrderState]:
        self._expire_due()
        return [
            VenueOrderState(
                venue_order_id=venue_order_id,
                state=working.state,
                filled_quantity=working.filled_quantity,
                remaining_quantity=working.order.quantity - working.filled_quantity,
                updated_at=working.updated_at,
            )
            for venue_order_id, working in sorted(self._orders.items())
            if working.updated_at >= since
        ]

    def fills(self, since: datetime) -> list[VenueFill]:
        self._expire_due()
        return [fill for fill in self._fills if fill.filled_at >= since]

    def positions(self) -> list[VenuePosition]:
        now = self._now()
        return [
            VenuePosition(instrument=instrument, quantity=quantity, avg_price=ZERO, as_of=now)
            for instrument, quantity in sorted(self._positions.items(), key=lambda item: item[0].symbol)
            if quantity != ZERO
        ]

    def cash_events(self, since: datetime) -> list[VenueCashEvent]:
        return []

    # -- matching ---------------------------------------------------------------------

    def process_snapshot(self, snapshot: ChainSnapshot) -> tuple[VenueFill, ...]:
        """Match every working order on the snapshot's underlying against it."""
        self._require_connected()
        now = self._now()
        if snapshot.as_of > now:
            raise SnapshotVenueError(
                f"{snapshot.underlying} snapshot of {snapshot.as_of.isoformat()} is after the "
                f"clock {now.isoformat()}: look-ahead (I7)"
            )
        self._expire_due()
        fills: list[VenueFill] = []
        for venue_order_id, working in sorted(
            self._orders.items(), key=lambda item: (item[1].order.submitted_at, item[0])
        ):
            order = working.order
            if (
                working.state not in _WORKING
                or order.submitted_at > snapshot.as_of
                or underlying_of(order.instrument) != snapshot.underlying
            ):
                continue
            prices = self._model_prices(order, snapshot)
            if prices is None:
                if order.order_type is OrderType.MARKET:
                    working.state = OrderState.REJECTED
                    working.updated_at = snapshot.as_of
                continue
            filled_at = self._fill_prices(order, prices)
            if filled_at is None:
                continue
            fills.extend(self._fill(venue_order_id, working, filled_at, snapshot.as_of))
        previous = self._seen.get(snapshot.underlying)
        self._seen[snapshot.underlying] = (
            snapshot.as_of if previous is None else max(previous, snapshot.as_of)
        )
        return tuple(fills)

    def model_price(self, instrument: Instrument, side: Side, snapshot: ChainSnapshot) -> Decimal | None:
        """What ``side`` of ``instrument`` trades at in ``snapshot``; None if it can't be priced."""
        if isinstance(instrument, Equity):
            if instrument.symbol != snapshot.underlying:
                return None
            price = snapshot.underlying_price
            moved = price * self._slippage / BPS
            return price + moved if side is Side.BUY else price - moved
        if not isinstance(instrument, OptionContract):
            return None
        quote = snapshot.get(instrument)
        if quote is None or (snapshot.as_of - quote.as_of).total_seconds() > self._max_quote_age:
            # Not quoted, or a quote nobody has updated for a while: not today's market.
            return None
        shade = self._fraction * quote.spread
        price = quote.mid + shade if side is Side.BUY else quote.mid - shade
        return price if price > ZERO else None

    def _model_prices(self, order: VenueOrder, snapshot: ChainSnapshot) -> list[Decimal] | None:
        prices = []
        for leg in self._legs(order):
            price = self.model_price(leg.contract, leg.side, snapshot)
            if price is None:
                return None
            prices.append(price)
        return prices

    def _fill_prices(self, order: VenueOrder, prices: list[Decimal]) -> list[Decimal] | None:
        """The price each leg fills at, or None if the order does not fill."""
        if order.order_type is OrderType.MARKET:
            return [price.quantize(TICK, rounding=ROUND_HALF_EVEN) for price in prices]
        limit = order.limit_price
        if limit is None:
            raise SnapshotVenueError(f"LIMIT order '{order.venue_order_id}' has no limit price")
        if not self._is_combo(order):
            [price] = prices
            through = price >= limit if order.side is Side.SELL else price <= limit
            return [limit] if through else None
        legs = self._legs(order)
        scale = self._multiplier(order)
        # The side whose legs are shaded to land on the limit: the legs sold for a credit,
        # the legs bought for a debit. The other side keeps its model prices.
        shaded = order.side

        def net(leg_prices: list[Decimal]) -> Decimal:
            """The order's net per unit in option points: credit for SELL, debit for BUY."""
            total = sum(
                (
                    (1 if leg.side is shaded else -1) * leg.ratio * price * leg.contract.multiplier
                    for leg, price in zip(legs, leg_prices)
                ),
                ZERO,
            )
            return total / scale

        model = net(prices)
        if (model < limit) if order.side is Side.SELL else (model > limit):
            return None
        kept = sum(
            (leg.ratio * price * leg.contract.multiplier for leg, price in zip(legs, prices) if leg.side is not shaded),
            ZERO,
        ) / scale
        factor = (limit + kept) / (model + kept)
        filled = [
            (price * factor if leg.side is shaded else price).quantize(TICK, rounding=ROUND_HALF_EVEN)
            for leg, price in zip(legs, prices)
        ]
        residue = limit - net(filled)
        if residue:
            # Rounding residue goes on the largest shaded leg, so the net is the limit.
            index = max((i for i, leg in enumerate(legs) if leg.side is shaded), key=lambda i: (filled[i], -i))
            leg = legs[index]
            filled[index] += residue * scale / (leg.ratio * leg.contract.multiplier)
        if any(price <= ZERO for price in filled):
            return None
        return filled

    def _fill(
        self, venue_order_id: str, working: _Working, prices: list[Decimal], at: datetime
    ) -> list[VenueFill]:
        order = working.order
        units = order.quantity - working.filled_quantity
        combo = self._is_combo(order)
        fills = []
        for index, (leg, price) in enumerate(zip(self._legs(order), prices)):
            quantity = units * leg.ratio
            number = self._fill_counts.get(venue_order_id, 0) + 1
            self._fill_counts[venue_order_id] = number
            fee = self._fee * quantity if isinstance(leg.contract, OptionContract) else ZERO
            fill = VenueFill(
                venue_fill_id=f"{venue_order_id}:fill:{number}",
                venue_order_id=venue_order_id,
                instrument=leg.contract,
                quantity=quantity,
                price=price,
                filled_at=at,
                side=leg.side,
                fee=fee,
                leg_id=str(index) if combo else None,
            )
            self._fills.append(fill)
            change = quantity if leg.side is Side.BUY else -quantity
            self._positions[leg.contract] = self._positions.get(leg.contract, ZERO) + change
            fills.append(fill)
        working.filled_quantity = order.quantity
        working.state = OrderState.FILLED
        working.updated_at = at
        return fills

    # -- helpers ----------------------------------------------------------------------

    @staticmethod
    def _is_combo(order: VenueOrder) -> bool:
        return isinstance(order.instrument, Combo)

    @staticmethod
    def _legs(order: VenueOrder) -> tuple[ComboLeg, ...]:
        if isinstance(order.instrument, Combo):
            return order.instrument.legs
        return (ComboLeg(order.instrument, 1, order.side),)

    @staticmethod
    def _multiplier(order: VenueOrder) -> int:
        """Option points: a combo's net is quoted per unit of its option multiplier."""
        found = {
            leg.contract.multiplier
            for leg in order.instrument.legs
            if isinstance(leg.contract, OptionContract)
        }
        if len(found) != 1:
            raise SnapshotVenueError(
                f"Combo '{order.venue_order_id}' has option multipliers {sorted(found)} (I6)"
            )
        return found.pop()

    def _leg_of(self, order: VenueOrder, fill: VenueFill) -> ComboLeg:
        legs = self._legs(order)
        if not self._is_combo(order):
            if fill.leg_id is not None:
                raise SnapshotVenueError(f"Fill '{fill.venue_fill_id}' names a leg of a single order")
            return legs[0]
        if fill.leg_id is None or not fill.leg_id.isdigit() or int(fill.leg_id) >= len(legs):
            raise SnapshotVenueError(f"Fill '{fill.venue_fill_id}' names leg {fill.leg_id!r}")
        return legs[int(fill.leg_id)]

    def _validate(self, order: VenueOrder) -> None:
        if order.order_type not in self.capabilities.supported_order_types:
            raise ValueError(f"SnapshotVenue does not support {order.order_type.value} orders")
        if order.tif not in self.capabilities.supported_tifs:
            raise ValueError(f"SnapshotVenue does not support time in force {order.tif.value}")
        if len(order.allocations) != 1 or order.allocations[0].account_id != self.account_id:
            raise ValueError("SnapshotVenue requires one allocation to its own account")
        instrument = order.instrument
        if isinstance(instrument, Combo):
            if any(not isinstance(leg.contract, OptionContract) for leg in instrument.legs):
                raise ValueError("SnapshotVenue combos are options only; trade the shares on their own")
            self._multiplier(order)
        elif not isinstance(instrument, (Equity, OptionContract)):
            raise ValueError(f"SnapshotVenue cannot trade {type(instrument).__name__}")
        underlying_of(instrument)

    def _expire_due(self) -> None:
        now = self._now()
        for working in self._orders.values():
            order = working.order
            if working.state not in _WORKING or order.tif is not TimeInForce.DAY:
                continue
            close = self._calendar.session_close(self._day_session(order.submitted_at))
            seen = self._seen.get(underlying_of(order.instrument))
            if now >= close and seen is not None and order.submitted_at <= seen <= close:
                working.state = OrderState.EXPIRED
                working.updated_at = close

    def _day_session(self, submitted_at: datetime) -> date:
        day = submitted_at.astimezone(NEW_YORK).date()
        if self._calendar.is_session(day):
            if submitted_at < self._calendar.session_close(day):
                return day
            return self._calendar.next_session(day)
        return self._calendar.roll_to_session(day, "next")

    def _require_connected(self) -> None:
        if not self._connected:
            raise SnapshotVenueError("Call connect() before using SnapshotVenue")

    def _require(self, venue_order_id: str) -> _Working:
        working = self._orders.get(venue_order_id)
        if working is None:
            raise SnapshotVenueError(f"Unknown SnapshotVenue order '{venue_order_id}'")
        return working

    def _ack(
        self, venue_order_id: str, status: Literal["ACCEPTED", "REJECTED"], message: str | None = None
    ) -> VenueAck:
        return VenueAck(venue_order_id=venue_order_id, status=status, timestamp=self._now(), message=message)

    def _now(self) -> datetime:
        now = self._clock.now_utc()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("clock.now_utc() must be timezone-aware")
        return now

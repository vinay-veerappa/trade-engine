"""Deterministic equities paper venue driven by one-minute bars."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Literal
from zoneinfo import ZoneInfo

from trade_engine.calendar.sessions import ExchangeCalendar
from trade_engine.domain.instruments import Equity, Instrument, Side
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
    VenueOrderAllocation,
    VenueOrderState,
    VenuePosition,
)
from trade_engine.interfaces.clock import Clock
from trade_engine.interfaces.market_data import Bar

ZERO = Decimal("0")
BPS = Decimal("10000")
NEW_YORK = ZoneInfo("America/New_York")


class SimBrokerError(RuntimeError):
    """Base class for invalid simulated-venue operations."""


class MissingBarError(SimBrokerError):
    """Raised when a session open or required one-minute bar is missing."""


class UnknownVenueOrderError(SimBrokerError):
    """Raised when an operation references an order not held by this simulator."""


@dataclass
class _WorkingOrder:
    order: VenueOrder
    state: OrderState
    filled_quantity: Decimal
    updated_at: datetime


class SimBroker(BrokerAdapter):
    """Single-account simulator; orders fill only from explicit one-minute bars."""

    name = "SimBroker"
    env = "sim"
    capabilities = Capabilities(
        supported_order_types=frozenset(
            {OrderType.MARKET, OrderType.LIMIT, OrderType.STOP}
        ),
        supported_tifs=frozenset({TimeInForce.DAY, TimeInForce.GTC, TimeInForce.OPG}),
        supports_multi_leg=False,
        supports_native_stops=True,
        supports_streaming=False,
    )

    def __init__(
        self,
        account_id: str,
        clock: Clock,
        slippage_bps: Decimal,
    ) -> None:
        if not account_id:
            raise ValueError("account_id must be non-empty")
        if not isinstance(slippage_bps, Decimal) or not slippage_bps.is_finite():
            raise ValueError("slippage_bps must be a finite Decimal")
        if slippage_bps < ZERO:
            raise ValueError("slippage_bps must be non-negative")
        self.account_id = account_id
        self._clock = clock
        self._slippage_bps = slippage_bps
        self._calendar = ExchangeCalendar()
        self._connected = False
        self._orders: dict[str, _WorkingOrder] = {}
        self._fills: list[VenueFill] = []
        self._fill_counts: dict[str, int] = {}
        self._last_bars: dict[Instrument, Bar] = {}
        self._positions: dict[Instrument, tuple[Decimal, Decimal, datetime]] = {}

    def connect(self) -> VenueIdentity:
        self._connected = True
        return VenueIdentity(
            account_id=self.account_id,
            env=self.env,
            connected_at=self._now(),
            broker_name=self.name,
        )

    def restore(
        self,
        orders: Iterable[tuple[VenueOrder, OrderState]],
        fills: Iterable[VenueFill],
        positions: Iterable[VenuePosition],
    ) -> None:
        """Load resting orders, their fills and positions into an empty simulator.

        SimBroker keeps its book in memory, so a new process starts empty while the
        ledger still holds working orders from earlier sessions (a DAY entry for D+1, a
        GTC stop protecting a swing position). The ledger is the source of truth (I2):
        the host folds it and hands the simulator what a real venue would still hold.
        Anything inconsistent refuses rather than being patched up (I5).
        """
        if self._orders or self._fills or self._last_bars or self._positions:
            raise SimBrokerError("restore() requires an empty SimBroker")
        restored = sorted(orders, key=lambda item: item[0].parent_order_id is not None)
        allowed = {
            OrderState.ACCEPTED,
            OrderState.PARTIALLY_FILLED,
            OrderState.FILLED,
            OrderState.CANCELLED,
            OrderState.EXPIRED,
            OrderState.REJECTED,
        }
        for order, state in restored:
            if state not in allowed:
                raise SimBrokerError(
                    f"Cannot restore '{order.venue_order_id}' in state {state.value}"
                )
            if order.venue_order_id in self._orders:
                raise SimBrokerError(f"Order '{order.venue_order_id}' restored twice")
            self._validate_venue_order(order)
            self._orders[order.venue_order_id] = _WorkingOrder(
                order=order, state=state, filled_quantity=ZERO, updated_at=order.submitted_at
            )
        for fill in sorted(fills, key=lambda item: (item.filled_at, item.venue_fill_id)):
            working = self._orders.get(fill.venue_order_id)
            if working is None:
                raise SimBrokerError(
                    f"Fill '{fill.venue_fill_id}' references unrestored order "
                    f"'{fill.venue_order_id}'"
                )
            prefix, separator, number = fill.venue_fill_id.rpartition(":fill:")
            if prefix != fill.venue_order_id or not separator or not number.isdecimal():
                # A new fill id must never collide with a recorded one, so every restored
                # id must follow the simulator's own numbering.
                raise SimBrokerError(
                    f"Fill id '{fill.venue_fill_id}' is not a SimBroker fill id"
                )
            if fill.instrument != working.order.instrument or fill.side is not working.order.side:
                raise SimBrokerError(
                    f"Fill '{fill.venue_fill_id}' does not match order '{fill.venue_order_id}'"
                )
            self._fill_counts[fill.venue_order_id] = max(
                self._fill_counts.get(fill.venue_order_id, 0), int(number)
            )
            working.filled_quantity += fill.quantity
            working.updated_at = max(working.updated_at, fill.filled_at)
            self._fills.append(fill)
        for venue_order_id, working in self._orders.items():
            filled = working.filled_quantity
            quantity = working.order.quantity
            consistent = {
                OrderState.ACCEPTED: filled == ZERO,
                OrderState.PARTIALLY_FILLED: ZERO < filled < quantity,
                OrderState.FILLED: filled == quantity,
            }.get(working.state, filled <= quantity)
            if not consistent:
                raise SimBrokerError(
                    f"Order '{venue_order_id}' is {working.state.value} with {filled} of "
                    f"{quantity} filled"
                )
        for position in positions:
            if position.instrument in self._positions:
                raise SimBrokerError(f"Position {position.instrument.symbol} restored twice")
            if position.quantity != ZERO:
                self._positions[position.instrument] = (
                    position.quantity,
                    position.avg_price,
                    position.as_of,
                )

    def submit(self, order: VenueOrder) -> VenueAck:
        self._require_connected()
        self._validate_venue_order(order)
        existing = self._orders.get(order.venue_order_id)
        if existing is not None:
            if existing.order != order:
                raise SimBrokerError(
                    f"venue_order_id '{order.venue_order_id}' was reused with different terms"
                )
            if existing.state is OrderState.CANCELLED:
                return self._ack(order.venue_order_id, "REJECTED", "Order is already cancelled")
            if existing.state is OrderState.REJECTED:
                return self._ack(order.venue_order_id, "REJECTED", "Order was rejected")
            return self._ack(order.venue_order_id, "ACCEPTED")
        now = self._now()
        late_reason = self._late_exit_reason(order)
        working = _WorkingOrder(
            order=order,
            state=OrderState.ACCEPTED if late_reason is None else OrderState.REJECTED,
            filled_quantity=ZERO,
            updated_at=now,
        )
        self._orders[order.venue_order_id] = working
        if late_reason is not None:
            return self._ack(order.venue_order_id, "REJECTED", late_reason)
        self._fill_stop_inside_entry_bar(order.venue_order_id, working)
        return self._ack(order.venue_order_id, "ACCEPTED")

    def cancel(self, venue_order_id: str) -> VenueAck:
        working = self._require_order(venue_order_id)
        if working.state is OrderState.CANCELLED:
            return self._ack(venue_order_id, "ACCEPTED")
        if working.state in (OrderState.FILLED, OrderState.EXPIRED, OrderState.REJECTED):
            return self._ack(venue_order_id, "REJECTED", f"Order is {working.state.value}")
        working.state = OrderState.CANCELLED
        working.updated_at = self._event_time()
        return self._ack(venue_order_id, "ACCEPTED")

    def replace(self, venue_order_id: str, changes: OrderChanges) -> VenueAck:
        working = self._require_order(venue_order_id)
        if working.state not in (OrderState.ACCEPTED, OrderState.PARTIALLY_FILLED):
            return self._ack(
                venue_order_id, "REJECTED", f"Order is {working.state.value}"
            )
        quantity = (
            working.order.quantity
            if changes.new_quantity is None
            else changes.new_quantity
        )
        if not quantity.is_finite() or quantity < working.filled_quantity:
            return self._ack(
                venue_order_id,
                "REJECTED",
                "Replacement quantity must be finite and at least the filled quantity",
            )
        if quantity <= ZERO:
            return self._ack(venue_order_id, "REJECTED", "Replacement quantity must be positive")
        try:
            allocations = working.order.allocations
            if changes.new_quantity is not None:
                if len(allocations) != 1:
                    return self._ack(
                        venue_order_id,
                        "REJECTED",
                        "SimBroker requires one-to-one order allocations",
                    )
                allocation = allocations[0]
                allocations = (
                    VenueOrderAllocation(
                        strategy_order_id=allocation.strategy_order_id,
                        account_id=allocation.account_id,
                        quantity=quantity,
                    ),
                )
            working.order = replace(
                working.order,
                quantity=quantity,
                limit_price=(
                    working.order.limit_price
                    if changes.new_limit_price is None
                    else changes.new_limit_price
                ),
                stop_price=(
                    working.order.stop_price
                    if changes.new_stop_price is None
                    else changes.new_stop_price
                ),
                allocations=allocations,
            )
        except ValueError as error:
            return self._ack(venue_order_id, "REJECTED", str(error))
        if working.filled_quantity == quantity:
            working.state = OrderState.FILLED
        working.updated_at = self._event_time()
        return self._ack(venue_order_id, "ACCEPTED")

    def orders(self, since: datetime) -> list[VenueOrderState]:
        self._validate_timestamp(since, "since")
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
        self._validate_timestamp(since, "since")
        return [fill for fill in self._fills if fill.filled_at >= since]

    def positions(self) -> list[VenuePosition]:
        return [
            VenuePosition(
                instrument=instrument,
                quantity=quantity,
                avg_price=average_price,
                as_of=updated_at,
            )
            for instrument, (quantity, average_price, updated_at) in sorted(
                self._positions.items(), key=lambda item: item[0].symbol
            )
            if quantity != ZERO
        ]

    def cash_events(self, since: datetime) -> list[VenueCashEvent]:
        self._validate_timestamp(since, "since")
        return []

    def process_bar(self, bar: Bar | None) -> tuple[VenueFill, ...]:
        """Match working orders against a one-minute bar stamped at its open."""
        self._require_connected()
        if bar is None:
            raise MissingBarError("Cannot simulate fills without an observed bar")
        if not isinstance(bar.instrument, Equity):
            raise ValueError("SimBroker supports equities only")
        if bar.timestamp.second or bar.timestamp.microsecond:
            raise ValueError("SimBroker requires minute-aligned bar timestamps")
        previous = self._last_bars.get(bar.instrument)
        if previous is not None and bar.timestamp == previous.timestamp:
            if bar != previous:
                raise ValueError("Conflicting bars share the same instrument and timestamp")
            return ()
        self._check_bar_sequence(bar)

        for working in self._orders.values():
            if (
                working.order.instrument == bar.instrument
                and working.state in (OrderState.ACCEPTED, OrderState.PARTIALLY_FILLED)
            ):
                self._expire_order(working, bar)
        if not self._is_regular_session_bar(bar):
            # Extended-hours bars keep the sequence contiguous but never trigger fills.
            self._last_bars[bar.instrument] = bar
            return ()

        active = [
            (venue_id, working)
            for venue_id, working in self._orders.items()
            if working.order.instrument == bar.instrument
            and working.state in (OrderState.ACCEPTED, OrderState.PARTIALLY_FILLED)
            and working.filled_quantity < working.order.quantity
        ]
        candidates: list[tuple[str, _WorkingOrder, Decimal]] = []
        for venue_id, working in active:
            price = self._execution_price(working.order, bar)
            if price is not None:
                candidates.append((venue_id, working, price))

        by_group: dict[str, list[tuple[str, _WorkingOrder, Decimal]]] = {}
        standalone: list[tuple[str, _WorkingOrder, Decimal]] = []
        for candidate in candidates:
            group = candidate[1].order.oco_group
            if group is None:
                standalone.append(candidate)
            else:
                by_group.setdefault(group, []).append(candidate)

        fills: list[VenueFill] = []
        for candidate in standalone:
            fill = self._fill(candidate[0], candidate[1], candidate[2], bar.timestamp)
            if fill is not None:
                fills.append(fill)
        for group in sorted(by_group):
            choices = sorted(by_group[group], key=self._oco_priority)
            stop = next(
                (candidate for candidate in choices if candidate[1].order.order_type is OrderType.STOP),
                None,
            )
            selected = (stop,) if stop is not None else tuple(choices)
            for venue_id, working, price in selected:
                fill = self._fill(venue_id, working, price, bar.timestamp)
                if fill is not None:
                    fills.append(fill)

        self._last_bars[bar.instrument] = bar
        return tuple(fills)

    def _execution_price(self, order: VenueOrder, bar: Bar) -> Decimal | None:
        if bar.timestamp <= order.submitted_at:
            return None
        if order.tif is TimeInForce.OPG:
            if (
                bar.timestamp != self._opg_session_open(order.submitted_at)
                or order.order_type is not OrderType.MARKET
            ):
                return None
            base = bar.open
        elif order.order_type is OrderType.MARKET:
            base = bar.open
        elif order.order_type is OrderType.LIMIT:
            limit_price = order.limit_price
            if limit_price is None:
                raise SimBrokerError("Accepted limit order has no limit price")
            if order.side is Side.BUY:
                if bar.low > limit_price:
                    return None
                base = min(bar.open, limit_price)
            else:
                if bar.high < limit_price:
                    return None
                base = max(bar.open, limit_price)
        elif order.order_type is OrderType.STOP:
            stop_price = order.stop_price
            if stop_price is None:
                raise SimBrokerError("Accepted stop order has no stop price")
            if order.side is Side.BUY:
                if bar.high < stop_price:
                    return None
                base = max(bar.open, stop_price)
            else:
                if bar.low > stop_price:
                    return None
                base = min(bar.open, stop_price)
        else:
            raise SimBrokerError(
                f"Unsupported accepted order type {order.order_type.value}"
            )
        slipped = self._slipped(base, order.side)
        if order.order_type is OrderType.LIMIT and order.limit_price is not None:
            if order.side is Side.BUY:
                return min(slipped, order.limit_price)
            return max(slipped, order.limit_price)
        return slipped

    def _slipped(self, base: Decimal, side: Side) -> Decimal:
        return base * (
            Decimal("1") + self._slippage_bps / BPS
            if side is Side.BUY
            else Decimal("1") - self._slippage_bps / BPS
        )

    def _late_exit_reason(self, order: VenueOrder) -> str | None:
        """Refuse an exit that arrives after bars following its entry fill were simulated.

        Those bars are gone, so the exit cannot be matched against them; accepting it
        would silently skip any stop or target they reached (I5). The caller must
        reconcile after every bar.
        """
        if order.parent_order_id is None or order.order_type is OrderType.MARKET:
            # A market close is a new decision, not a stop or target that should already
            # have been working; there is no trigger in the skipped bars for it to miss.
            return None
        entry_fills = [
            fill.filled_at for fill in self._fills if fill.venue_order_id == order.parent_order_id
        ]
        latest_bar = self._last_bars.get(order.instrument)
        if not entry_fills or latest_bar is None or latest_bar.timestamp <= max(entry_fills):
            return None
        return (
            f"Exit arrived after bars following entry '{order.parent_order_id}' filled at "
            f"{max(entry_fills).isoformat()} were simulated (latest bar "
            f"{latest_bar.timestamp.isoformat()}); reconcile after every bar"
        )

    def _fill_stop_inside_entry_bar(self, venue_order_id: str, working: _WorkingOrder) -> None:
        """Fill a protective stop against the bar that filled its entry, when touched.

        The OMS submits children only after it sees the entry fill, so they arrive after that
        bar has been processed. A one-minute bar does not reveal whether its low (for a long)
        came after the entry, so a touched stop is assumed hit: the same pessimism as
        stop-before-target. Targets are not filled this way; the bar may have reached them
        before the entry.
        """
        order = working.order
        if order.parent_order_id is None or order.order_type is not OrderType.STOP:
            return
        entry_bar = self._last_bars.get(order.instrument)
        if entry_bar is None or not self._is_regular_session_bar(entry_bar):
            return
        entry_fills = [
            fill
            for fill in self._fills
            if fill.venue_order_id == order.parent_order_id
            and fill.filled_at == entry_bar.timestamp
        ]
        stop_price = order.stop_price
        if not entry_fills or stop_price is None:
            return
        entry_price = entry_fills[-1].price
        if order.side is Side.SELL:
            if entry_bar.low > stop_price:
                return
            # An entry already below the stop exits at the entry price, not the stop.
            base = min(stop_price, entry_price)
        else:
            if entry_bar.high < stop_price:
                return
            base = max(stop_price, entry_price)
        self._fill(venue_order_id, working, self._slipped(base, order.side), entry_bar.timestamp)

    def _fill(
        self,
        venue_order_id: str,
        working: _WorkingOrder,
        price: Decimal,
        filled_at: datetime,
    ) -> VenueFill | None:
        if working.state not in (OrderState.ACCEPTED, OrderState.PARTIALLY_FILLED):
            return None
        quantity = working.order.quantity - working.filled_quantity
        if working.order.parent_order_id is not None:
            # Until the OMS reconciles, an exit may close only its own bracket's open
            # quantity, never another bracket's shares in the same symbol.
            quantity = min(quantity, self._bracket_open_quantity(working.order.parent_order_id))
            position_quantity = self._positions.get(
                working.order.instrument, (ZERO, ZERO, filled_at)
            )[0]
            closes_position = (
                position_quantity > ZERO and working.order.side is Side.SELL
            ) or (
                position_quantity < ZERO and working.order.side is Side.BUY
            )
            available = abs(position_quantity) if closes_position else ZERO
            quantity = min(quantity, available)
            if quantity == ZERO:
                return None
        fill_number = self._fill_counts.get(venue_order_id, 0) + 1
        self._fill_counts[venue_order_id] = fill_number
        fill = VenueFill(
            venue_fill_id=f"{venue_order_id}:fill:{fill_number}",
            venue_order_id=venue_order_id,
            instrument=working.order.instrument,
            quantity=quantity,
            price=price,
            filled_at=filled_at,
            side=working.order.side,
        )
        self._fills.append(fill)
        working.filled_quantity += quantity
        working.state = (
            OrderState.FILLED
            if working.filled_quantity == working.order.quantity
            else OrderState.PARTIALLY_FILLED
        )
        working.updated_at = filled_at
        self._update_position(fill)
        return fill

    def _bracket_open_quantity(self, parent_order_id: str) -> Decimal:
        parent = self._require_order(parent_order_id)
        exited = sum(
            (
                working.filled_quantity
                for working in self._orders.values()
                if working.order.parent_order_id == parent_order_id
            ),
            ZERO,
        )
        return max(parent.filled_quantity - exited, ZERO)

    def _update_position(self, fill: VenueFill) -> None:
        quantity, average_price, _ = self._positions.get(
            fill.instrument, (ZERO, ZERO, fill.filled_at)
        )
        change = fill.quantity if fill.side is Side.BUY else -fill.quantity
        updated_quantity = quantity + change
        if quantity == ZERO or (quantity > ZERO) == (change > ZERO):
            basis_quantity = abs(quantity) + abs(change)
            updated_average = (
                (abs(quantity) * average_price + abs(change) * fill.price) / basis_quantity
            )
        elif updated_quantity == ZERO:
            updated_average = ZERO
        elif (updated_quantity > ZERO) != (quantity > ZERO):
            updated_average = fill.price
        else:
            updated_average = average_price
        self._positions[fill.instrument] = (
            updated_quantity,
            updated_average,
            fill.filled_at,
        )

    def _expire_order(self, working: _WorkingOrder, bar: Bar) -> None:
        order = working.order
        if bar.timestamp <= order.submitted_at:
            return
        if order.tif is TimeInForce.DAY:
            # An order entered after the close (the 17:45 EOD job) works the next session.
            expired = bar.timestamp >= self._calendar.session_close(
                self._day_session(order.submitted_at)
            )
        elif order.tif is TimeInForce.OPG:
            expired = bar.timestamp > self._opg_session_open(order.submitted_at)
        else:
            expired = False
        if expired:
            working.state = OrderState.EXPIRED
            working.updated_at = bar.timestamp

    def _day_session(self, submitted_at: datetime) -> date:
        submitted_date = submitted_at.astimezone(NEW_YORK).date()
        if self._calendar.is_session(submitted_date):
            if submitted_at < self._calendar.session_close(submitted_date):
                return submitted_date
            return self._calendar.next_session(submitted_date)
        return self._calendar.roll_to_session(submitted_date, "next")

    def _opg_session_open(self, submitted_at: datetime) -> datetime:
        submitted_date = submitted_at.astimezone(NEW_YORK).date()
        if self._calendar.is_session(submitted_date):
            session_open = self._calendar.session_open(submitted_date)
            if submitted_at < session_open:
                return session_open
            session_date = self._calendar.next_session(submitted_date)
        else:
            session_date = self._calendar.roll_to_session(submitted_date, "next")
        return self._calendar.session_open(session_date)

    def _is_regular_session_bar(self, bar: Bar) -> bool:
        bar_date = bar.timestamp.astimezone(NEW_YORK).date()
        return (
            self._calendar.session_open(bar_date)
            <= bar.timestamp
            < self._calendar.session_close(bar_date)
        )

    @staticmethod
    def _oco_priority(
        candidate: tuple[str, _WorkingOrder, Decimal],
    ) -> tuple[bool, bool, int, str]:
        venue_id, working, _ = candidate
        if working.order.order_type is OrderType.STOP:
            return False, False, 0, venue_id
        strategy_order_id = working.order.allocations[0].strategy_order_id
        prefix, separator, suffix = strategy_order_id.rpartition(":target:")
        if separator and prefix and suffix.isdecimal():
            return True, False, int(suffix), strategy_order_id
        return True, True, 0, venue_id

    def _check_bar_sequence(self, bar: Bar) -> None:
        previous = self._last_bars.get(bar.instrument)
        bar_date = bar.timestamp.astimezone(NEW_YORK).date()
        if not self._calendar.is_session(bar_date):
            raise MissingBarError(
                f"Bar for {bar.instrument.symbol} is on non-session date {bar_date}"
            )
        session_open = self._calendar.session_open(bar_date)
        if previous is None:
            if bar.timestamp != session_open:
                raise MissingBarError(
                    f"Missing session opening one-minute bar for {bar.instrument.symbol} "
                    f"at {session_open.isoformat()}"
                )
            return
        if bar.timestamp == previous.timestamp:
            return
        if bar.timestamp < previous.timestamp:
            raise ValueError(
                f"Out-of-order bar for {bar.instrument.symbol}: "
                f"{bar.timestamp.isoformat()} follows {previous.timestamp.isoformat()}"
            )
        previous_date = previous.timestamp.astimezone(NEW_YORK).date()
        if bar_date == previous_date:
            if bar.timestamp - previous.timestamp != timedelta(minutes=1):
                raise MissingBarError(
                    f"Missing one-minute bar for {bar.instrument.symbol} between "
                    f"{previous.timestamp.isoformat()} and {bar.timestamp.isoformat()}"
                )
            return
        last_regular_bar = self._calendar.session_close(previous_date) - timedelta(minutes=1)
        if previous.timestamp < last_regular_bar:
            raise MissingBarError(
                f"Missing closing one-minute bars for {bar.instrument.symbol}: session "
                f"{previous_date} ended at {previous.timestamp.isoformat()}, expected "
                f"{last_regular_bar.isoformat()}"
            )
        next_session = self._calendar.next_session(previous_date)
        if bar_date != next_session:
            raise MissingBarError(
                f"Missing session bars for {bar.instrument.symbol}: expected "
                f"{next_session}, received {bar_date}"
            )
        if bar.timestamp != session_open:
            raise MissingBarError(
                f"Missing session opening one-minute bar for {bar.instrument.symbol} "
                f"at {session_open.isoformat()}"
            )

    def _validate_venue_order(self, order: VenueOrder) -> None:
        if not isinstance(order.instrument, Equity):
            raise ValueError("SimBroker accepts equity orders only")
        if order.order_type not in self.capabilities.supported_order_types:
            raise ValueError(f"Unsupported order type {order.order_type.value}")
        if order.tif not in self.capabilities.supported_tifs:
            raise ValueError(f"Unsupported time in force {order.tif.value}")
        if order.tif is TimeInForce.OPG and order.order_type is not OrderType.MARKET:
            raise ValueError("OPG is supported only for market-on-open orders")
        if any(allocation.account_id != self.account_id for allocation in order.allocations):
            raise ValueError("Venue order allocation account does not match SimBroker account")
        if len(order.allocations) != 1:
            raise ValueError("SimBroker requires one-to-one strategy-order allocations")
        if order.parent_order_id is not None:
            parent = self._orders.get(order.parent_order_id)
            if parent is None:
                raise ValueError(
                    f"Parent order '{order.parent_order_id}' is not held by this SimBroker"
                )
            if parent.order.instrument != order.instrument or parent.order.side is order.side:
                raise ValueError(
                    f"Exit '{order.venue_order_id}' must close parent "
                    f"'{order.parent_order_id}' in the same instrument"
                )

    def _require_connected(self) -> None:
        if not self._connected:
            raise SimBrokerError("Call connect() before using SimBroker")

    def _require_order(self, venue_order_id: str) -> _WorkingOrder:
        try:
            return self._orders[venue_order_id]
        except KeyError as error:
            raise UnknownVenueOrderError(
                f"Unknown SimBroker order '{venue_order_id}'"
            ) from error

    def _ack(
        self,
        venue_order_id: str,
        status: Literal["ACCEPTED", "REJECTED"],
        message: str | None = None,
    ) -> VenueAck:
        return VenueAck(
            venue_order_id=venue_order_id,
            status=status,
            timestamp=self._event_time(),
            message=message,
        )

    def _now(self) -> datetime:
        now = self._clock.now_utc()
        self._validate_timestamp(now, "clock.now_utc()")
        return now

    def _event_time(self) -> datetime:
        now = self._now()
        latest_bar = max((bar.timestamp for bar in self._last_bars.values()), default=now)
        return max(now, latest_bar)

    @staticmethod
    def _validate_timestamp(value: datetime, name: str) -> None:
        if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
            raise ValueError(f"{name} must be timezone-aware")

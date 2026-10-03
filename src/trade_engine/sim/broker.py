"""Deterministic equities paper venue driven by one-minute bars.

Every rule (order validation, the restore checks, expiry, the bar-sequence rule, every
fill price and the position average) lives in Rust: ``te_core::sim::broker``
(docs/RUST_PORT.md P3a). This adapter is the BrokerAdapter plumbing only: it reads the
clock where the rules ask for it, passes plain values in, and builds the carriers from
what comes back. A timestamp comes back as the ``isoformat()`` text it went in as, and
is handed back as the caller's own object.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from typing import Any

from trade_engine.domain.instruments import Instrument
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
from trade_engine.ledger import codec
from trade_engine.sim import _rs


class SimBrokerError(RuntimeError):
    """Base class for invalid simulated-venue operations."""


class MissingBarError(SimBrokerError):
    """Raised when a session open or required one-minute bar is missing."""


class UnknownVenueOrderError(SimBrokerError):
    """Raised when an operation references an order not held by this simulator."""


_rs.register("sim", SimBrokerError)
_rs.register("missing_bar", MissingBarError)
_rs.register("unknown_order", UnknownVenueOrderError)

_TIMES_HELD = 4096


class _Carriers:
    """The objects the host handed in, by the plain values that crossed for them."""

    def __init__(self) -> None:
        self._times: dict[str, datetime] = {}
        self._texts: dict[Instrument, str] = {}
        self._by_key: dict[str, Instrument] = {}

    def iso(self, value: datetime) -> str:
        text = value.isoformat()
        if text not in self._times:
            if len(self._times) >= _TIMES_HELD:
                del self._times[next(iter(self._times))]
            self._times[text] = value
        return text

    def dt(self, text: str) -> datetime:
        held = self._times.get(text)
        return held if held is not None else datetime.fromisoformat(text)

    def text(self, instrument: Instrument) -> str:
        try:
            held = self._texts.get(instrument)
        except TypeError:
            held = None
        if held is not None:
            return held
        text = codec.text(codec._encode(instrument))
        key = _rs.call(_rs.rs.sim_instrument_key, text)
        self._by_key.setdefault(key, instrument)
        try:
            self._texts[instrument] = text
        except TypeError:
            pass
        return text

    def instrument(self, key: str) -> Instrument:
        return self._by_key[key]


def _opt(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


class SimBroker(BrokerAdapter):
    """Single-account simulator; orders fill only from explicit one-minute bars."""

    name = "SimBroker"
    env = "sim"
    capabilities = Capabilities(
        supported_order_types=frozenset(
            {OrderType.MARKET, OrderType.LIMIT, OrderType.STOP, OrderType.STOP_LIMIT}
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
        is_decimal = isinstance(slippage_bps, Decimal)
        self._book = _rs.call(
            _rs.rs.SimBook, account_id, is_decimal, str(slippage_bps) if is_decimal else "0"
        )
        self.account_id = account_id
        self._clock = clock
        self._carriers = _Carriers()
        self._orders: dict[str, VenueOrder] = {}
        self._fills: list[VenueFill] = []

    # --- the plumbing ------------------------------------------------------------------

    def _now(self) -> str:
        return self._carriers.iso(self._clock.now_utc())

    def _call(
        self,
        method: str,
        *args: Any,
        restored: list[VenueFill] | None = None,
        hold: Iterable[VenueOrder] = (),
    ) -> Any:
        try:
            return _rs.call(getattr(self._book, method), *args)
        finally:
            for order in hold:
                self._hold(order)
            self._sync_fills(restored)

    def _sync_fills(self, restored: list[VenueFill] | None) -> None:
        while len(self._fills) < self._book.fill_count():
            fill_id, order_id, quantity, price, filled_at, _, src = self._book.fill(len(self._fills))
            if src is not None:
                assert restored is not None
                self._fills.append(restored[src])
                continue
            order = self._orders[order_id]
            self._fills.append(
                VenueFill(
                    venue_fill_id=fill_id,
                    venue_order_id=order_id,
                    instrument=order.instrument,
                    quantity=Decimal(quantity),
                    price=Decimal(price),
                    filled_at=self._carriers.dt(filled_at),
                    side=order.side,
                )
            )

    def _order(self, order: VenueOrder) -> tuple[Any, ...]:
        return (
            order.venue_order_id,
            self._carriers.text(order.instrument),
            order.order_type.value,
            order.side.value,
            str(order.quantity),
            self._carriers.iso(order.submitted_at),
            order.tif.value,
            (_opt(order.limit_price), _opt(order.stop_price), _opt(order.trail_amount)),
            [(a.strategy_order_id, a.account_id, str(a.quantity)) for a in order.allocations],
            order.parent_order_id,
            order.oco_group,
        )

    def _hold(self, order: VenueOrder) -> None:
        if order.venue_order_id not in self._orders and self._book.has(order.venue_order_id):
            self._orders[order.venue_order_id] = order

    def _ack(self, ack: tuple[str, str, str, str | None]) -> VenueAck:
        venue_order_id, status, timestamp, message = ack
        return VenueAck(
            venue_order_id=venue_order_id,
            status=status,  # type: ignore[arg-type]
            timestamp=self._carriers.dt(timestamp),
            message=message,
        )

    # --- BrokerAdapter -----------------------------------------------------------------

    def connect(self) -> VenueIdentity:
        connected_at = self._call("connect", self._now)
        return VenueIdentity(
            account_id=self.account_id,
            env=self.env,
            connected_at=self._carriers.dt(connected_at),
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
        carriers = self._carriers
        restored = sorted(orders, key=lambda item: item[0].parent_order_id is not None)
        held = list(fills)
        self._call(
            "restore",
            [(self._order(order), state.value) for order, state in restored],
            [
                (
                    fill.venue_fill_id,
                    fill.venue_order_id,
                    carriers.text(fill.instrument),
                    str(fill.quantity),
                    str(fill.price),
                    carriers.iso(fill.filled_at),
                    fill.side.value,
                )
                for fill in held
            ],
            [
                (
                    carriers.text(position.instrument),
                    str(position.quantity),
                    str(position.avg_price),
                    carriers.iso(position.as_of),
                )
                for position in positions
            ],
            self._now,
            restored=held,
            hold=[order for order, _ in restored],
        )

    def submit(self, order: VenueOrder) -> VenueAck:
        return self._ack(self._call("submit", self._order(order), self._now, hold=(order,)))

    def cancel(self, venue_order_id: str) -> VenueAck:
        return self._ack(self._call("cancel", venue_order_id, self._now))

    def replace(self, venue_order_id: str, changes: OrderChanges) -> VenueAck:
        new_quantity = changes.new_quantity
        outcome, value = self._call(
            "replace_begin", venue_order_id, _opt(new_quantity), self._now
        )
        if outcome == "ack":
            return self._ack(value)
        current = self._orders[venue_order_id]
        quantity = current.quantity if new_quantity is None else new_quantity
        try:
            allocations = current.allocations
            if new_quantity is not None:
                allocation = allocations[0]
                allocations = (
                    VenueOrderAllocation(
                        strategy_order_id=allocation.strategy_order_id,
                        account_id=allocation.account_id,
                        quantity=quantity,
                    ),
                )
            updated = replace(
                current,
                quantity=quantity,
                limit_price=(
                    current.limit_price if changes.new_limit_price is None else changes.new_limit_price
                ),
                stop_price=(
                    current.stop_price if changes.new_stop_price is None else changes.new_stop_price
                ),
                allocations=allocations,
            )
        except ValueError as error:
            return self._ack(self._call("replace_reject", venue_order_id, str(error), self._now))
        self._orders[venue_order_id] = updated
        return self._ack(self._call("replace_commit", venue_order_id, self._order(updated), self._now))

    def orders(self, since: datetime) -> list[VenueOrderState]:
        rows = self._call("orders", since.isoformat(), self._now)
        return [
            VenueOrderState(
                venue_order_id=venue_order_id,
                state=OrderState(state),
                filled_quantity=Decimal(filled),
                remaining_quantity=Decimal(remaining),
                updated_at=self._carriers.dt(updated_at),
            )
            for venue_order_id, state, filled, remaining, updated_at in rows
        ]

    def fills(self, since: datetime) -> list[VenueFill]:
        return [self._fills[i] for i in self._call("fills", since.isoformat(), self._now)]

    def positions(self) -> list[VenuePosition]:
        return [
            VenuePosition(
                instrument=self._carriers.instrument(key),
                quantity=Decimal(quantity),
                avg_price=Decimal(average),
                as_of=self._carriers.dt(as_of),
            )
            for key, quantity, average, as_of in self._call("positions", self._now)
        ]

    def cash_events(self, since: datetime) -> list[VenueCashEvent]:
        self._call("cash_events", since.isoformat())
        return []

    def process_bar(self, bar: Bar | None) -> tuple[VenueFill, ...]:
        """Match working orders against a one-minute bar stamped at its open."""
        carriers = self._carriers
        crossing = None
        if bar is not None:
            crossing = (
                carriers.text(bar.instrument),
                carriers.iso(bar.timestamp),
                str(bar.open),
                str(bar.high),
                str(bar.low),
                str(bar.close),
                str(bar.volume),
                carriers.iso(bar.as_of),
            )
        return tuple(self._fills[i] for i in self._call("process_bar", crossing))

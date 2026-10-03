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

Every rule above lives in Rust: ``te_core::sim::snapshot`` (docs/RUST_PORT.md P3a). This
adapter is the BrokerAdapter plumbing only: it reads the clock where the rules ask for
it, looks a quote up by OCC symbol as ``ChainSnapshot.get`` does, and builds the carriers
from what comes back. The sessions are the engine's XNYS ``ExchangeCalendar`` (D7).
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from decimal import Decimal
from typing import Any

from trade_engine.calendar.sessions import ExchangeCalendar
from trade_engine.domain.instruments import (
    Combo,
    Equity,
    Instrument,
    OptionContract,
    Side,
)
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
from trade_engine.sim import _rs
from trade_engine.sim.broker import _Carriers

ZERO = Decimal("0")


class SnapshotVenueError(RuntimeError):
    """An invalid operation on the snapshot venue."""


_rs.register("snapshot", SnapshotVenueError)

_UNDERLYINGS: dict[Any, str] = {}
_UNDERLYINGS_HELD = 4096
_TEXTS = _Carriers()


def underlying_of(instrument: Instrument) -> str:
    """The underlying whose chain snapshot prices ``instrument``."""
    if not isinstance(instrument, (Equity, OptionContract, Combo)):
        raise ValueError(f"Cannot price {type(instrument).__name__} from a chain snapshot")
    try:
        held = _UNDERLYINGS.get(instrument)
    except TypeError:
        held = None
    if held is not None:
        return held
    found = _rs.call(_rs.rs.sim_underlying_of, _TEXTS.text(instrument))
    try:
        if len(_UNDERLYINGS) >= _UNDERLYINGS_HELD:
            del _UNDERLYINGS[next(iter(_UNDERLYINGS))]
        _UNDERLYINGS[instrument] = found
    except TypeError:
        pass
    return found


def _decimal(value: object) -> str | None:
    """A Decimal's text; None for anything else (the rule refuses it by name)."""
    return str(value) if isinstance(value, Decimal) else None


class _Quotes:
    """``ChainSnapshot.get`` by OCC symbol: the first quote listed under it."""

    def __init__(self, snapshot: ChainSnapshot) -> None:
        self._snapshot = snapshot
        self._by_occ: dict[str, Any] | None = None

    def __call__(self, occ: str) -> tuple[str, str, str] | None:
        if self._by_occ is None:
            self._by_occ = {}
            for quote in self._snapshot.quotes:
                self._by_occ.setdefault(quote.contract.occ, quote)
        quote = self._by_occ.get(occ)
        if quote is None:
            return None
        return (str(quote.mid), str(quote.spread), quote.as_of.isoformat())


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
        age = max_quote_age_seconds
        is_number = isinstance(age, (int, float)) and not isinstance(age, bool)
        self._book = _rs.call(
            _rs.rs.SnapBook,
            account_id if isinstance(account_id, str) else (str(account_id) if account_id else ""),
            _decimal(fill_fraction),
            _decimal(fee_per_contract),
            _decimal(equity_slippage_bps),
            float(age) if is_number else None,
        )
        if calendar is not None and (
            not isinstance(calendar, ExchangeCalendar) or calendar.exchange != "XNYS"
        ):
            raise ValueError(f"SnapshotVenue sessions are the XNYS ExchangeCalendar, got {calendar!r} (I5)")
        self.account_id = account_id
        self._clock = clock
        self._carriers = _Carriers()
        self._orders: dict[str, VenueOrder] = {}
        self._fills: list[VenueFill] = []
        self._restored_positions: list[VenuePosition] = []

    # -- the plumbing -----------------------------------------------------------------

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
                if order.venue_order_id not in self._orders and self._book.has(order.venue_order_id):
                    self._orders[order.venue_order_id] = order
            self._sync_fills(restored)

    def _sync_fills(self, restored: list[VenueFill] | None) -> None:
        while len(self._fills) < self._book.fill_count():
            fill_id, order_id, quantity, price, filled_at, side, fee, leg_id, leg, src = self._book.fill(
                len(self._fills)
            )
            if src is not None:
                assert restored is not None
                self._fills.append(restored[src])
                continue
            instrument = self._orders[order_id].instrument
            contract = instrument.legs[leg].contract if isinstance(instrument, Combo) else instrument
            self._fills.append(
                VenueFill(
                    venue_fill_id=fill_id,
                    venue_order_id=order_id,
                    instrument=contract,
                    quantity=Decimal(quantity),
                    price=Decimal(price),
                    filled_at=self._carriers.dt(filled_at),
                    side=Side(side),
                    fee=Decimal(fee),
                    leg_id=leg_id,
                )
            )

    def _order(self, order: VenueOrder) -> tuple[Any, ...]:
        def opt(value: Decimal | None) -> str | None:
            return None if value is None else str(value)

        return (
            order.venue_order_id,
            self._carriers.text(order.instrument),
            order.order_type.value,
            order.side.value,
            str(order.quantity),
            self._carriers.iso(order.submitted_at),
            order.tif.value,
            (opt(order.limit_price), opt(order.stop_price), opt(order.trail_amount)),
            [(a.strategy_order_id, a.account_id, str(a.quantity)) for a in order.allocations],
            order.parent_order_id,
            order.oco_group,
        )

    def _ack(self, ack: tuple[str, str, str, str | None]) -> VenueAck:
        venue_order_id, status, timestamp, message = ack
        return VenueAck(
            venue_order_id=venue_order_id,
            status=status,  # type: ignore[arg-type]
            timestamp=self._carriers.dt(timestamp),
            message=message,
        )

    # -- the BrokerAdapter contract -------------------------------------------------

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
        """Load what the ledger says this venue holds into an empty venue (I2).

        Each run is a new process and this venue keeps its book in memory, so the host
        folds the ledger and hands back the working orders, their fills and the positions.
        Anything inconsistent refuses rather than being patched up (I5).
        """
        carriers = self._carriers
        restored = list(orders)
        held = list(fills)
        given = list(positions)
        # Set before the call: a refusal part-way keeps what was restored before it, as
        # the pre-port venue did, and those rows point into this list.
        self._restored_positions = given
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
                    str(fill.fee),
                    fill.leg_id,
                )
                for fill in held
            ],
            [(carriers.text(position.instrument), str(position.quantity)) for position in given],
            restored=held,
            hold=[order for order, _ in restored],
        )

    def submit(self, order: VenueOrder) -> VenueAck:
        return self._ack(self._call("submit", self._order(order), self._now, hold=(order,)))

    def cancel(self, venue_order_id: str) -> VenueAck:
        return self._ack(self._call("cancel", venue_order_id, self._now))

    def replace(self, venue_order_id: str, changes: OrderChanges) -> VenueAck:
        # Nothing here replaces an option order; a changed price is a cancel and a new order.
        return self._ack(self._call("replace", venue_order_id, self._now))

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
        as_of, rows = self._call("positions", self._now)
        now = self._carriers.dt(as_of)
        out = []
        for (origin, index), quantity in rows:
            source = self._restored_positions[index] if origin == "restored" else self._fills[index]
            out.append(
                VenuePosition(instrument=source.instrument, quantity=Decimal(quantity), avg_price=ZERO, as_of=now)
            )
        return out

    def cash_events(self, since: datetime) -> list[VenueCashEvent]:
        return []

    # -- matching ---------------------------------------------------------------------

    def process_snapshot(self, snapshot: ChainSnapshot) -> tuple[VenueFill, ...]:
        """Match every working order on the snapshot's underlying against it."""
        made = self._call(
            "process_snapshot",
            snapshot.underlying,
            self._carriers.iso(snapshot.as_of),
            str(snapshot.underlying_price),
            _Quotes(snapshot),
            self._now,
        )
        return tuple(self._fills[i] for i in made)

    def model_price(self, instrument: Instrument, side: Side, snapshot: ChainSnapshot) -> Decimal | None:
        """What ``side`` of ``instrument`` trades at in ``snapshot``; None if it can't be priced."""
        if not isinstance(instrument, (Equity, OptionContract)):
            return None
        price = _rs.call(
            self._book.model_price,
            self._carriers.text(instrument),
            side.value,
            snapshot.underlying,
            snapshot.as_of.isoformat(),
            str(snapshot.underlying_price),
            _Quotes(snapshot),
        )
        return None if price is None else Decimal(price)

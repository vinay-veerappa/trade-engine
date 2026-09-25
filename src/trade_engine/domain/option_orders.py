"""What an options strategy asks for, and what it sees of its open positions (O4).

An options strategy never touches a broker (I13). It returns these to the engine, which
routes them through the risk layer and the OMS:

- ``OptionIntent`` opens a structure (a single contract or an options ``Combo``), or buys
  or sells shares, e.g. the stock half of a buy-write. A structure may name a
  ``profit_target``: a net price per unit at which a GTC closing limit rests from the
  moment the entry fills (rules doc §3: "Option profit target — resting GTC limit").
- ``CloseStructure`` closes whatever is still open of a structure, at market or a limit.
- ``CloseHolding`` sells (or covers) shares the account holds outside any structure:
  shares delivered by an assignment, bought for a buy-write, or imported holdings.

A combo trades its legs as written. SELL means the order collects a net credit and BUY
that it pays a net debit (the Schwab ``NET_CREDIT`` / ``NET_DEBIT`` convention); the
price is that net amount per unit, in option points. A structure's closing order
reverses every leg.

``OpenStructure`` is the strategy's view of one open structure, folded from the ledger:
the entry order's legs, how many units are still open, and what was paid or collected
per unit.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from trade_engine.domain.instruments import Combo, ComboLeg, Equity, Instrument, OptionContract, Side
from trade_engine.domain.option_roots import option_style
from trade_engine.domain.orders import OrderType, TimeInForce


def _require(value: str, name: str) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty string")


def _positive(value: Decimal | None, name: str) -> None:
    if value is None:
        return
    if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
        raise ValueError(f"{name} must be a finite positive Decimal, got {value!r}")


def _whole(value: Decimal, name: str) -> None:
    if not isinstance(value, Decimal) or not value.is_finite() or value <= 0 or value != value.to_integral_value():
        raise ValueError(f"{name} must be a whole positive number, got {value!r}")


def is_structure(instrument: Instrument) -> bool:
    """An options position the engine tracks as one structure: a contract or an options combo."""
    if isinstance(instrument, OptionContract):
        return True
    return isinstance(instrument, Combo) and all(
        isinstance(leg.contract, OptionContract) for leg in instrument.legs
    )


def legs_of(instrument: Instrument, side: Side) -> tuple[ComboLeg, ...]:
    """The legs an order trades: a combo's own, or one leg of a single instrument."""
    if isinstance(instrument, Combo):
        return instrument.legs
    return (ComboLeg(instrument, 1, side),)


def reverse(instrument: Instrument, side: Side) -> tuple[Instrument, Side]:
    """The order that undoes ``side`` of ``instrument``: every leg flipped.

    Undoing a credit is a debit and the other way round, so the order's side flips too.
    """
    flipped = Side.BUY if side is Side.SELL else Side.SELL
    if isinstance(instrument, Combo):
        return (
            Combo(tuple(ComboLeg(leg.contract, leg.ratio, _flip(leg.side)) for leg in instrument.legs)),
            flipped,
        )
    return instrument, flipped


def _flip(side: Side) -> Side:
    return Side.BUY if side is Side.SELL else Side.SELL


@dataclass(frozen=True)
class OptionIntent:
    """Open an options structure, or trade shares, for one account."""

    intent_id: str
    account_id: str
    instrument: Instrument  # OptionContract, an options Combo, or Equity
    side: Side  # a combo: SELL collects a net credit, BUY pays a net debit
    quantity: Decimal  # whole units: contracts, combo units or shares
    reason: str
    command_id: str  # idempotency key (I3)
    order_type: OrderType = OrderType.LIMIT
    limit_price: Decimal | None = None  # net per unit in option points; required for LIMIT
    tif: TimeInForce = TimeInForce.DAY
    profit_target: Decimal | None = None  # net per unit the GTC closing limit rests at

    def __post_init__(self) -> None:
        for name in ("intent_id", "account_id", "reason", "command_id"):
            _require(getattr(self, name), name)
        if not isinstance(self.side, Side):
            raise ValueError(f"Invalid side {self.side!r}")
        _whole(self.quantity, "quantity")
        if isinstance(self.instrument, Combo):
            if not is_structure(self.instrument):
                raise ValueError("A combo intent must be options only; buy or sell the shares on their own")
        elif not isinstance(self.instrument, (OptionContract, Equity)):
            raise ValueError(f"Cannot open {type(self.instrument).__name__}")
        if self.order_type is OrderType.LIMIT:
            if self.limit_price is None:
                raise ValueError("A LIMIT intent needs a limit_price (I5)")
            _positive(self.limit_price, "limit_price")
        elif self.order_type is OrderType.MARKET:
            if self.limit_price is not None:
                raise ValueError("A MARKET intent cannot carry a limit_price")
        else:
            raise ValueError(f"Options intents are MARKET or LIMIT, not {self.order_type.value}")
        if self.tif is not TimeInForce.DAY:
            # An entry works one snapshot; one that missed is decided again, not left resting.
            raise ValueError("An entry works for one session: tif must be DAY")
        _positive(self.profit_target, "profit_target")
        if self.profit_target is not None:
            if not is_structure(self.instrument):
                raise ValueError("Only an options structure rests a profit target")
            if self.limit_price is not None:
                # A credit is taken profitably by buying it back for less; a debit by
                # selling it for more.
                if self.side is Side.SELL and self.profit_target >= self.limit_price:
                    raise ValueError(
                        f"profit_target {self.profit_target} must be below the credit {self.limit_price}"
                    )
                if self.side is Side.BUY and self.profit_target <= self.limit_price:
                    raise ValueError(
                        f"profit_target {self.profit_target} must be above the debit {self.limit_price}"
                    )


@dataclass(frozen=True)
class CloseStructure:
    """Close what is still open of the structure opened by ``entry_order_id``."""

    entry_order_id: str
    reason: str
    command_id: str
    limit_price: Decimal | None = None  # None closes at market

    def __post_init__(self) -> None:
        _require(self.entry_order_id, "entry_order_id")
        _require(self.reason, "reason")
        _require(self.command_id, "command_id")
        _positive(self.limit_price, "limit_price")


@dataclass(frozen=True)
class CloseHolding:
    """Sell (or cover) ``quantity`` shares held outside any structure, at market."""

    instrument: Equity
    quantity: Decimal
    reason: str
    command_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.instrument, Equity):
            raise ValueError("CloseHolding trades shares; close an option through its structure")
        _whole(self.quantity, "quantity")
        _require(self.reason, "reason")
        _require(self.command_id, "command_id")


OptionAction = OptionIntent | CloseStructure | CloseHolding


@dataclass(frozen=True)
class StructureLeg:
    """One leg of an open structure: how it was entered and how much is still held."""

    contract: OptionContract
    side: Side  # the side it was entered on: SELL for a short leg
    ratio: int
    open_quantity: Decimal  # contracts still held for this structure


@dataclass(frozen=True)
class OpenStructure:
    """A structure with contracts still open, as folded from the ledger at one instant."""

    entry_order_id: str
    account_id: str
    command_id: str
    instrument: Instrument  # the entry order's contract or combo
    side: Side  # the entry's side: SELL collected a credit, BUY paid a debit
    legs: tuple[StructureLeg, ...]
    units: Decimal  # units entered less units closed by its own orders
    entry_price: Decimal  # net per unit in option points, as filled
    opened_at: datetime
    target_order_id: str | None  # the resting profit target, while it works
    target_price: Decimal | None
    closing_order_id: str | None  # a close order still working

    @property
    def underlying(self) -> str:
        return option_style(self.legs[0].contract.underlying).underlying

    @property
    def credit(self) -> bool:
        return self.side is Side.SELL

    @property
    def expiry(self):
        """The nearest expiry among the legs still open."""
        return min(leg.contract.expiry for leg in self.legs if leg.open_quantity > 0)

"""Reg-T margin model (E8a equities, O3 options; rules doc §4/§6.1/§11).

Initial requirement is 50% of market value, maintenance 25%; both are defaults a
per-symbol override table can replace (hard-to-borrow names, concentrated names).
Options are grouped per underlying into strategies and margined by
`option_margin` (LEAN's matching and formulas); shares a strategy uses (a covered
call's) are margined inside it, and only the rest as plain stock.
Pure functions over an `AccountState` — nothing is written to the ledger; the
daily snapshot module calls these at each session close and tracks the peak.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal

from trade_engine.domain.instruments import Combo, Equity, Instrument, OptionContract
from trade_engine.domain.option_roots import option_style
from trade_engine.ledger.state import AccountState
from trade_engine.metrics.option_margin import (
    OptionMarginError,
    StrategyMargin,
    margin_book,
)

ZERO = Decimal("0")

INITIAL_FRACTION = Decimal("0.50")
MAINTENANCE_FRACTION = Decimal("0.25")

Symbol = str


@dataclass(frozen=True)
class MarginOverride:
    """A per-symbol replacement of the Reg-T fractions."""

    initial: Decimal
    maintenance: Decimal

    def __post_init__(self) -> None:
        if not ZERO < self.maintenance <= self.initial <= Decimal("1"):
            raise ValueError(
                f"Override fractions must satisfy 0 < maintenance <= initial <= 1, "
                f"got initial={self.initial}, maintenance={self.maintenance}"
            )


@dataclass(frozen=True)
class MarginRequirement:
    """Margin figures for one position at one mark."""

    symbol: Symbol
    quantity: Decimal
    mark: Decimal
    market_value: Decimal
    initial: Decimal
    maintenance: Decimal


@dataclass(frozen=True)
class AccountMargin:
    """Aggregated margin figures for one account at one session close."""

    equity: Decimal
    cash: Decimal
    market_value_long: Decimal
    market_value_short: Decimal
    gross_exposure: Decimal
    net_exposure: Decimal
    margin_initial: Decimal
    margin_maintenance: Decimal
    positions: tuple[MarginRequirement, ...]  # shares margined as plain stock
    strategies: tuple[StrategyMargin, ...] = ()  # option strategies, with any shares they use

    @property
    def margin_used(self) -> Decimal:
        """The binding overnight requirement is the maintenance figure."""
        return self.margin_maintenance

    @property
    def margin_available(self) -> Decimal:
        return self.equity - self.margin_maintenance


def margin_requirement(
    instrument: Instrument,
    quantity: Decimal,
    mark: Decimal,
    override: MarginOverride | None = None,
) -> MarginRequirement:
    """One position's Reg-T requirement; the signed quantity picks long/short value."""
    if quantity == ZERO:
        raise ValueError("margin_requirement needs a non-zero quantity")
    if mark <= ZERO:
        raise ValueError(f"Mark must be positive, got {mark} (I5)")
    market_value = quantity * mark
    magnitude = abs(market_value)
    initial_fraction = override.initial if override else INITIAL_FRACTION
    maintenance_fraction = override.maintenance if override else MAINTENANCE_FRACTION
    return MarginRequirement(
        symbol=instrument.symbol,
        quantity=quantity,
        mark=mark,
        market_value=market_value,
        initial=initial_fraction * magnitude,
        maintenance=maintenance_fraction * magnitude,
    )


def _whole(quantity: Decimal, what: str) -> int:
    if quantity != quantity.to_integral_value():
        raise OptionMarginError(f"{what} holds a fractional {quantity} contracts (I5)")
    return int(quantity)


def account_margin(
    state: AccountState,
    overrides: dict[Symbol, MarginOverride] | None = None,
    underlying_prices: Mapping[Symbol, Decimal] | None = None,
) -> AccountMargin:
    """Reg-T figures for one account from its folded state.

    Every open position must carry a mark at the session close — a missing mark
    refuses (I5); a guessed mark would silently understate the requirement. Options
    also need their underlying's price: from ``underlying_prices`` (an index has no
    shares to mark), else the underlying's own mark.
    """
    overrides = overrides or {}
    underlying_prices = underlying_prices or {}
    market_value_long = ZERO
    market_value_short = ZERO
    margin_initial = ZERO
    margin_maintenance = ZERO
    positions: list[MarginRequirement] = []
    strategies: list[StrategyMargin] = []
    shares: dict[Symbol, Decimal] = {}
    books: dict[Symbol, dict[OptionContract, int]] = {}

    for instrument, position in sorted(state.positions.items(), key=lambda kv: kv[0].symbol):
        if position.is_flat:
            continue
        mark = state.marks.get(instrument)
        if mark is None or mark <= ZERO:
            raise ValueError(f"No session-close mark for open position {instrument.symbol} (I5)")
        if isinstance(instrument, Combo):
            raise OptionMarginError(f"Combo position {instrument.symbol} must be held per leg (I6)")
        value = position.quantity * mark * instrument.multiplier
        if value > ZERO:
            market_value_long += value
        else:
            market_value_short += value
        if isinstance(instrument, OptionContract):
            underlying = option_style(instrument.underlying).underlying
            books.setdefault(underlying, {})[instrument] = _whole(position.quantity, instrument.occ.strip())
        else:
            shares[instrument.symbol] = position.quantity

    for underlying, book in sorted(books.items()):
        price = underlying_prices.get(underlying)
        if price is None:
            price = state.marks.get(Equity(underlying))
        if price is None or price <= ZERO:
            raise OptionMarginError(f"No underlying price for {underlying} options (I5)")
        override = overrides.get(underlying)
        matched, left = margin_book(
            underlying,
            book,
            shares.get(underlying, ZERO),
            price,
            state.marks,
            override.initial if override else INITIAL_FRACTION,
            override.maintenance if override else MAINTENANCE_FRACTION,
        )
        for figures in matched:
            margin_initial += figures.initial
            margin_maintenance += figures.maintenance
            strategies.append(figures)
        if underlying in shares:
            shares[underlying] = left

    for symbol, quantity in sorted(shares.items()):
        if quantity == ZERO:
            continue
        instrument = Equity(symbol)
        requirement = margin_requirement(instrument, quantity, state.marks[instrument], overrides.get(symbol))
        margin_initial += requirement.initial
        margin_maintenance += requirement.maintenance
        positions.append(requirement)

    equity = state.cash + market_value_long + market_value_short
    return AccountMargin(
        equity=equity,
        cash=state.cash,
        market_value_long=market_value_long,
        market_value_short=market_value_short,
        gross_exposure=market_value_long + abs(market_value_short),
        net_exposure=market_value_long + market_value_short,
        margin_initial=margin_initial,
        margin_maintenance=margin_maintenance,
        positions=tuple(positions),
        strategies=tuple(strategies),
    )

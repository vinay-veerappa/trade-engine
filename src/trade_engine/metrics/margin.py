"""Reg-T equity margin model (E8a, rules doc §4/§11).

Initial requirement is 50% of market value, maintenance 25%; both are defaults a
per-symbol override table can replace (hard-to-borrow names, concentrated names).
Pure functions over an `AccountState` — nothing is written to the ledger; the
daily snapshot module calls these at each session close and tracks the peak.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from trade_engine.domain.instruments import Instrument
from trade_engine.ledger.state import AccountState

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
    positions: tuple[MarginRequirement, ...]

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


def account_margin(
    state: AccountState,
    overrides: dict[Symbol, MarginOverride] | None = None,
) -> AccountMargin:
    """Reg-T figures for one account from its folded state.

    Every open position must carry a mark at the session close — a missing mark
    refuses (I5); a guessed mark would silently understate the requirement.
    """
    overrides = overrides or {}
    market_value_long = ZERO
    market_value_short = ZERO
    margin_initial = ZERO
    margin_maintenance = ZERO
    positions: list[MarginRequirement] = []
    for instrument, position in sorted(state.positions.items(), key=lambda kv: kv[0].symbol):
        if position.is_flat:
            continue
        mark = state.marks.get(instrument)
        if mark is None or mark <= ZERO:
            raise ValueError(f"No session-close mark for open position {instrument.symbol} (I5)")
        requirement = margin_requirement(
            instrument,
            position.quantity,
            mark,
            overrides.get(instrument.symbol),
        )
        if requirement.market_value > ZERO:
            market_value_long += requirement.market_value
        else:
            market_value_short += requirement.market_value
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
    )
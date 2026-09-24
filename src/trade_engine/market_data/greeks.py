"""Implied volatility and greeks, Black-Scholes-Merton via vollib (O1, Architecture §5).

Time runs from the quote to the contract's settlement instant (the open for AM-settled
roots, the close for PM) in calendar years of 365 days; ``rate`` and ``dividend_yield``
are annual continuous fractions taken from the chain snapshot the price came from, never
defaults (I5). American options are priced as European: the early-exercise premium is
left out, so a deep in-the-money American put's model value runs a little low. The
vendor's published greeks, where a quote carries them, are the ones to trade on; these
are for a quote without them and for pricing a contract away from its quote.
"""

from __future__ import annotations

import math
from datetime import datetime
from decimal import Decimal

from vollib.black_scholes_merton import black_scholes_merton
from vollib.black_scholes_merton.greeks import analytical
from vollib.black_scholes_merton.implied_volatility import implied_volatility
from vollib.helpers.exceptions import PriceIsAboveMaximum, PriceIsBelowIntrinsic
from vollib.lets_be_rational import AboveMaximumException, BelowIntrinsicException

from trade_engine.domain.instruments import OptionContract, OptionRight
from trade_engine.domain.option_roots import settlement_instant
from trade_engine.interfaces.market_data import Greeks

_YEAR_SECONDS = 365.0 * 24 * 60 * 60


class GreeksUnavailable(ValueError):
    """The model cannot answer for this input; the reason says why (I5)."""


def years_to_settlement(contract: OptionContract, as_of: datetime, calendar) -> float:
    """Calendar years from ``as_of`` to the contract's settlement instant; none left refuses."""
    if as_of.tzinfo is None or as_of.tzinfo.utcoffset(as_of) is None:
        raise ValueError("as_of must be timezone-aware (I7)")
    remaining = (settlement_instant(contract, calendar) - as_of).total_seconds()
    if remaining <= 0:
        raise GreeksUnavailable(f"{contract.occ} has settled by {as_of.isoformat()}")
    return remaining / _YEAR_SECONDS


def _inputs(contract, underlying_price, rate, dividend_yield) -> tuple[str, float, float, float, float]:
    for name, value in (("underlying_price", underlying_price), ("rate", rate), ("dividend_yield", dividend_yield)):
        if value is None:
            raise GreeksUnavailable(f"No {name} for {contract.occ} (I5)")
    spot = float(underlying_price)
    if not math.isfinite(spot) or spot <= 0:
        raise GreeksUnavailable(f"Underlying price {underlying_price} is not positive (I5)")
    flag = "c" if contract.right is OptionRight.CALL else "p"
    return flag, spot, float(contract.strike), float(rate), float(dividend_yield)


def model_price(
    contract: OptionContract,
    sigma: float,
    underlying_price: Decimal,
    as_of: datetime,
    rate: Decimal,
    dividend_yield: Decimal,
    calendar,
) -> float:
    """Theoretical premium per share at volatility ``sigma``."""
    flag, spot, strike, r, q = _inputs(contract, underlying_price, rate, dividend_yield)
    if not math.isfinite(sigma) or sigma <= 0:
        raise GreeksUnavailable(f"Volatility {sigma} is not positive")
    t = years_to_settlement(contract, as_of, calendar)
    return float(black_scholes_merton(flag, spot, strike, t, r, sigma, q))


def implied_vol(
    contract: OptionContract,
    price: Decimal,
    underlying_price: Decimal,
    as_of: datetime,
    rate: Decimal,
    dividend_yield: Decimal,
    calendar,
) -> float:
    """The volatility that prices the contract at ``price``; a price no volatility reaches refuses."""
    flag, spot, strike, r, q = _inputs(contract, underlying_price, rate, dividend_yield)
    premium = float(price)
    if not math.isfinite(premium) or premium <= 0:
        raise GreeksUnavailable(f"Price {price} for {contract.occ} is not positive")
    t = years_to_settlement(contract, as_of, calendar)
    # vollib signals the two bounds with its own exceptions or, depending on the solver
    # path, lets_be_rational's; both mean no volatility gives this price.
    try:
        sigma = float(implied_volatility(premium, spot, strike, t, r, q, flag))
    except (PriceIsBelowIntrinsic, BelowIntrinsicException) as err:
        raise GreeksUnavailable(f"{contract.occ} price {price} is below its discounted intrinsic value") from err
    except (PriceIsAboveMaximum, AboveMaximumException) as err:
        raise GreeksUnavailable(f"{contract.occ} price {price} is above what any volatility gives") from err
    if not math.isfinite(sigma) or sigma <= 0:
        raise GreeksUnavailable(f"No implied volatility for {contract.occ} at {price}")
    return sigma


def model_greeks(
    contract: OptionContract,
    sigma: float,
    underlying_price: Decimal,
    as_of: datetime,
    rate: Decimal,
    dividend_yield: Decimal,
    calendar,
) -> Greeks:
    """Greeks at volatility ``sigma``: theta per calendar day, vega and rho per point."""
    flag, spot, strike, r, q = _inputs(contract, underlying_price, rate, dividend_yield)
    if not math.isfinite(sigma) or sigma <= 0:
        raise GreeksUnavailable(f"Volatility {sigma} is not positive")
    t = years_to_settlement(contract, as_of, calendar)
    return Greeks(
        delta=float(analytical.delta(flag, spot, strike, t, r, sigma, q)),
        gamma=float(analytical.gamma(flag, spot, strike, t, r, sigma, q)),
        theta=float(analytical.theta(flag, spot, strike, t, r, sigma, q)),
        vega=float(analytical.vega(flag, spot, strike, t, r, sigma, q)),
        rho=float(analytical.rho(flag, spot, strike, t, r, sigma, q)),
        source="model",
    )

"""Instrument definitions and OCC option symbology."""

from __future__ import annotations

import decimal
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Protocol, runtime_checkable

import trade_engine_rs as _rs

# The validation and OCC symbology rules live in Rust (`te_core::options`,
# docs/RUST_PORT.md P1e). The classes below are data carriers: they hand `trade_engine_rs`
# plain values and build the answer. No rule may be written here: that would be a second
# reader.


class OptionRight(StrEnum):
    """Option contract right: Call or Put."""

    CALL = "C"
    PUT = "P"


class Side(StrEnum):
    """Trading side."""

    BUY = "BUY"
    SELL = "SELL"


class UnresolvableInstrumentError(Exception):
    """Raised when an instrument string cannot be resolved to a domain object (I6)."""


def rs_call(fn, *args):
    """Call into `trade_engine_rs`, raising the exception type the rule refused with.

    Rust refuses with ``ValueError(kind, message)``; the kind is what callers catch by.
    """
    try:
        return fn(*args)
    except ValueError as err:
        if len(err.args) != 2:
            raise
        kind, message = err.args
        if kind == "unresolvable":
            raise UnresolvableInstrumentError(message) from None
        if kind == "invalid_operation":
            raise decimal.InvalidOperation([decimal.InvalidOperation]) from None
        if kind == "overflow":
            raise decimal.Overflow([decimal.Overflow]) from None
        raise ValueError(message) from None


def decimal_args(d: Decimal) -> tuple:
    """A Decimal as Rust takes it: negative, digits, exponent, special code, str()."""
    sign, digits, exponent = d.as_tuple()
    special = exponent if isinstance(exponent, str) else ""
    return (
        bool(sign),
        "".join(map(str, digits)) or "0",
        0 if special else exponent,
        special,
        str(d),
    )


def contract_args(c: OptionContract) -> tuple:
    """An OptionContract as Rust takes it: underlying, expiry y/m/d, right, strike."""
    return (c.underlying, c.expiry.year, c.expiry.month, c.expiry.day, c.right.value, *decimal_args(c.strike))


def money(x: Decimal | int) -> str:
    """A money value as text for Rust (D6). Decimal and int only, as the pre-port
    arithmetic accepted without a TypeError."""
    if isinstance(x, bool) or not isinstance(x, (Decimal, int)):
        raise TypeError(f"Money must be Decimal or int, got {type(x).__name__}")
    return str(x)


class Instrument:
    """Base class for all financial instruments."""

    @property
    def symbol(self) -> str:
        raise NotImplementedError

    @property
    def multiplier(self) -> int:
        return 1


@dataclass(frozen=True)
class Equity(Instrument):
    """Equity (stock or ETF) instrument."""

    _symbol: str

    def __init__(self, symbol: str) -> None:
        sym = rs_call(_rs.option_equity_symbol, symbol if isinstance(symbol, str) else None)
        object.__setattr__(self, "_symbol", sym)

    @property
    def symbol(self) -> str:
        return self._symbol

    def __repr__(self) -> str:
        return f"Equity(symbol='{self._symbol}')"


@dataclass(frozen=True)
class OptionContract(Instrument):
    """Equity or Index Option contract with OCC symbology."""

    underlying: str
    expiry: date
    strike: Decimal
    right: OptionRight
    multiplier: int = 100

    def __post_init__(self) -> None:
        und = rs_call(_rs.option_validate_underlying, self.underlying)

        # Ensure strike is Decimal
        if not isinstance(self.strike, Decimal):
            object.__setattr__(self, "strike", Decimal(str(self.strike)))

        multiplier = self.multiplier
        right = rs_call(
            _rs.option_validate_rest,
            decimal_args(self.strike),
            str(int(multiplier)) if isinstance(multiplier, int) else None,
            str(multiplier),
            self.right if isinstance(self.right, str) else None,
            f"{self.right}",
        )
        if not isinstance(self.right, OptionRight):
            object.__setattr__(self, "right", OptionRight(right))

        object.__setattr__(self, "underlying", und)

    @property
    def symbol(self) -> str:
        return self.to_occ()

    @property
    def occ(self) -> str:
        """Returns the canonical 21-character OCC symbol."""
        return self.to_occ()

    def to_occ(self) -> str:
        """Build the standard 21-character OCC symbol: root padded to 6, YYMMDD, C or P,
        strike in thousandths to 8 digits."""
        return rs_call(_rs.option_to_occ, contract_args(self))

    @classmethod
    def from_occ(cls, occ_str: str, multiplier: int = 100) -> OptionContract:
        """Parse an OCC symbol (21-character canonical or compact form) into an OptionContract.

        Canonical: 'AAPL  260918C00150000' (21 chars)
        Compact:   'AAPL260918C00150000' (variable root length)
        """
        root, year, month, day, right, strike = rs_call(
            _rs.option_parse_occ, occ_str if isinstance(occ_str, str) else None
        )
        return cls(
            underlying=root,
            expiry=date(year, month, day),
            strike=Decimal(strike),
            right=OptionRight(right),
            multiplier=multiplier,
        )


@dataclass(frozen=True)
class ComboLeg:
    """A single leg in a multi-leg combo order or position."""

    contract: OptionContract | Equity
    ratio: int
    side: Side

    def __post_init__(self) -> None:
        if not isinstance(self.contract, (OptionContract, Equity)):
            raise ValueError(f"Combo leg contract must be OptionContract or Equity, got {type(self.contract)}")
        if self.ratio <= 0:
            raise ValueError(f"Combo leg ratio must be positive, got {self.ratio}")
        if not isinstance(self.side, Side):
            raise ValueError(f"Invalid side {self.side}")


@dataclass(frozen=True)
class Combo(Instrument):
    """Multi-leg option combo instrument (spread, straddle, condor, etc.)."""

    legs: tuple[ComboLeg, ...]

    def __init__(self, legs: tuple[ComboLeg, ...] | list[ComboLeg]) -> None:
        legs_tuple = tuple(legs)
        if not legs_tuple:
            raise ValueError("Combo must have at least one leg")
        object.__setattr__(self, "legs", legs_tuple)

    @property
    def multiplier(self) -> int:
        """Uniform leg multiplier (I6).

        Stock + option combos (buy-write, collar) have mixed multipliers and no single
        multiplier; they must be valued per leg, so asking for one refuses (I5).
        """
        mults = {leg.contract.multiplier for leg in self.legs}
        if len(mults) != 1:
            raise ValueError(f"Combo has mixed leg multipliers {sorted(mults)}; value it per leg (I6)")
        return self.legs[0].contract.multiplier

    @property
    def symbol(self) -> str:
        return "/".join(
            f"{leg.side.value}:{leg.ratio}x{leg.contract.symbol.strip()}" for leg in self.legs
        )


@runtime_checkable
class InstrumentResolver(Protocol):
    """Protocol for resolving instrument strings into typed domain objects (I6)."""

    def resolve(self, symbol: str) -> Instrument:
        """Resolve a symbol string to an Instrument.

        Raises UnresolvableInstrumentError if unresolvable.
        """
        ...

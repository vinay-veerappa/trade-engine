"""Instrument definitions and OCC option symbology."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Protocol, runtime_checkable


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
        if not symbol or not isinstance(symbol, str):
            raise ValueError("Equity symbol must be non-empty string")
        sym = symbol.strip().upper()
        if not sym:
            raise ValueError("Equity symbol must be non-empty")
        if len(sym) > 10:
            raise ValueError(f"Equity symbol exceeds maximum length of 10 characters: '{sym}'")
        if not sym.isalnum():
            raise ValueError(f"Equity symbol must be alphanumeric without slashes or spaces, got '{sym}'")
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
        und = self.underlying.strip().upper()
        if not und:
            raise ValueError("Option underlying must be non-empty")
        if len(und) > 6:
            raise ValueError(f"Option underlying must be at most 6 characters, got '{und}' (length {len(und)})")
        if not und.isalnum():
            raise ValueError(f"Option underlying must be alphanumeric, got '{und}'")

        # Ensure strike is Decimal
        if not isinstance(self.strike, Decimal):
            object.__setattr__(self, "strike", Decimal(str(self.strike)))

        if self.strike <= Decimal("0"):
            raise ValueError(f"Strike must be positive, got {self.strike}")

        if (self.strike * Decimal("1000")) != (self.strike * Decimal("1000")).to_integral_value():
            raise ValueError(f"Strike cannot have more than 3 decimal places (thousandths), got {self.strike} (I5)")

        strike_millis = int((self.strike * Decimal("1000")).to_integral_value())
        if strike_millis <= 0 or strike_millis > 99999999:
            raise ValueError(f"Strike {self.strike} out of bounds for OCC representation")

        if self.multiplier <= 0:
            raise ValueError(f"Multiplier must be positive, got {self.multiplier}")
        if not isinstance(self.right, OptionRight):
            if isinstance(self.right, str) and self.right.upper() in ("C", "CALL"):
                object.__setattr__(self, "right", OptionRight.CALL)
            elif isinstance(self.right, str) and self.right.upper() in ("P", "PUT"):
                object.__setattr__(self, "right", OptionRight.PUT)
            else:
                raise ValueError(f"Invalid option right: {self.right}")

        object.__setattr__(self, "underlying", und)

    @property
    def symbol(self) -> str:
        return self.to_occ()

    @property
    def occ(self) -> str:
        """Returns the canonical 21-character OCC symbol."""
        return self.to_occ()

    def to_occ(self) -> str:
        """Build standard 21-character OCC symbol:

        - Root symbol (up to 6 chars, right padded with spaces)
        - Expiration YYMMDD (6 chars)
        - Type C or P (1 char)
        - Strike price * 1000 (8 digits, zero-padded)
        """
        strike_millis = int((self.strike * Decimal("1000")).to_integral_value())
        if strike_millis <= 0 or strike_millis > 99999999:
            raise ValueError(f"Strike {self.strike} out of bounds for OCC representation")

        exp_str = self.expiry.strftime("%y%m%d")
        return f"{self.underlying:<6}{exp_str}{self.right.value}{strike_millis:08d}"

    @classmethod
    def from_occ(cls, occ_str: str, multiplier: int = 100) -> OptionContract:
        """Parse an OCC symbol (21-character canonical or compact form) into an OptionContract.

        Canonical: 'AAPL  260918C00150000' (21 chars)
        Compact:   'AAPL260918C00150000' (variable root length)
        """
        if not occ_str or not isinstance(occ_str, str):
            raise ValueError("OCC symbol must be a non-empty string")

        raw = occ_str.strip()
        # Regex matching:
        # Group 1: 1 to 6 characters root ticker
        # Group 2: 6 digits YYMMDD
        # Group 3: C or P
        # Group 4: 8 digits strike
        pattern = re.compile(r"^([A-Za-z0-9]{1,6})\s*(\d{6})([CPcp])(\d{8})$")
        match = pattern.match(raw)
        if not match:
            raise ValueError(f"Invalid OCC option symbol format: '{occ_str}'")

        root, exp_str, right_char, strike_str = match.groups()
        root = root.strip().upper()
        if not root:
            raise ValueError(f"Empty root symbol in OCC string '{occ_str}'")

        try:
            exp_date = datetime.strptime(exp_str, "%y%m%d").date()
        except ValueError as err:
            raise ValueError(f"Invalid expiration date '{exp_str}' in OCC symbol: {err}") from err

        right = OptionRight.CALL if right_char.upper() == "C" else OptionRight.PUT
        strike = Decimal(int(strike_str)) / Decimal("1000")
        if strike <= Decimal("0"):
            raise ValueError(f"Strike parsed from OCC symbol must be positive, got {strike}")

        return cls(
            underlying=root,
            expiry=exp_date,
            strike=strike,
            right=right,
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
        mults = {leg.contract.multiplier for leg in legs_tuple}
        if len(mults) != 1:
            raise ValueError(f"Combo has mixed leg multipliers: {mults} (I6)")
        object.__setattr__(self, "legs", legs_tuple)

    @property
    def multiplier(self) -> int:
        """Derive multiplier from legs; all legs guaranteed uniform (I6)."""
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

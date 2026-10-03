"""Option strategy margin (O3, rules doc §6.1, architecture §4.10).

The rules live in Rust (`te_core::margin::option`, docs/RUST_PORT.md P1c): LEAN's
strategy matching, its formulas, the best-grouping search, diagonals and current
marks. This module only turns `OptionContract` objects into the table `trade_engine_rs`
takes and the answer back into the dataclasses below. No margin rule may be written
here: that would be a second reader.

Crossing the boundary: decimals travel as strings, a book as a table of contracts
plus (index, quantity) pairs. The option-roots table (`domain/option_roots.py`) stays
in Python; its answer for each contract rides along, and a refusal of it is raised
only if a rule reads it. A refusal from Rust comes back as a ValueError whose args
are (kind, message) and is re-raised here as the exception type `kind` names.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import trade_engine_rs as _rs

from trade_engine.domain.instruments import OptionContract, OptionRight, UnresolvableInstrumentError
from trade_engine.domain.option_roots import Settlement, option_style

ZERO = Decimal("0")


class OptionMarginError(ValueError):
    """A book that cannot be margined without guessing (I5, I6)."""


_CONSTANTS = json.loads(_rs.margin_constants())
_NAMES = _CONSTANTS["names"]

NAKED_FLOOR = Decimal(_CONSTANTS["naked_floor"])
EQUITY_OTM_FRACTION = Decimal(_CONSTANTS["equity_otm_fraction"])
INDEX_OTM_FRACTION = Decimal(_CONSTANTS["index_otm_fraction"])
COVERED_CALL_CALL_FRACTION = Decimal(_CONSTANTS["covered_call_call_fraction"])
COLLAR_CALL_FRACTION = Decimal(_CONSTANTS["collar_call_fraction"])

NAKED_CALL = _NAMES["NAKED_CALL"]
NAKED_PUT = _NAMES["NAKED_PUT"]
COVERED_CALL = _NAMES["COVERED_CALL"]
PROTECTIVE_CALL = _NAMES["PROTECTIVE_CALL"]
COVERED_PUT = _NAMES["COVERED_PUT"]
PROTECTIVE_PUT = _NAMES["PROTECTIVE_PUT"]
PROTECTIVE_COLLAR = _NAMES["PROTECTIVE_COLLAR"]
BEAR_CALL_SPREAD = _NAMES["BEAR_CALL_SPREAD"]
BEAR_PUT_SPREAD = _NAMES["BEAR_PUT_SPREAD"]
BULL_CALL_SPREAD = _NAMES["BULL_CALL_SPREAD"]
BULL_PUT_SPREAD = _NAMES["BULL_PUT_SPREAD"]
CALL_CALENDAR_SPREAD = _NAMES["CALL_CALENDAR_SPREAD"]
SHORT_CALL_CALENDAR_SPREAD = _NAMES["SHORT_CALL_CALENDAR_SPREAD"]
PUT_CALENDAR_SPREAD = _NAMES["PUT_CALENDAR_SPREAD"]
SHORT_PUT_CALENDAR_SPREAD = _NAMES["SHORT_PUT_CALENDAR_SPREAD"]
CALL_DIAGONAL_SPREAD = _NAMES["CALL_DIAGONAL_SPREAD"]
PUT_DIAGONAL_SPREAD = _NAMES["PUT_DIAGONAL_SPREAD"]
LONG_CALL = _NAMES["LONG_CALL"]
LONG_PUT = _NAMES["LONG_PUT"]

# The number of distinct books the grouping search visits before it keeps LEAN's
# grouping. Read at every call, so a test can lower it.
SEARCH_LIMIT: int = _CONSTANTS["search_limit"]


@dataclass(frozen=True)
class LegDefinition:
    right: OptionRight
    quantity: int  # signed contracts per unit of the strategy
    predicates: tuple[str, ...] = ()  # names Rust understands, e.g. "same_expiry"


@dataclass(frozen=True)
class StrategyDefinition:
    name: str
    underlying_lots: int  # signed lots of the underlying per unit
    legs: tuple[LegDefinition, ...]

    @property
    def leg_count(self) -> int:
        return len(self.legs) + (0 if self.underlying_lots == 0 else 1)


def _definition(raw: Mapping[str, Any]) -> StrategyDefinition:
    return StrategyDefinition(
        name=raw["name"],
        underlying_lots=raw["underlying_lots"],
        legs=tuple(LegDefinition(OptionRight(leg["right"]), leg["quantity"], tuple(leg["predicates"])) for leg in raw["legs"]),
    )


# In LEAN's declaration order (written once, in Rust).
DEFINITIONS: tuple[StrategyDefinition, ...] = tuple(_definition(raw) for raw in json.loads(_rs.margin_definitions()))


@dataclass(frozen=True)
class MatchedStrategy:
    """One strategy found in a book: its legs in signed contracts and its underlying in
    signed lots, both already scaled by ``quantity`` units."""

    name: str
    quantity: int
    legs: tuple[tuple[OptionContract, int], ...]
    underlying_lots: int = 0


@dataclass(frozen=True)
class StrategyMargin:
    """One matched strategy's requirements at one set of prices.

    ``cash_secured`` is what an account without margin must hold for the option legs:
    the strike for a naked put, the width for a credit spread, nothing when shares or a
    long option cover the short. It is None where cash cannot secure the position at all
    (a naked call, anything short the shares).

    ``net_of_credit`` is the maintenance requirement less the credit taken in when the
    position was opened, what a broker shows as the buying power it uses (rules doc
    §6.1: a credit spread is width x 100 - credit). A debit strategy took no credit, so
    it equals the maintenance. It is None when an entry price is not known.
    """

    name: str
    underlying: str
    quantity: int
    legs: tuple[tuple[OptionContract, int], ...]
    shares: Decimal
    initial: Decimal
    maintenance: Decimal
    cash_secured: Decimal | None
    net_of_credit: Decimal | None = None


# --- the boundary -----------------------------------------------------------------------


def _s(value: Decimal) -> str:
    """A decimal as plain digits (never 1E+2), which is all Rust reads."""
    return format(value, "f")


def _opt(value: Decimal | None) -> str | None:
    return None if value is None else _s(value)


def _raise(err: ValueError) -> None:
    """Re-raise a refusal from Rust as the Python exception type it names."""
    if len(err.args) == 2 and err.args[0] in ("value", "option", "unresolvable"):
        kind, message = err.args
        if kind == "option":
            raise OptionMarginError(message) from None
        if kind == "unresolvable":
            raise UnresolvableInstrumentError(message) from None
        raise ValueError(message) from None
    raise err


def _rust(fn: Callable[..., Any], *args: Any) -> Any:
    try:
        return fn(*args)
    except ValueError as err:
        _raise(err)


def _style(contract: OptionContract) -> dict[str, Any]:
    try:
        style = option_style(contract.underlying)
    except UnresolvableInstrumentError as err:
        return {"underlying": None, "index": None, "style_error": str(err)}
    return {"underlying": style.underlying, "index": style.settlement is Settlement.CASH, "style_error": None}


def _wire_contract(contract: OptionContract) -> dict[str, Any]:
    return {
        "root": contract.underlying,
        "expiry": contract.expiry.isoformat(),
        "strike": _s(contract.strike),
        "right": contract.right.value,
        "multiplier": contract.multiplier,
        "occ": contract.occ.strip(),
        **_style(contract),
    }


class _Table:
    """The contracts of one call, each described once and addressed by index."""

    def __init__(self, contracts: Sequence[OptionContract] = ()) -> None:
        self.contracts: list[OptionContract] = []
        self._index: dict[OptionContract, int] = {}
        for contract in contracts:
            self.add(contract)

    def add(self, contract: OptionContract) -> int:
        if contract not in self._index:
            self._index[contract] = len(self.contracts)
            self.contracts.append(contract)
        return self._index[contract]

    def wire(self) -> list[dict[str, Any]]:
        return [_wire_contract(c) for c in self.contracts]

    def marks(self, marks: Mapping[OptionContract, Decimal]) -> list[str | None]:
        return [_opt(marks.get(c)) for c in self.contracts]

    def legs(self, legs: Sequence[Sequence[int]]) -> tuple[tuple[OptionContract, int], ...]:
        return tuple((self.contracts[c], q) for c, q in legs)


def _wire_definitions(definitions: Sequence[StrategyDefinition]) -> list[dict[str, Any]] | None:
    if definitions is DEFINITIONS:
        return None
    return [
        {
            "name": d.name,
            "underlying_lots": d.underlying_lots,
            "legs": [{"right": leg.right.value, "quantity": leg.quantity, "predicates": list(leg.predicates)} for leg in d.legs],
        }
        for d in definitions
    ]


def _wire_book(table: _Table, book: Mapping[OptionContract, int]) -> list[list[int]]:
    return [[table.add(contract), quantity] for contract, quantity in book.items()]


def strategy_margin_from(table: _Table, raw: Mapping[str, Any]) -> StrategyMargin:
    return StrategyMargin(
        name=raw["name"],
        underlying=raw["underlying"],
        quantity=raw["quantity"],
        legs=table.legs(raw["legs"]),
        shares=Decimal(raw["shares"]),
        initial=Decimal(raw["initial"]),
        maintenance=Decimal(raw["maintenance"]),
        cash_secured=None if raw["cash_secured"] is None else Decimal(raw["cash_secured"]),
        net_of_credit=None if raw["net_of_credit"] is None else Decimal(raw["net_of_credit"]),
    )


# --- the public API ---------------------------------------------------------------------


def match_strategies(
    book: Mapping[OptionContract, int],
    lots: int = 0,
    definitions: Sequence[StrategyDefinition] = DEFINITIONS,
) -> tuple[tuple[MatchedStrategy, ...], int]:
    """Group one underlying's options (signed contracts) and lots of its shares.

    Returns the strategies, with any leftover long option as a ``Long Call``/``Long Put``,
    and the lots no strategy used. A short option nothing covers comes back naked.
    """
    table = _Table()
    request = {
        "book": _wire_book(table, book),
        "lots": lots,
        "definitions": _wire_definitions(definitions),
    }
    request["contracts"] = table.wire()
    answer = json.loads(_rust(_rs.margin_match, json.dumps(request)))
    matched = tuple(
        MatchedStrategy(m["name"], m["quantity"], table.legs(m["legs"]), m["underlying_lots"]) for m in answer["strategies"]
    )
    return matched, answer["left"]


def naked_margin(contract: OptionContract, contracts: int, underlying: Decimal, mark: Decimal) -> Decimal:
    """OptionMarginModel for ``contracts`` short (negative) contracts, initial and
    maintenance alike. A long option needs nothing."""
    request = {
        "contract": _wire_contract(contract),
        "contracts": contracts,
        "underlying": _s(underlying),
        "mark": _s(mark),
    }
    return Decimal(_rust(_rs.margin_naked, json.dumps(request)))


def strategy_margin(
    strategy: MatchedStrategy,
    underlying: str,
    underlying_price: Decimal,
    marks: Mapping[OptionContract, Decimal],
    initial_fraction: Decimal,
    maintenance_fraction: Decimal,
    entry_prices: Mapping[OptionContract, Decimal] | None = None,
) -> StrategyMargin:
    """Margin one matched strategy. Initial margin also carries the net premium when the
    strategy was bought for a debit; a credit adds nothing. ``entry_prices`` (per share,
    what each leg was opened at) gives ``net_of_credit``."""
    table = _Table(c for c, _ in strategy.legs)
    request = {
        "contracts": table.wire(),
        "strategy": {
            "name": strategy.name,
            "quantity": strategy.quantity,
            "legs": [[table.add(c), q] for c, q in strategy.legs],
            "underlying_lots": strategy.underlying_lots,
        },
        "underlying": underlying,
        "underlying_price": _s(underlying_price),
        "marks": table.marks(marks),
        "initial_fraction": _s(initial_fraction),
        "maintenance_fraction": _s(maintenance_fraction),
        "entries": None if entry_prices is None else table.marks(entry_prices),
    }
    return strategy_margin_from(table, json.loads(_rust(_rs.margin_strategy, json.dumps(request))))


def margin_book(
    underlying: str,
    book: Mapping[OptionContract, int],
    shares: Decimal,
    underlying_price: Decimal,
    marks: Mapping[OptionContract, Decimal],
    initial_fraction: Decimal,
    maintenance_fraction: Decimal,
    entry_prices: Mapping[OptionContract, Decimal] | None = None,
    definitions: Sequence[StrategyDefinition] = DEFINITIONS,
) -> tuple[tuple[StrategyMargin, ...], Decimal]:
    """Margin one underlying's options (signed contracts) with the account's signed
    ``shares`` of it. Every grouping is searched; LEAN's wins ties and stands when the
    book is too large to search (``SEARCH_LIMIT``). Returns the strategies and the
    shares none used."""
    table = _Table()
    pairs = _wire_book(table, book)
    request = {
        "underlying": underlying,
        "contracts": table.wire(),
        "book": pairs,
        "shares": _s(shares),
        "underlying_price": _s(underlying_price),
        "marks": table.marks(marks),
        "initial_fraction": _s(initial_fraction),
        "maintenance_fraction": _s(maintenance_fraction),
        "entries": None if entry_prices is None else table.marks(entry_prices),
        "definitions": _wire_definitions(definitions),
        "search_limit": SEARCH_LIMIT,
    }
    answer = json.loads(_rust(_rs.margin_book, json.dumps(request)))
    return tuple(strategy_margin_from(table, raw) for raw in answer["strategies"]), Decimal(answer["shares_left"])

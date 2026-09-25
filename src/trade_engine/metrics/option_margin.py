"""Option strategy margin (O3, rules doc §6.1, architecture §4.10).

A book of options on one underlying is first grouped into strategies, then each strategy
is margined as a unit. Both halves are ported from LEAN (Apache-2.0,
``Common/Securities/Option``, master as of 2026-09-24):

- **Matching** (``StrategyMatcher/OptionStrategyMatcher.cs``). Definitions are tried in
  descending order of leg count, each matched greedily as many times as it fits. LEAN
  then tries a second order, with the definitions that leave a short leg uncovered moved
  last, and keeps it if it covers more shorts. With the definitions below those are only
  the one-leg naked ones, which already come last, so the second order is the first one
  and is not ported.
- **Formulas** (``OptionStrategyPositionGroupBuyingPowerModel.cs`` and, for a lone short
  option, ``OptionMarginModel.cs``).

Only the definitions the ``OPT_*`` accounts can hold are ported: naked calls and puts,
the four verticals, covered and protective calls and puts, the collar and the calendars.
A book outside them falls back to smaller pieces (an iron condor is margined as two
verticals), which never charges less than LEAN would.

Three things here are not LEAN's:

- **Cheapest grouping.** LEAN keeps the first grouping its greedy pass finds, which can
  pair the wrong legs (see ``margin_book``). The grouping with the least margin is kept
  instead, and LEAN's stands on a tie, so this never charges more than LEAN.

- **Diagonals.** LEAN has no diagonal: its calendars need equal strikes, so a poor man's
  covered call (long LEAPS call, short near call at a higher strike) comes apart into a
  naked call plus a long call. ``Call Diagonal Spread`` and ``Put Diagonal Spread`` cover a
  short with a long of the same right, a different strike and a later expiry, and are
  margined as a vertical: the strike difference when the long is further out of the
  money, nothing when it isn't. A long that expires *before* its short covers nothing.
- **Marks.** LEAN's maintenance margin for a naked option uses the premium it was sold
  for. Here both figures use the current mark, so a short put whose premium has tripled
  is charged on what it would cost to close.

Positions are enumerated in (expiry, strike, right, root) order, so a book always matches
the same way. LEAN enumerates hash sets and leaves the order unspecified.

Everything is pure. A price that is missing refuses (I5).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal

from trade_engine.domain.instruments import OptionContract, OptionRight
from trade_engine.domain.option_lifecycle import intrinsic
from trade_engine.domain.option_roots import Settlement, option_style

ZERO = Decimal("0")

# OptionMarginModel: a short option's floor is 10% of the strike (put) or of the
# underlying (call); the out-of-the-money test uses 20% of the underlying for equity
# options and 15% for index options.
NAKED_FLOOR = Decimal("0.10")
EQUITY_OTM_FRACTION = Decimal("0.20")
INDEX_OTM_FRACTION = Decimal("0.15")
# OptionStrategyPositionGroupBuyingPowerModel: covered call initial margin, which LEAN
# inferred from IB's actual requirements.
COVERED_CALL_CALL_FRACTION = Decimal("0.8")
# ...and the collar's call-side cap.
COLLAR_CALL_FRACTION = Decimal("0.25")

NAKED_CALL = "Naked Call"
NAKED_PUT = "Naked Put"
COVERED_CALL = "Covered Call"
PROTECTIVE_CALL = "Protective Call"
COVERED_PUT = "Covered Put"
PROTECTIVE_PUT = "Protective Put"
PROTECTIVE_COLLAR = "Protective Collar"
BEAR_CALL_SPREAD = "Bear Call Spread"
BEAR_PUT_SPREAD = "Bear Put Spread"
BULL_CALL_SPREAD = "Bull Call Spread"
BULL_PUT_SPREAD = "Bull Put Spread"
CALL_CALENDAR_SPREAD = "Call Calendar Spread"
SHORT_CALL_CALENDAR_SPREAD = "Short Call Calendar Spread"
PUT_CALENDAR_SPREAD = "Put Calendar Spread"
SHORT_PUT_CALENDAR_SPREAD = "Short Put Calendar Spread"
CALL_DIAGONAL_SPREAD = "Call Diagonal Spread"
PUT_DIAGONAL_SPREAD = "Put Diagonal Spread"
# Not a LEAN definition: LEAN matches a lone long option as an inverted naked
# definition, which carries no maintenance margin and pays its premium up front.
LONG_CALL = "Long Call"
LONG_PUT = "Long Put"

Predicate = Callable[[Sequence[OptionContract], OptionContract], bool]


class OptionMarginError(ValueError):
    """A book that cannot be margined without guessing (I5, I6)."""


@dataclass(frozen=True)
class LegDefinition:
    right: OptionRight
    quantity: int  # signed contracts per unit of the strategy
    predicates: tuple[Predicate, ...] = ()


@dataclass(frozen=True)
class StrategyDefinition:
    name: str
    underlying_lots: int  # signed lots of the underlying per unit
    legs: tuple[LegDefinition, ...]

    @property
    def leg_count(self) -> int:
        return len(self.legs) + (0 if self.underlying_lots == 0 else 1)


def _call(quantity: int, *predicates: Predicate) -> LegDefinition:
    return LegDefinition(OptionRight.CALL, quantity, predicates)


def _put(quantity: int, *predicates: Predicate) -> LegDefinition:
    return LegDefinition(OptionRight.PUT, quantity, predicates)


def _same_expiry(legs: Sequence[OptionContract], c: OptionContract) -> bool:
    return c.expiry == legs[0].expiry


def _later_expiry(legs: Sequence[OptionContract], c: OptionContract) -> bool:
    return c.expiry > legs[0].expiry


def _same_strike(legs: Sequence[OptionContract], c: OptionContract) -> bool:
    return c.strike == legs[0].strike


def _other_strike(legs: Sequence[OptionContract], c: OptionContract) -> bool:
    return c.strike != legs[0].strike


def _strike_above(legs: Sequence[OptionContract], c: OptionContract) -> bool:
    return c.strike > legs[0].strike


def _strike_below(legs: Sequence[OptionContract], c: OptionContract) -> bool:
    return c.strike < legs[0].strike


# In LEAN's declaration order, which breaks ties between definitions of equal leg count.
DEFINITIONS: tuple[StrategyDefinition, ...] = (
    StrategyDefinition(COVERED_CALL, 1, (_call(-1),)),
    StrategyDefinition(PROTECTIVE_CALL, -1, (_call(1),)),
    StrategyDefinition(COVERED_PUT, -1, (_put(-1),)),
    StrategyDefinition(PROTECTIVE_PUT, 1, (_put(1),)),
    StrategyDefinition(PROTECTIVE_COLLAR, 1, (_call(-1), _put(1, _strike_below, _same_expiry))),
    StrategyDefinition(NAKED_CALL, 0, (_call(-1),)),
    StrategyDefinition(NAKED_PUT, 0, (_put(-1),)),
    StrategyDefinition(BEAR_CALL_SPREAD, 0, (_call(-1), _call(1, _strike_above, _same_expiry))),
    StrategyDefinition(BEAR_PUT_SPREAD, 0, (_put(1), _put(-1, _strike_below, _same_expiry))),
    StrategyDefinition(BULL_CALL_SPREAD, 0, (_call(1), _call(-1, _strike_above, _same_expiry))),
    StrategyDefinition(BULL_PUT_SPREAD, 0, (_put(-1), _put(1, _strike_below, _same_expiry))),
    StrategyDefinition(CALL_CALENDAR_SPREAD, 0, (_call(-1), _call(1, _same_strike, _later_expiry))),
    StrategyDefinition(SHORT_CALL_CALENDAR_SPREAD, 0, (_call(1), _call(-1, _same_strike, _later_expiry))),
    StrategyDefinition(PUT_CALENDAR_SPREAD, 0, (_put(-1), _put(1, _same_strike, _later_expiry))),
    StrategyDefinition(SHORT_PUT_CALENDAR_SPREAD, 0, (_put(1), _put(-1, _same_strike, _later_expiry))),
    StrategyDefinition(CALL_DIAGONAL_SPREAD, 0, (_call(-1), _call(1, _other_strike, _later_expiry))),
    StrategyDefinition(PUT_DIAGONAL_SPREAD, 0, (_put(-1), _put(1, _other_strike, _later_expiry))),
)


@dataclass(frozen=True)
class MatchedStrategy:
    """One strategy found in a book: its legs in signed contracts and its underlying in
    signed lots, both already scaled by ``quantity`` units."""

    name: str
    quantity: int
    legs: tuple[tuple[OptionContract, int], ...]
    underlying_lots: int = 0


def _order(contract: OptionContract) -> tuple[object, ...]:
    return (contract.expiry, contract.strike, contract.right.value, contract.underlying)


def _matches(
    definition: StrategyDefinition,
    book: Mapping[OptionContract, int],
    lots: int,
) -> Iterator[MatchedStrategy]:
    """OptionStrategyDefinition.Match: every way ``definition`` fits the book, in
    enumeration order, each taken as many times as all its legs allow."""
    held = [c for c in sorted(book, key=_order) if book[c] != 0]
    if len(held) + (1 if lots != 0 else 0) < definition.leg_count:
        return
    units = None
    if definition.underlying_lots != 0:
        if (lots > 0) != (definition.underlying_lots > 0) or abs(lots) < abs(definition.underlying_lots):
            return
        units = abs(lots) // abs(definition.underlying_lots)

    def extend(chosen: list[OptionContract], units: int | None) -> Iterator[tuple[list[OptionContract], int]]:
        index = len(chosen)
        if index == len(definition.legs):
            if units:
                yield chosen, units
            return
        leg = definition.legs[index]
        for contract in held:
            if contract in chosen or contract.right is not leg.right:
                continue
            quantity = book[contract]
            if (quantity > 0) != (leg.quantity > 0):
                continue
            if not all(predicate(chosen, contract) for predicate in leg.predicates):
                continue
            fits = abs(quantity) // abs(leg.quantity)
            if fits == 0:
                continue
            yield from extend([*chosen, contract], fits if units is None else min(units, fits))

    for chosen, found in extend([], units):
        yield MatchedStrategy(
            name=definition.name,
            quantity=found,
            legs=tuple((c, leg.quantity * found) for c, leg in zip(chosen, definition.legs, strict=True)),
            underlying_lots=definition.underlying_lots * found,
        )


def _try_match(
    definition: StrategyDefinition,
    book: Mapping[OptionContract, int],
    lots: int,
) -> MatchedStrategy | None:
    """OptionStrategyDefinition.TryMatchOnce: the first match in enumeration order."""
    return next(_matches(definition, book, lots), None)


def _match_greedy(
    definitions: Sequence[StrategyDefinition],
    book: Mapping[OptionContract, int],
    lots: int,
) -> tuple[list[MatchedStrategy], dict[OptionContract, int], int]:
    remaining = dict(book)
    strategies: list[MatchedStrategy] = []
    for definition in definitions:
        while (match := _try_match(definition, remaining, lots)) is not None:
            for contract, quantity in match.legs:
                remaining[contract] -= quantity
            lots -= match.underlying_lots
            strategies.append(match)
        if lots == 0 and not any(remaining.values()):
            break
    return strategies, {c: q for c, q in remaining.items() if q != 0}, lots


def match_strategies(
    book: Mapping[OptionContract, int],
    lots: int = 0,
    definitions: Sequence[StrategyDefinition] = DEFINITIONS,
) -> tuple[tuple[MatchedStrategy, ...], int]:
    """Group one underlying's options (signed contracts) and lots of its shares.

    Returns the strategies, with any leftover long option as a ``Long Call``/``Long Put``,
    and the lots no strategy used. A short option nothing covers comes back naked.
    """
    by_legs = sorted(definitions, key=lambda d: -d.leg_count)  # stable: ties keep LEAN's order
    strategies, unmatched, left = _match_greedy(by_legs, book, lots)
    extra = []
    for contract in sorted(unmatched, key=_order):
        quantity = unmatched[contract]
        if quantity < 0:  # every naked definition is in DEFINITIONS; only a custom set gets here
            raise OptionMarginError(f"Short {contract.occ.strip()} matched no definition")
        extra.append(_long(contract, quantity))
    return tuple(strategies) + tuple(extra), left


def _long(contract: OptionContract, quantity: int) -> MatchedStrategy:
    name = LONG_CALL if contract.right is OptionRight.CALL else LONG_PUT
    return MatchedStrategy(name, quantity, ((contract, quantity),))


# LEAN bounds its matcher by time and solution count; this bounds the search by the
# number of distinct books it visits. A book too large to search keeps LEAN's grouping.
SEARCH_LIMIT = 20_000

Cost = tuple[Decimal, Decimal]  # (maintenance, initial), compared in that order
Items = tuple[tuple[OptionContract, int], ...]
Grouping = tuple[Cost, tuple[MatchedStrategy, ...], int]


class _TooLarge(Exception):
    pass


def _items(book: Mapping[OptionContract, int]) -> Items:
    return tuple((c, book[c]) for c in sorted(book, key=_order) if book[c] != 0)


def _cheapest(
    book: Mapping[OptionContract, int],
    lots: int,
    definitions: Sequence[StrategyDefinition],
    strategy_cost: Callable[[MatchedStrategy], Cost],
    lots_cost: Callable[[int], Cost],
) -> Grouping | None:
    """The cheapest grouping of the book, or None if it is too large to search. Every
    grouping places the first remaining contract somewhere, so each step only tries the
    strategies that include it."""
    memo: dict[tuple[Items, int], Grouping | None] = {}

    def best(items: Items, lots: int) -> Grouping | None:
        key = (items, lots)
        if key in memo:
            return memo[key]
        if len(memo) >= SEARCH_LIMIT:
            raise _TooLarge
        found: Grouping | None = None
        if not items:
            found = (lots_cost(lots), (), lots)
        else:
            first, quantity = items[0]
            current = dict(items)
            candidates = [
                match
                for definition in definitions
                for match in _matches(definition, current, lots)
                if any(contract == first for contract, _ in match.legs)
            ]
            if quantity > 0:
                candidates.append(_long(first, quantity))
            for match in candidates:
                remaining = dict(current)
                for contract, taken in match.legs:
                    remaining[contract] -= taken
                rest = best(_items(remaining), lots - match.underlying_lots)
                if rest is None:
                    continue
                own = strategy_cost(match)
                cost = (own[0] + rest[0][0], own[1] + rest[0][1])
                if found is None or cost < found[0]:
                    found = (cost, (match, *rest[1]), rest[2])
        memo[key] = found
        return found

    try:
        return best(_items(book), lots)
    except _TooLarge:
        return None


# --- formulas ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StrategyMargin:
    """One matched strategy's requirements at one set of prices.

    ``cash_secured`` is what an account without margin must hold for the option legs:
    the strike for a naked put, the width for a credit spread, nothing when shares or a
    long option cover the short. It is None where cash cannot secure the position at all
    (a naked call, anything short the shares).
    """

    name: str
    underlying: str
    quantity: int
    legs: tuple[tuple[OptionContract, int], ...]
    shares: Decimal
    initial: Decimal
    maintenance: Decimal
    cash_secured: Decimal | None


@dataclass(frozen=True)
class _Prices:
    underlying: Decimal
    marks: Mapping[OptionContract, Decimal]
    initial_fraction: Decimal
    maintenance_fraction: Decimal


def _is_index(contract: OptionContract) -> bool:
    # Every index root the roots table models settles in cash; equity options deliver.
    return option_style(contract.underlying).settlement is Settlement.CASH


def _otm(contract: OptionContract, price: Decimal) -> Decimal:
    if contract.right is OptionRight.CALL:
        return max(contract.strike - price, ZERO)
    return max(price - contract.strike, ZERO)


def _value(contract: OptionContract, contracts: int, prices: _Prices) -> Decimal:
    """Signed market value of ``contracts``: negative when short."""
    return prices.marks[contract] * contract.multiplier * contracts


def naked_margin(contract: OptionContract, contracts: int, underlying: Decimal, mark: Decimal) -> Decimal:
    """OptionMarginModel for ``contracts`` short (negative) contracts, initial and
    maintenance alike: the premium plus the larger of 10% of the strike (put) or
    underlying (call), and 20% (15% for an index) of the underlying less the amount out
    of the money. A long option needs nothing."""
    if contracts >= 0:
        return ZERO
    base = contract.strike if contract.right is OptionRight.PUT else underlying
    fraction = INDEX_OTM_FRACTION if _is_index(contract) else EQUITY_OTM_FRACTION
    per_share = mark + max(NAKED_FLOOR * base, fraction * underlying - _otm(contract, underlying))
    return per_share * contract.multiplier * -contracts


def _leg(strategy: MatchedStrategy, right: OptionRight, short: bool | None = None) -> tuple[OptionContract, int]:
    for contract, quantity in strategy.legs:
        if contract.right is right and (short is None or (quantity < 0) is short):
            return contract, quantity
    raise OptionMarginError(f"{strategy.name} has no {'short ' if short else 'long ' if short is False else ''}{right.name.lower()} leg")


def _width(strategy: MatchedStrategy, right: OptionRight) -> Decimal:
    """max(long strike - short strike, 0) for calls, max(short - long, 0) for puts, per
    unit, in dollars."""
    short, _ = _leg(strategy, right, short=True)
    long, _ = _leg(strategy, right, short=False)
    difference = long.strike - short.strike if right is OptionRight.CALL else short.strike - long.strike
    return max(difference, ZERO) * short.multiplier * strategy.quantity


def _strategy_margin(strategy: MatchedStrategy, prices: _Prices, multiplier: int) -> tuple[Decimal, Decimal, Decimal | None]:
    """(initial before premium, maintenance, cash secured)."""
    s = prices.underlying
    name = strategy.name
    shares = Decimal(strategy.underlying_lots * multiplier)
    stock_value = abs(shares) * s
    stock_initial = prices.initial_fraction * stock_value
    stock_maintenance = prices.maintenance_fraction * stock_value

    if name in (NAKED_CALL, NAKED_PUT):
        contract, quantity = strategy.legs[0]
        margin = naked_margin(contract, quantity, s, prices.marks[contract])
        cash = contract.strike * contract.multiplier * -quantity if name == NAKED_PUT else None
        return margin, margin, cash
    if name in (LONG_CALL, LONG_PUT, CALL_CALENDAR_SPREAD, PUT_CALENDAR_SPREAD):
        return ZERO, ZERO, ZERO
    if name in (BEAR_CALL_SPREAD, BULL_CALL_SPREAD, CALL_DIAGONAL_SPREAD):
        width = _width(strategy, OptionRight.CALL)
        return width, width, width
    if name in (BEAR_PUT_SPREAD, BULL_PUT_SPREAD, PUT_DIAGONAL_SPREAD):
        width = _width(strategy, OptionRight.PUT)
        return width, width, width
    if name in (SHORT_CALL_CALENDAR_SPREAD, SHORT_PUT_CALENDAR_SPREAD):
        contract, quantity = next((c, q) for c, q in strategy.legs if q < 0)
        margin = naked_margin(contract, quantity, s, prices.marks[contract])
        cash = contract.strike * contract.multiplier * -quantity if contract.right is OptionRight.PUT else None
        return margin, margin, cash
    if name == COVERED_CALL:
        # MAX[ITM + stock margin at min(price, strike), min(stock value, max(call value, stock margin))]
        contract, quantity = strategy.legs[0]
        itm = intrinsic(contract, s) * contract.multiplier * -quantity
        hypothetical = prices.maintenance_fraction * abs(shares) * min(s, contract.strike)
        second = min(stock_value, max(_value(contract, quantity, prices), stock_maintenance))
        maintenance = max(itm + hypothetical, second)
        initial = COVERED_CALL_CALL_FRACTION * abs(_value(contract, quantity, prices)) + stock_initial
        return initial, maintenance, ZERO
    if name in (PROTECTIVE_PUT, PROTECTIVE_CALL):
        # min(10% of strike + out-of-the-money amount, stock maintenance)
        contract, quantity = strategy.legs[0]
        option = (NAKED_FLOOR * contract.strike + _otm(contract, s)) * contract.multiplier * quantity
        return stock_initial, min(option, stock_maintenance), ZERO if name == PROTECTIVE_PUT else None
    if name == COVERED_PUT:
        contract, quantity = strategy.legs[0]
        margin = stock_initial + intrinsic(contract, s) * contract.multiplier * -quantity
        return margin, margin, None
    if name == PROTECTIVE_COLLAR:
        # maintenance: min(10% of put strike + put OTM, 25% of call strike);
        # initial: stock initial + call in-the-money amount
        put, put_quantity = _leg(strategy, OptionRight.PUT)
        call, call_quantity = _leg(strategy, OptionRight.CALL)
        per_share = min(NAKED_FLOOR * put.strike + _otm(put, s), COLLAR_CALL_FRACTION * call.strike)
        maintenance = per_share * put.multiplier * put_quantity
        initial = stock_initial + intrinsic(call, s) * call.multiplier * -call_quantity
        return initial, maintenance, ZERO
    raise OptionMarginError(f"No margin formula for {name}")


def strategy_margin(
    strategy: MatchedStrategy,
    underlying: str,
    underlying_price: Decimal,
    marks: Mapping[OptionContract, Decimal],
    initial_fraction: Decimal,
    maintenance_fraction: Decimal,
) -> StrategyMargin:
    """Margin one matched strategy. Initial margin also carries the net premium when the
    strategy was bought for a debit (``OptionInitialMargin``); a credit adds nothing."""
    if underlying_price <= ZERO:
        raise OptionMarginError(f"No price for {underlying} (I5)")
    for contract, _ in strategy.legs:
        mark = marks.get(contract)
        if mark is None or mark <= ZERO:
            raise OptionMarginError(f"No session-close mark for {contract.occ.strip()} (I5)")
    multipliers = {contract.multiplier for contract, _ in strategy.legs}
    if len(multipliers) != 1:
        raise OptionMarginError(f"{strategy.name} mixes contract multipliers {sorted(multipliers)} (I6)")
    multiplier = multipliers.pop()
    prices = _Prices(underlying_price, marks, initial_fraction, maintenance_fraction)
    initial, maintenance, cash = _strategy_margin(strategy, prices, multiplier)
    premium = sum((_value(c, q, prices) for c, q in strategy.legs), ZERO)
    return StrategyMargin(
        name=strategy.name,
        underlying=underlying,
        quantity=strategy.quantity,
        legs=strategy.legs,
        shares=Decimal(strategy.underlying_lots * multiplier),
        initial=initial + max(premium, ZERO),
        maintenance=maintenance,
        cash_secured=cash,
    )


def margin_book(
    underlying: str,
    book: Mapping[OptionContract, int],
    shares: Decimal,
    underlying_price: Decimal,
    marks: Mapping[OptionContract, Decimal],
    initial_fraction: Decimal,
    maintenance_fraction: Decimal,
    definitions: Sequence[StrategyDefinition] = DEFINITIONS,
) -> tuple[tuple[StrategyMargin, ...], Decimal]:
    """Margin one underlying's options (signed contracts) with the account's signed
    ``shares`` of it. Whole lots of shares can go into strategies, as LEAN counts them.

    LEAN takes its greedy grouping as found, and that grouping can be dear: two bull put
    spreads, 95/90 and 85/80, come out as a 90/85 bear put spread and a 95/80 bull put
    spread, 1,500 against 1,000. So every grouping is also searched, and the one with the
    least maintenance (then initial) margin, counting the lots left over as plain stock,
    is kept. LEAN's grouping wins ties and stands when the book is too large to search,
    so the answer never exceeds LEAN's. Returns the strategies and the shares none used.
    """
    multipliers = {contract.multiplier for contract in book}
    if len(multipliers) != 1:
        raise OptionMarginError(f"{underlying} options mix contract multipliers {sorted(multipliers)} (I6)")
    multiplier = multipliers.pop()
    lots = int(shares / multiplier)  # whole lots, toward zero
    priced: dict[MatchedStrategy, StrategyMargin] = {}

    def figures(strategy: MatchedStrategy) -> StrategyMargin:
        if strategy not in priced:
            priced[strategy] = strategy_margin(
                strategy, underlying, underlying_price, marks, initial_fraction, maintenance_fraction
            )
        return priced[strategy]

    def strategy_cost(strategy: MatchedStrategy) -> Cost:
        margin = figures(strategy)
        return margin.maintenance, margin.initial

    def lots_cost(left: int) -> Cost:
        value = abs(left) * multiplier * underlying_price
        return maintenance_fraction * value, initial_fraction * value

    greedy, left = match_strategies(book, lots, definitions)
    cost = lots_cost(left)
    for strategy in greedy:
        own = strategy_cost(strategy)
        cost = (cost[0] + own[0], cost[1] + own[1])
    chosen, chosen_left = greedy, left
    searched = _cheapest(book, lots, definitions, strategy_cost, lots_cost)
    if searched is not None and searched[0] < cost:
        _, chosen, chosen_left = searched
    used = (lots - chosen_left) * multiplier
    return tuple(figures(strategy) for strategy in chosen), shares - used

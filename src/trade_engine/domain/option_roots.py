"""Option roots: what an OCC root trades, how it exercises and when it settles (O1, I5, I6).

An OCC root is not always its underlying. SPX options list under two roots on the same
index: ``SPX`` (the monthly, AM-settled on the expiry-day opening prints, last traded the
session before) and ``SPXW`` (weeklies and dailies, PM-settled on the close). Both are
European and cash-settled. They share strikes and dates, so the root is what keeps them
apart in a book (I6) and what tells lifecycle which settlement price to book (I9).

Any other root is taken to be an equity option: American, physically settled, PM. The
index roots this module does not model yet are refused rather than treated as equities
(I5), and the chain adapter cross-checks every row's exercise style against this table,
so a root that is not what this table says it is refuses there.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from typing import Protocol

from trade_engine.domain.instruments import OptionContract, UnresolvableInstrumentError


class Exercise(StrEnum):
    AMERICAN = "A"
    EUROPEAN = "E"


class Settlement(StrEnum):
    PHYSICAL = "physical"
    CASH = "cash"


class SettleTime(StrEnum):
    AM = "AM"  # the expiry session's opening prints
    PM = "PM"  # the expiry session's close


@dataclass(frozen=True)
class OptionStyle:
    root: str
    underlying: str
    exercise: Exercise
    settlement: Settlement
    settle_time: SettleTime


_INDEX_ROOTS: dict[str, OptionStyle] = {
    "SPX": OptionStyle("SPX", "SPX", Exercise.EUROPEAN, Settlement.CASH, SettleTime.AM),
    "SPXW": OptionStyle("SPXW", "SPX", Exercise.EUROPEAN, Settlement.CASH, SettleTime.PM),
}

# Index roots whose rules are not modelled yet. Each has its own settlement quirks
# (VIX settles on a special Wednesday quotation, XSP is mini-sized), so none may fall
# through to the equity default.
_UNMODELLED_INDEX_ROOTS = frozenset({
    "NDX", "NDXP", "RUT", "RUTW", "MRUT", "XSP", "XND", "DJX", "OEX", "XEO", "VIX", "VIXW",
})


def option_style(root: str) -> OptionStyle:
    """The style of options listed under ``root``; an unmodelled index root refuses."""
    key = root.strip().upper()
    if key in _INDEX_ROOTS:
        return _INDEX_ROOTS[key]
    if key in _UNMODELLED_INDEX_ROOTS:
        raise UnresolvableInstrumentError(f"Index option root {key!r} is not modelled yet (I5)")
    if not key:
        raise UnresolvableInstrumentError("Empty option root (I6)")
    return OptionStyle(key, key, Exercise.AMERICAN, Settlement.PHYSICAL, SettleTime.PM)


def chain_roots(underlying: str) -> tuple[str, ...]:
    """Every root an underlying's chain lists under: ``SPX`` -> ``("SPX", "SPXW")``."""
    key = underlying.strip().upper()
    roots = tuple(sorted(r for r, style in _INDEX_ROOTS.items() if style.underlying == key))
    if roots:
        return roots
    return (option_style(key).root,)


class _Sessions(Protocol):
    def is_session(self, d: date) -> bool: ...
    def session_open(self, d: date) -> datetime: ...
    def session_close(self, d: date) -> datetime: ...
    def previous_session(self, d: date) -> date: ...


def settlement_instant(contract: OptionContract, calendar: _Sessions) -> datetime:
    """When the contract's value is fixed: the expiry session's open (AM) or close (PM).

    The OCC date is the expiry session. When a holiday takes that day the exchanges list
    the series on the session before, so a date that is not a session is refused rather
    than moved (I5).
    """
    if not calendar.is_session(contract.expiry):
        raise ValueError(f"{contract.occ} expires on {contract.expiry}, which is not a session (I5)")
    style = option_style(contract.underlying)
    if style.settle_time is SettleTime.AM:
        return calendar.session_open(contract.expiry)
    return calendar.session_close(contract.expiry)


def last_trade_date(contract: OptionContract, calendar: _Sessions) -> date:
    """The last session the contract trades: the session before expiry for AM-settled roots."""
    if not calendar.is_session(contract.expiry):
        raise ValueError(f"{contract.occ} expires on {contract.expiry}, which is not a session (I5)")
    if option_style(contract.underlying).settle_time is SettleTime.AM:
        return calendar.previous_session(contract.expiry)
    return contract.expiry

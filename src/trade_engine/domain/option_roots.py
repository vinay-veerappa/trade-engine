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
from datetime import date, datetime, timezone
from enum import StrEnum

import trade_engine_rs as _rs

from trade_engine.calendar.sessions import ExchangeCalendar
from trade_engine.domain.instruments import OptionContract, contract_args, rs_call

# The table and the settlement rules live in Rust (`te_core::options`, docs/RUST_PORT.md
# P1e, settlement against `te_core::calendar`). The enums and OptionStyle are the carriers.


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


def option_style(root: str) -> OptionStyle:
    """The style of options listed under ``root``; an unmodelled index root refuses."""
    r, underlying, exercise, settlement, settle_time = rs_call(_rs.option_style, root)
    return OptionStyle(r, underlying, Exercise(exercise), Settlement(settlement), SettleTime(settle_time))


def chain_roots(underlying: str) -> tuple[str, ...]:
    """Every root an underlying's chain lists under: ``SPX`` -> ``("SPX", "SPXW")``."""
    return tuple(rs_call(_rs.option_chain_roots, underlying))


def _xnys(calendar: ExchangeCalendar) -> None:
    """Settlement is the engine's XNYS calendar (D7); any other refuses (I5)."""
    if not isinstance(calendar, ExchangeCalendar) or calendar.exchange != "XNYS":
        raise ValueError(f"Option settlement needs the XNYS ExchangeCalendar, got {calendar!r} (I5)")


def settlement_instant(contract: OptionContract, calendar: ExchangeCalendar) -> datetime:
    """When the contract's value is fixed: the expiry session's open (AM) or close (PM).

    The OCC date is the expiry session. When a holiday takes that day the exchanges list
    the series on the session before, so a date that is not a session is refused rather
    than moved (I5).
    """
    _xnys(calendar)
    return datetime.fromtimestamp(rs_call(_rs.option_settlement_instant, contract_args(contract)), tz=timezone.utc)


def last_trade_date(contract: OptionContract, calendar: ExchangeCalendar) -> date:
    """The last session the contract trades: the session before expiry for AM-settled roots."""
    _xnys(calendar)
    return date.fromisoformat(rs_call(_rs.option_last_trade_date, contract_args(contract)))

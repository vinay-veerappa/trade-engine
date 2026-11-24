"""The after-close lifecycle pass: expiry, exercise, assignment (O2, Architecture §4.6, I9).

Run once per session, after the close, for every account's option positions:

- **Expiry.** A contract expiring this session settles on its official price: the
  close for PM-settled contracts, the opening settlement for AM-settled ones (``SPX``
  monthlies). In the money by at least the OCC threshold it is exercised (long) or
  assigned (short); otherwise it expires worthless (``domain.option_lifecycle``).
- **Early assignment before an ex-dividend date.** A short American call in the money at
  this session's close, whose shares go ex-dividend next session, is assigned when the
  dividend is worth more than what the holder would give up by exercising: the call's
  bid less its intrinsic value (``exercised_for_dividend``).

The pass appends one ``OptionLifecycle`` event per position settled; the fold works out
the shares, cash and P&L from the position's lots. It refuses (I5), writing nothing,
when the clock has not reached the close; when a price, dividend record or quote it needs
is unknown, stale, or from after the clock; when a settlement price was known before its
settlement instant; or when an option expired in an earlier session and was never
settled. Each event's command id is ``lifecycle:<account>:<occ>:<session>``, so a re-run
appends nothing (I3).

The pass itself is native (``trade_engine_rs.lifecycle_run``); the settlement, dividend and
quote sources stay Python plugins it calls.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date

from trade_engine.sim._rs import register
from trade_engine.interfaces.market_data import StaleDataError
from trade_engine.ledger import Event, Ledger
from trade_engine.lifecycle.sources import Dividends, OptionQuotes, Settlements
from trade_engine.sim._rs import rs


class LifecycleError(RuntimeError):
    """The pass refuses to settle a session (I5, I9)."""


register("lifecycle", LifecycleError)
register("stale", StaleDataError)


@dataclass(frozen=True)
class LifecycleResult:
    session: date
    events: tuple[Event, ...]


class LifecyclePass:
    """Settle a session's expiring option positions and early assignments, after its close."""

    def __init__(
        self,
        ledger: Ledger,
        clock,
        calendar,
        settlements: Settlements,
        *,
        dividends: Dividends | None = None,
        quotes: OptionQuotes | None = None,
    ) -> None:
        self._ledger = ledger
        self._clock = clock
        self._calendar = calendar
        self._settlements = settlements
        self._dividends = dividends
        self._quotes = quotes

    def run(self, session: date, accounts: Iterable[str] | None = None) -> LifecycleResult:
        return LifecycleResult(session, tuple(rs.lifecycle_run(self, session, accounts)))

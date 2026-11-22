"""Settle a venue mirror's expired option contracts before it reconciles (PM-B, 2026-11-22).

A contract that expires leaves the venue on its own; the mirror's book keeps it until an
``OptionLifecycle`` on the mirror account takes it out (the fold's ``on_mirror_lifecycle``).
Without this, a book left holding a contract the venue no longer reports drifts on every
reconcile and halts the venue for good (10-13: a REJECTED close left two SPXW spreads booked).

Each expiry at or before today whose session has closed is settled through the same
``LifecyclePass`` the strategy accounts use, with ``session = expiry``; the pass's command ids
make a re-run a no-op. A settlement price that is not known yet (``StaleDataError``) or a
refused pass (``LifecycleError``, or the fold's ``LedgerFoldError`` for an exercise that delivers
shares, which the mirror cannot book: settle that one by hand, I5) settles nothing and is
reported, never raised.
"""

from __future__ import annotations

from dataclasses import dataclass

from trade_engine.domain.instruments import OptionContract
from trade_engine.interfaces.market_data import StaleDataError
from trade_engine.ledger import Event, Ledger
from trade_engine.ledger.errors import LedgerFoldError
from trade_engine.ledger.events import mirror_account
from trade_engine.lifecycle.after_close import LifecycleError, LifecyclePass


@dataclass(frozen=True)
class ExpiryOutcome:
    events: tuple[Event, ...] = ()
    unsettled: tuple[str, ...] = ()  # "<expiry>: <why>" for each expiry that could not settle yet


def settle_expired(ledger: Ledger, venue: str, clock, calendar, settlements) -> ExpiryOutcome:
    account = mirror_account(venue)
    book = ledger.state(account).mirror.book
    now = clock.now_utc()
    expiries = sorted({
        instrument.expiry for (_strategy, instrument), quantity in book.items()
        if isinstance(instrument, OptionContract) and quantity
    })
    events: list[Event] = []
    unsettled: list[str] = []
    for expiry in expiries:
        try:
            if not calendar.is_session(expiry) or calendar.session_close(expiry) > now:
                continue
            result = LifecyclePass(ledger, clock, calendar, settlements).run(expiry, [account])
        except (LifecycleError, LedgerFoldError, StaleDataError, ValueError) as err:
            unsettled.append(f"{expiry.isoformat()}: {type(err).__name__}: {err}")
            continue
        events.extend(result.events)
    return ExpiryOutcome(events=tuple(events), unsettled=tuple(unsettled))

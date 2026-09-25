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
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from trade_engine.domain.instruments import OptionContract, OptionRight, Side
from trade_engine.domain.option_lifecycle import (
    EXERCISE_THRESHOLD,
    Outcome,
    can_exercise_early,
    exercised_for_dividend,
    expiry_outcome,
    intrinsic,
)
from trade_engine.domain.option_roots import SettleTime, option_style, settlement_instant
from trade_engine.interfaces.market_data import StaleDataError
from trade_engine.ledger import Event, EventKind, Ledger, OptionLifecycle
from trade_engine.lifecycle.sources import Dividends, OptionQuotes, SettlementPrice, Settlements

_KIND = {
    Outcome.EXPIRE: EventKind.EXPIRY,
    Outcome.EXERCISE: EventKind.EXERCISE,
    Outcome.ASSIGN: EventKind.ASSIGNMENT,
}


class LifecycleError(RuntimeError):
    """The pass refuses to settle a session (I5, I9)."""


@dataclass(frozen=True)
class LifecycleResult:
    session: date
    events: tuple[Event, ...]


def _compact(contract: OptionContract) -> str:
    return contract.occ.replace(" ", "")


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
        if not self._calendar.is_session(session):
            raise LifecycleError(f"{session.isoformat()} is not a session (I5)")
        now = self._clock.now_utc()
        close = self._calendar.session_close(session)
        if now < close:
            raise LifecycleError(
                f"The clock reads {now.isoformat()}, before the {session.isoformat()} close "
                f"{close.isoformat()}; expiry and assignment are decided after the close (I9)"
            )
        names = sorted(accounts) if accounts is not None else sorted(self._ledger.accounts())
        # Every account is decided before anything is written, so one refusal leaves the
        # ledger as it was and the re-run starts clean (I2).
        events = [event for account in names for event in self._account(account, session, now)]
        written = self._ledger.extend(events) if events else []
        return LifecycleResult(session=session, events=tuple(written))

    # -- one account ---------------------------------------------------------------

    def _account(self, account: str, session: date, now: datetime) -> list[Event]:
        state = self._ledger.state(account)
        held = sorted(
            (
                (instrument, position)
                for instrument, position in state.positions.items()
                if isinstance(instrument, OptionContract) and position.quantity != 0
            ),
            key=lambda item: item[0].occ,
        )
        events: list[Event] = []
        for contract, position in held:
            side = Side.BUY if position.quantity > 0 else Side.SELL
            quantity = abs(position.quantity)
            if contract.expiry <= session:
                self._instant(contract)  # refuses a contract whose expiry is not a session
                if contract.expiry < session:
                    raise LifecycleError(
                        f"{contract.occ.strip()} in '{account}' expired on "
                        f"{contract.expiry.isoformat()} and was never settled; run the "
                        f"lifecycle pass for that session first (I9)"
                    )
                events.append(self._expiry(account, contract, side, quantity, session, now))
                continue
            early = self._early_assignment(account, contract, side, quantity, session, now)
            if early is not None:
                events.append(early)
        return events

    def _instant(self, contract: OptionContract) -> datetime:
        try:
            return settlement_instant(contract, self._calendar)
        except ValueError as err:
            raise LifecycleError(str(err)) from err

    def _price(
        self, underlying: str, session: date, settle_time: SettleTime, settled_at: datetime, now: datetime
    ) -> SettlementPrice:
        price = self._settlements.settlement(underlying, session, settle_time)
        if (price.underlying, price.session, price.settle_time) != (underlying, session, settle_time):
            raise LifecycleError(
                f"Asked for the {settle_time.value} settlement of {underlying} on {session}, got "
                f"{price.settle_time.value} {price.underlying} on {price.session} (I5)"
            )
        if price.as_of > now:
            raise LifecycleError(
                f"{underlying} settlement is stamped {price.as_of.isoformat()}, after the clock "
                f"{now.isoformat()}: look-ahead (I7)"
            )
        if price.as_of < settled_at:
            raise LifecycleError(
                f"{underlying} settlement is stamped {price.as_of.isoformat()}, before the "
                f"settlement instant {settled_at.isoformat()}; it cannot be the official price (I9)"
            )
        return price

    def _expiry(
        self, account: str, contract: OptionContract, side: Side, quantity, session: date, now: datetime
    ) -> Event:
        style = option_style(contract.underlying)
        price = self._price(style.underlying, session, style.settle_time, self._instant(contract), now)
        outcome = expiry_outcome(contract, side, price.price)
        value = intrinsic(contract, price.price)
        reason = (
            f"expired worthless: {value} in the money at the {style.settle_time.value} settlement "
            f"{price.price}, under the {EXERCISE_THRESHOLD} exercise threshold"
            if outcome is Outcome.EXPIRE
            else f"{'exercised' if outcome is Outcome.EXERCISE else 'assigned'} at expiry: {value} in "
            f"the money at the {style.settle_time.value} settlement {price.price}"
        )
        return self._event(account, contract, side, quantity, price, now, _KIND[outcome], reason, session)

    def _early_assignment(
        self, account: str, contract: OptionContract, side: Side, quantity, session: date, now: datetime
    ) -> Event | None:
        if side is not Side.SELL or contract.right is not OptionRight.CALL or not can_exercise_early(contract):
            return None
        underlying = option_style(contract.underlying).underlying
        ex_date = self._calendar.next_session(session)
        if self._dividends is None:
            raise LifecycleError(
                f"'{account}' is short the American call {contract.occ.strip()} and no dividend "
                f"source is configured, so its early assignment cannot be decided (I5)"
            )
        dividends = self._dividends.dividends(underlying, ex_date)
        for dividend in dividends:
            if dividend.as_of > now:
                raise LifecycleError(f"{underlying} dividend record is from after the clock: look-ahead (I7)")
        if not dividends:
            return None
        amount = sum((d.amount for d in dividends), Decimal("0"))
        close = self._price(underlying, session, SettleTime.PM, self._calendar.session_close(session), now)
        if intrinsic(contract, close.price) < EXERCISE_THRESHOLD:
            return None
        if self._quotes is None:
            raise LifecycleError(
                f"{contract.occ.strip()} in '{account}' is in the money before a {amount} dividend "
                f"and no option quote source is configured (I5)"
            )
        quote = self._quotes.quote(contract, now)
        if quote.as_of > now:
            raise StaleDataError(f"{contract.occ.strip()} quote is from after the clock: look-ahead (I7)")
        if not exercised_for_dividend(contract, close.price, quote.bid, amount):
            return None
        extrinsic = quote.bid - intrinsic(contract, close.price)
        reason = (
            f"assigned early: goes ex a {amount} dividend on {ex_date.isoformat()}, more than the "
            f"call's extrinsic value {extrinsic} (bid {quote.bid}, close {close.price})"
        )
        return self._event(
            account, contract, side, quantity, close, now, EventKind.ASSIGNMENT, reason, session, early=True
        )

    def _event(
        self,
        account: str,
        contract: OptionContract,
        side: Side,
        quantity,
        price: SettlementPrice,
        now: datetime,
        kind: EventKind,
        reason: str,
        session: date,
        *,
        early: bool = False,
    ) -> Event:
        suffix = ":early" if early else ""
        return Event(
            account=account,
            kind=kind,
            payload=OptionLifecycle(
                account_id=account,
                contract=contract,
                quantity=quantity,
                held=side,
                underlying_price=price.price,
                price_source=price.source,
                as_of=now,
                reason=reason,
                early=early,
            ),
            ts_utc=now,
            command_id=f"lifecycle:{account}:{_compact(contract)}:{session.isoformat()}{suffix}",
        )

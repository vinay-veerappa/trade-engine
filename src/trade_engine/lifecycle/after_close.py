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

from trade_engine.domain.instruments import OptionContract, OptionRight, Side, money
from trade_engine.domain.option_lifecycle import can_exercise_early
from trade_engine.domain.option_roots import SettleTime, option_style, settlement_instant
from trade_engine.interfaces.market_data import StaleDataError
from trade_engine.ledger import Event, EventKind, Ledger, OptionLifecycle
from trade_engine.lifecycle.sources import Dividends, OptionQuotes, SettlementPrice, Settlements
from trade_engine._lifecycle_runtime import decide, flag, journal, register


class LifecycleError(RuntimeError):
    """The pass refuses to settle a session (I5, I9)."""


register("lifecycle", LifecycleError)
register("stale", StaleDataError)


@dataclass(frozen=True)
class LifecycleResult:
    session: date
    events: tuple[Event, ...]


def _compact(contract: OptionContract) -> str:
    return decide("compact", [contract.occ])[0][0]


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
        decide("session", [session.isoformat()], flags=[self._calendar.is_session(session)])
        now = self._clock.now_utc()
        close = self._calendar.session_close(session)
        decide("close", [now.isoformat(), session.isoformat(), close.isoformat()],
               [-int(journal("before", now, close))])
        names = decide("accounts", list(accounts) if accounts is not None else self._ledger.accounts())[0]
        # Every account is decided before anything is written, so one refusal leaves the
        # ledger as it was and the re-run starts clean (I2).
        events = [event for account in names for event in self._account(account, session, now)]
        written = self._ledger.extend(events) if events else []
        return LifecycleResult(session=session, events=tuple(written))

    # -- one account ---------------------------------------------------------------

    def _account(self, account: str, session: date, now: datetime) -> list[Event]:
        state = self._ledger.state(account)
        rows = list(state.positions.items())
        indices = decide("held",
            [v for instrument, position in rows for v in
             (instrument.occ if isinstance(instrument, OptionContract) else "", str(position.quantity))],
            flags=[isinstance(instrument, OptionContract) for instrument, _ in rows])[1]
        held = [rows[i] for i in indices]
        events: list[Event] = []
        for contract, position in held:
            text, _, flags = decide("position", [str(position.quantity)],
                                    [(contract.expiry - session).days])
            side = Side.BUY if flags[0] else Side.SELL
            quantity = Decimal(text[0])
            if flags[1]:
                self._instant(contract)  # refuses a contract whose expiry is not a session
                decide("overdue", [contract.occ.strip(), account, contract.expiry.isoformat()],
                       [(contract.expiry - session).days])
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
        text = [settle_time.value, underlying, str(session), price.settle_time.value,
                price.underlying, str(price.session)]
        decide("price_identity", text)
        text.extend([price.as_of.isoformat(), now.isoformat(), settled_at.isoformat()])
        decide("price_clock", text, [int(journal("after", price.as_of, now))])
        decide("price_known", text, [-int(journal("before", price.as_of, settled_at))])
        return price

    def _expiry(
        self, account: str, contract: OptionContract, side: Side, quantity, session: date, now: datetime
    ) -> Event:
        style = option_style(contract.underlying)
        price = self._price(style.underlying, session, style.settle_time, self._instant(contract), now)
        text = decide("expiry", [money(contract.strike), money(price.price), style.settle_time.value, contract.underlying],
                      [contract.expiry.year, contract.expiry.month, contract.expiry.day],
                      flags=[contract.right is OptionRight.CALL, side is Side.BUY])[0]
        return self._event(account, contract, side, quantity, price, now, EventKind(text[0]), text[1], session)

    def _early_assignment(
        self, account: str, contract: OptionContract, side: Side, quantity, session: date, now: datetime
    ) -> Event | None:
        if not flag("eligible", flags=[side is Side.SELL, contract.right is OptionRight.CALL,
                    can_exercise_early(contract)]):
            return None
        underlying = option_style(contract.underlying).underlying
        ex_date = self._calendar.next_session(session)
        decide("dividend_source", [account, contract.occ.strip()], flags=[self._dividends is not None])
        dividends = self._dividends.dividends(underlying, ex_date)
        for dividend in dividends:
            decide("dividend_time", [underlying], [int(journal("after", dividend.as_of, now))])
        summed = decide("sum", [str(d.amount) for d in dividends])
        if not summed[2][0]:
            return None
        amount = Decimal(summed[0][0])
        close = self._price(underlying, session, SettleTime.PM, self._calendar.session_close(session), now)
        if not flag("itm", [money(contract.strike), money(close.price), contract.underlying],
                    [contract.expiry.year, contract.expiry.month, contract.expiry.day],
                    flags=[contract.right is OptionRight.CALL]):
            return None
        decide("quote_source", [contract.occ.strip(), account, str(amount)],
               flags=[self._quotes is not None])
        quote = self._quotes.quote(contract, now)
        decide("quote_time", [contract.occ.strip()], [int(journal("after", quote.as_of, now))])
        plan = decide("early", [money(contract.strike), money(close.price), money(quote.bid),
                               money(amount), ex_date.isoformat(), contract.underlying],
                      [contract.expiry.year, contract.expiry.month, contract.expiry.day])
        if not plan[2][0]:
            return None
        return self._event(
            account, contract, side, quantity, close, now, EventKind.ASSIGNMENT, plan[0][0], session, early=True
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
        text, _, flags = decide("event", [account, contract.occ, str(quantity), str(price.price),
                                         price.source, session.isoformat()], flags=[early])
        return Event(
            account=text[0],
            kind=kind,
            payload=OptionLifecycle(
                account_id=text[0],
                contract=contract,
                quantity=Decimal(text[2]),
                held=side,
                underlying_price=Decimal(text[3]),
                price_source=text[4],
                as_of=now,
                reason=reason,
                early=flags[0],
            ),
            ts_utc=now,
            command_id=text[5],
        )

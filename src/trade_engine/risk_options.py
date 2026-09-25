"""Account rules for options entries (O4, rules doc §6.1–§6.2, I5, I11).

``OptionRiskEngine.evaluate(intent, context)`` checks an ``OptionIntent`` against the
account's ``OptionRiskRules`` and returns a ``RiskVerdict`` listing every rule, passed
or not (I11). It never resizes: the strategy sizes each structure to its own per-structure
cap (§6.2), and this layer refuses what would break an account-wide one.

Every figure is measured on the account as it would stand once the entry filled at its
limit (or, for a market order, at the snapshot's price): the O3 option margin of the
whole book, the cash that secures each underlying's strategies, the naked put notional.
An input that cannot be measured refuses the entry rather than passing it (I5): an
unknown regime, earnings date, price or mark.

Rules (a rule configured as None does not apply to the account, and says so):

- ``margin``: the Reg-T maintenance requirement of the whole book at most
  ``max_margin_frac`` of equity (§6.1: 50%, so a 2–3x premium expansion cannot force a
  margin call).
- ``name_margin``: the Reg-T maintenance of one underlying's strategies and shares at
  most ``max_name_margin_frac`` of equity. The owner's reading of §6.2's "10% per name"
  (2026-09-24): measured in cash, it would allow only strikes up to $50 on $50,000.
- ``name_collateral``: the cash securing one underlying's strategies at most
  ``max_name_collateral_frac`` of equity (§6.2 read literally).
- ``put_notional``: the strikes of every naked short put at most the regime's fraction of
  equity (§6.2: 100% in BULL_EXPLOSIVE, 50% in BULL_CHOPIER, 0 — spreads only — in
  BEAR_PROTECTIVE). A regime with no fraction configured refuses.
- ``max_loss``: the structure's own worst case at most ``max_loss_per_structure_frac``
  (§6.2 bull put spread: 2%). A structure whose loss has no bound (a naked call) refuses.
- ``debit``: a debit structure's cost at most ``max_debit_per_structure_frac`` (§6.2
  PMCC: 5%), and every open debit structure's together at most ``max_total_debit_frac``
  (30%).
- ``share_notional``: shares bought at most ``max_share_notional_frac`` (§6.2 buy-write:
  100 × S ≤ 20%).
- ``regime``: a known regime in ``allowed_regimes``. UNKNOWN never is.
- ``earnings``: with ``no_earnings_before_expiry``, no earnings date on or before the
  structure's expiry. A date the source cannot give refuses.
- ``duplicate_entry`` (C4) and ``covered_calls`` (C3): the OMS guards, measured here too,
  so a strategy that proposes one is refused and recorded rather than failing the run.
- ``duplicate_protection`` and ``persistent_kill_switch``: as for equity entries.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from decimal import Decimal
from types import MappingProxyType
from typing import Any, Protocol

from trade_engine.domain.instruments import Equity, OptionContract, OptionRight, Side
from trade_engine.domain.option_orders import OptionIntent, is_structure, legs_of
from trade_engine.domain.portfolio import Position
from trade_engine.domain.risk import RiskRuleResult, RiskVerdict
from trade_engine.interfaces.clock import Clock
from trade_engine.interfaces.market_data import StaleDataError
from trade_engine.ledger import Ledger
from trade_engine.ledger.state import AccountState
from trade_engine.metrics.margin import account_margin
from trade_engine.metrics.option_margin import NAKED_PUT, OptionMarginError
from trade_engine.oms.options import open_structures, uncovered_calls
from trade_engine.sim.snapshot_venue import underlying_of

ZERO = Decimal("0")
REGIMES = frozenset({"BULL_EXPLOSIVE", "BULL_CHOPIER", "BEAR_PROTECTIVE"})


class OptionRiskConfigurationError(ValueError):
    """Options risk rules that are invalid or incomplete."""


class EarningsSource(Protocol):
    def next_earnings(self, symbol: str, session: date) -> date | None:
        """The next earnings date on or after ``session``; None when the name reports
        none (an ETF). ``StaleDataError`` when it cannot say (I5)."""
        ...


def _fraction(value: Decimal | None, name: str) -> None:
    if value is None:
        return
    if not isinstance(value, Decimal) or not value.is_finite() or value < 0 or value > 10:
        raise OptionRiskConfigurationError(f"{name} must be a fraction of equity, got {value!r}")


@dataclass(frozen=True)
class OptionRiskRules:
    max_margin_frac: Decimal
    allowed_regimes: frozenset[str]
    no_earnings_before_expiry: bool
    max_name_margin_frac: Decimal | None = None
    max_name_collateral_frac: Decimal | None = None
    put_notional_frac_by_regime: Mapping[str, Decimal] | None = None
    max_loss_per_structure_frac: Decimal | None = None
    max_debit_per_structure_frac: Decimal | None = None
    max_total_debit_frac: Decimal | None = None
    max_share_notional_frac: Decimal | None = None

    def __post_init__(self) -> None:
        _fraction(self.max_margin_frac, "max_margin_frac")
        for name in (
            "max_name_margin_frac",
            "max_name_collateral_frac",
            "max_loss_per_structure_frac",
            "max_debit_per_structure_frac",
            "max_total_debit_frac",
            "max_share_notional_frac",
        ):
            _fraction(getattr(self, name), name)
        unknown = set(self.allowed_regimes) - REGIMES
        if unknown or not self.allowed_regimes:
            raise OptionRiskConfigurationError(
                f"allowed_regimes must be a non-empty subset of {sorted(REGIMES)}, got "
                f"{sorted(self.allowed_regimes)}"
            )
        object.__setattr__(self, "allowed_regimes", frozenset(self.allowed_regimes))
        if self.put_notional_frac_by_regime is not None:
            for regime, value in self.put_notional_frac_by_regime.items():
                if regime not in REGIMES:
                    raise OptionRiskConfigurationError(f"Unknown regime {regime!r} in put_notional_frac_by_regime")
                _fraction(value, f"put_notional_frac_by_regime[{regime}]")
            object.__setattr__(
                self, "put_notional_frac_by_regime", MappingProxyType(dict(self.put_notional_frac_by_regime))
            )


@dataclass(frozen=True)
class _Book:
    """The account's equity now, and the account as if the entry had filled.

    ``after`` is None, with ``error`` saying why, when the entry cannot be priced: a rule
    measured on the account without the entry would pass what it cannot see (I5).
    """

    equity: Decimal | None
    after: AccountState | None
    error: str | None


class OptionRiskEngine:
    """Evaluate options entries against one account's rules."""

    def __init__(
        self,
        rules: OptionRiskRules,
        clock: Clock,
        ledger: Ledger,
        *,
        venue_id: str,
        regime_of: Callable[[date], str | None],
        earnings: EarningsSource | None = None,
    ) -> None:
        if not venue_id:
            raise OptionRiskConfigurationError("venue_id must be non-empty")
        if rules.no_earnings_before_expiry and earnings is None:
            raise OptionRiskConfigurationError(
                "no_earnings_before_expiry needs an earnings source; none may be assumed (I5)"
            )
        self.rules = rules
        self.clock = clock
        self.ledger = ledger
        self.venue_id = venue_id
        self._regime_of = regime_of
        self._earnings = earnings

    # -- the verdict --------------------------------------------------------------------

    def evaluate(self, intent: OptionIntent, context: Any) -> RiskVerdict:
        results: list[RiskRuleResult] = []

        def record(name: str, passed: bool, measured: object, threshold: object, success: str, refusal: str) -> None:
            results.append(RiskRuleResult(name, passed, measured, threshold, success if passed else refusal))

        def not_configured(name: str) -> None:
            results.append(RiskRuleResult(name, True, "n/a", "n/a", f"{name} is not configured for this account"))

        rules = self.rules
        state: AccountState = context.state
        underlying = underlying_of(intent.instrument)
        price = self._entry_price(intent, context)
        book = self._book(state, intent, price, context)
        equity = book.equity
        book_error = book.error

        # regime
        regime = self._regime_of(context.session)
        record(
            "regime",
            regime in rules.allowed_regimes,
            regime or "UNKNOWN",
            sorted(rules.allowed_regimes),
            "Regime permits options entries",
            "Regime is unknown or not one this account enters in",
        )

        # margin, measured on the whole book with the entry in it
        margin = None
        if book.after is not None:
            try:
                margin = account_margin(book.after)
            except (OptionMarginError, ValueError) as err:
                book_error = str(err)
        cap = equity * rules.max_margin_frac if equity is not None and equity > 0 else None
        record(
            "margin",
            margin is not None and cap is not None and margin.margin_used <= cap,
            margin.margin_used if margin is not None else f"UNKNOWN ({book_error})",
            cap if cap is not None else "UNKNOWN",
            "Reg-T requirement with the entry is within the account cap",
            "Reg-T requirement is unknown or would exceed the account cap",
        )

        # margin on this underlying: its strategies and any of its shares margined alone
        if rules.max_name_margin_frac is None:
            not_configured("name_margin")
        else:
            on_name = None
            if margin is not None:
                on_name = sum(
                    (s.maintenance for s in margin.strategies if s.underlying == underlying), ZERO
                ) + sum((p.maintenance for p in margin.positions if p.symbol == underlying), ZERO)
            limit = equity * rules.max_name_margin_frac if equity is not None and equity > 0 else None
            record(
                "name_margin",
                on_name is not None and limit is not None and on_name <= limit,
                on_name if on_name is not None else "UNKNOWN",
                limit if limit is not None else "UNKNOWN",
                f"Margin on {underlying} is within the per-name cap",
                f"Margin on {underlying} is unknown or over the per-name cap",
            )

        # cash securing this underlying's strategies
        if rules.max_name_collateral_frac is None:
            not_configured("name_collateral")
        else:
            secured = None
            if margin is not None:
                secured = sum(
                    (s.cash_secured for s in margin.strategies if s.underlying == underlying and s.cash_secured is not None),
                    ZERO,
                )
            limit = equity * rules.max_name_collateral_frac if equity is not None and equity > 0 else None
            record(
                "name_collateral",
                secured is not None and limit is not None and secured <= limit,
                secured if secured is not None else "UNKNOWN",
                limit if limit is not None else "UNKNOWN",
                f"Cash securing {underlying} is within the per-name cap",
                f"Cash securing {underlying} is unknown or over the per-name cap",
            )

        # naked put notional by regime
        if rules.put_notional_frac_by_regime is None:
            not_configured("put_notional")
        else:
            fraction = rules.put_notional_frac_by_regime.get(regime) if regime else None
            notional = None
            if margin is not None:
                notional = sum(
                    (s.cash_secured for s in margin.strategies if s.name == NAKED_PUT and s.cash_secured is not None),
                    ZERO,
                )
            limit = equity * fraction if fraction is not None and equity is not None and equity > 0 else None
            record(
                "put_notional",
                notional is not None and limit is not None and notional <= limit,
                notional if notional is not None else "UNKNOWN",
                limit if limit is not None else f"UNKNOWN (no fraction for {regime or 'UNKNOWN'})",
                "Naked put notional is within the regime's cap",
                "Naked put notional is unknown or over the regime's cap",
            )

        # the structure's own worst case
        if rules.max_loss_per_structure_frac is None:
            not_configured("max_loss")
        else:
            loss = self._max_loss(intent, price) if price is not None else None
            limit = equity * rules.max_loss_per_structure_frac if equity is not None and equity > 0 else None
            record(
                "max_loss",
                loss is not None and limit is not None and loss <= limit,
                loss if loss is not None else "UNBOUNDED or UNKNOWN",
                limit if limit is not None else "UNKNOWN",
                "The structure's maximum loss is within the per-structure cap",
                "The structure's maximum loss is unbounded, unknown or over the cap",
            )

        # debit structures
        debit = (
            price * intent.quantity * self._multiplier(intent)
            if price is not None and intent.side is Side.BUY and is_structure(intent.instrument)
            else ZERO if price is not None else None
        )
        if rules.max_debit_per_structure_frac is None:
            not_configured("debit")
        else:
            limit = equity * rules.max_debit_per_structure_frac if equity is not None and equity > 0 else None
            record(
                "debit",
                debit is not None and limit is not None and debit <= limit,
                debit if debit is not None else "UNKNOWN",
                limit if limit is not None else "UNKNOWN",
                "The structure's debit is within the per-structure cap",
                "The structure's debit is unknown or over the per-structure cap",
            )
        if rules.max_total_debit_frac is None:
            not_configured("total_debit")
        else:
            held = sum(
                (
                    s.entry_price * s.units * s.legs[0].contract.multiplier
                    for s in open_structures(state)
                    if not s.credit
                ),
                ZERO,
            )
            total = held + debit if debit is not None else None
            limit = equity * rules.max_total_debit_frac if equity is not None and equity > 0 else None
            record(
                "total_debit",
                total is not None and limit is not None and total <= limit,
                total if total is not None else "UNKNOWN",
                limit if limit is not None else "UNKNOWN",
                "Debit structures together are within the account cap",
                "Debit structures together are unknown or over the account cap",
            )

        # shares bought
        if rules.max_share_notional_frac is None:
            not_configured("share_notional")
        elif isinstance(intent.instrument, Equity) and intent.side is Side.BUY:
            notional = price * intent.quantity if price is not None else None
            limit = equity * rules.max_share_notional_frac if equity is not None and equity > 0 else None
            record(
                "share_notional",
                notional is not None and limit is not None and notional <= limit,
                notional if notional is not None else "UNKNOWN",
                limit if limit is not None else "UNKNOWN",
                "Shares bought are within the position cap",
                "Shares bought are unknown or over the position cap",
            )
        else:
            results.append(RiskRuleResult("share_notional", True, "n/a", "n/a", "No shares are bought"))

        # earnings before expiry
        if not rules.no_earnings_before_expiry or not is_structure(intent.instrument):
            not_configured("earnings")
        else:
            expiry = max(leg.contract.expiry for leg in legs_of(intent.instrument, intent.side))
            try:
                earnings = self._earnings.next_earnings(underlying, context.session)
                known = True
            except StaleDataError:
                earnings, known = None, False
            record(
                "earnings",
                known and (earnings is None or earnings > expiry),
                earnings.isoformat() if earnings is not None else ("none scheduled" if known else "UNKNOWN"),
                f"after {expiry.isoformat()}",
                "No earnings before the structure expires",
                "Earnings fall before the structure expires, or the date is unknown",
            )

        # the OMS guards, recorded (C3, C4)
        if is_structure(intent.instrument):
            wanted = {leg.contract for leg in legs_of(intent.instrument, intent.side)}
            held_contracts = {c for c, p in state.positions.items() if isinstance(c, OptionContract) and p.quantity != 0}
            entering = {
                leg.contract
                for order in state.orders.values()
                if order.parent_order_id is None
                and order.state.value not in ("FILLED", "CANCELLED", "REJECTED", "EXPIRED")
                and is_structure(order.instrument)
                for leg in legs_of(order.instrument, order.side)
            }
            clash = sorted(c.occ.strip() for c in wanted & (held_contracts | entering))
            record(
                "duplicate_entry",
                not clash,
                clash or "none",
                "no contract already held or being entered",
                "No contract of the entry is already held or being entered (C4)",
                "A contract of the entry is already held or being entered (C4)",
            )
            short_calls = any(
                leg.side is Side.SELL and leg.contract.right is OptionRight.CALL
                for leg in legs_of(intent.instrument, intent.side)
            )
            if short_calls:
                needed, shares = (
                    uncovered_calls(book.after, closing_counts=True).get(underlying, (ZERO, ZERO))
                    if book.after is not None
                    else (None, None)
                )
                record(
                    "covered_calls",
                    needed is not None and needed <= shares,
                    needed if needed is not None else f"UNKNOWN ({book_error})",
                    shares if shares is not None else "UNKNOWN",
                    "Every short call is covered by shares or a later long call (C3)",
                    "A short call would be written on shares the account does not hold (C3)",
                )
            else:
                results.append(RiskRuleResult("covered_calls", True, "n/a", "n/a", "The entry writes no call"))

        record(
            "duplicate_protection",
            not self.ledger.has_command(intent.command_id),
            intent.command_id,
            "command id not present in ledger",
            "The entry's command id is new",
            "The entry's command id was already used",
        )
        kill = self.ledger.state(f"__venue__:{self.venue_id}").risk_controls.get("kill_switch", False)
        record("persistent_kill_switch", not kill, kill, False, "Kill switch is off", "Kill switch is engaged")
        refusals = tuple(result.reason for result in results if not result.passed)
        return RiskVerdict(order_intent_id=intent.intent_id, evaluations=tuple(results), refusal_reasons=refusals)

    # -- measurements ---------------------------------------------------------------------

    @staticmethod
    def _snapshot(context: Any, underlying: str):
        """The snapshot pricing ``underlying``: the one just matched, else the session's newest."""
        snapshot = getattr(context, "snapshot", None)
        if snapshot is not None and snapshot.underlying == underlying:
            return snapshot
        return getattr(context, "snapshots", {}).get(underlying)

    @staticmethod
    def _multiplier(intent: OptionIntent) -> int:
        return legs_of(intent.instrument, intent.side)[0].contract.multiplier

    def _entry_price(self, intent: OptionIntent, context: Any) -> Decimal | None:
        """The net per unit the entry is measured at: its limit, else the snapshot's mid
        (a share purchase after the close: the official close it was marked at)."""
        if intent.limit_price is not None:
            return intent.limit_price
        snapshot = self._snapshot(context, underlying_of(intent.instrument))
        if isinstance(intent.instrument, Equity):
            if snapshot is not None and getattr(context, "snapshot", None) is snapshot:
                return snapshot.underlying_price
            return context.state.marks.get(intent.instrument)
        if snapshot is None:
            return None
        net = ZERO
        for leg in legs_of(intent.instrument, intent.side):
            quote = snapshot.get(leg.contract)
            if quote is None:
                return None
            net += (quote.mid if leg.side is Side.SELL else -quote.mid) * leg.ratio
        net = net if intent.side is Side.SELL else -net
        return net if net > 0 else None

    def _book(self, state: AccountState, intent: OptionIntent, price: Decimal | None, context: Any) -> _Book:
        """The account's equity, and the account as if the entry had filled at ``price``."""
        marks = dict(state.marks)
        snapshot = self._snapshot(context, underlying_of(intent.instrument))
        live = getattr(context, "snapshot", None)
        if live is not None and live is snapshot:
            # At a snapshot the book is worth what these quotes say, not last night's marks.
            for instrument in state.positions:
                if isinstance(instrument, OptionContract) and underlying_of(instrument) == snapshot.underlying:
                    quote = snapshot.get(instrument)
                    if quote is not None and quote.mid > 0:
                        marks[instrument] = quote.mid
            marks[Equity(snapshot.underlying)] = snapshot.underlying_price
        value = ZERO
        for instrument, position in state.positions.items():
            if position.quantity == 0:
                continue
            mark = marks.get(instrument)
            if mark is None:
                return _Book(None, None, f"no mark for {instrument.symbol}")
            value += position.quantity * mark * instrument.multiplier
        equity = state.cash + value
        if price is None:
            return _Book(equity, None, "the entry has no price")
        positions = dict(state.positions)
        legs = legs_of(intent.instrument, intent.side)
        cash = state.cash
        for leg in legs:
            contracts = intent.quantity * leg.ratio
            signed = contracts if leg.side is Side.BUY else -contracts
            leg_price = self._leg_price(leg, price, legs, snapshot)
            if leg_price is None:
                return _Book(equity, None, f"no price for {leg.contract.symbol}")
            current = positions.get(leg.contract)
            quantity = (ZERO if current is None else current.quantity) + signed
            positions[leg.contract] = Position(
                account_id=state.account_id, instrument=leg.contract, quantity=quantity, avg_cost=leg_price
            )
            marks.setdefault(leg.contract, leg_price)
            cash -= signed * leg_price * leg.contract.multiplier
            if isinstance(leg.contract, OptionContract):
                marks.setdefault(Equity(underlying_of(leg.contract)), self._underlying_price(leg.contract, state, snapshot))
        marks = {k: v for k, v in marks.items() if v is not None}
        after = replace(state, positions=MappingProxyType(positions), marks=MappingProxyType(marks), cash=cash)
        return _Book(equity, after, None)

    @staticmethod
    def _underlying_price(contract: OptionContract, state: AccountState, snapshot) -> Decimal | None:
        underlying = underlying_of(contract)
        if snapshot is not None and snapshot.underlying == underlying:
            return snapshot.underlying_price
        return state.marks.get(Equity(underlying))

    @staticmethod
    def _leg_price(leg, net: Decimal, legs, snapshot) -> Decimal | None:
        """Each leg's price: its own quote where the snapshot has one, else the net for a
        single leg. A combo without quotes has no per-leg price to value it by."""
        if len(legs) == 1:
            return net
        if snapshot is None:
            return None
        quote = snapshot.get(leg.contract)
        return quote.mid if quote is not None and quote.mid > 0 else None

    @staticmethod
    def _max_loss(intent: OptionIntent, price: Decimal) -> Decimal | None:
        """The structure's worst case at expiry, or None when it has no bound."""
        legs = legs_of(intent.instrument, intent.side)
        multiplier = legs[0].contract.multiplier
        units = intent.quantity
        if isinstance(intent.instrument, Equity):
            return price * units  # shares can go to zero
        if len(legs) == 1:
            leg = legs[0]
            if leg.side is Side.BUY:
                return price * multiplier * units
            if leg.contract.right is OptionRight.PUT:
                return (leg.contract.strike - price) * multiplier * units
            return None  # a naked call
        if len(legs) == 2:
            a, b = legs
            same = a.contract.right is b.contract.right and a.contract.expiry == b.contract.expiry and a.ratio == b.ratio == 1
            if same and a.side is not b.side:
                width = abs(a.contract.strike - b.contract.strike)
                if intent.side is Side.SELL:
                    return (width - price) * multiplier * units
                return price * multiplier * units
        return None

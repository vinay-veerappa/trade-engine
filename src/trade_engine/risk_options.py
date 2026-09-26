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
  margin call). An entry that leaves it no higher than before (a call written on held
  shares) reduces risk and passes, as it would at a broker.
- ``name_margin``: the Reg-T maintenance of one underlying's strategies and shares at
  most ``max_name_margin_frac`` of equity. The owner's reading of §6.2's "10% per name"
  (2026-09-24): measured in cash, it would allow only strikes up to $50 on $50,000. An
  entry that leaves the name's margin no higher passes, like ``margin``.
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
- ``earnings``: with ``no_earnings_before_expiry``, no earnings date on or before a
  short leg expires. Earnings risk is short premium's, so a long option (a PMCC's LEAPS)
  passes. A date the source cannot give refuses.
- ``entry_quote.*``: an entry that opens a short put (a cash-secured put, a bull put
  spread) is re-checked on the quotes it is decided on against the gates of the scan that
  chose it (``EntryQuoteRules``). Entered the next morning, a gap or an overnight repricing
  that would have kept the scan from picking the contract refuses the entry. A quote,
  greek or open interest the snapshot does not carry refuses it too (I5).
- ``duplicate_entry`` (C4) and ``covered_calls`` (C3): the OMS guards, measured here too,
  so a strategy that proposes one is refused and recorded rather than failing the run.
- ``duplicate_protection`` and ``persistent_kill_switch``: as for equity entries.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal
from types import MappingProxyType
from typing import Any, Protocol

from trade_engine.domain.instruments import Combo, Equity, OptionContract, OptionRight, Side
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
class EntryQuoteRules:
    """What a short put entry's quotes must still show when it is entered.

    Every bound is optional; one left None is not checked. Each is measured on the
    snapshot the entry is decided on:

    - ``short_put_abs_delta``: (low, high) for each short put's |delta|.
    - ``min_short_bid``: each short put's bid must be above it.
    - ``short_bid_return``: (low, high) for each short put's bid / strike.
    - ``min_short_implied_vol``: each short put's implied vol at least this, in the units
      the snapshot stores it in: a fraction (0.70 is 70%; the hub source divides Schwab's percent by 100).
    - ``min_open_interest``: each short put's open interest at least this (the scan
      measures the contract it sells; a vertical's wing is judged by the friction).
    - ``max_leg_spread_frac``: each leg's (ask - bid) / mid at most this.
    - ``min_underlying_price``: the underlying at least this.
    - For a bull put vertical, on credit = short bid - long ask: ``min_credit_width_frac``
      (credit / width), ``min_credit_return`` (credit / (width - credit)) and
      ``max_friction_frac`` ((short spread + long spread) / credit).
    """

    short_put_abs_delta: tuple[Decimal, Decimal] | None = None
    min_short_bid: Decimal | None = None
    short_bid_return: tuple[Decimal, Decimal] | None = None
    min_short_implied_vol: Decimal | None = None
    min_open_interest: int | None = None
    max_leg_spread_frac: Decimal | None = None
    min_underlying_price: Decimal | None = None
    min_credit_width_frac: Decimal | None = None
    min_credit_return: Decimal | None = None
    max_friction_frac: Decimal | None = None

    def __post_init__(self) -> None:
        for name in ("short_put_abs_delta", "short_bid_return"):
            bounds = getattr(self, name)
            if bounds is None:
                continue
            if (
                not isinstance(bounds, tuple)
                or len(bounds) != 2
                or not all(isinstance(v, Decimal) and v.is_finite() and v >= 0 for v in bounds)
                or bounds[0] > bounds[1]
            ):
                raise OptionRiskConfigurationError(f"{name} must be a (low, high) pair of Decimals, got {bounds!r}")
        for name in (
            "min_short_bid",
            "min_short_implied_vol",
            "max_leg_spread_frac",
            "min_underlying_price",
            "min_credit_width_frac",
            "min_credit_return",
            "max_friction_frac",
        ):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, Decimal) or not value.is_finite() or value < 0):
                raise OptionRiskConfigurationError(f"{name} must be a non-negative Decimal, got {value!r}")
        interest = self.min_open_interest
        if interest is not None and (not isinstance(interest, int) or isinstance(interest, bool) or interest < 0):
            raise OptionRiskConfigurationError(f"min_open_interest must be a non-negative int, got {interest!r}")


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
    entry_quote: EntryQuoteRules | None = None

    def __post_init__(self) -> None:
        _fraction(self.max_margin_frac, "max_margin_frac")
        if self.entry_quote is not None and not isinstance(self.entry_quote, EntryQuoteRules):
            raise OptionRiskConfigurationError(f"entry_quote must be EntryQuoteRules, got {type(self.entry_quote).__name__}")
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
    before: AccountState | None = None  # the account now, at the same prices


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
            ", ".join(sorted(rules.allowed_regimes)),
            "Regime permits options entries",
            "Regime is unknown or not one this account enters in",
        )

        # margin, measured on the whole book with the entry in it
        margin = previous = None
        if book.after is not None:
            try:
                margin = account_margin(book.after)
                previous = account_margin(book.before)
            except (OptionMarginError, ValueError) as err:
                margin, book_error = None, str(err)
        cap = equity * rules.max_margin_frac if equity is not None and equity > 0 else None
        # An entry that leaves the requirement no higher (a call written on held shares)
        # reduces risk, and passes even on a book already over the cap.
        reduces = margin is not None and previous is not None and margin.margin_used <= previous.margin_used
        record(
            "margin",
            margin is not None and cap is not None and (margin.margin_used <= cap or reduces),
            margin.margin_used if margin is not None else f"UNKNOWN ({book_error})",
            cap if cap is not None else "UNKNOWN",
            "Reg-T requirement with the entry is within the account cap"
            if margin is None or cap is None or margin.margin_used <= cap
            else f"Reg-T requirement {margin.margin_used} is over the cap but no higher than before",
            "Reg-T requirement is unknown or would exceed the account cap",
        )

        # margin on this underlying: its strategies and any of its shares margined alone
        if rules.max_name_margin_frac is None:
            not_configured("name_margin")
        else:
            on_name = was = None
            if margin is not None and previous is not None:
                on_name, was = (
                    sum((s.maintenance for s in m.strategies if s.underlying == underlying), ZERO)
                    + sum((p.maintenance for p in m.positions if p.symbol == underlying), ZERO)
                    for m in (margin, previous)
                )
            limit = equity * rules.max_name_margin_frac if equity is not None and equity > 0 else None
            record(
                "name_margin",
                on_name is not None and limit is not None and (on_name <= limit or on_name <= was),
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

        # earnings before a short leg expires
        short_expiries = [
            leg.contract.expiry
            for leg in legs_of(intent.instrument, intent.side)
            if leg.side is Side.SELL and isinstance(leg.contract, OptionContract)
        ]
        if not rules.no_earnings_before_expiry or not is_structure(intent.instrument):
            not_configured("earnings")
        elif not short_expiries:
            # A long option carries no short premium through the report (a PMCC's LEAPS).
            results.append(RiskRuleResult("earnings", True, "n/a", "n/a", "The entry sells no option"))
        else:
            expiry = max(short_expiries)
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

        # the quotes the entry is made on, against the gates of the scan that chose it
        if rules.entry_quote is None:
            not_configured("entry_quote")
        else:
            results.extend(self._entry_quote(intent, context, rules.entry_quote))

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
                ", ".join(clash) or "none",
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

    # -- the entry's quotes ---------------------------------------------------------------

    def _entry_quote(self, intent: OptionIntent, context: Any, gates: EntryQuoteRules) -> list[RiskRuleResult]:
        """One result per configured gate, measured on the snapshot the entry is priced on."""
        legs = legs_of(intent.instrument, intent.side)
        shorts = [leg for leg in legs if leg.side is Side.SELL and isinstance(leg.contract, OptionContract)]
        if not shorts or any(leg.contract.right is not OptionRight.PUT for leg in shorts):
            return [RiskRuleResult("entry_quote", True, "n/a", "n/a", "The entry opens no short put")]
        underlying = underlying_of(intent.instrument)
        snapshot = self._snapshot(context, underlying)
        if snapshot is None:
            return [
                RiskRuleResult(
                    "entry_quote", False, "UNKNOWN", f"a {underlying} snapshot",
                    f"No {underlying} snapshot to check the entry's quotes on (I5)",
                )
            ]
        quotes = {leg.contract: snapshot.get(leg.contract) for leg in legs}
        missing = sorted(c.occ.strip() for c, q in quotes.items() if q is None)
        if missing:
            return [
                RiskRuleResult(
                    "entry_quote", False, f"no quote for {', '.join(missing)}", "every leg quoted",
                    f"The {underlying} snapshot does not quote every leg (I5)",
                )
            ]
        out: list[RiskRuleResult] = []

        def check(name: str, passed: bool, measured: object, threshold: object, what: str) -> None:
            out.append(
                RiskRuleResult(f"entry_quote.{name}", passed, measured, threshold, what if passed else f"Not so: {what}")
            )

        def each(values: list[tuple[str, object]]) -> str:
            return ", ".join(f"{occ} {'UNKNOWN' if value is None else value}" for occ, value in values)

        def ratio(value: Decimal | None) -> Decimal | None:
            return None if value is None else value.quantize(Decimal("0.0001"))

        if gates.min_underlying_price is not None:
            price = snapshot.underlying_price
            check(
                "underlying_price", price >= gates.min_underlying_price, price, f">= {gates.min_underlying_price}",
                f"{underlying} trades at {gates.min_underlying_price} or above",
            )
        short = [(leg.contract.occ.strip(), leg.contract, quotes[leg.contract]) for leg in shorts]
        if gates.short_put_abs_delta is not None:
            low, high = gates.short_put_abs_delta
            deltas = [(o, None if q.greeks is None else abs(Decimal(str(q.greeks.delta)))) for o, _, q in short]
            check(
                "delta", all(d is not None and low <= d <= high for _, d in deltas), each(deltas), f"{low}..{high}",
                "Each short put's |delta| is inside the scan's range",
            )
        if gates.min_short_bid is not None:
            check(
                "bid", all(q.bid > gates.min_short_bid for _, _, q in short), each([(o, q.bid) for o, _, q in short]),
                f"> {gates.min_short_bid}", "Each short put bids above the scan's floor",
            )
        if gates.short_bid_return is not None:
            low, high = gates.short_bid_return
            returns = [(o, q.bid / c.strike if c.strike > 0 else None) for o, c, q in short]
            check(
                "bid_return", all(r is not None and low <= r <= high for _, r in returns),
                each([(o, ratio(r)) for o, r in returns]), f"{low}..{high}",
                "Each short put's bid / strike is inside the scan's range",
            )
        if gates.min_short_implied_vol is not None:
            vols = [(o, q.implied_vol) for o, _, q in short]
            check(
                "implied_vol", all(v is not None and v >= gates.min_short_implied_vol for _, v in vols), each(vols),
                f">= {gates.min_short_implied_vol}", "Each short put's implied vol is at the scan's floor or above",
            )
        if gates.min_open_interest is not None:
            interest = [(o, q.open_interest) for o, _, q in short]
            check(
                "open_interest", all(i is not None and i >= gates.min_open_interest for _, i in interest),
                each(interest), f">= {gates.min_open_interest}",
                "Each short put's open interest is at the scan's floor or above",
            )
        if gates.max_leg_spread_frac is not None:
            spreads = [(c.occ.strip(), q.spread / q.mid if q.mid > 0 else None) for c, q in quotes.items()]
            check(
                "leg_spread", all(f is not None and f <= gates.max_leg_spread_frac for _, f in spreads),
                each([(o, ratio(f)) for o, f in spreads]), f"<= {gates.max_leg_spread_frac}",
                "Each leg's bid/ask spread is within the scan's limit",
            )
        vertical = (gates.min_credit_width_frac, gates.min_credit_return, gates.max_friction_frac)
        if isinstance(intent.instrument, Combo) and any(v is not None for v in vertical):
            longs = [leg for leg in legs if leg.side is Side.BUY]
            if (
                len(shorts) != 1
                or len(longs) != 1
                or longs[0].contract.right is not OptionRight.PUT
                or longs[0].contract.expiry != shorts[0].contract.expiry
                or longs[0].contract.strike >= shorts[0].contract.strike
            ):
                out.append(
                    RiskRuleResult(
                        "entry_quote.vertical", False, "not a bull put vertical", "one short put over one long put",
                        "The credit gates measure a bull put vertical only",
                    )
                )
                return out
            short_quote, long_quote = quotes[shorts[0].contract], quotes[longs[0].contract]
            width = shorts[0].contract.strike - longs[0].contract.strike
            credit = short_quote.bid - long_quote.ask
            if gates.min_credit_width_frac is not None:
                frac = credit / width
                check(
                    "credit_width", frac >= gates.min_credit_width_frac, ratio(frac), f">= {gates.min_credit_width_frac}",
                    "The credit is a large enough part of the width",
                )
            if gates.min_credit_return is not None:
                ret = credit / (width - credit) if ZERO < credit < width else None
                check(
                    "credit_return", ret is not None and ret >= gates.min_credit_return,
                    "UNKNOWN" if ret is None else ratio(ret), f">= {gates.min_credit_return}",
                    "The credit returns enough on the width at risk",
                )
            if gates.max_friction_frac is not None:
                friction = (short_quote.spread + long_quote.spread) / credit if credit > 0 else None
                check(
                    "friction", friction is not None and friction <= gates.max_friction_frac,
                    "UNKNOWN" if friction is None else ratio(friction), f"<= {gates.max_friction_frac}",
                    "The legs' spreads cost little enough of the credit",
                )
        return out

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
        before = replace(state, marks=MappingProxyType(marks))
        return _Book(equity, after, None, before)

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

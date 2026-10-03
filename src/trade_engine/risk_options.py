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
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from types import MappingProxyType
from typing import Any, Protocol

from trade_engine.domain.instruments import OptionContract
from trade_engine.domain.option_orders import OptionIntent, legs_of
from trade_engine.domain.risk import RiskRuleResult, RiskVerdict
from trade_engine.interfaces.clock import Clock
from trade_engine.interfaces.market_data import StaleDataError
from trade_engine.ledger import Ledger, codec
from trade_engine.ledger.state import AccountState
from trade_engine.sim import _rs
from trade_engine.sim.broker import _Carriers
from trade_engine.sim.snapshot_venue import underlying_of

REGIMES = frozenset({"BULL_EXPLOSIVE", "BULL_CHOPIER", "BEAR_PROTECTIVE"})
_TEXTS = _Carriers()


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
        """Every rule, passed or not (I11). The rules are Rust's (``te_core::risk_options``,
        P3a); this hands them the account, the snapshot's quotes, and the regime and
        earnings sources, then records the two rules that read the ledger."""
        state: AccountState = context.state
        underlying = underlying_of(intent.instrument)
        snapshot = self._snapshot(context, underlying)
        live = snapshot is not None and getattr(context, "snapshot", None) is snapshot
        session = context.session

        def regime() -> str | None:
            return self._regime_of(session)

        def earnings() -> tuple[bool, str | None]:
            try:
                found = self._earnings.next_earnings(underlying, session)
            except StaleDataError:
                return False, None
            return True, None if found is None else found.isoformat()

        rows = _rs.call(
            _rs.rs.option_risk_evaluate,
            codec.text(codec.canon(state)),
            _TEXTS.text(intent.instrument),
            intent.side.value,
            str(intent.quantity),
            None if intent.limit_price is None else str(intent.limit_price),
            _wire_rules(self.rules),
            None if snapshot is None else _wire_snapshot(snapshot, live, intent, state),
            regime,
            earnings,
        )
        results = [
            RiskRuleResult(
                name, passed, Decimal(measured) if m_dec else measured, Decimal(threshold) if t_dec else threshold, reason
            )
            for name, passed, m_dec, measured, t_dec, threshold, reason in rows
        ]

        def record(name: str, passed: bool, measured: object, threshold: object, success: str, refusal: str) -> None:
            results.append(RiskRuleResult(name, passed, measured, threshold, success if passed else refusal))

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

    @staticmethod
    def _snapshot(context: Any, underlying: str):
        """The snapshot pricing ``underlying``: the one just matched, else the session's newest."""
        snapshot = getattr(context, "snapshot", None)
        if snapshot is not None and snapshot.underlying == underlying:
            return snapshot
        return getattr(context, "snapshots", {}).get(underlying)


def _opt(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _pair(value: tuple[Decimal, Decimal] | None) -> tuple[str, str] | None:
    return None if value is None else (str(value[0]), str(value[1]))


def _wire_rules(rules: OptionRiskRules) -> tuple:
    """The validated rules as the plain values that cross into Rust (D6)."""
    q = rules.entry_quote
    quote = None
    if q is not None:
        quote = (
            _pair(q.short_put_abs_delta),
            _opt(q.min_short_bid),
            _pair(q.short_bid_return),
            _opt(q.min_short_implied_vol),
            q.min_open_interest,
            _opt(q.max_leg_spread_frac),
            _opt(q.min_underlying_price),
            _opt(q.min_credit_width_frac),
            _opt(q.min_credit_return),
            _opt(q.max_friction_frac),
        )
    by_regime = rules.put_notional_frac_by_regime
    return (
        str(rules.max_margin_frac),
        sorted(rules.allowed_regimes),
        bool(rules.no_earnings_before_expiry),
        _opt(rules.max_name_margin_frac),
        _opt(rules.max_name_collateral_frac),
        None if by_regime is None else [(k, str(v)) for k, v in by_regime.items()],
        _opt(rules.max_loss_per_structure_frac),
        _opt(rules.max_debit_per_structure_frac),
        _opt(rules.max_total_debit_frac),
        _opt(rules.max_share_notional_frac),
        quote,
    )


def _wire_snapshot(snapshot: Any, live: bool, intent: OptionIntent, state: AccountState) -> tuple:
    """The snapshot's quotes of every contract the rules may ask it for: the entry's legs
    and the options the account holds. A contract whose ``occ`` cannot be read is left
    out, so Rust reads it and refuses at the point the rule asked."""
    wanted = [leg.contract for leg in legs_of(intent.instrument, intent.side)]
    wanted += [i for i in state.positions if isinstance(i, OptionContract)]
    quotes: dict[str, tuple] = {}
    for contract in wanted:
        if not isinstance(contract, OptionContract):
            continue
        try:
            occ = contract.occ
        except Exception:  # noqa: BLE001 - Rust raises it where the rule reads it
            continue
        if occ in quotes:
            continue
        quote = snapshot.get(contract)
        if quote is None:
            continue
        quotes[occ] = (
            occ,
            str(quote.bid),
            str(quote.ask),
            _opt(quote.implied_vol),
            None if quote.greeks is None else str(quote.greeks.delta),
            quote.open_interest,
        )
    return (snapshot.underlying, str(snapshot.underlying_price), live, list(quotes.values()))

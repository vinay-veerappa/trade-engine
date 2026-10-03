"""Reg-T margin model (E8a equities, O3 options; rules doc §4/§6.1/§11).

The rules live in Rust (`te_core::margin`, docs/RUST_PORT.md P1c): initial 50% and
maintenance 25% of market value (a per-symbol override replaces them), options grouped
per underlying into strategies and margined by LEAN's formulas, shares a strategy uses
margined inside it. This module only turns an `AccountState` into what `trade_engine_rs`
takes and the answer back into the dataclasses below; no margin rule may be written
here. Pure: nothing is written to the ledger; the daily snapshot module calls this at
each session close and tracks the peak.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import trade_engine_rs as _rs

from trade_engine.domain.instruments import Combo, Equity, Instrument, OptionContract
from trade_engine.ledger.state import AccountState
from trade_engine.metrics import option_margin as _option_margin
from trade_engine.metrics.option_margin import (
    DEFINITIONS,
    StrategyMargin,
    _opt,
    _rust,
    _s,
    _Table,
    _wire_definitions,
    strategy_margin_from,
)

ZERO = Decimal("0")

INITIAL_FRACTION = Decimal(_option_margin._CONSTANTS["initial_fraction"])
MAINTENANCE_FRACTION = Decimal(_option_margin._CONSTANTS["maintenance_fraction"])

Symbol = str


@dataclass(frozen=True)
class MarginOverride:
    """A per-symbol replacement of the Reg-T fractions."""

    initial: Decimal
    maintenance: Decimal

    def __post_init__(self) -> None:
        _rust(_rs.margin_validate_override, _s(self.initial), _s(self.maintenance))


@dataclass(frozen=True)
class MarginRequirement:
    """Margin figures for one position at one mark."""

    symbol: Symbol
    quantity: Decimal
    mark: Decimal
    market_value: Decimal
    initial: Decimal
    maintenance: Decimal


@dataclass(frozen=True)
class AccountMargin:
    """Aggregated margin figures for one account at one session close."""

    equity: Decimal
    cash: Decimal
    market_value_long: Decimal
    market_value_short: Decimal
    gross_exposure: Decimal
    net_exposure: Decimal
    margin_initial: Decimal
    margin_maintenance: Decimal
    positions: tuple[MarginRequirement, ...]  # shares margined as plain stock
    strategies: tuple[StrategyMargin, ...] = ()  # option strategies, with any shares they use

    @property
    def margin_used(self) -> Decimal:
        """The binding overnight requirement is the maintenance figure."""
        return Decimal(_rust(_rs.margin_summary, _s(self.equity), _s(self.margin_maintenance))[0])

    @property
    def margin_available(self) -> Decimal:
        return Decimal(_rust(_rs.margin_summary, _s(self.equity), _s(self.margin_maintenance))[1])


def margin_requirement(
    instrument: Instrument,
    quantity: Decimal,
    mark: Decimal,
    override: MarginOverride | None = None,
) -> MarginRequirement:
    """One position's Reg-T requirement; the signed quantity picks long/short value."""
    value, initial, maintenance = _rust(
        _rs.margin_requirement,
        _s(quantity),
        _s(mark),
        None if override is None else _s(override.initial),
        None if override is None else _s(override.maintenance),
    )
    return MarginRequirement(
        symbol=instrument.symbol,
        quantity=quantity,
        mark=mark,
        market_value=Decimal(value),
        initial=Decimal(initial),
        maintenance=Decimal(maintenance),
    )


def _position(table: _Table, instrument: Instrument, state: AccountState) -> dict[str, Any]:
    position = state.positions[instrument]
    mark = state.marks.get(instrument)
    wire: dict[str, Any] = {
        "symbol": instrument.symbol,
        "quantity": _s(position.quantity),
        "mark": _opt(mark),
        "flat": position.is_flat,
        "contract": None,
        "avg_cost": None,
    }
    if isinstance(instrument, Combo):  # refused before its multiplier is ever read
        return {**wire, "kind": "combo", "multiplier": 1}
    if isinstance(instrument, OptionContract):
        return {
            **wire,
            "kind": "option",
            "multiplier": instrument.multiplier,
            "contract": table.add(instrument),
            "avg_cost": _opt(position.avg_cost),
        }
    return {**wire, "kind": "equity", "multiplier": instrument.multiplier}


def account_margin(
    state: AccountState,
    overrides: dict[Symbol, MarginOverride] | None = None,
    underlying_prices: Mapping[Symbol, Decimal] | None = None,
) -> AccountMargin:
    """Reg-T figures for one account from its folded state.

    Every open position must carry a mark at the session close — a missing mark
    refuses (I5); a guessed mark would silently understate the requirement. Options
    also need their underlying's price: from ``underlying_prices`` (an index has no
    shares to mark), else the underlying's own mark.
    """
    table = _Table()
    positions = [_position(table, instrument, state) for instrument in state.positions]
    request = {
        "cash": _s(state.cash),
        "positions": positions,
        "contracts": table.wire(),
        "equity_marks": {i.symbol: _s(m) for i, m in state.marks.items() if isinstance(i, Equity)},
        "underlying_prices": {k: _s(v) for k, v in (underlying_prices or {}).items()},
        "overrides": {k: {"initial": _s(v.initial), "maintenance": _s(v.maintenance)} for k, v in (overrides or {}).items()},
        "definitions": _wire_definitions(DEFINITIONS),
        "search_limit": _option_margin.SEARCH_LIMIT,
    }
    answer = json.loads(_rust(_rs.margin_account, json.dumps(request)))
    return AccountMargin(
        equity=Decimal(answer["equity"]),
        cash=state.cash,
        market_value_long=Decimal(answer["market_value_long"]),
        market_value_short=Decimal(answer["market_value_short"]),
        gross_exposure=Decimal(answer["gross_exposure"]),
        net_exposure=Decimal(answer["net_exposure"]),
        margin_initial=Decimal(answer["margin_initial"]),
        margin_maintenance=Decimal(answer["margin_maintenance"]),
        positions=tuple(
            MarginRequirement(
                symbol=p["symbol"],
                quantity=Decimal(p["quantity"]),
                mark=Decimal(p["mark"]),
                market_value=Decimal(p["market_value"]),
                initial=Decimal(p["initial"]),
                maintenance=Decimal(p["maintenance"]),
            )
            for p in answer["positions"]
        ),
        strategies=tuple(strategy_margin_from(table, raw) for raw in answer["strategies"]),
    )

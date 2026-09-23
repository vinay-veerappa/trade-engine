"""Tests for InstrumentResolver protocol and unresolvable symbol handling (I6)."""

from datetime import date
from decimal import Decimal

import pytest
from trade_engine.domain.instruments import (
    Equity,
    Instrument,
    InstrumentResolver,
    OptionContract,
    OptionRight,
    UnresolvableInstrumentError,
)


class DummyResolver:
    """Mock implementation of InstrumentResolver protocol."""

    def resolve(self, symbol: str) -> Instrument:
        cleaned = symbol.strip().upper()
        if cleaned in ("AAPL", "MSFT", "GOOG"):
            return Equity(cleaned)
        if len(cleaned) == 21 and (cleaned[12] in ("C", "P")):
            return OptionContract.from_occ(cleaned)
        raise UnresolvableInstrumentError(f"Cannot resolve symbol '{symbol}' into a known instrument")


def test_resolver_protocol_compliance() -> None:
    """Assert DummyResolver complies with InstrumentResolver protocol."""
    resolver = DummyResolver()
    assert isinstance(resolver, InstrumentResolver)


def test_resolver_resolves_equity_and_option() -> None:
    """Assert resolver correctly returns typed domain objects."""
    resolver = DummyResolver()
    eq = resolver.resolve("AAPL")
    assert isinstance(eq, Equity)
    assert eq.symbol == "AAPL"

    opt = resolver.resolve("AAPL  260918C00150000")
    assert isinstance(opt, OptionContract)
    assert opt.underlying == "AAPL"
    assert opt.strike == Decimal("150")


def test_resolver_raises_on_unresolvable_string() -> None:
    """Assert unresolvable string raises UnresolvableInstrumentError (I6)."""
    resolver = DummyResolver()
    with pytest.raises(UnresolvableInstrumentError, match="Cannot resolve symbol"):
        resolver.resolve("UNKNOWN_TICKER_XYZ")

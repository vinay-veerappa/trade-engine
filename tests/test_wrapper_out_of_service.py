"""Gate test: ensure old Python wrapper is out of service (Architecture §4.8, I5, I7).

1. AST scan of src/, tools/, launch/ ensuring nothing imports wrapper.py (except wrapper.py itself).
2. Behavioural tests exercising wrapper_native end-to-end through the Rust door with spec-derived expectations.
"""

from __future__ import annotations

import ast
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import logging
import os
from pathlib import Path

import pytest

from trade_engine.clock import ReplayClock
from trade_engine.domain.instruments import Equity, OptionContract
from trade_engine.interfaces.market_data import (
    Bar,
    CorporateAction,
    MarketData,
    OptionQuote,
    Quote,
    StaleData,
    StaleDataError,
)
from trade_engine.market_data.wrapper_native import StampingMarketDataWrapper

REPO_ROOT = Path(__file__).resolve().parent.parent


def _is_old_wrapper_import(node: ast.AST, file_path: Path) -> bool:
    """Check if an AST node imports the retired trade_engine.market_data.wrapper module."""
    if isinstance(node, ast.Import):
        for alias in node.names:
            if alias.name == "trade_engine.market_data.wrapper":
                return True
    elif isinstance(node, ast.ImportFrom):
        mod = node.module or ""
        if mod == "trade_engine.market_data.wrapper":
            return True
        if mod == "trade_engine.market_data" and any(alias.name == "wrapper" for alias in node.names):
            return True
        # Handle relative imports inside trade_engine/market_data
        if node.level == 1:
            # from .wrapper import ...
            if mod == "wrapper":
                return True
            # from . import wrapper
            if not mod and any(alias.name == "wrapper" for alias in node.names):
                return True
        elif node.level == 2:
            # from ..market_data.wrapper import ...
            if mod == "market_data.wrapper":
                return True
    return False


def test_wrapper_ast_scan_out_of_service() -> None:
    """AST scan of src/, tools/, launch/ fails if any live module imports wrapper.py."""
    retired_file = (REPO_ROOT / "src" / "trade_engine" / "market_data" / "wrapper.py").resolve()
    assert retired_file.is_file(), f"Retired reference file {retired_file} must exist"

    scan_dirs = [
        REPO_ROOT / "src",
        REPO_ROOT / "tools",
        REPO_ROOT / "launch",
    ]

    violations: list[str] = []

    for s_dir in scan_dirs:
        if not s_dir.is_dir():
            continue
        for py_path in s_dir.rglob("*.py"):
            resolved = py_path.resolve()
            if resolved == retired_file:
                continue

            try:
                tree = ast.parse(py_path.read_text(encoding="utf-8"), filename=str(py_path))
            except Exception as e:
                pytest.fail(f"Could not parse {py_path}: {e}")

            for node in ast.walk(tree):
                if _is_old_wrapper_import(node, resolved):
                    rel = py_path.relative_to(REPO_ROOT).as_posix()
                    lineno = getattr(node, "lineno", "?")
                    violations.append(f"{rel}:{lineno} imports retired market_data.wrapper")

    assert not violations, (
        "Retired market_data.wrapper is still imported by live code:\n"
        + "\n".join(violations)
    )


class MockProvider:
    """Test fixture provider for native wrapper behavioural tests."""

    def __init__(self) -> None:
        self.raw_bars: list[Bar] = []
        self.raw_quote: Quote | None = None
        self.raw_chain: list[OptionQuote] = []
        self.raw_actions: list[CorporateAction] = []

    def bars(self, instrument, tf, start, end, max_age_seconds):
        return self.raw_bars

    def quote(self, instrument, max_age_seconds):
        return self.raw_quote

    def chain(self, underlying, expiry_start, expiry_end, max_age_seconds):
        return self.raw_chain

    def corporate_actions(self, symbol, max_age_seconds):
        return self.raw_actions


def test_wrapper_native_quote_behaviour() -> None:
    """Behavioural test: Quote freshness and lookahead via Rust door."""
    t0 = datetime(2026, 9, 23, 14, 30, 0, tzinfo=timezone.utc)
    clock = ReplayClock(t0)
    provider = MockProvider()
    wrapper = StampingMarketDataWrapper(provider, clock=clock, future_tolerance_seconds=0.0)

    eq = Equity("SPY")

    # Fresh quote passes
    provider.raw_quote = Quote(
        instrument=eq,
        bid=Decimal("500.00"),
        ask=Decimal("500.20"),
        bid_size=Decimal("100"),
        ask_size=Decimal("200"),
        as_of=t0 - timedelta(seconds=2),
    )
    q = wrapper.quote(eq, max_age_seconds=5.0)
    assert q.bid == Decimal("500.00")
    assert q.mid == Decimal("500.10")
    assert q.spread == Decimal("0.20")

    # Stale quote raises StaleDataError
    clock.advance_by(10.0)
    with pytest.raises(StaleDataError, match="is stale"):
        wrapper.quote(eq, max_age_seconds=5.0)

    # Lookahead quote raises ValueError
    provider.raw_quote = Quote(
        instrument=eq,
        bid=Decimal("500.00"),
        ask=Decimal("500.20"),
        bid_size=Decimal("100"),
        ask_size=Decimal("200"),
        as_of=clock.now_utc() + timedelta(seconds=5),
    )
    with pytest.raises(ValueError, match="is in the future"):
        wrapper.quote(eq, max_age_seconds=10.0)


def test_wrapper_native_bars_strict_and_non_strict(caplog: pytest.LogCaptureFixture) -> None:
    """Behavioural test: Bars series warnings in non-strict mode vs error in strict mode."""
    t0 = datetime(2026, 9, 23, 14, 30, 0, tzinfo=timezone.utc)
    clock = ReplayClock(t0)
    provider = MockProvider()
    wrapper = StampingMarketDataWrapper(provider, clock=clock, future_tolerance_seconds=0.0)

    eq = Equity("SPY")
    start = t0 - timedelta(minutes=15)
    end = t0

    # Non-strict mode: out of order bars produce warnings and increment counter
    b1 = Bar(
        instrument=eq,
        timestamp=t0 - timedelta(minutes=5),
        open=Decimal("500"), high=Decimal("501"), low=Decimal("499"), close=Decimal("500.5"),
        volume=Decimal("100"), as_of=t0 - timedelta(seconds=10),
    )
    b2 = Bar(
        instrument=eq,
        timestamp=t0 - timedelta(minutes=10),  # Precedes b1 (out of order!)
        open=Decimal("499"), high=Decimal("500"), low=Decimal("498"), close=Decimal("499.5"),
        volume=Decimal("100"), as_of=t0 - timedelta(seconds=10),
    )
    provider.raw_bars = [b1, b2]

    with caplog.at_level(logging.WARNING, logger="trade_engine.market_data"):
        bars = wrapper.bars(eq, "1m", start, end, max_age_seconds=60.0)
        assert len(bars) == 2
        assert wrapper.warning_count >= 1
        assert any("chronological order" in record.message for record in caplog.records)

    # Strict mode: switches via TE_MARKET_DATA_STRICT=1 or wrapper._strict_override
    strict_wrapper = StampingMarketDataWrapper(provider, clock=clock, strict=True)
    with pytest.raises(ValueError, match="chronological order"):
        strict_wrapper.bars(eq, "1m", start, end, max_age_seconds=60.0)

    # Also test environment variable switch
    os.environ["TE_MARKET_DATA_STRICT"] = "1"
    try:
        env_strict_wrapper = StampingMarketDataWrapper(provider, clock=clock)
        with pytest.raises(ValueError, match="chronological order"):
            env_strict_wrapper.bars(eq, "1m", start, end, max_age_seconds=60.0)
    finally:
        os.environ.pop("TE_MARKET_DATA_STRICT", None)


def test_wrapper_native_chain_strict_and_non_strict(caplog: pytest.LogCaptureFixture) -> None:
    """Behavioural test: Option chain warnings in non-strict vs error in strict mode."""
    t0 = datetime(2026, 9, 23, 14, 30, 0, tzinfo=timezone.utc)
    clock = ReplayClock(t0)
    provider = MockProvider()
    wrapper = StampingMarketDataWrapper(provider, clock=clock)

    # Contract with underlying mismatch (QQQ instead of requested SPY)
    contract = OptionContract("QQQ", date(2026, 10, 16), Decimal("500"), "C")
    provider.raw_chain = [
        OptionQuote(
            contract=contract,
            bid=Decimal("5.00"), ask=Decimal("5.20"),
            bid_size=Decimal("10"), ask_size=Decimal("10"),
            as_of=t0 - timedelta(seconds=10),
        )
    ]

    # Non-strict: logs warning and returns chain
    with caplog.at_level(logging.WARNING, logger="trade_engine.market_data"):
        ch = wrapper.chain("SPY", date(2026, 10, 1), date(2026, 10, 31), max_age_seconds=60.0)
        assert len(ch) == 1
        assert wrapper.warning_count >= 1
        assert any("does not match requested underlying" in r.message for r in caplog.records)

    # Strict: raises ValueError
    strict_wrapper = StampingMarketDataWrapper(provider, clock=clock, strict=True)
    with pytest.raises(ValueError, match="does not match requested underlying"):
        strict_wrapper.chain("SPY", date(2026, 10, 1), date(2026, 10, 31), max_age_seconds=60.0)


def test_wrapper_native_corporate_actions_strict_and_non_strict(caplog: pytest.LogCaptureFixture) -> None:
    """Behavioural test: Corporate action warnings in non-strict vs error in strict mode."""
    t0 = datetime(2026, 9, 23, 14, 30, 0, tzinfo=timezone.utc)
    clock = ReplayClock(t0)
    provider = MockProvider()
    wrapper = StampingMarketDataWrapper(provider, clock=clock)

    # Action with symbol mismatch
    provider.raw_actions = [
        CorporateAction(
            symbol="MSFT",
            action_type="DIVIDEND",
            effective_date=date(2026, 10, 1),
            as_of=t0 - timedelta(seconds=10),
        )
    ]

    # Non-strict: logs warning and returns actions
    with caplog.at_level(logging.WARNING, logger="trade_engine.market_data"):
        acts = wrapper.corporate_actions("AAPL", max_age_seconds=60.0)
        assert len(acts) == 1
        assert wrapper.warning_count >= 1
        assert any("Corporate action symbol MSFT does not match" in r.message for r in caplog.records)

    # Strict: raises ValueError
    strict_wrapper = StampingMarketDataWrapper(provider, clock=clock, strict=True)
    with pytest.raises(ValueError, match="does not match requested symbol"):
        strict_wrapper.corporate_actions("AAPL", max_age_seconds=60.0)

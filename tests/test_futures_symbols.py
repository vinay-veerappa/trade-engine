"""BP-1: the futures symbol map through the pyo3 module. No rule, no table, in Python.

The spellings, the decade rule, the expiry and roll table and the front-month rule live
once, in `te_core::futures_symbols`. These tests only prove the carrier reaches them and
that Python keeps no copy.
"""

from __future__ import annotations

import ast
import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
import trade_engine_rs as _rs
from trade_engine.domain.instruments import Future, Platform

REPO_ROOT = Path(__file__).resolve().parent.parent
SOURCE_ROOT = REPO_ROOT / "src" / "trade_engine"

# The roots of the one Rust table. Used only to detect a Python copy, never to spell.
ROOTS = {"NQ", "MNQ", "ES", "MES", "YM", "MYM", "RTY", "M2K", "CL", "MCL", "GC", "MGC"}


def test_future_round_trips_through_the_pyo3_module() -> None:
    contract = Future(root="NQ", year=2026, month=12)
    assert contract.symbol == "NQZ6"
    assert contract.to_platform(Platform.NT8) == "NQ 12-26"
    assert contract.to_platform(Platform.TRADOVATE) == "NQZ6"
    assert contract.to_platform(Platform.TRADINGVIEW) == "NQZ2026"
    assert contract.to_platform(Platform.SCHWAB) == "/NQZ26"
    assert contract.to_platform(Platform.IBKR) == "NQZ6"
    assert contract.ibkr_last_trade_date == "202612"

    reference = date(2026, 11, 11)
    for platform, spelling in [
        (Platform.NT8, "NQ 12-26"),
        (Platform.TRADOVATE, "NQZ6"),
        (Platform.TRADINGVIEW, "NQZ2026"),
        (Platform.SCHWAB, "/NQZ26"),
        (Platform.IBKR, "NQZ6"),
    ]:
        back = Future.from_platform(platform, spelling, reference)
        assert back == contract
        assert back.symbol == "NQZ6"
        assert back.to_platform(platform) == spelling

    # The spec comes from the one table, as strings (D6).
    assert contract.tick_size == Decimal("0.25")
    assert contract.point_value == 20
    assert contract.tick_value == Decimal("5")
    assert contract.exchange == "CME"
    assert contract.currency == "USD"
    assert Future(root="MNQ", year=2026, month=12).tick_value == Decimal("0.50")


def test_dated_table_and_front_month_are_reached_not_reimplemented() -> None:
    contract = Future(root="NQ", year=2026, month=3)
    assert contract.expiry == date(2026, 3, 20)
    assert contract.roll_date == date(2026, 3, 12)

    # The day before the roll date is the old contract; on the roll date, the next one.
    assert Future.front_month("NQ", date(2026, 3, 11)) == contract
    assert Future.front_month("NQ", date(2026, 3, 12)) == Future(root="NQ", year=2026, month=6)
    assert Future.front_month_symbol(Platform.TRADINGVIEW, "NQ", date(2026, 3, 11)) == "NQH2026"
    assert Future.front_month_symbol(Platform.TRADINGVIEW, "NQ", date(2026, 3, 12)) == "NQM2026"
    assert Future.front_month_symbol(Platform.NT8, "NQ", date(2026, 3, 11)) == "NQ 03-26"

    # CL rolls monthly, three business days before the 25th of the prior month.
    crud = Future(root="CL", year=2026, month=3)
    assert crud.expiry == date(2026, 2, 20)
    assert Future.front_month("CL", date(2026, 2, 14)) == crud
    assert Future.front_month("CL", date(2026, 2, 15)) == Future(root="CL", year=2026, month=4)


def test_refusals_come_back_as_python_errors() -> None:
    reference = date(2026, 11, 11)
    # Unknown root.
    with pytest.raises(ValueError, match="Unknown futures root"):
        Future(root="ZN", year=2026, month=12)
    # Unknown month code in a platform spelling.
    with pytest.raises(ValueError, match="Invalid contract month code"):
        Future.from_platform(Platform.TRADOVATE, "NQA6", reference)
    # Malformed spelling and whitespace.
    with pytest.raises(ValueError):
        Future.from_platform(Platform.TRADOVATE, "NQZ2026", reference)
    with pytest.raises(ValueError):
        Future.from_platform(Platform.TRADOVATE, " NQZ6", reference)
    # Wrong case, where the spelling is uppercase.
    with pytest.raises(ValueError):
        Future.from_platform(Platform.SCHWAB, "/nqz26", reference)
    # Unknown platform.
    with pytest.raises(ValueError, match="Unknown platform"):
        Future(root="NQ", year=2026, month=12).to_platform("vela")
    # Continuous spellings never name a contract; they resolve through front_month only.
    for platform, text in [
        (Platform.NT8, "NQ"),
        (Platform.TRADOVATE, "NQ"),
        (Platform.TRADINGVIEW, "NQ1!"),
        (Platform.SCHWAB, "/NQ"),
    ]:
        assert Future.continuous_root(platform, text, reference) == "NQ"
        with pytest.raises(ValueError, match="front_month"):
            Future.from_platform(platform, text, reference)
    assert Future.continuous_root(Platform.TRADINGVIEW, "NQZ2026", reference) is None
    # A front-month date beyond the table is refused.
    with pytest.raises(ValueError, match="roll table"):
        Future.front_month("NQ", date(2031, 1, 2))
    # A month outside the index cycle has no dated row: the table refuses, never
    # extrapolates. The symbol map still spells it (B2 covers any month code).
    with pytest.raises(ValueError, match="quarterly"):
        Future(root="NQ", year=2026, month=7).expiry


def test_the_table_is_rust_and_python_holds_no_copy() -> None:
    # The read API body is Rust's, one row per root, every number a string.
    rows = json.loads(_rs.futures_table_json())
    assert {row["root"] for row in rows} == ROOTS
    for row in rows:
        assert all(isinstance(row[key], str) for key in ("tick_size", "point_value", "tick_value"))
        assert row["currency"] == "USD"
        assert row["session"] in {"equity", "energy", "metals"}

    # A gate: no Python module may tabulate futures roots, tick values or contract specs.
    offenders: list[str] = []
    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, (ast.Dict, ast.List, ast.Tuple, ast.Set)):
                literals = {
                    item.value
                    for item in ast.walk(node)
                    if isinstance(item, ast.Constant) and isinstance(item.value, str) and item.value in ROOTS
                }
                if len(literals) >= 2:
                    offenders.append(f"{path}:{node.lineno}: roots {sorted(literals)}")
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    for name in ast.walk(target):
                        if isinstance(name, ast.Name) and any(
                            token in name.id.upper()
                            for token in ("FUTURE_SPEC", "CONTRACT_SPEC", "TICK_VALUE", "SYMBOL_MAP")
                        ):
                            offenders.append(f"{path}:{node.lineno}: constant {name.id}")
    assert not offenders, "Python must not keep a futures table of its own:\n" + "\n".join(offenders)

"""P0a-T2: options OMS behaviour, each expectation written out as a literal.

Replaces the options parity oracles (test_p3a_parity, test_p3b_parity). Every value below
is worked out from the rules, not read back from a frozen oracle.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
import trade_engine_rs

from trade_engine.domain.instruments import Side
from trade_engine.domain.option_orders import CloseHolding
from trade_engine.eod.options import OptionContext
from trade_engine.ledger import EventKind, Ledger, fold
from trade_engine.oms.options import uncovered_calls

from test_option_oms import ACCOUNT, COHR, Desk, buy_shares
from test_option_risk import C55, NOW, P45, XYZ, Book, chain, evaluate, intent, rule, rules

D = Decimal


@pytest.fixture
def led(tmp_path):
    ledger = Ledger(tmp_path / "ledger.db").open()
    yield ledger
    ledger.close()


# -- uncovered short calls (oms/options.py: uncovered_calls) -----------------------------


def test_a_naked_short_call_leaves_its_whole_delivery_uncovered_and_no_shares_free() -> None:
    state = fold(Book().trade(C55, Side.SELL, "1", "1.10").events)[ACCOUNT]
    assert uncovered_calls(state, closing_counts=False) == {"XYZ": (D("100"), D("0"))}
    assert uncovered_calls(state, closing_counts=True) == {"XYZ": (D("100"), D("0"))}


def test_100_shares_under_a_short_call_are_still_free_to_sell() -> None:
    state = fold(Book().trade(XYZ, Side.BUY, "100", "50").trade(C55, Side.SELL, "1", "1.10").events)[ACCOUNT]
    assert uncovered_calls(state, closing_counts=False) == {"XYZ": (D("100"), D("100"))}
    assert uncovered_calls(state, closing_counts=True) == {"XYZ": (D("100"), D("100"))}


# -- risk: what the snapshot wiring hands the rules (risk_options.py: _wire_snapshot) ----


def test_an_entry_on_a_contract_already_held_fails_the_duplicate_rule(led) -> None:
    held = Book().trade(P45, Side.SELL, "1", "2.00")
    verdict = evaluate(led, rules(), intent(), held.context(chain()))
    assert verdict.accepted is False
    assert rule(verdict, "duplicate_entry").passed is False


def test_a_share_order_with_shares_held_passes_every_rule_it_is_checked_against(led) -> None:
    held = Book().trade(XYZ, Side.BUY, "100", "50")
    verdict = evaluate(led, rules(), intent(instrument=XYZ, side=Side.SELL, limit=None), held.context(chain()))
    assert verdict.accepted is True
    assert "duplicate_entry" not in {r.rule_name for r in verdict.evaluations}
    assert all(r.passed for r in verdict.evaluations)


# -- closing shares held outside a structure: a replay returns the same order -----------


def test_a_replayed_share_close_returns_the_same_order_and_writes_no_new_one(tmp_path) -> None:
    desk = Desk(tmp_path)
    try:
        buy_shares(desk, "100")
        first = desk.oms.close_holding(ACCOUNT, CloseHolding(COHR, D("100"), "exit", "sell-shares"))
        created = len(desk.ledger.events_of_kind(EventKind.ORDERS_CREATED, account=ACCOUNT))
        again = desk.oms.close_holding(ACCOUNT, CloseHolding(COHR, D("100"), "exit", "sell-shares"))
        assert (first.order_id, first.side, first.quantity) == ("sell-shares:holding", Side.SELL, D("100"))
        assert again.order_id == first.order_id
        assert len(desk.ledger.events_of_kind(EventKind.ORDERS_CREATED, account=ACCOUNT)) == created
    finally:
        desk.ledger.close()


# -- the option context's snapshot map defaults to empty (eod/options.py) ----------------


def test_a_close_phase_context_built_without_snapshots_has_none() -> None:
    state = fold(Book().events)[ACCOUNT]
    context = OptionContext(
        session=date(2026, 9, 25), account_id=ACCOUNT, phase="close", now=NOW, state=state, structures=(),
    )
    assert context.snapshot is None
    assert dict(context.snapshots) == {}


# -- the Rust option and OMS entry points exist (no silent skip) ------------------------


def test_the_rust_option_entry_points_are_imported_not_skipped() -> None:
    assert trade_engine_rs.option_risk_evaluate
    assert trade_engine_rs.oms_open_structures

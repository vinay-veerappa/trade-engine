"""The Rust-backed ledger fold: what a caller sees when a fold refuses, and the atomic contract.

Each expectation is written out. The incremental path (carriers patched between reads)
is covered by the store and reader tests; this file pins the refusal contract and the
seeded cache's bounds.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
import trade_engine_rs as rs  # a missing module is an error, never a skip

from ledger_gen import event_zoo, kind_of
from trade_engine.ledger import codec
from trade_engine.ledger.events import EventKind
from trade_engine.ledger.state import AccountState, FoldCache, IncrementalFold, LedgerFoldError, fold_account


def _row(event, seq: int, payload_json: str | None = None) -> tuple:
    return (
        event.kind.value,
        codec.payload_text(event.payload) if payload_json is None else payload_json,
        event.ts_utc.isoformat(),
        event.command_id,
        event.schema_version,
        seq,
    )


def _two_events():
    """Two events that fold on an empty account: a cash flow, then a signal."""
    zoo = event_zoo()
    first = next(e for e in zoo if e.kind.value == "CashFlow")
    second = next(e for e in zoo if e.kind.value == "SignalSeen")
    return first.account, first, replace(second, account=first.account)


def _orphan_fill(account: str):
    """A fill for an order this account never placed: refused as an unknown order (I5)."""
    fill = next(e for e in event_zoo() if e.kind.value == "Fill")
    return replace(fill, account=account)


# --- an empty log and an unhandled kind ----------------------------------------------


def test_an_empty_log_folds_to_the_default_state():
    assert fold_account([], "ACC") == AccountState(account_id="ACC")


def test_a_corporate_action_is_refused_as_unhandled():
    corporate = [e for e in event_zoo() if e.kind is EventKind.CORPORATE_ACTION]
    assert len(corporate) == 1
    with pytest.raises(Exception) as err:  # noqa: B017 - the refusal's kind is the contract
        fold_account(corporate, corporate[0].account)
    assert kind_of(err.value) == "unhandled"


def test_an_undecodable_event_is_refused_by_the_rust_fold():
    with pytest.raises(ValueError):
        rs.ledger_fold([b"not json"], "ACC")
    with pytest.raises(ValueError):
        rs.ledger_fold([b"{}"], "ACC")


# --- the handle's refusal contract ---------------------------------------------------


@pytest.mark.parametrize("atomic", [False, True])
def test_an_undecodable_row_drops_the_account_unless_atomic(atomic):
    account, first, second = _two_events()
    handle = rs.LedgerFold(atomic)
    handle.load(account, [_row(first, 1)])
    before = handle.export(account, True)
    with pytest.raises(ValueError):
        handle.apply_row1(account, *_row(second, 2, payload_json="not json"))
    assert handle.has(account) is atomic
    if atomic:
        assert handle.export(account, True) == before


@pytest.mark.parametrize("atomic", [False, True])
def test_a_refused_event_drops_the_account_unless_atomic(atomic):
    account, first, _ = _two_events()
    handle = rs.LedgerFold(atomic)
    handle.load(account, [_row(first, 1)])
    before = handle.export(account, True)
    with pytest.raises(ValueError):
        handle.apply_row1(account, *_row(_orphan_fill(account), 2))
    assert handle.has(account) is atomic
    if atomic:
        assert handle.export(account, True) == before


def test_an_atomic_fold_names_the_unknown_order_and_its_invariant():
    account, first, _ = _two_events()
    fold = IncrementalFold(atomic=True)
    fold.apply_event(replace(first, seq=1))
    with pytest.raises(LedgerFoldError, match=r"Fill f1 references unknown order 'o1' \(I5\)"):
        fold.apply_event(replace(_orphan_fill(account), seq=2))
    assert fold.has(account) is True
    assert fold.state(account).last_seq == 1


def test_a_non_atomic_refusal_drops_the_account_and_its_carrier():
    account, first, _ = _two_events()
    fold = IncrementalFold(atomic=False)
    fold.apply_event(replace(first, seq=1))
    assert fold.state(account).last_seq == 1  # the carrier is built, then cached
    with pytest.raises(LedgerFoldError):
        fold.apply_event(replace(_orphan_fill(account), seq=2))
    assert fold.has(account) is False
    assert fold.accounts() == []


# --- FoldCache ----------------------------------------------------------------------


def test_a_seeded_cache_refuses_the_base_seq_itself_and_accepts_the_next():
    account, first, _ = _two_events()
    seed = {account: AccountState(account_id=account)}
    cache = FoldCache(seed=seed, base_seq=5)
    with pytest.raises(LedgerFoldError, match="already folded into the seed"):
        cache.extend([replace(first, seq=5)])
    cache.extend([replace(first, seq=6)])
    assert cache.state(account).last_seq == 6


def test_a_cache_lists_its_accounts_sorted():
    _, first, _ = _two_events()
    cache = FoldCache([replace(first, seq=1), replace(first, account="AAA", seq=2)])
    assert cache.accounts == ("AAA", "ACC")


def test_a_cache_reports_an_unseen_account_as_its_default_state():
    assert FoldCache().state("NOBODY") == AccountState(account_id="NOBODY")

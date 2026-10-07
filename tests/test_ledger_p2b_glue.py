"""P2b glue: the incremental Rust fold, read back as Python carriers, equals the frozen
pre-port Python fold after EVERY event.

The parity tests (`test_ledger_fold_parity.py`) fold a whole stream and read it once,
so the carrier is always built whole. Production reads incrementally: a carrier is built
once, then patched from Rust's delta (`+t` fills, `+fs` fill ids, `+m` maps) on each
later read. These tests read between appends, so a patch that drops, duplicates or
replaces anything diverges from the oracle at the step it happens.

Also pinned here: the handle's refusal contract (atomic keeps the account as it was,
non-atomic drops it) and `FoldCache`'s seeded `base_seq` boundary.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
import trade_engine_rs as rs  # D5: a missing module is an error, never a skip

from frozen_ledger.state import AccountState as OracleState
from frozen_ledger.state import apply_event as oracle_apply
from ledger_gen import canon, dumps, event_zoo, kind_of, norm, random_stream
from trade_engine.ledger import codec
from trade_engine.ledger.reader import LedgerReader
from trade_engine.ledger.state import AccountState, FoldCache, LedgerFoldError
from trade_engine.ledger.store import Ledger

CACHE_SEEDS = 300
STORE_SEEDS = 100


def _oracle_step(states: dict, event) -> BaseException | None:
    """Apply one event to the oracle's states; the refusal if it refuses (states untouched)."""
    try:
        states[event.account] = oracle_apply(states.get(event.account, OracleState(account_id=event.account)), event)
    except Exception as err:  # noqa: BLE001 - classified by the caller
        return err
    return None


def _same(prod_state, oracle_state, where: str) -> None:
    # P7: by value; the oracle spells a decimal str(Decimal), production spells it canonically
    assert norm(dumps(canon(prod_state))) == norm(dumps(canon(oracle_state))), where


@pytest.mark.parametrize("read_every", [1, 3])
def test_fold_cache_read_between_events_matches_the_oracle(read_every):
    reads = refused = 0
    for seed in range(CACHE_SEEDS):
        events, _ = random_stream(seed)
        cache = FoldCache()
        oracle: dict = {}
        for i, event in enumerate(events):
            want = _oracle_step(oracle, event)
            try:
                cache.extend([event])
                got = None
            except Exception as err:  # noqa: BLE001 - compared to the oracle's refusal
                got = err
            assert (got is None) == (want is None), (seed, i, got, want)
            if got is not None:
                refused += 1
                assert kind_of(got) == kind_of(want), (seed, i, got, want)
            if i % read_every == 0 or got is not None:
                # a refused event (atomic) leaves every account exactly as it was
                for account, state in oracle.items():
                    _same(cache.state(account), state, f"seed={seed} event={i} {account}")
                reads += 1
        assert set(cache.accounts) == set(oracle), seed
        for account, state in oracle.items():
            _same(cache.state(account), state, f"seed={seed} end {account}")
    assert reads > 5000 // read_every and refused > 50, (reads, refused)


def test_store_and_reader_read_between_appends_match_the_oracle(tmp_path):
    appended = refused = 0
    for seed in range(STORE_SEEDS):
        path = tmp_path / f"s{seed}.db"
        events, _ = random_stream(seed)
        oracle: dict = {}
        with Ledger(path) as lg:
            reader = LedgerReader(path).open()
            try:
                for i, event in enumerate(events):
                    event = replace(event, seq=None)
                    try:
                        stored = lg.append(event)
                        got = None
                    except Exception as err:  # noqa: BLE001 - compared to the oracle's refusal
                        got = err
                    if got is None:
                        want = _oracle_step(oracle, stored)
                        assert want is None, (seed, i, want)
                        appended += 1
                    else:
                        want = _oracle_step(dict(oracle), event)
                        assert want is not None and kind_of(got) == kind_of(want), (seed, i, got, want)
                        refused += 1
                    account = event.account
                    if account in oracle:
                        where = f"seed={seed} event={i} {account}"
                        _same(lg.state(account), oracle[account], "store " + where)
                        _same(reader.state(account), oracle[account], "reader " + where)
            finally:
                reader.close()
    assert appended > 1000 and refused > 50, (appended, refused)


# --- the handle's refusal contract ----------------------------------------------------


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
    zoo_fill = next(e for e in event_zoo() if e.kind.value == "Fill")
    orphan = replace(zoo_fill, account=account)  # a fill for an order this account never placed
    handle = rs.LedgerFold(atomic)
    handle.load(account, [_row(first, 1)])
    before = handle.export(account, True)
    with pytest.raises(ValueError):
        handle.apply_row1(account, *_row(orphan, 2))
    assert handle.has(account) is atomic
    if atomic:
        assert handle.export(account, True) == before


def test_a_seeded_cache_refuses_the_base_seq_itself_and_accepts_the_next():
    account, first, _ = _two_events()
    seed = {account: AccountState(account_id=account)}
    cache = FoldCache(seed=seed, base_seq=5)
    with pytest.raises(LedgerFoldError, match="already folded into the seed"):
        cache.extend([replace(first, seq=5)])
    cache.extend([replace(first, seq=6)])
    assert cache.state(account).last_seq == 6

"""W7-CORE-C3: the chain snapshot store's file I/O lives in Rust (`ChainStoreBase`).

The Python `ChainSnapshotStore` is now a thin subclass of `trade_engine_rs.ChainStoreBase`.
These tests exercise the store end-to-end against `tmp_path` only: the on-disk layout and
gzip bytes are interchangeable with the pre-port Python code, and the concurrent writers
tv's options passes rely on all land.
"""

from __future__ import annotations

import gzip
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from trade_engine.domain.instruments import OptionContract, OptionRight
from trade_engine.interfaces.market_data import OptionQuote, StaleDataError
from trade_engine.market_data.chains import ChainSnapshot, ChainSnapshotStore

T0 = datetime(2026, 9, 24, 19, 45, tzinfo=UTC)


def snapshot(as_of: datetime = T0, price: str = "610") -> ChainSnapshot:
    quote = OptionQuote(
        contract=OptionContract("SPY", date(2026, 10, 16), Decimal("600"), OptionRight.PUT),
        bid=Decimal("1.00"),
        ask=Decimal("1.10"),
        bid_size=Decimal(10),
        ask_size=Decimal(10),
        as_of=as_of,
    )
    return ChainSnapshot(
        underlying="SPY",
        as_of=as_of,
        underlying_price=Decimal(price),
        quotes=(quote,),
        rate=Decimal("0.04"),
        dividend_yield=Decimal("0.01"),
        source="test",
    )


def _name(as_of: datetime) -> str:
    # The file name the store gives a stamp (previously `ChainSnapshotStore._name`).
    return as_of.astimezone(UTC).strftime("%Y%m%dT%H%M%S%fZ") + ".json.gz"


def test_a_file_written_by_the_old_python_code_is_read_back_and_reput_is_a_noop(tmp_path) -> None:
    store = ChainSnapshotStore(tmp_path)
    snap = snapshot()
    folder = tmp_path / "SPY"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / _name(snap.as_of)
    path.write_bytes(gzip.compress(snap.to_json().encode("utf-8"), compresslevel=6, mtime=0))

    assert store.put(snap) == path  # the identical snapshot re-put is a no-op
    assert store.load("SPY", snap.as_of) == snap
    assert store.latest("SPY", snap.as_of + timedelta(minutes=1), max_age_seconds=600) == snap


def test_the_new_put_writes_gzip_that_python_decompresses_to_the_json(tmp_path) -> None:
    store = ChainSnapshotStore(tmp_path)
    snap = snapshot()
    path = store.put(snap)
    raw = path.read_bytes()
    assert raw[:2] == bytes((0x1F, 0x8B))  # the gzip magic number
    assert gzip.decompress(raw).decode("utf-8") == snap.to_json()


def test_put_of_a_different_snapshot_at_the_same_stamp_refuses(tmp_path) -> None:
    store = ChainSnapshotStore(tmp_path)
    store.put(snapshot())
    with pytest.raises(ValueError) as exc:
        store.put(snapshot(price="611"))
    assert str(exc.value) == f"A different SPY snapshot is already stored at {_name(T0)}"


def test_latest_with_nothing_at_or_before_now_refuses(tmp_path) -> None:
    store = ChainSnapshotStore(tmp_path)
    with pytest.raises(StaleDataError) as exc:
        store.latest("SPY", T0, max_age_seconds=600)
    assert str(exc.value) == f"No SPY chain snapshot at or before {T0.isoformat()} (I5)"


def test_eight_threads_putting_distinct_stamps_all_land_sorted(tmp_path) -> None:
    store = ChainSnapshotStore(tmp_path)
    stamps = [T0 + timedelta(minutes=i) for i in range(8)]
    snaps = [snapshot(as_of=stamp) for stamp in stamps]
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(store.put, snaps))
    assert store.stamps("SPY") == tuple(stamps)
    folder = tmp_path / "SPY"
    leftovers = [p.name for p in folder.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []

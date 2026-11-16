"""TE-RING-1: the account-event ring and the TosPaper broker state cross threads safely.

`AccountEventRing` and `TosBroker` were `#[pyclass(unsendable)]`: pyo3 then pins the object to
the thread that built it and prints `RuntimeError: ... is unsendable, but is being dropped on
another thread` (and leaks the object) when anything else drops it. The feed hosts build their
adapters, and so each adapter's `PollAndDiff` and its ring, on one thread and release them from
another, so the rule "only the creating thread may touch it" could not hold.

Both classes hold plain Rust data (the build proves `Send + Sync`: pyo3 0.23 refuses a
non-`unsendable` pyclass that is not), so the attribute is gone. These tests make the
cross-thread life cycle observable: a message through `sys.unraisablehook` fails them.
"""

from __future__ import annotations

import gc
import sys
import threading
from datetime import datetime, timedelta, timezone

import pytest

import trade_engine_rs as rs
from trade_engine.accounts import _rs
from trade_engine.accounts.poll import PollAndDiff
from trade_engine.interfaces.broker import AccountEvent, AccountRecord, AccountSnapshot

NOW = datetime(2026, 11, 16, 15, 0, 0, tzinfo=timezone.utc)


class _Clock:
    def now_utc(self) -> datetime:
        return NOW


class _Broker:
    """A poller's I/O shell with one fixed one-account snapshot."""

    name = "RingBroker"
    env = "paper"
    stale_after = 120

    def accounts(self) -> AccountSnapshot:
        record = AccountRecord.of(
            "tos_paper",
            "ACCT-RING-1",
            env="paper",
            status="active",
            source="tos_paper",
            account_type="margin",
            api_ref="FAKE-API-REF-RING",
            routes=(),
            confirmed_at=NOW,
        )
        return AccountSnapshot(records=(record,), confirmed_at=NOW)

    def account_events(self, since: datetime) -> list[AccountEvent]:
        return []


@pytest.fixture
def unraisable(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    """Every exception CPython could only print (a drop on the wrong thread is one)."""
    seen: list[object] = []
    monkeypatch.setattr(sys, "unraisablehook", lambda arg: seen.append(arg.exc_value))
    return seen


def _on_thread(fn):
    """Run `fn` on a fresh thread to completion and return its result (or re-raise)."""
    box: dict[str, object] = {}

    def run() -> None:
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 - handed back to the caller's thread
            box["error"] = exc

    thread = threading.Thread(target=run)
    thread.start()
    thread.join(30)
    assert not thread.is_alive()
    if "error" in box:
        raise box["error"]  # type: ignore[misc]
    return box.get("value")


def test_a_ring_built_on_one_thread_is_used_and_dropped_on_another(unraisable) -> None:
    ring = _on_thread(_rs.event_ring)
    assert len(ring) == 0 and ring.dropped == 0 and ring.oldest() is None
    assert ring.since("2026-11-16T00:00:00Z", "2026-11-16T15:00:00Z") == "[]"
    del ring
    gc.collect()
    assert unraisable == []


def test_a_poller_built_and_polled_on_a_feed_thread_hands_its_ring_to_main(unraisable) -> None:
    poller = _on_thread(lambda: PollAndDiff(_Broker(), clock=_Clock()))
    events = _on_thread(poller.poll)  # a second thread polls it: the ring is pushed to there
    assert [e.kind for e in events] == ["added"]
    assert len(poller.ring) == 1 and poller.ring.oldest() is not None
    rows = poller.since(NOW - timedelta(hours=1)) if hasattr(poller, "since") else None
    assert rows is None or len(rows) == 1
    del poller, events
    gc.collect()
    assert unraisable == []


def test_a_ring_built_on_main_is_dropped_by_the_thread_that_releases_it(unraisable) -> None:
    holder = [PollAndDiff(_Broker(), clock=_Clock())]
    holder[0].poll()
    _on_thread(holder.clear)  # the last reference dies on the worker thread
    gc.collect()
    assert unraisable == []


def test_the_tos_paper_broker_state_crosses_threads_too(unraisable) -> None:
    broker = _on_thread(lambda: rs.TosBroker("D-RING-1", ["ACCT-RING-1"], False))
    assert broker.halted is False
    _on_thread(broker.mark_connected)
    del broker
    gc.collect()
    assert unraisable == []

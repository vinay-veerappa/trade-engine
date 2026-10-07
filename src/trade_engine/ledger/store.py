"""SQLite WAL event ledger (Architecture §4.2, I1–I4).

One `events` table, append-only. The ledger is the only source of state; sinks read from
it. Properties this store is responsible for:

- **I3 idempotent by persisted key** — `append()` takes `command_id`; a replayed command
  returns the original event and writes nothing.
- **I4 single instance** — one OS file lock per ledger, acquired before the first write.
- **I2 no partial writes** — the row and its `command_id` index entry commit together, so
  a crash between write and commit leaves no event and no claimed command id. A batch
  (`extend`) is one transaction: all of it lands or none of it does.
- **I12 outbox with the event** — `append(event, outbox=...)` writes the event's outbox
  rows in the same transaction, so a crash cannot keep the event and lose its delivery.
- **I2 no poison events** — every event is folded into its account's state inside the
  transaction, before COMMIT. The log is append-only, so an event the fold refuses would
  otherwise make every later fold raise, forever.

Since P2b the fold is held in Rust (``state.IncrementalFold``): each append applies its
stored row ONCE to the account's Rust state, never a refold. A refusal, or any failure
before COMMIT, drops the touched accounts, which reload from the committed log on their
next read.

Snapshots are a cache: `snapshot(account)` must equal `fold(events())[account]`, and
`verify_snapshot()` proves it.
"""

from __future__ import annotations

import json
import os
import weakref
import sqlite3
from pathlib import Path
from datetime import datetime
from typing import Any, Callable, Iterable, Mapping, Sequence, TYPE_CHECKING

from trade_engine.interfaces.clock import Clock
from trade_engine.ledger import _rs, codec
from trade_engine.ledger.events import Event, EventKind
from trade_engine.ledger.lock import LedgerLockError
from trade_engine.ledger.outbox import DrainResult, OutboxItem, OutboxStatus
from trade_engine.ledger.state import AccountState, FoldCache, fold

OutboxSpec = Mapping[str, dict[str, Any]] | Sequence[tuple[str, dict[str, Any]]]

if TYPE_CHECKING:
    from trade_engine_rs import LedgerStore, SqlConnection


class _LockView:
    """Non-owning compatibility view of the store's actual native guard."""

    def __init__(self, ledger: Ledger) -> None:
        self._ledger = weakref.ref(ledger)

    @property
    def held(self) -> bool:
        ledger = self._ledger()
        native = None if ledger is None else ledger._native
        return native is not None and native.held


class Ledger:
    """Append-only SQLite event ledger for one file."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._native: LedgerStore | None = None
        self._lock = _LockView(self)
        self._conn: SqlConnection | None = None
        self._carriers: dict[str, tuple[int, AccountState]] = {}
        self._listeners: list[Callable[[Event], None]] = []

    def add_listener(self, callback: Callable[[Event], None]) -> None:
        """Register a callback invoked after each commit with every newly written event."""
        if callback not in self._listeners:
            self._listeners.append(callback)

    def remove_listener(self, callback: Callable[[Event], None]) -> None:
        if callback in self._listeners:
            self._listeners.remove(callback)

    def _notify_listeners(self, events: Sequence[Event]) -> None:
        # After COMMIT only (commit-then-publish): a listener never sees an event that
        # could still roll back, and a failing listener cannot undo a committed write.
        for event in events:
            for listener in list(self._listeners):
                try:
                    listener(event)
                except Exception:
                    pass

    # -- lifecycle ---------------------------------------------------------------

    def open(self) -> Ledger:
        """Acquire the single-instance lock (I4) and open the database."""
        if self._conn is not None:
            return self
        sidecar = self.path.with_name(self.path.name + ".lock")
        self._native = _rs.rs.LedgerStore(str(self.path), str(sidecar), str(os.getpid()))
        self._conn = self._native.connection()
        self._carriers.clear()
        return self

    def close(self) -> None:
        if self._native is not None:
            self._native.close()
            self._native = None
            self._conn = None

    def __enter__(self) -> Ledger:
        return self.open()

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def conn(self) -> SqlConnection:
        if self._conn is None:
            raise RuntimeError("Ledger is not open; use `with Ledger(path):`")
        return self._conn

    @property
    def _store(self) -> LedgerStore:
        self.conn  # Preserve the public unopened-ledger refusal.
        assert self._native is not None
        return self._native

    def _commit(self) -> None:
        """Commit the open transaction.

        A seam, not a feature: tests replace this to simulate a crash between the write
        and the commit, which must leave no partial event and no burned command id (I2/I3).
        """
        self.conn.execute("COMMIT")

    def _rollback(self) -> None:
        self.conn.execute("ROLLBACK")

    # -- append ------------------------------------------------------------------

    def append(self, event: Event, *, outbox: OutboxSpec | None = None) -> Event:
        """Append one event. A replayed `command_id` is a no-op returning the original (I3).

        `outbox` maps destination -> payload; those rows commit with the event (I12). A
        replayed command writes no outbox rows either.
        """
        return self._write([(event, _outbox_items(outbox))])[0]

    def extend(self, events: Iterable[Event]) -> list[Event]:
        """Append events in one transaction: all land or none do (I2).

        Each new event is folded into its account's state before COMMIT; if the fold
        refuses it, the whole batch rolls back and nothing is written (I2, I5).
        """
        return self._write([(event, []) for event in events])

    def _write(
        self, entries: Sequence[tuple[Event, Sequence[tuple[str, dict[str, Any]]]]]
    ) -> list[Event]:
        written, new_events = _rs.call(_rs.rs.ledger_store_write, self._native, entries, self)
        self._notify_listeners(new_events)
        return written

    def _rows(self, account: str, after: int | None = None) -> list[tuple]:
        """The account's stored rows as the Rust fold reads them, in seq order."""
        return _rs.call(self._store.rows, account, after)

    def fold_handle(self, account: str):
        """The Rust fold holding ``account``'s committed state, loaded if absent: what
        ``state(account)`` is built from, for Rust callers that need no Python carrier."""
        return _rs.call(self._store.fold_handle, account)

    def _committed_state(self, account: str) -> AccountState:
        revision = _rs.call(self._store.revision, account)
        previous = self._carriers.get(account)
        if previous is not None and previous[0] == revision:
            return previous[1]
        whole, data = _rs.call(self._store.export, account, previous is None)
        tree = json.loads(data)
        carrier = codec.build(tree) if whole else codec.patch(previous[1], tree)
        self._carriers[account] = (revision, carrier)
        return carrier

    # -- read --------------------------------------------------------------------

    def next_seq(self) -> int:
        return self._store.next_seq()

    def count(self) -> int:
        return self._store.count()

    def events(self, *, after: int | None = None, account: str | None = None) -> list[Event]:
        return _rs.call(self._store.events, after, account)

    def events_of_kind(self, kind: EventKind, *, account: str | None = None) -> list[Event]:
        """Events of one kind in append order, filtered in SQL: a scan for one kind
        does not decode the rest of the log."""
        return _rs.call(self._store.events, None, account, kind.value)

    def accounts(self) -> list[str]:
        """Every account with events, in order of its first event."""
        return self._store.accounts()

    def fingerprint_alias(self, legacy: str) -> str | None:
        """The fingerprint a pre-P7 command now carries, if `p7_migrate` rewrote its ledger.

        `p7_key_map(old, new, ...)` is written only by the migration tool; a ledger that was
        never migrated has no such table and answers None."""
        try:
            row = self.conn.execute("SELECT new FROM p7_key_map WHERE old = ?", (legacy,)).fetchone()
        except sqlite3.OperationalError:
            return None
        return None if row is None else row[0]

    def event_by_command(self, command_id: str) -> Event | None:
        return _rs.call(self._store.event_by_command, command_id)

    @staticmethod
    def _row_to_event(row: sqlite3.Row) -> Event:
        return codec.event_from_row(
            row["account"],
            row["kind"],
            row["payload_json"],
            row["ts_utc"],
            row["command_id"],
            row["schema_version"],
            row["seq"],
        )

    # -- fold / snapshot ---------------------------------------------------------

    def fold(self) -> dict[str, AccountState]:
        """Full fold of every event. Pure over the log (I2)."""
        return {account: codec.build_text(state) for account, state in _rs.call(self._store.fold)}

    def state(self, account: str) -> AccountState:
        """Folded state of one account, from that account's events only (I8).

        Served from the committed-state cache: this instance is the only writer (I4)
        and every append folds its events into the cache, so the cache is the fold
        of the log (I2) without re-reading it. ``verify_snapshot`` proves the two agree.
        """
        return self._committed_state(account)

    def snapshot(self, account: str, *, at_seq: int | None = None) -> AccountState:
        """Folded state for one account, with `last_seq` pinned to the snapshot point."""
        return codec.build_text(_rs.call(self._store.snapshot, account, at_seq))

    @staticmethod
    def _fold_events(events: Sequence[Event], account: str) -> AccountState:
        result = fold(events)
        return result.get(account, AccountState(account_id=account))

    def verify_snapshot(self, account: str) -> None:
        """Assert snapshot(account) == fold(events())[account] (Architecture §4.2)."""
        full = self.fold().get(account, AccountState(account_id=account))
        cached = self.snapshot(account)
        if full != cached:
            raise AssertionError(
                f"Snapshot for '{account}' disagrees with full fold (I2)"
            )

    def incremental_cache(
        self, *, after: int | None = None, accounts: Iterable[str] = ()
    ) -> FoldCache:
        """Build a FoldCache seeded from a snapshot point, extended with later events.

        `cache.verify(ledger.events())` proves the incremental path equals a full fold.
        """
        after = self.next_seq() - 1 if after is None else after
        seed: dict[str, AccountState] = {}
        for account in accounts:
            state = self.snapshot(account, at_seq=after)
            if state.last_seq == 0:
                # No events at or below the cutoff: nothing to seed. The seed must
                # carry each account's *real* last_seq — stamping the cutoff in would
                # fabricate a position the account never saw, and seeding an empty
                # account at all would add a state the honest fold does not have,
                # so verify() would fail (I2).
                continue
            seed[account] = state
        return FoldCache(self.events(after=after), seed=seed, base_seq=after)

    # -- outbox ------------------------------------------------------------------

    def enqueue_outbox(
        self,
        event_seq: int,
        destination: str,
        payload: dict[str, Any],
        *,
        created_at: datetime | None = None,
    ) -> OutboxItem:
        """Enqueue an outbox item for delivery to an external sink."""
        return _rs.rs.ledger_outbox_enqueue(self, event_seq, destination, payload, created_at)

    def _insert_outbox(
        self, event_seq: int, destination: str, payload: dict[str, Any], created_at: datetime
    ) -> int:
        """INSERT one PENDING row; the caller owns the transaction."""
        return self._store.insert_outbox(event_seq, destination, payload, created_at)

    def pending_outbox(
        self,
        destination: str | None = None,
        *,
        include_failed: bool = True,
    ) -> list[OutboxItem]:
        """Fetch undelivered outbox entries in strict FIFO order (id ASC)."""
        return self._store.pending_outbox(destination, include_failed)

    def mark_outbox_delivered(self, outbox_id: int, delivered_at: datetime) -> None:
        """Mark an outbox item delivered with confirmation timestamp."""
        _rs.rs.ledger_outbox_delivered(self, outbox_id, delivered_at)

    def mark_outbox_failed(self, outbox_id: int, error: str) -> None:
        """Mark an outbox item failed and record error message."""
        self._store.mark_outbox_failed(self, outbox_id, error)

    def drain_outbox(
        self,
        destination: str,
        publisher: Callable[[OutboxItem], bool],
        clock: Clock,
    ) -> DrainResult:
        """Drain undelivered outbox entries for destination in strict FIFO order.

        Stops at the very first failure (I12) so later items remain queued in order.
        """
        return _rs.rs.ledger_outbox_drain(self, destination, publisher, clock)

    @staticmethod
    def _row_to_outbox(row: sqlite3.Row) -> OutboxItem:
        return _rs.rs.ledger_outbox_row(row)

    # -- meta --------------------------------------------------------------------

    def set_meta(self, key: str, value: str) -> None:
        self._store.set_meta(key, value)

    def get_meta(self, key: str) -> str | None:
        return self._store.get_meta(key)

    def has_command(self, command_id: str) -> bool:
        """Return whether a persisted event already claims this idempotency key (I3)."""
        if not command_id:
            raise ValueError("command_id must be non-empty")
        return self._store.has_command(command_id)

    def schema_version(self) -> int:
        return self._store.schema_version()


def fold_events(events: Iterable[Event]) -> dict[str, AccountState]:
    """Module-level pass-through so callers need not import state.py."""
    return fold(events)


def _outbox_items(outbox: OutboxSpec | None) -> list[tuple[str, dict[str, Any]]]:
    return _rs.rs.ledger_outbox_items(outbox)


__all__ = [
    "DrainResult",
    "EventKind",
    "Ledger",
    "LedgerLockError",
    "OutboxItem",
    "OutboxStatus",
    "fold_events",
]

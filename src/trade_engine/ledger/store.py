"""SQLite WAL event ledger (Architecture §4.2, I1–I4).

One `events` table, append-only. The ledger is the only source of state; sinks read from
it. Properties this store is responsible for:

- **I3 idempotent by persisted key** — `append()` takes `command_id`; a replayed command
  returns the original event and writes nothing.
- **I4 single instance** — one OS file lock per ledger, acquired before the first write.
- **I2 no partial writes** — the row and its `command_id` index entry commit together, so
  a crash between write and commit leaves no event and no claimed command id. A batch
  (`extend`) is one transaction: all of it lands or none of it does.
- **I2 no poison events** — every event is folded into its account's state inside the
  transaction, before COMMIT. The log is append-only, so an event the fold refuses would
  otherwise make every later fold raise, forever.

Snapshots are a cache: `snapshot(account)` must equal `fold(events())[account]`, and
`verify_snapshot()` proves it.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from pathlib import Path
from typing import Any, Iterable, Sequence

from trade_engine.ledger import codec
from trade_engine.ledger.events import SCHEMA_VERSION, Event, EventKind
from trade_engine.ledger.lock import LedgerLockError, SingleInstanceLock
from trade_engine.ledger.state import AccountState, FoldCache, apply_event, fold, fold_account

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    seq            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc         TEXT    NOT NULL,
    account        TEXT    NOT NULL,
    kind           TEXT    NOT NULL,
    command_id     TEXT,
    payload_json   TEXT    NOT NULL,
    schema_version INTEGER NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_events_command_id
    ON events(command_id) WHERE command_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_events_account_seq ON events(account, seq);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class Ledger:
    """Append-only SQLite event ledger for one file."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Always locked: there is no unlocked writer, or I4 is only a convention.
        self._lock = SingleInstanceLock(self.path)
        self._lock_held = False
        self._conn: sqlite3.Connection | None = None
        # Folded state per account as of the last commit; the base append() validates
        # against. Safe to cache because this instance is the only writer (I4).
        self._states: dict[str, AccountState] = {}

    # -- lifecycle ---------------------------------------------------------------

    def open(self) -> Ledger:
        """Acquire the single-instance lock (I4) and open the database."""
        if self._conn is not None:
            return self
        self._lock.acquire()
        self._lock_held = True
        self._states = {}
        try:
            conn = sqlite3.connect(str(self.path), isolation_level=None)
            conn.row_factory = sqlite3.Row
            # synchronous=FULL, so a committed row is durable and an interrupted write
            # rolls back rather than half-applying (I2).
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=FULL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.executescript(_SCHEMA)
            self._conn = conn
        except Exception:
            if self._lock_held:
                self._lock.release()
                self._lock_held = False
            raise
        return self

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None
        if self._lock_held:
            self._lock.release()
            self._lock_held = False

    def __enter__(self) -> Ledger:
        return self.open()

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("Ledger is not open; use `with Ledger(path):`")
        return self._conn

    def _commit(self) -> None:
        """Commit the open transaction.

        A seam, not a feature: tests replace this to simulate a crash between the write
        and the commit, which must leave no partial event and no burned command id (I2/I3).
        """
        self.conn.execute("COMMIT")

    def _rollback(self) -> None:
        self.conn.execute("ROLLBACK")

    # -- append ------------------------------------------------------------------

    def append(self, event: Event) -> Event:
        """Append one event. A replayed `command_id` is a no-op returning the original (I3)."""
        return self.extend([event])[0]

    def extend(self, events: Iterable[Event]) -> list[Event]:
        """Append events in one transaction: all land or none do (I2).

        Each new event is folded into its account's state before COMMIT; if the fold
        refuses it, the whole batch rolls back and nothing is written (I2, I5).
        """
        batch = list(events)
        for event in batch:
            if event.seq is not None:
                raise ValueError("Event.seq is assigned by the ledger; pass seq=None to append")

        conn = self.conn
        staged: dict[str, AccountState] = {}
        written: list[Event] = []
        in_transaction = False
        try:
            conn.execute("BEGIN IMMEDIATE")
            in_transaction = True
            for event in batch:
                if event.command_id is not None:
                    existing = conn.execute(
                        "SELECT * FROM events WHERE command_id = ?", (event.command_id,)
                    ).fetchone()
                    if existing is not None:
                        # Decode inside the transaction so a malformed stored row surfaces
                        # before anything commits.
                        written.append(self._row_to_event(existing))
                        continue

                # Read the base state before the INSERT, which would otherwise be folded in.
                if event.account in staged:
                    current = staged[event.account]
                else:
                    current = self._committed_state(event.account)
                encoded = codec.encode_event(event)
                cursor = conn.execute(
                    "INSERT INTO events (ts_utc, account, kind, command_id, payload_json, schema_version) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        event.ts_utc.isoformat(),
                        event.account,
                        event.kind.value,
                        event.command_id,
                        json.dumps(encoded["payload"], separators=(",", ":"), sort_keys=True),
                        event.schema_version,
                    ),
                )
                appended = replace(event, seq=int(cursor.lastrowid))
                staged[event.account] = apply_event(current, appended)
                written.append(appended)
            self._commit()
            in_transaction = False
        except Exception:
            if in_transaction:
                self._rollback()
            raise
        self._states.update(staged)
        return written

    def _committed_state(self, account: str) -> AccountState:
        state = self._states.get(account)
        if state is None:
            state = fold_account(self.events(account=account), account)
            self._states[account] = state
        return state

    # -- read --------------------------------------------------------------------

    def next_seq(self) -> int:
        row = self.conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 AS n FROM events").fetchone()
        return int(row["n"])

    def count(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()
        return int(row["n"])

    def events(self, *, after: int | None = None, account: str | None = None) -> list[Event]:
        sql = "SELECT * FROM events"
        clauses: list[str] = []
        params: list[Any] = []
        if after is not None:
            clauses.append("seq > ?")
            params.append(after)
        if account is not None:
            clauses.append("account = ?")
            params.append(account)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY seq ASC"
        rows = self.conn.execute(sql, params).fetchall()
        return [self._row_to_event(row) for row in rows]

    def event_by_command(self, command_id: str) -> Event | None:
        row = self.conn.execute(
            "SELECT * FROM events WHERE command_id = ?", (command_id,)
        ).fetchone()
        return None if row is None else self._row_to_event(row)

    @staticmethod
    def _row_to_event(row: sqlite3.Row) -> Event:
        encoded = {
            "account": row["account"],
            "kind": row["kind"],
            "payload": json.loads(row["payload_json"]),
            "ts_utc": row["ts_utc"],
            "command_id": row["command_id"],
            "schema_version": row["schema_version"],
            "seq": row["seq"],
        }
        return codec.decode_event(encoded)

    # -- fold / snapshot ---------------------------------------------------------

    def fold(self) -> dict[str, AccountState]:
        """Full fold of every event. Pure over the log (I2)."""
        return fold(self.events())

    def state(self, account: str) -> AccountState:
        """Folded state of one account, from that account's events only (I8)."""
        return fold_account(self.events(account=account), account)

    def snapshot(self, account: str, *, at_seq: int | None = None) -> AccountState:
        """Folded state for one account, with `last_seq` pinned to the snapshot point."""
        events = self.events(account=account)
        if at_seq is not None:
            events = [e for e in events if e.seq is not None and e.seq <= at_seq]
        state = self._fold_events(events, account)
        return state

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

    # -- meta --------------------------------------------------------------------

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return None if row is None else str(row["value"])

    def schema_version(self) -> int:
        stored = self.get_meta("schema_version")
        return int(stored) if stored is not None else SCHEMA_VERSION


def fold_events(events: Iterable[Event]) -> dict[str, AccountState]:
    """Module-level pass-through so callers need not import state.py."""
    return fold(events)


__all__ = [
    "EventKind",
    "Ledger",
    "LedgerLockError",
    "fold_events",
]

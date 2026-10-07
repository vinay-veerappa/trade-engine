"""A read-only view of a ledger that another process writes (T2 follow, owner 2026-09-29).

The intraday service holds its ledger's OS lock from the open to the close (I4), so no
second process may open that ledger as a :class:`Ledger`. A venue mirror that follows the
service live must still read what the sim did. :class:`LedgerReader` opens the file
through SQLite's read-only mode. It takes no lock and cannot write. The ledger runs in
WAL mode, so the reader sees every committed event and never blocks the writer.

``state`` folds an account's new rows when the file has grown since the last read (P2b:
only the rows past the last fold, applied once, in Rust). The writer's cache is sound
only because it is the one writer; this instance watches someone else's writes.
"""

from __future__ import annotations

from pathlib import Path

from trade_engine.ledger import _rs
from trade_engine.ledger.store import Ledger


class LedgerReadOnlyError(RuntimeError):
    """A write, or a missing file, on a read-only ledger view."""


class LedgerReader(Ledger):
    """Every read of :class:`Ledger`, none of its writes, and no lock (I4 stays the writer's)."""

    def __init__(self, path: str | Path) -> None:  # noqa: D107 - no mkdir, no lock
        self.path = Path(path)
        self._native = None
        self._conn = None
        self._carriers = {}
        self._listeners = []

    def open(self) -> LedgerReader:
        if self._conn is not None:
            return self
        if not self.path.is_file():
            raise LedgerReadOnlyError(f"no ledger at {self.path}; a reader never creates one")
        uri = f"file:{self.path.resolve().as_posix()}?mode=ro"
        if self._native is None:
            self._native = _rs.rs.LedgerStore(str(self.path), str(self.path), "", uri)
        else:
            self._native.reopen(uri)
        self._conn = self._native.connection()
        return self

    def close(self) -> None:
        if self._native is not None:
            self._native.close()
            self._conn = None

    def _refuse(self, *_args, **_kwargs):
        raise LedgerReadOnlyError(f"{self.path} is open read-only; its writer is another process (I4)")

    append = extend = add_listener = _refuse
    enqueue_outbox = mark_outbox_delivered = mark_outbox_failed = drain_outbox = set_meta = _refuse


__all__ = ["LedgerReadOnlyError", "LedgerReader"]

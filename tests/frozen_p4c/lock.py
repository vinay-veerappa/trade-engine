"""OS-level single-instance lock for a ledger file (Architecture §2, I4).

Two processes must never write the same ledger. The lock is held on a sidecar file
(`<ledger>.lock`) by the OS for the lifetime of the process, so it is released even if
the process dies without cleaning up.

The lock file is locked, not merely created: creating it is not evidence of ownership,
and a stale lock file from a crashed run must not block a fresh start.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import IO


class LedgerLockError(RuntimeError):
    """Raised when another process already holds the ledger lock (I4)."""


def _lock_file(handle: IO[bytes]) -> bool:
    """Try to take an exclusive non-blocking lock. True if acquired."""
    if sys.platform == "win32":
        import msvcrt

        try:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False

    import fcntl

    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _unlock_file(handle: IO[bytes]) -> None:
    if sys.platform == "win32":
        import msvcrt

        try:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        return

    import fcntl

    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass


class SingleInstanceLock:
    """An exclusive, process-lifetime lock on a ledger path."""

    def __init__(self, ledger_path: Path) -> None:
        self.ledger_path = Path(ledger_path)
        self.lock_path = self.ledger_path.with_name(self.ledger_path.name + ".lock")
        self._handle: IO[bytes] | None = None
        self._pid = os.getpid()

    def acquire(self) -> None:
        """Acquire the lock, or raise LedgerLockError if another process holds it (I4)."""
        if self._handle is not None:
            return
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        # Keep the file non-empty so byte-range locking is meaningful on every platform.
        handle = open(self.lock_path, "a+b")
        if not _lock_file(handle):
            handle.close()
            raise LedgerLockError(
                f"Another process already holds the ledger lock at {self.lock_path} (I4). "
                f"Refusing to start a second writer."
            )
        handle.seek(0)
        handle.truncate(0)
        handle.write(f"pid={self._pid}\n".encode("ascii"))
        handle.flush()
        self._handle = handle

    def release(self) -> None:
        if self._handle is None:
            return
        _unlock_file(self._handle)
        self._handle.close()
        self._handle = None

    @property
    def held(self) -> bool:
        return self._handle is not None

    def __enter__(self) -> SingleInstanceLock:
        self.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()

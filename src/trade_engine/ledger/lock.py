"""Single-instance ledger lock; the OS guard is owned by te_host (I4)."""
from __future__ import annotations

import os
from pathlib import Path

from trade_engine_rs import LedgerLock


class LedgerLockError(RuntimeError):
    """Raised when another process already holds the ledger lock (I4)."""


class SingleInstanceLock:
    """An exclusive lock held until release, close, or process teardown."""

    def __init__(self, ledger_path: Path) -> None:
        self.ledger_path = Path(ledger_path)
        self.lock_path = self.ledger_path.with_name(self.ledger_path.name + ".lock")
        self._handle = LedgerLock()
        self._pid = os.getpid()

    def acquire(self) -> None:
        if not self._handle.acquire(str(self.lock_path), str(self._pid)):
            raise LedgerLockError(
                f"Another process already holds the ledger lock at {self.lock_path} (I4). "
                f"Refusing to start a second writer."
            )

    def release(self) -> None:
        self._handle.release()

    @property
    def held(self) -> bool:
        return self._handle.held

    def __enter__(self) -> SingleInstanceLock:
        self.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()

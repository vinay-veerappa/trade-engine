"""The ledger's own exception types, in one module the Rust error mapping can import (P2b).

Each is re-exported from the module that always defined it (``codec``, ``state``,
``mirror``) and keeps that module as its ``__module__``, so ``repr``, pickling and every
``from trade_engine.ledger.state import LedgerFoldError`` see the class they always did.
"""

from __future__ import annotations


class PayloadCodecError(ValueError):
    """Raised when a payload cannot be encoded or decoded without guessing."""


PayloadCodecError.__module__ = "trade_engine.ledger.codec"


class LedgerFoldError(RuntimeError):
    """Raised when a recorded event cannot be applied to state (I5)."""


class LedgerFillMismatchError(LedgerFoldError):
    """A fill that contradicts its order's instrument or side (I1, I5)."""


class LedgerDuplicateFillError(LedgerFoldError):
    """A venue fill replayed under a new command id (I3)."""


for _cls in (LedgerFoldError, LedgerFillMismatchError, LedgerDuplicateFillError):
    _cls.__module__ = "trade_engine.ledger.state"


class MirrorFoldError(RuntimeError):
    """A mirror event that contradicts the mirror state (I5). Wrapped by the ledger fold."""


MirrorFoldError.__module__ = "trade_engine.ledger.mirror"

__all__ = [
    "LedgerDuplicateFillError",
    "LedgerFillMismatchError",
    "LedgerFoldError",
    "MirrorFoldError",
    "PayloadCodecError",
]

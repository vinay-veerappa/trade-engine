"""The one door from the Python ledger into the Rust one (P2b, ``trade_engine_rs``).

A Rust refusal crosses the boundary as ``ValueError((kind, message))``; :func:`call`
raises the exception the pre-port Python raised for that kind, with Rust's message.
The rules live in Rust (``te_core::ledger``); this module only translates.

``trade_engine_rs`` missing is an ImportError, never a fallback (D5): there is no Python
fold left to fall back to.
"""

from __future__ import annotations

import decimal
import json
from typing import Any, Callable

import trade_engine_rs as rs  # noqa: F401 - D5: missing is an error, never a skip

from trade_engine.domain.instruments import UnresolvableInstrumentError
from trade_engine.domain.orders import IllegalOrderStateTransitionError
from trade_engine.ledger.errors import (
    LedgerDuplicateFillError,
    LedgerFillMismatchError,
    LedgerFoldError,
    MirrorFoldError,
    PayloadCodecError,
)
from trade_engine.ledger.events import EventPayloadError, UnhandledEventError

LedgerFold = rs.LedgerFold

_BY_KIND: dict[str, type[BaseException]] = {
    "codec": PayloadCodecError,
    "payload": EventPayloadError,
    "duplicate_fill": LedgerDuplicateFillError,
    "fill_mismatch": LedgerFillMismatchError,
    "unhandled": UnhandledEventError,
    "fold": LedgerFoldError,
    "mirror_fold": MirrorFoldError,
    "illegal_transition": IllegalOrderStateTransitionError,
    "unresolvable": UnresolvableInstrumentError,
    "value": ValueError,
    "type": TypeError,
    "attribute": AttributeError,
    # The Rust JSON reader refuses what Python's would have accepted only by guessing
    # (a float, a lone surrogate, an out-of-range number): a codec refusal here.
    "unsupported": PayloadCodecError,
    "strict": PayloadCodecError,
}

_DECIMAL: dict[str, type[decimal.DecimalException]] = {
    "invalid_operation": decimal.InvalidOperation,
    "division_by_zero": decimal.DivisionByZero,
    "overflow": decimal.Overflow,
}


def refusal(kind: str, message: str) -> BaseException:
    """The Python exception for one Rust refusal."""
    cls = _BY_KIND.get(kind)
    if cls is not None:
        return cls(message)
    if kind in _DECIMAL:
        signal = _DECIMAL[kind]
        return signal([signal])  # what the decimal context raises: str() is the signal list
    if kind == "key":
        quoted = len(message) >= 2 and message[0] == message[-1] and message[0] in "'\""
        return KeyError(message[1:-1] if quoted else message)
    if kind == "json":
        err = json.JSONDecodeError.__new__(json.JSONDecodeError)
        ValueError.__init__(err, message)
        err.msg, err.doc, err.pos, err.lineno, err.colno = message, "", 0, 1, 1
        return err
    return LedgerFoldError(f"{kind}: {message}")


def call(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Call into Rust; a refusal becomes the Python exception its kind names."""
    try:
        return fn(*args, **kwargs)
    except ValueError as err:
        # pyo3 raises `ValueError((kind, message))` with the pair as the args.
        if type(err) is ValueError and len(err.args) == 2 and all(isinstance(a, str) for a in err.args):
            raise refusal(*err.args) from None
        raise


__all__ = ["LedgerFold", "call", "refusal", "rs"]

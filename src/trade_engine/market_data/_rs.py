"""The door from market data wrappers into Rust (Architecture §4.8, I5, I7).

A Rust refusal crosses as ``ValueError((kind, message))``.
Kind ``stale_data`` maps to ``StaleDataError``, every other kind maps to ``ValueError``.
"""

from __future__ import annotations

from typing import Any, Callable

import trade_engine_rs as rs  # noqa: F401 - D5: missing is an error, never a skip

from trade_engine.interfaces.market_data import StaleDataError


def refusal(kind: str, message: str) -> BaseException:
    if kind == "stale_data":
        return StaleDataError(message)
    return ValueError(message)


def call(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    try:
        return fn(*args, **kwargs)
    except ValueError as err:
        if type(err) is ValueError and len(err.args) == 2 and all(isinstance(a, str) for a in err.args):
            raise refusal(*err.args) from None
        raise


__all__ = ["call", "refusal", "rs"]

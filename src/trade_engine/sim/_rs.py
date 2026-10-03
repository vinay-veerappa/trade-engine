"""The door from simulated venues, option-risk and OMS decisions into Rust.

A Rust refusal crosses as ``ValueError((kind, message))``. The kinds the P3a rules add
name exceptions their Python modules own, so each module registers its own class here
(including the P3b-2a manager's reconciliation and idempotency refusals);
every other kind is the ledger's (``trade_engine.ledger._rs``). An exception a host
callback raised (the clock, a quote lookup) crosses back as itself.

``trade_engine_rs`` missing is an ImportError, never a fallback (D5).
"""

from __future__ import annotations

from typing import Any, Callable

import trade_engine_rs as rs  # noqa: F401 - D5: missing is an error, never a skip

from trade_engine.domain.instruments import UnresolvableInstrumentError
from trade_engine.ledger import _rs as ledger_rs

_KINDS: dict[str, type[BaseException]] = {"unresolvable": UnresolvableInstrumentError}


def register(kind: str, cls: type[BaseException]) -> None:
    _KINDS[kind] = cls


def refusal(kind: str, message: str) -> BaseException:
    cls = _KINDS.get(kind)
    if cls is not None:
        return cls(message)
    return ledger_rs.refusal(kind, message)


def call(fn: Callable[..., Any], *args: Any) -> Any:
    try:
        return fn(*args)
    except ValueError as err:
        if type(err) is ValueError and len(err.args) == 2 and all(isinstance(a, str) for a in err.args):
            raise refusal(*err.args) from None
        raise


__all__ = ["call", "refusal", "register", "rs"]

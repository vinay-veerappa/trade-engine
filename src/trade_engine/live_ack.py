"""I10: live trading needs an explicit acknowledgement.

The operator sets ``TRADE_ENGINE_LIVE_ACK`` to the exact phrase below. Without it, a broker
adapter that would send to a live account refuses ``submit`` and ``replace`` before any client
call. ``cancel`` is never gated: it reduces risk.

This module only reads the environment. The composition sites pass the result to the adapters.
"""

from __future__ import annotations

from collections.abc import Mapping

LIVE_ACK_ENV = "TRADE_ENGINE_LIVE_ACK"
LIVE_ACK_PHRASE = "I_ACKNOWLEDGE_LIVE_TRADING"

#: The refusal code an adapter returns for a live order without the acknowledgement.
REFUSAL_CODE = "live_not_acknowledged"


class LiveNotAcknowledged(Exception):
    """Raised where a caller needs the acknowledgement and does not have it."""


def live_acknowledged(environ: Mapping[str, str]) -> bool:
    """True only when the acknowledgement variable, stripped, equals the phrase exactly."""
    return environ.get(LIVE_ACK_ENV, "").strip() == LIVE_ACK_PHRASE

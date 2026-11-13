"""The Schwab Trader API accounts adapter (BROKER_PLATFORM phase 5b, read-only).

The hub (`schwab_hub.py`) is the only Schwab client, so the adapter takes the two accounts
endpoints' response text as an argument and maps it onto the phase 2 account port. Every
rule (key, mask, diff, freshness, interval, ring) lives in ``te_core::accounts`` and is
reached through :mod:`trade_engine.accounts._rs`. Nothing here opens a socket.
"""

from trade_engine.schwab.adapter import (
    ACTIVITY_REPOLL_SECONDS,
    BROKER,
    ENV_SWITCH,
    SOURCE,
    AdapterStats,
    SchwabAccountsAdapter,
    SchwabAccountsConfig,
    map_responses,
)

__all__ = [
    "ACTIVITY_REPOLL_SECONDS",
    "BROKER",
    "ENV_SWITCH",
    "SOURCE",
    "AdapterStats",
    "SchwabAccountsAdapter",
    "SchwabAccountsConfig",
    "map_responses",
]

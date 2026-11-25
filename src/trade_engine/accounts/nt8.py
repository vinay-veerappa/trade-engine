"""NT8 Desktop as a route (BROKER_PLATFORM phase 6a).

NT8 is a platform, not a broker: there is no NT8 `BrokerAdapter`. This is the one door
to the Rust name map in ``te_core::nt8_routes``; it carries data and decides nothing
(D2, D3).

- a Tradovate-connected prop account (the riskguard firm map's prefixes) maps to
  ``tradovate:<name>``;
- an NT8-local account (``Sim101``, ``Sim*``, ``Playback*``) maps to
  ``local:nt8:<name>``, a riskguard-only key the engine's key rule refuses;
- anything else is refused with ``unknown_nt8_account``, never guessed (I5).

Both outcomes carry ``{platform: "nt8", local_name: <name>}``. The name's case is kept.

The mapping bodies live in Rust (``trade_engine_rs.nt8_map_account`` /
``nt8_map_accounts``); this module keeps the `Nt8Account` carrier and the two thin
wrappers.
"""

from __future__ import annotations

from dataclasses import dataclass

import trade_engine_rs as rs
from trade_engine.interfaces.broker import Route


@dataclass(frozen=True)
class Nt8Account:
    """One NT8 account's mapped key, whether it is local, and its NT8 route."""

    key: str
    local: bool
    route: Route


def map_account(name: str) -> Nt8Account:
    """Map one NT8 account name; an unmatched name raises ``unknown_nt8_account``."""
    return rs.nt8_map_account(name)


def map_accounts(text: str) -> tuple[Nt8Account, ...]:
    """Map the bridge's account-list JSON text, in the list's order.

    A list holding one unknown account is refused as a whole, so a refusal can never
    read as a shorter fleet.
    """
    return rs.nt8_map_accounts(text)


__all__ = ["Nt8Account", "map_account", "map_accounts"]

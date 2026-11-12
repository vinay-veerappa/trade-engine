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
"""

from __future__ import annotations

from dataclasses import dataclass

from trade_engine.accounts import _rs
from trade_engine.interfaces.broker import Route


@dataclass(frozen=True)
class Nt8Account:
    """One NT8 account's mapped key, whether it is local, and its NT8 route."""

    key: str
    local: bool
    route: Route


def map_account(name: str) -> Nt8Account:
    """Map one NT8 account name; an unmatched name raises ``unknown_nt8_account``."""
    row = _rs.nt8_route(name)
    return Nt8Account(
        key=row["key"],
        local=row["local"],
        route=Route(row["route"]["platform"], row["route"]["local_name"]),
    )


def map_accounts(text: str) -> tuple[Nt8Account, ...]:
    """Map the bridge's account-list JSON text, in the list's order.

    A list holding one unknown account is refused as a whole, so a refusal can never
    read as a shorter fleet.
    """
    return tuple(
        Nt8Account(
            key=row["key"],
            local=row["local"],
            route=Route(row["route"]["platform"], row["route"]["local_name"]),
        )
        for row in _rs.nt8_routes_from_text(text)
    )


__all__ = ["Nt8Account", "map_account", "map_accounts"]

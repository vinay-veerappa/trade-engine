"""The one door from the account port into `te_core::accounts` (D2, D3).

A Rust refusal crosses as ``ValueError((kind, message))`` and is raised here as
:class:`AccountRuleError`. Nothing in this module decides: it formats the carrier's data
as JSON text and returns what Rust produced.

``trade_engine_rs`` missing is an ImportError, never a fallback (D5).
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Callable, Iterable

import trade_engine_rs as rs  # noqa: F401 - D5: missing is an error, never a skip

from trade_engine.accounts.errors import AccountRuleError


def _call(fn: Callable[..., Any], *args: Any) -> Any:
    try:
        return fn(*args)
    except ValueError as err:
        if type(err) is ValueError and len(err.args) == 2 and all(isinstance(a, str) for a in err.args):
            raise AccountRuleError(*err.args) from None
        raise


def key(broker: str, account_id: str) -> str:
    """`broker:accountId`, case kept; an empty or whitespace id is refused."""
    return _call(rs.accounts_key, broker, account_id)


def mask(account_id: str) -> str:
    """`*` plus the last three characters; a shorter id shows `*` plus what exists."""
    return rs.accounts_display(account_id)


def interval(seconds: int) -> int:
    """A poll interval: 30 s minimum (a smaller one is refused)."""
    return _call(rs.accounts_interval, seconds)


def nt8_route(name: str) -> dict[str, Any]:
    """One NT8 account name to its key, local flag and `{platform: nt8}` route (BP-6a)."""
    return json.loads(_call(rs.nt8_route, name))


def nt8_routes_from_text(text: str) -> list[dict[str, Any]]:
    """The bridge's account-list JSON text to mapped keys and routes (BP-6a)."""
    return json.loads(_call(rs.nt8_routes_from_text, text))


def ibkr_snapshot(text: str, now: datetime) -> dict[str, Any]:
    """One TWS/IB Gateway or Client Portal text read to a snapshot's contract form (BP-6b)."""
    return json.loads(_call(rs.ibkr_snapshot, text, now.isoformat()))


def max_records() -> int:
    """The design's hard cap on one broker's account list (2,000), from Rust."""
    return rs.accounts_max_records()


def check_snapshot(snapshot: dict[str, Any]) -> None:
    """Validate a snapshot's wire form with the one Rust rule set."""
    _call(rs.accounts_snapshot_check, _text(snapshot))


def diff(
    old: dict[str, Any] | None, new: dict[str, Any], now: datetime, first_seq: int
) -> list[dict[str, Any]]:
    """The events between two snapshots' contract forms, sorted by key.

    `first_seq` is the first seq the new events get (per broker, strictly rising).
    """
    payload = None if old is None else _text(old)
    return json.loads(_call(rs.accounts_diff, payload, _text(new), now.isoformat(), first_seq))


def is_stale(snapshot: dict[str, Any], now: datetime, stale_after: int = 120) -> bool:
    """True at exactly `stale_after` and after it, false just under it."""
    return _call(rs.accounts_is_stale, _text(snapshot), now.isoformat(), stale_after)


def replay(events: Iterable[dict[str, str]]) -> dict[str, Any]:
    """Rebuild membership (a `membership_only` snapshot) from a ledger's account events."""
    return json.loads(_call(rs.accounts_replay, json.dumps(list(events), separators=(",", ":"))))


def membership(
    rows: Iterable[dict[str, Any]], scope: Iterable[str]
) -> tuple[dict[str, Any], int] | None:
    """The membership replayed from the rows of the scope's brokers, and the largest kept seq.

    None when the scope is empty or no row is in it.
    """
    found = _call(
        rs.accounts_membership, json.dumps(list(rows), separators=(",", ":")), sorted(scope)
    )
    if found is None:
        return None
    snapshot, max_seq = found
    return json.loads(snapshot), max_seq


def scope(fixed: str | None, keys: Iterable[str]) -> frozenset[str]:
    """A poller's broker scope: the fixed broker, or the brokers of the keys (`broker:accountId`)."""
    return frozenset(_call(rs.accounts_scope, fixed, list(keys)))


def event_ring():
    """The bounded ring (10,000 events or 24 h, oldest dropped and counted)."""
    return rs.AccountEventRing()


def _text(value: dict[str, Any]) -> str:
    return json.dumps(value, separators=(",", ":"))


__all__ = [
    "diff",
    "event_ring",
    "ibkr_snapshot",
    "interval",
    "is_stale",
    "key",
    "mask",
    "max_records",
    "membership",
    "nt8_route",
    "nt8_routes_from_text",
    "replay",
    "rs",
    "scope",
    "check_snapshot",
]

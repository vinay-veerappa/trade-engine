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


def event_ring():
    """The bounded ring (10,000 events or 24 h, oldest dropped and counted)."""
    return rs.AccountEventRing()


def _text(value: dict[str, Any]) -> str:
    return json.dumps(value, separators=(",", ":"))


__all__ = [
    "diff",
    "event_ring",
    "interval",
    "is_stale",
    "key",
    "mask",
    "replay",
    "rs",
    "check_snapshot",
]

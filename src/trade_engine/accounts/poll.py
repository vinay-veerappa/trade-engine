"""Poll-and-diff for brokers that cannot push (BROKER_PLATFORM §6.3, B3).

A Python I/O shell around the Rust rule: it reads the adapter's snapshot, asks
``te_core::accounts`` for the diff and keeps the events in the bounded ring. It never
decides what changed.

Rules it carries:
- the interval is 30 s at minimum and 60 s by default (a smaller value is refused);
- **any** failed read keeps the last good snapshot and emits no event (B3), so a list is
  never emptied by an outage;
- the first successful read after start diffs against the records the ledger already
  holds; with none, every account is `added`;
- exactly one last-good snapshot is kept, replaced on each good read, never appended;
- events from a successful read are appended to the ledger (when one is wired) and to
  the ring (10,000 events or 24 h, oldest dropped and counted);
- `seq` rises strictly per broker: it starts after the highest seq the ledger holds, so
  a restart continues the sequence instead of reusing it.

The host drives the timer; nothing here reads a clock (I7).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any

from trade_engine.accounts import _rs
from trade_engine.interfaces.broker import AccountEvent, AccountSnapshot, Route, _iso_z

if TYPE_CHECKING:  # the ledger is I/O, imported only for typing
    from trade_engine.ledger.store import Ledger

log = logging.getLogger(__name__)

DEFAULT_INTERVAL_SECONDS = 60

_KIND_TO_EVENT = {
    "added": "ACCOUNT_ADDED",
    "removed": "ACCOUNT_REMOVED",
    "changed": "ACCOUNT_CHANGED",
}


def _value(value: Any) -> Any:
    if isinstance(value, tuple) and value and isinstance(value[0], Route):
        return [route.as_contract() for route in value]
    return value


def _record_dict(record) -> dict[str, Any]:
    return {
        "key": record.key,
        "display": record.display,
        "env": record.env,
        "status": record.status,
        "class": record.class_,
        "firm": record.firm,
        "account_type": record.account_type,
        "routes": [route.as_contract() for route in record.routes],
        "source": record.source,
        "confirmed_at": _iso_z(record.confirmed_at),
    }


def _internal_event(event: AccountEvent) -> dict[str, Any]:
    """The internal JSON the ledger, the ring and the replay all read."""
    return {
        "kind": event.kind,
        "key": event.key,
        "display": event.display,
        "at": _iso_z(event.at),
        "seq": event.seq,
        "record": None if event.record is None else _record_dict(event.record),
        "changes": [[name, _value(old), _value(new)] for name, old, new in event.changes],
    }


def _membership_events(ledger: "Ledger") -> tuple[list[dict[str, Any]], int]:
    """The account events the ledger holds, in ledger order, and its highest seq."""
    from trade_engine.ledger.events import EventKind

    kinds = {
        EventKind.ACCOUNT_ADDED,
        EventKind.ACCOUNT_REMOVED,
        EventKind.ACCOUNT_CHANGED,
    }
    rows: list[dict[str, Any]] = []
    max_seq = 0
    for event in ledger.events():
        if event.kind not in kinds:
            continue
        payload = event.payload
        record = payload.record
        internal_kind = {
            "AccountAdded": "added",
            "AccountRemoved": "removed",
            "AccountChanged": "changed",
        }[event.kind.value]
        rows.append(
            {
                "kind": internal_kind,
                "key": payload.key,
                "display": payload.display,
                "seq": payload.seq,
                "at": _iso_z(payload.at),
                "record": None if record is None else _record_dict(record),
                "changes": [
                    [name, _value(old), _value(new)] for name, old, new in payload.changes
                ],
            }
        )
        max_seq = max(max_seq, payload.seq)
    return rows, max_seq


class PollAndDiff:
    """One last-good snapshot, the diff against it, and the ledger append (B3, I1, I2)."""

    def __init__(
        self,
        adapter,
        *,
        clock,
        ledger: "Ledger | None" = None,
        interval_seconds: int = DEFAULT_INTERVAL_SECONDS,
        stale_after: int = 120,
    ) -> None:
        # The minimum (30 s) is Rust's rule; a smaller interval refuses here (I5).
        self.interval_seconds = _rs.interval(interval_seconds)
        if stale_after < 0:
            raise ValueError("stale_after must not be negative (I5)")
        self.stale_after = stale_after
        self._adapter = adapter
        self._clock = clock
        self._ledger = ledger
        self._last_good: AccountSnapshot | None = None
        self._seq = 0
        self._ring = _rs.event_ring()

    @property
    def last_good(self) -> AccountSnapshot | None:
        """The one last good snapshot, or None before the first successful read."""
        return self._last_good

    @property
    def ring(self):
        """The bounded account-event ring (`len()`, `dropped`)."""
        return self._ring

    def _replayed_membership(self) -> dict | None:
        if self._ledger is None:
            return None
        rows, max_seq = _membership_events(self._ledger)
        if not rows:
            return None
        self._seq = max(self._seq, max_seq)
        return _rs.replay(rows)

    def poll(self) -> list[AccountEvent]:
        """One successful read's events; an empty list on any error (never a failure)."""
        try:
            snapshot = self._adapter.accounts()
        except Exception as exc:  # noqa: BLE001 - B3: any failed read keeps the last list
            log.warning("account poll failed (%s); keeping the last good list", type(exc).__name__)
            return []
        now = self._clock.now_utc()
        if self._last_good is not None:
            old = self._last_good.as_contract()
        else:
            old = self._replayed_membership()
        try:
            events = [
                AccountEvent.from_internal(row)
                for row in _rs.diff(old, snapshot.as_contract(), now, self._seq + 1)
            ]
        except Exception as exc:  # noqa: BLE001 - a bad snapshot is a failed read, not a diff
            log.warning("account snapshot refused (%s); keeping the last good list", type(exc).__name__)
            return []
        if events:
            self._append(events, now)
            self._seq = max(event.seq for event in events)
        # Exactly one last-good snapshot, replaced (never appended).
        self._last_good = snapshot
        self._ring.push(
            json.dumps([_internal_event(e) for e in events], separators=(",", ":")), _iso_z(now)
        )
        return events

    def _append(self, events: list[AccountEvent], now: datetime) -> None:
        """Journal the events (I1, I2). No ledger wired: the caller only needs the diff."""
        if self._ledger is None:
            return
        from trade_engine.ledger.events import (
            ACCOUNTS_ACCOUNT,
            AccountLedgerRecord,
            AccountMembership,
            Event,
            EventKind,
        )

        for event in events:
            record = None
            if event.record is not None:
                record = AccountLedgerRecord(
                    key=event.record.key,
                    display=event.record.display,
                    env=event.record.env,
                    status=event.record.status,
                    class_=event.record.class_,
                    firm=event.record.firm,
                    account_type=event.record.account_type,
                    routes=event.record.routes,
                    source=event.record.source,
                    confirmed_at=event.record.confirmed_at,
                )
            kind = EventKind[_KIND_TO_EVENT[event.kind]]
            self._ledger.append(
                Event(
                    account=ACCOUNTS_ACCOUNT,
                    kind=kind,
                    payload=AccountMembership(
                        key=event.key,
                        display=event.display,
                        seq=event.seq,
                        at=event.at,
                        record=record,
                        changes=event.changes,
                    ),
                    ts_utc=now,
                    command_id=f"account:{event.kind}:{event.key}:{event.seq}",
                )
            )

    def account_events(self, since: datetime) -> list[AccountEvent]:
        """Poll now and return what the ring holds at or after `since`."""
        self.poll()
        now = self._clock.now_utc()
        rows = json.loads(self._ring.since(_iso_z(since), _iso_z(now)))
        return [AccountEvent.from_internal(row) for row in rows]


__all__ = ["DEFAULT_INTERVAL_SECONDS", "PollAndDiff"]

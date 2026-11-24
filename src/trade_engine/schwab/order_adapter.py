"""OA-6: Schwab order submit, cancel and replace through the hub's order routes.

The order half of a Schwab broker adapter. It takes engine :class:`VenueOrder` objects, builds
the Schwab body (:func:`build_order_json`), and sends each through :class:`HubOrdersClient`, so
every Schwab call goes through the hub (D3). The account and position reads stay in
:mod:`trade_engine.schwab.adapter`.

* **Riskguard.** With a :class:`Precheck`, openings go through it and closes and reduces are not
  gated (the precheck decides which is which). A mixed order is split there; the opening part
  arrives here with the ``:open`` suffix and is sent as an opening instruction.
* **D-5 idempotency.** The order is recorded under its ``venue_order_id`` (the command id)
  before the call. A call with no known outcome leaves it PENDING. :meth:`resolve_pending`
  settles it from one order-list read per account: exactly one match is ACCEPTED, none is
  REJECTED ``not_at_venue``, more than one stays PENDING and is logged. Nothing is ever resent.
* **Replace timeouts.** A replace with no known outcome keeps the old order and sets
  ``replace_pending`` on the record. Until :meth:`resolve_pending` settles it, cancel and replace
  refuse locally (``unresolved_replace``). Schwab replaces by cancel-and-create, so the old id may
  be dead; the replacing order is found from the old order's link or from the list.
* **Journal.** Each record is saved to an :class:`OrderJournal` before the call and after every
  change, so a restarted adapter repeats nothing. A restored record has no :class:`VenueOrder`:
  it can be repeat-short-circuited, resolved and cancelled, but not replaced (``restored_order``).
* **Caps.** At most :data:`ORDER_ID_CAP` orders are held in memory. Only settled orders are
  dropped (oldest first); an unsettled order is never dropped. The journal is not capped.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import sqlite3
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

from trade_engine.domain import Instrument
from trade_engine.interfaces.broker import (
    Capabilities,
    OrderChanges,
    UnsupportedCapability,
    VenueAck,
    VenueOrder,
)
from trade_engine.interfaces.clock import Clock
from trade_engine.oms.precheck import OPEN_SUFFIX, Precheck
from trade_engine.schwab.orders import (
    SUPPORTED_ORDER_TYPES,
    SUPPORTED_TIFS,
    HubOrderResult,
    HubOrdersClient,
    build_order_json,
    leg_instrument,
    match_pending,
)

log = logging.getLogger(__name__)

ORDER_ID_CAP = 512
DEFAULT_RESOLVE_WINDOW_S = 120
#: Old-order statuses that mean Schwab replaced it (a replacing order exists or should).
_REPLACED_OLD = frozenset({"REPLACED", "CANCELED"})
#: Old-order statuses that mean the order is still working at the venue.
_LIVE_OLD = frozenset({
    "WORKING", "QUEUED", "ACCEPTED", "PENDING_ACTIVATION", "AWAITING_PARENT_ORDER", "AWAITING_CONDITION",
    "AWAITING_MANUAL_REVIEW", "AWAITING_STOP_CONDITION", "NEW",
})

# (account_id, instrument) -> the account's signed position (long > 0)
PositionFn = Callable[[str, Instrument], Decimal]


def _mask(account: str | None) -> str:
    return "***" + (account or "")[-3:]


# --- the journal ----------------------------------------------------------------------------------

class OrderJournal(Protocol):
    """A durable copy of the order rows, keyed by command id. Written before each call (D-5)."""

    def load(self) -> dict[str, dict]: ...

    def save(self, venue_order_id: str, row: dict) -> None: ...


class MemoryOrderJournal:
    """The default: rows live in this process only, so a restart forgets them."""

    def __init__(self) -> None:
        self._rows: dict[str, dict] = {}

    def load(self) -> dict[str, dict]:
        return json.loads(json.dumps(self._rows))

    def save(self, venue_order_id: str, row: dict) -> None:
        self._rows[venue_order_id] = json.loads(json.dumps(row))


class SqliteOrderJournal:
    """One table, one JSON row per command id. WAL; each save is its own upsert transaction."""

    def __init__(self, path: str | Path) -> None:
        self._conn = sqlite3.connect(str(path))
        self._conn.execute("PRAGMA journal_mode=WAL")
        with self._conn:
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS schwab_orders ("
                "venue_order_id TEXT PRIMARY KEY, row TEXT NOT NULL, updated_at TEXT NOT NULL)")

    def load(self) -> dict[str, dict]:
        rows = self._conn.execute("SELECT venue_order_id, row FROM schwab_orders ORDER BY rowid").fetchall()
        return {venue_order_id: json.loads(text) for venue_order_id, text in rows}

    def save(self, venue_order_id: str, row: dict) -> None:
        text = json.dumps(row, separators=(",", ":"), sort_keys=True)
        with self._conn:
            self._conn.execute(
                "INSERT INTO schwab_orders (venue_order_id, row, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(venue_order_id) DO UPDATE SET row = excluded.row, updated_at = excluded.updated_at",
                (venue_order_id, text, row["updated_at"]))

    def close(self) -> None:
        self._conn.close()


# --- records --------------------------------------------------------------------------------------

@dataclass
class _Record:
    order: VenueOrder | None  # None once restored from the journal
    account_id: str
    account_hash: str
    instruction: str
    sent_at: datetime
    status: str  # PENDING | ACCEPTED | REJECTED
    schwab_id: str | None = None
    symbol: str = ""
    quantity: Decimal = Decimal(0)
    body: dict = field(default_factory=dict)  # the Schwab place body (or the last accepted replace)
    replace_pending: VenueOrder | None = None  # the replace sent with no known outcome
    replace_body: dict | None = None  # its Schwab body; set means a replace is unresolved
    replace_sent_at: datetime | None = None


def _settled(record: _Record) -> bool:
    return record.status != "PENDING" and record.replace_body is None


def _match_terms(body: Mapping[str, Any]) -> dict[str, Any]:
    """The match fields of a sent Schwab body, so the comparison uses exactly what was sent."""
    leg = body["orderLegCollection"][0]
    return {
        "quantity": Decimal(str(leg["quantity"])),
        "order_type": body.get("orderType"),
        "price": _optional_decimal(body.get("price")),
        "stop_price": _optional_decimal(body.get("stopPrice")),
    }


def _optional_decimal(value: Any) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def _replacing_id(old: Mapping[str, Any]) -> str | None:
    collection = old.get("replacingOrderCollection")
    if isinstance(collection, list) and collection and isinstance(collection[0], Mapping):
        order_id = collection[0].get("orderId")
        if order_id not in (None, ""):
            return str(order_id)
    return None


def _row(record: _Record, updated_at: datetime) -> dict[str, Any]:
    """The journal row. The VenueOrder is not stored: the body and the match fields are enough."""
    return {
        "account_id": record.account_id,
        "account_hash": record.account_hash,
        "instruction": record.instruction,
        "symbol": record.symbol,
        "quantity": str(record.quantity),
        "body": record.body,
        "sent_at": record.sent_at.isoformat(),
        "status": record.status,
        "schwab_id": record.schwab_id,
        "replace_pending": record.replace_body,
        "replace_sent_at": None if record.replace_sent_at is None else record.replace_sent_at.isoformat(),
        "updated_at": updated_at.isoformat(),
    }


def _restore(row: Mapping[str, Any]) -> _Record:
    replace_sent = row.get("replace_sent_at")
    return _Record(
        order=None,
        account_id=row["account_id"],
        account_hash=row["account_hash"],
        instruction=row["instruction"],
        sent_at=datetime.fromisoformat(row["sent_at"]),
        status=row["status"],
        schwab_id=row.get("schwab_id"),
        symbol=row["symbol"],
        quantity=Decimal(row["quantity"]),
        body=row["body"],
        replace_body=row.get("replace_pending"),
        replace_sent_at=datetime.fromisoformat(replace_sent) if replace_sent else None,
    )


class SchwabOrderAdapter:
    name = "schwab"

    def __init__(
        self,
        client: HubOrdersClient,
        account_hashes: Mapping[str, str],
        clock: Clock,
        position: PositionFn,
        *,
        precheck: Precheck | None = None,
        resolve_window_s: int = DEFAULT_RESOLVE_WINDOW_S,
        journal: OrderJournal | None = None,
    ) -> None:
        self._client = client
        self._hashes = dict(account_hashes)
        self._clock = clock
        self._position = position
        self._precheck = precheck
        self._window = timedelta(seconds=resolve_window_s)
        self._journal: OrderJournal = journal if journal is not None else MemoryOrderJournal()
        self._orders: OrderedDict[str, _Record] = OrderedDict()
        for venue_order_id, row in self._journal.load().items():
            self._orders[venue_order_id] = _restore(row)
        self.capabilities = Capabilities(
            supported_order_types=SUPPORTED_ORDER_TYPES,
            supported_tifs=SUPPORTED_TIFS,
            supports_multi_leg=False,
            supports_native_stops=True,
            supports_streaming=False,
        )

    # --- submit -------------------------------------------------------------------------------

    def submit(self, order: VenueOrder) -> VenueAck:
        accounts = {a.account_id for a in order.allocations}
        if len(accounts) != 1:
            raise UnsupportedCapability("netted orders not supported on schwab")
        if order.venue_order_id in self._orders:
            # D-5: a command id is sent once; a repeat gets the recorded state, never a resend.
            return self._ack(order.venue_order_id, self._orders[order.venue_order_id])
        if self._precheck is not None:
            return self._precheck.submit(order, self._place)
        return self._place(order)

    def _place(self, order: VenueOrder) -> VenueAck:
        account_id = order.allocations[0].account_id
        account_hash = self._hashes.get(account_id)
        if account_hash is None:
            return self._refused(order.venue_order_id, "place", account_id, "unmapped_account")
        # The precheck's opening part of a split order: the reducing part has not filled yet,
        # so the live position still shows it; this part opens from flat.
        opening = order.venue_order_id.endswith(OPEN_SUFFIX)
        position = Decimal(0) if opening else self._position(account_id, order.instrument)
        body = build_order_json(order, position)
        record = _Record(
            order=order, account_id=account_id, account_hash=account_hash,
            instruction=body["orderLegCollection"][0]["instruction"], sent_at=self._clock.now_utc(),
            status="PENDING", symbol=leg_instrument(order)["symbol"], quantity=order.quantity, body=body,
        )
        self._remember(order.venue_order_id, record)
        self._save(order.venue_order_id, record)  # recorded before the call (D-5)
        result = self._client.place(account_hash, body)
        self._settle(record, result, "place")
        self._save(order.venue_order_id, record)
        return self._ack(order.venue_order_id, record, result)

    # --- D-5 ----------------------------------------------------------------------------------

    def resolve_pending(self) -> list[VenueAck]:
        """Settle PENDING orders and unresolved replaces from one order-list read per account.

        Never resends.
        """
        unsettled = [(vid, r) for vid, r in self._orders.items()
                     if r.status == "PENDING" or r.replace_body is not None]
        acks: list[VenueAck] = []
        by_hash: dict[str, list[tuple[str, _Record]]] = {}
        for vid, record in unsettled:
            by_hash.setdefault(record.account_hash, []).append((vid, record))
        for account_hash, items in by_hash.items():
            start = min(r.sent_at for _, r in items) - timedelta(seconds=5)
            end = max(r.replace_sent_at or r.sent_at for _, r in items) + self._window
            listed = self._client.list_orders(account_hash, start, end)
            if listed is None:
                log.warning("schwab resolve: order list read failed account=%s; %d stay PENDING",
                            _mask(items[0][1].account_id), len(items))
                acks.extend(VenueAck(vid, "PENDING", self._clock.now_utc(), "replace_unresolved")
                            for vid, r in items if r.replace_body is not None)
                continue
            for vid, record in items:
                owned = {r.schwab_id for v, r in self._orders.items() if r.schwab_id and v != vid}
                if record.replace_body is not None:
                    acks.append(self._resolve_replace(vid, record, listed, owned))
                else:
                    acks.append(self._resolve_place(vid, record, listed, owned))
        return acks

    def _resolve_place(self, vid: str, record: _Record, listed: list[dict], owned: set[str]) -> VenueAck:
        verdict, schwab_id = match_pending(
            listed,
            symbol=record.symbol,
            instruction=record.instruction,
            window=(record.sent_at - timedelta(seconds=5), record.sent_at + self._window),
            exclude=owned,
            **_match_terms(record.body),
        )
        if verdict == "accepted":
            record.status, record.schwab_id = "ACCEPTED", schwab_id
            self._save(vid, record)
            return self._ack(vid, record)
        if verdict == "not_at_venue":
            record.status = "REJECTED"
            self._save(vid, record)
            return VenueAck(vid, "REJECTED", self._clock.now_utc(), "not_at_venue")
        log.warning("schwab resolve: %s matches more than one order account=%s; stays PENDING",
                    vid, _mask(record.account_id))
        return self._ack(vid, record)

    def _resolve_replace(self, vid: str, record: _Record, listed: list[dict], owned: set[str]) -> VenueAck:
        old_id = record.schwab_id
        old = next((o for o in listed if str(o.get("orderId")) == str(old_id)), None)
        status = str(old.get("status", "")) if old is not None else ""
        replacing = _replacing_id(old) if old is not None else None
        if old is not None and status in _REPLACED_OLD:
            new_id = replacing
            if new_id is None:
                verdict, new_id = self._match_replacing(listed, record, owned | {old_id})
                if verdict == "accepted":
                    return self._adopt(vid, record, new_id)
            else:
                return self._adopt(vid, record, new_id)
        elif old is not None and status in _LIVE_OLD and replacing is None:
            verdict, _ = self._match_replacing(listed, record, owned | {old_id})
            if verdict == "not_at_venue":
                # Nothing replaced the old order and it still works: the replace did not take.
                self._clear_replace(record)
                self._save(vid, record)
                return VenueAck(vid, "REJECTED", self._clock.now_utc(), "replace_not_at_venue")
        # Old order missing, filled, ambiguous, or no replacing order found: never guess.
        log.warning("schwab resolve: replace of %s unresolved (old order %s status=%s) account=%s; stays PENDING",
                    vid, old_id, status or "missing", _mask(record.account_id))
        return VenueAck(vid, "PENDING", self._clock.now_utc(), "replace_unresolved")

    def _match_replacing(self, listed: list[dict], record: _Record,
                         skip: set[str]) -> tuple[str, str | None]:
        sent = record.replace_sent_at or record.sent_at
        return match_pending(
            listed,
            symbol=record.symbol,
            instruction=record.instruction,
            window=(sent - timedelta(seconds=5), sent + self._window),
            exclude=skip,
            **_match_terms(record.replace_body or {}),
        )

    def _adopt(self, vid: str, record: _Record, new_id: str) -> VenueAck:
        """The replace took: the replacing order is the record's order from now on."""
        if record.replace_pending is not None:
            record.order = record.replace_pending
        record.body = record.replace_body or record.body
        record.quantity = _match_terms(record.body)["quantity"]
        record.schwab_id = new_id
        record.status = "ACCEPTED"
        self._clear_replace(record)
        self._save(vid, record)
        return self._ack(vid, record)

    @staticmethod
    def _clear_replace(record: _Record) -> None:
        record.replace_pending = None
        record.replace_body = None
        record.replace_sent_at = None

    # --- cancel / replace ---------------------------------------------------------------------

    def cancel(self, venue_order_id: str) -> VenueAck:
        record = self._orders.get(venue_order_id)
        if record is None:
            return self._refused(venue_order_id, "cancel", None, "unknown_order")
        if record.replace_body is not None:
            return self._refused(venue_order_id, "cancel", record.account_id, "unresolved_replace")
        if record.schwab_id is None:
            return self._refused(venue_order_id, "cancel", record.account_id, "unresolved_order")
        result = self._client.cancel(record.account_hash, record.schwab_id)
        return self._result_ack(venue_order_id, result)

    def replace(self, venue_order_id: str, changes: OrderChanges) -> VenueAck:
        if self._precheck is not None:
            return self._precheck.replace(venue_order_id, changes, self._replace)
        return self._replace(venue_order_id, changes)

    def _replace(self, venue_order_id: str, changes: OrderChanges) -> VenueAck:
        record = self._orders.get(venue_order_id)
        if record is None:
            return self._refused(venue_order_id, "replace", None, "unknown_order")
        if record.replace_body is not None:
            return self._refused(venue_order_id, "replace", record.account_id, "unresolved_replace")
        if record.schwab_id is None:
            return self._refused(venue_order_id, "replace", record.account_id, "unresolved_order")
        if record.order is None:
            return self._refused(venue_order_id, "replace", record.account_id, "restored_order")
        updates = {}
        if changes.new_quantity is not None:
            account = record.order.allocations[0]
            updates["quantity"] = changes.new_quantity
            updates["allocations"] = (dataclasses.replace(account, quantity=changes.new_quantity),)
        if changes.new_limit_price is not None:
            updates["limit_price"] = changes.new_limit_price
        if changes.new_stop_price is not None:
            updates["stop_price"] = changes.new_stop_price
        new_order = dataclasses.replace(record.order, **updates)
        body = build_order_json(new_order, instruction=record.instruction)
        sent = self._clock.now_utc()  # before the call: the replacing order's enteredTime is after it
        result = self._client.replace(record.account_hash, record.schwab_id, body)
        if result.outcome == "ok":
            record.order = new_order
            record.body = body
            record.quantity = new_order.quantity
            # Schwab cancels the old order and opens a new one; its id may come back on Location.
            if result.order_id and result.order_id != record.schwab_id:
                record.schwab_id = result.order_id
            self._save(venue_order_id, record)
        elif result.outcome == "unknown":
            # The old order may be dead and a new one live: keep the old id, settle it in resolve_pending.
            record.replace_pending = new_order
            record.replace_body = body
            record.replace_sent_at = sent
            self._save(venue_order_id, record)
            log.warning("schwab replace %s: outcome unknown; old order kept, resolve_pending settles it",
                        venue_order_id)
        return self._result_ack(venue_order_id, result)

    def schwab_order_id(self, venue_order_id: str) -> str | None:
        record = self._orders.get(venue_order_id)
        return record.schwab_id if record else None

    # --- helpers ------------------------------------------------------------------------------

    def _remember(self, venue_order_id: str, record: _Record) -> None:
        self._orders[venue_order_id] = record
        while len(self._orders) > ORDER_ID_CAP:
            evict = next((vid for vid, r in self._orders.items() if _settled(r)), None)
            if evict is None:
                log.warning("schwab orders: %d unsettled orders over cap %d; none evicted",
                            len(self._orders), ORDER_ID_CAP)
                return
            del self._orders[evict]

    def _save(self, venue_order_id: str, record: _Record) -> None:
        self._journal.save(venue_order_id, _row(record, self._clock.now_utc()))

    def _settle(self, record: _Record, result: HubOrderResult, op: str) -> None:
        if result.outcome == "ok":
            record.status = "ACCEPTED" if result.order_id else "PENDING"
            record.schwab_id = result.order_id
        elif result.outcome in ("rejected", "not_sent"):
            record.status = "REJECTED"
        else:
            record.status = "PENDING"
        log.info("schwab order %s account=%s outcome=%s http=%s status=%s",
                 op, _mask(record.account_id), result.outcome, result.http, record.status)

    def _ack(self, venue_order_id: str, record: _Record, result: HubOrderResult | None = None) -> VenueAck:
        message = None
        if result is not None:
            message = _message(result)
        return VenueAck(venue_order_id, record.status, self._clock.now_utc(), message)  # type: ignore[arg-type]

    def _result_ack(self, venue_order_id: str, result: HubOrderResult) -> VenueAck:
        status = {"ok": "ACCEPTED", "rejected": "REJECTED", "not_sent": "REJECTED"}.get(result.outcome, "PENDING")
        return VenueAck(venue_order_id, status, self._clock.now_utc(), _message(result))  # type: ignore[arg-type]

    def _refused(self, venue_order_id: str, op: str, account: str | None, code: str) -> VenueAck:
        log.info("schwab order %s refused locally: venue_order=%s account=%s code=%s",
                 op, venue_order_id, _mask(account), code)
        return VenueAck(venue_order_id, "REJECTED", self._clock.now_utc(), code)


def _message(result: HubOrderResult) -> str | None:
    if result.outcome == "ok":
        return None if result.order_id else "no_order_id"
    if result.outcome == "rejected":
        return f"venue_rejected:{result.http}:{result.message}"
    if result.outcome == "not_sent":
        return f"hub_refused:{result.message}"
    return "outcome_unknown"

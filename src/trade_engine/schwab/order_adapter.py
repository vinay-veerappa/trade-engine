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

import trade_engine_rs as rs

from trade_engine.domain import Instrument
from trade_engine.interfaces.broker import (
    Capabilities,
    OrderChanges,
    UnsupportedCapability,
    VenueAck,
    VenueOrder,
)
from trade_engine.interfaces.clock import Clock
from trade_engine.live_ack import REFUSAL_CODE
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
_ENVS = ("sim", "paper", "live")
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


class SchwabOrderAdapter(rs.SchwabOrderAdapterBase):
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
        env: str = "paper",
        live_ack: bool = False,
        journal: OrderJournal | None = None,
    ) -> None:
        if env not in _ENVS:
            raise ValueError(f"env must be one of {_ENVS}, not {env!r}")
        self._client = client
        self._hashes = dict(account_hashes)
        self._clock = clock
        self._position = position
        self._precheck = precheck
        self._window = timedelta(seconds=resolve_window_s)
        # I10: a live adapter without the acknowledgement refuses to place or modify. Cancel stays.
        self._env = env
        self._live_ack = live_ack
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

    # The method bodies (`submit`, `_place`, `resolve_pending`, `_resolve_place`,
    # `_resolve_replace`, `_match_replacing`, `_adopt`, `_clear_replace`, `cancel`,
    # `replace`, `_replace`, `schwab_order_id`, `_live_refused`, `_remember`, `_save`,
    # `_settle`, `_ack`, `_result_ack`, `_refused`) live in `rs.SchwabOrderAdapterBase`.


def _message(result: HubOrderResult) -> str | None:
    if result.outcome == "ok":
        return None if result.order_id else "no_order_id"
    if result.outcome == "rejected":
        return f"venue_rejected:{result.http}:{result.message}"
    if result.outcome == "not_sent":
        return f"hub_refused:{result.message}"
    return "outcome_unknown"

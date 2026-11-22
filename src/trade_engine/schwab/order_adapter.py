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
* **Caps.** At most :data:`ORDER_ID_CAP` orders are kept (oldest dropped, as Tradovate does).
  One list read per account per :meth:`resolve_pending`.
"""

from __future__ import annotations

import dataclasses
import logging
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

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

# (account_id, instrument) -> the account's signed position (long > 0)
PositionFn = Callable[[str, Instrument], Decimal]


def _mask(account: str | None) -> str:
    return "***" + (account or "")[-3:]


@dataclass
class _Record:
    order: VenueOrder
    account_id: str
    account_hash: str
    instruction: str
    sent_at: datetime
    status: str  # ACCEPTED | PENDING | REJECTED
    schwab_id: str | None = None


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
    ) -> None:
        self._client = client
        self._hashes = dict(account_hashes)
        self._clock = clock
        self._position = position
        self._precheck = precheck
        self._window = timedelta(seconds=resolve_window_s)
        self._orders: OrderedDict[str, _Record] = OrderedDict()
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
        record = _Record(order, account_id, account_hash,
                         body["orderLegCollection"][0]["instruction"], self._clock.now_utc(), "PENDING")
        self._remember(order.venue_order_id, record)  # recorded before the call (D-5)
        result = self._client.place(account_hash, body)
        self._settle(record, result, "place")
        return self._ack(order.venue_order_id, record, result)

    # --- D-5 ----------------------------------------------------------------------------------

    def resolve_pending(self) -> list[VenueAck]:
        """Settle PENDING orders from one order-list read per account. Never resends."""
        pending = [(vid, r) for vid, r in self._orders.items() if r.status == "PENDING"]
        acks: list[VenueAck] = []
        by_hash: dict[str, list[tuple[str, _Record]]] = {}
        for vid, record in pending:
            by_hash.setdefault(record.account_hash, []).append((vid, record))
        for account_hash, items in by_hash.items():
            start = min(r.sent_at for _, r in items) - timedelta(seconds=5)
            end = max(r.sent_at for _, r in items) + self._window
            listed = self._client.list_orders(account_hash, start, end)
            if listed is None:
                log.warning("schwab resolve: order list read failed account=%s; %d stay PENDING",
                            _mask(items[0][1].account_id), len(items))
                continue
            for vid, record in items:
                owned = {r.schwab_id for v, r in self._orders.items() if r.schwab_id and v != vid}
                verdict, schwab_id = match_pending(
                    listed,
                    symbol=leg_instrument(record.order)["symbol"],
                    instruction=record.instruction,
                    quantity=record.order.quantity,
                    window=(record.sent_at - timedelta(seconds=5), record.sent_at + self._window),
                    exclude=owned,
                )
                if verdict == "accepted":
                    record.status, record.schwab_id = "ACCEPTED", schwab_id
                    acks.append(self._ack(vid, record))
                elif verdict == "not_at_venue":
                    record.status = "REJECTED"
                    acks.append(VenueAck(vid, "REJECTED", self._clock.now_utc(), "not_at_venue"))
                else:
                    log.warning("schwab resolve: %s matches more than one order account=%s; stays PENDING",
                                vid, _mask(record.account_id))
                    acks.append(self._ack(vid, record))
        return acks

    # --- cancel / replace ---------------------------------------------------------------------

    def cancel(self, venue_order_id: str) -> VenueAck:
        record = self._orders.get(venue_order_id)
        if record is None:
            return self._refused(venue_order_id, "cancel", None, "unknown_order")
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
        if record.schwab_id is None:
            return self._refused(venue_order_id, "replace", record.account_id, "unresolved_order")
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
        result = self._client.replace(record.account_hash, record.schwab_id, body)
        if result.outcome == "ok":
            record.order = new_order
            # Schwab cancels the old order and opens a new one; its id may come back on Location.
            if result.order_id and result.order_id != record.schwab_id:
                record.schwab_id = result.order_id
        return self._result_ack(venue_order_id, result)

    def schwab_order_id(self, venue_order_id: str) -> str | None:
        record = self._orders.get(venue_order_id)
        return record.schwab_id if record else None

    # --- helpers ------------------------------------------------------------------------------

    def _remember(self, venue_order_id: str, record: _Record) -> None:
        self._orders[venue_order_id] = record
        while len(self._orders) > ORDER_ID_CAP:
            self._orders.popitem(last=False)

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

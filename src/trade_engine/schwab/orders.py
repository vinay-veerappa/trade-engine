"""OA-6: Schwab order JSON, the D-5 pending match, and the hub's order-route client.

Three pieces, the first two pure:

* :func:`build_order_json` turns one engine :class:`VenueOrder` into the Schwab Trader API order
  body: equity or single-leg OCC option, MARKET / LIMIT / STOP, DAY or GTC, and a TRIGGER
  bracket (entry, then one OCO of a STOP exit and a LIMIT exit) when the order carries both
  ``protective_stop_price`` and ``protective_target_price``. Anything else is
  :class:`UnsupportedCapability`, before any call.
* :func:`match_pending` resolves an order whose place call had no known outcome (D-5) from one
  order-list read: exactly one match is ``accepted``, none is ``not_at_venue``, more than one
  is ``ambiguous`` (stays PENDING). The order is never resent.
* :class:`HubOrdersClient` calls the hub's ``/accounts/{hash}/orders`` routes
  (``tvDownloadOHLC/scripts/streaming/hub_orders.py``). The hub is the only Schwab client (D3);
  nothing here talks to Schwab. Loopback only, one call per request, never retried.

No clock is read here (I7); the adapter passes the window in.
"""

from __future__ import annotations

import json
import logging
import socket
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Literal
from urllib.parse import quote, urlencode

from trade_engine.domain import Equity, OptionContract, OrderType, Side, TimeInForce
from trade_engine.interfaces.broker import UnsupportedCapability, VenueOrder
from trade_engine.schwab.hub_client import DEFAULT_HUB_URL, MAX_RESPONSE_BYTES, check_loopback

log = logging.getLogger(__name__)

SUPPORTED_ORDER_TYPES = frozenset({OrderType.MARKET, OrderType.LIMIT, OrderType.STOP})
SUPPORTED_TIFS = frozenset({TimeInForce.DAY, TimeInForce.GTC})
_DURATION = {TimeInForce.DAY: "DAY", TimeInForce.GTC: "GOOD_TILL_CANCEL"}
_ORDER_TYPE = {OrderType.MARKET: "MARKET", OrderType.LIMIT: "LIMIT", OrderType.STOP: "STOP"}
#: Opening instruction -> the instruction that closes it (bracket exits).
_EXIT = {
    "BUY": "SELL",
    "SELL_SHORT": "BUY_TO_COVER",
    "BUY_TO_OPEN": "SELL_TO_CLOSE",
    "SELL_TO_OPEN": "BUY_TO_CLOSE",
}
DEFAULT_TIMEOUT_S = 10.0


def _price(value: Decimal) -> str:
    return format(value, "f")


def _quantity(value: Decimal) -> int:
    if value != value.to_integral_value():
        raise UnsupportedCapability(f"schwab orders take whole quantities, got {value}")
    return int(value)


def leg_instrument(order: VenueOrder) -> dict[str, str]:
    instrument = order.instrument
    if isinstance(instrument, Equity):
        return {"symbol": instrument.symbol, "assetType": "EQUITY"}
    if isinstance(instrument, OptionContract):
        return {"symbol": instrument.occ, "assetType": "OPTION"}
    raise UnsupportedCapability(f"schwab orders take equities and options, got {type(instrument).__name__}")


def instruction_for(order: VenueOrder, position: Decimal) -> str:
    """The Schwab leg instruction from the side and the account's current position.

    A part that would cross zero (sell 5 against long 3) is refused: the precheck splits such an
    order into its reducing and opening parts first, so each part sent here is one or the other.
    """
    quantity = order.quantity
    option = isinstance(order.instrument, OptionContract)
    if order.side is Side.BUY:
        if position < 0:
            if quantity > -position:
                raise UnsupportedCapability("a buy that crosses zero must be split first")
            return "BUY_TO_CLOSE" if option else "BUY_TO_COVER"
        return "BUY_TO_OPEN" if option else "BUY"
    if position > 0:
        if quantity > position:
            raise UnsupportedCapability("a sell that crosses zero must be split first")
        return "SELL_TO_CLOSE" if option else "SELL"
    return "SELL_TO_OPEN" if option else "SELL_SHORT"


def _single(order_type: OrderType, duration: str, instruction: str, quantity: int,
            instrument: Mapping[str, str], limit: Decimal | None, stop: Decimal | None) -> dict[str, Any]:
    body: dict[str, Any] = {
        "orderType": _ORDER_TYPE[order_type],
        "session": "NORMAL",
        "duration": duration,
        "orderStrategyType": "SINGLE",
    }
    if limit is not None:
        body["price"] = _price(limit)
    if stop is not None:
        body["stopPrice"] = _price(stop)
    body["orderLegCollection"] = [
        {"instruction": instruction, "quantity": quantity, "instrument": dict(instrument)}
    ]
    return body


def build_order_json(order: VenueOrder, position: Decimal = Decimal("0"),
                     instruction: str | None = None) -> dict[str, Any]:
    """The Schwab order body for ``order``. Pure; raises :class:`UnsupportedCapability`.

    ``instruction`` overrides the one derived from ``position`` (a replace keeps the original's).
    """
    if order.order_type not in SUPPORTED_ORDER_TYPES:
        raise UnsupportedCapability(f"schwab order type {order.order_type.value} not supported")
    if order.tif not in SUPPORTED_TIFS:
        raise UnsupportedCapability(f"schwab time in force {order.tif.value} not supported")
    if len({a.account_id for a in order.allocations}) != 1:
        raise UnsupportedCapability("netted orders not supported on schwab")
    if order.parent_order_id is not None or order.oco_group is not None:
        raise UnsupportedCapability("schwab child orders go in one bracket, not as separate orders")
    instrument = leg_instrument(order)
    quantity = _quantity(order.quantity)
    instruction = instruction or instruction_for(order, position)
    duration = _DURATION[order.tif]
    limit = order.limit_price if order.order_type is OrderType.LIMIT else None
    stop = order.stop_price if order.order_type is OrderType.STOP else None
    entry = _single(order.order_type, duration, instruction, quantity, instrument, limit, stop)
    stop_exit, target_exit = order.protective_stop_price, order.protective_target_price
    if stop_exit is None and target_exit is None:
        return entry
    if stop_exit is None or target_exit is None:
        raise UnsupportedCapability("a schwab bracket needs both a stop and a target")
    exit_instruction = _EXIT.get(instruction)
    if exit_instruction is None:
        raise UnsupportedCapability("a bracket goes on an opening order only")
    entry["orderStrategyType"] = "TRIGGER"
    entry["childOrderStrategies"] = [{
        "orderStrategyType": "OCO",
        "childOrderStrategies": [
            _single(OrderType.STOP, duration, exit_instruction, quantity, instrument, None, stop_exit),
            _single(OrderType.LIMIT, duration, exit_instruction, quantity, instrument, target_exit, None),
        ],
    }]
    return entry


# --- D-5: resolve an unknown outcome from one order-list read --------------------------------

MatchResult = tuple[Literal["accepted", "not_at_venue", "ambiguous"], "str | None"]


def schwab_time(value: datetime) -> str:
    """Schwab's ``fromEnteredTime`` / ``toEnteredTime`` form: UTC, milliseconds, ``Z``."""
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{value.microsecond // 1000:03d}Z"


def _entered(text: Any) -> datetime | None:
    if not isinstance(text, str):
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%dT%H:%M:%S%z")
    except ValueError:
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None


def _price_is(item: Mapping[str, Any], key: str, want: Decimal | None) -> bool:
    if want is None:
        return True
    try:
        return Decimal(str(item[key])) == want
    except (KeyError, TypeError, ArithmeticError):
        return False


def match_pending(
    orders: Iterable[Mapping[str, Any]],
    *,
    symbol: str,
    instruction: str,
    quantity: Decimal,
    window: tuple[datetime, datetime],
    exclude: Iterable[str] = (),
    order_type: str | None = None,
    price: Decimal | None = None,
    stop_price: Decimal | None = None,
) -> MatchResult:
    """Match one pending order by symbol + instruction + quantity + enteredTime in ``window``.

    The account is already fixed: the list read is one account's. ``exclude`` holds Schwab order
    ids the adapter already owns, so a known order is never matched to a second pending one.
    ``order_type``, ``price`` and ``stop_price`` are optional: each one given must equal the
    listed order's field (a missing or unparseable field does not match). Left as None, it is
    not compared, so older callers match as before.
    """
    start, end = window
    skip = set(exclude)
    found: list[str] = []
    for item in orders:
        try:
            leg = item["orderLegCollection"][0]
            order_id = str(item["orderId"])
            same = (leg["instrument"]["symbol"] == symbol and leg["instruction"] == instruction
                    and Decimal(str(leg["quantity"])) == quantity)
        except (KeyError, IndexError, TypeError, ArithmeticError):
            continue
        entered = _entered(item.get("enteredTime"))
        typed = ((order_type is None or item.get("orderType") == order_type)
                 and _price_is(item, "price", price) and _price_is(item, "stopPrice", stop_price))
        if same and typed and entered is not None and start <= entered <= end and order_id not in skip:
            found.append(order_id)
    if len(found) == 1:
        return "accepted", found[0]
    if not found:
        return "not_at_venue", None
    return "ambiguous", None


# --- the hub's order routes -------------------------------------------------------------------

# (method, url, body or None, headers, timeout) -> (status, text); status 0 is "no answer".
Transport = Callable[[str, str, "bytes | None", Mapping[str, str], float], tuple[int, str]]

Outcome = Literal["ok", "rejected", "not_sent", "unknown"]


@dataclass(frozen=True)
class HubOrderResult:
    """One order call's outcome.

    * ``ok``: Schwab answered 2xx. ``order_id`` is Schwab's id (place: from Location).
    * ``rejected``: Schwab answered 4xx; ``message`` is its body (no secrets: Schwab's error text).
    * ``not_sent``: the hub refused before calling Schwab (flag off, not loopback, bad input) or
      the hub was not listening. The order is not at the venue.
    * ``unknown``: a timeout, a dropped connection, a hub 502/504 or a Schwab 5xx. The order may
      be at the venue; the caller resolves it from the order list and never resends.
    """

    outcome: Outcome
    http: int
    order_id: str | None = None
    body: str = ""
    message: str = ""


def urllib_transport(method: str, url: str, body: bytes | None,
                     headers: Mapping[str, str], timeout: float) -> tuple[int, str]:
    """One HTTP request, no retry. HTTP errors come back as (status, text)."""
    request = urllib.request.Request(url, data=body, method=method, headers=dict(headers))
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - loopback only
            return response.status, response.read(MAX_RESPONSE_BYTES).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(MAX_RESPONSE_BYTES).decode("utf-8", "replace")


class HubOrdersClient:
    def __init__(self, base_url: str = DEFAULT_HUB_URL, transport: Transport = urllib_transport,
                 timeout_s: float = DEFAULT_TIMEOUT_S) -> None:
        check_loopback(base_url)
        self._base = base_url.strip().rstrip("/")
        self._transport = transport
        self._timeout = timeout_s

    def place(self, account_hash: str, order: Mapping[str, Any]) -> HubOrderResult:
        return self._call("POST", self._path(account_hash), order)

    def replace(self, account_hash: str, order_id: str, order: Mapping[str, Any]) -> HubOrderResult:
        return self._call("PUT", self._path(account_hash, order_id), order)

    def cancel(self, account_hash: str, order_id: str) -> HubOrderResult:
        return self._call("DELETE", self._path(account_hash, order_id), None)

    def list_orders(self, account_hash: str, start: datetime, end: datetime) -> list[dict] | None:
        """One account's orders entered in [start, end]; ``None`` when the read failed."""
        query = urlencode({"fromEnteredTime": schwab_time(start), "toEnteredTime": schwab_time(end)})
        result = self._call("GET", self._path(account_hash) + "?" + query, None)
        if result.outcome != "ok":
            return None
        try:
            orders = json.loads(result.body or "[]")
        except ValueError:
            return None
        return orders if isinstance(orders, list) else None

    def _path(self, account_hash: str, order_id: str | None = None) -> str:
        path = f"{self._base}/accounts/{quote(account_hash, safe='')}/orders"
        return path if order_id is None else f"{path}/{quote(order_id, safe='')}"

    def _call(self, method: str, url: str, order: Mapping[str, Any] | None) -> HubOrderResult:
        body = None if order is None else json.dumps(order, separators=(",", ":")).encode()
        headers = {"Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        try:
            status, text = self._transport(method, url, body, headers, self._timeout)
        except ConnectionRefusedError:
            return HubOrderResult("not_sent", 0, message="hub_unreachable")
        except (TimeoutError, socket.timeout):
            return HubOrderResult("unknown", 0, message="outcome_unknown")
        except (OSError, urllib.error.URLError) as exc:
            reason = getattr(exc, "reason", None)
            if isinstance(reason, ConnectionRefusedError):
                return HubOrderResult("not_sent", 0, message="hub_unreachable")
            return HubOrderResult("unknown", 0, message="outcome_unknown")
        return _classify(status, text)


def _classify(status: int, text: str) -> HubOrderResult:
    try:
        payload = json.loads(text) if text else {}
    except ValueError:
        payload = None
    if not isinstance(payload, dict):
        # Not the hub's shape: from a 2xx this is unknown (the call may have gone through).
        return HubOrderResult("unknown" if status == 0 or status >= 200 and status < 300 or status >= 500
                              else "not_sent", status, message="bad_shape")
    if "error" in payload:
        error = str(payload["error"])
        if status in (502, 504) or status == 0:
            return HubOrderResult("unknown", status, message=error)
        return HubOrderResult("not_sent", status, message=error)
    schwab = payload.get("status")
    order_id = payload.get("order_id")
    body = payload.get("body") if isinstance(payload.get("body"), str) else ""
    order_id = str(order_id) if order_id not in (None, "") else None
    if not isinstance(schwab, int):
        return HubOrderResult("unknown", status, message="bad_shape")
    if 200 <= schwab < 300:
        return HubOrderResult("ok", schwab, order_id, body)
    if 400 <= schwab < 500:
        return HubOrderResult("rejected", schwab, order_id, body, message=_schwab_message(body))
    return HubOrderResult("unknown", schwab, order_id, body, message="upstream_status")


def _schwab_message(body: str) -> str:
    try:
        parsed = json.loads(body)
    except ValueError:
        return body[:200]
    if isinstance(parsed, dict) and isinstance(parsed.get("message"), str):
        return parsed["message"][:200]
    return body[:200]

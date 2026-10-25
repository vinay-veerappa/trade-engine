"""RiskGuard pre-check at the OMS submit and replace chokepoint (WAVE7-VELA).

The engine holds no rules. Before an order reaches the broker, the chokepoint splits it with
the engine's own position mirror (a classification, not a rule): the reducing part is sent at
once and never waits; the opening part is sent only after RiskGuard's ``POST /v1/check``
answers ``allowed: true`` within the time budget. Cancels are not gated here.

Modes (``riskguard.precheck``): ``off`` installs nothing; ``warn`` sends every order as it is
and logs what enforcement would refuse; ``enforce`` refuses the opening part. Every failure
(timeout, network, non-200, a body that is not a valid response, an unmapped account) counts
as a refusal of the opening part only.
"""

from __future__ import annotations

import http.client
import json
import logging
import queue
import re
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from decimal import Decimal
from urllib.parse import quote, urlsplit

from trade_engine.domain.instruments import Instrument, Side
from trade_engine.domain.orders import Order
from trade_engine.interfaces.broker import OrderChanges, VenueAck, VenueOrder
from trade_engine.interfaces.clock import Clock
from trade_engine.ledger import Ledger

MODES = ("off", "warn", "enforce")
CHECK_TIMEOUT_S = 0.25
ORIGIN = "strategy"
OPEN_SUFFIX = ":open"
ROOT_PATTERN = re.compile(r"^[A-Z][A-Z0-9]{0,3}$")
ACCOUNT_ENVS = ("live", "demo", "sim")
REASON_CODES = frozenset(
    {
        "OK",
        "OK_REDUCING",
        "VERDICT_STALE",
        "ACCOUNT_BLOCKED",
        "BLOCK_ADD",
        "INSTRUMENT_BLOCKED",
        "MAX_QTY",
        "COPY_OPENING_REFUSED",
    }
)
RESPONSE_KEYS = frozenset({"allowed", "isReducing", "reasonCode", "detail", "ruleId", "verdictRevision"})
REQUIRED_RESPONSE_KEYS = frozenset({"allowed", "isReducing", "reasonCode"})
ACCOUNT_KEY_FIELDS = frozenset({"venue", "env", "brokerAccountId"})

_LOG = logging.getLogger("trade_engine.oms.precheck")


class PrecheckConfigError(ValueError):
    """``riskguard.precheck`` is malformed; the engine refuses to start with it."""


@dataclass(frozen=True)
class AccountBinding:
    """How one engine account is named to RiskGuard: its core account and its ``accountKey``."""

    core_account: str
    account_key: Mapping[str, str]


@dataclass(frozen=True)
class PrecheckConfig:
    """The validated ``riskguard.precheck`` section. ``token`` is never shown in a repr or a log."""

    mode: str = "warn"
    base_url: str = ""
    token: str = field(default="", repr=False)
    accounts: Mapping[str, AccountBinding] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, section: Mapping) -> PrecheckConfig:
        mode = section.get("mode", "warn")
        if mode not in MODES:
            raise PrecheckConfigError(f"riskguard.precheck.mode must be off|warn|enforce, got {mode!r}")
        accounts = {
            engine_id: _binding(engine_id, item) for engine_id, item in section.get("accounts", {}).items()
        }
        if mode == "off":
            return cls(mode=mode, accounts=accounts)
        token = section.get("token")
        if not isinstance(token, str) or not token:
            raise PrecheckConfigError("riskguard.precheck.token must be a non-empty string")
        return cls(mode=mode, base_url=_loopback_base(section.get("baseUrl")), token=token, accounts=accounts)


def _loopback_base(value) -> str:
    if not isinstance(value, str):
        raise PrecheckConfigError("riskguard.precheck.baseUrl must be a string")
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError as err:
        raise PrecheckConfigError(f"riskguard.precheck.baseUrl is not a URL: {err}") from err
    if parts.scheme != "http" or parts.hostname not in ("127.0.0.1", "localhost"):
        raise PrecheckConfigError("riskguard.precheck.baseUrl must be a loopback http URL")
    if port is None or parts.path not in ("", "/"):
        raise PrecheckConfigError("riskguard.precheck.baseUrl needs an explicit port and no path")
    return f"http://{parts.hostname}:{port}"


def _binding(engine_id: str, item: Mapping) -> AccountBinding:
    core_account = item.get("coreAccount")
    if not isinstance(core_account, str) or not core_account:
        raise PrecheckConfigError(f"riskguard.precheck.accounts[{engine_id!r}].coreAccount must be a name")
    key = item.get("accountKey")
    if not isinstance(key, Mapping) or set(key) != ACCOUNT_KEY_FIELDS:
        raise PrecheckConfigError(f"riskguard.precheck.accounts[{engine_id!r}].accountKey needs {sorted(ACCOUNT_KEY_FIELDS)}")
    if not isinstance(key["venue"], str) or not key["venue"] or key["env"] not in ACCOUNT_ENVS:
        raise PrecheckConfigError(f"riskguard.precheck.accounts[{engine_id!r}].accountKey has a bad venue or env")
    if not isinstance(key["brokerAccountId"], str) or not key["brokerAccountId"]:
        raise PrecheckConfigError(f"riskguard.precheck.accounts[{engine_id!r}].accountKey has no brokerAccountId")
    return AccountBinding(core_account, dict(key))


@dataclass(frozen=True)
class CheckResult:
    """One answer. ``kind`` is ``allowed``, the service's reason code, or a local failure kind."""

    allowed: bool
    kind: str


def split_quantity(quantity: Decimal, side: Side, position: Decimal) -> tuple[Decimal, Decimal]:
    """Return ``(reducing, opening)``. A BUY closes a short first; a SELL closes a long first."""
    opposite = max(-position, Decimal("0")) if side is Side.BUY else max(position, Decimal("0"))
    reducing = min(quantity, opposite)
    return reducing, quantity - reducing


def _valid_response(body) -> bool:
    """The hand-written ``singleResponse`` check (check.schema.json), exact keys and types."""
    if not isinstance(body, dict) or not REQUIRED_RESPONSE_KEYS <= body.keys() <= RESPONSE_KEYS:
        return False
    if not isinstance(body["allowed"], bool) or not isinstance(body["isReducing"], bool):
        return False
    if body["reasonCode"] not in REASON_CODES:
        return False
    if "detail" in body and not isinstance(body["detail"], str):
        return False
    if "ruleId" in body and not isinstance(body["ruleId"], str):
        return False
    revision = body.get("verdictRevision", 0)
    return isinstance(revision, int) and not isinstance(revision, bool) and revision >= 0


class RiskGuardClient:
    """``POST /v1/check?account=`` over loopback with a 250 ms budget (a worker thread bounds it)."""

    def __init__(self, base_url: str, token: str) -> None:
        parts = urlsplit(base_url)
        self._host = parts.hostname
        self._port = parts.port
        self._token = token

    def check(self, core_account: str, request: dict) -> CheckResult:
        path = f"/v1/check?account={quote(core_account, safe='')}"
        headers = {"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"}
        error, status, body = self._post(path, json.dumps(request).encode(), headers)
        if error is not None:
            return CheckResult(False, error)
        if status != 200:
            return CheckResult(False, f"http_{status}")
        try:
            parsed = json.loads(body)
        except ValueError:
            return CheckResult(False, "invalid")
        if not _valid_response(parsed):
            return CheckResult(False, "invalid")
        if parsed["allowed"] is True:
            return CheckResult(True, "allowed")
        return CheckResult(False, parsed["reasonCode"])

    def _post(self, path: str, payload: bytes, headers: dict) -> tuple[str | None, int, bytes]:
        outcome: queue.Queue = queue.Queue(maxsize=1)

        def run() -> None:
            conn = http.client.HTTPConnection(self._host, self._port, timeout=CHECK_TIMEOUT_S)
            try:
                conn.request("POST", path, body=payload, headers=headers)
                response = conn.getresponse()
                outcome.put((None, response.status, response.read()))
            except TimeoutError:
                outcome.put(("timeout", 0, b""))
            except (OSError, http.client.HTTPException):
                outcome.put(("network", 0, b""))
            finally:
                conn.close()

        threading.Thread(target=run, name="riskguard-precheck", daemon=True).start()
        try:
            return outcome.get(timeout=CHECK_TIMEOUT_S)
        except queue.Empty:
            return ("timeout", 0, b"")


class Precheck:
    """The submit and replace wrapper. It reads the ledger's position mirror and sends through ``send``."""

    def __init__(self, config: PrecheckConfig, clock: Clock, ledger: Ledger) -> None:
        self._config = config
        self._clock = clock
        self._ledger = ledger
        self._client = RiskGuardClient(config.base_url, config.token)

    @property
    def enforcing(self) -> bool:
        return self._config.mode == "enforce"

    def submit(self, order: VenueOrder, send: Callable[[VenueOrder], VenueAck]) -> VenueAck:
        account = _single_account(order)
        position = self._position(account, order.instrument) if account is not None else Decimal("0")
        reducing, opening = split_quantity(order.quantity, order.side, position)
        if not self.enforcing:
            ack = send(order)
            if opening > 0:
                result = self._check(account, order.instrument, order.side, opening)
                if not result.allowed:
                    _LOG.warning("riskguard precheck would refuse (warn): venue_order=%s kind=%s qty=%s",
                                 order.venue_order_id, result.kind, opening)
            return ack
        if opening == 0:
            return send(order)
        if reducing == 0:
            result = self._check(account, order.instrument, order.side, order.quantity)
            if not result.allowed:
                return self._refuse(order.venue_order_id, result)
            return send(order)
        return self._submit_split(order, account, reducing, opening, send)

    def _submit_split(self, order, account, reducing, opening, send) -> VenueAck:
        # The reducing part goes at once, under the strategy order's own venue id, and never waits.
        reduce_ack = send(replace(order, quantity=reducing, allocations=_part(order, reducing)))
        if reduce_ack.status == "REJECTED":
            return reduce_ack
        result = self._check(account, order.instrument, order.side, opening)
        if not result.allowed:
            _LOG.warning("riskguard precheck refused opening part: venue_order=%s kind=%s qty=%s",
                         order.venue_order_id, result.kind, opening)
            return replace(reduce_ack, message=f"opening {opening} refused: {result.kind}")
        open_ack = send(replace(order, venue_order_id=order.venue_order_id + OPEN_SUFFIX,
                                quantity=opening, allocations=_part(order, opening)))
        if open_ack.status == "REJECTED":
            return replace(reduce_ack, message=f"opening {opening} rejected by the venue")
        return reduce_ack

    def replace(self, venue_order_id: str, changes: OrderChanges,
                send: Callable[[str, OrderChanges], VenueAck]) -> VenueAck:
        order = self._working_order(venue_order_id)
        if order is None:
            if self.enforcing:
                return self._refuse(venue_order_id, CheckResult(False, "unknown_order"))
            _LOG.warning("riskguard precheck: replace of an unknown order not checked: venue_order=%s",
                         venue_order_id)
            return send(venue_order_id, changes)
        new_quantity = order.quantity if changes.new_quantity is None else changes.new_quantity
        price_changed = (
            (changes.new_limit_price is not None and changes.new_limit_price != order.limit_price)
            or (changes.new_stop_price is not None and changes.new_stop_price != order.stop_price)
        )
        grows = new_quantity > order.quantity
        _, opening = split_quantity(new_quantity, order.side, self._position(order.account_id, order.instrument))
        if not ((grows or price_changed) and opening > 0):
            return send(venue_order_id, changes)
        result = self._check(order.account_id, order.instrument, order.side, opening)
        if not result.allowed and self.enforcing:
            return self._refuse(venue_order_id, result)
        if not result.allowed:
            _LOG.warning("riskguard precheck would refuse replace (warn): venue_order=%s kind=%s qty=%s",
                         venue_order_id, result.kind, opening)
        return send(venue_order_id, changes)

    def _refuse(self, venue_order_id: str, result: CheckResult) -> VenueAck:
        _LOG.warning("riskguard precheck refused: venue_order=%s kind=%s", venue_order_id, result.kind)
        return VenueAck(venue_order_id, "REJECTED", self._clock.now_utc(),
                        f"riskguard precheck refused: {result.kind}")

    def _check(self, account_id: str | None, instrument: Instrument, side: Side, quantity: Decimal) -> CheckResult:
        binding = self._config.accounts.get(account_id) if account_id is not None else None
        if binding is None:
            return CheckResult(False, "unmapped_account")
        root = _root(instrument)
        if root is None:
            return CheckResult(False, "invalid_instrument")
        request = {
            "accountKey": dict(binding.account_key),
            "instrument": root,
            "side": "long" if side is Side.BUY else "short",
            "qty": _json_number(quantity),
            "isReducing": False,
            "origin": ORIGIN,
        }
        return self._client.check(binding.core_account, request)

    def _position(self, account_id: str, instrument: Instrument) -> Decimal:
        position = self._ledger.state(account_id).positions.get(instrument)
        return Decimal("0") if position is None else position.quantity

    def _working_order(self, venue_order_id: str) -> Order | None:
        for account in self._ledger.accounts():
            state = self._ledger.state(account)
            for order_id, order in state.orders.items():
                if state.venue_order_ids.get(order_id, order_id) == venue_order_id:
                    return order
        return None


def _single_account(order: VenueOrder) -> str | None:
    # A netted or multi-allocation order has no single account to check against: it is not split.
    if len(order.allocations) != 1:
        return None
    return order.allocations[0].account_id


def _part(order: VenueOrder, quantity: Decimal) -> tuple:
    return tuple(replace(item, quantity=quantity) for item in order.allocations)


def _root(instrument: Instrument) -> str | None:
    try:
        symbol = instrument.symbol
    except NotImplementedError:
        return None
    return symbol if isinstance(symbol, str) and ROOT_PATTERN.match(symbol) else None


def _json_number(quantity: Decimal) -> int | float:
    return int(quantity) if quantity == quantity.to_integral_value() else float(quantity)

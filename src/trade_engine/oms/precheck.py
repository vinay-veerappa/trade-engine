"""RiskGuard pre-check at the OMS submit and replace chokepoint (WAVE7-VELA).

The engine holds no rules. Before an order reaches the broker, the chokepoint splits it with
the engine's own position mirror (a classification, not a rule): the reducing part is sent at
once and never waits; the opening part is sent only after RiskGuard's ``POST /v1/check``
answers ``allowed: true`` within the time budget. Cancels are not gated here.

Modes (``riskguard.precheck``): ``off`` installs nothing; ``warn`` sends every order as it is
and logs what enforcement would refuse; ``enforce`` refuses the opening part. Every failure
(timeout, network, non-200, a body that is not a valid response, an unmapped account) counts
as a refusal of the opening part only.

The decision rules (the split, the instrument root, the response check, the loopback URL and
the replace's opening part) are ``te_core::oms::precheck``, reached through ``trade_engine_rs``.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from decimal import Decimal

from trade_engine.domain.instruments import Instrument, Side
from trade_engine.domain.orders import Order
from trade_engine.interfaces.broker import (
    OrderChanges,
    RiskguardVerdict,
    VenueAck,
    VenueOrder,
    VenueOrderAllocation,
)
from trade_engine.interfaces.clock import Clock
from trade_engine.ledger import Ledger, codec
from trade_engine.sim import _rs

MODES = ("off", "warn", "enforce")
CHECK_TIMEOUT_S = 0.25
ORIGIN = "strategy"
OPEN_SUFFIX = ":open"
ACCOUNT_ENVS = ("live", "demo", "sim")
ACCOUNT_KEY_FIELDS = frozenset({"venue", "env", "brokerAccountId"})

_native = _rs.rs
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
        return _native.precheck_loopback_base(value)
    except ValueError as err:
        raise PrecheckConfigError(err.args[1]) from err


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
    verdict_revision: int | None = None
    checked_at: str | None = None

    def verdict(self) -> RiskguardVerdict:
        """The verdict an adapter that needs one puts on the wire (OA-3)."""
        return RiskguardVerdict(self.allowed, self.verdict_revision, self.checked_at)


def _text(value: Decimal) -> str:
    """A Decimal as the plain text the Rust side parses (never an exponent)."""
    return format(value, "f")


def split_quantity(quantity: Decimal, side: Side, position: Decimal) -> tuple[Decimal, Decimal]:
    """Return ``(reducing, opening)``. A BUY closes a short first; a SELL closes a long first."""
    reducing, opening = _rs.call(_native.precheck_split_quantity, _text(quantity), side.value, _text(position))
    return Decimal(reducing), Decimal(opening)


def valid_response(body) -> bool:
    """The hand-written ``singleResponse`` check (check.schema.json), run by te_core on the JSON text."""
    try:
        text = json.dumps(body)
    except (TypeError, ValueError):
        return False
    return _native.precheck_valid_response(text)


class RiskGuardClient:
    """``POST /v1/check?account=`` over loopback with a 250 ms budget (Rust bounds it)."""

    def __init__(self, base_url: str, token: str) -> None:
        self._base_url = base_url
        self._token = token

    def check(self, core_account: str, request: dict) -> CheckResult:
        allowed, reason, verdict_revision, checked_at = _rs.call(
            _native.riskguard_check,
            self._base_url,
            self._token,
            core_account,
            json.dumps(request),
            CHECK_TIMEOUT_S,
        )
        return CheckResult(allowed, reason, verdict_revision, checked_at)


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
        reducing_allocs, opening_allocs, openings = self._split_allocations(order)
        reducing = _total(reducing_allocs)
        opening = _total(opening_allocs)
        if not self.enforcing:
            ack = send(order)
            for account, quantity in openings.items():
                result = self._check(account, order.instrument, order.side, quantity)
                if not result.allowed:
                    _LOG.warning("riskguard precheck would refuse (warn): venue_order=%s account=%s kind=%s qty=%s",
                                 order.venue_order_id, account, result.kind, quantity)
            return ack
        if opening == 0:
            return send(order)
        if reducing == 0:
            result, verdict = self._first_refusal(order, openings)
            if result is not None:
                return self._refuse(order.venue_order_id, result)
            return send(replace(order, riskguard_verdict=verdict))
        return self._submit_split(order, reducing_allocs, opening_allocs, openings, send)

    def _submit_split(self, order, reducing_allocs, opening_allocs, openings, send) -> VenueAck:
        # The reducing part goes at once, under the strategy order's own venue id, and never waits.
        reducing = _total(reducing_allocs)
        opening = _total(opening_allocs)
        reduce_ack = send(replace(order, quantity=reducing, allocations=reducing_allocs))
        if reduce_ack.status == "REJECTED":
            return reduce_ack
        result, verdict = self._first_refusal(order, openings)
        if result is not None:
            _LOG.warning("riskguard precheck refused opening part: venue_order=%s kind=%s qty=%s",
                         order.venue_order_id, result.kind, opening)
            return replace(reduce_ack, message=f"opening {opening} refused: {result.kind}")
        open_ack = send(replace(order, venue_order_id=order.venue_order_id + OPEN_SUFFIX,
                                quantity=opening, allocations=opening_allocs,
                                riskguard_verdict=verdict))
        if open_ack.status == "REJECTED":
            return replace(reduce_ack, message=f"opening {opening} rejected by the venue")
        return reduce_ack

    def _first_refusal(
        self, order: VenueOrder, openings: Mapping[str, Decimal]
    ) -> tuple[CheckResult | None, RiskguardVerdict | None]:
        # Each account with an opening part is checked once, in order; the first refusal stops the rest.
        # The second item is the last allowed verdict (the only one for a single-account order).
        verdict: RiskguardVerdict | None = None
        for account, quantity in openings.items():
            result = self._check(account, order.instrument, order.side, quantity)
            if not result.allowed:
                return result, None
            verdict = result.verdict()
        return None, verdict

    def replace(self, venue_order_id: str, changes: OrderChanges,
                send: Callable[[str, OrderChanges], VenueAck]) -> VenueAck:
        orders = self._working_orders(venue_order_id)
        if len(orders) > 1:
            # A netted venue order: a venue-level change does not say how it splits across the accounts.
            _LOG.warning("riskguard precheck: replace of a netted order not checked: venue_order=%s accounts=%s",
                         venue_order_id, ",".join(sorted({item.account_id for item in orders})))
            return send(venue_order_id, changes)
        order = orders[0] if orders else None
        if order is None:
            if self.enforcing:
                return self._refuse(venue_order_id, CheckResult(False, "unknown_order"))
            _LOG.warning("riskguard precheck: replace of an unknown order not checked: venue_order=%s",
                         venue_order_id)
            return send(venue_order_id, changes)
        price_changed = (
            (changes.new_limit_price is not None and changes.new_limit_price != order.limit_price)
            or (changes.new_stop_price is not None and changes.new_stop_price != order.stop_price)
        )
        new_quantity = None if changes.new_quantity is None else _text(changes.new_quantity)
        opening_text = _rs.call(
            _native.precheck_replace_opening,
            _text(order.quantity),
            new_quantity,
            order.side.value,
            _text(self._position(order.account_id, order.instrument)),
            price_changed,
        )
        if opening_text is None:
            return send(venue_order_id, changes)
        opening = Decimal(opening_text)
        result = self._check(order.account_id, order.instrument, order.side, opening)
        if not result.allowed and self.enforcing:
            return self._refuse(venue_order_id, result)
        if not result.allowed:
            _LOG.warning("riskguard precheck would refuse replace (warn): venue_order=%s kind=%s qty=%s",
                         venue_order_id, result.kind, opening)
        elif self.enforcing:
            changes = replace(changes, riskguard_verdict=result.verdict())
        return send(venue_order_id, changes)

    def _refuse(self, venue_order_id: str, result: CheckResult) -> VenueAck:
        _LOG.warning("riskguard precheck refused: venue_order=%s kind=%s", venue_order_id, result.kind)
        return VenueAck(venue_order_id, "REJECTED", self._clock.now_utc(),
                        f"riskguard precheck refused: {result.kind}")

    def _check(self, account_id: str | None, instrument: Instrument, side: Side, quantity: Decimal) -> CheckResult:
        binding = self._config.accounts.get(account_id) if account_id is not None else None
        if binding is None:
            return CheckResult(False, "unmapped_account")
        root = _native.precheck_root(codec.text(codec.canon(instrument)))
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

    def _working_orders(self, venue_order_id: str) -> list[Order]:
        found: list[Order] = []
        for account in self._ledger.accounts():
            state = self._ledger.state(account)
            for order_id, order in state.orders.items():
                if state.venue_order_ids.get(order_id, order_id) == venue_order_id:
                    found.append(order)
        return found

    def _split_allocations(self, order: VenueOrder) -> tuple[
            tuple[VenueOrderAllocation, ...], tuple[VenueOrderAllocation, ...], dict[str, Decimal]]:
        """Split a venue order's allocations into the reducing and the opening parts, per account.

        The rule is te_core's ``split_allocations``; this reads each account's position once and
        maps the indices it returns back onto the order's allocations. Returns
        ``(reducing_allocations, opening_allocations, opening_quantity_by_account)``; the last
        holds only the accounts with an opening part, in first-appearance order.
        """
        positions = {
            account: _text(self._position(account, order.instrument))
            for account in dict.fromkeys(item.account_id for item in order.allocations)
        }
        allocs = [(item.account_id, _text(item.quantity)) for item in order.allocations]
        reducing, opening, openings = _rs.call(
            _native.precheck_split_allocations, order.side.value, allocs, positions)
        reducing_allocs = tuple(replace(order.allocations[i], quantity=Decimal(q)) for i, q in reducing)
        opening_allocs = tuple(replace(order.allocations[i], quantity=Decimal(q)) for i, q in opening)
        return reducing_allocs, opening_allocs, {account: Decimal(q) for account, q in openings}


def _total(allocations: tuple[VenueOrderAllocation, ...]) -> Decimal:
    return sum((item.quantity for item in allocations), Decimal("0"))


def _json_number(quantity: Decimal) -> int | float:
    return int(quantity) if quantity == quantity.to_integral_value() else float(quantity)

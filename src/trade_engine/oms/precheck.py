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
The submit/replace orchestration (``Precheck``) now lives in ``trade_engine_rs.PrecheckBase``.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal

from trade_engine.domain.instruments import Side
from trade_engine.interfaces.broker import RiskguardVerdict
from trade_engine.interfaces.clock import Clock
from trade_engine.ledger import Ledger
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


class Precheck(_native.PrecheckBase):
    """The submit and replace wrapper. It reads the ledger's position mirror and sends through ``send``."""

    def __init__(self, config: PrecheckConfig, clock: Clock, ledger: Ledger) -> None:
        self._config = config
        self._clock = clock
        self._ledger = ledger
        self._client = RiskGuardClient(config.base_url, config.token)

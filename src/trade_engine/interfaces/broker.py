"""Broker adapter protocol and venue data transfer structures (Architecture §4.5)."""

from __future__ import annotations

import json as _json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from types import MappingProxyType
from typing import Any, Literal, Protocol, runtime_checkable

from trade_engine.accounts import _rs as _accounts_rs
from trade_engine.accounts.errors import AccountRuleError
from trade_engine.domain.instruments import Instrument, Side
from trade_engine.domain.orders import OrderState, OrderType, TimeInForce, validate_order_prices


class UnsupportedCapability(Exception):
    """The venue cannot express this order (an order type, TIF, instrument or leg count
    its adapter does not declare). Refuse with the reason; never approximate (I5, §4.5)."""


@dataclass(frozen=True)
class Capabilities:
    """Venue capabilities declared by the broker adapter."""

    supported_order_types: frozenset[OrderType]
    supported_tifs: frozenset[TimeInForce]
    supports_multi_leg: bool
    supports_native_stops: bool
    supports_streaming: bool


@dataclass(frozen=True)
class VenueIdentity:
    """Venue identity proven at connect time (I10)."""

    account_id: str
    env: Literal["sim", "paper", "live"]
    connected_at: datetime
    broker_name: str

    def __post_init__(self) -> None:
        if self.env not in ("sim", "paper", "live"):
            raise ValueError(f"Invalid venue env '{self.env}' (I10)")
        if self.connected_at.tzinfo is None or self.connected_at.tzinfo.utcoffset(self.connected_at) is None:
            raise ValueError("connected_at must be timezone-aware UTC datetime (I7)")


@dataclass(frozen=True)
class VenueOrderAllocation:
    """Allocation of a venue order portion back to a specific strategy order (Architecture §4.4)."""

    strategy_order_id: str
    account_id: str
    quantity: Decimal

    def __post_init__(self) -> None:
        if not self.strategy_order_id:
            raise ValueError("strategy_order_id must be non-empty")
        if not self.account_id:
            raise ValueError("account_id must be non-empty")
        if self.quantity <= Decimal("0"):
            raise ValueError(f"quantity must be positive, got {self.quantity}")


@dataclass(frozen=True)
class VenueOrder:
    """Order submitted to a specific venue (supports 1:1 or netted strategy orders, Architecture §4.4)."""

    venue_order_id: str
    instrument: Instrument
    order_type: OrderType
    side: Side
    quantity: Decimal
    submitted_at: datetime
    tif: TimeInForce = TimeInForce.DAY
    limit_price: Decimal | None = None
    stop_price: Decimal | None = None
    trail_amount: Decimal | None = None
    allocations: tuple[VenueOrderAllocation, ...] = ()
    parent_order_id: str | None = None
    oco_group: str | None = None

    def __post_init__(self) -> None:
        if not self.venue_order_id:
            raise ValueError("venue_order_id must be non-empty")
        if self.quantity <= Decimal("0"):
            raise ValueError(f"quantity must be positive, got {self.quantity}")
        if self.submitted_at.tzinfo is None or self.submitted_at.tzinfo.utcoffset(self.submitted_at) is None:
            raise ValueError("submitted_at must be timezone-aware UTC datetime (I7)")
        if not isinstance(self.side, Side):
            raise ValueError(f"Invalid side '{self.side}'")
        validate_order_prices(self.order_type, self.limit_price, self.stop_price, self.trail_amount)
        # Every venue order must allocate back to strategy orders, or its fills are orphaned (§4.4)
        if not self.allocations:
            raise ValueError("VenueOrder must carry at least one strategy-order allocation")
        allocated = sum((a.quantity for a in self.allocations), Decimal("0"))
        if allocated != self.quantity:
            raise ValueError(
                f"VenueOrder allocations total {allocated} but order quantity is {self.quantity}"
            )


@dataclass(frozen=True)
class VenueAck:
    """Venue acknowledgment of an order action."""

    venue_order_id: str
    status: Literal["ACCEPTED", "REJECTED", "PENDING"]
    timestamp: datetime
    message: str | None = None

    def __post_init__(self) -> None:
        if self.timestamp.tzinfo is None or self.timestamp.tzinfo.utcoffset(self.timestamp) is None:
            raise ValueError("timestamp must be timezone-aware UTC datetime (I7)")


@dataclass(frozen=True)
class OrderChanges:
    """Changes requested in an order replace/modify request."""

    new_quantity: Decimal | None = None
    new_limit_price: Decimal | None = None
    new_stop_price: Decimal | None = None


@dataclass(frozen=True)
class VenueOrderState:
    """Venue-reported order status."""

    venue_order_id: str
    state: OrderState
    filled_quantity: Decimal
    remaining_quantity: Decimal
    updated_at: datetime

    def __post_init__(self) -> None:
        if self.updated_at.tzinfo is None or self.updated_at.tzinfo.utcoffset(self.updated_at) is None:
            raise ValueError("updated_at must be timezone-aware UTC datetime (I7)")


@dataclass(frozen=True)
class VenueFill:
    """Venue-reported fill event."""

    venue_fill_id: str
    venue_order_id: str
    instrument: Instrument
    quantity: Decimal
    price: Decimal
    filled_at: datetime
    side: Side  # Required: a defaulted side would guess the direction (I5)
    fee: Decimal = Decimal("0")
    # A combo order is reported leg by leg: the index of the leg this fill executed.
    leg_id: str | None = None

    def __post_init__(self) -> None:
        if self.quantity <= Decimal("0"):
            raise ValueError(f"VenueFill quantity must be strictly positive, got {self.quantity}")
        if self.price <= Decimal("0"):
            raise ValueError(f"VenueFill price must be strictly positive, got {self.price} (I5)")
        if self.filled_at.tzinfo is None or self.filled_at.tzinfo.utcoffset(self.filled_at) is None:
            raise ValueError("filled_at must be timezone-aware UTC datetime (I7)")
        if not isinstance(self.side, Side):
            raise ValueError(f"Invalid side '{self.side}'")


@dataclass(frozen=True)
class VenuePosition:
    """Venue-reported account position."""

    instrument: Instrument
    quantity: Decimal
    avg_price: Decimal
    as_of: datetime

    def __post_init__(self) -> None:
        if self.as_of.tzinfo is None or self.as_of.tzinfo.utcoffset(self.as_of) is None:
            raise ValueError("as_of must be timezone-aware UTC datetime (I7)")


@dataclass(frozen=True)
class VenueCashEvent:
    """Venue-reported cash or corporate action event."""

    event_id: str
    event_type: str  # dividend, assignment, exercise, interest, fee
    amount: Decimal
    timestamp: datetime
    details: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.timestamp.tzinfo is None or self.timestamp.tzinfo.utcoffset(self.timestamp) is None:
            raise ValueError("timestamp must be timezone-aware UTC datetime (I7)")
        if self.details is not None and not isinstance(self.details, MappingProxyType):
            object.__setattr__(self, "details", MappingProxyType(dict(self.details)))
        elif self.details is None:
            object.__setattr__(self, "details", MappingProxyType({}))


# --- the account port (BROKER_PLATFORM phase 2, design §6.2-6.3) -------------------------

AccountEnv = Literal["sim", "paper", "live"]
AccountStatus = Literal["active", "closed"]
AccountEventKind = Literal["added", "removed", "changed"]


def _aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ValueError(f"{name} must be timezone-aware UTC datetime (I7)")


def _iso_z(value: datetime) -> str:
    """RFC 3339 UTC with a `Z` (contract section 2)."""
    text = value.astimezone(timezone.utc).isoformat()
    return f"{text[:-6]}Z" if text.endswith("+00:00") else text


ACCOUNT_PLATFORMS = ("nt8", "tradingview", "tradovate", "tos", "ibkr")


@dataclass(frozen=True)
class Route:
    """One route to a broker account: the platform and the name that platform uses."""

    platform: str
    local_name: str

    def __post_init__(self) -> None:
        if self.platform not in ACCOUNT_PLATFORMS:
            raise AccountRuleError(
                "bad_shape",
                "route.platform must be one of "
                f"{_json.dumps(list(ACCOUNT_PLATFORMS))}, got {_json.dumps(self.platform)}",
            )
        if not self.local_name:
            raise AccountRuleError("bad_shape", "route.local_name must be non-empty")

    def as_contract(self) -> dict[str, str]:
        return {"platform": self.platform, "local_name": self.local_name}


@dataclass(frozen=True)
class AccountRecord:
    """One broker account (contract section 2, ACCT-REGISTRY-1 decision 2).

    `class_` and `firm` are riskguard's overlays: this side sends `None` and never guesses.
    `api_ref` is engine-internal (D-13): it is excluded from `repr` and from the contract
    serializer, and never reaches an event, the ledger or a log line.
    """

    key: str
    display: str
    env: AccountEnv
    status: AccountStatus
    class_: str | None
    firm: str | None
    account_type: str | None
    api_ref: str | None = field(repr=False)
    source: str
    confirmed_at: datetime
    routes: tuple[Route, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.routes, tuple):
            object.__setattr__(self, "routes", tuple(self.routes))
        if self.class_ is not None or self.firm is not None:
            raise AccountRuleError(
                "overlay_from_engine",
                "record.class and record.firm are riskguard overlays; this side sends null",
            )
        _aware(self.confirmed_at, "confirmed_at")
        # The key, the display, routes and the rest are validated by the one Rust rule set.
        _accounts_rs.check_snapshot(
            {"records": [self.as_contract()], "confirmed_at": _iso_z(self.confirmed_at)}
        )

    @classmethod
    def of(
        cls,
        broker: str,
        account_id: str,
        *,
        env: AccountEnv,
        source: str,
        confirmed_at: datetime,
        status: AccountStatus = "active",
        account_type: str | None = None,
        api_ref: str | None = None,
        routes: Sequence[Route | tuple[str, str]] = (),
    ) -> "AccountRecord":
        """Build a record from a broker and an account id: the key and mask are Rust's."""
        return cls(
            key=_accounts_rs.key(broker, account_id),
            display=_accounts_rs.mask(account_id),
            env=env,
            status=status,
            class_=None,
            firm=None,
            account_type=account_type,
            api_ref=api_ref,
            routes=tuple(
                route if isinstance(route, Route) else Route(*route) for route in routes
            ),
            source=source,
            confirmed_at=confirmed_at,
        )

    def as_contract(self) -> dict[str, Any]:
        """Contract section 2's shape: no `api_ref`, and `confirmed_at` with a `Z`."""
        return {
            "key": self.key,
            "display": self.display,
            "env": self.env,
            "status": self.status,
            "class": self.class_,
            "firm": self.firm,
            "account_type": self.account_type,
            "routes": [route.as_contract() for route in self.routes],
            "source": self.source,
            "confirmed_at": _iso_z(self.confirmed_at),
        }


@dataclass(frozen=True)
class AccountSnapshot:
    """A broker's full account list at `confirmed_at` (the adapter's own freshness clock)."""

    records: tuple[AccountRecord, ...]
    confirmed_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.records, tuple):
            object.__setattr__(self, "records", tuple(self.records))
        _aware(self.confirmed_at, "confirmed_at")
        _accounts_rs.check_snapshot(
            {"records": [r.as_contract() for r in self.records], "confirmed_at": _iso_z(self.confirmed_at)}
        )

    def as_contract(self) -> dict[str, Any]:
        return {
            "records": [r.as_contract() for r in self.records],
            "confirmed_at": _iso_z(self.confirmed_at),
        }

    def is_stale(self, now: datetime, stale_after: int = 120) -> bool:
        """Stale at exactly `stale_after` and after it (I7: `now` is an argument)."""
        return _accounts_rs.is_stale(self.as_contract(), now, stale_after)


@dataclass(frozen=True)
class AccountEvent:
    """One membership change: added, removed or changed (contract section 3, D-13).

    `key` and `display` identify the account; `api_ref` never appears. `added` carries the
    whole record without `api_ref`, so a restart can rebuild the set. `changed` carries the
    changed fields' old and new values in memory and on the ledger; the contract wire sends
    the new values only. `seq` rises strictly per broker and is kept in the ledger.
    """

    kind: AccountEventKind
    key: str
    display: str
    at: datetime
    seq: int
    record: AccountRecord | None = None
    changes: tuple[tuple[str, Any, Any], ...] = ()

    def __post_init__(self) -> None:
        if self.kind not in ("added", "removed", "changed"):
            raise AccountRuleError("bad_shape", f"unknown account event kind {self.kind!r}")
        if not self.key:
            raise AccountRuleError("bad_key", "an account event key must be non-empty")
        if not self.display.startswith("*"):
            raise AccountRuleError("bad_shape", f"an account event display must be masked, got {self.display!r}")
        if not isinstance(self.seq, int) or isinstance(self.seq, bool) or self.seq <= 0:
            raise AccountRuleError("bad_shape", f"an account event seq must be a positive int, got {self.seq!r}")
        _aware(self.at, "at")
        object.__setattr__(self, "changes", tuple(tuple(change) for change in self.changes))
        if self.kind == "added":
            if self.record is None:
                raise AccountRuleError("bad_shape", "an added event carries the whole record")
            if self.changes:
                raise AccountRuleError("bad_shape", "an added event carries no changed fields")
        elif self.kind == "changed":
            if self.record is not None:
                raise AccountRuleError("bad_shape", "a changed event carries fields, not a record")
            if not self.changes:
                raise AccountRuleError("bad_shape", "a changed event names at least one field")
        elif self.record is not None or self.changes:
            raise AccountRuleError("bad_shape", "a removed event carries neither a record nor fields")

    @classmethod
    def from_internal(cls, data: Mapping[str, Any]) -> "AccountEvent":
        """Build from the Rust diff's internal event JSON (record and old/new changes)."""
        record = data.get("record")
        changes = tuple(
            (name, _change_value(old), _change_value(new)) for name, old, new in data.get("changes", ())
        )
        return cls(
            kind=data["kind"],
            key=data["key"],
            display=data["display"],
            at=datetime.fromisoformat(data["at"]),
            seq=int(data["seq"]),
            record=None if record is None else AccountRecord(
                key=record["key"],
                display=record["display"],
                env=record["env"],
                status=record["status"],
                class_=record["class"],
                firm=record["firm"],
                account_type=record["account_type"],
                api_ref=None,
                routes=tuple(Route(r["platform"], r["local_name"]) for r in record["routes"]),
                source=record["source"],
                confirmed_at=datetime.fromisoformat(record["confirmed_at"].replace("Z", "+00:00")),
            ),
            changes=changes,
        )

    def as_wire(self) -> dict[str, Any]:
        """Contract section 3's shape: `added.record`, or `changed.fields` new values."""
        wire: dict[str, Any] = {
            "kind": self.kind,
            "key": self.key,
            "display": self.display,
            "seq": self.seq,
            "at": _iso_z(self.at),
        }
        if self.kind == "added":
            assert self.record is not None
            wire["record"] = self.record.as_contract()
        elif self.kind == "changed":
            wire["fields"] = {name: _contract_value(new) for name, _, new in self.changes}
        return wire


def _change_value(value: Any) -> Any:
    if isinstance(value, list):
        if value and isinstance(value[0], dict):
            return tuple(Route(item["platform"], item["local_name"]) for item in value)
        return tuple(value)
    return value


def _contract_value(value: Any) -> Any:
    if isinstance(value, tuple):
        if value and isinstance(value[0], Route):
            return [route.as_contract() for route in value]
        return list(value)
    return value


@runtime_checkable
class BrokerAdapter(Protocol):
    """Protocol for all broker adapters (Architecture §4.5, I13).

    No strategy knowledge belongs in this adapter. The account port (phase 2) is the
    adapter's authority on which accounts exist at its broker and on their changes.
    """

    name: str
    env: AccountEnv
    capabilities: Capabilities
    #: The per-broker freshness window; a snapshot older than this is stale (§6.3).
    stale_after: int

    def connect(self) -> VenueIdentity:
        """Prove environment and account identity. Live requires explicit ack token."""
        ...

    def submit(self, order: VenueOrder) -> VenueAck:
        """Submit an order to the venue."""
        ...

    def cancel(self, venue_order_id: str) -> VenueAck:
        """Cancel a resting venue order."""
        ...

    def replace(self, venue_order_id: str, changes: OrderChanges) -> VenueAck:
        """Replace or modify a resting venue order."""
        ...

    def orders(self, since: datetime) -> list[VenueOrderState]:
        """Fetch venue order states updated since given timestamp."""
        ...

    def fills(self, since: datetime) -> list[VenueFill]:
        """Fetch venue fills executed since given timestamp."""
        ...

    def positions(self) -> list[VenuePosition]:
        """Fetch all positions held at the venue."""
        ...

    def cash_events(self, since: datetime) -> list[VenueCashEvent]:
        """Fetch cash events (dividends, assignments, exercises) since given timestamp."""
        ...

    def accounts(self) -> AccountSnapshot:
        """The full account list for this broker, one record per account."""
        ...

    def account_events(self, since: datetime) -> list[AccountEvent]:
        """Membership changes (`added | removed | changed`) since the given timestamp."""
        ...

"""Exact JSON round-trip for event payloads (Architecture §4.2).

Every payload stored in the ledger must fold back to an object identical to the one
that was appended. That means no lossy conversions: `Decimal` stays a string (never a
float), timestamps keep their UTC offset, and every enum keeps its member.

The type list is an explicit allowlist. An unknown type is refused rather than
serialized by guesswork (I5).
"""

from __future__ import annotations

import dataclasses
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping

from trade_engine.domain.instruments import (
    Combo,
    ComboLeg,
    Equity,
    Instrument,
    OptionContract,
    OptionRight,
    Side,
)
from trade_engine.domain.orders import Order, OrderState, OrderType, TimeInForce
from trade_engine.domain.portfolio import Fill, Lot
from trade_engine.domain.risk import RiskRuleResult, RiskVerdict
from trade_engine.domain.signals import Signal
from trade_engine.interfaces.market_data import CorporateAction

from trade_engine.ledger.events import (
    CashFlow,
    Event,
    EventKind,
    LifecycleNotice,
    Mark,
    OrderStateChange,
    VenueReconcile,
)


class PayloadCodecError(ValueError):
    """Raised when a payload cannot be encoded or decoded without guessing."""


_DECIMAL = "d"
_DATETIME = "T"
_DATE = "D"
_ENUM = "e"
_TUPLE = "t"
_MAP = "m"
_DATACLASS = "dc"
_NONE = "n"

# Leaf/nested types the codec understands. Anything else is refused.
_TAG_TO_TYPE: dict[str, type] = {
    "Equity": Equity,
    "OptionContract": OptionContract,
    "Combo": Combo,
    "ComboLeg": ComboLeg,
    "Fill": Fill,
    "Lot": Lot,
    "Signal": Signal,
    "RiskVerdict": RiskVerdict,
    "RiskRuleResult": RiskRuleResult,
    "Order": Order,
    "OrderStateChange": OrderStateChange,
    "CashFlow": CashFlow,
    "Mark": Mark,
    "VenueReconcile": VenueReconcile,
    "LifecycleNotice": LifecycleNotice,
    "CorporateAction": CorporateAction,
}
_TYPE_TO_TAG: dict[type, str] = {v: k for k, v in _TAG_TO_TYPE.items()}

_ENUM_TYPES: tuple[type[Enum], ...] = (
    Side,
    OptionRight,
    OrderType,
    OrderState,
    TimeInForce,
    EventKind,
)
_ENUM_BY_NAME: dict[str, type[Enum]] = {t.__name__: t for t in _ENUM_TYPES}


def _encode(value: Any) -> Any:
    if value is None:
        return {_NONE: True}
    # Enum must be checked before str/int: StrEnum members ARE str instances, and a
    # plain-string round-trip would silently drop the member type.
    if isinstance(value, Enum):
        return {_ENUM: type(value).__name__, "v": value.value}
    if isinstance(value, bool) or isinstance(value, (int, str)):
        return value
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise PayloadCodecError(f"Refusing to persist a non-finite Decimal: {value} (I5)")
        return {_DECIMAL: str(value)}
    if isinstance(value, datetime):
        if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
            raise PayloadCodecError("Refusing to persist a naive datetime (I7)")
        return {_DATETIME: value.isoformat()}
    if isinstance(value, date):
        return {_DATE: value.isoformat()}
    if isinstance(value, Instrument):
        return _encode_instrument(value)
    if isinstance(value, tuple):
        return {_TUPLE: [_encode(item) for item in value]}
    if isinstance(value, Mapping):
        return {_MAP: [[str(k), _encode(v)] for k, v in value.items()]}

    tag = _TYPE_TO_TAG.get(type(value))
    if tag is not None and dataclasses.is_dataclass(value):
        fields = {f.name: _encode(getattr(value, f.name)) for f in dataclasses.fields(value)}
        return {_DATACLASS: tag, "f": fields}

    raise PayloadCodecError(
        f"Refusing to persist unsupported payload type {type(value).__name__} (I5)"
    )


def _encode_instrument(instrument: Instrument) -> Any:
    if isinstance(instrument, Equity):
        return {_DATACLASS: "Equity", "f": {"symbol": instrument.symbol}}
    if isinstance(instrument, OptionContract):
        return {
            _DATACLASS: "OptionContract",
            "f": {
                "underlying": instrument.underlying,
                "expiry": _encode(instrument.expiry),
                "strike": _encode(instrument.strike),
                "right": _encode(instrument.right),
                "multiplier": instrument.multiplier,
            },
        }
    if isinstance(instrument, Combo):
        return {_DATACLASS: "Combo", "f": {"legs": _encode(instrument.legs)}}
    raise PayloadCodecError(
        f"Refusing to persist unknown instrument type {type(instrument).__name__} (I6)"
    )


def _decode(node: Any) -> Any:
    if isinstance(node, (int, str)) or isinstance(node, bool):
        return node
    if isinstance(node, list):
        raise PayloadCodecError("A bare JSON list is not a valid encoded value")

    if not isinstance(node, dict):
        raise PayloadCodecError(f"Encoded value must be an object, got {type(node).__name__}")

    if _NONE in node:
        return None
    if _DECIMAL in node:
        try:
            return Decimal(node[_DECIMAL])
        except Exception as err:  # noqa: BLE001 - surfaced as codec error
            raise PayloadCodecError(f"Invalid Decimal literal {node[_DECIMAL]!r}") from err
    if _DATETIME in node:
        parsed = datetime.fromisoformat(node[_DATETIME])
        if parsed.tzinfo is None:
            raise PayloadCodecError("Decoded datetime is naive; stored events must be UTC (I7)")
        return parsed
    if _DATE in node:
        return date.fromisoformat(node[_DATE])
    if _ENUM in node:
        enum_type = _ENUM_BY_NAME.get(node[_ENUM])
        if enum_type is None:
            raise PayloadCodecError(f"Unknown enum type '{node[_ENUM]}' (I5)")
        try:
            return enum_type(node["v"])
        except ValueError as err:
            raise PayloadCodecError(f"Invalid {node[_ENUM]} value {node['v']!r}") from err
    if _TUPLE in node:
        return tuple(_decode(item) for item in node[_TUPLE])
    if _MAP in node:
        return MappingProxyType({k: _decode(v) for k, v in node[_MAP]})
    if _DATACLASS in node:
        tag = node[_DATACLASS]
        target = _TAG_TO_TYPE.get(tag)
        if target is None:
            raise PayloadCodecError(f"Unknown payload type tag '{tag}' (I5)")
        kwargs = {name: _decode(value) for name, value in node["f"].items()}
        try:
            return target(**kwargs)
        except (TypeError, ValueError) as err:
            raise PayloadCodecError(f"Could not rebuild {tag} from stored fields: {err}") from err

    raise PayloadCodecError(f"Unrecognised encoded object with keys {sorted(node)}")


def encode_payload(payload: Any) -> Any:
    """Encode a payload into JSON-safe primitives (dicts/lists/str/numbers)."""
    return _encode(payload)


def decode_payload(encoded: Any) -> Any:
    """Rebuild a payload from its encoded form, exactly (or refuse)."""
    return _decode(encoded)


def encode_event(event: Event) -> dict[str, Any]:
    """Encode an Event into a JSON-safe dict (payload included)."""
    return {
        "account": event.account,
        "kind": event.kind.value,
        "payload": encode_payload(event.payload),
        "ts_utc": event.ts_utc.isoformat(),
        "command_id": event.command_id,
        "schema_version": event.schema_version,
        "seq": event.seq,
    }


def decode_event(encoded: Mapping[str, Any]) -> Event:
    """Rebuild an Event from its stored form, exactly (or refuse)."""
    return Event(
        account=encoded["account"],
        kind=EventKind(encoded["kind"]),
        payload=decode_payload(encoded["payload"]),
        ts_utc=datetime.fromisoformat(encoded["ts_utc"]),
        command_id=encoded.get("command_id"),
        schema_version=int(encoded.get("schema_version", 1)),
        seq=encoded.get("seq"),
    )

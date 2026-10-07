"""Exact JSON round-trip for event payloads (Architecture §4.2).

Every payload stored in the ledger must fold back to an object identical to the one
that was appended. That means no lossy conversions: `Decimal` stays a string (never a
float), timestamps keep their UTC offset, and every enum keeps its member.

The type list is an explicit allowlist. An unknown type is refused rather than
serialized by guesswork (I5).

Since P2b the rules are Rust's (``te_core::ledger::codec``). What stays here is the
plumbing between Python objects and the encoded tree, and it decides nothing:

- the walker renders each value by its type and refuses nothing; where a value has no
  encoding it writes a marker (``{"?": type}``, ``{"?i": type}``) and Rust refuses the
  tree, with the message, in the order the old encoder did;
- the builder turns a tree Rust has ACCEPTED back into objects, by tag, through each
  type's own constructor.
"""

from __future__ import annotations

import dataclasses
import json
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
from trade_engine.domain.portfolio import Fill, Lot, Position
from trade_engine.domain.risk import RiskControlChange, RiskRuleResult, RiskVerdict
from trade_engine.domain.signals import Signal
from trade_engine.interfaces.market_data import CorporateAction

from trade_engine.ledger import _rs
from trade_engine.ledger.errors import PayloadCodecError
from trade_engine.ledger.events import (
    CashFlow,
    EodRun,
    EmulatedOrderState,
    Event,
    EventKind,
    OptionLifecycle,
    Mark,
    MirrorAck,
    MirrorAllocation,
    MirrorFill,
    MirrorQueued,
    MirrorRefused,
    OrderUpdated,
    OrderStateChange,
    OrdersCreated,
    VenueHaltCleared,
    VenueReconcile,
)

# Payload types by tag: the walker's dataclass allowlist and the builder's constructors.
_TAG_TO_TYPE: dict[str, type] = {
    "Equity": Equity,
    "OptionContract": OptionContract,
    "Combo": Combo,
    "ComboLeg": ComboLeg,
    "Fill": Fill,
    "Lot": Lot,
    "Signal": Signal,
    "RiskVerdict": RiskVerdict,
    "RiskControlChange": RiskControlChange,
    "RiskRuleResult": RiskRuleResult,
    "Order": Order,
    "OrderStateChange": OrderStateChange,
    "OrdersCreated": OrdersCreated,
    "OrderUpdated": OrderUpdated,
    "EmulatedOrderState": EmulatedOrderState,
    "CashFlow": CashFlow,
    "Mark": Mark,
    "VenueReconcile": VenueReconcile,
    "VenueHaltCleared": VenueHaltCleared,
    "EodRun": EodRun,
    "OptionLifecycle": OptionLifecycle,
    "CorporateAction": CorporateAction,
    "MirrorAllocation": MirrorAllocation,
    "MirrorQueued": MirrorQueued,
    "MirrorRefused": MirrorRefused,
    "MirrorAck": MirrorAck,
    "MirrorFill": MirrorFill,
}
_TYPE_TO_TAG: dict[type, str] = {v: k for k, v in _TAG_TO_TYPE.items()}

_ENUM_BY_NAME: dict[str, type[Enum]] = {
    t.__name__: t for t in (Side, OptionRight, OrderType, OrderState, TimeInForce, EventKind)
}

# The folded-state carriers (never stored; a fold's result crossing from Rust). `state`
# and `mirror` register theirs on import; Position is the domain's.
_CARRIERS: dict[str, type] = {"Position": Position}


def register_carrier(cls: type) -> type:
    _CARRIERS[cls.__name__] = cls
    return cls


# --- the walker: objects -> tree, deciding nothing ------------------------------------


class DecimalRangeError(ValueError):
    """A decimal outside the canonical bound (P7, I5): not finite, or more than 28 decimal
    places, or a mantissa of 2**96 or more. It is refused, never rounded."""


_MANTISSA_LIMIT = 1 << 96
_MAX_SCALE = 28


def canon_decimal(value: Decimal) -> str:
    """The one canonical spelling of a decimal (docs/RUST_PORT.md, P7 S1): the exact value,
    plain notation, no trailing zeros, ``-0`` is ``0``. Byte-identical to Rust's
    ``Money::canon``; a value outside the bound raises :class:`DecimalRangeError`."""
    if not value.is_finite():
        raise DecimalRangeError(f"decimal is not finite: {value}")
    sign, digits, exponent = value.as_tuple()
    coefficient = int("".join(map(str, digits)) or "0")
    if coefficient == 0:
        return "0"
    while coefficient % 10 == 0:
        coefficient //= 10
        exponent += 1
    if exponent > 0:
        coefficient *= 10**exponent
        exponent = 0
    scale = -exponent
    if scale > _MAX_SCALE or coefficient >= _MANTISSA_LIMIT:
        raise DecimalRangeError(f"decimal outside the canonical bound: {value}")
    body = str(coefficient)
    if scale:
        body = body.rjust(scale + 1, "0")
        body = f"{body[:-scale]}.{body[-scale:]}"
    return f"-{body}" if sign else body


def _encode(value: Any) -> Any:
    if value is None:
        return {"n": True}
    # Enum before str/int: a StrEnum member IS a str, and must keep its member type.
    if isinstance(value, Enum):
        return {"e": type(value).__name__, "v": value.value}
    if isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, Decimal):
        try:
            return {"d": canon_decimal(value)}
        except DecimalRangeError:
            # outside the bound: hand Rust the raw text so ITS refusal (the one error path) is raised
            return {"d": str(value)}
    if isinstance(value, datetime):
        return {"T": value.isoformat()}
    if isinstance(value, date):
        return {"D": value.isoformat()}
    if isinstance(value, Instrument):
        return _encode_instrument(value)
    if isinstance(value, tuple):
        return {"t": [_encode(item) for item in value]}
    if isinstance(value, Mapping):
        return {"m": [[str(k), _encode(v)] for k, v in value.items()]}
    tag = _TYPE_TO_TAG.get(type(value))
    if tag is not None and dataclasses.is_dataclass(value):
        return {"dc": tag, "f": {f.name: _encode(getattr(value, f.name)) for f in dataclasses.fields(value)}}
    return {"?": type(value).__name__}


def _encode_instrument(instrument: Instrument) -> Any:
    if isinstance(instrument, Equity):
        return {"dc": "Equity", "f": {"symbol": instrument.symbol}}
    if isinstance(instrument, OptionContract):
        return {
            "dc": "OptionContract",
            "f": {
                "underlying": instrument.underlying,
                "expiry": _encode(instrument.expiry),
                "strike": _encode(instrument.strike),
                "right": _encode(instrument.right),
                "multiplier": instrument.multiplier,
            },
        }
    if isinstance(instrument, Combo):
        return {"dc": "Combo", "f": {"legs": _encode(instrument.legs)}}
    return {"?i": type(instrument).__name__}


def canon(value: Any) -> Any:
    """A folded state (carrier) as the canonical tree Rust reads it back from: map keys
    are encoded values, frozensets are ``{"fs": [...]}`` sorted by their text (a set's
    iteration order depends on its build history, so equal states would print apart).
    The walker otherwise."""
    if dataclasses.is_dataclass(value) and _CARRIERS.get(type(value).__name__) is type(value):
        return {"dc": type(value).__name__, "f": {f.name: canon(getattr(value, f.name)) for f in dataclasses.fields(value)}}
    if isinstance(value, (frozenset, set)):
        return {"fs": sorted((canon(v) for v in value), key=text)}
    if isinstance(value, tuple):
        return {"t": [canon(v) for v in value]}
    if isinstance(value, Mapping):
        return {"m": [[canon(k), canon(v)] for k, v in value.items()]}
    return _encode(value)


def text(tree: Any) -> str:
    """A tree as JSON text, in walk order (Rust sorts what it stores)."""
    return json.dumps(tree, separators=(",", ":"))


def payload_text(payload: Any) -> str:
    """The payload's stored JSON text, or the old encoder's refusal (from Rust)."""
    return _rs.call(_rs.rs.ledger_check_payload, text(_encode(payload))).decode("ascii")


def event_bytes(event: Event) -> bytes:
    """An in-memory event, encoded WITHOUT the encoder's refusals, for an in-memory fold:
    the old fold never encoded, so a NaN fill must reach the fold and be refused THERE."""
    return text(_encode_event(event, _encode(event.payload))).encode()


def _encode_event(event: Event, payload: Any) -> dict[str, Any]:
    return {
        "account": event.account,
        "kind": event.kind.value,
        "payload": payload,
        "ts_utc": event.ts_utc.isoformat(),
        "command_id": event.command_id,
        "schema_version": event.schema_version,
        "seq": event.seq,
    }


# --- the builder: an accepted tree -> objects, by tag ---------------------------------


def build(node: Any) -> Any:
    """Objects from a tree Rust accepted (or produced). No rule: each tag is its type's
    constructor; a constructor that refuses anyway is a codec refusal, as it was."""
    if not isinstance(node, dict):
        return node
    if "n" in node:
        return None
    if "d" in node:
        return Decimal(node["d"])
    if "T" in node:
        return datetime.fromisoformat(node["T"])
    if "D" in node:
        return date.fromisoformat(node["D"])
    if "e" in node:
        return _ENUM_BY_NAME[node["e"]](node["v"])
    if "t" in node:
        return tuple([build(item) for item in node["t"]])
    if "m" in node:
        return MappingProxyType({build(k): build(v) for k, v in node["m"]})
    if "fs" in node:
        return frozenset(build(item) for item in node["fs"])
    tag = node["dc"]
    target = _TAG_TO_TYPE.get(tag) or _CARRIERS[tag]
    kwargs = {name: build(value) for name, value in node["f"].items()}
    try:
        return target(**kwargs)
    except (TypeError, ValueError) as err:
        raise PayloadCodecError(f"Could not rebuild {tag} from stored fields: {err}") from err


def patch(old: Any, node: Any) -> Any:
    """Apply a Rust state delta (``canon.rs`` ``export_delta``) to the carrier it was taken
    against: ``+dc`` replaces fields, ``+m`` sets map entries, ``+t`` appends to a tuple,
    ``+fs`` adds to a frozenset; anything else is a whole value."""
    if isinstance(node, dict):
        if "+dc" in node:
            return dataclasses.replace(old, **{k: patch(getattr(old, k), v) for k, v in node["f"].items()})
        if "+m" in node:
            entries = dict(old)
            for k, v in node["+m"]:
                entries[build(k)] = build(v)
            return MappingProxyType(entries)
        if "+t" in node:
            return old + tuple([build(item) for item in node["+t"]])
        if "+fs" in node:
            return old | frozenset(build(item) for item in node["+fs"])
    return build(node)


def build_text(data: bytes | str) -> Any:
    return build(json.loads(data))


def event_from_row(
    account: str, kind: str, payload_json: str, ts_utc: str, command_id: str | None, schema_version: int, seq: int | None
) -> Event:
    """A stored row as an Event: Rust refuses it as ``decode_event`` did, or it is built."""
    _rs.call(_rs.rs.ledger_check_row, account, kind, payload_json, ts_utc, command_id, schema_version, seq)
    return Event(
        account=account,
        kind=EventKind(kind),
        payload=build(json.loads(payload_json)),
        ts_utc=datetime.fromisoformat(ts_utc),
        command_id=command_id,
        schema_version=int(schema_version),
        seq=seq,
    )


# --- the public codec --------------------------------------------------------------------


def encode_payload(payload: Any) -> Any:
    """Encode a payload into JSON-safe primitives (dicts/lists/str/numbers)."""
    tree = _encode(payload)
    _rs.call(_rs.rs.ledger_check_payload, text(tree))
    return tree


def decode_payload(encoded: Any) -> Any:
    """Rebuild a payload from its encoded form, exactly (or refuse)."""
    _rs.call(_rs.rs.ledger_check_decode_payload, text(encoded))
    return build(encoded)


def encode_event(event: Event) -> dict[str, Any]:
    """Encode an Event into a JSON-safe dict (payload included)."""
    return _encode_event(event, encode_payload(event.payload))


def decode_event(encoded: Mapping[str, Any]) -> Event:
    """Rebuild an Event from its stored form, exactly (or refuse)."""
    _rs.call(_rs.rs.ledger_check_event, text(dict(encoded)).encode())
    return Event(
        account=encoded["account"],
        kind=EventKind(encoded["kind"]),
        payload=build(encoded["payload"]),
        ts_utc=datetime.fromisoformat(encoded["ts_utc"]),
        command_id=encoded.get("command_id"),
        schema_version=int(encoded.get("schema_version", 1)),
        seq=encoded.get("seq"),
    )


__all__ = [
    "DecimalRangeError",
    "PayloadCodecError",
    "canon_decimal",
    "decode_event",
    "decode_payload",
    "encode_event",
    "encode_payload",
]

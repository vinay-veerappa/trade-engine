"""P2a gate 1: the ledger codec, pinned to explicit values (docs/RUST_PORT.md).

P0a-T1: the Rust-against-frozen-Python comparison is retired. The Python codec is a shim over the
Rust codec, so an oracle could only say "Rust differs from old Python". What is pinned here instead:

* every zoo event the codec persists re-encodes to its canonical bytes;
* each decimal, datetime, envelope and not-an-event literal has one explicit expected outcome,
  read through the public codec (`ledger_gen.py_reencode`: `decode_event`, then `encode_event`);
* the mutation and garbage sweeps check invariants: an accepted payload re-encodes to itself, and
  a refused one carries a known refusal category.
"""

import copy
import json
import random
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pytest
from ledger_gen import dumps, encoded, event_zoo, norm, py_reencode, zoo_kinds

from trade_engine.ledger.codec import PayloadCodecError
from trade_engine.ledger.events import EventKind

# Refusal categories the codec may name (see ledger_gen.kind_of). Anything else is a regression.
KNOWN_REFUSALS = frozenset(
    {"codec", "payload", "json", "key", "type", "value", "attribute", "invalid_operation", "unsupported", "strict", "overflow"}
)
STRICT = frozenset({"strict", "unsupported"})


def outcome(data: bytes):
    """("ok", canonical bytes) or ("err", category), plus the invariant every outcome must meet."""
    result = py_reencode(data)
    if result[0] == "ok":
        assert py_reencode(result[1]) == result, data  # re-encoding is idempotent
        return result
    assert result[1] in KNOWN_REFUSALS, (result, data)
    return ("err", result[1])


def _first(kind_name: str):
    return next(e for e in event_zoo() if e.kind.name == kind_name)


def _node(event) -> dict:
    return json.loads(encoded([event])[0])


def _zoo_bytes():
    """(event, bytes) per zoo event; bytes is None where the codec refuses to persist the event."""
    out = []
    for event in event_zoo():
        try:
            out.append((event, encoded([event])[0]))
        except PayloadCodecError:
            out.append((event, None))
    return out


ZOO = [(event, data) for event, data in _zoo_bytes() if data is not None]


def test_zoo_covers_every_event_kind():
    assert zoo_kinds() == set(EventKind)


def test_only_the_39_digit_mark_is_refused_at_encode():
    refused = [event for event, data in _zoo_bytes() if data is None]
    assert [(e.kind, e.payload.price) for e in refused] == [
        (EventKind.MARK, Decimal("123456789012345678901234567890.123456789")),
    ]


# --- byte parity over the valid zoo, as explicit canonical bytes ------------------------


@pytest.mark.parametrize("index", range(len(ZOO)))
def test_reencode_is_byte_identical(index):
    _, data = ZOO[index]
    assert outcome(data) == ("ok", norm(data))  # the canonical spelling of the same document


def test_reencode_is_valid_utf8_json():
    for _, data in ZOO:
        result = outcome(data)
        assert result[0] == "ok"
        json.loads(result[1].decode("utf-8"))


# --- Decimal literals: one explicit outcome each -----------------------------------------
#
# (literal, canonical value, or the refusal category). Produced by running the codec once and
# reviewed; a change here is a change in what the codec accepts.

DECIMAL_OUTCOMES = [
    ('0', '0'),
    ('0.00', '0'),
    ('-0', '0'),
    ('-0.0', '0'),
    ('1', '1'),
    ('1.10', '1.1'),
    ('1.100', '1.1'),
    ('100', '100'),
    ('1E+2', '100'),
    ('1E+3', '1000'),
    ('1.5E+3', '1500'),
    ('0E-10', '0'),
    ('0E+5', '0'),
    ('1E-28', '0.0000000000000000000000000001'),
    ('1E-30', 'codec'),
    ('12345678901234567890123456789', '12345678901234567890123456789'),
    ('123456789012345678901234567890.123456789', 'codec'),
    ('0.000000000000000000000000001', '0.000000000000000000000000001'),
    ('9999999999999999999999999999999999999999', 'codec'),
    ('-12.340', '-12.34'),
    ('00012.50', '12.5'),
    ('5E-1', '0.5'),
    ('1e2', '100'),
    ('+3.5', '3.5'),
    (' 7 ', '7'),
    ('1_000', '1000'),
    ('٣.٥', '3.5'),
    ('１２', '12'),
    ('.5', '0.5'),
    ('5.', '5'),
    ('1E', 'codec'),
    ('abc', 'codec'),
    ('', 'codec'),
    ('NaN', 'codec'),
    ('sNaN', 'codec'),
    ('Infinity', 'codec'),
    ('-Inf', 'codec'),
    ('1e999999', 'codec'),
    ('1e-999999', 'codec'),
    ('1e1000000', 'codec'),
]


@pytest.mark.parametrize(("text", "expected"), DECIMAL_OUTCOMES)
def test_decimal_literals(text, expected):
    node = _node(_first("CASH_FLOW"))
    node["payload"]["f"]["amount"] = {"d": text}
    result = outcome(dumps(node))
    if expected in KNOWN_REFUSALS:
        assert result == ("err", expected), text
    else:
        assert result[0] == "ok", (text, result)
        assert json.loads(result[1])["payload"]["f"]["amount"] == {"d": expected}, text


# --- datetime literals: one explicit outcome per placement -------------------------------
#
# (literal, ts_utc placement, as_of.T placement, as_of.D placement); a refusal names its category.

DATETIME_OUTCOMES = [
    ('2026-09-24T14:30:00+00:00', '2026-09-24T14:30:00+00:00', '2026-09-24T14:30:00+00:00', 'value'),
    ('2026-09-24T14:30:00Z', '2026-09-24T14:30:00+00:00', '2026-09-24T14:30:00+00:00', 'value'),
    ('2026-09-24T14:30:00-04:00', '2026-09-24T18:30:00+00:00', '2026-09-24T14:30:00-04:00', 'value'),
    ('2026-09-24T14:30:00+05:30', '2026-09-24T09:00:00+00:00', '2026-09-24T14:30:00+05:30', 'value'),
    ('2026-09-24T14:30:00.5+00:00', '2026-09-24T14:30:00.500000+00:00', '2026-09-24T14:30:00.500000+00:00', 'value'),
    ('2026-09-24T14:30:00.123456+00:00', '2026-09-24T14:30:00.123456+00:00', '2026-09-24T14:30:00.123456+00:00', 'value'),
    ('2026-09-24T14:30:00.1234567+00:00', '2026-09-24T14:30:00.123456+00:00', '2026-09-24T14:30:00.123456+00:00', 'value'),
    ('2026-09-24T14:30:00,25+00:00', '2026-09-24T14:30:00.250000+00:00', '2026-09-24T14:30:00.250000+00:00', 'value'),
    ('2026-09-24 14:30:00+00:00', '2026-09-24T14:30:00+00:00', '2026-09-24T14:30:00+00:00', 'value'),
    ('2026-09-24T14:30+00:00', '2026-09-24T14:30:00+00:00', '2026-09-24T14:30:00+00:00', 'value'),
    ('2026-09-24T14+00:00', '2026-09-24T14:00:00+00:00', '2026-09-24T14:00:00+00:00', 'value'),
    ('20260924T143000+0000', '2026-09-24T14:30:00+00:00', '2026-09-24T14:30:00+00:00', 'value'),
    ('2026-09-24T143000+0000', '2026-09-24T14:30:00+00:00', '2026-09-24T14:30:00+00:00', 'value'),
    ('2026-09-24T14:30:00+0530', '2026-09-24T09:00:00+00:00', '2026-09-24T14:30:00+05:30', 'value'),
    ('2026-09-24T14:30:00+05', '2026-09-24T09:30:00+00:00', '2026-09-24T14:30:00+05:00', 'value'),
    ('2026-09-24T14:30:00+05:30:15', '2026-09-24T08:59:45+00:00', '2026-09-24T14:30:00+05:30:15', 'value'),
    ('2026-09-24T14:30:00', 'payload', 'codec', 'value'),
    ('2026-09-24', 'payload', 'codec', 'codec'),
    ('2026-09-24T', 'value', 'value', 'value'),
    ('2026-09-24T24:00:00+00:00', 'value', 'value', 'value'),
    ('2026-09-24T14:30:60+00:00', 'value', 'value', 'value'),
    ('2026-13-24T14:30:00+00:00', 'value', 'value', 'value'),
    ('2026-02-30T14:30:00+00:00', 'value', 'value', 'value'),
    ('0000-01-01T00:00:00+00:00', 'value', 'value', 'value'),
    ('0001-01-01T00:00:00+00:00', '0001-01-01T00:00:00+00:00', '0001-01-01T00:00:00+00:00', 'value'),
    ('0001-01-01T00:00:00+05:00', 'overflow', '0001-01-01T00:00:00+05:00', 'value'),
    ('9999-12-31T23:59:59+00:00', '9999-12-31T23:59:59+00:00', '9999-12-31T23:59:59+00:00', 'value'),
    ('9999-12-31T23:59:59-05:00', 'overflow', '9999-12-31T23:59:59-05:00', 'value'),
    ('2026-09-24T14:30:00+24:00', 'value', 'value', 'value'),
    ('2026-09-24T14:30:00+23:59', '2026-09-23T14:31:00+00:00', '2026-09-24T14:30:00+23:59', 'value'),
    ('2026-09-24T14:30:00 +00:00', '2026-09-24T14:30:00+00:00', '2026-09-24T14:30:00+00:00', 'value'),
    ('2026-09-24T14:30:00z', 'value', 'value', 'value'),
    ('２０２６-09-24T14:30:00+00:00', 'value', 'value', 'value'),
    (' 2026-09-24T14:30:00+00:00', 'value', 'value', 'value'),
    ('2026-9-24T14:30:00+00:00', 'value', 'value', 'value'),
    ('', 'value', 'value', 'value'),
    ('x', 'value', 'value', 'value'),
    ('2026-09-24T14:30:00+00', '2026-09-24T14:30:00+00:00', '2026-09-24T14:30:00+00:00', 'value'),
    ('2024-02-29T00:00:00+00:00', '2024-02-29T00:00:00+00:00', '2024-02-29T00:00:00+00:00', 'value'),
    ('2025-02-29T00:00:00+00:00', 'value', 'value', 'value'),
    ('2026-09-24T14:30:00.+00:00', 'value', 'value', 'value'),
    ('2026-09-24T14:3:00+00:00', 'value', 'value', 'value'),
]


def _expect(data: bytes, path: list, expected):
    result = outcome(data)
    if expected in KNOWN_REFUSALS:
        assert result == ("err", expected), (path, result)
        return
    assert result[0] == "ok", (path, result)
    node = json.loads(result[1])
    for key in path:
        node = node[key]
    assert node == expected, (path, node)


@pytest.mark.parametrize(("text", "ts_utc", "as_of_t", "as_of_d"), DATETIME_OUTCOMES)
def test_datetime_literals(text, ts_utc, as_of_t, as_of_d):
    node = _node(event_zoo()[0])
    node["ts_utc"] = text
    _expect(dumps(node), ["ts_utc"], ts_utc)
    node = _node(_first("MARK"))
    node["payload"]["f"]["as_of"] = {"T": text}
    _expect(dumps(node), ["payload", "f", "as_of", "T"], as_of_t)
    node = _node(_first("MARK"))
    node["payload"]["f"]["as_of"] = {"D": text}
    _expect(dumps(node), ["payload", "f", "as_of", "D"], as_of_d)


# --- mutation: every path, every substitute ----------------------------------------------

SUBSTITUTES = [
    {"n": True}, None, 1.5, 7, -1, 0, True, False, "x", "", "1", [], {}, {"d": "1"}, {"d": "-1"}, {"d": "0"},
    {"d": "NaN"}, {"d": "Infinity"}, {"T": "2026-09-24T14:30:00+00:00"}, {"T": "2026-09-24T14:30:00"},
    {"D": "2026-09-24"}, {"e": "Side", "v": "BUY"}, {"e": "Side", "v": "SELL"}, {"e": "Side", "v": "NOPE"},
    {"e": "Nope", "v": "x"}, {"e": "Side"}, {"t": []}, {"t": [{"n": True}]}, {"m": []}, {"m": [["a", {"d": "1"}]]},
    {"dc": "Nope", "f": {}}, {"dc": "Equity"}, {"dc": "Equity", "f": {}}, {"dc": "Equity", "f": {"symbol": "ZZ"}},
    {"dc": "Equity", "f": {"symbol": ""}}, {"dc": "Equity", "f": []}, {"zz": 1},
]


def _paths(node, prefix=()):
    yield prefix
    if isinstance(node, dict):
        for k, v in node.items():
            yield from _paths(v, prefix + (k,))
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _paths(v, prefix + (i,))


def _get(node, path):
    for p in path:
        node = node[p]
    return node


def _set(node, path, value):
    for p in path[:-1]:
        node = node[p]
    node[path[-1]] = value


def _delete(node, path):
    parent = _get(node, path[:-1])
    if isinstance(parent, dict):
        del parent[path[-1]]
    else:
        parent.pop(path[-1])


def _mutants(node):
    for path in _paths(node):
        if not path:
            continue
        copy1 = copy.deepcopy(node)
        _delete(copy1, path)
        yield copy1
        for sub in SUBSTITUTES:
            copy2 = copy.deepcopy(node)
            _set(copy2, path, copy.deepcopy(sub))
            yield copy2


def test_mutated_payloads_are_accepted_or_refused_with_a_known_category():
    total = accepted = strict = 0
    for _, data in ZOO:
        for mutant in _mutants(json.loads(data.decode("utf-8"))):
            result = outcome(dumps(mutant))  # asserts idempotence / known category
            total += 1
            if result[0] == "ok":
                accepted += 1
            elif result[1] in STRICT:
                strict += 1
    assert total > 20000
    assert accepted > 100, "the mutants must include payloads the codec ACCEPTS, or the sweep proves little"
    assert strict < total * 0.15, (strict, total)  # envelope fields of a non-str type dominate


def test_random_structural_garbage():
    rnd = random.Random(20260924)
    base = [json.loads(data.decode("utf-8")) for _, data in ZOO]
    for _ in range(4000):
        node = copy.deepcopy(rnd.choice(base))
        for _ in range(rnd.randint(1, 3)):
            paths = [p for p in _paths(node) if p]
            path = rnd.choice(paths)
            try:
                if rnd.random() < 0.3:
                    _delete(node, path)
                else:
                    _set(node, path, copy.deepcopy(rnd.choice(SUBSTITUTES)))
            except (KeyError, IndexError, TypeError):
                break
        outcome(dumps(node))


# --- not an event -----------------------------------------------------------------------

NOT_EVENT_OUTCOMES = [
    (b'', 'json'),
    (b'{', 'json'),
    (b'[]', 'key'),
    (b'null', 'type'),
    (b'1', 'type'),
    (b'"x"', 'value'),
    (b'{}', 'key'),
    (b'{"account":"A"}', 'key'),
    (b'\xff\xfe', 'value'),
    (b'NaN', 'type'),
    (b'{"account":NaN}', 'key'),
    (b'{"account":"A","kind":"Mark","payload":{"n":true},"ts_utc":"2026-09-24T14:30:00+00:00"}', 'payload'),
]


@pytest.mark.parametrize(("data", "expected"), NOT_EVENT_OUTCOMES)
def test_not_an_event(data, expected):
    assert outcome(data) == ("err", expected)


# --- envelope -----------------------------------------------------------------------------

ENVELOPE_OUTCOMES = [
    ({"schema_version": 1}, "ok"), ({"schema_version": 2}, "payload"), ({"schema_version": 0}, "payload"),
    ({"schema_version": "1"}, "ok"), ({"schema_version": "x"}, "value"), ({"schema_version": None}, "type"),
    ({"schema_version": 1.0}, "codec"), ({"schema_version": True}, "ok"),
    ({"seq": 0}, "payload"), ({"seq": -3}, "payload"), ({"seq": 5}, "ok"), ({"seq": "5"}, "payload"),
    ({"seq": None}, "ok"), ({"seq": True}, "codec"),
    ({"command_id": ""}, "payload"), ({"command_id": "c"}, "ok"), ({"command_id": None}, "ok"),
    ({"command_id": 5}, "codec"),
    ({"account": ""}, "payload"), ({"account": 5}, "codec"), ({"account": "other"}, "ok"),
    ({"kind": "Nope"}, "value"), ({"kind": "Fill"}, "payload"), ({"kind": 5}, "value"),
    ({"ts_utc": "2026-09-24T14:30:00"}, "payload"), ({"ts_utc": 5}, "type"),
]

DELETED_KEY_OUTCOMES = [
    ("account", "key"), ("kind", "key"), ("payload", "key"), ("ts_utc", "key"),
    ("command_id", "ok"), ("schema_version", "ok"), ("seq", "ok"),
]


@pytest.mark.parametrize(("change", "expected"), ENVELOPE_OUTCOMES)
def test_envelope_variants(change, expected):
    node = _node(_first("MARK"))
    node.update(change)
    result = outcome(dumps(node))
    if expected == "ok":
        assert result[0] == "ok", (change, result)
    else:
        assert result == ("err", expected), (change, result)


@pytest.mark.parametrize(("key", "expected"), DELETED_KEY_OUTCOMES)
def test_deleted_envelope_key(key, expected):
    node = _node(_first("MARK"))
    del node[key]
    result = outcome(dumps(node))
    if expected == "ok":
        assert result[0] == "ok", (key, result)
    else:
        assert result == ("err", expected), (key, result)


def test_seq_and_command_id_survive_the_round_trip():
    node = _node(_first("MARK"))
    node.update({"seq": 5, "command_id": "c"})
    back = json.loads(outcome(dumps(node))[1])
    assert (back["seq"], back["command_id"]) == (5, "c")


# --- key order and unicode ----------------------------------------------------------------


def test_key_order_and_unicode_escaping():
    symbol = "Aé中\U0001f600\x7f\x1f\"\\/"
    node = _node(_first("SIGNAL_SEEN"))
    node["payload"]["f"]["symbol"] = symbol
    variants = [
        dumps(node),
        json.dumps(node, ensure_ascii=False, indent=2).encode(),
        json.dumps(node, sort_keys=False).encode(),
    ]
    for data in variants:
        result = outcome(data)
        assert result[0] == "ok", result
        assert json.loads(result[1])["payload"]["f"]["symbol"] == symbol


# --- UTC normalisation and microseconds ---------------------------------------------------

UTC_OUTCOMES = [
    ("2026-09-24T10:30:00-04:00", "2026-09-24T14:30:00+00:00"),
    ("2026-09-24T20:00:00+05:30", "2026-09-24T14:30:00+00:00"),
    ("2026-09-24T14:30:00.000001+00:00", "2026-09-24T14:30:00.000001+00:00"),
    ("2026-09-24T14:30:00.123400-00:30", "2026-09-24T15:00:00.123400+00:00"),
    ("2026-12-31T23:30:00-01:00", "2027-01-01T00:30:00+00:00"),
    ("0001-01-01T00:30:00+01:00", "overflow"),
]


@pytest.mark.parametrize(("ts", "expected"), UTC_OUTCOMES)
def test_utc_normalisation_and_microseconds(ts, expected):
    node = _node(_first("MARK"))
    node["ts_utc"] = ts
    result = outcome(dumps(node))
    if expected == "overflow":
        assert result == ("err", "overflow"), result
    else:
        assert json.loads(result[1])["ts_utc"] == expected


# --- instrument variants ------------------------------------------------------------------


def test_instrument_variants_are_generic():
    """The decoder is generic over every instrument kind; only MirrorQueued gates them."""
    from ledger_gen import AAPL, C200, SPREAD, _ev, mirror_account
    from trade_engine.domain.instruments import Side
    from trade_engine.domain.orders import OrderType, TimeInForce
    from trade_engine.ledger.events import Mark, MirrorAllocation, MirrorQueued

    ts = _first("MARK").ts_utc
    for inst in (AAPL, C200, SPREAD):
        event = _ev(EventKind.MARK, Mark(inst, Decimal("1.5"), ts))
        result = outcome(encoded([event])[0])
        assert result[0] == "ok", (inst, result)
        assert json.loads(result[1])["payload"]["f"]["price"] == {"d": "1.5"}, inst
    # MirrorQueued carries shares of an Equity (covered-call mirror, S1a)
    queued = MirrorQueued("D-1", "k", C200, Side.SELL, Decimal("1"), OrderType.MARKET, None,
                          TimeInForce.DAY, (MirrorAllocation("a", "A", Decimal("1")),), ts)
    node = _node(_ev(EventKind.MIRROR_QUEUED, queued, account=mirror_account("D-1")))
    node["payload"]["f"]["instrument"] = {"dc": "Equity", "f": {"symbol": "AAPL"}}
    result = outcome(dumps(node))
    assert result[0] == "ok", result
    assert json.loads(result[1])["payload"]["f"]["instrument"] == {"dc": "Equity", "f": {"symbol": "AAPL"}}
    # anything that is not an instrument is refused, as a codec error
    node["payload"]["f"]["instrument"] = {"dc": "Mark", "f": {}}
    assert outcome(dumps(node)) == ("err", "codec")

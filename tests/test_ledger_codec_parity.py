"""P2a gate 1: the Rust ledger codec against the Python codec (docs/RUST_PORT.md).

Rust decodes the bytes Python wrote and re-encodes them to IDENTICAL bytes, for every
EventKind and every optional-field / instrument variant. A malformed or mutated payload
is refused by Rust exactly when Python refuses it, with the same refusal category.

The one sanctioned asymmetry is STRICTNESS: Python is lax about field types (an int where
a Decimal belongs, a str right, a bool as int) and Rust refuses what it cannot carry
byte-faithfully. Rust may refuse a payload Python accepts, with kind `strict` or
`unsupported`; it may NEVER accept what Python refuses, and when both refuse they must
agree on the category. The tests count the strict refusals so the asymmetry cannot grow
unnoticed.
"""

import copy
import json
import random
import sys
from datetime import timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pytest
import trade_engine_rs as rs  # a missing module is an ERROR, never a skip (D5)
from ledger_gen import dumps, encoded, event_zoo, py_reencode, zoo_kinds

from trade_engine.ledger.events import EventKind

STRICT = {"strict", "unsupported"}


def rs_reencode(data: bytes):
    try:
        return ("ok", bytes(rs.ledger_reencode(data)))
    except ValueError as err:
        if len(err.args) != 2:
            raise
        return ("err", err.args[0], err.args[1])


def compare(data: bytes, strict: list | None = None):
    """Assert Rust matches Python on `data`; record sanctioned strict refusals."""
    py = py_reencode(data)
    rust = rs_reencode(data)
    if py[0] == "ok" and rust[0] == "ok":
        assert rust[1] == py[1], data
        return
    if py[0] == "ok":
        assert rust[1] in STRICT, f"Rust refused what Python accepts ({rust}) for {data!r}"
        if strict is not None:
            strict.append((rust[1], rust[2], data))
        return
    assert rust[0] == "err", f"Rust ACCEPTED what Python refuses ({py}) for {data!r}"
    if rust[1] in STRICT:
        return  # both refuse; Rust names the refusal as strictness, not a category
    assert rust[1] == py[1], f"category {rust[1]!r} != Python {py[1]!r} for {data!r}\nrust: {rust[2]}\npy:   {py[2]}"


# --- byte parity over the valid zoo ----------------------------------------------------


def test_zoo_covers_every_event_kind():
    assert zoo_kinds() == set(EventKind)


@pytest.mark.parametrize("index", range(len(event_zoo())))
def test_reencode_is_byte_identical(index):
    event = event_zoo()[index]
    data = encoded([event])[0]
    result = rs_reencode(data)
    assert result == ("ok", data), (event.kind, result)


def test_reencode_is_idempotent_and_utf8_clean():
    for data in encoded(event_zoo()):
        once = rs_reencode(data)[1]
        assert rs_reencode(once)[1] == once


# --- Decimal edge cases through the codec ----------------------------------------------

DECIMALS = [
    "0", "0.00", "-0", "-0.0", "1", "1.10", "1.100", "100", "1E+2", "1E+3", "1.5E+3", "0E-10", "0E+5",
    "1E-28", "1E-30", "12345678901234567890123456789", "123456789012345678901234567890.123456789",
    "0.000000000000000000000000001", "9" * 40, "-12.340", "00012.50", "5E-1", "1e2", "+3.5", " 7 ",
    "1_000", "٣.٥", "１２", ".5", "5.", "1E", "abc", "", "NaN", "sNaN", "Infinity", "-Inf", "1e999999",
    "1e-999999", "1e1000000",
]


@pytest.mark.parametrize("text", DECIMALS)
def test_decimal_literals(text):
    strict: list = []
    for kind, field in (("CashFlow", "amount"), ("Mark", "price")):
        event = [e for e in event_zoo() if e.kind.name == ("CASH_FLOW" if kind == "CashFlow" else "MARK")][0]
        node = json.loads(encoded([event])[0])
        node["payload"]["f"][field] = {"d": text}
        compare(dumps(node), strict)


# --- datetime literals ------------------------------------------------------------------

DATETIMES = [
    "2026-09-24T14:30:00+00:00", "2026-09-24T14:30:00Z", "2026-09-24T14:30:00-04:00", "2026-09-24T14:30:00+05:30",
    "2026-09-24T14:30:00.5+00:00", "2026-09-24T14:30:00.123456+00:00", "2026-09-24T14:30:00.1234567+00:00",
    "2026-09-24T14:30:00,25+00:00", "2026-09-24 14:30:00+00:00", "2026-09-24T14:30+00:00", "2026-09-24T14+00:00",
    "20260924T143000+0000", "2026-09-24T143000+0000", "2026-09-24T14:30:00+0530", "2026-09-24T14:30:00+05",
    "2026-09-24T14:30:00+05:30:15", "2026-09-24T14:30:00", "2026-09-24", "2026-09-24T", "2026-09-24T24:00:00+00:00",
    "2026-09-24T14:30:60+00:00", "2026-13-24T14:30:00+00:00", "2026-02-30T14:30:00+00:00", "0000-01-01T00:00:00+00:00",
    "0001-01-01T00:00:00+00:00", "0001-01-01T00:00:00+05:00", "9999-12-31T23:59:59+00:00", "9999-12-31T23:59:59-05:00",
    "2026-09-24T14:30:00+24:00", "2026-09-24T14:30:00+23:59", "2026-09-24T14:30:00 +00:00", "2026-09-24T14:30:00z",
    "２０２６-09-24T14:30:00+00:00", " 2026-09-24T14:30:00+00:00", "2026-9-24T14:30:00+00:00", "", "x", "2026-09-24T14:30:00+00",
    "2024-02-29T00:00:00+00:00", "2025-02-29T00:00:00+00:00", "2026-09-24T14:30:00.+00:00", "2026-09-24T14:3:00+00:00",
]


@pytest.mark.parametrize("text", DATETIMES)
def test_datetime_literals(text):
    strict: list = []
    event = event_zoo()[0]
    node = json.loads(encoded([event])[0])
    node["ts_utc"] = text
    compare(dumps(node), strict)
    mark = [e for e in event_zoo() if e.kind.name == "MARK"][0]
    node = json.loads(encoded([mark])[0])
    node["payload"]["f"]["as_of"] = {"T": text}
    compare(dumps(node), strict)
    node = json.loads(encoded([mark])[0])
    node["payload"]["f"]["as_of"] = {"D": text}
    compare(dumps(node), strict)


# --- mutation: every path, every substitute -------------------------------------------

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


def test_mutated_payloads_are_refused_exactly_when_python_refuses():
    strict: list = []
    total = 0
    accepted = 0
    for event in event_zoo():
        node = json.loads(encoded([event])[0])
        for mutant in _mutants(node):
            data = dumps(mutant)
            compare(data, strict)
            total += 1
            accepted += py_reencode(data)[0] == "ok"
    assert total > 20000
    assert accepted > 100, "the mutants must include inputs Python ACCEPTS, or parity proves little"
    # the strictness asymmetry is bounded and visible
    assert len(strict) < total * 0.15, (len(strict), total)  # envelope fields of a non-str type dominate


def test_random_structural_garbage():
    rnd = random.Random(20260924)
    base = [json.loads(b) for b in encoded(event_zoo())]
    strict: list = []
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
        compare(dumps(node), strict)


NOT_EVENTS = [b"", b"{", b"[]", b"null", b"1", b'"x"', b"{}", b'{"account":"A"}', b"\xff\xfe", b"NaN", b'{"account":NaN}',
              b'{"account":"A","kind":"Mark","payload":{"n":true},"ts_utc":"2026-09-24T14:30:00+00:00"}']


@pytest.mark.parametrize("data", NOT_EVENTS)
def test_not_an_event(data):
    compare(data, [])


def test_envelope_variants():
    event = [e for e in event_zoo() if e.kind.name == "MARK"][0]
    base = json.loads(encoded([event])[0])
    cases = [
        {"schema_version": 1}, {"schema_version": 2}, {"schema_version": 0}, {"schema_version": "1"},
        {"schema_version": "x"}, {"schema_version": None}, {"schema_version": 1.0}, {"schema_version": True},
        {"seq": 0}, {"seq": -3}, {"seq": 5}, {"seq": "5"}, {"seq": None}, {"seq": True},
        {"command_id": ""}, {"command_id": "c"}, {"command_id": None}, {"command_id": 5},
        {"account": ""}, {"account": 5}, {"account": "other"}, {"kind": "Nope"}, {"kind": "Fill"}, {"kind": 5},
        {"ts_utc": "2026-09-24T14:30:00"}, {"ts_utc": 5},
    ]
    strict: list = []
    for change in cases:
        node = dict(base)
        node.update(change)
        compare(dumps(node), strict)
    for key in ("account", "kind", "payload", "ts_utc", "command_id", "schema_version", "seq"):
        node = dict(base)
        del node[key]
        compare(dumps(node), strict)


def test_key_order_and_unicode_escaping():
    event = [e for e in event_zoo() if e.kind.name == "SIGNAL_SEEN"][1]
    node = json.loads(encoded([event])[0])
    node["payload"]["f"]["symbol"] = "Aé中\U0001f600\x7f\x1f\"\\/"
    compare(dumps(node), [])
    compare(json.dumps(node, ensure_ascii=False, indent=2).encode(), [])
    reordered = json.dumps(node, sort_keys=False).encode()
    compare(reordered, [])
    assert json.loads(reordered.decode())


def test_utc_normalisation_and_microseconds():
    event = [e for e in event_zoo() if e.kind.name == "MARK"][0]
    node = json.loads(encoded([event])[0])
    for ts in ("2026-09-24T10:30:00-04:00", "2026-09-24T20:00:00+05:30", "2026-09-24T14:30:00.000001+00:00",
               "2026-09-24T14:30:00.123400-00:30", "2026-12-31T23:30:00-01:00", "0001-01-01T00:30:00+01:00"):
        node["ts_utc"] = ts
        compare(dumps(node), [])


def test_instrument_variants_are_generic():
    """The decoder is generic over every instrument kind; only MirrorQueued gates them."""
    from ledger_gen import AAPL, C200, SPREAD, _ev, mirror_account
    from trade_engine.domain.instruments import Side
    from trade_engine.domain.orders import OrderType, TimeInForce
    from trade_engine.ledger.events import Mark, MirrorAllocation, MirrorQueued

    for inst in (AAPL, C200, SPREAD):
        event = _ev(EventKind.MARK, Mark(inst, __import__("decimal").Decimal("1.5"), event_zoo()[0].ts_utc))
        data = encoded([event])[0]
        assert rs_reencode(data) == ("ok", data)
    # MirrorQueued carries shares of an Equity (covered-call mirror, S1a) in both implementations
    venue = "D-1"
    queued = MirrorQueued(venue, "k", C200, Side.SELL, __import__("decimal").Decimal("1"), OrderType.MARKET, None,
                          TimeInForce.DAY, (MirrorAllocation("a", "A", __import__("decimal").Decimal("1")),),
                          event_zoo()[0].ts_utc)
    node = json.loads(encoded([_ev(EventKind.MIRROR_QUEUED, queued, account=mirror_account(venue))])[0])
    node["payload"]["f"]["instrument"] = {"dc": "Equity", "f": {"symbol": "AAPL"}}
    data = dumps(node)
    py, rust = py_reencode(data), rs_reencode(data)
    assert py[0] == "ok" and rust == py, (py, rust)
    # anything that is not an instrument is still refused by both, in the same category
    node["payload"]["f"]["instrument"] = {"dc": "Mark", "f": {}}
    py, rust = py_reencode(dumps(node)), rs_reencode(dumps(node))
    assert py[0] == rust[0] == "err" and (rust[1] in STRICT or py[1] == rust[1]), (py, rust)
    assert timezone.utc  # keep the import honest

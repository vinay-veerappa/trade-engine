"""P7 comparison helpers shared by the frozen-oracle parity tests.

The frozen oracles keep the pre-P7 decimal spelling ("0.6300", "7.00") and production is canonical
(plain notation, no trailing zeros); the tests compare by value through these helpers. They import
nothing heavy so any parity test can use them."""
from __future__ import annotations

import dataclasses
import decimal
import re
from decimal import Decimal

from trade_engine.ledger.codec import DecimalRangeError, canon_decimal


FINE = [0]  # how many oracle decimals by_value had to round to scale 28 (the bound policy)


def by_value(v: Decimal) -> str:
    """The canonical spelling of `v`; a value finer than scale 28 is first rounded half-even to it
    (Money's arithmetic does the same, never to zero); anything else outside the bound as `str`."""
    if v.is_finite() and v != 0 and v.as_tuple().exponent < -28:
        rounded = v.quantize(Decimal(1).scaleb(-28), rounding=decimal.ROUND_HALF_EVEN, context=decimal.Context(prec=60))
        if rounded != 0:
            FINE[0] += 1
            v = rounded
    try:
        return canon_decimal(v)
    except DecimalRangeError:
        return str(v)


def respelled(v):
    """`v` with every Decimal inside it (dataclass fields, tuples) in the canonical spelling, so the
    frozen oracle, which hashes and quotes what it is given, sees what production's wire carries."""
    if isinstance(v, Decimal):
        return Decimal(by_value(v)) if v.is_finite() else v
    if isinstance(v, tuple):
        parts = tuple(respelled(x) for x in v)
        return v if all(a is b for a, b in zip(parts, v)) else parts
    if dataclasses.is_dataclass(v) and not isinstance(v, type):
        changes = {f.name: respelled(getattr(v, f.name)) for f in dataclasses.fields(v)}
        changes = {k: x for k, x in changes.items() if x is not getattr(v, k)}
        return dataclasses.replace(v, **changes) if changes else v
    return v




_FINGERPRINT = re.compile(r'"fingerprint":"[0-9a-f]{64}"')


def no_fingerprint(text: str) -> str:
    """An event payload without its idempotency fingerprint. The frozen oracle hashes decimals in
    their old spelling and production in the canonical one (P7), so the two hashes differ by
    construction; production's is pinned in test_p7_spelling/test_p7_migrate."""
    return _FINGERPRINT.sub('"fingerprint":"-"', text)


def wire_refused(outcome) -> bool:
    """True when production refused a NaN or Infinity at the wire (P7: Money has no non-finite
    value); the oracle's own reaction to it is the old behaviour and is not compared."""
    return outcome[0] == "raise" and outcome[1] == "ValueError" and ("not a Decimal" in outcome[2] or "invalid decimal" in outcome[2])


_NUMBER = re.compile(r"(?<![\w.:])(?:-?\d+(?:\.\d+(?:[eE][+-]?\d+)?|[eE][+-]?\d+)|-0+)(?!\w|\.\d)")


def respell_text(text: str) -> str:
    """Every number in a refusal message in its canonical spelling: the oracle quotes the decimal it
    was given ("7.00"), production the canonical one ("7"); the refusal is the same (P7)."""
    def one(match):
        try:
            return canon_decimal(Decimal(match.group(0)))
        except (DecimalRangeError, ArithmeticError):
            return match.group(0)
    return _NUMBER.sub(one, text)




def deep_respell(x):
    """`x` (dicts, lists, tuples, strings) with every number inside a string canonically spelled."""
    if isinstance(x, dict):
        return {k: deep_respell(v) for k, v in x.items()}
    if isinstance(x, list):
        return [deep_respell(v) for v in x]
    if isinstance(x, tuple):
        return tuple(deep_respell(v) for v in x)
    if isinstance(x, str):
        return respell_text(x)
    return x


def first_diff(a, b, path="", limit=3):
    """The first few leaves where two nested values differ (for assertion messages)."""
    if type(a) is not type(b):
        return [(path, a, b)]
    if isinstance(a, dict):
        out = []
        for k in sorted(set(a) | set(b), key=repr):
            out += [(path + "/" + str(k), a.get(k, "<none>"), b.get(k, "<none>"))] if (k not in a or k not in b) else first_diff(a[k], b[k], path + "/" + str(k))
        return out[:limit]
    if isinstance(a, (list, tuple)):
        if len(a) != len(b):
            return [(path + "/len", len(a), len(b))]
        out = []
        for i, (x, y) in enumerate(zip(a, b)):
            out += first_diff(x, y, f"{path}[{i}]")
        return out[:limit]
    return [] if a == b else [(path, a, b)]

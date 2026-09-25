"""Option chain snapshots and their store (O1, Architecture §4.6, §4.8, I5, I7).

No historical intraday option quotes exist, so every option decision and paper fill is
made against a chain snapshot taken at the time and kept. A snapshot is stamped with
the instant it was taken (``as_of``); each quote keeps its own quote time, which is
never after the snapshot's. The store answers "the newest snapshot at or before now"
and refuses one older than the caller's max age, so a replay reads what was known then
and nothing later.
"""

from __future__ import annotations

import gzip
import json
import os
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from trade_engine.domain.instruments import OptionContract, OptionRight
from trade_engine.domain.option_roots import chain_roots
from trade_engine.interfaces.market_data import Greeks, OptionQuote, StaleDataError

SCHEMA_VERSION = 1


def _aware(stamp: datetime, what: str) -> datetime:
    if stamp.tzinfo is None or stamp.tzinfo.utcoffset(stamp) is None:
        raise ValueError(f"{what} must be timezone-aware UTC (I7): {stamp!r}")
    return stamp.astimezone(UTC)


def _sort_key(quote: OptionQuote) -> tuple:
    c = quote.contract
    return (c.expiry, c.right.value, c.strike, c.underlying)


@dataclass(frozen=True)
class ChainSnapshot:
    """One underlying's option quotes as they stood at ``as_of``.

    ``rate`` and ``dividend_yield`` are the annual continuous fractions the source quoted
    with the chain (None when it gave none; the model greeks then refuse). ``source``
    names where the snapshot came from, e.g. ``"schwab-hub"``.

    ``underlying_as_of`` is when the source last quoted the underlying itself (Schwab's
    ``underlying.quoteTime``), never after ``as_of``. None means the source did not say:
    ``as_of`` is only when the answer arrived, so a caller that needs a live market
    (the intraday service) refuses such a snapshot rather than assuming it (I5).
    """

    underlying: str
    as_of: datetime
    underlying_price: Decimal
    quotes: tuple[OptionQuote, ...]
    rate: Decimal | None
    dividend_yield: Decimal | None
    source: str
    underlying_as_of: datetime | None = None

    def __post_init__(self) -> None:
        underlying = self.underlying.strip().upper()
        object.__setattr__(self, "underlying", underlying)
        object.__setattr__(self, "as_of", _aware(self.as_of, "Snapshot as_of"))
        if self.underlying_as_of is not None:
            quoted = _aware(self.underlying_as_of, "Snapshot underlying_as_of")
            if quoted > self.as_of:
                raise ValueError(
                    f"{underlying} was quoted at {quoted.isoformat()}, after the snapshot's "
                    f"{self.as_of.isoformat()} (I5: no look-ahead)"
                )
            object.__setattr__(self, "underlying_as_of", quoted)
        if not isinstance(self.underlying_price, Decimal) or not self.underlying_price.is_finite() or self.underlying_price <= 0:
            raise ValueError(f"Snapshot underlying price must be a positive Decimal, got {self.underlying_price!r} (I5)")
        if not self.source:
            raise ValueError("Snapshot source must be named")
        roots = set(chain_roots(underlying))
        seen: set[str] = set()
        for quote in self.quotes:
            occ = quote.contract.occ
            if quote.contract.underlying not in roots:
                raise ValueError(f"{occ} is not listed under {underlying} (roots {sorted(roots)}) (I6)")
            if occ in seen:
                raise ValueError(f"{occ} appears twice in the {underlying} snapshot")
            seen.add(occ)
            if quote.as_of > self.as_of:
                raise ValueError(
                    f"{occ} was quoted at {quote.as_of.isoformat()}, after the snapshot's "
                    f"{self.as_of.isoformat()} (I5: no look-ahead)"
                )
        object.__setattr__(self, "quotes", tuple(sorted(self.quotes, key=_sort_key)))

    # -- freshness -------------------------------------------------------------

    def age_seconds(self, now: datetime) -> float:
        return (_aware(now, "now") - self.as_of).total_seconds()

    def require_fresh(self, now: datetime, max_age_seconds: float) -> ChainSnapshot:
        """This snapshot, if it was taken no more than ``max_age_seconds`` before ``now``."""
        if not isinstance(max_age_seconds, (int, float)) or isinstance(max_age_seconds, bool) or not max_age_seconds > 0:
            raise ValueError(f"max_age_seconds must be positive (I5), got {max_age_seconds!r}")
        age = self.age_seconds(now)
        if age < 0:
            raise ValueError(
                f"{self.underlying} snapshot {self.as_of.isoformat()} is after now {now.isoformat()} (I5: no look-ahead)"
            )
        if age > max_age_seconds:
            raise StaleDataError(
                f"{self.underlying} chain snapshot is {age:.0f}s old, over the {max_age_seconds:.0f}s allowed "
                f"(as_of {self.as_of.isoformat()}) (I5)"
            )
        return self

    def split_by_quote_age(self, max_quote_age_seconds: float) -> tuple[tuple[OptionQuote, ...], tuple[OptionQuote, ...]]:
        """(quotes updated within the age of the snapshot, the stale rest).

        A contract nobody has quoted for a while still appears in a fresh chain; its
        bid and ask are not today's market and must not price a fill.
        """
        fresh, stale = [], []
        for quote in self.quotes:
            age = (self.as_of - quote.as_of).total_seconds()
            (fresh if age <= max_quote_age_seconds else stale).append(quote)
        return tuple(fresh), tuple(stale)

    # -- lookup ----------------------------------------------------------------

    def expiries(self) -> tuple[date, ...]:
        return tuple(sorted({q.contract.expiry for q in self.quotes}))

    def get(self, contract: OptionContract) -> OptionQuote | None:
        occ = contract.occ
        for quote in self.quotes:
            if quote.contract.occ == occ:
                return quote
        return None

    def select(self, expiry: date, right: OptionRight, root: str | None = None) -> tuple[OptionQuote, ...]:
        """One expiry and side, in ascending strike order; ``root`` picks SPX vs SPXW."""
        return tuple(
            q for q in self.quotes
            if q.contract.expiry == expiry
            and q.contract.right is right
            and (root is None or q.contract.underlying == root.strip().upper())
        )

    # -- codec -----------------------------------------------------------------

    def to_json(self) -> str:
        body = {
            "schema_version": SCHEMA_VERSION,
            "underlying": self.underlying,
            "as_of": self.as_of.isoformat(),
            "underlying_price": str(self.underlying_price),
            "rate": None if self.rate is None else str(self.rate),
            "dividend_yield": None if self.dividend_yield is None else str(self.dividend_yield),
            "source": self.source,
            "quotes": [_quote_to_dict(q) for q in self.quotes],
        }
        if self.underlying_as_of is not None:
            # Written only when known, so a snapshot without it keeps its old bytes.
            body["underlying_as_of"] = self.underlying_as_of.isoformat()
        return json.dumps(body, sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_json(cls, text: str) -> ChainSnapshot:
        raw = json.loads(text)
        if raw.get("schema_version") != SCHEMA_VERSION:
            raise ValueError(f"Unknown chain snapshot schema {raw.get('schema_version')!r}")
        return cls(
            underlying=raw["underlying"],
            as_of=datetime.fromisoformat(raw["as_of"]),
            underlying_price=Decimal(raw["underlying_price"]),
            quotes=tuple(_quote_from_dict(q) for q in raw["quotes"]),
            rate=None if raw["rate"] is None else Decimal(raw["rate"]),
            dividend_yield=None if raw["dividend_yield"] is None else Decimal(raw["dividend_yield"]),
            source=raw["source"],
            underlying_as_of=(
                None if raw.get("underlying_as_of") is None else datetime.fromisoformat(raw["underlying_as_of"])
            ),
        )


def _dec(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _quote_to_dict(q: OptionQuote) -> dict:
    return {
        "occ": q.contract.occ,
        "multiplier": q.contract.multiplier,
        "bid": str(q.bid),
        "ask": str(q.ask),
        "bid_size": str(q.bid_size),
        "ask_size": str(q.ask_size),
        "as_of": q.as_of.isoformat(),
        "underlying_price": _dec(q.underlying_price),
        "implied_vol": _dec(q.implied_vol),
        "open_interest": q.open_interest,
        "greeks": None if q.greeks is None else {
            "delta": q.greeks.delta,
            "gamma": q.greeks.gamma,
            "theta": q.greeks.theta,
            "vega": q.greeks.vega,
            "rho": q.greeks.rho,
            "source": q.greeks.source,
        },
    }


def _quote_from_dict(d: dict) -> OptionQuote:
    greeks = d.get("greeks")
    return OptionQuote(
        contract=OptionContract.from_occ(d["occ"], multiplier=int(d["multiplier"])),
        bid=Decimal(d["bid"]),
        ask=Decimal(d["ask"]),
        bid_size=Decimal(d["bid_size"]),
        ask_size=Decimal(d["ask_size"]),
        as_of=datetime.fromisoformat(d["as_of"]),
        underlying_price=None if d["underlying_price"] is None else Decimal(d["underlying_price"]),
        implied_vol=None if d["implied_vol"] is None else Decimal(d["implied_vol"]),
        greeks=None if greeks is None else Greeks(**greeks),
        open_interest=d.get("open_interest"),
    )


class ChainSnapshotStore:
    """Snapshots on disk, one gzipped JSON file each: ``<root>/<UNDERLYING>/<as_of UTC>.json.gz``.

    Gzip because a full SPX chain is 4.4 MB of JSON and 0.46 MB compressed (measured
    2026-09-24), about 115 MB a year at one snapshot a day instead of 1.1 GB. Written once: storing the same snapshot again is a no-op (I3), and a different
    snapshot under the same underlying and instant refuses rather than overwrite a record.
    """

    def __init__(self, root: Path) -> None:
        self._root = Path(root)

    def _dir(self, underlying: str) -> Path:
        return self._root / underlying.strip().upper()

    _SUFFIX = ".json.gz"
    _STAMP = "%Y%m%dT%H%M%S%fZ"

    @classmethod
    def _name(cls, as_of: datetime) -> str:
        return as_of.astimezone(UTC).strftime(cls._STAMP) + cls._SUFFIX

    @staticmethod
    def _read(path: Path) -> str:
        return gzip.decompress(path.read_bytes()).decode("utf-8")

    def put(self, snapshot: ChainSnapshot) -> Path:
        folder = self._dir(snapshot.underlying)
        path = folder / self._name(snapshot.as_of)
        text = snapshot.to_json()
        if path.exists():
            if self._read(path) == text:
                return path
            raise ValueError(f"A different {snapshot.underlying} snapshot is already stored at {path.name}")
        folder.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        # mtime=0 keeps the bytes a function of the snapshot alone.
        tmp.write_bytes(gzip.compress(text.encode("utf-8"), compresslevel=6, mtime=0))
        os.replace(tmp, path)
        return path

    def stamps(self, underlying: str) -> tuple[datetime, ...]:
        folder = self._dir(underlying)
        if not folder.is_dir():
            return ()
        found = []
        for path in folder.glob("*" + self._SUFFIX):
            try:
                found.append(datetime.strptime(path.name[: -len(self._SUFFIX)], self._STAMP).replace(tzinfo=UTC))
            except ValueError:
                continue
        return tuple(sorted(found))

    def load(self, underlying: str, as_of: datetime) -> ChainSnapshot:
        path = self._dir(underlying) / self._name(_aware(as_of, "as_of"))
        return ChainSnapshot.from_json(self._read(path))

    def latest(self, underlying: str, now: datetime, max_age_seconds: float) -> ChainSnapshot:
        """The newest snapshot taken at or before ``now``; none, or too old, refuses (I5).

        Snapshots after ``now`` are invisible, so a replay cannot see a chain from later
        in its own day.
        """
        now = _aware(now, "now")
        known = [stamp for stamp in self.stamps(underlying) if stamp <= now]
        if not known:
            raise StaleDataError(f"No {underlying.upper()} chain snapshot at or before {now.isoformat()} (I5)")
        return self.load(underlying, known[-1]).require_fresh(now, max_age_seconds)

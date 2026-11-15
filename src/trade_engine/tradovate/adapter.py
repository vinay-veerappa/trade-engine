"""The Tradovate adapter over ntx: accounts, positions, fills (BROKER_PLATFORM phase 3).

READ-ONLY. This module links no order call and has no code path that sends one: the
three order methods of the broker protocol refuse with `UnsupportedCapability`. The
order-verb gate test scans this module and the client for the verbs as calls or URLs.

It is an I/O shell (D2, D3). Membership is decided by `te_core::accounts` through
`PollAndDiff`; the key, the mask, the staleness and the diff are Rust's. This module maps
what the ntx `/snapshot` carries onto the account carriers and decides nothing about them.

Rules it carries:
- one `VenueIdentity` per ntx identity; the env of a record is the env its identity's
  connect reported, never parsed from the account name; a conflicting or unknown env refuses;
- one record per ntx account, key `tradovate:<ntx name>`, `api_ref` the numeric Tradovate
  id (engine-only, D-13), `source` `ntx`, routes `{nt8, <name>}` and `{tradingview, D<id>}`;
- membership is read from a `/snapshot` at connect, at reconnect and on a limited early
  re-read (an account-entity SSE event is only a hint); never one snapshot per event;
- a failed read (daemon down, 401, 500, bad shape) keeps the last good list with no event
  (B3); the list is stale from 120 s on (B4);
- an identity the daemon reports as degraded keeps its last good records unchanged, so no
  `removed` is emitted for them, and they are reported stale;
- a missing account is reported as `removed`, never closed;
- money crosses as `Decimal` via the shortest `repr`; NaN and infinity refuse;
- an unknown root or month refuses with a `kind` (I5).

No clock is read (I7): the clock, the sleep, the random source and the trading-day
mapping are injected.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from trade_engine.accounts import _rs as _accounts_rs
from trade_engine.accounts.errors import AccountRuleError
from trade_engine.accounts.poll import PollAndDiff
from trade_engine.domain.instruments import Future, Platform, Side, UnresolvableInstrumentError
from trade_engine.interfaces.broker import (
    AccountEvent,
    AccountRecord,
    AccountSnapshot,
    Capabilities,
    Route,
    UnsupportedCapability,
    VenueFill,
    VenueIdentity,
    VenuePosition,
)
from trade_engine.tradovate.ntx_client import (
    NtxClient,
    NtxError,
    ReconnectBackoff,
    RefreshLimiter,
)

log = logging.getLogger(__name__)

BROKER = "tradovate"
SOURCE = "ntx"
ACCOUNT_CAP = 2_000  # the design's hard cap on broker accounts (6.8a)
_ENVS = ("sim", "paper", "live")
_READ_ONLY = "this adapter is read-only: it carries no orders"


def _decimal(value: Any, what: str) -> Decimal:
    """An ntx number as a Decimal via the shortest repr; NaN, infinity and non-numbers refuse."""
    if isinstance(value, bool) or value is None:
        raise AccountRuleError("bad_money", f"{what} is not a number")
    if isinstance(value, float):
        if not math.isfinite(value):
            raise AccountRuleError("bad_money", f"{what} is not finite")
        return Decimal(repr(value))
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, str):
        try:
            out = Decimal(value)
        except InvalidOperation:
            raise AccountRuleError("bad_money", f"{what} is not a number") from None
        if not out.is_finite():
            raise AccountRuleError("bad_money", f"{what} is not finite")
        return out
    raise AccountRuleError("bad_money", f"{what} is not a number")


def _aware(text: Any, what: str) -> datetime:
    if not isinstance(text, str):
        raise AccountRuleError("bad_shape", f"{what} is not a timestamp")
    try:
        value = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise AccountRuleError("bad_shape", f"{what} is not an ISO timestamp") from None
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise AccountRuleError("bad_shape", f"{what} has no timezone")
    return value.astimezone(timezone.utc)


def _text(row: dict[str, Any], field: str, what: str) -> str:
    value = row.get(field)
    if not isinstance(value, str) or not value.strip():
        raise AccountRuleError("bad_shape", f"{what}.{field} is missing")
    return value


@dataclass
class AdapterStats:
    """Counters: nothing the adapter drops or refuses is silent."""

    reads: int = 0
    read_failures: int = 0
    refused_snapshots: int = 0
    over_cap_refused: int = 0
    degraded_carried: int = 0


@dataclass(frozen=True)
class _Account:
    """What one read kept of one ntx account (the raw row, replaced on each read)."""

    record: AccountRecord
    identity: str
    raw: dict[str, Any]


class TradovateAdapter:
    """`BrokerAdapter` for the Tradovate accounts ntx reads. Read-only."""

    name = "tradovate"
    env = "paper"
    stale_after = 120

    def __init__(
        self,
        client: NtxClient,
        *,
        clock,
        ledger=None,
        trading_day: Callable[[datetime], date] | None = None,
        on_poll=None,
    ) -> None:
        self._client = client
        self._on_poll = on_poll
        self._clock = clock
        self._ledger = ledger
        self._trading_day = trading_day
        self._last_accounts: AccountSnapshot | None = None
        self._kept: dict[str, _Account] = {}  # key -> account; replaced per read, <= ACCOUNT_CAP
        self._identities: dict[str, VenueIdentity] = {}
        self._reference: date | None = None
        self._poller: PollAndDiff | None = None
        self.stats = AdapterStats()
        # The broker protocol's capabilities: no order types, nothing to send.
        self.capabilities = Capabilities(
            supported_order_types=frozenset(),
            supported_tifs=frozenset(),
            supports_multi_leg=False,
            supports_native_stops=False,
            supports_streaming=False,
        )

    # -- identity ------------------------------------------------------------------

    def connect(self) -> VenueIdentity:
        """Read once and return the first identity; `identities()` has them all."""
        self.accounts()
        identities = self.identities()
        if not identities:
            raise AccountRuleError("no_identity", "ntx reports no identity")
        first = identities[0]
        self.env = first.env
        return first

    def identities(self) -> list[VenueIdentity]:
        """One `VenueIdentity` per ntx identity, in the order the daemon listed them."""
        return list(self._identities.values())

    # -- the account port ------------------------------------------------------------

    def accounts(self) -> AccountSnapshot:
        """One `/snapshot` read, mapped. A failed read keeps the last good list (B3)."""
        self.stats.reads += 1
        try:
            payload = self._client.snapshot()
            now = self._clock.now_utc()
            snapshot, kept, identities, reference = self._map(payload, now)
        except NtxError as exc:
            self.stats.read_failures += 1
            log.warning("ntx read failed (%s)", exc.kind)
            return self._fallback(exc)
        except AccountRuleError as exc:
            self.stats.refused_snapshots += 1
            if exc.kind == "over_cap":
                self.stats.over_cap_refused += 1
            log.warning("ntx snapshot refused (%s)", exc.kind)
            return self._fallback(exc)
        self._last_accounts = snapshot
        self._kept = kept
        self._identities = identities
        self._reference = reference
        return snapshot

    def _fallback(self, exc: Exception) -> AccountSnapshot:
        if self._last_accounts is not None:
            return self._last_accounts
        raise exc

    def account_events(self, since: datetime) -> list[AccountEvent]:
        """The poll helper's diff over `accounts()`; membership diffs per `/snapshot`."""
        return self.poller.account_events(since)

    @property
    def poller(self) -> PollAndDiff:
        if self._poller is None:
            self._poller = PollAndDiff(
                self,
                clock=self._clock,
                ledger=self._ledger,
                stale_after=self.stale_after,
                on_poll=self._on_poll,
                pnl_source=self.pnl_rows if self._trading_day is not None else None,
            )
        return self._poller

    def pnl_rows(self, snapshot: AccountSnapshot) -> list[tuple[str, tuple[str, str] | None]]:
        """PS-4a: `(key, (realized_usd, as_of))` per account in `snapshot`, `None` for no figure.

        The figure is `realized_pnl` for the trading day of the snapshot's `confirmed_at`, as a
        plain decimal string (D6). An account whose figure cannot be read is absent, not guessed.
        """
        day = self._trading_day(snapshot.confirmed_at)  # type: ignore[misc]
        rows: list[tuple[str, tuple[str, str] | None]] = []
        for record in snapshot.records:
            figure: tuple[str, str] | None = None
            try:
                value = self.realized_pnl(record.key, day)
                if value is not None:
                    as_of = _aware(self._kept[record.key].raw.get("as_of_utc"), "account.as_of_utc")
                    figure = (format(value, "f"), as_of.strftime("%Y-%m-%dT%H:%M:%SZ"))
            except AccountRuleError as exc:
                log.warning("ntx realized pnl unreadable for %s (%s)", _accounts_rs.mask(record.display), exc.kind)
            rows.append((record.key, figure))
        return rows

    def is_stale(self, now: datetime) -> bool:
        """The whole list is stale from `stale_after` seconds after the last good read."""
        return self._last_accounts is None or self._last_accounts.is_stale(now, self.stale_after)

    def stale_keys(self, now: datetime) -> list[str]:
        """Keys whose record was last confirmed `stale_after` or more seconds ago (B4).

        A degraded identity's records keep their old `confirmed_at`, so they are listed here
        while the rest of the list stays fresh. Staleness is Rust's rule, asked per record.
        """
        out: list[str] = []
        for key, account in self._kept.items():
            one = AccountSnapshot(records=(account.record,), confirmed_at=account.record.confirmed_at)
            if one.is_stale(now, self.stale_after):
                out.append(key)
        return out

    # -- mapping ------------------------------------------------------------------

    def _map(
        self, payload: dict[str, Any], now: datetime
    ) -> tuple[AccountSnapshot, dict[str, _Account], dict[str, VenueIdentity], date]:
        rows = payload.get("accounts")
        if not isinstance(rows, list):
            raise AccountRuleError("bad_shape", "snapshot.accounts is not a list")
        if len(rows) > ACCOUNT_CAP:
            raise AccountRuleError("over_cap", f"snapshot holds more than {ACCOUNT_CAP} accounts")
        fetched = _aware(payload.get("fetchedAt"), "snapshot.fetchedAt")

        degraded = self._degraded(payload)
        kept: dict[str, _Account] = {}
        identities: dict[str, VenueIdentity] = {}
        env_of: dict[str, str] = {}
        for row in rows:
            if not isinstance(row, dict):
                raise AccountRuleError("bad_shape", "an account row is not an object")
            record, identity, env = self._record(row, now)
            if identity in env_of and env_of[identity] != env:
                raise AccountRuleError("env_conflict", "one identity reports two environments")
            env_of[identity] = env
            if identity not in identities:
                identities[identity] = VenueIdentity(
                    account_id=identity, env=env, connected_at=now, broker_name=self.name
                )
            if record.key in kept:
                raise AccountRuleError("bad_shape", "two accounts share a key")
            kept[record.key] = _Account(record=record, identity=identity, raw=row)

        # A degraded identity: its last good records stay, unchanged (never `removed`).
        for key, old in self._kept.items():
            if old.identity in degraded and key not in kept:
                kept[key] = old
                self.stats.degraded_carried += 1
                if old.identity not in identities and old.identity in self._identities:
                    identities[old.identity] = self._identities[old.identity]
                    env_of[old.identity] = self._identities[old.identity].env
                log.warning("ntx identity %s is degraded; keeping its accounts, now stale",
                            _accounts_rs.mask(old.identity))
        if len(kept) > ACCOUNT_CAP:
            raise AccountRuleError("over_cap", f"the list would hold more than {ACCOUNT_CAP} accounts")

        records = tuple(account.record for account in kept.values())
        snapshot = AccountSnapshot(records=records, confirmed_at=now)
        return snapshot, kept, identities, fetched.date()

    @staticmethod
    def _degraded(payload: dict[str, Any]) -> frozenset[str]:
        errors = payload.get("identityErrors", [])
        if not isinstance(errors, list):
            raise AccountRuleError("bad_shape", "snapshot.identityErrors is not a list")
        out = set()
        for item in errors:
            if not isinstance(item, dict):
                raise AccountRuleError("bad_shape", "an identity error is not an object")
            out.add(_text(item, "identity", "identityErrors"))
        return frozenset(out)

    def _record(self, row: dict[str, Any], now: datetime) -> tuple[AccountRecord, str, str]:
        name = _text(row, "name", "account")
        ident = _text(row, "id", "account")
        if not ident.isdigit():
            raise AccountRuleError("bad_shape", "account.id is not a numeric Tradovate id")
        session = row.get("session")
        if not isinstance(session, dict):
            raise AccountRuleError("bad_shape", "account.session is missing")
        identity = _text(session, "identity", "account.session")
        env = row.get("environment")
        if env not in _ENVS:
            raise AccountRuleError("bad_env", "account.environment is not sim, paper or live")
        record = AccountRecord.of(
            BROKER,
            name,
            env=env,
            source=SOURCE,
            confirmed_at=now,
            api_ref=ident,
            routes=(Route("nt8", name), Route("tradingview", "D" + ident)),
        )
        return record, identity, env

    # -- positions, fills, realized P&L (from the last read; no extra call) ------------

    def _account(self, account_key: str | None) -> _Account:
        if account_key is None:
            if len(self._kept) != 1:
                raise AccountRuleError(
                    "account_required", "name an account key: the adapter holds more than one"
                )
            return next(iter(self._kept.values()))
        try:
            return self._kept[account_key]
        except KeyError:
            raise AccountRuleError("unknown_account", "no such account in the last read") from None

    def _future(self, symbol: Any):
        if not isinstance(symbol, str) or self._reference is None:
            raise AccountRuleError("unresolvable", "the instrument is not text")
        try:
            return Future.from_platform(Platform.TRADOVATE, symbol, self._reference)
        except (UnresolvableInstrumentError, ValueError):
            raise AccountRuleError("unresolvable", "an unknown root or month in a Tradovate symbol") from None

    def positions(self, account_key: str | None = None) -> list[VenuePosition]:
        """The account's open positions as of its last read: signed net quantity."""
        account = self._account(account_key)
        as_of = _aware(account.raw.get("as_of_utc"), "account.as_of_utc")
        rows = account.raw.get("positions") or []
        out = []
        for row in rows:
            out.append(
                VenuePosition(
                    instrument=self._future(row.get("symbol")),
                    quantity=_decimal(row.get("net_pos"), "position.net_pos"),
                    avg_price=_decimal(row.get("avg_price"), "position.avg_price"),
                    as_of=as_of,
                )
            )
        return out

    def fills(self, since: datetime, account_key: str | None = None) -> list[VenueFill]:
        """The account's fills at or after `since`. ntx trades carry no order id."""
        account = self._account(account_key)
        out = []
        for row in account.raw.get("trades") or []:
            filled_at = _aware(row.get("executedAt"), "trade.executedAt")
            if filled_at < since:
                continue
            side = row.get("side")
            if side not in ("buy", "sell"):
                raise AccountRuleError("bad_shape", "trade.side is not buy or sell")
            fee = row.get("fee")
            out.append(
                VenueFill(
                    venue_fill_id=str(row.get("id")),
                    venue_order_id="",
                    instrument=self._future(row.get("symbol")),
                    quantity=_decimal(row.get("quantity"), "trade.quantity"),
                    price=_decimal(row.get("price"), "trade.price"),
                    filled_at=filled_at,
                    side=Side.BUY if side == "buy" else Side.SELL,
                    fee=Decimal(0) if fee is None else _decimal(fee, "trade.fee"),
                )
            )
        return out

    def realized_pnl(self, account_key: str, day: date) -> Decimal | None:
        """The account's realized P&L for `day`, or None (PNL-SOURCE-1, first source).

        The daemon reports one running figure with its `as_of_utc`; it answers for `day`
        only when the injected trading-day mapping puts that instant on `day`. With no
        mapping injected, an unknown account or another day, the answer is None.
        """
        if self._trading_day is None:
            return None
        account = self._kept.get(account_key)
        if account is None:
            return None
        as_of = _aware(account.raw.get("as_of_utc"), "account.as_of_utc")
        if self._trading_day(as_of) != day:
            return None
        return _decimal(account.raw.get("realized_pnl"), "account.realized_pnl")

    # -- the protocol's remaining reads and the refused writes ---------------------

    def orders(self, since: datetime) -> list:
        return []

    def cash_events(self, since: datetime) -> list:
        return []

    def submit(self, order):
        raise UnsupportedCapability(_READ_ONLY)

    def cancel(self, venue_order_id):
        raise UnsupportedCapability(_READ_ONLY)

    def replace(self, venue_order_id, changes):
        raise UnsupportedCapability(_READ_ONLY)


class AccountWatcher:
    """Hold one `/events` socket and turn its hints into bounded re-reads.

    On every (re)connect: open the stream, then one fresh `/snapshot` through the poller,
    which diffs it. While connected, an account-entity `props` event or a `sync` event is a
    hint: at most one early re-read per 5 s (`RefreshLimiter`), the rest coalesced into one
    trailing re-read that fires at the next frame or keepalive. Never a snapshot per event.
    A dropped stream backs off 1 s to 30 s with jitter; only a stream that stayed up for a
    minute resets the backoff. Time, sleep and random are injected (I7).
    """

    def __init__(
        self,
        adapter: TradovateAdapter,
        client: NtxClient,
        *,
        clock,
        sleep: Callable[[float], None],
        rand: Callable[[], float],
        limiter: RefreshLimiter | None = None,
        backoff: ReconnectBackoff | None = None,
    ) -> None:
        self._adapter = adapter
        self._client = client
        self._clock = clock
        self._sleep = sleep
        self.limiter = limiter if limiter is not None else RefreshLimiter()
        self.backoff = backoff if backoff is not None else ReconnectBackoff(rand)
        self.snapshots = 0
        self.connects = 0

    @staticmethod
    def _is_hint(payload: dict[str, Any]) -> bool:
        kind = payload.get("type")
        if kind == "sync":
            return True
        return kind == "props" and payload.get("entity_type") == "accounts"

    def _read(self) -> None:
        self.snapshots += 1
        self._adapter.poller.poll()

    def run_once(self) -> str:
        """One connection: open, read a fresh snapshot, consume hints until it ends.

        Returns why it ended: `stream_ended`, or the NtxError kind that stopped it.
        """
        try:
            stream = self._client.open_events()
        except NtxError as exc:
            self.backoff.dropped(self._clock.now_utc())
            return exc.kind
        self.connects += 1
        try:
            self.backoff.connected(self._clock.now_utc())
            self._read()
            self.limiter.note_read(self._clock.now_utc())
            try:
                for payload in stream.events():
                    now = self._clock.now_utc()
                    if payload is not None and self._is_hint(payload):
                        if self.limiter.trigger(now):
                            self._read()
                    elif self.limiter.due(now):
                        self._read()
            except NtxError as exc:
                return exc.kind
            return "stream_ended"
        finally:
            stream.close()
            self.backoff.dropped(self._clock.now_utc())

    def run(self, stop: Callable[[], bool]) -> None:
        """Connect, consume, back off, repeat, until `stop()`; one socket at a time."""
        while not stop():
            self.run_once()
            if stop():
                return
            self._sleep(self.backoff.delay())
            self._client.stats.reconnects += 1


__all__ = ["ACCOUNT_CAP", "AccountWatcher", "AdapterStats", "TradovateAdapter"]

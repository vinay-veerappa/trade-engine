"""The Tradovate adapter over ntx: accounts, positions, fills (BROKER_PLATFORM phase 3).

Orders (OA-3): `submit`, `cancel` and `replace` go to the ntx daemon through `NtxClient`:
one HTTP call per action, no thread, one INFO log line per action (account masked to its
last 3). A submit needs a protective stop and a riskguard verdict, or it is refused locally
with no call (`no_stop`, `verdict_required`). The clOrdId is the venue order id; an
unanswered call is `PENDING` and is never resent (the next snapshot or event reconciles it).
Exits go through ntx flatten, which is out of scope here. The order-verb gate test allows
the three order paths only and still refuses liquidate and flatten.

Reconcile (OA-5): on connect and on every stream reconnect, one `/snapshot` read rebuilds the
entry -> {stopId, targetId} map for the entries still open in the ledger's folded state. A
bracket is matched by the clOrdId ntx keeps for it (D-5; the snapshot row's `brackets` list),
never by price or quantity. An entry with no match stays unmapped (logged once) and its
children still refuse `unknown_parent`. A PENDING entry is resolved from the same read:
listed means ACCEPTED with its ntx ids, absent means REJECTED `not_at_venue`; it is never
resent. A row without a `brackets` list carries no venue truth, so nothing is mapped or
rejected from it (fail closed). Rebuilt ids get a per-session modify/cancel sequence base
(the clock in ms) so a post-restart clOrdId never replays an earlier one from the ntx store.

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
import re
import threading
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
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
    OrderChanges,
    Route,
    UnsupportedCapability,
    VenueAck,
    VenueFill,
    VenueIdentity,
    VenueOrder,
    VenueOrderState,
    VenuePosition,
)
from trade_engine.domain.orders import OrderState, OrderType, TimeInForce
from trade_engine.live_ack import REFUSAL_CODE
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
# BP-4e: riskguard calls a broker figure stale after this many seconds (pnl_source.rs).
FIGURE_FRESH_SECONDS = 10
_ENVS = ("sim", "paper", "live")
ORDER_ID_CAP = 512  # venue order ids kept for replace/cancel; the oldest is dropped
_CL_ORD_ID_OK = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_MAX_QTY = 1000


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
    """`BrokerAdapter` for the Tradovate accounts ntx reads and orders (OA-3)."""

    name = "tradovate"
    env = "paper"
    stale_after = 120
    # OA-3b: entry, stop and first target go out as one order; the OMS host fills them on the entry.
    # A plain attribute, not a Capabilities field: that dataclass is serialized into the frozen p4c tape.
    atomic_bracket = True

    def __init__(
        self,
        client: NtxClient,
        *,
        clock,
        ledger=None,
        trading_day: Callable[[datetime], date] | None = None,
        on_poll=None,
        account_bindings: Mapping[str, Any] | None = None,
        live_ack: bool = False,
    ) -> None:
        self._client = client
        # engine account id -> binding whose account_key["brokerAccountId"] is the ntx account
        self._bindings = dict(account_bindings or {})
        # I10: a live account's place or modify is refused without the acknowledgement; cancel is not.
        self._live_ack = live_ack
        self._orders: OrderedDict[str, dict[str, Any]] = OrderedDict()
        # OA-5: reconcile verdicts for PENDING entries, read back by the OMS through `orders()`.
        self._resolved: OrderedDict[str, VenueOrderState] = OrderedDict()
        self._logged_unmatched: set[str] = set()
        self._last_payload: dict[str, Any] | None = None  # the last good /snapshot, for reconcile
        self._on_poll = on_poll
        self._clock = clock
        self._ledger = ledger
        self._logged_no_ledger = False
        self._trading_day = trading_day
        self._last_accounts: AccountSnapshot | None = None
        self._kept: dict[str, _Account] = {}  # key -> account; replaced per read, <= ACCOUNT_CAP
        self._identities: dict[str, VenueIdentity] = {}
        self._reference: date | None = None
        self._poller: PollAndDiff | None = None
        # BP-4e: the oldest stamped figure of the last pnl pass (None: no figures).
        self.last_figure_as_of: datetime | None = None
        self.stats = AdapterStats()
        # Market and limit entries with a native protective stop, day orders.
        self.capabilities = Capabilities(
            supported_order_types=frozenset({OrderType.MARKET, OrderType.LIMIT, OrderType.STOP}),
            supported_tifs=frozenset({TimeInForce.DAY}),
            supports_multi_leg=False,
            supports_native_stops=True,
            supports_streaming=False,
        )

    # -- identity ------------------------------------------------------------------

    def connect(self) -> VenueIdentity:
        """Read once and return the first identity; `identities()` has them all.

        With a ledger, the same read also reconciles the open brackets (OA-5).
        """
        self.take_payload()
        self.accounts()
        identities = self.identities()
        if not identities:
            raise AccountRuleError("no_identity", "ntx reports no identity")
        first = identities[0]
        self.env = first.env
        payload = self.take_payload()
        if self._ledger is not None and payload is not None:
            self.reconcile(payload=payload)
        return first

    def take_payload(self) -> dict[str, Any] | None:
        """The raw payload of the last good read since the previous take (None: none)."""
        payload, self._last_payload = self._last_payload, None
        return payload

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
        self._last_payload = payload
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
                broker=BROKER,
            )
        return self._poller

    def pnl_rows(self, snapshot: AccountSnapshot) -> list[tuple[str, tuple[str, str] | None]]:
        """PS-4a: `(key, (realized_usd, as_of))` per account in `snapshot`, `None` for no figure.

        The figure is `realized_pnl` for the trading day of the snapshot's `confirmed_at`, as a
        plain decimal string (D6). An account whose figure cannot be read is absent, not guessed.
        """
        rows: list[tuple[str, tuple[str, str] | None]] = []
        oldest: datetime | None = None
        for key, figure in self._figures(snapshot):
            if figure is None:
                rows.append((key, None))
                continue
            value, as_of = figure
            rows.append((key, (format(value, "f"), as_of.strftime("%Y-%m-%dT%H:%M:%SZ"))))
            oldest = as_of if oldest is None or as_of < oldest else oldest
        self.last_figure_as_of = oldest
        return rows

    def _figures(self, snapshot: AccountSnapshot) -> list[tuple[str, tuple[Decimal, datetime] | None]]:
        """Per account: `(value, as_of)` or None. BP-4e: `as_of` is the read time while the
        daemon reports `ws_connected` (or its last event is within `FIGURE_FRESH_SECONDS`);
        otherwise the daemon's time, so a figure whose stream is down goes stale honestly."""
        if self._trading_day is None:
            return [(record.key, None) for record in snapshot.records]
        day = self._trading_day(snapshot.confirmed_at)  # type: ignore[misc]
        out: list[tuple[str, tuple[Decimal, datetime] | None]] = []
        for record in snapshot.records:
            figure: tuple[Decimal, datetime] | None = None
            try:
                value = self.realized_pnl(record.key, day)
                if value is not None:
                    raw = self._kept[record.key].raw
                    daemon_as_of = _aware(raw.get("as_of_utc"), "account.as_of_utc")
                    connected = raw.get("ws_connected") is True
                    figure = (value, self._figure_as_of(daemon_as_of, snapshot.confirmed_at, connected))
            except AccountRuleError as exc:
                log.warning("ntx realized pnl unreadable for %s (%s)", _accounts_rs.mask(record.display), exc.kind)
            out.append((record.key, figure))
        return out

    @staticmethod
    def _figure_as_of(daemon_as_of: datetime, read_at: datetime, connected: bool) -> datetime:
        # ntx's `as_of_utc` is its store's `last_event_utc`: it moves only on a broker event, so a
        # quiet account with a live websocket is current at read time. A disconnected one is
        # freshened only while its last event is itself within the fresh window.
        if connected or read_at - daemon_as_of <= timedelta(seconds=FIGURE_FRESH_SECONDS):
            return read_at
        return daemon_as_of

    def has_open_position(self) -> bool:
        """True when any kept account holds a non-zero net position, or a row cannot be read
        (then it is treated as open, the fresher cadence)."""
        for account in self._kept.values():
            try:
                for row in account.raw.get("positions") or []:
                    if _decimal(row.get("net_pos"), "position.net_pos") != 0:
                        return True
            except (AccountRuleError, AttributeError):
                return True
        return False

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

    def orders(self, since: datetime) -> list[VenueOrderState]:
        """The venue states reconcile resolved for PENDING entries (OA-5), at or after `since`."""
        return [item for item in self._resolved.values() if item.updated_at >= since]

    # -- reconcile (OA-5) ------------------------------------------------------------

    def reconcile(self, ledger=None, *, payload: dict[str, Any] | None = None) -> ReconcileReport:
        """Rebuild the bracket map and resolve PENDING entries from one `/snapshot` read.

        `payload` reuses a read just made (connect, reconnect); otherwise one read is made here.
        A failed read changes nothing. Never sends an order. The caller runs the OMS
        `reconcile_order` for `report.accepted + report.rejected`.
        """
        report = ReconcileReport()
        ledger = ledger if ledger is not None else self._ledger
        if ledger is None:
            if not self._logged_no_ledger:
                self._logged_no_ledger = True
                log.info("ntx reconcile skipped (no ledger)")
            return report
        if payload is None:
            try:
                payload = self._client.snapshot()
            except NtxError as exc:
                log.warning("ntx reconcile read failed (%s)", exc.kind)
                report.read_failed = True
                return report
        rows = payload.get("accounts") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            log.warning("ntx reconcile refused (bad_shape)")
            report.read_failed = True
            return report
        by_name = {row["name"]: row for row in rows
                   if isinstance(row, dict) and isinstance(row.get("name"), str)}
        now = self._clock.now_utc()
        base = int(now.timestamp() * 1000)  # per-session clOrdId sequence base (see module doc)
        known_accounts = set(ledger.accounts())
        for engine_account in self._bindings:
            ntx_account = self._ntx_account(engine_account)
            if ntx_account is None or engine_account not in known_accounts:
                continue
            index, authoritative = _bracket_index(by_name.get(ntx_account))
            state = ledger.state(engine_account)
            for order in list(state.orders.values()):
                if order.parent_order_id is not None:
                    continue
                venue = state.venue_order_ids.get(order.order_id) or order.order_id
                kids = [item for item in state.orders.values()
                        if item.parent_order_id in (order.order_id, venue) and item.state in _LIVE]
                pending = order.state is OrderState.PENDING_UNKNOWN
                if not (order.state in _LIVE or (order.state is OrderState.FILLED and kids)):
                    continue
                if venue in self._orders and not pending:
                    continue  # mapped this session already; its record is current
                wire = _wire_cl_ord_id(venue)
                found = index.get(wire) if index is not None and wire is not None else None
                if found is None:
                    if pending and index is not None and authoritative:
                        self._resolve(venue, OrderState.REJECTED, order.quantity, now)
                        report.rejected.append(order.order_id)
                        log.info("ntx reconcile: venue_order=%s account=%s status=REJECTED code=not_at_venue",
                                 venue, _mask(ntx_account))
                        continue
                    report.unmatched.append(order.order_id)
                    if venue not in self._logged_unmatched:
                        self._logged_unmatched.add(venue)
                        log.warning("ntx reconcile: venue_order=%s account=%s unmatched; left unmapped",
                                    venue, _mask(ntx_account))
                    continue
                self._map_bracket(venue, order, kids, found, ntx_account, base, state.venue_order_ids)
                report.mapped.append(order.order_id)
                if pending:
                    self._resolve(venue, OrderState.ACCEPTED, order.quantity, now)
                    report.accepted.append(order.order_id)
                    log.info("ntx reconcile: venue_order=%s account=%s status=ACCEPTED",
                             venue, _mask(ntx_account))
        log.info("ntx reconcile: mapped=%d unmatched=%d accepted=%d rejected=%d",
                 len(report.mapped), len(report.unmatched), len(report.accepted), len(report.rejected))
        return report

    def _map_bracket(self, venue: str, order, kids, found: dict[str, Any], ntx_account: str, base: int,
                     venue_ids: Mapping[str, str]) -> None:
        """Rebuild the entry record and its stop and first-target child records from the ledger."""
        stops = [item for item in kids if item.order_type is OrderType.STOP]
        targets = sorted((item for item in kids if item.order_type is OrderType.LIMIT),
                         key=lambda item: item.order_id)
        stop = stops[0] if stops else None
        target = targets[0] if targets else None
        stop_id, target_id = found["stopId"], found.get("targetId")
        qty = int(order.quantity)
        entry_spec = ({"type": "limit", "price": _price(order.limit_price)}
                      if order.order_type is OrderType.LIMIT and order.limit_price is not None
                      else {"type": "market"})
        adopted = (OrderState.SUBMITTED, OrderState.ACCEPTED, OrderState.PARTIALLY_FILLED,
                   OrderState.PENDING_UNKNOWN)
        def wire_key(child) -> str:  # records are keyed by the venue id, as submit/_adopt_child key them
            return venue_ids.get(child.order_id) or child.order_id

        target_child = wire_key(target) if target is not None and target.state in adopted else None
        self._remember(venue, {
            "entryId": found["entryId"], "stopId": stop_id, "targetId": target_id,
            "account": ntx_account, "qty": qty, "entry": entry_spec, "seq": base,
            "stopQty": int(stop.quantity) if stop is not None else qty,
            "stopPrice": _price(stop.stop_price) if stop is not None and stop.stop_price is not None else None,
            "targetQty": int(target.quantity) if target is not None else qty,
            "targetPrice": (_price(target.limit_price)
                            if target is not None and target.limit_price is not None else None),
            "targetChild": target_child,
        })
        # A child adopted before the restart (past NEW) is replaced and cancelled by its own id.
        if stop is not None and stop.state in adopted and stop.stop_price is not None:
            self._remember(wire_key(stop), {
                "account": ntx_account, "entryId": stop_id, "stopId": stop_id, "qty": int(stop.quantity),
                "seq": base, "child": "stop", "side": stop.side,
                "entry": {"type": "stop", "price": _price(stop.stop_price)},
            })
        if (target_child is not None and target_id is not None and target is not None
                and target.limit_price is not None):
            self._remember(target_child, {
                "account": ntx_account, "entryId": target_id, "stopId": None, "qty": int(target.quantity),
                "seq": base, "child": "target", "side": target.side,
                "entry": {"type": "limit", "price": _price(target.limit_price)},
            })

    def _resolve(self, venue: str, state: OrderState, qty: Decimal, now: datetime) -> None:
        self._resolved[venue] = VenueOrderState(venue, state, Decimal(0), qty, now)
        self._resolved.move_to_end(venue)
        while len(self._resolved) > ORDER_ID_CAP:
            self._resolved.popitem(last=False)

    def cash_events(self, since: datetime) -> list:
        return []

    def submit(self, order: VenueOrder) -> VenueAck:
        if order.parent_order_id is not None and order.order_type in (OrderType.STOP, OrderType.LIMIT):
            return self._adopt_child(order)
        if order.order_type not in (OrderType.MARKET, OrderType.LIMIT):  # STOP only as an adopted child
            raise UnsupportedCapability(f"order type {order.order_type} not supported on tradovate")
        if order.tif not in self.capabilities.supported_tifs:
            raise UnsupportedCapability(f"time in force {order.tif} not supported on tradovate")
        accounts = {item.account_id for item in order.allocations}
        if len(accounts) != 1:
            raise UnsupportedCapability("netted orders not supported on tradovate")
        engine_account = next(iter(accounts))
        cl = order.venue_order_id
        if self._live_refused(self._ntx_account(engine_account)):
            return self._refused(cl, "place", engine_account, REFUSAL_CODE)
        # Local refusals: nothing reaches ntx unstopped, unchecked or unmapped.
        if order.protective_stop_price is None:
            return self._refused(cl, "place", engine_account, "no_stop")
        verdict = order.riskguard_verdict
        if verdict is None or not verdict.checked_at or verdict.verdict_revision is None:
            return self._refused(cl, "place", engine_account, "verdict_required")
        ntx_account = self._ntx_account(engine_account)
        if ntx_account is None:
            return self._refused(cl, "place", engine_account, "unmapped_account")
        wire_cl = _wire_cl_ord_id(cl)
        if wire_cl is None:
            return self._refused(cl, "place", ntx_account, "cl_ord_id_invalid")
        if order.quantity != order.quantity.to_integral_value() or not 1 <= order.quantity <= _MAX_QTY:
            return self._refused(cl, "place", ntx_account, "qty_invalid")
        try:
            symbol = order.instrument.to_platform(Platform.TRADOVATE)
        except Exception:  # noqa: BLE001 - any unresolvable instrument is a local refusal
            return self._refused(cl, "place", ntx_account, "symbol_invalid")
        body: dict[str, Any] = {
            "account": ntx_account,
            "clOrdId": wire_cl,
            "symbol": symbol,
            "side": "Buy" if order.side is Side.BUY else "Sell",
            "qty": int(order.quantity),
            "entry": _entry(order),
            "stop": _price(order.protective_stop_price),
        }
        if order.protective_target_price is not None:
            body["target"] = _price(order.protective_target_price)
        body["verdict"] = _verdict(verdict)
        ack, response = self._send("place", cl, ntx_account, self._client.place, body)
        if response is not None and response["outcome"] == "SENT" and response.get("orderIds"):
            ids = dict(response["orderIds"])
            ids.update(account=ntx_account, qty=int(order.quantity), entry=body["entry"], seq=0,
                       stopQty=int(order.quantity), targetQty=int(order.quantity),
                       stopPrice=body["stop"], targetPrice=body.get("target"), targetChild=None)
            self._remember(cl, ids)
        return ack

    def _adopt_child(self, order: VenueOrder) -> VenueAck:
        """OA-3b: a protective child of an entry this adapter placed adopts the ntx stop or target.

        The bracket's stop and first target went out with the entry, so a child submit makes no
        place call. It maps the child to the stored ntx order id and sends at most one modify, when
        the child's quantity or price differs from what ntx holds. A looser stop is refused.
        """
        cl = order.venue_order_id
        accounts = {item.account_id for item in order.allocations}
        if len(accounts) != 1:
            raise UnsupportedCapability("netted orders not supported on tradovate")
        ntx_account = self._ntx_account(next(iter(accounts)))
        if self._live_refused(ntx_account):
            return self._refused(cl, "adopt", ntx_account, REFUSAL_CODE)
        parent = self._orders.get(order.parent_order_id)
        if parent is None:
            return self._refused(cl, "adopt", ntx_account, "unknown_parent")
        if parent["account"] != ntx_account:
            return self._refused(cl, "adopt", ntx_account, "account_mismatch")
        is_stop = order.order_type is OrderType.STOP
        if is_stop:
            ntx_id, held_qty, held_price = parent.get("stopId"), parent["stopQty"], parent["stopPrice"]
            price = order.stop_price
        else:
            ntx_id, held_qty, held_price = parent.get("targetId"), parent["targetQty"], parent["targetPrice"]
            price = order.limit_price
            if ntx_id is None or held_price is None:
                return self._refused(cl, "adopt", ntx_account, "no_target")
            claimed = parent.get("targetChild")
            if claimed is not None and claimed != cl:
                return self._refused(cl, "adopt", ntx_account, "extra_target")
        if ntx_id is None or price is None:
            return self._refused(cl, "adopt", ntx_account, "no_stop")
        qty = order.quantity
        if qty != qty.to_integral_value() or not 1 <= qty <= _MAX_QTY:
            return self._refused(cl, "adopt", ntx_account, "qty_invalid")
        new_price = _price(price)
        price_changed = Decimal(new_price) != Decimal(held_price)
        if is_stop and price_changed:
            new, held = Decimal(new_price), Decimal(held_price)
            if (new > held) if order.side is Side.BUY else (new < held):
                return self._refused(cl, "adopt", ntx_account, "stop_loosen")
        seq = 0
        if price_changed or int(qty) != held_qty:
            wire_cl = _wire_cl_ord_id(f"{cl}.m0")
            if wire_cl is None:
                return self._refused(cl, "modify", ntx_account, "cl_ord_id_invalid")
            body = {
                "account": ntx_account,
                "clOrdId": wire_cl,
                "orderId": ntx_id,
                "qty": int(qty),
                "to": {"type": "stop" if is_stop else "limit", "price": new_price},
            }
            ack, response = self._send("modify", cl, ntx_account, self._client.modify, body)
            if response is None or response["outcome"] != "SENT":
                return ack
            seq = 1
        else:
            ack = VenueAck(cl, "ACCEPTED", self._clock.now_utc(), None)
            log.info("ntx order adopt: venue_order=%s account=%s status=ACCEPTED", cl, _mask(ntx_account))
        parent["stopQty" if is_stop else "targetQty"] = int(qty)
        parent["stopPrice" if is_stop else "targetPrice"] = new_price
        if not is_stop:
            parent["targetChild"] = cl
        self._remember(cl, {
            "account": ntx_account, "entryId": ntx_id, "stopId": ntx_id if is_stop else None,
            "qty": int(qty), "seq": seq, "child": "stop" if is_stop else "target", "side": order.side,
            "entry": {"type": "stop" if is_stop else "limit", "price": new_price},
        })
        return ack

    def cancel(self, venue_order_id: str) -> VenueAck:
        known = self._orders.get(venue_order_id)
        if known is None:
            return self._refused(venue_order_id, "cancel", None, "unknown_order")
        wire_cl = _wire_cl_ord_id(f"{venue_order_id}.c{known['seq']}")
        if wire_cl is None:
            return self._refused(venue_order_id, "cancel", known["account"], "cl_ord_id_invalid")
        body = {"account": known["account"], "clOrdId": wire_cl, "orderId": known["entryId"]}
        ack, response = self._send("cancel", venue_order_id, known["account"], self._client.cancel, body)
        if response is not None:
            known["seq"] += 1
            if response["outcome"] == "SENT":
                self._orders.pop(venue_order_id, None)
        return ack

    def replace(self, venue_order_id: str, changes: OrderChanges) -> VenueAck:
        known = self._orders.get(venue_order_id)
        if known is None:
            return self._refused(venue_order_id, "modify", None, "unknown_order")
        account = known["account"]
        if self._live_refused(account):
            return self._refused(venue_order_id, "modify", account, REFUSAL_CODE)
        if changes.new_limit_price is not None and changes.new_stop_price is not None:
            return self._refused(venue_order_id, "modify", account, "one_change_only")
        qty = Decimal(known["qty"]) if changes.new_quantity is None else changes.new_quantity
        if qty != qty.to_integral_value() or not 1 <= qty <= _MAX_QTY:
            return self._refused(venue_order_id, "modify", account, "qty_invalid")
        if changes.new_stop_price is not None and known.get("child") == "stop":
            held, new = Decimal(known["entry"]["price"]), changes.new_stop_price
            if (new > held) if known["side"] is Side.BUY else (new < held):
                return self._refused(venue_order_id, "modify", account, "stop_loosen")
        if changes.new_stop_price is not None:
            order_id, to = known["stopId"], {"type": "stop", "price": _price(changes.new_stop_price)}
        elif changes.new_limit_price is not None:
            order_id, to = known["entryId"], {"type": "limit", "price": _price(changes.new_limit_price)}
        elif changes.new_quantity is not None:
            order_id, to = known["entryId"], known["entry"]
        else:
            return self._refused(venue_order_id, "modify", account, "no_change")
        wire_cl = _wire_cl_ord_id(f"{venue_order_id}.m{known['seq']}")
        if wire_cl is None:
            return self._refused(venue_order_id, "modify", account, "cl_ord_id_invalid")
        body: dict[str, Any] = {
            "account": account,
            "clOrdId": wire_cl,
            "orderId": order_id,
            "qty": int(qty),
            "to": to,
        }
        verdict = changes.riskguard_verdict
        if verdict is not None:
            if not verdict.checked_at or verdict.verdict_revision is None:
                return self._refused(venue_order_id, "modify", account, "verdict_required")
            body["verdict"] = _verdict(verdict)
        ack, response = self._send("modify", venue_order_id, account, self._client.modify, body)
        if response is not None:
            known["seq"] += 1
            if response["outcome"] == "SENT":
                known["qty"] = int(qty)
                if to["type"] != "stop" or known.get("child") == "stop":
                    known["entry"] = to
        return ack

    # -- order helpers -------------------------------------------------------------

    def _live_refused(self, ntx_account: str | None) -> bool:
        """I10: a live account's place or modify needs the acknowledgement.

        The env is the account's identity env from the last snapshot. An account the adapter has
        not read has no known env and is not treated as live here; connect() reads first.
        """
        if self._live_ack or ntx_account is None:
            return False
        account = self._kept.get(_accounts_rs.key(BROKER, ntx_account))
        if account is None:
            return False
        identity = self._identities.get(account.identity)
        env = identity.env if identity is not None else account.record.env
        return env == "live"

    def _ntx_account(self, engine_account: str) -> str | None:
        binding = self._bindings.get(engine_account)
        if binding is None:
            return None
        name = binding.account_key.get("brokerAccountId")
        return name if isinstance(name, str) and name else None

    def _remember(self, venue_order_id: str, record: dict[str, Any]) -> None:
        self._orders[venue_order_id] = record
        self._orders.move_to_end(venue_order_id)
        while len(self._orders) > ORDER_ID_CAP:
            self._orders.popitem(last=False)

    def _refused(self, venue_order_id: str, op: str, account: str | None, code: str) -> VenueAck:
        log.info("ntx order %s refused locally: venue_order=%s account=%s code=%s",
                 op, venue_order_id, _mask(account), code)
        return VenueAck(venue_order_id, "REJECTED", self._clock.now_utc(), code)

    def _send(
        self,
        op: str,
        venue_order_id: str,
        account: str,
        call: Callable[[dict[str, Any]], dict[str, Any]],
        body: dict[str, Any],
    ) -> tuple[VenueAck, dict[str, Any] | None]:
        """One ntx call, never retried. Returns the ack and the daemon's answer (None: no answer)."""
        response: dict[str, Any] | None = None
        try:
            response = call(body)
        except NtxError as exc:
            if exc.kind in ("pending", "invalid"):
                # Written, no usable answer: the outcome is unknown. Never resend.
                status, message = "PENDING", "outcome_unknown"
            elif exc.kind == "invalid_body":
                status, message = "REJECTED", exc.message
            else:
                status, message = "REJECTED", exc.kind
        else:
            outcome = response["outcome"]
            if outcome == "SENT":
                status, message = "ACCEPTED", None
            elif outcome == "PENDING":
                status, message = "PENDING", response.get("code") or "outcome_unknown"
            else:
                status, message = "REJECTED", response.get("code") or response.get("reason") or "refused"
        log.info("ntx order %s: venue_order=%s account=%s status=%s code=%s",
                 op, venue_order_id, _mask(account), status, message)
        return VenueAck(venue_order_id, status, self._clock.now_utc(), message), response


_LIVE = frozenset({
    OrderState.NEW, OrderState.SUBMITTED, OrderState.ACCEPTED,
    OrderState.PARTIALLY_FILLED, OrderState.PENDING_UNKNOWN,
})


@dataclass
class ReconcileReport:
    """What one reconcile did, by engine order id."""

    mapped: list[str] = field(default_factory=list)
    unmatched: list[str] = field(default_factory=list)
    accepted: list[str] = field(default_factory=list)  # PENDING -> ACCEPTED
    rejected: list[str] = field(default_factory=list)  # PENDING -> REJECTED not_at_venue
    read_failed: bool = False


def _bracket_index(row: dict[str, Any] | None) -> tuple[dict[str, dict[str, Any]] | None, bool]:
    """`(clOrdId -> ntx ids, authoritative)` from one snapshot account row.

    The index is None when the row carries no bracket truth at all (an ntx without `brackets`).
    Order rows are read first (`clOrdId` plus `brackets {parentId, stopId, targetId?}`, OA-5b),
    then the account-level `brackets` list fills any id the rows did not give. `authoritative` is
    False when ntx says it has a placement of unknown outcome (`bracketsAuthoritative: false`):
    an entry missing from the index is then not proof it is absent from the venue.
    """
    if row is None:
        return None, False
    out: dict[str, dict[str, Any]] = {}
    seen = False
    orders = row.get("orders")
    for item in orders if isinstance(orders, list) else ():
        links = item.get("brackets") if isinstance(item, dict) else None
        cl = item.get("clOrdId") if isinstance(item, dict) else None
        if (isinstance(cl, str) and isinstance(links, dict)
                and links.get("parentId") is not None and links.get("stopId") is not None):
            seen = True
            out.setdefault(cl, {"clOrdId": cl, "entryId": links["parentId"], "stopId": links["stopId"],
                                "targetId": links.get("targetId")})
    brackets = row.get("brackets")
    if isinstance(brackets, list):
        seen = True
        for item in brackets:
            if (isinstance(item, dict) and isinstance(item.get("clOrdId"), str)
                    and item.get("entryId") is not None and item.get("stopId") is not None):
                out.setdefault(item["clOrdId"], item)
    if not seen:
        return None, False
    return out, row.get("bracketsAuthoritative") is not False


def _mask(account: str | None) -> str:
    return "***" + account[-3:] if account else "***"


def _wire_cl_ord_id(text: str) -> str | None:
    """The ntx clOrdId spelling: the id itself, with the split-suffix colon written as a dot."""
    text = text.replace(":", ".")
    return text if _CL_ORD_ID_OK.match(text) else None


def _price(value: Decimal) -> str:
    return format(value, "f")


def _entry(order: VenueOrder) -> dict[str, str]:
    if order.order_type is OrderType.LIMIT:
        return {"type": "limit", "price": _price(order.limit_price)}
    return {"type": "market"}


def _verdict(verdict: Any) -> dict[str, Any]:
    return {
        "allowed": verdict.allowed,
        "verdictRevision": verdict.verdict_revision,
        "checkedAt": verdict.checked_at,
    }


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
        read: Callable[[], None] | None = None,
    ) -> None:
        self._adapter = adapter
        self._client = client
        self._clock = clock
        self._sleep = sleep
        # BP-4e: where a read goes. None polls the adapter's poller on this thread (BP-3).
        self._read_fn = read
        self.limiter = limiter if limiter is not None else RefreshLimiter()
        self.backoff = backoff if backoff is not None else ReconnectBackoff(rand)
        self.snapshots = 0
        self.connects = 0
        # REGFEED-STOP-1: the open stream, so `wake()` (another thread, on stop) can reach it.
        self._lock = threading.Lock()
        self._stream = None
        self._stopping = False

    def wake(self) -> None:
        """Stop path (REGFEED-STOP-1): end a read blocked on the open `/events` socket now.

        The caller sets its stop flag first; this only marks the watcher stopping and shuts the
        socket, so `run_once` returns at once and `run` exits. Never blocks.
        """
        with self._lock:
            self._stopping = True
            stream = self._stream
        if stream is not None:
            stream.wake()

    @staticmethod
    def _is_hint(payload: dict[str, Any]) -> bool:
        kind = payload.get("type")
        if kind == "sync":
            return True
        return kind == "props" and payload.get("entity_type") == "accounts"

    def _read(self) -> None:
        self.snapshots += 1
        if self._read_fn is None:
            self._adapter.poller.poll()
        else:
            self._read_fn()

    def _reconcile(self) -> None:
        """OA-5: on each (re)connect, reconcile brackets from the read just made (one read)."""
        if getattr(self._adapter, "_ledger", None) is None:
            return
        if self._read_fn is None:
            payload = self._adapter.take_payload()
            if payload is None:
                return  # the read failed; the next reconnect tries again
            self._adapter.reconcile(payload=payload)
        else:
            self._adapter.reconcile()  # BP-4e: the read went elsewhere; one read of our own

    def run_once(self) -> str:
        """One connection: open, read a fresh snapshot, consume hints until it ends.

        Returns why it ended: `stream_ended`, or the NtxError kind that stopped it.
        """
        try:
            stream = self._client.open_events()
        except NtxError as exc:
            self.backoff.dropped(self._clock.now_utc())
            return exc.kind
        with self._lock:
            if self._stopping:  # a stop raced the connect: do not read at all
                stream.close()
                return "stopped"
            self._stream = stream
        self.connects += 1
        try:
            self.backoff.connected(self._clock.now_utc())
            if getattr(self._adapter, "_ledger", None) is not None:
                self._adapter.take_payload()  # drop a stale payload; reconcile uses this read's
            self._read()
            self._reconcile()
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
            with self._lock:
                self._stream = None
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


__all__ = ["ACCOUNT_CAP", "AccountWatcher", "AdapterStats", "ReconcileReport", "TradovateAdapter"]

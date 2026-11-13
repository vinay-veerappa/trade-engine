"""The Schwab Trader API accounts adapter (BROKER_PLATFORM phase 5b, read-only).

READ-ONLY. It has no HTTP client and no order path: the hub (`schwab_hub.py`) is the only
Schwab client (D3), so the adapter takes the two endpoints' **response text as arguments**
and maps it. Nothing here opens a socket, reads a token or sends anything.

It is an I/O shell around `te_core::accounts`: the key, the mask, the diff, the freshness,
the interval and the event ring are Rust's (`PollAndDiff`, phase 2). This module maps what
the two responses carry onto the account carriers and decides nothing about them.

Rules it carries:

- **Membership is the poll.** `GET /trader/v1/accounts/accountNumbers` is the account list;
  `GET /trader/v1/accounts` is read for its fields only (the account type). An account that
  is no longer listed is reported `removed`, never `closed` (the API has no closed marker).
- **One record per account**, key `schwab:<accountNumber>` (case kept), `display` the mask
  (`*194`), `source` `schwab`, `env` the configured environment, `status` `active`, routes
  `{platform: tos, local_name: <accountNumber>}` (plan phase 5a: the account is read through
  the TOS mirror). `account_type` is `margin`, `cash` or `ira`; a spelling that is not one
  of those is reported as `null` (the contract allows it) and counted, never guessed.
- **`hashValue` is the broker's `api_ref`** and stays in the adapter (D-13). It never reaches
  a `Record`'s contract form, an event, a ledger row, a log line or the 4a outbox; the
  record carrier already excludes it from its `repr` and its serializer.
- **Refuse, never approximate (I5).** A response that is not JSON, not a list, a row that is
  not an object, a missing or non-text `accountNumber`, a `hashValue` that is missing, an
  account key the one key rule refuses, or two rows sharing one account number is refused
  with a `kind` (`bad_shape` / `bad_key` / `cap_exceeded`). Refusal messages never carry an
  account number or a hash.
- **Poll and diff every 60 s** (`DEFAULT_INTERVAL_SECONDS`) through the phase 2 poller: one
  poll is the two REST calls the hub already made, so the steady cost is 2 calls/min of the
  shared 120/min (B7). `next_poll_due(now)` is the 60 s floor; a failed or refused poll keeps
  the last good list and emits nothing (B3).
- **ACCT_ACTIVITY is only a hint.** `on_activity(now, message)` marks "re-poll now" and reads
  nothing from the message: the account set comes from the poll, never from activity
  content. Hints are debounced to at most one re-poll per `ACTIVITY_REPOLL_SECONDS` (10 s);
  the rest are counted and dropped.
- **Behind a switch that is off by default** (`TE_SCHWAB_ACCOUNTS=on` enables it). An off
  adapter polls nothing, hints nothing and refuses `feed`/`accounts`/`poll` with `switch_off`
  (rollback = off).
- **Feeds the 4a outbox** with broker `schwab`: `on_poll` is the sink's `observe`, exactly as
  the phase 2 poller hands it over. Build the sink with `RegistrySinkConfig(broker="schwab")`.
- **Bounded.** At most `_rs.max_records()` accounts (the Rust cap, 2,000); one pending
  response pair (a newer pair supersedes an unread one, counted); the mapped state for 200
  accounts is under 2 MB (B7).

No clock is read (I7): `now` is always an argument or the injected clock.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Mapping

from trade_engine.accounts import _rs as _accounts_rs
from trade_engine.accounts.errors import AccountRuleError
from trade_engine.accounts.poll import DEFAULT_INTERVAL_SECONDS, PollAndDiff
from trade_engine.interfaces.broker import (
    AccountEvent,
    AccountRecord,
    AccountSnapshot,
    Capabilities,
    Route,
)

log = logging.getLogger(__name__)

BROKER = "schwab"
SOURCE = "schwab"
ENV_SWITCH = "TE_SCHWAB_ACCOUNTS"
#: A hint re-polls at most once per this many seconds (B7: bounded burst on the shared budget).
ACTIVITY_REPOLL_SECONDS = 10
#: The route Schwab accounts are read through (plan phase 5a: the TOS mirror).
ROUTE_PLATFORM = "tos"
READ_ONLY = "this adapter is read-only: it carries no orders"

_ENVS = ("sim", "paper", "live")
#: `securitiesAccount.type` spelling -> the contract's `account_type`. A spelling that is not
#: here becomes `null` (the contract allows it) and is counted: never guessed (I5).
_TYPES = {"MARGIN": "margin", "CASH": "cash", "IRA": "ira"}


@dataclass(frozen=True)
class SchwabAccountsConfig:
    """Whether and how the adapter runs. `enabled` is False by default and only `on` sets it."""

    enabled: bool = False
    env: str = "paper"
    interval_seconds: int = DEFAULT_INTERVAL_SECONDS
    stale_after: int = 120

    @classmethod
    def from_env(cls, environ: Mapping[str, str], **overrides: Any) -> "SchwabAccountsConfig":
        """The switch is the environment's; everything else is the caller's."""
        enabled = environ.get(ENV_SWITCH, "off").strip().lower() == "on"
        return cls(enabled=enabled, **overrides)

    def __repr__(self) -> str:
        return (
            f"SchwabAccountsConfig(enabled={self.enabled}, env={self.env!r}, "
            f"interval_seconds={self.interval_seconds})"
        )


@dataclass
class AdapterStats:
    """Counters: nothing the adapter drops or refuses is silent."""

    polls: int = 0
    polls_refused: int = 0
    feed_superseded: int = 0
    hints: int = 0
    hints_suppressed: int = 0
    activity_bodies_ignored: int = 0
    types_unmapped: int = 0
    details_unmatched: int = 0


@dataclass(frozen=True)
class Mapped:
    """One mapped read: the snapshot, plus what was not usable (never silently dropped)."""

    snapshot: AccountSnapshot
    types_unmapped: int = 0
    details_unmatched: int = 0


# --- the mapping rules (pure: text in, carriers out) --------------------------------------


def _list(text: str, what: str) -> list[Any]:
    if not isinstance(text, str):
        raise AccountRuleError("bad_shape", f"{what} is not response text")
    try:
        node = json.loads(text)
    except ValueError:
        raise AccountRuleError("bad_shape", f"{what} is not JSON") from None
    if not isinstance(node, list):
        raise AccountRuleError("bad_shape", f"{what} is not a list")
    return node


def _obj(row: Any, what: str) -> Mapping[str, Any]:
    if not isinstance(row, dict):
        raise AccountRuleError("bad_shape", f"{what} is not an object")
    return row


def _text(row: Mapping[str, Any], field: str, what: str) -> str:
    """A required text field. The message never echoes the value: it may be an id or a hash."""
    value = row.get(field)
    if not isinstance(value, str) or not value:
        raise AccountRuleError("bad_shape", f"{what}.{field} is missing or not text")
    return value


def _details(text: str) -> dict[str, str | None]:
    """`GET /trader/v1/accounts`: account number -> the broker's type spelling (fields only)."""
    out: dict[str, str | None] = {}
    for row in _list(text, "accounts"):
        account = _obj(_obj(row, "accounts[]").get("securitiesAccount"), "accounts[].securitiesAccount")
        number = _text(account, "accountNumber", "securitiesAccount")
        kind = account.get("type")
        if kind is not None and not isinstance(kind, str):
            raise AccountRuleError("bad_shape", "securitiesAccount.type is not text")
        out[number] = kind
    return out


def map_responses(
    account_numbers_json: str,
    accounts_json: str,
    now: datetime,
    *,
    env: str = "paper",
) -> Mapped:
    """Map the hub's two response texts to one snapshot. Time arrives as an argument (I7)."""
    if env not in _ENVS:
        raise AccountRuleError("bad_shape", f"env must be one of {list(_ENVS)}, got {env!r}")
    rows = _list(account_numbers_json, "accountNumbers")
    cap = _accounts_rs.max_records()
    if len(rows) > cap:
        raise AccountRuleError(
            "cap_exceeded",
            f"{len(rows)} accounts is over the {cap}-account cap; the snapshot is refused (B7)",
        )
    details = _details(accounts_json)
    seen: set[str] = set()
    records: list[AccountRecord] = []
    unmapped = 0
    for row in rows:
        row = _obj(row, "accountNumbers[]")
        number = _text(row, "accountNumber", "accountNumbers[]")
        hashed = _text(row, "hashValue", "accountNumbers[]")
        # The one key rule (Rust): an empty, whitespace-bearing or ':' id refuses `bad_key`.
        _accounts_rs.key(BROKER, number)
        if number in seen:
            raise AccountRuleError("bad_shape", "two account rows share one account number")
        seen.add(number)
        account_type: str | None = None
        kind = details.get(number)
        if kind is not None:
            account_type = _TYPES.get(kind)
            if account_type is None:
                unmapped += 1
        records.append(
            AccountRecord.of(
                BROKER,
                number,
                env=env,
                source=SOURCE,
                confirmed_at=now,
                account_type=account_type,
                api_ref=hashed,
                routes=(Route(ROUTE_PLATFORM, number),),
            )
        )
    records.sort(key=lambda record: record.key)
    snapshot = AccountSnapshot(records=tuple(records), confirmed_at=now)
    return Mapped(
        snapshot=snapshot,
        types_unmapped=unmapped,
        details_unmatched=len(set(details) - seen),
    )


# --- the adapter -------------------------------------------------------------------------


class SchwabAccountsAdapter:
    """The account port for Schwab, fed by the hub's response text. Read-only."""

    name = BROKER

    def __init__(
        self,
        config: SchwabAccountsConfig,
        *,
        clock,
        ledger=None,
        on_poll=None,
    ) -> None:
        if config.env not in _ENVS:
            raise AccountRuleError(
                "bad_shape", f"env must be one of {list(_ENVS)}, got {config.env!r}"
            )
        if config.stale_after < 0:
            raise AccountRuleError("bad_shape", "stale_after must not be negative")
        self._config = config
        self._clock = clock
        self._ledger = ledger
        self._on_poll = on_poll
        self.env = config.env
        self.stale_after = config.stale_after
        # The minimum (30 s) and the default (60 s) are Rust's rule (I5).
        self.interval_seconds = _accounts_rs.interval(config.interval_seconds)
        self.stats = AdapterStats()
        self._pending: tuple[str, str] | None = None
        self._last: AccountSnapshot | None = None
        self._last_attempt_at: datetime | None = None
        self._last_hint_at: datetime | None = None
        self._poller: PollAndDiff | None = None
        # Read-only: no order types and no TIFs, so the order path can never take one.
        self.capabilities = Capabilities(
            supported_order_types=frozenset(),
            supported_tifs=frozenset(),
            supports_multi_leg=False,
            supports_native_stops=False,
            supports_streaming=False,
        )
        if not config.enabled:
            log.info("schwab accounts adapter is off (switch %s)", ENV_SWITCH)

    def __repr__(self) -> str:
        return (
            f"SchwabAccountsAdapter(enabled={self.enabled}, env={self.env!r}, "
            f"accounts={0 if self._last is None else len(self._last.records)})"
        )

    # -- state --------------------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return self._config.enabled

    @property
    def last_good(self) -> AccountSnapshot | None:
        """The one last good snapshot, or None before the first successful poll."""
        return self._last

    @property
    def poller(self) -> PollAndDiff:
        if self._poller is None:
            self._poller = PollAndDiff(
                self,
                clock=self._clock,
                ledger=self._ledger,
                interval_seconds=self.interval_seconds,
                stale_after=self.stale_after,
                on_poll=self._on_poll,
            )
        return self._poller

    def _on(self) -> None:
        if not self.enabled:
            raise AccountRuleError(
                "switch_off",
                f"the schwab accounts adapter is off; set {ENV_SWITCH}=on to enable it",
            )

    # -- the host's side of the poll (the hub is the only Schwab client, D3) --------------

    def feed(self, account_numbers_json: str, accounts_json: str) -> None:
        """Hand the hub's two response texts to the next poll.

        One slot: a pair that was never read is superseded and counted (B7: no queue).
        """
        self._on()
        for text, what in ((account_numbers_json, "accountNumbers"), (accounts_json, "accounts")):
            if not isinstance(text, str):
                raise AccountRuleError("bad_shape", f"{what} is not response text")
        if self._pending is not None:
            self.stats.feed_superseded += 1
        self._pending = (account_numbers_json, accounts_json)

    def next_poll_due(self, now: datetime) -> bool:
        """True when the 60 s floor has elapsed since the last attempt (the host's timer)."""
        if not self.enabled:
            return False
        if self._last is None:
            return True
        last = self._last.confirmed_at
        if self._last_attempt_at is not None and self._last_attempt_at > last:
            last = self._last_attempt_at
        return now - last >= timedelta(seconds=self.interval_seconds)

    def on_activity(self, now: datetime, message: Mapping[str, Any] | None = None) -> bool:
        """An ACCT_ACTIVITY notice marks "re-poll now"; its content is never read.

        At most one re-poll per `ACTIVITY_REPOLL_SECONDS`; the rest are counted and
        suppressed. The account set comes from the poll, never from the message.
        """
        if not self.enabled:
            return False
        if message is not None:
            self.stats.activity_bodies_ignored += 1
        if self._last_hint_at is not None:
            since = now - self._last_hint_at
            if timedelta(0) <= since < timedelta(seconds=ACTIVITY_REPOLL_SECONDS):
                self.stats.hints_suppressed += 1
                return False
        self._last_hint_at = now
        self.stats.hints += 1
        return True

    # -- the account port ---------------------------------------------------------------

    def accounts(self) -> AccountSnapshot:
        """Map the fed pair to a snapshot; a refusal keeps the last good list (B3)."""
        self._on()
        if self._pending is None:
            raise AccountRuleError(
                "no_response", "no response text was fed; the hub feeds each poll (D3)"
            )
        pair, self._pending = self._pending, None
        now = self._clock.now_utc()
        self._last_attempt_at = now
        try:
            mapped = map_responses(pair[0], pair[1], now, env=self.env)
        except AccountRuleError as exc:
            self.stats.polls_refused += 1
            log.warning("schwab accounts snapshot refused (%s)", exc.kind)
            raise
        self.stats.polls += 1
        self.stats.types_unmapped += mapped.types_unmapped
        self.stats.details_unmatched += mapped.details_unmatched
        self._last = mapped.snapshot
        return mapped.snapshot

    def poll(self) -> list[AccountEvent]:
        """One poll: map the fed pair and diff it through the phase 2 port (B3)."""
        self._on()
        return self.poller.poll()

    def account_events(self, since: datetime) -> list[AccountEvent]:
        """The poll's diff, read from the bounded ring (`since` is answered by Rust)."""
        self._on()
        return self.poller.account_events(since)

    def is_stale(self, now: datetime) -> bool:
        """True before the first poll and from `stale_after` seconds after the last good one."""
        if self._last is None:
            return True
        return self._last.is_stale(now, self.stale_after)


__all__ = [
    "ACTIVITY_REPOLL_SECONDS",
    "AdapterStats",
    "BROKER",
    "ENV_SWITCH",
    "Mapped",
    "READ_ONLY",
    "SOURCE",
    "SchwabAccountsAdapter",
    "SchwabAccountsConfig",
    "map_responses",
]

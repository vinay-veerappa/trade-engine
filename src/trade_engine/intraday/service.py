"""The intraday service for 0DTE strategies (build plan I1, Architecture §4.9, P3).

A *live* loop, unlike the EOD runner's replay: the future's snapshots do not exist
yet, so the session cannot be re-derived. Instead it steps the clock tick by tick
between the session open and the close, and at each tick:

1. Pulls the underlying's chain snapshot now (injected source) and proves it is the
   live market (I5): the snapshot was taken within ``max_quote_age_seconds``, the
   *underlying's own quote time* is that recent too, and so is the quote of every leg
   the account holds. A snapshot that does not say when the underlying was quoted
   proves nothing and is refused — ``as_of`` is only when the answer arrived.
2. **Stale quote ⇒ flat-and-refuse**: working entries are cancelled, every open
   structure is closed at market, and entries are refused until fresh quotes return.
   The strategy is not asked anything on a stale tick. The flatten order rests at the
   venue and fills at the next fresh snapshot.
3. On a fresh tick, only the quotes that are themselves fresh are handed on: the venue
   matches working orders against them, the E4 rule reconciles at once, and the
   strategy sees the same ``OptionContext`` the EOD run does — so no candidate leg is
   ever priced from an old quote.
4. The session's clock rules are the service's, early closes included: entries end at
   ``min(entry_end, close − entry_before_close)`` and the sweep flattens at
   ``min(flat_at, close − flat_before_close)``. Past the entry end, working entries are
   cancelled and any entry the strategy returns is dropped.
5. Every fresh tick records a position check against the venue (``VenueReconcile``,
   I11), which is also the session's snapshot count; drift stops the service.
6. Writes a **heartbeat** file (atomically) from start-up to exit: the supervisor
   watches it, and a fresh one from this session refuses a second instance (C4); the
   ledger's OS lock enforces it too (I4).

Idempotency (I3): every derived command is claimed by id, so a restart mid-session
folds the ledger, restores the venue book from the fold (``oms.restore``), resolves
what the crash left half-done, and continues — a session replayed to the end after a
restart yields the same ledger as an uninterrupted run. The after-close work (expiry,
assignment, official-close marks) stays with the EOD job (O2, I9); its marker for the
previous session gates this one, and its marker for this session ends it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from trade_engine.calendar.sessions import ExchangeCalendar
from trade_engine.domain.instruments import OptionContract
from trade_engine.domain.option_orders import CloseStructure, OptionIntent
from trade_engine.eod.options_routing import OptionRouter, RoutingTally
from trade_engine.interfaces.clock import Clock
from trade_engine.interfaces.market_data import StaleDataError
from trade_engine.ledger import Event, EventKind, Ledger, VenueReconcile
from trade_engine.market_data.chains import ChainSnapshot
from trade_engine.sim import underlying_of
from trade_engine.sim._rs import register, rs as native
from trade_engine.eod._runtime import decide, flag, micros

NEW_YORK = ZoneInfo("America/New_York")
class IntradayServiceError(RuntimeError):
    """A refused intraday run (I5)."""


class IntradayServiceAlert(IntradayServiceError):
    """The service stopped mid-session on an unexpected error, after trying to be flat.

    Unlike a refusal at start-up, a restart can help: the ledger holds whatever flatten
    was asked, and the restarted service fills it at its first fresh snapshot.
    """


@dataclass(frozen=True)
class Heartbeat:
    """Liveness proof written from start-up to exit; the ledger stays the only state (I2).

    ``exited`` marks the last beat of a process that stopped on its own (the close, or an
    alert): it proves nobody is running, so it never refuses a restart. ``alert`` is set
    when the service stopped on an unexpected error.
    """

    account_id: str
    session: date
    at_utc: datetime
    refusing: bool
    note: str = ""
    alert: bool = False
    exited: bool = False


register("runtime_intraday", IntradayServiceError)
register("runtime_stale", StaleDataError)


@dataclass(frozen=True)
class IntradayConfig:
    """Wiring for one service instance, supplied entirely by the host (I5).

    ``strategy`` carries the host's plugin: its ``manage_options`` runs at every fresh
    snapshot, exactly as the EOD runner calls it. ``eod_job_name`` names the after-close
    job whose marker (``eod:<job>:<account>:<session>``) must exist for the previous
    session before this one starts. ``flat_at`` and ``entry_end`` are the rules' clock
    times; on an early close the service pulls both in (see the module doc).
    """

    job_name: str
    account_id: str
    underlying: str
    broker: Any
    strategy: Any
    option_risk_engine: Any | None
    # (underlying, now) -> the snapshot as it stood at ``now``, fetched and kept.
    snapshot_source: Callable[[str, datetime], ChainSnapshot]
    eod_job_name: str
    max_quote_age_seconds: float = 30.0
    tick_seconds: float = 5.0
    flat_at: time = time(15, 30)  # ET; "flat 15:30" (rules doc §6.4)
    entry_end: time = time(12, 0)  # ET; "9:45–12:00 entry" (rules doc §6.4)
    # Early closes: flat 30 minutes before any close, as on a full day; entries end 90
    # minutes before it, so the last entry still has an hour to work before the sweep.
    flat_before_close: timedelta = timedelta(minutes=30)
    entry_before_close: timedelta = timedelta(minutes=90)
    # The close mark needs a fresh snapshot taken this near the close; otherwise the
    # after-close pass marks (O2).
    close_mark_window_seconds: float = 120.0
    heartbeat_ttl_seconds: float = 60.0
    journal_account: str | None = None

    def __post_init__(self) -> None:
        decide("intraday:config_identity",
            flags=tuple(bool(v) for v in (self.job_name, self.account_id, self.underlying, self.eod_job_name)))
        decide("intraday:config_rates",
            floats=(self.max_quote_age_seconds, self.tick_seconds, self.heartbeat_ttl_seconds, self.close_mark_window_seconds))
        midnight = date(2000, 1, 1)
        decide("intraday:config_wall",
            numbers=(micros(datetime.combine(midnight, self.entry_end, tzinfo=NEW_YORK)),
                micros(datetime.combine(midnight, self.flat_at, tzinfo=NEW_YORK))))
        decide("intraday:config_early",numbers=(micros(self.flat_before_close),micros(self.entry_before_close)))


class _EntryGate:
    """The strategy as the router sees it: entries pass only while the service allows.

    Exits always pass (a stop must work at any hour); an ``OptionIntent`` returned while
    entries are closed is dropped and counted, so no entry is made after the entry end,
    after the sweep, or while the service is refusing.
    """

    def __init__(self, strategy: Any) -> None:
        self._strategy = strategy
        self.name = getattr(strategy, "name", type(strategy).__name__)
        self.allow = False
        self.dropped = 0

    def manage_options(self, context: Any) -> list:
        manage = getattr(self._strategy, "manage_options", None)
        if not callable(manage):
            return []
        actions = list(manage(context))
        keep = decide("intraday:gate", flags=(self.allow, *(isinstance(action, OptionIntent) for action in actions)))[2]
        kept = [action for action, selected in zip(actions, keep) if selected]
        self.dropped = decide("routing:increment", numbers=(self.dropped, len(actions) - len(kept)))[1][0]
        return kept


class IntradayService:
    """Run one 0DTE account live from the session open to the close."""

    def __init__(
        self,
        ledger: Ledger,
        clock: Clock,
        calendar: ExchangeCalendar,
        config: IntradayConfig,
        *,
        heartbeat_path: Any | None = None,
    ) -> None:
        self._ledger = ledger
        self._clock = clock
        self._calendar = calendar
        self._config = config
        self._heartbeat_path = None if heartbeat_path is None else Path(heartbeat_path)
        self._gate = _EntryGate(config.strategy)
        self._router = OptionRouter(
            ledger,
            clock,
            brokers={config.account_id: config.broker},
            strategies={config.account_id: self._gate},
            option_risk_engines=(
                {config.account_id: config.option_risk_engine} if config.option_risk_engine else {}
            ),
            journal_accounts=(
                {config.account_id: config.journal_account} if config.journal_account else {}
            ),
        )

    # -- entry -------------------------------------------------------------------

    def run(
        self,
        session: date,
        *,
        start_at: datetime | None = None,
        stop_at: datetime | None = None,
    ) -> dict[str, Any]:
        """Walk one session. ``stop_at`` simulates a crash: the loop stops there
        without a marker, exactly as a killed process would leave the ledger (I2)."""
        return native.intraday_run(self, session, start_at, stop_at)

    # -- the tick ----------------------------------------------------------------

    def _tick(self, session: date, state: "_TickState") -> None:
        native.intraday_tick(self, session, state)

    def _fresh_view(self, snapshot: ChainSnapshot, now: datetime) -> ChainSnapshot:
        """``snapshot`` cut to its fresh quotes, if it proves a live market; else refuse."""
        limit = self._config.max_quote_age_seconds
        decide("intraday:snapshot_identity", (type(snapshot).__name__, self._config.underlying,
            snapshot.underlying if isinstance(snapshot, ChainSnapshot) else ""),
            flags=(isinstance(snapshot, ChainSnapshot),))
        snapshot.require_fresh(now, limit)
        quoted = snapshot.underlying_as_of
        decide("intraday:underlying_stamp", (snapshot.underlying, snapshot.as_of.isoformat()), flags=(quoted is not None,))
        age = (now - quoted).total_seconds()
        decide("intraday:underlying_age", (snapshot.underlying, f"{age:.0f}", quoted.isoformat(), f"{limit:.0f}"),
            floats=(age, limit))
        for contract in self._held_contracts():
            quote = snapshot.get(contract)
            leg_age = (now - quote.as_of).total_seconds() if quote is not None else 0
            decide("intraday:leg", (contract.occ.strip(), snapshot.underlying, f"{leg_age:.0f}", f"{limit:.0f}"),
                flags=(quote is not None,), floats=(leg_age, limit))
        selected = decide("intraday:fresh", floats=(limit, *((now - q.as_of).total_seconds() for q in snapshot.quotes)))[2]
        fresh = tuple(q for q, keep in zip(snapshot.quotes, selected) if keep)
        return snapshot if len(fresh) == len(snapshot.quotes) else replace(snapshot, quotes=fresh)

    def _held_contracts(self) -> list[OptionContract]:
        folded = self._ledger.state(self._config.account_id)
        return sorted(
            (
                instrument
                for instrument, position in folded.positions.items()
                if isinstance(instrument, OptionContract) and flag("intraday:held",
                    (underlying_of(instrument), self._config.underlying),
                    flags=(True, flag("eod:nonzero", (str(position.quantity),))))
            ),
            key=lambda contract: contract.occ,
        )

    def _go_flat_and_refuse(self, session: date, state: "_TickState", why: str) -> None:
        """Stale quote: cancel working entries, ask to be flat, refuse entries (I5).

        The command ids stay free of the stale message — it varies between the first
        process and a resumed one — while the human reason carries the detail (I3).
        """
        native.intraday_refuse(self, session, state, why)

    def _flatten(self, session: date, code: str, reason: str) -> int:
        """Close at market whatever is open and not already closing; returns how many."""
        return native.intraday_flatten(self, session, code, reason)

    def _cancel_working_entries(self, session: date, code: str) -> None:
        """Cancel every entry still working: past the entry end, at the sweep, when stale."""
        native.intraday_cancel(self, session, code)

    def _record_tick(self, session: date, snapshot: ChainSnapshot) -> None:
        """One position check per fresh snapshot: the session's record of it (I2, I11)."""
        command = f"{self._cause(session)}:tick:{snapshot.as_of.isoformat()}"
        if self._ledger.has_command(command):
            return  # the same snapshot twice (a restart re-reading it) is one tick
        folded = self._ledger.state(self._config.account_id)
        ledger_side = {i: p.quantity for i, p in folded.positions.items() if p.quantity != 0}
        venue_side = {p.instrument: p.quantity for p in self._config.broker.positions() if p.quantity != 0}
        drift = tuple(decide("intraday:drift", tuple(value
            for instrument in set(ledger_side) | set(venue_side)
            for value in (instrument.symbol, str(ledger_side.get(instrument)), str(venue_side.get(instrument)))))[0])
        now = self._clock.now_utc()
        self._ledger.append(
            Event(
                account=self._config.account_id,
                kind=EventKind.VENUE_RECONCILE,
                payload=VenueReconcile(
                    venue=getattr(self._config.broker, "name", type(self._config.broker).__name__),
                    as_of=now,
                    reconciled=not drift,
                    drift=drift,
                    note=f"{snapshot.underlying} snapshot {snapshot.as_of.isoformat()}",
                ),
                ts_utc=now,
                command_id=command,
            )
        )
        decide("intraday:drift_refuse", (self._config.account_id, ", ".join(drift)))

    # -- clock rules ---------------------------------------------------------------

    def _deadlines(self, session: date) -> tuple[datetime, datetime]:
        """(the last instant an entry may be made, the sweep instant), early closes included."""
        close = self._calendar.session_close(session)
        flat = datetime.combine(session, self._config.flat_at, tzinfo=NEW_YORK)
        entry_end = datetime.combine(session, self._config.entry_end, tzinfo=NEW_YORK)
        times = decide("intraday:deadlines", numbers=(micros(flat), micros(entry_end), micros(close),
            micros(self._config.flat_before_close), micros(self._config.entry_before_close)))[1]
        chosen_flat = (flat, close - self._config.flat_before_close)[times[1]]
        return (entry_end, close - self._config.entry_before_close, chosen_flat)[times[0]], chosen_flat

    # -- restart -------------------------------------------------------------------

    def _rehydrate(self, session: date, state: "_TickState") -> None:
        """A new process starts with an empty venue; restore it from the fold (I2).

        Then finish what a crash left half-done: a pending venue request is carried out
        on the rebuilt venue and read back (``oms.restore``); an entry created but never
        sent is cancelled (it was decided on quotes that are gone); a close created but
        never sent is sent (the decision to be flat stands).
        """
        native.intraday_restore(self, session, state)

    # -- unexpected errors -----------------------------------------------------------

    def _emergency(self, session: date, state: "_TickState", err: Exception) -> None:
        """An unexpected error: try to be flat on a fresh quote, alert loudly, exit (I5).

        The flatten is only asked on a quote that proves the market is live; it rests in
        the ledger, so the restarted service (restart-on-failure) fills it at its first
        fresh snapshot. With no fresh quote nothing is sent and the alert says so.
        """
        native.intraday_emergency(self, session, state, err)

    # -- the close -----------------------------------------------------------------

    def _close_mark(self, session: date, state: "_TickState") -> str:
        return native.intraday_close(self, session, state)

    def _close_plan(
        self, session: date, state: "_TickState"
    ) -> tuple[datetime, dict[Any, tuple[Any, str]], str]:
        """Mark at a fresh snapshot taken near the close, then the run marker.

        Without such a snapshot (the service was refusing at the close, or restarted
        after it) nothing is marked here: the after-close pass marks at its own
        snapshot and the official close (O2). A guessed mark is not a mark (I5).
        """
        from trade_engine.domain.instruments import Equity

        now = self._clock.now_utc()
        close = self._calendar.session_close(session)
        folded = self._ledger.state(self._config.account_id)
        held = sorted(
            (i for i, p in folded.positions.items() if p.quantity != 0), key=lambda i: i.symbol
        )
        snapshot = state.snapshots.get(self._config.underlying)
        marks: dict[Any, tuple[Any, str]] = {}
        note = "nothing held at the close"
        if held:
            usable = flag("intraday:close_usable", flags=(snapshot is not None,),
                floats=((close - snapshot.as_of).total_seconds() if snapshot is not None else 0,
                    self._config.close_mark_window_seconds))
            quotes = {i: snapshot.get(i) for i in held if isinstance(i, OptionContract)} if usable else {}
            if not flag("intraday:close_plan", flags=(usable,
                all(q is not None and flag("eod:positive", (str(q.mid),)) for q in quotes.values())),
                numbers=(len(quotes), len(held))):
                note = "no fresh snapshot near the close; marks left to the after-close pass"
            else:
                source = f"snapshot:{snapshot.as_of.isoformat()}"
                marks = {i: (q.mid, source) for i, q in quotes.items()}
                marks[Equity(self._config.underlying)] = (snapshot.underlying_price, source)
                note = f"marked at {source}"
        return now, marks, note

    def _session_snapshot_count(self, session: date) -> int:
        """Fresh snapshots processed this session, across every process (I2, I3)."""
        prefix = f"{self._cause(session)}:tick:"
        return decide("eod:count_events", (EventKind.VENUE_RECONCILE.value, prefix, *(value
            for event in self._ledger.events(account=self._config.account_id)
            for value in (event.kind.value, event.command_id or ""))))[1][0]

    # -- restart gate, heartbeat, plumbing ---------------------------------------

    def _cause(self, session: date) -> str:
        return decide("eod:command", ("intraday", self._config.job_name, self._config.account_id, session.isoformat()))[0][0]

    def _own_marker(self, session: date) -> str:
        return self._cause(session)

    def _eod_marker(self, session: date) -> str:
        return decide("eod:command", ("eod", self._config.eod_job_name, self._config.account_id, session.isoformat()))[0][0]

    def _result(self, session: date, state: "_TickState", **extra: Any) -> dict[str, Any]:
        result = {
            "session": session,
            "account_id": self._config.account_id,
            "orders_submitted": state.tally.orders_submitted,
            "exit_actions": state.tally.exit_actions,
            "snapshots_processed": state.tally.snapshots_processed,
            "entries_dropped": self._gate.dropped,
        }
        if "stopped_at" in extra:
            extra["stopped_at"] = extra["stopped_at"].isoformat()
        result.update(extra)
        return result

    def _refuse_other_live_instance(self, session: date) -> None:
        native.intraday_live_guard(self, session)

    def _require_previous_eod_complete(self, session: date) -> None:
        native.intraday_previous_guard(self, session)

    def _has_history(self, account_id: str) -> bool:
        for event in self._ledger.events(account=account_id):
            if flag("eod:any_history", (event.kind.value,)):
                return True
        return False

    def _write_heartbeat(
        self,
        session: date,
        state: "_TickState",
        note: str,
        *,
        alert: bool = False,
        exited: bool = False,
    ) -> None:
        """Written to a temporary file and moved into place: a reader never sees half."""
        native.intraday_heartbeat_write(self, session, state, note, alert, exited)

    def _read_heartbeat(self) -> Heartbeat | None:
        return native.intraday_heartbeat_read(self)

    def _advance(self, target: datetime) -> None:
        native.intraday_advance(self, target)

    def _wait_until(self, target: datetime, session: date, state: "_TickState") -> None:
        """Nothing ticks before the open: a replay clock jumps, a wall clock waits."""
        native.intraday_wait(self, target, session, state)

    def _pause(self) -> None:
        self._clock.sleep(self._config.tick_seconds)


@dataclass
class _TickState:
    refusing: bool = False
    barred: str | None = None  # a reason entries are refused for the whole session
    tally: RoutingTally = field(default_factory=RoutingTally)
    snapshots: dict[str, ChainSnapshot] = field(default_factory=dict)


def flat_close(
    structure: Any, session: date, code: str, attempt: int = 1, reason: str | None = None
) -> CloseStructure:
    """The market close that undoes whatever is still open of ``structure``."""
    values = decide("intraday:flat_close", (reason or "", code, structure.entry_order_id, session.isoformat()),
        numbers=(attempt,))[0]
    return CloseStructure(
        entry_order_id=structure.entry_order_id,
        reason=values[0],
        command_id=values[1],
    )

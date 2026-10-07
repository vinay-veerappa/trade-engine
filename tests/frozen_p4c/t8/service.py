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

import json
import os
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from trade_engine.calendar.sessions import ExchangeCalendar
from trade_engine.domain.instruments import OptionContract
from trade_engine.domain.option_orders import CloseStructure, OptionIntent, is_structure
from trade_engine.domain.orders import OrderState
from trade_engine.eod.options_routing import OptionRouter, RoutingTally
from trade_engine.interfaces.clock import Clock
from trade_engine.interfaces.market_data import StaleDataError
from trade_engine.ledger import EodRun, Event, EventKind, Ledger, VenueReconcile
from trade_engine.market_data.chains import ChainSnapshot
from trade_engine.oms.restore import MIN_TIME, PendingResolution, RestoreError
from trade_engine.sim import underlying_of
from trade_engine.sim._rs import register
from trade_engine.eod._runtime import decide, flag, micros

NEW_YORK = ZoneInfo("America/New_York")
_ENTRY_WORKING = frozenset(
    {OrderState.NEW, OrderState.SUBMITTED, OrderState.ACCEPTED, OrderState.PARTIALLY_FILLED}
)


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
        decide("intraday:session", (session.isoformat(), self._calendar.exchange), flags=(self._calendar.is_session(session),))
        state = _TickState()
        if self._ledger.event_by_command(self._eod_marker(session)) is not None:
            # The after-close pass has settled this session: nothing here may add to it (I9).
            return self._result(session, state, settled=True)
        self._refuse_other_live_instance(session)
        self._require_previous_eod_complete(session)
        self._write_heartbeat(session, state, "starting")
        self._config.broker.connect()
        self._rehydrate(session, state)
        open_et = self._calendar.session_open(session)
        close_et = self._calendar.session_close(session)
        if start_at is not None:
            self._advance(start_at)
        self._wait_until(open_et, session, state)
        try:
            while self._clock.now_utc() < close_et:
                if stop_at is not None and self._clock.now_utc() >= stop_at:
                    return self._result(session, state, stopped_at=self._clock.now_utc())
                self._tick(session, state)
                self._pause()
            note = self._close_mark(session, state)
        except Exception as err:  # noqa: BLE001 - every unexpected error ends in an alert
            self._emergency(session, state, err)
        self._write_heartbeat(session, state, f"closed; {note}", exited=True)
        return self._result(session, state)

    # -- the tick ----------------------------------------------------------------

    def _tick(self, session: date, state: "_TickState") -> None:
        now = self._clock.now_utc()
        try:
            snapshot = self._config.snapshot_source(self._config.underlying, now)
            # A live pull takes time and the chain is stamped when it returns, after the
            # clock read above. Judge it, and time the rest of the tick, on the clock as it
            # stands now; a chain stamped past THIS reading is still a clock fault (I5).
            now = self._clock.now_utc()
            view = self._fresh_view(snapshot, now)
        except (StaleDataError, ValueError) as err:
            self._go_flat_and_refuse(session, state, str(err))
            self._write_heartbeat(session, state, f"stale quote: {err}")
            return
        # A fresh quote returned: the flatten order fills at these quotes and entries
        # are allowed again (unless the whole session is barred).
        state.refusing = False
        entry_end, flat = self._deadlines(session)
        flat_due, entries_open = decide("intraday:tick", numbers=(micros(now), micros(entry_end), micros(flat)),
            flags=(state.barred is not None,))[2]
        if not entries_open:
            self._cancel_working_entries(session, "flat-sweep" if flat_due else "entry-end")
        if flat_due:
            closed = self._flatten(
                session, "flat-sweep", f"flat by {flat.astimezone(NEW_YORK):%H:%M} ET"
            )
            state.tally.exit_actions = decide("routing:increment", numbers=(state.tally.exit_actions, closed))[1][0]
        self._gate.allow = entries_open
        # A refusal here (a reconcile that does not add up, an OMS guard) is unexpected
        # mid-session: it ends in the emergency path, which alerts and exits (I5).
        state.tally += self._router.manage_at_snapshot(
            self._config.account_id, session, view, state.snapshots, self._cause(session)
        )
        self._record_tick(session, view)
        self._write_heartbeat(session, state, "ok" if entries_open else "ok; entries closed")

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
        state.refusing = True
        self._cancel_working_entries(session, "stale-quote")
        closed = self._flatten(session, "stale-quote", f"stale quote: {why}")
        state.tally.exit_actions = decide("routing:increment", numbers=(state.tally.exit_actions, closed))[1][0]

    def _flatten(self, session: date, code: str, reason: str) -> int:
        """Close at market whatever is open and not already closing; returns how many."""
        manager = self._router.manager(self._config.account_id)
        folded = self._ledger.state(self._config.account_id)
        from trade_engine.oms.options import open_structures

        closed = 0
        for structure in open_structures(folded):
            if not flag("intraday:flatten", (underlying_of(structure.instrument), self._config.underlying),
                flags=(structure.closing_order_id is not None,)):
                continue
            # A close the venue rejected leaves the structure open: the next attempt
            # needs its own command id, derived from the fold so a restart agrees (I3).
            attempt = decide("intraday:attempt", (structure.entry_order_id, *folded.orders))[1][0]
            manager.close(self._config.account_id, flat_close(structure, session, code, attempt, reason))
            closed = decide("routing:increment", numbers=(closed, 1))[1][0]
        return closed

    def _cancel_working_entries(self, session: date, code: str) -> None:
        """Cancel every entry still working: past the entry end, at the sweep, when stale."""
        manager = self._router.manager(self._config.account_id)
        folded = self._ledger.state(self._config.account_id)
        for order in sorted(folded.orders.values(), key=lambda value: value.order_id):
            if not flag("intraday:cancel", (order.state.value,
                underlying_of(order.instrument) if is_structure(order.instrument) else "", self._config.underlying),
                flags=(order.parent_order_id is None, is_structure(order.instrument))):
                continue
            manager.orders.cancel(
                order.order_id,
                command_id=f"intraday:cancel-entry:{order.order_id}:{code}:{session.isoformat()}",
            )

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
        from trade_engine.oms.restore import restorable, restorable_positions

        broker = self._config.broker
        if broker.orders(MIN_TIME) or broker.fills(MIN_TIME):
            return  # this process's own venue: nothing was lost
        account = self._config.account_id
        folded = self._ledger.state(account)
        resolution = PendingResolution()
        try:
            orders, fills = restorable(
                self._ledger, account, folded, pending="resolve", resolution=resolution
            )
        except RestoreError as err:
            raise IntradayServiceError(str(err)) from err
        positions = restorable_positions(folded)
        if orders or fills or positions:
            broker.restore(orders, fills, positions)
        manager = self._router.manager(account)
        for order_id in resolution.resolved:
            manager.orders.reconcile_order(order_id)
        if resolution.unresolved:
            state.barred = "; ".join(resolution.unresolved.values())
        for order in sorted(self._ledger.state(account).orders.values(), key=lambda o: o.order_id):
            action = decide("intraday:restart", (order.state.value, order.order_id, str(order.parent_order_id)),
                flags=(order.parent_order_id is None, is_structure(order.instrument)))[0][0]
            if action == "cancel":
                manager.orders.cancel(
                    order.order_id, command_id=f"intraday:orphan:{order.order_id}:{session.isoformat()}"
                )
            elif action == "submit":
                sent = manager.orders.submit(order)
                if sent.state not in (OrderState.FILLED, OrderState.CANCELLED, OrderState.REJECTED, OrderState.EXPIRED):
                    manager.orders.reconcile_order(order.order_id)

    # -- unexpected errors -----------------------------------------------------------

    def _emergency(self, session: date, state: "_TickState", err: Exception) -> None:
        """An unexpected error: try to be flat on a fresh quote, alert loudly, exit (I5).

        The flatten is only asked on a quote that proves the market is live; it rests in
        the ledger, so the restarted service (restart-on-failure) fills it at its first
        fresh snapshot. With no fresh quote nothing is sent and the alert says so.
        """
        note = f"ALERT {type(err).__name__}: {err}"
        state.refusing = True
        try:
            now = self._clock.now_utc()
            self._fresh_view(self._config.snapshot_source(self._config.underlying, now), now)
        except Exception as stale:  # noqa: BLE001
            note += f"; no fresh quote to flatten on ({stale}); positions left open, entries refused"
        else:
            try:
                self._cancel_working_entries(session, "emergency")
                asked = self._flatten(session, "emergency", f"unexpected error: {type(err).__name__}")
                note += f"; flatten asked for {asked} structure(s) at a fresh quote, entries refused"
            except Exception as second:  # noqa: BLE001
                note += f"; flatten failed: {type(second).__name__}: {second}"
        self._write_heartbeat(session, state, note, alert=True, exited=True)
        raise IntradayServiceAlert(
            f"The intraday service for '{self._config.account_id}' stopped on "
            f"{session.isoformat()}: {note}"
        ) from err

    # -- the close -----------------------------------------------------------------

    def _close_mark(self, session: date, state: "_TickState") -> str:
        """Mark at a fresh snapshot taken near the close, then the run marker.

        Without such a snapshot (the service was refusing at the close, or restarted
        after it) nothing is marked here: the after-close pass marks at its own
        snapshot and the official close (O2). A guessed mark is not a mark (I5).
        """
        from trade_engine.domain.instruments import Equity
        from trade_engine.ledger import Mark

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
        for instrument, (price, source) in sorted(marks.items(), key=lambda item: item[0].symbol):
            command = (
                f"intraday:mark:{self._config.account_id}:{session.isoformat()}:{instrument.symbol}"
            )
            if self._ledger.event_by_command(command) is not None:
                continue
            self._ledger.append(
                Event(
                    account=self._config.account_id,
                    kind=EventKind.MARK,
                    payload=Mark(instrument=instrument, price=price, as_of=now, source=source),
                    ts_utc=now,
                    command_id=command,
                )
            )
        self._ledger.append(
            Event(
                account=self._config.account_id,
                kind=EventKind.EOD_RUN,
                payload=EodRun(
                    session=session,
                    job=f"intraday:{self._config.job_name}",
                    account_id=self._config.account_id,
                    # The session's snapshots, not this process's: a restarted run must
                    # append the same marker as an uninterrupted one (I3).
                    bars_processed=self._session_snapshot_count(session),
                    at_close=now,
                ),
                ts_utc=now,
                command_id=self._own_marker(session),
            )
        )
        return note

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
        heartbeat = self._read_heartbeat()
        if heartbeat is None:
            return
        decide("intraday:heartbeat_account", (heartbeat.account_id, self._config.account_id, str(self._heartbeat_path)))
        if heartbeat.exited:
            return  # the last process said it stopped; nobody is running
        decide("intraday:heartbeat", (self._config.account_id, heartbeat.at_utc.isoformat()),
            flags=(heartbeat.session == session,),
            floats=((self._clock.now_utc() - heartbeat.at_utc).total_seconds(), self._config.heartbeat_ttl_seconds))

    def _require_previous_eod_complete(self, session: date) -> None:
        account = self._config.account_id
        if self._ledger.event_by_command(self._own_marker(session)) is not None:
            return  # this session already completed; the re-run proves idempotency
        previous = self._calendar.previous_session(session)
        if self._ledger.event_by_command(self._eod_marker(previous)) is not None:
            return
        decide("intraday:previous", (session.isoformat(), account, previous.isoformat(), self._config.eod_job_name,
            self._eod_marker(previous)), flags=(self._has_history(account),))

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
        if self._heartbeat_path is None:
            return
        heartbeat = Heartbeat(
            account_id=self._config.account_id,
            session=session,
            at_utc=self._clock.now_utc(),
            refusing=state.refusing or state.barred is not None,
            note=note if state.barred is None else f"{note}; entries barred: {state.barred}",
            alert=alert,
            exited=exited,
        )
        body = asdict(heartbeat)
        body.update(session=heartbeat.session.isoformat(), at_utc=heartbeat.at_utc.isoformat())
        temporary = self._heartbeat_path.with_name(self._heartbeat_path.name + ".tmp")
        temporary.write_text(json.dumps(body), encoding="utf-8")
        os.replace(temporary, self._heartbeat_path)

    def _read_heartbeat(self) -> Heartbeat | None:
        path = self._heartbeat_path
        if path is None or not path.exists():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            return Heartbeat(
                account_id=raw["account_id"],
                session=date.fromisoformat(raw["session"]),
                at_utc=datetime.fromisoformat(raw["at_utc"]),
                refusing=bool(raw["refusing"]),
                note=str(raw.get("note", "")),
                alert=bool(raw.get("alert", False)),
                exited=bool(raw.get("exited", False)),
            )
        except (ValueError, KeyError, TypeError) as err:
            raise IntradayServiceError(
                f"The heartbeat file {path} cannot be read ({type(err).__name__}: {err}); it "
                f"cannot prove no other instance is running, so the service refuses to start "
                f"(C4). Remove it once you have checked that no service is running."
            ) from err

    def _advance(self, target: datetime) -> None:
        advance = getattr(self._clock, "advance_to", None)
        if callable(advance) and self._clock.now_utc() < target:
            advance(target)

    def _wait_until(self, target: datetime, session: date, state: "_TickState") -> None:
        """Nothing ticks before the open: a replay clock jumps, a wall clock waits."""
        if callable(getattr(self._clock, "advance_to", None)):
            self._advance(target)
            return
        while self._clock.now_utc() < target:
            self._write_heartbeat(session, state, f"waiting for the open at {target.isoformat()}")
            remaining = (target - self._clock.now_utc()).total_seconds()
            self._clock.sleep(max(min(self._config.tick_seconds, remaining), 0.001))

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

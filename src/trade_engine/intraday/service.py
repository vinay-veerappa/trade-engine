"""The intraday service for 0DTE strategies (build plan I1, Architecture §4.9, P3).

A *live* loop, unlike the EOD runner's replay: the future's snapshots do not exist
yet, so the session cannot be re-derived. Instead it steps the clock tick by tick
between the session open and the close, and at each tick:

1. Pulls the underlying's chain snapshot now (injected source, stamped when the last
   chunk arrived) and refuses one older than ``max_quote_age_seconds`` (I5).
2. **Stale quote ⇒ flat-and-refuse**: every open structure is closed at market and new
   entries are refused until fresh quotes return. A stale quote prices nothing (I5);
   the flatten order itself rests at the venue and fills at the next fresh snapshot.
3. Matches working orders at the snapshot's own instant, reconciles immediately (the
   E4 rule), and hands the strategy the same ``OptionContext`` the EOD run does.
4. Writes a **heartbeat** file every tick: the supervisor watches it, and at startup a
   fresh heartbeat from this session refuses a second instance (C4); the ledger's OS
   lock enforces it too (I4).

Idempotency (I3): every derived command is claimed by id, so a restart mid-session
folds the ledger, restores the venue book from the fold (``oms.restore``) and
continues — a session replayed to the end after a restart yields the same ledger as
an uninterrupted run. The after-close work (expiry, assignment, official-close
marks) stays with the EOD runner (O2); the service closes its mark at its own last
snapshot mid and the underlying at the fold's mark.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
import json
from zoneinfo import ZoneInfo

from trade_engine.calendar.sessions import ExchangeCalendar
from trade_engine.eod.options_routing import OptionRouter, RoutingTally
from trade_engine.eod.runner import MIN_TIME
from trade_engine.interfaces.broker import BrokerAdapter
from trade_engine.interfaces.clock import Clock
from trade_engine.interfaces.market_data import StaleDataError
from trade_engine.ledger import EodRun, Event, EventKind, Ledger
from trade_engine.market_data.chains import ChainSnapshot

NEW_YORK = ZoneInfo("America/New_York")


class IntradayServiceError(RuntimeError):
    """A refused intraday run (I5)."""


@dataclass(frozen=True)
class Heartbeat:
    """Liveness proof written every tick; the ledger stays the only state (I2)."""

    account_id: str
    session: date
    at_utc: datetime
    refusing: bool
    note: str = ""


@dataclass(frozen=True)
class IntradayConfig:
    """Wiring for one service instance, supplied entirely by the host (I5).

    ``strategy`` carries the host's plugin: its ``manage_options`` runs at every
    snapshot, exactly as the EOD runner calls it. The service itself decides nothing.
    """

    job_name: str
    account_id: str
    underlying: str
    broker: Any
    strategy: Any
    option_risk_engine: Any | None
    # (underlying, now) -> the snapshot as it stood at ``now``, fetched and kept.
    snapshot_source: Callable[[str, datetime], ChainSnapshot]
    max_quote_age_seconds: float = 30.0
    tick_seconds: float = 5.0
    flat_at: time = time(15, 30)  # ET; "flat 15:30" (rules doc §6.4)
    stop_at: time = time(16, 0)
    heartbeat_ttl_seconds: float = 60.0
    journal_account: str | None = None

    def __post_init__(self) -> None:
        if not self.job_name:
            raise IntradayServiceError("job_name must be non-empty")
        if not self.account_id:
            raise IntradayServiceError("account_id must be non-empty")
        if not self.underlying:
            raise IntradayServiceError("underlying must be non-empty")
        if self.max_quote_age_seconds <= 0 or self.tick_seconds <= 0:
            raise IntradayServiceError("quote age and tick must be positive")
        if self.heartbeat_ttl_seconds <= 0:
            raise IntradayServiceError("heartbeat ttl must be positive")
        if self.flat_at >= self.stop_at:
            raise IntradayServiceError("flat_at must come before stop_at")


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
        self._heartbeat_path = heartbeat_path
        self._router = OptionRouter(
            ledger,
            clock,
            brokers={config.account_id: config.broker},
            strategies={config.account_id: config.strategy},
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
        if not self._calendar.is_session(session):
            raise IntradayServiceError(
                f"{session.isoformat()} is not a trading session of "
                f"{self._calendar.exchange} (I5)"
            )
        self._refuse_other_live_instance(session)
        self._require_previous_eod_complete(session)
        self._config.broker.connect()
        self._rehydrate()
        state = _TickState()
        open_et = self._calendar.session_open(session)
        close_et = self._calendar.session_close(session)
        if start_at is not None and self._clock.now_utc() < start_at:
            self._advance(start_at)
        if self._clock.now_utc() < open_et:
            self._advance(open_et)
        while self._clock.now_utc() < close_et:
            if stop_at is not None and self._clock.now_utc() >= stop_at:
                return {
                    "session": session,
                    "account_id": self._config.account_id,
                    "stopped_at": self._clock.now_utc().isoformat(),
                    "orders_submitted": state.tally.orders_submitted,
                    "exit_actions": state.tally.exit_actions,
                    "snapshots_processed": state.tally.snapshots_processed,
                }
            self._tick(session, state)
            self._pause()
        self._close_mark(session, state)
        return {
            "session": session,
            "account_id": self._config.account_id,
            "orders_submitted": state.tally.orders_submitted,
            "exit_actions": state.tally.exit_actions,
            "snapshots_processed": state.tally.snapshots_processed,
        }

    # -- the tick ----------------------------------------------------------------

    def _tick(self, session: date, state: "_TickState") -> None:
        now = self._clock.now_utc()
        try:
            snapshot = self._config.snapshot_source(self._config.underlying, now)
            snapshot.require_fresh(now, self._config.max_quote_age_seconds)
        except (StaleDataError, ValueError) as err:
            self._go_flat_and_refuse(session, state, str(err))
            self._write_heartbeat(session, state, f"stale quote: {err}")
            return
        if state.refusing:
            # A fresh quote returned: the flatten order fills at these quotes and
            # entries are allowed again.
            state.refusing = False
            state.flat_asked = False
        self._write_heartbeat(session, state, "ok")
        cause = f"intraday:{self._config.job_name}:{self._config.account_id}:{session.isoformat()}"
        if self._flat_due(now, session):
            state.flat_asked = True
            self._sweep_flat(session, state)
        tallied = self._router.manage_at_snapshot(
            self._config.account_id, session, snapshot, state.snapshots, cause
        )
        state.tally += tallied

    def _sweep_flat(self, session: date, state: "_TickState") -> None:
        """The 15:30 sweep: flat no matter what the quotes say (rules doc §6.4)."""
        manager = self._router.manager(self._config.account_id)
        for structure in self._open_structures():
            if structure.closing_order_id is not None:
                continue
            manager.close(
                self._config.account_id,
                flat_close(structure, session, "flat 15:30"),
            )
            state.tally.exit_actions += 1

    def _go_flat_and_refuse(self, session: date, state: "_TickState", why: str) -> None:
        """Stale quote: ask to be flat; refuse entries until quotes return (I5).

        The command id stays free of the stale message — it varies between the first
        process and a resumed one — while the human reason carries the detail (I3).
        """
        state.refusing = True
        if state.flat_asked:
            return  # the sweep is already working; a second would over-close (C5)
        state.flat_asked = True
        manager = self._router.manager(self._config.account_id)
        for structure in self._open_structures():
            if structure.closing_order_id is not None:
                continue
            manager.close(
                self._config.account_id,
                flat_close(structure, session, "stale quote"),
            )
            state.tally.exit_actions += 1

    # -- pieces ------------------------------------------------------------------

    def _open_structures(self):
        from trade_engine.oms.options import open_structures

        return open_structures(self._ledger.state(self._config.account_id))

    def _flat_due(self, now: datetime, session: date) -> bool:
        flat = datetime.combine(session, self._config.flat_at, tzinfo=NEW_YORK)
        return now >= flat

    def _rehydrate(self) -> None:
        """A new process starts with an empty venue; restore it from the fold (I2)."""
        from trade_engine.oms.restore import restorable, restorable_positions

        broker = self._config.broker
        if broker.orders(MIN_TIME) or broker.fills(MIN_TIME):
            return
        state = self._ledger.state(self._config.account_id)
        orders, fills = restorable(self._ledger, self._config.account_id, state)
        positions = restorable_positions(state)
        if orders or fills or positions:
            broker.restore(orders, fills, positions)

    def _close_mark(self, session: date, state: "_TickState") -> None:
        """Mark at the newest snapshot's mid, then the run marker (provenance)."""
        from trade_engine.domain.instruments import Equity
        from trade_engine.ledger import Mark
        from trade_engine.oms.options import open_structures
        from trade_engine.sim import underlying_of

        now = self._clock.now_utc()
        folded = self._ledger.state(self._config.account_id)
        marks: dict[Any, tuple[Any, str]] = {}
        underlyings: set[str] = set()
        for instrument, position in sorted(
            folded.positions.items(), key=lambda item: item[0].symbol
        ):
            if position.quantity == 0:
                continue
            underlying = underlying_of(instrument)
            snapshot = state.snapshots.get(underlying)
            quote = None if snapshot is None else snapshot.get(instrument)
            if quote is None or quote.mid <= 0:
                raise IntradayServiceError(
                    f"No usable {session.isoformat()} quote for {instrument.occ.strip()} in "
                    f"'{self._config.account_id}'; refusing to mark it (I5)"
                )
            marks[instrument] = (quote.mid, f"snapshot:{snapshot.as_of.isoformat()}")
            underlyings.add(underlying)
        for underlying in sorted(underlyings):
            if Equity(underlying) not in marks:
                # The spot is priced by its own snapshot; no other mark is needed.
                snapshot = state.snapshots.get(underlying)
                if snapshot is None:
                    raise IntradayServiceError(
                        f"No {session.isoformat()} snapshot for the {underlying} spot; "
                        f"its option margin cannot be measured (I5)"
                    )
                marks[Equity(underlying)] = (
                    snapshot.underlying_price,
                    f"snapshot:{snapshot.as_of.isoformat()}",
                )
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
                command_id=(
                    f"intraday:{self._config.job_name}:{self._config.account_id}:{session.isoformat()}"
                ),
            )
        )

    def _session_snapshot_count(self, session: date) -> int:
        """Distinct snapshot instants matched this session, across every process.

        Each match writes reconcile commands stamped with the snapshot's instant; the
        ledger, not the process's own counter, is the record (I2, I3).
        """
        prefix = (
            f"intraday:{self._config.job_name}:{self._config.account_id}:{session.isoformat()}:"
        )
        instants: set[str] = set()
        for event in self._ledger.events(account=self._config.account_id):
            command = event.command_id or ""
            if command.startswith(prefix) and ":reconcile:" in command:
                instants.add(command.split(":reconcile:")[1].rsplit(":", 1)[0])
        return len(instants)

    # -- restart gate, heartbeat, plumbing ---------------------------------------

    def _refuse_other_live_instance(self, session: date) -> None:
        heartbeat = self._read_heartbeat()
        if heartbeat is None:
            return
        fresh = (
            self._clock.now_utc() - heartbeat.at_utc
        ).total_seconds() <= self._config.heartbeat_ttl_seconds
        if heartbeat.session == session and fresh:
            raise IntradayServiceError(
                f"A heartbeat for '{self._config.account_id}' written "
                f"{heartbeat.at_utc.isoformat()} is still fresh; another instance is "
                f"running this session — refusing to start a second one (C4)"
            )

    def _require_previous_eod_complete(self, session: date) -> None:
        account = self._config.account_id
        own_marker = (
            f"intraday:{self._config.job_name}:{account}:{session.isoformat()}"
        )
        if self._ledger.event_by_command(own_marker) is not None:
            return  # this session already completed; the re-run proves idempotency
        previous = self._calendar.previous_session(session)
        command = f"eod:options:{account}:{previous.isoformat()}"
        if self._ledger.event_by_command(command) is not None:
            return
        if self._has_history(account):
            raise IntradayServiceError(
                f"Cannot run {session.isoformat()} for '{account}': the previous session "
                f"{previous.isoformat()} has no options EOD marker; complete it first (I3)"
            )

    def _has_history(self, account_id: str) -> bool:
        for event in self._ledger.events(account=account_id):
            if event.kind is EventKind.EOD_RUN:
                return True
        return False

    def _write_heartbeat(self, session: date, state: "_TickState", note: str) -> None:
        if self._heartbeat_path is None:
            return
        heartbeat = Heartbeat(
            account_id=self._config.account_id,
            session=session,
            at_utc=self._clock.now_utc(),
            refusing=state.refusing,
            note=note,
        )
        self._heartbeat_path.write_text(
            json.dumps(asdict(heartbeat), default=str), encoding="utf-8"
        )

    def _read_heartbeat(self) -> Heartbeat | None:
        path = self._heartbeat_path
        if path is None or not path.exists():
            return None
        raw = json.loads(path.read_text(encoding="utf-8"))
        return Heartbeat(
            account_id=raw["account_id"],
            session=date.fromisoformat(raw["session"]),
            at_utc=datetime.fromisoformat(raw["at_utc"]),
            refusing=raw["refusing"],
            note=raw.get("note", ""),
        )

    def _advance(self, target: datetime) -> None:
        advance = getattr(self._clock, "advance_to", None)
        if callable(advance) and self._clock.now_utc() < target:
            advance(target)

    def _pause(self) -> None:
        self._clock.sleep(self._config.tick_seconds)


@dataclass
class _TickState:
    refusing: bool = False
    flat_asked: bool = False
    tally: RoutingTally = field(default_factory=RoutingTally)
    snapshots: dict[str, ChainSnapshot] = field(default_factory=dict)


def flat_close(structure: Any, session: date, reason: str) -> Any:
    """The market close that undoes whatever is still open of ``structure``."""
    from trade_engine.domain.option_orders import CloseStructure

    return CloseStructure(
        entry_order_id=structure.entry_order_id,
        reason=reason,
        command_id=f"intraday:flat:{structure.entry_order_id}:{reason}:{session.isoformat()}",
    )
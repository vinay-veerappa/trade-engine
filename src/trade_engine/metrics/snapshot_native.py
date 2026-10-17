"""Native daily account snapshot backed by te_core::snapshot (E8b, rules doc §11).

Derived from the ledger: one snapshot per account per session close, using the
Mark events the EOD runner wrote. Nothing is written back to the ledger; the
peak margin and drawdown are defined over the requested window, not the
ledger's lifetime. Heat is the sum of open risk against the current protective
stops (working stop orders in the folded state on the reduce side); a position
with no working stop contributes an unknown heat entry, listed — never guessed.

All business logic and decisions run in pure Rust (crates/te_core/src/snapshot.rs)
via trade_engine_rs.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
import decimal
from typing import Any, Callable

import trade_engine_rs as _rs  # missing is an ImportError, never a fallback (D5)

from trade_engine.domain.instruments import Instrument, UnresolvableInstrumentError
from trade_engine.ledger import codec
from trade_engine.ledger.errors import LedgerFoldError
from trade_engine.ledger.events import Event
from trade_engine.ledger.state import AccountState

ZERO = Decimal("0")


class SnapshotError(RuntimeError):
    """Raised when the ledger cannot support a snapshot (I5)."""


@dataclass(frozen=True)
class AccountSnapshot:
    """One account's margin/exposure figures at one session close."""

    session: date
    equity: Decimal
    cash: Decimal
    gross_exposure: Decimal
    net_exposure: Decimal
    margin_used: Decimal
    margin_available: Decimal
    margin_peak: Decimal
    heat: Decimal
    heat_unknown_positions: tuple[str, ...]
    drawdown_from_peak: Decimal
    drawdown_duration_sessions: int


def _refusal(kind: str, message: str) -> BaseException:
    if kind == "snapshot":
        return SnapshotError(message)
    if kind == "value":
        return ValueError(message)
    if kind == "unresolvable":
        return UnresolvableInstrumentError(message)
    if kind == "overflow":
        return decimal.Overflow(message)
    if kind in ("fold", "ledger"):
        return LedgerFoldError(message)
    return SnapshotError(message)


def _call(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    try:
        return fn(*args, **kwargs)
    except ValueError as err:
        if type(err) is ValueError and len(err.args) == 2 and all(isinstance(a, str) for a in err.args):
            raise _refusal(*err.args) from None
        raise


def _raw(s: AccountSnapshot) -> tuple:
    return (
        s.session.isoformat(),
        str(s.equity),
        str(s.cash),
        str(s.gross_exposure),
        str(s.net_exposure),
        str(s.margin_used),
        str(s.margin_available),
        str(s.margin_peak),
        str(s.heat),
        list(s.heat_unknown_positions),
        str(s.drawdown_from_peak),
        s.drawdown_duration_sessions,
    )


def _from_raw(r: tuple) -> AccountSnapshot:
    return AccountSnapshot(
        session=date.fromisoformat(r[0]),
        equity=Decimal(r[1]),
        cash=Decimal(r[2]),
        gross_exposure=Decimal(r[3]),
        net_exposure=Decimal(r[4]),
        margin_used=Decimal(r[5]),
        margin_available=Decimal(r[6]),
        margin_peak=Decimal(r[7]),
        heat=Decimal(r[8]),
        heat_unknown_positions=tuple(r[9]),
        drawdown_from_peak=Decimal(r[10]),
        drawdown_duration_sessions=r[11],
    )


def _session_of(event: Event) -> date:
    return _payload_as_of(event).date()


def _payload_as_of(event: Event) -> datetime:
    as_of = getattr(event.payload, "as_of", None)
    if as_of is None:
        raise SnapshotError(f"Event {event.kind} has no as_of to date a session close (I5)")
    return as_of


def _working_stop_risk(state: AccountState, session: date | None = None) -> tuple[Decimal, tuple[str, ...]]:
    """Sum (mark - stop) risk over open positions with a working protective stop."""
    del session
    state_text = codec.text(codec.canon(state))
    heat_str, unknown = _call(_rs.snapshot_working_stop_risk, state_text)
    return Decimal(heat_str), tuple(unknown)


def _protective_stop(state: AccountState, instrument: Instrument) -> Decimal | None:
    """The stop price of a working order on the opposite side (reduce side) of a position."""
    state_text = codec.text(codec.canon(state))
    instr_text = codec.text(codec.canon(instrument))
    res = _call(_rs.snapshot_protective_stop, state_text, instr_text)
    return Decimal(res) if res is not None else None


def daily_snapshots(
    events: list[Event],
    account: str,
    overrides: dict[str, object] | None = None,
) -> list[AccountSnapshot]:
    """Fold events day by day, then emit one snapshot per session close for one account.

    A session close is a Mark as_of date; state before the first Mark produces
    no snapshot (there is nothing to mark yet). Marks must exist for every open
    position at each close (I5).
    """
    raw_events = [codec.event_bytes(e) for e in events]
    wire_overrides = None
    if overrides:
        wire_overrides = []
        for sym, val in overrides.items():
            if isinstance(val, (tuple, list)):
                wire_overrides.append((sym, str(val[0]), str(val[1])))
            elif hasattr(val, "initial") and hasattr(val, "maintenance"):
                wire_overrides.append((sym, str(val.initial), str(val.maintenance)))
            else:
                raise ValueError(f"Invalid margin override for {sym}: {val!r}")

    results = _call(_rs.snapshot_daily, raw_events, account, wire_overrides)
    return [_from_raw(r) for r in results]


def peak_margin(snapshots: list[AccountSnapshot]) -> Decimal:
    """Peak margin over a list of snapshots. Refuses over empty list (I5)."""
    raw_snaps = [_raw(s) for s in snapshots]
    return Decimal(_call(_rs.snapshot_peak_margin, raw_snaps))


def max_drawdown(snapshots: list[AccountSnapshot]) -> Decimal:
    """Max drawdown over a list of snapshots. Refuses over empty list (I5)."""
    raw_snaps = [_raw(s) for s in snapshots]
    return Decimal(_call(_rs.snapshot_max_drawdown, raw_snaps))


def day_note_line(snapshot: AccountSnapshot, account: str) -> str:
    """The one-line daily journal note (E8b -> journal day note)."""
    return _call(_rs.snapshot_day_note_line, _raw(snapshot), account)


__all__ = [
    "AccountSnapshot",
    "SnapshotError",
    "daily_snapshots",
    "day_note_line",
    "max_drawdown",
    "peak_margin",
]

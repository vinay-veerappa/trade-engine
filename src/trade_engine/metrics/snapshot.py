"""Daily account snapshot (E8b, rules doc §11).

Derived from the ledger: one snapshot per account per session close, using the
Mark events the EOD runner wrote. Nothing is written back to the ledger; the
peak margin and drawdown are defined over the requested window, not the
ledger's lifetime. Heat is the sum of open risk against the current protective
stops (working stop orders in the folded state); a position with no working
stop contributes an unknown heat entry, listed — never guessed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from trade_engine.domain.instruments import Instrument
from trade_engine.domain.orders import Order, OrderState, OrderType
from trade_engine.ledger.events import Event
from trade_engine.ledger.state import AccountState, fold_account
from trade_engine.metrics.margin import AccountMargin, account_margin

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


def _session_of(event: Event) -> date:
    return _payload_as_of(event).date()


def _payload_as_of(event: Event) -> datetime:
    as_of = getattr(event.payload, "as_of", None)
    if as_of is None:
        raise SnapshotError(f"Event {event.kind} has no as_of to date a session close (I5)")
    return as_of


def _working_stop_risk(state: AccountState, session: date) -> tuple[Decimal, tuple[str, ...]]:
    """Sum (mark − stop) risk over open positions with a working protective stop.

    A position whose protective stop is missing from the working set is listed
    in heat_unknown_positions instead of being folded into the number.
    """
    del session
    heat = ZERO
    unknown: list[str] = []
    for instrument, position in sorted(state.positions.items(), key=lambda kv: kv[0].symbol):
        if position.is_flat:
            continue
        mark = state.marks.get(instrument)
        if mark is None or mark <= ZERO:
            raise SnapshotError(f"No session-close mark for {instrument.symbol} (I5)")
        stop = _protective_stop(state, instrument)
        if stop is None:
            unknown.append(instrument.symbol)
            continue
        if position.is_long:
            risk = mark - stop
            heat += risk * position.quantity
        else:
            risk = stop - mark
            heat += risk * (-position.quantity)
    return heat, tuple(unknown)


def _protective_stop(state: AccountState, instrument: Instrument) -> Decimal | None:
    """The stop price of a working order on the opposite side (reduce side) of a position."""
    reduce_side = None
    for order in state.orders.values():
        if order.instrument != instrument or order.stop_price is None:
            continue
        if order.order_type not in (OrderType.STOP, OrderType.STOP_LIMIT):
            continue
        if order.state not in (
            OrderState.NEW, OrderState.SUBMITTED, OrderState.ACCEPTED, OrderState.PARTIALLY_FILLED,
        ):
            continue
        reduce_side = order  # last working stop on this instrument wins
    return reduce_side.stop_price if reduce_side is not None else None


def daily_snapshots(
    events: list[Event],
    account: str,
    overrides: dict[str, object] | None = None,
) -> list[AccountSnapshot]:
    """Fold once, then emit one snapshot per session close for one account.

    A session close is a Mark as_of date; state before the first Mark produces
    no snapshot (there is nothing to mark yet). Marks must exist for every open
    position at each close (I5).
    """
    del overrides  # per-symbol margin overrides, threaded to account_margin
    state = fold_account(events, account)
    closes = sorted({
        _payload_as_of(event).date()
        for event in events
        if event.account == account and type(event.payload).__name__ == "Mark"
    })
    if not closes:
        return []
    # Replay the marks day by day so equity is the state AT that close (I2).
    snapshots: list[AccountSnapshot] = []
    peak_margin: Decimal | None = None
    peak_equity: Decimal | None = None
    dd_start: int | None = None
    ordered_events = sorted(
        [e for e in events if e.account == account],
        key=lambda e: (e.ts_utc, e.seq or 0),
    )
    upto = 0
    for index, session in enumerate(closes):
        # Include every account event at or before this session's last close stamp.
        close_stamp = max(
            _payload_as_of(event) for event in events
            if event.account == account and type(event.payload).__name__ == "Mark"
            and _payload_as_of(event).date() == session
        )
        while upto < len(ordered_events) and ordered_events[upto].ts_utc <= close_stamp:
            upto += 1
        state = fold_account(ordered_events[:upto], account)
        if not state.marks:
            continue
        try:
            margin: AccountMargin = account_margin(state)
        except ValueError as exc:
            if "No session-close mark" in str(exc):
                raise SnapshotError(str(exc)) from exc
            raise
        if peak_margin is None or margin.margin_used > peak_margin:
            peak_margin = margin.margin_used
        if peak_equity is None or margin.equity > peak_equity:
            peak_equity = margin.equity
            dd_start = None
        dd = ZERO if peak_equity is None or peak_equity <= ZERO else peak_equity - margin.equity
        if peak_equity is not None and dd > ZERO and dd_start is None:
            dd_start = index
        heat, unknown = _working_stop_risk(state, session)
        snapshots.append(
            AccountSnapshot(
                session=session,
                equity=margin.equity,
                cash=margin.cash,
                gross_exposure=margin.gross_exposure,
                net_exposure=margin.net_exposure,
                margin_used=margin.margin_used,
                margin_available=margin.margin_available,
                margin_peak=peak_margin,
                heat=heat,
                heat_unknown_positions=unknown,
                drawdown_from_peak=dd,
                drawdown_duration_sessions=0 if dd_start is None else index - dd_start,
            )
        )
    return snapshots


def peak_margin(snapshots: list[AccountSnapshot]) -> Decimal:
    if not snapshots:
        raise SnapshotError("peak_margin over an empty snapshot list refuses (I5)")
    return max(s.margin_used for s in snapshots)


def max_drawdown(snapshots: list[AccountSnapshot]) -> Decimal:
    if not snapshots:
        raise SnapshotError("max_drawdown over an empty snapshot list refuses (I5)")
    return max(s.drawdown_from_peak for s in snapshots)


def day_note_line(snapshot: AccountSnapshot, account: str) -> str:
    """The one-line daily journal note (E8b → journal day note)."""
    gross_pct = (
        f"{(snapshot.gross_exposure / snapshot.equity * 100):.1f}%"
        if snapshot.equity != ZERO
        else "n/a"
    )
    heat_line = (
        f"{snapshot.heat / snapshot.equity * 100:.1f}%"
        if snapshot.equity != ZERO
        else "n/a"
    )
    unknown = (
        f" (no stop: {','.join(snapshot.heat_unknown_positions)})"
        if snapshot.heat_unknown_positions
        else ""
    )
    return (
        f"snapshot {account} {snapshot.session}: equity {snapshot.equity}, "
        f"gross {gross_pct}, margin used {snapshot.margin_used:.0f} "
        f"(peak {snapshot.margin_peak:.0f}), heat {heat_line}{unknown}, "
        f"DD {snapshot.drawdown_from_peak}"
    )




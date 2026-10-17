"""A read-only invariant audit of one ledger sqlite file (branch te/ledger-audit).

    python tools/ledger_invariants_audit.py LEDGER.db [...]

The file is opened READ ONLY (`file:<path>?mode=ro`, uri=True); rows come from the
`events(seq, ts_utc, account, kind, command_id, payload_json, schema_version)` table.
The rows are folded with the Rust fold through `trade_engine_rs` (the production
`trade_engine.ledger.state.fold_account`, one call per account, exactly the call
`tools/ledger_parity.py`'s reader side is backed by). No rule is reimplemented in
Python: the Rust fold is the thing under audit; this tool only asserts properties of
its output and of the raw rows, and derives cash expectations from the flows
`crates/te_core/src/ledger/fold.rs` states directly.

One line per check: `OK|FAIL <id> <what> <count checked>`; detail lines under a FAIL.
Exit code: 0 clean, 1 when any check FAILs, 2 when a file cannot be opened.
"""

from __future__ import annotations

import sqlite3
import sys
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from trade_engine.domain.instruments import Equity, Instrument  # noqa: E402
from trade_engine.domain.orders import OrderState, Side  # noqa: E402
from trade_engine.ledger import codec  # noqa: E402
from trade_engine.ledger.events import EventKind  # noqa: E402
from trade_engine.ledger.state import fold_account  # noqa: E402 - the Rust fold over encoded events

TERMINAL_KINDS = frozenset({EventKind.ORDER_CANCELLED, EventKind.ORDER_EXPIRED, EventKind.ORDER_REJECTED})
TERMINAL_STATES = frozenset({OrderState.FILLED, OrderState.CANCELLED, OrderState.EXPIRED, OrderState.REJECTED})
SETTLEMENT_KINDS = frozenset({EventKind.EXPIRY, EventKind.EXERCISE, EventKind.ASSIGNMENT})


# --- loading -----------------------------------------------------------------------------

def open_ledger(path: Path) -> sqlite3.Connection:
    """Read-only connection; raises sqlite3.Error when the file cannot be opened."""
    uri = path.resolve().as_uri().replace("file:///", "file:/") + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("SELECT count(*) FROM events")  # not a ledger without the table
    return conn


def load_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM events ORDER BY seq, rowid").fetchall()


def decode_rows(rows: list[sqlite3.Row]) -> tuple[dict[str, list], list[str]]:
    """Decode every row with the production codec; returns (events per account, failures)."""
    events: dict[str, list] = {}
    failures: list[str] = []
    for row in rows:
        try:
            event = codec.event_from_row(
                row["account"], row["kind"], row["payload_json"], row["ts_utc"],
                row["command_id"], row["schema_version"], row["seq"],
            )
        except Exception as err:  # noqa: BLE001 - the refusal is the finding
            failures.append(f"seq {row['seq']} ({row['kind']}) does not decode: {err}")
            continue
        events.setdefault(event.account, []).append(event)
    return events, failures


def fold_states(events: dict[str, list]) -> tuple[dict, dict[str, str]]:
    """The Rust fold, one account at a time, so one refused account hides no other."""
    states: dict = {}
    errors: dict[str, str] = {}
    for account, account_events in events.items():
        try:
            states[account] = fold_account(account_events, account)
        except Exception as err:  # noqa: BLE001 - the refusal is the finding
            errors[account] = str(err)
    return states, errors


# --- expectations, from the flows fold.rs states ----------------------------------------

def _multiplier(instrument: Instrument) -> Decimal:
    """Multiplies a fill into notional: 1 for an equity, the contract multiplier for an
    option (I6), a uniform combo's leg multiplier; a mixed combo refuses."""
    if isinstance(instrument, Equity):
        return Decimal(1)
    return Decimal(instrument.multiplier)


def _label(instrument: Instrument) -> str:
    """The instrument's symbol (a property on the Python carriers)."""
    s = instrument.symbol
    return s() if callable(s) else s


def _fill_multiplier(payload) -> Decimal:
    instrument = payload.instrument
    try:
        return _multiplier(instrument)
    except Exception as err:  # noqa: BLE001 - a mixed-multiplier combo cannot tie out
        raise ValueError(f"fill {payload.fill_id} on {type(instrument).__name__}: {err}") from err


def _fill_cash_delta(payload) -> Decimal:
    """fold.rs `on_fill`: signed_gross - fee (a buy pays the gross, a sell receives it)."""
    signed_gross = payload.quantity * payload.price * _fill_multiplier(payload)
    return -signed_gross - payload.fee if payload.side == Side.BUY else signed_gross - payload.fee


# --- the checks --------------------------------------------------------------------------

class Check:
    """One invariant's verdict and what was examined."""

    def __init__(self, cid: str, what: str) -> None:
        self.cid = cid
        self.what = what
        self.ok = True
        self.count = 0
        self.details: list[str] = []

    def violation(self, detail: str) -> None:
        self.ok = False
        self.details.append(detail)

    def note(self, detail: str) -> None:
        self.details.append(detail)

    def line(self) -> str:
        return f"{'OK' if self.ok else 'FAIL'} {self.cid} {self.what} {self.count}"


def check_a1(rows: list[sqlite3.Row]) -> Check:
    """A1: seq is strictly increasing with no gaps per file."""
    check = Check("A1", "seq strictly increasing, no gaps")
    previous: int | None = None
    for row in rows:
        check.count += 1
        seq = int(row["seq"])
        if previous is not None:
            if seq == previous:
                check.violation(f"seq {seq} appears more than once")
            elif seq > previous + 1:
                check.violation(f"gap: seq jumps {previous} -> {seq}")
        previous = seq
    if rows and int(rows[0]["seq"]) != 1:
        check.note(f"first seq is {int(rows[0]['seq'])}; the store starts at 1, so the head is missing")
    return check


def check_a2(rows: list[sqlite3.Row], events: dict[str, list], decode_failures: list[str],
             fold_errors: dict[str, str]) -> Check:
    """A2: every row decodes and every account's rows refold without a refusal."""
    check = Check("A2", "rows decode and refold without refusal")
    check.count = len(rows) + len(events)
    for failure in decode_failures:
        check.violation(failure)
    for account, message in sorted(fold_errors.items()):
        check.violation(f"account {account} refuses to fold: {message}")
    return check


def check_a3(rows: list[sqlite3.Row]) -> Check:
    """A3: command_id unique among rows of the same account where non-null (I3)."""
    check = Check("A3", "command_id unique per account")
    seen: dict[tuple[str, str], int] = {}
    for row in rows:
        command_id = row["command_id"]
        if not command_id:
            continue
        check.count += 1
        key = (row["account"], command_id)
        seen[key] = seen.get(key, 0) + 1
    for (account, command_id), n in sorted(seen.items()):
        if n > 1:
            check.violation(f"command_id '{command_id}' appears on {n} rows of account {account}")
    return check


def check_a4(rows_by_account: dict[str, list[sqlite3.Row]], decoded: dict[str, list],
             states: dict, fold_errors: dict[str, str]) -> Check:
    """A4: cash conservation per account, over the flows fold.rs books directly.

    cash == sum(CashFlow.amount) + sum(fill cash deltas: signed gross - fee) + 0 per
    Expiry (it books no cash: its exit price is zero). An Exercise/Assignment books the
    intrinsic-value settlement or the strike delivery, which te_options' lifecycle rules
    own; those events are reported as untied rather than guessed. A fee is already inside
    the fold's realized_pnl too, so it is counted once here and not again there.
    """
    check = Check("A4", "cash ties out to cash flows, fills and fees")
    for account in sorted(decoded):
        check.count += 1
        rows = rows_by_account.get(account, [])
        if account in fold_errors or len(decoded[account]) != len(rows):
            check.violation(f"account {account}: cannot certify (rows undecoded or fold refused)")
            continue
        expected = Decimal(0)
        untied = 0
        for row, event in zip(rows, decoded[account]):  # same order; lengths verified above
            payload = event.payload
            if event.kind == EventKind.CASH_FLOW:
                expected += payload.amount
            elif event.kind == EventKind.FILL:
                try:
                    expected += _fill_cash_delta(payload)
                except ValueError as err:
                    check.violation(f"account {account}: {err}")
            elif event.kind == EventKind.EXPIRY:
                pass  # fold.rs: an expiry's exit price is zero, so it books no cash
            elif event.kind in (EventKind.EXERCISE, EventKind.ASSIGNMENT):
                untied += 1  # settlement is te_options lifecycle: reported, not guessed
        state = states[account]
        if state.cash != expected:
            check.violation(f"account {account}: folded cash {state.cash} != tied-out flows {expected}")
        if untied:
            check.note(f"account {account}: {untied} exercise/assignment settlement flows not tied out "
                       f"(te_options lifecycle owns them)")
    return check


def check_a5(decoded: dict[str, list], states: dict, fold_errors: dict[str, str]) -> Check:
    """A5: position quantity == signed sum of fills per (account, instrument); a flat
    position carries no open lots. Instruments with a lifecycle event are excluded from
    the strict equality (expiry/exercise/assignment consume lots without a fill) and noted."""
    check = Check("A5", "position quantity equals signed fill sum; flat means no lots")
    for account in sorted(decoded):
        check.count += 1
        if account in fold_errors:
            check.violation(f"account {account}: cannot certify (fold refused)")
            continue
        sums: dict[Instrument, Decimal] = {}
        settled: set[Instrument] = set()
        for event in decoded[account]:
            if event.kind == EventKind.FILL:
                sign = 1 if event.payload.side == Side.BUY else -1
                instrument = event.payload.instrument
                sums[instrument] = sums.get(instrument, Decimal(0)) + sign * event.payload.quantity
            elif event.kind in SETTLEMENT_KINDS:
                settled.add(event.payload.contract)
        positions = states[account].positions
        for instrument, total in sums.items():
            position = positions.get(instrument)
            quantity = position.quantity if position is not None else Decimal(0)
            if instrument in settled:
                check.note(f"account {account} {_label(instrument)}: lots were consumed by a "
                           f"settlement event; fill-sum equality not asserted")
                continue
            if quantity != total:
                check.violation(f"account {account} {_label(instrument)}: folded quantity {quantity} "
                                f"!= signed fill sum {total}")
            if total == 0 and position is not None and position.open_lots:
                check.violation(f"account {account} {_label(instrument)}: flat position carries "
                                f"{len(position.open_lots)} open lots")
    return check


def check_a6(states: dict, fold_errors: dict[str, str]) -> Check:
    """A6: FIFO — lots are consumed in acquisition order, so the lots a fold leaves are in
    non-decreasing acquisition order."""
    check = Check("A6", "open lots remain in acquisition order")
    for account, state in sorted(states.items()):
        if account in fold_errors:
            check.violation(f"account {account}: cannot certify (fold refused)")
            continue
        for instrument, position in state.positions.items():
            lots = position.open_lots
            if len(lots) < 2:
                continue
            check.count += 1
            for earlier, later in zip(lots, lots[1:]):
                if earlier.acquired_at > later.acquired_at:
                    check.violation(f"account {account} {_label(instrument)}: lot {later.lot_id} "
                                    f"acquired {later.acquired_at} sits after {earlier.lot_id} "
                                    f"acquired {earlier.acquired_at}")
    return check


def check_a7(decoded: dict[str, list], states: dict, fold_errors: dict[str, str]) -> Check:
    """A7: every order holds exactly one folded state, and an order whose account carries a
    terminal event row is terminal in the fold."""
    check = Check("A7", "every order one state; terminal events leave terminal orders")
    for account in sorted(decoded):
        rows = decoded[account]
        terminal_of: dict[str, int] = {}
        for event in rows:
            if event.kind in TERMINAL_KINDS:
                order_id = event.payload.order_id
                terminal_of[order_id] = max(terminal_of.get(order_id, 0), event.seq or 0)
        if not terminal_of:
            continue
        if account in fold_errors:
            check.violation(f"account {account}: cannot certify (fold refused)")
            continue
        orders = states[account].orders
        for order_id in sorted(terminal_of):
            check.count += 1
            order = orders.get(order_id)
            if order is None:
                check.violation(f"account {account}: terminal event for unknown order '{order_id}'")
            elif order.state not in TERMINAL_STATES:
                check.violation(f"account {account}: order '{order_id}' has a terminal event but "
                                f"folds as {order.state.value}")
    return check


def check_a8(decoded: dict[str, list]) -> Check:
    """A8 (the clause te_core defines): no fill arrives after the order's terminal state.
    The clause 'no negative quantity on a long-only cash account' is not checked — see the
    'not checked' note in the output."""
    check = Check("A8", "no fill after a terminal order state")
    for account in sorted(decoded):
        terminal_seq: dict[str, int] = {}
        for event in decoded[account]:
            if event.kind in TERMINAL_KINDS:
                order_id = event.payload.order_id
                terminal_seq[order_id] = max(terminal_seq.get(order_id, 0), event.seq or 0)
        for event in decoded[account]:
            if event.kind != EventKind.FILL:
                continue
            check.count += 1
            gate = terminal_seq.get(event.payload.order_id)
            if gate is not None and (event.seq or 0) > gate:
                check.violation(f"account {account}: fill {event.payload.fill_id} at seq {event.seq} "
                                f"follows a terminal event at seq {gate} for order "
                                f"'{event.payload.order_id}'")
    return check


NOT_CHECKED = [
    "A5/A8 long-only clause: 'no negative quantity on a long-only cash account' - nothing in "
    "README's invariants or crates/te_core marks an account long-only (the fold books a flip to "
    "short without refusing), so which accounts the clause applies to is not defined; not checked.",
    "A4 Exercise/Assignment settlement cash: fold.rs books intrinsic-value settlement for "
    "cash-settled contracts and strike-times-shares delivery for physical ones, with the rules in "
    "te_options' lifecycle (lc::); the ledger rows alone do not state them, so those flows are "
    "reported per account as untied rather than guessed.",
]


def audit_file(path: Path) -> tuple[list[str], int]:
    """One ledger's report lines and its status: 0 clean, 1 a check failed, 2 unopenable."""
    lines = []
    try:
        conn = open_ledger(path)
    except sqlite3.Error as err:
        return [f"FAIL open {path.name}: cannot be opened read-only ({err})"], 2
    try:
        rows = load_rows(conn)
    finally:
        conn.close()

    decoded, decode_failures = decode_rows(rows)
    rows_by_account: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        rows_by_account.setdefault(row["account"], []).append(row)
    states, fold_errors = fold_states(decoded)

    checks = [
        check_a1(rows),
        check_a2(rows, decoded, decode_failures, fold_errors),
        check_a3(rows),
        check_a4(rows_by_account, decoded, states, fold_errors),
        check_a5(decoded, states, fold_errors),
        check_a6(states, fold_errors),
        check_a7(decoded, states, fold_errors),
        check_a8(decoded),
    ]
    lines.append(f"{path.name}: {len(rows)} rows, {len(decoded)} accounts")
    status = 0
    for check in checks:
        if not check.ok:
            status = 1
        lines.append(check.line())
        for detail in check.details:
            lines.append(f"    {detail}")
    lines.append("not checked, and why:")
    for item in NOT_CHECKED:
        lines.append(f"    - {item}")
    return lines, status


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: ledger_invariants_audit.py LEDGER.db [...]")
        return 2
    worst = 0
    for arg in argv:
        lines, status = audit_file(Path(arg))
        for line in lines:
            print(line)
        worst = max(worst, status)
    return worst


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
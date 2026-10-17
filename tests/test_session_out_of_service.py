"""Gate test & behavioural tests for tos_paper.session_native retirement (§4.7, WIRE_TASK Step 3).

1. Gate test: AST scan of src/, tools/, launch/ proving nothing other than the old
   session.py imports trade_engine.tos_paper.session.
2. Behavioural tests: End-to-end execution of session_native through the Rust door
   (spec-derived expectations).
"""

from __future__ import annotations

import ast
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from trade_engine.domain.instruments import Combo, ComboLeg, OptionContract, Side
from trade_engine.domain.orders import Order, OrderState, OrderType, TimeInForce
from trade_engine.ledger import Event, EventKind, Ledger
from trade_engine.ledger.events import (
    EodRun,
    MirrorAck,
    MirrorFill,
    MirrorQueued,
    MirrorRefused,
    OrdersCreated,
    OrderUpdated,
    VenueHaltCleared,
    VenueReconcile,
    mirror_account,
)
from trade_engine.ledger.state import halted_venues
from trade_engine.tos_paper.broker import MirrorBinding, TosPaperBroker
from trade_engine.tos_paper.session_native import (
    MirrorRunReport,
    MirrorSessionError,
    cancel_ticket,
    clear_halt,
    collect_only,
    mirror_of,
    morning_orders,
    pending_orders,
    run_mirror,
    working_orders,
)
from trade_engine.tos_paper.transport import MirrorComboTicket

REPO_ROOT = Path(__file__).resolve().parent.parent

# -- 1. Gate Test: AST scan --------------------------------------------------------------------


def _check_out_of_service(root_dirs: list[Path], retired_module: str, allowed_file: Path) -> list[str]:
    violations: list[str] = []
    allowed_resolved = allowed_file.resolve()

    for rdir in root_dirs:
        if not rdir.exists():
            continue
        for py_file in rdir.rglob("*.py"):
            if py_file.resolve() == allowed_resolved:
                continue
            text = py_file.read_text(encoding="utf-8")
            if "session" not in text:
                continue
            try:
                tree = ast.parse(text, filename=str(py_file))
            except SyntaxError:
                continue

            rel = py_file.relative_to(REPO_ROOT).as_posix()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name == retired_module:
                            violations.append(f"{rel}:{node.lineno} imports '{alias.name}'")
                elif isinstance(node, ast.ImportFrom):
                    # Check absolute imports
                    if node.level == 0:
                        if node.module == retired_module:
                            violations.append(f"{rel}:{node.lineno} imports from '{node.module}'")
                        elif node.module == "trade_engine.tos_paper":
                            for alias in node.names:
                                if alias.name == "session":
                                    violations.append(f"{rel}:{node.lineno} imports '{alias.name}' from '{node.module}'")
                    else:
                        # Check relative imports inside trade_engine/tos_paper
                        try:
                            file_rel_src = py_file.relative_to(REPO_ROOT / "src").parts[:-1]
                            if len(file_rel_src) >= node.level:
                                base_pkg = ".".join(file_rel_src[: len(file_rel_src) - node.level + 1])
                                full_mod = f"{base_pkg}.{node.module}" if node.module else base_pkg
                                if full_mod == retired_module:
                                    violations.append(f"{rel}:{node.lineno} relatively imports from '{full_mod}'")
                                elif full_mod == "trade_engine.tos_paper":
                                    for alias in node.names:
                                        if alias.name == "session":
                                            violations.append(f"{rel}:{node.lineno} relatively imports '{alias.name}'")
                        except ValueError:
                            pass
    return violations


def test_session_out_of_service_ast_gate():
    """Assert no live file in src/, tools/, or launch/ imports the retired tos_paper.session module."""
    root_dirs = [REPO_ROOT / "src", REPO_ROOT / "tools", REPO_ROOT / "launch"]
    old_file = REPO_ROOT / "src" / "trade_engine" / "tos_paper" / "session.py"
    violations = _check_out_of_service(root_dirs, "trade_engine.tos_paper.session", old_file)
    assert not violations, "Forbidden import of retired tos_paper.session found:\n" + "\n".join(violations)


# -- 2. Behavioural Test Harness ---------------------------------------------------------------

T = datetime(2026, 9, 24, 20, 0, tzinfo=timezone.utc)
PM_A = "D-00000001"
VENUE_ACCT = mirror_account(PM_A)
P200 = OptionContract(underlying="AAPL", expiry=date(2026, 10, 16), strike=Decimal("200"), right="P")
P190 = OptionContract(underlying="AAPL", expiry=date(2026, 10, 16), strike=Decimal("190"), right="P")
SPREAD = Combo((ComboLeg(P200, 1, Side.SELL), ComboLeg(P190, 1, Side.BUY)))


class DummyClock:
    def __init__(self, now: datetime = T) -> None:
        self.now = now

    def now_utc(self) -> datetime:
        return self.now

    def sleep(self, seconds: float) -> None:
        pass


class FakeVenue:
    def __init__(self) -> None:
        self.orders: dict[str, dict] = {}
        self.positions: dict[str, Decimal] = {}
        self.next_id = 5400000001
        self.placed: list[object] = []
        self.prove_ids = True
        self.fill_read_raises: Exception | None = None

    def connect(self) -> dict[str, str]:
        return {"number": PM_A, "type": "margin"}

    def place_order(self, ticket, idempotency_key: str) -> dict:
        self.placed.append(ticket)
        oid = str(self.next_id)
        self.next_id += 1
        self.orders[oid] = {"ticket": ticket, "filled": 0, "avg": None, "status": "WORKING"}
        if not self.prove_ids:
            return {"status": "SENT"}
        return {"status": "SENT", "order_id": oid, "book_status": "WORKING"}

    def cancel_order(self, order_id: str) -> dict:
        self.orders[order_id]["status"] = "CANCELED"
        return {"status": "CANCELED", "order_id": order_id}

    def fill(self, oid: str, units: int, price: str) -> None:
        order = self.orders[oid]
        ticket = order["ticket"]
        new = units - order["filled"]
        for symbol, side, ratio in self._legs(ticket):
            signed = Decimal(new * ratio) * (1 if side == "BUY" else -1)
            self.positions[symbol] = self.positions.get(symbol, Decimal(0)) + signed
        order["filled"], order["avg"] = units, price
        if units == ticket.quantity:
            order["status"] = "FILLED"

    @staticmethod
    def _legs(ticket):
        if isinstance(ticket, MirrorComboTicket):
            return [(leg.symbol, leg.side, leg.ratio) for leg in ticket.legs]
        return [(ticket.symbol, ticket.side, 1)]

    def read_working_orders(self):
        rows = []
        for order in self.orders.values():
            ticket = order["ticket"]
            for symbol, side, ratio in self._legs(ticket):
                rows.append({
                    "symbol": symbol,
                    "side": side,
                    "quantity": str(ticket.quantity * ratio),
                    "filled": str(order["filled"] * ratio),
                    "order_type": ticket.order_type,
                    "limit_price": None if ticket.limit_price is None else str(ticket.limit_price),
                    "status": order["status"],
                })
        return rows

    def read_positions(self):
        return [{"symbol": s, "quantity": str(q), "avg_price": "1.00"} for s, q in self.positions.items() if q]

    def read_order_fills(self):
        if self.fill_read_raises is not None:
            raise self.fill_read_raises
        return [
            {"order_id": oid, "filled": str(o["filled"]), "avg_price": o["avg"], "status": o["status"]}
            for oid, o in self.orders.items()
        ]

    def close(self) -> None:
        pass


class FakeCalendar:
    def session_close(self, d: date) -> datetime:
        return datetime(d.year, d.month, d.day, 20, 0, tzinfo=timezone.utc)

    def session_open(self, d: date) -> datetime:
        return datetime(d.year, d.month, d.day, 13, 30, tzinfo=timezone.utc)


def _binding() -> MirrorBinding:
    return MirrorBinding(
        venue_account=PM_A,
        account_type="margin",
        mirrored_accounts=("OPT_CSP", "OPT_PUT_SPREAD"),
        minimum_balance=Decimal("100000"),
    )


def _broker(venue: FakeVenue | None = None, clock: DummyClock | None = None) -> tuple[TosPaperBroker, FakeVenue]:
    venue = venue or FakeVenue()
    broker = TosPaperBroker(venue, _binding(), clock=clock or DummyClock(), balance_unproven_ok=True)
    broker.connect()
    return broker, venue


def _make_order(
    oid: str,
    account: str = "OPT_CSP",
    side: Side = Side.SELL,
    qty: str = "1",
    *,
    instrument=P200,
    limit: str = "2.00",
    created: datetime = T,
    state=OrderState.NEW,
    parent: str | None = None,
    order_type=OrderType.LIMIT,
) -> Order:
    return Order(
        order_id=oid,
        account_id=account,
        instrument=instrument,
        order_type=order_type,
        side=side,
        quantity=Decimal(qty),
        command_id=oid,
        created_at=created,
        limit_price=Decimal(limit) if limit is not None else None,
        tif=TimeInForce.DAY,
        state=state,
        parent_order_id=parent,
    )


def _submit(ledger: Ledger, *orders: Order, session: date | None = date(2026, 9, 24), job: str = "eod") -> None:
    by_account: dict[str, list[Order]] = {}
    for order in orders:
        by_account.setdefault(order.account_id, []).append(order)
    for account, batch in by_account.items():
        for order in batch:
            ledger.append(
                Event(
                    account=account,
                    kind=EventKind.ORDERS_CREATED,
                    payload=OrdersCreated(orders=(order,), fingerprint=order.order_id, reason="entry"),
                    ts_utc=T,
                    command_id=f"create:{order.order_id}",
                )
            )
            ledger.append(
                Event(
                    account=account,
                    kind=EventKind.ORDER_UPDATED,
                    payload=OrderUpdated(order=order.transition_to(OrderState.SUBMITTED), reason="submit"),
                    ts_utc=T,
                    command_id=f"submit:{order.order_id}",
                )
            )
    if session is not None:
        for account in _binding().mirrored_accounts:
            ledger.append(
                Event(
                    account=account,
                    kind=EventKind.EOD_RUN,
                    payload=EodRun(session=session, job=job, account_id=account, bars_processed=1, at_close=T),
                    ts_utc=T,
                    command_id=f"eod:{job}:{account}:{session.isoformat()}",
                )
            )


@pytest.fixture()
def ledger(tmp_path: Path):
    with Ledger(tmp_path / "ledger.db") as lg:
        yield lg


# -- 3. Behavioural Tests ----------------------------------------------------------------------


def test_session_native_working_orders_spec(ledger: Ledger):
    """Test working_orders through session_native: entries only, deterministic sort, working state."""
    binding = _binding()
    o_csp_1 = _make_order("csp-1", "OPT_CSP")
    o_csp_exit = _make_order("csp-exit", "OPT_CSP", parent="csp-0")
    o_spread_1 = _make_order("sp-1", "OPT_PUT_SPREAD", instrument=SPREAD)
    o_spread_filled = _make_order("sp-2", "OPT_PUT_SPREAD", instrument=SPREAD)

    _submit(ledger, o_csp_1, o_csp_exit, o_spread_1, o_spread_filled)

    # Transition sp-2 to FILLED (non-working)
    st = ledger.state("OPT_PUT_SPREAD")
    cur_sp2 = st.orders["sp-2"]
    ledger.append(
        Event(
            account="OPT_PUT_SPREAD",
            kind=EventKind.ORDER_UPDATED,
            payload=OrderUpdated(order=cur_sp2.transition_to(OrderState.FILLED), reason="fill"),
            ts_utc=T,
            command_id="fill:sp-2",
        )
    )

    # working_orders should return entries only (parent_order_id=None), working only (SUBMITTED/ACCEPTED),
    # in account order (OPT_CSP, then OPT_PUT_SPREAD), then order_id order.
    orders = working_orders(ledger, binding)
    assert len(orders) == 2
    assert orders[0].order_id == "csp-1"
    assert orders[1].order_id == "sp-1"


def test_session_native_pending_and_morning_orders_markers(ledger: Ledger):
    """Test pending_orders and morning_orders through session_native enforce markers and job names."""
    binding = _binding()
    calendar = FakeCalendar()
    s = date(2026, 9, 24)

    # Without markers, pending_orders refuses (I5)
    with pytest.raises(MirrorSessionError, match="has no EOD run for"):
        pending_orders(ledger, binding, s, calendar=calendar)

    # Add orders and markers
    _submit(ledger, _make_order("csp-1", "OPT_CSP", created=T + timedelta(seconds=1)))
    pending = pending_orders(ledger, binding, s, calendar=calendar)
    assert len(pending) == 1
    assert pending[0].order_id == "csp-1"

    # Morning orders without morning marker refuses (I5)
    with pytest.raises(MirrorSessionError, match="has no morning pass for"):
        morning_orders(ledger, binding, s, calendar=calendar)


def test_session_native_run_mirror_end_to_end(ledger: Ledger):
    """Test run_mirror with session_native: write-ahead queue, drain acks, and collect_only."""
    broker, venue = _broker()
    clock = DummyClock()
    order = _make_order("csp-1", "OPT_CSP", limit="2.00")
    _submit(ledger, order)

    report = run_mirror(ledger, broker, [order], clock=clock)
    assert len(report.queued) == 1
    assert len(report.acks) == 1
    assert report.acks[0].venue_order_id == "5400000001"
    assert not report.halted

    # Re-running is idempotent (I3)
    report2 = run_mirror(ledger, broker, [order], clock=clock)
    assert len(report2.queued) == 0
    assert len(report2.acks) == 0

    # Venue fills order, collect_only picks up the fill
    venue.fill("5400000001", 1, "2.00")
    collected = collect_only(ledger, broker, clock=clock)
    assert len(collected.fills) == 1
    assert collected.fills[0].venue_order_id == "5400000001"
    assert collected.reconcile is not None
    assert collected.reconcile.reconciled


def test_session_native_unproven_halts_venue(ledger: Ledger):
    """Test unproven order ID halts the venue through session_native with SHA-256 command ID."""
    venue = FakeVenue()
    venue.prove_ids = False  # Venue does not return order_id
    broker, _ = _broker(venue)
    clock = DummyClock()
    order = _make_order("csp-unproven", "OPT_CSP")
    _submit(ledger, order)

    report = run_mirror(ledger, broker, [order], clock=clock)
    assert report.halted
    assert broker.halted
    assert [a.status for a in report.acks] == ["ACCEPTED"] and report.acks[0].venue_order_id is None

    # Verify VenueReconcile halt event in ledger
    halt_reconciles = [e for e in ledger.events(account=VENUE_ACCT) if e.kind == EventKind.VENUE_RECONCILE and not e.payload.reconciled]
    assert halt_reconciles, "Unproven VenueReconcile halt event must be recorded"
    halt_event = halt_reconciles[-1]
    assert "no proven venue Order ID" in (halt_event.payload.note or "")
    assert "unproven" in halt_event.command_id


def test_session_native_clear_halt_and_cancel_ticket(ledger: Ledger):
    """Test clear_halt validations and cancel_ticket through session_native."""
    clock = DummyClock()
    broker, venue = _broker(clock=clock)

    # Clear halt on unhalted venue raises MirrorSessionError
    with pytest.raises(MirrorSessionError, match="is not halted"):
        clear_halt(ledger, PM_A, reason="clean", clock=clock)

    # Halt the venue via unreadable fill read in collect_only
    venue.fill_read_raises = RuntimeError("order_events error")
    assert collect_only(ledger, broker, clock=clock).halted
    assert PM_A in halted_venues({a: ledger.state(a) for a in ledger.accounts()})

    # Empty reason refuses
    with pytest.raises(MirrorSessionError, match="operator's reason"):
        clear_halt(ledger, PM_A, reason="   ", clock=clock)

    # Still drifting / unreadable reconcile refuses to clear
    with pytest.raises(MirrorSessionError, match="still drifts"):
        clear_halt(ledger, PM_A, reason="attempted fix", clock=clock)

    # Clean the venue and collect
    venue.fill_read_raises = None
    later = DummyClock(clock.now + timedelta(minutes=2))
    broker_clean, _ = _broker(venue, clock=later)
    collected = collect_only(ledger, broker_clean, clock=later)
    assert collected.reconcile is not None and collected.reconcile.reconciled

    # Now clear halt succeeds
    cleared_holders = clear_halt(ledger, PM_A, reason="fixed by operator", clock=later)
    assert cleared_holders == (VENUE_ACCT,)
    assert PM_A not in halted_venues({a: ledger.state(a) for a in ledger.accounts()})
    [cleared_ev] = ledger.events_of_kind(EventKind.VENUE_HALT_CLEARED)
    assert cleared_ev.payload.reason == "fixed by operator"

    # Cancel ticket test
    broker_active, venue_active = _broker(clock=later)
    order2 = _make_order("csp-cancel", "OPT_CSP")
    _submit(ledger, order2)
    rep2 = run_mirror(ledger, broker_active, [order2], clock=later)
    assert len(rep2.queued) == 1
    key = rep2.queued[0].ticket_key
    ack = cancel_ticket(ledger, broker_active, key, clock=later)
    assert ack.status == "ACCEPTED"
    acks_in_ledger = [e for e in ledger.events(account=VENUE_ACCT) if e.kind == EventKind.MIRROR_ACK]
    assert any(a.payload.book_status == OrderState.CANCELLED for a in acks_in_ledger)

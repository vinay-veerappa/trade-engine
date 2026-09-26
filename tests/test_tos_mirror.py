"""T2 mirror wiring tests: fill read-back, verticals, restore from the fold, the session.

The venue below is an in-memory paperMoney behind the transport protocols: no network,
no JAB, no Schwab endpoint. Every guard has a firing and a non-firing test (§0.2).
"""

import sys
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pytest

from trade_engine.domain.instruments import Combo, ComboLeg, OptionContract, Side
from trade_engine.domain.orders import Order, OrderState, OrderType, TimeInForce
from trade_engine.interfaces.broker import UnsupportedCapability, VenueOrder, VenueOrderAllocation
from trade_engine.ledger import Ledger
from trade_engine.ledger.events import (
    EodRun,
    Event,
    EventKind,
    MirrorAck,
    MirrorQueued,
    OrdersCreated,
    OrderStateChange,
    OrderUpdated,
    VenueReconcile,
    mirror_account,
)
from trade_engine.ledger.mirror import MirrorState
from trade_engine.ledger.state import halted_venues
from trade_engine.tos_paper import normalize as norm
from trade_engine.tos_paper import session as mirror_session
from trade_engine.tos_paper.broker import (
    MirrorBinding,
    TosPaperBroker,
    TosPaperBrokerError,
    VenueUnreadable,
    venue_order_of,
)
from trade_engine.tos_paper.reconcile import confirm_ticket, ticket_contracts
from trade_engine.tos_paper.session import (
    MirrorSessionError,
    cancel_ticket,
    collect_only,
    mirror_of,
    morning_orders,
    pending_orders,
    run_mirror,
    working_orders,
)
from trade_engine.tos_paper.transport import (
    MirrorComboLeg,
    MirrorComboTicket,
    MirrorTicket,
    OrderFillReader,
    TosOrderTransport,
    ticket_for,
)

T = datetime(2026, 9, 24, 20, 0, tzinfo=timezone.utc)  # 16:00 ET, the session close
PM_A = "D-00000001"
VENUE_ACCT = mirror_account(PM_A)
P200 = OptionContract(underlying="AAPL", expiry=date(2026, 10, 16), strike=Decimal("200"), right="P")
P190 = OptionContract(underlying="AAPL", expiry=date(2026, 10, 16), strike=Decimal("190"), right="P")
SPREAD = Combo((ComboLeg(P200, 1, Side.SELL), ComboLeg(P190, 1, Side.BUY)))


class Clock:
    def __init__(self, now: datetime = T) -> None:
        self.now = now

    def now_utc(self) -> datetime:
        return self.now

    def sleep(self, seconds: float) -> None: ...


class Venue:
    """An in-memory paperMoney: rests, fills, expires and cancels by Order ID."""

    def __init__(self) -> None:
        self.orders: dict[str, dict] = {}  # Order ID -> {ticket, filled, avg, status}
        self.positions: dict[str, Decimal] = {}
        self.next_id = 5400000001
        self.placed: list[object] = []
        self.fill_rows_override: list[dict] | None = None
        self.fill_read_raises: Exception | None = None
        self.hide: set[str] = set()  # Order IDs missing from the fill read-back
        self.prove_ids = True

    def connect(self) -> dict[str, str]:
        return {"number": PM_A, "type": "margin"}

    def place_order(self, ticket, idempotency_key: str) -> dict:
        self.placed.append(ticket)
        oid = str(self.next_id)
        self.next_id += 1
        self.orders[oid] = {"ticket": ticket, "filled": 0, "avg": None, "status": "WORKING"}
        if not self.prove_ids:
            return {"status": "SENT"}  # sent, but the driver matched no Order Book row
        return {"status": "SENT", "order_id": oid, "book_status": "WORKING"}

    def cancel_order(self, order_id: str) -> dict:
        self.orders[order_id]["status"] = "CANCELED"
        return {"status": "CANCELED", "order_id": order_id}

    # venue-side events the tests drive
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

    def end(self, oid: str, status: str = "EXPIRED") -> None:
        self.orders[oid]["status"] = status

    @staticmethod
    def _legs(ticket):
        if isinstance(ticket, MirrorComboTicket):
            return [(leg.symbol, leg.side, leg.ratio) for leg in ticket.legs]
        return [(ticket.symbol, ticket.side, 1)]

    # read-backs
    def read_working_orders(self):
        rows = []
        for order in self.orders.values():
            ticket = order["ticket"]
            for symbol, side, ratio in self._legs(ticket):
                rows.append({
                    "symbol": symbol, "side": side, "quantity": str(ticket.quantity * ratio),
                    "filled": str(order["filled"] * ratio), "order_type": ticket.order_type,
                    "limit_price": None if ticket.limit_price is None else str(ticket.limit_price),
                    "status": order["status"],
                })
        return rows

    def read_positions(self):
        return [{"symbol": s, "quantity": str(q), "avg_price": "1.00"} for s, q in self.positions.items() if q]

    def read_order_fills(self):
        if self.fill_read_raises is not None:
            raise self.fill_read_raises
        if self.fill_rows_override is not None:
            return list(self.fill_rows_override)
        return [
            {"order_id": oid, "filled": str(o["filled"]), "avg_price": o["avg"], "status": o["status"]}
            for oid, o in self.orders.items() if oid not in self.hide
        ]

    def close(self) -> None: ...


class NoFillVenue:
    """A transport without the OrderFillReader capability (it can still send)."""

    def __init__(self) -> None:
        self.inner = Venue()
        self.placed = self.inner.placed

    def connect(self):
        return self.inner.connect()

    def place_order(self, ticket, idempotency_key):
        return self.inner.place_order(ticket, idempotency_key)

    def read_working_orders(self):
        return self.inner.read_working_orders()

    def read_positions(self):
        return self.inner.read_positions()

    def close(self) -> None: ...


def _binding() -> MirrorBinding:
    return MirrorBinding(venue_account=PM_A, account_type="margin",
                         mirrored_accounts=("OPT_CSP", "OPT_PUT_SPREAD"), minimum_balance=Decimal("100000"))


def _broker(venue: Venue | None = None, clock: Clock | None = None) -> tuple[TosPaperBroker, Venue]:
    venue = venue or Venue()
    broker = TosPaperBroker(venue, _binding(), clock=clock or Clock(), balance_unproven_ok=True)
    broker.connect()
    return broker, venue


def _order(oid: str, account: str = "OPT_CSP", side: Side = Side.SELL, qty: str = "1", *,
           instrument=P200, limit: str = "2.00", created: datetime = T, state=OrderState.NEW,
           parent: str | None = None, order_type=OrderType.LIMIT) -> Order:
    return Order(order_id=oid, account_id=account, instrument=instrument, order_type=order_type,
                 side=side, quantity=Decimal(qty), command_id=oid, created_at=created,
                 limit_price=Decimal(limit) if limit is not None else None, tif=TimeInForce.DAY,
                 state=state, parent_order_id=parent)


# -- normalize: the order-fill row --------------------------------------------------


@pytest.mark.parametrize(
    "raw,want",
    [
        ({"order_id": "5403527317", "filled": "1", "avg_price": "1.05", "status": "FILLED"},
         ("5403527317", Decimal("1"), Decimal("1.05"), OrderState.FILLED)),
        ({"order_id": "5403527317", "filled": "0", "avg_price": None, "status": "WORKING"},
         ("5403527317", Decimal("0"), None, OrderState.ACCEPTED)),
        ({"order_id": "5403527317", "filled": "0", "avg_price": "", "status": "EXPIRED"},
         ("5403527317", Decimal("0"), None, OrderState.EXPIRED)),
        ({"order_id": "5403527317", "filled": "0", "avg_price": "1.00", "status": "WORKING"},
         ("5403527317", Decimal("0"), None, OrderState.ACCEPTED)),
        ({"order_id": "5403527317", "filled": "2", "avg_price": "0.95", "status": "partial"},
         ("5403527317", Decimal("2"), Decimal("0.95"), OrderState.PARTIALLY_FILLED)),
        ({"order_id": "5403527317", "filled": "1", "avg_price": "1.05", "status": "WEIRD"},
         ("5403527317", Decimal("1"), Decimal("1.05"), OrderState.PENDING_UNKNOWN)),
        ({"order_id": "5403527317", "filled": "0"},
         ("5403527317", Decimal("0"), None, OrderState.PENDING_UNKNOWN)),
    ],
)
def test_order_fill_rows_normalize(raw, want) -> None:
    row = norm.normalize_order_fill(raw)
    assert (row.order_id, row.filled, row.avg_price, row.state) == want


@pytest.mark.parametrize(
    "raw,match",
    [
        ({"order_id": "54-03", "filled": "1", "avg_price": "1", "status": "FILLED"}, "all-digit"),
        ({"order_id": 5403527317, "filled": "1", "avg_price": "1", "status": "FILLED"}, "all-digit"),
        ({"filled": "1", "avg_price": "1", "status": "FILLED"}, "all-digit"),
        ({"order_id": "1", "filled": "-1", "avg_price": "1", "status": "WORKING"}, "whole non-negative"),
        ({"order_id": "1", "filled": "1.5", "avg_price": "1", "status": "WORKING"}, "whole non-negative"),
        ({"order_id": "1", "filled": 1.0, "avg_price": "1", "status": "WORKING"}, "decimal string"),
        ({"order_id": "1", "filled": "1", "avg_price": None, "status": "FILLED"}, "no positive average"),
        ({"order_id": "1", "filled": "1", "avg_price": "0", "status": "FILLED"}, "no positive average"),
        ({"order_id": "1", "filled": "0", "avg_price": None, "status": "FILLED"}, "nothing filled"),
        ({"order_id": "1", "filled": "x", "avg_price": None, "status": "WORKING"}, "not a number"),
    ],
)
def test_unreadable_order_fill_rows_raise(raw, match) -> None:
    with pytest.raises(norm.NormalizeError, match=match):
        norm.normalize_order_fill(raw)


def test_book_state_reads_known_states_and_pends_the_rest() -> None:
    assert norm.book_state("working") is OrderState.ACCEPTED
    assert norm.book_state("CANCELED") is OrderState.CANCELLED
    assert norm.book_state(None) is OrderState.PENDING_UNKNOWN
    assert norm.book_state("TRIGGERED") is OrderState.PENDING_UNKNOWN


# -- transport: the vertical ticket ------------------------------------------------


def _venue_order(instrument=SPREAD, side=Side.SELL, *, order_type=OrderType.LIMIT, limit="1.05",
                 qty="2", tif=TimeInForce.DAY) -> VenueOrder:
    return VenueOrder(venue_order_id="tos:v", instrument=instrument, order_type=order_type, side=side,
                      quantity=Decimal(qty), submitted_at=T, tif=tif,
                      limit_price=None if limit is None else Decimal(limit),
                      allocations=(VenueOrderAllocation("sp", "OPT_PUT_SPREAD", Decimal(qty)),))


def test_a_vertical_becomes_one_combo_ticket_with_an_explicit_price_effect() -> None:
    ticket = ticket_for(_venue_order())
    assert isinstance(ticket, MirrorComboTicket)
    assert ticket.underlying == "AAPL" and ticket.quantity == 2 and ticket.limit_price == Decimal("1.05")
    assert ticket.price_effect == "CREDIT" and ticket.order_type == "LMT" and ticket.tif == "DAY"
    assert [(leg.symbol, leg.side, leg.ratio) for leg in ticket.legs] == [
        (P200.to_occ(), "SELL", 1), (P190.to_occ(), "BUY", 1)]
    assert ticket_for(_venue_order(side=Side.BUY)).price_effect == "DEBIT"


def test_a_single_leg_ticket_is_unchanged() -> None:
    ticket = ticket_for(_venue_order(instrument=P200, limit="2.00", qty="1"))
    assert isinstance(ticket, MirrorTicket) and ticket.symbol == P200.to_occ() and ticket.side == "SELL"


@pytest.mark.parametrize(
    "kwargs,match",
    [
        (dict(instrument=Combo((ComboLeg(P200, 1, Side.SELL), ComboLeg(P190, 1, Side.SELL)))), "multi-leg"),
        (dict(order_type=OrderType.MARKET, limit=None), "one net LIMIT"),
        (dict(tif=TimeInForce.GTD), "TIF"),
        (dict(qty="1.5"), "whole number of units"),
    ],
)
def test_a_combo_the_venue_cannot_express_is_unsupported(kwargs, match) -> None:
    with pytest.raises(UnsupportedCapability, match=match):
        ticket_for(_venue_order(**kwargs))


@pytest.mark.parametrize(
    "changes,match",
    [
        (dict(legs=()), "vertical"),
        (dict(legs=(MirrorComboLeg("A", "SELL", 1, date(2026, 10, 16), Decimal(200), "P"),
                    MirrorComboLeg("B", "SELL", 1, date(2026, 10, 16), Decimal(190), "P"))), "one leg bought"),
        (dict(legs=(MirrorComboLeg("A", "SELL", 1, date(2026, 10, 16), Decimal(200), "P"),
                    MirrorComboLeg("B", "BUY", 2, date(2026, 10, 16), Decimal(190), "P"))), "equal ratio"),
        (dict(quantity=0), "positive int"),
        (dict(order_type="MKT"), "LMT with a positive"),
        (dict(limit_price=Decimal("0")), "LMT with a positive"),
        (dict(price_effect="EVEN"), "price_effect"),
        (dict(tif="GTD"), "tif"),
    ],
)
def test_a_combo_ticket_validates_itself(changes, match) -> None:
    base = ticket_for(_venue_order())
    fields = {**base.__dict__, **changes}
    with pytest.raises(ValueError, match=match):
        MirrorComboTicket(**fields)


@pytest.mark.parametrize(
    "changes,match", [(dict(side="HOLD"), "BUY or SELL"), (dict(ratio=0), "positive int"), (dict(ratio=True), "positive int")]
)
def test_a_combo_leg_validates_itself(changes, match) -> None:
    fields = dict(symbol="A", side="BUY", ratio=1, expiry=date(2026, 10, 16), strike=Decimal(1), right="P")
    with pytest.raises(ValueError, match=match):
        MirrorComboLeg(**{**fields, **changes})


def test_the_fake_venue_implements_the_protocols() -> None:
    assert isinstance(Venue(), TosOrderTransport) and isinstance(Venue(), OrderFillReader)
    assert isinstance(NoFillVenue(), TosOrderTransport) and not isinstance(NoFillVenue(), OrderFillReader)


# -- reconcile: confirming a vertical leg by leg -----------------------------------


def _rows(*states, limit="1.05", qty=2):
    legs = [(P200, "SELL"), (P190, "BUY")]
    return [
        norm.normalize_working_order({"symbol": c.to_occ(), "side": s, "quantity": str(qty), "filled": "0",
                                      "order_type": "LMT", "limit_price": limit, "status": st})
        for (c, s), st in zip(legs, states)
    ]


def test_a_vertical_on_the_book_leg_by_leg_is_accepted() -> None:
    claimed: set[int] = set()
    status, _ = confirm_ticket(_venue_order(), {}, [], _rows("WORKING", "WORKING"), claimed)
    assert status == "ACCEPTED" and claimed == {0, 1}


def test_a_vertical_leg_in_an_unknown_state_is_pending_and_an_ended_leg_rejects() -> None:
    assert confirm_ticket(_venue_order(), {}, [], _rows("WORKING", "HUH"), set())[0] == "PENDING"
    assert confirm_ticket(_venue_order(), {}, [], _rows("REJECTED", "REJECTED"), set())[0] == "REJECTED"
    assert confirm_ticket(_venue_order(), {}, [], _rows("FILLED", "FILLED"), set())[0] == "ACCEPTED"


def test_a_vertical_needs_a_row_for_every_leg() -> None:
    rows = _rows("WORKING", "WORKING")
    flipped = [norm.WorkingOrder(r.instrument, Side.BUY if r.side is Side.SELL else Side.SELL, r.quantity,
                                 r.filled, r.order_type, r.limit_price, r.state) for r in rows]
    assert confirm_ticket(_venue_order(), {}, [], flipped, set())[0] == "PENDING"  # legs on the wrong sides
    assert confirm_ticket(_venue_order(), {}, [], rows[:1], set())[0] == "PENDING"
    assert confirm_ticket(_venue_order(), {}, [], _rows("WORKING", "WORKING", limit="1.10"), set())[0] == "PENDING"
    assert confirm_ticket(_venue_order(), {}, [], _rows("WORKING", "WORKING", qty=1), set())[0] == "PENDING"
    claimed = {0}
    assert confirm_ticket(_venue_order(), {}, [], rows, claimed)[0] == "PENDING"  # a row another ticket claimed


def test_a_vertical_whose_every_leg_moved_is_accepted_and_one_leg_is_not_enough() -> None:
    from trade_engine.interfaces.broker import VenuePosition

    both = [VenuePosition(P200, Decimal(-2), Decimal(1), T), VenuePosition(P190, Decimal(2), Decimal(1), T)]
    assert confirm_ticket(_venue_order(), {}, both, [], set())[0] == "ACCEPTED"
    assert confirm_ticket(_venue_order(), {}, both[:1], [], set())[0] == "PENDING"


def test_ticket_contracts_is_per_leg_for_a_vertical() -> None:
    assert ticket_contracts(_venue_order()) == {P200: Decimal(-2), P190: Decimal(2)}
    assert ticket_contracts(_venue_order(instrument=P200, limit="2", qty="3")) == {P200: Decimal(-3)}
    assert ticket_contracts(_venue_order(side=Side.BUY, qty="1")) == {P200: Decimal(-1), P190: Decimal(1)}


# -- the ledger + session ----------------------------------------------------------


@pytest.fixture()
def ledger(tmp_path: Path):
    with Ledger(tmp_path / "ledger.db") as lg:
        yield lg


def _submit(ledger: Ledger, *orders: Order, session: date | None = date(2026, 9, 24), job: str = "eod") -> None:
    """What the EOD runner writes: entries created then submitted, then the EodRun marker."""
    by_account: dict[str, list[Order]] = {}
    for order in orders:
        by_account.setdefault(order.account_id, []).append(order)
    for account, batch in by_account.items():
        for order in batch:
            ledger.append(Event(account=account, kind=EventKind.ORDERS_CREATED,
                                payload=OrdersCreated(orders=(order,), fingerprint=order.order_id, reason="entry"),
                                ts_utc=T, command_id=f"create:{order.order_id}"))
            ledger.append(Event(account=account, kind=EventKind.ORDER_UPDATED,
                                payload=OrderUpdated(order=order.transition_to(OrderState.SUBMITTED), reason="submit"),
                                ts_utc=T, command_id=f"submit:{order.order_id}"))
    if session is not None:
        for account in _binding().mirrored_accounts:
            _marker(ledger, account, session, job)


def _marker(ledger: Ledger, account: str, session: date, job: str = "eod") -> None:
    ledger.append(Event(account=account, kind=EventKind.EOD_RUN,
                        payload=EodRun(session=session, job=job, account_id=account, bars_processed=1, at_close=T),
                        ts_utc=T, command_id=f"eod:{job}:{account}:{session.isoformat()}"))


class Calendar:
    def session_close(self, d: date) -> datetime:
        return datetime(d.year, d.month, d.day, 20, 0, tzinfo=timezone.utc)


def _mirror(ledger: Ledger) -> MirrorState:
    return mirror_of(ledger, PM_A)


def _kinds(ledger: Ledger) -> list[str]:
    return [e.kind.value for e in ledger.events(account=VENUE_ACCT)]


MORNING = Clock(datetime(2026, 9, 25, 14, 0, tzinfo=timezone.utc))


def test_pending_orders_takes_the_sessions_entries_that_the_venue_has_not_handled(ledger) -> None:
    _submit(ledger, _order("csp-1"), _order("sp-1", "OPT_PUT_SPREAD", instrument=SPREAD, limit="1.05"),
            _order("exit", parent="csp-0"), _order("early", created=T - timedelta(minutes=1)))
    ids = [o.order_id for o in pending_orders(ledger, _binding(), date(2026, 9, 24), calendar=Calendar())]
    assert ids == ["csp-1", "sp-1"]
    assert all(o.state is OrderState.SUBMITTED for o in
               pending_orders(ledger, _binding(), date(2026, 9, 24), calendar=Calendar()))


def test_pending_orders_refuses_a_session_without_its_eod_marker(ledger) -> None:
    _submit(ledger, _order("csp-1"), session=None)
    _marker(ledger, "OPT_CSP", date(2026, 9, 24))
    with pytest.raises(MirrorSessionError, match="'OPT_PUT_SPREAD' has no EOD run"):
        pending_orders(ledger, _binding(), date(2026, 9, 24), calendar=Calendar())
    _marker(ledger, "OPT_PUT_SPREAD", date(2026, 9, 24))
    assert len(pending_orders(ledger, _binding(), date(2026, 9, 24), calendar=Calendar())) == 1


def test_pending_orders_refuses_two_jobs_unless_one_is_named(ledger) -> None:
    _submit(ledger, _order("csp-1"))
    for account in _binding().mirrored_accounts:
        _marker(ledger, account, date(2026, 9, 24), job="other")
    with pytest.raises(MirrorSessionError, match="name the job"):
        pending_orders(ledger, _binding(), date(2026, 9, 24), calendar=Calendar())
    assert len(pending_orders(ledger, _binding(), date(2026, 9, 24), calendar=Calendar(), job="eod")) == 1
    with pytest.raises(MirrorSessionError, match=r"\(job nope\)"):
        pending_orders(ledger, _binding(), date(2026, 9, 24), calendar=Calendar(), job="nope")


def test_pending_orders_ignores_entries_created_after_the_marker(ledger) -> None:
    _submit(ledger, _order("csp-1"))
    _submit(ledger, _order("late"), session=None)  # after the marker: the next batch's
    _submit(ledger, _order("exit", parent="csp-1"), session=None)  # an exit is never mirrored
    ids = [o.order_id for o in pending_orders(ledger, _binding(), date(2026, 9, 24), calendar=Calendar())]
    assert ids == ["csp-1"]
    assert [o.order_id for o in working_orders(ledger, _binding())] == ["csp-1", "late"]


def test_pending_orders_skips_orders_the_sim_no_longer_works(ledger) -> None:
    _submit(ledger, _order("csp-1"), _order("csp-2"))
    ledger.append(Event(account="OPT_CSP", kind=EventKind.ORDER_CANCELLED,
                        payload=OrderStateChange("csp-2", "strategy cancelled"), ts_utc=T))
    ids = [o.order_id for o in pending_orders(ledger, _binding(), date(2026, 9, 24), calendar=Calendar())]
    assert ids == ["csp-1"]


def test_run_mirror_queues_ahead_sends_and_acks_with_the_proven_order_id(ledger) -> None:
    _submit(ledger, _order("csp-1"), _order("sp-1", "OPT_PUT_SPREAD", instrument=SPREAD, limit="1.05"))
    broker, venue = _broker(clock=MORNING)
    orders = pending_orders(ledger, _binding(), date(2026, 9, 24), calendar=Calendar())
    report = run_mirror(ledger, broker, orders, clock=MORNING)
    assert not report.halted and report.reconcile.reconciled and report.drain_reconcile.reconciled
    assert len(report.queued) == 2 and report.refused == () and {a.status for a in report.acks} == {"ACCEPTED"}
    assert isinstance(venue.placed[1], MirrorComboTicket) and venue.placed[1].price_effect == "CREDIT"
    kinds = _kinds(ledger)
    assert kinds.index("MirrorQueued") < kinds.index("MirrorAck")  # written ahead of the send
    mirror = _mirror(ledger)
    assert sorted(t.venue_order_id for t in mirror.tickets.values()) == ["5400000001", "5400000002"]
    assert mirror.expected() == {P200: Decimal(-2), P190: Decimal(1)}
    assert dict(mirror.book) == {}  # nothing filled yet
    assert ledger.state("OPT_CSP").mirror == MirrorState()


def test_run_mirror_twice_is_idempotent(ledger) -> None:
    _submit(ledger, _order("csp-1"))
    broker, venue = _broker(clock=MORNING)
    orders = pending_orders(ledger, _binding(), date(2026, 9, 24), calendar=Calendar())
    run_mirror(ledger, broker, orders, clock=MORNING)
    count = ledger.count()
    again = run_mirror(ledger, broker, orders, clock=MORNING)
    assert len(venue.placed) == 1 and again.queued == () and again.acks == ()
    assert ledger.count() == count  # the same collect and reconcile, same clock: replays (I3)
    assert pending_orders(ledger, _binding(), date(2026, 9, 24), calendar=Calendar()) == ()
    later = Clock(MORNING.now + timedelta(hours=1))
    fresh, _ = _broker(venue, clock=later)  # a restart: the fold is the memory
    assert run_mirror(ledger, fresh, orders, clock=later).queued == () and len(venue.placed) == 1


def test_a_repeated_order_in_the_input_is_mirrored_once(ledger) -> None:
    _submit(ledger, _order("csp-1"))
    broker, venue = _broker(clock=MORNING)
    order = ledger.state("OPT_CSP").orders["csp-1"]
    report = run_mirror(ledger, broker, [order, order], clock=MORNING)
    assert len(report.queued) == 1 and report.refused == () and len(venue.placed) == 1


def test_a_fill_is_booked_per_leg_and_the_reconcile_stays_clean(ledger) -> None:
    _submit(ledger, _order("sp-1", "OPT_PUT_SPREAD", qty="2", instrument=SPREAD, limit="1.05"))
    broker, venue = _broker(clock=MORNING)
    run_mirror(ledger, broker, pending_orders(ledger, _binding(), date(2026, 9, 24), calendar=Calendar()), clock=MORNING)
    venue.fill("5400000001", 1, "1.05")
    noon = Clock(MORNING.now + timedelta(hours=2))
    first = collect_only(ledger, _broker(venue, clock=noon)[0], clock=noon)
    assert [f.filled for f in first.fills] == [1] and first.reconcile.reconciled and not first.halted
    venue.fill("5400000001", 2, "1.04")
    close = Clock(datetime(2026, 9, 25, 20, 5, tzinfo=timezone.utc))
    second = collect_only(ledger, _broker(venue, clock=close)[0], clock=close)
    assert [f.filled for f in second.fills] == [2] and [c.book_status for c in second.closes] == [OrderState.FILLED]
    assert second.reconcile.reconciled
    mirror = _mirror(ledger)
    assert dict(mirror.book) == {("OPT_PUT_SPREAD", P200): Decimal(-2), ("OPT_PUT_SPREAD", P190): Decimal(2)}
    assert mirror.open_tickets == ()
    count = ledger.count()
    third = collect_only(ledger, _broker(venue, clock=close)[0], clock=close)
    assert third.fills == () and third.closes == () and ledger.count() == count


def test_a_day_ticket_that_expired_unfilled_is_no_drift(ledger) -> None:
    _submit(ledger, _order("csp-1"))
    broker, venue = _broker(clock=MORNING)
    run_mirror(ledger, broker, pending_orders(ledger, _binding(), date(2026, 9, 24), calendar=Calendar()), clock=MORNING)
    venue.end("5400000001", "EXPIRED")
    close = Clock(datetime(2026, 9, 25, 20, 5, tzinfo=timezone.utc))
    report = collect_only(ledger, _broker(venue, clock=close)[0], clock=close)
    assert [c.book_status for c in report.closes] == [OrderState.EXPIRED] and report.fills == ()
    assert report.reconcile.reconciled and not report.halted
    assert _mirror(ledger).expected() == {} and _mirror(ledger).tickets["tos:" + _key(ledger)].closed


def _key(ledger: Ledger) -> str:
    (key,) = _mirror(ledger).tickets
    return key.removeprefix("tos:")


def test_a_ticket_whose_row_vanished_unrecorded_halts(ledger) -> None:
    _submit(ledger, _order("csp-1"))
    broker, venue = _broker(clock=MORNING)
    run_mirror(ledger, broker, pending_orders(ledger, _binding(), date(2026, 9, 24), calendar=Calendar()), clock=MORNING)
    venue.orders.clear()  # the next day: yesterday's expired DAY row left the Order Book
    tomorrow = Clock(MORNING.now + timedelta(days=1))
    report = collect_only(ledger, _broker(venue, clock=tomorrow)[0], clock=tomorrow)
    assert report.halted and not report.reconcile.reconciled and P200.symbol in report.reconcile.drift
    assert PM_A in halted_venues({a: ledger.state(a) for a in ledger.accounts()})


def test_a_partial_fill_then_a_cancel_books_the_partial(ledger) -> None:
    _submit(ledger, _order("csp-1", qty="3"))
    broker, venue = _broker(clock=MORNING)
    run_mirror(ledger, broker, pending_orders(ledger, _binding(), date(2026, 9, 24), calendar=Calendar()), clock=MORNING)
    venue.fill("5400000001", 1, "2.00")
    venue.end("5400000001", "CANCELED")
    report = collect_only(ledger, _broker(venue, clock=MORNING)[0], clock=MORNING)
    assert [f.filled for f in report.fills] == [1] and [c.book_status for c in report.closes] == [OrderState.CANCELLED]
    assert report.reconcile.reconciled and _mirror(ledger).expected() == {P200: Decimal(-1)}


def test_a_restarted_broker_can_cancel_by_the_restored_order_id(ledger) -> None:
    _submit(ledger, _order("csp-1"))
    broker, venue = _broker(clock=MORNING)
    run_mirror(ledger, broker, pending_orders(ledger, _binding(), date(2026, 9, 24), calendar=Calendar()), clock=MORNING)
    fresh, _ = _broker(venue, clock=MORNING)
    (key,) = _mirror(ledger).tickets
    assert fresh.cancel(key).status == "REJECTED"  # nothing restored yet: no proven id
    fresh.restore(_mirror(ledger))
    ack = fresh.cancel(key)
    assert ack.status == "ACCEPTED" and venue.orders["5400000001"]["status"] == "CANCELED"
    assert fresh.reconcile_now().reconciled


def test_the_mirror_book_is_the_holdings_for_conflict_screening(ledger) -> None:
    _submit(ledger, _order("csp-1"))
    broker, venue = _broker(clock=MORNING)
    run_mirror(ledger, broker, pending_orders(ledger, _binding(), date(2026, 9, 24), calendar=Calendar()), clock=MORNING)
    venue.fill("5400000001", 1, "2.00")
    _submit(ledger, _order("sp-buy", "OPT_PUT_SPREAD", Side.BUY, created=T + timedelta(days=1)),
            session=date(2026, 9, 25))
    later = Clock(datetime(2026, 9, 28, 14, 0, tzinfo=timezone.utc))
    orders = pending_orders(ledger, _binding(), date(2026, 9, 25), calendar=Calendar())
    report = run_mirror(ledger, _broker(venue, clock=later)[0], orders, clock=later)
    assert [f.filled for f in report.fills] == [1]
    assert [r.strategy_order_id for r in report.refused] == ["sp-buy"] and "opposite side" in report.refused[0].reason
    assert _mirror(ledger).refused_orders.keys() == {"sp-buy"} and len(venue.placed) == 1


def test_a_halted_venue_refuses_every_pending_order_with_the_reason(ledger) -> None:
    ledger.append(Event(account=VENUE_ACCT, kind=EventKind.VENUE_RECONCILE,
                        payload=VenueReconcile(venue=PM_A, as_of=T, reconciled=False, drift=("X",)), ts_utc=T))
    _submit(ledger, _order("csp-1"))
    broker, venue = _broker(clock=MORNING)
    report = run_mirror(ledger, broker, pending_orders(ledger, _binding(), date(2026, 9, 24), calendar=Calendar()),
                        clock=MORNING)
    assert report.halted and report.queued == () and venue.placed == []
    assert [r.strategy_order_id for r in report.refused] == ["csp-1"] and "halted" in report.refused[0].reason
    assert pending_orders(ledger, _binding(), date(2026, 9, 24), calendar=Calendar()) == ()  # recorded (I11)


def test_a_transport_without_fill_reads_refuses_the_session(ledger) -> None:
    _submit(ledger, _order("csp-1"))
    broker, venue = _broker(NoFillVenue(), clock=MORNING)
    with pytest.raises(TosPaperBrokerError, match="OrderFillReader"):
        run_mirror(ledger, broker, [], clock=MORNING)
    assert venue.placed == []


def test_an_unreadable_fill_read_halts_and_sends_nothing(ledger) -> None:
    _submit(ledger, _order("csp-1"))
    venue = Venue()
    venue.fill_read_raises = RuntimeError("JAB timeout")
    broker, _ = _broker(venue, clock=MORNING)
    report = run_mirror(ledger, broker, pending_orders(ledger, _binding(), date(2026, 9, 24), calendar=Calendar()),
                        clock=MORNING)
    assert report.halted and "fill read-back failed" in report.reconcile.note and venue.placed == []
    assert [r.strategy_order_id for r in report.refused] == ["csp-1"]


def test_a_fill_the_fold_refuses_halts_the_venue(ledger, monkeypatch) -> None:
    _submit(ledger, _order("csp-1"))
    broker, venue = _broker(clock=MORNING)
    run_mirror(ledger, broker, pending_orders(ledger, _binding(), date(2026, 9, 24), calendar=Calendar()), clock=MORNING)
    from trade_engine.ledger.events import MirrorFill
    from trade_engine.tos_paper.broker import FillCollection

    def contradict(self, mirror):  # a broker bug: a fill naming another Order ID
        (key,) = mirror.tickets
        return FillCollection(fills=(MirrorFill(venue=PM_A, ticket_key=key, venue_order_id="5400000009",
                                                filled=Decimal(1), avg_price=Decimal("2.10"), at=MORNING.now),), closes=())

    monkeypatch.setattr(TosPaperBroker, "collect_fills", contradict)
    fresh, _ = _broker(venue, clock=MORNING)
    report = collect_only(ledger, fresh, clock=MORNING)
    assert report.halted and fresh.halted and "cannot book" in report.reconcile.note
    assert P200.symbol in report.reconcile.drift and report.fills == ()
    assert dict(_mirror(ledger).book) == {}
    assert PM_A in halted_venues({a: ledger.state(a) for a in ledger.accounts()})


def test_the_same_cumulative_at_another_price_is_a_contradiction(ledger) -> None:
    _submit(ledger, _order("csp-1", qty="2"))
    broker, venue = _broker(clock=MORNING)
    run_mirror(ledger, broker, pending_orders(ledger, _binding(), date(2026, 9, 24), calendar=Calendar()), clock=MORNING)
    venue.fill("5400000001", 1, "2.00")
    collect_only(ledger, _broker(venue, clock=MORNING)[0], clock=MORNING)
    venue.fill_rows_override = [{"order_id": "5400000001", "filled": "1", "avg_price": "2.0", "status": "WORKING"}]
    assert _broker(venue, clock=MORNING)[0].collect_fills(_mirror(ledger)).fills == ()  # 2.0 == 2.00
    venue.fill_rows_override = [{"order_id": "5400000001", "filled": "1", "avg_price": "2.10", "status": "WORKING"}]
    with pytest.raises(VenueUnreadable, match="booked it at 2.00"):
        _broker(venue, clock=MORNING)[0].collect_fills(_mirror(ledger))


def test_a_drift_at_the_instant_of_a_clean_reconcile_is_still_recorded(ledger) -> None:
    _submit(ledger, _order("csp-1"))
    broker, venue = _broker(clock=MORNING)
    run_mirror(ledger, broker, pending_orders(ledger, _binding(), date(2026, 9, 24), calendar=Calendar()), clock=MORNING)
    venue.orders.clear()
    report = collect_only(ledger, _broker(venue, clock=MORNING)[0], clock=MORNING)  # the same instant
    assert not report.reconcile.reconciled
    assert PM_A in halted_venues({a: ledger.state(a) for a in ledger.accounts()})


def test_a_failed_write_ahead_sends_nothing(ledger, monkeypatch) -> None:
    _submit(ledger, _order("csp-1"))
    broker, venue = _broker(clock=MORNING)
    orders = pending_orders(ledger, _binding(), date(2026, 9, 24), calendar=Calendar())
    real = ledger.extend

    def failing(events):
        events = list(events)
        if any(e.kind is EventKind.MIRROR_QUEUED for e in events):
            raise OSError("disk full")
        return real(events)

    monkeypatch.setattr(ledger, "extend", failing)
    with pytest.raises(OSError, match="disk full"):
        run_mirror(ledger, broker, orders, clock=MORNING)
    assert venue.placed == [] and broker.queued == ()


# -- the broker's restore and collect ------------------------------------------------


def _queued_ticket(ledger: Ledger) -> MirrorQueued:
    return next(e.payload for e in ledger.events_of_kind(EventKind.MIRROR_QUEUED))


def test_restore_refuses_another_venue_and_an_undrained_queue() -> None:
    broker, _ = _broker()
    with pytest.raises(TosPaperBrokerError, match="I8"):
        broker.restore(MirrorState(venue="D-00000002"))
    broker.restore(MirrorState(venue=PM_A))
    broker.mirror_batch([_order("csp-1")], holdings={})
    with pytest.raises(TosPaperBrokerError, match="undrained queue"):
        broker.restore(MirrorState(venue=PM_A))


def test_restore_keeps_a_halt_and_never_clears_one() -> None:
    broker, _ = _broker()
    broker.restore(MirrorState(), halted_venues={"D-00000002"})
    assert not broker.halted
    broker.restore(MirrorState(), halted_venues={PM_A})
    assert broker.halted
    broker.restore(MirrorState())
    assert broker.halted


def test_a_restored_ticket_is_never_queued_again(ledger) -> None:
    _submit(ledger, _order("csp-1"))
    broker, venue = _broker(clock=MORNING)
    orders = pending_orders(ledger, _binding(), date(2026, 9, 24), calendar=Calendar())
    run_mirror(ledger, broker, orders, clock=MORNING)
    fresh, _ = _broker(venue, clock=MORNING)
    fresh.restore(_mirror(ledger))
    fresh.mirror_batch(orders, holdings={})
    assert fresh.queued == ()


def test_collect_fills_ignores_order_ids_it_does_not_know_and_missing_rows(ledger) -> None:
    _submit(ledger, _order("csp-1"))
    broker, venue = _broker(clock=MORNING)
    run_mirror(ledger, broker, pending_orders(ledger, _binding(), date(2026, 9, 24), calendar=Calendar()), clock=MORNING)
    venue.fill_rows_override = [{"order_id": "9999999999", "filled": "5", "avg_price": "1", "status": "FILLED"}]
    fresh, _ = _broker(venue, clock=MORNING)
    assert fresh.collect_fills(_mirror(ledger)).fills == ()
    venue.fill_rows_override = []
    assert fresh.collect_fills(_mirror(ledger)).fills == () and not fresh.halted


def test_collect_fills_skips_tickets_with_no_proven_order_id(ledger) -> None:
    _submit(ledger, _order("csp-1"))
    broker, venue = _broker(clock=MORNING)
    orders = pending_orders(ledger, _binding(), date(2026, 9, 24), calendar=Calendar())
    run_mirror(ledger, broker, orders, clock=MORNING)
    key = _queued_ticket(ledger).ticket_key
    mirror = _mirror(ledger)
    from dataclasses import replace

    unproven = replace(mirror, tickets={key: replace(mirror.tickets[key], venue_order_id=None)})
    venue.fill("5400000001", 1, "2.00")
    assert _broker(venue, clock=MORNING)[0].collect_fills(unproven).fills == ()


@pytest.mark.parametrize(
    "rows,match",
    [
        ([{"order_id": "5400000001", "filled": "0", "avg_price": None, "status": "WORKING"}] * 2, "two fill rows"),
        ([{"order_id": "5400000001", "filled": "1.5", "avg_price": "1", "status": "WORKING"}], "fill read-back failed"),
        ([{"order_id": "5400000001", "filled": "2", "avg_price": "2", "status": "FILLED"}], "reads filled 2"),
        ([{"order_id": "5400000001", "filled": "1", "avg_price": "2", "status": "FILLED"}], "FILLED at 1 of 3"),
    ],
)
def test_collect_fills_refuses_what_it_cannot_read_or_what_contradicts_the_fold(ledger, rows, match) -> None:
    _submit(ledger, _order("csp-1", qty="3") if "FILLED at" in match else _order("csp-1"))
    broker, venue = _broker(clock=MORNING)
    run_mirror(ledger, broker, pending_orders(ledger, _binding(), date(2026, 9, 24), calendar=Calendar()), clock=MORNING)
    venue.fill_rows_override = rows
    fresh, _ = _broker(venue, clock=MORNING)
    with pytest.raises(VenueUnreadable, match=match) as raised:
        fresh.collect_fills(_mirror(ledger))
    assert fresh.halted and not raised.value.reconcile.reconciled


def test_collect_fills_refuses_a_cumulative_lower_than_recorded(ledger) -> None:
    _submit(ledger, _order("csp-1", qty="2"))
    broker, venue = _broker(clock=MORNING)
    run_mirror(ledger, broker, pending_orders(ledger, _binding(), date(2026, 9, 24), calendar=Calendar()), clock=MORNING)
    venue.fill("5400000001", 1, "2.00")
    collect_only(ledger, _broker(venue, clock=MORNING)[0], clock=MORNING)
    venue.fill_rows_override = [{"order_id": "5400000001", "filled": "0", "avg_price": None, "status": "WORKING"}]
    with pytest.raises(VenueUnreadable, match="the mirror has 1 of 2"):
        _broker(venue, clock=MORNING)[0].collect_fills(_mirror(ledger))
    venue.fill_rows_override = [{"order_id": "5400000001", "filled": "1", "avg_price": "2.00", "status": "WORKING"}]
    assert _broker(venue, clock=MORNING)[0].collect_fills(_mirror(ledger)).fills == ()


def test_collect_fills_closes_a_ticket_once_and_a_rejected_row_rejects(ledger) -> None:
    _submit(ledger, _order("csp-1"))
    broker, venue = _broker(clock=MORNING)
    run_mirror(ledger, broker, pending_orders(ledger, _binding(), date(2026, 9, 24), calendar=Calendar()), clock=MORNING)
    venue.end("5400000001", "REJECTED")
    closes = _broker(venue, clock=MORNING)[0].collect_fills(_mirror(ledger)).closes
    assert [(c.status, c.book_status) for c in closes] == [("REJECTED", OrderState.REJECTED)]
    venue.end("5400000001", "WORKING")
    assert _broker(venue, clock=MORNING)[0].collect_fills(_mirror(ledger)).closes == ()


def test_collect_fills_before_connect_or_for_another_venue_refuses() -> None:
    venue = Venue()
    broker = TosPaperBroker(venue, _binding(), clock=Clock(), balance_unproven_ok=True)
    with pytest.raises(TosPaperBrokerError, match="prove the venue first"):
        broker.collect_fills(MirrorState())
    broker.connect()
    with pytest.raises(TosPaperBrokerError, match="I8"):
        broker.collect_fills(MirrorState(venue="D-00000002"))


def test_venue_order_of_rebuilds_the_queued_ticket(ledger) -> None:
    _submit(ledger, _order("sp-1", "OPT_PUT_SPREAD", qty="2", instrument=SPREAD, limit="1.05"))
    broker, _ = _broker(clock=MORNING)
    batch = broker.mirror_batch(pending_orders(ledger, _binding(), date(2026, 9, 24), calendar=Calendar()), holdings={})
    (ticket,) = batch.venue_orders
    queued = mirror_session._queued(PM_A, ticket)
    assert venue_order_of(queued) == ticket


def test_no_real_paper_money_account_number_appears_here() -> None:
    text = Path(__file__).read_text(encoding="utf-8") + Path(mirror_session.__file__).read_text(encoding="utf-8")
    for real in ("68295" + "005", "68295" + "006"):
        assert real not in text


def test_a_second_session_expects_the_first_sessions_resting_ticket(ledger) -> None:
    _submit(ledger, _order("csp-1"))
    broker, venue = _broker(clock=MORNING)
    run_mirror(ledger, broker, pending_orders(ledger, _binding(), date(2026, 9, 24), calendar=Calendar()), clock=MORNING)
    _submit(ledger, _order("csp-2", instrument=P190, created=T + timedelta(days=1)), session=date(2026, 9, 25))
    later = Clock(MORNING.now + timedelta(hours=3))
    orders = pending_orders(ledger, _binding(), date(2026, 9, 25), calendar=Calendar())
    report = run_mirror(ledger, _broker(venue, clock=later)[0], orders, clock=later)
    assert len(report.queued) == 1 and report.drain_reconcile.reconciled and not report.halted


def test_an_order_already_queued_is_not_netted_again_with_a_new_one(ledger) -> None:
    _submit(ledger, _order("csp-1"))
    broker, venue = _broker(clock=MORNING)
    first = ledger.state("OPT_CSP").orders["csp-1"]
    run_mirror(ledger, broker, [first], clock=MORNING)
    _submit(ledger, _order("csp-2", created=T + timedelta(days=1)), session=date(2026, 9, 25))
    second = ledger.state("OPT_CSP").orders["csp-2"]
    later = Clock(MORNING.now + timedelta(hours=1))
    report = run_mirror(ledger, _broker(venue, clock=later)[0], [first, second], clock=later)
    assert [[a.strategy_order_id for a in q.allocations] for q in report.queued] == [["csp-2"]]


def test_a_retry_after_a_failed_write_ahead_refuses_rather_than_drops(ledger, monkeypatch) -> None:
    _submit(ledger, _order("csp-1"))
    broker, venue = _broker(clock=MORNING)
    orders = pending_orders(ledger, _binding(), date(2026, 9, 24), calendar=Calendar())
    real = ledger.extend
    failures = [OSError("disk full")]

    def failing_once(events):
        events = list(events)
        if failures and any(e.kind is EventKind.MIRROR_QUEUED for e in events):
            raise failures.pop()
        return real(events)

    monkeypatch.setattr(ledger, "extend", failing_once)
    with pytest.raises(OSError):
        run_mirror(ledger, broker, orders, clock=MORNING)
    report = run_mirror(ledger, broker, orders, clock=MORNING)  # the same process retries
    assert report.queued == () and venue.placed == []
    assert [r.strategy_order_id for r in report.refused] == ["csp-1"] and "never recorded" in report.refused[0].reason
    assert pending_orders(ledger, _binding(), date(2026, 9, 24), calendar=Calendar()) == ()


def test_a_closed_ticket_is_not_closed_again_by_a_later_row_state(ledger) -> None:
    _submit(ledger, _order("csp-1"))
    broker, venue = _broker(clock=MORNING)
    run_mirror(ledger, broker, pending_orders(ledger, _binding(), date(2026, 9, 24), calendar=Calendar()), clock=MORNING)
    venue.end("5400000001", "EXPIRED")
    collect_only(ledger, _broker(venue, clock=MORNING)[0], clock=MORNING)
    venue.end("5400000001", "CANCELED")
    assert _broker(venue, clock=MORNING)[0].collect_fills(_mirror(ledger)).closes == ()


# -- review fixes: open tickets screen, remainder-only cancels, unproven sends halt ----


def _mirror_first(ledger, *orders, clock=MORNING):
    _submit(ledger, *orders)
    broker, venue = _broker(clock=clock)
    report = run_mirror(ledger, broker, pending_orders(ledger, _binding(), date(2026, 9, 24), calendar=Calendar()),
                        clock=clock)
    return broker, venue, report


def _next_batch(ledger, venue, *orders, clock):
    _submit(ledger, *orders, session=date(2026, 9, 25))
    pending = pending_orders(ledger, _binding(), date(2026, 9, 25), calendar=Calendar())
    return run_mirror(ledger, _broker(venue, clock=clock)[0], pending, clock=clock)


LATER = Clock(MORNING.now + timedelta(hours=2))


def test_a_new_order_opposing_a_resting_ticket_of_another_account_is_refused(ledger) -> None:
    _, venue, _ = _mirror_first(ledger, _order("csp-1"))  # SELL 1 P200 rests unfilled
    report = _next_batch(ledger, venue, _order("sp-buy", "OPT_PUT_SPREAD", Side.BUY, created=T + timedelta(days=1)),
                         clock=LATER)
    assert [r.strategy_order_id for r in report.refused] == ["sp-buy"] and "opposite side of OPT_CSP" in report.refused[0].reason
    assert report.queued == () and len(venue.placed) == 1 and not report.halted


def test_a_new_order_on_the_same_side_as_a_resting_ticket_is_mirrored_and_expected_once(ledger) -> None:
    _, venue, _ = _mirror_first(ledger, _order("csp-1"))
    report = _next_batch(ledger, venue, _order("sp-sell", "OPT_PUT_SPREAD", Side.SELL, created=T + timedelta(days=1)),
                         clock=LATER)
    assert report.refused == () and len(report.queued) == 1
    assert report.drain_reconcile.reconciled and not report.halted  # the resting ticket is not counted twice
    assert _mirror(ledger).expected() == {P200: Decimal(-2)}


def test_a_filled_part_of_a_ticket_screens_through_the_book_and_the_rest_as_resting(ledger) -> None:
    _, venue, _ = _mirror_first(ledger, _order("csp-1", qty="3"))
    venue.fill("5400000001", 1, "2.00")
    assert _mirror(ledger).exposure() == {("OPT_CSP", P200): Decimal(-3)}
    collect_only(ledger, _broker(venue, clock=MORNING)[0], clock=MORNING)
    assert _mirror(ledger).exposure() == {("OPT_CSP", P200): Decimal(-3)}  # 1 booked + 2 resting
    assert dict(_mirror(ledger).book) == {("OPT_CSP", P200): Decimal(-1)}


def test_a_closed_ticket_no_longer_screens(ledger) -> None:
    _, venue, _ = _mirror_first(ledger, _order("csp-1"))
    venue.end("5400000001", "EXPIRED")
    collect_only(ledger, _broker(venue, clock=MORNING)[0], clock=MORNING)
    assert _mirror(ledger).exposure() == {}
    report = _next_batch(ledger, venue, _order("sp-buy", "OPT_PUT_SPREAD", Side.BUY, created=T + timedelta(days=1)),
                         clock=LATER)
    assert report.refused == () and len(report.queued) == 1 and report.drain_reconcile.reconciled


def test_cancelling_a_restored_partly_filled_ticket_takes_out_only_the_remainder(ledger) -> None:
    _, venue, _ = _mirror_first(ledger, _order("csp-1", qty="3"))
    venue.fill("5400000001", 1, "2.00")
    collect_only(ledger, _broker(venue, clock=MORNING)[0], clock=MORNING)
    (key,) = _mirror(ledger).tickets
    fresh, _ = _broker(venue, clock=LATER)
    fresh.restore(_mirror(ledger))
    assert fresh.cancel(key).status == "ACCEPTED"
    check = fresh.reconcile_now()
    assert check.reconciled, check  # expected -1 (the booked fill), the venue holds -1


def test_cancel_ticket_records_the_cancel_so_it_survives_a_restart(ledger) -> None:
    _, venue, _ = _mirror_first(ledger, _order("csp-1", qty="3"))
    venue.fill("5400000001", 1, "2.00")
    collect_only(ledger, _broker(venue, clock=MORNING)[0], clock=MORNING)
    (key,) = _mirror(ledger).tickets
    ack = cancel_ticket(ledger, _broker(venue, clock=LATER)[0], key, clock=LATER)
    assert ack.status == "ACCEPTED" and venue.orders["5400000001"]["status"] == "CANCELED"
    ticket = _mirror(ledger).tickets[key]
    assert ticket.closed and ticket.book_status is OrderState.CANCELLED and ticket.venue_order_id == "5400000001"
    assert _mirror(ledger).expected() == {P200: Decimal(-1)}
    del venue.orders["5400000001"]  # the next day the CANCELED row is gone
    tomorrow = Clock(LATER.now + timedelta(days=1))
    report = collect_only(ledger, _broker(venue, clock=tomorrow)[0], clock=tomorrow)
    assert report.reconcile.reconciled and not report.halted
    count = ledger.count()
    again = cancel_ticket(ledger, _broker(venue, clock=tomorrow)[0], key, clock=tomorrow)
    assert again.status == "ACCEPTED" and again.message == "already cancelled" and ledger.count() == count  # idempotent


def test_a_cancel_the_venue_did_not_confirm_is_not_recorded(ledger) -> None:
    _, venue, _ = _mirror_first(ledger, _order("csp-1"))
    (key,) = _mirror(ledger).tickets
    venue.cancel_order = lambda order_id: {"status": "UNKNOWN", "order_id": order_id, "note": "menu missing"}
    count = ledger.count()
    ack = cancel_ticket(ledger, _broker(venue, clock=LATER)[0], key, clock=LATER)
    assert ack.status == "PENDING" and ledger.count() == count and not _mirror(ledger).tickets[key].closed
    assert cancel_ticket(ledger, _broker(venue, clock=LATER)[0], "tos:unknown", clock=LATER).status == "REJECTED"
    assert ledger.count() == count


def test_a_send_that_proved_no_order_id_halts_the_venue(ledger) -> None:
    _submit(ledger, _order("csp-1"))
    venue = Venue()
    venue.prove_ids = False
    broker, _ = _broker(venue, clock=MORNING)
    report = run_mirror(ledger, broker, pending_orders(ledger, _binding(), date(2026, 9, 24), calendar=Calendar()),
                        clock=MORNING)
    assert [a.status for a in report.acks] == ["ACCEPTED"] and report.acks[0].venue_order_id is None
    assert report.halted and broker.halted
    assert PM_A in halted_venues({a: ledger.state(a) for a in ledger.accounts()})
    notes = [e.payload.note for e in ledger.events_of_kind(EventKind.VENUE_RECONCILE) if not e.payload.reconciled]
    assert any("no proven venue Order ID" in n for n in notes)


def test_a_crash_between_the_drain_and_the_ack_append_halts_the_next_session(ledger, monkeypatch) -> None:
    _submit(ledger, _order("csp-1"))
    broker, venue = _broker(clock=MORNING)
    real = ledger.extend

    def crash_on_acks(events):
        events = list(events)
        if any(e.kind is EventKind.MIRROR_ACK for e in events):
            raise SystemError("process killed")
        return real(events)

    monkeypatch.setattr(ledger, "extend", crash_on_acks)
    with pytest.raises(SystemError):
        run_mirror(ledger, broker, pending_orders(ledger, _binding(), date(2026, 9, 24), calendar=Calendar()),
                   clock=MORNING)
    monkeypatch.setattr(ledger, "extend", real)
    venue.fill("5400000001", 1, "2.00")  # the venue fills it; the mirror could never see the fill
    report = collect_only(ledger, _broker(venue, clock=LATER)[0], clock=LATER)
    assert report.halted and len(venue.placed) == 1
    assert PM_A in halted_venues({a: ledger.state(a) for a in ledger.accounts()})


def test_proven_sends_do_not_halt(ledger) -> None:
    _, venue, report = _mirror_first(ledger, _order("csp-1"))
    assert not report.halted and report.acks[0].venue_order_id == "5400000001"
    later = collect_only(ledger, _broker(venue, clock=LATER)[0], clock=LATER)
    assert not later.halted and later.reconcile.reconciled


def test_after_restore_the_expectation_is_the_folds_whatever_holdings_screen(ledger) -> None:
    _, venue, _ = _mirror_first(ledger, _order("csp-1"))  # SELL 1 P200 rests at the venue
    fresh, _ = _broker(venue, clock=LATER)
    fresh.restore(_mirror(ledger))
    fresh.mirror_batch([_order("csp-2", created=T + timedelta(days=1))], holdings={})
    fresh.drain()  # a screen told nothing still expects the resting ticket plus the new one
    check = fresh.reconcile_now()
    assert check.reconciled, check
    legacy, _ = _broker(venue, clock=LATER)  # never restored: holdings are the expectation
    legacy.mirror_batch([_order("csp-3", created=T + timedelta(days=1))], holdings={})
    legacy.drain()
    assert not legacy.reconcile_now().reconciled


# -- the morning batch: entries the morning pass made and the sim filled at once -------------

S2 = date(2026, 9, 25)
OPEN_S2 = datetime(2026, 9, 25, 13, 30, tzinfo=timezone.utc)
AT_0945 = OPEN_S2 + timedelta(minutes=15)


class SessionCalendar(Calendar):
    def session_open(self, d: date) -> datetime:
        return datetime(d.year, d.month, d.day, 13, 30, tzinfo=timezone.utc)


def _morning(ledger: Ledger, *orders: Order, fill: tuple[str, ...] = (), job: str = "eod-morning",
             session: date | None = S2) -> None:
    """What the morning pass writes: entries created, submitted and (at the snapshot)
    filled, then its marker."""
    for order in orders:
        ledger.append(Event(account=order.account_id, kind=EventKind.ORDERS_CREATED,
                            payload=OrdersCreated(orders=(order,), fingerprint=order.order_id, reason="entry"),
                            ts_utc=AT_0945, command_id=f"create:{order.order_id}"))
        submitted = order.transition_to(OrderState.SUBMITTED)
        ledger.append(Event(account=order.account_id, kind=EventKind.ORDER_UPDATED,
                            payload=OrderUpdated(order=submitted, reason="submit"),
                            ts_utc=AT_0945, command_id=f"submit:{order.order_id}"))
        if order.order_id in fill:
            ledger.append(Event(account=order.account_id, kind=EventKind.ORDER_UPDATED,
                                payload=OrderUpdated(order=submitted.transition_to(OrderState.FILLED), reason="fill"),
                                ts_utc=AT_0945, command_id=f"filled:{order.order_id}"))
    if session is not None:
        for account in _binding().mirrored_accounts:
            ledger.append(Event(account=account, kind=EventKind.EOD_RUN,
                                payload=EodRun(session=session, job=job, account_id=account, bars_processed=0,
                                               at_close=AT_0945 + timedelta(minutes=5)),
                                ts_utc=AT_0945, command_id=f"eod:{job}:{account}:{session.isoformat()}"))


def _ids(orders) -> list[str]:
    return [o.order_id for o in orders]


def test_morning_orders_takes_the_morning_entries_the_sim_filled(ledger) -> None:
    _submit(ledger, _order("yesterday"))  # the 09-24 after-close batch: not the morning's
    _morning(ledger, _order("csp-1", created=AT_0945), _order("sp-1", "OPT_PUT_SPREAD", instrument=SPREAD,
             limit="1.05", created=AT_0945), _order("exit", parent="csp-0", created=AT_0945), fill=("csp-1",))
    orders = morning_orders(ledger, _binding(), S2, calendar=SessionCalendar())
    assert _ids(orders) == ["csp-1", "sp-1"]
    assert [o.state for o in orders] == [OrderState.FILLED, OrderState.SUBMITTED]


def test_morning_orders_skips_what_the_sim_ended_unfilled(ledger) -> None:
    _morning(ledger, _order("csp-1", created=AT_0945), _order("csp-2", created=AT_0945), session=None)
    ledger.append(Event(account="OPT_CSP", kind=EventKind.ORDER_CANCELLED,
                        payload=OrderStateChange("csp-2", "strategy cancelled"), ts_utc=AT_0945))
    _morning(ledger)
    assert _ids(morning_orders(ledger, _binding(), S2, calendar=SessionCalendar())) == ["csp-1"]


def test_morning_orders_ignores_entries_after_the_marker(ledger) -> None:
    _morning(ledger, _order("csp-1", created=AT_0945))
    _morning(ledger, _order("later", created=AT_0945 + timedelta(hours=6)), session=None)
    assert _ids(morning_orders(ledger, _binding(), S2, calendar=SessionCalendar())) == ["csp-1"]


def test_morning_orders_refuses_a_session_without_its_morning_marker(ledger) -> None:
    _morning(ledger, _order("csp-1", created=AT_0945), session=None)
    for account in _binding().mirrored_accounts:
        _marker(ledger, account, S2)  # the after-close marker is not the morning's
    with pytest.raises(MirrorSessionError, match="no morning pass"):
        morning_orders(ledger, _binding(), S2, calendar=SessionCalendar())


def test_morning_orders_refuses_two_jobs_unless_one_is_named(ledger) -> None:
    _morning(ledger, _order("csp-1", created=AT_0945))
    _morning(ledger, job="other-morning")
    with pytest.raises(MirrorSessionError, match="name the job"):
        morning_orders(ledger, _binding(), S2, calendar=SessionCalendar())
    assert _ids(morning_orders(ledger, _binding(), S2, calendar=SessionCalendar(), job="eod")) == ["csp-1"]


def test_the_after_close_batch_is_not_the_morning_passes(ledger) -> None:
    # A session with both markers: pending_orders reads the after-close run's alone.
    _morning(ledger, _order("csp-1", created=AT_0945), fill=("csp-1",))
    _submit(ledger, _order("next-day", created=datetime(2026, 9, 25, 20, 0, tzinfo=timezone.utc)), session=S2)
    assert _ids(pending_orders(ledger, _binding(), S2, calendar=SessionCalendar())) == ["next-day"]
    with pytest.raises(MirrorSessionError, match="no EOD run"):
        pending_orders(ledger, _binding(), date(2026, 9, 28), calendar=SessionCalendar())


def test_a_morning_entry_the_sim_filled_is_sent_to_the_venue(ledger) -> None:
    _morning(ledger, _order("csp-1", created=AT_0945), fill=("csp-1",))
    now = Clock(AT_0945 + timedelta(minutes=10))
    broker, venue = _broker(clock=now)
    report = run_mirror(ledger, broker, morning_orders(ledger, _binding(), S2, calendar=SessionCalendar()), clock=now)
    assert not report.halted and len(report.queued) == 1 and len(venue.placed) == 1
    assert report.drain_reconcile.reconciled
    assert morning_orders(ledger, _binding(), S2, calendar=SessionCalendar()) == ()  # handled

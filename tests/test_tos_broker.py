"""T2 broker tests: connect gates, the off-critical-path submit queue, read-back, halt.

The fake venue below is an in-memory paperMoney: it never touches a network, JAB or
any Schwab endpoint. Every guard has a firing and a non-firing test (§0.2).
"""

import sys
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pytest

from trade_engine.domain.instruments import Equity, OptionContract, Side
from trade_engine.domain.orders import Order, OrderType, TimeInForce
from trade_engine.interfaces.broker import (
    BrokerAdapter,
    OrderChanges,
    UnsupportedCapability,
    VenueOrder,
    VenueOrderAllocation,
)
from trade_engine.ledger.events import Event, EventKind, VenueReconcile
from trade_engine.ledger.state import fold, halted_venues
from trade_engine.tos_paper.broker import MirrorBinding, TosPaperBroker, TosPaperBrokerError
from trade_engine.tos_paper.transport import (
    OrderCanceller,
    TosOrderTransport,
    TransportRefused,
    TransportReplay,
)

T = datetime(2026, 9, 24, 20, 0, tzinfo=timezone.utc)
PM_A = "D-00000001"
PM_B = "D-00000002"
P200 = OptionContract(underlying="AAPL", expiry=date(2026, 10, 16), strike=Decimal("200"), right="P")
P190 = OptionContract(underlying="AAPL", expiry=date(2026, 10, 16), strike=Decimal("190"), right="P")


class FakeClock:
    def now_utc(self) -> datetime:
        return T

    def sleep(self, seconds: float) -> None: ...


class FakeVenue:
    """An in-memory paperMoney account behind the transport protocol."""

    def __init__(self, number: str = PM_A, kind: str = "margin", *, paper: bool = True) -> None:
        self.number, self.kind, self.paper = number, kind, paper
        self.positions: list[dict] = []
        self.working: list[dict] = []
        self.calls: list[str] = []
        self.placed: list[tuple[object, str]] = []
        self.result: dict = {"status": "SENT"}
        self.on_send = "rest"  # rest | fill | vanish
        self.place_raises: Exception | None = None
        self.read_raises: Exception | None = None
        self.read_raises_after_sends: int | None = None

    def connect(self) -> dict[str, str]:
        if not self.paper:
            raise TransportRefused("window title is 'thinkorswim'; order entry is paperMoney-only")
        return {"number": self.number, "type": self.kind}

    def place_order(self, ticket, idempotency_key: str) -> dict:
        self.calls.append("place")
        self.placed.append((ticket, idempotency_key))
        if self.place_raises is not None:
            raise self.place_raises
        if self.result.get("status") == "SENT":
            row = {"symbol": ticket.symbol, "side": ticket.side, "quantity": str(ticket.quantity),
                   "order_type": ticket.order_type,
                   "limit_price": None if ticket.limit_price is None else str(ticket.limit_price)}
            if self.on_send == "rest":
                self.working.append({**row, "filled": "0", "status": "WORKING"})
            elif self.on_send == "fill":
                signed = ticket.quantity if ticket.side == "BUY" else -ticket.quantity
                self.positions.append({"symbol": ticket.symbol, "quantity": str(signed), "avg_price": "2.00"})
        return dict(self.result)

    def _read(self, rows):
        sends = self.calls.count("place")
        if self.read_raises is not None and (self.read_raises_after_sends is None or sends >= self.read_raises_after_sends):
            raise self.read_raises
        return list(rows)

    def read_working_orders(self):
        self.calls.append("read_working")
        return self._read(self.working)

    def read_positions(self):
        self.calls.append("read_positions")
        return self._read(self.positions)

    def close(self) -> None: ...


class NoTouchVenue(FakeVenue):
    """Fails the test if the sim critical path touches the venue at all."""

    armed = False

    def place_order(self, ticket, idempotency_key):
        if self.armed:
            raise AssertionError("mirror_batch called the transport on the sim critical path")
        return super().place_order(ticket, idempotency_key)

    def read_positions(self):
        if self.armed:
            raise AssertionError("mirror_batch read the venue on the sim critical path")
        return super().read_positions()


class Balance:
    def __init__(self, value) -> None:
        self.value = value

    def net_liquidation(self):
        return self.value


def _binding(**changes) -> MirrorBinding:
    fields = dict(
        venue_account=PM_A,
        account_type="margin",
        mirrored_accounts=("OPT_CSP", "OPT_PUT_SPREAD"),
        minimum_balance=Decimal("100000"),
    )
    fields.update(changes)
    return MirrorBinding(**fields)


def _broker(venue: FakeVenue | None = None, *, balance="150000", ok=False, halted=(), **binding) -> TosPaperBroker:
    return TosPaperBroker(
        venue or FakeVenue(),
        _binding(**binding),
        clock=FakeClock(),
        balance_reader=None if balance is None else Balance(balance),
        balance_unproven_ok=ok,
        halted_venues=halted,
    )


def _connected(venue: FakeVenue | None = None, **kwargs) -> tuple[TosPaperBroker, FakeVenue]:
    venue = venue or FakeVenue()
    broker = _broker(venue, **kwargs)
    broker.connect()
    return broker, venue


def _order(oid: str, account: str, side: Side, qty: str = "1", *, instrument=P200, limit: str = "2.00") -> Order:
    return Order(
        order_id=oid, account_id=account, instrument=instrument, order_type=OrderType.LIMIT,
        side=side, quantity=Decimal(qty), command_id=oid, created_at=T,
        limit_price=Decimal(limit), tif=TimeInForce.DAY,
    )


def _venue_order(instrument=P200) -> VenueOrder:
    return VenueOrder(
        venue_order_id="tos:direct",
        instrument=instrument,
        order_type=OrderType.LIMIT,
        side=Side.SELL,
        quantity=Decimal("1"),
        submitted_at=T,
        tif=TimeInForce.DAY,
        limit_price=Decimal("2.00"),
        allocations=(VenueOrderAllocation("so-1", "OPT_CSP", Decimal("1")),),
    )


def test_the_fake_is_a_transport_and_the_broker_an_adapter() -> None:
    assert isinstance(FakeVenue(), TosOrderTransport)
    assert isinstance(_broker(), BrokerAdapter)


# -- binding guards -------------------------------------------------------------------


def test_binding_requires_a_paper_money_id() -> None:
    with pytest.raises(TosPaperBrokerError, match="paperMoney id"):
        _binding(venue_account="12345678")
    assert _binding(venue_account=PM_B).venue_account == PM_B


def test_binding_requires_mirrored_accounts() -> None:
    with pytest.raises(TosPaperBrokerError, match="at least one virtual account"):
        _binding(mirrored_accounts=())
    assert _binding(mirrored_accounts=["OPT_CSP"]).mirrored_accounts == ("OPT_CSP",)


@pytest.mark.parametrize("minimum", [Decimal("0"), Decimal("-1"), "0"])
def test_binding_refuses_non_positive_minimum(minimum) -> None:
    with pytest.raises(TosPaperBrokerError, match="must be positive"):
        _binding(minimum_balance=minimum)


def test_binding_coerces_and_validates_minimum_units() -> None:
    assert _binding(minimum_balance="100000").minimum_balance == Decimal("100000")
    assert _binding(minimum_balance=1).minimum_balance == Decimal("1")
    for bad in (100000.0, "NaN", "abc", None):
        with pytest.raises(TosPaperBrokerError):
            _binding(minimum_balance=bad)


def test_binding_refuses_an_unknown_account_type() -> None:
    with pytest.raises(TosPaperBrokerError, match="account_type"):
        _binding(account_type="futures")


# -- connect: identity ---------------------------------------------------------------


def test_connect_proves_number_and_type() -> None:
    broker, _ = _connected()
    assert broker.venue == PM_A and broker.balance_proven is True


def test_connect_refuses_account_number_mismatch() -> None:
    with pytest.raises(TosPaperBrokerError, match="refusing to mirror"):
        _broker(FakeVenue(number="D-00000009")).connect()


def test_connect_refuses_account_type_mismatch() -> None:
    """The IRA's number with the margin binding's type: a CSP must never reach the IRA."""
    with pytest.raises(TosPaperBrokerError, match="'ira'.*binding needs 'margin'"):
        _broker(FakeVenue(kind="ira")).connect()
    with pytest.raises(TosPaperBrokerError, match="unknown"):
        _broker(FakeVenue(kind="")).connect()


def test_connect_type_check_is_case_insensitive_and_fires_for_pm_b_too() -> None:
    _broker(FakeVenue(kind="Margin")).connect()
    ira = TosPaperBroker(
        FakeVenue(number=PM_B, kind="margin"),
        _binding(venue_account=PM_B, account_type="ira", mirrored_accounts=("OPT_0DTE_PCS_SPX",),
                 minimum_balance="50000"),
        clock=FakeClock(), balance_reader=Balance("60000"),
    )
    with pytest.raises(TosPaperBrokerError, match="binding needs 'ira'"):
        ira.connect()


def test_adapter_connect_propagates_a_live_window_refusal_and_stays_unconnected() -> None:
    broker = _broker(FakeVenue(paper=False))
    with pytest.raises(TransportRefused, match="paperMoney-only"):
        broker.connect()
    with pytest.raises(TosPaperBrokerError, match="prove the venue first"):
        broker.mirror_batch([_order("a", "OPT_CSP", Side.SELL)], holdings={})


# -- connect: balance rail -----------------------------------------------------------


def test_short_balance_refuses() -> None:
    with pytest.raises(TosPaperBrokerError, match="under the .*minimum"):
        _broker(balance="50000").connect()


def test_balance_exactly_at_the_minimum_connects() -> None:
    broker, _ = _connected(balance="100000")
    assert broker.balance_proven is True


def test_no_balance_reader_refuses_unless_acknowledged() -> None:
    with pytest.raises(TosPaperBrokerError, match="unproven"):
        _broker(balance=None).connect()
    broker, _ = _connected(balance=None, ok=True)
    assert broker.balance_proven is False and broker.balance() is None


@pytest.mark.parametrize("value,expected", [("150000", Decimal("150000")), (150000, Decimal("150000")), (Decimal("100000.01"), Decimal("100000.01"))])
def test_balance_units_are_coerced(value, expected) -> None:
    assert _broker(balance=value).balance() == expected


@pytest.mark.parametrize("value", [150000.0, "NaN", "Infinity", "lots", None])
def test_unusable_balance_refuses(value) -> None:
    class Odd:
        def net_liquidation(self):
            return value

    broker = TosPaperBroker(FakeVenue(), _binding(), clock=FakeClock(), balance_reader=Odd())
    with pytest.raises(TosPaperBrokerError):
        broker.connect()


# -- the sim critical path ------------------------------------------------------------


def test_mirror_batch_before_connect_refuses() -> None:
    with pytest.raises(TosPaperBrokerError, match="mirror_batch before connect"):
        _broker().mirror_batch([_order("a", "OPT_CSP", Side.SELL)], holdings={})


def test_mirror_batch_never_touches_the_transport() -> None:
    venue = NoTouchVenue()
    broker, _ = _connected(venue)
    venue.armed = True
    batch = broker.mirror_batch([_order("a", "OPT_CSP", Side.SELL)], holdings={})
    assert len(batch.venue_orders) == 1 and len(broker.queued) == 1
    assert venue.calls == []


def test_mirror_batch_refuses_unmirrored_accounts_at_the_venue_only() -> None:
    broker, _ = _connected()
    batch = broker.mirror_batch(
        [_order("a", "OPT_CSP", Side.SELL), _order("z", "OPT_0DTE_PCS_SPX", Side.SELL)], holdings={}
    )
    assert "not mirrored" in dict(batch.refused)["z"]


def test_a_halted_venue_refuses_every_new_order() -> None:
    events = [Event(account="OPT_CSP", kind=EventKind.VENUE_RECONCILE,
                    payload=VenueReconcile(venue=PM_A, as_of=T, reconciled=False, drift=(P200.symbol,)),
                    ts_utc=T, seq=1)]
    broker, venue = _connected(halted=halted_venues(fold(events)))
    assert broker.halted
    batch = broker.mirror_batch([_order("a", "OPT_CSP", Side.SELL)], holdings={})
    assert batch.venue_orders == () and "halted" in dict(batch.refused)["a"]
    assert broker.queued == ()
    assert broker.submit(_venue_order()).status == "REJECTED" and venue.placed == []


def test_another_venues_halt_does_not_halt_this_one() -> None:
    broker, _ = _connected(halted={PM_B})
    assert not broker.halted
    assert len(broker.mirror_batch([_order("a", "OPT_CSP", Side.SELL)], holdings={}).venue_orders) == 1


def test_the_same_ticket_is_queued_once() -> None:
    broker, _ = _connected()
    broker.mirror_batch([_order("a", "OPT_CSP", Side.SELL)], holdings={})
    broker.mirror_batch([_order("a", "OPT_CSP", Side.SELL)], holdings={})
    assert len(broker.queued) == 1


# -- the slow path: drain ------------------------------------------------------------


def test_drain_before_connect_refuses() -> None:
    with pytest.raises(TosPaperBrokerError, match="drain before connect"):
        _broker().drain()


def test_empty_drain_is_a_no_op() -> None:
    broker, venue = _connected()
    report = broker.drain()
    assert report.acks == () and report.reconcile is None and venue.calls == []


def test_drain_sends_one_ticket_at_a_time_each_read_back() -> None:
    broker, venue = _connected()
    broker.mirror_batch(
        [_order("a", "OPT_CSP", Side.SELL, instrument=P200), _order("b", "OPT_PUT_SPREAD", Side.BUY, instrument=P190)],
        holdings={},
    )
    report = broker.drain()
    assert venue.calls == [
        "read_positions",
        "place", "read_positions", "read_working",
        "place", "read_positions", "read_working",
        "read_positions", "read_working",  # the reconcile after the batch
    ]
    assert [a.status for a in report.acks] == ["ACCEPTED", "ACCEPTED"]
    assert report.reconcile.reconciled and report.reconcile.venue == PM_A
    assert not broker.halted and broker.queued == ()
    ticket, key = venue.placed[0]
    assert ticket.symbol == P200.to_occ() and ticket.quantity == 1 and isinstance(ticket.quantity, int)
    assert key == report.acks[0].venue_order_id and key.startswith("tos:")


def test_an_immediate_fill_is_confirmed_by_the_position_read_back() -> None:
    venue = FakeVenue()
    venue.on_send = "fill"
    broker, _ = _connected(venue)
    broker.mirror_batch([_order("a", "OPT_CSP", Side.SELL)], holdings={})
    report = broker.drain()
    assert report.acks[0].status == "ACCEPTED" and "position moved" in report.acks[0].message
    assert report.reconcile.reconciled


def test_holdings_are_part_of_the_expected_book() -> None:
    venue = FakeVenue()
    venue.positions.append({"symbol": P190.to_occ(), "quantity": "-1", "avg_price": "1.00"})
    broker, _ = _connected(venue)
    broker.mirror_batch([_order("a", "OPT_CSP", Side.SELL)], holdings={("OPT_PUT_SPREAD", P190): Decimal("-1")})
    assert broker.drain().reconcile.reconciled


def test_tickets_queued_by_an_earlier_batch_stay_in_the_expected_book() -> None:
    broker, _ = _connected()
    broker.mirror_batch([_order("a", "OPT_CSP", Side.SELL, instrument=P200)], holdings={})
    broker.mirror_batch([_order("b", "OPT_CSP", Side.SELL, instrument=P190)], holdings={})
    report = broker.drain()
    assert len(report.acks) == 2 and report.reconcile.reconciled


def test_sent_is_never_accepted_without_read_back() -> None:
    """SENT but nothing appears at the venue: PENDING, then the reconcile halts."""
    venue = FakeVenue()
    venue.on_send = "vanish"
    broker, _ = _connected(venue)
    broker.mirror_batch([_order("a", "OPT_CSP", Side.SELL)], holdings={})
    report = broker.drain()
    assert report.acks[0].status == "PENDING"
    assert not report.reconcile.reconciled and report.reconcile.drift == (P200.symbol,)
    assert broker.halted
    assert "halted" in dict(broker.mirror_batch([_order("b", "OPT_CSP", Side.SELL)], holdings={}).refused)["b"]


def test_dry_run_is_pending_never_accepted() -> None:
    venue = FakeVenue()
    venue.result = {"status": "DRY_RUN", "echo": {}}
    broker, _ = _connected(venue)
    broker.mirror_batch([_order("a", "OPT_CSP", Side.SELL)], holdings={})
    report = broker.drain()
    assert report.acks[0].status == "PENDING" and "dry run" in report.acks[0].message


def test_a_venue_refusal_is_rejected_with_reason_and_leaves_the_book_clean() -> None:
    venue = FakeVenue()
    venue.result = {"status": "REFUSED", "reason": "account not eligible"}
    broker, _ = _connected(venue)
    broker.mirror_batch([_order("a", "OPT_CSP", Side.SELL)], holdings={})
    report = broker.drain()
    assert report.acks[0].status == "REJECTED" and "not eligible" in report.acks[0].message
    assert report.reconcile.reconciled and not broker.halted


@pytest.mark.parametrize(
    "exc,status",
    [(TransportRefused("echo mismatch"), "REJECTED"), (TransportReplay("key used"), "PENDING"), (RuntimeError("JAB hung"), "PENDING")],
)
def test_transport_exceptions_never_escape_a_batch(exc, status) -> None:
    venue = FakeVenue()
    venue.place_raises = exc
    broker, _ = _connected(venue)
    broker.mirror_batch(
        [_order("a", "OPT_CSP", Side.SELL, instrument=P200), _order("b", "OPT_CSP", Side.SELL, instrument=P190)],
        holdings={},
    )
    report = broker.drain()
    assert [a.status for a in report.acks] == [status, status]
    assert len(venue.placed) == 2
    assert report.reconcile.reconciled is (status == "REJECTED")


def test_a_failed_pre_send_read_sends_nothing_and_halts() -> None:
    venue = FakeVenue()
    venue.read_raises = RuntimeError("JAB tree gone")
    broker, _ = _connected(venue)
    broker.mirror_batch([_order("a", "OPT_CSP", Side.SELL)], holdings={})
    report = broker.drain()
    assert venue.placed == [] and report.acks[0].status == "REJECTED"
    assert not report.reconcile.reconciled and broker.halted


def test_a_failed_read_back_after_send_is_pending_and_halts() -> None:
    venue = FakeVenue()
    venue.read_raises = RuntimeError("JAB tree gone")
    venue.read_raises_after_sends = 1
    broker, _ = _connected(venue)
    broker.mirror_batch([_order("a", "OPT_CSP", Side.SELL)], holdings={})
    report = broker.drain()
    assert report.acks[0].status == "PENDING" and "read-back failed" in report.acks[0].message
    assert not report.reconcile.reconciled and report.reconcile.drift == (P200.symbol,)
    assert broker.halted


def test_a_venue_rejected_row_is_rejected_and_removed_from_the_book() -> None:
    venue = FakeVenue()
    broker, _ = _connected(venue)
    broker.mirror_batch([_order("a", "OPT_CSP", Side.SELL)], holdings={})
    original = venue.place_order

    def rejecting(ticket, key):
        result = original(ticket, key)
        venue.working[-1]["status"] = "REJECTED"
        return result

    venue.place_order = rejecting
    report = broker.drain()
    assert report.acks[0].status == "REJECTED" and report.reconcile.reconciled


def test_a_halt_between_queue_and_drain_sends_nothing() -> None:
    venue = FakeVenue()
    broker, _ = _connected(venue)
    broker.mirror_batch([_order("a", "OPT_CSP", Side.SELL)], holdings={})
    venue.positions.append({"symbol": P190.to_occ(), "quantity": "3", "avg_price": "1.00"})
    assert not broker.reconcile_now().reconciled and broker.halted
    report = broker.drain()
    assert venue.placed == [] and report.acks[0].status == "REJECTED" and "halted" in report.acks[0].message


def test_an_inexpressible_queued_ticket_is_rejected_not_raised() -> None:
    broker, venue = _connected()
    broker._queue.append(_venue_order(instrument=Equity("AAPL")))
    report = broker.drain()
    assert report.acks[0].status == "REJECTED" and "UnsupportedCapability" in report.acks[0].message
    assert venue.placed == []


# -- the adapter contract -------------------------------------------------------------


def test_submit_before_connect_refuses() -> None:
    with pytest.raises(TosPaperBrokerError, match="prove the venue first"):
        _broker().submit(_venue_order())


def test_submit_is_pending_until_read_back() -> None:
    broker, venue = _connected()
    ack = broker.submit(_venue_order())
    assert ack.status == "PENDING" and len(venue.placed) == 1


def test_submit_refuses_what_the_venue_cannot_express() -> None:
    broker, venue = _connected()
    with pytest.raises(UnsupportedCapability, match="single option contracts"):
        broker.submit(_venue_order(instrument=Equity("SPY")))
    stop = VenueOrder(
        venue_order_id="tos:stop", instrument=P200, order_type=OrderType.STOP, side=Side.SELL,
        quantity=Decimal("1"), submitted_at=T, stop_price=Decimal("1.00"),
        allocations=(VenueOrderAllocation("so-1", "OPT_CSP", Decimal("1")),),
    )
    with pytest.raises(UnsupportedCapability, match="order type"):
        broker.submit(stop)
    gtd = VenueOrder(
        venue_order_id="tos:gtd", instrument=P200, order_type=OrderType.LIMIT, side=Side.SELL,
        quantity=Decimal("1"), submitted_at=T, tif=TimeInForce.GTD, limit_price=Decimal("1.00"),
        allocations=(VenueOrderAllocation("so-1", "OPT_CSP", Decimal("1")),),
    )
    with pytest.raises(UnsupportedCapability, match="TIF"):
        broker.submit(gtd)
    frac = VenueOrder(
        venue_order_id="tos:frac", instrument=P200, order_type=OrderType.LIMIT, side=Side.SELL,
        quantity=Decimal("1.5"), submitted_at=T, limit_price=Decimal("1.00"),
        allocations=(VenueOrderAllocation("so-1", "OPT_CSP", Decimal("1.5")),),
    )
    with pytest.raises(UnsupportedCapability, match="whole number"):
        broker.submit(frac)
    assert venue.placed == []


def test_reads_that_cannot_be_proven_raise_instead_of_returning_empty() -> None:
    broker, _ = _connected()
    with pytest.raises(TosPaperBrokerError, match="cannot be read"):
        broker.orders(T)
    with pytest.raises(TosPaperBrokerError, match="not wired"):
        broker.fills(T)
    with pytest.raises(TosPaperBrokerError, match="cannot be read"):
        broker.cash_events(T)


def test_positions_are_read_back_and_normalized() -> None:
    venue = FakeVenue()
    venue.positions.append({"symbol": P200.to_occ(), "quantity": "-2", "avg_price": "2.10"})
    broker, _ = _connected(venue)
    (position,) = broker.positions()
    assert position.instrument == P200 and position.quantity == Decimal("-2")
    with pytest.raises(TosPaperBrokerError, match="prove the venue first"):
        _broker().positions()


# -- cancel ----------------------------------------------------------------------------


class CancelVenue(FakeVenue):
    """A FakeVenue whose sends name their Order Book row, and which can cancel it."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.next_id = 5400000001
        self.ids: dict[str, dict] = {}  # Order ID -> its working row
        self.cancelled: list[str] = []
        self.cancel_result: dict | None = None  # overrides the CANCELED answer
        self.cancel_raises: Exception | None = None
        self.name_rows = True  # False: return the raw result, naming no Order ID

    def place_order(self, ticket, idempotency_key: str) -> dict:
        rows = len(self.working)
        out = super().place_order(ticket, idempotency_key)
        if self.name_rows and out.get("status") == "SENT" and len(self.working) > rows:
            oid = str(self.next_id)
            self.next_id += 1
            self.ids[oid] = self.working[-1]
            out.update(order_id=oid, book_status="WORKING")
        return out

    def cancel_order(self, order_id: str) -> dict:
        self.calls.append("cancel")
        self.cancelled.append(order_id)
        if self.cancel_raises is not None:
            raise self.cancel_raises
        if self.cancel_result is not None:
            return dict(self.cancel_result)
        self.ids[order_id]["status"] = "CANCELED"
        return {"status": "CANCELED", "order_id": order_id, "book_status": "CANCELED"}


def _sent_ticket(venue: FakeVenue | None = None, **kwargs) -> tuple[TosPaperBroker, FakeVenue, str]:
    broker, venue = _connected(venue or CancelVenue(), **kwargs)
    batch = broker.mirror_batch([_order("a", "OPT_CSP", Side.SELL)], holdings={})
    report = broker.drain()
    assert report.acks[0].status == "ACCEPTED" and report.reconcile.reconciled
    return broker, venue, batch.venue_orders[0].venue_order_id


def test_the_cancel_venue_is_a_canceller_and_the_plain_fake_is_not() -> None:
    assert isinstance(CancelVenue(), OrderCanceller) and isinstance(CancelVenue(), TosOrderTransport)
    assert not isinstance(FakeVenue(), OrderCanceller)


def test_cancel_before_connect_refuses() -> None:
    with pytest.raises(TosPaperBrokerError, match="prove the venue first"):
        _broker(CancelVenue()).cancel("tos:x")


def test_cancel_of_a_sent_ticket_cancels_its_order_book_row() -> None:
    broker, venue, key = _sent_ticket()
    ack = broker.cancel(key)
    assert ack.status == "ACCEPTED" and ack.venue_order_id == key and "5400000001" in ack.message
    assert venue.cancelled == ["5400000001"]
    assert broker.reconcile_now().reconciled  # the ticket left the expected book with its row


def test_a_second_cancel_is_idempotent_and_clicks_nothing() -> None:
    broker, venue, key = _sent_ticket()
    broker.cancel(key)
    ack = broker.cancel(key)
    assert ack.status == "ACCEPTED" and ack.message == "already cancelled"
    assert venue.cancelled == ["5400000001"]
    assert broker.reconcile_now().reconciled  # not subtracted twice


def test_cancel_of_a_queued_ticket_drops_it_without_touching_the_venue() -> None:
    broker, venue = _connected(CancelVenue())
    batch = broker.mirror_batch([_order("a", "OPT_CSP", Side.SELL)], holdings={})
    key = batch.venue_orders[0].venue_order_id
    ack = broker.cancel(key)
    assert ack.status == "ACCEPTED" and "before send" in ack.message
    assert broker.queued == () and "cancel" not in venue.calls and not venue.placed
    assert broker.reconcile_now().reconciled
    assert broker.cancel(key).message == "already cancelled"


def test_cancel_of_a_queued_ticket_keeps_the_others() -> None:
    broker, venue = _connected(CancelVenue())
    batch = broker.mirror_batch(
        [_order("a", "OPT_CSP", Side.SELL), _order("b", "OPT_CSP", Side.SELL, instrument=P190)], holdings={}
    )
    first, second = (t.venue_order_id for t in batch.venue_orders)
    broker.cancel(first)
    assert [t.venue_order_id for t in broker.queued] == [second]
    report = broker.drain()
    assert [a.venue_order_id for a in report.acks] == [second] and report.reconcile.reconciled


def test_a_cancelled_queued_ticket_is_not_queued_again() -> None:
    broker, _ = _connected(CancelVenue())
    order = _order("a", "OPT_CSP", Side.SELL)
    broker.cancel(broker.mirror_batch([order], holdings={}).venue_orders[0].venue_order_id)
    broker.mirror_batch([order], holdings={})
    assert broker.queued == ()  # the key stays used (I3)


def test_cancel_of_an_unknown_key_is_rejected_without_a_venue_call() -> None:
    broker, venue, _ = _sent_ticket()
    ack = broker.cancel("tos:never")
    assert ack.status == "REJECTED" and "no venue Order ID" in ack.message
    assert "cancel" not in venue.calls


@pytest.mark.parametrize("result", [
    {"status": "SENT"},
    {"status": "SENT", "order_id": "5400000009", "book_status": "UNKNOWN"},
    {"status": "SENT", "order_id": "not-digits", "book_status": "WORKING"},
])
def test_a_send_that_proved_no_order_id_cannot_be_cancelled(result) -> None:
    venue = CancelVenue()
    venue.name_rows = False
    venue.result = result
    broker, _, key = _sent_ticket(venue)
    ack = broker.cancel(key)
    assert ack.status == "REJECTED" and "refusing to guess" in ack.message
    assert "cancel" not in venue.calls


def test_a_transport_without_cancel_is_rejected() -> None:
    venue = FakeVenue()
    venue.result = {"status": "SENT", "order_id": "5400000001", "book_status": "WORKING"}
    broker, _, key = _sent_ticket(venue)
    ack = broker.cancel(key)
    assert ack.status == "REJECTED" and "cannot cancel" in ack.message


def test_an_unconfirmed_cancel_is_pending_and_may_be_retried() -> None:
    broker, venue, key = _sent_ticket()
    venue.cancel_result = {"status": "UNKNOWN", "order_id": "5400000001", "note": "row still WORKING"}
    ack = broker.cancel(key)
    assert ack.status == "PENDING" and "row still WORKING" in ack.message
    assert broker.reconcile_now().reconciled  # still resting, still expected
    venue.cancel_result = None
    assert broker.cancel(key).status == "ACCEPTED"
    assert venue.cancelled == ["5400000001", "5400000001"]


@pytest.mark.parametrize("exc,status", [
    (TransportRefused("order 5400000001 is FILLED, not WORKING"), "REJECTED"),
    (TimeoutError("JAB hung"), "PENDING"),
])
def test_cancel_exceptions_are_mapped_never_raised(exc, status) -> None:
    broker, venue, key = _sent_ticket()
    venue.cancel_raises = exc
    ack = broker.cancel(key)
    assert ack.status == status
    assert broker.reconcile_now().reconciled  # the expectation is untouched
    venue.cancel_raises = None
    assert broker.cancel(key).status == "ACCEPTED"  # still cancellable


def test_cancel_still_works_on_a_halted_venue() -> None:
    broker, venue, key = _sent_ticket()
    venue.positions.append({"symbol": P190.to_occ(), "quantity": "1", "avg_price": "1.00"})
    assert not broker.reconcile_now().reconciled and broker.halted
    assert broker.cancel(key).status == "ACCEPTED"
    assert venue.cancelled == ["5400000001"]


def test_replace_still_refuses() -> None:
    broker, _ = _connected(CancelVenue())
    with pytest.raises(TosPaperBrokerError, match="not mapped over JAB"):
        broker.replace("tos:x", OrderChanges(new_limit_price=Decimal("3.00")))


def test_no_real_paper_money_account_numbers_in_this_repo() -> None:
    root = Path(__file__).resolve().parent.parent
    for path in list((root / "src").rglob("*.py")) + list((root / "tests").rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for digit in "56":
            assert "6829500" + digit not in text, path  # built from parts so this file passes

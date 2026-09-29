"""The live follower: a read-only sim, the venue's own ledger (T2 follow, owner 2026-09-29).

The sim ledger is held open by its writer throughout, as the intraday service holds it,
and the follower reads it through ``LedgerReader``. Same in-memory paperMoney as
``test_tos_mirror``.
"""

from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from test_tos_mirror import SPREAD, Clock, Venue, _broker, _order
from test_tos_mirror_exits import CLOSING, MIDDAY, MORNING, OPEN, S, _book, _price
from trade_engine.domain.instruments import Side
from trade_engine.domain.orders import Order, OrderState, OrderType, TimeInForce
from trade_engine.ledger import Event, EventKind, Ledger
from trade_engine.ledger.events import OrderUpdated, mirror_account
from trade_engine.ledger.reader import LedgerReader, LedgerReadOnlyError
from trade_engine.tos_paper.follow import SplitLedger, SplitLedgerError, follow_cycle, follow_entries, pass_name
from trade_engine.tos_paper.session import mirror_of

D = Decimal
ACCOUNTS = ("OPT_CSP", "OPT_PUT_SPREAD")
VENUE = "D-00000001"


@pytest.fixture()
def books(tmp_path: Path):
    """(the sim's writer, the follower's split view, the follower's own ledger)."""
    with Ledger(tmp_path / "sim.db") as sim, Ledger(tmp_path / "mirror.db") as own:
        reader = LedgerReader(tmp_path / "sim.db").open()
        try:
            yield sim, SplitLedger(reader, own, ACCOUNTS), own
        finally:
            reader.close()


def _entry(oid: str = "sp-1", created=MORNING.now, qty: str = "2") -> Order:
    return _order(oid, "OPT_PUT_SPREAD", instrument=SPREAD, qty=qty, limit="1.05", created=created)


def _child(oid: str, *, order_type=OrderType.MARKET, limit=None, tif=TimeInForce.DAY, at=MORNING.now) -> Order:
    return Order(order_id=oid, account_id="OPT_PUT_SPREAD", instrument=CLOSING, order_type=order_type,
                 side=Side.BUY, quantity=D(2), command_id=oid, created_at=at,
                 limit_price=None if limit is None else D(limit), tif=tif, parent_order_id="sp-1")


def _cancel(sim: Ledger, oid: str) -> None:
    sim.append(Event(account="OPT_PUT_SPREAD", kind=EventKind.ORDER_UPDATED,
                     payload=OrderUpdated(order=sim.state("OPT_PUT_SPREAD").orders[oid]
                                          .transition_to(OrderState.CANCELLED), reason="sim cancel"),
                     ts_utc=MORNING.now, command_id=f"cancel:{oid}"))


def _cycle(split, venue, clock=MORNING, price=None):
    broker, _ = _broker(venue, clock=clock)
    return follow_cycle(split, broker, S, session_open=OPEN, price=price or _price("1.40"), clock=clock)


def _entries(split, now=MORNING.now):
    return follow_entries(split, _broker()[0].binding, session_open=OPEN, now=now)


# -- the read-only reader ------------------------------------------------------------------


def test_the_reader_reads_a_ledger_its_writer_holds_and_sees_new_commits(tmp_path) -> None:
    with Ledger(tmp_path / "sim.db") as sim:
        reader = LedgerReader(tmp_path / "sim.db").open()
        assert reader.state("OPT_PUT_SPREAD").orders == {}
        _book(sim, _entry())
        assert set(reader.state("OPT_PUT_SPREAD").orders) == {"sp-1"}  # re-folded on growth
        reader.close()


@pytest.mark.parametrize("write", ["append", "extend", "set_meta", "add_listener", "enqueue_outbox"])
def test_the_reader_refuses_every_write(tmp_path, write) -> None:
    with Ledger(tmp_path / "sim.db"):
        reader = LedgerReader(tmp_path / "sim.db").open()
        with pytest.raises(LedgerReadOnlyError):
            getattr(reader, write)(object())
        reader.close()


def test_the_reader_never_creates_a_ledger(tmp_path) -> None:
    with pytest.raises(LedgerReadOnlyError, match="never creates"):
        LedgerReader(tmp_path / "absent.db").open()
    assert not (tmp_path / "absent.db").exists()


# -- the split view ------------------------------------------------------------------------


def test_the_split_reads_sim_accounts_from_the_sim_and_writes_only_its_own(books) -> None:
    sim, split, own = books
    _book(sim, _entry(), fill="1.05")
    assert set(split.state("OPT_PUT_SPREAD").orders) == {"sp-1"}
    _cycle(split, Venue())
    venue_account = mirror_account(VENUE)
    assert own.state(venue_account).orders == {} and mirror_of(own, VENUE).tickets  # the venue's state is its own
    assert venue_account not in sim.accounts()  # and nothing reached the sim
    assert venue_account in split.accounts() and "OPT_CSP" in split.accounts()


def test_the_split_never_writes_a_sim_account(books) -> None:
    _, split, _ = books
    event = Event(account="OPT_PUT_SPREAD", kind=EventKind.ORDER_UPDATED,
                  payload=OrderUpdated(order=_entry(), reason="x"), ts_utc=MORNING.now, command_id="w")
    with pytest.raises(SplitLedgerError, match="never writes"):
        split.extend([event])
    with pytest.raises(SplitLedgerError):
        split.append(event)
    with pytest.raises(SplitLedgerError, match="name the account"):
        split.events()


# -- the entries a cycle takes -------------------------------------------------------------


def test_a_new_entry_is_sent_and_a_second_cycle_sends_nothing(books) -> None:
    sim, split, _ = books
    _book(sim, _entry(), fill="1.05")
    venue = Venue()
    _cycle(split, venue)
    assert [t.price_effect for t in venue.placed] == ["CREDIT"]
    _cycle(split, venue)
    assert len(venue.placed) == 1


def test_an_entry_older_than_the_max_age_is_refused_with_the_reason(books) -> None:
    sim, split, _ = books
    _book(sim, _entry())
    entries, refused = _entries(split, now=MORNING.now + timedelta(minutes=6))
    assert entries == () and "a late copy" in refused[0][2]
    entries, refused = _entries(split, now=MORNING.now + timedelta(minutes=4))
    assert len(entries) == 1 and refused == ()


def test_a_refused_entry_is_recorded_once_and_never_sent(books) -> None:
    sim, split, own = books
    _book(sim, _entry())
    venue = Venue()
    late = Clock(MORNING.now + timedelta(minutes=10))
    _cycle(split, venue, clock=late)
    _cycle(split, venue, clock=late)
    assert venue.placed == [] and mirror_of(split, VENUE).handled("sp-1")
    assert len(own.events_of_kind(EventKind.MIRROR_REFUSED)) == 1


def test_an_entry_the_sim_already_opened_and_closed_is_refused(books) -> None:
    sim, split, _ = books
    _book(sim, _entry(), fill="1.05")
    _book(sim, _child("sp-1:close:1"), fill="1.50")
    entries, refused = _entries(split)
    assert entries == () and "opened and closed" in refused[0][2]


def test_an_entry_the_sim_still_holds_is_sent(books) -> None:
    sim, split, _ = books
    _book(sim, _entry(), fill="1.05")
    entries, refused = _entries(split)
    assert [e.order_id for e in entries] == ["sp-1"] and refused == ()


def test_entries_before_the_open_or_ended_unfilled_are_not_taken(books) -> None:
    sim, split, _ = books
    _book(sim, _entry("old", created=OPEN - timedelta(hours=1)))
    _book(sim, _entry("gone"))
    _cancel(sim, "gone")
    assert _entries(split) == ((), ())


# -- the whole follow: in, target, out -----------------------------------------------------


def test_the_follower_opens_rests_the_target_and_closes_as_the_sim_does(books) -> None:
    sim, split, _ = books
    _book(sim, _entry(), fill="1.05")
    _book(sim, _child("sp-1:target", order_type=OrderType.LIMIT, limit="0.50", tif=TimeInForce.GTC))
    venue = Venue()
    _cycle(split, venue)
    [oid] = list(venue.orders)
    venue.fill(oid, 2, "1.00")
    _cycle(split, venue, clock=Clock(MORNING.now + timedelta(minutes=1)))
    rested = venue.placed[-1]
    assert (rested.price_effect, rested.limit_price) == ("DEBIT", D("0.50"))
    _book(sim, _child("sp-1:close:1", at=MIDDAY.now), fill="1.50", at=MIDDAY.now)
    _cycle(split, venue, clock=MIDDAY)
    target_oid = next(k for k, o in venue.orders.items() if o["ticket"] is rested)
    assert venue.orders[target_oid]["status"] == "CANCELED"
    sent = venue.placed[-1]
    assert sent is not rested and (sent.price_effect, sent.quantity, sent.limit_price) == ("DEBIT", 2, D("1.40"))


def test_an_unfilled_venue_entry_is_cancelled_when_the_sim_closes(books) -> None:
    sim, split, _ = books
    _book(sim, _entry(), fill="1.05")
    venue = Venue()
    _cycle(split, venue)
    [oid] = list(venue.orders)
    _book(sim, _child("sp-1:close:1", at=MIDDAY.now), fill="1.50", at=MIDDAY.now)
    _cycle(split, venue, clock=MIDDAY)
    assert venue.orders[oid]["status"] == "CANCELED" and len(venue.placed) == 1


def test_pass_names_are_the_et_minute() -> None:
    assert pass_name(MIDDAY.now) == "follow-1235"

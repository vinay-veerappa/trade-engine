"""T2 reconcile tests: drift detection, read-back confirmation, per-venue halt fold (P4 gate)."""

import sys
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from trade_engine.domain.instruments import OptionContract, Side
from trade_engine.domain.orders import OrderState, OrderType
from trade_engine.interfaces.broker import VenueOrder, VenueOrderAllocation, VenuePosition
from trade_engine.ledger.events import Event, EventKind, VenueReconcile
from trade_engine.ledger.state import fold, halted_venues
from trade_engine.tos_paper.normalize import WorkingOrder
from trade_engine.tos_paper.reconcile import UNREADABLE, confirm_ticket, reconcile, unreadable

T = datetime(2026, 9, 24, 20, 0, tzinfo=timezone.utc)
VENUE = "D-00000001"
P200 = OptionContract(underlying="AAPL", expiry=date(2026, 10, 16), strike=Decimal("200"), right="P")
P190 = OptionContract(underlying="AAPL", expiry=date(2026, 10, 16), strike=Decimal("190"), right="P")


def _pos(contract, qty: str) -> VenuePosition:
    return VenuePosition(contract, Decimal(qty), Decimal("2.00"), T)


def _row(contract, side, qty="1", filled="0", state=OrderState.ACCEPTED, limit="2.00") -> WorkingOrder:
    return WorkingOrder(
        contract, side, Decimal(qty), Decimal(filled),
        OrderType.LIMIT if limit else OrderType.MARKET,
        Decimal(limit) if limit else None, state,
    )


def _ticket(side=Side.SELL, qty="1", limit="2.00", contract=P200) -> VenueOrder:
    return VenueOrder(
        venue_order_id="tos:abc",
        instrument=contract,
        order_type=OrderType.LIMIT if limit else OrderType.MARKET,
        side=side,
        quantity=Decimal(qty),
        submitted_at=T,
        limit_price=Decimal(limit) if limit else None,
        allocations=(VenueOrderAllocation("so-1", "OPT_CSP", Decimal(qty)),),
    )


# -- reconcile -------------------------------------------------------------------------


def test_matching_positions_reconcile_clean() -> None:
    event = reconcile(VENUE, T, {P200: Decimal("-1")}, [_pos(P200, "-1")], [])
    assert event.reconciled and event.drift == () and event.venue == VENUE


def test_a_working_order_covers_the_unfilled_expectation() -> None:
    event = reconcile(VENUE, T, {P200: Decimal("-2")}, [_pos(P200, "-1")], [_row(P200, Side.SELL, "1")])
    assert event.reconciled


def test_partial_working_order_counts_only_its_remainder() -> None:
    rows = [_row(P200, Side.SELL, "2", filled="1", state=OrderState.PARTIALLY_FILLED)]
    assert reconcile(VENUE, T, {P200: Decimal("-2")}, [_pos(P200, "-1")], rows).reconciled
    assert not reconcile(VENUE, T, {P200: Decimal("-3")}, [_pos(P200, "-1")], rows).reconciled


def test_a_dead_working_row_does_not_cover_anything() -> None:
    rows = [_row(P200, Side.SELL, state=OrderState.CANCELLED)]
    event = reconcile(VENUE, T, {P200: Decimal("-1")}, [], rows)
    assert not event.reconciled and event.drift == (P200.symbol,)


def test_missing_position_is_drift() -> None:
    event = reconcile(VENUE, T, {P200: Decimal("-1")}, [], [])
    assert not event.reconciled and event.drift == (P200.symbol,)


def test_unexpected_venue_position_is_drift() -> None:
    event = reconcile(VENUE, T, {}, [_pos(P190, "1")], [])
    assert event.drift == (P190.symbol,)


def test_unknown_order_state_is_drift_not_a_guess() -> None:
    rows = [_row(P200, Side.SELL, state=OrderState.PENDING_UNKNOWN)]
    event = reconcile(VENUE, T, {P200: Decimal("-1")}, [_pos(P200, "-1")], rows)
    assert not event.reconciled and event.drift == (P200.symbol,)


def test_unreadable_names_every_contract_or_a_marker() -> None:
    event = unreadable(VENUE, T, [P200, P190, P200], "JAB down")
    assert not event.reconciled and event.drift == tuple(sorted({P200.symbol, P190.symbol}))
    assert unreadable(VENUE, T, [], "JAB down").drift == (UNREADABLE,)


# -- confirm_ticket --------------------------------------------------------------------


def test_matching_live_row_confirms_accepted() -> None:
    status, _ = confirm_ticket(_ticket(), {}, [], [_row(P200, Side.SELL)], set())
    assert status == "ACCEPTED"


def test_matching_filled_row_confirms_accepted() -> None:
    status, reason = confirm_ticket(_ticket(), {}, [], [_row(P200, Side.SELL, state=OrderState.FILLED)], set())
    assert status == "ACCEPTED" and "filled" in reason


def test_matching_rejected_row_is_rejected() -> None:
    status, _ = confirm_ticket(_ticket(), {}, [], [_row(P200, Side.SELL, state=OrderState.REJECTED)], set())
    assert status == "REJECTED"


def test_matching_unknown_row_stays_pending() -> None:
    status, _ = confirm_ticket(_ticket(), {}, [], [_row(P200, Side.SELL, state=OrderState.PENDING_UNKNOWN)], set())
    assert status == "PENDING"


def test_position_moved_by_exactly_the_ticket_confirms() -> None:
    status, reason = confirm_ticket(_ticket(), {P200: Decimal("0")}, [_pos(P200, "-1")], [], set())
    assert status == "ACCEPTED" and "position moved" in reason


def test_nothing_visible_stays_pending() -> None:
    assert confirm_ticket(_ticket(), {}, [], [], set())[0] == "PENDING"
    # a position that moved the wrong way or by the wrong amount proves nothing
    assert confirm_ticket(_ticket(), {}, [_pos(P200, "1")], [], set())[0] == "PENDING"
    assert confirm_ticket(_ticket(qty="2"), {}, [_pos(P200, "-1")], [], set())[0] == "PENDING"


def test_rows_must_match_side_quantity_limit_and_contract() -> None:
    ticket = _ticket()
    for row in (
        _row(P200, Side.BUY),
        _row(P200, Side.SELL, qty="2"),
        _row(P200, Side.SELL, limit="2.05"),
        _row(P190, Side.SELL),
        _row(P200, Side.SELL, limit=None),
        WorkingOrder(P200, Side.SELL, Decimal("1"), Decimal("0"), OrderType.MARKET, Decimal("2.00"), OrderState.ACCEPTED),
    ):
        assert confirm_ticket(ticket, {}, [], [row], set())[0] == "PENDING", row


def test_a_row_is_claimed_once() -> None:
    claimed: set[int] = set()
    rows = [_row(P200, Side.SELL)]
    assert confirm_ticket(_ticket(), {}, [], rows, claimed)[0] == "ACCEPTED"
    assert confirm_ticket(_ticket(), {}, [], rows, claimed)[0] == "PENDING"


# -- the halt fold (per venue, sticky across replay) -----------------------------------


def _event(seq: int, venue: str, reconciled: bool, account: str = "OPT_CSP") -> Event:
    payload = (
        VenueReconcile(venue=venue, as_of=T, reconciled=True)
        if reconciled
        else VenueReconcile(venue=venue, as_of=T, reconciled=False, drift=(P200.symbol,))
    )
    return Event(account=account, kind=EventKind.VENUE_RECONCILE, payload=payload, ts_utc=T, seq=seq)


def test_drift_halts_that_venue_only_and_a_clean_reconcile_does_not_clear_it() -> None:
    states = fold([
        _event(1, VENUE, False),
        _event(2, VENUE, True),
        _event(3, "D-00000002", True, account="OPT_0DTE_PCS_SPX"),
    ])
    assert halted_venues(states) == frozenset({VENUE})
    assert states["OPT_CSP"].halted_venues == frozenset({VENUE})


def test_a_halt_under_any_ledger_account_halts_the_venue() -> None:
    states = fold([_event(1, "D-00000002", True, account="OPT_CSP"), _event(2, VENUE, False, account="OPT_PUT_SPREAD")])
    assert halted_venues(states) == frozenset({VENUE})


def test_clean_reconciles_halt_nothing() -> None:
    assert halted_venues(fold([_event(1, VENUE, True)])) == frozenset()


def test_halt_is_the_same_on_replay() -> None:
    events = [_event(1, VENUE, False, account="OPT_PUT_SPREAD"), _event(2, VENUE, True)]
    assert halted_venues(fold(events)) == halted_venues(fold(list(reversed(events)))) == frozenset({VENUE})

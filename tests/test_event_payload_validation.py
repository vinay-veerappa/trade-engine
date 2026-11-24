"""P0a-T8: direct constructor validation for ledger event payloads and Event (ledger/events.py).

Each refusal row builds one otherwise-valid payload or Event through its public constructor,
changes exactly one field, and asserts the exact exception type (EventPayloadError) and a
stable message fragment. Every listed raise in events.py that the P0 gate's coverage run lost
has a row (T8_LOST_LINES.txt). The positive controls at the bottom build each valid type, so
a broken fixture cannot make every refusal row pass vacuously.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from trade_engine.domain.instruments import Equity, OptionContract, OptionRight, Side
from trade_engine.domain.orders import Order, OrderState, OrderType, TimeInForce
from trade_engine.interfaces.broker import Route
from trade_engine.ledger.events import (
    CashFlow,
    EmulatedOrderState,
    EodRun,
    EodVoid,
    Event,
    EventKind,
    EventPayloadError,
    Mark,
    MirrorAck,
    MirrorAllocation,
    MirrorFill,
    MirrorQueued,
    MirrorRefused,
    OptionLifecycle,
    OrderStateChange,
    OrderUpdated,
    OrdersCreated,
    VenueReconcile,
    AccountLedgerRecord,
    AccountMembership,
)

UTC_T = datetime(2026, 11, 20, 15, 0, tzinfo=timezone.utc)
VENUE = "D-00000001"
ACCOUNT = "ACC"
ACCOUNTS = "__accounts__"
KEY = "tos:ACCT-TEST-194"
AAPL = Equity("AAPL")
PUT = OptionContract("AAPL", date(2026, 12, 18), Decimal("250"), OptionRight.PUT)


# -- valid fixtures (one per payload type) ---------------------------------------------------


def _order(order_id: str = "o1", account: str = ACCOUNT) -> Order:
    return Order(
        order_id=order_id,
        account_id=account,
        instrument=AAPL,
        order_type=OrderType.MARKET,
        side=Side.BUY,
        quantity=Decimal("10"),
        command_id=f"cmd-{order_id}",
        created_at=UTC_T,
    )


def _state_change(**overrides) -> OrderStateChange:
    fields = dict(order_id="o1", reason="venue rejected", venue_order_id="123")
    fields.update(overrides)
    return OrderStateChange(**fields)


def _orders_created(**overrides) -> OrdersCreated:
    fields = dict(orders=(_order("o1"), _order("o2")), fingerprint="fp-1", reason="bracket")
    fields.update(overrides)
    return OrdersCreated(**fields)


def _order_updated(**overrides) -> OrderUpdated:
    fields = dict(order=_order("o1"), reason="filled")
    fields.update(overrides)
    return OrderUpdated(**fields)


def _emulated(**overrides) -> EmulatedOrderState:
    fields = dict(
        order_id="o1",
        observed_price=Decimal("100"),
        extreme=Decimal("101"),
        stop_price=Decimal("99"),
        triggered=False,
        reason="emulated stop",
    )
    fields.update(overrides)
    return EmulatedOrderState(**fields)


def _cash_flow(**overrides) -> CashFlow:
    fields = dict(amount=Decimal("-1.50"), kind="fee", as_of=UTC_T, note="wire")
    fields.update(overrides)
    return CashFlow(**fields)


def _mark(**overrides) -> Mark:
    fields = dict(instrument=AAPL, price=Decimal("100"), as_of=UTC_T, source="close")
    fields.update(overrides)
    return Mark(**fields)


def _reconcile(**overrides) -> VenueReconcile:
    fields = dict(venue=VENUE, as_of=UTC_T, reconciled=True)
    fields.update(overrides)
    return VenueReconcile(**fields)


def _lifecycle(**overrides) -> OptionLifecycle:
    fields = dict(
        account_id="OPT_CSP",
        contract=PUT,
        quantity=Decimal("1"),
        held=Side.SELL,
        underlying_price=Decimal("240"),
        price_source="official settlement",
        as_of=UTC_T,
        reason="expired worthless",
    )
    fields.update(overrides)
    return OptionLifecycle(**fields)


def _eod_run(**overrides) -> EodRun:
    fields = dict(session=date(2026, 11, 20), job="eod", account_id=ACCOUNT, bars_processed=0, at_close=UTC_T)
    fields.update(overrides)
    return EodRun(**fields)


def _eod_void(**overrides) -> EodVoid:
    fields = dict(
        session=date(2026, 11, 20),
        account_id=ACCOUNT,
        reason="no chain snapshots",
        owner="owner",
        job="eod",
    )
    fields.update(overrides)
    return EodVoid(**fields)


def _record(**overrides) -> AccountLedgerRecord:
    fields = dict(
        key=KEY,
        display="*194",
        env="paper",
        status="active",
        class_=None,
        firm=None,
        account_type="margin",
        routes=(Route("tos", "ACCT-TEST-194"),),
        source="tos_paper",
        confirmed_at=UTC_T,
    )
    fields.update(overrides)
    return AccountLedgerRecord(**fields)


def _membership(**overrides) -> AccountMembership:
    fields = dict(key=KEY, display="*194", seq=1, at=UTC_T, record=None, changes=())
    fields.update(overrides)
    return AccountMembership(**fields)


def _allocation(**overrides) -> MirrorAllocation:
    fields = dict(strategy_order_id="so-1", strategy_account="OPT_CSP", quantity=Decimal("1"))
    fields.update(overrides)
    return MirrorAllocation(**fields)


def _queued(**overrides) -> MirrorQueued:
    fields = dict(
        venue=VENUE,
        ticket_key="tos:a",
        instrument=PUT,
        side=Side.SELL,
        quantity=Decimal("1"),
        order_type=OrderType.LIMIT,
        limit_price=Decimal("2.00"),
        tif=TimeInForce.DAY,
        allocations=(_allocation(),),
        at=UTC_T,
    )
    fields.update(overrides)
    return MirrorQueued(**fields)


def _refused(**overrides) -> MirrorRefused:
    fields = dict(
        venue=VENUE, strategy_order_id="so-9", strategy_account="OPT_CSP", reason="conflict", at=UTC_T
    )
    fields.update(overrides)
    return MirrorRefused(**fields)


def _ack(**overrides) -> MirrorAck:
    fields = dict(
        venue=VENUE,
        ticket_key="tos:a",
        status="ACCEPTED",
        message="read back",
        at=UTC_T,
        venue_order_id="5400000001",
        book_status=OrderState.ACCEPTED,
    )
    fields.update(overrides)
    return MirrorAck(**fields)


def _mirror_fill(**overrides) -> MirrorFill:
    fields = dict(
        venue=VENUE,
        ticket_key="tos:a",
        venue_order_id="5400000001",
        filled=Decimal("1"),
        avg_price=Decimal("2.00"),
        at=UTC_T,
    )
    fields.update(overrides)
    return MirrorFill(**fields)


def _event(payload, *, kind: EventKind, account: str, **overrides) -> Event:
    fields = dict(account=account, kind=kind, payload=payload, ts_utc=UTC_T)
    fields.update(overrides)
    return Event(**fields)


def _refusal(factory, message: str) -> None:
    """The factory must refuse with exactly EventPayloadError (not a subclass) and this message."""
    with pytest.raises(EventPayloadError, match=re.escape(message)) as info:
        factory()
    assert type(info.value) is EventPayloadError


# -- CashFlow, Mark (amount and price as Decimal) --------------------------------------------


@pytest.mark.parametrize(
    "factory, message",
    [
        pytest.param(
            lambda: _cash_flow(amount=Decimal("NaN")),
            "CashFlow.amount must be finite, got NaN",
            id="cash-flow-amount-nan",
        ),
        pytest.param(
            lambda: _mark(price=Decimal("NaN")),
            "Mark.price must be finite, got NaN",
            id="mark-price-nan",
        ),
        pytest.param(
            lambda: _mark(price=Decimal("Infinity")),
            "Mark.price must be finite, got Infinity",
            id="mark-price-infinity",
        ),
        pytest.param(
            lambda: _mark(instrument="AAPL"),
            "Mark.instrument must be an Instrument (I6)",
            id="mark-instrument-is-a-string",
        ),
        pytest.param(
            lambda: _mark(price=Decimal("0")),
            "Mark.price must be positive, got 0 (I5)",
            id="mark-price-zero",
        ),
        pytest.param(
            lambda: _mark(price=Decimal("-1")),
            "Mark.price must be positive, got -1 (I5)",
            id="mark-price-negative",
        ),
    ],
)
def test_cash_flow_and_mark_refuse_a_bad_field(factory, message) -> None:
    _refusal(factory, message)


# -- OrderStateChange, OrdersCreated, OrderUpdated ------------------------------------------


@pytest.mark.parametrize(
    "factory, message",
    [
        pytest.param(
            lambda: _state_change(order_id=""),
            "OrderStateChange.order_id must be non-empty",
            id="state-change-empty-order-id",
        ),
        pytest.param(
            lambda: _state_change(reason=""),
            "OrderStateChange.reason must be non-empty when provided",
            id="state-change-empty-reason",
        ),
        pytest.param(
            lambda: _orders_created(orders=()),
            "OrdersCreated.orders must not be empty",
            id="orders-created-no-orders",
        ),
        pytest.param(
            lambda: _orders_created(fingerprint=""),
            "OrdersCreated.fingerprint must be non-empty",
            id="orders-created-empty-fingerprint",
        ),
        pytest.param(
            lambda: _orders_created(reason=""),
            "OrdersCreated.reason must be non-empty",
            id="orders-created-empty-reason",
        ),
        pytest.param(
            lambda: _orders_created(orders=(_order("o1", "ACC"), _order("o2", "OTHER"))),
            "OrdersCreated orders must belong to one account",
            id="orders-created-two-accounts",
        ),
        pytest.param(
            lambda: _order_updated(reason=""),
            "OrderUpdated.reason must be non-empty",
            id="order-updated-empty-reason",
        ),
    ],
)
def test_order_events_refuse_a_bad_field(factory, message) -> None:
    _refusal(factory, message)


# -- EmulatedOrderState ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "factory, message",
    [
        pytest.param(
            lambda: _emulated(order_id=""),
            "EmulatedOrderState.order_id must be non-empty",
            id="emulated-empty-order-id",
        ),
        pytest.param(
            lambda: _emulated(observed_price=Decimal("0")),
            "EmulatedOrderState.observed_price must be positive",
            id="emulated-observed-price-zero",
        ),
        pytest.param(
            lambda: _emulated(observed_price=Decimal("-4")),
            "EmulatedOrderState.observed_price must be positive",
            id="emulated-observed-price-negative",
        ),
        pytest.param(
            lambda: _emulated(extreme=Decimal("0")),
            "EmulatedOrderState.extreme must be positive",
            id="emulated-extreme-zero",
        ),
        pytest.param(
            lambda: _emulated(stop_price=Decimal("0")),
            "EmulatedOrderState.stop_price must be positive",
            id="emulated-stop-price-zero",
        ),
        pytest.param(
            lambda: _emulated(reason=""),
            "EmulatedOrderState.reason must be non-empty",
            id="emulated-empty-reason",
        ),
    ],
)
def test_emulated_order_state_refuses_a_bad_field(factory, message) -> None:
    _refusal(factory, message)


# -- VenueReconcile --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "factory, message",
    [
        pytest.param(
            lambda: _reconcile(venue=""),
            "VenueReconcile.venue must be non-empty",
            id="reconcile-empty-venue",
        ),
        pytest.param(
            lambda: _reconcile(reconciled=True, drift=("AAPL",)),
            "VenueReconcile cannot be reconciled while listing drift instruments",
            id="reconcile-clean-but-lists-drift",
        ),
    ],
)
def test_venue_reconcile_refuses_a_bad_field(factory, message) -> None:
    _refusal(factory, message)


# -- OptionLifecycle -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "factory, message",
    [
        pytest.param(
            lambda: _lifecycle(account_id=""),
            "OptionLifecycle.account_id must be non-empty",
            id="lifecycle-empty-account-id",
        ),
        pytest.param(
            lambda: _lifecycle(contract="AAPL"),
            "OptionLifecycle.contract must be an OptionContract (I6)",
            id="lifecycle-contract-is-a-string",
        ),
        pytest.param(
            lambda: _lifecycle(quantity=Decimal("0")),
            "OptionLifecycle.quantity must be a positive whole number of contracts, got 0",
            id="lifecycle-quantity-zero",
        ),
        pytest.param(
            lambda: _lifecycle(quantity=Decimal("1.5")),
            "OptionLifecycle.quantity must be a positive whole number of contracts, got 1.5",
            id="lifecycle-quantity-fractional",
        ),
        pytest.param(
            lambda: _lifecycle(quantity=Decimal("-1")),
            "OptionLifecycle.quantity must be a positive whole number of contracts, got -1",
            id="lifecycle-quantity-negative",
        ),
        pytest.param(
            lambda: _lifecycle(held="BUY"),
            "OptionLifecycle.held must be a Side, got 'BUY' (I5)",
            id="lifecycle-held-is-a-string",
        ),
        pytest.param(
            lambda: _lifecycle(underlying_price=Decimal("0")),
            "OptionLifecycle.underlying_price must be positive, got 0 (I5)",
            id="lifecycle-underlying-price-zero",
        ),
        pytest.param(
            lambda: _lifecycle(price_source=""),
            "OptionLifecycle.price_source must be non-empty (I11)",
            id="lifecycle-empty-price-source",
        ),
        pytest.param(
            lambda: _lifecycle(reason=""),
            "OptionLifecycle.reason must be non-empty (I11)",
            id="lifecycle-empty-reason",
        ),
        pytest.param(
            lambda: _lifecycle(early="yes"),
            "OptionLifecycle.early must be a bool",
            id="lifecycle-early-not-bool",
        ),
    ],
)
def test_option_lifecycle_refuses_a_bad_field(factory, message) -> None:
    _refusal(factory, message)


# -- EodRun, EodVoid -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "factory, message",
    [
        pytest.param(lambda: _eod_run(job=""), "EodRun.job must be non-empty", id="eod-run-empty-job"),
        pytest.param(
            lambda: _eod_run(account_id=""),
            "EodRun.account_id must be non-empty",
            id="eod-run-empty-account-id",
        ),
        pytest.param(
            lambda: _eod_run(bars_processed=-1),
            "EodRun.bars_processed must be non-negative",
            id="eod-run-negative-bars",
        ),
        pytest.param(
            lambda: _eod_void(account_id=""),
            "EodVoid.account_id must be non-empty",
            id="eod-void-empty-account-id",
        ),
        pytest.param(lambda: _eod_void(reason=""), "EodVoid.reason must be non-empty (I11)", id="eod-void-empty-reason"),
        pytest.param(lambda: _eod_void(owner=""), "EodVoid.owner must be non-empty (I11)", id="eod-void-empty-owner"),
        pytest.param(lambda: _eod_void(job=""), "EodVoid.job must be non-empty", id="eod-void-empty-job"),
    ],
)
def test_eod_payloads_refuse_a_bad_field(factory, message) -> None:
    _refusal(factory, message)


# -- AccountLedgerRecord ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "factory, message",
    [
        pytest.param(
            lambda: _record(key="tos-ACCT-TEST-194"),
            "an account key must be 'broker:accountId'",
            id="record-key-without-colon",
        ),
        pytest.param(
            lambda: _record(display="*000"),
            "display must be the last three characters of the account id (*194), got '*000'",
            id="record-display-not-the-mask",
        ),
        pytest.param(
            lambda: _record(class_="riskguard"),
            "AccountLedgerRecord.class and .firm are riskguard overlays; the engine sends null",
            id="record-class-overlay-set",
        ),
        pytest.param(
            lambda: _record(firm="firm-x"),
            "AccountLedgerRecord.class and .firm are riskguard overlays; the engine sends null",
            id="record-firm-overlay-set",
        ),
        pytest.param(
            lambda: _record(env="dev"),
            "AccountLedgerRecord.env must be one of ('sim', 'paper', 'live')",
            id="record-env-not-allowed",
        ),
        pytest.param(
            lambda: _record(status="pending"),
            "AccountLedgerRecord.status must be one of ('active', 'closed')",
            id="record-status-not-allowed",
        ),
        pytest.param(
            lambda: _record(account_type="savings"),
            "AccountLedgerRecord.account_type must be null or one of ('margin', 'ira', 'cash')",
            id="record-account-type-not-allowed",
        ),
        pytest.param(
            lambda: _record(routes=("tos:ACCT-TEST-194",)),
            "AccountLedgerRecord.routes entries must be Routes",
            id="record-route-entry-is-a-string",
        ),
        pytest.param(
            lambda: _record(source=""),
            "AccountLedgerRecord.source must be non-empty",
            id="record-empty-source",
        ),
    ],
)
def test_account_record_refuses_a_bad_field(factory, message) -> None:
    _refusal(factory, message)


# -- AccountMembership -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "factory, message",
    [
        pytest.param(
            lambda: _membership(key="tos-ACCT-TEST-194"),
            "an account key must be 'broker:accountId'",
            id="membership-key-without-colon",
        ),
        pytest.param(
            lambda: _membership(display="*000"),
            "display must be the last three characters of the account id (*194), got '*000'",
            id="membership-display-not-the-mask",
        ),
        pytest.param(
            lambda: _membership(seq=0),
            "AccountMembership.seq must be a positive int, got 0",
            id="membership-seq-zero",
        ),
        pytest.param(
            lambda: _membership(seq=True),
            "AccountMembership.seq must be a positive int, got True",
            id="membership-seq-bool",
        ),
        pytest.param(
            lambda: _membership(record="not a record"),
            "AccountMembership.record must be an AccountLedgerRecord or None",
            id="membership-record-is-a-string",
        ),
        pytest.param(
            lambda: _membership(changes=(("env",),)),
            "AccountMembership.changes entries must be (name, old, new)",
            id="membership-change-wrong-arity",
        ),
        pytest.param(
            lambda: _membership(changes=(("bogus", "a", "b"),)),
            "AccountMembership change 'bogus' is not a diff field",
            id="membership-change-not-a-diff-field",
        ),
        pytest.param(
            lambda: _membership(changes=(("env", 5, "sim"),)),
            "AccountMembership env old must be a string, null or a list of routes, got int",
            id="membership-change-old-value-is-int",
        ),
        pytest.param(
            lambda: _membership(changes=(("routes", None, ["tos:x"]),)),
            "AccountMembership routes new must be a string, null or a list of routes, got list",
            id="membership-change-new-value-is-list",
        ),
    ],
)
def test_membership_refuses_a_bad_field(factory, message) -> None:
    _refusal(factory, message)


# -- MirrorAllocation, MirrorQueued, MirrorRefused, MirrorAck, MirrorFill --------------------


@pytest.mark.parametrize(
    "factory, message",
    [
        pytest.param(
            lambda: _allocation(strategy_order_id=""),
            "MirrorAllocation.strategy_order_id must be non-empty",
            id="allocation-empty-strategy-order-id",
        ),
        pytest.param(
            lambda: _allocation(strategy_account=""),
            "MirrorAllocation.strategy_account must be non-empty",
            id="allocation-empty-strategy-account",
        ),
        pytest.param(
            lambda: _queued(venue=""),
            "MirrorQueued.venue must be non-empty",
            id="queued-empty-venue",
        ),
        pytest.param(
            lambda: _queued(ticket_key=""),
            "MirrorQueued.ticket_key must be non-empty (I3)",
            id="queued-empty-ticket-key",
        ),
        pytest.param(
            lambda: _queued(side="SELL"),
            "MirrorQueued.side must be a Side, got 'SELL'",
            id="queued-side-is-a-string",
        ),
        pytest.param(
            lambda: _refused(venue=""),
            "MirrorRefused.venue must be non-empty",
            id="refused-empty-venue",
        ),
        pytest.param(
            lambda: _refused(strategy_order_id=""),
            "MirrorRefused.strategy_order_id must be non-empty",
            id="refused-empty-strategy-order-id",
        ),
        pytest.param(
            lambda: _refused(strategy_account=""),
            "MirrorRefused.strategy_account must be non-empty",
            id="refused-empty-strategy-account",
        ),
        pytest.param(
            lambda: _ack(venue=""),
            "MirrorAck.venue must be non-empty",
            id="ack-empty-venue",
        ),
        pytest.param(
            lambda: _ack(ticket_key=""),
            "MirrorAck.ticket_key must be non-empty",
            id="ack-empty-ticket-key",
        ),
        pytest.param(
            lambda: _mirror_fill(venue=""),
            "MirrorFill.venue must be non-empty",
            id="mirror-fill-empty-venue",
        ),
        pytest.param(
            lambda: _mirror_fill(ticket_key=""),
            "MirrorFill.ticket_key must be non-empty",
            id="mirror-fill-empty-ticket-key",
        ),
    ],
)
def test_mirror_payloads_refuse_a_bad_field(factory, message) -> None:
    _refusal(factory, message)


# -- Event -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "factory, message",
    [
        pytest.param(
            lambda: _event(_mark(), kind=EventKind.MARK, account=""),
            "Event.account must be non-empty",
            id="event-empty-account",
        ),
        pytest.param(
            lambda: _event(_mark(), kind=EventKind.MARK, account=ACCOUNT, command_id=""),
            "Event.command_id must be non-empty when provided (I3)",
            id="event-empty-command-id",
        ),
        pytest.param(
            lambda: _event(_mark(), kind=EventKind.MARK, account=ACCOUNT, schema_version=0),
            "Event.schema_version must be a positive int, got 0",
            id="event-schema-version-zero",
        ),
        pytest.param(
            lambda: _event(_mark(), kind=EventKind.MARK, account=ACCOUNT, schema_version=-3),
            "Event.schema_version must be a positive int, got -3",
            id="event-schema-version-negative",
        ),
        pytest.param(
            lambda: _event(_mark(), kind=EventKind.MARK, account=ACCOUNT, seq=0),
            "Event.seq must be a positive int, got 0",
            id="event-seq-zero",
        ),
        pytest.param(
            lambda: _event(_membership(record=_record()), kind=EventKind.ACCOUNT_ADDED, account="OPT_CSP"),
            "AccountAdded must be filed under '__accounts__', not 'OPT_CSP' (I8)",
            id="event-account-added-filed-under-a-trading-account",
        ),
        pytest.param(
            lambda: _event(_membership(record=None), kind=EventKind.ACCOUNT_ADDED, account=ACCOUNTS),
            "AccountAdded must carry the whole record (contract section 3)",
            id="event-account-added-without-record",
        ),
        pytest.param(
            lambda: _event(_membership(changes=()), kind=EventKind.ACCOUNT_CHANGED, account=ACCOUNTS),
            "AccountChanged must name at least one changed field (I5)",
            id="event-account-changed-without-changes",
        ),
        pytest.param(
            lambda: _event(
                _orders_created(orders=(_order("o1", ACCOUNT),)),
                kind=EventKind.ORDERS_CREATED,
                account="OTHER",
            ),
            "OrdersCreated contains orders for a different account (I8)",
            id="event-orders-created-orders-for-another-account",
        ),
    ],
)
def test_event_refuses_a_bad_field(factory, message) -> None:
    _refusal(factory, message)


# -- positive controls: one valid construction per payload type and per event shape ----------


@pytest.mark.parametrize(
    "build, expected_type",
    [
        pytest.param(lambda: _state_change(), OrderStateChange, id="valid-order-state-change"),
        pytest.param(lambda: _orders_created(), OrdersCreated, id="valid-orders-created"),
        pytest.param(lambda: _order_updated(), OrderUpdated, id="valid-order-updated"),
        pytest.param(lambda: _emulated(), EmulatedOrderState, id="valid-emulated-order-state"),
        pytest.param(lambda: _cash_flow(), CashFlow, id="valid-cash-flow"),
        pytest.param(lambda: _mark(), Mark, id="valid-mark"),
        pytest.param(lambda: _reconcile(), VenueReconcile, id="valid-venue-reconcile-clean"),
        pytest.param(
            lambda: _reconcile(reconciled=False, drift=("AAPL",)),
            VenueReconcile,
            id="valid-venue-reconcile-drift",
        ),
        pytest.param(lambda: _lifecycle(), OptionLifecycle, id="valid-option-lifecycle"),
        pytest.param(lambda: _eod_run(), EodRun, id="valid-eod-run"),
        pytest.param(lambda: _eod_void(), EodVoid, id="valid-eod-void"),
        pytest.param(lambda: _record(), AccountLedgerRecord, id="valid-account-ledger-record"),
        pytest.param(lambda: _membership(record=_record()), AccountMembership, id="valid-membership-added"),
        pytest.param(
            lambda: _membership(changes=(("env", "paper", "live"),)),
            AccountMembership,
            id="valid-membership-changed",
        ),
        pytest.param(lambda: _allocation(), MirrorAllocation, id="valid-mirror-allocation"),
        pytest.param(lambda: _queued(), MirrorQueued, id="valid-mirror-queued"),
        pytest.param(lambda: _refused(), MirrorRefused, id="valid-mirror-refused"),
        pytest.param(lambda: _ack(), MirrorAck, id="valid-mirror-ack"),
        pytest.param(lambda: _mirror_fill(), MirrorFill, id="valid-mirror-fill"),
    ],
)
def test_valid_payloads_construct(build, expected_type) -> None:
    assert isinstance(build(), expected_type)


@pytest.mark.parametrize(
    "event",
    [
        pytest.param(_event(_mark(), kind=EventKind.MARK, account=ACCOUNT, seq=1), id="valid-event-mark"),
        pytest.param(
            _event(_membership(record=_record()), kind=EventKind.ACCOUNT_ADDED, account=ACCOUNTS),
            id="valid-event-account-added",
        ),
        pytest.param(
            _event(_membership(changes=(("env", "paper", "live"),)), kind=EventKind.ACCOUNT_CHANGED, account=ACCOUNTS),
            id="valid-event-account-changed",
        ),
        pytest.param(
            _event(_orders_created(orders=(_order("o1", ACCOUNT),)), kind=EventKind.ORDERS_CREATED, account=ACCOUNT),
            id="valid-event-orders-created",
        ),
    ],
)
def test_valid_events_construct_and_keep_their_account(event) -> None:
    assert isinstance(event, Event)
    assert event.account in (ACCOUNT, ACCOUNTS)
    assert event.ts_utc.utcoffset().total_seconds() == 0

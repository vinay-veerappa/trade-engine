"""Deterministic populated synthetic books; never opens a real ledger."""
from datetime import timedelta
from decimal import Decimal

from ledger_gen import T0, _order, _fill
from trade_engine.domain.instruments import Equity
from trade_engine.ledger.events import CashFlow, Event, EventKind


def populated_book(accounts=3, positions=120, cash_rows=1000):
    events=[]
    for account_index in range(accounts):
        account=f"SYNTHETIC-{account_index}"
        for index in range(positions):
            instrument=Equity(f"SYN{index:04d}")
            order_id=f"{account}-o{index}"
            order=_order(order_id,account,instrument=instrument,quantity=Decimal("10.00"))
            fill=_fill(f"{order_id}-fill",order_id,account,instrument=instrument,
                       quantity=Decimal("10.00"),price=Decimal("123.4500"),fee=Decimal("0.070"))
            events.extend([
                Event(account=account,kind=EventKind.ORDER_SUBMITTED,payload=order,
                      ts_utc=T0,command_id=order.command_id),
                Event(account=account,kind=EventKind.FILL,payload=fill,ts_utc=T0),
            ])
    for index in range(cash_rows):
        account=f"SYNTHETIC-{index % accounts}"
        stamp=T0+timedelta(microseconds=index+1)
        events.append(Event(account=account,kind=EventKind.CASH_FLOW,
            payload=CashFlow(Decimal("2.0700"),"interest",stamp),ts_utc=stamp,
            command_id=f"seed-cash-{index}"))
    return events


def hot_events(count=60):
    for index in range(count):
        stamp=T0+timedelta(seconds=index+1,microseconds=123456)
        yield Event(account=f"SYNTHETIC-{index % 3}",kind=EventKind.CASH_FLOW,
            payload=CashFlow(Decimal("-0.0100"),"fee",stamp),ts_utc=stamp,
            command_id=f"hot-{index}")

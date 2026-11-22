"""SCAN-WRITEOFF-1: the owner-signed write-off of a stuck working order.

An accepted entry that never fills makes every session demand bars for its name; with
none to be had the I3 gate refuses every later run. The write-off cancels the entry and
its bracket children with zero P&L, and the chain catches up.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from trade_engine.calendar.sessions import ExchangeCalendar
from trade_engine.cli import main
from trade_engine.clock.replay import ReplayClock
from trade_engine.domain.instruments import Equity, Side
from trade_engine.domain.orders import OrderState
from trade_engine.domain.portfolio import Fill
from trade_engine.domain.signals import OrderIntent
from trade_engine.eod import EodRunner, EodRunnerConfig
from trade_engine.eod.runner import EodRunnerError, write_off_order
from trade_engine.ledger import Event, EventKind, Ledger, OrderStateChange
from trade_engine.oms.manager import OrderManager
from trade_engine.sim import SimBroker

UTC = timezone.utc
ACCOUNT = "scan-account"
SESSION = date(2026, 9, 23)
SESSION_OPEN = datetime(2026, 9, 23, 13, 30, tzinfo=UTC)
PREV_EOD = datetime(2026, 9, 22, 21, 45, tzinfo=UTC)
CALENDAR = ExchangeCalendar()
INSTRUMENT = Equity("ACVA")
REASON = "stop-limit never filled, no bars exist"
OWNER = "owner"


class SettableClock(ReplayClock):
    def set(self, now: datetime) -> None:
        self._current_time = now.astimezone(UTC)


class NoBarsAtAll:
    """Market data that fails the test if the session asks for any bars."""

    def __init__(self) -> None:
        self.calls = 0

    def bars(self, instrument, tf, start, end, max_age_seconds):
        self.calls += 1
        raise AssertionError(f"bars requested for {instrument}")


@pytest.fixture
def rig(tmp_path: Path):
    clock = SettableClock(PREV_EOD)
    with Ledger(tmp_path / "writeoff.db") as ledger:
        broker = SimBroker(ACCOUNT, clock, Decimal("0"))
        broker.connect()
        intent = OrderIntent(
            intent_id="intent-wo-1",
            account_id=ACCOUNT,
            instrument=INSTRUMENT,
            side=Side.BUY,
            quantity_rule="fixed_2",
            entry_price=Decimal("100"),
            stop_loss=Decimal("95"),
            profit_targets=(Decimal("110"),),
            reason="write-off seed",
            command_id="wo-1",
        )
        manager = OrderManager(broker, clock, ledger)
        bracket = manager.create_bracket(intent, Decimal("2"))
        manager.submit(bracket.entry)
        yield ledger, clock, broker, manager, bracket


def _ids(bracket) -> list[str]:
    return [bracket.entry.order_id, bracket.stop.order_id, *(t.order_id for t in bracket.targets)]


def _writeoff(ledger, clock, order_id, **over):
    args = dict(reason=REASON, owner=OWNER, account=ACCOUNT)
    args.update(over)
    account = args.pop("account")
    return write_off_order(ledger, clock, account, order_id, **args)


def _count(ledger: Ledger) -> int:
    return sum(1 for _ in ledger.events())


def test_entry_and_both_children_are_cancelled_with_zero_pnl(rig) -> None:
    ledger, clock, _b, _m, bracket = rig
    before = ledger.state(ACCOUNT)
    assert before.orders[bracket.entry.order_id].state is OrderState.ACCEPTED
    done = _writeoff(ledger, clock, bracket.entry.order_id)
    assert set(done) == set(_ids(bracket)) and done[0] == bracket.entry.order_id
    assert len(done) == 3
    after = ledger.state(ACCOUNT)
    for order_id in _ids(bracket):
        assert after.orders[order_id].state is OrderState.CANCELLED
    assert after.cash == before.cash
    assert after.realized_pnl == before.realized_pnl
    assert after.fills == before.fills
    kinds = {e.kind for e in ledger.events(account=ACCOUNT)}
    assert not kinds & {EventKind.FILL, EventKind.CASH_FLOW, EventKind.MARK, EventKind.EOD_RUN}
    text = [e.payload.reason for e in ledger.events_of_kind(EventKind.ORDER_CANCELLED)]
    assert text[0] == f"Owner write-off: {REASON} (owner: {OWNER})"
    assert text[1].endswith(f"(bracket child of {bracket.entry.order_id})")


def test_the_session_no_longer_asks_for_the_instruments_bars(rig) -> None:
    ledger, clock, broker, _m, bracket = rig
    clock.set(SESSION_OPEN - timedelta(minutes=1))
    data = NoBarsAtAll()
    runner = EodRunner(
        ledger, clock, CALENDAR, data, EodRunnerConfig(job_name="eod", brokers={ACCOUNT: broker})
    )
    assert runner._replay_instruments(ledger.state(ACCOUNT)) == (INSTRUMENT,)
    _writeoff(ledger, clock, bracket.entry.order_id)
    assert runner._replay_instruments(ledger.state(ACCOUNT)) == ()
    runner.run(SESSION)
    assert data.calls == 0


def test_a_second_call_writes_nothing(rig) -> None:
    ledger, clock, _b, _m, bracket = rig
    assert _writeoff(ledger, clock, bracket.entry.order_id)
    size = _count(ledger)
    assert _writeoff(ledger, clock, bracket.entry.order_id) == ()
    assert _count(ledger) == size


@pytest.mark.parametrize("over", [{"reason": ""}, {"reason": "  "}, {"owner": ""}, {"owner": " "}])
def test_empty_reason_or_owner_refuses(rig, over) -> None:
    ledger, clock, _b, _m, bracket = rig
    size = _count(ledger)
    with pytest.raises(EodRunnerError):
        _writeoff(ledger, clock, bracket.entry.order_id, **over)
    assert _count(ledger) == size


def test_unknown_account_and_unknown_order_refuse(rig) -> None:
    ledger, clock, _b, _m, bracket = rig
    size = _count(ledger)
    with pytest.raises(EodRunnerError, match="unknown account"):
        _writeoff(ledger, clock, bracket.entry.order_id, account="no-such")
    with pytest.raises(EodRunnerError, match="no order"):
        _writeoff(ledger, clock, "no-such-order")
    assert _count(ledger) == size


def test_an_already_cancelled_order_refuses(rig) -> None:
    ledger, clock, *_rest, bracket = rig
    ledger.append(Event(
        account=ACCOUNT, kind=EventKind.ORDER_CANCELLED,
        payload=OrderStateChange(bracket.entry.order_id, reason="plain cancel"),
        ts_utc=PREV_EOD, command_id="plain-cancel",
    ))
    size = _count(ledger)
    with pytest.raises(EodRunnerError, match="not working"):
        _writeoff(ledger, clock, bracket.entry.order_id)
    assert _count(ledger) == size


def _fill(order_id: str, quantity: str) -> Fill:
    return Fill(
        fill_id=f"fill-{order_id}-{quantity}", order_id=order_id, account_id=ACCOUNT,
        instrument=INSTRUMENT, quantity=Decimal(quantity), price=Decimal("100"),
        venue_env="sim", filled_at=PREV_EOD, side=Side.BUY,
    )


@pytest.mark.parametrize("quantity", ["1", "2"], ids=["partly-filled", "filled"])
def test_a_filled_or_partly_filled_order_refuses(rig, quantity) -> None:
    ledger, clock, _b, manager, bracket = rig
    manager.record_fill(_fill(bracket.entry.order_id, quantity))
    size = _count(ledger)
    with pytest.raises(EodRunnerError, match="position"):
        _writeoff(ledger, clock, bracket.entry.order_id)
    assert _count(ledger) == size


def _cli(path: str, *extra: str) -> int:
    return main(["eod", "--write-off", "--session", SESSION.isoformat(), "--ledger", path, *extra])


def test_cli_write_off_happy_path_and_repeat(rig, capsys) -> None:
    ledger, *_rest, bracket = rig
    path = str(ledger.path)
    ledger.close()
    argv = ["--accounts", ACCOUNT, "--order", bracket.entry.order_id, "--reason", REASON, "--owner", OWNER]
    assert _cli(path, *argv) == 0
    out = capsys.readouterr().out
    for order_id in _ids(bracket):
        assert f"written off {order_id}" in out
    assert _cli(path, *argv) == 0
    assert "already written off" in capsys.readouterr().out
    with Ledger(path) as led:
        assert led.state(ACCOUNT).orders[bracket.entry.order_id].state is OrderState.CANCELLED


@pytest.mark.parametrize(
    "extra",
    [
        ["--accounts", ACCOUNT + ",x", "--order", "o", "--reason", REASON, "--owner", OWNER],
        ["--order", "o", "--reason", REASON, "--owner", OWNER],
        ["--accounts", ACCOUNT, "--reason", REASON, "--owner", OWNER],
        ["--accounts", ACCOUNT, "--order", "no-such", "--reason", REASON, "--owner", OWNER],
        ["--accounts", ACCOUNT, "--order", "o", "--reason", "", "--owner", OWNER],
    ],
)
def test_cli_refusals_exit_2_and_write_nothing(rig, extra, capsys) -> None:
    ledger, *_rest = rig
    path = str(ledger.path)
    size = _count(ledger)
    ledger.close()
    assert _cli(path, *extra) == 2
    assert "eod:" in capsys.readouterr().err
    with Ledger(path) as led:
        assert _count(led) == size

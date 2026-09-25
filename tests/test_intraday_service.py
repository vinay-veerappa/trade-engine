"""I1: the 0DTE intraday service - heartbeat, stale-quote flat-and-refuse, resume."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from trade_engine.calendar import get_calendar
from trade_engine.clock.replay import ReplayClock
from trade_engine.domain.instruments import Combo, ComboLeg, OptionContract, OptionRight, Side
from trade_engine.domain.option_orders import OptionIntent
from trade_engine.domain.option_roots import SettleTime
from trade_engine.domain.orders import OrderState, OrderType
from trade_engine.domain.risk import RiskRuleResult, RiskVerdict
from trade_engine.eod import EodRunner, EodRunnerConfig
from trade_engine.interfaces.market_data import Greeks, OptionQuote, StaleDataError
from trade_engine.intraday import IntradayConfig, IntradayService, IntradayServiceAlert, IntradayServiceError
from trade_engine.ledger import CashFlow, EodRun, Event, EventKind, Ledger, OrderStateChange
from trade_engine.lifecycle import FixedDividends, FixedSettlements, LifecyclePass, SettlementPrice
from trade_engine.market_data.chains import ChainSnapshot
from trade_engine.sim import SnapshotVenue

D = Decimal
NY = ZoneInfo("America/New_York")
CAL = get_calendar()
ACCOUNT = "OPT_0DTE_PCS_SPX"
SESSION = date(2026, 9, 25)
NEXT = date(2026, 9, 28)
EARLY = date(2026, 11, 27)  # the day after Thanksgiving: a 13:00 close
SPOT = D("6710")
GREEKS = Greeks(delta=-0.10, gamma=0.0, theta=0.0, vega=0.0, rho=0.0, source="vendor")
BOOK = {"short": ("1.90", "2.10"), "long": ("0.90", "1.05")}


def legs(session: date) -> tuple[OptionContract, OptionContract]:
    return (
        OptionContract("SPX", session, D("6700"), OptionRight.PUT),
        OptionContract("SPX", session, D("6695"), OptionRight.PUT),
    )


SHORT, LONG = legs(SESSION)


def at_et(session: date, hh: int, mm: int, ss: int = 0) -> datetime:
    return datetime.combine(session, time(hh, mm, ss), tzinfo=NY)


def snap(
    session: date,
    hh: int,
    mm: int,
    *,
    quotes: dict | None = None,
    spot: D = SPOT,
    quoted: dict | None = None,
    underlying_quoted: datetime | None | str = "now",
) -> ChainSnapshot:
    """A chain as the hub source builds it: every quote and the underlying stamped now,
    unless ``quoted`` / ``underlying_quoted`` say a quote is older (or unknown: None)."""
    when = at_et(session, hh, mm)
    short, long = legs(session)
    book = quotes or {short: BOOK["short"], long: BOOK["long"]}
    quoted = quoted or {}
    rows = tuple(
        OptionQuote(c, D(b), D(a), D(10), D(10), quoted.get(c, when), greeks=GREEKS, underlying_price=spot)
        for c, (b, a) in book.items()
    )
    underlying_as_of = when if underlying_quoted == "now" else underlying_quoted
    return ChainSnapshot("SPX", when, spot, rows, None, None, "test", underlying_as_of=underlying_as_of)


def spread(name: str, credit: str = "0.90", session: date = SESSION, target: str | None = None) -> OptionIntent:
    short, long = legs(session)
    return OptionIntent(
        intent_id=name,
        account_id=ACCOUNT,
        instrument=Combo((ComboLeg(short, 1, Side.SELL), ComboLeg(long, 1, Side.BUY))),
        side=Side.SELL,
        quantity=D("1"),
        reason="test",
        command_id=name,
        order_type=OrderType.LIMIT,
        limit_price=D(credit),
        profit_target=None if target is None else D(target),
    )


class Approve:
    name = "approve"

    def evaluate(self, intent, context) -> RiskVerdict:
        rule = RiskRuleResult("test", True, "x", "x", "test rule")
        return RiskVerdict(intent.intent_id, evaluations=(rule,), refusal_reasons=())


class Scripted:
    """A strategy whose snapshot-phase actions the test sets per instant.

    Like a real plugin it enters only when the account is flat (the OMS refuses a
    second entry, C4). ``actions`` are keyed by the exact tick instant; ``at_or_after``
    fires once at the first tick at or after its instant; ``always`` fires every call.
    """

    name = "scripted"

    def __init__(self, ledger: Ledger | None = None) -> None:
        self.actions: dict[datetime, list] = {}
        self.at_or_after: dict[datetime, list] = {}
        self.always: list = []
        self.contexts: list = []
        self.ledger = ledger
        self.raise_at: datetime | None = None

    def _flat(self) -> bool:
        if self.ledger is None:
            return True
        state = self.ledger.state(ACCOUNT)
        if any(position.quantity != 0 for position in state.positions.values()):
            return False
        return not any(
            order.parent_order_id is None
            and order.state in (OrderState.NEW, OrderState.SUBMITTED, OrderState.ACCEPTED,
                                OrderState.PARTIALLY_FILLED, OrderState.PENDING_UNKNOWN)
            for order in state.orders.values()
        )

    def manage_options(self, context):
        self.contexts.append(context)
        if self.raise_at is not None and context.now >= self.raise_at:
            raise RuntimeError("strategy bug")
        if self.always:
            return list(self.always)
        if not self._flat():
            return []
        if context.now in self.actions:
            return list(self.actions[context.now])
        due = sorted(when for when in self.at_or_after if context.now >= when)
        if due:
            return list(self.at_or_after.pop(due[0]))
        return []


class Rig:
    def __init__(self, tmp_path: Path, session: date = SESSION, name: str = "ledger.db", *, fund: bool = True) -> None:
        self.tmp_path = tmp_path
        self.session = session
        self.clock = ReplayClock(CAL.session_open(session))
        self.path = tmp_path / name
        self.ledger = Ledger(self.path).open()
        self.strategy = Scripted(self.ledger)
        self.venue = SnapshotVenue(ACCOUNT, self.clock)
        self.snapshots: list[ChainSnapshot] = []
        self.heartbeat = tmp_path / f"{name}.heartbeat.json"
        self.source_calls: list[datetime] = []
        if fund:
            self.ledger.append(
                Event(
                    account=ACCOUNT, kind=EventKind.CASH_FLOW, ts_utc=self.clock.now_utc(), command_id="deposit",
                    payload=CashFlow(amount=D("50000"), kind="deposit", as_of=self.clock.now_utc()),
                )
            )

    def source(self, underlying: str, now: datetime) -> ChainSnapshot:
        self.source_calls.append(now)
        known = [s for s in self.snapshots if s.as_of <= now]
        if not known:
            raise StaleDataError(f"No {underlying} snapshot at or before {now.isoformat()} (I5)")
        return known[-1]

    def config(self, **changes) -> IntradayConfig:
        values = dict(
            job_name="intraday", account_id=ACCOUNT, underlying="SPX", broker=self.venue,
            strategy=self.strategy, option_risk_engine=Approve(), snapshot_source=self.source,
            eod_job_name="options", max_quote_age_seconds=30.0, tick_seconds=60.0,
        )
        values.update(changes)
        return IntradayConfig(**values)

    def service(self, **changes) -> IntradayService:
        return IntradayService(self.ledger, self.clock, CAL, self.config(**changes), heartbeat_path=self.heartbeat)

    def restart(self, **changes) -> IntradayService:
        """A new process: the ledger reopened, a fresh venue and strategy, same clock.

        The dead process's last heartbeat is still "fresh" at this clock reading; the
        supervisor that restarts it has seen the process die, so it is removed here
        (the TTL path itself is tested on its own).
        """
        self.heartbeat.unlink(missing_ok=True)
        self.ledger.close()
        self.ledger = Ledger(self.path).open()
        actions, later = self.strategy.actions, self.strategy.at_or_after
        self.strategy = Scripted(self.ledger)
        self.strategy.actions, self.strategy.at_or_after = actions, later
        self.venue = SnapshotVenue(ACCOUNT, self.clock)
        return self.service(**changes)

    def minutes(self, start: tuple[int, int], end: tuple[int, int], quotes: dict | None = None, **kwargs) -> None:
        when = at_et(self.session, *start)
        while when <= at_et(self.session, *end):
            local = when.astimezone(NY)
            self.snapshots.append(snap(self.session, local.hour, local.minute, quotes=quotes, **kwargs))
            when += timedelta(minutes=1)
        self.snapshots.sort(key=lambda s: s.as_of)

    @property
    def state(self):
        return self.ledger.state(ACCOUNT)

    def held(self, instrument) -> Decimal:
        position = self.state.positions.get(instrument)
        return D(0) if position is None else position.quantity

    def events(self, fragment: str = "", kind: EventKind | None = None) -> list:
        return [
            e for e in self.ledger.events(account=ACCOUNT)
            if fragment in (e.command_id or "") and (kind is None or e.kind is kind)
        ]

    def heartbeat_json(self) -> dict:
        return json.loads(self.heartbeat.read_text(encoding="utf-8"))


@pytest.fixture
def rig(tmp_path):
    r = Rig(tmp_path)
    yield r
    r.ledger.close()


class _Crash(BaseException):
    """A process death: nothing in the service catches a BaseException."""


# -- the stale-quote gate ------------------------------------------------------------


def test_a_stale_quote_closes_open_structures(rig) -> None:
    """Stale quote => the flatten close is submitted for what is open (I5)."""
    rig.strategy.actions[at_et(SESSION, 9, 40)] = [spread("o-1")]
    rig.snapshots = [snap(SESSION, 9, 40)]  # nothing after: from 09:41 every tick is stale
    rig.service().run(SESSION, start_at=at_et(SESSION, 9, 39))
    assert rig.held(SHORT) == -1 and rig.held(LONG) == 1  # nothing fresh to fill the close on
    closes = rig.events("stale-quote", EventKind.ORDERS_CREATED)
    assert len(closes) == 1 and closes[0].payload.orders[0].order_type is OrderType.MARKET


def test_stale_ticks_never_ask_the_strategy_and_enter_nothing(rig) -> None:
    """A stale quote prices nothing: the strategy is not called, so it cannot enter (I5)."""
    rig.strategy.actions[at_et(SESSION, 9, 40)] = [spread("o-1")]
    rig.strategy.actions[at_et(SESSION, 9, 45)] = [spread("o-2")]  # a stale instant
    rig.snapshots = [snap(SESSION, 9, 40), snap(SESSION, 9, 50)]
    rig.service().run(SESSION, start_at=at_et(SESSION, 9, 39), stop_at=at_et(SESSION, 9, 52))
    asked = {context.now for context in rig.strategy.contexts}
    assert asked == {at_et(SESSION, 9, 40), at_et(SESSION, 9, 50)}  # 09:51 sees a 60s-old chain
    assert not rig.events("o-2")  # neither ordered nor judged by risk
    assert rig.events("o-1", EventKind.ORDERS_CREATED)


def test_a_snapshot_that_does_not_say_when_the_underlying_was_quoted_refuses(rig) -> None:
    rig.strategy.at_or_after[at_et(SESSION, 9, 40)] = [spread("o-1")]
    rig.snapshots = [snap(SESSION, 9, 40, underlying_quoted=None)]
    rig.service().run(SESSION, start_at=at_et(SESSION, 9, 40), stop_at=at_et(SESSION, 9, 41))
    assert rig.strategy.contexts == [] and not rig.events("o-1")
    beat = rig.heartbeat_json()
    assert beat["refusing"] is True and "does not say when" in beat["note"]


def test_an_old_underlying_quote_in_a_new_snapshot_refuses(rig) -> None:
    """The chain arrived now, but the underlying was last quoted 31s ago: not live (I5)."""
    rig.strategy.at_or_after[at_et(SESSION, 9, 40)] = [spread("o-1")]
    rig.snapshots = [snap(SESSION, 9, 40, underlying_quoted=at_et(SESSION, 9, 39, 29))]
    rig.service().run(SESSION, start_at=at_et(SESSION, 9, 40), stop_at=at_et(SESSION, 9, 41))
    assert rig.strategy.contexts == [] and "last quoted 31s ago" in rig.heartbeat_json()["note"]
    # 30s old is still inside the limit: the same snapshot trades.
    fresh = Rig(rig.tmp_path, name="fresh.db")
    fresh.strategy.at_or_after[at_et(SESSION, 9, 40)] = [spread("o-1")]
    fresh.snapshots = [snap(SESSION, 9, 40, underlying_quoted=at_et(SESSION, 9, 39, 30))]
    fresh.service().run(SESSION, start_at=at_et(SESSION, 9, 40), stop_at=at_et(SESSION, 9, 41))
    assert fresh.events("o-1", EventKind.ORDERS_CREATED)
    fresh.ledger.close()


def test_a_snapshot_stamped_after_now_is_refused(rig) -> None:
    """A chain from the future is a clock fault, not a quote (I5, I7)."""
    rig.strategy.at_or_after[at_et(SESSION, 9, 40)] = [spread("o-1")]
    ahead = snap(SESSION, 9, 40, underlying_quoted=at_et(SESSION, 9, 39, 50))
    service = rig.service(snapshot_source=lambda underlying, now: ahead)
    service.run(SESSION, start_at=at_et(SESSION, 9, 39, 59), stop_at=at_et(SESSION, 9, 40))
    assert rig.strategy.contexts == [] and "after now" in rig.heartbeat_json()["note"]


def test_a_stale_held_leg_flattens_and_refuses_until_it_is_quoted_again(rig) -> None:
    """The chain and the underlying are live, but the held long put has not been quoted
    since 09:40: the account cannot be priced, so it flattens and refuses (I5)."""
    rig.strategy.actions[at_et(SESSION, 9, 40)] = [spread("o-1")]
    rig.snapshots = [snap(SESSION, 9, 40)]
    for mm in range(41, 45):
        rig.snapshots.append(snap(SESSION, 9, mm, quoted={LONG: at_et(SESSION, 9, 40)}))
    rig.snapshots.append(snap(SESSION, 9, 45))
    rig.service().run(SESSION, start_at=at_et(SESSION, 9, 40), stop_at=at_et(SESSION, 9, 46))
    [close] = rig.events("stale-quote", EventKind.ORDERS_CREATED)
    assert close.ts_utc == at_et(SESSION, 9, 41)
    fill = next(f for f in rig.state.fills if f.order_id == close.payload.orders[0].order_id)
    assert fill.filled_at == at_et(SESSION, 9, 45)  # not before its legs were quoted again
    assert rig.held(SHORT) == 0 and rig.held(LONG) == 0


def test_a_held_leg_missing_from_the_chain_flattens_and_refuses(rig) -> None:
    rig.strategy.actions[at_et(SESSION, 9, 40)] = [spread("o-1")]
    rig.snapshots = [snap(SESSION, 9, 40), snap(SESSION, 9, 41, quotes={SHORT: BOOK["short"]})]
    rig.service().run(SESSION, start_at=at_et(SESSION, 9, 40), stop_at=at_et(SESSION, 9, 42))
    assert rig.events("stale-quote", EventKind.ORDERS_CREATED)
    assert "is not in the SPX snapshot" in rig.heartbeat_json()["note"]


def test_a_stale_candidate_leg_is_never_shown_to_the_strategy(rig) -> None:
    """Flat, a live chain: the one stale row is cut out before the strategy sees it."""
    rig.snapshots = [snap(SESSION, 9, 40, quoted={LONG: at_et(SESSION, 9, 39)})]
    rig.service().run(SESSION, start_at=at_et(SESSION, 9, 40), stop_at=at_et(SESSION, 9, 41))
    [context] = rig.strategy.contexts
    assert context.snapshot.get(SHORT) is not None and context.snapshot.get(LONG) is None


# -- working entries are cancelled -----------------------------------------------------


def test_a_stale_quote_cancels_a_working_entry(rig) -> None:
    rig.strategy.actions[at_et(SESSION, 9, 40)] = [spread("o-1", credit="5.00")]  # never fills
    rig.snapshots = [snap(SESSION, 9, 40)]
    rig.service().run(SESSION, start_at=at_et(SESSION, 9, 40), stop_at=at_et(SESSION, 9, 42))
    assert rig.state.orders["o-1:entry"].state is OrderState.CANCELLED
    assert rig.events("intraday:cancel-entry:o-1:entry:stale-quote")


def test_the_entry_end_cancels_a_working_entry_and_drops_later_entries(rig) -> None:
    rig.minutes((11, 55), (12, 3))
    rig.strategy.actions[at_et(SESSION, 11, 58)] = [spread("o-1", credit="5.00")]
    rig.strategy.actions[at_et(SESSION, 12, 2)] = [spread("o-2")]
    service = rig.service()
    service.run(SESSION, start_at=at_et(SESSION, 11, 55), stop_at=at_et(SESSION, 12, 0, 30))
    assert rig.state.orders["o-1:entry"].state is OrderState.ACCEPTED  # 12:00 is still inside
    result = rig.restart().run(SESSION, stop_at=at_et(SESSION, 12, 3, 30))
    assert rig.state.orders["o-1:entry"].state is OrderState.CANCELLED
    [cancel] = rig.events("intraday:cancel-entry:o-1:entry:entry-end", EventKind.ORDER_CANCELLED)
    assert cancel.ts_utc == at_et(SESSION, 12, 1)
    assert not rig.events("o-2") and result["entries_dropped"] >= 1


def test_the_sweep_cancels_a_working_entry(rig) -> None:
    rig.minutes((15, 28), (15, 31))
    rig.strategy.actions[at_et(SESSION, 15, 29)] = [spread("o-1", credit="5.00")]
    rig.service(entry_end=time(15, 30), entry_before_close=timedelta(minutes=30)).run(
        SESSION, start_at=at_et(SESSION, 15, 28), stop_at=at_et(SESSION, 15, 31, 30)
    )
    assert rig.state.orders["o-1:entry"].state is OrderState.CANCELLED
    [cancel] = rig.events("intraday:cancel-entry:o-1:entry:flat-sweep", EventKind.ORDER_CANCELLED)
    assert cancel.ts_utc == at_et(SESSION, 15, 30)


# -- the sweep and the early close ------------------------------------------------------


def test_the_1530_sweep_flattens_what_is_still_open(rig) -> None:
    """Flat 15:30: the sweep closes what the strategy left open (rules doc §6.4)."""
    # Snapshots every 7 minutes all day, so a 900s limit never goes stale; the entry
    # fills at 09:40 and its profit target never reaches, so it holds to 15:30.
    rig.snapshots = [snap(SESSION, hh, mm) for hh in range(9, 16) for mm in range(0, 60, 7) if (hh, mm) >= (9, 40)]
    rig.minutes((15, 25), (15, 35))
    rig.strategy.at_or_after[at_et(SESSION, 9, 40)] = [spread("o-1")]
    rig.service(max_quote_age_seconds=900.0, tick_seconds=300.0).run(SESSION, start_at=at_et(SESSION, 9, 40))
    assert rig.held(SHORT) == 0 and rig.held(LONG) == 0
    [flat] = rig.events("flat-sweep", EventKind.ORDERS_CREATED)
    assert flat.ts_utc == at_et(SESSION, 15, 30)


def test_a_flatten_the_venue_rejects_is_asked_again_under_a_new_id(rig) -> None:
    """A long put quoted 0/0 cannot be sold at market: the close is rejected and the
    structure stays open, so the next sweep asks again rather than replaying the id."""
    rig.strategy.actions[at_et(SESSION, 15, 28)] = [spread("o-1")]
    rig.minutes((15, 28), (15, 29))
    rig.minutes((15, 30), (15, 31), quotes={SHORT: BOOK["short"], LONG: ("0", "0")})
    rig.minutes((15, 32), (15, 33))
    rig.service(entry_end=time(15, 30), entry_before_close=timedelta(minutes=30)).run(
        SESSION, start_at=at_et(SESSION, 15, 28), stop_at=at_et(SESSION, 15, 33, 30)
    )
    closes = sorted(o for o in rig.state.orders if o.startswith("o-1:entry:close:"))
    states = [rig.state.orders[o].state for o in closes]
    assert states[:2] == [OrderState.REJECTED, OrderState.REJECTED] and states[-1] is OrderState.FILLED
    assert rig.held(SHORT) == 0 and rig.held(LONG) == 0


def test_the_deadlines_follow_the_rules_on_a_full_day_and_pull_in_on_an_early_close(rig) -> None:
    service = rig.service()
    assert service._deadlines(SESSION) == (at_et(SESSION, 12, 0), at_et(SESSION, 15, 30))
    assert service._deadlines(EARLY) == (at_et(EARLY, 11, 30), at_et(EARLY, 12, 30))
    assert service._deadlines(date(2026, 12, 24)) == (at_et(date(2026, 12, 24), 11, 30), at_et(date(2026, 12, 24), 12, 30))


def test_an_early_close_sweeps_at_1230_and_ends_entries_at_1130(tmp_path) -> None:
    rig = Rig(tmp_path, session=EARLY)
    short, long = legs(EARLY)
    rig.minutes((11, 25), (12, 35))
    rig.strategy.actions[at_et(EARLY, 11, 30)] = [spread("o-1", session=EARLY)]
    rig.service(max_quote_age_seconds=900.0).run(EARLY, start_at=at_et(EARLY, 11, 25))
    assert rig.state.orders["o-1:entry"].state is OrderState.FILLED  # 11:30 is still inside
    [flat] = rig.events("flat-sweep", EventKind.ORDERS_CREATED)
    assert flat.ts_utc == at_et(EARLY, 12, 30)
    assert rig.held(short) == 0 and rig.held(long) == 0
    rig.ledger.close()

    late = Rig(tmp_path, session=EARLY, name="late.db")
    late.minutes((11, 25), (11, 35))
    late.strategy.actions[at_et(EARLY, 11, 31)] = [spread("o-1", session=EARLY)]
    result = late.service().run(EARLY, start_at=at_et(EARLY, 11, 25), stop_at=at_et(EARLY, 11, 33))
    assert not late.events("o-1") and result["entries_dropped"] == 1
    late.ledger.close()


# -- heartbeat ------------------------------------------------------------------------


def write_heartbeat(rig: Rig, **fields) -> None:
    body = {"account_id": ACCOUNT, "session": SESSION.isoformat(), "at_utc": rig.clock.now_utc().isoformat(),
            "refusing": False, "note": "ok"}
    body.update(fields)
    rig.heartbeat.write_text(json.dumps(body), encoding="utf-8")


def test_the_heartbeat_latches_refusing(rig) -> None:
    rig.snapshots = [snap(SESSION, 9, 40)]
    rig.strategy.actions[at_et(SESSION, 9, 40)] = [spread("o-1")]
    rig.service().run(SESSION, start_at=at_et(SESSION, 9, 39), stop_at=at_et(SESSION, 9, 45))
    raw = rig.heartbeat_json()
    assert raw["account_id"] == ACCOUNT and raw["session"] == SESSION.isoformat()
    assert raw["refusing"] is True and "stale" in raw["note"] and raw["exited"] is False


def test_the_heartbeat_is_written_before_the_open_and_marked_exited_at_the_close(rig) -> None:
    class Waiting:
        """A wall-like clock (no advance_to): the service waits for the open."""

        def __init__(self, start: datetime) -> None:
            self.now = start

        def now_utc(self) -> datetime:
            return self.now

        def sleep(self, seconds: float) -> None:
            self.now += timedelta(seconds=seconds)

    clock = Waiting(at_et(SESSION, 9, 0))
    rig.snapshots = [snap(SESSION, 9, 30)]
    beats: list[dict] = []
    service = IntradayService(rig.ledger, clock, CAL, rig.config(), heartbeat_path=rig.heartbeat)
    original = service._write_heartbeat

    def spy(*args, **kwargs):
        original(*args, **kwargs)
        beats.append(rig.heartbeat_json())

    service._write_heartbeat = spy
    service.run(SESSION, stop_at=at_et(SESSION, 9, 31))
    assert beats[0]["note"] == "starting" and beats[0]["at_utc"] == at_et(SESSION, 9, 0).isoformat()
    assert any("waiting for the open" in beat["note"] for beat in beats)
    assert min(rig.source_calls) >= CAL.session_open(SESSION)  # no pre-market tick
    assert not rig.heartbeat.with_name(rig.heartbeat.name + ".tmp").exists()  # moved into place

    done = Rig(rig.tmp_path, name="done.db")
    done.heartbeat = rig.tmp_path / "done.json"
    done.snapshots = [snap(SESSION, 15, 59)]
    done.service().run(SESSION, start_at=at_et(SESSION, 15, 58))
    final = done.heartbeat_json()
    assert final["exited"] is True and final["note"].startswith("closed")
    done.ledger.close()


def test_a_fresh_heartbeat_refuses_a_second_instance(rig) -> None:
    write_heartbeat(rig)
    with pytest.raises(IntradayServiceError, match="second one"):
        rig.service().run(SESSION)


def test_a_same_session_heartbeat_past_its_ttl_allows_the_resume(rig) -> None:
    write_heartbeat(rig, at_utc=(rig.clock.now_utc() - timedelta(seconds=61)).isoformat())
    assert rig.service().run(SESSION, stop_at=at_et(SESSION, 9, 31))["session"] == SESSION
    write_heartbeat(rig, at_utc=(rig.clock.now_utc() - timedelta(seconds=60)).isoformat())
    with pytest.raises(IntradayServiceError, match="second one"):
        rig.service().run(SESSION)


def test_an_exited_heartbeat_allows_a_restart_however_fresh(rig) -> None:
    write_heartbeat(rig, exited=True, alert=True)
    assert rig.service().run(SESSION, stop_at=at_et(SESSION, 9, 31))["session"] == SESSION


def test_a_stale_heartbeat_from_an_old_session_starts(rig) -> None:
    write_heartbeat(rig, session=date(2026, 9, 24).isoformat())
    assert rig.service().run(SESSION, stop_at=at_et(SESSION, 9, 31))["session"] == SESSION


def test_another_accounts_heartbeat_refuses(rig) -> None:
    write_heartbeat(rig, account_id="OPT_OTHER", session=date(2026, 9, 24).isoformat())
    with pytest.raises(IntradayServiceError, match="belongs to 'OPT_OTHER'"):
        rig.service().run(SESSION)


def test_a_torn_heartbeat_refuses_clearly(rig) -> None:
    rig.heartbeat.write_text('{"account_id": "OPT_0DTE_PCS_SPX", "sess', encoding="utf-8")
    with pytest.raises(IntradayServiceError, match="cannot be read"):
        rig.service().run(SESSION)


# -- the previous-session gate and the after-close pass ---------------------------------


def history(ledger: Ledger, clock) -> None:
    ledger.append(
        Event(
            account=ACCOUNT, kind=EventKind.EOD_RUN, ts_utc=clock.now_utc(), command_id="history",
            payload=EodRun(session=date(2026, 9, 23), job="options", account_id=ACCOUNT,
                           bars_processed=0, at_close=clock.now_utc()),
        )
    )


def marker(ledger: Ledger, clock, session: date, job: str = "options") -> None:
    ledger.append(
        Event(
            account=ACCOUNT, kind=EventKind.EOD_RUN, ts_utc=clock.now_utc(),
            command_id=f"eod:{job}:{ACCOUNT}:{session.isoformat()}",
            payload=EodRun(session=session, job=job, account_id=ACCOUNT, bars_processed=0, at_close=clock.now_utc()),
        )
    )


def test_a_run_without_the_previous_session_completed_refuses(rig) -> None:
    history(rig.ledger, rig.clock)
    with pytest.raises(IntradayServiceError, match=r"no 'options' EOD marker \(eod:options:OPT_0DTE_PCS_SPX:2026-09-24\)"):
        rig.service().run(SESSION)


def test_the_gate_reads_the_configured_eod_job(rig) -> None:
    history(rig.ledger, rig.clock)
    marker(rig.ledger, rig.clock, date(2026, 9, 24), job="options")
    with pytest.raises(IntradayServiceError, match="no 'nightly' EOD marker"):
        rig.service(eod_job_name="nightly").run(SESSION)
    marker(rig.ledger, rig.clock, date(2026, 9, 24), job="nightly")
    assert rig.service(eod_job_name="nightly").run(SESSION, stop_at=at_et(SESSION, 9, 31))["session"] == SESSION
    with pytest.raises(IntradayServiceError, match="eod_job_name"):
        rig.config(eod_job_name="")


def test_a_completed_session_reruns_past_the_gate_and_adds_nothing(rig) -> None:
    rig.snapshots = [snap(SESSION, 15, 59)]
    rig.service().run(SESSION, start_at=at_et(SESSION, 15, 58))
    history(rig.ledger, rig.clock)  # history, and no marker for 09-24: only the own marker lets it by
    count = len(rig.ledger.events())
    rig.service().run(SESSION)
    assert len(rig.ledger.events()) == count


def test_a_session_the_after_close_pass_settled_is_not_touched(rig) -> None:
    marker(rig.ledger, rig.clock, SESSION)
    rig.snapshots = [snap(SESSION, 9, 40)]
    count = len(rig.ledger.events())
    result = rig.service().run(SESSION)
    assert result["settled"] is True and len(rig.ledger.events()) == count and rig.source_calls == []


class _NoBars:
    def bars(self, *args, **kwargs):
        raise AssertionError("an options account replays no bars")


def eod_pass(ledger: Ledger, session: date, snapshot: ChainSnapshot):
    """The after-close pass for the 0DTE account: the options EOD job, no strategy."""
    clock = ReplayClock(CAL.session_open(session))
    closes = FixedSettlements([SettlementPrice("SPX", session, SettleTime.PM, SPOT, "official close",
                                               CAL.session_close(session) + timedelta(minutes=70))])
    dividends = FixedDividends({})
    return EodRunner(
        ledger, clock, CAL, _NoBars(),
        EodRunnerConfig(
            job_name="options", brokers={ACCOUNT: SnapshotVenue(ACCOUNT, clock)}, strategies={},
            chain_snapshots=lambda day: [snapshot], settlements=closes,
            lifecycle=LifecyclePass(ledger, clock, CAL, closes, dividends=dividends),
            dividends=dividends, option_risk_engines={},
            settle_delay=timedelta(minutes=105),
        ),
    ).run(session)


def test_two_sessions_trade_close_settle_and_the_next_one_starts(tmp_path) -> None:
    """Day 1: enter, stop out at the sweep, the after-close pass; day 2 starts and trades."""
    day1 = Rig(tmp_path)
    day1.minutes((9, 40), (9, 42))
    day1.minutes((15, 29), (15, 59))
    day1.strategy.at_or_after[at_et(SESSION, 9, 40)] = [spread("d1")]
    day1.service().run(SESSION, start_at=at_et(SESSION, 9, 40))
    assert day1.held(SHORT) == 0 and day1.events(f"intraday:intraday:{ACCOUNT}:{SESSION}", EventKind.EOD_RUN)
    eod_pass(day1.ledger, SESSION, snap(SESSION, 15, 45))
    assert day1.ledger.event_by_command(f"eod:options:{ACCOUNT}:{SESSION}") is not None
    day1.ledger.close()

    day2 = Rig(tmp_path, session=NEXT, fund=False)
    day2.minutes((9, 40), (9, 42))
    day2.strategy.at_or_after[at_et(NEXT, 9, 40)] = [spread("d2", session=NEXT)]
    day2.service().run(NEXT, start_at=at_et(NEXT, 9, 40), stop_at=at_et(NEXT, 9, 43))
    assert day2.state.orders["d2:entry"].state is OrderState.FILLED
    day2.ledger.close()


def test_without_the_after_close_pass_the_next_session_refuses(tmp_path) -> None:
    day1 = Rig(tmp_path)
    day1.snapshots = [snap(SESSION, 15, 59)]
    day1.service().run(SESSION, start_at=at_et(SESSION, 15, 58))
    day1.ledger.close()
    day2 = Rig(tmp_path, session=NEXT, fund=False)
    with pytest.raises(IntradayServiceError, match="no 'options' EOD marker"):
        day2.service().run(NEXT)
    day2.ledger.close()


# -- the marker counts the session's snapshots -------------------------------------------


def test_the_marker_counts_each_fresh_snapshot_once(rig) -> None:
    rig.snapshots = [snap(SESSION, 9, 40), snap(SESSION, 9, 50), snap(SESSION, 15, 59)]
    rig.service().run(SESSION, start_at=at_et(SESSION, 9, 39))
    [marker_event] = rig.events(f"intraday:intraday:{ACCOUNT}:{SESSION}", EventKind.EOD_RUN)
    assert marker_event.payload.bars_processed == 3
    ticks = rig.events(":tick:", EventKind.VENUE_RECONCILE)
    assert len(ticks) == 3 and all(t.payload.reconciled for t in ticks)


def test_venue_positions_that_disagree_with_the_ledger_stop_the_service(rig) -> None:
    class Forgetful(SnapshotVenue):
        def positions(self):
            return []

    rig.venue = Forgetful(ACCOUNT, rig.clock)
    rig.strategy.actions[at_et(SESSION, 9, 40)] = [spread("o-1")]
    rig.minutes((9, 40), (9, 42))
    with pytest.raises(IntradayServiceError, match="disagree with the ledger"):
        rig.service().run(SESSION, start_at=at_et(SESSION, 9, 40))
    assert rig.heartbeat_json()["alert"] is True


# -- restart mid-session ----------------------------------------------------------------


def _ledger_hash(path: Path) -> str:
    """sha256 over the stored event rows in order: the determinism gate."""
    conn = sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True)
    try:
        digest = hashlib.sha256()
        for row in conn.execute(
            "SELECT seq, ts_utc, account, kind, command_id, payload_json FROM events ORDER BY seq"
        ):
            digest.update(json.dumps(row, separators=(",", ":")).encode("utf-8"))
            digest.update(b"\n")
        return digest.hexdigest()
    finally:
        conn.close()


def test_a_restart_mid_session_reproduces_the_uninterrupted_ledger(tmp_path: Path) -> None:
    """P3 gate: (run to 09:42, new process) yields the ledger of one uninterrupted run."""
    quotes = [
        (9, 40, ("1.90", "2.10"), ("0.90", "1.05")),
        (9, 41, ("2.00", "2.20"), ("0.95", "1.10")),
        (9, 42, ("1.85", "2.05"), ("0.92", "1.04")),
        (9, 43, ("1.95", "2.15"), ("0.93", "1.06")),
        (15, 30, ("0.40", "0.50"), ("0.10", "0.15")),
        (15, 59, ("0.30", "0.40"), ("0.05", "0.10")),
    ]

    def build(name: str) -> Rig:
        rig = Rig(tmp_path, name=name)
        rig.heartbeat = tmp_path / f"{name}.json"
        rig.snapshots = [snap(SESSION, hh, mm, quotes={SHORT: s, LONG: l}) for hh, mm, s, l in quotes]
        rig.strategy.actions[at_et(SESSION, 9, 40)] = [spread("o-940", target="0.20")]
        return rig

    a = build("a.db")
    a.service().run(SESSION, start_at=at_et(SESSION, 9, 9))
    a.ledger.close()

    b = build("b.db")
    b.service().run(SESSION, start_at=at_et(SESSION, 9, 9), stop_at=at_et(SESSION, 9, 42))
    b.restart().run(SESSION)
    b.ledger.close()
    assert _ledger_hash(tmp_path / "a.db") == _ledger_hash(tmp_path / "b.db")


def test_a_resting_target_survives_a_restart_and_fills(rig) -> None:
    """The venue died with the process; the restored book still holds the target."""
    rig.strategy.actions[at_et(SESSION, 9, 40)] = [spread("o-1", target="0.30")]
    rig.minutes((9, 40), (9, 44))
    rig.service().run(SESSION, start_at=at_et(SESSION, 9, 40), stop_at=at_et(SESSION, 9, 42))
    assert rig.state.orders["o-1:entry:target"].state is OrderState.ACCEPTED
    rig.snapshots.append(snap(SESSION, 9, 45, quotes={SHORT: ("0.30", "0.40"), LONG: ("0.15", "0.20")}))
    rig.restart().run(SESSION, stop_at=at_et(SESSION, 9, 46))
    assert rig.state.orders["o-1:entry:target"].state is OrderState.FILLED
    assert rig.held(SHORT) == 0 and rig.held(LONG) == 0


def test_a_working_close_survives_a_restart_and_fills(rig) -> None:
    rig.strategy.actions[at_et(SESSION, 9, 40)] = [spread("o-1")]
    rig.snapshots = [snap(SESSION, 9, 40)]
    rig.service().run(SESSION, start_at=at_et(SESSION, 9, 40), stop_at=at_et(SESSION, 9, 42))
    [close] = rig.events("stale-quote", EventKind.ORDERS_CREATED)
    close_id = close.payload.orders[0].order_id
    assert rig.state.orders[close_id].state is OrderState.ACCEPTED
    rig.snapshots.append(snap(SESSION, 9, 45))
    rig.restart().run(SESSION, stop_at=at_et(SESSION, 9, 46))
    assert rig.state.orders[close_id].state is OrderState.FILLED
    assert rig.held(SHORT) == 0 and rig.held(LONG) == 0


def crash_once(monkeypatch, cls, method: str, when) -> None:
    """Make ``cls.method`` kill the process the first time ``when(*args)`` holds."""
    original = getattr(cls, method)
    fired = []

    def wrapper(self, *args, **kwargs):
        if not fired and when(*args):
            fired.append(True)
            raise _Crash(method)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(cls, method, wrapper)


def test_an_entry_created_but_never_sent_is_cancelled_after_a_restart(rig, monkeypatch) -> None:
    from trade_engine.oms.options import OptionOrderManager

    crash_once(monkeypatch, OptionOrderManager, "_submit", lambda order: order.order_id == "o-1:entry")
    rig.strategy.actions[at_et(SESSION, 9, 40)] = [spread("o-1")]
    rig.snapshots = [snap(SESSION, 9, 40), snap(SESSION, 9, 41)]
    with pytest.raises(_Crash):
        rig.service().run(SESSION, start_at=at_et(SESSION, 9, 40))
    assert rig.state.orders["o-1:entry"].state is OrderState.NEW
    rig.restart().run(SESSION, stop_at=at_et(SESSION, 9, 42))
    assert rig.state.orders["o-1:entry"].state is OrderState.CANCELLED
    assert rig.held(SHORT) == 0


def test_a_close_created_but_never_sent_is_sent_after_a_restart(rig, monkeypatch) -> None:
    from trade_engine.oms.options import OptionOrderManager

    crash_once(monkeypatch, OptionOrderManager, "_submit", lambda order: ":close:" in order.order_id)
    rig.strategy.actions[at_et(SESSION, 9, 40)] = [spread("o-1")]
    rig.snapshots = [snap(SESSION, 9, 40)]
    with pytest.raises(_Crash):
        rig.service().run(SESSION, start_at=at_et(SESSION, 9, 40))
    assert rig.state.orders["o-1:entry:close:1"].state is OrderState.NEW
    rig.snapshots.append(snap(SESSION, 9, 45))
    rig.restart().run(SESSION, stop_at=at_et(SESSION, 9, 46))
    assert rig.state.orders["o-1:entry:close:1"].state is OrderState.FILLED
    assert rig.held(SHORT) == 0 and rig.held(LONG) == 0


def test_a_close_whose_submit_outcome_was_lost_is_resolved_and_fills(rig, monkeypatch) -> None:
    crash_once(monkeypatch, SnapshotVenue, "submit", lambda order: ":close:" in order.venue_order_id)
    rig.strategy.actions[at_et(SESSION, 9, 40)] = [spread("o-1")]
    rig.snapshots = [snap(SESSION, 9, 40)]
    with pytest.raises(_Crash):
        rig.service().run(SESSION, start_at=at_et(SESSION, 9, 40))
    assert rig.state.orders["o-1:entry:close:1"].state is OrderState.PENDING_UNKNOWN
    rig.snapshots.append(snap(SESSION, 9, 45))
    rig.restart().run(SESSION, stop_at=at_et(SESSION, 9, 46))
    assert rig.state.orders["o-1:entry:close:1"].state is OrderState.FILLED
    assert rig.held(SHORT) == 0 and rig.held(LONG) == 0


def test_an_entry_whose_cancel_outcome_was_lost_is_resolved_cancelled(rig, monkeypatch) -> None:
    crash_once(monkeypatch, SnapshotVenue, "cancel", lambda venue_order_id: venue_order_id == "o-1:entry")
    rig.strategy.actions[at_et(SESSION, 9, 40)] = [spread("o-1", credit="5.00")]
    rig.snapshots = [snap(SESSION, 9, 40)]
    with pytest.raises(_Crash):
        rig.service().run(SESSION, start_at=at_et(SESSION, 9, 40))
    assert rig.state.orders["o-1:entry"].state is OrderState.PENDING_UNKNOWN
    rig.restart().run(SESSION, stop_at=at_et(SESSION, 9, 43))
    assert rig.state.orders["o-1:entry"].state is OrderState.CANCELLED


def test_a_pending_request_that_cannot_be_carried_out_bars_entries_for_the_session(rig) -> None:
    rig.strategy.actions[at_et(SESSION, 9, 40)] = [spread("o-1", credit="5.00")]
    rig.snapshots = [snap(SESSION, 9, 40)]
    rig.service().run(SESSION, start_at=at_et(SESSION, 9, 40), stop_at=at_et(SESSION, 9, 41))
    rig.ledger.append(
        Event(account=ACCOUNT, kind=EventKind.ORDER_PENDING, ts_utc=rig.clock.now_utc(),
              command_id="manual:replace-pending", payload=OrderStateChange("o-1:entry", "Replace pending"))
    )
    rig.minutes((9, 42), (9, 44))
    service = rig.restart()
    rig.strategy.always = [spread("o-2")]
    result = service.run(SESSION, stop_at=at_et(SESSION, 9, 44))
    assert not rig.events("o-2") and result["entries_dropped"] >= 1
    assert "entries barred" in rig.heartbeat_json()["note"]


def test_a_restart_after_the_close_does_not_crash_and_leaves_marks_to_the_eod(rig) -> None:
    rig.strategy.actions[at_et(SESSION, 9, 40)] = [spread("o-1")]
    rig.snapshots = [snap(SESSION, 9, 40)]
    rig.service().run(SESSION, start_at=at_et(SESSION, 9, 40), stop_at=at_et(SESSION, 9, 42))
    rig.clock.advance_to(at_et(SESSION, 16, 30))
    rig.restart().run(SESSION)
    assert rig.events(f"intraday:intraday:{ACCOUNT}:{SESSION}", EventKind.EOD_RUN)
    assert not rig.events("intraday:mark:")
    assert "marks left to the after-close pass" in rig.heartbeat_json()["note"]


# -- the close mark -----------------------------------------------------------------------


def test_the_close_mark_needs_a_fresh_snapshot_near_the_close(tmp_path) -> None:
    near = Rig(tmp_path, name="near.db")
    near.heartbeat = tmp_path / "near.json"
    near.strategy.actions[at_et(SESSION, 9, 40)] = [spread("o-1")]
    near.snapshots = [snap(SESSION, 9, 40)]
    near.minutes((15, 50), (15, 59))
    # Held through the close: a sweep one second before it has no tick to run on.
    # A seven-hour quote limit keeps the 09:40 chain live until the afternoon ones.
    config = dict(flat_at=time(16, 0), flat_before_close=timedelta(seconds=1),
                  entry_before_close=timedelta(seconds=1), max_quote_age_seconds=7 * 3600.0)
    near.service(**config).run(SESSION, start_at=at_et(SESSION, 9, 40))
    assert near.held(SHORT) == -1
    marks = {e.payload.instrument: e.payload.source for e in near.events("intraday:mark:", EventKind.MARK)}
    assert marks[SHORT] == f"snapshot:{snap(SESSION, 15, 59).as_of.isoformat()}"
    near.ledger.close()

    far = Rig(tmp_path, name="far.db")
    far.heartbeat = tmp_path / "far.json"
    far.strategy.actions[at_et(SESSION, 9, 40)] = [spread("o-1")]
    far.snapshots = [snap(SESSION, 9, 40)]
    far.minutes((15, 50), (15, 57))  # quotes stop at 15:57: nothing fresh at 15:59
    far.service(**config).run(SESSION, start_at=at_et(SESSION, 9, 40))
    assert not far.events("intraday:mark:")
    assert far.events(f"intraday:intraday:{ACCOUNT}:{SESSION}", EventKind.EOD_RUN)
    far.ledger.close()


# -- unexpected errors ----------------------------------------------------------------------


def test_an_unexpected_error_flattens_at_a_fresh_quote_alerts_and_exits(rig) -> None:
    rig.strategy.actions[at_et(SESSION, 9, 40)] = [spread("o-1")]
    rig.strategy.raise_at = at_et(SESSION, 9, 41)
    rig.minutes((9, 40), (9, 42))
    with pytest.raises(IntradayServiceAlert, match="strategy bug"):
        rig.service().run(SESSION, start_at=at_et(SESSION, 9, 40))
    assert rig.events("intraday:flat:o-1:entry:emergency", EventKind.ORDERS_CREATED)
    beat = rig.heartbeat_json()
    assert beat["alert"] is True and beat["exited"] is True and beat["refusing"] is True


def test_an_unexpected_error_with_no_fresh_quote_sends_nothing_and_alerts(rig) -> None:
    rig.strategy.actions[at_et(SESSION, 9, 40)] = [spread("o-1")]
    rig.strategy.raise_at = at_et(SESSION, 9, 41)
    rig.minutes((9, 40), (9, 42))
    calls = []
    fresh_source = rig.source

    def source(underlying, now):
        calls.append(now)
        if len([c for c in calls if c == at_et(SESSION, 9, 41)]) > 1:
            raise StaleDataError("hub down")
        return fresh_source(underlying, now)

    with pytest.raises(IntradayServiceError, match="no fresh quote to flatten on"):
        rig.service(snapshot_source=source).run(SESSION, start_at=at_et(SESSION, 9, 40))
    assert not rig.events("emergency")
    assert rig.heartbeat_json()["alert"] is True

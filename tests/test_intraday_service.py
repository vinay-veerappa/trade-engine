"""I1: the 0DTE intraday service - heartbeat, stale-quote flat-and-refuse, resume."""

from __future__ import annotations

import json
import sqlite3
import hashlib
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from trade_engine.calendar import get_calendar
from trade_engine.clock.replay import ReplayClock
from trade_engine.domain.instruments import Combo, ComboLeg, OptionContract, OptionRight, Side
from trade_engine.domain.option_orders import OptionIntent
from trade_engine.domain.orders import OrderState, OrderType
from trade_engine.domain.risk import RiskRuleResult, RiskVerdict
from trade_engine.intraday import IntradayConfig, IntradayService, IntradayServiceError
from trade_engine.interfaces.market_data import Greeks, OptionQuote, StaleDataError
from trade_engine.ledger import EodRun, CashFlow, Event, EventKind, Ledger
from trade_engine.market_data.chains import ChainSnapshot
from trade_engine.sim import SnapshotVenue

D = Decimal
NY = ZoneInfo("America/New_York")
CAL = get_calendar()
ACCOUNT = "OPT_0DTE_PCS_SPX"
SESSION = date(2026, 9, 25)
EXPIRY = SESSION  # 0DTE: the traded expiry is today
SPOT = D("6710")
SHORT = OptionContract("SPX", EXPIRY, D("6700"), OptionRight.PUT)
LONG = OptionContract("SPX", EXPIRY, D("6695"), OptionRight.PUT)
GREEKS = Greeks(delta=-0.10, gamma=0.0, theta=0.0, vega=0.0, rho=0.0, source="vendor")


def at_et(session: date, hh: int, mm: int) -> datetime:
    return datetime.combine(session, datetime.min.time(), tzinfo=NY).replace(hour=hh, minute=mm)


def snap(session: date, hh: int, mm: int, *, quotes: dict | None = None, spot: D = SPOT) -> ChainSnapshot:
    when = at_et(session, hh, mm)
    book = quotes or {SHORT: ("1.90", "2.10"), LONG: ("0.90", "1.05")}
    rows = tuple(
        OptionQuote(c, D(b), D(a), D(10), D(10), when, greeks=GREEKS, underlying_price=spot)
        for c, (b, a) in book.items()
    )
    return ChainSnapshot("SPX", when, spot, rows, None, None, "test")


def _spread_entry(name: str, credit: str = "0.90") -> OptionIntent:
    return OptionIntent(
        intent_id=name,
        account_id=ACCOUNT,
        instrument=Combo((ComboLeg(SHORT, 1, Side.SELL), ComboLeg(LONG, 1, Side.BUY))),
        side=Side.SELL,
        quantity=D("1"),
        reason="test",
        command_id=name,
        order_type=OrderType.LIMIT,
        limit_price=D(credit),
        profit_target=None,
    )


class Approve:
    name = "approve"

    def evaluate(self, intent, context) -> RiskVerdict:
        rule = RiskRuleResult("test", True, "x", "x", "test rule")
        return RiskVerdict(intent.intent_id, evaluations=(rule,), refusal_reasons=())


class Scripted:
    """A strategy whose snapshot-phase actions the test sets per instant.

    Like a real plugin, it acts only when the account is flat: an entry while one
    is already open would be refused by the OMS (C4) and fail the run loudly.
    Actions are keyed by the exact tick instant; ``at_or_after`` lets a test set
    one for the first tick at or after a given instant.
    """

    name = "scripted"

    def __init__(self) -> None:
        self.actions: dict[datetime, list] = {}
        self.at_or_after: dict[datetime, list] = {}
        self.contexts: list = []
        self.ledger: Ledger | None = None

    def _flat(self) -> bool:
        if self.ledger is None:
            return True
        state = self.ledger.state(ACCOUNT)
        if any(position.quantity != 0 for position in state.positions.values()):
            return False
        for order in state.orders.values():
            if order.parent_order_id is None and order.state in (
                OrderState.NEW,
                OrderState.SUBMITTED,
                OrderState.ACCEPTED,
                OrderState.PARTIALLY_FILLED,
                OrderState.PENDING_UNKNOWN,
            ):
                return False
        return True

    def manage_options(self, context):
        self.contexts.append(context)
        if not self._flat():
            return []
        if context.now in self.actions:
            return list(self.actions[context.now])
        due = [
            (when, actions)
            for when, actions in self.at_or_after.items()
            if context.now >= when
        ]
        if due:
            when = min(due, key=lambda item: item[0])
            del self.at_or_after[when[0]]
            return list(when[1])
        return []
        return list(self.actions.get(context.now, ()))


class Rig:
    def __init__(self, tmp_path: Path) -> None:
        self.clock = ReplayClock(CAL.session_open(SESSION))
        self.ledger = Ledger(tmp_path / "ledger.db").open()
        self.strategy = Scripted()
        self.strategy.ledger = self.ledger
        self.venue = SnapshotVenue(ACCOUNT, self.clock)
        self.snapshots: list[ChainSnapshot] = []
        self.heartbeat = tmp_path / "heartbeat.json"
        self.ledger.append(
            Event(
                account=ACCOUNT,
                kind=EventKind.CASH_FLOW,
                ts_utc=self.clock.now_utc(),
                command_id="deposit",
                payload=CashFlow(amount=D("50000"), kind="deposit", as_of=self.clock.now_utc()),
            )
        )

    def config(self, **changes) -> IntradayConfig:
        def source(underlying: str, now: datetime) -> ChainSnapshot:
            known = [s for s in self.snapshots if s.as_of <= now]
            if not known:
                raise StaleDataError(f"No {underlying} snapshot at or before {now.isoformat()} (I5)")
            return known[-1]

        values = dict(
            job_name="intraday",
            account_id=ACCOUNT,
            underlying="SPX",
            broker=self.venue,
            strategy=self.strategy,
            option_risk_engine=Approve(),
            snapshot_source=source,
            max_quote_age_seconds=30.0,
            tick_seconds=60.0,
        )
        values.update(changes)
        return IntradayConfig(**values)

    def service(self, **changes) -> IntradayService:
        return IntradayService(
            self.ledger, self.clock, CAL, self.config(**changes), heartbeat_path=self.heartbeat
        )

    @property
    def state(self):
        return self.ledger.state(ACCOUNT)

    def held(self, instrument) -> Decimal:
        position = self.state.positions.get(instrument)
        return D(0) if position is None else position.quantity


@pytest.fixture
def rig(tmp_path):
    r = Rig(tmp_path)
    yield r
    r.ledger.close()


# -- the stale-quote branch -------------------------------------------------------


def test_a_stale_quote_closes_open_structures(rig) -> None:
    """Stale quote => the flatten close is submitted for what is open (I5)."""
    fresh = snap(SESSION, 9, 40)
    entry = _spread_entry("o-1")
    rig.strategy.actions[at_et(SESSION, 9, 40)] = [entry]
    # The clock resumes at 09:39; the newest answer stays at 09:40's instant, so ticks
    # from 09:42 are one second past the 30s the service allows.
    rig.snapshots = [fresh]
    result = rig.service(max_quote_age_seconds=30.0, tick_seconds=60.0).run(
        SESSION, start_at=at_et(SESSION, 9, 39)
    )
    assert rig.held(SHORT) == -1 and rig.held(LONG) == 1
    closes = [e for e in rig.ledger.events(account=ACCOUNT) if "stale quote" in (e.command_id or "")]
    assert closes, "the stale branch must submit a flatten close"


def test_a_stale_quote_refuses_new_entries(rig) -> None:
    """A stale quote prices nothing: the strategy sees no context to enter on (I5)."""
    fresh = snap(SESSION, 9, 40)
    # Entry at the fresh tick fills; the stale ticks that follow see no new entry.
    later = snap(SESSION, 9, 50, quotes={SHORT: ("1.80", "2.00"), LONG: ("0.88", "1.02")})
    rig.strategy.actions[at_et(SESSION, 9, 40)] = [_spread_entry("o-1")]
    rig.strategy.actions[at_et(SESSION, 9, 50)] = [_spread_entry("o-2")]
    rig.snapshots = [fresh, later]
    rig.service(max_quote_age_seconds=30.0, tick_seconds=60.0).run(
        SESSION, start_at=at_et(SESSION, 9, 39)
    )
    entries = [
        e
        for e in rig.ledger.events(account=ACCOUNT)
        if e.kind is EventKind.ORDERS_CREATED and ":entry" in (e.command_id or "")
    ]
    assert len(entries) == 1  # only the fresh-quote entry; the stale tick added none


def test_the_1530_sweep_flattens_what_is_still_open(rig) -> None:
    """Flat 15:30: the sweep closes what the strategy left open (rules doc §6.4)."""
    # Snapshots every minute through 15:31 so quotes never go stale; the entry
    # fills at 09:40 and its profit target never reaches, so it holds to 15:30.
    quotes = {SHORT: ("1.90", "2.10"), LONG: ("0.90", "1.05")}
    snapshots = [snap(SESSION, 9, 40, quotes=quotes)]
    for mm in range(41, 60):
        snapshots.append(snap(SESSION, 9, mm, quotes=quotes))
    for hh in range(10, 16):
        for mm in range(0, 60, 7):
            snapshots.append(snap(SESSION, hh, mm, quotes=quotes))
    rig.snapshots = snapshots
    rig.strategy.at_or_after[at_et(SESSION, 9, 40)] = [_spread_entry("o-1")]
    result = rig.service(max_quote_age_seconds=900.0, tick_seconds=300.0).run(
        SESSION, start_at=at_et(SESSION, 9, 39)
    )
    # The sweep asked to be flat at 15:30 and it filled at the next snapshot.
    assert rig.held(SHORT) == 0 and rig.held(LONG) == 0
    flats = [
        e
        for e in rig.ledger.events(account=ACCOUNT)
        if "flat 15:30" in (e.command_id or "")
    ]
    assert flats


def test_the_heartbeat_latches_refusing(rig) -> None:
    """A refusing tick is visible in the heartbeat file the supervisor watches."""
    rig.snapshots = [snap(SESSION, 9, 40)]
    rig.strategy.actions[at_et(SESSION, 9, 40)] = [_spread_entry("o-1")]
    rig.service(max_quote_age_seconds=30.0, tick_seconds=60.0).run(
        SESSION, start_at=at_et(SESSION, 9, 39)
    )
    raw = json.loads(rig.heartbeat.read_text(encoding="utf-8"))
    assert raw["account_id"] == ACCOUNT and raw["session"] == SESSION.isoformat()
    assert raw["refusing"] is True and "stale" in raw["note"]


def test_fresh_quotes_after_a_stale_window_clear_the_refusal(rig) -> None:
    """The refusing latch clears when a fresh quote returns; the flatten fills there."""
    first = snap(SESSION, 9, 40)
    rig.strategy.actions[at_et(SESSION, 9, 40)] = [_spread_entry("o-1")]
    later = snap(SESSION, 9, 50, quotes={SHORT: ("1.80", "2.00"), LONG: ("0.88", "1.02")})
    rig.snapshots = [first, later]
    result = rig.service(max_quote_age_seconds=30.0, tick_seconds=60.0).run(
        SESSION, start_at=at_et(SESSION, 9, 39)
    )
    # The flatten close, asked during the stale window, filled at the fresh snapshot.
    assert rig.held(SHORT) == 0 and rig.held(LONG) == 0
    # Both fresh instants were processed: the latch cleared at each (09:40, 09:50).
    assert result["snapshots_processed"] == 2


# -- resume: restart mid-session replays to the same state -------------------------


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
    ticks = [
        (9, 40, {SHORT: ("1.90", "2.10"), LONG: ("0.90", "1.05")}),
        (9, 41, {SHORT: ("2.00", "2.20"), LONG: ("0.95", "1.10")}),
        (9, 42, {SHORT: ("1.85", "2.05"), LONG: ("0.92", "1.04")}),
        (9, 43, {SHORT: ("1.95", "2.15"), LONG: ("0.93", "1.06")}),
    ]
    snapshots = [snap(SESSION, hh, mm, quotes=q) for hh, mm, q in ticks]

    def source(now: datetime) -> ChainSnapshot:
        known = [s for s in snapshots if s.as_of <= now]
        if not known:
            raise StaleDataError(f"No SPX snapshot at or before {now.isoformat()} (I5)")
        return known[-1]

    def entries_for(strategy) -> None:
        for hh, mm, _ in ticks:
            strategy.actions[at_et(SESSION, hh, mm)] = [_spread_entry(f"o-{hh}{mm}")]

    def make(name: str) -> tuple[Ledger, ReplayClock, Scripted, IntradayConfig]:
        clock = ReplayClock(CAL.session_open(SESSION))
        ledger = Ledger(tmp_path / name).open()
        ledger.append(
            Event(
                account=ACCOUNT, kind=EventKind.CASH_FLOW, ts_utc=clock.now_utc(), command_id="deposit",
                payload=CashFlow(amount=D("50000"), kind="deposit", as_of=clock.now_utc()),
            )
        )
        strategy = Scripted()
        strategy.ledger = ledger
        entries_for(strategy)
        config = IntradayConfig(
            job_name="intraday", account_id=ACCOUNT, underlying="SPX",
            broker=SnapshotVenue(ACCOUNT, clock), strategy=strategy,
            option_risk_engine=Approve(), snapshot_source=lambda u, n: source(n),
            max_quote_age_seconds=30.0, tick_seconds=60.0,
        )
        return ledger, clock, strategy, config

    # Run A: one process walks the whole morning.
    ledger_a, clock_a, _, config_a = make("a.db")
    IntradayService(ledger_a, clock_a, CAL, config_a).run(SESSION, start_at=at_et(SESSION, 9, 9))
    hash_a = _ledger_hash(tmp_path / "a.db")
    ledger_a.close()

    # Run B: process 1 walks from the open and "crashes" at 09:42 (no marker, no
    # close-mark); process 2 folds, restores the venue, and continues from where the
    # clock sits — a restarted service never rewinds time (I7). The strategy is
    # rebuilt (as a fresh process would) and shares the clock at 09:42.
    ledger_b, clock_b, _, config_b = make("b.db")
    IntradayService(ledger_b, clock_b, CAL, config_b).run(
        SESSION, start_at=at_et(SESSION, 9, 9), stop_at=at_et(SESSION, 9, 42)
    )
    ledger_b.close()
    ledger_b2 = Ledger(tmp_path / "b.db").open()
    strategy_b2 = Scripted()
    strategy_b2.ledger = ledger_b2
    entries_for(strategy_b2)
    config_b2 = IntradayConfig(
        job_name="intraday", account_id=ACCOUNT, underlying="SPX",
        broker=SnapshotVenue(ACCOUNT, clock_b), strategy=strategy_b2,
        option_risk_engine=Approve(), snapshot_source=lambda u, n: source(n),
        max_quote_age_seconds=30.0, tick_seconds=60.0,
    )
    IntradayService(ledger_b2, clock_b, CAL, config_b2).run(SESSION)
    hash_b = _ledger_hash(tmp_path / "b.db")
    ledger_b2.close()
    assert hash_a == hash_b


def test_a_fresh_heartbeat_refuses_a_second_instance(rig) -> None:
    """A fresh heartbeat from this session refuses to start a second writer (C4)."""
    rig.snapshots = [snap(SESSION, 9, 40)]
    rig.heartbeat.write_text(
        json.dumps(
            {
                "account_id": ACCOUNT,
                "session": SESSION.isoformat(),
                "at_utc": rig.clock.now_utc().isoformat(),
                "refusing": False,
                "note": "ok",
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(IntradayServiceError, match="second one"):
        rig.service().run(SESSION, start_at=at_et(SESSION, 9, 9))


def test_a_stale_heartbeat_from_an_old_session_starts(rig) -> None:
    """A heartbeat from yesterday is not a live instance: the service resumes."""
    rig.snapshots = [snap(SESSION, 9, 40)]
    rig.heartbeat.write_text(
        json.dumps(
            {
                "account_id": ACCOUNT,
                "session": date(2026, 9, 24).isoformat(),
                "at_utc": rig.clock.now_utc().isoformat(),
                "refusing": False,
                "note": "ok",
            }
        ),
        encoding="utf-8",
    )
    result = rig.service().run(SESSION, start_at=at_et(SESSION, 9, 9))
    assert result["session"] == SESSION


# -- the previous-session gate ------------------------------------------------------


def test_a_run_without_the_previous_session_completed_refuses(tmp_path: Path) -> None:
    clock = ReplayClock(CAL.session_open(SESSION))
    ledger = Ledger(tmp_path / "ledger.db").open()
    ledger.append(
        Event(
            account=ACCOUNT, kind=EventKind.CASH_FLOW, ts_utc=clock.now_utc(), command_id="deposit",
            payload=CashFlow(amount=D("50000"), kind="deposit", as_of=clock.now_utc()),
        )
    )
    # History but no previous-session marker: the gate demands it (I3).
    ledger.append(
        Event(
            account=ACCOUNT, kind=EventKind.EOD_RUN, ts_utc=clock.now_utc(), command_id="history",
            payload=EodRun(
                session=date(2026, 9, 23), job="intraday", account_id=ACCOUNT,
                bars_processed=0, at_close=clock.now_utc(),
            ),
        )
    )
    config = IntradayConfig(
        job_name="intraday", account_id=ACCOUNT, underlying="SPX",
        broker=SnapshotVenue(ACCOUNT, clock), strategy=Scripted(), option_risk_engine=Approve(),
        snapshot_source=lambda u, n: snap(SESSION, 9, 40), tick_seconds=60.0,
    )
    with pytest.raises(IntradayServiceError, match="no options EOD marker"):
        IntradayService(ledger, clock, CAL, config).run(SESSION, start_at=at_et(SESSION, 9, 9))
    ledger.close()
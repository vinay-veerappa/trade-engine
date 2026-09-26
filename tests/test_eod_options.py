"""O4: options accounts in the EOD run — snapshot fills, settlement, dividends, marks."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from trade_engine.calendar import get_calendar
from trade_engine.clock.replay import ReplayClock
from trade_engine.domain.instruments import Equity, OptionContract, OptionRight, Side
from trade_engine.domain.option_orders import CloseHolding, CloseStructure, OptionIntent
from trade_engine.domain.option_roots import SettleTime
from trade_engine.domain.orders import OrderState, OrderType
from trade_engine.domain.risk import RiskRuleResult, RiskVerdict
from trade_engine.eod import EodRunner, EodRunnerConfig
from trade_engine.eod.runner import EodRunnerError, ReplayDataError
from trade_engine.interfaces.market_data import OptionQuote
from trade_engine.ledger import CashFlow, Event, EventKind, Ledger
from trade_engine.lifecycle import Dividend, FixedDividends, FixedSettlements, LifecyclePass, SettlementPrice
from trade_engine.market_data.chains import ChainSnapshot
from trade_engine.oms.options import open_structures
from trade_engine.sim import SnapshotVenue

D = Decimal
CAL = get_calendar()
ACCOUNT = "OPT_CSP"
S1, S2, S3, S4, S5, S6 = (date(2026, 9, 25), date(2026, 9, 28), date(2026, 9, 29), date(2026, 9, 30), date(2026, 10, 1), date(2026, 10, 2))
SESSIONS = [S1, S2, S3, S4, S5, S6]
EXPIRY = S6
P270 = OptionContract("COHR", EXPIRY, D("270"), OptionRight.PUT)
P260 = OptionContract("COHR", EXPIRY, D("260"), OptionRight.PUT)
C330 = OptionContract("COHR", EXPIRY, D("330"), OptionRight.CALL)
COHR = Equity("COHR")
BASE = {P270: ("10.10", "10.60"), P260: ("6.70", "7.80"), C330: ("4.00", "4.20")}


def snap_time(session: date) -> datetime:
    return CAL.session_close(session) - timedelta(minutes=15)  # 15:45 ET


def chain(session: date, quotes: dict | None = None, underlying: str = "COHR", price: str = "300") -> ChainSnapshot:
    at = snap_time(session)
    book = dict(BASE)
    book.update(quotes or {})
    rows = tuple(OptionQuote(c, D(b), D(a), D(10), D(10), at) for c, (b, a) in book.items() if c.underlying == underlying)
    return ChainSnapshot(underlying, at, D(price), rows, None, None, "test")


def close_price(session: date, price: str = "300", symbol: str = "COHR", at: datetime | None = None) -> SettlementPrice:
    return SettlementPrice(symbol, session, SettleTime.PM, D(price), "official close", at or CAL.session_close(session) + timedelta(minutes=70))


class Approve:
    name = "approve"

    def __init__(self, accept: bool = True) -> None:
        self.accept = accept
        self.seen: list = []

    def evaluate(self, intent, context) -> RiskVerdict:
        self.seen.append((intent, context))
        rule = RiskRuleResult("test", self.accept, "x", "x", "test rule")
        return RiskVerdict(intent.intent_id, evaluations=(rule,), refusal_reasons=() if self.accept else ("refused by test",))


class NoSignals:
    name = "none"

    def read_signals(self, session):
        return []


class Scripted:
    """A strategy whose actions are set per (session, phase) by the test."""

    name = "scripted"

    def __init__(self) -> None:
        self.entries: dict[date, list] = {}
        self.at_snapshot: dict[date, list] = {}
        self.at_close: dict[date, list] = {}
        self.contexts: list = []

    def manage_options(self, context):
        self.contexts.append(context)
        table = self.at_snapshot if context.phase == "snapshot" else self.at_close
        return list(table.get(context.session, ()))

    def generate_intents(self, signals, context):
        return list(self.entries.get(context.session, ()))


def csp(name: str = "csp", quantity: str = "1", limit: str = "10.00", target: str | None = "5.00", contract=P270, side=Side.SELL) -> OptionIntent:
    return OptionIntent(
        intent_id=name, account_id=ACCOUNT, instrument=contract, side=side, quantity=D(quantity), reason="test",
        command_id=name, order_type=OrderType.LIMIT, limit_price=D(limit), profit_target=None if target is None else D(target),
    )


class NoBars:
    def bars(self, *args, **kwargs):
        raise AssertionError("an options account replays no bars")


class Rig:
    def __init__(self, tmp_path, *, risk=None, lifecycle=True, dividends="none", quotes=None, closes=None) -> None:
        self.clock = ReplayClock(CAL.session_open(S1))
        self.ledger = Ledger(tmp_path / "ledger.db").open()
        self.ledger.append(Event(account=ACCOUNT, kind=EventKind.CASH_FLOW, ts_utc=self.clock.now_utc(), command_id="deposit",
                                 payload=CashFlow(amount=D("50000"), kind="deposit", as_of=self.clock.now_utc())))
        self.venue = SnapshotVenue(ACCOUNT, self.clock)
        self.strategy = Scripted()
        self.risk = risk or Approve()
        self.quotes: dict[date, dict] = quotes or {}
        self.snapshots: dict[date, list] = {}
        closes = closes if closes is not None else [close_price(s) for s in SESSIONS]
        self.settlements = FixedSettlements(closes)
        if dividends == "none":
            self.dividends = FixedDividends({"COHR": []})
        else:
            self.dividends = dividends
        self.lifecycle = LifecyclePass(self.ledger, self.clock, CAL, self.settlements, dividends=self.dividends) if lifecycle else None

    def config(self, **changes) -> EodRunnerConfig:
        values = dict(
            job_name="eod",
            brokers={ACCOUNT: self.venue},
            signal_adapters={ACCOUNT: NoSignals()},
            strategies={ACCOUNT: self.strategy},
            chain_snapshots=lambda session: self.snapshots.get(session, [chain(session, self.quotes.get(session))]),
            settlements=self.settlements,
            lifecycle=self.lifecycle,
            dividends=self.dividends,
            option_risk_engines={ACCOUNT: self.risk},
        )
        values.update(changes)
        return EodRunnerConfig(**values)

    def run(self, session: date, **changes):
        if self.clock.now_utc() < CAL.session_open(session):
            self.clock.advance_to(CAL.session_open(session))
        return EodRunner(self.ledger, self.clock, CAL, NoBars(), self.config(**changes)).run(session)

    @property
    def state(self):
        return self.ledger.state(ACCOUNT)

    def held(self, instrument) -> Decimal:
        position = self.state.positions.get(instrument)
        return D(0) if position is None else position.quantity

    def marks(self, session: date) -> dict:
        return {
            e.payload.instrument: (e.payload.price, e.payload.source)
            for e in self.ledger.events(account=ACCOUNT)
            if e.kind is EventKind.MARK and e.command_id.startswith(f"eod:mark:{ACCOUNT}:{session.isoformat()}:")
        }


@pytest.fixture
def rig(tmp_path):
    r = Rig(tmp_path)
    yield r
    r.ledger.close()


# -- an entry decided at the close works at the next session's snapshot --------------


def test_an_entry_decided_at_the_close_fills_at_the_next_snapshot(rig) -> None:
    rig.strategy.entries[S1] = [csp()]
    first = rig.run(S1)
    assert first.accounts[0].orders_submitted == 1 and rig.held(P270) == 0
    second = rig.run(S2)
    assert second.accounts[0].snapshots_processed == 1
    assert rig.held(P270) == -1
    fill = next(f for f in rig.state.fills if f.instrument == P270)
    assert fill.price == D("10.00") and fill.filled_at == snap_time(S2)
    assert rig.state.orders["csp:entry:target"].state is OrderState.ACCEPTED


def test_options_are_marked_at_the_snapshot_mid_and_the_underlying_at_the_official_close(rig) -> None:
    rig.strategy.entries[S1] = [csp()]
    rig.run(S1)
    rig.run(S2)
    marks = rig.marks(S2)
    assert marks[P270] == (D("10.35"), f"snapshot:{snap_time(S2).isoformat()}")
    assert marks[COHR] == (D("300"), "official close")  # not held, but its put's margin needs it


def test_the_options_run_happens_after_the_official_close_is_known(rig) -> None:
    rig.run(S1)
    [marker] = [e for e in rig.ledger.events(account=ACCOUNT) if e.kind is EventKind.EOD_RUN]
    assert marker.ts_utc == CAL.session_close(S1) + timedelta(minutes=105)  # 17:45 ET


def test_rerunning_a_session_appends_nothing(rig) -> None:
    rig.strategy.entries[S1] = [csp()]
    rig.run(S1)
    rig.run(S2)
    count = len(rig.ledger.events())
    rig.run(S2)
    assert len(rig.ledger.events()) == count


def test_a_new_process_restores_the_venue_from_the_ledger(rig, tmp_path) -> None:
    rig.strategy.entries[S1] = [csp()]
    rig.run(S1)
    rig.venue = SnapshotVenue(ACCOUNT, rig.clock)  # the next run is a new process
    rig.run(S2)
    assert rig.held(P270) == -1


def test_a_refused_entry_is_recorded_and_never_ordered(tmp_path) -> None:
    rig = Rig(tmp_path, risk=Approve(accept=False))
    rig.strategy.entries[S1] = [csp()]
    result = rig.run(S1)
    assert result.accounts[0].orders_submitted == 0
    verdicts = [e for e in rig.ledger.events(account=ACCOUNT) if e.kind is EventKind.RISK_VERDICT]
    assert len(verdicts) == 1 and not verdicts[0].payload.accepted
    assert "csp:entry" not in rig.state.orders


def test_the_risk_engine_sees_the_account_as_folded(rig) -> None:
    rig.strategy.entries[S1] = [csp()]
    rig.run(S1)
    [(intent, context)] = rig.risk.seen
    assert intent.intent_id == "csp" and context.phase == "close" and context.state.cash == D("50000")


# -- the snapshot phase ----------------------------------------------------------------


def test_a_close_decided_at_the_snapshot_trades_on_that_snapshot(rig) -> None:
    rig.strategy.entries[S1] = [csp(target=None)]
    rig.run(S1)
    rig.run(S2)
    rig.strategy.at_snapshot[S3] = [CloseStructure("csp:entry", "3x credit", "csp:defensive")]
    rig.run(S3, chain_snapshots=lambda s: [chain(s, {P270: ("30.00", "31.00")})])
    assert rig.held(P270) == 0
    close = next(f for f in rig.state.fills if f.order_id == "csp:entry:close:1")
    assert close.price == D("30.75") and close.filled_at == snap_time(S3)


def test_the_strategy_sees_the_snapshot_and_its_open_structures(rig) -> None:
    rig.strategy.entries[S1] = [csp()]
    rig.run(S1)
    rig.run(S2)
    rig.run(S3)
    snapshot_contexts = [c for c in rig.strategy.contexts if c.phase == "snapshot" and c.session == S3]
    [context] = snapshot_contexts
    assert context.snapshot.underlying == "COHR" and context.now == snap_time(S3)
    [structure] = context.structures
    assert structure.entry_order_id == "csp:entry" and structure.target_order_id == "csp:entry:target"


def test_an_action_at_a_snapshot_for_another_underlying_refuses(rig) -> None:
    other = OptionContract("NVDA", EXPIRY, D("150"), OptionRight.PUT)
    rig.strategy.at_snapshot[S1] = [csp(contract=other)]
    with pytest.raises(EodRunnerError, match="for another underlying"):
        rig.run(S1)


def test_an_entry_at_the_snapshot_fills_at_once(rig) -> None:
    rig.strategy.at_snapshot[S1] = [csp()]
    rig.run(S1)
    assert rig.held(P270) == -1


# -- close phase exits work the next snapshot -------------------------------------------


def test_a_close_decided_after_the_close_works_at_the_next_snapshot(rig) -> None:
    rig.strategy.entries[S1] = [csp(target=None)]
    rig.run(S1)
    rig.strategy.at_close[S2] = [CloseStructure("csp:entry", "closed below the strike", "csp:rule")]
    rig.run(S2)
    assert rig.held(P270) == -1  # decided after the close
    rig.run(S3)
    assert rig.held(P270) == 0


# -- expiry and assignment (O2) -------------------------------------------------------------


def run_to_expiry(rig: Rig, settle: str) -> None:
    rig.strategy.entries[S1] = [csp()]
    closes = [close_price(s) for s in SESSIONS[:-1]] + [close_price(EXPIRY, settle)]
    rig.settlements = FixedSettlements(closes)
    rig.lifecycle = LifecyclePass(rig.ledger, rig.clock, CAL, rig.settlements, dividends=rig.dividends)
    for session in SESSIONS:
        rig.run(session)


def test_a_put_expiring_out_of_the_money_leaves_nothing_and_cancels_its_target(rig) -> None:
    run_to_expiry(rig, "280")
    assert rig.held(P270) == 0 and rig.held(COHR) == 0
    assert rig.state.orders["csp:entry:target"].state is OrderState.CANCELLED
    assert open_structures(rig.state) == ()
    assert rig.state.realized_pnl == D("1000") - D("0.65")


def test_a_put_assigned_in_the_money_leaves_shares_marked_at_the_close(rig) -> None:
    run_to_expiry(rig, "250")
    assert rig.held(P270) == 0 and rig.held(COHR) == 100
    assert rig.marks(EXPIRY)[COHR] == (D("250"), "official close")
    assert rig.state.orders["csp:entry:target"].state is OrderState.CANCELLED


def test_holding_options_with_no_lifecycle_pass_refuses(rig) -> None:
    rig.strategy.entries[S1] = [csp()]
    rig.run(S1)
    rig.lifecycle = None
    with pytest.raises(EodRunnerError, match="no lifecycle pass"):
        rig.run(S2)


# -- dividends ----------------------------------------------------------------------------


def shares_rig(tmp_path, dividends) -> Rig:
    rig = Rig(tmp_path, dividends=dividends)
    rig.strategy.at_snapshot[S1] = [OptionIntent(
        intent_id="buy", account_id=ACCOUNT, instrument=COHR, side=Side.BUY, quantity=D(100), reason="buy-write",
        command_id="buy", order_type=OrderType.MARKET,
    )]
    return rig


def dividend(ex: date, amount: str = "0.26", at: datetime | None = None) -> Dividend:
    return Dividend("COHR", ex, D(amount), "tradingview", at or datetime(2026, 9, 20, tzinfo=UTC))


def dividends_booked(rig: Rig) -> list:
    return [e.payload for e in rig.ledger.events(account=ACCOUNT) if e.kind is EventKind.CASH_FLOW and e.payload.kind == "dividend"]


def test_shares_held_at_the_open_of_the_ex_date_are_paid_the_dividend(tmp_path) -> None:
    rig = shares_rig(tmp_path, FixedDividends({"COHR": [dividend(S2)]}))
    rig.run(S1)
    rig.run(S2)
    [paid] = dividends_booked(rig)
    assert paid.amount == D("26.00") and "held at the open" in paid.note


def test_shares_bought_on_the_ex_date_are_not_paid(tmp_path) -> None:
    rig = shares_rig(tmp_path, FixedDividends({"COHR": [dividend(S1)]}))
    rig.run(S1)
    assert dividends_booked(rig) == []


def test_a_session_with_no_dividend_books_none(tmp_path) -> None:
    rig = shares_rig(tmp_path, FixedDividends({"COHR": []}))
    rig.run(S1)
    rig.run(S2)
    assert dividends_booked(rig) == []


def test_holding_shares_with_no_dividend_source_refuses(tmp_path) -> None:
    rig = shares_rig(tmp_path, FixedDividends({"COHR": []}))
    rig.run(S1)
    rig.dividends = None
    rig.lifecycle = None
    with pytest.raises(EodRunnerError, match="no dividend source"):
        rig.run(S2)


def test_a_dividend_record_from_after_the_clock_refuses(tmp_path) -> None:
    late = dividend(S2, at=CAL.session_close(S2) + timedelta(hours=3))
    rig = shares_rig(tmp_path, FixedDividends({"COHR": [late]}))
    rig.run(S1)
    with pytest.raises(ReplayDataError, match="look-ahead"):
        rig.run(S2)


def test_shares_are_sold_through_close_holding_at_the_snapshot(tmp_path) -> None:
    rig = shares_rig(tmp_path, FixedDividends({"COHR": []}))
    rig.run(S1)
    rig.strategy.at_snapshot[S2] = [CloseHolding(COHR, D(100), "done", "sell")]
    rig.run(S2)
    assert rig.held(COHR) == 0


# -- what refuses (I5) ------------------------------------------------------------------------


def test_options_accounts_without_snapshots_or_closes_refuse(rig) -> None:
    with pytest.raises(EodRunnerError, match="need chain_snapshots"):
        rig.run(S1, chain_snapshots=None)
    with pytest.raises(EodRunnerError, match="need chain_snapshots"):
        rig.run(S1, settlements=None)


def test_a_held_option_with_no_snapshot_that_session_refuses(rig) -> None:
    rig.strategy.entries[S1] = [csp()]
    rig.run(S1)
    rig.run(S2)
    with pytest.raises(ReplayDataError, match="No 2026-09-29 chain snapshot for COHR"):
        rig.run(S3, chain_snapshots=lambda s: [])


def test_a_snapshot_outside_the_session_refuses(rig) -> None:
    stale = chain(S1)
    rig.run(S1)
    with pytest.raises(ReplayDataError, match="outside the 2026-09-28 session"):
        rig.run(S2, chain_snapshots=lambda s: [stale])


def test_a_held_option_the_snapshot_does_not_quote_refuses_its_mark(rig) -> None:
    rig.strategy.entries[S1] = [csp(target=None)]
    rig.run(S1)
    rig.run(S2)
    bare = ChainSnapshot("COHR", snap_time(S3), D("300"), (), None, None, "test")
    with pytest.raises(ReplayDataError, match="refusing to mark it"):
        rig.run(S3, chain_snapshots=lambda s: [bare])


def test_an_official_close_stamped_before_the_close_refuses(tmp_path) -> None:
    early = [close_price(s, at=CAL.session_close(s) - timedelta(minutes=5)) for s in SESSIONS]
    rig = Rig(tmp_path, closes=early)
    rig.strategy.entries[S1] = [csp()]
    rig.run(S1)
    with pytest.raises(ReplayDataError, match="cannot be the official close"):
        rig.run(S2)


def test_an_official_close_stamped_after_the_clock_refuses(tmp_path) -> None:
    late = [close_price(s, at=CAL.session_close(s) + timedelta(hours=3)) for s in SESSIONS]
    rig = Rig(tmp_path, closes=late)
    rig.strategy.entries[S1] = [csp()]
    rig.run(S1)
    with pytest.raises(ReplayDataError, match="look-ahead"):
        rig.run(S2)


def test_an_entry_with_no_options_risk_engine_refuses(rig) -> None:
    rig.strategy.entries[S1] = [csp()]
    with pytest.raises(EodRunnerError, match="nothing enters unchecked"):
        rig.run(S1, option_risk_engines={})


def test_an_equity_intent_from_an_options_strategy_refuses(rig) -> None:
    rig.strategy.entries[S1] = ["not an option intent"]
    with pytest.raises(EodRunnerError, match="enters with OptionIntent"):
        rig.run(S1)


def test_an_option_fill_reaches_the_journal_as_an_option_with_its_multiplier(rig) -> None:
    rig.strategy.entries[S1] = [csp()]
    rig.run(S1, journal_accounts={ACCOUNT: "journal-1"})
    rig.run(S2, journal_accounts={ACCOUNT: "journal-1"})
    [item] = rig.ledger.pending_outbox("journal:journal-1")
    assert item.payload["asset_class"] == "option" and item.payload["multiplier"] == 100
    assert item.payload["symbol"] == P270.occ and item.payload["profit_target"] == "5.00"


def test_the_daily_snapshot_margins_the_put_against_the_marked_underlying(rig) -> None:
    from trade_engine.metrics.snapshot import daily_snapshots

    rig.strategy.entries[S1] = [csp()]
    rig.run(S1)
    rig.run(S2)
    snapshot = daily_snapshots(list(rig.ledger.events()), ACCOUNT)[-1]
    # Naked put 270 at S=300 marked 10.35: max(20% x 300 - 30 OTM, 10% x 270) + 10.35, x100.
    assert snapshot.margin_used == D("4035")



# -- acting in rounds at one snapshot ---------------------------------------------------


class BuyWrite:
    """Buy the shares, then write the call on them: two rounds at one snapshot."""

    name = "buy-write"

    def manage_options(self, context):
        if context.phase != "snapshot":
            return []
        shares = context.state.positions.get(COHR)
        if shares is None or shares.quantity == 0:
            return [OptionIntent(intent_id="bw-shares", account_id=ACCOUNT, instrument=COHR, side=Side.BUY,
                                 quantity=D(100), reason="buy-write shares", command_id="bw-shares",
                                 order_type=OrderType.MARKET)]
        if not context.structures:
            return [OptionIntent(intent_id="bw-call", account_id=ACCOUNT, instrument=C330, side=Side.SELL,
                                 quantity=D(1), reason="buy-write call", command_id="bw-call",
                                 order_type=OrderType.MARKET)]
        return []

    def generate_intents(self, signals, context):
        return []


def test_a_buy_write_buys_the_shares_then_writes_the_call_on_the_same_snapshot(rig) -> None:
    rig.strategy = BuyWrite()
    rig.run(S1)
    assert rig.held(COHR) == 100 and rig.held(C330) == -1
    assert {f.filled_at for f in rig.state.fills} == {snap_time(S1)}


def test_a_refused_entry_is_not_asked_for_again_and_again(tmp_path) -> None:
    rig = Rig(tmp_path, risk=Approve(accept=False))
    rig.strategy.at_snapshot[S1] = [csp()]
    rig.run(S1)
    assert len(rig.risk.seen) == 1


class Runaway:
    name = "runaway"

    def __init__(self) -> None:
        self.n = 0

    def manage_options(self, context):
        if context.phase != "snapshot":
            return []
        self.n += 1
        return [csp(name=f"runaway-{self.n}", contract=OptionContract("COHR", EXPIRY, D(200 + self.n), OptionRight.PUT))]

    def generate_intents(self, signals, context):
        return []


def test_a_strategy_that_never_stops_acting_refuses(rig) -> None:
    rig.strategy = Runaway()
    rig.quotes[S1] = {OptionContract("COHR", EXPIRY, D(200 + n), OptionRight.PUT): ("1.00", "1.10") for n in range(1, 6)}
    with pytest.raises(EodRunnerError, match="still acting"):
        rig.run(S1)


# -- the close phase: a combo entry's risk check sees the session's snapshots (O3) ----


def test_a_combo_entered_at_the_close_is_judged_with_the_sessions_snapshots(rig) -> None:
    from trade_engine.domain.instruments import Combo, ComboLeg

    spread = OptionIntent(
        intent_id="spread", account_id=ACCOUNT,
        instrument=Combo((ComboLeg(P270, 1, Side.SELL), ComboLeg(P260, 1, Side.BUY))),
        side=Side.SELL, quantity=D("1"), reason="test", command_id="spread",
        order_type=OrderType.LIMIT, limit_price=D("3.00"), profit_target=None,
    )
    rig.strategy.entries[S1] = [spread]
    rig.run(S1)
    [(intent, context)] = rig.risk.seen
    assert intent.intent_id == "spread" and context.phase == "close"
    assert context.snapshots["COHR"].as_of == snap_time(S1)  # the quotes its margin is measured on


# -- refusals stay the runner's own type ---------------------------------------------


def test_a_venue_fill_for_an_unknown_order_refuses_as_the_runners_error(rig) -> None:
    from trade_engine.interfaces.broker import VenueFill

    class Ghostly(SnapshotVenue):
        def fills(self, since):
            ghost = VenueFill(
                venue_fill_id="ghost:fill:1", venue_order_id="ghost", instrument=P270, quantity=D("1"),
                price=D("1"), filled_at=snap_time(S1), side=Side.SELL, fee=D("0"),
            )
            return [*super().fills(since), ghost]

    rig.venue = Ghostly(ACCOUNT, rig.clock)
    with pytest.raises(EodRunnerError, match="references unknown order 'ghost'"):
        rig.run(S1)


def test_another_jobs_markers_are_not_this_jobs_history(rig) -> None:
    """The 0DTE account carries the intraday service's markers; its first after-close run
    must not be refused for a previous session this job never ran (I3)."""
    from trade_engine.ledger import EodRun

    rig.ledger.append(Event(
        account=ACCOUNT, kind=EventKind.EOD_RUN, ts_utc=rig.clock.now_utc(),
        command_id=f"intraday:intraday:{ACCOUNT}:{S1.isoformat()}",
        payload=EodRun(session=S1, job="intraday:intraday", account_id=ACCOUNT, bars_processed=3, at_close=rig.clock.now_utc()),
    ))
    rig.run(S1)
    assert rig.ledger.event_by_command(f"eod:eod:{ACCOUNT}:{S1.isoformat()}") is not None
    # Its own history still gates: skipping a session it did run refuses.
    with pytest.raises(EodRunnerError, match="no eod marker"):
        rig.run(S3)


# -- the morning pass: entries decided and filled on the morning's quotes ---------------------


def morning_time(session: date) -> datetime:
    return CAL.session_open(session) + timedelta(minutes=15)  # 09:45 ET


def chain_at(at: datetime, quotes: dict | None = None, price: str = "300", underlying: str = "COHR") -> ChainSnapshot:
    book = dict(BASE)
    book.update(quotes or {})
    rows = tuple(OptionQuote(c, D(b), D(a), D(10), D(10), at) for c, (b, a) in book.items() if c.underlying == underlying)
    return ChainSnapshot(underlying, at, D(price), rows, None, None, "test")


class Timed(Scripted):
    """Acts at the snapshot taken at a given instant, and records every snapshot it saw."""

    def __init__(self) -> None:
        super().__init__()
        self.by_time: dict[datetime, list] = {}
        self.seen: list[datetime] = []

    def manage_options(self, context):
        self.contexts.append(context)
        if context.phase != "snapshot":
            return list(self.at_close.get(context.session, ()))
        self.seen.append(context.snapshot.as_of)
        return list(self.by_time.get(context.snapshot.as_of, ()))


def morning_rig(rig: Rig, session: date = S2, morning_quotes: dict | None = None) -> Rig:
    """S1 done; ``session`` has a 09:45 snapshot and the 15:45 one."""
    rig.strategy = Timed()
    rig.run(S1)
    rig.snapshots[session] = [chain_at(morning_time(session), morning_quotes), chain(session)]
    return rig


def run_morning(rig: Rig, session: date, through: datetime | None = None, **changes):
    if rig.clock.now_utc() < CAL.session_open(session):
        rig.clock.advance_to(CAL.session_open(session))
    runner = EodRunner(rig.ledger, rig.clock, CAL, NoBars(), rig.config(**changes))
    return runner.run_morning(session, through or morning_time(session) + timedelta(minutes=5))


def seen(rig: Rig, since: int) -> list[datetime]:
    """The snapshots the strategy acted at since ``since``, each once (it is asked again
    after each action until it brings nothing new)."""
    return list(dict.fromkeys(rig.strategy.seen[since:]))


def new_process(rig: Rig, session: date) -> None:
    """The after-close run is a process of its own, with its clock at the open again."""
    rig.clock = ReplayClock(CAL.session_open(session))
    rig.venue = SnapshotVenue(ACCOUNT, rig.clock)
    rig.lifecycle = LifecyclePass(rig.ledger, rig.clock, CAL, rig.settlements, dividends=rig.dividends)


def morning_marker(session: date):
    return f"eod:eod-morning:{ACCOUNT}:{session.isoformat()}"


def test_an_entry_on_the_morning_snapshot_fills_on_the_mornings_quotes(rig) -> None:
    morning_rig(rig, morning_quotes={P270: ("8.00", "8.40")})
    rig.strategy.by_time[morning_time(S2)] = [csp(target=None, limit="8.00")]
    result = run_morning(rig, S2)
    assert rig.held(P270) == -1
    [fill] = [f for f in rig.state.fills if f.order_id == "csp:entry"]
    assert fill.filled_at == morning_time(S2) and fill.price <= D("8.40")
    [account] = result.accounts
    assert account.snapshots_processed == 1 and account.fills_recorded == 1
    marker = rig.ledger.event_by_command(morning_marker(S2)).payload
    assert marker.job == "eod-morning" and marker.at_close == morning_time(S2) + timedelta(minutes=5)


def test_the_morning_pass_stops_at_through(rig) -> None:
    morning_rig(rig)
    rig.strategy.by_time[snap_time(S2)] = [csp(target=None)]
    before = len(rig.strategy.seen)
    run_morning(rig, S2)
    assert seen(rig, before) == [morning_time(S2)] and rig.held(P270) == 0


def test_the_after_close_run_does_not_match_the_morning_snapshot_again(rig) -> None:
    morning_rig(rig)
    rig.strategy.by_time[morning_time(S2)] = [csp(target=None)]
    run_morning(rig, S2)
    before = len(rig.strategy.seen)
    new_process(rig, S2)
    result = rig.run(S2)
    assert seen(rig, before) == [snap_time(S2)]  # the 15:45 one only
    assert rig.held(P270) == -1
    [account] = result.accounts
    assert account.snapshots_processed == 1
    # The close still sees the session's newest quotes of each underlying.
    [close] = [c for c in rig.strategy.contexts if c.phase == "close" and c.session == S2]
    assert close.snapshots["COHR"].as_of == snap_time(S2)
    assert rig.ledger.event_by_command(f"eod:eod:{ACCOUNT}:{S2.isoformat()}") is not None


def test_a_morning_snapshot_is_the_newest_quote_when_no_later_one_comes(rig) -> None:
    other = OptionContract("NVDA", EXPIRY, D("150"), OptionRight.PUT)
    morning_rig(rig)
    rig.snapshots[S2] = [chain_at(morning_time(S2)), chain_at(morning_time(S2), {other: ("3.00", "3.20")}, "170", "NVDA"),
                         chain(S2)]
    run_morning(rig, S2)
    new_process(rig, S2)
    rig.run(S2)
    [close] = [c for c in rig.strategy.contexts if c.phase == "close" and c.session == S2]
    assert close.snapshots["NVDA"].as_of == morning_time(S2)


def test_without_a_morning_pass_the_after_close_run_matches_every_snapshot(rig) -> None:
    morning_rig(rig)
    rig.strategy.by_time[morning_time(S2)] = [csp(target=None)]
    before = len(rig.strategy.seen)
    rig.run(S2)
    assert seen(rig, before) == [morning_time(S2), snap_time(S2)] and rig.held(P270) == -1


def test_rerunning_the_morning_pass_appends_nothing(rig) -> None:
    morning_rig(rig)
    rig.strategy.by_time[morning_time(S2)] = [csp(target=None)]
    run_morning(rig, S2)
    count = len(list(rig.ledger.events(account=ACCOUNT)))
    new_process(rig, S2)
    result = run_morning(rig, S2)
    assert len(list(rig.ledger.events(account=ACCOUNT))) == count
    assert result.accounts[0].snapshots_processed == 0


def test_the_morning_pass_after_the_sessions_close_run_does_nothing(rig) -> None:
    morning_rig(rig)
    rig.run(S2)
    count = len(list(rig.ledger.events(account=ACCOUNT)))
    new_process(rig, S2)
    result = run_morning(rig, S2)
    assert len(list(rig.ledger.events(account=ACCOUNT))) == count
    assert rig.ledger.event_by_command(morning_marker(S2)) is None and result.accounts[0].snapshots_processed == 0


def test_the_morning_pass_needs_the_previous_session_complete(rig) -> None:
    morning_rig(rig)
    rig.snapshots[S3] = [chain_at(morning_time(S3))]
    with pytest.raises(EodRunnerError, match="no eod marker"):
        run_morning(rig, S3)


def test_a_session_begun_in_the_morning_still_needs_its_after_close_run(rig) -> None:
    rig.strategy = Timed()
    rig.snapshots[S1] = [chain_at(morning_time(S1)), chain(S1)]
    run_morning(rig, S1)  # the account's first ever pass
    new_process(rig, S2)
    with pytest.raises(EodRunnerError, match="no eod marker"):
        rig.run(S2)


def test_a_held_underlying_with_no_morning_snapshot_does_not_refuse_the_pass(rig) -> None:
    rig.strategy = Timed()
    rig.strategy.by_time[snap_time(S1)] = [csp(target=None)]
    rig.run(S1)
    assert rig.held(P270) == -1
    other = OptionContract("NVDA", EXPIRY, D("150"), OptionRight.PUT)
    # At 09:50 the store holds only what has been pulled so far: no COHR.
    rig.snapshots[S2] = [chain_at(morning_time(S2), {other: ("3.00", "3.20")}, "170", "NVDA")]
    run_morning(rig, S2)
    assert rig.ledger.event_by_command(morning_marker(S2)) is not None


@pytest.mark.parametrize(
    "through",
    [
        CAL.session_open(S2) - timedelta(minutes=1),
        CAL.session_close(S2),
        datetime(2026, 9, 28, 14, 0),  # naive
    ],
)
def test_a_morning_pass_that_does_not_end_inside_the_session_refuses(rig, through) -> None:
    morning_rig(rig)
    with pytest.raises(EodRunnerError, match="through|inside the session"):
        run_morning(rig, S2, through)


"""Seeded invariant walks over the production ledger (P0a-T6).

Each seed builds one interleaved event stream (equity orders with partial fills, round trips,
option expiry / exercise / assignment, combos, cash flows, marks, venue halts, risk controls,
the venue mirror, registry membership) and appends it one event at a time to a real SQLite
``Ledger``. About a third of seeds are perturbed (an event dropped, duplicated or swapped) so
the refusal paths run too; a refusal is recorded and checked, never swallowed.

Five invariants, each its own test, over every seed:

1. replaying the appended events from an empty state rebuilds the identical state;
2. closing and reopening the ledger file reopens to the identical state;
3. re-sending a command with a duplicate ``command_id`` writes nothing (the original comes back);
4. a refused command leaves the state and the event count unchanged;
5. every money field is a ``Decimal`` (never float), and quantities / prices the rules
   forbid from going negative stay non-negative.

Production only: this file imports ``trade_engine.*`` and no oracle package. The stream builder
below is its own variant of ``tests/ledger_gen.py``'s ``random_stream``, kept separate on purpose:
it writes command ids on every seed, so the duplicate-command invariant runs for all 50 (the shared
generator leaves 17 seeds with none, and that test would skip them).

A seed that finds a real violation is kept and marked ``xfail(strict=True)`` with the reason.
"""

from __future__ import annotations

import random
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from trade_engine.domain.instruments import (  # noqa: E402
    Combo,
    ComboLeg,
    Equity,
    OptionContract,
    Side,
)
from trade_engine.domain.orders import Order, OrderState, OrderType, TimeInForce  # noqa: E402
from trade_engine.domain.portfolio import Fill  # noqa: E402
from trade_engine.domain.risk import RiskControlChange, RiskRuleResult, RiskVerdict  # noqa: E402
from trade_engine.domain.signals import Signal  # noqa: E402
from trade_engine.interfaces.broker import Route  # noqa: E402
from trade_engine.ledger import codec  # noqa: E402
from trade_engine.ledger.events import (  # noqa: E402
    ACCOUNTS_ACCOUNT,
    AccountLedgerRecord,
    AccountMembership,
    CashFlow,
    EmulatedOrderState,
    EodRun,
    EodVoid,
    Event,
    EventKind,
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
    VenueHaltCleared,
    VenueReconcile,
    mirror_account,
)
from trade_engine.ledger.state import fold  # noqa: E402
from trade_engine.ledger.store import Ledger  # noqa: E402

SEEDS = tuple(range(50))
T0 = datetime(2026, 9, 24, 14, 30, tzinfo=timezone.utc)
ACC = "ACC"
VENUE = "D-00000001"

AAPL = Equity("AAPL")
C200 = OptionContract(underlying="AAPL", expiry=date(2026, 10, 16), strike=Decimal("200"), right="C")
P200 = OptionContract(underlying="AAPL", expiry=date(2026, 10, 16), strike=Decimal("200"), right="P")
P190 = OptionContract(underlying="AAPL", expiry=date(2026, 10, 16), strike=Decimal("190.50"), right="P")
SPX_C = OptionContract(underlying="SPX", expiry=date(2026, 10, 16), strike=Decimal("5000"), right="C")
SPREAD = Combo((ComboLeg(P200, 1, Side.SELL), ComboLeg(P190, 1, Side.BUY)))

# (seed, invariant) pairs where the production code violates the invariant, with the reason.
# Empty while every seed holds; a violation found by a walk is listed here, and its test is
# then marked xfail(strict=True) instead of being deleted.
KNOWN_VIOLATIONS: dict[tuple[int, str], str] = {}


def _params(invariant: str) -> list[Any]:
    out = []
    for seed in SEEDS:
        reason = KNOWN_VIOLATIONS.get((seed, invariant))
        if reason is None:
            out.append(pytest.param(seed, id=f"seed{seed}"))
        else:
            out.append(pytest.param(seed, id=f"seed{seed}",
                                    marks=pytest.mark.xfail(strict=True, reason=reason)))
    return out


# --- the stream builder (a port of the shared random_stream, production types only) ----


def _order(oid: str, **kw: Any) -> Order:
    fields_ = dict(
        order_id=oid,
        account_id=ACC,
        instrument=AAPL,
        order_type=OrderType.MARKET,
        side=Side.BUY,
        quantity=Decimal("100"),
        command_id=f"cmd-{oid}",
        created_at=T0,
    )
    fields_.update(kw)
    return Order(**fields_)


def _fill(fid: str, oid: str, **kw: Any) -> Fill:
    fields_ = dict(
        fill_id=fid,
        order_id=oid,
        account_id=ACC,
        instrument=AAPL,
        quantity=Decimal("100"),
        price=Decimal("100"),
        venue_env="sim",
        filled_at=T0,
        side=Side.BUY,
    )
    fields_.update(kw)
    return Fill(**fields_)


def _lifecycle(**kw: Any) -> OptionLifecycle:
    fields_ = dict(
        account_id=ACC,
        contract=C200,
        quantity=Decimal("2"),
        held=Side.BUY,
        underlying_price=Decimal("205.25"),
        price_source="eod",
        as_of=T0,
        reason="expiry",
    )
    fields_.update(kw)
    return OptionLifecycle(**fields_)


def _stream(seed: int) -> list[Event]:
    rnd = random.Random(seed)
    K = EventKind
    clock = [T0]
    ids = iter(range(10**6))
    evs: list[Event] = []

    def tick() -> datetime:
        clock[0] += timedelta(seconds=rnd.randint(1, 600))
        return clock[0]

    def px(lo: int = 1, hi: int = 400, places: int = 2) -> Decimal:
        return Decimal(rnd.randint(lo * 10**places, hi * 10**places)).scaleb(-places)

    def ev(kind: EventKind, payload: Any, account: str = ACC, **kw: Any) -> Event:
        return Event(account, kind, payload, tick(), **kw)

    def equity_episode() -> None:
        oid = f"o{next(ids)}"
        inst = rnd.choice([AAPL, Equity("MSFT"), Equity("NVDA")])
        side = rnd.choice([Side.BUY, Side.SELL])
        qty = Decimal(rnd.choice([1, 2, 3, 5, 10, 100]))
        ot = rnd.choice(list(OrderType))
        kw: dict[str, Any] = {}
        if ot in (OrderType.LIMIT, OrderType.STOP_LIMIT):
            kw["limit_price"] = px()
        if ot in (OrderType.STOP, OrderType.STOP_LIMIT):
            kw["stop_price"] = px()
        if ot is OrderType.TRAIL:
            kw["trail_amount"] = px(1, 5)
        o = _order(oid, instrument=inst, side=side, quantity=qty, order_type=ot, created_at=tick(),
                   tif=rnd.choice(list(TimeInForce)), **kw)
        evs.append(ev(K.ORDER_SUBMITTED, o, command_id=o.command_id))
        if rnd.random() < 0.25:  # venue silent: SUBMITTED -> PENDING_UNKNOWN, then resolved below
            evs.append(ev(K.ORDER_PENDING, OrderStateChange(oid)))
        if rnd.random() < 0.7:
            evs.append(ev(K.ORDER_ACCEPTED, OrderStateChange(oid, venue_order_id=str(rnd.randint(1, 99)))))
        if ot in (OrderType.STOP, OrderType.STOP_LIMIT) and rnd.random() < 0.7:
            evs.append(ev(K.ORDER_EMULATION_UPDATED, EmulatedOrderState(oid, px(), None, o.stop_price, False, "tick")))
            if rnd.random() < 0.6:
                evs.append(ev(K.ORDER_UPDATED, OrderUpdated(
                    _order(oid, instrument=inst, side=side, quantity=qty, order_type=ot, created_at=o.created_at,
                           tif=o.tif, state=OrderState.ACCEPTED, **{**kw, "stop_price": px()}), "amend")))
        remaining = qty
        for j in range(rnd.choice([0, 1, 1, 2, 3, 4])):
            if remaining <= 0:
                break
            q = Decimal(rnd.randint(1, int(remaining)))
            remaining -= q
            evs.append(ev(K.FILL, _fill(f"{oid}-f{j}", oid, instrument=inst, side=side, quantity=q,
                                        price=px(1, 300), fee=rnd.choice([Decimal(0), px(0, 3)]), filled_at=tick())))
        r = rnd.random()
        if remaining > 0 and r < 0.3:
            evs.append(ev(rnd.choice([K.ORDER_CANCELLED, K.ORDER_EXPIRED]), OrderStateChange(oid, reason="x")))
        elif r < 0.4:
            evs.append(ev(K.ORDER_REFUSED, OrderStateChange(oid, reason="risk")))

    def bracket_episode() -> None:
        """A bracket created as one batch, each leg then rejected or cancelled before submission."""
        inst = rnd.choice([AAPL, Equity("MSFT")])
        batch = tuple(_order(f"o{next(ids)}", instrument=inst, side=rnd.choice([Side.BUY, Side.SELL]),
                             quantity=Decimal(rnd.choice([1, 5])), created_at=tick())
                      for _ in range(rnd.randint(1, 3)))
        evs.append(ev(K.ORDERS_CREATED, OrdersCreated(batch, f"fp-{batch[0].order_id}", "bracket")))
        for o in batch:
            evs.append(ev(rnd.choice([K.ORDER_REJECTED, K.ORDER_CANCELLED]),
                          OrderStateChange(o.order_id, reason="x")))

    def round_trip_episode() -> None:
        """Buy then sell the same shares, so FIFO lots, flips and realized P&L are exercised."""
        inst = rnd.choice([AAPL, Equity("MSFT")])
        first = rnd.choice([Side.BUY, Side.SELL])
        for side, q in [(first, rnd.choice([3, 5, 10])),
                        (Side.SELL if first is Side.BUY else Side.BUY, rnd.choice([2, 4, 12]))]:
            oid = f"o{next(ids)}"
            o = _order(oid, instrument=inst, side=side, quantity=Decimal(q), created_at=tick())
            evs.append(ev(K.ORDER_SUBMITTED, o, command_id=o.command_id))
            evs.append(ev(K.FILL, _fill(f"{oid}-f", oid, instrument=inst, side=side, quantity=Decimal(q),
                                        price=px(1, 300), fee=px(0, 2), filled_at=tick())))

    def option_episode() -> None:
        kind = rnd.choice(["expire_long", "expire_short", "exercise", "assign", "cash"])
        contract, qty = rnd.choice([(C200, 2), (P200, 3), (P190, 1)])
        if kind == "cash":
            contract, qty = SPX_C, 2
        held = Side.SELL if kind in ("expire_short", "assign") else Side.BUY
        if kind == "cash":
            held = Side.BUY
        oid = f"o{next(ids)}"
        o = _order(oid, instrument=contract, side=held, quantity=Decimal(qty), created_at=tick())
        evs.append(ev(K.ORDER_SUBMITTED, o, command_id=o.command_id))
        evs.append(ev(K.FILL, _fill(f"{oid}-f", oid, instrument=contract, side=held, quantity=Decimal(qty),
                                    price=px(1, 20), fee=px(0, 2), filled_at=tick())))
        strike = contract.strike
        itm = (strike - px(1, 20)) if contract.right == "P" else (strike + px(1, 20))
        otm = (strike + px(1, 20)) if contract.right == "P" else max(strike - px(1, 20), Decimal(1))
        n = Decimal(rnd.randint(1, qty))
        if kind in ("expire_long", "expire_short"):
            evs.append(ev(K.EXPIRY, _lifecycle(contract=contract, quantity=n, held=held, underlying_price=otm)))
        elif kind == "cash":
            if rnd.random() < 0.3:
                evs.append(ev(K.EXPIRY, _lifecycle(contract=SPX_C, quantity=n, held=held, underlying_price=otm)))
            else:
                evs.append(ev(K.EXERCISE, _lifecycle(contract=SPX_C, quantity=n, held=Side.BUY, underlying_price=itm)))
        else:
            k2 = K.EXERCISE if kind == "exercise" else K.ASSIGNMENT
            evs.append(ev(k2, _lifecycle(contract=contract, quantity=n, held=held, underlying_price=itm,
                                         early=rnd.random() < 0.3)))

    def combo_episode() -> None:
        oid = f"o{next(ids)}"
        q = rnd.choice([1, 2, 3])
        o = _order(oid, instrument=SPREAD, quantity=Decimal(q), order_type=OrderType.LIMIT,
                   limit_price=px(1, 3), created_at=tick())
        evs.append(ev(K.ORDER_SUBMITTED, o, command_id=o.command_id))
        for u in range(rnd.randint(1, q)):
            for idx, leg in enumerate(SPREAD.legs):
                evs.append(ev(K.FILL, _fill(f"{oid}-u{u}-{idx}", oid, instrument=leg.contract, side=leg.side,
                                            quantity=Decimal(leg.ratio), price=px(1, 20), fee=px(0, 1),
                                            leg_id=str(idx), filled_at=tick())))

    def misc_episode() -> None:
        r = rnd.random()
        inst = rnd.choice([AAPL, C200, SPREAD])
        if r < 0.25:
            evs.append(ev(K.CASH_FLOW, CashFlow(px(1, 50) * rnd.choice([1, -1]),
                                                rnd.choice(["fee", "interest", "deposit"]), tick())))
        elif r < 0.5:
            evs.append(ev(K.MARK, Mark(inst, px(1, 300), tick(), source=rnd.choice(["last", "mid"]))))
            if rnd.random() < 0.5:  # equal key, different scale: the first key object is kept
                evs.append(ev(K.MARK, Mark(OptionContract(underlying="AAPL", expiry=date(2026, 10, 16),
                                                          strike=Decimal("200.00"), right="C"), px(), tick())))
        elif r < 0.6:
            evs.append(ev(K.SIGNAL_SEEN, Signal(f"s{next(ids)}", "scan", "AAPL", date(2026, 9, 24), "long")))
        elif r < 0.7:
            evs.append(ev(K.RISK_VERDICT, RiskVerdict(f"i{next(ids)}", evaluations=(RiskRuleResult("r", True, 1, 2, "ok"),),
                                                      approved_quantity=Decimal("1"))))
            evs.append(ev(K.RISK_VERDICT, RiskVerdict(f"i{next(ids)}", evaluations=(RiskRuleResult("r", False, 3, 2, "no"),),
                                                      refusal_reasons=("no",))))
        elif r < 0.8:
            evs.append(ev(K.RISK_CONTROL, RiskControlChange("kill", rnd.random() < 0.5, "why", tick())))
        elif r < 0.9:
            venue = rnd.choice(["tos", "ibkr"])
            evs.append(ev(K.VENUE_RECONCILE, VenueReconcile(venue, tick(), False, drift=("AAPL",))))
            if rnd.random() < 0.6:
                evs.append(ev(K.VENUE_HALT_CLEARED, VenueHaltCleared(venue, tick(), "ok", 1)))
            if rnd.random() < 0.4:
                evs.append(ev(K.VENUE_RECONCILE, VenueReconcile(venue, tick(), True)))
        else:
            evs.append(ev(K.EOD_RUN, EodRun(date(2026, 9, 24), "eod", ACC, 390, tick())))
            if rnd.random() < 0.3:
                evs.append(ev(K.EOD_VOID, EodVoid(date(2026, 9, 23), ACC, "cannot complete", "owner", "eod")))

    def mirror_episode() -> None:
        va = mirror_account(VENUE)
        n = next(ids)
        inst = rnd.choice([P200, SPREAD, C200])
        alloc = tuple(MirrorAllocation(f"so-{n}-{k}", "OPT_CSP", Decimal(rnd.randint(1, 3)))
                      for k in range(rnd.randint(1, 3)))
        total = sum((a.quantity for a in alloc), Decimal(0))
        key = f"tos:{n}"
        q = MirrorQueued(VENUE, key, inst, rnd.choice([Side.BUY, Side.SELL]), total, OrderType.LIMIT,
                         px(1, 5), TimeInForce.DAY, alloc, tick())
        evs.append(ev(K.MIRROR_QUEUED, q, account=va))
        if rnd.random() < 0.15:
            evs.append(ev(K.MIRROR_REFUSED, MirrorRefused(VENUE, f"so-r{n}", "OPT_CSP", "conflict", tick()), account=va))
        oid = str(5400000000 + n)
        evs.append(ev(K.MIRROR_ACK, MirrorAck(VENUE, key, "ACCEPTED", "ok", tick(), venue_order_id=oid,
                                              book_status=rnd.choice([OrderState.ACCEPTED, None])), account=va))
        cum = Decimal(0)
        for _ in range(rnd.randint(0, 3)):
            nxt = min(total, cum + Decimal(rnd.randint(1, 3)))
            if nxt == cum:
                break
            cum = nxt
            evs.append(ev(K.MIRROR_FILL, MirrorFill(VENUE, key, oid, cum, px(1, 5), tick()), account=va))
        if rnd.random() < 0.2:
            evs.append(ev(K.MIRROR_ACK, MirrorAck(VENUE, key, "REJECTED", "late", tick()), account=va))

    episodes = [equity_episode, round_trip_episode, option_episode, combo_episode, misc_episode,
                mirror_episode, bracket_episode]
    weights = [3, 3, 3, 2, 4, 2, 1]
    for _ in range(rnd.randint(2, 9)):
        rnd.choices(episodes, weights)[0]()

    if rnd.random() < 0.35 and evs:  # perturb: exercises the refusal paths
        i = rnd.randrange(len(evs))
        how = rnd.choice(["drop", "dup", "swap"])
        if how == "drop":
            del evs[i]
        elif how == "dup":
            evs.insert(i, evs[i])
        elif len(evs) > 1:
            j = rnd.randrange(len(evs))
            evs[i], evs[j] = evs[j], evs[i]
    if rnd.random() < 0.2:  # appended out of order: the fold refuses what precedes its order
        rnd.shuffle(evs)

    # Registry membership, from a separate RNG so the stream above keeps its shape.
    acc_rng = random.Random(seed * 7919 + 17)
    if acc_rng.random() < 0.8:
        n = acc_rng.randrange(1000)
        key = f"tos_paper:ACCT-TEST-{n:03}"
        shown = "*" + key[-3:]
        at = clock[0] + timedelta(seconds=acc_rng.randint(1, 60))
        record = AccountLedgerRecord(key, shown, "paper", "active", None, None, "margin",
                                     (Route("tos", f"local-{n}"),), "tos_paper", at)
        evs.append(Event(ACCOUNTS_ACCOUNT, K.ACCOUNT_ADDED, AccountMembership(key, shown, 1, at, record=record), at))
        if acc_rng.random() < 0.6:
            evs.append(Event(ACCOUNTS_ACCOUNT, K.ACCOUNT_CHANGED,
                             AccountMembership(key, shown, 2, at, changes=(("status", "active", "closed"),)), at))
        if acc_rng.random() < 0.3:
            evs.append(Event(ACCOUNTS_ACCOUNT, K.ACCOUNT_REMOVED, AccountMembership(key, shown, 3, at), at))
    return evs


# --- the walk: one real ledger per seed, built once and shared by the invariant tests -----------


@dataclass
class Walk:
    path: Path
    refused: list[str] = field(default_factory=list)             # refusal class names, in order
    refusal_violations: list[str] = field(default_factory=list)  # invariant 4 breaches


_WALKS: dict[int, Walk] = {}


def _canon_state(states: dict[str, Any]) -> Any:
    return codec.canon(states)


def _build(seed: int, base: Path) -> Walk:
    path = base / f"walk-{seed}.db"
    walk = Walk(path=path)
    with Ledger(path) as lg:
        for event in _stream(seed):
            count_before = lg.count()
            state_before = _canon_state(lg.fold())
            try:
                lg.append(event)
            except Exception as err:  # noqa: BLE001 - the refusal is the outcome under test
                walk.refused.append(type(err).__name__)
                if lg.count() != count_before:
                    walk.refusal_violations.append(f"refused {type(err).__name__} changed the event count")
                if _canon_state(lg.fold()) != state_before:
                    walk.refusal_violations.append(f"refused {type(err).__name__} changed the state")
    return walk


@pytest.fixture(scope="session")
def walk_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("invariant-walks")


def _walk(seed: int, base: Path) -> Walk:
    if seed not in _WALKS:
        _WALKS[seed] = _build(seed, base)
    return _WALKS[seed]


# --- the invariants -----------------------------------------------------------------------------


@pytest.mark.parametrize("seed", _params("replay"))
def test_replay_from_empty_rebuilds_identical_state(seed: int, walk_dir: Path) -> None:
    """Invariant 1: fold(appended events) from an empty state equals the ledger's own state."""
    walk = _walk(seed, walk_dir)
    with Ledger(walk.path) as lg:
        appended = lg.events()
        rebuilt = fold(appended)
        assert _canon_state(rebuilt) == _canon_state(lg.fold()), f"seed {seed}: replay diverged"


@pytest.mark.parametrize("seed", _params("reopen"))
def test_reopen_reproduces_identical_state(seed: int, walk_dir: Path) -> None:
    """Invariant 2: close the file, reopen it, and the events and the state are unchanged."""
    walk = _walk(seed, walk_dir)
    with Ledger(walk.path) as lg:
        before_events = [codec.encode_event(e) for e in lg.events()]
        before_state = _canon_state(lg.fold())
    with Ledger(walk.path) as lg:
        assert [codec.encode_event(e) for e in lg.events()] == before_events, f"seed {seed}: events changed"
        assert _canon_state(lg.fold()) == before_state, f"seed {seed}: state changed on reopen"


@pytest.mark.parametrize("seed", _params("duplicate_command"))
def test_duplicate_command_id_writes_nothing(seed: int, walk_dir: Path) -> None:
    """Invariant 3: re-sending a written command_id returns the original and appends nothing."""
    walk = _walk(seed, walk_dir)
    with Ledger(walk.path) as lg:
        written = [e for e in lg.events() if e.command_id is not None]
        if not written:
            pytest.skip(f"seed {seed} wrote no command id")
        for original in written[:3]:
            count_before = lg.count()
            state_before = _canon_state(lg.fold())
            same = Event(original.account, original.kind, original.payload, original.ts_utc,
                         command_id=original.command_id)
            # A resend of the same command, and one that carries a different payload.
            different = Event(original.account, EventKind.CASH_FLOW,
                              CashFlow(Decimal("1"), "fee", original.ts_utc), original.ts_utc,
                              command_id=original.command_id)
            for resend in (same, different):
                back = lg.append(resend)
                assert back.seq == original.seq, f"seed {seed}: {original.command_id} was written twice"
            assert lg.count() == count_before, f"seed {seed}: a duplicate command appended events"
            assert _canon_state(lg.fold()) == state_before, f"seed {seed}: a duplicate changed the state"


@pytest.mark.parametrize("seed", _params("refusal_is_atomic"))
def test_refused_command_leaves_state_and_count_unchanged(seed: int, walk_dir: Path) -> None:
    """Invariant 4: every refusal the walk met left the state and the event count as they were."""
    walk = _walk(seed, walk_dir)
    assert walk.refusal_violations == [], f"seed {seed}: " + "; ".join(walk.refusal_violations)


@pytest.mark.parametrize("seed", _params("money_types"))
def test_money_is_decimal_and_rule_bounded(seed: int, walk_dir: Path) -> None:
    """Invariant 5: money fields are Decimal, never float; quantities the rules bound stay >= 0 (or > 0)."""
    walk = _walk(seed, walk_dir)
    with Ledger(walk.path) as lg:
        states = lg.fold()
    problems: list[str] = []

    def money(where: str, value: Any) -> None:
        if isinstance(value, bool) or not isinstance(value, Decimal):
            problems.append(f"{where} is {type(value).__name__}, not Decimal")

    for account, state in states.items():
        at = f"{account}"
        money(f"{at}.cash", state.cash)
        money(f"{at}.realized_pnl", state.realized_pnl)
        for inst, price in state.marks.items():
            money(f"{at}.marks[{inst}]", price)
            if isinstance(price, Decimal) and price <= 0:
                problems.append(f"{at}.marks[{inst}] = {price} (marks are > 0)")
        for oid, qty in state.filled_quantity.items():
            money(f"{at}.filled_quantity[{oid}]", qty)
            if isinstance(qty, Decimal) and qty < 0:
                problems.append(f"{at}.filled_quantity[{oid}] = {qty} (cumulative fills are >= 0)")
        for key, qty in state.leg_filled.items():
            money(f"{at}.leg_filled[{key}]", qty)
            if isinstance(qty, Decimal) and qty < 0:
                problems.append(f"{at}.leg_filled[{key}] = {qty} (leg fills are >= 0)")
        for fill in state.fills:
            money(f"{at}.fill[{fill.fill_id}].price", fill.price)
            money(f"{at}.fill[{fill.fill_id}].fee", fill.fee)
            money(f"{at}.fill[{fill.fill_id}].quantity", fill.quantity)
            if isinstance(fill.quantity, Decimal) and fill.quantity <= 0:
                problems.append(f"{at}.fill[{fill.fill_id}].quantity = {fill.quantity} (fills are > 0)")
            if isinstance(fill.fee, Decimal) and fill.fee < 0:
                problems.append(f"{at}.fill[{fill.fill_id}].fee = {fill.fee} (fees are >= 0)")
        for inst, pos in state.positions.items():
            money(f"{at}.position[{inst}].avg_cost", pos.avg_cost)
            money(f"{at}.position[{inst}].realized_pnl", pos.realized_pnl)
            for lot in pos.open_lots:
                money(f"{at}.position[{inst}].lot[{lot.lot_id}].cost_basis", lot.cost_basis)
                money(f"{at}.position[{inst}].lot[{lot.lot_id}].quantity", lot.quantity)
                if isinstance(lot.quantity, Decimal) and lot.quantity <= 0:
                    problems.append(f"{at}.lot[{lot.lot_id}].quantity = {lot.quantity} (open lots are > 0)")
        for inst, qty in state.mirror.book.items():
            money(f"{at}.mirror.book[{inst}]", qty)
        for ticket_key, ticket in state.mirror.tickets.items():
            money(f"{at}.mirror.tickets[{ticket_key}].filled", ticket.filled)
            if ticket.avg_price is not None:
                money(f"{at}.mirror.tickets[{ticket_key}].avg_price", ticket.avg_price)
    assert problems == [], f"seed {seed}: " + "; ".join(problems[:5])

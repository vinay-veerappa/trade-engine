"""Event generators and the Python oracle for the P2a ledger parity tests.

Not a test module. `event_zoo()` builds one valid event for every `EventKind`
(every optional-field variant, all three instrument kinds, Decimal edge cases);
`py_reencode` / `py_fold` are the oracle the Rust port must reproduce, expressed in
the shape `trade_engine_rs.ledger_*` returns, so a comparison is a plain `==`.

Since P2b the oracle is the FROZEN pre-port Python (`tests/frozen_ledger/`), never the
production modules, which are now shims over Rust: parity is Rust vs pre-port Python.
`prod_*` run the production path (codec / fold shims) in the same shape, so the glue
between Python and Rust is held to the same oracle.
"""

from __future__ import annotations

import decimal
import json
import random
import sys
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from trade_engine.domain.instruments import (  # noqa: E402
    Combo,
    ComboLeg,
    Equity,
    OptionContract,
    Side,
    UnresolvableInstrumentError,
)
from trade_engine.domain.orders import (  # noqa: E402
    IllegalOrderStateTransitionError,
    Order,
    OrderState,
    OrderType,
    TimeInForce,
)
from trade_engine.domain.portfolio import Fill, Lot, Position  # noqa: E402
from trade_engine.domain.risk import RiskControlChange, RiskRuleResult, RiskVerdict  # noqa: E402
from trade_engine.domain.signals import Signal  # noqa: E402
from trade_engine.interfaces.market_data import CorporateAction  # noqa: E402
from frozen_ledger import codec  # noqa: E402  - the frozen oracle (P2b)
from trade_engine.ledger.events import (  # noqa: E402
    CashFlow,
    EodRun,
    EmulatedOrderState,
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
    VenueHaltCleared,
    VenueReconcile,
    mirror_account,
)
from frozen_ledger.state import AccountState, fold_account  # noqa: E402  - the frozen oracle (P2b)

UTC = timezone.utc
ET = timezone(timedelta(hours=-4))
ODD = timezone(timedelta(hours=5, minutes=30))
T0 = datetime(2026, 9, 24, 14, 30, tzinfo=UTC)

AAPL = Equity("AAPL")
C200 = OptionContract(underlying="AAPL", expiry=date(2026, 10, 16), strike=Decimal("200"), right="C")
P200 = OptionContract(underlying="AAPL", expiry=date(2026, 10, 16), strike=Decimal("200"), right="P")
P190 = OptionContract(underlying="AAPL", expiry=date(2026, 10, 16), strike=Decimal("190.50"), right="P")
SPREAD = Combo((ComboLeg(P200, 1, Side.SELL), ComboLeg(P190, 1, Side.BUY)))


def kind_of(err: BaseException) -> str:
    """The refusal category: what `rs_call` carries across the boundary.

    Matched by class NAME along the MRO: the frozen oracle and the production shims each
    define their own `LedgerFoldError` / `PayloadCodecError` / ..., and both must classify.
    """
    table = (
        ("PayloadCodecError", "codec"),
        ("EventPayloadError", "payload"),
        ("LedgerDuplicateFillError", "duplicate_fill"),
        ("LedgerFillMismatchError", "fill_mismatch"),
        ("UnhandledEventError", "unhandled"),
        ("MirrorFoldError", "mirror_fold"),
        ("LedgerFoldError", "fold"),
        ("IllegalOrderStateTransitionError", "illegal_transition"),
        ("UnresolvableInstrumentError", "unresolvable"),
        ("JSONDecodeError", "json"),
        ("UnicodeDecodeError", "value"),
        ("ValueError", "value"),
        ("KeyError", "key"),
        ("TypeError", "type"),
        ("AttributeError", "attribute"),
        ("InvalidOperation", "invalid_operation"),
        ("DivisionByZero", "division_by_zero"),
        ("Overflow", "overflow"),
        ("OverflowError", "overflow"),
    )
    names = {c.__name__ for c in type(err).__mro__}
    for name, kind in table:
        if name in names:
            return kind
    return "other:" + type(err).__name__


def dumps(obj) -> bytes:
    return json.dumps(obj, separators=(",", ":"), sort_keys=True).encode()


def py_reencode(data: bytes):
    """('ok', bytes) or ('err', kind, message): what the Python codec does with `data`."""
    try:
        node = json.loads(data.decode("utf-8"))
        return ("ok", dumps(codec.encode_event(codec.decode_event(node))))
    except Exception as err:  # noqa: BLE001 - classified, never swallowed
        return ("err", kind_of(err), str(err))


# --- canonical state ------------------------------------------------------------------

_CARRIERS = frozenset({"AccountState", "Position", "MirrorState", "MirrorTicketState"})


def canon(value):
    """Canonical JSON rendering of a fold result (AccountState, MirrorState, ...).

    Carriers are matched by class NAME, so a frozen-oracle state and a production state
    render identically when (and only when) their contents agree.
    """
    import dataclasses

    if dataclasses.is_dataclass(value) and type(value).__name__ in _CARRIERS:
        return {
            "dc": type(value).__name__,
            "f": {f.name: canon(getattr(value, f.name)) for f in dataclasses.fields(value)},
        }
    if isinstance(value, (frozenset, set)):
        items = [canon(v) for v in value]
        return {"fs": sorted(items, key=lambda j: json.dumps(j, sort_keys=True, separators=(",", ":")))}
    if isinstance(value, tuple):
        return {"t": [canon(v) for v in value]}
    if isinstance(value, dict) or (hasattr(value, "items") and not isinstance(value, (str, bytes))):
        return {"m": [[canon(k), canon(v)] for k, v in value.items()]}
    return codec._encode(value)


def py_fold(events: list[Event], account: str):
    """('ok', canonical-bytes) or ('err', kind, message) for `fold_account`."""
    try:
        state = fold_account(events, account)
        return ("ok", dumps(canon(state)))
    except Exception as err:  # noqa: BLE001 - classified, never swallowed
        return ("err", kind_of(err), str(err))


def py_fold_all(events: list[Event]):
    """('ok', canonical-bytes of {account: state}) or ('err', kind, message) for `fold`."""
    from frozen_ledger.state import fold

    try:
        return ("ok", dumps(canon(fold(events))))
    except Exception as err:  # noqa: BLE001 - classified, never swallowed
        return ("err", kind_of(err), str(err))


def encoded(events: list[Event]) -> list[bytes]:
    return [dumps(codec.encode_event(e)) for e in events]


# --- the production path, in the oracle's shape (P2b) ---------------------------------


def prod_reencode(data: bytes):
    """('ok', bytes) or ('err', kind, message): what the PRODUCTION codec does with `data`."""
    from trade_engine.ledger import codec as prod_codec

    try:
        node = json.loads(data.decode("utf-8"))
        return ("ok", dumps(prod_codec.encode_event(prod_codec.decode_event(node))))
    except Exception as err:  # noqa: BLE001 - classified, never swallowed
        return ("err", kind_of(err), str(err))


def prod_fold(events: list[Event], account: str):
    """`py_fold`, through the production `fold_account`."""
    from trade_engine.ledger.state import fold_account as prod_fold_account

    try:
        return ("ok", dumps(canon(prod_fold_account(events, account))))
    except Exception as err:  # noqa: BLE001 - classified, never swallowed
        return ("err", kind_of(err), str(err))


def prod_fold_all(events: list[Event]):
    """`py_fold_all`, through the production `fold`."""
    from trade_engine.ledger.state import fold as prod_fold_fn

    try:
        return ("ok", dumps(canon(prod_fold_fn(events))))
    except Exception as err:  # noqa: BLE001 - classified, never swallowed
        return ("err", kind_of(err), str(err))


# --- the zoo --------------------------------------------------------------------------


def _order(oid="o1", account="ACC", **kw) -> Order:
    fields = dict(
        order_id=oid,
        account_id=account,
        instrument=AAPL,
        order_type=OrderType.MARKET,
        side=Side.BUY,
        quantity=Decimal("100"),
        command_id=f"cmd-{oid}",
        created_at=T0,
    )
    fields.update(kw)
    return Order(**fields)


def _fill(fid="f1", oid="o1", account="ACC", **kw) -> Fill:
    fields = dict(
        fill_id=fid,
        order_id=oid,
        account_id=account,
        instrument=AAPL,
        quantity=Decimal("100"),
        price=Decimal("100"),
        venue_env="sim",
        filled_at=T0,
        side=Side.BUY,
    )
    fields.update(kw)
    return Fill(**fields)


def _lifecycle(**kw) -> OptionLifecycle:
    fields = dict(
        account_id="ACC",
        contract=C200,
        quantity=Decimal("2"),
        held=Side.BUY,
        underlying_price=Decimal("205.25"),
        price_source="eod",
        as_of=T0,
        reason="expiry",
    )
    fields.update(kw)
    return OptionLifecycle(**fields)


def _ev(kind: EventKind, payload, account="ACC", **kw) -> Event:
    return Event(account=account, kind=kind, payload=payload, ts_utc=kw.pop("ts", T0), **kw)


def event_zoo() -> list[Event]:
    """One valid event per EventKind, plus every optional-field / instrument variant."""
    V = "D-00000001"
    VA = mirror_account(V)
    K = EventKind
    queued = MirrorQueued(
        venue=V, ticket_key="tos:a", instrument=P200, side=Side.SELL, quantity=Decimal("2"),
        order_type=OrderType.LIMIT, limit_price=Decimal("2.00"), tif=TimeInForce.DAY,
        allocations=(MirrorAllocation("so-1", "OPT_CSP", Decimal("1")), MirrorAllocation("so-2", "OPT_CSP", Decimal("1"))),
        at=T0,
    )
    zoo = [
        # signals / risk
        _ev(K.SIGNAL_SEEN, Signal("s1", "scan", "AAPL", date(2026, 9, 24), "long")),
        _ev(K.SIGNAL_SEEN, Signal("s2", "scan", "MSFT", date(2026, 9, 24), "short",
                                  metrics={"rvol": Decimal("1.50"), "gap": Decimal("-0.0"), "zz": Decimal("1E+3")},
                                  next_earnings_date=date(2026, 10, 29), created_at=datetime(2026, 9, 24, 9, 0, tzinfo=ET))),
        _ev(K.RISK_VERDICT, RiskVerdict("i1", evaluations=(RiskRuleResult("r", True, Decimal("1"), Decimal("2"), "ok"),),
                                        approved_quantity=Decimal("50"))),
        _ev(K.RISK_VERDICT, RiskVerdict("i2", evaluations=(RiskRuleResult("r", False, 3, "x", "no"),),
                                        refusal_reasons=("too big",))),
        _ev(K.RISK_CONTROL, RiskControlChange("kill", True, "manual", T0)),
        # orders
        _ev(K.ORDERS_CREATED, OrdersCreated((_order("o1"), _order("o2", order_type=OrderType.LIMIT, limit_price=Decimal("99.5"),
                                                                 tif=TimeInForce.GTC)), "fp", "bracket")),
        _ev(K.ORDER_SUBMITTED, _order("o3", order_type=OrderType.STOP_LIMIT, limit_price=Decimal("10"), stop_price=Decimal("11"),
                                      parent_order_id="o1", oco_group="g")),
        _ev(K.ORDER_SUBMITTED, _order("o4", order_type=OrderType.TRAIL, trail_amount=Decimal("0.25"),
                                      instrument=C200, quantity=Decimal("1E+1"))),
        _ev(K.ORDER_SUBMITTED, _order("o5", instrument=SPREAD, quantity=Decimal("3"), order_type=OrderType.LIMIT,
                                      limit_price=Decimal("1.05"))),
        _ev(K.ORDER_UPDATED, OrderUpdated(_order("o1", stop_price=None), "amend", venue_order_id="5400000001")),
        _ev(K.ORDER_PENDING, OrderStateChange("o1")),
        _ev(K.ORDER_ACCEPTED, OrderStateChange("o1", venue_order_id="7")),
        _ev(K.ORDER_REJECTED, OrderStateChange("o1", reason="bad")),
        _ev(K.ORDER_CANCELLED, OrderStateChange("o1", reason="user", venue_order_id="7")),
        _ev(K.ORDER_REFUSED, OrderStateChange("o1", reason="risk")),
        _ev(K.ORDER_EXPIRED, OrderStateChange("o1", reason="eod")),
        _ev(K.ORDER_EMULATION_UPDATED, EmulatedOrderState("o1", Decimal("100"), None, Decimal("99.5"), False, "tick")),
        _ev(K.ORDER_EMULATION_UPDATED, EmulatedOrderState("o1", None, Decimal("101"), None, True, "trig")),
        # fills
        _ev(K.FILL, _fill()),
        _ev(K.FILL, _fill("f2", fee=Decimal("0.35"), leg_id="0", venue_order_id="9", venue_execution_id="e-1",
                          instrument=C200, filled_at=datetime(2026, 9, 24, 10, 0, 0, 123456, tzinfo=ET),
                          price=Decimal("1.2500"), quantity=Decimal("1E+1"))),
        _ev(K.FILL, _fill("f3", instrument=SPREAD, side=Side.SELL)),
        _ev(K.FILL, _fill("f4", venue_env="live", side=Side.SELL, filled_at=datetime(2026, 1, 2, 3, 4, 5, tzinfo=ODD))),
        # lifecycle
        _ev(K.EXPIRY, _lifecycle()),
        _ev(K.EXERCISE, _lifecycle(early=True, held=Side.SELL, reason="early")),
        _ev(K.ASSIGNMENT, _lifecycle(contract=P200, quantity=Decimal("3"), underlying_price=Decimal("150"))),
        _ev(K.CORPORATE_ACTION, CorporateAction("AAPL", "split", date(2026, 9, 25), T0,
                                                details={"ratio": Decimal("4"), "note": "x"})),
        # cash / marks / venue
        _ev(K.CASH_FLOW, CashFlow(Decimal("-12.340"), "fee", T0)),
        _ev(K.CASH_FLOW, CashFlow(Decimal("1E+4"), "deposit", T0, note="seed")),
        _ev(K.MARK, Mark(AAPL, Decimal("101.5"), T0)),
        _ev(K.MARK, Mark(P190, Decimal("0.05"), T0, source="mid")),
        _ev(K.MARK, Mark(SPREAD, Decimal("1.00"), T0)),
        _ev(K.VENUE_RECONCILE, VenueReconcile("tos", T0, True)),
        _ev(K.VENUE_RECONCILE, VenueReconcile("tos", T0, False, drift=("AAPL", "MSFT"), note="n")),
        _ev(K.VENUE_HALT_CLEARED, VenueHaltCleared("tos", T0, "checked", 4)),
        _ev(K.EOD_RUN, EodRun(date(2026, 9, 24), "eod", "ACC", 390, T0)),
        # mirror
        _ev(K.MIRROR_QUEUED, queued, account=VA),
        _ev(K.MIRROR_QUEUED, MirrorQueued(V, "tos:b", SPREAD, Side.SELL, Decimal("1"), OrderType.MARKET, None, TimeInForce.GTC,
                                         (MirrorAllocation("so-3", "OPT_CSP", Decimal("1")),), T0), account=VA),
        _ev(K.MIRROR_QUEUED, MirrorQueued(V, "tos:s", AAPL, Side.BUY, Decimal("100"), OrderType.LIMIT, Decimal("189.5"),
                                         TimeInForce.DAY, (MirrorAllocation("so-4", "OPT_COVERED_CALL", Decimal("100")),),
                                         T0), account=VA),
        _ev(K.MIRROR_REFUSED, MirrorRefused(V, "so-9", "OPT_CSP", "conflict", T0), account=VA),
        _ev(K.MIRROR_ACK, MirrorAck(V, "tos:a", "ACCEPTED", "read back", T0, venue_order_id="5400000001",
                                    book_status=OrderState.ACCEPTED), account=VA),
        _ev(K.MIRROR_ACK, MirrorAck(V, "tos:a", "PENDING", "wait", T0), account=VA),
        _ev(K.MIRROR_FILL, MirrorFill(V, "tos:a", "5400000001", Decimal("2"), Decimal("2.05"), T0), account=VA),
    ]
    # envelope variants
    zoo.append(_ev(K.MARK, Mark(AAPL, Decimal("1E-28"), T0), command_id="c-1", seq=7, ts=datetime(2026, 1, 1, tzinfo=ODD)))
    zoo.append(_ev(K.MARK, Mark(AAPL, Decimal("123456789012345678901234567890.123456789"), T0), seq=1))
    zoo.append(_ev(K.CASH_FLOW, CashFlow(Decimal("0E-10"), "interest", T0)))
    zoo.append(_ev(K.CASH_FLOW, CashFlow(Decimal("-0"), "dividend", T0)))
    zoo.append(_ev(K.CASH_FLOW, CashFlow(Decimal("00012.50"), "borrow", T0)))
    zoo.append(_ev(K.CASH_FLOW, CashFlow(Decimal("5"), "withdrawal", datetime(2026, 9, 24, 14, 30, 1, 500, tzinfo=UTC))))
    return zoo


def zoo_kinds() -> set[EventKind]:
    return {e.kind for e in event_zoo()}


# --- a seeded generator of streams ----------------------------------------------------

SPX_C = OptionContract(underlying="SPX", expiry=date(2026, 10, 16), strike=Decimal("5000"), right="C")


def random_stream(seed: int) -> tuple[list[Event], str]:
    """An interleaved stream for one account built from valid episodes (equity orders with
    partial fills, option positions settled by expiry / exercise / assignment, combos,
    cash flows, marks, venue halts, risk controls, the mirror), then, for about a third of
    seeds, perturbed (an event dropped, duplicated, swapped or retargeted) so the refusal
    paths are exercised too. `seq` is stamped on every event for some seeds."""
    rnd = random.Random(seed)
    K = EventKind
    acct = "ACC"
    clock = [T0]

    def tick() -> datetime:
        clock[0] += timedelta(seconds=rnd.randint(1, 600))
        return clock[0]

    def px(lo=1, hi=400, places=2) -> Decimal:
        return Decimal(rnd.randint(lo * 10**places, hi * 10**places)).scaleb(-places)

    def ev(kind, payload, account=acct, **kw):
        return Event(account, kind, payload, tick(), **kw)

    evs: list[Event] = []
    ids = iter(range(10**6))

    def equity_episode():
        oid = f"o{next(ids)}"
        inst = rnd.choice([AAPL, Equity("MSFT"), Equity("NVDA")])
        side = rnd.choice([Side.BUY, Side.SELL])
        qty = Decimal(rnd.choice([1, 2, 3, 5, 10, 100]))
        ot = rnd.choice(list(OrderType))
        kw = {}
        if ot in (OrderType.LIMIT, OrderType.STOP_LIMIT):
            kw["limit_price"] = px()
        if ot in (OrderType.STOP, OrderType.STOP_LIMIT):
            kw["stop_price"] = px()
        if ot is OrderType.TRAIL:
            kw["trail_amount"] = px(1, 5)
        o = _order(oid, acct, instrument=inst, side=side, quantity=qty, order_type=ot, created_at=tick(),
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
                    _order(oid, acct, instrument=inst, side=side, quantity=qty, order_type=ot, created_at=o.created_at,
                           tif=o.tif, state=OrderState.ACCEPTED, **{**kw, "stop_price": px()}), "amend")))
        remaining = qty
        for j in range(rnd.choice([0, 1, 1, 2, 3, 4])):
            if remaining <= 0:
                break
            q = Decimal(rnd.randint(1, int(remaining)))
            remaining -= q
            evs.append(ev(K.FILL, _fill(f"{oid}-f{j}", oid, acct, instrument=inst, side=side, quantity=q,
                                        price=px(1, 300), fee=rnd.choice([Decimal(0), px(0, 3)]), filled_at=tick())))
        r = rnd.random()
        if remaining > 0 and r < 0.3:
            evs.append(ev(rnd.choice([K.ORDER_CANCELLED, K.ORDER_EXPIRED]), OrderStateChange(oid, reason="x")))
        elif r < 0.4:
            evs.append(ev(K.ORDER_REFUSED, OrderStateChange(oid, reason="risk")))

    def bracket_episode():
        """A bracket created as one batch (orders in NEW), each leg then rejected or cancelled
        before submission -- the only accepted-stream route for OrdersCreated / OrderRejected."""
        inst = rnd.choice([AAPL, Equity("MSFT")])
        batch = tuple(_order(f"o{next(ids)}", acct, instrument=inst, side=rnd.choice([Side.BUY, Side.SELL]),
                             quantity=Decimal(rnd.choice([1, 5])), created_at=tick())
                      for _ in range(rnd.randint(1, 3)))
        evs.append(ev(K.ORDERS_CREATED, OrdersCreated(batch, f"fp-{batch[0].order_id}", "bracket")))
        for o in batch:
            evs.append(ev(rnd.choice([K.ORDER_REJECTED, K.ORDER_CANCELLED]),
                          OrderStateChange(o.order_id, reason="x")))

    def round_trip_episode():
        """Buy then sell the same shares, so FIFO lots, flips and realized P&L are exercised."""
        inst = rnd.choice([AAPL, Equity("MSFT")])
        first = rnd.choice([Side.BUY, Side.SELL])
        for side, q in [(first, rnd.choice([3, 5, 10])),
                        (Side.SELL if first is Side.BUY else Side.BUY, rnd.choice([2, 4, 12]))]:
            oid = f"o{next(ids)}"
            o = _order(oid, acct, instrument=inst, side=side, quantity=Decimal(q), created_at=tick())
            evs.append(ev(K.ORDER_SUBMITTED, o, command_id=o.command_id))
            evs.append(ev(K.FILL, _fill(f"{oid}-f", oid, acct, instrument=inst, side=side, quantity=Decimal(q),
                                        price=px(1, 300), fee=px(0, 2), filled_at=tick())))

    def option_episode():
        kind = rnd.choice(["expire_long", "expire_short", "exercise", "assign", "cash"])
        contract, qty = rnd.choice([(C200, 2), (P200, 3), (P190, 1)])
        if kind == "cash":
            contract, qty = SPX_C, 2
        held = Side.SELL if kind in ("expire_short", "assign") else Side.BUY
        if kind == "cash":
            held = Side.BUY
        oid = f"o{next(ids)}"
        o = _order(oid, acct, instrument=contract, side=held, quantity=Decimal(qty), created_at=tick())
        evs.append(ev(K.ORDER_SUBMITTED, o, command_id=o.command_id))
        evs.append(ev(K.FILL, _fill(f"{oid}-f", oid, acct, instrument=contract, side=held, quantity=Decimal(qty),
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

    def combo_episode():
        oid = f"o{next(ids)}"
        q = rnd.choice([1, 2, 3])
        o = _order(oid, acct, instrument=SPREAD, quantity=Decimal(q), order_type=OrderType.LIMIT, limit_price=px(1, 3),
                   created_at=tick())
        evs.append(ev(K.ORDER_SUBMITTED, o, command_id=o.command_id))
        for u in range(rnd.randint(1, q)):
            for idx, leg in enumerate(SPREAD.legs):
                evs.append(ev(K.FILL, _fill(f"{oid}-u{u}-{idx}", oid, acct, instrument=leg.contract, side=leg.side,
                                            quantity=Decimal(leg.ratio), price=px(1, 20), fee=px(0, 1), leg_id=str(idx),
                                            filled_at=tick())))

    def misc_episode():
        r = rnd.random()
        inst = rnd.choice([AAPL, C200, SPREAD])
        if r < 0.25:
            evs.append(ev(K.CASH_FLOW, CashFlow(px(1, 50) * rnd.choice([1, -1]),
                                                rnd.choice(["fee", "interest", "deposit"]), tick())))
        elif r < 0.5:
            evs.append(ev(K.MARK, Mark(inst, px(1, 300), tick(), source=rnd.choice(["last", "mid"]))))
            if rnd.random() < 0.5:  # equal key, different scale: first key object is kept
                evs.append(ev(K.MARK, Mark(OptionContract(underlying="AAPL", expiry=date(2026, 10, 16),
                                                          strike=Decimal("200.00"), right="C"), px(), tick())))
        elif r < 0.6:
            evs.append(ev(K.SIGNAL_SEEN, Signal(f"s{next(ids)}", "scan", "AAPL", date(2026, 9, 24), "long")))
        elif r < 0.7:
            evs.append(ev(K.RISK_VERDICT, RiskVerdict(f"i{next(ids)}", evaluations=(RiskRuleResult("r", True, 1, 2, "ok"),), approved_quantity=Decimal("1"))))
            evs.append(ev(K.RISK_VERDICT, RiskVerdict(f"i{next(ids)}", evaluations=(RiskRuleResult("r", False, 3, 2, "no"),), refusal_reasons=("no",))))
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
            evs.append(ev(K.EOD_RUN, EodRun(date(2026, 9, 24), "eod", acct, 390, tick())))

    def mirror_episode():
        V = "D-00000001"
        VA = mirror_account(V)
        n = next(ids)
        inst = rnd.choice([P200, SPREAD, C200])
        alloc = tuple(MirrorAllocation(f"so-{n}-{k}", "OPT_CSP", Decimal(rnd.randint(1, 3)))
                      for k in range(rnd.randint(1, 3)))
        total = sum((a.quantity for a in alloc), Decimal(0))
        key = f"tos:{n}"
        q = MirrorQueued(V, key, inst, rnd.choice([Side.BUY, Side.SELL]), total, OrderType.LIMIT,
                         px(1, 5), TimeInForce.DAY, alloc, tick())
        evs.append(ev(K.MIRROR_QUEUED, q, account=VA))
        if rnd.random() < 0.15:
            evs.append(ev(K.MIRROR_REFUSED, MirrorRefused(V, f"so-r{n}", "OPT_CSP", "conflict", tick()), account=VA))
        oid = str(5400000000 + n)
        evs.append(ev(K.MIRROR_ACK, MirrorAck(V, key, "ACCEPTED", "ok", tick(), venue_order_id=oid,
                                              book_status=rnd.choice([OrderState.ACCEPTED, None])), account=VA))
        cum = Decimal(0)
        for _ in range(rnd.randint(0, 3)):
            nxt = min(total, cum + Decimal(rnd.randint(1, 3)))
            if nxt == cum:
                break
            cum = nxt
            evs.append(ev(K.MIRROR_FILL, MirrorFill(V, key, oid, cum, px(1, 5), tick()), account=VA))
        if rnd.random() < 0.2:
            evs.append(ev(K.MIRROR_ACK, MirrorAck(V, key, "REJECTED", "late", tick()), account=VA))

    episodes = [equity_episode, round_trip_episode, option_episode, combo_episode, misc_episode, mirror_episode,
                bracket_episode]
    weights = [3, 3, 3, 2, 4, 2, 1]
    for _ in range(rnd.randint(2, 9)):
        rnd.choices(episodes, weights)[0]()

    if rnd.random() < 0.35 and evs:  # perturb: the refusal paths
        i = rnd.randrange(len(evs))
        how = rnd.choice(["drop", "dup", "swap"])
        if how == "drop":
            del evs[i]
        elif how == "dup":
            evs.insert(i, evs[i])
        elif len(evs) > 1:
            j = rnd.randrange(len(evs))
            evs[i], evs[j] = evs[j], evs[i]
    if rnd.random() < 0.4:  # a sequence on every event, the log shuffled: `_ordered` sorts it
        stamped = [Event(e.account, e.kind, e.payload, e.ts_utc, command_id=e.command_id, seq=k + 1)
                   for k, e in enumerate(evs)]
        if rnd.random() < 0.5:
            rnd.shuffle(stamped)
        evs = stamped
    return evs, acct


# --- P7: the oracles keep their old spelling; parity is by VALUE ------------------------------
#
# The frozen oracles (and the pre-P7 fixtures) spell a decimal as `str(Decimal)`; the
# production path spells it canonically (docs/RUST_PORT.md P7 S1). Comparing the two is done
# after re-spelling BOTH sides with `norm`, so a value difference still fails and a spelling
# difference does not. A literal outside the canonical bound is refused by production and
# accepted by the oracle: `outside_bound` names those, the one sanctioned asymmetry.


def _respell(node):
    from trade_engine.ledger.codec import DecimalRangeError, canon_decimal

    if isinstance(node, dict):
        if set(node) == {"d"} and isinstance(node["d"], str):
            try:
                return {"d": canon_decimal(Decimal(node["d"]))}
            except (DecimalRangeError, ArithmeticError, ValueError):
                return node
        return {k: _respell(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_respell(v) for v in node]
    return node


def norm(data: bytes) -> bytes:
    """`data` (an encoded event or state, JSON bytes) with every decimal canonically spelled."""
    return dumps(_respell(json.loads(data.decode("utf-8"))))


def norm_outcome(outcome):
    """An outcome tuple ('ok', bytes) with its bytes normalized; refusals pass through."""
    if outcome[0] == "ok" and isinstance(outcome[1], (bytes, bytearray)):
        return ("ok", norm(bytes(outcome[1])))
    return outcome


def outside_bound(data: bytes) -> bool:
    """True when some `{"d": text}` in `data` is not representable canonically (NaN,
    Infinity, beyond 96 bits or 28 places), or does not parse as a decimal at all."""
    from trade_engine.ledger.codec import DecimalRangeError, canon_decimal

    def walk(node) -> bool:
        if isinstance(node, dict):
            if set(node) == {"d"} and isinstance(node["d"], str):
                try:
                    canon_decimal(Decimal(node["d"]))
                except (DecimalRangeError, ArithmeticError, ValueError):
                    return True
                return False
            return any(walk(v) for v in node.values())
        if isinstance(node, list):
            return any(walk(v) for v in node)
        return False

    try:
        return walk(json.loads(data.decode("utf-8")))
    except (ValueError, UnicodeDecodeError):
        return False

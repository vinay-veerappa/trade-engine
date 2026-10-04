"""Frozen P4b lockstep: exact decimal spelling, refusal text and ordered host effects."""
from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from dataclasses import fields, is_dataclass, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
import json
from pathlib import Path
import random
import sys
from types import SimpleNamespace

import pytest
import trade_engine_rs  # D5: a missing extension is an error.

sys.path.insert(0, str(Path(__file__).resolve().parent))
from frozen_p4b import after_close as O, sources as OS, journal as OJ
from trade_engine.lifecycle import after_close as P, sources as PS
from trade_engine.sinks import journal as PJ
from trade_engine.domain.instruments import Equity, Side
from trade_engine.domain.option_roots import SettleTime
from trade_engine.interfaces.market_data import StaleDataError
from trade_engine.interfaces.sinks import JournalExecution
from trade_engine.ledger import codec
from test_option_lifecycle import Book, CAL, EXPIRY, AFTER, CLOSE, OPENED, option

D = Decimal
UTC = timezone.utc
TALLIES = {}


def norm(v):
    if isinstance(v, Decimal):
        return ("Decimal", str(v))
    if isinstance(v, datetime):
        return v.isoformat()
    if isinstance(v, Enum):
        return v.value
    if is_dataclass(v):
        return tuple((f.name, norm(getattr(v, f.name))) for f in fields(v))
    if isinstance(v, Mapping):
        return sorted(((norm(k), norm(x)) for k, x in v.items()), key=repr)
    if isinstance(v, (tuple, list)):
        return tuple(map(norm, v))
    return v


def result(fn):
    try:
        return ("ok", norm(fn()))
    except Exception as exc:
        return ("raise", type(exc).__name__, str(exc))


def compare(label, oracle, production, counts):
    a, b = result(oracle), result(production)
    assert a == b, (label, a, b)
    counts["steps"] += 1
    counts[label + ":" + a[0]] += 1
    if a[0] == "raise":
        counts["refusal:" + a[1]] += 1
    return a


def events(ledger):
    return [(e.seq, e.account, e.kind.value, e.command_id, e.ts_utc.isoformat(),
             codec.text(codec.encode_payload(e.payload))) for e in ledger.events()]


class RecordedLedger:
    def __init__(self, ledger, trace):
        self.ledger, self.trace = ledger, trace

    def accounts(self):
        self.trace.append(("accounts",))
        return self.ledger.accounts()

    def state(self, account):
        self.trace.append(("state", account))
        return self.ledger.state(account)

    def extend(self, items):
        self.trace.append(("extend", norm(items)))
        return self.ledger.extend(items)


MODES = ("normal", "non_session", "before_close", "overdue", "invalid_expiry",
         "settlement_missing", "price_wrong", "price_future", "price_before",
         "dividend_source", "dividend_unknown", "dividend_future", "quote_source",
         "quote_missing", "quote_stale", "quote_future", "no_dividends", "otm",
         "extrinsic_equal", "extrinsic_high")


class Sources:
    def __init__(self, module, trace, price, bid, amount):
        self.module, self.trace = module, trace
        self.price, self.bid, self.amount = price, bid, amount
        self.mode = "normal"

    def settlement(self, underlying, session, settle_time):
        self.trace.append(("settlement", underlying, session, settle_time.value))
        if self.mode == "settlement_missing":
            raise StaleDataError(f"No {settle_time.value} settlement for {underlying} on {session} (I5)")
        known = (CAL.session_open(session) if settle_time is SettleTime.AM
                 else CAL.session_close(session)) + timedelta(minutes=70)
        if self.mode == "price_future":
            known = AFTER + timedelta(microseconds=1)
        if self.mode == "price_before":
            known = (CAL.session_open(session) if settle_time is SettleTime.AM
                     else CAL.session_close(session)) - timedelta(microseconds=1)
        return self.module.SettlementPrice("MSFT" if self.mode == "price_wrong" else underlying,
            session, settle_time, D("99.999") if self.mode == "otm" else self.price,
            "officiel 世界", known)

    def dividends(self, underlying, ex_date):
        self.trace.append(("dividends", underlying, ex_date))
        if self.mode == "dividend_unknown":
            raise StaleDataError(f"No dividend record for {underlying} (I5)")
        items = [] if self.mode == "no_dividends" else [
            self.module.Dividend(underlying, ex_date, self.amount / 2, "source 🦊",
                                AFTER + timedelta(microseconds=1) if self.mode == "dividend_future" else CLOSE),
            self.module.Dividend(underlying, ex_date, self.amount / 2, "source 🦊", CLOSE),
            self.module.Dividend(underlying, CAL.next_session(ex_date), D("999"), "ignored ex-date", CLOSE),
        ]
        return self.module.FixedDividends({underlying: items}).dividends(underlying, ex_date)

    def quote(self, contract, now):
        self.trace.append(("quote", contract.occ, now.isoformat()))
        if self.mode in ("quote_missing", "quote_stale"):
            raise StaleDataError(f"{contract.occ.strip()} {self.mode} (I5)")
        bid = self.bid
        if self.mode == "extrinsic_equal":
            bid = self.price - contract.strike + self.amount
        if self.mode == "extrinsic_high":
            bid = self.price - contract.strike + self.amount + D(".0001")
        return SimpleNamespace(bid=bid, as_of=AFTER + timedelta(microseconds=1)
                               if self.mode == "quote_future" else CLOSE)


def test_lifecycle_seeded_worlds(tmp_path):
    rng = random.Random(0x504B)
    counts = Counter()
    for mode in MODES:
        for seed in range(5):
            world = []
            for label, module, sources in (("oracle", O, OS), ("production", P, PS)):
                path = tmp_path / f"{mode}-{seed}-{label}"
                path.mkdir()
                book = Book(path)
                for account in ("beta", "alpha"):
                    for j, root in enumerate(("AAPL", "SPXW", "SPX")):
                        for right in ("C", "P"):
                            expiry = (CAL.previous_session(EXPIRY) if mode == "overdue" else
                                      EXPIRY + timedelta(days=1) if mode == "invalid_expiry" else EXPIRY)
                            book.trade(option(right, str(100 + j), root=root, expiry=expiry),
                                       Side.BUY if seed % 2 else Side.SELL, str(1 + seed % 3),
                                       "2.500", account)
                    book.trade(option("C", "100", expiry=CAL.next_session(EXPIRY)),
                               Side.SELL, "2", "1.25", account)
                    book.trade(Equity("MSFT"), Side.BUY, "10", "123.45", account)
                trace = []
                src = Sources(sources, trace, D("101.010") + D(seed).scaleb(-3),
                              D("1.010"), D("0.75"))
                src.mode = mode
                session = (EXPIRY + timedelta(days=1) if mode == "non_session" else
                           CAL.next_session(EXPIRY) if mode == "invalid_expiry" else EXPIRY)
                at = CLOSE - timedelta(microseconds=1) if mode == "before_close" else AFTER
                lifecycle = module.LifecyclePass(RecordedLedger(book.ledger, trace),
                    SimpleNamespace(now_utc=lambda at=at: at), CAL, src,
                    dividends=None if mode == "dividend_source" else src,
                    quotes=None if mode == "quote_source" else src)
                world.append(SimpleNamespace(book=book, trace=trace, src=src,
                                             lifecycle=lifecycle, session=session))
            try:
                # The account iterator deliberately arrives unsorted, or is omitted.
                accounts = ["beta", "alpha"] if rng.randrange(2) else None
                before = events(world[0].book.ledger)
                outcome = compare(mode, lambda: world[0].lifecycle.run(world[0].session, accounts),
                                  lambda: world[1].lifecycle.run(world[1].session, accounts), counts)
                assert world[0].trace == world[1].trace, mode
                assert events(world[0].book.ledger) == events(world[1].book.ledger), mode
                if outcome[0] == "raise":
                    assert events(world[0].book.ledger) == before, mode
                for w in world:
                    w.trace.clear()
                    w.src.mode = "no_dividends"
                    w.lifecycle._dividends = w.src
                    w.lifecycle._quotes = w.src
                    w.lifecycle._clock = SimpleNamespace(now_utc=lambda: AFTER)
                    if mode == "overdue":
                        w.session = CAL.previous_session(EXPIRY)
                    else:
                        w.session = EXPIRY
                recovered = compare(mode + "_counterpart",
                    lambda: world[0].lifecycle.run(world[0].session, accounts),
                    lambda: world[1].lifecycle.run(world[1].session, accounts), counts)
                assert world[0].trace == world[1].trace, mode
                assert events(world[0].book.ledger) == events(world[1].book.ledger), mode
                for account in ("alpha", "beta"):
                    assert norm(world[0].book.ledger.state(account)) == norm(world[1].book.ledger.state(account))
                if recovered[0] == "ok":
                    for w in world:
                        w.trace.clear()
                    compare(mode + "_replay",
                        lambda: world[0].lifecycle.run(world[0].session),
                        lambda: world[1].lifecycle.run(world[1].session), counts)
                    assert world[0].trace == world[1].trace
                    assert events(world[0].book.ledger) == events(world[1].book.ledger)
            finally:
                for w in world:
                    w.book.ledger.close()
    refusing = set(MODES) - {"normal", "no_dividends", "otm", "extrinsic_equal", "extrinsic_high"}
    for mode in refusing:
        assert counts[mode + ":raise"] == 5, (mode, counts)
        assert counts[mode + "_counterpart:ok"] == 5
    for mode in set(MODES) - refusing:
        assert counts[mode + ":ok"] == 5
    assert counts["refusal:LifecycleError"] > 0 and counts["refusal:StaleDataError"] > 0
    TALLIES["lifecycle"] = dict(counts)
    print("P4B lifecycle", json.dumps(dict(counts), sort_keys=True))


def test_sources_value_grid():
    counts = Counter()
    rng = random.Random(0x504B51)
    amounts = [D("1.00"), D("0"), D("-1"), D("NaN"), D("sNaN"), D("Infinity"),
               D("-0"), D("1E+50"), D("1E-50"), None, 1, 1.0, "1.00"]
    names = [" Aapl ", "", " \t", "ß", "世界", "\x1cAAPL\x1f", "🦊", "\ud800", None]
    instants = [CLOSE, CLOSE.replace(tzinfo=None), CLOSE.astimezone(timezone(timedelta(hours=5, minutes=30)))]
    for i in range(2200):
        amount, name, stamp, source = (rng.choice(amounts), rng.choice(names),
                                       rng.choice(instants), rng.choice(["known", "", None]))
        for kind in ("SettlementPrice", "Dividend"):
            args = ((name, EXPIRY, SettleTime.PM, amount, source, stamp) if kind == "SettlementPrice"
                    else (name, EXPIRY, amount, source, stamp))
            compare(kind, lambda: getattr(OS, kind)(*args), lambda: getattr(PS, kind)(*args), counts)
    for name in ("SettlementPrice", "Dividend"):
        assert counts[name + ":ok"] > 0 and counts[name + ":raise"] > 0
    for family, changed in [
        ("not_decimal", {"money": 1}), ("nonfinite", {"money": D("Infinity")}),
        ("nonpositive", {"money": D("0")}), ("source_empty", {"source": ""}),
        ("naive", {"stamp": CLOSE.replace(tzinfo=None)}),
    ]:
        for kind in ("SettlementPrice", "Dividend"):
            def args(overrides):
                values = dict(money=D("1.00"), source="known", stamp=CLOSE)
                values.update(overrides)
                if kind == "SettlementPrice":
                    return ("AAPL", EXPIRY, SettleTime.PM, values["money"], values["source"], values["stamp"])
                return ("AAPL", EXPIRY, values["money"], values["source"], values["stamp"])
            assert compare(kind + ":" + family, lambda: getattr(OS, kind)(*args(changed)),
                           lambda: getattr(PS, kind)(*args(changed)), counts)[0] == "raise"
            assert compare(kind + ":" + family + "_counterpart", lambda: getattr(OS, kind)(*args({})),
                           lambda: getattr(PS, kind)(*args({})), counts)[0] == "ok"
    assert compare("underlying_empty",
        lambda: OS.SettlementPrice("", EXPIRY, SettleTime.PM, D("1"), "known", CLOSE),
        lambda: PS.SettlementPrice("", EXPIRY, SettleTime.PM, D("1"), "known", CLOSE), counts)[0] == "raise"
    assert compare("underlying_empty_counterpart",
        lambda: OS.SettlementPrice("AAPL", EXPIRY, SettleTime.PM, D("1"), "known", CLOSE),
        lambda: PS.SettlementPrice("AAPL", EXPIRY, SettleTime.PM, D("1"), "known", CLOSE), counts)[0] == "ok"
    TALLIES["source_values"] = dict(counts)
    print("P4B source_values", json.dumps(dict(counts), sort_keys=True))


def execution(**kwargs):
    return JournalExecution(**dict(symbol=" aapl ", side=Side.BUY, quantity=D("2.00"),
        price=D("101.010"), fee=D("0.25"), executed_at=AFTER.replace(microsecond=123456),
        account_id="journal-é", asset_class="option", multiplier=100, stop_loss=D("98.00"),
        profit_target=D("110.00"), strategy_tag="策略 🦊", notes="naïve\n世界", **kwargs))


def test_journal_mapping_grid():
    counts = Counter()
    rng = random.Random(0x504B52)
    sinks = [m.HttpJournalSink("http://not-used", "journal-é", http_client=lambda *a: (500, {}))
             for m in (OJ, PJ)]
    for i in range(1200):
        stamp = rng.choice([AFTER, AFTER.replace(tzinfo=None),
                           AFTER.astimezone(timezone(timedelta(hours=-7)))])
        seq = rng.choice([-1001, -1, 0, 1, 999, 1000, 10**60, i])
        compare("stamp", lambda: OJ.journal_executed_at(stamp, seq),
                lambda: PJ.journal_executed_at(stamp, seq), counts)
        value = rng.choice([None, "", 123, "2026-10-16", "garbage", "2026-10-16T20:00:00Z",
                            "20261016T200000+0000", "2026-10-16T20:00:00+05:30",
                            "2026-W42-5T20:00:00.001+00:00", stamp.isoformat()])
        compare("parse", lambda: OJ._parse_instant(value), lambda: PJ._parse_instant(value), counts)
        trade = rng.choice([{}, {"tags": ["策略", 1, None, {"a": 1}]},
            {"tags": [], "tagsJson": '["ignored"]'}, {"tags": "wrong", "tagsJson": '["策略", null, true]'},
            {"tagsJson": "bad"}, {"tagsJson": "{}"}, {"tagsJson": "null"}, {"tagsJson": 123}])
        compare("tags", lambda: OJ._trade_tags(trade), lambda: PJ._trade_tags(trade), counts)
        payload = dict(symbol=" ß世界 ", side=rng.choice(["buy", Side.SELL, None, 5, "boGus"]),
                       quantity=rng.choice(["2.00", "0", "bad"]), price="101.010", fee="0.00",
                       executed_at=rng.choice([stamp, stamp.isoformat(), "bad"]),
                       account_id="journal-é", asset_class="option", multiplier=rng.choice([100, "bad"]),
                       stop_loss="98.00", profit_target=None, strategy_tag="策略 🦊", notes="unicode")
        if i % 4 == 0:
            payload.pop(rng.choice(list(payload)))
        compare("execution", lambda: sinks[0]._dict_to_execution(payload),
                lambda: sinks[1]._dict_to_execution(payload), counts)
    # Deterministic success and missing-field order, not dependent on random rarity.
    payload = dict(symbol="AAPL", side="BUY", quantity="1.00", price="2.00", fee="0",
                   executed_at=AFTER.isoformat(), account_id="journal-é", asset_class="option", multiplier=100)
    for key in payload:
        bad = {**payload, key: None}
        outcome = compare("missing:" + key, lambda: sinks[0]._dict_to_execution(bad),
                          lambda: sinks[1]._dict_to_execution(bad), counts)
        assert outcome[0] == "raise"
        assert key in outcome[2]
    assert compare("execution_success", lambda: sinks[0]._dict_to_execution(payload),
                   lambda: sinks[1]._dict_to_execution(payload), counts)[0] == "ok"
    for family, changed in [
        ("side_type", {"side": 5}), ("side_enum", {"side": "invalid"}),
        ("instant_invalid", {"executed_at": "bad"}),
        ("instant_naive", {"executed_at": "2026-10-16T20:00:00"}),
        ("decimal_invalid", {"quantity": "bad"}),
        ("quantity_zero", {"quantity": "0"}), ("price_zero", {"price": "0"}),
        ("multiplier_invalid", {"multiplier": "bad"}),
        ("account_empty", {"account_id": ""}), ("symbol_empty", {"symbol": ""}),
        ("asset_empty", {"asset_class": ""}),
    ]:
        bad = {**payload, **changed}
        assert compare("mapping:" + family, lambda: sinks[0]._dict_to_execution(bad),
                       lambda: sinks[1]._dict_to_execution(bad), counts)[0] == "raise"
        assert compare("mapping:" + family + "_counterpart", lambda: sinks[0]._dict_to_execution(payload),
                       lambda: sinks[1]._dict_to_execution(payload), counts)[0] == "ok"
    assert counts["execution:ok"] > 0 and counts["execution:raise"] > 0
    TALLIES["journal_mapping"] = dict(counts)
    print("P4B journal_mapping", json.dumps(dict(counts), sort_keys=True))


class JournalHttp:
    """No sockets; records every request and answers the original journal API."""
    def __init__(self, module, execution, seq, mode):
        self.calls = []
        self.mode = mode
        self.e, self.seq = execution, seq
        self.module = module
        self.multipliers = {"OTHER": 50}
        self.tags = []
        self.stop = self.target = None
        self.fill = None

    def __call__(self, url, method, body):
        self.calls.append((url, method, norm(body)))
        if self.mode == "auth":
            return 401, {"error": "password"}
        if self.mode == "network":
            raise OSError("offline")
        if url.endswith("/api/settings"):
            if self.mode == "settings_missing":
                return 200, {}
            if method == "PATCH":
                if self.mode == "settings_patch":
                    return 500, {}
                self.multipliers = body["multipliers"]
            return 200, {"multipliers": self.multipliers}
        if url.endswith("/api/executions"):
            if self.mode == "post_status":
                return 300, {}
            self.fill = body["executions"][0].copy()
            if self.mode == "skipped":
                return 200, {"inserted": 1, "skipped": 1}
            if self.mode == "not_stored":
                return 200, {}
            return 200, {"duplicates": 1} if self.mode == "duplicate" else {"inserted": 1}
        if "/api/trades?" in url:
            if self.mode == "list_status":
                return 500, {}
            # Open wins over newer closed, and candidate confirmation runs latest first.
            rows = [dict(key="wrong", symbol="MSFT", status="open"),
                    dict(key="old", symbol="AAPL", status="open", tags=["已有"]),
                    dict(key="new", symbol="AAPL", status="closed", tagsJson='["其他"]')]
            if self.mode == "list_missing":
                rows = []
            return 200, {"trades": rows}
        if "/api/trades/" in url:
            if method == "PATCH":
                if self.mode == "annotation_fail":
                    return 500, {}
                self.tags = body.get("tags", self.tags)
                self.stop = body.get("stopLoss", self.stop)
                self.target = body.get("profitTarget", self.target)
                return 200, {}
            if self.mode == "detail_status":
                return 500, {}
            fill = self.fill.copy() if self.fill else {}
            detail = dict(contractMultiplier=self.multipliers.get("AAPL"), stopLoss=self.stop,
                          profitTarget=self.target, tagsJson=json.dumps(self.tags))
            changes = {
                "quantity": ("quantity", 99), "price": ("price", 99),
                "side": ("side", "sell"), "fee": ("fee", 99),
                "asset": ("assetClass", "crypto"),
                "instant": ("executedAt", AFTER.isoformat()),
                "invalid_quantity": ("quantity", "bad"),
            }
            if self.mode in changes:
                key, value = changes[self.mode]
                fill[key] = value
            for mode, key in [("multiplier", "contractMultiplier"), ("stop", "stopLoss"), ("target", "profitTarget")]:
                if self.mode == mode:
                    detail[key] = None
            if self.mode == "tag":
                detail["tagsJson"] = "[]"
            if self.mode == "zero_fee_default":
                fill["fee"] = 999
            return 200, {"executions": [fill], "trade": detail}
        raise AssertionError((url, method))


JMODES = ("normal", "duplicate", "auth", "network", "settings_missing", "settings_patch",
          "post_status", "skipped", "not_stored", "list_status", "list_missing",
          "detail_status", "annotation_fail", "quantity", "price", "side", "fee",
          "asset", "instant", "invalid_quantity", "multiplier", "stop", "target", "tag",
          "zero_fee_default", "cross_account")


def test_journal_http_worlds():
    counts = Counter()
    for i, mode in enumerate(JMODES):
        for seed in range(8):
            e = execution()
            if mode == "zero_fee_default":
                e = replace(e, fee=D("0"))
            if mode == "cross_account":
                e = replace(e, account_id="wrong")
            pair = []
            for module in (OJ, PJ):
                http = JournalHttp(module, e, i * 1000 + seed, mode)
                sink = module.HttpJournalSink("http://not-used", "journal-é", http_client=http)
                pair.append((sink, http))
            value = compare(mode, lambda: pair[0][0].publish_execution(i * 1000 + seed, e),
                            lambda: pair[1][0].publish_execution(i * 1000 + seed, e), counts)
            assert pair[0][1].calls == pair[1][1].calls, (mode, pair[0][1].calls, pair[1][1].calls)
            if mode in ("normal", "duplicate", "zero_fee_default"):
                assert value == ("ok", True), (mode, value)
            elif mode == "auth":
                assert value[0] == "raise" and value[1] == "JournalAuthError"
            else:
                assert value == ("ok", False), (mode, value)
            for _, http in pair:
                http.mode = "normal"
                http.calls.clear()
            good = replace(e, account_id="journal-é")
            assert compare(mode + "_counterpart",
                lambda: pair[0][0].publish_execution(123, good),
                lambda: pair[1][0].publish_execution(123, good), counts) == ("ok", True)
            assert pair[0][1].calls == pair[1][1].calls, mode
            counts["delivery:" + ("success" if value == ("ok", True) else "refusal")] += 1
    assert counts["delivery:success"] == 24
    assert counts["delivery:refusal"] == 184
    TALLIES["journal_http"] = dict(counts)
    print("P4B journal_http", json.dumps(dict(counts), sort_keys=True))


def test_lifecycle_threshold_grid(tmp_path):
    """Direct carrier helpers pin the threshold, dividend tie and command suffix."""
    counts = Counter()
    rng = random.Random(0x504B53)
    book = Book(tmp_path)
    try:
        for i in range(1200):
            side = rng.choice([Side.BUY, Side.SELL])
            contract = option(rng.choice(["C", "P"]), "100")
            price = rng.choice(["99.990", "99.991", "100", "100.009", "100.010", "101.50"])
            for method in ("expiry", "early"):
                worlds = []
                for module, sources in ((O, OS), (P, PS)):
                    src = Sources(sources, [], D(price), rng.choice([D("-1.00"), D("0.00"), D("1.50"), D("2.00")]), D("0.50"))
                    worlds.append(module.LifecyclePass(book.ledger, SimpleNamespace(), CAL, src,
                                                      dividends=src, quotes=src))
                # Identical source values, independent sources.
                worlds[1]._quotes.bid = worlds[0]._quotes.bid
                fn = "_expiry" if method == "expiry" else "_early_assignment"
                compare(method, lambda: getattr(worlds[0], fn)("alpha", contract, side, D("2.00"), EXPIRY, AFTER),
                        lambda: getattr(worlds[1], fn)("alpha", contract, side, D("2.00"), EXPIRY, AFTER), counts)
        assert counts["expiry:ok"] == 1200
        assert counts["early:ok"] > 0 and counts["early:raise"] > 0
    finally:
        book.ledger.close()
    TALLIES["lifecycle_helpers"] = dict(counts)
    print("P4B lifecycle_helpers", json.dumps(dict(counts), sort_keys=True))


def test_journal_readback_boundaries():
    counts = Counter()
    baseline = execution()
    stamp = OJ.journal_executed_at(baseline.executed_at, 7)
    for name, changed, updates in [
        ("quantity_exact", {"quantity": D(".000001")}, {"quantity": 0}),
        ("quantity_below", {"quantity": D(".000000999")}, {"quantity": 0}),
        ("price_exact", {"price": D(".0001")}, {"price": 0}),
        ("price_below", {"price": D(".0000999")}, {"price": 0}),
        ("fee_exact", {"fee": D(".0001")}, {"fee": 0}),
        ("fee_above", {"fee": D(".0001001")}, {"fee": 0}),
        ("quantity_nan", {}, {"quantity": float("nan")}),
        ("fee_nan", {}, {"fee": float("nan")}),
        ("time_offset", {}, {"executedAt": stamp.replace("+00:00", "-00:00")}),
        ("bad_time", {}, {"executedAt": "bad"}),
        ("missing_time", {}, {"executedAt": None}),
        ("side_upper", {}, {"side": "BUY"}),
        ("bad_quantity", {}, {"quantity": {}}),
    ]:
        e = replace(baseline, **changed)
        fill = dict(quantity=float(e.quantity), price=float(e.price), fee=float(e.fee),
                    side="buy", executedAt=stamp, assetClass=e.asset_class)
        fill.update(updates)
        detail = dict(contractMultiplier=100, stopLoss=98, profitTarget=110,
                      tagsJson=json.dumps([e.strategy_tag]))
        for tags_mode, tags_update in [
            ("json", {}), ("list", {"tags": [e.strategy_tag]}),
            ("empty_list_wins", {"tags": []}), ("invalid_json", {"tagsJson": "bad"}),
            ("wrong_json_type", {"tagsJson": "{}"}),
        ]:
            trade = {**detail, **tags_update}
            def http(url, method, body):
                if "/api/trades?" in url:
                    return 200, {"trades": [dict(symbol="AAPL", key="clé/世界")]}
                return 200, {"executions": [fill], "trade": trade}
            sinks = [module.HttpJournalSink("http://not-used", "journal-é", http_client=http)
                     for module in (OJ, PJ)]
            compare(name + ":" + tags_mode, lambda: sinks[0].confirm_delivery(7, e),
                    lambda: sinks[1].confirm_delivery(7, e), counts)
    for stop in (None, D("0"), D("98.00")):
        for target in (None, D("110.00")):
            for tag in (None, "", "已有", "策略 🦊"):
                e = replace(baseline, stop_loss=stop, profit_target=target, strategy_tag=tag)
                worlds = []
                for module in (OJ, PJ):
                    calls = []
                    def http(url, method, body, calls=calls):
                        calls.append((url, method, norm(body)))
                        return 200, {"trades": [dict(key="closed", symbol="AAPL", status="closed"),
                            dict(key="open", symbol="AAPL", status="open", tags=["已有"]),
                            dict(key="latest", symbol="AAPL", status="closed")]}
                    worlds.append((module.HttpJournalSink("http://not-used", "journal-é", http_client=http), calls))
                compare("patch", lambda: worlds[0][0]._patch_trade_annotations(e),
                        lambda: worlds[1][0]._patch_trade_annotations(e), counts)
                assert worlds[0][1] == worlds[1][1]
    assert counts["quantity_exact:json:ok"] == 1
    TALLIES["journal_boundaries"] = dict(counts)
    print("P4B journal_boundaries", json.dumps(dict(counts), sort_keys=True))


def test_source_adapters():
    counts = Counter()
    for raw in (None, "bad", "-1", "0", "NaN", "sNaN", "Infinity", "1.50", 2, "1E+50"):
        traces = []
        adapters = []
        for module in (OS, PS):
            trace = []
            class Market:
                def corporate_actions(self, symbol, age, trace=trace):
                    trace.append(("corporate_actions", symbol, age))
                    return [SimpleNamespace(action_type="split", effective_date=EXPIRY, details={}, as_of=CLOSE),
                            SimpleNamespace(action_type="dividend", effective_date=EXPIRY,
                                            details={"amount": raw}, as_of=CLOSE)]
            adapters.append(module.CorporateActionDividends(Market(), 60.0))
            traces.append(trace)
        compare("corporate_amount", lambda: adapters[0].dividends("AAPL", EXPIRY),
                lambda: adapters[1].dividends("AAPL", EXPIRY), counts)
        assert traces[0] == traces[1]
    for max_age in (0.0, 60.0, float("inf")):
        for missing in (False, True, "stale"):
            adapters, traces = [], []
            contract = option("C", "100")
            for module in (OS, PS):
                trace = []
                class Store:
                    def latest(self, underlying, now, age, trace=trace):
                        trace.append(("latest", underlying, now.isoformat(), age))
                        if missing == "stale":
                            raise StaleDataError("snapshot exceeds max_age_seconds (I5)")
                        return SimpleNamespace(as_of=CLOSE, get=lambda c: None if missing else
                                               SimpleNamespace(as_of=CLOSE, bid=D("1.00")))
                adapters.append(module.SnapshotQuotes(Store(), max_age))
                traces.append(trace)
            compare("snapshot_quote", lambda: adapters[0].quote(contract, AFTER),
                    lambda: adapters[1].quote(contract, AFTER), counts)
            assert traces[0] == traces[1]
    assert counts["corporate_amount:ok"] == 3 and counts["corporate_amount:raise"] == 7
    assert counts["snapshot_quote:ok"] == 3 and counts["snapshot_quote:raise"] == 6
    TALLIES["source_adapters"] = dict(counts)
    print("P4B source_adapters", json.dumps(dict(counts), sort_keys=True))


def test_inherited_option_and_time_boundaries(tmp_path):
    counts = Counter()
    book = Book(tmp_path)
    prices = ("1E+50", "1E-50", "1E+2", "100.009", "100.010", "101.50",
              "79228162514264337593543950335", "12345678901234567890123456789")
    bids = (D("0.00"), D("1.50"), D("1E+50"), D("NaN"), D("Infinity"), 1.5, True)
    try:
        for price in prices:
            for bid in bids:
                for right in ("C", "P"):
                    c = option(right, "100", expiry=CAL.next_session(EXPIRY))
                    worlds = []
                    for module, sources in ((O, OS), (P, PS)):
                        src = Sources(sources, [], D(price), bid, D("0.50"))
                        worlds.append(module.LifecyclePass(book.ledger, SimpleNamespace(), CAL, src,
                                                          dividends=src, quotes=src))
                    compare("inherited_early",
                        lambda: worlds[0]._early_assignment("alpha", c, Side.SELL, D("1"), EXPIRY, AFTER),
                        lambda: worlds[1]._early_assignment("alpha", c, Side.SELL, D("1"), EXPIRY, AFTER), counts)
                    c = option(right, "100")
                    compare("inherited_expiry",
                        lambda: worlds[0]._expiry("alpha", c, Side.BUY, D("1"), EXPIRY, AFTER),
                        lambda: worlds[1]._expiry("alpha", c, Side.BUY, D("1"), EXPIRY, AFTER), counts)
        for at in (CLOSE.replace(tzinfo=None), CLOSE, AFTER):
            worlds = [module.LifecyclePass(book.ledger, SimpleNamespace(now_utc=lambda at=at: at),
                                          CAL, SimpleNamespace()) for module in (O, P)]
            outcome = compare("clock_boundary", lambda: worlds[0].run(EXPIRY),
                              lambda: worlds[1].run(EXPIRY), counts)
            if at.tzinfo is None:
                assert outcome == ("raise", "TypeError", "can't compare offset-naive and offset-aware datetimes")
            else:
                assert outcome[0] == "ok"
        for stamp, now, known in (
            (CLOSE.replace(tzinfo=None), AFTER, CLOSE),
            (CLOSE, AFTER.replace(tzinfo=None), CLOSE),
            (CLOSE, AFTER, CLOSE.replace(tzinfo=None)),
            (CLOSE, CLOSE, CLOSE),
            (AFTER + timedelta(microseconds=1), AFTER, CLOSE),
            (CLOSE - timedelta(microseconds=1), AFTER, CLOSE),
        ):
            worlds = []
            for module in (O, P):
                src = SimpleNamespace(settlement=lambda *a: SimpleNamespace(
                    underlying="AAPL", session=EXPIRY, settle_time=SettleTime.PM,
                    price=D("101.50"), source="known", as_of=stamp))
                worlds.append(module.LifecyclePass(book.ledger, SimpleNamespace(), CAL, src))
            compare("price_time_boundary",
                lambda: worlds[0]._price("AAPL", EXPIRY, SettleTime.PM, known, now),
                lambda: worlds[1]._price("AAPL", EXPIRY, SettleTime.PM, known, now), counts)
        assert counts["inherited_early:raise"] > 0 and counts["inherited_early:ok"] > 0
        assert counts["inherited_expiry:raise"] > 0 and counts["inherited_expiry:ok"] > 0
    finally:
        book.ledger.close()
    TALLIES["inherited_boundaries"] = dict(counts)
    print("P4B inherited_boundaries", json.dumps(dict(counts), sort_keys=True))

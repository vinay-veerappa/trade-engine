"""T5 exact lockstep on synthetic books; no live path or sink is reachable."""
from __future__ import annotations

from collections import Counter
from dataclasses import asdict, is_dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import random
import sqlite3

import pytest
import trade_engine_rs  # Mandatory, including all native outbox entry points.

from frozen_p4c.t5_store import Ledger as FrozenLedger, _outbox_items as frozen_items
from trade_engine.ledger.codec import canon, event_bytes
from trade_engine.ledger.events import CashFlow, Event, EventKind
from trade_engine.ledger.store import Ledger, _outbox_items
from test_p4c_embed import package as native_package, run as native_run, PLUGIN as PACKAGING_PLUGIN

AT = datetime(2026, 9, 24, 14, 30, 1, 987654, tzinfo=timezone(timedelta(hours=-4)))


def event(index, account="A"):
    return Event(account=account, kind=EventKind.CASH_FLOW,
                 payload=CashFlow(Decimal("1.2300"), "deposit", AT),
                 ts_utc=AT, command_id=f"c{index}")


def shape(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if is_dataclass(value):
        return {key: shape(item) for key, item in asdict(value).items()}
    if isinstance(value, dict):
        return {key: shape(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [shape(item) for item in value]
    return value


def outcome(action):
    try:
        return ("ok", shape(action()))
    except BaseException as error:
        cause = error.__cause__
        return ("refusal", type(error).__name__, str(error),
                None if cause is None else (type(cause).__name__, str(cause)))


def stored(ledger):
    return {
        "events": [event_bytes(ev) for ev in ledger.events()],
        "state": canon(ledger.fold()),
        "outbox": [tuple(row) for row in ledger.conn.execute("SELECT * FROM outbox ORDER BY id")],
        "meta": [tuple(row) for row in ledger.conn.execute("SELECT * FROM meta ORDER BY key")],
        "transaction": ledger.conn.in_transaction,
    }


@pytest.fixture
def pair(tmp_path):
    with FrozenLedger(tmp_path / "old.db") as old, Ledger(tmp_path / "new.db") as new:
        yield old, new


def compare(pair, action, tally=None, family="command"):
    old, new = pair
    a, b = outcome(lambda: action(old)), outcome(lambda: action(new))
    assert a == b
    assert stored(old) == stored(new)
    if tally is not None:
        tally[(family, a[0])] += 1
    return a


@pytest.mark.parametrize("seed", range(40))
def test_seeded_outbox_meta_lockstep(pair, seed):
    rng = random.Random(seed)
    tally = Counter()
    for index in range(24):
        dest = rng.choice(["journal", "audit", " \u03bb \U0001f600 "])
        payload = {"index": index, "text": "\u00e9\U0001f600", "scale": "1.2300", "nested": [None, True, 2]}
        account = rng.choice(["A", "B"])
        compare(pair, lambda lg: lg.append(event(index, account), outbox={dest: payload}), tally, "append")
        compare(pair, lambda lg: lg.append(event(index, account), outbox={dest: {"replay": True}}),
                tally, "replay")
        compare(pair, lambda lg: lg.enqueue_outbox(index + 1, dest, payload), tally, "duplicate")
        other = dest + "-other"
        compare(pair, lambda lg: lg.enqueue_outbox(index + 1, other, payload,
                created_at=AT + timedelta(microseconds=index)), tally, "enqueue")
        compare(pair, lambda lg: lg.mark_outbox_failed(index * 2 + 1, f"error-{index}"), tally, "fail")
        compare(pair, lambda lg: lg.pending_outbox(dest, include_failed=False), tally, "pending")
        compare(pair, lambda lg: lg.pending_outbox(None), tally, "all")
        compare(pair, lambda lg: lg.set_meta("key", str(index)), tally, "meta")
        compare(pair, lambda lg: lg.get_meta("key"), tally, "meta")
        compare(pair, lambda lg: lg.get_meta("absent"), tally, "meta")
        compare(pair, lambda lg: lg.snapshot(account, at_seq=index + 1), tally, "snapshot")
    assert tally[("duplicate", "refusal")] == 24
    assert tally[("enqueue", "ok")] == tally[("append", "ok")] == 24


@pytest.mark.parametrize("destination", ["", " ", None, 42, b"journal"])
def test_destination_and_unopened_validation_order(tmp_path, destination):
    old, new = FrozenLedger(tmp_path / "old.db"), Ledger(tmp_path / "new.db")
    assert outcome(lambda: old.enqueue_outbox(1, destination, {})) == outcome(
        lambda: new.enqueue_outbox(1, destination, {}))


@pytest.mark.parametrize("spec", [
    None, {}, [], {"sink": {}}, [(" sink ", {})], [("", {})], [(1, {})],
    [("sink", [])], [("sink", {}, 1)], [("sink",)], [42], [["a", {}]],
])
def test_outbox_spec_validation(spec):
    assert outcome(lambda: frozen_items(spec)) == outcome(lambda: _outbox_items(spec))


def test_enqueue_defaults_constraints_and_timestamp_refusals(pair):
    compare(pair, lambda lg: lg.append(event(0)))
    for seq, dest, payload, at in [
        (1, " sink ", {"unicode": "\U0001f600"}, None),
        (1, "sink", {}, AT),
        (999, "sink", {}, AT.replace(tzinfo=None)),
        (1, "naive", {}, AT.replace(tzinfo=None)),
        (1, "aware", {}, AT),
        (1, "bad-json", {"x": Decimal("1.0")}, AT),
        (1, "bad-id", {}, "not-time"),
        (Decimal("1"), "type", {}, None),
    ]:
        compare(pair, lambda lg: lg.enqueue_outbox(seq, dest, payload, created_at=at))
    assert pair[1].pending_outbox("sink")[0].created_at.isoformat() == event(0).ts_utc.isoformat()


class Clock:
    def __init__(self, trace, error=None):
        self.trace, self.error = trace, error
    def now_utc(self):
        self.trace.append("clock")
        if self.error:
            raise self.error
        return AT + timedelta(minutes=1)


@pytest.mark.parametrize("mode", ["yes", "no", "raises", "base", "clock-error", "clock-naive",
                                  "truthy-error", "nested"])
def test_drain_fifo_callback_order_and_failures(pair, mode):
    for i in range(3):
        compare(pair, lambda lg: lg.append(event(i), outbox={" sink ": {"i": i}}))
    traces = []
    results = []
    class Truth:
        def __bool__(self):
            raise ValueError("truth failure")
    for lg in pair:
        trace = []
        def sink(item):
            trace.append(("publish", shape(item)))
            if item.id == 2:
                if mode == "no":
                    return False
                if mode == "raises":
                    raise ConnectionError("sink broke")
                if mode == "base":
                    raise KeyboardInterrupt("stop")
                if mode == "truthy-error":
                    return Truth()
            if mode == "nested":
                # Reentrant reads and enqueue use the same connection, not a new writer.
                lg.state("A")
                if item.id == 1:
                    lg.enqueue_outbox(1, "sink", {"added": True}, created_at=AT)
            return True
        clock = Clock(trace, ValueError("clock broke") if mode == "clock-error" else None)
        if mode == "clock-naive":
            clock.now_utc = lambda: AT.replace(tzinfo=None)
        results.append(outcome(lambda: lg.drain_outbox(" sink ", sink, clock)))
        traces.append(trace)
    assert results[0] == results[1]
    assert traces[0] == traces[1]
    assert stored(pair[0]) == stored(pair[1])
    if mode not in ["base", "yes", "nested"]:
        assert results[1][1]["remaining_count"] > 0
        assert results[1][1]["error"]
    compare(pair, lambda lg: lg.drain_outbox("sink", lambda item: True, Clock([])))


@pytest.mark.parametrize("operation", ["enqueue", "delivered", "failed"])
@pytest.mark.parametrize("exception", [RuntimeError, sqlite3.IntegrityError, KeyboardInterrupt])
def test_transaction_commit_failure_and_baseexception(pair, operation, exception):
    compare(pair, lambda lg: lg.append(event(0), outbox={"sink": {}}))
    actions = {
        "enqueue": lambda lg: lg.enqueue_outbox(1, "extra", {}),
        "delivered": lambda lg: lg.mark_outbox_delivered(1, AT),
        "failed": lambda lg: lg.mark_outbox_failed(1, "bad"),
    }
    for lg in pair:
        def crash():
            raise exception("before commit")
        lg._commit = crash
    result = compare(pair, actions[operation])
    assert result[0] == "refusal"
    assert pair[1].conn.in_transaction == (exception is KeyboardInterrupt)
    for lg in pair:
        if lg.conn.in_transaction:
            lg._rollback()


def test_atomic_outbox_json_failure_and_replay(pair):
    ev = event(0)
    # json fails after the first insertion: neither the event nor its first outbox survives.
    compare(pair, lambda lg: lg.append(ev, outbox=[("one", {}), ("two", {"bad": Decimal("1")})]))
    assert pair[1].count() == 0 and pair[1].pending_outbox() == []
    compare(pair, lambda lg: lg.append(ev, outbox={"one": {}}))
    compare(pair, lambda lg: lg.append(ev, outbox={"two": {"bad": Decimal("1")}}))
    assert len(pair[1].pending_outbox()) == 1


@pytest.mark.parametrize("value", ["2", "  +03 ", "bad", "9999999999999999999999999999999999"])
def test_meta_schema_conversion(pair, value):
    compare(pair, lambda lg: lg.schema_version())
    compare(pair, lambda lg: lg.set_meta("schema_version", value))
    compare(pair, lambda lg: lg.schema_version())


@pytest.mark.parametrize("key,value", [(None, "x"), ("x", None), (Decimal(1), "x"), ("x", b"bytes")])
def test_meta_binding_and_constraints(pair, key, value):
    compare(pair, lambda lg: lg.set_meta(key, value))
    compare(pair, lambda lg: lg.get_meta(key))


def test_unknown_mark_ids_and_readonly_meta(pair):
    compare(pair, lambda lg: lg.mark_outbox_failed(999, "missing"))
    compare(pair, lambda lg: lg.mark_outbox_delivered(999, AT))
    for lg in pair:
        lg.conn.execute("PRAGMA query_only=ON")
    compare(pair, lambda lg: lg.set_meta("cannot", "write"))


@pytest.mark.parametrize("field,value", [("payload_json", "!"), ("status", "INVALID"),
                                      ("created_at", "bad"), ("delivered_at", "bad")])
def test_malformed_outbox_row_refusals(pair, field, value):
    compare(pair, lambda lg: lg.append(event(0), outbox={"sink": {}}))
    for lg in pair:
        lg.conn.execute(f"UPDATE outbox SET {field}=?", (value,))
    assert outcome(lambda: pair[0].pending_outbox()) == outcome(lambda: pair[1].pending_outbox())


def test_row_conversion_failure_order():
    row = dict(id=1, event_seq=1, destination="sink", payload_json="!",
               status="invalid", attempts="bad", created_at="bad", last_error=None, delivered_at=None)
    assert outcome(lambda: FrozenLedger._row_to_outbox(row)) == outcome(lambda: Ledger._row_to_outbox(row))


def test_metadata_transaction_visibility_and_reopen(pair):
    for lg in pair:
        lg.conn.execute("BEGIN IMMEDIATE")
        lg.set_meta("partial", "uncommitted")
    compare(pair, lambda lg: lg.get_meta("partial"))
    for lg in pair:
        lg._rollback()
    compare(pair, lambda lg: lg.get_meta("partial"))
    compare(pair, lambda lg: lg.append(event(0), outbox={"sink": {}}))
    compare(pair, lambda lg: lg.mark_outbox_failed(1, "retry"))
    for lg in pair:
        lg.close()
        lg.open()
    compare(pair, lambda lg: lg.pending_outbox())
    compare(pair, lambda lg: lg.drain_outbox("sink", lambda item: True, Clock([])))


def test_refusal_success_counterparts(pair):
    compare(pair, lambda lg: lg.append(event(0)))
    tally = Counter()
    cases = [
        ("destination", lambda lg: lg.enqueue_outbox(1, "", {}),
         lambda lg: lg.enqueue_outbox(1, "destination", {})),
        ("unknown_event", lambda lg: lg.enqueue_outbox(999, "unknown", {}),
         lambda lg: lg.enqueue_outbox(1, "unknown", {})),
        ("created_timezone", lambda lg: lg.enqueue_outbox(1, "created", {}, created_at=AT.replace(tzinfo=None)),
         lambda lg: lg.enqueue_outbox(1, "created", {}, created_at=AT)),
        ("json", lambda lg: lg.enqueue_outbox(1, "json", {"bad": Decimal("1")}),
         lambda lg: lg.enqueue_outbox(1, "json", {})),
        ("duplicate", lambda lg: lg.enqueue_outbox(1, "destination", {}),
         lambda lg: lg.enqueue_outbox(1, "different", {})),
        ("delivered_timezone", lambda lg: lg.mark_outbox_delivered(1, AT.replace(tzinfo=None)),
         lambda lg: lg.mark_outbox_delivered(1, AT)),
        ("binding", lambda lg: lg.set_meta(Decimal("1"), "bad"),
         lambda lg: lg.set_meta("binding", "good")),
        ("constraint", lambda lg: lg.set_meta("constraint", None),
         lambda lg: lg.set_meta("constraint", "good")),
    ]
    for family, refused, succeeds in cases:
        assert compare(pair, refused, tally, family)[0] == "refusal"
        assert compare(pair, succeeds, tally, family)[0] == "ok"
    assert all(tally[(family, kind)] == 1 for family, *_ in cases for kind in ["ok", "refusal"])
    print("T5_COUNTERPARTS", dict(tally))


def test_delivery_commit_failure_becomes_failed_and_retry_is_fifo(pair):
    compare(pair, lambda lg: lg.append(event(0), outbox={"sink": {}}))
    traces, results = [], []
    for lg in pair:
        trace = []
        original = lg._commit
        def commit_once():
            trace.append("commit")
            if len(trace) == 1:
                raise RuntimeError("delivery commit failed")
            original()
        lg._commit = commit_once
        results.append(outcome(lambda: lg.drain_outbox("sink", lambda item: True, Clock([]))))
        traces.append(trace)
        lg._commit = original
    assert results[0] == results[1]
    assert traces[0] == traces[1] == ["commit", "commit"]
    assert stored(pair[0]) == stored(pair[1])
    assert results[1][1]["error"] == "Sink sink raised: delivery commit failed"
    compare(pair, lambda lg: lg.drain_outbox("sink", lambda item: True, Clock([])))


def test_native_builtin_outbox_without_python_sqlite(native_package, tmp_path):
    cfg, _, _, plugins = native_package
    cfg["plugin_config"]["ledger"] = str(tmp_path / "builtin-outbox.db")
    (plugins / "fake_plugin.py").write_text(PACKAGING_PLUGIN + """
base_probe=probe
def probe(config):
    import sqlite3
    from datetime import datetime,timezone
    from decimal import Decimal
    from trade_engine.ledger.store import Ledger
    from trade_engine.ledger.reader import LedgerReader
    from trade_engine.ledger.events import Event,EventKind,CashFlow
    def forbidden(*args,**kwargs):
        raise AssertionError("Python SQLite open is forbidden")
    sqlite3.connect=forbidden
    stamp=datetime(2026,9,24,14,30,0,123456,tzinfo=timezone.utc)
    class Clock:
        def now_utc(self): return stamp
    event=Event(account="synthetic",kind=EventKind.CASH_FLOW,ts_utc=stamp,
                payload=CashFlow(Decimal("2.000"),"deposit",stamp),command_id="once")
    with Ledger(config["ledger"]) as ledger:
        ledger.append(event,outbox={"sink":{"decimal":"2.000"}})
        ledger.enqueue_outbox(1,"audit",{})
        ledger.set_meta("source","native")
        failed=ledger.drain_outbox("sink",lambda item:False,Clock())
        assert failed.failed_item.attempts==1 and failed.remaining_count==1
        delivered=ledger.drain_outbox("sink",lambda item:True,Clock())
        assert delivered.drained_count==1 and delivered.remaining_count==0
        with LedgerReader(ledger.path) as reader:
            assert reader.get_meta("source")=="native"
            assert len(reader.pending_outbox())==1
        result={"remaining":len(ledger.pending_outbox()),"schema":ledger.schema_version()}
    report=base_probe(config)
    report["outbox"]=result
    return report
""", encoding="utf-8")
    code, report = native_run(native_package)
    assert code == 0, report
    assert report["module_origin"] == "built-in"
    assert report["result"]["outbox"] == {"remaining": 1, "schema": 1}
    assert not any("trade_engine_rs" in path and path.endswith(".pyd") for path in report["result"]["loaded"])

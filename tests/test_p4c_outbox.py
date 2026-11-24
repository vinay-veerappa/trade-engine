"""Outbox and metadata behaviour of ``trade_engine.ledger.store.Ledger``, explicit values only.

Each test drives the production ledger through its public methods and asserts literal
outcomes: the ``("ok", value)`` payloads, the refusal type and message, and the number of
rows that survive a failed transaction. The native built-in outbox test at the end runs the
packaged executable with Python's SQLite forbidden.
"""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import random
import sqlite3

import pytest
import trade_engine_rs  # Mandatory, including all native outbox entry points.

from trade_engine.ledger.events import CashFlow, Event, EventKind
from trade_engine.ledger.store import Ledger, _outbox_items
from test_p4c_embed import package as native_package, run as native_run, PLUGIN as PACKAGING_PLUGIN

AT = datetime(2026, 9, 24, 14, 30, 1, 987654, tzinfo=timezone(timedelta(hours=-4)))
UTC_AT = "2026-09-24T18:30:01.987654+00:00"
DUPLICATE_CAUSE = "UNIQUE constraint failed: outbox.event_seq, outbox.destination"
DUPLICATE = ("ValueError", "Outbox entry violates constraint (e.g. duplicate or missing foreign key): "
             + DUPLICATE_CAUSE)


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


def drain_brief(result):
    """(drained, remaining, failed item id, error) for an ``ok`` drain outcome."""
    kind, value = result[0], result[1]
    if kind != "ok":
        return result
    failed = value["failed_item"]
    return (value["drained_count"], value["remaining_count"],
            None if failed is None else failed["id"], value["error"])


class Clock:
    def __init__(self, trace, error=None):
        self.trace, self.error = trace, error

    def now_utc(self):
        self.trace.append("clock")
        if self.error:
            raise self.error
        return AT + timedelta(minutes=1)


@pytest.fixture
def ledger(tmp_path):
    with Ledger(tmp_path / "book.db") as opened:
        yield opened


def test_seeded_outbox_meta_operations(tmp_path):
    # Seeded destinations, accounts and payloads; every outcome is pinned by an explicit expectation.
    for seed in range(40):
        rng = random.Random(seed)
        with Ledger(tmp_path / f"seed-{seed}.db") as lg:
            for index in range(24):
                dest = rng.choice(["journal", "audit", " λ \U0001f600 "])
                payload = {"index": index, "text": "é\U0001f600", "scale": "1.2300",
                           "nested": [None, True, 2]}
                account = rng.choice(["A", "B"])
                assert outcome(lambda: lg.append(event(index, account), outbox={dest: payload}))[0] == "ok"
                # Same command id: the stored event is returned, the new outbox payload is not used.
                assert outcome(lambda: lg.append(event(index, account),
                                                 outbox={dest: {"replay": True}}))[0] == "ok"
                assert outcome(lambda: lg.enqueue_outbox(index + 1, dest, payload)) == (
                    "refusal", *DUPLICATE, ("IntegrityError", DUPLICATE_CAUSE))
                other = dest + "-other"
                assert outcome(lambda: lg.enqueue_outbox(
                    index + 1, other, payload, created_at=AT + timedelta(microseconds=index)))[0] == "ok"
                assert outcome(lambda: lg.mark_outbox_failed(index * 2 + 1, f"error-{index}"))[0] == "ok"
                assert outcome(lambda: lg.pending_outbox(dest, include_failed=False))[0] == "ok"
                assert outcome(lambda: lg.pending_outbox(None))[0] == "ok"
                assert outcome(lambda: lg.set_meta("key", str(index))) == ("ok", None)
                assert outcome(lambda: lg.get_meta("key")) == ("ok", str(index))
                assert outcome(lambda: lg.get_meta("absent")) == ("ok", None)
                assert lg.snapshot(account, at_seq=index + 1).account_id == account


@pytest.mark.parametrize("destination,expected", [
    ("", ("refusal", "ValueError", "Outbox destination must be non-empty string", None)),
    (" ", ("refusal", "ValueError", "Outbox destination must be non-empty string", None)),
    (None, ("refusal", "ValueError", "Outbox destination must be a str, got NoneType", None)),
    (42, ("refusal", "ValueError", "Outbox destination must be a str, got int", None)),
    # The type check runs in Python before the Rust call, so it also precedes the not-open check.
    (b"journal", ("refusal", "ValueError", "Outbox destination must be a str, got bytes", None)),
])
def test_destination_and_unopened_validation_order(tmp_path, destination, expected):
    ledger = Ledger(tmp_path / "unopened.db")
    assert outcome(lambda: ledger.enqueue_outbox(1, destination, {})) == expected


@pytest.mark.parametrize("spec,expected", [
    (None, ("ok", [])),
    ({}, ("ok", [])),
    ([], ("ok", [])),
    ({"sink": {}}, ("ok", [["sink", {}]])),
    ([(" sink ", {})], ("ok", [[" sink ", {}]])),
    ([["a", {}]], ("ok", [["a", {}]])),
    ([("", {})], ("refusal", "ValueError", "Outbox destination must be non-empty string", None)),
    ([(1, {})], ("refusal", "ValueError", "Outbox destination must be non-empty string", None)),
    ([("sink", [])], ("refusal", "ValueError", "Outbox payload for 'sink' must be a dict", None)),
    ([("sink", {}, 1)], ("refusal", "ValueError", "too many values to unpack (expected 2)", None)),
    ([("sink",)], ("refusal", "ValueError", "not enough values to unpack (expected 2, got 1)", None)),
    ([42], ("refusal", "TypeError", "cannot unpack non-iterable int object", None)),
])
def test_outbox_spec_validation(spec, expected):
    assert outcome(lambda: _outbox_items(spec)) == expected


def test_enqueue_defaults_constraints_and_timestamp_refusals(ledger):
    assert outcome(lambda: ledger.append(event(0)))[0] == "ok"
    # The first enqueue normalises the destination and defaults created_at to the event time.
    assert outcome(lambda: ledger.enqueue_outbox(1, " sink ", {"unicode": "\U0001f600"}))[0] == "ok"
    refusals = [
        (lambda: ledger.enqueue_outbox(1, "sink", {}, created_at=AT),
         ("refusal", "ValueError", DUPLICATE[1])),
        (lambda: ledger.enqueue_outbox(999, "sink", {}, created_at=AT.replace(tzinfo=None)),
         ("refusal", "ValueError", "Unknown event sequence 999")),
        (lambda: ledger.enqueue_outbox(1, "naive", {}, created_at=AT.replace(tzinfo=None)),
         ("refusal", "ValueError", "Outbox created_at must be timezone-aware UTC datetime (I7)")),
        (lambda: ledger.enqueue_outbox(1, "bad-json", {"x": Decimal("1.0")}, created_at=AT),
         ("refusal", "TypeError", "Object of type Decimal is not JSON serializable")),
        (lambda: ledger.enqueue_outbox(1, "bad-id", {}, created_at="not-time"),
         ("refusal", "ValueError", "Outbox created_at must be a datetime or None, got str")),
        (lambda: ledger.enqueue_outbox(Decimal("1"), "type", {}),
         ("refusal", "ProgrammingError", "Error binding parameter 1: type 'decimal.Decimal' is not supported")),
    ]
    for action, expected in refusals:
        assert outcome(action)[:3] == expected
    # The timezone-aware enqueue with an explicit created_at is accepted and keeps its offset.
    assert outcome(lambda: ledger.enqueue_outbox(1, "aware", {}, created_at=AT))[0] == "ok"
    created = {item.destination: item.created_at.isoformat() for item in ledger.pending_outbox()}
    assert created == {"sink": UTC_AT, "aware": AT.isoformat()}


@pytest.mark.parametrize("mode,expected,trace", [
    ("yes", ("ok", (3, 0, None, None)),
     [("publish", 1), "clock", ("publish", 2), "clock", ("publish", 3), "clock"]),
    ("no", ("ok", (1, 2, 2, "Delivery unconfirmed by sink  sink ")),
     [("publish", 1), "clock", ("publish", 2)]),
    ("raises", ("ok", (1, 2, 2, "Sink  sink  raised: sink broke")),
     [("publish", 1), "clock", ("publish", 2)]),
    ("base", ("refusal", "KeyboardInterrupt", "stop", None),
     [("publish", 1), "clock", ("publish", 2)]),
    ("clock-error", ("ok", (0, 3, 1, "Sink  sink  raised: clock broke")),
     [("publish", 1), "clock"]),
    ("clock-naive", ("ok", (0, 3, 1, "Sink  sink  raised: delivered_at must be timezone-aware UTC datetime (I7)")),
     [("publish", 1)]),
    ("truthy-error", ("ok", (1, 2, 2, "Sink  sink  raised: truth failure")),
     [("publish", 1), "clock", ("publish", 2)]),
    ("nested", ("ok", (0, 3, 1, "Sink  sink  raised: " + DUPLICATE[1])),
     [("publish", 1)]),
])
def test_drain_fifo_callback_order_and_failures(tmp_path, mode, expected, trace):
    with Ledger(tmp_path / "drain.db") as lg:
        for i in range(3):
            lg.append(event(i), outbox={" sink ": {"i": i}})
        seen = []

        class Truth:
            def __bool__(self):
                raise ValueError("truth failure")

        def sink(item):
            seen.append(("publish", item.id))
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

        clock = Clock(seen, ValueError("clock broke") if mode == "clock-error" else None)
        if mode == "clock-naive":
            clock.now_utc = lambda: AT.replace(tzinfo=None)
        result = outcome(lambda: lg.drain_outbox(" sink ", sink, clock))
        brief = result if result[0] != "ok" else ("ok", drain_brief(result))
        assert brief == expected
        assert seen == trace
        # Whatever the sink did, the rows left behind are delivered by the next drain.
        assert drain_brief(outcome(lambda: lg.drain_outbox("sink", lambda item: True, Clock([])))) == (
            0 if mode == "yes" else (2 if mode in ("no", "raises", "base", "truthy-error") else 3), 0, None, None)


@pytest.mark.parametrize("operation,exception,expected,in_transaction", [
    ("enqueue", RuntimeError, ("refusal", "RuntimeError", "before commit", None), False),
    ("enqueue", sqlite3.IntegrityError, ("refusal", "ValueError",
     "Outbox entry violates constraint (e.g. duplicate or missing foreign key): before commit",
     ("IntegrityError", "before commit")), False),
    ("enqueue", KeyboardInterrupt, ("refusal", "KeyboardInterrupt", "before commit", None), True),
    ("delivered", RuntimeError, ("refusal", "RuntimeError", "before commit", None), False),
    ("delivered", sqlite3.IntegrityError, ("refusal", "IntegrityError", "before commit", None), False),
    ("delivered", KeyboardInterrupt, ("refusal", "KeyboardInterrupt", "before commit", None), True),
    ("failed", RuntimeError, ("refusal", "RuntimeError", "before commit", None), False),
    ("failed", sqlite3.IntegrityError, ("refusal", "IntegrityError", "before commit", None), False),
    ("failed", KeyboardInterrupt, ("refusal", "KeyboardInterrupt", "before commit", None), True),
])
def test_transaction_commit_failure_and_baseexception(tmp_path, operation, exception, expected, in_transaction):
    with Ledger(tmp_path / "tx.db") as lg:
        assert outcome(lambda: lg.append(event(0), outbox={"sink": {}}))[0] == "ok"

        def crash():
            raise exception("before commit")

        lg._commit = crash
        actions = {
            "enqueue": lambda: lg.enqueue_outbox(1, "extra", {}),
            "delivered": lambda: lg.mark_outbox_delivered(1, AT),
            "failed": lambda: lg.mark_outbox_failed(1, "bad"),
        }
        assert outcome(actions[operation]) == expected
        assert lg.conn.in_transaction is in_transaction
        if lg.conn.in_transaction:
            lg._rollback()


def test_atomic_outbox_json_failure_and_replay(ledger):
    ev = event(0)
    # JSON fails after the first insertion: neither the event nor its first outbox row survives.
    assert outcome(lambda: ledger.append(ev, outbox=[("one", {}), ("two", {"bad": Decimal("1")})])) == (
        "refusal", "TypeError", "Object of type Decimal is not JSON serializable", None)
    assert ledger.count() == 0
    assert ledger.pending_outbox() == []
    assert outcome(lambda: ledger.append(ev, outbox={"one": {}}))[0] == "ok"
    # A replay of the stored command returns the stored event without writing the new payload.
    assert outcome(lambda: ledger.append(ev, outbox={"two": {"bad": Decimal("1")}}))[0] == "ok"
    assert len(ledger.pending_outbox()) == 1


@pytest.mark.parametrize("value,expected", [
    ("2", ("ok", 2)),
    ("  +03 ", ("ok", 3)),
    ("bad", ("refusal", "ValueError", "invalid literal for int() with base 10: 'bad'", None)),
    ("9999999999999999999999999999999999", ("ok", 9999999999999999999999999999999999)),
])
def test_meta_schema_conversion(ledger, value, expected):
    assert outcome(lambda: ledger.schema_version()) == ("ok", 1)
    assert outcome(lambda: ledger.set_meta("schema_version", value))[0] == "ok"
    assert outcome(lambda: ledger.schema_version()) == expected


@pytest.mark.parametrize("key,value,set_expected,get_expected", [
    (None, "x", ("refusal", "ValueError", "meta key must be a str, got NoneType", None), ("ok", None)),
    ("x", None, ("refusal", "ValueError", "meta value must be a str, got NoneType", None), ("ok", None)),
    (Decimal(1), "x",
     ("refusal", "ValueError", "meta key must be a str, got Decimal", None),
     ("refusal", "ProgrammingError", "Error binding parameter 1: type 'decimal.Decimal' is not supported", None)),
    ("x", b"bytes", ("refusal", "ValueError", "meta value must be a str, got bytes", None), ("ok", None)),
])
def test_meta_refuses_non_str_key_or_value(ledger, key, value, set_expected, get_expected):
    # Refused before the write, so nothing is stored under either key.
    assert outcome(lambda: ledger.set_meta(key, value)) == set_expected
    assert outcome(lambda: ledger.get_meta(key)) == get_expected


def test_unknown_mark_ids_refuse_with_key_error_and_store_nothing(ledger):
    assert outcome(lambda: ledger.append(event(0), outbox={"sink": {}}))[0] == "ok"
    assert outcome(lambda: ledger.mark_outbox_failed(999, "missing")) == ("refusal", "KeyError", "999", None)
    assert outcome(lambda: ledger.mark_outbox_delivered(999, AT)) == ("refusal", "KeyError", "999", None)
    # The real row is untouched by the refused calls.
    pending = ledger.pending_outbox()
    assert [(item.status.value, item.last_error) for item in pending] == [("PENDING", None)]


def test_readonly_meta_write_is_refused_by_sqlite(ledger):
    ledger.conn.execute("PRAGMA query_only=ON")
    assert outcome(lambda: ledger.set_meta("cannot", "write")) == (
        "refusal", "OperationalError", "attempt to write a readonly database", None)


@pytest.mark.parametrize("field,value,message", [
    ("payload_json", "!", ("JSONDecodeError", "Expecting value: line 1 column 1 (char 0)")),
    ("status", "INVALID", ("ValueError", "'INVALID' is not a valid OutboxStatus")),
    ("created_at", "bad", ("ValueError", "Invalid isoformat string: 'bad'")),
    ("delivered_at", "bad", ("ValueError", "Invalid isoformat string: 'bad'")),
])
def test_malformed_outbox_row_refusals(ledger, field, value, message):
    assert outcome(lambda: ledger.append(event(0), outbox={"sink": {}}))[0] == "ok"
    ledger.conn.execute(f"UPDATE outbox SET {field}=?", (value,))
    assert outcome(lambda: ledger.pending_outbox()) == ("refusal", message[0], message[1], None)


def test_row_conversion_failure_order():
    row = dict(id=1, event_seq=1, destination="sink", payload_json="!",
               status="invalid", attempts="bad", created_at="bad", last_error=None, delivered_at=None)
    # The payload is parsed first, so its JSON error wins over the later status and time fields.
    assert outcome(lambda: Ledger._row_to_outbox(row)) == (
        "refusal", "JSONDecodeError", "Expecting value: line 1 column 1 (char 0)", None)


def test_metadata_transaction_visibility_and_reopen(tmp_path):
    with Ledger(tmp_path / "visibility.db") as lg:
        lg.conn.execute("BEGIN IMMEDIATE")
        lg.set_meta("partial", "uncommitted")
        # The writing connection sees its own uncommitted metadata.
        assert outcome(lambda: lg.get_meta("partial")) == ("ok", "uncommitted")
        lg._rollback()
        assert outcome(lambda: lg.get_meta("partial")) == ("ok", None)
        assert outcome(lambda: lg.append(event(0), outbox={"sink": {}}))[0] == "ok"
        assert outcome(lambda: lg.mark_outbox_failed(1, "retry"))[0] == "ok"
        lg.close()
        lg.open()
        pending = outcome(lambda: lg.pending_outbox())
        assert pending[0] == "ok" and len(pending[1]) == 1
        assert pending[1][0]["status"] == "FAILED" and pending[1][0]["last_error"] == "retry"
        assert drain_brief(outcome(lambda: lg.drain_outbox("sink", lambda item: True, Clock([])))) == (
            1, 0, None, None)


def test_refusal_success_counterparts(ledger):
    assert outcome(lambda: ledger.append(event(0)))[0] == "ok"
    cases = [
        ("destination", lambda: ledger.enqueue_outbox(1, "", {}),
         ("refusal", "ValueError", "Outbox destination must be non-empty string"),
         lambda: ledger.enqueue_outbox(1, "destination", {})),
        ("unknown_event", lambda: ledger.enqueue_outbox(999, "unknown", {}),
         ("refusal", "ValueError", "Unknown event sequence 999"),
         lambda: ledger.enqueue_outbox(1, "unknown", {})),
        ("created_timezone", lambda: ledger.enqueue_outbox(1, "created", {}, created_at=AT.replace(tzinfo=None)),
         ("refusal", "ValueError", "Outbox created_at must be timezone-aware UTC datetime (I7)"),
         lambda: ledger.enqueue_outbox(1, "created", {}, created_at=AT)),
        ("json", lambda: ledger.enqueue_outbox(1, "json", {"bad": Decimal("1")}),
         ("refusal", "TypeError", "Object of type Decimal is not JSON serializable"),
         lambda: ledger.enqueue_outbox(1, "json", {})),
        ("duplicate", lambda: ledger.enqueue_outbox(1, "destination", {}),
         ("refusal", *DUPLICATE),
         lambda: ledger.enqueue_outbox(1, "different", {})),
        ("delivered_timezone", lambda: ledger.mark_outbox_delivered(1, AT.replace(tzinfo=None)),
         ("refusal", "ValueError", "delivered_at must be timezone-aware UTC datetime (I7)"),
         lambda: ledger.mark_outbox_delivered(1, AT)),
        ("meta_key", lambda: ledger.set_meta(Decimal("1"), "bad"),
         ("refusal", "ValueError", "meta key must be a str, got Decimal"),
         lambda: ledger.set_meta("binding", "good")),
        ("meta_value", lambda: ledger.set_meta("constraint", None),
         ("refusal", "ValueError", "meta value must be a str, got NoneType"),
         lambda: ledger.set_meta("constraint", "good")),
    ]
    for family, refused, expected_refusal, succeeds in cases:
        result = outcome(refused)
        assert result[:3] == expected_refusal, family
        assert outcome(succeeds)[0] == "ok", family


def test_delivery_commit_failure_becomes_failed_and_retry_is_fifo(ledger):
    assert outcome(lambda: ledger.append(event(0), outbox={"sink": {}}))[0] == "ok"
    trace = []
    original = ledger._commit

    def commit_once():
        trace.append("commit")
        if len(trace) == 1:
            raise RuntimeError("delivery commit failed")
        original()

    ledger._commit = commit_once
    result = outcome(lambda: ledger.drain_outbox("sink", lambda item: True, Clock([])))
    ledger._commit = original
    # The failed delivery commit is recorded as a failed item, and the failure is committed too.
    assert trace == ["commit", "commit"]
    assert drain_brief(result) == (0, 1, 1, "Sink sink raised: delivery commit failed")
    assert drain_brief(outcome(lambda: ledger.drain_outbox("sink", lambda item: True, Clock([])))) == (
        1, 0, None, None)


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

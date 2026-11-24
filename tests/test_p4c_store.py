"""T4 native SQL/transaction behaviour of the ledger store, against explicit expected values.

Every file here is synthetic. Expected outcomes are literals: the ledger contract (I2-I5,
I12, I4 single instance) and the sqlite3 messages the native connection surfaces.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import sqlite3
import threading

import pytest
import trade_engine_rs as rs

from ledger_gen import random_stream, event_zoo
from test_p4c_embed import package as native_package, run as native_run, PLUGIN as PACKAGING_PLUGIN
from trade_engine.ledger import codec
from trade_engine.ledger.events import CashFlow, Event, EventKind
from trade_engine.ledger.lock import SingleInstanceLock
from trade_engine.ledger.reader import LedgerReader
from trade_engine.ledger.store import Ledger

T0 = datetime(2026, 9, 24, 14, 30, microsecond=123456,
              tzinfo=timezone(timedelta(hours=5, minutes=30)))

OVERFLOW = ("err", "OverflowError", "Python int too large to convert to SQLite INTEGER")
DECIMAL_BIND = ("err", "ProgrammingError", "Error binding parameter 1: type 'decimal.Decimal' is not supported")
NON_EMPTY = ("err", "ValueError", "command_id must be non-empty")
NOT_OPEN = ("err", "RuntimeError", "Ledger is not open; use `with Ledger(path):`")
CLOSED_DB = ("err", "ProgrammingError", "Cannot operate on a closed database.")
BYTES_TEXT = "'bytes' object cannot be converted to 'PyString'"


def cash(account="z-last", command=None, amount="1.000", seq=None):
    return Event(account=account, kind=EventKind.CASH_FLOW, ts_utc=T0, payload=CashFlow(Decimal(amount), "deposit", T0),
                 command_id=command, seq=seq)


def normalize(value):
    if isinstance(value, Event):
        return codec.event_bytes(value)
    if isinstance(value, (sqlite3.Row, rs.SqlRow)):
        return tuple(normalize(v) for v in value)
    if isinstance(value, (list, tuple)):
        return tuple(normalize(v) for v in value)
    if isinstance(value, dict):
        return tuple((k, normalize(v)) for k, v in value.items())
    if isinstance(value, (str, bytes, int, float, bool, type(None))):
        return value
    return codec.text(codec.canon(value))


def result(fn):
    """One call's outcome: ("ok", normalized value) or ("err", exception type, message)."""
    try:
        return "ok", normalize(fn())
    except BaseException as error:  # Classify, never fall back: a refusal is the expected value.
        return "err", type(error).__name__, str(error)


def seqs(fn):
    """The seq of each event fn returns, or the error outcome."""
    try:
        return tuple(event.seq for event in fn())
    except BaseException as error:
        return "err", type(error).__name__, str(error)


def test_native_artifacts_and_no_python_sqlite_writer(tmp_path, monkeypatch):
    assert rs.LedgerStore and rs.SqlConnection
    def forbidden(*args, **kwargs):
        raise AssertionError("native owners must not call Python sqlite3.connect")
    monkeypatch.setattr(sqlite3, "connect", forbidden)
    with Ledger(tmp_path / "native.db") as ledger:
        assert type(ledger.conn).__module__ == "trade_engine_rs"
        assert ledger._lock.held
        assert ledger.append(cash()).seq == 1
        with LedgerReader(ledger.path) as reader:
            assert reader.state("z-last").cash == Decimal("1.000")


def test_seeded_streams_refusals_and_reader_agreement(tmp_path):
    tally = Counter()
    for seed in range(100):
        with Ledger(tmp_path / str(seed) / "native.db") as ledger, LedgerReader(ledger.path) as reader:
            stream, _ = random_stream(seed)
            for event in stream:
                event = replace(event, seq=None)
                before = ledger.count()
                replay = event.command_id is not None and ledger.has_command(event.command_id)
                outcome = result(lambda: ledger.append(event))
                if outcome[0] == "ok":
                    tally["ok"] += 1
                    # A replayed command_id returns the original event and writes nothing (I3).
                    assert ledger.count() == before + (0 if replay else 1), (seed, event)
                else:
                    tally[outcome[:2]] += 1
                    assert ledger.count() == before, (seed, event, outcome)  # A refusal writes nothing (I2).
                assert result(lambda: reader.state(event.account)) == result(lambda: ledger.state(event.account)), (seed, event)
    assert tally == Counter({
        "ok": 1700,
        ("err", "LedgerFoldError"): 131,
        ("err", "LedgerDuplicateFillError"): 5,
        ("err", "IllegalOrderStateTransitionError"): 4,
    })


def test_schema_journal_pragmas_and_row_proxy(tmp_path):
    with Ledger(tmp_path / "native.db") as ledger:
        ledger.append(cash(command="deposit"))
        ledger.set_meta("unicode-🚀", "exact \0 text")
        # The journal mode is TRUNCATE, not WAL: the data policy denies file delete/rename, and a
        # journal that must be unlinked at commit fails with a disk I/O error.
        assert ledger.conn.execute("PRAGMA journal_mode").fetchone()[0] == "truncate"
        assert ledger.conn.execute("PRAGMA synchronous").fetchone()[0] == 2  # FULL (I2)
        assert ledger.conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert [tuple(r) for r in ledger.conn.execute(
            "SELECT type,name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY name")] == [
            ("table", "events"), ("index", "ix_events_account_seq"), ("index", "ix_outbox_dest_status_id"),
            ("table", "meta"), ("table", "outbox"), ("index", "ux_events_command_id")]
        row = ledger.conn.execute("SELECT * FROM events").fetchone()
        assert list(row.keys()) == ["seq", "ts_utc", "account", "kind", "command_id", "payload_json", "schema_version"]
        assert tuple(row)[:5] == (1, "2026-09-24T09:00:00.123456+00:00", "z-last", "CashFlow", "deposit")
        assert tuple(row)[6] == 1
        assert [tuple(r) for r in ledger.conn.execute("SELECT * FROM meta")] == [("unicode-🚀", "exact \0 text")]
        assert ledger.conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 0
        for key, expected in (
            (0, ("ok", 1)), (-1, ("ok", 1)),
            (slice(1, 4), ("ok", ("2026-09-24T09:00:00.123456+00:00", "z-last", "CashFlow"))),
            ("ACCOUNT", ("ok", "z-last")), ("account", ("ok", "z-last")),
            ("missing", ("err", "IndexError", "No item with that key")),
            (99, ("err", "IndexError", "tuple index out of range")),
            (None, ("err", "IndexError", "Index must be int or string")),
        ):
            assert result(lambda: row[key]) == expected, key
        assert Ledger._row_to_event(row) == cash(command="deposit", seq=1)
        cursor = ledger.conn.cursor()
        cursor.row_factory = None
        assert cursor.execute("SELECT seq, account FROM events").fetchmany(1) == [(1, "z-last")]
        assert cursor.fetchone() is None
        assert cursor.fetchall() == []
        cursor.close()
        assert result(cursor.fetchone) == ("err", "ProgrammingError", "Cannot operate on a closed cursor.")
        ledger.conn.row_factory = lambda cursor, row: (cursor.description[0][0], row)
        assert ledger.conn.execute("SELECT 4").fetchone() == ("4", (4,))


@pytest.mark.parametrize("parameters,expected", [
    ((None, "x", 3, 2.5, b"blob"), ("ok", ((None, "x", 3, 2.5, b"blob"),))),
    ((False, "🚀", -(2**63), float("nan"), bytearray(b"b")), ("ok", ((0, "🚀", -(2**63), None, b"b"),))),
    ({"a": None, "b": "é", "c": 2**63-1, "d": 1.2, "e": memoryview(b"view")},
     ("ok", ((None, "é", 2**63-1, 1.2, b"view"),))),
    ((1, 2), ("err", "ProgrammingError",
              "Incorrect number of bindings supplied. The current statement uses 5, and there are 2 supplied.")),
    ((2**65, "b", 1, 1., b"v"), OVERFLOW),
    ((Decimal("2"), "b", 1, 1., b"v"), DECIMAL_BIND),
    ({"a": 1}, ("err", "ProgrammingError", "You did not supply a value for binding parameter :b.")),
    (object(), ("err", "ProgrammingError", "parameters are of unsupported type")),
], ids=["plain", "bool-bigint-nan", "dict-types", "too-few", "overflow", "decimal", "dict-missing", "object"])
def test_sql_parameter_types_and_exact_refusals(tmp_path, parameters, expected):
    with Ledger(tmp_path / "native.db") as ledger:
        query = "SELECT :a AS A, :b AS B, :c AS C, :d AS D, :e AS E"
        assert result(lambda: ledger.conn.execute(query, parameters).fetchall()) == expected


@pytest.mark.parametrize("query,parameters,expected", [
    ("SELECT * FROM absent", (), ("err", "OperationalError", "no such table: absent")),
    ("SELECT ?", (), ("err", "ProgrammingError",
                      "Incorrect number of bindings supplied. The current statement uses 1, and there are 0 supplied.")),
    ("SELECT ?; SELECT ?", (1, 2), ("err", "ProgrammingError", "You can only execute one statement at a time.")),
    ("SELECT 1; -- comment\n/* ok */", (), ("ok", ((1,),))),
    ("SELECT 'unterminated", (), ("err", "OperationalError", 'unrecognized token: "\'unterminated"')),
    ("SELECT \0", (), ("err", "ProgrammingError", "the query contains a null character")),
    ("INSERT INTO outbox(event_seq,destination,payload_json,status,created_at) VALUES(999,'x','{}','PENDING','now')",
     (), ("err", "IntegrityError", "FOREIGN KEY constraint failed")),
])
def test_sql_errors_are_exact_and_write_nothing(tmp_path, query, parameters, expected):
    with Ledger(tmp_path / "native.db") as ledger:
        assert result(lambda: ledger.conn.execute(query, parameters).fetchall()) == expected


def test_script_transaction_and_cursor_rowcount(tmp_path):
    with Ledger(tmp_path / "native.db") as ledger:
        cursor = ledger.conn.executescript("BEGIN; INSERT INTO meta VALUES('first','one'); COMMIT;")
        assert cursor.description is None and cursor.lastrowid is None
        with ledger.conn:
            ledger.conn.execute("BEGIN IMMEDIATE")
            ledger.conn.execute("INSERT INTO meta VALUES(:key,:value)", {"key": "second", "value": "two"})
        assert not ledger.conn.in_transaction
        assert [tuple(row) for row in ledger.conn.execute("SELECT * FROM meta ORDER BY key")] == [
            ("first", "one"), ("second", "two")]
        cursor = ledger.conn.execute("UPDATE meta SET value=? WHERE key=?", ("new", "second"))
        assert cursor.rowcount == 1
        assert [tuple(row) for row in ledger.conn.execute("SELECT * FROM meta ORDER BY key")] == [
            ("first", "one"), ("second", "new")]


def test_event_queries_order_and_refusals(tmp_path):
    with Ledger(tmp_path / "native.db") as ledger:
        ledger.extend([cash("z-last", "z1"), cash("a-first", "a1"), cash("z-last", "z2", "-0.00")])
        assert ledger.accounts() == ["z-last", "a-first"]
        handle = ledger.fold_handle("z-last")
        assert handle is ledger.fold_handle("a-first")
        assert handle is ledger.fold_handle("z-last")
        for after, expected in (
            (None, (1, 2, 3)), (-1, (1, 2, 3)), (0, (1, 2, 3)), (1, (2, 3)), (2, (3,)),
            (3, ()), (99, ()), (1.5, (2, 3)), ("1", (2, 3)),
        ):
            assert seqs(lambda a=after: ledger.events(after=a)) == expected, after
        assert seqs(lambda: ledger.events(after=2**100)) == OVERFLOW
        assert seqs(lambda: ledger.events(after=Decimal(2))) == DECIMAL_BIND
        for account, expected in ((None, (1, 2, 3)), ("a-first", (2,)), ("z-last", (1, 3)),
                                  ("none", ()), (9, ()), (b"z-last", ())):
            assert seqs(lambda a=account: ledger.events(account=a)) == expected, account
        assert ledger.event_by_command("z1").seq == 1
        assert ledger.event_by_command("none") is None and ledger.has_command("none") is False
        assert ledger.event_by_command("") is None
        assert result(lambda: ledger.has_command("")) == NON_EMPTY
        assert ledger.event_by_command(None) is None
        assert result(lambda: ledger.has_command(None)) == NON_EMPTY
        assert ledger.event_by_command(0) is None
        assert result(lambda: ledger.has_command(0)) == NON_EMPTY
        assert ledger.event_by_command(1) is None and ledger.has_command(1) is False
        assert result(lambda: ledger.event_by_command(2**100)) == OVERFLOW
        assert result(lambda: ledger.has_command(2**100)) == OVERFLOW
        assert result(lambda: ledger.event_by_command(Decimal(2))) == DECIMAL_BIND
        assert result(lambda: ledger.has_command(Decimal(2))) == DECIMAL_BIND


def test_snapshot_fold_and_incremental_cache_are_the_log_fold(tmp_path):
    with Ledger(tmp_path / "native.db") as ledger:
        ledger.extend([cash("z-last", "z1"), cash("a-first", "a1"), cash("z-last", "z2", "-0.00")])
        for at_seq, last_seq, cash_amount in (
            (None, 3, "1"), (-1, 0, "0"), (0, 0, "0"), (1, 1, "1"), (2, 1, "1"),
            (3, 3, "1"), (100, 3, "1"), (2**100, 3, "1"), (1.5, 1, "1"),
        ):
            state = ledger.snapshot("z-last", at_seq=at_seq)
            assert (state.last_seq, state.cash) == (last_seq, Decimal(cash_amount)), at_seq
        assert result(lambda: ledger.snapshot("z-last", at_seq="1")) == (
            "err", "TypeError", "'<=' not supported between instances of 'int' and 'str'")
        folded = ledger.fold()
        assert {a: (s.last_seq, s.cash) for a, s in folded.items()} == {
            "z-last": (3, Decimal("1")), "a-first": (2, Decimal("1"))}
        assert folded["z-last"] == ledger.state("z-last")
        assert folded["a-first"] == ledger.state("a-first")
        for cutoff in range(5):
            cache = ledger.incremental_cache(after=cutoff, accounts=["z-last", "a-first", "missing"])
            assert cache.base_seq == cutoff
            assert cache.states() == folded  # FoldCache equals fold(events) (the stated contract)
            assert cache.verify(ledger.events()) is None
        replay = ledger.append(cash("z-last", "z1", "999999"), outbox={"ignored": {"value": 1}})
        assert replay.seq == 1 and replay.payload.amount == Decimal("1")  # Original returned; no write (I3).
        assert ledger.count() == 3
        assert ledger.pending_outbox() == []


@pytest.mark.parametrize("mode,outcome,in_transaction,reader_count,own_count", [
    ("exception", ("err", "RuntimeError", "synthetic crash"), False, 1, 1),
    ("base_exception", ("err", "KeyboardInterrupt", "synthetic interrupt"), True, 1, 2),
    ("post_commit", ("err", "OperationalError", "cannot rollback - no transaction is active"), False, 2, 2),
])
def test_commit_seam_atomic_outbox_and_failure_cache(tmp_path, mode, outcome, in_transaction, reader_count, own_count):
    with Ledger(tmp_path / "native.db") as ledger, LedgerReader(ledger.path) as reader:
        ledger.append(cash(command="seed"))
        ledger.state("z-last")
        saved = ledger._commit

        def crash():
            if mode == "post_commit":
                ledger.conn.execute("COMMIT")
            if mode == "base_exception":
                raise KeyboardInterrupt("synthetic interrupt")
            raise RuntimeError("synthetic crash")
        ledger._commit = crash
        event = cash(command="interrupted", amount="3.010")
        assert result(lambda: ledger.append(event, outbox={"sink": {"amount": "3.010"}})) == outcome
        assert ledger.conn.in_transaction == in_transaction
        assert ledger.count() == own_count  # The writer's own connection sees its open transaction.
        assert [e.seq for e in reader.events()] == list(range(1, reader_count + 1))  # Readers see committed rows only.
        if mode == "base_exception":
            ledger._rollback()
        ledger._commit = saved
        assert ledger.count() == (2 if mode == "post_commit" else 1)
        assert len(ledger.pending_outbox()) == (1 if mode == "post_commit" else 0)
        retry = ledger.append(event, outbox={"sink": {"amount": "3.010"}})
        assert retry.seq == 2
        assert ledger.count() == 2
        assert len(ledger.pending_outbox()) == 1


def test_batch_fold_refusal_and_outbox_constraint_rollback(tmp_path):
    with Ledger(tmp_path / "native.db") as ledger:
        orphan = replace(next(e for e in event_zoo() if e.kind == EventKind.FILL), seq=None)
        ledger.append(cash(command="seed"))
        ledger.state("z-last")
        batch = [cash(command="batch-1", amount="100.000"), orphan]
        assert result(lambda: ledger.extend(batch)) == (
            "err", "LedgerFoldError", "Fill f1 references unknown order 'o1' (I5)")
        assert ledger.count() == 1 and not ledger.has_command("batch-1")
        spec = [("sink", {"one": 1}), (" sink ", {"two": 2})]
        assert result(lambda: ledger.append(cash(command="duplicate-sink"), outbox=spec)) == (
            "err", "IntegrityError", "UNIQUE constraint failed: outbox.event_seq, outbox.destination")
        assert ledger.count() == 1 and not ledger.has_command("duplicate-sink")
        assert ledger.append(cash(command="batch-1"), outbox={" sink ": {"one": 1}}).seq == 2
        assert len(ledger.pending_outbox("sink")) == 1
        ledger.set_meta("key", "first")
        ledger.set_meta("key", "second")
        assert ledger.get_meta("key") == "second"


# field, value, the refusal every read/write action returns, the action that succeeds (if any),
# the reopened append's refusal, and the accounts the store then lists (or its refusal: a blob account is
# not an account name).
MALFORMED = [
    pytest.param("payload_json", "not json", ("err", "JSONDecodeError", "Expecting value"), None,
                 ("err", "JSONDecodeError", "Expecting value"), ["z-last"], id="payload-not-json"),
    pytest.param("payload_json", "{}", ("err", "PayloadCodecError", "Unrecognised encoded object with keys []"), None,
                 ("err", "JSONDecodeError", "Expecting value"), ["z-last"], id="payload-empty-object"),
    pytest.param("ts_utc", "not a timestamp", ("err", "ValueError", 'Invalid isoformat string: "not a timestamp"'), None,
                 ("err", "JSONDecodeError", "Expecting value"), ["z-last"], id="ts-not-timestamp"),
    pytest.param("ts_utc", sqlite3.Binary(b"blob-time"),
                 ("err", "TypeError", "argument 'ts_utc': " + BYTES_TEXT), None,
                 ("err", "TypeError", "argument 'rows': " + BYTES_TEXT), ["z-last"], id="ts-blob"),
    pytest.param("schema_version", 99,
                 ("err", "EventPayloadError",
                  "Event.schema_version 99 is newer than this engine understands (1); refusing rather than mis-reading it (I5)"),
                 None, ("err", "JSONDecodeError", "Expecting value"), ["z-last"], id="schema-newer"),
    pytest.param("schema_version", "not-int",
                 ("err", "TypeError", "argument 'schema_version': 'str' object cannot be interpreted as an integer"), None,
                 ("err", "TypeError", "argument 'rows': 'str' object cannot be interpreted as an integer"), ["z-last"],
                 id="schema-text"),
    pytest.param("schema_version", 1.5,
                 ("err", "TypeError", "argument 'schema_version': 'float' object cannot be interpreted as an integer"), None,
                 ("err", "TypeError", "argument 'rows': 'float' object cannot be interpreted as an integer"), ["z-last"],
                 id="schema-float"),
    pytest.param("kind", "UnknownKind", ("err", "ValueError", "'UnknownKind' is not a valid EventKind"),
                 "events_of_kind", ("err", "JSONDecodeError", "Expecting value"), ["z-last"], id="kind-unknown"),
    pytest.param("kind", sqlite3.Binary(b"blob-kind"),
                 ("err", "TypeError", "argument 'kind': " + BYTES_TEXT), "events_of_kind",
                 ("err", "TypeError", "argument 'rows': " + BYTES_TEXT), ["z-last"], id="kind-blob"),
    pytest.param("account", sqlite3.Binary(b"blob-account"),
                 ("err", "TypeError", "argument 'account': " + BYTES_TEXT), "snapshot",
                 ("err", "JSONDecodeError", "Expecting value"), ("err", "TypeError", "account must be str"),
                 id="account-blob"),
]


@pytest.mark.parametrize("field,value,refusal,succeeds,reopened,accounts", MALFORMED)
def test_malformed_history_refuses_with_exact_errors(tmp_path, field, value, refusal, succeeds, reopened, accounts):
    with Ledger(tmp_path / "native.db") as ledger:
        ledger.append(cash(command="good"))
        ledger.append(cash(command="bad"))
        ledger.conn.execute(f"UPDATE events SET {field}=? WHERE seq=2", (value,))
        actions = {
            "append": lambda: ledger.append(cash(command="bad", amount="999")),
            "events": ledger.events,
            "events_of_kind": lambda: ledger.events_of_kind(EventKind.CASH_FLOW),
            "snapshot": lambda: ledger.snapshot("z-last", at_seq=1),
            "fold": ledger.fold,
        }
        for name, call in actions.items():
            if name != succeeds:
                assert result(call) == refusal, name
            elif name == "events_of_kind":
                assert [e.seq for e in call()] == [1]  # Filtered in SQL: the malformed row is never decoded.
            else:
                state = call()
                assert (state.last_seq, state.cash) == (1, Decimal("1"))
        assert (result(ledger.accounts) if isinstance(accounts, tuple) else ledger.accounts()) == accounts
        assert ledger.has_command("bad")  # has_command does not decode the row.
        ledger.close()
        ledger.open()
        ledger.conn.execute("UPDATE events SET account='z-last' WHERE seq=2")
        ledger.conn.execute("UPDATE events SET payload_json='not json' WHERE seq=2")
        invalid = cash(command="new")
        assert result(lambda: ledger.append(invalid)) == reopened
        assert ledger.count() == 2
        ledger.conn.execute("DELETE FROM events WHERE seq=2")
        assert ledger.append(invalid).seq == 3
        assert ledger.count() == 2


def test_unopened_ledger_and_sequence_refusals_precede_database_access(tmp_path):
    ledger = Ledger(tmp_path / "never-opened.db")
    assert result(lambda: ledger.append(cash())) == NOT_OPEN
    assert result(lambda: ledger.append(cash(seq=1))) == (
        "err", "ValueError", "Event.seq is assigned by the ledger; pass seq=None to append")
    assert result(lambda: ledger.has_command("")) == NON_EMPTY
    assert result(lambda: ledger.has_command(None)) == NON_EMPTY
    assert result(lambda: ledger.has_command(0)) == NON_EMPTY
    assert result(lambda: ledger.has_command("valid")) == NOT_OPEN
    assert not (tmp_path / "never-opened.db").exists()


def test_native_lock_precedes_database_open_and_releases_after_open_failure(tmp_path):
    path = tmp_path / "missing.db"
    with SingleInstanceLock(path):
        outcome = result(Ledger(path).open)
        assert outcome[:2] == ("err", "LedgerLockError")
        assert outcome[2].startswith("Another process already holds the ledger lock at ")
        assert not path.exists()
    with Ledger(path) as ledger:
        assert ledger._lock.held
    # A directory cannot be opened as SQLite. Its failed open must release its guard.
    directory = tmp_path / "directory.db"
    directory.mkdir()
    failed = Ledger(directory)
    assert result(failed.open) == ("err", "OperationalError", "unable to open database file")
    with SingleInstanceLock(directory):
        assert not failed._lock.held


def test_reader_refresh_reopen_and_readonly_refusals(tmp_path):
    with Ledger(tmp_path / "native.db") as writer, LedgerReader(writer.path) as reader:
        writer.append(cash(command="seed"))
        assert reader.state("z-last").cash == Decimal("1")
        writer.conn.execute("BEGIN IMMEDIATE")
        writer.conn.execute("INSERT INTO meta VALUES('private','uncommitted')")
        writer.conn.execute("INSERT INTO events(ts_utc,account,kind,payload_json,schema_version) "
                            "SELECT ts_utc,account,kind,payload_json,schema_version FROM events WHERE seq=1")
        assert reader.count() == 1  # Uncommitted rows are invisible to the reader.
        assert reader.state("z-last").cash == Decimal("1")
        assert reader.get_meta("private") is None
        writer.conn.execute("ROLLBACK")
        # Raw reader SQL is truly SQLite read-only, not a shim refusing by policy alone.
        assert result(lambda: reader.conn.execute("INSERT INTO meta VALUES('x','y')")) == (
            "err", "OperationalError", "attempt to write a readonly database")
        writer.append(cash(command="next", amount="2.00"))
        assert reader.state("z-last").cash == Decimal("3")
        reader.close()
        reader.open()
        assert reader.state("z-last").cash == Decimal("3")
        assert result(lambda: reader.append(cash())) == (
            "err", "LedgerReadOnlyError",
            f"{reader.path} is open read-only; its writer is another process (I4)")


def test_multiple_concurrent_readers_see_only_committed_native_rows(tmp_path):
    path = tmp_path / "native.db"
    with Ledger(path) as ledger:
        ledger.append(cash(command="initial"))
        start = threading.Barrier(4)
        done = threading.Barrier(4)
        outputs = []

        def read():
            with LedgerReader(path) as reader:
                start.wait()
                outputs.append((reader.count(), str(reader.state("z-last").cash), reader.get_meta("pending")))
                done.wait()
                start.wait()
                outputs.append((reader.count(), str(reader.state("z-last").cash), reader.get_meta("pending")))
                done.wait()
        threads = [threading.Thread(target=read) for _ in range(3)]
        for thread in threads:
            thread.start()
        ledger.conn.execute("BEGIN IMMEDIATE")
        ledger.conn.execute("INSERT INTO meta VALUES('pending','yes')")
        ledger.conn.execute("INSERT INTO events(ts_utc,account,kind,payload_json,schema_version) "
                            "SELECT ts_utc,account,kind,payload_json,schema_version FROM events WHERE seq=1")
        start.wait()
        done.wait()
        ledger.conn.execute("COMMIT")
        start.wait()
        done.wait()
        for thread in threads:
            thread.join(timeout=10)
            assert not thread.is_alive()
        assert Counter(outputs) == Counter({(1, "1", None): 3, (2, "2", "yes"): 3})


def test_native_builtin_event_store_without_loaded_extension(native_package, tmp_path):
    cfg, _, _, plugins = native_package
    cfg["plugin_config"]["ledger"] = str(tmp_path / "builtin-synthetic.db")
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
        raise AssertionError("Python SQLite writer is forbidden")
    sqlite3.connect=forbidden
    stamp=datetime(2026,9,24,14,30,tzinfo=timezone.utc)
    event=Event(account="synthetic",kind=EventKind.CASH_FLOW,ts_utc=stamp,
                payload=CashFlow(Decimal("2.000"),"deposit",stamp),command_id="once")
    with Ledger(config["ledger"]) as ledger:
        first=ledger.append(event,outbox={"sink":{"decimal":"2.000"}})
        assert ledger.append(event)==first
        assert ledger.fold_handle("synthetic") is ledger.fold_handle("synthetic")
        assert len(ledger.pending_outbox())==1
        ledger.set_meta("source","native")
        with LedgerReader(ledger.path) as reader:
            assert reader.count()==1
            assert reader.get_meta("source")=="native"
            assert reader.state("synthetic").cash==Decimal("2.000")
        result={"seq":first.seq,"cash":str(ledger.state("synthetic").cash),
                "connection":type(ledger.conn).__module__}
    report=base_probe(config)
    report["store"]=result
    return report
""", encoding="utf-8")
    code, report = native_run(native_package)
    assert code == 0, report
    assert report["module_origin"] == "built-in"
    assert report["result"]["store"] == {"seq": 1, "cash": "2", "connection": "trade_engine_rs"}
    assert not any("trade_engine_rs" in p and p.endswith(".pyd") for p in report["result"]["loaded"])


def test_malformed_later_decode_precedes_full_fold_but_not_cached_load(tmp_path):
    orphan = replace(next(e for e in event_zoo() if e.kind == EventKind.FILL), seq=None)
    with Ledger(tmp_path / "native.db") as ledger:
        ledger.conn.execute(
            "INSERT INTO events(ts_utc,account,kind,payload_json,schema_version) VALUES(?,?,?,?,?)",
            (orphan.ts_utc.isoformat(), orphan.account, orphan.kind.value, codec.payload_text(orphan.payload), 1))
        ledger.conn.execute(
            "INSERT INTO events(ts_utc,account,kind,payload_json,schema_version) VALUES(?,?,?,?,?)",
            (orphan.ts_utc.isoformat(), orphan.account, "CashFlow", "not json", 1))
        assert result(ledger.fold) == ("err", "JSONDecodeError", "Expecting value")
        assert result(lambda: ledger.snapshot(orphan.account, at_seq=0)) == ("err", "JSONDecodeError", "Expecting value")
        # The cached load folds row 1 first and refuses the orphan fill before it reaches row 2.
        assert result(lambda: ledger.state(orphan.account)) == (
            "err", "LedgerFoldError", "Fill f1 references unknown order 'o1' (I5)")


def test_reader_failure_cursor_invalidation_and_success_after_fix(tmp_path):
    with Ledger(tmp_path / "native.db") as writer, LedgerReader(writer.path) as reader:
        writer.append(cash(command="seed"))
        assert reader.state("z-last").cash == Decimal("1")
        writer.conn.execute(
            "INSERT INTO events(ts_utc,account,kind,payload_json,schema_version) VALUES(?,?,?,?,?)",
            (T0.isoformat(), "z-last", "CashFlow", "not json", 1))
        assert result(lambda: reader.state("z-last")) == ("err", "JSONDecodeError", "Expecting value")
        writer.conn.execute("DELETE FROM events WHERE seq=2")
        writer.append(cash(command="fixed", amount="2.000"))
        assert reader.state("z-last").cash == Decimal("3.000")


def test_incremental_fold_load_apply_rows_apply_row_and_clear(tmp_path):
    from trade_engine.ledger.state import IncrementalFold, fold

    def row(event, seq):
        return (event.kind.value, codec.payload_text(event.payload), event.ts_utc.isoformat(),
                event.command_id, event.schema_version, seq)

    first = cash(command="a", amount="1.000")
    second = cash(command="b", amount="2.000")
    third = cash(command="c", amount="3.000")
    expected = fold([replace(first, seq=1), replace(second, seq=2), replace(third, seq=3)])["z-last"]
    incremental = IncrementalFold(atomic=True)
    incremental.load("z-last", [row(first, 1)])
    incremental.apply_rows("z-last", [row(second, 2)])
    incremental.apply_row("z-last", *row(third, 3)[:6])
    assert incremental.state("z-last") == expected
    assert incremental.state("z-last").cash == Decimal("6.000")
    incremental.clear()
    assert incremental.accounts() == [] and not incremental.has("z-last")
    # The bare fold refuses an unknown account; FoldCache.state is the layer that answers with an empty state.
    assert result(lambda: incremental.state("z-last")) == ("err", "KeyError", "'z-last'")


def test_notifications_and_handles_after_close(tmp_path):
    ledger = Ledger(tmp_path / "native.db").open()
    calls = []
    ledger.add_listener(lambda event: calls.append((codec.event_bytes(event), ledger.conn.in_transaction, ledger.count())))
    ledger.extend([cash(command="first"), cash(command="second")])
    ledger.append(cash(command="first"))  # A replay notifies nothing.
    conn = ledger.conn
    cursor = ledger.conn.execute("SELECT * FROM events")
    assert [(in_transaction, count) for _, in_transaction, count in calls] == [(False, 2), (False, 2)]
    ledger.close()
    assert result(lambda: conn.execute("SELECT 1")) == CLOSED_DB
    assert result(conn.commit) == CLOSED_DB
    assert result(cursor.fetchall) == CLOSED_DB

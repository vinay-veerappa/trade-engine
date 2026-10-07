"""T4 native SQL/transaction ownership against immutable T3-based Python owners.

Every file here is synthetic. Refusals compare type AND message; the frozen
oracles use frozen SQL owners/lock and the unchanged P2b codec/fold.
"""
from __future__ import annotations

from collections import Counter
from contextlib import ExitStack
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import sqlite3
import threading

import pytest
import trade_engine_rs as rs

from frozen_p4c.store import Ledger as OldLedger
from frozen_p4c.reader import LedgerReader as OldReader
from frozen_p4c.lock import SingleInstanceLock as OldLock
from ledger_gen import random_stream, event_zoo
from test_p4c_embed import package as native_package, run as native_run, PLUGIN as PACKAGING_PLUGIN
from trade_engine.ledger import codec
from trade_engine.ledger.events import CashFlow, Event, EventKind
from trade_engine.ledger.reader import LedgerReader
from trade_engine.ledger.store import Ledger

T0 = datetime(2026, 9, 24, 14, 30, microsecond=123456,
              tzinfo=timezone(timedelta(hours=5, minutes=30)))


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
    try:
        return "ok", normalize(fn())
    except BaseException as error:  # Oracle classification, never production fallback.
        return "err", type(error).__name__, str(error)


def pair(tmp_path):
    stack = ExitStack()
    old = stack.enter_context(OldLedger(tmp_path / "oracle.db"))
    new = stack.enter_context(Ledger(tmp_path / "native.db"))
    return stack, old, new


def rows(ledger, table):
    return [tuple(row) for row in ledger.conn.execute(f"SELECT * FROM {table} ORDER BY 1")]


def same(old, new):
    for table in ("events", "outbox", "meta"):
        assert rows(old, table) == rows(new, table), table
    assert old.count() == new.count()
    assert old.next_seq() == new.next_seq()
    assert old.accounts() == new.accounts()
    assert result(old.events) == result(new.events)
    for account in old.accounts() + ["never-seen"]:
        assert result(lambda: old.state(account)) == result(lambda: new.state(account)), account


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


def test_seeded_frozen_lockstep_every_prefix(tmp_path):
    tally = Counter()
    for seed in range(100):
        stack, old, new = pair(tmp_path / str(seed))
        with stack, OldReader(old.path) as old_reader, LedgerReader(new.path) as new_reader:
            stream, _ = random_stream(seed)
            for event in stream:
                event = replace(event, seq=None)
                expected = result(lambda: old.append(event))
                actual = result(lambda: new.append(event))
                assert actual == expected, (seed, event, expected, actual)
                tally[expected[0]] += 1
                same(old, new)
                assert result(lambda: old_reader.state(event.account)) == result(lambda: new_reader.state(event.account))
    assert tally["ok"] > 1000 and tally["err"] > 50, tally
    print("T4_PREFIX_COUNTERS", dict(tally))


def test_schema_pragmas_and_native_proxy_rows(tmp_path):
    stack, old, new = pair(tmp_path)
    with stack:
        for ledger in (old, new):
            ledger.append(cash(command="deposit"))
            ledger.set_meta("unicode-🚀", "exact \0 text")
        # The journal mode is no longer cross-checked: the frozen oracle
        # opens WAL while the native store journals in TRUNCATE (the data\
        # policy denies file delete/rename; a journal that must be unlinked
        # at commit fails with disk I/O error). The pragma is asserted
        # directly instead.
        assert old.conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert new.conn.execute("PRAGMA journal_mode").fetchone()[0] == "truncate"
        for query in (
            "SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY name",
            "PRAGMA synchronous", "PRAGMA foreign_keys",
            "SELECT * FROM events", "SELECT * FROM meta", "SELECT * FROM outbox",
        ):
            def inspect(ledger):
                cursor = ledger.conn.execute(query)
                found = cursor.fetchall()
                return cursor.description, [r.keys() for r in found], [tuple(r) for r in found]
            assert result(lambda: inspect(old)) == result(lambda: inspect(new)), query
        old_row = old.conn.execute("SELECT * FROM events").fetchone()
        new_row = new.conn.execute("SELECT * FROM events").fetchone()
        assert old_row.keys() == new_row.keys()
        for key in (0, -1, slice(1, 4), "ACCOUNT", "account", "missing", 99, None):
            assert result(lambda: old_row[key]) == result(lambda: new_row[key]), key
        assert Ledger._row_to_event(new_row) == OldLedger._row_to_event(old_row)
        for ledger in (old, new):
            cursor = ledger.conn.cursor()
            cursor.row_factory = None
            assert cursor.execute("SELECT seq, account FROM events").fetchmany(1) == [(1, "z-last")]
            assert cursor.fetchone() is None
            assert cursor.fetchall() == []
            cursor.close()
            assert result(cursor.fetchone)[:2] == ("err", "ProgrammingError")
            ledger.conn.row_factory = lambda cursor, row: (cursor.description[0][0], row)
        assert old.conn.execute("SELECT 4").fetchone() == new.conn.execute("SELECT 4").fetchone()


@pytest.mark.parametrize("parameters", [
    (None, "x", 3, 2.5, b"blob"),
    (False, "🚀", -(2**63), float("nan"), bytearray(b"b")),
    {"a": None, "b": "é", "c": 2**63-1, "d": 1.2, "e": memoryview(b"view")},
    (1, 2), (2**65, "b", 1, 1., b"v"),
    (Decimal("2"), "b", 1, 1., b"v"),
    {"a": 1}, object(),
])
def test_sql_parameter_types_and_exact_refusals(tmp_path, parameters):
    stack, old, new = pair(tmp_path)
    with stack:
        query = "SELECT :a AS A, :b AS B, :c AS C, :d AS D, :e AS E"
        assert result(lambda: old.conn.execute(query, parameters).fetchall()) == result(
            lambda: new.conn.execute(query, parameters).fetchall())


def test_sql_errors_scripts_transaction_and_cursor_iteration(tmp_path):
    stack, old, new = pair(tmp_path)
    with stack:
        for query, parameters in (
            ("SELECT * FROM absent", ()), ("SELECT ?", ()),
            ("SELECT ?; SELECT ?", (1,2)), ("SELECT 1; -- comment\n/* ok */", ()),
            ("SELECT 'unterminated", ()), ("SELECT \0", ()),
            ("INSERT INTO outbox(event_seq,destination,payload_json,status,created_at) VALUES(999,'x','{}','PENDING','now')", ()),
        ):
            assert result(lambda: old.conn.execute(query, parameters).fetchall()) == result(
                lambda: new.conn.execute(query, parameters).fetchall()), query
        for ledger in (old,new):
            cursor = ledger.conn.executescript("BEGIN; INSERT INTO meta VALUES('first','one'); COMMIT;")
            assert cursor.description is None and cursor.lastrowid is None
            with ledger.conn:
                ledger.conn.execute("BEGIN IMMEDIATE")
                ledger.conn.execute("INSERT INTO meta VALUES(:key,:value)", {"key":"second","value":"two"})
            assert not ledger.conn.in_transaction
            assert [tuple(row) for row in ledger.conn.execute("SELECT * FROM meta ORDER BY key")] == [
                ("first","one"), ("second","two")]
            cursor = ledger.conn.execute("UPDATE meta SET value=? WHERE key=?", ("new","second"))
            assert cursor.rowcount == 1
        same(old,new)


def test_ordering_replay_query_snapshot_seed_and_native_handle(tmp_path):
    stack, old, new = pair(tmp_path)
    with stack:
        for ledger in (old,new):
            ledger.extend([cash("z-last","z1"),cash("a-first","a1"),cash("z-last","z2", "-0.00")])
        same(old,new)
        assert new.accounts() == ["z-last","a-first"]
        handle = new.fold_handle("z-last")
        assert handle is new.fold_handle("a-first")
        assert handle is new.fold_handle("z-last")
        for after in (None,-1,0,1,2,3,99,1.5,"1",2**100,Decimal(2)):
            assert result(lambda: old.events(after=after)) == result(lambda: new.events(after=after)), after
        for account in (None, "a-first", "z-last", "none", 9, b"z-last"):
            assert result(lambda: old.events(account=account)) == result(lambda: new.events(account=account)), account
        for command in ("z1","none","",None,0,1,2**100,Decimal(2)):
            assert result(lambda: old.event_by_command(command)) == result(lambda: new.event_by_command(command)), command
            assert result(lambda: old.has_command(command)) == result(lambda: new.has_command(command)), command
        for seq in (None, -1, 0, 1, 2, 3, 100, 2**100, 1.5, "1"):
            assert result(lambda: old.snapshot("z-last",at_seq=seq)) == result(lambda: new.snapshot("z-last",at_seq=seq)), seq
        assert result(old.fold) == result(new.fold)
        for cutoff in range(5):
            a=old.incremental_cache(after=cutoff,accounts=["z-last","a-first","missing"])
            b=new.incremental_cache(after=cutoff,accounts=["z-last","a-first","missing"])
            assert a.base_seq == b.base_seq
            assert result(a.states) == result(b.states)
            assert result(lambda: a.verify(old.events())) == result(lambda: b.verify(new.events()))
        event = cash("z-last","z1","999999")
        assert result(lambda: old.append(event,outbox={"ignored":{"value":1}})) == result(
            lambda: new.append(event,outbox={"ignored":{"value":1}}))
        same(old,new)
        assert new.pending_outbox() == []


@pytest.mark.parametrize("mode", ["exception","base_exception","post_commit"])
def test_commit_seam_atomic_outbox_and_failure_cache(tmp_path, mode):
    stack, old, new = pair(tmp_path)
    with stack, OldReader(old.path) as a_reader, LedgerReader(new.path) as b_reader:
        for ledger in (old,new):
            ledger.append(cash(command="seed"))
            ledger.state("z-last")
        saved = (old._commit, new._commit)
        for ledger in (old,new):
            def crash(ledger=ledger):
                if mode == "post_commit":
                    ledger.conn.execute("COMMIT")
                if mode == "base_exception":
                    raise KeyboardInterrupt("synthetic interrupt")
                raise RuntimeError("synthetic crash")
            ledger._commit = crash
        event = cash(command="interrupted", amount="3.010")
        assert result(lambda: old.append(event,outbox={"sink":{"amount":"3.010"}})) == result(
            lambda: new.append(event,outbox={"sink":{"amount":"3.010"}}))
        assert old.conn.in_transaction == new.conn.in_transaction == (mode == "base_exception")
        assert result(a_reader.events) == result(b_reader.events)
        if mode == "base_exception":
            assert old.count() == new.count() == 2  # Own connection, not committed for readers.
            assert a_reader.count() == b_reader.count() == 1
            old._rollback(); new._rollback()
        old._commit,new._commit = saved
        same(old,new)
        assert result(lambda: old.append(event,outbox={"sink":{"amount":"3.010"}})) == result(
            lambda: new.append(event,outbox={"sink":{"amount":"3.010"}}))
        same(old,new)
        assert len(new.pending_outbox()) == 1


def test_batch_fold_refusal_and_outbox_constraint_rollback_counterparts(tmp_path):
    stack, old, new = pair(tmp_path)
    with stack:
        orphan = replace(next(e for e in event_zoo() if e.kind == EventKind.FILL),seq=None)
        for ledger in (old,new):
            ledger.append(cash(command="seed"))
            ledger.state("z-last")
        batch=[cash(command="batch-1",amount="100.000"),orphan]
        assert result(lambda: old.extend(batch)) == result(lambda: new.extend(batch))
        same(old,new)
        assert new.count() == 1 and not new.has_command("batch-1")
        event=cash(command="duplicate-sink")
        spec=[("sink",{"one":1}),(" sink ",{"two":2})]
        assert result(lambda: old.append(event,outbox=spec)) == result(lambda: new.append(event,outbox=spec))
        same(old,new)
        assert new.count() == 1 and not new.has_command("duplicate-sink")
        for ledger in (old,new):
            assert ledger.append(cash(command="batch-1"),outbox={" sink ":{"one":1}}).seq == 2
            assert len(ledger.pending_outbox("sink")) == 1
            ledger.set_meta("key","first"); ledger.set_meta("key","second")
            assert ledger.get_meta("key") == "second"
        same(old,new)


@pytest.mark.parametrize("field,value", [
    ("payload_json","not json"), ("payload_json","{}"),
    ("ts_utc","not a timestamp"), ("ts_utc",sqlite3.Binary(b"blob-time")),
    ("schema_version",99), ("schema_version","not-int"), ("schema_version",1.5),
    ("kind","UnknownKind"), ("kind",sqlite3.Binary(b"blob-kind")),
    ("account",sqlite3.Binary(b"blob-account")),
])
def test_malformed_history_replay_and_error_order(tmp_path, field, value):
    stack, old, new = pair(tmp_path)
    with stack:
        for ledger in (old,new):
            ledger.append(cash(command="good"))
            ledger.append(cash(command="bad"))
            ledger.conn.execute(f"UPDATE events SET {field}=? WHERE seq=2",(value,))
        for action in (
            lambda ledger: ledger.append(cash(command="bad",amount="999")),
            lambda ledger: ledger.events(),
            lambda ledger: ledger.events_of_kind(EventKind.CASH_FLOW),
            lambda ledger: ledger.snapshot("z-last",at_seq=1),
            lambda ledger: ledger.fold(),
        ):
            assert result(lambda: action(old)) == result(lambda: action(new)), (field,action)
        assert result(old.accounts) == result(new.accounts)
        assert old.has_command("bad") and new.has_command("bad")  # Does not decode.
        # Reopen to ensure malformed history precedes the new event's codec error.
        old.close(); new.close(); old.open(); new.open()
        invalid = cash(command="new")
        for ledger in (old,new):
            ledger.conn.execute("UPDATE events SET account='z-last' WHERE seq=2")
            ledger.conn.execute("UPDATE events SET payload_json='not json' WHERE seq=2")
        assert result(lambda: old.append(invalid)) == result(lambda: new.append(invalid))
        assert old.count() == new.count() == 2
        for ledger in (old,new):
            ledger.conn.execute("DELETE FROM events WHERE seq=2")
            assert ledger.append(invalid).seq == 3
        same(old,new)


def test_unopened_and_assigned_sequence_error_order(tmp_path):
    old,new = OldLedger(tmp_path/"old.db"), Ledger(tmp_path/"new.db")
    for event in (cash(),cash(seq=1)):
        assert result(lambda: old.append(event)) == result(lambda: new.append(event))
    for command in ("",None,0,"valid"):
        assert result(lambda: old.has_command(command)) == result(lambda: new.has_command(command))


def test_native_lock_precedes_database_open_and_releases_after_open_failure(tmp_path):
    path=tmp_path/"missing.db"
    with OldLock(path):
        failed=result(Ledger(path).open)
        assert failed[1] == "LedgerLockError"
        assert not path.exists()
    with Ledger(path) as ledger:
        assert ledger._lock.held
    # A directory cannot be opened as SQLite. Its failed open must release its guard.
    directory=tmp_path/"directory.db"; directory.mkdir()
    a=OldLedger(directory); b=Ledger(directory)
    assert result(a.open) == result(b.open)
    with OldLock(directory):
        assert not b._lock.held


def test_reader_refresh_reopen_and_readonly_refusal_counterparts(tmp_path):
    stack, old,new=pair(tmp_path)
    with stack, OldReader(old.path) as a, LedgerReader(new.path) as b:
        for ledger in (old,new):
            ledger.append(cash(command="seed"))
        assert result(lambda:a.state("z-last")) == result(lambda:b.state("z-last"))
        for ledger in (old,new):
            ledger.conn.execute("BEGIN IMMEDIATE")
            ledger.conn.execute("INSERT INTO meta VALUES('private','uncommitted')")
            ledger.conn.execute("INSERT INTO events(ts_utc,account,kind,payload_json,schema_version) "
                "SELECT ts_utc,account,kind,payload_json,schema_version FROM events WHERE seq=1")
        assert a.count() == b.count() == 1
        assert result(lambda:a.state("z-last")) == result(lambda:b.state("z-last"))
        assert a.get_meta("private") == b.get_meta("private") is None
        old.conn.execute("ROLLBACK"); new.conn.execute("ROLLBACK")
        # Raw reader SQL is truly SQLite read-only, not a shim refusing by policy alone.
        assert result(lambda:a.conn.execute("INSERT INTO meta VALUES('x','y')")) == result(
            lambda:b.conn.execute("INSERT INTO meta VALUES('x','y')"))
        for ledger in (old,new):
            ledger.append(cash(command="next",amount="2.00"))
        assert result(lambda:a.state("z-last")) == result(lambda:b.state("z-last"))
        a.close(); b.close(); a.open(); b.open()
        assert result(lambda:a.state("z-last")) == result(lambda:b.state("z-last"))
        for reader in (a,b):
            failure=result(lambda:reader.append(cash()))
            assert failure[0:2] == ("err","LedgerReadOnlyError")


def test_multiple_concurrent_readers_see_only_committed_native_rows(tmp_path):
    path=tmp_path/"native.db"
    with Ledger(path) as ledger:
        ledger.append(cash(command="initial"))
        start=threading.Barrier(4)
        done=threading.Barrier(4)
        outputs=[]
        def read():
            with LedgerReader(path) as reader:
                start.wait()
                outputs.append((reader.count(),str(reader.state("z-last").cash),reader.get_meta("pending")))
                done.wait()
                start.wait()
                outputs.append((reader.count(),str(reader.state("z-last").cash),reader.get_meta("pending")))
                done.wait()
        threads=[threading.Thread(target=read) for _ in range(3)]
        for thread in threads: thread.start()
        ledger.conn.execute("BEGIN IMMEDIATE")
        ledger.conn.execute("INSERT INTO meta VALUES('pending','yes')")
        ledger.conn.execute("INSERT INTO events(ts_utc,account,kind,payload_json,schema_version) "
            "SELECT ts_utc,account,kind,payload_json,schema_version FROM events WHERE seq=1")
        start.wait(); done.wait()
        ledger.conn.execute("COMMIT")
        start.wait(); done.wait()
        for thread in threads: thread.join(timeout=10); assert not thread.is_alive()
        assert Counter(outputs) == Counter({(1,"1.000",None):3,(2,"2.000","yes"):3})


def test_native_builtin_event_store_without_loaded_extension(native_package, tmp_path):
    cfg,_,_,plugins=native_package
    cfg["plugin_config"]["ledger"]=str(tmp_path/"builtin-synthetic.db")
    (plugins/"fake_plugin.py").write_text(PACKAGING_PLUGIN+"""
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
""",encoding="utf-8")
    code,report=native_run(native_package)
    assert code==0,report
    assert report["module_origin"]=="built-in"
    assert report["result"]["store"]=={"seq":1,"cash":"2.000","connection":"trade_engine_rs"}
    assert not any("trade_engine_rs" in p and p.endswith(".pyd") for p in report["result"]["loaded"])


def test_malformed_later_decode_precedes_full_fold_but_not_cached_load(tmp_path):
    stack,old,new=pair(tmp_path)
    orphan=replace(next(e for e in event_zoo() if e.kind==EventKind.FILL),seq=None)
    with stack:
        for ledger in (old,new):
            ledger.conn.execute(
                "INSERT INTO events(ts_utc,account,kind,payload_json,schema_version) VALUES(?,?,?,?,?)",
                (orphan.ts_utc.isoformat(),orphan.account,orphan.kind.value,codec.payload_text(orphan.payload),1))
            ledger.conn.execute(
                "INSERT INTO events(ts_utc,account,kind,payload_json,schema_version) VALUES(?,?,?,?,?)",
                (orphan.ts_utc.isoformat(),orphan.account,"CashFlow","not json",1))
        assert result(old.fold)==result(new.fold)
        assert result(lambda:old.snapshot(orphan.account,at_seq=0))==result(
            lambda:new.snapshot(orphan.account,at_seq=0))
        assert result(lambda:old.state(orphan.account))==result(lambda:new.state(orphan.account))
        assert result(old.fold)[1]!=result(lambda:old.state(orphan.account))[1]


def test_reader_failure_cursor_invalidation_and_success_counterpart(tmp_path):
    stack,old,new=pair(tmp_path)
    with stack,OldReader(old.path) as a,LedgerReader(new.path) as b:
        for ledger in (old,new):
            ledger.append(cash(command="seed"))
        assert result(lambda:a.state("z-last"))==result(lambda:b.state("z-last"))
        for ledger in (old,new):
            ledger.conn.execute(
                "INSERT INTO events(ts_utc,account,kind,payload_json,schema_version) VALUES(?,?,?,?,?)",
                (T0.isoformat(),"z-last","CashFlow","not json",1))
        refused_a=result(lambda:a.state("z-last"))
        refused_b=result(lambda:b.state("z-last"))
        assert refused_a==refused_b and refused_a[0]=="err"
        for ledger in (old,new):
            ledger.conn.execute("DELETE FROM events WHERE seq=2")
            ledger.append(cash(command="fixed",amount="2.000"))
        assert result(lambda:a.state("z-last"))==result(lambda:b.state("z-last"))
        assert b.state("z-last").cash==Decimal("3.000")


def test_notifications_and_compatibility_handles_after_close(tmp_path):
    stack,old,new=pair(tmp_path)
    with stack:
        calls=[[],[]]
        handles=[]
        for index,ledger in enumerate((old,new)):
            ledger.add_listener(lambda event,index=index,ledger=ledger:
                calls[index].append((codec.event_bytes(event),ledger.conn.in_transaction,ledger.count())))
            ledger.extend([cash(command="first"),cash(command="second")])
            ledger.append(cash(command="first"))
            handles.append((ledger.conn,ledger.conn.execute("SELECT * FROM events")))
        assert calls[0]==calls[1] and len(calls[0])==2
        assert all(not in_transaction and count==2 for _,in_transaction,count in calls[0])
        old.close();new.close()
        for action in (
            lambda conn,cursor:conn.execute("SELECT 1"),
            lambda conn,cursor:conn.commit(),
            lambda conn,cursor:cursor.fetchall(),
        ):
            assert result(lambda:action(*handles[0]))==result(lambda:action(*handles[1]))

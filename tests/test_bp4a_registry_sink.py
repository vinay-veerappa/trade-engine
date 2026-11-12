"""BP-4a: the engine's registry sink and outbox (BROKER_PLATFORM phase 4a, I12).

Fixtures only. Nothing here reads the live riskguard install, NT8, a broker or a live token:
the riskguard side is either an in-test double that follows the documented apply rules, or a
`riskguard-service` binary BUILT FROM SOURCE into a scratch target dir and spawned against a
TEMP data dir with a FIXTURE connector-token file (set `RG_SERVICE_EXE` to that binary; the
G2 tests skip without it). Account ids are fakes (`ACCT-TEST-nnn`).

Enforcing tests, by rule:
- default OFF (rollback = off): `test_default_*`, `test_an_off_sink_*`.
- delivery is confirmed by the ack body only: `test_a_bare_200_never_confirms`,
  `test_an_ack_ahead_of_the_head_halts`.
- strictly in order, stop at the first failure: `test_delivery_is_in_order_after_an_outage`,
  `test_a_failure_stops_the_drain_at_the_failed_envelope`.
- seq gaps are refused: `test_a_seq_gap_is_refused_and_the_sink_reseeds_from_a_snapshot`,
  `test_g2_riskguard_refuses_a_seq_gap_and_asks_for_a_snapshot`.
- the token never leaks: `test_token_never_appears_in_logs_exceptions_reprs_or_the_outbox_file`.
- freshness: `test_confirmed_at_is_the_polls_not_the_delivery_time`, `test_heartbeat_*`.
- budgets (B7, I12): `test_budget_24h_of_events_stays_under_the_50mb_cap_and_replays_in_order`,
  `test_budget_outbox_cap_fill_compacts_to_snapshot_plus_tail`,
  `test_budget_tail_event_cap_fill_compacts`, `test_budget_delivery_adds_under_50ms_*`.
- G2 with the real riskguard-service and the fake ntx: `test_g2_*`.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from trade_engine.accounts import _rs
from trade_engine.accounts.errors import AccountRuleError
from trade_engine.interfaces.broker import AccountRecord, AccountSnapshot, _iso_z
from trade_engine.sinks.riskguard_registry import (
    RegistrySinkConfig,
    RiskguardRegistrySink,
    TokenUnavailable,
    read_token,
)
from trade_engine.tradovate.adapter import TradovateAdapter

from tests.test_bp3_tradovate_ntx import (  # noqa: F401 - fixtures are collected from here
    FAKE_NAME,
    FakeClock,
    FakeNtx,
    IDENT_A,
    IDENT_B,
    acct,
    clock,
    daemon,
    make_client,
    snap,
)

FAKE_TOKEN = "FAKE-RG-CONNECTOR-TOKEN-0123456789abcdef"
T0 = datetime(2026, 11, 11, 18, 0, tzinfo=timezone.utc)


# --- the sink under test, against a riskguard double -----------------------------------


class FakeRg:
    """Follows the documented ingest rules: sort by seq, `<= applied` is a no-op, a first new
    seq above `applied + 1` is a 409 `need: snapshot` with nothing applied, and the answer
    carries `applied_seq`. `mode` scripts the failures the sink must survive."""

    def __init__(self, token: str = FAKE_TOKEN) -> None:
        self.token = token
        self.up = True
        self.mode = "ack"  # ack | bare200 | ahead | status:<n>
        self.applied = 0
        self.applied_seqs: list[int] = []
        self.snapshots: list[int] = []
        self.envelopes: list[dict] = []
        self.headers: list[dict] = []
        self.members: dict[str, dict] = {}

    def post(self, url, body, headers, timeout):
        if not self.up:
            return 0, ""
        self.headers.append(dict(headers))
        if headers.get("Authorization") != f"Bearer {self.token}":
            return 401, json.dumps({"error": "unauthorized", "echo": self.token})
        env = json.loads(body)
        self.envelopes.append(env)
        if self.mode == "bare200":
            return 200, "{}"
        if self.mode.startswith("status:"):
            code = int(self.mode.split(":")[1])
            return code, json.dumps({"kind": "bad_shape", "echo": self.token})
        snapshot = env.get("snapshot")
        base = self.applied
        if snapshot:
            base = snapshot["seq"]
        fresh = sorted((e for e in env.get("events", [])), key=lambda e: e["seq"])
        fresh = [e for e in fresh if e["seq"] > base]
        if fresh and fresh[0]["seq"] != base + 1:
            return 409, json.dumps({"need": "snapshot", "applied_seq": self.applied})
        if snapshot:
            self.snapshots.append(snapshot["seq"])
            self.members = {a["key"]: a for a in snapshot["accounts"]}
        for e in fresh:
            self.applied_seqs.append(e["seq"])
            if e["kind"] == "removed":
                self.members.pop(e["key"], None)
            elif e["kind"] == "added":
                self.members[e["key"]] = e["record"]
            elif e["kind"] == "changed":
                self.members[e["key"]].update(e.get("fields") or {})
        self.applied = fresh[-1]["seq"] if fresh else base
        answer = self.applied + (100 if self.mode == "ahead" else 0)
        return 200, json.dumps({"applied_seq": answer})


def token_file(tmp_path: Path, token: str = FAKE_TOKEN) -> Path:
    path = tmp_path / "tokens.json"
    path.write_text(json.dumps({"version": 1, "tokens": [{"token": token, "role": "connector"}]}))
    return path


def make_sink(tmp_path, rg: FakeRg | None, clk, **kw) -> RiskguardRegistrySink:
    cfg = dict(
        enabled=True,
        base_url="http://127.0.0.1:9",
        token_file=str(token_file(tmp_path)),
        outbox_path=str(tmp_path / "outbox.json"),
    )
    cfg.update(kw)
    return RiskguardRegistrySink(RegistrySinkConfig(**cfg), clock=clk, http_post=None if rg is None else rg.post)


def rec(n: int, *, status: str = "active", at: datetime = T0) -> AccountRecord:
    num = f"{n:03d}"
    return AccountRecord.of(
        "tradovate",
        f"ACCT-TEST-{num}",
        env="paper",
        source="ntx",
        confirmed_at=at,
        status=status,
        routes=[("nt8", f"ACCT-TEST-{num}"), ("tradingview", f"D900{num}")],
    )


def fleet(n: int, *, at: datetime = T0, status: dict[int, str] | None = None) -> AccountSnapshot:
    status = status or {}
    return AccountSnapshot(tuple(rec(i, status=status.get(i, "active"), at=at) for i in range(1, n + 1)), at)


def added(record: AccountRecord, seq: int, at: datetime = T0) -> dict:
    return {
        "kind": "added",
        "key": record.key,
        "display": record.display,
        "at": _iso_z(at),
        "seq": seq,
        "record": record.as_contract(),
        "changes": [],
    }


def changed(n: int, seq: int, old: str, new: str, at: datetime = T0) -> dict:
    record = rec(n)
    return {
        "kind": "changed",
        "key": record.key,
        "display": record.display,
        "at": _iso_z(at),
        "seq": seq,
        "record": None,
        "changes": [["status", old, new]],
    }


def seed(sink: RiskguardRegistrySink, n: int = 3, at: datetime = T0) -> int:
    """First poll: `n` accounts added, seqs 1..n."""
    snapshot = fleet(n, at=at)
    sink.observe(snapshot, [added(r, i + 1, at) for i, r in enumerate(snapshot.records)], n)
    return n


class Fleet:
    """Drives polls of a fleet whose accounts flip status, like a busy day."""

    def __init__(self, sink: RiskguardRegistrySink, n: int, clk: FakeClock) -> None:
        self.sink, self.n, self.clk = sink, n, clk
        self.seq = seed(sink, n, clk.now_utc())
        self.status = {i: "active" for i in range(1, n + 1)}

    def poll(self, flips: int) -> None:
        events = []
        for k in range(flips):
            i = (self.seq + k) % self.n + 1
            new = "closed" if self.status[i] == "active" else "active"
            events.append(changed(i, self.seq + 1 + k, self.status[i], new, self.clk.now_utc()))
            self.status[i] = new
        self.seq += flips
        self.sink.observe(fleet(self.n, at=self.clk.now_utc(), status=self.status), events, self.seq)


def stats(sink: RiskguardRegistrySink) -> dict:
    return sink.stats()["outbox"]


# =====================================================================================
# default OFF (rollback = off)
# =====================================================================================


def test_default_config_is_off_and_only_the_word_on_enables_it():
    assert RegistrySinkConfig().enabled is False
    assert RegistrySinkConfig.from_env({}).enabled is False
    for value in ("", "off", "0", "true", "1", "yes", "ON "):
        got = RegistrySinkConfig.from_env({"TE_RISKGUARD_REGISTRY_SINK": value}).enabled
        assert got is (value.strip().lower() == "on"), value
    assert RegistrySinkConfig.from_env({"TE_RISKGUARD_REGISTRY_SINK": "on"}).enabled is True


def test_an_off_sink_reads_no_token_opens_no_socket_and_writes_no_file(tmp_path):
    calls: list = []

    def post(*args):
        calls.append(args)
        return 200, "{}"

    out = tmp_path / "outbox.json"
    sink = RiskguardRegistrySink(
        RegistrySinkConfig(token_file=str(tmp_path / "no-such-token"), outbox_path=str(out)),
        clock=FakeClock(),
        http_post=post,
    )
    assert not sink.enabled
    seed(sink)
    assert sink.pump() == 0
    sink.start()
    sink.stop()
    assert calls == [] and not out.exists()
    assert sink.counters["observed"] == 0


def test_an_enabled_sink_without_its_paths_is_a_config_error():
    with pytest.raises(ValueError):
        RiskguardRegistrySink(RegistrySinkConfig(enabled=True), clock=FakeClock())


# =====================================================================================
# I12: confirmed by the ack body, in order, stop at the first failure
# =====================================================================================


def test_a_bare_200_never_confirms(tmp_path):
    clk, rg = FakeClock(T0), FakeRg()
    sink = make_sink(tmp_path, rg, clk)
    seed(sink)
    rg.mode = "bare200"
    assert sink.pump() == 0
    assert stats(sink)["acked_seq"] == 0 and stats(sink)["unacked_200"] >= 1
    assert sink.counters["ack_refused"] >= 1 and sink.counters["acks"] == 0
    rg.mode = "ack"
    assert sink.pump() == 1
    assert stats(sink)["acked_seq"] == 3


def test_an_ack_ahead_of_the_head_halts(tmp_path):
    clk, rg = FakeClock(T0), FakeRg()
    sink = make_sink(tmp_path, rg, clk)
    seed(sink)
    rg.mode = "ahead"
    sink.pump()
    assert sink.halted == "ack_ahead"
    posts = sink.counters["posts"]
    rg.mode = "ack"
    sink.pump()
    assert sink.counters["posts"] == posts  # halted: nothing more is sent
    sink.resume()
    assert sink.pump() >= 1 and sink.halted is None


def test_delivery_is_in_order_after_an_outage(tmp_path):
    clk, rg = FakeClock(T0), FakeRg()
    sink = make_sink(tmp_path, rg, clk)
    flt = Fleet(sink, 5, clk)
    assert sink.pump() == 1  # the seed snapshot
    rg.up = False
    for _ in range(40):
        clk.advance(30)
        flt.poll(3)
        sink.pump()
    assert stats(sink)["pending"] == 120 and rg.applied == 5
    rg.up = True
    while sink.pump():
        pass
    assert rg.applied == flt.seq
    assert rg.applied_seqs == sorted(rg.applied_seqs) and len(set(rg.applied_seqs)) == len(rg.applied_seqs)
    assert rg.applied_seqs[-120:] == list(range(6, flt.seq + 1))  # contiguous: nothing skipped
    assert {k: v["status"] for k, v in rg.members.items()} == {
        rec(i).key: s for i, s in flt.status.items()
    }


def test_a_failure_stops_the_drain_at_the_failed_envelope(tmp_path):
    clk, rg = FakeClock(T0), FakeRg()
    sink = make_sink(tmp_path, rg, clk)
    flt = Fleet(sink, 3, clk)
    sink.pump()
    for _ in range(3):
        clk.advance(30)
        flt.poll(1)
    sent: list[int] = []
    real = rg.post

    def flaky(url, body, headers, timeout):
        env = json.loads(body)
        sent.append(min((e["seq"] for e in env.get("events", [])), default=0))
        return (500, "") if len(sent) == 1 else real(url, body, headers, timeout)

    sink._post = flaky
    assert sink.pump() == 0  # the first failure ends the pump: no envelope jumps the queue
    assert len(sent) == 1 and rg.applied == 3
    assert sink.pump() == 1
    assert rg.applied_seqs[-3:] == [4, 5, 6]


def test_a_seq_gap_is_refused_and_the_sink_reseeds_from_a_snapshot(tmp_path):
    clk, rg = FakeClock(T0), FakeRg()
    sink = make_sink(tmp_path, rg, clk)
    flt = Fleet(sink, 3, clk)
    sink.pump()
    before = stats(sink)
    # The poller jumped ahead (a ledger replay): events 7.. with nothing at 4..6.
    snapshot = fleet(3, at=T0)
    sink.observe(snapshot, [changed(1, 7, "active", "closed")], 7)
    assert sink.counters["reseeds"] == 1
    assert stats(sink)["gaps_refused"] >= 1 and before["head_seq"] == 3
    sink.pump()
    assert rg.snapshots == [3, 7]  # re-seeded by a snapshot at the new head
    assert rg.applied_seqs == []  # event 7 never reached riskguard without its predecessors
    assert rg.applied == 7


def test_the_outbox_core_refuses_a_gap_and_changes_nothing():
    ob = _rs.rs.RegistryOutbox("tradovate", 120, 50 * 1024 * 1024, 100_000)
    snap = fleet(2)
    ob.observe(json.dumps(snap.as_contract()), json.dumps([added(r, i + 1) for i, r in enumerate(snap.records)]), 2, _iso_z(T0))
    head = ob.head_seq()
    with pytest.raises(ValueError) as err:
        ob.observe(json.dumps(snap.as_contract()), json.dumps([changed(1, 9, "active", "closed")]), 9, _iso_z(T0))
    assert err.value.args[0] == "seq_gap"
    assert ob.head_seq() == head


def test_a_riskguard_409_need_snapshot_resends_a_snapshot(tmp_path):
    clk, rg = FakeClock(T0), FakeRg()
    sink = make_sink(tmp_path, rg, clk)
    flt = Fleet(sink, 3, clk)
    sink.pump()
    clk.advance(30)
    flt.poll(2)
    rg.applied = 0  # riskguard lost its state
    rg.members = {}
    sink.pump()
    assert sink.counters["snapshots_requested"] >= 1
    assert rg.applied == flt.seq and len(rg.members) == 3


def test_a_halting_answer_stops_delivery_and_never_echoes_the_body(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    clk, rg = FakeClock(T0), FakeRg()
    sink = make_sink(tmp_path, rg, clk)
    seed(sink)
    rg.mode = "status:400"
    sink.pump()
    assert sink.halted is not None
    assert sink.counters["halts"] == 1
    posts = sink.counters["posts"]
    sink.pump()
    assert sink.counters["posts"] == posts
    assert FAKE_TOKEN not in caplog.text


def test_restart_resumes_from_the_file_and_a_corrupt_file_is_discarded(tmp_path):
    clk, rg = FakeClock(T0), FakeRg()
    sink = make_sink(tmp_path, rg, clk)
    flt = Fleet(sink, 3, clk)
    sink.pump()
    rg.up = False
    for _ in range(3):
        clk.advance(30)
        flt.poll(2)
        sink.pump()
    again = make_sink(tmp_path, rg, clk)  # a restart
    assert stats(again)["pending"] == 6 and stats(again)["acked_seq"] == 3
    rg.up = True
    while again.pump():
        pass
    assert rg.applied == flt.seq

    (tmp_path / "outbox.json").write_text("{not json")
    third = make_sink(tmp_path, rg, clk)
    assert third.counters["state_discarded"] == 1 and third.enabled


# =====================================================================================
# freshness and heartbeat
# =====================================================================================


def test_confirmed_at_is_the_polls_not_the_delivery_time(tmp_path):
    clk, rg = FakeClock(T0), FakeRg()
    sink = make_sink(tmp_path, rg, clk)
    clk.advance(30)  # the sink sees the poll 30 s after it was confirmed
    seed(sink)  # the poll itself says confirmed_at = T0
    clk.advance(3600)  # delivered an hour after the poll
    sink.pump()
    assert rg.envelopes[-1]["confirmed_at"] == _iso_z(T0)


def test_heartbeat_goes_out_every_third_of_stale_and_keeps_the_old_confirmed_at(tmp_path):
    clk, rg = FakeClock(T0), FakeRg()
    sink = make_sink(tmp_path, rg, clk)
    seed(sink)
    sink.pump()
    n = len(rg.envelopes)
    clk.advance(39)  # under 120 / 3
    sink.pump()
    assert len(rg.envelopes) == n
    clk.advance(1)  # exactly a third
    sink.pump()
    beat = rg.envelopes[-1]
    assert len(rg.envelopes) == n + 1
    assert "snapshot" not in beat and beat["events"] == []  # a heartbeat carries neither
    assert beat["confirmed_at"] == _iso_z(T0) and beat["stale_after_s"] == 120


# =====================================================================================
# the token never leaks
# =====================================================================================


def test_token_never_appears_in_logs_exceptions_reprs_or_the_outbox_file(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    clk, rg = FakeClock(T0), FakeRg()
    tokens = token_file(tmp_path)
    sink = make_sink(tmp_path, rg, clk)
    flt = Fleet(sink, 3, clk)
    seen: list[str] = []

    def run(label: str) -> None:
        try:
            sink.pump()
        except Exception as exc:  # noqa: BLE001
            seen.append(f"{label}:{exc!r}")

    run("ok")
    rg.mode = "bare200"
    clk.advance(30)
    flt.poll(1)
    run("bare")
    rg.mode = "status:400"  # the server echoes the token in its error body
    run("echo")
    sink.resume()
    rg.mode = "ack"
    rg.token = "SOMETHING-ELSE"  # a 401 whose body echoes the token
    run("unauthorized")
    sink.resume()
    rg.token = FAKE_TOKEN
    rg.up = False
    run("down")
    tokens.unlink()
    run("no-token-file")  # the file is gone
    with pytest.raises(TokenUnavailable) as gone:
        read_token(str(tokens))
    tokens.write_text("{broken " + FAKE_TOKEN)
    with pytest.raises(TokenUnavailable) as broken:
        read_token(str(tokens))
    surfaces = [
        caplog.text,
        " ".join(seen),
        repr(sink),
        repr(sink.stats()),
        repr(sink._config),
        repr(gone.value),
        str(gone.value),
        repr(broken.value),
        str(broken.value),
        (tmp_path / "outbox.json").read_text(),
    ]
    for text in surfaces:
        assert FAKE_TOKEN not in text
    assert "ACCT-TEST" not in caplog.text  # no full account id in any log line
    assert FAKE_TOKEN in "".join(h.get("Authorization", "") for h in rg.headers)  # it is sent, and only there


def test_the_token_is_read_by_path_at_each_delivery_so_a_rotation_is_picked_up(tmp_path):
    clk, rg = FakeClock(T0), FakeRg()
    sink = make_sink(tmp_path, rg, clk)
    flt = Fleet(sink, 2, clk)
    sink.pump()
    rg.token = "ROTATED-FAKE-TOKEN"
    token_file(tmp_path, "ROTATED-FAKE-TOKEN")
    clk.advance(30)
    flt.poll(1)
    assert sink.pump() == 1
    assert rg.headers[-1]["Authorization"] == "Bearer ROTATED-FAKE-TOKEN"


def test_read_token_takes_a_raw_token_file_or_the_connector_row(tmp_path):
    raw = tmp_path / "raw"
    raw.write_text("  RAW-FAKE-TOKEN \n")
    assert read_token(str(raw)) == "RAW-FAKE-TOKEN"
    doc = tmp_path / "doc"
    doc.write_text(json.dumps({"version": 1, "tokens": [
        {"token": "OP-FAKE", "role": "operator"}, {"token": "CONN-FAKE", "role": "connector"}]}))
    assert read_token(str(doc)) == "CONN-FAKE"
    doc.write_text(json.dumps({"version": 1, "tokens": [{"token": "OP-FAKE", "role": "operator"}]}))
    with pytest.raises(TokenUnavailable):
        read_token(str(doc))


# =====================================================================================
# budgets (B7, I12)
# =====================================================================================


def test_budget_24h_of_events_stays_under_the_50mb_cap_and_replays_in_order(tmp_path):
    clk, rg = FakeClock(T0), FakeRg()
    sink = make_sink(tmp_path, rg, clk)  # the default 50 MB cap
    flt = Fleet(sink, 50, clk)
    assert sink.pump() == 1
    rg.up = False  # riskguard is down for the whole 24 h
    out = tmp_path / "outbox.json"
    biggest = 0
    for poll in range(2880):  # a poll every 30 s for 24 h, five flips each
        clk.advance(30)
        flt.poll(5)
        if poll % 60 == 59:
            sink.pump()
            biggest = max(biggest, out.stat().st_size)
    assert flt.seq - 50 == 14_400
    assert biggest <= 50 * 1024 * 1024
    assert stats(sink)["compactions"] == 0 and stats(sink)["pending"] == 14_400
    rg.up = True
    while sink.pump():
        pass
    assert rg.applied == flt.seq
    assert rg.applied_seqs[-14_400:] == list(range(51, flt.seq + 1))  # replayed in order, none lost
    assert all(len(e.get("events", [])) <= 1000 for e in rg.envelopes)  # the envelope cap
    assert max(len(json.dumps(e)) for e in rg.envelopes) < 1_000_000  # under riskguard's 1 MB body limit


def test_budget_outbox_cap_fill_compacts_to_snapshot_plus_tail(tmp_path):
    cap = 96 * 1024
    clk, rg = FakeClock(T0), FakeRg()
    sink = make_sink(tmp_path, rg, clk, cap_bytes=cap)
    flt = Fleet(sink, 50, clk)
    sink.pump()
    rg.up = False
    out = tmp_path / "outbox.json"
    for poll in range(2880):  # far more than the cap holds
        clk.advance(30)
        flt.poll(5)
        if poll % 20 == 19:
            sink.pump()
            assert out.stat().st_size <= cap
    sink.pump()
    s = stats(sink)
    assert s["compactions"] >= 1 and s["dropped_events"] > 0  # counted, never silent
    assert s["size_bytes"] <= cap and out.stat().st_size <= cap
    rg.up = True
    while sink.pump():
        pass
    assert rg.applied == flt.seq
    assert rg.snapshots[-1] > 50  # resumed from the LATEST snapshot, then the tail since it
    tail_since = [q for q in rg.applied_seqs if q > rg.snapshots[-1]]
    assert tail_since == list(range(rg.snapshots[-1] + 1, flt.seq + 1))
    assert {k: v["status"] for k, v in rg.members.items()} == {rec(i).key: st for i, st in flt.status.items()}


def test_budget_tail_event_cap_fill_compacts(tmp_path):
    clk, rg = FakeClock(T0), FakeRg()
    sink = make_sink(tmp_path, rg, clk, max_tail_events=40)
    flt = Fleet(sink, 5, clk)
    sink.pump()
    rg.up = False
    for _ in range(60):  # 120 events past a 40-event cap
        clk.advance(30)
        flt.poll(2)
    s = stats(sink)
    assert s["compactions"] >= 1 and s["dropped_events"] > 0 and s["pending"] <= 40
    rg.up = True
    while sink.pump():
        pass
    assert rg.applied == flt.seq and rg.snapshots[-1] > 5


def test_budget_the_worker_cap_on_envelopes_per_pump(tmp_path):
    clk, rg = FakeClock(T0), FakeRg()
    sink = make_sink(tmp_path, rg, clk)
    flt = Fleet(sink, 5, clk)
    sink.pump()
    rg.up = False
    for _ in range(300):
        clk.advance(30)
        flt.poll(10)  # 3000 events: 3 envelopes of 1000
    rg.up = True
    assert sink.pump() <= 64
    while sink.pump():
        pass
    assert rg.applied == flt.seq


def test_budget_delivery_adds_under_50ms_to_the_visibility_budget(tmp_path):
    clk, rg = FakeClock(T0), FakeRg()
    sink = make_sink(tmp_path, rg, clk)
    flt = Fleet(sink, 20, clk)
    sink.pump()
    laps = []
    for _ in range(20):
        clk.advance(30)
        t0 = time.perf_counter()
        flt.poll(1)  # observe
        sink.pump()  # persist + build + post (to a double) + apply the ack
        laps.append(time.perf_counter() - t0)
    assert max(laps) < 0.05, laps


def test_the_order_path_never_waits_on_the_network_or_the_disk(tmp_path):
    clk = FakeClock(T0)
    release = threading.Event()

    def stuck(url, body, headers, timeout):
        release.wait(10)
        return 0, ""

    sink = make_sink(tmp_path, None, clk)
    sink._post = stuck
    seed(sink)
    sink.start(tick_seconds=0.05)
    try:
        time.sleep(0.2)  # the worker is now blocked inside the post
        t0 = time.perf_counter()
        for i in range(200):
            sink.observe(fleet(3, at=clk.now_utc()), [], 3)  # a heartbeat-only poll
        assert time.perf_counter() - t0 < 1.0
    finally:
        release.set()
        sink.stop()


def test_the_worker_delivers_on_observe(tmp_path):
    clk, rg = FakeClock(T0), FakeRg()
    sink = make_sink(tmp_path, rg, clk)
    sink.start(tick_seconds=30)
    try:
        seed(sink)
        deadline = time.monotonic() + 3
        while rg.applied < 3 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert rg.applied == 3
    finally:
        sink.stop()


# =====================================================================================
# G2: the real riskguard-service (built from source, temp data dir, fixture token) + fake ntx
# =====================================================================================

RG_EXE = os.environ.get("RG_SERVICE_EXE", "")
needs_rg = pytest.mark.skipif(not RG_EXE or not Path(RG_EXE).exists(), reason="RG_SERVICE_EXE is not set")


class Riskguard:
    def __init__(self, base: str, token: str, data: Path) -> None:
        self.base, self.token, self.data = base, token, data

    def call(self, path: str, method: str = "GET", body: dict | None = None, token: str | None = None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(
            self.base + path,
            data=data,
            method=method,
            headers={"Authorization": f"Bearer {token or self.token}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read() or b"null")
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"null")

    def accounts(self, platform: str = "tradingview") -> list[str]:
        status, body = self.call(f"/v1/accounts?platform={platform}")
        assert status == 200
        return sorted(a["name"] for a in body["accounts"])

    def check(self, account: str, reducing: bool = False) -> dict:
        status, body = self.call(
            f"/v1/check?account={account}",
            "POST",
            {"instrument": "NQ", "side": "buy", "qty": 1, "isReducing": reducing},
        )
        assert status == 200
        return body


@pytest.fixture
def rg(tmp_path):
    if not RG_EXE or not Path(RG_EXE).exists():
        pytest.skip("RG_SERVICE_EXE is not set")
    data = Path(tempfile.mkdtemp(prefix="bp4a-rg-"))
    journal = data / "journal"
    journal.mkdir()
    tokens = data / "tokens.json"
    tokens.write_text(json.dumps({"version": 1, "tokens": [{"token": FAKE_TOKEN, "role": "connector"}]}))
    env = dict(
        os.environ,
        RISKGUARD_SERVICE_PORT="0",
        RISKGUARD_SERVICE_TOKENS_FILE=str(tokens),
        RISKGUARD_SERVICE_JOURNAL_DIR=str(journal),
    )
    env.pop("RISKGUARD_SERVICE_TOKEN", None)
    proc = subprocess.Popen(
        [RG_EXE], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, cwd=str(data)
    )
    addr = None
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        line = proc.stdout.readline()
        if not line:
            break
        if "listening on" in line:
            addr = line.split("listening on ")[1].split()[0]
            break
    if addr is None:
        proc.kill()
        pytest.fail("riskguard-service did not start")
    threading.Thread(target=lambda: [None for _ in proc.stdout], daemon=True).start()  # drain the pipe
    try:
        yield Riskguard("http://" + addr, FAKE_TOKEN, data)
    finally:
        proc.terminate()  # this test's own child, by handle: never by image name
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()


def real_sink(tmp_path, rg: Riskguard, clk, **kw) -> RiskguardRegistrySink:
    cfg = dict(
        enabled=True,
        base_url=rg.base,
        token_file=str(rg.data / "tokens.json"),
        outbox_path=str(tmp_path / "outbox.json"),
    )
    cfg.update(kw)
    return RiskguardRegistrySink(RegistrySinkConfig(**cfg), clock=clk)


def wait_for(fn, seconds: float):
    deadline = time.monotonic() + seconds
    last = None
    while time.monotonic() < deadline:
        last = fn()
        if last:
            return last
        time.sleep(0.01)
    return last


@needs_rg
def test_g2_an_account_added_at_ntx_is_visible_at_riskguard_within_2s(rg, daemon, tmp_path):
    clk = FakeClock(datetime.now(timezone.utc))
    daemon.snapshot_body = snap([acct("ACCT-TEST-194", "900194", identity=IDENT_A)])
    sink = real_sink(tmp_path, rg, clk)
    adapter = TradovateAdapter(make_client(daemon), clock=clk, on_poll=sink.observe)
    adapter.poller.poll()
    sink.start(tick_seconds=0.2)
    try:
        assert wait_for(lambda: rg.accounts() == ["D900194"], 2)
        daemon.snapshot_body = snap(
            [acct("ACCT-TEST-194", "900194", identity=IDENT_A), acct("ACCT-TEST-277", "900277", identity=IDENT_B)]
        )
        t0 = time.monotonic()
        events = adapter.poller.poll()  # the engine event for the new account
        assert [e.kind for e in events] == ["added"]
        assert wait_for(lambda: rg.accounts() == ["D900194", "D900277"], 2)
        assert time.monotonic() - t0 < 2.0
        status, body = rg.call("/v1/accounts?scope=all")
        assert status == 200
        assert all("api_ref" not in json.dumps(a) for a in body["accounts"])  # D-13
    finally:
        sink.stop()
    assert sink.halted is None and stats(sink)["acked_seq"] == 2


@needs_rg
def test_g2_budget_delivery_to_the_real_service_adds_under_50ms(rg, tmp_path):
    clk = FakeClock(datetime.now(timezone.utc))
    sink = real_sink(tmp_path, rg, clk)
    flt = Fleet(sink, 20, clk)
    assert sink.pump() == 1
    laps = []
    for _ in range(10):
        t0 = time.perf_counter()
        flt.poll(1)
        sink.pump()
        laps.append(time.perf_counter() - t0)
    assert sorted(laps)[len(laps) // 2] < 0.05, laps  # the median: loopback + one fsync-free write
    assert max(laps) < 0.25, laps
    status, body = rg.call("/v1/accounts?scope=all")
    got = {a["routes"][1]["local_name"]: a["status"] for a in body["accounts"]}
    assert got == {f"D900{i:03d}": s for i, s in flt.status.items()}


@needs_rg
def test_g2_ntx_down_goes_stale_and_known_accounts_are_not_refused_but_an_unseen_one_is(rg, daemon, tmp_path):
    # The poll's confirmed_at is 116 s old when sent: fresh for 4 more real seconds.
    clk = FakeClock(datetime.now(timezone.utc) - timedelta(seconds=116))
    daemon.snapshot_body = snap([acct("ACCT-TEST-194", "900194", identity=IDENT_A)])
    sink = real_sink(tmp_path, rg, clk)
    adapter = TradovateAdapter(make_client(daemon), clock=clk, on_poll=sink.observe)
    adapter.poller.poll()
    assert sink.pump() == 1
    fresh = rg.check("ACCT-NEVER-SEEN")
    assert fresh.get("ruleId") != "ACCOUNT_UNCONFIRMED"  # the source is still fresh: nothing refused
    daemon.stop()  # ntx goes down
    clk.advance(40)
    adapter.poller.poll()  # keeps the last good list, same confirmed_at
    sink.pump()  # a heartbeat goes out carrying the OLD confirmed_at
    assert rg.accounts() == ["D900194"]
    time.sleep(5)  # the 120 s line passes on riskguard's clock
    clk.advance(40)
    adapter.poller.poll()
    sink.pump()
    status, body = rg.call("/v1/accounts?platform=tradingview")
    assert body["sources"]["tradovate"]["stale"] is True
    unseen = rg.check("ACCT-NEVER-SEEN")
    assert unseen["allowed"] is False and unseen["ruleId"] == "ACCOUNT_UNCONFIRMED"
    assert unseen["detail"].startswith("account_unconfirmed")
    known = rg.check("D900194")
    assert known.get("ruleId") != "ACCOUNT_UNCONFIRMED"  # a known account is not refused for staleness
    assert rg.check("ACCT-NEVER-SEEN", reducing=True).get("ruleId") != "ACCOUNT_UNCONFIRMED"


@needs_rg
def test_g2_riskguard_refuses_a_seq_gap_and_asks_for_a_snapshot(rg, tmp_path):
    clk = FakeClock(datetime.now(timezone.utc))
    sink = real_sink(tmp_path, rg, clk)
    seed(sink, 2, clk.now_utc())
    assert sink.pump() == 1
    base = _iso_z(clk.now_utc())
    gap = {
        "contract": "broker-account/1",
        "broker": "tradovate",
        "confirmed_at": base,
        "stale_after_s": 120,
        "events": [
            {
                "kind": "changed",
                "key": rec(1).key,
                "seq": 9,
                "at": base,
                "fields": {"status": "closed"},
            }
        ],
    }
    status, body = rg.call("/v1/registry/ingest", "POST", gap)
    assert status == 409 and body["need"] == "snapshot" and body["applied_seq"] == 2
    status, body = rg.call("/v1/accounts?scope=all")
    assert {a["status"] for a in body["accounts"]} == {"active"}  # nothing was applied


@needs_rg
def test_g2_riskguard_down_then_up_drains_in_order_into_the_real_service(rg, tmp_path):
    clk = FakeClock(datetime.now(timezone.utc))
    sink = real_sink(tmp_path, rg, clk)
    flt = Fleet(sink, 10, clk)
    assert sink.pump() == 1
    up = {"on": False}
    real = sink._post

    def gated(url, body, headers, timeout):
        return real(url, body, headers, timeout) if up["on"] else (0, "")

    sink._post = gated
    for _ in range(30):
        clk.advance(30)
        flt.poll(4)
        sink.pump()
    assert stats(sink)["pending"] == 120
    up["on"] = True
    while sink.pump():
        pass
    assert stats(sink)["acked_seq"] == flt.seq and stats(sink)["pending"] == 0
    status, body = rg.call("/v1/accounts?scope=all")
    got = {a["routes"][1]["local_name"]: a["status"] for a in body["accounts"]}
    assert got == {f"D900{i:03d}": s for i, s in flt.status.items()}
    registry = rg.data / "journal" / "state" / "account_registry.json"
    assert registry.exists() and FAKE_TOKEN not in registry.read_text()


@needs_rg
def test_g2_the_real_service_rejects_a_wrong_token_and_the_sink_halts_without_leaking(rg, tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    clk = FakeClock(datetime.now(timezone.utc))
    bad = tmp_path / "bad-token"
    bad.write_text("FAKE-WRONG-TOKEN-0000")
    sink = real_sink(tmp_path, rg, clk, token_file=str(bad))
    seed(sink)
    sink.pump()
    assert sink.halted == "unauthorized"
    assert "FAKE-WRONG-TOKEN-0000" not in caplog.text + repr(sink) + (tmp_path / "outbox.json").read_text()

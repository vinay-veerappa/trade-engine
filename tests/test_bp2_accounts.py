"""BP-2: the account port (BROKER_PLATFORM phase 2, contract sections 2-3).

The enforcing tests the brief names: the diff (add/remove/change/nothing), the failed
poll (B3), restart replay from the ledger, interval and staleness, the key and masking
rules, no `api_ref` anywhere, the contract-shape fixtures, and the protocol contract for
every registered adapter.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from trade_engine.accounts import _rs
from trade_engine.accounts.poll import PollAndDiff
from trade_engine.interfaces.broker import (
    AccountEvent,
    AccountRecord,
    AccountSnapshot,
    BrokerAdapter,
    Route,
)
from trade_engine.ledger.store import Ledger
from trade_engine.sim.broker import SimBroker
from trade_engine.sim.snapshot_venue import SnapshotVenue
from trade_engine.tos_paper.broker import MirrorBinding, TosPaperBroker

NOW = datetime(2026, 11, 11, 18, 0, tzinfo=timezone.utc)
FAKE_API_REF = "FAKE-API-REF-194"
FAKE_ID = "ACCT-TEST-194"


class FakeClock:
    def __init__(self, now: datetime = NOW) -> None:
        self.now = now

    def now_utc(self) -> datetime:
        return self.now

    def advance(self, seconds: int) -> None:
        self.now += timedelta(seconds=seconds)


def record(
    broker: str = "tos_paper",
    account_id: str = FAKE_ID,
    *,
    status: str = "active",
    env: str = "paper",
    account_type: str | None = "margin",
    routes: tuple[Route, ...] = (),
    at: datetime = NOW,
) -> AccountRecord:
    return AccountRecord.of(
        broker,
        account_id,
        env=env,
        status=status,
        source="tos_paper",
        account_type=account_type,
        api_ref=FAKE_API_REF,
        routes=routes,
        confirmed_at=at,
    )


def snapshot(*records: AccountRecord, at: datetime = NOW) -> AccountSnapshot:
    return AccountSnapshot(records=tuple(records), confirmed_at=at)


class FakeBroker:
    """A poller's I/O shell: a scripted list of snapshots, or a raised error."""

    name = "FakeBroker"
    env = "paper"
    stale_after = 120

    def __init__(self, clock: FakeClock, *snapshots: AccountSnapshot) -> None:
        self._clock = clock
        self._snapshots = list(snapshots)
        self.fail = False

    def accounts(self) -> AccountSnapshot:
        if self.fail:
            raise RuntimeError("venue down")
        if len(self._snapshots) > 1:
            return self._snapshots.pop(0)
        return self._snapshots[0]

    def account_events(self, since: datetime) -> list[AccountEvent]:
        return []


# --- the diff rule ---------------------------------------------------------------------


def test_snapshot_add_remove_change_and_nothing_changed() -> None:
    old = snapshot(
        record(account_id="ACCT-TEST-194", routes=(Route("tos", "ACCT-TEST-194"),)),
        record(account_id="ACCT-TEST-195", routes=(Route("tos", "ACCT-TEST-195"),)),
    )
    new = snapshot(
        record(
            account_id="ACCT-TEST-194",
            status="closed",
            routes=(Route("tos", "ACCT-TEST-194"), Route("tradingview", "D194")),
        ),
        record(account_id="ACCT-TEST-196", routes=(Route("tos", "ACCT-TEST-196"),)),
    )
    rows = _rs.diff(old.as_contract(), new.as_contract(), NOW, 10)
    events = [AccountEvent.from_internal(row) for row in rows]
    assert [(e.kind, e.key, e.seq) for e in events] == [
        ("changed", "tos_paper:ACCT-TEST-194", 10),
        ("removed", "tos_paper:ACCT-TEST-195", 11),
        ("added", "tos_paper:ACCT-TEST-196", 12),
    ]
    changed = events[0]
    assert [name for name, _, _ in changed.changes] == ["routes", "status"]
    status = dict((name, (old_v, new_v)) for name, old_v, new_v in changed.changes)["status"]
    assert status == ("active", "closed")
    added = events[2]
    assert added.record is not None and added.record.key == "tos_paper:ACCT-TEST-196"
    # Nothing changed: no events at all.
    assert _rs.diff(new.as_contract(), new.as_contract(), NOW, 13) == []


def test_routes_compare_as_a_sorted_set() -> None:
    old = snapshot(record(routes=(Route("tos", "a"), Route("nt8", "n"))))
    new = snapshot(record(routes=(Route("nt8", "n"), Route("tos", "a"))))
    assert _rs.diff(old.as_contract(), new.as_contract(), NOW, 1) == []


# --- the failed poll (B3) --------------------------------------------------------------


def test_a_failed_poll_emits_nothing_and_keeps_the_last_good_list() -> None:
    clock = FakeClock()
    first = snapshot(record(account_id="ACCT-TEST-194"), record(account_id="ACCT-TEST-195"))
    broker = FakeBroker(clock, first)
    poller = PollAndDiff(broker, clock=clock)
    assert [e.kind for e in poller.poll()] == ["added", "added"]
    assert poller.last_good is first

    broker.fail = True
    clock.advance(60)
    assert poller.poll() == []
    assert poller.last_good is first  # the last good list, never emptied

    # A later good poll diffs against the last good one, not an empty list.
    later = snapshot(record(account_id="ACCT-TEST-194"), record(account_id="ACCT-TEST-195", status="closed"))
    broker.fail = False
    broker._snapshots = [later]
    events = poller.poll()
    assert [(e.kind, e.key) for e in events] == [("changed", "tos_paper:ACCT-TEST-195")]
    assert poller.last_good is later


def test_a_failed_poll_logs_nothing_but_the_error_kind(caplog) -> None:
    clock = FakeClock()
    broker = FakeBroker(clock, snapshot(record()))
    broker.fail = True
    with caplog.at_level(logging.WARNING, logger="trade_engine.accounts.poll"):
        assert PollAndDiff(broker, clock=clock).poll() == []
    assert "RuntimeError" in caplog.text
    assert FAKE_ID not in caplog.text
    assert FAKE_API_REF not in caplog.text


# --- restart replay (I1, I2) -----------------------------------------------------------


def test_a_restart_replays_membership_and_the_seq_from_the_ledger(tmp_path) -> None:
    clock = FakeClock()
    first = snapshot(
        record(account_id="ACCT-TEST-194", routes=(Route("tos", "ACCT-TEST-194"),)),
        record(account_id="ACCT-TEST-195"),
    )
    with Ledger(tmp_path / "ledger.db") as ledger:
        poller = PollAndDiff(FakeBroker(clock, first), clock=clock, ledger=ledger)
        first_events = poller.poll()
        assert [e.seq for e in first_events] == [1, 2]
        assert len(ledger.events()) == 2

        # A NEW process: a fresh poller and adapter, the same ledger.
        second = snapshot(
            record(account_id="ACCT-TEST-194", routes=(Route("tos", "ACCT-TEST-194"),)),
            record(account_id="ACCT-TEST-195", status="closed"),
        )
        restarted = PollAndDiff(FakeBroker(clock, second), clock=clock, ledger=ledger)
        events = restarted.poll()
        # Replayed membership: no `added` for either; only the real change, seq continuing.
        assert [(e.kind, e.key, e.seq) for e in events] == [("changed", "tos_paper:ACCT-TEST-195", 3)]

        # Rebuild from the ledger alone and compare.
        rows = [
            {
                "kind": {
                    "AccountAdded": "added",
                    "AccountRemoved": "removed",
                    "AccountChanged": "changed",
                }[e.kind.value],
                "key": e.payload.key,
                "display": e.payload.display,
                "seq": e.payload.seq,
                "at": e.payload.at.isoformat(),
                "record": None if e.payload.record is None else e.payload.record.__dict__,
                "changes": [[n, o, v] for n, o, v in e.payload.changes],
            }
            for e in ledger.events()
        ]
        for row in rows:
            if row["record"] is not None:
                row["record"] = _record_payload(row["record"])
        rebuilt = _rs.replay(rows)
        keys = [r["key"] for r in rebuilt["records"]]
        assert keys == ["tos_paper:ACCT-TEST-194", "tos_paper:ACCT-TEST-195"]


def _record_payload(data: dict) -> dict:
    return {
        "key": data["key"],
        "display": data["display"],
        "env": data["env"],
        "status": data["status"],
        "class": data["class_"],
        "firm": data["firm"],
        "account_type": data["account_type"],
        "routes": [route.as_contract() for route in data["routes"]],
        "source": data["source"],
        "confirmed_at": data["confirmed_at"].isoformat().replace("+00:00", "Z"),
    }


# --- interval and freshness ------------------------------------------------------------


def test_interval_below_thirty_seconds_is_refused_and_sixty_is_the_default() -> None:
    clock = FakeClock()
    broker = FakeBroker(clock, snapshot(record()))
    assert PollAndDiff(broker, clock=clock).interval_seconds == 60
    with pytest.raises(ValueError):
        PollAndDiff(broker, clock=clock, interval_seconds=29)
    assert PollAndDiff(broker, clock=clock, interval_seconds=30).interval_seconds == 30


def test_staleness_is_at_exactly_stale_after_and_not_just_under() -> None:
    snap = snapshot(record())
    assert snap.is_stale(NOW + timedelta(seconds=120), 120) is True
    assert snap.is_stale(NOW + timedelta(seconds=119, microseconds=999999), 120) is False


# --- the key and masking rules ---------------------------------------------------------


def test_key_keeps_case_and_refuses_empty_whitespace_and_local() -> None:
    assert _rs.key("Tradovate", "APEX12345aBc") == "Tradovate:APEX12345aBc"
    for broker, account_id in (("b", ""), ("", "a"), ("b b", "a"), ("b", "a a"), ("b", "a\tb"), ("b", "a:b")):
        with pytest.raises(ValueError):
            _rs.key(broker, account_id)
    with pytest.raises(ValueError):
        _rs.key("local", "Sim101")


@pytest.mark.parametrize(
    ("account_id", "display"),
    [("ACCT-TEST-194", "*194"), ("ab", "*ab"), ("a", "*a"), ("", "*")],
)
def test_masking_shows_only_the_last_three_characters(account_id: str, display: str) -> None:
    assert _rs.mask(account_id) == display


# --- privacy: no api_ref, no full id outside the key -----------------------------------


def test_no_api_ref_and_no_full_id_outside_the_key(caplog, tmp_path) -> None:
    clock = FakeClock()
    first = snapshot(record(routes=(Route("tos", FAKE_ID),)))
    with Ledger(tmp_path / "ledger.db") as ledger:
        poller = PollAndDiff(FakeBroker(clock, first), clock=clock, ledger=ledger)
        with caplog.at_level(logging.WARNING, logger="trade_engine.accounts.poll"):
            events = poller.poll()
        # The carrier's repr excludes api_ref (D-13).
        assert FAKE_API_REF not in repr(first.records[0])
        for event in events:
            wire = event.as_wire()
            assert FAKE_API_REF not in json.dumps(wire)
            # The full id may appear only in the key (contract section 6); display is masked.
            assert FAKE_ID not in event.display
            assert event.record is None or FAKE_API_REF not in json.dumps(event.record.as_contract())
        # The ledger carries the record/fields with no api_ref.
        for stored in ledger.events():
            payload_json = json.dumps(stored.payload.__dict__, default=str)
            assert FAKE_API_REF not in payload_json
        # A failed poll's log line names no account id and no api_ref.
        broker = FakeBroker(clock, first)
        broker.fail = True
        PollAndDiff(broker, clock=clock).poll()
    assert FAKE_ID not in caplog.text
    assert FAKE_API_REF not in caplog.text


# --- the contract-shape fixtures (BROKER_CONTRACT.md sections 2-3) ---------------------


def test_record_serializes_to_the_contract_section_two_shape() -> None:
    rec = record(routes=(Route("tos", "ACCT-TEST-194"), Route("tradingview", "D194")))
    assert rec.as_contract() == {
        "key": "tos_paper:ACCT-TEST-194",
        "display": "*194",
        "env": "paper",
        "status": "active",
        "class": None,
        "firm": None,
        "account_type": "margin",
        "routes": [
            {"platform": "tos", "local_name": "ACCT-TEST-194"},
            {"platform": "tradingview", "local_name": "D194"},
        ],
        "source": "tos_paper",
        "confirmed_at": "2026-11-11T18:00:00Z",
    }
    assert "api_ref" not in rec.as_contract()


def test_each_event_kind_serializes_to_the_contract_section_three_shape() -> None:
    rec = record(routes=(Route("tos", "ACCT-TEST-194"),))
    added = AccountEvent(kind="added", key=rec.key, display=rec.display, at=NOW, seq=42, record=rec)
    assert added.as_wire() == {
        "kind": "added",
        "key": "tos_paper:ACCT-TEST-194",
        "display": "*194",
        "seq": 42,
        "at": "2026-11-11T18:00:00Z",
        "record": rec.as_contract(),
    }
    changed = AccountEvent(
        kind="changed",
        key=rec.key,
        display=rec.display,
        at=NOW,
        seq=43,
        changes=(("status", "active", "closed"), ("routes", (Route("tos", "ACCT-TEST-194"),), ())),
    )
    assert changed.as_wire() == {
        "kind": "changed",
        "key": "tos_paper:ACCT-TEST-194",
        "display": "*194",
        "seq": 43,
        "at": "2026-11-11T18:00:00Z",
        "fields": {"status": "closed", "routes": []},  # new values only
    }
    removed = AccountEvent(kind="removed", key=rec.key, display=rec.display, at=NOW, seq=44)
    assert removed.as_wire() == {
        "kind": "removed",
        "key": "tos_paper:ACCT-TEST-194",
        "display": "*194",
        "seq": 44,
        "at": "2026-11-11T18:00:00Z",
    }


# --- every registered adapter ----------------------------------------------------------


def _tos_broker() -> TosPaperBroker:
    binding = MirrorBinding(
        venue_account="D-00000001",
        account_type="margin",
        mirrored_accounts=("OPT_CSP",),
        minimum_balance=Decimal("100000"),
    )
    return TosPaperBroker(object(), binding, clock=FakeClock())


def test_every_registered_adapter_satisfies_the_account_port() -> None:
    clock = FakeClock()
    adapters = [
        SimBroker(FAKE_ID, clock, Decimal("0")),
        SnapshotVenue(FAKE_ID, clock),
        _tos_broker(),
    ]
    for adapter in adapters:
        assert isinstance(adapter, BrokerAdapter), type(adapter).__name__
        assert isinstance(adapter.stale_after, int)
        snap = adapter.accounts()
        assert isinstance(snap, AccountSnapshot)
        assert len(snap.records) == 1
        assert isinstance(snap.records[0], AccountRecord)
        assert isinstance(adapter.account_events(clock.now_utc()), list)


def test_tos_paper_reads_mirror_json_as_its_fixed_list(tmp_path) -> None:
    path = tmp_path / "mirror.json"
    path.write_text(
        json.dumps(
            {
                "venues": [
                    {
                        "venue_account": "D-00000001",
                        "account_type": "margin",
                        "mirrored_accounts": ["OPT_CSP"],
                        "minimum_balance": "150000",
                    },
                    {
                        "venue_account": "D-00000002",
                        "account_type": "ira",
                        "mirrored_accounts": ["OPT_PUT_SPREAD"],
                        "minimum_balance": "50000",
                    },
                ]
            }
        ),
        encoding="utf8",
    )
    broker = _tos_broker()
    broker._mirror_path = path
    snap = broker.accounts()
    assert [r.key for r in snap.records] == ["tos_paper:D-00000001", "tos_paper:D-00000002"]
    assert {r.env for r in snap.records} == {"paper"}
    assert {r.source for r in snap.records} == {"tos_paper"}
    assert {r.status for r in snap.records} == {"active"}

    # A malformed file keeps the last good list (B3) instead of emptying it.
    path.write_text("{not json", encoding="utf8")
    assert broker.accounts() is snap


def test_the_poller_keeps_exactly_one_last_good_snapshot() -> None:
    clock = FakeClock()
    first = snapshot(record())
    second = snapshot(record(status="closed"))
    poller = PollAndDiff(FakeBroker(clock, first, second), clock=clock)
    poller.poll()
    assert poller.last_good is first
    poller.poll()
    assert poller.last_good is second
    assert not isinstance(poller.last_good, list)

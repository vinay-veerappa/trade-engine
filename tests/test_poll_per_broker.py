"""F11: `PollAndDiff` scopes its account diff and seq to its own broker.

Two brokers can share one ledger. A restarted poller for broker A must replay only A's
membership and A's seq; it must never diff against, or continue the seq of, broker B.
"""

from __future__ import annotations

from datetime import datetime, timezone

from trade_engine.accounts.poll import PollAndDiff
from trade_engine.interfaces.broker import AccountRecord, AccountSnapshot, Route
from trade_engine.ledger.store import Ledger

NOW = datetime(2026, 11, 23, 18, 0, tzinfo=timezone.utc)
BROKER_A = "tos_paper"
BROKER_B = "tradovate"


class FakeClock:
    def now_utc(self) -> datetime:
        return NOW


def account(broker: str, account_id: str, *, status: str = "active") -> AccountRecord:
    return AccountRecord.of(
        broker,
        account_id,
        env="paper",
        status=status,
        source=broker,
        account_type="margin",
        routes=(Route("tos", account_id),),
        confirmed_at=NOW,
    )


def snapshot(*records: AccountRecord) -> AccountSnapshot:
    return AccountSnapshot(records=tuple(records), confirmed_at=NOW)


class ScriptedBroker:
    """A poller's I/O shell: returns whatever snapshot the test last set."""

    name = "ScriptedBroker"
    env = "paper"
    stale_after = 120

    def __init__(self, snap: AccountSnapshot) -> None:
        self.snap = snap

    def accounts(self) -> AccountSnapshot:
        return self.snap

    def account_events(self, since: datetime) -> list:
        return []


def _poller(ledger: Ledger, venue: ScriptedBroker, broker: str | None) -> PollAndDiff:
    return PollAndDiff(venue, clock=FakeClock(), ledger=ledger, broker=broker)


def _seed_two_brokers(ledger: Ledger) -> None:
    """A adds two accounts (seq 1, 2); B adds three (seq 1, 2, 3); one shared ledger."""
    a_venue = ScriptedBroker(snapshot(account(BROKER_A, "ACCT-A-001"), account(BROKER_A, "ACCT-A-002")))
    assert len(_poller(ledger, a_venue, BROKER_A).poll()) == 2
    b_venue = ScriptedBroker(
        snapshot(
            account(BROKER_B, "ACCT-B-001"),
            account(BROKER_B, "ACCT-B-002"),
            account(BROKER_B, "ACCT-B-003"),
        )
    )
    assert len(_poller(ledger, b_venue, BROKER_B).poll()) == 3


def _a_snapshot() -> AccountSnapshot:
    return snapshot(account(BROKER_A, "ACCT-A-001"), account(BROKER_A, "ACCT-A-002"))


def test_restart_with_broker_given_emits_nothing_and_no_removal_of_other_brokers(tmp_path) -> None:
    with Ledger(tmp_path / "ledger.db") as ledger:
        _seed_two_brokers(ledger)
        restarted = _poller(ledger, ScriptedBroker(_a_snapshot()), BROKER_A)
        assert restarted.poll() == []


def test_restart_with_broker_none_emits_nothing_and_no_removal_of_other_brokers(tmp_path) -> None:
    with Ledger(tmp_path / "ledger.db") as ledger:
        _seed_two_brokers(ledger)
        restarted = _poller(ledger, ScriptedBroker(_a_snapshot()), None)
        events = restarted.poll()
        assert events == []
        assert not [e for e in events if e.kind == "removed"]


def test_after_restart_the_next_change_continues_this_brokers_seq(tmp_path) -> None:
    # A's previous max seq is 2; B's global max is 3. A's next change must be 3, not 4.
    for broker in (BROKER_A, None):
        with Ledger(tmp_path / f"ledger-{broker}.db") as ledger:
            _seed_two_brokers(ledger)
            venue = ScriptedBroker(_a_snapshot())
            restarted = _poller(ledger, venue, broker)
            assert restarted.poll() == []
            venue.snap = snapshot(
                account(BROKER_A, "ACCT-A-001"),
                account(BROKER_A, "ACCT-A-002", status="closed"),
            )
            events = restarted.poll()
            assert [(e.kind, e.key, e.seq) for e in events] == [
                ("changed", "tos_paper:ACCT-A-002", 3),
            ], f"broker={broker!r}"
            assert restarted.seq == 3


def test_empty_first_snapshot_with_broker_none_emits_no_removals(tmp_path) -> None:
    with Ledger(tmp_path / "ledger.db") as ledger:
        _seed_two_brokers(ledger)
        # The first successful read is empty, so the scope is empty: replay nothing (I5).
        restarted = _poller(ledger, ScriptedBroker(snapshot()), None)
        events = restarted.poll()
        assert events == []
        assert not [e for e in events if e.kind == "removed"]

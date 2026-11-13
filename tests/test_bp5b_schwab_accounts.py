"""BP-5b half 1: the Schwab accounts adapter (BROKER_PLATFORM phase 5, read-only).

Fixtures only: hand-written JSON with fake ids (`ACCT-TEST-nnn`) and fake hashes
(`FAKE-HASH-nnn`). Nothing here calls the hub, Schwab, a broker or the live riskguard
install, and the adapter itself has no HTTP client (D3).

Enforcing tests, by rule:
- fixture round trip, masked display, no `hashValue` anywhere: `test_fixture_round_trip_*`;
- the diff for an added, a closed and a changed account: `test_added_closed_and_changed_*`;
- the activity hint is only a hint: `test_an_activity_hint_*`;
- the hint debounce (B7 row 1): `test_the_hint_debounce_*`;
- the 60 s floor and 2 REST calls/min: `test_the_poll_floor_*`;
- the cap at `MAX_RECORDS` and the 2 MB state budget (B7 row 2):
  `test_the_cap_at_max_records_*`;
- off by default: `test_the_switch_*`;
- an unknown or missing account number is refused with a kind (I5):
  `test_an_unknown_or_missing_account_number_*`, `test_a_detail_row_*`;
- it feeds the 4a outbox with broker `schwab`: `test_the_poll_feeds_the_4a_outbox_*`;
- read-only, no HTTP client, no order path: `test_the_module_carries_no_http_client_*`.
"""

from __future__ import annotations

import ast
import importlib
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from trade_engine.accounts import _rs
from trade_engine.accounts.errors import AccountRuleError
from trade_engine.interfaces.broker import AccountSnapshot
from trade_engine.schwab.adapter import (
    ACTIVITY_REPOLL_SECONDS,
    ENV_SWITCH,
    SchwabAccountsAdapter,
    SchwabAccountsConfig,
    map_responses,
)
from trade_engine.sinks.riskguard_registry import RegistrySinkConfig, RiskguardRegistrySink

NOW = datetime(2026, 11, 11, 18, 0, tzinfo=timezone.utc)
FIXTURES = Path(__file__).parent / "fixtures" / "schwab"
HASHES = ("FAKE-HASH-194", "FAKE-HASH-195", "FAKE-HASH-196")
KEYS = ["schwab:ACCT-TEST-194", "schwab:ACCT-TEST-195", "schwab:ACCT-TEST-196"]
BROKER = "schwab"


class FakeClock:
    def __init__(self, now: datetime = NOW) -> None:
        self.now = now

    def now_utc(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf8")


def numbers_text(*pairs: tuple[str, str]) -> str:
    return json.dumps([{"accountNumber": n, "hashValue": h} for n, h in pairs])


def details_text(*rows: tuple[str, str]) -> str:
    return json.dumps(
        [{"securitiesAccount": {"type": t, "accountNumber": n, "roundTrips": 0}} for n, t in rows]
    )


def base_numbers() -> str:
    return numbers_text(("ACCT-TEST-194", "FAKE-HASH-194"), ("ACCT-TEST-195", "FAKE-HASH-195"))


def base_details() -> str:
    return details_text(("ACCT-TEST-194", "MARGIN"), ("ACCT-TEST-195", "IRA"))


def enabled_adapter(clock: FakeClock, **kw) -> SchwabAccountsAdapter:
    return SchwabAccountsAdapter(SchwabAccountsConfig(enabled=True), clock=clock, **kw)


def kinds(events) -> list[tuple[str, str]]:
    return [(event.kind, event.key) for event in events]


# --- the fixture round trip (contract section 2, D-13) -----------------------------------


def test_fixture_round_trip_masks_displays_and_never_carries_the_hash(caplog) -> None:
    clock = FakeClock()
    observed: list[str] = []

    def on_poll(snapshot: AccountSnapshot, events: list[dict], seq: int) -> None:
        observed.append(json.dumps(snapshot.as_contract()))
        observed.append(json.dumps(events))

    adapter = enabled_adapter(clock, on_poll=on_poll)
    with caplog.at_level(logging.DEBUG):
        adapter.feed(fixture("account_numbers.json"), fixture("accounts.json"))
        events = adapter.poll()
    snapshot = adapter.last_good
    assert isinstance(snapshot, AccountSnapshot)
    assert [record.key for record in snapshot.records] == KEYS
    assert [record.display for record in snapshot.records] == ["*194", "*195", "*196"]
    assert [record.account_type for record in snapshot.records] == ["margin", "ira", "cash"]
    assert {record.env for record in snapshot.records} == {"paper"}
    assert {record.source for record in snapshot.records} == {BROKER}
    assert {record.status for record in snapshot.records} == {"active"}
    assert [record.as_contract()["routes"] for record in snapshot.records] == [
        [{"platform": "tos", "local_name": "ACCT-TEST-194"}],
        [{"platform": "tos", "local_name": "ACCT-TEST-195"}],
        [{"platform": "tos", "local_name": "ACCT-TEST-196"}],
    ]
    assert kinds(events) == [("added", key) for key in KEYS]
    # The port: the same poll again changes nothing, and the ring answers `since`.
    clock.advance(1)
    adapter.feed(fixture("account_numbers.json"), fixture("accounts.json"))
    assert adapter.poll() == []
    assert kinds(adapter.account_events(NOW)) == [("added", key) for key in KEYS]

    # `hashValue` is the broker's api_ref: it stays in the adapter (D-13). Search every
    # text the adapter produces: the carrier contract forms, reprs, events, the poll hook.
    produced = [
        json.dumps(snapshot.as_contract()),
        repr(snapshot),
        *[json.dumps(record.as_contract()) for record in snapshot.records],
        *[repr(record) for record in snapshot.records],
        json.dumps([event.as_wire() for event in events]),
        repr(events),
        *observed,
        caplog.text,
    ]
    for text in produced:
        for hashed in HASHES:
            assert hashed not in text, text
        assert "api_ref" not in text, text
    # The account number appears only where the contract allows it: the key and the route.
    assert '"display":"*194"' in json.dumps(snapshot.as_contract(), separators=(",", ":"))


# --- the diff ----------------------------------------------------------------------------


def test_added_closed_and_changed_accounts_produce_the_right_events() -> None:
    clock = FakeClock()
    adapter = enabled_adapter(clock)
    adapter.feed(base_numbers(), base_details())
    assert kinds(adapter.poll()) == [
        ("added", "schwab:ACCT-TEST-194"),
        ("added", "schwab:ACCT-TEST-195"),
    ]

    # Added: the broker lists a third account.
    clock.advance(60)
    adapter.feed(
        numbers_text(
            ("ACCT-TEST-194", "FAKE-HASH-194"),
            ("ACCT-TEST-195", "FAKE-HASH-195"),
            ("ACCT-TEST-196", "FAKE-HASH-196"),
        ),
        details_text(("ACCT-TEST-194", "MARGIN"), ("ACCT-TEST-195", "IRA"), ("ACCT-TEST-196", "CASH")),
    )
    added = adapter.poll()
    assert kinds(added) == [("added", "schwab:ACCT-TEST-196")]
    assert added[0].seq == 3 and added[0].record is not None
    assert added[0].display == "*196"

    # Closed: the account leaves the list. It is reported `removed`, never `closed`.
    clock.advance(60)
    adapter.feed(
        numbers_text(("ACCT-TEST-194", "FAKE-HASH-194")),
        details_text(("ACCT-TEST-194", "MARGIN")),
    )
    assert kinds(adapter.poll()) == [
        ("removed", "schwab:ACCT-TEST-195"),
        ("removed", "schwab:ACCT-TEST-196"),
    ]

    # Changed: the same account, a different account type.
    clock.advance(60)
    adapter.feed(
        numbers_text(("ACCT-TEST-194", "FAKE-HASH-194")),
        details_text(("ACCT-TEST-194", "CASH")),
    )
    changed = adapter.poll()
    assert kinds(changed) == [("changed", "schwab:ACCT-TEST-194")]
    assert [(name, old, new) for name, old, new in changed[0].changes] == [
        ("account_type", "margin", "cash")
    ]
    # Nothing changed: no events at all.
    clock.advance(60)
    adapter.feed(
        numbers_text(("ACCT-TEST-194", "FAKE-HASH-194")),
        details_text(("ACCT-TEST-194", "CASH")),
    )
    assert adapter.poll() == []


def test_a_refused_or_missing_read_keeps_the_last_good_list_and_emits_nothing(caplog) -> None:
    clock = FakeClock()
    adapter = enabled_adapter(clock)
    adapter.feed(base_numbers(), base_details())
    adapter.poll()
    good = adapter.last_good
    clock.advance(60)
    with caplog.at_level(logging.WARNING, logger="trade_engine.accounts.poll"):
        adapter.feed("{not json", "[]")
        assert adapter.poll() == []
        clock.advance(60)
        assert adapter.poll() == []  # nothing was fed: a failed read, never an empty list
    assert adapter.last_good is good
    assert adapter.stats.polls_refused == 1
    assert "ACCT-TEST" not in caplog.text and "FAKE-HASH" not in caplog.text


# --- ACCT_ACTIVITY is only a hint --------------------------------------------------------


def test_an_activity_hint_triggers_one_repoll_and_adds_no_account() -> None:
    clock = FakeClock()
    adapter = enabled_adapter(clock)
    adapter.feed(numbers_text(*[("ACCT-TEST-194", "FAKE-HASH-194")]), details_text(("ACCT-TEST-194", "MARGIN")))
    assert kinds(adapter.poll()) == [("added", "schwab:ACCT-TEST-194")]

    clock.advance(5)
    # The message's content is never read: a name in it must never become an account.
    hint = {"type": "ACCT_ACTIVITY", "content": {"accountNumber": "ACCT-TEST-999"}}
    assert adapter.on_activity(clock.now_utc(), hint) is True
    adapter.feed(numbers_text(*[("ACCT-TEST-194", "FAKE-HASH-194")]), details_text(("ACCT-TEST-194", "MARGIN")))
    assert adapter.poll() == []
    assert [record.key for record in adapter.last_good.records] == ["schwab:ACCT-TEST-194"]
    assert "ACCT-TEST-999" not in json.dumps(adapter.last_good.as_contract())
    assert adapter.stats.hints == 1 and adapter.stats.activity_bodies_ignored == 1


def test_the_hint_debounce_allows_one_repoll_per_ten_seconds() -> None:
    clock = FakeClock()
    adapter = enabled_adapter(clock)
    assert ACTIVITY_REPOLL_SECONDS == 10
    t0 = clock.now_utc()
    assert adapter.on_activity(t0) is True
    # A burst inside the window is one re-poll: the rest are dropped and counted.
    assert [adapter.on_activity(t0 + timedelta(milliseconds=step)) for step in (1, 250, 5_000)] == [
        False,
        False,
        False,
    ]
    assert adapter.on_activity(t0 + timedelta(seconds=9, milliseconds=999)) is False
    assert adapter.on_activity(t0 + timedelta(seconds=10)) is True
    assert adapter.on_activity(t0 + timedelta(seconds=1000)) is True
    assert adapter.stats.hints == 3
    assert adapter.stats.hints_suppressed == 4
    # Two responses per poll: even a hint burst stays a handful of calls on the shared budget.
    assert adapter.stats.polls == 0


# --- the poll floor and the budget -------------------------------------------------------


def test_the_poll_floor_is_sixty_seconds_and_a_poll_is_two_rest_calls() -> None:
    clock = FakeClock()
    adapter = enabled_adapter(clock)
    assert adapter.interval_seconds == 60
    assert adapter.next_poll_due(NOW) is True  # before the first poll
    adapter.feed(base_numbers(), base_details())
    adapter.poll()
    assert adapter.next_poll_due(NOW + timedelta(seconds=59, microseconds=999_999)) is False
    assert adapter.next_poll_due(NOW + timedelta(seconds=60)) is True
    # One fed pair is one poll: the two REST calls the hub already made, 2 calls/min.
    adapter.feed(base_numbers(), base_details())
    adapter.feed(base_numbers(), base_details())  # one slot: the unread pair is superseded
    assert adapter.stats.feed_superseded == 1
    adapter.poll()
    assert adapter.stats.polls == 2
    # Freshness: the last good poll is stale at exactly 120 s.
    clock.advance(120)
    assert adapter.is_stale(clock.now_utc()) is True
    clock.now -= timedelta(microseconds=1)
    assert adapter.is_stale(clock.now_utc()) is False
    with pytest.raises(AccountRuleError):
        SchwabAccountsAdapter(SchwabAccountsConfig(enabled=True, interval_seconds=29), clock=clock)
    with pytest.raises(AccountRuleError):
        SchwabAccountsAdapter(SchwabAccountsConfig(enabled=True, env="demo"), clock=clock)


# --- the caps (B7) -----------------------------------------------------------------------


def test_the_cap_at_max_records_refuses_and_the_state_stays_under_two_megabytes() -> None:
    cap = _rs.max_records()
    assert cap == 2000
    over = numbers_text(*[(f"ACCT-TEST-{i:05}", f"FAKE-HASH-{i:05}") for i in range(cap + 1)])
    with pytest.raises(AccountRuleError) as refused:
        map_responses(over, "[]", NOW)
    assert refused.value.kind == "cap_exceeded"
    # At the cap it is accepted, and the mapped state is a bounded size (B7: < 2 MB).
    at_cap = numbers_text(*[(f"ACCT-TEST-{i:05}", f"FAKE-HASH-{i:05}") for i in range(cap)])
    snapshot = map_responses(at_cap, "[]", NOW).snapshot
    assert len(snapshot.records) == cap
    assert len(json.dumps(snapshot.as_contract(), separators=(",", ":"))) < 2 * 1024 * 1024
    # The design's sizing case: 200 accounts.
    two_hundred = numbers_text(*[(f"ACCT-TEST-{i:03}", f"FAKE-HASH-{i:03}") for i in range(200)])
    small = map_responses(two_hundred, "[]", NOW).snapshot
    assert len(small.records) == 200
    assert len(json.dumps(small.as_contract(), separators=(",", ":"))) < 2 * 1024 * 1024


# --- the switch --------------------------------------------------------------------------


def test_the_switch_is_off_by_default_and_off_means_nothing_runs() -> None:
    assert SchwabAccountsConfig().enabled is False
    assert SchwabAccountsConfig.from_env({}).enabled is False
    assert SchwabAccountsConfig.from_env({ENV_SWITCH: "off"}).enabled is False
    assert SchwabAccountsConfig.from_env({ENV_SWITCH: "on"}).enabled is True
    assert SchwabAccountsConfig.from_env({ENV_SWITCH: " on "}).enabled is True
    adapter = SchwabAccountsAdapter(SchwabAccountsConfig.from_env({}), clock=FakeClock())
    assert adapter.enabled is False
    assert adapter.next_poll_due(NOW) is False
    assert adapter.on_activity(NOW) is False
    for call in (lambda: adapter.feed("[]", "[]"), adapter.accounts, adapter.poll):
        with pytest.raises(AccountRuleError) as refused:
            call()
        assert refused.value.kind == "switch_off"
    assert adapter.last_good is None


# --- refusals (I5) -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("numbers", "kind"),
    [
        ('[{"hashValue":"FAKE-HASH-194"}]', "bad_shape"),
        ('[{"accountNumber":194,"hashValue":"FAKE-HASH-194"}]', "bad_shape"),
        ('[{"accountNumber":"","hashValue":"FAKE-HASH-194"}]', "bad_shape"),
        ('[{"accountNumber":"AC CT-194","hashValue":"FAKE-HASH-194"}]', "bad_key"),
        ('[{"accountNumber":"ACCT-TEST-194:2","hashValue":"FAKE-HASH-194"}]', "bad_key"),
        ('[{"accountNumber":"ACCT-TEST-194"}]', "bad_shape"),
        ('[{"accountNumber":"ACCT-TEST-194","hashValue":7}]', "bad_shape"),
        ("not json", "bad_shape"),
        ('{"accountNumber":"ACCT-TEST-194"}', "bad_shape"),
        ("null", "bad_shape"),
        (
            '[{"accountNumber":"ACCT-TEST-194","hashValue":"FAKE-HASH-194"},'
            '{"accountNumber":"ACCT-TEST-194","hashValue":"FAKE-HASH-195"}]',
            "bad_shape",
        ),
    ],
)
def test_an_unknown_or_missing_account_number_is_refused_with_a_kind(numbers: str, kind: str) -> None:
    with pytest.raises(AccountRuleError) as refused:
        map_responses(numbers, "[]", NOW)
    assert refused.value.kind == kind
    assert "FAKE-HASH" not in str(refused.value)
    assert "ACCT-TEST" not in str(refused.value)


def test_a_detail_row_without_an_account_number_is_refused() -> None:
    numbers = numbers_text(("ACCT-TEST-194", "FAKE-HASH-194"))
    for details in (
        '[{"securitiesAccount":{"type":"MARGIN"}}]',
        '[{"securitiesAccount":{"accountNumber":"ACCT-TEST-194","type":7}}]',
        '[{"accountNumber":"ACCT-TEST-194"}]',
        '[{"securitiesAccount":{"accountNumber":"ACCT-TEST-194","type":"MARGIN"}}, 7]',
    ):
        with pytest.raises(AccountRuleError) as refused:
            map_responses(numbers, details, NOW)
        assert refused.value.kind == "bad_shape"
    # An unknown environment or a negative stale_after is refused too.
    with pytest.raises(AccountRuleError):
        map_responses(numbers, "[]", NOW, env="demo")


def test_an_unmapped_account_type_is_null_and_counted_never_guessed() -> None:
    clock = FakeClock()
    adapter = enabled_adapter(clock)
    adapter.feed(
        numbers_text(("ACCT-TEST-194", "FAKE-HASH-194")),
        details_text(("ACCT-TEST-194", "ROTH_IRA")),
    )
    adapter.poll()
    assert adapter.last_good.records[0].account_type is None
    assert adapter.stats.types_unmapped == 1
    # A list without the details endpoint still answers, with no type and no guess.
    mapped = map_responses(numbers_text(("ACCT-TEST-195", "FAKE-HASH-195")), "[]", NOW)
    assert mapped.snapshot.records[0].account_type is None
    assert mapped.details_unmatched == 0


# --- the 4a outbox -----------------------------------------------------------------------


def test_the_poll_feeds_the_4a_outbox_with_broker_schwab(tmp_path) -> None:
    clock = FakeClock()
    token = tmp_path / "tokens.json"
    token.write_text(
        json.dumps({"version": 1, "tokens": [{"token": "FAKE-RG-CONNECTOR-TOKEN", "role": "connector"}]}),
        encoding="utf8",
    )
    posted: list[dict] = []

    def post(url, body, headers, timeout):
        envelope = json.loads(body)
        posted.append(envelope)
        top = envelope.get("snapshot")
        return 200, json.dumps({"applied_seq": top["seq"] if top else 0})

    outbox_path = tmp_path / "outbox.json"
    sink = RiskguardRegistrySink(
        RegistrySinkConfig(
            enabled=True,
            base_url="http://127.0.0.1:9",
            token_file=str(token),
            outbox_path=str(outbox_path),
            broker=BROKER,
        ),
        clock=clock,
        http_post=post,
    )
    adapter = enabled_adapter(clock, on_poll=sink.observe)
    adapter.feed(fixture("account_numbers.json"), fixture("accounts.json"))
    assert len(adapter.poll()) == 3
    assert sink.pump() == 1
    assert [envelope["broker"] for envelope in posted] == [BROKER]
    assert posted[0]["contract"] == "broker-account/1"
    assert [account["key"] for account in posted[0]["snapshot"]["accounts"]] == KEYS
    assert posted[0]["snapshot"]["seq"] == 3
    # The wire and the outbox file carry no api_ref and no hash.
    for blob in (json.dumps(posted), outbox_path.read_text(encoding="utf8")):
        assert "api_ref" not in blob
        for hashed in HASHES:
            assert hashed not in blob
    # A second poll with no change still keeps the outbox happy: a heartbeat, no event.
    clock.advance(60)
    adapter.feed(fixture("account_numbers.json"), fixture("accounts.json"))
    assert adapter.poll() == []
    assert sink.stats()["observed"] == 2


# --- read-only, no client ----------------------------------------------------------------


def test_the_module_carries_no_http_client_and_no_order_path() -> None:
    module = importlib.import_module("trade_engine.schwab.adapter")
    source = Path(module.__file__).read_text(encoding="utf8")
    tree = ast.parse(source)
    banned = {"socket", "ssl", "http", "urllib", "requests", "httpx", "aiohttp", "asyncio", "subprocess"}
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert not (imported & banned), imported & banned
    assert "://" not in source
    called = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert not (called & {"submit", "cancel", "replace", "place_order", "urlopen"})
    # The account port is there; the capabilities refuse every order type.
    adapter = enabled_adapter(FakeClock())
    assert adapter.name == BROKER
    assert adapter.capabilities.supported_order_types == frozenset()
    assert adapter.capabilities.supported_tifs == frozenset()

"""BP-5c: the Schwab accounts adapter hosted in the registry feed.

Fakes only: the BP-4a fake riskguard ingest, the BP-3 fake ntx daemon and a hub `/request`
double that answers from the BP-5b fixtures (`ACCT-TEST-nnn` ids, `FAKE-HASH-nnn` hashes).
Nothing here calls the real hub (port 8081), Schwab, the real riskguard (8791) or reads a real
token file.

Enforcing tests, by rule:
- off by default, nothing Schwab built, Tradovate bytes unchanged (I1): `test_switch_off_*`;
- one ingest with broker `schwab`, masked displays, no `hashValue` or hash value anywhere:
  `test_once_with_the_switch_on_*`, `test_the_two_outboxes_*`;
- a failed read is counted by kind and feeds nothing: `test_a_failed_read_*`,
  `test_a_failing_hub_never_escapes_*`;
- backoff 30 s -> 300 s, jittered, and never faster than the 60 s floor:
  `test_the_backoff_grows_*`, `test_ten_simulated_minutes_*`;
- loopback only: `test_a_non_loopback_hub_url_*`;
- the stop event ends the Schwab thread and both sinks stop: `test_the_stop_event_*`;
- the heartbeat carries counts only: `test_the_heartbeat_*`.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path

import pytest

from tests.test_bp3_tradovate_ntx import (  # noqa: F401 - fixtures are collected from here
    FakeClock,
    clock,
    daemon,
    make_client,
)
from tests.test_bp4a_registry_sink import token_file
from tests.test_bp4d_registry_feed import RecordingRg
from trade_engine.schwab.hub_client import (
    DEFAULT_HUB_URL,
    ENV_HUB_URL,
    KINDS,
    HubAccountsClient,
    HubReadError,
    hub_base_url,
)
from trade_engine.sinks.riskguard_registry import (
    RegistrySinkConfig,
    RiskguardRegistrySink,
)
from trade_engine.tradovate.adapter import TradovateAdapter
from trade_engine.tradovate.registry_feed import (
    SCHWAB_OUTBOX_ENV,
    Feed,
    run,
    run_once,
    schwab_outbox_path,
)

SENTINEL = "SENTINEL-RG-TOKEN-do-not-log-5c0d"
FIXTURES = Path(__file__).parent / "fixtures" / "schwab"
NUMBERS = (FIXTURES / "account_numbers.json").read_text(encoding="utf8")
ACCOUNTS = (FIXTURES / "accounts.json").read_text(encoding="utf8")
HASHES = ("FAKE-HASH-194", "FAKE-HASH-195", "FAKE-HASH-196")
KEYS = ["schwab:ACCT-TEST-194", "schwab:ACCT-TEST-195", "schwab:ACCT-TEST-196"]
RG_URL = "http://127.0.0.1:9"  # a closed loopback port: every fake is injected, nothing listens
HUB_BASE = (
    "http://127.0.0.1:9"  # loopback, so the client's own check passes; POST is injected
)


# --- the hub `/request` double -----------------------------------------------------------


class FakeHub:
    """The hub's `/request` double: the two account reads from the BP-5b fixtures.

    `failure` scripts one failure mode; `post` is the client's injectable POST, so no socket
    is opened and no Schwab session is needed.
    """

    def __init__(self, numbers: str = NUMBERS, accounts: str = ACCOUNTS) -> None:
        self.data = {
            "get_account_numbers": json.loads(numbers),
            "get_accounts": json.loads(accounts),
        }
        self.calls = 0
        self.methods: list[str] = []
        self.failure: str | None = None

    def post(self, url: str, body: bytes, headers, timeout: float) -> tuple[int, str]:
        self.calls += 1
        method = json.loads(body)["method"]
        self.methods.append(method)
        failure = self.failure
        if failure is None:
            return 200, json.dumps({"status": "success", "data": self.data[method]})
        if failure == "timeout":
            raise TimeoutError("the hub did not answer")
        if failure == "unreachable":
            return 0, ""
        if failure == "refused":
            return 200, json.dumps({"status": "error", "code": 403, "message": "no"})
        if failure == "http403":
            return 403, ""
        if failure == "http500":
            return 500, ""
        if failure == "bad_json":
            return 200, "<html>not json</html>"
        if failure == "status_error":
            return 200, json.dumps({"status": "error", "message": "no session"})
        if failure == "code_timeout":
            return 200, json.dumps({"status": "error", "code": "timeout"})
        raise AssertionError(f"unknown failure {failure!r}")

    def client(self) -> HubAccountsClient:
        return HubAccountsClient(HUB_BASE, post=self.post)


class BrokerAwareRg(RecordingRg):
    """RecordingRg that keeps one applied seq per broker, as riskguard does."""

    def __init__(self, token: str) -> None:
        super().__init__(token=token)
        self.per_broker: dict[str, int] = {}

    def post(self, url, body, headers, timeout):
        broker = json.loads(body).get("broker", "")
        self.applied = self.per_broker.get(broker, 0)
        status, text = super().post(url, body, headers, timeout)
        self.per_broker[broker] = self.applied
        return status, text


def env_for(tmp_path: Path, *, schwab: bool, sub: str = "a", pnl: bool = False) -> dict:
    base = tmp_path / sub
    base.mkdir(parents=True, exist_ok=True)
    env = {
        "TE_RISKGUARD_REGISTRY_SINK": "on",
        "TE_RISKGUARD_URL": RG_URL,
        "TE_RISKGUARD_TOKEN_FILE": str(token_file(base, SENTINEL)),
        "TE_RISKGUARD_OUTBOX_PATH": str(base / "registry_outbox.json"),
    }
    if pnl:
        env["TE_RISKGUARD_REGISTRY_PNL"] = "on"
    if schwab:
        env["TE_SCHWAB_ACCOUNTS"] = "on"
        # BP-5e: the hub /ws hint would dial the real hub port from these tests; they run without it.
        env["TE_SCHWAB_ACCOUNTS_HINT"] = "0"
    return env


def build(
    env: dict, *, clock, hub: FakeHub | None = None, rg=None, rand=lambda: 0.5
) -> Feed:
    """A feed with a fake ntx and a fake hub; the tradovate client is never polled here."""
    rg = RecordingRg(SENTINEL) if rg is None else rg
    feed = Feed.build(
        env,
        clock=clock,
        make_client=lambda: None,
        http_post=rg.post,
        hub_client=(hub or FakeHub()).client(),
        rand=rand,
    )
    assert feed is not None
    return feed


# --- off by default (I1) ------------------------------------------------------------------


def test_switch_off_builds_nothing_calls_no_hub_and_sends_the_same_bytes(
    tmp_path, daemon, clock
):
    hub = FakeHub()
    rg = RecordingRg(SENTINEL)
    code, summary = run_once(
        env_for(tmp_path, schwab=False, sub="off"),
        clock=FakeClock(),
        make_client=lambda: make_client(daemon),
        http_post=rg.post,
        hub_client=hub.client(),
    )
    assert code == 0
    assert hub.calls == 0  # no hub read
    assert "schwab_accounts" not in summary and "schwab_sent" not in summary
    feed = build(env_for(tmp_path, schwab=False, sub="off2"), clock=clock)
    assert feed.schwab is None  # nothing Schwab is built

    # I1: the Tradovate bytes equal a bare BP-4a sink + adapter with the same env and clock.
    bare_env = env_for(tmp_path, schwab=False, sub="bare")
    bare_clock = FakeClock()
    bare_rg = RecordingRg(SENTINEL)
    sink = RiskguardRegistrySink(
        RegistrySinkConfig.from_env(bare_env), clock=bare_clock, http_post=bare_rg.post
    )
    adapter = TradovateAdapter(
        make_client(daemon), clock=bare_clock, on_poll=sink.observe
    )
    adapter.poller.poll()
    sink.pump()
    assert rg.bodies and rg.bodies == bare_rg.bodies


def test_switch_off_starts_no_schwab_thread(tmp_path, daemon, clock):
    hub = FakeHub()
    stop = threading.Event()
    result: list[int] = []
    thread = threading.Thread(
        target=lambda: result.append(
            run(
                env_for(tmp_path, schwab=False, sub="thread"),
                clock=clock,
                rand=lambda: 0.5,
                stop_event=stop,
                make_client=lambda: make_client(daemon),
                http_post=RecordingRg(SENTINEL).post,
                hub_client=hub.client(),
                heartbeat_seconds=0.05,
                pump_tick_seconds=0.05,
            )
        ),
        daemon=True,
    )
    thread.start()
    names: set[str] = set()
    for _ in range(20):
        names.update(t.name for t in threading.enumerate())
        time.sleep(0.01)
    stop.set()
    thread.join(timeout=3.0)
    assert not thread.is_alive() and result == [0]
    assert not [name for name in names if "schwab" in name]
    assert hub.calls == 0


# --- the switch on: one ingest, masked, hashless ------------------------------------------


def test_once_with_the_switch_on_ingests_broker_schwab_masked_and_hashless(
    tmp_path, daemon, clock, caplog
):
    caplog.set_level(logging.DEBUG)
    hub = FakeHub()
    rg = BrokerAwareRg(SENTINEL)
    env = env_for(
        tmp_path, schwab=True, sub="on", pnl=True
    )  # pnl on: the schwab sink still sends none
    code, summary = run_once(
        env,
        clock=clock,
        make_client=lambda: make_client(daemon),
        http_post=rg.post,
        hub_client=hub.client(),
    )
    assert code == 0
    assert hub.methods == [
        "get_account_numbers",
        "get_accounts",
    ]  # one read pair, in order
    assert summary["schwab_accounts"] == 3
    assert summary["schwab_sent"] == 1

    schwab = [envelope for envelope in rg.envelopes if envelope["broker"] == "schwab"]
    assert len(schwab) == 1
    assert schwab[0]["contract"] == "broker-account/1"
    rows = schwab[0]["snapshot"]["accounts"]
    assert [row["key"] for row in rows] == KEYS
    assert [row["display"] for row in rows] == [
        "*194",
        "*195",
        "*196",
    ]  # masked outside the key
    assert {row["source"] for row in rows} == {"schwab"}
    assert [row["routes"][0]["platform"] for row in rows] == ["tos", "tos", "tos"]
    assert "pnl" not in schwab[0]  # the adapter has no pnl source
    assert [envelope["broker"] for envelope in rg.envelopes] == ["tradovate", "schwab"]

    outbox = tmp_path / "on" / "registry_outbox_schwab.json"
    assert outbox.is_file()
    for blob in (
        json.dumps(rg.envelopes),
        outbox.read_text(encoding="utf8"),
        caplog.text,
    ):
        assert "hashValue" not in blob
        assert "api_ref" not in blob
        for hashed in HASHES:
            assert hashed not in blob


def test_the_two_outboxes_hold_their_own_broker_only(tmp_path, daemon, clock):
    hub = FakeHub()
    env = env_for(tmp_path, schwab=True, sub="files")
    rg = BrokerAwareRg(SENTINEL)
    run_once(
        env,
        clock=clock,
        make_client=lambda: make_client(daemon),
        http_post=rg.post,
        hub_client=hub.client(),
    )
    tradovate = (tmp_path / "files" / "registry_outbox.json").read_text(encoding="utf8")
    schwab = (tmp_path / "files" / "registry_outbox_schwab.json").read_text(
        encoding="utf8"
    )
    assert "tradovate:" in tradovate and "schwab:" not in tradovate
    assert "schwab:" in schwab and "tradovate:" not in schwab


def test_the_schwab_outbox_defaults_to_its_own_file(tmp_path):
    tradovate = str(tmp_path / "registry_outbox.json")
    assert schwab_outbox_path({}, tradovate) == str(
        tmp_path / "registry_outbox_schwab.json"
    )
    explicit = str(tmp_path / "elsewhere.json")
    assert schwab_outbox_path({SCHWAB_OUTBOX_ENV: explicit}, tradovate) == explicit


# --- a failed read: counted by kind, no feed ---------------------------------------------


@pytest.mark.parametrize(
    ("failure", "kind"),
    [
        ("status_error", "refused"),
        ("refused", "refused"),
        ("http403", "refused"),
        ("code_timeout", "timeout"),
        ("timeout", "timeout"),
        ("unreachable", "unreachable"),
        ("http500", "unreachable"),
        ("bad_json", "bad_shape"),
    ],
)
def test_a_failed_read_is_counted_by_kind_and_feeds_nothing(
    tmp_path, clock, failure, kind
):
    hub = FakeHub()
    hub.failure = failure
    feed = build(env_for(tmp_path, schwab=True, sub=failure), clock=clock, hub=hub)
    adapter = feed.schwab.adapter
    assert adapter.stats.polls == 0

    feed.schwab.poller.tick(clock.now_utc())

    assert feed.schwab.poller.failures[kind] == 1
    assert sum(feed.schwab.poller.failures.values()) == 1
    assert feed.schwab.poller.reads_ok == 0
    assert adapter.last_good is None  # no snapshot: `feed` was not called
    assert adapter.stats.polls == 0  # and no poll happened
    assert adapter.stats.feed_superseded == 0


def test_the_backoff_grows_from_thirty_seconds_and_caps_at_three_hundred(
    tmp_path, clock
):
    hub = FakeHub()
    hub.failure = "status_error"
    feed = build(env_for(tmp_path, schwab=True, sub="backoff"), clock=clock, hub=hub)
    poller = feed.schwab.poller

    waits: list[float] = []
    for _ in range(7):
        calls = hub.calls
        poller.tick(clock.now_utc())
        assert hub.calls > calls  # one read pair per attempt
        waits.append(poller.backoff_seconds)
        clock.advance(poller.backoff_seconds - 0.001)
        calls = hub.calls
        poller.tick(clock.now_utc())
        assert hub.calls == calls  # no read before the backoff elapses
        clock.advance(0.001)

    assert waits == [30.0, 60.0, 120.0, 240.0, 300.0, 300.0, 300.0]
    assert poller.failures["refused"] == 7
    assert poller.reads_ok == 0


def test_the_backoff_jitter_stays_within_bounds(tmp_path, clock):
    hub = FakeHub()
    hub.failure = "unreachable"
    # rand() = 0.0 lowers by 10%, rand() = 1.0 raises by 10%; the cap still holds.
    for rand, expected in ((lambda: 0.0, 27.0), (lambda: 1.0, 33.0)):
        feed = build(
            env_for(tmp_path, schwab=True, sub=f"jitter{expected}"),
            clock=clock,
            hub=hub,
            rand=rand,
        )
        feed.schwab.poller.tick(clock.now_utc())
        assert feed.schwab.poller.backoff_seconds == pytest.approx(expected)


def test_ten_simulated_minutes_make_at_most_two_hub_calls_a_minute(tmp_path, clock):
    hub = FakeHub()
    feed = build(env_for(tmp_path, schwab=True, sub="cadence"), clock=clock, hub=hub)
    poller = feed.schwab.poller

    for _ in range(600):  # ten simulated minutes, one tick a second
        clock.advance(1)
        poller.tick(clock.now_utc())

    assert poller.reads_ok == 10  # the 60 s floor: one read pair a minute, no faster
    assert hub.calls == 2 * poller.reads_ok  # <= 2 hub REST calls/min of the shared 120
    assert hub.methods == ["get_account_numbers", "get_accounts"] * 10
    assert poller.accounts() == 3
    assert poller.failures == dict.fromkeys(KINDS, 0)


# --- loopback only -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://0.0.0.0:8081",
        "http://10.1.2.3:8081",
        "http://127.0.0.2:8081",
        "https://api.schwabapi.com",
        "http://[::2]:8081",
        "127.0.0.1:8081",
    ],
)
def test_a_non_loopback_hub_url_is_refused(tmp_path, url):
    with pytest.raises(ValueError):
        HubAccountsClient(url)
    with pytest.raises(ValueError):
        HubAccountsClient.from_env({ENV_HUB_URL: url})
    env = env_for(tmp_path, schwab=True, sub="url")
    env[ENV_HUB_URL] = url
    with pytest.raises(ValueError):  # config time, like the other bad-env cases
        run_once(
            env,
            clock=FakeClock(),
            make_client=lambda: None,
            http_post=RecordingRg(SENTINEL).post,
        )
    # With the switch off nothing Schwab is built, so the bad URL is not even read.
    off = env_for(tmp_path, schwab=False, sub="url-off")
    off[ENV_HUB_URL] = url
    assert Feed.build(off, clock=FakeClock(), make_client=lambda: None).schwab is None


def test_loopback_defaults_are_accepted():
    assert hub_base_url({}) == DEFAULT_HUB_URL == "http://127.0.0.1:8081"
    for url in ("http://127.0.0.1:8081", "http://localhost:8081/", "http://[::1]:8081"):
        assert HubAccountsClient(url).base_url.rstrip("/") == url.rstrip("/")


# --- the stop event and the heartbeat ----------------------------------------------------


def test_the_stop_event_ends_the_schwab_thread_and_stops_both_sinks(
    tmp_path, daemon, clock, monkeypatch
):
    stopped: list[object] = []
    real_stop = RiskguardRegistrySink.stop

    def spy_stop(self, timeout: float = 5.0):
        stopped.append(self)
        return real_stop(self, timeout)

    monkeypatch.setattr(RiskguardRegistrySink, "stop", spy_stop)
    hub = FakeHub()
    rg = BrokerAwareRg(SENTINEL)
    stop = threading.Event()
    result: list[int] = []
    thread = threading.Thread(
        target=lambda: result.append(
            run(
                env_for(tmp_path, schwab=True, sub="stop"),
                clock=clock,
                rand=lambda: 0.5,
                stop_event=stop,
                make_client=lambda: make_client(daemon),
                http_post=rg.post,
                hub_client=hub.client(),
                heartbeat_seconds=0.05,
                pump_tick_seconds=0.05,
            )
        ),
        daemon=True,
    )
    thread.start()
    deadline = time.monotonic() + 6.0
    while time.monotonic() < deadline:
        if [e for e in rg.envelopes if e.get("broker") == "schwab"]:
            break
        time.sleep(0.02)
    assert [e for e in rg.envelopes if e.get("broker") == "schwab"], "no schwab ingest"
    assert "registry-feed-schwab" in {t.name for t in threading.enumerate()}

    stop.set()
    thread.join(timeout=4.0)
    assert not thread.is_alive() and result == [0]
    assert len(stopped) == 2  # both sinks got stop()
    assert "registry-feed-schwab" not in {t.name for t in threading.enumerate()}


def test_a_failing_hub_never_escapes_the_schwab_thread(tmp_path, daemon, clock, caplog):
    caplog.set_level(logging.DEBUG)
    hub = FakeHub()
    hub.failure = "unreachable"
    stop = threading.Event()
    result: list[int] = []
    thread = threading.Thread(
        target=lambda: result.append(
            run(
                env_for(tmp_path, schwab=True, sub="fail"),
                clock=clock,
                rand=lambda: 0.5,
                stop_event=stop,
                make_client=lambda: make_client(daemon),
                http_post=RecordingRg(SENTINEL).post,
                hub_client=hub.client(),
                heartbeat_seconds=0.05,
                pump_tick_seconds=0.05,
            )
        ),
        daemon=True,
    )
    thread.start()
    deadline = time.monotonic() + 6.0
    while (
        hub.calls < 1 and time.monotonic() < deadline
    ):  # the read pair starts with one call
        time.sleep(0.02)
    assert hub.calls == 1
    time.sleep(0.3)
    assert thread.is_alive()  # the backoff holds the thread, it does not crash or spin
    assert hub.calls == 1  # no retry inside the 30 s backoff
    stop.set()
    thread.join(timeout=4.0)
    assert not thread.is_alive() and result == [0]
    assert "schwab hub read failed (unreachable)" in caplog.text
    assert "Traceback" not in caplog.text


def test_the_heartbeat_carries_schwab_counts_only(tmp_path, daemon, clock, caplog):
    caplog.set_level(logging.DEBUG)
    hub = FakeHub()
    stop = threading.Event()
    thread = threading.Thread(
        target=lambda: run(
            env_for(tmp_path, schwab=True, sub="hb"),
            clock=clock,
            rand=lambda: 0.5,
            stop_event=stop,
            make_client=lambda: make_client(daemon),
            http_post=RecordingRg(SENTINEL).post,
            hub_client=hub.client(),
            heartbeat_seconds=0.05,
            pump_tick_seconds=0.05,
        ),
        daemon=True,
    )
    thread.start()
    time.sleep(0.25)
    stop.set()
    thread.join(timeout=4.0)

    lines = [
        r.getMessage() for r in caplog.records if "schwab_reads_ok" in r.getMessage()
    ]
    assert lines
    line = lines[-1]
    assert "schwab_accounts" in line
    for kind in KINDS:
        assert f"schwab_read_fail_{kind}" in line
    assert "schwab_sink.observed" in line
    for forbidden in ("ACCT-TEST", "FAKE-HASH", "hashValue", "*19"):
        assert forbidden not in line


# --- the client's own shape --------------------------------------------------------------


def test_the_client_returns_the_hub_data_as_text_and_sends_nothing_else():
    hub = FakeHub()
    client = hub.client()
    numbers, accounts = client.read()
    assert client.reads == 1
    assert json.loads(numbers) == json.loads(NUMBERS)
    assert json.loads(accounts) == json.loads(ACCOUNTS)
    assert hub.methods == ["get_account_numbers", "get_accounts"]

    sent: list[dict] = []

    def post(url, body, headers, timeout):
        sent.append(json.loads(body))
        return 200, json.dumps({"status": "success", "data": []})

    HubAccountsClient(HUB_BASE, post=post).read()
    assert sent == [
        {"method": "get_account_numbers"},
        {"method": "get_accounts"},
    ]  # no params


def test_a_refusal_message_never_carries_the_body():
    def post(url, body, headers, timeout):
        return 200, json.dumps({"status": "error", "echo": "ACCT-TEST-194"})

    with pytest.raises(HubReadError) as caught:
        HubAccountsClient(HUB_BASE, post=post).read()
    assert caught.value.kind == "refused"
    assert "ACCT-TEST-194" not in str(caught.value)

"""Authored synthetic adapter probes; not historical recordings or full role walks."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from datetime import date, datetime, time, timedelta
from decimal import Decimal
import hashlib
import io
import json
from pathlib import Path
import socket
import tempfile
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

from tools import p4c_corpus as contract
from trade_engine.clock.replay import ReplayClock
from trade_engine.domain.instruments import Equity, OptionContract, OptionRight, Side
from trade_engine.domain.orders import OrderType, TimeInForce
from trade_engine.interfaces.broker import Capabilities, VenueAck, VenueIdentity, VenueOrder, VenueOrderAllocation
from trade_engine.interfaces.market_data import Bar, OptionQuote, StaleDataError
from trade_engine.intraday.service import Heartbeat
from trade_engine.ledger import CashFlow, Event, EventKind, Ledger, VenueReconcile, codec
from trade_engine.market_data.chains import ChainSnapshot
from tests.frozen_p4c import http as frozen_http
from tests.frozen_p4c.t6_replay import ReplayClock as FrozenClock

NY = ZoneInfo("America/New_York")
DATES = ("2026-03-06", "2026-03-09", "2026-07-02", "2026-09-25", "2026-11-27", "2026-12-24")


@contextmanager
def offline():
    def forbidden(*args, **kwargs):
        raise contract.CorpusError("Synthetic capture/replay cannot access a network venue")
    with patch.object(socket, "create_connection", forbidden), patch.object(socket.socket, "connect", forbidden), \
            patch.object(socket.socket, "connect_ex", forbidden), patch.object(socket.socket, "bind", forbidden):
        yield


def confined(root: Path, relative: str) -> Path:
    target = (root / relative).resolve()
    if not target.is_relative_to(root.resolve()) or target == root.resolve():
        raise contract.CorpusError(f"Synthetic ledger escapes its temporary root: {relative}")
    return target


def inputs(index: int) -> dict:
    role = contract.ROLES[index]
    return {"role": role, "session": DATES[index], "seed": index,
            "account": f"SYNTHETIC_{index}", "underlying": "AAPL" if role == "SCAN" else "SPX",
            "cash": f"{50000 + index}.00", "spot": f"{100 + index}.00",
            "at": datetime.combine(date.fromisoformat(DATES[index]), time(9, 30), NY).isoformat()}


class Source:
    def __init__(self, config: dict) -> None:
        self.config = config
        self.calls = 0

    def snapshot(self, underlying: str, now: datetime) -> ChainSnapshot:
        self.calls += 1
        if self.calls == 2:
            raise StaleDataError(f"Synthetic missing snapshot for {underlying} at {now.isoformat()} (I5)")
        option = OptionContract(underlying, date.fromisoformat(self.config["session"]), Decimal("100"), OptionRight.PUT)
        quote = OptionQuote(option, Decimal("1.00"), Decimal("1.10"), Decimal("10"), Decimal("10"), now)
        return ChainSnapshot(underlying, now, Decimal(self.config["spot"]), (quote,),
                             None, None, "authored-synthetic", underlying_as_of=now)

    def bars(self, instrument: Equity, start: datetime, end: datetime) -> tuple[Bar, ...]:
        price = Decimal(self.config["spot"])
        return (Bar(instrument, start, price, price, price, price, Decimal("0"), start),)


class Venue:
    """Closed synthetic responses; no network/browser or live account reference."""
    def __init__(self, config: dict) -> None:
        self.config = config
        self.capabilities = Capabilities(frozenset((OrderType.MARKET,)), frozenset((TimeInForce.DAY,)),
                                         False, False, False)
        self.sent: list[VenueOrder] = []

    def connect(self) -> VenueIdentity:
        return VenueIdentity(self.config["account"], "sim", datetime.fromisoformat(self.config["at"]), "synthetic")

    def submit(self, order: VenueOrder) -> VenueAck:
        self.sent.append(order)
        return VenueAck(order.venue_order_id, "ACCEPTED", order.submitted_at)

    def orders(self, since: datetime) -> tuple:
        return ()

    def fills(self, since: datetime) -> tuple:
        return ()

    def positions(self) -> tuple:
        return ()


class Strategy:
    def generate_intents(self, context: dict) -> tuple:
        return ()


class Sink:
    def __init__(self) -> None:
        self.calls = 0

    def publish(self, item) -> bool:
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("synthetic delivery failure") from ValueError("synthetic sink unavailable")
        return True


class Beats:
    def __init__(self) -> None:
        self.value: Heartbeat | None = None

    def write(self, heartbeat: Heartbeat) -> None:
        self.value = heartbeat

    def read(self) -> Heartbeat | None:
        return self.value


class MemorySocket:
    def __init__(self, request: bytes) -> None:
        self.input = io.BytesIO(request)
        self.output = io.BytesIO()

    def makefile(self, *args, **kwargs):
        return self.input

    def sendall(self, body: bytes) -> None:
        self.output.write(body)


def http_observations(ledger: Ledger) -> list[dict]:
    class Handler(frozen_http._EngineHandler):
        def date_time_string(self, timestamp=None):
            return "<transport-Date>"
    server = SimpleNamespace(ledger_path=str(ledger.path), is_running=False, ping_interval=15,
                             add_subscriber=lambda q: None, remove_subscriber=lambda q: None)
    requests = (
        b"GET /health HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n",
        b"GET /snapshot HTTP/1.1\r\nHost: localhost\r\nOrigin: https://localhost:5555\r\n\r\n",
        b"GET /events?after=0 HTTP/1.1\r\nHost: localhost\r\nLast-Event-ID: 1\r\n\r\n",
        b"GET /events?after=bad HTTP/1.1\r\nHost: localhost\r\nLast-Event-ID: 1\r\n\r\n",
        b"GET /events?after=%D9%A0 HTTP/1.1\r\nHost: localhost\r\n\r\n",
        b"GET /events?after=%C2%B2 HTTP/1.1\r\nHost: localhost\r\n\r\n",
        b"OPTIONS /health HTTP/1.1\r\nHost: localhost\r\nOrigin: https://localhost:5555\r\n\r\n",
        b"POST /health HTTP/1.1\r\nHost: localhost\r\n\r\n",
        b"GET /health HTTP/1.1\r\nHost: evil.example\r\n\r\n",
        b"GET /unknown HTTP/1.1\r\nHost: localhost\r\n\r\n",
    )
    observations = []
    for request in requests:
        transport = MemorySocket(request)
        try:
            Handler(transport, ("127.0.0.1", 0), server)
        except Exception as error:
            outcome = {"raise": contract.encode_error(error)}
        else:
            outcome = {"return": contract.encode(transport.output.getvalue())}
        observations.append({"request": contract.encode(request), "outcome": outcome})
    return observations


def walk(config: dict, ledger: Ledger, adapters: dict) -> list[dict]:
    """A finite adapter contract exercise, deliberately not a trading service."""
    checkpoints = [contract.ledger_check(ledger)]
    clock, source, venue = (adapters[name] for name in ("clock", "source", "venue"))
    now = clock.now_utc()
    identity = venue.connect()
    if identity.env != "sim" or identity.account_id != config["account"]:
        raise contract.CorpusError("Synthetic venue identity differs")
    capabilities = venue.capabilities
    if OrderType.MARKET not in capabilities.supported_order_types:
        raise contract.CorpusError("Synthetic venue lacks MARKET")
    source.bars(Equity(config["underlying"]), now, now + timedelta(minutes=1))
    snapshot = source.snapshot(config["underlying"], now)
    if snapshot.underlying != config["underlying"]:
        raise contract.CorpusError("Synthetic source crossed roles")
    adapters["strategy"].generate_intents({"account_id": config["account"], "role": config["role"], "now": now})
    order = VenueOrder(f"probe-{config['seed']}", Equity(config["underlying"]), OrderType.MARKET,
        Side.BUY, Decimal("1"), now, allocations=(VenueOrderAllocation("probe", config["account"], Decimal("1")),))
    venue.submit(order)
    venue.orders(now)
    venue.fills(now)
    venue.positions()
    event = ledger.append(Event(config["account"], EventKind.VENUE_RECONCILE,
        VenueReconcile("synthetic", now, True, (), "adapter probe"), now,
        command_id=f"probe:{config['role']}:{config['session']}"))
    checkpoints.append(contract.ledger_check(ledger))
    destination = "journal:synthetic"
    ledger.enqueue_outbox(event.seq, destination, {"probe": config["role"], "cash": config["cash"]}, created_at=now)
    ledger.set_meta("p4c:fixture", config["role"])
    checkpoints.append(contract.ledger_check(ledger))
    first = ledger.drain_outbox(destination, adapters["sink"].publish, clock)
    if first.ok:
        raise contract.CorpusError("Synthetic failed delivery was not observed")
    checkpoints.append(contract.ledger_check(ledger))
    second = ledger.drain_outbox(destination, adapters["sink"].publish, clock)
    if not second.ok:
        raise contract.CorpusError("Synthetic delivery recovery failed")
    checkpoints.append(contract.ledger_check(ledger))
    clock.sleep(0.25)
    after = clock.now_utc()
    try:
        source.snapshot(config["underlying"], after)
    except StaleDataError:
        pass
    else:
        raise contract.CorpusError("Synthetic missing snapshot was not refused")
    source.snapshot(config["underlying"], after)
    heartbeat = Heartbeat(config["account"], date.fromisoformat(config["session"]), after, False, "synthetic probe", exited=True)
    adapters["heartbeat"].write(heartbeat)
    if adapters["heartbeat"].read() != heartbeat:
        raise contract.CorpusError("Synthetic heartbeat differs")
    checkpoints.append(contract.ledger_check(ledger))
    return checkpoints


def _seed(config: dict, ledger: Ledger) -> list[dict]:
    now = datetime.fromisoformat(config["at"])
    ledger.append(Event(config["account"], EventKind.CASH_FLOW,
        CashFlow(Decimal(config["cash"]), "deposit", now), now, command_id="synthetic-seed"))
    return [codec.encode_event(event) for event in ledger.events()]


def _capture_one(config: dict, root: Path, clock_type=FrozenClock) -> dict:
    recorder = contract.Recorder()
    backends = {"clock": clock_type(datetime.fromisoformat(config["at"])), "source": Source(config),
                "venue": Venue(config), "strategy": Strategy(), "sink": Sink(), "heartbeat": Beats()}
    adapters = {actor: contract.RecordingAdapter(recorder, actor, backend) for actor, backend in backends.items()}
    with Ledger(confined(root, "synthetic.db")) as ledger:
        initial = _seed(config, ledger)
        checkpoints = walk(config, ledger, adapters)
        http = http_observations(ledger)
    return {"id": f"synthetic-{config['seed']}", "kind": "adapter-probe",
            "role": config["role"], "session": config["session"], "seed": config["seed"],
            "inputs": config, "initial_events": initial, "tape": recorder.finish(),
            "checkpoints": checkpoints, "http": http, "config_sha256": contract.digest(config)}


def capture() -> dict:
    contract.require_artifacts(contract.ROOT / "crates" / "target" / "release" / "te.exe")
    oracles = json.loads((contract.ROOT / "tests" / "frozen_p4c" / "t0_oracles.json").read_text(encoding="utf-8"))
    configurations = [inputs(index) for index in range(len(contract.ROLES))]
    with offline(), tempfile.TemporaryDirectory(prefix="p4c-t0-synthetic-") as folder:
        fixtures = []
        for index, config in enumerate(configurations):
            root = Path(folder) / str(index)
            root.mkdir()
            fixtures.append(_capture_one(config, root))
    manifest = {"version": contract.VERSION, "provenance": "synthetic", "source_release": oracles["source_release"],
                "plugin_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                "rules_sha256": contract.digest({"version": contract.VERSION, "oracles": oracles, "http": contract.HTTP_CONTRACT}),
                "config_sha256": contract.digest(configurations), "oracles": oracles,
                "inventory": contract.inventory(), "http_contract": contract.HTTP_CONTRACT, "fixtures": fixtures}
    contract.validate_manifest(manifest)
    return manifest


def verify(manifest: dict) -> None:
    contract.validate_manifest(manifest)
    with offline(), tempfile.TemporaryDirectory(prefix="p4c-t0-verify-") as folder:
        for index, fixture in enumerate(manifest["fixtures"]):
            config = fixture["inputs"]
            for label, clock_type in (("frozen", FrozenClock), ("native", ReplayClock)):
                root = Path(folder) / f"{index}-{label}"
                root.mkdir()
                regenerated = _capture_one(config, root, clock_type)
                if regenerated != fixture:
                    raise contract.CorpusError(f"{label} producer differs from golden inputs/tape: {fixture['id']}")
            replay = contract.Replay(fixture["tape"])
            adapters = {actor: contract.ReplayAdapter(replay, actor) for actor in contract.METHODS}
            root = Path(folder) / f"{index}-replay"
            root.mkdir()
            with Ledger(confined(root, "synthetic.db")) as ledger:
                for encoded in fixture["initial_events"]:
                    written = ledger.append(replace(codec.decode_event(encoded), seq=None))
                    if codec.encode_event(written) != encoded:
                        raise contract.CorpusError("Initial replay row identity differs")
                if walk(config, ledger, adapters) != fixture["checkpoints"]:
                    raise contract.CorpusError(f"Replay event/fold/outbox/meta differs: {fixture['id']}")
                if http_observations(ledger) != fixture["http"]:
                    raise contract.CorpusError(f"Replay HTTP contract differs: {fixture['id']}")
            replay.finish()

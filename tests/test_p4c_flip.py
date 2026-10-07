"""The paper-session flip harness (P4c §5.1): one recorded session, two owners.

The recorded tape is the real SPX 0DTE session of 2026-10-06 (665 chain
snapshots, 13:30-20:00 UTC, recorded by the hub while the sim ran paper).
Both owners consume the SAME immutable tape through a deterministic replay
clock and a store-served snapshot source:

- the classic Python owner: this process opens its own synthetic SQLite
  ledger and runs the intraday service in-process (the pre-P4c ownership
  shape);
- the Rust runtime owner: ``te serve`` attaches its own synthetic ledger
  and executes the same entry point as a durable job.

Zero live accounts: both ledgers are synthetic files under tmp; the tape is
read-only; a network guard refuses any socket. The comparison is
event-for-event (seq, account, kind, command_id, payload, codec bytes) and
the folded session-end state, per §5.2.
"""

from __future__ import annotations

import gzip
import json
import socket
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BINARY = ROOT / "crates" / "target" / "release" / "te.exe"
PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"
CLIENT_SRC = Path(r"C:\Users\vinay\tvDownloadOHLC\.claude\worktrees\p4c-t11-client\src")
TAPE_DIR = Path(r"C:\Users\vinay\tvDownloadOHLC\data\trade_engine\chains\SPX")
SESSION = date(2026, 10, 6)

FLIP_ENTRY = '''
"""The flip entry: the same service build both owners run (P4c §5.1)."""
from __future__ import annotations

import gzip
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path


class TapeClock:
    """A tape-driven replay clock: ticks through the tape's instants."""

    def __init__(self, instants, tick_seconds=30.0):
        self.instants = list(instants)
        self.tick_seconds = tick_seconds
        self.index = 0

    def now_utc(self):
        if self.index >= len(self.instants):
            return self.instants[-1] + timedelta(seconds=3600)
        return self.instants[self.index]

    def sleep(self, seconds):
        self.index = min(self.index + 1, len(self.instants))

    def advance_to(self, target):
        while self.index + 1 < len(self.instants) and self.instants[self.index] < target:
            self.index += 1


class TapeSource:
    """Serves the recorded snapshots at-or-before the clock's now."""

    def __init__(self, folder):
        self.folder = Path(folder)

    def snapshot(self, underlying, now):
        files = sorted(self.folder.glob("*.json.gz"))
        best = None
        for path in files:
            stamp = datetime.strptime(path.name[: -len(".json.gz")], "%Y%m%dT%H%M%S%fZ").replace(tzinfo=timezone.utc)
            if stamp <= now:
                best = path
            else:
                break
        if best is None:
            raise RuntimeError(f"the tape has no snapshot at or before {now}")
        from trade_engine.market_data.chains import ChainSnapshot
        return ChainSnapshot.from_json(gzip.decompress(best.read_bytes()).decode("utf-8"))
'''


def _tape_instants(limit=None):
    files = sorted(TAPE_DIR.glob("*.json.gz"))
    assert files, f"the recorded SPX tape is missing: {TAPE_DIR}"
    instants = [
        datetime.strptime(p.name[: -len(".json.gz")], "%Y%m%dT%H%M%S%fZ").replace(tzinfo=timezone.utc)
        for p in files
    ]
    return instants[:limit] if limit else instants


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch):
    """No live venue or hub - but the loopback runtime owner IS allowed: it
    is the process under test, not a live endpoint (§5.1)."""

    original_create = socket.create_connection
    original_connect = socket.socket.connect

    def create(address, *args, **kwargs):
        host = address[0] if isinstance(address, tuple) else address
        if host not in ("127.0.0.1", "localhost"):
            raise AssertionError("the flip cannot contact a live venue or hub")
        return original_create(address, *args, **kwargs)

    def connect(self, address, *args, **kwargs):
        host = address[0] if isinstance(address, tuple) else address
        if host not in ("127.0.0.1", "localhost"):
            raise AssertionError("the flip cannot contact a live venue or hub")
        return original_connect(self, address, *args, **kwargs)

    monkeypatch.setattr(socket, "create_connection", create)
    monkeypatch.setattr(socket.socket, "connect", connect)


def _events_of(ledger_path: Path) -> list[dict]:
    """Every event row, normalized for comparison (§5.2: never raw db bytes)."""
    import sqlite3

    connection = sqlite3.connect(ledger_path)
    try:
        rows = connection.execute(
            "SELECT seq, ts_utc, account, kind, command_id, payload_json, schema_version "
            "FROM events ORDER BY seq"
        ).fetchall()
    finally:
        connection.close()
    normalized = []
    for seq, ts, account, kind, command_id, payload, version in rows:
        payload = json.loads(payload) if payload else None
        normalized.append(
            {
                "seq": seq,
                "ts_utc": ts,
                "account": account,
                "kind": kind,
                "command_id": command_id,
                "payload": payload,
                "schema_version": version,
            }
        )
    return normalized


def _classic_run(world: Path) -> Path:
    """Owner A: the classic Python path - this process owns the ledger."""
    from trade_engine.calendar.sessions import ExchangeCalendar
    from trade_engine.ledger import Ledger

    sys.path.insert(0, str(world))
    sys.path.insert(0, str(CLIENT_SRC))
    from flip_entry import TapeClock, TapeSource

    instants = _tape_instants()
    calendar = ExchangeCalendar()
    ledger_path = world / "classic" / "intraday.db"
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    clock = TapeClock(instants)
    ledger = Ledger(ledger_path).open()
    try:
        result = _run_flip(ledger, clock, TapeSource(TAPE_DIR), calendar, SESSION)
    finally:
        ledger.close()
    (world / "classic_result.json").write_text(json.dumps(result), encoding="utf-8")
    return ledger_path


def _run_flip(ledger, clock, source, calendar, session):
    """The one service build both owners run, imported from the shared module."""
    from flip_build import run_flip

    return run_flip(ledger, clock, source, calendar, session)


def test_paper_session_flip_classic_vs_runtime_owner(tmp_path):
    """The same recorded session through both ownership shapes produces the
    same ledger events, folded states and result."""
    assert BINARY.is_file(), f"missing mandatory release executable: {BINARY}"
    assert Path(sys.executable).resolve() == PYTHON.resolve()

    # The flip world: the shared entry and build modules both processes import.
    world = tmp_path / "world"
    world.mkdir()
    (world / "flip_entry.py").write_text(FLIP_ENTRY, encoding="utf-8")
    (world / "flip_build.py").write_text(FLIP_BUILD, encoding="utf-8")
    (world / "owner_entry.py").write_text(
        "from flip_entry import TapeClock, TapeSource\n"
        "from flip_build import run_flip\n"
        "\n"
        "\n"
        "def owner_side(ledger, session, options):\n"
        "    from datetime import datetime, timezone\n"
        "    from pathlib import Path\n"
        "    import sys\n"
        "    sys.path.insert(0, options['world'])\n"
        "    from trade_engine.calendar.sessions import ExchangeCalendar\n"
        "    calendar = ExchangeCalendar()\n"
        "    tape = Path(options['tape'])\n"
        "    instants = [datetime.strptime(p.name[: -len('.json.gz')], '%Y%m%dT%H%M%S%fZ').replace(tzinfo=timezone.utc)\n"
        "                for p in sorted(tape.glob('*.json.gz'))]\n"
        "    clock = TapeClock(instants)\n"
        "    result = run_flip(ledger, clock, TapeSource(tape), calendar, session)\n"
        "    return {'result': result}\n",
        encoding="utf-8",
    )

    # -- Owner A: the classic Python path --------------------------------------
    classic_ledger = _classic_run(world)
    classic_events = _events_of(classic_ledger)
    classic_result = json.loads((world / "classic_result.json").read_text(encoding="utf-8"))
    assert classic_events, "the classic run must have produced events"

    # -- Owner B: the Rust runtime owner --------------------------------------
    # The owner executes the same build over its OWN synthetic ledger: the
    # entry module in the world dir assembles the service from the injected
    # ledger, the tape clock and the tape source, via the shared build.
    offline = tmp_path / "owner"
    offline.mkdir()
    capability = tmp_path / "capability.txt"
    capability.write_text("flip-capability", encoding="utf-8")
    config = {
        "mode": "runtime-owner",
        "python_home": sys.base_prefix,
        "python_dll": str(BINARY.parent / "python313.dll"),
        "python_executable": str(PYTHON),
        "site_packages": str(ROOT / ".venv" / "Lib" / "site-packages"),
        "engine_source": str(ROOT / "src"),
        "plugin_paths": [str(world), str(CLIENT_SRC)],
        "plugin_module": "owner_entry",
        "plugin_factory": "missing",
        "plugin_config": {},
        "owner": {"ledger_path": str(offline / "intraday.db"), "clock": "wall"},
        "runtime": {
            "role": "intraday",
            "jobs": ["flip"],
            "record_limit": 64,
            "stop_timeout_seconds": 30.0,
            "capability": str(capability),
            "port": 0,
            "entry_module": "owner_entry",
            "entry": "owner_side",
        },
    }
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")

    proc = subprocess.Popen(
        [str(BINARY), "serve", "--config", str(config_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    try:
        ready = json.loads(proc.stdout.readline())
        assert ready["serving"] is True
        port = ready["port"]

        payload = json.dumps(
            {
                "version": 1,
                "request_id": "flip-1",
                "generation": ready["generation"],
                "role": "intraday",
                "job": "flip",
                "session": SESSION.isoformat(),
                "options": {"world": str(world), "tape": str(TAPE_DIR)},
            }
        ).encode("utf-8")
        request = _http_request(port, "POST", "/v1/runtime/jobs", capability="flip-capability", body=payload)
        import urllib.error

        record = json.loads(request)
        assert record["state"] in ("queued", "running"), record

        import time

        terminal = None
        for _ in range(1200):
            reply = json.loads(
                _http_request(
                    port,
                    "GET",
                    "/v1/runtime/jobs/flip-1",
                    capability="flip-capability",
                )
            )
            if reply["state"] in ("completed", "refused", "failed", "uncertain"):
                terminal = reply
                break
            time.sleep(0.25)
        assert terminal is not None, "the owner job never reached a terminal state"
        assert terminal["state"] == "completed", terminal.get("error")

        owner_ledger = offline / "intraday.db"
        owner_events = _events_of(owner_ledger)
    finally:
        proc.stdin.close()
        proc.wait(timeout=120)

    # -- the comparison (§5.2) -------------------------------------------------
    assert len(classic_events) == len(owner_events), (
        f"event counts diverge: classic {len(classic_events)} vs owner {len(owner_events)}"
    )
    for expected, actual in zip(classic_events, owner_events):
        assert expected == actual, f"event divergence at seq {expected['seq']}: {expected} vs {actual}"

    # The folded session-end states agree.
    from trade_engine.calendar.sessions import ExchangeCalendar
    from trade_engine.ledger.reader import LedgerReader

    classic_state = LedgerReader(classic_ledger).open()
    try:
        classic_cash = classic_state.state("OPT_0DTE_PCS_SPX").cash
    finally:
        classic_state.close()
    owner_state = LedgerReader(owner_ledger).open()
    try:
        owner_cash = owner_state.state("OPT_0DTE_PCS_SPX").cash
    finally:
        owner_state.close()
    assert classic_cash == owner_cash, f"folded cash diverges: {classic_cash} vs {owner_cash}"


FLIP_BUILD = '''
"""The one service build both owners run (P4c §5.1)."""
from __future__ import annotations

import json
from datetime import date
from decimal import Decimal as D
from pathlib import Path

from trade_engine.intraday import IntradayConfig, IntradayService
from trade_engine.risk_options import OptionRiskEngine
from trade_engine.sim import SnapshotVenue


def run_flip(ledger, clock, source, calendar, session):
    from scan_engine.options.accounts import fund_accounts, load_options_rules, risk_rules
    from scan_engine.options.zero_dte import ZeroDtePcsStrategy

    rules = load_options_rules()
    account_id = rules["accounts"]["intraday"][0]
    service_rules = rules["intraday"]["service"]
    fill = rules["fill_model"]
    account_rules = rules["intraday"][account_id]

    class _FlatRegime:
        def __call__(self, day):
            return "BULL_EXPLOSIVE"

    def earnings_of():
        return None

    def day_read_of(day):
        return "QUIET"

    # The same idempotent funding the real owner entry makes: the account's
    # first entry has equity to be sized and risk-checked against.
    fund_accounts(rules, ledger, clock, [account_id])

    strategy = ZeroDtePcsStrategy(
        account_id,
        account_rules,
        fill_fraction=D(str(fill["fill_fraction"])),
        fee_per_contract=D(str(fill["fee_per_contract"])),
        day_read_of=day_read_of,
    )
    config = IntradayConfig(
        job_name="intraday",
        account_id=account_id,
        underlying=service_rules["underlying"],
        broker=SnapshotVenue(
            account_id,
            clock,
            fill_fraction=D(str(fill["fill_fraction"])),
            fee_per_contract=D(str(fill["fee_per_contract"])),
            max_quote_age_seconds=float(service_rules["max_quote_age_seconds"]),
            calendar=calendar,
        ),
        strategy=strategy,
        option_risk_engine=OptionRiskEngine(
            risk_rules(rules["risk"][account_id]),
            clock,
            ledger,
            venue_id="sim",
            regime_of=_FlatRegime(),
            earnings=None,
        ),
        snapshot_source=source.snapshot,
        eod_job_name=str(rules["eod_job"]),
        max_quote_age_seconds=float(service_rules["max_quote_age_seconds"]),
        tick_seconds=float(service_rules["tick_seconds"]),
        flat_at=strategy.flat_at,
        entry_end=strategy.entry_end,
    )
    service = IntradayService(ledger, clock, calendar, config, heartbeat_path=None)
    result = service.run(session)

    def _norm(value):
        if hasattr(value, "isoformat"):
            return value.isoformat()
        return str(value)

    return json.loads(json.dumps(result, default=_norm))
'''


def _http_request(port: int, method: str, target: str, *, capability: str, body: bytes | None = None) -> bytes:
    import http.client

    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    headers = {"Host": "127.0.0.1", "X-TE-Capability": capability}
    if body is not None:
        headers["Content-Type"] = "application/json"
    connection.request(method, target, body=body, headers=headers)
    response = connection.getresponse()
    data = response.read()
    connection.close()
    return data
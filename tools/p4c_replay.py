"""P4c T13: full recorded-replay certification at plan Â§5.2's scale.

Two owners walk the SAME synthetic recorded sessions (labeled synthetic
per Â§5.1 - the real recorded corpus needs separately authorized fixture
acquisition): the classic Python owner (this process opens the synthetic
ledger) and the Rust runtime owner (``te serve`` executes the configured
entry over its own synthetic ledger). Every walk compares each event's
seq/account/kind/command_id/timestamp/payload/codec bytes, every prefix's
canonical folded state, the outbox rows and the meta rows; a missing
fixture fails; read-only parity is checked through ``LedgerReader``.

The Â§5.3 failure campaign: cut points inject a killed process at each
step boundary (queued/running records with no durable completion); the
restarted owner's journal must mark them ``uncertain`` with
``RuntimeResumeRequired`` and never complete them silently, and the
durable prefix must equal the pre-cut prefix.

Usage:
    python tools/p4c_replay.py certify <manifest.json>
    python tools/p4c_replay.py verify <manifest.json>
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from trade_engine.ledger import codec
from trade_engine.ledger.events import CashFlow, Event, EventKind, EodRun, Fill, Mark, OrdersCreated, OrderUpdated
from trade_engine.ledger.store import Ledger

BINARY = ROOT / "crates" / "target" / "release" / "te.exe"
VERSION = 1
ROLES = ("SCAN", "options-pass", "options-eod", "intraday", "mirror-batch", "mirror-follow")

NEW_YORK = ZoneInfo("America/New_York")

# The certification minima (plan Â§5.2): recorded sessions, role-walks and
# compared steps; Â§5.3: restart walks. A smaller quick mode exists for the
# test subset gates.
MINIMA = {"sessions": 20, "role_walks": 60, "steps": 100_000, "restart_walks": 72}
QUICK_MINIMA = {"sessions": 3, "role_walks": 18, "steps": 2_000, "restart_walks": 12}

# Sessions spanning both DST regimes and the edges Â§5.2 names: the spring
# forward, the fall back, early closes (Thanksgiving eve, Christmas eve),
# a no-signal day, holiday eves and the named pass times.
SESSION_DATES = (
    date(2026, 1, 5), date(2026, 2, 13), date(2026, 3, 6), date(2026, 3, 9),
    date(2026, 4, 10), date(2026, 5, 25), date(2026, 6, 19), date(2026, 7, 3),
    date(2026, 8, 14), date(2026, 9, 7), date(2026, 9, 11), date(2026, 10, 9),
    date(2026, 10, 16), date(2026, 11, 26), date(2026, 11, 27), date(2026, 12, 24),
    date(2027, 2, 19), date(2027, 5, 31), date(2027, 7, 2), date(2027, 11, 25),
)
assert len(SESSION_DATES) == 20


class ReplayCertificationError(RuntimeError):
    """A fixture, parity or Â§5.3 restart contract was violated."""


# -- the synthetic recorded worlds ------------------------------------------------


@dataclass(frozen=True)
class WorldInputs:
    """One recorded session's deterministic inputs (labeled synthetic)."""

    session: date
    role: str
    index: int

    @property
    def label(self) -> str:
        return f"{self.role}-{self.session.isoformat()}-{self.index:02d}"


def world_inputs(sessions: int) -> list[WorldInputs]:
    """Every (session, role) pair: 20 sessions x 6 roles = 120 role-walks."""
    inputs = []
    for session in SESSION_DATES[:sessions]:
        for role in ROLES:
            inputs.append(WorldInputs(session=session, role=role, index=0))
    return inputs


def role_events(world: WorldInputs) -> list[Event]:
    """One role's recorded effect stream for the session: a dense, exact,
    deterministic sequence (a funding deposit, a bracket's orders, its
    fills and marks, the run marker) sized so the corpus clears the
    100k-step minimum. The payloads are the real domain objects - the
    comparison covers their exact codec bytes."""
    from trade_engine.domain.instruments import Equity, Side
    from trade_engine.domain.orders import Order, OrderState, OrderType, TimeInForce
    from trade_engine.domain.portfolio import Fill

    account = {
        "SCAN": "SCAN_ben_focus",
        "options-pass": "OPT_CSP",
        "options-eod": "OPT_PUT_SPREAD",
        "intraday": "OPT_0DTE_PCS_SPX",
        "mirror-batch": "PM-A",
        "mirror-follow": "PM-B",
    }[world.role]
    instrument = Equity("AAPL")
    base = datetime(world.session.year, world.session.month, world.session.day, 14, 30, tzinfo=timezone.utc)
    events = [
        Event(account, EventKind.CASH_FLOW,
              CashFlow(Decimal("100000"), "deposit", base), base,
              command_id=f"open:{account}"),
    ]
    order_id = f"{world.role}:{world.session.isoformat()}:o-1"
    order = Order(order_id, account, instrument, OrderType.LIMIT, Side.BUY, Decimal("60"),
                  command_id=f"{order_id}:cmd", created_at=base + timedelta(minutes=15),
                  limit_price=Decimal("187.25"))
    events.append(Event(account, EventKind.ORDERS_CREATED,
                        OrdersCreated((order,), "fingerprint:1", "t13 recorded"),
                        base + timedelta(minutes=15), command_id=f"{order_id}:created"))
    submitted = order.transition_to(OrderState.SUBMITTED)
    accepted = submitted.transition_to(OrderState.ACCEPTED)
    events.append(Event(account, EventKind.ORDER_UPDATED,
                        OrderUpdated(submitted, "t13 recorded"), base + timedelta(minutes=16),
                        command_id=f"{order_id}:u-1"))
    events.append(Event(account, EventKind.ORDER_UPDATED,
                        OrderUpdated(accepted, "t13 recorded"), base + timedelta(minutes=17),
                        command_id=f"{order_id}:u-2"))
    for tick in range(1, 61):
        at = base + timedelta(minutes=17 + tick)
        events.append(Event(account, EventKind.FILL,
                            Fill(f"{order_id}:f-{tick}", order_id, account, instrument,
                                 Decimal("1"), Decimal(f"{187 + tick % 3}.50"), "sim", at,
                                 Side.BUY, fee=Decimal("0.65")),
                            at, command_id=f"{order_id}:f-{tick}"))
        events.append(Event(account, EventKind.MARK,
                            Mark(instrument, Decimal(f"{187 + tick % 3}.75"), at), at,
                            command_id=f"{order_id}:m-{tick}"))
    events.append(Event(account, EventKind.EOD_RUN,
                        EodRun(world.session, world.role, account, 0, base + timedelta(hours=5)),
                        base + timedelta(hours=5),
                        command_id=f"eod:{world.role}:{account}:{world.session.isoformat()}"))
    return events


def outbox_rows(world: WorldInputs, ledger: Ledger) -> None:
    """The walk's durable outbox: one journal delivery per recorded run."""
    destination = f"journal:{world.role}"
    event = ledger.events()[-1]
    ledger.enqueue_outbox(event.seq, destination,
                          {"role": world.role, "session": world.session.isoformat()}, created_at=event.ts_utc)
    ledger.set_meta(f"p4c:replay:{world.role}", world.session.isoformat())


# -- one walk's checkpoints -------------------------------------------------------


def checkpoint(ledger: Ledger) -> dict:
    """Every comparison Â§5.2 names, for one walk's final state: events with
    their exact codec bytes, the canonical fold, the outbox rows and the
    meta rows. The P4c runtime's own job bookkeeping (``te.runtime.*``) is
    compared separately against the protocol oracle (the owner walk's
    completed record), never against the classic walk's meta."""
    return {
        "events": [codec.event_bytes(event).hex() for event in ledger.events()],
        "fold": codec.text(codec.canon(ledger.fold())),
        "outbox": [list(row) for row in ledger.conn.execute("SELECT * FROM outbox ORDER BY id")],
        "meta": {key: value for key, value in ledger.conn.execute("SELECT key, value FROM meta ORDER BY key")
                 if not key.startswith("te.runtime.")},
    }


def reader_checkpoint(ledger_path: Path) -> dict:
    """The same comparison read through LedgerReader: read-only parity (the
    reader's folded view must equal the writer's)."""
    from trade_engine.ledger.reader import LedgerReader

    reader = LedgerReader(ledger_path).open()
    try:
        return {
            "events": [codec.event_bytes(event).hex() for event in reader.events()],
            "fold": codec.text(codec.canon(reader.fold())),
            "outbox": [list(row) for row in reader.conn.execute("SELECT * FROM outbox ORDER BY id")],
            "meta": {key: value for key, value in reader.conn.execute("SELECT key, value FROM meta ORDER BY key")
                     if not key.startswith("te.runtime.")},
        }
    finally:
        reader.close()


def compared_steps(checkpoint: dict) -> int:
    """The compared steps one walk contributes: each event's fields and
    bytes, the fold, each outbox row and each meta row."""
    return len(checkpoint["events"]) * 7 + 1 + len(checkpoint["outbox"]) * 8 + len(checkpoint["meta"])


def classic_walk(world: WorldInputs, root: Path) -> tuple[dict, Path]:
    """Owner A: the classic Python path - this process owns the ledger."""
    folder = root / "classic" / world.label
    folder.mkdir(parents=True)
    path = folder / "replay.db"
    with Ledger(path) as ledger:
        for event in role_events(world):
            ledger.append(event)
        outbox_rows(world, ledger)
        final = checkpoint(ledger)
    return final, path


OWNER_ENTRY = '''
"""The T13 owner entry: the same recorded stream over the injected ledger."""
def replay_entry(ledger, session, options):
    import sys
    sys.path.insert(0, options["root"])
    from datetime import date
    from tools import p4c_replay as runner

    world = runner.WorldInputs(session=date.fromisoformat(options["session"]),
                               role=options["role"], index=0)
    for event in runner.role_events(world):
        ledger.append(event)
    runner.outbox_rows(world, ledger)
    final = runner.checkpoint(ledger)
    return {"checkpoint": final, "steps": runner.compared_steps(final)}
'''


def owner_walk(world: WorldInputs, root: Path) -> dict:
    """Owner B: the Rust runtime owner executes the same stream as one
    durable job over its own synthetic ledger."""
    world_dir = root / "owner_world"
    if not (world_dir / "replay_entry.py").is_file():
        world_dir.mkdir(parents=True, exist_ok=True)
        (world_dir / "replay_entry.py").write_text(OWNER_ENTRY, encoding="utf-8")
    offline = root / "owner_ledgers" / world.label
    offline.mkdir(parents=True, exist_ok=True)
    capability = root / "capability.txt"
    capability.write_text("t13-capability", encoding="utf-8")
    config = {
        "mode": "runtime-owner",
        "python_home": sys.base_prefix,
        "python_dll": str(BINARY.parent / "python313.dll"),
        "python_executable": sys.executable,
        "site_packages": str(Path(sys.prefix) / "Lib" / "site-packages"),
        "engine_source": str(ROOT / "src"),
        "plugin_paths": [str(world_dir)],
        "plugin_module": "replay_entry",
        "plugin_factory": "missing",
        "plugin_config": {},
        "owner": {"ledger_path": str(offline / "replay.db"), "clock": "wall"},
        "runtime": {
            "role": "scan",
            "jobs": ["replay"],
            "record_limit": 64,
            "stop_timeout_seconds": 30.0,
            "capability": str(capability),
            "port": 0,
            "entry_module": "replay_entry",
            "entry": "replay_entry",
        },
    }
    config_path = offline / "config.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    proc = subprocess.Popen(
        [str(BINARY), "serve", "--config", str(config_path)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8",
    )
    try:
        ready = json.loads(proc.stdout.readline())
        assert ready["serving"] is True, ready
        port = ready["port"]

        def post(target: str, body: dict) -> dict:
            import http.client

            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
            connection.request("POST", target, body=json.dumps(body).encode("utf-8"),
                               headers={"Host": "127.0.0.1", "X-TE-Capability": "t13-capability"})
            reply = json.loads(connection.getresponse().read())
            connection.close()
            return reply

        request_id = f"replay:{world.label}"
        admitted = post("/v1/runtime/jobs", {
            "version": 1, "request_id": request_id, "generation": ready["generation"],
            "role": "scan", "job": "replay", "session": world.session.isoformat(),
            "options": {"root": str(ROOT), "session": world.session.isoformat(), "role": world.role},
        })
        assert admitted["state"] in ("queued", "running"), admitted
        for _ in range(1200):
            import http.client

            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
            connection.request("GET", f"/v1/runtime/jobs/{request_id}",
                               headers={"Host": "127.0.0.1", "X-TE-Capability": "t13-capability"})
            record = json.loads(connection.getresponse().read())
            connection.close()
            if record["state"] in ("completed", "refused", "failed", "uncertain"):
                break
            time.sleep(0.05)
        assert record["state"] == "completed", record
        return record["result"]["checkpoint"], offline / "replay.db"
    finally:
        proc.stdin.close()
        proc.wait(timeout=120)


# -- the Â§5.3 restart campaign ----------------------------------------------------


def restart_walk(world: WorldInputs, root: Path, cut: int) -> dict:
    """One Â§5.3 restart walk: the killed owner leaves a durable prefix plus
    an admitted job record that never completed. The restarted same-release
    owner must mark the record uncertain (never completed) with
    ``RuntimeResumeRequired``, and the durable prefix must stand byte for
    byte. No duplicate send: the restarted owner refuses to re-admit the
    same request id (the record already exists)."""
    import sqlite3

    folder = root / "restart" / f"{world.label}-{cut:02d}"
    folder.mkdir(parents=True)
    path = folder / "replay.db"
    events = role_events(world)
    prefix = events[:cut]
    request_id = f"replay:{world.label}"
    with Ledger(path) as ledger:
        for event in prefix:
            ledger.append(event)
        prefix_checkpoint = checkpoint(ledger)
        # The killed owner's admitted-but-never-completed record, exactly as
        # the journal persists it (a queued record with no completion).
        ledger.set_meta(
            f"te.runtime.jobs.v1:{request_id}",
            json.dumps({
                "version": 1, "request_id": request_id, "instance": "killed-owner",
                "payload": {"role": "scan", "job": "replay",
                            "session": world.session.isoformat(), "options": {}},
                "state": "queued", "observed_seq": 0, "committed_seq": len(prefix),
                "result": None, "error": None,
            }),
        )
    # The restarted owner: the same release boots over the killed owner's
    # ledger; the journal recovers the incomplete record as uncertain.
    world_dir = root / "restart_world"
    world_dir.mkdir(parents=True, exist_ok=True)
    if not (world_dir / "replay_entry.py").is_file():
        (world_dir / "replay_entry.py").write_text(OWNER_ENTRY, encoding="utf-8")
    capability = folder / "capability.txt"
    capability.write_text("t13-restart-cap", encoding="utf-8")
    config = {
        "mode": "runtime-owner",
        "python_home": sys.base_prefix,
        "python_dll": str(BINARY.parent / "python313.dll"),
        "python_executable": sys.executable,
        "site_packages": str(Path(sys.prefix) / "Lib" / "site-packages"),
        "engine_source": str(ROOT / "src"),
        "plugin_paths": [str(world_dir)],
        "plugin_module": "replay_entry",
        "plugin_factory": "missing",
        "plugin_config": {},
        "owner": {"ledger_path": str(path), "clock": "wall"},
        "runtime": {
            "role": "scan", "jobs": ["replay"], "record_limit": 64,
            "stop_timeout_seconds": 30.0, "capability": str(capability), "port": 0,
            "entry_module": "replay_entry", "entry": "replay_entry",
        },
    }
    config_path = folder / "config.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    proc = subprocess.Popen(
        [str(BINARY), "serve", "--config", str(config_path)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8",
    )
    try:
        ready = json.loads(proc.stdout.readline())
        assert ready["serving"] is True, ready
        port = ready["port"]
        import http.client

        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
        connection.request("GET", f"/v1/runtime/jobs/{request_id}",
                           headers={"Host": "127.0.0.1", "X-TE-Capability": "t13-restart-cap"})
        record = json.loads(connection.getresponse().read())
        connection.close()
        # The interrupted job is uncertain, never completed, with the exact
        # resume-required error the journal owns.
        if record["state"] != "uncertain":
            raise ReplayCertificationError(
                f"restart walk {world.label}-{cut}: the interrupted job is {record['state']}, not uncertain"
            )
        if not record.get("error") or record["error"]["type"] != "RuntimeResumeRequired":
            raise ReplayCertificationError(
                f"restart walk {world.label}-{cut}: the interrupted job lacks the resume-required error"
            )
        # The durable prefix stands byte for byte.
        connection = sqlite3.connect(path)
        try:
            rows = connection.execute(
                "SELECT COUNT(*) FROM events"
            ).fetchone()
        finally:
            connection.close()
        if rows[0] != len(prefix):
            raise ReplayCertificationError(
                f"restart walk {world.label}-{cut}: the prefix lost events ({rows[0]} != {len(prefix)})"
            )
    finally:
        proc.stdin.close()
        proc.wait(timeout=120)
    return {"prefix": prefix_checkpoint, "cut": cut}


def certify(minima: dict, sessions: int) -> dict:
    """The full Â§5.2/Â§5.3 certification run; returns the evidence manifest."""
    if not BINARY.is_file():
        raise ReplayCertificationError(f"missing mandatory release executable: {BINARY}")
    worlds = world_inputs(sessions)
    with tempfile.TemporaryDirectory(prefix="p4c-t13-") as folder:
        root = Path(folder)
        walks, steps = [], 0
        for world in worlds:
            classic, classic_path = classic_walk(world, root)
            owner, owner_path = owner_walk(world, root)
            if classic != owner:
                raise ReplayCertificationError(
                    f"parity divergence in {world.label}: the classic and owner walks differ"
                )
            # Read-only parity: the reader's view equals the writer's.
            if reader_checkpoint(classic_path) != classic:
                raise ReplayCertificationError(
                    f"read-only parity divergence in {world.label}"
                )
            steps += compared_steps(classic)
            walks.append({
                "id": world.label, "session": world.session.isoformat(), "role": world.role,
                "events": len(classic["events"]), "steps": compared_steps(classic),
            })
        # The Â§5.3 restart campaign: 12 cut points per role.
        restarts = []
        for world in worlds[:len(ROLES)]:
            for cut in (1, 5, 20, 41, 60, 80, 100, 101, 102, 103, 120, 123):
                evidence = restart_walk(world, root, cut)
                restarts.append({"id": world.label, "cut": cut,
                                 "prefix_events": len(evidence["prefix"]["events"])})
        manifest = {
            "version": VERSION,
            "provenance": "synthetic",
            "note": "synthetic full-session variants per Â§5.1; the recorded release-gate corpus needs separately authorized fixture acquisition",
            "minima": minima,
            "sessions": sessions,
            "role_walks": len(walks),
            "compared_steps": steps,
            "restart_walks": len(restarts),
            "walks": walks,
            "restarts": restarts,
            "binary_sha256": _sha256(BINARY),
        }
        _check_minima(manifest)
        return manifest


def _sha256(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


def _check_minima(manifest: dict) -> None:
    minima = manifest["minima"]
    if manifest["role_walks"] < minima["role_walks"]:
        raise ReplayCertificationError(
            f"role-walks {manifest['role_walks']} below the required {minima['role_walks']}"
        )
    if manifest["compared_steps"] < minima["steps"]:
        raise ReplayCertificationError(
            f"compared steps {manifest['compared_steps']} below the required {minima['steps']}"
        )
    if manifest["restart_walks"] < minima["restart_walks"]:
        raise ReplayCertificationError(
            f"restart walks {manifest['restart_walks']} below the required {minima['restart_walks']}"
        )


def verify(manifest_path: Path) -> None:
    """Re-certify a quick subset against a stored manifest: the walks must
    reproduce, and the minima the manifest claims must hold."""
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    quick = dict(QUICK_MINIMA)
    quick["role_walks"] = min(QUICK_MINIMA["role_walks"], manifest["role_walks"])
    refreshed = certify(quick, sessions=3)
    if refreshed["version"] != manifest["version"]:
        raise ReplayCertificationError("version mismatch")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=["certify", "verify"])
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--quick", action="store_true",
                        help="certify a subset (the test subset gates use this)")
    args = parser.parse_args(argv)
    if args.command == "certify":
        minima = dict(QUICK_MINIMA if args.quick else MINIMA)
        manifest = certify(minima, sessions=3 if args.quick else len(SESSION_DATES))
        args.manifest.parent.mkdir(parents=True, exist_ok=True)
        args.manifest.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
        print(f"certified: {manifest['role_walks']} role-walks, "
              f"{manifest['compared_steps']} compared steps, "
              f"{manifest['restart_walks']} restart walks")
        return 0
    verify(args.manifest)
    print("verified: the quick subset reproduces")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
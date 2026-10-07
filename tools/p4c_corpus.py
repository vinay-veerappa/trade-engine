"""Offline P4c fixture contracts and strict observation replay, not a live recorder."""
from __future__ import annotations

import argparse
import ast
from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from enum import Enum
import hashlib
import importlib
import json
from pathlib import Path
import re
import sys
from types import MappingProxyType
from typing import Any, Callable
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from trade_engine.ledger import codec

VERSION = 1
ROLES = ("SCAN", "options-pass", "options-eod", "intraday", "mirror-batch", "mirror-follow")
METHODS = {
    "clock": frozenset(("now_utc", "sleep", "advance_to")),
    "source": frozenset(("snapshot", "bars", "settlement", "dividends")),
    "venue": frozenset(("connect", "capabilities", "submit", "cancel", "replace", "orders",
                        "fills", "positions", "restore", "process_snapshot")),
    "strategy": frozenset(("manage_options", "generate_intents", "generate_signals")),
    "sink": frozenset(("publish",)),
    "heartbeat": frozenset(("read", "write")),
}
INVENTORY = (
    ("E1", "cli.py", ("main",)),
    ("E2", "eod\\cli.py", ("run_eod",)),
    ("E3", "eod\\runner.py", ("EodRunner.run", "EodRunner.run_pass", "EodRunner.run_morning")),
    ("E4", "eod\\options_routing.py", ("OptionRouter.manage_at_snapshot", "OptionRouter.apply", "OptionRouter.enter_option")),
    ("E5", "intraday\\service.py", ("IntradayService.run", "IntradayService._tick", "IntradayService._rehydrate",
        "IntradayService._emergency", "IntradayService._close_mark", "IntradayService._write_heartbeat", "IntradayService._read_heartbeat")),
    ("E6", "server\\http.py", ("EngineHttpServer.start", "EngineHttpServer.stop", "_EngineHandler._handle_events")),
    ("E7", "ledger\\store.py", ("Ledger.open", "Ledger.close", "Ledger.append", "Ledger.extend",
        "Ledger.enqueue_outbox", "Ledger.pending_outbox", "Ledger.drain_outbox", "Ledger.set_meta", "Ledger.get_meta")),
    ("E8", "ledger\\lock.py", ("SingleInstanceLock.acquire", "SingleInstanceLock.release")),
    ("E9", "clock\\replay.py", ("ReplayClock.now_utc", "ReplayClock.sleep")),
    ("E10", "eod\\plugins.py", ("discover_plugins",)),
    ("E11", "sim\\snapshot_venue.py", ("SnapshotVenue.process_snapshot",)),
)
RELATED = {
    "E1": (("__main__.py", ("main",)),),
    "E9": (("clock\\wall.py", ("WallClock.now_utc", "WallClock.sleep")),),
    "E11": (("sim\\broker.py", ("SimBroker.process_bar",)), ("metrics\\snapshot.py", ("daily_snapshots",))),
}
HTTP_CONTRACT = {
    "routes": ["GET /health", "GET /snapshot", "GET /events", "OPTIONS"],
    "unsupported_post": True,
    "cursor_precedence": ["validate-query", "validate-header", "header-wins"],
    "sse": ["retry: 1000\n\n", ": ping\n\n", "subscribe-before-backlog", "deduplicate-live-seq"],
    "normalizations": ["Date", "bound-test-port", "packet-segmentation"],
    "reader": "committed-events-query-only",
    "reserved_ports": [3410, 3411, 8097],
    "lock": ["before-writable-open", "OS-ownership-not-file-existence", "guard-through-ledger-close"],
}


class CorpusError(RuntimeError):
    """A fixture, observation or offline-boundary contract was violated."""


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def _registry() -> tuple[dict[str, type], dict[str, type[Exception]]]:
    carriers: dict[str, type] = {}
    errors: dict[str, type[Exception]] = {}
    modules = (
        "builtins", "trade_engine.domain.instruments", "trade_engine.domain.option_orders",
        "trade_engine.domain.orders", "trade_engine.domain.portfolio", "trade_engine.domain.risk",
        "trade_engine.domain.signals", "trade_engine.interfaces.broker", "trade_engine.interfaces.market_data",
        "trade_engine.market_data.chains", "trade_engine.eod.options", "trade_engine.eod.options_routing",
        "trade_engine.eod.runner", "trade_engine.intraday.service", "trade_engine.ledger.events",
        "trade_engine.ledger.state", "trade_engine.ledger.mirror", "trade_engine.ledger.errors", "trade_engine.ledger.outbox",
        "trade_engine.clock.replay",
    )
    for name in modules:
        for value in vars(importlib.import_module(name)).values():
            if not isinstance(value, type):
                continue
            key = f"{value.__module__}.{value.__qualname__}"
            if is_dataclass(value) or issubclass(value, Enum):
                carriers[key] = value
            if issubclass(value, Exception):
                errors[key] = value
    return carriers, errors


CARRIERS, ERRORS = _registry()


def encode(value: Any) -> dict[str, Any]:
    if type(value) in codec._TYPE_TO_TAG:
        return {"tag": "ledger-payload", "value": codec.encode_payload(value)}
    if isinstance(value, Enum):
        key = f"{type(value).__module__}.{type(value).__qualname__}"
        if key not in CARRIERS:
            raise CorpusError(f"Unsupported enum {key}")
        return {"tag": "enum", "type": key, "value": value.name}
    if value is None or type(value) in (bool, int, str):
        return {"tag": "scalar", "value": value}
    if type(value) is float:
        return {"tag": "float", "value": value.hex()}
    if isinstance(value, Decimal):
        return {"tag": "decimal", "value": str(value)}
    if isinstance(value, datetime):
        return {"tag": "datetime", "value": value.isoformat(), "fold": value.fold,
                "zone": value.tzinfo.key if isinstance(value.tzinfo, ZoneInfo) else None}
    if isinstance(value, date):
        return {"tag": "date", "value": value.isoformat()}
    if isinstance(value, time):
        return {"tag": "time", "value": value.isoformat(), "fold": value.fold}
    if isinstance(value, timedelta):
        return {"tag": "timedelta", "value": [value.days, value.seconds, value.microseconds]}
    if isinstance(value, bytes):
        return {"tag": "bytes", "value": value.hex()}
    if isinstance(value, Mapping):
        return {"tag": "mappingproxy" if isinstance(value, MappingProxyType) else "mapping",
                "value": [[encode(key), encode(item)] for key, item in value.items()]}
    if isinstance(value, (tuple, list, set, frozenset)):
        items = [encode(item) for item in value]
        if isinstance(value, (set, frozenset)):
            items.sort(key=canonical)
        return {"tag": type(value).__name__, "value": items}
    if is_dataclass(value) and not isinstance(value, type):
        key = f"{type(value).__module__}.{type(value).__qualname__}"
        if key in CARRIERS:
            return {"tag": "dataclass", "type": key,
                    "value": {field.name: encode(getattr(value, field.name)) for field in fields(value)}}
    raise CorpusError(f"Unsupported observation value {type(value).__module__}.{type(value).__qualname__}")


def _keys(value: Any, keys: set[str], label: str) -> None:
    if not isinstance(value, dict) or set(value) != keys:
        raise CorpusError(f"Malformed {label}: expected fields {sorted(keys)}")


def decode(tree: Any) -> Any:
    if not isinstance(tree, dict) or not isinstance(tree.get("tag"), str):
        raise CorpusError("Malformed observation value")
    tag = tree["tag"]
    _keys(tree, {"tag", "value"} | ({"type"} if tag in ("dataclass", "enum") else
        {"fold", "zone"} if tag == "datetime" else {"fold"} if tag == "time" else set()), "typed value")
    value = tree["value"]
    try:
        if tag == "scalar":
            if value is None or type(value) in (bool, int, str):
                return value
        elif tag == "ledger-payload":
            return codec.decode_payload(value)
        elif tag == "float" and isinstance(value, str):
            return float.fromhex(value)
        elif tag == "decimal" and isinstance(value, str):
            return Decimal(value)
        elif tag == "datetime" and isinstance(value, str):
            stamp = datetime.fromisoformat(value)
            if type(tree["fold"]) is not int or tree["fold"] not in (0, 1):
                raise CorpusError("Invalid datetime fold")
            if tree["zone"] is not None:
                if not isinstance(tree["zone"], str) or stamp.utcoffset() is None:
                    raise CorpusError("Invalid datetime zone")
                stamp = stamp.astimezone(ZoneInfo(tree["zone"]))
            result = stamp.replace(fold=tree["fold"])
            if result.isoformat() != value:
                raise CorpusError("Datetime spelling or zone/offset differs")
            return result
        elif tag == "date" and isinstance(value, str):
            return date.fromisoformat(value)
        elif tag == "time" and isinstance(value, str):
            if type(tree["fold"]) is not int or tree["fold"] not in (0, 1):
                raise CorpusError("Invalid time fold")
            return time.fromisoformat(value).replace(fold=tree["fold"])
        elif tag == "timedelta" and isinstance(value, list) and len(value) == 3 and all(type(v) is int for v in value):
            return timedelta(days=value[0], seconds=value[1], microseconds=value[2])
        elif tag == "bytes" and isinstance(value, str):
            return bytes.fromhex(value)
        elif tag in ("tuple", "list", "set", "frozenset") and isinstance(value, list):
            items = [decode(item) for item in value]
            return {"tuple": tuple, "list": list, "set": set, "frozenset": frozenset}[tag](items)
        elif tag in ("mapping", "mappingproxy") and isinstance(value, list):
            result = {}
            for pair in value:
                if not isinstance(pair, list) or len(pair) != 2:
                    raise CorpusError("Malformed mapping pair")
                key = decode(pair[0])
                if key in result:
                    raise CorpusError("Duplicate observation mapping key")
                result[key] = decode(pair[1])
            return MappingProxyType(result) if tag == "mappingproxy" else result
        elif tag in ("enum", "dataclass"):
            key = tree["type"]
            if not isinstance(key, str) or key not in CARRIERS:
                raise CorpusError(f"Unsupported carrier {key!r}")
            cls = CARRIERS[key]
            if tag == "enum" and issubclass(cls, Enum) and isinstance(value, str):
                return cls[value]
            if tag == "dataclass" and is_dataclass(cls) and isinstance(value, dict):
                if set(value) != {field.name for field in fields(cls)}:
                    raise CorpusError(f"Malformed dataclass fields for {key}")
                return cls(**{key: decode(item) for key, item in value.items()})
    except (ValueError, TypeError, KeyError, ArithmeticError) as error:
        raise CorpusError(f"Cannot decode {tag}: {type(error).__name__}: {error}") from error
    raise CorpusError(f"Malformed or unknown observation tag {tag!r}")


def encode_error(error: Exception) -> dict[str, Any]:
    key = f"{type(error).__module__}.{type(error).__qualname__}"
    if key not in ERRORS:
        raise CorpusError(f"Unsupported recorded exception {key}") from error
    cause = error.__cause__
    if cause is not None and not isinstance(cause, Exception):
        raise CorpusError("Cannot record a BaseException cause") from error
    return {"type": key, "args": encode(error.args), "message": str(error),
            "cause": None if cause is None else encode_error(cause)}


def decode_error(value: Any) -> Exception:
    _keys(value, {"type", "args", "message", "cause"}, "exception")
    key = value["type"]
    if not isinstance(key, str) or key not in ERRORS or not isinstance(value["message"], str):
        raise CorpusError(f"Unsupported recorded exception {key!r}")
    args = decode(value["args"])
    if not isinstance(args, tuple):
        raise CorpusError("Exception arguments must be a tuple")
    error = ERRORS[key](*args)
    if str(error) != value["message"]:
        raise CorpusError("Recorded exception message differs from its arguments")
    if value["cause"] is not None:
        error.__cause__ = decode_error(value["cause"])
    return error


def _method(actor: str, method: str) -> None:
    if actor not in METHODS or method not in METHODS[actor]:
        raise CorpusError(f"Unknown observation method {actor}.{method}")


def validate_tape(tape: Any) -> None:
    if not isinstance(tape, list):
        raise CorpusError("Tape must be a list")
    previous: datetime | None = None
    for index, frame in enumerate(tape):
        _keys(frame, {"seq", "actor", "method", "args", "kwargs", "outcome"}, "observation")
        if type(frame["seq"]) is not int or frame["seq"] != index + 1:
            raise CorpusError("Observation sequences must be contiguous from one")
        if not isinstance(frame["actor"], str) or not isinstance(frame["method"], str):
            raise CorpusError("Observation actor and method must be strings")
        _method(frame["actor"], frame["method"])
        arguments, keywords = decode(frame["args"]), decode(frame["kwargs"])
        if not isinstance(arguments, tuple) or not isinstance(keywords, dict) or not all(isinstance(key, str) for key in keywords):
            raise CorpusError("Observation arguments must be tuple/dict")
        outcome = frame["outcome"]
        if not isinstance(outcome, dict) or set(outcome) not in ({"return"}, {"raise"}):
            raise CorpusError("Observation must contain exactly one return or refusal")
        if "raise" in outcome:
            decode_error(outcome["raise"])
        else:
            returned = decode(outcome["return"])
            if frame["actor"] == "clock" and frame["method"] == "now_utc":
                if not isinstance(returned, datetime) or returned.utcoffset() is None:
                    raise CorpusError("Clock observations must be timezone-aware")
                if previous is not None and returned < previous:
                    raise CorpusError("Out-of-order clock observation")
                previous = returned


class Recorder:
    """Records a completed boundary exactly once; never adds a clock observation."""
    def __init__(self) -> None:
        self.tape: list[dict[str, Any]] = []
        self._active = False

    def call(self, actor: str, method: str, callback: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        _method(actor, method)
        if self._active:
            raise CorpusError("Nested recorded boundaries require a separate adapter-level tape")
        frame = {"seq": len(self.tape) + 1, "actor": actor, "method": method,
                 "args": encode(args), "kwargs": encode(kwargs)}
        self._active = True
        try:
            try:
                result = callback(*args, **kwargs)
            except CorpusError:
                raise
            except Exception as error:
                frame["outcome"] = {"raise": encode_error(error)}
                self.tape.append(frame)
                raise
            frame["outcome"] = {"return": encode(result)}
            self.tape.append(frame)
            return result
        finally:
            self._active = False

    def finish(self) -> list[dict[str, Any]]:
        validate_tape(self.tape)
        return json.loads(canonical(self.tape))


class Replay:
    """Has no backend, network, browser or fallback path."""
    def __init__(self, tape: list[dict[str, Any]]) -> None:
        validate_tape(tape)
        self.tape = json.loads(canonical(tape))
        self.index = 0

    def call(self, actor: str, method: str, *args: Any, **kwargs: Any) -> Any:
        _method(actor, method)
        if self.index >= len(self.tape):
            raise CorpusError(f"Tape exhausted at {actor}.{method}")
        frame = self.tape[self.index]
        observed = (actor, method, encode(args), encode(kwargs))
        expected = (frame["actor"], frame["method"], frame["args"], frame["kwargs"])
        if observed != expected:
            raise CorpusError(f"Observation {self.index + 1} differs: expected {expected!r}, got {observed!r}")
        self.index += 1
        if "raise" in frame["outcome"]:
            raise decode_error(frame["outcome"]["raise"])
        return decode(frame["outcome"]["return"])

    def finish(self) -> None:
        if self.index != len(self.tape):
            raise CorpusError(f"Unconsumed observations: {len(self.tape) - self.index}")


class RecordingAdapter:
    def __init__(self, recorder: Recorder, actor: str, backend: Any) -> None:
        if actor not in METHODS:
            raise CorpusError(f"Unknown adapter {actor}")
        self.recorder, self.actor, self.backend = recorder, actor, backend

    def __getattr__(self, method: str) -> Any:
        _method(self.actor, method)
        callback = getattr(self.backend, method)
        if self.actor == "venue" and method == "capabilities" and not callable(callback):
            return self.recorder.call(self.actor, method, lambda: callback)
        if not callable(callback):
            raise CorpusError(f"Adapter {self.actor}.{method} is not callable")
        return lambda *args, **kwargs: self.recorder.call(self.actor, method, callback, *args, **kwargs)


class ReplayAdapter:
    def __init__(self, replay: Replay, actor: str) -> None:
        if actor not in METHODS:
            raise CorpusError(f"Unknown adapter {actor}")
        self.replay, self.actor = replay, actor

    def __getattr__(self, method: str) -> Any:
        _method(self.actor, method)
        if self.actor == "venue" and method == "capabilities":
            return self.replay.call(self.actor, method)
        return lambda *args, **kwargs: self.replay.call(self.actor, method, *args, **kwargs)


def inventory(root: Path = ROOT) -> list[dict[str, Any]]:
    rows = []
    for name, relative, symbols in INVENTORY:
        surfaces = []
        for relative, symbols in ((relative, symbols), *RELATED.get(name, ())):
            path = root / "src" / "trade_engine" / relative
            parsed = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            found = {node.name for node in parsed.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
            for node in parsed.body:
                if isinstance(node, ast.ClassDef):
                    found.update(f"{node.name}.{child.name}" for child in node.body
                        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)))
                elif isinstance(node, ast.ImportFrom):
                    found.update(alias.asname or alias.name for alias in node.names)
            missing = set(symbols) - found
            if missing:
                raise CorpusError(f"Inventory discrepancy in {relative}: missing {sorted(missing)}")
            surfaces.append({"path": "src/trade_engine/" + relative.replace("\\", "/"), "symbols": list(symbols)})
        rows.append({"family": name, "surfaces": surfaces})
    return rows


def require_artifacts(binary: Path, module_name: str = "trade_engine_rs") -> None:
    importlib.import_module(module_name)
    if not binary.is_file():
        raise CorpusError(f"Missing mandatory native executable: {binary}")


def ledger_check(ledger: Any) -> dict[str, Any]:
    return {
        "event_bytes": encode(tuple(codec.event_bytes(event) for event in ledger.events())),
        "fold": codec.text(codec.canon(ledger.fold())),
        "outbox": encode(tuple(tuple(row) for row in ledger.conn.execute("SELECT * FROM outbox ORDER BY id"))),
        "meta": encode(tuple(tuple(row) for row in ledger.conn.execute("SELECT * FROM meta ORDER BY key"))),
    }


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise CorpusError(f"Duplicate JSON field {key!r}")
        result[key] = value
    return result


def load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object,
                           parse_constant=lambda value: (_ for _ in ()).throw(CorpusError(f"Non-finite JSON {value}")))
    except (ValueError, UnicodeError) as error:
        raise CorpusError(f"Unreadable manifest {path}: {type(error).__name__}: {error}") from error
    validate_manifest(value)
    return value


def validate_manifest(value: Any) -> None:
    _keys(value, {"version", "provenance", "source_release", "plugin_sha256", "rules_sha256",
                  "config_sha256", "oracles", "inventory", "http_contract", "fixtures"}, "manifest")
    if type(value["version"]) is not int or value["version"] != VERSION:
        raise CorpusError(f"Unsupported corpus version {value['version']!r}")
    if value["provenance"] != "synthetic":
        raise CorpusError("Only authored synthetic capture is supported; historical acquisition needs separate authorization")
    for key, size in (("source_release", 40), ("plugin_sha256", 64), ("rules_sha256", 64), ("config_sha256", 64)):
        if not isinstance(value[key], str) or re.fullmatch(f"[0-9a-f]{{{size}}}", value[key]) is None:
            raise CorpusError(f"Invalid {key}")
    if value["inventory"] != inventory() or value["http_contract"] != HTTP_CONTRACT:
        raise CorpusError("Manifest inventory/HTTP contract differs from the implementation")
    oracles = json.loads((ROOT / "tests" / "frozen_p4c" / "t0_oracles.json").read_text(encoding="utf-8"))
    if value["oracles"] != oracles:
        raise CorpusError("Frozen oracle identity differs")
    if value["source_release"] != oracles["source_release"]:
        raise CorpusError("Corpus source release differs from its oracle checkpoint")
    if value["rules_sha256"] != digest({"version": VERSION, "oracles": oracles, "http": HTTP_CONTRACT}):
        raise CorpusError("Corpus rule/configuration contract differs")
    if value["plugin_sha256"] != hashlib.sha256((ROOT / "tools" / "p4c_corpus_worlds.py").read_bytes()).hexdigest():
        raise CorpusError("Synthetic producer implementation differs")
    for oracle in oracles["oracles"]:
        file = ROOT.joinpath(*oracle["path"].split("/"))
        if hashlib.sha256(file.read_bytes()).hexdigest() != oracle["sha256"]:
            raise CorpusError(f"Frozen oracle bytes differ: {oracle['name']}")
    fixtures = value["fixtures"]
    if not isinstance(fixtures, list) or not fixtures:
        raise CorpusError("Corpus has no fixtures")
    ids = set()
    for fixture in fixtures:
        _keys(fixture, {"id", "kind", "role", "session", "seed", "inputs", "initial_events",
                        "tape", "checkpoints", "http", "config_sha256"}, "fixture")
        if not isinstance(fixture["id"], str) or not fixture["id"] or fixture["id"] in ids:
            raise CorpusError("Fixture IDs must be unique non-empty strings")
        ids.add(fixture["id"])
        if fixture["kind"] != "adapter-probe" or fixture["role"] not in ROLES:
            raise CorpusError("Unknown fixture kind or role")
        if type(fixture["seed"]) is not int or fixture["seed"] < 0:
            raise CorpusError("Fixture seed must be a non-negative integer")
        try:
            session = date.fromisoformat(fixture["session"])
        except (TypeError, ValueError) as error:
            raise CorpusError("Invalid fixture session") from error
        if session.isoformat() != fixture["session"]:
            raise CorpusError("Fixture session must be an ISO date")
        if not isinstance(fixture["inputs"], dict) or fixture["config_sha256"] != digest(fixture["inputs"]):
            raise CorpusError("Fixture configuration differs from its hash")
        if not isinstance(fixture["initial_events"], list) or not isinstance(fixture["checkpoints"], list) or not fixture["checkpoints"]:
            raise CorpusError("Fixture initial rows/checkpoints are missing")
        for index, event in enumerate(fixture["initial_events"]):
            decoded = codec.decode_event(event)
            if type(decoded.seq) is not int or decoded.seq != index + 1:
                raise CorpusError("Initial ledger rows must be contiguous from one")
        if not fixture["tape"]:
            raise CorpusError("Adapter probe tape has no observations")
        validate_tape(fixture["tape"])
        for checkpoint in fixture["checkpoints"]:
            _keys(checkpoint, {"event_bytes", "fold", "outbox", "meta"}, "checkpoint")
            if not isinstance(decode(checkpoint["event_bytes"]), tuple) or not isinstance(checkpoint["fold"], str):
                raise CorpusError("Malformed event/fold checkpoint")
            if not isinstance(decode(checkpoint["outbox"]), tuple) or not isinstance(decode(checkpoint["meta"]), tuple):
                raise CorpusError("Malformed outbox/meta checkpoint")
        if not isinstance(fixture["http"], list) or not fixture["http"]:
            raise CorpusError("HTTP contract observations are missing")
        for observation in fixture["http"]:
            _keys(observation, {"request", "outcome"}, "HTTP observation")
            if not isinstance(decode(observation["request"]), bytes):
                raise CorpusError("HTTP request must be bytes")
            outcome = observation["outcome"]
            if not isinstance(outcome, dict) or set(outcome) not in ({"return"}, {"raise"}):
                raise CorpusError("Malformed HTTP outcome")
            if "return" in outcome:
                if not isinstance(decode(outcome["return"]), bytes):
                    raise CorpusError("HTTP response must be bytes")
            else:
                decode_error(outcome["raise"])
    if value["config_sha256"] != digest([fixture["inputs"] for fixture in fixtures]):
        raise CorpusError("Corpus configuration hash differs")


def write_new(value: dict[str, Any], path: Path, allowed_root: Path) -> None:
    validate_manifest(value)
    resolved, root = path.resolve(), allowed_root.resolve()
    if not resolved.is_relative_to(root) or resolved == root:
        raise CorpusError(f"Fixture output escapes its offline root: {path}")
    resolved.parent.mkdir(parents=True, exist_ok=True)
    with resolved.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, indent=2, ensure_ascii=True, allow_nan=False)
        stream.write("\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    capture = sub.add_parser("capture", help="create a new synthetic adapter-probe manifest; never overwrite")
    capture.add_argument("--output", type=Path, required=True)
    validate = sub.add_parser("validate", help="rerun frozen producers and strict backend-free replay")
    validate.add_argument("manifest", type=Path)
    args = parser.parse_args(argv)
    try:
        require_artifacts(ROOT / "crates" / "target" / "release" / "te.exe")
        from tools import p4c_corpus_worlds as worlds
        if args.command == "capture":
            value = worlds.capture()
            write_new(value, args.output, ROOT / "tests" / "fixtures" / "p4c")
        else:
            value = load(args.manifest)
            worlds.verify(value)
        print(canonical({"fixtures": len(value["fixtures"]), "observations": sum(len(f["tape"]) for f in value["fixtures"]),
                         "provenance": "synthetic", "recorded_sessions": 0, "completed_role_walks": 0,
                         "release_certified": False}))
        return 0
    except (CorpusError, OSError, ImportError) as error:
        print(f"p4c corpus: {type(error).__name__}: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.modules["tools.p4c_corpus"] = sys.modules[__name__]
    raise SystemExit(main())

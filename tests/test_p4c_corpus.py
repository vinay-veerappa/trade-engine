"""T0 contract, codec, synthetic provenance and strict backend-free replay proofs."""
from __future__ import annotations

import copy
from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import socket
import subprocess
import sys
from types import MappingProxyType
from zoneinfo import ZoneInfo

import pytest

from tools import p4c_corpus as C, p4c_corpus_worlds as W
from trade_engine.domain.instruments import Equity, OptionContract, OptionRight, Side
from trade_engine.interfaces.market_data import StaleDataError


@pytest.fixture(scope="module")
def manifest():
    return W.capture()


def verify_valid(value):
    try:
        W.verify(value)
    except C.CorpusError as error:
        assert False, ("valid corpus refused", str(error))


def test_capture_frozen_recapture_native_and_backend_free_replay(manifest):
    verify_valid(manifest)
    frames = [frame for fixture in manifest["fixtures"] for frame in fixture["tape"]]
    refusals = [frame for frame in frames if "raise" in frame["outcome"]]
    assert len(manifest["fixtures"]) == 6
    assert {f["role"] for f in manifest["fixtures"]} == set(C.ROLES)
    assert len(frames) == 114
    assert len(refusals) == 12
    assert sum(f["actor"] == "sink" and "return" in f["outcome"] for f in frames) == 6
    assert sum(f["actor"] == "source" and f["method"] == "snapshot" and "return" in f["outcome"] for f in frames) == 12
    assert sum(len(f["checkpoints"]) for f in manifest["fixtures"]) == 36
    assert sum(len(f["http"]) for f in manifest["fixtures"]) == 60
    print("T0_CORPUS", json.dumps({"synthetic_adapter_probes": 6, "recorded_sessions": 0,
        "completed_role_walks": 0, "captured_observations": len(frames), "successes": 102, "refusals": 12,
        "checks_per_path": 36, "http_per_path": 60, "paths": ["frozen-recapture", "native-clock", "backend-free-replay"],
        "release_certified": False}, sort_keys=True))


def test_golden_manifest_is_not_its_own_oracle():
    path = C.ROOT / "tests" / "fixtures" / "p4c" / "synthetic-adapter-v1.json"
    assert path.is_file(), f"Missing mandatory synthetic fixture: {path}"
    try:
        value = C.load(path)
    except C.CorpusError as error:
        assert False, ("valid golden refused", str(error))
    verify_valid(value)


VALUES = (
    None, False, True, 0, 2**80, "", "quote \u00e9 \U0001f680", -0.0, float("inf"), float("nan"),
    Decimal("-0.00"), Decimal("1E+25"), Decimal("0.1234567890123456789012345678"),
    date(2026, 9, 25), time(9, 30, 0, 123, fold=1), timedelta(days=-1, microseconds=17),
    datetime(2026, 9, 25, 12, tzinfo=timezone.utc),
    datetime(2026, 11, 1, 1, 30, tzinfo=ZoneInfo("America/New_York"), fold=0),
    datetime(2026, 11, 1, 1, 30, tzinfo=ZoneInfo("America/New_York"), fold=1),
    b"\x00\xff", (1, "x"), [1, "x"], {1, 2}, frozenset((Side.BUY, Side.SELL)),
    {Equity("AAPL"): Decimal("1.00"), None: False}, MappingProxyType({"x": Decimal("1.00")}),
    Equity("AAPL"), OptionContract("SPX", date(2026, 9, 25), Decimal("100"), OptionRight.PUT),
)


@pytest.mark.parametrize("value", VALUES)
def test_lossless_typed_carriers(value):
    encoded = C.encode(value)
    rebuilt = C.decode(json.loads(C.canonical(encoded)))
    assert C.encode(rebuilt) == encoded
    assert type(rebuilt) is type(value)
    if isinstance(value, Decimal):
        assert str(rebuilt) == str(value)


@pytest.mark.parametrize("error", [ValueError("kind", "host error"), KeyError("missing"),
                                  TypeError("bad type"), RuntimeError("failure"), StaleDataError("no quote")])
def test_exact_error_type_arguments_message_and_cause(error):
    error.__cause__ = ValueError("cause")
    recorded = C.encode_error(error)
    rebuilt = C.decode_error(recorded)
    assert type(rebuilt) is type(error)
    assert rebuilt.args == error.args and str(rebuilt) == str(error)
    assert C.encode_error(rebuilt) == recorded


def test_strategy_context_and_folded_state_carriers_are_lossless():
    from trade_engine.eod.options import OptionContext
    from trade_engine.ledger.state import AccountState
    state = AccountState("SYNTHETIC", cash=Decimal("50000.00"),
        marks=MappingProxyType({Equity("AAPL"): Decimal("100.00")}))
    context = OptionContext(date(2026, 9, 25), "SYNTHETIC", "close",
        datetime(2026, 9, 25, 20, tzinfo=timezone.utc), state, ())
    encoded = C.encode(context)
    assert C.encode(C.decode(encoded)) == encoded


def test_observations_have_no_extra_clock_reads():
    calls = []
    recorder = C.Recorder()
    stamp = datetime(2026, 9, 25, tzinfo=timezone.utc)
    def now():
        calls.append("now")
        return stamp
    assert recorder.call("clock", "now_utc", now) == stamp
    assert calls == ["now"]
    tape = recorder.finish()
    replay = C.Replay(tape)
    assert replay.call("clock", "now_utc") == stamp
    replay.finish()


def test_unsupported_values_methods_and_nested_boundaries_fail_explicitly():
    with pytest.raises(C.CorpusError, match="Unsupported"):
        C.encode(object())
    recorder = C.Recorder()
    with pytest.raises(C.CorpusError):
        recorder.call("venue", "submit", lambda: object())
    assert recorder.tape == []
    with pytest.raises(C.CorpusError):
        recorder.call("venue", "connect_live", lambda: None)
    with pytest.raises(C.CorpusError):
        recorder.call("sink", "publish", lambda: recorder.call("clock", "now_utc", lambda: None))
    adapter = C.RecordingAdapter(C.Recorder(), "venue", object())
    with pytest.raises(C.CorpusError):
        adapter.connect_live()


def simple_tape():
    recorder = C.Recorder()
    recorder.call("venue", "positions", lambda: (Decimal("-0.00"),))
    recorder.call("source", "snapshot", lambda underlying, now: b"quote", "SPX", date(2026, 9, 25))
    return recorder.finish()


@pytest.mark.parametrize("mode", ["method", "actor", "args", "kwargs", "money-scale", "order", "extra", "missing"])
def test_replay_divergence_is_explicit_and_never_falls_back(mode):
    replay = C.Replay(simple_tape())
    with pytest.raises(C.CorpusError):
        if mode == "method":
            replay.call("venue", "fills")
        elif mode == "actor":
            replay.call("clock", "now_utc")
        elif mode == "args":
            replay.call("venue", "positions", "unexpected")
        elif mode == "kwargs":
            replay.call("venue", "positions", limit=1)
        elif mode == "money-scale":
            recorder = C.Recorder()
            recorder.call("venue", "submit", lambda value: None, Decimal("1.00"))
            C.Replay(recorder.finish()).call("venue", "submit", Decimal("1.0"))
        elif mode == "order":
            replay.call("source", "snapshot", "SPX", date(2026, 9, 25))
        elif mode == "extra":
            replay.call("venue", "positions")
            replay.call("source", "snapshot", "SPX", date(2026, 9, 25))
            replay.call("venue", "positions")
        else:
            replay.call("venue", "positions")
            replay.finish()
    good = C.Replay(simple_tape())
    assert str(good.call("venue", "positions")[0]) == "-0.00"
    assert good.call("source", "snapshot", "SPX", date(2026, 9, 25)) == b"quote"
    good.finish()


def test_record_and_replay_raised_host_exception():
    recorder = C.Recorder()
    def failure():
        raise RuntimeError("sink failed") from ValueError("network unavailable")
    with pytest.raises(RuntimeError):
        recorder.call("sink", "publish", failure)
    replay = C.Replay(recorder.finish())
    with pytest.raises(RuntimeError, match="sink failed") as caught:
        replay.call("sink", "publish")
    assert isinstance(caught.value.__cause__, ValueError)
    assert str(caught.value.__cause__) == "network unavailable"
    replay.finish()


@pytest.mark.parametrize("mode", ["version", "bool-version", "provenance", "source", "plugin", "rules", "config",
                                 "fixture-config", "duplicate-id", "role", "seed", "session", "inventory",
                                 "http-contract", "oracles", "initial-seq", "checkpoint", "empty", "http", "unknown-field"])
def test_manifest_refusal_and_success_counterparts(manifest, mode):
    bad = copy.deepcopy(manifest)
    fixture = bad["fixtures"][0]
    if mode == "version":
        bad["version"] = 2
    elif mode == "bool-version":
        bad["version"] = True
    elif mode == "provenance":
        bad["provenance"] = "recorded"
    elif mode in ("source", "plugin", "rules", "config"):
        bad[{"source": "source_release", "plugin": "plugin_sha256", "rules": "rules_sha256", "config": "config_sha256"}[mode]] = "0" * (40 if mode == "source" else 64)
    elif mode == "fixture-config":
        fixture["inputs"]["cash"] = "1.00"
        bad["config_sha256"] = C.digest([f["inputs"] for f in bad["fixtures"]])
    elif mode == "duplicate-id":
        bad["fixtures"][1]["id"] = fixture["id"]
    elif mode == "role":
        fixture["role"] = "unknown"
    elif mode == "seed":
        fixture["seed"] = False
    elif mode == "session":
        fixture["session"] = "2026-02-30"
    elif mode == "inventory":
        bad["inventory"].pop()
    elif mode == "http-contract":
        bad["http_contract"]["cursor_precedence"].reverse()
    elif mode == "oracles":
        bad["oracles"]["oracles"][0]["sha256"] = "0" * 64
    elif mode == "initial-seq":
        fixture["initial_events"][0]["seq"] = 2
    elif mode == "checkpoint":
        fixture["checkpoints"] = []
    elif mode == "empty":
        bad["fixtures"] = []
    elif mode == "http":
        fixture["http"] = [None]
    else:
        bad["unexpected"] = True
    with pytest.raises(C.CorpusError):
        C.validate_manifest(bad)
    C.validate_manifest(manifest)


@pytest.mark.parametrize("mode", ["missing-seq", "duplicate-seq", "bool-seq", "extra-field", "no-outcome",
                                 "two-outcomes", "unknown-method", "unknown-type", "bad-kwargs", "bad-args",
                                 "naive-clock", "backwards-clock", "exception-message"])
def test_tape_schema_and_chronology_refusal(mode):
    tape = simple_tape()
    frame = tape[0]
    if mode == "missing-seq":
        del frame["seq"]
    elif mode == "duplicate-seq":
        tape[1]["seq"] = 1
    elif mode == "bool-seq":
        frame["seq"] = True
    elif mode == "extra-field":
        frame["unexpected"] = 1
    elif mode == "no-outcome":
        frame["outcome"] = {}
    elif mode == "two-outcomes":
        frame["outcome"]["raise"] = C.encode_error(ValueError("no"))
    elif mode == "unknown-method":
        frame["method"] = "connect_live"
    elif mode == "unknown-type":
        frame["outcome"] = {"return": {"tag": "dataclass", "type": "os.system", "value": {}}}
    elif mode == "bad-kwargs":
        frame["kwargs"] = C.encode({1: "non-string"})
    elif mode == "bad-args":
        frame["args"] = C.encode([])
    elif mode in ("naive-clock", "backwards-clock"):
        first = datetime(2026, 9, 25, tzinfo=timezone.utc)
        frame["actor"], frame["method"] = "clock", "now_utc"
        frame["outcome"] = {"return": C.encode(first if mode == "backwards-clock" else first.replace(tzinfo=None))}
        tape[1] = dict(frame, seq=2, outcome={"return": C.encode(first - timedelta(seconds=1))})
    else:
        frame["outcome"] = {"raise": C.encode_error(ValueError("x"))}
        frame["outcome"]["raise"]["message"] = "different"
    with pytest.raises(C.CorpusError):
        C.Replay(tape)
    C.Replay(simple_tape())


@pytest.mark.parametrize("tree", [
    {"tag": "unknown", "value": 1},
    {"tag": "scalar", "value": 1.5},
    {"tag": "bytes", "value": "xx"},
    {"tag": "decimal", "value": "not-money"},
    {"tag": "datetime", "value": "bad", "zone": None, "fold": 0},
    {"tag": "datetime", "value": "2026-09-25", "zone": "America/New_York", "fold": 0},
    {"tag": "time", "value": "09:30", "fold": True},
    {"tag": "dataclass", "type": "trade_engine.intraday.service.Heartbeat", "value": {}},
    {"tag": "enum", "type": "trade_engine.domain.instruments.Side", "value": "UNKNOWN"},
    {"tag": "mapping", "value": [[C.encode("x"), C.encode(1)], [C.encode("x"), C.encode(2)]]},
])
def test_malformed_typed_values_refuse(tree):
    with pytest.raises(C.CorpusError):
        C.decode(tree)


@pytest.mark.parametrize("raw", ['{"version":', '{"version":1,"version":1}', '{"x":NaN}', "\ufeff{}"])
def test_torn_duplicate_nonfinite_manifest(tmp_path, raw):
    path = tmp_path / "bad.json"
    path.write_text(raw, encoding="utf-8")
    with pytest.raises(C.CorpusError):
        C.load(path)


def test_existing_goldens_cannot_be_overwritten_and_output_is_confined(manifest, tmp_path):
    path = tmp_path / "offline" / "fixture.json"
    C.write_new(manifest, path, tmp_path / "offline")
    before = path.read_bytes()
    with pytest.raises(FileExistsError):
        C.write_new(manifest, path, tmp_path / "offline")
    assert path.read_bytes() == before
    with pytest.raises(C.CorpusError):
        C.write_new(manifest, tmp_path / "outside.json", tmp_path / "offline")
    assert not (tmp_path / "outside.json").exists()
    with pytest.raises(C.CorpusError):
        W.confined(tmp_path, "..\\live.db")
    assert not (tmp_path.parent / "live.db").exists()


def test_network_boundaries_are_forbidden():
    with W.offline():
        with pytest.raises(C.CorpusError, match="network"):
            socket.create_connection(("127.0.0.1", 8097))
        with socket.socket() as connection:
            with pytest.raises(C.CorpusError, match="network"):
                connection.connect(("127.0.0.1", 8097))
            with pytest.raises(C.CorpusError, match="network"):
                connection.bind(("127.0.0.1", 3410))


def test_binary_and_import_absence_are_errors(tmp_path):
    with pytest.raises(C.CorpusError, match="mandatory"):
        C.require_artifacts(tmp_path / "missing.exe")
    with pytest.raises(ModuleNotFoundError):
        C.require_artifacts(C.ROOT / "crates" / "target" / "release" / "te.exe", "missing_p4c_native_module")
    C.require_artifacts(C.ROOT / "crates" / "target" / "release" / "te.exe")


def test_inventory_detects_plan_code_discrepancy(tmp_path):
    rows = C.inventory()
    assert len(rows) == 11 and sum(len(row["surfaces"]) for row in rows) == 15
    for row in rows:
        for surface in row["surfaces"]:
            parts = surface["path"].split("/")
            destination = tmp_path.joinpath(*parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(C.ROOT.joinpath(*parts).read_bytes())
    file = tmp_path / "src" / "trade_engine" / "cli.py"
    file.write_text("def unrelated(): pass\n", encoding="utf-8")
    with pytest.raises(C.CorpusError, match="Inventory discrepancy"):
        C.inventory(tmp_path)


def test_http_frozen_bytes_headers_cursor_and_error_shapes(manifest):
    http = manifest["fixtures"][0]["http"]
    bodies = [C.decode(row["outcome"]["return"]) for row in http if "return" in row["outcome"]]
    assert bodies[0].startswith(b"HTTP/1.0 200 OK\r\n")
    assert b"Date: <transport-Date>\r\n" in bodies[0]
    assert b"Content-Length:" in bodies[0]
    assert b'"count":2' in bodies[0]
    assert b"Access-Control-Allow-Origin: https://localhost:5555" in bodies[1]
    stream = C.decode(http[2]["outcome"]["return"])
    assert b"retry: 1000\n\n" in stream and b"id: 2\n" in stream and b"id: 1\n" not in stream
    assert b"400 Bad Request: 'after'" in C.decode(http[3]["outcome"]["return"])
    assert http[5]["outcome"]["raise"]["type"] == "builtins.ValueError"
    assert b"204 No Content" in C.decode(http[6]["outcome"]["return"])
    assert b"501 Unsupported method" in C.decode(http[7]["outcome"]["return"])
    assert b"403 Forbidden: Invalid Host header" in C.decode(http[8]["outcome"]["return"])
    assert b"404 Not Found" in C.decode(http[9]["outcome"]["return"])


@pytest.mark.parametrize("part", ["tape", "fold", "outbox", "meta", "http", "unused-source-value"])
def test_golden_results_must_match_frozen_recapture(manifest, part):
    bad = copy.deepcopy(manifest)
    fixture = bad["fixtures"][0]
    if part == "tape":
        fixture["tape"][0]["outcome"]["return"]["value"] = "2026-03-06T15:00:00+00:00"
    elif part == "unused-source-value":
        frame = next(f for f in fixture["tape"] if f["actor"] == "source" and f["method"] == "bars")
        bars = C.decode(frame["outcome"]["return"])
        price = Decimal("999.00")
        frame["outcome"]["return"] = C.encode(tuple(replace(bar, open=price, high=price, low=price, close=price) for bar in bars))
    elif part == "http":
        fixture["http"][0]["outcome"]["return"] = C.encode(b"different response")
    elif part == "fold":
        fixture["checkpoints"][0]["fold"] = "{}"
    else:
        fixture["checkpoints"][0][part] = C.encode((("different",),))
    with pytest.raises(C.CorpusError):
        W.verify(bad)


def test_cli_validation_and_explicit_failure(tmp_path):
    root = C.ROOT
    command = [sys.executable, "-B", str(root / "tools" / "p4c_corpus.py"), "validate"]
    golden = root / "tests" / "fixtures" / "p4c" / "synthetic-adapter-v1.json"
    good = subprocess.run([*command, str(golden)], cwd=root, capture_output=True, text=True, timeout=30)
    assert good.returncode == 0, (good.stdout, good.stderr)
    report = json.loads(good.stdout)
    assert report["recorded_sessions"] == 0 and report["completed_role_walks"] == 0
    assert report["release_certified"] is False
    bad = subprocess.run([*command, str(tmp_path / "missing.json")], cwd=root, capture_output=True, text=True, timeout=30)
    assert bad.returncode == 2
    assert "p4c corpus:" in bad.stderr and not bad.stdout

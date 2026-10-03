"""P2a gate 2: the Rust ledger fold against the Python fold (docs/RUST_PORT.md).

Three sources of event streams, each folded by BOTH implementations; the canonical JSON of
the whole `AccountState` (positions, lots, orders, fills, marks, the mirror book ...) must
be identical, and a refusal must be identical in kind AND message:

* the zoo (one event per EventKind), every prefix, per account;
* the streams the existing suites fold, recorded by `ledger_record_plugin` while those
  suites run unchanged (store round trips, combos, lifecycle, OMS, the TOS mirror ...);
* a seeded generator of interleaved episodes (partial fills, combos, expiry / exercise /
  assignment, cash flows, marks, halts, risk controls, the mirror), a third of them
  perturbed so the refusal paths run too.

A stream Rust refuses as `strict` / `unsupported` while Python folds it is the sanctioned
codec asymmetry (see test_ledger_codec_parity); it is counted and bounded.
"""

import collections
import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "src"))

import pytest
import trade_engine_rs as rs  # a missing module is an ERROR, never a skip (D5)
from ledger_gen import (
    encoded,
    event_zoo,
    py_fold,
    py_fold_all,
    random_stream,
)

from trade_engine.ledger import codec
from trade_engine.ledger.events import EventKind

STRICT = {"strict", "unsupported"}
SEEDS = 1500

# the suites whose folds are replayed (every test file that folds a ledger)
RECORDED_SUITES = [
    "test_ledger.py", "test_mirror_ledger.py", "test_combo_fold.py", "test_option_lifecycle.py",
    "test_oms.py", "test_option_oms.py", "test_eod_options.py", "test_eod_runner.py",
    "test_intraday_service.py", "test_margin_parity.py", "test_metrics_margin.py",
    "test_option_margin.py", "test_option_risk.py", "test_risk_engine.py", "test_risk_parity.py",
    "test_tos_broker.py", "test_tos_mirror.py", "test_tos_mirror_exits.py", "test_tos_mirror_halt.py",
    "test_tos_reconcile.py",
    "test_tos_stock.py", "test_tos_unavailable.py", "test_tos_cover.py", "test_tos_cover_session.py",
    "test_tos_netting.py",
]


def rs_fold(blobs, account):
    try:
        return ("ok", bytes(rs.ledger_fold(blobs, account)))
    except ValueError as err:
        if len(err.args) != 2:
            raise
        return ("err", err.args[0], err.args[1])


def rs_fold_all(blobs):
    try:
        return ("ok", bytes(rs.ledger_fold_all(blobs)))
    except ValueError as err:
        if len(err.args) != 2:
            raise
        return ("err", err.args[0], err.args[1])


class Tally:
    def __init__(self):
        self.ok = 0
        self.refused = 0
        self.strict = 0
        self.kinds = collections.Counter()

    def total(self):
        return self.ok + self.refused + self.strict


def compare(py, rust, what, tally):
    """Rust must equal Python: same bytes, or same refusal kind and message."""
    if py[0] == "ok" and rust[0] == "ok":
        assert rust[1] == py[1], f"state differs for {what}\npy:   {py[1]!r}\nrust: {rust[1]!r}"
        tally.ok += 1
        return
    if py[0] == "ok":
        assert rust[1] in STRICT, f"Rust refused what Python folds ({rust}) for {what}"
        tally.strict += 1
        return
    assert rust[0] == "err", f"Rust FOLDED what Python refuses ({py}) for {what}"
    if rust[1] in STRICT:
        tally.strict += 1
        return
    assert (rust[1], rust[2]) == (py[1], py[2]), f"refusal differs for {what}\npy:   {py}\nrust: {rust}"
    tally.refused += 1
    tally.kinds[py[1]] += 1


def accounts_of(events):
    seen = []
    for e in events:
        if e.account not in seen:
            seen.append(e.account)
    return seen


def check_stream(events, tally, what):
    """Per-account `fold_account` and the all-accounts `fold`, both implementations."""
    blobs = encoded(events)
    for account in accounts_of(events):
        compare(py_fold(events, account), rs_fold(blobs, account), f"{what} account={account}", tally)
    compare(py_fold_all(events), rs_fold_all(blobs), f"{what} fold-all", tally)


# --- the zoo -------------------------------------------------------------------------


def test_zoo_every_prefix_per_account_matches():
    tally = Tally()
    zoo = event_zoo()
    for account in accounts_of(zoo):
        sub = [e for e in zoo if e.account == account]
        for n in range(1, len(sub) + 1):
            compare(py_fold(sub[:n], account), rs_fold(encoded(sub[:n]), account), f"zoo {account}[:{n}]", tally)
    assert tally.ok > 5 and tally.refused > 5
    assert tally.strict == 0


def test_an_empty_log_folds_to_the_default_state():
    tally = Tally()
    compare(py_fold([], "ACC"), rs_fold([], "ACC"), "empty", tally)
    assert tally.ok == 1


def test_corporate_action_is_refused_as_unhandled_with_the_same_message():
    zoo = [e for e in event_zoo() if e.kind is EventKind.CORPORATE_ACTION]
    assert len(zoo) == 1
    py = py_fold(zoo, zoo[0].account)
    rust = rs_fold(encoded(zoo), zoo[0].account)
    assert py[0] == "err" and py[1] == "unhandled"
    assert rust == py


def test_an_undecodable_event_is_a_refusal_not_a_state():
    assert rs_fold([b"not json"], "ACC")[0] == "err"
    assert rs_fold([b"{}"], "ACC")[0] == "err"


# --- the seeded generator ------------------------------------------------------------


def test_random_streams_fold_identically():
    tally = Tally()
    covered = set()
    for seed in range(SEEDS):
        events, _ = random_stream(seed)
        check_stream(events, tally, f"seed={seed}")
        for account in accounts_of(events):
            if py_fold(events, account)[0] == "ok":
                covered |= {e.kind for e in events if e.account == account}
    assert tally.strict == 0
    # a majority of the work is real folds, and the refusal paths run too
    assert tally.ok > tally.total() * 0.6, (tally.ok, tally.total())
    assert tally.refused > 100
    assert {"fold", "duplicate_fill"} <= set(tally.kinds)
    # every EventKind but the (always refused) CorporateAction folded in an accepted stream
    assert covered == set(EventKind) - {EventKind.CORPORATE_ACTION}, set(EventKind) - covered - {
        EventKind.CORPORATE_ACTION
    }


# --- the existing suites' streams ----------------------------------------------------


@pytest.fixture(scope="module")
def recorded(tmp_path_factory):
    """Run the suites that fold ledgers, unchanged, with the recording plugin."""
    out = tmp_path_factory.mktemp("rec") / "streams.jsonl"
    out.write_text("")
    env = dict(os.environ, LEDGER_RECORD=str(out), PYTHONPATH=str(HERE), PYTHONDONTWRITEBYTECODE="1")
    env.pop("PYTEST_CURRENT_TEST", None)
    files = [str(HERE / name) for name in RECORDED_SUITES]
    run = subprocess.run(
        [sys.executable, "-B", "-m", "pytest", "-q", "-p", "ledger_record_plugin", "-p", "no:cacheprovider", *files],
        env=env, capture_output=True, text=True, cwd=str(HERE.parent),
    )
    assert run.returncode == 0, run.stdout[-2000:] + run.stderr[-2000:]
    return [json.loads(line) for line in out.read_text(encoding="utf8").splitlines() if line]


def test_the_suites_streams_fold_identically(recorded):
    assert len(recorded) > 200, "the recorder captured too few streams to mean anything"
    tally = Tally()
    kinds_folded = set()
    for i, rec in enumerate(recorded):
        blobs = [s.encode() for s in rec["events"]]
        events = [codec.decode_event(json.loads(s)) for s in rec["events"]]
        what = f"recorded[{i}] {rec['kind']}"
        if rec["kind"] == "fold_account":
            py = py_fold(events, rec["account"])
            compare(py, rs_fold(blobs, rec["account"]), what, tally)
            if py[0] == "ok":
                kinds_folded |= {e.kind for e in events if e.account == rec["account"]}
        else:
            py = py_fold_all(events)
            compare(py, rs_fold_all(blobs), what, tally)
            if py[0] == "ok":
                kinds_folded |= {e.kind for e in events}
    assert tally.strict == 0
    assert tally.ok > 150
    # the real suites exercise fills, lifecycle, the mirror and venue events
    assert {EventKind.FILL, EventKind.EXPIRY, EventKind.MIRROR_FILL, EventKind.MIRROR_ACK,
            EventKind.ORDER_SUBMITTED} <= kinds_folded

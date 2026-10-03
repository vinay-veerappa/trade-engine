"""A pytest plugin (`-p ledger_record_plugin`) that records every event stream the suite
folds, for the P2a/P2b fold parity test. It changes nothing: the original callables still
run and return their result; each call's input is appended, encoded by the FROZEN oracle
codec (`tests/frozen_ledger`, P2b), to the file named by `LEDGER_RECORD`. Streams the codec
cannot encode (a deliberately malformed event in a negative test) are counted and skipped.

Since P2b the store no longer refolds through `fold_account` (it holds the fold in Rust and
applies each append once), so `Ledger.state` / `LedgerReader.state` are recorded too: the
account's whole committed log at the moment of the read, as a `fold_account` stream.
Identical streams are written once.
"""

import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))
sys.path.insert(0, str(HERE))

_ORIG = {}
_DEPTH = [0]
_SKIPPED = [0]
_SEEN = set()


def _record(kind, events, account):
    from frozen_ledger import codec

    try:
        enc = [json.dumps(codec.encode_event(e), sort_keys=True, separators=(",", ":")) for e in events]
    except Exception:  # noqa: BLE001 - an unencodable event is a negative test's input
        _SKIPPED[0] += 1
        return
    line = json.dumps({"kind": kind, "account": account, "events": enc})
    if line in _SEEN:
        return
    _SEEN.add(line)
    with open(os.environ["LEDGER_RECORD"], "a", encoding="utf8") as fh:
        fh.write(line + "\n")


def _wrap(name, orig):
    def wrapper(events, *args, **kw):
        events = list(events)
        if _DEPTH[0] == 0:
            account = args[0] if args else kw.get("account")
            _record(name, events, account)
        _DEPTH[0] += 1
        try:
            return orig(events, *args, **kw)
        finally:
            _DEPTH[0] -= 1

    wrapper.__wrapped__ = orig
    return wrapper


def _wrap_state(orig, store):
    def state(self, account):
        result = orig(self, account)  # the read itself first: a refusal records nothing
        if _DEPTH[0] == 0:
            _DEPTH[0] += 1
            try:
                _record("fold_account", list(store.Ledger.events(self, account=account)), account)
            finally:
                _DEPTH[0] -= 1
        return result

    state.__wrapped__ = orig
    return state


def pytest_collection_finish(session):
    if "LEDGER_RECORD" not in os.environ:
        return
    from trade_engine.ledger import reader, state, store

    for name in ("fold", "fold_account"):
        orig = getattr(state, name)
        _ORIG[name] = orig
        new = _wrap(name, orig)
        for mod in list(sys.modules.values()):
            if mod is not None and getattr(mod, name, None) is orig:
                setattr(mod, name, new)
    for cls in (store.Ledger, reader.LedgerReader):
        cls.state = _wrap_state(cls.state, store)

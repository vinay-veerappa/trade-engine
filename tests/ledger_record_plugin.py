"""A pytest plugin (`-p ledger_record_plugin`) that records every event stream the suite
folds, for the P2a fold parity test. It changes nothing: the original `fold` /
`fold_account` still run and return their result; each call's input is appended, encoded
by the production codec, to the file named by `LEDGER_RECORD`. Streams the codec cannot
encode (a deliberately malformed event in a negative test) are counted and skipped.
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

_ORIG = {}
_DEPTH = [0]
_SKIPPED = [0]


def _record(kind, events, account):
    from trade_engine.ledger import codec

    try:
        enc = [json.dumps(codec.encode_event(e), sort_keys=True, separators=(",", ":")) for e in events]
    except Exception:  # noqa: BLE001 - an unencodable event is a negative test's input
        _SKIPPED[0] += 1
        return
    with open(os.environ["LEDGER_RECORD"], "a", encoding="utf8") as fh:
        fh.write(json.dumps({"kind": kind, "account": account, "events": enc}) + "\n")


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


def pytest_collection_finish(session):
    if "LEDGER_RECORD" not in os.environ:
        return
    from trade_engine.ledger import state

    for name in ("fold", "fold_account"):
        orig = getattr(state, name)
        _ORIG[name] = orig
        new = _wrap(name, orig)
        for mod in list(sys.modules.values()):
            if mod is not None and getattr(mod, name, None) is orig:
                setattr(mod, name, new)

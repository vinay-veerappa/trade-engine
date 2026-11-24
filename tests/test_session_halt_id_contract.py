"""The reconcile command id is contract: it is replayed against existing ledgers (I3).

The Rust door must derive it byte-for-byte as the retired pre-port session module's ``_reconcile`` did
(now ``tos_paper.session_native._reconcile``):
sha256 over compact sorted-key JSON of the encoded payload, first 16 hex chars.
"""

import hashlib
import json
from datetime import datetime, timezone

import pytest

from trade_engine.ledger import codec
from trade_engine.ledger.events import VenueReconcile
from trade_engine.tos_paper import _rs

_CASES = [
    VenueReconcile(venue="paper-venue", as_of=datetime(2026, 1, 2, 10, 30, tzinfo=timezone.utc), reconciled=True),
    VenueReconcile(
        venue="paper-venue",
        as_of=datetime(2026, 1, 2, 10, 30, 15, 123456, tzinfo=timezone.utc),
        reconciled=False,
        drift=("SPY", "QQQ"),
        note='unicode é "q"',
    ),
]


@pytest.mark.parametrize("event", _CASES)
@pytest.mark.parametrize("phase", ["collect", "unproven:morning"])
def test_reconcile_command_id_matches_the_retired_derivation(event, phase):
    digest = hashlib.sha256(
        json.dumps(codec.encode_payload(event), sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:16]
    expected = f"mirror:{event.venue}:reconcile:{phase}:{event.as_of.isoformat()}:{digest}"
    command = _rs.call(_rs.rs.session_reconcile_command_id, event.venue, phase, event.as_of, event)
    assert command == expected

"""The mirror's fill and book command ids are idempotency keys already stored in live ledgers.

``session_collect_items`` must spell ``filled`` exactly as ``str(Decimal)`` does (not the codec's
canon text), or a fill already booked under the old id would book a second time.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from trade_engine.tos_paper import _rs

VENUE = "PM-A"


@pytest.mark.parametrize("text", ["2.50", "1E+1", "0.000001", "3"])
def test_fill_id_is_str_decimal(text: str) -> None:
    filled = Decimal(text)
    ids = _rs.call(_rs.rs.session_collect_items, VENUE, [("k1", str(filled))], [])
    assert ids == [f"mirror:{VENUE}:fill:k1:{filled}"]


def test_fills_come_before_closes_in_input_order() -> None:
    ids = _rs.call(
        _rs.rs.session_collect_items,
        VENUE,
        [("a", "1"), ("b", "2.0")],
        [("c", "CANCELLED")],
    )
    assert ids == [f"mirror:{VENUE}:fill:a:1", f"mirror:{VENUE}:fill:b:2.0", f"mirror:{VENUE}:book:c:CANCELLED"]


def test_close_without_book_status_refuses() -> None:
    with pytest.raises(ValueError):
        _rs.call(_rs.rs.session_collect_items, VENUE, [], [("c", None)])

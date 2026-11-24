"""Behaviour tests for the journal sink, asserting explicit values through the public API.

Replaces the lockstep oracle comparison in `test_p4b_parity.py`. Every HTTP exchange goes
through an injected `http_client` fake: nothing here opens a socket. The fake answers the
journal's routes the way the journal does, and a test scripts a single route to fail when it
wants to reach one specific branch.
"""

from __future__ import annotations

import json
import re
import urllib.parse
from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal as D, InvalidOperation
from typing import Any

import pytest

from trade_engine.domain.instruments import Side
from trade_engine.domain.option_roots import SettleTime
from trade_engine.interfaces.sinks import JournalExecution
from trade_engine.lifecycle import sources as LS
from trade_engine.sinks import journal as J
from trade_engine.sinks.journal import HttpJournalSink, JournalAuthError

BASE = "http://journal.test"
ACCOUNT = "journal-é"
AT = datetime(2026, 10, 16, 20, 0, tzinfo=timezone.utc)
AFTER = datetime(2026, 10, 16, 21, 45, tzinfo=timezone.utc)
EXPIRY = date(2026, 10, 16)


def execution(**changes: Any) -> JournalExecution:
    base = JournalExecution(
        symbol="AAPL",
        side=Side.BUY,
        quantity=D("2.00"),
        price=D("101.010"),
        fee=D("0.25"),
        executed_at=AT,
        account_id=ACCOUNT,
        asset_class="equity",
    )
    return replace(base, **changes)


class FakeJournal:
    """The journal's HTTP API as a plain function. Healthy by default, with one open AAPL trade.

    `overrides` maps (method, path) to a (status, body) answer or to an exception to raise.
    Every request is recorded in `calls` as (method, url-with-query, body).
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Any]] = []
        self.overrides: dict[tuple[str, str], Any] = {}
        self.multipliers: dict[str, int] = {}
        self.post_body: dict[str, Any] = {"inserted": 1}
        self.landing_key = "t1"
        self.trades: list[dict[str, Any]] = [{"key": "t1", "symbol": "AAPL", "status": "open"}]
        # Per trade: its executions, and its `trade` row (annotations and tags as JSON text).
        self.details: dict[str, dict[str, Any]] = {"t1": {"executions": [], "trade": {"tagsJson": "[]"}}}
        self.patches: list[tuple[str, Any]] = []

    def __call__(self, url: str, method: str, body: Any) -> tuple[int, dict[str, Any]]:
        self.calls.append((method, url, body))
        path = urllib.parse.urlsplit(url).path
        scripted = self.overrides.get((method, path))
        if isinstance(scripted, BaseException):
            raise scripted
        if scripted is not None:
            return scripted
        if path == "/api/settings":
            if method == "PATCH":
                self.multipliers = dict(body["multipliers"])
                self.patches.append((path, body))
                return 200, {}
            return 200, {"multipliers": dict(self.multipliers)}
        if path == "/api/executions":
            landing = self.details.setdefault(self.landing_key, {"executions": [], "trade": {"tagsJson": "[]"}})
            landing["executions"].append(dict(body["executions"][0]))
            return 200, dict(self.post_body)
        if path == "/api/trades":
            return 200, {"trades": [dict(row) for row in self.trades]}
        key = urllib.parse.unquote(path.rsplit("/", 1)[-1])
        if method == "PATCH":
            self.patches.append((path, body))
            trade = self.details[key]["trade"]
            for field in ("stopLoss", "profitTarget"):
                if field in body:
                    trade[field] = body[field]
            if "tags" in body:
                trade["tagsJson"] = json.dumps(body["tags"])
            return 200, {}
        if key not in self.details:
            return 404, {}
        record = self.details[key]
        trade = dict(record["trade"], contractMultiplier=self.multipliers.get("AAPL"))
        return 200, {"trade": trade, "executions": list(record["executions"])}

    def methods(self) -> list[str]:
        return [method for method, _, _ in self.calls]

    def paths(self) -> list[str]:
        return [urllib.parse.urlsplit(url).path for _, url, _ in self.calls]


@pytest.fixture
def fake() -> FakeJournal:
    return FakeJournal()


@pytest.fixture
def sink(fake: FakeJournal) -> HttpJournalSink:
    return HttpJournalSink(BASE, ACCOUNT, http_client=fake)


# --- JournalExecution refusals (interfaces/sinks.py 36-50) -------------------------------


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"symbol": "   "}, "symbol must be non-empty string"),
        ({"side": "BUY"}, "side must be Side enum, got BUY"),
        ({"quantity": D("0")}, "quantity must be positive, got 0"),
        ({"price": D("0")}, "price must be positive, got 0"),
        ({"account_id": " "}, "account_id must be non-empty string from config (I5)"),
        ({"asset_class": ""}, "asset_class must be non-empty string"),
        ({"multiplier": 0}, "multiplier must be positive integer"),
        ({"executed_at": AT.replace(tzinfo=None)}, "executed_at must be timezone-aware UTC datetime (I7)"),
    ],
)
def test_journal_execution_refuses_each_invalid_field(changes: dict[str, Any], message: str) -> None:
    with pytest.raises(ValueError, match=re.escape(message)):
        execution(**changes)


def test_journal_execution_accepts_a_valid_fill() -> None:
    e = execution(asset_class="option", multiplier=100)
    assert e.multiplier == 100
    assert e.account_id == ACCOUNT


# --- stamp, parse, and tag helpers (sinks/journal.py 42-54) ------------------------------


def test_executed_at_carries_the_ledger_seq_in_sub_millisecond_digits() -> None:
    assert J.journal_executed_at(AT, 7) == "2026-10-16T20:00:00.000007+00:00"


def test_instant_parsing_accepts_utc_z_and_refuses_garbage() -> None:
    assert J._parse_instant("2026-10-16T20:00:00Z") == AT
    assert J._parse_instant(None) is None
    assert J._parse_instant("garbage") is None


@pytest.mark.parametrize(
    ("trade", "expected"),
    [
        ({"tags": ["a", "b"]}, ["a", "b"]),
        ({"tags": []}, []),
        # An explicit `tags` list, even empty, takes precedence over `tagsJson`.
        ({"tags": [], "tagsJson": '["ignored"]'}, []),
        ({"tagsJson": '["策略", "x"]'}, ["策略", "x"]),
        ({"tagsJson": "bad"}, []),
        ({"tagsJson": "{}"}, []),
        ({"tagsJson": "null"}, []),
        ({}, []),
    ],
)
def test_trade_tags_come_from_tags_or_the_tags_json_string(trade: dict[str, Any], expected: list[str]) -> None:
    assert J._trade_tags(trade) == expected


# --- payload mapping (_dict_to_execution) ------------------------------------------------


def _payload(**changes: Any) -> dict[str, Any]:
    base = dict(
        symbol="AAPL",
        side="BUY",
        quantity="1.00",
        price="2.00",
        fee="0",
        executed_at=AFTER.isoformat(),
        account_id=ACCOUNT,
        asset_class="option",
        multiplier=100,
    )
    base.update(changes)
    return base


def test_payload_maps_to_the_execution_it_describes(sink: HttpJournalSink) -> None:
    mapped = sink._dict_to_execution(_payload())
    assert mapped == JournalExecution(
        symbol="AAPL",
        side=Side.BUY,
        quantity=D("1.00"),
        price=D("2.00"),
        fee=D("0"),
        executed_at=AFTER,
        account_id=ACCOUNT,
        asset_class="option",
        multiplier=100,
    )


@pytest.mark.parametrize(
    ("changes", "error", "message"),
    [
        ({"symbol": None}, ValueError, "Missing required field 'symbol' in execution payload (I5)"),
        ({"side": None}, ValueError, "Missing required field 'side' in execution payload (I5)"),
        ({"side": "boGus"}, ValueError, "'BOGUS' is not a valid Side"),
        ({"side": 5}, ValueError, "Invalid side: 5"),
        (
            {"executed_at": "2026-10-16T20:00:00"},
            ValueError,
            "executed_at must be timezone-aware UTC datetime (I7)",
        ),
        ({"quantity": "bad"}, InvalidOperation, None),
        ({"quantity": "0"}, ValueError, "quantity must be positive, got 0"),
        ({"multiplier": "bad"}, ValueError, "invalid literal for int() with base 10: 'bad'"),
        ({"account_id": ""}, ValueError, "account_id must be non-empty string from config (I5)"),
    ],
)
def test_payload_refusals_name_the_bad_field(
    sink: HttpJournalSink, changes: dict[str, Any], error: type[Exception], message: str | None
) -> None:
    with pytest.raises(error, match=None if message is None else re.escape(message)):
        sink._dict_to_execution(_payload(**changes))


# --- publish_execution: the delivery path ------------------------------------------------


def test_equity_fill_is_posted_and_confirmed_by_readback(sink: HttpJournalSink, fake: FakeJournal) -> None:
    assert sink.publish_execution(7, execution()) is True
    # No multiplier to set for an equity, so the order is post, list trades, read the detail.
    assert fake.methods() == ["POST", "GET", "GET"]
    assert fake.paths() == ["/api/executions", "/api/trades", "/api/trades/t1"]
    posted = json.dumps(fake.calls[0][2])
    assert "2026-10-16T20:00:00.000007" in posted


def test_option_fill_sets_its_multiplier_first_and_keeps_other_symbols(
    sink: HttpJournalSink, fake: FakeJournal
) -> None:
    fake.multipliers = {"OTHER": 50}
    assert sink.publish_execution(7, execution(asset_class="option", multiplier=100)) is True
    # The multiplier is written, then read back, before the post, so the journal prices the fill on insert.
    assert fake.methods() == ["GET", "PATCH", "GET", "POST", "GET", "GET"]
    assert fake.patches == [("/api/settings", {"multipliers": {"OTHER": 50, "AAPL": 100}})]
    assert fake.multipliers == {"OTHER": 50, "AAPL": 100}


def test_multiplier_already_recorded_is_not_written_again(sink: HttpJournalSink, fake: FakeJournal) -> None:
    fake.multipliers = {"AAPL": 100}
    assert sink.publish_execution(7, execution(asset_class="option", multiplier=100)) is True
    assert fake.patches == []
    assert fake.methods() == ["GET", "POST", "GET", "GET"]


def test_refused_multiplier_write_stops_before_posting(sink: HttpJournalSink, fake: FakeJournal) -> None:
    fake.multipliers = {"OTHER": 50}
    fake.overrides[("PATCH", "/api/settings")] = (500, {})
    assert sink.publish_execution(7, execution(asset_class="option", multiplier=100)) is False
    assert fake.methods() == ["GET", "PATCH"]


def test_multipliers_that_cannot_be_read_are_never_written(sink: HttpJournalSink, fake: FakeJournal) -> None:
    fake.overrides[("GET", "/api/settings")] = OSError("offline")
    assert sink.publish_execution(7, execution(asset_class="option", multiplier=100)) is False
    assert fake.methods() == ["GET"]


def test_auth_refusal_on_the_settings_read_propagates(sink: HttpJournalSink, fake: FakeJournal) -> None:
    fake.overrides[("GET", "/api/settings")] = (401, {})
    with pytest.raises(JournalAuthError, match="refused GET /api/settings with 401"):
        sink.publish_execution(7, execution(asset_class="option", multiplier=100))
    assert fake.methods() == ["GET"]


def test_auth_refusal_on_the_post_propagates(sink: HttpJournalSink, fake: FakeJournal) -> None:
    fake.overrides[("POST", "/api/executions")] = (401, {})
    with pytest.raises(JournalAuthError, match="refused POST /api/executions with 401"):
        sink.publish_execution(7, execution())


def test_post_the_journal_does_not_accept_is_not_delivered(sink: HttpJournalSink, fake: FakeJournal) -> None:
    fake.overrides[("POST", "/api/executions")] = (300, {})
    assert sink.publish_execution(7, execution()) is False
    assert fake.methods() == ["POST"]


def test_network_failure_on_the_post_keeps_the_item_queued(sink: HttpJournalSink, fake: FakeJournal) -> None:
    fake.overrides[("POST", "/api/executions")] = OSError("offline")
    assert sink.publish_execution(7, execution()) is False
    assert fake.methods() == ["POST"]


def test_a_post_with_no_inserted_or_duplicate_count_is_not_stored(
    sink: HttpJournalSink, fake: FakeJournal
) -> None:
    fake.post_body = {}
    assert sink.publish_execution(7, execution()) is False
    assert fake.methods() == ["POST"]


def test_a_duplicate_post_is_delivered_once_the_readback_finds_the_row(
    sink: HttpJournalSink, fake: FakeJournal
) -> None:
    fake.post_body = {"duplicates": 1}
    assert sink.publish_execution(7, execution()) is True


def test_a_failed_trade_list_is_not_confirmed(sink: HttpJournalSink, fake: FakeJournal) -> None:
    fake.overrides[("GET", "/api/trades")] = (500, {})
    assert sink.publish_execution(7, execution()) is False
    assert fake.methods() == ["POST", "GET"]


def test_a_failed_trade_detail_is_skipped_and_the_next_candidate_confirms(
    sink: HttpJournalSink, fake: FakeJournal
) -> None:
    # Candidates are tried newest first: "new" is last in the list, so it comes first.
    fake.trades = [
        {"key": "old", "symbol": "AAPL", "status": "open"},
        {"key": "new", "symbol": "AAPL", "status": "closed"},
    ]
    fake.details = {
        "old": {"executions": [], "trade": {"tagsJson": "[]"}},
        "new": {"executions": [], "trade": {"tagsJson": "[]"}},
    }
    fake.landing_key = "old"
    fake.overrides[("GET", "/api/trades/new")] = (500, {})
    assert sink.publish_execution(7, execution()) is True
    detail_paths = [p for p in fake.paths() if p.startswith("/api/trades/")]
    assert detail_paths == ["/api/trades/new", "/api/trades/old"]


def test_a_trade_list_that_throws_is_not_delivery(sink: HttpJournalSink, fake: FakeJournal) -> None:
    fake.overrides[("GET", "/api/trades")] = OSError("reset")
    assert sink.publish_execution(7, execution()) is False


def test_auth_refusal_on_the_readback_propagates(sink: HttpJournalSink, fake: FakeJournal) -> None:
    fake.overrides[("GET", "/api/trades")] = (401, {})
    with pytest.raises(JournalAuthError, match="refused GET /api/trades"):
        sink.publish_execution(7, execution())


def test_stop_target_and_tag_are_patched_onto_the_matching_trade(sink: HttpJournalSink, fake: FakeJournal) -> None:
    e = execution(stop_loss=D("98"), profit_target=D("110"), strategy_tag="s")
    assert sink.publish_execution(7, e) is True
    assert fake.methods() == ["POST", "GET", "PATCH", "GET", "GET"]
    trade_patches = [(path, body) for path, body in fake.patches if path == "/api/trades/t1"]
    assert len(trade_patches) == 1
    assert trade_patches[0][1] == {"stopLoss": 98, "profitTarget": 110, "tags": ["s"]}


def test_a_failed_annotation_read_is_left_to_the_readback(sink: HttpJournalSink, fake: FakeJournal) -> None:
    # The annotation read fails and the read-back fails with it, so delivery is not confirmed.
    fake.overrides[("GET", "/api/trades")] = (500, {})
    assert sink.publish_execution(7, execution(strategy_tag="s")) is False
    assert "PATCH" not in fake.methods()


def test_annotation_without_a_matching_trade_patches_nothing(sink: HttpJournalSink, fake: FakeJournal) -> None:
    fake.trades = [{"key": "msft", "symbol": "MSFT", "status": "open"}]
    assert sink.publish_execution(7, execution(strategy_tag="s")) is False
    assert "PATCH" not in fake.methods()
    assert fake.methods() == ["POST", "GET", "GET"]


def test_publish_accepts_a_dict_event_through_the_mapping(sink: HttpJournalSink, fake: FakeJournal) -> None:
    assert sink.publish(7, _payload(asset_class="option", multiplier=1)) is True


def test_publish_refuses_an_unknown_event_type_without_a_request(sink: HttpJournalSink, fake: FakeJournal) -> None:
    assert sink.publish(7, "not an event") is False
    assert fake.calls == []


def test_a_fill_for_another_account_is_refused_before_any_request(
    sink: HttpJournalSink, fake: FakeJournal
) -> None:
    assert sink.publish_execution(7, execution(account_id="another-account")) is False
    assert fake.calls == []


def test_confirm_without_an_execution_is_false(sink: HttpJournalSink, fake: FakeJournal) -> None:
    assert sink.confirm_delivery(7) is False
    assert fake.calls == []


# --- lifecycle sources refusals (lifecycle/sources.py). Kept here by decision; see report. -


def test_dividend_refuses_non_positive_amount_naive_time_and_empty_source() -> None:
    with pytest.raises(ValueError, match=re.escape("Dividend.amount must be a positive Decimal, got Decimal('0') (I5)")):
        LS.Dividend("AAPL", EXPIRY, D("0"), "known", AFTER)
    with pytest.raises(ValueError, match=re.escape("Dividend.as_of must be timezone-aware UTC (I7)")):
        LS.Dividend("AAPL", EXPIRY, D("1.00"), "known", AFTER.replace(tzinfo=None))
    with pytest.raises(ValueError, match=re.escape("Dividend.source must be non-empty (I11)")):
        LS.Dividend("AAPL", EXPIRY, D("1.00"), "", AFTER)


def test_settlement_price_accepts_a_positive_decimal() -> None:
    assert LS.SettlementPrice("AAPL", EXPIRY, SettleTime.PM, D("1.00"), "known", AFTER).price == D("1.00")


@pytest.mark.parametrize(
    ("underlying", "price", "as_of", "message"),
    [
        ("", D("1.00"), AFTER, "SettlementPrice.underlying must be non-empty"),
        ("AAPL", D("Infinity"), AFTER, "SettlementPrice.price must be a positive Decimal, got Decimal('Infinity') (I5)"),
        ("AAPL", D("0"), AFTER, "SettlementPrice.price must be a positive Decimal, got Decimal('0') (I5)"),
        ("AAPL", 1, AFTER, "SettlementPrice.price must be a positive Decimal, got 1 (I5)"),
        ("AAPL", D("1.00"), AFTER.replace(tzinfo=None), "SettlementPrice.as_of must be timezone-aware UTC (I7)"),
    ],
)
def test_settlement_price_refusals(underlying: str, price: Any, as_of: datetime, message: str) -> None:
    with pytest.raises(ValueError, match=re.escape(message)):
        LS.SettlementPrice(underlying, EXPIRY, SettleTime.PM, price, "known", as_of)

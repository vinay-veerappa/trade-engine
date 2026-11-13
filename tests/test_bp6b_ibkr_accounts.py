"""BP-6b: the IBKR accounts adapter (BROKER_PLATFORM phase 6b).

Fixtures only; no TWS, no Client Portal. Every account id here is fake
(``ACCT-TEST-*``); the mask is the only form a test reads back from a record.
"""

from __future__ import annotations

import ast
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from trade_engine.accounts.errors import AccountRuleError
from trade_engine.ibkr.adapter import MAX_TEXT_BYTES, IbkrAccountsAdapter
from trade_engine.interfaces.broker import UnsupportedCapability

NOW = datetime(2026, 11, 11, 18, 0, tzinfo=timezone.utc)

TWS_TEXT = "\n".join(
    [
        "managedAccounts\tACCT-TEST-194,DUACCT-TEST-195,ACCT-TEST-196",
        "updateAccountValue\tNetLiquidation\t250000.00\tUSD\tACCT-TEST-194",
        "updateAccountValue\tAccountType\tMARGIN\tUSD\tACCT-TEST-194",
        "updateAccountValue\tAccountType\tCASH\tUSD\tDUACCT-TEST-195",
        "",
    ]
)

PORTAL_TEXT = (
    '[{"accountId":"ACCT-TEST-194","accountType":"MARGIN","currency":"USD"},'
    '{"accountId":"DUACCT-TEST-195","accountType":"IRA","currency":"USD"},'
    '{"accountId":"ACCT-TEST-196","currency":"USD"}]'
)


class FakeClock:
    def __init__(self, now: datetime = NOW) -> None:
        self.now = now

    def now_utc(self) -> datetime:
        return self.now


def ibkr(clock: FakeClock | None = None, *, enabled: bool = True) -> IbkrAccountsAdapter:
    return IbkrAccountsAdapter(clock=clock or FakeClock(), enabled=enabled)


def test_ibkr_tws_read_maps_key_env_type_and_route():
    adapter = ibkr()
    adapter.feed(TWS_TEXT)
    snapshot = adapter.accounts()
    records = {record.key: record for record in snapshot.records}
    assert set(records) == {
        "ibkr:ACCT-TEST-194",
        "ibkr:DUACCT-TEST-195",
        "ibkr:ACCT-TEST-196",
    }
    live = records["ibkr:ACCT-TEST-194"]
    paper = records["ibkr:DUACCT-TEST-195"]
    assert live.env == "live" and paper.env == "paper"
    assert live.account_type == "margin" and paper.account_type == "cash"
    assert records["ibkr:ACCT-TEST-196"].account_type is None
    assert live.source == "ibkr:tws"
    assert live.routes[0].platform == "ibkr"
    assert live.routes[0].local_name == "ACCT-TEST-194"
    assert live.display == "*194" and paper.display == "*195"
    assert snapshot.confirmed_at == NOW


def test_ibkr_portal_read_maps_key_env_type_and_route():
    adapter = ibkr()
    adapter.feed(PORTAL_TEXT)
    snapshot = adapter.accounts()
    records = {record.key: record for record in snapshot.records}
    assert records["ibkr:ACCT-TEST-194"].source == "ibkr:client_portal"
    assert records["ibkr:ACCT-TEST-194"].account_type == "margin"
    assert records["ibkr:DUACCT-TEST-195"].env == "paper"
    assert records["ibkr:DUACCT-TEST-195"].account_type == "ira"
    assert records["ibkr:ACCT-TEST-196"].account_type is None


@pytest.mark.parametrize("text", ["", "garbage", '{"accounts":[]}', "FlexQuery:123"])
def test_ibkr_unknown_shape_is_refused_with_a_kind(text):
    adapter = ibkr()
    adapter.feed(text)
    with pytest.raises(AccountRuleError) as caught:
        adapter.accounts()
    assert caught.value.kind == "unknown_ibkr_shape"
    assert adapter.stats.refused_snapshots == 1


def test_ibkr_is_off_by_default_and_the_switch_turns_it_on():
    adapter = IbkrAccountsAdapter(clock=FakeClock())  # no `enabled` argument: the default
    adapter.feed(TWS_TEXT)
    assert adapter.enabled is False
    with pytest.raises(AccountRuleError) as caught:
        adapter.accounts()
    assert caught.value.kind == "ibkr_disabled"
    assert adapter.stats.disabled_reads == 1
    adapter.set_enabled(True)
    assert adapter.enabled is True
    assert len(adapter.accounts().records) == 3


def test_ibkr_a_refused_read_keeps_the_last_good_list():
    adapter = ibkr()
    adapter.feed(TWS_TEXT)
    good = adapter.accounts()
    adapter.feed("garbage")
    assert adapter.accounts() is good
    assert adapter.stats.refused_snapshots == 1
    assert [record.display for record in adapter.accounts().records] == ["*194", "*195", "*196"]


def test_ibkr_goes_stale_at_exactly_120_seconds():
    clock = FakeClock()
    adapter = ibkr(clock)
    adapter.feed(TWS_TEXT)
    adapter.accounts()
    assert not adapter.is_stale(NOW + timedelta(seconds=119, microseconds=999999))
    assert adapter.is_stale(NOW + timedelta(seconds=120))


def test_ibkr_poller_reports_added_accounts():
    adapter = ibkr()
    adapter.feed(TWS_TEXT)
    events = adapter.account_events(NOW)
    assert [event.kind for event in events] == ["added", "added", "added"]
    assert {event.key for event in events} == {
        "ibkr:ACCT-TEST-194",
        "ibkr:DUACCT-TEST-195",
        "ibkr:ACCT-TEST-196",
    }


def test_ibkr_a_read_over_the_cap_is_refused_and_counted():
    ids = ",".join(f"ACCT-TEST-{i:04}" for i in range(2001))
    adapter = ibkr()
    adapter.feed(f"managedAccounts\t{ids}\n")
    with pytest.raises(AccountRuleError) as caught:
        adapter.accounts()
    assert caught.value.kind == "cap_exceeded"
    assert adapter.stats.over_cap_refused == 1


def test_ibkr_a_fed_read_over_the_byte_cap_is_refused():
    adapter = ibkr()
    with pytest.raises(AccountRuleError) as caught:
        adapter.feed("x" * (MAX_TEXT_BYTES + 1))
    assert caught.value.kind == "cap_exceeded"
    assert adapter.stats.over_cap_refused == 1


def test_ibkr_order_methods_refuse_and_send_nothing():
    adapter = ibkr()
    adapter.feed(TWS_TEXT)
    adapter.accounts()
    for call in (
        lambda: adapter.submit(object()),
        lambda: adapter.cancel("X"),
        lambda: adapter.replace("X", {}),
    ):
        with pytest.raises(UnsupportedCapability):
            call()
    assert adapter.orders(NOW) == [] and adapter.cash_events(NOW) == []
    assert adapter.positions() == [] and adapter.fills(NOW) == []
    assert not adapter.capabilities.supported_order_types
    with pytest.raises(UnsupportedCapability):
        adapter.connect()


VERB = re.compile(r"(place|cancel|modify|liquidate|flatten)", re.IGNORECASE)
VERB_WORD = re.compile(r"\b(place|cancel|modify|liquidate|flatten)", re.IGNORECASE)
GATED = sorted((Path(__file__).parent.parent / "src" / "trade_engine" / "ibkr").glob("*.py"))


def verb_findings(source: str) -> list[str]:
    """Order-verb calls, imports and strings (docstrings and def names are prose)."""
    tree = ast.parse(source)
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant):
                docstrings.add(id(body[0].value))
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if VERB.match(name):
                found.append(f"call {name}")
        elif isinstance(node, ast.Attribute) and VERB.match(node.attr):
            found.append(f"attribute {node.attr}")
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                if VERB.match(alias.name.split(".")[-1]):
                    found.append(f"import {alias.name}")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
            if VERB_WORD.search(node.value) or re.search(r"/(place|cancel|modify|liquidate|flatten)", node.value, re.I):
                found.append(f"string {node.value[:40]!r}")
    return found


def test_ibkr_module_carries_no_order_verb_or_network_client():
    assert [path.name for path in GATED] == ["__init__.py", "adapter.py"]
    for path in GATED:
        source = path.read_text(encoding="utf-8")
        assert verb_findings(source) == [], path.name
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name.split(".")[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [(node.module or "").split(".")[0]]
            else:
                continue
            assert not set(names) & {"socket", "http", "urllib", "requests", "ibapi", "asyncio"}, path.name


def test_ibkr_rust_rule_has_no_socket_or_http_client():
    source = (
        Path(__file__).parent.parent / "crates" / "te_core" / "src" / "ibkr.rs"
    ).read_text(encoding="utf-8")
    for forbidden in ("std::net", "TcpStream", "reqwest", "hyper", "ureq", "std::process"):
        assert forbidden not in source, forbidden

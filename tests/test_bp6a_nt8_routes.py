"""BP-6a: NT8 Desktop as a route (BROKER_PLATFORM phase 6a).

Fixtures only; no live bridge. Every account id here is fake (``ACCT-TEST-*``).
"""

from __future__ import annotations

import pytest

from trade_engine.accounts import _rs
from trade_engine.accounts.errors import AccountRuleError
from trade_engine.accounts.nt8 import map_account, map_accounts


@pytest.mark.parametrize(
    ("name", "key", "local"),
    [
        ("APEX-ACCT-TEST-194", "tradovate:APEX-ACCT-TEST-194", False),
        ("PAAPEX-ACCT-TEST-195", "tradovate:PAAPEX-ACCT-TEST-195", False),
        ("TAKEPROFITPRO-ACCT-TEST-196", "tradovate:TAKEPROFITPRO-ACCT-TEST-196", False),
        ("TAKEPROFIT-ACCT-TEST-197", "tradovate:TAKEPROFIT-ACCT-TEST-197", False),
        ("TDYG-ACCT-TEST-198", "tradovate:TDYG-ACCT-TEST-198", False),
        ("TDFYG-ACCT-TEST-199", "tradovate:TDFYG-ACCT-TEST-199", False),
        ("FTDFYG-ACCT-TEST-200", "tradovate:FTDFYG-ACCT-TEST-200", False),
        ("LFE-ACCT-TEST-201", "tradovate:LFE-ACCT-TEST-201", False),
        ("Sim101", "local:nt8:Sim101", True),
        ("Playback101", "local:nt8:Playback101", True),
        ("SimFan01", "local:nt8:SimFan01", True),
        ("Sim_All_Day_ORB", "local:nt8:Sim_All_Day_ORB", True),
    ],
)
def test_nt8_name_table_maps_each_firm_prefix_and_local_family(name, key, local):
    mapped = map_account(name)
    assert mapped.key == key
    assert mapped.local is local
    assert (mapped.route.platform, mapped.route.local_name) == ("nt8", name)


@pytest.mark.parametrize("name", ["MYSTERY-194", "", "ACME-TRADING"])
def test_nt8_unknown_name_is_refused_never_guessed(name):
    with pytest.raises(AccountRuleError) as caught:
        map_account(name)
    assert caught.value.kind == "unknown_nt8_account"


def test_nt8_local_key_never_passes_the_engine_key_rule():
    local = map_account("Sim101")
    assert local.key.startswith("local:nt8:")
    with pytest.raises(AccountRuleError) as caught:
        _rs.key("local", "nt8:Sim101")
    assert caught.value.kind == "bad_key"


def test_nt8_bridge_list_text_maps_every_account_and_refuses_one_unknown():
    text = (
        '{"success":true,"count":3,"accounts":['
        '{"account":"APEX-ACCT-TEST-194","provider":"Provider31"},'
        '{"account":"Sim101","provider":"Simulator","connection":"TPT"},'
        '{"account":"Playback101","provider":"Playback"}]}'
    )
    mapped = map_accounts(text)
    assert [m.key for m in mapped] == [
        "tradovate:APEX-ACCT-TEST-194",
        "local:nt8:Sim101",
        "local:nt8:Playback101",
    ]
    with pytest.raises(AccountRuleError) as caught:
        map_accounts('{"accounts":[{"account":"MYSTERY-194"}]}')
    assert caught.value.kind == "unknown_nt8_account"


def test_nt8_name_case_is_kept():
    assert map_account("APEX-AbC-194").key == "tradovate:APEX-AbC-194"

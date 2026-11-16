"""PS-4a: the registry sink sends each account's ntx broker realized P&L (default off).

Fixtures only: a riskguard double, fake `ACCT-TEST-nnn` ids, a loopback fake ntx daemon. The
rules live in `te_core::registry_outbox`; these tests drive them through the sink.

Enforcing tests, by rule:
- OFF is byte-identical to before: `test_off_*`, and the Rust golden
  `pnl_off_envelopes_are_the_pre_ps4a_bytes`.
- ON: sorted list, decimal strings, None absent, a change goes out at once without a seq:
  `test_on_*`.
- B7 cap (a new key past the account cap is refused and counted): `test_cap_*`, and the Rust
  `the_pnl_map_holds_at_the_account_cap_and_counts_refusals`.
- an account gone from the snapshot drops its entry: `test_snapshot_drop_*`.
- no full account id in any log line: `test_masking_*`.
- config: `test_config_*`.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timezone

from trade_engine.sinks.riskguard_registry import ENV_PNL, RegistrySinkConfig
from trade_engine.tradovate.adapter import TradovateAdapter

from tests.test_bp3_tradovate_ntx import (  # noqa: F401 - fixtures are collected from here
    FakeClock,
    IDENT_A,
    IDENT_B,
    acct,
    clock,
    daemon,
    make_client,
    snap,
)
from tests.test_bp4a_registry_sink import FakeRg, T0, fleet, make_sink, seed

AS_OF = "2026-11-11T17:59:30Z"
K1, K2, K3 = ("tradovate:ACCT-TEST-00%d" % i for i in (1, 2, 3))


def recording(rg: FakeRg):
    """`rg.post` that also keeps the raw request bytes."""
    raw: list[bytes] = []

    def post(url, body, headers, timeout):
        raw.append(bytes(body))
        return rg.post(url, body, headers, timeout)

    return post, raw


def run_polls(tmp_path, name, rows, **kw):
    """Two polls and a heartbeat into a double; returns (raw request bodies, envelopes)."""
    (tmp_path / name).mkdir()
    clk, rg = FakeClock(T0), FakeRg()
    post, raw = recording(rg)
    sink = make_sink(tmp_path / name, rg, clk, **kw)
    sink._post = post
    sink.observe(fleet(3), [], 0, pnl=rows)
    sink.pump()
    clk.advance(40)
    sink.pump()
    return raw, rg.envelopes


# --- OFF ----------------------------------------------------------------------------------


def test_off_envelopes_are_byte_identical_whatever_the_poller_passes(tmp_path):
    rows = [(K1, ("-62.50", AS_OF)), (K2, None)]
    never, _ = run_polls(tmp_path, "never", None)
    passed, envelopes = run_polls(tmp_path, "passed", rows)  # switch off: the rows are ignored
    assert len(never) == 2 and never == passed
    assert all("pnl" not in e for e in envelopes)


def test_off_a_sink_that_is_off_ignores_the_pnl_switch(tmp_path):
    clk = FakeClock(T0)
    sink = make_sink(tmp_path, None, clk, enabled=False, pnl_enabled=True)
    assert not sink.enabled
    sink.observe(fleet(3), [], 0, pnl=[(K1, ("1", AS_OF))])
    assert sink.counters["observed"] == 0 and not (tmp_path / "outbox.json").exists()


# --- ON -----------------------------------------------------------------------------------


def test_on_two_accounts_sorted_decimal_strings_none_absent_and_a_change_goes_out_at_once(tmp_path):
    clk, rg = FakeClock(T0), FakeRg()
    sink = make_sink(tmp_path, rg, clk, pnl_enabled=True)
    seed(sink, 3)
    # K3 first on purpose, K2 has no figure: the list is sorted and holds only K1 and K3.
    sink.observe(fleet(3), [], 3, pnl=[(K3, ("0", AS_OF)), (K2, None), (K1, ("-62.50", AS_OF))])
    sink.pump()
    first = rg.envelopes[-1]
    assert first["pnl"] == [
        {"key": K1, "realized_usd": "-62.50", "as_of": AS_OF},
        {"key": K3, "realized_usd": "0", "as_of": AS_OF},
    ]
    assert all(isinstance(r["realized_usd"], str) for r in first["pnl"])
    n = len(rg.envelopes)

    # An unchanged value waits for the heartbeat cadence (stale_after / 3 = 40 s) ...
    sink.observe(fleet(3), [], 3, pnl=[(K3, ("0", AS_OF)), (K2, None), (K1, ("-62.50", AS_OF))])
    sink.pump()
    assert len(rg.envelopes) == n

    # ... a changed one is sent on the next pump, without a seq, as a heartbeat-shaped envelope.
    sink.observe(fleet(3), [], 3, pnl=[(K3, ("0", AS_OF)), (K2, None), (K1, ("-80.25", AS_OF))])
    sink.pump()
    beat = rg.envelopes[-1]
    assert len(rg.envelopes) == n + 1
    assert beat["events"] == [] and "snapshot" not in beat
    assert [r["realized_usd"] for r in beat["pnl"]] == ["-80.25", "0"]
    assert rg.applied == 3  # no seq taken

    # And it keeps riding on the later heartbeats.
    clk.advance(40)
    sink.pump()
    assert rg.envelopes[-1]["pnl"] == beat["pnl"]
    assert sink.counters["pnl_refused"] == 0


def test_on_a_figure_that_becomes_none_leaves_the_list(tmp_path):
    clk, rg = FakeClock(T0), FakeRg()
    sink = make_sink(tmp_path, rg, clk, pnl_enabled=True)
    seed(sink, 3)
    sink.observe(fleet(3), [], 3, pnl=[(K1, ("1.5", AS_OF)), (K2, ("2", AS_OF))])
    sink.pump()
    sink.observe(fleet(3), [], 3, pnl=[(K1, ("1.5", AS_OF)), (K2, None)])
    sink.pump()
    assert [r["key"] for r in rg.envelopes[-1]["pnl"]] == [K1]


# --- cap (B7) -----------------------------------------------------------------------------


def test_cap_the_pnl_map_holds_at_the_account_cap_and_counts_refusals(tmp_path):
    clk, rg = FakeClock(T0), FakeRg()
    sink = make_sink(tmp_path, rg, clk, pnl_enabled=True)
    seed(sink, 3)
    cap = 2000  # te_core accounts::MAX_RECORDS
    rows = [("tradovate:ACCT-FILL-%04d" % i, ("1", AS_OF)) for i in range(cap + 5)]
    sink.observe(fleet(3), [], 3, pnl=rows)
    assert sink._outbox.pnl_len() == cap
    assert sink.counters["pnl_refused"] == 5
    assert json.loads(sink._outbox.stats())["pnl_refused"] == 5
    assert sink.last_error_kind == "cap_exceeded"
    # An existing key still overwrites at the cap.
    sink._outbox.set_pnl("tradovate:ACCT-FILL-0000", "2", AS_OF)
    assert sink._outbox.pnl_len() == cap
    # The next snapshot drops every figure whose account it does not list (the fill keys).
    sink.observe(fleet(3), [], 3)
    assert sink._outbox.pnl_len() == 0
    assert json.loads(sink._outbox.stats())["pnl_dropped"] == cap


# --- snapshot drop ------------------------------------------------------------------------


def test_snapshot_drop_an_account_gone_from_the_snapshot_drops_its_pnl_entry(tmp_path):
    clk, rg = FakeClock(T0), FakeRg()
    sink = make_sink(tmp_path, rg, clk, pnl_enabled=True)
    seed(sink, 3)
    sink.observe(fleet(3), [], 3, pnl=[(K1, ("1", AS_OF)), (K3, ("3", AS_OF))])
    assert sink._outbox.pnl_len() == 2
    removed = {
        "kind": "removed",
        "key": K3,
        "display": "*003",
        "at": "2026-11-11T18:00:00Z",
        "seq": 4,
        "record": None,
        "changes": [],
    }
    sink.observe(fleet(2), [removed], 4)  # no pnl argument: the drop is Rust's, on the snapshot
    assert sink._outbox.pnl_len() == 1
    assert json.loads(sink._outbox.stats())["pnl_dropped"] == 1
    sink.pump()
    assert [r["key"] for r in rg.envelopes[-1]["pnl"]] == [K1]


# --- end to end: poller, adapter trading-day mapping, sink ---------------------------------


def trading_day(at: datetime) -> date:
    return at.astimezone(timezone.utc).date()


def test_the_poller_passes_the_ntx_figures_for_the_trading_day_only(tmp_path, daemon, clock):
    rg = FakeRg()
    sink = make_sink(tmp_path, rg, clock, pnl_enabled=True)
    daemon.snapshot_body = snap(
        [
            acct("ACCT-TEST-194", "900194", identity=IDENT_A, realized=-62.5),
            acct("ACCT-TEST-277", "900277", identity=IDENT_B, realized=10, as_of="2026-11-10T17:00:00Z"),
        ]
    )
    adapter = TradovateAdapter(make_client(daemon), clock=clock, trading_day=trading_day, on_poll=sink.observe)
    adapter.poller.poll()
    sink.pump()
    sent = rg.envelopes[-1]["pnl"]
    # 277's figure is from another day: absent. Python decided nothing but the day mapping.
    # 194's stream is connected, so its figure is stamped at the read (BP-4e).
    assert [(r["key"], r["realized_usd"], r["as_of"]) for r in sent] == [
        ("tradovate:ACCT-TEST-194", "-62.5", "2026-11-11T18:00:00Z")
    ]


def test_without_a_trading_day_mapping_the_poller_passes_no_pnl(tmp_path, daemon, clock):
    rg = FakeRg()
    sink = make_sink(tmp_path, rg, clock, pnl_enabled=True)
    daemon.snapshot_body = snap([acct("ACCT-TEST-194", "900194", identity=IDENT_A)])
    adapter = TradovateAdapter(make_client(daemon), clock=clock, on_poll=sink.observe)
    adapter.poller.poll()
    sink.pump()
    assert "pnl" not in rg.envelopes[-1]


# --- masking ------------------------------------------------------------------------------


def test_masking_no_full_account_id_in_any_log_line(tmp_path, daemon, clock, caplog):
    caplog.set_level(logging.DEBUG)
    rg = FakeRg()
    sink = make_sink(tmp_path, rg, clock, pnl_enabled=True)
    daemon.snapshot_body = snap(
        [
            acct("ACCT-TEST-194", "900194", identity=IDENT_A, realized=float("nan")),  # unreadable
            acct("ACCT-TEST-277", "900277", identity=IDENT_B, realized=5),
        ]
    )
    adapter = TradovateAdapter(make_client(daemon), clock=clock, trading_day=trading_day, on_poll=sink.observe)
    adapter.poller.poll()
    # A refusal inside the sink: bad value for a key.
    sink.observe(fleet(3), [], 3, pnl=[("tradovate:ACCT-TEST-194", ("1e3", AS_OF))])
    sink.observe(fleet(3), [], 3, pnl=[("other:ACCT-TEST-194", ("1", AS_OF))])
    assert sink.counters["pnl_refused"] == 2
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "ntx realized pnl unreadable" in text and "*194" in text
    assert "ACCT-TEST-194" not in text and "ACCT-TEST-277" not in text
    assert "900194" not in text and "900277" not in text


# --- config -------------------------------------------------------------------------------


def test_config_pnl_switch_is_off_by_default_only_on_enables_it_and_shows_in_repr():
    assert RegistrySinkConfig().pnl_enabled is False
    assert RegistrySinkConfig.from_env({}).pnl_enabled is False
    for value in ("", "off", "0", "true", "1", "yes"):
        assert RegistrySinkConfig.from_env({ENV_PNL: value}).pnl_enabled is False, value
    cfg = RegistrySinkConfig.from_env({ENV_PNL: " ON "})
    assert cfg.pnl_enabled is True
    assert "pnl_enabled=True" in repr(cfg) and "pnl_enabled=False" in repr(RegistrySinkConfig())
    assert ENV_PNL == "TE_RISKGUARD_REGISTRY_PNL"

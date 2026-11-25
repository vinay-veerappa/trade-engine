"""OA-5 end to end: reconcile on start rebuilds the bracket map; PENDING entries resolve once.

A stateful, idempotent in-process fake of the ntx order endpoints and `/snapshot` drives a real
`OrderManager` and ledger. A restart is a new adapter and manager over the same ledger. Accounts
are fakes; nothing reaches a venue.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from decimal import Decimal

import pytest

from trade_engine.domain.instruments import Equity, Side
from trade_engine.domain.orders import OrderState, TimeInForce
from trade_engine.oms import OrderManager
from trade_engine.oms.manager import OrderManagementError
from trade_engine.oms.precheck import AccountBinding, PrecheckConfig
from trade_engine.tradovate.adapter import ORDER_ID_CAP, TradovateAdapter
from trade_engine.tradovate.ntx_client import NtxError

from test_bp3_tradovate_ntx import acct, snap
from test_oa3_ntx_orders import (  # noqa: F401  (fixtures)
    NTX_ACCOUNT, Binding, Clock, OmsClock, guard, ledger,
)
from test_oms import make_fill, make_intent
from test_oms_precheck import ALLOWED

ENGINE = "account-1"


class FakeNtx:
    """ntx as the adapter sees it: idempotent by clOrdId, brackets listed on `/snapshot`."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.answers: dict[str, dict] = {}  # clOrdId -> first answer, replayed on a duplicate
        self.brackets: dict[str, dict] = {}  # entry clOrdId -> {clOrdId, entryId, stopId, targetId}
        self.working: dict[int, dict] = {}  # ntx order id -> {type, price, qty}
        self.next_id = 100
        self.timeout: str | None = None  # "present" / "absent": place answers nothing
        self.list_brackets = True  # False: the row carries no bracket truth at all
        self.row_brackets = True  # OA-5b ntx: order rows carry clOrdId + brackets {parentId, stopId, targetId}
        self.authoritative: bool | None = None  # bracketsAuthoritative, omitted when None
        self.snapshots = 0

    def _id(self) -> int:
        self.next_id += 1
        return self.next_id

    def place(self, body: dict) -> dict:
        self.calls.append(("place", body))
        cl = body["clOrdId"]
        if cl in self.answers:
            return self.answers[cl]
        ids = {"entryId": self._id(), "stopId": self._id()}
        if "target" in body:
            ids["targetId"] = self._id()
        self.working[ids["entryId"]] = {"type": body["entry"]["type"], "qty": body["qty"]}
        self.working[ids["stopId"]] = {"type": "stop", "price": body["stop"], "qty": body["qty"]}
        if "targetId" in ids:
            self.working[ids["targetId"]] = {"type": "limit", "price": body["target"], "qty": body["qty"]}
        if self.timeout is not None:
            if self.timeout == "present":  # written at the venue, the answer lost
                self.brackets[cl] = {"clOrdId": cl, **ids}
            else:
                for key in ids.values():
                    self.working.pop(key, None)
            raise NtxError("pending", "no answer before the deadline")
        self.brackets[cl] = {"clOrdId": cl, **ids}
        return self._answer("place", cl, ids)

    def modify(self, body: dict) -> dict:
        self.calls.append(("modify", body))
        cl = body["clOrdId"]
        if cl in self.answers:
            return self.answers[cl]
        held = self.working[body["orderId"]]
        held.update(qty=body["qty"], **body["to"])
        return self._answer("modify", cl, {"entryId": body["orderId"]})

    def cancel(self, body: dict) -> dict:
        self.calls.append(("cancel", body))
        cl = body["clOrdId"]
        if cl in self.answers:
            return self.answers[cl]
        self.working.pop(body["orderId"])
        return self._answer("cancel", cl, {"entryId": body["orderId"]})

    def snapshot(self) -> dict:
        self.snapshots += 1
        row = acct(NTX_ACCOUNT, "900194")
        if self.list_brackets:
            row["brackets"] = list(self.brackets.values())
            if self.authoritative is not None:
                row["bracketsAuthoritative"] = self.authoritative
            if self.row_brackets:
                row["orders"] = self._order_rows()
        return snap([row])

    def _order_rows(self) -> list[dict]:
        """Working order rows as the OA-5b ntx emits them: only ntx-placed brackets carry links."""
        rows = []
        for cl, ids in self.brackets.items():
            links = {"parentId": ids["entryId"], "stopId": ids["stopId"]}
            if "targetId" in ids:
                links["targetId"] = ids["targetId"]
            for key in ("entryId", "stopId", "targetId"):
                if key in ids and ids[key] in self.working:
                    rows.append({"id": ids[key], "clOrdId": cl, "brackets": links, **self.working[ids[key]]})
        rows.append({"id": 9999, "type": "limit", "qty": 1})  # a venue order ntx did not place: no links
        return rows

    def _answer(self, op: str, cl: str, ids: dict) -> dict:
        answer = {"account": "***194", "clOrdId": cl, "op": op, "outcome": "SENT", "orderIds": ids}
        self.answers[cl] = answer
        return answer

    def ops(self, op: str) -> list[dict]:
        return [body for name, body in self.calls if name == op]


@pytest.fixture
def fake():
    return FakeNtx()


def start(fake, ledger, guard, monkeypatch):
    """One engine process: an adapter over the fake ntx and an OMS over the shared ledger."""
    monkeypatch.setattr(Equity, "to_platform", lambda self, platform: "MNQZ6", raising=False)
    guard.body = {**ALLOWED, "verdictRevision": 9, "checkedAt": "2026-11-17T15:00:01.000Z"}
    adapter = TradovateAdapter(fake, clock=Clock(), ledger=ledger, account_bindings={ENGINE: Binding(NTX_ACCOUNT)})
    config = PrecheckConfig(
        mode="enforce", base_url=guard.base_url, token="check-token",
        accounts={ENGINE: AccountBinding("core-1", {"venue": "tradovate", "env": "demo", "brokerAccountId": "123"})},
    )
    return adapter, OrderManager(adapter, OmsClock(), ledger, precheck=config)


def new_bracket(manager):
    targets = (Decimal("22010"),)
    intent = replace(
        make_intent(side=Side.BUY, targets=targets),
        entry_price=Decimal("22000"), exit_tif=TimeInForce.DAY,
        stop_loss=Decimal("21990"), profit_targets=targets,
    )
    return manager.create_bracket(intent, Decimal("2"))


def fill(adapter, entry, qty="2"):
    return replace(make_fill(entry, "f1", qty, "22000"), venue_env=adapter.env)


def test_restart_reconciles_then_tightens_on_the_mapped_stop_and_cancels(fake, ledger, guard, monkeypatch):
    adapter, manager = start(fake, ledger, guard, monkeypatch)
    adapter.connect()  # empty ledger: nothing to map
    bracket = new_bracket(manager)
    entry = manager.submit(bracket.entry)
    assert entry.state is OrderState.ACCEPTED
    manager.record_fill(fill(adapter, entry))
    stop = manager.get_order(bracket.stop.order_id)
    target = manager.get_order(bracket.targets[0].order_id)
    assert stop.state is OrderState.ACCEPTED and target.state is OrderState.ACCEPTED
    (ids,) = fake.brackets.values()  # keyed by the wire clOrdId ntx stored
    assert fake.ops("modify") == []  # the protective sync adopted the ntx children as they were

    # Restart: a new adapter knows nothing until it reconciles from one snapshot read.
    adapter, manager = start(fake, ledger, guard, monkeypatch)
    assert adapter._orders == {}
    reads = fake.snapshots
    adapter.connect()
    assert fake.snapshots == reads + 1  # connect's own read; reconcile reuses it
    entry_record = adapter._orders[entry.order_id]
    assert (entry_record["entryId"], entry_record["stopId"], entry_record["targetId"]) == (
        ids["entryId"], ids["stopId"], ids["targetId"])
    assert adapter._orders[stop.order_id]["stopId"] == ids["stopId"]
    assert adapter._orders[target.order_id]["entryId"] == ids["targetId"]

    # A stop tighten goes out on the mapped ntx stop id with a fresh clOrdId (never a replay).
    before = set(fake.answers)
    moved = manager.move_stop(entry.order_id, Decimal("21995"), command_id="cmd-move-1")
    assert moved.state is not OrderState.REJECTED
    mods = fake.ops("modify")
    assert len(mods) == 1
    assert mods[0]["orderId"] == ids["stopId"] and mods[0]["to"] == {"type": "stop", "price": "21995"}
    assert mods[0]["clOrdId"] not in before
    assert fake.working[ids["stopId"]]["price"] == "21995"
    assert manager.get_order(bracket.stop.order_id).stop_price == Decimal("21995")

    # A looser stop is still refused locally after the restart.
    with pytest.raises(OrderManagementError, match="stops only tighten"):
        manager.move_stop(entry.order_id, Decimal("21980"), command_id="cmd-move-2")
    assert len(fake.ops("modify")) == 1

    # Cancel the profit target by its mapped id, again with a fresh clOrdId.
    cancelled = manager.cancel(target.order_id, command_id="cmd-cancel-1")
    assert cancelled.state is OrderState.CANCELLED
    cancels = fake.ops("cancel")
    assert len(cancels) == 1 and cancels[0]["orderId"] == ids["targetId"]
    assert cancels[0]["clOrdId"] not in before
    assert ids["targetId"] not in fake.working
    assert len(fake.ops("place")) == 1  # nothing was ever resent


def test_reconnect_reconcile_keeps_records_mapped_this_session(fake, ledger, guard, monkeypatch):
    adapter, manager = start(fake, ledger, guard, monkeypatch)
    bracket = new_bracket(manager)
    entry = manager.submit(bracket.entry)
    manager.record_fill(fill(adapter, entry))
    adapter, manager = start(fake, ledger, guard, monkeypatch)
    first = adapter.reconcile()
    assert first.mapped == [entry.order_id]
    record = adapter._orders[entry.order_id]
    second = adapter.reconcile()  # a stream reconnect: one more read, the live record is kept
    assert second.mapped == [] and second.unmatched == []
    assert adapter._orders[entry.order_id] is record


def test_unmatched_entry_fails_closed_after_restart(fake, ledger, guard, monkeypatch, caplog):
    adapter, manager = start(fake, ledger, guard, monkeypatch)
    bracket = new_bracket(manager)
    entry = manager.submit(bracket.entry)
    assert entry.state is OrderState.ACCEPTED
    fake.brackets.clear()  # ntx no longer lists the clOrdId: no venue truth for this entry

    adapter, manager = start(fake, ledger, guard, monkeypatch)
    with caplog.at_level(logging.INFO, logger="trade_engine.tradovate.adapter"):
        report = adapter.reconcile()
        again = adapter.reconcile()
    assert report.unmatched == [entry.order_id] and again.unmatched == [entry.order_id]
    assert report.mapped == [] and report.rejected == []  # an ACCEPTED entry is never rejected
    assert sum("unmatched" in r.getMessage() and "left unmapped" in r.getMessage()
               for r in caplog.records) == 1  # logged once
    assert entry.order_id not in adapter._orders

    calls = len(fake.calls)
    with caplog.at_level(logging.INFO, logger="trade_engine.tradovate.adapter"):
        with pytest.raises(OrderManagementError, match="requires venue reconciliation"):
            manager.record_fill(fill(adapter, entry))  # protection the adapter cannot map
    assert manager.get_order(bracket.stop.order_id).state is OrderState.REJECTED
    assert len(fake.calls) == calls  # refused locally: no ntx call
    assert "code=unknown_parent" in caplog.text


def test_restart_maps_children_from_the_order_rows_alone(fake, ledger, guard, monkeypatch):
    adapter, manager = start(fake, ledger, guard, monkeypatch)
    bracket = new_bracket(manager)
    entry = manager.submit(bracket.entry)
    manager.record_fill(fill(adapter, entry))
    (ids,) = fake.brackets.values()
    snapshot = fake.snapshot()
    (row,) = snapshot["accounts"]
    assert {o["clOrdId"] for o in row["orders"] if "clOrdId" in o} == set(fake.brackets)
    assert {o["brackets"]["stopId"] for o in row["orders"] if "brackets" in o} == {ids["stopId"]}
    row.pop("brackets")  # no account-level list: the order rows are the only source
    adapter, manager = start(fake, ledger, guard, monkeypatch)
    report = adapter.reconcile(payload=snapshot)
    assert report.mapped == [entry.order_id] and report.unmatched == []
    record = adapter._orders[entry.order_id]
    assert (record["entryId"], record["stopId"], record["targetId"]) == (
        ids["entryId"], ids["stopId"], ids["targetId"])


def test_a_row_without_order_links_still_maps_by_cl_ord_id(fake, ledger, guard, monkeypatch):
    adapter, manager = start(fake, ledger, guard, monkeypatch)
    bracket = new_bracket(manager)
    entry = manager.submit(bracket.entry)
    manager.record_fill(fill(adapter, entry))
    (ids,) = fake.brackets.values()
    fake.row_brackets = False  # an ntx before OA-5b: only the account-level clOrdId list
    adapter, manager = start(fake, ledger, guard, monkeypatch)
    report = adapter.reconcile()
    assert report.mapped == [entry.order_id]
    assert adapter._orders[entry.order_id]["stopId"] == ids["stopId"]
    moved = manager.move_stop(entry.order_id, Decimal("21995"), command_id="cmd-move-1")
    assert moved.state is not OrderState.REJECTED
    assert fake.ops("modify")[0]["orderId"] == ids["stopId"]


def test_unmatched_entry_is_still_rejected_unknown_parent_with_order_rows(fake, ledger, guard, monkeypatch, caplog):
    adapter, manager = start(fake, ledger, guard, monkeypatch)
    bracket = new_bracket(manager)
    entry = manager.submit(bracket.entry)
    fake.brackets.clear()  # order rows carry links for other brackets only; this clOrdId is not listed
    fake.brackets["someone-else"] = {"clOrdId": "someone-else", "entryId": 7, "stopId": 8}
    adapter, manager = start(fake, ledger, guard, monkeypatch)
    report = adapter.reconcile()
    assert report.unmatched == [entry.order_id] and report.mapped == []
    with caplog.at_level(logging.INFO, logger="trade_engine.tradovate.adapter"):
        with pytest.raises(OrderManagementError, match="requires venue reconciliation"):
            manager.record_fill(fill(adapter, entry))
    assert manager.get_order(bracket.stop.order_id).state is OrderState.REJECTED
    assert "code=unknown_parent" in caplog.text


def test_absent_pending_entry_is_not_rejected_when_ntx_is_not_authoritative(fake, ledger, guard, monkeypatch):
    adapter, manager = start(fake, ledger, guard, monkeypatch)
    fake.timeout = "absent"
    entry = manager.submit(new_bracket(manager).entry)
    assert entry.state is OrderState.PENDING_UNKNOWN
    fake.timeout = None
    fake.authoritative = False  # ntx has another unresolved placement: absence proves nothing
    adapter, manager = start(fake, ledger, guard, monkeypatch)
    report = adapter.reconcile()
    assert report.rejected == [] and report.accepted == [] and report.unmatched == [entry.order_id]
    fake.authoritative = True
    assert adapter.reconcile().rejected == [entry.order_id]


def test_adapter_without_a_ledger_skips_reconcile_with_one_log_line(fake, monkeypatch, caplog):
    adapter = TradovateAdapter(fake, clock=Clock(), account_bindings={ENGINE: Binding(NTX_ACCOUNT)})
    reads = fake.snapshots
    with caplog.at_level(logging.INFO, logger="trade_engine.tradovate.adapter"):
        for _ in range(3):
            report = adapter.reconcile()
    assert report.mapped == [] and report.unmatched == [] and report.rejected == []
    assert fake.snapshots == reads  # no read either
    assert sum("reconcile skipped (no ledger)" in r.getMessage() for r in caplog.records) == 1


@pytest.mark.parametrize("row, expected", [
    (None, (None, False)),
    ({"orders": []}, (None, False)),
    ({"brackets": []}, ({}, True)),
    ({"brackets": [], "bracketsAuthoritative": False}, ({}, False)),
    ({"orders": [{"clOrdId": "a", "brackets": {"parentId": 1, "stopId": 2}}]},
     ({"a": {"clOrdId": "a", "entryId": 1, "stopId": 2, "targetId": None}}, True)),
    ({"orders": [{"clOrdId": "a", "brackets": {"parentId": 1}}]}, (None, False)),  # no stop: not a bracket
    ({"orders": [{"brackets": {"parentId": 1, "stopId": 2}}]}, (None, False)),  # no clOrdId: never by price/qty
])
def test_bracket_index_reads_rows_and_the_account_list(row, expected):
    from trade_engine.tradovate.adapter import _bracket_index

    assert _bracket_index(row) == expected


def test_a_row_without_brackets_maps_and_rejects_nothing(fake, ledger, guard, monkeypatch):
    adapter, manager = start(fake, ledger, guard, monkeypatch)
    fake.timeout = "absent"
    bracket = new_bracket(manager)
    entry = manager.submit(bracket.entry)
    assert entry.state is OrderState.PENDING_UNKNOWN
    fake.list_brackets = False
    adapter, manager = start(fake, ledger, guard, monkeypatch)
    report = adapter.reconcile()
    assert report.rejected == [] and report.accepted == [] and report.unmatched == [entry.order_id]
    assert adapter.orders(entry.created_at) == []


@pytest.mark.parametrize("where, expected", [
    ("present", OrderState.ACCEPTED),
    ("absent", OrderState.REJECTED),
])
def test_pending_entry_resolves_from_one_read_and_is_never_resent(fake, ledger, guard, monkeypatch, where, expected,
                                                                  caplog):
    adapter, manager = start(fake, ledger, guard, monkeypatch)
    fake.timeout = where
    bracket = new_bracket(manager)
    entry = manager.submit(bracket.entry)
    assert entry.state is OrderState.PENDING_UNKNOWN
    fake.timeout = None

    adapter, manager = start(fake, ledger, guard, monkeypatch)
    reads = fake.snapshots
    with caplog.at_level(logging.INFO, logger="trade_engine.tradovate.adapter"):
        report = adapter.reconcile()
    assert fake.snapshots == reads + 1
    resolved = report.accepted if expected is OrderState.ACCEPTED else report.rejected
    assert resolved == [entry.order_id]
    # The OMS read the verdict back automatically (adapter._on_reconciled): no manual loop.
    assert manager.get_order(entry.order_id).state is expected
    assert len(fake.ops("place")) == 1  # never resent
    if expected is OrderState.ACCEPTED:
        (ids,) = fake.brackets.values()
        assert adapter._orders[entry.order_id]["stopId"] == ids["stopId"]
        manager.record_fill(fill(adapter, manager.get_order(entry.order_id)))
        assert manager.get_order(bracket.stop.order_id).state is OrderState.ACCEPTED
        assert len(fake.ops("place")) == 1
    else:
        assert "code=not_at_venue" in caplog.text
        assert entry.order_id not in adapter._orders


def test_read_back_continues_past_a_failing_id(fake, ledger, guard, monkeypatch, caplog):
    adapter, manager = start(fake, ledger, guard, monkeypatch)
    fake.timeout = "present"

    def bracket(command_id):
        targets = (Decimal("22010"),)
        intent = replace(
            make_intent(command_id=command_id, side=Side.BUY, targets=targets),
            entry_price=Decimal("22000"), exit_tif=TimeInForce.DAY,
            stop_loss=Decimal("21990"), profit_targets=targets,
        )
        return manager.create_bracket(intent, Decimal("2"))

    first = manager.submit(bracket("readback-a").entry)
    second = manager.submit(bracket("readback-b").entry)
    assert first.state is OrderState.PENDING_UNKNOWN
    assert second.state is OrderState.PENDING_UNKNOWN
    fake.timeout = None

    adapter, manager = start(fake, ledger, guard, monkeypatch)
    original = manager.reconcile_order
    seen: list[str] = []

    def flaky(order_id):
        seen.append(order_id)
        if len(seen) == 1:  # the first id fails; the rest must still be read back
            raise RuntimeError("read-back boom")
        return original(order_id)

    monkeypatch.setattr(manager, "reconcile_order", flaky)
    with caplog.at_level(logging.ERROR, logger="trade_engine.oms.manager"):
        report = adapter.reconcile()
    assert set(report.accepted) == {first.order_id, second.order_id}
    assert seen == list(report.accepted)
    failed, ok = seen
    assert manager.get_order(failed).state is OrderState.PENDING_UNKNOWN  # read-back failed
    assert manager.get_order(ok).state is OrderState.ACCEPTED  # the rest still resolved
    assert "reconcile read-back failed" in caplog.text
    assert failed in caplog.text and "RuntimeError" in caplog.text


def test_adapter_without_an_order_manager_reconciles_unchanged(fake, ledger, guard, monkeypatch):
    adapter, manager = start(fake, ledger, guard, monkeypatch)
    fake.timeout = "present"
    entry = manager.submit(new_bracket(manager).entry)
    assert entry.state is OrderState.PENDING_UNKNOWN
    fake.timeout = None

    # A fresh adapter with no OrderManager wired: `_on_reconciled` is None, so reconcile still
    # resolves the verdict in the adapter but never reads it back into the ledger.
    adapter = TradovateAdapter(fake, clock=Clock(), ledger=ledger,
                               account_bindings={ENGINE: Binding(NTX_ACCOUNT)})
    assert adapter._on_reconciled is None
    report = adapter.reconcile()
    assert report.accepted == [entry.order_id]
    # The verdict is stored for a future OMS read-back, but the ledger is untouched.
    assert manager.get_order(entry.order_id).state is OrderState.PENDING_UNKNOWN


def test_resolved_and_mapped_ids_stay_capped(fake, ledger, guard, monkeypatch):
    adapter, _ = start(fake, ledger, guard, monkeypatch)
    for n in range(ORDER_ID_CAP + 5):
        adapter._resolve(f"cmd-{n}", OrderState.REJECTED, Decimal("1"), Clock().now_utc())
        adapter._remember(f"cmd-{n}", {"account": NTX_ACCOUNT, "seq": 0})
    assert len(adapter._resolved) == ORDER_ID_CAP and "cmd-0" not in adapter._resolved
    assert len(adapter._orders) == ORDER_ID_CAP


def test_a_failed_read_changes_nothing(fake, ledger, guard, monkeypatch):
    adapter, manager = start(fake, ledger, guard, monkeypatch)
    bracket = new_bracket(manager)
    entry = manager.submit(bracket.entry)

    def down():
        raise NtxError("unavailable", "daemon down")

    adapter, manager = start(fake, ledger, guard, monkeypatch)
    fake.snapshot = down
    report = adapter.reconcile()
    assert report.read_failed and report.mapped == [] and entry.order_id not in adapter._orders

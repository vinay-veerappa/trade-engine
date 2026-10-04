"""Hand mutants of the P3b-2b switched flow. Build and green baseline FIRST, rebuild EACH mutant.

Run with this worktree's interpreter: python -B tools\\mutate_p3b2b.py [names ...].
Every mutation must compile and fail the frozen-oracle lockstep tests. The mutants
target the command flow (flow.rs) and the plan functions it calls (manager.rs); each
file's original bytes are restored in finally, followed unconditionally by a rebuild
and a green run.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OMS = ROOT / "crates" / "te_core" / "src" / "oms"
FLOW = OMS / "flow.rs"
PLAN = OMS / "manager.rs"
PY = sys.executable
ENV = dict(os.environ, PYTHONPATH=str(ROOT / "src"))
TEST = [PY, "-B", "-m", "pytest", str(ROOT / "tests" / "test_p3b2b_flow.py"),
        str(ROOT / "tests" / "test_p3b2_parity.py"), "-x", "-q", "-p", "no:cacheprovider"]
MUTANTS = [
    # durable before network
    ("create-bracket-append-skipped", FLOW,
     "append(h, &intent.account_id, EventKind::OrdersCreated, Obj::OrdersCreated(batch), &intent.command_id)?;",
     "let _ = batch;"),
    ("submit-pending-marker-skipped", FLOW,
     '    mark_pending(\n        h,\n        &submitted,\n        "Submit outcome is pending until the venue responds",\n'
     '        &format!("{}:submit-pending", order.command_id),\n    )?;\n',
     ""),
    ("replace-pending-append-skipped", FLOW,
     "append(h, &order.account_id, EventKind::OrderPending, pending_payload, &pending_command_id)?;",
     "let _ = pending_payload;"),
    ("cancel-pending-marker-skipped", FLOW,
     'mark_pending(h, &current, &format!("Cancel pending: {reason}"), &format!("{command}:pending"))?;',
     "let _ = &current;"),
    # submit
    ("submit-replay-sends-argument-not-stored", FLOW,
     "    submit_native(h, &current)\n}", "    submit_native(h, order)\n}"),
    ("submit-unknown-ack-accepted", PLAN,
     '_=>return management(format!("Unrecognized venue submit status {}",py_repr(status))),',
     '_=>(EventKind::OrderAccepted,"Venue accepted order".into()),'),
    # replace
    ("replace-status-resolves-pending", PLAN, 'if status=="PENDING" {', 'if false {'),
    ("replace-emulated-stop-not-persisted", FLOW,
     "            order: updated,\n            reason,\n            venue_order_id: None,\n        }),\n        &local_command_id,",
     "            order: order.clone(),\n            reason,\n            venue_order_id: None,\n        }),\n        &local_command_id,"),
    # cancel
    ("oco-sibling-cancel-skipped", FLOW,
     'cancel_exits(h, &siblings, &format!("{}:{}:trigger", command, order.order_id))',
     "{ let _ = siblings; Ok(()) }"),
    ("cancel-unknown-not-oco-kind", FLOW,
     'return err(if oco { "oco_unknown" } else { "broker_unknown" }, msg);',
     'return err("broker_unknown", msg);'),
    # fills
    ("fill-replay-double-counted", FLOW,
     'append(h, &order.account_id, EventKind::Fill, payload, &format!("fill:{}", fill.fill_id))?;',
     'append(h, &order.account_id, EventKind::Fill, payload, &format!("fill:{}:{}", fill.fill_id, h.events_of_kind(EventKind::Fill)?.len()))?;'),
    ("fill-skips-bracket-sync", FLOW,
     "synchronize_bracket(h, &order, &fill.fill_id)?;", "let _ = &fill.fill_id;"),
    # emulation
    ("stop-touch-does-not-trigger", PLAN,
     'let triggered=if o.side==Side::Buy { ge(price,stop)? } else { le(price,stop)? };',
     'let triggered=if o.side==Side::Buy { gt(price,stop)? } else { lt(price,stop)? };'),
    ("trigger-prefers-limit", PLAN,
     "if types.contains(&OrderType::Market) { Some(OrderType::Market) }",
     "if types.contains(&OrderType::Market) { Some(OrderType::Limit) }"),
    ("native-stop-ignores-capability", PLAN,
     "(!(t == OrderType::Stop || t == OrderType::StopLimit) || native_stops) && supported.contains(&t)",
     "(true || native_stops) && supported.contains(&t)"),
    # reconcile
    ("reconcile-readback-resends", FLOW,
     "    let order = ctx.order;\n    let states = h.orders(&order.created_at)?;",
     "    let order = ctx.order;\n    let (_, resend) = venue_order(h, &order, None)?;\n    let _ = h.submit(&resend)?;\n    let states = h.orders(&order.created_at)?;"),
    ("reconcile-terminal-readback-not-applied", PLAN,
     "OrderState::Cancelled=>Ok(Resolution::Record(EventKind::OrderCancelled)),",
     "OrderState::Cancelled=>Ok(Resolution::Return),"),
    ("reconcile-ignores-unresolved-replace", FLOW,
     "plan::reconcile_replace(order_id, found.state, has_unresolved_replace(h, order_id)?)?;",
     "let _ = has_unresolved_replace(h, order_id)?;"),
    # brackets
    ("send-reduce-submits-before-cancelling-targets", FLOW,
     '    cancel_exits(h, &targets, &format!("{command}:replaces-targets"))?;\n    submit(h, reduce)\n',
     '    let sent = submit(h, reduce)?;\n    cancel_exits(h, &targets, &format!("{command}:replaces-targets"))?;\n    Ok(sent)\n'),
    ("close-replay-ignores-stored-close", FLOW,
     "if plan::close_replay(existing, entry, command)? {",
     "if false && plan::close_replay(existing, entry, command)? {"),
    ("reduce-rounds-nearest", PLAN,
     "let q=floor(&mul(open,fraction)?)?;",
     "let q=mul(open,fraction)?.to_integral(Round::HalfEven).map_err(derr)?;"),
    ("sync-forgets-runner-budget", PLAN,
     "let runner=sub(planned,&sum(weights)?)?;", "let runner=zero();"),
    ("sync-activates-targets-before-entry-terminal", PLAN,
     "Ok(eq(stop_filled,&zero())? && entry_terminal)", "eq(stop_filled,&zero())"),
    # plumbing
    ("utc-now-accepts-naive-clock", FLOW, "    if !aware {", "    if false {"),
    ("append-replay-ignores-payload-conflict", PLAN,
     "prior.kind==Some(kind) && payload_equal(&existing,&requested)?;",
     "prior.kind==Some(kind) && { let _ = payload_equal(&existing,&requested)?; true };"),
]


def build():
    subprocess.run([PY, "-m", "pip", "install", "--no-deps", "--force-reinstall", "-q",
                    str(ROOT / "crates" / "te_py")],
                   cwd=ROOT, env=ENV, check=True, capture_output=True)


def tests():
    return subprocess.run(TEST, cwd=ROOT, env=ENV, capture_output=True, text=True)


def main():
    selected = set(sys.argv[1:])
    unknown = selected - {name for name, *_ in MUTANTS}
    if unknown:
        raise ValueError(f"unknown mutants: {sorted(unknown)}")
    originals = {path: path.read_bytes() for path in (FLOW, PLAN)}
    failures = []
    killed = []
    baseline_green = False
    final_green = False
    try:
        build()
        baseline = tests()
        baseline_green = baseline.returncode == 0
        if not baseline_green:
            print("BASELINE RED\n" + baseline.stdout[-4000:], flush=True)
        else:
            print("baseline green", flush=True)
            for name, path, old, new in MUTANTS:
                if selected and name not in selected:
                    continue
                text = originals[path].decode("utf-8")
                if text.count(old) != 1:
                    failures.append((name, f"original string {text.count(old)}x"))
                    print(f"ORIGINAL STRING {text.count(old)}x: {name}", flush=True)
                    continue
                try:
                    path.write_bytes(text.replace(old, new).encode("utf-8"))
                    try:
                        build()
                    except subprocess.CalledProcessError as error:
                        failures.append((name, "does not build"))
                        print(f"BUILD FAILURE: {name}\n{error.stderr[-1600:]!r}", flush=True)
                        continue
                    result = tests()
                    if result.returncode == 0:
                        failures.append((name, "survived"))
                        print("SURVIVED: " + name, flush=True)
                    else:
                        lines = [line for line in result.stdout.splitlines() if line.startswith("FAILED")]
                        if not lines:
                            failures.append((name, "no assertion failure evidence"))
                            print(f"INVALID KILL: {name}\n{result.stdout[-1600:]}", flush=True)
                        else:
                            killed.append(name)
                            print(f"killed: {name} <- {lines[0]}", flush=True)
                finally:
                    for original_path, data in originals.items():
                        original_path.write_bytes(data)
    finally:
        for original_path, data in originals.items():
            original_path.write_bytes(data)
        build()
        result = tests()
        final_green = result.returncode == 0
        print("final restored lockstep: " + ("green" if final_green else "RED"), flush=True)
        if not final_green:
            print(result.stdout[-4000:], flush=True)
        for original_path, data in originals.items():
            assert original_path.read_bytes() == data, "original bytes not restored"
    print(f"{len(killed)} killed; failures: {failures or 'none'}", flush=True)
    return 0 if baseline_green and final_green and not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())

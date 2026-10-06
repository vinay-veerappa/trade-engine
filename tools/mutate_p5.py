"""Compiling P5 (T1-T9: transport, normalize, slippage, netting, cover, reconcile, exits, follow, broker) hand mutants: assertion kills only, finally restore, final green build."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent.parent
PY = Path(sys.executable).resolve()
SOURCES = ROOT / "crates" / "te_core" / "src" / "tos_paper"
ENV = dict(os.environ, PATH=str(Path.home() / ".cargo" / "bin") + os.pathsep + os.environ["PATH"],
           PYTHONPATH=str(ROOT / "src"), PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8",
           CARGO_TARGET_DIR=os.environ.get("CARGO_TARGET_DIR", str(ROOT / "crates" / "target")))
TEST = [str(PY), "-B", "-m", "pytest", str(ROOT / "tests" / "test_p5_broker_parity.py"),
        str(ROOT / "tests" / "test_p5_parity.py"),
        "-x", "-q", "--tb=short", "-p", "no:cacheprovider",
        "--basetemp", str(ROOT / ".ci-local" / "p5-mutant-tests")]
MUTANTS = (
    ("fee-rounds-nearest-not-floor", "slippage.rs", "d.quantize_round(-2, Round::Floor)",
     "d.quantize_round(-2, Round::HalfEven)"),
    ("fee-remainder-dropped", "slippage.rs", "fees[0] = add(&fees[0], &remainder)?;",
     "fees[0] = fees[0].clone();"),
    ("zero-share-gets-a-fill", "slippage.rs", "if gt(&piece, &zero())? {", "if !lt(&piece, &zero())? {"),
    ("adverse-sign-flipped", "slippage.rs",
     'if side == "BUY" { sub(&venue_price, &sim_price)? } else { sub(&sim_price, &venue_price)? }',
     'if side == "BUY" { sub(&sim_price, &venue_price)? } else { sub(&venue_price, &sim_price)? }'),
    ("zero-sim-price-gets-bps", "slippage.rs", "if gt(&sim_price, &zero())? {", "if !lt(&sim_price, &zero())? {"),
    ("sent-marked-accepted", "normalize.rs", '"SENT" => ack("PENDING", "sent; awaiting read-back".into()),',
     '"SENT" => ack("ACCEPTED", "sent; awaiting read-back".into()),'),
    ("dry-run-marked-accepted", "normalize.rs", '"DRY_RUN" => ack("PENDING"', '"DRY_RUN" => ack("ACCEPTED"'),
    ("unknown-status-marked-accepted", "normalize.rs", '_ => ack("PENDING", format!("unknown transport status',
     '_ => ack("ACCEPTED", format!("unknown transport status'),
    ("order-id-read-from-any-status", "normalize.rs", 'status_of(raw, "status") != "SENT"',
     'status_of(raw, "status") == "SENT_"'),
    ("fractional-shares-accepted", "normalize.rs", "d.is_integral().map_err(derr)", "Ok(true)"),
    ("status-not-stripped", "pytext.rs", "strip(s).to_uppercase()", "s.to_uppercase()"),
    ("whole-contracts-check-off", "transport.rs", "q.is_integral().map_err(derr)", "Ok(true)"),
    ("zero-limit-accepted", "transport.rs", "Some(d) => d.le(&PyDec::zero()).map_err(derr),",
     "Some(d) => d.lt(&PyDec::zero()).map_err(derr),"),
    ("one-strike-vertical", "netting.rs", "if first.strike.eq_num(&second.strike).map_err(derr)? {", "if false {"),
    ("ratio-spread-vertical", "netting.rs", "if legs[0].ratio != legs[1].ratio {", "if false {"),
    ("same-side-vertical", "netting.rs", "if legs[0].side == legs[1].side {", "if false {"),
    ("netting-conflict-sign-inverted", "netting.rs", "if *first_side != side {", "if *first_side == side {"),
    ("netting-fractional-rounded-not-refused", "netting.rs", "if !order.quantity.is_integral().map_err(derr)? {", "if false {"),
    ("cover-credit-diagonal-accepted", "cover.rs", "long.strike.le(&short.strike).map_err(derr)?)", "true)"),
    # Reversing the longs a short tries is EQUIVALENT (greedy over a transversal matroid yields the same
    # covered-short set for any long order); reversing the order shorts are matched in is not.
    ("cover-kuhn-short-order-reversed", "cover.rs", "for short in 0..shorts.len() {", "for short in (0..shorts.len()).rev() {"),
    ("cover-open-sell-tickets-not-subtracted", "cover.rs",
     "for (instrument, quantity) in ticket_contracts(&ticket.queued, &ticket.remaining()?)?.iter() {",
     "for (instrument, quantity) in ticket_contracts(&ticket.queued, &ticket.remaining()?)?.iter().take(0) {"),
    ("reconcile-unknown-working-state-ignored", "reconcile.rs", "if row.state == OrderState::PendingUnknown {\n            unknown.insert",
     "if false {\n            unknown.insert"),
    ("reconcile-one-share-drift-tolerated", "reconcile.rs", "if unknown.contains(&contract.hk()) || ne(&have, &want)? {",
     "if unknown.contains(&contract.hk()) || crate::ledger::ops::gt(&crate::ledger::ops::abs(&sub(&have, &want)?)?, &PyDec::from_i128(1))? {"),
    ("exits-vertical-closed-leg-by-leg", "exits.rs", "if in_vertical(&ctx.mirror, account, contract)? {", "if false {"),
    ("exits-target-rested-gtc", "exits.rs", '("tif", jstr(Tif::Day.value())),', '("tif", jstr(Tif::Gtc.value())),'),
    ("exits-zero-limit-priced", "exits.rs", "Some(l) if !le(l, &zero())? => None,", "Some(l) if !lt(l, &zero())? => None,"),
    ("exits-shared-ticket-ignored", "exits.rs", "let shared = Self::shared_with(&resting, account);",
     "let shared: Vec<String> = Self::shared_with(&resting, account).into_iter().take(0).collect();"),
    ("exits-target-room-not-decremented", "exits.rs", "room = sub(&room, &quantity)?;", "room = room.clone();"),
    ("follow-entry-older-than-max-age-accepted", "follow.rs", "if age > max_age_us {", "if false {"),
    ("follow-max-age-boundary-inclusive", "follow.rs", "if age > max_age_us {", "if age >= max_age_us {"),
    ("follow-pass-named-in-utc", "follow.rs", "New_York.timestamp_opt(unix, 0)", "chrono_tz::UTC.timestamp_opt(unix, 0)"),
    ("follow-combo-flat-always", "follow.rs", "Some(u) => eq(&u, &zero()),", "Some(_) => Ok(true),"),
    ("broker-contradiction-does-not-latch-halt", "broker.rs",
     "let event = venue_reconcile(&self.venue, &now, false, names, Some(note));\n            return Err(self.unreadable_halt(event));",
     "let event = venue_reconcile(&self.venue, &now, false, names, Some(note));\n"
     "            return Err(match event { Ok(e) => LErr { kind: VENUE_UNREADABLE, msg: json::dumps(&e) }, Err(e) => e });"),
    ("broker-halt-not-sticky-across-restore", "broker.rs",
     "if halted_venues.contains(&self.venue) {\n            self.halted = true;\n        }",
     "self.halted = halted_venues.contains(&self.venue);"),
    ("broker-restore-ignores-halted-venues", "broker.rs",
     "if halted_venues.contains(&self.venue) {\n            self.halted = true;", "if false {\n            self.halted = true;"),
    ("broker-failed-send-left-in-expected", "broker.rs",
     'if sent.status == "REJECTED" {\n                self.unexpect(ticket, None)?;\n                acks.push(sent);',
     'if sent.status == "REJECTED" {\n                acks.push(sent);'),
    ("broker-cancel-of-unsent-hits-the-venue", "broker.rs",
     "self.unexpect(&queued, None)?;\n            self.cancelled.insert(venue_order_id.to_string());",
     "self.unexpect(&queued, None)?;\n            let _ = h.cancel_order(venue_order_id);\n            self.cancelled.insert(venue_order_id.to_string());"),
    ("broker-drain-continues-after-failed-send", "broker.rs",
     "                acks.push(sent);\n                continue;\n            }\n",
     "            }\n"),
    ("broker-unexpect-wrong-sign", "broker.rs", "add_ticket(&mut self.expected, ticket, -1, units)",
     "add_ticket(&mut self.expected, ticket, 1, units)"),
    ("broker-proven-order-id-before-read-back", "broker.rs",
     "self.sent.get(ticket_key).map_or(Json::Null, |s| jstr(s.order_id.clone()))",
     "self.sent.get(ticket_key).map_or(jstr(ticket_key.to_string()), |s| jstr(s.order_id.clone()))"),
)


def paths():
    assert PY == (ROOT / ".venv" / "Scripts" / "python.exe").resolve()
    assert ROOT.name == "rust-p5"
    out = subprocess.check_output([str(PY), "-B", "-c",
        "import sys,trade_engine,trade_engine_rs;print(sys.executable);print(trade_engine.__file__);print(trade_engine_rs.__file__)"],
        cwd=ROOT, env=ENV, text=True).strip().splitlines()
    assert Path(out[0]).resolve() == PY
    assert Path(out[1]).resolve().is_relative_to(ROOT / "src")
    assert Path(out[2]).resolve().is_relative_to(ROOT / ".venv")


def build():
    start = time.perf_counter()
    subprocess.run([str(PY), "-m", "pip", "install", "--no-deps", "--force-reinstall", "-q",
                    str(ROOT / "crates" / "te_py")], cwd=ROOT, env=ENV, check=True, capture_output=True)
    paths()
    return time.perf_counter() - start


def tests():
    return subprocess.run(TEST, cwd=ROOT, env=ENV, capture_output=True,
                          text=True, encoding="utf-8", errors="replace")


def main():
    (ROOT / ".ci-local").mkdir(exist_ok=True)
    selected = set(sys.argv[1:])
    assert selected <= {m[0] for m in MUTANTS}, selected
    originals = {SOURCES / filename: (SOURCES / filename).read_bytes() for _, filename, _, _ in MUTANTS}
    kills, failures = [], []
    baseline_green = restored_green = False
    try:
        seconds = build()
        baseline = tests()
        baseline_green = baseline.returncode == 0
        print(f"baseline exit={baseline.returncode} build={seconds:.3f}s\n{baseline.stdout[-2000:]}", flush=True)
        if baseline_green:
            for name, filename, anchor, replacement in MUTANTS:
                if selected and name not in selected:
                    continue
                source = SOURCES / filename
                text = originals[source].decode("utf-8")
                assert text.count(anchor) == 1, (name, text.count(anchor))
                try:
                    source.write_bytes(text.replace(anchor, replacement).encode("utf-8"))
                    try:
                        seconds = build()
                    except subprocess.CalledProcessError as exc:
                        failures.append((name, "compile failed"))
                        print(f"INVALID {name}: compile failed {exc.stderr[-2000:]!r}", flush=True)
                        continue
                    proc = tests()
                    failed = [line for line in proc.stdout.splitlines() if line.startswith("FAILED")]
                    assertion = (proc.returncode == 1 and failed and "AssertionError" in proc.stdout
                                 and "ERROR collecting" not in proc.stdout and "ImportError" not in proc.stdout)
                    if assertion:
                        kills.append((name, failed[0]))
                        print(f"KILLED {name} build={seconds:.3f}s {failed[0]}", flush=True)
                    else:
                        failures.append((name, "survived" if proc.returncode == 0 else "not an assertion kill"))
                        print(f"INVALID {name} exit={proc.returncode}\n{proc.stdout[-5000:]}", flush=True)
                finally:
                    source.write_bytes(originals[source])
    finally:
        for source, original in originals.items():
            source.write_bytes(original)
        seconds = build()
        restored = tests()
        restored_green = restored.returncode == 0
        print(f"restored exit={restored.returncode} build={seconds:.3f}s\n{restored.stdout[-2000:]}", flush=True)
        for source, original in originals.items():
            assert source.read_bytes() == original
            print(f"restored SHA256 {source.name} {hashlib.sha256(original).hexdigest()}", flush=True)
        paths()
    print(f"killed={len(kills)} failures={failures}", flush=True)
    return 0 if baseline_green and restored_green and not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""P4c paper-ledger EOD catch-up planner (reconstructed; plan-only by default).

Reads two ledgers (options and scan), finds the newest EodRun marker per account,
lists the exchange sessions with no marker since then, and prints the submit
commands a catch-up would run, with where port / capability / generation come from.

Read-only: opens ledgers with LedgerReader (no lock, no mkdir). It never reads
capability values or token files, never prints the capability, and never submits.

    python tools/p4c_catchup_eod.py --options-ledger PATH --scan-ledger PATH \
        [--as-of YYYY-MM-DD] [--since YYYY-MM-DD]

--execute is accepted only to be refused: this reconstruction does not submit.
Submit by hand, after the owner approves, using the printed commands.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
# Owner endpoint files are written by the tvDownloadOHLC runner, not this repo.
TVD_REPO = Path("C:/Users/vinay/tvDownloadOHLC")
sys.path.insert(0, str(REPO / "src"))

from trade_engine.calendar.sessions import ExchangeCalendar  # noqa: E402
from trade_engine.ledger.events import EventKind  # noqa: E402
from trade_engine.ledger.reader import LedgerReader  # noqa: E402

# Owner endpoints (port + where the capability/generation live). Values are NOT printed.
OWNERS = {
    "options": {
        "port": 8390,
        "prefix": "TE_OPT_OWNER",
        "endpoint": "logs/trade_engine/runtime-owners/batch.json",
    },
    "scan": {
        "port": 8391,
        "prefix": "TE_RUNTIME",
        "endpoint": "logs/trade_engine/runtime-owners/scan.json",
    },
}


def newest_eod_session_per_account(ledger_path: Path) -> dict[str, date]:
    """Newest EodRun session per account. Empty dict if the ledger has no markers."""
    reader = LedgerReader(ledger_path).open()
    try:
        newest: dict[str, date] = {}
        for ev in reader.events_of_kind(EventKind.EOD_RUN):
            session = ev.payload.session
            current = newest.get(ev.account)
            if current is None or session > current:
                newest[ev.account] = session
        return newest
    finally:
        reader.close()


def missing_sessions(cal: ExchangeCalendar, after: date, through: date) -> list[date]:
    """Exchange sessions in (after, through]."""
    if after >= through:
        return []
    return cal.sessions_in_range(after + timedelta(days=1), through)


def endpoint_status(owner: str) -> str:
    """Report only whether the endpoint file exists and its shape, never its values."""
    spec = OWNERS[owner]
    path = TVD_REPO / spec["endpoint"]
    if not path.exists():
        return f"endpoint file MISSING: {path}"
    return f"endpoint file present: {path} (capability/generation read at submit time, not printed)"


def build_plan(args: argparse.Namespace) -> list[str]:
    cal = ExchangeCalendar("XNYS")
    through = date.fromisoformat(args.as_of) if args.as_of else cal.previous_session(date.today())
    since = date.fromisoformat(args.since) if args.since else None

    lines: list[str] = [f"P4c EOD catch-up plan (plan-only), through session {through.isoformat()}"]
    ledgers = [
        ("options", Path(args.options_ledger), "options-eod"),
        ("scan", Path(args.scan_ledger), "eod"),
    ]
    for owner, ledger_path, job in ledgers:
        lines.append("")
        lines.append(f"[{owner}] ledger {ledger_path}")
        if not ledger_path.exists():
            lines.append("  ledger MISSING; nothing planned for this owner")
            continue
        newest = newest_eod_session_per_account(ledger_path)
        if not newest:
            lines.append("  no EodRun markers at all; catch-up start must be set with --since")
        for account in sorted(newest):
            last = newest[account]
            start = since or last
            gaps = missing_sessions(cal, start, through)
            lines.append(f"  account {account}: last marker {last.isoformat()}, "
                         f"missing sessions {len(gaps)}"
                         + (f" ({gaps[0].isoformat()}..{gaps[-1].isoformat()})" if gaps else ""))
        # Request ids. Options submits are keyed by role + session (not account);
        # scan submits are keyed by account + session. Sessions are the union of gaps.
        all_gaps: set[date] = set()
        for account in newest:
            all_gaps.update(missing_sessions(cal, since or newest[account], through))
        if owner == "options":
            lines.append("  request ids: options-eod:<batch|intraday>:<session> "
                         f"for {len(all_gaps)} session(s) (retry suffix :retryN)")
        else:
            lines.append(f"  request ids: {job}:<account>:<session> "
                         f"(retry suffix :retryN); {len(all_gaps)} session(s) across accounts")
        lines.append(f"  {endpoint_status(owner)} -> port {OWNERS[owner]['port']}, "
                     f"prefix {OWNERS[owner]['prefix']}")
        lines.append("  submit: scan_engine/runtime_rollout_job.py --name <job> --arguments <args> "
                     "--config <cfg> --endpoint-port <port> --endpoint-capability <from endpoint file> "
                     "--endpoint-generation <gen:... from endpoint file>")
    lines.append("")
    lines.append("Options flags to confirm with --help before use: --session, --missing-scan-is-empty, "
                 "--closing-quotes, --accounts, --repo-root")
    lines.append("Do not run with --missing-scan-is-empty unless the scan input is confirmed absent by design "
                 "(see the result file's section 3).")
    return lines


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--options-ledger", required=True)
    p.add_argument("--scan-ledger", required=True)
    p.add_argument("--as-of", help="last session to catch up through (default: previous session)")
    p.add_argument("--since", help="override the start session for every account")
    p.add_argument("--execute", action="store_true",
                   help="refused in this reconstruction; submit by hand after owner approval")
    args = p.parse_args()

    if args.execute:
        print("REFUSED: --execute is not wired in this reconstruction. No job was submitted.",
              file=sys.stderr)
        return 2

    for line in build_plan(args):
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

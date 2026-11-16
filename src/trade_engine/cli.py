"""Command line interface for trade_engine."""

import argparse
import sys
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

import trade_engine


def _session_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as err:
        raise argparse.ArgumentTypeError(f"expected YYYY-MM-DD, got {value!r}") from err


def _slippage_bps(value: str) -> Decimal:
    try:
        slippage = Decimal(value)
    except InvalidOperation as err:
        raise argparse.ArgumentTypeError(f"expected a number, got {value!r}") from err
    if not slippage.is_finite() or slippage < 0:
        raise argparse.ArgumentTypeError(f"must be finite and non-negative, got {value!r}")
    return slippage


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="trade_engine",
        description="Generic event-sourced trading engine",
    )
    parser.add_argument(
        "-v",
        "--version",
        action="store_true",
        help="Show version and resolved package path",
    )
    subparsers = parser.add_subparsers(dest="command")
    eod_parser = subparsers.add_parser(
        "eod", help="Run the end-of-day job for one session (E7, Architecture §4.9)"
    )
    eod_parser.add_argument(
        "--session", required=True, type=_session_date, help="Session date YYYY-MM-DD"
    )
    eod_parser.add_argument("--ledger", required=True, help="Path to the ledger database")
    eod_parser.add_argument(
        "--market-data",
        help="Name of a trade_engine.marketdata entry-point plugin to source bars from",
    )
    eod_parser.add_argument(
        "--accounts",
        default="",
        help="Comma-separated account ids to run (default: every account in the ledger)",
    )
    eod_parser.add_argument(
        "--slippage-bps",
        default=Decimal("0"),
        type=_slippage_bps,
        help="SimBroker slippage in basis points (default 0)",
    )
    eod_parser.add_argument(
        "--void",
        action="store_true",
        help="Void --session for --accounts instead of running it: the owner's signed "
        "record that a session which can never complete is complete (EOD-VOID-1). "
        "Needs --accounts, --reason and --owner",
    )
    eod_parser.add_argument("--reason", default="", help="Why the session is voided (--void)")
    eod_parser.add_argument("--owner", default="", help="Who signs the void (--void)")

    feed_parser = subparsers.add_parser(
        "registry-feed", help="Run the Tradovate registry feed to riskguard (BP-4d)"
    )
    feed_parser.add_argument(
        "--once", action="store_true", help="One snapshot, one pump, print counts, exit"
    )

    args = parser.parse_args(argv)

    if args.version:
        resolved_path = Path(trade_engine.__file__).resolve().parent
        print(f"trade-engine {trade_engine.__version__} (from {resolved_path})")
        return 0

    if args.command == "eod":
        from trade_engine.eod.cli import run_eod, run_void

        return run_void(args) if args.void else run_eod(args)

    if args.command == "registry-feed":
        import json
        import os

        from trade_engine.tradovate import registry_feed

        if args.once:
            code, summary = registry_feed.run_once(os.environ)
            if summary is None:
                print(registry_feed.SINK_OFF_LINE)
            else:
                print(json.dumps(summary, sort_keys=True))
            return code
        import logging

        # The heartbeat and start/stop lines are INFO; without a handler they are dropped and
        # the quantd log stays empty. Counts only are logged (BP-4d), never money or ids.
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
        return registry_feed.main_loop(os.environ)

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
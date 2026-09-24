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

    args = parser.parse_args(argv)

    if args.version:
        resolved_path = Path(trade_engine.__file__).resolve().parent
        print(f"trade-engine {trade_engine.__version__} (from {resolved_path})")
        return 0

    if args.command == "eod":
        from trade_engine.eod.cli import run_eod

        return run_eod(args)

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
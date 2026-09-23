"""Command line interface for trade_engine."""

import argparse
import sys
from pathlib import Path

import trade_engine


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

    args = parser.parse_args(argv)

    if args.version:
        resolved_path = Path(trade_engine.__file__).resolve().parent
        print(f"trade-engine {trade_engine.__version__} (from {resolved_path})")
        return 0

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())

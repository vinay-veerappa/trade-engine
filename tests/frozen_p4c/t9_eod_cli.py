"""`trade_engine eod` subcommand: wire plugins into an EodRunner and run one session.

The CLI stays thin: it resolves what the host would otherwise inject (I7 clock, I5
market data) and refuses when a required piece is absent rather than inventing one.
"""

from __future__ import annotations

import argparse
import sys
from trade_engine.calendar.sessions import ExchangeCalendar
from trade_engine.clock.replay import ReplayClock
from trade_engine.eod import EodRunner, EodRunnerConfig, PluginDiscoveryError, discover_plugins
from trade_engine.eod.runner import EodRunnerError
from trade_engine.ledger import Ledger
from trade_engine.sim import SimBroker


def _resolve_market_data(name: str | None) -> object:
    plugins = discover_plugins()
    found = list(plugins.market_data)
    if not found:
        raise EodRunnerError(
            "No trade_engine.marketdata plugins are installed; cannot source bars (I5)"
        )
    if name is None:
        if len(found) != 1:
            raise EodRunnerError(
                "Exactly one trade_engine.marketdata plugin must be installed to run "
                f"the EOD job; found {len(found)}. Pass --market-data to name one (I5)"
            )
        return found[0]
    matching = [
        plugin
        for plugin in found
        if getattr(plugin, "name", None) == name
        or getattr(type(plugin), "__name__", "") == name
    ]
    if not matching:
        raise EodRunnerError(
            f"No market-data plugin named '{name}' among {[type(p).__name__ for p in found]}"
        )
    return matching[0]


def run_eod(args: argparse.Namespace) -> int:
    session = args.session
    ledger_path = args.ledger
    if not ledger_path:
        print("eod: --ledger is required", file=sys.stderr)
        return 2

    try:
        market_data = _resolve_market_data(args.market_data)
    except (EodRunnerError, PluginDiscoveryError) as err:
        print(f"eod: refused: {err}", file=sys.stderr)
        return 2
    slippage_bps = args.slippage_bps
    # The EOD pass is a replay: the clock must sit at the session open so the runner
    # can advance it bar by bar (I7); a wall clock cannot re-derive a session.
    clock = ReplayClock(ExchangeCalendar().session_open(session))
    calendar = ExchangeCalendar()

    with Ledger(ledger_path) as ledger:
        accounts = [item.strip() for item in args.accounts.split(",") if item.strip()]
        if not accounts:
            known = sorted(ledger.fold())
            accounts = [a for a in known if not a.startswith("__venue__:")]
            if not accounts:
                print(
                    "eod: no accounts in the ledger and none given with --accounts (I5)",
                    file=sys.stderr,
                )
                return 2
        brokers = {
            account_id: SimBroker(account_id, clock, slippage_bps)
            for account_id in accounts
        }
        runner = EodRunner(
            ledger,
            clock,
            calendar,
            market_data,  # type: ignore[arg-type]
            EodRunnerConfig(job_name="eod", brokers=brokers),
        )
        try:
            result = runner.run(session)
        except EodRunnerError as err:
            print(f"eod: refused: {err}", file=sys.stderr)
            return 2
        for account in result.accounts:
            print(
                f"{account.account_id}: bars={account.bars_processed} "
                f"fills={account.fills_recorded} marks={account.marks_appended} "
                f"orders={account.orders_submitted}"
            )
    return 0
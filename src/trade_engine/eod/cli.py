"""`trade_engine eod` subcommand: wire plugins into an EodRunner and run one session.

The CLI stays thin: it resolves what the host would otherwise inject (I7 clock, I5
market data) and refuses when a required piece is absent rather than inventing one.
The one session runs through the native one-shot actor (`eod_once`): admission is
durable before execution, a duplicate reports the persisted record, and the exact
flags, refusals and counters of the pre-port CLI are preserved.
"""

from __future__ import annotations

import argparse
import sys
import uuid
from trade_engine.calendar.sessions import ExchangeCalendar
from trade_engine.clock.replay import ReplayClock
from trade_engine.clock.wall import WallClock
from trade_engine.eod import EodRunner, EodRunnerConfig, PluginDiscoveryError, discover_plugins
from trade_engine.eod.runner import EodRunnerError, _void_command, void_session, write_off_order
from trade_engine.ledger import Ledger
from trade_engine.ledger._rs import call as rs_call
from trade_engine.sim import SimBroker
import trade_engine_rs as _rs


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


def run_void(args: argparse.Namespace) -> int:
    """`eod --void`: write the owner-signed void of one session for the named accounts.

    No market data and no runner: a void is a ledger record, not a replay. Every
    refusal (no reason or owner, no accounts, an unknown account, a session that
    already ran) writes nothing and exits 2; a repeat void prints the same lines and
    writes nothing new (I3).
    """
    ledger_path = args.ledger
    if not ledger_path:
        print("eod: --ledger is required", file=sys.stderr)
        return 2
    accounts = [item.strip() for item in args.accounts.split(",") if item.strip()]
    if not accounts:
        print("eod: --void needs --accounts; a void never defaults to every account", file=sys.stderr)
        return 2
    session = args.session
    with Ledger(ledger_path) as ledger:
        before = {a for a in accounts if ledger.event_by_command(_void_command("eod", a, session))}
        try:
            done = void_session(
                ledger, WallClock(), ExchangeCalendar(), "eod", session, accounts,
                reason=args.reason, owner=args.owner,
            )
        except EodRunnerError as err:
            print(f"eod: refused: {err}", file=sys.stderr)
            return 2
    for account_id in done:
        state = "already voided" if account_id in before else "voided"
        print(f"{account_id}: {state} {session.isoformat()} (reason: {args.reason.strip()}; owner: {args.owner.strip()})")
    return 0


def run_write_off(args: argparse.Namespace) -> int:
    """`eod --write-off`: the owner-signed cancel of one stuck working order.

    No market data and no runner: a ledger record. Needs exactly one account. Every
    refusal writes nothing and exits 2; a repeat prints ``already written off``.
    """
    ledger_path = args.ledger
    if not ledger_path:
        print("eod: --ledger is required", file=sys.stderr)
        return 2
    accounts = [item.strip() for item in args.accounts.split(",") if item.strip()]
    if len(accounts) != 1:
        print("eod: --write-off needs exactly one --accounts", file=sys.stderr)
        return 2
    if not args.order.strip():
        print("eod: --write-off needs --order", file=sys.stderr)
        return 2
    with Ledger(ledger_path) as ledger:
        try:
            done = write_off_order(
                ledger, WallClock(), accounts[0], args.order.strip(),
                reason=args.reason, owner=args.owner,
            )
        except EodRunnerError as err:
            print(f"eod: refused: {err}", file=sys.stderr)
            return 2
    if not done:
        print(f"{accounts[0]}: already written off {args.order.strip()}")
        return 0
    for order_id in done:
        print(f"{accounts[0]}: written off {order_id} (reason: {args.reason.strip()}; owner: {args.owner.strip()})")
    return 0


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
        # The one-shot actor admits durably, runs the existing flow once and
        # records the terminal state. Each invocation is a distinct request;
        # re-running a recorded session refuses through the runner's own
        # command-id machinery (I3), exactly as the pre-port CLI did.
        request_id = f"eod:{session.isoformat()}:{uuid.uuid4().hex[:16]}"
        record = rs_call(_rs.eod_once, ledger, runner, session, request_id)
        state = record["state"]
        if state == "completed":
            for account in record["result"]["accounts"]:
                print(
                    f"{account['account_id']}: bars={account['bars_processed']} "
                    f"fills={account['fills_recorded']} marks={account['marks_appended']} "
                    f"orders={account['orders_submitted']}"
                )
            return 0
        if state == "refused":
            print(f"eod: refused: {record['error']['message']}", file=sys.stderr)
            return 2
        if state == "failed":
            failure = record["error"]
            raise RuntimeError(f"{failure['type']}: {failure['message']}")
        print(
            f"eod: session {session.isoformat()} was admitted as {state}; "
            "no side effects were replayed",
            file=sys.stderr,
        )
        return 2
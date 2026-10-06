"""Nine paired books/path; blocking connects under an independent process watchdog."""
from __future__ import annotations

from contextlib import ExitStack
from decimal import Decimal
import json
import os
from pathlib import Path
import socket
import statistics
import subprocess
import sys
import tempfile
from time import perf_counter

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from test_p4c_http import books, exchange, next_frame, request, stream, T
from trade_engine.domain.instruments import Equity, Side
from trade_engine.domain.orders import Order, OrderType
from trade_engine.ledger.events import Event, EventKind


def blocking_connect(address, *, timeout):
    if address[0] != "127.0.0.1" or address[1] in (3410, 3411, 8097):
        raise ValueError("timing client requires an isolated loopback port")
    with ExitStack() as stack:
        peer = stack.enter_context(socket.socket(socket.AF_INET, socket.SOCK_STREAM))
        peer.setblocking(True)
        peer.connect(address)
        peer.settimeout(timeout)
        stack.pop_all()
        return peer


def measure(ledger, server, role):
    start = perf_counter()
    if role in ("health", "snapshot"):
        result = [exchange(server, request("/" + role), connect=blocking_connect) for _ in range(20)]
    elif role == "backlog":
        with stream(server, request("/events"), connect=blocking_connect) as (_, reader, header):
            result = [header, next_frame(reader)] + [next_frame(reader) for _ in range(251)]
    else:
        with stream(server, request("/events?after=251"), connect=blocking_connect) as (_, reader, header):
            result = [header, next_frame(reader)]
            for index in range(20):
                order = Order(f"timing-{index}", "ACC-\u00e9", Equity("MSFT"), OrderType.MARKET,
                    Side.BUY, Decimal("1"), f"timing-{index}", T)
                ledger.append(Event("ACC-\u00e9", EventKind.ORDER_SUBMITTED, order, T, command_id=f"timing-{index}"))
                frame = next_frame(reader)
                while frame == b": ping\n\n":
                    frame = next_frame(reader)
                result.append(frame)
    return perf_counter() - start, result


def measured(folder):
    assert Path(sys.executable).resolve() == (ROOT / ".venv" / "Scripts" / "python.exe").resolve()
    folder = Path(folder).resolve()
    if folder.parent != Path(tempfile.gettempdir()).resolve() or not folder.name.startswith("t2-http-timing-"):
        raise ValueError("timing worker requires a parent-managed temporary fixture root")
    report = {}
    for role in ("health", "snapshot", "backlog", "live"):
        samples = []
        for index in range(9):
            root = Path(folder) / f"{role}-{index}"
            root.mkdir()
            with books(root, count=250, ping=1.0) as (ledgers, servers, _):
                outputs, elapsed = {}, {}
                for which in ((0, 1) if index % 2 == 0 else (1, 0)):
                    elapsed[which], outputs[which] = measure(ledgers[which], servers[which], role)
                assert outputs[0] == outputs[1], (role, index)
                samples.append((elapsed[0], elapsed[1]))
        old, new = zip(*samples)
        report[role] = {
            "provenance": "synthetic", "pairs": len(samples), "events_per_book": 251,
            "oracle_median_seconds": statistics.median(old), "native_median_seconds": statistics.median(new),
            "oracle_tail_seconds": max(old), "native_tail_seconds": max(new),
            "median_ratio": statistics.median(new) / statistics.median(old), "samples": samples,
        }
    print(json.dumps(report, indent=2), flush=True)
    assert all(path["median_ratio"] <= 1.25 for path in report.values()), report


def bounded(command, *, timeout=300):
    env = None
    if sys.platform == "win32" and Path(command[0]).resolve() == Path(sys.executable).resolve():
        # Mirror CPython's venv redirector without its extra process, so killing
        # the watchdog child also closes its sockets and ledger handles.
        env = dict(os.environ, __PYVENV_LAUNCHER__=sys.executable)
        command = [sys._base_executable, *command[1:]]
    try:
        return subprocess.run(command, cwd=ROOT, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout, env=env)
    except subprocess.TimeoutExpired as error:
        raise RuntimeError(f"timing watchdog exceeded {timeout}s; measured child was killed and reaped") from error


def main():
    with tempfile.TemporaryDirectory(prefix="t2-http-timing-") as folder:
        result = bounded([sys.executable, "-B", str(Path(__file__).resolve()), "--measure", folder])
        sys.stdout.write(result.stdout)
        sys.stderr.write(result.stderr)
        return result.returncode


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--measure":
        measured(Path(sys.argv[2]))
        raise SystemExit(0)
    raise SystemExit(main())

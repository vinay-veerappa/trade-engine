"""G3: the live read-only check of the Tradovate account plane against riskguard.

What it does, in order, and nothing else:

1. `GET /health` on the ntx daemon. If any identity is not healthy (status 200 and
   `degraded` false) it stops with "G3 blocked on ntx 401" and never touches `/snapshot`
   or `/events`. It does not log in, refresh a token or recover a session.
2. One `GET /snapshot` through `TradovateAdapter.accounts()` (flat, read-only; no order
   path exists in the adapter).
3. One `GET /v1/fleet` on riskguard (read-only), then compares the ntx accounts with the
   fleet rows by broker account id (riskguard's `venue` is the firm, never "tradovate").
4. RSS and CPU of this process (engine account plane) and of the riskguard process
   (found by its listening port), before and after. Budget: engine +20 MB RSS,
   riskguard +5 MB RSS.

Output is masked: only the last 3 characters of an account name or id are printed, and
the tokens are never printed. Loopback only. It never kills or signals a process.

Exit status: 0 pass; 2 blocked on ntx health; 3 count/key mismatch; 4 budget breach;
1 any other failure.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import re
import subprocess
import sys
from ctypes import wintypes
from http.client import HTTPConnection
from pathlib import Path
from typing import Any

ENGINE_BUDGET_MB = 20.0
RISKGUARD_BUDGET_MB = 5.0
RISKGUARD_DEFAULT_PORT = 8791
RISKGUARD_DEFAULT_TOKENS = r"C:\Users\vinay\nt8-riskguard\tokens.local.json"
_READ_ROLES = ("read", "reader", "viewer", "readonly", "read_only", "admin", "operator")

EXIT_PASS, EXIT_ERROR, EXIT_BLOCKED, EXIT_MISMATCH, EXIT_BUDGET = 0, 1, 2, 3, 4


def mask(text: object) -> str:
    """The last 3 characters only."""
    s = str(text)
    return "***" + s[-3:] if len(s) > 3 else "***"


# -- process stats without psutil (Windows API, read-only) ------------------------------


class _Counters(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("PageFaultCount", wintypes.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


def _filetime(ft: wintypes.FILETIME) -> float:
    return ((ft.dwHighDateTime << 32) | ft.dwLowDateTime) / 1e7


def process_stats(pid: int) -> dict[str, float] | None:
    """`{rss_mb, cpu_s}` for `pid`, or None when it cannot be read."""
    if os.name != "nt":
        return None
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE
    handle = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return None
    try:
        counters = _Counters()
        counters.cb = ctypes.sizeof(counters)
        if not psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
            return None
        created, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
        if not k32.GetProcessTimes(
            handle, ctypes.byref(created), ctypes.byref(exited), ctypes.byref(kernel), ctypes.byref(user)
        ):
            return None
        return {
            "rss_mb": counters.WorkingSetSize / (1024 * 1024),
            "cpu_s": _filetime(kernel) + _filetime(user),
        }
    finally:
        k32.CloseHandle(handle)


def listening_pid(port: int) -> int | None:
    """The PID listening on 127.0.0.1:`port` (netstat, read-only), or None."""
    try:
        out = subprocess.run(
            ["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True, timeout=15, check=False
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    pattern = re.compile(rf"^\s*TCP\s+(?:127\.0\.0\.1|0\.0\.0\.0|\[::1?\]):{port}\s+\S+\s+LISTENING\s+(\d+)\s*$")
    for line in out.splitlines():
        match = pattern.match(line)
        if match:
            return int(match.group(1))
    return None


# -- riskguard (read-only) --------------------------------------------------------------


def riskguard_token(environ: dict[str, str]) -> str | None:
    """The first read-capable token in the tokens file, else `RISKGUARD_SERVICE_TOKEN`."""
    path = Path(environ.get("RISKGUARD_SERVICE_TOKENS_FILE") or RISKGUARD_DEFAULT_TOKENS)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        tokens = data.get("tokens", []) if isinstance(data, dict) else []
        for entry in tokens:
            if isinstance(entry, dict) and str(entry.get("role", "")).lower() in _READ_ROLES:
                token = entry.get("token")
                if isinstance(token, str) and token:
                    return token
    except (OSError, ValueError):
        pass
    fallback = environ.get("RISKGUARD_SERVICE_TOKEN")
    return fallback or None


def fetch_fleet(port: int, token: str) -> dict[str, Any]:
    conn = HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.request("GET", "/v1/fleet", headers={"Authorization": f"Bearer {token}"})
        resp = conn.getresponse()
        body = resp.read(8 * 1024 * 1024)
        if resp.status != 200:
            raise RuntimeError(f"riskguard /v1/fleet answered {resp.status}")
        payload = json.loads(body)
        if not isinstance(payload, dict):
            raise RuntimeError("riskguard /v1/fleet is not an object")
        return payload
    finally:
        conn.close()


def fleet_rows(payload: dict[str, Any]) -> list[dict[str, str]]:
    """Every fleet row as `{venue, env, id}`. riskguard's `venue` is the firm (apex, tpt,
    lucid, fundednext, ...), not the broker, so no row is identifiable as Tradovate by
    venue: the match is by broker account id."""
    rows = []
    for row in payload.get("accounts", []):
        key = row.get("accountKey") if isinstance(row, dict) else None
        if isinstance(key, dict):
            rows.append(
                {
                    "venue": str(key.get("venue")),
                    "env": str(key.get("env")),
                    "id": str(key.get("brokerAccountId")),
                }
            )
    return rows


# -- the comparison ---------------------------------------------------------------------


def forms(name: str, ident: str | None) -> set[str]:
    out = {name}
    if ident:
        out.update({ident, "D" + ident})
    return out


def compare(engine: list[tuple[str, str | None, str]], fleet: list[dict[str, str]]) -> dict[str, Any]:
    """Count and key comparison. `engine` is `(name, numeric id, env)`. An engine account
    is matched by any of its name, numeric id or `D<id>` against a fleet row's
    brokerAccountId, in any venue. It must match exactly one row. The count passes when
    every engine account matched one distinct row (so the matched rows equal the engine
    count). Fleet rows that match no engine account are other firms and platforms:
    they cannot be called missing Tradovate rows, so they are counted, not failed."""
    used: set[int] = set()
    unmatched: list[str] = []
    ambiguous: list[str] = []
    env_pairs: dict[tuple[str, str], int] = {}
    for name, ident, env in engine:
        wanted = forms(name, ident)
        hits = [i for i, row in enumerate(fleet) if row["id"] in wanted]
        if not hits:
            unmatched.append(name)
        elif len(hits) > 1:
            ambiguous.append(name)
        else:
            used.add(hits[0])
            pair = (env, fleet[hits[0]]["env"])
            env_pairs[pair] = env_pairs.get(pair, 0) + 1
    matched = len(used)
    return {
        "engine_count": len(engine),
        "fleet_count": len(fleet),
        "matched_rows": matched,
        "count_ok": matched == len(engine),
        "engine_only": unmatched,
        "ambiguous": ambiguous,
        "fleet_unmatched": len(fleet) - matched,
        "keys_ok": not unmatched and not ambiguous,
        "env_pairs": env_pairs,
    }


def _delta(before: dict[str, float] | None, after: dict[str, float] | None) -> dict[str, float] | None:
    if before is None or after is None:
        return None
    return {
        "rss_before_mb": before["rss_mb"],
        "rss_after_mb": after["rss_mb"],
        "rss_delta_mb": after["rss_mb"] - before["rss_mb"],
        "cpu_delta_s": after["cpu_s"] - before["cpu_s"],
    }


def _fmt(label: str, d: dict[str, float] | None, budget: float) -> str:
    if d is None:
        return f"{label}: not measurable"
    verdict = "ok" if d["rss_delta_mb"] <= budget else "OVER BUDGET"
    return (
        f"{label}: rss {d['rss_before_mb']:.1f} -> {d['rss_after_mb']:.1f} MB "
        f"(delta {d['rss_delta_mb']:+.1f} MB, budget +{budget:.0f}), "
        f"cpu delta {d['cpu_delta_s']:+.2f} s [{verdict}]"
    )


def health_gate(client) -> tuple[bool, str]:
    """True only when the daemon answered 200 and every identity is `degraded == false`."""
    status, body = client.health()
    identities = body.get("identities")
    if status != 200 or not isinstance(identities, list) or not identities:
        return False, f"/health status {status}, {len(identities) if isinstance(identities, list) else 0} identities"
    bad = [
        mask(item.get("ident", "?")) if isinstance(item, dict) else "?"
        for item in identities
        if not isinstance(item, dict) or item.get("degraded") is not False
    ]
    if bad:
        return False, f"/health status {status}, degraded identities: {', '.join(bad)}"
    return True, f"/health 200, {len(identities)} identities, none degraded"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--health-only", action="store_true", help="only the /health gate; no /snapshot")
    parser.add_argument("--ntx-port", type=int, default=None)
    args = parser.parse_args(argv)

    from trade_engine.clock.wall import WallClock
    from trade_engine.tradovate.adapter import TradovateAdapter
    from trade_engine.tradovate.ntx_client import (
        DEFAULT_PORT,
        NtxClient,
        NtxError,
        default_token_path,
        read_token,
    )

    # The baseline is taken with the engine modules loaded (a running engine has them), so the
    # delta is the account read itself.
    engine_before = process_stats(os.getpid())

    try:
        token = read_token(default_token_path(dict(os.environ)))
        client = NtxClient(token, port=args.ntx_port or DEFAULT_PORT)
        ok, why = health_gate(client)
    except NtxError as exc:
        print(f"G3 blocked on ntx ({exc.kind})")
        return EXIT_BLOCKED
    print(f"ntx: {why}")
    if not ok:
        print("G3 blocked on ntx 401")
        return EXIT_BLOCKED
    if args.health_only:
        return EXIT_PASS

    rg_port = int(os.environ.get("RISKGUARD_SERVICE_PORT") or RISKGUARD_DEFAULT_PORT)
    rg_pid = listening_pid(rg_port)
    rg_before = process_stats(rg_pid) if rg_pid else None

    adapter = TradovateAdapter(client, clock=WallClock())
    try:
        snapshot = adapter.accounts()
    except Exception as exc:  # noqa: BLE001 - report the kind, never the message (it may carry ids)
        print(f"G3 error: ntx snapshot failed ({type(exc).__name__})")
        return EXIT_ERROR
    engine = [(rec.key.split(":", 1)[1], rec.api_ref, rec.env) for rec in snapshot.records]
    print(f"engine: {len(engine)} accounts, identities {len(adapter.identities())}")
    for rec in snapshot.records:
        print(f"  {rec.env:<5} {mask(rec.key.split(':', 1)[1])}  routes={len(rec.routes)}")

    rg_token = riskguard_token(dict(os.environ))
    if not rg_token:
        print("G3 error: no riskguard read token found")
        return EXIT_ERROR
    try:
        fleet = fleet_rows(fetch_fleet(rg_port, rg_token))
    except Exception as exc:  # noqa: BLE001
        print(f"G3 error: riskguard fleet read failed ({type(exc).__name__})")
        return EXIT_ERROR
    result = compare(engine, fleet)
    print(f"riskguard: {result['fleet_count']} fleet rows, {result['matched_rows']} matched to an ntx account")
    print(
        f"count: {'PASS' if result['count_ok'] else 'FAIL'} "
        f"(engine {result['engine_count']}, matched fleet rows {result['matched_rows']})"
    )
    print(f"keys:  {'PASS' if result['keys_ok'] else 'FAIL'}")
    for name in result["engine_only"]:
        print(f"  engine only (no fleet row): {mask(name)}")
    for name in result["ambiguous"]:
        print(f"  ambiguous (several fleet rows): {mask(name)}")
    print(f"  fleet rows with no ntx account (other firms/platforms, not failed): {result['fleet_unmatched']}")
    for (eng_env, rg_env), n in sorted(result["env_pairs"].items()):
        print(f"  env engine={eng_env} riskguard={rg_env}: {n} (informational)")

    engine_after = process_stats(os.getpid())
    rg_after = process_stats(rg_pid) if rg_pid else None
    engine_d = _delta(engine_before, engine_after)
    rg_d = _delta(rg_before, rg_after)
    print(_fmt("engine account plane", engine_d, ENGINE_BUDGET_MB))
    print(_fmt("riskguard", rg_d, RISKGUARD_BUDGET_MB))

    if not (result["count_ok"] and result["keys_ok"]):
        return EXIT_MISMATCH
    over = (engine_d and engine_d["rss_delta_mb"] > ENGINE_BUDGET_MB) or (
        rg_d and rg_d["rss_delta_mb"] > RISKGUARD_BUDGET_MB
    )
    return EXIT_BUDGET if over else EXIT_PASS


if __name__ == "__main__":
    sys.exit(main())
